// @ts-nocheck -- standalone Pi runtime; executable tests cover this extension.
/**
 * tf-approve — Terraform plan-aware approval gate for Pi
 *
 * Hooks into Pi AFTER terraform plan runs to capture the output.
 * When terraform apply or destroy is next attempted, sends the plan
 * to a reviewer agent via FIFO and shows a structured table to the
 * human for approval — instead of showing the raw command.
 *
 * The gate fails closed. Apply and destroy are blocked when there is no
 * captured plan, no reviewer response, or no confirmation UI. Saved plan
 * files are review evidence only and can never be passed to apply.
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
const REVIEWER_TIMEOUT_MS = 30_000;

// ─── Module-level state ──────────────────────────────────────────────────────
let lastPlanOutput = "";
let lastPlanMode: "apply" | "destroy" | "" = "";
let lastPlanCwd = "";

// ─── Terraform command checks ────────────────────────────────────────────────
function normalizedCwd(cwd: string): string {
	return path.resolve(cwd);
}

function requestedMode(command: string): "apply" | "destroy" | undefined {
	const cmd = command.trim().toLowerCase();
	if (/\b(?:terraform|tofu)\b.*\bdestroy\b/.test(cmd)) return "destroy";
	if (/\b(?:terraform|tofu)\b.*\bapply\b/.test(cmd)) {
		return /\b-destroy\b/.test(cmd) ? "destroy" : "apply";
	}
	return undefined;
}

const APPLY_OPTIONS_WITH_VALUES = new Set([
	"-backup",
	"-generate-config-out",
	"-invoke",
	"-lock-timeout",
	"-parallelism",
	"-replace",
	"-state",
	"-state-out",
	"-target",
	"-var",
	"-var-file",
]);
const APPLY_OPTIONS_WITHOUT_VALUES = new Set([
	"-auto-approve",
	"-compact-warnings",
	"-destroy",
	"-input",
	"-json",
	"-lock",
	"-no-color",
	"-refresh",
	"-refresh-only",
]);

function shellTokens(text: string): string[] {
	const tokens: string[] = [];
	const pattern = /"((?:\\.|[^"\\])*)"|'([^']*)'|([^\s]+)/g;
	let match: RegExpExecArray | null;
	while ((match = pattern.exec(text)) !== null) {
		tokens.push(match[1] ?? match[2] ?? match[3]);
	}
	return tokens;
}

export function savedPlanApplyBlockReason(command: string): string | undefined {
	for (const clause of command.split(/&&|\|\||[;|\n]/)) {
		const match = clause.match(
			/\b(?:terraform|tofu)\b(?:\s+-[^\s]+)*\s+apply\b(.*)$/i,
		);
		if (!match) continue;

		let expectsValue = false;
		for (const token of shellTokens(match[1] ?? "")) {
			if (/^\d*(?:>>?|<<?|>&|<&)/.test(token)) break;
			if (expectsValue) {
				expectsValue = false;
				continue;
			}
			if (!token.startsWith("-")) {
				return "tf-approve: saved plan files are review-only; run a fresh direct apply";
			}

			const option = token.split("=", 1)[0];
			if (APPLY_OPTIONS_WITH_VALUES.has(option)) {
				expectsValue = !token.includes("=");
				continue;
			}
			if (APPLY_OPTIONS_WITHOUT_VALUES.has(option)) continue;
			return `tf-approve: cannot verify fresh apply because option ${option} is unknown`;
		}
		if (expectsValue) {
			return "tf-approve: cannot verify fresh apply because an option value is missing";
		}
	}
	return undefined;
}

function isPlanCommand(command: string): boolean {
	return /\b(?:terraform|tofu)\b(?:\s+-[^\s]+)*\s+plan\b/i.test(command);
}

function toolResultText(event: any): string {
	const content = event?.content ?? event?.result?.content;
	if (Array.isArray(content)) {
		return content
			.filter(
				(item: any) => item?.type === "text" && typeof item.text === "string",
			)
			.map((item: any) => item.text)
			.join("\n")
			.trim();
	}
	if (typeof event?.output === "string") return event.output.trim();
	if (typeof event?.result?.output === "string")
		return event.result.output.trim();
	return "";
}

function capturePlan(event: any, ctx: any): void {
	const command = event?.input?.command ?? event?.args?.command ?? "";
	if (!isPlanCommand(command)) return;

	lastPlanOutput = event?.isError ? "" : toolResultText(event);
	lastPlanMode =
		lastPlanOutput && /\b-destroy\b/i.test(command)
			? "destroy"
			: lastPlanOutput
				? "apply"
				: "";
	lastPlanCwd = lastPlanOutput ? normalizedCwd(ctx?.cwd ?? process.cwd()) : "";
}

// ─── FIFO helpers ────────────────────────────────────────────────────────────
function withTimeout<T>(promise: Promise<T>, ms: number): Promise<T> {
	return Promise.race([
		promise,
		new Promise<never>((_, reject) =>
			setTimeout(
				() =>
					reject(
						new Error(
							`reviewer timed out after ${ms}ms — is tf-reviewer running?`,
						),
					),
				ms,
			),
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
			chunks.push(
				Buffer.isBuffer(chunk) ? chunk : Buffer.from(chunk as string),
			),
		);
		stream.on("end", () =>
			resolve(Buffer.concat(chunks).toString("utf-8").trim()),
		);
		stream.on("error", reject);
	});
}

// ─── Response type ───────────────────────────────────────────────────────────
interface PlanResponse {
	message: string;
}

// ─── Extension entry ─────────────────────────────────────────────────────────
export default function (pi: ExtensionAPI) {
	// ── /tf:approve — register slash command ─────────────────────────────────
	(pi as any).registerCommand("tf:approve", {
		description:
			"Confirm the terraform apply/destroy approval gate is active in this session",
		handler: async (_args: string, ctx: any) => {
			ctx.ui.notify(
				"tf:approve is active — terraform apply/destroy will require human approval before running",
				"info",
			);
		},
	});

	// Capture successful plan output for the next matching apply or destroy. The
	// result event carries the bash input and rendered text in current Pi.
	pi.on("tool_result" as any, async (event: any, ctx: any) => {
		capturePlan(event, ctx);
	});

	// Gate terraform/tofu apply and destroy with review evidence and a human decision.
	pi.on("tool_call" as any, async (event: any, ctx: any) => {
		if (event?.toolName !== "bash") return;
		const command: string = event?.input?.command ?? "";
		if (!command.trim()) return;

		const mode = requestedMode(command);
		if (!mode) return;

		const savedPlanReason = savedPlanApplyBlockReason(command);
		if (savedPlanReason) return { block: true, reason: savedPlanReason };

		if (!lastPlanOutput) {
			return {
				block: true,
				reason:
					"tf-approve: apply/destroy requires a fresh plan and explicit human approval",
			};
		}
		if (lastPlanMode !== mode) {
			return {
				block: true,
				reason: `tf-approve: ${mode} requires a fresh ${mode} plan before human approval`,
			};
		}

		const cwd: string = normalizedCwd(ctx?.cwd ?? process.cwd());
		if (!lastPlanCwd || lastPlanCwd !== cwd) {
			return {
				block: true,
				reason: `tf-approve: plan evidence belongs to ${lastPlanCwd || "an unknown directory"}; run a fresh plan in ${cwd}`,
			};
		}

		const confirm = ctx?.ui?.confirm;
		if (!ctx?.hasUI || typeof confirm !== "function") {
			return {
				block: true,
				reason: "tf-approve: no UI available to confirm terraform changes",
			};
		}

		let tableString: string;
		try {
			ensureFifo(REQUEST_FIFO);
			ensureFifo(RESPONSE_FIFO);
			await withTimeout(
				writeFifo(
					REQUEST_FIFO,
					JSON.stringify({ type: "plan_summary", plan_output: lastPlanOutput }),
				),
				REVIEWER_TIMEOUT_MS,
			);
			const responseText = await withTimeout(
				readFifo(RESPONSE_FIFO),
				REVIEWER_TIMEOUT_MS,
			);
			if (!responseText) throw new Error("reviewer returned empty response");
			const resp: PlanResponse = JSON.parse(responseText);
			if (typeof resp.message !== "string" || !resp.message.trim()) {
				return {
					block: true,
					reason: "tf-approve: reviewer returned no approval evidence",
				};
			}
			tableString = resp.message;
		} catch (err) {
			const msg = err instanceof Error ? err.message : String(err);
			const isAbsent =
				msg.includes("timed out") ||
				(err as NodeJS.ErrnoException).code === "ENXIO";
			if (isAbsent) {
				return {
					block: true,
					reason: `tf-approve: reviewer unavailable: ${msg}`,
				};
			}
			throw err;
		}

		const confirmMessage = [tableString, "", `cd ${cwd}`, command.trim()].join(
			"\n",
		);
		const approved = await confirm.call(
			ctx.ui,
			"Terraform approval required",
			confirmMessage,
			{ timeout: CONFIRM_TIMEOUT_MS },
		);

		if (!approved) {
			return { block: true, reason: "Denied by human." };
		}
		lastPlanOutput = "";
		lastPlanMode = "";
		lastPlanCwd = "";
	});
}
