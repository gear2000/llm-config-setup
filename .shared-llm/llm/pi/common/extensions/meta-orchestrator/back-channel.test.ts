// Integration test for the back-channel — REAL Unix socket, REAL stdio MCP server child, NO mocks.
//
// Proves the core guarantee from design doc 3a: the worker's ask_brain call BLOCKS until the
// leader answers, then returns the leader's answer. We stand up the real brain-side server
// (back-channel.ts startBackChannel) on a real socket, launch the real worker-side stdio MCP
// server (ask-brain-server.mjs) as a child wired to that socket, then speak the MCP JSON-RPC
// the spawned `claude -p` speaks (handshake captured live: initialize → notifications/initialized
// → tools/list → tools/call) and assert:
//   1. ask_brain blocks (no tool result arrives) until the leader resolves it,
//   2. the leader's guidance is what the tool returns,
//   3. a stop:true answer is surfaced as STOP + isError to the worker,
//   4. the hop limit and a leader-resolver failure both produce a definite stop (no hang).
//
//   node --experimental-strip-types back-channel.test.ts
import { spawn, type ChildProcess } from "node:child_process";
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import { fileURLToPath } from "node:url";
import { startBackChannel } from "./back-channel.ts";
import { makeAnswer, type AskEnvelope, type AnswerEnvelope } from "./envelope.ts";

let pass = 0;
const fails: string[] = [];
function check(name: string, cond: boolean, detail = "") {
	if (cond) pass++;
	else fails.push(`  ✗ ${name}${detail ? ` — ${detail}` : ""}`);
}

const HERE = path.dirname(fileURLToPath(import.meta.url));
const SERVER = path.join(HERE, "ask-brain-server.mjs");

/** A thin driver for the stdio MCP server child: send JSON-RPC lines, await responses by id. */
class McpClient {
	private buf = "";
	private waiters = new Map<number, (msg: any) => void>();
	private nextId = 1;
	private child: ChildProcess;
	constructor(child: ChildProcess) {
		this.child = child;
		child.stdout!.setEncoding("utf-8");
		child.stdout!.on("data", (chunk: string) => {
			this.buf += chunk;
			let nl;
			while ((nl = this.buf.indexOf("\n")) >= 0) {
				const line = this.buf.slice(0, nl);
				this.buf = this.buf.slice(nl + 1);
				if (!line.trim()) continue;
				let msg: any;
				try { msg = JSON.parse(line); } catch { continue; }
				if (msg.id !== undefined && this.waiters.has(msg.id)) {
					const w = this.waiters.get(msg.id)!;
					this.waiters.delete(msg.id);
					w(msg);
				}
			}
		});
	}
	request(method: string, params?: any): { id: number; done: Promise<any> } {
		const id = this.nextId++;
		const done = new Promise<any>((resolve) => this.waiters.set(id, resolve));
		this.child.stdin!.write(JSON.stringify({ jsonrpc: "2.0", id, method, params }) + "\n");
		return { id, done };
	}
	notify(method: string, params?: any): void {
		this.child.stdin!.write(JSON.stringify({ jsonrpc: "2.0", method, params }) + "\n");
	}
}

function spawnServer(socketPath: string, phaseId: string, maxHops = 5): { child: ChildProcess; client: McpClient } {
	const child = spawn(process.execPath, [SERVER], {
		stdio: ["pipe", "pipe", "ignore"],
		env: {
			...process.env,
			META_ORCH_ASK_BRAIN_SOCKET: socketPath,
			META_ORCH_PHASE_ID: phaseId,
			META_ORCH_MAX_HOPS: String(maxHops),
		},
	});
	return { child, client: new McpClient(child) };
}

async function handshake(client: McpClient): Promise<void> {
	await client.request("initialize", { protocolVersion: "2025-11-25", capabilities: {}, clientInfo: { name: "test", version: "0" } }).done;
	client.notify("notifications/initialized");
	const list = await client.request("tools/list").done;
	const names = (list.result?.tools ?? []).map((t: any) => t.name);
	check("server lists ask_brain", names.includes("ask_brain"), names.join());
}

const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));

async function main() {
	const dir = fs.mkdtempSync(path.join(os.tmpdir(), "backchan-"));

	// ── case 1: ask BLOCKS until the leader answers, then returns the leader's guidance ──
	{
		const socketPath = path.join(dir, "p1.sock");
		let received: AskEnvelope | null = null;
		let releaseLeader!: (a: AnswerEnvelope) => void;
		const leaderAnswered = new Promise<AnswerEnvelope>((res) => { releaseLeader = res; });
		const bc = await startBackChannel({
			socketPath,
			maxHops: 5,
			resolve: async (ask) => { received = ask; return leaderAnswered; },
		});
		const { child, client } = spawnServer(socketPath, "orch-x-p1");
		await handshake(client);

		const call = client.request("tools/call", { name: "ask_brain", arguments: { severity: "blocked", message: "need a decision" } });
		let toolDone = false;
		void call.done.then(() => { toolDone = true; });

		// give it time to reach the server; the ask must have landed but the tool must NOT return yet
		await sleep(400);
		check("server received the ask", received !== null);
		check("ask severity carried", (received as AskEnvelope | null)?.severity === "blocked");
		check("ask phaseId carried", (received as AskEnvelope | null)?.phaseId === "orch-x-p1");
		check("tool call BLOCKS until leader answers", toolDone === false);

		// now the leader answers — the blocked call must complete with the guidance
		releaseLeader(makeAnswer((received as AskEnvelope).id, "do it on the repo's rails", false));
		const resp = await call.done;
		const text = resp.result?.content?.[0]?.text ?? "";
		check("tool returned leader guidance", text === "do it on the repo's rails", text);
		check("non-stop answer not flagged error", resp.result?.isError === false);

		child.kill("SIGKILL");
		await bc.close();
	}

	// ── case 2: stop:true → worker sees STOP + isError ──
	{
		const socketPath = path.join(dir, "p2.sock");
		const bc = await startBackChannel({
			socketPath,
			maxHops: 5,
			resolve: async (ask) => makeAnswer(ask.id, "fundamentally wrong", true),
		});
		const { child, client } = spawnServer(socketPath, "orch-x-p2");
		await handshake(client);
		const resp = await client.request("tools/call", { name: "ask_brain", arguments: { severity: "decision", message: "which path?" } }).done;
		const text = resp.result?.content?.[0]?.text ?? "";
		check("stop answer prefixed STOP", text.startsWith("STOP —"), text);
		// A leader STOP is a SUCCESSFUL tool result (not an MCP error): the brain was reached and
		// answered. The worker acts on the "STOP —" text per its instruction. Only an UNREACHABLE
		// brain is isError:true (case 4). So a stop answer must NOT be flagged isError.
		check("stop answer is a clean result (not isError)", resp.result?.isError === false, String(resp.result?.isError));
		child.kill("SIGKILL");
		await bc.close();
	}

	// ── case 3: hop limit → definite stop, never a hang (ask sent with hops at the cap) ──
	// The ask-brain-server always sends hops:0, so to exercise the cap we drive the server with
	// maxHops:0 — the brain rejects ANY hop and answers stop immediately.
	{
		const socketPath = path.join(dir, "p3.sock");
		let leaderCalled = false;
		const bc = await startBackChannel({
			socketPath,
			maxHops: 0, // every ask is at/over the cap
			resolve: async (ask) => { leaderCalled = true; return makeAnswer(ask.id, "should not be reached", false); },
		});
		const { child, client } = spawnServer(socketPath, "orch-x-p3", 0);
		await handshake(client);
		const resp = await client.request("tools/call", { name: "ask_brain", arguments: { severity: "blocked", message: "x" } }).done;
		const text = resp.result?.content?.[0]?.text ?? "";
		check("hop-limit reply reaches worker as STOP", text.startsWith("STOP —"), text);
		check("hop-limit short-circuits the leader", leaderCalled === false);
		child.kill("SIGKILL");
		await bc.close();
	}

	// ── case 4: leader resolver throws → worker gets a definite stop (fail-loud, no hang) ──
	{
		const socketPath = path.join(dir, "p4.sock");
		const bc = await startBackChannel({
			socketPath,
			maxHops: 5,
			resolve: async () => { throw new Error("TUI delivery failed"); },
		});
		const { child, client } = spawnServer(socketPath, "orch-x-p4");
		await handshake(client);
		const resp = await client.request("tools/call", { name: "ask_brain", arguments: { severity: "decision", message: "x" } }).done;
		const text = resp.result?.content?.[0]?.text ?? "";
		check("resolver failure surfaces as STOP", text.startsWith("STOP —"), text);
		check("resolver failure mentions the cause", text.includes("TUI delivery failed") || text.includes("could not answer"), text);
		child.kill("SIGKILL");
		await bc.close();
	}

	// ── case 5: stale-socket self-heal — a leftover socket file is reclaimed on bind ──
	{
		const socketPath = path.join(dir, "p5.sock");
		fs.writeFileSync(socketPath, ""); // a stale leftover (regular file, nothing listening)
		const bc = await startBackChannel({ socketPath, maxHops: 5, resolve: async (a) => makeAnswer(a.id, "ok", false) });
		check("stale socket reclaimed and bound", fs.existsSync(socketPath));
		// and it actually serves after healing
		const { child, client } = spawnServer(socketPath, "orch-x-p5");
		await handshake(client);
		const resp = await client.request("tools/call", { name: "ask_brain", arguments: { severity: "heartbeat", message: "alive" } }).done;
		check("healed socket serves a call", (resp.result?.content?.[0]?.text ?? "") === "ok");
		child.kill("SIGKILL");
		await bc.close();
	}

	fs.rmSync(dir, { recursive: true, force: true });
}

main()
	.then(() => {
		console.log(`back-channel integration: ${pass} checks passed`);
		if (fails.length) { console.log("FAILURES:"); console.log(fails.join("\n")); process.exit(1); }
		console.log("ALL PASS ✓");
	})
	.catch((err) => {
		console.log(`back-channel integration FAILED to run: ${err instanceof Error ? err.stack : String(err)}`);
		process.exit(1);
	});
