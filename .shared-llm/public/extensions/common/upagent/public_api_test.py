# pyright: reportMissingImports=false
"""Strict public request boundary, zero-launch, and idempotency tests."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location(
    "public_api_test_module", HERE / "public_api.py"
)
assert spec and spec.loader
public_api = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = public_api
spec.loader.exec_module(public_api)
recruiter_spec = importlib.util.spec_from_file_location(
    "public_api_test_recruiter", HERE / "recruiter.py"
)
assert recruiter_spec and recruiter_spec.loader
recruiter = importlib.util.module_from_spec(recruiter_spec)
sys.modules[recruiter_spec.name] = recruiter
recruiter_spec.loader.exec_module(recruiter)
public_api._bind_recruiter_runtime(recruiter)

REQUEST_ID = "01957f4e-7f7f-7f8b-9c42-6e7f52f9321a"


def _prompt(tmp_path: Path, text: str = "Do the bounded task.\n") -> Path:
    path = tmp_path / "prompt.md"
    path.write_text(text)
    return path


def _worker_argv(tmp_path: Path, **overrides: str) -> list[str]:
    values = {
        "request_id": REQUEST_ID,
        "offering": "pi-gpt-5-6-sol",
        "effort": "high",
        "agent": "backend",
        "prompt_file": str(_prompt(tmp_path)),
        "cwd": str(tmp_path),
        **overrides,
    }
    return [
        "request",
        "--type",
        "worker",
        "--request-id",
        values["request_id"],
        "--offering",
        values["offering"],
        "--effort",
        values["effort"],
        "--agent",
        values["agent"],
        "--prompt-file",
        values["prompt_file"],
        "--cwd",
        values["cwd"],
    ]


def _args(argv: list[str]) -> Any:
    return public_api.contract.parse_argv(argv)


def _enable_managed_leader(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    receipt = tmp_path / "phase-start.json"
    receipt.write_text(
        json.dumps(
            {
                "state": "ready",
                "phase_id": "phase-1",
                "leader_pane": "leader-pane",
                "herdr_session": "test-session",
            }
        )
    )
    monkeypatch.setenv("UPAGENT_PHASE_START_RECEIPT", str(receipt))
    monkeypatch.setenv("HERDR_PANE_ID", "leader-pane")
    monkeypatch.setattr(
        recruiter, "_resolve_current_herdr_session_name", lambda: "test-session"
    )
    monkeypatch.setattr(
        recruiter, "_live_pane_ids", lambda **_kwargs: {"leader-pane", "tui-pane"}
    )


def _control_token_file(tmp_path: Path, token: str) -> str:
    path = tmp_path / "control-token"
    path.write_text(token)
    path.chmod(0o600)
    return str(path)


def _registered_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Any, Any, Any, str, str]:
    monkeypatch.setenv("UPAGENT_HUB_DIR", str(tmp_path / "ledger"))
    validated = public_api.validate_request(_args(_worker_argv(tmp_path)), tmp_path)
    store = public_api.PublicRequestStore()
    registered = store.register(validated, "recruiter-pane")
    store.transition_submission(registered, "submitting")
    store.transition_submission(registered, "submitted")
    order = recruiter.load_order(registered.order_path)
    ledger = recruiter.JobLedger()
    key, _ = ledger.submit(order)
    token = ledger.claim(
        key,
        order["order_id"],
        60_000,
        owner={
            "generation": 1,
            "herdr_session": "test-session",
            "request_id": REQUEST_ID,
            "runner_pid": -1,
        },
    )
    assert isinstance(token, str)
    control_token = ledger.state(key)["requester_control_token"]
    assert isinstance(control_token, str)
    return store, registered, ledger, token, control_token


def _finish_registered_request(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    cleanup_verified: bool = True,
) -> tuple[Any, Any, Any, str, str]:
    store, registered, ledger, token, control_token = _registered_request(
        tmp_path, monkeypatch
    )
    order = recruiter.load_order(registered.order_path)
    key = ledger.key_for_order(order)
    manifest = recruiter.completion.build_manifest(
        order, ledger.request_dir(key), token, REQUEST_ID
    )
    recruiter.completion.write_manifest(
        ledger.request_dir(key) / "artifact-manifest.json", manifest
    )
    result = recruiter._write_required_blocked_bundle(order, manifest, "test terminal")
    receipt = ledger.finalize(
        key,
        token,
        order,
        result,
        cleanup={
            "status": "closed" if cleanup_verified else "cleanup-failed",
            "verified_absent": cleanup_verified,
            "worker_pane": None,
        },
        completion_source="test",
    )
    assert receipt is not None
    return store, registered, ledger, token, control_token


def test_public_help_describes_manager_degradation_and_cursor_map() -> None:
    help_text = public_api.contract.help_text()

    assert "advisory manager failure degrades" in help_text
    assert "--cockpit-pane LIVE_PANE" in help_text
    assert "--cursor '{\"ID\": 12}'" in help_text


def test_worker_request_ignores_unrelated_invalid_specialist_roster(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    persona_dir = tmp_path / ".claude/agents"
    persona_dir.mkdir(parents=True)
    (persona_dir / "backend.md").write_text("---\nname: backend\n---\n")

    def invalid_specialists() -> dict[str, object]:
        raise recruiter.RecruiterError("retired specialist schema")

    monkeypatch.setattr(
        public_api.recruiter, "load_specialist_roster", invalid_specialists
    )

    validated = public_api.validate_request(_args(_worker_argv(tmp_path)), tmp_path)

    assert validated.payload["type"] == "worker"
    assert validated.payload["agent"] == "backend"


def test_verifier_request_ignores_unrelated_invalid_specialist_roster(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    _store, _registered, _ledger, _token, _control = _registered_request(
        tmp_path, monkeypatch
    )
    persona_dir = tmp_path / ".claude/agents"
    persona_dir.mkdir(parents=True)
    (persona_dir / "reviewer.md").write_text("---\nname: reviewer\n---\n")

    def invalid_specialists() -> dict[str, object]:
        raise recruiter.RecruiterError("retired specialist schema")

    calls: list[tuple[object, ...]] = []
    monkeypatch.setattr(
        public_api.recruiter, "load_specialist_roster", invalid_specialists
    )
    monkeypatch.setattr(public_api.recruiter, "_require_hub_authority", lambda: None)
    monkeypatch.setattr(
        public_api.recruiter,
        "cmd_verify",
        lambda *args, **kwargs: calls.append(args) or 0,
    )

    assert (
        public_api.main(
            [
                "verify",
                "--request",
                REQUEST_ID,
                "--offering",
                "pi-gpt-5-4-mini",
                "--effort",
                "low",
                "--agent",
                "reviewer",
            ]
        )
        == 0
    )
    assert len(calls) == 1


def test_duration_minutes_accepts_120_and_defaults_to_60(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("UPAGENT_HUB_DIR", str(tmp_path / "ledger"))
    default_request = public_api.validate_request(
        _args(_worker_argv(tmp_path)), tmp_path
    )
    default_registered = public_api.PublicRequestStore().register(
        default_request, "recruiter-pane"
    )
    default_order = json.loads(default_registered.order_path.read_text())
    assert "duration_minutes" not in default_request.payload
    assert default_order["timeout_ms"] == 3_600_000

    long_id = "01957f4e-7f7f-7f8b-9c42-6e7f52f9321b"
    argv = _worker_argv(tmp_path) + [
        "--request-id",
        long_id,
        "--duration-minutes",
        "120",
    ]
    long_request = public_api.validate_request(_args(argv), tmp_path)
    long_registered = public_api.PublicRequestStore().register(
        long_request, "recruiter-pane"
    )
    long_order = json.loads(long_registered.order_path.read_text())
    assert long_request.payload["duration_minutes"] == 120
    assert long_order["timeout_ms"] == 7_200_000


@pytest.mark.parametrize("minutes", ("0", "121", "-1"))
def test_duration_minutes_rejects_values_outside_public_cap(
    tmp_path: Path, minutes: str
) -> None:
    with pytest.raises(
        public_api.contract.PublicCommandError, match="between 1 and 120"
    ):
        _args(_worker_argv(tmp_path) + ["--duration-minutes", minutes])


def test_keep_open_is_worker_only_and_maps_to_release_policy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("UPAGENT_HUB_DIR", str(tmp_path / "ledger"))
    _enable_managed_leader(tmp_path, monkeypatch)
    request = public_api.validate_request(
        _args(_worker_argv(tmp_path) + ["--keep-open"]), tmp_path
    )
    registered = public_api.PublicRequestStore().register(request, "recruiter-pane")
    order = json.loads(registered.order_path.read_text())
    assert request.payload["keep_open"] is True
    assert order["completion_policy"] == "requester_release"
    with pytest.raises(public_api.PublicError, match="incompatible with --wait"):
        public_api.execute(
            _args(_worker_argv(tmp_path) + ["--keep-open", "--wait"]), tmp_path
        )

    prompt = _prompt(tmp_path, "Question\n")
    with pytest.raises(public_api.PublicError, match="incompatible keep_open"):
        public_api.validate_request(
            _args(
                [
                    "request",
                    "--type",
                    "specialist",
                    "--specialist",
                    "backend",
                    "--prompt-file",
                    str(prompt),
                    "--cwd",
                    str(tmp_path),
                    "--keep-open",
                ]
            ),
            tmp_path,
        )


def test_keep_open_accepts_managed_tui_owner_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    token = "b" * 32
    monkeypatch.setenv("RUNNER_OWNER_TOKEN_FILE", _control_token_file(tmp_path, token))
    monkeypatch.setenv("RUNNER_RUN_DIR", str(tmp_path))
    monkeypatch.setenv("HERDR_PANE_ID", "tui-pane")
    control = tmp_path / "control"
    control.mkdir()
    (control / "run-owner.json").write_text(
        json.dumps(
            {
                "token": token,
                "heartbeat_at_ns": public_api.time.time_ns(),
                "heartbeat_ttl_seconds": 60,
                "owner": {
                    "kind": "tui",
                    "pane_id": "tui-pane",
                    "herdr_session": "test-session",
                },
            }
        )
    )
    monkeypatch.setattr(
        recruiter, "_resolve_current_herdr_session_name", lambda: "test-session"
    )
    monkeypatch.setattr(recruiter, "_live_pane_ids", lambda **_kwargs: {"tui-pane"})
    request = public_api.validate_request(
        _args(_worker_argv(tmp_path) + ["--keep-open"]), tmp_path
    )
    assert request.payload["managed_requester"] == {
        "id": "tui-controller:tui-pane",
        "kind": "herdr-agent",
        "address": "tui-pane",
    }
    lease_path = control / "run-owner.json"
    stale = json.loads(lease_path.read_text())
    stale["token"] = "c" * 32
    lease_path.write_text(json.dumps(stale))
    with pytest.raises(public_api.PublicError, match="stale or does not match"):
        public_api.validate_request(
            _args(_worker_argv(tmp_path) + ["--keep-open"]), tmp_path
        )


def test_keep_open_rejects_unmanaged_public_caller(tmp_path: Path) -> None:
    with pytest.raises(
        public_api.PublicError, match="managed TUI controller or phase leader"
    ):
        public_api.validate_request(
            _args(_worker_argv(tmp_path) + ["--keep-open"]), tmp_path
        )


def test_review_status_preserves_requester_timeout_decision(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("UPAGENT_HUB_DIR", str(tmp_path / "ledger"))
    _enable_managed_leader(tmp_path, monkeypatch)
    request = public_api.validate_request(
        _args(_worker_argv(tmp_path) + ["--keep-open"]), tmp_path
    )
    store = public_api.PublicRequestStore()
    registered = store.register(request, "recruiter-pane")
    order = recruiter.load_order(registered.order_path)
    ledger = recruiter.JobLedger()
    key, _ = ledger.submit(order)
    token = ledger.claim(
        key,
        order["order_id"],
        60_000,
        owner={"generation": 1, "herdr_session": "test-session", "runner_pid": -1},
    )
    assert isinstance(token, str)
    assert ledger.record_worker(
        key, token, "worker-pane", "workspace", "worker-address"
    )
    assert ledger.mark_worker_healthy(key, token, {"healthy": True})
    ledger.mark_awaiting_requester(key, token, "nonce-1", 1)

    status = public_api._public_status(store, registered)
    assert status["state"]["state"] == "awaiting-requester"
    assert (
        public_api.execute(
            _args(["review-await", "--request", REQUEST_ID, "--timeout-ms", "10"]),
            tmp_path,
        )
        == 2
    )


def test_retained_continue_and_release_use_same_live_worker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("UPAGENT_HUB_DIR", str(tmp_path / "ledger"))
    _enable_managed_leader(tmp_path, monkeypatch)
    request = public_api.validate_request(
        _args(_worker_argv(tmp_path) + ["--keep-open", "--duration-minutes", "120"]),
        tmp_path,
    )
    store = public_api.PublicRequestStore()
    registered = store.register(request, "recruiter-pane")
    order = recruiter.load_order(registered.order_path)
    ledger = recruiter.JobLedger()
    key, _ = ledger.submit(order)
    token = ledger.claim(
        key,
        order["order_id"],
        order["timeout_ms"],
        owner={"generation": 1, "herdr_session": "test-session", "runner_pid": -1},
    )
    assert isinstance(token, str)
    assert ledger.record_worker(
        key, token, "worker-pane", "workspace", "worker-address"
    )
    assert ledger.mark_worker_healthy(key, token, {"healthy": True})
    control_token = ledger.state(key)["requester_control_token"]
    control_file = _control_token_file(tmp_path, control_token)
    review_dir = ledger.result_staging_path(key, token).parent / "review"
    checkpoint1 = review_dir / "checkpoint-0001.json"
    recruiter.JobLedger._write_json(
        checkpoint1,
        {
            "schema_version": 1,
            "order_id": order["order_id"],
            "request_id": REQUEST_ID,
            "lease_token": token,
            "generation": 1,
            "sequence": 1,
            "summary": "First pass",
            "tests": "pass",
            "changed_files": ["x.py"],
        },
    )
    checkpoint1_sha256 = hashlib.sha256(checkpoint1.read_bytes()).hexdigest()
    feedback = tmp_path / "feedback.md"
    feedback.write_text("Please tighten the edge case.\n")
    prompts: list[tuple[str, str, str | None]] = []

    def capture_prompt(
        address: str,
        message: str,
        idle_timeout_ms: int,
        *,
        herdr_session: str | None = None,
    ) -> None:
        assert idle_timeout_ms == 60_000
        prompts.append((address, message, herdr_session))

    monkeypatch.setattr(recruiter, "_submit_agent_prompt", capture_prompt)

    assert (
        public_api.execute(
            _args(
                [
                    "review-continue",
                    "--request",
                    REQUEST_ID,
                    "--checkpoint",
                    "1",
                    "--checkpoint-sha256",
                    checkpoint1_sha256,
                    "--prompt-file",
                    str(feedback),
                    "--control-token-file",
                    control_file,
                ]
            ),
            tmp_path,
        )
        == 0
    )
    feedback_record = json.loads((review_dir / "feedback-0001.json").read_text())
    assert feedback_record["feedback"] == "Please tighten the edge case.\n"
    assert (review_dir / "feedback-0001.delivery.json").is_file()
    assert prompts[0][0::2] == ("worker-address", "test-session")
    assert "checkpoint-0002.json" in prompts[0][1]

    checkpoint2 = review_dir / "checkpoint-0002.json"
    recruiter.JobLedger._write_json(
        checkpoint2,
        {
            "schema_version": 1,
            "order_id": order["order_id"],
            "request_id": REQUEST_ID,
            "lease_token": token,
            "generation": 1,
            "sequence": 2,
            "summary": "Revised pass",
            "tests": "pass",
            "changed_files": ["x.py"],
        },
    )
    checkpoint2_sha256 = hashlib.sha256(checkpoint2.read_bytes()).hexdigest()
    assert (
        public_api.execute(
            _args(
                [
                    "review-release",
                    "--request",
                    REQUEST_ID,
                    "--checkpoint",
                    "2",
                    "--checkpoint-sha256",
                    checkpoint2_sha256,
                    "--control-token-file",
                    control_file,
                ]
            ),
            tmp_path,
        )
        == 0
    )
    release_path = ledger.review_release_path(key, token)
    release = json.loads(release_path.read_text())
    assert release["lease_token"] == token and release["sequence"] == 2
    assert release_path.with_name("release.delivery.json").is_file()
    assert prompts[1][0] == "worker-address"
    assert "REVIEW_RELEASE" in prompts[1][1]


def test_flags_and_file_feed_the_same_canonical_parser(tmp_path: Path) -> None:
    prompt = _prompt(tmp_path)
    file_path = tmp_path / "request.json"
    file_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "request_id": REQUEST_ID,
                "type": "worker",
                "offering": "pi-gpt-5-6-sol",
                "effort": "high",
                "agent": "backend",
                "prompt_file": str(prompt),
                "cwd": str(tmp_path),
            }
        )
    )

    inline = public_api.validate_request(
        _args([*_worker_argv(tmp_path), "--cockpit-pane", "caller-pane"]),
        tmp_path,
    )
    from_file = public_api.validate_request(
        _args(
            [
                "request",
                "--file",
                str(file_path),
                "--cockpit-pane",
                "caller-pane",
                "--wait",
                "--json",
            ]
        ),
        tmp_path,
    )

    assert inline.payload == from_file.payload
    assert "cockpit_pane" not in inline.payload
    assert inline.payload_sha256 == from_file.payload_sha256
    assert inline.prompt_bytes == from_file.prompt_bytes


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda value: value.update(extra=True), "unknown keys"),
        (lambda value: value.update(cockpit_pane="caller-pane"), "unknown keys"),
        (lambda value: value.update(schema_version="1"), "schema_version"),
        (lambda value: value.update(offering="unknown"), "unknown offering"),
        (lambda value: value.update(effort="ultra"), "does not allow effort"),
        (lambda value: value.update(agent="not-a-persona"), "unknown worker persona"),
        (lambda value: value.update(prompt_file="relative.md"), "must be absolute"),
        (lambda value: value.update(request_id="BAD"), "canonical UUID or ULID"),
        (lambda value: value.update(specialist="backend"), "incompatible specialist"),
    ],
)
def test_invalid_file_requests_fail_before_store_or_launch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mutate: Any, message: str
) -> None:
    prompt = _prompt(tmp_path)
    value: dict[str, object] = {
        "schema_version": 1,
        "request_id": REQUEST_ID,
        "type": "worker",
        "offering": "pi-gpt-5-6-sol",
        "effort": "high",
        "agent": "backend",
        "prompt_file": str(prompt),
        "cwd": str(tmp_path),
    }
    mutate(value)
    request_file = tmp_path / "request.json"
    request_file.write_text(json.dumps(value))
    monkeypatch.setenv("UPAGENT_HUB_DIR", str(tmp_path / "ledger"))
    launched: list[object] = []
    monkeypatch.setattr(
        public_api.recruiter,
        "cmd_request_strict",
        lambda *args, **kwargs: launched.append(args) or 0,
    )

    with pytest.raises(public_api.PublicError, match=message):
        public_api.validate_request(
            _args(["request", "--file", str(request_file)]), tmp_path
        )

    assert launched == []
    assert not (tmp_path / "ledger/public/requests").exists()


def test_file_is_mutually_exclusive_with_defining_flags_but_not_transport_flags(
    tmp_path: Path,
) -> None:
    request_file = tmp_path / "request.json"
    request_file.write_text("{}")

    with pytest.raises(
        public_api.contract.PublicCommandError, match="mutually exclusive"
    ):
        _args(["request", "--file", str(request_file), "--agent", "backend"])
    parsed = _args(
        [
            "request",
            "--file",
            str(request_file),
            "--cockpit-pane",
            "caller-pane",
            "--wait",
            "--json",
        ]
    )
    assert parsed.cockpit_pane == "caller-pane"
    assert parsed.wait is True and parsed.json is True


def test_explicit_live_cockpit_pane_is_written_to_submitted_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("UPAGENT_HUB_DIR", str(tmp_path / "ledger"))
    monkeypatch.setattr(
        public_api,
        "_cockpit_pane",
        lambda: pytest.fail("explicit pane must bypass service-pane resolution"),
    )
    monkeypatch.setattr(
        public_api.recruiter,
        "_resolve_current_herdr_session_name",
        lambda: "current-session",
    )
    listed_sessions: list[str | None] = []

    def live_panes(*, herdr_session: str | None = None) -> set[str]:
        listed_sessions.append(herdr_session)
        return {"caller-pane", "other-pane"}

    submitted_orders: list[dict[str, object]] = []

    def submit(order_path: str, _roster_path: str) -> int:
        submitted_orders.append(json.loads(Path(order_path).read_text()))
        return 0

    monkeypatch.setattr(public_api.recruiter, "_live_pane_ids", live_panes)
    monkeypatch.setattr(public_api.recruiter, "cmd_request_strict", submit)
    args = _args([*_worker_argv(tmp_path), "--cockpit-pane", "caller-pane"])

    assert public_api.execute(args, tmp_path) == 0

    assert listed_sessions == ["current-session"]
    assert submitted_orders[0]["cockpit_pane"] == "caller-pane"


def test_non_live_cockpit_pane_is_rejected_before_registration_or_submission(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("UPAGENT_HUB_DIR", str(tmp_path / "ledger"))
    monkeypatch.setattr(
        public_api,
        "_cockpit_pane",
        lambda: pytest.fail("explicit pane must bypass service-pane resolution"),
    )
    monkeypatch.setattr(
        public_api.recruiter,
        "_resolve_current_herdr_session_name",
        lambda: "current-session",
    )
    monkeypatch.setattr(
        public_api.recruiter,
        "_live_pane_ids",
        lambda *, herdr_session=None: {"other-pane"},
    )
    submissions: list[str] = []
    monkeypatch.setattr(
        public_api.recruiter,
        "cmd_request_strict",
        lambda order, _roster: submissions.append(order) or 0,
    )
    args = _args([*_worker_argv(tmp_path), "--cockpit-pane", "stale-pane"])

    with pytest.raises(
        public_api.PublicError, match="not live in the current Herdr session"
    ):
        public_api.execute(args, tmp_path)

    assert submissions == []
    assert not (tmp_path / "ledger/public/requests").exists()


def test_specialist_resolves_pinned_offering_and_rejects_public_override(
    tmp_path: Path,
) -> None:
    prompt = _prompt(tmp_path, "What contract applies?\n")
    parsed = _args(
        [
            "request",
            "--type",
            "specialist",
            "--specialist",
            "backend",
            "--prompt-file",
            str(prompt),
            "--cwd",
            str(tmp_path),
        ]
    )

    request = public_api.validate_request(parsed, tmp_path)

    assert request.payload["offering"] == "claude-sonnet-5"
    assert request.payload["effort"] == "medium"
    assert request.payload["agent"] == "backend"
    with pytest.raises(public_api.contract.PublicCommandError, match="request"):
        _args(
            [
                "request",
                "--type",
                "specialist",
                "--specialist",
                "backend",
                "--offering",
                "pi-gpt-5-6-sol",
                "--prompt-file",
                str(prompt),
            ]
        )


def test_public_specialist_uses_private_staging_and_dedicated_manager(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("UPAGENT_HUB_DIR", str(tmp_path / "ledger"))
    prompt = _prompt(tmp_path, "What contract applies?\n")
    validated = public_api.validate_request(
        _args(
            [
                "request",
                "--type",
                "specialist",
                "--request-id",
                REQUEST_ID,
                "--specialist",
                "backend",
                "--prompt-file",
                str(prompt),
                "--cwd",
                str(tmp_path),
            ]
        ),
        tmp_path,
    )
    registered = public_api.PublicRequestStore().register(validated, "recruiter-pane")
    order = json.loads(registered.order_path.read_text())
    instructions = (registered.request_dir / "instructions.md").read_text()

    assert order["management"] == {"mode": "dedicated"}
    assert order["artifact_publication"]["answer_path"].endswith("/answer.json")
    assert order["artifact_publication"]["consult_id"] == REQUEST_ID
    assert order["public_request"]["answer_path"] not in instructions
    assert "lease-private answer path" in instructions


def test_public_request_ignores_legacy_manager_command_and_uses_approved_renderer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("UPAGENT_HUB_DIR", str(tmp_path / "ledger"))
    monkeypatch.setattr(public_api, "_cockpit_pane", lambda: "recruiter-pane")
    legacy = tmp_path / "upagent.yaml"
    legacy.write_text(
        """harnesses:\n  claude: legacy-worker\nmanagement:\n  account_manager:\n    command: legacy-manager {brief_path} {output_path}\n"""
    )
    monkeypatch.setattr(
        public_api.recruiter, "default_roster_path", lambda: str(legacy)
    )
    submitted_rosters: list[str] = []
    monkeypatch.setattr(
        public_api.recruiter,
        "cmd_request_strict",
        lambda _order, roster: submitted_rosters.append(roster) or 0,
    )

    assert public_api.execute(_args(_worker_argv(tmp_path)), tmp_path) == 0

    assert submitted_rosters == [str(HERE / "offerings.yaml")]
    public_roster = public_api.recruiter.load_roster(submitted_rosters[0])
    manager_command = public_roster["management"]["account_manager"]["command"]
    assert "legacy-manager" not in manager_command
    assert "--agent upagent-account-manager" in manager_command
    assert "--model claude-sonnet-5 --effort low" in manager_command


def test_same_id_same_hash_attaches_without_second_launch_and_changed_prompt_conflicts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("UPAGENT_HUB_DIR", str(tmp_path / "ledger"))
    monkeypatch.setattr(public_api, "_cockpit_pane", lambda: "recruiter-pane")
    launches: list[str] = []
    monkeypatch.setattr(
        public_api.recruiter,
        "cmd_request_strict",
        lambda order, roster: launches.append(order) or 0,
    )
    argv = _worker_argv(tmp_path)
    args = _args(argv)

    assert public_api.execute(args, tmp_path) == 0
    assert public_api.execute(args, tmp_path) == 0
    assert len(launches) == 1
    captured = capsys.readouterr().out
    assert "attached" in captured
    store = public_api.PublicRequestStore()
    before_conflict = store.submission(store.load(REQUEST_ID))

    _prompt(tmp_path, "The prompt changed after first submission.\n")
    with pytest.raises(public_api.PublicError, match="request_id_conflict"):
        public_api.execute(_args(argv), tmp_path)
    assert len(launches) == 1
    assert store.submission(store.load(REQUEST_ID)) == before_conflict


def test_registered_request_resumes_after_crash_before_recruiter_submission(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("UPAGENT_HUB_DIR", str(tmp_path / "ledger"))
    monkeypatch.setattr(public_api, "_cockpit_pane", lambda: "recruiter-pane")
    args = _args(_worker_argv(tmp_path))
    validated = public_api.validate_request(args, tmp_path)
    store = public_api.PublicRequestStore()

    # Fault seam: registration committed, then the caller process died before submission.
    registered = store.register(validated, "recruiter-pane")
    assert store.submission(registered)["state"] == "registered"
    launches: list[str] = []
    monkeypatch.setattr(
        public_api.recruiter,
        "cmd_request_strict",
        lambda order, roster: launches.append(order) or 0,
    )

    assert public_api.execute(args, tmp_path) == 0

    resumed = store.load(REQUEST_ID)
    assert launches == [str(resumed.order_path)]
    assert store.submission(resumed)["state"] == "submitted"
    assert store.submission(resumed)["attempts"] == 1


def test_retry_after_recruiter_acceptance_reattaches_idempotently(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class InjectedCrash(RuntimeError):
        pass

    monkeypatch.setenv("UPAGENT_HUB_DIR", str(tmp_path / "ledger"))
    monkeypatch.setattr(public_api, "_cockpit_pane", lambda: "recruiter-pane")
    accepted: list[bool] = []

    def accept_then_crash_once(order_path: str, _roster_path: str) -> int:
        order = public_api.recruiter.load_order(order_path)
        _key, created = public_api.recruiter.JobLedger().submit(order)
        accepted.append(created)
        if len(accepted) == 1:
            raise InjectedCrash("crash after Recruiter acceptance")
        return 0

    monkeypatch.setattr(
        public_api.recruiter, "cmd_request_strict", accept_then_crash_once
    )
    args = _args(_worker_argv(tmp_path))

    with pytest.raises(InjectedCrash, match="after Recruiter acceptance"):
        public_api.execute(args, tmp_path)
    store = public_api.PublicRequestStore()
    interrupted = store.load(REQUEST_ID)
    assert store.submission(interrupted)["state"] == "submitting"

    assert public_api.execute(args, tmp_path) == 0

    recovered = store.load(REQUEST_ID)
    assert accepted == [True, False]
    assert store.submission(recovered)["state"] == "submitted"
    assert store.submission(recovered)["attempts"] == 2
    key = public_api.recruiter.JobLedger.key_for_order(
        public_api.recruiter.load_order(recovered.order_path)
    )
    assert public_api.recruiter.JobLedger().request_dir(key).is_dir()


def test_concurrent_identical_retries_submit_once_under_request_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("UPAGENT_HUB_DIR", str(tmp_path / "ledger"))
    monkeypatch.setattr(public_api, "_cockpit_pane", lambda: "recruiter-pane")
    args = _args(_worker_argv(tmp_path))
    validated = public_api.validate_request(args, tmp_path)
    store = public_api.PublicRequestStore()
    store.register(validated, "recruiter-pane")
    entered = threading.Event()
    release = threading.Event()
    launches: list[str] = []

    def blocking_submit(order_path: str, _roster_path: str) -> int:
        launches.append(order_path)
        entered.set()
        assert release.wait(timeout=3)
        return 0

    monkeypatch.setattr(public_api.recruiter, "cmd_request_strict", blocking_submit)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(public_api.execute, args, tmp_path)
        assert entered.wait(timeout=2)
        second = pool.submit(public_api.execute, args, tmp_path)
        assert not second.done()
        assert launches == [str(store.load(REQUEST_ID).order_path)]
        release.set()
        assert first.result(timeout=3) == 0
        assert second.result(timeout=3) == 0

    submission = store.submission(store.load(REQUEST_ID))
    assert launches == [str(store.load(REQUEST_ID).order_path)]
    assert submission["state"] == "submitted"
    assert submission["attempts"] == 1


def test_prompt_bytes_and_offering_snapshot_are_immutable_request_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("UPAGENT_HUB_DIR", str(tmp_path / "ledger"))
    validated = public_api.validate_request(_args(_worker_argv(tmp_path)), tmp_path)
    registered = public_api.PublicRequestStore().register(validated, "recruiter-pane")
    record = json.loads((registered.request_dir / "request.json").read_text())
    order = json.loads(registered.order_path.read_text())

    assert (registered.request_dir / "prompt.md").read_bytes() == validated.prompt_bytes
    assert record["payload_sha256"] == validated.payload_sha256
    assert record["offering_snapshot"] == order["offering_snapshot"]
    assert (
        order["public_request"]["prompt_sha256"] == validated.payload["prompt_sha256"]
    )
    assert order["instructions_path"] == str(registered.request_dir / "prompt.md")
    status = public_api._public_status(public_api.PublicRequestStore(), registered)
    assert status["payload_sha256"] == validated.payload_sha256
    assert {artifact["kind"] for artifact in status["artifacts"]} == {
        "prompt",
        "order",
        "result",
        "compacted",
        "handoff",
        "receipt",
    }


def test_request_id_accepts_only_canonical_uuid_or_ulid() -> None:
    assert public_api._canonical_request_id(REQUEST_ID) == REQUEST_ID
    ulid = "01J00000000000000000000000"
    assert public_api._canonical_request_id(ulid) == ulid
    for invalid in (REQUEST_ID.upper(), ulid.lower(), "01I00000000000000000000000"):
        with pytest.raises(public_api.PublicError):
            public_api._canonical_request_id(invalid)


def test_get_cancel_and_cleanup_grammar_is_explicit_and_fail_loud() -> None:
    assert _args(["get", "--request", REQUEST_ID]).request == REQUEST_ID
    cancel = _args(
        [
            "cancel",
            "--request",
            REQUEST_ID,
            "--control-token-file",
            "/private/token",
        ]
    )
    assert cancel.control_token_file == "/private/token"
    cleanup = _args(
        [
            "cleanup",
            "--all-terminal",
            "--older-than-seconds",
            "5",
            "--apply",
        ]
    )
    assert cleanup.all_terminal is True and cleanup.apply is True
    with pytest.raises(public_api.contract.PublicCommandError):
        _args(["cancel", "--request", REQUEST_ID])
    with pytest.raises(public_api.contract.PublicCommandError):
        _args(["cleanup", "--request", REQUEST_ID, "--all-terminal"])
    with pytest.raises(public_api.contract.PublicCommandError, match="zero or greater"):
        _args(["cleanup", "--request", REQUEST_ID, "--older-than-seconds", "-1"])


def test_cancel_control_token_file_must_be_absolute_private_regular_file(
    tmp_path: Path,
) -> None:
    token = "a" * 32
    private = Path(_control_token_file(tmp_path, token))
    assert public_api._private_control_token_file(str(private)) == token

    private.chmod(0o644)
    with pytest.raises(public_api.PublicError, match="group/world"):
        public_api._private_control_token_file(str(private))
    private.chmod(0o600)

    symlink = tmp_path / "token-link"
    symlink.symlink_to(private)
    with pytest.raises(public_api.PublicError, match="non-symlink"):
        public_api._private_control_token_file(str(symlink))

    fifo = tmp_path / "token-fifo"
    os.mkfifo(fifo, mode=0o600)
    with pytest.raises(public_api.PublicError, match="regular file"):
        public_api._private_control_token_file(str(fifo))

    with pytest.raises(public_api.PublicError, match="must be absolute"):
        public_api._private_control_token_file("relative-token")


def test_get_is_read_only_and_includes_typed_artifact_and_log_pointers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    store, registered, _ledger, _token, _control = _finish_registered_request(
        tmp_path, monkeypatch
    )
    before = {
        path.relative_to(registered.request_dir): path.stat().st_mtime_ns
        for path in registered.request_dir.rglob("*")
        if path.is_file()
    }

    assert (
        public_api.execute(_args(["get", "--request", REQUEST_ID, "--json"]), tmp_path)
        == 0
    )

    value = json.loads(capsys.readouterr().out)
    assert value["state"]["state"] == "finished"
    assert "requester_control_token" not in value["state"]
    assert value["result"]["verdict"] == "blocked"
    assert {item["kind"] for item in value["artifacts"]} >= {
        "result",
        "compacted",
        "handoff",
        "receipt",
        "log",
    }
    after = {
        path.relative_to(registered.request_dir): path.stat().st_mtime_ns
        for path in registered.request_dir.rglob("*")
        if path.is_file()
    }
    assert after == before
    assert store.load(REQUEST_ID).pruned is False


def test_originating_async_request_token_file_can_cancel_the_active_request(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("UPAGENT_HUB_DIR", str(tmp_path / "ledger"))
    monkeypatch.setattr(public_api, "_cockpit_pane", lambda: "recruiter-pane")

    def submit_running(order_path: str, _roster_path: str) -> int:
        order = recruiter.load_order(Path(order_path))
        ledger = recruiter.JobLedger()
        key, _ = ledger.submit(order)
        token = ledger.claim(
            key,
            order["order_id"],
            60_000,
            owner={
                "generation": 1,
                "herdr_session": "test-session",
                "request_id": REQUEST_ID,
                "runner_pid": -1,
            },
        )
        assert isinstance(token, str)
        return 0

    monkeypatch.setattr(public_api.recruiter, "cmd_request_strict", submit_running)

    assert public_api.execute(_args([*_worker_argv(tmp_path), "--json"]), tmp_path) == 0
    request_value = json.loads(capsys.readouterr().out)
    control_token = request_value["state"]["requester_control_token"]
    token_file = _control_token_file(tmp_path, control_token)

    assert (
        public_api.execute(
            _args(
                [
                    "cancel",
                    "--request",
                    REQUEST_ID,
                    "--control-token-file",
                    token_file,
                    "--json",
                ]
            ),
            tmp_path,
        )
        == 0
    )

    cancelled = json.loads(capsys.readouterr().out)
    assert cancelled["cancellation"]["cancelled"] is True
    assert cancelled["result"]["verdict"] == "blocked"
    assert control_token not in json.dumps(cancelled)


def test_anytime_cancel_authenticates_fences_and_publishes_blocked_terminal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, registered, ledger, old_token, control_token = _registered_request(
        tmp_path, monkeypatch
    )
    with pytest.raises(recruiter.RecruiterError, match="control token"):
        recruiter.cmd_cancel(str(registered.order_path), "wrong")

    outcome = recruiter.cmd_cancel(str(registered.order_path), control_token)

    order = recruiter.load_order(registered.order_path)
    key = ledger.key_for_order(order)
    assert outcome["cancelled"] is True
    assert outcome["result"]["verdict"] == "blocked"
    assert "cancelled" in outcome["result"]["reason"]
    assert outcome["receipt"]["state"] == "finished"
    assert outcome["receipt"]["cancelled"] is True
    assert outcome["receipt"]["cleanup"]["verified_absent"] is True
    assert (
        ledger.finalize(
            key,
            old_token,
            order,
            outcome["result"],
            cleanup={"status": "closed", "verified_absent": True},
        )
        is False
    )
    assert public_api._public_status(store, registered)["state"]["state"] == "finished"


def test_control_token_is_exposed_only_to_originating_request_and_cancel_redacts_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    store, _registered, _ledger, old_token, control_token = _registered_request(
        tmp_path, monkeypatch
    )
    active = public_api._public_status(store, store.load(REQUEST_ID))
    assert "token" not in active["state"]
    assert "requester_control_token" not in active["state"]
    originating = public_api._public_status(
        store,
        store.load(REQUEST_ID),
        include_requester_control_token=True,
    )
    assert originating["state"]["requester_control_token"] == control_token

    assert (
        public_api.execute(
            _args(
                [
                    "cancel",
                    "--request",
                    REQUEST_ID,
                    "--control-token-file",
                    _control_token_file(tmp_path, control_token),
                    "--json",
                ]
            ),
            tmp_path,
        )
        == 0
    )

    rendered = capsys.readouterr().out
    value = json.loads(rendered)
    assert old_token not in rendered
    assert control_token not in rendered
    assert ".staging" not in rendered
    assert "requester_control_token_sha256" not in rendered
    assert all(
        "staging_path" not in artifact for artifact in value["receipt"]["artifacts"]
    )


def test_identical_active_reattachment_never_discloses_control_token(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _store, _registered, _ledger, _old_token, control_token = _registered_request(
        tmp_path, monkeypatch
    )
    monkeypatch.setattr(public_api, "_cockpit_pane", lambda: "recruiter-pane")

    assert public_api.execute(_args([*_worker_argv(tmp_path), "--json"]), tmp_path) == 0

    rendered = capsys.readouterr().out
    value = json.loads(rendered)
    assert value["attached"] is True
    assert control_token not in rendered
    assert "requester_control_token" not in value["state"]


def test_cancel_returns_existing_terminal_result_when_publication_wins(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _store, registered, _ledger, _token, control_token = _finish_registered_request(
        tmp_path, monkeypatch
    )

    outcome = recruiter.cmd_cancel(str(registered.order_path), control_token)

    assert outcome["terminal"] is True
    assert outcome["cancelled"] is False
    assert outcome["result"]["reason"].endswith("test terminal")


def test_cleanup_dry_run_apply_tombstone_get_and_hash_idempotency(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    store, registered, ledger, _token, control_token = _finish_registered_request(
        tmp_path, monkeypatch
    )
    original_prompt = tmp_path / "prompt.md"
    evidence = ledger.terminal_cleanup_evidence(
        ledger.key(REQUEST_ID),
        REQUEST_ID,
        registered.record["payload_sha256"],
    )
    terminal_at_ns = evidence["terminal_at_ns"]
    assert isinstance(terminal_at_ns, int)

    planned = public_api._cleanup_one(
        store,
        REQUEST_ID,
        older_than_seconds=5,
        apply=False,
        now_ns=terminal_at_ns + 5_000_000_000,
    )
    assert planned["status"] == "planned"
    assert registered.order_path.exists()

    cleaned = public_api._cleanup_one(
        store,
        REQUEST_ID,
        older_than_seconds=5,
        apply=True,
        now_ns=terminal_at_ns + 5_000_000_000,
    )
    assert cleaned["status"] == "cleaned"
    assert original_prompt.is_file(), "caller-owned prompt must never be deleted"
    assert {path.name for path in registered.request_dir.iterdir()} == {
        "tombstone.json"
    }
    assert {
        path.name for path in ledger.request_dir(ledger.key(REQUEST_ID)).iterdir()
    } == {"tombstone.json"}
    status = public_api._public_status(store, store.load(REQUEST_ID))
    assert status["state"]["state"] == "pruned"
    assert status["result"]["verdict"] == "blocked"
    assert {item["kind"] for item in status["artifacts"]} >= {
        "result",
        "compacted",
        "handoff",
        "receipt",
        "log",
    }
    (registered.request_dir / "interrupted-public-residual").write_text("residual")
    recruiter_dir = ledger.request_dir(ledger.key(REQUEST_ID))
    (recruiter_dir / "interrupted-private-residual").write_text("residual")
    assert (
        public_api._cleanup_one(
            store,
            REQUEST_ID,
            older_than_seconds=0,
            apply=True,
            now_ns=terminal_at_ns + 6_000_000_000,
        )["status"]
        == "already-pruned"
    )
    assert {path.name for path in registered.request_dir.iterdir()} == {
        "tombstone.json"
    }
    assert {path.name for path in recruiter_dir.iterdir()} == {"tombstone.json"}

    validated = public_api.validate_request(_args(_worker_argv(tmp_path)), tmp_path)
    assert store.register(validated, "recruiter-pane").pruned is True
    launches: list[str] = []
    monkeypatch.setattr(public_api, "_cockpit_pane", lambda: "recruiter-pane")
    monkeypatch.setattr(
        public_api.recruiter,
        "cmd_request_strict",
        lambda order, _roster: launches.append(order) or 0,
    )
    assert public_api.execute(_args(_worker_argv(tmp_path)), tmp_path) == 1
    assert launches == []
    capsys.readouterr()
    changed_argv = _worker_argv(tmp_path)
    _prompt(tmp_path, "changed immutable prompt\n")
    changed = public_api.validate_request(_args(changed_argv), tmp_path)
    with pytest.raises(public_api.PublicError, match="request_id_conflict"):
        store.register(changed, "recruiter-pane")

    assert (
        public_api.execute(
            _args(
                [
                    "cancel",
                    "--request",
                    REQUEST_ID,
                    "--control-token-file",
                    _control_token_file(tmp_path, control_token),
                    "--json",
                ]
            ),
            tmp_path,
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["state"]["state"] == "pruned"


def test_cleanup_recovers_after_private_tombstone_commits_before_public_store(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, registered, ledger, _token, _control_token = _finish_registered_request(
        tmp_path, monkeypatch
    )
    key = ledger.key(REQUEST_ID)
    payload_sha256 = registered.record["payload_sha256"]
    evidence = ledger.terminal_cleanup_evidence(key, REQUEST_ID, payload_sha256)
    terminal_at_ns = evidence["terminal_at_ns"]
    assert isinstance(terminal_at_ns, int)
    tombstone = public_api._cleanup_tombstone(
        registered,
        public_api._public_status(store, registered),
        evidence,
        terminal_at_ns + 1,
    )

    ledger.prune_terminal(
        key,
        REQUEST_ID,
        payload_sha256,
        tombstone,
        verify_absence=recruiter._verify_terminal_cleanup_absence,
    )

    assert (registered.request_dir / "request.json").is_file()
    assert public_api._public_status(store, registered)["state"]["state"] == "pruned"
    recovered = public_api._cleanup_one(
        store,
        REQUEST_ID,
        older_than_seconds=0,
        apply=True,
        now_ns=terminal_at_ns + 2,
    )
    assert recovered["status"] == "cleaned"
    assert {path.name for path in registered.request_dir.iterdir()} == {
        "tombstone.json"
    }
    assert {path.name for path in ledger.request_dir(key).iterdir()} == {
        "tombstone.json"
    }


def test_cleanup_refuses_active_cleanup_failed_and_batch_skips_malformed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    store, _registered, active_ledger, _token, _control = _registered_request(
        tmp_path, monkeypatch
    )
    active_ledger._snapshot(
        active_ledger.key(REQUEST_ID), "awaiting-requester", decision_nonce="nonce"
    )
    with pytest.raises(recruiter.RecruiterError, match="awaiting-requester"):
        public_api._cleanup_one(
            store,
            REQUEST_ID,
            older_than_seconds=0,
            apply=False,
            now_ns=10**20,
        )

    second = "01957f4e-7f7f-7f8b-9c42-6e7f52f9321b"
    second_root = tmp_path / "second"
    second_root.mkdir()
    args = _worker_argv(second_root, request_id=second)
    validated = public_api.validate_request(_args(args), second_root)
    registered = store.register(validated, "recruiter-pane")
    order = recruiter.load_order(registered.order_path)
    ledger = recruiter.JobLedger()
    key, _ = ledger.submit(order)
    token = ledger.claim(
        key,
        order["order_id"],
        60_000,
        owner={
            "generation": 1,
            "herdr_session": "test",
            "request_id": second,
            "runner_pid": -1,
        },
    )
    assert isinstance(token, str)
    manifest = recruiter.completion.build_manifest(
        order, ledger.request_dir(key), token, second
    )
    recruiter.completion.write_manifest(
        ledger.request_dir(key) / "artifact-manifest.json", manifest
    )
    result = recruiter._write_required_blocked_bundle(order, manifest, "failed cleanup")
    assert (
        ledger.finalize(
            key,
            token,
            order,
            result,
            cleanup={"status": "cleanup-failed", "verified_absent": False},
        )
        is not None
    )
    with pytest.raises(recruiter.RecruiterError, match="cleanup-failed"):
        public_api._cleanup_one(
            store, second, older_than_seconds=0, apply=False, now_ns=10**20
        )

    malformed = "01957f4e-7f7f-7f8b-9c42-6e7f52f9321c"
    (store.path(malformed)).mkdir(parents=True)
    (store.path(malformed) / "request.json").write_text("not-json")
    assert (
        public_api._cleanup(_args(["cleanup", "--all-terminal", "--json"]), store) == 0
    )
    rows = json.loads(capsys.readouterr().out)
    by_id = {row["request_id"]: row for row in rows}
    assert by_id[REQUEST_ID]["status"] == "skipped"
    assert by_id[second]["status"] == "skipped"
    assert by_id[malformed]["status"] == "skipped"


def test_missing_service_state_self_heals_without_prior_up(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    panes = iter((None, "created-service-pane"))
    starts: list[str] = []
    monkeypatch.setattr(
        public_api.recruiter, "_recruiter_pane_from_state", lambda: next(panes)
    )
    monkeypatch.setattr(
        public_api.recruiter,
        "cmd_up",
        lambda roster: starts.append(roster) or 0,
    )

    assert public_api._cockpit_pane() == "created-service-pane"
    assert starts == [str(public_api.HERE / "offerings.yaml")]


# --- Cursor default-effort canonicalization -----------------------------------


def _cursor_argv(tmp_path: Path, *, effort: str | None = None) -> list[str]:
    argv = [
        "request",
        "--type",
        "worker",
        "--request-id",
        REQUEST_ID,
        "--offering",
        "cursor-composer-2-5",
        "--agent",
        "backend",
        "--prompt-file",
        str(_prompt(tmp_path)),
        "--cwd",
        str(tmp_path),
    ]
    if effort is not None:
        argv += ["--effort", effort]
    return argv


def _persona(tmp_path: Path) -> None:
    persona_dir = tmp_path / ".claude/agents"
    persona_dir.mkdir(parents=True, exist_ok=True)
    (persona_dir / "backend.md").write_text("---\nname: backend\n---\n")


def test_cursor_omitted_and_explicit_default_are_canonical(tmp_path: Path) -> None:
    _persona(tmp_path)

    omitted = public_api.validate_request(_args(_cursor_argv(tmp_path)), tmp_path)
    explicit = public_api.validate_request(
        _args(_cursor_argv(tmp_path, effort="default")), tmp_path
    )

    assert omitted.payload == explicit.payload
    assert omitted.payload_sha256 == explicit.payload_sha256
    assert omitted.payload["effort"] == "default"
    assert omitted.payload["offering_snapshot"]["selected_effort"] == "default"


def test_cursor_rejects_every_global_effort(tmp_path: Path) -> None:
    _persona(tmp_path)

    for effort in ("low", "medium", "high", "xhigh", "max"):
        with pytest.raises(public_api.PublicError, match="does not allow effort"):
            public_api.validate_request(
                _args(_cursor_argv(tmp_path, effort=effort)), tmp_path
            )


def test_effortful_offering_rejects_omitted_effort(tmp_path: Path) -> None:
    _persona(tmp_path)
    argv = _cursor_argv(tmp_path)
    argv[argv.index("cursor-composer-2-5")] = "pi-gpt-5-6-sol"

    with pytest.raises(public_api.PublicError, match="requires an explicit effort"):
        public_api.validate_request(_args(argv), tmp_path)
