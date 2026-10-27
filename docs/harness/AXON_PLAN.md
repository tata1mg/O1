# Plan: running rfc-agent on Axon (Pi) alongside Claude Code

Status: **MVP implemented 2026-10-28** (P0–P4, plus doctor and docs; P5's in-Axon `/rfc` front end is not built).
What was decided along the way, and every deviation from this plan, is in [decisions.md](../../decisions.md).
How to use it: [docs/AXON.md](../AXON.md).

**Goal.** Add Axon as a second, opt-in way to run the rfc-agent pipeline. The Claude Code path stays exactly as it
is: same default, same argv, same plugin, same results. You switch with a flag or a config key, never a code edit.

**Why Axon.** The org has picked Pi as its harness ([Harness Assessment](https://1mgtech.atlassian.net/wiki/spaces/DD/pages/5640028226/Harness+Assessment+-+WIP)),
and Axon is the managed launcher for it. Axon brings org sign-in, the LiteLLM gateway (cost attribution and
model governance), OTEL, session backup and terminal policy. The build plan's
[efforts database](https://1mgtech.atlassian.net/wiki/spaces/DD/database/5640126471) lists those same platform
pieces. Its rows need a browser sign-in, so this plan relies only on its summary and on the assessment's Section 8.

---

## 1. What exists today

| Layer | Claude Code coupling | Notes |
|---|---|---|
| Harness kernel (`src/harness/`) | none | `Engine` protocol; `claude-code`, `pi`, `replay` adapters; `--engine` / `--stage-engine` flags |
| Harness `pi` adapter | n/a | Built for **standalone Pi 0.85.x**. It does not work under Axon unchanged (section 2) |
| rfc pipeline agents: draft, investigate, review, revise, summarize | **hard** | All go through `rfc_agent/investigate/agent.py`, which builds `claude -p` argv and parses Claude stream-json |
| Investigate hook (`investigate/hook.py`) | **hard** | Claude PreToolUse/PostToolUse: write allow-list, write-time citation check, call-budget nudge |
| Context layer (fleetlens MCP) | medium | HTTP MCP with url and headers from config, so it can be carried to any MCP-capable engine |
| Scope: PRD and Jira fetch (`scope/atlassian.py`) | **hard** | Rides the claude.ai Atlassian connector's OAuth through a headless `claude -p` |
| Scope: repo discovery and PRD summary (`scope/context.py`) | **hard** | One-shot `claude -p` calls |
| Memory promote (`memory.py`) | hard | One-shot `claude -p` |
| Front end (`commands/*.md`, `hooks/register.tsx`) | **hard, by design** | Claude Code plugin UI (AskUserQuestion, Monitor, task cards). **Stays unchanged** |

## 2. What we verified on this machine (Axon 0.2.6, Pi 1.4.0)

Tested with tiny one-shot prompts on `gemini-3.8-flash` (fractions of a cent each).

1. **Headless works.** `axon pi --mode json -p ...` streams the same Pi JSON events the existing translator
   parses: a `session` header (v3), `message_*`, `turn_*`, then `agent_end` and finally `agent_settled`. Cost comes
   inside `message_end.usage.cost`.
2. **`--no-extensions` breaks Axon.** It unloads `pi-provider-litellm`, and the run fails with
   `Unknown provider "litellm"`. The current adapter always passes it, so it can't be reused as-is.
3. **Axon ignores `PI_CODING_AGENT_DIR`.** It always uses `~/.axon/pi`, so the adapter's per-run private home has
   no effect (it was never created). Under Axon that's acceptable, because auth comes from Axon's encrypted
   session rather than a user-editable `auth.json`.
4. **Pi 1.4 has built-in MCP** (`--no-mcp`, `pi mcp`, `pi.registerMcpServer()`). The adapter's rule "Pi has no MCP,
   refuse the stage" dates from 0.85 and is wrong for Axon. An extension can register a server **for one session
   only**, without touching `~/.axon/pi/mcp.json`. Tools are named `mcp__<server>__<tool>`, the same scheme
   Claude uses.
5. **The gateway offers no Claude models today.** It lists `axon-auto-mode`, `deepseek/deepseek-v4.1-flash`,
   `gemini-3.8-flash`, `Kimi-k3.1-pro`, and `openai/gpt-6-luna`, `gpt-6.1-sol`, `gpt-6.1-terra`. The model names
   in our prompts and stage config need a mapping, and output quality needs measuring (section 6).
6. **Axon loads its own extensions** from `~/.axon/runtime/current/pi/extensions`: `terminal-permissions`,
   `compaction`, `model-recommender`, `session-backup`, `pi-org-telemetry`, `web-search`, `git-ai`,
   `managed-config` and `axon`. Several of these change agent behaviour (section 3.3).

## 3. Design

### 3.1 One agent seam inside rfc-agent (refactor only, no behaviour change)

Split `investigate/agent.py` into:

- `agent.py`: `AgentSpec`, `AgentResult`, and `run()`, which picks a backend and fans out the `on_event` ticks.
  Its signature is unchanged, so the six stage CLIs don't change.
- `backends/claude.py`: **today's code, moved verbatim** (argv, settings and hook wiring, `_consume`).
- `backends/axon.py`: new. Maps `AgentSpec` to a `harness.engines.EngineRequest`, runs `AxonEngine`, and maps
  the canonical trace back to `AgentResult` plus the same `start` / `tool` / `text` / `end` ticks, so
  progress cards, `activity.log` and Langfuse keep working.

How the engine is chosen, from highest to lowest precedence:

1. CLI `--engine axon` and `--stage-engine review=axon`, the same syntax as the harness.
2. Config `AGENT.ENGINE` and `AGENT.STAGE_ENGINES`.
3. Default `claude-code`.

The one-shot `claude -p` sites (`scope/context.py` repo discovery and PRD summary, `memory.py`) go behind a
small `oneshot(prompt, model, tools)` helper with the same two backends.

**Guarantee for Claude Code:** add a golden-argv test that freezes today's `claude -p` command line and env for
every `AgentSpec` the stages build. The refactor is only allowed to land if that test passes byte for byte.

### 3.2 `harness.engines.axon:AxonEngine`

A subclass of `PiEngine`. It reuses the translator, the guard, argv validation, budget kill, the stderr
`Extension error` scan, and the cost fallback. The rfc pipeline and the harness `testing` workflow both get it.

| Concern | Standalone `pi` (today) | `axon` |
|---|---|---|
| binary | `pi` | `axon pi` (flag `axon.bin`, default `axon`) |
| version pin | Pi `>= 0.85.0`, `KNOWN_FLAGS` for 0.85 | Pi `1.4.x` and Axon `>= 0.2.6`; refresh `KNOWN_FLAGS` from 1.4 `--help` (adds `--no-mcp`, etc.) |
| extensions | `--no-extensions -e harness-guard.js` | **no** `--no-extensions`; Axon's managed set stays loaded, plus `-e harness-guard.js -e rfc-tools.js` |
| hermetic flags | `--no-approve --no-skills --no-prompt-templates --no-themes --no-context-files` | same (keeps repo `AGENTS.md`, `.pi/` and skills out of a stage) |
| home and auth | private `PI_CODING_AGENT_DIR`, API key env | Axon's home and session; doctor runs `axon auth status` |
| provider and model | flag `pi.provider` | `--provider litellm`, model from the map in 3.4 |
| MCP | refused | registered per run by `rfc-tools.js` (3.5); allowed through `--tools` with `mcp__fleetlens__*` entries |
| sessions | `--session-dir <run>/logs/pi-sessions --session-id <uuid>` | same; **to verify** that Axon honours `--session-dir` (P0) |

### 3.3 Axon's managed extensions inside a headless stage

| Extension | Risk in a pipeline stage | Proposal |
|---|---|---|
| `terminal-permissions` | Commands that need approval hang or are blocked without a UI | Stages give agents no bash (`--tools` allowlist), so it is mostly inert. Confirm in P0 |
| `compaction` (Axon turns it on, with its own model) | Silent summarising of evidence makes the citation gates lie, which is why the harness turns compaction off | Turn it off per run if Axon allows (`PI_COMPACTION_ENABLED=0`?); otherwise record a `LOG` event and size budgets so it never triggers |
| `model-recommender` / `axon-auto-mode` | Could override the model we pinned | Always pass an explicit `--model`; doctor fails if the run's model resolves to `axon-auto-mode` |
| `session-backup` | Uploads the **full transcript** (code, PRD text) for every child agent, maybe 20+ per run | Decide with the DeputyDev team: allow it, or get a headless opt-out |
| `web-search` | Lets agents leave the snapshot | Not in the `--tools` allowlist |
| `pi-org-telemetry` (OTEL) | none | Keep; it complements Langfuse |
| harness guard alongside `terminal-permissions` | Two `tool_call` handlers | Verify that a block from either one wins (P0) |

### 3.4 Models

Add a config key `AGENT.AXON_MODELS` that maps our model tiers to gateway IDs, for example
`{"opus": "openai/gpt-6.1-sol", "sonnet": "Kimi-k3.1-pro", "haiku": "gemini-3.8-flash"}`. The backend resolves each
stage's Claude model name to a tier, then to a gateway ID. `effort` maps to `--thinking`, and Pi clamps it to the
model's limits. Doctor checks every mapped ID against `axon pi --list-models`. The defaults are a quality
decision: pick them after the A/B in section 6, not before.

### 3.5 Context layer (fleetlens) and the Investigate hook on Pi

Ship one more extension, `src/harness/engines/pi_ext/rfc-tools.js` (plain ESM, no build step, like the guard). It
reads a per-run 0600 JSON file named by an env var and does three things:

1. **MCP.** Calls `pi.registerMcpServer("fleetlens", {url, headers, exposure: "direct"})`. `direct` keeps the tool
   names `mcp__fleetlens__*` identical to Claude's, so the prompts don't change. Nothing is written to
   `~/.axon/pi/mcp.json`.
2. **Write allow-list.** Expressed as `Containment.writable_roots` plus `INVESTIGATE_WRITE_ALLOW`, and enforced by
   the existing guard through `harness.policy`. The rules aren't copied into JS.
3. **Post-tool feedback.** On `tool_result` it runs `python hook.py post` with a Claude-shaped payload and appends
   the citation-check and wrap-up messages to the tool result. `hook.py` gets a tiny input adapter and stays the
   only implementation. Whether a `tool_result` handler can append to the result in 1.4 is checked in P0. The
   fallback is to send the message as a steering note.

### 3.6 Scope on Axon

1. **Now:** `--prd <file>` already skips Atlassian, so an Axon run works end to end from a local PRD. Repo
   discovery and the PRD summary move onto the `oneshot` backend.
2. **Next:** Atlassian through Pi MCP. Either register the Atlassian remote MCP and do a one-time
   `axon pi mcp login atlassian` (headless runs then reuse the stored token), or use Atlassian through the LiteLLM
   MCP gateway if DeputyDev exposes it (`litellm.mcp.enabled` is already on, but `pi-mcp-sync` last reported
   `failed`). `atlassian.py` keeps its rule that Python writes `raw/` from the tool result. Only the transport
   changes: a Pi RPC session (`--mode rpc`) in place of the stream-json stdin session.

### 3.7 Front end

- **Claude Code:** unchanged. `/rfc-agent:rfc` keeps running `uv run rfc ...`. With `--engine axon` in
  `$ARGUMENTS` the same UI drives an Axon-backed run, since the UI only reads `progress.jsonl` and
  `activity.log`, which both backends write.
- **Terminal:** `uv run rfc --prd ... --engine axon` works with no IDE at all.
- **Inside Axon (later):** a Pi package `rfc-agent-pi` with an extension that registers `/rfc`, starts the Python
  pipeline in the background, shows progress from `progress.jsonl`, and answers pauses through the confirm file
  using Pi's UI prompts (`rpiv-ask-user-question` is already in Axon's managed set). Ship it first with
  `axon pi install`, later through Axon's central skills and extension distribution (build plan, "Central
  management").

### 3.8 Doctor and setup

`rfc-agent:doctor`, `rfc-agent:setup` and `harness doctor` gain an Axon section that runs only when Axon is
selected:

- `axon --version` against the pin
- `axon auth status`
- `axon doctor pi`
- the mapped models exist
- fleetlens is reachable
- (once 3.6.2 lands) Atlassian MCP signed in

Each finding names its fix. Claude checks run exactly as today when the engine is `claude-code`.

## 4. Phases

| Phase | Scope | Exit criterion | Size |
|---|---|---|---|
| **P0: spikes** | Verify on real Axon: `--session-dir` + `--session-id` + resume; guard and `terminal-permissions` together; `registerMcpServer` for fleetlens headless; appending to a tool result; turning off compaction and backup per run; auth refresh across a 60-min run | A short findings note; any "no" changes the design before P1 | 1–2 d |
| **P1: seam** | Split `agent.py` into backends, add the `oneshot` helper, engine selection flags and config, golden-argv test | Full test suite and golden argv pass; a Claude run is byte-identical | 2 d |
| **P2: AxonEngine and simple stages** | `AxonEngine`, `KNOWN_FLAGS` for 1.4, model map, doctor; summarize, review, revise and draft on Axon | `rfc --from draft --engine axon` completes on a recorded run; fake-axon tests (reuse `tests/fixtures/pi/fake_pi.py`) | 3–4 d |
| **P3: Investigate** | `rfc-tools.js` (fleetlens MCP and post-hook), write allow-list via policy | Investigate gate passes on Axon for one real PRD | 3 d |
| **P4: Scope** | Repo discovery and summary on `oneshot`; Atlassian via Pi MCP or gateway | A full `rfc --prd <confluence url> --engine axon` with no Claude installed | 2–3 d |
| **P5: Axon front end** | `rfc-agent-pi` package with `/rfc` and pause UI | Run started and paused from inside `axon` | 3–5 d |

P1 and P2 together are usable on their own: per-stage mixing like `--stage-engine review=axon` lets you try Axon
on cheap stages first.

## 5. Tests

- **Golden argv (Claude):** the regression wall for "Claude Code remains as it is".
- **Translator fixture:** record one real `axon pi --mode json` stream, then assert the canonical events and the
  mapping to `AgentResult` (cost, tool calls, final text, terminal `agent_settled`).
- **Fake-binary stage test:** a stub `axon` that runs the fake Pi, driven through each migrated stage, plus a
  failure path where the stream has no `agent_settled`.
- **Guard and hook tests:** existing `test_engine_pi_guard.py` style; add `rfc-tools.js` under plain `node`.
- **Live smoke (opt-in):** `RFC_AGENT_LIVE_AXON=1 make smoke-axon` on a tiny fixture PRD.

## 6. Quality check before recommending Axon for real RFCs

The gateway has no Claude models, and our prompts were tuned on Claude. Run 2–3 past PRDs on both engines with the
same scope and snapshots, and compare:

- gate pass rate and revise rounds
- citation-check misses
- reviewer findings
- cost and wall time

The harness's `replay` engine and Langfuse traces (tagged with `engine`) make this cheap. Choose the
`AGENT.AXON_MODELS` defaults from the results. A mixed setup, such as tracers on a cheap gateway model and draft on
a strong one, is expected to win.

## 7. Open questions for the DeputyDev / Axon team

1. Can a headless child turn off `session-backup` and Axon's compaction? Is an env var supported and allowed by
   policy?
2. Will the gateway serve a Claude model, or is there a recommended model for long, citation-heavy writing?
3. Is Atlassian (Confluence and Jira read) planned behind the LiteLLM MCP gateway?
4. Can Axon skip its update check and config refresh on a headless launch, so a 40-agent run doesn't pay it 40
   times? (The test run took about 11 s end to end.)
5. Will `rfc-agent-pi` be distributed centrally once Axon's skills and extension distribution ships?
