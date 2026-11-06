/// <reference lib="es2022" />
// Zero-dependency test for tf-implement's fallback plan path, the one used only
// when the canonical planish_resolve.py cannot be found. It must place plans
// exactly where the script would: one folder per unix user, so several
// accounts can share a machine, with the same guard on that folder.
//   node --experimental-strip-types .shared-llm/public/llm/pi/common/extensions/tf-implement-fallback.test.ts
import assert from "node:assert/strict";
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
// @ts-expect-error -- Node native type-stripping resolves the TypeScript source directly.
import { fallbackPlanPath } from "./tf-implement.ts";

const USER = os.userInfo().username;
const today = new Date();
const ymd = [
	today.getFullYear(),
	String(today.getMonth() + 1).padStart(2, "0"),
	String(today.getDate()).padStart(2, "0"),
].join("-");

let passed = 0;
function check(name: string, run: () => void): void {
	const saved = { WORK_LOG_DIR: process.env.WORK_LOG_DIR, PLANISH_DIR: process.env.PLANISH_DIR };
	delete process.env.WORK_LOG_DIR;
	delete process.env.PLANISH_DIR;
	try {
		run();
	} finally {
		for (const [key, value] of Object.entries(saved)) {
			if (value === undefined) delete process.env[key];
			else process.env[key] = value;
		}
	}
	passed += 1;
	console.log(`  ✓ ${name}`);
}

const scratch = fs.realpathSync(fs.mkdtempSync(path.join(os.tmpdir(), "tf-fallback-")));

check("the default is /var/tmp/<user>/work-log/<date>/<slug>", () => {
	assert.equal(
		fallbackPlanPath(scratch, "Redesign Auth"),
		`/var/tmp/${USER}/work-log/${ymd}/redesign-auth`,
	);
});

check("{user} in $WORK_LOG_DIR names the current unix user", () => {
	process.env.WORK_LOG_DIR = "plans/{user}/{slug}";
	assert.equal(
		fallbackPlanPath(scratch, "Redesign Auth"),
		path.join(scratch, "plans", USER, "redesign-auth"),
	);
});

check("the user folder is created private (0700)", () => {
	process.env.WORK_LOG_DIR = path.join(scratch, "private", "{user}", "work-log", "{slug}");
	fallbackPlanPath(scratch, "Topic");
	assert.equal(fs.lstatSync(path.join(scratch, "private", USER)).mode & 0o777, 0o700);
});

check("a symlinked user folder is refused, not followed", () => {
	const elsewhere = path.join(scratch, "elsewhere");
	fs.mkdirSync(elsewhere);
	fs.mkdirSync(path.join(scratch, "squat"));
	fs.symlinkSync(elsewhere, path.join(scratch, "squat", USER));
	process.env.WORK_LOG_DIR = path.join(scratch, "squat", "{user}", "work-log", "{slug}");
	assert.throws(() => fallbackPlanPath(scratch, "Topic"), /real directory/);
	assert.equal(fs.readdirSync(elsewhere).length, 0);
});

fs.rmSync(scratch, { recursive: true, force: true });
console.log(`tf-implement-fallback: ${passed}/${passed} passed`);
console.log("ALL PASS ✓");
