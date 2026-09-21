# Code Context

## Files Retrieved
1. `.shared-llm/public/layers/agents/common/terraform.md` (lines 1-22) - canonical Terraform/OpenTofu persona layer, including apply/destroy approval and saved-plan rules.
2. `.shared-llm/public/layers/agents/common/terraform.description.md` (line 1) - source description declaring the human-approval gate.
3. `.shared-llm/public/compose/agents/terraform.yaml` (lines 1-9) - compose recipe for the Terraform agent.
4. `.claude/agents/terraform.md` (lines 1-43) - checked-in generated Terraform agent output.
5. `.shared-llm/public/layers/slash-commands/common/common/phase-leader/command.md` (lines 126-149) - IaC phase orchestration, saved planning artifact, approval evidence, and fresh-apply rules.
6. `.shared-llm/public/compose/slash-commands/common/common/phase-leader.yaml` (lines 1-10) - recipe targeting `.claude/skills/phase-leader/SKILL.md`.
7. `.shared-llm/public/layers/slash-commands/common/common/tui-control/command.md` (lines 136-149) - direct human approval/apply procedure.
8. `.shared-llm/public/compose/slash-commands/common/common/tui-control.yaml` (lines 1-10) - recipe targeting `.claude/skills/tui-control/SKILL.md`.
9. `.shared-llm/public/llm/pi/common/meta-plan/meta-plan-format.md` (lines 253-257) - whole-file Pi runtime source defining IaC phase semantics.
10. `.shared-llm/public/compose/slash-commands/common/common/do-convert.yaml` (lines 1-10) and `.shared-llm/public/compose/slash-commands/common/claude/cc-convert.yaml` (lines 1-10) - recipes that include `meta-plan-format.md` and target generated conversion skills.
11. `.shared-llm/public/llm/pi/common/agents/tf-reviewer.md` (lines 1-40) - whole-file Pi reviewer persona used to turn raw plans into human approval evidence.

## Key Code

### Canonical Terraform agent policy

`.shared-llm/public/layers/agents/common/terraform.md:7-19`:

> `tofu apply`, `tofu destroy` (and the terraform equivalents) are allowed, but only after a human
> approves — never on a natural-language "sounds good." Before asking, run `tofu plan`, show its
> output, and present a table summarizing the changes (create / update / replace / destroy counts
> and the notable resources — call out replace explicitly, since it destroys and recreates the
> resource). Then show the human exactly what will run: `cd <absolute path>` on one line, the
> command on the next — never a bare relative path, never an implied cwd. Only after approval that
> follows that presentation, run the command. Destroys get the same treatment plus any stronger
> confirmation already required (e.g. typing the destroy count).
>
> Never save a plan to a file and apply that file (`plan -out=<file>` then `apply <file>`) — that
> pattern is banned. A saved plan is opaque to the human reviewing it, and it is only useful when
> you need an immutable plan, which is not how these runs work. Always plan, summarize, get
> approval, then run a fresh apply.

The one-line description at `.shared-llm/public/layers/agents/common/terraform.description.md:1` is:

> Terraform infrastructure specialist that writes and validates IaC, produces plans, and gates apply/destroy on human approval.

The compose mapping is `.shared-llm/public/compose/agents/terraform.yaml:5-9`:

```yaml
description: .shared-llm/public/layers/agents/common/terraform.description.md
inputs:
  - .shared-llm/public/layers/agents/common/terraform.md
  - .shared-llm/public/layers/agents/common/_report-contract.md
output: .claude/agents/terraform.md
```

### Generated agent output

`.claude/agents/terraform.md:14-26` reproduces the policy:

> `tofu apply`, `tofu destroy` (and the terraform equivalents) are allowed, but only after a human
> approves — never on a natural-language "sounds good." ... Only after approval that
> follows that presentation, run the command. Destroys get the same treatment plus any stronger
> confirmation already required (e.g. typing the destroy count).
>
> Never save a plan to a file and apply that file (`plan -out=<file>` then `apply <file>`) — that
> pattern is banned. ... Always plan, summarize, get
> approval, then run a fresh apply.

### IaC orchestration policy

`.shared-llm/public/layers/slash-commands/common/common/phase-leader/command.md:130-143` says stage workers never apply, but Stage 3 may create a saved plan strictly as review evidence:

> Every IaC stage brief restricts the worker to `fmt`, `validate`, `init`, `plan`, and `show` — a stage worker never applies...
>
> Stage-3 runs `init` and `plan -out <pass-dir>/iac/plan.bin`, saves `terraform show -json` output as `<pass-dir>/iac/plan.json`, builds the human table ... and records the artifact's SHA-256...
>
> Stage-4 is the TUI-performed apply ... The TUI always re-plans fresh at apply time ... rather than applying `plan.bin`, so what runs is never an opaque saved-plan artifact. There is no unattended variant...

Its recipe, `.shared-llm/public/compose/slash-commands/common/common/phase-leader.yaml:6-10`, composes the shared phase/handoff protocols plus this command and declares:

```yaml
output: .claude/skills/phase-leader/SKILL.md
```

`.shared-llm/public/layers/slash-commands/common/common/tui-control/command.md:138-149` gives the human-facing gate:

> The TUI runs this flow itself; the apply is never delegated:
>
> 1. ... Print the table to the human VERBATIM — never summarize it away.
> 2. Show the human exactly what will run: `cd <absolute pass-dir>` ... the apply (or destroy) command...
> 3. ... When the table shows "Destroy total to confirm: N" with N above zero, the human approves by typing that exact number; any other answer is a decline. A zero-destroy plan accepts a plain yes.
> 4. Write `<pass-dir>/iac/approval.json` ... `"by": "human"` ...
> 5. On approval, apply DIRECTLY in this pane with a FRESH plan ... NEVER save a plan to a file and apply that file ... apply always re-plans fresh...
> 7. On decline ... expect the phase to end `blocked`.

Its recipe declares `.claude/skills/tui-control/SKILL.md` as output.

### Pi whole-file sources

`.shared-llm/public/llm/pi/common/meta-plan/meta-plan-format.md:255` restates the complete model: workers are plan-only; Stage 3 captures plan evidence and a replacement-aware approval table; the TUI shows the exact command and requires typed destroy count when nonzero; Stage 4 executes a fresh apply, never a saved plan.

`.shared-llm/public/llm/pi/common/agents/tf-reviewer.md:23-39` accepts raw plan output and says:

> Your job is to produce a message the human will read to decide whether to approve or deny this terraform apply/destroy. ... The human cannot see the raw plan — what you write is all they get.

It orders REMOVE first and treats replacement as paired REMOVE + ADD rows. This is a whole Pi runtime persona, not a compose-layer output.

## Architecture

The direct agent path is `terraform.md` + description + shared report contract → `terraform.yaml` → checked-in `.claude/agents/terraform.md`.

The Herdr/IaC path splits responsibility: phase leader creates review evidence and waits; TUI presents that evidence, obtains a human decision, and performs a fresh direct apply; workers cannot apply. A binary saved plan (`plan.bin`) is permitted as Stage-3 evidence, but passing that file to `apply` is forbidden. `meta-plan-format.md` supplies matching semantics to conversion skills and is also deployed as Pi whole-file runtime content.

## Review Findings

- **Informational:** The canonical agent source and checked-in generated agent output agree on apply/destroy requiring human approval and on banning `apply <saved-plan>`.
- **Informational:** “Never save a plan to a file and apply that file” does not prohibit creating `plan.bin` for inspection/evidence. The phase flow explicitly creates it, hashes/reviews related output, then performs a fresh apply instead of applying the artifact.
- **Informational:** Destroy approval is stronger in the TUI flow: a nonzero destroy total must be typed exactly; a plain yes is accepted only for zero destroys.
- **Informational:** No OpenTofu-named separate persona/recipe exists; the Terraform persona explicitly covers `tofu` and Terraform equivalents.

## Residual Risks

- The declared generated outputs `.claude/skills/phase-leader/SKILL.md`, `.claude/skills/tui-control/SKILL.md`, `.claude/skills/do-convert/SKILL.md`, and `.claude/skills/cc-convert/SKILL.md` are not present in this checkout, so their current materialized text could not be compared with source. This is consistent with generated/global artifacts not necessarily being checked in, but leaves their local deployed copies unverified.
- Home deployment targets under `~/.shared-llm/generated/` / `~/.pi/` were outside the requested repository inspection and were not read.

## Start Here

Open `.shared-llm/public/layers/agents/common/terraform.md` first: it is the concise canonical persona policy. Then read the IaC sections in `phase-leader/command.md` and `tui-control/command.md` to understand the more detailed approval/apply execution flow.

```acceptance-report
{
  "criteriaSatisfied": [
    {
      "id": "criterion-1",
      "status": "satisfied",
      "evidence": "review-findings identify the canonical source, recipes, checked-in generated agent, IaC orchestration sources, and absent declared generated skills with exact paths and informational severity."
    }
  ],
  "changedFiles": [],
  "testsAddedOrUpdated": [],
  "commandsRun": [
    {
      "command": "targeted find/grep/read inspection plus git status --short and numbered source/output excerpts",
      "result": "passed",
      "summary": "Located Terraform/OpenTofu policy sources, compose recipes, generated output, Pi whole-file sources, and verified the working tree status before writing this requested report."
    }
  ],
  "validationOutput": [
    "Canonical terraform.md lines 7-19 match generated .claude/agents/terraform.md lines 14-26 for approval and saved-plan policy.",
    "Declared phase-leader and tui-control generated skill outputs are absent from this checkout."
  ],
  "residualRisks": [
    "Absent generated skill files and out-of-repository home deployments were not textually verified."
  ],
  "noStagedFiles": true,
  "notes": "Read-only investigation; context.md is the sole requested report artifact and is excluded from changedFiles because no project source/config was edited."
}
```
