/**
 * mcp-config — generate the per-phase `--mcp-config` that gives the spawned `claude -p`
 * its `ask_brain` tool, and build the full `claude` argv.
 *
 * This is the crux of the claude bridge. `claude -p` cannot listen on a socket, so to let
 * the worker phone the brain we hand it ONE local MCP server, `ask_brain`, implemented as a
 * tiny stdio program (ask-brain-server.mjs). The server is wired to the per-phase Unix
 * socket via an env var; when the worker calls the `ask_brain` tool, the server opens that
 * socket, writes an `ask` envelope, BLOCKS reading the `answer`, and returns it to the
 * worker (see envelope.ts + back-channel.ts).
 *
 * Claude's `--mcp-config` accepts a JSON file describing `mcpServers` (the same schema
 * Claude Code uses for its own .mcp.json). A `stdio` server entry is `{ command, args, env }`.
 * We pin `--strict-mcp-config` on the spawn so ONLY this generated config is loaded — the
 * worker can't pick up an unrelated project .mcp.json and the brain channel is the only
 * MCP surface.
 *
 * Pure + fs-write only (no spawn, no net) → the config generation is unit-testable: feed a
 * socket path, assert the JSON has the ask_brain server pointing at that socket.
 */

import * as fs from "node:fs";
import * as path from "node:path";
import { fileURLToPath } from "node:url";

/** The MCP server name the worker sees. The init line's mcp_servers must contain this. */
export const ASK_BRAIN_SERVER_NAME = "ask_brain";

/** Env var the stdio server reads to find its per-phase socket + phase id + hop budget. */
export const ASK_BRAIN_SOCKET_ENV = "META_ORCH_ASK_BRAIN_SOCKET";
export const ASK_BRAIN_PHASE_ENV = "META_ORCH_PHASE_ID";
export const ASK_BRAIN_MAX_HOPS_ENV = "META_ORCH_MAX_HOPS";

/** Absolute path to the stdio MCP server that ships next to this module. */
export function askBrainServerPath(): string {
	return path.join(path.dirname(fileURLToPath(import.meta.url)), "ask-brain-server.mjs");
}

export interface McpConfig {
	mcpServers: Record<string, { command: string; args: string[]; env: Record<string, string> }>;
}

/**
 * Build the MCP config object that registers the ask_brain stdio server for one phase. The
 * server is launched by `node <ask-brain-server.mjs>`; its socket/phase/hop settings ride
 * in `env` so the same server binary serves every phase, parameterised per spawn.
 */
export function buildMcpConfig(opts: {
	socketPath: string;
	phaseId: string;
	maxHops: number;
	serverPath?: string;
}): McpConfig {
	const serverPath = opts.serverPath ?? askBrainServerPath();
	return {
		mcpServers: {
			[ASK_BRAIN_SERVER_NAME]: {
				command: process.execPath, // the same node that runs Pi — guaranteed present
				args: [serverPath],
				env: {
					[ASK_BRAIN_SOCKET_ENV]: opts.socketPath,
					[ASK_BRAIN_PHASE_ENV]: opts.phaseId,
					[ASK_BRAIN_MAX_HOPS_ENV]: String(opts.maxHops),
				},
			},
		},
	};
}

/** Serialise + write the config to disk; returns the path. Fails loud on a write error. */
export function writeMcpConfig(configPath: string, config: McpConfig): string {
	fs.mkdirSync(path.dirname(configPath), { recursive: true });
	fs.writeFileSync(configPath, JSON.stringify(config, null, 2));
	return configPath;
}

/**
 * Build the full argv for the spawned worker:
 *
 *   claude -p "/team <team> <task>"
 *     --output-format stream-json --verbose
 *     --mcp-config <generatedPath> --strict-mcp-config
 *     --append-system-prompt <ask_brain contract>
 *     --allow-dangerously-skip-permissions
 *
 * Notes locked against the live CLI (`claude --help`):
 *  - `--output-format stream-json` REQUIRES `--print`/`-p`; and stream-json output requires
 *    `--verbose` to emit the per-event lines we parse.
 *  - `--mcp-config <configs...>` takes the generated file; `--strict-mcp-config` restricts
 *    to it so no stray project config leaks in.
 *  - the worker runs unattended head-down, so permission prompts must not block it; the
 *    leader's safety is the worker's own escalations (stop-from-ask) plus one generous
 *    wall-clock timeout, and the /team skill restricts unattended auto-fix to scratch only.
 */
export function buildClaudeArgs(opts: {
	team: string;
	task: string;
	mcpConfigPath: string;
	appendSystemPrompt: string;
	model?: string;
}): string[] {
	const prompt = `/team ${opts.team} ${opts.task}`.trim();
	return assembleClaudeArgs(prompt, opts.mcpConfigPath, opts.appendSystemPrompt, opts.model);
}

/**
 * Build the full argv for the NEW worker invocation — the meta-orchestrator brain's per-phase
 * launch:
 *
 *   claude -p "/run-phase plan=<planFile> phase=<phase> agents=<a,b,...>"
 *     --output-format stream-json --verbose
 *     --mcp-config <generatedPath> --strict-mcp-config
 *     --append-system-prompt <ask_brain contract>
 *     --allow-dangerously-skip-permissions
 *
 * Same ask_brain wiring as buildClaudeArgs (the worker still phones the brain), but the prompt
 * is the brain's `/run-phase` playbook — the plan file, the phase number, and the EXPLICIT agent
 * list the brain chose — instead of a `/team` registry lookup. The agent list is rendered as a
 * single comma-separated token (no spaces), exactly as the `/run-phase` skill parses `agents=`.
 *
 * Separate from buildClaudeArgs on purpose: the Pi leader still calls buildClaudeArgs for its
 * `/team` worker; this is the SDK brain's path. Neither touches the other.
 */
export function buildRunPhaseArgs(opts: {
	planFile: string;
	phase: string;
	agents: string[];
	mcpConfigPath: string;
	appendSystemPrompt: string;
	model?: string;
}): string[] {
	const agentList = opts.agents.map((a) => a.trim()).filter(Boolean).join(",");
	const prompt = `/run-phase plan=${opts.planFile} phase=${opts.phase} agents=${agentList}`;
	return assembleClaudeArgs(prompt, opts.mcpConfigPath, opts.appendSystemPrompt, opts.model);
}

/**
 * The shared flag block both worker invocations need: stream-json + the ask_brain --mcp-config
 * (pinned with --strict-mcp-config) + the ask_brain --append-system-prompt + the headless
 * permission skip. Only the `-p` prompt and the optional --model differ between the two callers,
 * so they pass those in and this assembles the rest identically.
 */
function assembleClaudeArgs(prompt: string, mcpConfigPath: string, appendSystemPrompt: string, model?: string): string[] {
	const args = [
		"-p",
		prompt,
		"--output-format",
		"stream-json",
		"--verbose",
		"--mcp-config",
		mcpConfigPath,
		"--strict-mcp-config",
		"--append-system-prompt",
		appendSystemPrompt,
		"--allow-dangerously-skip-permissions",
	];
	if (model) {
		args.push("--model", model);
	}
	return args;
}

/**
 * The system-prompt addendum that teaches the worker the ask_brain contract: WHEN to call it
 * (the decide-or-ask threshold). There is NO heartbeat cadence and NO step limit — the worker runs
 * to completion like a plain `claude -p`, and the leader reaches it only when the worker itself
 * escalates (or via the one generous wall-clock timeout, the last-resort guard). The `/team` and
 * `/run-phase` skills document this contract too; we inject it at spawn so it holds even if the
 * skill prompt drifts.
 */
export function buildAskBrainInstruction(opts: { phaseId: string }): string {
	return [
		`META-ORCHESTRATION BACK-CHANNEL (phase ${opts.phaseId}).`,
		`You have an MCP tool "${ASK_BRAIN_SERVER_NAME}". It reaches the resident leader and BLOCKS until the leader answers.`,
		`Call ${ASK_BRAIN_SERVER_NAME}(severity, message) when:`,
		`- severity "blocked": you cannot proceed without a decision (missing input, ambiguous scope, a failure you cannot resolve on the repo's rails).`,
		`- severity "decision": two valid paths and the choice is the leader's to make.`,
		`- severity "progress": a discovery that changes the plan — report it, then continue.`,
		`The call returns the leader's guidance. If the answer says STOP, end the phase now and report why.`,
		`Run the phase to completion; there is no step limit. Do not call ${ASK_BRAIN_SERVER_NAME} for routine progress that needs no decision.`,
	].join("\n");
}
