# Feedback

People rate a run where they already work, and the rating lands on that run's own Langfuse trace. No form, no
copy-pasting a run id, and every rating is joined to the run's cost, gates and stages.

Code: `src/rfc_agent/feedback.py` (the command), `src/rfc_agent/run_facts.py` and `src/rfc_agent/run_metrics.py`
(what the run was about and what it consumed), `src/rfc_agent/observe.py` (records the trace id). Prompt:
`commands/feedback.md`, and the last step of `commands/rfc.md`.

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
   rating in         up | down, what is wrong in each area asked (none, or the gaps), run speed / cost only if asked,
                     optional comment, optional --stage
   pick the trace    from logs/traces.jsonl                         (see "Trace selection")
   read run facts    from the files above, at rating time
   scrub comment     secret-shaped strings redacted, capped at 2000 characters

3  WHERE IT LANDS
   Langfuse          scores on the picked trace; facts ride as score metadata
   local copy        logs/feedback.jsonl, always written, even when Langfuse is off or fails
```

## Scores

The person rating is meant to know the infra, services and business context. Every area gets a verdict, and an
area with gaps gets the gaps. A 👍 can carry gaps too: the thumb is the headline, not a filter.

| Score | Type | Value | Notes |
|---|---|---|---|
| `rfc_feedback` | BOOLEAN | 1 up, 0 down | headline; its comment is what was typed under Other on the overall question |
| `rfc_<area>` | CATEGORICAL | `usable`, `usable_with_gaps`, `not_usable`, `not_rated` | one per area, always written; its comment is what was typed under Other on that area's question |
| `rfc_<area>_issue` | CATEGORICAL | one gap, or `other_see_comment` | one score per gap picked; `metadata.likely_stages` says where to look first. `other_see_comment` is a gap typed under Other that matches no listed one, and carries the text as its comment |
| `rfc_suspect_stage` | CATEGORICAL | `scope`, `investigate`, `draft`, `review`, `summarize` | one score per stage the picked gaps point at (each gap names one or two); `none` when a re-rate removes it. This is what makes "which stage to fix" a chart |
| `rfc_run_speed` | CATEGORICAL | `fine`, `too_slow_to_run`, `too_slow_to_review`, `too_slow_to_run_and_review`, `not_rated` | always written; its comment is what was typed under Other on the run question |
| `rfc_run_cost` | CATEGORICAL | `worth_it`, `too_token_heavy`, `not_rated` | always written; same comment as `rfc_run_speed` |
| `rfc_start_ready` | CATEGORICAL | `yes`, `after_a_few_clarifications`, `after_many_clarifications`, `no`, `not_rated` | could implementation start |
| `rfc_feedback_stage` | CATEGORICAL | scope, investigate, draft, review, revise | only with `--stage` |

Areas and their gaps (`feedback.DIMENSIONS`):

| Area | Judged on | Gaps (stages to check first) |
|---|---|---|
| `readable` | `rfc.readable.md` is faithful to `rfc.md`, easy to scan | `too_long_or_repetitive`, `hard_to_navigate`, `leaves_out_something_important`, `wrong_emphasis` (Summarize) |
| `buildability` | a coding agent could build from `rfc.md` | `too_thin_to_build_from` (Draft, Investigate), `interface_or_contract_unclear`, `build_order_or_dependency_missing` (Draft), `citation_does_not_support_claim` (Draft, Investigate) |
| `context_layer` | right code, APIs and existing services found | `wrong_or_stale_code_fact` (Investigate, Scope), `missed_existing_code_or_service` (Investigate), `ignored_existing_pattern` (Investigate, Draft), `wrong_repos_or_pages` (Scope) |
| `coverage` | PRD and Figma flows handled, "covered" claims true | `missing_prd_flow`, `unneeded_flow_included` (Investigate), `missing_figma_flow_or_state` (Investigate, Scope), `marked_covered_but_not_addressed` (Draft, Review) |
| `approach` | the choice fits our infra and engineering standards | `ignores_existing_infra` (Draft, Investigate), `ignores_engineering_standards`, `generic_industry_choice_over_local_fit`, `tradeoff_not_justified`, `missing_alternative` (Draft) |
| `rollout_risk` | rollout, rollback, compatibility, performance, security, monitoring | `missing_rollout_rollback`, `missing_compatibility_or_migration`, `missing_performance_or_scale`, `missing_security_or_privacy`, `missing_monitoring` (Draft) |

Contradictory ticks resolve the same way everywhere: the problem wins. The chat tool cannot make an option
exclusive, so `Nothing wrong` ticked with a gap (or with typed text) records the gap, and `Fine` ticked with a
problem on the run question records the problem; ticking both slow options records `too_slow_to_run_and_review`. The overall
question is single-choice, so there is nothing to contradict there. A 👎 with every area marked `Nothing wrong` is
kept as given: it shows up as an unexplained 👎.

`not_rated` means the question was not answered. A gap given for an area marked `usable` makes it
`usable_with_gaps` (the gap wins), and so does a gap given with no verdict. An unknown area or value is dropped. The end-of-run question skips a
stopped run (`commands/rfc.md`), but `/rfc-agent:feedback --run-dir <folder>` rates any run folder, and areas the run
never reached stay `not_rated`.

How the person is asked (`commands/feedback.md`): a thumb with four choices (`👍 Good`, `👍 Good, with notes`,
`👎 Needs work`, `Later`), then, for the two that continue, four areas once each with their gaps in the same question
(`Nothing wrong` plus three gaps): coverage, buildability, approach and context layer use, all on one screen. The
readable summary, rollout and risk, and the run questions are not asked by default; they are asked only on request,
and otherwise stay `not_rated`. The thumb is asked alone first, so `👍 Good` and `Later` never show the area
questions. `👍 Good` is one tap and records the headline only, so
every area is `not_rated`. The chat tool allows four options per question, so the gaps not
shown (`unneeded_flow_included`, `citation_does_not_support_claim`, `missing_alternative`, `generic_industry_choice_over_local_fit`,
`ignored_existing_pattern`, `wrong_emphasis`, `missing_performance_or_scale`, `missing_monitoring`) are set when the person types
them under Other. What is typed under Other is kept against the question it was typed on (`--note <area>=…`, or `run`), next to
whatever was ticked on that same question: the ticked gaps are the issue scores and the text is the comment of the
question's score. A single-choice question (the overall one) takes the typed text as the answer in place of any
option (`--comment`), and the thumb is asked again on its own. A note on an area means the area has a gap, so it is
never `usable`. Each note is scrubbed for secret-shaped strings and capped at 2000 characters, and all of them are
also saved in `logs/feedback.jsonl`. The prompt records `usable` or `usable_with_gaps`;
`not_usable` is still accepted by the command but is not asked. The run question sets speed and cost together;
`rfc_start_ready` is `yes` when buildability has no gaps and is otherwise not asked. A re-rate is a fresh look: it
replaces the earlier rating in full, so an answer the new prompt does not ask is reset to `not_rated`.

Score names and values are the dashboard's schema. Add values freely; renaming one splits its history.

## The brief that travels with a rating

Every rating carries a `run_brief`: a few plain lines on the run, the review rounds and what the reviewer said, put
together from files (no model). It is on the headline score's metadata, and the latest is saved as
`<run>/logs/run_brief.md`. It names the run, attempts, time, cost and tokens; the input (PRD size, repos kept of those
proposed, Figma frames); the RFC (flows, requirement statuses, open questions, options); the review (findings by
severity, how many fixed, unresolved, revise rounds and lines changed); the reviewer's thumb, gaps, fine areas, the
stages to look at, run speed and cost, and whatever they typed. It says "no Figma exported" when the PRD had no
Figma, so a "missing Figma flow" gap can be read against it. So a rating read in Langfuse is understandable without
opening the run folder.

## The summary at the end of a run

Every run, rated or not, ends with the same lines as the brief, built from the run folder by
`feedback.run_summary` and ending in `Reviewer: not rated yet`. It is printed in the terminal, saved as
`<run>/logs/run_summary.md` and set as the Output of the run's `rfc-run` trace in Langfuse (a stage run on its own
does not write one). It is written when the run ends and is not updated when someone rates later; the rating
carries its own brief.

The review comments of every round stay in the run folder and are never overwritten: `review/R-*.md` (each
reviewer's findings), `review/R-*.confirm.md` and `confirm2.md` (the round-by-round confirmations),
`review/fixes.md` (the fix log), `review/rfc.round1.diff` and `round2.diff` (what each round changed) and
`review/status.json` (each finding's final state and round). The brief points at them.

## How a run ended, and how many attempts it took

A whole-run trace also carries `rfc_run_outcome` (`completed` or `stopped_at_<stage>`, never changed after the
attempt, so every stop is counted) and its session carries `rfc_run_outcome_final` with a fixed id, rewritten by each
attempt: a run that stopped at draft and was resumed ends as `completed`. The numbers `fact_attempts` and
`fact_resumes` say how many tries the run took, and `fact_duration_s` is the time of all of them. A stage run on its
own does not write either outcome.

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
- **Deterministic score ids** (`fb:<trace>:overall`, `:dim:<area>`, `:issue:<area>:<gap>`, `:run_speed`, `:stage`, …). Rating again overwrites instead
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
| the fields below | `run_metrics.py`, read after the run from the same files and each agent's `logs/*.stream.jsonl` |
| `prd_words`, `prd_tokens_est` | `raw/prd.md` (tokens are characters / 4) |
| `figma_sections`, `figma_frames`, `figma_frames_in_scope`, `figma_sections_skipped` | `scope.json` (a skipped section shares little vocabulary with the PRD) |
| `snapshot_age_days` | oldest `commit_date` of the repo snapshots, at the time the facts are read |
| `flows`, `flows_without_screens`, `reqs`, `open_questions` | `work/flows.json`, `work/reqs.json`, `work/open_questions.json` |
| `req_covered`, `req_assumes`, `req_deferred`, `req_open` | `logs/draft.json` |
| `alternatives` | rows of the §3 options table in `out/rfc.md` |
| `tokens_input`, `tokens_output`, `tokens_cache_read`, `tokens_cache_write`, `tokens_total`, `cache_hit_ratio` | the `result` event of each agent stream (Scope's Atlassian connector writes none, so it is not counted) |
| `agent_runs`, `agent_turns`, `models` | the agent streams |
| `compactions`, `peak_context_tokens` | the agent streams (compaction events; the largest input + cache of any message) |
| `context_calls`, `context_errors`, `context_empty` | `mcp__fleetlens__*` calls in the agent streams; empty means the result was `[]`, `{}` or blank |
| `repos_proposed`, `repos_preticked`, `repos_kept`, `pages_proposed`, `pages_preticked`, `pages_kept` | `confirm/proposal.json` (what Scope offered, and what it pre-ticked) against `scope.json` (what the person kept). Only when Scope asked |
| `figma_sections_kept` | `scope.json` |
| `readable_chars`, `readable_ratio` | `out/rfc.readable.md` against `out/rfc.md` (Summarize) |
| `review_blockers`, `review_majors`, `review_minors`, `review_fixed`, `review_ignored` | `review/status.json`, the findings' final state. `ignored` = the person chose not to fix it |
| `revise_rounds`, `revise_lines_changed` | the rounds recorded in `review/status.json`, and the changed lines of `review/rfc.round*.diff` |
| `langfuse_export_failures` | `[langfuse] export failed` lines in `logs/activity.log`, one for each failed or dropped upload to Langfuse (spans in such a batch are missing from the trace) |
| `attempts`, `resumes` | `logs/run.json`: how many tries the run took (a resume is one more try, not a new run) |
| `operator_wait_s`, `triage_wait_s` | `scope.json` confirm wait, Investigate's pauses, Review's triage (`logs/review.json`) |
| `rate_limit_rejections` | `rate_limit_event`s in the agent streams whose status is a refusal. The stage logs' `rate_limit_events` is not used: it counts every line that mentions a rate limit, and the CLI emits an "allowed" one on every check |

The numeric ones are also written as scores. Metadata cannot be summed or averaged on a dashboard, so when a run
closes `observe.py` writes each numeric fact as a NUMERIC score named `fact_<key>` (for example `fact_prd_words`,
`fact_req_covered`, `fact_tokens_total`, `fact_operator_wait_s`; the list is `observe.FACT_SCORES`) on the run's
root observation. A fact the run does not have is simply absent. A resumed run closes a new trace each time, and
each carries the facts as they stood then, so average these per run folder, don't sum them.

They go in two places: on every feedback score's metadata (read at rating time, so they describe the RFC as it
is now, including hand edits), and on the `rfc-run` observation as `metadata.run_facts` (read when the run
closes, so they describe the run as it finished).

## Where the data goes

- Langfuse project: whichever the config's keys and `LANGFUSE.HOST` point at. Feedback is not sent anywhere else.
- Tracing is required, so feedback is sent whenever the keys are there. The internal switch
  `RFC_AGENT_LANGFUSE=false` (tests and development) turns sending off; the rating is still saved locally and the
  command says so.
- Missing keys or a network error: saved locally, one line explains why, exit code 0. Feedback never fails the caller.
- The score metadata carries `run`, `session_id`, `stage`, `plugin_version` and the run facts. The trace
  already carries the user id, so approval can be charted per person and per plugin version.

## Dashboards

Two dashboards are built in Langfuse from these scores. They are not in this repository.

- **RFC Agent Feedback**, the overview: runs ended, completed and rated, thumbs-up share, typical cost and time; lines
  by release for approval, cost, time, review findings, open requirements and unresolved findings; areas and gaps
  that reviewers flagged; the suspected stage and where runs stopped; time per stage, cost per agent and the job's
  size; and failed uploads and failed context-layer calls.
- **RFC Agent: Deep Dive**, reason by reason: every gap picked by area, how each area was rated, areas with
  problems, gaps in each of the four asked areas, top gaps, the suspected stage and approval.

The rating scores sit on the run's trace, not its session, so no dashboard table can list one row per run for them;
open the run's trace in Langfuse to see its scores next to its cost. Chart `rfc_feedback` (average = approval rate) by
release, user or repo. Break each `rfc_<area>` down by value to see where RFCs fall short, and `rfc_<area>_issue`
(filter out `none`) for the specific gaps; group the gaps by `metadata.likely_stages` to see which stage to fix. Set
them beside the run facts (`cost_usd`, `tokens_total`, `prd_words`, `operator_wait_s`, `review_unresolved`).

## Known limits

- **Re-rating** rewrites every area, and clears the gaps picked on an earlier rating of the same trace by
  overwriting them with `none`. Typed notes and the overall comment it does not repeat are cleared too (sent as an
  empty comment; Langfuse keeps the old comment if none is sent). Scores written before the area form (`rfc_feedback_reason`) stay in Langfuse as
  history and are not touched. The clearing reads `logs/feedback.jsonl`, so a rating made from another
  machine, or after that file was deleted, cannot clear what it cannot see.
- **A resumed run is a new trace** in the same session; the rating goes to the latest `rfc` trace, as above.
- **The end-of-run question is an instruction to Claude** in `commands/rfc.md`, not code. It can be skipped
  by a model that doesn't follow it; `/rfc-agent:feedback` always works.
- **Facts are not versioned.** `rfc_chars` and the review counts change if the RFC is edited after the run, and
  `duration_s` comes from `logs/run.json`: `seconds_all_attempts` (the time of every attempt of the run) when it is
  there, and only the stages the last invocation ran for a run folder written before that field existed.
- **Token totals are a sum over agent stream files**; an agent that wrote no stream file is not counted.
- **Notes and comments are free text**, scrubbed for secret-shaped strings only. They depend on Claude passing them
  through as `commands/feedback.md` says; the command cannot tell a typed answer that was dropped. Anything else a person types lands in
  your Langfuse project.
- **Only whole-run and stage traces are scorable**; there is no per-finding or per-section rating.
- The unit tests use a fake Langfuse client. The send path has been checked against the real project on a full run
  (46 of the 55 run numbers then in use matched the run folder) and with one test span after the OpenTelemetry
  upgrade, but no automated test covers delivery.
