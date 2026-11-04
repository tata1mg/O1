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

## D-14 Scope on Axon: discovery and summary move; ~~the Atlassian fetch stays on Claude~~ (fetch: see D-22)

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

## D-17 ~~No in-Axon `/rfc` front end yet~~ — superseded by D-21

**Decision (original).** No Pi extension adding `/rfc` inside an `axon` session; runs started from the terminal
or from Claude Code. Deferred because a Pi front end needed interactive testing. D-21 builds it.

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

---

## D-21 `/rfc` inside Axon: a Pi extension that drives the CLI (plan P5)

**Decision.** `src/rfc_agent/axon_ext/rfc-command.js` registers `/rfc` in Axon. It does what
`commands/rfc.md` tells Claude Code to do, in code: it runs the Axon doctor as a preflight, starts
`uv run rfc … --run-dir <run> --confirm-file` as a detached child, and tails `run.out`, `progress.jsonl` and
`activity.log` into a widget above the editor plus a status entry. Each `WAITING FOR …` pause is asked with Pi
dialogs, and the answers file is written in the format the CLI already reads. When the run finishes, the CLI's
closing lines are posted into the conversation. `/rfc` without `--engine`/`--stage-engine` adds `--engine axon`.
`/rfc attach <run>` follows a run again after Axon restarts, and `/rfc doctor` runs the doctor.

`python -m rfc_agent.axon_agent install` writes a two-line stub, `~/.axon/pi/extensions/rfc-agent.js` (or under
`PI_CODING_AGENT_DIR`). The stub imports the file from this checkout and records the project path and `uv`, so
a `git pull` updates `/rfc` without reinstalling. `uninstall` removes it.

**Why this shape.**
- **A command, not a skill.** A Pi skill only adds instructions for the model. The model would then need a
  background shell and a wake-up on each pause, and Pi has neither. A command needs no model at all: the
  pipeline is unchanged, and the extension is just its progress view and pause UI. So `/rfc`, not `/skill:rfc`.
- **Pause UI on `ui.select`/`ui.input`.** These work in the TUI and over RPC. Pi has no multi-select dialog, so
  a checklist is a toggle loop: pick a row to flip it, then `✔ Done`. Escape keeps the proposal's ticks.
  `ui.custom` would look nicer but is TUI-only and untestable over RPC.
- **Detached child, pid file.** A 30–60 min run shouldn't die with the Axon session. `/rfc attach` uses the pid
  file to tell a live run from a finished one. A proposal left by a dead process is never asked.
- **`PI_*` variables are stripped from the child's environment.** They belong to the running session. The
  pipeline's own `axon pi` children set up their own (same reasoning as D-05).
- **No feedback step yet.** The 👍/👎 rating `/rfc-agent:rfc` asks for at the end is not ported. Run
  `/rfc-agent:feedback` from Claude Code, or skip it.

**Verified.** Node tests (`tests/fixtures/axon/rfc_command_cases.mjs` + `fake_uv.py`) drive the command
end to end: preflight failure, launch argv, all three pause kinds, the answer formats and the finish. In real
Axon (0.2.6 / Pi 1.4.0): `/rfc help` and `/rfc doctor` (→ `axon: READY`) run in print mode, and in RPC mode
`/rfc --prd …` against the fake pipeline got every dialog, wrote all three answers files and posted the
closing summary. Still to try: a real PRD driven from the interactive TUI.

## D-22 Scope's Confluence/Jira fetch runs on Axon's own Atlassian sign-in

**Decision.** When Scope runs on Axon, `scope/atlassian_axon.py` (`AxonConnectorSession`) fetches the PRD, its
descendants and Jira, in place of Claude's `ConnectorSession`. The interface is the same, so `scope/cli.py` only
picks which one `open_session` builds. The server is the operator's own `atlassian` entry in
`~/.axon/pi/mcp.json`, added with `axon pi mcp add` and signed in once with `axon pi mcp login` (Pi keeps the OAuth
token). `AGENT.AXON.ATLASSIAN_SERVER` names it if it's called something else. With this, `--engine axon` needs no
`claude` at all.

**How.**
- **Each turn is one short `axon pi -p` process with no session**, running the haiku-tier gateway model
  (`gemini-3.8-flash`). Pi waits for direct-exposure servers before the first model call, so there's no warm-up
  turn or `pending` polling as on Claude.
- **Exposure is overridden only for this process.** The user-level server is `codemode` by default, which hides
  the tools behind scripts. The turn runs in `logs/atlassian-axon/`, whose `.pi/mcp.json` changes only
  `exposure` to `direct` (Pi's documented override of a user-level server), loaded with `--approve`. The operator's
  config is never edited. Registering the server from an extension wouldn't work: a `mcp.json` entry with the
  same name wins, and a different name means a separate OAuth sign-in.
- **`--tools` lists exactly the four fetch tools.** No read, write or shell. Otherwise it's Pi's hermetic set,
  minus `--no-approve`.
- **Byte-exact despite Pi's 20 KB cut.** Pi trims tool results over 20 KB before the model sees them, and the
  stream carries the cut text. The full result goes to a file named in `details.fullOutputPath`, and that file
  is what gets parsed.
- **Errors.** No resources result, then `axon pi mcp list --json` explains why: a missing server or a missing
  sign-in fails at once and names the fix; a connected but silent server is retried with a fresh process, as on
  Claude.
- **Doctor.** The Axon doctor checks the server is `connected`. `--no-atlassian` skips the check, and `/rfc`
  passes it when the PRD is a local file and there's no `--jira`.

**Verified (2026-10-29).** Live: connect plus fetch of PRD 5672665111, its descendants, and a 25 KB page
(5640028226, cut to 20 KB in the stream) cost $0.015 in about 20 s. The PRD body is byte-identical to the one
Claude's connector fetched. The 25 KB page came back whole. Unit tests stub `axon` for the turn parsing, the
truncation file, the missing / signed-out / silent server and the retry.

## D-23 Read-only tools in the interactive Axon session, not a bash allow-list

**Problem.** Asking an Axon session about a run ("status?") made the agent run commands like
`cat logs/status.json && tail … 2>/dev/null` or `ps -p $(cat …pid)`. Axon's managed `terminal-permissions`
extension asks the user about any bash command with `$`, a redirect, a glob or a wrapper, and about whatever the
org's backend lists. One session timed out an approval.

**Decision.** The `/rfc` extension registers `rfc_status`, a read-only tool that reports a run's process, any
waiting pause, `status.json`, the tails of `run.out` and `activity.log`, and which outputs exist. On
`session_start` it also adds Pi's built-in `read`, `grep`, `find` and `ls` to the active tools, using
`pi.setActiveTools`. None of these is bash, so the terminal policy never sees them. That's the footing
Read/Grep/Glob have in Claude Code. The stages need nothing new: their logs show no bash calls on Axon, and the
harness guard already mirrors Claude's allow/deny lists there (D-19).

**Ruled out: auto-approving read-only bash, as `approve_readonly.py` does for Claude stages.** In Axon that
decision belongs to the org's terminal policy (its required-approval list, and whether `/yolo` is allowed), set
by Axon's backend. An rfc-agent extension that approved commands on its behalf would override the org's control.
Bash prompts stay Axon's.

**Verified (2026-11-02).** Loaded into Axon's Pi 1.4.0 runtime (RPC mode, private agent dir): `rfc_status` is
registered and active with `readOnlyHint`, and `grep`/`find`/`ls` join the active set next to `bash`. A live
session in Axon wasn't run because the Axon login had expired. The node tests cover the snapshot (finished run,
live run with a waiting pause, non-run folder) and the active-set change.

## D-24 Sharing `/rfc`: the repo is a Pi package

**Decision.** A root `package.json` with a `pi` manifest makes the repo a Pi package, so anyone installs `/rfc`
with `axon pi install git:github.com/tata1mg/O1@<tag>`. Its entry, `axon_ext/package-entry.js`, takes the
project from its own location (the clone is a full checkout, so `uv run --project <clone>` runs the pipeline
from it). It finds `uv` from `RFC_AGENT_UV`, then PATH, then the installers' usual folders. The checkout stub
(`axon_agent install`) stays for development. Both register `/rfc`, so a machine uses one of them.

**Why a package and not the stub for everyone.** The stub needs a checkout and hard-codes its path. A package
is versioned by the ref it pins, installs in one command, and is what Pi and Axon manage. Axon removes only
the packages it installed itself and keeps a user's own (seen in the launcher's reconcile code, Axon 1.0.0).

**Verified (2026-11-02).** Axon's Pi 1.4.0 runtime, private agent dir, `-e <repo dir>`: Pi read the manifest,
registered `/rfc` from `package-entry.js`, and `/rfc help` answered, so the project path resolved. `rfc_status`
and the read-only tools came up as in D-23. Not yet tried: an `axon pi install git:…` from GitHub, because the
branch with this isn't pushed.

## Live verification (2026-10-28, Axon 0.2.6 / Pi 1.4.0)

All runs used copies of the finished Claude run `prd-5672665111` (Upload Report optimisation), with Langfuse off.

| Stage on Axon | Result | Time | Cost (gateway) |
|---|---|---|---|
| Scope (local PRD, `--no-figma --yes`) | Discovery made 42 fleetlens calls and found the same 4 repos as the Claude run; the summary found `consumer_web` | 1m51s | $0.12 |
| Investigate (`--fresh --yes`) | 6 flows, 30 REQs. Plan check sent it back once and the lead resumed its Axon session. 6 parallel tracers, all citation checks clean. 40+ fleetlens calls over MCP | 21m06s | $2.78 |
| Draft (`--fresh`) | The Claude lead session couldn't be resumed, so it fell back to a fresh lead (D-13); one "not finished" retry. Gate clean: 82 citations, 8 CHANGEs, 6 diagrams | 20m21s | $1.36 |
| Review (`--fresh`) | 7 reviewers in parallel: 19 blocker, 25 major, 5 minor | 13m41s | $5.65 |
| Revise (`--fresh`) + Summarize | Round 1: 43/44 resolved, 4 new blockers. Round 2: 3 resolved. 4 left unresolved and published. Gate clean: 117 citations | 23m15s + 1m39s | $7.24 + $0.08 |

**Observations for the quality A/B (AXON_PLAN §6):**

- The Axon RFC is smaller than the Claude one for the same PRD: 76.7 KB vs 116.3 KB, 8 CHANGEs vs 12, 117
  citations vs 178.
- Both ended with 4 unresolved review findings. Claude's whole run cost $148.63; the Axon stages above total
  about $17. The two RFCs haven't been compared by a human yet.
- The "N rate-limit event(s)" count is noisy on both engines. It counts the text `rate_limit` anywhere in the
  stream, including source code the agents read. It is not an Axon problem.
