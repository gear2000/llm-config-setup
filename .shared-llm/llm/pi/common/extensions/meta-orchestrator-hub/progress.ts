/**
 * progress — the durable per-phase outcome ledger that makes a run RESUMABLE.
 *
 * The problem this solves: every `meta-server:autorun <plan>` startup was a blank slate. If a run
 * failed at phase 4A and the human restarted with the SAME plan, the brain redid the phases from
 * the top. This module persists each phase's terminal outcome to a file in the plan's run dir, and
 * loads the prior outcomes on startup so the brain can RESUME from the first not-yet-passed phase.
 *
 * The status recorded is the phase's TRUE semantic outcome (judgePhaseStatus), NOT the worker
 * PROCESS exit. A worker that finishes but reports PARTIAL/BLOCKED (or whose adversarial-evaluator
 * VEERED) is recorded partial/blocked and RE-RUN on resume — never silently skipped as `passed`.
 * Only a genuine `passed` (work done AND the evaluator CLEARED) lets resume skip a phase.
 *
 * Why a dedicated ledger and not the existing run-log:
 *  - The Pi leader logs via `pi.appendEntry` (Pi's own entry store), NOT a file in the run dir — so
 *    there was no cross-leader, file-based progress to reuse.
 *  - The SDK `run-log.jsonl` does append `phase_done` events, but it does NOT carry the worker's
 *    handoff summary (that goes to the brain via the message queue, not the log), and it interleaves
 *    many event kinds. Parsing it for resume would be fragile and summary-less.
 * So this is a purpose-built, append-only ledger with clean `{phase, status, timestamp, summary}`
 * records — written by the SHARED brain-core (brain-core.ts finishPhase), consumed by BOTH leaders.
 *
 * Keyed by plan-slug (the run dir already is `~/.pi/meta-orch/<slug>/`), so two different plans
 * never share progress. We also record a cheap plan-content hash per entry so a reload can NOTE
 * (not block) when the plan text changed since the last run — the brain is told the plan may have
 * shifted, and the human still has the `--fresh` escape hatch.
 *
 * Framework-free: imports only Node builtins, so it loads under `node --experimental-strip-types`
 * and inside Pi's jiti loader the same way the rest of the worker half does. The timestamps here use
 * a real wall clock (`Date.now()` / `new Date()`) — this is normal Node extension code reporting when
 * a phase finished, NOT a deterministic workflow script.
 */

import * as fs from "node:fs";
import * as path from "node:path";
import * as crypto from "node:crypto";

/** The ledger filename inside a plan's run dir (`~/.pi/meta-orch/<slug>/progress.jsonl`). */
export const PROGRESS_FILENAME = "progress.jsonl";

/** A phase's terminal status as recorded in the ledger. `passed` is the ONLY one that lets the brain
 *  SKIP a phase on resume; every other value — `partial` / `failed` / `blocked` / `errored` and any
 *  `breached_*` — means "NOT passed, re-run this". A phase still in progress when the process died
 *  writes NOTHING, so it is absent → also re-run.
 *
 *  The non-`passed` set is deliberately RICH (not collapsed to one "fail"), because the bug this
 *  fixes was recording a process-exit as `passed` when the worker itself reported PARTIAL/BLOCKED.
 *  We carry the TRUE semantic outcome the worker reported (its `PHASE_RESULT` verdict) so a resume
 *  re-runs exactly what is not done:
 *   - `passed`    — work fully done AND the adversarial-evaluator CLEARED it (the worker said so).
 *   - `partial`   — real work landed but the phase goal is not fully met.
 *   - `blocked`   — could not proceed (evaluator VEERED uncleared, or a hard blocker).
 *   - `failed`    — errored, OR the worker exited with no parseable verdict (we never assume pass).
 *   - `errored`   — no outcome at all (the phase errored before producing one).
 *  A guardrail breach uses a distinct `breached_<kind>` literal so the ledger shows the worker DIED
 *  before it could emit a verdict. (`breached_turn_cap` is a forward-compat placeholder — there is no
 *  turn cap today, only the wall-clock timeout, so the live breach kind is `breached_timeout`; the
 *  turn-cap literal is kept so a re-introduced turn cap needs no type edit.) These are string literals
 *  rather than enum members so a new breach kind never needs a type edit here. */
export type PhaseProgressStatus =
	| "passed"
	| "partial"
	| "failed"
	| "blocked"
	| "errored"
	| "breached_turn_cap"
	| `breached_${string}`;

/** The one status the brain may SKIP on resume. Everything else is NOT-PASSED and gets re-run.
 *  Centralised so the ledger, the resume block, and any future caller agree on the single rule. */
export function isPassed(status: PhaseProgressStatus): boolean {
	return status === "passed";
}

/** The worker's machine-readable end-of-run verdict, parsed from the `PHASE_RESULT:` line it prints
 *  as the LAST thing it does. `passed` ONLY when the phase's work is fully done AND the adversarial-
 *  evaluator CLEARED it; the worker must never claim `passed` over a VEERED gate or incomplete work. */
export type WorkerVerdict = "passed" | "partial" | "blocked" | "failed";

/**
 * Pull the worker's `PHASE_RESULT: passed|partial|blocked|failed` verdict out of its final report.
 * The worker writes this as the FIRST line of results.md (the /meta-run contract), so we scan ALL matches and
 * take the LAST one — a transcript may quote the literal token earlier (e.g. these very instructions)
 * and the real verdict is the one the worker emits at the end. Case-insensitive on both the key and
 * the value; tolerates surrounding markdown/backticks/whitespace. Returns null when no verdict line is
 * present (an old worker, or one that died before emitting one) so the judge can fall back loudly.
 */
export function parsePhaseResult(report: string | null | undefined): WorkerVerdict | null {
	if (!report) return null;
	// Match `PHASE_RESULT: <verdict>` anywhere on a line, ignoring leading markdown (#, *, -, >, `).
	// The verdict is captured as a word; we validate it against the known set below.
	const re = /PHASE_RESULT\s*:\s*`?\s*(passed|partial|blocked|failed)\b/gi;
	let last: WorkerVerdict | null = null;
	for (const m of report.matchAll(re)) {
		const v = m[1]?.toLowerCase();
		if (v === "passed" || v === "partial" || v === "blocked" || v === "failed") last = v;
	}
	return last;
}

/**
 * Judge the TRUE terminal status of a phase from THREE signals, in this precedence:
 *  1. a GUARDRAIL BREACH wins — the worker was killed before it could emit a verdict, so whatever it
 *     printed cannot be trusted. `turn_cap` → the distinct `breached_turn_cap`; any other kind →
 *     `breached_<kind>`. (All are NOT-PASSED → re-run.)
 *  2. otherwise the WORKER'S `PHASE_RESULT` verdict is authoritative — it judged its own phase
 *     (work done? evaluator CLEARED?). `passed`→`passed`, `partial`→`partial`, `blocked`→`blocked`,
 *     `failed`→`failed`. A worker that finishes but reports PARTIAL/BLOCKED is recorded as such and
 *     re-runs on resume — the core fix.
 *  3. NO parseable verdict and NO breach → we must NOT assume the phase passed. A leader STOP
 *     (proxy `stopped`) → `blocked`; an explicit `errored`/null outcome → `errored`; anything else
 *     (including a clean `completed` exit with no verdict line) → `failed`. We default to NOT-passed
 *     on purpose: a silent skip of an unfinished phase is exactly the bug being fixed.
 *
 * `outcomeStatus` is the proxy's PhaseOutcome.status ("completed" | "failed" | "stopped" | "breached")
 * or null when the phase errored before producing an outcome. `breachKind` is PhaseOutcome.breach?.kind.
 */
export function judgePhaseStatus(args: {
	outcomeStatus: string | null | undefined;
	breachKind?: string | null;
	report?: string | null;
}): PhaseProgressStatus {
	const { outcomeStatus, breachKind, report } = args;

	// 1. A breach killed the worker mid-flight — it never got to emit a trustworthy verdict.
	if (breachKind) {
		return breachKind === "turn_cap" ? "breached_turn_cap" : (`breached_${breachKind}` as PhaseProgressStatus);
	}

	// 2. The worker's own verdict is the truth when present (and we are not in a breach).
	const verdict = parsePhaseResult(report);
	if (verdict) return verdict; // passed | partial | blocked | failed, verbatim

	// 3. No verdict, no breach → never assume a pass.
	if (outcomeStatus === "stopped") return "blocked"; // a leader STOP ended the phase
	if (!outcomeStatus || outcomeStatus === "errored") return "errored"; // no outcome produced at all
	// A clean process exit ("completed") OR a non-breach "failed" with no verdict line both land here:
	// the worker did not certify a pass, so we record `failed` (re-run), NOT `passed`.
	return "failed";
}

/** One durable record, appended the moment a phase reaches a terminal outcome. */
export interface PhaseProgressEntry {
	/** The brain's free-form phase token, e.g. "0" or "4A" — what resume keys on. */
	phase: string;
	status: PhaseProgressStatus;
	/** Wall-clock ISO timestamp of when the phase finished. */
	timestamp: string;
	/** The backtrack-safe phase id (orch-<slug>-p<phase>-<seq>) — disambiguates repeated runs. */
	phaseId?: string;
	/** A short handoff/summary the brain reads on resume (the worker's report, trimmed). */
	summary?: string;
	/** Cheap hash of the plan content at write time → reload can flag a changed plan. */
	planHash?: string;
}

/** Absolute path to a run's ledger, given the run dir (the dir that already holds logs/ sockets/). */
export function progressPathFor(runDir: string): string {
	return path.join(runDir, PROGRESS_FILENAME);
}

/** A stable short hash of the plan content — used only to NOTE a changed plan on reload, never to
 *  block. 12 hex chars of sha256 is plenty to spot a content change by eye. */
export function planContentHash(planContent: string): string {
	return crypto.createHash("sha256").update(planContent, "utf-8").digest("hex").slice(0, 12);
}

/**
 * LEGACY process-status-only mapping, kept only for the narrow case where there is NO worker verdict
 * and NO breach to consider. The PROCESS exit is NOT the semantic truth — that is exactly the bug
 * judgePhaseStatus fixes — so the live persist point (recordPhaseProgress) now calls judgePhaseStatus,
 * which weighs the worker's `PHASE_RESULT` verdict + the breach kind ABOVE this. This helper survives
 * for callers that only have the process status (e.g. a torn run with no report) and for the existing
 * unit cases that assert the raw process→ledger mapping.
 *
 * Mapping: "completed"→"passed" (process exited 0 — a coarse signal only), "failed"→"failed",
 * "stopped"/"breached"→"blocked", null/absent/"errored"→"errored". Prefer judgePhaseStatus.
 */
export function statusFromOutcome(outcomeStatus: string | null | undefined): PhaseProgressStatus {
	if (outcomeStatus === "completed") return "passed";
	if (!outcomeStatus || outcomeStatus === "errored") return "errored";
	if (outcomeStatus === "failed") return "failed";
	return "blocked";
}

/**
 * Append one terminal phase outcome to the ledger (creating the dir + file if needed). Best-effort:
 * a logging failure must never break the run, so we swallow write errors exactly like the run-log.
 * The record is one JSON object per line (JSONL) so a crash mid-write loses at most the last line.
 */
export function appendProgress(runDir: string, entry: PhaseProgressEntry): void {
	try {
		fs.mkdirSync(runDir, { recursive: true });
		fs.appendFileSync(progressPathFor(runDir), JSON.stringify(entry) + "\n");
	} catch {
		// best-effort durable ledger; never let persistence break the run.
	}
}

/**
 * Read the raw ledger entries for a run, oldest-first, skipping any unparseable line (a crash may
 * leave a torn final line). Returns [] when the ledger is absent — a brand-new run. This is the raw
 * append history: a phase can appear MORE THAN ONCE (a retry, or a backtrack that re-ran it).
 */
export function readProgress(runDir: string): PhaseProgressEntry[] {
	let text: string;
	try {
		text = fs.readFileSync(progressPathFor(runDir), "utf-8");
	} catch (err) {
		const code = err && typeof err === "object" && "code" in err ? (err as NodeJS.ErrnoException).code : undefined;
		if (code === "ENOENT") return []; // no prior progress — fresh run
		throw err; // a real read fault (perms, etc.) should surface, not be hidden
	}
	const entries: PhaseProgressEntry[] = [];
	for (const line of text.split("\n")) {
		const trimmed = line.trim();
		if (!trimmed) continue;
		try {
			const parsed = JSON.parse(trimmed) as PhaseProgressEntry;
			if (parsed && typeof parsed.phase === "string" && typeof parsed.status === "string") entries.push(parsed);
		} catch {
			// torn / malformed line (e.g. a crash mid-append) — skip it, keep the rest.
		}
	}
	return entries;
}

/** The LATEST status per phase, collapsing the append history. A phase that was partial then later
 *  passed on a re-run reads as "passed"; one that passed then was re-run and came back partial reads
 *  as "partial". Last-write-wins is exactly the resume semantics we want: the most recent attempt is
 *  the truth, and only a latest-"passed" lets resume skip it. */
export function latestStatusByPhase(entries: PhaseProgressEntry[]): Map<string, PhaseProgressEntry> {
	const latest = new Map<string, PhaseProgressEntry>();
	for (const e of entries) latest.set(e.phase, e); // later entries overwrite earlier ones
	return latest;
}

/**
 * Build the human-and-brain-readable PRIOR PROGRESS block injected into the brain's first turn.
 * Returns null when there is NO usable prior progress (so the caller starts fresh and says nothing).
 *
 * The block lists, by latest status:
 *  - which phases PASSED (with their handoff summary) → the brain must NOT re-run them,
 *  - which phases FAILED / blocked / errored / are incomplete → resume from the first of these.
 * It also notes if the plan content changed since the recorded run (planHash mismatch), so the brain
 * knows the plan may have shifted — informational, never blocking (the human owns `--fresh`).
 */
export function buildPriorProgressBlock(entries: PhaseProgressEntry[], currentPlanHash?: string): string | null {
	if (entries.length === 0) return null;
	const latest = latestStatusByPhase(entries);
	if (latest.size === 0) return null;

	const passed: PhaseProgressEntry[] = [];
	const notPassed: PhaseProgressEntry[] = [];
	for (const e of latest.values()) {
		// ONLY a truly-passed phase is skipped on resume. partial / blocked / failed / errored /
		// breached_* are ALL not-passed and get re-run — the fix's core invariant.
		if (isPassed(e.status)) passed.push(e);
		else notPassed.push(e);
	}
	// Stable, readable order: by phase token (natural-ish — numeric phases sort by value, the rest
	// lexically). The brain re-reads the plan for the authoritative order; this is just a digest.
	const byPhase = (a: PhaseProgressEntry, b: PhaseProgressEntry) => comparePhaseTokens(a.phase, b.phase);
	passed.sort(byPhase);
	notPassed.sort(byPhase);

	const lines: string[] = [];
	lines.push("----- PRIOR PROGRESS (this plan was partly run before) -----");
	lines.push(
		"This plan-slug has a saved progress ledger from an earlier run. Do NOT re-run a phase already " +
			"marked PASSED below; resume from the first not-yet-passed phase. A phase recorded partial / " +
			"blocked / failed / breached_* is NOT passed — its work is unfinished, so RE-RUN it (these are " +
			"under NOT PASSED below). You MAY still backtrack to an earlier phase if a failure's real cause " +
			"is upstream. Announce your resume decision before you call run_phase.",
	);
	lines.push("");
	if (passed.length > 0) {
		lines.push("PASSED (skip these):");
		for (const e of passed) {
			const summary = e.summary ? ` — ${oneLine(e.summary, 240)}` : "";
			lines.push(`  - phase ${e.phase}: passed${summary}`);
		}
	} else {
		lines.push("PASSED (skip these): none.");
	}
	lines.push("");
	if (notPassed.length > 0) {
		lines.push("NOT PASSED (re-run these — resume from the first):");
		for (const e of notPassed) {
			const summary = e.summary ? ` — ${oneLine(e.summary, 240)}` : "";
			// e.status is the TRUE semantic outcome (partial / blocked / failed / errored / breached_*),
			// NOT a process-exit. Surface it verbatim so the brain re-runs exactly what is not done.
			lines.push(`  - phase ${e.phase}: ${e.status}${summary}`);
		}
	} else {
		lines.push("NOT PASSED: none recorded — every recorded phase passed; continue with the next unrun phase in the plan.");
	}

	// Cheap "did the plan change?" note: compare the current plan hash to the most recent recorded
	// one. A mismatch does NOT block — it just warns the brain the plan text may have shifted.
	if (currentPlanHash) {
		const lastHash = mostRecentPlanHash(entries);
		if (lastHash && lastHash !== currentPlanHash) {
			lines.push("");
			lines.push(
				`NOTE: the plan file CHANGED since this progress was recorded (was ${lastHash}, now ${currentPlanHash}). ` +
					"The saved phase outcomes may no longer line up with the current phases — re-read the plan carefully, " +
					"and if the structure shifted, prefer re-running affected phases (or ask the human to restart with --fresh).",
			);
		}
	}
	lines.push("----- END PRIOR PROGRESS -----");
	return lines.join("\n");
}

/** The planHash of the most recent entry that carried one (the latest run's plan content). */
function mostRecentPlanHash(entries: PhaseProgressEntry[]): string | undefined {
	for (let i = entries.length - 1; i >= 0; i--) {
		if (entries[i]?.planHash) return entries[i]!.planHash;
	}
	return undefined;
}

/**
 * Archive the existing ledger out of the way (for `--fresh`): rename progress.jsonl →
 * progress.<timestamp>.jsonl so the prior run's history is kept but no longer loaded. No-op when
 * there is no ledger. Best-effort — a failure to archive must not block the fresh start.
 */
export function archiveProgress(runDir: string): void {
	const live = progressPathFor(runDir);
	try {
		if (!fs.existsSync(live)) return;
		const stamp = new Date().toISOString().replace(/[:.]/g, "-");
		fs.renameSync(live, path.join(runDir, `progress.${stamp}.jsonl`));
	} catch {
		// best-effort archive; a fresh run can proceed even if the old ledger can't be moved.
	}
}

/** Delete every file directly inside `dir` (non-recursive — these dirs hold only flat per-phase
 *  files). No-op when the dir does not exist. Best-effort: a prune failure must never block the run. */
function pruneDirFiles(dir: string): number {
	let removed = 0;
	let names: string[];
	try {
		names = fs.readdirSync(dir);
	} catch {
		return 0; // dir absent or unreadable — nothing to prune
	}
	for (const name of names) {
		try {
			const full = path.join(dir, name);
			if (fs.statSync(full).isFile()) {
				fs.unlinkSync(full);
				removed++;
			}
		} catch {
			// best-effort: skip a file we can't stat/remove, keep pruning the rest.
		}
	}
	return removed;
}

/**
 * Prune a run's accumulated per-phase artifacts for a `--fresh` start: the transcript logs
 * (`logs/`), the generated --mcp-config files (`configs/`), and any leftover back-channel sockets
 * (`sockets/`). Across a long session these grow unbounded — one set per phase run (every backtrack
 * adds more) — and `--fresh` already means "ignore the prior run", so its artifacts are dead weight.
 *
 * Safe on a fresh start specifically: the brain has not launched any phase yet, so every file in
 * these three dirs is a leftover from an earlier run — including any `*.sock`, which can only be a
 * stale leftover here (a live phase would have its own fresh socket). We do NOT touch the durable
 * ledger (progress.jsonl / its archives) — that lives in runDir itself and is handled by
 * archiveProgress; this only clears the three sub-dirs. Best-effort throughout: a prune failure is
 * swallowed (like archiveProgress) so it can never block the run. Returns the per-dir removal counts
 * for the caller to log.
 */
export function pruneRunArtifacts(dirs: { logsDir: string; socketsDir: string; configsDir: string }): {
	logs: number;
	sockets: number;
	configs: number;
} {
	return {
		logs: pruneDirFiles(dirs.logsDir),
		sockets: pruneDirFiles(dirs.socketsDir),
		configs: pruneDirFiles(dirs.configsDir),
	};
}

/** One-line a possibly-multiline summary, collapsing whitespace and truncating with an ellipsis. */
function oneLine(text: string, max: number): string {
	const collapsed = text.replace(/\s+/g, " ").trim();
	return collapsed.length > max ? collapsed.slice(0, max - 1) + "…" : collapsed;
}

/** Compare two phase tokens so numeric phases ("0","1","2","10") sort by value and mixed tokens
 *  ("4A","4B") sort sensibly. Numeric-leading tokens order by their number first, then by the rest. */
function comparePhaseTokens(a: string, b: string): number {
	const ma = /^(\d+)(.*)$/.exec(a);
	const mb = /^(\d+)(.*)$/.exec(b);
	if (ma && mb) {
		const na = Number(ma[1]);
		const nb = Number(mb[1]);
		if (na !== nb) return na - nb;
		return (ma[2] || "").localeCompare(mb[2] || "");
	}
	if (ma) return -1; // numeric-leading sorts before non-numeric
	if (mb) return 1;
	return a.localeCompare(b);
}
