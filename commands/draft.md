---
description: The draft stage — the lead writes the RFC (out/rfc.md), then the gate checks it
argument-hint: --run-dir <run dir after Investigate> [--fresh] [--check-only]
---

Run rfc-agent's Draft for: `$ARGUMENTS`

Draft runs in the background; you relay its progress in short lines. It has no pauses. No live feed.

## 1. Start

```
PROJ="${CLAUDE_PLUGIN_ROOT}"
RUN=<the --run-dir value from $ARGUMENTS, made absolute>
TASK="draft · <run folder name>" uv run --project "$PROJ" python -m rfc_agent.draft $ARGUMENTS > "$RUN/logs/draft.out" 2>&1
```

If it exits 2 with `Langfuse observability is ON but the Langfuse keys are missing`, nothing ran: relay the message (add the keys to the config, or rerun with `--langfuse false`). Never pick `false` for the user.

Run it with `run_in_background`. If `$RUN/work/understanding.md` is missing, stop: Investigate
(`/rfc-agent:investigate`) comes first.

The terminal's task list (`/tasks`) names a background shell by the **start of its command**, so send it as
one command that begins with the `TASK="…"` label: write `$PROJ` and `$RUN` out as literal paths, and put
no `cd`, `PROJ=` or other assignment in front of it.

## 2. Relay progress

Watch `$RUN/logs/progress.jsonl` for new lines (every ~30 s) and post each new milestone's `text` in one
short line; stay quiet when nothing is new. Expect: `Draft: the lead is writing the RFC` (it takes several
minutes — say so once), possibly `coverage.json: N problem(s) → back to the lead once`, then `RFC written: …`.
The gate then runs: expect possibly `Gate: … → back to the lead once`, then `RFC written: …`, and a final
`Gate: … clean` / `Gate: clean after the fix` / `Gate: N known issue(s) left for review` line.
`--check-only` skips writing and re-runs only the gate (e.g. after a hand edit).
If the user asks what the lead is doing, read the last few `tool_use` lines of the newest
`$RUN/logs/draft.lead.*.stream.jsonl`.

If `draft.out` shows `draft:` followed by an error and the process exited, report that line and stop.

## 3. Finish

Report the `RFC written` line (size, flow sections, CHANGEs, coverage counts, time, cost), the gate's result
from `$RUN/out/check.json` (status, known issues grouped by check, flags for review), and the file to read:
`$RUN/out/rfc.md`.
