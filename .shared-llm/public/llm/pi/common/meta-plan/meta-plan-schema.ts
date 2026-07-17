export type CheckSeverity = "error" | "warning";

export interface CheckIssue {
	severity: CheckSeverity;
	message: string;
}

export interface PlanCheckResult {
	ok: boolean;
	issues: CheckIssue[];
	phases: number[];
}

export interface RouteCheckResult {
	ok: boolean;
	issues: CheckIssue[];
	profiles: string[];
	phases: string[];
}

export interface RunnableCheckResult {
	ok: boolean;
	issues: CheckIssue[];
	plan: PlanCheckResult;
	route?: RouteCheckResult;
}

export interface ConversionResult {
	plan: string;
	route: string;
	routeHasTodos: boolean;
	notes: string[];
}

// The five base stages, ALWAYS required for every phase. `buildRouteTodo` emits exactly
// these (a medium-accuracy stub). Also the required-stage list in `validateRoute`.
const STAGE_IDS = [
	"stage-1-implementation",
	"stage-2-adversarial-audit",
	"stage-3-integration-acceptance-seams",
	"stage-4-upstream-dag-verification",
	"stage-5-finalization",
] as const;

// The optional pre-code alignment stage, required ONLY when a phase sets `accuracy: high`
// and forbidden otherwise.
const STAGE_0_ID = "stage-0-alignment";

// Every stage id the route parser will accept under `stages:`. The base five plus the
// optional stage-0. Unknown-stage rejection checks against THIS set; the required-stage
// loop checks against STAGE_IDS (+ stage-0 when high) — the two lists are deliberately
// distinct so stage-0 is recognized-but-conditional rather than always-required.
const RECOGNIZED_STAGE_IDS = [STAGE_0_ID, ...STAGE_IDS] as const;

const ACCURACY_LEVELS = ["medium", "high", "max"] as const;

const MERGE_BACK_STAGE_IDS = [
	"stage-3-integration-acceptance-seams",
	"stage-4-upstream-dag-verification",
	"stage-5-finalization",
] as const;

const DEFAULT_WORKTREE_BRANCH_TEMPLATE =
	"tmp-worktree-{date}-{repo}-phase-{phase}-{run_id}";

const WORKTREE_TEMPLATE_TOKENS = ["{date}", "{repo}", "{phase}", "{run_id}"];

function issue(message: string, severity: CheckSeverity = "error"): CheckIssue {
	return { severity, message };
}

function hasBlockingIssues(issues: CheckIssue[]): boolean {
	return issues.some((i) => i.severity === "error");
}

function linesOf(text: string): string[] {
	return text.replace(/\r\n?/g, "\n").split("\n");
}

function firstNonBlank(text: string): string | undefined {
	return linesOf(text).find((line) => line.trim().length > 0);
}

const PLACEHOLDER_PATTERN = /<[a-z][a-z0-9]*(?:-[a-z0-9]+)*>|\{\{[^}]+\}\}/;

function hasTodo(value: string | undefined): boolean {
	return !value || /\btodo\b/i.test(value) || PLACEHOLDER_PATTERN.test(value);
}

function containsTab(line: string): boolean {
	return /^[ \t]*\t/.test(line);
}

export function validatePlan(planContent: string): PlanCheckResult {
	const issues: CheckIssue[] = [];
	const first = firstNonBlank(planContent);
	if (!first || !/^# Plan:\s+\S/.test(first.trim())) {
		issues.push(issue("plan.md must start with one `# Plan: <title>` heading"));
	}
	const planTitleCount = linesOf(planContent).filter((line) =>
		/^# Plan:\s+\S/.test(line.trim()),
	).length;
	if (planTitleCount !== 1)
		issues.push(issue("plan.md must contain exactly one `# Plan:` heading"));

	const goalLine = linesOf(planContent).find((line) =>
		/^Goal:\s+\S/.test(line.trim()),
	);
	const goalCount = linesOf(planContent).filter((line) =>
		/^Goal:\s+\S/.test(line.trim()),
	).length;
	if (goalCount !== 1)
		issues.push(issue("plan.md must contain exactly one `Goal:` line"));
	if (goalLine && hasTodo(goalLine.trim().replace(/^Goal:\s*/, ""))) {
		issues.push(issue("plan.md Goal: line has an unresolved TODO/placeholder"));
	}

	const headingPattern = /^##\s+(.+)$/gm;
	const phasePattern = /^##\s+Phase\s+(\d+)\s+[—-]\s+(.+)\s*$/gm;
	const enDashHeadingPattern = /^##\s+Phase\s+(\d+)\s+–\s+.+$/gm;
	const allHeadings = Array.from(planContent.matchAll(headingPattern));
	const phaseMatches = Array.from(planContent.matchAll(phasePattern));
	if (enDashHeadingPattern.test(planContent)) {
		issues.push(
			issue(
				"phase headings must use ` — ` (em dash) or ` - ` (hyphen) as the separator, not an en dash (–)",
			),
		);
	}
	for (const h of allHeadings) {
		if (!/^Phase\s+\d+\s+[—-]\s+\S/.test(h[1].trim())) {
			issues.push(
				issue(
					`non-phase section is not allowed in runnable plan.md: ## ${h[1].trim()}`,
				),
			);
		}
	}
	if (phaseMatches.length === 0)
		issues.push(
			issue(
				"plan.md must contain ordered phase headings like `## Phase 0 — <title>`",
			),
		);

	const phases = phaseMatches.map((m) => Number(m[1]));
	phases.forEach((phase, index) => {
		if (phase !== index)
			issues.push(
				issue(
					`phases must be numbered from 0 with no gaps; expected Phase ${index}, found Phase ${phase}`,
				),
			);
	});

	for (let i = 0; i < phaseMatches.length; i++) {
		const m = phaseMatches[i];
		const start = m.index ?? 0;
		const end =
			i + 1 < phaseMatches.length
				? (phaseMatches[i + 1].index ?? planContent.length)
				: planContent.length;
		const block = planContent.slice(start, end);
		const phaseId = `Phase ${m[1]}`;
		const blockLines = linesOf(block);
		const doneLineIndex = blockLines.findIndex((line) =>
			/^Done:\s*$|^Done:\s+\S/.test(line.trim()),
		);
		if (doneLineIndex < 0) {
			issues.push(issue(`${phaseId} must include a Done: section`));
		} else {
			const firstLine = blockLines[doneLineIndex].trim().slice("Done:".length).trim();
			const rest: string[] = [];
			for (let i = doneLineIndex + 1; i < blockLines.length; i++) {
				if (/^Ideal:\s*$|^Ideal:\s+\S/.test(blockLines[i].trim())) break;
				rest.push(blockLines[i]);
			}
			const doneContent = firstLine ? [firstLine, ...rest] : rest;
			const doneLines = doneContent.filter((line) => {
				const trimmed = line.trim();
				return trimmed.length > 0 && !/^#{1,6}\s/.test(trimmed);
			});
			if (doneLines.length === 0) {
				issues.push(issue(`${phaseId} Done: section must not be empty`));
			} else if (doneLines.some((line) => hasTodo(line))) {
				issues.push(issue(`${phaseId} has an unresolved Done: TODO`));
			}
		}
	}

	const routingLine =
		/^(?:\s*[-*]\s*)?(?:llm_profiles|runner_adapters|phases|lead|stages|stage-\d|agents?|agent|team|worker|model|harness|merge_back_at|worktree|branch_template|green_checks|log_checks|finalization(?:_defaults)?|ci)\s*:/im;
	if (routingLine.test(planContent)) {
		issues.push(
			issue(
				"plan.md contains routing fields; move model/harness/agent/stage routing to route.yaml",
			),
		);
	}
	if (
		/route profile sketch|route\.ya?ml|llm profile|\bmerge[ -]back\b|\bworktree\b/i.test(
			planContent,
		)
	) {
		issues.push(
			issue(
				"plan.md appears to contain route-profile notes; route information belongs in route.yaml",
			),
		);
	}

	return { ok: !hasBlockingIssues(issues), issues, phases };
}

function countLeadingSpaces(line: string): number {
	return line.length - line.trimStart().length;
}

function keyNameAtIndent(line: string, indent: number): string | undefined {
	if (countLeadingSpaces(line) !== indent) return undefined;
	const trimmed = line.trim();
	const match = /^([A-Za-z0-9_-]+):(?:\s|$)/.exec(trimmed);
	return match?.[1];
}

function isColumnZeroComment(line: string): boolean {
	return countLeadingSpaces(line) === 0 && line.trim().startsWith("#");
}

function blockForKey(
	content: string,
	indent: number,
	key: string,
): string | undefined {
	const lines = linesOf(content);
	const start = lines.findIndex(
		(line) => keyNameAtIndent(line, indent) === key,
	);
	if (start < 0) return undefined;
	const body: string[] = [];
	for (let i = start + 1; i < lines.length; i++) {
		const line = lines[i];
		if (isColumnZeroComment(line)) continue;
		if (line.trim() && countLeadingSpaces(line) <= indent) break;
		body.push(line);
	}
	return body.join("\n");
}

function duplicateKeysAtIndent(content: string, indent: number): string[] {
	const seen = new Map<string, number>();
	for (const line of linesOf(content)) {
		if (isColumnZeroComment(line)) continue;
		const key = keyNameAtIndent(line, indent);
		if (key === undefined) continue;
		seen.set(key, (seen.get(key) ?? 0) + 1);
	}
	return Array.from(seen.entries())
		.filter(([, count]) => count > 1)
		.map(([key]) => key);
}

function topLevelBlock(content: string, key: string): string | undefined {
	return blockForKey(content, 0, key);
}

function indentedBlock(
	parentBlock: string,
	indent: number,
	key: string,
): string | undefined {
	return blockForKey(parentBlock, indent, key);
}

function childKeys(block: string | undefined, indent: number): string[] {
	if (!block) return [];
	return linesOf(block)
		.map((line) => keyNameAtIndent(line, indent))
		.filter((key): key is string => key !== undefined);
}

function valueIn(
	block: string | undefined,
	indent: number,
	key: string,
): string | undefined {
	if (!block) return undefined;
	for (const line of linesOf(block)) {
		if (keyNameAtIndent(line, indent) === key)
			return line.trim().slice(`${key}:`.length).trim();
	}
	return undefined;
}

function blockHasConcreteValue(block: string | undefined): boolean {
	if (!block) return false;
	return linesOf(block).some((line) => {
		const trimmed = line.trim();
		return trimmed.length > 0 && !trimmed.startsWith("#") && !hasTodo(trimmed);
	});
}

function blockHasTodo(block: string | undefined): boolean {
	return (
		block !== undefined &&
		linesOf(block).some((line) => {
			const trimmed = line.trim();
			return trimmed.length > 0 && !trimmed.startsWith("#") && hasTodo(trimmed);
		})
	);
}

function isAllowedMergeBackStage(value: string | undefined): boolean {
	return MERGE_BACK_STAGE_IDS.some((stage) => stage === value);
}

function hasAllWorktreeTemplateTokens(value: string | undefined): boolean {
	return WORKTREE_TEMPLATE_TOKENS.every((token) => value?.includes(token));
}

export function validateRoute(
	routeContent: string,
	planPhases: number[] = [],
): RouteCheckResult {
	const issues: CheckIssue[] = [];

	const tabLines = linesOf(routeContent).filter((line) => containsTab(line));
	if (tabLines.length > 0) {
		issues.push(
			issue(
				"route.yaml indentation must use spaces; tabs are not allowed",
			),
		);
	}

	for (const dup of duplicateKeysAtIndent(routeContent, 0)) {
		issues.push(issue(`route.yaml has a duplicate top-level key: ${dup}:`));
	}

	const profileBlock = topLevelBlock(routeContent, "llm_profiles");
	const phasesBlock = topLevelBlock(routeContent, "phases");
	const worktreeBlock = topLevelBlock(routeContent, "worktree");
	const finalizationDefaultsBlock = topLevelBlock(
		routeContent,
		"finalization_defaults",
	);
	if (!profileBlock)
		issues.push(issue("route.yaml must define top-level `llm_profiles:`"));
	if (!phasesBlock)
		issues.push(issue("route.yaml must define top-level `phases:`"));
	if (!finalizationDefaultsBlock)
		issues.push(
			issue("route.yaml must define top-level `finalization_defaults:`"),
		);
	if (profileBlock) {
		for (const dup of duplicateKeysAtIndent(profileBlock, 2)) {
			issues.push(
				issue(`route.yaml llm_profiles has a duplicate profile: ${dup}:`),
			);
		}
	}
	if (phasesBlock) {
		for (const dup of duplicateKeysAtIndent(phasesBlock, 2)) {
			issues.push(issue(`route.yaml phases has a duplicate entry: ${dup}:`));
		}
	}

	const branchTemplate = valueIn(worktreeBlock, 2, "branch_template");
	if (hasTodo(branchTemplate)) {
		issues.push(
			issue(
				`route.yaml worktree.branch_template is missing or unresolved; default is ${DEFAULT_WORKTREE_BRANCH_TEMPLATE}`,
			),
		);
	} else if (!hasAllWorktreeTemplateTokens(branchTemplate)) {
		issues.push(
			issue(
				"route.yaml worktree.branch_template must include {date}, {repo}, {phase}, and {run_id}",
			),
		);
	}

	const defaultGreenChecks = indentedBlock(
		finalizationDefaultsBlock ?? "",
		2,
		"green_checks",
	);
	const defaultLogChecks = indentedBlock(
		finalizationDefaultsBlock ?? "",
		2,
		"log_checks",
	);
	if (blockHasTodo(defaultGreenChecks) || blockHasTodo(defaultLogChecks)) {
		issues.push(
			issue("route.yaml finalization_defaults contains unresolved TODO values"),
		);
	}

	const profiles = childKeys(profileBlock, 2);
	if (profileBlock && profiles.length === 0)
		issues.push(
			issue("route.yaml `llm_profiles:` must contain at least one profile"),
		);
	for (const profile of profiles) {
		const block = indentedBlock(profileBlock!, 2, profile);
		for (const key of ["harness", "model"]) {
			const val = valueIn(block, 4, key);
			if (hasTodo(val))
				issues.push(
					issue(`llm_profiles.${profile}.${key} is missing or unresolved`),
				);
		}
		// Optional per-profile scope leash: when true, the phase leader must copy the
		// scope-leash brief block into every stage brief routed to this profile.
		const leash = valueIn(block, 4, "scope_leash");
		if (leash !== undefined && leash !== "true" && leash !== "false") {
			issues.push(
				issue(
					`llm_profiles.${profile}.scope_leash must be true or false (got ${leash})`,
				),
			);
		}
	}

	// Optional lifecycle/escalation profiles must reference known profiles when set. Budgets
	// (absent ⇒ engine default) must be positive integers.
	const advisorProfile = valueIn(
		finalizationDefaultsBlock,
		2,
		"advisor_profile",
	);
	if (advisorProfile !== undefined && !hasTodo(advisorProfile)) {
		if (!profiles.includes(advisorProfile)) {
			issues.push(
				issue(
					`finalization_defaults.advisor_profile references unknown profile ${advisorProfile}`,
				),
			);
		}
	}
	const watchdogProfile = valueIn(
		finalizationDefaultsBlock,
		2,
		"watchdog_profile",
	);
	if (watchdogProfile !== undefined && !hasTodo(watchdogProfile)) {
		if (!profiles.includes(watchdogProfile)) {
			issues.push(
				issue(
					`finalization_defaults.watchdog_profile references unknown profile ${watchdogProfile}`,
				),
			);
		}
	}
	for (const budgetKey of ["phase_pass_budget", "stage_try_budget"]) {
		const raw = valueIn(finalizationDefaultsBlock, 2, budgetKey);
		if (raw !== undefined && !hasTodo(raw) && !/^[1-9]\d*$/.test(raw)) {
			issues.push(
				issue(
					`finalization_defaults.${budgetKey} must be a positive integer (got ${raw})`,
				),
			);
		}
	}

	const allPhaseChildKeys = childKeys(phasesBlock, 2);
	const routePhases = allPhaseChildKeys.filter((p) => /^phase-\d+$/.test(p));
	for (const key of allPhaseChildKeys) {
		if (!/^phase-\d+$/.test(key))
			issues.push(
				issue(`route.yaml phases has an unrecognized entry: ${key}:`),
			);
	}
	if (phasesBlock && routePhases.length === 0)
		issues.push(
			issue("route.yaml `phases:` must contain entries like `phase-0:`"),
		);
	for (const phase of planPhases.map((n) => `phase-${n}`)) {
		if (!routePhases.includes(phase))
			issues.push(
				issue(`route.yaml is missing ${phase} for the matching plan phase`),
			);
	}
	for (const phase of routePhases) {
		const n = Number(phase.replace("phase-", ""));
		if (planPhases.length > 0 && !planPhases.includes(n))
			issues.push(
				issue(
					`route.yaml has ${phase}, but plan.md has no matching Phase ${n}`,
				),
			);
	}

	for (const phase of routePhases) {
		const phaseBlock = indentedBlock(phasesBlock!, 2, phase);
		const mergeBackAt = valueIn(phaseBlock, 4, "merge_back_at");
		if (hasTodo(mergeBackAt)) {
			issues.push(issue(`${phase}.merge_back_at is missing or unresolved`));
		} else if (!isAllowedMergeBackStage(mergeBackAt)) {
			issues.push(
				issue(
					`${phase}.merge_back_at must be one of ${MERGE_BACK_STAGE_IDS.join(", ")}`,
				),
			);
		}

		const phaseFinalizationBlock = indentedBlock(
			phaseBlock ?? "",
			4,
			"finalization",
		);
		const phaseGreenChecks = indentedBlock(
			phaseFinalizationBlock ?? "",
			6,
			"green_checks",
		);
		const phaseLogChecks = indentedBlock(
			phaseFinalizationBlock ?? "",
			6,
			"log_checks",
		);
		if (blockHasTodo(phaseGreenChecks) || blockHasTodo(phaseLogChecks)) {
			issues.push(
				issue(`${phase}.finalization contains unresolved TODO values`),
			);
		}
		if (
			!blockHasConcreteValue(phaseGreenChecks) &&
			!blockHasConcreteValue(defaultGreenChecks)
		) {
			issues.push(
				issue(
					`${phase} must define at least one effective stage-5 green check`,
				),
			);
		}
		if (
			!blockHasConcreteValue(phaseLogChecks) &&
			!blockHasConcreteValue(defaultLogChecks)
		) {
			issues.push(
				issue(`${phase} must define at least one effective stage-5 log check`),
			);
		}

		const leadBlock = indentedBlock(phaseBlock ?? "", 4, "lead");
		if (!leadBlock) issues.push(issue(`${phase}.lead is required`));
		const leadProfile = valueIn(leadBlock, 6, "llm_profile");
		const leadAgent = valueIn(leadBlock, 6, "agent");
		if (hasTodo(leadProfile))
			issues.push(issue(`${phase}.lead.llm_profile is missing or unresolved`));
		else if (!profiles.includes(leadProfile!))
			issues.push(
				issue(
					`${phase}.lead.llm_profile references unknown profile ${leadProfile}`,
				),
			);
		if (hasTodo(leadAgent))
			issues.push(issue(`${phase}.lead.agent is missing or unresolved`));

		// Per-phase accuracy. Absent ⇒ medium (today's five-stage behavior, unchanged).
		// high ⇒ stage-0-alignment is additionally required. Any other value is an error.
		const accuracyRaw = valueIn(phaseBlock, 4, "accuracy");
		const accuracySet = accuracyRaw !== undefined && !hasTodo(accuracyRaw);
		const accuracy = accuracySet ? accuracyRaw! : "medium";
		if (accuracySet && !ACCURACY_LEVELS.some((level) => level === accuracy)) {
			issues.push(
				issue(
					`${phase}.accuracy must be one of ${ACCURACY_LEVELS.join(", ")} (got ${accuracy})`,
				),
			);
		}
		// high and max both require the pre-code alignment stage; max additionally
		// requires a second stage-2 reviewer from a different harness or model.
		const needsAlignment = accuracy === "high" || accuracy === "max";
		const isMaxAccuracy = accuracy === "max";

		// Optional phase flavor. `kind: iac` runs the same ladder with terraform
		// meanings (stage-3 plans and builds the approval table; the TUI approves
		// and applies). Any other value is an error; absent means a normal phase.
		const kindRaw = valueIn(phaseBlock, 4, "kind");
		if (kindRaw !== undefined && !hasTodo(kindRaw) && kindRaw !== "iac") {
			issues.push(
				issue(`${phase}.kind must be \`iac\` when present (got ${kindRaw})`),
			);
		}
		// Optional parallel-group escape hatch: phases sharing a group token may be
		// started together by the TUI. Absent means strictly sequential (the default).
		const groupRaw = valueIn(phaseBlock, 4, "parallel_group");
		if (
			groupRaw !== undefined &&
			!hasTodo(groupRaw) &&
			!/^[a-z0-9][a-z0-9-]*$/.test(groupRaw)
		) {
			issues.push(
				issue(
					`${phase}.parallel_group must be a lowercase token like \`net-fixes\` (got ${groupRaw})`,
				),
			);
		}

		const stagesBlock = indentedBlock(phaseBlock ?? "", 4, "stages");
		if (!stagesBlock) issues.push(issue(`${phase}.stages is required`));
		for (const dup of duplicateKeysAtIndent(stagesBlock ?? "", 6)) {
			issues.push(issue(`${phase}.stages has a duplicate entry: ${dup}:`));
		}
		const stageChildren = childKeys(stagesBlock, 6);
		for (const stage of stageChildren) {
			if (!RECOGNIZED_STAGE_IDS.some((known) => known === stage)) {
				issues.push(
					issue(`${phase}.${stage} is not a recognized stage route entry`),
				);
			}
		}
		// stage-0-alignment is allowed ONLY under accuracy: high or max.
		if (!needsAlignment && stageChildren.includes(STAGE_0_ID)) {
			issues.push(
				issue(
					`${phase}.${STAGE_0_ID} is only allowed when accuracy: high or max`,
				),
			);
		}
		const requiredStages = needsAlignment
			? [STAGE_0_ID, ...STAGE_IDS]
			: [...STAGE_IDS];
		let stage0Profile = "";
		let stage0Agent = "";
		let stage1Profile = "";
		let stage1Agent = "";
		let stage2Profile = "";
		let stage2Agent = "";
		let stage2SecondProfile: string | undefined;
		for (const stage of requiredStages) {
			const stageBlock = indentedBlock(stagesBlock ?? "", 6, stage);
			if (!stageBlock) {
				issues.push(issue(`${phase}.${stage} is required`));
				continue;
			}
			const stageProfile = valueIn(stageBlock, 8, "llm_profile");
			const stageAgent = valueIn(stageBlock, 8, "agent");
			if (hasTodo(stageProfile))
				issues.push(
					issue(`${phase}.${stage}.llm_profile is missing or unresolved`),
				);
			else if (!profiles.includes(stageProfile!))
				issues.push(
					issue(
						`${phase}.${stage}.llm_profile references unknown profile ${stageProfile}`,
					),
				);
			if (hasTodo(stageAgent))
				issues.push(issue(`${phase}.${stage}.agent is missing or unresolved`));
			if (stage === STAGE_0_ID) {
				stage0Profile = stageProfile ?? "";
				stage0Agent = stageAgent ?? "";
			}
			if (stage === "stage-1-implementation") {
				stage1Profile = stageProfile ?? "";
				stage1Agent = stageAgent ?? "";
			}
			if (stage === "stage-2-adversarial-audit") {
				stage2Profile = stageProfile ?? "";
				stage2Agent = stageAgent ?? "";
				stage2SecondProfile = valueIn(stageBlock, 8, "second_llm_profile");
			}
		}
		const resolveHarnessModel = (profileName: string) => {
			if (!profileBlock || !profiles.includes(profileName)) return undefined;
			const block = indentedBlock(profileBlock, 2, profileName);
			return `${valueIn(block, 4, "harness") ?? ""}::${valueIn(block, 4, "model") ?? ""}`;
		};
		// Two stages are "the same reviewer" when they share the agent AND either the same
		// profile or the same resolved harness+model — the check the independence rules use.
		const notIndependent = (
			aProfile: string,
			aAgent: string,
			bProfile: string,
			bAgent: string,
		): boolean => {
			if (!aAgent || !bAgent || aAgent !== bAgent) return false;
			if (aProfile && aProfile === bProfile) return true;
			const aHM = resolveHarnessModel(aProfile);
			return aHM !== undefined && aHM === resolveHarnessModel(bProfile);
		};
		if (notIndependent(stage1Profile, stage1Agent, stage2Profile, stage2Agent)) {
			issues.push(
				issue(
					`${phase}.stage-2-adversarial-audit must be independent from stage-1-implementation`,
				),
			);
		}
		// stage-0's mini-plan audit must not be the same reviewer as the implementer.
		if (
			needsAlignment &&
			notIndependent(stage1Profile, stage1Agent, stage0Profile, stage0Agent)
		) {
			issues.push(
				issue(
					`${phase}.${STAGE_0_ID} must be independent from stage-1-implementation`,
				),
			);
		}
		// The max gear doubles the stage-2 audit: a second reviewer from a different
		// harness or model must also clear the work (a judge resolves disagreement).
		if (isMaxAccuracy) {
			if (hasTodo(stage2SecondProfile)) {
				issues.push(
					issue(
						`${phase}.stage-2-adversarial-audit.second_llm_profile is required when accuracy: max`,
					),
				);
			} else if (!profiles.includes(stage2SecondProfile!)) {
				issues.push(
					issue(
						`${phase}.stage-2-adversarial-audit.second_llm_profile references unknown profile ${stage2SecondProfile}`,
					),
				);
			} else {
				const primary = resolveHarnessModel(stage2Profile);
				const second = resolveHarnessModel(stage2SecondProfile!);
				if (
					primary !== undefined &&
					second !== undefined &&
					primary === second
				) {
					issues.push(
						issue(
							`${phase}.stage-2-adversarial-audit.second_llm_profile must use a different harness or model than the primary auditor`,
						),
					);
				}
			}
		} else if (
			stage2SecondProfile !== undefined &&
			!hasTodo(stage2SecondProfile)
		) {
			issues.push(
				issue(
					`${phase}.stage-2-adversarial-audit.second_llm_profile is only allowed when accuracy: max`,
				),
			);
		}
	}

	return {
		ok: !hasBlockingIssues(issues),
		issues,
		profiles,
		phases: routePhases,
	};
}

export function validateRunnable(
	planContent: string,
	routeContent?: string,
): RunnableCheckResult {
	const plan = validatePlan(planContent);
	const issues = [...plan.issues];
	let route: RouteCheckResult | undefined;
	if (routeContent === undefined) {
		issues.push(
			issue(
				"runnable check requires a route.yaml next to the canonical plan.md",
			),
		);
	} else {
		route = validateRoute(routeContent, plan.phases);
		issues.push(...route.issues);
	}
	return { ok: !hasBlockingIssues(issues), issues, plan, route };
}

function slugTitle(raw: string): string {
	return (
		raw
			.replace(/^#+\s*/, "")
			.replace(/^Plan:\s*/i, "")
			.trim() || "Converted meta plan"
	);
}

function splitPhases(source: string): Array<{ title: string; body: string }> {
	const phaseRe = /^#{2,3}\s+Phase\s+(\d+)\s+[—-]\s+(.+)\s*$/gm;
	const matches = Array.from(source.matchAll(phaseRe));
	if (matches.length === 0)
		return [{ title: "Converted work", body: source.trim() }];
	return matches.map((m, i) => {
		const start = (m.index ?? 0) + m[0].length;
		const end =
			i + 1 < matches.length
				? (matches[i + 1].index ?? source.length)
				: source.length;
		return { title: m[2].trim(), body: source.slice(start, end).trim() };
	});
}

function extractGoal(source: string): string {
	const goal = /^Goal:\s*(.+)\s*$/m.exec(source)?.[1]?.trim();
	if (goal) return goal;
	const purpose = /^##\s+Purpose\s*\n+([\s\S]*?)(?:\n##\s+|$)/m
		.exec(source)?.[1]
		?.trim()
		.split("\n")
		.find((line) => line.trim());
	return purpose?.trim() || "TODO — needs one clear goal";
}

function cleanPhaseBody(body: string): {
	work: string;
	done: string;
	ideal?: string;
	trailing?: string;
} {
	const trailingCut = /\n##\s+\S/.exec(body);
	const scoped =
		trailingCut?.index !== undefined
			? body.slice(0, trailingCut.index).trim()
			: body.trim();
	const trailing =
		trailingCut?.index !== undefined
			? body.slice(trailingCut.index).trim()
			: undefined;

	const scopedLines = linesOf(scoped);
	const doneLineIndex = scopedLines.findIndex((line) =>
		/^Done:\s*$|^Done:\s+\S/.test(line.trim()),
	);
	let done = "";
	let ideal: string | undefined;
	let work = scoped;
	if (doneLineIndex >= 0) {
		const idealLineIndex = scopedLines.findIndex(
			(line, i) => i > doneLineIndex && /^Ideal:\s*$|^Ideal:\s+\S/.test(line.trim()),
		);
		const doneEnd = idealLineIndex >= 0 ? idealLineIndex : scopedLines.length;
		const firstLine = scopedLines[doneLineIndex]
			.trim()
			.slice("Done:".length)
			.trim();
		const doneBody = scopedLines.slice(doneLineIndex + 1, doneEnd);
		done = [firstLine, ...doneBody].filter(Boolean).join("\n").trim();
		if (idealLineIndex >= 0) {
			const idealFirstLine = scopedLines[idealLineIndex]
				.trim()
				.slice("Ideal:".length)
				.trim();
			ideal =
				[idealFirstLine, ...scopedLines.slice(idealLineIndex + 1)]
					.filter(Boolean)
					.join("\n")
					.trim() || undefined;
		}
		work = scopedLines.slice(0, doneLineIndex).join("\n").trim();
	}
	done = done || "- TODO — needs a checkable condition";
	return {
		work: work || "TODO — preserve source work here",
		done,
		ideal,
		trailing,
	};
}

function extractRouteHints(source: string): string[] {
	const hints: string[] = [];
	let capture = false;
	for (const raw of linesOf(source)) {
		const line = raw.trim();
		if (
			/^#{2,3}\s+Route\b/i.test(line) ||
			/^#{2,3}\s+Required route profile\b/i.test(line)
		) {
			capture = true;
			continue;
		}
		if (capture && /^#{2,3}\s+/.test(line)) capture = false;
		if (capture && line) hints.push(line.replace(/^[-*]\s*/, ""));
	}
	return hints;
}

function extractPreamble(source: string): string {
	const goalMatch = /^Goal:\s*.+$/m.exec(source);
	if (!goalMatch) return "";
	const phaseRe = /^#{2,3}\s+Phase\s+\d+\s+[—-]\s+.+$/m;
	const firstPhaseMatch = phaseRe.exec(source);
	const start = goalMatch.index + goalMatch[0].length;
	const end = firstPhaseMatch?.index ?? source.length;
	if (end <= start) return "";
	return source
		.slice(start, end)
		.replace(/^##\s+Purpose\s*$/im, "")
		.trim();
}

export function convertLoosePlan(source: string): ConversionResult {
	const title = slugTitle(firstNonBlank(source) ?? "Converted meta plan");
	const goal = extractGoal(source);
	const phases = splitPhases(source);
	const out: string[] = [`# Plan: ${title}`, "", `Goal: ${goal}`, ""];
	const notes: string[] = [];
	const preamble = extractPreamble(source);
	const trailingHints: string[] = [];
	phases.forEach((phase, index) => {
		const body = cleanPhaseBody(phase.body);
		if (/TODO — needs a checkable condition/.test(body.done))
			notes.push(`Phase ${index} needs a human-supplied Done: check`);
		let work = body.work;
		if (index === 0 && preamble) {
			work = `${preamble}\n\n${work}`.trim();
			notes.push(
				"Preamble prose between Goal: and the first phase heading was folded into Phase 0's work text",
			);
		}
		if (body.trailing) {
			trailingHints.push(`Phase ${index}:\n${body.trailing}`);
			notes.push(
				`Phase ${index} had a trailing non-phase section; it was moved to route.yaml as a comment`,
			);
		}
		out.push(
			`## Phase ${index} — ${phase.title}`,
			"",
			work,
			"",
			"Done:",
			body.done,
			"",
		);
		if (body.ideal) out.push("Ideal:", body.ideal, "");
	});
	const routeHints = extractRouteHints(source);
	if (
		routeHints.length > 0 ||
		/route profile sketch|\bllm_profiles\b|\brunner_adapters\b|\bagent\b|\bharness\b|\bmodel\b/i.test(
			source,
		)
	) {
		notes.push(
			"Source appears to contain routing notes; route.yaml is emitted as an explicit TODO stub for human completion",
		);
	}
	const commentBlocks: string[] = [];
	if (routeHints.length > 0) {
		commentBlocks.push(
			`# Source routing hints preserved from the loose plan:\n${routeHints.map((hint) => `# - ${hint}`).join("\n")}`,
		);
	}
	if (trailingHints.length > 0) {
		commentBlocks.push(
			`# Trailing non-phase sections preserved from the loose plan:\n${trailingHints
				.join("\n")
				.split("\n")
				.map((line) => `# ${line}`)
				.join("\n")}`,
		);
	}
	const route =
		commentBlocks.length === 0
			? buildRouteTodo(phases.length)
			: `${buildRouteTodo(phases.length)}\n${commentBlocks.join("\n")}\n`;
	return {
		plan: `${out
			.join("\n")
			.replace(/\n{3,}/g, "\n\n")
			.trim()}\n`,
		route,
		routeHasTodos: true,
		notes,
	};
}

export function buildRouteTodo(phaseCount: number): string {
	const lines = [
		"llm_profiles:",
		"  TODO-profile:",
		"    harness: TODO",
		"    model: TODO",
		"",
		"worktree:",
		`  branch_template: ${DEFAULT_WORKTREE_BRANCH_TEMPLATE}`,
		"",
		"finalization_defaults:",
		"  green_checks:",
		"    - command: TODO",
		"  log_checks:",
		"    - source: TODO",
		"",
		"phases:",
	];
	for (let i = 0; i < Math.max(1, phaseCount); i++) {
		lines.push(
			`  phase-${i}:`,
			"    merge_back_at: stage-3-integration-acceptance-seams",
			"    lead:",
			"      llm_profile: TODO-profile",
			"      agent: TODO",
			"    stages:",
			"      stage-1-implementation:",
			"        llm_profile: TODO-profile",
			"        agent: TODO",
			"      stage-2-adversarial-audit:",
			"        llm_profile: TODO-profile",
			"        agent: TODO",
			"      stage-3-integration-acceptance-seams:",
			"        llm_profile: TODO-profile",
			"        agent: TODO",
			"      stage-4-upstream-dag-verification:",
			"        llm_profile: TODO-profile",
			"        agent: TODO",
			"      stage-5-finalization:",
			"        llm_profile: TODO-profile",
			"        agent: TODO",
		);
	}
	return `${lines.join("\n")}\n`;
}

export function formatCheckResult(
	prefix: string,
	result: { ok: boolean; issues: CheckIssue[] },
): string {
	const status = result.ok ? "PASS" : "FAIL";
	const lines = [`${prefix}: ${status}`];
	if (result.issues.length === 0) lines.push("- no issues found");
	else
		for (const item of result.issues)
			lines.push(`- ${item.severity.toUpperCase()}: ${item.message}`);
	return lines.join("\n");
}
