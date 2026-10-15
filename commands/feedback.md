---
description: Rate an rfc-agent run 👍/👎 — saved on that run's Langfuse trace so it can be charted later
argument-hint: [--run-dir <run dir>] [up|down]
---

Record feedback for an rfc-agent run: `$ARGUMENTS`

How the trace is chosen and where the data goes: `${CLAUDE_PLUGIN_ROOT}/docs/FEEDBACK.md` (read it only if asked).

The rating is attached to the run's own Langfuse trace (and kept in `$RUN/logs/feedback.jsonl`), so it is
joined to that run's cost, gates and stages. It takes under a minute. Never rate on the user's behalf.

## 1. Which run

`RUN` = the `--run-dir` value if given; otherwise the run folder from this conversation (the one `/rfc-agent:rfc`
or a stage just worked on); otherwise the newest folder under `RUNS_ROOT`
(`ls -td "$(uv run --project "${CLAUDE_PLUGIN_ROOT}" python -c "import rfc_agent.config as c; print(c.get_config().get('RUNS_ROOT') or '~/rfc-runs')")"/*/ | head -1`).
Say which run you are rating in one line, so the user can correct it.

## 2. Ask (AskUserQuestion — skip any answer already given in `$ARGUMENTS`)

1. **"How was this RFC?"** — `👍 Good — usable as written or close` · `👎 Needs work`. (An `up`/`down` argument answers this.)
2. Only on 👎, ONE multiSelect **"What was wrong? (pick any, or type your own under Other)"**:
   `inaccurate` (claims don't match the code) · `missed_flows` (a flow or edge is missing) · `wrong_repos` (Scope
   picked the wrong repos/pages) · `too_shallow` (not specific enough to build from).
   - Whatever the user types under Other is their comment: pass it as typed, never paraphrased. If it clearly
     names one of `too_long` (hard to read), `too_slow`, `too_costly`, add that as a `--reason` as well;
     otherwise add none for it.
   - No third question. On 👍 ask nothing more; if the user volunteered a comment in `$ARGUMENTS` or in the
     reply, pass it, else don't prompt for one.

## 3. Record

```
uv run --project "${CLAUDE_PLUGIN_ROOT}" python -m rfc_agent.feedback --run-dir "$RUN" --rating <up|down> \
  [--reason <r> …] [--comment "<their line>"] [--stage <scope|investigate|draft|review|revise>]
```

`--stage` only when the user said the rating is about one stage. Quote the comment safely (it may contain
quotes). Relay the single line it prints. If it says `saved locally (not sent to Langfuse: …)`, tell the user the
reason in one line — the rating is not lost, and it is not an error. Rating the same run again replaces the
earlier rating.
