#!/usr/bin/env python3
"""Resolve one work-log output directory deterministically."""

from __future__ import annotations

import argparse
from collections.abc import Callable
from datetime import date
import ipaddress
import json
import os
from pathlib import Path
import re
import sys
from typing import Any
from urllib.parse import quote, urlsplit

import yaml

CONFIG_NAME = ".shared-llm.yaml"
LEGACY_CONFIG_NAME = ".planish.yaml"
DEFAULT_TEMPLATE = "/var/tmp/work-log/{date}/{slug}"
_CLAIM_ATTEMPTS = 64
# The only schemes a review URL can be handed to a human in. `file:` needs no
# base, and anything else either does not fetch or is not a location at all.
_URL_SCHEMES = ("http", "https")
# Dot-separated host labels — a DNS name or an IPv4 literal, matched against the
# lowercased `.hostname`. IP literals in brackets are checked by urlsplit itself.
_LABEL = r"[a-z0-9_](?:[a-z0-9_-]*[a-z0-9_])?"
_HOSTNAME_RE = re.compile(rf"{_LABEL}(?:\.{_LABEL})*\.?")
# DNS limits the pattern says nothing about: a label is at most 63 characters
# and a name at most 253 once the root's trailing dot is dropped. Past either,
# no resolver will answer for it.
_MAX_LABEL = 63
_MAX_HOSTNAME = 253


def _warn(message: str) -> None:
    """Deprecation notices go to stderr — stdout stays pure JSON."""
    print(f"planish-resolve: {message}", file=sys.stderr)


def _slug(topic: str) -> str:
    value = re.sub(r"[^a-z0-9]+", "-", topic.lower()).strip("-")
    return value or "plan"


_MERGE_TAG = "tag:yaml.org,2002:merge"


class _NoDuplicateKeyLoader(yaml.SafeLoader):
    """SafeLoader that refuses a duplicate mapping key instead of keeping the last one.

    PyYAML's default is last-one-wins, silently. A config with `dir:` written
    twice then resolves to a directory nobody configured, and a second
    `url_base:` hands a human a review URL nobody wrote — exactly the quiet pick
    every other check here exists to prevent. Two keys with one name are never a
    legible intent, so the duplicate is the fault, at every level: a repeated
    top-level `work_log:` and a repeated `dir:` inside it fail the same way.
    """

    def __init__(self, stream: Any, source: Path) -> None:
        super().__init__(stream)
        self.source = source

    def construct_mapping(
        self, node: yaml.MappingNode, deep: bool = False
    ) -> dict[Any, Any]:
        # What is rejected is one *syntactic* level naming an ordinary key twice.
        # A `<<:` merge key is not one of those: it is an instruction to pull in
        # another mapping, `super()` flattens it below, and YAML lets a block
        # carry more than one, so repeated `<<:` is valid and passes. A key that
        # only collides once merged is likewise not a duplicate — the explicit
        # key wins per the merge spec, which is what an anchor is written for.
        seen: list[Any] = []
        for key_node, _ in node.value:
            if key_node.tag == _MERGE_TAG:
                continue
            # Keys are compared by equality in a list, never hashed: an unhashable
            # key is PyYAML's own error to report, below, with its own line.
            key = self.construct_object(key_node, deep=True)
            if key in seen:
                raise ValueError(
                    f"{self.source} sets {key!r} twice in one block "
                    f"(line {key_node.start_mark.line + 1}); the later value "
                    "would silently replace the earlier one"
                )
            seen.append(key)
        return super().construct_mapping(node, deep=deep)


def _load_mapping(path: Path) -> dict:
    try:
        # `yaml.load` with a SafeLoader subclass — the same safe tag set as
        # `yaml.safe_load`, plus the duplicate-key refusal, and the source path
        # so the refusal can name the file it read.
        value = (
            yaml.load(
                path.read_text(), lambda stream: _NoDuplicateKeyLoader(stream, path)
            )
            or {}
        )
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


def _claim(candidate: Path) -> Path:
    """Create the resolved directory, claiming a `{n}` version exclusively.

    Scanning for the highest sibling and then creating with `exist_ok=True`
    hands the same directory to every caller that scans before any of them
    creates. The version segment is therefore claimed with a non-recursive
    `mkdir(exist_ok=False)`: whoever loses the race sees FileExistsError,
    rescans, and takes the next integer.
    """
    parts = candidate.parts
    try:
        index = next(i for i, part in enumerate(parts) if "{n}" in part)
    except StopIteration:
        # No version token — concurrent callers legitimately share the path.
        candidate.mkdir(parents=True, exist_ok=True)
        return candidate

    for _ in range(_CLAIM_ATTEMPTS):
        versioned = _next_version(candidate)
        claim = Path(*versioned.parts[: index + 1])
        claim.parent.mkdir(parents=True, exist_ok=True)
        try:
            claim.mkdir(exist_ok=False)
        except FileExistsError:
            continue
        versioned.mkdir(parents=True, exist_ok=True)
        return versioned
    raise ValueError(
        f"could not claim a version directory under {Path(*parts[:index])} "
        f"after {_CLAIM_ATTEMPTS} attempts"
    )


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
    return _claim(candidate.resolve())


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


def _artifact_parts(plan_dir: Path, artifact: str) -> list[str]:
    """Path components of `artifact`, checked against the plan directory.

    A URL is only worth handing to a human if the file behind it is there, so
    the name is resolved on disk before it is allowed into one: an artifact that
    leaves the plan directory (a `..`, an absolute path, a symlink pointing
    out), or that is not an existing regular file inside it, is a loud failure
    rather than a URL that answers 404.
    """
    if artifact.startswith("/"):
        raise ValueError(
            f'artifact "{artifact}" must be relative to the plan directory'
        )
    parts = [part for part in artifact.split("/") if part]
    if not parts or any(not part.strip() for part in parts):
        raise ValueError(f'artifact "{artifact}" must be a non-empty file name')
    if any(part in (".", "..") for part in parts):
        raise ValueError(f'artifact "{artifact}" must stay inside the plan directory')

    root = plan_dir.resolve()
    target = root.joinpath(*parts).resolve()
    if not target.is_relative_to(root):
        raise ValueError(
            f'artifact "{artifact}" must stay inside the plan directory {root}'
        )
    if not target.is_file():
        raise ValueError(
            f'artifact "{artifact}" is not a file in the plan directory {root} — '
            "name the page you actually wrote"
        )
    return parts


def _check_name_host(host: str, fault: Callable[[str], ValueError]) -> None:
    """Check an unbracketed host as what it actually is — a literal or a name.

    Four numeric labels satisfy any label pattern, so `256.256.256.256` reads as
    a DNS name and passes one — then no resolver answers for it and `new URL()`
    throws on the "review link" it produced. An all-numeric host is an IPv4
    literal or nothing, so it is checked as one; a real name is checked against
    the DNS length limits, which the label pattern does not express.
    """
    name = host[:-1] if host.endswith(".") else host
    labels = name.split(".")
    if name and all(label.isdigit() for label in labels):
        try:
            ipaddress.IPv4Address(name)
        except ValueError as error:
            raise fault(
                f'names "{host}", which is not a usable IPv4 address'
            ) from error
        return

    if _HOSTNAME_RE.fullmatch(host) is None:
        raise fault(f'names an invalid host "{host}"')
    if len(name) > _MAX_HOSTNAME:
        raise fault(
            f"names a host of {len(name)} characters, past the {_MAX_HOSTNAME} "
            "DNS allows"
        )
    for label in labels:
        if len(label) > _MAX_LABEL:
            raise fault(
                f'names a host whose label "{label[:20]}…" is {len(label)} '
                f"characters, past the {_MAX_LABEL} DNS allows"
            )


def _checked_url_base(config: Path, url_base: str) -> str:
    """`url_base` without its trailing slash, checked as a base a browser opens.

    The plan path is appended to this base as PATH, so the base has to be an
    absolute http(s) URL naming a real host: `http://` names no host, `not a
    url` names no scheme, and `javascript:alert(1)` is not a location at all —
    each one hands a human a "review link" that is dead or worse.

    A base carrying a query or a fragment (`…?token=x`, `…#top`) puts that path
    inside the query or the fragment instead, where the server never sees it —
    and a bare `?` or `#` does it just as thoroughly, so the delimiter alone is
    the fault. Neither is reordered around the path: a base that cannot carry
    one is rejected.
    """

    def fault(reason: str) -> ValueError:
        return ValueError(f'{config} "work_log.url_base" {reason}: "{url_base}"')

    try:
        split = urlsplit(url_base)
        # `.hostname` and `.port` parse the netloc lazily, so an unclosed
        # bracket, a bad IPv6 literal, or a port that is not an integer in range
        # surfaces on access — here, rather than in whatever opens the link.
        host, _port = split.hostname, split.port
    except ValueError as error:
        raise fault(f"is not a usable URL ({error})") from error

    if split.scheme not in _URL_SCHEMES:
        raise fault(
            "must be an absolute URL starting with "
            + " or ".join(f"{scheme}://" for scheme in _URL_SCHEMES)
        )
    # urlsplit drops tabs and newlines outright, so the parsed host is only as
    # trustworthy as the raw string: "http://exa\nmple" parses as "example".
    if any(
        character.isspace() or ord(character) < 32 or ord(character) == 127
        for character in url_base
    ):
        raise fault("must not contain whitespace or control characters")
    if not host:
        raise fault("names no host, so there is nothing to serve the plan path")
    # A bracketed host is an IP literal `.hostname` validated above; anything
    # else is a name or an IPv4 literal, checked as whichever it is.
    if not split.netloc.rpartition("@")[2].startswith("["):
        _check_name_host(host, fault)

    for label, delimiter, component in (
        ("query", "?", split.query),
        ("fragment", "#", split.fragment),
    ):
        if component or delimiter in url_base:
            raise ValueError(
                f'{config} "work_log.url_base" must not carry a {label} '
                f'("{delimiter}") — the plan path is appended to it as path, so '
                f'"{url_base}" would put that path in the {label}'
            )
    return url_base.rstrip("/")


def resolve_review_url(
    cwd: Path, plan_dir: Path, artifact: str | None = None
) -> str | None:
    """Browser URL for `plan_dir` — or for `artifact` inside it — else None.

    Requires BOTH `work_log.url_base` (where the static server answers) and
    `work_log.serve_root` (the filesystem root it serves) — with only one of
    them, or with a plan directory outside that root, there is no way to know
    the URL, so this returns None rather than guessing one that 404s.

    Name an `artifact` (`plan.html`) and the URL is the page itself; without one
    it is the directory, ending in a slash so a static server lists it instead
    of redirecting. A named artifact must be an existing regular file inside the
    plan directory — checked before any URL is built, so a typo fails loudly on
    both the configured and the unconfigured machine instead of only on the one
    that hands back a 404. Every component is percent-encoded, so a `#`, a
    space, or a `%` in a configured directory stays part of the PATH rather than
    turning into a fragment or a query the server never sees.
    """
    artifact_parts = None if artifact is None else _artifact_parts(plan_dir, artifact)

    found = _find_work_log(cwd)
    if found is None:
        return None
    config, work_log = found
    url_base = _string_field(config, work_log, "url_base", "work_log.url_base")
    serve_root = _string_field(config, work_log, "serve_root", "work_log.serve_root")
    if url_base is None or serve_root is None:
        return None
    base = _checked_url_base(config, url_base)

    root = Path(serve_root).expanduser()
    if not root.is_absolute():
        root = config.parent / root
    try:
        relative = plan_dir.relative_to(root.resolve())
    except ValueError:
        return None

    parts = [] if relative == Path(".") else list(relative.parts)
    if artifact_parts is not None:
        parts.extend(artifact_parts)
    url_path = "".join(f"/{quote(part, safe='')}" for part in parts)
    return f"{base}{url_path}" if artifact is not None else f"{base}{url_path}/"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="planish-resolve")
    parser.add_argument("--topic")
    parser.add_argument("--dir")
    parser.add_argument("--cwd", type=Path, default=Path.cwd())
    parser.add_argument(
        "--artifact",
        help="file inside the plan directory the review URL should name, e.g. "
        "plan.html; it must already exist there, and without it the URL names "
        "the directory",
    )
    parser.add_argument(
        "--host-only",
        action="store_true",
        help="resolve the review host alone and create nothing — plan_dir and "
        "review_url come back null",
    )
    args = parser.parse_args(argv)
    cwd = args.cwd.resolve()

    if args.host_only:
        try:
            host = resolve_host(cwd)
        except (OSError, ValueError) as error:
            sys.exit(f"planish-resolve: {error}")
        print(
            json.dumps(
                {"host": host, "plan_dir": None, "review_url": None}, sort_keys=True
            )
        )
        return 0

    if not args.topic:
        parser.error("--topic is required unless --host-only is given")
    try:
        directory = resolve(cwd, args.topic, args.dir)
        host = resolve_host(cwd)
        review_url = resolve_review_url(cwd, directory, args.artifact)
    except (OSError, ValueError) as error:
        sys.exit(f"planish-resolve: {error}")
    print(
        json.dumps(
            {"host": host, "plan_dir": str(directory), "review_url": review_url},
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
