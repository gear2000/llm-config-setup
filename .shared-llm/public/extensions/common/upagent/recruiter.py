#!/usr/bin/env python3
"""UpAgent Recruiter — the always-up broker that hires a fresh worker per work order.

The Recruiter is a pane in the `shared-services` Herdr workspace. The phase leader places
an order by writing `order.json` and signaling the Recruiter's pane:

    herdr pane run <recruiter-pane> "just upagent recruit <path/to/order.json>"

The Recruiter then, for that one order:
  1. reads + validates the order (contracts.py, fail-loud);
  2. resolves the per-harness launch template from the roster (upagent.yaml);
  3. splits a fresh worker pane from the order's cockpit pane (into the cockpit), with the
     order's cwd (the phase worktree) and env (optional OTel vars);
  4. runs the worker, then blocks on `herdr wait agent-status <worker> --status done`;
  5. reads + validates the worker's result.json (must echo the order_id);
  6. closes the worker pane;
  7. emits `ORDER <order_id> DONE` — the signal the leader waits on.

The RESULT FILE is the source of truth; the `ORDER ... DONE` line is only the accelerator
the leader matches on. If anything goes wrong (herdr error, timeout, missing/bad result),
the Recruiter still writes a fail-loud `blocked` result.json and emits `ORDER ... DONE`, so
the leader is never stranded — it reads the blocked verdict and escalates per its budget.

route.yaml is authoritative for which harness/model/agent runs each stage; the Recruiter
only knows HOW to launch each harness. It never picks the agent.

Pure stdlib + PyYAML (as the sibling specialist hub uses). No Go hub, no tmux — Herdr only.
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import contracts  # noqa: E402  (sibling module, path-imported)

SHARED_SERVICES_WORKSPACE = "shared-services"
DEFAULT_TIMEOUT_MS = 1_800_000  # 30 min per worker unless the order overrides
# Where `up` records the resolved workspace + Recruiter pane so `status`/callers can find it.
STATE_FILE = Path(os.environ.get("UPAGENT_STATE", "/tmp/.upagent/recruiter.json")).expanduser()


def default_roster_path() -> str:
    """Resolve the launch-template roster. The filled roster is repo-owned, so prefer, in order:
      1. $UPAGENT_CONFIG (explicit override);
      2. the repo-owned `this_repo` roster, if the enclosing repo has one — walk up from cwd for
         a `.shared-llm/` dir and look under `.shared-llm/this_repo/extensions/common/upagent/`;
      3. `upagent.yaml` beside this engine (the kit's own adoption — editable in the kit source).
    `load_roster` fails loud if the resolved path does not exist, so a destination that has done
    neither (1) nor (2) gets a clear error rather than silently reading a kit-owned public file.
    Mirrors the Specialist Hub's SPECIALIST_HUB_CONFIG convention.
    """
    env = os.environ.get("UPAGENT_CONFIG")
    if env:
        return env
    for parent in [Path.cwd(), *Path.cwd().parents]:
        this_repo = parent / ".shared-llm/this_repo/extensions/common/upagent/upagent.yaml"
        if this_repo.is_file():
            return str(this_repo)
    return str(HERE / "upagent.yaml")
# Placeholders a launch template may use. The template author decides how each harness
# consumes them; the Recruiter only substitutes.
TEMPLATE_FIELDS = ("model", "agent", "cwd", "instructions_path", "result_path")


class RecruiterError(RuntimeError):
    """A fail-loud Recruiter fault (bad roster, missing herdr, herdr call failed)."""


# --- pure, unit-testable core ------------------------------------------------


def load_roster(path: str | Path) -> dict:
    """Read + validate the launch-template roster (upagent.yaml). Fail-loud.

    Shape:
        harnesses:
          claude: "<launch template with {placeholders}>"
          codex:  "..."
    """
    p = Path(path)
    if not p.is_file():
        raise RecruiterError(f"roster not found: {p} (template: upagent.yaml.example)")
    try:
        data = yaml.safe_load(p.read_text()) or {}
    except (OSError, yaml.YAMLError) as e:
        # Surface an unreadable file or invalid YAML as a RecruiterError so cmd_recruit's
        # fallback catches it (a blocked result + DONE) instead of it escaping past main().
        raise RecruiterError(f"roster {p} is unreadable or invalid YAML: {e}") from e
    harnesses = data.get("harnesses")
    if not isinstance(harnesses, dict) or not harnesses:
        raise RecruiterError(f"{p} must define a non-empty `harnesses:` map")
    for name, tmpl in harnesses.items():
        if not isinstance(tmpl, str) or not tmpl.strip():
            raise RecruiterError(f"{p} harness `{name}` must map to a non-empty template string")
    return data


def resolve_launch_command(order: dict, roster: dict) -> str:
    """Substitute an order's fields into its harness launch template. Pure. Fail-loud on an
    unknown harness or a template referencing an unknown placeholder."""
    harness = order["harness"]
    template = roster.get("harnesses", {}).get(harness)
    if template is None:
        raise RecruiterError(
            f"no launch template for harness {harness!r} in the roster; "
            f"add it under harnesses: (have: {', '.join(roster.get('harnesses', {}))})"
        )
    fields = {k: order.get(k, "") for k in TEMPLATE_FIELDS}
    try:
        return template.format(**fields)
    except KeyError as e:
        raise RecruiterError(
            f"harness {harness!r} template references unknown placeholder {e}; "
            f"allowed: {', '.join('{%s}' % f for f in TEMPLATE_FIELDS)}"
        ) from e


# --- herdr runtime helpers ---------------------------------------------------


def _herdr_available() -> None:
    if shutil.which("herdr") is None:
        raise RecruiterError("`herdr` not found in PATH — the Recruiter runs inside Herdr")


def _herdr_json(*args: str) -> dict:
    """Run a herdr subcommand expected to print JSON; return the parsed object. Fail-loud."""
    _herdr_available()
    proc = subprocess.run(["herdr", *args], capture_output=True, text=True)
    if proc.returncode != 0:
        raise RecruiterError(f"herdr {' '.join(args)} failed: {proc.stderr.strip()}")
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError as e:
        raise RecruiterError(f"herdr {' '.join(args)} did not print JSON: {proc.stdout[:200]}") from e


def _herdr(*args: str) -> None:
    """Run a herdr subcommand that prints nothing on success. Fail-loud on non-zero."""
    _herdr_available()
    proc = subprocess.run(["herdr", *args], capture_output=True, text=True)
    if proc.returncode != 0:
        raise RecruiterError(f"herdr {' '.join(args)} failed: {proc.stderr.strip()}")


def _write_blocked_result(order: dict, reason: str) -> None:
    """Ensure a result.json exists so the leader is never stranded. Only writes a fallback
    `blocked` result when the worker did not leave a valid one of its own.

    BEST-EFFORT and never raises: it runs from cmd_recruit's except path, so a filesystem fault
    here must not escape (which would skip the `ORDER <id> DONE` emission). If it truly cannot
    write, the leader's bounded `wait output --timeout` falls back to treating the stage as
    blocked anyway."""
    try:
        result_path = Path(order["result_path"])
        if result_path.is_file():
            try:
                contracts.parse_result(
                    result_path.read_text(), expected_order_id=order["order_id"]
                )
                return  # worker left a valid result — keep it
            except (contracts.ContractError, OSError):
                pass  # unreadable/invalid/stale → fall through and overwrite with a blocked result
        result_path.parent.mkdir(parents=True, exist_ok=True)
        # Only name a stage in `revisit` when it is a recognized one (a malformed order may carry
        # no valid stage_id); an unrecognized stage would fail result re-validation.
        stage = order.get("stage_id")
        revisit = [stage] if stage in contracts.RECOGNIZED_STAGE_IDS else []
        result_path.write_text(
            json.dumps(
                {
                    "order_id": order["order_id"],
                    "verdict": "blocked",
                    "revisit": revisit,
                    "reason": f"recruiter: {reason}",
                    "full_log": "(none — worker did not run to completion)",
                },
                indent=2,
            )
        )
    except OSError as e:
        sys.stderr.write(f"recruiter: could not write blocked result for {order.get('order_id')}: {e}\n")


# --- commands ----------------------------------------------------------------


def _recover_order_fields(order_path: str) -> tuple[str, str] | None:
    """Best-effort (order_id, result_path) from a malformed order.json, so the Recruiter can
    still leave a blocked result + emit DONE instead of stranding the leader. Returns None if
    the file is too broken to recover either field."""
    try:
        raw = json.loads(Path(order_path).read_text())
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(raw, dict):
        return None
    order_id, result_path = raw.get("order_id"), raw.get("result_path")
    if isinstance(order_id, str) and order_id and isinstance(result_path, str) and result_path:
        return order_id, result_path
    return None


def cmd_recruit(order_path: str, roster_path: str) -> int:
    """Hire one worker for one order. Emits `ORDER <id> DONE` whenever the order_id is known (a
    result.json always exists after that). Returns 0 on a clean hire, 1 on a blocked fallback,
    2 when the order is too malformed to even recover an id (leader falls back on its timeout)."""
    try:
        order = contracts.load_order(order_path)
    except contracts.ContractError as e:
        # Malformed order: try to still honor the DONE contract so the leader is not stranded.
        recovered = _recover_order_fields(order_path)
        if recovered is None:
            sys.stderr.write(f"recruiter: unrecoverable order {order_path}: {e}\n")
            return 2
        order_id, result_path = recovered
        _write_blocked_result(
            {"order_id": order_id, "result_path": result_path, "stage_id": "unknown"},
            f"malformed order.json: {e}",
        )
        print(f"ORDER {order_id} DONE", flush=True)
        return 1
    order_id = order["order_id"]
    fell_back = False
    worker_pane: str | None = None
    try:
        # Everything that can fail lives INSIDE the fallback block, now that order_id is known, so
        # a bad roster / launch / Herdr call still writes a blocked result and emits DONE rather
        # than raising past main() and stranding the leader.
        roster = load_roster(roster_path)
        # Remove any stale result.json from a prior try at this path BEFORE launching, so the only
        # result we can read is the one this worker writes (never a leftover from an earlier try).
        Path(order["result_path"]).unlink(missing_ok=True)
        launch = resolve_launch_command(order, roster)
        # `herdr pane split` splits an EXISTING pane; the order carries the cockpit pane to
        # split the worker from (there is no --workspace on split). This places the worker in
        # the cockpit beside the phase leader, per the topology.
        split_args = [
            "pane", "split", order["cockpit_pane"],
            "--direction", "right", "--no-focus",
            "--cwd", order["cwd"],
        ]
        for k, v in (order.get("env") or {}).items():
            split_args += ["--env", f"{k}={v}"]
        split = _herdr_json(*split_args)
        worker_pane = split["result"]["pane"]["pane_id"]

        _herdr("pane", "run", worker_pane, launch)
        timeout = str(order.get("timeout_ms", DEFAULT_TIMEOUT_MS))
        _herdr("wait", "agent-status", worker_pane, "--status", "done", "--timeout", timeout)

        # The worker must have written a valid result.json echoing this order_id.
        contracts.load_result(order["result_path"], expected_order_id=order_id)
    except (RecruiterError, contracts.ContractError, KeyError, TypeError, OSError) as e:
        # KeyError/TypeError guard Herdr JSON shape drift; OSError guards filesystem faults (e.g.
        # a result_path that is a dir, or a permission error on unlink) — all still write a blocked
        # result rather than a silent exit that strands the leader.
        _write_blocked_result(order, str(e))
        fell_back = True
        sys.stderr.write(f"recruiter: order {order_id} fell back to blocked: {e}\n")
    finally:
        if worker_pane is not None:
            try:
                _herdr("pane", "close", worker_pane)
            except (RecruiterError, OSError):
                pass  # closing a gone pane (or a fork/exec fault) must not skip the DONE emit
    # The accelerator signal the leader waits on. The RESULT FILE is the real verdict.
    print(f"ORDER {order_id} DONE", flush=True)
    return 1 if fell_back else 0


def _find_shared_services(workspaces_resp: dict) -> dict | None:
    """The shared-services WorkspaceInfo from a `workspace list` response, or None."""
    for w in workspaces_resp.get("result", {}).get("workspaces", []):
        if w.get("label") == SHARED_SERVICES_WORKSPACE:
            return w
    return None


RECRUITER_PANE_LABEL = "recruiter"


def _ensure_role_pane(role_label: str) -> tuple[str, str, bool]:
    """Resolve (workspace_id, pane_id, reused) for THIS engine's role pane in the single shared
    `shared-services` workspace, claiming ONLY a pane labeled `role_label` — never an arbitrary
    pane. This lets the Recruiter and the Librarian share one workspace without fighting over each
    other's panes, regardless of which engine started first:
      - create the shared-services workspace if it is absent, and label its root pane for my role;
      - if it exists, reuse my role-labeled pane if present, else split a fresh pane off an
        existing one and label it for my role.
    """
    existing = _find_shared_services(_herdr_json("workspace", "list"))
    if existing is None:
        created = _herdr_json(
            "workspace", "create", "--label", SHARED_SERVICES_WORKSPACE, "--no-focus"
        )["result"]
        workspace_id = created["workspace"]["workspace_id"]
        pane_id = created["root_pane"]["pane_id"]
        _herdr("pane", "rename", pane_id, role_label)
        return workspace_id, pane_id, False

    workspace_id = existing["workspace_id"]
    panes = _herdr_json("pane", "list", "--workspace", workspace_id).get("result", {}).get("panes", [])
    mine = next((p for p in panes if p.get("label") == role_label and p.get("pane_id")), None)
    if mine is not None:
        return workspace_id, mine["pane_id"], True
    anchor = next((p["pane_id"] for p in panes if p.get("pane_id")), None)
    if anchor is None:
        raise RecruiterError(f"shared-services workspace {workspace_id} has no pane to split from")
    new_pane = _herdr_json("pane", "split", anchor, "--direction", "down", "--no-focus")[
        "result"
    ]["pane"]["pane_id"]
    _herdr("pane", "rename", new_pane, role_label)
    return workspace_id, new_pane, True


def cmd_up(roster_path: str) -> int:
    """Ensure the shared-services workspace + an armed Recruiter pane. Idempotent.

    Arms a `recruit` shell function in the Recruiter pane so the phase leader can signal it with
    `herdr pane run <recruiter> "recruit <order.json>"`. The resolved roster path is baked into
    that function, so the Recruiter always hires against the intended (repo-owned) roster.
    Persists {workspace_id, recruiter_pane, roster} to STATE_FILE and prints it.
    """
    # Validate the roster up front so a missing/bad roster fails loudly at bring-up, not silently
    # at the first hire (the armed recruit() bakes this path in).
    load_roster(roster_path)
    workspace_id, recruiter_pane, reused = _ensure_role_pane(RECRUITER_PANE_LABEL)

    # Bake the resolved roster into the armed function so every hire uses the right roster.
    # shlex.quote every interpolated token so paths with spaces/metacharacters can't break arming.
    arm = (
        f"recruit() {{ {shlex.quote(sys.executable)} {shlex.quote(str(Path(__file__).resolve()))} "
        f"--roster {shlex.quote(roster_path)} recruit \"$1\"; }}"
    )
    _herdr("pane", "run", recruiter_pane, arm)

    state = {"workspace_id": workspace_id, "recruiter_pane": recruiter_pane, "roster": roster_path}
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2))
    print(json.dumps({**state, "reused": reused}))
    return 0


def cmd_status() -> int:
    up = _find_shared_services(_herdr_json("workspace", "list")) is not None
    print(f"shared-services: {'up' if up else 'down'}")
    if STATE_FILE.is_file():
        print(STATE_FILE.read_text())
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="recruiter", description="UpAgent Recruiter")
    parser.add_argument(
        "--roster",
        default=default_roster_path(),
        help="launch-template roster (default: $UPAGENT_CONFIG, else upagent.yaml next to this file)",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    p_recruit = sub.add_parser("recruit", help="hire a worker for one order.json")
    p_recruit.add_argument("order", help="path to order.json")
    sub.add_parser("up", help="ensure the shared-services workspace")
    sub.add_parser("status", help="report shared-services state")

    args = parser.parse_args(argv)
    try:
        if args.command == "recruit":
            return cmd_recruit(args.order, args.roster)
        if args.command == "up":
            return cmd_up(args.roster)
        if args.command == "status":
            return cmd_status()
    except RecruiterError as e:
        sys.exit(f"recruiter: {e}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
