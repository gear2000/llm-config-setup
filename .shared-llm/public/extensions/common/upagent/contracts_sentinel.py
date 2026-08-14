"""The Sentinel closeout contract — a SEPARATE boundary from contracts.py.

That one gates the leader<->worker lifecycle (`result.json`); this one gates the
Recruiter<->Sentinel exchange (`closeout.json`): the single typed file a per-request
Sentinel pane writes when its worker's lifecycle ends, and the only file the Recruiter's
wait loop watches for sentinel-supervised requests.

Everything a Sentinel says is advisory. The Recruiter re-verifies every citation before
it counts (the rescuer rule), and a COMPLETE closeout never publishes `passed` on the
Sentinel's word — the staged bundle must independently pass the existing mechanical
validation. A fooled Sentinel may end a request early; it can never mint a false verdict.
The contract deliberately allows an empty or unverified `citations` list (an
uncorroborated closeout may still END the wait), but the Recruiter builds the published
blocked reason from Python-corroborated evidence only: verified citations are listed
separately as checked fact, while the Sentinel's prose — never mechanically checkable —
always carries its `(uncorroborated)` marker.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Sequence
from pathlib import Path

# The four ways a supervised worker lifecycle can end, per the Sentinel's duty cycle:
# LANDING verified the bundle on disk (COMPLETE); LIFTOFF saw no first tool action within
# its deadline (NEVER_STARTED); PULSE found the worker unrecoverably quiet mid-work
# (STALLED); the landing dialogue exhausted its exchange cap without a bundle
# (FINALIZATION_FAILED).
SENTINEL_OUTCOMES = ("COMPLETE", "NEVER_STARTED", "STALLED", "FINALIZATION_FAILED")

# The landing dialogue is bounded: at most this many question/answer exchanges before the
# Sentinel must stop steering and write FINALIZATION_FAILED with what actually got done.
MAX_LANDING_EXCHANGES = 3

_ALLOWED_KEYS = frozenset(
    (
        "request_id",
        "order_id",
        "outcome",
        "interpretation",
        "citations",
        "bundle",
        "blocking_question",
        "exchanges",
        "progress_so_far",
        "last_alive",
    )
)
_COMMIT_SHA_RE = re.compile(r"^[0-9a-f]{40}$")


class SentinelContractError(ValueError):
    """A malformed or forged closeout.json. Raised fail-loud; never swallowed."""


def _optional_str(value: object, field: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise SentinelContractError(
            f"closeout `{field}` must be a non-empty string or null (got {value!r})"
        )
    return value


def parse_closeout(text: str, request_id: str, order_id: str) -> dict:
    """Validate + return one Sentinel closeout. Fail-loud on any problem."""
    try:
        closeout = json.loads(text)
    except json.JSONDecodeError as error:
        raise SentinelContractError(
            f"closeout.json is not valid JSON: {error}"
        ) from error
    if not isinstance(closeout, dict):
        raise SentinelContractError("closeout.json must be a JSON object")
    unknown = set(closeout) - _ALLOWED_KEYS
    if unknown:
        raise SentinelContractError(
            "closeout.json has unknown keys: " + ", ".join(sorted(unknown))
        )
    if (closeout.get("request_id"), closeout.get("order_id")) != (request_id, order_id):
        raise SentinelContractError(
            f"closeout identity {closeout.get('request_id')!r}/"
            f"{closeout.get('order_id')!r} does not match request "
            f"{request_id!r}/{order_id!r}"
        )
    outcome = closeout.get("outcome")
    if outcome not in SENTINEL_OUTCOMES:
        raise SentinelContractError(
            f"closeout outcome {outcome!r} must be one of "
            + ", ".join(SENTINEL_OUTCOMES)
        )
    interpretation = closeout.get("interpretation")
    if not isinstance(interpretation, str) or not interpretation.strip():
        raise SentinelContractError(
            "closeout needs a non-empty `interpretation` string"
        )
    citations = closeout.get("citations")
    if not isinstance(citations, list) or any(
        not isinstance(item, str) or not item.strip() for item in citations
    ):
        raise SentinelContractError(
            "closeout `citations` must be a list of non-empty strings"
        )
    exchanges = closeout.get("exchanges")
    if not isinstance(exchanges, list):
        raise SentinelContractError(
            "closeout `exchanges` must be a list (empty when no landing dialogue ran)"
        )
    if len(exchanges) > MAX_LANDING_EXCHANGES:
        raise SentinelContractError(
            f"closeout records {len(exchanges)} exchanges; the landing dialogue is "
            f"capped at {MAX_LANDING_EXCHANGES}"
        )
    for exchange in exchanges:
        if not isinstance(exchange, dict):
            raise SentinelContractError(
                "each closeout exchange must be an object with `question` and `answer`"
            )
        for field in ("question", "answer"):
            value = exchange.get(field)
            if not isinstance(value, str) or not value.strip():
                raise SentinelContractError(
                    f"closeout exchange `{field}` must be a non-empty string"
                )
        verified = exchange.get("verified")
        if verified is not None and not isinstance(verified, bool):
            raise SentinelContractError(
                "closeout exchange `verified` must be a boolean when present"
            )
        unknown_exchange = set(exchange) - {"question", "answer", "verified"}
        if unknown_exchange:
            raise SentinelContractError(
                "closeout exchange has unknown keys: "
                + ", ".join(sorted(unknown_exchange))
            )
    bundle = _optional_str(closeout.get("bundle"), "bundle")
    if outcome == "COMPLETE" and bundle is None:
        raise SentinelContractError(
            "a COMPLETE closeout must name the `bundle` it verified on disk"
        )
    _optional_str(closeout.get("blocking_question"), "blocking_question")
    if outcome == "STALLED":
        for field in ("progress_so_far", "last_alive"):
            value = closeout.get(field)
            if not isinstance(value, str) or not value.strip():
                raise SentinelContractError(
                    f"a STALLED closeout must carry a non-empty `{field}` string"
                )
    else:
        _optional_str(closeout.get("progress_so_far"), "progress_so_far")
        _optional_str(closeout.get("last_alive"), "last_alive")
    return closeout


def load_closeout(path: Path, request_id: str, order_id: str) -> dict:
    """Read + validate one closeout file. OSError and contract errors propagate."""
    return parse_closeout(path.read_text(encoding="utf-8"), request_id, order_id)


def _path_within_scope(value: str, scope_roots: Sequence[str | Path]) -> bool:
    """True only when the cited absolute path resolves inside one of the scope roots.

    Resolution (symlinks included) happens BEFORE the containment check, so a link
    planted inside the worktree cannot smuggle an out-of-scope target into scope.
    """
    try:
        resolved = Path(value).resolve()
    except OSError:
        return False
    for root in scope_roots:
        try:
            root_resolved = Path(root).resolve()
        except OSError:
            continue
        if resolved == root_resolved or resolved.is_relative_to(root_resolved):
            return True
    return False


def verify_citations(
    citations: list[str],
    *,
    file_exists: Callable[[str], bool],
    commit_exists: Callable[[str], bool],
    scope_roots: Sequence[str | Path],
) -> tuple[list[str], list[str]]:
    """Split citations into (corroborated, uncorroborated) by mechanical re-verification.

    The rescuer rule applied to the Sentinel: a 40-hex citation must resolve to a real
    commit; an absolute path must exist on disk AND live inside one of `scope_roots` —
    the request's own territory (worktree/cwd subtree, ledger directory). A path that
    exists but sits outside every root proves nothing about THIS request (/etc/passwd
    exists on every machine), so it is recorded uncorroborated with an explicit
    out-of-scope marker. An empty `scope_roots` therefore leaves every path citation
    uncorroborated. Anything else — prose, a relative path, a rounded SHA — is
    uncorroborated by form. Only corroborated citations may accompany the Sentinel's
    interpretation into a terminal record.
    """
    corroborated: list[str] = []
    uncorroborated: list[str] = []
    for citation in citations:
        value = citation.strip()
        if _COMMIT_SHA_RE.fullmatch(value):
            (corroborated if commit_exists(value) else uncorroborated).append(citation)
        elif value.startswith("/"):
            if not _path_within_scope(value, scope_roots):
                uncorroborated.append(
                    f"{citation} (out-of-scope)"
                    if file_exists(value)
                    else citation
                )
            elif file_exists(value):
                corroborated.append(citation)
            else:
                uncorroborated.append(citation)
        else:
            uncorroborated.append(citation)
    return corroborated, uncorroborated
