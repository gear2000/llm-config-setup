// Unit test for the SDK BRAIN's core logic — NO real query(), NO real worker, a fake stdin.
//
// Proves the linchpin loop the Phase-0 spike validated live, in fast house-style isolation, plus
// the brain's NEW capabilities:
//   1. relayUp(ask) records a pending ask AND pushes an escalation (carrying the ask_id) onto the
//      streaming-input queue — the message that fires the leader's fresh turn.
//   2. answer_worker resolves the matching pending ask with the right AnswerEnvelope and empties
//      pendingAsks; stop:true flows through; an unknown ask_id is handled, not thrown.
//   3. run_phase({phase,agents}) validates the agents against the roster, rejects empty/unknown,
//      enforces the single-running guard, and (on a good call) flips state.running + bumps runSeq.
//   4. the phase-end drain resolves only THIS phase's asks (stop=true); the shutdown drain resolves
//      ALL and clears the map.
//   5. the MessageQueue generator yields a pushed message and close() ends it.
//   6. wireStdin pipes a typed line into the queue as a user message (the human-takes-the-controls
//      path) — driven with a fake input stream, no real process.stdin.
//   7. buildSystemPrompt fills BOTH placeholders (every <PLAN_FILE>, the <AVAILABLE_AGENTS> list);
//      loadAvailableAgents reads .md names and drops _archived-.
//
//   node --experimental-strip-types sdk-leader.test.ts
import { __test, type LeaderState } from "./sdk-leader.ts";
import { type AskEnvelope, type AnswerEnvelope } from "./envelope.ts";
import { type ProxyEvents } from "./claude-proxy.ts";
import { PassThrough } from "node:stream";
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";

const { MessageQueue, makeRelayUp, drainPendingAsksForPhase, drainAllPendingAsks, buildBrainServer, buildSystemPrompt, loadAvailableAgents, wireStdin } = __test;

let pass = 0;
const fails: string[] = [];
function check(name: string, cond: boolean, detail = "") {
	if (cond) pass++;
	else fails.push(`  ✗ ${name}${detail ? ` — ${detail}` : ""}`);
}

const NOOP_EVENTS: ProxyEvents = {};

/** A minimal brain LeaderState with a real queue + pendingAsks map + a small agent roster — enough
 *  to drive the non-query logic. The dirs/runLog/runDir/planHash fields are present but never touched
 *  here (the stub phase-starter never persists). */
function makeState(agents: string[] = ["code-review", "deployer", "backend"]): LeaderState {
	return {
		planPath: "/tmp/plan.md",
		planSlug: "plan",
		availableAgents: agents,
		dirs: { logsDir: "/tmp/l", socketsDir: "/tmp/s", configsDir: "/tmp/c" },
		runDir: "/tmp",
		planHash: "deadbeef0000",
		queue: new MessageQueue(),
		pendingAsks: new Map(),
		runLogPath: "/tmp/run-log.jsonl",
		running: false,
		runningPhase: null,
		runSeq: 0,
	};
}

function makeAsk(id: string, phaseId: string, severity: AskEnvelope["severity"] = "blocked"): AskEnvelope {
	return { type: "ask", id, severity, message: `worker stuck on ${id}`, hops: 0, phaseId };
}

/** Drain ONE message off the queue's generator (the queue is async-iterable). */
async function nextQueued(queue: InstanceType<typeof MessageQueue>): Promise<string | null> {
	const it = queue[Symbol.asyncIterator]();
	const { value, done } = await it.next();
	if (done) return null;
	const content = (value as { message?: { content?: unknown } }).message?.content;
	return typeof content === "string" ? content : JSON.stringify(content);
}

async function main() {
	// ── case 1: relayUp → queue + pendingAsks → answer_worker resolves the promise ──
	{
		const state = makeState();
		const relayUp = makeRelayUp(state);
		const { answerWorker } = buildBrainServer(state, relayUp, NOOP_EVENTS);

		const ask = makeAsk("a1", "orch-test-p1");
		let resolved: AnswerEnvelope | null = null;
		const blocked = relayUp(ask).then((a) => { resolved = a; });

		check("relayUp recorded the pending ask", state.pendingAsks.has("a1"));
		check("worker promise is still blocked", resolved === null);

		const queued = await nextQueued(state.queue);
		check("relayUp pushed an escalation onto the queue", queued !== null);
		check("escalation carries the ask_id", (queued ?? "").includes("a1"), queued ?? "");
		check("escalation names the answer_worker tool", (queued ?? "").includes("answer_worker"));

		const res = await answerWorker.handler({ ask_id: "a1", answer: "do it on the rails", stop: false }, {});
		await blocked;

		check("answer_worker resolved the blocked promise", resolved !== null);
		check("resolved answer is the AnswerEnvelope", (resolved as AnswerEnvelope | null)?.type === "answer");
		check("resolved answer text matches", (resolved as AnswerEnvelope | null)?.answer === "do it on the rails", JSON.stringify(resolved));
		check("resolved ask_id matches", (resolved as AnswerEnvelope | null)?.id === "a1");
		check("resolved stop is false", (resolved as AnswerEnvelope | null)?.stop === false);
		check("pendingAsks emptied after answer", state.pendingAsks.size === 0);
		check("answer_worker result mentions resume", JSON.stringify(res).includes("resumes"));
	}

	// ── case 2: answer_worker stop:true → AnswerEnvelope.stop true ──
	{
		const state = makeState();
		const relayUp = makeRelayUp(state);
		const { answerWorker } = buildBrainServer(state, relayUp, NOOP_EVENTS);
		let resolved: AnswerEnvelope | null = null;
		const blocked = relayUp(makeAsk("a2", "orch-test-p1", "decision")).then((a) => { resolved = a; });
		await nextQueued(state.queue);
		const res = await answerWorker.handler({ ask_id: "a2", answer: "fundamentally wrong", stop: true }, {});
		await blocked;
		check("stop answer carries stop=true", (resolved as AnswerEnvelope | null)?.stop === true);
		check("stop result mentions STOP", JSON.stringify(res).includes("STOP"));
		check("pendingAsks emptied after stop answer", state.pendingAsks.size === 0);
	}

	// ── case 3: answer_worker for an unknown ask_id → handled, not thrown ──
	{
		const state = makeState();
		const relayUp = makeRelayUp(state);
		const { answerWorker } = buildBrainServer(state, relayUp, NOOP_EVENTS);
		let threw = false;
		let text = "";
		try {
			const res = await answerWorker.handler({ ask_id: "ghost", answer: "x", stop: false }, {});
			text = JSON.stringify(res);
		} catch {
			threw = true;
		}
		check("unknown ask_id does not throw", !threw);
		check("unknown ask_id reports no pending ask", text.includes("No pending worker ask"), text);
	}

	// ── case 4: run_phase({phase,agents}) validation + running-guard (stub starter, no real spawn) ──
	{
		const state = makeState();
		const relayUp = makeRelayUp(state);
		// Inject a stub phase-starter so the handler's validation + running-guard run WITHOUT spawning
		// a real `claude` (which would bind sockets and need creds). The stub just records
		// the brain's chosen phase + agents — exactly what we want to assert reaches the worker layer.
		const starts: Array<{ phase: string; agents: string[]; phaseId: string }> = [];
		const stubStart = (_s: LeaderState, _r: typeof relayUp, _e: typeof NOOP_EVENTS, phase: string, agents: string[], phaseId: string) => {
			starts.push({ phase, agents, phaseId });
		};
		const { runPhaseTool } = buildBrainServer(state, relayUp, NOOP_EVENTS, stubStart);

		// 4a. an UNKNOWN agent is rejected loud, and no phase is marked running / started
		const unknownRes = await runPhaseTool.handler({ phase: "0", agents: ["code-review", "not-a-real-agent"] }, {});
		check("run_phase rejects an unknown agent", JSON.stringify(unknownRes).includes("Unknown agent"), JSON.stringify(unknownRes));
		check("run_phase did not start on a bad agent", state.running === false && starts.length === 0);
		check("run_phase did not bump runSeq on a bad agent", state.runSeq === 0);

		// 4b. an EMPTY agent list (all whitespace) is rejected
		const emptyRes = await runPhaseTool.handler({ phase: "0", agents: ["   "] }, {});
		check("run_phase rejects an empty agent list", JSON.stringify(emptyRes).includes("empty list"), JSON.stringify(emptyRes));
		check("run_phase still not running after empty list", state.running === false && starts.length === 0);

		// 4c. a GOOD call flips running, bumps runSeq, starts the phase with the trimmed agents, and
		// reports the backtrack-safe phase id (orch-<slug>-p0-1) with the agents.
		const goodRes = await runPhaseTool.handler({ phase: "0", agents: ["code-review"] }, {});
		check("run_phase good call marks running", state.running === true);
		check("run_phase good call bumped runSeq to 1", state.runSeq === 1);
		check("run_phase good call started exactly one phase", starts.length === 1);
		check("started phase carried the brain's phase + agents", starts[0]?.phase === "0" && JSON.stringify(starts[0]?.agents) === JSON.stringify(["code-review"]));
		check("started phase id is backtrack-safe (p0-1)", starts[0]?.phaseId === "orch-plan-p0-1", starts[0]?.phaseId);
		check("run_phase good result names the phase + agents", JSON.stringify(goodRes).includes("code-review") && JSON.stringify(goodRes).includes("p0"), JSON.stringify(goodRes));

		// 4d. a second run_phase while one is running is rejected (single-running guard), no new start
		const busyRes = await runPhaseTool.handler({ phase: "1", agents: ["deployer"] }, {});
		check("run_phase rejects a second concurrent run", JSON.stringify(busyRes).includes("already running"), JSON.stringify(busyRes));
		check("rejected concurrent run did NOT start a phase", starts.length === 1);
	}

	// ── case 5a: phase-end drain resolves only THIS phase's asks (stop=true), leaves others ──
	{
		const state = makeState();
		const relayUp = makeRelayUp(state);
		const settled = new Map<string, AnswerEnvelope>();
		for (const [id, phase] of [["p1a", "orch-test-p1"], ["p1b", "orch-test-p1"], ["p2a", "orch-test-p2"]] as const) {
			void relayUp(makeAsk(id, phase)).then((a) => settled.set(id, a));
		}
		for (let i = 0; i < 3; i++) await nextQueued(state.queue);
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
		const relayUp = makeRelayUp(state);
		const settled = new Map<string, AnswerEnvelope>();
		for (const [id, phase] of [["x1", "orch-test-p1"], ["x2", "orch-test-p2"]] as const) {
			void relayUp(makeAsk(id, phase)).then((a) => settled.set(id, a));
		}
		for (let i = 0; i < 2; i++) await nextQueued(state.queue);

		drainAllPendingAsks(state, "leader is shutting down");
		await new Promise((r) => setTimeout(r, 0));

		check("shutdown drain resolved all asks", settled.size === 2);
		check("shutdown drained asks are stop=true", settled.get("x1")?.stop === true && settled.get("x2")?.stop === true);
		check("shutdown drained reason carried", settled.get("x1")?.answer === "leader is shutting down");
		check("pendingAsks cleared after shutdown drain", state.pendingAsks.size === 0);
	}

	// ── case 6: MessageQueue generator yields a pushed message and close() ends it ──
	{
		const queue = new MessageQueue();
		queue.push("hello");
		const first = await nextQueued(queue);
		check("queue yields the pushed message", first === "hello", String(first));

		const queue2 = new MessageQueue();
		const it = queue2[Symbol.asyncIterator]();
		queue2.close();
		const { done } = await it.next();
		check("close() ends the generator when empty", done === true);
	}

	// ── case 7: wireStdin pipes a typed line into the queue (human-takes-the-controls path) ──
	{
		const state = makeState();
		const fakeStdin = new PassThrough();
		const close = wireStdin(state, NOOP_EVENTS, fakeStdin);

		fakeStdin.write("redo phase 0 first\n");
		fakeStdin.write("\n"); // a bare Enter is ignored
		fakeStdin.write("use option A\n");
		await new Promise((r) => setTimeout(r, 10)); // let readline emit the line events

		const m1 = await nextQueued(state.queue);
		const m2 = await nextQueued(state.queue);
		check("first typed line reached the queue", (m1 ?? "").includes("redo phase 0 first"), m1 ?? "");
		check("second typed line reached the queue", (m2 ?? "").includes("use option A"), m2 ?? "");
		check("bare Enter did NOT enqueue a message", !((m1 ?? "").trim() === "" || (m2 ?? "").includes("redo phase 0")));
		check("injected line is framed as a steer/answer", (m1 ?? "").toLowerCase().includes("human typed"), m1 ?? "");
		close();
	}

	// ── case 8: buildSystemPrompt fills BOTH placeholders (every <PLAN_FILE> + the agent list) ──
	{
		const dir = fs.mkdtempSync(path.join(os.tmpdir(), "brainprompt-"));
		const promptPath = path.join(dir, "prompt.md");
		fs.writeFileSync(
			promptPath,
			[
				"The plan: <PLAN_FILE>.",
				"Agents: <AVAILABLE_AGENTS>.",
				'Launch: claude -p "/run-phase plan=<PLAN_FILE> phase=<N> agents=<agent-a>"',
			].join("\n"),
		);
		const out = buildSystemPrompt("/home/user/plan.md", ["code-review", "deployer"], promptPath);
		check("no <PLAN_FILE> placeholder remains", !out.includes("<PLAN_FILE>"), out);
		check("BOTH <PLAN_FILE> sites filled (input ref + launch template)", out.split("/home/user/plan.md").length === 3, out);
		check("agent list substituted", out.includes("Agents: code-review, deployer."), out);
		check("<N> and <agent-a> in the template are left for the brain", out.includes("phase=<N>") && out.includes("agents=<agent-a>"), out);

		// missing prompt file → fail loud
		let threw = false;
		try { buildSystemPrompt("/p.md", ["x"], path.join(dir, "nope.md")); } catch { threw = true; }
		check("buildSystemPrompt fails loud on a missing prompt file", threw);
		fs.rmSync(dir, { recursive: true, force: true });
	}

	// ── case 9: loadAvailableAgents reads .md names, drops _archived-, sorts ──
	{
		const dir = fs.mkdtempSync(path.join(os.tmpdir(), "agents-"));
		for (const f of ["deployer.md", "code-review.md", "_archived-watchdog.md", "notes.txt", "README"]) {
			fs.writeFileSync(path.join(dir, f), "x");
		}
		const got = loadAvailableAgents(dir);
		check("loadAvailableAgents returns the .md basenames", JSON.stringify(got) === JSON.stringify(["code-review", "deployer"]), JSON.stringify(got));
		check("loadAvailableAgents drops _archived-", !got.includes("_archived-watchdog"));
		check("loadAvailableAgents drops non-.md files", !got.includes("notes") && !got.includes("README"));
		fs.rmSync(dir, { recursive: true, force: true });
	}
}

main()
	.then(() => {
		console.log(`sdk-leader: ${pass} checks passed`);
		if (fails.length) { console.log("FAILURES:"); console.log(fails.join("\n")); process.exit(1); }
		console.log("ALL PASS ✓");
	})
	.catch((err) => {
		console.log(`sdk-leader test FAILED to run: ${err instanceof Error ? err.stack : String(err)}`);
		process.exit(1);
	});
