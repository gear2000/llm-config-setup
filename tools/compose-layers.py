#!/usr/bin/env python3
"""Assemble layer .md files into skill, agent, CLAUDE.md, and AGENTS.md definitions.

Reads compose YAML files from layers/compose/ that specify which layer
markdown files to concatenate, and produces output .md files with proper
formatting for the target type.

Compose YAML types:
    type: skill      — YAML frontmatter with name + description (default if no type)
    type: agent      — YAML frontmatter with name + description + model
    type: claude-md  — plain concatenated markdown, no frontmatter (CLAUDE.md)
    type: agents-md  — plain concatenated markdown, no frontmatter (AGENTS.md)

Usage:
    python tools/compose-layers.py                              # compose all
    python tools/compose-layers.py layers/compose/skills/x.yaml # compose one
    python tools/compose-layers.py --dry-run                    # preview only
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

import yaml


def find_repo_root() -> Path:
    """Walk up from script location looking for layers/ directory."""
    candidate = Path(__file__).resolve().parent
    for _ in range(10):
        if (candidate / "layers").is_dir():
            return candidate
        candidate = candidate.parent
    # Fall back to cwd
    cwd = Path.cwd()
    if (cwd / "layers").is_dir():
        return cwd
    print("error: cannot find repo root (no layers/ directory found)", file=sys.stderr)
    sys.exit(1)


def read_file(path: Path) -> str:
    """Read a file, failing hard if it doesn't exist."""
    if not path.exists():
        print(f"error: file not found: {path}", file=sys.stderr)
        sys.exit(1)
    return path.read_text()


VALID_TYPES = {"skill", "agent", "claude-md", "agents-md"}


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

    if compose_type not in ("claude-md", "agents-md"):
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

    def write(self, data: dict[str, Any], body: str, output_path: Path, *, dry_run: bool) -> None:
        description = data["_description_content"]
        frontmatter = self.build_frontmatter(data["name"], description)
        content = f"---\n{frontmatter}---\n\n{body}"

        if dry_run:
            print(f"  [dry-run] would write skill -> {output_path}")
            return

        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(content)


class ClaudeAgent:
    """Produces an agent .md file with name + description + model frontmatter."""

    def build_frontmatter(self, name: str, description: str, model: str) -> str:
        fm: dict[str, Any] = {"name": name, "description": description, "model": model}
        return yaml.dump(fm, default_flow_style=False, sort_keys=False, allow_unicode=True)

    def write(self, data: dict[str, Any], body: str, output_path: Path, *, dry_run: bool) -> None:
        description = data["_description_content"]
        model = data["model"]
        frontmatter = self.build_frontmatter(data["name"], description, model)
        content = f"---\n{frontmatter}---\n\n{body}"

        if dry_run:
            print(f"  [dry-run] would write agent -> {output_path}")
            return

        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(content)


class ClaudeMd:
    """Produces a plain markdown file with no frontmatter."""

    def write(self, data: dict[str, Any], body: str, output_path: Path, *, dry_run: bool) -> None:
        if dry_run:
            print(f"  [dry-run] would write claude-md -> {output_path}")
            return

        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(body)


class AgentsMd:
    """Produces a plain markdown file for AGENTS.md (cross-harness instruction files)."""

    def write(self, data: dict[str, Any], body: str, output_path: Path, *, dry_run: bool) -> None:
        if dry_run:
            print(f"  [dry-run] would write agents-md -> {output_path}")
            return

        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(body)


# ---------------------------------------------------------------------------
# Composer
# ---------------------------------------------------------------------------

class Composer:
    """Discovers compose YAMLs and dispatches to the right output handler."""

    def __init__(self, repo_root: Path) -> None:
        self.root = repo_root
        self.skill_handler = ClaudeSkill()
        self.agent_handler = ClaudeAgent()
        self.claude_md_handler = ClaudeMd()
        self.agents_md_handler = AgentsMd()

    def resolve(self, relative: str) -> Path:
        """Resolve a path relative to repo root."""
        return self.root / relative

    def discover(self) -> list[Path]:
        """Find all YAML files under layers/compose/."""
        compose_dir = self.root / "layers" / "compose"
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

    def compose_one(self, yaml_path: Path, *, dry_run: bool) -> None:
        """Process a single compose YAML."""
        data = load_compose_yaml(yaml_path)
        compose_type = self._resolve_type(data)

        if compose_type not in ("claude-md", "agents-md"):
            desc_path = self.resolve(data["description"])
            data["_description_content"] = read_file(desc_path).strip()

        parts: list[str] = []
        for input_rel in data["inputs"]:
            input_path = self.resolve(input_rel)
            parts.append(read_file(input_path).rstrip())

        separator = "\n\n---\n\n" if compose_type in ("claude-md", "agents-md") else "\n"
        body = separator.join(parts) + "\n"
        output_path = self.resolve(data["output"])
        label = data.get("name", output_path.name)

        print(f"  {compose_type}: {label} -> {output_path}")

        match compose_type:
            case "claude-md":
                self.claude_md_handler.write(data, body, output_path, dry_run=dry_run)
            case "agents-md":
                self.agents_md_handler.write(data, body, output_path, dry_run=dry_run)
            case "agent":
                self.agent_handler.write(data, body, output_path, dry_run=dry_run)
            case _:
                self.skill_handler.write(data, body, output_path, dry_run=dry_run)

    def compose_all(self, *, dry_run: bool) -> None:
        """Discover and compose all targets."""
        yamls = self.discover()
        for yp in yamls:
            self.compose_one(yp, dry_run=dry_run)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Assemble layer files into skill, agent, CLAUDE.md, and AGENTS.md definitions."
    )
    parser.add_argument(
        "target",
        nargs="?",
        help="Specific compose YAML to process (default: all under layers/compose/)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be generated without writing files",
    )
    args = parser.parse_args()

    repo_root = find_repo_root()
    composer = Composer(repo_root)

    print(f"repo root: {repo_root}")

    if args.target:
        target = Path(args.target)
        if not target.is_absolute():
            target = repo_root / target
        composer.compose_one(target, dry_run=args.dry_run)
    else:
        composer.compose_all(dry_run=args.dry_run)

    print("done.")


if __name__ == "__main__":
    main()
