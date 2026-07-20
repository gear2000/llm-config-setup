# GPT-5.6 in the Claude Code harness ("Claudex") — Options & Research

> Status: **research only — no route change decided.** Current herdr runs keep the decided
> 2026-07-16 lineup: Fable 5 (high) implements on the claude harness, gpt-5.6-sol (high)
> audits on the pi harness, Fable low for leads and QA stages.

## Context

On 2026-07-11 Theo (t3.gg) reported that `gpt-5.6-sol` runs *meaningfully better inside
Claude Code than in its native Codex harness* — same model, different shell, better output.
OpenAI's Codex lead (Tibo) then published the recipe himself ("if this gets blocked, I owe
you a reset"), which made the pattern semi-official. The community name for it is
**Claudex**: Claude Code's interface and tool-calling, GPT's inference.

The load-bearing insight is not the model — it's the workflow layer. Codex has a live
subagent bug: setting Sol to `ultra` effort forces every spawned subagent to `ultra` too,
with no way to pin a subagent's model or effort (massive token burn). Claude Code exposes
deterministic workflow primitives — `CLAUDE_CODE_SUBAGENT_MODEL`, per-agent effort, hooks
that always fire, skills, slash commands — so the same brain does better work because the
harness around it is controllable. That is the same thesis our route.yaml already encodes:
pin who runs what, per stage, deterministically.

## The approaches

### 1. Codex CLI direct (dropped 2026-07-16)

`codex exec` with `gpt-5.6-*`. Used through the 2026-07-14 proofread runs. Dropped by
user direction. Known problems: the subagent effort-inheritance bug above, CLI version
gating (5.6 family needs codex-cli ≥ 0.143), and one hallucinated-model incident in route
history ("codex-5.5-max") that forced a verify-before-wiring rule.

### 2. Pi harness routing (current)

Route profile `harness: pi`, `model: openai-codex/gpt-5.6-sol:high`. Precedent:
`work-log/2026-07-12/diagram-findings/route.yaml`. Pros: already wired into herdr
profiles, no extra proxy process, determinism lives in OUR layer (route.yaml pins
harness/model/effort/agent per stage). Cons: pi's toolset, not Claude Code's — the GPT
brain doesn't get Claude Code's skills/hooks/subagent controls.

### 3. Claudex via CLIProxyAPI (the researched option)

A local proxy (`router-for-me/CLIProxyAPI`) logs into ChatGPT Codex with normal OAuth and
exposes an Anthropic-compatible endpoint. Claude Code reads `ANTHROPIC_BASE_URL`, so
pointing it at the proxy makes the unmodified `claude` CLI run with GPT-5.6 answering.
Community convention is a `claudex` shell alias so plain `claude` stays untouched, with:

```bash
CLAUDE_CODE_SUBAGENT_MODEL=gpt-5.6-sol   # pin subagents (fixes the Codex inheritance bug)
CLAUDE_CODE_ALWAYS_ENABLE_EFFORT=1
CLAUDE_CODE_MAX_TOOL_USE_CONCURRENCY=3   # stability
ENABLE_TOOL_SEARCH=false                 # leaner sessions
```

Pros: the reported quality gain, Claude Code's deterministic workflow primitives with a
GPT brain, subscription-based (no per-token API bill), recipe endorsed by OpenAI's Codex
lead. Cons and risks: quality claims are single-source side-by-sides (Theo), not
independent benchmarks; third-party frontends are formally a ToS grey area (community
advice: secondary ChatGPT account); the proxy holds OAuth tokens locally (never deploy it
publicly); translation quirks possible on streaming/tool calls. Note the asymmetry:
Anthropic explicitly blocks the *reverse* (Claude-subscription OAuth through proxies,
since early 2026) — Claudex uses only OpenAI credentials, so Anthropic isn't implicated.

### 4. Lighter single-purpose proxies (same pattern)

- `raine/claude-code-proxy` — subscription OAuth, maintained specifically for
  gpt-5.6-sol/terra/luna.
- `1rgs/claude-code-proxy`, `fuergaosi233/claude-code-proxy` — API-key translation
  (pay per token, no OAuth grey area).

## Fit with our routing

Our runner already delivers the thing Theo praises: one command (`just herdr-plan <dir>`),
then route.yaml deterministically pins harness, model, effort, and agent persona for every
stage of every phase — plus adversarial gates and durable evidence no single interactive
session gives. Claudex would slot in as **one new `llm_profiles` entry** (claude harness
launched with the proxy's env), e.g. a stage-2 reviewer that is "gpt-5.6-sol inside Claude
Code" instead of "gpt-5.6-sol on pi". Stage-2 independence would then rest on model family
+ profile + agent (both stages on the claude harness), which the runnable check accepts.
Required runner extension: per-profile environment injection when launching the worker
pane (small, contained). Skill-placement note from HARNESS-ROUTING.md applies: a claudex
worker reads `.claude/skills/`, so it sees `cc-*`/common skills, not `do-*`.

## Open questions before adopting

1. Independent quality validation on OUR workloads — cleanest test: rerun one finished
   phase with the stage-2 profile swapped (pi-sol vs claudex-sol) and diff the audits.
2. Long-run stability: herdr stages run 20–60 minutes; proxy token refresh under load.
3. ChatGPT subscription quota burn vs the pi route.
4. Whether the route schema grows an `env:` block per profile, or the launcher owns it.

## Recommendation

No change now. If the temptation returns, pilot on ONE phase (pilot-before-scale), on a
secondary ChatGPT account, proxy bound to localhost only.

## Sources (accessed 2026-07-16)

- Theo's original claim: <https://x.com/theo/status/2075776733626892542>
- Theo on the Codex subagent effort bug: <https://x.com/theo/status/2075742083370127504>
- Claudex setup guide (explainx, 2026-07): <https://www.explainx.ai/blog/gpt-5-6-sol-claude-code-claudex-setup-guide-july-2026>
- CLIProxyAPI: <https://github.com/router-for-me/CLIProxyAPI>
- CLIProxyAPI Claude Code compatibility docs: <https://help.router-for.me/configuration/provider/claude-code-compatibility>
- raine/claude-code-proxy: <https://github.com/raine/claude-code-proxy>
- Harness comparison writeup: <https://ai-checker.webcoda.com.au/articles/gpt-5-6-sol-claude-code-harness-test-2026>
- Apiyi 5-step guide: <https://help.apiyi.com/en/claudex-cliproxyapi-setup-guide-en.html>
