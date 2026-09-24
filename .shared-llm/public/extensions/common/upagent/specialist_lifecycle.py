"""Opt-in, command-driven resident specialists. No daemon and no Recruiter jobs.

Only this module owns resident panes. Commands serialize on one resident, not the
Recruiter's ledger lock. Herdr observations are sequential, not atomic or a sandbox.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

LIFETIME_SECONDS = 2 * 60 * 60
WAIT_SECONDS = 10 * 60
START_SECONDS = 3 * 60
# How long a response file may sit unreadable (a non-atomic writer's torn read) before its
# content is trusted as the resident's final, settled word. Mirrors the Recruiter's own
# `INVALID_RESULT_SETTLE_SECONDS` (recruiter.py) without importing a private constant across
# the module boundary the fake test backend also has to stand in for.
RESPONSE_SETTLE_SECONDS = 0.5
recruiter: Any = None


class SpecialistError(RuntimeError):
    """A resident could not be safely used or changed."""


class LaunchUnresolved(SpecialistError):
    """The journaled launch has no recorded pane and Herdr has no agent under its name.

    Herdr 0.7.1 never cancels a queued `agent start` (a killed or timed-out CLI does not
    withdraw it), so absence proves nothing about a later launch. The generation's unique
    `warm-<generation>` name is the only fence: Herdr refuses a second live agent with that
    name. So this generation is never abandoned: it is resumed under the same name, or
    probed again by name, never replaced by a new generation.
    """


class SpecialistBusy(SpecialistError):
    """A resident's lock could not be acquired without waiting for someone else.

    A distinct subclass (not just a message) so best-effort callers -- `preflight`'s
    per-resident refresh -- can skip only a contended resident and let every other
    `SpecialistError` (a real `_ensure` failure) propagate instead of being swallowed.
    """


def _bind_recruiter_runtime(runtime: Any) -> None:
    global recruiter
    recruiter = runtime


def _hash(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, default=str).encode()
    ).hexdigest()


def _private_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.is_symlink() or path.stat().st_uid != os.getuid():
        raise SpecialistError(f"unsafe specialist directory: {path}")
    os.chmod(path, 0o700)
    return path


def _write(path: Path, value: dict) -> None:
    # Caller-owned answer directories must not have their permissions changed.
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.parent.is_symlink():
        raise SpecialistError(f"unsafe output directory: {path.parent}")
    temporary = path.with_name("." + path.name + "." + uuid.uuid4().hex)
    try:
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as stream:
            json.dump(value, stream, sort_keys=True, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        temporary.unlink(missing_ok=True)


def _read(path: Path) -> dict | None:
    try:
        if path.is_symlink():
            raise SpecialistError(f"refusing symlink: {path}")
        value = json.loads(path.read_text())
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as error:
        raise SpecialistError(f"cannot read {path}: {error}") from error
    if not isinstance(value, dict):
        raise SpecialistError(f"expected JSON object: {path}")
    return value


@contextmanager
def _lock(path: Path, timeout: float = WAIT_SECONDS) -> Iterator[None]:
    _private_dir(path.parent)
    fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w") as stream:
        deadline = time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise SpecialistBusy(
                        "specialist is busy; bounded lock wait expired"
                    )
                time.sleep(0.1)
        try:
            yield
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)


def root() -> Path:
    return Path(recruiter.JobLedger().root).parent / "specialists"


def _directory(name: str, cwd: str | Path) -> Path:
    return root() / "residents" / _hash([name, str(Path(cwd).resolve())])


def _entry(name: str) -> dict:
    entries = recruiter._specialist_index(recruiter.load_specialist_roster())
    if name not in entries:
        raise SpecialistError(
            f"unknown specialist {name!r}; available: {', '.join(entries)}"
        )
    return entries[name]


def _context(entry: dict, cwd: str | Path) -> tuple[str, dict]:
    location = entry.get("location")
    files = [Path(cwd) / "AGENTS.md", Path(cwd) / "CLAUDE.md"]
    if location:
        files.append(
            recruiter._resolve_specialist_path(
                location, entry["repo_root"], "specialist location"
            )
        )
    snapshots = {}
    for path in files:
        path = Path(path).resolve()
        snapshots[str(path)] = (
            hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None
        )
    return _hash([entry, str(Path(cwd).resolve()), snapshots]), snapshots


def _herdr(state: dict, *args: str, timeout_seconds: float | None = 15) -> dict:
    return recruiter._herdr_json(
        *args, herdr_session=state["session"], timeout_seconds=timeout_seconds
    )


def _identity(state: dict, *, initial: bool = False) -> dict | None:
    """Prove name, pane, harness, cwd and process birth. Unknown is never absent."""
    generation = state.get("generation")
    if (
        not isinstance(generation, str)
        or re.fullmatch(r"[0-9a-f]{32}", generation) is None
        or state.get("agent_name") != "warm-" + generation
    ):
        raise SpecialistError("invalid resident generation/name ownership record")
    try:
        agent = _herdr(state, "agent", "get", state["agent_name"])["result"]["agent"]
    except recruiter.RecruiterError as error:
        if "agent_not_found" not in str(error):
            raise
        if not state.get("pane"):
            raise LaunchUnresolved(
                "launch outcome uncertain; refusing duplicate launch"
            ) from error
        try:
            _herdr(state, "pane", "get", state["pane"])
        except recruiter.RecruiterError as pane_error:
            if "pane_not_found" in str(pane_error):
                return None
            raise
        raise SpecialistError(
            "recorded pane exists but its named agent is missing"
        ) from error
    if not isinstance(agent, dict) or agent.get("name") != state["agent_name"]:
        raise SpecialistError("resident named agent identity mismatch")
    pane_id = agent.get("pane_id")
    if not pane_id or (state.get("pane") and state["pane"] != pane_id):
        raise SpecialistError("resident pane identity changed")
    try:
        pane = _herdr(state, "pane", "get", pane_id)["result"]["pane"]
    except recruiter.RecruiterError as error:
        if "pane_not_found" in str(error) and state.get("pane") == pane_id:
            return None
        raise
    info = _herdr(state, "pane", "process-info", "--pane", pane_id)["result"][
        "process_info"
    ]
    expected = recruiter.EXPECTED_HARNESS_AGENT[state["harness"]]
    process_name = recruiter.EXPECTED_HARNESS_PROCESS[state["harness"]]
    process = recruiter._matching_intake_process(info, process_name)
    cwd = pane.get("foreground_cwd", pane.get("cwd"))
    if (
        pane.get("agent") != expected
        or not isinstance(cwd, str)
        or os.path.realpath(cwd) != state["cwd"]
    ):
        raise SpecialistError("resident harness or cwd does not match its journal")
    pid = process.get("pid") if process else None
    birth = recruiter._process_start_time(pid)
    if not isinstance(pid, int) or not birth:
        raise SpecialistError("resident process birth identity unavailable")
    if not initial and (state.get("pid"), state.get("birth")) != (pid, birth):
        raise SpecialistError("resident foreground process was replaced")
    return {
        "pane": pane_id,
        "pid": pid,
        "birth": birth,
        "idle": pane.get("agent_status") in ("idle", "done"),
    }


def _launch_unresolved(state: dict) -> bool:
    """True while no `agent start` reply has recorded a pane for this generation."""
    return state["state"] == "starting" and not state.get("pane")


def _expired(state: dict) -> bool:
    age = time.time() - state["launched_at"]
    elapsed = time.monotonic() - state["launched_monotonic"]
    return age < 0 or elapsed < 0 or max(age, elapsed) >= LIFETIME_SECONDS


def _save(directory: Path, state: dict) -> None:
    _write(directory / "state.json", state)


def _settle_turn(
    directory: Path, state: dict, turn: dict, next_state: str, outcome: str, reason: str
) -> str:
    """Durably record one terminal non-answer outcome and make the resident safe to reuse.

    Persists BEFORE returning, so the very next call against this resident -- even the one
    that receives this same failure -- finds a resident that is already `ready` (idle,
    reusable) or `stopped` (verified gone), never a leftover `busy` wedge. The turn record
    keeps its own outcome/reason rather than being discarded, so a failed or abandoned turn
    stays distinguishable from one that never happened.
    """
    state["state"] = next_state
    state["turn"] = {**turn, "outcome": outcome, "reason": reason}
    _save(directory, state)
    return reason


def _finish_busy(directory: Path, state: dict) -> str | None:
    """Resolve a turn once safely possible; never assume an idle pane answered.

    Returns `None` once a genuinely validated answer exists. Otherwise returns a reason
    string once that turn's outcome -- an invalid answer, an abandoned deadline, or the
    resident having vanished -- has been durably recorded and it is safe for the immediate
    caller, and every later one, to reuse or replace this resident. Raises `SpecialistError`
    only for the one case that must keep blocking: the deadline has not resolved anything and
    the pane is either still working or its identity cannot yet be proven, so nothing has been
    changed on disk.
    """
    turn = state.get("turn")
    if state["state"] != "busy" or not turn:
        return None
    deadline = time.monotonic() + max(
        0, min(WAIT_SECONDS, turn["deadline"] - time.time())
    )
    response_path = Path(turn["response"])
    _unset = object()
    torn_signature: object = _unset
    torn_since = 0.0
    while True:
        identity = _identity(state)
        if identity is None:
            return _settle_turn(
                directory,
                state,
                turn,
                "stopped",
                "vanished",
                "resident vanished before its turn completed",
            )
        if identity["idle"]:
            # A non-atomic writer can momentarily leave a torn read, and a well-formed but
            # invalid answer is not trustworthy the instant it appears either -- both are
            # judged only once the file's own identity (mtime, size) has stopped changing for
            # a settle window, exactly like a plain missing file is never itself a failure.
            error: Exception | None = None
            try:
                response = _read(response_path)
            except SpecialistError as read_error:
                response, error = None, read_error
            if response is not None:
                try:
                    _validated_answer(state, turn, response)
                except (
                    SpecialistError,
                    recruiter.contracts_consult.ConsultError,
                ) as invalid:
                    error = invalid
                else:
                    state["state"] = "ready"
                    _save(directory, state)
                    return None
            if error is not None:
                try:
                    stat = response_path.stat()
                    signature: object = (stat.st_mtime, stat.st_size)
                except OSError:
                    signature = None
                if signature != torn_signature:
                    torn_signature, torn_since = signature, time.monotonic()
                elif time.monotonic() - torn_since >= RESPONSE_SETTLE_SECONDS:
                    return _settle_turn(
                        directory,
                        state,
                        turn,
                        "ready",
                        "failed",
                        f"resident answer failed validation: {error}",
                    )
        if time.monotonic() >= deadline:
            if identity["idle"]:
                return _settle_turn(
                    directory,
                    state,
                    turn,
                    "ready",
                    "abandoned",
                    "resident turn deadline passed with no usable answer",
                )
            raise SpecialistError(
                "resident turn remains unresolved; refusing reuse or termination"
            )
        time.sleep(0.2)


def _stop(directory: Path, state: dict) -> None:
    if state["state"] == "stopped":
        return
    _finish_busy(directory, state)
    if state["state"] == "stopped":
        return
    if state["state"] == "starting" and "pid" not in state:
        # A lost launch reply -- `agent start`'s own client-side reply timed out, or the
        # startup loop in `_start` never got far enough to confirm process identity -- but
        # the named `warm-<generation>` agent may genuinely be live. Adopt its identity
        # (`initial=True`, the same flag `_start`'s own readiness loop uses while "pid" is
        # not yet in state) instead of rejecting a real, correctly-identified process as
        # "replaced". Every other check inside `_identity` -- name, harness, cwd, live
        # process, and a previously recorded pane matching -- still applies, so an unrelated
        # or mismatched agent is still rejected, never adopted.
        identity = _identity(state, initial=True)
        if identity:
            state.update(
                pane=identity["pane"], pid=identity["pid"], birth=identity["birth"]
            )
            _save(directory, state)
    identity = _identity(state)
    if identity:
        if not identity["idle"]:
            raise SpecialistError("resident is not idle; refusing to interrupt it")
        if _identity(state) != identity:
            raise SpecialistError("resident changed before cleanup")
        _herdr(state, "pane", "close", identity["pane"])
        try:
            _herdr(state, "pane", "get", identity["pane"])
        except recruiter.RecruiterError as error:
            if "pane_not_found" not in str(error):
                raise
        else:
            raise SpecialistError("resident cleanup not verified")
    state["state"] = "stopped"
    _save(directory, state)


def _start(
    directory: Path, name: str, cwd: str, entry: dict, cockpit: str | None
) -> dict:
    snapshot = entry["offering_snapshot"]
    recruiter.offering_catalog.preflight_snapshot(snapshot)
    harness = snapshot["harness"]
    if harness not in ("pi", "claude", "claudex", "cursor"):
        raise SpecialistError(
            f"{harness} does not support interactive resident specialists"
        )
    generation = uuid.uuid4().hex
    attempt = _private_dir(directory / generation)
    fingerprint, files = _context(entry, cwd)
    prompt = attempt / "instructions.md"
    state = {
        "name": name,
        "cwd": str(Path(cwd).resolve()),
        "enabled": True,
        "generation": generation,
        "agent_name": "warm-" + generation,
        "harness": harness,
        "session": recruiter._resolve_current_herdr_session_name(),
        "pane": None,
        "state": "starting",
        "context": fingerprint,
        "launched_at": time.time(),
        "launched_monotonic": time.monotonic(),
        # Journaled so a resumed launch replays exactly the same command.
        "launch": recruiter.offering_catalog.render_shell(
            snapshot, entry["agent"], str(prompt)
        ),
    }
    ack = attempt / "ready.json"
    prompt.write_text(
        f"You are the resident {name} specialist. Load your persona {entry['agent']} and read "
        f"these context files that exist: {json.dumps(files)}. Repository: {state['cwd']}. "
        "Read the specialist definition and its referenced context before acknowledging readiness. "
        "Then atomically write STRICT JSON to "
        + str(ack)
        + ": "
        + json.dumps({"generation": generation, "ready": True})
        + ". "
        "Remain in this interactive session, idle until the next question. Do not exit. "
        "Answer only the question currently delivered. Read current source before making claims; "
        "cached context is not proof files are unchanged. Cite file:line evidence. "
        "Do not modify repository files. Never retrieve or retain secret values; provide references only. "
        "Each later question specifies a private response file and generation/turn identifiers. "
        "Write only that response, then return to idle. Do not close your pane.\n"
        + str(snapshot.get("prompt_addendum", ""))
        + "\n"
    )
    os.chmod(prompt, 0o600)
    return _launch(directory, state, entry, cockpit)


def _launch(
    directory: Path,
    state: dict,
    entry: dict,
    cockpit: str | None,
    *,
    resume: bool = False,
) -> dict:
    """Launch the journaled generation, or resume its unresolved launch, then await readiness.

    A resume re-sends `agent start` under the SAME `warm-<generation>` name. Herdr checks name
    conflicts and names the new terminal inside one app handler, so at most one live agent can
    hold that name. Either this call wins the name (any earlier queued start then fails with
    `agent_name_taken`), or the earlier start already holds it and is adopted here.
    """
    generation = state["generation"]
    attempt = directory / generation
    ack = attempt / "ready.json"
    # Records journaled before the launch command was recorded render it the same way.
    state.setdefault(
        "launch",
        recruiter.offering_catalog.render_shell(
            entry["offering_snapshot"], entry["agent"], str(attempt / "instructions.md")
        ),
    )
    if not cockpit:
        raise SpecialistError("a live caller pane is required to start specialists")
    tab = _herdr(state, "pane", "get", cockpit)["result"]["pane"].get("tab_id")
    if not tab:
        raise SpecialistError("caller pane has no tab")
    # Journal BEFORE launching. A lost response must block, never cause an orphan duplicate.
    _save(directory, state)
    # Start the bound before IPC: a hung launch must not bypass readiness.
    # A timed-out reply leaves the journaled generation in `starting` with no pane; later
    # commands resume it under the same name (see `_ensure`), never under a new generation.
    deadline = time.monotonic() + START_SECONDS
    adopted = None
    if resume:
        # No pane was ever recorded, so no process identity is proven yet either.
        state.pop("pid", None)
        state.pop("birth", None)
        # Probe by name first: an earlier start that did land is adopted, never duplicated.
        try:
            adopted = _identity(state, initial=True)
        except LaunchUnresolved:
            # No live agent holds the name, so any acknowledgement on disk came from a
            # process that no longer exists; it must not satisfy this launch's readiness.
            ack.unlink(missing_ok=True)
    if adopted is None:
        try:
            result = _herdr(
                state,
                "agent",
                "start",
                state["agent_name"],
                "--cwd",
                state["cwd"],
                "--tab",
                tab,
                "--split",
                "right",
                "--no-focus",
                "--",
                "bash",
                "-lc",
                state["launch"],
                timeout_seconds=START_SECONDS,
            )
        except recruiter.RecruiterError as error:
            if "agent_name_taken" not in str(error):
                raise
            # An earlier start of this generation won the name between the probe and this
            # start. Adopt it only once `_identity` proves its harness, cwd and process.
            adopted = _identity(state, initial=True)
        else:
            agent = result.get("result", {}).get("agent", {})
            if agent.get("name") != state["agent_name"] or not agent.get("pane_id"):
                raise SpecialistError(
                    "specialist launch returned no matching agent identity"
                )
            state.update(
                pane=agent["pane_id"],
                launched_at=time.time(),
                launched_monotonic=time.monotonic(),
            )
    if adopted is not None:
        state["pane"] = adopted["pane"]
    _save(directory, state)
    cwd = state["cwd"]
    fingerprint = state["context"]
    last_error = "no readiness acknowledgement"
    while time.monotonic() < deadline:
        try:
            identity = _identity(state, initial="pid" not in state)
            if identity and "pid" not in state:
                state.update({key: identity[key] for key in ("pid", "birth")})
                _save(directory, state)
            acknowledgement = _read(ack)
            if (
                identity
                and identity["idle"]
                and acknowledgement is not None
                and set(acknowledgement) == {"generation", "ready"}
                and acknowledgement.get("generation") == generation
                and acknowledgement.get("ready") is True
            ):
                if _context(entry, cwd)[0] != fingerprint:
                    raise SpecialistError("context changed during specialist warm-up")
                state.update({key: identity[key] for key in ("pid", "birth")})
                state["state"] = "ready"
                _save(directory, state)
                return state
        except (SpecialistError, recruiter.RecruiterError) as error:
            last_error = str(error)
        time.sleep(0.2)
    raise SpecialistError(
        f"specialist did not load context and become ready: {last_error}"
    )


def _ensure(
    directory: Path,
    name: str,
    cwd: str,
    cockpit: str | None,
    *,
    enable: bool = False,
    restart: bool = False,
) -> dict | None:
    state = _read(directory / "state.json")
    if not enable and (state is None or not state["enabled"]):
        return None
    entry = _entry(name)
    context, _ = _context(entry, cwd)
    if state:
        _finish_busy(directory, state)
        if _launch_unresolved(state):
            # Never mint a new generation over an unresolved launch: resume it by name. The
            # process this starts (or adopts) is fresh, so it also satisfies `restart`.
            state["enabled"] = True
            return _launch(directory, state, entry, cockpit, resume=True)
        if state["state"] == "ready":
            identity = _identity(state)
            if identity and not identity["idle"]:
                raise SpecialistError("resident is working outside a recorded question")
            if (
                identity
                and not restart
                and not _expired(state)
                and context == state["context"]
            ):
                state["enabled"] = True
                _save(directory, state)
                return state
        _stop(directory, state)
    return _start(directory, name, cwd, entry, cockpit)


def _down(directory: Path, state: dict) -> None:
    """Disable, then stop what can be proven to exist. Never launches anything.

    An unresolved launch with no agent under its name stays `starting` (disabled) instead of
    `stopped`: a later `down` probes the same name again and a later `up` resumes it, so a
    late launch is always found by name rather than orphaned behind a new generation.
    """
    state["enabled"] = False
    _save(directory, state)
    if _launch_unresolved(state):
        try:
            _identity(state, initial=True)
        except LaunchUnresolved:
            return
    _stop(directory, state)


def configured() -> bool:
    """True once any resident has been journaled; otherwise every caller stays cold."""
    return any((root() / "residents").glob("*/state.json"))


def enabled(name: str, cwd: str | Path) -> bool:
    state = _read(_directory(name, cwd) / "state.json")
    return bool(state and state["enabled"])


def preflight(cwd: str | Path, cockpit: str | None = None) -> None:
    """Refresh only this canonical context; never borrow another worktree's memory.

    Best-effort rotation only: a resident whose lock is currently held (mid-turn, or another
    refresh already in flight) is skipped rather than waited for, so one busy resident never
    stalls a caller that only needed a different one, or needed none at all. Skipping never
    authorizes a stale answer -- `consult` still re-checks identity, idle state, expiry and
    context under its own lock before ever using a resident -- so a skipped stale resident
    simply rotates on the next preflight or consult instead of this one.
    """
    cwd = str(Path(cwd).resolve())
    for path in sorted((root() / "residents").glob("*/state.json")):
        state = _read(path)
        if state and state["enabled"] and state["cwd"] == cwd:
            try:
                with _lock(path.parent / "lock", timeout=0):
                    _ensure(
                        path.parent,
                        state["name"],
                        cwd,
                        cockpit or recruiter._recruiter_pane_from_state(),
                    )
            except SpecialistBusy:
                continue


def _validated_answer(state: dict, turn: dict, response: dict) -> dict:
    if (
        response.get("generation") != state["generation"]
        or response.get("turn") != turn["id"]
    ):
        raise SpecialistError("resident answer has stale generation or turn")
    return recruiter.contracts_consult.parse_answer(
        json.dumps(response.get("answer")), turn["consult_id"]
    )


def consult(
    consult: dict, cockpit: str | None = None, *, evidence: dict[str, str] | None = None
) -> dict | None:
    """Return None for cold routing; otherwise return a validated private answer.

    When `evidence` is given, it is filled with ``{"generation": ..., "turn": ...}`` naming the
    exact resident instance and turn that produced the answer — but only once that answer has
    passed `_validated_answer` and the post-answer freshness check below, so it can never name a
    turn that did not really complete. A caller that never receives an update (because this
    raised, or returned None) must not treat the consult as verifiable.
    """
    name = consult["specialist"]
    cwd = str(Path(consult["cwd"]).resolve())
    directory = _directory(name, cwd)
    if not enabled(name, cwd):
        return None
    with _lock(directory / "lock"):
        state = _ensure(
            directory, name, cwd, cockpit or recruiter._recruiter_pane_from_state()
        )
        if state is None:
            return None
        turn_id = uuid.uuid4().hex
        response = directory / state["generation"] / (turn_id + ".json")
        turn = {
            "id": turn_id,
            "consult_id": consult["consult_id"],
            "response": str(response),
            "deadline": time.time()
            + min(WAIT_SECONDS, consult.get("timeout_seconds", WAIT_SECONDS)),
        }
        state.update(state="busy", turn=turn)
        _save(directory, state)
        try:
            identity = _identity(state)
            if not identity or not identity["idle"]:
                raise SpecialistError("resident is not ready for delivery")
            question = response.with_name(turn_id + ".question.md")
            question.write_text(
                "Answer this question from current sources: "
                + consult["question"]
                + "\nAtomically write STRICT JSON to "
                + str(response)
                + " with this shape: "
                + json.dumps(
                    {
                        "generation": state["generation"],
                        "turn": turn_id,
                        "answer": {
                            "consult_id": consult["consult_id"],
                            "answer": "<answer>",
                            "citations": ["path/to/source:1"],
                        },
                    }
                )
                + ". Use real file:line citations. Do not write other files. "
                "Then go idle; do not exit.\n"
            )
            os.chmod(question, 0o600)
            # A single-line pointer, not the inline multiline question: mirrors the
            # Recruiter's own one-line "read your prompt file" delivery instead of pasting
            # arbitrary multiline text into a live TUI turn.
            message = f"Read {question} and answer exactly as it instructs."
        except (SpecialistError, recruiter.RecruiterError):
            # Nothing was ever sent to the pane: this is KNOWN non-delivery, so the turn can
            # be safely rolled back rather than left `busy` for a question no one received.
            state.update(state="ready")
            state.pop("turn", None)
            _save(directory, state)
            raise
        # Deliver one serialized TUI turn, under the process-shared resident lock. Past this
        # point delivery may have started, so a failure here is UNCERTAIN, not known
        # non-delivery: stay `busy` and let `_finish_busy`'s deadline/idle policy resolve it,
        # rather than risk ever delivering the same question twice.
        if state["harness"] == "cursor":
            recruiter._herdr(
                "pane",
                "send-text",
                state["pane"],
                message,
                herdr_session=state["session"],
                timeout_seconds=15,
            )
            time.sleep(recruiter.CURSOR_PROMPT_PASTE_SETTLE_SECONDS)
            recruiter._herdr(
                "pane",
                "send-keys",
                state["pane"],
                "Enter",
                herdr_session=state["session"],
                timeout_seconds=15,
            )
        else:
            # Herdr's pane run/send commands print nothing on success. Its read/query
            # commands print JSON; treating a successful send as JSON leaves a real
            # delivered turn stuck in busy even when the specialist has answered.
            recruiter._herdr(
                "pane",
                "run",
                state["pane"],
                message,
                herdr_session=state["session"],
                timeout_seconds=15,
            )
        reason = _finish_busy(directory, state)
        if reason is not None:
            raise SpecialistError(reason)
        response_value = _read(response)
        if response_value is None:
            raise SpecialistError(
                "validated resident response disappeared before consumption"
            )
        answer = _validated_answer(state, turn, response_value)
        if _context(_entry(name), cwd)[0] != state["context"]:
            raise SpecialistError(
                "specialist instructions changed during its answer; request again"
            )
        if evidence is not None:
            evidence.update(generation=state["generation"], turn=turn_id)
        return answer


def legacy_consult(path: str) -> int | None:
    # No resident configuration means the cold path is completely unchanged.
    if not configured():
        return None
    # Leave malformed inputs and unknown names to the existing cold error publisher.
    try:
        consult_value = recruiter.contracts_consult.load_consult(path)
    except recruiter.contracts_consult.ConsultError:
        return None
    try:
        entries = recruiter._specialist_index(recruiter.load_specialist_roster())
    except recruiter.RecruiterError:
        return None  # Existing consult path publishes the roster error as a failure answer.
    name, _note = recruiter._resolve_specialist_name(
        consult_value["specialist"], list(entries)
    )
    if name is None:
        return None
    consult_value["specialist"] = name
    entry = entries[name]
    consult_value["cwd"] = recruiter._resolve_consult_cwd(
        entry, consult_value, consult_value["consult_id"]
    )
    # No unrelated-resident preflight here: `consult` already `_ensure`s the TARGET specialist
    # under its own lock (rotating it if stale) before ever delivering to it. Refreshing every
    # OTHER enabled resident in this cwd first would make one question depend on a stranger's
    # health -- a wedged or slow-to-verify resident could lose this answer even for a cold
    # target that never touches it.
    if not enabled(consult_value["specialist"], consult_value["cwd"]):
        return None
    artifacts = recruiter.consult_artifact_paths(path)
    recruiter._require_consult_receipt_destination(artifacts["receipt"])
    # Populated only once `consult` has validated a genuine answer against the exact resident
    # generation and turn that produced it. A pre-run rejection (the resident was not ready for
    # delivery, or never started), an uncompleted turn (delivered but never confirmed), and a
    # stale/mismatched answer all raise below without ever touching this — so none of them can
    # be mistaken for a completed turn.
    turn_evidence: dict[str, str] = {}
    try:
        answer = consult(consult_value, evidence=turn_evidence)
        if answer is None:
            return None
        reason = answer.get("error")
    except (
        SpecialistError,
        recruiter.RecruiterError,
        recruiter.contracts_consult.ConsultError,
    ) as error:
        reason = str(error)
        answer = recruiter.contracts_consult.failure_answer(
            consult_value["consult_id"], reason
        )
        turn_evidence = {}
    _write(Path(consult_value["answer_path"]), answer)
    receipt = {
        "consult_id": consult_value["consult_id"],
        "request_id": recruiter.consult_request_id(consult_value["consult_id"]),
        "specialist": consult_value["specialist"],
        "resolved_specialist": consult_value["specialist"],
        "requested_by": consult_value.get("requested_by"),
        "answer_path": consult_value["answer_path"],
        "cwd": consult_value["cwd"],
        "answer_verdict": "failed" if reason else "cited",
        "resident": True,
    }
    if reason:
        receipt["reason"] = reason
    # Never `order_receipt_state`: no Recruiter order or worker ever ran for a resident turn.
    # `resident_turn_state` is the resident's own truthful completion signal, and it is set only
    # when `turn_evidence` names the real generation/turn a validated answer came from.
    if turn_evidence:
        receipt["resident_turn_state"] = "finished"
        receipt.update(turn_evidence)
    recruiter._publish_consult_receipt(receipt, artifacts["receipt"])
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Opt-in resident specialists; fixed two-hour lifetime."
    )
    parser.add_argument("action", choices=("up", "status", "down", "restart"))
    parser.add_argument("names", nargs="?", help="comma-separated specialist names")
    args = parser.parse_args(argv)
    cwd = str(Path.cwd().resolve())
    names = args.names.split(",") if args.names else []
    if args.action != "status" and (
        not names or any(not name.strip() for name in names)
    ):
        parser.error("provide comma-separated specialist names")
    if not names:
        names = [
            s["name"]
            for p in (root() / "residents").glob("*/state.json")
            if (s := _read(p)) and s["cwd"] == cwd
        ]
    rows = []
    for name in dict.fromkeys(n.strip() for n in names):
        directory = _directory(name, cwd)
        if args.action == "status":
            state = _read(directory / "state.json")
            row = {"name": name, "state": "stopped", "enabled": False}
            if state:
                row = {
                    k: state[k]
                    for k in (
                        "name",
                        "state",
                        "enabled",
                        "generation",
                        "launched_at",
                        "pane",
                        "session",
                        "agent_name",
                        "cwd",
                    )
                }
                try:
                    if state["state"] != "stopped":
                        identity = _identity(state)
                        row["state"] = (
                            "missing"
                            if identity is None
                            else ("expired" if _expired(state) else state["state"])
                        )
                except (SpecialistError, recruiter.RecruiterError) as error:
                    row.update(state="uncertain", reason=str(error))
            rows.append(row)
            continue
        with _lock(directory / "lock"):
            state = _read(directory / "state.json")
            if args.action == "down":
                if state:
                    _down(directory, state)
            else:
                state = _ensure(
                    directory,
                    name,
                    cwd,
                    os.environ.get("HERDR_PANE_ID")
                    or recruiter._recruiter_pane_from_state(),
                    enable=True,
                    restart=args.action == "restart",
                )
            rows.append(
                {
                    "name": name,
                    "state": state["state"] if state else "stopped",
                    "enabled": bool(state and state["enabled"]),
                }
            )
    print(json.dumps(rows, sort_keys=True))
    return 0
