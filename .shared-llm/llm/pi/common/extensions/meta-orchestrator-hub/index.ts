/**
 * meta-orchestrator-hub — the resident Pi BRAIN for the FILE-BASED hub + agent-teams orchestration
 * model.
 *
 * The model this extension implements (the proven, live-tested worker contract):
 *   - The human starts a NAMED hub: `/hub start --json ~/.meta-orch/<name>.json` (a Go HTTP message
 *     broker). The brain only CONNECTS — it never starts the hub. (The hub ping is an accelerator;
 *     the RESULTS FILE the worker writes is the source of truth.)
 *   - The human starts the brain: `brain:execute-plan <session_name> <plan_location> [--hub-json …]`.
 *   - The brain copies the plan to a session dir, then per phase does exactly TWO things: decide the
 *     phase + WRITE its instructions, and call ONE tool — `run_phase({phase, instructions, team})`.
 *   - `run_phase` (TS, the whole transport) writes `phases/<phase>/iteration/<n>/instructions.md`,
 *     spawns an interactive Claude TUI worker (`just worker-up`) that runs `/meta-auto-run`, WATCHES
 *     for the worker's `phases/<phase>/iteration/<n>/results.md` (first line `PHASE_RESULT: <verdict>`),
 *     reads it, judges it via judgePhaseStatus, tears the worker down, and pushes the verdict back to
 *     the brain. The FILE is the completion signal — not any hub message.
 *
 * The brain NEVER runs shell / tmux / just / curl / git / files itself. It only decides the phase,
 * writes the instructions, and calls `run_phase`. A not-passed phase is the brain's cue to rerun (a
 * new iteration) or backtrack.
 *
 * NOTE: the command name `brain:execute-plan` is shared with the (now-unloaded) socket extension —
 * this extension and that one MUST NOT be loaded in the same Pi session.
 *
 * The pure logic (prompt fill, roster, run_phase validation, the file transport, the resume ledger)
 * lives in the sibling modules and is unit-tested without a live Pi; THIS file is the thin-but-deep
 * wiring that needs the Pi runtime. It also registers the `/hub` command (registerHub from hub.ts).
 */

import type { ExtensionAPI, ExtensionContext } from "@mariozechner/pi-coding-agent";
import { Type } from "@sinclair/typebox";
import * as os from "node:os";
import * as path from "node:path";
import * as fs from "node:fs";
import registerHub, { expandHome, ensureHubStarted, stopHub } from "./hub.ts";
import { type ProxyEvents } from "./types.ts";
import { buildSystemPrompt, loadAvailableAgents } from "./brain-prompt.ts";
import {
	type BrainState,
	LOG_CHANNEL,
	handleRunPhase,
	startPhaseInBackground,
	abortRunningPhase,
} from "./brain-core.ts";
import { sanitizeIdPart } from "./phase-id.ts";
import { readProgress, buildPriorProgressBlock, archiveProgress, planContentHash } from "./progress.ts";

const STATUS_KEY = "meta-orch-hub";
const DEFAULT_JSON = path.join(os.homedir(), ".meta-orch", "hub.json");

/** The file-contract base dir: `/tmp/meta-orch` by default, overridable via META_ORCH_FILE_DIR. The
 *  worker slash commands write under `<base>/<session_name>/`, so the brain and the worker MUST agree
 *  on this base — the env var (if set) must match what the worker uses. */
function fileBaseDir(): string {
	return process.env.META_ORCH_FILE_DIR || path.join(os.tmpdir(), "meta-orch");
}

export default function (pi: ExtensionAPI) {
	// Register the /hub command (start / status / stop) in this same session. registerHub also
	// registers the `hub-json` flag, which we read below to point the brain at its named hub.
	registerHub(pi);

	// Ignore (and archive) any saved progress for this session and start from the top. Without it,
	// brain:execute-plan auto-RESUMES from the durable progress ledger when one exists.
	pi.registerFlag("fresh", {
		description: "ignore saved progress for this session and start from the top (archives the prior ledger)",
		type: "boolean",
		default: false,
	});

	let state: BrainState | null = null;
	let currentCtx: ExtensionContext | null = null;

	function setStatus(): void {
		if (!currentCtx?.hasUI) return;
		try {
			if (!state) currentCtx.ui.setStatus(STATUS_KEY, undefined);
			else currentCtx.ui.setStatus(STATUS_KEY, `${state.sessionName}${state.running ? " (phase running)" : " (idle)"}`);
		} catch { /* non-fatal */ }
	}

	const log = (data: Record<string, unknown>): void => {
		try { pi.appendEntry(LOG_CHANNEL, data); } catch { /* best-effort */ }
	};

	const proxyEvents: ProxyEvents = {
		onProgress: (phaseId, line) => {
			if (currentCtx?.hasUI) {
				try { currentCtx.ui.setStatus(STATUS_KEY, `${state?.sessionName ?? ""} · ${phaseId} · ${line.slice(0, 48)}`); } catch { /* ignore */ }
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

	/** Deliver a phase-completion summary (or a steer) as a follow-up turn so the brain reasons and
	 *  continues / backtracks / stops. brain-core's per-phase transport calls this when a phase ends. */
	const pushFollowUp = (text: string): void => {
		// finishPhase (brain-core.ts) calls this from the phase promise's terminal .then/.catch, so a
		// throw here would become an UNHANDLED rejection that could take Pi down. Swallow + log.
		try {
			pi.sendMessage(
				{ customType: "meta-orch-hub", content: text, display: true },
				{ deliverAs: "followUp", triggerTurn: true },
			);
		} catch (err) {
			log({ event: "push_follow_up_error", error: err instanceof Error ? err.message : String(err) });
		}
	};

	/** Split the command's raw arg STRING into the positionals (session_name, plan_location) and an
	 *  optional `--hub-json <path>`. Pi parses `registerFlag` flags only at LAUNCH, NOT inside a slash
	 *  command typed in the TUI — there, everything after the command name arrives as one raw string.
	 *  So we parse `--hub-json` ourselves; the remaining tokens are the positionals. */
	function parseArgs(args: string): { positionals: string[]; hubJsonOverride?: string } {
		const toks = (args || "").trim().split(/\s+/).filter(Boolean);
		let hubJsonOverride: string | undefined;
		const positionals: string[] = [];
		for (let i = 0; i < toks.length; i++) {
			if (toks[i] === "--hub-json" && i + 1 < toks.length) {
				hubJsonOverride = toks[i + 1];
				i++;
				continue;
			}
			positionals.push(toks[i]);
		}
		return { positionals, hubJsonOverride };
	}

	/** Resolve the hub discovery JSON path: in-command `--hub-json` > launch `--hub-json` flag >
	 *  env META_ORCH_HUB_JSON > default ~/.meta-orch/hub.json — then expandHome the tilde. */
	function resolveHubJsonPath(override?: string): string {
		const flag = pi.getFlag("hub-json") as string | undefined;
		const raw =
			(override && override.length > 0 ? override : undefined) ??
			(flag && flag.length > 0 ? flag : undefined) ??
			process.env.META_ORCH_HUB_JSON ??
			DEFAULT_JSON;
		return expandHome(raw);
	}

	// ── brain:execute-plan <session_name> <plan_location> — become the brain and run the plan ───────
	pi.registerCommand("brain:execute-plan", {
		description: "Become the file-based hub-model brain and execute a markdown phase-plan: brain:execute-plan <session_name> <plan_location> [--hub-json <path>]",
		handler: async (args, ctx) => {
			currentCtx = ctx;
			const { positionals, hubJsonOverride } = parseArgs(args);
			if (positionals.length < 2) {
				ctx.ui.notify("Usage: brain:execute-plan <session_name> <plan_location> [--hub-json <path>]", "error");
				return;
			}
			const sessionNameRaw = positionals[0];
			const planArg = positionals.slice(1).join(" ");
			const sessionName = sanitizeIdPart(sessionNameRaw);
			const planSrc = path.isAbsolute(planArg) ? planArg : path.resolve(ctx.cwd || process.cwd(), planArg);

			// Fail loud if the plan file is missing — the plan IS the work; never half-run on a guess.
			let planContent: string;
			try {
				planContent = fs.readFileSync(planSrc, "utf-8");
			} catch (err) {
				const code = err && typeof err === "object" && "code" in err ? (err as NodeJS.ErrnoException).code : undefined;
				ctx.ui.notify(code === "ENOENT" ? `meta-orch-hub: plan file not found: ${planSrc}` : `meta-orch-hub: cannot read plan: ${err instanceof Error ? err.message : String(err)}`, "error");
				return;
			}

			// Build the roster + the brain-prompt (fail loud if the prompt file or the roster dir is missing).
			let availableAgents: string[];
			let systemPrompt: string;

			// Create the session dir, copy the plan to plan.md, mkdir phases/ + logs/. Fail loud on any IO
			// error — the file contract is the whole transport, so we must not proceed on a half-made dir.
			const sessionDir = path.join(fileBaseDir(), sessionName);
			const planPath = path.join(sessionDir, "plan.md");
			const logsDir = path.join(sessionDir, "logs");
			try {
				fs.mkdirSync(path.join(sessionDir, "phases"), { recursive: true });
				fs.mkdirSync(logsDir, { recursive: true });
				fs.writeFileSync(planPath, planContent, "utf-8");
				availableAgents = loadAvailableAgents();
				systemPrompt = buildSystemPrompt(planPath, availableAgents);
			} catch (err) {
				ctx.ui.notify(err instanceof Error ? err.message : String(err), "error");
				return;
			}

			const hubJsonPath = resolveHubJsonPath(hubJsonOverride);

			// Auto-start the hub if it isn't already up — the human never runs /hub start. We record
			// whether WE started it so session_shutdown only tears down a hub we created (never one the
			// human already had running). A hub we cannot start is a hard fault — fail loud and stop.
			const hubResult = await ensureHubStarted(hubJsonPath);
			if (!hubResult.ok) {
				ctx.ui.notify(`meta-orch-hub: ${hubResult.error}`, "error");
				return;
			}
			const hubStartedByUs = hubResult.alreadyUp !== true;
			try {
				ctx.ui.notify(hubResult.alreadyUp ? `hub already up at ${hubResult.url}` : `hub started at ${hubResult.url} (pid ${hubResult.pid})`, "info");
			} catch { /* hasUI may be false */ }

			// RESUME-FROM-PROGRESS: read the durable ledger for THIS session dir. `--fresh` archives it and
			// starts from the top; otherwise a non-empty ledger becomes a PRIOR PROGRESS block the brain
			// sees on its first turn, so it resumes from the first not-yet-passed phase.
			const planHash = planContentHash(planContent);
			const fresh = pi.getFlag("fresh") === true;
			if (fresh) {
				archiveProgress(sessionDir);
				log({ event: "fresh_start", sessionName });
			}
			const priorProgress = fresh ? null : buildPriorProgressBlock(readProgress(sessionDir), planHash);

			state = {
				sessionName,
				sessionDir,
				planPath,
				availableAgents,
				dirs: { logsDir },
				runDir: sessionDir,
				planHash,
				hubJsonPath,
				hubStartedByUs,
				running: false,
				runningPhase: null,
				iterations: {},
			};
			setStatus();
			log({ event: "brain_start", sessionName, sessionDir, planPath, agents: availableAgents.length, hubJsonPath, resuming: priorProgress !== null });

			// Hand the brain its operating instructions (the filled brain-prompt) WITHOUT a turn, then
			// deliver the plan content + the kick-off and trigger the first turn.
			pi.sendMessage(
				{ customType: "meta-orch-hub-brain-prompt", content: systemPrompt, display: false },
				{ deliverAs: "followUp", triggerTurn: false },
			);
			const kickoff: string[] = [
				`🧠 **You are now the brain (file-based hub model)** — session ${sessionName}`,
				`Plan copied to ${planPath}; session dir ${sessionDir}.`,
				`${availableAgents.length} agents available; the hub is up at ${hubJsonPath} (the brain manages it for you).`,
				`You run each phase by writing its instructions and calling the \`run_phase\` tool. You NEVER run a command or touch a file yourself.`,
				"",
				`Here is the plan to execute (also at ${planPath}):`,
				"",
				"----- PLAN BEGIN -----",
				planContent,
				"----- PLAN END -----",
			];
			if (priorProgress) {
				kickoff.push("", priorProgress);
				kickoff.push(
					"",
					"Read the ENTIRE plan, then RESUME per the PRIOR PROGRESS block above: do NOT re-run already-passed phases — resume from the first not-yet-passed phase (announce your resume decision first), write its instructions, and call run_phase. I (the human) will answer if you stop and ask.",
				);
			} else {
				kickoff.push(
					"",
					"Read the ENTIRE plan, then begin your loop: pick the phase (Phase 0 first), write its instructions, and call run_phase. I (the human) will answer if you stop and ask.",
				);
			}
			pi.sendMessage(
				{ customType: "meta-orch-hub", content: kickoff.join("\n"), display: true },
				{ deliverAs: "followUp", triggerTurn: true },
			);
		},
	});

	// ── run_phase — the brain picks the phase + writes instructions; fire the file transport ────────
	pi.registerTool({
		name: "run_phase",
		label: "Run Phase",
		description:
			"Run ONE phase. You pass the phase number (Phase 0 is valid), the instructions text you wrote " +
			"for this attempt (the goal + the steps + a done-check), and team (true → a TeamCreate team, " +
			"false → subagents; prefer true). The system writes your instructions to a file, spawns a worker " +
			"that builds the team and does the work, then watches for the worker's results file and returns " +
			"the PHASE_RESULT verdict (passed | partial | blocked | failed). You never run a command or touch " +
			"a file yourself — this tool is how a phase runs. Do NOT add a reviewer/evaluator — the worker " +
			"always runs the adversarial-evaluator gate itself. The phase runs in the background; its " +
			"completion arrives as a follow-up message — do not call run_phase again until it does. You may " +
			"BACKTRACK: re-run an earlier phase (a new iteration) when a later failure's real cause is upstream.",
		parameters: Type.Object({
			phase: Type.String({ description: 'The phase to run, e.g. "0" or "3".' }),
			instructions: Type.String({ description: "The work this attempt should do: goal, steps, and a clear done-check. The worker acts on this text." }),
			team: Type.Optional(Type.Boolean({ description: "true → TeamCreate team (default); false → subagents for a trivial phase.", default: true })),
		}),
		async execute(_id, params) {
			if (!state) {
				return { content: [{ type: "text" as const, text: "No plan loaded — run `brain:execute-plan <session_name> <plan_location>` first." }] };
			}
			const team = params.team ?? true;
			const result = handleRunPhase(
				state,
				{ phase: params.phase, instructions: params.instructions, team },
				{ events: proxyEvents, pushFollowUp, log, startPhase: startPhaseInBackground },
			);
			setStatus();
			return { content: [{ type: "text" as const, text: result.text }], details: { started: result.started, phase: params.phase, team } };
		},
	});

	// ── brain:execute-plan:status — where the run is ───────────────────────────────────────────────
	pi.registerCommand("brain:execute-plan:status", {
		description: "Show the file-based hub-model brain's current session, running phase, and hub path",
		handler: async (_args, ctx) => {
			currentCtx = ctx;
			if (!state) { ctx.ui.notify("No plan loaded — run `brain:execute-plan <session_name> <plan_location>` first", "info"); return; }
			const iters = Object.entries(state.iterations).map(([p, n]) => `p${p}×${n}`).join(", ") || "(none yet)";
			const lines = [
				`**${state.sessionName}** — ${state.running ? "a phase is running" : "idle"}; iterations: ${iters}.`,
				`Session dir: ${state.sessionDir}`,
				`Plan: ${state.planPath}`,
				`Hub: ${state.hubJsonPath}`,
			];
			pi.sendMessage({ customType: "meta-orch-hub", content: lines.join("\n"), display: true }, { deliverAs: "followUp", triggerTurn: false });
		},
	});

	// ── lifecycle ────────────────────────────────────────────────────────────────────────────────
	pi.on("session_start", async (_event, ctx) => {
		currentCtx = ctx;
		setStatus();
	});

	pi.on("session_shutdown", async () => {
		// Tear down the phase that is RUNNING (if any) before the brain goes away: abortRunningPhase
		// calls the transport's worker-down so the tmux worker doesn't keep running after the brain is
		// gone. Runs for EVERY shutdown reason.
		if (state) await abortRunningPhase(state);
		// Stop the hub IF we started it — leave a human-started hub alone. The whole lifecycle
		// (create on launch, destroy on shutdown) lives in the brain, so nothing is run by hand.
		if (state?.hubStartedByUs) {
			const r = stopHub(state.hubJsonPath);
			log({ event: "hub_stopped_on_shutdown", ok: r.ok, pid: r.pid, error: r.error });
		}
	});
}
