/**
 * plan — load and validate the phase plan the leader runs one phase at a time.
 *
 * The plan is a JSON file on disk (the brain's memory lives on disk — design doc: "plan +
 * notes live on disk → the brain never fills up"). Shape:
 *
 *   {
 *     "feature": "onboarding",
 *     "phases": [
 *       { "team": "onboarding", "task": "offboard → fix the layer → onboard → test",
 *         "timeoutSeconds": 3600 }
 *     ]
 *   }
 *
 * Parsing/validation is pure and fail-loud: a malformed plan throws with the exact reason
 * (no silent default that would run an empty or half-specified phase). The only per-phase
 * guardrail field is the optional `timeoutSeconds` (the one last-resort wall-clock guard); it
 * falls back to the built-in default at run time. There is NO turn cap and NO liveness/heartbeat
 * limit — the worker runs to completion like a plain `claude -p`. A phase with no `team` or no
 * `task` is an error, because the proxy cannot spawn `claude -p "/team <name> <task>"` without both.
 */

import * as fs from "node:fs";

export interface PhaseSpec {
	/** The named team to run: becomes `/team <team> <task>` in the claude -p prompt. */
	team: string;
	/** The concrete task handed to the team this phase. */
	task: string;
	/** Wall-clock budget for this phase — the one last-resort guard. Optional → built-in default. */
	timeoutSeconds?: number;
}

export interface PhasePlan {
	feature: string;
	phases: PhaseSpec[];
}

function fail(reason: string): never {
	throw new Error(`meta-orchestrator: invalid phase plan — ${reason}`);
}

/** Validate an already-parsed value into a PhasePlan, or throw with the precise reason. */
export function validatePlan(raw: unknown): PhasePlan {
	if (!raw || typeof raw !== "object" || Array.isArray(raw)) fail("top level must be a JSON object");
	const obj = raw as Record<string, unknown>;

	const feature = obj.feature;
	if (typeof feature !== "string" || feature.trim() === "") fail("`feature` must be a non-empty string");

	const phases = obj.phases;
	if (!Array.isArray(phases) || phases.length === 0) fail("`phases` must be a non-empty array");

	const validated: PhaseSpec[] = phases.map((entry, i) => {
		const where = `phases[${i}]`;
		if (!entry || typeof entry !== "object" || Array.isArray(entry)) fail(`${where} must be an object`);
		const p = entry as Record<string, unknown>;

		if (typeof p.team !== "string" || p.team.trim() === "") fail(`${where}.team must be a non-empty string`);
		if (typeof p.task !== "string" || p.task.trim() === "") fail(`${where}.task must be a non-empty string`);

		const spec: PhaseSpec = { team: p.team.trim(), task: p.task.trim() };

		if (p.timeoutSeconds !== undefined) {
			if (typeof p.timeoutSeconds !== "number" || !Number.isFinite(p.timeoutSeconds) || p.timeoutSeconds <= 0) {
				fail(`${where}.timeoutSeconds must be a positive number`);
			}
			spec.timeoutSeconds = p.timeoutSeconds;
		}
		return spec;
	});

	return { feature: feature.trim(), phases: validated };
}

/** Read a plan file from disk and validate it. Throws (fail-loud) on missing or bad file. */
export function loadPlan(planPath: string): PhasePlan {
	let raw: string;
	try {
		raw = fs.readFileSync(planPath, "utf-8");
	} catch (err) {
		const code = err && typeof err === "object" && "code" in err ? (err as NodeJS.ErrnoException).code : undefined;
		if (code === "ENOENT") fail(`plan file not found: ${planPath}`);
		throw err; // permission / IO errors propagate — not ours to swallow
	}
	let parsed: unknown;
	try {
		parsed = JSON.parse(raw);
	} catch (err) {
		fail(`plan file is not valid JSON (${planPath}): ${err instanceof Error ? err.message : String(err)}`);
	}
	return validatePlan(parsed);
}
