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
 * Slash cmd: /planish <what to plan>   — START a planning session: turns on
 *                planMode so before_agent_start drives the agent through
 *                grill → build plan.html → submit-for-review, until approved.
 *            /planish --review <path>  — re-open an existing plan.html for review.
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

// ─── Plan output directory ──────────────────────────────────────────────────────
//
// planish writes plan.md + plan.html into a RESOLVED directory, never the cwd
// (writing into the cwd pollutes whatever repo you happen to be planning in).
// Precedence for the directory:
//   1. --dir <path> passed to /planish
//   2. $PLANISH_DIR
//   3. nearest .planish.yaml walking UP from cwd — its "dir" template
//   4. fallback: /tmp/planish/{date}/{slug}
// Template tokens: {date} → YYYY-MM-DD (local), {slug} → slugified topic,
// {type} → "plan" (hardcoded for the Pi planish extension),
// {n} → next vN integer (glob the parent dir, max + 1, start at 1). A relative
// template from .planish.yaml resolves against the directory holding that file;
// a relative --dir / $PLANISH_DIR resolves against cwd.
//
// NOTE: this resolver is intentionally DUPLICATED (not shared) in tf-implement.ts.
// Keep the two copies in sync.

// Minimal YAML parser for the .planish.yaml subset: top-level scalars and one
// level of nested key: value blocks. Handles strings, integers, and booleans.
function parseSimpleYaml(content: string): Record<string, any> {
  const result: Record<string, any> = {};
  let nested: Record<string, any> | null = null;
  for (const raw of content.split("\n")) {
    const line = raw.replace(/#.*$/, ""); // strip inline comments
    if (!line.trim()) continue;
    const indent = raw.match(/^(\s+)/)?.[1]?.length ?? 0;
    const colon = line.indexOf(":");
    if (colon === -1) continue;
    const key = line.slice(0, colon).trim();
    const rest = line.slice(colon + 1).trim();
    if (indent === 0) {
      if (rest === "") {
        nested = {};
        result[key] = nested;
      } else {
        nested = null;
        result[key] = parseYamlScalar(rest);
      }
    } else if (nested !== null) {
      nested[key] = parseYamlScalar(rest);
    }
  }
  return result;
}

function parseYamlScalar(v: string): string | number | boolean {
  if (v === "true") return true;
  if (v === "false") return false;
  const n = Number(v);
  if (!isNaN(n) && v.trim() !== "") return n;
  return v.replace(/^["']|["']$/g, "");
}

function slugifyTopic(topic: string): string {
  const slug = topic.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "");
  return slug || "plan";
}

function todayYmd(): string {
  const d = new Date();
  const month = String(d.getMonth() + 1).padStart(2, "0");
  const day = String(d.getDate()).padStart(2, "0");
  return `${d.getFullYear()}-${month}-${day}`;
}

function findConfigUp(startDir: string, filename: string): string | null {
  let dir = path.resolve(startDir);
  while (true) {
    const candidate = path.join(dir, filename);
    if (fs.existsSync(candidate)) return candidate;
    const parent = path.dirname(dir);
    if (parent === dir) return null;
    dir = parent;
  }
}

// Replace a {n} token in a path segment with the next version integer, found by
// globbing the parent dir for siblings matching the segment's prefix/suffix.
function expandVersionToken(absPath: string): string {
  const parts = absPath.split(path.sep);
  const idx = parts.findIndex((seg) => seg.includes("{n}"));
  if (idx === -1) return absPath;
  const [prefix, suffix] = parts[idx].split("{n}");
  const parent = parts.slice(0, idx).join(path.sep) || path.sep;
  let maxN = 0;
  if (fs.existsSync(parent)) {
    for (const entry of fs.readdirSync(parent)) {
      if (!entry.startsWith(prefix) || !entry.endsWith(suffix)) continue;
      const mid = entry.slice(prefix.length, entry.length - suffix.length);
      if (/^\d+$/.test(mid)) maxN = Math.max(maxN, parseInt(mid, 10));
    }
  }
  parts[idx] = `${prefix}${maxN + 1}${suffix}`;
  return parts.join(path.sep);
}

function resolvePlanDir(cwd: string, topic: string, dirFlag?: string): string {
  let template: string;
  let baseDir: string;

  if (dirFlag && dirFlag.trim()) {
    template = dirFlag.trim();
    baseDir = cwd;
  } else if (process.env.PLANISH_DIR && process.env.PLANISH_DIR.trim()) {
    template = process.env.PLANISH_DIR.trim();
    baseDir = cwd;
  } else {
    const configPath = findConfigUp(cwd, ".planish.yaml");
    if (configPath) {
      const parsed = parseSimpleYaml(fs.readFileSync(configPath, "utf-8"));
      if (typeof parsed?.dir !== "string" || !parsed.dir.trim()) {
        throw new Error(`${configPath} has no "dir" string field`);
      }
      template = parsed.dir.trim();
      baseDir = path.dirname(configPath);
    } else {
      template = "/tmp/planish/{date}/{slug}";
      baseDir = cwd;
    }
  }

  const expanded = template
    .replace(/\{date\}/g, todayYmd())
    .replace(/\{slug\}/g, slugifyTopic(topic))
    .replace(/\{type\}/g, "plan");
  const absPath = path.isAbsolute(expanded) ? expanded : path.resolve(baseDir, expanded);
  const finalDir = expandVersionToken(absPath);
  fs.mkdirSync(finalDir, { recursive: true });
  return finalDir;
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
    background: #0d1017; border-top: 1px solid #1e222a;
    padding: 12px 16px; display: flex; gap: 12px; align-items: flex-start;
    z-index: 9999; box-shadow: 0 -2px 8px rgba(0,0,0,.4);
    font-family: 'JetBrains Mono', monospace;
  }
  body { padding-bottom: 96px !important; }
  #planish-fb {
    flex: 1; border: 1px solid #2e3440; border-radius: 6px;
    padding: 8px 10px; font-size: 12px; resize: none; font-family: inherit;
    background: #0b0e14; color: #c8ccd4;
  }
  #planish-fb.error { border-color: #e06c75; }
  .pbtn {
    padding: 7px 16px; border-radius: 6px; font-size: 12px;
    font-weight: 500; cursor: pointer; border: none; white-space: nowrap;
    font-family: inherit;
  }
  .pbtn-ok  { background: #0f2d17; color: #98c379; border: 1px solid #3a5a2a; }
  .pbtn-chg { background: #1a1208; color: #d19a66; border: 1px solid #5a4226; }
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
    padding:9px 11px;color:#c8ccd4;font:12px/1.5 'JetBrains Mono',monospace;resize:vertical;outline:none;}
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
  // /planish sets these; before_agent_start then drives the agent through the
  // grill → build → review flow until the plan is approved.
  let planMode = false;
  let planTopic = "";
  let planDir = ""; // absolute dir for plan.md + plan.html; resolved at /planish time

  pi.on("before_agent_start", async (event: any) => {
    if (!planMode) return;
    const topic = planTopic ? `The user wants to plan: ${planTopic}\n\n` : "";
    const planHtml = path.join(planDir, "plan.html");
    const planMd = path.join(planDir, "plan.md");
    // NOTE: planish grill->build->review prompt is intentionally DUPLICATED (not shared) in tf-implement.ts and the Claude /do:planish command. Keep in sync. See do:planish.
    return {
      systemPrompt:
        event.systemPrompt +
        `\n\n${topic}You are helping the user create a PLAN with planish — produce a plan, not an implementation. Do NOT build or run anything unless the user explicitly asks after the plan is approved.\n\n` +
        "STEP 1 — GRILL: Call the planish_grill tool with a batch of clarifying questions (scope, constraints, the real choices, unknowns, what already exists). Give each one your recommended answer. If the answers raise new questions, call planish_grill again.\n\n" +
        `STEP 2 — BUILD: Write the plan to TWO files (the directory already exists):\n` +
        // # ref 1 (plan-html-style) — also duplicated in: planish_submit_plan description below, tf-implement.ts STEP 2
        `  • ${planHtml} — the visual plan: a title, a summary of phases, key decisions, and verification steps.\n` +
        `    Use the v3 dark style (NO Tailwind CDN). Include in <head>:\n` +
        `    <link href="https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@300;400;500;600;700&family=IBM+Plex+Sans:wght@300;400;500;600&display=swap" rel="stylesheet">\n` +
        `    <style>\n` +
        `      *{box-sizing:border-box;margin:0;padding:0;}\n` +
        `      body{font-family:'JetBrains Mono',monospace;background:#0d1017;color:#c8ccd4;padding:40px;max-width:1040px;line-height:1.5;}\n` +
        `      h1{font-family:'IBM Plex Sans',sans-serif;font-size:22px;font-weight:600;color:#e6e9ef;letter-spacing:-0.3px;margin-bottom:6px;}\n` +
        `      .subtitle{font-size:11px;color:#545862;letter-spacing:1.5px;text-transform:uppercase;margin-bottom:28px;padding-bottom:22px;border-bottom:1px solid #1e222a;}\n` +
        `      h2{font-family:'IBM Plex Sans',sans-serif;font-size:14px;font-weight:500;color:#e6e9ef;margin:32px 0 14px;padding-bottom:8px;border-bottom:1px solid #1e222a;}\n` +
        `      .card{border:1px solid #1e222a;border-radius:10px;padding:18px 22px;background:#0f1219;margin-bottom:16px;}\n` +
        `      .card.amber{border-left:3px solid #d19a66;background:#15120d;}\n` +
        `      .card.blue{border-left:3px solid #7ab4db;background:#0d1320;}\n` +
        `      .card.green{border-left:3px solid #7aa87a;background:#0f1f14;}\n` +
        `      .card.red{border-left:3px solid #e06c75;background:#1c1012;}\n` +
        `      .phase-num{font-size:10px;font-weight:600;color:#545862;letter-spacing:1px;text-transform:uppercase;margin-bottom:6px;}\n` +
        `      .phase-title{font-size:13px;font-weight:500;color:#e6e9ef;margin-bottom:10px;}\n` +
        `      ul{list-style:none;padding:0;}\n` +
        `      li{font-size:11px;color:#a0a4ac;line-height:2;padding-left:12px;position:relative;}\n` +
        `      li::before{content:'·';color:#d19a66;position:absolute;left:0;}\n` +
        `      code{background:#1a1f29;color:#7ab4db;border-radius:3px;padding:1px 5px;font-size:11px;}\n` +
        `      .chip{font-size:10px;padding:2px 8px;border-radius:9999px;border:1px solid #2a2e38;background:#13141a;color:#a0a4ac;display:inline-block;margin-left:6px;}\n` +
        `      .chip.green{border-color:#3a5a2a;color:#98c379;} .chip.amber{border-color:#5a4226;color:#d19a66;}\n` +
        `    </style>\n` +
        `    Structure: page header (h1 + .subtitle), then h2 sections per phase with .card divs (.phase-num, .phase-title, task bullets, a Verification bullet at end).\n` +
        `  • ${planMd} — the same plan as token-lean Markdown (the .md is the lean agent record, the .html is the visual/annotatable copy).\n` +
        `Both files hold the same plan content.\n\n` +
        `STEP 3 — REVIEW: Call planish_submit_plan with the path ${planHtml}. The user approves or requests changes in the browser; on changes, revise both files and submit again. The approved plan is the deliverable.`,
    };
  });

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
      // # dup 1 (plan-html-style)
      "Write your plan to a .html file: a title, a summary of phases, key decisions, and verification steps. " +
      "Use the v3 dark style (NO Tailwind CDN) — body background #0d1017, JetBrains Mono + IBM Plex Sans fonts, " +
      ".card divs with colored left-border accents for each phase. " +
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
        const result = await review(filePath, planDir || (ctx?.cwd ?? process.cwd()));
        if (result.approved) {
          planMode = false; // planning session done
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

  // ── /planish [description] — START a planning session ─────────────────────
  //
  // Default use: /planish <what you want to plan>. Turns on planMode; the
  // before_agent_start hook then drives the agent: grill → build plan.html →
  // submit for browser review, iterating until approved.
  // Escape hatch: /planish --review <path> re-opens an existing plan.html.

  (pi as any).registerCommand("planish", {
    description: "Start a planning session: /planish <what to plan> — grills you in the browser, builds a visual HTML plan, iterates until you approve. Re-open an existing plan with: /planish --review <path>",
    handler: async (args: string, ctx: any) => {
      const trimmed = args.trim();

      // Escape hatch — re-open an existing plan for review.
      const reviewMatch = trimmed.match(/^--review\s+(.+)$/);
      if (reviewMatch) {
        const filePath = reviewMatch[1].trim();
        try {
          ctx.ui.notify(`planish: opening ${filePath} for review…`, "info");
          const result = await review(filePath, ctx?.cwd ?? process.cwd());
          ctx.ui.notify(
            result.approved
              ? "planish: approved" + (result.feedback ? ` — note: ${result.feedback}` : "")
              : `planish: changes requested — ${result.feedback || "(no feedback)"}`,
            result.approved ? "info" : "warning"
          );
        } catch (err) {
          ctx.ui.notify(`planish: ${err instanceof Error ? err.message : String(err)}`, "error");
        }
        return;
      }

      // Default — kick off a planning session.
      // Optional --dir <path> overrides where plan.md + plan.html are written; the
      // remainder is the topic.
      let dirFlag: string | undefined;
      let topic = trimmed;
      const dirMatch = topic.match(/(^|\s)--dir\s+(\S+)/);
      if (dirMatch) {
        dirFlag = dirMatch[2];
        topic = topic.replace(/(^|\s)--dir\s+\S+/, " ").trim();
      }

      try {
        planDir = resolvePlanDir(ctx?.cwd ?? process.cwd(), topic, dirFlag);
      } catch (err) {
        ctx.ui.notify(`planish: ${err instanceof Error ? err.message : String(err)}`, "error");
        return;
      }
      planMode = true;
      planTopic = topic;
      ctx.ui.notify(
        topic
          ? `planish: planning "${topic}" — Pi will grill you in the browser, then build a visual plan in ${planDir} for your review.`
          : `planish: planning mode on — tell Pi what you want to plan. It will grill you in the browser, then build a visual plan in ${planDir} for review.`,
        "info"
      );
    },
  });
}
