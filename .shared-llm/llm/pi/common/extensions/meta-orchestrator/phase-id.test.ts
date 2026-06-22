// Unit test for phase-id naming. Pure — no Pi runtime.
//   node --experimental-strip-types phase-id.test.ts
import { makePhaseId, makeRunPhaseId, sanitizeIdPart, logPathFor, socketPathFor, mcpConfigPathFor } from "./phase-id.ts";

let pass = 0;
const fails: string[] = [];
function check(name: string, cond: boolean, detail = "") {
	if (cond) pass++;
	else fails.push(`  ✗ ${name}${detail ? ` — ${detail}` : ""}`);
}

// sanitizer: lowercase, collapse non-[a-z0-9_-] runs, trim dashes
check("sanitize lowercases + collapses", sanitizeIdPart("Add Project!") === "add-project", sanitizeIdPart("Add Project!"));
check("sanitize trims dashes", sanitizeIdPart("--x--") === "x");
check("sanitize collapses repeats", sanitizeIdPart("a   b") === "a-b");
check("sanitize empty → phase", sanitizeIdPart("###") === "phase");

// phase id: orch-<feature>-p<n>
check("makePhaseId basic", makePhaseId("alpha", 1) === "orch-alpha-p1");
check("makePhaseId sanitizes feature", makePhaseId("Add Project!", 12) === "orch-add-project-p12");

// id is a valid filename segment: no spaces, no ':' or '.'
{
	const id = makePhaseId("Add Project: phase one.", 3);
	check("id has no space/colon/dot", !/[ :.]/.test(id), id);
}

// positive-integer guard
{
	let threw = false;
	try { makePhaseId("x", 0); } catch { threw = true; }
	check("makePhaseId rejects 0", threw);
	threw = false;
	try { makePhaseId("x", 1.5); } catch { threw = true; }
	check("makePhaseId rejects non-integer", threw);
}

// makeRunPhaseId (the SDK brain's id): orch-<slug>-p<phase>-<seq>, phase 0 valid, seq disambiguates
check("makeRunPhaseId basic", makeRunPhaseId("alpha", "3", 7) === "orch-alpha-p3-7");
check("makeRunPhaseId allows phase 0", makeRunPhaseId("brain-smoke-plan", "0", 1) === "orch-brain-smoke-plan-p0-1");
check("makeRunPhaseId sanitizes slug", makeRunPhaseId("Add Project!", "1", 2) === "orch-add-project-p1-2");
{
	// a backtrack re-runs the same phase → distinct ids via seq → distinct sockets
	const a = makeRunPhaseId("alpha", "0", 1);
	const b = makeRunPhaseId("alpha", "0", 5);
	check("same phase, different seq → different id", a !== b, `${a} vs ${b}`);
	const id = makeRunPhaseId("Add Project: phase one.", "0", 3);
	check("run-phase id has no space/colon/dot", !/[ :.]/.test(id), id);
	let threw = false;
	try { makeRunPhaseId("x", "0", 0); } catch { threw = true; }
	check("makeRunPhaseId rejects seq 0", threw);
}

// derived paths: log / socket / mcp-config all keyed by the SAME id, distinct dirs
{
	const id = makePhaseId("alpha", 2);
	check("log path", logPathFor("/r/logs", id) === "/r/logs/orch-alpha-p2.log");
	check("socket path", socketPathFor("/r/sockets", id) === "/r/sockets/orch-alpha-p2.sock");
	check("mcp config path", mcpConfigPathFor("/r/configs", id) === "/r/configs/orch-alpha-p2.mcp.json");
}

console.log(`phase-id: ${pass} checks passed`);
if (fails.length) { console.log("FAILURES:"); console.log(fails.join("\n")); process.exit(1); }
console.log("ALL PASS ✓");
