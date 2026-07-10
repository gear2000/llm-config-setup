#!/usr/bin/env node
/**
 * ask-brain-server — the stdio MCP server the spawned `claude -p` runs as its ask_brain tool.
 *
 * This is the worker side of the back-channel. `claude -p` cannot listen on a socket, but it
 * CAN run a local MCP server over stdio (verified live: a hand-rolled stdio server registers
 * with status "connected" and its tools are callable). So we give the worker exactly one MCP
 * server — this one — via the generated --mcp-config (see mcp-config.ts). When the worker
 * calls the `ask_brain` tool, this server:
 *   1. opens the per-phase Unix socket (path in env META_ORCH_ASK_BRAIN_SOCKET),
 *   2. writes one `ask` frame (the brain is the server; we are the client — design doc 3a),
 *   3. BLOCKS reading the `answer` frame,
 *   4. returns the leader's guidance as the tool result (so the worker resumes on it).
 *
 * Plain ESM .mjs (no TypeScript, no npm deps) so the spawned `node` runs it directly with no
 * build step — it ships next to the extension and is launched by `node ask-brain-server.mjs`.
 * The MCP JSON-RPC framing is implemented by hand against the live-captured Claude handshake:
 *   initialize (protocolVersion 2025-11-25) → notifications/initialized → tools/list → tools/call.
 * We echo the client's protocolVersion rather than pin one, so a CLI upgrade does not break us.
 *
 * Fail-loud: a socket connect error, a closed connection before the answer, or a malformed
 * answer is reported back to the worker as a tool error (isError:true) — never a silent hang,
 * never a fabricated "ok". The worker's contract (mcp-config.ts) says: on a STOP answer, end
 * the phase; a tool error is treated the same — the worker cannot reach the brain, so it stops.
 */

import * as net from "node:net";

const SOCKET_PATH = process.env.META_ORCH_ASK_BRAIN_SOCKET;
const PHASE_ID = process.env.META_ORCH_PHASE_ID || "unknown-phase";
const MAX_HOPS = Number(process.env.META_ORCH_MAX_HOPS) || 5;
const LINE_CAP_BYTES = 64 * 1024;
const SEVERITIES = ["blocked", "decision", "progress", "heartbeat"];

if (!SOCKET_PATH) {
	// No socket wired in — the spawn is misconfigured. Fail loud at startup so the worker's
	// MCP connect surfaces the error instead of silently offering a dead tool.
	process.stderr.write("ask-brain-server: META_ORCH_ASK_BRAIN_SOCKET is not set\n");
	process.exit(1);
}

let counter = 0;
function nextId() {
	counter += 1;
	return `${PHASE_ID}-${Date.now().toString(36)}-${counter}`;
}

/** Read one newline-delimited frame from a socket, capped, as a promise. */
function readOneFrame(socket) {
	return new Promise((resolve, reject) => {
		let buf = "";
		let settled = false;
		const onData = (chunk) => {
			if (settled) return;
			buf += chunk.toString("utf-8");
			if (buf.length > LINE_CAP_BYTES) {
				settled = true;
				socket.removeListener("data", onData);
				reject(new Error("answer frame too large"));
				return;
			}
			const nl = buf.indexOf("\n");
			if (nl >= 0) {
				settled = true;
				socket.removeListener("data", onData);
				resolve(buf.slice(0, nl));
			}
		};
		socket.on("data", onData);
		socket.once("error", (err) => { if (!settled) { settled = true; reject(err); } });
		socket.once("close", () => { if (!settled) { settled = true; reject(new Error("brain closed the channel before answering")); } });
	});
}

/**
 * Send one ask to the brain and block for the answer. Resolves to the leader's
 * { answer, stop } or rejects on any transport/protocol failure (caller maps to isError).
 */
function askBrain(severity, message) {
	return new Promise((resolve, reject) => {
		const sock = net.createConnection({ path: SOCKET_PATH });
		let settled = false;
		const fail = (err) => {
			if (settled) return;
			settled = true;
			try { sock.destroy(); } catch { /* ignore */ }
			reject(err instanceof Error ? err : new Error(String(err)));
		};
		sock.once("error", fail);
		sock.once("connect", async () => {
			const ask = {
				type: "ask",
				id: nextId(),
				severity,
				message,
				hops: 0,
				phaseId: PHASE_ID,
			};
			try {
				sock.write(JSON.stringify(ask) + "\n");
				const line = await readOneFrame(sock);
				try { sock.end(); } catch { /* ignore */ }
				if (settled) return;
				settled = true;
				let parsed;
				try {
					parsed = JSON.parse(line);
				} catch (e) {
					reject(new Error(`brain answer was not JSON: ${e.message}`));
					return;
				}
				if (!parsed || parsed.type !== "answer" || typeof parsed.answer !== "string") {
					reject(new Error("brain answer frame was malformed"));
					return;
				}
				resolve({ answer: parsed.answer, stop: parsed.stop === true });
			} catch (err) {
				fail(err);
			}
		});
	});
}

// ── minimal stdio JSON-RPC (MCP) loop ──────────────────────────────────────────────────

function writeMessage(obj) {
	process.stdout.write(JSON.stringify(obj) + "\n");
}

function ok(id, result) {
	writeMessage({ jsonrpc: "2.0", id, result });
}

function rpcError(id, code, message) {
	writeMessage({ jsonrpc: "2.0", id, error: { code, message } });
}

const TOOL = {
	name: "ask_brain",
	description:
		"Reach the resident orchestration leader and BLOCK until it answers. Call with severity " +
		"(blocked|decision|progress|heartbeat) and a message. Returns the leader's guidance; if it " +
		"says STOP, end the phase. Also send a 'heartbeat' periodically so the leader can reach you.",
	inputSchema: {
		type: "object",
		properties: {
			severity: { type: "string", enum: SEVERITIES, description: "blocked | decision | progress | heartbeat" },
			message: { type: "string", description: "What you need from the leader, or your status." },
		},
		required: ["severity", "message"],
	},
};

async function handleToolCall(id, params) {
	const name = params && params.name;
	if (name !== "ask_brain") {
		rpcError(id, -32601, `unknown tool: ${name}`);
		return;
	}
	const args = (params && params.arguments) || {};
	const severity = SEVERITIES.includes(args.severity) ? args.severity : "decision";
	const message = typeof args.message === "string" ? args.message : "";
	if (!message) {
		ok(id, { content: [{ type: "text", text: "ask_brain error: message is required" }], isError: true });
		return;
	}
	try {
		const { answer, stop } = await askBrain(severity, message);
		const text = stop ? `STOP — ${answer}` : answer;
		ok(id, { content: [{ type: "text", text }], isError: false });
	} catch (err) {
		// Could not reach the brain. Tell the worker loudly; do not fabricate guidance.
		ok(id, {
			content: [{ type: "text", text: `ask_brain could not reach the leader: ${err.message}. Treat as STOP and end the phase.` }],
			isError: true,
		});
	}
}

function handleMessage(msg) {
	const { id, method, params } = msg;
	if (method === "initialize") {
		// Echo the client's protocolVersion (live-captured: claude-code sends 2025-11-25).
		const protocolVersion = (params && params.protocolVersion) || "2025-11-25";
		ok(id, {
			protocolVersion,
			capabilities: { tools: {} },
			serverInfo: { name: "meta-orchestrator-ask-brain", version: "1.0.0" },
		});
		return;
	}
	if (method === "tools/list") {
		ok(id, { tools: [TOOL] });
		return;
	}
	if (method === "tools/call") {
		// async — JSON-RPC allows out-of-order; the worker made one blocking tool call.
		handleToolCall(id, params).catch((err) => {
			rpcError(id, -32603, `ask_brain internal error: ${err.message}`);
		});
		return;
	}
	if (typeof method === "string" && method.startsWith("notifications/")) {
		return; // notifications get no response
	}
	if (method === "ping") {
		ok(id, {});
		return;
	}
	if (id !== undefined) {
		rpcError(id, -32601, `method not found: ${method}`);
	}
}

let stdinBuf = "";
process.stdin.setEncoding("utf-8");
process.stdin.on("data", (chunk) => {
	stdinBuf += chunk;
	let nl;
	while ((nl = stdinBuf.indexOf("\n")) >= 0) {
		const line = stdinBuf.slice(0, nl);
		stdinBuf = stdinBuf.slice(nl + 1);
		if (!line.trim()) continue;
		let msg;
		try {
			msg = JSON.parse(line);
		} catch {
			continue; // ignore a malformed JSON-RPC line; the client will time out its request
		}
		handleMessage(msg);
	}
});
process.stdin.on("end", () => process.exit(0));
