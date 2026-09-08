"""Bounded authority tests using real files, flock, and competing processes.

Run: python3 -m pytest .shared-llm/public/extensions/common/upagent/{stall_nudge,nudge_authority}_test.py -q
"""

from __future__ import annotations

import fcntl
import hashlib
import importlib.util
import json
import multiprocessing
import os
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest

HERE = Path(__file__).parent


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


na = load_module("nudge_authority_test_subject", HERE / "nudge_authority.py")
TARGET = na.TargetIdentity(
    "test-session", "workspace-1", "pane-1", "worker", "request", "order-1"
)


def idle(target=TARGET):
    return na.Probe(target, "idle", False, na.PaneState.PRESENT, "claude")


@pytest.fixture
def authority(tmp_path):
    return na.NudgeAuthority(tmp_path / "ledger")


def read_state(authority):
    return json.loads(authority.state_path(TARGET).read_text())


def request(
    authority, payload="continue", trigger="status", probe=idle, deliver=None, seqs=()
):
    if deliver is None:

        def deliver(text):
            return None

    return authority.request_nudge(TARGET, payload, trigger, probe, deliver, seqs)


def test_equal_identities_use_specified_versioned_canonical_hash(authority):
    fields = json.loads(json.dumps(TARGET.canonical(), sort_keys=True))
    del fields["v"]
    independent = na.TargetIdentity(**fields)
    expected = hashlib.sha256(
        json.dumps(TARGET.canonical(), sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    assert independent is not TARGET
    assert independent == TARGET
    assert independent.target_id == TARGET.target_id == expected
    assert authority.state_path(independent) == authority.state_path(TARGET)
    for key in ("herdr_session", "workspace_id", "pane_id", "agent_name", "owner_id"):
        assert replace(TARGET, **{key: "changed"}).target_id != expected
    assert replace(TARGET, owner_kind="flow1-run").target_id != expected


def test_real_client_and_detached_runner_resolve_service_state_file(
    tmp_path, monkeypatch
):
    service_file = tmp_path / "services.json"
    service_file.write_text('{"state":"ready"}')
    monkeypatch.setenv("UPAGENT_STATE", str(service_file))
    monkeypatch.setenv("UPAGENT_RUNTIME_DIR", str(tmp_path / "runtime"))
    monkeypatch.setenv("UPAGENT_CANONICAL_REPO", str(HERE.resolve().parents[4]))
    monkeypatch.delenv("UPAGENT_HUB_DIR", raising=False)
    client = load_module("nudge_authority_test_client", HERE / "client.py")
    recruiter, _ = client._load_command_modules("recruiter", HERE)
    ledger = recruiter.JobLedger()
    assert ledger.root == tmp_path / "runtime" / "ledger"
    bound = na.NudgeAuthority(ledger.root)
    sent = []
    na.request_nudge(TARGET, "continue", "status", idle, sent.append)
    assert len(read_state(bound)["continue"]["nudges"]) == 1
    environment = dict(os.environ)
    # These are the exact root and service-file exports used by _spawn_job.
    environment["UPAGENT_HUB_DIR"] = str(ledger.root.resolve())
    environment["UPAGENT_STATE"] = str(recruiter.STATE_FILE.resolve())
    code = """
import importlib.util, json, sys
from pathlib import Path
root = Path(sys.argv[1])
def load(name):
    spec = importlib.util.spec_from_file_location(name, root / (name + '.py'))
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module
recruiter = load('recruiter')
na = load('nudge_authority')
target = na.TargetIdentity(**json.loads(sys.argv[2]))
pane = na.Probe(target, 'idle', False, na.PaneState.PRESENT, 'claude')
result = na.request_nudge(target, 'continue', 'run-watch', lambda: pane, lambda _: sys.exit(91))
print(json.dumps({'root': str(recruiter.JobLedger().root), 'path': str(na.NudgeAuthority(recruiter.JobLedger().root).state_path(target)), 'outcome': result.outcome}))
"""
    target_fields = TARGET.canonical()
    del target_fields["v"]
    completed = subprocess.run(
        [sys.executable, "-c", code, str(HERE.resolve()), json.dumps(target_fields)],
        env=environment,
        capture_output=True,
        text=True,
        timeout=15,
        check=True,
    )
    result = json.loads(completed.stdout)
    assert result == {
        "root": str(ledger.root),
        "path": str(bound.state_path(TARGET)),
        "outcome": "backoff",
    }
    assert sent == ["continue"]
    assert service_file.read_text() == '{"state":"ready"}'


def race(authority, calls):
    context = multiprocessing.get_context("fork")
    barrier = context.Barrier(len(calls))
    outcomes = context.Queue()
    deliveries = authority.root.parent / "deliveries.jsonl"

    def worker(payload, trigger, seqs):
        barrier.wait(timeout=5)

        def deliver(text):
            # The authority's one lock protects these appends as well as its JSON.
            with deliveries.open("a") as stream:
                stream.write(json.dumps(text) + "\n")

        result = request(authority, payload, trigger, deliver=deliver, seqs=seqs)
        outcomes.put(result.outcome)

    processes = [context.Process(target=worker, args=call) for call in calls]
    try:
        for process in processes:
            process.start()
        for process in processes:
            process.join(timeout=10)
            assert process.exitcode == 0
        results = [outcomes.get(timeout=2) for _ in processes]
    finally:
        for process in processes:
            if process.is_alive():
                process.terminate()
            process.join(timeout=2)
        outcomes.close()
        outcomes.join_thread()
    sent = []
    if deliveries.exists():
        sent = [json.loads(line) for line in deliveries.read_text().splitlines()]
    return results, sent


@pytest.mark.parametrize(
    "triggers", [("status", "status"), ("status", "sentinel"), ("status", "run-watch")]
)
def test_processes_share_one_continue_intent_and_delivery(authority, triggers):
    results, sent = race(authority, [("continue", trigger, ()) for trigger in triggers])
    assert sorted(results) == ["backoff", "delivered"]
    assert sent == ["continue"]
    intents = read_state(authority)["continue"]["nudges"]
    assert len(intents) == 1
    assert intents[0]["state"] == "delivered"
    assert intents[0]["trigger"] in triggers


def test_cross_payload_race_shares_lock_and_inbox_never_spends_cap(authority):
    results, sent = race(
        authority,
        [
            ("continue", "status", ()),
            ("inbox", "hil", (1,)),
            ("continue", "sentinel", ()),
            ("inbox", "hil", (1,)),
        ],
    )
    assert sorted(results) == ["backoff", "delivered", "delivered", "nothing-pending"]
    assert sorted(sent) == ["continue", "read your inbox"]
    state = read_state(authority)
    assert len(state["continue"]["nudges"]) == 1
    assert state["inbox"]["1"]["attempts"] == 1


def test_duplicate_inject_processes_deliver_one_prompt_for_three_envelopes(authority):
    results, sent = race(authority, [("inbox", "hil", (3, 1, 2, 1))] * 2)
    assert sorted(results) == ["delivered", "nothing-pending"]
    assert sent == ["read your inbox"]
    state = read_state(authority)
    assert state["continue"]["nudges"] == []
    assert set(state["inbox"]) == {"1", "2", "3"}
    assert all(record["state"] == "delivered" for record in state["inbox"].values())


def crash_after_reserve(authority, payload):
    context = multiprocessing.get_context("fork")

    def worker():
        calls = 0

        def probe():
            nonlocal calls
            calls += 1
            if calls == 2:
                os._exit(73)
            return idle()

        request(
            authority, payload, probe=probe, seqs=(1,) if payload == "inbox" else ()
        )

    process = context.Process(target=worker)
    try:
        process.start()
        process.join(timeout=10)
        assert process.exitcode == 73
    finally:
        if process.is_alive():
            process.terminate()
        process.join(timeout=2)


def test_crash_after_continue_reserve_does_not_reserve_or_send_again(authority):
    crash_after_reserve(authority, "continue")
    before = read_state(authority)
    assert len(before["continue"]["nudges"]) == 1
    assert before["continue"]["nudges"][0]["state"] == "reserved"
    sent = []
    assert (
        request(authority, trigger="sentinel", deliver=sent.append).outcome == "backoff"
    )
    assert read_state(authority) == before
    assert sent == []


def test_crashed_inbox_reservation_retries_without_a_second_sequence(authority):
    crash_after_reserve(authority, "inbox")
    assert read_state(authority)["inbox"]["1"]["state"] == "reserved"
    sent = []
    assert (
        request(authority, "inbox", deliver=sent.append, seqs=(1,)).outcome
        == "delivered"
    )
    assert (
        request(authority, "inbox", deliver=sent.append, seqs=(1,)).outcome
        == "nothing-pending"
    )
    state = read_state(authority)
    assert list(state["inbox"]) == ["1"]
    assert state["inbox"]["1"]["attempts"] == 2
    assert state["continue"]["nudges"] == []
    assert sent == ["read your inbox"]


@pytest.mark.parametrize("payload", ["continue", "inbox"])
@pytest.mark.parametrize(
    "change",
    [
        "terminal",
        "identity",
        "working",
        "done",
        "blocked",
        "gone",
        "unknown",
        "harness",
    ],
)
def test_reprobe_change_aborts_reserved_delivery(authority, payload, change):
    second = idle()
    if change == "terminal":
        second = replace(second, terminal_valid=True)
    elif change == "identity":
        second = replace(second, target=replace(TARGET, pane_id="replacement-pane"))
    elif change == "gone":
        second = replace(second, pane_state=na.PaneState.GONE, target=None, status=None)
    elif change == "unknown":
        second = replace(
            second, pane_state=na.PaneState.UNKNOWN, target=None, status=None
        )
    elif change == "harness":
        second = replace(second, harness="codex")
    else:
        second = replace(second, status=change)
    probes = iter([idle(), second])
    sent = []
    if change == "unknown":
        with pytest.warns(na.stall_nudge.PaneProbeWarning):
            result = request(
                authority,
                payload,
                probe=lambda: next(probes),
                deliver=sent.append,
                seqs=(1,) if payload == "inbox" else (),
            )
    else:
        result = request(
            authority,
            payload,
            probe=lambda: next(probes),
            deliver=sent.append,
            seqs=(1,) if payload == "inbox" else (),
        )
    assert result.outcome == "aborted"
    assert result.reason
    assert sent == []
    state = read_state(authority)
    record = (
        state["continue"]["nudges"][0] if payload == "continue" else state["inbox"]["1"]
    )
    assert record["state"] == "aborted"
    assert record["reason"] == result.reason
    if change == "working":
        assert state["continue"]["last_verdict"] == "WORKING"


def test_valid_result_file_landing_after_reserve_prevents_send(authority, tmp_path):
    contracts = load_module("nudge_authority_test_contracts", HERE / "contracts.py")
    result_file = tmp_path / "result.json"
    calls = 0

    def probe():
        nonlocal calls
        calls += 1
        valid = False
        if calls == 2:
            assert read_state(authority)["continue"]["nudges"][0]["state"] == "reserved"
            result_file.write_text(
                json.dumps(
                    {
                        "order_id": "order-1",
                        "verdict": "passed",
                        "full_log": "test session",
                    }
                )
            )
        if result_file.exists():
            valid = (
                contracts.load_result(result_file, expected_order_id="order-1")[
                    "verdict"
                ]
                == "passed"
            )
        return replace(idle(), terminal_valid=valid)

    sent = []
    assert request(authority, probe=probe, deliver=sent.append).outcome == "aborted"
    assert request(authority, probe=probe, deliver=sent.append).outcome == "finished"
    assert sent == []


def test_aborted_inbox_retries_on_next_idle_call(authority):
    probes = iter([idle(), replace(idle(), status="done")])
    assert (
        request(authority, "inbox", probe=lambda: next(probes), seqs=(2,)).outcome
        == "aborted"
    )
    sent = []
    result = request(authority, "inbox", deliver=sent.append, seqs=(2,))
    assert result.outcome == "delivered"
    assert result.envelope_seqs == (2,)
    assert sent == ["read your inbox"]
    assert read_state(authority)["inbox"]["2"]["attempts"] == 2


def test_continue_backoff_cap_and_new_idle_episode(authority, monkeypatch):
    sent = []
    for at, expected in [
        (1000, "delivered"),
        (1299, "backoff"),
        (1300, "delivered"),
        (2199, "backoff"),
        (2200, "delivered"),
        (5000, "cap-exhausted"),
    ]:
        monkeypatch.setattr(na.time, "time", lambda: at)
        assert request(authority, deliver=sent.append).outcome == expected
    assert len(sent) == 3
    assert (
        request(authority, "inbox", seqs=(7,), deliver=sent.append).outcome
        == "delivered"
    )
    assert len(read_state(authority)["continue"]["nudges"]) == 3
    assert (
        request(
            authority,
            "inbox",
            probe=lambda: replace(idle(), status="working"),
            seqs=(8,),
        ).outcome
        == "not-idle"
    )
    assert request(authority, deliver=sent.append).outcome == "delivered"
    assert read_state(authority)["continue"]["episode"] == 2
    assert len(read_state(authority)["continue"]["nudges"]) == 1


@pytest.mark.parametrize("status", ["working", "blocked"])
def test_nonidle_never_sends_or_reserves_inbox(authority, status):
    sent = []
    result = request(
        authority,
        "inbox",
        probe=lambda: replace(idle(), status=status),
        deliver=sent.append,
        seqs=(1,),
    )
    assert result.outcome == "not-idle"
    assert sent == []
    result = request(authority, "inbox", deliver=sent.append, seqs=(1,))
    assert result.outcome == "delivered"
    assert read_state(authority)["inbox"]["1"]["attempts"] == 1


def test_empty_inbox_is_nothing_pending(authority):
    assert request(authority, "inbox").outcome == "nothing-pending"
    assert read_state(authority)["continue"]["nudges"] == []


def test_terminal_wins_even_over_initial_missing_identity(authority):
    assert (
        request(
            authority,
            probe=lambda: replace(
                idle(),
                target=None,
                terminal_valid=True,
                pane_state=na.PaneState.GONE,
                harness="codex",
            ),
        ).outcome
        == "finished"
    )
    assert not authority.state_path(TARGET).exists()


def test_initial_identity_mismatch_aborts_without_reservation(authority):
    assert (
        request(
            authority, probe=lambda: idle(replace(TARGET, owner_id="different"))
        ).outcome
        == "aborted"
    )
    assert not authority.state_path(TARGET).exists()


@pytest.mark.parametrize("payload", ["continue", "inbox"])
def test_delivery_error_propagates_and_retains_reservation(authority, payload):
    def fail(_):
        raise OSError("delivery failed")

    with pytest.raises(OSError, match="delivery failed"):
        request(
            authority, payload, deliver=fail, seqs=(1,) if payload == "inbox" else ()
        )
    state = read_state(authority)
    record = (
        state["continue"]["nudges"][0] if payload == "continue" else state["inbox"]["1"]
    )
    assert record["state"] == "reserved"
    expected = "backoff" if payload == "continue" else "delivered"
    assert (
        request(authority, payload, seqs=(1,) if payload == "inbox" else ()).outcome
        == expected
    )


@pytest.mark.parametrize(
    "mutation",
    [
        "target",
        "hash",
        "intent-hash",
        "schema",
        "episode",
        "cap",
        "inbox",
        "attempts",
        "nan",
        "delivered",
        "last_verdict",
    ],
)
def test_corruption_fails_before_probe_or_mutation(authority, mutation):
    request(authority)
    request(authority, "inbox", seqs=(1,))
    state = read_state(authority)
    if mutation == "target":
        state["target"]["owner_id"] = "other"
    elif mutation == "hash":
        state["target_id"] = "0" * 64
    elif mutation == "intent-hash":
        state["continue"]["nudges"][0]["digest"] = "0" * 64
    elif mutation == "schema":
        state["schema"] = True
    elif mutation == "episode":
        state["continue"]["episode"] = -1
    elif mutation == "cap":
        state["continue"]["nudges"] *= 4
    elif mutation == "inbox":
        state["inbox"]["01"] = state["inbox"].pop("1")
    elif mutation == "attempts":
        state["inbox"]["1"]["attempts"] = True
    elif mutation == "nan":
        state["continue"]["nudges"][0]["at"] = float("nan")
    elif mutation == "delivered":
        state["continue"]["nudges"][0]["delivered"] = False
    elif mutation == "last_verdict":
        del state["continue"]["last_verdict"]
    path = authority.state_path(TARGET)
    path.write_text(json.dumps(state))
    before = path.read_bytes()

    def forbidden():
        pytest.fail("corrupt state must fail before probing")

    with pytest.raises(na.NudgeAuthorityError):
        request(authority, probe=forbidden)
    assert path.read_bytes() == before


@pytest.mark.parametrize("raw", [b"{", b"[]", b"\xff", b'{"schema":1,"schema":1}'])
def test_unreadable_or_duplicate_json_fails_loud(authority, raw):
    authority.root.mkdir(parents=True)
    authority.state_path(TARGET).write_bytes(raw)
    with pytest.raises(na.NudgeAuthorityError):
        request(authority)


def test_directory_cannot_be_treated_as_absent_state(authority):
    authority.state_path(TARGET).mkdir(parents=True)
    with pytest.raises(na.NudgeAuthorityError):
        request(authority)


def test_lock_contention_preserves_valid_json_and_probes_only_after_lock(authority):
    request(authority, "inbox", seqs=(0,))
    context = multiprocessing.get_context("fork")
    started = context.Event()
    probed = context.Event()

    def worker():
        started.set()

        def probe():
            probed.set()
            return idle()

        request(authority, "inbox", probe=probe, seqs=tuple(range(30)))

    process = context.Process(target=worker)
    before = read_state(authority)
    try:
        with authority.state_path(TARGET).with_suffix(".lock").open("a+") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            process.start()
            assert started.wait(timeout=5)
            assert not probed.wait(timeout=0.1)
            for _ in range(30):
                assert read_state(authority) == before
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
        process.join(timeout=10)
        assert process.exitcode == 0
        assert probed.is_set()
        state = read_state(authority)
        assert len(state["inbox"]) == 30
        assert all(record["state"] == "delivered" for record in state["inbox"].values())
    finally:
        if process.is_alive():
            process.terminate()
        process.join(timeout=2)


def test_replacement_working_pane_cannot_reset_this_targets_cap(authority):
    probes = iter(
        [
            idle(),
            replace(
                idle(),
                target=replace(TARGET, agent_name="replacement"),
                status="working",
            ),
        ]
    )
    assert request(authority, probe=lambda: next(probes)).outcome == "aborted"
    assert read_state(authority)["continue"]["last_verdict"] == "NUDGE"
    assert request(authority).outcome == "backoff"


def test_inbox_never_calls_continue_decision_logic(authority, monkeypatch):
    def forbidden(*args):
        pytest.fail("inbox must never call decide")

    monkeypatch.setattr(na.stall_nudge, "decide", forbidden)
    assert request(authority, "inbox", seqs=(1, 2)).outcome == "delivered"
    assert request(authority, "inbox", seqs=(1, 2)).outcome == "nothing-pending"
    assert read_state(authority)["continue"]["nudges"] == []


@pytest.mark.parametrize("state", [na.PaneState.GONE, na.PaneState.UNKNOWN])
def test_absent_or_uncertain_first_probe_never_reserves(authority, state):
    def probe():
        return replace(idle(), target=None, pane_state=state, status=None)

    if state == na.PaneState.UNKNOWN:
        with pytest.warns(na.stall_nudge.PaneProbeWarning):
            result = request(authority, probe=probe)
    else:
        result = request(authority, probe=probe)
    assert result.outcome == "not-idle"
    assert not authority.state_path(TARGET).exists()


def test_exec_style_never_sends(authority):
    result = request(authority, probe=lambda: replace(idle(), harness="codex"))
    assert result.outcome == "not-idle"
    assert not authority.state_path(TARGET).exists()


@pytest.mark.parametrize(
    "field,value,error",
    [
        ("harness", "unknown", na.offerings.OfferingError),
        ("status", "unrecognized", na.stall_nudge.StallNudgeError),
        ("status", None, na.stall_nudge.StallNudgeError),
        ("terminal_valid", "yes", na.NudgeAuthorityError),
    ],
)
def test_invalid_probe_fails_loud(authority, field, value, error):
    with pytest.raises(error):
        request(authority, probe=lambda: replace(idle(), **{field: value}))
    assert not authority.state_path(TARGET).exists()


@pytest.mark.parametrize(
    "kwargs",
    [
        {"payload": "other"},
        {"trigger": ""},
        {"trigger": None},
        {"payload": "inbox", "seqs": (-1,)},
        {"payload": "inbox", "seqs": (True,)},
        {"payload": "inbox", "seqs": (1.0,)},
        {"seqs": (1,)},
    ],
)
def test_invalid_request_fails_before_storage(authority, kwargs):
    with pytest.raises(na.NudgeAuthorityError):
        request(authority, **kwargs)
    assert not authority.root.exists()


@pytest.mark.parametrize(
    "field,value", [("owner_kind", "other"), ("pane_id", ""), ("workspace_id", None)]
)
def test_invalid_target_fails_loud(field, value):
    with pytest.raises(na.NudgeAuthorityError):
        replace(TARGET, **{field: value})


def test_observing_transitions_never_spends_or_delivers_and_resets_spent_episode(
    authority,
):
    authority.observe(TARGET, idle)
    assert read_state(authority)["continue"]["episode"] == 1
    assert read_state(authority)["continue"]["nudges"] == []
    assert request(authority).outcome == "delivered"
    authority.observe(TARGET, lambda: replace(idle(), status="working"))
    authority.observe(TARGET, idle)
    assert read_state(authority)["continue"]["episode"] == 2
    assert read_state(authority)["continue"]["nudges"] == []
    assert request(authority).outcome == "delivered"


def test_observation_and_escalation_cannot_mutate_a_replacement_episode(authority):
    authority.observe(TARGET, idle)
    authority.mark_escalated(TARGET, 1)
    assert read_state(authority)["continue"]["escalated"] is True
    authority.observe(
        TARGET,
        lambda: replace(
            idle(), target=replace(TARGET, agent_name="other"), status="working"
        ),
    )
    assert read_state(authority)["continue"]["last_verdict"] == "NUDGE"
    authority.observe(TARGET, lambda: replace(idle(), status="working"))
    authority.observe(TARGET, idle)
    authority.mark_escalated(TARGET, 1)
    assert read_state(authority)["continue"]["episode"] == 2
    assert read_state(authority)["continue"]["escalated"] is False
