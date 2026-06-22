// Unit test for the Pi BRAIN's core logic (brain-core.ts) — NO real Pi, NO real worker spawn.
//
// Mirrors the SDK brain's test (sdk-leader.test.ts) for the SHARED contract, against the Pi brain's
// framework-free core. Proves:
//   1. relayUp(ask) records a pending ask AND calls the injected sendEscalation with the ask + the
//      escalation text (carrying the ask_id) — the message index.ts paints into the brain TUI.
//   2. handleAnswerWorker resolves the matching pending ask with the right AnswerEnvelope and empties
//      pendingAsks; stop:true flows through; an unknown ask_id is reported (matched:false), not thrown.
//   3. handleRunPhase({phase,agents}) validates the agents against the roster, rejects empty/unknown,
//      enforces the single-running guard, and (on a good call) flips running + bumps runSeq + fires
//      the injected proxy starter with the brain's phase + agents + a backtrack-safe phase id.
//   4. the phase-end drain resolves only THIS phase's asks (stop=true); the shutdown drain resolves
//      ALL and clears the map.
//   5. buildEscalationText names the severity, the ask_id, and the answer_worker call.
//
//   node --experimental-strip-types brain-core.test.ts
import {
	type BrainState,
	type PhaseStarter,
	makeRelayUp,
	handleAnswerWorker,
	handleRunPhase,
	drainPendingAsksForPhase,
	drainAllPendingAsks,
	buildEscalationText,
} from "./brain-core.ts";
import { type AskEnvelope, type AnswerEnvelope } from "./envelope.ts";
import { type ProxyEvents } from "./claude-proxy.ts";

let pass = 0;
const fails: string[] = [];
function check(name: string, cond: boolean, detail = "") {
	if (cond) pass++;
	else fails.push(`  ✗ ${name}${detail ? ` — ${detail}` : ""}`);
}

const NOOP_EVENTS: ProxyEvents = {};
const NOOP_LOG = (_data: Record<string, unknown>) => {};
const NOOP_PUSH = (_text: string) => {};

/** A minimal Pi BrainState with a real pendingAsks map + a small agent roster — enough to drive the
 *  pure handlers. The dirs/proxyModel fields are present but never touched here. */
function makeState(agents: string[] = ["code-review", "deployer", "backend"]): BrainState {
	return {
		planPath: "/tmp/plan.md",
		planSlug: "plan",
		availableAgents: agents,
		dirs: { logsDir: "/tmp/l", socketsDir: "/tmp/s", configsDir: "/tmp/c" },
		runDir: "/tmp",
		planHash: "deadbeef0000",
		proxyModel: "claude-haiku-4-5",
		pendingAsks: new Map(),
		running: false,
		runningPhase: null,
		runSeq: 0,
	};
}

function makeAsk(id: string, phaseId: string, severity: AskEnvelope["severity"] = "blocked"): AskEnvelope {
	return { type: "ask", id, severity, message: `worker stuck on ${id}`, hops: 0, phaseId };
}

async function main() {
	// ── case 1: relayUp → pendingAsks + sendEscalation → handleAnswerWorker resolves the promise ──
	{
		const state = makeState();
		const escalations: Array<{ ask: AskEnvelope; text: string }> = [];
		const relayUp = makeRelayUp(state, (ask, text) => escalations.push({ ask, text }));

		const ask = makeAsk("a1", "orch-test-p1");
		let resolved: AnswerEnvelope | null = null;
		const blocked = relayUp(ask).then((a) => { resolved = a; });

		check("relayUp recorded the pending ask", state.pendingAsks.has("a1"));
		check("worker promise is still blocked", resolved === null);
		check("relayUp called sendEscalation once", escalations.length === 1);
		check("escalation carries the ask object", escalations[0]?.ask.id === "a1");
		check("escalation text carries the ask_id", (escalations[0]?.text ?? "").includes("a1"));
		check("escalation text names answer_worker", (escalations[0]?.text ?? "").includes("answer_worker"));

		const res = handleAnswerWorker(state, { ask_id: "a1", answer: "do it on the rails", stop: false }, NOOP_LOG);
		await blocked;

		check("handleAnswerWorker resolved the blocked promise", resolved !== null);
		check("resolved answer is the AnswerEnvelope", (resolved as AnswerEnvelope | null)?.type === "answer");
		check("resolved answer text matches", (resolved as AnswerEnvelope | null)?.answer === "do it on the rails", JSON.stringify(resolved));
		check("resolved ask_id matches", (resolved as AnswerEnvelope | null)?.id === "a1");
		check("resolved stop is false", (resolved as AnswerEnvelope | null)?.stop === false);
		check("pendingAsks emptied after answer", state.pendingAsks.size === 0);
		check("answer result matched=true", res.matched === true);
		check("answer result mentions resume", res.text.includes("resumes"));
	}

	// ── case 2: handleAnswerWorker stop:true → AnswerEnvelope.stop true ──
	{
		const state = makeState();
		const relayUp = makeRelayUp(state, () => {});
		let resolved: AnswerEnvelope | null = null;
		const blocked = relayUp(makeAsk("a2", "orch-test-p1", "decision")).then((a) => { resolved = a; });
		const res = handleAnswerWorker(state, { ask_id: "a2", answer: "fundamentally wrong", stop: true }, NOOP_LOG);
		await blocked;
		check("stop answer carries stop=true", (resolved as AnswerEnvelope | null)?.stop === true);
		check("stop result has stop=true", res.stop === true);
		check("stop result mentions STOP", res.text.includes("STOP"));
		check("pendingAsks emptied after stop answer", state.pendingAsks.size === 0);
	}

	// ── case 3: handleAnswerWorker for an unknown ask_id → matched:false, not thrown ──
	{
		const state = makeState();
		let threw = false;
		let res = { text: "", matched: true, stop: false };
		try {
			res = handleAnswerWorker(state, { ask_id: "ghost", answer: "x", stop: false }, NOOP_LOG);
		} catch {
			threw = true;
		}
		check("unknown ask_id does not throw", !threw);
		check("unknown ask_id matched=false", res.matched === false);
		check("unknown ask_id reports no pending ask", res.text.includes("No pending worker ask"), res.text);
	}

	// ── case 4: handleRunPhase validation + running-guard (stub starter, no real spawn) ──
	{
		const state = makeState();
		const relayUp = makeRelayUp(state, () => {});
		// Inject a stub phase-starter so the handler's validation + running-guard run WITHOUT spawning
		// a real `claude` (which would bind sockets and need creds). The stub records the
		// brain's chosen phase + agents — exactly what we want to assert reaches the worker layer.
		const starts: Array<{ phase: string; agents: string[]; phaseId: string }> = [];
		const stubStart: PhaseStarter = (_s, _r, _e, _p, _l, phase, agents, phaseId) => {
			starts.push({ phase, agents, phaseId });
		};
		const deps = { relayUp, events: NOOP_EVENTS, pushFollowUp: NOOP_PUSH, log: NOOP_LOG, startPhase: stubStart };

		// 4a. an UNKNOWN agent is rejected loud, and no phase is marked running / started
		const unknownRes = handleRunPhase(state, { phase: "0", agents: ["code-review", "not-a-real-agent"] }, deps);
		check("run_phase rejects an unknown agent", unknownRes.text.includes("Unknown agent"), unknownRes.text);
		check("run_phase rejected → started=false", unknownRes.started === false);
		check("run_phase did not start on a bad agent", state.running === false && starts.length === 0);
		check("run_phase did not bump runSeq on a bad agent", state.runSeq === 0);

		// 4b. an EMPTY agent list (all whitespace) is rejected
		const emptyRes = handleRunPhase(state, { phase: "0", agents: ["   "] }, deps);
		check("run_phase rejects an empty agent list", emptyRes.text.includes("empty list"), emptyRes.text);
		check("run_phase still not running after empty list", state.running === false && starts.length === 0);

		// 4c. a GOOD call flips running, bumps runSeq, starts the phase with the trimmed agents, and
		// reports the backtrack-safe phase id (orch-<slug>-p0-1) with the agents.
		const goodRes = handleRunPhase(state, { phase: "0", agents: ["code-review"] }, deps);
		check("run_phase good call marks running", state.running === true);
		check("run_phase good call started=true", goodRes.started === true);
		check("run_phase good call bumped runSeq to 1", state.runSeq === 1);
		check("run_phase good call started exactly one phase", starts.length === 1);
		check("started phase carried the brain's phase + agents", starts[0]?.phase === "0" && JSON.stringify(starts[0]?.agents) === JSON.stringify(["code-review"]));
		check("started phase id is backtrack-safe (p0-1)", starts[0]?.phaseId === "orch-plan-p0-1", starts[0]?.phaseId);
		check("run_phase good result names the phase + agents", goodRes.text.includes("code-review") && goodRes.text.includes("p0"), goodRes.text);

		// 4d. a second run_phase while one is running is rejected (single-running guard), no new start
		const busyRes = handleRunPhase(state, { phase: "1", agents: ["deployer"] }, deps);
		check("run_phase rejects a second concurrent run", busyRes.text.includes("already running"), busyRes.text);
		check("rejected concurrent run started=false", busyRes.started === false);
		check("rejected concurrent run did NOT start a phase", starts.length === 1);
	}

	// ── case 5a: phase-end drain resolves only THIS phase's asks (stop=true), leaves others ──
	{
		const state = makeState();
		const relayUp = makeRelayUp(state, () => {});
		const settled = new Map<string, AnswerEnvelope>();
		for (const [id, phase] of [["p1a", "orch-test-p1"], ["p1b", "orch-test-p1"], ["p2a", "orch-test-p2"]] as const) {
			void relayUp(makeAsk(id, phase)).then((a) => settled.set(id, a));
		}
		check("three asks pending before drain", state.pendingAsks.size === 3);

		drainPendingAsksForPhase(state, "orch-test-p1");
		await new Promise((r) => setTimeout(r, 0));

		check("phase-1 asks resolved by the drain", settled.has("p1a") && settled.has("p1b"));
		check("phase-1 drained asks are stop=true", settled.get("p1a")?.stop === true && settled.get("p1b")?.stop === true);
		check("phase-2 ask left untouched", !settled.has("p2a") && state.pendingAsks.has("p2a"));
		check("only phase-2 ask remains pending", state.pendingAsks.size === 1);
	}

	// ── case 5b: shutdown drain resolves ALL and clears the map ──
	{
		const state = makeState();
		const relayUp = makeRelayUp(state, () => {});
		const settled = new Map<string, AnswerEnvelope>();
		for (const [id, phase] of [["x1", "orch-test-p1"], ["x2", "orch-test-p2"]] as const) {
			void relayUp(makeAsk(id, phase)).then((a) => settled.set(id, a));
		}

		drainAllPendingAsks(state, "leader is shutting down");
		await new Promise((r) => setTimeout(r, 0));

		check("shutdown drain resolved all asks", settled.size === 2);
		check("shutdown drained asks are stop=true", settled.get("x1")?.stop === true && settled.get("x2")?.stop === true);
		check("shutdown drained reason carried", settled.get("x1")?.answer === "leader is shutting down");
		check("pendingAsks cleared after shutdown drain", state.pendingAsks.size === 0);
	}

	// ── case 6: buildEscalationText names severity + ask_id + answer_worker ──
	{
		const blocked = buildEscalationText(makeAsk("e1", "orch-test-p3", "blocked"));
		check("blocked escalation says BLOCKED", blocked.includes("worker BLOCKED"), blocked);
		const decision = buildEscalationText(makeAsk("e2", "orch-test-p3", "decision"));
		check("decision escalation says DECISION", decision.includes("worker needs a DECISION"), decision);
		check("escalation embeds the ask_id in the answer_worker call", decision.includes(`ask_id: "e2"`), decision);
		check("escalation names the phase", decision.includes("orch-test-p3"), decision);
	}
}

main()
	.then(() => {
		console.log(`brain-core: ${pass} checks passed`);
		if (fails.length) { console.log("FAILURES:"); console.log(fails.join("\n")); process.exit(1); }
		console.log("ALL PASS ✓");
	})
	.catch((err) => {
		console.log(`brain-core test FAILED to run: ${err instanceof Error ? err.stack : String(err)}`);
		process.exit(1);
	});
