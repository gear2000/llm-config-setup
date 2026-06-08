/**
 * hub-common — shared Unix-socket plumbing for the Pi review hubs.
 *
 * Imported by doc-review-hub.ts and pr-review-hub.ts. It owns the socket
 * lifecycle (bind, stale-probe, chmod, shutdown) and the one-line JSON protocol;
 * each hub supplies its own per-connection handler, because the request schema
 * and dispatch differ between document reviews and PR reviews.
 *
 * The legacy codex-reviewer-hub.ts (now the iac-guard hub) is intentionally NOT
 * refactored onto this helper — it stays standalone so the IaC approval gate is
 * zero-risk.
 */

import type { ExtensionAPI } from "@mariozechner/pi-coding-agent";
import * as net from "node:net";
import * as fs from "node:fs";
import * as path from "node:path";
import * as crypto from "node:crypto";

export const LINE_CAP_BYTES = 64 * 1024;

export function generateId(): string {
  return crypto.randomBytes(12).toString("hex");
}

export function writeError(outputPath: string, reason: string, requestId: string): void {
  const dir = path.dirname(outputPath);
  try {
    fs.mkdirSync(dir, { recursive: true });
  } catch { /* best-effort */ }
  const content = [
    "# Review Failed",
    `Status: ERROR`,
    `Reason: ${reason}`,
    `Request ID: ${requestId}`,
    `Timestamp: ${new Date().toISOString()}`,
  ].join("\n");
  try {
    fs.writeFileSync(outputPath, content);
  } catch { /* best-effort */ }
}

export function readOneLine(socket: net.Socket): Promise<string> {
  return new Promise((resolve, reject) => {
    let buf = "";
    let settled = false;
    const onData = (chunk: Buffer) => {
      if (settled) return;
      buf += chunk.toString("utf-8");
      if (buf.length > LINE_CAP_BYTES) {
        settled = true;
        socket.removeListener("data", onData);
        reject(new Error("line too large"));
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
    socket.once("error", (err) => {
      if (!settled) { settled = true; reject(err); }
    });
    socket.once("close", () => {
      if (!settled) { settled = true; reject(new Error("closed")); }
    });
  });
}

export function probeStaleSocket(endpoint: string): Promise<"in_use" | "stale"> {
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
    sock.once("connect", () => {
      clearTimeout(timer);
      finish("in_use");
    });
    sock.once("error", () => {
      clearTimeout(timer);
      finish("stale");
    });
  });
}

/**
 * Accept the old field names from pre-split callers, mapping them onto the new
 * ones in place. The new names win when both are present. Note the unit change:
 * the old timeout_ms is milliseconds, the new timeout_seconds is seconds.
 */
export function normalizeAliases(parsed: any): any {
  if (parsed == null || typeof parsed !== "object") return parsed;
  if (parsed.handoff_path == null && parsed.handoff != null) parsed.handoff_path = parsed.handoff;
  if (parsed.output_path == null && parsed.output != null) parsed.output_path = parsed.output;
  if (parsed.timeout_seconds == null && parsed.timeout_ms != null) {
    const ms = Number(parsed.timeout_ms);
    if (Number.isFinite(ms)) parsed.timeout_seconds = Math.round(ms / 1000);
  }
  return parsed;
}

export interface SocketServerOptions {
  pi: ExtensionAPI;
  defaultSocket: string;
  hubLabel: string;
  logChannel: string;
  onConnection: (socket: net.Socket) => void;
  onShutdown?: () => void;
}

/**
 * Wire up the socket lifecycle for a hub: register the --socket flag, bind on
 * session_start (clearing a stale socket first), chmod 0o600, and tear down on
 * shutdown. The hub passes its own onConnection; this helper never inspects the
 * request payload.
 */
export function mountSocketServer(opts: SocketServerOptions): void {
  const { pi, defaultSocket, hubLabel, logChannel, onConnection, onShutdown } = opts;

  pi.registerFlag("socket", {
    description: "Unix socket path to listen on",
    type: "string",
    default: defaultSocket,
  });

  let server: net.Server | null = null;
  let socketPath: string = defaultSocket;

  pi.on("session_start", async (_event, extCtx) => {
    socketPath = (pi.getFlag("socket") as string) || defaultSocket;

    const dir = path.dirname(socketPath);
    try { fs.mkdirSync(dir, { recursive: true }); } catch { /* best-effort */ }

    if (fs.existsSync(socketPath)) {
      const verdict = await probeStaleSocket(socketPath);
      if (verdict === "in_use") {
        extCtx.ui?.notify?.(
          `${hubLabel}: socket ${socketPath} is already in use — another hub is running`,
          "error",
        );
        return;
      }
      try { fs.unlinkSync(socketPath); } catch { /* ignore */ }
    }

    server = net.createServer(onConnection);
    try {
      await new Promise<void>((resolve, reject) => {
        server!.once("error", reject);
        server!.listen(socketPath, () => {
          server!.removeListener("error", reject);
          resolve();
        });
      });
    } catch (err) {
      extCtx.ui?.notify?.(
        `${hubLabel}: bind failed — ${err instanceof Error ? err.message : String(err)}`,
        "error",
      );
      return;
    }

    try { fs.chmodSync(socketPath, 0o600); } catch { /* best-effort */ }

    extCtx.ui?.notify?.(`${hubLabel}: listening on ${socketPath}`, "info");
    try { extCtx.ui.setStatus(hubLabel, `listening on ${socketPath}`); } catch { /* ignore */ }
    try { pi.appendEntry(logChannel, { event: "started", socket: socketPath }); } catch { /* best-effort */ }
  });

  let shuttingDown = false;
  function shutdown(): void {
    if (shuttingDown) return;
    shuttingDown = true;
    try { onShutdown?.(); } catch { /* ignore */ }
    if (server) {
      try { server.close(); } catch { /* ignore */ }
      server = null;
    }
    try { fs.unlinkSync(socketPath); } catch { /* ignore */ }
    try { pi.appendEntry(logChannel, { event: "shutdown" }); } catch { /* best-effort */ }
  }

  pi.on("session_shutdown", async () => { shutdown(); });
  process.on("SIGINT", () => { shutdown(); });
  process.on("SIGTERM", () => { shutdown(); });
}
