# pyright: reportMissingImports=false
"""Pipeline registry validation, listing shapes, and the Herdr pipeline launch."""

from __future__ import annotations

import copy
import importlib.util
import json
import re
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location(
    "pipelines_test_module", HERE / "pipelines.py"
)
assert spec and spec.loader
pipelines = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = pipelines
spec.loader.exec_module(pipelines)
PipelineError = pipelines.PipelineError

# .../<repo>/.shared-llm/public/extensions/common/upagent -> <repo>. The skill layer is kit-synced
# into every destination at the same path, so this resolves in the kit and in a destination alike.
SKILL_COMMAND = (
    HERE.parents[4]
    / ".shared-llm/public/layers/slash-commands/common/common/upagent-pipeline/command.md"
)
_ROUTE_SECTION_RE = re.compile(r"^## Pipeline: `([^`]+)`", re.MULTILINE)


def _shipped() -> dict[str, Any]:
    import yaml

    return yaml.safe_load((HERE / "pipelines.yaml").read_text())


def _written(tmp_path: Path, source: object) -> Path:
    import yaml

    path = tmp_path / "pipelines.yaml"
    path.write_text(yaml.safe_dump(source))
    return path


def _mutated(tmp_path: Path, mutate: Callable[[dict[str, Any]], None]) -> Path:
    source = _shipped()
    mutate(source)
    return _written(tmp_path, source)


# --- the registry -------------------------------------------------------------


def test_shipped_registry_loads_with_both_approved_pipelines() -> None:
    registry = pipelines.load_registry()

    assert list(registry.pipelines) == ["rpi", "no-mistakes"]
    rpi = registry.resolve("rpi")
    assert rpi.stages == ("research", "plan", "implement")
    assert rpi.optional_stages == ("research", "plan")
    assert rpi.required_stages() == ("implement",)
    assert rpi.review_gate == "plan"
    assert rpi.skip_gate == "issue-approval"
    assert rpi.max_phases == 3
    gate = registry.resolve("no-mistakes")
    assert gate.stages == ("implement", "validate")
    assert gate.optional_stages == ()
    assert gate.review_gate == pipelines.NO_REVIEW_GATE
    assert gate.skip_gate is None
    assert gate.max_phases is None


def test_the_shipped_registry_defines_exactly_the_code_owned_pipelines() -> None:
    # A pipeline id is not free text either: the registry may only ship ids the code owns, and
    # every id the code owns must actually be shipped. Order included — `list` prints in this one.
    registry = pipelines.load_registry()

    assert tuple(registry.pipelines) == pipelines.SUPPORTED_PIPELINES


def test_every_supported_pipeline_has_exactly_one_skill_route_section() -> None:
    # The registry can only launch what the skill can run. An id in one file and not the other is
    # a pane that starts with nothing to follow, so the two files are pinned to each other here
    # rather than to a comment asking the next author to remember.
    assert SKILL_COMMAND.is_file(), f"skill layer not found: {SKILL_COMMAND}"

    sections = _ROUTE_SECTION_RE.findall(SKILL_COMMAND.read_text())

    assert sorted(sections) == sorted(pipelines.SUPPORTED_PIPELINES)
    assert len(sections) == len(set(sections))


def test_every_skippable_review_gate_declares_where_approval_goes() -> None:
    # The registry's own invariant, checked against whatever it ships: no pipeline may be
    # gateless, so a gate stage that a flag can skip must name its fallback.
    for pipeline in pipelines.load_registry().pipelines.values():
        if (
            pipeline.review_gate != pipelines.NO_REVIEW_GATE
            and pipeline.review_gate in pipeline.optional_stages
        ):
            assert pipeline.skip_gate in pipelines.SUPPORTED_SKIP_GATES


def test_resolving_an_unknown_pipeline_lists_the_valid_ids() -> None:
    registry = pipelines.load_registry()

    with pytest.raises(PipelineError, match="unknown pipeline 'rpo'.*rpi, no-mistakes"):
        registry.resolve("rpo")


@pytest.mark.parametrize(
    ("case", "mutate", "message"),
    [
        (
            "schema_version",
            lambda source: source.__setitem__("schema_version", 2),
            "schema_version must equal 1",
        ),
        (
            "unknown-top-level-key",
            lambda source: source.__setitem__("defaults", {"stages": []}),
            "pipeline registry has unknown keys: defaults",
        ),
        (
            "no-pipelines",
            lambda source: source.__setitem__("pipelines", {}),
            "non-empty pipelines object",
        ),
        (
            "unknown-pipeline-key",
            lambda source: source["pipelines"]["rpi"].__setitem__("command", "sh -c x"),
            "pipeline rpi has unknown keys: command",
        ),
        (
            "pipeline-not-an-object",
            lambda source: source["pipelines"].__setitem__("rpi", ["research"]),
            "pipeline rpi must be an object",
        ),
        (
            "bad-pipeline-id",
            lambda source: source["pipelines"].__setitem__(
                "RPI", source["pipelines"]["rpi"]
            ),
            "pipeline id 'RPI' must be a shell-safe lowercase id",
        ),
        (
            # `rpii` is regex-valid and every stage under it is supported, so shape alone
            # accepts it. Only the closed id set catches a pipeline the skill cannot run.
            "regex-valid-but-unsupported-pipeline-id",
            lambda source: source["pipelines"].__setitem__(
                "rpii", source["pipelines"]["rpi"]
            ),
            "registry has unsupported pipeline 'rpii'; expected one of rpi, no-mistakes",
        ),
        (
            "missing-description",
            lambda source: source["pipelines"]["rpi"].__setitem__("description", "  "),
            "pipeline rpi needs a non-empty description",
        ),
        (
            "empty-stages",
            lambda source: source["pipelines"]["rpi"].__setitem__("stages", []),
            "pipeline rpi needs at least one stage",
        ),
        (
            "stages-not-a-list",
            lambda source: source["pipelines"]["rpi"].__setitem__("stages", "research"),
            "pipeline rpi stages must be a list of stage ids",
        ),
        (
            "invalid-stage-id",
            lambda source: source["pipelines"]["rpi"].__setitem__(
                "stages", ["research", "Plan"]
            ),
            "pipeline rpi stages has an invalid stage id: 'Plan'",
        ),
        (
            "repeated-stage",
            lambda source: source["pipelines"]["rpi"].__setitem__(
                "stages", ["research", "research"]
            ),
            "pipeline rpi stages repeats stage 'research'",
        ),
        (
            "typo-stage-id",
            lambda source: source["pipelines"]["rpi"].__setitem__(
                "stages", ["reserach", "plan", "implement"]
            ),
            "pipeline rpi stages has unsupported stage 'reserach'; expected one of "
            "research, plan, implement, validate",
        ),
        (
            "regex-valid-but-unsupported-stage",
            lambda source: source["pipelines"]["rpi"].__setitem__(
                "stages", ["research", "plan", "deploy"]
            ),
            "pipeline rpi stages has unsupported stage 'deploy'",
        ),
        (
            "typo-optional-stage-id",
            lambda source: source["pipelines"]["rpi"].__setitem__(
                "optional_stages", ["research", "plna"]
            ),
            "pipeline rpi optional_stages has unsupported stage 'plna'",
        ),
        (
            "skippable-review-gate-without-skip-gate",
            lambda source: source["pipelines"]["rpi"].pop("skip_gate"),
            "lists its review_gate 'plan' in optional_stages, so it must declare where "
            "approval goes",
        ),
        (
            "skippable-review-gate-with-unknown-skip-gate",
            lambda source: source["pipelines"]["rpi"].__setitem__(
                "skip_gate", "trust-the-worker"
            ),
            "skip_gate must be one of issue-approval; got 'trust-the-worker'",
        ),
        (
            "skip-gate-that-can-never-apply",
            lambda source: source["pipelines"]["no-mistakes"].__setitem__(
                "skip_gate", "issue-approval"
            ),
            "sets skip_gate but its review_gate 'none' is not an optional stage",
        ),
        (
            # `validate` is a supported stage, just not one THIS pipeline runs — so the
            # subset check is what has to catch it, not the vocabulary check.
            "optional-stage-outside-stages",
            lambda source: source["pipelines"]["rpi"].__setitem__(
                "optional_stages", ["research", "validate"]
            ),
            "optional_stages must be stages of the pipeline; validate not in",
        ),
        (
            "optional-stages-not-a-list",
            lambda source: source["pipelines"]["rpi"].__setitem__(
                "optional_stages", None
            ),
            "pipeline rpi optional_stages must be a list of stage ids",
        ),
        (
            "review-gate-outside-stages",
            lambda source: source["pipelines"]["rpi"].__setitem__(
                "review_gate", "review"
            ),
            "review_gate must be 'none' or one of its stages",
        ),
        (
            "review-gate-missing",
            lambda source: source["pipelines"]["rpi"].pop("review_gate"),
            "review_gate must be 'none' or one of its stages",
        ),
        (
            "max-phases-zero",
            lambda source: source["pipelines"]["rpi"].__setitem__("max_phases", 0),
            "max_phases must be a positive integer when present",
        ),
        (
            "max-phases-string",
            lambda source: source["pipelines"]["rpi"].__setitem__("max_phases", "3"),
            "max_phases must be a positive integer when present",
        ),
        (
            "max-phases-boolean",
            lambda source: source["pipelines"]["rpi"].__setitem__("max_phases", True),
            "max_phases must be a positive integer when present",
        ),
        (
            "max-phases-empty",
            lambda source: source["pipelines"]["rpi"].__setitem__("max_phases", None),
            "max_phases must be a positive integer when present",
        ),
    ],
)
def test_every_registry_validation_failure_names_its_field(
    tmp_path: Path,
    case: str,
    mutate: Callable[[dict[str, Any]], None],
    message: str,
) -> None:
    path = _mutated(tmp_path, mutate)

    with pytest.raises(PipelineError, match=message):
        pipelines.load_registry(path)


def test_registry_must_be_one_yaml_object(tmp_path: Path) -> None:
    path = _written(tmp_path, ["rpi"])

    with pytest.raises(PipelineError, match="must be one YAML object"):
        pipelines.load_registry(path)


def test_unreadable_registry_fails_loud(tmp_path: Path) -> None:
    with pytest.raises(PipelineError, match="unreadable or invalid YAML"):
        pipelines.load_registry(tmp_path / "absent.yaml")


# --- listing ------------------------------------------------------------------


def test_table_marks_optional_stages_and_lists_every_pipeline() -> None:
    table = pipelines.render_table(pipelines.load_registry())

    lines = table.splitlines()
    assert lines[0].split() == ["PIPELINE", "STAGES", "DESCRIPTION"]
    assert lines[1].startswith("rpi ")
    assert "research? -> plan? -> implement" in lines[1]
    assert "implement -> validate" in lines[2]
    assert "`?` marks an optional stage" in table


def test_json_index_carries_the_full_validated_record() -> None:
    index = pipelines.load_registry().index()

    assert index["rpi"] == {
        "id": "rpi",
        "description": "research -> plan -> implement; research and plan are optional stages",
        "stages": ["research", "plan", "implement"],
        "optional_stages": ["research", "plan"],
        "required_stages": ["implement"],
        "review_gate": "plan",
        "skip_gate": "issue-approval",
        "max_phases": 3,
    }
    assert index["no-mistakes"]["review_gate"] == "none"
    assert index["no-mistakes"]["skip_gate"] is None
    assert index["no-mistakes"]["max_phases"] is None


def test_main_list_prints_the_table_and_the_json_index(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert pipelines.main(["list"]) == 0
    table = capsys.readouterr().out
    assert "PIPELINE" in table and "rpi" in table

    assert pipelines.main(["list", "--json"]) == 0
    printed = json.loads(capsys.readouterr().out)
    assert sorted(printed) == ["no-mistakes", "rpi"]
    assert printed["rpi"]["stages"] == ["research", "plan", "implement"]


# --- the launch ---------------------------------------------------------------


class FakeHerdr:
    """One scripted Herdr session: records every call, answers the four launch queries."""

    def __init__(self, *, workspaces: list[dict] | None = None) -> None:
        self.workspaces = workspaces if workspaces is not None else []
        self.calls: list[tuple[str, ...]] = []

    def herdr_json(self, *args: str, **_: object) -> dict:
        self.calls.append(args)
        head = args[:2]
        if head == ("workspace", "list"):
            return {"result": {"workspaces": self.workspaces}}
        if head == ("pane", "list"):
            return {"result": {"panes": [{"pane_id": "pane-anchor"}]}}
        if head == ("pane", "split"):
            return {"result": {"pane": {"pane_id": "pane-split"}}}
        if head == ("workspace", "create"):
            return {
                "result": {
                    "root_pane": {
                        "pane_id": "pane-root",
                        "tab_id": "tab-root",
                        "workspace_id": "ws-created",
                    }
                }
            }
        raise AssertionError(f"unexpected herdr json call: {args}")

    def herdr(self, *args: str, **_: object) -> None:
        self.calls.append(args)

    def pane_command(self) -> str:
        run = next(call for call in self.calls if call[:2] == ("pane", "run"))
        return run[3]


@pytest.fixture
def fake_herdr(monkeypatch: pytest.MonkeyPatch) -> FakeHerdr:
    fake = FakeHerdr()
    monkeypatch.setattr(
        pipelines.control, "resolve_current_herdr_session_name", lambda: "lab-session"
    )
    monkeypatch.setattr(pipelines.control, "herdr_json", fake.herdr_json)
    monkeypatch.setattr(pipelines.control, "herdr", fake.herdr)
    monkeypatch.setattr(
        pipelines.control,
        "place_started_agent_in_role_tab",
        lambda pane_id, *args, **kwargs: pane_id,
    )
    monkeypatch.setattr(
        pipelines.control,
        "wait_for_agent_health",
        lambda pane_id, **kwargs: {"healthy": True, "pane_id": pane_id},
    )
    return fake


@pytest.mark.parametrize(
    ("args", "expected_tail"),
    [
        ([], ""),
        (["--skip-research"], " --skip-research"),
        (["docs/issues/dry-run-flag.md"], " docs/issues/dry-run-flag.md"),
        (["docs/issues/dry run.md"], " 'docs/issues/dry run.md'"),
    ],
)
def test_prompt_requotes_the_arguments_it_receives(
    args: list[str], expected_tail: str
) -> None:
    """`shlex.join` regroups what reaches Python — NOT what the caller typed.

    `just` word-splits `{{args}}` in the calling shell before this runs, so a location with a
    space in it arrives here as two separate arguments and no requoting can put it back
    together. This covers the requoting of what survives that split, nothing more.
    """
    pipeline = pipelines.load_registry().resolve("rpi")

    assert (
        pipelines.pipeline_prompt(pipeline, args)
        == f"/upagent-pipeline rpi{expected_tail}"
    )


def test_prompt_refuses_a_control_character_argument() -> None:
    pipeline = pipelines.load_registry().resolve("rpi")

    with pytest.raises(PipelineError, match="control character"):
        pipelines.pipeline_prompt(pipeline, ["do this\nthen /exit"])


def test_unknown_pipeline_fails_before_herdr_is_touched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refuse() -> str:
        raise AssertionError("Herdr must not be contacted for an unknown pipeline")

    monkeypatch.setattr(pipelines.control, "resolve_current_herdr_session_name", refuse)

    with pytest.raises(PipelineError, match="unknown pipeline 'rpo'.*rpi, no-mistakes"):
        pipelines.launch("rpo", [], repo=tmp_path)


def test_a_herdr_server_that_is_not_running_fails_with_the_start_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def not_running() -> str:
        raise pipelines.control.HerdrTransportError(
            "could not resolve Herdr session: server is not running"
        )

    monkeypatch.setattr(
        pipelines.control, "resolve_current_herdr_session_name", not_running
    )

    with pytest.raises(PipelineError, match="server is not running.*run `herdr`"):
        pipelines.launch("rpi", [], repo=tmp_path)


def test_launch_adopts_the_unified_workspace_and_runs_the_slash_command(
    tmp_path: Path, fake_herdr: FakeHerdr
) -> None:
    fake_herdr.workspaces = [
        {"label": pipelines.control.UNIFIED_WORKSPACE_LABEL, "workspace_id": "ws-1"}
    ]

    result = pipelines.launch("rpi", ["--skip-research"], repo=tmp_path)

    assert result["workspace_state"] == "adopted"
    assert result["workspace_id"] == "ws-1"
    assert result["pane_id"] == "pane-split"
    assert result["prompt"] == "/upagent-pipeline rpi --skip-research"
    assert result["health"] == {"healthy": True, "pane_id": "pane-split"}
    assert fake_herdr.pane_command() == (
        f"cd {tmp_path} && claude --dangerously-skip-permissions "
        "'/upagent-pipeline rpi --skip-research'"
    )
    assert ("pane", "rename", "pane-split", "pipeline-rpi") in fake_herdr.calls
    assert not any(call[:2] == ("workspace", "create") for call in fake_herdr.calls)


def test_launch_quotes_a_repo_path_that_needs_quoting(
    tmp_path: Path, fake_herdr: FakeHerdr
) -> None:
    repo = tmp_path / "a dir"
    repo.mkdir()

    pipelines.launch("rpi", [], repo=repo)

    assert fake_herdr.pane_command() == (
        f"cd '{repo}' && claude --dangerously-skip-permissions '/upagent-pipeline rpi'"
    )


def test_launch_creates_the_unified_workspace_when_none_exists(
    tmp_path: Path, fake_herdr: FakeHerdr
) -> None:
    result = pipelines.launch("no-mistakes", [], repo=tmp_path)

    assert result["workspace_state"] == "created"
    assert result["workspace_id"] == "ws-created"
    assert result["pane_id"] == "pane-root"
    assert result["prompt"] == "/upagent-pipeline no-mistakes"
    assert ("tab", "rename", "tab-root", "control") in fake_herdr.calls
    assert ("pane", "rename", "pane-root", "pipeline-no-mistakes") in fake_herdr.calls


def test_a_failed_startup_closes_the_workspace_this_launch_created(
    tmp_path: Path, fake_herdr: FakeHerdr, monkeypatch: pytest.MonkeyPatch
) -> None:
    def unhealthy(pane_id: str, **_: object) -> dict:
        raise pipelines.control.HerdrTransportError(
            f"agent pane {pane_id} did not start claude"
        )

    monkeypatch.setattr(pipelines.control, "wait_for_agent_health", unhealthy)

    with pytest.raises(PipelineError, match="pipeline launch failed.*did not start"):
        pipelines.launch("rpi", [], repo=tmp_path)

    assert ("workspace", "close", "ws-created") in fake_herdr.calls


def test_a_failed_startup_closes_only_its_own_pane_in_an_adopted_workspace(
    tmp_path: Path, fake_herdr: FakeHerdr, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_herdr.workspaces = [
        {"label": pipelines.control.UNIFIED_WORKSPACE_LABEL, "workspace_id": "ws-1"}
    ]
    monkeypatch.setattr(
        pipelines.control,
        "wait_for_agent_health",
        lambda pane_id, **_: (_ for _ in ()).throw(
            pipelines.control.HerdrTransportError("pane never became healthy")
        ),
    )

    with pytest.raises(PipelineError, match="pipeline launch failed"):
        pipelines.launch("rpi", [], repo=tmp_path)

    assert ("pane", "close", "pane-split") in fake_herdr.calls
    assert not any(call[:2] == ("workspace", "close") for call in fake_herdr.calls)


def test_a_failed_tab_placement_closes_the_pane_the_split_created(
    tmp_path: Path, fake_herdr: FakeHerdr, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The split pane exists before `launch` knows its id, so nothing but the splitter can
    # close it — an adopted workspace would otherwise keep it forever.
    fake_herdr.workspaces = [
        {"label": pipelines.control.UNIFIED_WORKSPACE_LABEL, "workspace_id": "ws-1"}
    ]
    monkeypatch.setattr(
        pipelines.control,
        "place_started_agent_in_role_tab",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            pipelines.control.HerdrTransportError("'control' tab has no target pane")
        ),
    )

    with pytest.raises(PipelineError, match="could not place pipeline pane pane-split"):
        pipelines.launch("rpi", [], repo=tmp_path)

    assert ("pane", "close", "pane-split") in fake_herdr.calls
    assert not any(call[:2] == ("pane", "run") for call in fake_herdr.calls)
    assert not any(call[:2] == ("workspace", "close") for call in fake_herdr.calls)


def test_launch_requires_an_existing_repo_directory(
    tmp_path: Path, fake_herdr: FakeHerdr
) -> None:
    with pytest.raises(PipelineError, match="must be an existing directory"):
        pipelines.launch("rpi", [], repo=tmp_path / "absent")


def test_main_launch_prints_one_pipeline_started_receipt(
    tmp_path: Path, fake_herdr: FakeHerdr, capsys: pytest.CaptureFixture[str]
) -> None:
    assert (
        pipelines.main(["launch", "--repo", str(tmp_path), "rpi", "--skip-plan"]) == 0
    )

    printed = capsys.readouterr().out.strip()
    tag, _, payload = printed.partition(" ")
    assert tag == "PIPELINE_STARTED"
    receipt = json.loads(payload)
    assert receipt["prompt"] == "/upagent-pipeline rpi --skip-plan"
    assert receipt["pipeline"]["id"] == "rpi"
    assert receipt["herdr_session"] == "lab-session"


def test_main_launch_of_an_unknown_pipeline_exits_with_the_valid_ids(
    tmp_path: Path, fake_herdr: FakeHerdr
) -> None:
    with pytest.raises(SystemExit) as failure:
        pipelines.main(["launch", "--repo", str(tmp_path), "rpo"])

    assert "unknown pipeline 'rpo'" in str(failure.value)


def test_the_shipped_registry_is_the_one_the_client_target_loads() -> None:
    # The registry must stay beside the module that validates it: `--target pipelines`
    # resolves the module through the canonical checkout, never through the caller's cwd.
    registry = pipelines.load_registry()

    assert registry.source == (HERE / "pipelines.yaml").resolve()
    assert copy.deepcopy(_shipped())["schema_version"] == pipelines.SCHEMA_VERSION
