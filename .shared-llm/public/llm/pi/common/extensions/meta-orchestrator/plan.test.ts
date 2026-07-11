// Unit test for phase-plan validation. Pure (validatePlan) + one temp-file load round-trip.
//   node --experimental-strip-types plan.test.ts
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import { validatePlan, loadPlan } from "./plan.ts";

let pass = 0;
const fails: string[] = [];
function check(name: string, cond: boolean, detail = "") {
	if (cond) pass++;
	else fails.push(`  ✗ ${name}${detail ? ` — ${detail}` : ""}`);
}
function rejects(name: string, raw: unknown, expectFragment: string) {
	try {
		validatePlan(raw);
		fails.push(`  ✗ ${name} — expected throw, got none`);
	} catch (e) {
		const msg = e instanceof Error ? e.message : String(e);
		if (msg.includes(expectFragment)) pass++;
		else fails.push(`  ✗ ${name} — threw but missing "${expectFragment}": ${msg}`);
	}
}

// a valid plan parses and carries the one optional per-phase guardrail field (timeoutSeconds) through.
// turnCap / heartbeatEverySteps are GONE — the worker runs to completion, so they are no longer plan fields.
{
	const plan = validatePlan({
		feature: "onboarding",
		phases: [
			{ team: "onboarding", task: "offboard then onboard", timeoutSeconds: 1800 },
			{ team: "resources", task: "verify" },
		],
	});
	check("feature parsed", plan.feature === "onboarding");
	check("two phases", plan.phases.length === 2);
	check("phase team", plan.phases[0].team === "onboarding");
	check("phase optional timeout kept", plan.phases[0].timeoutSeconds === 1800);
	check("phase without optionals omits them", plan.phases[1].timeoutSeconds === undefined);
}

// fail-loud on every malformed shape, with a precise reason
rejects("non-object top level", [], "top level must be a JSON object");
rejects("missing feature", { phases: [{ team: "t", task: "x" }] }, "`feature` must be a non-empty");
rejects("empty phases", { feature: "f", phases: [] }, "`phases` must be a non-empty array");
rejects("phase missing team", { feature: "f", phases: [{ task: "x" }] }, "team must be a non-empty");
rejects("phase missing task", { feature: "f", phases: [{ team: "t" }] }, "task must be a non-empty");
rejects("bad timeout", { feature: "f", phases: [{ team: "t", task: "x", timeoutSeconds: -1 }] }, "timeoutSeconds must be a positive");

// load from disk: missing file fails loud; bad JSON fails loud; good file round-trips
{
	rejects_file("missing file", "/no/such/plan.json", "plan file not found");
	const dir = fs.mkdtempSync(path.join(os.tmpdir(), "plan-"));
	const badPath = path.join(dir, "bad.json");
	fs.writeFileSync(badPath, "{not json");
	rejects_file("bad json file", badPath, "not valid JSON");
	const goodPath = path.join(dir, "good.json");
	fs.writeFileSync(goodPath, JSON.stringify({ feature: "f", phases: [{ team: "t", task: "x" }] }));
	const loaded = loadPlan(goodPath);
	check("loadPlan round-trips a good file", loaded.feature === "f" && loaded.phases[0].team === "t");
	fs.rmSync(dir, { recursive: true, force: true });
}

function rejects_file(name: string, p: string, expectFragment: string) {
	try {
		loadPlan(p);
		fails.push(`  ✗ ${name} — expected throw, got none`);
	} catch (e) {
		const msg = e instanceof Error ? e.message : String(e);
		if (msg.includes(expectFragment)) pass++;
		else fails.push(`  ✗ ${name} — threw but missing "${expectFragment}": ${msg}`);
	}
}

console.log(`plan: ${pass} checks passed`);
if (fails.length) { console.log("FAILURES:"); console.log(fails.join("\n")); process.exit(1); }
console.log("ALL PASS ✓");
