/// <reference lib="es2022" />
// Zero-dependency behavioral tests for the Terraform/OpenTofu approval gate.
// Runs on Node 22.6+ via native type-stripping.
// @ts-expect-error - Node native type-stripping resolves TypeScript directly.
import * as tfApproveModule from "./tf-approve.ts";

const { default: tfApprove, savedPlanApplyBlockReason } = tfApproveModule;

function assert(condition: unknown, message: string): asserts condition {
	if (!condition) throw new Error(message);
}

const savedPlanCommands = [
	"tofu apply plan.bin",
	"terraform apply 'approved.tfplan'",
	"terraform apply -input=false /tmp/review.plan",
	"terraform -chdir=infra apply -var-file=prod.tfvars saved-plan",
];
for (const command of savedPlanCommands) {
	assert(
		savedPlanApplyBlockReason(command)?.includes("review-only"),
		`Expected saved-plan apply to be blocked: ${command}`,
	);
}

const freshCommands = [
	"tofu apply",
	"terraform apply -input=false",
	"cd /tmp/infra && terraform apply 2>&1 | tee apply.log",
	"tofu destroy",
];
for (const command of freshCommands) {
	assert(
		savedPlanApplyBlockReason(command) === undefined,
		`Expected fresh direct command to be allowed: ${command}`,
	);
}

type Handler = (event: unknown, context: unknown) => Promise<unknown>;

async function testGate(): Promise<void> {
	const handlers = new Map<string, Handler>();
	const fakePi = {
		registerCommand(): void {},
		on(name: string, handler: Handler): void {
			handlers.set(name, handler);
		},
	};

	tfApprove(fakePi);
	const gate = handlers.get("tool_call");
	const capture = handlers.get("tool_result");
	assert(
		gate && capture,
		"Expected tf-approve to register plan capture and apply gate handlers",
	);

	const missingPlan = (await gate(
		{ toolName: "bash", input: { command: "tofu apply" } },
		{ hasUI: true, cwd: "/tmp/infra", ui: { confirm: async () => true } },
	)) as { block?: boolean; reason?: string };
	assert(
		missingPlan.block === true &&
			missingPlan.reason?.includes("requires a fresh plan"),
		"Apply without captured plan must fail closed",
	);

	await capture(
		{
			toolName: "bash",
			input: { command: "tofu plan" },
			content: [
				{ type: "text", text: "Plan: 1 to add, 0 to change, 0 to destroy." },
			],
			isError: false,
		},
		{ cwd: "/tmp/infra" },
	);
	const mismatchedDestroy = (await gate(
		{ toolName: "bash", input: { command: "tofu destroy" } },
		{ hasUI: true, cwd: "/tmp/infra", ui: { confirm: async () => true } },
	)) as { block?: boolean; reason?: string };
	assert(
		mismatchedDestroy.block === true &&
			mismatchedDestroy.reason?.includes("fresh destroy plan"),
		"Destroy must not reuse a normal apply plan",
	);

	const staleCwd = (await gate(
		{ toolName: "bash", input: { command: "tofu apply" } },
		{ hasUI: true, cwd: "/tmp/other", ui: { confirm: async () => true } },
	)) as { block?: boolean; reason?: string };
	assert(
		staleCwd.block === true &&
			staleCwd.reason?.includes("belongs to /tmp/infra"),
		"Plan evidence must not cross working directories",
	);

	const missingUi = (await gate(
		{ toolName: "bash", input: { command: "tofu apply" } },
		{ hasUI: false, cwd: "/tmp/infra", ui: {} },
	)) as { block?: boolean; reason?: string };
	assert(
		missingUi.block === true && missingUi.reason?.includes("no UI"),
		"Apply without confirmation UI must fail closed",
	);
}

void testGate().then(() => console.log("tf-approve: ok"));
