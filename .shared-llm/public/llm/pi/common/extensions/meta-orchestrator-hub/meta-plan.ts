/**
 * meta-plan.ts — the `meta-plan:check` and `meta-plan:convert` Pi commands.
 *
 * These commands prepare the two files every synchronized meta runner needs before semi-AFK work can
 * start:
 *
 *   plan.md    — clean canonical work plan (# Plan, Goal, ordered phases, Done, optional Ideal)
 *   route.yaml — llm_profiles plus per-phase lead/stage llm_profile + agent routing
 *
 * The schema comes from the 2026-07-04 meta runner synchronization plan-v9. Do not invent a new shape
 * here. Runners should call the same deterministic check and fail loud before execution.
 */

import type {
	ExtensionAPI,
	ExtensionContext,
} from "@mariozechner/pi-coding-agent";
import * as fs from "node:fs";
import * as path from "node:path";
import { fileURLToPath } from "node:url";
import {
	convertLoosePlan,
	formatCheckResult,
	validateRunnable,
} from "./meta-plan-schema.ts";

function specPath(): string {
	return path.join(
		path.dirname(fileURLToPath(import.meta.url)),
		"meta-plan-format.md",
	);
}

function readSpec(): string {
	return fs.readFileSync(specPath(), "utf-8");
}

function resolvePath(ctx: ExtensionContext, p: string): string {
	return path.isAbsolute(p) ? p : path.resolve(ctx.cwd || process.cwd(), p);
}

function readText(
	label: string,
	filePath: string,
): { content?: string; error?: string } {
	try {
		return { content: fs.readFileSync(filePath, "utf-8") };
	} catch (err) {
		const code =
			err && typeof err === "object" && "code" in err
				? String((err as { code?: unknown }).code)
				: undefined;
		return {
			error:
				code === "ENOENT"
					? `${label} not found: ${filePath}`
					: `cannot read ${label}: ${err instanceof Error ? err.message : String(err)}`,
		};
	}
}

function routeOutputFor(planOutputPath: string): string {
	const parsed = path.parse(planOutputPath);
	return path.join(parsed.dir, "route.todo.yaml");
}

export default function registerMetaPlan(pi: ExtensionAPI): void {
	// ── meta-plan:check <plan> [route] — deterministic runnable-input report ───────────────────────
	pi.registerCommand("meta-plan:check", {
		description:
			"Check canonical meta plan inputs: meta-plan:check <plan.md> [route.yaml]",
		handler: async (args: string, ctx: ExtensionContext) => {
			const toks = (args || "").trim().split(/\s+/).filter(Boolean);
			if (toks.length < 1 || toks.length > 2) {
				ctx.ui.notify("Usage: meta-plan:check <plan.md> [route.yaml]", "error");
				return;
			}
			const planPath = resolvePath(ctx, toks[0]);
			const routePath = toks[1] ? resolvePath(ctx, toks[1]) : undefined;
			const planRead = readText("plan file", planPath);
			if (planRead.error) {
				ctx.ui.notify(`meta-plan: ${planRead.error}`, "error");
				return;
			}
			let routeContent: string | undefined;
			if (routePath) {
				const routeRead = readText("route file", routePath);
				if (routeRead.error) {
					ctx.ui.notify(`meta-plan: ${routeRead.error}`, "error");
					return;
				}
				routeContent = routeRead.content;
			}

			const result = validateRunnable(planRead.content!, routeContent);
			// validateRunnable ALSO enforces "a route is required to be runnable", which is irrelevant to
			// whether the PLAN ITSELF is well-formed when the caller only passed a plan. Reporting that
			// combined result under the `PLAN_CHECK:` prefix reads as "your plan is broken" when the plan
			// may be fine and only the route is absent — report the RUNNABLE_CHECK verdict under its own
			// prefix instead, plus an explicit note when it failed solely for lack of a route.
			const prefix = routePath ? "PLAN_CHECK" : "RUNNABLE_CHECK";
			const lines = [
				formatCheckResult(prefix, result),
				"",
				`plan: ${planPath}`,
				routePath
					? `route: ${routePath}`
					: "route: missing (plan-only check is not runnable — the failure above may be solely due to the missing route.yaml, not a defect in the plan itself)",
			];
			pi.sendMessage(
				{
					customType: "meta-plan-check",
					content: lines.join("\n"),
					display: true,
				},
				{ deliverAs: "followUp", triggerTurn: false },
			);
		},
	});

	// ── meta-plan:convert <source> <plan-output> [route-output] — deterministic starter conversion ──
	pi.registerCommand("meta-plan:convert", {
		description:
			"Convert a loose Markdown plan to plan.md plus route TODO stub: meta-plan:convert <source.md> <plan-output.md> [route-output.yaml]",
		handler: async (args: string, ctx: ExtensionContext) => {
			const toks = (args || "").trim().split(/\s+/).filter(Boolean);
			if (toks.length < 2 || toks.length > 3) {
				ctx.ui.notify(
					"Usage: meta-plan:convert <source.md> <plan-output.md> [route-output.yaml]",
					"error",
				);
				return;
			}
			const sourcePath = resolvePath(ctx, toks[0]);
			const planOutputPath = resolvePath(ctx, toks[1]);
			const routeOutputPath = resolvePath(
				ctx,
				toks[2] ?? routeOutputFor(planOutputPath),
			);
			if (/\.html?$/i.test(sourcePath)) {
				ctx.ui.notify(
					"meta-plan: HTML input is not supported; pass the paired Markdown plan file",
					"error",
				);
				return;
			}
			const sourceRead = readText("source plan", sourcePath);
			if (sourceRead.error) {
				ctx.ui.notify(`meta-plan: ${sourceRead.error}`, "error");
				return;
			}
			try {
				readSpec();
			} catch (err) {
				ctx.ui.notify(
					`meta-plan: cannot read the format spec: ${err instanceof Error ? err.message : String(err)}`,
					"error",
				);
				return;
			}

			const converted = convertLoosePlan(sourceRead.content!);
			try {
				fs.mkdirSync(path.dirname(planOutputPath), { recursive: true });
				fs.mkdirSync(path.dirname(routeOutputPath), { recursive: true });
				fs.writeFileSync(planOutputPath, converted.plan, "utf-8");
				fs.writeFileSync(routeOutputPath, converted.route, "utf-8");
			} catch (err) {
				ctx.ui.notify(
					`meta-plan: cannot write converted outputs: ${err instanceof Error ? err.message : String(err)}`,
					"error",
				);
				return;
			}

			const result = validateRunnable(converted.plan, converted.route);
			const lines = [
				"META_PLAN_CONVERT: wrote canonical starter files",
				`plan: ${planOutputPath}`,
				`route: ${routeOutputPath}`,
				"",
				"The route output intentionally contains TODO values unless the human fills real profiles/agents.",
				formatCheckResult("RUNNABLE_CHECK", result),
			];
			if (converted.notes.length)
				lines.push("", "Notes:", ...converted.notes.map((note) => `- ${note}`));
			pi.sendMessage(
				{
					customType: "meta-plan-convert",
					content: lines.join("\n"),
					display: true,
				},
				{ deliverAs: "followUp", triggerTurn: false },
			);
		},
	});
}
