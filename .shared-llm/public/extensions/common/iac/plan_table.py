"""Build the human approval table for one IaC layer from terraform's machine-readable plan.

Feed it the output of ``terraform show -json <planfile>`` (tofu speaks the same format).
Every resource change is classified as add, change, destroy, or replace. Replace is broken
out on purpose: terraform hides it inside its change counts, and a replace destroys the
resource before recreating it. The rendered table is what the TUI shows the human before
an apply is approved; when the destroy total is above zero the human confirms by typing
that number, never just pressing y.

CLI:      python3 plan_table.py <show-json-file | -> [--json]
Library:  rows(plan) -> list, counts(rows) -> dict, confirm_destroy_total(counts) -> int,
          render(rows) -> str. Pure stdlib.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

# terraform's change.actions arrays, normalized to one human word per row.
_SIMPLE = {
    ("create",): "add",
    ("update",): "change",
    ("delete",): "destroy",
}
_REPLACE = {("create", "delete"), ("delete", "create")}
_IGNORED = {(), ("no-op",), ("read",)}

# Destructive rows sort first so the human reads the dangerous part before scrolling.
_ORDER = {"replace": 0, "destroy": 1, "change": 2, "add": 3}


class PlanTableError(ValueError):
    """The show-json input is not a terraform plan representation."""


def classify(actions: list[str]) -> str | None:
    """One word per change. None means the row is a no-op or a read and is dropped.

    An action combination this map does not recognize is treated as ``replace`` — the
    destructive assumption — so a new terraform action shape can never hide from the
    human behind a softer label.
    """
    shape = tuple(actions)
    if shape in _IGNORED:
        return None
    if shape in _SIMPLE:
        return _SIMPLE[shape]
    # Everything else — the recognized replace orderings (_REPLACE) and unknown
    # shapes alike — gets the destructive assumption.
    return "replace"


def rows(plan: dict) -> list[dict]:
    """Classified, destructive-first rows from a parsed show-json document."""
    if not isinstance(plan, dict):
        raise PlanTableError("plan json must be an object")
    changes = plan.get("resource_changes", [])
    if not isinstance(changes, list):
        raise PlanTableError("resource_changes must be a list")
    result: list[dict] = []
    for change in changes:
        if not isinstance(change, dict):
            raise PlanTableError("every resource_changes entry must be an object")
        change_block = change.get("change")
        if change_block is None:
            change_block = {}
        if not isinstance(change_block, dict):
            raise PlanTableError("resource change entry has a non-object `change`")
        actions = change_block.get("actions")
        if actions is None:
            actions = []
        if not isinstance(actions, list):
            raise PlanTableError("resource change `actions` must be a list")
        action = classify([str(item) for item in actions])
        if action is None:
            continue
        result.append(
            {
                "action": action,
                "address": str(change.get("address", "(unknown address)")),
                "actions": list(actions),
            }
        )
    result.sort(key=lambda row: (_ORDER[row["action"]], row["address"]))
    return result


def counts(table_rows: list[dict]) -> dict[str, int]:
    summary = {"add": 0, "change": 0, "destroy": 0, "replace": 0}
    for row in table_rows:
        summary[row["action"]] += 1
    return summary


def confirm_destroy_total(summary: dict[str, int]) -> int:
    """The number the human must type to approve: destroys plus replaces."""
    return summary["destroy"] + summary["replace"]


def render(table_rows: list[dict]) -> str:
    summary = counts(table_rows)
    if not table_rows:
        return (
            "No changes. The plan contains nothing to add, change, destroy, or replace."
        )
    width = max(len(row["address"]) for row in table_rows)
    lines = [f"{'ACTION':<9} RESOURCE"]
    for row in table_rows:
        raw = ""
        if row["action"] == "replace":
            raw = f"   [{', '.join(row['actions'])}]"
        lines.append(f"{row['action']:<9} {row['address']:<{width}}{raw}")
    lines.append("")
    lines.append(
        f"Plan: {summary['add']} to add, {summary['change']} to change, "
        f"{summary['destroy']} to destroy, {summary['replace']} to replace."
    )
    if summary["replace"]:
        lines.append("Note: a replace destroys the resource and recreates it.")
    total = confirm_destroy_total(summary)
    if total:
        lines.append(f"Destroy total to confirm: {total} (destroys plus replaces).")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="iac-plan-table", description=__doc__)
    parser.add_argument("show_json", help="path to `terraform show -json` output, or -")
    parser.add_argument(
        "--json", action="store_true", help="machine output instead of the table"
    )
    args = parser.parse_args(argv)
    try:
        text = (
            sys.stdin.read()
            if args.show_json == "-"
            else Path(args.show_json).read_text()
        )
        plan = json.loads(text)
        table_rows = rows(plan)
    except (OSError, json.JSONDecodeError, PlanTableError) as error:
        sys.stderr.write(f"iac-plan-table: {error}\n")
        return 2
    if args.json:
        summary = counts(table_rows)
        print(
            json.dumps(
                {
                    "rows": table_rows,
                    "counts": summary,
                    "confirm_destroy_total": confirm_destroy_total(summary),
                },
                indent=2,
                sort_keys=True,
            )
        )
    else:
        print(render(table_rows))
    return 0


if __name__ == "__main__":
    sys.exit(main())
