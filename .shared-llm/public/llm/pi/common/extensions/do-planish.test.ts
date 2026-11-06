/// <reference lib="es2022" />
// Zero-dependency test for do-planish's page port.
// Several unix users on one machine each run their own Pi, and every do-planish page
// server used to bind the fixed port 4390, so the second user's serve failed.
// $WORK_LOG_PORT picks another port; unset keeps 4390.
//   node --experimental-strip-types .shared-llm/public/llm/pi/common/extensions/do-planish.test.ts
import assert from "node:assert/strict";
// @ts-expect-error -- Node native type-stripping resolves the TypeScript source directly.
import { resolvePort } from "./do-planish.ts";

let passed = 0;
function check(name: string, run: () => void): void {
	run();
	passed += 1;
	console.log(`  ✓ ${name}`);
}

check("unset keeps the shared default 4390", () => {
	assert.equal(resolvePort({}), 4390);
});
check("blank keeps the shared default 4390", () => {
	assert.equal(resolvePort({ WORK_LOG_PORT: "  " }), 4390);
});
check("$WORK_LOG_PORT picks this user's port", () => {
	assert.equal(resolvePort({ WORK_LOG_PORT: "4391" }), 4391);
});
check("surrounding spaces are ignored", () => {
	assert.equal(resolvePort({ WORK_LOG_PORT: " 4392 " }), 4392);
});
for (const bad of ["abc", "0", "65536", "43.9", "-1", "4390x", "0x10f6"]) {
	check(`refuses ${JSON.stringify(bad)} with a named error`, () => {
		assert.throws(() => resolvePort({ WORK_LOG_PORT: bad }), /WORK_LOG_PORT/);
	});
}

console.log(`do-planish-port: ${passed}/${passed} passed`);
console.log("ALL PASS ✓");
