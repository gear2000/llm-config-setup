/**
 * planish — visual HTML plan review for Pi
 *
 * Pi writes a plan as plan.html (structured tables, step lists, Tailwind CDN
 * for styling). planish opens it in the local browser with an approve /
 * request-changes toolbar. The result comes back as a tool response.
 *
 * Standalone: no phase forcing, no execution assumption, no workflow coupling.
 * The approved plan.html is the output — what happens next is up to the caller.
 *
 * Pi tool:   planish_submit_plan { filePath: "plan.html" }
 * Slash cmd: /planish [path]  — opens plan.html (or given path) in browser
 *
 * HTTP server: http://localhost:4390 (lazy start, shared across reviews in a session)
 */

import type { ExtensionAPI } from "@mariozechner/pi-coding-agent";
import * as fs from "node:fs";
import * as http from "node:http";
import * as path from "node:path";
import { spawnSync } from "node:child_process";

// ─── Config ───────────────────────────────────────────────────────────────────

const PORT = 4390;

// ─── Server state (module-level) ─────────────────────────────────────────────

let server: http.Server | null = null;
let currentPlanHtml = "";
let pendingResolve: ((r: { approved: boolean; feedback: string }) => void) | null = null;

// ─── Browser ──────────────────────────────────────────────────────────────────

function openBrowser(): void {
  const cmd = process.platform === "darwin" ? "open" : "xdg-open";
  spawnSync(cmd, [`http://localhost:${PORT}/`], { detached: true, stdio: "ignore" });
}

// ─── Toolbar injection ────────────────────────────────────────────────────────
//
// Appended before </body> (or at end if absent). Uses only inline styles so
// it works regardless of what CSS the plan HTML loads.

function withToolbar(html: string): string {
  const bar = `
<style>
  #planish-bar {
    position: fixed; bottom: 0; left: 0; right: 0;
    background: #fff; border-top: 1px solid #e5e7eb;
    padding: 12px 16px; display: flex; gap: 12px; align-items: flex-start;
    z-index: 9999; box-shadow: 0 -2px 8px rgba(0,0,0,.08);
    font-family: system-ui, -apple-system, sans-serif;
  }
  body { padding-bottom: 96px !important; }
  #planish-fb {
    flex: 1; border: 1px solid #d1d5db; border-radius: 6px;
    padding: 8px 10px; font-size: 13px; resize: none; font-family: inherit;
  }
  #planish-fb.error { border-color: #ef4444; }
  .pbtn {
    padding: 8px 18px; border-radius: 6px; font-size: 13px;
    font-weight: 500; cursor: pointer; border: none; white-space: nowrap;
  }
  .pbtn-ok  { background: #16a34a; color: #fff; }
  .pbtn-chg { background: #d97706; color: #fff; }
</style>
<div id="planish-bar">
  <textarea id="planish-fb" placeholder="Feedback (optional for approval, required for changes)…" rows="2"></textarea>
  <div style="display:flex;flex-direction:column;gap:6px;">
    <button class="pbtn pbtn-ok"  onclick="planishSend('approve')">Approve ✓</button>
    <button class="pbtn pbtn-chg" onclick="planishSend('changes')">Request Changes</button>
  </div>
</div>
<script>
async function planishSend(action) {
  const fb = document.getElementById('planish-fb').value.trim();
  if (action === 'changes' && !fb) {
    const el = document.getElementById('planish-fb');
    el.classList.add('error');
    el.placeholder = 'Feedback is required when requesting changes.';
    el.focus();
    return;
  }
  document.getElementById('planish-bar').innerHTML =
    '<p style="padding:12px 16px;color:#6b7280;font-size:13px;">Response sent — you can close this tab.</p>';
  await fetch('/respond', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ action, feedback: fb }),
  }).catch(() => {});
}
</script>`;

  return html.includes("</body>")
    ? html.replace("</body>", bar + "\n</body>")
    : html + bar;
}

// ─── HTTP request handler ─────────────────────────────────────────────────────

function handleRequest(req: http.IncomingMessage, res: http.ServerResponse): void {
  if (req.method === "GET" && req.url === "/") {
    res.writeHead(200, { "Content-Type": "text/html; charset=utf-8" });
    res.end(withToolbar(currentPlanHtml));
    return;
  }

  if (req.method === "POST" && req.url === "/respond") {
    let body = "";
    req.on("data", (chunk) => (body += chunk));
    req.on("end", () => {
      res.writeHead(200, { "Content-Type": "text/plain" });
      res.end("OK");
      try {
        const { action, feedback } = JSON.parse(body) as { action: string; feedback?: string };
        if (pendingResolve) {
          pendingResolve({ approved: action === "approve", feedback: feedback ?? "" });
          pendingResolve = null;
        }
      } catch { /* ignore malformed bodies */ }
    });
    return;
  }

  res.writeHead(404);
  res.end();
}

// ─── Server lifecycle ─────────────────────────────────────────────────────────

function ensureServer(): Promise<void> {
  if (server) return Promise.resolve();
  return new Promise((resolve, reject) => {
    const s = http.createServer(handleRequest);
    s.on("error", (err) => {
      server = null;
      reject(err);
    });
    s.listen(PORT, "127.0.0.1", () => {
      server = s;
      resolve();
    });
  });
}

// ─── Core review ─────────────────────────────────────────────────────────────

async function review(
  filePath: string,
  cwd: string
): Promise<{ approved: boolean; feedback: string }> {
  const resolved = path.isAbsolute(filePath) ? filePath : path.join(cwd, filePath);
  if (!fs.existsSync(resolved)) {
    throw new Error(`plan file not found: ${resolved}`);
  }
  if (pendingResolve) {
    throw new Error("a plan review is already in progress — wait for it to complete");
  }
  currentPlanHtml = fs.readFileSync(resolved, "utf-8");
  await ensureServer();
  return new Promise((resolve) => {
    pendingResolve = resolve;
    openBrowser();
  });
}

// ─── Extension entry ──────────────────────────────────────────────────────────

export default function (pi: ExtensionAPI) {

  // ── planish_submit_plan — Pi tool ─────────────────────────────────────────

  (pi as any).registerTool({
    name: "planish_submit_plan",
    label: "Submit Plan for Review",
    description:
      "Submit a plan HTML file for human review in the browser. " +
      "Write your plan to a .html file first — use structured HTML: a title, " +
      "a summary table of what will be created/changed, dependencies, and any relevant notes. " +
      "Style with Tailwind CDN (add <script src='https://cdn.tailwindcss.com'></script>). " +
      "Then call this tool with the file path. " +
      "The user sees it in the browser and can approve (optionally with a note) " +
      "or request changes with feedback. " +
      "If changes are requested: revise the file in place and call this tool again.",
    parameters: {
      type: "object",
      properties: {
        filePath: {
          type: "string",
          description: "Path to the plan HTML file, relative to cwd (e.g. plan.html)",
        },
      },
      required: ["filePath"],
    } as any,

    async execute(
      _id: string,
      params: { filePath?: string },
      _signal: AbortSignal,
      _onUpdate: unknown,
      ctx: any
    ) {
      const filePath = (params?.filePath ?? "").trim();
      if (!filePath) {
        return { content: [{ type: "text", text: "Error: filePath is required." }] };
      }
      try {
        const result = await review(filePath, ctx?.cwd ?? process.cwd());
        if (result.approved) {
          const note = result.feedback ? ` Human note: ${result.feedback}` : "";
          return {
            content: [{ type: "text", text: `Plan approved.${note}` }],
          };
        }
        const fb = result.feedback || "(no feedback provided)";
        return {
          content: [{
            type: "text",
            text: `Changes requested: ${fb}\n\nRevise ${filePath} and call planish_submit_plan again with the same path.`,
          }],
        };
      } catch (err) {
        return {
          content: [{
            type: "text",
            text: `planish error: ${err instanceof Error ? err.message : String(err)}`,
          }],
        };
      }
    },
  });

  // ── /planish [path] — slash command for manual review ─────────────────────

  (pi as any).registerCommand("planish", {
    description: "Open a plan HTML file for review in the browser: /planish [path] (defaults to plan.html in cwd)",
    handler: async (args: string, ctx: any) => {
      const filePath = args.trim() || "plan.html";
      try {
        ctx.ui.notify(`planish: opening ${filePath}…`, "info");
        const result = await review(filePath, ctx?.cwd ?? process.cwd());
        if (result.approved) {
          ctx.ui.notify(
            "planish: approved" + (result.feedback ? ` — note: ${result.feedback}` : ""),
            "info"
          );
        } else {
          ctx.ui.notify(
            `planish: changes requested — ${result.feedback || "(no feedback)"}`,
            "warning"
          );
        }
      } catch (err) {
        ctx.ui.notify(
          `planish: ${err instanceof Error ? err.message : String(err)}`,
          "error"
        );
      }
    },
  });
}
