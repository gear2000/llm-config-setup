#!/usr/bin/env python3
"""Validated public offering roster and code-owned harness renderers."""

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

import yaml

EFFORTS = ("low", "medium", "high", "xhigh", "max")
# Canonical selection for offerings whose harness exposes no effort control.
# Omitted and explicit "default" requests normalize to the same snapshot/hash.
DEFAULT_EFFORT = "default"
_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")

APPROVED: dict[str, tuple[str, str, tuple[str, ...], str]] = {
    "claude-fable-5": ("claude", "claude-fable-5", EFFORTS, "anthropic"),
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
    "cursor-fable-5": (
        "cursor",
        "claude-fable-5-high",
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
}


# Harness completion semantics. "exec" harnesses run one non-interactive
# process that exits when its turn is done: the Herdr agent disappears, so no
# follow-up repair prompt is ever possible. "interactive" harnesses keep an
# addressable agent that can accept exactly one same-worker repair prompt.
COMPLETION_STYLES: dict[str, str] = {
    "claude": "interactive",
    "codex": "exec",
    "cursor": "interactive",
    "pi": "interactive",
}

# Herdr health identity is code-owned for the same reason command rendering is:
# public YAML selects offerings, but cannot select an executable or process identity.
MANAGEMENT_HEALTH: dict[str, tuple[str, str]] = {
    "claude": ("claude", "claude"),
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


def load_roster(path: str | Path | None = None) -> OfferingRoster:
    source = Path(path or Path(__file__).with_name("offerings.yaml")).resolve()
    try:
        raw = yaml.safe_load(source.read_text())
    except (OSError, yaml.YAMLError) as error:
        raise OfferingError(
            f"offering roster {source} is unreadable or invalid YAML: {error}"
        ) from error
    if not isinstance(raw, dict):
        raise OfferingError(f"offering roster {source} must be one YAML object")
    _strict_keys(raw, {"schema_version", "offerings", "management"}, "offering roster")
    if raw.get("schema_version") != 1:
        raise OfferingError("offering roster schema_version must equal 1")
    values = raw.get("offerings")
    if not isinstance(values, dict):
        raise OfferingError("offering roster must define an offerings object")
    if set(values) != set(APPROVED):
        missing = sorted(set(APPROVED) - set(values))
        extra = sorted(set(values) - set(APPROVED))
        detail = []
        if missing:
            detail.append("missing " + ", ".join(missing))
        if extra:
            detail.append("unknown " + ", ".join(extra))
        raise OfferingError(
            f"offering roster must contain exactly the {len(APPROVED)} approved ids: "
            + "; ".join(detail)
        )
    parsed: dict[str, Offering] = {}
    for offering_id, expected in APPROVED.items():
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
    return OfferingRoster(parsed, dict(management), source)


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
    for role_name in ("account_manager", "sentinel"):
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

    role = management.get("checker")
    if not isinstance(role, dict):
        raise OfferingError("management.checker must be an object")
    _strict_keys(
        role,
        {
            "offering",
            "effort",
            "agent",
            "expected_agent",
            "expected_process",
            "timeout_ms",
        },
        "management.checker",
    )
    for field in ("offering", "effort", "agent", "expected_agent", "expected_process"):
        if not isinstance(role.get(field), str) or not role[field]:
            raise OfferingError(
                f"management.checker.{field} must be a non-empty string"
            )
    offering = offerings.get(role["offering"])
    if offering is None:
        raise OfferingError(
            f"management.checker references unknown offering {role['offering']!r}"
        )
    if role["effort"] not in offering.efforts:
        raise OfferingError(
            f"management.checker effort {role['effort']!r} is not allowed by {role['offering']!r}"
        )


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

    raw = dict(cast(dict[str, object], management["checker"]))
    snapshot = roster.resolve(str(raw.pop("offering")), str(raw.pop("effort")))
    agent = str(raw.pop("agent"))
    argv = render_argv(snapshot, agent, "{brief_path}")
    argv[-1] = (
        "Read {brief_path}, perform that one bounded assessment, write {output_path}, then exit."
    )
    raw["command"] = shlex.join(argv)
    management["checker"] = raw

    management["sentinel"] = _materialize_candidate_role(
        roster,
        cast(dict[str, object], management["sentinel"]),
        "Read {brief_path}, perform that one sentinel duty cycle for its worker, write {output_path} when the worker lifecycle ends, then exit.",
    )
    return management
