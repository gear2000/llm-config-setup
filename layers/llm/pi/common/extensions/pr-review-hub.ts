/**
 * pr-review-hub — Unix socket dispatch for adversarial PR / repo-state review.
 *
 * Listens on ~/.pi/pr-reviewer.sock. Keeps an in-memory registry keyed by
 * review_key so only ONE active PR review runs per key — this is what stops two
 * duplicate reviews of the same scope running at once. The registry lives in the
 * hub process and is cleared on restart.
 *
 * Request (old field names handoff/output/timeout_ms are also accepted):
 *   {
 *     "request_id": "caller attempt id",
 *     "review_key": "repo|branch-or-pr|base|head-sha|scope",
 *     "mode": "new|status|replace|cancel",
 *     "repo_path": "/abs/repo/path",
 *     "branch": "branch-name",
 *     "base_ref": "main-or-sha",
 *     "head_sha": "exact-head-commit-sha",
 *     "scope": "integration|security|tests|ux|full|other",
 *     "handoff_path": "/abs/path/to/handoff.md",
 *     "output_path": "/abs/path/to/review.md",
 *     "timeout_seconds": 1200,
 *     "previous_run_id": "optional"
 *   }
 *
 * Usage: pi -e ~/.pi/extensions/pr-review-hub.ts
 *   or:  pi -e ~/.pi/extensions/pr-review-hub.ts --socket /custom/path.sock
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

const DEFAULT_SOCKET = path.join(os.homedir(), ".pi", "pr-reviewer.sock");
const DEFAULT_TIMEOUT_SECONDS = 1200;
const AGENT = "pr-reviewer";
const LOG = "pr-review-hub-log";
const COMPLETION_POLL_MS = 15_000;
const MODES = ["new", "status", "replace", "cancel"];
const SCOPES = ["integration", "security", "tests", "ux", "full", "other"];

interface ActiveEntry {
  request_id: string;
  output_path: string;
  head_sha: string;
  status: "active" | "superseded" | "cancelled";
  dispatched_at: string;
}

export default function (pi: ExtensionAPI) {
  // review_key -> the one active review for that key
  const active = new Map<string, ActiveEntry>();
  // request_id -> its release timers (so completion or timeout frees the key)
  const timers = new Map<string, { fallback: NodeJS.Timeout; poll: NodeJS.Timeout }>();

  function clearTimers(requestId: string): void {
    const t = timers.get(requestId);
    if (!t) return;
    try { clearTimeout(t.fallback); } catch { /* ignore */ }
    try { clearInterval(t.poll); } catch { /* ignore */ }
    timers.delete(requestId);
  }

  // Free the registry slot, but only if this request still owns it — a stale
  // timer from a superseded run must not evict the replacement.
  function release(reviewKey: string, requestId: string): void {
    clearTimers(requestId);
    const entry = active.get(reviewKey);
    if (entry && entry.request_id === requestId) {
      active.delete(reviewKey);
    }
  }

  function reply(socket: net.Socket, obj: any): void {
    try { socket.write(JSON.stringify(obj) + "\n"); } catch { /* ignore */ }
    try { socket.end(); } catch { /* ignore */ }
  }

  function dispatch(req: any, reviewKey: string, requestId: string, note?: string): void {
    const timeoutSeconds = Number(req.timeout_seconds) || DEFAULT_TIMEOUT_SECONDS;

    const task = [
      `Read the handoff at ${req.handoff_path}.`,
      `This is a PR review.`,
      `Repo: ${req.repo_path}. Branch: ${req.branch}. Base ref: ${req.base_ref}. Head SHA: ${req.head_sha}. Scope: ${req.scope}.`,
      `Review the requested repo state at that head SHA.`,
      `Do not modify any project or source files — only write your review findings to ${req.output_path}.`,
    ].join(" ");

    const content = [`[pr-review request_id=${requestId} review_key=${reviewKey}]`];
    if (note) content.push(note);
    content.push(
      `Use the subagent tool to run the ${AGENT} agent:`,
      `subagent({ agent: "${AGENT}", task: "${task.replace(/"/g, '\\"')}", async: true })`,
    );

    pi.sendMessage(
      { customType: "pr-review-dispatch", content: content.join("\n"), display: true },
      { deliverAs: "followUp", triggerTurn: true },
    );

    // Release the slot when the review finishes (output appears) or at timeout.
    const fallback = setTimeout(() => {
      if (!fs.existsSync(req.output_path)) {
        writeError(req.output_path, `timeout after ${timeoutSeconds}s`, requestId);
      }
      release(reviewKey, requestId);
    }, timeoutSeconds * 1000 + 30_000);
    try { (fallback as any).unref?.(); } catch { /* ignore */ }

    const poll = setInterval(() => {
      if (fs.existsSync(req.output_path)) {
        release(reviewKey, requestId);
      }
    }, COMPLETION_POLL_MS);
    try { (poll as any).unref?.(); } catch { /* ignore */ }

    timers.set(requestId, { fallback, poll });
  }

  function handleConnection(socket: net.Socket): void {
    readOneLine(socket)
      .then((line) => {
        let parsed: any;
        try {
          parsed = normalizeAliases(JSON.parse(line));
        } catch {
          reply(socket, { status: "error", request_id: "", error: "malformed JSON" });
          return;
        }

        const requestId = (typeof parsed.request_id === "string" && parsed.request_id) ? parsed.request_id : generateId();
        const mode = String(parsed.mode || "");
        const reviewKey = parsed.review_key;

        if (!reviewKey) {
          reply(socket, { status: "error", request_id: requestId, error: "missing field: review_key" });
          return;
        }
        if (!MODES.includes(mode)) {
          reply(socket, { status: "error", request_id: requestId, error: `invalid mode: ${parsed.mode}` });
          return;
        }

        // status / cancel never dispatch, so they skip repo/handoff validation.
        if (mode === "status") {
          const entry = active.get(reviewKey);
          if (entry) {
            reply(socket, {
              status: "active",
              request_id: requestId,
              review_key: reviewKey,
              active_request_id: entry.request_id,
              active_output_path: entry.output_path,
              head_sha: entry.head_sha,
            });
          } else {
            reply(socket, { status: "not_found", request_id: requestId, review_key: reviewKey });
          }
          return;
        }

        if (mode === "cancel") {
          const entry = active.get(reviewKey);
          if (!entry) {
            reply(socket, { status: "not_found", request_id: requestId, review_key: reviewKey });
            return;
          }
          entry.status = "cancelled";
          reply(socket, {
            status: "cancelled",
            request_id: requestId,
            review_key: reviewKey,
            cancelled_request_id: entry.request_id,
          });
          pi.sendMessage(
            {
              customType: "pr-review-cancel",
              content: [
                `[pr-review cancel review_key=${reviewKey}]`,
                `If a ${AGENT} run for request_id=${entry.request_id} is still in progress, interrupt/supersede it if possible.`,
              ].join("\n"),
              display: true,
            },
            { deliverAs: "followUp", triggerTurn: true },
          );
          release(reviewKey, entry.request_id);
          return;
        }

        // new / replace dispatch a real review — validate the full field set.
        const missing: string[] = [];
        if (!parsed.repo_path) missing.push("repo_path");
        if (!parsed.handoff_path) missing.push("handoff_path");
        if (!parsed.output_path) missing.push("output_path");
        if (!parsed.head_sha) missing.push("head_sha"); // mandatory — no moving-branch review
        if (missing.length) {
          reply(socket, { status: "error", request_id: requestId, error: `missing field(s): ${missing.join(", ")}` });
          return;
        }
        if (parsed.scope != null && !SCOPES.includes(String(parsed.scope))) {
          reply(socket, { status: "error", request_id: requestId, error: `invalid scope: ${parsed.scope}` });
          return;
        }
        if (!fs.existsSync(parsed.repo_path)) {
          reply(socket, { status: "error", request_id: requestId, error: `repo_path not found: ${parsed.repo_path}` });
          return;
        }
        if (!fs.existsSync(parsed.handoff_path)) {
          reply(socket, { status: "error", request_id: requestId, error: `handoff_path not found: ${parsed.handoff_path}` });
          return;
        }

        const existing = active.get(reviewKey);

        if (mode === "new" && existing && existing.status === "active") {
          reply(socket, {
            status: "duplicate_active",
            request_id: requestId,
            review_key: reviewKey,
            active_request_id: existing.request_id,
            active_output_path: existing.output_path,
          });
          return;
        }

        let note: string | undefined;
        if (mode === "replace" && existing) {
          existing.status = "superseded";
          clearTimers(existing.request_id);
          note = `This REPLACES an in-progress review (request_id=${existing.request_id}); interrupt/supersede that previous run if possible.`;
        }

        active.set(reviewKey, {
          request_id: requestId,
          output_path: parsed.output_path,
          head_sha: String(parsed.head_sha),
          status: "active",
          dispatched_at: new Date().toISOString(),
        });

        reply(socket, { status: "dispatched", request_id: requestId, review_key: reviewKey });

        try {
          pi.appendEntry(LOG, {
            event: "dispatch",
            mode,
            request_id: requestId,
            review_key: reviewKey,
            head_sha: parsed.head_sha,
            output: parsed.output_path,
          });
        } catch { /* best-effort */ }

        dispatch(parsed, reviewKey, requestId, note);
      })
      .catch(() => {
        try { socket.destroy(); } catch { /* ignore */ }
      });
  }

  mountSocketServer({
    pi,
    defaultSocket: DEFAULT_SOCKET,
    hubLabel: "pr-review-hub",
    logChannel: LOG,
    onConnection: handleConnection,
    onShutdown: () => {
      for (const t of timers.values()) {
        try { clearTimeout(t.fallback); } catch { /* ignore */ }
        try { clearInterval(t.poll); } catch { /* ignore */ }
      }
      timers.clear();
      active.clear();
    },
  });
}
