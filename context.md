# Code Context

## Files Retrieved
1. `.shared-llm/public/extensions/common/upagent/recruiter.py` (lines 163-195, 2588-2601) - runtime timeout constants and fallback selection.
2. `.shared-llm/public/extensions/common/upagent/public_api.py` (lines 345-358, 633-656) - public duration validation and public-request-to-order mapping.
3. `.shared-llm/public/extensions/common/upagent/recruiter_test.py` (lines 1515-1524, 4132-4139) - assertions pinning the 30-minute retained/public fallback and separate 3-hour stage defaults.
4. `.shared-llm/public/extensions/common/upagent/public_api_test.py` (lines 240-262) - test pinning the omitted-duration order shape (no `timeout_ms`) and accepting the 120-minute maximum.
5. `.shared-llm/public/extensions/common/upagent/contracts.py` (lines 131-155) - retained-worker hard maximum of 7,200,000 ms / 120 minutes.
6. `.shared-llm/public/extensions/common/upagent/contracts_test.py` (lines 66-68) - test pinning the 120-minute retained maximum.
7. `.shared-llm/public/layers/slash-commands/common/common/upagent-run/command.md` (lines 1-18) - source documentation for `/upagent-run`; explicitly says omitted duration defaults to 30 minutes and only forwards the flag when supplied.
8. `.shared-llm/public/layers/slash-commands/common/common/upagent-run/description.md` (line 1) - documents the valid 1-120-minute range; no default value change is needed here.
9. `.shared-llm/public/extensions/common/upagent/README.md` (lines 81-107) - public CLI syntax and explicit statement that omission preserves the 30-minute default.
10. `.shared-llm/public/llm/pi/common/meta-plan/meta-plan-format.md` (line 13) - unrelated internal stage documentation: stages 1 and 2 default to 3 hours; must remain unchanged.

## Key Code

### Runtime source of the current 30-minute fallback

`.shared-llm/public/extensions/common/upagent/recruiter.py:169-190`:

```python
DEFAULT_TIMEOUT_MS = 1_800_000  # 30 min per worker unless the order overrides
MAX_RETAINED_TIMEOUT_MS = 7_200_000  # 120 min across coding plus requester review
...
STAGE_TIMEOUT_MS = {
    "stage-1-implementation": 10_800_000,
    "stage-2-adversarial-audit": 10_800_000,
}
```

`.shared-llm/public/extensions/common/upagent/recruiter.py:2588-2601`:

```python
def _default_timeout_ms(stage_id: str) -> int:
    return STAGE_TIMEOUT_MS.get(stage_id, DEFAULT_TIMEOUT_MS)


def _order_timeout_ms(order: dict) -> int:
    """Keep retained workers on the public 30-minute default unless explicitly extended."""
    explicit = order.get("timeout_ms")
    if isinstance(explicit, int) and not isinstance(explicit, bool):
        return explicit
    if order.get("completion_policy") == "requester_release":
        return DEFAULT_TIMEOUT_MS
    return _default_timeout_ms(order["stage_id"])
```

**High-severity coupling/risk:** `DEFAULT_TIMEOUT_MS` is not exclusively public. It is also the fallback for every ordinary stage not listed in `STAGE_TIMEOUT_MS` (the test explicitly demonstrates stage 3). Blindly changing it to `3_600_000` makes the public default 60, but also changes unrelated internal/stage fallback defaults from 30 to 60. Stages 1 and 2 remain independently fixed at 3 hours.

### Public order path

`.shared-llm/public/extensions/common/upagent/public_api.py:349-358` validates an explicit duration as integer `1 <= duration_minutes <= 120`. At lines 633-656, public worker requests are emitted as `stage_id: "stage-5-finalization"`, and `timeout_ms` is added only if the caller supplied `duration_minutes`:

```python
**(
    {"timeout_ms": cast(int, payload["duration_minutes"]) * 60_000}
    if "duration_minutes" in payload
    else {}
),
```

Thus `/upagent-run` omission currently reaches Recruiter without `timeout_ms` and inherits `DEFAULT_TIMEOUT_MS`.

### Exact changes needed for a public-only 60-minute default

**Runtime (high):** Do not merely change `recruiter.DEFAULT_TIMEOUT_MS` unless changing internal non-special stage fallbacks is intended. The least-coupled change is to materialize the public default in `.shared-llm/public/extensions/common/upagent/public_api.py` when registering a public worker order: use `3_600_000` when `duration_minutes` is omitted, while preserving explicit values. Prefer a named `PUBLIC_DEFAULT_DURATION_MINUTES = 60` (or millisecond equivalent) near the public API constants rather than a literal. This preserves Recruiter's existing internal `DEFAULT_TIMEOUT_MS`, its 3-hour `STAGE_TIMEOUT_MS`, and the 10-minute consult timeout.

**Tests (high):** Update `.shared-llm/public/extensions/common/upagent/public_api_test.py:240-262`. Its current assertion `"timeout_ms" not in default_order` must instead assert the public default order contains `timeout_ms == 3_600_000`; retain/assert the explicit 120-minute case yields `7_200_000`.

**Recruiter test/docstring (conditional):** If the chosen implementation instead introduces a distinct Recruiter public constant/fallback, update `.shared-llm/public/extensions/common/upagent/recruiter.py:2593-2600` and `.shared-llm/public/extensions/common/upagent/recruiter_test.py:1515-1524` from 30/`1_800_000` to 60/`3_600_000`. With the recommended public-API explicit-default approach, these lines describe generic missing-timeout retained orders rather than `/upagent-run` orders and should not be changed without deciding that all retained internal orders also get 60.

**Documentation (medium):**
- `.shared-llm/public/layers/slash-commands/common/common/upagent-run/command.md:5`: change “omitted keeps the 30-minute default” to 60.
- `.shared-llm/public/extensions/common/upagent/README.md:107`: change “omission preserves the 30-minute default” to 60.

**Maximum (do not change):**
- `.shared-llm/public/extensions/common/upagent/public_api.py:349-358`: retain `1..120` validation.
- `.shared-llm/public/extensions/common/upagent/contracts.py:147-155`: retain the `7_200_000` / 120-minute retained cap.
- `.shared-llm/public/extensions/common/upagent/contracts_test.py:66-68`: retain maximum test.
- `.shared-llm/public/extensions/common/upagent/recruiter.py:170`: retain `MAX_RETAINED_TIMEOUT_MS = 7_200_000` (not currently referenced elsewhere, but documents the limit).
- `/upagent-run` description and command range references remain `1–120`.

## Architecture

`/upagent-run` is composed from the layer source and invokes `just upagent request`. It omits `--duration-minutes` when the user does not provide it. `public_api.py` validates the request and registers a stage-5 order, currently omitting `timeout_ms` in that case. Recruiter then resolves the missing timeout through `_order_timeout_ms` -> `_default_timeout_ms` -> `DEFAULT_TIMEOUT_MS` (30 minutes). Explicit public durations become milliseconds before Recruiter and bypass all defaults.

The important boundary is that Recruiter's `DEFAULT_TIMEOUT_MS` is shared with internal ordinary-stage fallback behavior. Public defaulting at the public API boundary isolates `/upagent-run` from internal/stage defaults. The 120-minute public validation and retained contract cap are separate and should remain unchanged.

## Start Here

Open `.shared-llm/public/extensions/common/upagent/public_api.py` at lines 633-656 first. It is the narrowest public boundary at which omission can become an explicit 60-minute timeout without altering unrelated Recruiter stage defaults.

## Review Findings

- **High:** A one-line `DEFAULT_TIMEOUT_MS = 3_600_000` change in `recruiter.py` has collateral effect on internal stages not present in `STAGE_TIMEOUT_MS`, proven by `recruiter_test.py:4132-4139`.
- **High:** The public API currently deliberately preserves an absent `timeout_ms`, pinned by `public_api_test.py:240-262`; implementing a public-only default requires changing that behavior and test.
- **Medium:** Two source documentation statements explicitly say 30 minutes and must be synchronized.
- **None:** Maximum remains independently enforced at 120 minutes in public validation and retained contracts.

## Residual Risks

- The phrase “public default” could be intended to include every retained `requester_release` order, including internal callers. The recommended boundary change covers `/upagent-run` and all requests through `public_api.py`, but intentionally leaves manually constructed/internal missing-timeout retained orders at 30 minutes. A product decision is needed only if those should also become 60.
- `MAX_RETAINED_TIMEOUT_MS` is declarative in `recruiter.py`; the actual cap is enforced in `contracts.py`. Keep both at 7,200,000 to avoid documentation drift.
- Generated `CLAUDE.md`/skill artifacts must not be hand-edited; regenerate through the repository composer after changing layer sources.

```acceptance-report
{
  "criteriaSatisfied": [
    {
      "id": "criterion-1",
      "status": "satisfied",
      "evidence": "review-findings identify the shared Recruiter default as high severity, enumerate exact runtime/tests/docs paths and line ranges, and residual-risks distinguish public API requests from internal retained/stage orders."
    }
  ],
  "changedFiles": [],
  "testsAddedOrUpdated": [],
  "commandsRun": [
    {
      "command": "grep/find/read targeted searches for upagent-run, 30-minute defaults, timeout constants, validation, and stage defaults",
      "result": "passed",
      "summary": "Located runtime default/cap, public mapping, tests, source docs, and unrelated stage defaults."
    }
  ],
  "validationOutput": [
    "Public omission currently emits no timeout_ms and falls through to recruiter.DEFAULT_TIMEOUT_MS=1800000.",
    "Explicit public duration is constrained to 1..120 and converted to milliseconds.",
    "Stages 1 and 2 independently default to 10800000; other internal stages share DEFAULT_TIMEOUT_MS."
  ],
  "residualRisks": [
    "Public-only defaulting at public_api.py leaves manually constructed internal requester_release orders at 30 minutes; confirm whether that distinction is desired.",
    "Changing recruiter.DEFAULT_TIMEOUT_MS directly would also alter unrelated internal stage fallbacks."
  ],
  "noStagedFiles": true,
  "notes": "Read-only investigation; context.md is the requested report artifact and no source files were modified."
}
```
