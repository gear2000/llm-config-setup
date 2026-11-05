#!/usr/bin/env python3
"""Linux-only, single-task subreaper. No Recruiter, Herdr, or LLM helper imports.

Both launch boundaries are gated: the guardian waits for its durably recorded identity,
then the forked exec child waits until its birth identity is journaled. A lost supervisor
or fenced lease stops work. Only descendants of this dedicated subreaper receive signals.
A guardian crash is NOT proof of descendant cleanup; recovery reports that uncertainty.
"""

from __future__ import annotations

import ctypes
import json
import os
import select
import signal
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

LOG_LIMIT = 16 * 1024 * 1024
POLL_SECONDS = 0.05
GATE_SECONDS = 10.0


def write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(f".{os.getpid()}.tmp")
    with temporary.open("w") as stream:
        os.chmod(temporary, 0o600)
        json.dump(value, stream, sort_keys=True)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)
    fd = os.open(path.parent, os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text())
    if not isinstance(value, dict):
        raise ValueError(f"expected an object: {path}")
    return value


def boot_id() -> str:
    return Path("/proc/sys/kernel/random/boot_id").read_text().strip()


def proc(pid: int) -> dict[str, Any] | None:
    try:
        text = Path(f"/proc/{pid}/stat").read_text()
    except (FileNotFoundError, ProcessLookupError):
        return None
    fields = text[text.rfind(")") + 2 :].split()
    return {"pid": pid, "start": fields[19], "ppid": int(fields[1]), "state": fields[0]}


def identity(pid: int) -> dict[str, Any]:
    current = proc(pid)
    if current is None:
        raise RuntimeError(f"process {pid} vanished before identity recording")
    return {"pid": pid, "start": current["start"], "boot_id": boot_id()}


def validate_identity(record: dict[str, Any]) -> None:
    if (
        isinstance(record.get("pid"), bool)
        or not isinstance(record.get("pid"), int)
        or record["pid"] <= 1
        or not isinstance(record.get("start"), str)
        or not record["start"]
        or not isinstance(record.get("boot_id"), str)
        or not record["boot_id"]
    ):
        raise RuntimeError(
            "headless process requires a complete PID/birth/boot identity"
        )


@contextmanager
def pinned_identity(record: dict[str, Any]) -> Iterator[int | None]:
    """Pin before probing; both liveness and signals refer to the same kernel process.

    Missing, exited, previous-boot, or reused identities are absent. Other OS failures
    propagate: an unreadable identity is not proof of absence.
    """
    validate_identity(record)
    if record["boot_id"] != boot_id():
        yield None
        return
    try:
        fd = os.pidfd_open(record["pid"])
    except ProcessLookupError:
        yield None
        return
    try:
        current = proc(record["pid"])
        poller = select.poll()
        poller.register(fd, select.POLLIN)
        if (
            current is None
            or current["state"] == "Z"
            or current["start"] != record["start"]
            or poller.poll(0)
        ):
            yield None
        else:
            yield fd
    finally:
        os.close(fd)


def alive(record: dict[str, Any]) -> bool:
    with pinned_identity(record) as fd:
        return fd is not None


def stop_reason(config: dict[str, Any]) -> str | None:
    if config["boot_id"] != boot_id():
        return "host rebooted"
    if time.monotonic() >= config["deadline"]:
        return "total headless deadline exceeded"
    if not alive(config["runner"]):
        return "supervisor disappeared"
    try:
        lease = read_json(Path(config["lease_path"]))
    except FileNotFoundError:
        return "lease disappeared"
    if lease["token"] != config["token"]:
        return "lease fenced or requester cancelled"
    if lease["expires_at"] <= time.time():
        return "lease expired"
    return None


def descendants(root: int) -> list[dict[str, Any]]:
    processes = []
    for path in Path("/proc").iterdir():
        if path.name.isdecimal():
            current = proc(int(path.name))
            if current is not None:
                processes.append(current)
    owned = {root}
    while True:
        found = {item["pid"] for item in processes if item["ppid"] in owned}
        if found <= owned:
            break
        owned |= found
    return [item for item in processes if item["pid"] in owned and item["pid"] != root]


def reap() -> dict[int, int]:
    statuses = {}
    while True:
        try:
            pid, status = os.waitpid(-1, os.WNOHANG)
        except ChildProcessError:
            return statuses
        if pid == 0:
            return statuses
        statuses[pid] = os.waitstatus_to_exitcode(status)


def signal_identity(record: dict[str, Any], sig: int) -> bool:
    """Pin the kernel process before rechecking birth; PID reuse cannot redirect a signal."""
    with pinned_identity(record) as fd:
        if fd is None:
            return False
        try:
            signal.pidfd_send_signal(fd, sig)
        except ProcessLookupError:
            return False
        return True


def clean_children() -> dict[str, Any]:
    deadline = time.monotonic() + 4
    term_until = time.monotonic() + 0.5
    signalled: list[dict[str, Any]] = []
    errors: list[str] = []
    while time.monotonic() < deadline:
        reap()
        owned = descendants(os.getpid())
        if not owned:
            return {
                "status": "closed",
                "verified_absent": True,
                "signalled": signalled,
                "errors": errors,
            }
        for item in reversed(owned):
            # Recheck birth and ancestry immediately before each individual signal. Never
            # kill a numeric process group (it might include unproven/reused processes).
            current = proc(item["pid"])
            if (
                current is None
                or current["start"] != item["start"]
                or current["state"] == "Z"
            ):
                continue
            if item["pid"] not in {child["pid"] for child in descendants(os.getpid())}:
                continue
            sig = signal.SIGTERM if time.monotonic() < term_until else signal.SIGKILL
            try:
                sent = signal_identity({**item, "boot_id": boot_id()}, sig)
            except OSError as error:
                errors.append(str(error))
                continue
            if sent:
                signalled.append(
                    {"pid": item["pid"], "start": item["start"], "signal": sig}
                )
        time.sleep(POLL_SECONDS)
    reap()
    remaining = descendants(os.getpid())
    return {
        "status": "cleanup-failed",
        "verified_absent": False,
        "remaining": remaining,
        "errors": errors,
    }


def exec_child(
    config: dict[str, Any], gate: int, stdout_fd: int, stderr_fd: int
) -> None:
    # EOF means the guardian died before durable ownership was recorded.
    released = os.read(gate, 1)
    os.close(gate)
    if released != b"G":
        os._exit(125)
    os.setsid()
    with open(config["stdin"], "rb") as source:
        os.dup2(source.fileno(), 0)
    os.dup2(stdout_fd, 1)
    os.dup2(stderr_fd, 2)
    os.close(stdout_fd)
    os.close(stderr_fd)
    os.chdir(config["cwd"])
    environment = dict(os.environ)
    for key in list(environment):
        if key.startswith("HERDR_"):
            del environment[key]
    environment.update(config["env"])
    try:
        os.execve(config["argv"][0], config["argv"], environment)
    except OSError as error:
        os.write(2, f"headless exec failed: {error}\n".encode())
        os._exit(127)


def drain(
    streams: dict[int, tuple[Any, int]], counts: dict[str, int], *, wait: float
) -> None:
    """Consume beyond the retention limit too; never backpressure the worker."""
    readable, _, _ = select.select(list(streams), [], [], wait)
    for fd in readable:
        chunk = os.read(fd, 65536)
        sink, count = streams[fd]
        if not chunk:
            os.close(fd)
            sink.close()
            del streams[fd]
            continue
        sink.write(chunk[: max(0, LOG_LIMIT - count)])
        counts[str(sink.name)] = count + len(chunk)
        streams[fd] = (sink, count + len(chunk))


def guard(config_path: Path) -> int:
    if sys.platform != "linux":
        raise RuntimeError("headless execution requires Linux")
    # Fail before any Claude launch if the kernel cannot provide race-free signaling.
    fd = os.pidfd_open(os.getpid())
    os.close(fd)
    os.umask(0o077)
    config = read_json(config_path)
    directory = config_path.parent
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(36, 1, 0, 0, 0) != 0:  # PR_SET_CHILD_SUBREAPER
        raise OSError(ctypes.get_errno(), "cannot enable headless child subreaper")
    own = identity(os.getpid())
    write_json(directory / "ready.json", own)
    gate_deadline = time.monotonic() + GATE_SECONDS
    reason = stop_reason(config)
    while reason is None and time.monotonic() < gate_deadline:
        if (directory / "go.json").exists():
            if read_json(directory / "go.json") != own:
                raise RuntimeError("headless guardian gate identity mismatch")
            break
        time.sleep(POLL_SECONDS)
        reason = stop_reason(config)
    else:
        if reason is None:
            reason = "guardian launch gate expired"
    with (
        open(config["stdout"], "wb", buffering=0) as output,
        open(config["stderr"], "wb", buffering=0) as errors,
    ):
        return supervise(config, directory, own, reason, output, errors)


def supervise(
    config: dict[str, Any],
    directory: Path,
    own: dict[str, Any],
    reason: str | None,
    output: Any,
    errors: Any,
) -> int:
    returncode = None
    worker = None
    streams: dict[int, tuple[Any, int]] = {}
    counts: dict[str, int] = {}
    try:
        if reason is None:
            reader, writer = os.pipe()
            out_r, out_w = os.pipe()
            err_r, err_w = os.pipe()
            pid = os.fork()
            if pid == 0:
                os.close(writer)
                os.close(out_r)
                os.close(err_r)
                try:
                    exec_child(config, reader, out_w, err_w)
                finally:
                    # Never unwind the fork child through the guardian's cleanup or
                    # publication finally block, even if setup before exec fails.
                    os._exit(126)
            os.close(reader)
            os.close(out_w)
            os.close(err_w)
            streams[out_r] = (output, 0)
            streams[err_r] = (errors, 0)
            try:
                worker = identity(pid)
                write_json(directory / "worker.json", worker)
                # The durable identity precedes the first possible Claude instruction.
                if stop_reason(config) is None:
                    os.write(writer, b"G")
            finally:
                os.close(writer)
            while True:
                statuses = reap()
                # Completion observed after the deadline cannot outrank the deadline,
                # even when stdout/artifacts already look final and exit status is zero.
                reason = stop_reason(config)
                if pid in statuses:
                    returncode = statuses[pid]
                    break
                if reason is not None:
                    break
                drain(streams, counts, wait=POLL_SECONDS)
    finally:
        cleanup = clean_children()
        # After cleanup remaining bytes are finite. Bound this drain if an unkillable
        # child still owns a writer; absence remains false in that case.
        drain_deadline = time.monotonic() + 1
        while streams and time.monotonic() < drain_deadline:
            drain(streams, counts, wait=POLL_SECONDS)
        for fd, (sink, _count) in streams.items():
            os.close(fd)
            sink.close()
        write_json(
            directory / "exit.json",
            {
                "token": config["token"],
                "guardian": own,
                "worker": worker,
                "returncode": returncode,
                "reason": reason,
                "cleanup": cleanup,
                "output_bytes": counts,
                "log_limit_bytes": LOG_LIMIT,
            },
        )
    return 0 if cleanup["verified_absent"] else 1


if __name__ == "__main__":
    raise SystemExit(guard(Path(sys.argv[1])))
