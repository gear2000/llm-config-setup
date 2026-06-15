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

Optional keys:
    catalog: <path>  — shared partial injected FIRST before 'inputs' (single-source for
                       content repeated across many outputs, e.g. the service catalog).
                       Exactly one source file; the tool reads it once and prepends it.

Usage:
    python tools/compose-layers.py                                       # compose all
    python tools/compose-layers.py .shared-llm/compose/skills/x.yaml     # compose one
    python tools/compose-layers.py --shared-llm /path/.shared-llm --target /path/out
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import sys
from typing import Any

import yaml


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


VALID_TYPES = {"skill", "agent", "claude-md", "agents-md", "prompt"}

# Types that produce plain concatenated markdown with no name/description
# frontmatter (CLAUDE.md / AGENTS.md / whole feature prompts).
PLAIN_TYPES = ("claude-md", "agents-md", "prompt")


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

    if compose_type not in PLAIN_TYPES:
        for field in ("name", "description"):
            if field not in data:
                print(f"error: {path} missing required field: {field}", file=sys.stderr)
                sys.exit(1)
        if not isinstance(data["description"], str):
            print(f"error: {path} 'description' must be a string", file=sys.stderr)
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
    """Produces a skill .md file with name + description frontmatter."""

    def build_frontmatter(self, name: str, description: str) -> str:
        fm: dict[str, Any] = {"name": name, "description": description}
        return yaml.dump(fm, default_flow_style=False, sort_keys=False, allow_unicode=True)

    def write(self, data: dict[str, Any], body: str, output_path: Path) -> None:
        description = data["_description_content"]
        frontmatter = self.build_frontmatter(data["name"], description)
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
        return yaml.dump(fm, default_flow_style=False, sort_keys=False, allow_unicode=True)

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

    def compose_all(self) -> None:
        """Discover and compose all targets."""
        yamls = self.discover()
        for yp in yamls:
            self.compose_one(yp)

    def compose_dir(self, recipe_dir: Path) -> None:
        """Compose every recipe under a single recipe directory (a subset)."""
        yamls = self.discover(recipe_dir)
        for yp in yamls:
            self.compose_one(yp)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Assemble layer files into skill, agent, CLAUDE.md, and AGENTS.md definitions."
    )
    parser.add_argument(
        "recipe",
        nargs="?",
        help=(
            "A specific compose YAML to process, OR a directory of recipes to compose "
            "as a subset (e.g. .shared-llm/compose/agents). Default: all recipes under "
            "<shared-llm>/compose/."
        ),
    )
    parser.add_argument(
        "--shared-llm",
        help="Path to the .shared-llm source root (default: $SHARED_LLM_DIR, then walk up for .shared-llm/)",
    )
    parser.add_argument(
        "--target",
        help="Output base directory where 'output:' paths land (default: current working directory)",
    )
    args = parser.parse_args()

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


if __name__ == "__main__":
    main()
