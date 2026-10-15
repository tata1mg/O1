# Observability

Every run can be traced to [Langfuse](https://langfuse.com): one trace per invocation, with a span for every
stage, every agent, every mechanical check and every human pause. The tracing layer is
`src/rfc_agent/observe.py`; secret scrubbing before export is `src/rfc_agent/privacy.py`.

Tracing has two jobs: **visibility** (see what a run did and where it failed) and **collection** (annotate
runs and build datasets that improve the pipeline).

## Switches

| Setting | Default | Env | Flag (wins) | What it does |
|---|---|---|---|---|
| `LANGFUSE.ENABLED` | `true` | `RFC_AGENT_LANGFUSE` | `--langfuse true\|false` | trace the run |
| `LANGFUSE.VERBOSE` | `false` | `RFC_AGENT_LANGFUSE_VERBOSE` | `--langfuse-verbose true\|false` | also one span per tool call |

Precedence: CLI flag > env var > `config.json`. The flags work on `rfc` and on every stage
(`python -m rfc_agent.draft --run-dir … --langfuse false`).

Other keys, all in the `LANGFUSE` section of `config.json`:

| Key | Purpose |
|---|---|
| `PUBLIC_KEY` / `SECRET_KEY` | Langfuse credentials (or the env vars `LANGFUSE_PUBLIC_KEY` / `LANGFUSE_SECRET_KEY`) |
| `HOST` | Langfuse server URL (empty = Langfuse cloud default) |
| `ENVIRONMENT` | Langfuse environment for this machine's runs, to separate benchmark traffic from operator runs. The `LANGFUSE_TRACING_ENVIRONMENT` env var wins |
| `MASK_SECRETS` | Scrub secrets from every exported span (default `true`; turn off only for local debugging) |

**On with no keys: the run stops before it starts** (exit 2), with a message naming the config file and the
two ways out: add `LANGFUSE.PUBLIC_KEY` / `SECRET_KEY`, or run with `--langfuse false`. Nothing is spent.
Check without running:

```bash
python -c "from rfc_agent import observe; raise SystemExit(observe.preflight())"
```

**Once a run is going, tracing never fails it.** An SDK or network error prints one `[observe]` warning and the
run carries on untraced.

## Trace shape

One trace per invocation. `rfc` is one trace for the whole run; a stage started on its own gets its own
trace. Every trace from one run folder shares one Langfuse **session** (from the run folder's
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

## User feedback

Full design, trace selection and limits: [FEEDBACK.md](FEEDBACK.md).

`/rfc-agent:rfc` asks for a 👍/👎 when it finishes; `/rfc-agent:feedback [--run-dir …]` rates any run later,
after the RFC has been read. Both call `python -m rfc_agent.feedback`, which scores the run's own trace. Every
traced invocation appends its `trace_id` (and the original `--prd` argument) to `<run>/logs/traces.jsonl`; a
whole-run rating uses the latest `rfc` trace, `--stage S` that stage's trace.

| Score | Type | Meaning |
|---|---|---|
| `rfc_feedback` | BOOLEAN | 1 = 👍, 0 = 👎; the user's comment is the score's comment |
| `rfc_feedback_reason` | CATEGORICAL | 👎 only, one score per reason: `wrong_repos`, `missed_flows`, `inaccurate`, `too_shallow`, `too_long`, `too_slow`, `too_costly`, `other` |
| `rfc_feedback_stage` | CATEGORICAL | only with `--stage` |

Score metadata carries `run`, `session_id`, `stage`, `plugin_version` and the run's facts (`run_facts.py`):
`prd`, `jira`, `repos`, `figma_count`, `context_layer_ok`, `rfc_chars`, `review_findings`, `review_unresolved`,
`cost_usd`, `duration_s`. The same facts are stamped on the `rfc-run` observation (`metadata.run_facts`) when a
traced run closes. Each is read from a file the run already wrote; a missing file drops that field.

Rating again replaces the earlier rating (deterministic score id). A run with no recorded trace is scored at
its Langfuse session. Comments go through the same secret scrubber as traces. Feedback goes to whichever
Langfuse project the config's keys and `LANGFUSE.HOST` point at, and respects `LANGFUSE.ENABLED=false`; every
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
Matches become `[REDACTED:<rule>]`.

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
  stay empty.
- **Langfuse graphs individual traces only.** The Sessions page is a chronological list, so a view across a
  whole workstream has to be built on the API.
- **Tool span start times** are approximate (the drain read, up to 0.5 s late); durations come from the CLI
  events' own timestamps.
