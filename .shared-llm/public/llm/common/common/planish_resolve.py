#!/usr/bin/env python3
"""Resolve one work-log output directory deterministically."""

from __future__ import annotations

import argparse
from datetime import date
import json
import os
from pathlib import Path
import re
import sys

import yaml

CONFIG_NAME = ".shared-llm.yaml"
LEGACY_CONFIG_NAME = ".planish.yaml"
DEFAULT_TEMPLATE = "/var/tmp/work-log/{date}/{slug}"


def _warn(message: str) -> None:
    """Deprecation notices go to stderr — stdout stays pure JSON."""
    print(f"planish-resolve: {message}", file=sys.stderr)


def _slug(topic: str) -> str:
    value = re.sub(r"[^a-z0-9]+", "-", topic.lower()).strip("-")
    return value or "plan"


def _load_mapping(path: Path) -> dict:
    try:
        value = yaml.safe_load(path.read_text()) or {}
    except (OSError, yaml.YAMLError) as error:
        raise ValueError(f"{path} is invalid: {error}") from error
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain a YAML mapping")
    return value


def _find_work_log(start: Path) -> tuple[Path, dict] | None:
    """Nearest `.shared-llm.yaml` carrying a `work_log:` mapping.

    A `.shared-llm.yaml` without `work_log:` is skipped and the walk continues —
    `~/.shared-llm.yaml` is the machine-level destination roster and must never
    shadow a repo's work-log config.
    """
    current = start.resolve()
    for directory in (current, *current.parents):
        candidate = directory / CONFIG_NAME
        if not candidate.is_file():
            continue
        value = _load_mapping(candidate)
        if "work_log" not in value:
            continue
        work_log = value["work_log"]
        if not isinstance(work_log, dict):
            raise ValueError(f'{candidate} "work_log" must be a mapping')
        if not work_log:
            # `work_log: {}` would otherwise fall through to the default
            # directory — the silent fallback this contract exists to prevent.
            raise ValueError(
                f'{candidate} "work_log" is empty — set "dir" and/or "host" under it'
            )
        return candidate, work_log
    return None


def _find_legacy(start: Path) -> tuple[Path, dict] | None:
    current = start.resolve()
    for directory in (current, *current.parents):
        candidate = directory / LEGACY_CONFIG_NAME
        if candidate.is_file():
            return candidate, _load_mapping(candidate)
    return None


def _string_field(config: Path, mapping: dict, key: str, label: str) -> str | None:
    if key not in mapping:
        return None
    value = mapping[key]
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f'{config} "{label}" must be a non-empty string')
    return value.strip()


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


def _template(cwd: Path, explicit_dir: str | None) -> tuple[str, Path]:
    """Return the (template, base directory) pair the precedence selects."""
    if explicit_dir and explicit_dir.strip():
        return explicit_dir.strip(), cwd
    if os.environ.get("WORK_LOG_DIR", "").strip():
        return os.environ["WORK_LOG_DIR"].strip(), cwd
    if os.environ.get("PLANISH_DIR", "").strip():
        _warn("$PLANISH_DIR is deprecated — use $WORK_LOG_DIR")
        return os.environ["PLANISH_DIR"].strip(), cwd

    found = _find_work_log(cwd)
    if found is not None:
        config, work_log = found
        configured = _string_field(config, work_log, "dir", "work_log.dir")
        if configured is None:
            return DEFAULT_TEMPLATE, cwd
        return configured, config.parent

    legacy = _find_legacy(cwd)
    if legacy is not None:
        config, mapping = legacy
        configured = _string_field(config, mapping, "dir", "dir")
        if configured is None:
            return DEFAULT_TEMPLATE, cwd
        _warn(
            f"{config} is deprecated — move `dir:` into {CONFIG_NAME} "
            "under `work_log.dir`"
        )
        return configured, config.parent

    return DEFAULT_TEMPLATE, cwd


def resolve(cwd: Path, topic: str, explicit_dir: str | None = None) -> Path:
    if not topic.strip():
        raise ValueError("topic must be non-empty")
    template, base = _template(cwd, explicit_dir)

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


def resolve_host(cwd: Path) -> str | None:
    """Hostname the review URLs use, or None for the harness default."""
    if os.environ.get("WORK_LOG_HOST", "").strip():
        return os.environ["WORK_LOG_HOST"].strip()
    if os.environ.get("PLANISH_HOST", "").strip():
        _warn("$PLANISH_HOST is deprecated — use $WORK_LOG_HOST")
        return os.environ["PLANISH_HOST"].strip()

    found = _find_work_log(cwd)
    if found is not None:
        config, work_log = found
        return _string_field(config, work_log, "host", "work_log.host")

    legacy = _find_legacy(cwd)
    if legacy is not None:
        config, mapping = legacy
        host = _string_field(config, mapping, "host", "host")
        if host is not None:
            _warn(
                f"{config} is deprecated — move `host:` into {CONFIG_NAME} "
                "under `work_log.host`"
            )
        return host
    return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="planish-resolve")
    parser.add_argument("--topic", required=True)
    parser.add_argument("--dir")
    parser.add_argument("--cwd", type=Path, default=Path.cwd())
    args = parser.parse_args(argv)
    cwd = args.cwd.resolve()
    try:
        directory = resolve(cwd, args.topic, args.dir)
        host = resolve_host(cwd)
    except (OSError, ValueError) as error:
        sys.exit(f"planish-resolve: {error}")
    print(json.dumps({"host": host, "plan_dir": str(directory)}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
