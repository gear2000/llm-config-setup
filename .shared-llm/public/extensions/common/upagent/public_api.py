#!/usr/bin/env python3
"""Strict server-side implementation of the public ``just upagent`` façade."""

from __future__ import annotations

import fcntl
import hashlib
import hmac
import importlib.util
import json
import os
import re
import shutil
import stat
import sys
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

HERE = Path(__file__).resolve().parent


def _module(name: str, filename: str) -> Any:
    spec = importlib.util.spec_from_file_location(name, HERE / filename)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"could not load {filename}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


contract = _module("upagent_public_contract", "public_contract.py")
offerings = _module("upagent_offerings", "offerings.py")
command_runtime = sys.modules.get("upagent_command_runtime") or _module(
    "upagent_command_runtime", "command_runtime.py"
)
recruiter: Any = None


def _bind_recruiter_runtime(runtime: Any) -> None:
    """Accept the Hub's one canonical Recruiter module; never load a target-local copy."""
    global recruiter
    if recruiter is not None and recruiter is not runtime:
        raise RuntimeError("public API Recruiter runtime is already bound")
    recruiter = runtime


SCHEMA_VERSION = 1
REQUEST_KEYS = {
    "schema_version",
    "request_id",
    "type",
    "offering",
    "effort",
    "agent",
    "specialist",
    "prompt_file",
    "cwd",
}
REQUEST_ID_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[89ab0-9a-f][0-9a-f]{3}-[0-9a-f]{12}$"
)
REQUEST_ID_ULID_RE = re.compile(r"^[0-7][0-9A-HJKMNP-TV-Z]{25}$")
PERSONA_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
TERMINAL_STATES = {"finished", "cleanup-failed"}
SUBMISSION_STATES = frozenset(("registered", "submitting", "submitted"))


class PublicError(RuntimeError):
    """A closed-schema request or public operation is invalid."""


@dataclass(frozen=True)
class ValidatedRequest:
    request_id: str
    payload: dict[str, object]
    payload_sha256: str
    prompt_bytes: bytes
    specialist_entry: dict[str, object] | None


@dataclass(frozen=True)
class RegisteredRequest:
    request_id: str
    request_dir: Path
    order_path: Path
    submission_path: Path
    created: bool
    record: dict[str, object]

    @property
    def pruned(self) -> bool:
        return self.record.get("pruned") is True


def _canonical_hash(value: dict[str, object]) -> str:
    raw = json.dumps(value, separators=(",", ":"), sort_keys=True).encode()
    return hashlib.sha256(raw).hexdigest()


def _payload_hash(record: dict[str, object], request_id: str) -> str:
    value = record.get("payload_sha256")
    if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None:
        raise PublicError(f"request {request_id} has an invalid immutable payload hash")
    return value


def _canonical_request_id(value: object | None) -> str:
    if value is None:
        return str(uuid.uuid4())
    if not isinstance(value, str) or not value:
        raise PublicError("request_id must be a canonical UUID or ULID string")
    if REQUEST_ID_UUID_RE.fullmatch(value) or REQUEST_ID_ULID_RE.fullmatch(value):
        return value
    raise PublicError(
        "request_id must be a canonical UUID or ULID: lowercase hyphenated UUID or "
        "uppercase Crockford ULID"
    )


def _absolute_readable_file(value: object, field: str) -> tuple[Path, bytes]:
    if not isinstance(value, str) or not value:
        raise PublicError(f"{field} must be a non-empty absolute path")
    path = Path(value)
    if not path.is_absolute():
        raise PublicError(f"{field} must be absolute: {path}")
    try:
        metadata = path.stat()
        content = path.read_bytes()
    except OSError as error:
        raise PublicError(f"{field} is not readable: {path}: {error}") from error
    if not stat.S_ISREG(metadata.st_mode):
        raise PublicError(f"{field} must name a regular file: {path}")
    try:
        content.decode("utf-8")
    except UnicodeDecodeError as error:
        raise PublicError(
            f"{field} must contain UTF-8 text: {path}: {error}"
        ) from error
    return path.resolve(), content


def _private_control_token_file(value: object) -> str:
    if not isinstance(value, str) or not value:
        raise PublicError("--control-token-file must be a non-empty absolute path")
    path = Path(value)
    if not path.is_absolute():
        raise PublicError(f"--control-token-file must be absolute: {path}")
    flags = (
        os.O_RDONLY
        | os.O_NONBLOCK
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise PublicError(
            f"--control-token-file is not a readable non-symlink file: {path}: {error}"
        ) from error
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise PublicError(
                f"--control-token-file must name a non-symlink regular file: {path}"
            )
        if metadata.st_uid != os.geteuid():
            raise PublicError(
                f"--control-token-file must be owned by the current user: {path}"
            )
        if stat.S_IMODE(metadata.st_mode) & 0o077:
            raise PublicError(
                f"--control-token-file must not be group/world accessible: {path}"
            )
        if metadata.st_size <= 0 or metadata.st_size > 256:
            raise PublicError(f"--control-token-file has an invalid size: {path}")
        raw = os.read(descriptor, 257)
    finally:
        os.close(descriptor)
    try:
        token = raw.decode("utf-8").strip()
    except UnicodeDecodeError as error:
        raise PublicError("--control-token-file must contain UTF-8 text") from error
    if re.fullmatch(r"[0-9a-f]{32}", token) is None:
        raise PublicError("--control-token-file does not contain a valid control token")
    return token


def _absolute_directory(value: object | None, caller_cwd: Path) -> Path:
    if value is None:
        return caller_cwd
    if not isinstance(value, str) or not value:
        raise PublicError("cwd must be a non-empty absolute path when present")
    path = Path(value)
    if not path.is_absolute() or not path.is_dir():
        raise PublicError(f"cwd must be an existing absolute directory: {path}")
    return path.resolve()


def _known_personas(
    cwd: Path, specialist_index: dict[str, dict[str, object]]
) -> set[str]:
    names = {
        str(entry["agent"])
        for entry in specialist_index.values()
        if isinstance(entry.get("agent"), str)
    }
    roots = [cwd, *cwd.parents]
    repo = next((root for root in roots if (root / ".git").exists()), cwd)
    for directory in (
        repo / ".claude/agents",
        repo / ".agents/agents",
        repo / ".shared-llm/public/compose/agents",
        Path.home() / ".claude/agents",
    ):
        if directory.is_dir():
            names.update(path.stem for path in directory.glob("*.md"))
            names.update(path.stem for path in directory.glob("*.yaml"))
    return names


def _load_json_request(path_value: str) -> dict[str, object]:
    path, raw = _absolute_readable_file(path_value, "--file")
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as error:
        raise PublicError(f"request file {path} is not valid JSON: {error}") from error
    if not isinstance(value, dict):
        raise PublicError(f"request file {path} must contain exactly one JSON object")
    return cast(dict[str, object], value)


def _inline_request(args: Any) -> dict[str, object]:
    value: dict[str, object] = {"schema_version": SCHEMA_VERSION}
    for field in (
        "request_id",
        "type",
        "offering",
        "effort",
        "agent",
        "specialist",
        "prompt_file",
        "cwd",
    ):
        item = getattr(args, field)
        if item is not None:
            value[field] = item
    return value


def validate_request(args: Any, caller_cwd: Path) -> ValidatedRequest:
    raw = (
        _load_json_request(args.file)
        if args.file is not None
        else _inline_request(args)
    )
    unknown = sorted(set(raw) - REQUEST_KEYS)
    if unknown:
        raise PublicError("request has unknown keys: " + ", ".join(unknown))
    if raw.get("schema_version") != SCHEMA_VERSION or isinstance(
        raw.get("schema_version"), bool
    ):
        raise PublicError("request schema_version must be integer 1")
    for key, value in raw.items():
        if key != "schema_version" and (not isinstance(value, str) or not value):
            raise PublicError(f"request {key} must be a non-empty string")
    request_type = raw.get("type")
    if request_type not in ("worker", "specialist"):
        raise PublicError("request type must be worker or specialist")
    prompt_path, prompt_bytes = _absolute_readable_file(
        raw.get("prompt_file"), "prompt_file"
    )
    cwd = _absolute_directory(raw.get("cwd"), caller_cwd)
    request_id = _canonical_request_id(raw.get("request_id"))

    roster = offerings.load_roster()
    try:
        specialist_roster = recruiter.load_specialist_roster()
        specialist_index = recruiter._specialist_index(specialist_roster)
    except recruiter.RecruiterError as error:
        raise PublicError(f"specialist roster is invalid: {error}") from error
    known_personas = _known_personas(cwd, specialist_index)
    specialist_entry: dict[str, object] | None = None

    if request_type == "worker":
        required = ("offering", "effort", "agent")
        missing = [field for field in required if field not in raw]
        prohibited = [field for field in ("specialist",) if field in raw]
        if missing or prohibited:
            detail = []
            if missing:
                detail.append("missing " + ", ".join(missing))
            if prohibited:
                detail.append("incompatible " + ", ".join(prohibited))
            raise PublicError("worker request is invalid: " + "; ".join(detail))
        agent = cast(str, raw["agent"])
        if PERSONA_RE.fullmatch(agent) is None or agent not in known_personas:
            raise PublicError(
                f"unknown worker persona {agent!r}; expected one of {', '.join(sorted(known_personas))}"
            )
        offering_id = cast(str, raw["offering"])
        effort = cast(str, raw["effort"])
        try:
            snapshot = roster.resolve(offering_id, effort)
        except offerings.OfferingError as error:
            raise PublicError(str(error)) from error
        specialist_name: str | None = None
    else:
        missing = [field for field in ("specialist",) if field not in raw]
        prohibited = [
            field for field in ("offering", "effort", "agent") if field in raw
        ]
        if missing or prohibited:
            detail = []
            if missing:
                detail.append("missing " + ", ".join(missing))
            if prohibited:
                detail.append("incompatible " + ", ".join(prohibited))
            raise PublicError("specialist request is invalid: " + "; ".join(detail))
        specialist_name = cast(str, raw["specialist"])
        specialist_entry = specialist_index.get(specialist_name)
        if specialist_entry is None:
            raise PublicError(
                f"unknown specialist {specialist_name!r}; expected one of {', '.join(specialist_index)}"
            )
        offering_id = cast(str, specialist_entry["offering"])
        effort = cast(str, specialist_entry["effort"])
        agent = cast(str, specialist_entry["agent"])
        try:
            snapshot = roster.resolve(offering_id, effort)
        except offerings.OfferingError as error:
            raise PublicError(
                f"specialist {specialist_name!r} has invalid offering: {error}"
            ) from error

    prompt_sha256 = hashlib.sha256(prompt_bytes).hexdigest()
    payload: dict[str, object] = {
        "schema_version": SCHEMA_VERSION,
        "type": request_type,
        "offering": offering_id,
        "effort": effort,
        "agent": agent,
        "prompt_file": str(prompt_path),
        "prompt_sha256": prompt_sha256,
        "cwd": str(cwd),
        "offering_snapshot": snapshot,
    }
    if specialist_name is not None:
        payload["specialist"] = specialist_name
    return ValidatedRequest(
        request_id=request_id,
        payload=payload,
        payload_sha256=_canonical_hash(payload),
        prompt_bytes=prompt_bytes,
        specialist_entry=specialist_entry,
    )


def _write_json(path: Path, value: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as stream:
        json.dump(value, stream, indent=2, sort_keys=True)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())


def _write_json_atomic(path: Path, value: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    _write_json(temporary, value)
    os.replace(temporary, path)
    directory_fd = os.open(path.parent, os.O_DIRECTORY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


class PublicRequestStore:
    def __init__(self, root: Path | None = None):
        ledger_root = Path(
            root
            or (recruiter.JobLedger().root if recruiter is not None else None)
            or command_runtime.getenv("UPAGENT_HUB_DIR", str(HERE / ".upagent-ledger"))
        )
        self.root = ledger_root.expanduser().resolve() / "public"
        self.requests = self.root / "requests"
        self.locks = self.root / "locks"

    def path(self, request_id: str) -> Path:
        return self.requests / request_id

    @contextmanager
    def request_lock(self, request_id: str) -> Iterator[None]:
        canonical = _canonical_request_id(request_id)
        self.locks.mkdir(parents=True, exist_ok=True)
        lock_path = self.locks / f"{canonical}.lock"
        with lock_path.open("a+", encoding="utf-8") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

    def load(self, request_id: str) -> RegisteredRequest:
        canonical = _canonical_request_id(request_id)
        directory = self.path(canonical)
        tombstone_path = directory / "tombstone.json"
        source = (
            tombstone_path if tombstone_path.is_file() else directory / "request.json"
        )
        try:
            record = json.loads(source.read_text())
        except (OSError, json.JSONDecodeError) as error:
            raise PublicError(
                f"unknown or unreadable request {canonical}: {error}"
            ) from error
        if not isinstance(record, dict) or record.get("request_id") != canonical:
            raise PublicError(f"public request record {directory} has invalid identity")
        if source == tombstone_path:
            record = {**record, "pruned": True}
        return RegisteredRequest(
            canonical,
            directory,
            directory / "order.json",
            directory / "submission.json",
            False,
            record,
        )

    def submission(self, registered: RegisteredRequest) -> dict[str, object]:
        if registered.pruned:
            value = registered.record.get("submission")
            if isinstance(value, dict):
                return cast(dict[str, object], value)
            raise PublicError(
                f"pruned request {registered.request_id} has no retained submission state"
            )
        try:
            value = json.loads(registered.submission_path.read_text())
        except (OSError, json.JSONDecodeError) as error:
            raise PublicError(
                f"public submission state for {registered.request_id} is unreadable: {error}"
            ) from error
        state = value.get("state") if isinstance(value, dict) else None
        if state not in SUBMISSION_STATES:
            raise PublicError(
                f"public submission state for {registered.request_id} is invalid"
            )
        return cast(dict[str, object], value)

    def transition_submission(
        self, registered: RegisteredRequest, state: str
    ) -> dict[str, object]:
        if state not in SUBMISSION_STATES:
            raise PublicError(f"invalid public submission transition target: {state}")
        current = self.submission(registered)
        current_state = cast(str, current["state"])
        allowed = {
            "registered": {"submitting"},
            "submitting": {"submitting", "submitted"},
            "submitted": set(),
        }
        if state not in allowed[current_state]:
            raise PublicError(
                f"invalid public submission transition for {registered.request_id}: "
                f"{current_state} -> {state}"
            )
        updated: dict[str, object] = {
            **current,
            "state": state,
            "updated_at_ns": time.time_ns(),
        }
        if state == "submitting":
            attempts = current.get("attempts", 0)
            if isinstance(attempts, bool) or not isinstance(attempts, int):
                raise PublicError(
                    f"public submission attempts for {registered.request_id} is invalid"
                )
            updated["attempts"] = attempts + 1
        _write_json_atomic(registered.submission_path, updated)
        return updated

    def register(
        self, request: ValidatedRequest, cockpit_pane: str
    ) -> RegisteredRequest:
        with self.request_lock(request.request_id):
            directory = self.path(request.request_id)
            if directory.exists():
                existing = self.load(request.request_id)
                if existing.record.get("payload_sha256") != request.payload_sha256:
                    raise PublicError(
                        f"request_id_conflict: {request.request_id} already names a different canonical payload"
                    )
                return existing
            temporary = self.requests / f".{request.request_id}.{uuid.uuid4().hex}.tmp"
            temporary.mkdir(parents=True)
            final_prompt = directory / "prompt.md"
            final_instructions = directory / "instructions.md"
            final_result = directory / "result.json"
            final_compacted = directory / "compacted.md"
            final_handoff = directory / "handoff.md"
            final_answer = directory / "answer.json"
            temporary_prompt = temporary / "prompt.md"
            temporary_prompt.write_bytes(request.prompt_bytes)
            os.chmod(temporary_prompt, 0o600)
            payload = request.payload
            if payload["type"] == "specialist":
                assert request.specialist_entry is not None
                question = request.prompt_bytes.decode("utf-8")
                location = str(
                    request.specialist_entry.get("location") or "(no definition file)"
                )
                consult = {
                    "consult_id": request.request_id,
                    "specialist": payload["specialist"],
                    "question": question,
                    "answer_path": str(final_answer),
                }
                instructions = recruiter.build_consult_brief(
                    consult, location, cast(str, payload["cwd"])
                )
                (temporary / "instructions.md").write_text(instructions)
                instructions_path = final_instructions
            else:
                instructions_path = final_prompt
            order: dict[str, object] = {
                "order_id": f"public-{request.request_id}",
                "request_id": request.request_id,
                "phase_id": "public",
                "stage_id": "stage-5-finalization",
                "harness": cast(dict[str, object], payload["offering_snapshot"])[
                    "harness"
                ],
                "model": cast(dict[str, object], payload["offering_snapshot"])["model"],
                "agent": payload["agent"],
                "effort": payload["effort"],
                "cwd": payload["cwd"],
                "instructions_path": str(instructions_path),
                "result_path": str(final_result),
                "cockpit_pane": cockpit_pane,
                "offering_snapshot": payload["offering_snapshot"],
                "management": {"mode": "dedicated"},
                "artifact_publication": {
                    "schema_version": 1,
                    "compacted_path": str(final_compacted),
                    "handoff_path": str(final_handoff),
                    "mandatory_consults": [],
                    **(
                        {
                            "answer_path": str(final_answer),
                            "consult_id": request.request_id,
                            "consult_payload_sha256": request.payload_sha256,
                        }
                        if payload["type"] == "specialist"
                        else {}
                    ),
                },
                "public_request": {
                    "payload_sha256": request.payload_sha256,
                    "prompt_sha256": payload["prompt_sha256"],
                    "prompt_snapshot": str(final_prompt),
                    "type": payload["type"],
                    **(
                        {"answer_path": str(final_answer)}
                        if payload["type"] == "specialist"
                        else {}
                    ),
                },
            }
            recruiter.contracts.parse_order(json.dumps(order))
            _write_json(temporary / "order.json", order)
            record: dict[str, object] = {
                "schema_version": SCHEMA_VERSION,
                "request_id": request.request_id,
                "payload": payload,
                "payload_sha256": request.payload_sha256,
                "prompt_snapshot": str(final_prompt),
                "order_path": str(directory / "order.json"),
                "offering_snapshot": payload["offering_snapshot"],
                "created_at_ns": time.time_ns(),
            }
            _write_json(temporary / "request.json", record)
            _write_json(
                temporary / "submission.json",
                {
                    "attempts": 0,
                    "registered_at_ns": time.time_ns(),
                    "state": "registered",
                    "updated_at_ns": time.time_ns(),
                },
            )
            self.requests.mkdir(parents=True, exist_ok=True)
            try:
                os.replace(temporary, directory)
            except OSError as error:
                shutil.rmtree(temporary, ignore_errors=True)
                if directory.exists():
                    existing = self.load(request.request_id)
                    if existing.record.get("payload_sha256") != request.payload_sha256:
                        raise PublicError(
                            f"request_id_conflict: {request.request_id} already names a different canonical payload"
                        ) from error
                    return existing
                raise
            directory_fd = os.open(self.requests, os.O_DIRECTORY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
            return RegisteredRequest(
                request.request_id,
                directory,
                directory / "order.json",
                directory / "submission.json",
                True,
                record,
            )

    def request_ids(self) -> list[str]:
        if not self.requests.is_dir():
            return []
        return sorted(
            path.name
            for path in self.requests.iterdir()
            if path.is_dir() and not path.name.startswith(".")
        )

    def prune(
        self, registered: RegisteredRequest, tombstone: dict[str, object]
    ) -> RegisteredRequest:
        """Commit one atomic tombstone, then prune only its Hub-owned siblings."""
        request_dir = registered.request_dir
        if not registered.pruned:
            _write_json_atomic(request_dir / "tombstone.json", tombstone)
        for child in request_dir.iterdir():
            if child.name == "tombstone.json":
                continue
            if child.is_symlink() or child.is_file():
                child.unlink()
            else:
                shutil.rmtree(child)
        directory_fd = os.open(request_dir, os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
        garbage = self.requests / f".{registered.request_id}.cleanup-old"
        if garbage.exists():
            shutil.rmtree(garbage)
        return self.load(registered.request_id)


def _public_lifecycle_roster_path() -> str:
    """Return the code-owned public roster, never a legacy configurable roster."""
    return str(HERE / "offerings.yaml")


def _cockpit_pane() -> str:
    pane = recruiter._recruiter_pane_from_state()
    if not pane:
        raise PublicError("UpAgent services are not ready; run `just upagent up` first")
    return pane


def _capture(call: Any, *args: object, **kwargs: object) -> tuple[int, str]:
    with command_runtime.capture_output() as (stdout, stderr):
        code = call(*args, **kwargs)
    if stderr.getvalue():
        command_runtime.write_stderr(stderr.getvalue())
    return int(code), stdout.getvalue()


def _public_state(
    value: dict[str, object], *, include_requester_control_token: bool = False
) -> dict[str, object]:
    """Expose no mutation credential except in the originating request response."""
    hidden = {"token", "lease_token"}
    if not include_requester_control_token:
        hidden.add("requester_control_token")
    return {key: item for key, item in value.items() if key not in hidden}


def _public_receipt(value: dict[str, object] | None) -> dict[str, object] | None:
    if value is None:
        return None
    sanitized = {
        key: item
        for key, item in value.items()
        if key not in ("artifact_manifest_path", "published_result_path")
    }
    artifacts = value.get("artifacts")
    if isinstance(artifacts, list):
        sanitized["artifacts"] = [
            {
                key: item
                for key, item in artifact.items()
                if key not in ("staging_path", "lease_token")
            }
            for artifact in artifacts
            if isinstance(artifact, dict)
        ]
    return sanitized


def _read_json_optional(path: Path, label: str) -> dict[str, object] | None:
    if not path.is_file():
        return None
    try:
        value = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as error:
        raise PublicError(f"{label} is unreadable: {error}") from error
    if not isinstance(value, dict):
        raise PublicError(f"{label} is not an object")
    return cast(dict[str, object], value)


def _tombstone_status(tombstone: dict[str, object]) -> dict[str, object]:
    artifacts = tombstone.get("artifacts")
    submission = tombstone.get("submission")
    receipt = tombstone.get("receipt")
    result = tombstone.get("result")
    request_id = tombstone.get("request_id")
    terminal_state = tombstone.get("terminal_state")
    terminal_verdict = tombstone.get("terminal_verdict")
    terminal_at_ns = tombstone.get("terminal_at_ns")
    cleanup_at_ns = tombstone.get("cleanup_at_ns")
    if (
        not isinstance(request_id, str)
        or not isinstance(artifacts, list)
        or not isinstance(submission, dict)
        or not isinstance(receipt, dict)
        or not isinstance(result, dict)
        or not isinstance(terminal_state, str)
        or not isinstance(terminal_verdict, str)
        or isinstance(terminal_at_ns, bool)
        or not isinstance(terminal_at_ns, int)
        or isinstance(cleanup_at_ns, bool)
        or not isinstance(cleanup_at_ns, int)
    ):
        raise PublicError("pruned request tombstone lacks retained terminal evidence")
    payload_sha256 = _payload_hash(tombstone, request_id)
    return {
        "request_id": request_id,
        "submission": submission,
        "payload": None,
        "payload_pruned": True,
        "payload_sha256": payload_sha256,
        "offering_snapshot": tombstone.get("offering_snapshot"),
        "order_path": None,
        "artifacts": artifacts,
        "state": {
            "state": "pruned",
            "terminal_state": terminal_state,
            "terminal_verdict": terminal_verdict,
            "terminal_at_ns": terminal_at_ns,
            "cleanup_at_ns": cleanup_at_ns,
        },
        "receipt": receipt,
        "result": result,
        "pruned": True,
    }


def _public_status(
    store: PublicRequestStore,
    registered: RegisteredRequest,
    *,
    include_requester_control_token: bool = False,
) -> dict[str, object]:
    if registered.pruned:
        return _tombstone_status(registered.record)
    order = recruiter.load_order(registered.order_path)
    ledger = recruiter.JobLedger()
    key = ledger.key_for_order(order)
    ledger_request = ledger.request_dir(key)
    ledger_tombstone = _read_json_optional(
        ledger_request / "tombstone.json", "Recruiter tombstone"
    )
    if ledger_tombstone is not None:
        return _tombstone_status(ledger_tombstone)
    receipt_path = ledger_request / "receipt.json"
    result_path = ledger.published_result_path(key)
    submission = store.submission(registered)
    if not ledger_request.is_dir():
        state: dict[str, object] = {"state": submission["state"]}
        receipt = None
        result = None
    else:
        state = ledger.state(key)
        receipt = _public_receipt(_read_json_optional(receipt_path, "terminal receipt"))
        result = _read_json_optional(result_path, "published result")
    public_evidence = cast(dict[str, object], order["public_request"])
    publication = cast(dict[str, object], order["artifact_publication"])
    artifacts: list[dict[str, object]] = [
        {"kind": "prompt", "path": registered.record["prompt_snapshot"]},
        {"kind": "order", "path": str(registered.order_path)},
        {"kind": "result", "path": order["result_path"]},
        {"kind": "compacted", "path": publication["compacted_path"]},
        {"kind": "handoff", "path": publication["handoff_path"]},
        {"kind": "receipt", "path": str(receipt_path)},
    ]
    if isinstance(result, dict) and isinstance(result.get("full_log"), str):
        full_log = cast(str, result["full_log"])
        artifacts.append(
            {
                "kind": "log",
                **(
                    {"path": full_log}
                    if Path(full_log).is_absolute()
                    else {"value": full_log}
                ),
            }
        )
    if isinstance(public_evidence.get("answer_path"), str):
        artifacts.append({"kind": "answer", "path": public_evidence["answer_path"]})
    return {
        "request_id": registered.request_id,
        "submission": submission,
        "payload": registered.record["payload"],
        "payload_sha256": registered.record["payload_sha256"],
        "offering_snapshot": registered.record["offering_snapshot"],
        "order_path": str(registered.order_path),
        "artifacts": artifacts,
        "state": _public_state(
            state,
            include_requester_control_token=include_requester_control_token,
        ),
        "receipt": receipt,
        "result": result,
        "pruned": False,
    }


def _emit(
    value: dict[str, object] | list[dict[str, object]], as_json: bool, human: str
) -> None:
    print(json.dumps(value, indent=2, sort_keys=True) if as_json else human)


def _identity() -> dict[str, object]:
    socket_path = Path(command_runtime.getenv("UPAGENT_SOCKET", "") or "")
    identity_path = socket_path.with_name(f"{socket_path.name}.identity.json")
    try:
        identity = json.loads(identity_path.read_text())
    except (OSError, json.JSONDecodeError) as error:
        raise PublicError(f"Hub identity is unreadable: {error}") from error
    if not isinstance(identity, dict):
        raise PublicError("Hub identity must be an object")
    identity["services_state_file"] = str(recruiter.STATE_FILE)
    identity["services_ready"] = _cockpit_pane_or_none() is not None
    return identity


def _cockpit_pane_or_none() -> str | None:
    return recruiter._recruiter_pane_from_state()


def _list_workers(status_filter: str) -> list[dict[str, object]]:
    ledger = recruiter.JobLedger()
    rows: list[dict[str, object]] = []
    if not ledger.requests.is_dir():
        return rows
    for request_dir in sorted(ledger.requests.iterdir()):
        tombstone_path = request_dir / "tombstone.json"
        if tombstone_path.is_file():
            tombstone = _read_json_optional(tombstone_path, "request tombstone")
            if tombstone is None:
                raise PublicError(f"request tombstone {tombstone_path} disappeared")
            if status_filter == "active":
                continue
            rows.append(
                {
                    "request_id": tombstone.get("request_id"),
                    "order_id": tombstone.get("order_id"),
                    "state": "pruned",
                    "terminal_verdict": tombstone.get("terminal_verdict"),
                    "worker_address": None,
                    "worker_pane": None,
                    "manager_address": None,
                }
            )
            continue
        state_path = request_dir / "state/latest.json"
        order_path = request_dir / "request.json"
        if not state_path.is_file() or not order_path.is_file():
            continue
        state = json.loads(state_path.read_text())
        order = json.loads(order_path.read_text())
        if not isinstance(state, dict) or not isinstance(order, dict):
            raise PublicError(f"worker ledger entry {request_dir} is malformed")
        terminal = state.get("state") in TERMINAL_STATES
        if status_filter == "active" and terminal:
            continue
        if status_filter == "terminal" and not terminal:
            continue
        rows.append(
            {
                "request_id": recruiter.lifecycle.request_identity(order),
                "order_id": order.get("order_id"),
                "state": state.get("state"),
                "worker_address": state.get("worker_address"),
                "worker_pane": state.get("worker_pane"),
                "manager_address": state.get("manager_address"),
            }
        )
    return rows


def _cleanup_tombstone(
    registered: RegisteredRequest,
    status: dict[str, object],
    evidence: dict[str, object],
    cleanup_at_ns: int,
) -> dict[str, object]:
    retained: list[dict[str, object]] = []
    artifacts = status.get("artifacts")
    if not isinstance(artifacts, list):
        raise PublicError("terminal request has invalid artifact pointers")
    tombstone_path = registered.request_dir / "tombstone.json"
    for artifact in artifacts:
        if not isinstance(artifact, dict):
            raise PublicError("terminal request has malformed artifact pointers")
        kind = artifact.get("kind")
        if kind == "receipt":
            retained.append(
                {
                    "kind": "receipt",
                    "path": str(tombstone_path),
                    "embedded_field": "receipt",
                }
            )
            continue
        artifact_path = artifact.get("path")
        hub_owned = (
            isinstance(artifact_path, str)
            and Path(artifact_path).is_absolute()
            and Path(artifact_path).is_relative_to(registered.request_dir)
        )
        if kind in ("prompt", "order") or hub_owned:
            retained.append(
                {
                    "kind": kind,
                    "pruned": True,
                    **(
                        {"previous_path": artifact_path}
                        if isinstance(artifact_path, str)
                        else {}
                    ),
                }
            )
        else:
            retained.append(cast(dict[str, object], artifact))
    raw_receipt = evidence.get("receipt")
    receipt = status.get("receipt")
    result = evidence.get("result")
    control = evidence.get("control")
    terminal_at_ns = evidence.get("terminal_at_ns")
    if (
        not isinstance(raw_receipt, dict)
        or not isinstance(receipt, dict)
        or not isinstance(result, dict)
        or not isinstance(control, dict)
        or isinstance(terminal_at_ns, bool)
        or not isinstance(terminal_at_ns, int)
    ):
        raise PublicError("terminal cleanup evidence is incomplete")
    order_id = result.get("order_id")
    return {
        "schema_version": 1,
        "pruned": True,
        "request_id": registered.request_id,
        "order_id": order_id,
        "payload_sha256": registered.record["payload_sha256"],
        "offering_snapshot": registered.record.get("offering_snapshot"),
        "terminal_state": evidence["terminal_state"],
        "terminal_verdict": evidence["terminal_verdict"],
        "terminal_at_ns": terminal_at_ns,
        "cleanup_at_ns": cleanup_at_ns,
        "cancelled": raw_receipt.get("cancelled", False),
        "submission": status["submission"],
        "receipt": receipt,
        "result": result,
        "artifacts": retained,
        "control": control,
    }


def _cleanup_one(
    store: PublicRequestStore,
    request_id: str,
    *,
    older_than_seconds: int,
    apply: bool,
    now_ns: int,
) -> dict[str, object]:
    with store.request_lock(request_id):
        registered = store.load(request_id)
        payload_sha256 = _payload_hash(registered.record, request_id)
        if registered.pruned:
            terminal_at_ns = registered.record.get("terminal_at_ns")
            if isinstance(terminal_at_ns, bool) or not isinstance(terminal_at_ns, int):
                raise PublicError(
                    f"pruned request {request_id} has no terminal timestamp"
                )
            age_ns = max(0, now_ns - terminal_at_ns)
            eligible = age_ns >= older_than_seconds * 1_000_000_000
            if eligible and apply:
                ledger = recruiter.JobLedger()
                ledger.prune_terminal(
                    ledger.key(request_id),
                    request_id,
                    payload_sha256,
                    registered.record,
                    verify_absence=recruiter._verify_terminal_cleanup_absence,
                )
                store.prune(registered, registered.record)
            return {
                "request_id": request_id,
                "status": "already-pruned" if eligible else "skipped",
                "eligible": eligible,
                "age_seconds": age_ns // 1_000_000_000,
                **(
                    {"reason": "terminal age is below the inclusive threshold"}
                    if not eligible
                    else {}
                ),
            }
        try:
            order = recruiter.load_order(registered.order_path)
        except recruiter.ContractError as error:
            raise PublicError(
                f"request {request_id} has a malformed order: {error}"
            ) from error
        ledger = recruiter.JobLedger()
        key = ledger.key_for_order(order)
        evidence = ledger.terminal_cleanup_evidence(
            key,
            request_id,
            payload_sha256,
        )
        terminal_at_ns = evidence.get("terminal_at_ns")
        if isinstance(terminal_at_ns, bool) or not isinstance(terminal_at_ns, int):
            raise PublicError(f"request {request_id} has no terminal timestamp")
        age_ns = max(0, now_ns - terminal_at_ns)
        if age_ns < older_than_seconds * 1_000_000_000:
            return {
                "request_id": request_id,
                "status": "skipped",
                "eligible": False,
                "age_seconds": age_ns // 1_000_000_000,
                "reason": "terminal age is below the inclusive threshold",
            }
        status = _public_status(store, registered)
        tombstone = (
            {key: value for key, value in evidence.items() if key != "already_pruned"}
            if evidence.get("already_pruned") is True
            else _cleanup_tombstone(registered, status, evidence, now_ns)
        )
        if not apply:
            return {
                "request_id": request_id,
                "status": "planned",
                "eligible": True,
                "age_seconds": age_ns // 1_000_000_000,
                "would_prune": [
                    str(registered.request_dir),
                    str(ledger.request_dir(key)),
                ],
                "caller_artifacts_retained": [
                    artifact
                    for artifact in cast(
                        list[dict[str, object]], tombstone["artifacts"]
                    )
                    if artifact.get("pruned") is not True
                ],
            }
        ledger.prune_terminal(
            key,
            request_id,
            payload_sha256,
            tombstone,
            verify_absence=recruiter._verify_terminal_cleanup_absence,
        )
        store.prune(registered, tombstone)
        return {
            "request_id": request_id,
            "status": "cleaned",
            "eligible": True,
            "age_seconds": age_ns // 1_000_000_000,
            "tombstone": str(registered.request_dir / "tombstone.json"),
        }


def _cancel(args: Any, store: PublicRequestStore) -> int:
    control_token = _private_control_token_file(args.control_token_file)
    with store.request_lock(args.request):
        registered = store.load(args.request)
        if registered.pruned:
            control = registered.record.get("control")
            if not isinstance(control, dict) or not isinstance(
                control.get("requester_control_token_sha256"), str
            ):
                raise PublicError("pruned request has no retained control-token proof")
            supplied = hashlib.sha256(control_token.encode()).hexdigest()
            if not hmac.compare_digest(
                cast(str, control["requester_control_token_sha256"]), supplied
            ):
                raise PublicError(
                    "requester control token does not match the current request generation"
                )
            outcome: dict[str, object] = {
                "cancelled": registered.record.get("cancelled", False),
                "terminal": True,
                "already_pruned": True,
            }
        else:
            raw_outcome = recruiter.cmd_cancel(
                str(registered.order_path), control_token
            )
            outcome = {
                "cancelled": raw_outcome.get("cancelled", False),
                "terminal": raw_outcome.get("terminal", False),
            }
        status = _public_status(store, store.load(args.request))
        status["cancellation"] = outcome
    _emit(
        status,
        args.json,
        f"request {args.request}: "
        + ("cancelled" if outcome.get("cancelled") is True else "already terminal"),
    )
    return 0


def _cleanup(args: Any, store: PublicRequestStore) -> int:
    now_ns = time.time_ns()
    if args.request:
        try:
            rows = [
                _cleanup_one(
                    store,
                    args.request,
                    older_than_seconds=args.older_than_seconds,
                    apply=args.apply,
                    now_ns=now_ns,
                )
            ]
        except (OSError, recruiter.RecruiterError) as error:
            raise PublicError(
                f"cleanup refused request {args.request}: {error}"
            ) from error
    else:
        rows = []
        for request_id in store.request_ids():
            try:
                rows.append(
                    _cleanup_one(
                        store,
                        request_id,
                        older_than_seconds=args.older_than_seconds,
                        apply=args.apply,
                        now_ns=now_ns,
                    )
                )
            except (OSError, PublicError, recruiter.RecruiterError) as error:
                rows.append(
                    {
                        "request_id": request_id,
                        "status": "skipped",
                        "eligible": False,
                        "reason": str(error),
                    }
                )
    action = "apply" if args.apply else "dry-run"
    _emit(
        rows,
        args.json,
        f"cleanup {action}: "
        + ", ".join(f"{row['request_id']}={row['status']}" for row in rows),
    )
    return 0


def _submit_registered(
    store: PublicRequestStore, registered: RegisteredRequest, *, wait: bool
) -> tuple[int, bool]:
    """Fence the public-to-Recruiter seam and resume interrupted submissions safely."""
    with store.request_lock(registered.request_id):
        registered = store.load(registered.request_id)
        if registered.pruned:
            result = registered.record.get("result")
            verdict = result.get("verdict") if isinstance(result, dict) else None
            return (0 if verdict == "passed" else 1), False
        order = recruiter.load_order(registered.order_path)
        ledger = recruiter.JobLedger()
        ledger_tombstone = _read_json_optional(
            ledger.request_dir(ledger.key_for_order(order)) / "tombstone.json",
            "Recruiter tombstone",
        )
        if ledger_tombstone is not None:
            verdict = ledger_tombstone.get("terminal_verdict")
            return (0 if verdict == "passed" else 1), False
        submission = store.submission(registered)
        if submission["state"] == "submitted":
            if wait:
                code, _ = _capture(
                    recruiter.cmd_await, str(registered.order_path), 600_000
                )
                return code, False
            return 0, False
        store.transition_submission(registered, "submitting")
        command = (
            recruiter.cmd_dispatch_strict if wait else recruiter.cmd_request_strict
        )
        code, _ = _capture(
            command,
            str(registered.order_path),
            _public_lifecycle_roster_path(),
        )
        store.transition_submission(registered, "submitted")
        return code, True


def _request(args: Any, cwd: Path) -> int:
    validated = validate_request(args, cwd)
    store = PublicRequestStore()
    registered = store.register(validated, _cockpit_pane())
    code, submitted_now = _submit_registered(store, registered, wait=args.wait)
    status = _public_status(
        store,
        registered,
        include_requester_control_token=registered.created and submitted_now,
    )
    status["attached"] = not registered.created or not submitted_now
    _emit(
        status,
        args.json,
        f"request {registered.request_id}: {cast(dict[str, object], status['state']).get('state')}"
        + (" (attached)" if not registered.created else ""),
    )
    return code


def execute(args: Any, cwd: Path) -> int:
    if args.command == "help":
        print(contract.help_text(), end="")
        return 0
    if args.command == "up":
        return recruiter.cmd_up(
            _public_lifecycle_roster_path(),
            separate_workspaces=args.separate_workspaces,
        )
    store = PublicRequestStore()
    if args.command in ("status", "get"):
        if args.command == "get" or args.request:
            status = _public_status(store, store.load(args.request))
            _emit(
                status,
                args.json,
                f"request {args.request}: {cast(dict[str, object], status['state']).get('state')}",
            )
        else:
            identity = _identity()
            human = "\n".join(f"{key}: {value}" for key, value in identity.items())
            _emit(identity, args.json, human)
        return 0
    if args.command == "lists":
        if args.type == "offerings":
            rows = offerings.load_roster().listing()
            human = "\n".join(
                f"{row['id']}  {row['rendered_identity']}  {','.join(cast(list[str], row['efforts']))}"
                for row in rows
            )
        elif args.type == "specialists":
            rows = list(
                recruiter._specialist_index(recruiter.load_specialist_roster()).values()
            )
            human = "\n".join(
                f"{row['name']}  {row['offering']}  {row['effort']}  {row['agent']}"
                for row in rows
            )
        else:
            rows = _list_workers(args.status)
            human = (
                "\n".join(
                    f"{row['request_id']}  {row['state']}  {row.get('worker_address') or '-'}"
                    for row in rows
                )
                or "no workers"
            )
        _emit(rows, args.json, human)
        return 0
    if args.command == "request":
        return _request(args, cwd)
    if args.command == "await":
        registered = store.load(args.request)
        if registered.pruned:
            status = _public_status(store, registered)
            _emit(status, args.json, f"request {args.request}: pruned")
            return 0
        code, _ = _capture(
            recruiter.cmd_await, str(registered.order_path), args.notify_after_ms
        )
        status = _public_status(store, registered)
        _emit(
            status,
            args.json,
            f"request {args.request}: {cast(dict[str, object], status['state']).get('state')}",
        )
        return code
    if args.command == "await-any":
        registered = [store.load(request_id) for request_id in args.request]
        if any(item.pruned for item in registered):
            raise PublicError(
                "await-any cannot watch pruned terminal requests; use get"
            )
        code, output = _capture(
            recruiter.cmd_await_any,
            [str(item.order_path) for item in registered],
            args.timeout_ms,
            args.cursor,
        )
        marker = output.strip().removeprefix("AWAIT_EVENT ")
        event = json.loads(marker)
        _emit(event, args.json, f"{event.get('kind')}: {event.get('summary')}")
        return code
    if args.command == "verify":
        registered = store.load(args.request)
        if registered.pruned:
            raise PublicError("cannot verify a pruned terminal request")
        roster = offerings.load_roster()
        try:
            snapshot = roster.resolve(args.offering, args.effort)
        except offerings.OfferingError as error:
            raise PublicError(str(error)) from error
        specialists = recruiter._specialist_index(recruiter.load_specialist_roster())
        if args.agent not in _known_personas(cwd, specialists):
            raise PublicError(f"unknown verifier persona {args.agent!r}")
        code, output = _capture(
            recruiter.cmd_verify,
            str(registered.order_path),
            _public_lifecycle_roster_path(),
            harness=snapshot["harness"],
            model=snapshot["model"],
            agent=args.agent,
            effort=args.effort,
            offering_snapshot=snapshot,
            wait=args.wait,
        )
        value = {
            "request_id": args.request,
            "verification_started": True,
            "output": output.strip(),
        }
        _emit(value, args.json, f"verification started for {args.request}")
        return code
    if args.command == "respond":
        registered = store.load(args.request)
        if registered.pruned:
            raise PublicError("cannot respond to a pruned terminal request")
        extension = args.extension_ms if args.action == "extend" else None
        code, output = _capture(
            recruiter.cmd_respond,
            str(registered.order_path),
            args.control_token,
            args.nonce,
            args.action,
            extension,
            f"Requester authorized {args.action}.",
        )
        value = json.loads(output)
        _emit(value, args.json, f"response accepted: {args.action}")
        return code
    if args.command == "cancel":
        return _cancel(args, store)
    if args.command == "cleanup":
        return _cleanup(args, store)
    if args.command == "reconcile":
        code, output = _capture(recruiter.cmd_reconcile, force=False)
        value = json.loads(output)
        _emit(value, args.json, f"reconciled {value.get('reconciled', 0)} request(s)")
        return code
    raise PublicError(f"unsupported public command: {args.command}")


def main(argv: list[str] | None = None) -> int:
    if recruiter is None:
        raise SystemExit(
            "upagent: direct Recruiter execution is forbidden; use the UpAgent Hub"
        )
    command = list(sys.argv[1:] if argv is None else argv)
    try:
        args = contract.parse_argv(command)
        if args.command != "help":
            recruiter._require_hub_authority()
        return execute(args, command_runtime.current_cwd())
    except (
        PublicError,
        contract.PublicCommandError,
        offerings.OfferingError,
        recruiter.RecruiterError,
    ) as error:
        raise SystemExit(f"upagent: {error}") from error


if __name__ == "__main__":
    raise SystemExit(main())
