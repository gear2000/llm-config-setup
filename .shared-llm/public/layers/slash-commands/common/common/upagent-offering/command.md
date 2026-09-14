# Update UpAgent offerings

Use `$ARGUMENTS` as the requested offering change. Work only in the `llm-config-setup` kit checkout. If the request does not identify the offering id, harness, harness-native model id, offering set, provider, and allowed efforts, ask for the missing values before editing.

## Verify the model id

Verify every harness-native model id before changing files:

- Cursor: run `cursor-agent models`. Copy the exact id. Cursor offerings use only `default` because the model id carries its effort tier.
- Pi: run `pi --list-models <search>`. Keep the provider-qualified model id.
- Codex: use the bare model id accepted by Codex.
- Claude and ClaudeX: use the harness model id.

If the relevant CLI is unavailable or does not list the requested model, stop and report that. Do not guess.

## Edit the smallest valid set

For a routine offering, update these files together:

1. `.shared-llm/public/extensions/common/upagent/offerings.py`
   - Add or change the `APPROVED` entry.
   - Keep its order aligned with the offering fragment because that order drives `STANDARD_IDS`.
   - Change `APPROVED_SETS` only when adding or changing a set.
2. `.shared-llm/public/extensions/common/upagent/offerings.d/<set>.yaml`
   - Keep `harness`, `model`, and `efforts` identical to `APPROVED`.
3. `.shared-llm/public/extensions/common/upagent/offerings_test.py`
   - Update only fixed inventory assertions affected by the change, including roster counts, id sets, provider maps, rendered identities, and the standard roster SHA-256 when applicable.
4. `.shared-llm/public/extensions/common/upagent/README.md`
   - Update the stable offering count and harness breakdown when they change.

Apply these branches only when requested:

- New offering set: add `offerings.d/<set>.yaml`, update `APPROVED_SETS`, and tell the user that machines must select the set with `upagent.offering_sets` in `~/.shared-llm.yaml`.
- Management defaults: update `offerings-management.yaml` and its focused assertions.
- Specialist pin: update the applicable `specialists.yaml` source and focused assertions.
- New harness, completion style, or preflight: update the code-owned policy in `offerings.py` and add focused tests.

`offerings.yaml` is generated. Never edit it by hand. Do not change `public_contract.py`, `public_api_test.py`, or `recruiter_test.py` for a routine roster addition unless a focused failure proves that the contract changed.

## Verify without the full suite

Run this tight loop:

1. `pytest .shared-llm/public/extensions/common/upagent/offerings_test.py -q`
2. `just update`
3. Run `just update` again. The second run must leave no new diff.
4. Inspect the generated `offerings.yaml` and the complete git diff. Confirm that fragment YAML and `APPROVED` agree.

Do not run `just test` as part of this command. Run extra tests only when the change crosses a branch named above or the focused test exposes a wider contract change. Report every command and result.

This is a public repository. Check the complete diff for private names, infrastructure details, account data, credential paths, secrets, and private design details. Do not commit or push unless the user asks. Before any push, obtain the independent public-safety review required by `AGENTS.md`.
