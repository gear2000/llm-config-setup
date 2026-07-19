"""Characterization tests for the consult ANSWER contract — the evidence gate on a specialist's
reply. This is the only mechanical check that a consult answer is backed by real citations rather
than confident prose, so it must outlive whichever module happens to implement it.

MIGRATION GATE. The implementation lives in the Specialist Hub today and moves into the Recruiter
when the hub folds in. The move is three lines, all of them in the SEAM block below; every test
is written against behavior and should pass unchanged. Do not delete this file with the hub —
the gate is the point, the hub is just its current address.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest


MODULES = Path(__file__).resolve().parent.parent

# ═══ THE SEAM ════════════════════════════════════════════════════════════════════════════
# This file reaches the implementation through these three names and nothing else.
# When the Specialist Hub folds into the Recruiter, change them — all three, here, only here:
#
#   IMPLEMENTATION -> "upagent/<the module that validates consult answers>.py"
#   VALIDATOR      -> its "parse an answer, raise when refusing it" function
#   REFUSAL        -> the exception that function raises
#
IMPLEMENTATION = "upagent/contracts_consult.py"   # VALIDATOR and REFUSAL unchanged by the move
VALIDATOR = "parse_answer"
REFUSAL = "ConsultError"
# ═════════════════════════════════════════════════════════════════════════════════════════


_MOVED = """
CONSULT ANSWER CONTRACT — the implementation moved and this seam did not follow it.

    IMPLEMENTATION = {implementation!r}
    resolves to {path}
    which does not exist.

This file is a MIGRATION GATE, not a test of the Specialist Hub. It pins the only mechanical
check that a consult answer carries real `file:line` citations instead of confident prose. The
hub was this contract's address, never its owner.

TO FIX: point the three constants in the SEAM block at the module that validates consult
answers now — look under {modules}/upagent/ — and run this file again. All {count} tests below
are written against behavior and should pass without being touched.

DO NOT delete this file to clear the error. That removes the citation requirement from the
consult path entirely, and `tools/test_phase4_acceptance.py` fails its
`migrated-capability-enforced` criterion on the missing file.
"""


def _load_seam():
    """Import IMPLEMENTATION, or explain the repoint. Never skip: a silently disabled gate is
    the failure mode this whole file exists to prevent."""
    path = MODULES / IMPLEMENTATION
    if not path.is_file():
        raise RuntimeError(
            _MOVED.format(implementation=IMPLEMENTATION, path=path, modules=MODULES, count=26)
        )
    spec = importlib.util.spec_from_file_location("consult_answer_contract", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    for attribute in (VALIDATOR, REFUSAL):
        if not hasattr(module, attribute):
            raise RuntimeError(
                f"{IMPLEMENTATION} has no {attribute!r} — update the SEAM block in {__file__}"
            )
    return module


_contract = _load_seam()

RejectedAnswer = getattr(_contract, REFUSAL)


def _accept(answer: dict, expected_consult_id: str | None = None) -> dict:
    """Submit an answer to the contract. Returns it when accepted, raises when refused."""
    return getattr(_contract, VALIDATOR)(json.dumps(answer), expected_consult_id)


CONSULT_ID = "phase-0.stage-1.pass-1.consult-1"


def _answer(**overrides: object) -> dict:
    base = {
        "consult_id": CONSULT_ID,
        "answer": "The retry budget is checked in the leader loop before each try.",
        "citations": ["recruiter.py:134"],
    }
    base.update(overrides)
    return base


# --- accepted -----------------------------------------------------------------


def test_a_file_line_citation_is_accepted() -> None:
    assert _accept(_answer())["citations"] == ["recruiter.py:134"]


def test_a_line_range_citation_is_accepted() -> None:
    _accept(_answer(citations=["contracts.py:96-101"]))


def test_pathed_and_absolute_citations_are_accepted() -> None:
    _accept(_answer(citations=["a/b/c.py:1", "/srv/repo/contracts.py:88"]))


def test_a_file_line_column_citation_is_accepted() -> None:
    """The trailing `:\\d+` is what is anchored, so a `file:line:column` form passes. A stricter
    reimplementation (`^[^:]+:\\d+$`) would start rejecting these — that is a behavior change."""
    _accept(_answer(citations=["recruiter.py:134:12"]))


def test_every_citation_in_a_list_is_kept() -> None:
    citations = ["recruiter.py:1", "contracts.py:2-3", "hub.py:4"]
    assert _accept(_answer(citations=citations))["citations"] == citations


def test_a_failure_answer_needs_no_citations() -> None:
    """The always-answer guarantee: a consult that could not be answered still resolves the
    caller's bounded wait, so the evidence gate must not apply to a signaled failure."""
    assert _accept({"consult_id": CONSULT_ID, "error": "specialist crashed"})["error"]


def test_a_matching_consult_id_echo_is_accepted() -> None:
    assert _accept(_answer(), expected_consult_id=CONSULT_ID)["consult_id"] == CONSULT_ID


# --- rejected -----------------------------------------------------------------


def test_an_answer_with_no_citations_key_is_rejected() -> None:
    bad = _answer()
    del bad["citations"]
    with pytest.raises(RejectedAnswer, match="citations"):
        _accept(bad)


def test_an_empty_citation_list_is_rejected() -> None:
    with pytest.raises(RejectedAnswer, match="citations"):
        _accept(_answer(citations=[]))


def test_citations_that_are_not_a_list_are_rejected() -> None:
    with pytest.raises(RejectedAnswer, match="citations"):
        _accept(_answer(citations="recruiter.py:134"))


def test_a_citation_without_a_line_number_is_rejected() -> None:
    with pytest.raises(RejectedAnswer, match="file:line"):
        _accept(_answer(citations=["recruiter.py"]))


def test_a_citation_with_a_non_numeric_line_is_rejected() -> None:
    with pytest.raises(RejectedAnswer, match="file:line"):
        _accept(_answer(citations=["recruiter.py:somewhere"]))


def test_a_citation_with_no_file_is_rejected() -> None:
    with pytest.raises(RejectedAnswer, match="file:line"):
        _accept(_answer(citations=[":134"]))


def test_a_citation_that_is_not_a_string_is_rejected() -> None:
    with pytest.raises(RejectedAnswer, match="file:line"):
        _accept(_answer(citations=[134]))


def test_one_bad_citation_rejects_the_whole_answer() -> None:
    """Every citation is checked, not just the first — otherwise a single real reference
    launders a list of unsourced claims behind it."""
    with pytest.raises(RejectedAnswer, match="file:line"):
        _accept(_answer(citations=["recruiter.py:134", "trust me"]))


def test_a_citation_smuggling_a_newline_is_rejected() -> None:
    """One citation is one reference: a newline-joined pair is not two citations."""
    with pytest.raises(RejectedAnswer, match="file:line"):
        _accept(_answer(citations=["recruiter.py:1\ncontracts.py:2"]))


def test_an_answer_with_citations_but_no_answer_text_is_rejected() -> None:
    bad = _answer()
    del bad["answer"]
    with pytest.raises(RejectedAnswer, match="answer"):
        _accept(bad)


def test_an_empty_answer_text_is_rejected() -> None:
    with pytest.raises(RejectedAnswer, match="answer"):
        _accept(_answer(answer=""))


def test_a_stale_answer_at_a_reused_path_is_rejected() -> None:
    """The consult_id echo is what distinguishes this consult's answer from the previous
    consult's leftovers at the same answer_path."""
    with pytest.raises(RejectedAnswer, match="does not match"):
        _accept(_answer(), expected_consult_id="phase-0.stage-1.pass-1.consult-2")


def test_a_failure_answer_is_still_consult_id_checked() -> None:
    with pytest.raises(RejectedAnswer, match="does not match"):
        _accept({"consult_id": CONSULT_ID, "error": "boom"}, expected_consult_id="other")


def test_a_blank_error_does_not_buy_a_citation_exemption() -> None:
    """`error` is the only way past the evidence gate, so an empty one must not open it."""
    with pytest.raises(RejectedAnswer, match="error"):
        _accept({"consult_id": CONSULT_ID, "error": "   "})
