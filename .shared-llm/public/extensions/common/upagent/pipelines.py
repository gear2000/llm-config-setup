#!/usr/bin/env python3
"""Validated pipeline registry and the Herdr launch for one pipeline TUI.

Two jobs, one module:

    load_registry()   which named pipelines exist, and what shape each one has
    launch()          put one interactive `claude "/upagent-pipeline <name> ..."` in a Herdr pane

The registry is the same validated-roster shape as `offerings.yaml`: stable ids, strict keys, no
silent defaults, and every failure names the file and the field. Nothing here interprets a
pipeline — the stages, the optional stages, and the review gate are read by the `/upagent-pipeline`
skill inside the launched pane. This module only guarantees that what the pane is asked to run
is a pipeline the registry actually defines.

`PipelineError` is a RuntimeError, not a ValueError, so `client.py` prints one legible line
instead of a traceback when a registry or a launch fails.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import re
import shlex
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

import yaml

HERE = Path(__file__).resolve().parent

_runtime_name = "upagent_command_runtime"
if _runtime_name in sys.modules:
    command_runtime = sys.modules[_runtime_name]
else:
    _runtime_spec = importlib.util.spec_from_file_location(
        _runtime_name, HERE / "command_runtime.py"
    )
    if _runtime_spec is None or _runtime_spec.loader is None:
        raise RuntimeError("could not load UpAgent command runtime")
    command_runtime = importlib.util.module_from_spec(_runtime_spec)
    sys.modules[_runtime_name] = command_runtime
    _runtime_spec.loader.exec_module(command_runtime)

_control_spec = importlib.util.spec_from_file_location(
    "upagent_pipelines_herdr_transport", HERE.parent / "herdr" / "herdr_transport.py"
)
if _control_spec is None or _control_spec.loader is None:
    raise RuntimeError("could not load canonical Herdr transport")
control = cast(Any, importlib.util.module_from_spec(_control_spec))
_control_spec.loader.exec_module(control)

REGISTRY_FILE = "pipelines.yaml"
SCHEMA_VERSION = 1
NO_REVIEW_GATE = "none"
_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")

# Code-owned vocabulary. A stage id is not free text: the `/upagent-pipeline` skill has one
# section per stage, so `reserach` is not a new stage, it is a stage that will never run. Shape
# alone cannot catch that — only a closed set can.
SUPPORTED_STAGES = ("research", "plan", "implement", "validate")
# Pipeline ids are code-owned for the same reason. The skill dispatches its per-pipeline prose by
# id and has one route section per id, so `rpii` is not a new pipeline — it is a registry entry
# that lists, launches a pane, and lands in a skill with nothing to run. Adding a pipeline is a
# code change: an entry in pipelines.yaml, a `## Pipeline: <id>` route section in the skill, and
# an id here.
SUPPORTED_PIPELINES = ("rpi", "no-mistakes")
# Where human approval lands when the review_gate stage is skipped. Never gateless: skipping the
# stage moves the gate, it does not remove it.
SUPPORTED_SKIP_GATES = ("issue-approval",)

SLASH_COMMAND = "/upagent-pipeline"
CLAUDE_LAUNCH = "claude --dangerously-skip-permissions"
EXPECTED_AGENT = "claude"
EXPECTED_PROCESS = "claude"
CONTROL_TAB_LABEL = "control"
STARTUP_TIMEOUT_MS = 45_000
HERDR_START_HINT = (
    "start Herdr first — run `herdr` (or `herdr --session <name>`) and leave that "
    "session running, then re-run this command"
)


class PipelineError(RuntimeError):
    """A pipeline registry, selection, or launch fault."""


# --- the registry -------------------------------------------------------------


@dataclass(frozen=True)
class Pipeline:
    pipeline_id: str
    description: str
    stages: tuple[str, ...]
    optional_stages: tuple[str, ...]
    review_gate: str
    skip_gate: str | None
    max_phases: int | None

    def required_stages(self) -> tuple[str, ...]:
        return tuple(
            stage for stage in self.stages if stage not in self.optional_stages
        )

    def listing(self) -> dict[str, object]:
        return {
            "id": self.pipeline_id,
            "description": self.description,
            "stages": list(self.stages),
            "optional_stages": list(self.optional_stages),
            "required_stages": list(self.required_stages()),
            "review_gate": self.review_gate,
            "skip_gate": self.skip_gate,
            "max_phases": self.max_phases,
        }


@dataclass(frozen=True)
class PipelineRegistry:
    pipelines: dict[str, Pipeline]
    source: Path

    def resolve(self, pipeline_id: str) -> Pipeline:
        pipeline = self.pipelines.get(pipeline_id)
        if pipeline is None:
            raise PipelineError(
                f"unknown pipeline {pipeline_id!r}; expected one of "
                + ", ".join(self.pipelines)
                + f" (registry: {self.source})"
            )
        return pipeline

    def listing(self) -> list[dict[str, object]]:
        return [pipeline.listing() for pipeline in self.pipelines.values()]

    def index(self) -> dict[str, dict[str, object]]:
        return {
            pipeline_id: pipeline.listing()
            for pipeline_id, pipeline in self.pipelines.items()
        }


def _strict_keys(value: dict[str, Any], allowed: set[str], where: str) -> None:
    unknown = sorted(str(key) for key in set(value) - allowed)
    if unknown:
        raise PipelineError(f"{where} has unknown keys: {', '.join(unknown)}")


def _stage_ids(value: object, where: str) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise PipelineError(f"{where} must be a list of stage ids")
    stages: list[str] = []
    for item in value:
        if not isinstance(item, str) or _ID_RE.fullmatch(item) is None:
            raise PipelineError(f"{where} has an invalid stage id: {item!r}")
        if item not in SUPPORTED_STAGES:
            raise PipelineError(
                f"{where} has unsupported stage {item!r}; expected one of "
                + ", ".join(SUPPORTED_STAGES)
            )
        if item in stages:
            raise PipelineError(f"{where} repeats stage {item!r}")
        stages.append(item)
    return tuple(stages)


def _parse_pipeline(pipeline_id: object, value: object) -> Pipeline:
    if not isinstance(pipeline_id, str) or _ID_RE.fullmatch(pipeline_id) is None:
        raise PipelineError(
            f"pipeline id {pipeline_id!r} must be a shell-safe lowercase id "
            "(letters, digits, dashes)"
        )
    if pipeline_id not in SUPPORTED_PIPELINES:
        raise PipelineError(
            f"registry has unsupported pipeline {pipeline_id!r}; expected one of "
            + ", ".join(SUPPORTED_PIPELINES)
            + "; adding a pipeline is a code change (registry entry + skill route "
            "section + SUPPORTED_PIPELINES entry)"
        )
    where = f"pipeline {pipeline_id}"
    if not isinstance(value, dict):
        raise PipelineError(f"{where} must be an object")
    _strict_keys(
        value,
        {
            "description",
            "stages",
            "optional_stages",
            "review_gate",
            "skip_gate",
            "max_phases",
        },
        where,
    )
    description = value.get("description")
    if not isinstance(description, str) or not description.strip():
        raise PipelineError(f"{where} needs a non-empty description")
    stages = _stage_ids(value.get("stages"), f"{where} stages")
    if not stages:
        raise PipelineError(f"{where} needs at least one stage")
    optional = _stage_ids(value.get("optional_stages", []), f"{where} optional_stages")
    outside = [stage for stage in optional if stage not in stages]
    if outside:
        raise PipelineError(
            f"{where} optional_stages must be stages of the pipeline; "
            f"{', '.join(outside)} not in {', '.join(stages)}"
        )
    review_gate = value.get("review_gate")
    if not isinstance(review_gate, str) or (
        review_gate != NO_REVIEW_GATE and review_gate not in stages
    ):
        raise PipelineError(
            f"{where} review_gate must be {NO_REVIEW_GATE!r} or one of its stages "
            f"({', '.join(stages)}); got {review_gate!r}"
        )
    # A skippable review gate has to say where approval goes instead. A pipeline whose gate
    # stage can be skipped and that names no fallback is not a pipeline with a smaller gate,
    # it is a pipeline that runs ungated whenever the human passes one flag.
    skip_gate = _skip_gate(value, where, review_gate, optional)
    # Absent means unbounded. An explicit `max_phases:` with no value is a half-written
    # bound, so it fails here rather than reading as unbounded.
    max_phases: int | None = None
    if "max_phases" in value:
        candidate = value["max_phases"]
        if (
            not isinstance(candidate, int)
            or isinstance(candidate, bool)
            or candidate < 1
        ):
            raise PipelineError(
                f"{where} max_phases must be a positive integer when present; "
                f"got {candidate!r}"
            )
        max_phases = candidate
    return Pipeline(
        pipeline_id,
        description.strip(),
        stages,
        optional,
        review_gate,
        skip_gate,
        max_phases,
    )


def _skip_gate(
    value: dict[str, Any],
    where: str,
    review_gate: str,
    optional_stages: tuple[str, ...],
) -> str | None:
    """The fallback gate, required exactly when the review_gate stage is skippable.

    Required, because skipping the gate stage must move human approval rather than remove it.
    Forbidden otherwise, because a `skip_gate` on a pipeline whose gate can never be skipped
    reads as a gate that exists and is one nobody will ever reach.
    """
    skippable = review_gate != NO_REVIEW_GATE and review_gate in optional_stages
    if not skippable:
        if "skip_gate" in value:
            raise PipelineError(
                f"{where} sets skip_gate but its review_gate {review_gate!r} is not an "
                "optional stage, so the fallback gate can never apply"
            )
        return None
    skip_gate = value.get("skip_gate")
    if skip_gate not in SUPPORTED_SKIP_GATES:
        raise PipelineError(
            f"{where} lists its review_gate {review_gate!r} in optional_stages, so it "
            "must declare where approval goes when that stage is skipped: skip_gate must "
            "be one of " + ", ".join(SUPPORTED_SKIP_GATES) + f"; got {skip_gate!r}"
        )
    return skip_gate


_MERGE_TAG = "tag:yaml.org,2002:merge"


class _NoDuplicateKeyLoader(yaml.SafeLoader):
    """SafeLoader that refuses a duplicate mapping key instead of keeping the last one.

    PyYAML's default is last-one-wins, silently. Every check in this module then runs
    against a registry nobody wrote: a block with `review_gate: plan` and, further down,
    `review_gate: none` validates cleanly as `none` and drops the human gate. Two keys
    with one name are never a legible intent, so the duplicate is the fault — at every
    level, so a second `pipelines:` block and a repeated pipeline id fail the same way.
    """

    def __init__(self, stream: Any, source: Path) -> None:
        super().__init__(stream)
        self.source = source

    def construct_mapping(
        self, node: yaml.MappingNode, deep: bool = False
    ) -> dict[Any, Any]:
        # What is rejected is one *syntactic* level naming an ordinary key twice.
        # A `<<:` merge key is not one of those: it is an instruction to pull in
        # another mapping, `super()` flattens it below, and YAML lets a block carry
        # more than one, so repeated `<<:` is valid and passes. A key that only
        # collides once merged is likewise not a duplicate — the explicit key wins
        # per the merge spec, which is what a shared anchor is written for.
        seen: list[Any] = []
        for key_node, _ in node.value:
            if key_node.tag == _MERGE_TAG:
                continue
            # Keys are compared by equality in a list, never hashed: an unhashable key is
            # PyYAML's own error to report, below, with the line and column it knows.
            key = self.construct_object(key_node, deep=True)
            if key in seen:
                raise PipelineError(
                    f"pipeline registry {self.source} sets {key!r} twice in one block "
                    f"(line {key_node.start_mark.line + 1}); the later value would "
                    "silently replace the earlier one"
                )
            seen.append(key)
        return cast(dict[Any, Any], super().construct_mapping(node, deep=deep))


def load_registry(path: str | Path | None = None) -> PipelineRegistry:
    """Read and validate the pipeline registry. Fail-loud on every malformed field."""
    source = Path(path or HERE / REGISTRY_FILE).resolve()
    try:
        # `yaml.load` with a SafeLoader subclass — the same safe tag set as
        # `yaml.safe_load`, plus the duplicate-key refusal, and the source path
        # so the refusal can name the file it read.
        raw = yaml.load(
            source.read_text(),
            lambda stream: _NoDuplicateKeyLoader(stream, source),
        )
    except (OSError, yaml.YAMLError) as error:
        raise PipelineError(
            f"pipeline registry {source} is unreadable or invalid YAML: {error}"
        ) from error
    if not isinstance(raw, dict):
        raise PipelineError(f"pipeline registry {source} must be one YAML object")
    _strict_keys(raw, {"schema_version", "pipelines"}, "pipeline registry")
    if raw.get("schema_version") != SCHEMA_VERSION:
        raise PipelineError(
            f"pipeline registry schema_version must equal {SCHEMA_VERSION}"
        )
    values = raw.get("pipelines")
    if not isinstance(values, dict) or not values:
        raise PipelineError(
            "pipeline registry must define a non-empty pipelines object"
        )
    parsed: dict[str, Pipeline] = {}
    for pipeline_id, value in values.items():
        pipeline = _parse_pipeline(pipeline_id, value)
        parsed[pipeline.pipeline_id] = pipeline
    return PipelineRegistry(parsed, source)


# --- listing ------------------------------------------------------------------


def _stages_label(pipeline: Pipeline) -> str:
    """`research? -> plan? -> implement`: the run order, optional stages marked."""
    return " -> ".join(
        f"{stage}?" if stage in pipeline.optional_stages else stage
        for stage in pipeline.stages
    )


def render_table(registry: PipelineRegistry) -> str:
    rows = [("PIPELINE", "STAGES", "DESCRIPTION")] + [
        (pipeline.pipeline_id, _stages_label(pipeline), pipeline.description)
        for pipeline in registry.pipelines.values()
    ]
    id_width = max(len(row[0]) for row in rows)
    stage_width = max(len(row[1]) for row in rows)
    lines = [
        f"{row[0]:<{id_width}}  {row[1]:<{stage_width}}  {row[2]}".rstrip()
        for row in rows
    ]
    lines += [
        "",
        "`?` marks an optional stage, skipped with --skip-<stage>.",
        "`just upagent-list-pipelines --json` prints the validated record, review gate included.",
        "`just upagent-pipeline <id> [args]` launches one in a Herdr pane.",
    ]
    return "\n".join(lines)


# --- the launch ---------------------------------------------------------------


def pipeline_prompt(pipeline: Pipeline, args: Sequence[str]) -> str:
    """The exact first prompt the launched TUI receives.

    Arguments are re-quoted with `shlex.join`, so an argument that contained spaces still
    reaches the skill as one argument. Control characters are refused: the prompt rides as one
    argv token into an interactive pane, and an embedded newline would submit a second line
    nobody wrote.
    """
    for arg in args:
        if not isinstance(arg, str):
            raise PipelineError(f"pipeline argument must be a string: {arg!r}")
        if any(ord(character) < 32 or ord(character) == 127 for character in arg):
            raise PipelineError(
                f"pipeline argument contains a control character: {arg!r}"
            )
    tail = shlex.join(args)
    return f"{SLASH_COMMAND} {pipeline.pipeline_id}" + (f" {tail}" if tail else "")


def _resolved_session() -> str:
    try:
        return control.resolve_current_herdr_session_name()
    except control.HerdrTransportError as error:
        raise PipelineError(
            f"cannot reach a running Herdr session: {error}; {HERDR_START_HINT}"
        ) from error


def _unified_workspace_id(herdr_session: str) -> str | None:
    workspaces = (
        control.herdr_json("workspace", "list", herdr_session=herdr_session)
        .get("result", {})
        .get("workspaces", [])
    )
    for workspace in workspaces:
        if (
            isinstance(workspace, dict)
            and workspace.get("label") == control.UNIFIED_WORKSPACE_LABEL
            and isinstance(workspace.get("workspace_id"), str)
            and workspace["workspace_id"]
        ):
            return cast(str, workspace["workspace_id"])
    return None


def _split_control_pane(workspace_id: str, herdr_session: str) -> str:
    """A fresh pane for this pipeline in the unified workspace's `control` tab."""
    panes = (
        control.herdr_json(
            "pane", "list", "--workspace", workspace_id, herdr_session=herdr_session
        )
        .get("result", {})
        .get("panes", [])
    )
    anchor = next(
        (
            pane["pane_id"]
            for pane in panes
            if isinstance(pane, dict)
            and isinstance(pane.get("pane_id"), str)
            and pane["pane_id"]
        ),
        None,
    )
    if anchor is None:
        raise PipelineError(f"Herdr workspace {workspace_id} has no pane to split from")
    split = control.herdr_json(
        "pane",
        "split",
        anchor,
        "--direction",
        "right",
        "--no-focus",
        herdr_session=herdr_session,
    )
    pane_id = split.get("result", {}).get("pane", {}).get("pane_id")
    if not isinstance(pane_id, str) or not pane_id:
        raise PipelineError("herdr pane split returned no pane_id")
    try:
        return control.place_started_agent_in_role_tab(
            pane_id,
            workspace_id,
            CONTROL_TAB_LABEL,
            split_direction="right",
            herdr_session=herdr_session,
        )
    except (OSError, control.HerdrTransportError) as error:
        # The split already created a pane. `launch` has not been told its id yet, so its own
        # compensation cannot close it and the pane would outlive the failed placement in a
        # workspace this launch does not own. Close it here, before anyone else can miss it.
        detail = (
            f"could not place pipeline pane {pane_id} in the {CONTROL_TAB_LABEL!r} "
            f"tab: {error}"
        )
        try:
            control.herdr("pane", "close", pane_id, herdr_session=herdr_session)
        except control.HerdrTransportError as cleanup:
            detail += f"; closing the split pane also failed: {cleanup}"
        raise PipelineError(detail) from error


def launch(
    name: str,
    args: Sequence[str],
    *,
    repo: Path,
    registry: PipelineRegistry | None = None,
) -> dict[str, object]:
    """Start one pipeline as an interactive Claude TUI in a Herdr pane.

    The pipeline name and every argument are validated BEFORE Herdr is touched, so a typo
    costs a message rather than an orphaned pane. The pane joins the unified `upagent`
    workspace's `control` tab when one exists and creates it otherwise, exactly like
    `just run-start`; the launch is not reported as started until the harness is healthy.
    """
    registry = registry or load_registry()
    pipeline = registry.resolve(name)
    prompt = pipeline_prompt(pipeline, args)
    repo = repo.expanduser().resolve()
    if not repo.is_dir():
        raise PipelineError(f"pipeline repo must be an existing directory: {repo}")
    command = f"cd {shlex.quote(str(repo))} && {CLAUDE_LAUNCH} " + shlex.quote(prompt)

    herdr_session = _resolved_session()
    workspace_id = _unified_workspace_id(herdr_session)
    created_workspace = workspace_id is None
    pane_id: str | None = None
    try:
        if created_workspace:
            root = (
                control.herdr_json(
                    "workspace",
                    "create",
                    "--cwd",
                    str(repo),
                    "--label",
                    control.UNIFIED_WORKSPACE_LABEL,
                    "--no-focus",
                    herdr_session=herdr_session,
                )
                .get("result", {})
                .get("root_pane", {})
            )
            pane_id = root.get("pane_id") if isinstance(root, dict) else None
            tab_id = root.get("tab_id") if isinstance(root, dict) else None
            workspace_id = root.get("workspace_id") if isinstance(root, dict) else None
            if not isinstance(workspace_id, str) or not workspace_id:
                raise PipelineError("herdr workspace create returned no workspace_id")
            if not isinstance(pane_id, str) or not pane_id:
                raise PipelineError("herdr workspace create returned no root pane_id")
            if not isinstance(tab_id, str) or not tab_id:
                raise PipelineError("herdr workspace create returned no root tab_id")
            control.herdr(
                "tab",
                "rename",
                tab_id,
                CONTROL_TAB_LABEL,
                herdr_session=herdr_session,
            )
        else:
            pane_id = _split_control_pane(cast(str, workspace_id), herdr_session)
        control.herdr(
            "pane",
            "rename",
            pane_id,
            f"pipeline-{pipeline.pipeline_id}",
            herdr_session=herdr_session,
        )
        control.herdr("pane", "run", pane_id, command, herdr_session=herdr_session)
        health = control.wait_for_agent_health(
            pane_id,
            expected_agent=EXPECTED_AGENT,
            expected_process=EXPECTED_PROCESS,
            expected_cwd=str(repo),
            timeout_ms=STARTUP_TIMEOUT_MS,
            herdr_session=herdr_session,
        )
    except (OSError, PipelineError, control.HerdrTransportError) as error:
        cleanup_error: str | None = None
        try:
            # Close the workspace only when this launch created it; an adopted unified
            # workspace hosts the shared services and possibly other runs.
            if created_workspace and isinstance(workspace_id, str) and workspace_id:
                control.herdr(
                    "workspace", "close", workspace_id, herdr_session=herdr_session
                )
            elif isinstance(pane_id, str) and pane_id:
                control.herdr("pane", "close", pane_id, herdr_session=herdr_session)
        except control.HerdrTransportError as cleanup:
            cleanup_error = str(cleanup)
        detail = f"pipeline launch failed: {error}"
        if cleanup_error is not None:
            detail += f"; launch cleanup also failed: {cleanup_error}"
        raise PipelineError(detail) from error
    return {
        "health": health,
        "herdr_session": herdr_session,
        "pane_id": pane_id,
        "pipeline": pipeline.listing(),
        "prompt": prompt,
        "repo": str(repo),
        "workspace_id": workspace_id,
        "workspace_state": "created" if created_workspace else "adopted",
    }


# --- one command --------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = command_runtime.ArgumentParser(prog="upagent-pipelines")
    sub = parser.add_subparsers(dest="command", required=True)
    listing = sub.add_parser("list", help="print the validated pipeline registry")
    listing.add_argument(
        "--json", action="store_true", help="print the raw validated index"
    )
    launching = sub.add_parser(
        "launch", help="start one pipeline as an interactive Claude TUI in a Herdr pane"
    )
    launching.add_argument("--repo", type=Path, default=command_runtime.current_cwd())
    launching.add_argument("name", help="pipeline id from `list`")
    # REMAINDER, so `--skip-research` reaches the slash command instead of being read as
    # an option of this launcher. Every launcher option therefore precedes `name`.
    launching.add_argument(
        "args",
        nargs=argparse.REMAINDER,
        help="arguments passed through verbatim to the slash command",
    )
    parsed = parser.parse_args(argv)
    try:
        registry = load_registry()
        if parsed.command == "list":
            if parsed.json:
                command_runtime.command_print(
                    json.dumps(registry.index(), indent=2, sort_keys=True), flush=True
                )
            else:
                command_runtime.command_print(render_table(registry), flush=True)
            return 0
        result = launch(parsed.name, parsed.args, repo=parsed.repo, registry=registry)
    except (OSError, PipelineError, control.HerdrTransportError) as error:
        sys.exit(f"upagent-pipelines: {error}")
    command_runtime.command_print(
        f"PIPELINE_STARTED {json.dumps(result, sort_keys=True)}", flush=True
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
