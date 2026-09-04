# Updating UpAgent offerings

Guide for adding or changing public UpAgent worker offerings in the llm-config-setup kit.
YAML fragments declare harness/model/efforts only; `offerings.py` owns provider metadata,
command rendering, health identity, and preflight. Fragment + `APPROVED` must stay in sync.

When the kit skill exists, prefer invoking `/upagent-offering` instead of pasting the prompt below.

## File matrix

| Scenario | Edit | Usually skip |
|---|---|---|
| New model in **standard** set | `offerings.py`, `offerings.d/standard.yaml`, `offerings_test.py`, README counts | `claudex.yaml`, `public_contract.py`, `public_api_test.py`, `recruiter_test.py` |
| New **ClaudeX** offering | above + `offerings.d/claudex.yaml`, `APPROVED_SETS["claudex"]` if new id | management yaml |
| New **offering set** (not standard/claudex) | new `offerings.d/<set>.yaml`, extend `APPROVED_SETS` in `offerings.py`, destination `upagent.offering_sets` in `~/.shared-llm.yaml` | — |
| Change management defaults | `offerings-management.yaml` + offerings_test management assertions | worker YAML fragments |
| Pin specialist to new offering | `specialists.yaml` (kit + optional repo overlay) | offerings fragments |
| New harness / completion style / preflight | `offerings.py` (`COMPLETION_STYLES`, `MANAGEMENT_HEALTH`, `render_argv`, maybe `preflight_snapshot`) + tests | YAML cannot add commands |

**Generated (never hand-edit):** `offerings.yaml` — produced by `just update` from fragments +
`offerings-management.yaml`.

## Reusable agent prompt

Copy, fill in the bracketed values, and give to an agent:

```
Add UpAgent public offering(s) to the llm-config-setup kit.

## New offering(s) to add
- Offering id(s): [e.g. cursor-gpt-6-astra, pi-gpt-6-astra, codex-gpt-6-astra]
- Harness per id: [claude | claudex | codex | cursor | pi]
- Harness-native model id per id: [verify before editing — see below]
- Offering set: [standard | claudex | new set name]
- Provider per id: [anthropic | openai | cursor | openrouter | xai | …]

## Verify model ids BEFORE editing
- Cursor: run `cursor-agent models` and copy the exact id (includes embedded effort tier, e.g. gpt-5.6-terra-high). Public Cursor offerings use efforts: [default] only.
- Pi: run `pi --list-models <search>` — model is provider-qualified (e.g. openai-codex/gpt-6-astra).
- Codex: bare model id (e.g. gpt-5.6-sol).
- Claude / ClaudeX: harness model id matches offering id pattern.

## Files to edit (standard-set worker offering)
MUST edit together — YAML declares only; code owns provider, argv, health, preflight:
1. `.shared-llm/public/extensions/common/upagent/offerings.py`
   - Add entry to `APPROVED` dict: (harness, model, efforts_tuple, provider)
   - Insert in dict order matching YAML (drives `STANDARD_IDS`)
   - If new offering set: extend `APPROVED_SETS`
   - Only if new harness/completion/preflight: `COMPLETION_STYLES`, `MANAGEMENT_HEALTH`, `render_argv`, `preflight_snapshot`
2. `.shared-llm/public/extensions/common/upagent/offerings.d/standard.yaml` (or claudex.yaml / new offerings.d/<set>.yaml)
   - harness, model, efforts must match `APPROVED`
   - codex: completion_style: exec; cursor/claude/pi/claudex: interactive (or omit — code default)
3. `.shared-llm/public/extensions/common/upagent/offerings_test.py`
   - Roster count, cursor id sets, parametrize tables, provider map, rendered_identities
   - Recompute `test_standard_render_is_the_pre_set_roster_byte_for_byte` SHA-256 after changes
4. `.shared-llm/public/extensions/common/upagent/README.md`
   - Update stable id count and harness breakdown in "Changing models" section

## Files to SKIP for a routine standard worker offering
- `public_contract.py` — CLI grammar only, no roster
- `public_api_test.py` — dynamic roster, no fixed inventory
- `recruiter_test.py` — no hardcoded offering list
- `offerings.d/claudex.yaml` — unless adding ClaudeX set member
- `offerings-management.yaml` — unless changing Account Manager / Checker / Sentinel candidates
- `offerings.yaml` — GENERATED; never hand-edit. Run `just update`.

## Optional / scenario-specific
- New ClaudeX offering: also `offerings.d/claudex.yaml` + `APPROVED_SETS["claudex"]`
- New offering set: new `offerings.d/<name>.yaml`, `APPROVED_SETS` in offerings.py, machine config `upagent.offering_sets` in ~/.shared-llm.yaml
- Pin specialist to offering: `specialists.yaml` (kit + optional repo overlay)
- Change management defaults: `offerings-management.yaml` + offerings_test management assertions

## Verify after edits
1. pytest .shared-llm/public/extensions/common/upagent/offerings_test.py
2. just update  (then again — second run must show zero changes)
3. just test

## Invariant
Fragment YAML + `APPROVED` must stay in sync. Unknown ids, partial sets, commands in YAML, or mismatched efforts fail loudly at load time.
```

## Verification loop

1. `pytest .shared-llm/public/extensions/common/upagent/offerings_test.py`
2. `pytest .shared-llm/public/extensions/common/upagent/recruiter_test.py -k offering` (optional)
3. `just update` twice — second run must report zero changes
4. `just test`
