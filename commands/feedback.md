---
description: Rate an rfc-agent run — a thumb and what was wrong in each area, or why it stopped or failed; saved on that run's Langfuse trace so it can be charted later
argument-hint: [--run-dir <run dir>] [up|down]
---

Record feedback for an rfc-agent run: `$ARGUMENTS`

How the trace is chosen, the scores and where the data goes: `${CLAUDE_PLUGIN_ROOT}/docs/FEEDBACK.md` (read it only
if asked).

The rating is attached to the run's own Langfuse trace (and kept in `$RUN/logs/feedback.jsonl`), so it is joined to
that run's cost, tokens, gates and stages. It is meant for someone who knows the infra, services and business
context. A plain 👍 is one tap; the rest is two more short screens. Never rate on the user's behalf.

## 1. Which run

`RUN` = the `--run-dir` value if given; otherwise the run folder from this conversation (the one `/rfc-agent:rfc`
or a stage just worked on); otherwise the newest folder under `RUNS_ROOT`
(`ls -td "$(uv run --project "${CLAUDE_PLUGIN_ROOT}" python -c "import rfc_agent.config as c; print(c.get_config().get('RUNS_ROOT') or '~/rfc-runs')")"/*/ | head -1`).
Say which run you are rating in one line, so the user can correct it.

## 2. The run's numbers

```
uv run --project "${CLAUDE_PLUGIN_ROOT}" python -m rfc_agent.feedback --run-dir "$RUN" --context
```

prints JSON with `line`: the run's measured numbers (time, cost, tokens, PRD size, requirement counts; may be
empty), and how the latest attempt ended, taken from the run's own record:

- `end`: `finished`, `stopped` (you stopped it), `failed`, or `unknown` (no end record: killed, still running, or an old run);
- `recorded`: one line saying so (stage, cause), to show at the top of the first question;
- `cause` and `excerpt`: the recorded failure cause (failed only); `stage`; `attempt` of `attempts`;
- `earlier`: attempts before this one that did not finish (they keep their own feedback).
- `previous`: what this same attempt was rated before (`null` if it was not): `rating`, `verdicts`, `issues`, `notes`,
  `comment`, `run_speed`, `run_cost`, `start_ready`, `minutes_saved`, `minutes_spent`, or for a stopped or failed
  attempt `stop_reason`, `failure_check`, `failure_reported`, `rerun`; `source` says where it came from (the run
  folder, or Langfuse when the run folder has no record). Another attempt's feedback is never in it.

Show `recorded` above the first question so the person sees which path they are on. **Never ask how the run ended
when `end` is not `unknown`**: go straight to the path below.

| `end` | Go to |
|---|---|
| `finished` | section 3a (the RFC form) |
| `stopped` | section 3b |
| `failed` | section 3c |
| `unknown` | one question first, "How did this run end?" (`It finished` · `I stopped it` · `It failed` · `Later`), pass the answer as `--run-end finished|stopped|failed`, then the matching path. `Later`: ask nothing more |

## Re-rating: show the earlier answers (`previous` is not null)

Applies to all three paths. A second rating of the same attempt replaces the first, so show what was said last time
and let the person keep or change it, question by question:

- Start with one line: "You rated this run before: <one-line summary, e.g. 👍, 4 areas, 2 gaps>. Keep or change?"
  `Keep it as it is` · `Change some answers`. On `Keep it as it is`, run the record command with every value from
  `previous` and ask nothing more.
- On `Change some answers`, ask every question as usual, and mark what was picked last time: append
  " (picked last time)" to those options (for an area, the verdict and each gap; for the run questions, the speed and
  cost options; for a stopped or failed run, the reason and the run-again answer). For the two time questions, show
  the earlier number: "Last time: 30 minutes. A number to change it, or keep".
- Always pass the whole rating again, kept answers included (that is what keeps one complete rating on Langfuse).
  Treat `keep` on a time question as passing the earlier number. Never pass only the changed answers.
- An earlier typed note is shown with its question and kept unless the person types a new one (then pass the new one).

When `previous` is null, or the person is rating another attempt, nothing is prefilled: ask fresh.

## 3a. Ask for a finished run (AskUserQuestion — skip any answer already given in `$ARGUMENTS`)

If `earlier` is not empty, say in one line that attempt(s) N stopped or failed and keep their own feedback, and that
anything more about them can be typed under Other here. Do not ask which attempt to rate: this rates the finished one.

Each area is asked once, with its gaps in the same question, so the person says what is wrong while it is fresh.
The thumb is asked alone first, so a plain 👍 or `Later` never shows the area questions.

**Screen 1** — one question, nothing else on the screen:

1. **"How was this RFC overall?"** — `👍 Good` · `👍 Good, with notes` · `👎 Needs work` · `Later`.
   An `up` argument means `👍 Good`, a `down` argument `👎 Needs work`.
   - `👍 Good`: record the thumb and stop. Ask nothing more.
   - `Later`: ask nothing more; say they can run `/rfc-agent:feedback` any time after reading the RFC.
   - `👍 Good, with notes` and `👎 Needs work` continue with screen 2, the same for both: an RFC the person likes
     can still have gaps worth recording.

**Screen 2** — the four main areas, in one call (four questions, asked right after the thumb with no pause):

2. **Coverage** — "Coverage: anything wrong? (pick any)"
3. **Buildability** — "Buildability: anything wrong? (pick any)"
4. **Approach** — "Approach: anything wrong? (pick any)"
5. **Context layer use** — "Context layer use: anything wrong? (pick any)"

The readable summary, rollout and run questions are not asked by default: pass nothing for those dimensions, and
ask them only if the person asks for them. Keep the turn between screens to the mapping; do not re-deliberate.

Every area question is multiSelect and starts with `Nothing wrong`, then three gaps (a fourth would not fit the
chat tool's four options). A question may not be left on `Nothing wrong` together with a gap; if both are picked,
the gap wins.

| dimension | question hint | gaps shown (value) | not shown: add when typed under Other (value) |
|---|---|---|---|
| `coverage` | are all PRD and Figma flows handled, and are the "covered" claims true? | missing PRD flow (`missing_prd_flow`) · missing Figma flow or state (`missing_figma_flow_or_state`) · RFC says it's covered, but the software design in the RFC doesn't address it (`marked_covered_but_not_addressed`) | flow not needed (`unneeded_flow_included`) |
| `buildability` | could a coding agent build from `rfc.md` without guessing? | Too vague to start coding (`too_thin_to_build_from`) · Ambiguous interface or API contract (`interface_or_contract_unclear`) · Doesn't say what to build first or what depends on what (`build_order_or_dependency_missing`) | A source is cited but doesn't say that (`citation_does_not_support_claim`) |
| `approach` | does the chosen option fit our infra and engineering standards? | poor approach for our systems, e.g. the wrong service or place to build it (`poor_approach`) · ignores engineering standards (`ignores_engineering_standards`) · tradeoff not justified (`tradeoff_not_justified`) | missing alternative (`missing_alternative`) · a generic industry choice over local fit (`generic_industry_choice_over_local_fit`) |
| `context_layer` | did it find the right code, APIs and existing services? | wrong or stale code fact (`wrong_or_stale_code_fact`) · missed existing code, service or API (`missed_existing_code_or_service`) · wrong repos or pages (`wrong_repos_or_pages`) | ignored an existing pattern (`ignored_existing_pattern`) |
| `readable` | can you review `rfc.readable.md` quickly, and does it match `rfc.md`? | too long or repetitive (`too_long_or_repetitive`) · hard to navigate (`hard_to_navigate`) · leaves out something important (`leaves_out_something_important`) | wrong emphasis (`wrong_emphasis`) |
| `rollout_risk` | rollout, rollback, compatibility, performance, security, monitoring? | missing rollout or rollback (`missing_rollout_rollback`) · missing compatibility or migration (`missing_compatibility_or_migration`) · missing security or privacy (`missing_security_or_privacy`) | missing performance or scale (`missing_performance_or_scale`) · missing monitoring (`missing_monitoring`) |

**Turning the answers into the command:**

- An area with `Nothing wrong` → `--verdict <dimension>=usable`. An area with gaps → `--verdict
  <dimension>=usable_with_gaps` and one `--issue <dimension>=<value>` per gap. An area the person skipped → pass
  nothing for it.
- **Whatever the user types under Other is kept against the question it was typed on**, alongside whatever they
  ticked on that same question. Pass each as `--note <key>=<text>`, as typed, never paraphrased, where `<key>` is
  the area's dimension (`coverage`, `buildability`, …) or `run` for the run question. The ticked gaps still become
  `--issue`s; the text is kept next to them. If the text clearly names a gap from the "not shown" column of that
  area, add that `--issue` as well. Don't add `other_see_comment` yourself: the command records the text as the area's
  `other_see_comment` gap when no listed gap was ticked.
- **The overall question is single-choice**, so text typed under Other is the person's answer and takes preference
  over any option: pass it as `--comment`, as typed. It carries no thumb, so ask the thumb once on its own
  (`👍 Good` · `👍 Good, with notes` · `👎 Needs work`) and continue as above.
- The run question: both `Too slow to run` and `Too slow to review` → `--run-speed too_slow_to_run_and_review`; only
  `Too slow to run` → `--run-speed too_slow_to_run`; only `Too slow to review` → `--run-speed too_slow_to_review`;
  otherwise (anything else picked) `--run-speed fine`. `Too token heavy` → `--run-cost too_token_heavy`, otherwise
  (anything picked) `--run-cost worth_it`. Nothing picked → pass neither.
- **Contradictory ticks: the problem always wins.** The chat tool cannot make an option exclusive, so a person can
  tick `Nothing wrong` together with a gap (or with text under Other), or `Fine` together with a problem. Treat the
  `Nothing wrong` / `Fine` as not ticked, and say so in one line when you relay the result (for example
  "Coverage had both Nothing wrong and a gap ticked; recorded the gap").
- `--start-ready yes` when buildability is `Nothing wrong`; otherwise pass nothing (it is not asked).
- **Time, two plain questions in chat after screen 2** (not multiple choice: type a whole number of minutes, or
  `skip`). Ask both, in this order, only for `👍 Good, with notes` and `👎 Needs work` (the two that go on to screen
  2); a plain `👍 Good` stays one tap, and `Later` asks nothing:
  1. "How many minutes did this RFC save you versus writing it yourself? (a number, or skip)" →
     `--minutes-saved N`.
  2. "After the RFC was generated, how many minutes did you spend reviewing it and correcting it yourself? (a number,
     or skip; leave out the run itself and any pauses, which are already measured)" → `--minutes-spent N`.
  Pass only a whole number of 0 or more. Anything else, or `skip`, means pass nothing for it. If both are skipped
  (no number at all), pass `--minutes-skipped`, and never together with `--minutes-saved` or `--minutes-spent` (the command refuses it). Always ask both for the two rated screen-2 answers, so the answer is never blank.
- No other questions. If the user volunteered a comment in `$ARGUMENTS` or in a reply, pass it as `--comment`; don't
  prompt for one.

## 3b. Ask for a stopped run (two questions, one screen each)

The stage it stopped at is already known: do not ask it. No thumb, no areas, no time questions.

1. **"What was the main reason you stopped it?"** (single choice; pick the most relevant):
   - It was taking too long (`too_slow`)
   - Cost or tokens were too high (`too_costly`)
   - Wrong repos or pages were picked (`wrong_repos_or_pages`)
   - The PRD or Figma input was wrong (`bad_input`)
   - I changed my mind, or no longer need it (`changed_mind`)
   - Other (type it) (`other`)
   Text typed under Other may name more than one reason: pass it as `--note stop=<text>`, as typed, with
   `--stop-reason other`. An option picked plus typed text: pass both.
2. **"Would you run it again?"** — `Yes` (`yes`) · `No` (`no`).

Record: `--stop-reason <v> --rerun <v> [--note stop=…]`, and no `--rating`.

## 3c. Ask for a failed run (two questions, one screen each)

The run already recorded why (`cause`, `excerpt`): do not ask for the cause.

1. **"Is that what happened?"** — show `cause` and `excerpt`. `Yes, that is what happened` (`yes`) · `No, something else
   happened` (`no`; pass any text typed as `--note failure=<text>`). → `--failure-check <v>`.
   If `cause` is empty (the process was killed, so nothing was recorded), ask instead **"What made it fail?"** —
   `A usage or login limit` (`usage_or_login_limit`) · `Atlassian, Figma or another connector` (`connector`) ·
   `Connection or network` (`connection`) · `Other (type it)` (`other`, text as `--note failure=<text>`) →
   `--failure-reported <v>`.
2. **"Would you run it again?"** — `Yes` · `No` → `--rerun <v>`.

Record: `--failure-check <v>` (or `--failure-reported <v>`) `--rerun <v>`, and no `--rating`.

## 4. Record

For a stopped or failed run:

```
uv run --project "${CLAUDE_PLUGIN_ROOT}" python -m rfc_agent.feedback --run-dir "$RUN" \
  [--run-end <stopped|failed|finished>] [--stop-reason <v>] [--failure-check <yes|no>] [--failure-reported <v>] \
  --rerun <yes|no> [--note <stop|failure>=<text>]
```

Pass `--run-end` only when `end` was `unknown`. It is scored on the attempt it was given on, so a resume and a finish
later keeps this feedback and gets its own rating. For a finished run:

```
uv run --project "${CLAUDE_PLUGIN_ROOT}" python -m rfc_agent.feedback --run-dir "$RUN" --rating <up|down> \
  [--verdict <dimension>=<value> …] [--issue <dimension>=<issue> …] [--run-speed <v>] [--run-cost <v>] \
  [--start-ready <v>] [--minutes-saved <n>] [--minutes-spent <n>] [--minutes-skipped] [--note <key>=<text> …] [--comment "<their line>"] \
  [--stage <scope|investigate|draft|review|revise>]
```

`--rating up` for both 👍 options, `down` for 👎. Pass only what was answered. `--stage` only when the user said the
rating is about one stage. Quote notes and the comment safely (they may contain quotes). Relay the single line it prints. If it
says `saved locally (not sent to Langfuse: …)`, tell the user the reason in one line — the rating is not lost, and
it is not an error. Rating the same attempt again replaces the earlier rating in full: every area is rewritten, and a
gap picked before but not now is cleared (see the re-rating step above).
