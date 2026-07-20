#!/usr/bin/env python3
"""Resolve one Planish output directory deterministically."""

from __future__ import annotations

import argparse
from datetime import date
import json
import os
from pathlib import Path
import re
import sys

import yaml


def _slug(topic: str) -> str:
    value = re.sub(r"[^a-z0-9]+", "-", topic.lower()).strip("-")
    return value or "plan"


def _find_config(start: Path) -> Path | None:
    current = start.resolve()
    for directory in (current, *current.parents):
        candidate = directory / ".planish.yaml"
        if candidate.is_file():
            return candidate
    return None


def _next_version(path: Path) -> Path:
    parts = list(path.parts)
    try:
        index = next(i for i, part in enumerate(parts) if "{n}" in part)
    except StopIteration:
        return path
    segment = parts[index]
    prefix, suffix = segment.split("{n}", 1)
    parent = Path(*parts[:index])
    highest = 0
    if parent.is_dir():
        pattern = re.compile(rf"^{re.escape(prefix)}(\d+){re.escape(suffix)}$")
        for child in parent.iterdir():
            match = pattern.match(child.name)
            if match:
                highest = max(highest, int(match.group(1)))
    parts[index] = f"{prefix}{highest + 1}{suffix}"
    return Path(*parts)


def resolve(cwd: Path, topic: str, explicit_dir: str | None = None) -> Path:
    if not topic.strip():
        raise ValueError("topic must be non-empty")
    config = _find_config(cwd)
    if explicit_dir and explicit_dir.strip():
        template = explicit_dir.strip()
        base = cwd
    elif os.environ.get("PLANISH_DIR", "").strip():
        template = os.environ["PLANISH_DIR"].strip()
        base = cwd
    elif config is not None:
        try:
            value = yaml.safe_load(config.read_text()) or {}
        except (OSError, yaml.YAMLError) as error:
            raise ValueError(f"{config} is invalid: {error}") from error
        if not isinstance(value, dict):
            raise ValueError(f"{config} must contain a YAML mapping")
        if "dir" in value:
            configured = value["dir"]
            if not isinstance(configured, str) or not configured.strip():
                raise ValueError(f'{config} "dir" must be a non-empty string')
            template = configured.strip()
            base = config.parent
        else:
            template = "/tmp/planish/{date}/{slug}"
            base = cwd
    else:
        template = "/tmp/planish/{date}/{slug}"
        base = cwd

    expanded = (
        template.replace("{date}", date.today().isoformat())
        .replace("{slug}", _slug(topic))
        .replace("{type}", "plan")
    )
    candidate = Path(expanded).expanduser()
    if not candidate.is_absolute():
        candidate = base / candidate
    resolved = _next_version(candidate.resolve())
    resolved.mkdir(parents=True, exist_ok=True)
    return resolved


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="planish-resolve")
    parser.add_argument("--topic", required=True)
    parser.add_argument("--dir")
    parser.add_argument("--cwd", type=Path, default=Path.cwd())
    args = parser.parse_args(argv)
    try:
        directory = resolve(args.cwd.resolve(), args.topic, args.dir)
    except (OSError, ValueError) as error:
        sys.exit(f"planish-resolve: {error}")
    print(json.dumps({"plan_dir": str(directory)}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
