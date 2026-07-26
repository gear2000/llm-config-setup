/// <reference lib="es2022" />
// Executed corpus for work-log resolution. There is now ONE implementation —
// planish_resolve.py — and the two Pi extensions reach it by SUBPROCESS, so
// what this corpus proves is that the delegation is real and complete: every
// case is run against tf-implement.ts, do-planish.ts, and the script directly,
// and the three outcomes must agree. Substring assertions over the sources
// cannot catch an extension that quietly grew a parser of its own again.
//   node --experimental-strip-types .shared-llm/public/llm/pi/common/extensions/resolver-parity.test.ts
//
// The last two cases are the ones that killed the hand-rolled scanners: an
// escaped double-quote before ` #` (truncated at a comment that was never
// there) and a quoted comma inside a flow mapping (split into a rejected
// entry). Both are ordinary YAML that PyYAML has always read correctly.
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import { execFileSync, spawn } from "node:child_process";
import { fileURLToPath } from "node:url";
// @ts-expect-error -- Node native type-stripping resolves the TypeScript source directly.
import { resolvePlanDir } from "./tf-implement.ts";
// @ts-expect-error -- Node native type-stripping resolves the TypeScript source directly.
import { configuredHost } from "./do-planish.ts";

const HERE = path.dirname(fs.realpathSync(fileURLToPath(import.meta.url)));
const RESOLVER_PY = path.resolve(HERE, "../../../common/common/planish_resolve.py");
const TF_EXTENSION = path.join(HERE, "tf-implement.ts");
const PYTHON = process.env.PYTHON_BIN || "python3";
const TOPIC = "Redesign Auth";
const SLUG = "redesign-auth";

let passed = 0;
const failures: string[] = [];
function check(ok: boolean, label: string): void {
	if (ok) passed++;
	else failures.push(label);
}

type Outcome = { ok: boolean; dir?: string; host?: string | null };

function pythonOutcome(cwd: string): Outcome {
	try {
		const stdout = execFileSync(
			PYTHON,
			[RESOLVER_PY, "--topic", TOPIC, "--cwd", cwd],
			{ encoding: "utf-8", stdio: ["ignore", "pipe", "pipe"] },
		);
		const parsed = JSON.parse(stdout);
		return { ok: true, dir: parsed.plan_dir, host: parsed.host };
	} catch {
		return { ok: false };
	}
}

function tfOutcome(cwd: string): Outcome {
	try {
		return { ok: true, dir: resolvePlanDir(cwd, TOPIC) };
	} catch {
		return { ok: false };
	}
}

function planishOutcome(cwd: string): Outcome {
	const previous = process.cwd();
	try {
		process.chdir(cwd);
		return { ok: true, host: configuredHost() };
	} catch {
		return { ok: false };
	} finally {
		process.chdir(previous);
	}
}

type Case = {
	name: string;
	config: string;
	/** "resolve" — every resolver returns a directory; "fault" — every one fails loud. */
	outcome: "resolve" | "fault";
	/**
	 * Which resolvers a fault reaches. "all" — the `work_log:` block itself is
	 * unusable, so every resolver must fail loud. "dir" — only `work_log.dir` is
	 * bad; do-planish reads nothing but the host, so it legitimately still
	 * succeeds and must report no host rather than inventing one.
	 */
	faultScope?: "all" | "dir";
	dir?: string;
	dirPrefix?: string;
	host?: string | null;
};

const CASES: Case[] = [
	{
		name: "block mapping",
		config: 'work_log:\n  dir: "plans/{slug}"\n  host: example-host\n',
		outcome: "resolve",
		dir: `plans/${SLUG}`,
		host: "example-host",
	},
	{
		// PyYAML keeps a `#` inside a quoted scalar; a naive /#.*$/ strip loses it.
		name: "quoted hash in dir",
		config: 'work_log:\n  dir: "plans/#ticket/{slug}"\n',
		outcome: "resolve",
		dir: `plans/#ticket/${SLUG}`,
		host: null,
	},
	{
		name: "real trailing comment still stripped",
		config: "work_log:\n  dir: plans/{slug}   # where plans land\n",
		outcome: "resolve",
		dir: `plans/${SLUG}`,
		host: null,
	},
	{
		name: "flow mapping",
		config: 'work_log: {dir: "plans/{slug}", host: flow-host}\n',
		outcome: "resolve",
		dir: `plans/${SLUG}`,
		host: "flow-host",
	},
	{
		name: "sequence work_log fails loud",
		config: "work_log:\n  - dir: plans/{slug}\n",
		outcome: "fault",
	},
	{
		name: "empty block fails loud",
		config: "work_log:\n",
		outcome: "fault",
	},
	{
		name: "empty flow mapping fails loud",
		config: "work_log: {}\n",
		outcome: "fault",
	},
	{
		name: "scalar work_log fails loud",
		config: "work_log: /var/tmp/plans\n",
		outcome: "fault",
	},
	{
		name: "malformed dir fails loud for the dir consumers",
		config: 'work_log:\n  dir: ""\n',
		outcome: "fault",
		faultScope: "dir",
	},
	{
		// The hand-rolled scanners closed the quote on the `\"` escape and then
		// truncated at the ` #` that followed, losing the rest of the path.
		name: "escaped quote before a hash keeps the whole path",
		config: 'work_log:\n  dir: "a\\" #b/{slug}"\n',
		outcome: "resolve",
		dir: `a" #b/${SLUG}`,
		host: null,
	},
	{
		// The hand-rolled scanners split a flow mapping on every comma, including
		// one inside a quoted scalar, and then rejected the fragment as not a
		// mapping entry — a config PyYAML reads without complaint.
		name: "quoted comma inside a flow mapping",
		config: 'work_log: {dir: "plans/a,b/{slug}", host: flow-host}\n',
		outcome: "resolve",
		dir: `plans/a,b/${SLUG}`,
		host: "flow-host",
	},
	{
		// A destination roster carries no work_log: — it must be skipped, not
		// mistaken for one, leaving every resolver on the default template.
		name: "roster without work_log takes the default",
		config: "destinations:\n  - dir: /elsewhere\n    harnesses: cc\n",
		outcome: "resolve",
		dirPrefix: `${fs.realpathSync("/var/tmp")}/work-log/`,
		host: null,
	},
];

for (const testCase of CASES) {
	const root = fs.realpathSync(fs.mkdtempSync(path.join(os.tmpdir(), "parity-")));
	fs.writeFileSync(path.join(root, ".shared-llm.yaml"), testCase.config);

	const py = pythonOutcome(root);
	const tf = tfOutcome(root);
	const planish = planishOutcome(root);
	const want = testCase.outcome === "resolve";
	// A dir-only fault never reaches do-planish, which reads the host and nothing else.
	const wantPlanish = want || testCase.faultScope === "dir";

	check(
		py.ok === want && tf.ok === want && planish.ok === wantPlanish,
		`${testCase.name}: expected python/tf-implement to ${testCase.outcome} and ` +
			`do-planish to ${wantPlanish ? "resolve" : "fault"} — ` +
			`python=${py.ok} tf-implement=${tf.ok} do-planish=${planish.ok}`,
	);
	if (!want) {
		if (testCase.faultScope === "dir" && planish.ok) {
			check(
				(planish.host ?? null) === null,
				`${testCase.name}: do-planish must report no host, got ${planish.host}`,
			);
		}
		continue;
	}
	if (!py.ok || !tf.ok || !planish.ok) continue;

	check(
		py.dir === tf.dir,
		`${testCase.name}: plan dir differs — python=${py.dir} tf-implement=${tf.dir}`,
	);
	if (testCase.dir !== undefined) {
		check(
			py.dir === path.join(root, testCase.dir),
			`${testCase.name}: expected ${testCase.dir}, got ${py.dir}`,
		);
	}
	if (testCase.dirPrefix !== undefined) {
		check(
			py.dir!.startsWith(testCase.dirPrefix),
			`${testCase.name}: expected a dir under ${testCase.dirPrefix}, got ${py.dir}`,
		);
	}
	check(
		(py.host ?? null) === (planish.host ?? null),
		`${testCase.name}: host differs — python=${py.host} do-planish=${planish.host}`,
	);
	check(
		(py.host ?? null) === (testCase.host ?? null),
		`${testCase.name}: expected host ${testCase.host}, got ${py.host}`,
	);
}

// ─── The copied extension with no script and no config ────────────────────────
//
// tf-implement.ts carries ONE piece of resolution of its own: the fallback for a
// machine that has neither the canonical script nor any config file. It is
// reached only by a COPY of the extension living outside the kit, so the checks
// above — which import the in-tree file, beside the script — never touch it.
// Here the extension is copied to a temp directory (nothing resolvable beside
// it), run from a temp cwd with no config above it, and its answer compared to
// the script's for the same $WORK_LOG_DIR. A "~" template is the case that
// diverged: the script expands it to $HOME, the copy used to make a literal "~"
// directory under the cwd.
function copiedExtensionOutcome(cwd: string, env: NodeJS.ProcessEnv): Outcome {
	const box = fs.realpathSync(fs.mkdtempSync(path.join(os.tmpdir(), "parity-copy-")));
	const copy = path.join(box, "tf-implement.ts");
	fs.copyFileSync(TF_EXTENSION, copy);
	const runner = path.join(box, "run.ts");
	fs.writeFileSync(
		runner,
		`import { resolvePlanDir } from ${JSON.stringify(copy)};\n` +
			`console.log(resolvePlanDir(process.argv[2], ${JSON.stringify(TOPIC)}));\n`,
	);
	try {
		const stdout = execFileSync(
			process.execPath,
			["--experimental-strip-types", runner, cwd],
			{ encoding: "utf-8", env, stdio: ["ignore", "pipe", "pipe"] },
		);
		return { ok: true, dir: stdout.trim() };
	} catch {
		return { ok: false };
	}
}

function pythonOutcomeWithEnv(cwd: string, env: NodeJS.ProcessEnv): Outcome {
	try {
		const stdout = execFileSync(
			PYTHON,
			[RESOLVER_PY, "--topic", TOPIC, "--cwd", cwd],
			{ encoding: "utf-8", env, stdio: ["ignore", "pipe", "pipe"] },
		);
		const parsed = JSON.parse(stdout);
		return { ok: true, dir: parsed.plan_dir, host: parsed.host };
	} catch {
		return { ok: false };
	}
}

{
	const fakeHome = fs.realpathSync(fs.mkdtempSync(path.join(os.tmpdir(), "parity-home-")));
	// A temp cwd with no .shared-llm.yaml / .planish.yaml anywhere above it, so
	// the copy takes the fallback instead of stopping on a config it cannot read.
	const bareCwd = fs.realpathSync(fs.mkdtempSync(path.join(os.tmpdir(), "parity-bare-")));

	for (const template of ["~/plans/{slug}", "~"]) {
		const env = { ...process.env, HOME: fakeHome, WORK_LOG_DIR: template };
		const py = pythonOutcomeWithEnv(bareCwd, env);
		const copied = copiedExtensionOutcome(bareCwd, env);
		check(
			py.ok && copied.ok,
			`copied extension, $WORK_LOG_DIR=${template}: both resolvers must succeed — ` +
				`python=${py.ok} copy=${copied.ok}`,
		);
		if (!py.ok || !copied.ok) continue;
		check(
			py.dir === copied.dir,
			`copied extension, $WORK_LOG_DIR=${template}: plan dir differs — ` +
				`python=${py.dir} copy=${copied.dir}`,
		);
		check(
			copied.dir!.startsWith(fakeHome + path.sep) || copied.dir === fakeHome,
			`copied extension, $WORK_LOG_DIR=${template}: "~" must expand to ${fakeHome}, ` +
				`got ${copied.dir}`,
		);
	}

	// "~otheruser" needs a passwd lookup Node has no equivalent for. The copy
	// refuses it rather than inventing <cwd>/~otheruser or a path under $HOME.
	const otherUser = {
		...process.env,
		HOME: fakeHome,
		WORK_LOG_DIR: "~nosuchuser42/plans/{slug}",
	};
	check(
		!copiedExtensionOutcome(bareCwd, otherUser).ok,
		'copied extension: "~nosuchuser42/…" must fail loud, not resolve',
	);
}

// ─── Concurrent {n} allocation ────────────────────────────────────────────────
//
// Scan-then-mkdir(recursive) hands the same vN to every caller that scans before
// any of them creates. Eight processes race for the same versioned template; all
// eight must come away with a directory of their own.
const raceRoot = fs.realpathSync(fs.mkdtempSync(path.join(os.tmpdir(), "parity-race-")));
fs.writeFileSync(
	path.join(raceRoot, ".shared-llm.yaml"),
	'work_log:\n  dir: "plans/{slug}/v{n}"\n',
);
// Each child blocks on a starting gun before resolving. Without it, process
// startup skew lets every child finish before the next one scans, and the race
// this check exists to catch never actually happens.
const startingGun = path.join(raceRoot, "go");
const driver = path.join(raceRoot, "driver.ts");
fs.writeFileSync(
	driver,
	`import * as fs from "node:fs";\n` +
		`import { resolvePlanDir } from ${JSON.stringify(TF_EXTENSION)};\n` +
		`const gun = ${JSON.stringify(startingGun)};\n` +
		`const idle = new Int32Array(new SharedArrayBuffer(4));\n` +
		`const deadline = Date.now() + 30_000;\n` +
		`while (!fs.existsSync(gun) && Date.now() < deadline) Atomics.wait(idle, 0, 0, 2);\n` +
		`console.log(resolvePlanDir(process.argv[2], ${JSON.stringify(TOPIC)}));\n`,
);

const CALLERS = 8;
const children = Array.from({ length: CALLERS }, () => {
	return new Promise<string>((resolve, reject) => {
		const child = spawn(
			process.execPath,
			["--experimental-strip-types", driver, raceRoot],
			{ stdio: ["ignore", "pipe", "pipe"] },
		);
		let out = "";
		let err = "";
		child.stdout.on("data", (chunk) => (out += chunk));
		child.stderr.on("data", (chunk) => (err += chunk));
		child.on("close", (code) =>
			code === 0 ? resolve(out.trim()) : reject(new Error(err.trim())),
		);
	});
});
await new Promise((resolve) => setTimeout(resolve, 750)); // let every child reach the gun
fs.writeFileSync(startingGun, "");
const claimed = await Promise.all(children);
check(
	new Set(claimed).size === CALLERS,
	`tf-implement {n}: ${CALLERS} concurrent callers must claim ${CALLERS} distinct dirs — got ` +
		`${new Set(claimed).size} unique of ${claimed.length}`,
);
check(
	claimed.every((dir) => /\/v\d+$/.test(dir)),
	`tf-implement {n}: every claimed dir must end in a version segment — ${claimed.join(", ")}`,
);

const total = passed + failures.length;
console.log(`resolver-parity: ${passed}/${total} passed`);
if (failures.length > 0) {
	console.error("FAILURES:");
	console.error(failures.join("\n"));
	throw new Error(`resolver-parity failed ${failures.length} check(s)`);
}
console.log("ALL PASS ✓");
