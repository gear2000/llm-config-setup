// hub.test.ts — unit test for the /hub command's pure arg parsing.
// Run: node --experimental-strip-types hub.test.ts
// (Importing hub.ts also confirms the extension strips/loads clean — its
//  @mariozechner type import is erased, the rest are node builtins.)

import { parseHubArgs } from "./hub.ts";

let pass = 0;
let fail = 0;
function eq(name: string, got: unknown, want: unknown): void {
	const g = JSON.stringify(got);
	const w = JSON.stringify(want);
	if (g === w) {
		pass++;
	} else {
		fail++;
		console.error(`FAIL ${name}: got ${g} want ${w}`);
	}
}

eq("empty -> status", parseHubArgs(""), { sub: "status" });
eq("start", parseHubArgs("start"), { sub: "start" });
eq("start --json", parseHubArgs("start --json /tmp/a.json"), { sub: "start", json: "/tmp/a.json" });
eq("status", parseHubArgs("status"), { sub: "status" });
eq("stop", parseHubArgs("stop"), { sub: "stop" });
eq("flag only -> defaults to status", parseHubArgs("--json /x"), { sub: "status", json: "/x" });
eq("extra whitespace", parseHubArgs("  start   --json   /p  "), { sub: "start", json: "/p" });
eq("trailing --json with no value -> ignored", parseHubArgs("start --json"), { sub: "start" });

console.log(`hub.test: ${pass} passed, ${fail} failed`);
if (fail > 0) process.exit(1);
