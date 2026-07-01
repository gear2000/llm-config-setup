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
    type: agent      — YAML frontmatter with name + description + model
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

This file also reconciles the per-harness symlinks (pi / codex) that wire the
composed outputs into the dirs the tools read at startup — see the reconciler
section near the bottom. One file, three subcommands, driven by the justfile
(setup) and the Taskfile (compose).

Usage:
    python tools/harness.py compose                                      # compose all
    python tools/harness.py compose .shared-llm/compose/skills/x.yaml    # compose one
    python tools/harness.py compose --shared-llm /path/.shared-llm --target /path/out
    python tools/harness.py sync                                         # create + prune all harness links
    python tools/harness.py sync --plan                                  # preview, touch nothing
    python tools/harness.py unlink                                       # remove managed links
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
    """Produces an agent .md file with name + description + model frontmatter.

    An optional `color:` field is emitted only when the recipe declares one.
    """

    def build_frontmatter(self, name: str, description: str, model: str, color: str | None) -> str:
        fm: dict[str, Any] = {"name": name, "description": description, "model": model}
        if color is not None:
            fm["color"] = color
        return yaml.dump(fm, default_flow_style=False, sort_keys=False, allow_unicode=True, width=2**31)

    def write(self, data: dict[str, Any], body: str, output_path: Path) -> None:
        description = data["_description_content"]
        model = data["model"]
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
    """Discovers compose YAMLs and dispatches to the right output handler."""

    def __init__(self, source_root: Path, target_root: Path) -> None:
        self.source = source_root
        self.target = target_root
        self.skill_handler = ClaudeSkill()
        self.agent_handler = ClaudeAgent()
        self.claude_md_handler = ClaudeMd()
        self.agents_md_handler = AgentsMd()
        self.prompt_handler = Prompt()
        self.copy_handler = CopyFile()
        self.settings_handler = SettingsMerge()

    def resolve_input(self, relative: str) -> Path:
        """Resolve an input/catalog/description path against the SOURCE root.

        A literal that already names the source root (`.shared-llm/...`) is kept
        repo-relative by stripping that prefix, so recipes can carry the full
        `.shared-llm/layers/...` path the repo commits while still resolving
        against an arbitrary --shared-llm base.
        """
        rel = relative
        base = self.source.name  # ".shared-llm"
        if rel.startswith(base + "/"):
            rel = rel[len(base) + 1 :]
        return self.source / rel

    def resolve_output(self, relative: str) -> Path:
        """Resolve an output path against the TARGET root."""
        return self.target / relative

    def discover(self, root: Path | None = None) -> list[Path]:
        """Find all recipe YAML files under a directory (default <source>/compose/).

        Pass an explicit `root` (e.g. <source>/compose/agents) to compose only a
        SUBSET of recipes — this is how a consumer install composes the
        consumer-relevant recipes (root CLAUDE.md/AGENTS.md, skills, agents) while
        leaving the home-only `global/` skills and the `example-*` demo samples out
        of the consumer's tree.
        """
        compose_dir = root if root is not None else self.source / "compose"
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
        skills_src = self.source / "skills"
        if not skills_src.is_dir():
            return
        for skill_dir in sorted(skills_src.iterdir()):
            if not skill_dir.is_dir() or skill_dir.name.startswith("."):
                continue
            dest = self.target / ".claude" / "skills" / skill_dir.name
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
# Symlink reconciler — sync / unlink
# ===========================================================================
#
# compose (above) turns layer files into outputs under .claude/. The reconciler
# wires those outputs and the Pi extensions into the per-harness discovery dirs
# the tools read at startup, by symlink:
#
#   pi     skills   .claude/skills/<name>     -> ~/.pi/agent/skills/<name>  (portable skills only; Pi reads <agentDir>/skills)
#          agents   .claude/agents/<name>.md  -> ~/.pi/agents/<name>.md
#          ext      .shared-llm/llm/pi/common/extensions/<x>
#                                             -> ~/.pi/agent/extensions/<x>  (or ~/.pi/extensions for *-hub.ts)
#   codex  skills   .claude/skills/<name>     -> ~/.codex/skills/<name> (portable + codex skills)
#
# Unlike the old add-only bash, this RECONCILES desired-vs-actual: it creates
# missing links, re-points drifted ones, and PRUNES links whose source was
# renamed or deleted (the gap that left dangling links). It only ever touches a
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


def link_is_ours(dest: Path, family: str) -> bool:
    """True iff dest is a symlink whose literal OR resolved target points into
    our repo family's managed dirs. Works on a dangling link (the literal target
    string still carries the marker). Never returns True for a real file."""
    if not dest.is_symlink():
        return False
    literal = os.readlink(dest)
    resolved = os.path.realpath(dest)  # a path string even when it doesn't exist
    for t in (literal, resolved):
        if family in t and any(m in t for m in MANAGED_MARKERS):
            return True
    # Also ours if it resolves inside the current clone.
    try:
        Path(resolved).relative_to(project_root())
        return True
    except ValueError:
        return False


def _skill_dirs(root: Path) -> list[Path]:
    skills = root / ".claude/skills"
    if not skills.is_dir():
        return []
    return [d for d in sorted(skills.iterdir()) if d.is_dir() and not d.name.startswith(".") and d.name != "_archived"]


def plan_pi(root: Path) -> LinkPlan:
    desired: dict[Path, Path] = {}
    # Pi reads user skills from <agentDir>/skills, i.e. ~/.pi/agent/skills — NOT
    # ~/.pi/skills. Don't "fix" this back without re-checking the Pi runtime.
    pi_skills = HOME / ".pi/agent/skills"
    # NOTE: ~/.pi/agents is unverified against the current Pi runtime layout —
    # audit separately; not in scope for this rename.
    pi_agents = HOME / ".pi/agents"
    agent_ext = HOME / ".pi/agent/extensions"
    hub_ext = HOME / ".pi/extensions"

    # skills — Pi gets a skill only when it is portable to every harness (common).
    # Pi does not support colons in command names, so any skill named "foo:bar"
    # is installed under the hyphenated alias "foo-bar" instead.
    for d in _skill_dirs(root):
        if harness_of(root, d.name) == "common":
            pi_name = d.name.replace(":", "-")
            desired[pi_skills / pi_name] = d

    # agents — composed personas under .claude/agents/ AND the hand-authored Pi
    # agent personas kept in the runtime tree (.shared-llm/llm/pi/common/agents/,
    # e.g. the hub reviewers + tf-reviewer). Both feed ~/.pi/agents/.
    for agents_src in (root / ".claude/agents", root / ".shared-llm/llm/pi/common/agents"):
        if agents_src.is_dir():
            for f in sorted(agents_src.glob("*.md")):
                desired[pi_agents / f.name] = f

    # extensions — dirs + flat .ts (minus *.test.ts and the skip-list); hub routing
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

    return LinkPlan(desired, [pi_skills, pi_agents, agent_ext, hub_ext])


def plan_codex(root: Path) -> LinkPlan:
    desired: dict[Path, Path] = {}
    codex_home = Path(os.environ.get("CODEX_HOME", str(HOME / ".codex")))
    codex_skills = codex_home / "skills"
    # Codex gets portable (common) and codex-specific skills; not claude-only.
    for d in _skill_dirs(root):
        if harness_of(root, d.name) in ("common", "codex"):
            desired[codex_skills / d.name] = d
    return LinkPlan(desired, [codex_skills])


PLAN_BUILDERS = {"pi": plan_pi, "codex": plan_codex}


def reconcile(plan: LinkPlan, family: str, *, plan_only: bool, force: bool) -> collections.Counter:
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
            if link_is_ours(dest, family):
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
                if link_is_ours(entry, family):
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
    source_root = find_shared_llm(args.shared_llm)
    target_root = Path(args.target).expanduser().resolve() if args.target else Path.cwd()
    composer = Composer(source_root, target_root)

    print(f"shared-llm source: {source_root}")
    print(f"target output: {target_root}")

    if args.recipe:
        recipe = Path(args.recipe)
        if not recipe.is_absolute():
            # A recipe path may be given repo-relative (`.shared-llm/compose/...`)
            # or source-relative (`compose/...`). Resolve against the source root,
            # stripping the source dir name if the caller included it.
            rel = args.recipe
            base = source_root.name
            if rel.startswith(base + "/"):
                rel = rel[len(base) + 1 :]
            recipe = source_root / rel
        # A directory composes the subset of recipes under it; a file composes one.
        if recipe.is_dir():
            composer.compose_dir(recipe)
        else:
            composer.compose_one(recipe)
    else:
        composer.compose_all()

    print("done.")


def cmd_sync(args: argparse.Namespace) -> None:
    root = project_root()
    family = repo_family(root)
    harnesses = list(PLAN_BUILDERS) if args.harness == "all" else [args.harness]
    print(f"repo: {root}  family: {family}  mode: {'plan' if args.plan else 'apply'}")
    total: collections.Counter = collections.Counter()
    for h in harnesses:
        print(f"--- {h} ---")
        total += reconcile(PLAN_BUILDERS[h](root), family, plan_only=args.plan, force=args.force)
    line = (
        f"done. created {total['create']}, repointed {total['repoint']}, "
        f"pruned {total['prune']}, skipped-foreign {total['skip-foreign']}."
    )
    if total["shadow"]:
        line += f"  ⚠ {total['shadow']} shadow conflict(s) — remove the [WARN] real files above."
    print(line)


def cmd_unlink(args: argparse.Namespace) -> None:
    root = project_root()
    family = repo_family(root)
    harnesses = list(PLAN_BUILDERS) if args.harness == "all" else [args.harness]
    removed = 0
    for h in harnesses:
        for d in PLAN_BUILDERS[h](root).dest_dirs:
            if not d.is_dir():
                continue
            for entry in sorted(d.iterdir()):
                if link_is_ours(entry, family):
                    print(f"  unlink: {entry}")
                    entry.unlink()
                    removed += 1
    print(f"done. unlinked {removed}.")


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

    ps = sub.add_parser("sync", help="Reconcile per-harness symlinks: create, re-point, prune.")
    ps.add_argument("--harness", choices=["pi", "codex", "all"], default="all")
    ps.add_argument("--plan", action="store_true", help="Preview the changes; touch nothing.")
    ps.add_argument("--force", action="store_true", help="Re-create even a correct link.")
    ps.set_defaults(func=cmd_sync)

    pu = sub.add_parser("unlink", help="Remove every managed symlink for a harness.")
    pu.add_argument("--harness", choices=["pi", "codex", "all"], default="all")
    pu.set_defaults(func=cmd_unlink)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
