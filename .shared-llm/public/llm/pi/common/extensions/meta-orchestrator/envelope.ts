/**
 * envelope — the one-line-JSON wire protocol for the ask_brain back-channel.
 *
 * Design doc (3a): the worker is a claude -p that CANNOT listen on a socket, so we flip it
 * — the BRAIN is the server that listens, the WORKER is a client that calls out and BLOCKS
 * for the reply. The worker's ask_brain tool (an stdio MCP server, see ask-brain-server.mjs)
 * opens the per-phase Unix socket, writes one `ask` envelope, and reads back one `answer`.
 *
 * This module is the protocol itself: the envelope types, a validator, the hop-limit guard,
 * and the answer builders. It is pure (no `net`, no `fs`) so the framing and the hop logic
 * are unit-testable without a live socket — the socket transport lives in back-channel.ts.
 *
 * Mirrors coms.ts's envelope discipline (typed `type` discriminator, `hops` field, a
 * MAX_HOPS cap) but for the single brain↔worker channel rather than a peer mesh.
 */

/** Default ceiling on intercom hops, overridable via env. Mirrors coms.ts PI_COMS_MAX_HOPS. */
export const DEFAULT_MAX_HOPS = Number(process.env.META_ORCH_MAX_HOPS) || 5;

export type AskSeverity = "blocked" | "decision" | "progress" | "heartbeat";

/** Worker → brain. One question (or heartbeat check-in) that blocks until answered. */
export interface AskEnvelope {
	type: "ask";
	/** Correlates the answer to this ask. */
	id: string;
	/** How urgent / what kind — drives whether the leader must act or may just note it. */
	severity: AskSeverity;
	/** The worker's message to the brain. */
	message: string;
	/** Hop count, incremented each relay; rejected at DEFAULT_MAX_HOPS to stop loops. */
	hops: number;
	/** The phase this ask belongs to (the brain uses it to label the escalation in the TUI). */
	phaseId: string;
}

/** Brain → worker. The leader's reply; the worker's blocked ask_brain call returns this. */
export interface AnswerEnvelope {
	type: "answer";
	id: string;
	/** The leader's guidance text the worker resumes on, or the reason it was rejected. */
	answer: string;
	/** When true the leader is telling the worker to STOP this phase (fundamentally wrong). */
	stop: boolean;
}

export type Envelope = AskEnvelope | AnswerEnvelope;

/** Type-guard for a well-formed ask envelope arriving on the socket. */
export function isAskEnvelope(value: unknown): value is AskEnvelope {
	if (!value || typeof value !== "object") return false;
	const e = value as Record<string, unknown>;
	return (
		e.type === "ask" &&
		typeof e.id === "string" && e.id.length > 0 &&
		typeof e.message === "string" &&
		typeof e.phaseId === "string" &&
		typeof e.hops === "number" &&
		isSeverity(e.severity)
	);
}

export function isAnswerEnvelope(value: unknown): value is AnswerEnvelope {
	if (!value || typeof value !== "object") return false;
	const e = value as Record<string, unknown>;
	return e.type === "answer" && typeof e.id === "string" && typeof e.answer === "string" && typeof e.stop === "boolean";
}

function isSeverity(v: unknown): v is AskSeverity {
	return v === "blocked" || v === "decision" || v === "progress" || v === "heartbeat";
}

/**
 * Decide whether an ask may be relayed up another hop. Returns the rejection answer when
 * the cap is hit (so the worker gets a definite reply and stops, rather than hanging), or
 * `null` when the hop is allowed. Keeping this a pure decision makes the limit testable
 * and keeps back-channel.ts from re-deriving the rule.
 */
export function checkHopLimit(env: AskEnvelope, maxHops: number = DEFAULT_MAX_HOPS): AnswerEnvelope | null {
	if (env.hops >= maxHops) {
		return {
			type: "answer",
			id: env.id,
			answer: `intercom hop limit reached (${env.hops} >= ${maxHops}); stopping to avoid a relay loop`,
			stop: true,
		};
	}
	return null;
}

/** Build the leader's answer to an ask. */
export function makeAnswer(id: string, answer: string, stop: boolean): AnswerEnvelope {
	return { type: "answer", id, answer, stop };
}

/** Serialize an envelope to a single newline-terminated frame for the socket. */
export function encodeFrame(env: Envelope): string {
	return JSON.stringify(env) + "\n";
}

/**
 * Parse one received frame into an Envelope, or throw a precise error. Used by both ends
 * of the socket. Fail-loud: a malformed frame is a protocol break, not something to paper
 * over with a default — the caller decides how to respond on the wire.
 */
export function decodeFrame(line: string): Envelope {
	let obj: unknown;
	try {
		obj = JSON.parse(line);
	} catch (err) {
		throw new Error(`ask_brain frame is not JSON: ${err instanceof Error ? err.message : String(err)}`);
	}
	if (isAskEnvelope(obj)) return obj;
	if (isAnswerEnvelope(obj)) return obj;
	throw new Error("ask_brain frame is neither a valid ask nor answer envelope");
}
