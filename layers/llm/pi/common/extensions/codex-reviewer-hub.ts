/**
 * codex-reviewer-hub — Unix socket dispatch for adversarial code review
 *
 * Listens on a Unix socket. Receives JSON dispatch requests from Claude Code
 * (or any local caller, e.g. the iac-guard gate). Spawns the requested sub-agent
 * via pi-subagents — defaults to codex-reviewer; pass "agent" to run another
 * (e.g. iac-verifier). Writes error content to the output path if the sub-agent
 * fails or times out.
 *
 * Usage: pi -e ~/.pi/extensions/codex-reviewer-hub.ts
 *   or:  pi -e ~/.pi/extensions/codex-reviewer-hub.ts --socket /custom/path.sock
 */

import type { ExtensionAPI, ExtensionContext } from "@mariozechner/pi-coding-agent";
import * as net from "node:net";
import * as fs from "node:fs";
import * as path from "node:path";
import * as os from "node:os";
import * as crypto from "node:crypto";

const DEFAULT_SOCKET = path.join(os.homedir(), ".pi", "codex-reviewer.sock");
const DEFAULT_TIMEOUT_MS = 600_000; // 10 minutes
const LINE_CAP_BYTES = 64 * 1024;

interface DispatchRequest {
  handoff: string;
  output: string;
  timeout_ms?: number;
  agent?: string; // which sub-agent to run (default: codex-reviewer). Lets one socket serve iac-verifier etc.
}

interface DispatchResponse {
  status: "dispatched" | "error";
  request_id: string;
  error?: string;
}

function generateId(): string {
  return crypto.randomBytes(12).toString("hex");
}

function writeError(outputPath: string, reason: string, requestId: string): void {
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

function readOneLine(socket: net.Socket): Promise<string> {
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
    sock.once("connect", () => {
      clearTimeout(timer);
      finish("in_use");
    });
    sock.once("error", (err: any) => {
      clearTimeout(timer);
      if (err && err.code === "ECONNREFUSED") {
        finish("stale");
      } else {
        finish("stale");
      }
    });
  });
}

export default function (pi: ExtensionAPI) {
  pi.registerFlag("socket", {
    description: "Unix socket path to listen on",
    type: "string",
    default: DEFAULT_SOCKET,
  });

  let server: net.Server | null = null;
  let socketPath: string = DEFAULT_SOCKET;
  const fallbackTimers: Set<NodeJS.Timeout> = new Set();

  async function dispatch(req: DispatchRequest, requestId: string): Promise<void> {
    const timeoutMs = req.timeout_ms ?? DEFAULT_TIMEOUT_MS;

    try {
      if (!fs.existsSync(req.handoff)) {
        writeError(req.output, `handoff file not found: ${req.handoff}`, requestId);
        return;
      }

      const agent = req.agent ?? "codex-reviewer";
      const task =
        agent === "codex-reviewer"
          ? [
              `Read the handoff at ${req.handoff}.`,
              `Perform an adversarial review of the artifacts it references.`,
              `Write your review to ${req.output}.`,
            ].join(" ")
          : [
              `Read the handoff at ${req.handoff}.`,
              `Follow its instructions and your agent system prompt.`,
              `Write your output to ${req.output}.`,
            ].join(" ");

      pi.sendMessage(
        {
          customType: "codex-dispatch",
          content: [
            `[dispatch request_id=${requestId}]`,
            `Use the subagent tool to run the ${agent} agent:`,
            `subagent({ agent: "${agent}", task: "${task.replace(/"/g, '\\"')}", async: true })`,
          ].join("\n"),
          display: true,
        },
        { deliverAs: "followUp", triggerTurn: true },
      );

      const fallbackTimer = setTimeout(() => {
        fallbackTimers.delete(fallbackTimer);
        if (!fs.existsSync(req.output)) {
          writeError(req.output, `timeout after ${Math.round(timeoutMs / 1000)}s`, requestId);
        }
      }, timeoutMs + 30_000);
      try { (fallbackTimer as any).unref?.(); } catch { /* ignore */ }
      fallbackTimers.add(fallbackTimer);

    } catch (err) {
      writeError(
        req.output,
        err instanceof Error ? err.message : String(err),
        requestId,
      );
    }
  }

  function handleConnection(socket: net.Socket): void {
    readOneLine(socket)
      .then((line) => {
        let parsed: any;
        try {
          parsed = JSON.parse(line);
        } catch {
          const resp: DispatchResponse = {
            status: "error",
            request_id: "",
            error: "malformed JSON",
          };
          socket.write(JSON.stringify(resp) + "\n");
          socket.end();
          return;
        }

        const requestId = generateId();

        if (!parsed.handoff || !parsed.output) {
          const resp: DispatchResponse = {
            status: "error",
            request_id: requestId,
            error: "missing handoff or output field",
          };
          socket.write(JSON.stringify(resp) + "\n");
          socket.end();
          return;
        }

        const resp: DispatchResponse = {
          status: "dispatched",
          request_id: requestId,
        };
        socket.write(JSON.stringify(resp) + "\n");
        socket.end();

        try {
          pi.appendEntry("codex-hub-log", {
            event: "dispatch",
            request_id: requestId,
            handoff: parsed.handoff,
            output: parsed.output,
          });
        } catch { /* best-effort */ }

        dispatch(parsed as DispatchRequest, requestId).catch(() => {});
      })
      .catch(() => {
        try { socket.destroy(); } catch { /* ignore */ }
      });
  }

  pi.on("session_start", async (_event, extCtx) => {
    socketPath = (pi.getFlag("socket") as string) || DEFAULT_SOCKET;

    const dir = path.dirname(socketPath);
    try {
      fs.mkdirSync(dir, { recursive: true });
    } catch { /* best-effort */ }

    if (fs.existsSync(socketPath)) {
      const verdict = await probeStaleSocket(socketPath);
      if (verdict === "in_use") {
        extCtx.ui?.notify?.(
          `codex-hub: socket ${socketPath} is already in use — another hub is running`,
          "error",
        );
        return;
      }
      try { fs.unlinkSync(socketPath); } catch { /* ignore */ }
    }

    server = net.createServer(handleConnection);
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
        `codex-hub: bind failed — ${err instanceof Error ? err.message : String(err)}`,
        "error",
      );
      return;
    }

    try { fs.chmodSync(socketPath, 0o600); } catch { /* best-effort */ }

    extCtx.ui?.notify?.(`codex-hub: listening on ${socketPath}`, "info");
    try { extCtx.ui.setStatus("codex-hub", `listening on ${socketPath}`); } catch { /* ignore */ }

    try {
      pi.appendEntry("codex-hub-log", { event: "started", socket: socketPath });
    } catch { /* best-effort */ }
  });

  let shuttingDown = false;
  async function shutdown(): Promise<void> {
    if (shuttingDown) return;
    shuttingDown = true;
    for (const t of fallbackTimers) {
      try { clearTimeout(t); } catch { /* ignore */ }
    }
    fallbackTimers.clear();
    if (server) {
      try { server.close(); } catch { /* ignore */ }
      server = null;
    }
    try { fs.unlinkSync(socketPath); } catch { /* ignore */ }
    try {
      pi.appendEntry("codex-hub-log", { event: "shutdown" });
    } catch { /* best-effort */ }
  }

  pi.on("session_shutdown", async () => { await shutdown(); });
  process.on("SIGINT", () => { void shutdown(); });
  process.on("SIGTERM", () => { void shutdown(); });
}
