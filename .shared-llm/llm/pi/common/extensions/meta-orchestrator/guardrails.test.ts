// Unit test for the guardrail state machine with an INJECTED clock — no real setTimeout.
// Proves the ONE remaining limit (a generous wall-clock timeout) actually fires, rather than being
// decorative. There is no turn cap and no liveness/heartbeat kill any more — the worker runs to
// completion like a plain `claude -p`; only this last-resort guard remains.
//   node --experimental-strip-types guardrails.test.ts
import {
	resolveLimits,
	createGuardrailState,
	evaluate,
	describeBreach,
	DEFAULT_TIMEOUT_SECONDS,
} from "./guardrails.ts";

let pass = 0;
const fails: string[] = [];
function check(name: string, cond: boolean, detail = "") {
	if (cond) pass++;
	else fails.push(`  ✗ ${name}${detail ? ` — ${detail}` : ""}`);
}

// resolveLimits: a GENEROUS default + an override; nothing but the timeout is configured now.
{
	const def = resolveLimits({});
	check("default timeout", def.timeoutMs === DEFAULT_TIMEOUT_SECONDS * 1000);
	// The default must be generous enough to never cut normal work — at least a couple of hours.
	check("default timeout is generous (>= 2h)", def.timeoutMs >= 2 * 60 * 60 * 1000, `${def.timeoutMs}ms`);
	const over = resolveLimits({ timeoutSeconds: 100 });
	check("override timeout", over.timeoutMs === 100_000);
	// resolveLimits only knows about the timeout now — no turn cap / heartbeat fields on the result.
	check("limits expose only timeoutMs", Object.keys(over).join(",") === "timeoutMs", Object.keys(over).join(","));
}

// within bounds → no breach
{
	const limits = resolveLimits({ timeoutSeconds: 100 });
	const s = createGuardrailState(limits, 0);
	check("fresh state is within bounds", evaluate(s, 1000) === null);
}

// TIMEOUT fires at the wall-clock budget (the only guard)
{
	const limits = resolveLimits({ timeoutSeconds: 10 });
	const s = createGuardrailState(limits, 0);
	check("no timeout just before budget", evaluate(s, 9_999) === null);
	const b = evaluate(s, 10_000);
	check("timeout fires at budget", b?.kind === "timeout");
	check("timeout describes", b ? describeBreach(b).includes("timeout") : false);
}

// A quiet, long-running worker is NEVER killed before the budget — no liveness/heartbeat guard
// exists any more, so the only thing that can stop a within-budget worker is the wall clock.
{
	const limits = resolveLimits({ timeoutSeconds: 3600 }); // 1h budget
	const s = createGuardrailState(limits, 0);
	// 50 minutes of total silence (no turns, no ask_brain) and still no breach — the OLD heartbeat
	// kill would have fired here; now nothing does until the wall-clock budget itself is hit.
	check("silent worker within budget is not killed", evaluate(s, 50 * 60_000) === null);
	check("only the wall clock can stop it", evaluate(s, 3_600_000)?.kind === "timeout");
}

console.log(`guardrails: ${pass} checks passed`);
if (fails.length) { console.log("FAILURES:"); console.log(fails.join("\n")); process.exit(1); }
console.log("ALL PASS ✓");
