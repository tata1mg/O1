# Feedback

People rate a run where they already work, and the rating lands on that run's own Langfuse trace. No form, no
copy-pasting a run id, and every rating is joined to the run's cost, gates and stages.

Code: `src/rfc_agent/feedback.py` (the command), `src/rfc_agent/run_facts.py` (what the run was about),
`src/rfc_agent/observe.py` (records the trace id). Prompt: `commands/feedback.md`, and the last step of
`commands/rfc.md`.

---

## Flow

```
1  DURING THE RUN (recorded in the run folder, no extra model calls)
   trace opens   ->  logs/traces.jsonl      {trace_id, entry, ts, prd}   one line per traced invocation
   Scope         ->  scope.json             repos, PRD copy, Figma, Jira, context layer status
   Draft/Review  ->  out/rfc.md, review/status.json
   run closes    ->  logs/run.json          cost, duration
                     rfc-run observation gets metadata.run_facts (see "Run facts")

2  WHEN SOMEONE RATES  (end of /rfc-agent:rfc, or /rfc-agent:feedback later)
   rating in         up | down, reasons (down only), optional comment, optional --stage
   pick the trace    from logs/traces.jsonl                         (see "Trace selection")
   read run facts    from the files above, at rating time
   scrub comment     secret-shaped strings redacted, capped at 2000 characters

3  WHERE IT LANDS
   Langfuse          scores on the picked trace; facts ride as score metadata
   local copy        logs/feedback.jsonl, always written, even when Langfuse is off or fails
```

## Scores

| Score | Type | Value | Notes |
|---|---|---|---|
| `rfc_feedback` | BOOLEAN | 1 up, 0 down | headline; the comment is the score's comment |
| `rfc_feedback_reason` | CATEGORICAL | one of the reasons below | one score per reason; down only |
| `rfc_feedback_stage` | CATEGORICAL | scope, investigate, draft, review, revise | only with `--stage` |

Reasons: `wrong_repos`, `missed_flows`, `inaccurate`, `too_shallow`, `too_long`, `too_slow`, `too_costly`, `other`.
An unknown reason is dropped. Reasons given with an up rating are ignored.

Why the prompt shows only four reasons: a rating should cost one or two taps. The chat tool allows four options
per question, so the picker offers the four content reasons that need a human to judge. `too_slow` and
`too_costly` are cheap to confirm from `duration_s` and `cost_usd` on the same trace, and `too_long` is rare;
all three are still valid values, set when the person types them under Other. The typed text is always kept as
the comment. A 👎 is two prompts (thumb, reasons); a 👍 is one.

Score names and reason values are the dashboard's schema. Add values freely; renaming one splits its history.

## Trace selection

| Situation | Trace scored |
|---|---|
| Normal: whole run via `/rfc-agent:rfc` | the **latest** `rfc` trace in `traces.jsonl` |
| Run resumed or rerun | the latest `rfc` trace; earlier invocations are not scored |
| Stages run one by one, no `rfc` trace | the latest trace of any kind |
| `--stage S` and that stage has its own trace | that stage's latest trace |
| `--stage S` but only a whole-run trace exists | the whole-run trace; `rfc_feedback_stage` still names the stage |
| No trace recorded (tracing off, or a run from before `traces.jsonl`) | the Langfuse **session** (`identity.json`'s `session_id`, else the folder name) |

Decisions behind this:

- **Whole-run trace by default.** A person rates the RFC, not a stage, and the `rfc` trace is the one with
  total cost and duration, so approval can be charted against them.
- **Latest, not first.** After a resume the latest trace describes the RFC they are looking at.
- **Session as the fallback, not nothing.** A rating with a run and no trace detail beats a lost rating.
- **A file, not Langfuse, is the record of which trace.** The rating can come days later from a different
  Claude session; the run folder is the only thing both sides share.
- **Deterministic score ids** (`fb:<trace>:overall`, `:reason:<r>`, `:stage`). Rating again overwrites instead
  of double-counting.

## Run facts

Flat fields, each read from a file the run already wrote; a missing file drops that field.

| Field | Source |
|---|---|
| `prd` | original `--prd` argument (from `traces.jsonl`), else the local copy's path in `scope.json` |
| `jira`, `figma_count`, `context_layer_ok` | `scope.json` |
| `repos` | `scope.json` (repo names) |
| `rfc_chars` | size of `out/rfc.md` |
| `review_findings`, `review_unresolved` | `review/status.json` (`unresolved` and `not_reviewed` count as unresolved) |
| `cost_usd`, `duration_s` | `logs/run.json` |

They go in two places: on every feedback score's metadata (read at rating time, so they describe the RFC as it
is now, including hand edits), and on the `rfc-run` observation as `metadata.run_facts` (read when the run
closes, so they describe the run as it finished).

## Where the data goes

- Langfuse project: whichever the config's keys and `LANGFUSE.HOST` point at. Feedback is not sent anywhere else.
- `LANGFUSE.ENABLED=false` (or `--langfuse false`) turns feedback sending off too. The rating is still saved
  locally and the command says so.
- Missing keys or a network error: saved locally, one line explains why, exit code 0. Feedback never fails the caller.
- The score metadata carries `run`, `session_id`, `stage`, `plugin_version` and the run facts. The trace
  already carries the user id, so approval can be charted per person and per plugin version.

## Dashboard

Chart `rfc_feedback` (average = approval rate) by `plugin_version`, user, or repo; break down down-ratings by
`rfc_feedback_reason`; set it beside `cost_usd`, `duration_s`, `review_unresolved` and the `gate_*` scores on
the same trace.

## Known limits

- **Re-rating with fewer reasons leaves the old reason scores.** Scores are overwritten by id, but a reason
  that is no longer chosen is not deleted, and switching from down to up does not remove earlier reasons.
  Until that is handled, filter reasons by the latest `rfc_feedback` for the same trace.
- **The end-of-run question is an instruction to Claude** in `commands/rfc.md`, not code. It can be skipped
  by a model that doesn't follow it; `/rfc-agent:feedback` always works.
- **Facts are not versioned.** `rfc_chars` and the review counts change if the RFC is edited after the run.
- **Comments are free text**, scrubbed for secret-shaped strings only. Anything else a person types lands in
  your Langfuse project.
- **Only whole-run and stage traces are scorable**; there is no per-finding or per-section rating.
- The send path has been unit-tested with a fake client. Verify it once against the real Langfuse project.
