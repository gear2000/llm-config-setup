/**
 * memsearch — give Pi memory over the SAME shared store Claude Code builds (RECALL + CAPTURE).
 *
 * Reads and writes the same per-project store as Claude's memsearch plugin: daily markdown
 * logs under `<git-root>/.memsearch/memory/<YYYY-MM-DD>.md`, indexed into a per-project
 * Milvus collection whose name is derived to match memsearch's scripts/derive-collection.sh
 * exactly, so Pi and Claude converge on the same collection for a given repo.
 *
 *   RECALL — full parity. Same shared store, same `memsearch` CLI as Claude:
 *     - `memory_search` tool  + `/recall <query>`        → ranked hits from past memory
 *     - `memory_expand` tool  + `/recall-expand <hash>`  → full markdown section for a hit
 *     - on session_start: inject a one-line hint naming how many past daily logs exist
 *       and their date range, nudging the model to call memory_search.
 *
 *   CAPTURE — NOT full parity, by design. On agent_end (once per user prompt) it writes
 *     DETERMINISTIC third-person notes (what the user asked, which tools the agent used,
 *     a clipped agent reply) — deliberately LIGHTER than Claude's / the memsearch codex
 *     reference plugin's default, which run an LLM to summarize the turn. The deterministic
 *     path is chosen so capture never makes a blocking nested LLM call: it appends
 *     `### HH:MM` + a `<!-- session: turn: transcript: -->` anchor + the notes to today's
 *     daily md synchronously, then fires `memsearch index` in a DETACHED child.
 *     - markdown is the source of truth; the index is derived. If the Milvus Lite
 *       single-writer lock is contended (Claude indexing the same store concurrently),
 *       the detached child retries that condition only; on exhaustion it fails LOUD
 *       (writes to ~/.pi/agent/memsearch-index.log + drops an `index-deferred` breadcrumb
 *       in the daily md) and the next session_start re-index catches it up. Non-lock
 *       errors fail loud immediately. Nothing is lost — the markdown was already written.
 *
 * OWN extension: authored here, symlinked into ~/.pi/agent/extensions/ by setup-pi.sh
 * (auto-loaded). NOT a `pi install` third-party — never in third-party-extensions.txt.
 */

import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { Type } from "typebox";
import * as fs from "node:fs";
import * as path from "node:path";
import * as os from "node:os";
import * as crypto from "node:crypto";
import { execFile } from "node:child_process";
import { deriveCollection } from "./collection.ts";

// ─── Config ──────────────────────────────────────────────────────────────────
const TOP_K_DEFAULT = 5;
const SUMMARY_MAX_CHARS = 4000; // cap turn text fed to the summarizer (ARG_MAX + cost guard)
const INDEX_LOCK_RETRIES = 4; // attempts when the Milvus Lite db lock is contended
const INDEX_LOCK_BACKOFF_MS = 750; // base backoff, grows linearly per attempt
const EXEC_TIMEOUT_MS = 120_000; // memsearch search/expand can be slow on a cold ONNX model
const INDEX_LOG = path.join(os.homedir(), ".pi", "agent", "memsearch-index.log"); // detached-index failures land here

// A contended Milvus Lite db file (Claude indexing the same store at the same time) is a
// DOCUMENTED single-writer condition, not a bug — retry it. Anything that does NOT match
// these signatures is a real failure and must surface, never be swallowed. Kept as a
// POSIX extended-regex string because the retry loop runs in a detached shell child.
const LOCK_SIGNATURE_ERE = "lock|locked|in use|another process|database is locked|RESOURCE_EXHAUSTED|cannot open|deadline|unavailable|busy";

// ─── memsearch CLI resolution ────────────────────────────────────────────────
// Prefer `memsearch` on PATH; else `uvx --from memsearch[onnx] memsearch`.
interface Cli {
  bin: string;
  base: string[];
}
let _cli: Cli | null | undefined;

function resolveCli(): Cli | null {
  if (_cli !== undefined) return _cli;
  // Make sure the usual user bin dirs are visible (Pi may launch from a minimal env).
  const extra = [
    path.join(os.homedir(), ".local", "bin"),
    path.join(os.homedir(), ".cargo", "bin"),
    "/usr/local/bin",
  ];
  const parts = (process.env.PATH ?? "").split(path.delimiter);
  for (const p of extra) if (p && !parts.includes(p)) parts.push(p);
  process.env.PATH = parts.join(path.delimiter);

  if (which("memsearch")) _cli = { bin: "memsearch", base: [] };
  else if (which("uvx")) _cli = { bin: "uvx", base: ["--from", "memsearch[onnx]", "memsearch"] };
  else _cli = null;
  return _cli;
}

function which(bin: string): string | null {
  for (const dir of (process.env.PATH ?? "").split(path.delimiter)) {
    if (!dir) continue;
    const full = path.join(dir, bin);
    try {
      fs.accessSync(full, fs.constants.X_OK);
      return full;
    } catch {
      /* not here */
    }
  }
  return null;
}

interface ExecResult {
  stdout: string;
  stderr: string;
  code: number;
}

// Promise wrapper over execFile — no shell, args passed as an array (no injection).
function run(bin: string, args: string[], signal?: AbortSignal): Promise<ExecResult> {
  return new Promise((resolve, reject) => {
    execFile(
      bin,
      args,
      { signal, timeout: EXEC_TIMEOUT_MS, maxBuffer: 32 * 1024 * 1024 },
      (err, stdout, stderr) => {
        if (err && (err as NodeJS.ErrnoException).code === "ENOENT") {
          reject(err);
          return;
        }
        const code = err && typeof (err as any).code === "number" ? (err as any).code : err ? 1 : 0;
        resolve({ stdout: stdout ?? "", stderr: stderr ?? "", code });
      },
    );
  });
}

function memsearch(args: string[], signal?: AbortSignal): Promise<ExecResult> {
  const cli = resolveCli();
  if (!cli) return Promise.reject(new Error("memsearch CLI not found (need `memsearch` on PATH or `uvx`)"));
  return run(cli.bin, [...cli.base, ...args], signal);
}

// ─── Collection + paths (must match memsearch/scripts/derive-collection.sh) ───
function gitRoot(cwd: string): string {
  try {
    const r = require("node:child_process").execFileSync("git", ["-C", cwd, "rev-parse", "--show-toplevel"], {
      encoding: "utf-8",
      stdio: ["ignore", "pipe", "ignore"], // swallow git's "fatal: not a git repository" stderr
    });
    const top = String(r).trim();
    if (top) return top;
  } catch {
    /* not a git repo — fall through to cwd */
  }
  return path.resolve(cwd);
}

interface Project {
  root: string;
  memoryDir: string;
  collection: string;
}

function projectFor(cwd: string): Project {
  const root = gitRoot(cwd);
  // Honor MEMSEARCH_DIR (global scope) the way the codex plugin does.
  const explicit = process.env.MEMSEARCH_DIR;
  const memsearchDir = explicit && explicit.trim() ? explicit : path.join(root, ".memsearch");
  return {
    root,
    memoryDir: path.join(memsearchDir, "memory"),
    collection: deriveCollection(explicit && explicit.trim() ? memsearchDir : root),
  };
}

// ─── Detached re-index ───────────────────────────────────────────────────────
// CAPTURE writes the markdown synchronously (source of truth), then fires the index in
// a DETACHED child so it never blocks the turn or process shutdown — the async model the
// codex plugin uses. The child captures each attempt's output and:
//   - exits 0 on success;
//   - retries ONLY the documented Milvus Lite lock condition (Claude indexing the same
//     store concurrently), with linear backoff;
//   - on a NON-lock error, fails LOUD immediately — appends the error to the index log
//     and exits 1 (no swallowing);
//   - on lock-retry exhaustion, fails LOUD — appends a line to the index log, drops a
//     `<!-- index-deferred: <ISO> reason=lock -->` breadcrumb into the daily md so the
//     deferral is visible in the source of truth, and exits 1.
// The markdown is already saved either way, and session_start re-indexes on next startup,
// so a deferred index always catches up. `memFile` is the daily-log file just written.
function spawnDetachedIndex(proj: Project, memFile: string): void {
  const cli = resolveCli();
  if (!cli) return;
  // memsearch argv as single-quoted positional args (quoting-safe).
  const sq = (a: string) => `'${a.replace(/'/g, "'\\''")}'`;
  const idxArgs = [...cli.base, "index", proj.memoryDir, "--collection", proj.collection].map(sq).join(" ");
  const backoff = (INDEX_LOCK_BACKOFF_MS / 1000).toFixed(2);
  const logF = sq(INDEX_LOG);
  const memF = sq(memFile);
  const coll = sq(proj.collection);
  // POSIX sh loop. Capture combined output per attempt; branch on lock-signature.
  const script = [
    `n=0`,
    `while [ "$n" -lt ${INDEX_LOCK_RETRIES} ]; do`,
    `  out="$(${cli.bin} ${idxArgs} 2>&1)" && exit 0`,
    `  if printf '%s' "$out" | grep -qiE ${sq(LOCK_SIGNATURE_ERE)}; then`,
    `    n=$((n+1)); sleep ${backoff}; continue`,
    `  fi`,
    // Non-lock failure → surface immediately, do not retry, do not swallow.
    `  printf '%s memsearch index FAILED (non-lock) collection=%s: %s\\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" ${coll} "$out" >> ${logF}`,
    `  exit 1`,
    `done`,
    // Lock never cleared after all retries → loud deferral: log + breadcrumb + exit 1.
    `ts="$(date -u +%Y-%m-%dT%H:%M:%SZ)"`,
    `printf '%s memsearch index DEFERRED collection=%s reason=lock after ${INDEX_LOCK_RETRIES} tries; markdown saved, session_start will re-index\\n' "$ts" ${coll} >> ${logF}`,
    `printf '<!-- index-deferred: %s reason=lock -->\\n' "$ts" >> ${memF}`,
    `exit 1`,
  ].join("\n");
  try {
    const child = require("node:child_process").spawn("bash", ["-c", script], {
      detached: true,
      stdio: "ignore",
    });
    child.unref(); // let Pi exit without waiting for the index
  } catch {
    // spawn itself failed (e.g. no bash) — record it; markdown is saved and
    // session_start re-indexes next startup, so nothing is lost.
    try {
      fs.appendFileSync(INDEX_LOG, `${new Date().toISOString()} memsearch index spawn failed (no bash?)\n`);
    } catch {
      /* index log unwritable too — out of options; markdown remains the source of truth */
    }
  }
}

// ─── Turn summary (third-person notes, built deterministically) ──────────────
// CAPTURE must not block the turn's shutdown, so we do NOT make a nested LLM call on
// the hot path (a headless `pi -p` summarize costs ~15-20s and would stall every
// turn, especially in `-p` mode where Pi waits for agent_end to finish before exit).
// Instead we build concise factual notes straight from the turn — the same shape the
// memsearch codex plugin falls back to: what the user asked + what the agent did,
// plus the tools it called. This is fast, deterministic, and indexable.
function buildSummary(turn: { userQuestion: string; agentText: string; toolCalls: string[] }): string {
  const bullets: string[] = [];
  if (turn.userQuestion) bullets.push(`- User asked: ${turn.userQuestion}`);
  if (turn.toolCalls.length) {
    const uniq = [...new Set(turn.toolCalls)].slice(0, 8);
    bullets.push(`- Agent used tools: ${uniq.join(", ")}`);
  }
  if (turn.agentText) bullets.push(`- Agent: ${turn.agentText.replace(/\s+/g, " ").slice(0, 800)}`);
  if (bullets.length === 0) return "- (turn captured; no extractable text)";
  return bullets.join("\n");
}

interface Turn {
  userQuestion: string;
  agentText: string;
  toolCalls: string[];
}

// Extract the last turn (from the last user message to the end) from session entries.
function lastTurn(entries: any[]): Turn {
  let startIdx = -1;
  for (let i = entries.length - 1; i >= 0; i--) {
    const e = entries[i];
    if (e?.type === "message" && e.message?.role === "user") {
      startIdx = i;
      break;
    }
  }
  const slice = startIdx >= 0 ? entries.slice(startIdx) : entries;
  let userQuestion = "";
  const agentParts: string[] = [];
  const toolCalls: string[] = [];

  for (const e of slice) {
    if (e?.type !== "message") continue;
    const m = e.message;
    const text = contentToText(m?.content);
    if (m?.role === "user") {
      if (text.trim() && !userQuestion) userQuestion = text.trim().split("\n")[0].slice(0, 200);
    } else if (m?.role === "assistant") {
      if (text.trim()) agentParts.push(text.trim());
      for (const name of toolNames(m?.content)) toolCalls.push(name);
    }
  }
  let agentText = agentParts.join(" ");
  if (agentText.length > SUMMARY_MAX_CHARS) agentText = `${agentText.slice(0, SUMMARY_MAX_CHARS)}...`;
  return { userQuestion, agentText, toolCalls };
}

function contentToText(content: any): string {
  if (typeof content === "string") return content;
  if (!Array.isArray(content)) return "";
  const out: string[] = [];
  for (const block of content) {
    if (typeof block === "string") out.push(block);
    else if (block?.type === "text" && typeof block.text === "string") out.push(block.text);
  }
  return out.join("\n");
}

function toolNames(content: any): string[] {
  if (!Array.isArray(content)) return [];
  const names: string[] = [];
  for (const block of content) {
    if ((block?.type === "tool_use" || block?.type === "toolUse") && block.name) names.push(String(block.name));
  }
  return names;
}

// ─── Daily-log helpers ───────────────────────────────────────────────────────
function pad(n: number): string {
  return String(n).padStart(2, "0");
}
function today(): string {
  const d = new Date();
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
}
function nowHM(): string {
  const d = new Date();
  return `${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

function listDailyLogs(memoryDir: string): string[] {
  try {
    return fs
      .readdirSync(memoryDir)
      .filter((f) => /^\d{4}-\d{2}-\d{2}\.md$/.test(f))
      .sort();
  } catch {
    return [];
  }
}

// ─── Extension entry ─────────────────────────────────────────────────────────
export default function (pi: ExtensionAPI) {
  // RECALL: model-callable search ----------------------------------------------
  pi.registerTool({
    name: "memory_search",
    label: "Memory Search",
    description:
      "Search this project's past session memory (decisions, debugging notes, prior work) and return ranked hits. " +
      "Each hit has a chunk_hash you can pass to memory_expand for the full section.",
    promptSnippet: "Search past project memory for prior decisions, notes, and context",
    promptGuidelines: [
      "Use memory_search when the user's question could benefit from past decisions or prior work " +
        "('what did we decide about X', 'why did we do Y', 'have we seen this before'), or when you see a " +
        "'[memsearch] Memory available' hint.",
    ],
    parameters: Type.Object({
      query: Type.String({ description: "What to search past memory for" }),
      top_k: Type.Optional(Type.Number({ description: `Max hits (default ${TOP_K_DEFAULT})` })),
    }),
    async execute(_id, params, signal) {
      const proj = projectFor(process.cwd());
      const k = params.top_k && params.top_k > 0 ? Math.floor(params.top_k) : TOP_K_DEFAULT;
      const r = await memsearch(
        ["search", params.query, "--top-k", String(k), "--json-output", "--collection", proj.collection],
        signal,
      );
      if (r.code !== 0) {
        throw new Error(`memsearch search failed (exit ${r.code}): ${(r.stderr || r.stdout).trim().slice(0, 500)}`);
      }
      let hits: any[];
      try {
        hits = JSON.parse(r.stdout || "[]");
      } catch {
        hits = [];
      }
      const summary =
        hits.length === 0
          ? `No memory hits for "${params.query}" in collection ${proj.collection}.`
          : hits
              .map((h, i) => {
                const head = h.heading ? ` (${h.heading})` : "";
                const src = h.source ? ` — ${h.source}` : "";
                const body = String(h.content ?? "").trim().slice(0, 400);
                return `${i + 1}. [${h.chunk_hash}]${head}${src}\n${body}`;
              })
              .join("\n\n");
      return {
        content: [{ type: "text", text: summary }],
        details: { collection: proj.collection, count: hits.length, hits },
      };
    },
  });

  // RECALL: model-callable expand ----------------------------------------------
  pi.registerTool({
    name: "memory_expand",
    label: "Memory Expand",
    description: "Expand a memory_search hit (by chunk_hash) to its full markdown section.",
    promptSnippet: "Expand a memory_search hit to its full section",
    promptGuidelines: ["Use memory_expand to read the full section behind a memory_search chunk_hash."],
    parameters: Type.Object({
      chunk_hash: Type.String({ description: "chunk_hash from a memory_search hit" }),
    }),
    async execute(_id, params, signal) {
      const proj = projectFor(process.cwd());
      const r = await memsearch(["expand", params.chunk_hash, "--collection", proj.collection], signal);
      if (r.code !== 0) {
        throw new Error(`memsearch expand failed (exit ${r.code}): ${(r.stderr || r.stdout).trim().slice(0, 500)}`);
      }
      return { content: [{ type: "text", text: r.stdout.trim() || "(empty)" }], details: {} };
    },
  });

  // RECALL: /recall <query> and /recall-expand <hash> --------------------------
  pi.registerCommand("recall", {
    description: "Search past project memory: /recall <query>",
    handler: async (args, ctx) => {
      const query = (args ?? "").trim();
      if (!query) {
        ctx.ui.notify("Usage: /recall <query>", "warning");
        return;
      }
      const proj = projectFor(ctx.cwd);
      ctx.ui.setStatus("memsearch", `recall: ${query}`);
      const r = await memsearch([
        "search",
        query,
        "--top-k",
        String(TOP_K_DEFAULT),
        "--json-output",
        "--collection",
        proj.collection,
      ]);
      if (r.code !== 0) {
        ctx.ui.notify(`recall failed: ${(r.stderr || r.stdout).trim().slice(0, 300)}`, "error");
        return;
      }
      let hits: any[] = [];
      try {
        hits = JSON.parse(r.stdout || "[]");
      } catch {
        /* leave empty */
      }
      ctx.ui.setStatus("memsearch", "");
      if (hits.length === 0) {
        ctx.ui.notify(`No memory hits for "${query}".`, "info");
        return;
      }
      // Hand the hits to the model so it can weave them into the conversation.
      const block = hits
        .map((h, i) => `${i + 1}. [${h.chunk_hash}] ${h.heading ?? ""} ${h.source ?? ""}\n${String(h.content ?? "").trim().slice(0, 500)}`)
        .join("\n\n");
      pi.sendMessage(
        {
          customType: "memsearch-recall",
          content: `Recall results for "${query}" (collection ${proj.collection}):\n\n${block}\n\nUse memory_expand <chunk_hash> for full sections.`,
          display: true,
        },
        { deliverAs: "followUp", triggerTurn: true },
      );
    },
  });

  pi.registerCommand("recall-expand", {
    description: "Expand a recall hit: /recall-expand <chunk_hash>",
    handler: async (args, ctx) => {
      const hash = (args ?? "").trim();
      if (!hash) {
        ctx.ui.notify("Usage: /recall-expand <chunk_hash>", "warning");
        return;
      }
      const proj = projectFor(ctx.cwd);
      const r = await memsearch(["expand", hash, "--collection", proj.collection]);
      if (r.code !== 0) {
        ctx.ui.notify(`expand failed: ${(r.stderr || r.stdout).trim().slice(0, 300)}`, "error");
        return;
      }
      pi.sendMessage(
        { customType: "memsearch-recall", content: r.stdout.trim() || "(empty)", display: true },
        { deliverAs: "followUp", triggerTurn: true },
      );
    },
  });

  // RECALL: inject a "memory available" hint on session start; also re-index so any
  // index deferred by a lock on a previous turn actually catches up here (mirrors
  // Claude's SessionStart re-index). Both are non-blocking.
  pi.on("session_start", async (_event, ctx) => {
    if (!resolveCli()) return; // no memsearch → silently no-op
    const proj = projectFor(ctx.cwd);
    const logs = listDailyLogs(proj.memoryDir);
    if (logs.length === 0) return;

    // Detached re-index: this is the documented "catch-up" path. If a capture's index
    // was deferred under lock contention, the markdown is on disk and gets indexed now.
    spawnDetachedIndex(proj, path.join(proj.memoryDir, `${today()}.md`));

    const oldest = logs[0].replace(/\.md$/, "");
    const newest = logs[logs.length - 1].replace(/\.md$/, "");
    const range = oldest === newest ? oldest : `${oldest} to ${newest}`;
    pi.sendMessage(
      {
        customType: "memsearch-hint",
        content:
          `[memsearch] Memory available — ${logs.length} past daily log(s) (${range}) in collection ${proj.collection}. ` +
          `Call memory_search (or /recall) when the user's question could benefit from past decisions or prior work.`,
        display: false,
      },
      { deliverAs: "nextTurn" },
    );
    ctx.ui.setStatus("memsearch", `memory: ${logs.length} logs`);
  });

  // CAPTURE: note the turn, append to the daily md (sync), index (detached) ----
  // Must be fast: in `-p` mode Pi waits for agent_end before exiting, so this path
  // does NO blocking network/LLM work. It builds deterministic notes, appends the
  // markdown synchronously (the source of truth), and fires the index in a detached
  // child that outlives the process.
  pi.on("agent_end", async (event, ctx) => {
    if (!resolveCli()) return; // no memsearch → nothing to capture into
    const proj = projectFor(ctx.cwd);

    // Prefer the just-completed prompt's messages; fall back to the session branch.
    const evMsgs = (event as any)?.messages;
    let entries: any[];
    if (Array.isArray(evMsgs) && evMsgs.length) {
      entries = evMsgs.map((m: any) => ({ type: "message", message: m }));
    } else {
      try {
        entries = ctx.sessionManager.getBranch(); // only this can raise (odd/ephemeral session)
      } catch {
        return; // no branch to read — nothing to capture this turn
      }
    }
    const turn = lastTurn(entries);
    if (!turn.userQuestion && !turn.agentText && turn.toolCalls.length === 0) return; // nothing to record
    const summary = buildSummary(turn);

    // Anchor: Pi session file path + a per-turn id (mirrors Claude's session/turn/transcript).
    let sessionFile = "";
    try {
      sessionFile = ctx.sessionManager.getSessionFile() ?? "";
    } catch {
      /* ephemeral session */
    }
    const sessionId = sessionFile ? path.basename(sessionFile).replace(/\.jsonl$/, "") : "ephemeral";
    const turnId = crypto.randomBytes(6).toString("hex");

    const entryText =
      `\n### ${nowHM()}\n` +
      `<!-- session:${sessionId} turn:${turnId} transcript:${sessionFile} -->\n` +
      `${summary.trim()}\n`;

    const memFile = path.join(proj.memoryDir, `${today()}.md`);
    try {
      fs.mkdirSync(proj.memoryDir, { recursive: true });
      fs.appendFileSync(memFile, entryText); // markdown = source of truth, written FIRST
    } catch (e) {
      // A filesystem write failure is real — surface it, do not swallow.
      ctx.ui.notify(`[memsearch] failed to write daily log: ${(e as Error).message}`, "error");
      return;
    }

    // Index is derived and runs detached — never blocks the turn or shutdown.
    spawnDetachedIndex(proj, memFile);
  });
}
