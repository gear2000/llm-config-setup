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
 * 2. Plannotator  /tf:auto [description]
 *    Use the Plannotator extension as the planning step first. Pi writes PLAN.md,
 *    submits it via plannotator_submit_plan, the user approves in the browser, then
 *    Pi implements the .tf files. The approved PLAN.md is read from cwd and passed
 *    to the reviewer as context.
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
  let planContent = "";           // set in standalone mode
  let pendingIssues: string[] = [];
  let plannotatorMode = false;    // set by /tf:auto; clears on completion

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
        plannotatorMode = false;
        ctx.ui.notify(`tf:implement: plan loaded from ${planPath} — ask Pi to write .tf files`, "info");
      } catch (err) {
        ctx.ui.notify(
          `tf:implement: failed to read plan at ${planPath} — ${err instanceof Error ? err.message : String(err)}`,
          "error"
        );
      }
    },
  });

  // ── /tf:auto [description] — Plannotator-first workflow ──────────────────
  (pi as any).registerCommand("tf:auto", {
    description: "Plan with Plannotator first, then implement: /tf:auto [optional description]",
    handler: async (args: string, ctx: any) => {
      if (!setupFifos(ctx)) return;
      planContent = "";
      pendingIssues = [];
      plannotatorMode = true;
      const hint = args.trim();
      const msg = hint
        ? `tf:auto: Plannotator planning mode active — Pi will plan "${hint}", submit for your review, then implement the .tf files after approval.`
        : "tf:auto: Plannotator planning mode active — tell Pi what Terraform infrastructure to build. It will create a plan for your review, then implement after approval.";
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
      plannotatorMode = false;
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

    if (plannotatorMode) {
      // Plannotator mode: cover both the planning phase (create PLAN.md, submit)
      // and the execution phase (write .tf files, signal ready) in a single prompt.
      // Plannotator injects its own approved-plan context on transition, so we keep
      // our addition minimal and non-conflicting.
      return {
        systemPrompt:
          event.systemPrompt +
          `\n\n${issuesBlock}You are a Terraform infrastructure engineer.\n\n` +
          (pendingIssues.length > 0
            ? "Fix the issues above in your .tf files. When all issues are resolved, run:\n  echo TF_REVIEW_READY\n\nDo NOT reopen the planning phase."
            : "STEP 1 — PLAN: Write a Terraform implementation plan to PLAN.md. Include: what resources to create, the file/module structure, and key variables. Then submit it for user review by calling the plannotator_submit_plan tool.\n\n" +
              "STEP 2 — IMPLEMENT: Once the plan is approved, write all .tf files to implement it exactly. Follow the approved plan.\n\n" +
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

    if (!planContent && !plannotatorMode) return; // no active session

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

    // In plannotator mode, read the approved plan from PLAN.md in cwd.
    let planForReview = planContent;
    if (plannotatorMode && !planForReview) {
      try {
        const planFilePath = path.join(cwd, "PLAN.md");
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
      plannotatorMode = false;
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
        plannotatorMode = false;
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
