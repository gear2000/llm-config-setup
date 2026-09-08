"""Human inbox delivery through the controller and the shared nudge authority."""

from __future__ import annotations

import importlib.util
import json
import os
import re
import subprocess
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[4]
spec = importlib.util.spec_from_file_location(
    "implementer_inbox_controller", HERE / "implementer_controller.py"
)
assert spec and spec.loader
controller = importlib.util.module_from_spec(spec)
spec.loader.exec_module(controller)
rw = controller.supervision._load("run_watch")
HIL = ROOT / ".shared-llm/public/layers/slash-commands/common/claude/hil/command.md"
COMMAND = ROOT / ".shared-llm/public/layers/slash-commands/common/common/plan-implementer/command.md"


@pytest.fixture
def inbox(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict:
    monkeypatch.setenv("UPAGENT_HUB_DIR", str(tmp_path / "ledger"))
    monkeypatch.setenv("UPAGENT_STATE", str(tmp_path / "service.json"))
    receipt = tmp_path / "run/control/implementer-start.json"
    receipt.parent.mkdir(parents=True)
    receipt.write_text(json.dumps({
        "state": "ready", "run_id": "run", "phase_id": "plan",
        "implementer_pane": "implementer", "hil_pane": "human",
        "agent_name": "controller", "workspace_id": "workspace",
        "herdr_session": "test-session", "harness": "claude", "supervise": False,
    }))
    state = {
        "receipt": receipt, "root": receipt.parent.parent, "sent": [],
        "status": "idle", "workspace": "workspace", "probes": 0,
    }

    def herdr(self: object, session: str, *args: str) -> dict:
        assert session == "test-session"
        if args[:2] == ("pane", "get"):
            state["probes"] += 1
            if state.get("on_probe"):
                state["on_probe"]()
            return {"result": {"pane": {
                "pane_id": "implementer", "workspace_id": state["workspace"],
                "agent_status": state["status"],
            }}}
        if args[:2] == ("agent", "get"):
            return {"result": {"agent": {
                "pane_id": "implementer", "workspace_id": "workspace",
                "name": "controller",
            }}}
        assert args == ("pane", "run", "implementer", "read your inbox")
        records = authority_state(state)
        assert all(item["state"] == "reserved" for item in records["inbox"].values())
        if state.get("fail_send"):
            raise OSError("fake send failed")
        state["sent"].append(args[-1])
        return {"result": {}}

    monkeypatch.setattr(rw.Runtime, "herdr", herdr)
    return state


def envelope(state: dict, seq: int, *, acked: bool = False) -> Path:
    path = state["root"] / "inbox" / f"msg-{seq}.json"
    rw.write_json(path, {"seq": seq, "text": "Human's text\n$(false); `false`", "at_ns": 1, "acked": acked})
    return path


def authority_state(state: dict) -> dict:
    paths = list((state["root"].parent / "ledger/nudge").glob("*.json"))
    assert len(paths) == 1
    return json.loads(paths[0].read_text())


@pytest.mark.parametrize("acked", [None, True])
def test_no_pending_does_not_probe(inbox: dict, acked: bool | None) -> None:
    if acked:
        envelope(inbox, 1, acked=True)
    result = controller.inject_implementer(inbox["receipt"])
    assert result["outcome"] == "nothing-pending"
    assert not result["envelope_seqs"]
    assert inbox["sent"] == [] and inbox["probes"] == 0


def test_working_then_idle_batches_and_suppresses_duplicates(inbox: dict) -> None:
    paths = [envelope(inbox, seq) for seq in (10, 2, 1)]
    originals = [path.read_bytes() for path in paths]
    inbox["status"] = "working"
    result = controller.inject_implementer(inbox["receipt"])
    assert result["outcome"] == "not-idle"
    assert result["envelope_seqs"] == [1, 2, 10]
    assert inbox["sent"] == []
    inbox["status"] = "idle"
    assert controller.inject_implementer(inbox["receipt"])["outcome"] == "delivered"
    duplicate = controller.inject_implementer(inbox["receipt"])
    assert duplicate["outcome"] == "nothing-pending"
    assert duplicate["envelope_seqs"] == [1, 2, 10]
    assert inbox["sent"] == ["read your inbox"]
    assert [path.read_bytes() for path in paths] == originals
    state = authority_state(inbox)
    assert state["continue"]["nudges"] == []
    assert set(state["inbox"]) == {"1", "2", "10"}
    assert all(item["state"] == "delivered" and item["trigger"] == "hil"
               and item["attempts"] == 1 for item in state["inbox"].values())


@pytest.mark.parametrize("guard,outcome", [
    ("identity", "aborted"), ("finishing", "not-idle"),
    ("terminal", "finished"), ("codex", "not-idle"), ("blocked", "not-idle"),
])
def test_owner_guards_prevent_delivery(inbox: dict, guard: str, outcome: str) -> None:
    path = envelope(inbox, 1)
    before = path.read_bytes()
    if guard == "identity":
        inbox["workspace"] = "replacement"
    elif guard == "finishing":
        (inbox["receipt"].parent / "finishing.json").write_text('{"step": 1}')
    elif guard == "terminal":
        write_result(inbox)
    elif guard == "codex":
        receipt = json.loads(inbox["receipt"].read_text())
        receipt["harness"] = "codex"
        inbox["receipt"].write_text(json.dumps(receipt))
    else:
        inbox["status"] = "blocked"
    assert controller.inject_implementer(inbox["receipt"])["outcome"] == outcome
    assert not inbox["sent"] and path.read_bytes() == before


def write_result(state: dict, run_id: str = "run") -> None:
    (state["root"] / "implementer-result.json").write_text(json.dumps({
        "verdict": "passed", "summary": "test", "run_id": run_id,
        "run_root": str(state["root"]),
    }))


def test_invalid_terminal_evidence_does_not_block_delivery(inbox: dict) -> None:
    envelope(inbox, 1)
    write_result(inbox, "wrong-run")
    assert controller.inject_implementer(inbox["receipt"])["outcome"] == "delivered"


@pytest.mark.parametrize("change", ["finishing", "terminal", "working", "identity"])
def test_reprobe_aborts_and_idle_retry_succeeds(inbox: dict, change: str) -> None:
    envelope(inbox, 1)

    def change_before_reprobe() -> None:
        assert authority_state(inbox)["inbox"]["1"]["state"] == "reserved"
        if change == "finishing":
            (inbox["receipt"].parent / "finishing.json").write_text('{"step": 1}')
        elif change == "terminal":
            write_result(inbox)
        elif change == "identity":
            inbox["workspace"] = "replacement"
        else:
            inbox["status"] = "working"

    # Run the real owner's second probe after the authority has reserved the seq.
    original = rw.Runtime.probe
    calls = 0

    def probe(runtime: object, target: object) -> object:
        nonlocal calls
        calls += 1
        if calls == 2:
            change_before_reprobe()
        return original(runtime, target)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(rw.Runtime, "probe", probe)
        assert controller.inject_implementer(inbox["receipt"])["outcome"] == "aborted"
    assert not inbox["sent"]
    assert authority_state(inbox)["inbox"]["1"]["state"] == "aborted"
    (inbox["receipt"].parent / "finishing.json").unlink(missing_ok=True)
    (inbox["root"] / "implementer-result.json").unlink(missing_ok=True)
    inbox.update(status="idle", workspace="workspace")
    assert controller.inject_implementer(inbox["receipt"])["outcome"] == "delivered"
    assert authority_state(inbox)["inbox"]["1"]["attempts"] == 2


def test_failed_delivery_retains_reservation_for_retry(inbox: dict) -> None:
    envelope(inbox, 1)
    inbox["fail_send"] = True
    with pytest.raises(OSError, match="fake send failed"):
        controller.inject_implementer(inbox["receipt"])
    assert authority_state(inbox)["inbox"]["1"]["state"] == "reserved"
    inbox["fail_send"] = False
    assert controller.inject_implementer(inbox["receipt"])["outcome"] == "delivered"
    assert authority_state(inbox)["inbox"]["1"]["attempts"] == 2


@pytest.mark.parametrize("supervise", [False, True])
def test_finish_fences_inbox_under_the_same_authority_lock(
    inbox: dict, monkeypatch: pytest.MonkeyPatch, supervise: bool,
) -> None:
    receipt = json.loads(inbox["receipt"].read_text())
    receipt["supervise"] = supervise
    rw.write_json(inbox["receipt"], receipt)
    write_result(inbox)
    runtime = rw.Runtime(rw.awaiter.ImplementerContext(inbox["receipt"]), {})
    authority_lock = runtime.nudges.state_path(runtime.implementer().identity).with_suffix(".lock")
    held: set[Path] = set()

    @contextmanager
    def exclusive(path: Path) -> Iterator[None]:
        with controller._exclusive(path):
            held.add(path)
            yield
            held.remove(path)

    original = controller.supervision.write
    fenced = []

    def write(path: Path, data: dict) -> None:
        if path.name == "finishing.json" and data["step"] == 1:
            assert authority_lock in held
            fenced.append(True)
        original(path, data)

    monkeypatch.setattr(controller.supervision, "write", write)
    result = controller.supervision.finish(
        inbox["receipt"], receipt, False, rw.recruiter, controller.contracts, exclusive,
    )
    assert fenced == [True] and result["verdict"] == "passed"


@pytest.mark.parametrize("field,value", [
    ("seq", True), ("seq", 2), ("seq", -1), ("text", None),
    ("at_ns", "1"), ("acked", "false"), ("extra", 1),
])
def test_malformed_envelope_fails_before_probing(inbox: dict, field: str, value: object) -> None:
    path = envelope(inbox, 1)
    data = json.loads(path.read_text())
    data[field] = value
    path.write_text(json.dumps(data))
    with pytest.raises(controller.ImplementerStartError, match="invalid human inbox"):
        controller.inject_implementer(inbox["receipt"])
    assert inbox["probes"] == 0


def test_hil_contract_and_real_file_writer(tmp_path: Path) -> None:
    text = HIL.read_text()
    assert "unless `--offering` is literally present" in text
    assert text.index("unless `--offering` is literally present") < text.index("4. Run `just upagent lists")
    echo = "offering=<id> harness=<harness> effort=<effort> supervise=<bool>"
    assert text.index("4. Run `just upagent lists") < text.index(echo) < text.index("## Start + relay")
    assert '[--no-supervise]' in text and '"$AFTER" 60000' in text
    assert "590000" not in text
    assert "After every await return" in text
    assert "whenever any envelope remains unacknowledged" in text
    assert "even if this return brought no new human text" in text
    assert 'just upagent-implementer-inject "$RECEIPT"' in text
    assert "`inject`, `ack`, `respond`, and `finish`" in text
    script = next(block for block in re.findall(r"```bash\n(.*?)```", text, re.S)
                  if '"$HUMAN_MESSAGE_FILE"' in block)
    source = tmp_path / "human text"
    literal = "Keep 'quotes', Unicode ☎, `false`, and $(false).\nSecond line.\n"
    source.write_text(literal)
    env = {**os.environ, "RUN_ROOT": str(tmp_path / "run"), "HUMAN_MESSAGE_FILE": str(source)}
    for seq in (1, 2):
        result = subprocess.run(["bash", "-eu", "-c", script], env=env, text=True, capture_output=True, timeout=10)
        assert result.returncode == 0, result.stderr
        path = Path(result.stdout.strip())
        data = json.loads(path.read_text())
        assert path.name == f"msg-{seq}.json"
        assert data["seq"] == seq and data["text"] == literal and data["acked"] is False
        assert type(data["at_ns"]) is int and data["at_ns"] > 0
        assert path.stat().st_mode & 0o777 == 0o600
        # Acknowledgement must not permit sequence reuse.
        data["acked"] = True
        rw.write_json(path, data)
    assert not list(path.parent.glob("*.tmp"))
    implementer = COMMAND.read_text()
    for rule in ("At every slice boundary", "read your inbox", "numeric `seq` order",
                 "Quote its entire `text` verbatim", "atomically set `acked: true`",
                 "before writing the final result", "Never delete envelopes"):
        assert rule in implementer


def test_recomposed_relay_rollback_keeps_registry_and_phase1(tmp_path: Path) -> None:
    """Remove only the relay layer additions in an isolated composition tree."""
    hire = COMMAND.with_name("hire.md")
    hil_recipe = ROOT / ".shared-llm/public/compose/slash-commands/common/claude/hil.yaml"
    implementer_recipe = ROOT / ".shared-llm/public/compose/slash-commands/common/common/plan-implementer.yaml"
    inputs = [HIL, COMMAND, hire, HIL.with_name("description.md"),
              COMMAND.with_name("description.md"), hil_recipe, implementer_recipe]
    for source in inputs:
        target = tmp_path / source.relative_to(ROOT)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(source.read_bytes())
    hil_copy = tmp_path / HIL.relative_to(ROOT)
    command_copy = tmp_path / COMMAND.relative_to(ROOT)
    output = tmp_path / "composed"
    for rollback in (False, True):
        if rollback:
            text = hil_copy.read_text()
            begin = text.index("After every await return")
            end = text.index("Handle the JSON `kind`", begin)
            text = text[:begin] + text[end:]
            hil_copy.write_text(text.replace('"$AFTER" 60000', '"$AFTER" 590000'))
            text = command_copy.read_text()
            begin = text.index("## Human inbox")
            end = text.index("## Stop and ask", begin)
            command_copy.write_text(text[:begin] + text[end:])
        for recipe in (hil_recipe, implementer_recipe):
            result = subprocess.run([
                sys.executable, str(ROOT / "tools/harness.py"), "compose",
                str(tmp_path / recipe.relative_to(ROOT)),
                "--shared-llm", str(tmp_path / ".shared-llm"), "--target", str(output),
            ], text=True, capture_output=True, timeout=20)
            assert result.returncode == 0, result.stderr
        hil = (output / ".claude/skills/hil/SKILL.md").read_text()
        implementer = (output / ".claude/skills/plan-implementer/SKILL.md").read_text()
        assert ("just upagent-implementer-inject" in hil) is not rollback
        assert ("## Human inbox" in implementer) is not rollback
        assert "state: ready-degraded" in hil and "startup_advisory` verbatim" in hil
        assert "Print every `cleanup` entry verbatim" in hil
        assert "flow1:cleanup-failed:" in hil and "--no-supervise" in hil
        assert hire.read_text() in implementer
        assert 'just upagent-register-worker "$run_root" "$response"' in implementer
        assert (tmp_path / hire.relative_to(ROOT)).read_bytes() == hire.read_bytes()
