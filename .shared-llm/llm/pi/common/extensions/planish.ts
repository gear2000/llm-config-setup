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
 * Slash cmd: /planish <what to plan>   — START a Pi-native planning session: turns on
 *                planMode so before_agent_start drives the agent through
 *                browser grill → build plan.html → submit-for-review, until approved.
 *            /planish --review <path>  — re-open an existing plan.html for review.
 *
 * Note: standalone markdown skill variants (/do-planish and /cc-planish) are intentionally
 * removed. /planish is the standalone Pi planner; /do-plan-and-grill is the workflow-suite planner.
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

// Returns false when the opener is missing or exits non-zero (headless box,
// no default browser). Callers MUST surface that — a silent failure here left
// the user staring at a blocked "working…" spinner with no idea a page was
// waiting for them at localhost:4390.
function openBrowser(): boolean {
  const cmd = process.platform === "darwin" ? "open" : "xdg-open";
  const r = spawnSync(cmd, [`http://localhost:${PORT}/`], { detached: true, stdio: "ignore" });
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

// Every grill round is served at the SAME URL (localhost:4390/), so keying the
// sticky-note storage by pathname alone made round 2 load round 1's notes.
// Each rendered page gets a fresh nonce baked into its storage key, and the
// page prunes every other planish_notes__ key on load — new round, clean slate.
let grillRoundCounter = 0;

function grillFormHtml(payloadOrQuestions: GrillPayload | GrillQuestion[]): string {
  const roundKey = `${Date.now().toString(36)}r${++grillRoundCounter}`;
  const payload: GrillPayload = Array.isArray(payloadOrQuestions)
    ? { questions: payloadOrQuestions }
    : payloadOrQuestions;
  const questions = payload.questions ?? [];
  const title = payload.title?.trim() || "A few questions before the plan";
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
      <textarea class="pq-a grill-a" data-i="${i}" placeholder="Your answer…"></textarea>
    </div>`
    )
    .join("");

  return `<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
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
  .pq-rec{font-size:12px;color:#98c379;margin-bottom:10px;}
  .pq-a{width:100%;min-height:60px;background:#0d1017;border:1px solid #2e3440;border-radius:6px;
    padding:9px 11px;color:#c8ccd4;font:12px/1.5 'JetBrains Mono',monospace;resize:vertical;outline:none;}
  .pq-a:focus{border-color:#456a8a;}
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
  #bar{position:fixed;bottom:0;left:0;right:0;background:#0d1017;border-top:1px solid #1e222a;
    padding:8px 16px;display:flex;gap:8px;align-items:center;justify-content:flex-end;z-index:9999;
    font-family:'JetBrains Mono',monospace;font-size:12px;color:#6b7280;}
  .pbtn{padding:5px 11px;border-radius:5px;border:1px solid #2e3440;background:#0d1017;color:#c8ccd4;
    cursor:pointer;font-size:11px;font-family:'JetBrains Mono',monospace;white-space:nowrap;}
  .pbtn.copy{background:#1a2d4a;border-color:#456a8a;color:#7ab4db;}
  .pbtn.fin{background:#0f1f14;border-color:#3a5a2a;color:#98c379;}
  #done{display:none;text-align:center;color:#98c379;font-size:13px;padding:40px;}
  #desdoc-cnt{background:#2e3440;color:#c8ccd4;padding:1px 6px;border-radius:9999px;font-size:10px;margin-left:2px;}
  #desdoc-badge{display:none;position:fixed;top:0;left:0;right:0;background:#0f1f14;border-bottom:1px solid #3a5a2a;color:#98c379;text-align:center;padding:5px 16px;font-size:11px;z-index:10000;}
  .sticky-note{position:absolute;z-index:9000;min-width:180px;max-width:260px;background:#1c1a10;border:1px solid #8a6a1a;border-radius:4px;box-shadow:0 2px 8px rgba(0,0,0,.4);}
  .sticky-head{background:#8a6a1a;padding:3px 8px;cursor:move;display:flex;justify-content:space-between;align-items:center;border-radius:4px 4px 0 0;font:11px/20px 'JetBrains Mono',monospace;color:#1a1208;user-select:none;}
  .sticky-note textarea{width:100%;border:none;background:transparent;padding:6px 8px;font:12px/1.5 'JetBrains Mono',monospace;color:#c8ccd4;resize:vertical;min-height:56px;box-sizing:border-box;outline:none;}
</style></head>
<body>
  <div id="desdoc-badge">✓ FINALIZED — summary copied to clipboard</div>
  <h1>${esc(title)}</h1>
  <div class="sub">Answer in the page, add sticky notes if useful, then Copy Answers. Blanks are fine — they come back as skipped.</div>
  ${contextHtml}
  <div id="form">${blocks}</div>
  <div id="done">Answers submitted — you can close this tab.</div>
  <div id="bar">
    <span style="color:#3e4450;margin-right:auto;">planish grill</span>
    <button class="pbtn" onclick="ddAdd()">+ Note</button>
    <button class="pbtn" onclick="ddToggle()">Notes <span id="desdoc-cnt">0</span></button>
    <button class="pbtn copy" id="copybtn" onclick="planishCopyAnswers()">Copy Answers</button>
    <button class="pbtn copy" id="fbbtn" onclick="ddCopy()">Copy Feedback</button>
    <button class="pbtn fin" onclick="ddFinalize()">Finalize ✓</button>
    <button class="pbtn fin" onclick="planishGrillSend()">Submit Answers</button>
  </div>
<script>
(function(){
  function execCopy(text){var ta=document.createElement('textarea');ta.value=text;ta.setAttribute('readonly','');ta.style.position='fixed';ta.style.top='-1000px';document.body.appendChild(ta);ta.select();try{document.execCommand('copy');}catch(e){}document.body.removeChild(ta);}
  function writeCopy(text){if(navigator.clipboard&&navigator.clipboard.writeText){return navigator.clipboard.writeText(text).catch(function(){execCopy(text);});}execCopy(text);return Promise.resolve();}
  window.planishAnswerMarkdown=function(){
    var out='## Answers — '+document.title+'\\nFile: '+location.pathname+'\\n';
    document.querySelectorAll('.pq').forEach(function(q,i){var qt=q.querySelector('.pq-text').textContent.trim();var a=q.querySelector('.pq-a');out+='\\n### Q'+(i+1)+': '+qt+'\\n'+((a&&a.value.trim())||'(skipped)')+'\\n';});
    return out;
  };
  window.planishCopyAnswers=function(){writeCopy(window.planishAnswerMarkdown()).then(function(){var b=document.getElementById('copybtn');var t=b.textContent;b.textContent='Copied ✓';setTimeout(function(){b.textContent=t;},1600);});};
  window.planishGrillSend=async function(){
    const answers=[];document.querySelectorAll('.pq-a').forEach(function(t){answers[parseInt(t.dataset.i)]=t.value.trim();});
    document.getElementById('form').style.display='none';document.getElementById('done').style.display='block';
    await fetch('/grill-respond',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({answers:answers})}).catch(function(){});
  };
  const KEY='planish_notes__${roundKey}';let notes=[],ctr=0,vis=true;
  try{Object.keys(localStorage).forEach(function(k){if(k.indexOf('planish_notes__')===0&&k!==KEY)localStorage.removeItem(k);});}catch(e){}
  function save(){localStorage.setItem(KEY,JSON.stringify(notes.map(function(n){return {id:n.id,x:parseFloat(n.el.style.left),y:parseFloat(n.el.style.top),text:n.el.querySelector('textarea').value};})));var c=document.getElementById('desdoc-cnt');if(c)c.textContent=notes.length;}
  function mk(x,y,id,text){id=id||String(++ctr);var el=document.createElement('div');el.className='sticky-note';el.style.left=x+'px';el.style.top=y+'px';el.innerHTML='<div class="sticky-head"><span>Note '+id+'</span><span onclick="ddDel(\\''+id+'\\')" style="cursor:pointer;font-weight:700;padding:0 3px;color:#3a2808;">✕</span></div><textarea placeholder="Add note…">'+(text||'')+'</textarea>';document.body.appendChild(el);el.querySelector('textarea').addEventListener('input',save);var h=el.firstElementChild;h.addEventListener('mousedown',function(e){if(e.target.onclick)return;var ox=e.clientX-el.getBoundingClientRect().left,oy=e.clientY-el.getBoundingClientRect().top;var mv=function(e2){el.style.left=(e2.clientX-ox+scrollX)+'px';el.style.top=(e2.clientY-oy+scrollY)+'px';};var up=function(){save();removeEventListener('mousemove',mv);removeEventListener('mouseup',up);};addEventListener('mousemove',mv);addEventListener('mouseup',up);e.preventDefault();});notes.push({id:id,el:el});save();}
  window.ddDel=function(id){var i=notes.findIndex(function(n){return n.id===id;});if(i<0)return;notes[i].el.remove();notes.splice(i,1);save();};
  window.ddAdd=function(){document.body.style.cursor='crosshair';var h=function(e){if(e.target.closest('#bar'))return;document.body.style.cursor='';removeEventListener('click',h);mk(e.pageX-90,e.pageY-20,null,'');};addEventListener('click',h);};
  window.ddToggle=function(){vis=!vis;notes.forEach(function(n){n.el.style.display=vis?'':'none';});};
  window.ddCopy=function(){var items=notes.map(function(n){return '- [Note '+n.id+'] '+(n.el.querySelector('textarea').value||'(empty)');}).join('\\n');writeCopy('## Feedback — '+document.title+'\\nFile: '+location.pathname+'\\n\\n'+(items||'(no notes)')).then(function(){var b=document.getElementById('fbbtn');var t=b.textContent;b.textContent='Copied ✓';setTimeout(function(){b.textContent=t;},1600);});};
  window.ddFinalize=function(){if(!confirm('Mark this grill round finalized?'))return;document.getElementById('desdoc-badge').style.display='block';window.ddCopy();};
  var saved=JSON.parse(localStorage.getItem(KEY)||'[]');if(saved.length)ctr=saved.reduce(function(m,n){return Math.max(m,parseInt(n.id)||0);},0);saved.forEach(function(n){mk(n.x,n.y,n.id,n.text);});
})();
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

// Wait for the browser to POST back, OR for the tool call to be aborted from the
// TUI. Without the abort path, a grill page that never submits (wrong page open,
// browser failed to launch, user walked away) left the tool call — and the whole
// Pi session — blocked forever, with pendingResolve wedged so even the next
// planish call failed with "already in progress". Abort resolves `null`, clears
// the lock, and the execute() handlers translate that into a clear cancellation
// message so the agent can fall back to chat.
function awaitBrowser<T>(signal?: AbortSignal, onStatus?: (text: string) => void): Promise<T | null> {
  return new Promise((resolve) => {
    const onAbort = () => {
      if (pendingResolve === wrapped) pendingResolve = null;
      resolve(null);
    };
    const wrapped = (v: T) => {
      signal?.removeEventListener("abort", onAbort);
      resolve(v);
    };
    pendingResolve = wrapped as (v: unknown) => void;
    signal?.addEventListener("abort", onAbort, { once: true });
    const opened = openBrowser();
    onStatus?.(
      (opened ? "" : "Could not open a browser automatically. ") +
        `Page ready at http://localhost:${PORT}/ — waiting for you in the browser. ` +
        "If no tab appeared, open that URL yourself. Esc cancels."
    );
  });
}

async function review(
  filePath: string,
  cwd: string,
  signal?: AbortSignal,
  onStatus?: (text: string) => void
): Promise<{ approved: boolean; feedback: string } | null> {
  const resolved = path.isAbsolute(filePath) ? filePath : path.join(cwd, filePath);
  if (!fs.existsSync(resolved)) {
    throw new Error(`plan file not found: ${resolved}`);
  }
  if (pendingResolve) {
    throw new Error("a planish interaction is already in progress — wait for it to complete (or abort it from the TUI)");
  }
  currentHtml = withToolbar(fs.readFileSync(resolved, "utf-8"));
  await ensureServer();
  return awaitBrowser<{ approved: boolean; feedback: string }>(signal, onStatus);
}

async function grill(
  payload: GrillPayload,
  signal?: AbortSignal,
  onStatus?: (text: string) => void
): Promise<string[] | null> {
  if (pendingResolve) {
    throw new Error("a planish interaction is already in progress — wait for it to complete (or abort it from the TUI)");
  }
  currentHtml = grillFormHtml(payload);
  await ensureServer();
  return awaitBrowser<string[]>(signal, onStatus);
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
    // NOTE: planish grill->build->review prompt is intentionally DUPLICATED (not shared) in tf-implement.ts. Keep in sync with the Planish HTML Grill Contract.
    return {
      systemPrompt:
        event.systemPrompt +
        `\n\n${topic}You are helping the user create a PLAN with planish — produce a plan, not an implementation. Do NOT build or run anything unless the user explicitly asks after the plan is approved.\n\n` +
        "STEP 1 — GRILL: Call the planish_grill tool with title, contextHtml, and a batch of clarifying questions (scope, constraints, the real choices, unknowns, what already exists). Give each question your recommended answer. Do NOT make a plain Q&A-only grill. Write for the user: open contextHtml with a plain-English explanation of what the plan is trying to do and what you found so far; ask about the mechanism or design choice, never 'these files changed'; define every acronym at first use; file paths/method names/change lists go only in an Appendix section at the BOTTOM of the page. Visuals (two modes only — NEVER Mermaid): default is an ASCII/tree diagram in ascii; when ASCII can't carry it, visualHtml using .grill-fig/.flow/.flow-box drawn row by row. A diagram only when it genuinely helps — never for its own sake. If the answers raise new questions, call planish_grill again.\n\n" +
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
        `    Structure: page header (h1 + .subtitle), then h2 sections per phase with .card divs (.phase-num, .phase-title, task bullets, a Verification bullet at end). Include annotation controls before </body> (sticky notes + Copy Feedback / finalize) so the saved plan remains annotatable after review.\n` +
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
      "Ask the user a VISUAL, annotatable batch of questions in the browser BEFORE writing a plan. " +
      "ALWAYS grill first when planning with planish. Do not send a plain Q&A-only form unless the user explicitly asked for terminal fallback. " +
      "Provide title and contextHtml so the page explains, in plain English, what is being decided and what you found — define acronyms at first use, and keep file paths/method names out of questions (Appendix at the bottom only). For each question give question, note, recommendation, and when useful a visual: ascii for a tree/shape (the default), visualHtml for complex .grill-fig/.flow/.flow-box diagrams. Never Mermaid. " +
      "The browser page includes Copy Answers plus sticky-note annotation/feedback controls. If the answers raise new questions, call planish_grill again. Once everything is resolved, write the plan to .md + .html and call planish_submit_plan.",
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
              recommendation: { type: "string", description: "Your recommended answer. Strongly expected for every question." },
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
      signal: AbortSignal,
      onUpdate: any,
      _ctx: any
    ) {
      const questions = Array.isArray(params?.questions) ? params!.questions! : [];
      if (questions.length === 0) {
        return { content: [{ type: "text", text: "Error: provide at least one question." }] };
      }
      // Stream the URL into the TUI the moment the wait starts. This blocking
      // call used to be completely silent — if the browser tab failed to open,
      // the user saw "working…" and nothing else, forever.
      const onStatus = (text: string) => onUpdate?.({ content: [{ type: "text", text }] });
      try {
        const answers = await grill({ title: params?.title, contextHtml: params?.contextHtml, questions }, signal, onStatus);
        if (answers === null) {
          return {
            content: [{
              type: "text",
              text:
                "Grill cancelled from the TUI before Submit Answers was clicked — no answers received. " +
                "The TUI is unblocked now. Ask the user how to proceed: re-run planish_grill (the page is at http://localhost:4390/ and only its Submit Answers button returns answers), or take answers pasted directly in chat.",
            }],
          };
        }
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
      signal: AbortSignal,
      onUpdate: any,
      ctx: any
    ) {
      const filePath = (params?.filePath ?? "").trim();
      if (!filePath) {
        return { content: [{ type: "text", text: "Error: filePath is required." }] };
      }
      const onStatus = (text: string) => onUpdate?.({ content: [{ type: "text", text }] });
      try {
        const result = await review(filePath, planDir || (ctx?.cwd ?? process.cwd()), signal, onStatus);
        if (result === null) {
          return {
            content: [{
              type: "text",
              text:
                "Plan review cancelled from the TUI before the user approved or requested changes. " +
                "The TUI is unblocked now. Ask the user how to proceed: call planish_submit_plan again to re-open the review page, or take their verdict directly in chat.",
            }],
          };
        }
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
    description: "Start a standalone Pi planning session: /planish <what to plan> — grills you in an annotatable browser page, builds a visual HTML plan, iterates until you approve. Re-open an existing plan with: /planish --review <path>.",
    handler: async (args: string, ctx: any) => {
      const trimmed = args.trim();

      // Escape hatch — re-open an existing plan for review.
      const reviewMatch = trimmed.match(/^--review\s+(.+)$/);
      if (reviewMatch) {
        const filePath = reviewMatch[1].trim();
        try {
          ctx.ui.notify(`planish: opening ${filePath} for review…`, "info");
          const result = await review(filePath, ctx?.cwd ?? process.cwd(), undefined, (text) =>
            ctx.ui.notify(`planish: ${text}`, "info")
          );
          if (result === null) {
            ctx.ui.notify("planish: review cancelled", "warning");
            return;
          }
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
