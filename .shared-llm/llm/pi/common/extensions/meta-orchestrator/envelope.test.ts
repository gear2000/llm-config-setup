// Unit test for the ask_brain wire protocol: envelope validation + hop limit. Pure.
//   node --experimental-strip-types envelope.test.ts
import {
	isAskEnvelope,
	isAnswerEnvelope,
	checkHopLimit,
	makeAnswer,
	encodeFrame,
	decodeFrame,
	type AskEnvelope,
} from "./envelope.ts";

let pass = 0;
const fails: string[] = [];
function check(name: string, cond: boolean, detail = "") {
	if (cond) pass++;
	else fails.push(`  ✗ ${name}${detail ? ` — ${detail}` : ""}`);
}

const goodAsk: AskEnvelope = { type: "ask", id: "a1", severity: "blocked", message: "stuck", hops: 0, phaseId: "orch-x-p1" };

// validation: a good ask passes; missing/typo'd fields fail
check("valid ask passes", isAskEnvelope(goodAsk));
check("ask missing id fails", !isAskEnvelope({ ...goodAsk, id: "" }));
check("ask bad severity fails", !isAskEnvelope({ ...goodAsk, severity: "panic" }));
check("ask non-number hops fails", !isAskEnvelope({ ...goodAsk, hops: "0" }));
check("answer is not an ask", !isAskEnvelope({ type: "answer", id: "a1", answer: "x", stop: false }));

// answer validation
check("valid answer passes", isAnswerEnvelope({ type: "answer", id: "a1", answer: "go", stop: false }));
check("answer missing stop fails", !isAnswerEnvelope({ type: "answer", id: "a1", answer: "go" }));

// hop limit: below cap → allowed (null); at/over cap → rejection answer with stop=true
check("hop below cap allowed", checkHopLimit({ ...goodAsk, hops: 2 }, 5) === null);
{
	const rej = checkHopLimit({ ...goodAsk, hops: 5 }, 5);
	check("hop at cap rejected", rej !== null && rej.stop === true && rej.id === "a1");
	check("hop rejection explains", rej !== null && rej.answer.includes("hop limit"));
}
{
	const rej = checkHopLimit({ ...goodAsk, hops: 9 }, 5);
	check("hop over cap rejected", rej !== null && rej.stop === true);
}

// frame round-trip: encode adds a newline, decode parses both kinds
{
	const frame = encodeFrame(goodAsk);
	check("frame ends with newline", frame.endsWith("\n"));
	const back = decodeFrame(frame.trimEnd());
	check("decode round-trips an ask", back.type === "ask" && (back as AskEnvelope).id === "a1");
	const ans = makeAnswer("a1", "do this", true);
	const back2 = decodeFrame(encodeFrame(ans).trimEnd());
	check("decode round-trips an answer", back2.type === "answer" && (back2 as { answer: string }).answer === "do this");
}

// decode fail-loud: malformed / wrong-shape frames throw
{
	let threw = false;
	try { decodeFrame("{not json"); } catch { threw = true; }
	check("decode throws on non-JSON", threw);
	threw = false;
	try { decodeFrame(JSON.stringify({ type: "weird" })); } catch { threw = true; }
	check("decode throws on unknown shape", threw);
}

console.log(`envelope: ${pass} checks passed`);
if (fails.length) { console.log("FAILURES:"); console.log(fails.join("\n")); process.exit(1); }
console.log("ALL PASS ✓");
