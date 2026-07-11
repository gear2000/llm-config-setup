/**
 * doc-review-hub — Unix socket dispatch for adversarial DOCUMENT review.
 *
 * Listens on ~/.pi/doc-reviewer.sock. Accepts one JSON line per connection,
 * dispatches the doc-reviewer subagent, replies immediately. For fixed artifacts
 * only — research docs, plans, PRDs, ADRs, handoffs.
 *
 * Request (old field names handoff/output/timeout_ms are also accepted):
 *   {
 *     "request_id": "caller attempt id",
 *     "doc_type": "research|plan|prd|adr|handoff|other",
 *     "handoff_path": "/abs/path/to/handoff.md",
 *     "output_path": "/abs/path/to/review.md",
 *     "timeout_seconds": 600
 *   }
 *
 * Usage: pi -e ~/.pi/extensions/doc-review-hub.ts
 *   or:  pi -e ~/.pi/extensions/doc-review-hub.ts --socket /custom/path.sock
 */

import type { ExtensionAPI } from "@mariozechner/pi-coding-agent";
import * as net from "node:net";
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import {
  generateId,
  writeError,
  readOneLine,
  normalizeAliases,
  mountSocketServer,
} from "./hub-common";

const DEFAULT_SOCKET = path.join(os.homedir(), ".pi", "doc-reviewer.sock");
const DEFAULT_TIMEOUT_SECONDS = 600;
const AGENT = "doc-reviewer";
const LOG = "doc-review-hub-log";
const DOC_TYPES = ["research", "plan", "prd", "adr", "handoff", "other"];

export default function (pi: ExtensionAPI) {
  const fallbackTimers = new Set<NodeJS.Timeout>();

  function dispatch(req: any, requestId: string): void {
    const timeoutSeconds = Number(req.timeout_seconds) || DEFAULT_TIMEOUT_SECONDS;

    const task = [
      `Read the handoff at ${req.handoff_path}.`,
      `This is a document review of type "${req.doc_type}".`,
      `Review the fixed artifacts the handoff references (research docs, plans, PRDs, ADRs, handoffs).`,
      `Do not modify any project or source files — only write your review findings to ${req.output_path}.`,
    ].join(" ");

    pi.sendMessage(
      {
        customType: "doc-review-dispatch",
        content: [
          `[doc-review request_id=${requestId}]`,
          `Use the subagent tool to run the ${AGENT} agent:`,
          `subagent({ agent: "${AGENT}", task: "${task.replace(/"/g, '\\"')}", async: true })`,
        ].join("\n"),
        display: true,
      },
      { deliverAs: "followUp", triggerTurn: true },
    );

    const fallbackTimer = setTimeout(() => {
      fallbackTimers.delete(fallbackTimer);
      if (!fs.existsSync(req.output_path)) {
        writeError(req.output_path, `timeout after ${timeoutSeconds}s`, requestId);
      }
    }, timeoutSeconds * 1000 + 30_000);
    try { (fallbackTimer as any).unref?.(); } catch { /* ignore */ }
    fallbackTimers.add(fallbackTimer);
  }

  function handleConnection(socket: net.Socket): void {
    readOneLine(socket)
      .then((line) => {
        let parsed: any;
        try {
          parsed = normalizeAliases(JSON.parse(line));
        } catch {
          socket.write(JSON.stringify({ status: "error", request_id: "", error: "malformed JSON" }) + "\n");
          socket.end();
          return;
        }

        // request_id is the caller's attempt id; fall back to a generated one so
        // pre-split callers (which never sent it) still work.
        const requestId = (typeof parsed.request_id === "string" && parsed.request_id) ? parsed.request_id : generateId();

        const missing: string[] = [];
        if (!parsed.doc_type) missing.push("doc_type");
        if (!parsed.handoff_path) missing.push("handoff_path");
        if (!parsed.output_path) missing.push("output_path");
        if (missing.length) {
          socket.write(JSON.stringify({ status: "error", request_id: requestId, error: `missing field(s): ${missing.join(", ")}` }) + "\n");
          socket.end();
          return;
        }
        if (!DOC_TYPES.includes(String(parsed.doc_type))) {
          socket.write(JSON.stringify({ status: "error", request_id: requestId, error: `invalid doc_type: ${parsed.doc_type}` }) + "\n");
          socket.end();
          return;
        }
        if (!fs.existsSync(parsed.handoff_path)) {
          socket.write(JSON.stringify({ status: "error", request_id: requestId, error: `handoff_path not found: ${parsed.handoff_path}` }) + "\n");
          socket.end();
          return;
        }

        socket.write(JSON.stringify({ status: "dispatched", request_id: requestId }) + "\n");
        socket.end();

        try {
          pi.appendEntry(LOG, {
            event: "dispatch",
            request_id: requestId,
            doc_type: parsed.doc_type,
            handoff: parsed.handoff_path,
            output: parsed.output_path,
          });
        } catch { /* best-effort */ }

        dispatch(parsed, requestId);
      })
      .catch(() => {
        try { socket.destroy(); } catch { /* ignore */ }
      });
  }

  mountSocketServer({
    pi,
    defaultSocket: DEFAULT_SOCKET,
    hubLabel: "doc-review-hub",
    logChannel: LOG,
    onConnection: handleConnection,
    onShutdown: () => {
      for (const t of fallbackTimers) {
        try { clearTimeout(t); } catch { /* ignore */ }
      }
      fallbackTimers.clear();
    },
  });
}
