// Unit test for the file-based hub-model Pi BRAIN's core logic (brain-core.ts) — NO real Pi, NO real
// worker. Covers handleRunPhase's validation + the single-running guard, that a good call flips
// running + bumps THIS phase's iteration + fires the injected transport with the brain's phase +
// instructions + a backtrack-safe phase id, the retry-budget guard, and the iterationDir path helper.
//
//   node --experimental-strip-types brain-core.test.ts
import {
	type BrainState,
	type PhaseStarter,
	handleRunPhase,
	abortRunningPhase,
	iterationDir,
} from "./brain-core.ts";
import { type ProxyEvents, type PhaseHandle } from "./types.ts";
import * as path from "node:path";

let pass = 0;
const fails: string[] = [];
function check(name: string, cond: boolean, detail = "") {
	if (cond) pass++;
	else fails.push(`  ✗ ${name}${detail ? ` — ${detail}` : ""}`);
}

const NOOP_EVENTS: ProxyEvents = {};
const NOOP_LOG = (_data: Record<string, unknown>) => {};
const NOOP_PUSH = (_text: string) => {};

/** A minimal file-based hub BrainState — enough to drive the pure handler. */
function makeState(): BrainState {
	return {
		sessionName: "sess",
		sessionDir: "/tmp/meta-orch/sess",
		planPath: "/tmp/meta-orch/sess/plan.md",
		routePath: "/tmp/meta-orch/sess/route.yaml",
		availableAgents: ["code-review", "deployer", "onboarding-agent"],
		dirs: { logsDir: "/tmp/meta-orch/sess/logs" },
		runDir: "/tmp/meta-orch/sess",
		planHash: "deadbeef0000",
		hubJsonPath: "/home/u/.meta-orch/hub.json",
		hubStartedByUs: false,
		running: false,
		runningPhase: null,
		iterations: {},
		maxRetries: 10,
		workerType: "claude",
		workerModel: "",
		workerMode: "subagents",
	};
}

async function main() {
	// ── case 1: handleRunPhase validation + running-guard + per-phase iteration (stub starter) ──
	{
		const state = makeState();
		const starts: Array<{ phase: string; instructions: string; iteration: number; phaseId: string }> = [];
		const stubStart: PhaseStarter = (_s, _e, _p, _l, phase, instructions, iteration, phaseId) => {
			starts.push({ phase, instructions, iteration, phaseId });
		};
		const deps = { events: NOOP_EVENTS, pushFollowUp: NOOP_PUSH, log: NOOP_LOG, startPhase: stubStart };

		// 1a. EMPTY instructions are rejected loud; nothing marked running / started
		const emptyRes = handleRunPhase(state, { phase: "0", instructions: "   " }, deps);
		check("run_phase rejects empty instructions", emptyRes.text.includes("instructions are empty"), emptyRes.text);
		check("run_phase rejected → started=false", emptyRes.started === false);
		check("run_phase did not start on empty instructions", state.running === false && starts.length === 0);

		// 1b. EMPTY phase is rejected
		const noPhaseRes = handleRunPhase(state, { phase: "  ", instructions: "do the thing" }, deps);
		check("run_phase rejects empty phase", noPhaseRes.text.includes("phase is empty"), noPhaseRes.text);
		check("run_phase still not running after empty phase", state.running === false && starts.length === 0);

		// 1c. a GOOD call flips running, bumps THIS phase's iteration to 1, starts with the trimmed
		// instructions, and reports the backtrack-safe phase id (orch-sess-p0-1).
		const goodRes = handleRunPhase(state, { phase: "0", instructions: "  build it  " }, deps);
		check("run_phase good call marks running", state.running === true);
		check("run_phase good call started=true", goodRes.started === true);
		check("run_phase good call set phase-0 iteration to 1", state.iterations["0"] === 1);
		check("run_phase good call started exactly one phase", starts.length === 1);
		check("started phase carried phase + trimmed instructions", starts[0]?.phase === "0" && starts[0]?.instructions === "build it");
		check("started phase id is backtrack-safe (p0-1)", starts[0]?.phaseId === "orch-sess-p0-1", starts[0]?.phaseId);
		check("started iteration is 1", starts[0]?.iteration === 1);

		// 1d. a second run_phase while one is running is rejected (single-running guard), no new start
		const busyRes = handleRunPhase(state, { phase: "1", instructions: "next" }, deps);
		check("run_phase rejects a second concurrent run", busyRes.text.includes("already running"), busyRes.text);
		check("rejected concurrent run started=false", busyRes.started === false);
		check("rejected concurrent run did NOT start a phase", starts.length === 1);

		// 1e. once idle, a RERUN of phase 0 bumps its iteration to 2 (a distinct attempt)
		state.running = false;
		const rerun = handleRunPhase(state, { phase: "0", instructions: "fix it" }, deps);
		check("rerun of phase 0 bumped iteration to 2", state.iterations["0"] === 2 && rerun.started === true);
		check("rerun phase id is p0-2", starts[1]?.phaseId === "orch-sess-p0-2", starts[1]?.phaseId);

		// 1f. a fresh phase starts at iteration 1 (worker config is global state, not a per-call param)
		state.running = false;
		handleRunPhase(state, { phase: "2", instructions: "trivial" }, deps);
		check("a new phase starts at iteration 1", starts[2]?.phase === "2" && starts[2]?.iteration === 1);
	}

	// ── case 1g: retry-budget guard — a phase at its budget is REFUSED, not started ──
	{
		const state = makeState();
		state.maxRetries = 2; // two attempts per phase, then stop and ask the human
		const starts: Array<{ phase: string }> = [];
		const stubStart: PhaseStarter = (_s, _e, _p, _l, phase) => { starts.push({ phase }); };
		const deps = { events: NOOP_EVENTS, pushFollowUp: NOOP_PUSH, log: NOOP_LOG, startPhase: stubStart };

		// attempt 1 + attempt 2 both fire (within budget); reset running between them
		const a1 = handleRunPhase(state, { phase: "3", instructions: "try" }, deps);
		check("budget: attempt 1 starts", a1.started === true && state.iterations["3"] === 1);
		state.running = false;
		const a2 = handleRunPhase(state, { phase: "3", instructions: "try again" }, deps);
		check("budget: attempt 2 starts", a2.started === true && state.iterations["3"] === 2);

		// attempt 3 is OVER budget → refused, not started, counter unchanged, message says stop+ask
		state.running = false;
		const a3 = handleRunPhase(state, { phase: "3", instructions: "third try" }, deps);
		check("budget: attempt 3 refused (not started)", a3.started === false && starts.length === 2);
		check("budget: counter not bumped past budget", state.iterations["3"] === 2);
		check("budget: message names the budget + says stop", a3.text.includes("retry budget") && a3.text.includes("meta-server:retries"), a3.text);
		check("budget: running not left set after refusal", state.running === false);

		// raising the budget live (what meta-server:retries does) lets the next attempt fire again
		state.maxRetries = 3;
		const a4 = handleRunPhase(state, { phase: "3", instructions: "after raise" }, deps);
		check("budget: raising the budget lets attempt 3 start", a4.started === true && state.iterations["3"] === 3);
	}

	// ── case 2: iterationDir path shape ──
	check(
		"iterationDir builds phases/<phase>/iteration/<n>/",
		iterationDir("/tmp/meta-orch/sess", "0", 1) === path.join("/tmp/meta-orch/sess", "phases", "0", "iteration", "1"),
		iterationDir("/tmp/meta-orch/sess", "0", 1),
	);

	// ── case 3: abortRunningPhase tears down the live handle and clears it ──
	{
		const state = makeState();
		let aborted = false;
		const handle: PhaseHandle = {
			phaseId: "orch-sess-p0-1",
			abort: async () => { aborted = true; },
		};
		state.running = true;
		state.runningPhase = handle;
		await abortRunningPhase(state);
		check("abortRunningPhase called the handle's abort", aborted === true);
		check("abortRunningPhase cleared runningPhase", state.runningPhase === null);

		// no-op when nothing is running (must not throw)
		let threw = false;
		try { await abortRunningPhase(makeState()); } catch { threw = true; }
		check("abortRunningPhase is a safe no-op when idle", !threw);
	}
}

main()
	.then(() => {
		console.log(`brain-core (file hub): ${pass} checks passed`);
		if (fails.length) { console.log("FAILURES:"); console.log(fails.join("\n")); process.exit(1); }
		console.log("ALL PASS ✓");
	})
	.catch((err) => {
		console.log(`brain-core (file hub) test FAILED to run: ${err instanceof Error ? err.stack : String(err)}`);
		process.exit(1);
	});
