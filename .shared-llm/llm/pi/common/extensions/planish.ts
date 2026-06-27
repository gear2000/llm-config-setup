/**
 * planish — visual HTML plan review for Pi, grill-first
 *
 * Planning with planish is always a two-beat flow in the browser (port 4390):
 *
 *   1. GRILL   — planish_grill { questions[] }
 *                Pi asks a batch of questions; the user answers them in a form
 *                and submits. Always grill before writing a plan — it sharpens
 *                the plan and avoids rework. Follow-ups: call planish_grill again.
 *
 *   2. APPROVE — planish_submit_plan { filePath }
 *                Pi writes the plan as plan.html (structured tables, Tailwind CDN)
 *                and submits it. The user approves (optionally with a note) or
 *                requests changes with feedback.
 *
 * Standalone: no phase forcing beyond grill-then-plan, no execution assumption,
 * no workflow coupling. The approved plan.html is the output — what happens next
 * is up to the caller.
 *
 * Slash cmd: /planish [path]  — open plan.html (or given path) for review only
 *
 * HTTP server: http://localhost:4390 (lazy start, shared across a session)
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
let currentHtml = "";
// One interaction at a time. A plan review resolves { approved, feedback };
// a grill resolves string[] (answers indexed by question). The matching POST
// endpoint (/respond vs /grill-respond) resolves with the right shape.
let pendingResolve: ((r: any) => void) | null = null;

// ─── Helpers ──────────────────────────────────────────────────────────────────

function openBrowser(): void {
  const cmd = process.platform === "darwin" ? "open" : "xdg-open";
  spawnSync(cmd, [`http://localhost:${PORT}/`], { detached: true, stdio: "ignore" });
}

function esc(s: string): string {
  return String(s)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

// ─── Plan review: toolbar injection ─────────────────────────────────────────────
//
// Appended before </body> (or at end if absent). Inline styles only, so it works
// regardless of what CSS the plan HTML loads.

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

// ─── Grill: self-contained question form ────────────────────────────────────────

interface GrillQuestion {
  question: string;
  note?: string;
  recommendation?: string;
}

function grillFormHtml(questions: GrillQuestion[]): string {
  const blocks = questions
    .map(
      (q, i) => `
    <div class="pq">
      <div class="pq-text">Q${i + 1}. ${esc(q.question)}</div>
      ${q.note ? `<div class="pq-note">${esc(q.note)}</div>` : ""}
      ${q.recommendation ? `<div class="pq-rec">Recommended: ${esc(q.recommendation)}</div>` : ""}
      <textarea class="pq-a" data-i="${i}" placeholder="Your answer…"></textarea>
    </div>`
    )
    .join("");

  return `<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>planish — grill</title>
<style>
  *{box-sizing:border-box;margin:0;padding:0;}
  body{font-family:system-ui,-apple-system,sans-serif;background:#0d1017;color:#c8ccd4;
    padding:32px 24px 96px;line-height:1.5;max-width:860px;margin:0 auto;}
  h1{font-size:18px;color:#e6e9ef;margin-bottom:6px;}
  .sub{font-size:12px;color:#6b7280;margin-bottom:24px;}
  .pq{border:1px solid #2e3440;border-radius:8px;padding:16px 18px;margin:14px 0;background:#11151c;}
  .pq-text{font-size:14px;color:#e6e9ef;font-weight:600;margin-bottom:4px;}
  .pq-note{font-size:12px;color:#6b7280;margin-bottom:8px;}
  .pq-rec{font-size:12px;color:#98c379;margin-bottom:10px;}
  .pq-a{width:100%;min-height:60px;background:#0d1017;border:1px solid #2e3440;border-radius:6px;
    padding:9px 11px;color:#c8ccd4;font:13px/1.5 system-ui,sans-serif;resize:vertical;outline:none;}
  .pq-a:focus{border-color:#456a8a;}
  #bar{position:fixed;bottom:0;left:0;right:0;background:#0d1017;border-top:1px solid #1e222a;
    padding:12px 24px;display:flex;justify-content:flex-end;z-index:9999;}
  #submit{background:#16a34a;color:#fff;border:none;border-radius:6px;padding:9px 22px;
    font-size:13px;font-weight:600;cursor:pointer;}
  #done{display:none;text-align:center;color:#98c379;font-size:13px;padding:40px;}
</style></head>
<body>
  <h1>A few questions before the plan</h1>
  <div class="sub">Answer what you can, then click Submit. Blanks are fine — they come back as skipped.</div>
  <div id="form">${blocks}</div>
  <div id="done">Answers submitted — you can close this tab.</div>
  <div id="bar"><button id="submit" onclick="planishGrillSend()">Submit Answers</button></div>
<script>
async function planishGrillSend(){
  const answers=[];
  document.querySelectorAll('.pq-a').forEach(function(t){answers[parseInt(t.dataset.i)]=t.value.trim();});
  document.getElementById('form').style.display='none';
  document.getElementById('bar').style.display='none';
  document.getElementById('done').style.display='block';
  await fetch('/grill-respond',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({answers:answers})}).catch(function(){});
}
</script>
</body></html>`;
}

// ─── HTTP request handler ─────────────────────────────────────────────────────

function handleRequest(req: http.IncomingMessage, res: http.ServerResponse): void {
  if (req.method === "GET" && req.url === "/") {
    res.writeHead(200, { "Content-Type": "text/html; charset=utf-8" });
    res.end(currentHtml);
    return;
  }

  if (req.method === "POST" && (req.url === "/respond" || req.url === "/grill-respond")) {
    const isGrill = req.url === "/grill-respond";
    let body = "";
    req.on("data", (chunk) => (body += chunk));
    req.on("end", () => {
      res.writeHead(200, { "Content-Type": "text/plain" });
      res.end("OK");
      try {
        const parsed = JSON.parse(body);
        if (!pendingResolve) return;
        if (isGrill) {
          pendingResolve(Array.isArray(parsed.answers) ? parsed.answers : []);
        } else {
          pendingResolve({ approved: parsed.action === "approve", feedback: parsed.feedback ?? "" });
        }
        pendingResolve = null;
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

// ─── Core interactions ─────────────────────────────────────────────────────────

async function review(
  filePath: string,
  cwd: string
): Promise<{ approved: boolean; feedback: string }> {
  const resolved = path.isAbsolute(filePath) ? filePath : path.join(cwd, filePath);
  if (!fs.existsSync(resolved)) {
    throw new Error(`plan file not found: ${resolved}`);
  }
  if (pendingResolve) {
    throw new Error("a planish interaction is already in progress — wait for it to complete");
  }
  currentHtml = withToolbar(fs.readFileSync(resolved, "utf-8"));
  await ensureServer();
  return new Promise((resolve) => {
    pendingResolve = resolve;
    openBrowser();
  });
}

async function grill(questions: GrillQuestion[]): Promise<string[]> {
  if (pendingResolve) {
    throw new Error("a planish interaction is already in progress — wait for it to complete");
  }
  currentHtml = grillFormHtml(questions);
  await ensureServer();
  return new Promise((resolve) => {
    pendingResolve = resolve;
    openBrowser();
  });
}

// ─── Extension entry ──────────────────────────────────────────────────────────

export default function (pi: ExtensionAPI) {

  // ── planish_grill — ask a batch of questions before planning ──────────────

  (pi as any).registerTool({
    name: "planish_grill",
    label: "Grill Before Planning",
    description:
      "Ask the user a batch of questions in the browser BEFORE writing a plan. " +
      "ALWAYS grill first when planning with planish — resolving the open questions up front " +
      "sharpens the plan and avoids rework. Batch every question you can ask at once (the user " +
      "answers them together in one form, which is far faster than one-at-a-time). For each " +
      "question give the question text, optionally why it matters (note), and your recommended " +
      "answer (recommendation). The user fills in the form and submits; you get the answers back. " +
      "If the answers raise new questions, call planish_grill again. Once everything is resolved, " +
      "write the plan to a .html file and call planish_submit_plan.",
    parameters: {
      type: "object",
      properties: {
        questions: {
          type: "array",
          description: "The batch of questions to ask the user.",
          items: {
            type: "object",
            properties: {
              question: { type: "string", description: "The question to ask." },
              note: { type: "string", description: "Optional: why this matters / context." },
              recommendation: { type: "string", description: "Optional: your recommended answer." },
            },
            required: ["question"],
          },
        },
      },
      required: ["questions"],
    } as any,

    async execute(
      _id: string,
      params: { questions?: GrillQuestion[] },
      _signal: AbortSignal,
      _onUpdate: unknown,
      _ctx: any
    ) {
      const questions = Array.isArray(params?.questions) ? params!.questions! : [];
      if (questions.length === 0) {
        return { content: [{ type: "text", text: "Error: provide at least one question." }] };
      }
      try {
        const answers = await grill(questions);
        const text = questions
          .map((q, i) => `Q${i + 1}: ${q.question}\nA: ${answers[i]?.trim() ? answers[i] : "(skipped)"}`)
          .join("\n\n");
        return {
          content: [{
            type: "text",
            text:
              `Grill answers:\n\n${text}\n\n` +
              "Incorporate these. If they raise new questions, call planish_grill again. " +
              "Otherwise write the plan to a .html file and call planish_submit_plan.",
          }],
        };
      } catch (err) {
        return {
          content: [{
            type: "text",
            text: `planish grill error: ${err instanceof Error ? err.message : String(err)}`,
          }],
        };
      }
    },
  });

  // ── planish_submit_plan — submit the plan for approval ────────────────────

  (pi as any).registerTool({
    name: "planish_submit_plan",
    label: "Submit Plan for Review",
    description:
      "Submit a plan HTML file for human review in the browser. " +
      "Grill the user first with planish_grill — do not write the plan until the open questions are answered. " +
      "Write your plan to a .html file: a title, a summary table of what will be created/changed, " +
      "dependencies, and any relevant notes. Style with Tailwind CDN " +
      "(add <script src='https://cdn.tailwindcss.com'></script>). " +
      "Then call this tool with the file path. The user sees it in the browser and can approve " +
      "(optionally with a note) or request changes with feedback. " +
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
