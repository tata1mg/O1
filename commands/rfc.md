---
description: Generate a tech RFC from a PRD — the whole pipeline (Scope → Summarize) in one command; progress and every pause shown here
argument-hint: --prd <confluence-url | page-id | file> [--jira KEY] [--repos a,b] [--no-figma] [--preset balanced|fast] [--ask] [--ask-review] [--langfuse true|false] [--langfuse-verbose true|false]
---

Run the whole rfc-agent pipeline for: `$ARGUMENTS`

It runs in the background (typically 30–60 min — say so once at the start). YOU are its progress view and its pause
UI: relay milestones in short lines, open one task card per agent (section 2), and at each pause show the
proposal as AskUserQuestion selections and write the answers back. Never answer a pause yourself. No browser pane.

## 1. Start

```
PROJ="${CLAUDE_PLUGIN_ROOT}"
RUN="$(uv run --project "$PROJ" python -m rfc_agent.scope run-dir $ARGUMENTS)"     # or the --run-dir given
mkdir -p "$RUN/logs"
TASK="rfc pipeline · <run folder name>" uv run --project "$PROJ" rfc $ARGUMENTS --run-dir "$RUN" --confirm-file > "$RUN/logs/run.out" 2>&1
```

Run the last line with `run_in_background`. The terminal's task list (`/tasks`) names a background shell by the
**start of its command**, so send it as one command that begins with the `TASK="…"` label: write `$PROJ` and
`$RUN` out as literal paths, and put no `cd`, `PROJ=` or other assignment in front of it.

Preflight first (seconds): `claude mcp list | grep -iE "claude\.ai Atlassian|^atlassian"` —
either line (the claude.ai Atlassian connector, or a user-scope server named `atlassian`) showing ✔ Connected
is enough. If neither is connected, stop and tell the user to connect one (`/mcp`). VPN is needed for the
context layer and Bitbucket. Then Langfuse (skip when `$ARGUMENTS` has
`--langfuse false`): `uv run --project "$PROJ" python -c "from rfc_agent import observe; raise SystemExit(observe.preflight())"`
— exit 2 means tracing is on (the default) but the keys are missing: stop and relay its message (add the keys to
the config, or rerun with `--langfuse false`). Never pick `false` for the user.

## 2. Relay progress

Don't poll. The background shell only notifies you when the whole run exits, and a pause happens mid-run — if
you wait to "check again in 20 s", nothing wakes you and the user sits at a pause. Right after the launch (in
the same turn), start a **Monitor** (`timeout_ms` 1800000, description `rfc run events`):

```
cd "$RUN/logs" && touch run.out progress.jsonl activity.log && tail -n +1 -F run.out progress.jsonl activity.log | grep --line-buffered -E 'WAITING FOR|press ⌥⌘P|ONE-TIME SETUP|OLD rfc-agent plugin|STOPPED|Langfuse observability|\[rfc\] ── |"text"|▶ started'
```

Each event is one of:

- a `WAITING FOR …` line — **a pause. Ask the question first (section 3), before anything else in that turn**:
  no status post, no card, no file reads beyond the proposal. Relay the rest after the answers are written.
- another line from section 3's table — act on it at once.
- a `[rfc] ── <Stage> — … ──` banner or a `progress.jsonl` line (`text` field: plan, tracers done, gate,
  reviewers done, fix rounds, readable copy) — post what's new, one short line per milestone (group several).
- a `▶ started · … · card <name>` line from `activity.log` — open that agent's card (below).

A Monitor lives at most 30 min and the run takes 30–60, so when it expires and the run is still going, re-arm it
with `tail -n 0 -F` (not `+1`, which replays everything) — and in the same turn read the last ~20 lines of
`run.out` yourself, so a pause that landed between the two monitors isn't missed. Stop it (TaskStop) when the
run exits.

Don't quote agents' reasoning in chat — the cards below carry it. If the user asks what an agent is doing, read
`$RUN/logs/status.json` (each agent's `last` action) or the end of its card log.

**One card per agent.** Every agent (Scope's repo discovery, lead, tracers, UI readers, reviewers, the summarizer)
writes its own log,
`$RUN/logs/agents/<stage>.<agent>.<step>.log`: its tool calls, its messages, and a final `■` line with the
`claude --resume … --fork-session` command that opens the full session. For each `▶ started · … · card <name>`
event the Monitor delivers, start (once, never twice for the same name):

```
TASK="<stage> · <agent>" uv run --project "$PROJ" python -m rfc_agent.watch "$RUN" <name>
```

with `run_in_background`, e.g. `TASK="investigate · T1" uv run --project /path/to/plugin python -m rfc_agent.watch
/path/to/run investigate.T1.run`. Same rule as the launch: the label first, literal paths, nothing in front. It shows as a task
card that runs while the agent works and finishes with it; clicking it shows the agent's output. Don't read
these cards' output yourself and don't post about them — they are for the user. Say once, at the first card:
*"Each agent has its own card — click one to see what it's doing."* `$RUN/logs/activity.log` is the one-line
feed across all agents (`tail -f` it in a terminal if you'd rather watch everything in one place).

**The rfc agents pane.** The plugin also draws a pane (hooks/register.tsx) that opens by itself when the
launch command above runs (it reads `--run-dir`, so that path must be literal): every agent with its state
and call count, and the newest running agent's log under it. The person picks another agent with its number
key once the pane has the keyboard (ctrl+x tab, or a click), `0` goes back to following the newest, Esc
returns to the prompt. `/rfc-agents <absolute run folder>` opens it on any run. Mention it once, with the
first card: *"The rfc agents pane beside the chat shows every agent — ctrl+x tab, then a number, to read
one."* It is drawn in the terminal; nothing is served or opened in a browser.

## 3. Things that need the user — these arrive as Monitor events from `run.out`

| Line | What to do |
|---|---|
| `press ⌥⌘P` (Scope, Figma) | One line: *"Figma is open at the linked design — press ⌥⌘P in Figma (Plugins → Development → rfc-agent Figma snapshot) and keep the plugin window open."* |
| `ONE-TIME SETUP` / `Figma plugin: import it from the repo` | Relay the import step and the manifest path verbatim |
| `an OLD rfc-agent plugin window is still exporting` | Tell the user to close the plugin panel (✕) and press ⌥⌘P again |
| `WAITING FOR SELECTION` (Scope) | Scope's selection: follow sections 3–4 of `${CLAUDE_PLUGIN_ROOT}/commands/scope.md` (proposal `$RUN/confirm/proposal.json` → answers `$RUN/confirm/answers.json`) |
| `WAITING FOR PAUSE 1` / `WAITING FOR PAUSE 2` (Investigate) | Follow section 3 of `${CLAUDE_PLUGIN_ROOT}/commands/investigate.md` (`$RUN/confirm/investigate-pause<n>.proposal.json` → `.answers.json`) |
| `WAITING FOR PAUSE 3` (Review, only with `--ask-review`) | Follow section 3 of `${CLAUDE_PLUGIN_ROOT}/commands/review.md` (triage) |
| `Langfuse observability is ON but the Langfuse keys are missing` (exit 2) | Nothing ran. Relay the message: add `LANGFUSE.PUBLIC_KEY`/`SECRET_KEY` to the config, or rerun with `--langfuse false` |
| `[rfc] STOPPED at …` | Report the stage and the lines just before it (the cause), and that rerunning the same command resumes |

Read the referenced section only when that pause arrives.

## 4. Finish

When the process exits, read `$RUN/logs/run.json` and report in a few lines:
- time and cost (total, and the biggest stage);
- the review outcome from `$RUN/review/status.json` — counts by status, and every `unresolved` / `not_reviewed`
  finding (id, severity, claim): those are in the RFC's "⚠ Unresolved review findings" section;
- the files: **`$RUN/out/rfc.readable.md`** (read this first), `$RUN/out/rfc.md` (the full RFC with citations),
  `$RUN/review/review.md`.
- Every agent's full Claude Code session is listed in `$RUN/logs/sessions.json` (`<stage>.<agent>.<step>` →
  session id). Offer once: `cd "$RUN" && claude --resume <id> --fork-session` opens one (`--fork-session` keeps
  the run's own session untouched — the lead's is resumed by later stages).
- **Ask for a rating, once.** After the report, ask one AskUserQuestion: *"How was this RFC?"* — `👍 Good`,
  `👎 Needs work`, `Later` (it lands in the run's Langfuse trace). On 👍 or 👎 follow `${CLAUDE_PLUGIN_ROOT}/commands/feedback.md`
  from section 2 (👎 asks what was wrong, one question; 👍 asks nothing more). On `Later`, say they can run
  `/rfc-agent:feedback` any time after reading the RFC, and drop it. Skip this whole step if the run STOPPED.
- The repo snapshots under `$RUN/_snap/` can be removed with
  `uv run --project "$PROJ" python -m rfc_agent.scope cleanup "$RUN"` once nobody needs to re-run a stage.
