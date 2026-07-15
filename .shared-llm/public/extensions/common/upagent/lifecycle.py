"""Pure UpAgent lifecycle contracts and durable requester mailbox.

This module contains no Herdr or harness code. It defines the small interface shared by any
requester, the Python Recruiter Hub, and the LLM management roles. Runtime adapters may execute
only values that have passed these contracts.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import time
from typing import Iterator
import uuid


REQUEST_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{2,127}$")
REQUESTER_KINDS = ("herdr-agent", "file-mailbox")
MANAGER_DECISIONS = ("approved", "needs-requester", "blocked")
CHECK_ASSESSMENTS = ("healthy", "suspected-stall", "startup-failed", "completed", "unknown")
RECOMMENDED_ACTIONS = ("none", "ask-requester", "retry-startup", "inspect", "extend", "cancel")
REQUESTER_ACTIONS = ("extend", "cancel")
MAX_EXTENSION_MS = 24 * 60 * 60 * 1000


class LifecycleError(ValueError):
    """A malformed lifecycle identity, address, LLM decision, or mailbox operation."""


@dataclass(frozen=True)
class RequesterAddress:
    requester_id: str
    kind: str
    address: str


@dataclass(frozen=True)
class ManagerDecision:
    request_id: str
    generation: int
    decision: str
    message: str
    requested_changes: tuple[str, ...] = ()


@dataclass(frozen=True)
class CheckAssessment:
    request_id: str
    generation: int
    assessment: str
    confidence: float
    evidence: tuple[str, ...]
    recommended_action: str
    message: str


@dataclass(frozen=True)
class RequesterDecision:
    request_id: str
    generation: int
    action: str
    extension_ms: int | None
    message: str


def _require_string(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise LifecycleError(f"{field} must be a non-empty string")
    return value


def _require_generation(value: object, expected: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value != expected:
        raise LifecycleError(f"generation must equal current generation {expected}")
    return value


def request_identity(order: dict) -> str:
    """Return the stable Hub identity for one logical order.

    A human-readable order id is scoped by its public result destination, which is already the
    caller's durable idempotency seam. This prevents two runs with the same phase/stage/try names
    from colliding while preserving duplicate submission of the same order.
    """
    explicit = order.get("request_id")
    if explicit is not None:
        value = _require_string(explicit, "request_id")
        if REQUEST_ID_PATTERN.fullmatch(value) is None:
            raise LifecycleError("request_id must contain only letters, digits, '.', '_', ':', or '-'")
        return value
    order_id = _require_string(order.get("order_id"), "order_id")
    result_path = _require_string(order.get("result_path"), "result_path")
    digest = hashlib.sha256(f"{order_id}\0{Path(result_path).expanduser().absolute()}".encode()).hexdigest()
    return f"req-{digest[:32]}"


def requester_address(order: dict) -> RequesterAddress:
    raw = order.get("requester")
    if raw is None:
        pane = _require_string(order.get("cockpit_pane"), "cockpit_pane")
        return RequesterAddress(requester_id=f"pane:{pane}", kind="herdr-agent", address=pane)
    if not isinstance(raw, dict):
        raise LifecycleError("requester must be an object")
    requester_id = _require_string(raw.get("id"), "requester.id")
    kind = _require_string(raw.get("kind"), "requester.kind")
    address = _require_string(raw.get("address"), "requester.address")
    if kind not in REQUESTER_KINDS:
        raise LifecycleError(f"requester.kind must be one of {', '.join(REQUESTER_KINDS)}")
    return RequesterAddress(requester_id=requester_id, kind=kind, address=address)


def _json_object(text: str, where: str) -> dict:
    try:
        value = json.loads(text)
    except json.JSONDecodeError as error:
        raise LifecycleError(f"{where} is not valid JSON: {error}") from error
    if not isinstance(value, dict):
        raise LifecycleError(f"{where} must be a JSON object")
    return value


def parse_manager_decision(text: str, request_id: str, generation: int) -> ManagerDecision:
    value = _json_object(text, "manager decision")
    if value.get("request_id") != request_id:
        raise LifecycleError("manager decision request_id does not match the current request")
    current_generation = _require_generation(value.get("generation"), generation)
    decision = _require_string(value.get("decision"), "manager decision")
    if decision not in MANAGER_DECISIONS:
        raise LifecycleError(f"manager decision must be one of {', '.join(MANAGER_DECISIONS)}")
    message = _require_string(value.get("message"), "manager message")
    requested_changes = value.get("requested_changes", [])
    if not isinstance(requested_changes, list) or not all(isinstance(item, str) and item.strip() for item in requested_changes):
        raise LifecycleError("requested_changes must be a list of non-empty strings")
    return ManagerDecision(request_id, current_generation, decision, message, tuple(requested_changes))


def parse_check_assessment(text: str, request_id: str, generation: int) -> CheckAssessment:
    value = _json_object(text, "check assessment")
    if value.get("request_id") != request_id:
        raise LifecycleError("check assessment request_id does not match the current request")
    current_generation = _require_generation(value.get("generation"), generation)
    assessment = _require_string(value.get("assessment"), "assessment")
    if assessment not in CHECK_ASSESSMENTS:
        raise LifecycleError(f"assessment must be one of {', '.join(CHECK_ASSESSMENTS)}")
    confidence = value.get("confidence")
    if isinstance(confidence, bool) or not isinstance(confidence, (int, float)) or not 0 <= confidence <= 1:
        raise LifecycleError("confidence must be a number from 0 through 1")
    evidence = value.get("evidence")
    if not isinstance(evidence, list) or not all(isinstance(item, str) and item for item in evidence):
        raise LifecycleError("evidence must be a list of non-empty strings")
    recommended_action = _require_string(value.get("recommended_action"), "recommended_action")
    if recommended_action not in RECOMMENDED_ACTIONS:
        raise LifecycleError(f"recommended_action must be one of {', '.join(RECOMMENDED_ACTIONS)}")
    message = _require_string(value.get("message"), "assessment message")
    return CheckAssessment(
        request_id,
        current_generation,
        assessment,
        float(confidence),
        tuple(evidence),
        recommended_action,
        message,
    )


def parse_requester_decision(text: str, request_id: str, generation: int) -> RequesterDecision:
    """Validate one consequential action from the request's recorded owner."""
    value = _json_object(text, "requester decision")
    if value.get("request_id") != request_id:
        raise LifecycleError("requester decision request_id does not match the current request")
    current_generation = _require_generation(value.get("generation"), generation)
    action = _require_string(value.get("action"), "requester action")
    if action not in REQUESTER_ACTIONS:
        raise LifecycleError(f"requester action must be one of {', '.join(REQUESTER_ACTIONS)}")
    extension_ms = value.get("extension_ms")
    if action == "extend":
        if (
            isinstance(extension_ms, bool)
            or not isinstance(extension_ms, int)
            or extension_ms <= 0
            or extension_ms > MAX_EXTENSION_MS
        ):
            raise LifecycleError(
                f"extension_ms must be a positive integer no greater than {MAX_EXTENSION_MS}"
            )
    elif extension_ms is not None:
        raise LifecycleError("extension_ms is allowed only for the extend action")
    message = _require_string(value.get("message"), "requester message")
    return RequesterDecision(request_id, current_generation, action, extension_ms, message)


def write_json_atomic(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        json.dump(value, stream, indent=2, sort_keys=True)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)
    directory_fd = os.open(path.parent, os.O_DIRECTORY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


@contextmanager
def _locked(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+", encoding="utf-8") as stream:
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


class RequestMailbox:
    """Immutable, ordered requester messages with an atomic publication seam."""

    def __init__(self, root: str | Path):
        self.root = Path(root)
        self.messages = self.root / "messages"
        self.lock = self.root / ".publish.lock"

    def publish(
        self,
        request_id: str,
        generation: int,
        message_type: str,
        message: str,
        detail: dict | None = None,
    ) -> Path:
        if not REQUEST_ID_PATTERN.fullmatch(request_id):
            raise LifecycleError("mailbox request_id is invalid")
        if isinstance(generation, bool) or not isinstance(generation, int) or generation <= 0:
            raise LifecycleError("mailbox generation must be a positive integer")
        _require_string(message_type, "message type")
        _require_string(message, "message")
        payload = {
            "at_ns": time.time_ns(),
            "detail": detail or {},
            "generation": generation,
            "message": message,
            "request_id": request_id,
            "type": message_type,
        }
        with _locked(self.lock):
            self.messages.mkdir(parents=True, exist_ok=True)
            used = [int(path.name.split("-", 1)[0]) for path in self.messages.glob("[0-9]*-*.json")]
            sequence = max(used, default=0) + 1
            destination = self.messages / f"{sequence:06d}-{message_type}.json"
            write_json_atomic(destination, payload)
            return destination

    def read_all(self) -> list[dict]:
        values = []
        for path in sorted(self.messages.glob("*.json")) if self.messages.is_dir() else []:
            try:
                value = json.loads(path.read_text())
            except (OSError, json.JSONDecodeError) as error:
                raise LifecycleError(f"mailbox message {path} is unreadable: {error}") from error
            if not isinstance(value, dict):
                raise LifecycleError(f"mailbox message {path} is not an object")
            values.append(value)
        return values
