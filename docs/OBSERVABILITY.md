# Observability

Every run can be traced to [Langfuse](https://langfuse.com): one trace per invocation, with a span for every
stage, every agent, every mechanical check and every human pause. The tracing layer is
`src/rfc_agent/observe.py`; secret scrubbing before export is `src/rfc_agent/privacy.py`.

Tracing has two jobs: **visibility** (see what a run did and where it failed) and **collection** (annotate
runs and build datasets that improve the pipeline).

## Switches

Tracing is required: every run is traced, and there is no flag or setting to turn it off. (Tests and development
use the environment variable `RFC_AGENT_LANGFUSE=false`; it is an internal switch, not part of the user-facing
setup.)

| Setting | Default | Env | Flag (wins) | What it does |
|---|---|---|---|---|
| `LANGFUSE.VERBOSE` | `false` | `RFC_AGENT_LANGFUSE_VERBOSE` | `--langfuse-verbose true\|false` | also one span per tool call |
| `LANGFUSE.PER_CALL` | `false` | `RFC_AGENT_LLM_CALLS` | — | per-visible-assistant-response metrics and generations |

Precedence: CLI flag > env var > `config.json`. The flag works on `rfc` and on every stage
(`python -m rfc_agent.draft --run-dir … --langfuse-verbose true`) and applies to that call only: a resumed run
does not inherit it. The old `--langfuse` flag is refused.

Other keys, all in the `LANGFUSE` section of `config.json`:

| Key | Purpose |
|---|---|
| `PUBLIC_KEY` / `SECRET_KEY` | Langfuse credentials (or the env vars `LANGFUSE_PUBLIC_KEY` / `LANGFUSE_SECRET_KEY`) |
| `HOST` | Langfuse server URL (empty = Langfuse cloud default) |
| `ENVIRONMENT` | Langfuse environment for this machine's runs, to separate benchmark traffic from operator runs. The `LANGFUSE_TRACING_ENVIRONMENT` env var wins |
| `MASK_SECRETS` | Scrub secrets from every exported span (default `true`; turn off only for local debugging) |

**With no keys the run stops before it starts** (exit 2), with a message naming the config file: add
`LANGFUSE.PUBLIC_KEY` / `SECRET_KEY` (or run `/rfc-agent:setup`). Nothing is spent.
Check without running:

```bash
python -c "from rfc_agent import observe; raise SystemExit(observe.preflight())"
```

**Once a run is going, tracing never fails it.** An SDK or network error prints one `[observe]` warning and the
run carries on untraced.

## Trace shape

One trace per invocation. `rfc` is one trace for the whole run; a stage started on its own gets its own
trace. A new run is a new folder, so it is a new session; a run you resume with `--run-dir` stays in its own. Every
trace from one run folder shares one Langfuse **session** (from the run folder's
`identity.json`), and a `--jira` run carries the `ticket:<KEY>` tag.

```
rfc-run                                   chain   input: argv · output: {exit}
├─ stage:scope                             chain   output: {exit}; ERROR + status message when it stopped
│   ├─ agent:atlassian → llm:atlassian    one per connector turn (cost; connector calls as tools if verbose)
│   ├─ agent:repo-discovery               the context-layer discovery agent
│   ├─ agent:prd-summary → llm:prd-summary
│   └─ operator:confirm                   your Scope selection: selected / not_selected
├─ stage:investigate
│   ├─ agent:lead:1a → llm:lead:1a        agent = identity + outcome; generation = model, tokens, cost
│   │   └─ tool:Read, tool:Grep …         VERBOSE only: input + output (2K clip), ERROR on a failed call
│   ├─ agent:T1 · agent:T2 · agent:UI1    parallel tracers / UI readers, all children of the stage
│   ├─ gate:T1, gate:T1:after-fix         the per-agent checks
│   ├─ operator:pause1, operator:pause2   your pause answers (selection, feedback, answers, notes)
│   └─ agent:lead (1c) …, gate:understanding
├─ stage:draft  → agent:lead:write, gate:draft[, agent:lead:gate-fix, gate:draft:after-fix]
├─ stage:review  → one agent per reviewer [, operator:pause3]
└─ stage:revise  → lead fix + reviewer confirm rounds; stage:draft (check-only gate) and stage:summarize nest inside
```

Agent names are `agent:<label>[:<step>]`, where the step is the one the stage passes (`1a`, `write`, `fix`, …).
Each agent's metadata has `session_id`, `seconds`, `turns`, `tool_calls`, `rate_limited`, `resumed`, `effort`
and the path of its stream log. A failed agent (timeout, crash, "claude refused") is `ERROR` with the reason
as the status message.

Worker threads (parallel tracers and reviewers) get the main thread's trace context, so their spans keep the
session and user, and they name their parent span directly.

## Per-response latency and cache diagnostics

Set `LANGFUSE.PER_CALL: true` in the private config (or `RFC_AGENT_LLM_CALLS=true`). Works for Claude Code,
Axon and vanilla Pi, including both local-PRD Scope helpers. With this enabled:

- Each invocation writes `logs/<agent>.llm-calls.jsonl` and `<agent>.llm-metrics.json`. These are content-free
  metrics: engine, actual reported model/provider, call index, tokens, cache reads/writes, token-cache-hit
  ratio, explicit timing boundaries, completion status, and reported per-response cost when available.
- Each visible response is a Langfuse **generation** named `llm:<agent>:call-N`. The agent summary becomes a
  **span**, retaining aggregate usage/cost in metadata instead of billing it again. A cost-only
  `billing:<agent>` generation records any reported invocation cost not already attributed to responses,
  with zero tokens. Scope helpers are not exported a second time as aggregate usage generations.
  Existing run totals remain authoritative.
  In this mode the clipped prompt/result pair remains on `summary:<agent>`; per-response generations
  carry metrics, not a reconstructed conversation history.
- Claude Code additionally receives `--include-partial-messages`; Pi is measured from its raw native stream,
  not its Claude-shaped adapter. Local metrics still work if tracing is disabled or export fails.
- Claude tool results can arrive before the response's final usage. They advance the next-turn timing boundary,
  but streamed responses are finalized only at `message_stop`. Without partial events, the next response or
  invocation end flushes the accumulated assistant blocks; interrupted streams remain marked incomplete.
  Older per-response records may undercount output if they were finalized on a tool result. Tokens can be
  reconciled from the saved final stream usage; original receipt-based timings cannot be reconstructed from
  untimestamped raw events.

### Interpret the clocks honestly

| Field | Meaning |
| --- | --- |
| `observed_turn_s` | Previous tool-result receipt to response end; first response includes process startup. Without intervening tools, starts at previous response end. Includes harness preparation/provider waits; not pure API latency. |
| `tool_and_harness_gap_s` | Previous response end to last tool-result receipt (when present). Includes tool/hook work; not a provider wait. |
| `event_span_s` | Claude: response-stream message-start to message-stop, **after response headers**. Pi: assistant-message lifecycle. These are **different clocks**, not a fair direct cross-engine API-latency comparison. Langfuse generation duration covers this event span. |
| `time_to_first_delta_s` | First observed text/thinking/tool delta after message-start; **not portable provider TTFT**. `null` when absent. |
| `boundary_to_first_delta_s` | First delta relative to the explicit observed-turn boundary; first response includes startup. |
| `first_event_delay_s` / `first_assistant_delay_s` | Process start to first received event / assistant-start event; startup/initialization diagnostics, not isolated network latency. |
| `settled_to_exit_s` | Terminal result/settled event to process cleanup; useful for distinguishing Axon shutdown/session-backup overhead from model work. |

`RFC_AGENT_BENCHMARK_ID` adds a common `benchmark_id` metadata field and benchmark tag to traces.
Engine tags reflect the effective CLI, environment and config selection, including per-stage overrides.
Mixed-engine pipeline traces carry `engine:mixed`, tags for their engines, and a `stage_engines` metadata map.

Token-cache-hit ratio is `cache_read / (uncached_input + cache_read + cache_write)`, NOT the fraction of
requests with a cache hit. Missing values remain `null`, not zero. `reasoning_tokens` is recorded only when
the CLI/provider exposes it; otherwise it stays `null` (output usage may already include reasoning).
Claude per-response billed cost is often
unavailable; no aggregate cost is divided into invented per-call costs. Those response records retain
`cost_usd: null` / `cost_source: unavailable`; their Langfuse generations use an explicit zero accounting cost
so automatic price estimates cannot double-bill the reported invocation remainder on `billing:<agent>`.
Pi's reported response costs are billed directly, with any additional session cost reconciled the same way.

Coverage is **visible assistant responses**, not every HTTP attempt: provider-internal retries, automatic
compaction model calls, and other hidden SDK traffic may not emit response events. Retry/compaction event
counts and `metrics_errors` are retained in each invocation summary; inspect them before comparing. Exact
HTTP latency, gateway queue time, and request-level retry details require provider/gateway tracing.

## User feedback

Full design, trace selection and limits: [FEEDBACK.md](FEEDBACK.md).

`/rfc-agent:rfc` asks for a 👍/👎 when it finishes; `/rfc-agent:feedback [--run-dir …]` rates any run later,
after the RFC has been read. Both call `python -m rfc_agent.feedback`, which scores the run's own trace. Every
traced invocation appends its `trace_id` (and the original `--prd` argument) to `<run>/logs/traces.jsonl`; a
whole-run rating uses the latest `rfc` trace, `--stage S` that stage's trace.

| Score | Type | Meaning |
|---|---|---|
| `rfc_feedback` | BOOLEAN | 1 = 👍, 0 = 👎; the user's comment is the score's comment |
| `rfc_<area>`, `rfc_<area>_issue` | CATEGORICAL | a verdict for each area, and one score per gap picked (coverage, buildability, approach, context layer use; readable summary and rollout and risk when asked) |
| `rfc_suspect_stage` | CATEGORICAL | the stages the picked gaps point at |
| `rfc_run_speed`, `rfc_run_cost`, `rfc_start_ready` | CATEGORICAL | asked only on request; `not_rated` otherwise |
| `rfc_feedback_stage` | CATEGORICAL | only with `--stage` |
| `rfc_feedback_reason` | CATEGORICAL | legacy: the removed 👎 reason list. Old ratings keep it as history; nothing writes it now |

The area scores, their values and the gaps are defined in [FEEDBACK.md](FEEDBACK.md), which is the reference for the
feedback schema.

Score metadata carries `run`, `session_id`, `stage`, `plugin_version` and the run's facts (`run_facts.py`):
`prd`, `jira`, `repos`, `figma_count`, `context_layer_ok`, `rfc_chars`, `review_findings`, `review_unresolved`,
`cost_usd`, `duration_s`. The same facts are stamped on the `rfc-run` observation (`metadata.run_facts`) when a
traced run closes. Each is read from a file the run already wrote; a missing file drops that field.

Rating again replaces the earlier rating (deterministic score id). A run with no recorded trace is scored at
its Langfuse session. Comments go through the same secret scrubber as traces. Feedback goes to whichever
Langfuse project the config's keys and `LANGFUSE.HOST` point at; every
rating is also appended to `<run>/logs/feedback.jsonl`, and a Langfuse failure never loses it or fails the
command.

## Gates and scores

Each mechanical check is an `evaluator` node with two scores on it:

| Score | Type | Gate |
|---|---|---|
| `gate_<kind>_passed` | BOOLEAN | `investigate_tracer`, `investigate_ui`, `investigate_understanding`, `draft` |
| `gate_<kind>_must_fix` | numeric | same; 0 on a pass so charts keep the zeros |

`<kind>` is the check type, not the agent, so every tracer's check adds to one series. The node's output lists
the must-fix items and flags; Draft's also counts failures per check (`failures_by_check`). `WARNING` means
flags only; `ERROR` means must-fix items.

## Privacy: client-side secret redaction

Every span attribute the client exports — tool input and output, prompts, metadata, trace input and output —
passes `privacy.py`'s scrub at the export stage, so raw bytes never leave the machine with a secret in them.
The ruleset is deterministic and derived from gitleaks: AWS, GitHub, GitLab, Slack, Atlassian, OpenAI,
Anthropic, Langfuse and Google keys, JWTs, whole PEM blocks, connection-string credentials, bearer tokens, plus
one `keyword=value` rule gated by entropy and an allowlist. Free-standing entropy scanning is deliberately
absent: it produces false positives on hashes, UUIDs and base64, which is exactly what tool output contains.
Matches become `[REDACTED:<rule>]`. The Langfuse Python SDK v5 JSON-encodes every metadata value on the span
(the string `prod` ships as `"prod"`), so metadata attributes are decoded, scrubbed string by string and
re-encoded; a raw pass could break the encoding.

It is **fail-closed**: a span whose scrub errors ships with sentinel strings, and a hook-level error makes the
SDK drop the export batch.

## Where it hooks in

| Hook | File |
|---|---|
| trace + stage spans, the flags, the no-keys stop | `@observe.entry(...)` on `run.main` and every stage `main` |
| every `claude -p` agent from Investigate to Summarize | `investigate/agent.py` `run()` |
| Scope model calls | `scope/context.py` `discover` (the discovery agent, `agent:repo-discovery`) and `summarise`; `scope/atlassian.py` `_turn` |
| gates | `investigate/cli.py` (agent checks, understanding), `draft/cli.py` |
| pauses | `investigate/pauses.py` `ask` (Investigate and Review), `scope/cli.py` (confirm) |

## Using traces for annotation and datasets

Langfuse annotation queues accept traces, observations and sessions. Dataset items are created with "Add to
dataset" on any observation.

- **The evaluation pair lives on the generations.** `llm:<stage>` carries the prompt as input and the agent's
  result as output; that is what "Add to dataset" copies.
- **Every dataset item needs an evaluation oracle:** an expected output, an assertion list, or a judge rubric.
- **Keep the system's judgment and the human's judgment separate.** The `gate_*` scores are the system's; a
  human annotator's scores are distinct. Their disagreement is the most valuable calibration data this loop
  produces, so never overwrite one with the other.
- **Tool observations are the evidence** a human needs to judge a failure (did the agent actually read the file
  it cited?). Turn `LANGFUSE.VERBOSE` on for runs you intend to annotate.

## Known limitations

- **In-flight traces have dangling nodes.** Spans export on close, so while a run is going its open stage and
  root are not yet in the tree. A run killed mid-way leaves those holes, and its trace-level input and output
  stay empty: since Langfuse v4 they are the root observation's own input and output (SDK v5 has no separate
  trace-io setter), and the root never closes.
- **Langfuse graphs individual traces only.** The Sessions page is a chronological list, so a view across a
  whole workstream has to be built on the API.
- **A slow connection can drop spans.** The Langfuse SDK gives each export 5 seconds by default
  (`LANGFUSE_TIMEOUT`); a batch that times out is dropped, and with it whatever ended in that batch. Seen on a full
  run: Draft's stage span, its agent and its model call (405 s, $3.72) were missing from the trace, while its tool
  spans and gate were there; the run's own files still had them. The run folder is the complete record, and the
  `fact_*` scores are computed from it. If spans go missing, run with `LANGFUSE_TIMEOUT=30` in the environment.
  Each failed or dropped span export (the exporter's own errors only) is written to `logs/activity.log` as
  `[langfuse] export failed: …` and counted as `fact_langfuse_export_failures`; if that stays above zero across
  runs, a retry or a rebuild-from-files step is due. The count covers failures up to the flush that runs just before
  the run's numbers are collected. The last send, which carries the numbers and the run summary, cannot report its
  own failure, so a failure of that final send is not counted.
- **Tool span start times** are approximate (the drain read, up to 0.5 s late); durations come from the CLI
  events' own timestamps.
