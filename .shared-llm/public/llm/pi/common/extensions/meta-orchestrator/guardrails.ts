/**
 * guardrails — the ONE last-resort limit the leader enforces on a worker.
 *
 * A per-phase worker is a `claude -p` that RUNS TO COMPLETION, exactly like the Ralph loop or a
 * plain `claude -p`: it ends on its own when the task is done. We do NOT cap its turns and we do
 * NOT kill it for going quiet — a head-down worker grinding through a real phase can easily go
 * 15-20 min without an escalation, and an artificial step/liveness cap was killing it before it
 * finished. The only guard left is a GENEROUS wall-clock timeout: a truly-hung process can't sit
 * forever, but normal work is never cut off.
 *
 * It is a PURE state machine driven by an injected `now()` clock — no real `setTimeout` — so the
 * limit is unit-testable with a fake clock and a reviewer can verify it actually fires rather than
 * being decorative. claude-proxy.ts owns the live wiring (a real interval calls `evaluate(now)`);
 * it does the killing, this module only DECIDES.
 */

export interface GuardrailLimits {
	/** Wall-clock budget for the phase — the only worker limit. Generous by default. */
	timeoutMs: number;
}

/** Why a phase is being stopped — surfaced to the leader TUI and the log verbatim. The only
 *  kind is the wall-clock timeout (the last-resort guard); kept as a tagged union so a future
 *  guard, if one is ever added, slots in without churning callers. */
export type Breach = { kind: "timeout"; elapsedMs: number; limitMs: number };

export interface GuardrailState {
	startedAtMs: number;
	limits: GuardrailLimits;
}

/** Built-in default: a GENEROUS last-resort budget so normal work is never cut off, only a
 *  genuinely-hung process. 4h; a plan/phase may override it. */
export const DEFAULT_TIMEOUT_SECONDS = 4 * 60 * 60; // 4h per phase — last-resort only

/**
 * Resolve a phase's effective limits from the (optional) per-phase timeout and the default.
 * The timeout is the single worker limit: generous enough to never cut normal work, present
 * only so a truly-wedged process can't sit forever.
 */
export function resolveLimits(opts: { timeoutSeconds?: number }): GuardrailLimits {
	const timeoutSeconds = opts.timeoutSeconds ?? DEFAULT_TIMEOUT_SECONDS;
	return { timeoutMs: timeoutSeconds * 1000 };
}

export function createGuardrailState(limits: GuardrailLimits, nowMs: number): GuardrailState {
	return { startedAtMs: nowMs, limits };
}

/**
 * Evaluate the wall-clock limit at time `nowMs`. Returns the timeout breach once the budget is
 * exceeded, or `null` while the worker is within bounds. Pure: same state + same now → same verdict.
 */
export function evaluate(state: GuardrailState, nowMs: number): Breach | null {
	const elapsed = nowMs - state.startedAtMs;
	if (elapsed >= state.limits.timeoutMs) {
		return { kind: "timeout", elapsedMs: elapsed, limitMs: state.limits.timeoutMs };
	}
	return null;
}

/** A one-line, human-readable reason for a breach — for the TUI and the log. */
export function describeBreach(breach: Breach): string {
	return `phase timeout: ${Math.round(breach.elapsedMs / 1000)}s elapsed >= ${Math.round(breach.limitMs / 1000)}s budget`;
}
