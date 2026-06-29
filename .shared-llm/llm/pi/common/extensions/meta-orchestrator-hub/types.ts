/**
 * types — the small, LOCAL type contract for the hub extension's transport.
 *
 * The hub extension is self-contained: it does NOT import the socket extension's claude-proxy.ts.
 * The hub model has no Unix socket and no mid-phase escalation (the worker runs to completion and
 * reports a verdict), so these types are trimmed to exactly what the hub transport needs — no
 * RelayUp, no AskEnvelope, no maxHops, no guardrail limits.
 */

/** Per-run working dirs — the hub transport only needs the logs dir (no sockets/configs here). */
export interface BrainDirs {
	logsDir: string;
}

/** Surfaced to the brain so it can paint live progress / notices in the TUI. Trimmed to what the
 *  hub transport emits (onLog for structured events; onNotify for an abort notice). */
export interface ProxyEvents {
	onProgress?: (phaseId: string, line: string) => void;
	onNotify?: (phaseId: string, message: string, level: "info" | "warning" | "error") => void;
	onLog?: (channel: string, data: Record<string, unknown>) => void;
}

/**
 * The brain's per-phase worker command for the FILE transport. The brain LLM picks the phase and
 * WRITES the instructions text; brain-core resolves all the file paths and the tmux session name from
 * the session dir. The transport writes instructions.md, spawns the worker, and watches results.md.
 */
export interface RunPhaseCommand {
	/** The plan.md the brain copied into the session dir (the worker reads it via /meta-autorun). */
	planFile: string;
	/** The brain's free-form phase token, e.g. "0" or "3" — names the phase's iteration dir. */
	phase: string;
	/** The instructions text the brain wrote for THIS attempt — the transport writes it to instructionsPath. */
	instructions: string;
	/** Where the transport writes the instructions text: phases/<phase>/iteration/<n>/instructions.md. */
	instructionsPath: string;
	/** The results.md the worker must write and the transport watches: phases/<phase>/iteration/<n>/results.md. */
	resultsPath: string;
	/** The tmux/worker session name, e.g. "<session_name>-p<phase>-<n>". */
	session: string;
	/** Which harness runs this phase: "claude" → a Claude TUI worker (team/subagents), "pi" → a
	 *  single-agent Pi worker on `model`, "cursor" → a Cursor Agent worker (--print mode) on `model`.
	 *  Set globally for the run by --worker-type. */
	workerType: "claude" | "pi" | "cursor";
	/** The model id for "pi" (e.g. "openai-codex/gpt-5.5") or "cursor" (e.g. "claude-sonnet-4-6");
	 *  ignored for "claude". */
	model: string;
	/** Claude only: "team" → a TeamCreate team, "subagents" → subagents. Pi and cursor are always single-agent. */
	mode: "team" | "subagents";
}

/**
 * A live handle to a running phase, handed to the brain the moment the worker is spawned (via
 * RunPhaseInput.onStart). The brain stores it so it can ABORT the phase out-of-band — on session
 * shutdown it calls abort() to tear the tmux worker down (`just worker-down`) instead of leaving it
 * running after the brain is gone. abort() resolves once teardown is complete.
 */
export interface PhaseHandle {
	phaseId: string;
	abort(): Promise<void>;
}

/** What hubRunPhase needs to run one phase attempt over the file transport. */
export interface RunPhaseInput {
	phaseId: string;
	/** The plan + phase + instructions + file paths + session — the file transport's whole input. */
	runPhaseCommand: RunPhaseCommand;
	dirs: BrainDirs;
	/** The discovery JSON path of the NAMED hub the human started (passed to the worker's /meta-connect). */
	hubJsonPath: string;
	/** How long to watch for results.md before failing loud (ms). Defaults in the transport. */
	resultsTimeoutMs?: number;
	events?: ProxyEvents;
	/** Called once, right after the worker is spawned, with a handle the brain uses to abort the
	 *  phase on shutdown. */
	onStart?: (handle: PhaseHandle) => void;
}

/**
 * The phase outcome the transport resolves with. `status` is the worker PROCESS-level result, NOT
 * the semantic verdict — the `PHASE_RESULT: passed|partial|blocked|failed` verdict lives in `report`
 * and judgePhaseStatus (progress.ts) reads it. The hub model has no guardrail breach, so there is no
 * `breach` field here.
 */
export interface PhaseOutcome {
	phaseId: string;
	status: "completed" | "failed" | "stopped";
	/** The worker's final reply text (its report back to the brain — carries the PHASE_RESULT line). */
	report: string;
	/** True only when the brain explicitly aborted the phase. */
	stoppedByLeader: boolean;
	logPath: string;
}
