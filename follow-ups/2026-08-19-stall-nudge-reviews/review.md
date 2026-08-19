# Adversarial review — UpAgent stall-nudge ladder

## Findings

### 1. BLOCKER — The advertised lease/generation/attempt fence does not exist at delivery time

**Location:** `.shared-llm/public/extensions/common/upagent/recruiter.py:5930-5966`, `.shared-llm/public/extensions/common/upagent/recruiter.py:6016-6022`, `.shared-llm/public/extensions/common/upagent/recruiter.py:7036-7077`, `.shared-llm/public/extensions/common/upagent/recruiter.py:11952-11966`

**What breaks:** `_live_worker_journal()` returns the newest started worker without checking its `attempt`, `generation`, `lease_token`, pane, or address against this `_SentinelWatch`. The generation and attempt at lines 5961-5962 are used only to hash/log the intent; they fence nothing. A recovering generation can therefore start attempt 2 while an old attempt-1 watcher is still runnable: the old watcher positively probes its old pane, takes attempt 2's newer address, and sends `continue` to the replacement worker under an attempt-1 digest. I reproduced that exact setup; an attempt-1 watch delivered to a synthetic attempt-2/generation-2 journal.

The state gate is also a lock-free, fail-open snapshot: unreadable/unknown state is accepted, and cancellation can change `latest.json` after line 5950 but before line 6017. I reproduced that interleaving by switching the state to `cancelling` during `load_state()`; `continue` was still delivered. `_PROMPT_SUBMISSION_LOCK` is process-local, so it does not serialize the runner against separate `cancel` or requester-message processes. The idle wait similarly has a TOCTOU window before `pane run`, so the README's foreground-state rejection claim is stronger than the fence actually provided.

**Smallest fix:** reserve a nudge under `JobLedger._claim_lock`: require the active lease token/generation, state exactly `running`, and an exact journal match for attempt/generation/pane/address. Make cancellation and other worker-delivery paths observe that bounded reservation, then revalidate/commit it at send. Also fail closed on unreadable or non-`running` state. This is meaningful lifecycle complexity, but it is required for the core safety claim; do not ship the automatic delivery without it.

### 2. MAJOR — A completed worker is nudged before the completion monitor gets priority

**Location:** `.shared-llm/public/extensions/common/upagent/recruiter.py:4686-4706`, `.shared-llm/public/extensions/common/upagent/recruiter.py:4850-4878`, `.shared-llm/public/extensions/common/upagent/recruiter.py:6183-6184`

**What breaks:** both wait loops call `sentinel_watch.poll()` before `landing_pass()`. If a valid bundle and a STALLED closeout are present together, `poll()` sends `continue`, archives the stall, and only the next step notices completion. I staged a bundle that passed `completion.validate_bundle()`, added a corroborated STALLED closeout, and observed `watch.poll()` send `continue` to the worker. The resumed worker can modify already-valid output or repeat a side effect after completion won the race.

**Smallest fix:** give mechanically valid completion priority over a STALLED nudge. Thread the monitor/validation result into the nudge decision (and recheck immediately before the send); suppress the nudge and continue through the existing landing/completion path when the bundle is valid. Add the valid-bundle-plus-STALLED race test without monkeypatching away bundle validation.

### 3. MAJOR — The Sentinel is still instructed to exit after its first STALLED closeout

**Location:** `.shared-llm/public/extensions/common/upagent/llm_management.py:28-31`, `.shared-llm/public/extensions/common/upagent/llm_management.py:460-461`, `.shared-llm/public/extensions/common/upagent/llm_management.py:538-542`, `.shared-llm/public/extensions/common/upagent/recruiter.py:6043-6064`

**What breaks:** the ladder requires the same Sentinel to resume PULSE and publish later STALLED closeouts, but its command and brief still say to write exactly one closeout and exit. A compliant Sentinel can exit immediately after publishing the first STALLED file; the best-effort resume prompt then gets `agent_not_found`, supervision degrades, and rungs 2/3 can never be reached. The new tests replace `_submit_agent_prompt` with a list append, so they cannot expose this teardown race.

**Smallest fix:** change only the Sentinel contract text/command: a STALLED closeout is provisional, the Sentinel must remain idle for the hub's disposition, and `SENTINEL_STALL_NUDGED` authorizes another PULSE/closeout; terminal non-STALLED closeouts may still exit. Add one drill in which the fake Sentinel follows the actual lifecycle rather than merely accepting a captured prompt.

### 4. MAJOR — Corrupt durable nudge state escapes the normal fail-loud terminal path

**Location:** `.shared-llm/public/extensions/common/upagent/stall_nudge.py:44-53`, `.shared-llm/public/extensions/common/upagent/recruiter.py:5963-5966`, `.shared-llm/public/extensions/common/upagent/recruiter.py:7918`

**What breaks:** `load_state()` raises `StallNudgeError`, a `ValueError`, while `_run_order()` does not catch `ValueError` or `StallNudgeError`. A truncated/unreadable `nudges.json` therefore escapes the ordinary `SentinelStalledError` → blocked-bundle/receipt path and can leave the request active without its required terminal artifacts. I reproduced `watch.poll()` raising `StallNudgeError` (not `RecruiterError`) from a corrupt `nudges.json`; all 811 repository tests still pass because the pure test asserts only that corruption raises.

**Smallest fix:** catch `StallNudgeError` at `_attempt_stall_nudge`, emit one typed invalid-state event, and return `False` so the exact pre-ladder `SentinelStalledError` path runs. Validate each stored nudge item's `at`, `digest`, and `delivered` fields while loading so malformed JSON objects fail through the same route.

### 5. MAJOR — Cursor nudges use the delivery path already documented as non-submitting

**Location:** `.shared-llm/public/extensions/common/upagent/recruiter.py:6016-6022`, `.shared-llm/public/extensions/common/upagent/recruiter.py:7044-7051`

**What breaks:** `_attempt_stall_nudge()` omits `paste_settle_seconds`, so Cursor gets the default atomic `pane run`. The helper's own contract says Cursor requires split `send-text` + settle + Enter; otherwise the prompt remains drafted and never runs. The ladder nevertheless marks the nudge delivered and can spend all three rungs without resuming the worker. `_nudgeable()` monkeypatches the entire submit function and never inspects this argument.

**Smallest fix:** pass `CURSOR_PROMPT_PASTE_SETTLE_SECONDS` when `order["harness"] == "cursor"`, exactly as the existing repair/requester-message paths do, and assert the real submit-adapter calls in a Cursor test.

### 6. MAJOR — Exhaustion publication is not idempotent

**Location:** `.shared-llm/public/extensions/common/upagent/recruiter.py:5967-5984`, `.shared-llm/public/extensions/common/upagent/sentinel_test.py:2255-2279`, `.shared-llm/public/extensions/common/upagent/README.md:379-381`

**What breaks:** exhausted state has no durable `escalated` marker or idempotency check. Every later accepted STALLED closeout emits another `worker-stall-escalation` event and another requester-mailbox message. Calling `poll()` twice over exhausted state produced two escalation events and two mailbox messages. The test named “escalate once” invokes `poll()` only once, so it proves existence rather than exactly-once behavior; runner recovery after a publication crash remains a duplicate window.

**Smallest fix:** give escalation a deterministic idempotency identity for this request generation/attempt and make requester-mailbox publication idempotent on that identity before raising the original stall. A plain “write a flag then publish” swaps duplication for message loss, so this needs a small atomic/deterministic publication seam rather than only an in-memory boolean.

### 7. MAJOR — The opt-in cross-provider gate can approve a same-provider configured Sentinel

**Location:** `.shared-llm/public/extensions/common/upagent/recruiter.py:5483-5506`, `.shared-llm/public/extensions/common/upagent/llm_management.py:93-126`, `.shared-llm/public/extensions/common/upagent/recruiter.py:12464-12485`, `.shared-llm/public/extensions/common/upagent/README.md:382-386`

**What breaks:** management configuration explicitly permits overriding the Sentinel command/process, but the gate always hardcodes the Sentinel provider as Anthropic. With `UPAGENT_REQUIRE_CROSS_PROVIDER_SENTINEL=1`, a configured Codex/OpenAI Sentinel and an OpenAI worker are approved because Python compares the worker only to the hardcoded `anthropic` value. The stated disjointness invariant is then false. Separately, the two providers are placed only in the ephemeral `sentinel_state`; neither the durable `sentinel-hired` event nor requester message records them, contrary to the README.

**Smallest fix:** under the opt-in flag, fail closed when the configured Sentinel command/provider is not the known shipped Anthropic command (or derive a known provider from that configured role); pass that value into the conflict check. Include both provider values in the existing durable `sentinel-hired` event. This stays within the approved env-gate design and does not require offering metadata.

### 8. MINOR — Repeated held closeouts overwrite their own evidence archive

**Location:** `.shared-llm/public/extensions/common/upagent/recruiter.py:5985-5995`

**What breaks:** `ordinal` is `len(nudges)+1`, which does not change while a rung is in backoff. Every repeated closeout during that window is renamed to the same `closeout.stalled-held-N.json`, and `os.replace` silently destroys the previous archive. I submitted two held closeouts after nudge 1: two `worker-nudge-held` events remained, but only one file survived, containing the later interpretation.

**Smallest fix:** add `time.time_ns()` or a UUID to held archive names; keep the nudge ordinal as a separate event field.

### 9. MINOR — Failed delivery is reported to the Sentinel as successful delivery

**Location:** `.shared-llm/public/extensions/common/upagent/recruiter.py:6016-6040`, `.shared-llm/public/extensions/common/upagent/recruiter.py:6048-6058`

**What breaks:** after `_submit_agent_prompt()` raises and `worker-nudge-failed` is recorded, the unconditional `held=False` resume prompt says “the hub delivered a 'continue' nudge.” The Sentinel then reasons from a false observation, even though the rung was merely spent/refused.

**Smallest fix:** pass the actual delivery outcome into `_prompt_sentinel_after_nudge()` and say “delivery failed and this rung was spent” for the failure branch.

## Test execution

- `python3 -m pytest .shared-llm/public/extensions/common/upagent/stall_nudge_test.py -q` — **12 passed**
- `python3 -m pytest .shared-llm/public/extensions/common/upagent/sentinel_test.py -q` — **72 passed**
- `python3 -m pytest .shared-llm/public/extensions/common/upagent -q` — **811 passed**
- Additional read-only induced probes reproduced: attempt-1 delivery to an attempt-2 journal; delivery after state changed to `cancelling`; delivery despite a mechanically valid completed bundle; uncaught corrupt-state `StallNudgeError`; duplicate exhaustion messages; and held-archive overwrite.

## Verdict

**CHANGES_REQUIRED**

The intended increment remains appropriately narrow—one literal nudge, one bounded ladder, and an opt-in provider gate—but the current integration does not yet stay a safe “slight improvement.” In particular, it adds an unfenced delivery across cancellation/recovery/completion races and depends on a Sentinel that its unchanged brief tells to exit. The fixes should remain targeted to the existing lifecycle (reservation/fence, completion priority, corrected Sentinel brief, Cursor adapter, error containment, and idempotent escalation); no broader supervision subsystem or new configuration surface is warranted.
