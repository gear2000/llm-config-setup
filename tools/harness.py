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
    python3 tools/harness.py compose .shared-llm/public/compose/skills/x.yaml --shared-llm /p/.shared-llm --target /out
"""

from __future__ import annotations

import argparse
import collections
import hashlib
import importlib.util
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from contextlib import contextmanager, redirect_stderr, suppress
from dataclasses import dataclass
from pathlib import Path
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
            print(
                f"error: --shared-llm path is not a directory: {candidate}",
                file=sys.stderr,
            )
            sys.exit(1)
        return candidate

    # 2. environment variable
    env = os.environ.get("SHARED_LLM_DIR")
    if env:
        candidate = Path(env).expanduser().resolve()
        if not candidate.is_dir():
            print(
                f"error: $SHARED_LLM_DIR is not a directory: {candidate}",
                file=sys.stderr,
            )
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

# Build-time placeholder token: the kit's inline fill-in convention is
# {{UPPERCASE_UNDERSCORE}} (e.g. {{PROJECT_NAME}}, {{CRED_ROOT}}). Deliberately
# NOT matching lowercase/dotted/spaced braces, so a `{{ user.name }}` inside an
# example code fence never trips the fail-loud check.
PLACEHOLDER_RE = re.compile(r"\{\{([A-Z][A-Z0-9_]*)\}\}")

# Discovery metadata policy. The hard ceiling matches the Agent Skills
# specification and is applied to this kit's owned agent descriptions too, so
# all harness discovery surfaces stay predictable. Length is measured in UTF-16
# code units because that is the conservative cross-harness boundary.
DESCRIPTION_REVIEW_THRESHOLD = 300
DESCRIPTION_HARD_MAX = 1024


@dataclass(frozen=True)
class DescriptionItem:
    """One owned discovery description measured by the audit/preflight."""

    owner: str  # item owner: "public" or "destination"
    source_owner: str  # description source owner: "public" or "destination"
    kind: str
    name: str
    source: Path
    length: int
    location: str
    destination_index: int | None = None


@dataclass(frozen=True)
class DescriptionError:
    owner: str
    kind: str
    name: str
    source: Path
    message: str
    destination_index: int | None = None


def utf16_code_units(text: str) -> int:
    """Count UTF-16 code units without a BOM; supplementary chars count as 2."""
    return len(text.encode("utf-16-le")) // 2


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
        print(
            f"error: {path} has invalid type '{compose_type}' (valid: {VALID_TYPES})",
            file=sys.stderr,
        )
        sys.exit(1)

    for field in ("inputs", "output"):
        if field not in data:
            print(f"error: {path} missing required field: {field}", file=sys.stderr)
            sys.exit(1)

    if compose_type == "copy" and len(data.get("inputs", [])) != 1:
        print(
            f"error: {path} type 'copy' needs exactly one input (a single file)",
            file=sys.stderr,
        )
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
            print(
                f"error: {path} 'resources' must be a string (a source directory path)",
                file=sys.stderr,
            )
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

    def build_frontmatter(
        self, name: str, description: str, extra: dict[str, Any]
    ) -> str:
        fm: dict[str, Any] = {"name": name, "description": description}
        fm.update(extra)
        return yaml.dump(
            fm,
            default_flow_style=False,
            sort_keys=False,
            allow_unicode=True,
            width=2**31,
        )

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

    def build_frontmatter(
        self, name: str, description: str, model: str | None, color: str | None
    ) -> str:
        fm: dict[str, Any] = {"name": name, "description": description}
        if model is not None:
            fm["model"] = model
        if color is not None:
            fm["color"] = color
        return yaml.dump(
            fm,
            default_flow_style=False,
            sort_keys=False,
            allow_unicode=True,
            width=2**31,
        )

    def write(self, data: dict[str, Any], body: str, output_path: Path) -> None:
        description = data["_description_content"]
        model = data.get("model")
        color = data.get("color")
        frontmatter = self.build_frontmatter(data["name"], description, model, color)
        content = f"---\n{frontmatter}---\n\n{body}"
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(content)


# Provenance banner for committed instruction files: they must stay real files
# (self-contained repos, other clones, CI), so the header — not a symlink — is
# what marks them as build artifacts.
GENERATED_HEADER = (
    "<!-- GENERATED by llm-config-setup (`just update`) — edit the layers under "
    ".shared-llm/, not this file. -->\n\n"
)


class ClaudeMd:
    """Produces a plain markdown file with no frontmatter."""

    def write(self, data: dict[str, Any], body: str, output_path: Path) -> None:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(GENERATED_HEADER + body)


class AgentsMd:
    """Produces a plain markdown file for AGENTS.md (cross-harness instruction files)."""

    def write(self, data: dict[str, Any], body: str, output_path: Path) -> None:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(GENERATED_HEADER + body)


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

    def __init__(
        self,
        repo_root: Path,
        output_base: Path | None = None,
        shared_root: Path | None = None,
        placeholders: dict[str, str] | None = None,
    ) -> None:
        self.repo_root = repo_root
        self.output = output_base if output_base is not None else repo_root
        self.shared = (
            shared_root if shared_root is not None else repo_root / ".shared-llm"
        )
        # Per-destination build-time fill values for kit-synced layers that carry
        # {{TOKEN}} placeholders. Empty for the low-level compose CLI and the
        # kit's own self-compose (whose layers carry no placeholders).
        self.placeholders = placeholders or {}
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

    def _fill_placeholders(self, text: str) -> str:
        """Replace each {{TOKEN}} for which this destination has a value; leave
        unknown tokens untouched (the fail-loud check catches those)."""
        if not text:
            return text
        return PLACEHOLDER_RE.sub(
            lambda m: self.placeholders.get(m.group(1), m.group(0)), text
        )

    @staticmethod
    def _pulls_template(data: dict[str, Any]) -> bool:
        """True if a recipe pulls a TEMPLATE.* stub as an input/description. Such
        a recipe is composing a deliberately-unfilled stub (the kit placeholder
        convention), so it is exempt from the unfilled-placeholder fail-loud."""
        refs = [r for r in (data.get("inputs") or []) if isinstance(r, str)]
        for key in RECIPE_PATH_FIELDS:
            v = data.get(key)
            if isinstance(v, str):
                refs.append(v)
        return any(Path(r).name.startswith("TEMPLATE.") for r in refs)

    def _assert_filled(
        self, text: str, output_path: Path, yaml_path: Path, exempt: bool
    ) -> None:
        """Fail loud (non-zero exit) if composed output still holds a {{TOKEN}}
        that no placeholder filled — naming the token(s), the output, and the
        recipe. Exempt when the recipe pulls a TEMPLATE.* stub."""
        if exempt:
            return
        unfilled = sorted(set(PLACEHOLDER_RE.findall(text)))
        if unfilled:
            tokens = ", ".join("{{" + t + "}}" for t in unfilled)
            print(
                f"error: unfilled placeholder(s) {tokens} in composed output "
                f"{output_path} (recipe {yaml_path}). Add them to this "
                f"destination's `placeholders:` map in ~/.shared-llm.yaml.",
                file=sys.stderr,
            )
            sys.exit(1)

    def discover(self, root: Path | None = None) -> list[Path]:
        """Find all recipe YAML files under a directory (default <shared>/compose/).

        Pass an explicit `root` (e.g. <shared>/compose/agents) to compose only a
        SUBSET of recipes — this is how a consumer install composes the
        consumer-relevant recipes (root CLAUDE.md/AGENTS.md, dest-owned nested
        md recipes under this_repo/compose/{claude,agents}-md/, skills, agents)
        while leaving the home-only `global/` skills and the public `example-*`
        demo samples out of the consumer's tree.
        """
        compose_dir = root if root is not None else self.shared / "public" / "compose"
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
            data["_description_content"] = self._fill_placeholders(
                read_file(desc_path).strip()
            )

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
        body = self._fill_placeholders(separator.join(parts) + "\n")
        output_path = self.resolve_output(data["output"])
        label = data.get("name", output_path.name)

        # Fail loud on any {{TOKEN}} left unfilled by the destination's
        # placeholders map (build-time fill for kit-synced layers).
        exempt = self._pulls_template(data)
        self._assert_filled(body, output_path, yaml_path, exempt)
        if compose_type not in PLAIN_TYPES:
            self._assert_filled(
                data["_description_content"], output_path, yaml_path, exempt
            )

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
                print(
                    f"error: resources path is not a directory: {src_dir}",
                    file=sys.stderr,
                )
                sys.exit(1)
            public_root = self.repo_root / ".shared-llm" / PUBLIC_DIR
            ignored_common_rels = _git_ignored_common_rels(public_root)
            for item in src_dir.rglob("*"):
                if item.is_dir():
                    continue
                resource_rel = item.relative_to(src_dir)
                if _is_artifact_rel(resource_rel):
                    continue
                try:
                    common_rel = item.relative_to(public_root)
                except ValueError:
                    common_rel = None
                if common_rel is not None and str(common_rel) in ignored_common_rels:
                    continue
                dest = output_path.parent / resource_rel
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(item, dest)

    def copy_standalone_skills(self, skills_src: Path | None = None) -> None:
        """Copy whole standalone skills verbatim into .claude/skills/.

        Skills under the source skills dir are complete, multi-file skills
        (SKILL.md + README + agents/ + references/) — nothing to stitch. Unlike
        the recipe-driven skills, they are copied in their entirety: whole dir in,
        whole dir out. The repo's .codex-plugin points Codex at the same
        .claude/skills/ dir, so both harnesses pick them up from one copy. Stale
        dest dirs are cleared first.

        `skills_src` defaults to <shared>/skills; the split destination flow points
        it at <shared>/this_repo/skills (standalone skills are repo-owned).
        """
        if skills_src is None:
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
#     ext      ~/.shared-llm/generated/pi/extensions/<x> -> ~/.pi/agent/extensions/<x>
#              (or ~/.pi/extensions for *-hub.ts)
#     personas ~/.shared-llm/generated/pi/agents/<x>.md  -> ~/.pi/agent/agents/<x>.md
#
#   The kit sources under .shared-llm/public/llm/pi/common/ are COPIED into that
#   generated tree first. No home link ever targets the repo checkout, so a moved,
#   renamed, or deleted clone cannot take the user's runtime down with it.
#
# Pi's agent dirs sit INSIDE its config dir (`PI_CODING_AGENT_DIR`, default
# ~/.pi/agent) — user personas are read from ~/.pi/agent/agents, exactly as
# skills are read from ~/.pi/agent/skills. ~/.pi/agents is NOT a discovery
# path; personas parked there are invisible to Pi. See LEGACY_PI_AGENTS_REL.
#
# Pi reads user personas from a SECOND dir too: ~/.agents — the very dir this
# kit installs Codex home skills into (see _global_home_dirs). pi-subagents
# below 0.30.0 walked it recursively and took every SKILL.md carrying `name:`
# + `description:` for an agent definition, so each Codex home skill surfaced
# as a phantom Pi agent; the two user dirs merge last-wins, so the phantom
# `backend` SKILL beat this kit's real `backend` PERSONA. 0.30.0 skips
# ~/.agents/skills, which is why third-party-extensions.txt pins that as a
# floor. Keep the floor: composing a convention skill and an agent persona
# under one name is only safe above it.
#
# It RECONCILES desired-vs-actual: creates missing links, re-points drifted ones,
# and PRUNES links whose source was renamed or deleted. It only ever touches a
# link that resolves into THIS repo family — never a foreign link or a real file.

HOME = Path.home()

# Where this kit USED to park Pi personas. Pi never read it (see the reconciler
# note above), so every run migrates what it owns out of here and leaves
# anything foreign alone. Kept relative and joined to HOME at call time — baking
# HOME in here would make the test suite write to the real home dir.
LEGACY_PI_AGENTS_REL = ".pi/agents"

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
    "/.shared-llm/public/llm/pi/common/extensions/",
    "/.shared-llm/public/llm/pi/common/agents/",
    "/.shared-llm/public/llm/claude/common/",
    "/herdr-config.toml",
    # the durable generated tree every home link now points into
    "/.shared-llm/generated/",
    # legacy pre-migration paths — kept so the reconciler recognises and prunes
    # links left dangling by the .shared-llm/ -> .shared-llm/public/ move and the
    # earlier layers/ -> .shared-llm/ move.
    "/.shared-llm/llm/pi/common/extensions/",
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
    desired: dict[Path, Path]  # dest link path -> source path it should point at
    dest_dirs: list[Path]  # dirs scanned for orphaned / dangling managed links


def harness_of(root: Path, name: str) -> str:
    """Harness a skill belongs to, derived from where its recipe lives:
      compose/slash-commands/<scope>/<harness>/<name>.yaml -> <harness>
      compose/skills/<name>.yaml (convention skill)        -> common
    else 'unknown'. Mirrors the bash harness_of.

    Searches the flat kit layout AND both split-destination trees
    (public/compose, this_repo/compose), so it works for the kit's own
    self-compose (flat) and a configured destination (split) alike."""
    compose_bases = [
        root / ".shared-llm/compose",
        root / ".shared-llm" / PUBLIC_DIR / "compose",
        root / ".shared-llm" / THIS_REPO_DIR / "compose",
    ]
    # Composition writes public recipes first and repository recipes second. Keep
    # scanning after a public match so the reported harness belongs to the final
    # writer at a shared output path, not to the portable recipe it replaced.
    slash_harness = "unknown"
    for base in compose_bases:
        slash = base / "slash-commands"
        if slash.is_dir():
            hits = sorted(slash.rglob(f"{name}.yaml"))
            if hits:
                slash_harness = hits[-1].parent.name
    if slash_harness != "unknown":
        return slash_harness
    for base in compose_bases:
        if (base / "skills" / f"{name}.yaml").exists():
            return "common"
    return "unknown"


def link_is_ours(dest: Path, family: str, repo_root: Path | None = None) -> bool:
    """True iff dest is a symlink whose literal OR resolved target points into
    our repo family's managed dirs. Works on a dangling link (the literal target
    string still carries the marker). Never returns True for a real file.

    `repo_root` is the repo whose managed links we're reconciling. It defaults to
    the engine's own repo (project_root()) only for the legacy global sync path;
    the config-driven, per-destination link step passes the DESTINATION root so a
    link is judged against the repo it actually belongs to, not the kit that
    happens to host the engine.

    BOTH halves are required: the target must be contained in a root we deploy
    from, AND land on a managed sub-path within it. Containment alone is not
    ownership — a user's own link to some unrelated file in the checkout (a
    README, a script) is contained too, and unlinking it would destroy a live
    configuration this tool never created."""
    if not dest.is_symlink():
        return False
    # A link into our own durable generated tree is provably ours regardless of
    # which repo family produced it — that tree exists only because of this kit.
    if _link_points_generated(dest):
        return True
    own = repo_root if repo_root is not None else project_root()
    # Containment, never substrings. A foreign link into
    # /tmp/foreign-<family>-backup/.shared-llm/public/llm/pi/common/extensions/x.ts
    # spells both the family token and a managed marker, and a substring test
    # would happily unlink it. Judging CONTAINMENT under an enumerated root makes
    # the decoy impossible: it simply is not under any root we deploy from.
    for target in (_absolute_link_target(dest), os.path.realpath(dest)):
        norm = Path(os.path.normpath(target))
        for root, is_trees in _repo_link_roots(own, family):
            if _is_under(norm, root) and _has_managed_marker(
                norm.relative_to(root), worktree_container=is_trees
            ):
                return True
    return False


def _has_managed_marker(rel: Path, *, worktree_container: bool = False) -> bool:
    """True iff a path RELATIVE to a repo root STARTS with one of the sub-paths
    this kit deploys from.

    Anchoring is the point. A marker matched anywhere in the path makes
    `<repo>/docs/.claude/skills/x` read as a managed deployment when it is just a
    documentation example — and pruning it would delete a live link we never
    created. The only tolerated prefix is a single worktree-name component, when
    the root is a `<family>-trees/` container holding sibling checkouts."""
    candidates = [rel]
    if worktree_container and len(rel.parts) > 1:
        candidates.append(Path(*rel.parts[1:]))
    for candidate in candidates:
        spelled = f"/{candidate.as_posix()}"
        for marker in MANAGED_MARKERS:
            trimmed = marker.rstrip("/")
            if spelled == trimmed or spelled.startswith(trimmed + "/"):
                return True
    return False


def _repo_link_roots(own: Path, family: str) -> list[tuple[Path, bool]]:
    """Roots a link of ours may legitimately point into, each flagged as a
    worktree CONTAINER or not: the repo being reconciled, this engine's own
    checkout, and the sibling-worktree dir of the same family (`<family>-trees/`)
    from either direction. A container holds one checkout per child, so a managed
    path there sits one component deeper."""
    roots = [(own, False), (project_root(), False)]
    for base in (own, project_root()):
        trees = (
            base.parent
            if base.parent.name == f"{family}-trees"
            else base.parent / f"{family}-trees"
        )
        roots.append((trees, True))
    return roots


def _skill_dirs(root: Path) -> list[Path]:
    skills = root / ".claude/skills"
    if not skills.is_dir():
        return []
    return [
        d
        for d in sorted(skills.iterdir())
        if d.is_dir() and not d.name.startswith(".") and d.name != "_archived"
    ]


def plan_pi_runtime(root: Path, exclude: list[str] | None = None) -> LinkPlan:
    """Global Pi RUNTIME links: the bundled extensions and the hand-authored Pi
    agent personas (tf-reviewer, doc-reviewer, …) under
    .shared-llm/public/llm/pi/common/. Skills are handled separately by do_global
    (copied, routed) and the composed generic agents are handled by
    do_home_runtime, so this plan covers only the stable .ts/.md runtime sources.

    The home links point at the DURABLE generated tree, never at the repo
    checkout: each source is copied into ~/.shared-llm/generated/pi/ first, so a
    moved, deleted, or half-written kit clone cannot break the user's runtime.
    Copying is why this planner writes: the desired targets it returns only exist
    because it materialised them."""
    pi_agents = HOME / ".pi/agent/agents"
    agent_ext = HOME / ".pi/agent/extensions"
    hub_ext = HOME / ".pi/extensions"
    kit_shared = root / ".shared-llm" / PUBLIC_DIR
    gen_ext = generated_root() / "pi/extensions"
    gen_personas = generated_root() / "pi/agents"

    def excluded(src: Path) -> bool:
        return bool(exclude) and _is_excluded(src, kit_shared, exclude)

    desired: dict[Path, Path] = {}
    sources: dict[Path, Path] = {}  # generated path -> kit source

    personas = kit_shared / "llm/pi/common/agents"
    if personas.is_dir():
        for f in sorted(personas.glob("*.md")):
            if excluded(f):
                continue
            gen = gen_personas / f.name
            sources[gen] = f
            desired[pi_agents / f.name] = gen
    ext_src = kit_shared / "llm/pi/common/extensions"
    if ext_src.is_dir():
        for entry in sorted(ext_src.iterdir()):
            name = entry.name
            if name.startswith(".") or name in EXT_SKIP or excluded(entry):
                continue
            if entry.is_dir():
                home_dest = agent_ext / name
            elif name.endswith(".ts") and not name.endswith(".test.ts"):
                is_hub = name.endswith("-hub.ts") or name.startswith("hub-")
                home_dest = (hub_ext if is_hub else agent_ext) / name
            else:
                continue
            gen = gen_ext / name
            sources[gen] = entry
            desired[home_dest] = gen

    for gen, src in sources.items():
        _sync_generated_any(src, gen)
    # Retiring a source is NOT done here: the generated copy is dropped in the
    # commit phase at the end of the run, after the home links that point at it
    # have been removed (see HomeManifest.commit_generated).
    return LinkPlan(desired, [pi_agents, agent_ext, hub_ext])


def plan_herdr_config(root: Path) -> LinkPlan:
    """Managed whole-file Herdr config under the standard XDG config path, served
    from the durable generated copy rather than from the repo checkout."""
    source = root / "herdr-config.toml"
    destination = HOME / ".config/herdr/config.toml"
    generated = generated_root() / "herdr-config.toml"
    if not source.is_file():
        # The stale generated copy is dropped by the end-of-run commit phase,
        # after its home link is gone — never before.
        return LinkPlan({}, [destination.parent])
    _sync_generated_file(source, generated)
    return LinkPlan({destination: generated}, [destination.parent])


def reconcile(
    plan: LinkPlan,
    family: str,
    *,
    plan_only: bool,
    force: bool,
    repo_root: Path | None = None,
    protect: set[Path] | None = None,
) -> collections.Counter:
    counts: collections.Counter = collections.Counter()
    tag = "plan" if plan_only else "done"

    def emit(kind: str, dest: Path, src: Path | None) -> None:
        counts[kind] += 1
        arrow = f" -> {src}" if src is not None else ""
        print(f"  [{tag}] {kind}: {dest}{arrow}")
        if kind == "skip-foreign" and dest.is_symlink():
            target = os.readlink(dest)
            # Judge the RESOLVED target too: a relative link or a chain that
            # lands in the store is just as home-manager-owned as a literal one.
            resolved = os.path.realpath(dest)
            if target.startswith("/nix/store/") or resolved.startswith("/nix/store/"):
                print(
                    f"  [WARN] {dest} is managed by Nix/home-manager ({target}) — "
                    "two managers own one path; remove it from home.nix or from "
                    "this kit's plan",
                    file=sys.stderr,
                )

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
            if protect and entry in protect:
                continue  # deployed by another step of this same run
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


def _parse_placeholder_args(items: list[str] | None) -> dict[str, str]:
    """Parse repeated `--placeholder NAME=VALUE` CLI args into a fill map.

    Used only by the low-level `compose` CLI so the kit can fill a kit-synced
    layer's {{TOKEN}} when composing ITSELF (e.g. `OPS_REPO=your-repo-ops` when
    regenerating the kit's own tracked slash-command skills). The config-driven
    per-destination flow fills tokens from each destination's `placeholders:`
    map, not from here."""
    out: dict[str, str] = {}
    for it in items or []:
        if "=" not in it:
            sys.exit(f"error: --placeholder must be NAME=VALUE, got: {it!r}")
        name, value = it.split("=", 1)
        out[name] = value
    return out


def cmd_compose(args: argparse.Namespace) -> None:
    shared_root = find_shared_llm(args.shared_llm)
    # Inputs resolve against the repo that owns the .shared-llm we found (its
    # parent). Outputs default to that same repo (consumer flow: read and write
    # one repo). An explicit --target redirects ONLY where outputs land — used by
    # the kit self-compose to stage into examples/ — while inputs still resolve
    # against the real repo root.
    repo_root = shared_root.parent
    output_base = Path(args.target).expanduser().resolve() if args.target else repo_root
    placeholders = _parse_placeholder_args(getattr(args, "placeholder", None))
    composer = Composer(
        repo_root,
        output_base=output_base,
        shared_root=shared_root,
        placeholders=placeholders,
    )

    print(f"shared-llm source: {shared_root}")
    print(f"repo root: {repo_root}")
    print(f"output base: {output_base}")

    if args.recipe:
        recipe = Path(args.recipe)
        if not recipe.is_absolute():
            # A recipe path may be given repo-relative (`.shared-llm/public/compose/...`)
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
ENGINE_ROOT = Path(__file__).resolve().parent.parent
# "cursor" is the Cursor Agent CLI (`cursor-agent`). It consumes exactly the
# Codex surfaces — the root AGENTS.md instruction file and skills discovered
# from ~/.agents/skills (plus .claude/skills for compat) — so everywhere the
# plumbing routes by harness, cursor rides the codex path (see
# wants_codex_surface / _codex_surface_tokens). No cursor-specific dirs exist.
VALID_HARNESSES = ("cc", "pi", "codex", "cursor")
UPAGENT_ROSTER_REL = Path("extensions/common/upagent/offerings.yaml")
_UPAGENT_OFFERINGS_MODULE: Any | None = None


def _upagent_offerings_module() -> Any:
    """Load the code-owned offering-set policy from this kit checkout."""
    global _UPAGENT_OFFERINGS_MODULE
    if _UPAGENT_OFFERINGS_MODULE is not None:
        return _UPAGENT_OFFERINGS_MODULE
    path = ENGINE_ROOT / ".shared-llm/public/extensions/common/upagent/offerings.py"
    spec = importlib.util.spec_from_file_location("llm_config_setup_offerings", path)
    if spec is None or spec.loader is None:
        sys.exit(f"error: could not load UpAgent offering policy: {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    _UPAGENT_OFFERINGS_MODULE = module
    return module


def _validated_upagent_block(value: object, where: str) -> tuple[str, ...]:
    offerings = _upagent_offerings_module()
    if not isinstance(value, dict):
        sys.exit(f"error: {where}.upagent must be a mapping")
    unknown = sorted(set(value) - {"offering_sets"})
    if unknown:
        sys.exit(f"error: {where}.upagent has unknown keys: {', '.join(unknown)}")
    if "offering_sets" not in value:
        sys.exit(f"error: {where}.upagent must define offering_sets")
    raw_sets = value["offering_sets"]
    try:
        return tuple(offerings.normalize_offering_sets(raw_sets))
    except offerings.OfferingError as error:
        raise SystemExit(f"error: {where}: {error}") from error


def selected_offering_sets(
    cfg: dict[str, Any], destination: dict[str, Any] | None = None
) -> tuple[str, ...]:
    offerings = _upagent_offerings_module()
    machine = (
        _validated_upagent_block(cfg["upagent"], "config")
        if "upagent" in cfg
        else tuple(offerings.DEFAULT_SETS)
    )
    if destination is None or "upagent" not in destination:
        return machine
    return _validated_upagent_block(destination["upagent"], "destination")


def has_configured_update_work(cfg: dict[str, Any]) -> bool:
    return bool(cfg["destinations"] or cfg["global"] or "upagent" in cfg)


def _write_if_changed(path: Path, content: str) -> bool:
    # A leaf symlink is not a managed generated file even when its target has
    # equal bytes. Replace the link itself instead of adopting its external target.
    if path.is_file() and not path.is_symlink() and path.read_text() == content:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(
        dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_name, path)
    except BaseException:
        with suppress(OSError):
            os.unlink(temporary_name)
        raise
    _fsync_dir(path.parent)
    return True


def materialize_offering_roster(selected_sets: tuple[str, ...], target: Path) -> bool:
    # Home rosters live in the managed generated tree. Apply the same symlink
    # and ownership guard as every other generated artifact before writing.
    try:
        target.relative_to(generated_root())
    except ValueError:
        pass
    else:
        _require_generated_path(target)
    offerings = _upagent_offerings_module()
    source_dir = ENGINE_ROOT / ".shared-llm/public/extensions/common/upagent"
    try:
        rendered = offerings.render_roster(selected_sets, source_dir)
    except offerings.OfferingError as error:
        raise SystemExit(
            f"error: could not render UpAgent offering roster: {error}"
        ) from error
    return _write_if_changed(target, rendered)


def wants_codex_surface(harnesses) -> bool:
    """True when a harness list requires the Codex deployment surface
    (AGENTS.md + ~/.agents/skills). Both the codex CLI and the Cursor Agent
    CLI read those same paths, so 'cursor' is an alias at the plumbing level."""
    return "codex" in harnesses or "cursor" in harnesses


# The ONLY trees `copy` propagates wholesale: pure common layer + runtime
# content. It never touches a destination's this_repo/ overlays. In the split
# destination layout (see below) these land under <dest>/.shared-llm/public/.
# Paths are relative to a `.shared-llm/` dir.
COMMON_ROOTS = (
    "layers/agents/common",
    "layers/llm/common",
    "layers/skills/common",
    "layers/slash-commands/common",
    "llm/claude/common",
    "llm/common/common",
    "llm/pi/common",
    # Public tool-module extensions (justfile-imported dirs like upagent/ and the
    # ported specialist/): kit-owned, synced to a destination's public/extensions/common/.
    # Repo-owned tool modules stay under extensions/this_repo/ and are never synced.
    "extensions/common",
)

# ---------------------------------------------------------------------------
# Destination ownership split (public/ vs this_repo/)
# ---------------------------------------------------------------------------
#
# A configured destination's `.shared-llm/` is split into two trees with an
# explicit ownership boundary:
#
#   public/     — KIT-SYNCED. The kit's common layers, its runtime trees
#                 (llm/{claude,common,pi}/common), and its compose recipes are
#                 copied here on every `just update`. The engine sweeps and
#                 (for layers + recipes) PRUNES it wholesale — a destination must
#                 never hand-edit it, because the next update overwrites it.
#   this_repo/  — REPO-OWNED. The repo's this_repo layer overlays, its own
#                 compose recipes, prompts, standalone skills, and extensions
#                 live here. The engine NEVER writes or prunes anything under it.
#
# Recipes reference layers across both trees BY EXPLICIT PATH — a recipe under
# public/compose/ may pull a this_repo overlay via `.shared-llm/this_repo/...`,
# and a repo recipe may pull a common layer via `.shared-llm/public/...`. The
# input resolver stays the one repo-root-relative rule (Composer.resolve_input);
# the split is expressed in the path itself, not in a forking resolver.
#
# The KIT's own source tree is public-rooted too (`.shared-llm/public/layers/...`),
# matching a destination's public/ tree 1:1. Kit recipes are authored with public
# paths, so the copy-time translation (translate_shared_path / _translate_recipe_text)
# is identity for kit content — it still routes any legacy flat path, but the kit no
# longer ships one.
PUBLIC_DIR = "public"
THIS_REPO_DIR = "this_repo"
SHARED_PREFIX = ".shared-llm/"

# Which public/ subtrees the wholesale copy also PRUNES (removes dest files the
# source no longer ships). Layers are pure source, safe to prune. The runtime
# trees (llm/) are copy-overwrite only — they accrue build artifacts
# (node_modules, compiled hub binaries) that are not kit-provided and must not be
# nuked. Recipes under public/compose/ are pruned by _sync_public_recipes.
PRUNABLE_PUBLIC_LAYER_PREFIX = "layers"


def _split_scope(rel_under_shared: str) -> str:
    """`this_repo` or `public` for a path written relative to a `.shared-llm/`
    dir. A path that contains a `this_repo` component is repo-owned; everything
    else is kit-synced (public)."""
    return THIS_REPO_DIR if "this_repo" in Path(rel_under_shared).parts else PUBLIC_DIR


def translate_shared_path(path: str) -> str:
    """Translate a flat kit path (`.shared-llm/<rel>`) into its split-layout form
    (`.shared-llm/<public|this_repo>/<rel>`). A non-`.shared-llm/` path (repo
    content like `ops/...`), and a path already under public/ or this_repo/, pass
    through unchanged."""
    if not path.startswith(SHARED_PREFIX):
        return path
    rest = path[len(SHARED_PREFIX) :]
    first = rest.split("/", 1)[0]
    if first in (PUBLIC_DIR, THIS_REPO_DIR):
        return path  # already split
    return f"{SHARED_PREFIX}{_split_scope(rest)}/{rest}"


# A kit recipe's INPUT-side path fields. `output`/`name`/frontmatter are NOT
# translated (outputs land at the repo root; names/frontmatter carry no paths).
RECIPE_PATH_FIELDS = ("description", "catalog", "resources")


def _translate_recipe_text(text: str, data: dict) -> str:
    """Rewrite a kit recipe's input-side path fields from flat to split form,
    preserving the file's original formatting (targeted string replacement, not a
    YAML round-trip). Paths are replaced longest-first so a shorter path can never
    corrupt a longer one that contains it as a prefix."""
    paths: set[str] = set()
    for rel in data.get("inputs") or []:
        if isinstance(rel, str):
            paths.add(rel)
    for key in RECIPE_PATH_FIELDS:
        v = data.get(key)
        if isinstance(v, str):
            paths.add(v)
    for p in sorted(paths, key=len, reverse=True):
        t = translate_shared_path(p)
        if t != p:
            text = text.replace(p, t)
    return text


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


class _NoDuplicateKeyLoader(yaml.SafeLoader):
    """Safe machine-config loader that rejects duplicate keys at every level."""


def _construct_unique_config_mapping(
    loader: _NoDuplicateKeyLoader, node: yaml.MappingNode, deep: bool = False
) -> dict[object, object]:
    loader.flatten_mapping(node)
    result: dict[object, object] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        try:
            duplicate = key in result
        except TypeError as error:
            raise yaml.constructor.ConstructorError(
                "while constructing a mapping",
                node.start_mark,
                "found an unhashable mapping key",
                key_node.start_mark,
            ) from error
        if duplicate:
            raise yaml.constructor.ConstructorError(
                "while constructing a mapping",
                node.start_mark,
                f"found duplicate key {key!r}",
                key_node.start_mark,
            )
        result[key] = loader.construct_object(value_node, deep=deep)
    return result


_NoDuplicateKeyLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _construct_unique_config_mapping
)


def load_config() -> dict:
    cfg: dict = {}
    if CONFIG_PATH.exists():
        try:
            loaded = (
                yaml.load(CONFIG_PATH.read_text(), Loader=_NoDuplicateKeyLoader) or {}
            )
        except (OSError, yaml.YAMLError) as error:
            sys.exit(
                f"error: config {CONFIG_PATH} is unreadable or invalid YAML: {error}"
            )
        if not isinstance(loaded, dict):
            sys.exit(f"error: config {CONFIG_PATH} must be a YAML mapping")
        cfg = loaded
    cfg.setdefault("source", str(DEFAULT_SOURCE))
    cfg.setdefault("global", [])
    cfg.setdefault("destinations", [])
    cfg.setdefault("exclude", [])
    if not isinstance(cfg["destinations"], list) or not all(
        isinstance(destination, dict) for destination in cfg["destinations"]
    ):
        sys.exit("error: config destinations must be a list of mappings")
    selected_offering_sets(cfg)
    for index, destination in enumerate(cfg["destinations"], start=1):
        if "upagent" in destination:
            _validated_upagent_block(destination["upagent"], f"destinations[{index}]")
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
            sys.exit(
                f"error: unknown harness '{h}' (valid: {', '.join(VALID_HARNESSES)})"
            )
        if h not in out:
            out.append(h)
    return out


def _print_config(cfg: dict) -> None:
    print(f"  source: {cfg['source']}")
    print(f"  global: {', '.join(cfg['global']) or '(none)'}")
    print(f"  exclude: {', '.join(cfg.get('exclude', [])) or '(none)'}")
    print(f"  upagent offering sets: {', '.join(selected_offering_sets(cfg))}")
    if not cfg["destinations"]:
        print("  destinations: (none)")
    for d in cfg["destinations"]:
        replacement = (
            f"  upagent=[{', '.join(selected_offering_sets(cfg, d))}]"
            if "upagent" in d
            else ""
        )
        print(
            f"  dest: {d['path']}  [{', '.join(d.get('harnesses', []))}]{replacement}"
        )


def cmd_configure(args: argparse.Namespace) -> None:
    existed = CONFIG_PATH.exists()
    cfg = load_config()
    if args.source:
        cfg["source"] = str(Path(args.source).expanduser())
    if args.global_list is not None:
        cfg["global"] = parse_harnesses(args.global_list)
    if args.exclude is not None:
        cfg["exclude"] = [
            p.strip().strip("/") for p in args.exclude.split(",") if p.strip()
        ]
    offering_sets_arg = getattr(args, "offering_sets", None)
    parsed_offering_sets = None
    if offering_sets_arg is not None:
        raw_sets = [
            item.strip() for item in offering_sets_arg.split(",") if item.strip()
        ]
        offerings = _upagent_offerings_module()
        try:
            parsed_offering_sets = list(offerings.normalize_offering_sets(raw_sets))
        except offerings.OfferingError as error:
            raise SystemExit(f"error: {error}") from error
    if args.dest:
        path = str(Path(args.dest).expanduser().resolve())
        requested_harnesses = parse_harnesses(args.list) if args.list else None
        for d in cfg["destinations"]:
            if d.get("path") == path:
                # Setting only a destination offering-set replacement must not
                # silently reset its existing harness list to the configure default.
                if requested_harnesses is not None:
                    d["harnesses"] = requested_harnesses
                if parsed_offering_sets is not None:
                    d["upagent"] = {"offering_sets": parsed_offering_sets}
                break
        else:
            harnesses = requested_harnesses or ["cc", "pi"]
            destination: dict[str, Any] = {"path": path, "harnesses": harnesses}
            if parsed_offering_sets is not None:
                destination["upagent"] = {"offering_sets": parsed_offering_sets}
            cfg["destinations"].append(destination)
    elif parsed_offering_sets is not None:
        cfg["upagent"] = {"offering_sets": parsed_offering_sets}
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
        hint = {"mac": "brew install", "ubuntu": "sudo apt install"}.get(
            args.os, "install"
        )
        sys.exit(
            f"error: missing prerequisite(s): {', '.join(missing)}  (try: {hint} {' '.join(missing)})"
        )
    print(
        "init: all prerequisites present. Next: `just configure -s ~/.shared-llm`, `just configure -g cc,pi`, then `just update`."
    )
    print(
        "init: pin Herdr 0.7.1 with `just herdr-pin` (do not `herdr update`; newer Herdr breaks UpAgent)."
    )
    print(
        "init: for the Pi harness, `npm install -g @earendil-works/pi-coding-agent` then `just pi-extensions`."
    )
    print(
        "init: third-party skills (Lavish, Impeccable, …): `just misc`. unslop comes from `just update`."
    )


# --- copy ------------------------------------------------------------------

# Build/cache artifacts never propagated by common copies or packaged skill
# resources (a fresh machine builds its own). node_modules can be huge; a
# compiled binary can be a running process (copying over it fails EBUSY /
# "Text file busy").
ARTIFACT_DIR_NAMES = frozenset(
    {"node_modules", "__pycache__", ".ruff_cache", ".pytest_cache"}
)
ARTIFACT_FILE_SUFFIXES = (".pyc", ".pyo", ".pyd")


def _is_artifact_rel(rel: Path) -> bool:
    return bool(
        ARTIFACT_DIR_NAMES.intersection(rel.parts)
        or rel.name == ".DS_Store"
        or rel.name.endswith(ARTIFACT_FILE_SUFFIXES)
    )


def _git_ignored_common_rels(kit_shared: Path) -> frozenset[str]:
    """Relative paths (under the kit's public content root) that git ignores in
    the KIT — build artifacts (e.g. a compiled hub binary the kit .gitignore
    lists) that must never propagate to the hub or a destination.

    Git discovery is authoritative when the kit is a worktree. Archive/non-git
    checkouts are supported, but the missing discovery is announced on stderr so
    ignored-resource filtering can never be mistaken for having succeeded.
    """
    # kit_shared is <kit>/.shared-llm/public; its parent (.shared-llm) is inside
    # the git repo, so `git -C` discovers the repo and lists paths relative to
    # that cwd — i.e. prefixed with `public/`, which we strip below.
    cwd = kit_shared.parent
    try:
        in_worktree = subprocess.run(
            ["git", "-C", str(cwd), "rev-parse", "--is-inside-work-tree"],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        print(
            "warning: git discovery unavailable for public resource exclusions; "
            "continuing as an archive/non-git checkout without gitignored-path filtering",
            file=sys.stderr,
        )
        return frozenset()
    if in_worktree.returncode != 0 or in_worktree.stdout.strip() != "true":
        print(
            "warning: git discovery unavailable for public resource exclusions; "
            "continuing as an archive/non-git checkout without gitignored-path filtering",
            file=sys.stderr,
        )
        return frozenset()
    try:
        out = subprocess.run(
            [
                "git",
                "-C",
                str(cwd),
                "ls-files",
                "--others",
                "--ignored",
                "--exclude-standard",
                "-z",
                "--",
                str(kit_shared),
            ],
            capture_output=True,
            text=True,
            check=True,
        ).stdout
    except (OSError, subprocess.CalledProcessError):
        sys.exit(
            "error: git ignored-path discovery failed for public resources; "
            "cannot safely apply .gitignore exclusions"
        )
    prefix = kit_shared.name + "/"  # "public/"
    return frozenset(p[len(prefix) :] for p in out.split("\0") if p.startswith(prefix))


def _iter_common_rels(shared: Path, exclude_rels: frozenset[str] = frozenset()):
    """Yield each common file's path relative to a `.shared-llm/` dir. Skips
    anything under a this_repo/ segment (defense in depth), any build-artifact
    directory (node_modules), and any path in `exclude_rels` (git-ignored kit
    artifacts)."""
    for root in COMMON_ROOTS:
        base = shared / root
        if not base.is_dir():
            continue
        for p in sorted(base.rglob("*")):
            if not p.is_file():
                continue
            rel = p.relative_to(shared)
            if "this_repo" in rel.parts or _is_artifact_rel(rel):
                continue
            if str(rel) in exclude_rels:
                continue
            yield rel


def _copy_common(
    src_shared: Path,
    dst_shared: Path,
    log: RunLog,
    dst_subdir: str = "",
    exclude_rels: frozenset[str] = frozenset(),
) -> collections.Counter:
    """Copy every common file from one `.shared-llm/` dir to another. Overwrites;
    FLAGS (does not block) when an existing dest file's content differs before it
    is overwritten. Never touches this_repo/. `exclude_rels` (git-ignored kit
    artifacts) and build-artifact dirs are skipped.

    `dst_subdir` prefixes the destination side — the split destination flow passes
    `public` so kit content lands under `<dest>/.shared-llm/public/`. It is empty
    for the flat kit -> hub mirror. Pruning of stale files is handled separately
    by `_prune_public_layers` (layers only; runtime trees keep build artifacts)."""
    counts: collections.Counter = collections.Counter()
    prefix = Path(dst_subdir) if dst_subdir else Path()
    for rel in _iter_common_rels(src_shared, exclude_rels):
        src = src_shared / rel
        dst = dst_shared / prefix / rel
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


def _prune_empty_dirs(root: Path) -> None:
    """Remove now-empty directories under `root` (bottom-up), leaving `root`."""
    if not root.is_dir():
        return
    for d in sorted(
        (p for p in root.rglob("*") if p.is_dir()),
        key=lambda p: len(p.parts),
        reverse=True,
    ):
        with suppress(OSError):
            d.rmdir()  # not empty — keep


def _prune_hub_slash_command_layers(kit_shared: Path, hub: Path, log: RunLog) -> int:
    """Keep the hub's slash-command layers an exact kit mirror.

    The hub may carry additive generic layers, but it must not preserve a retired
    command layer: otherwise a removed runner can be copied back into every
    destination even after its recipe was pruned. Repo-specific commands belong
    under a destination's this_repo tree, never the shared hub.
    """
    source = kit_shared / "layers/slash-commands"
    target = hub / "layers/slash-commands"
    if not source.is_dir() or not target.is_dir():
        return 0
    wanted = {path.relative_to(source) for path in source.rglob("*") if path.is_file()}
    removed = 0
    for path in sorted(target.rglob("*")):
        if path.is_file() and path.relative_to(target) not in wanted:
            path.unlink()
            removed += 1
            log(f"    - pruned stale hub slash-command layer: {path.relative_to(hub)}")
    if removed:
        _prune_empty_dirs(target)
    return removed


def _prune_public_layers(src_shared: Path, dst_shared: Path, log: RunLog) -> int:
    """Sweep the destination's public/layers/ wholesale: delete any file there
    that the source (hub) no longer ships. Scoped to layers/ — pure source that
    is safe to prune. The runtime trees under public/llm/ are copy-overwrite only
    (they accrue build artifacts that are not kit-provided)."""
    pub_layers = dst_shared / PUBLIC_DIR / PRUNABLE_PUBLIC_LAYER_PREFIX
    if not pub_layers.is_dir():
        return 0
    wanted = {
        rel
        for rel in _iter_common_rels(src_shared)
        if rel.parts and rel.parts[0] == PRUNABLE_PUBLIC_LAYER_PREFIX
    }
    if not wanted:
        # The source ships no layers at all — never interpret that as "prune
        # everything" (a mis-set hub would nuke a destination's public/). Refuse.
        return 0
    removed = 0
    for f in sorted(pub_layers.rglob("*")):
        if not f.is_file():
            continue
        rel = f.relative_to(dst_shared / PUBLIC_DIR)
        if rel not in wanted:
            f.unlink()
            removed += 1
            log(f"    - pruned stale public layer: {rel}")
    if removed:
        _prune_empty_dirs(pub_layers)
    return removed


# Kit compose groups synced wholesale into a destination's public/compose/.
# (global/ is home-only — it never goes to a destination.) A recipe that lives
# under one of these groups in the KIT is kit-owned: it is copied (translated to
# split paths) into public/compose/, and a public/compose/ recipe the kit no
# longer ships is pruned. A destination's OWN recipes live in this_repo/compose/
# and are never touched here — a repo overrides a kit recipe by placing one at the
# same relative path under this_repo/compose/ (compose composes public first, then
# this_repo, so the repo copy wins).
PUBLIC_RECIPE_GROUPS = (
    "compose/agents",
    "compose/agents-md",
    "compose/claude-md",
    "compose/skills",
    "compose/slash-commands",
)


def _recipe_layer_refs(data: dict) -> list[str]:
    """The `.shared-llm/...` layer files a recipe reads (inputs + description +
    catalog). Used to gate syncing a recipe whose layers are not all present."""
    refs = [r for r in (data.get("inputs") or []) if isinstance(r, str)]
    for key in ("description", "catalog"):
        v = data.get(key)
        if isinstance(v, str):
            refs.append(v)
    return [r for r in refs if r.startswith(SHARED_PREFIX)]


def _sync_public_recipes(
    kit_shared: Path, dest_shared: Path, log: RunLog, exclude: list[str]
) -> collections.Counter:
    """Wholesale-sync the kit's recipes into a destination's public/compose/,
    translating each recipe's input-side paths from flat to split form, and prune
    any public/compose/ recipe the kit no longer ships. this_repo/compose/ is
    never read or written here.

    A kit recipe is synced only when every layer it references (translated to
    split form) already exists at the destination — a recipe that pulls a
    this_repo overlay the repo has not filled in is skipped, so composing it can
    never fail on a missing input (this preserves the additive-seeding guard from
    the pre-split copy).

    A recipe whose kit source path is in `exclude` (see `_is_excluded`) is never
    synced. It is also never added to `wanted`, so the prune sweep below removes
    any stale copy already at the destination — exactly as if the kit dropped it."""
    counts: collections.Counter = collections.Counter()
    dest_root = dest_shared.parent
    pub_compose = dest_shared / PUBLIC_DIR / "compose"
    wanted: set[Path] = set()
    for group in PUBLIC_RECIPE_GROUPS:
        base = kit_shared / group
        if not base.is_dir():
            continue
        for src in sorted(list(base.rglob("*.yaml")) + list(base.rglob("*.yml"))):
            rel = src.relative_to(kit_shared)  # e.g. compose/agents/backend.yaml
            if _is_excluded(src, kit_shared, exclude):
                # Config-excluded: skip, and leave it out of `wanted` so the prune
                # sweep below drops any stale copy already at the destination.
                log(f"    exclude: {rel}")
                continue
            dst = dest_shared / PUBLIC_DIR / rel
            text = src.read_text()
            try:
                data = yaml.safe_load(text)
            except yaml.YAMLError:
                counts["skipped"] += 1
                log(f"    ! recipe unreadable, skipped: {rel}")
                continue
            if not isinstance(data, dict):
                counts["skipped"] += 1
                continue
            missing = [
                translate_shared_path(r)
                for r in _recipe_layer_refs(data)
                if not (dest_root / translate_shared_path(r)).is_file()
            ]
            if missing:
                counts["skipped"] += 1
                log(
                    f"    ! recipe skipped (missing inputs at destination): {rel} -> {', '.join(missing)}"
                )
                continue
            wanted.add(dst)
            translated = _translate_recipe_text(text, data)
            if dst.exists() and dst.read_text() == translated:
                counts["same"] += 1
                continue
            counts["changed" if dst.exists() else "new"] += 1
            log(f"    {'~ updated' if dst.exists() else '+ new'} public recipe: {rel}")
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.write_text(translated)
    if pub_compose.is_dir():
        for f in sorted(pub_compose.rglob("*")):
            if f.is_file() and f not in wanted:
                f.unlink()
                counts["pruned"] += 1
                log(f"    - pruned stale public recipe: {f.relative_to(dest_shared)}")
        _prune_empty_dirs(pub_compose)
    return counts


def do_copy(cfg: dict, log: RunLog) -> None:
    kit_shared = project_root() / ".shared-llm" / PUBLIC_DIR
    hub = Path(cfg["source"]).expanduser()
    # Build artifacts the kit .gitignore lists (e.g. a compiled hub binary) must
    # never propagate — a fresh machine builds its own, and a running binary can't
    # be overwritten ("Text file busy").
    ignored = _git_ignored_common_rels(kit_shared)
    exclude = cfg.get("exclude", [])
    log.always(f"copy: kit {kit_shared} -> hub {hub}")
    c = _copy_common(kit_shared, hub, log, exclude_rels=ignored)
    pruned_hub_commands = _prune_hub_slash_command_layers(kit_shared, hub, log)
    log.always(
        f"  hub: {c['new']} new, {c['changed']} changed, {c['same']} unchanged, "
        f"{pruned_hub_commands} retired slash-command layers pruned"
    )
    for d in cfg["destinations"]:
        dest = Path(d["path"]).expanduser()
        dest_shared = dest / ".shared-llm"
        name = Path(d["path"]).name
        log.always(f"copy: hub -> {d['path']}/.shared-llm/{PUBLIC_DIR}")
        c = _copy_common(
            hub, dest_shared, log, dst_subdir=PUBLIC_DIR, exclude_rels=ignored
        )
        pruned_layers = _prune_public_layers(hub, dest_shared, log)
        log.always(
            f"  {name}: {c['new']} new, {c['changed']} changed, "
            f"{c['same']} unchanged, {pruned_layers} pruned"
        )
        r = _sync_public_recipes(kit_shared, dest_shared, log, exclude)
        log.always(
            f"  {name} public recipes: {r['new']} new, {r['changed']} updated, "
            f"{r['same']} unchanged, {r['pruned']} pruned, {r['skipped']} skipped"
        )
        roster_target = dest_shared / PUBLIC_DIR / UPAGENT_ROSTER_REL
        selected_sets = selected_offering_sets(cfg, d)
        changed = materialize_offering_roster(selected_sets, roster_target)
        log.always(
            f"  {name} UpAgent offerings: {', '.join(selected_sets)} "
            f"({'updated' if changed else 'unchanged'})"
        )


# --- compose (config-driven, per destination) ------------------------------

# Consumer recipe groups composed for a destination (never global/ or example-*).
# File groups named …/root.yaml compose that public file only, so kit
# example-package / example-service recipes stay out of consumer trees. For the
# matching this_repo/ tree, the whole sibling directory is composed instead
# (root.yaml override plus dest-owned nested CLAUDE.md / AGENTS.md recipes).
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

# public file group → this_repo directory that owns nested md recipes.
THIS_REPO_NESTED_MD_GROUPS = {
    "compose/claude-md/root.yaml": "claude-md",
    "compose/agents-md/root.yaml": "agents-md",
}


# Per-harness skill routing is by NAME PREFIX (matches the retired install-global):
#   do-*  -> Pi ONLY      cc-*  -> Claude Code ONLY      (anything else) -> both
# Claude Code reads <repo>/.claude/skills directly, so to keep do-* OUT of Claude
# they are routed out of .claude/skills into a Pi-only source dir after compose.
# Pi links do-* from there; common skills stay in .claude/skills for both.
PI_ONLY_SKILLS_DIR = ".pi-skills"


def _declared_skill_names(dest: Path, prefix: str) -> set[str]:
    """Return composer-managed slash-command skill names with ``prefix``."""
    names: set[str] = set()
    shared = dest / ".shared-llm"
    for compose_root in (
        shared / PUBLIC_DIR / "compose",
        shared / THIS_REPO_DIR / "compose",
    ):
        if not compose_root.is_dir():
            continue
        for recipe in compose_root.rglob("*.yaml"):
            try:
                data = yaml.safe_load(recipe.read_text())
            except yaml.YAMLError:
                continue
            if isinstance(data, dict) and data.get("type", "skill") == "skill":
                name = data.get("name")
                if isinstance(name, str) and name.startswith(prefix):
                    names.add(name)
    return names


def _prune_removed_cc_skills(dest: Path, log: RunLog) -> int:
    """Remove stale composer-managed Claude workflow skills after recipe deletion."""
    claude_skills = dest / ".claude/skills"
    declared = _declared_skill_names(dest, "cc-")
    removed = 0
    if not claude_skills.is_dir():
        return removed
    for stale in sorted(claude_skills.iterdir()):
        if (
            stale.is_dir()
            and stale.name.startswith("cc-")
            and stale.name not in declared
        ):
            shutil.rmtree(stale)
            removed += 1
            log(f"    - pruned stale Claude cc-* skill: .claude/skills/{stale.name}")
    return removed


# A retired recipe leaves a tracked generated output behind. Restrict cleanup to
# an unmistakable signature so an unrelated manually maintained `team` skill is
# never removed.
LEGACY_RUNNER_SKILL_MARKERS = {
    "team": ("ask_brain", "meta-orchestrator"),
    "meta-cc-plan-and-grill": ("name: meta-cc-plan-and-grill",),
    "meta-plan-check": ("name: meta-plan-check",),
    "meta-plan-convert": ("name: meta-plan-convert",),
}


def _prune_removed_legacy_runner_skills(dest: Path, log: RunLog) -> int:
    """Remove unreferenced legacy runner skills that retain their old signature."""
    claude_skills = dest / ".claude/skills"
    if not claude_skills.is_dir():
        return 0
    declared = _declared_skill_names(dest, "")
    removed = 0
    for name, markers in LEGACY_RUNNER_SKILL_MARKERS.items():
        skill_dir = claude_skills / name
        skill = skill_dir / "SKILL.md"
        if name in declared or not skill.is_file():
            continue
        if not any(marker in skill.read_text() for marker in markers):
            continue
        shutil.rmtree(skill_dir)
        removed += 1
        log(f"    - pruned stale legacy runner skill: .claude/skills/{name}")
    return removed


def _route_do_skills(dest: Path, log: RunLog) -> int:
    """Move composed do-* skill dirs OUT of .claude/skills (which Claude Code
    reads) into <repo>/.pi-skills, so do-* are Pi-only. Idempotent: compose
    rewrites .claude/skills/do-* each run and this relocates them."""
    claude_skills = dest / ".claude/skills"
    pi_src = dest / PI_ONLY_SKILLS_DIR
    moved = 0
    declared = _declared_skill_names(dest, "do-")
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
    # The directory is composer-managed. A removed recipe no longer creates its
    # source dir above, so remove the old routed output rather than leaving an
    # executable stale Pi command or a global link to it.
    if pi_src.is_dir():
        for stale in sorted(pi_src.iterdir()):
            if (
                stale.is_dir()
                and stale.name.startswith("do-")
                and stale.name not in declared
            ):
                shutil.rmtree(stale)
                log(
                    f"    - pruned stale routed do-* skill: {PI_ONLY_SKILLS_DIR}/{stale.name}"
                )
    return moved


def _compose_destination(
    dest: Path, log: RunLog, placeholders: dict[str, str] | None = None
) -> None:
    shared = dest / ".shared-llm"
    if not shared.is_dir():
        log.always(f"compose: skip {dest} — no .shared-llm/ (run copy/configure first)")
        return
    composer = Composer(dest, shared_root=shared, placeholders=placeholders)
    log.always(f"compose: {dest}")
    for group in CONSUMER_RECIPE_GROUPS:
        # group is "compose/skills" (a dir) or "compose/claude-md/root.yaml" (a file).
        # Compose the public/ (kit) tree FIRST, then the this_repo/ (repo-owned)
        # tree — same-output recipes let the this_repo copy win, so a repo can
        # override a kit recipe by name. A group absent from a tree is skipped.
        rel = group[len("compose/") :]
        nested_rel = THIS_REPO_NESTED_MD_GROUPS.get(group)
        for tree in (PUBLIC_DIR, THIS_REPO_DIR):
            if tree == THIS_REPO_DIR and nested_rel is not None:
                path = shared / THIS_REPO_DIR / "compose" / nested_rel
            else:
                path = shared / tree / "compose" / rel
            if path.is_dir():
                for yp in composer.discover(path):
                    composer.compose_one(yp)
            elif path.is_file():
                composer.compose_one(path)
    composer.copy_standalone_skills(shared / THIS_REPO_DIR / "skills")
    pruned_cc = _prune_removed_cc_skills(dest, log)
    pruned_legacy = _prune_removed_legacy_runner_skills(dest, log)
    moved = _route_do_skills(dest, log)
    if pruned_cc:
        log.always(f"  pruned {pruned_cc} stale cc-* skill(s) from .claude/skills")
    if pruned_legacy:
        log.always(
            f"  pruned {pruned_legacy} stale legacy runner skill(s) from .claude/skills"
        )
    if moved:
        log.always(
            f"  routed {moved} do-* skill(s) to {PI_ONLY_SKILLS_DIR}/ (Pi-only, out of Claude's .claude/skills)"
        )


def do_compose(cfg: dict, log: RunLog) -> None:
    for d in cfg["destinations"]:
        _compose_destination(
            Path(d["path"]).expanduser(), log, placeholders=d.get("placeholders") or {}
        )


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
    # Common skills and Pi-scoped helpers stay in .claude/skills — never cc-*,
    # never do-*. Pi-scoped helpers are intentionally excluded from Codex.
    for d in _skill_dirs(dest):
        if d.name.startswith("cc-") or d.name.startswith("do-"):
            continue
        if harness_of(dest, d.name) in ("common", "pi"):
            out[d.name.replace(":", "-")] = d
    return out


def _common_codex_skills(dest: Path) -> dict[str, Path]:
    """Codex gets common + codex-scoped skills from .claude/skills — never do-*
    (Pi-only) and never cc-* (Claude-only)."""
    out: dict[str, Path] = {}
    for d in _skill_dirs(dest):
        if d.name.startswith("cc-") or d.name.startswith("do-"):
            continue
        if harness_of(dest, d.name) in ("common", "codex", "cursor"):
            out[d.name] = d
    return out


def _destination_skill_roots(dest: Path) -> tuple[Path, Path]:
    """The only two dirs in a destination repo this kit ever links a home skill
    to: its composed skills, and the Pi-only routed ones."""
    return (dest / ".claude/skills", dest / PI_ONLY_SKILLS_DIR)


def _resolves_into(link: Path, roots: list[Path]) -> bool:
    """True iff `link` is a symlink whose canonical target is a skill dir we
    would actually deploy from one of `roots` — a DIRECT child of
    `<dest>/.claude/skills/` or `<dest>/.pi-skills/`. Never true for a real file.

    "Somewhere below a configured repo" is not ownership: a user's own link to
    `<repo>/README.md` lives under the destination too, and prune-by-containment
    would delete it. The link step only ever creates these direct children, so
    anything else in the dir was put there by someone else.

    Both the literal target (canonicalised against the link's own dir, so a
    dangling link still counts) and the resolved one are checked."""
    if not link.is_symlink():
        return False
    bases = [base for r in roots for base in _destination_skill_roots(r)]
    for target in (_absolute_link_target(link), os.path.realpath(link)):
        parent = Path(os.path.normpath(target)).parent
        if any(parent == base for base in bases):
            return True
    return False


def _reconcile_global(
    link_dir: Path,
    desired: dict[str, Path],
    owned_roots: list[Path],
    log: RunLog,
    prior_owned: dict[Path, str] | None = None,
) -> collections.Counter:
    """Reconcile a GLOBAL skill dir against `desired` (name -> source). Creates and
    re-points our links, prunes links we own that are no longer desired, and never
    touches a foreign link or real file.

    A link into the generated tree is ours too, so a destination that wants a
    skill name the global flow currently owns TAKES it (destination wins, the
    long-standing rule). Only the desired branch does that: an undesired
    generated link is left for the manifest to retire, since this reconciler has
    no idea whether the global flow still wants it.

    `prior_owned` maps a home path to the exact target the PREVIOUS run recorded
    there. It is what makes a source TRANSITION work in a single run: when a
    destination repo moves, or a same-name collision changes winner, the old
    target is no longer under any configured root and would read as foreign —
    the link would be skipped here and then retired as stale, leaving the skill
    missing until the next run. The previous run's own record proves we made it,
    so it is repointed instead."""
    counts: collections.Counter = collections.Counter()
    link_dir.mkdir(parents=True, exist_ok=True)
    for name, src in sorted(desired.items()):
        dest = link_dir / name
        if dest.is_symlink():
            if os.readlink(dest) == str(src):
                continue
            was_ours = prior_owned is not None and os.readlink(dest) == prior_owned.get(
                dest
            )
            if (
                was_ours
                or _resolves_into(dest, owned_roots)
                or _link_points_generated(dest)
            ):
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


def destination_home_skills(
    cfg: dict,
) -> tuple[dict[str, Path], dict[str, Path], list[str]]:
    """The home skill links every configured DESTINATION wants: (pi, codex,
    collisions), each keyed by skill name.

    One function, two consumers: the link step deploys these, and the global flow
    records them in the manifest so they can be retired later. Computing the set
    twice from two copies of the rules is how a link ends up deployed but never
    recorded, or recorded but never deployed."""
    pi_desired: dict[str, Path] = {}
    codex_desired: dict[str, Path] = {}
    collisions: list[str] = []
    for d in cfg["destinations"]:
        dest = Path(d["path"]).expanduser()
        harnesses = d.get("harnesses", [])
        if "pi" in harnesses:
            for name, src in _common_pi_skills(dest).items():
                if name in pi_desired and pi_desired[name] != src:
                    collisions.append(f"pi:{name}")
                pi_desired[name] = src
        if wants_codex_surface(harnesses):
            for name, src in _common_codex_skills(dest).items():
                if name in codex_desired and codex_desired[name] != src:
                    collisions.append(f"codex:{name}")
                codex_desired[name] = src
    return pi_desired, codex_desired, collisions


def destination_home_links(cfg: dict) -> dict[Path, Path]:
    """Those same skills as absolute home link paths -> destination source."""
    pi_desired, codex_desired, _ = destination_home_skills(cfg)
    links = {_pi_global_skills() / n: src for n, src in pi_desired.items()}
    links.update({_codex_global_skills() / n: src for n, src in codex_desired.items()})
    return links


def do_link(cfg: dict, log: RunLog, manifest: HomeManifest | None = None) -> None:
    """Reconcile the destination-provided home skill links.

    `manifest` carries the PREVIOUS run's repo-link records, which is the only
    evidence available when a destination's path changes underneath a link — see
    `_reconcile_global`. Callers that mutate home paths build it before this step
    and finalize it after, so both halves share one transaction."""
    dests = [Path(d["path"]).expanduser() for d in cfg["destinations"]]
    prior_owned = manifest.prior_repo_links() if manifest is not None else None
    pi_desired, codex_desired, collisions = destination_home_skills(cfg)
    for d in cfg["destinations"]:
        if "cc" in d.get("harnesses", []):
            dest = Path(d["path"]).expanduser()
            log(f"  [{dest.name}] cc: no link needed (reads .claude/ directly)")

    # Clean up the abandoned repo-scoped .pi/skills links (reverted approach).
    cleaned = _cleanup_repo_scoped_pi(dests, log)
    if cleaned:
        log.always(f"  cleaned {cleaned} obsolete repo-scoped .pi/skills link(s)")

    # Both dirs are reconciled whenever ANY destination is configured, even down
    # to an empty desired set: a destination that drops the pi harness must have
    # its links retired, and gating on "some destination still wants pi" is what
    # left them stranded. Destinations that are gone entirely are retired by the
    # manifest instead — their root is no longer proof of anything.
    if dests:
        for label, link_dir, desired in (
            ("pi", _pi_global_skills(), pi_desired),
            ("codex", _codex_global_skills(), codex_desired),
        ):
            if not desired and not link_dir.is_dir():
                continue  # nothing wanted and nothing deployed — do not create it
            c = _reconcile_global(link_dir, desired, dests, log, prior_owned)
            log.always(
                f"  {label} (global {link_dir}): created {c['create']}, "
                f"repointed {c['repoint']}, pruned {c['prune']}, "
                f"skipped-foreign {c['skip-foreign']}"
            )
    for c in sorted(set(collisions)):
        log.always(
            f"  ⚠ name collision across destinations: {c} (last destination wins)"
        )


# --- global home-skill flow (ported from the retired install-global.sh) ----
#
# Two skill families, one mechanism (a recipe assembles layers into a skill dir;
# the destination is HOME). Family A = convention skills (python/nextjs/...).
# Family B = the slash-command group, routed by recipe scope.
#
# Home outputs are SYMLINKED, not copied: compose stages under examples/
# (gitignored, regenerated), the staged result is synced into the durable
# per-machine generated tree (~/.shared-llm/generated/), and the home path is a
# symlink into that tree. `readlink` on any home skill/agent answers "generated
# or handwritten?"; every deployed path is recorded in ~/.shared-llm/manifest.json
# so a rename/removal in the source prunes the old deployment on the next run.
# settings.json is the deliberate exception (scaffold-once real file): Claude
# Code rewrites it at runtime, and a rename-style save would silently replace a
# symlink with a real file.
#
# Safe-migration discipline (mirrors the reconciler): idempotent; never clobber a
# foreign symlink or a divergent real dir; a byte-identical real copy (the old
# deployment mechanism) is upgraded to a symlink in place.


def generated_root() -> Path:
    """Durable machine-local tree the home symlinks point into. Reads HOME at
    call time so a patched test HOME is honoured."""
    return HOME / ".shared-llm/generated"


def manifest_path() -> Path:
    return HOME / ".shared-llm/manifest.json"


GENERATED_MARKER = "/.shared-llm/generated/"

LOCK_PATH_REL = ".shared-llm/.global.lock"

# The ONLY home surfaces this kit deploys into. Everything the manifest may ever
# delete has to live under one of them — a recorded path outside them is a sign
# of a hand-edited or corrupted manifest, not of something we deployed.
MANAGED_HOME_RELS = (".claude", ".pi", ".agents", ".config/herdr")

# The two mutable settings files, the only paths kind "settings" may name.
SETTINGS_RELS = (".claude/settings.json", ".pi/agent/settings.json")

# Generated namespaces: dirs whose entries are individually owned, and the
# stand-alone generated files. One desired set covers all of them, so retiring a
# recipe OR disabling a harness retires its generated source too.
GENERATED_DIR_NAMESPACES = (
    "skills",
    "agents",
    "claude/hooks",
    "pi/extensions",
    "pi/agents",
)
GENERATED_FILES = (
    "claude/statusline.sh",
    "extensions/common/upagent/offerings.yaml",
    "herdr-config.toml",
)


def managed_home_roots() -> list[Path]:
    return [HOME / rel for rel in MANAGED_HOME_RELS]


def settings_targets() -> set[str]:
    return {str(HOME / rel) for rel in SETTINGS_RELS}


def _paths_overlap(a: Path, b: Path) -> bool:
    """True iff the two paths are the same or one contains the other."""
    return a == b or a in b.parents or b in a.parents


def _is_under(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _managed_home_links():
    """Every symlink on the managed home surfaces, walked WITHOUT following
    directory symlinks — a foreign dir link must not drag the walk outside the
    surfaces we own. Shared by ownership reconstruction and the live-reference
    scan so both see exactly the same set."""
    for root in managed_home_roots():
        if not root.is_dir():
            continue
        for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
            for name in sorted(dirnames + filenames):
                entry = Path(dirpath) / name
                if entry.is_symlink():
                    yield entry


def _fsync_dir(path: Path) -> None:
    """Durably record a rename: the entry is only safe once its DIRECTORY is
    flushed, not just the file's contents."""
    fd = os.open(str(path), os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _file_mode(path: Path) -> int:
    """The permission bits that matter for a deployed artifact. Hooks and the
    statusline are EXECUTED through the generated copy, so a mode-only drift is a
    real breakage, not cosmetic."""
    return path.stat().st_mode & 0o777


def _same_file(a: Path, b: Path) -> bool:
    """Identical content AND mode. Both halves are load-bearing: content alone
    would let an 0644 copy masquerade as a deployed 0755 hook, and would adopt a
    real file whose mode the user deliberately changed."""
    if a.is_symlink() or b.is_symlink() or not a.is_file() or not b.is_file():
        return False
    return a.read_bytes() == b.read_bytes() and _file_mode(a) == _file_mode(b)


def _under_generated(target: str) -> bool:
    """True iff a path STRING lies inside the generated root. Pure string work
    (normpath only) so it is meaningful for a dangling link too: a link into our
    generated tree stays ours — and prunable — after its source disappears."""
    root = str(generated_root())
    norm = os.path.normpath(target)
    return norm == root or norm.startswith(root + os.sep)


def _link_points_generated(dest: Path) -> bool:
    """True iff dest is a symlink OWNED by the generated tree.

    Ownership is decided on the RESOLVED target being inside the resolved
    generated root — never on a substring, so a foreign path that merely spells
    `/.shared-llm/generated/` somewhere else (e.g. /tmp/foreign/.shared-llm/
    generated/x) is NOT ours. A dangling link cannot be resolved, so its literal
    target is judged against the same root instead."""
    if not dest.is_symlink():
        return False
    resolved = Path(os.path.realpath(dest))
    if resolved.exists():
        try:
            resolved.relative_to(generated_root().resolve())
            return True
        except ValueError:
            return False
    literal = os.readlink(dest)
    if not os.path.isabs(literal):
        literal = os.path.join(os.path.dirname(str(dest)), literal)
    return _under_generated(literal) or _under_generated(str(resolved))


class GeneratedTreeError(RuntimeError):
    """The generated tree is not shaped the way we require. Raised instead of
    writing or deleting through something we do not own."""


def _generated_ancestors_safe(path: Path) -> bool:
    """True iff every DIRECTORY leading to `path` — the generated root itself and
    each step below it — is a real dir rather than a symlink.

    Ancestors are what make an operation reach outside the tree: if
    `generated/skills` were a symlink elsewhere, is_dir() would follow it and
    iterdir() would enumerate a stranger's directory, so a prune would delete
    their files while the symlink itself survived. The LEAF is deliberately not
    part of this test — replacing or unlinking a leaf acts on the link itself,
    never through it, so a planted leaf link is simply overwritten rather than
    wedging every future run."""
    root = generated_root()
    if root.is_symlink():
        return False
    try:
        rel = path.relative_to(root)
    except ValueError:
        return False
    current = root
    for part in rel.parts[:-1]:
        current = current / part
        if current.is_symlink():
            return False
    return True


def _require_generated_path(path: Path) -> None:
    if not _generated_ancestors_safe(path):
        raise GeneratedTreeError(
            f"refusing to touch {path}: a directory between it and "
            f"{generated_root()} is a symlink, so it is not ours to write or "
            "delete. Remove or rename the foreign path and re-run."
        )


def _aside_name(gen: Path) -> Path:
    """An unpredictable, unused sibling to park the old artifact at."""
    return gen.with_name(f".old-{gen.name}.{os.urandom(4).hex()}")


def _discard(path: Path) -> None:
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path, ignore_errors=True)
    elif path.exists() or path.is_symlink():
        path.unlink()


def _swap_in(new: Path, gen: Path) -> None:
    """Put `new` at `gen`, then discard whatever was there.

    Replacing one regular file with another is a single atomic os.replace.
    Every other combination — old dir, old file or symlink being replaced by a
    dir, and back — cannot be one syscall, so the old artifact is renamed ASIDE
    first and only discarded once the new name is in place. If the second rename
    fails the aside copy goes straight back, because in between every home link
    to `gen` dangles. Unlinking the old artifact first (the obvious shortcut)
    would make that failure unrecoverable, whatever its type."""
    _require_generated_path(gen)
    gen.parent.mkdir(parents=True, exist_ok=True)
    old_exists = gen.exists() or gen.is_symlink()
    if not old_exists:
        os.rename(new, gen)
        return
    new_is_file = new.is_file() and not new.is_symlink()
    old_is_file = gen.is_file() and not gen.is_symlink()
    if new_is_file and old_is_file:
        os.replace(new, gen)
        return
    aside = _aside_name(gen)
    os.rename(gen, aside)
    try:
        os.rename(new, gen)
    except BaseException:
        os.rename(aside, gen)  # never leave the links dangling
        raise
    _discard(aside)


def _sync_generated_dir(staged: Path, gen: Path) -> None:
    """Mirror a staged dir into the generated tree (only when it differs), via a
    same-filesystem temp build + atomic swap."""
    _require_generated_path(gen)
    if gen.is_dir() and not gen.is_symlink() and _dirs_equal(staged, gen):
        return
    gen.parent.mkdir(parents=True, exist_ok=True)
    tmp = gen.with_name(f".tmp-{gen.name}.{os.urandom(4).hex()}")
    shutil.copytree(staged, tmp)
    _swap_in(tmp, gen)


def _sync_generated_file(staged: Path, gen: Path) -> None:
    _require_generated_path(gen)
    if gen.is_file() and not gen.is_symlink() and _same_file(staged, gen):
        return
    gen.parent.mkdir(parents=True, exist_ok=True)
    tmp = gen.with_name(f".tmp-{gen.name}.{os.urandom(4).hex()}")
    shutil.copy2(staged, tmp)
    _swap_in(tmp, gen)


def _sync_generated_any(staged: Path, gen: Path) -> None:
    if staged.is_dir():
        _sync_generated_dir(staged, gen)
    else:
        _sync_generated_file(staged, gen)


def _looks_like_destination_skill(source: str) -> bool:
    """True iff `source` is spelled like a skill dir inside SOME destination
    repo — a DIRECT child of `<dest>/.claude/skills/` or `<dest>/.pi-skills/`.

    The destination may already be gone from the config (that is what a
    retirement is), so this shape check is all we can require of the source; the
    real deletion proof is the live link still pointing exactly here. It matches
    the writer exactly, though: accepting a nested path this code never emits
    would let a hand-edited manifest name someone else's symlink and have
    finalize delete it."""
    parent = Path(source).parent
    if parent.name == PI_ONLY_SKILLS_DIR:
        return True
    return parent.name == "skills" and parent.parent.name == ".claude"


def _is_canonical_abs(value: str) -> bool:
    """True iff `value` is an absolute path spelled canonically — no `.`, no
    `..`, no doubled or trailing separators. Containment checks are lexical, so
    a non-canonical spelling could pass one and still resolve elsewhere."""
    return (
        os.path.isabs(value)
        and os.path.normpath(value) == value
        and ".." not in Path(value).parts
    )


def _absolute_link_target(link: Path) -> str:
    """The link's target as a canonical absolute path, resolving a RELATIVE
    literal against the link's own directory (not the process cwd)."""
    literal = os.readlink(link)
    if not os.path.isabs(literal):
        literal = os.path.join(str(link.parent), literal)
    return os.path.normpath(literal)


_SHA256_RE = re.compile(r"[0-9a-f]{64}")


def _is_sha256(value: Any) -> bool:
    return isinstance(value, str) and _SHA256_RE.fullmatch(value) is not None


class HomeManifest:
    """Records every home path this run deployed; prunes what the previous run
    deployed but this one no longer does (the rename/removal cleanup).

    Pruning only ever deletes a SYMLINK we can still prove is ours — one into the
    generated tree, or one whose current target is exactly what we recorded when
    we made it. Scaffolded settings are the deliberate exception in the other
    direction: they are real files, they are kept permanently once created, and
    drift is logged rather than acted on. No real file is ever deleted."""

    MANIFEST_VERSION = 1

    def __init__(self) -> None:
        self.entries: dict[str, dict] = {}
        # Generated sources this run WANTED (synced), regardless of whether the
        # home link was actually deployed — retention is based on the desired
        # recipe set, so a run where every target was foreign/divergent does not
        # delete and recreate the generated sources on every pass (fix 7).
        self.generated_wanted: set[Path] = set()
        self.previous, self.previous_ok = self._read_previous()

    # --- recording ---------------------------------------------------------

    def record_link(self, dest: Path, source: Path) -> None:
        self.entries[str(dest)] = {"kind": "link", "source": str(source)}

    def record_repo_link(
        self, dest: Path, source: Path, log: RunLog | None = None
    ) -> None:
        """Track a home skill link a DESTINATION provides.

        These are not deployed by the global flow, but they have to be recorded
        by it: when the last destination that wanted pi/codex is removed from the
        config, its root is gone as proof of ownership and the link step is not
        even called for that harness. The previous run's record is then the only
        remaining evidence that we made the link — without it the link is
        stranded, shadowing global skills or dangling once the repo moves."""
        key = str(dest)
        if dest.is_symlink() and os.readlink(dest) == str(source):
            self.entries[key] = {"kind": "repo-link", "source": str(source)}
            return
        # The link on disk is not (yet) the desired one. If it is provably the
        # link a PREVIOUS run made, the config still wants this path and only the
        # SOURCE moved — which the entry points that skip the link step (`global`,
        # `prune`) cannot repoint. Carry the old record forward so finalize does
        # not retire a skill the config still asks for; `update` repoints it.
        prior = self.previous.get(key) if self.previous_ok else None
        if not prior or not dest.is_symlink():
            return
        kind = prior.get("kind")
        if kind == "repo-link" and os.readlink(dest) == prior.get("source"):
            self.entries[key] = dict(prior)
            if log is not None:
                log.always(
                    f"  manifest: {dest} still points at {prior['source']} but its "
                    f"destination now offers {source} — kept as is; run `just update` "
                    "to repoint it"
                )
            return
        # Crash residue from a half-finished handback: a previous update retired
        # the destination, gave the name to a generated skill, and died before it
        # could persist that. The durable record still says repo-link while the
        # live link already points into the generated tree — and the config wants
        # the path again. Record what is ACTUALLY there, so finalize does not read
        # the mismatch as "no longer ours" and delete a skill the config asks for.
        # The next full update runs the link step and transitions it back.
        #
        # This has to be a FIXED POINT, not a one-shot rescue: after the first
        # global-only run the record is kind "link", so a second one must
        # recognise its own output and preserve it again. It re-adopts that
        # record only when it still describes the link on disk exactly.
        if _link_points_generated(dest):
            live_source = _absolute_link_target(dest)
            preserved = kind == "link" and prior.get("source") == live_source
            if kind == "repo-link" or preserved:
                self.record_link(dest, Path(live_source))
                if log is not None:
                    log.always(
                        f"  manifest: {dest} points into the generated tree while its "
                        f"destination offers {source} — an interrupted handback; kept "
                        "as is, run `just update` to finish it"
                    )

    def prior_repo_links(self) -> dict[Path, str]:
        """Every destination link the previous run recorded: home path -> the
        exact target it had then. Ownership evidence for a link whose source has
        since moved out from under it."""
        if not self.previous_ok:
            return {}
        return {
            Path(key): meta["source"]
            for key, meta in self.previous.items()
            if meta.get("kind") == "repo-link"
        }

    def commit_repo_links(self, cfg: dict, log: RunLog) -> None:
        """Persist repo-link state ALONE, for the entry point that only touches
        destination links (`just link`).

        Every other record is carried across untouched: this run did not deploy
        the global skills or the runtime, so it has no business retiring them —
        but it must not forget the links it just created either, or removing the
        last destination would strand them with nothing left to prove ownership."""
        if not self.previous_ok:
            log.always(
                "  manifest: previous manifest is unusable — not rewriting it from "
                "the link step alone; run `just update` to rebuild it safely"
            )
            return
        self.entries = {
            key: dict(meta)
            for key, meta in self.previous.items()
            if meta.get("kind") != "repo-link"
        }
        for dest, source in destination_home_links(cfg).items():
            self.record_repo_link(dest, source, log)
        for key, meta in self.previous.items():
            if meta.get("kind") != "repo-link" or key in self.entries:
                continue
            dest = Path(key)
            if dest.is_symlink() and os.readlink(dest) == meta["source"]:
                dest.unlink()
                log(f"    manifest: pruned {dest} (destination no longer wants it)")
        self._write(log)
        log.always(f"  manifest: {len(self.entries)} paths recorded")

    def retiring_repo_links(self, still_wanted: dict[Path, Path]) -> dict[Path, str]:
        """Destination links the PREVIOUS run recorded that this config no longer
        wants — the ones the global flow may reclaim for a generated skill of the
        same name instead of skipping as foreign."""
        if not self.previous_ok:
            return {}
        return {
            Path(key): meta["source"]
            for key, meta in self.previous.items()
            if meta.get("kind") == "repo-link" and Path(key) not in still_wanted
        }

    def record_settings(self, dest: Path, *, created: bool) -> None:
        """Track a mutable settings file we OWN. Ownership is only ever acquired
        by creating the file — a pre-existing settings.json is the user's, never
        adopted. The recorded hash is the hash at DEPLOYMENT time, carried
        forward from the previous manifest; re-hashing the current bytes would
        launder the user's later edits into "what we deployed"."""
        key = str(dest)
        prior = self.previous.get(key) if self.previous_ok else None
        if prior and prior.get("kind") == "settings":
            self.entries[key] = dict(prior)
            return
        if not created:
            return  # pre-existing and never ours — do not adopt
        if dest.is_file():
            self.entries[key] = {
                "kind": "settings",
                "sha256": hashlib.sha256(dest.read_bytes()).hexdigest(),
            }

    def mark_generated(self, path: Path) -> None:
        self.generated_wanted.add(path)

    # --- previous manifest -------------------------------------------------

    def _read_previous(self) -> tuple[dict[str, dict], bool]:
        """Load the previous manifest. Returns (paths, ok). `ok` is False when a
        manifest exists but cannot be trusted — the caller then prunes NOTHING
        this run.

        Everything in this file is a DELETION TARGET, so the schema is validated
        as a closed, tagged union rather than duck-typed: a hand-edited or
        corrupted manifest naming /etc/something must never become an unlink."""
        path = manifest_path()
        if not path.is_file():
            return {}, True
        try:
            data = json.loads(path.read_text())
        except (json.JSONDecodeError, OSError, UnicodeDecodeError):
            return {}, False
        if not isinstance(data, dict) or data.get("version") != self.MANIFEST_VERSION:
            return {}, False
        paths = data.get("paths")
        if not isinstance(paths, dict):
            return {}, False
        for key, meta in paths.items():
            if not self._valid_entry(key, meta):
                return {}, False
        return paths, True

    @staticmethod
    def _valid_entry(key: Any, meta: Any) -> bool:
        """One manifest entry, validated as strictly as a deletion deserves.

        The destination must be spelled CANONICALLY: `/home/u/.claude/../../x`
        passes a lexical containment check against `/home/u/.claude` while the
        filesystem resolves it somewhere else entirely, so a traversal spelling
        is rejected outright rather than normalised into acceptance.

        Only the two kinds this tool can actually emit are accepted. `file` was
        dropped when its last caller went away: an accepted kind with no writer
        is a deletion path nothing tests."""
        if not isinstance(key, str) or not isinstance(meta, dict):
            return False
        if not _is_canonical_abs(key):
            return False
        # Deletion targets live only on the home surfaces we deploy into.
        if not any(_is_under(Path(key), root) for root in managed_home_roots()):
            return False
        kind = meta.get("kind")
        if kind == "link":
            source = meta.get("source")
            if not isinstance(source, str) or not _is_canonical_abs(source):
                return False
            # A link source is either the durable generated tree or — for a
            # manifest written before the decoupling — the kit checkout, which
            # this run repoints. Nothing else can have been deployed by us.
            return _under_generated(source) or _is_under(Path(source), project_root())
        if kind == "repo-link":
            source = meta.get("source")
            if not isinstance(source, str) or not _is_canonical_abs(source):
                return False
            # Narrow on both ends: only the two home skill dirs can hold one, and
            # only a destination's skill dir can be its source. The source root
            # is deliberately NOT checked against the current config — the whole
            # point is to retire links whose destination is already gone — so
            # deletion is gated on the live link still matching this exact
            # source, which _prune_stale_home enforces.
            return Path(key).parent in (
                _pi_global_skills(),
                _codex_global_skills(),
            ) and _looks_like_destination_skill(source)
        if kind == "settings":
            return _is_sha256(meta.get("sha256")) and key in settings_targets()
        return False

    def quarantine(self, log: RunLog) -> None:
        """Move an untrusted manifest aside instead of overwriting it, so the
        record of what we once deployed survives for a human to inspect."""
        path = manifest_path()
        if not path.is_file():
            return
        from datetime import datetime

        stamp = datetime.now().strftime("%Y-%m-%d-%H%M%S")
        aside = path.with_name(f"{path.name}.corrupt-{stamp}")
        os.replace(path, aside)
        log.always(f"  manifest: quarantined the unusable manifest as {aside}")

    def reconstruct(self, log: RunLog) -> None:
        """Re-derive ownership of live deployments by scanning the managed home
        roots for links into the generated tree.

        Without this, an untrusted manifest would be replaced by one that knows
        nothing about the links already on disk: the NEXT run would trust that
        empty record, prune the generated sources as orphans, and leave every
        surviving home link dangling. A link into our generated tree is provable
        ownership on its own, so it can be re-adopted safely. Settings files are
        deliberately NOT reconstructed — nothing on disk proves we created one."""
        found = 0
        for entry in _managed_home_links():
            key = str(entry)
            if key in self.entries or not _link_points_generated(entry):
                continue
            # The target is stored ABSOLUTE and canonical: a relative literal
            # (which _link_points_generated accepts) would be rejected by our own
            # schema on the next run, quarantining forever instead of recovering.
            record = {"kind": "link", "source": _absolute_link_target(entry)}
            if not self._valid_entry(key, record):
                log.always(
                    f"  manifest: not re-adopting {entry} — cannot record it safely"
                )
                continue
            self.entries[key] = record
            found += 1
        if found:
            log.always(
                f"  manifest: re-adopted {found} live link(s) into the generated tree, "
                "so the next run can retire them cleanly"
            )

    # --- finalize ----------------------------------------------------------

    def finalize(self, log: RunLog) -> None:
        """Reconcile the home surface against the previous run, then persist.

        Generated SOURCES are deliberately not touched here — commit_generated()
        does that afterwards, once the links that point at them are known to be
        gone. Deleting a source first would strand a live link on any failure."""
        if not self.previous_ok:
            log.always(
                "  manifest: previous manifest at "
                f"{manifest_path()} is unreadable or has an unexpected schema — "
                "PRUNING NOTHING this run (nothing will be deleted)"
            )
            self.quarantine(log)
            self.reconstruct(log)
        else:
            self._prune_stale_home(log)
        self._write(log)

    def commit_generated(self, log: RunLog) -> None:
        """The final, ordered cleanup: retire generated sources nothing wants.

        Runs only after finalize() has removed the stale home links and written
        the manifest, so a source is deleted only once nothing points at it and
        the record of the run is durable. A distrusted manifest skips it, exactly
        as it skips every other deletion."""
        if self.previous_ok:
            self._prune_generated_orphans(log)

    def _live_generated_sources(self) -> set[Path]:
        """Generated sources some home link still points at, scanned FRESH off
        the disk rather than taken from the manifest.

        The manifest is written after the links, so a run interrupted in between
        leaves live links nothing recorded. Trusting the record alone would then
        delete their sources and leave them dangling — this scan is what makes
        the cleanup safe regardless of how the previous run ended."""
        live: set[Path] = set()
        for entry in _managed_home_links():
            if _link_points_generated(entry):
                live.add(Path(_absolute_link_target(entry)))
        return live

    def _prune_stale_home(self, log: RunLog) -> None:
        pruned = kept = 0
        for dest_str, meta in self.previous.items():
            if dest_str in self.entries:
                continue
            if meta.get("kind") == "settings":
                # Mutable, user-owned once written: never destructively pruned.
                self._log_settings_drift(Path(dest_str), meta, log)
                self.entries[dest_str] = dict(meta)
                continue
            dest = Path(dest_str)
            if dest.is_symlink():
                ours = _link_points_generated(dest) or os.readlink(dest) == meta.get(
                    "source"
                )
                if ours:
                    dest.unlink()
                    pruned += 1
                    log(f"    manifest: pruned {dest} (no longer generated)")
                    continue
            elif not dest.exists():
                continue  # already gone — drop the entry silently
            kept += 1
            log.always(
                f"  manifest: NOT pruning {dest} — changed since deployment; review by hand"
            )
        log.always(
            f"  manifest: {len(self.entries)} paths recorded, {pruned} stale pruned"
            + (f", {kept} divergent kept" if kept else "")
        )

    def _log_settings_drift(self, dest: Path, meta: dict, log: RunLog) -> None:
        if not dest.is_file():
            return
        current = hashlib.sha256(dest.read_bytes()).hexdigest()
        if current != meta.get("sha256"):
            log(f"    manifest: settings drifted since deployment: {dest} (kept)")

    def _prune_generated_orphans(self, log: RunLog) -> None:
        """Retire every generated artifact this run did not want — skills and
        agents, and the whole pieces too (hooks, statusline, Pi extensions and
        personas, herdr config). One desired set covers all of them, so disabling
        a harness retires its runtime instead of leaving dead code behind.

        Two guards bound the deletion. A namespace reached through a SYMLINK is
        refused outright: following one would enumerate — and empty — a directory
        that is not ours. And a source some live home link still points at is
        kept whatever the manifest says, so an interrupted previous run cannot
        turn into a dangling link here."""
        root = generated_root()
        if root.is_symlink():
            log.always(
                f"  manifest: {root} is a symlink — refusing to prune through it; "
                "the generated tree must be a real directory we own"
            )
            return
        live = self._live_generated_sources()

        def drop(entry: Path) -> None:
            # Overlap, not equality: a link may point at a FILE inside a
            # generated dir (deleting the dir dangles it), or at a namespace dir
            # ABOVE one (deleting a child mutates what the link resolves to).
            # Either direction means something live still depends on this path.
            if any(_paths_overlap(target, entry) for target in live):
                log.always(
                    f"  manifest: keeping {entry} — a home link still points at it "
                    "or into it though nothing recorded it; re-run after removing "
                    "that link"
                )
                return
            if entry.is_dir() and not entry.is_symlink():
                shutil.rmtree(entry)
            else:
                entry.unlink()
            log(f"    manifest: removed orphaned generated {entry}")

        for sub in GENERATED_DIR_NAMESPACES:
            base = generated_root() / sub
            if not base.is_dir():
                continue
            # The namespace dir is about to be ENUMERATED, so it must be a real
            # dir itself — not only its parents. Following a symlink here would
            # list, and then empty, a directory that belongs to someone else.
            if base.is_symlink() or not _generated_ancestors_safe(base):
                log.always(
                    f"  manifest: {base} is a symlink (or sits under one) — refusing "
                    "to prune through it; it is not ours to empty"
                )
                continue
            for entry in sorted(base.iterdir()):
                if entry in self.generated_wanted or entry.name.startswith(
                    (".tmp-", ".old-")
                ):
                    continue
                drop(entry)
        for rel in GENERATED_FILES:
            entry = generated_root() / rel
            if not (entry.exists() or entry.is_symlink()):
                continue
            if entry in self.generated_wanted:
                continue
            if not _generated_ancestors_safe(entry):
                log.always(
                    f"  manifest: {entry} is a symlink (or sits under one) — refusing "
                    "to delete through it"
                )
                continue
            drop(entry)

    def _write(self, log: RunLog) -> None:
        """Persist atomically, through paths nothing else can have prepared: the
        temp file is created exclusively under an unpredictable name, so a
        pre-planted symlink at a guessable one cannot redirect the write."""
        path = manifest_path()
        if path.parent.is_symlink():
            raise GeneratedTreeError(
                f"refusing to write {path}: its directory {path.parent} is a symlink"
            )
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = (
            json.dumps(
                {"version": self.MANIFEST_VERSION, "paths": self.entries},
                indent=2,
                sort_keys=True,
            )
            + "\n"
        )
        fd, tmp_name = tempfile.mkstemp(
            dir=str(path.parent), prefix=".manifest-", suffix=".json"
        )
        try:
            with os.fdopen(fd, "w") as fh:
                fh.write(payload)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp_name, path)
        except BaseException:
            with suppress(OSError):
                os.unlink(tmp_name)
            raise
        _fsync_dir(path.parent)


GLOBAL_CONVENTION_SKILLS = {
    "python": "compose/global/python.yaml",
    "nextjs": "compose/global/nextjs.yaml",
    "backend": "compose/global/backend.yaml",
    "golang": "compose/global/golang.yaml",
    "herdr": "compose/global/herdr.yaml",
    "clickhouse": "compose/global/clickhouse.yaml",
    "kafka": "compose/global/kafka.yaml",
    "lucidchart": "compose/global/lucidchart.yaml",
    "drawio": "compose/global/drawio.yaml",
    "create-html": "compose/global/create-html.yaml",
}

# Home-dir skill names to prune when found (matched by their SKILL.md `name:` so we
# never delete a foreign dir of the same name). The redundant meta wrapper is retired now;
# other legacy planner names remain one-release aliases.
DEPRECATED_GLOBAL_SKILLS: tuple[str, ...] = ("meta-cc-plan-and-grill",)


def _global_home_dirs() -> dict[str, Path]:
    """Home skill dir per harness token. Codex uses ~/.agents/skills (item 4)."""
    return {
        "cc": HOME / ".claude/skills",
        "pi": HOME / ".pi/agent/skills",
        "codex": HOME / ".agents/skills",
    }


def _global_targets_for(scope: str, name: str) -> set[str]:
    """Harness tokens a slash-command skill routes to, from its recipe scope.
    Common goes to every harness unless it is a do-* workflow command; do-* is Pi-only
    because Claude has cc-* counterparts and Codex does not run the Pi workflow front doors."""
    if scope == "claude":
        return {"cc"}
    if scope == "common":
        if name.startswith("do-"):
            return {"pi"}
        return {"cc", "pi", "codex"}
    if scope == "codex" or scope == "cursor":
        return {"codex"}  # cursor shares the codex home dir (~/.agents/skills)
    if scope == "pi":
        return {"pi"}
    return set()


def _dirs_equal(a: Path, b: Path) -> bool:
    """Same names, same bytes, same modes — recursively, comparing every file.

    This is a DELETION proof: callers recursively remove `b` when it returns
    True, so it reads the bytes rather than trusting stat. filecmp.dircmp (the
    obvious tool) compares shallowly by default — same type, size, and mtime
    counts as equal — which quietly classifies two different files as identical
    and made this proof unsound."""
    if a.is_symlink() or b.is_symlink() or not (a.is_dir() and b.is_dir()):
        return False
    # The dirs' OWN modes count, not just their children's: a target the user
    # chmod-ed to 0700 is a deliberate local change, and identical contents do
    # not make it ours to delete.
    if _file_mode(a) != _file_mode(b):
        return False
    entries = sorted(p.name for p in a.iterdir())
    if entries != sorted(p.name for p in b.iterdir()):
        return False
    for name in entries:
        pa, pb = a / name, b / name
        if pa.is_symlink() or pb.is_symlink():
            # Two links match only as links, with the same literal target.
            if not (pa.is_symlink() and pb.is_symlink()):
                return False
            if os.readlink(pa) != os.readlink(pb):
                return False
        elif pa.is_dir() and pb.is_dir():
            if _file_mode(pa) != _file_mode(pb) or not _dirs_equal(pa, pb):
                return False
        elif pa.is_file() and pb.is_file():
            if not _same_file(pa, pb):
                return False
        else:
            return False  # type mismatch, or something we cannot compare
    return True


def _link_skill_dir(
    generated: Path,
    target: Path,
    log: RunLog,
    reclaimable: dict[Path, str] | None = None,
) -> str:
    """Point a home skill dir at its generated source. A byte-identical real
    dir (the old copy mechanism) is upgraded to a symlink; anything foreign or
    divergent is left alone, loudly.

    `reclaimable` maps a home path to the destination source a PREVIOUS run
    recorded there. A link matching its record is one we made for a destination
    that no longer wants it, so the name comes back to the generated skill in
    this run instead of being skipped as foreign and unlinked at the end."""
    if target.is_symlink():
        if os.readlink(target) == str(generated):
            return "uptodate"
        if _link_points_generated(target) or (
            reclaimable is not None and os.readlink(target) == reclaimable.get(target)
        ):
            target.unlink()
            target.symlink_to(generated)
            log(f"    repointed {target}")
            return "installed"
        log(f"    skip {target} (symlink — foreign)")
        return "skip"
    if target.is_dir():
        if _dirs_equal(generated, target):
            shutil.rmtree(target)
            target.symlink_to(generated)
            log(f"    migrated copy -> link {target}")
            return "installed"
        log(f"    skip {target} (exists & differs — not ours)")
        return "skip"
    if target.exists():
        log(f"    skip {target} (non-dir — foreign)")
        return "skip"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.symlink_to(generated)
    log(f"    linked {target}")
    return "installed"


def _prune_stale_skill(
    name: str, staged: Path, keep: set[str], home_dirs: dict[str, Path], log: RunLog
) -> int:
    """Remove our deployments (generated-tree links, or byte-identical legacy
    copies) from home dirs whose token is NOT in keep."""
    removed = 0
    for tok, base in home_dirs.items():
        if tok in keep:
            continue
        target = base / name
        if target.is_symlink():
            if _link_points_generated(target):
                target.unlink()
                removed += 1
                log(f"    pruned stale link {target}")
            continue
        if not target.exists():
            continue
        if target.is_dir() and _dirs_equal(staged, target):
            shutil.rmtree(target)
            removed += 1
            log(f"    pruned stale {target}")
    return removed


def _prune_deprecated_global(home_dirs: dict[str, Path], log: RunLog) -> int:
    """Remove a retired skill's deployment from the home dirs.

    Ownership must be PROVEN, never inferred from the name. A skill's own
    frontmatter `name:` is the weakest possible evidence — every independently
    authored skill at that path carries exactly that name, and a stale kit copy
    the user has since edited carries it too. So only two things are deleted: a
    link we own, and a real dir that is byte-and-mode identical to the generated
    artifact of the same name. Anything else is preserved and named in a warning
    for the human to deal with; a recursive delete is not undoable."""
    removed = 0
    for name in DEPRECATED_GLOBAL_SKILLS:
        generated = generated_root() / "skills" / name
        for base in home_dirs.values():
            target = base / name
            if target.is_symlink():
                if _link_points_generated(target):
                    target.unlink()
                    removed += 1
                    log(f"    removed deprecated link {target}")
                continue
            if not target.exists():
                continue
            if (
                target.is_dir()
                and generated.is_dir()
                and _dirs_equal(generated, target)
            ):
                shutil.rmtree(target)
                removed += 1
                log(f"    removed deprecated copy {target}")
                continue
            log.always(
                f"  deprecated '{name}': PRESERVED {target} — it is not a link we own "
                "and its contents are not what the kit deployed, so it may be yours; "
                "delete it by hand if it is stale"
            )
    return removed


# Generic placeholder fills for the kit's OWN self-compose (the home skills built
# by do_global here, and the tracked slash-command skills rebuilt by `just
# selfcompose`). A kit-synced common layer ships {{OPS_REPO}} as a placeholder that
# a REAL destination fills from its ~/.shared-llm.yaml `placeholders:` map; when the
# kit composes ITSELF it fills the same token with the generic default so its home /
# tracked outputs stay generic (byte-identical to what they were before the token was
# introduced) and never trip the unfilled-placeholder fail-loud check.
KIT_SELF_PLACEHOLDERS = {"OPS_REPO": "your-repo-ops"}


def _fill_text_placeholders(text: str, placeholders: dict[str, str]) -> str:
    return PLACEHOLDER_RE.sub(lambda m: placeholders.get(m.group(1), m.group(0)), text)


def _frontmatter_description(path: Path) -> tuple[str | None, str | None]:
    """Return a markdown frontmatter description, or a parse error.

    This parser is deliberately narrow: only a leading YAML frontmatter block is
    accepted. It is shared by the description audit for standalone skills,
    tracked generated outputs, and whole-file Pi personas.
    """
    try:
        text = path.read_text()
    except OSError as exc:
        return None, str(exc)
    if not text.startswith("---\n"):
        return None, "missing leading YAML frontmatter"
    end = text.find("\n---", 4)
    if end == -1:
        return None, "unterminated YAML frontmatter"
    raw = text[4:end]
    try:
        data = yaml.safe_load(raw) or {}
    except yaml.YAMLError as exc:
        return None, f"invalid YAML frontmatter: {exc}"
    if not isinstance(data, dict):
        return None, "YAML frontmatter must be a mapping"
    description = data.get("description")
    if not isinstance(description, str):
        return None, "frontmatter description must be a string"
    return description.strip(), None


def _recipe_description_source_owner(desc_rel: str, fallback_owner: str) -> str:
    public_prefix = f".shared-llm/{PUBLIC_DIR}/"
    if desc_rel == f".shared-llm/{PUBLIC_DIR}" or desc_rel.startswith(public_prefix):
        return "public"
    return fallback_owner


def _recipe_description_source(repo_root: Path, desc_rel: str) -> Path:
    """Resolve a recipe description source for dry-run auditing.

    Destination-owned recipes can legitimately point at kit-owned public layers
    before the destination's .shared-llm/public/ tree has been copied. Resolve
    those public references against the kit source that will be copied, while
    keeping destination-owned references relative to the destination source.
    """
    desc_path = Path(desc_rel)
    if desc_path.is_absolute():
        return desc_path
    public_prefix = f".shared-llm/{PUBLIC_DIR}/"
    if desc_rel == f".shared-llm/{PUBLIC_DIR}" or desc_rel.startswith(public_prefix):
        return project_root() / desc_rel
    return repo_root / desc_rel


def _recipe_description_item(
    *,
    repo_root: Path,
    recipe: Path,
    owner: str,
    placeholders: dict[str, str],
    destination_index: int | None = None,
) -> tuple[DescriptionItem | None, DescriptionError | None]:
    try:
        if owner == "destination":
            with redirect_stderr(io.StringIO()):
                data = load_compose_yaml(recipe)
        else:
            data = load_compose_yaml(recipe)
    except SystemExit as exc:
        return None, DescriptionError(
            owner,
            "recipe",
            recipe.stem,
            recipe,
            str(exc) or "invalid recipe",
            destination_index,
        )
    compose_type = data.get("type") or ("agent" if "model" in data else "skill")
    if compose_type in PLAIN_TYPES or compose_type in STRUCTURED_TYPES:
        return None, None
    desc_rel = data.get("description")
    name = str(data.get("name") or recipe.stem)
    if not isinstance(desc_rel, str):
        return None, DescriptionError(
            owner,
            compose_type,
            name,
            recipe,
            "missing description path",
            destination_index,
        )
    desc_path = _recipe_description_source(repo_root, desc_rel)
    try:
        description = _fill_text_placeholders(
            desc_path.read_text().strip(), placeholders
        )
    except OSError as exc:
        return None, DescriptionError(
            owner, compose_type, name, desc_path, str(exc), destination_index
        )
    return (
        DescriptionItem(
            owner=owner,
            source_owner=_recipe_description_source_owner(desc_rel, owner),
            kind=compose_type,
            name=name,
            source=desc_path,
            length=utf16_code_units(description),
            location=str(data.get("output", "")),
            destination_index=destination_index,
        ),
        None,
    )


def _public_recipe_description_items() -> tuple[
    list[DescriptionItem], list[DescriptionError]
]:
    root = project_root()
    compose = root / ".shared-llm" / PUBLIC_DIR / "compose"
    items: list[DescriptionItem] = []
    errors: list[DescriptionError] = []
    for recipe in sorted(list(compose.rglob("*.yaml")) + list(compose.rglob("*.yml"))):
        item, error = _recipe_description_item(
            repo_root=root,
            recipe=recipe,
            owner="public",
            placeholders=KIT_SELF_PLACEHOLDERS,
        )
        if item:
            items.append(item)
        if error:
            errors.append(error)
    return items, errors


def _destination_recipe_description_items(
    dest: Path, placeholders: dict[str, str], destination_index: int
) -> tuple[list[DescriptionItem], list[DescriptionError]]:
    compose = dest / ".shared-llm" / THIS_REPO_DIR / "compose"
    items: list[DescriptionItem] = []
    errors: list[DescriptionError] = []
    if not compose.is_dir():
        return items, errors
    for recipe in sorted(list(compose.rglob("*.yaml")) + list(compose.rglob("*.yml"))):
        item, error = _recipe_description_item(
            repo_root=dest,
            recipe=recipe,
            owner="destination",
            placeholders=placeholders,
            destination_index=destination_index,
        )
        if item:
            items.append(item)
        if error:
            errors.append(error)
    return items, errors


def _frontmatter_description_item(
    path: Path,
    *,
    owner: str,
    kind: str,
    name: str | None = None,
    destination_index: int | None = None,
) -> tuple[DescriptionItem | None, DescriptionError | None]:
    description, error = _frontmatter_description(path)
    item_name = name or path.stem
    if error:
        return None, DescriptionError(
            owner, kind, item_name, path, error, destination_index
        )
    assert description is not None
    return (
        DescriptionItem(
            owner=owner,
            source_owner=owner,
            kind=kind,
            name=item_name,
            source=path,
            length=utf16_code_units(description),
            location=str(path),
            destination_index=destination_index,
        ),
        None,
    )


def _source_pi_agent_description_items() -> tuple[
    list[DescriptionItem], list[DescriptionError]
]:
    base = project_root() / ".shared-llm" / PUBLIC_DIR / "llm/pi/common/agents"
    items: list[DescriptionItem] = []
    errors: list[DescriptionError] = []
    if not base.is_dir():
        return items, errors
    for path in sorted(base.glob("*.md")):
        item, error = _frontmatter_description_item(
            path, owner="public", kind="pi-agent", name=path.stem
        )
        if item:
            items.append(item)
        if error:
            errors.append(error)
    return items, errors


def _tracked_generated_description_items() -> tuple[
    list[DescriptionItem], list[DescriptionError]
]:
    """Tracked generated discovery outputs owned by this public kit.

    These are not home files. They are committed outputs in the kit checkout and
    are checked separately so self-compose can catch drift in the artifacts this
    repository intentionally tracks.
    """
    root = project_root()
    generated_roots = (root / ".claude/agents", root / ".claude/skills")
    if not any(path.exists() for path in generated_roots):
        return [], []
    candidates: list[Path] = []
    items: list[DescriptionItem] = []
    errors: list[DescriptionError] = []
    try:
        listed_paths: set[str] = set()
        for git_args in (("ls-files",), ("ls-files", "--others", "--exclude-standard")):
            out = subprocess.run(
                [
                    "git",
                    "-C",
                    str(root),
                    *git_args,
                    "-z",
                    "--",
                    ".claude/agents",
                    ".claude/skills",
                ],
                check=True,
                capture_output=True,
            ).stdout.decode()
            listed_paths.update(p for p in out.split("\0") if p.endswith(".md"))
        candidates = [root / p for p in listed_paths]
    except (OSError, subprocess.CalledProcessError, UnicodeDecodeError):
        return [], [
            DescriptionError(
                "public",
                "tracked-generated",
                "git-discovery",
                Path(".claude"),
                "git discovery failed; cannot safely enumerate generated descriptions with --exclude-standard",
            )
        ]
    for path in sorted(candidates):
        if path.name != "SKILL.md" and path.parent.name != "agents":
            continue
        if path.name == "SKILL.md":
            kind = "tracked-skill"
            name = path.parent.name
        else:
            kind = "tracked-agent"
            name = path.stem
        item, error = _frontmatter_description_item(
            path, owner="public", kind=kind, name=name
        )
        if item:
            items.append(item)
        if error:
            errors.append(error)
    return items, errors


def _destination_standalone_description_items(
    dest: Path, destination_index: int
) -> tuple[list[DescriptionItem], list[DescriptionError]]:
    base = dest / ".shared-llm" / THIS_REPO_DIR / "skills"
    items: list[DescriptionItem] = []
    errors: list[DescriptionError] = []
    if not base.is_dir():
        return items, errors
    for skill in sorted(base.iterdir()):
        if not skill.is_dir() or skill.name.startswith("."):
            continue
        path = skill / "SKILL.md"
        if not path.is_file():
            errors.append(
                DescriptionError(
                    "destination",
                    "standalone-skill",
                    skill.name,
                    path,
                    "missing SKILL.md",
                    destination_index,
                )
            )
            continue
        item, error = _frontmatter_description_item(
            path,
            owner="destination",
            kind="standalone-skill",
            name=skill.name,
            destination_index=destination_index,
        )
        if item:
            items.append(item)
        if error:
            errors.append(error)
    return items, errors


def build_description_corpus(
    cfg: dict,
) -> tuple[list[DescriptionItem], list[DescriptionError]]:
    """Dry-run corpus builder for description audit/enforcement.

    It reads source recipe description files and standalone source SKILL.md files
    only. It does not compose, copy, link, reconcile home paths, or scan foreign
    home/generated directories.
    """
    items: list[DescriptionItem] = []
    errors: list[DescriptionError] = []

    for producer in (
        _public_recipe_description_items,
        _source_pi_agent_description_items,
        _tracked_generated_description_items,
    ):
        got, bad = producer()
        items.extend(got)
        errors.extend(bad)

    for destination_index, dest_cfg in enumerate(cfg.get("destinations", []), start=1):
        dest = Path(dest_cfg.get("path", "")).expanduser()
        placeholders = dest_cfg.get("placeholders") or {}
        if not isinstance(placeholders, dict):
            placeholders = {}
        got, bad = _destination_recipe_description_items(
            dest, placeholders, destination_index
        )
        items.extend(got)
        errors.extend(bad)
        got, bad = _destination_standalone_description_items(dest, destination_index)
        items.extend(got)
        errors.extend(bad)

    return items, errors


def _destination_report_label(index: int | None) -> str:
    return f"destination#{index}" if index is not None else "destination#?"


def print_description_report(
    items: list[DescriptionItem],
    errors: list[DescriptionError],
    *,
    enforce_destinations: bool,
) -> bool:
    """Print a private-safe report and return True if enforcement passes."""
    hard_owners = {"public"} | ({"destination"} if enforce_destinations else set())
    warnings = [i for i in items if i.length > DESCRIPTION_REVIEW_THRESHOLD]
    hard = [
        i
        for i in items
        if i.length > DESCRIPTION_HARD_MAX
        and (i.owner in hard_owners or i.source_owner in hard_owners)
    ]
    staged_over = [
        i
        for i in items
        if i.length > DESCRIPTION_HARD_MAX
        and i.owner == "destination"
        and i.source_owner == "destination"
        and not enforce_destinations
    ]
    total = sum(i.length for i in items)

    print(
        f"descriptions: checked {len(items)} item(s), total discovery metadata {total} UTF-16 code units"
    )
    for item in sorted(warnings, key=lambda i: (i.owner, i.length, str(i.source))):
        level = "FAIL" if item in hard else "WARN"
        if item.owner == "destination":
            print(
                f"  {level} {_destination_report_label(item.destination_index)} "
                f"{item.kind}: {item.length} units (status=over-threshold)"
            )
            continue
        owner_note = (
            f" source-owner={item.source_owner}"
            if item.source_owner != item.owner
            else ""
        )
        print(
            f"  {level} {item.owner} {item.kind} {item.name}: {item.length} units "
            f"({item.source}{owner_note})"
        )
    for item in sorted(staged_over, key=lambda i: (i.length, str(i.source))):
        print(
            f"  STAGED {_destination_report_label(item.destination_index)} "
            f"{item.kind}: {item.length} units "
            f"(status=warn-only until Phase 2 follow-up)"
        )
    for error in sorted(errors, key=lambda e: str(e.source)):
        if error.owner == "destination":
            print(
                f"  FAIL {_destination_report_label(error.destination_index)} "
                f"{error.kind}: status=parse/source-error"
            )
            continue
        print(
            f"  FAIL {error.owner} {error.kind} {error.name}: {error.message} ({error.source})"
        )

    if hard or errors:
        print(
            f"descriptions: FAILED ({len(hard)} over {DESCRIPTION_HARD_MAX}; {len(errors)} parse/source error(s))",
            file=sys.stderr,
        )
        return False
    print(
        f"descriptions: OK ({len(warnings)} warning(s) above {DESCRIPTION_REVIEW_THRESHOLD}; "
        f"destination hard-limit enforcement {'on' if enforce_destinations else 'staged/warn-only'})"
    )
    return True


def enforce_description_preflight(
    cfg: dict, *, enforce_destinations: bool = False
) -> None:
    items, errors = build_description_corpus(cfg)
    if not print_description_report(
        items, errors, enforce_destinations=enforce_destinations
    ):
        sys.exit("description preflight blocked before copy/compose/link/home writes")


def _clear_staging(*dirs: Path) -> None:
    """Empty the compose staging dirs this engine owns, so a run's outputs are
    exactly what today's recipes produce — no residue from a retired recipe."""
    for d in dirs:
        if d.is_dir() and not d.is_symlink():
            shutil.rmtree(d)
        elif d.is_symlink() or d.exists():
            d.unlink()


def do_global(cfg: dict, log: RunLog, manifest: HomeManifest | None = None) -> None:
    if manifest is None:
        manifest = HomeManifest()
    wanted = [h for h in cfg.get("global", []) if h in VALID_HARNESSES]
    # cursor shares codex's home surface (~/.agents/skills) — collapse it so the
    # same dir is never reconciled twice under two tokens.
    wanted = list(dict.fromkeys("codex" if h == "cursor" else h for h in wanted))
    if not wanted:
        return
    kit = project_root()
    kit_shared = kit / ".shared-llm" / PUBLIC_DIR
    staging = kit / "examples"
    home_dirs = {tok: d for tok, d in _global_home_dirs().items() if tok in wanted}
    log.always(f"global: routing home skills to {', '.join(wanted)}")

    composer = Composer(
        kit,
        output_base=staging,
        shared_root=kit_shared,
        placeholders=KIT_SELF_PLACEHOLDERS,
    )
    # Staging is cumulative on disk, so an output left behind by a RETIRED recipe
    # would be read back as current and redeployed forever. Clear what we own
    # first: the desired set must equal what today's recipes produce.
    _clear_staging(staging / ".claude/skills", staging / "global-staging/skills")
    # Family A — convention skills (staged under examples/global-staging/skills/).
    for recipe in GLOBAL_CONVENTION_SKILLS.values():
        composer.compose_one(kit_shared / recipe)
    # Family B — the whole slash-command group (staged under examples/.claude/skills/).
    composer.compose_dir(kit_shared / "compose/slash-commands")

    installed = uptodate = skipped = pruned = 0
    # Names a previous run linked to a destination that no longer wants them:
    # this run may take them back for the generated skill of the same name.
    reclaimable = manifest.retiring_repo_links(destination_home_links(cfg))

    gen_skills = generated_root() / "skills"

    # Family A: every convention skill goes to every wanted home dir.
    conv_staging = staging / "global-staging/skills"
    for name in GLOBAL_CONVENTION_SKILLS:
        staged = conv_staging / name
        if not staged.is_dir():
            log.always(f"  ⚠ convention skill missing after compose: {staged}")
            continue
        generated = gen_skills / name
        _sync_generated_dir(staged, generated)
        manifest.mark_generated(generated)
        for base in home_dirs.values():
            r = _link_skill_dir(generated, base / name, log, reclaimable)
            installed += r == "installed"
            uptodate += r == "uptodate"
            skipped += r == "skip"
            if r != "skip":
                manifest.record_link(base / name, generated)

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
            generated = gen_skills / name
            if keep:
                _sync_generated_dir(staged, generated)
                manifest.mark_generated(generated)
            for tok in keep:
                r = _link_skill_dir(generated, home_dirs[tok] / name, log, reclaimable)
                installed += r == "installed"
                uptodate += r == "uptodate"
                skipped += r == "skip"
                if r != "skip":
                    manifest.record_link(home_dirs[tok] / name, generated)
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

# Home agent dir per harness token (codex intentionally absent). Pi's lives
# inside its config dir, alongside skills/ and extensions/ — see LEGACY_PI_AGENTS_REL.
GENERIC_AGENT_HOME = {"cc": ".claude/agents", "pi": ".pi/agent/agents"}


def _link_file(source: Path, target: Path, log: RunLog) -> str:
    """Point a single home file (agent persona, hook, statusline) at its
    generated / kit source. A byte-identical real file (the old copy mechanism)
    is upgraded to a symlink; foreign or divergent targets are left alone."""
    kit = project_root()
    if target.is_symlink():
        if os.readlink(target) == str(source):
            return "uptodate"
        # Same predicate as the reconciler: a pre-decoupling link into the kit's
        # managed sub-paths is migrated, while a link to anything else in the
        # checkout — a README, a script — is the user's and is left alone.
        if link_is_ours(target, repo_family(kit), kit):
            target.unlink()
            target.symlink_to(source)
            log(f"    repointed {target}")
            return "installed"
        log(f"    skip {target} (symlink — foreign)")
        return "skip"
    if target.exists():
        # Content AND mode must match: a copy the user chmod-ed is a deliberate
        # local change, so it is not the stale copy we are entitled to replace.
        if _same_file(source, target):
            target.unlink()
            target.symlink_to(source)
            log(f"    migrated copy -> link {target}")
            return "installed"
        log(f"    skip {target} (exists & differs — not ours)")
        return "skip"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.symlink_to(source)
    log(f"    linked {target}")
    return "installed"


def _scaffold_settings(template: Path, target: Path, log: RunLog) -> bool:
    """Copy a settings template into place ONLY when absent — never clobber
    per-machine tweaks. Returns True iff this call CREATED the file, which is the
    only way the kit ever acquires ownership of a settings file."""
    if not template.is_file():
        return False
    if target.is_symlink():
        # exists() is False for a DANGLING symlink, so checking it alone would
        # copy straight THROUGH the link and create whatever it points at — the
        # opposite of never adopting a path we did not create.
        log.always(
            f"  settings: {target} is a symlink -> {os.readlink(target)}; "
            "leaving it alone (the kit never writes through a link)"
        )
        return False
    if target.exists():
        log(f"    settings: preserved existing {target}")
        return False
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(template, target)
    log(f"    settings: scaffolded {target}")
    return True


def _install_claude_runtime(
    kit_shared: Path, log: RunLog, exclude: list[str], manifest: HomeManifest
) -> None:
    src = kit_shared / "llm/claude/common"
    claude_home = HOME / ".claude"

    def skip(p: Path) -> bool:
        if _is_excluded(p, kit_shared, exclude):
            log(f"    exclude: {p.relative_to(kit_shared)}")
            return True
        return False

    # Hooks and the statusline are COPIED into the durable generated tree first;
    # home links only ever point there, never into the repo checkout.
    gen_hooks = generated_root() / "claude/hooks"
    if (src / "hooks").is_dir():
        for hook in sorted((src / "hooks").iterdir()):
            if hook.is_file() and not skip(hook):
                generated = gen_hooks / hook.name
                _sync_generated_file(hook, generated)
                manifest.mark_generated(generated)
                dest = claude_home / "hooks" / hook.name
                if _link_file(generated, dest, log) != "skip":
                    manifest.record_link(dest, generated)
    if (src / "statusline.sh").is_file() and not skip(src / "statusline.sh"):
        generated = generated_root() / "claude/statusline.sh"
        _sync_generated_file(src / "statusline.sh", generated)
        manifest.mark_generated(generated)
        dest = claude_home / "statusline.sh"
        if _link_file(generated, dest, log) != "skip":
            manifest.record_link(dest, generated)
    if not skip(src / "settings.template.json"):
        # Deliberate real file (Claude Code mutates it) — only tracked when WE
        # created it, and never destructively pruned afterwards.
        created = _scaffold_settings(
            src / "settings.template.json", claude_home / "settings.json", log
        )
        manifest.record_settings(claude_home / "settings.json", created=created)


def _migrate_legacy_pi_agents(kit: Path, staged_agents: Path, log: RunLog) -> None:
    """Empty out ~/.pi/agents — where this kit parked Pi personas before it was
    established that Pi reads them from ~/.pi/agent/agents. Removes only what is
    PROVABLY the kit's: its own symlinks into this repo family, and real files
    that are byte-for-byte the composed persona of the same name (a known stale
    copy the kit once wrote).

    A same-name file whose bytes DIFFER is not proven ours — it may be a user's
    hand-edited or independently authored persona — so it is preserved and named
    in a warning, exactly like a live dir keeps a divergent file. Deleting on a
    name match alone would be irreversible data loss. Anything the kit does not
    compose is left alone too, and the dir is removed only once truly empty.

    `HOME` is read here (not captured at import) so a patched test HOME is honoured
    and the real ~/.pi is never touched during tests."""
    legacy = HOME / LEGACY_PI_AGENTS_REL
    if not legacy.is_dir():
        return
    composed = (
        {p.name for p in staged_agents.glob("*.md")}
        if staged_agents.is_dir()
        else set()
    )
    removed = 0
    foreign = []  # names the kit does not compose — genuinely not ours
    divergent = []  # same name as a persona, but the bytes are not what we ship
    for entry in sorted(legacy.iterdir()):
        if entry.is_symlink():
            ours = link_is_ours(entry, repo_family(kit), kit)
        elif entry.is_file() and entry.name in composed:
            # Delete only a byte-identical stale copy; a divergent same-name file
            # is preserved (nothing proves the kit wrote its current bytes).
            if _same_file(staged_agents / entry.name, entry):
                ours = True
            else:
                divergent.append(entry)
                log(
                    f"    legacy: kept {entry} (differs from composed {entry.name} — not deleting)"
                )
                continue
        else:
            ours = False
        if ours:
            entry.unlink()
            removed += 1
            log(f"    legacy: migrated {entry.name}")
        else:
            foreign.append(entry)
            log(f"    legacy: left {entry} (not ours)")
    if divergent:
        log.always(
            "  legacy ~/.pi/agents: PRESERVED "
            + ", ".join(e.name for e in divergent)
            + " — same name as a composed persona but the bytes differ, so the kit will "
            "not delete them; review and remove by hand if they are stale"
        )
    if foreign or divergent:
        log.always(
            f"  legacy ~/.pi/agents: migrated {removed}, left {len(foreign) + len(divergent)} "
            f"in place — Pi never read this dir, so move or delete them"
        )
        return
    legacy.rmdir()
    log.always(f"  legacy ~/.pi/agents: migrated {removed}, removed empty dir")


def do_home_runtime(
    cfg: dict, log: RunLog, manifest: HomeManifest | None = None
) -> None:
    if manifest is None:
        manifest = HomeManifest()
    selected_sets = selected_offering_sets(cfg)
    home_roster = generated_root() / "extensions/common/upagent/offerings.yaml"
    if _generated_ancestors_safe(home_roster):
        roster_changed = materialize_offering_roster(selected_sets, home_roster)
        manifest.mark_generated(home_roster)
        log.always(
            f"  home UpAgent offerings: {', '.join(selected_sets)} "
            f"({'updated' if roster_changed else 'unchanged'})"
        )
    else:
        # Preserve the global flow's existing foreign-symlink contract. The
        # manifest finalizer reports the refusal and leaves the path untouched.
        log.always(
            f"  home UpAgent offerings: skipped unsafe generated path {home_roster}"
        )
    wanted = [h for h in cfg.get("global", []) if h in VALID_HARNESSES]
    # cursor shares codex's home surface (~/.agents/skills) — collapse it so the
    # same dir is never reconciled twice under two tokens.
    wanted = list(dict.fromkeys("codex" if h == "cursor" else h for h in wanted))
    if not wanted:
        return
    kit = project_root()
    kit_shared = kit / ".shared-llm" / PUBLIC_DIR
    staging = kit / "examples"
    exclude = cfg.get("exclude", [])
    log.always(
        f"home-runtime: {', '.join(wanted)}"
        + (f"  (exclude: {', '.join(exclude)})" if exclude else "")
    )

    # 1. Generic agents — compose to staging, deploy into the wanted home dirs.
    composer = Composer(kit, output_base=staging, shared_root=kit_shared)
    _clear_staging(staging / ".claude/agents")
    composer.compose_dir(kit_shared / "compose/agents")
    staged_agents = staging / ".claude/agents"
    agent_bases = [
        HOME / rel for tok, rel in GENERIC_AGENT_HOME.items() if tok in wanted
    ]
    a_ins = a_up = a_sk = 0
    deployed_agents: set[Path] = set()
    gen_agents = generated_root() / "agents"
    if staged_agents.is_dir():
        for staged in sorted(staged_agents.glob("*.md")):
            generated = gen_agents / staged.name
            _sync_generated_file(staged, generated)
            manifest.mark_generated(generated)
            for base in agent_bases:
                r = _link_file(generated, base / staged.name, log)
                a_ins += r == "installed"
                a_up += r == "uptodate"
                a_sk += r == "skip"
                if r != "skip":
                    manifest.record_link(base / staged.name, generated)
                    deployed_agents.add(base / staged.name)
    log.always(
        f"  agents: {a_ins} linked, {a_up} current, {a_sk} skipped (foreign/divergent)"
    )

    # 2. Claude runtime — hooks + statusline + settings scaffold (exclude-aware).
    if "cc" in wanted:
        _install_claude_runtime(kit_shared, log, exclude, manifest)

    # 3. Pi runtime — extension/persona symlinks + settings scaffold. Drop any
    #    source under an exclude path from the link plan before reconciling.
    if "pi" in wanted:
        plan = plan_pi_runtime(kit, exclude)
        counts = reconcile(
            plan,
            repo_family(kit),
            plan_only=False,
            force=False,
            repo_root=kit,
            protect=deployed_agents,
        )
        log.always(
            f"  pi runtime: created {counts['create']}, repointed {counts['repoint']}, "
            f"pruned {counts['prune']}, skipped-foreign {counts['skip-foreign']}"
        )
        for dst, src in plan.desired.items():
            manifest.record_link(dst, src)
            manifest.mark_generated(src)
        _migrate_legacy_pi_agents(kit, staged_agents, log)
        if not _is_excluded(
            kit_shared / "llm/pi/common/settings.template.json", kit_shared, exclude
        ):
            created = _scaffold_settings(
                kit_shared / "llm/pi/common/settings.template.json",
                HOME / ".pi/agent/settings.json",
                log,
            )
            manifest.record_settings(HOME / ".pi/agent/settings.json", created=created)

        herdr_plan = plan_herdr_config(kit)
        for dst, src in herdr_plan.desired.items():
            manifest.record_link(dst, src)
            manifest.mark_generated(src)
        herdr_counts = reconcile(
            herdr_plan,
            repo_family(kit),
            plan_only=False,
            force=False,
            repo_root=kit,
        )
        log.always(
            f"  herdr config: created {herdr_counts['create']}, "
            f"repointed {herdr_counts['repoint']}, pruned {herdr_counts['prune']}, "
            f"skipped-foreign {herdr_counts['skip-foreign']}"
        )


@contextmanager
def home_lock(log: RunLog):
    """Exclusive machine-local lock around EVERY home-path mutation — the
    destination link step as well as the global flow. Two concurrent runs would
    race each other's symlink swaps and manifest writes, so a second run BLOCKS
    (loudly) until the first finishes.

    flock is per open file description, so a nested re-lock inside one process
    would deadlock: callers that already hold it pass lock_held=True instead.

    The lock path is opened O_NOFOLLOW and checked to be a regular file. Opening
    it the obvious way would follow a symlink parked at that predictable name and
    TRUNCATE whatever it pointed at, before any locking even happened."""
    import fcntl
    import stat

    path = HOME / LOCK_PATH_REL
    if path.parent.is_symlink():
        sys.exit(f"error: refusing to lock through a symlinked dir: {path.parent}")
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(str(path), os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    except OSError as exc:
        sys.exit(f"error: cannot open the lock file {path} ({exc}); is it a symlink?")
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            sys.exit(f"error: the lock path {path} is not a regular file")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            log.always(f"  home: another run holds {path} — waiting for it to finish")
            fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def do_global_flow(
    cfg: dict,
    log: RunLog,
    *,
    lock_held: bool = False,
    manifest: HomeManifest | None = None,
) -> None:
    """The ONE global entry point: compose + deploy home skills, deploy the home
    runtime, finalize the manifest, then commit the generated cleanup.

    Finalizing unconditionally is the point. With an empty `global:` list the
    desired set is empty, so finalize prunes everything previous runs deployed
    (mutable settings excepted); short-circuiting instead would strand the whole
    previous deployment in place. Every caller — global, prune, update — routes
    here, so the behaviour cannot differ between them.

    The order of the last two steps is the failure-atomicity contract: home links
    go first, generated sources only once the manifest is durable."""

    def run() -> None:
        # A caller that already mutated home paths (the link step) passes its own
        # manifest so both halves land in ONE transaction.
        m = manifest if manifest is not None else HomeManifest()
        do_global(cfg, log, m)
        do_home_runtime(cfg, log, m)
        # Destination-provided home links are deployed by the link step but
        # recorded here, because this is the only step that always runs — an
        # emptied config has no link step left to speak for them.
        for dest, source in destination_home_links(cfg).items():
            m.record_repo_link(dest, source, log)
        m.finalize(log)
        m.commit_generated(log)

    if lock_held:
        run()
    else:
        with home_lock(log):
            run()


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
    cfg = load_config()
    log = RunLog(verbose=True)
    with home_lock(log):
        manifest = HomeManifest()
        do_link(cfg, log, manifest)
        # Record what this step deployed. Without it, `just link` creates home
        # links nothing knows about, and removing the last destination strands
        # them — the global flow would have no evidence they were ever ours.
        manifest.commit_repo_links(cfg, log)


def cmd_global(args: argparse.Namespace) -> None:
    cfg = load_config()
    enforce_description_preflight(cfg, enforce_destinations=False)
    do_global_flow(cfg, RunLog(verbose=True))


def cmd_descriptions(args: argparse.Namespace) -> None:
    cfg = load_config()
    items, errors = build_description_corpus(cfg)
    if not print_description_report(
        items, errors, enforce_destinations=args.enforce_destinations
    ):
        sys.exit(1)


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

    uses_pi = any(
        "pi" in d.get("harnesses", []) for d in cfg["destinations"]
    ) or "pi" in cfg.get("global", [])
    uses_codex = any(
        wants_codex_surface(d.get("harnesses", [])) for d in cfg["destinations"]
    ) or wants_codex_surface(cfg.get("global", []))

    if uses_pi:
        pi = _pi_global_skills()
        report(f"Pi {pi} has NO cc-*", names(pi, lambda p: p.name.startswith("cc-")))
        report(
            f"Pi {pi} has no broken links",
            names(pi, lambda p: p.is_symlink() and not p.exists()),
        )
    if uses_codex:
        cx = _codex_global_skills()
        report(f"Codex {cx} has NO do-*", names(cx, lambda p: p.name.startswith("do-")))
        report(f"Codex {cx} has NO cc-*", names(cx, lambda p: p.name.startswith("cc-")))

    for d in cfg["destinations"]:
        dest = Path(d["path"]).expanduser()
        cs = dest / ".claude/skills"
        ps = dest / PI_ONLY_SKILLS_DIR
        report(
            f"[{dest.name}] .claude/skills has NO do-*",
            names(cs, lambda p: p.is_dir() and p.name.startswith("do-")),
        )
        report(
            f"[{dest.name}] .pi-skills is do-* only",
            names(ps, lambda p: not p.name.startswith(("do-", "."))),
        )
    return ok


def cmd_check(args: argparse.Namespace) -> None:
    log = RunLog(verbose=True)
    if not do_check(load_config(), log):
        sys.exit("check: FAILED — placement invariants violated (see above)")
    log.always("check: all placement invariants hold ✓")


def cmd_update(args: argparse.Namespace) -> None:
    cfg = load_config()
    # An emptied config is not "nothing to do" — it is a RETIREMENT. If a
    # manifest exists, a previous run deployed something and update must still
    # take the lock and prune it, exactly as the docs promise. The informational
    # error is only for a machine that never deployed anything at all.
    if not has_configured_update_work(cfg) and not manifest_path().is_file():
        sys.exit(
            "error: nothing configured. Run `just configure -d <repo> -l cc,pi` first."
        )
    log_path = _log_path()
    log = RunLog(verbose=args.verbose, path=log_path)
    log.always(f"update: log -> {log_path}")
    enforce_destinations = os.environ.get("SHARED_LLM_ENFORCE_DEST_DESCRIPTIONS") == "1"
    enforce_description_preflight(cfg, enforce_destinations=enforce_destinations)
    if cfg["destinations"]:
        log.always("=== copy ===")
        do_copy(cfg, log)
        log.always("=== compose ===")
        do_compose(cfg, log)
    # One lock spans BOTH home-link steps. The destination link step and the
    # global flow reconcile the same home skill dirs and hand ownership of a name
    # back and forth, so splitting them across two locks would let a concurrent
    # run observe (and act on) the half-reconciled state in between.
    with home_lock(log):
        manifest = HomeManifest()
        log.always("=== link ===")
        do_link(cfg, log, manifest)
        log.always("=== global ===")
        do_global_flow(cfg, log, lock_held=True, manifest=manifest)
    log.always("update: done.")
    log.close()
    if not args.verbose:
        print(f"(run `just update -v` or see {log_path} for the per-file detail)")


def do_reset(cfg: dict, log: RunLog) -> None:
    """Delete the kit-owned state so the next update rebuilds it from scratch:
    each destination's ``.shared-llm/public/`` tree, and the hub's kit-synced
    top-level entries. Never touches a destination's ``this_repo/`` tree, and
    never the hub's ``generated/`` tree or ``manifest.json`` — home symlinks
    point into those, and the update flow reconciles them in place."""
    kit_shared = project_root() / ".shared-llm" / PUBLIC_DIR
    hub = Path(cfg["source"]).expanduser()
    if hub.resolve() == kit_shared.resolve():
        sys.exit(
            "error: the hub (`source:` in ~/.shared-llm.yaml) is the kit's own "
            "public/ source tree — refusing to reset it."
        )
    for entry in sorted(kit_shared.iterdir()):
        target = hub / entry.name
        if target.exists():
            log.always(f"reset: rm hub {target}")
            shutil.rmtree(target) if target.is_dir() else target.unlink()
    for d in cfg["destinations"]:
        pub = Path(d["path"]).expanduser() / ".shared-llm" / PUBLIC_DIR
        # The kit's own public/ tree is the SOURCE, not a build product — if the
        # kit itself is registered as a destination, deleting it destroys the kit.
        if pub.resolve() == kit_shared.resolve():
            log.always(f"reset: skip {pub} (this is the kit source tree)")
            continue
        if pub.exists():
            log.always(f"reset: rm {pub}")
            shutil.rmtree(pub)


def cmd_reset(args: argparse.Namespace) -> None:
    cfg = load_config()
    if not cfg["destinations"] and not cfg["global"]:
        sys.exit(
            "error: nothing configured. Run `just configure -d <repo> -l cc,pi` first."
        )
    log = RunLog(verbose=True)
    do_reset(cfg, log)
    log.always("reset: kit-owned state removed — rebuilding via update")
    cmd_update(argparse.Namespace(verbose=args.verbose))


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="harness.py",
        description="Compose layer files and reconcile per-harness symlinks (pi / codex / cursor).",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    pc = sub.add_parser(
        "compose", help="Assemble skills/agents/CLAUDE.md from layer recipes."
    )
    pc.add_argument(
        "recipe",
        nargs="?",
        help=(
            "A specific compose YAML to process, OR a directory of recipes to compose "
            "as a subset (e.g. .shared-llm/public/compose/agents). Default: all recipes."
        ),
    )
    pc.add_argument("--shared-llm", help="Path to the .shared-llm source root.")
    pc.add_argument(
        "--target", help="Output base dir where 'output:' paths land (default: cwd)."
    )
    pc.add_argument(
        "--placeholder",
        action="append",
        metavar="NAME=VALUE",
        help=(
            "Fill a {{NAME}} token in composed output (repeatable). For the kit's "
            "OWN self-compose only (e.g. --placeholder OPS_REPO=your-repo-ops when "
            "regenerating the kit's tracked slash-command skills). Per-destination "
            "fills come from ~/.shared-llm.yaml `placeholders:`, not this flag."
        ),
    )
    pc.set_defaults(func=cmd_compose)

    # --- config-driven, centralized surface (the user-facing flow) ---
    pi = sub.add_parser("init", help="Check OS prerequisites (python3, just).")
    pi.add_argument("-o", "--os", choices=["mac", "ubuntu"], default="ubuntu")
    pi.set_defaults(func=cmd_init)

    pcfg = sub.add_parser("configure", help="Create/update ~/.shared-llm.yaml.")
    pcfg.add_argument(
        "-s", "--source", help="Set the source hub path (default ~/.shared-llm)."
    )
    pcfg.add_argument("-d", "--dest", help="Add/update a destination repo path.")
    pcfg.add_argument(
        "-l",
        "--list",
        help="Harnesses for -d (comma-separated: cc,pi,codex,cursor). Default cc,pi.",
    )
    pcfg.add_argument(
        "-g", "--global-list", help="Set the global harness list (comma-separated)."
    )
    pcfg.add_argument(
        "-x",
        "--exclude",
        help="Set the home-install exclude list: source paths under .shared-llm/ (comma-separated).",
    )
    pcfg.add_argument(
        "--offering-sets",
        help=(
            "Replace the machine UpAgent offering sets, or with -d replace that "
            "destination's sets (comma-separated: standard,claudex)."
        ),
    )
    pcfg.set_defaults(func=cmd_configure)

    pcp = sub.add_parser(
        "copy", help="Kit -> hub -> each destination's .shared-llm/ (common only)."
    )
    pcp.set_defaults(func=cmd_copy)

    pcd = sub.add_parser(
        "compose-dests",
        help="Compose every configured destination from its own .shared-llm/.",
    )
    pcd.set_defaults(func=cmd_compose_cfg)

    pl = sub.add_parser(
        "link", help="Reconcile repo-scoped pi/codex skill links per destination."
    )
    pl.set_defaults(func=cmd_link)

    pg = sub.add_parser(
        "global",
        help="Compose + route the global home skills into ~/.claude, ~/.pi/agent, ~/.agents.",
    )
    pg.set_defaults(func=cmd_global)

    ppr = sub.add_parser(
        "prune",
        help="Re-run the global home flow so manifest-tracked deployments that no "
        "recipe produces anymore are removed (same pruning `update` does).",
    )
    ppr.set_defaults(func=cmd_global)

    pck = sub.add_parser(
        "check",
        help="Verify skill placement per harness (do-* Pi-only, cc-* Claude-only).",
    )
    pck.set_defaults(func=cmd_check)

    pd = sub.add_parser(
        "descriptions",
        help="Audit owned skill/agent discovery descriptions without writing files.",
    )
    pd.add_argument(
        "--enforce-destinations",
        action="store_true",
        help=(
            "Enable the future Phase 2 hard-fail mode for destination-owned "
            "descriptions. Default is public hard-fail plus destination warn-only."
        ),
    )
    pd.set_defaults(func=cmd_descriptions)

    pup = sub.add_parser(
        "update",
        help="copy -> compose -> link (+ global) across all configured destinations.",
    )
    pup.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Print per-file detail (always written to the log).",
    )
    pup.set_defaults(func=cmd_update)

    prs = sub.add_parser(
        "reset",
        help="Delete kit-owned state (hub kit content + every destination's "
        "public/ tree), then rebuild via a full update.",
    )
    prs.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Print per-file detail (always written to the log).",
    )
    prs.set_defaults(func=cmd_reset)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
