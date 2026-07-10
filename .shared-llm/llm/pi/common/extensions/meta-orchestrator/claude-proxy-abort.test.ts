// Integration test for FIX 2 — abort/shutdown stops a RUNNING phase and the kill targets the whole
// PROCESS GROUP (not just the direct child), so the `claude` sub-processes the worker spawned die too.
//
// This drives the REAL runPhase() against a FAKE `claude`: we drop an executable `claude` script on a
// temp dir, prepend it to PATH, and let runPhase spawn it exactly as it spawns the real CLI. The fake:
//   - prints the stream-json init line WITH ask_brain in mcp_servers (so runPhase's init-check passes
//     and the phase is treated as a healthy running worker),
//   - spawns a long-lived GRANDCHILD (node sleeper) and records its pid to a file — the grandchild
//     stands in for the `claude` sub-processes a real worker launches,
//   - then idles, so the phase stays running until we abort it.
// We grab the abort handle from onStart, abort once the worker + grandchild are up, and assert:
//   - the phase outcome resolved as "stopped",
//   - the per-phase socket file was unlinked (teardown ran),
//   - BOTH the worker child AND the grandchild are dead → the kill hit the process GROUP.
// Then a second case proves the brain wrapper (brain-core.abortRunningPhase) calls the handle and
// drains pending asks, using a fake handle (no spawn) — the unit-level half of the same guarantee.
//
//   node --experimental-strip-types claude-proxy-abort.test.ts
import { runPhase, type PhaseHandle, type PhaseOutcome, type RelayUp } from "./claude-proxy.ts";
import { socketPathFor } from "./phase-id.ts";
import { resolveLimits } from "./guardrails.ts";
import { DEFAULT_MAX_HOPS } from "./envelope.ts";
import { type BrainState, abortRunningPhase } from "./brain-core.ts";
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";

let pass = 0;
const fails: string[] = [];
function check(name: string, cond: boolean, detail = "") {
	if (cond) pass++;
	else fails.push(`  ✗ ${name}${detail ? ` — ${detail}` : ""}`);
}

const NEVER_RELAY: RelayUp = () => new Promise(() => {}); // the fake worker never asks

/** True if a pid is still alive (signal 0 probes without killing; ESRCH ⇒ gone). */
function isAlive(pid: number): boolean {
	try {
		process.kill(pid, 0);
		return true;
	} catch {
		return false;
	}
}

async function waitFor(predicate: () => boolean, timeoutMs: number): Promise<boolean> {
	const deadline = Date.now() + timeoutMs;
	while (Date.now() < deadline) {
		if (predicate()) return true;
		await new Promise((r) => setTimeout(r, 25));
	}
	return predicate();
}

/**
 * Write an executable fake `claude` that prints the init line, spawns a detached grandchild sleeper
 * (recording its pid), then idles. `gcPidFile` receives the grandchild pid so the test can watch it.
 */
function writeFakeClaude(binDir: string, gcPidFile: string): void {
	const script = `#!${process.execPath}
const { spawn } = require("node:child_process");
const fs = require("node:fs");
// Announce a healthy init so runPhase's ask_brain check passes (it kills the worker otherwise).
process.stdout.write(JSON.stringify({ type: "system", subtype: "init", session_id: "fake", model: "fake", mcp_servers: [{ name: "ask_brain" }], tools: [] }) + "\\n");
// Spawn a long-lived grandchild — stands in for the claude sub-processes a real worker launches. It
// stays in OUR process group (default), so a group-kill of us must take it down too.
const gc = spawn(process.execPath, ["-e", "setTimeout(() => {}, 600000)"], { stdio: "ignore" });
fs.writeFileSync(${JSON.stringify(gcPidFile)}, String(gc.pid));
// Idle forever; the test aborts the phase, which group-kills us (and the grandchild).
setInterval(() => {}, 100000);
`;
	const p = path.join(binDir, "claude");
	fs.writeFileSync(p, script);
	fs.chmodSync(p, 0o755);
}

function makeBrainState(): BrainState {
	return {
		planPath: "/tmp/plan.md",
		planSlug: "abort",
		availableAgents: ["code-review"],
		dirs: { logsDir: "/tmp/l", socketsDir: "/tmp/s", configsDir: "/tmp/c" },
		runDir: "/tmp",
		planHash: "deadbeef0000",
		proxyModel: "claude-haiku-4-5",
		pendingAsks: new Map(),
		running: true,
		runningPhase: null,
		runSeq: 1,
	};
}

async function main() {
	// ── case 1: real runPhase + fake claude — abort kills the whole process group, socket torn down ──
	{
		const dir = fs.mkdtempSync(path.join(os.tmpdir(), "fix2-"));
		const binDir = path.join(dir, "bin");
		const dirs = { logsDir: path.join(dir, "logs"), socketsDir: path.join(dir, "sockets"), configsDir: path.join(dir, "configs") };
		for (const d of [binDir, dirs.logsDir, dirs.socketsDir, dirs.configsDir]) fs.mkdirSync(d, { recursive: true });
		const gcPidFile = path.join(dir, "grandchild.pid");
		writeFakeClaude(binDir, gcPidFile);

		const savedPath = process.env.PATH;
		process.env.PATH = `${binDir}:${savedPath ?? ""}`;

		const phaseId = "orch-abort-p0-1";
		const socketPath = socketPathFor(dirs.socketsDir, phaseId);

		let handle: PhaseHandle | null = null;
		const phasePromise: Promise<PhaseOutcome> = runPhase({
			phaseId,
			runPhaseCommand: { planFile: "/tmp/plan.md", phase: "0", agents: ["code-review"] },
			limits: resolveLimits({}), // the generous 4h timeout — far longer than this test
			maxHops: DEFAULT_MAX_HOPS,
			dirs,
			relayUp: NEVER_RELAY,
			onStart: (h) => { handle = h; },
		});

		// Wait until the worker is up: onStart fired AND the grandchild pid file exists.
		const up = await waitFor(() => handle !== null && fs.existsSync(gcPidFile), 8000);
		check("worker spawned and onStart handed over an abort handle", up && handle !== null);
		const gcPid = up && fs.existsSync(gcPidFile) ? Number(fs.readFileSync(gcPidFile, "utf-8").trim()) : NaN;
		check("grandchild process recorded a pid", Number.isInteger(gcPid) && gcPid > 0, String(gcPid));
		check("grandchild is alive before abort", Number.isInteger(gcPid) && isAlive(gcPid));
		check("the per-phase socket exists while the phase runs", fs.existsSync(socketPath));

		// Abort the running phase — this is what session_shutdown does.
		const h = handle as unknown as PhaseHandle;
		await h.abort();
		const outcome = await phasePromise;

		check("aborted phase resolves with status 'stopped'", outcome.status === "stopped", outcome.status);
		// Teardown ran: the socket file was unlinked when the back-channel closed.
		check("the per-phase socket was unlinked on teardown", !fs.existsSync(socketPath));
		// The kill targeted the GROUP: the grandchild (a separate process in the worker's group) is dead.
		const gcDead = await waitFor(() => Number.isInteger(gcPid) && !isAlive(gcPid), 8000);
		check("the grandchild was killed too — the kill hit the process GROUP, not just the child", gcDead);

		if (savedPath === undefined) delete process.env.PATH;
		else process.env.PATH = savedPath;
		// Safety net: if the assertion above failed, don't leak the sleeper.
		if (Number.isInteger(gcPid) && isAlive(gcPid)) { try { process.kill(gcPid, "SIGKILL"); } catch { /* ignore */ } }
		fs.rmSync(dir, { recursive: true, force: true });
	}

	// ── case 2: brain-core.abortRunningPhase calls the handle AND drains pending asks (no spawn) ──
	{
		const state = makeBrainState();
		let aborted = 0;
		const fakeHandle: PhaseHandle = { phaseId: "orch-abort-p0-1", abort: async () => { aborted++; } };
		state.runningPhase = fakeHandle;
		// Seed a pending ask the way relayUp would, so we can prove the drain releases it (stop=true).
		let askStop: boolean | null = null;
		state.pendingAsks.set("k1", {
			ask: { type: "ask", id: "k1", severity: "blocked", message: "stuck", hops: 0, phaseId: "orch-abort-p0-1" },
			phaseId: "orch-abort-p0-1",
			resolve: (ans) => { askStop = ans.stop; },
		});

		await abortRunningPhase(state, "the brain is shutting down");

		check("abortRunningPhase called the running phase's abort()", aborted === 1, `aborted=${aborted}`);
		check("abortRunningPhase cleared the stored handle", state.runningPhase === null);
		check("abortRunningPhase drained the pending ask with stop=true", askStop === true, String(askStop));
		check("abortRunningPhase emptied pendingAsks", state.pendingAsks.size === 0);

		// Safe when nothing is running (no handle): just drains, never throws.
		const idle = makeBrainState();
		idle.runningPhase = null;
		let threw = false;
		try { await abortRunningPhase(idle, "shutdown"); } catch { threw = true; }
		check("abortRunningPhase is safe with no running phase", !threw);
	}
}

main()
	.then(() => {
		console.log(`claude-proxy-abort: ${pass} checks passed`);
		if (fails.length) { console.log("FAILURES:"); console.log(fails.join("\n")); process.exit(1); }
		console.log("ALL PASS ✓");
	})
	.catch((err) => {
		console.log(`claude-proxy-abort test FAILED to run: ${err instanceof Error ? err.stack : String(err)}`);
		process.exit(1);
	});
