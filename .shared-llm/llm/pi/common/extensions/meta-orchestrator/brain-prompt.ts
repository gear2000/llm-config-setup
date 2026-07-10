/**
 * brain-prompt — the shared, framework-free helpers that turn the on-disk brain-prompt file
 * (brain-execute-plan-prompt.md) into a ready system prompt, and read the agent roster the brain
 * may choose from.
 *
 * Both brains use these IDENTICALLY:
 *  - the SDK brain (sdk-leader.ts) fills them into its `query()` systemPrompt, and
 *  - the Pi brain (index.ts) fills them into the operating-instructions message it hands the
 *    resident Pi session.
 *
 * They live here (not in sdk-leader.ts) so the Pi extension can reuse them WITHOUT pulling in the
 * SDK brain's heavy deps (@anthropic-ai/claude-agent-sdk, zod). This module imports only Node
 * builtins (fs / path / url), so it loads under `node --experimental-strip-types` and inside Pi's
 * jiti loader the same way.
 */

import * as fs from "node:fs";
import * as path from "node:path";
import { fileURLToPath } from "node:url";

/** The brain-prompt file that ships next to this module — the single source of truth (both brains
 *  load the same text). Placeholders <PLAN_FILE> / <AVAILABLE_AGENTS> are filled at launch. */
export function brainPromptPath(): string {
	return path.join(path.dirname(fileURLToPath(import.meta.url)), "brain-execute-plan-prompt.md");
}

/** Default location of the agent roster the brain may pick from. The brain reads the NAMES (the
 *  `.claude/agents/*.md` filenames, minus the `_archived-` ones — those are retired). The path
 *  walks up from this module to the repo root's `.claude/agents`. */
export function defaultAgentsDir(): string {
	return process.env.META_ORCH_AGENTS_DIR || path.resolve(fileURLToPath(import.meta.url), "../../../../../../../.claude/agents");
}

/** A short slug for the run, derived from the plan filename — namespaces this run's sockets / logs /
 *  configs. (A markdown plan has only its path; the slug is its basename without extension.) */
export function planSlug(planPath: string): string {
	return path.basename(planPath).replace(/\.[^.]+$/, "");
}

/** Read the brain-prompt file and substitute BOTH placeholders. <PLAN_FILE> appears twice (the
 *  "Your inputs" reference AND the step-4 launch template) — both must become the real path, so we
 *  replace globally. Fail loud if the prompt file is missing: the brain has no identity without it. */
export function buildSystemPrompt(planPath: string, availableAgents: string[], promptPath = brainPromptPath()): string {
	let template: string;
	try {
		template = fs.readFileSync(promptPath, "utf-8");
	} catch (err) {
		const code = err && typeof err === "object" && "code" in err ? (err as NodeJS.ErrnoException).code : undefined;
		if (code === "ENOENT") throw new Error(`meta-orch: brain-prompt file not found: ${promptPath}`);
		throw err;
	}
	const agents = availableAgents.length ? availableAgents.join(", ") : "(none found)";
	return template.split("<PLAN_FILE>").join(planPath).split("<AVAILABLE_AGENTS>").join(agents);
}

/** List the agent names the brain may choose from: every `<name>.md` in the agents dir, minus the
 *  `_archived-` retired ones. Names only — the brain passes them verbatim to /run-phase. Fail loud
 *  if the dir is unreadable: a brain with an empty roster cannot honour a "choose the agents" phase. */
export function loadAvailableAgents(agentsDir = defaultAgentsDir()): string[] {
	const entries = fs.readdirSync(agentsDir); // throws if the dir is missing — fail loud
	return entries
		.filter((f) => f.endsWith(".md") && !f.startsWith("_archived-"))
		.map((f) => f.slice(0, -".md".length))
		.sort();
}
