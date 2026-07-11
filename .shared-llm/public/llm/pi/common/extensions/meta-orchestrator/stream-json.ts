/**
 * stream-json — parse the event stream emitted by `claude -p --output-format stream-json`.
 *
 * THIS IS THE BRIDGE. The disler subagent-widget reads Pi's own json stream, whose
 * envelopes look like `{type:"message_update", assistantMessageEvent:{type:"text_delta",…}}`
 * and `{type:"tool_execution_start"}`. The Claude CLI emits a DIFFERENT shape — verified
 * live against `claude -p --output-format stream-json --verbose`:
 *
 *   {"type":"system","subtype":"init", model, tools, mcp_servers, cwd, session_id, …}
 *   {"type":"system","subtype":"hook_started"|"hook_response", …}              ← hook noise
 *   {"type":"assistant","message":{role,content:[{type:"text",text}|{type:"tool_use",name,id,input}],usage,…}}
 *   {"type":"user","message":{content:[{type:"tool_result",…}]}}              ← tool results fed back in
 *   {"type":"result","subtype":"success"|…,"is_error":bool,"result":"…","num_turns",session_id,…}
 *
 * This module owns ALL of that envelope knowledge so nothing downstream has to. It is a
 * pure, runtime-free line→event normaliser, which is why it is unit-testable in isolation
 * (parsing is the part most likely to drift when the CLI's schema changes).
 *
 * Deep-module contract: callers see only `parseStreamLine` and the small `ClaudeEvent`
 * union — never the raw `message.content[]` block shapes. A change to Claude's envelope
 * is absorbed here.
 */

/** A normalised event lifted out of one stream-json line. */
export type ClaudeEvent =
	| { kind: "init"; sessionId: string; model: string | null; mcpServers: string[]; tools: string[] }
	| { kind: "text"; text: string }
	| { kind: "tool_use"; name: string; id: string }
	| { kind: "tool_result"; isError: boolean }
	| { kind: "turn" } // one assistant message landed — used by the turn-cap guardrail
	| { kind: "result"; isError: boolean; text: string; numTurns: number | null; sessionId: string }
	| { kind: "noise" }; // hooks / unknown system lines / blank — carry no progress signal

interface RawContentBlock {
	type?: unknown;
	text?: unknown;
	name?: unknown;
	id?: unknown;
}

function asString(v: unknown): string {
	return typeof v === "string" ? v : "";
}

function asStringArray(v: unknown): string[] {
	if (!Array.isArray(v)) return [];
	return v.filter((x): x is string => typeof x === "string");
}

/**
 * Lift the content blocks of an assistant message into discrete events. Claude streams a
 * whole assistant message per `{"type":"assistant"}` line (not per-token deltas like Pi),
 * so one line can carry several blocks: any number of text + tool_use blocks. We emit one
 * event per block plus a single `turn` event marking that an assistant message completed.
 */
function liftAssistantBlocks(content: unknown): ClaudeEvent[] {
	const out: ClaudeEvent[] = [];
	if (Array.isArray(content)) {
		for (const raw of content) {
			const block = raw as RawContentBlock;
			if (block.type === "text") {
				const text = asString(block.text);
				if (text) out.push({ kind: "text", text });
			} else if (block.type === "tool_use") {
				out.push({ kind: "tool_use", name: asString(block.name) || "tool", id: asString(block.id) });
			}
		}
	} else if (typeof content === "string" && content) {
		// Some CLI builds collapse a text-only message to a bare string.
		out.push({ kind: "text", text: content });
	}
	out.push({ kind: "turn" });
	return out;
}

function liftUserBlocks(content: unknown): ClaudeEvent[] {
	const out: ClaudeEvent[] = [];
	if (Array.isArray(content)) {
		for (const raw of content) {
			const block = raw as RawContentBlock;
			if (block.type === "tool_result") {
				// tool_result carries an `is_error` flag in Claude's schema; default to false.
				const isError = (raw as { is_error?: unknown }).is_error === true;
				out.push({ kind: "tool_result", isError });
			}
		}
	}
	return out;
}

/**
 * Parse exactly one newline-delimited stream-json line into zero or more `ClaudeEvent`s.
 *
 * Returns `[]` for a blank line. Returns `[{kind:"noise"}]` for a malformed line (the
 * caller logs the raw bytes; we never throw on a single bad line because the stream is a
 * best-effort progress feed, not a contract surface — the authoritative completion signal
 * is the `result` event, and a dropped progress line cannot corrupt that). A line that is
 * valid JSON but an unrecognised type also normalises to `noise`.
 *
 * One line can yield several events (an assistant message with multiple content blocks),
 * which is why the return type is an array rather than a single event.
 */
export function parseStreamLine(line: string): ClaudeEvent[] {
	const trimmed = line.trim();
	if (!trimmed) return [];

	let obj: Record<string, unknown>;
	try {
		obj = JSON.parse(trimmed) as Record<string, unknown>;
	} catch {
		return [{ kind: "noise" }];
	}
	if (!obj || typeof obj !== "object") return [{ kind: "noise" }];

	const type = obj.type;

	if (type === "system") {
		if (obj.subtype === "init") {
			return [{
				kind: "init",
				sessionId: asString(obj.session_id),
				model: typeof obj.model === "string" ? obj.model : null,
				mcpServers: liftMcpServerNames(obj.mcp_servers),
				tools: asStringArray(obj.tools),
			}];
		}
		// hook_started / hook_response / anything else system-level → no progress signal.
		return [{ kind: "noise" }];
	}

	if (type === "assistant") {
		const message = obj.message as { content?: unknown } | undefined;
		return liftAssistantBlocks(message?.content);
	}

	if (type === "user") {
		const message = obj.message as { content?: unknown } | undefined;
		return liftUserBlocks(message?.content);
	}

	if (type === "result") {
		return [{
			kind: "result",
			isError: obj.is_error === true || obj.subtype === "error" || obj.subtype === "error_during_execution",
			text: asString(obj.result),
			numTurns: typeof obj.num_turns === "number" ? obj.num_turns : null,
			sessionId: asString(obj.session_id),
		}];
	}

	return [{ kind: "noise" }];
}

/**
 * The init line reports configured MCP servers either as an array of names/objects or as
 * an object keyed by server name. We only need the NAMES, so the orchestrator can assert
 * that the `ask_brain` server we generated actually registered (a real, non-stubbed check
 * that the --mcp-config was honoured — see mcp-config.ts ASK_BRAIN_SERVER_NAME).
 */
export function liftMcpServerNames(raw: unknown): string[] {
	if (Array.isArray(raw)) {
		const names: string[] = [];
		for (const entry of raw) {
			if (typeof entry === "string") names.push(entry);
			else if (entry && typeof entry === "object" && typeof (entry as { name?: unknown }).name === "string") {
				names.push((entry as { name: string }).name);
			}
		}
		return names;
	}
	if (raw && typeof raw === "object") {
		return Object.keys(raw as Record<string, unknown>);
	}
	return [];
}

/**
 * A line-buffered splitter for a byte stream. The child's stdout arrives in arbitrary
 * chunks; this accumulates and yields only complete lines, holding the partial tail for
 * the next chunk. Returns the completed lines and the new residual buffer. Kept pure (no
 * stream object) so the framing logic is unit-testable without a live process.
 */
export function drainLines(buffer: string, chunk: string): { lines: string[]; rest: string } {
	const combined = buffer + chunk;
	const parts = combined.split("\n");
	const rest = parts.pop() ?? "";
	return { lines: parts, rest };
}
