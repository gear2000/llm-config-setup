/**
 * tf-approve — Terraform plan-aware approval gate for Pi
 *
 * Hooks into Pi AFTER terraform plan runs to capture the output.
 * When terraform apply or destroy is next attempted, sends the plan
 * to a reviewer agent via FIFO and shows a structured table to the
 * human for approval — instead of showing the raw command.
 *
 * Falls through to iac-guard's normal dialog when no plan output
 * has been captured yet (e.g. agent skipped plan and went straight
 * to apply).
 *
 * FIFOs:
 *   ~/.pi/tf-review-request.fifo   — extension writes raw plan → agent reads
 *   ~/.pi/tf-review-response.fifo  — agent writes pre-formatted message → extension reads
 */

import type { ExtensionAPI } from "@mariozechner/pi-coding-agent";
import * as fs from "node:fs";
import * as path from "node:path";
import * as os from "node:os";
import { spawnSync } from "node:child_process";

// ─── Config ──────────────────────────────────────────────────────────────────
const PI_DIR = path.join(os.homedir(), ".pi");
const REQUEST_FIFO = path.join(PI_DIR, "tf-review-request.fifo");
const RESPONSE_FIFO = path.join(PI_DIR, "tf-review-response.fifo");
const CONFIRM_TIMEOUT_MS = 120_000;

// ─── Module-level state ──────────────────────────────────────────────────────
let lastPlanOutput = "";

// ─── Terraform destructive check (inlined to avoid cross-repo import) ────────
//
// The full classifier lives in iac-guard.ts (classifyCommand). We only need the
// terraform apply/destroy tier here — iac-guard gates everything else.
function isTerraformDestructive(command: string): boolean {
  const cmd = command.trim().toLowerCase();
  return (
    /\bterraform\b.*\b(apply|destroy)\b/.test(cmd) ||
    /\btofu\b.*\b(apply|destroy)\b/.test(cmd)
  );
}

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
  if (!fs.existsSync(PI_DIR)) {
    fs.mkdirSync(PI_DIR, { recursive: true });
  }
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

// ─── Response type ────────────────────────────────────────────────────────────
interface PlanResponse {
  message: string;
}

// ─── Extension entry ─────────────────────────────────────────────────────────
export default function (pi: ExtensionAPI) {
  // ── /tf:approve — register slash command ─────────────────────────────────
  (pi as any).registerCommand("tf:approve", {
    description: "Confirm the terraform apply/destroy approval gate is active in this session",
    handler: async (_args: string, ctx: any) => {
      ctx.ui.notify(
        "tf:approve is active — terraform apply/destroy will require human approval before running",
        "info"
      );
    },
  });


  // Capture the output of every terraform plan so we can show a structured
  // summary when the agent moves on to apply or destroy.
  pi.on("tool_execution_end" as any, async (event: any) => {
    const command: string = event?.input?.command ?? "";
    if (/\b(terraform|tofu)\s+plan\b/.test(command)) {
      lastPlanOutput = event?.output ?? "";
    }
  });

  // Gate terraform apply / destroy — replace the raw command dialog with a
  // structured table from the reviewer agent.
  pi.on("tool_call" as any, async (event: any, ctx: any) => {
    if (event?.toolName !== "bash") return;
    const command: string = event?.input?.command ?? "";
    if (!command.trim()) return;

    // Only intercept terraform apply / destroy
    if (!isTerraformDestructive(command)) return;

    // No plan output yet — fall through so iac-guard shows its normal dialog
    if (!lastPlanOutput) return;

    // Send plan to reviewer agent; get back a structured table
    let tableString: string;
    try {
      ensureFifo(REQUEST_FIFO);
      ensureFifo(RESPONSE_FIFO);
      await withTimeout(
        writeFifo(REQUEST_FIFO, JSON.stringify({ type: "plan_summary", plan_output: lastPlanOutput })),
        REVIEWER_TIMEOUT_MS
      );
      const responseText = await withTimeout(readFifo(RESPONSE_FIFO), REVIEWER_TIMEOUT_MS);
      if (!responseText) throw new Error("reviewer returned empty response");
      const resp: PlanResponse = JSON.parse(responseText);
      tableString = resp.message;
    } catch (err) {
      const msg = err instanceof Error ? err.message : String(err);
      const isAbsent =
        msg.includes("timed out") ||
        (err as NodeJS.ErrnoException).code === "ENXIO";
      if (isAbsent) return; // reviewer not running — fall through to iac-guard's dialog
      throw err; // unexpected error — propagate loud
    }

    // Build the full confirmation message: directory + command + plan table
    const cwd: string = ctx?.cwd ?? process.cwd();
    const confirmMessage = [
      tableString,
      "",
      `cd ${cwd}`,
      command.trim(),
    ].join("\n");

    // Show the table to the human
    const confirm = ctx?.ui?.confirm;
    if (typeof confirm !== "function") {
      return { block: true, reason: "tf-approve: no UI available to confirm terraform changes" };
    }

    let approved: boolean;
    try {
      approved = await ctx.ui.confirm("Terraform approval required", confirmMessage, {
        timeout: CONFIRM_TIMEOUT_MS,
      });
    } catch {
      approved = false;
    }

    if (!approved) {
      return { block: true, reason: "Denied by human." };
    }
    // Approved — let the command run
  });
}
