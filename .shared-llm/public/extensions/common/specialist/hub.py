#!/usr/bin/env python3
"""specialist-hub — the Librarian: deterministic routing into managed UpAgent specialists.

Herdr-native. There is no tmux and no Go message hub anywhere in this engine — the
Librarian pane lives in a `shared-services` Herdr workspace and every action goes over
the `herdr` CLI (which talks to the running Herdr over its unix socket).

Reads TWO rosters and merges them: the kit's BASE agents.yaml (next to this file, synced into
every destination) plus the destination's own `this_repo` agents.yaml overlay. Same-named overlay
entries clobber base entries; everything else is the union, so a repo gets the kit's generic
specialists AND its own. $SPECIALIST_HUB_CONFIG remains a single-file override (no merge). Each
entry names a specialist, points at its definition, and declares an UpAgent harness/model/agent.
Executable launch commands remain centralized in the Recruiter roster.

Topology (default: everything shares ONE `herdr` workspace; `up --separate-workspaces`
restores the dedicated `shared-services` workspace):

    ws: herdr                      single-workspace default
    └── tab: services              ├── recruiter · └── librarian
    (runs add control/workers/oversight tabs to the same workspace)

    ws: shared-services            with --separate-workspaces: services-only workspace
    ├── librarian                  owns the routing map only
    └── recruiter                  owns manager/specialist lifecycle and cleanup

Consult protocol (files + signal, mirroring the UpAgent order/result pattern):

    caller:    write  consults/<id>.json   {consult_id, specialist, question, answer_path}
    caller:    invoke `specialist-hub consult <consults/<id>.json>` directly
    librarian: validate+route -> write an ordinary UpAgent order and cited-answer brief
    recruiter: atomically start/verify specialist (direct lifecycle) -> monitor result/deadline
    specialist: writes answer.json {consult_id, answer, citations:[file:line, ...]}
    librarian: receive durable receipt -> validate answer.json -> print "CONSULT <id> DONE"

The Librarian ALWAYS emits `CONSULT <id> DONE` once the consult_id is known — even on its error
paths, where it first writes a FAILURE answer.json ({consult_id, error}) — so a bounded wait
resolves promptly. On timeout (only if the id was unrecoverable and no sentinel was emitted) OR on
reading a failure answer.json, the caller treats the consult as failed/unanswered.

Commands:
  up                  create/attach the services workspace + Librarian pane (idempotent);
                      --separate-workspaces keeps services in their own workspace
  down                close the Librarian pane, remove runtime state
  status              workspace/pane health + merged roster size
  reindex             rewrite index.json from the merged rosters
  roster              print the merged roster as a paste-ready stage-brief block (--json for raw)
  consult <path>      route and await one managed specialist consult

Runtime files (one directory, default /tmp/.herdr-specialist):
  state.json    {workspace, workspace_label, librarian_pane, repo_root} written by `up`
                `repo_root` here is a record of where `up` ran and is INFORMATIONAL ONLY —
                never start a process in it. It goes stale the moment that directory is a
                worktree someone deleted. Use `cfg["repo_root"]`, re-derived on every load.
  index.json    roster: name -> {description, location, harness, model, agent, effort, origin}
  consults/     where callers drop consult.json (and the Librarian writes prompt briefs)

Roster paths are portable: when `repo_root` is absent, the hub walks upward from the discovered
roster path until it finds the repository's `.git` marker. Relative specialist `location` values
are always resolved from that repository root, never from the process current directory or the
nested roster directory.

To adopt from another repo: import the sibling justfile so `just specialist-hub <cmd>` runs this
file. The kit's base roster works as-is; add repo-owned specialists (or clobber base ones by
name) in `.shared-llm/this_repo/extensions/common/specialist/agents.yaml`.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys

import yaml

HERE = Path(__file__).resolve().parent
# Import the consult/answer contracts once (stable module + exception identity, so failures can
# be caught by type). Path-imported like the sibling Recruiter imports its `contracts`.
sys.path.insert(0, str(HERE))
import contracts_consult as cc  # noqa: E402  (sibling module, path-imported)
import intake  # noqa: E402  (sibling module, path-imported — the forgiving intake ladder)

# Single-workspace default: services (and every run's tabs) share one `herdr` workspace.
# `up --separate-workspaces` restores the dedicated services-only workspace.
UNIFIED_WORKSPACE_LABEL = "herdr"
SEPARATE_WORKSPACE_LABEL = "shared-services"
SERVICES_TAB_LABEL = "services"
# The Librarian claims ONLY a pane with this label in the shared services workspace,
# so it and the UpAgent Recruiter (label "recruiter") coexist without fighting over each
# other's panes — regardless of which engine brought the workspace up first.
LIBRARIAN_PANE_LABEL = "librarian"
RECRUITER_PANE_LABEL = "recruiter"
DEFAULT_RUNTIME = "/tmp/.herdr-specialist"
# How long the Librarian grants a managed specialist worker (ms).
CONSULT_TIMEOUT_MS = 600_000
# The intake clerk is a quick normalization pass, not research — a short lease keeps a
# wedged clerk from stalling the refusal it exists to improve.
INTAKE_TIMEOUT_MS = 300_000
# Roster-overridable via a top-level `intake:` mapping in agents.yaml (overlay wins).
DEFAULT_INTAKE_PROFILE = {
    "harness": "claude",
    "model": "sonnet",
    "agent": "intake-clerk",
    "effort": "low",
}
UPAGENT_RECRUITER = HERE.parent / "upagent" / "recruiter.py"


# --- config -------------------------------------------------------------------


def config_paths() -> tuple[Path | None, Path, str]:
    """Resolve (base, primary, primary_origin) roster paths. The effective roster is base merged
    under primary — primary entries clobber same-named base entries, everything else is the union:
      1. $SPECIALIST_HUB_CONFIG set — a single-file override, no merge (base is None);
      2. a repo-owned `this_repo` overlay — walk up from cwd for
         `.shared-llm/this_repo/extensions/common/specialist/agents.yaml` — merged ON TOP of the
         kit base `agents.yaml` beside this engine (kit-synced into every destination);
      3. no overlay — the kit base alone.
    load_config fails loud if the primary path does not exist. The walk-up mirrors the UpAgent
    Recruiter's default_roster_path convention.
    """
    env = os.environ.get("SPECIALIST_HUB_CONFIG")
    if env:
        return None, Path(env), "override"
    base = HERE / "agents.yaml"
    for parent in [Path.cwd(), *Path.cwd().parents]:
        this_repo = parent / ".shared-llm/this_repo/extensions/common/specialist/agents.yaml"
        if this_repo.is_file():
            return (base if base.is_file() else None), this_repo, "this-repo"
    return None, base, "kit-base"


class ConfigError(RuntimeError):
    """A bad roster (missing/invalid agents.yaml). Raised fail-loud; the consult path catches it
    to still leave a failure answer + emit DONE, other commands convert it to a clean exit."""


def _resolve_path(value: object, base: Path, field: str) -> Path:
    """Resolve a configured path against `base`, rejecting non-string path values."""
    if not isinstance(value, str) or not value:
        raise ConfigError(f"{field} must be a non-empty string (got {value!r})")
    path = Path(value).expanduser()
    return path.resolve() if path.is_absolute() else (base / path).resolve()


def _repo_root_from_roster(path: Path) -> Path:
    """Find the repository containing a discovered roster without consulting the current directory."""
    for candidate in (path.parent, *path.parent.parents):
        if (candidate / ".git").exists():
            return candidate
    raise ConfigError(f"could not find a repository root above roster {path}: no .git marker")


def _model_from_compose(repo_root: Path, agent_name: str) -> str | None:
    """Read an agent model from the repo's compose recipe during legacy roster migration."""
    for recipe in (
        repo_root / ".shared-llm/this_repo/compose/agents" / f"{agent_name}.yaml",
        repo_root / ".shared-llm/public/compose/agents" / f"{agent_name}.yaml",
    ):
        if not recipe.is_file():
            continue
        try:
            data = yaml.safe_load(recipe.read_text()) or {}
        except yaml.YAMLError as e:
            raise ConfigError(f"{recipe} is not valid YAML: {e}") from e
        model = data.get("model") if isinstance(data, dict) else None
        if isinstance(model, str) and model:
            return model
    return None


def _legacy_agent_from_cmd(cmd: object, default: str) -> str:
    """Extract `--agent <name>` from the retired direct Claude command format."""
    if not isinstance(cmd, str) or not cmd:
        return default
    parts = cmd.split()
    for idx, part in enumerate(parts):
        if part == "--agent" and idx + 1 < len(parts) and parts[idx + 1]:
            return parts[idx + 1]
    return default


def _normalize_legacy_agent(repo_root: Path, agent: dict) -> None:
    """Upgrade the retired `cmd:` Specialist roster shape in memory.

    Old destination-owned rosters listed direct Claude commands. The current hub routes all work
    through UpAgent, so it needs harness/model/agent fields. If the matching compose recipe omits a
    model because routing chooses it later, keep model as an empty string; UpAgent accepts that.
    """
    if not agent.get("cmd"):
        return
    name = agent.get("name")
    if not isinstance(name, str) or not name:
        return
    cmd = agent["cmd"]
    if not isinstance(cmd, str) or not cmd.strip().startswith("claude "):
        return
    agent.setdefault("harness", "claude")
    agent.setdefault("agent", _legacy_agent_from_cmd(cmd, name))
    model = _model_from_compose(repo_root, name)
    agent.setdefault("model", model or "")


def _read_roster_file(path: Path) -> dict:
    """One roster document: a YAML object with a non-empty `agents:` list. Fail-loud per file."""
    try:
        cfg = yaml.safe_load(path.read_text()) or {}
    except yaml.YAMLError as e:
        raise ConfigError(f"{path} is not valid YAML: {e}") from e
    if not isinstance(cfg, dict):
        raise ConfigError(f"{path} must be a YAML object with an 'agents:' list")
    agents = cfg.get("agents")
    if not isinstance(agents, list) or not agents:
        raise ConfigError(f"{path} must have a non-empty 'agents:' list")
    return cfg


def _named_entries(agents: list, origin: str, path: Path) -> dict[str, dict]:
    """Ordered name -> entry copies tagged with their roster of origin. Merging is by name, so an
    unnamed entry is unmergeable and fails loud here with its source file."""
    entries: dict[str, dict] = {}
    for a in agents:
        if not isinstance(a, dict):
            raise ConfigError(f"every agent entry must be an object: {a!r} (in {path})")
        name = a.get("name")
        if not isinstance(name, str) or not name:
            raise ConfigError(f"every agent needs a non-empty 'name': {a} (in {path})")
        entries[name] = {**a, "origin": origin}
    return entries


def load_config() -> dict:
    """Read + validate + merge the rosters (kit base under the repo overlay; overlay entries
    clobber same-named base entries). Raises ConfigError (catchable) on any problem, so the
    consult path can still honor its always-signal contract when a roster is bad."""
    base_path, primary_path, primary_origin = config_paths()
    if not primary_path.is_file():
        raise ConfigError(
            f"{primary_path} not found (template: agents.yml.sample, next to this file)"
        )
    primary = _read_roster_file(primary_path)
    base = _read_roster_file(base_path) if base_path is not None else None

    named = _named_entries(primary["agents"], primary_origin, primary_path)
    overridden: list[str] = []
    cfg = dict(primary)
    if base is not None and base_path is not None:
        base_named = _named_entries(base["agents"], "kit-base", base_path)
        overridden = sorted(set(base_named) & set(named))
        base_named.update(named)  # overlay clobbers same-named base entries, appends new ones
        named = base_named
        # Overlay scalars (runtime_dir, repo_root, ...) win over base scalars.
        cfg = {**{k: v for k, v in base.items() if k != "agents"}, **cfg}
    agents = list(named.values())
    cfg["agents"] = agents
    cfg["overridden"] = overridden

    config_path = primary_path.resolve()
    repo_root = primary.get("repo_root")
    repo_root_anchor = config_path.parent
    if repo_root is None and base is not None and base.get("repo_root") is not None:
        repo_root = base.get("repo_root")
        repo_root_anchor = base_path.resolve().parent if base_path is not None else repo_root_anchor
    cfg["repo_root"] = (
        _repo_root_from_roster(config_path)
        if repo_root is None
        else _resolve_path(repo_root, repo_root_anchor, "repo_root")
    )
    for a in agents:
        if not isinstance(a, dict):
            raise ConfigError(f"every agent entry must be an object: {a!r}")
        _normalize_legacy_agent(cfg["repo_root"], a)
        for key in ("name", "harness", "agent"):
            if not isinstance(a.get(key), str) or not a[key]:
                extra = (
                    "; legacy cmd-only entries must either add harness/agent or be a "
                    "recognizable direct Claude command"
                    if a.get("cmd")
                    else ""
                )
                raise ConfigError(f"every agent needs a non-empty '{key}': {a}{extra}")
        if not isinstance(a.get("model"), str):
            raise ConfigError(f"every agent needs a string 'model' (may be empty): {a}")
        if a["harness"] not in ("claude", "codex", "pi", "cursor"):
            raise ConfigError(f"agent {a['name']!r} has unsupported harness {a['harness']!r}")
        if "effort" in a and (not isinstance(a["effort"], str) or not a["effort"]):
            raise ConfigError(f"agent {a['name']!r} effort must be a non-empty string")

    intake_profile = cfg.get("intake")
    if intake_profile is not None:
        if not isinstance(intake_profile, dict) or not all(
            isinstance(k, str) and isinstance(v, str) for k, v in intake_profile.items()
        ):
            raise ConfigError("`intake:` must be a mapping of string settings when present")
        intake_harness = intake_profile.get("harness")
        if intake_harness is not None and intake_harness not in ("claude", "codex", "pi", "cursor"):
            raise ConfigError(f"intake harness {intake_harness!r} is unsupported")

    runtime_dir = _resolve_path(cfg.get("runtime_dir", DEFAULT_RUNTIME), cfg["repo_root"], "runtime_dir")
    cfg["runtime_dir"] = runtime_dir
    cfg["config_path"] = config_path
    cfg["config_dir"] = config_path.parent
    cfg["base_config_path"] = base_path.resolve() if base_path is not None else None
    return cfg


def resolve_specialist_location(cfg: dict, location: str) -> Path:
    """Resolve a specialist definition location from the configured repository root."""
    return _resolve_path(location, cfg["repo_root"], "agent location")


def load_config_or_exit() -> dict:
    """Fail-loud config load for the non-consult commands: a bad roster is a clean hard error."""
    try:
        return load_config()
    except ConfigError as e:
        sys.exit(f"error: {e}")


def paths(cfg: dict) -> dict:
    rt = cfg["runtime_dir"]
    return {"state": rt / "state.json", "index": rt / "index.json", "consults": rt / "consults"}


# --- index --------------------------------------------------------------------


def _description(cfg: dict, agent: dict) -> str:
    """One-line description: agent's own 'description', else the definition file's
    frontmatter description, else empty."""
    if agent.get("description"):
        return str(agent["description"]).strip()
    loc = agent.get("location")
    if not loc:
        return ""
    p = resolve_specialist_location(cfg, str(loc))
    if not p.is_file():
        return ""
    m = re.match(r"\A---\n(.*?)\n---\n", p.read_text(), re.DOTALL)
    if not m:
        return ""
    try:
        fm = yaml.safe_load(m.group(1))
    except yaml.YAMLError:
        return ""
    desc = (fm or {}).get("description", "") if isinstance(fm, dict) else ""
    return str(desc).strip().split("\n")[0]


def _build_index(cfg: dict) -> dict:
    return {
        a["name"]: {
            "description": _description(cfg, a),
            "location": a.get("location", ""),
            "agent": a["agent"],
            "effort": a.get("effort", "medium"),
            "harness": a["harness"],
            "model": a["model"],
            "origin": a.get("origin", ""),
        }
        for a in cfg["agents"]
    }


def write_index(cfg: dict) -> dict:
    index = _build_index(cfg)
    cfg["runtime_dir"].mkdir(parents=True, exist_ok=True)
    paths(cfg)["index"].write_text(json.dumps(index, indent=2) + "\n")
    print(f"index: {len(index)} specialist(s) -> {paths(cfg)['index']}")
    return index


def read_index(cfg: dict) -> dict:
    p = paths(cfg)["index"]
    if not p.is_file():
        return write_index(cfg)
    try:
        return json.loads(p.read_text())
    except json.JSONDecodeError:
        return write_index(cfg)


# --- herdr socket -------------------------------------------------------------


def _require_herdr() -> None:
    if os.environ.get("HERDR_ENV") != "1":
        sys.exit("error: specialist-hub is Herdr-native; run it inside a Herdr pane (HERDR_ENV=1)")
    if shutil.which("herdr") is None:
        sys.exit("error: `herdr` CLI not found on PATH")


def _herdr(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(["herdr", *args], check=check, capture_output=True, text=True)


def _herdr_json(*args: str) -> dict:
    """Run a herdr command that returns JSON on stdout; fail loud on a non-JSON body."""
    cp = _herdr(*args)
    try:
        return json.loads(cp.stdout)
    except json.JSONDecodeError as e:
        sys.exit(f"error: herdr {' '.join(args)} did not return JSON: {e}\n{cp.stdout}\n{cp.stderr}")


def _dig(obj: dict, *keys: str) -> str:
    """Pull a nested value out of a herdr JSON result, failing loud if the shape drifts."""
    cur: object = obj
    for k in keys:
        if not isinstance(cur, dict) or k not in cur:
            sys.exit(f"error: herdr JSON missing {'.'.join(keys)}: {json.dumps(obj)[:400]}")
        cur = cur[k]
    if not isinstance(cur, str) or not cur:
        sys.exit(f"error: herdr JSON {'.'.join(keys)} not a non-empty string: {cur!r}")
    return cur


def _find_workspace(label: str) -> dict | None:
    """The WorkspaceInfo labeled `label` from a `herdr workspace list`, or None if absent."""
    resp = _herdr_json("workspace", "list")
    for w in resp.get("result", {}).get("workspaces", []):
        if isinstance(w, dict) and w.get("label") == label and w.get("workspace_id"):
            return w
    return None


def _find_services_workspace() -> dict | None:
    """The live services workspace under either mode's label, preferring the unified default."""
    for label in (UNIFIED_WORKSPACE_LABEL, SEPARATE_WORKSPACE_LABEL):
        found = _find_workspace(label)
        if found is not None:
            return found
    return None


def _find_role_pane(workspace_id: str, role_label: str) -> str | None:
    """The pane_id in `workspace_id` whose label is `role_label`, or None."""
    resp = _herdr_json("pane", "list", "--workspace", workspace_id)
    for p in resp.get("result", {}).get("panes", []):
        if isinstance(p, dict) and p.get("label") == role_label and p.get("pane_id"):
            return p["pane_id"]
    return None


def _ensure_role_pane(role_label: str, workspace_label: str) -> tuple[str, str, bool]:
    """Resolve (workspace_id, pane_id, reused) for THIS engine's role pane in the ONE services
    workspace (`workspace_label`), claiming ONLY a pane labeled `role_label` — never an arbitrary
    or first pane. This lets the Librarian and the Recruiter share one workspace without fighting,
    regardless of which engine started first:
      - if services are already up under the OTHER mode's label, fail loud (run `just herdr-down`
        first) rather than splitting the services across two workspaces;
      - if the workspace is absent, create it and label its root pane for my role;
      - if it exists, reuse my role-labeled pane when present, else split a fresh pane off an
        existing pane and label it for my role.
    """
    other_label = (
        SEPARATE_WORKSPACE_LABEL
        if workspace_label == UNIFIED_WORKSPACE_LABEL
        else UNIFIED_WORKSPACE_LABEL
    )
    other = _find_workspace(other_label)
    if other is not None and any(
        _find_role_pane(other["workspace_id"], role)
        for role in (LIBRARIAN_PANE_LABEL, RECRUITER_PANE_LABEL)
    ):
        sys.exit(
            f"error: services are already up in workspace {other_label!r}; "
            "run `just herdr-down` first to switch workspace modes"
        )
    existing = _find_workspace(workspace_label)
    if existing is None:
        created = _herdr_json("workspace", "create", "--label", workspace_label, "--no-focus")
        # Herdr returns nested objects (ResponseResult::WorkspaceCreated), not bare ids.
        workspace_id = _dig(created, "result", "workspace", "workspace_id")
        pane_id = _dig(created, "result", "root_pane", "pane_id")
        _herdr("pane", "rename", pane_id, role_label)
        return workspace_id, pane_id, False

    workspace_id = existing["workspace_id"]
    mine = _find_role_pane(workspace_id, role_label)
    if mine is not None:
        return workspace_id, mine, True

    panes = _herdr_json("pane", "list", "--workspace", workspace_id).get("result", {}).get("panes", [])
    # Prefer splitting beside the Recruiter pane so services stay together in one tab even when
    # the unified workspace already holds run panes.
    anchor = next(
        (
            p["pane_id"]
            for p in panes
            if isinstance(p, dict) and p.get("label") == RECRUITER_PANE_LABEL and p.get("pane_id")
        ),
        None,
    ) or next((p["pane_id"] for p in panes if isinstance(p, dict) and p.get("pane_id")), None)
    if anchor is None:
        sys.exit(f"error: services workspace {workspace_id} has no pane to split from")
    new_pane = _dig(
        _herdr_json("pane", "split", anchor, "--direction", "down", "--no-focus"),
        "result",
        "pane",
        "pane_id",
    )
    _herdr("pane", "rename", new_pane, role_label)
    return workspace_id, new_pane, True


def _label_services_tab(workspace_id: str, pane_id: str) -> None:
    """Best-effort: label the tab holding the services panes `services`, so the unified-workspace
    sidebar reads services / control / workers / oversight. Skips the rename when the tab also
    holds non-service panes (e.g. a tui-agent, when bring-up ran in an unusual order).
    Presentation-only — a failure warns and never breaks bring-up."""
    try:
        cp = _herdr("pane", "list", "--workspace", workspace_id, check=False)
        panes = (
            json.loads(cp.stdout).get("result", {}).get("panes", [])
            if cp.returncode == 0
            else []
        )
        tab_id = next(
            (
                p.get("tab_id")
                for p in panes
                if isinstance(p, dict) and p.get("pane_id") == pane_id
            ),
            None,
        )
        if not isinstance(tab_id, str) or not tab_id:
            return
        service_labels = (LIBRARIAN_PANE_LABEL, RECRUITER_PANE_LABEL)
        foreign = [
            p
            for p in panes
            if isinstance(p, dict)
            and p.get("tab_id") == tab_id
            and p.get("label") not in (*service_labels, None, "")
        ]
        if not foreign:
            _herdr("tab", "rename", tab_id, SERVICES_TAB_LABEL, check=False)
    except (OSError, json.JSONDecodeError) as e:
        sys.stderr.write(f"specialist-hub: could not label the services tab: {e}\n")


def _report_librarian_state(pane: str, agent_state: str, message: str) -> None:
    """Best-effort sidebar truth for the Librarian pane — `working` while a consult routes,
    `idle` with a served tally after — so the TUI distinguishes a working Librarian from a
    bypassed one. Status display must never break the consult contract."""
    try:
        _herdr(
            "pane",
            "report-agent",
            pane,
            "--source",
            "specialist-librarian",
            "--agent",
            "librarian",
            "--state",
            agent_state,
            "--message",
            message,
            check=False,
        )
    except OSError as e:
        sys.stderr.write(f"specialist-hub: could not update librarian sidebar state: {e}\n")


def _report_librarian_idle(cfg: dict) -> None:
    """Refresh the Librarian sidebar to idle with a served tally. Silent no-op when the hub
    state is missing (consult before `up`)."""
    try:
        state = _read_state(cfg)
    except ConsultFailure:
        return
    # Clerk hires write clerk-*.upagent-result.json; the tally counts real specialist
    # consults only (mechanically repaired ones, prefixed intake-, still count).
    served = len(
        [
            p
            for p in paths(cfg)["consults"].glob("*.upagent-result.json")
            if not p.name.startswith("clerk-")
        ]
    )
    _report_librarian_state(
        state["librarian_pane"],
        "idle",
        f"{served} consult(s) served — send consults with 'just specialist-hub consult "
        "<consult.json>'; never paste text into this pane",
    )


def _recruiter_consult_token() -> str | None:
    """The consult token the Recruiter issued at its `up` (read from the Recruiter's own state
    file). The Librarian stamps it on every order it authors so the Recruiter can tell a
    brokered consult from a hand-written imitation. None when the Recruiter state or token is
    absent (older Recruiter `up`); the stamp is then simply omitted."""
    state_path = Path(
        os.environ.get("UPAGENT_STATE", "/tmp/.upagent/recruiter.json")
    ).expanduser()
    try:
        state = json.loads(state_path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    token = state.get("consult_token") if isinstance(state, dict) else None
    return token if isinstance(token, str) and token else None


# --- specialist briefing ------------------------------------------------------


def build_prompt(consult: dict, location: str, cwd: str) -> str:
    """The briefing a transient specialist reads: the question, where its own definition and the
    repo live, and the exact answer.json contract it must satisfy (answer + file:line citations).
    """
    return (
        f"You are the '{consult['specialist']}' specialist answering ONE consult. "
        f"Load only your own definition at {location} and inspect the repo at {cwd}. "
        f"Question: {consult['question']} "
        f"Answer concisely from your domain, then write STRICT JSON to {consult['answer_path']} "
        f'with keys: "consult_id": "{consult["consult_id"]}", "answer": "<your answer>", '
        f'"citations": ["path/to/file:line", ...]. Every claim MUST carry a real file:line '
        f"citation into the repo. Write nothing outside that JSON file."
    )


def _specialist_order(
    cfg: dict, consult: dict, entry: dict, prompt_file: Path, librarian: str, cwd: str
) -> tuple[Path, Path]:
    """Build an ordinary UpAgent order; the Librarian remains routing, never lifecycle.

    Consults follow the roster's default lifecycle — the deterministic Python direct mode,
    which already proves process/agent/cwd startup and monitors result/deadline. No
    per-consult dedicated manager is pinned: that historical broker duplicated the Python
    checks with an idle LLM pane per question. A roster that truly wants managers can still
    opt in globally via its `management` configuration.
    """
    # The Recruiter rotates its consult token on every `up`. Fold that generation into the
    # request identity so retrying a previously failed consult after Hub restart cannot collide
    # with the old ledger entry. Retries within one generation remain idempotent.
    token = _recruiter_consult_token()
    identity = consult["consult_id"] + (f"\0{token}" if token is not None else "")
    digest = hashlib.sha256(identity.encode()).hexdigest()[:24]
    order_path = paths(cfg)["consults"] / f"{consult['consult_id']}.order.json"
    result_path = paths(cfg)["consults"] / f"{consult['consult_id']}.upagent-result.json"
    order = {
        "order_id": f"specialist-consult-{digest}",
        "request_id": f"specialist-{digest}",
        "requester": {
            "id": "specialist-librarian",
            "kind": "file-mailbox",
            "address": str(cfg["runtime_dir"] / "librarian-inbox"),
        },
        "phase_id": "specialist-consult",
        "stage_id": "stage-5-finalization",
        "harness": entry["harness"],
        "model": entry["model"],
        "agent": entry["agent"],
        "effort": entry.get("effort", "medium"),
        "cwd": cwd,
        "instructions_path": str(prompt_file),
        "result_path": str(result_path),
        "cockpit_pane": librarian,
        "timeout_ms": CONSULT_TIMEOUT_MS,
    }
    if token is not None:
        order["consult_token"] = token
    result_path.unlink(missing_ok=True)
    document = json.dumps(order, indent=2, sort_keys=True) + "\n"
    temporary = order_path.with_name(f".{order_path.name}.{os.getpid()}.tmp")
    temporary.write_text(document)
    os.replace(temporary, order_path)
    return order_path, result_path


def _dispatch_specialist(order_path: Path, cwd: str) -> None:
    if not UPAGENT_RECRUITER.is_file():
        raise ConsultFailure(f"UpAgent Recruiter is missing: {UPAGENT_RECRUITER}")
    proc = subprocess.run(
        [sys.executable, str(UPAGENT_RECRUITER), "dispatch", str(order_path)],
        check=False,
        capture_output=True,
        text=True,
        cwd=cwd,
        timeout=CONSULT_TIMEOUT_MS / 1000 + 600,
    )
    if proc.returncode != 0:
        detail = proc.stderr.strip() or proc.stdout.strip() or "no diagnostic output"
        raise ConsultFailure(
            f"UpAgent dispatch failed for {order_path.name} (exit {proc.returncode}): {detail}"
        )


def _librarian_status_message(agent_count: int) -> str:
    """The honest sidebar label: the Librarian is a broker mailbox, never a chat pane.

    Workers read this label in the TUI. It must name the real consult door so nobody
    pastes a question into the pane, where a plain shell would eat it.
    """
    return (
        f"broker mailbox ({agent_count} specialists indexed) — send consults with "
        "'just specialist-hub consult <consult.json>'; never paste text into this pane"
    )


# --- commands -----------------------------------------------------------------


def cmd_up(cfg: dict, args: argparse.Namespace) -> None:
    _require_herdr()
    write_index(cfg)
    paths(cfg)["consults"].mkdir(parents=True, exist_ok=True)

    # Reuse the services workspace + my own librarian pane if they already exist (idempotent), and
    # NEVER create a duplicate services workspace or claim the Recruiter's pane.
    separate = bool(getattr(args, "separate_workspaces", False))
    workspace_label = SEPARATE_WORKSPACE_LABEL if separate else UNIFIED_WORKSPACE_LABEL
    workspace, librarian, reused = _ensure_role_pane(LIBRARIAN_PANE_LABEL, workspace_label)
    if not separate:
        _label_services_tab(workspace, librarian)
    state = {
        "workspace": workspace,
        "workspace_label": workspace_label,
        "separate_workspaces": separate,
        "librarian_pane": librarian,
        "repo_root": str(cfg["repo_root"]),
    }
    paths(cfg)["state"].write_text(json.dumps(state, indent=2) + "\n")
    # Surface the broker in Herdr's agents sidebar so "up" is visible, not just a shell.
    # BEST-EFFORT: status display must never break bring-up.
    _herdr(
        "pane",
        "report-agent",
        librarian,
        "--source",
        "specialist-librarian",
        "--agent",
        "librarian",
        "--state",
        "idle",
        "--message",
        _librarian_status_message(len(cfg["agents"])),
        check=False,
    )
    print(f"up: {'reused' if reused else 'created'} librarian pane {librarian} in workspace {workspace}")


def cmd_down(cfg: dict, args: argparse.Namespace) -> None:
    _require_herdr()
    # Close ONLY the librarian pane (found by label), never the shared workspace — the Recruiter
    # may still own its own pane there. Prefer the live label lookup over stale state.
    ws = _find_services_workspace()
    pane = _find_role_pane(ws["workspace_id"], LIBRARIAN_PANE_LABEL) if ws else None
    if pane:
        _herdr("pane", "close", pane, check=False)
        print(f"down: closed librarian pane {pane}")
    else:
        print("down: no librarian pane found")
    state_path = paths(cfg)["state"]
    if state_path.is_file():
        state_path.unlink()


def _roster_summary(index: dict) -> str:
    """`N specialist(s) (K kit-base + M this-repo)` — the merged-roster shape at a glance."""
    base = sum(1 for e in index.values() if isinstance(e, dict) and e.get("origin") == "kit-base")
    repo = sum(1 for e in index.values() if isinstance(e, dict) and e.get("origin") == "this-repo")
    detail = f" ({base} kit-base + {repo} this-repo)" if base and repo else ""
    return f"{len(index)} specialist(s){detail}"


def cmd_status(cfg: dict, args: argparse.Namespace) -> None:
    _require_herdr()
    index = read_index(cfg)
    ws = _find_services_workspace()
    if ws is None:
        print(f"librarian: NOT UP (no services workspace) | roster: {_roster_summary(index)}")
        return
    pane = _find_role_pane(ws["workspace_id"], LIBRARIAN_PANE_LABEL)
    state = f"alive (pane {pane})" if pane else "DOWN (no librarian pane)"
    print(f"librarian: {state} in workspace {ws['workspace_id']} | roster: {_roster_summary(index)}")


def cmd_roster(cfg: dict, args: argparse.Namespace) -> None:
    """Print the merged roster. Default: a paste-ready stage-brief block (the phone book) that a
    phase leader embeds VERBATIM in every worker's instructions.md; --json: the raw merged index."""
    index = _build_index(cfg)
    if getattr(args, "json", False):
        print(json.dumps(index, indent=2))
        return
    consults_dir = paths(cfg)["consults"]
    lines = [
        "## Repo specialists — consult before deciding (MANDATORY where a specialist owns the area)",
        "",
        "Conventions are asked, never guessed. Before deciding anything in an area listed below,",
        "consult its specialist through the Librarian. Record every consult in your result.json",
        "`consults` list (empty list when none applied); a skipped mandated consult is a blocking",
        "audit finding.",
        "",
    ]
    for name, entry in index.items():
        origin = " (this repo)" if entry.get("origin") == "this-repo" else ""
        # The block rides inside EVERY stage brief: one line per specialist, hard-capped, so a
        # roster whose descriptions are whole agent preambles cannot bloat the briefs.
        description = str(entry.get("description") or "(no description)").strip().split("\n")[0]
        if len(description) > 220:
            description = description[:217].rstrip() + "..."
        lines.append(f"- **{name}**{origin} — {description}")
    lines += [
        "",
        "How to consult (files + signal; NEVER paste a question into any pane):",
        f"1. Write {consults_dir}/<consult-id>.json with:",
        '   {"consult_id": "<unique-id>", "specialist": "<name above>",',
        '    "question": "<one specific question>", "answer_path": "<absolute path for answer.json>"}',
        "2. Run `just specialist-hub consult <that consult.json>` and wait (bounded) for the",
        "   sentinel `CONSULT <id> DONE` — emitted even on failure, after a failure answer.json.",
        "3. Read answer_path: a success answer carries file:line citations; a failure carries `error`.",
        "4. Record {consult_id, specialist, answer_path} under `consults` in your result.json.",
    ]
    print("\n".join(lines))


class ConsultFailure(RuntimeError):
    """A fail-loud consult fault (unknown specialist, hub not up, herdr call failed, bad answer).
    Caught inside cmd_consult so a failure answer.json is written and the DONE sentinel is still
    emitted — the caller's bounded wait must never be left to hang."""


def _persist_intake(
    consults_dir: Path, raw_path: Path, consult: dict, mode: str, changes: list[str]
) -> None:
    """Write the normalized consult + the intake stamp beside the submission. The
    interpretation is ALWAYS written down — forgiveness with a paper trail."""
    consults_dir.mkdir(parents=True, exist_ok=True)
    normalized_path = consults_dir / f"{consult['consult_id']}.normalized.json"
    normalized_path.write_text(json.dumps(consult, indent=2, sort_keys=True) + "\n")
    record = intake.intake_record(
        mode=mode,
        raw_path=str(raw_path),
        normalized_path=str(normalized_path),
        changes=changes,
    )
    (consults_dir / f"{consult['consult_id']}.intake.json").write_text(
        json.dumps(record, indent=2, sort_keys=True) + "\n"
    )
    print(
        f"intake: {mode} normalized {raw_path} -> {normalized_path} "
        f"({len(changes)} change(s))",
        flush=True,
    )


def _hire_intake_clerk(cfg: dict, raw_text: str) -> dict:
    """One managed clerk hire through the same door as every consult — leased,
    token-stamped, visible pane. Returns the strictly parsed clerk document. Raises on any
    fault so the ladder degrades to a refusal instead of hanging."""
    state = _read_state(cfg)
    profile = {**DEFAULT_INTAKE_PROFILE, **(cfg.get("intake") or {})}
    tag = intake.generate_clerk_tag()
    consults = paths(cfg)["consults"]
    consults.mkdir(parents=True, exist_ok=True)
    prompt_file = consults / f"{tag}.prompt.txt"
    output_path = consults / f"{tag}.clerk.json"
    output_path.unlink(missing_ok=True)
    prompt_file.write_text(
        intake.clerk_brief(raw_text, _build_index(cfg), str(output_path))
        + "\nAfter the JSON file is durable, satisfy the Recruiter delivery contract "
        "appended to this brief and exit.\n"
    )
    order = {
        "order_id": f"specialist-{tag}",
        "request_id": f"specialist-{tag}",
        "requester": {
            "id": "specialist-librarian",
            "kind": "file-mailbox",
            "address": str(cfg["runtime_dir"] / "librarian-inbox"),
        },
        "phase_id": "specialist-intake",
        "stage_id": "stage-5-finalization",
        "harness": profile["harness"],
        "model": str(profile.get("model", "")),
        "agent": profile["agent"],
        "effort": str(profile.get("effort", "low")),
        "cwd": str(cfg["repo_root"]),
        "instructions_path": str(prompt_file),
        "result_path": str(consults / f"{tag}.upagent-result.json"),
        "cockpit_pane": state["librarian_pane"],
        "timeout_ms": INTAKE_TIMEOUT_MS,
    }
    token = _recruiter_consult_token()
    if token is not None:
        order["consult_token"] = token
    order_path = consults / f"{tag}.order.json"
    document = json.dumps(order, indent=2, sort_keys=True) + "\n"
    temporary = order_path.with_name(f".{order_path.name}.{os.getpid()}.tmp")
    temporary.write_text(document)
    os.replace(temporary, order_path)
    _report_librarian_state(
        state["librarian_pane"], "working", f"intake {tag}: normalizing a submission"
    )
    _dispatch_specialist(order_path, str(cfg["repo_root"]))
    return intake.parse_clerk_output(output_path.read_text())


def _refuse_intake(
    raw_path: Path, strict_error: cc.ConsultError, clerk_error: str | None
) -> None:
    """Ladder step 4: refuse HELPFULLY — say what was understood, what is missing, and what
    a valid consult looks like. Honors the always-signal contract: a failure answer lands at
    the caller's answer_path when one was recoverable, else a refusal file beside the raw
    submission; the DONE sentinel prints either way."""
    message = (
        f"intake refusal: could not understand {raw_path} as a consult. "
        f"Strict parse said: {strict_error}. "
        + (f"Intake clerk said: {clerk_error}. " if clerk_error else "")
        + "A consult needs: consult_id, specialist (a phone-book name), question, and an "
        "absolute answer_path. Submit one consult per specialist with "
        "`just specialist-hub consult <consult.json>`."
    )
    recovered = _recover_consult_fields(str(raw_path))
    if recovered is not None:
        consult_id, answer_path = recovered
        _write_failure_answer(answer_path, consult_id, message)
    else:
        consult_id = intake.generate_consult_id()
        try:
            raw_path.with_name(raw_path.name + ".refusal.json").write_text(
                json.dumps(
                    cc.failure_answer(consult_id, message), indent=2, sort_keys=True
                )
                + "\n"
            )
        except OSError as e:
            sys.stderr.write(f"specialist-hub: could not write the refusal file: {e}\n")
    sys.stderr.write(f"specialist-hub: {message}\n")
    print(f"CONSULT {consult_id} DONE", flush=True)


def _forgiving_intake(
    consult_path: str, strict_error: cc.ConsultError
) -> dict | None:
    """Walk a failed submission down the intake ladder. Returns a valid consult dict to
    continue with, or None after a refusal/failure was durably written and the DONE
    sentinel printed — the caller's bounded wait resolves on every path."""
    raw_path = Path(consult_path)
    try:
        raw_text = raw_path.read_text()
    except OSError:
        sys.exit(f"error: unrecoverable consult.json {consult_path}: {strict_error}")

    cfg: dict | None
    try:
        cfg = load_config()
    except (ConfigError, OSError) as e:
        sys.stderr.write(f"specialist-hub: intake continues without a roster: {e}\n")
        cfg = None

    roster_names = [a["name"] for a in cfg["agents"]] if cfg is not None else []
    consults_dir = paths(cfg)["consults"] if cfg is not None else raw_path.parent
    clerk_error: str | None = None
    intake_fault: str | None = None
    try:
        repaired = intake.mechanical_repair(
            raw_text, roster_names=roster_names, consults_dir=consults_dir
        )
        if repaired is not None:
            consult, changes = repaired
            _persist_intake(consults_dir, raw_path, consult, "mechanical-repair", changes)
            return consult

        if cfg is not None:
            try:
                clerk = _hire_intake_clerk(cfg, raw_text)
            except (
                ConsultFailure,
                ConfigError,
                OSError,
                subprocess.CalledProcessError,
                subprocess.TimeoutExpired,
                ValueError,
                KeyError,
            ) as e:
                sys.stderr.write(f"specialist-hub: intake clerk unavailable: {e}\n")
                clerk = None
            if clerk is not None and "consult" in clerk:
                try:
                    consult = cc.parse_consult(json.dumps(clerk["consult"]))
                except cc.ConsultError as e:
                    clerk_error = f"clerk produced an invalid consult: {e}"
                else:
                    changes = [
                        "normalized by the intake clerk from a free-form submission"
                    ]
                    if not Path(str(consult["answer_path"])).is_absolute():
                        consult["answer_path"] = str(
                            consults_dir / str(consult["answer_path"])
                        )
                        changes.append(
                            "anchored the clerk's relative answer_path at "
                            f"{consult['answer_path']}"
                        )
                    _persist_intake(
                        consults_dir, raw_path, consult, "intake-clerk", changes
                    )
                    return consult
            elif clerk is not None:
                clerk_error = clerk["error"]
    except OSError as e:
        # A failed intake WRITE (disk full, permissions) must degrade to a refusal —
        # never escape past the always-signal contract and leave the caller hanging.
        intake_fault = f"intake could not persist its interpretation: {e}"
        sys.stderr.write(f"specialist-hub: {intake_fault}\n")

    _refuse_intake(raw_path, strict_error, clerk_error or intake_fault)
    return None


def _recover_consult_fields(consult_path: str) -> tuple[str, str] | None:
    """Best-effort (consult_id, answer_path) from a malformed consult.json, so the Librarian can
    still leave a failure answer + emit DONE instead of stranding the caller. Returns None if the
    file is too broken to recover either field. Mirrors the Recruiter's _recover_order_fields."""
    try:
        raw = json.loads(Path(consult_path).read_text())
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(raw, dict):
        return None
    consult_id, answer_path = raw.get("consult_id"), raw.get("answer_path")
    if isinstance(consult_id, str) and consult_id and isinstance(answer_path, str) and answer_path:
        return consult_id, answer_path
    return None


def _write_failure_answer(answer_path: str, consult_id: str, reason: str) -> None:
    """Leave a legible FAILURE answer.json so the caller reads a clear failure instead of a stale
    or missing file. Best-effort: a filesystem fault here must not skip the DONE emission (the
    caller's bounded wait then falls back to treating the consult as failed anyway)."""
    try:
        p = Path(answer_path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(cc.failure_answer(consult_id, f"specialist-hub: {reason}"), indent=2))
    except OSError as e:
        sys.stderr.write(f"specialist-hub: could not write failure answer for {consult_id}: {e}\n")


def _resolve_consult_cwd(cfg: dict, consult: dict, consult_id: str) -> str:
    """Pick a directory the specialist can actually be started in.

    The rule is deliberately dull: run in the repo the roster came from, unless the caller
    named a directory that exists. `cfg["repo_root"]` is re-derived on every config load from
    a path the running hub process resolved, so it is live by construction.

    What this replaces: the hub used to inherit the repo_root frozen into services state at
    `up` time. Bring services up inside a throwaway worktree, delete the worktree, and every
    consult afterwards died starting a process in a directory that no longer existed. Consults
    are read-and-cite work; there is no reason for them to be pinned to a transient checkout.
    """
    requested = consult.get("cwd")
    if requested and Path(requested).is_dir():
        return str(requested)
    root = str(cfg["repo_root"])
    if requested:
        sys.stderr.write(
            f"specialist-hub: consult {consult_id}: requested cwd {requested} does not "
            f"exist; running in {root}\n"
        )
    return root


def cmd_consult(args: argparse.Namespace) -> None:
    """Route one question into an ordinary managed UpAgent order and validate its answer.

    Guarantee: whenever the consult_id is known,
    this ALWAYS writes an answer.json (real or failure) and emits `CONSULT <id> DONE`, even on the
    error paths — so a direct caller receives a terminal signal instead of a silent return.
    Failures are still logged to stderr (fail-loud, nothing swallowed).

    This handler loads its OWN config (rather than taking a pre-loaded cfg) so that a bad roster
    fails INSIDE the recoverable block — a recoverable consult must be answerable even when the
    roster is missing/invalid, or the caller would only ever resolve on timeout.
    """
    _require_herdr()

    # Load + validate the consult; a near-miss walks the forgiving intake ladder
    # (mechanical repair -> intake clerk -> helpful refusal) before anything is refused.
    try:
        consult = cc.load_consult(args.consult_path)
    except cc.ConsultError as strict_error:
        consult = _forgiving_intake(args.consult_path, strict_error)
        if consult is None:
            return  # the intake wrote its refusal/failure and printed the DONE sentinel

    consult_id = consult["consult_id"]
    cfg: dict | None = None
    try:
        # Everything that can fail lives INSIDE this block, now that consult_id is known — INCLUDING
        # config load — so a bad roster / unknown specialist / herdr fault still writes a failure
        # answer + emits DONE rather than stranding the caller.
        cfg = load_config()
        index = read_index(cfg)
        specialist = consult["specialist"]
        if specialist not in index:
            raise ConsultFailure(f"unknown specialist {specialist!r}; roster: {', '.join(index) or '(empty)'}")
        entry = index[specialist]

        state = _read_state(cfg)
        librarian = state["librarian_pane"]
        _report_librarian_state(
            librarian, "working", f"consult {consult_id} -> {specialist}: routing to a managed specialist"
        )
        cwd = _resolve_consult_cwd(cfg, consult, consult_id)

        location = entry.get("location") or "(no definition file)"
        if entry.get("location"):
            location = str(resolve_specialist_location(cfg, entry["location"]))

        prompt = build_prompt(consult, location, cwd)
        prompt_file = paths(cfg)["consults"] / f"{consult_id}.prompt.txt"
        prompt_file.write_text(
            prompt
            + "\nAfter the cited answer is durable, satisfy the Recruiter delivery contract appended "
            "to this brief and exit.\n"
        )

        # Remove any stale answer.json from a prior consult at this path BEFORE launching, so the
        # only answer we can read back is the fresh one this specialist writes.
        Path(consult["answer_path"]).unlink(missing_ok=True)

        order_path, _ = _specialist_order(cfg, consult, entry, prompt_file, librarian, cwd)
        # The worker runs in the consult's requested cwd because that cwd is sealed in the
        # order. The Recruiter process itself starts in the roster repository so its default
        # UpAgent roster can always be resolved, even when the consult inspects another repo.
        _dispatch_specialist(order_path, str(cfg["repo_root"]))
        # The specialist must have written a valid answer.json echoing this consult_id.
        cc.load_answer(consult["answer_path"], expected_consult_id=consult_id)
    except (
        ConsultFailure,
        ConfigError,
        cc.ConsultError,
        OSError,
        subprocess.CalledProcessError,
        subprocess.TimeoutExpired,
        KeyError,
        TypeError,
    ) as e:
        _write_failure_answer(consult["answer_path"], consult_id, str(e))
        sys.stderr.write(f"specialist-hub: consult {consult_id} failed: {e}\n")
    if cfg is not None:
        _report_librarian_idle(cfg)
    # The go/done signal the caller waits on; the answer.json (real or failure) is the durable
    # record. Emitted on EVERY path above so a bounded caller wait always resolves.
    print(f"CONSULT {consult_id} DONE", flush=True)


def _read_state(cfg: dict) -> dict:
    """Read the runtime state written by `up`, raising ConsultFailure (catchable) if it is missing
    or corrupt — so a consult run before `up` still signals the caller instead of hard-exiting."""
    p = paths(cfg)["state"]
    if not p.is_file():
        raise ConsultFailure(f"hub not up (no {p}); run `just specialist-hub up` first")
    try:
        return json.loads(p.read_text())
    except json.JSONDecodeError as e:
        raise ConsultFailure(f"corrupt state file {p}: {e}") from e


# --- entrypoint ---------------------------------------------------------------


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    up = sub.add_parser("up")
    up.add_argument(
        "--separate-workspaces",
        action="store_true",
        help="keep services in their own `shared-services` workspace instead of the unified `herdr` one",
    )
    sub.add_parser("down")
    sub.add_parser("status")
    sub.add_parser("reindex")
    roster = sub.add_parser(
        "roster", help="print the merged roster as a paste-ready stage-brief block"
    )
    roster.add_argument("--json", action="store_true", help="print the raw merged index as JSON")
    sub.add_parser("runtime-dir", help="print the configured Specialist Hub runtime directory")
    con = sub.add_parser("consult")
    con.add_argument("consult_path", help="path to the consult.json to answer")
    args = ap.parse_args()

    # `consult` loads its OWN config inside its recoverable block (a bad roster must not strand a
    # recoverable consult), so it is dispatched WITHOUT a pre-loaded cfg. Every other command
    # fails loud up front on a bad roster.
    if args.cmd == "consult":
        cmd_consult(args)
        return
    cfg = load_config_or_exit()
    handlers = {
        "up": cmd_up,
        "down": cmd_down,
        "status": cmd_status,
        "reindex": lambda c, a: write_index(c),
        "roster": cmd_roster,
        "runtime-dir": lambda c, a: print(c["runtime_dir"]),
    }
    handlers[args.cmd](cfg, args)


if __name__ == "__main__":
    main()
