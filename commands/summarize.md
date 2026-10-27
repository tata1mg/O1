---
description: The summarize stage — a concise, human-readable copy of the final RFC (runs automatically after Revise)
argument-hint: --run-dir <run dir after Revise> [--fresh] [--engine claude-code|axon] [--stage-engine STAGE=ENGINE]
---

Run rfc-agent's Summarize for: `$ARGUMENTS` — normally it already ran at the end of Revise; use this after
hand-editing `out/rfc.md`, or with `--fresh` to rewrite it.

```
PROJ="${CLAUDE_PLUGIN_ROOT}"
RUN=<the --run-dir value from $ARGUMENTS, made absolute>
TASK="summarize · <run folder name>" uv run --project "$PROJ" python -m rfc_agent.summarize $ARGUMENTS > "$RUN/logs/summarize.out" 2>&1
```

The terminal's task list (`/tasks`) names a background shell by the **start of its command**, so send it as
one command that begins with the `TASK="…"` label: write `$PROJ` and `$RUN` out as literal paths, and put
no `cd`, `PROJ=` or other assignment in front of it.

If it exits 2 with `Langfuse observability is ON but the Langfuse keys are missing`, nothing ran: relay the message (add the keys to the config, or rerun with `--langfuse false`). Never pick `false` for the user.

It takes a minute or two — run it with `run_in_background`. When it exits, report the last line of
`$RUN/logs/progress.jsonl` (size of the copy vs. the full RFC) and point to `$RUN/out/rfc.readable.md` (the copy
to read) and `$RUN/out/rfc.md` (the full RFC with citations).
