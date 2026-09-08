"""Flow 1 management adapter. Run-local ownership, no hub leases or ledgers.

Python owns health, nudges and cleanup. Management output is advisory. Every
cleanup compares the recorded pane/agent and process birth before acting.
"""

from __future__ import annotations

import importlib.util
import json
import os
import shlex
import signal
import subprocess
import sys
import time
import uuid
from contextvars import ContextVar
from functools import wraps
from pathlib import Path
from typing import Any


def _load(name: str) -> Any:
    key = "upagent_flow1_" + name
    if key in sys.modules:
        return sys.modules[key]
    spec = importlib.util.spec_from_file_location(
        key, Path(__file__).with_name(name + ".py")
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[key] = module
    spec.loader.exec_module(module)
    return module


management = _load("llm_management")
process = _load("process_identity")
sentinel_contracts = _load("contracts_sentinel")
BUDGET_SECONDS = 480
CLEANUP_SECONDS = 60
SPAWN_SECONDS = 10
RPC_SECONDS = 3
_deadline: ContextVar[float | None] = ContextVar("flow1_deadline", default=None)


def bounded(function: Any) -> Any:
    """One wall-clock budget, including time waiting for the operation lock."""

    @wraps(function)
    def invoke(*args: Any, **kwargs: Any) -> Any:
        token = _deadline.set(time.monotonic() + BUDGET_SECONDS)
        try:
            return function(*args, **kwargs)
        finally:
            _deadline.reset(token)

    return invoke


def remaining() -> float:
    deadline = _deadline.get()
    return BUDGET_SECONDS if deadline is None else max(0, deadline - time.monotonic())


def allocation(seconds: float, *, reserve: float = 0) -> float:
    available = min(seconds, remaining() - reserve)
    if available <= 0:
        raise SupervisionError("Flow 1 operation exhausted its 480-second budget")
    return available


class SupervisionError(RuntimeError):
    """Mechanical verification, deadline or ownership failure."""


def read(path: Path) -> dict:
    value = json.loads(path.read_text())
    if not isinstance(value, dict):
        raise SupervisionError(f"{path} must contain an object")
    return value


def write(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name("." + path.name + "." + uuid.uuid4().hex)
    with tmp.open("x") as stream:
        json.dump(value, stream, indent=2, sort_keys=True)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(tmp, path)
    fd = os.open(path.parent, os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def finish_budget(policy: dict) -> float:
    policy = _load("offerings").validate_run_watch(policy)
    wait = policy["drain_minutes"] * 60 + 60
    if wait + CLEANUP_SECONDS > BUDGET_SECONDS:
        raise SupervisionError(
            "finish drain plus margin and cleanup exceeds 480 seconds"
        )
    return wait


def start_budget(roster: dict, supervise: bool) -> dict:
    policy = _load("offerings").validate_run_watch(roster.get("run_watch", {}))
    finish_budget(policy)
    config = management.load_management_config(roster)
    manager = (
        max(
            [config.account_manager.timeout_ms]
            + [c.role.timeout_ms for c in config.account_manager_candidates]
        )
        / 1000
    )
    sentinel = (
        max(
            [config.sentinel.timeout_ms]
            + [r.timeout_ms for r in config.sentinels.values()]
            + [c.role.timeout_ms for c in config.sentinel_candidates]
        )
        / 1000
    )
    total = (
        45 + CLEANUP_SECONDS + (manager + sentinel + SPAWN_SECONDS if supervise else 0)
    )
    if total > BUDGET_SECONDS:
        raise SupervisionError(
            "start health, manager, Sentinel landing and spawn exceed 480 seconds"
        )
    return {
        "manager_seconds": manager,
        "sentinel_seconds": sentinel,
        "total_seconds": total,
        "run_watch": policy,
    }


class Panes:
    def __init__(self, recruiter: Any, receipt: dict):
        self.recruiter = recruiter
        self.receipt = receipt

    def rpc(self, *args: str) -> dict:
        return self.recruiter._herdr_json(
            *args,
            herdr_session=self.receipt["herdr_session"],
            timeout_seconds=allocation(RPC_SECONDS),
        )

    def inspect(self, owner: dict) -> tuple[dict, dict, list]:
        pane = self.rpc("pane", "get", owner["pane_id"])["result"]["pane"]
        agent = self.rpc("agent", "get", owner["agent_name"])["result"]["agent"]
        processes = self.rpc("pane", "process-info", "--pane", owner["pane_id"])[
            "result"
        ]["process_info"].get("foreground_processes", [])
        return pane, agent, processes

    def capture(self, pane_id: str, name: str) -> dict:
        owner = {
            "pane_id": pane_id,
            "agent_name": name,
            "workspace_id": self.receipt["workspace_id"],
            "herdr_session": self.receipt["herdr_session"],
        }
        pane, agent, processes = self.inspect(owner)
        if not self.matches(owner, pane, agent):
            raise SupervisionError("pane/agent launch identity mismatch")
        if not processes:
            raise SupervisionError("launched pane has no foreground process identity")
        foreground = next(
            (
                item
                for item in processes
                if self.recruiter._matches_expected_process(
                    item, (pane.get("agent") or "")
                )
            ),
            processes[0],
        )
        pid = foreground.get("pid")
        stamp = process.process_start_time(pid)
        argv = process.process_cmdline(pid)
        if stamp is None or not argv:
            raise SupervisionError("launched process has no birth/argv identity")
        owner.update(pid=pid, process_start_time=stamp, argv=argv, argv_marker=argv[0])
        return owner

    def refresh_launch(self, owner: dict) -> dict:
        """A startup shell may exec its harness before health fails.

        Refresh argv only inside that startup transaction, and only for the
        same pane/agent and exact process birth. Finish never uses this rule.
        """
        current = self.capture(owner["pane_id"], owner["agent_name"])
        if (
            current["pid"] != owner["pid"]
            or current["process_start_time"] != owner["process_start_time"]
        ):
            raise SupervisionError("startup process ownership changed")
        return current

    @staticmethod
    def matches(owner: dict, pane: dict, agent: dict) -> bool:
        return (
            pane.get("pane_id") == owner.get("pane_id")
            and pane.get("workspace_id") == owner.get("workspace_id")
            and agent.get("pane_id") == owner.get("pane_id")
            and agent.get("workspace_id") == owner.get("workspace_id")
            and agent.get("name") == owner.get("agent_name")
        )

    def close(self, owner: dict | None, target: str) -> dict:
        if not owner:
            return {"target": target, "status": "not-started"}
        if owner.get("pane_id") == self.receipt.get("hil_pane"):
            return {
                "target": target,
                "status": "cleanup-failed",
                "message": "refusing to close HIL pane",
            }
        try:
            pane, agent, processes = self.inspect(owner)
        except (self.recruiter.RecruiterError, SupervisionError) as error:
            if "pane_not_found" in str(error):
                return {"target": target, "status": "already-gone"}
            return {"target": target, "status": "cleanup-failed", "message": str(error)}
        if (
            not self.matches(owner, pane, agent)
            or owner.get("herdr_session") != self.receipt["herdr_session"]
        ):
            return {
                "target": target,
                "status": "cleanup-failed",
                "message": "pane identity mismatch",
            }
        stamp = process.process_start_time(owner.get("pid"))
        # An exited process may leave an owned, empty PTY. A replacement process
        # or a reused PID never authorizes closing it.
        if stamp is None:
            verified = not processes and bool(owner.get("process_start_time"))
        else:
            verified = (
                stamp == owner.get("process_start_time")
                and process.process_cmdline(owner.get("pid")) == owner.get("argv")
                and any(p.get("pid") == owner.get("pid") for p in processes)
            )
        if not verified:
            return {
                "target": target,
                "status": "cleanup-failed",
                "message": "process birth/argv identity mismatch",
            }
        try:
            self.rpc("pane", "close", owner["pane_id"])
            live = self.rpc("pane", "list")["result"]["panes"]
        except (self.recruiter.RecruiterError, SupervisionError) as error:
            return {"target": target, "status": "cleanup-failed", "message": str(error)}
        if any(p.get("pane_id") == owner["pane_id"] for p in live):
            return {
                "target": target,
                "status": "cleanup-failed",
                "message": "pane remains after close",
            }
        return {"target": target, "status": "closed", "pane_id": owner["pane_id"]}

    def verify_start(self, receipt: dict, script: Path, launch: str) -> dict:
        owner = self.capture(receipt["implementer_pane"], receipt["agent_name"])
        pane, _, processes = self.inspect(owner)
        health = receipt["health"]
        if (
            not health.get("healthy")
            or pane.get("agent") != health["expected_agent"]
            or Path(pane.get("foreground_cwd", pane.get("cwd", ""))).resolve()
            != Path(receipt["cwd"]).resolve()
            or not any(
                self.recruiter._matches_expected_process(p, health["expected_process"])
                for p in processes
            )
        ):
            raise SupervisionError(
                "mechanical process/cwd/harness verification mismatch"
            )
        words = shlex.split(launch)
        if receipt["model"] not in words or not any(
            receipt["effort"] == w or w == "model_reasoning_effort=" + receipt["effort"]
            for w in words
        ):
            raise SupervisionError("offering/effort launch rendering mismatch")
        text = script.read_text()
        if (
            shlex.quote("cd " + shlex.quote(receipt["cwd"]) + " && " + launch)
            not in text
        ):
            raise SupervisionError("start.sh launch rendering mismatch")
        return owner


def order_for(receipt: dict) -> dict:
    result = {key: receipt[key] for key in ("harness", "model", "effort", "cwd")}
    result.update(order_id="flow1-" + receipt["run_id"], agent="plan-implementer")
    if "offering_snapshot" in receipt:
        result["offering_snapshot"] = _load("offerings").validate_snapshot(
            receipt["offering_snapshot"]
        )
    return result


def advisory(
    receipt_path: Path, key: str, message: str, kind: str = "advisory"
) -> None:
    awaiter = _load("implementer_await")
    ctx = awaiter.ImplementerContext(receipt_path, allow_incomplete=True)
    awaiter.publish_event(ctx, kind, message, severity="attention", dedupe_key=key)


def cleanup_event(receipt_path: Path, entry: dict) -> None:
    if entry["status"] == "cleanup-failed":
        advisory(
            receipt_path,
            "flow1:cleanup-failed:" + entry["target"],
            json.dumps(entry, sort_keys=True),
        )


def launch_role(
    panes: Panes,
    role: Any,
    directory: Path,
    brief: str,
    output: Path,
    *,
    number: int = 1,
) -> dict:
    directory.mkdir(parents=True, exist_ok=True)
    brief_path = directory / "brief.md"
    brief_path.write_text(brief)
    command = management.render_role_command(
        role, brief_path, panes.receipt["cwd"], output
    )
    name = "flow1-" + directory.name + "-" + uuid.uuid4().hex[:16]
    launch = {
        "state": "launching",
        "agent_name": name,
        "herdr_session": panes.receipt["herdr_session"],
        "workspace_id": panes.receipt["workspace_id"],
        "request_id": panes.receipt["run_id"],
        "order_id": "flow1-" + panes.receipt["run_id"],
        "generation": 1,
        "attempt": 1,
        "landing": number,
        "command": command,
    }
    write(directory / "launch.json", launch)
    anchor = panes.rpc("pane", "get", panes.receipt["implementer_pane"])["result"][
        "pane"
    ]
    response = panes.rpc(
        "agent",
        "start",
        name,
        "--cwd",
        panes.receipt["cwd"],
        "--tab",
        anchor["tab_id"],
        "--split",
        "down",
        "--no-focus",
        "--",
        "bash",
        "-lc",
        command,
    )
    launch["pane_id"] = response["result"]["agent"]["pane_id"]
    write(directory / "launch.json", launch)
    owner = panes.capture(launch["pane_id"], name)
    launch.update(ownership=owner, state="started")
    write(directory / "launch.json", launch)
    return launch


def manager_start(
    panes: Panes, path: Path, config: Any, seconds: float
) -> tuple[dict, str | None]:
    directory = path.parent / "manager"
    output = directory / "decision.json"
    order = order_for(panes.receipt)
    candidate = panes.recruiter._account_manager_candidates(order, config)[0]
    brief = (
        management.account_manager_brief(
            panes.receipt["run_id"],
            1,
            order,
            output,
            {"valid": True, "errors": [], "health": panes.receipt["health"]},
        )
        + "\nWrite the one advisory, then exit.\n"
    )
    deadline = time.monotonic() + allocation(seconds, reserve=CLEANUP_SECONDS)
    launch = launch_role(panes, candidate.role, directory, brief, output)
    # Bind cleanup to the actual role process after its shell has exec'd.
    # Both health and output polling spend the same manager deadline.
    try:
        panes.recruiter._wait_for_agent_health(
            launch["pane_id"],
            expected_agent=candidate.role.expected_agent,
            expected_process=candidate.role.expected_process,
            expected_cwd=panes.receipt["cwd"],
            timeout_ms=max(1, int((deadline - time.monotonic()) * 1000)),
            herdr_session=panes.receipt["herdr_session"],
        )
    except panes.recruiter.RecruiterError as error:
        launch["health_warning"] = str(error)
        launch["ownership"] = panes.refresh_launch(launch["ownership"])
        write(directory / "launch.json", launch)
    else:
        launch["ownership"] = panes.capture(launch["pane_id"], launch["agent_name"])
        write(directory / "launch.json", launch)
    decision = None
    message = "Account Manager timed out without valid output"
    while True:
        if output.exists():
            try:
                decision = panes.recruiter.lifecycle.parse_manager_decision(
                    output.read_text(), panes.receipt["run_id"], 1
                )
            except panes.recruiter.lifecycle.LifecycleError as error:
                message = f"Account Manager invalid output: {error}"
            else:
                message = decision.message
            break
        if time.monotonic() >= deadline:
            break
        time.sleep(min(0.25, max(0, deadline - time.monotonic())))
    cleanup = panes.close(launch["ownership"], "manager")
    launch.update(
        decision=decision.decision if decision else "degraded",
        message=message,
        cleanup=cleanup,
        state="closed" if cleanup["status"] != "cleanup-failed" else "cleanup-failed",
    )
    write(directory / "launch.json", launch)
    return launch, None if decision and decision.decision == "approved" else message


def sentinel_start(
    panes: Panes, path: Path, config: Any, seconds: float, *, number: int = 1
) -> dict:
    directory = path.parent / "sentinel"
    output = directory / "closeout-a1.json"
    brief = management.sentinel_brief(
        panes.receipt["run_id"],
        "flow1-" + panes.receipt["run_id"],
        panes.receipt["implementer_pane"],
        panes.receipt["cwd"],
        output,
        liftoff_deadline_ms=45000,
        wake_path=directory / "wake",
    )
    brief += (
        "\nFlow 1: the sole terminal bundle is "
        + str(path.parent.parent / "implementer-result.json")
        + ". Python validates its run identity. Generation 1, attempt 1. Your lease ends when this run finishes.\n"
    )
    selections = panes.recruiter._resolve_sentinel_roles(
        order_for(panes.receipt), config
    )
    deadline = time.monotonic() + allocation(seconds, reserve=CLEANUP_SECONDS)
    failures = []
    for selection in selections:
        if time.monotonic() >= deadline:
            break
        launch = launch_role(
            panes, selection.role, directory, brief, output, number=number
        )
        try:
            health = panes.recruiter._wait_for_agent_health(
                launch["pane_id"],
                expected_agent=selection.role.expected_agent,
                expected_process=selection.role.expected_process,
                expected_cwd=panes.receipt["cwd"],
                timeout_ms=max(1, int((deadline - time.monotonic()) * 1000)),
                herdr_session=panes.receipt["herdr_session"],
            )
        except panes.recruiter.RecruiterError as error:
            launch["ownership"] = panes.refresh_launch(launch["ownership"])
            cleanup = panes.close(launch["ownership"], "sentinel")
            failures.append(str(error))
            launch.update(state="failed", cleanup=cleanup, failures=failures)
            write(directory / "launch.json", launch)
            if cleanup["status"] == "cleanup-failed":
                raise SupervisionError(json.dumps(cleanup)) from error
            continue
        launch.update(
            ownership=panes.capture(launch["pane_id"], launch["agent_name"]),
            provider=selection.sentinel_provider,
            health=health,
            failures=failures,
        )
        write(directory / "launch.json", launch)
        return launch
    raise SupervisionError(
        "Sentinel landing candidates exhausted: " + "; ".join(failures)
    )


def spawn_watch(path: Path, roster_path: str) -> dict:
    log = path.parent / "run-watch.log"
    with log.open("a") as stream:
        child = subprocess.Popen(
            [
                sys.executable,
                str(Path(__file__).with_name("run_watch.py")),
                str(path.parent.parent),
                "--roster",
                roster_path,
            ],
            stdin=subprocess.DEVNULL,
            stdout=stream,
            stderr=stream,
            start_new_session=True,
        )
    owner = {
        "pid": child.pid,
        "process_start_time": process.process_start_time(child.pid),
        "argv_marker": "run_watch.py",
        "argv": process.process_cmdline(child.pid),
        "herdr_session": read(path)["herdr_session"],
        "workspace_id": read(path)["workspace_id"],
        "pane_id": None,
        "agent_name": "run-watch",
    }
    write(path.parent / "run-watch-launch.json", {"ownership": owner})
    deadline = time.monotonic() + allocation(SPAWN_SECONDS, reserve=CLEANUP_SECONDS)
    status = path.parent / "run-watch.json"
    while time.monotonic() < deadline:
        if status.exists():
            data = read(status)
            if data.get("ownership", {}).get("pid") == child.pid:
                return owner
        if child.poll() is not None:
            raise SupervisionError("run-watch exited during startup; see run-watch.log")
        time.sleep(0.1)
    raise SupervisionError("run-watch did not publish ownership within spawn budget")


def start(
    path: Path,
    receipt: dict,
    roster: dict,
    roster_path: str,
    recruiter: Any,
    budget: dict,
) -> dict:
    panes = Panes(recruiter, receipt)
    config = management.load_management_config(roster)
    messages = []
    for target, action in (
        (
            "manager",
            lambda: manager_start(panes, path, config, budget["manager_seconds"]),
        ),
        (
            "sentinel",
            lambda: (
                sentinel_start(panes, path, config, budget["sentinel_seconds"]),
                None,
            ),
        ),
    ):
        try:
            launch, message = action()
        except (SupervisionError, recruiter.RecruiterError) as error:
            message = f"{target}: {error}"
            launch_path = path.parent / target / "launch.json"
            launch = (
                read(launch_path) if launch_path.exists() else {"state": "degraded"}
            )
            if launch.get("ownership"):
                launch["cleanup"] = panes.close(launch["ownership"], target)
            elif launch.get("pane_id") or launch.get("state") == "launching":
                launch["cleanup"] = {
                    "target": target,
                    "status": "cleanup-failed",
                    "message": "partial launch has no verified process ownership",
                }
            launch["state"] = "degraded"
            if launch_path.exists():
                write(launch_path, launch)
            if target == "sentinel":
                advisory(path, "flow1:sentinel-degraded", message)
        receipt[target] = launch
        receipt["ownership"][target] = launch.get("ownership")
        write(path, receipt)
        if message:
            messages.append(message)
        if launch.get("cleanup"):
            cleanup_event(path, launch["cleanup"])
    if budget["run_watch"]["enabled"]:
        try:
            receipt["ownership"]["run-watch"] = spawn_watch(path, roster_path)
        except SupervisionError as error:
            messages.append(str(error))
            launch_path = path.parent / "run-watch-launch.json"
            if launch_path.exists():
                receipt["ownership"]["run-watch"] = read(launch_path)["ownership"]
    if messages:
        receipt.update(state="ready-degraded", startup_advisory="\n".join(messages))
        write(path, receipt)
        advisory(
            path,
            "flow1:startup-degraded",
            receipt["startup_advisory"],
            "startup-degraded",
        )
    write(path, receipt)
    return receipt


def stop_watch(path: Path, receipt: dict, wait_seconds: float, deadline: float) -> dict:
    owner = receipt.get("ownership", {}).get("run-watch")
    if not owner:
        launch = path.parent / "run-watch-launch.json"
        owner = read(launch).get("ownership") if launch.exists() else None
    if not owner:
        return {"target": "run-watch", "status": "not-started"}
    status_path = path.parent / "run-watch.json"
    current = read(status_path) if status_path.exists() else {}

    def terminal(data: dict) -> bool:
        identity = data.get("ownership", {})
        return (
            data.get("state") in ("finished", "drain-timeout")
            and identity.get("pid") == owner.get("pid")
            and identity.get("process_start_time") == owner.get("process_start_time")
            and identity.get("herdr_session") == owner.get("herdr_session")
        )

    if terminal(current):
        return {
            "target": "run-watch",
            "status": current["state"],
            "remaining_request_ids": current.get("remaining_request_ids", []),
        }
    current_owner = current.get("ownership", {})
    if current_owner and any(
        current_owner.get(key) != owner.get(key)
        for key in ("pid", "process_start_time", "herdr_session")
    ):
        return {
            "target": "run-watch",
            "status": "cleanup-failed",
            "message": "run-watch status ownership mismatch",
        }
    pid = owner.get("pid")
    stamp = process.process_start_time(pid)
    if (
        not stamp
        or stamp != owner.get("process_start_time")
        or process.process_cmdline(pid) != owner.get("argv")
        or owner.get("herdr_session") != receipt.get("herdr_session")
        or not any(
            Path(word).name == "run_watch.py" or word == "run-watch"
            for word in owner.get("argv", [])
        )
    ):
        return {
            "target": "run-watch",
            "status": "cleanup-failed",
            "message": "run-watch process birth/argv/session identity mismatch",
        }
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return {
            "target": "run-watch",
            "status": "cleanup-failed",
            "message": "run-watch disappeared before drain signal",
        }
    until = min(time.monotonic() + wait_seconds, deadline - 15)
    while time.monotonic() < until:
        current = read(status_path) if status_path.exists() else {}
        if terminal(current):
            return {
                "target": "run-watch",
                "status": current["state"],
                "remaining_request_ids": current.get("remaining_request_ids", []),
            }
        time.sleep(min(0.25, max(0, until - time.monotonic())))
    return {
        "target": "run-watch",
        "status": "cleanup-failed",
        "message": "run-watch drain wait expired",
    }


def finish(
    path: Path,
    receipt: dict,
    force: bool,
    recruiter: Any,
    contracts: Any,
    exclusive: Any,
) -> dict:
    wait_seconds = finish_budget(receipt.get("run_watch", {}))
    deadline = time.monotonic() + remaining()
    root = path.parent.parent
    cursor_path = path.parent / "finishing.json"
    finish_path = path.parent / "implementer-finish.json"
    panes = Panes(recruiter, receipt)
    with exclusive(root / ".implementer-finish.lock"):
        cursor = (
            read(cursor_path)
            if cursor_path.exists()
            else {"step": 0, "cleanup": [], "run_id": receipt["run_id"]}
        )
        if cursor.get("run_id") != receipt["run_id"]:
            raise SupervisionError("finish cursor run identity mismatch")
        if cursor["step"] >= 7:
            return read(finish_path)
        if cursor["step"] < 1:
            cursor.update(step=1)
            if (
                receipt.get("implementer_pane")
                and receipt.get("workspace_id")
            ):
                rw = _load("run_watch")
                runtime = rw.Runtime(
                    _load("implementer_await").ImplementerContext(
                        path, allow_incomplete=True
                    ),
                    {},
                )
                target = runtime.implementer()
                # Linearize finishing with every trigger's final probe and send.
                # Once this marker is visible no in-flight authority send remains.
                with exclusive(
                    runtime.nudges.state_path(target.identity).with_suffix(".lock")
                ):
                    write(cursor_path, cursor)
            else:
                write(cursor_path, cursor)
        if cursor["step"] < 2:
            try:
                result = contracts.parse_implementer_result(
                    (root / "implementer-result.json").read_text(),
                    expected_run_root=root,
                    expected_run_id=receipt["run_id"],
                )
            except (OSError, contracts.ContractError) as error:
                if not force:
                    raise SupervisionError(
                        f"finish requires a valid implementer-result.json: {error}"
                    ) from error
                result = None
            cursor.update(
                step=2,
                result=result,
                verdict=result["verdict"] if result else "cancelled",
                forced=bool(force),
                force=bool(force),
                hil_pane=receipt.get("hil_pane"),
            )
            write(cursor_path, cursor)
        # Repair a shell interruption after the validated-result cursor was
        # committed but before its terminal journal event was written.
        if cursor["result"] is None:
            advisory(
                path,
                "flow1:forced-finish",
                "Implementer run cancelled by forced finish",
                "cancelled",
            )
        else:
            awaiter = _load("implementer_await")
            verdict = cursor["result"]["verdict"]
            awaiter.publish_event(
                awaiter.ImplementerContext(path, allow_incomplete=True),
                {"passed": "completed", "failed": "failed", "blocked": "blocked"}[
                    verdict
                ],
                f"Plan-implementer result: verdict={verdict}",
                dedupe_key=f"implementer-result:{verdict}",
            )
        if cursor["step"] < 3:
            for target in ("sentinel", "manager"):
                if any(e["target"] == target for e in cursor["cleanup"]):
                    continue
                launch_path = path.parent / target / "launch.json"
                launch = read(launch_path) if launch_path.exists() else {}
                owner = launch.get("ownership") or receipt.get("ownership", {}).get(
                    target
                )
                if not owner and (
                    launch.get("pane_id")
                    or launch.get("cleanup", {}).get("status") == "cleanup-failed"
                ):
                    entry = {
                        "target": target,
                        "status": "cleanup-failed",
                        "message": "partial launch has no verified process ownership",
                    }
                else:
                    entry = panes.close(owner, target)
                cursor["cleanup"].append(entry)
                write(cursor_path, cursor)
                cleanup_event(path, entry)
            cursor["step"] = 3
            write(cursor_path, cursor)
        if cursor["step"] < 4:
            write(finish_path, {k: v for k, v in cursor.items() if k != "step"})
            cursor["step"] = 4
            write(cursor_path, cursor)
        if cursor["step"] < 5:
            entry = stop_watch(path, receipt, wait_seconds, deadline)
            cursor["cleanup"].append(entry)
            cursor["step"] = 5
            write(cursor_path, cursor)
            cleanup_event(path, entry)
        if cursor["step"] < 6:
            entry = panes.close(
                receipt.get("ownership", {}).get("implementer"), "implementer"
            )
            cursor["cleanup"].append(entry)
            if entry["status"] in ("closed", "already-gone"):
                cursor["closed_pane"] = receipt.get("implementer_pane")
            cursor["step"] = 6
            write(cursor_path, cursor)
            cleanup_event(path, entry)
        if cursor["step"] < 7:
            # Re-publish persisted failures too, repairing an interruption between
            # a completed cleanup step and its advisory write.
            for entry in cursor["cleanup"]:
                cleanup_event(path, entry)
            write(finish_path, {k: v for k, v in cursor.items() if k != "step"})
            cursor["step"] = 7
            write(cursor_path, cursor)
        return read(finish_path)


class Reconciler:
    """Per-await watch with run-local cursors and the shared nudge authority."""

    def __init__(self, ctx: Any, recruiter: Any = None, runtime: Any = None):
        self.ctx = ctx
        self.path = ctx.control_dir / "implementer-start.json"
        self.rw = _load("run_watch")
        self.recruiter = recruiter or self.rw.recruiter
        self.runtime = runtime or self.rw.Runtime(ctx, {})
        self.target = self.runtime.implementer()
        self.panes = Panes(self.recruiter, ctx.receipt)
        self.cursor_path = ctx.control_dir / "sentinel" / "watch.json"
        self.authority = self.rw.authority

    def notify(self, message: str) -> None:
        launch_path = self.ctx.control_dir / "sentinel" / "launch.json"
        if not launch_path.exists():
            return
        launch = read(launch_path)
        owner = launch.get("ownership")
        if not owner or launch.get("state") != "started":
            return
        try:
            pane, agent, _ = self.panes.inspect(owner)
            if not self.panes.matches(owner, pane, agent):
                raise SupervisionError("Sentinel identity mismatch during recheck")
            self.panes.rpc("pane", "run", owner["pane_id"], message)
        except (SupervisionError, self.recruiter.RecruiterError) as error:
            advisory(
                self.path,
                "flow1:sentinel-degraded",
                f"Sentinel recheck unavailable: {error}",
            )
            return
        wake = self.ctx.control_dir / "sentinel" / "wake"
        temp = wake.with_name("wake." + uuid.uuid4().hex)
        temp.write_text(message + "\n")
        os.replace(temp, wake)

    def nudge(self, trigger: str) -> Any:
        outcome = self.runtime.nudges.request_nudge(
            self.target.identity,
            "continue",
            trigger,
            lambda: self.runtime.probe(self.target),
            lambda text: self.runtime.deliver(self.target, text),
            wait_for_lock=False,
        )
        if outcome.outcome == "delivered":
            self.notify(
                "SENTINEL_STALL_NUDGED: Python sent continue. Resume PULSE and write a fresh closeout if needed."
            )
        if outcome.outcome == "cap-exhausted":
            awaiter = _load("implementer_await")
            awaiter.publish_event(
                self.ctx,
                "leader-stalled",
                "Implementer continue ladder exhausted",
                severity="urgent",
                dedupe_key=f"flow1:stalled:{outcome.episode}",
                requested_action="inspect-and-decide",
            )
        return outcome

    def tick(self) -> dict:
        # One cursor writer across concurrent awaits; nudge delivery itself is
        # serialized independently with run-watch under the target authority.
        controller = _load("implementer_controller")
        with controller._exclusive(self.ctx.control_dir / ".reconcile.lock"):
            cursor = (
                read(self.cursor_path)
                if self.cursor_path.exists()
                else {"idle": 0, "absent": 0, "landing_retries": 0}
            )
            observed = self.runtime.probe(self.target)
            verdict = observed.classify()
            cursor["idle"] = (
                cursor.get("idle", 0) + 1
                if verdict == self.authority.Verdict.NUDGE
                else 0
            )
            awaiter = _load("implementer_await")
            if verdict == self.authority.Verdict.NUDGE:
                if cursor["idle"] >= 2:
                    self.nudge("await")
                else:
                    self.runtime.nudges.observe(
                        self.target.identity, lambda: self.runtime.probe(self.target)
                    )
            elif verdict == self.authority.Verdict.WORKING:
                self.runtime.nudges.observe(
                    self.target.identity, lambda: self.runtime.probe(self.target)
                )
            elif verdict == self.authority.Verdict.GONE:
                awaiter.publish_event(
                    self.ctx,
                    "leader-missing",
                    "Implementer pane is gone without a valid implementer-result.json",
                    severity="urgent",
                    dedupe_key="implementer-missing",
                    requested_action="inspect-and-decide",
                )
            elif verdict == self.authority.Verdict.HOLD:
                if observed.status == "blocked":
                    awaiter.publish_event(
                        self.ctx,
                        "decision-required",
                        "Implementer pane is blocked",
                        severity="attention",
                        dedupe_key="flow1:blocked",
                        requested_action="inspect-and-decide",
                    )
                elif observed.pane_state == self.authority.PaneState.UNKNOWN:
                    advisory(
                        self.path,
                        "flow1:probe-unknown",
                        "probe-unknown: implementer held until Herdr presence can be verified",
                    )
            self.watch(cursor)
            write(self.cursor_path, cursor)
            return {
                "alive": observed.pane_state == self.authority.PaneState.PRESENT
                if observed.pane_state != self.authority.PaneState.UNKNOWN
                else None,
                "agent_status": observed.status,
            }

    def watch(self, cursor: dict) -> None:
        if (self.ctx.control_dir / "finishing.json").exists():
            return
        directory = self.cursor_path.parent
        launch_path = directory / "launch.json"
        if not launch_path.exists():
            return
        launch = read(launch_path)
        if launch.get("state") != "started" or cursor.get("degraded"):
            return
        closeout_path = directory / "closeout-a1.json"
        if closeout_path.exists():
            import hashlib

            digest = (
                str(launch.get("landing", 1))
                + ":"
                + hashlib.sha256(closeout_path.read_bytes()).hexdigest()
            )
            if digest != cursor.get("closeout_digest"):
                try:
                    closeout = sentinel_contracts.load_closeout(
                        closeout_path, self.ctx.run_id, "flow1-" + self.ctx.run_id
                    )
                except (OSError, sentinel_contracts.SentinelContractError) as error:
                    advisory(
                        self.path,
                        "flow1:sentinel-invalid",
                        f"Invalid Sentinel closeout: {error}",
                    )
                else:
                    self.consume(closeout, cursor)
                    cursor["closeout_digest"] = digest
                    write(self.cursor_path, cursor)
        if cursor.get("complete") or cursor.get("degraded"):
            return
        owner = launch.get("ownership")
        if not owner:
            return
        try:
            self.panes.rpc("pane", "get", owner["pane_id"])
        except self.recruiter.RecruiterError as error:
            cursor["absent"] = (
                cursor.get("absent", 0) + 1 if "pane_not_found" in str(error) else 0
            )
        else:
            cursor["absent"] = 0
        if cursor["absent"] >= 2:
            advisory(
                self.path,
                "flow1:sentinel-degraded",
                "Sentinel pane absent on two probes; mechanical supervision continues",
            )
            cursor["degraded"] = True

    def consume(self, closeout: dict, cursor: dict) -> None:
        cwd = self.ctx.receipt["cwd"]
        worktree = self.recruiter._git_worktree_root(cwd)
        verified, rejected = sentinel_contracts.verify_citations(
            closeout["citations"],
            file_exists=lambda p: Path(p).is_file(),
            commit_exists=lambda sha: (
                worktree is not None
                and self.recruiter.commit_exists_in_worktree(worktree, sha)
            ),
            scope_roots=(cwd, self.ctx.run_root),
        )
        cursor["last_closeout"] = {
            "closeout": closeout,
            "corroborated_citations": verified,
            "uncorroborated_citations": rejected,
        }
        outcome = closeout["outcome"]
        awaiter = _load("implementer_await")
        if outcome == "COMPLETE":
            if awaiter.has_terminal_result(self.ctx):
                cursor["complete"] = True
            else:
                awaiter.publish_event(
                    self.ctx,
                    "invalid-result",
                    "Sentinel COMPLETE has no valid implementer result",
                    severity="attention",
                    dedupe_key="flow1:sentinel-false-complete",
                )
        elif outcome == "STALLED":
            self.nudge("sentinel")
        elif outcome == "FINALIZATION_FAILED":
            advisory(
                self.path,
                "flow1:finalization-failed",
                json.dumps(cursor["last_closeout"], sort_keys=True),
            )
            # Still status-first: a working, blocked or exec pane is never typed at.
            self.nudge("sentinel")
        elif outcome == "NEVER_STARTED":
            if cursor.get("landing_retries", 0) >= 1:
                advisory(
                    self.path,
                    "flow1:sentinel-degraded",
                    "Sentinel NEVER_STARTED after one landing retry; mechanical supervision continues",
                )
                cursor["degraded"] = True
                return
            cursor["landing_retries"] = 1
            write(self.cursor_path, cursor)
            launch = read(self.ctx.control_dir / "sentinel" / "launch.json")
            cleanup = self.panes.close(launch.get("ownership"), "sentinel")
            cleanup_event(self.path, cleanup)
            if cleanup["status"] == "cleanup-failed":
                cursor["degraded"] = True
                advisory(
                    self.path,
                    "flow1:sentinel-degraded",
                    "Sentinel landing retry could not verify cleanup",
                )
                return
            closeout_path = self.ctx.control_dir / "sentinel" / "closeout-a1.json"
            closeout_path.replace(closeout_path.with_name("closeout-before-retry.json"))
            roster = self.recruiter.load_roster(self.ctx.receipt["roster_path"])
            try:
                sentinel_start(
                    self.panes,
                    self.path,
                    management.load_management_config(roster),
                    self.ctx.receipt["startup_budget"]["sentinel_seconds"],
                    number=2,
                )
            except (SupervisionError, self.recruiter.RecruiterError) as error:
                cursor["degraded"] = True
                advisory(
                    self.path,
                    "flow1:sentinel-degraded",
                    f"Sentinel landing retry failed: {error}",
                )
