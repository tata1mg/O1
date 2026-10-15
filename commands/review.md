---
description: The review stage — independent review of the RFC (one reviewer per flow + one cross-cutting, in parallel)
argument-hint: --run-dir <run dir after Draft> [--ask-review] [--fresh]
---

Run rfc-agent's Review for: `$ARGUMENTS`

The review runs in the background. YOU relay its progress in short lines and, with `--ask-review`, are the
triage UI. Never triage findings yourself. No live feed.

## 1. Start

```
PROJ="${CLAUDE_PLUGIN_ROOT}"
RUN=<the --run-dir value from $ARGUMENTS, made absolute>
TASK="review · <run folder name>" uv run --project "$PROJ" python -m rfc_agent.review $ARGUMENTS --confirm-file > "$RUN/logs/review.out" 2>&1
```

If it exits 2 with `Langfuse observability is ON but the Langfuse keys are missing`, nothing ran: relay the message (add the keys to the config, or rerun with `--langfuse false`). Never pick `false` for the user.

Run it with `run_in_background`. If `$RUN/out/rfc.md` is missing, stop: Draft (`/rfc-agent:draft`) comes first.

The terminal's task list (`/tasks`) names a background shell by the **start of its command**, so send it as
one command that begins with the `TASK="…"` label: write `$PROJ` and `$RUN` out as literal paths, and put
no `cd`, `PROJ=` or other assignment in front of it.

## 2. Relay progress

Don't poll on a timer — the background shell only notifies you when the stage exits, and the triage pause (with `--ask-review`) happens
mid-run. Right after the launch (same turn), start a **Monitor** (`timeout_ms` 1800000, description `review events`):

```
cd "$RUN/logs" && touch review.out progress.jsonl && tail -n +1 -F review.out progress.jsonl | grep --line-buffered -E 'WAITING FOR|Langfuse observability|"text"|^review:'
```

Each event is a `progress.jsonl` milestone or a line that needs you. On a `WAITING FOR PAUSE` line, go to
section 3 and ask first — nothing else in that turn. If the Monitor expires while the stage still runs, re-arm it
with `tail -n 0 -F` and read the last ~20 lines of `review.out` yourself in the same turn. Stop it (TaskStop) when
the stage exits.

Post each new milestone's `text` in one short line;
stay quiet when nothing is new. Expect `Review: N reviewer(s) running (…)`, then one `R-… done (time, cost):
n blocker, n major, n minor` per reviewer, possibly `… retrying once` or `… NOT reviewed`, then a final
`Review: …` line. If the user asks what a reviewer is doing, read the last few `tool_use` lines of
`$RUN/logs/review.<reviewer>.run.stream.jsonl`.

## 3. Triage (only with --ask-review)

A `pause` milestone (and `WAITING FOR PAUSE 3` in `review.out`) means the review waits for you. Read
`$RUN/confirm/investigate-pause3.proposal.json` — items of kind `finding` with key (e.g. `R-F1.2`), label
(severity + claim) and `include` (BLOCKER/MAJOR pre-ticked). Show the totals first, then ask with AskUserQuestion
(multi-select, up to 4 options per question, up to 4 questions per call; chunk as needed): label = the finding id
and severity, description = the claim. Say which are pre-ticked. "Other" takes a note on a finding, written as
`<id>: <note>` (e.g. `R-F1.2: disagree, svc_a never edits`).

Write `$RUN/confirm/investigate-pause3.answers.json`:

```json
{"selected": [1, 2, 4], "notes": {"R-F1.2": "disagree: svc_a never edits"}, "feedback": ""}
```

`selected` = the `n` of every finding to FIX; unselected ones are ignored (their notes still go to the lead).

## 4. Finish

Report the final `Review:` line, any reviewer that was not reviewed, and the file to read:
`$RUN/review/review.md` (summary table, then every finding with its id).
