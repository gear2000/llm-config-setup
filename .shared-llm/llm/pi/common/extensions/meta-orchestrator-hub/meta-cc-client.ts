#!/usr/bin/env -S node --experimental-strip-types
/**
 * meta-cc-client — a THIN command-line front door to the meta-orchestrator's
 * harness-neutral transport, so a Claude Code "brain" can drive a phase the same
 * way the resident Pi brain does, without the Pi runtime.
 *
 * It owns NO transport logic. Each subcommand parses its flags, calls ONE existing
 * function, and turns the result into stdout + an exit code:
 *
 *   hub-up   --json <path>                 → ensureHubStarted(path)   (hub.ts)
 *   hub-down --json <path>                 → stopHub(path)            (hub.ts)
 *   run-phase --plan … --phase … …         → hubRunPhase(input)       (hub-transport.ts)
 *   record-progress --session-dir … …      → appendProgress(entry)    (progress.ts)
 *
 * `run-phase` builds a RunPhaseInput EXACTLY as brain-core.ts does, calls
 * hubRunPhase (which blocks until the worker writes results.md with a leading
 * `PHASE_RESULT:` line), prints the worker's full report to stdout, and exits 0
 * only when the verdict parsed by parsePhaseResult (progress.ts) is `passed` —
 * every other verdict (partial | blocked | failed | none) exits 1.
 *
 * `record-progress` appends ONE phase outcome to the run's durable ledger EXACTLY as the
 * Pi brain's brain-core.recordPhaseProgress does — same judgePhaseStatus / planContentHash /
 * appendProgress library calls — so the Claude skill's progress.jsonl matches the Pi side
 * field-for-field instead of the skill hand-rolling a JSON line (which would risk drift on
 * summary escaping + the plan hash).
 *
 * Runtime: Node builtins only. hub.ts / hub-transport.ts / progress.ts / phase-id.ts
 * import the heavy pi-coding-agent + typebox symbols only as `import type` (erased by
 * type stripping), so this CLI needs no node_modules and runs under
 * `node --experimental-strip-types`.
 */

import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import { ensureHubStarted, stopHub, expandHome } from "./hub.ts";
import { hubRunPhase } from "./hub-transport.ts";
import { parsePhaseResult, judgePhaseStatus, planContentHash, appendProgress } from "./progress.ts";
import { makeRunPhaseId, makeWorkerSessionName } from "./phase-id.ts";
import type { PhaseProgressEntry } from "./progress.ts";
import type { RunPhaseInput } from "./types.ts";

const USAGE = `meta-cc-client — thin CLI over the meta-orchestrator transport

Usage:
  meta-cc-client hub-up   --json <path>
  meta-cc-client hub-down --json <path>
  meta-cc-client run-phase --plan <plan.md> --phase <N> \\
      --instructions-file <instructions.md> --results <results.md> \\
      --session <run-name> --worker-type claude|pi|cursor [--model <model>] \\
      --mode team|subagents --hub-json <path> [--attempt <N>] [--timeout <ms>]
  meta-cc-client record-progress --session-dir <dir> --phase <N> --attempt <N> \\
      [--results <results.md>] [--outcome-status completed|failed|stopped]

Notes:
  --session is the run NAME (a slug), NOT a tmux session — the worker tmux session and
  the phase id are DERIVED as <run-name>-p<phase>-<attempt>. --attempt (default 1) is the
  retry iteration; give each retry a distinct value so its worker session/log do not collide.
  --model is REQUIRED for --worker-type pi (the Pi model id) and --worker-type cursor (the
  Cursor model id, e.g. composer-2.5); it is ignored for claude, so it may be omitted there.

  record-progress appends ONE ledger entry to <session-dir>/progress.jsonl, deriving the TRUE
  status from the results.md PHASE_RESULT line (judgePhaseStatus), the summary from the report's
  first 1000 chars, and the planHash from <session-dir>/plan.md — identical to the Pi brain. Omit
  --results only when run-phase errored before writing one; then pass --outcome-status failed.

Exit codes:
  0   run-phase verdict was 'passed' (or hub-up/hub-down succeeded)
  1   run-phase verdict not 'passed' (partial|blocked|failed|none), or a hub op failed
  2   usage error (missing/invalid/unknown flags)`;

/** A bad-argument fault the CLI can handle: print usage + exit 2. Anything else propagates. */
class UsageError extends Error {}

/**
 * Parse `--key value` pairs into a map. Every flag takes a value (the CLI has no
 * boolean flags). Fail loud on a bare positional, an unknown flag, or a flag with
 * no value — never silently drop or default an argument.
 */
function parseValueFlags(argv: string[], known: ReadonlySet<string>): Map<string, string> {
	const out = new Map<string, string>();
	for (let i = 0; i < argv.length; i++) {
		const tok = argv[i];
		if (!tok.startsWith("--")) {
			throw new UsageError(`unexpected argument "${tok}" — expected --flag value pairs`);
		}
		const key = tok.slice(2);
		if (!known.has(key)) {
			throw new UsageError(`unknown flag --${key}`);
		}
		const val = argv[i + 1];
		if (val === undefined || val.startsWith("--")) {
			throw new UsageError(`flag --${key} requires a value`);
		}
		out.set(key, val);
		i++; // consumed the value
	}
	return out;
}

/** A required flag's value, or fail loud. */
function need(flags: Map<string, string>, key: string): string {
	const v = flags.get(key);
	if (v === undefined || v.length === 0) throw new UsageError(`missing required flag --${key}`);
	return v;
}

/** Read a file's full text; turn ENOENT into a clear fail-loud message, let other IO faults propagate. */
function readFileLoud(p: string, label: string): string {
	try {
		return fs.readFileSync(p, "utf-8");
	} catch (err) {
		const code = err && typeof err === "object" && "code" in err ? (err as NodeJS.ErrnoException).code : undefined;
		if (code === "ENOENT") throw new UsageError(`${label} not found: ${p}`);
		throw err;
	}
}

/** hub-up --json <path> → ensureHubStarted. Exit 0 when up, 1 when it could not start. */
async function cmdHubUp(argv: string[]): Promise<number> {
	const flags = parseValueFlags(argv, new Set(["json"]));
	const jsonPath = need(flags, "json");
	const r = await ensureHubStarted(jsonPath);
	if (!r.ok) {
		process.stderr.write(`hub-up: ${r.error ?? "could not start the hub"}\n`);
		return 1;
	}
	process.stdout.write(
		`hub ${r.alreadyUp ? "already up" : "started"} at ${r.url} (pid ${r.pid}, json ${expandHome(jsonPath)})\n`,
	);
	return 0;
}

/** hub-down --json <path> → stopHub. Exit 0 when stopped, 1 when there was nothing to stop / kill failed. */
async function cmdHubDown(argv: string[]): Promise<number> {
	const flags = parseValueFlags(argv, new Set(["json"]));
	const jsonPath = need(flags, "json");
	const r = stopHub(jsonPath);
	if (!r.ok) {
		process.stderr.write(`hub-down: ${r.error ?? "could not stop the hub"}\n`);
		return 1;
	}
	process.stdout.write(`stopped hub pid ${r.pid} (json ${expandHome(jsonPath)})\n`);
	return 0;
}

const KNOWN_RUN_PHASE_FLAGS = new Set([
	"plan",
	"phase",
	"instructions-file",
	"results",
	"session",
	"worker-type",
	"model",
	"mode",
	"hub-json",
	"attempt",
	"timeout",
]);

/**
 * run-phase — build a RunPhaseInput identical to brain-core.ts's, call hubRunPhase
 * (blocks until results.md carries a PHASE_RESULT line), print the report, and exit
 * on the parsed verdict (0 for passed, else 1).
 */
async function cmdRunPhase(argv: string[]): Promise<number> {
	const flags = parseValueFlags(argv, KNOWN_RUN_PHASE_FLAGS);

	const planFile = path.resolve(need(flags, "plan"));
	const phase = need(flags, "phase");
	const instructionsPath = path.resolve(need(flags, "instructions-file"));
	const resultsPath = path.resolve(need(flags, "results"));
	const sessionName = need(flags, "session");
	const hubJsonPath = expandHome(need(flags, "hub-json"));

	const workerTypeRaw = need(flags, "worker-type");
	if (workerTypeRaw !== "claude" && workerTypeRaw !== "pi" && workerTypeRaw !== "cursor") {
		throw new UsageError(`--worker-type must be "claude", "pi", or "cursor", got "${workerTypeRaw}"`);
	}
	const workerType: "claude" | "pi" | "cursor" = workerTypeRaw;

	// --model is REQUIRED for pi and cursor workers (it selects the model). For a claude worker the
	// model is ignored downstream (just worker-up launches a bare `claude`), so an empty model is fine
	// and --model may be omitted — no placeholder needed.
	const model = (workerType === "pi" || workerType === "cursor") ? need(flags, "model") : (flags.get("model") ?? "");

	const modeRaw = need(flags, "mode");
	if (modeRaw !== "team" && modeRaw !== "subagents") {
		throw new UsageError(`--mode must be "team" or "subagents", got "${modeRaw}"`);
	}
	const mode: "team" | "subagents" = modeRaw;

	// --attempt is optional (default 1): the retry iteration that disambiguates re-runs of the
	// SAME phase. It feeds BOTH the phaseId and the derived worker tmux session name, so each
	// retry gets a distinct session + log instead of colliding on the previous attempt's name.
	// (makeRunPhaseId / makeWorkerSessionName both require an integer >= 1.)
	let attempt = 1;
	const attemptRaw = flags.get("attempt");
	if (attemptRaw !== undefined) {
		const n = Number(attemptRaw);
		if (!Number.isInteger(n) || n < 1) {
			throw new UsageError(`--attempt must be a positive integer (>= 1), got "${attemptRaw}"`);
		}
		attempt = n;
	}

	// --timeout is optional; when present it must be a positive integer (ms).
	let resultsTimeoutMs: number | undefined;
	const timeoutRaw = flags.get("timeout");
	if (timeoutRaw !== undefined) {
		const n = Number(timeoutRaw);
		if (!Number.isInteger(n) || n <= 0) {
			throw new UsageError(`--timeout must be a positive integer (ms), got "${timeoutRaw}"`);
		}
		resultsTimeoutMs = n;
	}

	// Read the instructions text the worker will act on — the transport writes it to
	// instructionsPath, but RunPhaseInput also carries the text itself (instructions).
	const instructions = readFileLoud(instructionsPath, "--instructions-file");

	// Derive the session dir + logs dir EXACTLY as index.ts does: <META_ORCH_FILE_DIR
	// or os.tmpdir()/meta-orch>/<sessionName>/logs. The transport mkdir -p's logsDir.
	const fileBaseDir = process.env.META_ORCH_FILE_DIR || path.join(os.tmpdir(), "meta-orch");
	const sessionDir = path.join(fileBaseDir, sessionName);
	const logsDir = path.join(sessionDir, "logs");

	// Derive the two ids from (run-name slug, phase, attempt) EXACTLY as brain-core.ts does:
	//   phaseId       = makeRunPhaseId(sessionName, phase, attempt)        — names the transport
	//                   log file + log events,
	//   workerSession = makeWorkerSessionName(sessionName, phase, attempt) — the tmux session
	//                   `just worker-up` spawns; types.ts:39 requires "<run-name>-p<phase>-<n>",
	//                   NOT the bare slug, or two phases / two retries of one plan collide on it.
	const phaseId = makeRunPhaseId(sessionName, phase, attempt);
	const workerSession = makeWorkerSessionName(sessionName, phase, attempt);

	const input: RunPhaseInput = {
		phaseId,
		runPhaseCommand: {
			planFile,
			phase,
			instructions,
			instructionsPath,
			resultsPath,
			session: workerSession,
			workerType,
			model,
			mode,
		},
		dirs: { logsDir },
		hubJsonPath,
		resultsTimeoutMs,
	};

	// Blocks until the worker writes results.md with a PHASE_RESULT verdict (or fails loud).
	const outcome = await hubRunPhase(input);

	// Print the worker's full report to stdout.
	process.stdout.write(outcome.report.endsWith("\n") ? outcome.report : outcome.report + "\n");

	// Exit on the SEMANTIC verdict (progress.ts), not the process-level outcome.status.
	const verdict = parsePhaseResult(outcome.report);
	process.stderr.write(`run-phase: verdict=${verdict ?? "none"} (log ${outcome.logPath})\n`);
	return verdict === "passed" ? 0 : 1;
}

const KNOWN_RECORD_PROGRESS_FLAGS = new Set(["session-dir", "phase", "attempt", "results", "outcome-status"]);

/**
 * record-progress — append ONE phase outcome to the run's durable ledger, EXACTLY as the Pi brain's
 * brain-core.recordPhaseProgress does, so the Claude skill's ledger matches the Pi side field-for-field.
 * It REUSES the library — judgePhaseStatus (the TRUE semantic status from the worker's `PHASE_RESULT`
 * line, never the process exit), planContentHash (the changed-plan flag), appendProgress (the JSONL
 * writer with correct escaping) — instead of the skill echoing a hand-built JSON line.
 *
 *  - status  = judgePhaseStatus({ outcomeStatus, report }) — the verdict in results.md wins; with no
 *              verdict, the process status decides (completed/no-verdict → failed; never a faked pass).
 *  - summary = the report's first 1000 chars (brain-core.ts) — one-lined for display on resume.
 *  - planHash= planContentHash(<session-dir>/plan.md) — the SAME hash the Pi brain records, so a
 *              changed plan is flagged identically on the next resume.
 *  - phaseId = makeRunPhaseId(basename(session-dir), phase, attempt) — matches run-phase's id, so the
 *              ledger lines up with the transport's per-attempt logs.
 */
async function cmdRecordProgress(argv: string[]): Promise<number> {
	const flags = parseValueFlags(argv, KNOWN_RECORD_PROGRESS_FLAGS);
	const sessionDir = path.resolve(need(flags, "session-dir"));
	const phase = need(flags, "phase");

	const attemptRaw = need(flags, "attempt");
	const attempt = Number(attemptRaw);
	if (!Number.isInteger(attempt) || attempt < 1) {
		throw new UsageError(`--attempt must be a positive integer (>= 1), got "${attemptRaw}"`);
	}

	// The run slug is the session-dir basename (the skill's $SESSION = sanitized plan basename); feeding it
	// through makeRunPhaseId makes the ledger's phaseId identical to the one run-phase derived.
	const phaseId = makeRunPhaseId(path.basename(sessionDir), phase, attempt);

	// The worker's report = the results.md run-phase produced. Present → judgePhaseStatus reads its
	// PHASE_RESULT verdict. ABSENT (flag omitted) → report=undefined, mirroring brain-core's optional
	// report: the run-phase call errored before writing one, so the status falls to --outcome-status.
	// A --results path that is given but missing is a real fault → fail loud (do NOT treat it as "no report").
	const resultsRaw = flags.get("results");
	const report = resultsRaw !== undefined ? readFileLoud(path.resolve(resultsRaw), "--results") : undefined;

	// The worker PROCESS status (default "completed": the blocking run-phase call returned, so the worker
	// finished). judgePhaseStatus weighs the PHASE_RESULT verdict ABOVE this; it only decides the
	// no-verdict fallback (completed + no verdict → failed). Use --outcome-status failed when run-phase
	// errored with no results.md.
	const outcomeStatus = flags.get("outcome-status") ?? "completed";

	// planHash over the COPIED working plan — the skill placed it at <session-dir>/plan.md in Step 3. A
	// missing one is a broken run (Step 3 must have copied it), so fail loud rather than skip the hash.
	const planContent = readFileLoud(path.join(sessionDir, "plan.md"), "plan.md (under --session-dir)");

	// Build the entry EXACTLY as brain-core.recordPhaseProgress, plus the skill's `attempt` field, and
	// hand it to the library writer (one JSONL line, correctly escaped). appendProgress is best-effort
	// (it swallows write faults) — the same durability contract the Pi side relies on.
	const entry: PhaseProgressEntry & { attempt: number } = {
		phase,
		status: judgePhaseStatus({ outcomeStatus, report }),
		timestamp: new Date().toISOString(),
		phaseId,
		summary: report ? report.slice(0, 1000) : undefined,
		planHash: planContentHash(planContent),
		attempt,
	};
	appendProgress(sessionDir, entry);

	process.stdout.write(
		`recorded phase ${phase} (attempt ${attempt}): status=${entry.status} planHash=${entry.planHash} → ${path.join(sessionDir, "progress.jsonl")}\n`,
	);
	return 0;
}

async function main(): Promise<number> {
	const [sub, ...rest] = process.argv.slice(2);
	switch (sub) {
		case "hub-up":
			return cmdHubUp(rest);
		case "hub-down":
			return cmdHubDown(rest);
		case "run-phase":
			return cmdRunPhase(rest);
		case "record-progress":
			return cmdRecordProgress(rest);
		case "-h":
		case "--help":
		case "help":
			process.stdout.write(USAGE + "\n");
			return 0;
		case undefined:
			throw new UsageError("no subcommand — expected hub-up | hub-down | run-phase | record-progress");
		default:
			throw new UsageError(`unknown subcommand "${sub}" — expected hub-up | hub-down | run-phase | record-progress`);
	}
}

main()
	.then((code) => process.exit(code))
	.catch((err) => {
		// Top-level reporter. A UsageError is the one fault we handle here (friendly usage,
		// exit 2). Anything else is NOT swallowed: we print its full stack and exit 1 — loud,
		// not a fake default that limps onward.
		if (err instanceof UsageError) {
			process.stderr.write(`meta-cc-client: ${err.message}\n\n${USAGE}\n`);
			process.exit(2);
		}
		process.stderr.write(`meta-cc-client: ${err instanceof Error ? (err.stack ?? err.message) : String(err)}\n`);
		process.exit(1);
	});
