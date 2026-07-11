/**
 * brain-core — the framework-free brain orchestration logic for the file-based hub-model Pi leader.
 *
 * The brain decides the phase + WRITES the instructions for the attempt, then calls ONE tool
 * (run_phase). The TS transport writes the instructions file, spawns an interactive Claude TUI worker
 * (`just worker-up`) that runs /meta-auto-run, WATCHES for the worker's results.md (the file whose
 * first line is `PHASE_RESULT: <verdict>` — the completion signal), reads it, judges it via
 * judgePhaseStatus, tears the worker down, and pushes the verdict back to the brain as a follow-up.
 * A not-passed phase is the brain's cue to rerun (a new iteration) or backtrack.
 *
 * The brain NEVER runs shell / tmux / just / files itself — the tool does all of it.
 *
 * Imports ONLY Node builtins + the transport + progress.ts + phase-id.ts (all Node-builtins-only), so
 * it loads under `node --experimental-strip-types` for tests. It NEVER imports
 * `@mariozechner/pi-coding-agent` or `@sinclair/typebox` — those stay in index.ts.
 */

import * as path from "node:path";
import { makeRunPhaseId, makeWorkerSessionName } from "./phase-id.ts";
import { hubRunPhase } from "./hub-transport.ts";
import type { PhaseOutcome, ProxyEvents, PhaseHandle, BrainDirs } from "./types.ts";
import { appendProgress, judgePhaseStatus } from "./progress.ts";

export const LOG_CHANNEL = "meta-orch-hub-log";

/**
 * Persist one phase's TERMINAL outcome to the run's durable ledger so a restart of the same plan can
 * RESUME instead of restart. The recorded status is the phase's TRUE semantic outcome
 * (judgePhaseStatus reads the worker's `PHASE_RESULT:` verdict out of `report`), NOT the worker
 * process exit. Best-effort: appendProgress swallows write errors, so persistence can never break
 * the run.
 */
export function recordPhaseProgress(
	runDir: string,
	phase: string,
	phaseId: string,
	outcomeStatus: string | null | undefined,
	report: string | undefined,
	planHash: string | undefined,
	log?: LogFn,
): void {
	appendProgress(
		runDir,
		{
			phase,
			status: judgePhaseStatus({ outcomeStatus, report }),
			timestamp: new Date().toISOString(),
			phaseId,
			summary: report ? report.slice(0, 1000) : undefined,
			planHash,
		},
		// appendProgress never throws (best-effort ledger), but a silently-lost entry breaks resume
		// (a re-run would redo an already-passed phase, or a human reading the ledger would see a gap
		// with no clue why) — surface it once via the caller's log channel instead of swallowing.
		log
			? (err) =>
					log({
						event: "progress_append_failed",
						runDir,
						phase,
						phaseId,
						error: err instanceof Error ? err.message : String(err),
					})
			: undefined,
	);
}

/**
 * All run-scoped brain state, lifted to one object so the tool handler and the phase transport share
 * one source of truth. The BRAIN picks the phase, so there is no fixed cursor; `iterations` counts
 * attempts PER phase so a rerun gets a fresh iteration dir + worker session. No mid-phase escalation
 * in this model.
 */
export interface BrainState {
	/** The brain's run name — namespaces the session dir + the worker tmux sessions. */
	sessionName: string;
	/** The session dir: `<META_ORCH_FILE_DIR>/<sessionName>/` — holds plan.md + phases/. */
	sessionDir: string;
	/** The copied plan: `<sessionDir>/plan.md` — the worker reads it via /meta-auto-run. */
	planPath: string;
	/** The copied route profile: `<sessionDir>/route.yaml` — the phase lead resolves llm_profile + agent routing from it. */
	routePath: string;
	availableAgents: string[];
	/** The transport's logs dir (under sessionDir). */
	dirs: BrainDirs;
	/** The run root for the durable progress ledger (the session dir). */
	runDir: string;
	/** Cheap hash of the plan content at launch → the ledger flags a changed plan on the next reload. */
	planHash: string;
	/** The discovery JSON path (or url) of the hub — passed to the worker's /meta-connect. */
	hubJsonPath: string;
	/** true when THIS brain auto-started the hub (so session_shutdown stops it). false when the hub
	 *  was already up (the human started it) — we must NOT tear down a hub we did not start. */
	hubStartedByUs: boolean;
	running: boolean;
	/** The live abort handle for the phase currently running, set when it starts and cleared when it
	 *  ends. session_shutdown calls abortRunningPhase(state) to tear the worker down. null when idle. */
	runningPhase: PhaseHandle | null;
	/** Per-phase attempt counter: iterations[phase] = how many times this phase has been run. A rerun
	 *  bumps it so the new attempt gets iteration/<n>/ and a distinct worker session. */
	iterations: Record<string, number>;
	/** The per-phase retry budget (from --max-retries, default 2). When a phase has already been
	 *  attempted this many times and still hasn't passed, handleRunPhase REFUSES a further attempt and
	 *  tells the brain to stop and ask the human. The human can raise it live (`meta-server:retries`). */
	maxRetries: number;
	/** The worker harness for the WHOLE run (from --worker-type, default "claude"). "claude" → a Claude
	 *  TUI worker; "pi" → a single-agent Pi worker. Set once at launch — not per phase. */
	workerType: "claude" | "pi";
	/** The Pi model id when workerType==="pi" (from --model or configured default); the Claude
	 *  worker takes no model (it runs the user's Claude). */
	workerModel: string;
	/** Claude only: synchronized meta execution resolves to subagent-style stage workers.
	 *  Kept for transport compatibility; route profiles choose the phase lead and stage agents. */
	workerMode: "subagents";
}

/** Best-effort structured logging — index.ts injects pi.appendEntry; tests inject a recorder. */
export type LogFn = (data: Record<string, unknown>) => void;

/** Push a follow-up turn into the brain's conversation (index.ts injects pi.sendMessage followUp +
 *  triggerTurn). Used to deliver a phase-completion summary so the brain reasons + continues. */
export type PushFollowUp = (text: string) => void;

/** The per-phase transport launcher run_phase calls. index.ts uses the real startPhaseInBackground;
 *  the unit test injects a stub so it can exercise the validation + the running-guard WITHOUT
 *  spawning a real worker. */
export type PhaseStarter = (
	state: BrainState,
	events: ProxyEvents,
	pushFollowUp: PushFollowUp,
	log: LogFn,
	phase: string,
	instructions: string,
	iteration: number,
	phaseId: string,
) => void;

/** The result the run_phase handler hands back — index.ts wraps this as the Pi tool result. */
export interface RunPhaseResult {
	text: string;
	/** True when a phase was actually started (running flipped, transport fired). */
	started: boolean;
}

/**
 * Validate + fire one phase. The BRAIN supplies the phase + the instructions IT wrote + the team
 * flag. We:
 *  - reject a second concurrent run (single-running guard). Phases are SERIALIZED on purpose:
 *    parallel phases MUST NOT be enabled until each worker runs in its OWN git worktree, because
 *    every worker shares the one git index/working tree and concurrent commits would corrupt each
 *    other. There is no flag to relax this guard,
 *  - reject empty instructions (the worker has nothing to do without them — fail loud NOW),
 *  - bump this phase's iteration counter + compute a backtrack-safe phase id, then fire the transport
 *    (fire-and-return).
 */
export function handleRunPhase(
	state: BrainState,
	params: { phase: string; instructions: string },
	deps: { events: ProxyEvents; pushFollowUp: PushFollowUp; log: LogFn; startPhase: PhaseStarter },
): RunPhaseResult {
	if (state.running) {
		// Single-running guard: phases are serialized. Do NOT lift this to allow parallel phases until
		// each worker gets its OWN git worktree — they share one git index, so concurrent commits clash.
		return { text: "A phase is already running — wait for its completion message before starting another.", started: false };
	}
	const phase = (params.phase ?? "").trim();
	if (!phase) {
		return { text: "phase is empty — name the phase to run (e.g. \"0\") and call run_phase again.", started: false };
	}
	const instructions = (params.instructions ?? "").trim();
	if (!instructions) {
		return { text: "instructions are empty — write the work this phase should do (its goal + done-check), then call run_phase again.", started: false };
	}
	// Retry-budget guard: a phase already attempted maxRetries times and still not passed gets NO
	// further attempt. Refuse here and tell the brain to STOP and ask the human — never silently burn
	// past the budget. The human can raise it live with `meta-server:retries <n>` and say continue.
	const used = state.iterations[phase] ?? 0;
	if (used >= state.maxRetries) {
		return {
			text: `phase ${phase} has used its retry budget (${state.maxRetries} attempt${state.maxRetries === 1 ? "" : "s"}) and still has not passed. STOP — do NOT start another attempt. Tell the human what you tried across those ${used} attempt${used === 1 ? "" : "s"} and ask how to proceed: they can raise the budget with \`meta-server:retries <n>\` and tell you to continue, or change the approach.`,
			started: false,
		};
	}
	// Mark running BEFORE firing so a second run_phase before completion is rejected above. Bump THIS
	// phase's iteration count and compute the phase id once (backtrack-safe), then fire-and-return.
	state.running = true;
	const iteration = (state.iterations[phase] = (state.iterations[phase] ?? 0) + 1);
	const phaseId = makeRunPhaseId(state.sessionName, phase, iteration);
	deps.startPhase(state, deps.events, deps.pushFollowUp, deps.log, phase, instructions, iteration, phaseId);
	const workerDesc = state.workerType === "pi" ? `pi:${state.workerModel}` : `claude:${state.workerMode}`;
	return {
		text: `Started phase ${phase} (iteration ${iteration}, ${phaseId}, worker=${workerDesc}) in the background. Its completion will arrive as a follow-up message; do not call run_phase again until it does.`,
		started: true,
	};
}

/**
 * Stop a running phase on shutdown: abort the live worker (tears the tmux worker down via the
 * transport's worker-down). Awaits the abort's teardown so the caller can shut down cleanly. Safe
 * when no phase is running (no-op).
 */
export async function abortRunningPhase(state: BrainState): Promise<void> {
	const handle = state.runningPhase;
	state.runningPhase = null;
	if (handle) {
		try {
			await handle.abort();
		} catch {
			/* best-effort shutdown — a failed abort must not block the brain from going down */
		}
	}
}

/** The iteration dir for one attempt: `<sessionDir>/phases/<phase>/iteration/<n>/`. */
export function iterationDir(sessionDir: string, phase: string, iteration: number): string {
	return path.join(sessionDir, "phases", phase, "iteration", String(iteration));
}

/**
 * The real per-phase TRANSPORT: fire the phase WITHOUT awaiting it and route its completion back to
 * the brain as a follow-up turn. hubRunPhase() resolves only when the worker writes results.md
 * (minutes), so awaiting it inside the tool handler would block the brain-LLM for the whole phase.
 * Instead we fire-and-return and let hubRunPhase()'s .then/.catch push the outcome — keeping the
 * brain free while a phase runs.
 *
 * The transport writes instructions.md, spawns the tmux /meta-auto-run worker, watches results.md,
 * and reads it as the report. It judges nothing — the brain reasons; judgePhaseStatus reads the
 * `PHASE_RESULT:` verdict out of the report.
 */
export function startPhaseInBackground(
	state: BrainState,
	events: ProxyEvents,
	pushFollowUp: PushFollowUp,
	log: LogFn,
	phase: string,
	instructions: string,
	iteration: number,
	phaseId: string,
): void {
	const finishPhase = (outcome: PhaseOutcome | null, errText?: string): void => {
		state.running = false;
		state.runningPhase = null; // the abort handle is dead now the phase has ended
		// Judge the TRUE semantic status (worker PHASE_RESULT verdict), NOT the process exit.
		const report = errText ?? outcome?.report;
		const recorded = judgePhaseStatus({ outcomeStatus: outcome?.status ?? null, report });
		log({ event: "phase_done", phaseId, phase, iteration, status: recorded, processStatus: outcome?.status ?? "errored" });
		// Persist this terminal outcome to the durable ledger so a restart of the same plan RESUMES.
		recordPhaseProgress(state.runDir, phase, phaseId, outcome?.status ?? null, report, state.planHash, log);

		if (errText) {
			pushFollowUp(
				`phase ${phase} (${phaseId}) — could not run: ${errText}. ` +
					`Recorded as ${recorded}; the phase did NOT pass. Reason about the real cause, then call run_phase again (rerun with sharper instructions, or backtrack to an earlier phase).`,
			);
			return;
		}
		const o = outcome!;
		// A phase passes ONLY when the judged status is `passed` (worker certified done + evaluator
		// CLEARED). partial / blocked / failed are NOT passes — tell the brain to rerun.
		const passed = recorded === "passed";
		const next = passed
			? "It PASSED. Reason about the outcome, then call run_phase for the next phase (or stop if the plan's goal is met)."
			: `It is ${recorded} — it did NOT pass. Reason about the REAL cause, then call run_phase again — rerun with sharper instructions (a new iteration), or backtrack to an earlier phase if the fix lives upstream.`;
		const resultsPath = path.join(iterationDir(state.sessionDir, phase, iteration), "results.md");
		pushFollowUp(
			[
				`phase ${phase} (${phaseId}) — ${recorded}.`,
				`Results file: ${resultsPath}`,
				`Worker report: ${o.report.slice(0, 2000)}`,
				`Full transcript: ${o.logPath}`,
				next,
			].join("\n"),
		);
	};

	const dir = iterationDir(state.sessionDir, phase, iteration);
	const instructionsPath = path.join(dir, "instructions.md");
	const resultsPath = path.join(dir, "results.md");
	const session = makeWorkerSessionName(state.sessionName, phase, iteration);

	log({ event: "phase_start", phaseId, phase, iteration, session, workerType: state.workerType, model: state.workerModel, mode: state.workerMode });
	hubRunPhase({
		phaseId,
		runPhaseCommand: { planFile: state.planPath, phase, instructions, instructionsPath, routeFile: state.routePath, resultsPath, session, workerType: state.workerType, model: state.workerModel, mode: state.workerMode },
		dirs: state.dirs,
		hubJsonPath: state.hubJsonPath,
		events,
		// Store the live abort handle so session_shutdown (abortRunningPhase) can tear this worker down.
		onStart: (handle) => { state.runningPhase = handle; },
	})
		.then((outcome) => finishPhase(outcome))
		.catch((err) => finishPhase(null, err instanceof Error ? err.message : String(err)));
}
