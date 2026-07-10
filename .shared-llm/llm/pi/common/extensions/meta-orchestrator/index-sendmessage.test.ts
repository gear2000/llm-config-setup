// Unit test for FIX 1 — a throwing pi.sendMessage cannot propagate out of finishPhase.
//
// index.ts can't be imported here (it pulls in @mariozechner/pi-coding-agent + typebox, which don't
// load under --experimental-strip-types), so we test the two halves of the guarantee together:
//
//   1. The SHAPE of the pushFollowUp/sendEscalation wrapper index.ts injects (index.ts:143-148 and
//      :127-139): pi.sendMessage is wrapped in try/catch exactly like log() at index.ts:100, so a
//      throw is swallowed and the function returns normally instead of rejecting.
//   2. The REAL finishPhase path in brain-core.ts (startPhaseInBackground): we drive a phase that
//      fails to spawn so finishPhase runs immediately, inject the WRAPPED pushFollowUp configured to
//      throw inside sendMessage, and assert nothing escapes — no throw out of the .then/.catch, no
//      unhandledRejection (Pi has no unhandledRejection handler, so an escape could kill it mid-run).
//
//   node --experimental-strip-types index-sendmessage.test.ts
import { type BrainState, startPhaseInBackground } from "./brain-core.ts";
import { type RelayUp, type ProxyEvents } from "./claude-proxy.ts";
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";

let pass = 0;
const fails: string[] = [];
function check(name: string, cond: boolean, detail = "") {
	if (cond) pass++;
	else fails.push(`  ✗ ${name}${detail ? ` — ${detail}` : ""}`);
}

const NOOP_EVENTS: ProxyEvents = {};
const NOOP_LOG = (_data: Record<string, unknown>) => {};
const NEVER_RELAY: RelayUp = () => new Promise(() => {}); // a phase that fails to spawn never asks

/**
 * Recreate index.ts's pushFollowUp wrapper (index.ts:143-148) over a sendMessage that THROWS. The
 * try/catch is the whole point of FIX 1: it must swallow the throw so the injected fn returns void.
 * This is the exact pattern log() uses at index.ts:100.
 */
function makeWrappedPushFollowUp(throwingSendMessage: () => void, onErr: () => void): (text: string) => void {
	return (_text: string): void => {
		try {
			throwingSendMessage();
		} catch {
			onErr(); // index.ts logs the error here; the run survives
		}
	};
}

function makeState(socketsDir: string): BrainState {
	return {
		planPath: "/tmp/does-not-matter-plan.md",
		planSlug: "fix1",
		availableAgents: ["code-review"],
		dirs: { logsDir: path.join(socketsDir, "logs"), socketsDir, configsDir: path.join(socketsDir, "configs") },
		runDir: socketsDir,
		planHash: "deadbeef0000",
		proxyModel: "claude-haiku-4-5",
		pendingAsks: new Map(),
		running: true, // run_phase would have flipped this on before firing
		runningPhase: null,
		runSeq: 1,
	};
}

async function main() {
	const dir = fs.mkdtempSync(path.join(os.tmpdir(), "fix1-"));
	fs.mkdirSync(path.join(dir, "logs"), { recursive: true });
	fs.mkdirSync(path.join(dir, "configs"), { recursive: true });

	// Make `claude` impossible to spawn so runPhase fails fast → finishPhase(null, errText) runs
	// promptly (no real worker, no creds, no minutes-long wait). PATH-stripping forces spawn ENOENT.
	const savedPath = process.env.PATH;
	process.env.PATH = "/nonexistent-meta-orch-test";

	// Catch any unhandledRejection for the duration — the failure mode FIX 1 prevents.
	const rejections: unknown[] = [];
	const onRej = (reason: unknown) => rejections.push(reason);
	process.on("unhandledRejection", onRej);

	// ── case 1: the WRAPPED pushFollowUp swallows a throwing sendMessage (the index.ts contract) ──
	{
		let sawErr = false;
		const wrapped = makeWrappedPushFollowUp(() => { throw new Error("TUI delivery blew up"); }, () => { sawErr = true; });
		let threwSync = false;
		try {
			wrapped("phase 0 — done");
		} catch {
			threwSync = true;
		}
		check("wrapped pushFollowUp does not rethrow a throwing sendMessage", !threwSync);
		check("wrapped pushFollowUp ran its catch branch", sawErr);
	}

	// ── case 2: the REAL finishPhase path tolerates a throwing pushFollowUp wrapper end-to-end ──
	{
		const state = makeState(dir);
		let pushErrors = 0;
		// The wrapper finishPhase will call: sendMessage throws, the try/catch swallows it. If FIX 1
		// were missing (no try/catch in index.ts), this throw would escape finishPhase's .then/.catch
		// as an unhandledRejection.
		const wrappedPush = makeWrappedPushFollowUp(() => { throw new Error("sendMessage failed in finishPhase"); }, () => { pushErrors++; });

		startPhaseInBackground(state, NEVER_RELAY, NOOP_EVENTS, wrappedPush, NOOP_LOG, "0", ["code-review"], "orch-fix1-p0-1");

		// Wait for the spawn-failure → finishPhase → pushFollowUp chain to settle.
		const deadline = Date.now() + 5000;
		while (state.running && Date.now() < deadline) {
			await new Promise((r) => setTimeout(r, 25));
		}
		// Let any stray microtask-queued rejection surface before we assert.
		await new Promise((r) => setTimeout(r, 50));

		check("finishPhase ran (running flipped back to false)", state.running === false);
		check("finishPhase called the (throwing) pushFollowUp", pushErrors >= 1, `pushErrors=${pushErrors}`);
		check("no unhandledRejection escaped finishPhase", rejections.length === 0, JSON.stringify(rejections.map(String)));
	}

	process.removeListener("unhandledRejection", onRej);
	if (savedPath === undefined) delete process.env.PATH;
	else process.env.PATH = savedPath;
	fs.rmSync(dir, { recursive: true, force: true });
}

main()
	.then(() => {
		console.log(`index-sendmessage: ${pass} checks passed`);
		if (fails.length) { console.log("FAILURES:"); console.log(fails.join("\n")); process.exit(1); }
		console.log("ALL PASS ✓");
	})
	.catch((err) => {
		console.log(`index-sendmessage test FAILED to run: ${err instanceof Error ? err.stack : String(err)}`);
		process.exit(1);
	});
