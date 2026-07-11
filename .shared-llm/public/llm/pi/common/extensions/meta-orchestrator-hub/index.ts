/**
 * meta-orchestrator-hub — the resident Pi BRAIN for the FILE-BASED hub + agent-teams orchestration
 * model.
 *
 * The model this extension implements (the proven, live-tested worker contract):
 *   - The brain runs a plan via ONE of two commands, which differ ONLY in who owns the hub (a Go
 *     HTTP message broker; the hub ping is an accelerator, the RESULTS FILE the worker writes is the
 *     source of truth):
 *       · `meta-server:autorun <session> <plan> [--hub-json …]` — the brain CREATES the hub, runs the
 *         plan, and TEARS the hub down on shutdown. One shot, owns everything.
 *       · `meta-server:run <session> <plan> [--hub-json …]` — the brain JOINS a hub the human already
 *         started (`meta-server:hub start`), runs the plan, and NEVER creates or tears down the hub.
 *         This is the manual, multi-client path.
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
 * NOTE: the command name `meta-server:autorun` is shared with the (now-unloaded) socket extension —
 * this extension and that one MUST NOT be loaded in the same Pi session.
 *
 * The pure logic (prompt fill, roster, run_phase validation, the file transport, the resume ledger)
 * lives in the sibling modules and is unit-tested without a live Pi; THIS file is the thin-but-deep
 * wiring that needs the Pi runtime. It also registers the `/hub` command (registerHub from hub.ts).
 */

import type {
	ExtensionAPI,
	ExtensionContext,
} from "@mariozechner/pi-coding-agent";
import { Type } from "@sinclair/typebox";
import * as os from "node:os";
import * as path from "node:path";
import * as fs from "node:fs";
import registerHub, {
	expandHome,
	ensureHubStarted,
	checkHubUp,
	stopHub,
} from "./hub.ts";
import registerMetaPlan from "./meta-plan.ts";
import { formatCheckResult, validateRunnable } from "./meta-plan-schema.ts";
import type { ProxyEvents } from "./types.ts";
import { buildSystemPrompt, loadAvailableAgents } from "./brain-prompt.ts";
import {
	type BrainState,
	LOG_CHANNEL,
	handleRunPhase,
	startPhaseInBackground,
	abortRunningPhase,
} from "./brain-core.ts";
import { sanitizeIdPart } from "./phase-id.ts";
import {
	readProgress,
	buildPriorProgressBlock,
	archiveProgress,
	planContentHash,
} from "./progress.ts";

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
	// Register the plan-shaping commands (meta-plan:check / meta-plan:convert) — standalone, usable
	// before any run, sharing the canonical meta-plan-format.md spec.
	registerMetaPlan(pi);

	// Ignore (and archive) any saved progress for this session and start from the top. Without it,
	// meta-server:autorun auto-RESUMES from the durable progress ledger when one exists.
	pi.registerFlag("fresh", {
		description:
			"ignore saved progress for this session and start from the top (archives the prior ledger)",
		type: "boolean",
		default: false,
	});

	let state: BrainState | null = null;
	let currentCtx: ExtensionContext | null = null;

	function setStatus(): void {
		if (!currentCtx?.hasUI) return;
		try {
			if (!state) currentCtx.ui.setStatus?.(STATUS_KEY, undefined);
			else
				currentCtx.ui.setStatus?.(
					STATUS_KEY,
					`${state.sessionName}${state.running ? " (phase running)" : " (idle)"}`,
				);
		} catch {
			/* non-fatal */
		}
	}

	const log = (data: Record<string, unknown>): void => {
		try {
			pi.appendEntry?.(LOG_CHANNEL, data);
		} catch {
			/* best-effort */
		}
	};

	const proxyEvents: ProxyEvents = {
		onProgress: (phaseId, line) => {
			if (currentCtx?.hasUI) {
				try {
					currentCtx.ui.setStatus?.(
						STATUS_KEY,
						`${state?.sessionName ?? ""} · ${phaseId} · ${line.slice(0, 48)}`,
					);
				} catch {
					/* ignore */
				}
			}
		},
		onNotify: (phaseId, message, level) => {
			if (currentCtx?.hasUI) {
				try {
					currentCtx.ui.notify(`[${phaseId}] ${message}`, level);
				} catch {
					/* ignore */
				}
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
			log({
				event: "push_follow_up_error",
				error: err instanceof Error ? err.message : String(err),
			});
		}
	};

	/** Split the command's raw arg STRING into the positionals (session_name, plan_location) and the
	 *  optional global flags `--route <route.yaml>` / `--hub-json <path>` / `--max-retries <n>` /
	 *  `--worker-type claude|pi` / `--model <id>`. Pi parses `registerFlag` flags only at LAUNCH, NOT
	 *  inside a slash command typed in the TUI — there, everything after the command name arrives as one
	 *  raw string. So we parse these ourselves; the remaining tokens are the positionals. A legacy
	 *  `--mode` token is accepted for compatibility but synchronized meta execution resolves to
	 *  subagent-style stage workers. */
	function parseArgs(args: string): {
		positionals: string[];
		routeOverride?: string;
		hubJsonOverride?: string;
		maxRetriesOverride?: number;
		/** Set when `--max-retries` was given but was not an integer >= 1 — the caller must fail loud
		 *  with this message rather than silently falling back to the default budget. */
		maxRetriesError?: string;
		workerTypeOverride?: string;
		modelOverride?: string;
		modeOverride?: string;
	} {
		const toks = (args || "").trim().split(/\s+/).filter(Boolean);
		let routeOverride: string | undefined;
		let hubJsonOverride: string | undefined;
		let maxRetriesOverride: number | undefined;
		let maxRetriesError: string | undefined;
		let workerTypeOverride: string | undefined;
		let modelOverride: string | undefined;
		let modeOverride: string | undefined;
		const takesValue: Record<string, (v: string) => void> = {
			"--route": (v) => {
				routeOverride = v;
			},
			"--hub-json": (v) => {
				hubJsonOverride = v;
			},
			"--max-retries": (v) => {
				const n = Number(v);
				if (Number.isInteger(n) && n >= 1) maxRetriesOverride = n;
				else maxRetriesError = `--max-retries must be a whole number >= 1, got "${v}"`;
			},
			"--worker-type": (v) => {
				workerTypeOverride = v;
			},
			"--model": (v) => {
				modelOverride = v;
			},
			"--mode": (v) => {
				modeOverride = v;
			},
		};
		const positionals: string[] = [];
		for (let i = 0; i < toks.length; i++) {
			const setter = takesValue[toks[i]];
			if (setter && i + 1 < toks.length) {
				setter(toks[i + 1]);
				i++;
				continue;
			}
			positionals.push(toks[i]);
		}
		return {
			positionals,
			routeOverride,
			hubJsonOverride,
			maxRetriesOverride,
			maxRetriesError,
			workerTypeOverride,
			modelOverride,
			modeOverride,
		};
	}

	/** The per-phase retry budget: in-command `--max-retries` if valid (integer ≥ 1), else the default 2. */
	const DEFAULT_MAX_RETRIES = 2;
	function resolveMaxRetries(override?: number): number {
		return override && override >= 1 ? override : DEFAULT_MAX_RETRIES;
	}

	/** The default Pi worker model when --worker-type pi is chosen without --model.
	 *  Keep this configurable rather than hard-coding a future model alias as supported. */
	const DEFAULT_PI_MODEL =
		process.env.META_ORCH_DEFAULT_PI_MODEL || "configured-default";
	/** Resolve the run's worker config from the flags. worker-type defaults to "claude" when omitted;
	 *  an EXPLICIT but unrecognized value is a usage fault — fail loud rather than silently falling
	 *  back to "claude" and running the wrong worker without telling the human. `cursor` is the
	 *  meta-cc-client CLI's worker type, not this Pi transport's (hub-transport.ts / brain-core.ts
	 *  only know "claude" | "pi" here) — reject it with a message saying so. For pi, the model defaults
	 *  to DEFAULT_PI_MODEL; for claude the model is unused (it runs the user's Claude). Synchronized
	 *  meta execution does not use Claude team mode, so the worker mode resolves to subagents. */
	function resolveWorkerConfig(
		typeOverride?: string,
		modelOverride?: string,
		modeOverride?: string,
	): {
		workerType: "claude" | "pi";
		workerModel: string;
		workerMode: "subagents";
	} {
		if (
			typeOverride !== undefined &&
			typeOverride !== "claude" &&
			typeOverride !== "pi"
		) {
			if (typeOverride === "cursor") {
				throw new Error(
					`--worker-type cursor is only supported by the meta-cc-client CLI, not this Pi extension — use "claude" or "pi" here.`,
				);
			}
			throw new Error(
				`--worker-type must be "claude" or "pi", got "${typeOverride}"`,
			);
		}
		const workerType: "claude" | "pi" = typeOverride === "pi" ? "pi" : "claude";
		const workerModel =
			workerType === "pi"
				? modelOverride && modelOverride.length > 0
					? modelOverride
					: DEFAULT_PI_MODEL
				: (modelOverride ?? "");
		void modeOverride;
		const workerMode: "subagents" = "subagents";
		return { workerType, workerModel, workerMode };
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

	// ── the shared brain-start body, used by BOTH meta-server:autorun and meta-server:run. The ONLY
	//    difference between the two commands is the hub step (see `ownHub`):
	//      · ownHub=true  (autorun) → CREATE the hub and ALWAYS tear it down on shutdown.
	//      · ownHub=false (run)     → require a human-started hub to already be UP; NEVER tear it down.
	//    Everything else — parse args, read the plan, build the roster, make the session dir, resume
	//    from the ledger, set state, kick off the first turn — is identical, so it lives here once.
	async function startBrain(
		args: string,
		ctx: ExtensionContext,
		ownHub: boolean,
	): Promise<void> {
		currentCtx = ctx;
		const cmd = ownHub ? "meta-server:autorun" : "meta-server:run";
		// Re-entrancy guard: a second start while a run is already active must NOT silently replace
		// `state` out from under the phase that is mid-flight (its transport promise still references
		// the old state object, and abortRunningPhase on shutdown would then race a stale handle).
		// Fail loud and tell the human to wait or abort first — never auto-abort behind their back.
		if (state?.running) {
			ctx.ui.notify(
				`meta-orch-hub: a run is already active (session "${state.sessionName}", phase running) — wait for it to finish, or stop the phase before starting a new one.`,
				"error",
			);
			return;
		}
		const {
			positionals,
			routeOverride,
			hubJsonOverride,
			maxRetriesOverride,
			maxRetriesError,
			workerTypeOverride,
			modelOverride,
			modeOverride,
		} = parseArgs(args);
		if (positionals.length < 2 || !routeOverride) {
			ctx.ui.notify(
				`Usage: ${cmd} <session_name> <plan_location> --route <route.yaml> [--hub-json <path>] [--max-retries <n>] [--worker-type claude|pi] [--model <id>]`,
				"error",
			);
			return;
		}
		if (maxRetriesError) {
			ctx.ui.notify(`meta-orch-hub: ${maxRetriesError}`, "error");
			return;
		}
		const maxRetries = resolveMaxRetries(maxRetriesOverride);
		let workerConfig: { workerType: "claude" | "pi"; workerModel: string; workerMode: "subagents" };
		try {
			workerConfig = resolveWorkerConfig(workerTypeOverride, modelOverride, modeOverride);
		} catch (err) {
			ctx.ui.notify(
				`meta-orch-hub: ${err instanceof Error ? err.message : String(err)}`,
				"error",
			);
			return;
		}
		const { workerType, workerModel, workerMode } = workerConfig;
		{
			const sessionNameRaw = positionals[0];
			const planArg = positionals.slice(1).join(" ");
			const sessionName = sanitizeIdPart(sessionNameRaw);
			const planSrc = path.isAbsolute(planArg)
				? planArg
				: path.resolve(ctx.cwd || process.cwd(), planArg);
			const routeSrc = path.isAbsolute(routeOverride)
				? routeOverride
				: path.resolve(ctx.cwd || process.cwd(), routeOverride);

			// Fail loud if the plan or route file is missing — together they define the run.
			let planContent: string;
			let routeContent: string;
			try {
				planContent = fs.readFileSync(planSrc, "utf-8");
			} catch (err) {
				const code =
					err && typeof err === "object" && "code" in err
						? (err as NodeJS.ErrnoException).code
						: undefined;
				ctx.ui.notify(
					code === "ENOENT"
						? `meta-orch-hub: plan file not found: ${planSrc}`
						: `meta-orch-hub: cannot read plan: ${err instanceof Error ? err.message : String(err)}`,
					"error",
				);
				return;
			}
			try {
				routeContent = fs.readFileSync(routeSrc, "utf-8");
			} catch (err) {
				const code =
					err && typeof err === "object" && "code" in err
						? (err as NodeJS.ErrnoException).code
						: undefined;
				ctx.ui.notify(
					code === "ENOENT"
						? `meta-orch-hub: route profile not found: ${routeSrc}`
						: `meta-orch-hub: cannot read route profile: ${err instanceof Error ? err.message : String(err)}`,
					"error",
				);
				return;
			}
			const runnableCheck = validateRunnable(planContent, routeContent);
			if (!runnableCheck.ok) {
				ctx.ui.notify(
					`meta-orch-hub: runnable input check failed; run meta-plan:check <plan.md> <route.yaml> before starting.\n${formatCheckResult("RUNNABLE_CHECK", runnableCheck)}`,
					"error",
				);
				return;
			}

			// Build the roster + the brain-prompt (fail loud if the prompt file or the roster dir is missing).
			let availableAgents: string[];
			let systemPrompt: string;

			// Create the session dir, copy the plan to plan.md, mkdir phases/ + logs/. Fail loud on any IO
			// error — the file contract is the whole transport, so we must not proceed on a half-made dir.
			const sessionDir = path.join(fileBaseDir(), sessionName);
			const planPath = path.join(sessionDir, "plan.md");
			const routePath = path.join(sessionDir, "route.yaml");
			const logsDir = path.join(sessionDir, "logs");
			try {
				fs.mkdirSync(path.join(sessionDir, "phases"), { recursive: true });
				fs.mkdirSync(logsDir, { recursive: true });
				fs.writeFileSync(planPath, planContent, "utf-8");
				fs.writeFileSync(routePath, routeContent, "utf-8");
				availableAgents = loadAvailableAgents();
				systemPrompt = buildSystemPrompt(planPath, availableAgents);
			} catch (err) {
				ctx.ui.notify(
					err instanceof Error ? err.message : String(err),
					"error",
				);
				return;
			}

			const hubJsonPath = resolveHubJsonPath(hubJsonOverride);

			// The one place the two commands diverge. autorun OWNS the hub: start it (fail loud if it
			// can't), and session_shutdown ALWAYS tears it down (hubStartedByUs = true). run JOINS a
			// human-started hub: it must already be UP (fail loud if not), and we NEVER tear it down
			// (hubStartedByUs = false). Either way a hub we can't reach is a hard fault — stop now.
			let hubStartedByUs: boolean;
			if (ownHub) {
				const hubResult = await ensureHubStarted(hubJsonPath);
				if (!hubResult.ok) {
					ctx.ui.notify(`meta-orch-hub: ${hubResult.error}`, "error");
					return;
				}
				hubStartedByUs = true;
				try {
					ctx.ui.notify(
						`hub started at ${hubResult.url} (pid ${hubResult.pid}) — the brain owns it and will tear it down on exit`,
						"info",
					);
				} catch {
					/* hasUI may be false */
				}
			} else {
				const hubResult = await checkHubUp(hubJsonPath);
				if (!hubResult.ok) {
					ctx.ui.notify(`meta-orch-hub: ${hubResult.error}`, "error");
					return;
				}
				hubStartedByUs = false;
				try {
					ctx.ui.notify(
						`joined hub at ${hubResult.url} — you started it, so the brain will NOT tear it down`,
						"info",
					);
				} catch {
					/* hasUI may be false */
				}
			}

			// RESUME-FROM-PROGRESS: read the durable ledger for THIS session dir. `--fresh` archives it and
			// starts from the top; otherwise a non-empty ledger becomes a PRIOR PROGRESS block the brain
			// sees on its first turn, so it resumes from the first not-yet-passed phase.
			const planHash = planContentHash(planContent);
			const fresh = pi.getFlag("fresh") === true;
			if (fresh) {
				archiveProgress(sessionDir);
				log({ event: "fresh_start", sessionName });
			}
			const priorProgress = fresh
				? null
				: buildPriorProgressBlock(readProgress(sessionDir), planHash);

			state = {
				sessionName,
				sessionDir,
				planPath,
				routePath,
				availableAgents,
				dirs: { logsDir },
				runDir: sessionDir,
				planHash,
				hubJsonPath,
				hubStartedByUs,
				running: false,
				runningPhase: null,
				iterations: {},
				maxRetries,
				workerType,
				workerModel,
				workerMode,
			};
			setStatus();
			log({
				event: "brain_start",
				sessionName,
				sessionDir,
				planPath,
				routePath,
				agents: availableAgents.length,
				hubJsonPath,
				resuming: priorProgress !== null,
			});

			// Hand the brain its operating instructions (the filled brain-prompt) WITHOUT a turn, then
			// deliver the plan content + the kick-off and trigger the first turn.
			pi.sendMessage(
				{
					customType: "meta-orch-hub-brain-prompt",
					content: systemPrompt,
					display: false,
				},
				{ deliverAs: "followUp", triggerTurn: false },
			);
			const kickoff: string[] = [
				`🧠 **You are now the brain (file-based hub model)** — session ${sessionName}`,
				`Plan copied to ${planPath}; route profile copied to ${routePath}; session dir ${sessionDir}.`,
				`${availableAgents.length} agents available; the hub is up at ${hubJsonPath} (${ownHub ? "the brain created it and will tear it down on exit" : "you started it; the brain will not tear it down"}).`,
				`You run each phase by writing its instructions and calling the \`run_phase\` tool. You NEVER run a command or touch a file yourself.`,
				`Worker for this run: ${workerType === "pi" ? `a Pi worker on ${workerModel}` : `a Claude phase worker using ${workerMode}`}. Phase/stage agents come from the external route profile, not the plan body.`,
				`Retry budget: each phase may be attempted at most ${maxRetries} time${maxRetries === 1 ? "" : "s"}. When a phase uses up its budget and still hasn't passed, STOP and ask me — don't keep retrying. I can raise it live with \`meta-server:retries <n>\`.`,
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
				{
					customType: "meta-orch-hub",
					content: kickoff.join("\n"),
					display: true,
				},
				{ deliverAs: "followUp", triggerTurn: true },
			);
		}
	}

	// ── meta-server:autorun <session> <plan> — brain CREATES the hub, runs the plan, tears it down ──
	pi.registerCommand("meta-server:autorun", {
		description:
			"Become the brain: create the hub, run a canonical meta plan plus route profile, tear the hub down on exit: meta-server:autorun <session_name> <plan_location> --route <route.yaml> [--hub-json <path>]",
		handler: async (args, ctx) => {
			await startBrain(args, ctx, true);
		},
	});

	// ── meta-server:run <session> <plan> — brain JOINS a human-started hub; never touches its life ──
	pi.registerCommand("meta-server:run", {
		description:
			"Become the brain against an ALREADY-RUNNING hub; run a canonical meta plan plus route profile: meta-server:run <session_name> <plan_location> --route <route.yaml> [--hub-json <path>]",
		handler: async (args, ctx) => {
			await startBrain(args, ctx, false);
		},
	});

	// ── run_phase — the brain picks the phase + writes instructions; fire the file transport ────────
	pi.registerTool({
		name: "run_phase",
		label: "Run Phase",
		description:
			"Run ONE phase. You pass the phase number (Phase 0 is valid) and the instructions text you " +
			"wrote for this attempt (the goal + the steps + a done-check). The system writes your " +
			"instructions to a file, spawns the run's phase worker, and that worker resolves the phase lead " +
			"and stage agents from the external route profile. It then watches for the worker's results file " +
			"and returns the PHASE_RESULT verdict (passed | partial | blocked | failed). You never run a " +
			"command or touch a file yourself — this tool is how a phase runs. Do NOT add a reviewer/evaluator. " +
			"The phase runs in the background; its completion arrives as a follow-up message — do not call " +
			"run_phase again until it does. You may BACKTRACK: re-run an earlier phase (a new iteration) when " +
			"a later failure's real cause is upstream.",
		parameters: Type.Object({
			phase: Type.String({ description: 'The phase to run, e.g. "0" or "3".' }),
			instructions: Type.String({
				description:
					"The work this attempt should do: goal, steps, and a clear done-check. The worker acts on this text.",
			}),
		}),
		async execute(
			_id: string,
			params: { phase: string; instructions: string },
		) {
			if (!state) {
				return {
					content: [
						{
							type: "text" as const,
							text: "No plan loaded — run `meta-server:autorun` or `meta-server:run` <session_name> <plan_location> first.",
						},
					],
				};
			}
			const result = handleRunPhase(
				state,
				{ phase: params.phase, instructions: params.instructions },
				{
					events: proxyEvents,
					pushFollowUp,
					log,
					startPhase: startPhaseInBackground,
				},
			);
			setStatus();
			return {
				content: [{ type: "text" as const, text: result.text }],
				details: { started: result.started, phase: params.phase },
			};
		},
	});

	// ── meta-server:status — where the run is (shared by autorun + run) ─────────────────────────────
	pi.registerCommand("meta-server:status", {
		description:
			"Show the brain's current session, running phase, and hub path",
		handler: async (_args, ctx) => {
			currentCtx = ctx;
			if (!state) {
				ctx.ui.notify(
					"No plan loaded — run `meta-server:autorun` or `meta-server:run` <session_name> <plan_location> first",
					"info",
				);
				return;
			}
			const iters =
				Object.entries(state.iterations)
					.map(([p, n]) => `p${p}×${n}`)
					.join(", ") || "(none yet)";
			const workerDesc =
				state.workerType === "pi"
					? `pi (${state.workerModel})`
					: `claude (${state.workerMode})`;
			const lines = [
				`**${state.sessionName}** — ${state.running ? "a phase is running" : "idle"}; iterations: ${iters}.`,
				`Worker: ${workerDesc}.`,
				`Retry budget: ${state.maxRetries} attempt${state.maxRetries === 1 ? "" : "s"} per phase.`,
				`Session dir: ${state.sessionDir}`,
				`Plan: ${state.planPath}`,
				`Hub: ${state.hubJsonPath}`,
			];
			pi.sendMessage(
				{
					customType: "meta-orch-hub",
					content: lines.join("\n"),
					display: true,
				},
				{ deliverAs: "followUp", triggerTurn: false },
			);
		},
	});

	// ── meta-server:retries <n> — raise/lower the per-phase retry budget LIVE (human-in-the-loop) ────
	pi.registerCommand("meta-server:retries", {
		description:
			"Set the per-phase retry budget live: meta-server:retries <n> (n ≥ 1). Use it after a phase maxed out to let the brain try again, then tell it to continue.",
		handler: async (args, ctx) => {
			currentCtx = ctx;
			if (!state) {
				ctx.ui.notify(
					"No plan loaded — start a run first with `meta-server:autorun` or `meta-server:run`",
					"info",
				);
				return;
			}
			const n = Number((args || "").trim());
			if (!Number.isInteger(n) || n < 1) {
				ctx.ui.notify(
					`Usage: meta-server:retries <n> (a whole number ≥ 1) — current budget is ${state.maxRetries}`,
					"error",
				);
				return;
			}
			const prev = state.maxRetries;
			state.maxRetries = n;
			pi.sendMessage(
				{
					customType: "meta-orch-hub",
					content: `Retry budget changed from ${prev} to ${n} attempts per phase. If a phase was waiting on its budget, you may call run_phase for it again now.`,
					display: true,
				},
				{ deliverAs: "followUp", triggerTurn: false },
			);
		},
	});

	// ── lifecycle ────────────────────────────────────────────────────────────────────────────────
	pi.on("session_start", async (_event: unknown, ctx?: ExtensionContext) => {
		if (!ctx) return;
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
			log({
				event: "hub_stopped_on_shutdown",
				ok: r.ok,
				pid: r.pid,
				error: r.error,
			});
		}
	});
}
