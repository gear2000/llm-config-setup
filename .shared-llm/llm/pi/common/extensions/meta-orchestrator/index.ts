/**
 * meta-orchestrator — the resident Pi BRAIN for autonomous, interactive plan execution.
 *
 * `brain:execute-plan <plan.md>` turns the resident Pi session INTO the brain (the design in
 * brain-execute-plan-prompt.md): it loads that brain-prompt as the session's operating
 * instructions, fills <PLAN_FILE> (the arg) + <AVAILABLE_AGENTS> (the .claude/agents roster), and
 * injects the plan's CONTENT as a fresh turn. From there the brain — the Pi LLM you watch in the
 * TUI — reads the plan, loops the phases ON ITS OWN, decides the agents per the plan's contract
 * (explicit list / brain chooses / else BLOCK and ask you), reasons between phases, and can
 * BACKTRACK (re-run an earlier phase). YOU interact in the TUI: answer an escalation, or say stop.
 *
 * It exposes two tools (mirroring the SDK brain, sdk-leader.ts):
 *   - run_phase({phase, agents}): the brain picks the phase + the agents and calls this. It
 *     fire-and-returns a per-phase PROXY that spawns `claude -p "/run-phase plan=<file> phase=<N>
 *     agents=<list>"` (the worker team), owns that phase's Unix-socket back-channel, and RELAYS —
 *     worker ask_brain (socket) → UP to the brain via pi.sendMessage(followUp, triggerTurn) → the
 *     brain's answer → back DOWN the socket. Fire-and-return keeps the brain free to handle
 *     escalations while a phase runs (the parallelism seam — but phases stay SERIALIZED by the
 *     single-running guard in brain-core.ts: parallel phases must not be enabled until each worker
 *     runs in its own git worktree, since workers share one git index and concurrent commits clash).
 *   - answer_worker({ask_id, answer, stop}): the brain settles a BLOCKED worker — the answer travels
 *     back down the socket and the worker resumes, or (stop=true) the phase ends.
 *
 * The pure logic (plan-prompt fill, roster, run_phase validation, relayUp, the per-phase proxy, the
 * drains) lives in the sibling modules (brain-prompt.ts + brain-core.ts) and is unit-tested without
 * a live Pi; THIS file is the thin-but-deep wiring that needs the Pi runtime. Pi API usage matches
 * the kit extensions exactly (registerCommand / registerTool / registerFlag / on / sendMessage /
 * appendEntry; ctx.ui.notify / setStatus).
 *
 * ── On the per-phase proxy and the `brain-proxy-model` setting ───────────────────────────────────
 * The proxy is IN-PROCESS: it runs the shared runPhase() on the brain's own event loop. That is
 * deliberate. A Pi sub-agent (the pi-subagents `subagent` tool) is a SEPARATE `pi` OS process whose
 * only talk-back to the parent is the cross-process pi-intercom broker (LLM-level ask/reply, a
 * 10-minute timeout, idle-gating) — it canNOT transparently carry the worker's raw ask_brain socket
 * protocol to the brain. Pushing the socket into a separate proxy process would therefore replace
 * the PROVEN in-process relayUp (where the socket handler awaits a Promise the brain's answer_worker
 * settles in the same loop) with a fragile cross-process bridge. So the proxy stays in-process here.
 * `brain-proxy-model` (default a cheap Haiku tier) records the model a future CROSS-PROCESS proxy
 * sub-agent WOULD use; the in-process proxy consumes no model (it is free). See the rework report.
 */

import type { ExtensionAPI, ExtensionContext } from "@mariozechner/pi-coding-agent";
import { Type } from "@sinclair/typebox";
import * as os from "node:os";
import * as path from "node:path";
import * as fs from "node:fs";
import { type AskEnvelope } from "./envelope.ts";
import { type ProxyEvents } from "./claude-proxy.ts";
import { buildSystemPrompt, loadAvailableAgents, planSlug } from "./brain-prompt.ts";
import {
	type BrainState,
	LOG_CHANNEL,
	makeRelayUp,
	handleAnswerWorker,
	handleRunPhase,
	startPhaseInBackground,
	abortRunningPhase,
} from "./brain-core.ts";
import { readProgress, buildPriorProgressBlock, archiveProgress, pruneRunArtifacts, planContentHash } from "./progress.ts";

const STATUS_KEY = "meta-orch";

/** The cheap model the per-phase proxy layer is configured to use (see the module note above). */
const DEFAULT_PROXY_MODEL = "claude-haiku-4-5";

/** Per-run working dirs under ~/.pi/meta-orch/<slug>/ — sockets, logs, configs. Same namespace as
 *  the SDK brain (phase-id helpers are reused as-is), so both brains share one layout. */
function runRoot(slug: string): string {
	const base = process.env.META_ORCH_DIR || path.join(os.homedir(), ".pi", "meta-orch");
	return path.join(base, slug);
}

export default function (pi: ExtensionAPI) {
	// The cheap model the per-phase proxy layer uses (reserved for the future cross-process proxy
	// sub-agent — the in-process proxy consumes no model). Configurable so it can be repointed
	// without a code change once Pi sub-agent proxies land.
	pi.registerFlag("brain-proxy-model", {
		description: "cheap model for the per-phase proxy sub-agent layer (e.g. claude-haiku-4-5)",
		type: "string",
		default: DEFAULT_PROXY_MODEL,
	});
	// Ignore (and archive) any saved progress for this plan-slug and start from the top. Without it,
	// brain:execute-plan auto-RESUMES from the durable progress ledger when one exists.
	pi.registerFlag("fresh", {
		description: "ignore saved progress for this plan and start from the top (archives the prior ledger)",
		type: "boolean",
		default: false,
	});

	let state: BrainState | null = null;
	let currentCtx: ExtensionContext | null = null;

	function setStatus(): void {
		if (!currentCtx?.hasUI) return;
		try {
			if (!state) currentCtx.ui.setStatus(STATUS_KEY, undefined);
			else currentCtx.ui.setStatus(STATUS_KEY, `${state.planSlug}${state.running ? " (phase running)" : " (idle)"} seq=${state.runSeq}`);
		} catch { /* non-fatal */ }
	}

	const log = (data: Record<string, unknown>): void => {
		try { pi.appendEntry(LOG_CHANNEL, data); } catch { /* best-effort */ }
	};

	const proxyEvents: ProxyEvents = {
		onProgress: (phaseId, line) => {
			// Raw progress goes to the on-disk <id>.log; keep the TUI quiet except for a terse
			// status. Only curated escalations (relayUp) bubble into the conversation.
			if (currentCtx?.hasUI) {
				try { currentCtx.ui.setStatus(STATUS_KEY, `${state?.planSlug ?? ""} · ${phaseId} · ${line.slice(0, 48)}`); } catch { /* ignore */ }
			}
		},
		onNotify: (phaseId, message, level) => {
			if (currentCtx?.hasUI) {
				try { currentCtx.ui.notify(`[${phaseId}] ${message}`, level); } catch { /* ignore */ }
			}
			log({ event: "notify", phaseId, level, message });
		},
		onLog: (_channel, data) => log(data),
	};

	/**
	 * Paint one worker escalation in the brain TUI as a follow-up that fires a fresh turn, so the
	 * brain-LLM reasons about it with WHOLE-PLAN context and answers via answer_worker. This is the
	 * exact pi-intercom surface technique (pi.sendMessage followUp + triggerTurn) — brain-core builds
	 * the text + records the pending ask; here we just deliver it. (relayUp returns the promise the
	 * worker stays blocked on; answer_worker settles it.)
	 */
	const sendEscalation = (ask: AskEnvelope, escalationText: string): void => {
		const header = ask.severity === "blocked" ? "🛑 worker BLOCKED" : ask.severity === "decision" ? "🤔 worker needs a DECISION" : "📣 worker progress";
		// Wrap pi.sendMessage like log() above: this runs inside the per-phase proxy's async flow
		// (relayUp / finishPhase), so a throw here would surface as an UNHANDLED rejection — and Pi
		// has no unhandledRejection handler, so it could exit mid-run. Swallow + log instead.
		try {
			pi.sendMessage(
				{
					customType: "meta-orch-escalation",
					content: `${header} — phase ${ask.phaseId}\n\n${escalationText}`,
					display: true,
					details: { ask_id: ask.id, phaseId: ask.phaseId, severity: ask.severity },
				},
				{ deliverAs: "followUp", triggerTurn: true },
			);
		} catch (err) {
			log({ event: "escalation_up_error", phaseId: ask.phaseId, askId: ask.id, error: err instanceof Error ? err.message : String(err) });
			return;
		}
		log({ event: "escalation_up", phaseId: ask.phaseId, askId: ask.id, severity: ask.severity });
	};

	/** Deliver a phase-completion summary (or a steer) as a follow-up turn so the brain reasons and
	 *  continues / backtracks / stops. brain-core's per-phase proxy calls this when a phase ends. */
	const pushFollowUp = (text: string): void => {
		// finishPhase (brain-core.ts) calls this from the phase promise's terminal .then/.catch, so a
		// throw here would become an UNHANDLED rejection that could take Pi down (no unhandledRejection
		// handler). Swallow it the same way log() does — the run must outlive a failed TUI delivery.
		try {
			pi.sendMessage(
				{ customType: "meta-orch", content: text, display: true },
				{ deliverAs: "followUp", triggerTurn: true },
			);
		} catch (err) {
			log({ event: "push_follow_up_error", error: err instanceof Error ? err.message : String(err) });
		}
	};

	// ── brain:execute-plan <plan.md> — become the brain and run the plan autonomously ────────────
	pi.registerCommand("brain:execute-plan", {
		description: "Become the brain and execute a markdown phase-plan autonomously: brain:execute-plan <plan.md>",
		handler: async (args, ctx) => {
			currentCtx = ctx;
			const planArg = (args || "").trim();
			if (!planArg) {
				ctx.ui.notify("Usage: brain:execute-plan <path-to-plan.md>", "error");
				return;
			}
			const planPath = path.isAbsolute(planArg) ? planArg : path.resolve(ctx.cwd || process.cwd(), planArg);

			// Fail loud if the plan file is missing — the plan IS the work; never half-run on a guess.
			let planContent: string;
			try {
				planContent = fs.readFileSync(planPath, "utf-8");
			} catch (err) {
				const code = err && typeof err === "object" && "code" in err ? (err as NodeJS.ErrnoException).code : undefined;
				ctx.ui.notify(code === "ENOENT" ? `meta-orch: plan file not found: ${planPath}` : `meta-orch: cannot read plan: ${err instanceof Error ? err.message : String(err)}`, "error");
				return;
			}

			// Build the roster + the brain-prompt (fail loud if the prompt file or the roster dir is
			// missing — the brain has no identity / no agents to choose without them).
			let availableAgents: string[];
			let systemPrompt: string;
			try {
				availableAgents = loadAvailableAgents();
				systemPrompt = buildSystemPrompt(planPath, availableAgents);
			} catch (err) {
				ctx.ui.notify(err instanceof Error ? err.message : String(err), "error");
				return;
			}

			const slug = planSlug(planPath);
			const proxyModel = (pi.getFlag("brain-proxy-model") as string) || DEFAULT_PROXY_MODEL;
			const root = runRoot(slug);
			const dirs = {
				logsDir: path.join(root, "logs"),
				socketsDir: path.join(root, "sockets"),
				configsDir: path.join(root, "configs"),
			};
			fs.mkdirSync(dirs.logsDir, { recursive: true });
			fs.mkdirSync(dirs.socketsDir, { recursive: true });
			fs.mkdirSync(dirs.configsDir, { recursive: true });

			// RESUME-FROM-PROGRESS: read the durable ledger for THIS plan-slug. `--fresh` archives it and
			// starts from the top; otherwise a non-empty ledger becomes a PRIOR PROGRESS block the brain
			// sees on its first turn, so it skips already-passed phases and resumes from the first failure.
			const planHash = planContentHash(planContent);
			const fresh = pi.getFlag("fresh") === true;
			if (fresh) {
				archiveProgress(root);
				// Prune the prior run's accumulated logs/configs/stale-sockets — `--fresh` ignores that
				// run, so its per-phase artifacts are dead weight that would otherwise grow unbounded.
				const pruned = pruneRunArtifacts(dirs);
				log({ event: "fresh_start", slug, pruned });
			}
			const priorProgress = fresh ? null : buildPriorProgressBlock(readProgress(root), planHash);

			state = {
				planPath,
				planSlug: slug,
				availableAgents,
				dirs,
				runDir: root,
				planHash,
				proxyModel,
				pendingAsks: new Map(),
				running: false,
				runningPhase: null,
				runSeq: 0,
			};
			setStatus();
			log({ event: "brain_start", slug, planPath, agents: availableAgents.length, proxyModel, resuming: priorProgress !== null });

			// Hand the brain its operating instructions (the filled brain-prompt) WITHOUT a turn, then
			// deliver the plan content + the kick-off and trigger the first turn — so the resident Pi
			// session begins its autonomous loop (pick the phase, decide agents, call run_phase).
			pi.sendMessage(
				{ customType: "meta-orch-brain-prompt", content: systemPrompt, display: false },
				{ deliverAs: "followUp", triggerTurn: false },
			);
			const kickoff: string[] = [
				`🧠 **You are now the brain** — plan ${planPath}`,
				`${availableAgents.length} agents available; you watch me here in the TUI. A worker's raw transcript is on disk at \`${dirs.logsDir}/<id>.log\` if you need to debug.`,
				"",
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
					"Read the ENTIRE plan, then RESUME per the PRIOR PROGRESS block above: do NOT re-run already-passed phases — resume from the first not-yet-passed phase (announce your resume decision first). Run autonomously; I (the human) will answer escalations or say stop in this TUI.",
				);
			} else {
				kickoff.push(
					"",
					"Read the ENTIRE plan, then begin your loop: pick the phase (Phase 0 first), decide the agents per the phase's contract, and call run_phase. Run autonomously; I (the human) will answer escalations or say stop in this TUI.",
				);
			}
			pi.sendMessage(
				{ customType: "meta-orch", content: kickoff.join("\n"), display: true },
				{ deliverAs: "followUp", triggerTurn: true },
			);
		},
	});

	// ── run_phase — the brain picks the phase + agents; fire a per-phase proxy and return ────────
	pi.registerTool({
		name: "run_phase",
		label: "Run Phase",
		description:
			"Run ONE phase of the plan with an EXPLICIT agent list YOU choose. Pass the phase number " +
			"(Phase 0 is valid) and the agents for that phase — from the phase's own list, or your pick " +
			"when the phase says 'brain, choose the agents'. The phase runs in the background (spawns " +
			"`claude -p \"/run-phase plan=<file> phase=<N> agents=<list>\"`); its completion arrives as a " +
			"follow-up message. You pick the phase, so you may BACKTRACK — re-run an earlier phase when a " +
			"later failure's real cause is upstream. Do not call run_phase again until the running phase's " +
			"completion message has arrived.",
		parameters: Type.Object({
			phase: Type.String({ description: 'The phase number to run, e.g. "0" or "3".' }),
			agents: Type.Array(Type.String(), { minItems: 1, description: "The exact agent names for this phase (from the plan, or your choice)." }),
		}),
		async execute(_id, params) {
			if (!state) {
				return { content: [{ type: "text" as const, text: "No plan loaded — run `brain:execute-plan <plan.md>` first." }] };
			}
			const result = handleRunPhase(
				state,
				{ phase: params.phase, agents: params.agents },
				{ relayUp: makeRelayUp(state, sendEscalation), events: proxyEvents, pushFollowUp, log, startPhase: startPhaseInBackground },
			);
			setStatus();
			return { content: [{ type: "text" as const, text: result.text }], details: { started: result.started, phase: params.phase } };
		},
	});

	// ── answer_worker — the brain settles a BLOCKED worker ask ───────────────────────────────────
	pi.registerTool({
		name: "answer_worker",
		label: "Answer Worker",
		description:
			"Answer a worker escalation that is BLOCKING on the back-channel. Provide the ask_id from the " +
			"escalation message and your guidance. Set stop:true ONLY when the work is fundamentally wrong " +
			"and the phase should end now; otherwise stop:false and the worker resumes on your answer.",
		parameters: Type.Object({
			ask_id: Type.String({ description: "The ask_id from the escalation message." }),
			answer: Type.String({ description: "Guidance the worker resumes on (or the reason for stopping)." }),
			stop: Type.Optional(Type.Boolean({ description: "End the phase now. Default false." })),
		}),
		async execute(_id, params) {
			if (!state) {
				return { content: [{ type: "text" as const, text: "No plan loaded — there is no worker to answer." }] };
			}
			const result = handleAnswerWorker(state, { ask_id: params.ask_id, answer: params.answer, stop: params.stop }, log);
			return { content: [{ type: "text" as const, text: result.text }], details: { ask_id: params.ask_id, stop: result.stop, matched: result.matched } };
		},
	});

	// ── brain:execute-plan:status — where the run is (optional, nice-to-have) ─────────────────────
	pi.registerCommand("brain:execute-plan:status", {
		description: "Show the brain's current plan, phase-run sequence, and any pending worker asks",
		handler: async (_args, ctx) => {
			currentCtx = ctx;
			if (!state) { ctx.ui.notify("No plan loaded — run `brain:execute-plan <plan.md>` first", "info"); return; }
			const lines = [
				`**${state.planSlug}** — ${state.running ? "a phase is running" : "idle"}; ${state.runSeq} phase-run(s) so far.`,
				`Plan: ${state.planPath}`,
				`Proxy model: ${state.proxyModel} (reserved for the future cross-process proxy; the in-process proxy uses no model).`,
				state.pendingAsks.size > 0 ? `${state.pendingAsks.size} worker ask(s) awaiting your answer_worker call.` : "No worker asks pending.",
			];
			pi.sendMessage({ customType: "meta-orch", content: lines.join("\n"), display: true }, { deliverAs: "followUp", triggerTurn: false });
		},
	});

	// ── lifecycle ────────────────────────────────────────────────────────────────────────────────
	pi.on("session_start", async (_event, ctx) => {
		currentCtx = ctx;
		setStatus();
	});

	pi.on("session_shutdown", async () => {
		// Stop the phase that is RUNNING (if any) before the brain goes away: abortRunningPhase kills
		// the worker AND its whole process group (so the `claude` sub-processes it spawned don't keep
		// running on the user's plan), tears the phase down, AND releases any worker still blocked on an
		// ask so its ask_brain returns STOP instead of hanging. Draining the asks alone (the old
		// behaviour) left the live worker + its children running. Runs for EVERY shutdown reason.
		if (state) await abortRunningPhase(state, "the brain is shutting down");
	});
}
