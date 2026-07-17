# pyright: reportMissingImports=false
"""Unit tests for the IaC plan table builder. Pure stdlib — no terraform needed.

Run: python3 -m pytest .shared-llm/public/extensions/common/iac/plan_table_test.py -q
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

_spec = importlib.util.spec_from_file_location(
    "iac_plan_table", Path(__file__).with_name("plan_table.py")
)
assert _spec and _spec.loader
plan_table = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(plan_table)

FIXTURES = Path(__file__).with_name("fixtures")


def _fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


def test_add_only_plan_counts_and_renders() -> None:
    rows = plan_table.rows(_fixture("add-only.json"))
    assert [row["action"] for row in rows] == ["add", "add"]
    summary = plan_table.counts(rows)
    assert summary == {"add": 2, "change": 0, "destroy": 0, "replace": 0}
    assert plan_table.confirm_destroy_total(summary) == 0
    rendered = plan_table.render(rows)
    assert "2 to add" in rendered
    assert "Destroy total" not in rendered


def test_change_and_noop_rows_are_classified_and_filtered() -> None:
    rows = plan_table.rows(_fixture("change.json"))
    assert [row["action"] for row in rows] == ["change"]
    assert "no-op" not in plan_table.render(rows)


def test_destroy_requires_a_typed_total() -> None:
    rows = plan_table.rows(_fixture("destroy.json"))
    summary = plan_table.counts(rows)
    assert summary["destroy"] == 1
    rendered = plan_table.render(rows)
    assert "1 to destroy" in rendered
    assert "Destroy total to confirm: 1" in rendered


def test_replace_is_broken_out_and_counts_toward_the_destroy_total() -> None:
    """Regression guard: a replace must never hide inside the change count."""
    rows = plan_table.rows(_fixture("replace.json"))
    summary = plan_table.counts(rows)
    assert summary == {"add": 1, "change": 1, "destroy": 1, "replace": 1}
    assert plan_table.confirm_destroy_total(summary) == 2
    rendered = plan_table.render(rows)
    assert "1 to replace" in rendered
    assert "a replace destroys the resource and recreates it" in rendered
    assert "Destroy total to confirm: 2" in rendered
    # Destructive rows sort first so the human reads them before scrolling.
    assert [row["action"] for row in rows] == ["replace", "destroy", "change", "add"]


def test_unknown_action_shapes_get_the_destructive_assumption() -> None:
    assert plan_table.classify(["forget"]) == "replace"
    assert plan_table.classify(["create", "delete"]) == "replace"
    assert plan_table.classify(["no-op"]) is None
    assert plan_table.classify(["read"]) is None


def test_malformed_plan_fails_loud() -> None:
    with pytest.raises(plan_table.PlanTableError):
        plan_table.rows({"resource_changes": "nope"})
    with pytest.raises(plan_table.PlanTableError):
        plan_table.rows([])
    with pytest.raises(plan_table.PlanTableError):
        plan_table.rows({"resource_changes": [{"address": "x", "change": "oops"}]})
    with pytest.raises(plan_table.PlanTableError):
        plan_table.rows(
            {"resource_changes": [{"address": "x", "change": {"actions": 5}}]}
        )


def test_sparse_real_world_shapes_do_not_crash() -> None:
    assert plan_table.rows({}) == []
    assert plan_table.rows({"resource_changes": [{"address": "x"}]}) == []
    assert (
        plan_table.rows({"resource_changes": [{"address": "x", "change": {}}]}) == []
    )
    # Explicit null actions counts as no actions, same as terraform omitting them.
    assert (
        plan_table.rows(
            {"resource_changes": [{"address": "x", "change": {"actions": None}}]}
        )
        == []
    )


def test_cli_json_output_carries_the_confirm_total(tmp_path: Path, capsys) -> None:
    source = FIXTURES / "replace.json"
    assert plan_table.main([str(source), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["confirm_destroy_total"] == 2
    assert payload["counts"]["replace"] == 1
