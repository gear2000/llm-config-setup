"""Hermetic resident lifecycle checks: no Herdr, provider, or home-state access."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import multiprocessing
import os
import re
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

HERE = Path(__file__).resolve().parent


def load(name):
    spec = importlib.util.spec_from_file_location(name, HERE / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class BackendError(RuntimeError):
    pass


class FakeRecruiter:
    RecruiterError = BackendError
    EXPECTED_HARNESS_AGENT = {"pi": "pi"}
    EXPECTED_HARNESS_PROCESS = {"pi": "pi"}
    CURSOR_PROMPT_PASTE_SETTLE_SECONDS = 0.5

    def __init__(self, tmp):
        self.tmp = tmp
        self.agents = {}
        self.starts = 0
        self.closes = []
        self.prompts = []
        self.acknowledge = True
        self.answer = True
        # "success" | "failure" | "mismatched" | "invalid" (well-formed JSON, empty
        # citations -- rejected by the answer schema) | "malformed" (not JSON at all)
        self.answer_mode = "success"
        self.available = True
        # B2: delivery starts (the prompt is recorded) but the send itself then fails --
        # UNCERTAIN, not known non-delivery.
        self.fail_send = False
        self.contracts_consult = load("contracts_consult")
        self.entry = {
            "name": "advisor",
            "agent": "advisor",
            "location": "",
            "repo_root": tmp,
            "effort": "high",
            "offering": "fake",
            "offering_snapshot": {"harness": "pi"},
        }
        self.offering_catalog = SimpleNamespace(
            render_shell=lambda snap, agent, prompt: prompt,
            preflight_snapshot=lambda snapshot: None,
        )

    def JobLedger(self):
        return SimpleNamespace(root=self.tmp / "runtime" / "ledger")

    def load_specialist_roster(self):
        return {}

    def _specialist_index(self, roster):
        index = {"advisor": self.entry}
        other = getattr(self, "other_entry", None)
        if other:
            index["other"] = other
        return index

    def consult_request_id(self, consult_id):
        return "consult-" + hashlib.sha256(consult_id.encode()).hexdigest()[:24]

    def _resolve_current_herdr_session_name(self):
        return "test-session"

    def _recruiter_pane_from_state(self):
        return "caller"

    def _process_start_time(self, pid):
        return "birth-" + str(pid) if isinstance(pid, int) else None

    def _matching_intake_process(self, info, expected):
        return next(
            (p for p in info["foreground_processes"] if p["name"] == expected), None
        )

    def _herdr_json(self, *args, herdr_session, timeout_seconds):
        assert herdr_session == "test-session"
        # Launch and delivery have finite bounds; observations use 15 seconds.
        if args[:2] == ("agent", "start"):
            assert 0 < timeout_seconds <= 180
        else:
            assert timeout_seconds == 15
        if not self.available:
            raise BackendError("backend unavailable")
        if args[:2] == ("agent", "start"):
            name = args[2]
            self.starts += 1
            pane = "pane-" + str(self.starts)
            cwd = args[args.index("--cwd") + 1]
            self.agents[name] = {
                "name": name,
                "pane_id": pane,
                "cwd": cwd,
                "pid": 100 + self.starts,
                "status": "idle",
            }
            prompt = Path(args[-1])
            if self.acknowledge:
                (prompt.parent / "ready.json").write_text(
                    json.dumps({"generation": name[5:], "ready": True})
                )
            return {"result": {"agent": self.agents[name]}}
        if args[:2] == ("agent", "get"):
            if args[2] not in self.agents:
                raise BackendError("agent_not_found")
            return {"result": {"agent": self.agents[args[2]]}}
        if args[:2] == ("pane", "get") and args[2] == "caller":
            return {"result": {"pane": {"tab_id": "tab-test"}}}
        pane_id = args[3] if args[:2] == ("pane", "process-info") else args[2]
        agent = next((a for a in self.agents.values() if a["pane_id"] == pane_id), None)
        if agent is None:
            raise BackendError("pane_not_found")
        if args[:2] == ("pane", "get"):
            return {
                "result": {
                    "pane": {
                        "pane_id": pane_id,
                        "agent": "pi",
                        "cwd": agent["cwd"],
                        "agent_status": agent["status"],
                    }
                }
            }
        if args[:2] == ("pane", "process-info"):
            return {
                "result": {
                    "process_info": {
                        "foreground_processes": [{"name": "pi", "pid": agent["pid"]}]
                    }
                }
            }
        if args[:2] == ("pane", "close"):
            self.closes.append(pane_id)
            del self.agents[agent["name"]]
            return {"result": {}}
        if args[:2] == ("pane", "run"):
            message = args[3]
            self.prompts.append(message)
            if self.fail_send:
                # Delivery genuinely started (the prompt is recorded above) but the send
                # command itself then fails -- B2's UNCERTAIN case.
                raise BackendError("send failed after delivery started")
            if self.answer:
                # M1: delivery is a one-line pointer, not the question inline. Read the
                # private per-turn file it names, the same way a real resident would.
                question_path = Path(
                    re.search(
                        r"^Read (.+) and answer exactly as it instructs\.$", message
                    ).group(1)
                )
                question_text = question_path.read_text()
                path = Path(
                    re.search(r"JSON to (.*?) with this shape:", question_text).group(1)
                )
                if self.answer_mode == "malformed":
                    path.write_text("{not valid json")
                    return {"result": {}}
                shape = json.JSONDecoder().raw_decode(
                    question_text.split("with this shape: ")[1]
                )[0]
                if self.answer_mode == "failure":
                    consult_id = shape["answer"]["consult_id"]
                    shape["answer"] = {
                        "consult_id": consult_id,
                        "error": "could not determine from current source",
                    }
                elif self.answer_mode == "mismatched":
                    shape["turn"] = "not-the-turn-that-was-sent"
                    shape["answer"]["answer"] = "Current source says yes."
                    shape["answer"]["citations"] = ["source.py:1"]
                elif self.answer_mode == "invalid":
                    # Well-formed JSON, but the answer schema itself rejects it (no citations).
                    shape["answer"]["answer"] = "Current source says yes."
                    shape["answer"]["citations"] = []
                else:
                    shape["answer"]["answer"] = "Current source says yes."
                    shape["answer"]["citations"] = ["source.py:1"]
                path.write_text(json.dumps(shape))
            return {"result": {}}
        raise AssertionError(args)

    def _herdr(self, *args, herdr_session, timeout_seconds):
        # Herdr delivery commands succeed with empty stdout; the fake still models
        # their side effects and failures through the same in-memory backend.
        assert timeout_seconds == 15
        FakeRecruiter._herdr_json(
            self, *args, herdr_session=herdr_session, timeout_seconds=timeout_seconds
        )


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    module = load("specialist_lifecycle")
    backend = FakeRecruiter(tmp_path)
    module._bind_recruiter_runtime(backend)
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("HERDR_PANE_ID", raising=False)
    return module, backend, tmp_path


def start(runtime):
    module, backend, cwd = runtime
    module.main(["up", "advisor"])
    directory = module._directory("advisor", cwd)
    return directory, module._read(directory / "state.json")


def test_up_idempotent_and_real_ack(runtime):
    module, backend, cwd = runtime
    directory, state = start(runtime)
    module.main(["up", "advisor"])
    assert backend.starts == 1
    assert module._read(directory / "state.json")["launched_at"] == state["launched_at"]
    assert state["state"] == "ready" and state["pid"] == 101
    assert (directory.stat().st_mode & 0o777) == 0o700
    assert ((directory / "state.json").stat().st_mode & 0o777) == 0o600


@pytest.mark.parametrize(
    "age,expired", [(7199, False), (7200, True), (7201, True), (-1, True)]
)
def test_fixed_two_hour_boundary(runtime, monkeypatch, age, expired):
    module, _, _ = runtime
    monkeypatch.setattr(module.time, "time", lambda: 10000)
    monkeypatch.setattr(module.time, "monotonic", lambda: 10000)
    assert (
        module._expired({"launched_at": 10000 - age, "launched_monotonic": 10000 - age})
        is expired
    )


def test_refresh_before_worker_and_disabled_stays_down(runtime):
    module, backend, cwd = runtime
    directory, state = start(runtime)
    state["launched_at"] -= 7200
    module._save(directory, state)
    module.preflight(cwd, "caller")
    assert backend.starts == 2 and backend.closes == ["pane-1"]
    module.main(["down", "advisor"])
    module.preflight(cwd, "caller")
    assert backend.starts == 2
    assert not module.enabled("advisor", cwd)


def test_restart_closes_before_new_start(runtime):
    module, backend, _ = runtime
    _, old = start(runtime)
    module.main(["restart", "advisor"])
    assert backend.starts == 2 and backend.closes == [old["pane"]]


def test_missing_resident_replaced(runtime):
    module, backend, cwd = runtime
    start(runtime)
    backend.agents.clear()
    module.preflight(cwd, "caller")
    assert backend.starts == 2


def test_contexts_do_not_share(runtime, tmp_path):
    module, backend, cwd = runtime
    start(runtime)
    other = tmp_path / "other"
    other.mkdir()
    module.preflight(other, "caller")
    assert not module.enabled("advisor", other)
    assert module.consult({"specialist": "advisor", "cwd": str(other)}) is None
    assert backend.starts == 1


def test_instruction_change_refreshes(runtime):
    module, backend, cwd = runtime
    start(runtime)
    (cwd / "AGENTS.md").write_text("Changed instructions")
    module.preflight(cwd, "caller")
    assert backend.starts == 2


def test_bad_readiness_blocks_without_duplicate(runtime, monkeypatch):
    module, backend, cwd = runtime
    backend.acknowledge = False
    monkeypatch.setattr(module, "START_SECONDS", 0.01)
    with pytest.raises(module.SpecialistError, match="did not load context"):
        start(runtime)
    state = module._read(module._directory("advisor", cwd) / "state.json")
    assert state["state"] == "starting"
    assert backend.starts == 1


def test_unavailable_backend_blocks_and_retains_ownership(runtime):
    module, backend, cwd = runtime
    directory, old = start(runtime)
    backend.available = False
    with pytest.raises(BackendError, match="unavailable"):
        module.preflight(cwd, "caller")
    assert module._read(directory / "state.json")["generation"] == old["generation"]
    assert backend.starts == 1


def test_pid_replacement_never_closed_or_reused(runtime):
    module, backend, cwd = runtime
    _, old = start(runtime)
    backend.agents[old["agent_name"]]["pid"] = 999
    with pytest.raises(module.SpecialistError, match="foreground process was replaced"):
        module.main(["restart", "advisor"])
    assert not backend.closes
    assert backend.starts == 1


def test_two_questions_reuse_with_distinct_turns(runtime):
    module, backend, cwd = runtime
    directory, state = start(runtime)
    for question_id in ("first", "second"):
        answer = module.consult(
            {
                "specialist": "advisor",
                "cwd": str(cwd),
                "consult_id": question_id,
                "question": "What is current?",
            }
        )
        assert answer["consult_id"] == question_id
    assert backend.starts == 1 and not backend.closes
    assert len(backend.prompts) == 2 and backend.prompts[0] != backend.prompts[1]
    # M1: delivery is a one-line pointer to a private per-turn file, never the question
    # pasted inline into a live TUI turn.
    for prompt in backend.prompts:
        assert "What is current?" not in prompt
        match = re.match(r"^Read (.+) and answer exactly as it instructs\.$", prompt)
        assert match
        question_path = Path(match.group(1))
        assert question_path.stat().st_mode & 0o777 == 0o600
        assert "What is current?" in question_path.read_text()
    assert module._read(directory / "state.json")["state"] == "ready"


def test_delivery_uses_the_non_json_herdr_command(runtime, monkeypatch):
    module, backend, cwd = runtime
    start(runtime)
    original = backend._herdr_json

    def query_only(*args, **kwargs):
        if args[:2] == ("pane", "run"):
            raise AssertionError("pane run prints no JSON on success")
        return original(*args, **kwargs)

    monkeypatch.setattr(backend, "_herdr_json", query_only)
    answer = module.consult(
        {
            "specialist": "advisor",
            "cwd": str(cwd),
            "consult_id": "silent-send",
            "question": "What is current?",
        }
    )
    assert answer["consult_id"] == "silent-send"
    assert len(backend.prompts) == 1


def test_stale_generation_answer_rejected(runtime):
    module, _, _ = runtime
    with pytest.raises(module.SpecialistError, match="stale generation"):
        module._validated_answer(
            {"generation": "new"}, {"id": "turn"}, {"generation": "old"}
        )


def test_invalid_answer_records_failure_and_self_heals(runtime):
    module, backend, cwd = runtime
    directory, state = start(runtime)
    backend.answer_mode = "invalid"  # well-formed JSON, but empty `citations`
    with pytest.raises(module.SpecialistError, match="failed validation"):
        module.consult(
            {
                "specialist": "advisor",
                "cwd": str(cwd),
                "consult_id": "bad",
                "question": "What is current?",
            }
        )
    saved = module._read(directory / "state.json")
    assert saved["state"] == "ready"
    assert saved["turn"]["outcome"] == "failed"
    assert backend.starts == 1  # the same resident instance, never relaunched
    # The very next call -- even a background preflight -- finds it reusable.
    module.preflight(cwd, "caller")
    assert backend.starts == 1
    backend.answer_mode = "success"
    answer = module.consult(
        {
            "specialist": "advisor",
            "cwd": str(cwd),
            "consult_id": "good",
            "question": "What is current?",
        }
    )
    assert answer["consult_id"] == "good"


def test_permanently_malformed_response_resolves_as_failed_not_wedged(runtime):
    module, backend, cwd = runtime
    directory, _ = start(runtime)
    backend.answer_mode = "malformed"  # not JSON at all, and never becomes valid
    started = time.monotonic()
    with pytest.raises(module.SpecialistError, match="failed validation"):
        module.consult(
            {
                "specialist": "advisor",
                "cwd": str(cwd),
                "consult_id": "bad",
                "question": "What is current?",
            }
        )
    elapsed = time.monotonic() - started
    # Actually waited out the settle window (never judged on the very first read) but
    # bounded by it, not the full ten-minute turn deadline.
    assert (
        module.RESPONSE_SETTLE_SECONDS <= elapsed < module.RESPONSE_SETTLE_SECONDS + 2
    )
    assert module._read(directory / "state.json")["state"] == "ready"


def test_torn_file_while_pane_still_working_is_never_judged(runtime, monkeypatch):
    """H1's actual bug: a partial/torn response file must never be read at all while the
    pane is still working, let alone judged as a failed answer -- only once it goes idle."""
    module, backend, cwd = runtime
    directory, state = start(runtime)
    response_path = directory / state["generation"] / "turn.json"
    response_path.write_text("{not valid json yet")  # a torn, in-progress write
    turn = {
        "id": "turn",
        "consult_id": "q",
        "response": str(response_path),
        "deadline": time.time() + 30,
    }
    state.update(state="busy", turn=turn)
    module._save(directory, state)
    real_identity = module._identity
    calls = {"n": 0}

    def flaky_identity(current_state, *, initial=False):
        calls["n"] += 1
        result = real_identity(current_state, initial=initial)
        if result is None:
            return None
        if calls["n"] < 3:
            # The pane is still working: the torn file above must never be read here.
            return {**result, "idle": False}
        # Only now does the resident finish its write and go idle.
        response_path.write_text(
            json.dumps(
                {
                    "generation": state["generation"],
                    "turn": "turn",
                    "answer": {
                        "consult_id": "q",
                        "answer": "ok",
                        "citations": ["a.py:1"],
                    },
                }
            )
        )
        return result

    monkeypatch.setattr(module, "_identity", flaky_identity)
    reason = module._finish_busy(directory, state)
    assert reason is None
    assert calls["n"] >= 3  # it really waited through the not-idle calls
    assert module._read(directory / "state.json")["state"] == "ready"


def test_single_torn_read_while_idle_does_not_fail_the_turn(runtime, monkeypatch):
    """One torn read while already idle must not itself fail the turn -- only a read (or a
    validation failure) that stays torn/invalid across the full settle window does."""
    module, backend, cwd = runtime
    directory, state = start(runtime)
    response_path = directory / state["generation"] / "turn.json"
    response_path.write_text("{not valid json yet")
    turn = {
        "id": "turn",
        "consult_id": "q",
        "response": str(response_path),
        "deadline": time.time() + 30,
    }
    state.update(state="busy", turn=turn)
    module._save(directory, state)
    real_identity = module._identity
    calls = {"n": 0}

    def fix_on_second_call(current_state, *, initial=False):
        calls["n"] += 1
        if calls["n"] == 2:
            response_path.write_text(
                json.dumps(
                    {
                        "generation": state["generation"],
                        "turn": "turn",
                        "answer": {
                            "consult_id": "q",
                            "answer": "ok",
                            "citations": ["a.py:1"],
                        },
                    }
                )
            )
        return real_identity(current_state, initial=initial)

    monkeypatch.setattr(module, "_identity", fix_on_second_call)
    reason = module._finish_busy(directory, state)
    assert reason is None
    assert (
        calls["n"] == 2
    )  # settled well within the settle window, on the very next poll
    assert module._read(directory / "state.json")["state"] == "ready"


def test_uncertain_send_failure_stays_busy_and_never_redelivers(runtime):
    """B2: a `RecruiterError` from the send call itself, after delivery has already started,
    is UNCERTAIN, not known non-delivery -- stay `busy` and let the deadline/idle policy
    resolve it, and never send the same question twice."""
    module, backend, cwd = runtime
    directory, _ = start(runtime)
    backend.fail_send = True
    with pytest.raises(BackendError, match="send failed"):
        module.consult(
            {
                "specialist": "advisor",
                "cwd": str(cwd),
                "consult_id": "q1",
                "question": "What is current?",
                "timeout_seconds": 0.01,
            }
        )
    saved = module._read(directory / "state.json")
    assert (
        saved["state"] == "busy"
    )  # uncertain delivery stays fail-closed, not rolled back
    assert len(backend.prompts) == 1
    first_question = re.match(r"^Read (.+) and answer", backend.prompts[0]).group(1)

    # Once the deadline passes and the pane is verifiably idle, the turn is abandoned --
    # never silently retried -- and the next question gets its own new turn.
    backend.fail_send = False
    answer = module.consult(
        {
            "specialist": "advisor",
            "cwd": str(cwd),
            "consult_id": "q2",
            "question": "What is current now?",
        }
    )
    assert answer["consult_id"] == "q2"
    assert len(backend.prompts) == 2
    second_question = re.match(r"^Read (.+) and answer", backend.prompts[1]).group(1)
    assert second_question != first_question  # never delivered twice on the same id
    assert backend.starts == 1  # the same resident instance, never relaunched


def test_busy_turn_pane_gone_via_preflight_recycles_without_closing(runtime):
    """M2: a pane proven gone (not merely idle-past-deadline) can be stopped safely and
    recycled even while a turn was in flight -- there is nothing left to interrupt or close."""
    module, backend, cwd = runtime
    directory, state = start(runtime)
    state.update(
        state="busy",
        turn={
            "id": "unfinished",
            "consult_id": "q",
            "response": str(directory / "never.json"),
            "deadline": time.time() + 999,
        },
    )
    module._save(directory, state)
    backend.agents.clear()  # the pane, and its process, are simply gone
    module.preflight(cwd, "caller")
    assert backend.starts == 2
    assert not backend.closes  # nothing was left to close


def test_pre_delivery_refusal_rolls_back_known_non_delivery(runtime, monkeypatch):
    module, backend, cwd = runtime
    directory, _ = start(runtime)
    real_identity = module._identity
    calls = {"n": 0}

    def flaky_identity(state, *, initial=False):
        calls["n"] += 1
        result = real_identity(state, initial=initial)
        # The reuse check inside `_ensure` (call 1) still finds it idle; only the
        # pre-delivery check consult() itself makes (call 2) sees it not-idle.
        if calls["n"] == 2 and result is not None:
            result = {**result, "idle": False}
        return result

    monkeypatch.setattr(module, "_identity", flaky_identity)
    with pytest.raises(module.SpecialistError, match="not ready for delivery"):
        module.consult(
            {
                "specialist": "advisor",
                "cwd": str(cwd),
                "consult_id": "q1",
                "question": "What is current?",
            }
        )
    assert not backend.prompts  # nothing was ever sent to the pane
    saved = module._read(directory / "state.json")
    assert saved["state"] == "ready"
    assert "turn" not in saved
    monkeypatch.setattr(module, "_identity", real_identity)
    answer = module.consult(
        {
            "specialist": "advisor",
            "cwd": str(cwd),
            "consult_id": "q2",
            "question": "What is current?",
        }
    )
    assert answer["consult_id"] == "q2"
    assert backend.prompts  # the second question really was delivered


def test_abandoned_idle_turn_self_heals_and_down_closes_in_one_call(runtime):
    module, backend, _ = runtime
    directory, state = start(runtime)
    state.update(
        state="busy",
        turn={
            "id": "unfinished",
            "consult_id": "q",
            "response": str(directory / "never.json"),
            "deadline": time.time() - 1,
        },
    )
    module._save(directory, state)
    # The pane never left idle, so once the deadline has passed the turn is recoverable:
    # `down` both records the abandonment and completes cleanup in this one call.
    module.main(["down", "advisor"])
    assert backend.closes == [state["pane"]]
    saved = module._read(directory / "state.json")
    assert saved["state"] == "stopped" and not saved["enabled"]
    assert saved["turn"]["outcome"] == "abandoned"


def test_active_unresolved_turn_still_blocks_and_is_never_interrupted(
    runtime, monkeypatch
):
    module, backend, _ = runtime
    directory, state = start(runtime)
    state.update(
        state="busy",
        turn={
            "id": "unfinished",
            "consult_id": "q",
            "response": str(directory / "never.json"),
            "deadline": time.time() - 1,
        },
    )
    module._save(directory, state)
    real_identity = module._identity

    def still_working(state, *, initial=False):
        result = real_identity(state, initial=initial)
        return {**result, "idle": False} if result else result

    monkeypatch.setattr(module, "_identity", still_working)
    with pytest.raises(module.SpecialistError, match="unresolved"):
        module.main(["down", "advisor"])
    assert not backend.closes
    saved = module._read(directory / "state.json")
    assert (
        saved["state"] == "busy"
    )  # no autonomous action while the pane is still active
    assert not saved["enabled"]


def test_second_consult_after_abandoned_turn_uses_a_new_turn(runtime):
    module, backend, cwd = runtime
    directory, state = start(runtime)
    stale_response = directory / state["generation"] / "unfinished.json"
    state.update(
        state="busy",
        turn={
            "id": "unfinished",
            "consult_id": "q",
            "response": str(stale_response),
            "deadline": time.time() - 1,
        },
    )
    module._save(directory, state)
    module._finish_busy(directory, state)
    assert module._read(directory / "state.json")["state"] == "ready"
    answer = module.consult(
        {
            "specialist": "advisor",
            "cwd": str(cwd),
            "consult_id": "fresh",
            "question": "What is current?",
        }
    )
    assert answer["consult_id"] == "fresh"
    new_turn = module._read(directory / "state.json")["turn"]
    assert new_turn["id"] != "unfinished"
    assert new_turn["response"] != str(stale_response)
    assert backend.starts == 1  # the same resident instance, never relaunched


def test_preflight_skips_locked_resident_without_blocking(runtime):
    module, backend, cwd = runtime
    directory, state = start(runtime)
    context = multiprocessing.get_context("fork")
    ready, release = context.Event(), context.Event()
    process = context.Process(
        target=_hold_lock, args=(str(directory / "lock"), ready, release)
    )
    process.start()
    try:
        assert ready.wait(3)
        started = time.monotonic()
        module.preflight(cwd, "caller")  # must return promptly, not wait or raise
        assert time.monotonic() - started < 2
    finally:
        release.set()
        process.join(3)
    assert backend.starts == 1  # untouched while locked
    # Once the lock is free, an ordinary preflight still rotates an expired resident.
    state["launched_at"] -= 7200
    module._save(directory, state)
    module.preflight(cwd, "caller")
    assert backend.starts == 2


def test_legacy_consult_cold_target_unaffected_by_wedged_resident(runtime):
    module, backend, cwd = runtime
    directory, state = start(runtime)
    backend.other_entry = {**backend.entry, "name": "other", "agent": "other"}
    # advisor is genuinely busy with a turn that will not resolve for a long time -- the
    # exact scenario B3 fixed: an ordinary question for an unrelated specialist must not
    # depend on this resident's health, or its lock, at all.
    state.update(
        state="busy",
        turn={
            "id": "stuck",
            "consult_id": "q",
            "response": str(directory / "never.json"),
            "deadline": time.time() + 999,
        },
    )
    module._save(directory, state)
    _wire_legacy_consult(module, backend, cwd)
    request, _answer = _consult_request(
        cwd, specialist="other", requested_by="worker-1"
    )
    started = time.monotonic()
    assert module.legacy_consult(str(request)) is None
    assert time.monotonic() - started < 2  # never touched advisor's stuck turn or lock
    assert not backend.prompts
    assert module._read(directory / "state.json")["state"] == "busy"


def test_lost_launch_reply_does_not_duplicate(runtime):
    module, backend, cwd = runtime
    directory, state = start(runtime)
    state.update(state="starting", pane=None)
    backend.agents.clear()
    module._save(directory, state)
    with pytest.raises(module.SpecialistError, match="launch outcome uncertain"):
        module.preflight(cwd, "caller")
    assert backend.starts == 1


def test_lost_launch_reply_adopts_live_agent_and_recycles(runtime):
    module, backend, cwd = runtime
    directory, state = start(runtime)
    # Generation 1's OWN launch reply is lost: the journal never advanced past "starting"
    # even though the agent it names is genuinely live under its unique warm-<generation>
    # name (exactly what `agent start`'s removed client-side timeout used to risk).
    orphan = {**state, "state": "starting", "pane": None}
    orphan.pop("pid", None)
    orphan.pop("birth", None)
    module._save(directory, orphan)
    module.preflight(cwd, "caller")
    # Adopted the real agent (never rejected it as "replaced"), safely closed it once
    # verified idle, and launched exactly one fresh replacement -- never a duplicate.
    assert backend.closes == [state["pane"]]
    assert backend.starts == 2


def test_lost_launch_reply_wrong_cwd_is_rejected_not_adopted(runtime, tmp_path):
    module, backend, cwd = runtime
    directory, state = start(runtime)
    other_cwd = tmp_path / "other"
    other_cwd.mkdir()
    backend.agents[state["agent_name"]]["cwd"] = str(other_cwd)
    orphan = {**state, "state": "starting", "pane": None}
    orphan.pop("pid", None)
    orphan.pop("birth", None)
    module._save(directory, orphan)
    with pytest.raises(module.SpecialistError, match="cwd does not match"):
        module.preflight(cwd, "caller")
    assert not backend.closes
    assert backend.starts == 1


def test_time_flag_rejected(runtime):
    module, backend, _ = runtime
    with pytest.raises(SystemExit) as error:
        module.main(["up", "advisor", "--hours", "4"])
    assert error.value.code == 2 and backend.starts == 0


def test_output_does_not_chmod_caller_directory(runtime):
    module, _, cwd = runtime
    os.chmod(cwd, 0o755)
    module._write(cwd / "answer.json", {"ok": True})
    assert cwd.stat().st_mode & 0o777 == 0o755


def _wire_legacy_consult(module, backend, cwd):
    """Route `legacy_consult`'s roster/cwd/receipt-destination calls to fakes. Whether a receipt
    like this actually gets INDEXED (and later verified) is production `recruiter.py` logic
    (`_record_consult_in_index` / `_recorded_consult` / `resolve_consult_claims`), exercised
    against the real module in `recruiter_test.py`; here the fake just persists what
    `legacy_consult` decided to publish, so these tests can assert on the receipt's own fields."""
    backend._resolve_specialist_name = lambda name, names: (name, None)
    backend._resolve_consult_cwd = lambda entry, consult, cid: str(cwd)
    backend.consult_artifact_paths = lambda path: {
        "receipt": Path(path + ".receipt.json")
    }
    backend._require_consult_receipt_destination = lambda path: None
    backend._publish_consult_receipt = lambda receipt, path: module._write(
        path, receipt
    )


def _consult_request(cwd, **over):
    answer = cwd / "answer.json"
    request = cwd / "consult.json"
    payload = {
        "consult_id": "q1",
        "specialist": "advisor",
        "question": "What source?",
        "answer_path": str(answer),
        **over,
    }
    request.write_text(json.dumps(payload))
    return request, answer


def test_legacy_consult_reuses_and_publishes_answer(runtime):
    module, backend, cwd = runtime
    start(runtime)
    _wire_legacy_consult(module, backend, cwd)
    request, answer = _consult_request(cwd)
    assert module.legacy_consult(str(request)) == 0
    assert json.loads(answer.read_text())["consult_id"] == "q1"
    assert (
        json.loads(Path(str(request) + ".receipt.json").read_text())["resident"] is True
    )
    assert backend.starts == 1 and not backend.closes
    module.main(["down", "advisor"])
    assert module.legacy_consult(str(request)) is None


def test_legacy_consult_indexes_a_real_answer_for_its_requester(runtime):
    """A successfully executed resident turn produces a truthful, indexable receipt: the exact
    generation and turn that answered, never a synthesized Recruiter `order_receipt_state`, and
    the request id the ordinary consult-index reader expects."""
    module, backend, cwd = runtime
    _, state = start(runtime)
    _wire_legacy_consult(module, backend, cwd)
    request, answer = _consult_request(cwd, requested_by="worker-order-1")

    assert module.legacy_consult(str(request)) == 0

    receipt = json.loads(Path(str(request) + ".receipt.json").read_text())
    assert receipt["answer_verdict"] == "cited"
    assert receipt["resident_turn_state"] == "finished"
    assert "order_receipt_state" not in receipt
    assert receipt["generation"] == state["generation"]
    assert receipt["turn"]  # the exact turn id that was delivered
    assert receipt["request_id"] == backend.consult_request_id("q1")
    assert receipt["requested_by"] == "worker-order-1"


def test_legacy_consult_specialist_signaled_failure_still_indexed(runtime):
    """The specialist genuinely ran and signaled failure via the answer envelope — this is a
    real completed turn, distinct from a pre-run rejection, and must stay verifiable."""
    module, backend, cwd = runtime
    _, state = start(runtime)
    backend.answer_mode = "failure"
    _wire_legacy_consult(module, backend, cwd)
    request, answer = _consult_request(cwd, requested_by="worker-order-1")

    assert module.legacy_consult(str(request)) == 0

    assert json.loads(answer.read_text())["error"]
    receipt = json.loads(Path(str(request) + ".receipt.json").read_text())
    assert receipt["answer_verdict"] == "failed"
    assert receipt["resident_turn_state"] == "finished"
    assert receipt["generation"] == state["generation"]


def test_legacy_consult_pre_run_rejection_rolls_back_and_next_consult_succeeds(
    runtime, monkeypatch
):
    """B2: found not idle at the moment of delivery -- the one race `consult` itself guards
    against, now with no unrelated preflight call ahead of it (B3). No turn was ever assigned
    to a live delivery, so nothing here may be mistaken for a completed one, and the rollback
    leaves the resident reusable for the very next question."""
    module, backend, cwd = runtime
    start(runtime)
    _wire_legacy_consult(module, backend, cwd)
    request, answer = _consult_request(cwd, requested_by="worker-order-1")
    real_identity = module._identity
    calls = {"n": 0}

    def flaky_identity(state, *, initial=False):
        calls["n"] += 1
        result = real_identity(state, initial=initial)
        # Call 1 is `_ensure`'s reuse check (still idle); call 2 is `consult`'s own
        # pre-delivery check, exactly the race B2 fixed.
        if calls["n"] >= 2 and result is not None:
            result = {**result, "idle": False}
        return result

    monkeypatch.setattr(module, "_identity", flaky_identity)

    assert module.legacy_consult(str(request)) == 0

    assert json.loads(answer.read_text())["error"]
    receipt = json.loads(Path(str(request) + ".receipt.json").read_text())
    assert "resident_turn_state" not in receipt
    assert "generation" not in receipt and "turn" not in receipt
    assert receipt["answer_verdict"] == "failed"
    assert not backend.prompts  # nothing was ever delivered to the resident
    assert (
        module._read(module._directory("advisor", cwd) / "state.json")["state"]
        == "ready"
    )

    monkeypatch.setattr(module, "_identity", real_identity)
    request2, answer2 = _consult_request(
        cwd, consult_id="q2", requested_by="worker-order-2"
    )
    assert module.legacy_consult(str(request2)) == 0
    assert json.loads(answer2.read_text())["consult_id"] == "q2"
    assert backend.prompts  # the second question really was delivered


def test_legacy_consult_uncompleted_turn_is_not_indexed(runtime):
    """The question was actually delivered to the resident, but no confirmed response ever came
    back within its bound. Delivered-but-unconfirmed must not be mistaken for a completed turn.
    Since the pane never left idle, this now also self-heals (M2) instead of wedging, and a
    second, unrelated question against the same resident still succeeds."""
    module, backend, cwd = runtime
    start(runtime)
    backend.answer = False  # the resident never writes a response
    _wire_legacy_consult(module, backend, cwd)
    request, answer = _consult_request(
        cwd, requested_by="worker-order-1", timeout_seconds=0.01
    )

    assert module.legacy_consult(str(request)) == 0

    assert "no usable answer" in json.loads(answer.read_text())["error"]
    receipt = json.loads(Path(str(request) + ".receipt.json").read_text())
    assert "resident_turn_state" not in receipt
    assert "generation" not in receipt and "turn" not in receipt
    assert receipt["answer_verdict"] == "failed"
    assert backend.prompts  # it really was delivered

    backend.answer = True
    request2, answer2 = _consult_request(
        cwd, consult_id="q2", requested_by="worker-order-2"
    )
    assert module.legacy_consult(str(request2)) == 0
    assert json.loads(answer2.read_text())["consult_id"] == "q2"


def test_legacy_consult_mismatched_answer_is_not_indexed(runtime):
    """A response naming a different turn than the one just delivered is stale or forged, not a
    real answer to this consult, and must never be indexed."""
    module, backend, cwd = runtime
    start(runtime)
    backend.answer_mode = "mismatched"
    _wire_legacy_consult(module, backend, cwd)
    request, answer = _consult_request(cwd, requested_by="worker-order-1")

    assert module.legacy_consult(str(request)) == 0

    assert "stale generation or turn" in json.loads(answer.read_text())["error"]
    receipt = json.loads(Path(str(request) + ".receipt.json").read_text())
    assert "resident_turn_state" not in receipt


def test_legacy_consult_no_requester_is_not_indexed(runtime):
    """`requested_by` is worker-supplied; omitting it must lose attribution, not verifiability
    for someone else. A real turn with no requester stays unindexed, same as the cold path."""
    module, backend, cwd = runtime
    start(runtime)
    _wire_legacy_consult(module, backend, cwd)
    request, _ = _consult_request(cwd)  # no requested_by

    assert module.legacy_consult(str(request)) == 0

    receipt = json.loads(Path(str(request) + ".receipt.json").read_text())
    assert receipt["resident_turn_state"] == "finished"  # it really ran
    assert receipt.get("requested_by") is None


def _simultaneous_start(cwd, gate):
    module = load("specialist_lifecycle")
    backend = FakeRecruiter(Path(cwd))
    module._bind_recruiter_runtime(backend)
    directory = module._directory("advisor", cwd)
    module._identity = lambda state: {"pane": "p", "pid": 1, "birth": "b", "idle": True}

    def record_start(directory, name, cwd, entry, cockpit):
        count = Path(cwd) / "starts"
        count.write_text(str(int(count.read_text()) + 1 if count.exists() else 1))
        state = {
            "name": name,
            "cwd": cwd,
            "state": "ready",
            "enabled": True,
            "context": module._context(entry, cwd)[0],
            "launched_at": time.time(),
            "launched_monotonic": time.monotonic(),
        }
        module._save(directory, state)
        return state

    module._start = record_start
    gate.wait(3)
    with module._lock(directory / "lock"):
        module._ensure(directory, "advisor", cwd, "caller", enable=True)


def test_two_processes_start_only_one_instance(runtime):
    _, _, cwd = runtime
    context = multiprocessing.get_context("fork")
    gate = context.Event()
    processes = [
        context.Process(target=_simultaneous_start, args=(str(cwd), gate))
        for _ in range(2)
    ]
    for process in processes:
        process.start()
    gate.set()
    for process in processes:
        process.join(5)
        assert process.exitcode == 0
    assert (cwd / "starts").read_text() == "1"


def _hold_lock(path, ready, release):
    module = load("specialist_lifecycle")
    with module._lock(Path(path)):
        ready.set()
        release.wait(5)


def test_interprocess_lock_is_bounded(runtime):
    module, _, cwd = runtime
    context = multiprocessing.get_context("fork")
    ready, release = context.Event(), context.Event()
    path = cwd / "lock"
    process = context.Process(target=_hold_lock, args=(str(path), ready, release))
    process.start()
    try:
        assert ready.wait(3)
        with pytest.raises(module.SpecialistError, match="busy"):
            with module._lock(path, timeout=0.01):
                pytest.fail("concurrent owner acquired lock")
    finally:
        release.set()
        process.join(3)
    assert process.exitcode == 0
