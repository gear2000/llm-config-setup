#!/usr/bin/env python3
"""Standalone, registry-only Flow 1 supervision. Never owns request terminality.

The run lock is held for the process lifetime. Checker polling shares the five
second loop, so a Checker cannot delay worker sweeps, SIGTERM, or finish drains.
Only the nudge authority writes beneath the canonical ledger root.
"""

from __future__ import annotations

import argparse
import fcntl
import importlib.util
import json
import os
import re
import signal
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any


def _load(name: str) -> Any:
    key = f"upagent_run_watch_{name}"
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


authority = _load("nudge_authority")
offerings = authority.offerings
awaiter = _load("implementer_await")
management = _load("llm_management")
process_identity = _load("process_identity")
recruiter = _load("recruiter")
POLL_SECONDS = 5
CHECKER_SECONDS = 240  # Reserve a minute for an in-flight probe and verified cleanup.
TERMINAL = {"finished", "cleanup-failed"}


class RunWatchError(ValueError):
    """Invalid registry, policy, ownership, or evidence. No target is guessed."""


def _bind_recruiter_runtime(runtime: Any) -> None:
    global recruiter
    recruiter = runtime


def read_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as error:
        raise RunWatchError(f"cannot read {path}: {error}") from error
    if not isinstance(value, dict):
        raise RunWatchError(f"{path} must contain an object")
    return value


def write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    with temporary.open("x", encoding="utf-8") as stream:
        os.chmod(temporary, 0o600)
        json.dump(value, stream, indent=2, sort_keys=True, allow_nan=False)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)
    fd = os.open(path.parent, os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def validate_record(value: dict) -> dict:
    if set(value) != {
        "request_id",
        "payload_sha256",
        "order_id",
        "generation",
        "placed_at_ns",
    }:
        raise RunWatchError("registry record has missing or unknown fields")
    if not isinstance(value["request_id"], str) or not re.fullmatch(
        r"[A-Za-z0-9_-]+", value["request_id"]
    ):
        raise RunWatchError("invalid registry request_id")
    if not isinstance(value["payload_sha256"], str) or not re.fullmatch(
        r"[a-f0-9]{64}", value["payload_sha256"]
    ):
        raise RunWatchError("invalid registry payload_sha256")
    if not isinstance(value["order_id"], str) or not value["order_id"]:
        raise RunWatchError("invalid registry order_id")
    for field in ("generation", "placed_at_ns"):
        if type(value[field]) is not int or value[field] <= 0:
            raise RunWatchError(f"invalid registry {field}")
    return value


def register_worker(run_root: Path, response: dict) -> dict:
    state = response.get("state")
    if not isinstance(state, dict):
        raise RunWatchError("accepted response has no state object")
    if "requester_control_token" in state:
        raise RunWatchError("redact requester_control_token before registration")
    record = validate_record(
        {
            "request_id": response.get("request_id"),
            "payload_sha256": response.get("payload_sha256"),
            "order_id": state.get("order_id"),
            "generation": state.get("generation"),
            "placed_at_ns": time.time_ns(),
        }
    )
    directory = run_root.resolve() / "control" / "workers"
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / ".registry.lock").open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        path = directory / (record["request_id"] + ".json")
        if path.exists():
            existing = validate_record(read_json(path))
            if any(existing[k] != record[k] for k in record if k != "placed_at_ns"):
                raise RunWatchError(
                    "registry identity/generation mismatch on attachment"
                )
            return existing
        write_json(path, record)
    return record


@dataclass
class Target:
    identity: Any
    harness: str
    generation: int
    order: dict
    record: dict | None = None
    terminal: bool = False
    held: bool = False


class Runtime:
    """Read canonical request evidence and perform identity-checked Herdr I/O."""

    def __init__(self, ctx: Any, roster: dict):
        self.ctx = ctx
        self.roster = roster
        self.ledger_root = authority.transport.ledger_path(Path(__file__).parent)
        self.nudges = authority.NudgeAuthority(self.ledger_root)
        self.config = management.load_management_config(roster)

    def resolve(self, record: dict) -> Target | None:
        # Same canonical ledger key as `just upagent get --request`. Read only
        # this registered request, never enumerate requests or match by cwd.
        directory = (
            self.ledger_root
            / "requests"
            / recruiter.JobLedger.key(record["request_id"])
        )
        order = recruiter.load_order(directory / "request.json")
        state = read_json(directory / "state" / "latest.json")
        public = order.get("public_request", {})
        if (
            recruiter.lifecycle.request_identity(order) != record["request_id"]
            or public.get("payload_sha256") != record["payload_sha256"]
            or order.get("order_id") != record["order_id"]
            or state.get("order_id") != record["order_id"]
            or state.get("request_id") != record["request_id"]
            or type(state.get("generation")) is not int
            or state["generation"] != record["generation"]
        ):
            raise RunWatchError("registered request identity/generation mismatch")
        snapshot = offerings.validate_snapshot(order.get("offering_snapshot"))
        harness = snapshot["harness"]
        if state.get("state") in TERMINAL:
            return Target(
                None, harness, record["generation"], order, record, terminal=True
            )
        journals = [read_json(p) for p in (directory / "launches").glob("*.json")]
        workers = [
            j
            for j in journals
            if j.get("role") == "worker" and j.get("state") == "started"
        ]
        if not workers:
            return None  # Accepted but not landed yet; stays in the drain set.
        journal = max(workers, key=lambda j: j.get("started_at_ns", 0))
        if journal.get("generation") != record["generation"]:
            raise RunWatchError("worker launch generation mismatch")
        identity = authority.TargetIdentity(
            journal.get("herdr_session"),
            journal.get("workspace_id"),
            journal.get("pane"),
            journal.get("agent_name"),
            "request",
            record["request_id"],
        )
        held = (
            order.get("completion_policy") == "requester_release"
            or state.get("state") != "running"
        )
        return Target(identity, harness, record["generation"], order, record, held=held)

    def implementer(self) -> Target:
        receipt = self.ctx.receipt
        harness = receipt.get("harness")
        offerings.completion_style(harness)
        # Older receipts predate agent_name but used this fixed name function.
        name = receipt.get("agent_name")
        if name is None:
            controller = _load("implementer_controller")
            name = controller._safe_name(self.ctx.run_id)
        identity = authority.TargetIdentity(
            self.ctx.herdr_session,
            receipt.get("workspace_id"),
            self.ctx.leader_pane,
            name,
            "flow1-run",
            self.ctx.run_id,
        )
        order = {
            "harness": harness,
            "model": receipt.get("model"),
            "cwd": str(self.ctx.run_root),
            "order_id": "flow1-" + self.ctx.run_id,
        }
        return Target(identity, harness, 1, order)

    def herdr(self, session: str, *args: str) -> dict:
        return recruiter._herdr_json(*args, herdr_session=session, timeout_seconds=3)

    def presence(self, identity: Any, harness: str, terminal: bool = False) -> Any:
        P = authority.PaneState
        if terminal:
            return authority.Probe(identity, None, True, P.UNKNOWN, harness)
        try:
            reply = self.herdr(identity.herdr_session, "pane", "get", identity.pane_id)
        except recruiter.RecruiterError as error:
            state = P.GONE if "pane_not_found" in str(error) else P.UNKNOWN
            return authority.Probe(identity, None, False, state, harness)
        pane = reply.get("result", {}).get("pane")
        if not isinstance(pane, dict):
            raise RunWatchError("Herdr probe has no pane object")
        try:
            reply = self.herdr(
                identity.herdr_session, "agent", "get", identity.agent_name
            )
        except recruiter.RecruiterError:
            return authority.Probe(None, None, False, P.UNKNOWN, harness)
        agent = reply.get("result", {}).get("agent", {})
        matches = (
            pane.get("pane_id") == identity.pane_id
            and pane.get("workspace_id") == identity.workspace_id
            and agent.get("pane_id") == identity.pane_id
            and agent.get("workspace_id") == identity.workspace_id
            and agent.get("name") == identity.agent_name
        )
        return authority.Probe(
            identity if matches else None,
            pane.get("agent_status"),
            False,
            P.PRESENT,
            harness,
        )

    def probe(self, target: Target) -> Any:
        if target.record is not None:
            current = self.resolve(target.record)
            if current is None:
                raise RunWatchError("registered worker launch disappeared")
            if current.terminal:
                return authority.Probe(
                    target.identity,
                    None,
                    True,
                    authority.PaneState.UNKNOWN,
                    target.harness,
                )
            if current.identity != target.identity or current.harness != target.harness:
                raise RunWatchError("worker identity changed before send")
            held = current.held
        else:
            current_receipt = read_json(self.ctx.control_dir / "implementer-start.json")
            identity_fields = (
                "run_id",
                "herdr_session",
                "workspace_id",
                "implementer_pane",
                "leader_pane",
                "agent_name",
                "harness",
                "model",
            )
            if any(
                current_receipt.get(key) != self.ctx.receipt.get(key)
                for key in identity_fields
            ):
                raise RunWatchError(
                    "implementer receipt identity changed; restart run-watch"
                )
            if awaiter.has_terminal_result(self.ctx):
                return authority.Probe(
                    target.identity,
                    None,
                    True,
                    authority.PaneState.UNKNOWN,
                    target.harness,
                )
            held = (self.ctx.control_dir / "finishing.json").exists()
        probe = self.presence(target.identity, target.harness)
        if held and probe.pane_state == authority.PaneState.PRESENT:
            return authority.Probe(
                probe.target, "blocked", False, probe.pane_state, target.harness
            )
        return probe

    def deliver(self, target: Target, text: str) -> None:
        if target.harness == "cursor":
            self.herdr(
                target.identity.herdr_session,
                "pane",
                "send-text",
                target.identity.pane_id,
                text,
            )
            time.sleep(recruiter.CURSOR_PROMPT_PASTE_SETTLE_SECONDS)
            self.herdr(
                target.identity.herdr_session,
                "pane",
                "send-keys",
                target.identity.pane_id,
                "Enter",
            )
        else:
            self.herdr(
                target.identity.herdr_session,
                "pane",
                "run",
                target.identity.pane_id,
                text,
            )

    def publish(self, key: str, message: str) -> None:
        for event in awaiter.read_journal(self.ctx):
            if event.get("dedupe_key") == key:
                awaiter.record_ack(self.ctx, event["event_id"], "resolved", "run-watch")
        awaiter.publish_event(
            self.ctx,
            "advisory",
            message,
            severity="attention",
            ack_required=False,
            dedupe_key=key,
        )

    def recent_output(self, identity: Any) -> str:
        result = subprocess.run(
            [
                "herdr",
                "--session",
                identity.herdr_session,
                "pane",
                "read",
                identity.pane_id,
                "--source",
                "recent-unwrapped",
                "--lines",
                "120",
            ],
            capture_output=True,
            text=True,
            timeout=3,
            check=True,
        )
        return result.stdout[-8000:]

    def checker_start(self, target: Target, directory: Path, now: float) -> dict:
        candidates = recruiter._checker_candidates(target.order, self.config)
        candidate = candidates[0]
        provider = candidate.provider
        if provider == "legacy-unclassified":
            provider = recruiter._sentinel_command_provider(candidate.role.command)
        if provider == "unknown" or provider == recruiter._worker_provider(
            target.order
        ):
            raise RunWatchError("no provider-disjoint Checker candidate")
        identity = target.identity
        evidence = {
            "target": identity.canonical(),
            "generation": target.generation,
            "at": now,
            "probe": self.probe(target).classify().value,
            "recent_output": self.recent_output(identity),
        }
        write_json(directory / "evidence.json", evidence)
        output = directory / "assessment.json"
        brief = directory / "brief.md"
        brief.write_text(
            management.checker_brief(
                identity.owner_id,
                target.generation,
                directory / "evidence.json",
                output,
            ).replace(
                "Read it and, only when useful, read the named worker pane's recent output.",
                "Read only that snapshot. Do not read the live pane.",
            )
            + "\nUse only this one evidence snapshot. Do not read live pane output. Then exit.\n"
        )
        command = management.render_role_command(
            candidate.role, brief, target.order["cwd"], output
        )
        pane = self.herdr(identity.herdr_session, "pane", "get", identity.pane_id)[
            "result"
        ]["pane"]
        name = "run-watch-check-" + uuid.uuid4().hex[:16]
        launch = {
            "name": name,
            "herdr_session": identity.herdr_session,
            "workspace_id": identity.workspace_id,
            "provider": provider,
            "request_id": identity.owner_id,
            "generation": target.generation,
            "output": str(output),
            "deadline": now + CHECKER_SECONDS,
            "state": "launching",
            "harness": candidate.role.expected_agent,
        }
        write_json(directory / "launch.json", launch)
        reply = self.herdr(
            identity.herdr_session,
            "agent",
            "start",
            name,
            "--cwd",
            target.order["cwd"],
            "--tab",
            pane["tab_id"],
            "--split",
            "down",
            "--no-focus",
            "--",
            "bash",
            "-lc",
            command,
        )
        agent = reply.get("result", {}).get("agent", {})
        launch.update(pane_id=agent.get("pane_id"), state="running")
        write_json(directory / "launch.json", launch)
        if not isinstance(launch["pane_id"], str) or not launch["pane_id"]:
            raise RunWatchError("Checker launch returned no pane identity")
        return launch

    def checker_close(self, launch: dict) -> None:
        session, pane = launch["herdr_session"], launch.get("pane_id")
        if not pane:
            try:
                reply = self.herdr(session, "agent", "get", launch["name"])
            except recruiter.RecruiterError as error:
                if "agent_not_found" in str(error):
                    return
                raise
            pane = reply.get("result", {}).get("agent", {}).get("pane_id")
        if not pane:
            raise RunWatchError("Checker cleanup cannot resolve launch identity")
        identity = authority.TargetIdentity(
            session,
            launch["workspace_id"],
            pane,
            launch["name"],
            "flow1-run",
            launch["request_id"],
        )
        probe = self.presence(identity, "claude")
        if probe.pane_state == authority.PaneState.GONE:
            return
        if probe.target != identity or probe.pane_state != authority.PaneState.PRESENT:
            raise RunWatchError("Checker cleanup identity unverified")
        self.herdr(session, "pane", "close", pane)
        # Herdr pane close destroys the PTY and its process tree. Verify absence
        # immediately, even when the Checker has already exited on its own.
        try:
            self.herdr(session, "pane", "get", pane)
        except recruiter.RecruiterError as error:
            if "pane_not_found" in str(error):
                return
            raise
        raise RunWatchError("Checker pane still present after force-close")

    def checker_poll(self, launch: dict, now: float) -> tuple[bool, str | None]:
        output = Path(launch["output"])
        if output.exists():
            try:
                assessment = recruiter.lifecycle.parse_check_assessment(
                    output.read_text(), launch["request_id"], launch["generation"]
                )
            except recruiter.lifecycle.LifecycleError as error:
                return True, f"checker produced nothing: malformed output: {error}"
            return True, assessment.message
        if now >= launch["deadline"]:
            return True, None
        identity = authority.TargetIdentity(
            launch["herdr_session"],
            launch["workspace_id"],
            launch["pane_id"],
            launch["name"],
            "flow1-run",
            launch["request_id"],
        )
        probe = self.presence(identity, "claude")
        if probe.pane_state == authority.PaneState.GONE:
            return True, None
        if probe.pane_state == authority.PaneState.PRESENT:
            process = self.herdr(
                identity.herdr_session,
                "pane",
                "process-info",
                "--pane",
                identity.pane_id,
            )
            info = process.get("result", {}).get("process_info", {})
            if info.get("foreground_processes") == []:
                return True, None
        return False, None


class Watch:
    def __init__(self, run_root: Path, policy: dict, runtime: Any, *, clock=time.time):
        self.root = run_root.resolve()
        self.path = self.root / "control" / "run-watch.json"
        self.policy = offerings.validate_run_watch(policy)
        self.runtime = runtime
        self.clock = clock
        self.stop = False
        previous = read_json(self.path) if self.path.exists() else {}
        self.data = {
            "state": "running",
            "last_sweep": None,
            "ownership": {
                "pid": os.getpid(),
                "process_start_time": process_identity.process_start_time(os.getpid()),
                "argv_marker": "run-watch",
                "argv": process_identity.process_cmdline(os.getpid()),
                "herdr_session": runtime.ctx.herdr_session,
                "workspace_id": getattr(runtime.ctx, "receipt", {}).get("workspace_id"),
                "pane_id": None,
                "agent_name": "run-watch",
            },
            "run_watch": self.policy,
            "advisories": previous.get("advisories", {}),
            "completed": previous.get("completed", []),
            "checks": previous.get("checks", {}),
            "provider_failures": previous.get("provider_failures", {}),
            "remaining_request_ids": previous.get("remaining_request_ids", []),
            "drain_deadline": previous.get("drain_deadline"),
            "configuration_at_ns": time.time_ns(),
        }
        # Recover even a crash between launch intent and the status-file update.
        for path in (self.root / "control" / "checks").glob("*/launch.json"):
            launch = read_json(path)
            if launch.get("state") in ("running", "launching"):
                previous_check = self.data["checks"].get(path.parent.name, {})
                if previous_check.get("state") not in ("finished", "failed"):
                    self.data["checks"][path.parent.name] = launch
        self.save()

    def save(self) -> None:
        write_json(self.path, self.data)

    def advisory(self, key: str, message: str) -> None:
        now = self.clock()
        previous = self.data["advisories"].get(key)
        if previous is not None and now - previous["at"] < 3600:
            return
        self.runtime.publish(key, message)
        self.data["advisories"][key] = {"at": now, "message": message}
        self.save()

    def sweep(self, *, draining: bool = False) -> list[str]:
        remaining = []
        targets = []
        for path in sorted((self.root / "control" / "workers").glob("*.json")):
            try:
                record = validate_record(read_json(path))
            except RunWatchError as error:
                self.advisory(
                    "run-watch:registry:" + path.stem, f"registry-invalid: {error}"
                )
                remaining.append(path.stem)
                continue
            if path.stem != record["request_id"]:
                self.advisory(
                    "run-watch:registry:" + path.stem,
                    "registry-invalid: filename identity mismatch",
                )
                remaining.append(path.stem)
                continue
            fingerprint = f"{record['request_id']}:{record['generation']}:{record['payload_sha256']}"
            if fingerprint in self.data["completed"]:
                continue
            try:
                target = self.runtime.resolve(record)
            except (
                RunWatchError,
                recruiter.ContractError,
                offerings.OfferingError,
                authority.NudgeAuthorityError,
            ) as error:
                self.advisory(
                    "run-watch:registry:" + record["request_id"],
                    f"registry-rejected: {error}",
                )
                remaining.append(record["request_id"])
                continue
            if target is not None and target.terminal:
                self.data["completed"].append(fingerprint)
                continue
            remaining.append(record["request_id"])
            if target is not None:
                targets.append((target, fingerprint))
        if not draining and "implementer" not in self.data["completed"]:
            targets.append((self.runtime.implementer(), "implementer"))
        for target, fingerprint in targets:
            self.poll_checks(close=draining or self.stop)
            if (self.stop and not draining) or (
                draining and self.clock() >= self.data["drain_deadline"]
            ):
                break
            try:
                probe = self.runtime.probe(target)
            except RunWatchError as error:
                self.advisory(
                    "run-watch:identity:" + target.identity.pane_id,
                    f"identity-rejected: {error}",
                )
                continue
            verdict = probe.classify()
            pane = target.identity.pane_id
            if verdict == authority.Verdict.FINISHED:
                self.data["completed"].append(fingerprint)
                if target.record:
                    remaining.remove(target.record["request_id"])
            elif verdict == authority.Verdict.GONE:
                self.advisory(
                    "run-watch:gone:" + pane,
                    f"Pane {pane} is gone without validated terminal evidence",
                )
                if draining and target.record:
                    remaining.remove(target.record["request_id"])
            elif probe.pane_state == authority.PaneState.UNKNOWN:
                self.advisory(
                    "run-watch:probe:" + pane, f"probe-unknown: holding pane {pane}"
                )
            elif probe.target != target.identity:
                self.advisory(
                    "run-watch:identity:" + pane,
                    f"identity-rejected: holding pane {pane}",
                )
            elif verdict == authority.Verdict.HOLD:
                if probe.status == "blocked":
                    self.advisory(
                        "run-watch:blocked:" + pane, f"Pane {pane} is blocked"
                    )
            else:
                try:
                    outcome = self.runtime.nudges.request_nudge(
                        target.identity,
                        "continue",
                        "run-watch",
                        lambda: self.runtime.probe(target),
                        lambda text: self.runtime.deliver(target, text),
                        wait_for_lock=False,
                    )
                except RunWatchError as error:
                    self.advisory(
                        "run-watch:identity:" + pane, f"identity-rejected: {error}"
                    )
                    continue
                if outcome.outcome == "finished":
                    self.data["completed"].append(fingerprint)
                    if target.record:
                        remaining.remove(target.record["request_id"])
                if outcome.outcome == "cap-exhausted" and not draining:
                    self.escalate(target, outcome.episode)
        self.data.update(last_sweep=self.clock(), remaining_request_ids=remaining)
        self.save()
        return remaining

    def escalate(self, target: Target, episode: int) -> None:
        key = f"{target.identity.target_id}-{episode}"
        if key in self.data["checks"]:
            return
        # One active assessment keeps every lease serviceable within its deadline,
        # even when several targets exhaust their ladders in the same sweep.
        if any(
            check["state"] in ("running", "launching", "reserved")
            for check in self.data["checks"].values()
        ):
            return
        directory = self.root / "control" / "checks" / key
        # Persist the reservation before starting a one-shot assessment.
        self.data["checks"][key] = {
            "state": "reserved",
            "target_pane": target.identity.pane_id,
        }
        self.save()
        try:
            launch = self.runtime.checker_start(target, directory, self.clock())
        except (RunWatchError, recruiter.RecruiterError) as error:
            intent = directory / "launch.json"
            if intent.exists():
                launch = read_json(intent)
                launch["target_pane"] = target.identity.pane_id
                self.data["checks"][key] = launch
                self.save()
                # A partial launch still owns cleanup. Never retry into a second pane.
                self.runtime.checker_close(launch)
            self.data["checks"][key]["state"] = "failed"
            self.advisory(
                "run-watch:stuck:" + target.identity.pane_id,
                f"checker produced nothing: {error}",
            )
        else:
            launch["target_pane"] = target.identity.pane_id
            self.data["checks"][key] = launch
            write_json(directory / "launch.json", launch)
        self.runtime.nudges.mark_escalated(target.identity, episode)
        self.save()

    def poll_checks(self, *, close: bool = False) -> None:
        for key, launch in self.data["checks"].items():
            if launch["state"] == "reserved":
                launch["state"] = "failed"
                self.advisory(
                    "run-watch:stuck:" + launch["target_pane"],
                    "checker produced nothing: interrupted before launch",
                )
                self.save()
            if launch["state"] not in ("running", "launching"):
                continue
            done, message = (
                (True, None)
                if close or launch["state"] == "launching"
                else self.runtime.checker_poll(launch, self.clock())
            )
            if not done:
                continue
            self.runtime.checker_close(launch)
            launch.update(
                state="finished", verified_absent=True, finished_at=self.clock()
            )
            write_json(self.root / "control" / "checks" / key / "launch.json", launch)
            pane = launch.get("target_pane", launch["request_id"])
            empty = message is None or message.startswith("checker produced nothing")
            self.advisory(
                "run-watch:stuck:" + pane, message or "checker produced nothing"
            )
            provider = launch["provider"]
            count = self.data["provider_failures"].get(provider, 0) + 1 if empty else 0
            self.data["provider_failures"][provider] = count
            if count >= 2:
                self.advisory(
                    "run-watch:provider:" + provider,
                    f"Checker provider {provider} produced nothing in two consecutive assessments",
                )
            self.save()

    def tick(self) -> bool:
        now = self.clock()
        receipt_path = self.root / "control" / "implementer-start.json"
        if (
            not receipt_path.exists()
            or read_json(receipt_path).get("state") == "failed"
        ):
            self.poll_checks(close=True)
            self.data["state"] = "orphaned"
            self.save()
            return False
        if not self.policy["enabled"]:
            self.poll_checks(close=True)
            self.data["state"] = "disabled"
            self.save()
            return False
        finishing = (
            self.stop or (self.root / "control" / "implementer-finish.json").exists()
        )
        if finishing and self.data["drain_deadline"] is None:
            self.data.update(
                state="draining", drain_deadline=now + self.policy["drain_minutes"] * 60
            )
            self.save()
        draining = self.data["drain_deadline"] is not None
        self.poll_checks(close=draining)
        last = self.data["last_sweep"]
        if (
            draining
            or last is None
            or now - last >= self.policy["interval_minutes"] * 60
        ):
            remaining = self.sweep(draining=draining)
            if draining and (
                not remaining or self.clock() >= self.data["drain_deadline"]
            ):
                self.data["state"] = "drain-timeout" if remaining else "finished"
                self.save()
                return False
        return True


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if args[:1] == ["register"]:
        parser = argparse.ArgumentParser(prog="upagent-register-worker")
        parser.add_argument("run_root", type=Path)
        parser.add_argument("response", type=Path)
        parsed = parser.parse_args(args[1:])
        print(json.dumps(register_worker(parsed.run_root, read_json(parsed.response))))
        return 0
    parser = argparse.ArgumentParser(prog="upagent-run-watch")
    parser.add_argument("run_root", type=Path)
    parser.add_argument("--roster", type=Path)
    parser.add_argument(
        "--once", action="store_true", help="perform one bounded poll/sweep"
    )
    parsed = parser.parse_args(args)
    roster_path = parsed.roster or offerings.resolve_roster_path(Path.cwd())
    roster = recruiter.load_roster(roster_path)
    policy = roster["run_watch"]
    root = parsed.run_root.resolve()
    control = root / "control"
    control.mkdir(parents=True, exist_ok=True)
    with (control / ".run-watch.lock").open("a+") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise RunWatchError("one run-watch already owns this run") from error
        receipt_path = control / "implementer-start.json"
        if (
            not receipt_path.exists()
            or read_json(receipt_path).get("state") == "failed"
        ):
            # Even orphan exits publish their process identity and effective policy.
            from types import SimpleNamespace

            ctx = SimpleNamespace(herdr_session=None)
        else:
            ctx = awaiter.ImplementerContext(receipt_path)
        runtime = Runtime(ctx, roster)
        watch = Watch(root, policy, runtime)
        previous_handler = signal.signal(
            signal.SIGTERM, lambda *_: setattr(watch, "stop", True)
        )
        try:
            while watch.tick():
                if parsed.once:
                    # --once owns no detached assessment: close anything it started.
                    watch.poll_checks(close=True)
                    watch.data["state"] = "stopped"
                    watch.save()
                    break
                time.sleep(POLL_SECONDS)
        finally:
            signal.signal(signal.SIGTERM, previous_handler)
            watch.poll_checks(close=True)
        print(json.dumps(watch.data))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (RunWatchError, offerings.OfferingError, recruiter.RecruiterError) as error:
        raise SystemExit(f"run-watch: {error}") from error
