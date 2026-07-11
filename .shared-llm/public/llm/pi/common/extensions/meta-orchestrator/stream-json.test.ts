// Unit test for the claude -p stream-json parser (the bridge). Pure — no Pi runtime, no child.
// Zero deps; runs on Node 22.6+ native type-stripping:
//   node --experimental-strip-types stream-json.test.ts
//
// The envelope shapes asserted here were captured LIVE from
//   claude -p --output-format stream-json --verbose
// (claude-code 2.1.x): a system/init line carrying mcp_servers + tools, an assistant message
// whose content is an array of {type:"text"} / {type:"tool_use"} blocks, and a terminal
// result line with subtype/is_error/result. This parser is the most drift-prone surface in
// the extension, which is why it gets the most cases.
import { parseStreamLine, liftMcpServerNames, drainLines, type ClaudeEvent } from "./stream-json.ts";

let pass = 0;
const fails: string[] = [];
function check(name: string, cond: boolean, detail = "") {
	if (cond) pass++;
	else fails.push(`  ✗ ${name}${detail ? ` — ${detail}` : ""}`);
}

function kinds(events: ClaudeEvent[]): string[] {
	return events.map((e) => e.kind);
}

// 1. blank line → no events
check("blank line yields nothing", parseStreamLine("   ").length === 0);

// 2. malformed JSON → a single noise event (never throws)
check("malformed line is noise", kinds(parseStreamLine("{not json")).join() === "noise");

// 3. system/init lifts session, model, mcp servers (array-of-objects form, live shape) and tools
{
	const line = JSON.stringify({
		type: "system",
		subtype: "init",
		session_id: "sess-1",
		model: "claude-opus",
		mcp_servers: [{ name: "ask_brain", status: "connected" }, { name: "other", status: "connected" }],
		tools: ["Bash", "Read"],
	});
	const evs = parseStreamLine(line);
	const init = evs[0];
	check("init kind", init?.kind === "init");
	if (init?.kind === "init") {
		check("init sessionId", init.sessionId === "sess-1");
		check("init model", init.model === "claude-opus");
		check("init mcpServers includes ask_brain", init.mcpServers.includes("ask_brain"), JSON.stringify(init.mcpServers));
		check("init tools", init.tools.join(",") === "Bash,Read");
	}
}

// 4. system/hook lines are noise (no progress signal)
check("hook_started is noise", kinds(parseStreamLine(JSON.stringify({ type: "system", subtype: "hook_started" }))).join() === "noise");

// 5. assistant message with text + tool_use blocks → text, tool_use, then a turn marker
{
	const line = JSON.stringify({
		type: "assistant",
		message: {
			role: "assistant",
			content: [
				{ type: "text", text: "working on it" },
				{ type: "tool_use", name: "Bash", id: "tu-1", input: { command: "ls" } },
			],
		},
	});
	const evs = parseStreamLine(line);
	check("assistant emits text+tool_use+turn", kinds(evs).join() === "text,tool_use,turn", kinds(evs).join());
	const text = evs.find((e) => e.kind === "text");
	check("assistant text content", text?.kind === "text" && text.text === "working on it");
	const tool = evs.find((e) => e.kind === "tool_use");
	check("assistant tool name", tool?.kind === "tool_use" && tool.name === "Bash");
}

// 6. assistant with empty text block is skipped but still yields a turn
{
	const line = JSON.stringify({ type: "assistant", message: { content: [{ type: "text", text: "" }] } });
	check("empty-text assistant still a turn", kinds(parseStreamLine(line)).join() === "turn");
}

// 7. assistant content as a bare string (collapsed form) → text + turn
{
	const line = JSON.stringify({ type: "assistant", message: { content: "hi" } });
	check("string-content assistant", kinds(parseStreamLine(line)).join() === "text,turn");
}

// 8. user tool_result with is_error flag
{
	const line = JSON.stringify({ type: "user", message: { content: [{ type: "tool_result", is_error: true, content: "boom" }] } });
	const evs = parseStreamLine(line);
	const tr = evs[0];
	check("tool_result kind", tr?.kind === "tool_result");
	check("tool_result isError", tr?.kind === "tool_result" && tr.isError === true);
}

// 9. result success line (live shape)
{
	const line = JSON.stringify({ type: "result", subtype: "success", is_error: false, result: "done", num_turns: 7, session_id: "sess-1" });
	const evs = parseStreamLine(line);
	const r = evs[0];
	check("result kind", r?.kind === "result");
	if (r?.kind === "result") {
		check("result text", r.text === "done");
		check("result not error", r.isError === false);
		check("result numTurns", r.numTurns === 7);
	}
}

// 10. result error variants flagged as error
{
	const a = parseStreamLine(JSON.stringify({ type: "result", subtype: "error_during_execution", result: "" }))[0];
	const b = parseStreamLine(JSON.stringify({ type: "result", subtype: "success", is_error: true, result: "" }))[0];
	check("result subtype error → isError", a?.kind === "result" && a.isError === true);
	check("result is_error flag → isError", b?.kind === "result" && b.isError === true);
}

// 11. unknown top-level type → noise
check("unknown type is noise", kinds(parseStreamLine(JSON.stringify({ type: "wat" }))).join() === "noise");

// 12. liftMcpServerNames handles array-of-strings, array-of-objects, object-map, and junk
check("mcp names from string array", liftMcpServerNames(["a", "b"]).join() === "a,b");
check("mcp names from object array", liftMcpServerNames([{ name: "ask_brain" }]).join() === "ask_brain");
check("mcp names from object map", liftMcpServerNames({ ask_brain: {}, x: {} }).sort().join() === "ask_brain,x");
check("mcp names from junk → empty", liftMcpServerNames(42).length === 0);

// 13. drainLines framing: holds a partial tail, yields complete lines across chunks
{
	const first = drainLines("", '{"a":1}\n{"b":2}\n{"c":');
	check("drain yields complete lines", first.lines.length === 2 && first.lines[0] === '{"a":1}');
	check("drain holds partial tail", first.rest === '{"c":');
	const second = drainLines(first.rest, '3}\n');
	check("drain completes across chunk boundary", second.lines.length === 1 && second.lines[0] === '{"c":3}');
}

console.log(`stream-json parser: ${pass} checks passed`);
if (fails.length) {
	console.log("FAILURES:");
	console.log(fails.join("\n"));
	process.exit(1);
}
console.log("ALL PASS ✓");
