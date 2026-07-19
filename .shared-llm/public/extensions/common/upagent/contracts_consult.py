"""Specialist consult/answer contracts — the single source of truth for the two files a
caller (a worker) and a specialist exchange through the Recruiter's consult door.

Two JSON files cross the boundary, mirroring the UpAgent order/result pattern:

  consult.json — a caller writes it; the consult door reads it to route + brief a specialist.
  answer.json  — the transient specialist writes it before its pane closes; the caller reads
                 it as the authoritative answer to its question.

`answer.json` is a DIFFERENT artifact from the order's `result.json`, and both are written.
`result.json` says the specialist worker ran and delivered — the Recruiter's lifecycle receipt,
validated by `contracts.parse_result` like every other worker's. `answer.json` says the answer
is backed by citations, and `parse_answer` below is the only mechanical check of that anywhere
in the consult path. Folding citations into `result.json` would put consult-specific keys in
front of every non-consult worker in the system.

Everything here is fail-loud: a malformed consult or answer raises `ConsultError` with a
precise message rather than being silently tolerated. The door refuses to spawn a specialist on
a bad consult; the caller treats a missing/bad answer as an unanswered consult.

This module is pure stdlib and has no Herdr dependency, so it is unit-testable without a
running Herdr instance (see contracts_consult_test.py).
"""

from __future__ import annotations

import json
import re
from pathlib import Path

# A citation is a `file:line` (or `file:line-range`) reference into the repo — the evidence a
# specialist answer must carry. Enforced so an answer cannot be hand-wavy prose with no source.
CITATION_RE = re.compile(r"^.+:\d+(-\d+)?$")

# Required keys on a consult.json the caller writes.
CONSULT_REQUIRED = (
    "consult_id",   # unique per question, e.g. "phase-0.stage-1.pass-1.consult-1"
    "specialist",   # roster name the door routes to (a key in specialists.yaml)
    "question",     # the natural-language question for the specialist
    "answer_path",  # absolute path the specialist MUST write answer.json to
)

# Required keys on an answer.json the specialist writes.
ANSWER_REQUIRED = (
    "consult_id",   # MUST echo the consult's consult_id
    "answer",       # the answer text
    # `citations` is validated separately below (list shape + file:line form).
)


class ConsultError(ValueError):
    """A malformed consult.json or answer.json. Raised fail-loud; never swallowed."""


def _require_str(obj: dict, key: str, where: str) -> str:
    val = obj.get(key)
    if not isinstance(val, str) or val == "":
        raise ConsultError(f"{where}: `{key}` must be a non-empty string (got {val!r})")
    return val


def parse_consult(text: str) -> dict:
    """Validate + return a consult dict from raw JSON text. Fail-loud on any problem."""
    try:
        consult = json.loads(text)
    except json.JSONDecodeError as e:
        raise ConsultError(f"consult.json is not valid JSON: {e}") from e
    if not isinstance(consult, dict):
        raise ConsultError("consult.json must be a JSON object")

    for key in CONSULT_REQUIRED:
        _require_str(consult, key, "consult.json")

    # Optional `cwd`: the directory the specialist runs in so its citations resolve. When
    # present it must be a non-empty string; when absent the door falls back to the repo root
    # its merged roster was resolved from.
    cwd = consult.get("cwd")
    if cwd is not None and (not isinstance(cwd, str) or cwd == ""):
        raise ConsultError("consult.json: `cwd` must be a non-empty string when present")
    return consult


def parse_answer(text: str, expected_consult_id: str | None = None) -> dict:
    """Validate + return an answer dict from raw JSON text. Fail-loud on any problem.

    Two legal shapes, distinguished by the `error` key:
      - a SUCCESS answer carries `answer` text + a non-empty `citations` list of `file:line`s;
      - a FAILURE answer carries a non-empty `error` string (and no answer/citations required) —
        the door writes one when a consult cannot be answered, so the caller's bounded wait
        resolves to a legible failure instead of only timing out. Mirrors the Recruiter writing a
        `blocked` result.json rather than leaving the leader to hang.

    When `expected_consult_id` is given, the answer's consult_id MUST match it — this catches a
    stale answer.json left over from a prior consult at the same path (checked for both shapes).
    """
    try:
        answer = json.loads(text)
    except json.JSONDecodeError as e:
        raise ConsultError(f"answer.json is not valid JSON: {e}") from e
    if not isinstance(answer, dict):
        raise ConsultError("answer.json must be a JSON object")

    _require_str(answer, "consult_id", "answer.json")
    if expected_consult_id is not None and answer["consult_id"] != expected_consult_id:
        raise ConsultError(
            f"answer.json: consult_id {answer['consult_id']!r} does not match the consult "
            f"{expected_consult_id!r} — stale or mismatched answer file"
        )

    # A failure answer: `error` present ⇒ this is a signaled failure, not a real answer.
    err = answer.get("error")
    if err is not None:
        if not isinstance(err, str) or not err.strip():
            raise ConsultError("answer.json: `error` must be a non-empty string when present")
        return answer

    # A success answer: real answer text + at least one `file:line` citation backing it.
    _require_str(answer, "answer", "answer.json")
    citations = answer.get("citations")
    if not isinstance(citations, list) or not citations:
        raise ConsultError(
            "answer.json: `citations` must be a non-empty list of `file:line` strings"
        )
    for c in citations:
        if not isinstance(c, str) or not CITATION_RE.match(c):
            raise ConsultError(
                f"answer.json: citation {c!r} must be a `file:line` (or `file:line-range`) string"
            )
    return answer


def failure_answer(consult_id: str, error: str) -> dict:
    """The canonical FAILURE answer.json shape the door writes when a consult cannot be
    answered. `parse_answer` accepts it; the caller reads `error` to see the consult failed."""
    if not consult_id:
        raise ConsultError("failure_answer: consult_id must be non-empty")
    if not error or not error.strip():
        raise ConsultError("failure_answer: error must be non-empty")
    return {"consult_id": consult_id, "error": error}


def load_consult(path: str | Path) -> dict:
    """Read + validate a consult.json file. Fail-loud if missing or malformed."""
    p = Path(path)
    if not p.is_file():
        raise ConsultError(f"consult.json not found: {p}")
    return parse_consult(p.read_text())


def load_answer(path: str | Path, expected_consult_id: str | None = None) -> dict:
    """Read + validate an answer.json file. Fail-loud if missing or malformed."""
    p = Path(path)
    if not p.is_file():
        raise ConsultError(f"answer.json not found: {p}")
    return parse_answer(p.read_text(), expected_consult_id)
