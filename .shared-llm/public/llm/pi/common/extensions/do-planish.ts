/**
 * do-planish — visual HTML plan review for Pi, grill-first
 *
 * Planning with do-planish is always a two-beat flow in the browser (port 4390):
 *
 *   1. GRILL   — planish_grill { questions[] }
 *                Pi asks a batch of questions on an annotatable page. The tool
 *                serves the page and returns IMMEDIATELY — the agent gives the
 *                user the URL and ends its turn. The user drops sticky notes on
 *                the questions, clicks Copy Feedback, and pastes the ## Feedback
 *                block into the TUI. A question with no note means "go with the
 *                recommendation". Follow-ups: call planish_grill again.
 *
 *   2. REVIEW  — planish_submit_plan { filePath }
 *                Pi writes the plan as plan.html (+ plan.md) and serves it for
 *                review — again returning immediately. The user annotates and
 *                pastes feedback; a ## FINALIZED block (or explicit approval)
 *                approves the plan, notes request changes.
 *
 * ONE feedback transport, per the Planish HTML Grill Contract: annotate →
 * Copy Feedback → paste back. No answer boxes, no Submit/Approve buttons, no
 * browser→agent POST, and no tool call that blocks waiting on the browser.
 *
 * Standalone: no phase forcing beyond grill-then-plan, no execution assumption,
 * no workflow coupling. The approved plan.html is the output — what happens next
 * is up to the caller.
 *
 * Slash cmd: /do-planish <what to plan>   — START a Pi-native planning session: turns on
 *                planMode so before_agent_start drives the agent through
 *                browser grill → build plan.html → serve-for-review, until approved.
 *            /do-planish --review <path>  — re-open an existing plan.html for review.
 *
 * Note: /do-planish (this extension) is the standalone Pi planner; /cc-planish is the
 * standalone Claude Code port of the same flow (same .planish.yaml contract).
 * /do-plan-and-grill and /cc-plan-and-grill are the workflow-suite planners.
 *
 * HTTP server: port 4390 (lazy start, shared across a session). The URL host
 * comes from `host:` in the nearest .planish.yaml or $PLANISH_HOST (default
 * localhost) — set it to the machine name remote browsers use (e.g. a
 * Tailscale name) and the server binds 0.0.0.0 so those connections work.
 */

import type { ExtensionAPI } from "@mariozechner/pi-coding-agent";
import * as fs from "node:fs";
import * as http from "node:http";
import * as path from "node:path";
import { fileURLToPath } from "node:url";
import { spawnSync } from "node:child_process";

// ─── Config ───────────────────────────────────────────────────────────────────

const PORT = 4390;

// ─── Server state (module-level) ─────────────────────────────────────────────

let server: http.Server | null = null;
let currentHtml = "";

// ─── Helpers ──────────────────────────────────────────────────────────────────

// Returns false when the opener is missing or exits non-zero (headless box,
// no default browser). Callers MUST surface that — the user needs the URL.
function openBrowser(): boolean {
  const cmd = process.platform === "darwin" ? "open" : "xdg-open";
  const r = spawnSync(cmd, [`http://${resolveHost()}:${PORT}/`], { detached: true, stdio: "ignore" });
  return !r.error && r.status === 0;
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
//   1. --dir <path> passed to /do-planish
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
    const parsed = configPath ? parseSimpleYaml(fs.readFileSync(configPath, "utf-8")) : null;
    if (configPath && parsed?.dir !== undefined) {
      // dir present but unusable is a config typo — fail loud, never fall back.
      if (typeof parsed.dir !== "string" || !parsed.dir.trim()) {
        throw new Error(`${configPath} "dir" must be a non-empty string`);
      }
      template = parsed.dir.trim();
      baseDir = path.dirname(configPath!);
    } else {
      // no config, or a host-only config — default template.
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

// ─── Serve host ─────────────────────────────────────────────────────────────
//
// URLs the tools hand out default to localhost, which breaks remote sessions
// (Tailscale/SSH): the user's browser is not on this box. `host:` in the
// nearest .planish.yaml — or $PLANISH_HOST — names this machine as the
// browser reaches it (e.g. a Tailscale MagicDNS name). With a non-localhost
// host the server binds 0.0.0.0 so those remote connections are accepted;
// the default stays 127.0.0.1-only. Resolved once, at first use.

let resolvedHost: string | null = null;

function resolveHost(): string {
  if (resolvedHost) return resolvedHost;
  let host = "localhost";
  if (process.env.PLANISH_HOST && process.env.PLANISH_HOST.trim()) {
    host = process.env.PLANISH_HOST.trim();
  } else {
    const configPath = findConfigUp(process.cwd(), ".planish.yaml");
    if (configPath) {
      const parsed = parseSimpleYaml(fs.readFileSync(configPath, "utf-8"));
      if (typeof parsed?.host === "string" && parsed.host.trim()) host = parsed.host.trim();
    }
  }
  resolvedHost = host;
  return host;
}

function isLocalOnly(host: string): boolean {
  return host === "localhost" || host === "127.0.0.1";
}

// ─── Canonical annotation toolkit (runtime-read — the ONLY interactive surface) ─
//
// The sticky-note bar is NOT inlined here. It has exactly ONE canonical
// implementation in the kit, at
// .shared-llm/public/llm/common/common/toolkits/annotation-toolkit.html — skills paste
// it verbatim, and this extension READS that same file at serve time, so the two
// can never drift. The path is resolved relative to THIS module's directory via
// import.meta.url; Node ESM resolves import.meta.url to the module's realpath,
// so the read works identically whether the extension is loaded from the repo
// or through its ~/.pi/agent/extensions/ symlink.
//
// Injected into every page planish serves: the grill page embeds it, and a
// reviewed plan.html gets it appended unless the file already carries its own
// annotation controls. Feedback flows one way — the user annotates, clicks
// Copy Feedback, and pastes the block into the TUI. No answer boxes, no
// Submit buttons, no POST back to the agent.
const CANONICAL_TOOLKIT_PATH = path.join(
  path.dirname(fileURLToPath(import.meta.url)),
  "../../../common/common/toolkits/annotation-toolkit.html",
);

function annotationToolkitHtml(): string {
  return fs.readFileSync(CANONICAL_TOOLKIT_PATH, "utf-8");
}

// The canonical toolkit keys notes off <meta name="desdoc-key"> and, when that
// meta is present, prunes every OTHER desdoc_r__ key on load. planish serves
// every round from the one URL (localhost:4390/), so each serve injects that
// meta with a fresh per-serve nonce — new round, clean slate (pathname keying
// would otherwise resurrect the previous round's notes).
let serveNonceCounter = 0;

function nextNonce(): string {
  return `${Date.now().toString(36)}r${++serveNonceCounter}`;
}

function desdocKeyMeta(nonce: string): string {
  return `<meta name="desdoc-key" content="${nonce}">`;
}

// A reviewed plan.html usually embeds its own annotation controls (the build
// step requires them). Only inject ours when the file has none — two bars
// (and two ddAdd definitions) on one page would collide.
function ensureAnnotable(html: string): string {
  if (html.includes("desdoc-bar") || html.includes("ddCopy") || html.includes("Copy Feedback")) {
    return html;
  }
  // Give the injected canonical toolkit a unique per-serve key so notes don't
  // carry over from a previous serve (all rounds share the one URL).
  const meta = desdocKeyMeta(nextNonce());
  const withMeta = html.includes("</head>")
    ? html.replace("</head>", `${meta}\n</head>`)
    : `${meta}\n${html}`;
  const block = "\n" + annotationToolkitHtml();
  return withMeta.includes("</body>")
    ? withMeta.replace("</body>", block + "\n</body>")
    : withMeta + block;
}

// ─── Auto-freeze: plan-v<k> versioning enforced by the tool, not the prompt ─────
//
// Versioning is tool-enforced: before serving a plan for review, planish_submit_plan
// compares plan.html / plan.md against the newest frozen plan-v<k> pair in the same
// directory and, when they differ (or no frozen pair exists yet), writes the next
// plan-v<k+1>.{html,md} pair FIRST, then serves. The frozen files are immutable
// history; plan.html / plan.md are the always-latest working copy. Prompt prose that
// told the agent to freeze by hand demonstrably failed — the discipline lives here.

interface FreezeResult {
  froze: boolean;
  version: number; // the plan-v<version> written (froze) or the newest existing (no-op)
}

// Highest k for which <stem>-v<k>.html exists in dir (0 when none).
function newestFrozenVersion(dir: string, stem: string): number {
  const prefix = `${stem}-v`;
  const suffix = ".html";
  let maxK = 0;
  if (!fs.existsSync(dir)) return 0;
  for (const entry of fs.readdirSync(dir)) {
    if (!entry.startsWith(prefix) || !entry.endsWith(suffix)) continue;
    const mid = entry.slice(prefix.length, entry.length - suffix.length);
    if (/^\d+$/.test(mid)) maxK = Math.max(maxK, parseInt(mid, 10));
  }
  return maxK;
}

function readFileOrNull(p: string): string | null {
  return fs.existsSync(p) ? fs.readFileSync(p, "utf-8") : null;
}

// Freeze the current plan.{html,md} as the next plan-v<k> pair unless it already
// matches the newest frozen pair byte-for-byte. htmlPath is the resolved plan.html.
function autoFreezePlan(htmlPath: string): FreezeResult {
  const dir = path.dirname(htmlPath);
  const ext = path.extname(htmlPath); // ".html"
  const stem = path.basename(htmlPath, ext); // "plan"
  const mdPath = path.join(dir, `${stem}.md`);

  const curHtml = readFileOrNull(htmlPath);
  // Nothing on disk to freeze — review() surfaces the missing-file error.
  if (curHtml === null) return { froze: false, version: newestFrozenVersion(dir, stem) };
  const curMd = readFileOrNull(mdPath);

  const newest = newestFrozenVersion(dir, stem);
  if (newest > 0) {
    const frozenHtml = readFileOrNull(path.join(dir, `${stem}-v${newest}.html`));
    const frozenMd = readFileOrNull(path.join(dir, `${stem}-v${newest}.md`));
    // Unchanged since the last snapshot — do not write a duplicate version.
    if (frozenHtml === curHtml && frozenMd === curMd) return { froze: false, version: newest };
  }

  const next = newest + 1;
  fs.writeFileSync(path.join(dir, `${stem}-v${next}.html`), curHtml);
  if (curMd !== null) fs.writeFileSync(path.join(dir, `${stem}-v${next}.md`), curMd);
  return { froze: true, version: next };
}

// ─── Grill: self-contained annotatable question page ───────────────────────────

interface GrillQuestion {
  question: string;
  note?: string;
  recommendation?: string;
  // No mermaid field: Mermaid rendered from a CDN at view time, so any syntax
  // slip = a silently broken diagram. ASCII/tree and raw HTML flow are the only
  // two modes — both render offline with zero parse risk.
  /** ASCII/tree diagram, rendered monospace in a <pre>. The default visual mode. */
  ascii?: string;
  /** Complex decisions: raw HTML using .grill-fig / .flow / .flow-box, inserted intentionally. */
  visualHtml?: string;
}

interface GrillPayload {
  title?: string;
  contextHtml?: string;
  questions?: GrillQuestion[];
}

function renderQuestionVisual(q: GrillQuestion): string {
  const parts: string[] = [];
  if (q.ascii?.trim()) {
    parts.push(`<div class="grill-fig"><div class="grill-fig-cap">tree / shape</div><pre class="ascii">${esc(q.ascii)}</pre></div>`);
  }
  if (q.visualHtml?.trim()) {
    // visualHtml is intentionally raw: this local browser page is generated by the agent
    // so complex questions can use the .flow/.flow-box vocabulary instead of flattening
    // critical context into escaped prose.
    parts.push(q.visualHtml);
  }
  return parts.join("\n");
}

function grillPageHtml(payloadOrQuestions: GrillPayload | GrillQuestion[]): string {
  const payload: GrillPayload = Array.isArray(payloadOrQuestions)
    ? { questions: payloadOrQuestions }
    : payloadOrQuestions;
  const questions = payload.questions ?? [];
  const title = payload.title?.trim() || "A few questions before the plan";
  const nonce = nextNonce();
  const contextHtml = payload.contextHtml?.trim()
    ? `<section class="context card">${payload.contextHtml}</section>`
    : "";
  const blocks = questions
    .map(
      (q, i) => `
    <div class="pq grill-q">
      <div class="pq-text grill-q-text">Q${i + 1}. ${esc(q.question)}</div>
      ${renderQuestionVisual(q)}
      ${q.note ? `<div class="pq-note grill-q-note">${esc(q.note)}</div>` : ""}
      ${q.recommendation ? `<div class="pq-rec grill-q-rec">Recommended: ${esc(q.recommendation)}</div>` : ""}
    </div>`
    )
    .join("");

  return `<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
${desdocKeyMeta(nonce)}
<title>${esc(title)} — grill</title>
<style>
  *{box-sizing:border-box;margin:0;padding:0;}
  body{font-family:system-ui,-apple-system,sans-serif;background:#0d1017;color:#c8ccd4;
    padding:32px 24px 96px;line-height:1.5;max-width:860px;margin:0 auto;}
  h1{font-size:18px;color:#e6e9ef;margin-bottom:6px;}
  .sub{font-size:12px;color:#6b7280;margin-bottom:24px;}
  .pq{border:1px solid #2e3440;border-radius:8px;padding:16px 18px;margin:14px 0;background:#11151c;}
  .pq-text{font-size:14px;color:#e6e9ef;font-weight:600;margin-bottom:4px;}
  .pq-note{font-size:12px;color:#6b7280;margin-bottom:8px;}
  .pq-rec{font-size:12px;color:#98c379;}
  /* bullets/prose inside a question — keep it tight, never a wall of text */
  .pq ul,.pq ol{margin:4px 0 10px;padding-left:18px;}
  .pq li{font-size:12px;color:#c8ccd4;line-height:1.55;margin:2px 0;}
  .pq b,.pq strong{color:#e6e9ef;}
  .pq code{background:#0d1017;border:1px solid #1e222a;border-radius:3px;padding:0 4px;font-size:11px;color:#7ab4db;}
  /* ── diagram vocabulary (offline-safe; the LLM picks by complexity) ──
     default → ascii/tree in <pre>; complex → flow rows in raw HTML.
     Two modes only — no CDN-rendered diagram libs (they break silently). */
  .grill-fig{margin:8px 0 12px;background:#0b0e14;border:1px solid #1e222a;border-radius:6px;
    padding:12px 14px;overflow-x:auto;}
  .grill-fig-cap{font-size:10px;letter-spacing:.08em;text-transform:uppercase;color:#6b7280;margin-bottom:8px;}
  .grill-fig pre,pre.ascii{margin:0;font:12px/1.5 'JetBrains Mono',monospace;color:#c8ccd4;white-space:pre;}
  .flow{display:flex;flex-direction:column;gap:7px;}
  .flow-row{display:flex;align-items:center;gap:8px;flex-wrap:wrap;}
  .flow-box{border:1px solid #2e3440;border-radius:6px;padding:6px 11px;background:#141820;color:#c8ccd4;
    font-size:12px;line-height:1.4;white-space:nowrap;}
  .flow-box small{display:block;color:#6b7280;font-size:10px;white-space:normal;}
  .flow-box.in{border-color:#7aa87a;color:#98c379;background:#111a14;}    /* input  */
  .flow-box.sut{border-color:#d19a66;color:#d19a66;background:#1a1610;}   /* main   */
  .flow-box.out{border-color:#456a8a;color:#7ab4db;background:#0f151d;}   /* output */
  .flow-arrow{color:#6b7280;font-size:14px;}
  .flow-note{font-size:11px;color:#6b7280;margin-top:2px;}
  .chip{display:inline-block;font-size:10px;padding:1px 7px;border-radius:9999px;border:1px solid #2e3440;
    color:#a0a4ac;margin-right:4px;}
  .chip.in{border-color:#7aa87a;color:#98c379;} .chip.sut{border-color:#d19a66;color:#d19a66;}
  .chip.out{border-color:#456a8a;color:#7ab4db;}
</style></head>
<body>
  <h1>${esc(title)}</h1>
  <div class="sub">Answer by annotation: <b>+ Note</b> on a question → type → next note → <b>Copy Feedback</b> → paste it back into the Pi chat. No note on a question = the recommendation stands.</div>
  ${contextHtml}
  <div id="form">${blocks}</div>
${annotationToolkitHtml()}
</body></html>`;
}

// ─── HTTP request handler ─────────────────────────────────────────────────────

function handleRequest(req: http.IncomingMessage, res: http.ServerResponse): void {
  if (req.method === "GET" && req.url === "/") {
    res.writeHead(200, { "Content-Type": "text/html; charset=utf-8" });
    res.end(currentHtml);
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
    // A remote-reachable host is useless if the socket only accepts loopback.
    const bindAddr = isLocalOnly(resolveHost()) ? "127.0.0.1" : "0.0.0.0";
    s.listen(PORT, bindAddr, () => {
      server = s;
      resolve();
    });
  });
}

// ─── Core interactions (serve and return — never block on the browser) ─────────

async function serve(html: string): Promise<{ url: string; opened: boolean }> {
  currentHtml = html;
  await ensureServer();
  const opened = openBrowser();
  return { url: `http://${resolveHost()}:${PORT}/`, opened };
}

function serveNote(r: { url: string; opened: boolean }): string {
  return r.opened
    ? `Page is live at ${r.url} (a browser tab should have opened).`
    : `Page is live at ${r.url} — could NOT auto-open a browser; give the user this URL.`;
}

async function review(filePath: string, cwd: string): Promise<{ url: string; opened: boolean }> {
  const resolved = path.isAbsolute(filePath) ? filePath : path.join(cwd, filePath);
  if (!fs.existsSync(resolved)) {
    throw new Error(`plan file not found: ${resolved}`);
  }
  return serve(ensureAnnotable(fs.readFileSync(resolved, "utf-8")));
}

async function grill(payload: GrillPayload): Promise<{ url: string; opened: boolean }> {
  return serve(grillPageHtml(payload));
}

// ─── Extension entry ──────────────────────────────────────────────────────────

export default function (pi: ExtensionAPI) {
  // /do-planish sets these; before_agent_start then drives the agent through the
  // grill → build → serve-for-review flow until the plan is approved.
  let planMode = false;
  let planTopic = "";
  let planDir = ""; // absolute dir for plan.md + plan.html; resolved at /do-planish time

  pi.on("before_agent_start", async (event: any) => {
    if (!planMode) return;
    const topic = planTopic ? `The user wants to plan: ${planTopic}\n\n` : "";
    const planHtml = path.join(planDir, "plan.html");
    const planMd = path.join(planDir, "plan.md");
    // NOTE: planish grill->build->review prompt is intentionally DUPLICATED (not shared) in tf-implement.ts. Keep in sync with the Planish HTML Grill Contract.
    return {
      systemPrompt:
        event.systemPrompt +
        `\n\n${topic}You are helping the user create a PLAN with planish — produce a plan, not an implementation. Do NOT build or run anything unless the user explicitly asks after the plan is approved.\n\n` +
        "STEP 1 — GRILL: Call the planish_grill tool with title, contextHtml, and a batch of clarifying questions (scope, constraints, the real choices, unknowns, what already exists). Give each question a concrete recommended answer — the page is annotation-only, and a question with no note means the user accepted your recommendation. The tool serves the page and returns immediately: give the user the URL, END YOUR TURN, and wait for their pasted ## Feedback block. Do NOT make a plain Q&A-only grill. Write for the user: open contextHtml with a plain-English explanation of what the plan is trying to do and what you found so far; ask about the mechanism or design choice, never 'these files changed'; define every acronym at first use; file paths/method names/change lists go only in an Appendix section at the BOTTOM of the page. Visuals (two modes only — NEVER Mermaid): default is an ASCII/tree diagram in ascii; when ASCII can't carry it, visualHtml using .grill-fig/.flow/.flow-box drawn row by row. A diagram only when it genuinely helps — never for its own sake. If the pasted feedback raises new questions, call planish_grill again.\n\n" +
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
        `    Structure: page header (h1 + .subtitle), then h2 sections per phase with .card divs (.phase-num, .phase-title, task bullets, a Verification bullet at end). Include annotation controls before </body> (sticky notes + Copy Feedback / Finalize) and a unique <meta name="desdoc-key" content="<slug>-plan-v<n>"> in <head> so each plan version starts with a clean note slate. The annotation bar is the page's ONLY interactive control — no answer boxes, no submit buttons.\n` +
        `  • ${planMd} — the same plan as token-lean Markdown (the .md is the lean agent record, the .html is the visual/annotatable copy).\n` +
        `Both files hold the same plan content, and plan.md/plan.html always hold the latest. You do NOT freeze versions by hand: planish_submit_plan freezes the next plan-v<k>.md + plan-v<k>.html pair for you automatically whenever the submitted plan differs from the newest frozen pair (v1 on the first submit, incrementing on each changed revision). NEVER edit a frozen plan-v* file; the frozen plan-v* files show how the plan evolved.\n\n` +
        `STEP 3 — REVIEW: Call planish_submit_plan with the path ${planHtml}. It serves the plan in the browser and returns immediately — tell the user the page is ready, then END YOUR TURN. The user annotates and pastes feedback into the chat: a ## FINALIZED block (or an explicit approval message) means the plan is APPROVED; notes requesting changes mean revise BOTH files and call planish_submit_plan again (it auto-freezes the next plan-v<k> pair before serving). The approved plan is the deliverable.`,
    };
  });

  // ── planish_grill — ask a batch of questions before planning ──────────────

  (pi as any).registerTool({
    name: "planish_grill",
    label: "Grill Before Planning",
    description:
      "Ask the user a VISUAL, annotatable batch of questions in the browser BEFORE writing a plan. " +
      "ALWAYS grill first when planning with planish. Do not send a plain Q&A-only grill unless the user explicitly asked for terminal fallback. " +
      "Provide title and contextHtml so the page explains, in plain English, what is being decided and what you found — define acronyms at first use, and keep file paths/method names out of questions (Appendix at the bottom only). For each question give question, note, recommendation, and when useful a visual: ascii for a tree/shape (the default), visualHtml for complex .grill-fig/.flow/.flow-box diagrams. Never Mermaid. " +
      "The page is annotation-only (sticky notes + Copy Feedback — no answer boxes, no submit): the tool serves it and returns IMMEDIATELY. Give the user the URL, END YOUR TURN, and wait for their pasted ## Feedback block. A question with no note means your recommendation was accepted. If the feedback raises new questions, call planish_grill again. Once everything is resolved, write the plan to .md + .html and call planish_submit_plan.",
    parameters: {
      type: "object",
      properties: {
        title: {
          type: "string",
          description: "Short title for the grill page (e.g. '<topic> — grill v1').",
        },
        contextHtml: {
          type: "string",
          description: "Optional raw HTML context header: what is being planned, current shape, and decisions already locked. Use headings/tables/bullets, not a wall of text.",
        },
        questions: {
          type: "array",
          description: "The batch of questions to ask the user. Nontrivial questions should include ascii or visualHtml so the page is not plain Q&A-only.",
          items: {
            type: "object",
            properties: {
              question: { type: "string", description: "The question to ask." },
              note: { type: "string", description: "Optional: why this matters / context." },
              recommendation: { type: "string", description: "Your recommended answer. Strongly expected for every question — no note on the question means the user accepted it." },
              ascii: { type: "string", description: "ASCII/tree diagram for this question (the default visual mode)." },
              visualHtml: { type: "string", description: "Complex raw HTML visual using .grill-fig / .flow / .flow-box, drawn row by row. Use when ASCII can't carry it. Never Mermaid." },
            },
            required: ["question"],
          },
        },
      },
      required: ["questions"],
    } as any,

    async execute(
      _id: string,
      params: GrillPayload,
      _signal: AbortSignal,
      _onUpdate: any,
      _ctx: any
    ) {
      const questions = Array.isArray(params?.questions) ? params!.questions! : [];
      if (questions.length === 0) {
        return { content: [{ type: "text", text: "Error: provide at least one question." }] };
      }
      try {
        const served = await grill({ title: params?.title, contextHtml: params?.contextHtml, questions });
        return {
          content: [{
            type: "text",
            text:
              `Grill round served. ${serveNote(served)}\n\n` +
              "Now tell the user the grill is ready and END YOUR TURN — do not proceed in this turn. " +
              "The user will annotate the page (+ Note on a question → type → next note), click Copy Feedback, and paste the ## Feedback block here. " +
              "Each note is tagged with the nearest question/heading; a question with no note means your recommendation was accepted. " +
              "If the feedback raises new questions, call planish_grill again with the next round; otherwise write the plan (plan.md + plan.html) and call planish_submit_plan.",
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

  // ── planish_submit_plan — serve the plan for review ───────────────────────

  (pi as any).registerTool({
    name: "planish_submit_plan",
    label: "Submit Plan for Review",
    description:
      "Serve a plan HTML file for human review in the browser. " +
      "Grill the user first with planish_grill — do not write the plan until the open questions are answered. " +
      // # dup 1 (plan-html-style)
      "Write your plan to a .html file: a title, a summary of phases, key decisions, and verification steps. " +
      "Use the v3 dark style (NO Tailwind CDN) — body background #0d1017, JetBrains Mono + IBM Plex Sans fonts, " +
      ".card divs with colored left-border accents for each phase, annotation controls before </body>. " +
      "Then call this tool with the file path. It serves the page and returns IMMEDIATELY — tell the user, then END YOUR TURN. " +
      "This tool auto-freezes plan versions for you: before serving, it snapshots the current plan.html/plan.md as the next plan-v<k>.md + plan-v<k>.html pair whenever they differ from the newest frozen pair (never edit a frozen plan-v* file yourself). " +
      "The user annotates and pastes feedback into the chat: a ## FINALIZED block (or explicit approval) means APPROVED; " +
      "notes requesting changes mean revise the plan and call this tool again.",
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
      _onUpdate: any,
      ctx: any
    ) {
      const filePath = (params?.filePath ?? "").trim();
      if (!filePath) {
        return { content: [{ type: "text", text: "Error: filePath is required." }] };
      }
      try {
        const base = planDir || (ctx?.cwd ?? process.cwd());
        // Tool-enforced versioning: freeze the current plan as the next plan-v<k>
        // pair (only when it changed) BEFORE serving. The agent never freezes by hand.
        const resolvedPlan = path.isAbsolute(filePath) ? filePath : path.join(base, filePath);
        const freeze = fs.existsSync(resolvedPlan) ? autoFreezePlan(resolvedPlan) : null;
        const served = await review(filePath, base);
        // The drive-the-flow prompt has done its job once the plan is up for
        // review; the revise-and-resubmit loop is guided by this result text.
        planMode = false;
        const freezeNote = freeze?.froze
          ? `Froze plan-v${freeze.version} (immutable snapshot) before serving. `
          : "";
        return {
          content: [{
            type: "text",
            text:
              `Plan review page served. ${freezeNote}${serveNote(served)}\n\n` +
              "Tell the user the plan is ready for review, then END YOUR TURN — do not proceed in this turn. " +
              "The user will annotate the page and paste feedback here: a ## FINALIZED block (or an explicit approval message) means the plan is APPROVED and planning is done. " +
              `Notes requesting changes mean: revise BOTH files (${filePath} and its .md twin) and call planish_submit_plan again with the same path — it auto-freezes the next plan-v<k>.md + plan-v<k>.html pair for you before serving (never edit a frozen plan-v* file). ` +
              "This is still a PLANNING session — do not start implementing unless the user explicitly asks after approval.",
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

  // ── /do-planish [description] — START a planning session ──────────────────
  //
  // Default use: /do-planish <what you want to plan>. Turns on planMode; the
  // before_agent_start hook then drives the agent: grill → build plan.html →
  // serve for browser review, iterating on pasted feedback until approved.
  // Escape hatch: /do-planish --review <path> re-opens an existing plan.html.

  (pi as any).registerCommand("do-planish", {
    description: "Start a standalone Pi planning session: /do-planish <what to plan> — grills you in an annotatable browser page (+ Note → Copy Feedback → paste back), builds a visual HTML plan, iterates until you approve. Re-open an existing plan with: /do-planish --review <path>.",
    handler: async (args: string, ctx: any) => {
      const trimmed = args.trim();

      // Escape hatch — re-open an existing plan for review.
      const reviewMatch = trimmed.match(/^--review\s+(.+)$/);
      if (reviewMatch) {
        const filePath = reviewMatch[1].trim();
        try {
          const served = await review(filePath, ctx?.cwd ?? process.cwd());
          ctx.ui.notify(
            `do-planish: ${filePath} is up for review at ${served.url}` +
              (served.opened ? "" : " (could not auto-open a browser — open the URL yourself)") +
              " — annotate (+ Note), click Copy Feedback, and paste the block into this chat. Finalize ✓ copies an approval block.",
            "info"
          );
        } catch (err) {
          ctx.ui.notify(`do-planish: ${err instanceof Error ? err.message : String(err)}`, "error");
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
        ctx.ui.notify(`do-planish: ${err instanceof Error ? err.message : String(err)}`, "error");
        return;
      }
      planMode = true;
      planTopic = topic;
      ctx.ui.notify(
        topic
          ? `do-planish: planning "${topic}" — Pi will grill you in the browser (annotate → Copy Feedback → paste back), then build a visual plan in ${planDir} for your review.`
          : `do-planish: planning mode on — tell Pi what you want to plan. It will grill you in the browser (annotate → Copy Feedback → paste back), then build a visual plan in ${planDir} for review.`,
        "info"
      );
    },
  });
}
