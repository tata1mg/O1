---
description: Rate an rfc-agent run — a thumb, and what was wrong in each area; saved on that run's Langfuse trace so it can be charted later
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
empty). Show it in the run question so the answer is anchored on the numbers.

## 3. Ask (AskUserQuestion — skip any answer already given in `$ARGUMENTS`)

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
| `coverage` | are all PRD and Figma flows handled, and are the "covered" claims true? | missing PRD flow (`missing_prd_flow`) · missing Figma flow or state (`missing_figma_flow_or_state`) · marked covered but is not (`marked_covered_but_not_addressed`) | flow not needed (`unneeded_flow_included`) |
| `buildability` | could a coding agent build from `rfc.md` without guessing? | too thin to build from (`too_thin_to_build_from`) · ambiguous interface or contract (`interface_or_contract_unclear`) · missing build order or dependency (`build_order_or_dependency_missing`) | a citation does not support its claim (`citation_does_not_support_claim`) |
| `approach` | does the chosen option fit our infra and engineering standards? | ignores existing infra (`ignores_existing_infra`) · ignores engineering standards (`ignores_engineering_standards`) · tradeoff not justified (`tradeoff_not_justified`) | missing alternative (`missing_alternative`) · a generic industry choice over local fit (`generic_industry_choice_over_local_fit`) |
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
- No other questions. If the user volunteered a comment in `$ARGUMENTS` or in a reply, pass it as `--comment`; don't
  prompt for one.

## 4. Record

```
uv run --project "${CLAUDE_PLUGIN_ROOT}" python -m rfc_agent.feedback --run-dir "$RUN" --rating <up|down> \
  [--verdict <dimension>=<value> …] [--issue <dimension>=<issue> …] [--run-speed <v>] [--run-cost <v>] \
  [--start-ready <v>] [--note <key>=<text> …] [--comment "<their line>"] \
  [--stage <scope|investigate|draft|review|revise>]
```

`--rating up` for both 👍 options, `down` for 👎. Pass only what was answered. `--stage` only when the user said the
rating is about one stage. Quote notes and the comment safely (they may contain quotes). Relay the single line it prints. If it
says `saved locally (not sent to Langfuse: …)`, tell the user the reason in one line — the rating is not lost, and
it is not an error. Rating the same run again replaces the earlier rating in full: every area is rewritten, and a
gap picked before but not now is cleared.
