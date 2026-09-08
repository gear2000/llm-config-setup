"""One durable nudge authority per target, shared by all triggers and payloads.

Use NudgeAuthority(ledger.root) with the caller's bound JobLedger. The standalone
request_nudge resolves the same root through hub_transport. UPAGENT_STATE names
service-state JSON, never a directory. No caller integration lives here.

Probe callbacks must freshly verify pane identity and owner-validated terminal
evidence, and derive their harness from a validated snapshot or receipt. Both
callbacks run under the target lock and must be bounded and non-reentrant.
A delivery exception propagates, leaving the durable reservation. Continue
intents, including aborted or uncertain sends, spend the episode cap. Inbox
reservations retry until delivery is recorded; a crash after sending but before
that write can repeat the fixed prompt, so inbox consumers must acknowledge seqs.
"""

from __future__ import annotations

import fcntl
import hashlib
import importlib.util
import json
import math
import os
import sys
import time
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Literal


def _sibling(name: str) -> Any:
    key = f"upagent_nudge_authority_{name}"
    if key in sys.modules:
        return sys.modules[key]
    spec = importlib.util.spec_from_file_location(
        key, Path(__file__).with_name(f"{name}.py")
    )
    if spec is None or spec.loader is None:
        raise RuntimeError(f"could not load nudge authority dependency {name}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[key] = module
    spec.loader.exec_module(module)
    return module


stall_nudge = _sibling("stall_nudge")
offerings = _sibling("offerings")
transport = _sibling("hub_transport")
PaneState = stall_nudge.PaneState
Verdict = stall_nudge.Verdict
INBOX_PAYLOAD = "read your inbox"
Payload = Literal["continue", "inbox"]
Outcome = Literal[
    "delivered",
    "backoff",
    "cap-exhausted",
    "aborted",
    "not-idle",
    "finished",
    "nothing-pending",
]


class NudgeAuthorityError(ValueError):
    """Corrupt authority state or invalid identity/input; never a quiet reset."""


@dataclass(frozen=True)
class TargetIdentity:
    herdr_session: str
    workspace_id: str
    pane_id: str
    agent_name: str
    owner_kind: Literal["request", "flow1-run"]
    owner_id: str

    def __post_init__(self) -> None:
        if any(
            not isinstance(value, str) or not value.strip()
            for value in asdict(self).values()
        ):
            raise NudgeAuthorityError(
                "target identity fields must be non-empty strings"
            )
        if self.owner_kind not in ("request", "flow1-run"):
            raise NudgeAuthorityError("invalid target owner_kind")

    def canonical(self) -> dict:
        return {"v": 1, **asdict(self)}

    @property
    def target_id(self) -> str:
        return _digest(self.canonical())


@dataclass(frozen=True)
class Probe:
    target: TargetIdentity | None
    status: str | None
    terminal_valid: bool
    pane_state: PaneState
    harness: str

    def classify(self) -> Verdict:
        if type(self.terminal_valid) is not bool:
            raise NudgeAuthorityError("probe terminal_valid must be a boolean")
        return stall_nudge.classify(
            self.status,
            self.terminal_valid,
            self.pane_state,
            offerings.completion_style(self.harness),
        )


@dataclass(frozen=True)
class NudgeOutcome:
    outcome: Outcome
    reason: str
    episode: int
    envelope_seqs: tuple[int, ...] = ()


def _digest(value: dict) -> str:
    canonical = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _intent_digest(target: TargetIdentity, episode: int, index: int) -> str:
    return _digest(
        {
            "v": 1,
            "target_id": target.target_id,
            "episode": episode,
            "nudge_index": index,
        }
    )


def _object(pairs: list[tuple[str, Any]]) -> dict:
    result: dict = {}
    for key, value in pairs:
        if key in result:
            raise NudgeAuthorityError(f"duplicate authority state key {key!r}")
        result[key] = value
    return result


def _integer(value: Any, minimum: int = 0) -> bool:
    return type(value) is int and value >= minimum


def _verify(state: Any, target: TargetIdentity) -> None:
    if (
        not isinstance(state, dict)
        or type(state.get("schema")) is not int
        or state["schema"] != 1
    ):
        raise NudgeAuthorityError("invalid authority state schema")
    if (
        state.get("target") != target.canonical()
        or _digest(state["target"]) != target.target_id
    ):
        raise NudgeAuthorityError("authority target identity mismatch")
    if state.get("target_id") != target.target_id:
        raise NudgeAuthorityError("authority target hash mismatch")
    episode = state.get("continue")
    if not (
        isinstance(episode, dict)
        and type(episode.get("schema")) is int
        and episode["schema"] == 2
        and _integer(episode.get("episode"))
        and episode.get("last_verdict") in (None, "NUDGE", "WORKING")
        and "last_verdict" in episode
        and type(episode.get("escalated")) is bool
        and isinstance(episode.get("nudges"), list)
        and len(episode["nudges"]) <= stall_nudge.NUDGE_CAP
    ):
        raise NudgeAuthorityError("invalid continue episode state")
    if episode["nudges"] and (
        episode["episode"] == 0 or episode["last_verdict"] is None
    ):
        raise NudgeAuthorityError("continue intents have no idle episode")
    previous_at = 0.0
    for index, item in enumerate(episode["nudges"]):
        if not (
            isinstance(item, dict)
            and type(item.get("at")) in (int, float)
            and math.isfinite(item["at"])
            and item["at"] >= previous_at
            and type(item.get("delivered")) is bool
            and item.get("state") in ("reserved", "delivered", "aborted")
            and item["delivered"] == (item["state"] == "delivered")
            and isinstance(item.get("trigger"), str)
            and item["trigger"].strip()
            and isinstance(item.get("reason"), str)
        ):
            raise NudgeAuthorityError("invalid continue intent")
        if item.get("digest") != _intent_digest(target, episode["episode"], index):
            raise NudgeAuthorityError("continue intent hash mismatch")
        previous_at = item["at"]
    inbox = state.get("inbox")
    if not isinstance(inbox, dict):
        raise NudgeAuthorityError("invalid inbox state")
    for seq, item in inbox.items():
        if not (seq.isascii() and seq.isdecimal() and str(int(seq)) == seq):
            raise NudgeAuthorityError("invalid inbox sequence")
        if not (
            isinstance(item, dict)
            and item.get("state") in ("reserved", "delivered", "aborted")
            and _integer(item.get("at_ns"))
            and _integer(item.get("attempts"), 1)
            and isinstance(item.get("trigger"), str)
            and item["trigger"].strip()
            and isinstance(item.get("reason"), str)
        ):
            raise NudgeAuthorityError("invalid inbox reservation")


class NudgeAuthority:
    """Bind authority storage to an already-resolved JobLedger.root."""

    def __init__(self, ledger_root: str | Path) -> None:
        self.root = Path(ledger_root).expanduser().resolve() / "nudge"

    def state_path(self, target: TargetIdentity) -> Path:
        return self.root / f"{target.target_id}.json"

    def _load(self, target: TargetIdentity) -> dict:
        path = self.state_path(target)
        try:
            raw = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return {
                "schema": 1,
                "target": target.canonical(),
                "target_id": target.target_id,
                "continue": {
                    "schema": 2,
                    "episode": 0,
                    "last_verdict": None,
                    "nudges": [],
                    "escalated": False,
                },
                "inbox": {},
            }
        except (OSError, UnicodeError) as error:
            raise NudgeAuthorityError(
                f"cannot read authority state {path}: {error}"
            ) from error
        try:
            state = json.loads(raw, object_pairs_hook=_object)
        except json.JSONDecodeError as error:
            raise NudgeAuthorityError(
                f"corrupt authority state {path}: {error}"
            ) from error
        _verify(state, target)
        return state

    def _save(self, target: TargetIdentity, state: dict) -> None:
        _verify(state, target)
        path = self.state_path(target)
        temporary = path.with_suffix(".tmp")
        # The sibling lock serializes writers; replace keeps unlocked readers safe.
        with temporary.open("w", encoding="utf-8") as stream:
            json.dump(state, stream, sort_keys=True, indent=2, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory_fd = os.open(self.root, os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)

    def observe(
        self, target: TargetIdentity, probe: Callable[[], Probe]
    ) -> NudgeOutcome:
        """Record an episode transition without reserving or delivering a payload.

        Owners use this for the first idle confirmation and for WORKING. A fresh
        idle probe during a WORKING observation must never bypass their debounce.
        """
        self.root.mkdir(parents=True, exist_ok=True)
        with self.state_path(target).with_suffix(".lock").open("a+") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            try:
                state = self._load(target)
                episode = state["continue"]
                current = probe()
                verdict = current.classify()
                if verdict == Verdict.FINISHED:
                    return NudgeOutcome("finished", verdict.value, episode["episode"])
                if current.pane_state == PaneState.PRESENT and current.target != target:
                    return NudgeOutcome(
                        "aborted", "target identity mismatch", episode["episode"]
                    )
                before = json.dumps(episode, sort_keys=True)
                stall_nudge.record_verdict(episode, verdict)
                if json.dumps(episode, sort_keys=True) != before:
                    self._save(target, state)
                return NudgeOutcome("not-idle", verdict.value, episode["episode"])
            finally:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

    def mark_escalated(self, target: TargetIdentity, episode_number: int) -> None:
        """Record a published escalation only if its episode is still current."""
        with self.state_path(target).with_suffix(".lock").open("a+") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            try:
                state = self._load(target)
                episode = state["continue"]
                if episode["episode"] == episode_number:
                    episode["escalated"] = True
                    self._save(target, state)
            finally:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

    def request_nudge(
        self,
        target: TargetIdentity,
        payload: Payload,
        trigger: str,
        probe: Callable[[], Probe],
        deliver: Callable[[str], None],
        envelope_seqs: Sequence[int] = (),
        *,
        wait_for_lock: bool = True,
    ) -> NudgeOutcome:
        """Reserve, re-probe, and send under one interprocess target lock.

        Only continue calls decide or consumes the cap. Every call observes episode
        transitions, including working probes made by an inbox caller. A re-probe
        change releases reservations as aborted and records the reason. Continue
        intent history remains spent, matching Shared A's failed-send semantics.
        """
        if payload not in ("continue", "inbox"):
            raise NudgeAuthorityError("unknown nudge payload class")
        if not isinstance(trigger, str) or not trigger.strip():
            raise NudgeAuthorityError("nudge trigger must be a non-empty string")
        seqs = tuple(envelope_seqs)
        if any(not _integer(seq) for seq in seqs):
            raise NudgeAuthorityError(
                "envelope sequences must be non-negative integers"
            )
        if payload == "continue" and seqs:
            raise NudgeAuthorityError("continue cannot reserve inbox envelopes")
        seqs = tuple(sorted(set(seqs)))
        if type(wait_for_lock) is not bool:
            raise NudgeAuthorityError("wait_for_lock must be a boolean")
        self.root.mkdir(parents=True, exist_ok=True)
        with self.state_path(target).with_suffix(".lock").open("a+") as lock:
            flags = fcntl.LOCK_EX | (0 if wait_for_lock else fcntl.LOCK_NB)
            try:
                fcntl.flock(lock.fileno(), flags)
            except BlockingIOError:
                # A sweep owner must keep servicing its own bounded leases.
                # No state is read or changed without the lock; episode 0 is unknown.
                return NudgeOutcome("backoff", "target authority lock busy", 0)
            try:
                return self._request_locked(
                    target, payload, trigger, probe, deliver, seqs
                )
            finally:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

    def _request_locked(
        self,
        target: TargetIdentity,
        payload: Payload,
        trigger: str,
        probe: Callable[[], Probe],
        deliver: Callable[[str], None],
        seqs: tuple[int, ...],
    ) -> NudgeOutcome:
        state = self._load(target)
        episode = state["continue"]
        first = probe()
        verdict = first.classify()
        if verdict == Verdict.FINISHED:
            return NudgeOutcome(
                "finished", "validated terminal evidence", episode["episode"]
            )
        if first.pane_state == PaneState.PRESENT and first.target != target:
            return NudgeOutcome(
                "aborted", "target identity mismatch", episode["episode"]
            )
        before = json.dumps(episode, sort_keys=True)
        stall_nudge.record_verdict(episode, verdict)
        if json.dumps(episode, sort_keys=True) != before:
            self._save(target, state)
        if verdict != Verdict.NUDGE:
            return NudgeOutcome("not-idle", verdict.value, episode["episode"])
        records: list[dict] = []
        if payload == "continue":
            now = time.time()
            decision = stall_nudge.decide(episode, now)
            if decision == "hold":
                return NudgeOutcome(
                    "backoff", "continue ladder backoff", episode["episode"]
                )
            if decision == "exhausted":
                return NudgeOutcome(
                    "cap-exhausted", "continue cap spent", episode["episode"]
                )
            digest = _intent_digest(target, episode["episode"], len(episode["nudges"]))
            stall_nudge.record_nudge(episode, at=now, digest=digest, delivered=False)
            intent = episode["nudges"][-1]
            intent.update(state="reserved", trigger=trigger, reason="")
            records.append(intent)
            text = stall_nudge.NUDGE_PAYLOAD
        else:
            pending = []
            for seq in seqs:
                existing = state["inbox"].get(str(seq))
                if existing is not None and existing["state"] == "delivered":
                    continue
                attempts = 1
                if existing is not None:
                    attempts = existing["attempts"] + 1
                record = {
                    "state": "reserved",
                    "at_ns": time.time_ns(),
                    "attempts": attempts,
                    "trigger": trigger,
                    "reason": "",
                }
                state["inbox"][str(seq)] = record
                records.append(record)
                pending.append(seq)
            seqs = tuple(pending)
            if not seqs:
                return NudgeOutcome(
                    "nothing-pending", "no undelivered envelopes", episode["episode"]
                )
            text = INBOX_PAYLOAD
        self._save(target, state)
        second = probe()
        second_verdict = second.classify()
        reason = ""
        if second_verdict == Verdict.FINISHED:
            reason = "validated terminal evidence arrived before send"
        elif second.target != target:
            reason = "target identity mismatch before send"
        elif second != first:
            reason = "pane status, presence, or harness changed before send"
        if reason:
            for record in records:
                record.update(state="aborted", reason=reason)
            # A replacement pane cannot close this target's idle episode.
            if second.target == target:
                stall_nudge.record_verdict(episode, second_verdict)
            self._save(target, state)
            return NudgeOutcome("aborted", reason, episode["episode"], seqs)
        deliver(text)
        for record in records:
            record["state"] = "delivered"
        if payload == "continue":
            stall_nudge.mark_delivered(episode, digest)
        self._save(target, state)
        return NudgeOutcome("delivered", "delivery recorded", episode["episode"], seqs)


def request_nudge(
    target: TargetIdentity,
    payload: Payload,
    trigger: str,
    probe: Callable[[], Probe],
    deliver: Callable[[str], None],
    envelope_seqs: Sequence[int] = (),
) -> NudgeOutcome:
    """Standalone entry point using the canonical client/detached-runner root."""
    return NudgeAuthority(transport.ledger_path(Path(__file__).parent)).request_nudge(
        target,
        payload,
        trigger,
        probe,
        deliver,
        envelope_seqs,
    )
