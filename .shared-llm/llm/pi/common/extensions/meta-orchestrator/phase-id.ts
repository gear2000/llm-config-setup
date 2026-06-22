/**
 * phase-id — one stable id per phase that names the log file, the socket, and the mcp-config.
 *
 * "One phase ID names the per-phase artifacts" — the transcript `<id>.log`, the back-channel
 * socket `<id>.sock`, and the generated `<id>.mcp.json`, plus the curated escalations in the
 * leader TUI. This module is the single source of that id and the names derived from it, so
 * the proxy, the socket binder, and the log writer can never disagree on the string.
 *
 * Pure and runtime-free → unit-testable. The sanitiser is deliberately strict because the id
 * becomes a filename segment (no spaces, `:` or `.`).
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
 * Build the phase id from the feature name and the 1-based phase number.
 *   makePhaseId("onboarding", 1)            → "orch-onboarding-p1"
 *   makePhaseId("Add Project!", 12)         → "orch-add-project-p12"
 *
 * The `orch-` prefix namespaces these logs/sockets away from anything else on disk, and the
 * `-p<n>` suffix keeps successive phases of one run distinct.
 */
export function makePhaseId(feature: string, phaseNumber: number): string {
	if (!Number.isInteger(phaseNumber) || phaseNumber < 1) {
		throw new Error(`phase number must be a positive integer, got ${phaseNumber}`);
	}
	return `orch-${sanitizeIdPart(feature)}-p${phaseNumber}`;
}

/**
 * Build a phase id for the SDK BRAIN's `run_phase({phase, agents})` calls — additive to
 * makePhaseId (the Pi leader's 1-based-cursor id), which this does NOT replace.
 *
 *   makeRunPhaseId("brain-smoke-plan", "0", 1)  → "orch-brain-smoke-plan-p0-1"
 *   makeRunPhaseId("onboarding", "3", 7)         → "orch-onboarding-p3-7"
 *
 * Two differences the brain needs that makePhaseId can't give:
 *  - **Phase 0 is valid** — the brain runs Phase 0 first, and the phase is the brain's free-form
 *    token (sanitised), not a positive 1-based integer.
 *  - **A per-call sequence** disambiguates re-runs of the SAME phase. The brain backtracks (redo
 *    Phase 0 after Phase 3), so two runs of phase 0 must get DISTINCT sockets / logs / configs
 *    or the second would collide with the first's leftovers. `seq` (a monotonic counter the
 *    leader bumps every run_phase call) keeps them apart.
 */
export function makeRunPhaseId(slug: string, phase: string, seq: number): string {
	if (!Number.isInteger(seq) || seq < 1) {
		throw new Error(`run-phase seq must be a positive integer, got ${seq}`);
	}
	return `orch-${sanitizeIdPart(slug)}-p${sanitizeIdPart(phase)}-${seq}`;
}

/**
 * Absolute path to the phase's transcript log. Logs live under a per-run directory so a
 * directory listing groups one orchestration's phases together and `clean` can remove a
 * whole run at once.
 */
export function logPathFor(logsDir: string, phaseId: string): string {
	return path.join(logsDir, `${phaseId}.log`);
}

/** Absolute path to the per-phase Unix socket the worker's ask_brain connects back on. */
export function socketPathFor(socketsDir: string, phaseId: string): string {
	return path.join(socketsDir, `${phaseId}.sock`);
}

/** Absolute path to the generated --mcp-config JSON handed to the spawned claude -p. */
export function mcpConfigPathFor(configsDir: string, phaseId: string): string {
	return path.join(configsDir, `${phaseId}.mcp.json`);
}
