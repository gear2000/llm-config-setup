// Unit test for the durable progress ledger (progress.ts) + the shared persist point
// (brain-core.ts recordPhaseProgress) — the RESUME-FROM-PROGRESS path. NO real Pi, NO real worker.
//
// Proves the persist → reload → resume-context path end to end:
//   1. appendProgress / recordPhaseProgress write a JSONL ledger; readProgress reads it back oldest-
//      first, skipping a torn final line.
//   2. statusFromOutcome keeps the legacy raw process-status mapping, while judgePhaseStatus records
//      the TRUE worker verdict from PHASE_RESULT (or a breach) — only "passed" lets resume skip a phase.
//   3. latestStatusByPhase collapses the append history last-write-wins: a phase that FAILED then PASSED
//      on a backtrack reads as passed; one that passed then re-ran and failed reads as failed.
//   4. buildPriorProgressBlock reflects passed vs not-passed correctly, lists handoff summaries, and is
//      null when there is no prior progress.
//   5. a CHANGED plan (planHash mismatch) is NOTED in the block, not blocked.
//   6. the --fresh path (archiveProgress) moves the ledger aside so readProgress → [] → no block.
//   7. an IN-PROGRESS phase (process died before a terminal outcome → no record) is absent → re-run.
//   8. pruneRunArtifacts (the --fresh artifact prune) clears logs/configs/stale-sockets but leaves the
//      durable ledger and the dirs themselves intact, and is safe + idempotent on empty/absent dirs.
//
//   node --experimental-strip-types progress.test.ts
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import {
	appendProgress,
	readProgress,
	statusFromOutcome,
	parsePhaseResult,
	judgePhaseStatus,
	isPassed,
	latestStatusByPhase,
	buildPriorProgressBlock,
	archiveProgress,
	pruneRunArtifacts,
	planContentHash,
	progressPathFor,
	type PhaseProgressEntry,
} from "./progress.ts";
import { recordPhaseProgress } from "./brain-core.ts";

let pass = 0;
const fails: string[] = [];
function check(name: string, cond: boolean, detail = "") {
	if (cond) pass++;
	else fails.push(`  ✗ ${name}${detail ? ` — ${detail}` : ""}`);
}

/** A throwaway run dir under the OS temp dir, unique per case, cleaned at the end. */
function freshRunDir(tag: string): string {
	return fs.mkdtempSync(path.join(os.tmpdir(), `meta-orch-progress-${tag}-`));
}

function rm(dir: string): void {
	try { fs.rmSync(dir, { recursive: true, force: true }); } catch { /* best-effort */ }
}

async function main() {
	const dirs: string[] = [];
		check("completed → passed", statusFromOutcome("completed") === "passed");
		check("failed → failed", statusFromOutcome("failed") === "failed");
		check("stopped → blocked", statusFromOutcome("stopped") === "blocked");
		check("breached → blocked", statusFromOutcome("breached") === "blocked");
		check("null → errored", statusFromOutcome(null) === "errored");
		check("undefined → errored", statusFromOutcome(undefined) === "errored");
		check("explicit errored → errored", statusFromOutcome("errored") === "errored");
		check(
			"parsePhaseResult uses the last verdict line",
			parsePhaseResult("instructions mention PHASE_RESULT: blocked\nfinal line: PHASE_RESULT: passed") === "passed",
		);
		check(
			"completed + PHASE_RESULT partial → partial",
			judgePhaseStatus({ outcomeStatus: "completed", report: "work landed\nPHASE_RESULT: partial" }) === "partial",
		);
		check(
			"completed without PHASE_RESULT → failed, not passed",
			judgePhaseStatus({ outcomeStatus: "completed", report: "worker finished but no verdict" }) === "failed",
		);
		check(
			"turn_cap breach wins over a passed-looking report",
			judgePhaseStatus({ outcomeStatus: "breached", breachKind: "turn_cap", report: "PHASE_RESULT: passed" }) === "breached_turn_cap",
		);

	// ── case 2: append + read round-trip; recordPhaseProgress writes a real terminal record ──
	{
		const dir = freshRunDir("rw"); dirs.push(dir);
		// Two phases via the shared brain-core persist point (the SAME call both leaders make).
		recordPhaseProgress(dir, "0", "orch-plan-p0-1", "completed", "phase 0 report: offboarded clean\nPHASE_RESULT: passed", "hash-aaaa");
		recordPhaseProgress(dir, "1", "orch-plan-p1-2", "failed", "phase 1 report: build broke at layer X", "hash-aaaa");

		const entries = readProgress(dir);
		check("read back two entries", entries.length === 2, `got ${entries.length}`);
		check("entry 0 phase + status", entries[0]?.phase === "0" && entries[0]?.status === "passed");
		check("entry 1 phase + status", entries[1]?.phase === "1" && entries[1]?.status === "failed");
		check("entry carries phaseId", entries[1]?.phaseId === "orch-plan-p1-2");
		check("entry carries summary", (entries[0]?.summary ?? "").includes("offboarded clean"));
		check("entry carries a timestamp", typeof entries[0]?.timestamp === "string" && entries[0]!.timestamp.length > 0);
		check("ledger file exists at progressPathFor", fs.existsSync(progressPathFor(dir)));
	}

	// ── case 3: readProgress on a brand-new run dir → [] (no prior progress) ──
	{
		const dir = freshRunDir("empty"); dirs.push(dir);
		check("absent ledger reads as []", readProgress(dir).length === 0);
		check("no prior progress → null block", buildPriorProgressBlock(readProgress(dir)) === null);
	}

	// ── case 4: readProgress skips a torn final line (crash mid-append) ──
	{
		const dir = freshRunDir("torn"); dirs.push(dir);
		appendProgress(dir, { phase: "0", status: "passed", timestamp: new Date().toISOString() });
		// Simulate a crash that left a half-written final line.
		fs.appendFileSync(progressPathFor(dir), '{"phase":"1","status":"fai');
		const entries = readProgress(dir);
		check("torn line skipped, good line kept", entries.length === 1 && entries[0]?.phase === "0", `got ${entries.length}`);
	}

	// ── case 5: latestStatusByPhase is last-write-wins (failed→passed on backtrack reads passed) ──
	{
		const entries: PhaseProgressEntry[] = [
			{ phase: "0", status: "passed", timestamp: "t0" },
			{ phase: "1", status: "failed", timestamp: "t1" },
			{ phase: "1", status: "passed", timestamp: "t2" }, // retry / backtrack succeeded
			{ phase: "2", status: "passed", timestamp: "t3" },
			{ phase: "2", status: "failed", timestamp: "t4" }, // re-ran and failed later
		];
		const latest = latestStatusByPhase(entries);
		check("phase 1 collapses to latest passed", latest.get("1")?.status === "passed");
		check("phase 2 collapses to latest failed", latest.get("2")?.status === "failed");
		check("one entry per phase after collapse", latest.size === 3, `got ${latest.size}`);
	}

	// ── case 6: buildPriorProgressBlock reflects passed vs not-passed + lists summaries ──
	{
		const dir = freshRunDir("block"); dirs.push(dir);
		recordPhaseProgress(dir, "0", "orch-plan-p0-1", "completed", "offboarded the sample tenant clean\nPHASE_RESULT: passed", "hash-bbbb");
		recordPhaseProgress(dir, "1", "orch-plan-p1-2", "completed", "fixed the sample-service layer; committed\nPHASE_RESULT: passed", "hash-bbbb");
		recordPhaseProgress(dir, "2", "orch-plan-p2-3", "completed", "onboarded fresh from main\nPHASE_RESULT: passed", "hash-bbbb");
		recordPhaseProgress(dir, "3", "orch-plan-p3-4", "completed", "verified some\nPHASE_RESULT: passed", "hash-bbbb");
		recordPhaseProgress(dir, "4A", "orch-plan-p4a-5", "failed", "GET /resources returned vpc_id=null", "hash-bbbb");

		// Same plan content this run → its hash matches the recorded one → no "changed" note.
		const block = buildPriorProgressBlock(readProgress(dir), "hash-bbbb");
		check("block is non-null when progress exists", block !== null);
		const b = block ?? "";
		check("block has a PRIOR PROGRESS header", b.includes("PRIOR PROGRESS"));
		check("block lists passed phase 0", b.includes("phase 0: passed"));
		check("block lists passed phase 3", b.includes("phase 3: passed"));
		check("block carries a passed summary", b.includes("sample-service layer"));
		check("block lists the failed phase 4A", b.includes("phase 4A: failed"));
		check("block carries the failure summary", b.includes("vpc_id=null"));
		check("block tells the brain to resume from the first not-yet-passed", b.toLowerCase().includes("resume from the first"));
		check("block does NOT flag a plan change when hash matches", !b.includes("plan file CHANGED"));
		// Ordering: numeric phases sort by value (0 before 4A in the passed/not-passed lists).
		check("passed phases listed before phase 4A in the not-passed list", b.indexOf("phase 0: passed") < b.indexOf("phase 4A: failed"));
	}

	// ── case 7: a CHANGED plan (hash mismatch) is NOTED, not blocked ──
	{
		const dir = freshRunDir("changed"); dirs.push(dir);
		recordPhaseProgress(dir, "0", "orch-plan-p0-1", "completed", "did phase 0\nPHASE_RESULT: passed", "hash-OLD");
		recordPhaseProgress(dir, "1", "orch-plan-p1-2", "failed", "phase 1 broke", "hash-OLD");
		// Current plan content hashes to something different → the block must WARN but still resume.
		const block = buildPriorProgressBlock(readProgress(dir), "hash-NEW") ?? "";
		check("changed plan still produces a block (not blocked)", block.length > 0);
		check("changed plan note names the change", block.includes("plan file CHANGED"));
		check("changed plan note carries old + new hash", block.includes("hash-OLD") && block.includes("hash-NEW"));
		check("changed plan still lists the failed phase to resume", block.includes("phase 1: failed"));
	}

	// ── case 8: --fresh path — archiveProgress moves the ledger aside → readProgress [] → no block ──
	{
		const dir = freshRunDir("fresh"); dirs.push(dir);
		recordPhaseProgress(dir, "0", "orch-plan-p0-1", "completed", "did phase 0\nPHASE_RESULT: passed", "hash-cccc");
		recordPhaseProgress(dir, "1", "orch-plan-p1-2", "failed", "phase 1 broke", "hash-cccc");
		check("ledger present before fresh", readProgress(dir).length === 2);

		archiveProgress(dir); // this is what --fresh / META_ORCH_FRESH=1 does
		check("live ledger gone after archive", !fs.existsSync(progressPathFor(dir)));
		check("fresh reload reads as [] (ignores prior progress)", readProgress(dir).length === 0);
		check("fresh reload → null PRIOR PROGRESS block", buildPriorProgressBlock(readProgress(dir), "hash-cccc") === null);
		// The archive is KEPT (history preserved), just renamed away from the live filename.
		const archived = fs.readdirSync(dir).filter((f) => f.startsWith("progress.") && f.endsWith(".jsonl") && f !== "progress.jsonl");
		check("prior history preserved as an archived file", archived.length === 1, `archives: ${archived.join(",")}`);
	}

	// ── case 9: an IN-PROGRESS phase (no terminal record) is absent → must re-run ──
	{
		const dir = freshRunDir("inprogress"); dirs.push(dir);
		// Phases 0 and 1 finished; phase 2 was running when the process "died" → nothing written for it.
		recordPhaseProgress(dir, "0", "orch-plan-p0-1", "completed", "phase 0 ok\nPHASE_RESULT: passed", "hash-dddd");
		recordPhaseProgress(dir, "1", "orch-plan-p1-2", "completed", "phase 1 ok\nPHASE_RESULT: passed", "hash-dddd");
		const latest = latestStatusByPhase(readProgress(dir));
		check("finished phases recorded", latest.get("0")?.status === "passed" && latest.get("1")?.status === "passed");
		check("the in-progress phase 2 is absent (→ re-run)", !latest.has("2"));
		const block = buildPriorProgressBlock(readProgress(dir), "hash-dddd") ?? "";
		// With nothing not-passed recorded, the block tells the brain to continue with the next unrun phase.
		check("block notes no not-passed phase recorded", block.includes("NOT PASSED: none recorded"));
		check("block still lists the passed phases", block.includes("phase 0: passed") && block.includes("phase 1: passed"));
	}

	// ── case 10: planContentHash is stable + content-sensitive ──
	{
		const a = planContentHash("# Plan\nPhase 0 ...");
		const aAgain = planContentHash("# Plan\nPhase 0 ...");
		const b = planContentHash("# Plan\nPhase 0 ... CHANGED");
		check("planContentHash is deterministic", a === aAgain);
		check("planContentHash changes with content", a !== b);
		check("planContentHash is a short hex string", /^[0-9a-f]{12}$/.test(a), a);
	}

	// ── case 11: the BUG fix end-to-end — a worker that PROCESS-completes but reports PARTIAL is
	//   recorded `partial` (NOT passed) and shows under NOT PASSED on reload; a turn_cap breach is
	//   recorded `breached_turn_cap` (NOT passed); only the truly-passed phase is skippable on resume. ──
	{
		const dir = freshRunDir("semantic"); dirs.push(dir);
		// Phase 0: real B1-style case — process exited 0, but the worker's own verdict is PARTIAL/BLOCKED.
		recordPhaseProgress(dir, "0", "orch-plan-p0-1", "completed", "only a dormant resolver committed; dispatch gate still blocked\nPHASE_RESULT: partial", "hash-eeee");
		// Phase 1: genuinely passed (work done + evaluator CLEARED → worker said passed).
		recordPhaseProgress(dir, "1", "orch-plan-p1-2", "completed", "fix landed; adversarial-evaluator CLEARED\nPHASE_RESULT: passed", "hash-eeee");
		// Phase 2: the worker was killed by the turn cap before it could emit any verdict.
		recordPhaseProgress(dir, "2", "orch-plan-p2-3", "breached", undefined, "hash-eeee", "turn_cap");

		const latest = latestStatusByPhase(readProgress(dir));
		check("PARTIAL phase recorded partial, NOT passed", latest.get("0")?.status === "partial");
		check("partial is not treated as passed", isPassed(latest.get("0")!.status) === false);
		check("genuinely-passed phase recorded passed", latest.get("1")?.status === "passed" && isPassed(latest.get("1")!.status));
		check("turn_cap breach recorded breached_turn_cap, NOT passed", latest.get("2")?.status === "breached_turn_cap");
		check("breached_turn_cap is not treated as passed", isPassed(latest.get("2")!.status) === false);

		const b = buildPriorProgressBlock(readProgress(dir), "hash-eeee") ?? "";
		// Only phase 1 (passed) is under PASSED/skip; the partial + breached phases are under NOT PASSED.
		check("passed phase 1 listed under PASSED", b.includes("phase 1: passed"));
		check("partial phase 0 listed with its real status", b.includes("phase 0: partial"));
		check("breached phase 2 listed with its real status", b.includes("phase 2: breached_turn_cap"));
		check("NOT PASSED section names the re-run intent", b.includes("NOT PASSED (re-run these"));
		// The partial + breached phases sit in the NOT-PASSED region (after the NOT PASSED header),
		// never in the PASSED-skip region — so resume re-runs them and skips ONLY phase 1.
		const notPassedAt = b.indexOf("NOT PASSED (re-run these");
		check("partial phase 0 is in the NOT-PASSED region", b.indexOf("phase 0: partial") > notPassedAt);
		check("breached phase 2 is in the NOT-PASSED region", b.indexOf("phase 2: breached_turn_cap") > notPassedAt);
	}

	// ── case 10: pruneRunArtifacts clears logs/configs/sockets but never the ledger ──
	{
		const dir = freshRunDir("prune");
		dirs.push(dir);
		const logsDir = path.join(dir, "logs");
		const socketsDir = path.join(dir, "sockets");
		const configsDir = path.join(dir, "configs");
		for (const d of [logsDir, socketsDir, configsDir]) fs.mkdirSync(d, { recursive: true });

		// Seed a prior run's artifacts: two logs, two configs, one leftover socket file.
		fs.writeFileSync(path.join(logsDir, "orch-plan-p0-1.log"), "transcript a");
		fs.writeFileSync(path.join(logsDir, "orch-plan-p1-2.log"), "transcript b");
		fs.writeFileSync(path.join(configsDir, "orch-plan-p0-1.mcp.json"), "{}");
		fs.writeFileSync(path.join(configsDir, "orch-plan-p1-2.mcp.json"), "{}");
		fs.writeFileSync(path.join(socketsDir, "orch-plan-p0-1.sock"), ""); // stale leftover, not a live socket
		// And a durable ledger in runDir itself — prune MUST leave this untouched.
		recordPhaseProgress(dir, "0", "orch-plan-p0-1", "completed", "done\nPHASE_RESULT: passed", "hash-prune");

		const counts = pruneRunArtifacts({ logsDir, socketsDir, configsDir });
		check("prune removed both log files", counts.logs === 2, JSON.stringify(counts));
		check("prune removed both config files", counts.configs === 2, JSON.stringify(counts));
		check("prune removed the stale socket file", counts.sockets === 1, JSON.stringify(counts));
		check("logs dir is now empty", fs.readdirSync(logsDir).length === 0);
		check("configs dir is now empty", fs.readdirSync(configsDir).length === 0);
		check("sockets dir is now empty", fs.readdirSync(socketsDir).length === 0);
		check("the three dirs still exist (only their files were removed)", fs.existsSync(logsDir) && fs.existsSync(socketsDir) && fs.existsSync(configsDir));
		// The ledger lives in runDir, NOT in the three sub-dirs, so prune must not have touched it.
		check("the durable ledger survives the prune", fs.existsSync(progressPathFor(dir)) && readProgress(dir).length === 1);

		// Idempotent + safe on already-empty / absent dirs (a brand-new run): all zeros, no throw.
		const again = pruneRunArtifacts({ logsDir, socketsDir, configsDir });
		check("prune on empty dirs removes nothing", again.logs === 0 && again.configs === 0 && again.sockets === 0);
		let threw = false;
		try {
			pruneRunArtifacts({ logsDir: path.join(dir, "nope-logs"), socketsDir: path.join(dir, "nope-sock"), configsDir: path.join(dir, "nope-cfg") });
		} catch {
			threw = true;
		}
		check("prune on absent dirs does not throw", !threw);
	}

	for (const d of dirs) rm(d);
}

main()
	.then(() => {
		console.log(`progress: ${pass} checks passed`);
		if (fails.length) { console.log("FAILURES:"); console.log(fails.join("\n")); process.exit(1); }
		console.log("ALL PASS ✓");
	})
	.catch((err) => {
		console.log(`progress test FAILED to run: ${err instanceof Error ? err.stack : String(err)}`);
		process.exit(1);
	});
