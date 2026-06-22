// Unit test for the generated --mcp-config and claude argv. Pure generation; writes one temp
// file to assert round-trip. No spawn, no Pi runtime.
//   node --experimental-strip-types mcp-config.test.ts
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import {
	buildMcpConfig,
	writeMcpConfig,
	buildClaudeArgs,
	buildRunPhaseArgs,
	buildAskBrainInstruction,
	ASK_BRAIN_SERVER_NAME,
	ASK_BRAIN_SOCKET_ENV,
	ASK_BRAIN_PHASE_ENV,
	ASK_BRAIN_MAX_HOPS_ENV,
} from "./mcp-config.ts";

let pass = 0;
const fails: string[] = [];
function check(name: string, cond: boolean, detail = "") {
	if (cond) pass++;
	else fails.push(`  ✗ ${name}${detail ? ` — ${detail}` : ""}`);
}

// 1. config registers exactly the ask_brain server pointing at the given socket
{
	const cfg = buildMcpConfig({ socketPath: "/tmp/p1.sock", phaseId: "orch-x-p1", maxHops: 5, serverPath: "/srv/ask.mjs" });
	check("one mcp server", Object.keys(cfg.mcpServers).length === 1);
	const srv = cfg.mcpServers[ASK_BRAIN_SERVER_NAME];
	check("server name is ask_brain", !!srv, Object.keys(cfg.mcpServers).join());
	check("server args reference the server path", srv.args.join(" ") === "/srv/ask.mjs");
	check("env carries the socket", srv.env[ASK_BRAIN_SOCKET_ENV] === "/tmp/p1.sock");
	check("env carries the phase id", srv.env[ASK_BRAIN_PHASE_ENV] === "orch-x-p1");
	check("env carries max hops", srv.env[ASK_BRAIN_MAX_HOPS_ENV] === "5");
	check("command is an absolute node path", path.isAbsolute(srv.command), srv.command);
}

// 2. write + read back round-trips to the same JSON
{
	const dir = fs.mkdtempSync(path.join(os.tmpdir(), "mcpcfg-"));
	const p = path.join(dir, "deep", "phase.mcp.json"); // also asserts mkdir -p of the dirname
	const cfg = buildMcpConfig({ socketPath: "/tmp/s.sock", phaseId: "orch-x-p1", maxHops: 3, serverPath: "/srv/ask.mjs" });
	writeMcpConfig(p, cfg);
	const back = JSON.parse(fs.readFileSync(p, "utf-8"));
	check("written config has ask_brain server", !!back.mcpServers[ASK_BRAIN_SERVER_NAME]);
	check("written socket matches", back.mcpServers[ASK_BRAIN_SERVER_NAME].env[ASK_BRAIN_SOCKET_ENV] === "/tmp/s.sock");
	fs.rmSync(dir, { recursive: true, force: true });
}

// 3. claude argv: the exact flags the live CLI requires, in order, with the team prompt
{
	const args = buildClaudeArgs({
		team: "alpha",
		task: "offboard then onboard",
		mcpConfigPath: "/cfg/phase.mcp.json",
		appendSystemPrompt: "INSTRUCTION",
	});
	check("argv starts with -p and the /team prompt", args[0] === "-p" && args[1] === "/team alpha offboard then onboard", args.slice(0, 2).join(" "));
	check("argv requests stream-json", args.includes("--output-format") && args[args.indexOf("--output-format") + 1] === "stream-json");
	check("argv passes --verbose (required for stream-json events)", args.includes("--verbose"));
	check("argv passes the mcp-config path", args[args.indexOf("--mcp-config") + 1] === "/cfg/phase.mcp.json");
	check("argv pins --strict-mcp-config", args.includes("--strict-mcp-config"));
	check("argv appends the system prompt", args[args.indexOf("--append-system-prompt") + 1] === "INSTRUCTION");
	check("argv skips permission prompts for headless run", args.includes("--allow-dangerously-skip-permissions"));
	check("no model flag when none given", !args.includes("--model"));
}

// 4. model is appended when provided
{
	const args = buildClaudeArgs({ team: "t", task: "x", mcpConfigPath: "/c.json", appendSystemPrompt: "I", model: "claude-sonnet" });
	check("model appended", args[args.indexOf("--model") + 1] === "claude-sonnet");
}

// 5. ask_brain instruction names the tool + the severities, says the worker runs to completion
//    (no step limit), and carries NO heartbeat cadence (the liveness kill + heartbeat were removed).
{
	const instr = buildAskBrainInstruction({ phaseId: "orch-onb-p1" });
	check("instruction names the tool", instr.includes(ASK_BRAIN_SERVER_NAME));
	check("instruction says run to completion / no step limit", instr.toLowerCase().includes("no step limit"));
	check("instruction does NOT inject a heartbeat cadence", !instr.toLowerCase().includes("heartbeat") && !instr.includes("significant steps"));
	check("instruction lists the blocked severity", instr.includes('"blocked"'));
	check("instruction says STOP ends the phase", instr.toLowerCase().includes("stop"));
}

// 6. buildRunPhaseArgs: the brain's /run-phase prompt + the SAME ask_brain wiring as /team
{
	const args = buildRunPhaseArgs({
		planFile: "/home/user/plan.md",
		phase: "0",
		agents: ["code-review", "deployer"],
		mcpConfigPath: "/cfg/phase.mcp.json",
		appendSystemPrompt: "INSTRUCTION",
	});
	check(
		"argv starts with -p and the /run-phase prompt",
		args[0] === "-p" && args[1] === "/run-phase plan=/home/user/plan.md phase=0 agents=code-review,deployer",
		args.slice(0, 2).join(" "),
	);
	check("run-phase argv requests stream-json", args.includes("--output-format") && args[args.indexOf("--output-format") + 1] === "stream-json");
	check("run-phase argv passes --verbose", args.includes("--verbose"));
	check("run-phase argv passes the mcp-config path", args[args.indexOf("--mcp-config") + 1] === "/cfg/phase.mcp.json");
	check("run-phase argv pins --strict-mcp-config", args.includes("--strict-mcp-config"));
	check("run-phase argv appends the system prompt", args[args.indexOf("--append-system-prompt") + 1] === "INSTRUCTION");
	check("run-phase argv skips permission prompts", args.includes("--allow-dangerously-skip-permissions"));
	check("no model flag when none given", !args.includes("--model"));
}

// 7. buildRunPhaseArgs: agents render as one comma-token (trimmed, no empties), model appended
{
	const args = buildRunPhaseArgs({
		planFile: "/p.md",
		phase: "3",
		agents: [" backend ", "", "plan-watchdog"],
		mcpConfigPath: "/c.json",
		appendSystemPrompt: "I",
		model: "opus",
	});
	check(
		"agents are trimmed + comma-joined with empties dropped",
		args[1] === "/run-phase plan=/p.md phase=3 agents=backend,plan-watchdog",
		args[1],
	);
	check("run-phase model appended", args[args.indexOf("--model") + 1] === "opus");
}

console.log(`mcp-config: ${pass} checks passed`);
if (fails.length) { console.log("FAILURES:"); console.log(fails.join("\n")); process.exit(1); }
console.log("ALL PASS ✓");
