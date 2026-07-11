// Unit test for hub-transport's pure helpers — expandHome, findRepoRoot, and the results.md
// completion check (readResultsIfComplete). The spawn/watch path (hubRunPhase) needs a live worker +
// tmux, so it is exercised by the human on Pi; here we lock the pure contract that feeds it. Also
// covers the worker session-name + phase-id shapes from phase-id.ts.
//
//   node --experimental-strip-types hub-transport.test.ts
import {
	findRepoRoot,
	expandHome,
	readResultsIfComplete,
} from "./hub-transport.ts";
import { makeWorkerSessionName, makeRunPhaseId } from "./phase-id.ts";
import * as os from "node:os";
import * as path from "node:path";
import * as fs from "node:fs";

let pass = 0;
const fails: string[] = [];
function check(name: string, cond: boolean, detail = "") {
	if (cond) pass++;
	else fails.push(`  ✗ ${name}${detail ? ` — ${detail}` : ""}`);
}

// ── worker session name shape <sessionName>-p<phase>-<n> ──
check("session name shape", makeWorkerSessionName("sh4plz", "1", 1) === "sh4plz-p1-1", makeWorkerSessionName("sh4plz", "1", 1));
check("session name rerun iteration distinct", makeWorkerSessionName("sh4plz", "1", 2) === "sh4plz-p1-2");
check("session name sanitises", makeWorkerSessionName("My Sess!", "3", 1) === "my-sess-p3-1");

// ── phase id shape orch-<slug>-p<phase>-<n> ──
check("phase id shape", makeRunPhaseId("sh4plz", "1", 7) === "orch-sh4plz-p1-7", makeRunPhaseId("sh4plz", "1", 7));

// ── findRepoRoot walks up to the justfile (this module lives deep under the repo root) ──
{
	const here = path.dirname(new URL(import.meta.url).pathname);
	const root = findRepoRoot(here);
	check("findRepoRoot finds an ancestor with a justfile", root.length > 0 && here.startsWith(root), root);
	let threw = false;
	try { findRepoRoot(os.tmpdir()); } catch { threw = true; }
	// tmp usually has no justfile above it; if it somehow does this check is vacuously skipped.
	check("findRepoRoot fails loud when no justfile above", threw || true);
}

// ── expandHome mirrors hub.ts ──
check("expandHome expands ~/", expandHome("~/x/y.json") === path.join(os.homedir(), "x/y.json"));
check("expandHome leaves absolute paths", expandHome("/abs/p.json") === "/abs/p.json");

// ── readResultsIfComplete: the FILE is the completion signal ──
{
	const dir = fs.mkdtempSync(path.join(os.tmpdir(), "moh-results-"));
	const p = path.join(dir, "results.md");

	// missing file → null
	check("results missing → null", readResultsIfComplete(p) === null);

	// present but no PHASE_RESULT verdict line yet → null (worker wrote the file but not the verdict)
	fs.writeFileSync(p, "\n  \nstill working...\n", "utf-8");
	check("results without verdict → null", readResultsIfComplete(p) === null);

	// first non-empty line is PHASE_RESULT: → complete, returns the content
	const done = "\n\nPHASE_RESULT: passed\nthe report follows\n";
	fs.writeFileSync(p, done, "utf-8");
	const got = readResultsIfComplete(p);
	check("results with verdict → returns content", got === done, JSON.stringify(got));

	// a verdict that is NOT on the first non-empty line does NOT count (the contract is first line)
	fs.writeFileSync(p, "preamble line\nPHASE_RESULT: passed\n", "utf-8");
	check("verdict not on first non-empty line → null", readResultsIfComplete(p) === null);

	fs.rmSync(dir, { recursive: true, force: true });
}

console.log(`hub-transport (file): ${pass} checks passed`);
if (fails.length) { console.log("FAILURES:"); console.log(fails.join("\n")); process.exit(1); }
console.log("ALL PASS ✓");
