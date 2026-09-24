"""Public request records for synchronous resident turns, never Recruiter jobs."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any


def marker(registered: Any) -> Path:
    return registered.request_dir / "resident.json"


def _owner_dead(api: Any, residents: Any, current: dict) -> bool:
    """Whether the process that started this turn is provably gone, or the turn has run past
    even the most generous bound a live owner could still be waiting inside. Shared by
    `submit` and `status` so a replay and a status read agree on the same resident request."""
    return (
        api.recruiter._process_start_time(current["owner_pid"])
        != current["owner_birth"]
        or time.time() - current["started_at"] > 3 * residents.WAIT_SECONDS
    )


def submit(
    api: Any, residents: Any, registered: Any, cockpit: str | None
) -> tuple[int, bool] | None:
    """Called under the existing public request lock; replay never redelivers."""
    payload = registered.record["payload"]
    path = marker(registered)
    current = residents._read(path)
    if current:
        if current["state"] == "running":
            if not _owner_dead(api, residents, current):
                raise api.PublicError(
                    "resident request delivery is unresolved; inspect status, do not replay"
                )
            # The owner is gone but the question may already have reached the resident, so
            # delivery is UNKNOWN, not known-failed: report blocked without ever redelivering
            # it, and leave the marker exactly as it was for an operator to inspect.
            return 1, False
        return (0 if current["state"] == "finished" else 1), False
    if payload["type"] != "specialist" or not residents.enabled(
        payload["specialist"], payload["cwd"]
    ):
        return None
    # Do not migrate an already accepted cold request into a resident turn.
    if (
        api.recruiter.JobLedger()
        .request_dir(
            api.recruiter.JobLedger().key_for_order(
                api.recruiter.load_order(registered.order_path)
            )
        )
        .exists()
    ):
        return None
    if api.PublicRequestStore().submission(registered)["state"] != "registered":
        return None
    residents._private_dir(registered.request_dir)
    current = {
        "state": "running",
        "started_at": time.time(),
        "owner_pid": os.getpid(),
        "owner_birth": api.recruiter._process_start_time(os.getpid()),
    }
    residents._write(path, current)
    question = Path(registered.record["prompt_snapshot"]).read_text()
    consult = {
        "specialist": payload["specialist"],
        "cwd": payload["cwd"],
        "consult_id": registered.request_id,
        "question": question,
        "timeout_seconds": min(
            residents.WAIT_SECONDS, payload.get("duration_minutes", 10) * 60
        ),
    }
    try:
        answer = residents.consult(consult, cockpit)
    except (
        residents.SpecialistError,
        api.recruiter.RecruiterError,
        api.recruiter.contracts_consult.ConsultError,
    ) as error:
        answer = api.recruiter.contracts_consult.failure_answer(
            registered.request_id, str(error)
        )
    if answer is None:
        answer = api.recruiter.contracts_consult.failure_answer(
            registered.request_id,
            "specialist was disabled before delivery; submit a new request",
        )
    reason = answer.get("error")
    verdict = "blocked" if reason else "passed"
    order = api.recruiter.load_order(registered.order_path)
    log = registered.request_dir / "resident-turn.json"
    residents._write(
        log,
        {"question": question, "answer": answer, "request_id": registered.request_id},
    )
    result = {
        "order_id": order["order_id"],
        "verdict": verdict,
        "full_log": str(log),
        "summary": reason or answer["answer"],
        "reason": reason or "Resident answered with citations",
        "resident": True,
    }
    api.recruiter.contracts.parse_result(
        json.dumps(result), expected_order_id=order["order_id"]
    )
    residents._write(registered.request_dir / "answer.json", answer)
    residents._write(registered.request_dir / "result.json", result)
    # These are summaries of the actual answer, not fabricated job/cleanup evidence.
    text = reason or answer["answer"] + "\n\n" + "\n".join(answer["citations"])
    for filename in ("compacted.md", "handoff.md"):
        destination = registered.request_dir / filename
        destination.write_text(text + "\n")
        os.chmod(destination, 0o600)
    receipt = {
        "request_id": registered.request_id,
        "resident": True,
        "answer_verdict": "failed" if reason else "cited",
        "answer_path": str(registered.request_dir / "answer.json"),
        "specialist": payload["specialist"],
        "reason": result["reason"],
        "cleanup": "resident retained; no Recruiter job was created",
    }
    residents._write(registered.request_dir / "resident-receipt.json", receipt)
    current.update(state="blocked" if reason else "finished", completed_at=time.time())
    residents._write(path, current)
    return (1 if reason else 0), True


def status(api: Any, residents: Any, registered: Any) -> dict | None:
    current = residents._read(marker(registered))
    if current is None:
        return None
    if current["state"] == "running" and _owner_dead(api, residents, current):
        current = {
            **current,
            "state": "blocked",
            "reason": "resident request caller disappeared or deadline elapsed; delivery outcome uncertain",
        }
    artifacts = []
    for kind, filename in (
        ("prompt", "prompt.md"),
        ("order", "order.json"),
        ("result", "result.json"),
        ("answer", "answer.json"),
        ("receipt", "resident-receipt.json"),
        ("compacted", "compacted.md"),
        ("handoff", "handoff.md"),
    ):
        path = registered.request_dir / filename
        artifacts.append({"kind": kind, "path": str(path), "present": path.is_file()})
    return {
        "request_id": registered.request_id,
        "resident": True,
        "payload": registered.record["payload"],
        "payload_sha256": registered.record["payload_sha256"],
        "offering_snapshot": registered.record["offering_snapshot"],
        "state": {
            "state": current["state"],
            **({"reason": current["reason"]} if "reason" in current else {}),
        },
        "result": residents._read(registered.request_dir / "result.json"),
        "receipt": residents._read(registered.request_dir / "resident-receipt.json"),
        "artifacts": artifacts,
        "pruned": False,
        "review": None,
    }


def wait(api: Any, residents: Any, registered: Any) -> tuple[int, dict]:
    deadline = time.monotonic() + 3 * residents.WAIT_SECONDS
    while True:
        value = status(api, residents, registered)
        if value is None:
            raise api.PublicError("resident request status disappeared while awaiting")
        if value["state"]["state"] != "running":
            return (0 if value["state"]["state"] == "finished" else 1), value
        if time.monotonic() >= deadline:
            raise api.PublicError("bounded resident request wait expired")
        time.sleep(0.2)
