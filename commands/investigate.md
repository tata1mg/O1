---
description: The investigate stage — lead plans, tracers trace the code in parallel, lead integrates (progress and pauses shown here)
argument-hint: --run-dir <scope run dir> [--preset balanced|fast] [--req-granularity grouped|fine] [--ask] [--fresh]
---

Run rfc-agent's Investigate for: `$ARGUMENTS`

Investigate runs in the background. YOU are its progress view and its pause UI: relay milestones in short lines,
and at each pause show the proposal as AskUserQuestion selections and write the answers back. Never answer a
pause yourself. No live feed, no browser pane.

## 1. Start

```
PROJ="${CLAUDE_PLUGIN_ROOT}"
RUN=<the --run-dir value from $ARGUMENTS, made absolute>
mkdir -p "$RUN/logs"
TASK="investigate · <run folder name>" uv run --project "$PROJ" python -m rfc_agent.investigate $ARGUMENTS --confirm-file > "$RUN/logs/investigate.out" 2>&1
```

If it exits 2 with `Langfuse observability is ON but the Langfuse keys are missing`, nothing ran: relay the message (add the keys to the config, or rerun with `--langfuse false`). Never pick `false` for the user.

Run it with `run_in_background`. If `$RUN/scope.json` is missing, stop: Scope (`/rfc-agent:scope`) comes first.

The terminal's task list (`/tasks`) names a background shell by the **start of its command**, so send it as
one command that begins with the `TASK="…"` label: write `$PROJ` and `$RUN` out as literal paths, and put
no `cd`, `PROJ=` or other assignment in front of it.

## 2. Relay progress

Don't poll on a timer — the background shell only notifies you when the stage exits, and pauses 1 and 2 happen
mid-run. Right after the launch (same turn), start a **Monitor** (`timeout_ms` 1800000, description `investigate events`):

```
cd "$RUN/logs" && touch investigate.out progress.jsonl && tail -n +1 -F investigate.out progress.jsonl | grep --line-buffered -E 'WAITING FOR|Langfuse observability|"text"|^investigate:'
```

Each event is a `progress.jsonl` milestone or a line that needs you. On a `WAITING FOR PAUSE` line, go to
section 3 and ask first — nothing else in that turn. If the Monitor expires while the stage still runs, re-arm it
with `tail -n 0 -F` and read the last ~20 lines of `investigate.out` yourself in the same turn. Stop it (TaskStop) when
the stage exits.

Post each new milestone's `text` as one short line — group several that arrived together into one message. Don't post when
nothing is new. Don't quote agents' reasoning. Examples of what arrives: `Plan: 4 flows, 3 tracers (…)`,
`T1 done (6m12s, $0.92) · checks clean`, `T3: 2 must-fix item(s) → sent back once`, `1c done: …`.

If the user asks what an agent is doing, read `$RUN/logs/status.json` (current state per agent) or the last
few `tool_use` lines of `$RUN/logs/investigate.<agent>.run.stream.jsonl`, and answer in one or two lines.

If `investigate.out` shows `investigate:` followed by an error and the process exited, report that line and stop.

## 3. Pauses

A milestone of kind `pause1` / `pause2` (and a `WAITING FOR PAUSE <n>` line in `investigate.out`) means Investigate
is waiting. Read `$RUN/confirm/investigate-pause<n>.proposal.json`:
`{"header", "items": [{n, kind, key, label, note, include, question?}]}` — kinds `retry` (repo in scope
without a snapshot), `question` (open question; `question` holds `{question, blocks, suggested_answer}`).

Scope's repos are final: never offer to add a repo. If the user asks for one, tell them to rerun
`/rfc-agent:scope`. Hops outside scope arrive as an `outside` milestone ("Not traced …") — relay it as info.

Show the header (for Pause 1 it lists the tracer plan; read `$RUN/work/flows.md` if the user wants detail).
Then ask with AskUserQuestion — up to 4 options per question, up to 4 questions per call, header ≤ 12 chars;
chunk when needed:

- **Retry snapshots** (multi-select): label = repo; ticking retries its snapshot, unticked = its hops stay
  untraced.
- **Pause 1 only — plan feedback** (single-select): "Plan looks right" / "Change it" — on "Change it", ask
  for the change in "Other" free text.
- **Open questions** (Pause 2 with `--ask`): one question each; options = the suggested answer (if any) and
  "Leave open"; "Other" = their own answer.

Write `$RUN/confirm/investigate-pause<n>.answers.json` with the Write tool:

```json
{"selected": [1, 3], "feedback": "", "answers": {"Q1": "cart team"}}
```

`selected` = the `n` of every retry item to act on and every question answered; `answers` holds the
answer text by question id (a "Leave open" question is not selected and has no answer).

## 4. Finish

When the process exits, read `$RUN/logs/investigate.json` and report: time, cost, agents (with any `unverified`
or `untraced` status), rate-limit events, and the files to read next: `$RUN/work/flows.md`,
`$RUN/work/understanding.md`, `$RUN/work/open_questions.json`.
