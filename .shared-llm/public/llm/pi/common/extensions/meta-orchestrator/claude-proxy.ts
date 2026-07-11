/**
 * claude-proxy — one Pi sub-agent per phase: spawn the claude -p team, surface it, steer it.
 *
 * This is layer 2 of shape 3b: the per-phase proxy. For one phase it:
 *   - spawns `claude -p "/team <team> <task>" --output-format stream-json --mcp-config <gen>`
 *     (adapting the disler subagent-widget spawn from `pi` to `claude -p`, reading CLAUDE's
 *     stream-json shape — the genuinely new bridge),
 *   - tees every raw stdout line to `<id>.log` AND parses it for progress (the on-disk log is
 *     the forensic record; you read it to debug a worker after the fact),
 *   - runs the ask_brain back-channel server (brain = server) whose resolver relays the
 *     worker's question UP to the leader TUI and blocks for the leader's answer, then returns
 *     it down the socket to the still-blocked worker,
 *   - enforces the ONE last-resort guard on a real interval (a generous wall-clock timeout) and
 *     kills the worker only if it blows that budget — otherwise the worker runs to completion,
 *     like a plain `claude -p` (no turn cap, no liveness/heartbeat kill),
 *   - tears everything down at phase end (kill child, close socket, close the log stream).
 *
 * It is deep: the leader (index.ts) calls `runPhase` and gets back an outcome; all the
 * spawn/stream/socket/guardrail machinery is hidden here. The leader supplies the one
 * thing only it can do — `relayUp`, which paints an escalation in the TUI and resolves with
 * the leader's answer.
 */

import { spawn, type ChildProcess } from "node:child_process";
import * as fs from "node:fs";
import { drainLines, parseStreamLine, type ClaudeEvent } from "./stream-json.ts";
import { buildClaudeArgs, buildRunPhaseArgs, buildMcpConfig, writeMcpConfig, buildAskBrainInstruction, ASK_BRAIN_SERVER_NAME } from "./mcp-config.ts";
import { startBackChannel, type BackChannel } from "./back-channel.ts";
import { type AskEnvelope, type AnswerEnvelope, makeAnswer } from "./envelope.ts";
import {
	type GuardrailLimits,
	type Breach,
	createGuardrailState,
	evaluate,
	describeBreach,
} from "./guardrails.ts";
import { logPathFor, socketPathFor, mcpConfigPathFor } from "./phase-id.ts";

/** What the leader must provide: relay a worker ask up to the TUI and resolve with the answer. */
export type RelayUp = (ask: AskEnvelope) => Promise<AnswerEnvelope>;

/** Surfaced to the leader so it can paint live progress / notices in the TUI. */
export interface ProxyEvents {
	onProgress?: (phaseId: string, line: string) => void;
	onNotify?: (phaseId: string, message: string, level: "info" | "warning" | "error") => void;
	onLog?: (channel: string, data: Record<string, unknown>) => void;
}

export interface PhaseDirs {
	logsDir: string;
	socketsDir: string;
	configsDir: string;
}

/**
 * The brain's `/run-phase` worker command — the SDK leader passes THIS instead of team/task. When
 * present, the proxy spawns `claude -p "/run-phase plan=… phase=… agents=…"` (buildRunPhaseArgs);
 * when absent, it falls back to the `/team` path (buildClaudeArgs) the Pi leader still uses. Adding
 * it as an optional field keeps the existing Pi signature compiling untouched.
 */
export interface RunPhaseCommand {
	planFile: string;
	phase: string;
	agents: string[];
}

/**
 * A live handle to a running phase, handed to the leader the moment the worker is spawned (via
 * RunPhaseInput.onStart). The leader stores it so it can ABORT the phase out-of-band — on session
 * shutdown it calls abort() to kill the worker (and its whole process group) instead of leaving the
 * child + its `claude` sub-processes running after the brain is gone. abort() drives the SAME
 * teardown as a leader STOP (kill group → settle "stopped" → close socket + log), and resolves once
 * the phase's runPhase() promise has fully settled (child reaped, socket unlinked, log closed).
 */
export interface PhaseHandle {
	phaseId: string;
	abort(): Promise<void>;
}

export interface RunPhaseInput {
	phaseId: string;
	/** `/team` worker path (Pi leader): the named team + its task. Provide these OR runPhaseCommand. */
	team?: string;
	task?: string;
	/** `/run-phase` worker path (SDK brain): the plan file + phase + explicit agents. */
	runPhaseCommand?: RunPhaseCommand;
	limits: GuardrailLimits;
	maxHops: number;
	dirs: PhaseDirs;
	relayUp: RelayUp;
	events?: ProxyEvents;
	model?: string;
	/** Called once, right after the worker is spawned, with a handle the leader uses to abort the
	 *  phase on shutdown. Optional so existing callers (and the unit tests) need not pass it. */
	onStart?: (handle: PhaseHandle) => void;
}

export interface PhaseOutcome {
	phaseId: string;
	status: "completed" | "failed" | "stopped" | "breached";
	/** The worker's final `result` text (its report back to the leader). */
	report: string;
	/** Set when a guardrail breach ended the phase. */
	breach?: Breach;
	/** True when the leader answered a worker ask with stop=true. */
	stoppedByLeader: boolean;
	logPath: string;
}

/**
 * Send `signal` to the worker's whole PROCESS GROUP, not just the direct child. The worker is a
 * `claude -p` that spawns its own `claude` sub-processes (the agents it runs); signalling only the
 * direct child would orphan those — they'd keep running on the user's plan after the phase is gone.
 * Because the child is spawned `detached: true` it is its own process-group leader, so the negative
 * pid (`-pid`) reaches the leader AND every descendant in one call. Falls back to a direct child
 * kill if the pid is missing (child never spawned) or the group signal fails. Best-effort: a kill
 * race (the group already exited) is swallowed.
 */
function signalProcessGroup(child: ChildProcess, signal: NodeJS.Signals): void {
	const pid = child.pid;
	if (typeof pid === "number") {
		try {
			process.kill(-pid, signal); // negative pid → the whole process group
			return;
		} catch {
			// group gone, or not a group leader — fall through to a direct child kill.
		}
	}
	try { child.kill(signal); } catch { /* already gone */ }
}

/**
 * Kill the worker's process group (SIGTERM now, SIGKILL after a grace period if it lingers). Returns
 * the SIGKILL-fallback timer so the caller can clear it once the child actually exits — no point
 * SIGKILLing a group that already died cleanly. Killing the GROUP (not just the child) is what stops
 * the `claude` sub-processes the worker spawned from being orphaned.
 */
function killChild(child: ChildProcess): NodeJS.Timeout {
	signalProcessGroup(child, "SIGTERM");
	const t = setTimeout(() => {
		signalProcessGroup(child, "SIGKILL");
	}, 5000);
	try { (t as unknown as { unref?: () => void }).unref?.(); } catch { /* ignore */ }
	return t;
}

/**
 * Run one phase end-to-end. Resolves when the worker exits (or is stopped/breached). It
 * reports a worker-level failure in the outcome rather than throwing, but DOES throw if the
 * phase cannot be set up at all (e.g. the back-channel socket is held by a live server),
 * because that is a real configuration fault the leader must see — not paper over.
 */
export async function runPhase(input: RunPhaseInput): Promise<PhaseOutcome> {
	const { phaseId, dirs, events } = input;
	fs.mkdirSync(dirs.logsDir, { recursive: true });

	// Exactly one worker path must be specified — the `/team` path (team+task) or the brain's
	// `/run-phase` path (runPhaseCommand). Fail loud on neither/both rather than spawn a malformed
	// or ambiguous command.
	const hasTeam = typeof input.team === "string" && typeof input.task === "string";
	const hasRunPhase = !!input.runPhaseCommand;
	if (hasTeam === hasRunPhase) {
		throw new Error(
			`runPhase: provide exactly one of {team,task} or runPhaseCommand (got ${hasTeam ? "both" : "neither"})`,
		);
	}

	const logPath = logPathFor(dirs.logsDir, phaseId);
	const socketPath = socketPathFor(dirs.socketsDir, phaseId);
	const mcpConfigPath = mcpConfigPathFor(dirs.configsDir, phaseId);

	// 1. Generate + write the per-phase --mcp-config pointing at THIS phase's socket.
	writeMcpConfig(mcpConfigPath, buildMcpConfig({ socketPath, phaseId, maxHops: input.maxHops }));

	// 2. Open the log for teeing; truncate any prior run's file for this id.
	const logStream = fs.createWriteStream(logPath, { flags: "w" });
	const writeLog = (line: string) => { try { logStream.write(line.endsWith("\n") ? line : line + "\n"); } catch { /* best-effort */ } };
	if (hasRunPhase) {
		const rpc = input.runPhaseCommand!;
		writeLog(`# meta-orchestrator phase ${phaseId} — /run-phase phase=${rpc.phase} agents=${rpc.agents.join(",")}`);
		writeLog(`# plan: ${rpc.planFile}`);
	} else {
		writeLog(`# meta-orchestrator phase ${phaseId} — team=${input.team}`);
		writeLog(`# task: ${input.task}`);
	}

	// 3. Shared per-phase proxy state — referenced by BOTH the back-channel resolver (which
	//    runs whenever the worker calls ask_brain) and the spawn promise below. Lifting these
	//    to the function scope is what lets a leader STOP answer reach into the live child.
	const guard = createGuardrailState(input.limits, Date.now());
	let child: ChildProcess | null = null;
	let stoppedByLeader = false;
	let settle: ((status: PhaseOutcome["status"]) => void) | null = null;
	// A kill was issued (STOP / breach / abort) → we must wait for the child to actually exit before
	// tearing the socket + log down, so teardown order is correct. `killTimer` is the SIGKILL-fallback
	// timer the killer started; we clear it once the child exits so a cleanly-exited group is never
	// SIGKILLed needlessly. `childExited` resolves on the child's close/exit (or stays unresolved if it
	// never spawned, in which case there is nothing to wait for).
	let killIssued = false;
	let killTimer: NodeJS.Timeout | null = null;
	let aborted = false;
	let resolveChildExited: () => void = () => {};
	const childExited = new Promise<void>((res) => { resolveChildExited = res; });
	// Resolves only after the FULL teardown (child reaped, socket unlinked, log closed) — abort()
	// returns it so the leader can await an orderly stop on shutdown.
	let resolveTornDown: () => void = () => {};
	const tornDown = new Promise<void>((res) => { resolveTornDown = res; });
	// Kill the worker's whole process group ONCE (idempotent across STOP / breach / abort racing each
	// other) and remember it so teardown waits for the exit. Records the SIGKILL-fallback timer.
	const killGroupOnce = (): void => {
		if (killIssued) return;
		killIssued = true;
		if (child) killTimer = killChild(child);
	};

	// 4. Back-channel: the brain is the server. The resolver acks a routine heartbeat check-in
	//    cheaply (a worker may still send one per the /run-phase contract; it is no longer required
	//    and never kills the worker — there is no liveness guard), otherwise relays the question UP
	//    to the leader and — if the leader says STOP — kills the worker here.
	const resolveAsk = async (ask: AskEnvelope): Promise<AnswerEnvelope> => {
		events?.onLog?.("meta-orch-log", { event: "ask", phaseId, severity: ask.severity, id: ask.id });
		if (ask.severity === "heartbeat") {
			return makeAnswer(ask.id, "ack — continue", false);
		}
		const answer = await input.relayUp(ask);
		if (answer.stop) {
			stoppedByLeader = true;
			events?.onNotify?.(phaseId, "leader answered STOP — ending phase", "warning");
			writeLog(`# leader answered STOP for ask ${ask.id}: ${answer.answer}`);
			killGroupOnce();
			settle?.("stopped");
		}
		return answer;
	};
	const backChannel: BackChannel = await startBackChannel({
		socketPath,
		resolve: resolveAsk,
		maxHops: input.maxHops,
		onError: (err) => events?.onLog?.("meta-orch-log", { event: "back_channel_error", phaseId, error: err.message }),
	});

	// 5. Spawn claude -p with the generated config + the ask_brain instruction, then drive the
	//    stream/guardrail to an outcome.
	const instruction = buildAskBrainInstruction({ phaseId });
	const args = hasRunPhase
		? buildRunPhaseArgs({
				planFile: input.runPhaseCommand!.planFile,
				phase: input.runPhaseCommand!.phase,
				agents: input.runPhaseCommand!.agents,
				mcpConfigPath,
				appendSystemPrompt: instruction,
				model: input.model,
			})
		: buildClaudeArgs({ team: input.team!, task: input.task!, mcpConfigPath, appendSystemPrompt: instruction, model: input.model });

	const outcome = await new Promise<PhaseOutcome>((resolve) => {
		let finalReport = "";
		let resultIsError = false;
		let breach: Breach | undefined;
		let resolved = false;

		const finish = (status: PhaseOutcome["status"]) => {
			if (resolved) return;
			resolved = true;
			clearInterval(guardTimer);
			resolve({ phaseId, status, report: finalReport || "(worker produced no final result)", breach, stoppedByLeader, logPath });
		};
		settle = finish;

		const breachAndKill = (b: Breach) => {
			breach = b;
			events?.onNotify?.(phaseId, `guardrail breach — ${describeBreach(b)}; stopping worker`, "warning");
			writeLog(`# GUARDRAIL BREACH: ${describeBreach(b)}`);
			killGroupOnce();
			finish("breached");
		};

		// 6a. The real guardrail clock: evaluate the wall-clock timeout (the only guard) each second.
		const guardTimer = setInterval(() => {
			const b = evaluate(guard, Date.now());
			if (b) breachAndKill(b);
		}, 1000);
		try { (guardTimer as unknown as { unref?: () => void }).unref?.(); } catch { /* ignore */ }

		try {
			child = spawn("claude", args, { stdio: ["ignore", "pipe", "pipe"], env: { ...process.env }, detached: true });
		} catch (err) {
			clearInterval(guardTimer);
			resolve({ phaseId, status: "failed", report: `failed to spawn claude: ${err instanceof Error ? err.message : String(err)}`, stoppedByLeader, logPath });
			return;
		}
		const proc = child;

		// Hand the leader a live ABORT handle now that the worker exists. On session shutdown the leader
		// calls abort() to kill the worker's whole process group (the same teardown as a STOP) rather
		// than leave it — and the `claude` sub-processes it spawned — running after the brain is gone.
		// abort() resolves via `tornDown`, which settles only after the FULL teardown below, so the
		// leader can await an orderly stop.
		input.onStart?.({
			phaseId,
			abort: (): Promise<void> => {
				if (!resolved) {
					aborted = true;
					events?.onNotify?.(phaseId, "leader aborted the phase (shutdown) — killing worker", "warning");
					writeLog(`# leader ABORTED the phase (shutdown) — killing worker process group`);
					killGroupOnce();
					finish("stopped");
				}
				return tornDown;
			},
		});

		// When the child finally exits, unblock teardown and cancel the now-pointless SIGKILL fallback.
		proc.on("exit", () => {
			if (killTimer) { clearTimeout(killTimer); killTimer = null; }
			resolveChildExited();
		});

		const applyEvent = (ev: ClaudeEvent) => {
			switch (ev.kind) {
				case "init": {
					// Real, non-stubbed check that the bridge wired up: the worker MUST report the
					// ask_brain MCP server. If it is absent, the back-channel is dead — the leader can
					// neither hear an escalation nor STOP the worker, so it would run UNSUPERVISED for
					// the whole timeout. That is a setup fault: fail loud + immediate, the same way a
					// spawn failure aborts — kill the child and finish('failed').
					if (!ev.mcpServers.includes(ASK_BRAIN_SERVER_NAME)) {
						const reason = `${ASK_BRAIN_SERVER_NAME} MCP server failed to register — back-channel dead, aborting phase (worker mcp_servers=${JSON.stringify(ev.mcpServers)})`;
						events?.onNotify?.(phaseId, reason, "error");
						writeLog(`# ABORT: ${reason}`);
						finalReport = reason;
						killGroupOnce();
						finish("failed");
					}
					break;
				}
				case "text":
					events?.onProgress?.(phaseId, ev.text);
					break;
				case "result":
					finalReport = ev.text;
					resultIsError = ev.isError;
					break;
				default:
					break; // turn / tool_use / tool_result / noise carry no leader decision (no turn cap)
			}
		};

		// 6b. Stream reader: tee raw lines to the log AND parse for progress + turns + result.
		let buffer = "";
		proc.stdout!.setEncoding("utf-8");
		proc.stdout!.on("data", (chunk: string) => {
			const { lines, rest } = drainLines(buffer, chunk);
			buffer = rest;
			for (const line of lines) {
				writeLog(line); // full transcript → <id>.log
				for (const ev of parseStreamLine(line)) applyEvent(ev);
			}
		});

		proc.stderr!.setEncoding("utf-8");
		proc.stderr!.on("data", (chunk: string) => {
			if (chunk.trim()) writeLog(`# stderr: ${chunk.trimEnd()}`);
		});

		proc.on("error", (err) => {
			writeLog(`# spawn error: ${err.message}`);
			finish("failed");
		});

		proc.on("close", (code) => {
			if (buffer.trim()) {
				writeLog(buffer);
				for (const ev of parseStreamLine(buffer)) applyEvent(ev);
			}
			if (resolved) return; // a breach or a leader STOP already finished us
			if (stoppedByLeader) finish("stopped");
			else if (code === 0 && !resultIsError) finish("completed");
			else finish("failed");
		});
	});

	// 6. Teardown — ORDER MATTERS: if we KILLED the worker (STOP / breach / abort), wait for the child
	//    to actually exit BEFORE unlinking the socket + closing the log, so we don't pull the socket out
	//    from under a still-dying child or drop its final log lines. We bound the wait (the SIGKILL
	//    fallback caps how long a child can linger) so a wedged un-killable process can never hang
	//    teardown. When the worker exited on its own, `childExited` is already resolved → no wait.
	if (killIssued) {
		await Promise.race([childExited, new Promise<void>((res) => { const t = setTimeout(res, 6000); (t as unknown as { unref?: () => void }).unref?.(); })]);
	}
	await backChannel.close();
	await new Promise<void>((res) => logStream.end(() => res()));
	events?.onLog?.("meta-orch-log", { event: "phase_end", phaseId, status: outcome.status, aborted });

	resolveTornDown(); // abort() awaits this — teardown is complete
	return outcome;
}
