interface ContextUsage {
	tokens: number | null;
	contextWindow: number;
	percent: number | null;
}

interface CompactOptions {
	customInstructions?: string;
	onComplete?: () => void;
	onError?: (error: Error) => void;
}

interface ExtensionContext {
	mode: "tui" | "rpc" | "json" | "print";
	hasUI: boolean;
	ui: {
		notify(message: string, level: "info" | "warning" | "error"): void;
	};
	getContextUsage(): ContextUsage | undefined;
	compact(options?: CompactOptions): void;
}

interface ExtensionAPI {
	on(
		event:
			| "session_start"
			| "session_shutdown"
			| "model_select"
			| "agent_settled",
		handler: (event: unknown, ctx: ExtensionContext) => void,
	): void;
	registerCommand(
		name: string,
		options: {
			description: string;
			handler: (args: string, ctx: ExtensionContext) => void;
		},
	): void;
}

/** Compact once the active model's measured context reaches this percentage. */
export const AUTO_COMPACT_PERCENT = 50;

/** Retry only after another settled run, with a bounded delay after failures. */
export const RETRY_BASE_MS = 60_000;
export const RETRY_MAX_MS = 15 * 60_000;

export const AUTO_COMPACT_INSTRUCTIONS = `This compaction was triggered automatically to keep the active context focused.
Preserve all information needed to continue the work:
- the user's current goal, requirements, constraints, and preferences;
- completed work, current state, and concrete next steps;
- decisions together with their rationale;
- errors, successful fixes, failed approaches, and why those approaches were rejected;
- exact evidence pointers such as file paths, symbols, commands, URLs, and durable artifact paths.
Summarize repetitive discussion and raw tool output aggressively. Do not invent missing facts.`;

interface AutoCompactState {
	armed: boolean;
	compacting: boolean;
	consecutiveFailures: number;
	nextAttemptAt: number;
}

function freshState(): AutoCompactState {
	return {
		armed: true,
		compacting: false,
		consecutiveFailures: 0,
		nextAttemptAt: 0,
	};
}

function formatUsage(
	tokens: number,
	contextWindow: number,
	percent: number,
): string {
	return `${tokens.toLocaleString()} / ${contextWindow.toLocaleString()} tokens (${percent.toFixed(1)}%)`;
}

export default function autoCompact(pi: ExtensionAPI): void {
	let state = freshState();
	let generation = 0;

	const resetState = (): void => {
		generation += 1;
		state = freshState();
	};

	const notify = (
		ctx: ExtensionContext,
		message: string,
		level: "info" | "warning" | "error",
	): void => {
		if (ctx.hasUI) {
			ctx.ui.notify(message, level);
		} else if (level === "error") {
			console.error(`[auto-compact] ${message}`);
		} else if (ctx.mode === "print") {
			console.log(`[auto-compact] ${message}`);
		}
	};

	const maybeCompact = (ctx: ExtensionContext): void => {
		// Print and JSON runtimes dispose immediately after a prompt settles. Starting
		// fire-and-forget compaction there races teardown and aborts the request.
		if (ctx.mode !== "tui" && ctx.mode !== "rpc") return;

		const usage = ctx.getContextUsage();
		if (
			!usage ||
			usage.percent === null ||
			usage.tokens === null ||
			state.compacting
		) {
			return;
		}

		// A successful compaction reports unknown usage until a fresh response. Once
		// that response proves the context is below the threshold, arm the next cycle.
		if (usage.percent < AUTO_COMPACT_PERCENT) {
			state.armed = true;
			state.consecutiveFailures = 0;
			state.nextAttemptAt = 0;
			return;
		}

		const now = Date.now();
		if (!state.armed || now < state.nextAttemptAt) return;

		const attemptGeneration = generation;
		state.armed = false;
		state.compacting = true;
		notify(
			ctx,
			`Automatic compaction starting at ${formatUsage(usage.tokens, usage.contextWindow, usage.percent)}`,
			"info",
		);

		const options: CompactOptions = {
			customInstructions: AUTO_COMPACT_INSTRUCTIONS,
			onComplete: () => {
				if (attemptGeneration !== generation) return;
				state.compacting = false;
				state.consecutiveFailures = 0;
				state.nextAttemptAt = 0;
				notify(ctx, "Automatic context compaction completed", "info");
			},
			onError: (error) => {
				if (attemptGeneration !== generation) return;
				state.compacting = false;
				state.armed = true;
				state.consecutiveFailures += 1;
				const delayMs = Math.min(
					RETRY_BASE_MS * 2 ** (state.consecutiveFailures - 1),
					RETRY_MAX_MS,
				);
				state.nextAttemptAt = Date.now() + delayMs;
				notify(
					ctx,
					`Automatic compaction failed: ${error.message}. Will retry after another settled run in ${Math.ceil(delayMs / 1000)}s.`,
					"error",
				);
			},
		};

		ctx.compact(options);
	};

	pi.on("session_start", () => {
		resetState();
	});

	pi.on("session_shutdown", () => {
		// Invalidate callbacks before Pi disposes the runtime and its contexts.
		resetState();
	});

	pi.on("model_select", () => {
		// A model switch changes the denominator. Invalidate any old-model callbacks
		// and re-evaluate from a clean policy state after the next run settles.
		resetState();
	});

	// Compact only after Pi has fully settled. Triggering from turn_end can abort a
	// tool-heavy agent loop midway through its work.
	pi.on("agent_settled", (_event, ctx) => {
		maybeCompact(ctx);
	});

	pi.registerCommand("auto-compact-status", {
		description: "Show context usage and the automatic compaction threshold",
		handler: (_args, ctx) => {
			const usage = ctx.getContextUsage();
			if (!usage || usage.percent === null || usage.tokens === null) {
				notify(
					ctx,
					`Context usage is unavailable until the next post-compaction response; threshold is ${AUTO_COMPACT_PERCENT}%`,
					"info",
				);
				return;
			}

			notify(
				ctx,
				`${formatUsage(usage.tokens, usage.contextWindow, usage.percent)}; automatic threshold ${AUTO_COMPACT_PERCENT}%`,
				"info",
			);
		},
	});
}
