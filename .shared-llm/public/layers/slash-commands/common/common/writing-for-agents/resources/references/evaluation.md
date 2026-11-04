# Evaluate instruction changes

Use this procedure to compare skill, prompt, or workflow variants. A clearer-looking document is a hypothesis, not evidence that agents perform better.

## Freeze the comparison

1. State the behavior being tested and acceptance criteria before running. Include representative tasks, failure cases, ambiguous reports, duplicates, and already-correct behavior where relevant.
2. Freeze each variant's full instructions, repository baseline, task set/order, dependencies, and evaluation harness. Record revisions or content hashes. Keep exploratory prompt-tuning runs separate from the frozen comparison; a changed variant starts a new comparison.
3. Match model/version, model settings, tool permissions, environment, budgets, product-owner answers, and review conditions across variants. Use fresh isolated sessions/workspaces. Record unavoidable differences as confounders, not advantages attributable to the instructions.
4. Hold acceptance tests and robustness probes outside worker-visible files and context. An evaluator runs them after delivery. Keep the same human-review bar and reviewer access across variants; blind reviewers to variant identity where practical. If hidden tests leak, mark that run compromised and rerun cleanly.
5. Use explicitly authorized disposable workspaces and storage for all execution. Pin database, home/config/cache, and service targets rather than using application defaults. Do not let an evaluation mutate real user data or publish artifacts without authorization.

## Run and measure

- Repeat matched tasks where feasible and report sample size, run order, variability, and any missing runs. Preserve raw logs and delivered revisions so claims can be checked.
- Score each artifact as delivered, before evaluator feedback or human repair. Record subsequent fixes and rescues separately; a repaired pass must not erase an initial failure.
- Count missed defects, false findings, human rescues, question stops and individual questions, review rounds, permission denials, cost, and time. Define a rescue as human intervention needed to correct a defect or unblock failure the workflow did not resolve. Distinguish model/runtime from elapsed human wait time; include retries and resumes in cost/time totals.
- Run withheld acceptance tests and robustness probes per task. For sequential work, advance each variant's isolated baseline under the same acceptance policy and rerun cumulative tests after later changes and at the end. Track regressions against earlier requirements, not just the latest ticket's pass count.
- If an evaluation harness is defective, record the correction and rerun affected comparisons consistently. Do not quietly change expectations to fit an implementation.

## Evidence and outcomes

Report each criterion and run as:

- **PASS**: the stated criterion was exercised and met, with evidence.
- **FAIL**: observed evidence contradicts the criterion; include the failing case.
- **UNVERIFIED**: missing, blocked, leaked, or incomparable evidence prevents a conclusion. Name what is needed to resolve it.

Use a compact table: variant/revision, task, delivered outcome, test/probe evidence, missed defects, human rescues, questions, cost/time, and cumulative regressions. Link logs and note coverage limits, confounders, and post-delivery repairs. Summaries must retain failures and unverified cases.

Conclude only what the measured comparison supports. Passing a finite suite does not prove defect-free behavior; one small or changing run does not establish a general benchmark. Do not import external benchmark numbers as claims about this kit or claim improvement without matched evidence. State untested benefits as hypotheses and identify the next useful experiment.

<!--
Portions of these review, testing, planning, audit, and refactoring instructions
are adapted from Owain Lewis's MIT-licensed projects:
https://github.com/owainlewis/blueprint
https://github.com/owainlewis/machinist
https://github.com/owainlewis/software-factory

MIT License

Copyright (c) 2026 Owain Lewis

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
-->
