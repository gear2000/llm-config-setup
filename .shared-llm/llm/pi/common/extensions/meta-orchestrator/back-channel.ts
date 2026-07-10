/**
 * back-channel — the brain-side Unix-socket server the worker's ask_brain connects to.
 *
 * Design doc (3a): the brain is the SERVER that listens; the worker is a CLIENT that calls
 * out and blocks for the reply. This module binds one per-phase Unix socket, reads a single
 * `ask` frame per connection, hands the question to a leader-supplied resolver, and writes
 * the `answer` frame back on the SAME still-open connection — so the worker's ask_brain
 * tool stays blocked exactly until the leader answers, then unblocks with the guidance.
 *
 * Socket lifecycle (bind, stale-probe self-heal, chmod 0o600, shutdown) is copied from the
 * kit's coms.ts / hub-common.ts conventions — including the critical stale-socket heal: if
 * a `<id>.sock` is left behind by a crashed prior run, we probe it; ECONNREFUSED/ENOENT ⇒
 * stale ⇒ unlink and rebind; a live connection ⇒ refuse (another server owns it).
 *
 * The resolver is injected (not implemented here) because relaying the question up to the
 * leader TUI and awaiting the answer is the leader's job — see claude-proxy.ts, which wires
 * `resolveAsk` to pi.sendMessage(followUp) + a pending-answer promise the leader settles.
 */

import * as net from "node:net";
import * as fs from "node:fs";
import * as path from "node:path";
import {
	type AskEnvelope,
	type AnswerEnvelope,
	checkHopLimit,
	decodeFrame,
	encodeFrame,
	makeAnswer,
} from "./envelope.ts";

const LINE_CAP_BYTES = 64 * 1024;

/** What the leader supplies: take a worker's ask, return the leader's answer (may block). */
export type AskResolver = (ask: AskEnvelope) => Promise<AnswerEnvelope>;

export interface BackChannel {
	readonly socketPath: string;
	close(): Promise<void>;
}

/** Probe an existing socket file: is a server live on it, or is it a stale leftover? */
function probeStaleSocket(endpoint: string): Promise<"in_use" | "stale"> {
	return new Promise((resolve) => {
		const sock = net.createConnection({ path: endpoint });
		let settled = false;
		const finish = (verdict: "in_use" | "stale") => {
			if (settled) return;
			settled = true;
			try { sock.destroy(); } catch { /* ignore */ }
			resolve(verdict);
		};
		const timer = setTimeout(() => finish("stale"), 250);
		sock.once("connect", () => { clearTimeout(timer); finish("in_use"); });
		sock.once("error", () => { clearTimeout(timer); finish("stale"); });
	});
}

/** Read exactly one newline-delimited frame from a socket, capped to guard against a flood. */
function readOneFrame(socket: net.Socket): Promise<string> {
	return new Promise((resolve, reject) => {
		let buf = "";
		let settled = false;
		const onData = (chunk: Buffer) => {
			if (settled) return;
			buf += chunk.toString("utf-8");
			if (buf.length > LINE_CAP_BYTES) {
				settled = true;
				socket.removeListener("data", onData);
				reject(new Error("ask_brain frame too large"));
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
		socket.once("close", () => { if (!settled) { settled = true; reject(new Error("closed before frame")); } });
	});
}

/**
 * Remove a stale socket file if present, refusing only when a live server owns it. Exposed
 * (and used at bind time) so the leader can also pre-sweep a run's sockets dir on startup.
 */
export async function healStaleSocket(socketPath: string): Promise<void> {
	if (process.platform === "win32") return; // named pipes don't leave files
	if (!fs.existsSync(socketPath)) return;
	const verdict = await probeStaleSocket(socketPath);
	if (verdict === "in_use") {
		throw new Error(`ask_brain socket already in use: ${socketPath}`);
	}
	fs.unlinkSync(socketPath); // stale → reclaim
}

/**
 * Bind the per-phase back-channel and start serving asks. Each inbound connection carries
 * one ask; we enforce the hop limit, then call the resolver and write its answer back on
 * the same socket. A malformed or oversized frame gets a `stop` answer (fail-loud to the
 * worker, never a silent hang) and the connection closes.
 *
 * `onError` is invoked for per-connection faults (so the leader can log them) but a single
 * bad connection never tears down the server — the next phase's worker, and even a retry
 * within this phase, must still be able to call in.
 */
export async function startBackChannel(opts: {
	socketPath: string;
	resolve: AskResolver;
	maxHops: number;
	onError?: (err: Error) => void;
}): Promise<BackChannel> {
	await healStaleSocket(opts.socketPath);
	fs.mkdirSync(path.dirname(opts.socketPath), { recursive: true });

	const handleConnection = (socket: net.Socket): void => {
		readOneFrame(socket)
			.then(async (line) => {
				let env;
				try {
					env = decodeFrame(line);
				} catch (err) {
					writeAnswerAndClose(socket, makeAnswer("", `ask_brain protocol error: ${err instanceof Error ? err.message : String(err)}`, true));
					return;
				}
				if (env.type !== "ask") {
					writeAnswerAndClose(socket, makeAnswer((env as AnswerEnvelope).id ?? "", "ask_brain server received a non-ask frame", true));
					return;
				}
				const ask = env as AskEnvelope;

				const hopReject = checkHopLimit(ask, opts.maxHops);
				if (hopReject) {
					writeAnswerAndClose(socket, hopReject);
					return;
				}

				let answer: AnswerEnvelope;
				try {
					answer = await opts.resolve(ask);
				} catch (err) {
					// The leader's resolver failed (e.g. it could not deliver to the TUI). Tell the
					// worker to stop rather than leave it blocked forever — fail loud, both ends.
					answer = makeAnswer(ask.id, `leader could not answer: ${err instanceof Error ? err.message : String(err)}`, true);
					opts.onError?.(err instanceof Error ? err : new Error(String(err)));
				}
				writeAnswerAndClose(socket, answer);
			})
			.catch((err) => {
				try { socket.destroy(); } catch { /* ignore */ }
				opts.onError?.(err instanceof Error ? err : new Error(String(err)));
			});
	};

	const server = await new Promise<net.Server>((resolve, reject) => {
		const s = net.createServer(handleConnection);
		s.once("error", reject);
		s.listen(opts.socketPath, () => { s.removeListener("error", reject); resolve(s); });
	});

	try { fs.chmodSync(opts.socketPath, 0o600); } catch { /* best-effort: socket perms */ }

	let closed = false;
	return {
		socketPath: opts.socketPath,
		close(): Promise<void> {
			if (closed) return Promise.resolve();
			closed = true;
			return new Promise<void>((resolve) => {
				server.close(() => {
					if (process.platform !== "win32") {
						try { fs.unlinkSync(opts.socketPath); } catch { /* already gone */ }
					}
					resolve();
				});
			});
		},
	};
}

function writeAnswerAndClose(socket: net.Socket, answer: AnswerEnvelope): void {
	try {
		socket.write(encodeFrame(answer));
	} catch {
		// If the worker already dropped the connection there is nothing to answer; the
		// worker-side ask_brain treats a closed socket as a hard error and stops.
	}
	try { socket.end(); } catch { /* ignore */ }
}
