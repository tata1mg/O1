---
description: The revise stage — the lead fixes the review findings, the gate re-checks, reviewers confirm (up to two rounds)
argument-hint: --run-dir <run dir after Review> [--fresh]
---

Run rfc-agent's Revise (fix) for: `$ARGUMENTS`

It runs in the background with no pauses; you relay progress in short lines. No live feed.

```
PROJ="${CLAUDE_PLUGIN_ROOT}"
RUN=<the --run-dir value from $ARGUMENTS, made absolute>
TASK="revise · <run folder name>" uv run --project "$PROJ" python -m rfc_agent.revise $ARGUMENTS > "$RUN/logs/revise.out" 2>&1
```

If it exits 2 with `Langfuse observability is ON but the Langfuse keys are missing`, nothing ran: relay the message (add the keys to the config, or rerun with `--langfuse false`). Never pick `false` for the user.

Run it with `run_in_background`. If `$RUN/review/review.md` is missing, stop: Review (`/rfc-agent:review`) comes
first.

The terminal's task list (`/tasks`) names a background shell by the **start of its command**, so send it as
one command that begins with the `TASK="…"` label: write `$PROJ` and `$RUN` out as literal paths, and put
no `cd`, `PROJ=` or other assignment in front of it.

Watch `$RUN/logs/progress.jsonl` (new lines every ~30 s) and post each new milestone's `text` in one short line;
stay quiet when nothing is new. Expect: `Revise: fixing N finding(s) …`, then per round `Fix log (round N): …
confirming with …` and `Round N: … resolved, … accepted, … not resolved …`, and a final `Revise done: …` line.
Gate lines (`Gate: …`) also appear — that is Draft's gate re-checking the fixed RFC — and at the end
`Summarize: writing the readable copy` / `Readable copy written: …`.

When it exits, report the final line, then list any finding whose status in `$RUN/review/status.json` is
`unresolved` or `not_reviewed` (id, severity, claim) — they are in the RFC's "⚠ Unresolved review findings"
section — and point to `$RUN/out/rfc.readable.md` (the concise copy, written automatically at the end — Summarize),
`$RUN/out/rfc.md` (the full RFC) and `$RUN/review/fixes.md`.
