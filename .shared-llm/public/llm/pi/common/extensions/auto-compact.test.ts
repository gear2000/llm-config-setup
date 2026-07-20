/// <reference lib="es2022" />
// Zero-dependency behavioral test for the auto-compact Pi extension.
// Runs on Node 22.6+ via native type-stripping.
//   node --experimental-strip-types .shared-llm/public/llm/pi/common/extensions/auto-compact.test.ts
// @ts-expect-error -- Node native type-stripping resolves the TypeScript source directly.
import * as autoCompactModule from "./auto-compact.ts";
const {
	default: autoCompact,
	AUTO_COMPACT_INSTRUCTIONS,
	AUTO_COMPACT_PERCENT,
	RETRY_BASE_MS,
	RETRY_MAX_MS,
} = autoCompactModule;

type EventName = "session_start" | "session_shutdown" | "model_select" | "agent_settled";
type EventHandler = (event: unknown, ctx: FakeContext) => void;
type Mode = "tui" | "rpc" | "json" | "print";

type CompactOptions = {
	customInstructions?: string;
	onComplete?: () => void;
	onError?: (error: Error) => void;
};

type Usage = {
	tokens: number | null;
	contextWindow: number;
	percent: number | null;
};

type FakeContext = {
	mode: Mode;
	hasUI: boolean;
	ui: { notify(message: string, level: string): void };
	getContextUsage(): Usage | undefined;
	compact(options?: CompactOptions): void;
};

type Command = {
	description: string;
	handler(args: string, ctx: FakeContext): void;
};

const handlers = new Map<EventName, EventHandler>();
const commands = new Map<string, Command>();
const compactCalls: CompactOptions[] = [];
const notifications: Array<{ message: string; level: string }> = [];
let usage: Usage | undefined = {
	tokens: 49_000,
	contextWindow: 100_000,
	percent: 49,
};

const fakePi = {
	on(event: EventName, handler: EventHandler): void {
		handlers.set(event, handler);
	},
	registerCommand(name: string, command: Command): void {
		commands.set(name, command);
	},
};

const ctx: FakeContext = {
	mode: "tui",
	hasUI: true,
	ui: {
		notify(message, level): void {
			notifications.push({ message, level });
		},
	},
	getContextUsage(): Usage | undefined {
		return usage;
	},
	compact(options = {}): void {
		compactCalls.push(options);
	},
};

autoCompact(fakePi as never);

let passed = 0;
const failures: string[] = [];
function check(condition: boolean, name: string): void {
	if (condition) passed += 1;
	else failures.push(`  ✗ ${name}`);
}

const settled = handlers.get("agent_settled");
const modelSelect = handlers.get("model_select");
const sessionStart = handlers.get("session_start");
const sessionShutdown = handlers.get("session_shutdown");
const status = commands.get("auto-compact-status");
check(Boolean(settled), "registers agent_settled handler");
check(Boolean(modelSelect), "registers model_select handler");
check(Boolean(sessionStart), "registers session_start handler");
check(Boolean(sessionShutdown), "registers session_shutdown handler");
check(Boolean(status), "registers status command");

const realDateNow = Date.now;
let now = 1_000_000;
Date.now = () => now;

settled?.({}, ctx);
check(compactCalls.length === 0, "does not compact below 50%");

usage = { tokens: 50_000, contextWindow: 100_000, percent: AUTO_COMPACT_PERCENT };
settled?.({}, ctx);
check(compactCalls.length === 1, "compacts exactly at 50%");
check(
	compactCalls[0]?.customInstructions === AUTO_COMPACT_INSTRUCTIONS,
	"passes preservation instructions to Pi native compaction",
);
check(
	AUTO_COMPACT_INSTRUCTIONS.includes("rationale") &&
		AUTO_COMPACT_INSTRUCTIONS.includes("failed approaches"),
	"preserves rationale and rejected approaches",
);
settled?.({}, ctx);
check(compactCalls.length === 1, "blocks duplicate settled events while compaction is active");

const notificationCountBeforeStaleCallback = notifications.length;
modelSelect?.({}, ctx);
compactCalls[0]?.onError?.(new Error("old-model failure"));
check(
	notifications.length === notificationCountBeforeStaleCallback,
	"ignores stale callbacks after a model switch",
);
settled?.({}, ctx);
check(compactCalls.length === 2, "re-evaluates the ratio for the newly selected model");

compactCalls[1]?.onComplete?.();
settled?.({}, ctx);
check(compactCalls.length === 2, "does not compact repeatedly while still above threshold");
usage = { tokens: 40_000, contextWindow: 100_000, percent: 40 };
settled?.({}, ctx);
usage = { tokens: 51_000, contextWindow: 100_000, percent: 51 };
settled?.({}, ctx);
check(compactCalls.length === 3, "re-arms after fresh post-compaction usage falls below 50%");

const realConsoleError = console.error;
const stderr: string[] = [];
console.error = (...parts: unknown[]) => stderr.push(parts.map(String).join(" "));
ctx.hasUI = false;
compactCalls[2]?.onError?.(new Error("provider unavailable"));
ctx.hasUI = true;
console.error = realConsoleError;
check(
	stderr.some((line) => line.includes("provider unavailable")),
	"reports compaction failures loudly without a UI",
);

settled?.({}, ctx);
check(compactCalls.length === 3, "does not retry before the first backoff expires");
now += RETRY_BASE_MS;
settled?.({}, ctx);
check(compactCalls.length === 4, "retries on a later settled run after the first backoff");

compactCalls[3]?.onError?.(new Error("second failure"));
now += RETRY_BASE_MS * 2 - 1;
settled?.({}, ctx);
check(compactCalls.length === 4, "doubles the retry delay after a second failure");
now += 1;
settled?.({}, ctx);
check(compactCalls.length === 5, "retries when the doubled backoff expires");

compactCalls[4]?.onError?.(new Error("third failure"));
now += RETRY_BASE_MS * 4;
settled?.({}, ctx);
compactCalls[5]?.onError?.(new Error("fourth failure"));
now += RETRY_BASE_MS * 8;
settled?.({}, ctx);
compactCalls[6]?.onError?.(new Error("fifth failure"));
now += RETRY_MAX_MS - 1;
settled?.({}, ctx);
check(compactCalls.length === 7, "caps exponential retry delay at 15 minutes");
now += 1;
settled?.({}, ctx);
check(compactCalls.length === 8, "retries when the capped delay expires");

const beforeSessionReset = compactCalls.length;
sessionStart?.({}, ctx);
compactCalls[7]?.onError?.(new Error("stale session failure"));
settled?.({}, ctx);
check(
	compactCalls.length === beforeSessionReset + 1,
	"session reset invalidates stale callbacks and re-arms the policy",
);

sessionStart?.({}, ctx);
const beforeSingleShotChecks = compactCalls.length;
ctx.mode = "print";
settled?.({}, ctx);
ctx.mode = "json";
settled?.({}, ctx);
check(
	compactCalls.length === beforeSingleShotChecks,
	"never starts fire-and-forget compaction in print or JSON mode",
);
ctx.mode = "tui";

usage = { tokens: 42_000, contextWindow: 100_000, percent: 42 };
status?.handler("", ctx);
check(
	notifications.at(-1)?.message.includes("42.0%") === true,
	"status command reports measured context usage",
);
usage = { tokens: null, contextWindow: 100_000, percent: null };
status?.handler("", ctx);
check(
	notifications.at(-1)?.message.includes("unavailable") === true,
	"status command explains unknown post-compaction usage",
);

sessionShutdown?.({}, ctx);
Date.now = realDateNow;

const total = passed + failures.length;
console.log(`auto-compact: ${passed}/${total} passed`);
if (failures.length > 0) {
	console.error("FAILURES:");
	console.error(failures.join("\n"));
	throw new Error(`auto-compact failed ${failures.length} check(s)`);
}
console.log("ALL PASS ✓");
