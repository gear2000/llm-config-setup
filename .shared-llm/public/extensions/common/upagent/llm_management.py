"""Configuration and bounded prompts for UpAgent's LLM management roles."""

from __future__ import annotations

import json
import shlex
import string
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path


ROLE_TEMPLATE_FIELDS = {"brief_path", "cwd", "output_path"}
MAX_INTAKE_CLERK_TIMEOUT_MS = 300_000
DEFAULT_ACCOUNT_MANAGER_COMMAND = (
    "claude --dangerously-skip-permissions --agent upagent-account-manager --model claude-sonnet-5 --effort low "
    '"Read {brief_path}, perform that one lifecycle review, write {output_path}, then remain available."'
)
DEFAULT_CHECKER_COMMAND = (
    "claude --dangerously-skip-permissions --agent upagent-checker --model haiku --effort low "
    '"Read {brief_path}, perform that one bounded assessment, write {output_path}, then exit."'
)
DEFAULT_RESCUER_COMMAND = (
    "claude --dangerously-skip-permissions --agent upagent-rescuer --model claude-sonnet-5 --effort low "
    '"Read {brief_path}, perform that one bounded salvage assessment, write {output_path}, then exit."'
)
DEFAULT_SENTINEL_COMMAND = (
    "claude --dangerously-skip-permissions --agent upagent-sentinel --model haiku --effort low "
    '"Read {brief_path}, perform that one sentinel duty cycle for its worker, write '
    '{output_path} when the worker lifecycle ends, then exit."'
)
DEFAULT_OPENAI_SENTINEL_COMMAND = shlex.join(
    [
        "pi",
        "--approve",
        "--no-extensions",
        "-e",
        str(Path.home() / ".pi/agent/extensions/herdr-agent-state.ts"),
        "--model",
        "openai-codex/gpt-5.4-mini",
        "--thinking",
        "low",
        "Read {brief_path}, perform that one sentinel duty cycle for its worker, write "
        "{output_path} when the worker lifecycle ends, then exit.",
    ]
)
DEFAULT_INTAKE_CLERK_COMMAND = (
    'claude --print --output-format text --tools "" --agent intake-clerk '
    "--model sonnet --effort low < {brief_path}"
)


class ManagementConfigError(ValueError):
    """An invalid LLM management-role configuration."""


@dataclass(frozen=True)
class ManagementRole:
    command: str
    expected_agent: str
    expected_process: str
    timeout_ms: int


# How each request lifecycle is owned. KEEP IN SYNC with contracts.MANAGEMENT_MODES
# (both modules load standalone by path).
MANAGEMENT_MODES = ("direct", "dedicated")


@dataclass(frozen=True)
class ManagementConfig:
    account_manager: ManagementRole
    checker: ManagementRole
    # Hired ONLY when the Recruiter's mechanical salvage inspection finds contradictory
    # evidence about a vanished worker. A clean hit and a clean miss are both decided in
    # Python and never spawn this role.
    rescuer: ManagementRole
    # One per sentinel-supervised request: a cheap pane duty-bound to that request's worker
    # from liftoff to closeout. Eyes and interpretation only — it holds no kill switch and
    # its closeout citations are re-verified in Python before they count.
    sentinel: ManagementRole
    sentinels: dict[str, ManagementRole]
    sentinel_is_override: bool
    intake_clerk: ManagementRole
    startup_timeout_ms: int
    inactivity_check_ms: int
    requester_grace_ms: int
    mode: str = "direct"
    # One automatic broker-advised relaunch when a worker launch fails or stalls before
    # health verification. The fast Python path stays the default; intelligence is hired
    # exactly at the failure point.
    rescue_on_startup_failure: bool = True


def _positive_int(value: object, field: str, default: int) -> int:
    candidate = default if value is None else value
    if isinstance(candidate, bool) or not isinstance(candidate, int) or candidate <= 0:
        raise ManagementConfigError(f"management.{field} must be a positive integer")
    return candidate


def _role(
    raw: object,
    name: str,
    default_command: str,
    *,
    max_timeout_ms: int | None = None,
) -> ManagementRole:
    value = {} if raw is None else raw
    if not isinstance(value, dict):
        raise ManagementConfigError(f"management.{name} must be an object")
    command = value.get("command", default_command)
    if not isinstance(command, str) or not command.strip():
        raise ManagementConfigError(
            f"management.{name}.command must be a non-empty string"
        )
    fields = {
        field_name
        for _, field_name, _, _ in string.Formatter().parse(command)
        if field_name
    }
    unknown = fields - ROLE_TEMPLATE_FIELDS
    if unknown:
        raise ManagementConfigError(
            f"management.{name}.command uses unknown placeholder(s): {', '.join(sorted(unknown))}"
        )
    expected_agent = value.get("expected_agent", "claude")
    expected_process = value.get("expected_process", "claude")
    if not isinstance(expected_agent, str) or not expected_agent:
        raise ManagementConfigError(
            f"management.{name}.expected_agent must be a non-empty string"
        )
    if not isinstance(expected_process, str) or not expected_process:
        raise ManagementConfigError(
            f"management.{name}.expected_process must be a non-empty string"
        )
    timeout_ms = _positive_int(value.get("timeout_ms"), f"{name}.timeout_ms", 120_000)
    if max_timeout_ms is not None and timeout_ms > max_timeout_ms:
        raise ManagementConfigError(
            f"management.{name}.timeout_ms must be no greater than {max_timeout_ms}"
        )
    return ManagementRole(command, expected_agent, expected_process, timeout_ms)


def _sentinel_roles(raw: dict) -> dict[str, ManagementRole]:
    configured = raw.get("sentinels")
    if configured is None:
        return {
            "anthropic": _role(None, "sentinels.anthropic", DEFAULT_SENTINEL_COMMAND),
            # The openai default launches pi, so its health identity must be pi —
            # _role's claude defaults would fail every hire on legacy rosters.
            "openai": _role(
                {"expected_agent": "pi", "expected_process": "pi"},
                "sentinels.openai",
                DEFAULT_OPENAI_SENTINEL_COMMAND,
            ),
        }
    if not isinstance(configured, dict) or set(configured) != {"anthropic", "openai"}:
        raise ManagementConfigError(
            "management.sentinels must define exactly anthropic and openai"
        )
    return {
        provider: _role(value, f"sentinels.{provider}", "")
        for provider, value in configured.items()
    }


def load_management_config(roster: dict) -> ManagementConfig:
    raw = roster.get("management", {})
    if not isinstance(raw, dict):
        raise ManagementConfigError("management must be an object")
    mode = raw.get("mode", "direct")
    if mode not in MANAGEMENT_MODES:
        raise ManagementConfigError(
            "management.mode must be one of " + ", ".join(MANAGEMENT_MODES)
        )
    rescue = raw.get("rescue_on_startup_failure", True)
    if not isinstance(rescue, bool):
        raise ManagementConfigError(
            "management.rescue_on_startup_failure must be a boolean"
        )
    return ManagementConfig(
        account_manager=_role(
            raw.get("account_manager"),
            "account_manager",
            DEFAULT_ACCOUNT_MANAGER_COMMAND,
        ),
        checker=_role(raw.get("checker"), "checker", DEFAULT_CHECKER_COMMAND),
        rescuer=_role(raw.get("rescuer"), "rescuer", DEFAULT_RESCUER_COMMAND),
        sentinel=_role(raw.get("sentinel"), "sentinel", DEFAULT_SENTINEL_COMMAND),
        sentinels=_sentinel_roles(raw),
        sentinel_is_override="sentinel" in raw,
        intake_clerk=_role(
            raw.get("intake_clerk"),
            "intake_clerk",
            DEFAULT_INTAKE_CLERK_COMMAND,
            max_timeout_ms=MAX_INTAKE_CLERK_TIMEOUT_MS,
        ),
        startup_timeout_ms=_positive_int(
            raw.get("startup_timeout_ms"), "startup_timeout_ms", 45_000
        ),
        inactivity_check_ms=_positive_int(
            raw.get("inactivity_check_ms"), "inactivity_check_ms", 900_000
        ),
        requester_grace_ms=_positive_int(
            raw.get("requester_grace_ms"), "requester_grace_ms", 300_000
        ),
        mode=mode,
        rescue_on_startup_failure=rescue,
    )


def render_role_command(
    role: ManagementRole, brief_path: Path, cwd: str, output_path: Path
) -> str:
    return role.command.format(brief_path=brief_path, cwd=cwd, output_path=output_path)


def render_intake_clerk_command(
    role: ManagementRole, brief_path: Path, cwd: str, output_path: Path
) -> str:
    """Render the trusted configured clerk command and atomically capture its stdout.

    The shipped command is no-tools. The roster is trusted executable configuration and may
    override that command, so roster changes require review. Every path substitution is
    shell-quoted; caller payload text never enters this command. The wrapper owns output
    persistence, so the shipped command needs no filesystem or shell tools.
    """
    command = role.command.format(
        brief_path=shlex.quote(str(brief_path)),
        cwd=shlex.quote(cwd),
        output_path=shlex.quote(str(output_path)),
    )
    temporary = output_path.with_name(
        f".{output_path.name}.{uuid.uuid4().hex}.stdout.tmp"
    )
    return (
        "set -eu; umask 077; "
        f"_out={shlex.quote(str(output_path))}; "
        f"_tmp={shlex.quote(str(temporary))}; "
        "trap 'rm -f -- \"$_tmp\"' EXIT HUP INT TERM; "
        f'{command} >"$_tmp"; '
        'mv -f -- "$_tmp" "$_out"; trap - EXIT HUP INT TERM'
    )


def _unclassified_section(unknown_fields: Sequence[str]) -> str:
    """Name the keys Python cannot classify so the clerk can map or refuse them, not drop them."""
    if not unknown_fields:
        return ""
    return (
        "## Keys Python does not recognize\n\n"
        "The submission carries these unrecognized keys: "
        + ", ".join(unknown_fields)
        + ". Python will not execute an order while any of them is unaccounted for. Either each "
        "one is another spelling of a canonical field — put its exact value under that field — or "
        "you must refuse and name it. Never drop one silently.\n\n"
    )


def _correction_section(correction: dict | None) -> str:
    """Hand this same role Python's authoritative errors so it can correct its own answer."""
    if correction is None:
        return ""
    errors = correction.get("errors") or []
    return (
        "## Correction round\n\n"
        "Your previous interpretation did not pass Python's provenance and contract checks. "
        "Python's findings are authoritative; do not argue with them.\n\n"
        "Your previous interpreted order:\n\n```json\n"
        + json.dumps(correction.get("order"), indent=2, sort_keys=True)
        + "\n```\n\nPython rejected it for:\n\n"
        + "".join(f"- {error}\n" for error in errors)
        + "\nReturn a corrected order that fixes exactly those findings using only values already "
        "present in the submission below, or return a refusal naming what the submission does not "
        "contain. Do not repeat the same interpretation.\n\n"
    )


def intake_clerk_brief(
    raw_text: str,
    raw_path: Path,
    output_path: Path,
    *,
    attempt: int = 1,
    attempt_limit: int = 1,
    unknown_fields: Sequence[str] = (),
    correction: dict | None = None,
) -> str:
    """One bounded normalization assignment. The clerk interprets form, never authority."""
    return f"""# UpAgent intake envelope normalization (attempt {attempt} of {attempt_limit})

A caller submitted one work-order envelope. Every submission reaches you: canonical JSON, malformed
JSON, prose, an incomplete object, unknown fields, or a request worded as specialist expertise. The
exact submitted bytes are preserved at `{raw_path}` and repeated below. Convert only its FORM into
canonical order fields, or refuse. An already-canonical submission is returned unchanged.
Do not execute the task, inspect a repository, launch an agent, or authorize an operation.

NEVER invent or change any execution intent. This includes target harness/model/effort,
agent/persona, cwd, task/instructions_path, cockpit_pane/requester, lifecycle mode,
operation/apply/approval or plan artifact, env, timeout, management placement, plan/phase/step
identity, consult authority, and watchdog identity. Values in the interpreted order must already
be explicitly present in the submitted payload. If a required value is absent, conflicting, or
ambiguous, refuse and name it. Python alone may generate bookkeeping identifiers, a result path,
and missing phase/stage bookkeeping after it independently verifies provenance.

Return STRICT JSON as your only stdout. Python captures that stdout at `{output_path}` and runs
all provenance and contract checks. Return exactly one of these shapes and nothing else:

```json
{{"order": {{"harness": "...", "model": "...", "agent": "...", "cwd": "...", "instructions_path": "...", "cockpit_pane": "..."}}, "notes": ["form-only change"]}}
```

```json
{{"refusal": "what is missing or ambiguous", "understood": ["explicit value"], "missing": ["field"]}}
```

{_unclassified_section(unknown_fields)}{_correction_section(correction)}----- BEGIN EXACT SUBMISSION -----
{raw_text}
----- END EXACT SUBMISSION -----
"""


def account_manager_brief(
    request_id: str,
    generation: int,
    order: dict,
    output_path: Path,
    mechanical_validation: dict | None = None,
) -> str:
    return f"""# Dedicated Account Manager review

You own conversation and interpretation for exactly one UpAgent request. Python owns durable
state and execution. Do not create, close, or kill a Herdr pane. Do not interrupt a pane or modify
the worker's result.

Request id: `{request_id}`
Generation: `{generation}`
Requested worker configuration:

```json
{json.dumps({key: order.get(key) for key in ("order_id", "harness", "model", "agent", "effort", "cwd")}, indent=2)}
```

Python mechanical validation (authoritative for its stated facts):

```json
{json.dumps(mechanical_validation or {"valid": True, "errors": []}, indent=2)}
```

Classify the request for advisory reporting only. You cannot approve or reject Python-valid
startup, mutate leases, publish artifacts, declare success, or terminalize a request. Python may
continue despite any classification. If Python later provides bounded invalid-artifact evidence,
you may recommend at most one repair to the original worker/address; never send the repair or
create a replacement worker yourself. An unsupported model, effort, persona, contradictory request,
or mechanical validation error is `needs-requester`; any mechanical validation error must be
reported verbatim. Explicit unrecoverable supplied-policy danger
is `blocked`; otherwise `approved` means only "no advisory concern observed". The route and roster
are authoritative: never infer restrictions from a model name or prior model knowledge. For
requester clarification, list concrete `requested_changes`.
Write exactly one JSON object to `{output_path}`:

```json
{{
  "request_id": "{request_id}",
  "generation": {generation},
  "decision": "approved|needs-requester|blocked",
  "message": "concise explanation for the requester",
  "requested_changes": ["optional concrete correction or clarification"]
}}
```
"""


RESCUER_VERDICTS = ("salvageable-done", "truly-blocked", "rerun")


def rescuer_brief(
    request_id: str, order_id: str, evidence_path: Path, output_path: Path
) -> str:
    """One bounded salvage assessment of contradictory artifact evidence.

    Reached only when Python's mechanical inspection could not decide. Everything this role
    says is advisory: the runner re-verifies every cited commit and file before a
    `salvageable-done` may become a terminal, and an uncorroborated citation is downgraded to
    `truly-blocked`. Acceptance requires at least one corroborated COMMIT — a corroborated
    file alone is never enough, and a cited file inside the order's own staging directory
    never corroborates at all, since that is worker-authored bytes the Rescuer would be
    citing right back at the runner.
    """
    return f"""# One-shot UpAgent salvage assessment

This assessment is advisory. A worker's pane vanished and Python's mechanical salvage
inspection found CONTRADICTORY evidence — artifacts that partly exist, or a ledger that
disagrees with what is on disk. Python supplied the bounded evidence bundle at
`{evidence_path}`: the ledger tail, the staging directory listing, the git log of the order's
worktree, and the last pane capture when one survived. Read it and, only when useful, read a
file it names.

Do not create, close, interrupt, or kill any pane.
Do not launch, delegate to, or resume a worker.
Do not write, repair, move, or delete any artifact, result, or commit.
Do not run the worker's task yourself.
You are reading evidence that already exists and saying what it shows.

You cannot mark work done. Python re-verifies every fact you cite — a commit SHA must resolve
to a real commit in that exact worktree, and a cited file must exist and parse. A file inside
the order's own staging directory (where the worker itself wrote) can never corroborate,
however cleanly it parses — that is worker-authored evidence, not independent proof. A
citation Python cannot corroborate is discarded, and `salvageable-done` is accepted only when
at least one cited COMMIT corroborates; citing files alone, however many, is never enough. An
uncorroborated or file-only verdict is recorded as `truly-blocked`. So cite only what you
actually observed in the bundle, and never invent, guess, complete, or round a SHA or a path.

The literal request id is `{request_id}` and the literal order id is `{order_id}`. Copy those
values exactly into the response. Directory names, path components, pane names, and evidence
fields are not request ids; never derive or substitute an identity from them.

Choose exactly one verdict:
- `salvageable-done` — the evidence shows the worker's work reached disk (a commit landed, or
  a readable artifact holds its output). Cite it.
- `truly-blocked` — the evidence shows no work survived.
- `rerun` — the evidence is genuinely undecidable from what is here.

Write exactly one JSON object to `{output_path}`:

```json
{{
  "request_id": "{request_id}",
  "order_id": "{order_id}",
  "verdict": "{'|'.join(RESCUER_VERDICTS)}",
  "cited_commits": ["full 40-character SHA observed in the evidence bundle"],
  "cited_files": ["absolute path observed in the evidence bundle"],
  "message": "concise explanation naming the evidence you relied on"
}}
```
"""


# The pulse interval is only the FALLBACK: the Sentinel's wait is event-driven on its
# per-attempt wake file (touched by the Recruiter on staging activity or worker death),
# so a short interval costs one cheap extra loop and the file arrives in seconds.
SENTINEL_PULSE_MINUTES = 5
# The wait exceeds the harness's DEFAULT foreground-command timeout (Claude Code's Bash
# tool defaults to 120 s), so the brief instructs running it with this explicit command
# timeout — proven to work live — and the pair must fit the 600 s hard cap. One
# mechanism only: explicit timeout, no sleep chunking.
SENTINEL_PULSE_COMMAND_TIMEOUT_MS = SENTINEL_PULSE_MINUTES * 60 * 1000 + 10_000
assert SENTINEL_PULSE_COMMAND_TIMEOUT_MS <= 600_000, (
    "the sentinel pulse wait plus its buffer must fit the harness's 600 s hard "
    "command-timeout cap"
)
SENTINEL_MAX_LANDING_EXCHANGES = 3


def _liftoff_deadline_phrase(deadline_ms: int) -> str:
    """The liftoff deadline as duty-brief prose. Whole minutes read as minutes; anything
    else (a clamped short order, e.g. 30s on a one-minute cap) reads as seconds so the
    Sentinel's instructions never round past the mechanical deadline."""
    if deadline_ms <= 0:
        raise ManagementConfigError(
            f"sentinel liftoff deadline must be positive, got {deadline_ms} ms"
        )
    if deadline_ms % 60_000 == 0:
        minutes = deadline_ms // 60_000
        return f"{minutes} minute{'' if minutes == 1 else 's'}"
    seconds = deadline_ms / 1000
    return f"{seconds:g} seconds"


def sentinel_brief(
    request_id: str,
    order_id: str,
    worker_pane: str,
    cwd: str,
    closeout_path: Path,
    *,
    liftoff_deadline_ms: int,
    wake_path: Path,
) -> str:
    """One Sentinel duty cycle: watch exactly one worker from liftoff to closeout.

    The Sentinel is eyes and interpretation only. Python spawned the worker, holds the
    kill switch and the verdict, and re-verifies every citation in the closeout before it
    counts. The closeout file is the one thing the Recruiter waits on for this request, so
    the brief is explicit about when and how to write it.

    `liftoff_deadline_ms` is the caller's EFFECTIVE clamped first-action deadline
    (`_first_action_deadline_ms`), threaded in so the Sentinel's LIFTOFF instructions
    always match the mechanical deadline — a short order clamps both, never just one.

    `wake_path` is this attempt's wake file. The Recruiter (Python) is its only
    writer — it touches the file on worker staging activity or proven worker death —
    and this Sentinel is its only consumer: the pulse wait blocks until the file
    exists or the interval elapses, then deletes it before checking the worker.
    """
    liftoff_deadline = _liftoff_deadline_phrase(liftoff_deadline_ms)
    pulse_wait_seconds = SENTINEL_PULSE_MINUTES * 60
    return f"""# UpAgent Sentinel — duty-bound to one worker

You watch exactly one worker for its whole lifecycle and then write exactly one closeout
file. You never kill, close, or interrupt any pane, never write or repair the worker's
artifacts, and never do the worker's task yourself. Python owns the kill switch and the
verdict; you own eyes and interpretation.

The literal request id is `{request_id}` and the literal order id is `{order_id}`. Copy
those values exactly into the closeout. The worker's Herdr pane is `{worker_pane}` and its
working directory is `{cwd}`.

Your tools for every observation:
- pane tail: `herdr pane read {worker_pane} --source recent-unwrapped --lines 80`
- work deltas: `git -C {cwd} log --oneline -5` and `git -C {cwd} status --porcelain`
- speak to the worker (dialogue only, never instructions to stop or exit):
  `herdr pane run {worker_pane} "<one short question>"`
- wait between pulses (your wake file is `{wake_path}`; the Recruiter WRITES THE WAKE
  REASON INTO THE FILE and can wake you in seconds — the {SENTINEL_PULSE_MINUTES}-minute
  interval is only the fallback). Run EXACTLY this one bounded command, never a bare
  long `sleep`, and ALWAYS run it with an explicit command timeout of
  {SENTINEL_PULSE_COMMAND_TIMEOUT_MS} ms — your harness's default foreground timeout is
  shorter than the wait and would kill it mid-block:
  `for i in $(seq {pulse_wait_seconds}); do [ -e "{wake_path}" ] && break; sleep 1; done; mv "{wake_path}" "{wake_path}.claimed" 2>/dev/null; cat "{wake_path}.claimed" 2>/dev/null; rm -f "{wake_path}.claimed"`
  The `mv` CLAIMS the wake atomically (a Recruiter write racing your claim lands as a
  fresh wake file for your next wait — nothing is lost), and the `cat` prints the wake
  reason (`valid-bundle`, `partial-staging`, `worker-gone`, or `never-started`); empty
  output means the interval simply elapsed.

## 1. LIFTOFF

Watch the pane until you see the worker's FIRST real tool action (a command running, a
file being edited, tool output appearing — not a banner, prompt, or greeting). Python
records the same signal mechanically; you corroborate it. If you see a first action,
note the evidence and continue to PULSE. If {liftoff_deadline} pass with no action,
write the closeout now with outcome `NEVER_STARTED` and cite the pane evidence you saw.

## 2. PULSE

Run the bounded wake-wait command above; it returns when your wake file appears OR
after {SENTINEL_PULSE_MINUTES} minutes, printing the wake reason and consuming the
file. Act on the printed reason:
- `valid-bundle` → the worker's bundle already passed the Recruiter's mechanical
  validation. Do NOT re-sleep and do NOT wait for the worker to go quiet — whether it
  is still printing output is irrelevant. Skip the landing dialogue: list and read the
  bundle files on disk yourself, then write the `COMPLETE` closeout immediately
  (Python revalidates the bundle when it consumes your closeout).
- `partial-staging` → the worker staged some-but-not-all files (or an invalid
  result). Go to LANDING now: steer it to finish the bundle by dialogue.
- `worker-gone` → the worker's pane/process is proven gone. Inspect the pane tail and
  git/fs deltas and write your closeout now.
- `never-started` → the worker recorded no first tool action by the deadline. It may
  still be live but idle — inspect the pane yourself, then write the `NEVER_STARTED`
  closeout now if the evidence warrants it.
- empty (interval elapsed) → an ordinary pulse: check the worker for quiet
  IMMEDIATELY, before anything else.
A prompt from the Recruiter (`SENTINEL_LANDING_RETRY` or `SENTINEL_STALL_RECHECK`)
OVERRIDES waiting: act on it at once. Each pulse: read the pane tail and the git/fs
deltas since your last pulse.
- Progressing → go back to sleep.
- Quiet → send ONE status nudge ("status? what are you on?"). If it resumes, log what you
  saw and sleep again.
- Unrecoverably stuck (repeated pulses with no output change, no deltas, and no answer to
  your nudge) → write the closeout with outcome `STALLED`, including `progress_so_far`
  (what verifiably got done, from the deltas) and `last_alive` (the last moment you saw
  real activity). A STALLED closeout is PROVISIONAL — do NOT exit after writing it.
  Stay idle: the Recruiter may nudge the worker itself and prompt you with
  `SENTINEL_STALL_NUDGED`, which authorizes you to resume PULSE and, if the worker
  stalls again, write a fresh STALLED closeout.

## 3. LANDING

When the worker goes quiet after real work, steer it to finalization by dialogue:
"finished?" — "did you write all N result files?". HARD RULE: never believe the worker's
answer. After EVERY exchange, list and read the bundle files on disk yourself before
concluding anything. At most {SENTINEL_MAX_LANDING_EXCHANGES} exchanges.
- Bundle verified on disk → closeout `COMPLETE` with the `bundle` path you verified, and
  `blocking_question` if the worker finished but is blocked on a question only the
  requester can answer.
- Exchange cap hit without a verified bundle → closeout `FINALIZATION_FAILED`, citing the
  evidence of what actually got done.

## 4. CLOSEOUT

Write exactly one JSON object to `{closeout_path}` and then exit — with one exception:
a `STALLED` closeout is provisional, so after writing it stay idle for the Recruiter's
disposition (`SENTINEL_STALL_NUDGED` resumes PULSE; a fresh STALLED closeout is then
allowed). Only a terminal closeout (`COMPLETE`, `NEVER_STARTED`, `FINALIZATION_FAILED`)
ends your duty. This file ends the
request, so write it when the worker's lifecycle has ended — never early,
never speculatively. Cite only what you actually observed: full 40-character commit SHAs
and absolute file paths. Python re-verifies every citation; one that does not check out
is discarded.

```json
{{
  "request_id": "{request_id}",
  "order_id": "{order_id}",
  "outcome": "COMPLETE|NEVER_STARTED|STALLED|FINALIZATION_FAILED",
  "interpretation": "concise account of what you observed and what it means",
  "citations": ["full 40-character commit SHA or absolute file path you observed"],
  "bundle": "absolute path of the verified bundle (COMPLETE only, else null)",
  "blocking_question": "the worker's open question for the requester, or null",
  "exchanges": [{{"question": "...", "answer": "...", "verified": false}}],
  "progress_so_far": "STALLED only: what verifiably got done",
  "last_alive": "STALLED only: last observed real activity"
}}
```
"""


def checker_brief(
    request_id: str, generation: int, evidence_path: Path, output_path: Path
) -> str:
    return f"""# One-shot UpAgent lifecycle assessment

This assessment is advisory. Python supplied bounded mechanical evidence at `{evidence_path}`.
Read it and, only when useful, read the named worker pane's recent output.
Do not create, close, interrupt, or kill any pane. Do not declare a verdict for the worker's task.

The literal request id is `{request_id}` and the literal generation is `{generation}`. Copy those
values exactly into the response. Directory names, path components, pane names, and evidence fields
are not request ids; never derive or substitute an identity from them.

Write exactly one JSON object to `{output_path}`:

```json
{{
  "request_id": "{request_id}",
  "generation": {generation},
  "assessment": "healthy|suspected-stall|startup-failed|completed|unknown",
  "confidence": 0.0,
  "evidence": ["specific observation"],
  "recommended_action": "none|ask-requester|retry-startup|inspect|extend|cancel",
  "message": "concise explanation for the requester"
}}
```
"""
