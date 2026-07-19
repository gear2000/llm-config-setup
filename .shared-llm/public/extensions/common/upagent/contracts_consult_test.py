"""Unit tests for the specialist consult/answer contracts. Pure stdlib — no Herdr needed.

Run: python3 -m pytest .shared-llm/public/extensions/common/upagent/contracts_consult_test.py -q
"""

from __future__ import annotations

import json

import pytest

# Import the sibling module by path so the test runs from the repo root without packaging.
import importlib.util
from pathlib import Path

_spec = importlib.util.spec_from_file_location(
    "specialist_contracts_consult", Path(__file__).with_name("contracts_consult.py")
)
contracts = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(contracts)  # type: ignore[union-attr]
ConsultError = contracts.ConsultError


def _valid_consult() -> dict:
    return {
        "consult_id": "phase-0.stage-1.pass-1.consult-1",
        "specialist": "python",
        "question": "Where is the retry budget enforced?",
        "answer_path": "/tmp/run/answer.json",
    }


def _valid_answer() -> dict:
    return {
        "consult_id": "phase-0.stage-1.pass-1.consult-1",
        "answer": "The retry budget is checked in the leader loop before each try.",
        "citations": ["hub.py:134", "contracts.py:96-101"],
    }


def test_valid_consult_parses() -> None:
    consult = contracts.parse_consult(json.dumps(_valid_consult()))
    assert consult["specialist"] == "python"


def test_consult_missing_key_fails() -> None:
    bad = _valid_consult()
    del bad["answer_path"]
    with pytest.raises(ConsultError, match="answer_path"):
        contracts.parse_consult(json.dumps(bad))


def test_consult_empty_question_fails() -> None:
    bad = _valid_consult()
    bad["question"] = ""
    with pytest.raises(ConsultError, match="question"):
        contracts.parse_consult(json.dumps(bad))


def test_consult_bad_cwd_fails() -> None:
    bad = _valid_consult()
    bad["cwd"] = 3
    with pytest.raises(ConsultError, match="cwd"):
        contracts.parse_consult(json.dumps(bad))


def test_consult_cwd_optional_absent_ok() -> None:
    contracts.parse_consult(json.dumps(_valid_consult()))  # no cwd key → fine


def test_consult_not_json_fails() -> None:
    with pytest.raises(ConsultError, match="not valid JSON"):
        contracts.parse_consult("{not json")


def test_valid_answer_parses() -> None:
    a = contracts.parse_answer(json.dumps(_valid_answer()))
    assert a["citations"] == ["hub.py:134", "contracts.py:96-101"]


def test_answer_missing_answer_fails() -> None:
    bad = _valid_answer()
    del bad["answer"]
    with pytest.raises(ConsultError, match="answer"):
        contracts.parse_answer(json.dumps(bad))


def test_answer_empty_citations_fails() -> None:
    bad = _valid_answer()
    bad["citations"] = []
    with pytest.raises(ConsultError, match="citations"):
        contracts.parse_answer(json.dumps(bad))


def test_answer_missing_citations_fails() -> None:
    bad = _valid_answer()
    del bad["citations"]
    with pytest.raises(ConsultError, match="citations"):
        contracts.parse_answer(json.dumps(bad))


def test_answer_malformed_citation_fails() -> None:
    bad = _valid_answer()
    bad["citations"] = ["hub.py"]  # no :line
    with pytest.raises(ConsultError, match="file:line"):
        contracts.parse_answer(json.dumps(bad))


def test_answer_consult_id_mismatch_fails() -> None:
    with pytest.raises(ConsultError, match="does not match"):
        contracts.parse_answer(json.dumps(_valid_answer()), expected_consult_id="other-id")


def test_answer_consult_id_match_ok() -> None:
    a = contracts.parse_answer(
        json.dumps(_valid_answer()),
        expected_consult_id="phase-0.stage-1.pass-1.consult-1",
    )
    assert a["consult_id"] == "phase-0.stage-1.pass-1.consult-1"


def test_failure_answer_parses_without_citations() -> None:
    fa = {"consult_id": "phase-0.stage-1.pass-1.consult-1", "error": "specialist crashed"}
    parsed = contracts.parse_answer(json.dumps(fa))
    assert parsed["error"] == "specialist crashed"


def test_failure_answer_empty_error_fails() -> None:
    bad = {"consult_id": "c-1", "error": "   "}
    with pytest.raises(ConsultError, match="error"):
        contracts.parse_answer(json.dumps(bad))


def test_failure_answer_still_echo_checks_consult_id() -> None:
    fa = {"consult_id": "c-1", "error": "boom"}
    with pytest.raises(ConsultError, match="does not match"):
        contracts.parse_answer(json.dumps(fa), expected_consult_id="other-id")


def test_failure_answer_helper_shape() -> None:
    fa = contracts.failure_answer("c-1", "boom")
    assert fa == {"consult_id": "c-1", "error": "boom"}
    # round-trips through the parser
    assert contracts.parse_answer(json.dumps(fa))["error"] == "boom"


def test_failure_answer_helper_rejects_empty() -> None:
    with pytest.raises(ConsultError, match="error must be non-empty"):
        contracts.failure_answer("c-1", "")
