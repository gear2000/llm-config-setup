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


@pytest.mark.parametrize("kind", ["result", "answer"])
@pytest.mark.parametrize("failure", ["missing", "malformed"])
def test_every_mandatory_artifact_is_validated(
    tmp_path: Path, kind: str, failure: str
) -> None:
    """result.json carries the verdict and answer.json is a consult's deliverable."""
    manifest = _manifest(tmp_path, specialist=True)
    _write_valid(manifest)
    artifact = manifest.artifact(kind)
    if failure == "missing":
        artifact.staging_path.unlink()
    else:
        artifact.staging_path.write_text("not-json")

    with pytest.raises(completion.CompletionError, match=kind):
        completion.validate_bundle(
            manifest,
            load_result=contracts.load_result,
            load_answer=consults.load_answer,
        )


@pytest.mark.parametrize("kind", ["compacted", "handoff"])
@pytest.mark.parametrize("failure", ["missing", "blank"])
def test_optional_summaries_never_fail_a_finished_job(
    tmp_path: Path, kind: str, failure: str
) -> None:
    """A worker that skipped a summary still gets its result published.

    These carry no schema, and a later reader can rebuild both from the result, so losing a
    whole successful job over one absent summary is the worse outcome.
    """
    manifest = _manifest(tmp_path, specialist=True)
    _write_valid(manifest)
    artifact = manifest.artifact(kind)
    if failure == "missing":
        artifact.staging_path.unlink()
    else:
        artifact.staging_path.write_text(" \n")

    validated = completion.validate_bundle(
        manifest,
        load_result=contracts.load_result,
        load_answer=consults.load_answer,
    )
    assert validated["verdict"] == "passed"

    projected = completion.project_bundle(
        manifest, load_result=contracts.load_result, load_answer=consults.load_answer
    )
    assert projected["verdict"] == "passed"
    # The skipped summary is simply never published; nothing else is disturbed.
    assert not artifact.public_path.exists()
    assert manifest.artifact("result").public_path.is_file()


def _review_order(tmp_path: Path) -> dict[str, object]:
    order = _order(tmp_path)
    publication = order["artifact_publication"]
    assert isinstance(publication, dict)
    publication["required_artifacts"] = ["compacted"]
    return order


@pytest.mark.parametrize("failure", ["missing", "blank"])
def test_declared_required_compacted_fails_the_bundle(
    tmp_path: Path, failure: str
) -> None:
    """An order that declared `compacted` required can no longer pass without it.

    Enforcement follows the DECLARED contract: this is the field failure where a review
    published "passed" while its verdict document did not exist, forcing a full re-run.
    """
    manifest = completion.build_manifest(
        _review_order(tmp_path), tmp_path / "request", "lease-token", "request-1"
    )
    _write_valid(manifest)
    artifact = manifest.artifact("compacted")
    assert artifact.required
    assert not completion.skip_optional(artifact, artifact.staging_path)
    if failure == "missing":
        artifact.staging_path.unlink()
    else:
        artifact.staging_path.write_text(" \n")

    with pytest.raises(completion.CompletionError, match="compacted"):
        completion.validate_bundle(
            manifest,
            load_result=contracts.load_result,
            load_answer=consults.load_answer,
        )


def test_declared_required_compacted_present_still_publishes(tmp_path: Path) -> None:
    manifest = completion.build_manifest(
        _review_order(tmp_path), tmp_path / "request", "lease-token", "request-1"
    )
    _write_valid(manifest)
    projected = completion.project_bundle(
        manifest, load_result=contracts.load_result, load_answer=consults.load_answer
    )
    assert projected["verdict"] == "passed"
    assert manifest.artifact("compacted").public_path.is_file()


def test_review_stage_orders_default_to_required_compacted(tmp_path: Path) -> None:
    """stage-2-adversarial-audit orders declare compacted required when they spell nothing out."""
    order = _order(tmp_path)
    del order["artifact_publication"]
    order["stage_id"] = "stage-2-adversarial-audit"
    contract = completion.ensure_publication_contract(order)
    assert contract["required_artifacts"] == ["compacted"]

    ordinary = _order(tmp_path)
    del ordinary["artifact_publication"]
    ordinary["stage_id"] = "stage-1-implementation"
    assert "required_artifacts" not in completion.ensure_publication_contract(ordinary)


def test_required_artifacts_rejects_unknown_and_duplicate_kinds(tmp_path: Path) -> None:
    for bad in (["result"], ["verdict"], ["compacted", "compacted"], "compacted"):
        order = _order(tmp_path)
        publication = order["artifact_publication"]
        assert isinstance(publication, dict)
        publication["required_artifacts"] = bad
        with pytest.raises(completion.CompletionError, match="required_artifacts"):
            completion.publication_contract(order)


_REVIEW_DOCUMENT = (
    "## Adversarial review\n\n"
    "Every claim in the result was checked against the diff and the captured test output; "
    "nothing was half-done and no silent failure survived a re-run.\n\n"
    "VERDICT: CLEARED"
)


def _review_contract_order(tmp_path: Path) -> dict[str, object]:
    order = _review_order(tmp_path)
    order["result_contract"] = "review"
    return order


def test_review_compacted_is_derived_from_the_validated_verdict_document(
    tmp_path: Path,
) -> None:
    """The worker writes ONLY result.json; the hub authors compacted.md from its
    verdict_document, the declared-required compacted gate passes, and file content can
    never diverge from the validated field."""
    order = _review_contract_order(tmp_path)
    manifest = completion.build_manifest(
        order, tmp_path / "request", "lease-token", "request-1"
    )
    _write_valid(manifest)
    manifest.artifact("compacted").staging_path.unlink()
    result_path = manifest.artifact("result").staging_path
    staged = json.loads(result_path.read_text())
    staged["verdict_document"] = _REVIEW_DOCUMENT
    result_path.write_text(json.dumps(staged))

    completion.derive_review_compacted(order, manifest, load_result=contracts.load_result)
    derived = manifest.artifact("compacted").staging_path
    assert derived.read_text() == _REVIEW_DOCUMENT + "\n"

    loader = contracts.result_loader(order)
    validated = completion.validate_bundle(
        manifest, load_result=loader, load_answer=consults.load_answer
    )
    assert validated["verdict"] == "passed"

    # Idempotent: identical content is never rewritten (mtime unchanged).
    before = derived.stat().st_mtime_ns
    completion.derive_review_compacted(order, manifest, load_result=contracts.load_result)
    assert derived.stat().st_mtime_ns == before


def test_review_bundle_without_verdict_document_stays_invalid(tmp_path: Path) -> None:
    order = _review_contract_order(tmp_path)
    manifest = completion.build_manifest(
        order, tmp_path / "request", "lease-token", "request-1"
    )
    _write_valid(manifest)
    completion.derive_review_compacted(order, manifest, load_result=contracts.load_result)
    with pytest.raises(completion.CompletionError, match="result"):
        completion.validate_bundle(
            manifest,
            load_result=contracts.result_loader(order),
            load_answer=consults.load_answer,
        )


def test_derive_review_compacted_is_a_noop_for_ordinary_orders(tmp_path: Path) -> None:
    order = _order(tmp_path)
    manifest = completion.build_manifest(
        order, tmp_path / "request", "lease-token", "request-1"
    )
    _write_valid(manifest)
    manifest.artifact("compacted").staging_path.unlink()
    completion.derive_review_compacted(order, manifest, load_result=contracts.load_result)
    assert not manifest.artifact("compacted").staging_path.exists()


def test_manifest_roundtrip_preserves_promoted_summary(tmp_path: Path) -> None:
    """write_manifest -> parse_manifest accepts a promoted compacted and never a demoted result."""
    manifest = completion.build_manifest(
        _review_order(tmp_path), tmp_path / "request", "lease-token", "request-1"
    )
    path = tmp_path / "request" / "manifest.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    completion.write_manifest(path, manifest)
    parsed = completion.parse_manifest(path.read_text(), manifest)
    by_kind = {item["kind"]: item for item in parsed["artifacts"]}
    assert by_kind["compacted"]["required"] is True
    assert by_kind["handoff"]["required"] is False

    demoted = json.loads(path.read_text())
    for item in demoted["artifacts"]:
        if item["kind"] == "result":
            item["required"] = False
    with pytest.raises(completion.CompletionError, match="required flag must be True"):
        completion.parse_manifest(json.dumps(demoted))


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


def test_public_reactor_owns_internal_revisit_instead_of_rejecting_worker_stage_guess(
    tmp_path: Path,
) -> None:
    recruiter = _load("completion_public_recruiter_under_test", "recruiter.py")
    order = _order(tmp_path)
    order["public_request"] = {
        "payload_sha256": "a" * 64,
        "prompt_sha256": "b" * 64,
        "prompt_snapshot": str(tmp_path / "prompt.md"),
        "type": "worker",
    }
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
                "revisit": ["worker"],
                "full_log": "worker-session",
            }
        )
    )
    manifest.artifact("compacted").staging_path.write_text("# Compact\n")
    manifest.artifact("handoff").staging_path.write_text("# Handoff\n")

    class Ledger:
        def _event(self, *args: object, **kwargs: object) -> None:
            pass

    assert (
        recruiter._complete_typed_bundle(
            Ledger(),
            "key",
            order,
            manifest,
            None,
            herdr_session="test-session",
        )
        is False
    )
    assert json.loads(result_path.read_text())["revisit"] == []


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
    # Only a mandatory artifact triggers repair now, so stage a malformed result.json.
    result_path.write_text("not-json")
    calls: list[str] = []

    def repair(address: str, prompt: str, **kwargs: object) -> None:
        calls.append(address)
        assert "COMPLETION_REPAIR 1/1" in prompt
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
