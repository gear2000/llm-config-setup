/// <reference path="./runtime-shims.d.ts" />

import * as fs from "node:fs";
import * as path from "node:path";
import { convertLoosePlan, validateRunnable } from "./meta-plan-schema.ts";

let pass = 0;
const fails: string[] = [];
function check(name: string, cond: boolean, detail = "") {
	if (cond) pass++;
	else fails.push(`  ✗ ${name}${detail ? ` — ${detail}` : ""}`);
}

const here = path.dirname(new URL(import.meta.url).pathname);
const fixturesDir = path.join(here, "fixtures");
function fixture(name: string): string {
	return fs.readFileSync(path.join(fixturesDir, name), "utf8");
}

const canonicalPlan = fixture("canonical-plan.md");
const validRoute = fixture("route.yaml");
const loosePlan = fixture("loose-plan.md");
const invalidRoutedPlan = fixture("invalid-plan-with-routing.md");

async function main() {
	const ok = validateRunnable(canonicalPlan, validRoute);
	check(
		"canonical plan + five-stage route passes",
		ok.ok,
		ok.issues.map((i) => i.message).join("; "),
	);

	const missingRoute = validateRunnable(canonicalPlan);
	check(
		"plan without route is not runnable",
		!missingRoute.ok &&
			missingRoute.issues.some((i) => i.message.includes("route.yaml")),
	);

	const badPlan = validateRunnable(
		canonicalPlan + "\n## Verification\n\nExtra section.\n",
		validRoute,
	);
	check(
		"extra non-phase plan sections fail",
		!badPlan.ok &&
			badPlan.issues.some((i) => i.message.includes("non-phase section")),
	);

	const badRoute = validateRunnable(
		canonicalPlan,
		validRoute
			.replace("agent: adversarial-evaluator", "agent: frontend")
			.replace("llm_profile: pi-default", "llm_profile: claude-low"),
	);
	check(
		"stage 2 must differ from stage 1",
		!badRoute.ok &&
			badRoute.issues.some((i) => i.message.includes("must be independent")),
	);

	const missingStageFive = validateRunnable(
		canonicalPlan,
		validRoute.replace(
			/ {6}stage-5-finalization:\n {8}llm_profile: claude-low\n {8}agent: qa\n/g,
			"",
		),
	);
	check(
		"stage 5 is required for every phase",
		!missingStageFive.ok &&
			missingStageFive.issues.some((i) =>
				i.message.includes("stage-5-finalization"),
			),
	);

	const invalidMerge = validateRunnable(
		canonicalPlan,
		validRoute.replace(
			"merge_back_at: stage-3-integration-acceptance-seams",
			"merge_back_at: stage-2-adversarial-audit",
		),
	);
	check(
		"merge_back_at must be stage 3, 4, or 5",
		!invalidMerge.ok &&
			invalidMerge.issues.some((i) => i.message.includes("merge_back_at")),
	);

	const validMergeStage4 = validateRunnable(
		canonicalPlan,
		validRoute.replace(
			"merge_back_at: stage-3-integration-acceptance-seams",
			"merge_back_at: stage-4-upstream-dag-verification",
		),
	);
	check(
		"merge_back_at accepts stage-4-upstream-dag-verification",
		validMergeStage4.ok,
		validMergeStage4.issues.map((i) => i.message).join("; "),
	);

	const validMergeStage5 = validateRunnable(
		canonicalPlan,
		validRoute.replace(
			"merge_back_at: stage-3-integration-acceptance-seams",
			"merge_back_at: stage-5-finalization",
		),
	);
	check(
		"merge_back_at accepts stage-5-finalization",
		validMergeStage5.ok,
		validMergeStage5.issues.map((i) => i.message).join("; "),
	);

	const missingFinalization = validateRunnable(
		canonicalPlan,
		validRoute.replace(/finalization_defaults:[\s\S]*?\nphases:/, "phases:"),
	);
	check(
		"stage 5 requires effective green and log checks",
		!missingFinalization.ok &&
			missingFinalization.issues.some((i) =>
				i.message.includes("green check"),
			) &&
			missingFinalization.issues.some((i) => i.message.includes("log check")),
	);
	check(
		"missing finalization_defaults top-level block fails",
		!missingFinalization.ok &&
			missingFinalization.issues.some((i) =>
				i.message.includes("finalization_defaults"),
			),
	);

	const missingLlmProfiles = validateRunnable(
		canonicalPlan,
		validRoute.replace(/llm_profiles:[\s\S]*?\nworktree:/, "worktree:"),
	);
	check(
		"missing top-level llm_profiles fails",
		!missingLlmProfiles.ok &&
			missingLlmProfiles.issues.some((i) => i.message.includes("llm_profiles")),
	);

	const missingPhases = validateRunnable(
		canonicalPlan,
		validRoute.replace(/\nphases:[\s\S]*$/, "\n"),
	);
	check(
		"missing top-level phases fails",
		!missingPhases.ok &&
			missingPhases.issues.some((i) =>
				i.message.includes("must define top-level `phases:`"),
			),
	);

	const badBranchTemplate = validateRunnable(
		canonicalPlan,
		validRoute.replace(
			"branch_template: tmp-worktree-{date}-{repo}-phase-{phase}-{run_id}",
			"branch_template: tmp-worktree-{date}-{repo}",
		),
	);
	check(
		"worktree.branch_template missing tokens fails",
		!badBranchTemplate.ok &&
			badBranchTemplate.issues.some((i) => i.message.includes("branch_template")),
	);

	const unknownProfileRef = validateRunnable(
		canonicalPlan,
		validRoute.replace(
			"llm_profile: claude-low\n      agent: herdr-phase-leader",
			"llm_profile: does-not-exist\n      agent: herdr-phase-leader",
		),
	);
	check(
		"unknown llm_profile reference fails",
		!unknownProfileRef.ok &&
			unknownProfileRef.issues.some((i) =>
				i.message.includes("unknown profile"),
			),
	);

	const unknownStageEntry = validateRunnable(
		canonicalPlan,
		validRoute.replace(
			"      stage-5-finalization:\n        llm_profile: claude-low\n        agent: qa\n  phase-1:",
			"      stage-5-finalization:\n        llm_profile: claude-low\n        agent: qa\n      stage-6-extra:\n        llm_profile: claude-low\n        agent: qa\n  phase-1:",
		),
	);
	check(
		"unknown extra stage-6 entry fails",
		!unknownStageEntry.ok &&
			unknownStageEntry.issues.some((i) =>
				i.message.includes("not a recognized stage route entry"),
			),
	);

	// --- accuracy / stage-0-alignment (medium vs high) ---
	const withHighStage0 = (route: string, stage0Body: string): string =>
		route
			.replace(
				"  phase-0:\n    merge_back_at:",
				"  phase-0:\n    accuracy: high\n    merge_back_at:",
			)
			.replace(
				"    stages:\n      stage-1-implementation:",
				`    stages:\n${stage0Body}      stage-1-implementation:`,
			);
	const independentStage0 =
		"      stage-0-alignment:\n        llm_profile: pi-default\n        agent: aligner\n";

	const highOk = validateRunnable(
		canonicalPlan,
		withHighStage0(validRoute, independentStage0),
	);
	check(
		"accuracy: high with an independent stage-0-alignment passes",
		highOk.ok,
		highOk.issues.map((i) => i.message).join("; "),
	);

	const highNoStage0 = validateRunnable(
		canonicalPlan,
		validRoute.replace(
			"  phase-0:\n    merge_back_at:",
			"  phase-0:\n    accuracy: high\n    merge_back_at:",
		),
	);
	check(
		"accuracy: high without stage-0-alignment fails",
		!highNoStage0.ok &&
			highNoStage0.issues.some(
				(i) =>
					i.message.includes("stage-0-alignment") &&
					i.message.includes("required"),
			),
	);

	const mediumWithStage0 = validateRunnable(
		canonicalPlan,
		validRoute.replace(
			"    stages:\n      stage-1-implementation:",
			`    stages:\n${independentStage0}      stage-1-implementation:`,
		),
	);
	check(
		"medium (default) phase with a stage-0-alignment entry fails",
		!mediumWithStage0.ok &&
			mediumWithStage0.issues.some((i) =>
				i.message.includes("only allowed when accuracy: high"),
			),
	);

	const badAccuracy = validateRunnable(
		canonicalPlan,
		validRoute.replace(
			"  phase-0:\n    merge_back_at:",
			"  phase-0:\n    accuracy: ultra\n    merge_back_at:",
		),
	);
	check(
		"unknown accuracy value fails",
		!badAccuracy.ok &&
			badAccuracy.issues.some((i) => i.message.includes("accuracy must be one of")),
	);

	const stage0NotIndependent = validateRunnable(
		canonicalPlan,
		withHighStage0(
			validRoute,
			"      stage-0-alignment:\n        llm_profile: claude-low\n        agent: frontend\n",
		),
	);
	check(
		"stage-0-alignment sharing stage-1's profile+agent fails independence",
		!stage0NotIndependent.ok &&
			stage0NotIndependent.issues.some(
				(i) =>
					i.message.includes("stage-0-alignment") &&
					i.message.includes("must be independent"),
			),
	);

	// --- optional finalization_defaults: advisor_profile + budgets ---
	const withAdvisorAndBudgets = validRoute.replace(
		"finalization_defaults:\n",
		"finalization_defaults:\n  advisor_profile: claude-low\n  phase_pass_budget: 5\n  stage_try_budget: 2\n",
	);
	const advisorOk = validateRunnable(canonicalPlan, withAdvisorAndBudgets);
	check(
		"optional advisor_profile + budgets parse and pass",
		advisorOk.ok,
		advisorOk.issues.map((i) => i.message).join("; "),
	);

	const advisorUnknown = validateRunnable(
		canonicalPlan,
		validRoute.replace(
			"finalization_defaults:\n",
			"finalization_defaults:\n  advisor_profile: nonexistent\n",
		),
	);
	check(
		"advisor_profile referencing an unknown profile fails",
		!advisorUnknown.ok &&
			advisorUnknown.issues.some(
				(i) =>
					i.message.includes("advisor_profile") &&
					i.message.includes("unknown profile"),
			),
	);

	const badBudget = validateRunnable(
		canonicalPlan,
		validRoute.replace(
			"finalization_defaults:\n",
			"finalization_defaults:\n  stage_try_budget: 0\n",
		),
	);
	check(
		"non-positive budget fails",
		!badBudget.ok &&
			badBudget.issues.some((i) =>
				i.message.includes("must be a positive integer"),
			),
	);

	const duplicateRouteKey = validateRunnable(
		canonicalPlan,
		`${validRoute}\nphases:\n  phase-0:\n    merge_back_at: stage-3-integration-acceptance-seams\n`,
	);
	check(
		"duplicate top-level key in route.yaml fails",
		!duplicateRouteKey.ok &&
			duplicateRouteKey.issues.some((i) => i.message.includes("duplicate")),
	);

	const tabIndentedRoute = validateRunnable(
		canonicalPlan,
		validRoute.replace("  claude-low:", "\tclaude-low:"),
	);
	check(
		"tab-indented route.yaml fails",
		!tabIndentedRoute.ok &&
			tabIndentedRoute.issues.some((i) => i.message.includes("tabs are not allowed")),
	);

	const routedPlan = validateRunnable(invalidRoutedPlan, validRoute);
	check(
		"plan with Agent:/Model: routing lines is rejected",
		!routedPlan.ok &&
			routedPlan.issues.some((i) => i.message.includes("routing fields")),
	);

	const doneTodoPlan = validateRunnable(
		canonicalPlan.replace(
			"- the form renders;\n- required-field validation blocks empty submissions;\n- submit produces a visible summary.",
			"TODO",
		),
		validRoute,
	);
	check(
		"Done: TODO unresolved is rejected",
		!doneTodoPlan.ok &&
			doneTodoPlan.issues.some((i) => i.message.includes("unresolved Done: TODO")),
	);

	const lowercaseTodoPlan = validateRunnable(
		canonicalPlan.replace(
			"- the form renders;\n- required-field validation blocks empty submissions;\n- submit produces a visible summary.",
			"- todo",
		),
		validRoute,
	);
	check(
		"lowercase todo value is rejected",
		!lowercaseTodoPlan.ok &&
			lowercaseTodoPlan.issues.some((i) =>
				i.message.includes("unresolved Done: TODO"),
			),
	);

	const emptyDonePlan = validateRunnable(
		canonicalPlan.replace(
			"Done:\n\n- the form renders;\n- required-field validation blocks empty submissions;\n- submit produces a visible summary.\n",
			"Done:\n\n",
		),
		validRoute,
	);
	check(
		"empty Done: section is rejected",
		!emptyDonePlan.ok &&
			emptyDonePlan.issues.some((i) => i.message.includes("must not be empty")),
	);

	const converted = convertLoosePlan(loosePlan);
	const convertedCheck = validateRunnable(converted.plan, converted.route);
	check(
		"conversion writes canonical phase heading",
		converted.plan.includes("## Phase 0 — build MVP"),
	);
	check(
		"conversion strips route sketch from plan body",
		!converted.plan.includes("Route profile sketch"),
	);
	check(
		"conversion defaults non-interactive merge timing to stage 3",
		converted.route.includes(
			"merge_back_at: stage-3-integration-acceptance-seams",
		),
	);
	check(
		"conversion emits five-stage route stub",
		converted.route.includes("stage-5-finalization"),
	);
	check(
		"conversion preserves route hints in route output comments",
		converted.route.includes(
			"Claude Code should do the initial implementation",
		),
	);
	check(
		"conversion route stub is intentionally not runnable",
		!convertedCheck.ok &&
			convertedCheck.issues.some((i) => i.message.includes("unresolved")),
	);

	const loosePlanWithRisks = `${loosePlan}\n## Risks\n\n- something might break.\n`;
	const convertedWithRisks = convertLoosePlan(loosePlanWithRisks);
	const convertedPlanAlone = validateRunnable(
		convertedWithRisks.plan,
		undefined,
	).plan;
	check(
		"convertLoosePlan with trailing ## Risks section keeps plan.md alone passing validatePlan",
		convertedPlanAlone.ok,
		convertedPlanAlone.issues.map((i) => i.message).join("; "),
	);
	check(
		"convertLoosePlan with trailing ## Risks section strips it from the plan body",
		!convertedWithRisks.plan.includes("## Risks"),
	);
}

main()
	.then(() => {
		console.log(`meta-plan-schema: ${pass} checks passed`);
		if (fails.length) {
			console.log("FAILURES:");
			console.log(fails.join("\n"));
			process.exit(1);
		}
		console.log("ALL PASS ✓");
	})
	.catch((err) => {
		console.log(
			`meta-plan-schema test failed: ${err instanceof Error ? err.stack : String(err)}`,
		);
		process.exit(1);
	});
