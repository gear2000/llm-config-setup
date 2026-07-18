"""Forgiving intake for the Specialist Hub — liberal in what it accepts, conservative in
what it executes (Postel's law, applied to an LLM-facing broker).

A caller — usually an LLM — may not know the exact consult schema. Instead of bouncing a
near-miss with a parse error (which trains agents to improvise around the broker), the hub
walks every failed submission down this ladder:

    1. strict parse           already handled by the caller (contracts_consult.parse_consult)
    2. mechanical repair      THIS MODULE — deterministic, no LLM: map field aliases,
                              generate a missing consult_id, default a missing answer_path,
                              resolve an unambiguous specialist-name variant
    3. intake clerk           an LLM hired through the Recruiter that turns prose or
                              unfixable payloads into a valid consult — or says what is
                              missing (the hub owns the hire; this module owns the brief
                              and the strict validation of the clerk's output)
    4. helpful refusal        a message naming what was understood and what is missing

Two invariants keep forgiveness safe:
  * The interpretation is always written down: the normalized consult and an intake record
    (mode, raw path, every change made) are persisted in the consults directory, so the
    caller and the Stage 2 audit can see exactly how a sloppy ask was understood.
  * Guess the FORM, never the INTENT: ids and paths may be invented; the specialist and the
    question may only be mapped or matched, never fabricated. When intent cannot be
    established, the ladder ends in a refusal, not a guess.

Forgiveness is a safety net, not an advertised API: agent-facing briefs (the phone book)
keep teaching the strict format; this module exists so a near-miss reaches the strict core
instead of dying at the door.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
import time
import uuid

import contracts_consult as cc  # sibling module; the importing hub put HERE on sys.path

# Caller-supplied ids become file names in the consults directory; anything outside this
# charset (path separators above all) is REGENERATED — ids are form, never intent.
SAFE_ID_RE = re.compile(r"^[A-Za-z0-9._-]+$")


# Accepted spellings for each consult field, first entry canonical. Alias mapping is FORM:
# the caller plainly said "agent"/"ask"; we relabel, we do not reinterpret.
FIELD_ALIASES: dict[str, tuple[str, ...]] = {
    "consult_id": ("consult_id", "id", "ticket", "ticket_id", "consult"),
    "specialist": ("specialist", "agent", "expert", "who", "specialist_name", "to"),
    "question": ("question", "q", "ask", "query", "prompt", "questions"),
    "answer_path": (
        "answer_path",
        "answer",
        "answer_file",
        "output",
        "output_path",
        "result_path",
        "response_path",
    ),
    "cwd": ("cwd", "repo", "workdir", "working_directory"),
}

# The clerk writes exactly one of these two shapes to its output file:
#   {"consult": {consult_id, specialist, question, answer_path[, cwd]}}
#   {"error": "<what is missing or ambiguous>", "missing": ["field", ...]}
CLERK_OUTPUT_KEYS = ("consult", "error")


def generate_consult_id() -> str:
    return f"intake-{uuid.uuid4().hex[:12]}"


def generate_clerk_tag() -> str:
    """Clerk-hire artifacts get their own `clerk-` prefix so observability (the librarian's
    served tally) can tell normalization hires apart from real consults — including the
    mechanically repaired consults that carry the `intake-` prefix."""
    return f"clerk-{uuid.uuid4().hex[:12]}"


def deterministic_id(raw_text: str) -> str:
    """A repair-generated id derived from the raw submission bytes, so an LLM retry loop
    that rewrites the same sloppy file gets the SAME id — downstream dedupe keeps working
    instead of hiring twice."""
    return f"intake-{hashlib.sha256(raw_text.encode()).hexdigest()[:12]}"


def _first_alias_value(raw: dict, canonical: str) -> tuple[object, str | None]:
    """(value, alias_used) for the first alias present in raw, else (None, None)."""
    for alias in FIELD_ALIASES[canonical]:
        if alias in raw:
            return raw[alias], alias
    return None, None


def _resolve_specialist(value: str, roster_names: list[str]) -> tuple[str | None, str | None]:
    """(resolved_name, note) when `value` matches exactly one roster name under deterministic
    normalizations; (None, None) when ambiguous or unmatched — that is the clerk's job, made
    with the full phone book in front of it, or a refusal's."""
    if value in roster_names:
        return value, None
    lowered = value.lower()
    case_matches = [n for n in roster_names if n.lower() == lowered]
    if len(case_matches) == 1:
        return case_matches[0], f"specialist {value!r} matched {case_matches[0]!r} (case)"
    stripped = lowered.removesuffix("-agent")
    suffix_matches = [
        n for n in roster_names if n.lower() in (stripped, f"{stripped}-agent")
    ]
    if len(set(suffix_matches)) == 1:
        resolved = suffix_matches[0]
        return resolved, f"specialist {value!r} matched {resolved!r} (-agent suffix)"
    return None, None


def mechanical_repair(
    raw_text: str,
    *,
    roster_names: list[str],
    consults_dir: Path,
) -> tuple[dict, list[str]] | None:
    """Deterministically normalize a near-miss JSON submission into a consult dict.

    Returns (consult, changes) — `changes` is the human-readable record persisted with the
    intake stamp — or None when repair cannot produce a valid consult without guessing
    intent (hand the raw payload to the clerk). Pure: no filesystem writes here.
    """
    try:
        raw = json.loads(raw_text)
    except json.JSONDecodeError:
        return None  # prose or broken JSON — clerk territory
    if not isinstance(raw, dict):
        return None

    consult: dict[str, object] = {}
    changes: list[str] = []

    for canonical in ("specialist", "question", "consult_id", "answer_path", "cwd"):
        value, alias = _first_alias_value(raw, canonical)
        if value is None:
            continue
        if canonical == "question" and isinstance(value, list):
            if not value or not all(isinstance(item, str) and item for item in value):
                return None
            value = "\n".join(value)
            changes.append("joined question list into one question")
        if not isinstance(value, str) or not value:
            return None  # a non-string where intent lives — do not coerce, escalate
        consult[canonical] = value
        if alias != canonical:
            changes.append(f"mapped field {alias!r} -> {canonical!r}")
        for other in FIELD_ALIASES[canonical]:
            if other != alias and other in raw and raw[other] != value:
                changes.append(
                    f"ignored conflicting alias {other!r} for {canonical!r} (kept {alias!r})"
                )

    known_aliases = {a for aliases in FIELD_ALIASES.values() for a in aliases}
    dropped = sorted(key for key in raw if key not in known_aliases)
    if dropped:
        changes.append(f"dropped unrecognized fields: {', '.join(dropped)}")

    # Intent must be present: a specialist and a question. Everything below is form.
    specialist = consult.get("specialist")
    if not isinstance(specialist, str) or "question" not in consult:
        return None
    resolved, note = _resolve_specialist(specialist, roster_names)
    if resolved is None:
        return None
    if note is not None:
        changes.append(note)
    consult["specialist"] = resolved

    consult_id = consult.get("consult_id")
    if isinstance(consult_id, str) and consult_id and not SAFE_ID_RE.match(consult_id):
        consult.pop("consult_id")
        changes.append(
            f"regenerated consult_id: {consult_id!r} is not filesystem-safe"
        )
    if "consult_id" not in consult:
        consult["consult_id"] = deterministic_id(raw_text)
        changes.append(f"generated consult_id {consult['consult_id']}")
    if "answer_path" not in consult:
        consult["answer_path"] = str(
            consults_dir / f"{consult['consult_id']}-answer.json"
        )
        changes.append(f"defaulted answer_path to {consult['answer_path']}")
    elif not Path(str(consult["answer_path"])).is_absolute():
        consult["answer_path"] = str(consults_dir / str(consult["answer_path"]))
        changes.append(f"anchored relative answer_path at {consult['answer_path']}")

    if not changes:
        return None  # nothing repairable changed; the strict error stands as-is
    try:
        cc.parse_consult(json.dumps(consult))
    except cc.ConsultError:
        return None  # repair could not reach a valid consult — escalate, never half-fix
    return consult, changes


def clerk_brief(raw_text: str, roster_index: dict, output_path: str) -> str:
    """The one-shot brief for the intake clerk. The clerk fills in envelopes, never meaning:
    it may generate ids and paths, map names to the roster, and reshape fields — it must
    never invent a question or pick a specialist the payload does not point at."""
    roster_lines = "\n".join(
        f"  - {name}: {entry.get('description', '')}"
        for name, entry in roster_index.items()
    )
    return (
        "You are the Specialist Hub intake clerk. One imperfect consult submission follows; "
        "convert it into a VALID consult, or explain precisely what is missing.\n\n"
        "Rules — envelopes, not meaning:\n"
        "1. NEVER invent the question or the specialist. Both must be identifiable in the "
        "payload. If the payload names a specialist loosely, match it to the roster below "
        "only when the match is unambiguous.\n"
        "2. You MAY generate a consult_id and an absolute answer_path (under the consults "
        "directory of this submission) when missing, and reshape/rename fields.\n"
        "3. If the payload contains questions for MORE THAN ONE specialist, do not merge "
        "them: return an error explaining it must be one consult per specialist, and list "
        "each (specialist, question) pair you found so the caller can resubmit them.\n"
        "4. Do not answer the question yourself. Do not run commands. Do not read the repo.\n\n"
        f"Write STRICT JSON to {output_path} — exactly one of:\n"
        '  {"consult": {"consult_id": "...", "specialist": "<roster name>", '
        '"question": "...", "answer_path": "/absolute/path.json"}}\n'
        '  {"error": "<what is missing or ambiguous>", "missing": ["field", ...]}\n\n'
        f"Roster (the only valid specialist names):\n{roster_lines}\n\n"
        "Raw submission between the markers:\n"
        "----- BEGIN SUBMISSION -----\n"
        f"{raw_text}\n"
        "----- END SUBMISSION -----\n"
    )


def parse_clerk_output(text: str) -> dict:
    """Validate the clerk's output STRICTLY (the conservative half of Postel). Returns the
    parsed object; raises ValueError on any deviation — a sloppy clerk is refused exactly
    like a sloppy caller, because its output enters the strict core."""
    document = json.loads(text)
    if not isinstance(document, dict):
        raise ValueError("clerk output must be a JSON object")
    present = [key for key in CLERK_OUTPUT_KEYS if key in document]
    if len(present) != 1:
        raise ValueError(
            "clerk output must contain exactly one of 'consult' or 'error'"
        )
    if "error" in document:
        if not isinstance(document["error"], str) or not document["error"]:
            raise ValueError("clerk 'error' must be a non-empty string")
    return document


def intake_record(
    *,
    mode: str,
    raw_path: str,
    normalized_path: str | None,
    changes: list[str],
) -> dict:
    """The durable stamp persisted (in the consults directory) for every normalized
    submission — the audit's view of how a sloppy ask was understood."""
    return {
        "at_ns": time.time_ns(),
        "mode": mode,
        "raw_path": raw_path,
        "normalized_path": normalized_path,
        "changes": changes,
    }
