#!/usr/bin/env python3
"""specialist-hub — one long-lived hub of idle specialist agents in tmux.

Reads agents.yaml (next to this file, or $SPECIALIST_HUB_CONFIG): each entry names an
agent, its definition file, and the FULL command that runs it. No discovery
magic — the yaml is the roster. The hub is a second, long-lived instance of
the meta-orchestrator Go hub with its own discovery file; a per-plan
`hub-down` never touches it.

Commands:
  up [--agents a,b]   start hub, write index, spawn each agent's cmd in a tmux window
  down                kill the tmux session + hub, remove the discovery file
  status              hub health + window list
  reindex             rewrite index.json from agents.yaml

Runtime files (one directory, default /tmp/.meta-orch/specialist):
  hub.json    hub discovery (address, pid)
  index.json  roster: name -> {description, location, cmd}

To use from another repo, copy this WHOLE directory (the one this file lives
in) to the same relative path under the destination's .shared-llm/ tree, add
an `import '<that path>/justfile'` line to the repo's root justfile, and
write agents.yaml next to this file (template: agents.yaml.example).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.request
from pathlib import Path

import yaml

SESSION = "specialist-hub"
HERE = Path(__file__).resolve().parent


def _resolve_hub_src(here: Path) -> Path:
    """Default location of the meta-orchestrator Go hub source (holds hub.go +
    the built `hub` binary). hub.py ships inside a `.shared-llm/` tree at a
    depth that varies by repo (flat in the kit, deeper in a split-layout
    consumer) — walk up to the `.shared-llm` ancestor instead of trusting a
    fixed parent-hop count, then try the split shape (nested under `public/`)
    before the flat one, whichever exists."""
    root = here
    for d in (here, *here.parents):
        if d.name == ".shared-llm":
            root = d
            break
    rel = Path("llm/pi/common/extensions/meta-orchestrator-hub/hub")
    split, flat = root / "public" / rel, root / rel
    return split if split.is_dir() else flat


# The Go hub source sits inside the same .shared-llm tree as this file, in
# both the kit and every destination — resolved relative to wherever that
# tree's root actually is (see _resolve_hub_src).
KIT_HUB_SRC = _resolve_hub_src(HERE)
DEFAULT_RUNTIME = "/tmp/.meta-orch/specialist"


def load_config() -> dict:
    path = Path(os.environ.get("SPECIALIST_HUB_CONFIG", HERE / "agents.yaml"))
    if not path.is_file():
        sys.exit(f"error: {path} not found (template: agents.yaml.example in the kit)")
    cfg = yaml.safe_load(path.read_text()) or {}
    agents = cfg.get("agents")
    if not isinstance(agents, list) or not agents:
        sys.exit(f"error: {path} must have a non-empty 'agents:' list")
    for a in agents:
        for key in ("name", "cmd"):
            if not a.get(key):
                sys.exit(f"error: every agent needs '{key}': {a}")
    cfg["runtime_dir"] = Path(cfg.get("runtime_dir", DEFAULT_RUNTIME)).expanduser()
    cfg["hub_src"] = Path(cfg.get("hub_src", KIT_HUB_SRC)).expanduser()
    cfg["config_dir"] = path.resolve().parent
    return cfg


def paths(cfg: dict) -> dict:
    rt = cfg["runtime_dir"]
    return {"discovery": rt / "hub.json", "index": rt / "index.json"}


# --- index -------------------------------------------------------------------

def _description(cfg: dict, agent: dict) -> str:
    """One-line description: agent's own 'description', else the definition
    file's frontmatter description, else empty."""
    if agent.get("description"):
        return str(agent["description"]).strip()
    loc = agent.get("location")
    if not loc:
        return ""
    p = Path(loc)
    if not p.is_absolute():
        p = cfg["config_dir"] / p
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
    print(f"index: {len(index)} agent(s) -> {paths(cfg)['index']}")
    return index


# --- hub ---------------------------------------------------------------------

def ensure_hub(cfg: dict) -> None:
    hub_bin = cfg["hub_src"] / "hub"
    if not hub_bin.is_file():
        print("hub: building Go hub binary ...")
        subprocess.run(["go", "build", "-o", "hub", "hub.go"], cwd=cfg["hub_src"], check=True)
    # The hub self-claims its discovery file: if a healthy hub is already
    # recorded there it exits instead of starting a second one.
    subprocess.Popen(
        [str(hub_bin), "--json", str(paths(cfg)["discovery"])],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True,
    )


def hub_url(cfg: dict) -> str | None:
    f = paths(cfg)["discovery"]
    if not f.is_file():
        return None
    try:
        d = json.loads(f.read_text())
        with urllib.request.urlopen(d["url"] + "/health", timeout=2) as r:
            return d["url"] if r.status == 200 else None
    except Exception:
        return None


# --- tmux --------------------------------------------------------------------

def _tmux(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(["tmux", *args], check=check, capture_output=True, text=True)


def session_exists() -> bool:
    return _tmux("has-session", "-t", SESSION, check=False).returncode == 0


def window_names() -> list[str]:
    if not session_exists():
        return []
    return _tmux("list-windows", "-t", SESSION, "-F", "#{window_name}").stdout.split()


def spawn(name: str, cmd: str) -> None:
    if name in window_names():
        print(f"  = already running: {name}")
        return
    if not session_exists():
        _tmux("new-session", "-d", "-s", SESSION, "-n", name, cmd)
    else:
        _tmux("new-window", "-d", "-t", SESSION, "-n", name, cmd)
    print(f"  + spawned: {name}")


# --- commands ------------------------------------------------------------------

def cmd_up(cfg: dict, args: argparse.Namespace) -> None:
    index = write_index(cfg)
    ensure_hub(cfg)
    roster = list(index)
    if args.agents:
        roster = [a.strip() for a in args.agents.split(",") if a.strip()]
        unknown = [a for a in roster if a not in index]
        if unknown:
            sys.exit(f"error: not in agents.yaml: {', '.join(unknown)}")
    for name in roster:
        spawn(name, index[name]["cmd"])
    print(f"up: hub {hub_url(cfg) or 'starting'} | attach: tmux attach -t {SESSION}")


def cmd_down(cfg: dict, args: argparse.Namespace) -> None:
    if session_exists():
        _tmux("kill-session", "-t", SESSION)
        print(f"down: killed tmux session '{SESSION}'")
    f = paths(cfg)["discovery"]
    if f.is_file():
        try:
            pid = json.loads(f.read_text()).get("pid")
            if pid:
                os.kill(int(pid), 15)
                print(f"down: stopped hub (pid {pid})")
        except (ProcessLookupError, ValueError, json.JSONDecodeError):
            pass
        f.unlink()
    print("down: done (per-plan hubs untouched)")


def cmd_status(cfg: dict, args: argparse.Namespace) -> None:
    print(f"hub: {hub_url(cfg) or 'NOT RUNNING'}")
    names = window_names()
    print(f"session '{SESSION}': " + (", ".join(names) if names else "none"))


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    up = sub.add_parser("up")
    up.add_argument("--agents", help="comma list; default: every agent in agents.yaml")
    sub.add_parser("down")
    sub.add_parser("status")
    sub.add_parser("reindex")
    args = ap.parse_args()
    if shutil.which("tmux") is None:
        sys.exit("error: tmux is required")
    cfg = load_config()
    {"up": cmd_up, "down": cmd_down, "status": cmd_status,
     "reindex": lambda c, a: write_index(c)}[args.cmd](cfg, args)


if __name__ == "__main__":
    main()
