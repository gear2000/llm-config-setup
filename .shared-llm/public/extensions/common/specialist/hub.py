#!/usr/bin/env python3
"""specialist-hub — the Librarian: deterministic routing into managed UpAgent specialists.

Herdr-native. There is no tmux and no Go message hub anywhere in this engine — the
Librarian pane lives in a `shared-services` Herdr workspace and every action goes over
the `herdr` CLI (which talks to the running Herdr over its unix socket).

Reads agents.yaml (next to this file, or $SPECIALIST_HUB_CONFIG): each entry names a
specialist, points at its definition, and declares an UpAgent harness/model/agent. Executable
launch commands remain centralized in the Recruiter roster. The engine is public; the FILLED
roster is a destination's own `this_repo` config (template: agents.yml.sample).

Topology:

    ws: shared-services            always up, plan-agnostic
    ├── librarian                  owns the routing map only
    └── recruiter                  owns manager/specialist lifecycle and cleanup

Consult protocol (files + signal, mirroring the UpAgent order/result pattern):

    caller:    write  consults/<id>.json   {consult_id, specialist, question, answer_path}
    caller:    invoke `specialist-hub consult <consults/<id>.json>` directly
    librarian: validate+route -> write an ordinary UpAgent order and cited-answer brief
    recruiter: create manager -> atomically start/verify specialist -> monitor result/deadline
    specialist: writes answer.json {consult_id, answer, citations:[file:line, ...]}
    librarian: receive durable receipt -> validate answer.json -> print "CONSULT <id> DONE"

The Librarian ALWAYS emits `CONSULT <id> DONE` once the consult_id is known — even on its error
paths, where it first writes a FAILURE answer.json ({consult_id, error}) — so a bounded wait
resolves promptly. On timeout (only if the id was unrecoverable and no sentinel was emitted) OR on
reading a failure answer.json, the caller treats the consult as failed/unanswered.

Commands:
  up                  create/attach the shared-services workspace + Librarian pane (idempotent)
  down                close the Librarian pane, remove runtime state
  status              workspace/pane health + roster size
  reindex             rewrite index.json from agents.yaml
  consult <path>      route and await one managed specialist consult

Runtime files (one directory, default /tmp/.herdr-specialist):
  state.json    {workspace, librarian_pane, repo_root} written by `up`
  index.json    roster: name -> {description, location, harness, model, agent, effort}
  consults/     where callers drop consult.json (and the Librarian writes prompt briefs)

Roster paths are portable: when `repo_root` is absent, the hub walks upward from the discovered
roster path until it finds the repository's `.git` marker. Relative specialist `location` values
are always resolved from that repository root, never from the process current directory or the
nested roster directory.

To adopt from another repo: fill agents.yaml in your repo's `this_repo` config (this engine
is synced from the kit into `.shared-llm/public/extensions/specialist/`) and import the
sibling justfile so `just specialist-hub <cmd>` runs this file.
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

WORKSPACE_LABEL = "shared-services"
# The Librarian claims ONLY a pane with this label in the shared `shared-services` workspace,
# so it and the UpAgent Recruiter (label "recruiter") coexist without fighting over each
# other's panes — regardless of which engine brought the workspace up first.
LIBRARIAN_PANE_LABEL = "librarian"
DEFAULT_RUNTIME = "/tmp/.herdr-specialist"
# How long the Librarian grants a managed specialist worker (ms).
CONSULT_TIMEOUT_MS = 600_000
UPAGENT_RECRUITER = HERE.parent / "upagent" / "recruiter.py"


# --- config -------------------------------------------------------------------


def default_config_path() -> Path:
    """Resolve the roster path. The filled roster is repo-owned, so prefer, in order:
      1. $SPECIALIST_HUB_CONFIG (explicit override);
      2. the repo-owned `this_repo` roster, if the enclosing repo has one — walk up from cwd for
         a `.shared-llm/this_repo/extensions/common/specialist/agents.yaml`;
      3. `agents.yaml` beside this engine (the kit's own adoption — editable in the kit source).
    load_config fails loud if the resolved path does not exist, so a destination that has done
    neither (1) nor (2) gets a clear error rather than silently reading a kit-owned public file.
    Mirrors the UpAgent Recruiter's default_roster_path convention.
    """
    env = os.environ.get("SPECIALIST_HUB_CONFIG")
    if env:
        return Path(env)
    for parent in [Path.cwd(), *Path.cwd().parents]:
        this_repo = parent / ".shared-llm/this_repo/extensions/common/specialist/agents.yaml"
        if this_repo.is_file():
            return this_repo
    return HERE / "agents.yaml"


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


def load_config() -> dict:
    """Read + validate the roster. Raises ConfigError (catchable) on any problem, so the consult
    path can still honor its always-signal contract when the roster is bad."""
    path = default_config_path()
    if not path.is_file():
        raise ConfigError(f"{path} not found (template: agents.yml.sample, next to this file)")
    try:
        cfg = yaml.safe_load(path.read_text()) or {}
    except yaml.YAMLError as e:
        raise ConfigError(f"{path} is not valid YAML: {e}") from e
    agents = cfg.get("agents")
    if not isinstance(agents, list) or not agents:
        raise ConfigError(f"{path} must have a non-empty 'agents:' list")
    config_path = path.resolve()
    repo_root = cfg.get("repo_root")
    cfg["repo_root"] = (
        _repo_root_from_roster(config_path)
        if repo_root is None
        else _resolve_path(repo_root, config_path.parent, "repo_root")
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

    runtime_dir = _resolve_path(cfg.get("runtime_dir", DEFAULT_RUNTIME), cfg["repo_root"], "runtime_dir")
    cfg["runtime_dir"] = runtime_dir
    cfg["config_path"] = config_path
    cfg["config_dir"] = config_path.parent
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


def write_index(cfg: dict) -> dict:
    index = {
        a["name"]: {
            "description": _description(cfg, a),
            "location": a.get("location", ""),
            "agent": a["agent"],
            "effort": a.get("effort", "medium"),
            "harness": a["harness"],
            "model": a["model"],
        }
        for a in cfg["agents"]
    }
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


def _find_shared_services() -> dict | None:
    """The shared-services WorkspaceInfo from a `herdr workspace list`, or None if absent."""
    resp = _herdr_json("workspace", "list")
    for w in resp.get("result", {}).get("workspaces", []):
        if isinstance(w, dict) and w.get("label") == WORKSPACE_LABEL and w.get("workspace_id"):
            return w
    return None


def _find_role_pane(workspace_id: str, role_label: str) -> str | None:
    """The pane_id in `workspace_id` whose label is `role_label`, or None."""
    resp = _herdr_json("pane", "list", "--workspace", workspace_id)
    for p in resp.get("result", {}).get("panes", []):
        if isinstance(p, dict) and p.get("label") == role_label and p.get("pane_id"):
            return p["pane_id"]
    return None


def _ensure_role_pane(role_label: str) -> tuple[str, str, bool]:
    """Resolve (workspace_id, pane_id, reused) for THIS engine's role pane in the ONE shared
    `shared-services` workspace, claiming ONLY a pane labeled `role_label` — never an arbitrary
    or first pane. This lets the Librarian and the Recruiter share one workspace without fighting,
    regardless of which engine started first:
      - if the workspace is absent, create it and label its root pane for my role;
      - if it exists, reuse my role-labeled pane when present, else split a fresh pane off an
        existing pane and label it for my role.
    """
    existing = _find_shared_services()
    if existing is None:
        created = _herdr_json("workspace", "create", "--label", WORKSPACE_LABEL, "--no-focus")
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
    anchor = next((p["pane_id"] for p in panes if isinstance(p, dict) and p.get("pane_id")), None)
    if anchor is None:
        sys.exit(f"error: shared-services workspace {workspace_id} has no pane to split from")
    new_pane = _dig(
        _herdr_json("pane", "split", anchor, "--direction", "down", "--no-focus"),
        "result",
        "pane",
        "pane_id",
    )
    _herdr("pane", "rename", new_pane, role_label)
    return workspace_id, new_pane, True


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
    """Build an ordinary UpAgent order; the Librarian remains routing, never lifecycle."""
    digest = hashlib.sha256(consult["consult_id"].encode()).hexdigest()[:24]
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
    result_path.unlink(missing_ok=True)
    document = json.dumps(order, indent=2, sort_keys=True) + "\n"
    temporary = order_path.with_name(f".{order_path.name}.{os.getpid()}.tmp")
    temporary.write_text(document)
    os.replace(temporary, order_path)
    return order_path, result_path


def _dispatch_specialist(order_path: Path, cwd: str) -> None:
    if not UPAGENT_RECRUITER.is_file():
        raise ConsultFailure(f"UpAgent Recruiter is missing: {UPAGENT_RECRUITER}")
    subprocess.run(
        [sys.executable, str(UPAGENT_RECRUITER), "dispatch", str(order_path)],
        check=True,
        capture_output=True,
        text=True,
        cwd=cwd,
        timeout=CONSULT_TIMEOUT_MS / 1000 + 600,
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

    # Reuse the shared workspace + my own librarian pane if they already exist (idempotent), and
    # NEVER create a duplicate shared-services workspace or claim the Recruiter's pane.
    workspace, librarian, reused = _ensure_role_pane(LIBRARIAN_PANE_LABEL)
    state = {
        "workspace": workspace,
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
    ws = _find_shared_services()
    pane = _find_role_pane(ws["workspace_id"], LIBRARIAN_PANE_LABEL) if ws else None
    if pane:
        _herdr("pane", "close", pane, check=False)
        print(f"down: closed librarian pane {pane}")
    else:
        print("down: no librarian pane found")
    state_path = paths(cfg)["state"]
    if state_path.is_file():
        state_path.unlink()


def cmd_status(cfg: dict, args: argparse.Namespace) -> None:
    _require_herdr()
    index = read_index(cfg)
    ws = _find_shared_services()
    if ws is None:
        print(f"librarian: NOT UP (no shared-services workspace) | roster: {len(index)} specialist(s)")
        return
    pane = _find_role_pane(ws["workspace_id"], LIBRARIAN_PANE_LABEL)
    state = f"alive (pane {pane})" if pane else "DOWN (no librarian pane)"
    print(f"librarian: {state} in workspace {ws['workspace_id']} | roster: {len(index)} specialist(s)")


class ConsultFailure(RuntimeError):
    """A fail-loud consult fault (unknown specialist, hub not up, herdr call failed, bad answer).
    Caught inside cmd_consult so a failure answer.json is written and the DONE sentinel is still
    emitted — the caller's bounded wait must never be left to hang."""


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

    # Load + validate the consult; if malformed, still honor the DONE contract when possible.
    try:
        consult = cc.load_consult(args.consult_path)
    except cc.ConsultError as e:
        recovered = _recover_consult_fields(args.consult_path)
        if recovered is None:
            sys.exit(f"error: unrecoverable consult.json {args.consult_path}: {e}")
        consult_id, answer_path = recovered
        _write_failure_answer(answer_path, consult_id, f"malformed consult.json: {e}")
        sys.stderr.write(f"specialist-hub: consult {consult_id} failed: malformed consult.json: {e}\n")
        print(f"CONSULT {consult_id} DONE", flush=True)
        return

    consult_id = consult["consult_id"]
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
        cwd = consult.get("cwd", state["repo_root"])

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
        _dispatch_specialist(order_path, cwd)
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
    sub.add_parser("up")
    sub.add_parser("down")
    sub.add_parser("status")
    sub.add_parser("reindex")
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
        "runtime-dir": lambda c, a: print(c["runtime_dir"]),
    }
    handlers[args.cmd](cfg, args)


if __name__ == "__main__":
    main()
