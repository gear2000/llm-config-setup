# Layered PR reviews

Use `pr-review` for ordinary pull-request review. It performs the review itself. It does not require specialist reports or launch a parallel review team. Choose a targeted reviewer only when you want a particular emphasis.

Every toolkit skill has exactly one same-named agent. A slash invocation and the matching agent use the same procedure and contracts. Existing `pr-risk`, general-purpose reviewers, and the standalone Pi `pr-reviewer` keep their current integrations.

## Agent, skill, contracts

```text
pr-review agent                       Default general reviewer
  └── pr-review skill
      ├── Shared PR contracts
      └── General and relevant domain contracts

Optional targeted pairs
  ├── pr-security agent      → pr-security skill
  ├── pr-tests agent         → pr-tests skill
  ├── pr-integration agent   → pr-integration skill
  ├── pr-compatibility agent → pr-compatibility skill
  ├── pr-architecture agent  → pr-architecture skill
  ├── pr-devops agent        → pr-devops skill
  ├── pr-dead-code agent     → pr-dead-code skill
  └── pr-readiness agent     → pr-readiness skill

Supporting pairs
  ├── pr-prepare agent       → pr-prepare skill
  └── pr-recheck agent       → pr-recheck skill

Each skill
  ├── Shared snapshot, authority, evidence, and report contracts
  └── One or more domain contracts
```

The agent supplies the specialist identity. The skill owns the procedure. Contracts define the requirements and evidence. Agents do not maintain another review procedure, and one specialist does not load another specialist's skill.

## Choose a review

| Same-named agent and skill | Emphasis | Domain contracts |
| --- | --- | --- |
| `pr-review` | General correctness, tests, security, integration, compatibility, and maintainability; operational concerns where affected | General plus relevant domain contracts below; hosted readiness applies only when explicitly requested, and supporting tasks remain separate |
| `pr-security` | Reachable security defects and exploit preconditions | Security, boundary tracing |
| `pr-tests` | Meaningful assertions and regression evidence | Regression, integration |
| `pr-integration` | Component handoffs, errors, retries, and end-to-end effects | Integration, boundary tracing, compatibility |
| `pr-compatibility` | Supported consumers, persisted formats, upgrades, and rollback | Compatibility, migration and rollback |
| `pr-architecture` | Ownership, dependency direction, and system boundaries | Architecture, integration |
| `pr-devops` | Build/deploy configuration, infrastructure, observability, and recovery | Operational safety, migration and rollback |
| `pr-dead-code` | Evidence of unreachable or unused code, including indirect consumers | Reachability |
| `pr-readiness` | Current hosted checks, approvals, and stale evidence | Readiness |
| `pr-prepare` | Draft a description from the final committed diff | Description |
| `pr-recheck` | Revisit findings while retaining IDs | Recheck |

Shared contracts are included in every row. Domain checks apply when the diff or affected behavior makes them relevant. Explain exclusions from repository evidence. They do not require every specialist or every possible test for every PR.

Architecture asks whether ownership and dependency boundaries are sound. Integration follows behavior across those boundaries. Compatibility checks supported consumers and versions. These perspectives may identify the same root cause; deduplicate findings while retaining their source IDs and disagreements.

## Typical use

These are natural-language skill invocations, not a new CLI parser:

```text
/pr-review PR #42. Review the committed change. Do not dispatch specialists.
/pr-security PR #42. Focus on authorization and tenant boundaries.
/pr-integration PR #42. Trace the changed producer-consumer handoff.
/pr-devops PR #42. Review rollout and recovery. Do not deploy anything.
/pr-prepare PR #42. Draft only, using the pinned final diff.
/pr-recheck PR #42 against the supplied prior report. Do not fix code.
```

Alternatively, hire the same-named agent with that request. The caller supplies repository context, PR identity or an explicit committed candidate, refs to resolve or immutable commit IDs, scope, accepted requirements, evidence permissions, and report destination. A review request alone does not authorize tests, probes, publication, or remote approval.

The general reviewer can recommend a targeted follow-up with a concrete question. It does not dispatch it or treat a missing optional specialist as a prerequisite. Supplied reports can inform the general review only when their snapshot and provenance match. One agent's repeated passes are not several independent reviews.

## Boundaries

All pairs are report-only. No application or repository-test edits, commits, publication, workflow triggers, deployments, remote approvals, or merges. Explicitly authorized probes use disposable targets. Negative controls may copy the same existing tests and necessary harness files into isolated fixed/unfixed revisions, but cannot rewrite assertions or application code to manufacture a result.

Uncommitted changes belong with `code-review` or `reviewer` and a captured working-tree snapshot. This toolkit must not silently review committed HEAD instead.

Readiness is separate from code quality. A general code review can pass its scope while current hosted readiness remains UNVERIFIED. Readiness requires authoritative policy, trusted check producers, accepted commit relationships, and fresh hosted state. Skipped execution is not a passed test; a policy exemption must be evidenced separately. No review verdict grants merge permission.

Dead-code findings must account for plugins, reflection, configuration, exports, and external consumers. An empty text search alone is not proof. DevOps expertise does not confer deployment authority. Recheck dispositions FIXED, OPEN, UNVERIFIED, and SUPERSEDED remain separate from criterion status and overall verdict.

## Source ownership and composition

- Skill procedures: `.shared-llm/public/layers/slash-commands/common/common/pr-*/command.md`.
- Thin agent wrappers: `.shared-llm/public/layers/agents/common/pr-*.md`.
- Shared and domain contracts: [the PR contract directory](../.shared-llm/public/layers/skills/common/pr-toolkit/contracts/).
- The existing [evidence layer](../.shared-llm/public/layers/agents/common/_review-evidence.md) and [MIT notice](../.shared-llm/public/layers/agents/common/_owainlewis-license.md) are reused, not copied into new source prose.

The composer accepts flat source lists. Each skill recipe lists its procedure and contracts. Its agent recipe prepends only the wrapper to that exact list and uses the skill's description source. This repeats path lists, not instructions. Tests reject a mismatch. Adding or removing a contract requires updating both recipe lists; changing contract prose requires one edit. No recipe reads a generated `SKILL.md`, and agents need not find an installed skill at runtime because its canonical content is composed into them.

The former combined `_pr-review-contract.md` is split into snapshot, authority, and report sources. Contract text is included once per generated prompt. The Markdown `PR report v1` is an instruction contract, not an executable security boundary or a new JSON API. Stricter caller schemas retain their existing format; the standalone Pi review hub is unchanged.

## Verification and deployment

`tools/test_pr_layering.py` checks all eleven pairs, exact agent/skill body parity, shared contract reuse, description limits, and source-edit propagation to every affected pair in isolated copies. `tools/test_review_prompt_composition.py` checks licensing, repeatable composition, and public copy plus destination composition in a temporary home. It also preserves existing Pi verdict and QA edit-boundary checks.

```sh
python3 -m pytest tools/test_pr_layering.py tools/test_review_prompt_composition.py tools/test_compose_layers.py tools/test_descriptions.py -q
```

These checks prove packaging and source relationships, not model compliance or improved review quality. Use the [controlled evaluation procedure](../.shared-llm/public/layers/slash-commands/common/common/writing-for-agents/resources/references/evaluation.md) for behavioral comparisons. Use the kit's normal update process when you choose to deploy; local composition does not install the toolkit globally.

See [Review procedures](REVIEW-PROCEDURES.md) for upstream adaptations. Every composed pair retains the MIT notice.
