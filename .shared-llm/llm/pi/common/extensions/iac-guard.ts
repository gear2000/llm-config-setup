/**
 * iac-guard — destructive-IaC approval gate for Pi
 *
 * Intercepts the agent's bash commands BEFORE they run (the blocking `tool_call`
 * hook) and gates infrastructure tooling — terraform / tofu / aws / kubectl:
 *
 *   - read / pure-create operations (plan, describe, get, create …)
 *       → run silently (ALLOW)
 *   - destroy / update operations (destroy, delete, update, patch …)
 *       → ALWAYS require human approval (ASK, deterministic)
 *   - apply commands (terraform apply, kubectl apply)
 *       → run the dry-run first (terraform plan / kubectl diff),
 *         build a table of adds / changes / destroys:
 *           • adds only   → ALLOW automatically
 *           • any changes or destroys → show table + require human approval
 *   - unclassified gray-zone → an LLM verifier decides ALLOW vs ASK
 *
 * Fail-closed: any parse ambiguity, missing UI, dry-run failure, or unavailable/
 * timed-out verifier falls back to human approval.
 *
 * Auto-loaded (agent-scoped: ~/.pi/agent/extensions/). No `pi -e` needed.
 *
 * The POLICY TABLES below are the artifact to tune for your environment.
 */

import type { ExtensionAPI } from "@mariozechner/pi-coding-agent";
import { spawnSync } from "node:child_process";
import * as net from "node:net";
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import * as crypto from "node:crypto";

// ─── Config ──────────────────────────────────────────────────────────────────
const SOCKET_PATH = path.join(os.homedir(), ".pi", "codex-reviewer.sock");
const VERIFIER_AGENT = "iac-verifier";
const GRAY_TIMEOUT_MS = 25_000;
const CONFIRM_TIMEOUT_MS = 120_000;
const DRY_RUN_TIMEOUT_MS = 300_000; // 5 min for terraform plan
const POLL_INTERVAL_MS = 400;
const DISPATCH_CONNECT_MS = 3_000;

// "plan-check" = run the tool's dry-run first, then decide based on the output.
// Ranked between gray and ask: if a compound command also has an ASK segment,
// ASK wins (we go straight to human without the dry-run).
type Tier = "allow" | "ask" | "gray" | "plan-check";
interface Decision {
  tier: Tier;
  reason: string;
}
const RANK: Record<Tier, number> = { allow: 0, gray: 1, "plan-check": 1.5, ask: 2 };

// ─── In-scope tools ──────────────────────────────────────────────────────────
const TERRAFORM = new Set(["terraform", "tofu", "opentofu"]);
const SHELLS = new Set(["bash", "sh", "zsh", "dash", "ash"]);
const ENV_ASSIGN = /^[A-Za-z_][A-Za-z0-9_]*=/;
const WRAPPERS = new Set([
  "sudo", "env", "nohup", "time", "command", "exec", "builtin",
  "stdbuf", "nice", "ionice", "setsid", "doas", "xargs",
]);

// ─── POLICY: terraform / tofu ────────────────────────────────────────────────
const TF_ALLOW = new Set([
  "plan", "validate", "fmt", "show", "output", "providers", "version",
  "graph", "init", "get", "console", "login", "logout", "test", "metadata",
]);
const TF_ASK = new Set(["destroy", "taint", "untaint", "import", "force-unlock"]);

// ─── POLICY: aws (matched on the operation verb) ─────────────────────────────
const AWS_ALLOW_PREFIX = /^(describe|list|get|search|lookup|scan|head|wait|test|validate|estimate|preview|generate|simulate|create)-/;
const AWS_ASK_PREFIX = /^(delete|terminate|deregister|remove|purge|destroy|revoke|update|modify|replace|put|set)-/;
const AWS_DOWNTIME_PREFIX = /^(stop|reboot)-/;
// gray: remaining state-change verbs that don't clearly add, update, or delete
const AWS_GRAY_PREFIX = /^(attach|associate|disassociate|detach|enable|disable|register|tag|untag|start|restore|import|copy|apply|run|send|publish|reset|rotate|cancel)-/;
const AWS_DESTRUCTIVE_WORD = /(delete|terminate|destroy|remove|purge|deregister)/;

// ─── POLICY: kubectl ─────────────────────────────────────────────────────────
const KC_ALLOW = new Set([
  // read-only
  "get", "describe", "logs", "top", "explain", "api-resources",
  "api-versions", "version", "cluster-info", "diff", "wait", "events", "whoami",
  // pure adds (create a new resource, no in-place mutation of existing ones)
  "create", "run", "expose",
  // metadata-only (non-destructive labels/annotations; adding metadata is low-risk)
  "label", "annotate",
  // un-restricts capacity (makes nodes available, not destructive)
  "uncordon",
]);
const KC_ASK = new Set([
  // removes resources
  "delete", "drain", "evict",
  // in-place mutations of existing resources
  "patch", "edit", "set",
  // scheduling policy changes
  "taint", "cordon",
]);
// gray: verbs that may add OR update depending on context (goes to LLM verifier)
const KC_GRAY = new Set([
  "scale", "replace", "autoscale",
]);

// ─── Token helpers ───────────────────────────────────────────────────────────
function basename(p: string): string {
  const i = p.lastIndexOf("/");
  return i >= 0 ? p.slice(i + 1) : p;
}
function hasFlag(args: string[], name: string): boolean {
  return args.some((a) => a === name || a.startsWith(name + "="));
}
function flagValue(args: string[], name: string): string | null {
  for (let i = 0; i < args.length; i++) {
    if (args[i] === name) return args[i + 1] ?? "";
    if (args[i].startsWith(name + "=")) return args[i].slice(name.length + 1);
  }
  return null;
}

/** Quote-aware tokenizer that also emits shell operators (&& || ; |) as tokens. */
function tokenize(s: string): string[] {
  const tokens: string[] = [];
  let cur = "";
  let quote: '"' | "'" | null = null;
  const flush = () => { if (cur) { tokens.push(cur); cur = ""; } };
  for (let i = 0; i < s.length; i++) {
    const c = s[i];
    if (quote) {
      if (c === quote) quote = null;
      else cur += c;
      continue;
    }
    if (c === '"' || c === "'") { quote = c; continue; }
    if (c === " " || c === "\t" || c === "\n" || c === "\r") { flush(); continue; }
    if (c === "&" && s[i + 1] === "&") { flush(); tokens.push("&&"); i++; continue; }
    if (c === "|" && s[i + 1] === "|") { flush(); tokens.push("||"); i++; continue; }
    if (c === ";") { flush(); tokens.push(";"); continue; }
    if (c === "|") { flush(); tokens.push("|"); continue; }
    cur += c;
  }
  flush();
  return tokens;
}

/** Split a token stream into simple-command segments on shell operators. */
function segments(tokens: string[]): string[][] {
  const segs: string[][] = [];
  let cur: string[] = [];
  for (const t of tokens) {
    if (t === "&&" || t === "||" || t === ";" || t === "|") {
      if (cur.length) segs.push(cur);
      cur = [];
    } else cur.push(t);
  }
  if (cur.length) segs.push(cur);
  return segs;
}

/** Drop leading env assignments (FOO=bar) and wrapper words (sudo, env, …). */
function stripWrappers(seg: string[]): string[] {
  let i = 0;
  while (i < seg.length) {
    const t = seg[i];
    if (ENV_ASSIGN.test(t)) { i++; continue; }
    if (WRAPPERS.has(basename(t))) {
      i++;
      while (i < seg.length && seg[i].startsWith("-")) {
        if (seg[i] === "-u" || seg[i] === "--user") i += 2;
        else i++;
      }
      continue;
    }
    break;
  }
  return seg.slice(i);
}

// ─── Per-tool classifiers ────────────────────────────────────────────────────
function classifyTerraform(args: string[]): Decision {
  let i = 0;
  while (i < args.length && args[i].startsWith("-")) i++;
  const sub = args[i];
  const rest = args.slice(i + 1);
  if (!sub) return { tier: "allow", reason: "terraform (no subcommand — help/usage)" };

  if (TF_ALLOW.has(sub)) return { tier: "allow", reason: `terraform ${sub} (read-only)` };
  if (TF_ASK.has(sub)) return { tier: "ask", reason: `terraform ${sub} (tears down / forces replacement)` };
  if (sub === "apply") {
    if (hasFlag(args, "-destroy")) return { tier: "ask", reason: "terraform apply -destroy" };
    // Dry-run first: run terraform plan, build table, auto-allow if adds-only.
    return { tier: "plan-check", reason: "terraform apply — will run plan first to assess changes" };
  }
  if (sub === "refresh") return { tier: "gray", reason: "terraform refresh (mutates state)" };
  if (sub === "state") {
    const op = rest.find((a) => !a.startsWith("-"));
    if (op === "rm" || op === "mv" || op === "replace-provider" || op === "push")
      return { tier: "ask", reason: `terraform state ${op} (rewrites state)` };
    if (op === "list" || op === "show" || op === "pull")
      return { tier: "allow", reason: `terraform state ${op} (read-only)` };
    return { tier: "gray", reason: `terraform state ${op ?? "?"}` };
  }
  if (sub === "workspace") {
    const op = rest.find((a) => !a.startsWith("-"));
    if (op === "delete") return { tier: "ask", reason: "terraform workspace delete" };
    if (op === "list" || op === "show") return { tier: "allow", reason: `terraform workspace ${op}` };
    return { tier: "gray", reason: `terraform workspace ${op ?? "?"}` };
  }
  return { tier: "gray", reason: `terraform ${sub} (unrecognized — verify)` };
}

function classifyAws(args: string[]): Decision {
  const positionals = args.filter((a) => !a.startsWith("-"));
  const service = positionals[0];
  const op = positionals[1];
  if (!service) return { tier: "allow", reason: "aws (no service — help/usage)" };

  if (service === "s3") {
    switch (op) {
      case "ls": case "presign": return { tier: "allow", reason: `aws s3 ${op}` };
      case "rb": return { tier: "ask", reason: "aws s3 rb (remove bucket)" };
      case "rm": return { tier: "ask", reason: `aws s3 rm${hasFlag(args, "--recursive") ? " --recursive" : ""} (deletes objects)` };
      case "mv": return { tier: "ask", reason: "aws s3 mv (deletes source after copy)" };
      case "sync":
        if (hasFlag(args, "--delete")) return { tier: "ask", reason: "aws s3 sync --delete (removes objects absent from source)" };
        return { tier: "gray", reason: "aws s3 sync (may overwrite)" };
      case "cp": return { tier: "gray", reason: "aws s3 cp (may overwrite destination)" };
      case undefined: return { tier: "allow", reason: "aws s3 (no subcommand — help/usage)" };
      default: return { tier: "gray", reason: `aws s3 ${op}` };
    }
  }

  if (!op) return { tier: "allow", reason: `aws ${service} (no operation — help/usage)` };
  const o = op.toLowerCase();

  if (AWS_ALLOW_PREFIX.test(o) || /-status$/.test(o) || o === "help" ||
      (service === "sts" && o === "get-caller-identity"))
    return { tier: "allow", reason: `aws ${service} ${op} (read-only or pure add)` };

  if (o === "schedule-key-deletion") return { tier: "ask", reason: "aws kms schedule-key-deletion" };
  if (service === "cloudformation" && o === "delete-stack")
    return { tier: "ask", reason: "aws cloudformation delete-stack" };

  if (AWS_ASK_PREFIX.test(o)) return { tier: "ask", reason: `aws ${service} ${op} (destructive or in-place update)` };
  if (AWS_DOWNTIME_PREFIX.test(o)) return { tier: "ask", reason: `aws ${service} ${op} (causes downtime)` };

  if (service === "cloudformation" &&
      (o === "deploy" || o === "execute-change-set" || o === "create-change-set"))
    return { tier: "gray", reason: `aws cloudformation ${op} (may replace/delete resources)` };

  if (AWS_GRAY_PREFIX.test(o)) return { tier: "gray", reason: `aws ${service} ${op} (state change — verify blast radius)` };

  if (AWS_DESTRUCTIVE_WORD.test(o)) return { tier: "ask", reason: `aws ${service} ${op} (names a destructive verb)` };

  return { tier: "gray", reason: `aws ${service} ${op} (unclassified — verify)` };
}

function classifyKubectl(args: string[]): Decision {
  const positionals = args.filter((a) => !a.startsWith("-"));
  const verb = positionals[0];
  if (!verb) return { tier: "allow", reason: "kubectl (no verb — help/usage)" };

  if (KC_ALLOW.has(verb)) return { tier: "allow", reason: `kubectl ${verb} (read-only or pure add)` };
  if (verb === "config") return positionals[1] === "view"
    ? { tier: "allow", reason: "kubectl config view" }
    : { tier: "gray", reason: `kubectl config ${positionals[1] ?? "?"}` };
  if (verb === "auth") return { tier: "allow", reason: `kubectl auth ${positionals[1] ?? ""}`.trim() };

  if (verb === "delete")
    return { tier: "ask", reason: `kubectl delete${hasFlag(args, "--all") ? " --all" : ""} (removes resources)` };
  if (KC_ASK.has(verb)) return { tier: "ask", reason: `kubectl ${verb} (in-place mutation or eviction)` };

  if (verb === "replace")
    return hasFlag(args, "--force")
      ? { tier: "ask", reason: "kubectl replace --force (delete + recreate)" }
      : { tier: "ask", reason: "kubectl replace (replaces existing resource)" };

  if (verb === "scale")
    return flagValue(args, "--replicas") === "0"
      ? { tier: "ask", reason: "kubectl scale --replicas=0 (stops workload)" }
      : { tier: "gray", reason: "kubectl scale (verify: scaling down may drop capacity)" };

  if (verb === "apply")
    return hasFlag(args, "--prune")
      ? { tier: "ask", reason: "kubectl apply --prune (deletes resources absent from config)" }
      : { tier: "plan-check", reason: "kubectl apply — will run diff first to assess changes" };

  if (verb === "rollout") {
    const op = positionals[1];
    if (op === "restart" || op === "undo" || op === "pause")
      return { tier: "ask", reason: `kubectl rollout ${op} (disrupts a live workload)` };
    if (op === "status" || op === "history")
      return { tier: "allow", reason: `kubectl rollout ${op} (read-only)` };
    return { tier: "gray", reason: `kubectl rollout ${op ?? "?"}` };
  }

  if (KC_GRAY.has(verb)) return { tier: "gray", reason: `kubectl ${verb} (verify: may update or replace)` };
  return { tier: "gray", reason: `kubectl ${verb} (unrecognized — verify)` };
}

/** Classify one simple-command segment (may recurse into `bash -c "…"`). */
function classifySegment(rawSeg: string[], depth = 0): Decision[] {
  if (depth > 4) return [{ tier: "ask", reason: "command nesting too deep (unparseable)" }];
  const seg = stripWrappers(rawSeg);
  if (seg.length === 0) return [];
  const bin = basename(seg[0]);
  const args = seg.slice(1);

  if (SHELLS.has(bin)) {
    const ci = args.indexOf("-c");
    if (ci >= 0 && args[ci + 1])
      return segments(tokenize(args[ci + 1])).flatMap((s) => classifySegment(s, depth + 1));
    return [];
  }
  if (TERRAFORM.has(bin)) return [classifyTerraform(args)];
  if (bin === "aws") return [classifyAws(args)];
  if (bin === "kubectl") return [classifyKubectl(args)];
  return [];
}

/** Classify a full bash command: the most dangerous in-scope segment wins. */
export function classifyCommand(command: string): Decision {
  let worst: Decision = { tier: "allow", reason: "no in-scope infra command" };
  for (const seg of segments(tokenize(command)))
    for (const d of classifySegment(seg))
      if (RANK[d.tier] > RANK[worst.tier]) worst = d;
  return worst;
}

// ─── Dry-run (plan-check tier) ───────────────────────────────────────────────
interface PlanCheckResult {
  tier: "allow" | "ask";
  reason: string; // used as the confirm-dialog message when tier === "ask"
}

function fmtTable(adds: number, changes: number, destroys: number): string {
  const rows = [
    `  + ${adds.toString().padStart(3)}  to add`,
    `  ~ ${changes.toString().padStart(3)}  to change${changes > 0 ? "  ← review required" : ""}`,
    `  - ${destroys.toString().padStart(3)}  to destroy${destroys > 0 ? "  ← review required" : ""}`,
  ];
  return rows.join("\n");
}

function terraformPlanCheck(bin: string, applyArgs: string[]): PlanCheckResult {
  // Rebuild the plan command from the apply args:
  // - swap "apply" for "plan"
  // - drop any -out=<file> (not needed for review)
  // - add -no-color for clean parsing
  const planArgs: string[] = [];
  for (const a of applyArgs) {
    if (a === "apply") planArgs.push("plan");
    else if (a.startsWith("-out=") || a.startsWith("--out=")) continue;
    else planArgs.push(a);
  }
  planArgs.push("-no-color");

  const r = spawnSync(bin, planArgs, {
    encoding: "utf-8",
    timeout: DRY_RUN_TIMEOUT_MS,
    env: process.env,
    input: "",
  });

  if (r.error) {
    return { tier: "ask", reason: `plan failed (${r.error.message}) — cannot auto-assess` };
  }

  const out = (r.stdout ?? "") + (r.stderr ?? "");

  if (/no changes/i.test(out)) {
    return { tier: "allow", reason: "terraform plan: no changes" };
  }

  const m = out.match(/Plan:\s*(\d+)\s*to add[^,]*,\s*(\d+)\s*to change[^,]*,\s*(\d+)\s*to destroy/i);
  if (!m) {
    return { tier: "ask", reason: `terraform plan output unparseable — failing closed\n\n${out.slice(0, 800)}` };
  }

  const adds = parseInt(m[1]);
  const changes = parseInt(m[2]);
  const destroys = parseInt(m[3]);

  if (changes === 0 && destroys === 0) {
    return { tier: "allow", reason: `terraform plan: ${adds} to add, no changes or destroys` };
  }

  return {
    tier: "ask",
    reason: `terraform plan — changes detected:\n\n${fmtTable(adds, changes, destroys)}\n\nRun terraform apply?`,
  };
}

function kubectlDiffCheck(applyArgs: string[]): PlanCheckResult {
  // Swap "apply" for "diff"; keep all other flags (-f, --filename, -n, etc.)
  const diffArgs = applyArgs.map((a) => (a === "apply" ? "diff" : a));

  const r = spawnSync("kubectl", diffArgs, {
    encoding: "utf-8",
    timeout: 60_000,
    env: process.env,
  });

  if (r.error) {
    return { tier: "ask", reason: `kubectl diff failed (${r.error.message}) — failing closed` };
  }

  // kubectl diff exits 0 = no differences, 1 = differences found, 2+ = error
  if ((r.status ?? 0) >= 2) {
    return { tier: "ask", reason: `kubectl diff error — failing closed\n\n${(r.stderr ?? "").slice(0, 400)}` };
  }
  if ((r.status ?? 0) === 0) {
    return { tier: "allow", reason: "kubectl diff: no changes" };
  }

  const out = r.stdout ?? "";
  // A line starting with "-" (not "---") means an existing value is being removed or replaced.
  const hasUpdates = out.split("\n").some((l) => l.startsWith("-") && !l.startsWith("---"));

  if (!hasUpdates) {
    return { tier: "allow", reason: "kubectl diff: additions only — no existing resources modified" };
  }

  // Count distinct resources changing (rough: count "---" diff-header separators)
  const resourceCount = (out.match(/^---/gm) ?? []).length;
  return {
    tier: "ask",
    reason: `kubectl diff — changes detected in ${resourceCount || "some"} resource(s):\n\n` +
      out.slice(0, 800) + (out.length > 800 ? "\n…(truncated)" : "") +
      "\n\nRun kubectl apply?",
  };
}

/** Dispatch the right dry-run for a plan-check command and return the verdict. */
function runPlanCheck(command: string, classifierReason: string): PlanCheckResult {
  for (const rawSeg of segments(tokenize(command))) {
    const seg = stripWrappers(rawSeg);
    if (seg.length === 0) continue;
    const bin = basename(seg[0]);
    const args = seg.slice(1);
    if (TERRAFORM.has(bin)) return terraformPlanCheck(bin, args);
    if (bin === "kubectl") return kubectlDiffCheck(args);
  }
  return { tier: "ask", reason: classifierReason };
}

// ─── Gray-zone verifier dispatch (reuses the codex-reviewer-hub socket) ──────
interface Verdict { decision: "allow" | "ask"; summary: string; }

function buildHandoff(command: string, classifierReason: string, output: string): string {
  return [
    "# IaC command — approval verdict request",
    "",
    "A coding agent is about to run the infrastructure command below. It was classified as",
    "**gray zone**: an operation that may or may not destroy or replace existing resources.",
    "Decide whether it is safe to run automatically (ALLOW) or a human must approve it (ASK).",
    "",
    "## Command",
    "```",
    command,
    "```",
    "",
    "## Classifier note",
    classifierReason,
    "",
    "## How to decide",
    "- ALLOW only if you are confident the command cannot delete, destroy, or replace existing",
    "  infrastructure or data (e.g. a pure add with no in-place mutation).",
    "- ASK if it could update/replace/destroy resources, cause downtime, or you cannot tell.",
    "  When in doubt, ASK.",
    "",
    "## Output",
    `Write your verdict to \`${output}\` using EXACTLY this format and nothing else:`,
    "",
    "```",
    "## Verdict",
    "DECISION: ALLOW | ASK",
    'BLAST_RADIUS: <what could be destroyed/replaced/disrupted, or "none">',
    "REASON: <one or two sentences>",
    "```",
  ].join("\n");
}

function dispatch(handoff: string, output: string): Promise<boolean> {
  return new Promise((resolve) => {
    let settled = false;
    const sock = net.createConnection({ path: SOCKET_PATH });
    const done = (v: boolean) => {
      if (settled) return;
      settled = true;
      try { sock.destroy(); } catch { /* ignore */ }
      resolve(v);
    };
    const timer = setTimeout(() => done(false), DISPATCH_CONNECT_MS);
    try { (timer as any).unref?.(); } catch { /* ignore */ }
    let buf = "";
    sock.once("connect", () => {
      sock.write(JSON.stringify({ handoff, output, agent: VERIFIER_AGENT, timeout_ms: GRAY_TIMEOUT_MS }) + "\n");
    });
    sock.on("data", (chunk) => {
      buf += chunk.toString("utf-8");
      const nl = buf.indexOf("\n");
      if (nl < 0) return;
      clearTimeout(timer);
      try { done(JSON.parse(buf.slice(0, nl)).status === "dispatched"); }
      catch { done(false); }
    });
    sock.once("error", () => { clearTimeout(timer); done(false); });
    sock.once("close", () => { clearTimeout(timer); done(false); });
  });
}

function parseVerdict(content: string): Verdict | null {
  const m = content.match(/^DECISION:\s*(ALLOW|ASK)\b/im);
  if (!m) return null;
  const decision = m[1].toUpperCase() === "ALLOW" ? "allow" : "ask";
  const br = content.match(/^BLAST_RADIUS:\s*(.+)$/im);
  const rs = content.match(/^REASON:\s*(.+)$/im);
  const summary =
    [br && `Blast radius: ${br[1].trim()}`, rs && rs[1].trim()].filter(Boolean).join(" — ") ||
    "verifier verdict";
  return { decision, summary };
}

const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));

async function requestVerdict(command: string, classifierReason: string): Promise<Verdict | null> {
  const id = crypto.randomBytes(8).toString("hex");
  const dir = path.join(os.tmpdir(), `iac-guard-${id}`);
  const handoff = path.join(dir, "handoff.md");
  const output = path.join(dir, "verdict.md");
  try {
    fs.mkdirSync(dir, { recursive: true });
    fs.writeFileSync(handoff, buildHandoff(command, classifierReason, output));
    if (!(await dispatch(handoff, output))) return null;

    const deadline = Date.now() + GRAY_TIMEOUT_MS;
    while (Date.now() < deadline) {
      try {
        if (fs.statSync(output).size > 0) {
          const v = parseVerdict(fs.readFileSync(output, "utf-8"));
          if (v) return v;
        }
      } catch { /* not written yet */ }
      await sleep(POLL_INTERVAL_MS);
    }
    return null;
  } finally {
    try { fs.rmSync(dir, { recursive: true, force: true }); } catch { /* best-effort */ }
  }
}

// ─── Human approval + audit ──────────────────────────────────────────────────
async function confirmHuman(ctx: any, command: string, reason: string): Promise<boolean> {
  const confirm = ctx?.ui?.confirm;
  if (typeof confirm !== "function") return false;
  const message = [reason, "", command, "", "Allow this command to run?"].join("\n");
  try {
    return await confirm.call(ctx.ui, "⚠ iac-guard — approval required", message, { timeout: CONFIRM_TIMEOUT_MS });
  } catch {
    return false;
  }
}

function audit(pi: ExtensionAPI, command: string, outcome: string, reason: string): void {
  try {
    (pi as any).appendEntry?.("iac-guard-log", { outcome, reason, command: command.slice(0, 500) });
  } catch { /* best-effort */ }
}

// ─── Extension entry ─────────────────────────────────────────────────────────
export default function (pi: ExtensionAPI) {
  pi.on("tool_call" as any, async (event: any, ctx: any) => {
    if (event?.toolName !== "bash") return;
    const command: string = event?.input?.command ?? "";
    if (!command.trim()) return;

    const decision = classifyCommand(command);
    if (decision.tier === "allow") return; // run silently

    let reason = decision.reason;

    if (decision.tier === "plan-check") {
      // Run the dry-run (terraform plan / kubectl diff) synchronously, build table.
      const result = runPlanCheck(command, decision.reason);
      if (result.tier === "allow") {
        audit(pi, command, "allow", `plan-check→ ALLOW (${result.reason})`);
        return; // adds only — safe to run
      }
      reason = result.reason; // table summary for the confirm dialog
      // Fall through to confirmHuman with the table as the reason.
    } else if (decision.tier === "gray") {
      const verdict = await requestVerdict(command, decision.reason).catch(() => null);
      if (verdict?.decision === "allow") {
        audit(pi, command, "allow", `gray→verifier ALLOW (${verdict.summary})`);
        return;
      }
      reason = verdict
        ? `verifier requires approval — ${verdict.summary}`
        : `verifier unavailable; failing closed — ${decision.reason}`;
    }

    const approved = await confirmHuman(ctx, command, reason);
    if (!approved) {
      audit(pi, command, "blocked", reason);
      return { block: true, reason: `iac-guard: blocked — ${reason}` };
    }
    audit(pi, command, "approved", reason);
    return;
  });
}
