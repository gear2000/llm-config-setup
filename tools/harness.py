#!/usr/bin/env python3
"""Assemble layer .md files into skill, agent, CLAUDE.md, and AGENTS.md definitions.

Reads compose YAML files from <shared-llm>/compose/ that specify which layer
markdown files to concatenate, and produces output .md files with proper
formatting for the target type.

Two roots are decoupled:
    SOURCE  — the .shared-llm/ dir (layers + compose recipes). Inputs, catalog,
              description, and recipe discovery resolve against it. Selected via
              --shared-llm, then $SHARED_LLM_DIR, then a walk-up for `.shared-llm/`.
    TARGET  — the output base (where `output:` paths land). Selected via --target;
              defaults to the current working directory.

Compose YAML types:
    type: skill      — YAML frontmatter with name + description (default if no type)
    type: agent      — YAML frontmatter with name + description (+ optional model/color)
    type: claude-md  — plain concatenated markdown, no frontmatter (CLAUDE.md)
    type: agents-md  — plain concatenated markdown, no frontmatter (AGENTS.md)
    type: prompt     — plain concatenated markdown, no frontmatter; a whole
                       feature prompt assembled from an explicit manifest of
                       layer fragments (credentials/CI pointer, the per-feature
                       context, the chosen agent role) for a systemPrompt harness
    type: copy       — one source file copied verbatim (executable bit preserved)
                       to the output. For code that lives in the layer tree but
                       is not composable prose: hook scripts, statusline.sh.
    type: settings   — JSON inputs deep-merged left-to-right (dicts recurse,
                       lists concatenate, scalars overlay-win). For settings.json
                       assembled from a common base + a this_repo overlay.

Optional keys:
    catalog: <path>  — shared partial injected FIRST before 'inputs' (single-source for
                       content repeated across many outputs, e.g. the service catalog).
                       Exactly one source file; the tool reads it once and prepends it.

This is the ONE engine and it lives ONLY in the llm-config-setup kit — it is
never copied into a destination. The config-driven surface (configure / copy /
compose-dests / link / global / update) reads ~/.shared-llm.yaml and runs every
operation centrally against the destination paths it lists, so the engine can
never drift out of sync with a per-repo copy of itself. It also reconciles the
per-harness symlinks (repo-scoped pi/codex skills, the global Pi runtime) — see
the reconciler section below.

Usage:
    python3 tools/harness.py init -o mac|ubuntu                           # prereq check
    python3 tools/harness.py configure -d /path/to/repo -l cc,pi          # edit ~/.shared-llm.yaml
    python3 tools/harness.py update -v                                    # copy -> compose -> link (+ global)
    # low-level (used by tests + the global staging compose):
    python3 tools/harness.py compose .shared-llm/compose/skills/x.yaml --shared-llm /p/.shared-llm --target /out
"""

from __future__ import annotations

import argparse
import collections
from dataclasses import dataclass
import json
import os
from pathlib import Path
import shutil
import sys
from typing import Any

try:
    import yaml
except ImportError:
    sys.exit("error: PyYAML is required. Install it with: pip install pyyaml")


def find_shared_llm(arg: str | None) -> Path:
    """Resolve the .shared-llm source root.

    Resolution order:
      1. explicit --shared-llm argument
      2. $SHARED_LLM_DIR environment variable
      3. walk up from the script location (then cwd) for a `.shared-llm/` dir
    Fails loud if none resolve to a directory.
    """
    # 1. explicit argument
    if arg:
        candidate = Path(arg).expanduser().resolve()
        if not candidate.is_dir():
            print(f"error: --shared-llm path is not a directory: {candidate}", file=sys.stderr)
            sys.exit(1)
        return candidate

    # 2. environment variable
    env = os.environ.get("SHARED_LLM_DIR")
    if env:
        candidate = Path(env).expanduser().resolve()
        if not candidate.is_dir():
            print(f"error: $SHARED_LLM_DIR is not a directory: {candidate}", file=sys.stderr)
            sys.exit(1)
        return candidate

    # 3. walk up from the script location, then cwd, for a `.shared-llm/` dir
    for start in (Path(__file__).resolve().parent, Path.cwd()):
        candidate = start
        for _ in range(10):
            shared = candidate / ".shared-llm"
            if shared.is_dir():
                return shared
            candidate = candidate.parent

    print("error: cannot find .shared-llm root", file=sys.stderr)
    sys.exit(1)


def read_file(path: Path) -> str:
    """Read a file, failing hard if it doesn't exist."""
    if not path.exists():
        print(f"error: file not found: {path}", file=sys.stderr)
        sys.exit(1)
    return path.read_text()


VALID_TYPES = {"skill", "agent", "claude-md", "agents-md", "prompt", "copy", "settings"}

# Types that produce plain concatenated markdown with no name/description
# frontmatter (CLAUDE.md / AGENTS.md / whole feature prompts).
PLAIN_TYPES = ("claude-md", "agents-md", "prompt")

# Types that are NOT markdown concatenation at all — they bypass the
# description read + concatenation flow entirely:
#   copy     — a single source file copied verbatim (preserving the executable
#              bit) to the output. For hook scripts / statusline.sh, which
#              are code, not composable prose.
#   settings — JSON inputs deep-merged left-to-right into one JSON document
#              (dicts merge recursively, lists concatenate, scalars overlay-win).
#              For settings.json = a common base + a this_repo overlay.
STRUCTURED_TYPES = ("copy", "settings")


def load_compose_yaml(path: Path) -> dict[str, Any]:
    """Load and validate a compose YAML file."""
    text = read_file(path)
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        print(f"error: invalid YAML in {path}: {exc}", file=sys.stderr)
        sys.exit(1)

    if not isinstance(data, dict):
        print(f"error: {path} must be a YAML mapping", file=sys.stderr)
        sys.exit(1)

    compose_type = data.get("type", "")
    if compose_type and compose_type not in VALID_TYPES:
        print(f"error: {path} has invalid type '{compose_type}' (valid: {VALID_TYPES})", file=sys.stderr)
        sys.exit(1)

    for field in ("inputs", "output"):
        if field not in data:
            print(f"error: {path} missing required field: {field}", file=sys.stderr)
            sys.exit(1)

    if compose_type == "copy" and len(data.get("inputs", [])) != 1:
        print(f"error: {path} type 'copy' needs exactly one input (a single file)", file=sys.stderr)
        sys.exit(1)

    if compose_type not in PLAIN_TYPES and compose_type not in STRUCTURED_TYPES:
        for field in ("name", "description"):
            if field not in data:
                print(f"error: {path} missing required field: {field}", file=sys.stderr)
                sys.exit(1)
        if not isinstance(data["description"], str):
            print(f"error: {path} 'description' must be a string", file=sys.stderr)
            sys.exit(1)
        if "frontmatter" in data and not isinstance(data["frontmatter"], dict):
            print(f"error: {path} 'frontmatter' must be a mapping", file=sys.stderr)
            sys.exit(1)
        if "resources" in data and not isinstance(data["resources"], str):
            print(f"error: {path} 'resources' must be a string (a source directory path)", file=sys.stderr)
            sys.exit(1)

    if not isinstance(data["inputs"], list):
        print(f"error: {path} 'inputs' must be a list", file=sys.stderr)
        sys.exit(1)
    if not isinstance(data["output"], str):
        print(f"error: {path} 'output' must be a string", file=sys.stderr)
        sys.exit(1)

    return data


# ---------------------------------------------------------------------------
# Output handlers
# ---------------------------------------------------------------------------

class ClaudeSkill:
    """Produces a skill .md file with name + description frontmatter.

    A recipe may declare an optional `frontmatter:` mapping of extra fields
    (e.g. `argument-hint`, `allowed-tools`). They are emitted in order AFTER
    name + description, so a slash-command source can carry the same frontmatter
    its hand-authored SKILL.md had without the composer dropping it.
    """

    def build_frontmatter(self, name: str, description: str, extra: dict[str, Any]) -> str:
        fm: dict[str, Any] = {"name": name, "description": description}
        fm.update(extra)
        return yaml.dump(fm, default_flow_style=False, sort_keys=False, allow_unicode=True, width=2**31)

    def write(self, data: dict[str, Any], body: str, output_path: Path) -> None:
        description = data["_description_content"]
        extra = data.get("frontmatter") or {}
        frontmatter = self.build_frontmatter(data["name"], description, extra)
        content = f"---\n{frontmatter}---\n\n{body}"
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(content)


class ClaudeAgent:
    """Produces an agent .md file with name + description frontmatter.

    Optional `model:` and `color:` fields are emitted only when the recipe
    declares them. An agent with no `model:` pin inherits the dispatching
    session's model, and a call-time model override always wins — so a recipe
    omits `model:` when the agent's LLM is chosen per run (e.g. by a route
    profile) rather than hardwired.
    """

    def build_frontmatter(self, name: str, description: str, model: str | None, color: str | None) -> str:
        fm: dict[str, Any] = {"name": name, "description": description}
        if model is not None:
            fm["model"] = model
        if color is not None:
            fm["color"] = color
        return yaml.dump(fm, default_flow_style=False, sort_keys=False, allow_unicode=True, width=2**31)

    def write(self, data: dict[str, Any], body: str, output_path: Path) -> None:
        description = data["_description_content"]
        model = data.get("model")
        color = data.get("color")
        frontmatter = self.build_frontmatter(data["name"], description, model, color)
        content = f"---\n{frontmatter}---\n\n{body}"
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(content)


class ClaudeMd:
    """Produces a plain markdown file with no frontmatter."""

    def write(self, data: dict[str, Any], body: str, output_path: Path) -> None:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(body)


class AgentsMd:
    """Produces a plain markdown file for AGENTS.md (cross-harness instruction files)."""

    def write(self, data: dict[str, Any], body: str, output_path: Path) -> None:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(body)


class Prompt:
    """Produces a whole feature prompt — plain concatenated markdown, no frontmatter.

    A feature prompt is a complete system prompt: it bundles the selected layer
    fragments (credentials pointer, CI/CD-via-Taskfile, the per-feature context,
    the chosen agent role) into one piece so a systemPrompt-controlling harness
    gets everything without traversing the layer tree.
    """

    def write(self, data: dict[str, Any], body: str, output_path: Path) -> None:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(body)


class CopyFile:
    """Copies one source file verbatim to the output, preserving its mode.

    For code that lives in the layer tree but is not composable prose — hook
    scripts, statusline.sh. `shutil.copy2` carries the executable bit across, so
    a `chmod +x`-ed hook source lands executable (git tracks that bit, so
    compose:check catches a mode drift the same as a content drift).
    """

    def write(self, source_path: Path, output_path: Path) -> None:
        if not source_path.exists():
            print(f"error: file not found: {source_path}", file=sys.stderr)
            sys.exit(1)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source_path, output_path)


def deep_merge(base: Any, overlay: Any) -> Any:
    """Deep-merge two JSON-like values, overlay winning.

    - dict + dict  -> recurse key by key
    - list + list  -> concatenate (base first) — so a common base's hook array
                      and a this_repo overlay's hook array both survive
    - anything else -> overlay replaces base
    """
    if isinstance(base, dict) and isinstance(overlay, dict):
        merged = dict(base)
        for key, value in overlay.items():
            merged[key] = deep_merge(merged[key], value) if key in merged else value
        return merged
    if isinstance(base, list) and isinstance(overlay, list):
        return base + overlay
    return overlay


class SettingsMerge:
    """Deep-merges JSON inputs left-to-right into one JSON document.

    For settings.json = a generic common base + a this_repo overlay. The `hooks`
    object merges key by key and each hook-event array concatenates, so the
    common base carries the portable hooks (formatting, quality checks) and the
    this_repo overlay adds the repo-specific ones without either clobbering the
    other.
    """

    def write(self, source_paths: list[Path], output_path: Path) -> None:
        merged: Any = {}
        for src in source_paths:
            if not src.exists():
                print(f"error: file not found: {src}", file=sys.stderr)
                sys.exit(1)
            try:
                data = json.loads(src.read_text())
            except json.JSONDecodeError as exc:
                print(f"error: invalid JSON in {src}: {exc}", file=sys.stderr)
                sys.exit(1)
            merged = deep_merge(merged, data)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(json.dumps(merged, indent=2) + "\n")


# ---------------------------------------------------------------------------
# Composer
# ---------------------------------------------------------------------------

class Composer:
    """Discovers compose YAMLs and dispatches to the right output handler.

    INPUT resolution is ONE rule. Every input/catalog/description path in a
    recipe is repo-root-relative and resolves against `repo_root`: a path that
    starts with `.shared-llm/...` (layer content) and one that starts with
    `ops/...` (repo content outside the layer tree) resolve identically —
    `repo_root / path`. This replaced the old dual resolver that silently forked
    between the kit and a consumer repo.

    OUTPUT resolution uses `output_base`, which defaults to `repo_root` (so a
    normal compose reads and writes the same repo — the consumer flow). It is
    only ever pointed elsewhere for the kit's OWN self-compose, which stages
    outputs into the gitignored examples/ dir while still reading inputs from the
    real kit root. That is a safe output redirect, not an input fork.

    `shared_root` locates the compose/ and skills/ dirs for discovery; it
    defaults to `repo_root/.shared-llm` and never affects path resolution.
    """

    def __init__(self, repo_root: Path, output_base: Path | None = None,
                 shared_root: Path | None = None) -> None:
        self.repo_root = repo_root
        self.output = output_base if output_base is not None else repo_root
        self.shared = shared_root if shared_root is not None else repo_root / ".shared-llm"
        self.skill_handler = ClaudeSkill()
        self.agent_handler = ClaudeAgent()
        self.claude_md_handler = ClaudeMd()
        self.agents_md_handler = AgentsMd()
        self.prompt_handler = Prompt()
        self.copy_handler = CopyFile()
        self.settings_handler = SettingsMerge()

    def resolve_input(self, relative: str) -> Path:
        """Resolve an input/catalog/description path against the repo root.

        Every recipe path is repo-root-relative as written (`.shared-llm/...`
        for layer content, `ops/...` for repo content that lives outside the
        layer tree). One rule, no prefix-stripping, no second base.
        """
        return self.repo_root / relative

    def resolve_output(self, relative: str) -> Path:
        """Resolve an output path against the output base (defaults to repo root)."""
        return self.output / relative

    def discover(self, root: Path | None = None) -> list[Path]:
        """Find all recipe YAML files under a directory (default <shared>/compose/).

        Pass an explicit `root` (e.g. <shared>/compose/agents) to compose only a
        SUBSET of recipes — this is how a consumer install composes the
        consumer-relevant recipes (root CLAUDE.md/AGENTS.md, skills, agents) while
        leaving the home-only `global/` skills and the `example-*` demo samples out
        of the consumer's tree.
        """
        compose_dir = root if root is not None else self.shared / "compose"
        if not compose_dir.is_dir():
            print(f"error: compose directory not found: {compose_dir}", file=sys.stderr)
            sys.exit(1)
        yamls = sorted(compose_dir.rglob("*.yaml"))
        yamls += sorted(compose_dir.rglob("*.yml"))
        if not yamls:
            print(f"warning: no YAML files found in {compose_dir}", file=sys.stderr)
        return yamls

    def _resolve_type(self, data: dict[str, Any]) -> str:
        """Determine compose type from explicit field or legacy heuristics."""
        if "type" in data:
            return data["type"]
        if "model" in data:
            return "agent"
        return "skill"

    def compose_one(self, yaml_path: Path) -> None:
        """Process a single compose YAML."""
        data = load_compose_yaml(yaml_path)
        compose_type = self._resolve_type(data)

        # Structured/verbatim types bypass the markdown concatenation flow.
        if compose_type == "copy":
            source_path = self.resolve_input(data["inputs"][0])
            output_path = self.resolve_output(data["output"])
            print(f"  copy: {output_path.name} -> {output_path}")
            self.copy_handler.write(source_path, output_path)
            return
        if compose_type == "settings":
            source_paths = [self.resolve_input(rel) for rel in data["inputs"]]
            output_path = self.resolve_output(data["output"])
            print(f"  settings: {output_path.name} -> {output_path}")
            self.settings_handler.write(source_paths, output_path)
            return

        if compose_type not in PLAIN_TYPES:
            desc_path = self.resolve_input(data["description"])
            data["_description_content"] = read_file(desc_path).strip()

        parts: list[str] = []
        # Inject shared catalog partial FIRST when declared (single-source pattern).
        catalog_rel = data.get("catalog")
        if catalog_rel:
            catalog_path = self.resolve_input(catalog_rel)
            parts.append(read_file(catalog_path).rstrip())
        for input_rel in data["inputs"]:
            input_path = self.resolve_input(input_rel)
            parts.append(read_file(input_path).rstrip())

        separator = "\n\n---\n\n" if compose_type in PLAIN_TYPES else "\n"
        body = separator.join(parts) + "\n"
        output_path = self.resolve_output(data["output"])
        label = data.get("name", output_path.name)

        print(f"  {compose_type}: {label} -> {output_path}")

        match compose_type:
            case "claude-md":
                self.claude_md_handler.write(data, body, output_path)
            case "agents-md":
                self.agents_md_handler.write(data, body, output_path)
            case "agent":
                self.agent_handler.write(data, body, output_path)
            case "prompt":
                self.prompt_handler.write(data, body, output_path)
            case _:
                self.skill_handler.write(data, body, output_path)

        # Copy bundled resources (a source dir of static reference files) into the
        # output dir alongside the composed file — for skills that ship a
        # references/ tree the SKILL.md points at. Stale copies are cleared first.
        resources_rel = data.get("resources")
        if resources_rel:
            src_dir = self.resolve_input(resources_rel)
            if not src_dir.is_dir():
                print(f"error: resources path is not a directory: {src_dir}", file=sys.stderr)
                sys.exit(1)
            for item in src_dir.rglob("*"):
                if item.is_dir():
                    continue
                dest = output_path.parent / item.relative_to(src_dir)
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(item, dest)

    def copy_standalone_skills(self) -> None:
        """Copy whole standalone skills verbatim into .claude/skills/.

        Skills under <source>/skills/ are complete, multi-file skills (SKILL.md +
        README + agents/ + references/) — nothing to stitch. Unlike the recipe-driven
        skills, they are copied in their entirety: whole dir in, whole dir out. The
        repo's .codex-plugin points Codex at the same .claude/skills/ dir, so both
        harnesses pick them up from one copy. Stale dest dirs are cleared first.
        """
        skills_src = self.shared / "skills"
        if not skills_src.is_dir():
            return
        for skill_dir in sorted(skills_src.iterdir()):
            if not skill_dir.is_dir() or skill_dir.name.startswith("."):
                continue
            dest = self.output / ".claude" / "skills" / skill_dir.name
            print(f"  standalone-skill: {skill_dir.name} -> {dest}")
            if dest.exists():
                shutil.rmtree(dest)
            shutil.copytree(skill_dir, dest)

    def compose_all(self) -> None:
        """Discover and compose all targets."""
        yamls = self.discover()
        for yp in yamls:
            self.compose_one(yp)
        self.copy_standalone_skills()

    def compose_dir(self, recipe_dir: Path) -> None:
        """Compose every recipe under a single recipe directory (a subset)."""
        yamls = self.discover(recipe_dir)
        for yp in yamls:
            self.compose_one(yp)


# ===========================================================================
# Symlink reconciler
# ===========================================================================
#
# compose turns layer files into outputs under .claude/. The reconciler wires
# those outputs into the per-harness discovery dirs the tools read at startup,
# by symlink. Two callers use it:
#
#   link (per destination, repo-scoped — plan_pi_repo / plan_codex_repo):
#     pi     .claude/skills/<name>  -> <repo>/.pi/skills/<name>      (common skills)
#     codex  .claude/skills/<name>  -> <repo>/.agents/skills/<name>  (common + codex skills)
#
#   do_home_runtime (global Pi runtime — plan_pi_runtime):
#     ext      .shared-llm/llm/pi/common/extensions/<x> -> ~/.pi/agent/extensions/<x>
#              (or ~/.pi/extensions for *-hub.ts)
#     personas .shared-llm/llm/pi/common/agents/<x>.md  -> ~/.pi/agents/<x>.md
#
# It RECONCILES desired-vs-actual: creates missing links, re-points drifted ones,
# and PRUNES links whose source was renamed or deleted. It only ever touches a
# link that resolves into THIS repo family — never a foreign link or a real file.

HOME = Path.home()

# Authored extensions present in the tree but intentionally NOT linked on this
# clone. Empty in the portable kit — nothing to skip.
EXT_SKIP = set()  # no private overrides in the portable kit

# Managed sub-path markers. A link counts as "ours" only if its target contains
# the repo-family token AND one of these — the guard that stops us ever removing
# a foreign link or a real file.
MANAGED_MARKERS = (
    "/.claude/skills/",
    "/.claude/agents/",
    "/.ai/shared/skills/",
    "/.ai/shared/agents/",
    "/.shared-llm/llm/pi/common/extensions/",
    # legacy pre-migration paths — kept so the reconciler recognises and prunes
    # links left dangling by the layers/ -> .shared-llm/ move.
    "/layers/llm/pi/common/extensions/",
    "/layers/llm/pi/common/agents/",
)


def project_root() -> Path:
    """The repo root (tools/ lives directly under it)."""
    return Path(__file__).resolve().parent.parent


def repo_family(root: Path) -> str:
    """Token identifying our own links even from a sibling worktree clone.
    Worktrees live in <repo>-trees/<wt>, so the family is the parent dir name
    minus the -trees suffix; otherwise the repo dir name itself."""
    if root.parent.name.endswith("-trees"):
        return root.parent.name[: -len("-trees")]
    return root.name


@dataclass
class LinkPlan:
    desired: dict[Path, Path]   # dest link path -> source path it should point at
    dest_dirs: list[Path]       # dirs scanned for orphaned / dangling managed links


def harness_of(root: Path, name: str) -> str:
    """Harness a skill belongs to, derived from where its recipe lives:
      compose/slash-commands/<scope>/<harness>/<name>.yaml -> <harness>
      compose/skills/<name>.yaml (convention skill)        -> common
    else 'unknown'. Mirrors the bash harness_of."""
    slash = root / ".shared-llm/compose/slash-commands"
    if slash.is_dir():
        hits = list(slash.rglob(f"{name}.yaml"))
        if hits:
            return hits[0].parent.name
    if (root / ".shared-llm/compose/skills" / f"{name}.yaml").exists():
        return "common"
    return "unknown"


def link_is_ours(dest: Path, family: str, repo_root: Path | None = None) -> bool:
    """True iff dest is a symlink whose literal OR resolved target points into
    our repo family's managed dirs. Works on a dangling link (the literal target
    string still carries the marker). Never returns True for a real file.

    `repo_root` is the repo whose managed links we're reconciling — the fallback
    "resolves inside this clone" check uses it. It defaults to the engine's own
    repo (project_root()) only for the legacy global sync path; the
    config-driven, per-destination link step passes the DESTINATION root so a
    link is judged against the repo it actually belongs to, not the kit that
    happens to host the engine (review item 1)."""
    if not dest.is_symlink():
        return False
    literal = os.readlink(dest)
    resolved = os.path.realpath(dest)  # a path string even when it doesn't exist
    for t in (literal, resolved):
        if family in t and any(m in t for m in MANAGED_MARKERS):
            return True
    # Also ours if it resolves inside the repo we're reconciling.
    try:
        Path(resolved).relative_to(repo_root if repo_root is not None else project_root())
        return True
    except ValueError:
        return False


def _skill_dirs(root: Path) -> list[Path]:
    skills = root / ".claude/skills"
    if not skills.is_dir():
        return []
    return [d for d in sorted(skills.iterdir()) if d.is_dir() and not d.name.startswith(".") and d.name != "_archived"]


def plan_pi_runtime(root: Path) -> LinkPlan:
    """Global Pi RUNTIME links: the bundled extensions and the hand-authored Pi
    agent personas (tf-reviewer, doc-reviewer, …) under
    .shared-llm/llm/pi/common/. Skills are handled separately by do_global (copied,
    routed) and the composed generic agents are COPIED by do_home_runtime, so this
    plan deliberately excludes both — it is only the stable .ts/.md runtime sources
    that are safe to symlink."""
    pi_agents = HOME / ".pi/agents"
    agent_ext = HOME / ".pi/agent/extensions"
    hub_ext = HOME / ".pi/extensions"
    desired: dict[Path, Path] = {}
    personas = root / ".shared-llm/llm/pi/common/agents"
    if personas.is_dir():
        for f in sorted(personas.glob("*.md")):
            desired[pi_agents / f.name] = f
    ext_src = root / ".shared-llm/llm/pi/common/extensions"
    if ext_src.is_dir():
        for entry in sorted(ext_src.iterdir()):
            name = entry.name
            if name.startswith(".") or name in EXT_SKIP:
                continue
            if entry.is_dir():
                desired[agent_ext / name] = entry
            elif name.endswith(".ts") and not name.endswith(".test.ts"):
                is_hub = name.endswith("-hub.ts") or name.startswith("hub-")
                desired[(hub_ext if is_hub else agent_ext) / name] = entry
    return LinkPlan(desired, [pi_agents, agent_ext, hub_ext])


def reconcile(plan: LinkPlan, family: str, *, plan_only: bool, force: bool,
              repo_root: Path | None = None) -> collections.Counter:
    counts: collections.Counter = collections.Counter()
    tag = "plan" if plan_only else "done"

    def emit(kind: str, dest: Path, src: Path | None) -> None:
        counts[kind] += 1
        arrow = f" -> {src}" if src is not None else ""
        print(f"  [{tag}] {kind}: {dest}{arrow}")

    if not plan_only:
        for d in plan.dest_dirs:
            d.mkdir(parents=True, exist_ok=True)

    # create / re-point
    for dest, src in sorted(plan.desired.items()):
        if dest.is_symlink():
            if os.readlink(dest) == str(src):
                if force and not plan_only:
                    dest.unlink()
                    dest.symlink_to(src)
                continue  # already correct
            if link_is_ours(dest, family, repo_root):
                emit("repoint", dest, src)
                if not plan_only:
                    dest.unlink()
                    dest.symlink_to(src)
            else:
                emit("skip-foreign", dest, None)
        elif dest.exists():
            emit("skip-foreign", dest, None)
        else:
            emit("create", dest, src)
            if not plan_only:
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.symlink_to(src)

    # prune orphaned / dangling managed links (source renamed or deleted), and
    # WARN on a real file that shadows a managed extension — Pi loads every dest
    # dir, so a real file sharing a name with a managed link double-loads and
    # collides (duplicate command/tool registration). We never auto-delete a real
    # file; we surface it loud so the human removes the stale copy.
    desired_by_dir: dict[Path, set] = collections.defaultdict(set)
    for dest in plan.desired:
        desired_by_dir[dest.parent].add(dest.name)
    managed_names = {dest.name for dest in plan.desired}
    for d in plan.dest_dirs:
        if not d.is_dir():
            continue
        for entry in sorted(d.iterdir()):
            if entry.name in desired_by_dir[d]:
                continue  # wanted — handled above
            if entry.is_symlink():
                if link_is_ours(entry, family, repo_root):
                    emit("prune", entry, None)
                    if not plan_only:
                        entry.unlink()
            elif entry.name in managed_names:
                counts["shadow"] += 1
                print(
                    f"  [WARN] real file shadows managed '{entry.name}' — Pi double-loads "
                    f"and collides; remove the stale copy: {entry}",
                    file=sys.stderr,
                )
    return counts


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def cmd_compose(args: argparse.Namespace) -> None:
    shared_root = find_shared_llm(args.shared_llm)
    # Inputs resolve against the repo that owns the .shared-llm we found (its
    # parent). Outputs default to that same repo (consumer flow: read and write
    # one repo). An explicit --target redirects ONLY where outputs land — used by
    # the kit self-compose to stage into examples/ — while inputs still resolve
    # against the real repo root.
    repo_root = shared_root.parent
    output_base = Path(args.target).expanduser().resolve() if args.target else repo_root
    composer = Composer(repo_root, output_base=output_base, shared_root=shared_root)

    print(f"shared-llm source: {shared_root}")
    print(f"repo root: {repo_root}")
    print(f"output base: {output_base}")

    if args.recipe:
        recipe = Path(args.recipe)
        if not recipe.is_absolute():
            # A recipe path may be given repo-relative (`.shared-llm/compose/...`)
            # or source-relative (`compose/...`). Resolve against the shared root,
            # stripping the source dir name if the caller included it.
            rel = args.recipe
            base = shared_root.name
            if rel.startswith(base + "/"):
                rel = rel[len(base) + 1 :]
            recipe = shared_root / rel
        # A directory composes the subset of recipes under it; a file composes one.
        if recipe.is_dir():
            composer.compose_dir(recipe)
        else:
            composer.compose_one(recipe)
    else:
        composer.compose_all()

    print("done.")


# ===========================================================================
# Config-driven, centralized multi-destination flow:
#   configure / copy / compose / link / update / init
#
# The engine lives ONLY in this kit and is never copied into a destination. All
# operations run centrally from here against destination paths read from
# ~/.shared-llm.yaml. This is what makes engine drift impossible: there is only
# ever one engine (review item 1 root cause).
# ===========================================================================

CONFIG_PATH = HOME / ".shared-llm.yaml"
DEFAULT_SOURCE = HOME / ".shared-llm"
VALID_HARNESSES = ("cc", "pi", "codex")

# The ONLY trees `copy` propagates: pure common layer + runtime content. It never
# touches a destination's this_repo/ overlays or its compose/ recipes — those are
# destination-owned and wire the private overlays (resolved decision #6). Paths
# are relative to a `.shared-llm/` dir.
COMMON_ROOTS = (
    "layers/agents/common",
    "layers/llm/common",
    "layers/skills/common",
    "layers/slash-commands/common",
    "llm/claude/common",
    "llm/common/common",
    "llm/pi/common",
)


class RunLog:
    """Prints to stdout when verbose; always appends to the run log file."""

    def __init__(self, verbose: bool, path: Path | None = None) -> None:
        self.verbose = verbose
        self.path = path
        self._fh = None
        if path is not None:
            path.parent.mkdir(parents=True, exist_ok=True)
            self._fh = path.open("a")

    def __call__(self, msg: str) -> None:
        if self.verbose:
            print(msg)
        if self._fh is not None:
            self._fh.write(msg + "\n")

    def always(self, msg: str) -> None:
        """Print regardless of verbosity (for summary lines), and log it."""
        print(msg)
        if self._fh is not None:
            self._fh.write(msg + "\n")

    def close(self) -> None:
        if self._fh is not None:
            self._fh.close()


def load_config() -> dict:
    cfg: dict = {}
    if CONFIG_PATH.exists():
        cfg = yaml.safe_load(CONFIG_PATH.read_text()) or {}
    cfg.setdefault("source", str(DEFAULT_SOURCE))
    cfg.setdefault("global", [])
    cfg.setdefault("destinations", [])
    cfg.setdefault("exclude", [])
    return cfg


def _is_excluded(src: Path, shared_root: Path, exclude: list[str]) -> bool:
    """True if `src` (a path under the .shared-llm source root) is at or under any
    `exclude` entry. Exclude entries are plain paths written the way they appear
    under .shared-llm/ (e.g. 'llm/claude/common/hooks'). Pure prefix match — no
    component-name logic."""
    try:
        rel = src.resolve().relative_to(shared_root.resolve())
    except ValueError:
        return False
    rel_str = str(rel)
    for entry in exclude:
        entry = str(entry).strip().strip("/")
        if entry and (rel_str == entry or rel_str.startswith(entry + "/")):
            return True
    return False


def save_config(cfg: dict) -> None:
    CONFIG_PATH.write_text(yaml.safe_dump(cfg, sort_keys=False, allow_unicode=True))


def parse_harnesses(spec: str) -> list[str]:
    out: list[str] = []
    for h in spec.split(","):
        h = h.strip()
        if not h:
            continue
        if h not in VALID_HARNESSES:
            sys.exit(f"error: unknown harness '{h}' (valid: {', '.join(VALID_HARNESSES)})")
        if h not in out:
            out.append(h)
    return out


def _print_config(cfg: dict) -> None:
    print(f"  source: {cfg['source']}")
    print(f"  global: {', '.join(cfg['global']) or '(none)'}")
    print(f"  exclude: {', '.join(cfg.get('exclude', [])) or '(none)'}")
    if not cfg["destinations"]:
        print("  destinations: (none)")
    for d in cfg["destinations"]:
        print(f"  dest: {d['path']}  [{', '.join(d.get('harnesses', []))}]")


def cmd_configure(args: argparse.Namespace) -> None:
    existed = CONFIG_PATH.exists()
    cfg = load_config()
    if args.source:
        cfg["source"] = str(Path(args.source).expanduser())
    if args.global_list is not None:
        cfg["global"] = parse_harnesses(args.global_list)
    if args.exclude is not None:
        cfg["exclude"] = [p.strip().strip("/") for p in args.exclude.split(",") if p.strip()]
    if args.dest:
        path = str(Path(args.dest).expanduser().resolve())
        harnesses = parse_harnesses(args.list) if args.list else ["cc", "pi"]
        for d in cfg["destinations"]:
            if d.get("path") == path:
                d["harnesses"] = harnesses
                break
        else:
            cfg["destinations"].append({"path": path, "harnesses": harnesses})
    save_config(cfg)
    print(f"{'updated' if existed else 'created'} {CONFIG_PATH}")
    _print_config(cfg)


def cmd_init(args: argparse.Namespace) -> None:
    print(f"init: checking prerequisites (os: {args.os}) ...")
    missing: list[str] = []
    for tool in ("python3", "just"):
        found = shutil.which(tool)
        if found:
            print(f"  ✓ {tool}: {found}")
        else:
            print(f"  ✗ {tool}: NOT FOUND")
            missing.append(tool)
    if missing:
        hint = {"mac": "brew install", "ubuntu": "sudo apt install"}.get(args.os, "install")
        sys.exit(f"error: missing prerequisite(s): {', '.join(missing)}  (try: {hint} {' '.join(missing)})")
    print("init: all prerequisites present. Next: `just configure -d <repo> -l cc,pi` then `just update`.")
    print("init: for the Pi harness, also run `just pi-extensions` to install the pinned third-party extensions.")


# --- copy ------------------------------------------------------------------

def _iter_common_rels(shared: Path):
    """Yield each common file's path relative to a `.shared-llm/` dir.
    Skips anything under a this_repo/ segment (defense in depth)."""
    for root in COMMON_ROOTS:
        base = shared / root
        if not base.is_dir():
            continue
        for p in sorted(base.rglob("*")):
            if p.is_file() and "this_repo" not in p.relative_to(shared).parts:
                yield p.relative_to(shared)


def _copy_common(src_shared: Path, dst_shared: Path, log: RunLog) -> collections.Counter:
    """Copy every common file from one `.shared-llm/` dir to another. Overwrites;
    FLAGS (does not block) when an existing dest file's content differs before it
    is overwritten (resolved decision #6). Never touches this_repo/ or compose/
    recipes. No auto-prune — a removed common file is left in place (matches the
    no-uninstall philosophy; drop it by hand if needed)."""
    counts: collections.Counter = collections.Counter()
    for rel in _iter_common_rels(src_shared):
        src = src_shared / rel
        dst = dst_shared / rel
        if dst.exists():
            if src.read_bytes() == dst.read_bytes():
                counts["same"] += 1
                continue
            counts["changed"] += 1
            log(f"    ~ changed (overwriting local edit): {rel}")
        else:
            counts["new"] += 1
            log(f"    + new: {rel}")
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
    return counts


def do_copy(cfg: dict, log: RunLog) -> None:
    kit_shared = project_root() / ".shared-llm"
    hub = Path(cfg["source"]).expanduser()
    log.always(f"copy: kit {kit_shared} -> hub {hub}")
    c = _copy_common(kit_shared, hub, log)
    log.always(f"  hub: {c['new']} new, {c['changed']} changed, {c['same']} unchanged")
    for d in cfg["destinations"]:
        dest_shared = Path(d["path"]).expanduser() / ".shared-llm"
        log.always(f"copy: hub -> {d['path']}/.shared-llm")
        c = _copy_common(hub, dest_shared, log)
        log.always(f"  {Path(d['path']).name}: {c['new']} new, {c['changed']} changed, {c['same']} unchanged")


# --- compose (config-driven, per destination) ------------------------------

# Consumer recipe groups composed for a destination (never global/ or example-*).
CONSUMER_RECIPE_GROUPS = (
    "compose/claude-md/root.yaml",
    "compose/agents-md/root.yaml",
    "compose/skills",
    "compose/agents",
    "compose/slash-commands",
    "compose/settings",
    "compose/hooks",
    "compose/statusline",
)


# Per-harness skill routing is by NAME PREFIX (matches the retired install-global):
#   do-*  -> Pi ONLY      cc-*  -> Claude Code ONLY      (anything else) -> both
# Claude Code reads <repo>/.claude/skills directly, so to keep do-* OUT of Claude
# they are routed out of .claude/skills into a Pi-only source dir after compose.
# Pi links do-* from there; common skills stay in .claude/skills for both.
PI_ONLY_SKILLS_DIR = ".pi-skills"


def _route_do_skills(dest: Path, log: RunLog) -> int:
    """Move composed do-* skill dirs OUT of .claude/skills (which Claude Code
    reads) into <repo>/.pi-skills, so do-* are Pi-only. Idempotent: compose
    rewrites .claude/skills/do-* each run and this relocates them."""
    claude_skills = dest / ".claude/skills"
    pi_src = dest / PI_ONLY_SKILLS_DIR
    moved = 0
    if not claude_skills.is_dir():
        return 0
    for d in sorted(claude_skills.iterdir()):
        if not d.is_dir() or not d.name.startswith("do-"):
            continue
        target = pi_src / d.name
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            shutil.rmtree(target)
        shutil.move(str(d), str(target))
        moved += 1
        log(f"    route do-* (Pi-only): {d.name} -> {PI_ONLY_SKILLS_DIR}/")
    return moved


def _compose_destination(dest: Path, log: RunLog) -> None:
    shared = dest / ".shared-llm"
    if not shared.is_dir():
        log.always(f"compose: skip {dest} — no .shared-llm/ (run copy/configure first)")
        return
    composer = Composer(dest, shared_root=shared)
    log.always(f"compose: {dest}")
    for group in CONSUMER_RECIPE_GROUPS:
        # group is like "compose/skills" (a dir) or "compose/claude-md/root.yaml" (a file)
        path = shared / Path(group)
        if path.is_dir():
            for yp in composer.discover(path):
                composer.compose_one(yp)
        elif path.is_file():
            composer.compose_one(path)
    composer.copy_standalone_skills()
    moved = _route_do_skills(dest, log)
    if moved:
        log.always(f"  routed {moved} do-* skill(s) to {PI_ONLY_SKILLS_DIR}/ (Pi-only, out of Claude's .claude/skills)")


def do_compose(cfg: dict, log: RunLog) -> None:
    for d in cfg["destinations"]:
        _compose_destination(Path(d["path"]).expanduser(), log)


# --- link (GLOBAL per-harness skill dirs) ----------------------------------
#
# Skills link into each harness's GLOBAL discovery dir, NOT a repo-scoped one:
#   pi     -> ~/.pi/agent/skills        codex -> ~/.agents/skills
# Global dirs have NO trust gate; Pi's project-local `.pi/skills/` loads only
# after a project is "trusted" (pi docs, skills.md), which silently hid the
# skills — the repo-scoped approach was reverted for that reason. Claude Code
# still needs no link (it reads <repo>/.claude/skills directly).
#
# All destinations' skills are aggregated into ONE reconcile per global dir, so a
# second destination does not prune the first's links. On a same-name collision
# the last destination wins and we WARN (we can't control it; just surface it).

# Computed from HOME at call time (NOT module-level constants) so a test that
# redirects HOME reaches the right dirs and never touches the real home.
def _pi_global_skills() -> Path:
    return HOME / ".pi/agent/skills"


def _codex_global_skills() -> Path:
    return HOME / ".agents/skills"


def _common_pi_skills(dest: Path) -> dict[str, Path]:
    """name -> source skill dir Pi should link. Pi gets: every do-* (routed into
    <repo>/.pi-skills, Pi-only) PLUS common skills from .claude/skills. Never cc-*
    (Claude-only) and never claude-scoped repo skills. Pi has no colon in command
    names, so foo:bar links as foo-bar."""
    out: dict[str, Path] = {}
    # do-* — Pi-only, routed out of .claude/skills into .pi-skills.
    pi_src = dest / PI_ONLY_SKILLS_DIR
    if pi_src.is_dir():
        for d in sorted(pi_src.iterdir()):
            if d.is_dir() and not d.name.startswith("."):
                out[d.name.replace(":", "-")] = d
    # common (portable) skills that stay in .claude/skills — never cc-*, never do-*.
    for d in _skill_dirs(dest):
        if d.name.startswith("cc-") or d.name.startswith("do-"):
            continue
        if harness_of(dest, d.name) == "common":
            out[d.name.replace(":", "-")] = d
    return out


def _common_codex_skills(dest: Path) -> dict[str, Path]:
    """Codex gets common + codex-scoped skills from .claude/skills — never do-*
    (Pi-only) and never cc-* (Claude-only)."""
    out: dict[str, Path] = {}
    for d in _skill_dirs(dest):
        if d.name.startswith("cc-") or d.name.startswith("do-"):
            continue
        if harness_of(dest, d.name) in ("common", "codex"):
            out[d.name] = d
    return out


def _resolves_into(link: Path, roots: list[Path]) -> bool:
    """True iff `link` is a symlink pointing (literally or resolved) into one of
    `roots` via a managed .claude/skills path — i.e. a link WE created. Works on a
    dangling link via the literal target string. Never true for a real file."""
    if not link.is_symlink():
        return False
    literal = os.readlink(link)
    target = os.path.realpath(link)
    for r in roots:
        try:
            Path(target).relative_to(r)
            return True
        except ValueError:
            pass
        if str(r) in literal and "/.claude/skills/" in literal:
            return True
    return False


def _reconcile_global(link_dir: Path, desired: dict[str, Path],
                      owned_roots: list[Path], log: RunLog) -> collections.Counter:
    """Reconcile a GLOBAL skill dir against `desired` (name -> source). Creates and
    re-points our links, prunes links we own that are no longer desired, and never
    touches a foreign link or real file."""
    counts: collections.Counter = collections.Counter()
    link_dir.mkdir(parents=True, exist_ok=True)
    for name, src in sorted(desired.items()):
        dest = link_dir / name
        if dest.is_symlink():
            if os.readlink(dest) == str(src):
                continue
            if _resolves_into(dest, owned_roots):
                dest.unlink()
                dest.symlink_to(src)
                counts["repoint"] += 1
                log(f"    repoint {dest} -> {src}")
            else:
                counts["skip-foreign"] += 1
        elif dest.exists():
            counts["skip-foreign"] += 1
        else:
            dest.symlink_to(src)
            counts["create"] += 1
            log(f"    create {dest} -> {src}")
    for entry in sorted(link_dir.iterdir()):
        if entry.name in desired:
            continue
        if entry.is_symlink() and _resolves_into(entry, owned_roots):
            entry.unlink()
            counts["prune"] += 1
            log(f"    prune {entry}")
    return counts


def _cleanup_repo_scoped_pi(dests: list[Path], log: RunLog) -> int:
    """Remove the abandoned repo-scoped <repo>/.pi/skills links from the reverted
    approach, so nothing dangles. Leaves other .pi/ contents alone."""
    removed = 0
    for dest in dests:
        rs = dest / ".pi/skills"
        if not rs.is_dir():
            continue
        for e in sorted(rs.iterdir()):
            if e.is_symlink() and _resolves_into(e, [dest]):
                e.unlink()
                removed += 1
        try:
            rs.rmdir()
            (dest / ".pi").rmdir()
        except OSError:
            pass
    return removed


def do_link(cfg: dict, log: RunLog) -> None:
    dests = [Path(d["path"]).expanduser() for d in cfg["destinations"]]
    pi_desired: dict[str, Path] = {}
    codex_desired: dict[str, Path] = {}
    want_pi = want_codex = False
    collisions: list[str] = []

    for d in cfg["destinations"]:
        dest = Path(d["path"]).expanduser()
        harnesses = d.get("harnesses", [])
        if "cc" in harnesses:
            log(f"  [{dest.name}] cc: no link needed (reads .claude/ directly)")
        if "pi" in harnesses:
            want_pi = True
            for name, src in _common_pi_skills(dest).items():
                if name in pi_desired and pi_desired[name] != src:
                    collisions.append(f"pi:{name}")
                pi_desired[name] = src
        if "codex" in harnesses:
            want_codex = True
            for name, src in _common_codex_skills(dest).items():
                if name in codex_desired and codex_desired[name] != src:
                    collisions.append(f"codex:{name}")
                codex_desired[name] = src

    # Clean up the abandoned repo-scoped .pi/skills links (reverted approach).
    cleaned = _cleanup_repo_scoped_pi(dests, log)
    if cleaned:
        log.always(f"  cleaned {cleaned} obsolete repo-scoped .pi/skills link(s)")

    if want_pi:
        pi_dir = _pi_global_skills()
        c = _reconcile_global(pi_dir, pi_desired, dests, log)
        log.always(f"  pi (global {pi_dir}): created {c['create']}, "
                   f"repointed {c['repoint']}, pruned {c['prune']}, skipped-foreign {c['skip-foreign']}")
    if want_codex:
        codex_dir = _codex_global_skills()
        c = _reconcile_global(codex_dir, codex_desired, dests, log)
        log.always(f"  codex (global {codex_dir}): created {c['create']}, "
                   f"repointed {c['repoint']}, pruned {c['prune']}, skipped-foreign {c['skip-foreign']}")
    for c in sorted(set(collisions)):
        log.always(f"  ⚠ name collision across destinations: {c} (last destination wins)")


# --- global home-skill flow (ported from the retired install-global.sh) ----
#
# Two skill families, one mechanism (a recipe assembles layers into a skill dir;
# the destination is HOME). Family A = convention skills (python/nextjs/...).
# Family B = the slash-command group, routed by recipe scope. Skills are COPIED
# (not symlinked) into home: the staged source under examples/ is a regenerated,
# gitignored artifact, so a symlink would dangle on the next clean/rebuild.
#
# Safe-migration discipline (mirrors the reconciler): idempotent; never clobber a
# foreign symlink or a divergent real dir; prune only byte-identical stale copies.

import filecmp

GLOBAL_CONVENTION_SKILLS = {
    "python": "compose/global/python.yaml",
    "nextjs": "compose/global/nextjs.yaml",
    "backend": "compose/global/backend.yaml",
    "golang": "compose/global/golang.yaml",
}

# Home-dir skill names to prune when found (matched by their SKILL.md `name:` so we
# never delete a foreign dir of the same name). Currently empty: cc-planish is a live
# skill again, and do-planish is the Pi extension command /do-planish, not a global
# skill (extensions are symlinked into ~/.pi/, never composed as skills).
DEPRECATED_GLOBAL_SKILLS: tuple[str, ...] = ()


def _global_home_dirs() -> dict[str, Path]:
    """Home skill dir per harness token. Codex uses ~/.agents/skills (item 4)."""
    return {
        "cc": HOME / ".claude/skills",
        "pi": HOME / ".pi/agent/skills",
        "codex": HOME / ".agents/skills",
    }


def _global_targets_for(scope: str, name: str) -> set[str]:
    """Harness tokens a slash-command skill routes to, from its recipe scope.
    Mirrors install-global.sh's slash_skill_bases: common goes to pi+codex, and
    also to cc UNLESS it's a do-* workflow command (cc has cc-* counterparts)."""
    if scope == "claude":
        return {"cc"}
    if scope == "common":
        targets = {"pi", "codex"}
        if not name.startswith("do-"):
            targets.add("cc")
        return targets
    if scope == "codex":
        return {"codex"}
    if scope == "pi":
        return {"pi"}
    return set()


def _dirs_equal(a: Path, b: Path) -> bool:
    cmp = filecmp.dircmp(a, b)
    if cmp.left_only or cmp.right_only or cmp.diff_files or cmp.funny_files:
        return False
    return all(_dirs_equal(a / sub, b / sub) for sub in cmp.common_dirs)


def _install_skill_dir(staged: Path, target: Path, log: RunLog) -> str:
    if target.is_symlink():
        log(f"    skip {target} (symlink — foreign)")
        return "skip"
    if target.is_dir():
        if _dirs_equal(staged, target):
            return "uptodate"
        log(f"    skip {target} (exists & differs — not ours)")
        return "skip"
    if target.exists():
        log(f"    skip {target} (non-dir — foreign)")
        return "skip"
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(staged, target)
    log(f"    installed {target}")
    return "installed"


def _prune_stale_skill(name: str, staged: Path, keep: set[str],
                       home_dirs: dict[str, Path], log: RunLog) -> int:
    """Remove byte-identical copies from home dirs whose token is NOT in keep."""
    removed = 0
    for tok, base in home_dirs.items():
        if tok in keep:
            continue
        target = base / name
        if target.is_symlink() or not target.exists():
            continue
        if target.is_dir() and _dirs_equal(staged, target):
            shutil.rmtree(target)
            removed += 1
            log(f"    pruned stale {target}")
    return removed


def _prune_deprecated_global(home_dirs: dict[str, Path], log: RunLog) -> int:
    removed = 0
    for name in DEPRECATED_GLOBAL_SKILLS:
        for base in home_dirs.values():
            target = base / name
            skill_md = target / "SKILL.md"
            if target.is_symlink() or not target.exists():
                continue
            if skill_md.is_file() and f"name: {name}" in skill_md.read_text():
                shutil.rmtree(target)
                removed += 1
                log(f"    removed deprecated {target}")
    return removed


def do_global(cfg: dict, log: RunLog) -> None:
    wanted = [h for h in cfg.get("global", []) if h in VALID_HARNESSES]
    if not wanted:
        return
    kit = project_root()
    kit_shared = kit / ".shared-llm"
    staging = kit / "examples"
    home_dirs = {tok: d for tok, d in _global_home_dirs().items() if tok in wanted}
    log.always(f"global: routing home skills to {', '.join(wanted)}")

    composer = Composer(kit, output_base=staging, shared_root=kit_shared)
    # Family A — convention skills (staged under examples/global-staging/skills/).
    for recipe in GLOBAL_CONVENTION_SKILLS.values():
        composer.compose_one(kit_shared / recipe)
    # Family B — the whole slash-command group (staged under examples/.claude/skills/).
    composer.compose_dir(kit_shared / "compose/slash-commands")

    installed = uptodate = skipped = pruned = 0

    # Family A: every convention skill goes to every wanted home dir.
    conv_staging = staging / "global-staging/skills"
    for name in GLOBAL_CONVENTION_SKILLS:
        staged = conv_staging / name
        if not staged.is_dir():
            log.always(f"  ⚠ convention skill missing after compose: {staged}")
            continue
        for base in home_dirs.values():
            r = _install_skill_dir(staged, base / name, log)
            installed += r == "installed"
            uptodate += r == "uptodate"
            skipped += r == "skip"

    # Family B: route each slash skill by its recipe scope, intersected with the
    # configured global harness list.
    slash_staging = staging / ".claude/skills"
    if slash_staging.is_dir():
        for staged in sorted(slash_staging.iterdir()):
            if not staged.is_dir():
                continue
            name = staged.name
            scope = harness_of(kit, name)
            keep = _global_targets_for(scope, name) & set(wanted)
            for tok in keep:
                r = _install_skill_dir(staged, home_dirs[tok] / name, log)
                installed += r == "installed"
                uptodate += r == "uptodate"
                skipped += r == "skip"
            pruned += _prune_stale_skill(name, staged, keep, home_dirs, log)

    pruned += _prune_deprecated_global(home_dirs, log)
    log.always(
        f"  global: {installed} installed, {uptodate} current, {skipped} skipped "
        f"(foreign/divergent), {pruned} stale/deprecated removed"
    )


# --- global home RUNTIME (ported from the retired install-local.sh) ---------
#
# Beyond skills, `install local` also laid down the home runtime: the 18 generic
# agents, the Pi extension/persona symlinks + settings, and the Claude hooks /
# statusline / settings. Ported here so install-local.sh can be retired. Codex
# has no user-agent dir concept, so agents skip it (never invent a dir).

# Home agent dir per harness token (codex intentionally absent).
GENERIC_AGENT_HOME = {"cc": ".claude/agents", "pi": ".pi/agents"}


def _install_file(staged: Path, target: Path, log: RunLog) -> str:
    """Foreign-safe single-file copy (agents, hooks, statusline). Preserves mode."""
    if target.is_symlink():
        log(f"    skip {target} (symlink — foreign)")
        return "skip"
    if target.exists():
        if target.read_bytes() == staged.read_bytes():
            return "uptodate"
        log(f"    skip {target} (exists & differs — not ours)")
        return "skip"
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(staged, target)
    log(f"    installed {target}")
    return "installed"


def _scaffold_settings(template: Path, target: Path, log: RunLog) -> None:
    """Copy a settings template into place ONLY when absent — never clobber
    per-machine tweaks."""
    if not template.is_file():
        return
    if target.exists():
        log(f"    settings: preserved existing {target}")
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(template, target)
    log(f"    settings: scaffolded {target}")


def _install_claude_runtime(kit_shared: Path, log: RunLog, exclude: list[str]) -> None:
    src = kit_shared / "llm/claude/common"
    claude_home = HOME / ".claude"

    def skip(p: Path) -> bool:
        if _is_excluded(p, kit_shared, exclude):
            log(f"    exclude: {p.relative_to(kit_shared)}")
            return True
        return False

    if (src / "hooks").is_dir():
        for hook in sorted((src / "hooks").iterdir()):
            if hook.is_file() and not skip(hook):
                _install_file(hook, claude_home / "hooks" / hook.name, log)
    if (src / "statusline.sh").is_file() and not skip(src / "statusline.sh"):
        _install_file(src / "statusline.sh", claude_home / "statusline.sh", log)
    if not skip(src / "settings.template.json"):
        _scaffold_settings(src / "settings.template.json", claude_home / "settings.json", log)


def do_home_runtime(cfg: dict, log: RunLog) -> None:
    wanted = [h for h in cfg.get("global", []) if h in VALID_HARNESSES]
    if not wanted:
        return
    kit = project_root()
    kit_shared = kit / ".shared-llm"
    staging = kit / "examples"
    exclude = cfg.get("exclude", [])
    log.always(f"home-runtime: {', '.join(wanted)}" + (f"  (exclude: {', '.join(exclude)})" if exclude else ""))

    # 1. Generic agents — compose to staging, copy into the wanted home agent dirs.
    composer = Composer(kit, output_base=staging, shared_root=kit_shared)
    composer.compose_dir(kit_shared / "compose/agents")
    staged_agents = staging / ".claude/agents"
    agent_bases = [HOME / rel for tok, rel in GENERIC_AGENT_HOME.items() if tok in wanted]
    a_ins = a_up = a_sk = 0
    if staged_agents.is_dir():
        for staged in sorted(staged_agents.glob("*.md")):
            for base in agent_bases:
                r = _install_file(staged, base / staged.name, log)
                a_ins += r == "installed"
                a_up += r == "uptodate"
                a_sk += r == "skip"
    log.always(f"  agents: {a_ins} copied, {a_up} current, {a_sk} skipped (foreign/divergent)")

    # 2. Claude runtime — hooks + statusline + settings scaffold (exclude-aware).
    if "cc" in wanted:
        _install_claude_runtime(kit_shared, log, exclude)

    # 3. Pi runtime — extension/persona symlinks + settings scaffold. Drop any
    #    source under an exclude path from the link plan before reconciling.
    if "pi" in wanted:
        plan = plan_pi_runtime(kit)
        if exclude:
            kept = {dst: src for dst, src in plan.desired.items()
                    if not _is_excluded(src, kit_shared, exclude)}
            dropped = len(plan.desired) - len(kept)
            if dropped:
                log.always(f"  pi runtime: excluded {dropped} source(s) via exclude list")
            plan = LinkPlan(kept, plan.dest_dirs)
        counts = reconcile(plan, repo_family(kit),
                           plan_only=False, force=False, repo_root=kit)
        log.always(
            f"  pi runtime: created {counts['create']}, repointed {counts['repoint']}, "
            f"pruned {counts['prune']}, skipped-foreign {counts['skip-foreign']}"
        )
        if not _is_excluded(kit_shared / "llm/pi/common/settings.template.json", kit_shared, exclude):
            _scaffold_settings(kit_shared / "llm/pi/common/settings.template.json",
                               HOME / ".pi/agent/settings.json", log)


# --- update (copy -> compose -> link, with a full run log) -----------------

def _log_path() -> Path:
    from datetime import datetime
    stamp = datetime.now().strftime("%Y-%m-%d-%H%M%S")
    return Path("/tmp/.shared-llm/log") / f"{stamp}.log"


def cmd_copy(args: argparse.Namespace) -> None:
    do_copy(load_config(), RunLog(verbose=True))


def cmd_compose_cfg(args: argparse.Namespace) -> None:
    do_compose(load_config(), RunLog(verbose=True))


def cmd_link(args: argparse.Namespace) -> None:
    do_link(load_config(), RunLog(verbose=True))


def cmd_global(args: argparse.Namespace) -> None:
    cfg = load_config()
    log = RunLog(verbose=True)
    do_global(cfg, log)
    do_home_runtime(cfg, log)


def do_check(cfg: dict, log: RunLog) -> bool:
    """Assert the placement invariants: do-* Pi-only, cc-* Claude-only. Returns
    True if everything holds. Prints PASS/FAIL per check."""
    ok = True

    def report(label: str, bad: list[str]) -> None:
        nonlocal ok
        if bad:
            ok = False
            log.always(f"  FAIL  {label}: {' '.join(sorted(bad))}")
        else:
            log.always(f"  PASS  {label}")

    def names(d: Path, pred) -> list[str]:
        return [p.name for p in d.iterdir() if pred(p)] if d.is_dir() else []

    uses_pi = any("pi" in d.get("harnesses", []) for d in cfg["destinations"]) or "pi" in cfg.get("global", [])
    uses_codex = any("codex" in d.get("harnesses", []) for d in cfg["destinations"]) or "codex" in cfg.get("global", [])

    if uses_pi:
        pi = _pi_global_skills()
        report(f"Pi {pi} has NO cc-*", names(pi, lambda p: p.name.startswith("cc-")))
        report(f"Pi {pi} has no broken links", names(pi, lambda p: p.is_symlink() and not p.exists()))
    if uses_codex:
        cx = _codex_global_skills()
        report(f"Codex {cx} has NO do-*", names(cx, lambda p: p.name.startswith("do-")))
        report(f"Codex {cx} has NO cc-*", names(cx, lambda p: p.name.startswith("cc-")))

    for d in cfg["destinations"]:
        dest = Path(d["path"]).expanduser()
        cs = dest / ".claude/skills"
        ps = dest / PI_ONLY_SKILLS_DIR
        report(f"[{dest.name}] .claude/skills has NO do-*",
               names(cs, lambda p: p.is_dir() and p.name.startswith("do-")))
        report(f"[{dest.name}] .pi-skills is do-* only",
               names(ps, lambda p: not p.name.startswith(("do-", "."))))
    return ok


def cmd_check(args: argparse.Namespace) -> None:
    log = RunLog(verbose=True)
    if not do_check(load_config(), log):
        sys.exit("check: FAILED — placement invariants violated (see above)")
    log.always("check: all placement invariants hold ✓")


def cmd_update(args: argparse.Namespace) -> None:
    cfg = load_config()
    if not cfg["destinations"] and not cfg["global"]:
        sys.exit("error: nothing configured. Run `just configure -d <repo> -l cc,pi` first.")
    log_path = _log_path()
    log = RunLog(verbose=args.verbose, path=log_path)
    log.always(f"update: log -> {log_path}")
    log.always("=== copy ===")
    do_copy(cfg, log)
    log.always("=== compose ===")
    do_compose(cfg, log)
    log.always("=== link ===")
    do_link(cfg, log)
    if cfg["global"]:
        log.always("=== global ===")
        do_global(cfg, log)
        do_home_runtime(cfg, log)
    log.always("update: done.")
    log.close()
    if not args.verbose:
        print(f"(run `just update -v` or see {log_path} for the per-file detail)")


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="harness.py",
        description="Compose layer files and reconcile per-harness symlinks (pi / codex).",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    pc = sub.add_parser("compose", help="Assemble skills/agents/CLAUDE.md from layer recipes.")
    pc.add_argument(
        "recipe",
        nargs="?",
        help=(
            "A specific compose YAML to process, OR a directory of recipes to compose "
            "as a subset (e.g. .shared-llm/compose/agents). Default: all recipes."
        ),
    )
    pc.add_argument("--shared-llm", help="Path to the .shared-llm source root.")
    pc.add_argument("--target", help="Output base dir where 'output:' paths land (default: cwd).")
    pc.set_defaults(func=cmd_compose)

    # --- config-driven, centralized surface (the user-facing flow) ---
    pi = sub.add_parser("init", help="Check OS prerequisites (python3, just).")
    pi.add_argument("-o", "--os", choices=["mac", "ubuntu"], default="ubuntu")
    pi.set_defaults(func=cmd_init)

    pcfg = sub.add_parser("configure", help="Create/update ~/.shared-llm.yaml.")
    pcfg.add_argument("-s", "--source", help="Set the source hub path (default ~/.shared-llm).")
    pcfg.add_argument("-d", "--dest", help="Add/update a destination repo path.")
    pcfg.add_argument("-l", "--list", help="Harnesses for -d (comma-separated: cc,pi,codex). Default cc,pi.")
    pcfg.add_argument("-g", "--global-list", help="Set the global harness list (comma-separated).")
    pcfg.add_argument("-x", "--exclude", help="Set the home-install exclude list: source paths under .shared-llm/ (comma-separated).")
    pcfg.set_defaults(func=cmd_configure)

    pcp = sub.add_parser("copy", help="Kit -> hub -> each destination's .shared-llm/ (common only).")
    pcp.set_defaults(func=cmd_copy)

    pcd = sub.add_parser("compose-dests", help="Compose every configured destination from its own .shared-llm/.")
    pcd.set_defaults(func=cmd_compose_cfg)

    pl = sub.add_parser("link", help="Reconcile repo-scoped pi/codex skill links per destination.")
    pl.set_defaults(func=cmd_link)

    pg = sub.add_parser("global", help="Compose + route the global home skills into ~/.claude, ~/.pi/agent, ~/.agents.")
    pg.set_defaults(func=cmd_global)

    pck = sub.add_parser("check", help="Verify skill placement per harness (do-* Pi-only, cc-* Claude-only).")
    pck.set_defaults(func=cmd_check)

    pup = sub.add_parser("update", help="copy -> compose -> link (+ global) across all configured destinations.")
    pup.add_argument("-v", "--verbose", action="store_true", help="Print per-file detail (always written to the log).")
    pup.set_defaults(func=cmd_update)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
