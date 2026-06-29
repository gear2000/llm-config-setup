/**
 * meta-plan.ts — the `meta-plan:check` and `meta-plan:convert` Pi commands.
 *
 * These shape an incoming plan to the format the meta-orchestrator brain expects (see the canonical
 * spec, meta-plan-format.md, which ships next to this module — the single source of truth for both
 * commands and for the Claude-side `/meta-plan:*` skills).
 *
 *   meta-plan:check <plan>            — read-only: report PASS or the specific violations.
 *   meta-plan:convert <plan> <output> — rewrite <plan> into the format and WRITE it to <output>.
 *
 * Both are pure LLM text tasks: the handler reads the plan + the spec, then hands the model a prompt
 * and triggers a turn — the model does the reasoning/writing with its own read/write tools. Kept
 * SEPARATE from the brain (index.ts) so they can be used standalone, before any run. Registered
 * alongside the brain via registerMetaPlan(pi) from index.ts's default export.
 */

import type { ExtensionAPI, ExtensionContext } from "@mariozechner/pi-coding-agent";
import * as fs from "node:fs";
import * as path from "node:path";
import { fileURLToPath } from "node:url";

/** The canonical spec doc that ships next to this module. */
function specPath(): string {
	return path.join(path.dirname(fileURLToPath(import.meta.url)), "meta-plan-format.md");
}

/** Read the spec, or fail loud (the commands have no meaning without their format definition). */
function readSpec(): string {
	return fs.readFileSync(specPath(), "utf-8"); // throws if missing — fail loud, never check/convert against a guessed format
}

/** Resolve a possibly-relative path against the session cwd. */
function resolvePath(ctx: ExtensionContext, p: string): string {
	return path.isAbsolute(p) ? p : path.resolve(ctx.cwd || process.cwd(), p);
}

/** Read a plan file, returning its content or an error string (never throws). */
function readPlan(planPath: string): { content?: string; error?: string } {
	try {
		return { content: fs.readFileSync(planPath, "utf-8") };
	} catch (err) {
		const code = err && typeof err === "object" && "code" in err ? (err as NodeJS.ErrnoException).code : undefined;
		return { error: code === "ENOENT" ? `plan file not found: ${planPath}` : `cannot read plan: ${err instanceof Error ? err.message : String(err)}` };
	}
}

export default function registerMetaPlan(pi: ExtensionAPI): void {
	// ── meta-plan:check <plan> — read-only conformance report ───────────────────────────────────────
	pi.registerCommand("meta-plan:check", {
		description: "Check a plan against the meta-plan format (read-only): meta-plan:check <plan>",
		handler: async (args, ctx) => {
			const planArg = (args || "").trim();
			if (!planArg) { ctx.ui.notify("Usage: meta-plan:check <plan>", "error"); return; }
			const planPath = resolvePath(ctx, planArg);
			const { content, error } = readPlan(planPath);
			if (error) { ctx.ui.notify(`meta-plan: ${error}`, "error"); return; }
			let spec: string;
			try { spec = readSpec(); } catch (err) { ctx.ui.notify(`meta-plan: cannot read the format spec: ${err instanceof Error ? err.message : String(err)}`, "error"); return; }

			const prompt = [
				"You are CHECKING a plan against the meta-orchestrator plan format. This is READ-ONLY — do NOT modify any file.",
				"",
				"===== THE FORMAT SPEC (the authority) =====",
				spec,
				"===== END SPEC =====",
				"",
				`===== THE PLAN UNDER CHECK (${planPath}) =====`,
				content!,
				"===== END PLAN =====",
				"",
				"Report conformance. Start your reply with exactly `PLAN_CHECK: PASS` or `PLAN_CHECK: FAIL`, then a short bullet list of any issues, worst first:",
				"- missing `# Plan:` title or `Goal:` line;",
				"- phases not `## Phase <N> — <title>`, not numbered from 0, or out of order;",
				"- any phase with NO `Done:` line (this is the one hard requirement — flag a MISSING Done:, but do NOT flag a Done: merely for being modest; the bar is allowed to be 'sufficient', not perfect);",
				"- leftover per-phase worker/agent/team directives (e.g. `Agents:`/`team:`) — obsolete, the worker is global;",
				"- anything that ENCODES A SHORTCUT or compromises the non-negotiables (dropping a feature to pass, faking a pass, untested/dead code, swallowing failures).",
				"Be lenient on bar height; be strict on a missing Done:, on shortcuts, and on the non-negotiables. Do not rewrite the plan — only report.",
			].join("\n");

			pi.sendMessage({ customType: "meta-plan-check", content: prompt, display: true }, { deliverAs: "followUp", triggerTurn: true });
		},
	});

	// ── meta-plan:convert <plan> <output> — rewrite into the format, write to <output> ───────────────
	pi.registerCommand("meta-plan:convert", {
		description: "Convert a loose plan into the meta-plan format and write it: meta-plan:convert <plan> <output>",
		handler: async (args, ctx) => {
			const toks = (args || "").trim().split(/\s+/).filter(Boolean);
			if (toks.length < 2) { ctx.ui.notify("Usage: meta-plan:convert <plan> <output>", "error"); return; }
			const planPath = resolvePath(ctx, toks[0]);
			const outputPath = resolvePath(ctx, toks[1]);
			const { content, error } = readPlan(planPath);
			if (error) { ctx.ui.notify(`meta-plan: ${error}`, "error"); return; }
			let spec: string;
			try { spec = readSpec(); } catch (err) { ctx.ui.notify(`meta-plan: cannot read the format spec: ${err instanceof Error ? err.message : String(err)}`, "error"); return; }

			const prompt = [
				"You are CONVERTING a plan into the meta-orchestrator plan format, then WRITING the result.",
				"",
				"===== THE FORMAT SPEC (the authority — follow it exactly) =====",
				spec,
				"===== END SPEC =====",
				"",
				`===== THE SOURCE PLAN (${planPath}) =====`,
				content!,
				"===== END SOURCE PLAN =====",
				"",
				`Rewrite the source plan into the format above and WRITE it to: ${outputPath} (use your write tool).`,
				"Rules for the conversion:",
				"- PRESERVE the author's intent and scope — do NOT drop, weaken, or simplify away any work to make the plan tidy. If the source asks for something hard, the converted plan still asks for it.",
				"- Number phases from 0, in order. Give EACH phase a sufficient `Done:`. If a phase has no checkable condition and you cannot HONESTLY infer one from its text, write `Done: TODO — needs a checkable condition` — never fabricate a check.",
				"- Lift any stretch/perfection goal into an optional `Ideal:`.",
				"- Strip obsolete per-phase worker/agent/team directives (the worker is global).",
				"- Embed the non-negotiables section from the spec into the converted plan, verbatim, so the plan is self-contained.",
				"- Hold YOURSELF to those non-negotiables while converting: do not compromise the plan, and do not invent or omit anything to make conversion easier.",
				"After writing, confirm the output path and give a short summary of what you changed (renumbered phases, added/flagged Done: checks, stripped lines, embedded non-negotiables).",
			].join("\n");

			pi.sendMessage({ customType: "meta-plan-convert", content: prompt, display: true }, { deliverAs: "followUp", triggerTurn: true });
		},
	});
}
