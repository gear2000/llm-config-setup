#!/usr/bin/env python3
"""Deterministic phase-await: block until one typed phase event is deliverable."""

from __future__ import annotations

import argparse
import datetime as dt
import fcntl
import importlib.util
import json
import os
import subprocess
import sys
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

HERE = Path(__file__).resolve().parent
_runtime_name = "upagent_command_runtime"
if _runtime_name in sys.modules:
    command_runtime = sys.modules[_runtime_name]
else:
    _runtime_spec = importlib.util.spec_from_file_location(
        _runtime_name, HERE / "command_runtime.py"
    )
    if _runtime_spec is None or _runtime_spec.loader is None:
        raise RuntimeError("could not load UpAgent command runtime")
    command_runtime = importlib.util.module_from_spec(_runtime_spec)
    sys.modules[_runtime_name] = command_runtime
    _runtime_spec.loader.exec_module(command_runtime)

_spec = importlib.util.spec_from_file_location(
    "upagent_await_contracts", HERE / "contracts.py"
)
if _spec is None or _spec.loader is None:
    raise RuntimeError("could not load UpAgent contracts")
contracts = cast(Any, importlib.util.module_from_spec(_spec))
_spec.loader.exec_module(contracts)

DEFAULT_TIMEOUT_MS = 10 * 60 * 1000
DEFAULT_POLL_MS = 750
DEFAULT_RECONCILE_MS = 20_000
DEFAULT_INACTIVITY_MS = 15 * 60 * 1000
DEFAULT_ESCALATE_MS = 10 * 60 * 1000
STALL_CONFIRMATIONS = 2
_RESULT_VERDICT_KINDS = {
    "passed": "completed",
    "failed": "failed",
    "blocked": "blocked",
}
_EPHEMERAL_KINDS = ("await-heartbeat", "progress")


class AwaitError(RuntimeError):
    pass


class PhaseContext:
    def __init__(self, receipt_path: Path):
        receipt_path = receipt_path.resolve()
        if not receipt_path.is_file():
            raise AwaitError(f"phase-start receipt not found: {receipt_path}")
        try:
            receipt = json.loads(receipt_path.read_text())
        except (OSError, json.JSONDecodeError) as error:
            raise AwaitError(f"phase-start receipt unreadable: {error}") from error
        if not isinstance(receipt, dict):
            raise AwaitError("phase-start receipt must be a JSON object")
        if receipt.get("state") not in ("ready", "ready-degraded"):
            raise AwaitError(
                f"phase-start receipt state must be ready/ready-degraded (got {receipt.get('state')!r})"
            )
        phase_id = receipt.get("phase_id")
        pass_number = receipt.get("pass")
        leader_pane = receipt.get("leader_pane")
        if not isinstance(phase_id, str) or not phase_id:
            raise AwaitError("phase-start receipt has no phase_id")
        if isinstance(pass_number, bool) or not isinstance(pass_number, int):
            raise AwaitError("phase-start receipt has no integer pass")
        if not isinstance(leader_pane, str) or not leader_pane:
            raise AwaitError("phase-start receipt has no leader_pane")
        self.receipt = receipt
        self.control_dir = receipt_path.parent
        self.run_root = self.control_dir.parents[3]
        self.phase_dir = self.control_dir.parents[1]
        self.run_id = self.run_root.name
        self.phase_id = phase_id
        self.pass_number = pass_number
        self.leader_pane = leader_pane
        self.events_dir = self.control_dir / "events"
        self.inbox_dir = self.control_dir / "inbox"
        self.acks_dir = self.control_dir / "acknowledgements"
        self.result_path = self.run_root / "phases" / phase_id / "phase-result.json"


def _now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _write_json_atomic(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    with tmp.open("w", encoding="utf-8") as stream:
        json.dump(value, stream, indent=2, sort_keys=True)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(tmp, path)


class _JournalLock:
    def __init__(self, ctx: PhaseContext):
        ctx.events_dir.mkdir(parents=True, exist_ok=True)
        self._path = ctx.events_dir / ".lock"

    def __enter__(self):
        self._stream = self._path.open("a+", encoding="utf-8")
        fcntl.flock(self._stream.fileno(), fcntl.LOCK_EX)

    def __exit__(self, *exc: object):
        fcntl.flock(self._stream.fileno(), fcntl.LOCK_UN)
        self._stream.close()


def read_journal(ctx: PhaseContext) -> list[dict]:
    if not ctx.events_dir.is_dir():
        return []
    events: list[dict] = []
    previous = None
    for path in sorted(ctx.events_dir.glob("*.json")):
        try:
            event = contracts.parse_event(
                path.read_text(),
                expected_run_id=ctx.run_id,
                expected_phase_id=ctx.phase_id,
            )
        except (OSError, contracts.ContractError) as error:
            raise AwaitError(f"journal event {path.name} invalid: {error}") from error
        previous = contracts.validate_event_order(previous, event)
        events.append(event)
    return events


def ack_state(ctx: PhaseContext, event_id: str) -> str | None:
    path = ctx.acks_dir / f"{event_id}.json"
    if not path.is_file():
        return None
    try:
        return cast(
            str,
            contracts.parse_ack(path.read_text(), expected_event_id=event_id)["state"],
        )
    except (OSError, contracts.ContractError) as error:
        raise AwaitError(f"ack record for {event_id} invalid: {error}") from error


def record_ack(ctx: PhaseContext, event_id: str, state: str, actor: str) -> dict:
    previous = ack_state(ctx, event_id)
    try:
        contracts.validate_ack_transition(previous, state)
    except contracts.ContractError as error:
        raise AwaitError(str(error)) from error
    if previous == state:
        return contracts.parse_ack(
            (ctx.acks_dir / f"{event_id}.json").read_text(), expected_event_id=event_id
        )
    ack = {
        "event_id": event_id,
        "state": state,
        "actor": actor,
        "occurred_at": _now_iso(),
    }
    _write_json_atomic(ctx.acks_dir / f"{event_id}.json", ack)
    return ack


def _unresolved_dedupe_keys(ctx: PhaseContext, events: list[dict]) -> set[str]:
    keys: set[str] = set()
    for event in events:
        key = event.get("dedupe_key")
        if isinstance(key, str) and ack_state(ctx, event["event_id"]) != "resolved":
            keys.add(key)
    return keys


def publish_event(
    ctx: PhaseContext,
    kind: str,
    summary: str,
    *,
    severity: str = "info",
    ack_required: bool | None = None,
    dedupe_key: str | None = None,
    evidence: list[dict] | None = None,
    requested_action: str | None = None,
    request_id: str | None = None,
) -> dict | None:
    if kind not in contracts.EVENT_KINDS:
        raise AwaitError(f"unknown event kind {kind!r}")
    with _JournalLock(ctx):
        events = read_journal(ctx)
        if dedupe_key is not None and dedupe_key in _unresolved_dedupe_keys(
            ctx, events
        ):
            return None
        if any(e.get("terminal") for e in events):
            return None
        sequence = events[-1]["sequence"] + 1 if events else 1
        event: dict[str, object] = {
            "schema_version": contracts.COORDINATION_SCHEMA_VERSION,
            "event_id": f"evt-{uuid.uuid4().hex}",
            "sequence": sequence,
            "occurred_at": _now_iso(),
            "run_id": ctx.run_id,
            "phase_id": ctx.phase_id,
            "pass": ctx.pass_number,
            "kind": kind,
            "terminal": contracts.EVENT_KINDS[kind],
            "severity": severity,
            "summary": summary,
            "ack_required": ack_required
            if ack_required is not None
            else kind not in _EPHEMERAL_KINDS,
            "source": {"component": "phase-await", "address": None},
        }
        if dedupe_key is not None:
            event["dedupe_key"] = dedupe_key
        if evidence:
            event["evidence"] = evidence
        if requested_action is not None:
            event["requested_action"] = requested_action
        if request_id is not None:
            event["request_id"] = request_id
        validated = contracts.parse_event(
            json.dumps(event),
            expected_run_id=ctx.run_id,
            expected_phase_id=ctx.phase_id,
        )
        _write_json_atomic(ctx.events_dir / f"{sequence:08d}.json", validated)
        return validated


def promote_inbox(ctx: PhaseContext) -> None:
    if not ctx.inbox_dir.is_dir():
        return
    for path in sorted(ctx.inbox_dir.glob("*.json")):
        try:
            envelope = json.loads(path.read_text())
            if not isinstance(envelope, dict):
                raise ValueError("inbox envelope must be a JSON object")
            kind = envelope.get("kind")
            summary = envelope.get("summary")
            if kind not in contracts.EVENT_KINDS:
                raise ValueError(f"unknown kind {kind!r}")
            if not isinstance(summary, str) or not summary:
                raise ValueError("missing summary")
        except (OSError, ValueError, json.JSONDecodeError) as error:
            quarantine = path.with_suffix(".rejected")
            path.replace(quarantine)
            publish_event(
                ctx,
                "advisory",
                f"Rejected malformed inbox envelope {path.name}: {error}",
                severity="attention",
                evidence=[{"kind": "durable-file", "path": str(quarantine)}],
                dedupe_key=f"inbox-rejected:{path.name}",
            )
            continue
        publish_event(
            ctx,
            cast(str, kind),
            cast(str, summary),
            severity=cast(str, envelope.get("severity", "attention")),
            ack_required=cast(bool | None, envelope.get("ack_required")),
            dedupe_key=cast(str | None, envelope.get("dedupe_key")),
            evidence=cast(list[dict] | None, envelope.get("evidence")),
            requested_action=cast(str | None, envelope.get("requested_action")),
            request_id=cast(str | None, envelope.get("request_id")),
        )
        path.unlink()


def check_phase_result(ctx: PhaseContext) -> None:
    if not ctx.result_path.is_file():
        return
    try:
        result = json.loads(ctx.result_path.read_text())
    except (OSError, json.JSONDecodeError):
        return
    if not isinstance(result, dict) or result.get("pass") != ctx.pass_number:
        return
    verdict = result.get("verdict")
    kind = _RESULT_VERDICT_KINDS.get(cast(str, verdict))
    if kind is None:
        return
    summary = f"Phase {ctx.phase_id} pass {ctx.pass_number} result: verdict={verdict}"
    revisit = result.get("revisit")
    if isinstance(revisit, list) and revisit:
        summary += f" revisit={','.join(str(item) for item in revisit)}"
    publish_event(
        ctx,
        kind,
        summary,
        severity="info" if verdict == "passed" else "attention",
        ack_required=True,
        dedupe_key=f"phase-result:pass-{ctx.pass_number}:{verdict}",
        evidence=[{"kind": "durable-file", "path": str(ctx.result_path)}],
        requested_action=None if verdict == "passed" else "inspect-and-decide",
    )


def _notify_human(title: str, body: str) -> bool:
    try:
        proc = subprocess.run(
            [
                "herdr",
                "notification",
                "show",
                title,
                "--body",
                body,
                "--sound",
                "request",
            ],
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        command_runtime.write_stderr(
            f"phase-await: human notification failed: {error}\n"
        )
        return False
    if proc.returncode != 0:
        command_runtime.write_stderr(
            f"phase-await: human notification exited {proc.returncode}: {proc.stderr.strip()}\n"
        )
        return False
    return True


def _escalate_unacked_urgent(
    ctx: PhaseContext,
    events: list[dict],
    escalate_ms: int,
    notify: Callable[[str, str], object],
) -> None:
    if escalate_ms <= 0:
        return
    now_ns = time.time_ns()
    for event in events:
        if event.get("severity") != "urgent" or not event.get("ack_required"):
            continue
        if ack_state(ctx, event["event_id"]) in ("acknowledged", "resolved"):
            continue
        marker = ctx.acks_dir / f"{event['event_id']}.notified"
        if marker.exists():
            continue
        try:
            occurred = dt.datetime.strptime(
                event["occurred_at"], "%Y-%m-%dT%H:%M:%SZ"
            ).replace(tzinfo=dt.timezone.utc)
        except ValueError:
            continue
        age_ms = (now_ns - int(occurred.timestamp() * 1_000_000_000)) / 1_000_000
        if age_ms < escalate_ms:
            continue
        delivered = notify(
            "Herdr plan needs attention",
            f"Phase {ctx.phase_id} pass {ctx.pass_number}: {event['kind']} is unacknowledged. Open the TUI for details.",
        )
        if delivered is not False:
            # A failed notification stays unmarked so the next sweep retries it.
            _write_json_atomic(marker, {"event_id": event["event_id"], "at_ns": now_ns})


def _probe_leader(pane_id: str) -> dict:
    try:
        proc = subprocess.run(
            ["herdr", "pane", "get", pane_id],
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired):
        return {"alive": None, "agent_status": None}
    if proc.returncode != 0:
        return {"alive": False, "agent_status": None}
    try:
        pane = json.loads(proc.stdout).get("result", {}).get("pane", {})
    except (json.JSONDecodeError, AttributeError):
        return {"alive": None, "agent_status": None}
    if not isinstance(pane, dict) or not pane:
        return {"alive": False, "agent_status": None}
    return {"alive": True, "agent_status": pane.get("agent_status")}


def _has_terminal_result(ctx: PhaseContext) -> bool:
    try:
        result = json.loads(ctx.result_path.read_text())
    except (OSError, json.JSONDecodeError):
        return False
    return isinstance(result, dict) and result.get("pass") == ctx.pass_number


def _deliverable(ctx: PhaseContext, events: list[dict], after: int) -> dict | None:
    last_sequence = events[-1]["sequence"] if events else 0
    for event in events:
        state = ack_state(ctx, event["event_id"])
        if state in ("acknowledged", "resolved"):
            continue
        if event["kind"] in _EPHEMERAL_KINDS and (
            event["sequence"] < last_sequence or event["sequence"] <= after
        ):
            record_ack(ctx, event["event_id"], "resolved", "phase-await")
            continue
        if event["sequence"] > after or event.get("ack_required"):
            return event
    return None


def await_event(
    receipt_path: str | Path,
    *,
    after: int = 0,
    timeout_ms: int = DEFAULT_TIMEOUT_MS,
    poll_ms: int = DEFAULT_POLL_MS,
    reconcile_ms: int = DEFAULT_RECONCILE_MS,
    inactivity_ms: int = DEFAULT_INACTIVITY_MS,
    escalate_ms: int = DEFAULT_ESCALATE_MS,
    actor: str = "owner",
    probe: Callable[[str], dict] | None = None,
    notify: Callable[[str, str], object] | None = None,
) -> dict:
    ctx = PhaseContext(Path(receipt_path))
    probe = probe or _probe_leader
    notify = notify or _notify_human
    deadline = time.monotonic() + timeout_ms / 1000
    next_reconcile = 0.0
    dead_sweeps = 0
    stalled_sweeps = 0
    last_status: str | None = None
    last_material_change = time.monotonic()
    last_journal_len = -1
    while True:
        promote_inbox(ctx)
        check_phase_result(ctx)
        now = time.monotonic()
        if now >= next_reconcile:
            next_reconcile = now + reconcile_ms / 1000
            observed = probe(ctx.leader_pane)
            alive = observed.get("alive")
            status = observed.get("agent_status")
            if status != last_status:
                last_status = cast(str | None, status)
                last_material_change = now
            if alive is False and not _has_terminal_result(ctx):
                dead_sweeps += 1
                if dead_sweeps >= STALL_CONFIRMATIONS:
                    publish_event(
                        ctx,
                        "leader-missing",
                        f"Leader pane {ctx.leader_pane} cannot be resolved and no pass-{ctx.pass_number} phase result exists.",
                        severity="urgent",
                        dedupe_key=f"leader-missing:pass-{ctx.pass_number}",
                        requested_action="inspect-and-decide",
                    )
            else:
                dead_sweeps = 0
            if (
                alive is True
                and status in ("idle", "done")
                and not _has_terminal_result(ctx)
            ):
                stalled_sweeps += 1
                if stalled_sweeps >= STALL_CONFIRMATIONS:
                    publish_event(
                        ctx,
                        "leader-stalled",
                        f"Leader pane {ctx.leader_pane} reports agent status {status!r} but durable state says pass {ctx.pass_number} is still active with no phase result.",
                        severity="urgent",
                        dedupe_key=f"leader-stalled:pass-{ctx.pass_number}",
                        requested_action="inspect-and-decide",
                    )
            else:
                stalled_sweeps = 0
        events = read_journal(ctx)
        if len(events) != last_journal_len:
            last_journal_len = len(events)
            last_material_change = time.monotonic()
        if (
            inactivity_ms > 0
            and (time.monotonic() - last_material_change) * 1000 >= inactivity_ms
        ):
            published = publish_event(
                ctx,
                "inactivity-checkpoint",
                f"No durable movement for {inactivity_ms} ms on phase {ctx.phase_id} pass {ctx.pass_number}; ambiguous-state check advised.",
                severity="attention",
                dedupe_key=f"inactivity:pass-{ctx.pass_number}:{last_journal_len}",
                requested_action="run-one-shot-checker",
            )
            if published is not None:
                events = read_journal(ctx)
        _escalate_unacked_urgent(ctx, events, escalate_ms, notify)
        event = _deliverable(ctx, events, after)
        if event is not None:
            record_ack(ctx, event["event_id"], "returned-to-owner", actor)
            return event
        terminal = next((e for e in events if e.get("terminal")), None)
        if terminal is not None:
            raise AwaitError(
                f"phase {ctx.phase_id} pass {ctx.pass_number} is terminal ({terminal['kind']}, sequence {terminal['sequence']}) and already acknowledged; nothing to await"
            )
        if time.monotonic() >= deadline:
            heartbeat = publish_event(
                ctx,
                "await-heartbeat",
                f"Quiet and healthy: leader {ctx.leader_pane} status {last_status!r}; {last_journal_len} journal event(s); re-await.",
                severity="info",
                ack_required=False,
            )
            if heartbeat is None:
                event = _deliverable(ctx, read_journal(ctx), after)
                if event is None:
                    raise AwaitError(
                        "await expired with a terminal journal but nothing deliverable"
                    )
                record_ack(ctx, event["event_id"], "returned-to-owner", actor)
                return event
            record_ack(ctx, heartbeat["event_id"], "returned-to-owner", actor)
            return heartbeat
        time.sleep(poll_ms / 1000)


def _cmd_wait(args: argparse.Namespace) -> int:
    event = await_event(
        args.receipt,
        after=args.after,
        timeout_ms=args.timeout_ms,
        poll_ms=args.poll_ms,
        reconcile_ms=args.reconcile_ms,
        inactivity_ms=args.inactivity_ms,
        escalate_ms=args.escalate_ms,
        actor=args.actor,
    )
    json.dump(event, command_runtime.stdout_stream(), indent=2, sort_keys=True)
    command_runtime.write_stdout("\n")
    return 0


def _cmd_ack(args: argparse.Namespace) -> int:
    ctx = PhaseContext(Path(args.receipt))
    ack = record_ack(ctx, args.event_id, args.state, args.actor)
    json.dump(ack, command_runtime.stdout_stream(), indent=2, sort_keys=True)
    command_runtime.write_stdout("\n")
    return 0


def _cmd_publish(args: argparse.Namespace) -> int:
    ctx = PhaseContext(Path(args.receipt))
    if args.kind not in contracts.EVENT_KINDS:
        raise AwaitError(f"unknown event kind {args.kind!r}")
    envelope: dict[str, object] = {
        "kind": args.kind,
        "summary": args.summary,
        "severity": args.severity,
    }
    if args.dedupe_key:
        envelope["dedupe_key"] = args.dedupe_key
    if args.request_id:
        envelope["request_id"] = args.request_id
    if args.requested_action:
        envelope["requested_action"] = args.requested_action
    if args.evidence:
        envelope["evidence"] = [
            {"kind": "durable-file", "path": path} for path in args.evidence
        ]
    ctx.inbox_dir.mkdir(parents=True, exist_ok=True)
    _write_json_atomic(ctx.inbox_dir / f"{uuid.uuid4().hex}.json", envelope)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = command_runtime.ArgumentParser(prog="upagent-phase-await")
    sub = parser.add_subparsers(dest="command", required=True)
    wait = sub.add_parser("wait")
    wait.add_argument("--receipt", required=True)
    wait.add_argument("--after", type=int, default=0)
    wait.add_argument("--timeout-ms", type=int, default=DEFAULT_TIMEOUT_MS)
    wait.add_argument("--poll-ms", type=int, default=DEFAULT_POLL_MS)
    wait.add_argument("--reconcile-ms", type=int, default=DEFAULT_RECONCILE_MS)
    wait.add_argument("--inactivity-ms", type=int, default=DEFAULT_INACTIVITY_MS)
    wait.add_argument("--escalate-ms", type=int, default=DEFAULT_ESCALATE_MS)
    wait.add_argument("--actor", default="owner")
    wait.set_defaults(handler=_cmd_wait)
    ack = sub.add_parser("ack")
    ack.add_argument("--receipt", required=True)
    ack.add_argument("--event-id", required=True)
    ack.add_argument(
        "--state", default="acknowledged", choices=list(contracts.ACK_STATES)
    )
    ack.add_argument("--actor", default="owner")
    ack.set_defaults(handler=_cmd_ack)
    publish = sub.add_parser("publish")
    publish.add_argument("--receipt", required=True)
    publish.add_argument("--kind", required=True)
    publish.add_argument("--summary", required=True)
    publish.add_argument("--severity", default="attention")
    publish.add_argument("--dedupe-key", default=None)
    publish.add_argument("--request-id", default=None)
    publish.add_argument(
        "--requested-action",
        default=None,
        help="what the owner should do with this event (e.g. iac-approval)",
    )
    publish.add_argument(
        "--evidence",
        action="append",
        default=None,
        help="durable file path the owner should read; repeatable",
    )
    publish.set_defaults(handler=_cmd_publish)
    args = parser.parse_args(argv)
    try:
        return cast(int, args.handler(args))
    except (AwaitError, contracts.ContractError) as error:
        command_runtime.write_stderr(f"upagent-phase-await: {error}\n")
        return 2


if __name__ == "__main__":
    sys.exit(main())
