# Verification round 2 — stall-nudge review dispositions

## Verdict

**CHANGES_REQUIRED**

The targeted fixes are present for six findings, but three original failure modes remain reachable. This verdict accepts the owner's cheaper fencing and publish-then-flag designs; it does not require the previously proposed mutation-lock reservation or atomic escalation seam.

## Per-finding disposition

| # | Finding | Status | Verification |
|---|---|---|---|
| 1 | Delivery fencing | **INADEQUATE** | The attempt/generation journal match and fail-closed unreadable-state checks are present, and the state gate is called again before `_submit_agent_prompt`. However, that call is not immediately before delivery in practice: `_submit_agent_prompt` can spend up to 120 seconds in `agent wait` before resolving the pane and sending (`recruiter.py:6060-6082`, `recruiter.py:7126-7164`). A cancellation can set `latest.json` to `cancelling` during that wait, after the last gate, and the eventual `pane run`/Cursor Enter still has no state recheck. An induced probe flipped the state inside the submission adapter and still observed `continue` plus `worker-nudge-delivered`. **Smallest fix:** keep the cheap design, but add a nudge-only pre-send guard inside the prompt adapter, after `agent wait`/`agent get` and immediately before the pane send, which re-runs `_nudge_state_allows`. No mutation-lock reservation is required for this fix. |
| 2 | Completion priority | **INADEQUATE** | A bundle that is valid at the initial check correctly produces `worker-nudge-superseded` and `closeout.stalled-superseded.json` before `nudges.json` is touched (`recruiter.py:5969-5991`). But the same up-to-120-second prompt-idle wait follows that check. A worker can finish and stage a valid bundle while the adapter waits for idle; the adapter then sends `continue` without another completion check. An induced probe staged a valid bundle on entry to the submission adapter and still observed `continue` plus `worker-nudge-delivered`. **Smallest fix:** use the same nudge-only pre-send guard after idle resolution to revalidate the bundle; suppress the pane send and take the existing superseded archive/event path when it validates. |
| 3 | Provisional Sentinel STALLED lifecycle | **CONFIRMED-FIXED** | `llm_management.py` now explicitly says a STALLED closeout is provisional, requires the Sentinel to stay idle, and authorizes PULSE to resume only on `SENTINEL_STALL_NUDGED`; terminal closeouts still exit. |
| 4 | `StallNudgeError` containment | **INADEQUATE** | Corrupt JSON is caught around `load_state`, emits `worker-nudge-state-invalid`, and reaches the pre-ladder `SentinelStalledError`. Structurally malformed but parseable records are still accepted by `load_state`, though: `{"nudges":[{}]}` raises uncaught `KeyError('at')` in `decide`, and a string `at` raises uncaught `TypeError`. Those errors occur after the narrow `StallNudgeError` catch and still bypass the pre-ladder terminal path (`stall_nudge.py:33-53`, `recruiter.py:5993-6004`). **Smallest fix:** validate every loaded nudge as an object with numeric `at`, non-empty string `digest`, and boolean `delivered` (and validate `escalated` when present), raising `StallNudgeError` from `load_state`. |
| 5 | Cursor delivery adapter | **CONFIRMED-FIXED** | Cursor now receives `CURSOR_PROMPT_PASTE_SETTLE_SECONDS`; the integration test inspects the adapter call. |
| 6 | Escalation idempotency | **CONFIRMED-FIXED** | Exhaustion checks durable `state["escalated"]`, publishes first, then saves `escalated: true`. Repeated stalls do not republish. This accepts the owner's documented rare crash-window duplicate and does not require an atomic publication seam. |
| 7 | Cross-provider gate and audit data | **CONFIRMED-FIXED** | With the opt-in gate enabled, any non-default Sentinel command fails closed. The durable requester `sentinel-hired` payload includes both `worker_provider` and `sentinel_provider`. |
| 8 | Held closeout archives | **CONFIRMED-FIXED** | Held archive names now include `time.time_ns()`, and repeated held closeouts survive as distinct files. |
| 9 | Failed-delivery Sentinel prompt | **CONFIRMED-FIXED** | The Sentinel receives the explicit message that delivery failed and the rung was spent; successful-delivery wording is no longer used on that branch. |

## Verification run

- `python3 -m pytest .shared-llm/public/extensions/common/upagent -q` — **821 passed** (run twice: 76.45s and 82.44s)
- `python3 -m pytest .shared-llm/public/extensions/common/upagent/sentinel_test.py .shared-llm/public/extensions/common/upagent/stall_nudge_test.py -q` — **94 passed**
- Read-only induced probes confirmed the residual state-flip-after-final-gate race, completion-during-prompt-wait race, and malformed-record `KeyError`/`TypeError` escape.
