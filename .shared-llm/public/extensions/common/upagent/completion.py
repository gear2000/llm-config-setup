"""Typed completion artifact manifests and fail-loud bundle publication.

Workers stage artifacts only in lease-private paths.  Python validates the bundle, projects
it to caller-visible paths, validates the projection, and only then permits the Recruiter to
publish a receipt or terminal event.

Only `result.json` carries the verdict, so only it (plus `answer.json`, a consult's actual
deliverable) is mandatory.  `compacted.md` and `handoff.md` are post-work summaries with no
schema — any non-empty text passes — and a later reader can reconstruct both from the result.
Losing an entire successful job because a worker forgot to write a summary is a worse outcome
than publishing without it, so an absent or blank optional artifact is skipped, not fatal.
The manifest still declares all of them: the paths stay known whether or not the files land.
"""

from __future__ import annotations

import json
import os
import shutil
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1
# The lifecycle kinds every manifest declares, in this order.  Declaring a kind is not the
# same as requiring the file: see MANDATORY_KINDS.
REQUIRED_KINDS = ("result", "compacted", "handoff")
# The kinds whose files must actually exist and validate before anything is published.
MANDATORY_KINDS = frozenset({"result", "answer"})
MEDIA_TYPES = {
    "result": "application/json",
    "compacted": "text/markdown",
    "handoff": "text/markdown",
    "answer": "application/json",
}


class CompletionError(ValueError):
    """A manifest or artifact bundle is missing, malformed, or cannot be published."""


@dataclass(frozen=True)
class Artifact:
    kind: str
    staging_path: Path
    public_path: Path
    media_type: str
    # An order may promote a normally-optional summary to required through its declared
    # publication contract (`artifact_publication.required_artifacts`). Enforcement follows
    # the declared contract: a review whose whole deliverable is its verdict document
    # declares `compacted` required and can no longer publish "passed" without it, while
    # ordinary workers keep the documented tolerance above.
    declared_required: bool = False

    @property
    def required(self) -> bool:
        """MANDATORY_KINDS are always required; the order's contract may add more."""
        return self.kind in MANDATORY_KINDS or self.declared_required

    def as_dict(self) -> dict[str, object]:
        return {
            "kind": self.kind,
            "media_type": self.media_type,
            "public_path": str(self.public_path),
            "required": self.required,
            "staging_path": str(self.staging_path),
        }


@dataclass(frozen=True)
class Manifest:
    order_id: str
    request_id: str
    lease_token: str
    artifacts: tuple[Artifact, ...]
    consult_id: str | None = None
    mandatory_consults: tuple[dict[str, str], ...] = ()

    def artifact(self, kind: str) -> Artifact:
        matches = [item for item in self.artifacts if item.kind == kind]
        if len(matches) != 1:
            raise CompletionError(
                f"artifact manifest needs exactly one {kind!r} artifact"
            )
        return matches[0]

    def as_dict(self) -> dict[str, object]:
        return {
            "artifacts": [item.as_dict() for item in self.artifacts],
            "consult_id": self.consult_id,
            "lease_token": self.lease_token,
            "mandatory_consults": list(self.mandatory_consults),
            "order_id": self.order_id,
            "request_id": self.request_id,
            "schema_version": SCHEMA_VERSION,
        }


def _absolute(value: object, field: str) -> Path:
    if not isinstance(value, str) or not value:
        raise CompletionError(f"{field} must be a non-empty absolute path")
    path = Path(value)
    if not path.is_absolute():
        raise CompletionError(f"{field} must be an absolute path")
    return path


def ensure_publication_contract(order: dict[str, Any]) -> dict[str, Any]:
    """Add deterministic typed publication metadata to a compatibility order.

    Controller/legacy wrappers may omit the v1 metadata, but no such order reaches the ledger
    without a required three-artifact contract. Existing metadata is never rewritten.
    """
    raw = order.get("artifact_publication")
    if raw is None:
        result = _absolute(order.get("result_path"), "result_path")
        if result.name == "result.json":
            compacted = result.with_name("compacted.md")
            handoff = result.with_name("handoff.md")
        else:
            compacted = result.with_name(f"{result.stem}.compacted.md")
            handoff = result.with_name(f"{result.stem}.handoff.md")
        raw = {
            "schema_version": SCHEMA_VERSION,
            "compacted_path": str(compacted),
            "handoff_path": str(handoff),
            "mandatory_consults": [],
        }
        # A review's whole deliverable IS its verdict document: a "passed" with no
        # compacted.md is unusable and forces the caller to re-run the entire review
        # (observed in the field). Review-stage orders therefore default to declaring
        # `compacted` required AND to the review result contract — the verdict document
        # becomes a schema-validated result field the hub derives compacted.md from, so
        # the model literally cannot finish a review without producing the document.
        # Ordinary workers keep the module-docstring tolerance, and an order that spells
        # out its own artifact_publication decides for itself.
        if order.get("stage_id") == "stage-2-adversarial-audit":
            raw["required_artifacts"] = ["compacted"]
            order.setdefault("result_contract", "review")
        order["artifact_publication"] = raw
    return publication_contract(order)


def publication_contract(order: dict[str, Any]) -> dict[str, Any]:
    raw = order.get("artifact_publication")
    if raw is None:
        raise CompletionError(
            "artifact_publication is required; normalize compatibility input before ledger mutation"
        )
    if not isinstance(raw, dict):
        raise CompletionError("artifact_publication must be an object")
    allowed = {
        "schema_version",
        "compacted_path",
        "handoff_path",
        "answer_path",
        "consult_id",
        "consult_payload_sha256",
        "mandatory_consults",
        "required_artifacts",
    }
    unknown = set(raw) - allowed
    if unknown:
        raise CompletionError(
            "artifact_publication has unknown keys: " + ", ".join(sorted(unknown))
        )
    required_artifacts = raw.get("required_artifacts", [])
    if not isinstance(required_artifacts, list) or len(required_artifacts) != len(
        set(required_artifacts)
    ):
        raise CompletionError(
            "artifact_publication.required_artifacts must be a list of unique kinds"
        )
    for kind in required_artifacts:
        # result/answer are always mandatory — declaring them is meaningless, and only the
        # schemaless summaries can be promoted; an unknown kind is a contract typo.
        if kind not in ("compacted", "handoff"):
            raise CompletionError(
                "artifact_publication.required_artifacts may only name compacted or handoff"
            )
    if raw.get("schema_version") != SCHEMA_VERSION:
        raise CompletionError(
            f"artifact_publication.schema_version must be {SCHEMA_VERSION}"
        )
    _absolute(raw.get("compacted_path"), "artifact_publication.compacted_path")
    _absolute(raw.get("handoff_path"), "artifact_publication.handoff_path")
    answer_path = raw.get("answer_path")
    consult_id = raw.get("consult_id")
    if (answer_path is None) != (consult_id is None):
        raise CompletionError(
            "artifact_publication answer_path and consult_id must appear together"
        )
    if answer_path is not None:
        _absolute(answer_path, "artifact_publication.answer_path")
        if not isinstance(consult_id, str) or not consult_id:
            raise CompletionError(
                "artifact_publication.consult_id must be a non-empty string"
            )
    consult_payload_sha256 = raw.get("consult_payload_sha256")
    if consult_payload_sha256 is not None and (
        not isinstance(consult_payload_sha256, str)
        or len(consult_payload_sha256) != 64
        or any(
            character not in "0123456789abcdef" for character in consult_payload_sha256
        )
        or consult_id is None
    ):
        raise CompletionError(
            "artifact_publication.consult_payload_sha256 must be a lowercase SHA-256 for a consult"
        )
    requirements = raw.get("mandatory_consults", [])
    if not isinstance(requirements, list):
        raise CompletionError("artifact_publication.mandatory_consults must be a list")
    seen: set[str] = set()
    for index, requirement in enumerate(requirements):
        if not isinstance(requirement, dict) or set(requirement) != {
            "consult_id",
            "specialist",
        }:
            raise CompletionError(
                f"artifact_publication.mandatory_consults[{index}] must contain only consult_id and specialist"
            )
        for field in ("consult_id", "specialist"):
            if not isinstance(requirement.get(field), str) or not requirement[field]:
                raise CompletionError(
                    f"artifact_publication.mandatory_consults[{index}].{field} must be non-empty"
                )
        if requirement["consult_id"] in seen:
            raise CompletionError("mandatory consult ids must be unique")
        seen.add(requirement["consult_id"])
    return raw


def build_manifest(
    order: dict[str, Any], request_dir: Path, lease_token: str, request_id: str
) -> Manifest:
    contract = publication_contract(order)
    root = request_dir / "artifacts" / lease_token
    public = {
        "result": _absolute(order.get("result_path"), "result_path"),
        "compacted": _absolute(
            contract["compacted_path"], "artifact_publication.compacted_path"
        ),
        "handoff": _absolute(
            contract["handoff_path"], "artifact_publication.handoff_path"
        ),
    }
    if contract.get("answer_path") is not None:
        public["answer"] = _absolute(
            contract["answer_path"], "artifact_publication.answer_path"
        )
    declared_required = frozenset(contract.get("required_artifacts", []))
    artifacts = tuple(
        Artifact(
            kind,
            root
            / (
                "answer.json"
                if kind == "answer"
                else "result.json"
                if kind == "result"
                else f"{kind}.md"
            ),
            path,
            MEDIA_TYPES[kind],
            declared_required=kind in declared_required,
        )
        for kind, path in public.items()
    )
    requirements = tuple(dict(item) for item in contract.get("mandatory_consults", []))
    return Manifest(
        order_id=str(order["order_id"]),
        request_id=request_id,
        lease_token=lease_token,
        artifacts=artifacts,
        consult_id=contract.get("consult_id"),
        mandatory_consults=requirements,
    )


def write_manifest(path: Path, manifest: Manifest) -> None:
    _write_json_atomic(path, manifest.as_dict())
    parse_manifest(path.read_text(), manifest)


def parse_manifest(text: str, expected: Manifest | None = None) -> dict[str, object]:
    try:
        value = json.loads(text)
    except json.JSONDecodeError as error:
        raise CompletionError(
            f"artifact manifest is not valid JSON: {error}"
        ) from error
    if not isinstance(value, dict):
        raise CompletionError("artifact manifest must be an object")
    required = {
        "schema_version",
        "order_id",
        "request_id",
        "lease_token",
        "consult_id",
        "mandatory_consults",
        "artifacts",
    }
    if set(value) != required:
        raise CompletionError("artifact manifest keys do not match the closed schema")
    if value.get("schema_version") != SCHEMA_VERSION:
        raise CompletionError(
            f"artifact manifest schema_version must be {SCHEMA_VERSION}"
        )
    if expected is not None and value != expected.as_dict():
        raise CompletionError(
            "artifact manifest does not match the current request lease"
        )
    artifacts = value.get("artifacts")
    if not isinstance(artifacts, list):
        raise CompletionError("artifact manifest artifacts must be a list")
    kinds: list[str] = []
    for item in artifacts:
        if not isinstance(item, dict) or set(item) != {
            "kind",
            "media_type",
            "public_path",
            "required",
            "staging_path",
        }:
            raise CompletionError(
                "artifact manifest entry does not match the closed schema"
            )
        kind = item.get("kind")
        if kind not in MEDIA_TYPES or item.get("media_type") != MEDIA_TYPES[kind]:
            raise CompletionError(
                "artifact manifest entry has an invalid kind or media type"
            )
        # MANDATORY_KINDS can never be demoted; a summary may be promoted by the order's
        # declared contract, so its flag is a bool the lease-equality check (above, when
        # `expected` is present) cross-validates against the order-derived manifest.
        if kind in MANDATORY_KINDS:
            if item.get("required") is not True:
                raise CompletionError(f"artifact {kind} required flag must be True")
        elif not isinstance(item.get("required"), bool):
            raise CompletionError(f"artifact {kind} required flag must be a bool")
        _absolute(item.get("staging_path"), f"artifact {kind} staging_path")
        _absolute(item.get("public_path"), f"artifact {kind} public_path")
        kinds.append(str(kind))
    if tuple(kinds[:3]) != REQUIRED_KINDS or len(kinds) != len(set(kinds)):
        raise CompletionError(
            "artifact manifest must begin with result, compacted, handoff exactly once"
        )
    if value.get("consult_id") is not None and kinds != [*REQUIRED_KINDS, "answer"]:
        raise CompletionError(
            "a specialist manifest must include answer after the three lifecycle artifacts"
        )
    return value


def derive_review_compacted(
    order: dict[str, Any],
    manifest: Manifest,
    *,
    load_result: Callable[..., dict[str, Any]],
) -> None:
    """Author staged compacted.md FROM a review result's validated `verdict_document`.

    One source of truth: for a `result_contract: review` order the verdict document is a
    schema-validated field of result.json, and the compacted artifact is DERIVED from it at
    completion time instead of trusted as a separate free-form worker write — so the file
    and the result can never diverge, and there is nothing for the worker to forget.

    Deliberately quiet on absence: when the staged result is missing, unparseable, or has no
    usable `verdict_document`, there is nothing to derive from and bundle validation (which
    runs right after every call site) is the party that speaks. Idempotent: identical
    content is never rewritten, so the completion monitor may call this every poll.
    """
    if order.get("result_contract") != "review":
        return
    try:
        result = load_result(
            manifest.artifact("result").staging_path, manifest.order_id
        )
    except (OSError, ValueError):
        return
    document = result.get("verdict_document")
    if not isinstance(document, str) or not document.strip():
        return
    text = document if document.endswith("\n") else document + "\n"
    target = manifest.artifact("compacted").staging_path
    try:
        if target.is_file() and target.read_text(encoding="utf-8") == text:
            return
    except OSError:
        pass
    target.parent.mkdir(parents=True, exist_ok=True)
    _write_text_atomic(target, text)


def persist_normalized_result(manifest: Manifest, parsed: dict[str, Any]) -> None:
    """Write the reader-repaired result back to the staged file before projection.

    `contracts.parse_result` repairs harmless `revisit` shapes and records the repair as
    `revisit_normalized` — but publication copies the staged BYTES. Without this write-back
    the caller-visible result.json would keep the raw malformed field while every hub-side
    reader saw the normalized view. The staged file is rewritten only when a repair actually
    happened, so untouched results keep their original bytes.
    """
    if "revisit_normalized" not in parsed:
        return
    _write_json_atomic(manifest.artifact("result").staging_path, parsed)


def skip_optional(artifact: Artifact, path: Path) -> bool:
    """True when an optional artifact is absent or blank, so callers pass over it.

    A mandatory artifact is never skipped — a missing result.json is still fatal.  For an
    optional summary, blank is treated exactly like absent: neither carries information, and
    neither is worth failing a finished job over.
    """
    if artifact.required:
        return False
    try:
        if not path.is_file():
            return True
        return not path.read_text(encoding="utf-8").strip()
    except OSError:
        return True


def validate_bundle(
    manifest: Manifest,
    *,
    load_result: Callable[[str | Path, str | None], dict[str, Any]],
    load_answer: Callable[[str | Path, str | None], dict[str, Any]],
    public: bool = False,
) -> dict[str, Any]:
    result: dict[str, Any] | None = None
    for artifact in manifest.artifacts:
        path = artifact.public_path if public else artifact.staging_path
        if skip_optional(artifact, path):
            continue
        try:
            if artifact.kind == "result":
                result = load_result(path, manifest.order_id)
            elif artifact.kind == "answer":
                load_answer(path, manifest.consult_id)
            else:
                text = path.read_text(encoding="utf-8")
                if not text.strip():
                    raise CompletionError(
                        f"{artifact.kind}.md must contain non-whitespace text"
                    )
        except (OSError, ValueError) as error:
            location = "public" if public else "staged"
            raise CompletionError(
                f"{location} {artifact.kind} artifact is invalid: {error}"
            ) from error
    if result is None:
        location = "public" if public else "staged"
        raise CompletionError(f"{location} result artifact is missing")
    return result


def mandatory_consult_errors(
    manifest: Manifest, result: dict[str, Any], resolved: dict[str, list[Any]]
) -> list[str]:
    if not manifest.mandatory_consults:
        return []
    verified = resolved.get("consults_verified", [])
    unverified = resolved.get("consults_unverified", [])
    errors: list[str] = []
    for requirement in manifest.mandatory_consults:
        match = [
            item
            for item in verified
            if isinstance(item, dict)
            and item.get("consult_id") == requirement["consult_id"]
        ]
        if len(match) != 1:
            forged = any(
                isinstance(item, dict)
                and item.get("consult_id") == requirement["consult_id"]
                for item in unverified
            )
            errors.append(
                f"mandatory consult {requirement['consult_id']} has no Recruiter-verified receipt"
                + (" (the worker claim was rejected)" if forged else "")
            )
            continue
        item = match[0]
        if item.get("specialist") != requirement["specialist"]:
            errors.append(
                f"mandatory consult {requirement['consult_id']} resolved to the wrong specialist"
            )
        if item.get("answer_verdict") != "cited":
            errors.append(
                f"mandatory consult {requirement['consult_id']} did not publish a cited success answer"
            )
    return errors


def write_blocked_bundle(
    manifest: Manifest,
    reason: str,
    *,
    write_result: Callable[[Path, str], dict[str, Any]],
    failure_answer: Callable[[str, str], dict[str, Any]],
) -> dict[str, Any]:
    result = write_result(manifest.artifact("result").staging_path, reason)
    _write_text_atomic(
        manifest.artifact("compacted").staging_path,
        f"# Blocked completion\n\n{reason}\n",
    )
    _write_text_atomic(
        manifest.artifact("handoff").staging_path,
        "# Handoff\n\nPython blocked this request because its required completion bundle "
        f"could not be validated.\n\nReason: {reason}\n",
    )
    if manifest.consult_id is not None:
        _write_json_atomic(
            manifest.artifact("answer").staging_path,
            failure_answer(
                manifest.consult_id, f"upagent completion blocked: {reason}"
            ),
        )
    return result


def project_bundle(
    manifest: Manifest,
    *,
    load_result: Callable[[str | Path, str | None], dict[str, Any]],
    load_answer: Callable[[str | Path, str | None], dict[str, Any]],
) -> dict[str, Any]:
    """Validate staging, atomically replace each public file, then validate the public bundle.

    All temporary files are prepared and fsynced before the first public replacement.  A receipt
    or terminal event is the transaction commit marker; callers must publish neither until this
    function has returned successfully.
    """
    result = validate_bundle(
        manifest, load_result=load_result, load_answer=load_answer, public=False
    )
    temporaries: list[tuple[Path, Path]] = []
    backups: dict[Path, Path | None] = {}
    committed: list[Path] = []
    try:
        for artifact in manifest.artifacts:
            if skip_optional(artifact, artifact.staging_path):
                continue
            target = artifact.public_path
            target.parent.mkdir(parents=True, exist_ok=True)
            temporary = target.with_name(
                f".{target.name}.{uuid.uuid4().hex}.publish.tmp"
            )
            with (
                artifact.staging_path.open("rb") as source,
                temporary.open("wb") as sink,
            ):
                shutil.copyfileobj(source, sink)
                sink.flush()
                os.fsync(sink.fileno())
            temporaries.append((temporary, target))
            if target.exists():
                backup = target.with_name(
                    f".{target.name}.{uuid.uuid4().hex}.publish.backup"
                )
                with target.open("rb") as source, backup.open("wb") as sink:
                    shutil.copyfileobj(source, sink)
                    sink.flush()
                    os.fsync(sink.fileno())
                backups[target] = backup
            else:
                backups[target] = None
        for temporary, target in temporaries:
            os.replace(temporary, target)
            committed.append(target)
        for directory in {target.parent for _, target in temporaries}:
            descriptor = os.open(directory, os.O_DIRECTORY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        projected = validate_bundle(
            manifest, load_result=load_result, load_answer=load_answer, public=True
        )
        if projected != result:
            raise CompletionError(
                "public result differs from the validated staged result"
            )
    except (OSError, CompletionError) as error:
        rollback_error: OSError | None = None
        for target in reversed(committed):
            backup = backups[target]
            try:
                if backup is None:
                    target.unlink(missing_ok=True)
                else:
                    os.replace(backup, target)
            except OSError as current:
                rollback_error = current
        for temporary, _ in temporaries:
            temporary.unlink(missing_ok=True)
        for backup in backups.values():
            if backup is not None:
                backup.unlink(missing_ok=True)
        if rollback_error is not None:
            raise CompletionError(
                f"completion publication failed ({error}) and rollback failed: {rollback_error}"
            ) from rollback_error
        if isinstance(error, CompletionError):
            raise
        raise CompletionError(
            f"could not project completion bundle: {error}"
        ) from error
    for backup in backups.values():
        if backup is not None:
            backup.unlink(missing_ok=True)
    return projected


def _write_text_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("w", encoding="utf-8") as output:
            output.write(text)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    except OSError:
        temporary.unlink(missing_ok=True)
        raise


def _write_json_atomic(path: Path, value: object) -> None:
    _write_text_atomic(path, json.dumps(value, indent=2, sort_keys=True) + "\n")
