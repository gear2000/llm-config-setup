#!/usr/bin/env python3
"""Validated public offering roster and code-owned harness renderers."""

from __future__ import annotations

import os
import re
import shlex
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

import yaml

EFFORTS = ("low", "medium", "high", "xhigh", "max")
# Canonical selection for offerings whose harness exposes no effort control.
# Omitted and explicit "default" requests normalize to the same snapshot/hash.
DEFAULT_EFFORT = "default"
_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")
CANONICAL_REPO_ENV = "UPAGENT_CANONICAL_REPO"


class _UniqueKeyLoader(yaml.SafeLoader):
    """Safe YAML loader that rejects duplicate mapping keys at every level."""


def _construct_unique_mapping(
    loader: _UniqueKeyLoader, node: yaml.MappingNode, deep: bool = False
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


_UniqueKeyLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _construct_unique_mapping
)


def _load_yaml(text: str) -> object:
    return yaml.load(text, Loader=_UniqueKeyLoader)


APPROVED: dict[str, tuple[str, str, tuple[str, ...], str]] = {
    "claude-fable-5-1": ("claude", "claude-fable-5-1", EFFORTS, "anthropic"),
    "claude-sonnet-5": ("claude", "claude-sonnet-5", EFFORTS, "anthropic"),
    "claude-opus-4-8": ("claude", "claude-opus-4-8", EFFORTS, "anthropic"),
    "codex-gpt-5-6-sol": ("codex", "gpt-5.6-sol", EFFORTS, "openai"),
    # Cursor model ids carry their native effort tier, so each public cursor
    # offering exposes the canonical default selection only.
    "cursor-composer-2-5": (
        "cursor",
        "composer-2.5",
        (DEFAULT_EFFORT,),
        "cursor",
    ),
    "cursor-grok-4-6": (
        "cursor",
        "cursor-grok-4.6-high",
        (DEFAULT_EFFORT,),
        "xai",
    ),
    "cursor-opus-4-6": (
        "cursor",
        "claude-4.6-opus-high",
        (DEFAULT_EFFORT,),
        "anthropic",
    ),
    "cursor-sonnet-4-6": (
        "cursor",
        "claude-4.6-sonnet-medium",
        (DEFAULT_EFFORT,),
        "anthropic",
    ),
    "cursor-fable-5-1": (
        "cursor",
        "claude-fable-5-1-high",
        (DEFAULT_EFFORT,),
        "anthropic",
    ),
    "cursor-gpt-5-5": (
        "cursor",
        "gpt-5.5-high",
        (DEFAULT_EFFORT,),
        "openai",
    ),
    "cursor-gpt-5-6-sol": (
        "cursor",
        "gpt-5.6-sol-high",
        (DEFAULT_EFFORT,),
        "openai",
    ),
    "cursor-gpt-5-6-terra": (
        "cursor",
        "gpt-5.6-terra-high",
        (DEFAULT_EFFORT,),
        "openai",
    ),
    "pi-gpt-5-6-sol": ("pi", "openai-codex/gpt-5.6-sol", EFFORTS, "openai"),
    "pi-gpt-5-6-terra": ("pi", "openai-codex/gpt-5.6-terra", EFFORTS, "openai"),
    "pi-gpt-5-6-luna": ("pi", "openai-codex/gpt-5.6-luna", EFFORTS, "openai"),
    "pi-gpt-5-5": ("pi", "openai-codex/gpt-5.5", EFFORTS[:-1], "openai"),
    "pi-gpt-5-4-mini": (
        "pi",
        "openai-codex/gpt-5.4-mini",
        EFFORTS[:-1],
        "openai",
    ),
    "pi-glm-5-3-flash": (
        "pi",
        "openrouter/z-ai/glm-5.3-flash",
        ("low", "high", "max"),
        "openrouter",
    ),
    "claudex-gpt-5-6-sol": ("claudex", "gpt-5.6-sol", EFFORTS, "openai"),
}

STANDARD_IDS = tuple(
    offering_id for offering_id in APPROVED if offering_id != "claudex-gpt-5-6-sol"
)
APPROVED_SETS: dict[str, tuple[str, ...]] = {
    "standard": STANDARD_IDS,
    "claudex": ("claudex-gpt-5-6-sol",),
}
DEFAULT_SETS = ("standard",)
ROSTER_RELATIVE_PATH = Path(
    ".shared-llm/public/extensions/common/upagent/offerings.yaml"
)
MAIN_BRANCH_REF = "refs/heads/main"


# Harness completion semantics. "exec" harnesses run one non-interactive
# process that exits when its turn is done: the Herdr agent disappears, so no
# follow-up repair prompt is ever possible. "interactive" harnesses keep an
# addressable agent that can accept exactly one same-worker repair prompt.
COMPLETION_STYLES: dict[str, str] = {
    "claude": "interactive",
    "claudex": "interactive",
    "codex": "exec",
    "cursor": "interactive",
    "pi": "interactive",
}

# Herdr health identity is code-owned for the same reason command rendering is:
# public YAML selects offerings, but cannot select an executable or process identity.
MANAGEMENT_HEALTH: dict[str, tuple[str, str]] = {
    "claude": ("claude", "claude"),
    # The wrapper execs Claude Code, so Herdr observes the final `claude` process.
    "claudex": ("claude", "claude"),
    "codex": ("codex", "codex"),
    "cursor": ("cursor", "cursor-agent"),
    "pi": ("pi", "pi"),
}


def completion_style(harness: str) -> str:
    style = COMPLETION_STYLES.get(harness)
    if style is None:
        raise OfferingError(f"harness {harness!r} has no declared completion style")
    return style


class OfferingError(ValueError):
    """An offering roster, selection, or snapshot is invalid."""


@dataclass(frozen=True)
class Offering:
    offering_id: str
    harness: str
    model: str
    efforts: tuple[str, ...]
    provider: str

    def snapshot(self, effort: str) -> dict[str, object]:
        if effort not in self.efforts:
            raise OfferingError(
                f"offering {self.offering_id!r} does not allow effort {effort!r}; "
                f"expected one of {', '.join(self.efforts)}"
            )
        return {
            "id": self.offering_id,
            "harness": self.harness,
            "model": self.model,
            "provider": self.provider,
            "efforts": list(self.efforts),
            "selected_effort": effort,
        }


@dataclass(frozen=True)
class OfferingRoster:
    offerings: dict[str, Offering]
    management: dict[str, object]
    source: Path
    selected_sets: tuple[str, ...]

    def resolve(self, offering_id: str, effort: str | None) -> dict[str, object]:
        offering = self.offerings.get(offering_id)
        if offering is None:
            raise OfferingError(
                f"unknown offering {offering_id!r}; expected one of "
                + ", ".join(self.offerings)
            )
        if effort is None:
            if offering.efforts == (DEFAULT_EFFORT,):
                effort = DEFAULT_EFFORT
            else:
                raise OfferingError(
                    f"offering {offering_id!r} requires an explicit effort; "
                    f"expected one of {', '.join(offering.efforts)}"
                )
        return offering.snapshot(effort)

    def listing(self) -> list[dict[str, object]]:
        return [
            {
                "id": item.offering_id,
                "harness": item.harness,
                "model": item.model,
                "provider": item.provider,
                "efforts": list(item.efforts),
                "rendered_identity": f"{item.harness}:::{item.model}",
            }
            for item in self.offerings.values()
        ]


def _strict_keys(value: dict[str, Any], allowed: set[str], where: str) -> None:
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise OfferingError(f"{where} has unknown keys: {', '.join(unknown)}")


def normalize_offering_sets(value: object) -> tuple[str, ...]:
    """Validate one machine/destination selection and return code-owned order."""
    if not isinstance(value, (list, tuple)) or not value:
        raise OfferingError("upagent.offering_sets must be a non-empty list")
    if not all(isinstance(item, str) and item for item in value):
        raise OfferingError("upagent.offering_sets must contain non-empty strings")
    if len(set(value)) != len(value):
        raise OfferingError("upagent.offering_sets must not contain duplicates")
    unknown = sorted(set(value) - set(APPROVED_SETS))
    if unknown:
        raise OfferingError(
            "unknown UpAgent offering set(s): "
            + ", ".join(unknown)
            + "; expected one or more of "
            + ", ".join(APPROVED_SETS)
        )
    if "standard" not in value:
        raise OfferingError("upagent.offering_sets must include standard")
    return tuple(name for name in APPROVED_SETS if name in value)


def _selected_sets_for_ids(ids: set[str]) -> tuple[str, ...]:
    unknown = sorted(ids - set(APPROVED))
    if unknown:
        raise OfferingError("offering roster has unknown ids: " + ", ".join(unknown))
    selected: list[str] = []
    covered: set[str] = set()
    for name, approved_ids in APPROVED_SETS.items():
        approved = set(approved_ids)
        present = ids & approved
        if present and present != approved:
            missing = sorted(approved - present)
            raise OfferingError(
                f"offering roster contains only part of approved set {name!r}; "
                f"missing {', '.join(missing)}"
            )
        if present:
            selected.append(name)
            covered.update(approved)
    if not selected or covered != ids:
        raise OfferingError("offering roster does not equal an approved set union")
    return tuple(selected)


def _parse_roster(raw: object, source: Path) -> OfferingRoster:
    if not isinstance(raw, dict):
        raise OfferingError(f"offering roster {source} must be one YAML object")
    _strict_keys(raw, {"schema_version", "offerings", "management"}, "offering roster")
    if raw.get("schema_version") != 1:
        raise OfferingError("offering roster schema_version must equal 1")
    values = raw.get("offerings")
    if not isinstance(values, dict):
        raise OfferingError("offering roster must define an offerings object")
    selected_sets = _selected_sets_for_ids(set(values))
    expected_order = [
        offering_id
        for set_name in selected_sets
        for offering_id in APPROVED_SETS[set_name]
    ]
    parsed: dict[str, Offering] = {}
    for offering_id in expected_order:
        expected = APPROVED[offering_id]
        value = values[offering_id]
        if _ID_RE.fullmatch(offering_id) is None or not isinstance(value, dict):
            raise OfferingError(
                f"offering {offering_id!r} must be a shell-safe id mapped to an object"
            )
        _strict_keys(
            value,
            {"harness", "model", "efforts", "completion_style"},
            f"offering {offering_id}",
        )
        harness, model, efforts, provider = expected
        if value.get("harness") != harness or value.get("model") != model:
            raise OfferingError(
                f"offering {offering_id!r} must resolve to {harness}:::{model}"
            )
        declared_style = value.get("completion_style")
        if declared_style is not None and declared_style != completion_style(harness):
            raise OfferingError(
                f"offering {offering_id!r} declares completion_style "
                f"{declared_style!r} but harness {harness!r} is "
                f"{completion_style(harness)!r}"
            )
        raw_efforts = value.get("efforts")
        if not isinstance(raw_efforts, list) or tuple(raw_efforts) != efforts:
            raise OfferingError(
                f"offering {offering_id!r} efforts must be exactly {list(efforts)!r}"
            )
        if len(set(raw_efforts)) != len(raw_efforts) or not all(
            isinstance(item, str) and (item in EFFORTS or item == DEFAULT_EFFORT)
            for item in raw_efforts
        ):
            raise OfferingError(f"offering {offering_id!r} has invalid efforts")
        parsed[offering_id] = Offering(offering_id, harness, model, efforts, provider)
    management = raw.get("management", {})
    if not isinstance(management, dict):
        raise OfferingError("offering roster management must be an object")
    _validate_management(management, parsed)
    return OfferingRoster(parsed, dict(management), source, selected_sets)


def load_roster(path: str | Path) -> OfferingRoster:
    source = Path(path).resolve()
    try:
        raw = _load_yaml(source.read_text())
    except (OSError, yaml.YAMLError) as error:
        raise OfferingError(
            f"offering roster {source} is unreadable or invalid YAML: {error}"
        ) from error
    return _parse_roster(raw, source)


def _worktree_records(porcelain: str) -> list[dict[str, str]]:
    records: list[dict[str, str]] = []
    current: dict[str, str] = {}
    for line in porcelain.splitlines():
        if not line:
            if current:
                records.append(current)
                current = {}
            continue
        key, _, value = line.partition(" ")
        current[key] = value
    if current:
        records.append(current)
    return records


def _git_stdout(current: Path, *args: str) -> str | None:
    try:
        probe = subprocess.run(
            ["git", "-C", str(current), *args],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if probe.returncode != 0:
        return None
    output = probe.stdout.strip()
    return output or None


def _git_common_dir(current: Path) -> Path | None:
    output = _git_stdout(
        current, "rev-parse", "--path-format=absolute", "--git-common-dir"
    )
    return Path(output).resolve() if output is not None else None


def _git_checkout_root(current: Path) -> Path | None:
    output = _git_stdout(
        current, "rev-parse", "--path-format=absolute", "--show-toplevel"
    )
    return Path(output).resolve() if output is not None else None


def _roster_in_explicit_canonical_repo(current: Path) -> Path | None:
    current_common = _git_common_dir(current)
    if current_common is None:
        return None
    value = os.environ.get(CANONICAL_REPO_ENV)
    if not value:
        return None
    root = Path(value).expanduser()
    if not root.is_absolute():
        raise OfferingError(f"{CANONICAL_REPO_ENV} must be absolute")
    root = root.resolve()
    checkout_root = _git_checkout_root(root)
    if checkout_root is None:
        raise OfferingError(f"{CANONICAL_REPO_ENV} must be a git checkout root: {root}")
    if checkout_root != root:
        raise OfferingError(
            f"{CANONICAL_REPO_ENV} must be a git checkout root, got {root}"
        )
    canonical_common = _git_common_dir(root)
    if canonical_common != current_common:
        return None
    candidate = root / ROSTER_RELATIVE_PATH
    if candidate.is_file():
        return candidate
    raise OfferingError(
        f"{CANONICAL_REPO_ENV} roster not found: {candidate}; run `just update`"
    )


def _roster_in_main_worktree(current: Path) -> Path | None:
    output = _git_stdout(current, "worktree", "list", "--porcelain")
    if output is None:
        return None
    for record in _worktree_records(output):
        if "bare" in record or record.get("branch") != MAIN_BRANCH_REF:
            continue
        worktree = record.get("worktree")
        if not worktree:
            continue
        candidate = Path(worktree).resolve() / ROSTER_RELATIVE_PATH
        if candidate.is_file():
            return candidate
    return None


def resolve_roster_path(cwd: Path, home: Path | None = None) -> Path:
    """Resolve current repo, explicit/main checkout, then machine home roster."""
    current = cwd.resolve()
    for parent in (current, *current.parents):
        candidate = parent / ROSTER_RELATIVE_PATH
        if candidate.is_file():
            return candidate
    canonical_roster = _roster_in_explicit_canonical_repo(current)
    if canonical_roster is not None:
        return canonical_roster
    main_roster = _roster_in_main_worktree(current)
    if main_roster is not None:
        return main_roster
    machine_home = home or Path(os.environ.get("HOME", str(Path.home())))
    candidate = (
        machine_home / ".shared-llm/generated/extensions/common/upagent/offerings.yaml"
    )
    if candidate.is_file():
        return candidate
    # Source-tree tests materialize the same generated sibling that a deployed
    # canonical main-checkout module has. It is checked only after the documented
    # repository/worktree/home chain, so it never shadows machine policy.
    canonical_sibling = Path(__file__).resolve().with_name("offerings.yaml")
    if canonical_sibling.is_file():
        return canonical_sibling
    raise OfferingError(
        "no generated UpAgent offering roster found in the current repository, "
        "its main checkout, or the home runtime; run `just update`"
    )


def _read_set(path: Path, expected_ids: tuple[str, ...]) -> tuple[str, dict[str, Any]]:
    try:
        text = path.read_text()
        raw = _load_yaml(text)
    except (OSError, yaml.YAMLError) as error:
        raise OfferingError(
            f"offering set {path} is unreadable or invalid YAML: {error}"
        ) from error
    if not isinstance(raw, dict):
        raise OfferingError(f"offering set {path} must be one YAML object")
    _strict_keys(raw, {"schema_version", "offerings"}, f"offering set {path.name}")
    if raw.get("schema_version") != 1 or not isinstance(raw.get("offerings"), dict):
        raise OfferingError(f"offering set {path.name} has invalid schema")
    values = cast(dict[str, Any], raw["offerings"])
    if list(values) != list(expected_ids):
        raise OfferingError(
            f"offering set {path.name} must contain exactly: {', '.join(expected_ids)}"
        )
    return text, values


def render_roster(
    selected_sets: object,
    source_dir: Path | None = None,
) -> str:
    """Render an exact, deterministic union of approved offering fragments."""
    selected = normalize_offering_sets(selected_sets)
    root = source_dir or Path(__file__).resolve().parent
    merged: dict[str, Any] = {}
    texts: dict[str, str] = {}
    for name in selected:
        text, values = _read_set(
            root / "offerings.d" / f"{name}.yaml", APPROVED_SETS[name]
        )
        duplicate_ids = set(merged) & set(values)
        if duplicate_ids:
            raise OfferingError(
                f"offering set {name!r} duplicates ids: {', '.join(sorted(duplicate_ids))}"
            )
        merged.update(values)
        texts[name] = text
    management_path = root / "offerings-management.yaml"
    try:
        management_text = management_path.read_text()
        management_raw = _load_yaml(management_text)
    except (OSError, yaml.YAMLError) as error:
        raise OfferingError(
            f"offering management policy {management_path} is unreadable or invalid YAML: {error}"
        ) from error
    if not isinstance(management_raw, dict):
        raise OfferingError("offering management policy must be one YAML object")
    _strict_keys(management_raw, {"management"}, "offering management policy")

    # Standard is required by the fixed management candidates. Keeping its authored text as
    # the base preserves the pre-offering-set standard roster byte-for-byte.
    if "standard" not in texts:
        raise OfferingError(
            "selected offering sets must include standard management candidates"
        )
    body = texts["standard"].rstrip()
    for name in selected:
        if name == "standard":
            continue
        marker = "offerings:\n"
        if marker not in texts[name]:
            raise OfferingError(f"offering set {name!r} has no offerings mapping")
        body += "\n" + texts[name].split(marker, 1)[1].rstrip()
    rendered = body + "\n\n" + management_text.lstrip("\n")
    parsed = _load_yaml(rendered)
    roster = _parse_roster(parsed, Path("<rendered-offering-roster>"))
    if roster.selected_sets != selected or list(roster.offerings) != list(merged):
        raise OfferingError(
            "rendered offering roster does not equal the selected set union"
        )
    return rendered


def load_selected_roster(
    selected_sets: object = DEFAULT_SETS,
    source_dir: Path | None = None,
) -> OfferingRoster:
    """Assemble and parse approved source fragments without writing an output."""
    rendered = render_roster(selected_sets, source_dir)
    return _parse_roster(_load_yaml(rendered), Path("<approved-offering-sets>"))


def _validate_management(
    management: dict[str, Any], offerings: dict[str, Offering]
) -> None:
    allowed = {
        "mode",
        "rescue_on_startup_failure",
        "startup_timeout_ms",
        "inactivity_check_ms",
        "requester_grace_ms",
        "account_manager",
        "checker",
        "sentinel",
    }
    _strict_keys(management, allowed, "management")
    for role_name in ("account_manager", "checker", "sentinel"):
        role = management.get(role_name)
        if not isinstance(role, dict):
            raise OfferingError(f"management.{role_name} must be an object")
        _strict_keys(
            role,
            {"candidates", "agent", "timeout_ms"},
            f"management.{role_name}",
        )
        agent = role.get("agent")
        if not isinstance(agent, str) or not agent:
            raise OfferingError(
                f"management.{role_name}.agent must be a non-empty string"
            )
        candidates = role.get("candidates")
        if not isinstance(candidates, list) or not candidates:
            raise OfferingError(
                f"management.{role_name}.candidates must be a non-empty list"
            )
        seen: set[tuple[str, str]] = set()
        for index, candidate in enumerate(candidates):
            where = f"management.{role_name}.candidates[{index}]"
            if not isinstance(candidate, dict):
                raise OfferingError(f"{where} must be an object")
            _strict_keys(candidate, {"offering", "effort"}, where)
            offering_id = candidate.get("offering")
            effort = candidate.get("effort")
            if not isinstance(offering_id, str) or not offering_id:
                raise OfferingError(f"{where}.offering must be a non-empty string")
            if not isinstance(effort, str) or not effort:
                raise OfferingError(f"{where}.effort must be a non-empty string")
            offering = offerings.get(offering_id)
            if offering is None:
                raise OfferingError(
                    f"{where} references unknown offering {offering_id!r}"
                )
            if effort not in offering.efforts:
                raise OfferingError(
                    f"{where} effort {effort!r} is not allowed by {offering_id!r}"
                )
            identity = (offering_id, effort)
            if identity in seen:
                raise OfferingError(
                    f"{where} duplicates candidate {offering_id!r}/{effort}"
                )
            seen.add(identity)


def validate_snapshot(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise OfferingError("offering snapshot must be an object")
    snapshot_keys = {
        "id",
        "harness",
        "model",
        "provider",
        "efforts",
        "selected_effort",
    }
    _strict_keys(value, snapshot_keys, "offering snapshot")
    missing = sorted(snapshot_keys - set(value))
    if missing:
        raise OfferingError(f"offering snapshot is missing keys: {', '.join(missing)}")
    offering_id = value.get("id")
    if not isinstance(offering_id, str) or offering_id not in APPROVED:
        raise OfferingError(f"offering snapshot has unknown id {offering_id!r}")
    harness, model, efforts, provider = APPROVED[offering_id]
    if (
        value.get("harness") != harness
        or value.get("model") != model
        or value.get("provider") != provider
        or value.get("efforts") != list(efforts)
    ):
        raise OfferingError(
            f"offering snapshot for {offering_id!r} does not match approved policy"
        )
    effort = value.get("selected_effort")
    if effort not in efforts:
        raise OfferingError(
            f"offering snapshot for {offering_id!r} has unsupported effort {effort!r}"
        )
    return dict(value)


def render_argv(snapshot: object, persona: str, instructions_path: str) -> list[str]:
    selected = validate_snapshot(snapshot)
    harness = str(selected["harness"])
    model = str(selected["model"])
    effort = str(selected["selected_effort"])
    prompt = f"Read {instructions_path} and do exactly that work."
    if harness == "claude":
        return [
            "claude",
            "--dangerously-skip-permissions",
            "--agent",
            persona,
            "--model",
            model,
            "--effort",
            effort,
            prompt,
        ]
    if harness == "claudex":
        return [
            "claudex",
            model,
            "--dangerously-skip-permissions",
            "--agent",
            persona,
            "--effort",
            effort,
            prompt,
        ]
    if harness == "codex":
        return [
            "codex",
            "exec",
            "--dangerously-bypass-approvals-and-sandbox",
            "--skip-git-repo-check",
            "--model",
            model,
            "-c",
            f"model_reasoning_effort={effort}",
            prompt,
        ]
    if harness == "pi":
        return [
            "pi",
            "--approve",
            "--no-extensions",
            "-e",
            str(Path.home() / ".pi/agent/extensions/herdr-agent-state.ts"),
            "--model",
            model,
            "--thinking",
            effort,
            prompt,
        ]
    if harness == "cursor":
        # Keep Cursor's interactive TUI so Herdr receives live progress. --force
        # approves commands and --trust bypasses the workspace prompt for an
        # unattended launch. Cursor exposes no effort or persona flag; both the
        # persona and delivery contract travel in the instructions. Repeat the
        # delivery gate at the launch boundary because a missing bundle is the
        # one failure an otherwise successful Cursor turn cannot self-report.
        cursor_prompt = (
            f"Read {instructions_path} and do exactly that work. Before returning idle, "
            "verify every artifact named in the final Recruiter delivery contract exists "
            "and satisfies that contract."
        )
        return [
            "cursor-agent",
            "--force",
            "--trust",
            "--model",
            model,
            cursor_prompt,
        ]
    raise OfferingError(f"offering snapshot has unsupported harness {harness!r}")


def preflight_snapshot(snapshot: object) -> dict[str, object]:
    """Run the code-owned ClaudeX doctor before any worker pane is created."""
    selected = validate_snapshot(snapshot)
    if selected["harness"] != "claudex":
        return {"required": False, "validated": True}
    model = str(selected["model"])
    if shutil.which("claudex") is None:
        raise OfferingError("ClaudeX preflight failed: `claudex` is not on PATH")
    doctor = shutil.which("claudex-doctor")
    if doctor is None:
        raise OfferingError("ClaudeX preflight failed: `claudex-doctor` is not on PATH")
    try:
        result = subprocess.run(
            [doctor, model], capture_output=True, text=True, timeout=20
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise OfferingError(
            f"ClaudeX preflight for model {model!r} could not run: {error}"
        ) from error
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip() or "doctor returned no detail"
        raise OfferingError(
            f"ClaudeX preflight for required model {model!r} failed: {detail}"
        )
    return {
        "required": True,
        "validated": True,
        "doctor": doctor,
        "model": model,
    }


def render_shell(snapshot: object, persona: str, instructions_path: str) -> str:
    return shlex.join(render_argv(snapshot, persona, instructions_path))


def _materialize_candidate_role(
    roster: OfferingRoster, raw_value: dict[str, object], prompt: str
) -> dict[str, object]:
    """Render an ordered public candidate list without trusting YAML for commands."""
    raw = dict(raw_value)
    candidate_values = cast(list[dict[str, str]], raw.pop("candidates"))
    agent = str(raw.pop("agent"))
    candidates: list[dict[str, object]] = []
    for value in candidate_values:
        snapshot = roster.resolve(value["offering"], value["effort"])
        harness = str(snapshot["harness"])
        expected_agent, expected_process = MANAGEMENT_HEALTH[harness]
        argv = render_argv(snapshot, agent, "{brief_path}")
        argv[-1] = prompt
        candidates.append(
            {
                **raw,
                "command": shlex.join(argv),
                "expected_agent": expected_agent,
                "expected_process": expected_process,
                "offering_id": snapshot["id"],
                "provider": snapshot["provider"],
            }
        )
    # Keep the historical singular role shape available to lifecycle code and external
    # readers. Runtime candidate selection uses `candidates`; this mirror is never read
    # from public YAML and always equals the first code-rendered candidate.
    return {**candidates[0], "candidates": candidates}


def materialize_management(roster: OfferingRoster) -> dict[str, object]:
    """Translate validated offering references into code-owned lifecycle commands."""
    management = dict(roster.management)
    management["account_manager"] = _materialize_candidate_role(
        roster,
        cast(dict[str, object], management["account_manager"]),
        "Read {brief_path}, perform that one lifecycle review, write {output_path}, then remain available.",
    )

    management["checker"] = _materialize_candidate_role(
        roster,
        cast(dict[str, object], management["checker"]),
        "Read {brief_path}, perform that one bounded assessment, write {output_path}, then exit.",
    )

    management["sentinel"] = _materialize_candidate_role(
        roster,
        cast(dict[str, object], management["sentinel"]),
        "Read {brief_path}, perform that one sentinel duty cycle for its worker, write {output_path} when the worker lifecycle ends, then exit.",
    )
    return management
