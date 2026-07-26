# Shared cc/do-plan contract

Produce one approved human-readable implementation plan. Do not decompose it for Herdr and do not implement it.

## Invocation

```text
/<command> <topic> [@research.md ...] [--dir <path>] [--adversarial-iterations N] [--adversary-profile <profile>]
```

- `--adversarial-iterations N` defaults to `2`.
- `N=0` is an explicit opt-out.
- Cap unattended review at `4` rounds. If the user asks for more, stop after round 4 and ask for a fresh human decision.
- `--adversary-profile <profile>` selects the UpAgent review profile without prompting, but it must still resolve through the configured UpAgent roster.

## Plan directory

Resolve one plan directory before writing anything by running `.shared-llm/public/llm/common/common/planish_resolve.py --topic <topic> [--dir <path>]` and reading its `plan_dir` JSON result; do not reproduce this selection in model prose. The resolver's precedence is: explicit `--dir <path>`, then `$WORK_LOG_DIR`, then `$PLANISH_DIR` (deprecated — still honored, warns on stderr), then the nearest `.shared-llm.yaml` with a `work_log:` mapping found by walking upward from the current directory, then the nearest `.planish.yaml` walking upward (deprecated — still honored, warns on stderr), then `/var/tmp/work-log/{date}/{slug}`. A `.shared-llm.yaml` without a `work_log:` key is skipped and the walk continues, so the machine-level destination roster at `~/.shared-llm.yaml` never shadows a repo's config. A configured `work_log.dir` must be a non-empty string, and a `work_log:` block that is present but empty is itself a fault — both fail loudly; never silently fall back from a malformed config. Relative configured paths are resolved from the config file's directory. Expand `{date}`, `{slug}`, `{type}` (`plan`), and `{n}` (the next sibling version). `$WORK_LOG_HOST`, then `$PLANISH_HOST` (deprecated), then `work_log.host`, then a legacy `.planish.yaml` `host:` controls the review URL only. A `work_log:` block that sets no `host` still ends the host search — the review URL falls back to the harness default rather than reaching past it to a legacy `.planish.yaml`. The resolver also returns `review_url`: when `work_log.url_base` and `work_log.serve_root` are both set and the plan directory sits inside `serve_root`, it is `url_base` plus the plan directory relative to `serve_root`, and otherwise `null` — report the plan path alone rather than inventing a URL. Warnings go to stderr; the resolver's stdout is pure JSON.

Create the directory before grilling. Pass the absolute `plan.html` path inside that resolved directory to `planish_submit_plan`; relative submission paths are rejected so planning cannot pollute the current repository. Keep the mutable latest visual pair as `plan.md` + `plan.html`; `planish_submit_plan` freezes changed visual submissions as immutable `plan-vN.md` + `plan-vN.html`. The adversarial workflow separately freezes `plan-candidate-vN.*` and its typed review receipts. These are complementary histories, not competing names. Make the final `plan.html` downloadable through the harness's normal file-delivery mechanism when available.

## Workflow

1. Research the request and read any supplied context. Prefer a fresh research worker when the harness supports it.
2. Read existing design docs, architecture notes, and ADRs before planning.
3. Grill the user with Planish by default. Use the shared Planish HTML Grill Contract at `.shared-llm/public/llm/common/common/planish-html-grill-contract.md`: static annotatable HTML, sticky notes, Copy Feedback, pasted feedback, no in-page submit, and no plain chat questionnaire unless the user explicitly asks for a terminal fallback.
4. Resolve design only when the work requires it. Create or update `design.md`/ADRs when the plan changes service, package, trust, persistence, deployment, shared API, event, schema, security, state, concurrency, migration, rollback, performance, availability, or parallel-phase architecture. If existing docs answer the design question, cite them and continue.
5. If a material product or architecture fork remains, pause for the human. Never let a reviewer consensus or planner guess settle it.
6. Write the current candidate to mutable `plan.md` + `plan.html`, submit the HTML for visual review/version freezing, and also freeze `plan-candidate-vN.md` + `plan-candidate-vN.html` as adversarial-round receipts. Preserve each candidate and `plan-vN` snapshot; never rewrite a frozen file.
7. Before adversarial review, show the planning model/profile and select the review profile:
   - Recommend a configured profile from a different model/provider family when one exists.
   - Do not hard-code a provider, model, or effort.
   - If the selected reviewer appears to share the planning model family, warn and ask for explicit confirmation or a different profile.
8. Run the requested number of fresh full-plan adversarial rounds. The default is exactly two rounds. Each round hires a new read-only `plan-adversary` reviewer through UpAgent, reviewing the full current candidate plan rather than only the diff.
9. For every finding, record a typed disposition: `accepted`, `rejected-with-reason`, or `human-decision-required`. Accepted findings must produce a candidate revision plus a durable diff receipt. Rejected findings must cite why the plan remains correct. `human-decision-required` pauses immediately.
10. After the final adversarial round, regenerate mutable `plan.md` + `plan.html` with the adversarial diff summary, submit it so the final changed pair is frozen as `plan-vN.*`, and ask for final human approval.
11. Final human approval writes `review/final-approval.json`. The approved `plan.md` remains the mutable-latest pathname that downstream tooling reads; its matching immutable `plan-vN.md` and candidate receipts preserve the approved content and history.

## Required receipts

Keep these under the resolved plan directory:

- `plan-candidate-vN.md`
- `plan-candidate-vN.html`
- `review/plan-adversary-r<N>.json`
- `review/plan-adversary-r<N>.md`
- `review/dispositions-r<N>.json`
- `review/diff-r<N>.patch`
- mutable-latest `plan.md` + `plan.html`
- immutable `plan-vN.md` + `plan-vN.html` snapshots created by `planish_submit_plan`
- `review/final-approval.json` after human approval

## Hard rules

- This command always grills. Skipping the grill is not a valid default.
- The adversary is the separate `plan-adversary` persona. Do not reuse the code-focused `adversarial-evaluator`.
- The reviewer is read-only and writes typed findings only.
- Do not create `route.yaml`, run `/cc-convert`, run `/do-convert`, start a managed run, start workers, or edit implementation code.
- If a conditional design artifact is needed but cannot be resolved from available context and the human, stop with the unresolved fork and evidence rather than inventing architecture.
