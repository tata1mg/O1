# Decisions: rfc-agent on Axon

A running log of the decisions taken while adding Axon (the org's managed Pi) as a second engine for the
rfc-agent pipeline, 2026-10-27/28. The plan this implements is [docs/harness/AXON_PLAN.md](docs/harness/AXON_PLAN.md).
Each entry says what was decided, why, and what would change it.

Environment measured: Axon 0.2.6, managed Pi 1.4.0, gateway provider `litellm`, macOS arm64.

---

## D-01 Claude Code stays the default, untouched

**Decision.** `claude-code` remains the default engine everywhere. Axon is opt-in through `--engine axon`,
`--stage-engine <stage>=axon`, or config `AGENT.ENGINE` / `AGENT.STAGE_ENGINES`. The Claude code path in
`investigate/agent.py` is unchanged: the only edit is an early branch that is taken when the stage's engine is
`axon`.

**Why.** The ask was that current Claude Code behaviour stays exactly as it is. A branch in front of unchanged
code is the smallest change that guarantees that, and the full test suite (1159 tests) passes unchanged.

## D-02 One seam: `investigate.agent.run`

**Decision.** Axon support plugs in at `rfc_agent.investigate.agent.run`, the function every stage already uses
to start an agent (lead, tracers, UI reader, drafter, reviewers, fixers, summarizer). The six stage CLIs needed
no change beyond accepting the engine flags.

**Why.** It's the one chokepoint. Routing through the harness `Engine` protocol instead would have meant
rewriting the stage CLIs' live progress and Langfuse plumbing.

## D-03 Pi's stream is converted into Claude stream-json, not the other way round

**Decision.** `rfc_agent/axon_agent.py` converts each Pi `--mode json` event into the Claude stream-json shape
(`PiToClaude`). It writes the converted stream to the same `logs/<stage>.<label>.<step>.stream.jsonl` file the
Claude path writes, and keeps Pi's raw stream beside it as `.pi.jsonl`.

**Why.** Everything downstream reads Claude-shaped events: live progress ticks (`agent._consume`), Langfuse
tool spans, `_code_reads` (which counts `Read`/`Grep`/`Glob` calls on `_snap`), and the ledger. Converting at
the edge means none of them changes. Pi tool names are mapped back to Claude's (`read`→`Read`,
`find`→`Glob`, …) and `path` becomes an absolute `file_path`.

## D-04 Keep Axon's extensions; add ours with `-e`

**Decision.** Axon runs never pass `--no-extensions`. The harness guard (`harness-guard.js`) and a new
`rfc-tools.js` are added with `-e` next to Axon's managed set.

**Why.** Measured: `--no-extensions` unloads Axon's LiteLLM provider and the run fails with
`Unknown provider "litellm"`. The plain `pi` adapter's hermetic approach can't be used under Axon.

## D-05 No private Pi home; no `PI_OFFLINE`

**Decision.** The Axon adapter doesn't set `PI_CODING_AGENT_DIR` or `PI_OFFLINE`.

**Why.** Measured: Axon ignores a `PI_CODING_AGENT_DIR` override and always uses `~/.axon/pi`. Auth is Axon's
encrypted session, not a user-editable `auth.json`, so the private home's protection isn't needed. Gateway
model discovery is a startup network operation, which `PI_OFFLINE` would block.

## D-06 The prompt goes on stdin; stdin is always closed

**Decision.** The prompt is written to Pi's stdin, then stdin is closed, exactly like the Claude path.

**Why.** Measured: Pi prepends piped stdin to the first prompt, so this avoids argv size limits on large
prompts. An inherited, never-closed stdin hangs Pi forever (seen in the first spike), so closing it is
mandatory.

## D-07 Default model mapping (Claude tier → gateway model)

**Decision.**

| Stage model (Claude) | Gateway model | Why |
|---|---|---|
| `opus[1m]` (Investigate lead, Draft, Review, Revise) | `Kimi-k3.1-pro` | Premium price tier and a 1M-token window; the lead is resumed across 1a → 1c → Draft |
| `opus` (UI reader) | `openai/gpt-6.1-sol` | Premium tier, 272K window is enough for one-shot agents |
| `sonnet`, `sonnet[1m]` (tracers, summarize, scope discovery) | `openai/gpt-6.1-terra` | Mid tier, 922K window |
| `haiku` (scope PRD summary) | `gemini-3.8-flash` | Cheap and fast, 1M window |

Measured prices per 1M tokens (input/output): sol $2/$10, Kimi-k3.1-pro $2/$10, terra $1/$5, gemini-3.8-flash
$0.75/$3.75, deepseek-v4.1-flash $0.30/$1.20, gpt-6-luna $0.10/$0.50. The gateway lists no Claude models.

Overridable in config `AGENT.AXON.MODELS` (by tier, or by exact Claude id), or for the harness with
`--set axon.models=…`. A non-Claude model id passes through unchanged.

**Would change if** the A/B in AXON_PLAN §6 shows a better pick, or the gateway adds Claude.

## D-08 Effort maps one-to-one to `--thinking`

**Decision.** `low|medium|high|xhigh|max` pass through as Pi thinking levels. Pi clamps them to what each model
supports.

## D-09 fleetlens is registered per session by an extension

**Decision.** `rfc-tools.js` calls `pi.registerMcpServer("fleetlens", {url, headers, exposure: "direct"})`
from a 0600 per-agent config file. The operator's `~/.axon/pi/mcp.json` is never touched. The read-only tools
are granted by name through `--tools` and through a new `granted_tools` list the guard honours.

**Why.** Measured: registration works headless, and `direct` exposure keeps the tool names `mcp__fleetlens__*`
identical to Claude's, so prompts don't change. Before `granted_tools`, the guard default-denied every MCP call
("no policy rule"), which a spike caught.

## D-10 The Investigate hook is reused, not rewritten

**Decision.** `rfc-tools.js` runs the existing `investigate/hook.py` with a Claude-shaped payload. `pre` runs on
`tool_call` for write/edit (exit 2 blocks with the hook's message). `post` runs on `tool_result`, and its
`additionalContext` is appended to the result the model reads.

**Why.** One implementation of the write allow-list, the write-time citation check and the call-budget nudge.
Measured: a write outside the allow-list was blocked with the hook's exact message.

## D-11 Axon's threshold compaction is cancelled in pipeline agents

**Decision.** `rfc-tools.js` cancels `session_before_compact` when the reason is `threshold`. Overflow recovery
is left alone. Config `AGENT.AXON.THRESHOLD_COMPACTION: true` (harness: `axon.threshold_compaction`) turns it
back on.

**Why.** Axon enables early compaction with its own summary model. Compaction is lossy and makes the citation
gates lie (the same reason the harness turns it off for plain Pi). `PI_COMPACTION_ENABLED=false` was not used
because Axon says backend values are applied ahead of local ones at startup.

## D-12 Cost: the session file for fresh agents, the stream sum for resumed ones

**Decision.** A fresh agent's cost is summed from its Pi session file. A resumed agent's cost is the sum of
this invocation's assistant messages in the stream.

**Why.** The session file is authoritative but holds every earlier turn of a resumed session, so it would
re-bill them.

## D-13 Resume of a missing session fails like Claude does

**Decision.** If `--session` has no file to continue, the agent returns an error without starting. Callers
already rerun cold with a "fallback intro" when a resume fails on Claude, so they behave the same.

## D-14 Scope on Axon: discovery and summary move; the Atlassian fetch stays on Claude

**Decision.** Under `--engine axon`, Scope's repo-discovery agent (fleetlens) and PRD-summary call run on
Axon. Fetching the PRD and Jira from Confluence still uses the claude.ai Atlassian connector through
`claude -p`. With no Claude installed, use `--prd <local file>`.

**Why.** Atlassian needs OAuth. Under Pi that means a one-time `axon pi mcp login` against Atlassian's remote
MCP, or Atlassian behind the LiteLLM MCP gateway (Axon's `pi-mcp-sync` reported `failed` on this machine).
Neither could be verified tonight without signing in interactively. The fetch is byte-exact Python-side
anyway: the model only calls tools, so which engine makes the call doesn't affect RFC quality.

**Would change when** Atlassian is available through the gateway or after a one-time Pi MCP login.

## D-15 Session backup is left on

**Decision.** Axon's `session-backup` extension stays loaded for pipeline agents.

**Why.** It's org-managed infrastructure built for transcripts, and Axon offers no supported headless opt-out
(only `PI_AXON_BACKUP_HOME` and the service origin are read). This is listed as an open question for the
DeputyDev team: a run starts 10–40 agents, and each is a backed-up session.

## D-16 Legacy `memory.promote` stays on Claude

**Decision.** `rfc_agent/memory.py` (a post-run learning pass of the older ingest/extract/ground stages) is not
moved. It isn't on the current Scope → Summarize path.

## D-17 No in-Axon `/rfc` front end yet (plan P5)

**Decision.** There is no Pi extension that adds `/rfc` inside an `axon` session. You start an Axon-backed run
from the terminal (`uv run rfc … --engine axon`) or from Claude Code (`/rfc-agent:rfc … --engine axon`, the same
progress cards and pause UI).

**Why.** The engine choice is independent of the front end. The UI only reads `progress.jsonl`,
`activity.log` and the confirm files, which both engines write the same way. Building a Pi TUI front end needs
interactive testing in a terminal session, which an unattended overnight build can't do. It's the next
follow-up.

## D-18 The engine choice is per invocation, recorded in `run.json`

**Decision.** `--engine` / `--stage-engine` apply to the command they're given to. They're exported as
`RFC_AGENT_ENGINE` / `RFC_AGENT_STAGE_ENGINES` so every in-process stage (including Summarize, which Revise
starts) sees them. They're not stored in the run folder, so rerunning a run without the flag uses the config
default again. `logs/run.json` gets an `engines` map of what each stage used.

**Why.** It matches how `--preset`, `--langfuse` and the other run flags behave. A sticky per-run engine would
surprise someone who reruns a stopped run on Claude to compare.

## D-19 Containment on Axon: guard + hook, not either alone

**Decision.** Both run on every Axon agent. The harness guard (writes confined to the run dir, bash classified,
unknown tools denied, granted MCP tools allowed) and `hook.py pre` (the agent's exact write allow-list).

**Why.** That's the same layering as on Claude, where `--permission-mode acceptEdits` and `--disallowed-tools`
sit under the hook. The guard alone is too coarse (the whole run dir); the hook alone would let an
unrecognised tool through.

## D-20 Ruled out: one argv list shared with the harness adapter

**Decision.** rfc-agent's Axon backend builds its own argv (`axon_agent.build_argv`) instead of calling
`AxonEngine.run`. It shares the constants and helpers: hermetic flags, the guard path, the flag validator, the
process-group reader and killer, and the session-file lookup.

**Why.** `AxonEngine.run` writes the harness's canonical trace, but the rfc stages need the Claude-shaped stream
and live ticks while the agent runs. Wrapping the engine would mean tailing its trace from a second thread.
Sharing the helpers keeps one implementation of every risky piece (flag validation, killing the process group,
the read deadline).
