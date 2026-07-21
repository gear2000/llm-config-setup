from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import pytest

HERE = Path(__file__).resolve().parent


def _load(name: str, filename: str) -> Any:
    spec = importlib.util.spec_from_file_location(name, HERE / filename)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


completion = _load("completion_under_test", "completion.py")
contracts = _load("completion_contracts_under_test", "contracts.py")
consults = _load("completion_consults_under_test", "contracts_consult.py")


def _order(tmp_path: Path, *, specialist: bool = False) -> dict[str, object]:
    public = tmp_path / "public"
    value: dict[str, object] = {
        "order_id": "stage-1",
        "request_id": "request-1",
        "result_path": str(public / "result.json"),
        "artifact_publication": {
            "schema_version": 1,
            "compacted_path": str(public / "compacted.md"),
            "handoff_path": str(public / "handoff.md"),
            "mandatory_consults": [],
        },
    }
    if specialist:
        publication = value["artifact_publication"]
        assert isinstance(publication, dict)
        publication.update(
            {"answer_path": str(public / "answer.json"), "consult_id": "consult-1"}
        )
    return value


def _manifest(tmp_path: Path, *, specialist: bool = False) -> Any:
    manifest = completion.build_manifest(
        _order(tmp_path, specialist=specialist),
        tmp_path / "request",
        "lease-token",
        "request-1",
    )
    assert manifest is not None
    return manifest


def _write_valid(manifest: Any) -> None:
    paths = {item.kind: item.staging_path for item in manifest.artifacts}
    for path in paths.values():
        path.parent.mkdir(parents=True, exist_ok=True)
    paths["result"].write_text(
        json.dumps(
            {
                "order_id": manifest.order_id,
                "verdict": "passed",
                "revisit": [],
                "full_log": "/tmp/transcript",
            }
        )
    )
    paths["compacted"].write_text("# Compact\n\nDone.\n")
    paths["handoff"].write_text("# Handoff\n\nNothing pending.\n")
    if "answer" in paths:
        paths["answer"].write_text(
            json.dumps(
                {
                    "consult_id": manifest.consult_id,
                    "answer": "Use the typed boundary.",
                    "citations": ["completion.py:1"],
                }
            )
        )


def test_manifest_and_complete_bundle_project_all_artifacts(tmp_path: Path) -> None:
    manifest = _manifest(tmp_path, specialist=True)
    manifest_path = tmp_path / "request" / "artifact-manifest.json"
    completion.write_manifest(manifest_path, manifest)
    _write_valid(manifest)

    projected = completion.project_bundle(
        manifest, load_result=contracts.load_result, load_answer=consults.load_answer
    )

    assert projected["verdict"] == "passed"
    assert (
        completion.parse_manifest(manifest_path.read_text(), manifest)["lease_token"]
        == "lease-token"
    )
    assert [item.public_path.name for item in manifest.artifacts] == [
        "result.json",
        "compacted.md",
        "handoff.md",
        "answer.json",
    ]
    for artifact in manifest.artifacts:
        assert artifact.public_path.read_bytes() == artifact.staging_path.read_bytes()


@pytest.mark.parametrize("kind", ["result", "compacted", "handoff", "answer"])
@pytest.mark.parametrize("failure", ["missing", "malformed"])
def test_every_required_artifact_is_validated(
    tmp_path: Path, kind: str, failure: str
) -> None:
    manifest = _manifest(tmp_path, specialist=True)
    _write_valid(manifest)
    artifact = manifest.artifact(kind)
    if failure == "missing":
        artifact.staging_path.unlink()
    elif kind in ("compacted", "handoff"):
        artifact.staging_path.write_text(" \n")
    else:
        artifact.staging_path.write_text("not-json")

    with pytest.raises(completion.CompletionError, match=kind):
        completion.validate_bundle(
            manifest,
            load_result=contracts.load_result,
            load_answer=consults.load_answer,
        )


def test_blocked_bundle_is_schema_valid_including_specialist_failure(
    tmp_path: Path,
) -> None:
    manifest = _manifest(tmp_path, specialist=True)
    order = _order(tmp_path, specialist=True)

    def write_result(path: Path, reason: str) -> dict[str, object]:
        value = {
            "order_id": order["order_id"],
            "verdict": "blocked",
            "revisit": [],
            "reason": reason,
            "full_log": "python-authored",
        }
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value))
        return value

    completion.write_blocked_bundle(
        manifest,
        "repair exhausted",
        write_result=write_result,
        failure_answer=consults.failure_answer,
    )

    result = completion.validate_bundle(
        manifest, load_result=contracts.load_result, load_answer=consults.load_answer
    )
    answer = consults.load_answer(manifest.artifact("answer").staging_path, "consult-1")
    assert result["verdict"] == "blocked"
    assert "repair exhausted" in answer["error"]


def test_mandatory_consult_accepts_only_matching_cited_hub_receipt(
    tmp_path: Path,
) -> None:
    order = _order(tmp_path)
    publication = order["artifact_publication"]
    assert isinstance(publication, dict)
    publication["mandatory_consults"] = [
        {"consult_id": "consult-required", "specialist": "api-reviewer"}
    ]
    manifest = completion.build_manifest(
        order, tmp_path / "request", "lease-token", "request-1"
    )
    assert manifest is not None
    result = {"verdict": "passed"}

    assert (
        completion.mandatory_consult_errors(
            manifest,
            result,
            {
                "consults_verified": [
                    {
                        "consult_id": "consult-required",
                        "specialist": "api-reviewer",
                        "answer_verdict": "cited",
                    }
                ],
                "consults_unverified": [],
            },
        )
        == []
    )
    for bad in (
        {"consults_verified": [], "consults_unverified": []},
        {
            "consults_verified": [],
            "consults_unverified": [{"consult_id": "consult-required"}],
        },
        {
            "consults_verified": [
                {
                    "consult_id": "consult-required",
                    "specialist": "api-reviewer",
                    "answer_verdict": "failed",
                }
            ],
            "consults_unverified": [],
        },
    ):
        assert completion.mandatory_consult_errors(manifest, result, bad)


def test_reactor_sends_exactly_one_repair_to_the_original_worker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    recruiter = _load("completion_recruiter_under_test", "recruiter.py")
    order = _order(tmp_path)
    manifest = recruiter.completion.build_manifest(
        order, tmp_path / "request", "lease-token", "request-1"
    )
    assert manifest is not None
    result_path = manifest.artifact("result").staging_path
    result_path.parent.mkdir(parents=True, exist_ok=True)
    result_path.write_text(
        json.dumps(
            {
                "order_id": "stage-1",
                "verdict": "passed",
                "revisit": [],
                "full_log": "/tmp/transcript",
            }
        )
    )
    calls: list[str] = []

    def repair(address: str, prompt: str, **kwargs: object) -> None:
        calls.append(address)
        assert "COMPLETION_REPAIR 1/1" in prompt
        manifest.artifact("compacted").staging_path.write_text("# Compact\n")
        manifest.artifact("handoff").staging_path.write_text("# Handoff\n")

    class Ledger:
        def _event(self, *args: object, **kwargs: object) -> None:
            pass

    monkeypatch.setattr(recruiter, "_submit_agent_prompt", repair)
    assert (
        recruiter._complete_typed_bundle(
            Ledger(),
            "key",
            order,
            manifest,
            "worker-address",
            herdr_session="test-session",
        )
        is False
    )
    assert calls == ["worker-address"]


def test_publication_fault_never_creates_a_partial_receipt_signal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest = _manifest(tmp_path)
    _write_valid(manifest)
    original_replace = completion.os.replace
    calls = 0

    def fail_second_projection(source: Path, target: Path) -> None:
        nonlocal calls
        if target in {item.public_path for item in manifest.artifacts}:
            calls += 1
            if calls == 2:
                raise OSError("injected publication crash")
        original_replace(source, target)

    monkeypatch.setattr(completion.os, "replace", fail_second_projection)
    receipt = tmp_path / "request" / "receipt.json"
    with pytest.raises(completion.CompletionError, match="injected publication crash"):
        completion.project_bundle(
            manifest,
            load_result=contracts.load_result,
            load_answer=consults.load_answer,
        )
    assert not receipt.exists()
    assert all(not item.public_path.exists() for item in manifest.artifacts)
