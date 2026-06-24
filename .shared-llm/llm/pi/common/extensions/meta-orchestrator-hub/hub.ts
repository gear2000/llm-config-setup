/**
 * hub.ts — a standalone Pi extension: the `/hub` command to start, inspect, and
 * stop the meta-orchestrator message hub.
 *
 * DELIBERATELY SEPARATE from index.ts (the brain). It registers exactly one
 * command, `/hub`, and holds exactly one piece of state — the discovery JSON
 * path this session is pointed at — so `/hub status` and `/hub stop` INTROSPECT
 * (no name to re-type; the session already knows its own hub). Loading or
 * failing here cannot affect brain:execute-plan; the two share nothing.
 *
 *   /hub start [--json <path>]   start the hub (or just connect if one's there)
 *   /hub status                  is THIS session's hub up?   (no flag needed)
 *   /hub stop                    stop THIS session's hub      (no flag needed)
 *
 * The hub is the Go binary at ./hub/hub. The JSON path is the brain's identity:
 * launch a brain with `pi -e hub.ts --hub-json ~/.meta-orch/A.json` (or pass
 * `--json` to /hub start) and different brains get separate hubs. Precedence:
 * `/hub start --json` > launch `--hub-json` > env META_ORCH_HUB_JSON > default.
 */

import type { ExtensionAPI, ExtensionContext } from "@mariozechner/pi-coding-agent";
import { spawn } from "node:child_process";
import { fileURLToPath } from "node:url";
import * as os from "node:os";
import * as path from "node:path";
import * as fs from "node:fs";

const DEFAULT_JSON = path.join(os.homedir(), ".meta-orch", "hub.json");
const HUB_BIN = path.join(path.dirname(fileURLToPath(import.meta.url)), "hub", "hub");

/**
 * Expand a leading `~/` (or a bare `~`) to the home dir. Pi command args do NOT pass through a
 * shell, so a user-typed `--hub-json ~/.meta-orch/x.json` or `/hub start --json ~/x.json` arrives
 * with a LITERAL tilde — it must be expanded in code or the path resolves wrong. Mirrors
 * hub-transport.ts's expandHome so both halves of the extension agree on the path.
 */
export function expandHome(p: string): string {
	if (p === "~") return os.homedir();
	if (p.startsWith("~/")) return path.join(os.homedir(), p.slice(2));
	return p;
}

interface Discovery {
	host: string;
	port: number;
	url: string;
	pid: number;
	started_at: string;
}

function readDiscovery(p: string): Discovery | null {
	try {
		const d = JSON.parse(fs.readFileSync(p, "utf-8")) as Discovery;
		return d && typeof d.url === "string" && typeof d.pid === "number" ? d : null;
	} catch {
		return null;
	}
}

async function healthy(url: string): Promise<boolean> {
	const ac = new AbortController();
	const t = setTimeout(() => ac.abort(), 1500);
	try {
		const r = await fetch(`${url}/health`, { signal: ac.signal });
		return r.ok;
	} catch {
		return false;
	} finally {
		clearTimeout(t);
	}
}

const sleep = (ms: number): Promise<void> => new Promise((res) => setTimeout(res, ms));

export interface HubStartResult {
	ok: boolean;
	url?: string;
	pid?: number;
	/** true when a healthy hub already existed (we did NOT start it — so we must NOT stop it). */
	alreadyUp?: boolean;
	error?: string;
}

/**
 * Ensure a hub is running at `jsonPath`. If a healthy one is already there, return `alreadyUp:true`
 * WITHOUT starting one (so the caller knows not to tear it down). Otherwise spawn the Go binary,
 * wait for it to answer /health, and return ok with `alreadyUp:false`. Shared by `/hub start` and
 * the brain's auto-start so both behave identically. Never throws — faults come back in `error`.
 */
export async function ensureHubStarted(jsonPath: string): Promise<HubStartResult> {
	const p = expandHome(jsonPath);
	const existing = readDiscovery(p);
	if (existing && (await healthy(existing.url))) {
		return { ok: true, url: existing.url, pid: existing.pid, alreadyUp: true };
	}
	if (!fs.existsSync(HUB_BIN)) {
		return { ok: false, error: `hub binary missing at ${HUB_BIN} — build it: (cd ${path.dirname(HUB_BIN)} && go build -o hub hub.go)` };
	}
	let launchErr: string | null = null;
	const child = spawn(HUB_BIN, ["--json", p], { detached: true, stdio: "ignore" });
	// A missing/non-executable binary surfaces as an async 'error' event, not a sync throw.
	child.on("error", (e) => { launchErr = e instanceof Error ? e.message : String(e); });
	child.unref();
	for (let n = 0; n < 25; n++) {
		if (launchErr) return { ok: false, error: `could not launch the hub at ${HUB_BIN}: ${launchErr}` };
		const d = readDiscovery(p);
		if (d && (await healthy(d.url))) return { ok: true, url: d.url, pid: d.pid, alreadyUp: false };
		await sleep(200);
	}
	return { ok: false, error: `launched the hub but it didn't answer at ${p} within ~5s` };
}

/** Stop the hub recorded at `jsonPath` (SIGTERM its pid; the hub removes its own JSON on SIGTERM).
 *  Never throws. Shared by `/hub stop` and the brain's auto-teardown on shutdown. */
export function stopHub(jsonPath: string): { ok: boolean; pid?: number; error?: string } {
	const d = readDiscovery(expandHome(jsonPath));
	if (!d) return { ok: false, error: `no hub to stop — no discovery file at ${expandHome(jsonPath)}` };
	try {
		process.kill(d.pid, "SIGTERM");
		return { ok: true, pid: d.pid };
	} catch (e) {
		return { ok: false, pid: d.pid, error: e instanceof Error ? e.message : String(e) };
	}
}

/** Parse a `/hub` argument string: first non-flag token is the subcommand; --json <path> optional. */
export function parseHubArgs(args: string): { sub: string; json?: string } {
	const toks = (args ?? "").trim().split(/\s+/).filter(Boolean);
	let sub = "status";
	if (toks.length > 0 && !toks[0].startsWith("--")) sub = toks[0];
	let json: string | undefined;
	const i = toks.indexOf("--json");
	if (i >= 0 && i + 1 < toks.length) json = toks[i + 1];
	return { sub, json };
}

export default function (pi: ExtensionAPI): void {
	let jsonPath = expandHome(process.env.META_ORCH_HUB_JSON || DEFAULT_JSON);
	let ctx: ExtensionContext | null = null;

	pi.registerFlag("hub-json", {
		description: "discovery JSON path for this brain's hub (default ~/.meta-orch/hub.json)",
		type: "string",
		default: undefined,
	});

	pi.on("session_start", (_event, c) => {
		ctx = c;
		const f = pi.getFlag("hub-json") as string | undefined;
		if (f && f.length > 0) jsonPath = expandHome(f);
	});

	const notify = (msg: string, level: "info" | "warning" | "error" = "info"): void => {
		if (ctx?.hasUI) {
			try {
				ctx.ui.notify(`📡 ${msg}`, level);
			} catch {
				/* hasUI may be false in some contexts */
			}
		}
	};

	pi.registerCommand("hub", {
		description: "Meta-orchestrator hub: /hub start [--json <path>] · /hub status · /hub stop",
		handler: async (args: string, c: ExtensionContext): Promise<void> => {
			ctx = c;
			try {
				const { sub, json } = parseHubArgs(args);
				if (json) jsonPath = expandHome(json); // remember it for this session (tilde-expanded)

				if (sub === "start") {
					const r = await ensureHubStarted(jsonPath);
					if (!r.ok) { notify(r.error ?? "could not start the hub", "error"); return; }
					notify(r.alreadyUp ? `hub already up at ${r.url} (json ${jsonPath})` : `hub started at ${r.url} (json ${jsonPath}, pid ${r.pid})`);
					return;
				}

				if (sub === "status") {
					const d = readDiscovery(jsonPath);
					if (!d) {
						notify(`no hub — no discovery file at ${jsonPath}`);
						return;
					}
					const up = await healthy(d.url);
					notify(
						up ? `hub UP at ${d.url} (json ${jsonPath}, pid ${d.pid})` : `hub DOWN — stale json at ${jsonPath} (was ${d.url})`,
						up ? "info" : "warning",
					);
					return;
				}

				if (sub === "stop") {
					const r = stopHub(jsonPath);
					if (!r.ok) { notify(r.error ?? "could not stop the hub", r.pid ? "error" : "info"); return; }
					notify(`stopped hub pid ${r.pid} (json ${jsonPath})`);
					return;
				}

				notify(`unknown: /hub ${sub} — use start | status | stop`, "warning");
			} catch (err) {
				// Never let the handler throw — a thrown rejection could reach Pi's loop.
				notify(`/hub error: ${err instanceof Error ? err.message : String(err)}`, "error");
			}
		},
	});
}
