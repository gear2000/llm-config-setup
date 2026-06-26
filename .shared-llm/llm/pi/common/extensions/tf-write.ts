/**
 * tf-write — Autonomous Terraform file authoring loop for Pi
 *
 * Loads a Terraform plan from TF_PLAN_PATH, injects it into Pi's system
 * prompt, then intercepts the "echo TF_REVIEW_READY" signal Pi emits when
 * it considers itself done. At that point the extension bundles all .tf files
 * in cwd and routes them to an external reviewer via named pipes (FIFOs).
 *
 * Reviewer protocol:
 *   Extension → tf-review-request.fifo  — JSON: { type: "code_review", files: Record<filename, content> }
 *   Reviewer  → tf-review-response.fifo — JSON: { status: "approved" }
 *                                       |        { status: "issues", escalate: boolean, items: string[] }
 *
 * FIFOs:
 *   ~/.pi/tf-review-request.fifo   — extension writes, reviewer reads
 *   ~/.pi/tf-review-response.fifo  — reviewer writes, extension reads
 *
 * Usage: TF_PLAN_PATH=/path/to/plan.txt pi -e extensions/tf-write.ts
 */

import type { ExtensionAPI } from "@mariozechner/pi-coding-agent";
import * as fs from "node:fs";
import * as path from "node:path";
import * as os from "node:os";
import { spawnSync } from "node:child_process";

// ─── Config ───────────────────────────────────────────────────────────────────

const PI_DIR = path.join(os.homedir(), ".pi");
const REQUEST_FIFO = path.join(PI_DIR, "tf-review-request.fifo");
const RESPONSE_FIFO = path.join(PI_DIR, "tf-review-response.fifo");

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

// ─── Extension entry ──────────────────────────────────────────────────────────

export default function (pi: ExtensionAPI) {
  // State persists across turns (closure-level, not event-level).
  let planContent = "";
  let pendingIssues: string[] = [];

  // ── session_start: set up FIFOs and load the plan ──────────────────────────

  pi.on("session_start", async (_event, ctx) => {
    // Create FIFOs if they don't exist.
    fs.mkdirSync(PI_DIR, { recursive: true });
    try {
      ensureFifo(REQUEST_FIFO);
      ensureFifo(RESPONSE_FIFO);
    } catch (err) {
      ctx.ui.notify(
        `tf-write: failed to create FIFOs — ${err instanceof Error ? err.message : String(err)}`,
        "error"
      );
      return;
    }

    // Load the plan.
    const planPath = process.env.TF_PLAN_PATH;
    if (!planPath) {
      ctx.ui.notify(
        "tf-write: TF_PLAN_PATH is not set — no plan loaded; set the env var and restart.",
        "warning"
      );
      return;
    }

    try {
      planContent = fs.readFileSync(planPath, "utf-8");
      ctx.ui.notify(`tf-write: plan loaded from ${planPath}`, "info");
    } catch (err) {
      ctx.ui.notify(
        `tf-write: failed to read plan at ${planPath} — ${err instanceof Error ? err.message : String(err)}`,
        "error"
      );
    }
  });

  // ── before_agent_start: inject plan (and any pending issues) into the prompt ─

  pi.on("before_agent_start", async (event) => {
    if (!planContent) return;

    const issuesBlock =
      pendingIssues.length > 0
        ? `The reviewer found these issues with your previous iteration. Fix them before signalling ready:\n\n${pendingIssues.join("\n")}\n\n`
        : "";

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

    // Only intercept the review-ready signal.
    const trimmed = command.trim();
    if (
      trimmed !== "echo TF_REVIEW_READY" &&
      !trimmed.startsWith("echo TF_REVIEW_READY")
    ) {
      return;
    }

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
        reason: `tf-write: failed to read .tf files from ${cwd} — ${err instanceof Error ? err.message : String(err)}`,
      };
    }

    // Send files to reviewer and wait for response.
    let response: ReviewResponse;
    try {
      const payload = JSON.stringify({ type: "code_review", files: tfFiles, plan: planContent });
      await withTimeout(writeFifo(REQUEST_FIFO, payload), REVIEWER_TIMEOUT_MS);
      const raw = await withTimeout(readFifo(RESPONSE_FIFO), REVIEWER_TIMEOUT_MS);
      if (!raw) throw new Error("reviewer returned empty response");
      response = JSON.parse(raw) as ReviewResponse;
    } catch (err) {
      return {
        block: true,
        reason: `tf-write: FIFO error during review — ${err instanceof Error ? err.message : String(err)}`,
      };
    }

    // Branch on reviewer verdict.
    if (response.status === "approved") {
      pendingIssues = [];
      return {
        block: true,
        reason: "Code approved by reviewer. tf:write complete.",
      };
    }

    if (response.status === "issues") {
      if (!response.escalate) {
        // Reviewer says fix it — block the echo and tell Pi what to fix.
        pendingIssues = response.items;
        const issueList = response.items
          .map((i) => `  - ${i}`)
          .join("\n");
        return {
          block: true,
          reason: `Reviewer found ${response.items.length} issue(s). Fix them and run echo TF_REVIEW_READY again:\n\n${issueList}`,
        };
      }

      // Reviewer flagged issues for human escalation.
      const issueList = response.items.map((i) => `  - ${i}`).join("\n");
      const confirmUI = ctx?.ui?.confirm;
      if (typeof confirmUI !== "function") {
        // No UI — treat as rejected; tell Pi to fix the issues.
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
        return {
          block: true,
          reason: "Approved by human override.",
        };
      }

      // Human rejected — send Pi back to fix the issues.
      pendingIssues = response.items;
      return {
        block: true,
        reason: `Human review rejected. Fix the following before signalling ready again:\n\n${issueList}`,
      };
    }

    // Unknown response shape — fail loud.
    return {
      block: true,
      reason: `tf-write: unexpected reviewer response: ${JSON.stringify(response)}`,
    };
  });
}
