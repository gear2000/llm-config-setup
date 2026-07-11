/**
 * phase-id — one stable id per phase that names the log file and the worker's tmux session.
 *
 * This is the hub extension's OWN copy (self-contained — no import from ../meta-orchestrator/). The
 * hub model has no Unix socket and no generated --mcp-config, so the socket/mcp path helpers are
 * dropped; what remains is the run-phase id, the log path, and the worker SESSION NAME the hub
 * transport spawns (`<hubName>-phase<phase>-<seq>`).
 *
 * Pure and runtime-free → unit-testable. The sanitiser is deliberately strict because the id and the
 * session name both become shell/tmux/filename segments (no spaces, `:` or `.`).
 */

import * as path from "node:path";

/** Lowercase, collapse any run of non-[a-z0-9_-] to a single `-`, trim leading/trailing `-`. */
export function sanitizeIdPart(value: string): string {
	const cleaned = value
		.trim()
		.toLowerCase()
		.replace(/[^a-z0-9_-]+/g, "-")
		.replace(/-+/g, "-")
		.replace(/^-+|-+$/g, "");
	return cleaned || "phase";
}

/**
 * Build a phase id for the BRAIN's `run_phase({phase, agents})` calls.
 *
 *   makeRunPhaseId("brain-smoke-plan", "0", 1)  → "orch-brain-smoke-plan-p0-1"
 *   makeRunPhaseId("onboarding", "3", 7)         → "orch-onboarding-p3-7"
 *
 * Two properties the brain needs:
 *  - **Phase 0 is valid** — the brain runs Phase 0 first, and the phase is the brain's free-form
 *    token (sanitised), not a positive 1-based integer.
 *  - **A per-call sequence** disambiguates re-runs of the SAME phase. The brain backtracks (redo
 *    Phase 0 after Phase 3), so two runs of phase 0 must get DISTINCT logs / worker sessions or the
 *    second would collide with the first's leftovers. `seq` (a monotonic counter the brain bumps
 *    every run_phase call) keeps them apart.
 */
export function makeRunPhaseId(slug: string, phase: string, seq: number): string {
	if (!Number.isInteger(seq) || seq < 1) {
		throw new Error(`run-phase seq must be a positive integer, got ${seq}`);
	}
	return `orch-${sanitizeIdPart(slug)}-p${sanitizeIdPart(phase)}-${seq}`;
}

/**
 * The tmux WORKER session name the file transport spawns: `<sessionName>-p<phase>-<n>`.
 *
 *   makeWorkerSessionName("sh4plz", "1", 1)  → "sh4plz-p1-1"
 *   makeWorkerSessionName("sh4plz", "1", 2)  → "sh4plz-p1-2"   (a rerun → distinct session)
 *
 * The session name (the brain's run name) + the phase + the per-attempt iteration. All parts are
 * sanitised so the name is a safe tmux / `just worker-up` argument.
 */
export function makeWorkerSessionName(sessionName: string, phase: string, iteration: number): string {
	if (!Number.isInteger(iteration) || iteration < 1) {
		throw new Error(`worker session iteration must be a positive integer, got ${iteration}`);
	}
	return `${sanitizeIdPart(sessionName)}-p${sanitizeIdPart(phase)}-${iteration}`;
}

/**
 * Absolute path to the phase's transcript log. Logs live under a per-run directory so a directory
 * listing groups one orchestration's phases together.
 */
export function logPathFor(logsDir: string, phaseId: string): string {
	return path.join(logsDir, `${phaseId}.log`);
}
