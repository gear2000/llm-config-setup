/**
 * brain-core — the framework-free brain orchestration logic for the Pi leader.
 *
 * The Pi extension (index.ts) is the thin-but-deep wiring: it registers the `brain:execute-plan`
 * command + the `run_phase` / `answer_worker` tools, and translates between Pi's runtime
 * (pi.sendMessage / typebox tool params / ctx.ui) and THIS module's pure handlers. Everything that
 * can be unit-tested without a live Pi runtime lives here, mirroring how sdk-leader.ts keeps its
 * pure logic in `buildBrainServer` + `startPhaseInBackground`.
 *
 * This module imports ONLY Node builtins + the framework-free worker half (claude-proxy.ts and its
 * siblings, which are themselves Node-builtins-only), so it loads under
 * `node --experimental-strip-types` for tests. It NEVER imports `@mariozechner/pi-coding-agent`,
 * `@sinclair/typebox`, or the SDK — those stay in index.ts.
 *
 * The per-phase PROXY here is IN-PROCESS (it runs the shared runPhase() on the brain's own event
 * loop, exactly like the SDK brain). See the module README note at the bottom of index.ts for why
 * the proxy is in-process rather than a separate cheap Pi sub-agent process, and what the
 * `brain-proxy-model` setting is reserved for.
 */

import { makeRunPhaseId, socketPathFor } from "./phase-id.ts";
import { type AskEnvelope, type AnswerEnvelope, makeAnswer, DEFAULT_MAX_HOPS } from "./envelope.ts";
import { runPhase, type PhaseOutcome, type RelayUp, type ProxyEvents, type PhaseHandle } from "./claude-proxy.ts";
import { resolveLimits } from "./guardrails.ts";
import { healStaleSocket } from "./back-channel.ts";
import { appendProgress, judgePhaseStatus } from "./progress.ts";

export const LOG_CHANNEL = "meta-orch-log";

/**
 * Persist one phase's TERMINAL outcome to the run's durable ledger so a restart of the same plan can
 * RESUME instead of restart. Shared by both leaders (the Pi brain's finishPhase below AND the SDK
 * brain's finishPhase in sdk-leader.ts) so they record identically.
 *
 * The recorded status is the phase's TRUE semantic outcome (judgePhaseStatus), NOT the worker
 * PROCESS exit — that is the bug this fixes. judgePhaseStatus weighs, in order: a guardrail breach
 * (the worker died before it could certify → `breached_turn_cap` / `breached_<kind>`), then the
 * worker's own `PHASE_RESULT: passed|partial|blocked|failed` verdict parsed out of its report, then
 * a no-verdict fallback that NEVER assumes a pass. So a worker that finishes but reports PARTIAL or
 * BLOCKED (or whose adversarial-evaluator VEERED, which the worker reports as blocked) is recorded
 * partial/blocked and RE-RUN on resume; only a genuine `passed` is skipped. A phase still IN PROGRESS
 * when the process dies writes NOTHING here → absent → re-run. The summary is the worker's report,
 * trimmed, so the brain reads a handoff on resume.
 *
 * `outcomeStatus` is the proxy's PhaseOutcome.status ("completed" | "failed" | "stopped" | "breached")
 * or null when the phase errored before producing an outcome. `breachKind` is PhaseOutcome.breach?.kind
 * (e.g. "turn_cap"), or null/undefined when no breach ended the phase. `report` is the worker's final
 * result text — the verdict line is parsed from it. Best-effort: appendProgress swallows write errors,
 * so persistence can never break the run.
 */
export function recordPhaseProgress(
	runDir: string,
	phase: string,
	phaseId: string,
	outcomeStatus: string | null | undefined,
	report: string | undefined,
	planHash: string | undefined,
	breachKind?: string | null,
): void {
	appendProgress(runDir, {
		phase,
		status: judgePhaseStatus({ outcomeStatus, breachKind, report }),
		timestamp: new Date().toISOString(),
		phaseId,
		summary: report ? report.slice(0, 1000) : undefined,
		planHash,
	});
}

/** An ask awaiting the brain-LLM's answer_worker call, keyed by ask id. The worker is blocked on
 *  the per-phase socket until one of these resolves. Mirrors sdk-leader.ts's PendingAsk. */
export interface PendingAsk {
	ask: AskEnvelope;
	phaseId: string;
	resolve: (answer: AnswerEnvelope) => void;
}

/** Per-run working dirs (sockets / logs / configs) — index.ts creates them and passes them in. */
export interface BrainDirs {
	logsDir: string;
	socketsDir: string;
	configsDir: string;
}

/**
 * All run-scoped brain state, lifted to one object so the tool handlers, relayUp, and the phase
 * proxy share one source of truth. No `plan`/`cursor` — the BRAIN (the resident Pi LLM) picks the
 * phase, so there is no fixed cursor; `runSeq` only disambiguates sockets/logs for repeated runs of
 * the same phase (a backtrack). Mirrors sdk-leader.ts's LeaderState, minus the SDK MessageQueue
 * (Pi owns the turn loop).
 */
export interface BrainState {
	planPath: string;
	planSlug: string;
	availableAgents: string[];
	dirs: BrainDirs;
	/** The plan's run root (`~/.pi/meta-orch/<slug>/`) — holds the durable progress ledger. The
	 *  per-phase dirs in `dirs` all sit under it; this is the dir progress.jsonl lives in directly. */
	runDir: string;
	/** Cheap hash of the plan content at launch → the ledger flags a changed plan on the next reload. */
	planHash: string;
	/** The cheap model the per-phase proxy layer is configured to use (see index.ts note). */
	proxyModel: string;
	pendingAsks: Map<string, PendingAsk>;
	running: boolean;
	/** The live abort handle for the phase currently running, set when it starts and cleared when it
	 *  ends. session_shutdown calls abortRunningPhase(state) to kill the worker (+ its process group)
	 *  rather than leave it running after the brain goes away. null when no phase is running. */
	runningPhase: PhaseHandle | null;
	/** Monotonic per-run_phase counter → a fresh phase id each launch (backtrack-safe). */
	runSeq: number;
}

/** Best-effort structured logging — index.ts injects pi.appendEntry; tests inject a recorder. */
export type LogFn = (data: Record<string, unknown>) => void;

/** Push a follow-up turn into the brain's conversation (index.ts injects pi.sendMessage followUp +
 *  triggerTurn). Used to deliver a phase-completion summary so the brain reasons + continues. */
export type PushFollowUp = (text: string) => void;

/**
 * Surface one worker escalation to the brain — index.ts injects the pi.sendMessage(followUp,
 * triggerTurn) call that paints it in the TUI and fires a fresh leader turn. The escalation text is
 * built HERE (so it's tested) and carries the ask_id the brain echoes back via answer_worker.
 */
export type SendEscalation = (ask: AskEnvelope, escalationText: string) => void;

/** Build the escalation text the brain sees for one ask — names the severity, the phase, the
 *  question, and the exact answer_worker call to make. Pure so the wording is unit-tested. */
export function buildEscalationText(ask: AskEnvelope): string {
	const header =
		ask.severity === "blocked"
			? "worker BLOCKED"
			: ask.severity === "decision"
				? "worker needs a DECISION"
				: "worker progress";
	return [
		`${header} — phase ${ask.phaseId}`,
		"",
		ask.message,
		"",
		"Answer with the answer_worker tool:",
		`  answer_worker({ ask_id: "${ask.id}", answer: "<guidance>", stop: false })`,
		"Set stop:true only if this is fundamentally wrong and the phase should end.",
	].join("\n");
}

/**
 * Relay one worker escalation UP to the brain and return the promise the worker stays blocked on.
 * Records the pending ask, then asks index.ts to paint it in the TUI (via `sendEscalation`). The
 * returned promise settles when the brain-LLM calls answer_worker (handleAnswerWorker resolves it).
 * Byte-for-byte the RelayUp contract runPhase() expects — the SAME shape sdk-leader.ts's makeRelayUp
 * builds, only the "deliver to the brain" step is injected instead of pushing onto an SDK queue.
 */
export function makeRelayUp(state: BrainState, sendEscalation: SendEscalation): RelayUp {
	return function relayUp(ask: AskEnvelope): Promise<AnswerEnvelope> {
		return new Promise<AnswerEnvelope>((resolve) => {
			state.pendingAsks.set(ask.id, { ask, phaseId: ask.phaseId, resolve });
			sendEscalation(ask, buildEscalationText(ask));
		});
	};
}

/** The result the answer_worker handler hands back — index.ts wraps this as the Pi tool result. */
export interface AnswerWorkerResult {
	/** Human-readable line for the tool result. */
	text: string;
	/** True when the ask_id matched a pending ask (settled it); false when nothing matched. */
	matched: boolean;
	/** Whether the brain told the worker to STOP. */
	stop: boolean;
}

/**
 * Settle a blocked worker ask — the brain-LLM's answer_worker call lands here. Resolves the matching
 * pending ask with the AnswerEnvelope and removes it; an unknown ask_id is reported, not thrown
 * (it may have timed out or the phase already ended). Mirrors sdk-leader.ts's answer_worker handler.
 */
export function handleAnswerWorker(
	state: BrainState,
	params: { ask_id: string; answer: string; stop?: boolean },
	log: LogFn,
): AnswerWorkerResult {
	const pending = state.pendingAsks.get(params.ask_id);
	if (!pending) {
		return {
			text: `No pending worker ask with id ${params.ask_id} (it may have timed out or the phase ended).`,
			matched: false,
			stop: false,
		};
	}
	const stop = params.stop === true;
	pending.resolve(makeAnswer(params.ask_id, params.answer, stop));
	state.pendingAsks.delete(params.ask_id);
	log({ event: "answer_worker", askId: params.ask_id, stop });
	return {
		text: `Answered worker ask ${params.ask_id}${stop ? " with STOP — phase will end" : " — worker resumes"}.`,
		matched: true,
		stop,
	};
}

/** The per-phase proxy launcher run_phase calls. index.ts uses the real startPhaseInBackground
 *  (the in-process proxy that spawns the /run-phase worker); the unit test injects a stub so it can
 *  exercise the validation + running-guard WITHOUT spawning a real `claude`. */
export type PhaseStarter = (
	state: BrainState,
	relayUp: RelayUp,
	events: ProxyEvents,
	pushFollowUp: PushFollowUp,
	log: LogFn,
	phase: string,
	agents: string[],
	phaseId: string,
) => void;

/** The result the run_phase handler hands back — index.ts wraps this as the Pi tool result. */
export interface RunPhaseResult {
	text: string;
	/** True when a phase was actually started (running flipped, proxy fired). */
	started: boolean;
}

/**
 * Validate + fire one phase. The BRAIN supplies the phase + the agents IT chose (from the phase's
 * own list, or its pick when the phase says "brain, choose"). We:
 *  - reject a second concurrent run (single-running guard). Phases are SERIALIZED on purpose:
 *    parallel phases MUST NOT be enabled until each worker runs in its OWN git worktree, because
 *    every worker shares the one git index/working tree and concurrent commits would corrupt each
 *    other. There is no flag to relax this guard — when one is added it must hard-fail until
 *    per-worker worktrees exist (see the note at startPhaseInBackground),
 *  - reject an empty agent list and any agent not on the roster (fail loud to the brain NOW, not
 *    minutes later inside the worker),
 *  - bump runSeq + compute a backtrack-safe phase id, then fire the proxy (fire-and-return).
 * Mirrors sdk-leader.ts's run_phase tool exactly; only the result shape is framework-neutral.
 */
export function handleRunPhase(
	state: BrainState,
	params: { phase: string; agents: string[] },
	deps: { relayUp: RelayUp; events: ProxyEvents; pushFollowUp: PushFollowUp; log: LogFn; startPhase: PhaseStarter },
): RunPhaseResult {
	if (state.running) {
		// Single-running guard: phases are serialized. Do NOT lift this to allow parallel phases until
		// each worker gets its OWN git worktree — they share one git index, so concurrent commits clash.
		return { text: "A phase is already running — wait for its completion message before starting another.", started: false };
	}
	const agents = params.agents.map((a) => a.trim()).filter(Boolean);
	if (agents.length === 0) {
		return { text: "agents resolved to an empty list — a phase needs at least one agent. State the agents and call run_phase again.", started: false };
	}
	const unknown = agents.filter((a) => !state.availableAgents.includes(a));
	if (unknown.length > 0) {
		return {
			text: `Unknown agent(s): ${unknown.join(", ")}. Pick from the available agents: ${state.availableAgents.join(", ")}.`,
			started: false,
		};
	}
	// Mark running BEFORE firing so a second run_phase before completion is rejected above. Bump the
	// per-run sequence and compute the phase id once (backtrack-safe: a repeated phase gets a
	// distinct id), then fire-and-return — startPhase runs runPhase() in the background; its
	// completion arrives later as a follow-up turn, keeping the brain free to handle escalations.
	state.running = true;
	const phaseId = makeRunPhaseId(state.planSlug, params.phase, ++state.runSeq);
	deps.startPhase(state, deps.relayUp, deps.events, deps.pushFollowUp, deps.log, params.phase, agents, phaseId);
	return {
		text: `Started phase ${params.phase} (${phaseId}) with agents [${agents.join(", ")}] in the background. Its completion will arrive as a follow-up message; do not call run_phase again until it does.`,
		started: true,
	};
}

/** Resolve every ask still pending for a phase (the worker is gone — they cannot be answered).
 *  Mirrors sdk-leader.ts's post-runPhase drain. */
export function drainPendingAsksForPhase(state: BrainState, phaseId: string): void {
	for (const [id, pending] of Array.from(state.pendingAsks.entries())) {
		if (pending.phaseId === phaseId) {
			pending.resolve(makeAnswer(id, "phase ended before this could be answered", true));
			state.pendingAsks.delete(id);
		}
	}
}

/** Resolve ALL pending asks (the brain is shutting down) so any blocked worker's ask_brain returns
 *  STOP instead of hanging. Mirrors sdk-leader.ts's session_shutdown drain. */
export function drainAllPendingAsks(state: BrainState, reason: string): void {
	for (const [id, pending] of Array.from(state.pendingAsks.entries())) {
		pending.resolve(makeAnswer(id, reason, true));
	}
	state.pendingAsks.clear();
}

/**
 * Stop a running phase on shutdown: abort the live worker (kill its process group, close the socket,
 * clear the guard timer, end the log — runPhase's abort() does all of that) AND drain every pending
 * ask so no blocked worker hangs. Without the abort, draining the asks alone left the worker child
 * and the `claude` sub-processes it spawned running after the brain went away. Awaits the abort's
 * teardown so the caller can shut down cleanly. Safe when no phase is running (just drains).
 */
export async function abortRunningPhase(state: BrainState, reason: string): Promise<void> {
	const handle = state.runningPhase;
	state.runningPhase = null;
	if (handle) {
		// abort() kills the worker group + tears the phase down; its promise resolves when teardown is
		// complete. Never let an abort error block shutdown — swallow it (best-effort, like the drains).
		try {
			await handle.abort();
		} catch {
			/* best-effort shutdown — a failed abort must not block the brain from going down */
		}
	}
	drainAllPendingAsks(state, reason);
}

/**
 * The real in-process per-phase PROXY: fire the phase WITHOUT awaiting it and route its completion
 * back to the brain as a follow-up turn. This is the load-bearing shape (identical to the SDK
 * brain's startPhaseInBackground): runPhase() resolves only when the worker child exits (minutes),
 * so awaiting it inside the tool handler would block the brain-LLM for the whole phase and starve it
 * of escalations. Instead we fire-and-return and let runPhase()'s .then/.catch push the outcome —
 * keeping the brain free while a phase runs.
 *
 * Fire-and-return leaves ROOM for concurrent proxies, but the single-running guard in handleRunPhase
 * deliberately holds them to ONE at a time: parallel phases MUST NOT be enabled until each worker
 * runs in its own git worktree. Every worker today shares the one git index/working tree, so two
 * phases committing at once would corrupt each other's commits. Lifting the guard without per-worker
 * worktrees is a data-loss bug, not a config knob — so there is intentionally no flag to relax it.
 *
 * The proxy is a PURE MESSENGER: it spawns the `/run-phase` worker (buildRunPhaseArgs, via
 * runPhase's runPhaseCommand path), owns the per-phase Unix socket (startBackChannel inside
 * runPhase), relays the worker's ask_brain UP via `relayUp`, and routes the brain's answer back
 * DOWN the socket. It judges nothing — the brain does all the reasoning.
 */
export function startPhaseInBackground(
	state: BrainState,
	relayUp: RelayUp,
	events: ProxyEvents,
	pushFollowUp: PushFollowUp,
	log: LogFn,
	phase: string,
	agents: string[],
	phaseId: string,
): void {
	// The worker runs to completion like a plain `claude -p` (no turn cap, no liveness kill); the
	// markdown plan carries no per-phase guardrail fields, so the phase runs on the one built-in
	// default — a generous wall-clock timeout (the last-resort guard). The brain can STOP a phase
	// any time via answer_worker(stop:true); the human can steer in the TUI — those are the live controls.
	const limits = resolveLimits({});

	const finishPhase = (outcome: PhaseOutcome | null, errText?: string): void => {
		// The worker is gone — drop any asks still pending for this phase (mirrors the SDK brain).
		drainPendingAsksForPhase(state, phaseId);
		state.running = false;
		state.runningPhase = null; // the abort handle is dead now the phase has ended
		// Judge the TRUE semantic status (worker PHASE_RESULT verdict + breach), NOT the process exit.
		const report = errText ?? outcome?.report;
		const recorded = judgePhaseStatus({ outcomeStatus: outcome?.status ?? null, breachKind: outcome?.breach?.kind, report });
		log({ event: "phase_done", phaseId, phase, agents, status: recorded, processStatus: outcome?.status ?? "errored" });
		// Persist this terminal outcome to the durable ledger so a restart of the same plan RESUMES.
		// (A phase still in progress when the process dies never reaches here → no record → re-run.)
		recordPhaseProgress(state.runDir, phase, phaseId, outcome?.status ?? null, report, state.planHash, outcome?.breach?.kind);

		if (errText) {
			pushFollowUp(
				`phase ${phase} (${phaseId}) — could not run: ${errText}. ` +
					`Recorded as ${recorded}; the phase did NOT pass. Reason about the real cause, then call run_phase again (retry, or backtrack to an earlier phase).`,
			);
			return;
		}
		const o = outcome!;
		// A phase passes ONLY when the judged status is `passed` (worker certified done + evaluator
		// CLEARED). partial / blocked / failed / breached_* are NOT passes — tell the brain to re-run.
		const passed = recorded === "passed";
		const next = passed
			? "It PASSED. Reason about the outcome, then call run_phase for the next phase (or stop if the plan's goal is met)."
			: `It is ${recorded} — it did NOT pass. Reason about the REAL cause, then call run_phase again — retry with sharper instructions, or backtrack to an earlier phase if the fix lives upstream.`;
		pushFollowUp(
			[
				`phase ${phase} (${phaseId}) — ${recorded}${o.breach ? ` (breach: ${o.breach.kind})` : ""}.`,
				`Worker report: ${o.report.slice(0, 2000)}`,
				`Full transcript: ${o.logPath}`,
				next,
			].join("\n"),
		);
	};

	// Stale-socket self-heal for THIS phase's socket before the proxy binds it (a prior crashed run
	// may have left the file behind), THEN fire runPhase without awaiting.
	void healStaleSocket(socketPathFor(state.dirs.socketsDir, phaseId))
		.then(() => {
			log({ event: "phase_start", phaseId, phase, agents });
			runPhase({
				phaseId,
				runPhaseCommand: { planFile: state.planPath, phase, agents },
				limits,
				maxHops: DEFAULT_MAX_HOPS,
				dirs: state.dirs,
				relayUp,
				events,
				// Store the live abort handle so session_shutdown (abortRunningPhase) can kill this
				// worker + its process group instead of leaving it running after the brain is gone.
				onStart: (handle) => { state.runningPhase = handle; },
			})
				.then((outcome) => finishPhase(outcome))
				.catch((err) => finishPhase(null, err instanceof Error ? err.message : String(err)));
		})
		.catch((err) => finishPhase(null, err instanceof Error ? err.message : String(err)));
}
