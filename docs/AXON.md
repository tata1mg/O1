# Running rfc-agent on Axon

Axon is the org's managed launcher for Pi, the coding agent the org picked as its harness. rfc-agent can run its
agents on Axon instead of `claude -p`. Claude Code stays the default; Axon is opt-in, per run or per stage.

Same pipeline, same pauses, same outputs (`out/rfc.md`, `out/rfc.readable.md`, `review/`), same progress cards
and Langfuse traces. What changes is which agent runtime and which models do the work.

## 1. Set up (once)

1. Install Axon and sign in. See the [Axon user guide](https://1mgtech.atlassian.net/wiki/spaces/DD/pages/5666996423).

   ```bash
   curl --proto '=https' --tlsv1.2 -fsSL https://deputydev.ai/axon/install.sh | sh
   ```

   ```bash
   axon
   ```

   Choose *Login from browser*, sign in, then quit the session.
2. Check everything rfc-agent needs:

   ```bash
   uv run python -m rfc_agent.axon_agent doctor
   ```

   It prints `axon: READY`, or one `FAIL` line per problem with its fix. It checks that Axon is installed, the
   sign-in is valid, the managed Pi is at least 1.4.0, every mapped model exists on the gateway, and fleetlens
   answers (VPN on).

## 2. Run

```bash
uv run rfc --prd <confluence url | page id | file.md> --engine axon
```

```bash
uv run rfc --run-dir <run> --from review --engine axon
```

```bash
uv run rfc --prd <…> --stage-engine review=axon
```

The first runs everything on Axon. The second redoes one stage onward on Axon. The third keeps everything on
Claude except Review. From Claude Code, the plugin commands take the same flags, for example
`/rfc-agent:rfc --prd … --engine axon`.

Every stage CLI accepts the flags too (`python -m rfc_agent.review --run-dir … --engine axon`). To make Axon your
default, set it in `~/.config/rfc-agent/config.json`:

```json
{"AGENT": {"ENGINE": "axon", "STAGE_ENGINES": {}, "AXON": {"MODELS": {}}}}
```

Precedence: `--stage-engine` > `--engine` > `AGENT.STAGE_ENGINES` > `AGENT.ENGINE` > `claude-code`.
`logs/run.json` records which engine each stage used.

## 3. Models

The stages name Claude models. On Axon each is mapped to a gateway model by tier:

| Stage model | Used by | Gateway model |
|---|---|---|
| `claude-opus-5-5[1m]` | Investigate lead, Draft, Review, Revise | `Kimi-k3.1-pro` |
| `claude-opus-5-5` | Investigate UI reader | `openai/gpt-6.1-sol` |
| `claude-sonnet-5-5` | tracers, Summarize, Scope discovery | `openai/gpt-6.1-terra` |
| `claude-haiku-4-5…` | Scope PRD summary | `gemini-3.8-flash` |

Print the current mapping:

```bash
uv run python -m rfc_agent.axon_agent models
```

Override by tier or by exact id in config:

```json
{"AGENT": {"AXON": {"MODELS": {"opus[1m]": "openai/gpt-6.1-terra", "haiku": "deepseek/deepseek-v4.1-flash"}}}}
```

Effort maps to Pi's `--thinking` level of the same name.

## 4. What is the same, and what isn't

The same on both engines:

- One prompt per agent, sent on stdin.
- Built-in tools limited to what the stage allows: no shell, no web.
- fleetlens read-only tools for the agents that use the context layer.
- Writes confined to each agent's own files. It's the same `investigate/hook.py`, so the same messages.
- The write-time citation check and the "wrap up" nudge at the call budget.
- Resume across steps (Investigate lead → Draft, Revise fix rounds).
- Logs: `logs/<stage>.<label>.<step>.stream.jsonl` is written in the Claude stream format on both engines.
  Axon adds the raw Pi stream (`.pi.jsonl`) and Pi sessions under `logs/pi-sessions/`.

Different on Axon:

- **The Confluence/Jira fetch in Scope still uses Claude's Atlassian connector.** A Confluence PRD on
  `--engine axon` still needs `claude` installed with Atlassian connected. A local `--prd file.md` needs neither.
- **A run started on Claude can't resume its Claude sessions on Axon.** The pipeline then starts the agent cold,
  exactly as when a Claude session has expired.
- **Axon's early (threshold) compaction is turned off for pipeline agents**, because it summarises evidence away.
  Set `AGENT.AXON.THRESHOLD_COMPACTION: true` to allow it.
- **Axon backs up each agent session to DeputyDev**, as it does for interactive use.
- **Quality and cost differ.** The prompts were tuned on Claude. Compare a few runs before relying on it.

Design notes and the reasoning behind each choice are in [decisions.md](../decisions.md) and
[docs/harness/AXON_PLAN.md](harness/AXON_PLAN.md).
