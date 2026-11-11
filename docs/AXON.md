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
2. Connect Axon to Atlassian, for Confluence and Jira PRDs:

   ```bash
   axon pi mcp add atlassian --url https://mcp.atlassian.com/v1/mcp
   ```

   ```bash
   axon pi mcp login atlassian
   ```

   The second command opens Atlassian's consent page in the browser. Pi keeps the token. A local `--prd file.md`
   doesn't need this. If your server has another name, set `AGENT.AXON.ATLASSIAN_SERVER`.
3. Check everything rfc-agent needs:

   ```bash
   uv run python -m rfc_agent.axon_agent doctor
   ```

   It prints `axon: READY`, or one `FAIL` line per problem with its fix. It checks that Axon is installed, the
   sign-in is valid, the managed Pi is at least 1.4.0, every mapped model exists on the gateway, fleetlens
   answers (VPN on), and Axon's Atlassian server is signed in (`--no-atlassian` skips that check).

## 2. Run from inside Axon (`/rfc`)

Setting up a new machine from scratch, with no Claude Code? Follow [AXON_INSTALL.md](AXON_INSTALL.md).

Install the command once, one of two ways:

- **As a Pi package (to share it).** No checkout needed. Pi clones the repo, and the clone is also where the
  pipeline runs from:

  ```bash
  axon pi install git:github.com/tata1mg/O1@<tag>
  ```

  Pin a tag (or a commit) so everyone runs the same version. Move to a new one with `axon pi remove` and an
  install at the new tag. You need GitHub access to the repo, `uv` installed, and `~/.config/rfc-agent/config.json`
  ([INSTALL.md](../INSTALL.md)). Steps 1–2 above still apply. Check with `/rfc doctor` inside Axon. If Axon can't
  find `uv`, set `RFC_AGENT_UV` to its path.
- **From your own checkout (for development).** Edits to the checkout show up after `/reload`:

  ```bash
  uv run python -m rfc_agent.axon_agent install
  ```

Use one or the other: both register `/rfc`. `axon pi remove git:github.com/tata1mg/O1` drops the package, and
`… axon_agent uninstall` drops the checkout stub.

Then, in any `axon` session (`/reload` an open one first):

```
/rfc --prd <confluence url | page id | file.md>
```

- `/rfc` checks Axon first (`/rfc doctor` runs that check on its own), then starts the pipeline in the
  background. It takes 30–60 minutes.
- Progress shows above the editor: stage, milestones, agents started.
- Each pause (Scope's selection, Investigate's pauses, Review triage with `--ask-review`) opens as a question in
  the session. To answer a list, pick rows to tick or untick them, then `✔ Done`.
- When it finishes, the time, cost and RFC paths are posted into the conversation, so you can ask Axon about the
  RFC right there.
- Every rfc flag works (`--jira`, `--repos`, `--ask`, `--ask-review`, `--yes`, `--from`, `--run-dir`, …). The
  agents run on Axon unless you pass `--engine` or `--stage-engine` yourself.
- The run keeps going if you quit Axon. `/rfc attach <run folder>` follows it again and asks any pause that is
  still waiting.
- Ask Axon about the run ("status?", "what is it doing?") without approval prompts. The command also gives Axon an
  `rfc_status` tool (process, waiting pause, `status.json`, log tails, outputs) and turns on Pi's read-only
  `read`/`grep`/`find`/`ls`, so the agent doesn't need bash for this. Bash itself stays under Axon's terminal
  policy: a command with `$`, a redirect or a glob still asks, unless your org allows `/yolo`
  (`/terminal-permissions` shows what it allows).

With the checkout install, the stub points at your checkout, so pulling new code updates `/rfc` without a
reinstall.

## 3. Run from a terminal or Claude Code

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

## 4. Models

The stages name Claude models. On Axon each is mapped to a gateway model by tier. The gateway serves
Opus 5.5 and Sonnet 5.5, so those stages run on Claude; it has no Haiku:

| Stage model | Used by | Gateway model |
|---|---|---|
| `claude-opus-5-5[1m]` | Investigate lead, Draft, Review, Revise | `anthropic/claude-opus-5.5` |
| `claude-opus-5-5` | Investigate UI reader | `anthropic/claude-opus-5.5` |
| `claude-sonnet-5-5` | tracers, Summarize, Scope discovery | `anthropic/claude-sonnet-5.5` |
| `claude-haiku-4-5…` | Scope PRD summary, Atlassian fetch | `gemini-3.8-flash` |

Print the current mapping:

```bash
uv run python -m rfc_agent.axon_agent models
```

Override by tier or by exact id in config:

```json
{"AGENT": {"AXON": {"MODELS": {"opus[1m]": "openai/gpt-6.1-terra", "haiku": "deepseek/deepseek-v4.1-flash"}}}}
```

Effort maps to Pi's `--thinking` level of the same name.

## 5. What is the same, and what isn't

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

- **Scope fetches Confluence and Jira through Axon's own Atlassian sign-in** (set up in §1). Claude isn't
  involved, and the PRD is saved exactly as Atlassian returns it, the same as on Claude. A Scope that runs on
  Claude (for example `--stage-engine review=axon`) still uses Claude's connector.
- **A run started on Claude can't resume its Claude sessions on Axon.** The pipeline then starts the agent cold,
  exactly as when a Claude session has expired.
- **Axon's early (threshold) compaction is turned off for pipeline agents**, because it summarises evidence away.
  Set `AGENT.AXON.THRESHOLD_COMPACTION: true` to allow it.
- **Axon backs up each agent session to DeputyDev**, as it does for interactive use.
- **Quality and cost differ.** The prompts were tuned on Claude. Compare a few runs before relying on it.

Design notes and the reasoning behind each choice are in [decisions.md](../decisions.md).
