#!/usr/bin/env python3
"""specialist-hub — the Librarian: an always-up pane that answers repo-knowledge
questions by spawning a transient specialist per consult.

Herdr-native. There is no tmux and no Go message hub anywhere in this engine — the
Librarian pane lives in a `shared-services` Herdr workspace and every action goes over
the `herdr` CLI (which talks to the running Herdr over its unix socket).

Reads agents.yaml (next to this file, or $SPECIALIST_HUB_CONFIG): each entry names a
specialist, points at its definition file, and states the FULL command that runs it. No
discovery magic — the yaml IS the roster. The engine is public (kit-synced); the FILLED
roster is a destination's own `this_repo` config (template: agents.yaml.example).

Topology:

    ws: shared-services            always up, plan-agnostic
    └── librarian (root pane)      owns the routing map; runs `consult <path>` per question
        └── <specialist>           TRANSIENT pane, split per consult, closed after it answers

Consult protocol (files + signal, mirroring the UpAgent order/result pattern):

    caller:    write  consults/<id>.json   {consult_id, specialist, question, answer_path}
    caller:    herdr pane run <librarian> "consult <consults/<id>.json>"
    librarian: read+route the consult -> herdr pane split (transient specialist in shared-services)
    librarian: herdr pane run <specialist> "<cmd, briefed with the question + answer contract>"
    librarian: herdr wait agent-status <specialist> --status done, or polls answer_path for
               a `codex exec` specialist because Codex may never report agent-status=done
    specialist: writes answer.json {consult_id, answer, citations:[file:line, ...]}
    librarian: validate answer.json -> herdr pane close <specialist> -> print "CONSULT <id> DONE"
    caller:    herdr wait output <librarian> --match "CONSULT <id> DONE" --timeout <ms> -> read answer.json

ALWAYS bound the caller's wait with --timeout: Herdr's `wait output` blocks FOREVER without it.
The Librarian ALWAYS emits `CONSULT <id> DONE` once the consult_id is known — even on its error
paths, where it first writes a FAILURE answer.json ({consult_id, error}) — so a bounded wait
resolves promptly. On timeout (only if the id was unrecoverable and no sentinel was emitted) OR on
reading a failure answer.json, the caller treats the consult as failed/unanswered.

Commands:
  up                  create/attach the shared-services workspace + Librarian pane (idempotent)
  down                close the Librarian pane, remove runtime state
  status              workspace/pane health + roster size
  reindex             rewrite index.json from agents.yaml
  consult <path>      per-question handler run IN the Librarian pane (spawns the specialist)

Runtime files (one directory, default /tmp/.herdr-specialist):
  state.json    {workspace, librarian_pane, repo_root} written by `up`
  index.json    roster: name -> {description, location, cmd}
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
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import time

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
# How long the Librarian waits for a transient specialist to finish answering (ms).
CONSULT_TIMEOUT_MS = 600_000
ANSWER_POLL_INTERVAL_SECONDS = 0.05
# Specialist roster entries have only a command string, not a structured harness field. Codex
# does not reliably report agent-status=done, so its private answer_path is the completion signal.
CODEX_CMD_MARKER = "codex exec"


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


def load_config() -> dict:
    """Read + validate the roster. Raises ConfigError (catchable) on any problem, so the consult
    path can still honor its always-signal contract when the roster is bad."""
    path = default_config_path()
    if not path.is_file():
        raise ConfigError(f"{path} not found (template: agents.yaml.example, next to this file)")
    try:
        cfg = yaml.safe_load(path.read_text()) or {}
    except yaml.YAMLError as e:
        raise ConfigError(f"{path} is not valid YAML: {e}") from e
    agents = cfg.get("agents")
    if not isinstance(agents, list) or not agents:
        raise ConfigError(f"{path} must have a non-empty 'agents:' list")
    for a in agents:
        for key in ("name", "cmd"):
            if not a.get(key):
                raise ConfigError(f"every agent needs '{key}': {a}")
        # The cmd must state how the specialist receives its briefing prompt, so the
        # Librarian can inject the question. Fail loud now rather than spawning a
        # clueless specialist that never learns what it was asked.
        if "{prompt}" not in a["cmd"] and "{prompt_file}" not in a["cmd"]:
            raise ConfigError(
                f"agent {a['name']!r} cmd must contain '{{prompt}}' or '{{prompt_file}}' "
                f"so the Librarian can pass the question: {a['cmd']!r}"
            )

    config_path = path.resolve()
    repo_root = cfg.get("repo_root")
    cfg["repo_root"] = (
        _repo_root_from_roster(config_path)
        if repo_root is None
        else _resolve_path(repo_root, config_path.parent, "repo_root")
    )
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
            "cmd": a["cmd"],
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


def render_launch(cmd: str, prompt: str, prompt_file: Path) -> str:
    """Substitute the briefing into the roster cmd.

    ``{prompt_file}`` expands to the shell-quoted path, never the file contents. A CLI that reads
    its prompt from stdin must say so in its roster command, for example
    ``codex exec ... - < {prompt_file}``. ``{prompt}`` expands to shell-quoted inline text.
    ``load_config`` guarantees that every command contains at least one placeholder.
    """
    if "{prompt_file}" in cmd:
        cmd = cmd.replace("{prompt_file}", shlex.quote(str(prompt_file)))
    if "{prompt}" in cmd:
        cmd = cmd.replace("{prompt}", shlex.quote(prompt))
    return cmd


# --- commands -----------------------------------------------------------------


def cmd_up(cfg: dict, args: argparse.Namespace) -> None:
    _require_herdr()
    write_index(cfg)
    paths(cfg)["consults"].mkdir(parents=True, exist_ok=True)

    # Reuse the shared workspace + my own librarian pane if they already exist (idempotent), and
    # NEVER create a duplicate shared-services workspace or claim the Recruiter's pane.
    workspace, librarian, reused = _ensure_role_pane(LIBRARIAN_PANE_LABEL)
    _arm_librarian(cfg, librarian)

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
        f"{len(cfg['agents'])} specialists indexed",
        check=False,
    )
    print(f"up: {'reused' if reused else 'created'} librarian pane {librarian} in workspace {workspace}")


def _arm_librarian(cfg: dict, librarian: str) -> None:
    """Define a `consult` shell function in the Librarian pane so a caller's
    `herdr pane run <librarian> "consult <path>"` dispatches back into this engine.

    The resolved roster path is baked into the function as $SPECIALIST_HUB_CONFIG so the
    consult handler loads the SAME roster `up` did — even when the caller's environment does
    not set it (otherwise consult would fall back to HERE/agents.yaml, a different roster)."""
    hub = shlex.quote(str(Path(__file__).resolve()))
    cfg_path = shlex.quote(str(cfg["config_path"]))
    py = shlex.quote(os.environ.get("PYTHON_BIN", "python3"))
    fn = f'consult() {{ SPECIALIST_HUB_CONFIG={cfg_path} {py} {hub} consult "$1"; }}'
    _herdr("pane", "run", librarian, fn)


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


def _wait_for_answer_path(answer_path: Path, timeout_ms: int) -> None:
    """Wait for a Codex specialist's private answer file when agent-status never reaches done."""
    deadline = time.monotonic() + timeout_ms / 1000
    while time.monotonic() < deadline:
        if answer_path.is_file():
            return
        time.sleep(ANSWER_POLL_INTERVAL_SECONDS)
    raise ConsultFailure(f"timed out waiting for {answer_path} to appear")


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
    """Per-question handler — runs IN the Librarian pane. Route the consult to a specialist, spawn
    it as a transient pane, wait for its answer, validate it, close the pane, signal done.

    Guarantee (mirrors the Recruiter's always-emit-ORDER-DONE): whenever the consult_id is known,
    this ALWAYS writes an answer.json (real or failure) and emits `CONSULT <id> DONE`, even on the
    error paths — so the caller's bounded `wait output --timeout` resolves promptly rather than
    only via timeout. Failures are still logged to stderr (fail-loud, nothing swallowed).

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
    specialist_pane: str | None = None
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
        prompt_file.write_text(prompt + "\n")

        # Remove any stale answer.json from a prior consult at this path BEFORE launching, so the
        # only answer we can read back is the fresh one this specialist writes.
        Path(consult["answer_path"]).unlink(missing_ok=True)

        # Spawn the transient specialist as a pane split off the Librarian, running in the repo.
        split = _herdr("pane", "split", librarian, "--direction", "down", "--cwd", cwd, "--no-focus")
        try:
            pane_id = json.loads(split.stdout)["result"]["pane"]["pane_id"]
        except (json.JSONDecodeError, KeyError, TypeError) as e:
            raise ConsultFailure(f"herdr pane split returned unexpected output: {e}") from e
        if not isinstance(pane_id, str) or not pane_id:
            raise ConsultFailure(f"herdr pane split returned invalid pane_id: {pane_id!r}")
        specialist_pane = pane_id

        launch = render_launch(entry["cmd"], prompt, prompt_file)
        _herdr("pane", "run", pane_id, launch)
        if CODEX_CMD_MARKER in entry["cmd"]:
            _wait_for_answer_path(Path(consult["answer_path"]), CONSULT_TIMEOUT_MS)
        else:
            _herdr(
                "wait",
                "agent-status",
                pane_id,
                "--status",
                "done",
                "--timeout",
                str(CONSULT_TIMEOUT_MS),
            )
        # The specialist must have written a valid answer.json echoing this consult_id.
        cc.load_answer(consult["answer_path"], expected_consult_id=consult_id)
    except (
        ConsultFailure,
        ConfigError,
        cc.ConsultError,
        OSError,
        subprocess.CalledProcessError,
        KeyError,
        TypeError,
    ) as e:
        _write_failure_answer(consult["answer_path"], consult_id, str(e))
        sys.stderr.write(f"specialist-hub: consult {consult_id} failed: {e}\n")
    finally:
        if specialist_pane is not None:
            try:
                _herdr("pane", "close", specialist_pane, check=False)
            except OSError as e:
                # A fork/exec fault closing a pane must not skip the CONSULT DONE emit below.
                sys.stderr.write(f"specialist-hub: could not close pane {specialist_pane}: {e}\n")

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
