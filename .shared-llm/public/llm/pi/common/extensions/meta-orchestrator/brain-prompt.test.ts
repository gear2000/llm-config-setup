// Unit test for defaultAgentsDir's cwd-anchored repo-root discovery (brain-prompt.ts) — the
// correctness fix for the bug where the agents dir was resolved from this module's OWN install
// location (import.meta.url + a hardcoded 7-level `../` walk) instead of the cwd of the repo whose
// plan is running. That fixed-depth walk broke the moment the extension's on-disk depth changed: a
// normal symlinked load silently resolved to the KIT's .claude/agents (wrong roster), and a direct
// load from a destination's deeper .shared-llm/public/... path failed outright. Pure — no real Pi,
// no real repo; every case builds a throwaway fixture with fs.mkdtempSync.
//
//   node --experimental-strip-types brain-prompt.test.ts
import { defaultAgentsDir } from "./brain-prompt.ts";
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";

let pass = 0;
const fails: string[] = [];
function check(name: string, cond: boolean, detail = "") {
	if (cond) pass++;
	else fails.push(`  ✗ ${name}${detail ? ` — ${detail}` : ""}`);
}

/** A throwaway fake repo: a `.git` marker dir at the top + `.claude/agents/` under it — enough for
 *  the walk-up (inside defaultAgentsDir) to anchor on. `nestedSegments` places the returned cwd at
 *  some depth under the root, mirroring where the extension module actually sits on disk (shallow
 *  in the kit vs. deep under a destination's .shared-llm/public/...) — the walk-up must land on the
 *  SAME root regardless of how many segments that is. Returns the root, the nested cwd, and a
 *  cleanup fn. */
function makeFakeRepo(...nestedSegments: string[]): { root: string; nestedDir: string; cleanup: () => void } {
	const root = fs.mkdtempSync(path.join(os.tmpdir(), "meta-orch-agentsdir-"));
	fs.mkdirSync(path.join(root, ".git")); // a plain checkout's .git is a directory (a worktree's is a file — either satisfies existsSync)
	fs.mkdirSync(path.join(root, ".claude", "agents"), { recursive: true });
	const nestedDir = nestedSegments.length ? path.join(root, ...nestedSegments) : root;
	fs.mkdirSync(nestedDir, { recursive: true });
	return { root, nestedDir, cleanup: () => fs.rmSync(root, { recursive: true, force: true }) };
}

async function main() {
	// Defensive: make sure no ambient env var leaks into the no-override cases below (the test
	// process shouldn't have this set, but never assume — save it to restore at the end either way).
	const savedEnv = process.env.META_ORCH_AGENTS_DIR;
	delete process.env.META_ORCH_AGENTS_DIR;

	// ── case 1: SHALLOW fixture path — a shorter `.shared-llm/` depth the walk-up
	//    resolver must still handle (.shared-llm/llm/pi/common/extensions/meta-orchestrator).
	//    The kit's OWN content now sits at the deeper public/ depth (case 2); this
	//    case just proves the resolver is depth-agnostic. ──
	{
		const { root, nestedDir, cleanup } = makeFakeRepo(".shared-llm", "llm", "pi", "common", "extensions", "meta-orchestrator");
		const got = defaultAgentsDir(nestedDir);
		check("shallow fixture resolves to <root>/.claude/agents", got === path.join(root, ".claude", "agents"), got);
		cleanup();
	}

	// ── case 2: DEEP fixture path — mirrors a destination repo's deeper install depth
	//    (.shared-llm/public/llm/pi/common/extensions/meta-orchestrator). This is the core of the
	//    fix: the old code hardcoded a 7-level `../` walk from import.meta.url, which only matched
	//    ONE specific depth — it must now match BOTH depths identically. ──
	{
		const { root, nestedDir, cleanup } = makeFakeRepo(".shared-llm", "public", "llm", "pi", "common", "extensions", "meta-orchestrator");
		const got = defaultAgentsDir(nestedDir);
		check("deep fixture resolves to the SAME <root>/.claude/agents shape, depth-independent", got === path.join(root, ".claude", "agents"), got);
		cleanup();
	}

	// ── case 3: a cwd that is an arbitrary subdirectory below the repo root (not shaped like the
	//    extension's own install path at all) still walks up and finds the root ──
	{
		const { root, nestedDir, cleanup } = makeFakeRepo("some", "unrelated", "nested", "subdir");
		const got = defaultAgentsDir(nestedDir);
		check("arbitrary subdirectory still resolves to <root>/.claude/agents", got === path.join(root, ".claude", "agents"), got);
		cleanup();
	}

	// ── case 4: META_ORCH_AGENTS_DIR overrides and wins regardless of cwd ──
	{
		const { nestedDir, cleanup } = makeFakeRepo(".shared-llm", "public", "llm", "pi", "common", "extensions", "meta-orchestrator");
		const overrideDir = fs.mkdtempSync(path.join(os.tmpdir(), "meta-orch-agentsdir-override-"));
		process.env.META_ORCH_AGENTS_DIR = overrideDir;
		const got = defaultAgentsDir(nestedDir);
		check("META_ORCH_AGENTS_DIR overrides the walk-up result", got === overrideDir, got);
		delete process.env.META_ORCH_AGENTS_DIR;
		fs.rmSync(overrideDir, { recursive: true, force: true });
		cleanup();
	}

	// Restore whatever the ambient env had (almost certainly undefined) so this test never leaks
	// state into a later test run in the same process.
	if (savedEnv === undefined) delete process.env.META_ORCH_AGENTS_DIR;
	else process.env.META_ORCH_AGENTS_DIR = savedEnv;
}

main()
	.then(() => {
		console.log(`brain-prompt: ${pass} checks passed`);
		if (fails.length) { console.log("FAILURES:"); console.log(fails.join("\n")); process.exit(1); }
		console.log("ALL PASS ✓");
	})
	.catch((err) => {
		console.log(`brain-prompt test FAILED to run: ${err instanceof Error ? err.stack : String(err)}`);
		process.exit(1);
	});
