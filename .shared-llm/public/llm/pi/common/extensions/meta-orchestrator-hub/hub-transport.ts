/**
 * hub-transport — the FILE-BASED per-phase worker transport.
 *
 * This is the whole transport for the file-based hub model. It runs ONE phase attempt by:
 *   1. writing the brain's instructions for this attempt to
 *      `phases/<phase>/iteration/<n>/instructions.md`,
 *   2. spawning an INTERACTIVE Claude Code TUI worker in tmux via
 *      `just worker-up <session> <hub> <plan.md> <instructions.md> <route.yaml> <results.md> <team>` — the worker
 *      runs `/meta-auto-run`, which connects to the hub, does the phase work, and WRITES
 *      `phases/<phase>/iteration/<n>/results.md` whose first non-empty line is `PHASE_RESULT: <verdict>`,
 *   3. WATCHING for that results.md — polling until it exists and its first non-empty line matches
 *      `/^PHASE_RESULT:/`. THAT FILE is the completion signal, not any hub message,
 *   4. reading the file as the `report` and tearing the worker tmux session down.
 *
 * The verdict is NOT parsed here — judgePhaseStatus (progress.ts) reads `PHASE_RESULT:` out of the
 * `report`. This module only writes instructions, spawns the worker, watches the file, and delivers
 * its contents as `report`; it judges nothing.
 *
 * Node built-ins only (child_process, fs, path, url) — no npm deps.
 */

import { spawn, type ChildProcess } from "node:child_process";
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import { fileURLToPath } from "node:url";
import { logPathFor } from "./phase-id.ts";
import type { RunPhaseInput, PhaseOutcome } from "./types.ts";

/** Default ceiling for how long to watch for results.md before failing loud (ms). A phase that has
 *  not written its verdict in 30 minutes is treated as a blocked/timeout outcome. Override per-call
 *  via RunPhaseInput.resultsTimeoutMs. */
export const DEFAULT_RESULTS_TIMEOUT_MS = 30 * 60 * 1000;

/** How often to poll for the results.md file (ms). */
export const RESULTS_POLL_INTERVAL_MS = 2000;

/**
 * Expand a leading `~/` (or a bare `~`) to the home dir. Mirrors hub.ts's expandHome — a user-typed
 * `--hub-json ~/.meta-orch/x.json` arrives with a literal tilde (Pi args don't pass through a shell).
 */
export function expandHome(p: string): string {
	if (p === "~") return os.homedir();
	if (p.startsWith("~/")) return path.join(os.homedir(), p.slice(2));
	return p;
}

const sleep = (ms: number): Promise<void> => new Promise((res) => setTimeout(res, ms));

/**
 * Walk up from this module's directory to the nearest ancestor holding a `justfile` (the repo root —
 * `just worker-up`/`worker-down` are defined there and must run with that cwd). Fail loud if none is
 * found rather than spawn `just` in the wrong place where the recipes don't exist.
 */
export function findRepoRoot(startDir: string): string {
	let dir = startDir;
	for (;;) {
		if (fs.existsSync(path.join(dir, "justfile"))) return dir;
		const parent = path.dirname(dir);
		if (parent === dir) {
			throw new Error(`hub-transport: no justfile found walking up from ${startDir} — cannot locate the repo root for 'just worker-up'`);
		}
		dir = parent;
	}
}

/**
 * Run `just <recipe> <args...>` to completion with the given cwd, resolving with the exit code. Used
 * for both `worker-up` (spawn the TUI worker) and `worker-down` (teardown). Never rejects — a
 * non-zero/teardown error is surfaced via the resolved code so the caller can log it without an
 * unhandled rejection taking the brain down.
 */
function runJust(repoRoot: string, args: string[]): Promise<{ code: number | null; stderr: string }> {
	return new Promise((resolve) => {
		let child: ChildProcess;
		try {
			child = spawn("just", args, { cwd: repoRoot, stdio: ["ignore", "ignore", "pipe"], env: { ...process.env } });
		} catch (err) {
			resolve({ code: null, stderr: err instanceof Error ? err.message : String(err) });
			return;
		}
		let stderr = "";
		child.stderr?.setEncoding("utf-8");
		child.stderr?.on("data", (c: string) => { stderr += c; });
		child.on("error", (err) => resolve({ code: null, stderr: err.message }));
		child.on("close", (code) => resolve({ code, stderr: stderr.trimEnd() }));
	});
}

/** The first non-empty line of a file's content, or "" if the file is empty / all blank. */
function firstNonEmptyLine(content: string): string {
	for (const raw of content.split("\n")) {
		const line = raw.trim();
		if (line) return line;
	}
	return "";
}

/**
 * Is results.md present AND complete? Complete = its first non-empty line starts with `PHASE_RESULT:`.
 * Returns the file content when complete, else null (file missing, empty, or verdict line not yet
 * written — the worker writes the file then the verdict, so we wait for the verdict line). A read
 * error other than ENOENT is swallowed → treated as not-yet-ready, so a mid-write read does not crash
 * the poll loop.
 */
export function readResultsIfComplete(resultsPath: string): string | null {
	let content: string;
	try {
		content = fs.readFileSync(resultsPath, "utf-8");
	} catch {
		return null; // not written yet (ENOENT) or transient read error mid-write
	}
	return /^PHASE_RESULT:/.test(firstNonEmptyLine(content)) ? content : null;
}

/**
 * Run one phase attempt over the file-based transport. Resolves with a PhaseOutcome; throws only on a
 * real setup fault the brain must see (no runPhaseCommand, repo root not found, worker-up failed, or
 * the results.md never appeared within the timeout). A worker that writes results.md (even a failing
 * verdict) resolves "completed" and the verdict lives in `report` for judgePhaseStatus.
 *
 * Teardown (`just worker-down <session>`) is GUARANTEED on every exit path — success, error, timeout,
 * and abort — and is idempotent, so no tmux session leaks.
 */
export async function hubRunPhase(input: RunPhaseInput): Promise<PhaseOutcome> {
	const { phaseId, dirs, events } = input;

	if (!input.runPhaseCommand) {
		throw new Error("hubRunPhase: runPhaseCommand is required (the file transport runs the /meta-auto-run worker path)");
	}
	const rpc = input.runPhaseCommand;
	if (!rpc.instructionsPath || !rpc.routeFile || !rpc.resultsPath || !rpc.planFile) {
		throw new Error("hubRunPhase: runPhaseCommand needs planFile, instructionsPath, routeFile and resultsPath — the brain-core sets these from the session dir");
	}

	fs.mkdirSync(dirs.logsDir, { recursive: true });
	const logPath = logPathFor(dirs.logsDir, phaseId);
	const logStream = fs.createWriteStream(logPath, { flags: "w" });
	const writeLog = (line: string) => { try { logStream.write(line.endsWith("\n") ? line : line + "\n"); } catch { /* best-effort */ } };
	const closeLog = (): Promise<void> => new Promise<void>((res) => logStream.end(() => res()));

	// 1. Write this attempt's instructions.md (the brain wrote the text; the transport lays it down).
	fs.mkdirSync(path.dirname(rpc.instructionsPath), { recursive: true });
	fs.writeFileSync(rpc.instructionsPath, rpc.instructions ?? "", "utf-8");

	// 2. Locate the repo root (justfile) so `just worker-up/worker-down` run with the right cwd.
	const repoRoot = findRepoRoot(path.dirname(fileURLToPath(import.meta.url)));
	const session = rpc.session;

	// Build the worker-launch command for this run's harness.
	// claude  → `worker-up`        (a Claude TUI that runs /meta-auto-run, with team|subagents from `mode`)
	// pi      → `worker-up-pi`     (a single-agent Pi TUI on `model`, no hub, no team)
	// cursor  → `worker-up-cursor` (a Cursor Agent --print headless worker on `model`, no hub, no team)
	// All three write results.md, which is what the transport watches.
	const workerType = rpc.workerType;
	// Legacy wire arg kept for worker-up compatibility. Synchronized meta execution never enables
	// Claude team mode; phase/stage agents are resolved by the route profile.
	const teamArg = "false";
	let upArgs: string[];
	if (workerType === "pi") {
		upArgs = ["worker-up-pi", session, rpc.model, rpc.planFile, rpc.instructionsPath, rpc.resultsPath];
	} else if (workerType === "cursor") {
		upArgs = ["worker-up-cursor", session, rpc.model, rpc.planFile, rpc.instructionsPath, rpc.resultsPath];
	} else {
		// "claude" — the default
		upArgs = ["worker-up", session, input.hubJsonPath, rpc.planFile, rpc.instructionsPath, rpc.resultsPath, teamArg];
	}

	writeLog(`# meta-orchestrator phase ${phaseId} — FILE transport`);
	writeLog(`# session: ${session}`);
	writeLog(`# plan: ${rpc.planFile}`);
	writeLog(`# instructions: ${rpc.instructionsPath}`);
	writeLog(`# route: ${rpc.routeFile}`);
	writeLog(`# results (watched): ${rpc.resultsPath}`);
	if (workerType === "cursor") writeLog(`# worker: cursor (model ${rpc.model})`);
	else if (workerType === "pi") writeLog(`# worker: pi (model ${rpc.model})`);
	else writeLog(`# worker: claude (mode ${rpc.mode})`);
	if (workerType === "claude") writeLog(`# hub: ${input.hubJsonPath}`);

	// Teardown is idempotent + guaranteed. Run at most once.
	let tornDown = false;
	const teardown = async (): Promise<void> => {
		if (tornDown) return;
		tornDown = true;
		const r = await runJust(repoRoot, ["worker-down", session]);
		if (r.code !== 0) writeLog(`# worker-down exit=${r.code}${r.stderr ? ` stderr=${r.stderr}` : ""}`);
		else writeLog(`# worker-down ${session} ok`);
	};

	// 3. abort(): the leader calls this on session_shutdown — tear the worker down and resolve the
	//    outcome as "stopped". The poll loop observes `aborted` and returns stoppedOutcome itself.
	let aborted = false;
	const stoppedOutcome = (): PhaseOutcome => ({
		phaseId,
		status: "stopped",
		report: "(phase aborted on brain shutdown — worker torn down before it wrote results.md)",
		stoppedByLeader: false,
		logPath,
	});

	input.onStart?.({
		phaseId,
		abort: async (): Promise<void> => {
			if (aborted) return;
			aborted = true;
			events?.onNotify?.(phaseId, "leader aborted the phase (shutdown) — tearing down worker", "warning");
			writeLog(`# leader ABORTED the phase (shutdown) — worker-down ${session}`);
			await teardown();
		},
	});

	try {
		// 4. Spawn the interactive TUI worker. Its tmux output is irrelevant — completion is the file.
		events?.onLog?.("meta-orch-log", { event: "worker_up", phaseId, session, workerType: rpc.workerType, model: workerType === "pi" ? rpc.model : undefined });
		const up = await runJust(repoRoot, upArgs);
		if (up.code !== 0) {
			throw new Error(`just ${upArgs[0]} ${session} failed (exit=${up.code})${up.stderr ? `: ${up.stderr}` : ""}`);
		}
		if (aborted) { await closeLog(); return stoppedOutcome(); }

		// 5. WATCH for results.md — poll until it exists AND its first non-empty line is PHASE_RESULT:.
		//    The FILE is the completion signal. Bound by a generous timeout; on timeout, fail loud.
		const timeoutMs = input.resultsTimeoutMs ?? DEFAULT_RESULTS_TIMEOUT_MS;
		const deadline = Date.now() + timeoutMs;
		writeLog(`# watching ${rpc.resultsPath} (timeout ${timeoutMs}ms, poll ${RESULTS_POLL_INTERVAL_MS}ms)`);
		let report: string | null = null;
		while (Date.now() < deadline) {
			if (aborted) { await closeLog(); return stoppedOutcome(); }
			report = readResultsIfComplete(rpc.resultsPath);
			if (report !== null) break;
			await sleep(RESULTS_POLL_INTERVAL_MS);
		}
		if (report === null) {
			throw new Error(`hubRunPhase: results.md never reached a PHASE_RESULT verdict within ${timeoutMs}ms at ${rpc.resultsPath} — worker ${session} did not finish the phase`);
		}

		// 6. The worker wrote a verdict → "completed". The verdict lives in `report`; judgePhaseStatus
		//    reads it — this transport judges nothing.
		writeLog(`# results.md complete — worker reported a PHASE_RESULT verdict`);
		writeLog(report);
		events?.onLog?.("meta-orch-log", { event: "results_ready", phaseId, session, resultsPath: rpc.resultsPath });
		await teardown();
		await closeLog();
		return {
			phaseId,
			status: "completed",
			report,
			stoppedByLeader: false,
			logPath,
		};
	} catch (err) {
		if (aborted) {
			await teardown();
			await closeLog();
			return stoppedOutcome();
		}
		writeLog(`# hub-transport error: ${err instanceof Error ? err.message : String(err)}`);
		await teardown();
		await closeLog();
		throw err;
	} finally {
		// Belt-and-suspenders: teardown is idempotent, so a path that somehow skipped it is still
		// cleaned up. The first call already ran; this is a no-op then.
		await teardown();
	}
}
