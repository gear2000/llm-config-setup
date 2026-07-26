/**
 * tf-implement — Terraform implementation loop for Pi
 *
 * Implements Terraform code in two modes:
 *
 * 1. Standalone  /tf:implement <plan-path>
 *    Load a plan from a file, inject it into Pi's system prompt, guide Pi through
 *    writing .tf files. When Pi signals ready (echo TF_REVIEW_READY), bundle the
 *    files and route them to an external reviewer via FIFOs.
 *
 * 2. Auto       /tf:auto [--planish] [description]
 *    Always plans first, then implements. Planning medium is the user's choice:
 *      • planish (--planish forces it, else prompted) — Pi grills the user via
 *        planish_grill (annotatable browser page; feedback comes back as a
 *        pasted ## Feedback block), writes plan.html, serves it for review via
 *        planish_submit_plan (approval = pasted ## FINALIZED / explicit OK),
 *        then implements the .tf files. The approved plan.html is read from
 *        cwd and passed to the reviewer as context.
 *      • direct — Pi works out the plan with the user in the terminal, then
 *        implements.
 *    Both paths end in the same reviewer loop.
 *
 * Reviewer protocol (shared by both modes):
 *   Extension → tf-review-request.fifo  — JSON: { type: "code_review", files: Record<filename, content>, plan: string }
 *   Reviewer  → tf-review-response.fifo — JSON: { status: "approved" }
 *                                        |        { status: "issues", escalate: boolean, items: string[] }
 *
 * FIFOs:
 *   ~/.pi/tf-review-request.fifo   — extension writes, reviewer reads
 *   ~/.pi/tf-review-response.fifo  — reviewer writes, extension reads
 */

import type { ExtensionAPI } from "@mariozechner/pi-coding-agent";
import * as fs from "node:fs";
import * as path from "node:path";
import * as os from "node:os";
import { fileURLToPath } from "node:url";
import { spawnSync } from "node:child_process";

// ─── Config ───────────────────────────────────────────────────────────────────

const PI_DIR = path.join(os.homedir(), ".pi");
const REQUEST_FIFO = path.join(PI_DIR, "tf-review-request.fifo");
const RESPONSE_FIFO = path.join(PI_DIR, "tf-review-response.fifo");
const CONFIG_NAME = ".shared-llm.yaml";
const LEGACY_CONFIG_NAME = ".planish.yaml";
const DEFAULT_TEMPLATE = "/var/tmp/work-log/{date}/{slug}";
const CLAIM_ATTEMPTS = 64;
const PYTHON_BIN = process.env.PYTHON_BIN || "python3";
const RESOLVER_REL = "public/llm/common/common/planish_resolve.py";

// ─── FIFO helpers ─────────────────────────────────────────────────────────────

const REVIEWER_TIMEOUT_MS = 30_000;

function withTimeout<T>(promise: Promise<T>, ms: number): Promise<T> {
  return Promise.race([
    promise,
    new Promise<never>((_, reject) =>
      setTimeout(() => reject(new Error(`reviewer timed out after ${ms}ms — is tf-reviewer running?`)), ms)
    ),
  ]);
}

function ensureFifo(fifoPath: string): void {
  if (!fs.existsSync(fifoPath)) {
    const result = spawnSync("mkfifo", [fifoPath]);
    if (result.status !== 0) {
      const stderr = (result.stderr as Buffer | null)?.toString() ?? "";
      throw new Error(`mkfifo failed for ${fifoPath}: ${stderr}`);
    }
  }
}

function writeFifo(fifoPath: string, data: string): Promise<void> {
  return new Promise((resolve, reject) => {
    const stream = fs.createWriteStream(fifoPath);
    stream.once("error", reject);
    stream.write(data + "\n", (err) => {
      if (err) return reject(err);
      stream.end(() => resolve());
    });
  });
}

function readFifo(fifoPath: string): Promise<string> {
  return new Promise((resolve, reject) => {
    const chunks: Buffer[] = [];
    const stream = fs.createReadStream(fifoPath);
    stream.on("data", (chunk) =>
      chunks.push(Buffer.isBuffer(chunk) ? chunk : Buffer.from(chunk as string))
    );
    stream.on("end", () =>
      resolve(Buffer.concat(chunks).toString("utf-8").trim())
    );
    stream.on("error", reject);
  });
}

// ─── Response types ───────────────────────────────────────────────────────────

interface ReviewApproved {
  status: "approved";
}

interface ReviewIssues {
  status: "issues";
  escalate: boolean;
  items: string[];
}

type ReviewResponse = ReviewApproved | ReviewIssues;

// ─── Plan output directory ──────────────────────────────────────────────────────
//
// In planish mode the PLAN (plan.md + plan.html) is written into a RESOLVED
// directory, never the cwd (the .tf implementation files still go to cwd — only
// the plan moves). There is exactly ONE implementation of that resolution:
// planish_resolve.py, run here as a subprocess. This extension used to carry a
// hand-rolled YAML scanner beside it; two reviews running the same configs
// through both found it diverging from PyYAML in ways substring checks never
// see (an escaped quote before ` #`, a quoted comma inside a flow mapping), so
// the scanner is gone and no config is parsed here at all. python3 is already a
// kit prerequisite — `just init` checks for it.
//
// Precedence (--dir, $WORK_LOG_DIR, $PLANISH_DIR, work_log.dir, legacy
// .planish.yaml, then /var/tmp/work-log/{date}/{slug}), the {date}/{slug}/
// {type}/{n} tokens, the atomic {n} claim, and the fail-loud rules for a
// malformed config ALL live in that script. It creates and claims the directory
// it returns, so nothing here claims it a second time.
//
// The only resolution left below is the fallback for a machine carrying neither
// the kit nor any config file: with nothing configured there is nothing to
// diverge from. A config file present WITHOUT the canonical script is a loud
// failure — re-implementing the parse is exactly what this change removed.

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

// The canonical resolver, looked for by walking UP from cwd — a repo that has
// adopted the kit carries it under .shared-llm/ — and then beside this
// extension. The second location is how a DEPLOYED copy finds it: pi loads
// extensions at their ~/.pi/agent/extensions/ symlink, where import.meta.url
// does not follow the link, so the path is realpath-resolved back into the
// generated kit tree first (the same trick do-planish uses for its toolkit).
function canonicalResolver(startDir: string): string | null {
  const fromCwd = findConfigUp(startDir, path.join(".shared-llm", RESOLVER_REL));
  if (fromCwd) return fromCwd;
  const beside = path.resolve(
    path.dirname(fs.realpathSync(fileURLToPath(import.meta.url))),
    "../../../common/common/planish_resolve.py",
  );
  return fs.existsSync(beside) ? beside : null;
}

interface ResolverResult {
  host: string | null;
  plan_dir: string | null;
  review_url: string | null;
}

// Run the canonical resolver. Its stderr — deprecation notices, the reason a
// config was rejected — is passed through verbatim, and a non-zero exit is a
// fault here too: never a quiet fall back to a locally invented directory.
function runResolver(script: string, cwd: string, args: string[]): ResolverResult {
  const result = spawnSync(PYTHON_BIN, [script, "--cwd", cwd, ...args], {
    encoding: "utf-8",
  });
  if (result.error) throw result.error;
  const stderr = (result.stderr ?? "").trim();
  if (stderr) process.stderr.write(`${stderr}\n`);
  if (result.status !== 0) {
    throw new Error(`${script} exited ${result.status}: ${stderr}`);
  }
  return JSON.parse(result.stdout) as ResolverResult;
}

// The config file whose rules only the canonical script knows how to apply.
function configPresent(cwd: string): string | null {
  return findConfigUp(cwd, CONFIG_NAME) ?? findConfigUp(cwd, LEGACY_CONFIG_NAME);
}

// Deprecation notices go to stderr — never into a plan file or a tool result.
function warnDeprecated(message: string): void {
  console.error(`[tf-implement] ${message}`);
}

// Create the resolved directory, claiming a {n} version exclusively. Scanning
// for the highest sibling and then creating with `recursive: true` hands the
// same directory to every caller that scans before any of them creates, so the
// version segment is claimed with a NON-recursive mkdir: whoever loses the race
// gets EEXIST, rescans, and takes the next integer. Mirrors planish_resolve.py.
function claimPlanDir(absPath: string): string {
  const parts = absPath.split(path.sep);
  const idx = parts.findIndex((seg) => seg.includes("{n}"));
  if (idx === -1) {
    // No version token — concurrent callers legitimately share the path.
    fs.mkdirSync(absPath, { recursive: true });
    return absPath;
  }
  for (let attempt = 0; attempt < CLAIM_ATTEMPTS; attempt++) {
    const versioned = expandVersionToken(absPath);
    const claim = versioned.split(path.sep).slice(0, idx + 1).join(path.sep) || path.sep;
    fs.mkdirSync(path.dirname(claim), { recursive: true });
    try {
      fs.mkdirSync(claim);
    } catch (err: any) {
      if (err?.code === "EEXIST") continue;
      throw err;
    }
    fs.mkdirSync(versioned, { recursive: true });
    return versioned;
  }
  throw new Error(
    `could not claim a version directory under ${parts.slice(0, idx).join(path.sep)} ` +
      `after ${CLAIM_ATTEMPTS} attempts`,
  );
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

// The template a --dir flag or an env var names, or null when neither is set
// and the directory therefore has to come from a config file. These beat the
// config outright in the canonical precedence, so reading one costs no parsing.
function explicitTemplate(dirFlag?: string): string | null {
  if (dirFlag && dirFlag.trim()) return dirFlag.trim();
  if (process.env.WORK_LOG_DIR && process.env.WORK_LOG_DIR.trim()) {
    return process.env.WORK_LOG_DIR.trim();
  }
  if (process.env.PLANISH_DIR && process.env.PLANISH_DIR.trim()) {
    warnDeprecated("$PLANISH_DIR is deprecated — use $WORK_LOG_DIR");
    return process.env.PLANISH_DIR.trim();
  }
  return null;
}

// Python's Path.expanduser(), which the canonical script applies to the expanded
// template: a "~" that is the whole first segment is the home directory, and a
// "~" anywhere else is an ordinary path character. Without this, a
// $WORK_LOG_DIR of "~/plans/{slug}" lands in <cwd>/~/plans/<slug> here while
// the script puts it in $HOME/plans/<slug> — one template, two directories,
// decided by which resolver happened to run.
//
// "~otheruser/…" is the one case this cannot mirror: resolving it needs a
// passwd lookup Node has no equivalent for (Python either expands it or raises
// RuntimeError). It is a loud stop rather than a path invented from os.homedir.
function expandUser(template: string): string {
  if (template === "~" || template.startsWith("~/")) {
    // posixpath.expanduser verbatim: strip trailing slashes off the home, glue
    // the rest of the template on, and call an empty result the root. Not
    // path.join — path.join("", "") is ".", which path.resolve then turns into
    // the cwd, so an empty $HOME would put the plan log wherever the caller
    // happened to be standing while the script puts it in "/".
    return (tildeHome().replace(/\/+$/, "") + template.slice(1)) || "/";
  }
  if (template.startsWith("~")) {
    throw new Error(
      `cannot expand "${template}": this fallback expands "~" and "~/…" only, ` +
        `never another user's home — use an absolute path in $WORK_LOG_DIR ` +
        `(or --dir), or make the canonical resolver reachable`,
    );
  }
  return template;
}

// The home directory posixpath.expanduser would use. It reads $HOME whenever the
// variable is SET — an empty $HOME expands "~" to "/" there — and consults the
// passwd entry only when the variable is absent. os.homedir() cannot express
// that difference: it treats an empty $HOME as unset and falls back to passwd,
// so $HOME is read directly and os.homedir() is used only for the passwd case
// it does match. With neither, the answer is not knowable and this stops.
function tildeHome(): string {
  const home = process.env.HOME;
  if (home !== undefined) return home;
  const fromPasswd = os.homedir();
  if (fromPasswd) return fromPasswd;
  throw new Error(
    `cannot expand "~": $HOME is unset and this machine has no home directory ` +
      `to fall back to — set $WORK_LOG_DIR (or --dir) to an absolute path`,
  );
}

// Used only when the canonical script is unreachable AND no config file decides
// the directory. Nothing here reads YAML, so nothing here can drift from PyYAML.
function fallbackPlanPath(cwd: string, topic: string, dirFlag?: string): string {
  const expanded = expandUser(
    (explicitTemplate(dirFlag) ?? DEFAULT_TEMPLATE)
      .replace(/\{date\}/g, todayYmd())
      .replace(/\{slug\}/g, slugifyTopic(topic))
      .replace(/\{type\}/g, "plan"),
  );
  return path.isAbsolute(expanded) ? expanded : path.resolve(cwd, expanded);
}

export function resolvePlanDir(cwd: string, topic: string, dirFlag?: string): string {
  const script = canonicalResolver(cwd);
  if (script) {
    // An empty description used to slugify to "plan"; the canonical script
    // rejects an empty topic, so name that slug explicitly to keep /tf:auto
    // with no description landing where it always did.
    const args = ["--topic", topic.trim() || "plan"];
    if (dirFlag && dirFlag.trim()) args.push("--dir", dirFlag.trim());
    const resolved = runResolver(script, cwd, args);
    // The script created and claimed it — claiming again would burn a version.
    if (!resolved.plan_dir) throw new Error(`${script} returned no plan_dir`);
    return resolved.plan_dir;
  }

  // No script. Guessing what a config file says is how the deleted scanner got
  // this wrong, so a config that would decide the directory is a loud stop —
  // unless a flag or env var already outranks it, in which case the config is
  // never consulted anyway and there is nothing to guess at.
  const config = explicitTemplate(dirFlag) === null ? configPresent(cwd) : null;
  if (config) {
    throw new Error(
      `${config} may configure the work log, but the canonical resolver ` +
        `(.shared-llm/${RESOLVER_REL}) is not reachable from ${cwd} — run ` +
        `\`just update\` for this repo, or set $WORK_LOG_DIR for this session. ` +
        `This extension never parses the config itself.`,
    );
  }
  return claimPlanDir(fallbackPlanPath(cwd, topic, dirFlag));
}

// ─── Extension entry ──────────────────────────────────────────────────────────

export default function (pi: ExtensionAPI) {
  // State persists across turns (closure-level, not event-level).
  let planContent = "";           // set in standalone mode
  let pendingIssues: string[] = [];
  let planishMode = false;         // /tf:auto with planish planning; clears on completion
  let directMode = false;          // /tf:auto planning directly in the terminal; clears on completion
  let planDir = "";                // resolved at /tf:auto time (planish); where plan.md + plan.html land

  function setupFifos(ctx: any): boolean {
    fs.mkdirSync(PI_DIR, { recursive: true });
    try {
      ensureFifo(REQUEST_FIFO);
      ensureFifo(RESPONSE_FIFO);
      return true;
    } catch (err) {
      ctx.ui.notify(
        `tf: failed to create FIFOs — ${err instanceof Error ? err.message : String(err)}`,
        "error"
      );
      return false;
    }
  }

  // ── /tf:implement <plan-path> — standalone mode ───────────────────────────
  (pi as any).registerCommand("tf:implement", {
    description: "Load a Terraform plan and start the implement loop: /tf:implement <plan-path>",
    handler: async (args: string, ctx: any) => {
      const planPath = args.trim();
      if (!planPath) {
        ctx.ui.notify("Usage: /tf:implement <path-to-plan-file>", "error");
        return;
      }
      if (!setupFifos(ctx)) return;
      try {
        planContent = fs.readFileSync(planPath, "utf-8");
        pendingIssues = [];
        planishMode = false;
        directMode = false;
        ctx.ui.notify(`tf:implement: plan loaded from ${planPath} — ask Pi to write .tf files`, "info");
      } catch (err) {
        ctx.ui.notify(
          `tf:implement: failed to read plan at ${planPath} — ${err instanceof Error ? err.message : String(err)}`,
          "error"
        );
      }
    },
  });

  // ── /tf:auto [--planish] [description] — plan then implement ──────────────
  //
  // tf:auto always PLANS first, then implements. How it plans is your choice:
  //   • planish — visual HTML plan in the browser, with a grill step (--planish
  //     forces this and skips the prompt)
  //   • direct  — plan in the terminal conversation
  // Without --planish, it asks once which you want.
  (pi as any).registerCommand("tf:auto", {
    description: "Plan (planish or direct) then implement: /tf:auto [--planish] [optional description]",
    handler: async (args: string, ctx: any) => {
      if (!setupFifos(ctx)) return;
      planContent = "";
      pendingIssues = [];

      const forcePlanish = /(^|\s)--planish(\s|$)/.test(args);
      let rest = args.replace(/(^|\s)--planish(\s|$)/, " ");
      // Optional --dir <path> overrides where the plan (plan.md + plan.html) lands.
      let dirFlag: string | undefined;
      const dirMatch = rest.match(/(^|\s)--dir\s+(\S+)/);
      if (dirMatch) {
        dirFlag = dirMatch[2];
        rest = rest.replace(/(^|\s)--dir\s+\S+/, " ");
      }
      const hint = rest.trim();

      let usePlanish: boolean;
      if (forcePlanish) {
        usePlanish = true;
      } else {
        const confirm = ctx?.ui?.confirm;
        if (typeof confirm === "function") {
          try {
            usePlanish = await ctx.ui.confirm(
              "Plan with planish?",
              "Yes → planish: a visual HTML plan in your browser, with a grill step first.\n" +
                "No → plan directly here in the terminal.\n\n" +
                "Either way, Pi implements the .tf files after the plan is settled and routes them to the reviewer.",
              { timeout: 120_000 }
            );
          } catch {
            usePlanish = false;
          }
        } else {
          usePlanish = false; // no UI available — plan directly
        }
      }

      planishMode = usePlanish;
      directMode = !usePlanish;

      if (usePlanish) {
        try {
          planDir = resolvePlanDir(ctx?.cwd ?? process.cwd(), hint, dirFlag);
        } catch (err) {
          ctx.ui.notify(`tf:auto: ${err instanceof Error ? err.message : String(err)}`, "error");
          planishMode = false;
          return;
        }
      }

      const subject = hint ? `"${hint}"` : "the Terraform infrastructure you describe";
      const msg = usePlanish
        ? `tf:auto: planish planning active — Pi will grill you in the browser (annotate → Copy Feedback → paste back), write the plan (plan.md + plan.html) to ${planDir} for ${subject}, and implement the .tf files in the working directory after you paste your approval.`
        : `tf:auto: direct planning active — Pi will work out the plan for ${subject} with you here, then implement the .tf files and route them to the reviewer.`;
      ctx.ui.notify(msg, "info");
    },
  });

  // ── session_start: load plan from TF_PLAN_PATH env var (CLI / just tf-implement) ─

  pi.on("session_start", async (_event, ctx) => {
    fs.mkdirSync(PI_DIR, { recursive: true });
    try {
      ensureFifo(REQUEST_FIFO);
      ensureFifo(RESPONSE_FIFO);
    } catch (err) {
      ctx.ui.notify(
        `tf-implement: failed to create FIFOs — ${err instanceof Error ? err.message : String(err)}`,
        "error"
      );
      return;
    }
    const planPath = process.env.TF_PLAN_PATH;
    if (!planPath) return;
    try {
      planContent = fs.readFileSync(planPath, "utf-8");
      planishMode = false;
      directMode = false;
      ctx.ui.notify(`tf-implement: plan loaded from ${planPath}`, "info");
    } catch (err) {
      ctx.ui.notify(
        `tf-implement: failed to read plan at ${planPath} — ${err instanceof Error ? err.message : String(err)}`,
        "error"
      );
    }
  });

  // ── before_agent_start: inject instructions for the active mode ───────────

  pi.on("before_agent_start", async (event) => {
    const issuesBlock =
      pendingIssues.length > 0
        ? `The reviewer found these issues with your previous iteration. Fix them before signalling ready:\n\n${pendingIssues.join("\n")}\n\n`
        : "";

    // NOTE: planish grill->build->review prompt is intentionally DUPLICATED (not shared) in do-planish.ts. Keep in sync with the Planish HTML Grill Contract.
    if (planishMode) {
      const planHtml = path.join(planDir, "plan.html");
      const planMd = path.join(planDir, "plan.md");
      return {
        systemPrompt:
          event.systemPrompt +
          `\n\n${issuesBlock}You are a Terraform infrastructure engineer.\n\n` +
          (pendingIssues.length > 0
            ? "Fix the issues above in your .tf files. When all issues are resolved, run:\n  echo TF_REVIEW_READY\n\nDo NOT reopen the planning phase."
            : "STEP 1 — GRILL: Before writing anything, call the planish_grill tool with title, contextHtml, and a batch of clarifying infrastructure questions (regions, sizing, naming, dependencies, what already exists, ordering constraints). Give each question a concrete recommended answer — the page is annotation-only, and a question with no note means the user accepted your recommendation. The tool serves the page and returns immediately: give the user the URL, END YOUR TURN, and wait for their pasted ## Feedback block. Do NOT make a plain Q&A-only grill. Visuals (two modes only — NEVER Mermaid): default → ascii tree/shape, complex → visualHtml with .grill-fig/.flow/.flow-box drawn row by row. A diagram only when it genuinely helps — never for its own sake. Use the feedback to inform the plan. Ask follow-ups by calling planish_grill again if needed.\n\n" +
              `STEP 2 — PLAN: Write a Terraform implementation plan to TWO files (the directory already exists):\n` +
              // # dup 1 (plan-html-style) — canonical in do-planish.ts STEP 2 BUILD
              `  • ${planHtml} — the visual plan: a title, a summary table of resources to create (columns: resource type, name, action, key parameters), the file/module structure, and key variables/outputs.\n` +
              `    Use the v3 dark style (NO Tailwind CDN). Include in <head>:\n` +
              `    <link href="https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@300;400;500;600;700&family=IBM+Plex+Sans:wght@300;400;500;600&display=swap" rel="stylesheet">\n` +
              `    <style>*{box-sizing:border-box;margin:0;padding:0;}body{font-family:'JetBrains Mono',monospace;background:#0d1017;color:#c8ccd4;padding:40px;max-width:1040px;line-height:1.5;}h1{font-family:'IBM Plex Sans',sans-serif;font-size:22px;font-weight:600;color:#e6e9ef;letter-spacing:-0.3px;margin-bottom:6px;}.subtitle{font-size:11px;color:#545862;letter-spacing:1.5px;text-transform:uppercase;margin-bottom:28px;padding-bottom:22px;border-bottom:1px solid #1e222a;}h2{font-family:'IBM Plex Sans',sans-serif;font-size:14px;font-weight:500;color:#e6e9ef;margin:32px 0 14px;padding-bottom:8px;border-bottom:1px solid #1e222a;}table{width:100%;border-collapse:collapse;margin-bottom:16px;}th{font-size:10px;letter-spacing:1px;text-transform:uppercase;color:#545862;padding:8px 12px;border-bottom:1px solid #1e222a;text-align:left;}td{font-size:11px;color:#a0a4ac;padding:7px 12px;border-bottom:1px solid #1e222a;}tr:last-child td{border-bottom:none;}.card{border:1px solid #1e222a;border-radius:10px;padding:18px 22px;background:#0f1219;margin-bottom:16px;}.card.amber{border-left:3px solid #d19a66;background:#15120d;}code{background:#1a1f29;color:#7ab4db;border-radius:3px;padding:1px 5px;font-size:11px;}</style>\n` +
              `  • ${planMd} — the same plan as token-lean Markdown (the .md is the lean agent record, the .html is the visual/annotatable copy).\n` +
              `Both files hold the same plan content; give the .html annotation controls before </body> and a unique <meta name="desdoc-key"> — no answer boxes, no submit buttons. plan.md/plan.html always hold the latest; you do NOT freeze versions by hand. Then serve it for user review by calling the planish_submit_plan tool with the path ${planHtml}; that tool auto-freezes the next plan-v<k>.md + plan-v<k>.html pair for you (whenever the plan changed) before serving, and returns immediately — tell the user, END YOUR TURN, and wait for their pasted feedback (## FINALIZED or explicit approval = approved; notes = revise both files and resubmit; never edit a frozen plan-v* file).\n\n` +
              "STEP 3 — IMPLEMENT: Once the user pastes their approval, write all .tf files in the current working directory to implement it exactly. Follow the approved plan.\n\n" +
              "STEP 4 — SIGNAL: When all .tf files are written and ready for review, run:\n  echo TF_REVIEW_READY\n\nDo NOT run terraform init, plan, apply, or destroy — only write .tf files."),
      };
    }

    if (directMode) {
      return {
        systemPrompt:
          event.systemPrompt +
          `\n\n${issuesBlock}You are a Terraform infrastructure engineer.\n\n` +
          (pendingIssues.length > 0
            ? "Fix the issues above in your .tf files. When all issues are resolved, run:\n  echo TF_REVIEW_READY\n\nDo NOT reopen the planning phase."
            : "STEP 1 — PLAN: Work out the plan with the user directly in this conversation. Ask your clarifying questions, state the resources you intend to create (type, name, key parameters), the file/module structure, and ordering. Get explicit agreement before writing any code.\n\n" +
              "STEP 2 — IMPLEMENT: Once the user agrees, write all .tf files to implement the plan exactly.\n\n" +
              "STEP 3 — SIGNAL: When all .tf files are written and ready for review, run:\n  echo TF_REVIEW_READY\n\nDo NOT run terraform init, plan, apply, or destroy — only write .tf files."),
      };
    }

    if (!planContent) return;

    // Standalone mode: inject plan content and optional issues.
    return {
      systemPrompt:
        event.systemPrompt +
        `\n\n${issuesBlock}You are a Terraform code writer. Your task is described in this plan:\n\n<plan>\n${planContent}\n</plan>\n\nWrite or update .tf files to implement this plan exactly.\nWhen you have written all files and are ready for review, run:\n  echo TF_REVIEW_READY\nDo NOT run terraform init, plan, apply, or destroy — only write .tf files.`,
    };
  });

  // ── tool_call: intercept "echo TF_REVIEW_READY" ───────────────────────────

  pi.on("tool_call" as any, async (event: any, ctx: any) => {
    if (event?.toolName !== "bash") return;
    const command: string = event?.input?.command ?? "";
    if (!command.trim()) return;

    const trimmed = command.trim();
    if (
      trimmed !== "echo TF_REVIEW_READY" &&
      !trimmed.startsWith("echo TF_REVIEW_READY")
    ) {
      return;
    }

    if (!planContent && !planishMode && !directMode) return; // no active session

    // Collect all .tf files in cwd.
    const cwd: string = ctx?.cwd ?? process.cwd();
    const tfFiles: Record<string, string> = {};
    try {
      for (const f of fs.readdirSync(cwd)) {
        if (f.endsWith(".tf")) {
          tfFiles[f] = fs.readFileSync(path.join(cwd, f), "utf-8");
        }
      }
    } catch (err) {
      return {
        block: true,
        reason: `tf-implement: failed to read .tf files from ${cwd} — ${err instanceof Error ? err.message : String(err)}`,
      };
    }

    // In planish mode, read the approved plan from plan.html in the resolved planDir.
    let planForReview = planContent;
    if (planishMode && !planForReview) {
      try {
        const planFilePath = path.join(planDir, "plan.html");
        if (fs.existsSync(planFilePath)) {
          planForReview = fs.readFileSync(planFilePath, "utf-8");
        }
      } catch { /* ignore — reviewer gets empty plan context */ }
    }

    // Send files to reviewer and wait for response.
    let response: ReviewResponse;
    try {
      const payload = JSON.stringify({ type: "code_review", files: tfFiles, plan: planForReview });
      await withTimeout(writeFifo(REQUEST_FIFO, payload), REVIEWER_TIMEOUT_MS);
      const raw = await withTimeout(readFifo(RESPONSE_FIFO), REVIEWER_TIMEOUT_MS);
      if (!raw) throw new Error("reviewer returned empty response");
      response = JSON.parse(raw) as ReviewResponse;
    } catch (err) {
      return {
        block: true,
        reason: `tf-implement: FIFO error during review — ${err instanceof Error ? err.message : String(err)}`,
      };
    }

    // Branch on reviewer verdict.
    if (response.status === "approved") {
      pendingIssues = [];
      planishMode = false;
      directMode = false;
      return {
        block: true,
        reason: "Code approved by reviewer. tf:implement complete.",
      };
    }

    if (response.status === "issues") {
      if (!response.escalate) {
        pendingIssues = response.items;
        const issueList = response.items.map((i) => `  - ${i}`).join("\n");
        return {
          block: true,
          reason: `Reviewer found ${response.items.length} issue(s). Fix them and run echo TF_REVIEW_READY again:\n\n${issueList}`,
        };
      }

      const issueList = response.items.map((i) => `  - ${i}`).join("\n");
      const confirmUI = ctx?.ui?.confirm;
      if (typeof confirmUI !== "function") {
        pendingIssues = response.items;
        return {
          block: true,
          reason: `Reviewer escalated ${response.items.length} issue(s) for human review. No UI available — fix before proceeding:\n\n${issueList}`,
        };
      }

      let humanApproved: boolean;
      try {
        humanApproved = await ctx.ui.confirm(
          "Reviewer escalated issues — approve or send back for fixes?",
          `The reviewer found these issue(s):\n\n${issueList}\n\nApprove to accept as-is, or cancel to have Pi fix them.`,
          { timeout: 120_000 }
        );
      } catch {
        humanApproved = false;
      }

      if (humanApproved) {
        pendingIssues = [];
        planishMode = false;
        directMode = false;
        return { block: true, reason: "Approved by human override." };
      }

      pendingIssues = response.items;
      return {
        block: true,
        reason: `Human review rejected. Fix the following before signalling ready again:\n\n${issueList}`,
      };
    }

    return {
      block: true,
      reason: `tf-implement: unexpected reviewer response: ${JSON.stringify(response)}`,
    };
  });
}
