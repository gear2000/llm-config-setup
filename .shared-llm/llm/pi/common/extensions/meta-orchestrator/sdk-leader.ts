/**
 * sdk-leader — the standalone SDK BRAIN for the meta-orchestrator (the Pi-free alternative).
 *
 * Run from a shell:  node --experimental-strip-types sdk-leader.ts <plan.md>
 *
 * This is the second leader. The Pi leader (index.ts) is a resident extension you watch in Pi's
 * TUI; this one is a plain Node process driven by the Claude Agent SDK. Both drive the SAME worker
 * half (claude-proxy.ts + back-channel.ts + the spawned `claude -p` child) — only the brain's
 * harness changes. Nothing here imports Pi.
 *
 * What makes this "the brain" (the approved design — brain-execute-plan-prompt.md):
 *  - The argument is a PLAN MARKDOWN FILE (ordered phases, Phase 0 first), not the old JSON.
 *  - Its system prompt IS that brain-prompt, with `<PLAN_FILE>` and `<AVAILABLE_AGENTS>` filled in.
 *    The plan's CONTENT is also injected as the first user message, and the brain has the Read tool,
 *    so it can reason over the whole plan.
 *  - The brain PICKS the phase and the agents itself: the in-process `run_phase({phase, agents})`
 *    tool fire-and-returns a `claude -p "/run-phase plan=<file> phase=<N> agents=<list>"` worker
 *    (buildRunPhaseArgs). Because the brain chooses the phase, it can BACKTRACK — re-run Phase 0
 *    after Phase 3 — which the old cursor-based `run_next_phase` could not.
 *  - It is INTERACTIVE: a line typed on stdin is injected into the streaming-input queue as a user
 *    message, so the human can answer an escalation or steer ("redo phase 0 first"). When the human
 *    is silent the brain runs autonomously; stdin just lets the human take the controls.
 *
 * The mechanism the Phase-0 spike proved live, kept intact: ONE long-lived `query()` with a
 * streaming-input async generator. The leader parks idle on an empty queue; when a worker escalates,
 * a phase finishes, or the human types a line, we PUSH an SDKUserMessage and the leader-LLM takes a
 * fresh turn. answer_worker settles a blocked worker; run_phase fire-and-returns the real runPhase()
 * — both run in-process on the host event loop, so a tool handler can resolve a Promise the host
 * awaits with no cross-thread hazard.
 */

import { query, tool, createSdkMcpServer, type SDKUserMessage } from "@anthropic-ai/claude-agent-sdk";
import { z } from "zod";
import { EventEmitter } from "node:events";
import * as os from "node:os";
import * as path from "node:path";
import * as fs from "node:fs";
import * as readline from "node:readline";
import { fileURLToPath } from "node:url";
import { makeRunPhaseId, socketPathFor } from "./phase-id.ts";
import { type AskEnvelope, type AnswerEnvelope, makeAnswer, DEFAULT_MAX_HOPS } from "./envelope.ts";
import { runPhase, type PhaseOutcome, type RelayUp, type ProxyEvents, type PhaseHandle } from "./claude-proxy.ts";
import { resolveLimits } from "./guardrails.ts";
import { healStaleSocket } from "./back-channel.ts";
// The prompt-fill + roster helpers are SHARED with the Pi brain (index.ts) — they live in
// brain-prompt.ts so the Pi extension can reuse them without pulling in this module's SDK deps.
import { buildSystemPrompt, loadAvailableAgents, planSlug } from "./brain-prompt.ts";
// The durable progress ledger (RESUME-FROM-PROGRESS) is SHARED with the Pi brain: recordPhaseProgress
// lives in brain-core.ts (the one persist point both finishPhase impls call), the read/build/archive
// helpers in progress.ts. Both are Node-builtins-only, so they load under --experimental-strip-types.
import { recordPhaseProgress } from "./brain-core.ts";
import { readProgress, buildPriorProgressBlock, archiveProgress, pruneRunArtifacts, planContentHash, judgePhaseStatus } from "./progress.ts";

const LOG_CHANNEL = "meta-orch-log";

/** Per-run working dirs under ~/.pi/meta-orch/<slug>/ — sockets, logs, configs. Same namespace as
 *  the Pi leader (phase-id helpers are reused as-is), so both leaders share one layout. */
function runRoot(slug: string): string {
	const base = process.env.META_ORCH_DIR || path.join(os.homedir(), ".pi", "meta-orch");
	return path.join(base, slug);
}

/** An ask awaiting the leader-LLM's answer_worker call, keyed by ask id. The worker is blocked on
 *  the socket until one of these resolves. Mirrors index.ts's PendingAsk. */
interface PendingAsk {
	ask: AskEnvelope;
	phaseId: string;
	resolve: (answer: AnswerEnvelope) => void;
}

/**
 * The streaming-input queue the leader's `query()` drains. Parks on an EMPTY queue (await the
 * "message" event) and yields the moment something is pushed — so an injected escalation, a
 * phase-done message, or a human stdin line wakes the generator immediately, not by polling.
 */
class MessageQueue extends EventEmitter {
	private q: SDKUserMessage[] = [];
	private closed = false;

	push(text: string): void {
		this.q.push({
			type: "user",
			session_id: "",
			parent_tool_use_id: null,
			message: { role: "user", content: text },
		} as SDKUserMessage);
		this.emit("message");
	}

	/** Stop the generator after the in-flight item drains — ends the long-lived query() cleanly. */
	close(): void {
		this.closed = true;
		this.emit("message");
	}

	async *[Symbol.asyncIterator](): AsyncIterator<SDKUserMessage> {
		while (true) {
			if (this.q.length) {
				yield this.q.shift()!;
			} else if (this.closed) {
				return;
			} else {
				await new Promise<void>((r) => this.once("message", () => r()));
			}
		}
	}
}

/** All run-scoped brain state, lifted to one object so the tools, relayUp, and the host loop share
 *  one source of truth. No `plan`/`cursor` — the BRAIN picks the phase, so there is no fixed cursor;
 *  `runSeq` only disambiguates sockets/logs for repeated runs of the same phase (a backtrack). */
interface LeaderState {
	planPath: string;
	planSlug: string;
	availableAgents: string[];
	dirs: { logsDir: string; socketsDir: string; configsDir: string };
	/** The plan's run root (`~/.pi/meta-orch/<slug>/`) — holds the durable progress ledger. */
	runDir: string;
	/** Cheap hash of the plan content at launch → the ledger flags a changed plan on the next reload. */
	planHash: string;
	queue: MessageQueue;
	pendingAsks: Map<string, PendingAsk>;
	runLogPath: string;
	running: boolean;
	/** The live abort handle for the phase currently running, set when it starts and cleared when it
	 *  ends. onShutdown calls abortRunningPhase to kill the worker (+ its process group) rather than
	 *  leave it running after the leader goes away (Ctrl-C / exit). null when no phase is running. */
	runningPhase: PhaseHandle | null;
	/** Monotonic per-run_phase counter → a fresh phase id each launch (backtrack-safe). */
	runSeq: number;
}

/**
 * The SDK brain's ProxyEvents — stands in for the Pi leader's ctx.ui.* / pi.appendEntry. Curated
 * lines go to stderr; structured events go to a JSONL run-log. Raw worker progress already lands in
 * the per-phase <id>.log inside claude-proxy.ts, so the leader channel stays terse.
 */
function makeProxyEvents(state: LeaderState): ProxyEvents {
	const logLine = (data: Record<string, unknown>) => {
		try {
			fs.appendFileSync(state.runLogPath, JSON.stringify({ ts: new Date().toISOString(), ...data }) + "\n");
		} catch {
			// best-effort run-log; never let logging break the run
		}
	};
	return {
		onProgress: (phaseId, line) => {
			// Keep the leader channel quiet — raw progress goes to the on-disk <id>.log. A terse stderr
			// breadcrumb is enough to follow along without flooding the brain.
			process.stderr.write(`· ${phaseId} ${line.slice(0, 80)}\n`);
		},
		onNotify: (phaseId, message, level) => {
			process.stderr.write(`[${level}] ${phaseId}: ${message}\n`);
			logLine({ event: "notify", phaseId, level, message });
		},
		onLog: (_channel, data) => logLine(data),
	};
}

/**
 * Relay one worker escalation UP to the leader. The Pi leader paints it in the TUI with
 * triggerTurn; here we PUSH it onto the streaming-input queue so the leader-LLM takes a fresh turn,
 * and return the promise the worker stays blocked on. claude-proxy.ts's resolver calls this whenever
 * the worker's ask_brain fires; the returned promise settles when the leader-LLM calls answer_worker.
 * Byte-for-byte the RelayUp contract runPhase() expects.
 */
function makeRelayUp(state: LeaderState): RelayUp {
	return function relayUp(ask: AskEnvelope): Promise<AnswerEnvelope> {
		return new Promise<AnswerEnvelope>((resolve) => {
			state.pendingAsks.set(ask.id, { ask, phaseId: ask.phaseId, resolve });
			const header =
				ask.severity === "blocked" ? "worker BLOCKED" : ask.severity === "decision" ? "worker needs a DECISION" : "worker progress";
			state.queue.push(
				[
					`${header} — phase ${ask.phaseId}`,
					"",
					ask.message,
					"",
					`Answer with the answer_worker tool:`,
					`  answer_worker({ ask_id: "${ask.id}", answer: "<guidance>", stop: false })`,
					`Set stop:true only if this is fundamentally wrong and the phase should end.`,
				].join("\n"),
			);
		});
	};
}

/** Resolve every ask still pending for a phase (the worker is gone — they cannot be answered).
 *  Mirrors the Pi leader's post-runPhase drain. */
function drainPendingAsksForPhase(state: LeaderState, phaseId: string): void {
	for (const [id, pending] of Array.from(state.pendingAsks.entries())) {
		if (pending.phaseId === phaseId) {
			pending.resolve(makeAnswer(id, "phase ended before this could be answered", true));
			state.pendingAsks.delete(id);
		}
	}
}

/** Resolve ALL pending asks (leader is shutting down) so any blocked worker's ask_brain returns
 *  STOP instead of hanging. Mirrors the Pi leader's session_shutdown handler. */
function drainAllPendingAsks(state: LeaderState, reason: string): void {
	for (const [id, pending] of Array.from(state.pendingAsks.entries())) {
		pending.resolve(makeAnswer(id, reason, true));
	}
	state.pendingAsks.clear();
}

/**
 * Fire a phase WITHOUT awaiting it (fire-and-return) and route its completion back as an injected
 * message. The load-bearing shape: runPhase() resolves only when the worker child exits (minutes),
 * so awaiting it inside the tool handler would block the leader-LLM for the whole phase and starve
 * the generator of escalations. Instead we return the tool immediately and let runPhase()'s
 * .then/.catch enqueue the outcome — keeping the leader IDLE while a phase runs.
 *
 * The BRAIN supplies the phase + agents (it chose them); we spawn `/run-phase` via runPhase's
 * runPhaseCommand path. runPhase() itself is UNCHANGED (shared with the Pi leader).
 */
function startPhaseInBackground(state: LeaderState, relayUp: RelayUp, events: ProxyEvents, phase: string, agents: string[], phaseId: string): void {
	// The worker runs to completion like a plain `claude -p` (no turn cap, no liveness kill); the
	// markdown plan carries no per-phase guardrail fields, so the phase runs on the one built-in
	// default — a generous wall-clock timeout (the last-resort guard). The brain can STOP a phase
	// any time via answer_worker(stop:true), and the human can steer via stdin — those are the live controls.
	const limits = resolveLimits({});

	const finishPhase = (outcome: PhaseOutcome | null, errText?: string): void => {
		// The worker is gone — drop any asks still pending for this phase (mirrors the Pi leader).
		drainPendingAsksForPhase(state, phaseId);
		state.running = false;
		state.runningPhase = null; // the abort handle is dead now the phase has ended
		// Judge the TRUE semantic status (worker PHASE_RESULT verdict + breach), NOT the process exit.
		const report = errText ?? outcome?.report;
		const recorded = judgePhaseStatus({ outcomeStatus: outcome?.status ?? null, breachKind: outcome?.breach?.kind, report });
		events.onLog?.(LOG_CHANNEL, { event: "phase_done", phaseId, phase, agents, status: recorded, processStatus: outcome?.status ?? "errored" });
		// Persist this terminal outcome to the durable ledger so a restart of the same plan RESUMES.
		// (A phase still in progress when the process dies never reaches here → no record → re-run.)
		recordPhaseProgress(state.runDir, phase, phaseId, outcome?.status ?? null, report, state.planHash, outcome?.breach?.kind);

		if (errText) {
			state.queue.push(
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
		state.queue.push(
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
			events.onLog?.(LOG_CHANNEL, { event: "phase_start", phaseId, phase, agents });
			runPhase({
				phaseId,
				runPhaseCommand: { planFile: state.planPath, phase, agents },
				limits,
				maxHops: DEFAULT_MAX_HOPS,
				dirs: state.dirs,
				relayUp,
				events,
				// Store the live abort handle so onShutdown (abortRunningPhase) can kill this worker + its
				// process group instead of leaving it running after the leader is gone (Ctrl-C / exit).
				onStart: (handle) => { state.runningPhase = handle; },
			})
				.then((outcome) => finishPhase(outcome))
				.catch((err) => finishPhase(null, err instanceof Error ? err.message : String(err)));
		})
		.catch((err) => finishPhase(null, err instanceof Error ? err.message : String(err)));
}

/** The fire-and-return phase launcher run_phase calls. Defaults to startPhaseInBackground (the real
 *  worker spawn); the unit test injects a stub so it can exercise the validation + running-guard
 *  WITHOUT spawning a real `claude` (and without the socket/log side effects that would entail). */
type PhaseStarter = (state: LeaderState, relayUp: RelayUp, events: ProxyEvents, phase: string, agents: string[], phaseId: string) => void;

/** Build the two in-process SDK tools + the brain MCP server. answer_worker settles a blocked ask
 *  exactly as the Pi leader does; run_phase fire-and-returns the real runPhase() for the BRAIN-chosen
 *  phase + agents. */
function buildBrainServer(state: LeaderState, relayUp: RelayUp, events: ProxyEvents, startPhase: PhaseStarter = startPhaseInBackground) {
	const answerWorker = tool(
		"answer_worker",
		"Answer a worker escalation that is BLOCKING on the back-channel. Provide the ask_id from the " +
			"escalation message and your guidance. Set stop:true ONLY when the work is fundamentally wrong " +
			"and the phase should end now; otherwise stop:false and the worker resumes on your answer.",
		{
			ask_id: z.string(),
			answer: z.string(),
			stop: z.boolean().optional().default(false),
		},
		async (args) => {
			const pending = state.pendingAsks.get(args.ask_id);
			if (!pending) {
				return {
					content: [
						{ type: "text" as const, text: `No pending worker ask with id ${args.ask_id} (it may have timed out or the phase ended).` },
					],
				};
			}
			const stop = args.stop === true;
			pending.resolve(makeAnswer(args.ask_id, args.answer, stop));
			state.pendingAsks.delete(args.ask_id);
			events.onLog?.(LOG_CHANNEL, { event: "answer_worker", askId: args.ask_id, stop });
			return {
				content: [
					{ type: "text" as const, text: `Answered worker ask ${args.ask_id}${stop ? " with STOP — phase will end" : " — worker resumes"}.` },
				],
			};
		},
	);

	const runPhaseTool = tool(
		"run_phase",
		"Run ONE phase of the plan with an EXPLICIT agent list YOU choose. Pass the phase number " +
			"(Phase 0 is valid) and the comma-resolved agents for that phase — from the phase's own list, " +
			"or your pick when the phase says 'brain, choose the agents'. The phase runs in the background " +
			"(spawns `claude -p \"/run-phase plan=<file> phase=<N> agents=<list>\"`); its completion arrives " +
			"as a follow-up message. You pick the phase, so you may BACKTRACK — re-run an earlier phase " +
			"when a later failure's real cause is upstream. Do not call run_phase again until the running " +
			"phase's completion message has arrived.",
		{
			phase: z.string().describe("The phase number to run, e.g. \"0\" or \"3\"."),
			agents: z.array(z.string()).min(1).describe("The exact agent names for this phase (from the plan, or your choice)."),
		},
		async (args) => {
			if (state.running) {
				// Single-running guard: phases are serialized. Do NOT lift this to allow parallel phases
				// until each worker gets its OWN git worktree — workers share one git index, so two
				// phases committing at once would corrupt each other. There is no flag to relax this.
				return { content: [{ type: "text" as const, text: "A phase is already running — wait for its completion message before starting another." }] };
			}
			const agents = args.agents.map((a) => a.trim()).filter(Boolean);
			if (agents.length === 0) {
				return { content: [{ type: "text" as const, text: "agents resolved to an empty list — a phase needs at least one agent. State the agents and call run_phase again." }] };
			}
			// Validate every agent against the roster the brain was given — fail loud here so a typo
			// surfaces to the brain immediately rather than dying inside the worker minutes later.
			const unknown = agents.filter((a) => !state.availableAgents.includes(a));
			if (unknown.length > 0) {
				return {
					content: [
						{
							type: "text" as const,
							text: `Unknown agent(s): ${unknown.join(", ")}. Pick from the available agents: ${state.availableAgents.join(", ")}.`,
						},
					],
				};
			}
			// Mark running BEFORE firing so a second run_phase before completion is rejected above.
			// Bump the per-run sequence and compute the phase id once here (backtrack-safe: a repeated
			// phase gets a distinct id), then fire-and-return — startPhase runs runPhase() in the
			// background; its completion arrives later as an injected message, keeping the leader idle.
			state.running = true;
			const phaseId = makeRunPhaseId(state.planSlug, args.phase, ++state.runSeq);
			startPhase(state, relayUp, events, args.phase, agents, phaseId);
			return {
				content: [
					{
						type: "text" as const,
						text: `Started phase ${args.phase} (${phaseId}) with agents [${agents.join(", ")}] in the background. Its completion will arrive as a follow-up message; do not call run_phase again until it does.`,
					},
				],
			};
		},
	);

	const server = createSdkMcpServer({ name: "brain", version: "1.0.0", tools: [answerWorker, runPhaseTool] });
	// Return the tool defs alongside the server so the unit test can call the handlers directly
	// (each is a plain async fn) without standing up a real query().
	return { server, answerWorker, runPhaseTool };
}

/**
 * Wire the human's keyboard into the run. Every line typed on stdin is pushed onto the streaming-
 * input queue as a user message, so the leader-LLM takes a fresh turn on it — this is how the human
 * answers an escalation or steers ("redo phase 0 first", "use option A"). The brain still runs
 * autonomously when stdin is silent; this only lets the human take the controls.
 *
 * Returns a close fn (stop reading on shutdown). On stdin EOF we do NOT end the run — the brain may
 * still have phases to finish; the human simply can no longer chime in.
 */
function wireStdin(state: LeaderState, events: ProxyEvents, input: NodeJS.ReadableStream = process.stdin): () => void {
	const stdinLike = input as NodeJS.ReadStream;
	if (input === process.stdin && !stdinLike.isTTY && process.env.META_ORCH_FORCE_STDIN !== "1") {
		// Non-interactive stdin (piped/closed) and not explicitly forced: nothing to read. The run
		// stays fully autonomous. (META_ORCH_FORCE_STDIN=1 lets a test/script feed lines on a pipe;
		// passing an explicit `input` stream — as the unit test does — also bypasses this gate.)
		if (stdinLike.readableEnded || !stdinLike.readable) return () => {};
	}
	const rl = readline.createInterface({ input });
	rl.on("line", (raw) => {
		const line = raw.trim();
		if (!line) return; // ignore bare Enter
		events.onLog?.(LOG_CHANNEL, { event: "human_input", line });
		process.stderr.write(`» you: ${line}\n`);
		state.queue.push(`The human typed (take the controls / answer if this addresses an open escalation): ${line}`);
	});
	rl.on("close", () => {
		events.onLog?.(LOG_CHANNEL, { event: "stdin_closed" });
	});
	return () => { try { rl.close(); } catch { /* already closed */ } };
}

async function main(): Promise<void> {
	const planArg = (process.argv[2] || "").trim();
	if (!planArg) {
		process.stderr.write("Usage: node --experimental-strip-types sdk-leader.ts <plan.md>\n");
		process.exit(2);
	}
	const planPath = path.isAbsolute(planArg) ? planArg : path.resolve(process.cwd(), planArg);

	// Fail loud if the plan file is missing — the plan IS the work; never half-run on a guessed path.
	let planContent: string;
	try {
		planContent = fs.readFileSync(planPath, "utf-8");
	} catch (err) {
		const code = err && typeof err === "object" && "code" in err ? (err as NodeJS.ErrnoException).code : undefined;
		if (code === "ENOENT") {
			process.stderr.write(`meta-orch: plan file not found: ${planPath}\n`);
			process.exit(2);
		}
		throw err;
	}

	const availableAgents = loadAvailableAgents();
	const systemPrompt = buildSystemPrompt(planPath, availableAgents);

	const slug = planSlug(planPath);
	const root = runRoot(slug);
	const dirs = {
		logsDir: path.join(root, "logs"),
		socketsDir: path.join(root, "sockets"),
		configsDir: path.join(root, "configs"),
	};
	fs.mkdirSync(dirs.logsDir, { recursive: true });
	fs.mkdirSync(dirs.socketsDir, { recursive: true });
	fs.mkdirSync(dirs.configsDir, { recursive: true });

	const state: LeaderState = {
		planPath,
		planSlug: slug,
		availableAgents,
		dirs,
		runDir: root,
		planHash: planContentHash(planContent),
		queue: new MessageQueue(),
		pendingAsks: new Map<string, PendingAsk>(),
		runLogPath: path.join(root, "run-log.jsonl"),
		running: false,
		runningPhase: null,
		runSeq: 0,
	};

	const events = makeProxyEvents(state);

	// RESUME-FROM-PROGRESS: read the durable ledger for THIS plan-slug. The `--fresh` arg (or
	// META_ORCH_FRESH=1) archives it and starts from the top; otherwise a non-empty ledger becomes a
	// PRIOR PROGRESS block injected into the first turn, so the brain skips already-passed phases.
	const fresh = process.env.META_ORCH_FRESH === "1" || process.argv.slice(3).includes("--fresh");
	if (fresh) {
		archiveProgress(root);
		// Prune the prior run's accumulated logs/configs/stale-sockets — `--fresh` ignores that run,
		// so its per-phase artifacts are dead weight that would otherwise grow unbounded.
		const pruned = pruneRunArtifacts(dirs);
		events.onLog?.(LOG_CHANNEL, { event: "fresh_start", slug, pruned });
	}
	const priorProgress = fresh ? null : buildPriorProgressBlock(readProgress(root), state.planHash);

	// relayUp pushes a worker escalation onto the queue + returns the blocking promise; it is handed
	// to runPhase() inside run_phase so claude-proxy's resolver can reach the leader.
	const relayUp = makeRelayUp(state);

	const { server: brain } = buildBrainServer(state, relayUp, events);

	// On shutdown (Ctrl-C / process exit): KILL the worker that is running (if any) AND release any
	// worker still blocked on an ask so its ask_brain returns STOP instead of hanging. Mirrors the Pi
	// leader's session_shutdown. The old behaviour drained the asks only, which left the live worker
	// child + the `claude` sub-processes it spawned running after the leader was gone.
	const closeStdin = wireStdin(state, events);
	let draining = false;
	const onShutdown = () => {
		if (draining) return;
		draining = true;
		closeStdin();
		// abort() issues the process-group kill SYNCHRONOUSLY (before it returns its teardown promise),
		// so even the hard SIGINT-then-exit path below still signals the worker's whole group. We let
		// the teardown promise (socket unlink + log close) run best-effort; on SIGINT the process exits
		// right after, but the SIGTERM has already gone to the group.
		const handle = state.runningPhase;
		state.runningPhase = null;
		if (handle) {
			void Promise.resolve(handle.abort()).catch(() => { /* best-effort shutdown */ });
		}
		drainAllPendingAsks(state, "leader is shutting down");
		state.queue.close();
	};
	process.on("SIGINT", () => { onShutdown(); process.exit(130); });
	process.on("beforeExit", onShutdown);

	events.onLog?.(LOG_CHANNEL, { event: "leader_start", slug, planPath, agents: availableAgents.length });
	process.stderr.write(
		`meta-orch SDK brain — plan ${planPath}. ${availableAgents.length} agents available. ` +
			`Run-log: ${state.runLogPath}. Worker transcripts on disk in ${state.dirs.logsDir}/<id>.log. ` +
			`Type a line to steer the brain at any time.\n`,
	);

	// Kick the brain off: inject the plan CONTENT as the first user message so it can reason over the
	// whole plan immediately (it also has the Read tool to re-read it). The system prompt is the
	// brain-prompt; this message just delivers the plan and tells it to begin its loop. When a PRIOR
	// PROGRESS block exists (a partial earlier run of this plan), it is injected too so the brain
	// RESUMES from the first not-yet-passed phase instead of restarting from the top.
	const kickoff: string[] = [
		`Here is the plan to execute (also at ${planPath}; you have the Read tool to re-read it):`,
		"",
		"----- PLAN BEGIN -----",
		planContent,
		"----- PLAN END -----",
	];
	if (priorProgress) {
		kickoff.push("", priorProgress);
		kickoff.push(
			"",
			"Read the ENTIRE plan, then RESUME per the PRIOR PROGRESS block above: do NOT re-run already-passed phases — resume from the first not-yet-passed phase (announce your resume decision first). Run autonomously; the human can type a line to steer you at any time.",
		);
	} else {
		kickoff.push(
			"",
			"Read the ENTIRE plan, then begin your loop: pick the phase (Phase 0 first), decide the agents per the phase's contract, and call run_phase. Run autonomously; the human can type a line to steer you at any time.",
		);
	}
	state.queue.push(kickoff.join("\n"));

	const q = query({
		prompt: state.queue,
		options: {
			mcpServers: { brain },
			// The brain is a DIRECTOR, not a doer (brain-execute-plan-prompt.md, rule 1). Its ONLY tools
			// are the two brain MCP tools — it picks the phase + agents (run_phase) and answers worker
			// escalations (answer_worker). It is deliberately given NO Read/Edit/Bash: every file read,
			// edit, command, and fix is done by a WORKER it launches. Withholding work-tools is what keeps
			// the long-lived brain LIGHT — a brain that pulled file contents / command output into its own
			// context would bloat and stall mid-plan, the exact failure this guards against. The plan's
			// full text is injected into the kick-off message below, so the brain needs no Read to reason
			// over it.
			allowedTools: ["mcp__brain__answer_worker", "mcp__brain__run_phase"],
			permissionMode: "dontAsk",
			settingSources: ["user", "project"],
			systemPrompt,
			// The brain is the LIGHT, LONG-LIVED orchestrator: it must run for as many turns as the plan
			// needs (one+ per phase across a long multi-phase run) and is never cut off by a turn ceiling.
			// (A per-phase WORKER has no turn cap either — its only limit is the generous wall-clock
				// timeout in guardrails.ts; it otherwise runs to completion like a plain `claude -p`.) The SDK
			// applies no maxTurns default, but we set it EXPLICITLY to an effectively-unbounded value so no
			// future SDK default — or an inherited setting — can ever cap the brain mid-plan. The real,
			// intended end of a brain run is: the plan's goal met, the human stops it (SIGINT/EOF), or it
			// escalates and waits — never a turn ceiling.
			maxTurns: Number.MAX_SAFE_INTEGER,
		},
	});

	for await (const msg of q) {
		const t = (msg as { type?: string }).type;
		if (t === "system" && (msg as { subtype?: string }).subtype === "init") {
			const m = msg as { model?: string; permissionMode?: string; apiKeySource?: string };
			events.onLog?.(LOG_CHANNEL, { event: "init", model: m.model, permissionMode: m.permissionMode, apiKeySource: m.apiKeySource });
		}
		if (t === "assistant") {
			const blocks = (msg as { message?: { content?: Array<{ type: string; text?: string }> } }).message?.content ?? [];
			for (const b of blocks) {
				if (b.type === "text" && b.text?.trim()) process.stdout.write(b.text + "\n");
			}
		}
		// The brain decides when the plan's goal is met; it ends the run by saying so. We do NOT
		// auto-close on a cursor here (there is no cursor — the brain may backtrack). The run ends on
		// SIGINT/EOF (onShutdown closes the queue) or when the human stops it.
	}

	// The query() generator has ended. With streaming input the queue parks on an empty queue and only
	// returns when WE close it (onShutdown), so a clean end means `draining` is set. If the generator
	// ended while we did NOT ask it to AND work is still outstanding (a phase running or worker asks
	// pending), the brain stopped PREMATURELY — surface that LOUDLY instead of masking it as "done", so
	// a brain that quit mid-plan is never reported as a clean finish. (This guards the user-reported
	// symptom; resume-from-progress will re-run anything not truly passed on the next launch.)
	if (!draining && (state.running || state.pendingAsks.size > 0)) {
		events.onLog?.(LOG_CHANNEL, { event: "leader_end_premature", slug, running: state.running, pendingAsks: state.pendingAsks.size });
		process.stderr.write(
			`meta-orch SDK brain ENDED PREMATURELY — the query() stream closed while work was still ` +
				`outstanding (phase running=${state.running}, pending worker asks=${state.pendingAsks.size}). ` +
				`The plan is NOT finished. Re-launch to resume from the durable progress ledger.\n`,
		);
		drainAllPendingAsks(state, "leader ended prematurely");
		process.exitCode = 1;
		return;
	}

	events.onLog?.(LOG_CHANNEL, { event: "leader_end", slug });
	process.stderr.write(`meta-orch SDK brain done — plan ${planPath}.\n`);
}

// Run main() only when this file is the process entry point — never on import (the unit test imports
// __test and must not trigger a real query()).
if (process.argv[1] && fileURLToPath(import.meta.url) === path.resolve(process.argv[1])) {
	main().catch((err) => {
		process.stderr.write(`sdk-leader fatal: ${err instanceof Error ? err.stack : String(err)}\n`);
		process.exit(1);
	});
}

// Exported for the unit test (Phase 4) — exercised without a real query().
export const __test = {
	MessageQueue,
	makeRelayUp,
	makeProxyEvents,
	drainPendingAsksForPhase,
	drainAllPendingAsks,
	buildBrainServer,
	startPhaseInBackground,
	buildSystemPrompt,
	loadAvailableAgents,
	wireStdin,
	planSlug,
};
export type { LeaderState, PendingAsk };
