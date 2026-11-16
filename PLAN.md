# PLAN — raise derive_ui (Kiwi → Playwright) conversion to ≥ 80%

On approval, step 0 copies this file to `/Users/prabhashishsingh/o_1/O1/PLAN.md` (plan mode only lets me write here).

## Context

`derive_ui` turns CONFIRMED Kiwi cases into Playwright tests. It is the costliest stage (47–78% of run cost, 30–39% of run time) and most cases never become a working test. Agreed definitions:

- **Converted** = a test is written, passes the stage check, and reaches a real verdict when run (PASS, or FAIL for a product reason — not a wrong locator / missing setup).
- **Denominator** = all CONFIRMED non-backend cases (everything not `exec:http`), including cases the generator tagged `manual`.
- **Target** = Playwright first. `maestro-automation` is a reference for practices only. Code changes go in `/Users/prabhashishsingh/o_1/O1` only.

### Current conversion rate (from `~/rfc-runs`, read-only; no runs started)

| Funnel step | HIH-1VOICE (real cases) | LABS-29776 (6 demo cases) | Both |
|---|---|---|---|
| CONFIRMED web UI cases reaching derive_ui | 8 | 6 | 14 |
| Test written (stage check passed) | 5 (62.5%) | 6 (100%, see note) | 11 (78.6%) |
| Passes when run | 3 | 0 | 3 |
| **Converted, agreed definition** | 3–5 of 8 | 0 of 6 | **3–5 of 14 = 21–36%** |

- The range is because 2 HIH failures (ENTRY-FUNC-01/02) can't be classified from artifacts as wrong-test vs feature-not-deployed.
- LABS' 100% is inflated: on retry the model used case text as element names; all 6 then timed out on the first locator.
- **The true rate on the agreed denominator is lower and not measurable today**: CONFIRMED status lives only in Kiwi, and confirmed `manual` / app-surface cases are never counted. HIH generated 99 cases: 21 web UI, 1 Android UI, 55 manual, 22 backend.

### Why cases don't convert

| # | Cause | Evidence |
|---|---|---|
| R1 | Cases never reach the stage: only `exec:ui` + `surface:web` are read; generator rule says "when in doubt, tag the weaker value (`ui` or `manual`)"; cases with no `extid:` tag are dropped silently | `derive_ui/__init__.py:35,121`, `store/kiwi.py:399-407`, generator `SKILL.md:144` |
| R2 | The check only accepts a locator that cites `<file>:<line>`. Text that comes from an API/CMS has no line to cite, so the case is skipped | 4 of 7 recorded skips; `ui_web.py:459-468`, `SKILL.md` rule 2 and 7 |
| R3 | Feature in the case isn't in the code snapshot | 3 of 7 recorded skips (HIH) |
| R4 | Written tests are wrong when run: no shared app-entry setup (city/location screen), guessed locators, wrong mocked endpoint, environment quirks. Nothing feeds run results back into the tests; fixes were made by hand | 8 of 11 written tests fail; hand edits on 11-06 and 11-11 |
| R5 | Skips are permanent in incremental mode (`testing.ui_only_new`, the default for `/test-agent:execute web`) | `derive_ui/__init__.py:137-145` |
| R6 | Slow and costly: one serial session for all cases, searches every `_snap/*` repo including backends, the one retry is often spent on citation format (`:46-52` rejected), one Kiwi tag call per case | 438 tool calls / $18.88 on LABS; `kiwi.py:400` |
| R7 | No funnel numbers: nothing counts cases lost before the model, and skip reasons are free text | `status.py:137-143` |

### What maestro-automation does that this stage lacks

- Triage every case into a fixed verdict (Draft / Draft with mock / Route elsewhere / Blocked) and re-check the blocked list against the mock rule — `agent/MAESTRO_FLOW_DRAFTER.md`.
- Shared entry flows every test reuses (`launch.yaml`, `ensure-city.yaml`, `*-launch.yaml`).
- A hand-kept list of known-good anchors and traps per module (`diagnostics-sanity/README.md`).
- Guessed selectors are allowed but marked `# UNVALIDATED`.
- Draft-to-green: run, fix, rerun, capped at 3–4 cycles — `agent/MAESTRO_DRAFT_TO_GREEN.md`.
- One bulk Kiwi tag query — `scripts/fetch_kiwi_plan.py:123-150`.

## 1. Scope

**In**
- Funnel measurement for derive_ui → execute_web on the agreed definition.
- derive_ui input widening (confirmed `manual`/web cases triaged, not dropped), skip-reason codes, locator-citation rules, shared setup, hints file, retry of old skips.
- A repair loop after `execute_web` that fixes wrong tests (not product failures).
- Generator tagging rule for `ui` vs `manual`.
- Speed: front-end-only source roots, bulk Kiwi tag fetch, batching.
- Unit tests and README for the above.

**Out**
- Any Maestro derive stage, and any edit to `maestro-automation` or `druid`.
- `O1-session-fix` copy.
- Backend stages (`derive_spec`, `execute_be`), `publish_app`.
- Re-tagging or editing existing cases in Kiwi.
- Running any workflow or Playwright run without asking you first (steps marked **ASK**).

## 2. Files to touch (all under `/Users/prabhashishsingh/o_1/O1`)

| File | Why |
|---|---|
| `src/workflows/testing/ui_funnel.py` (new) | Compute the funnel (confirmed non-backend → reached stage → written → verdict) from store + `plan.json` + `results.json`; write `qa/ui/funnel.json` |
| `src/workflows/testing/stages/derive_ui/__init__.py` | Read all confirmed non-`exec:http` cases; record pre-model drops; front-end-only roots; batching; `testing.ui_retry_skipped` |
| `src/workflows/testing/ui_web.py` | `validate`: skip `reason_code` enum, locator `basis` (`source` / `dynamic` / `live`), accept line ranges; new `classify_failure` (authoring / product / environment) |
| `src/workflows/testing/skills/qa-ui-playwright/SKILL.md` | Triage verdicts, dynamic-text locator rule, use shared setup and hints, mock re-check before skipping |
| `src/workflows/testing/skills/qa-ui-playwright/hints/` (new, one `.md` per web app) | Known-good anchors and traps (city screen, widgets that load late), like the Maestro module READMEs |
| `src/workflows/testing/stages/repair_ui/__init__.py` (new) + `stages/__init__.py`, `__init__.py:96-97` | Fix-and-rerun loop for authoring failures, max 2 cycles, off by default |
| `src/workflows/testing/stages/execute_web/__init__.py` | Store the failure class per result; let cases converted from `manual` attach to the run |
| `src/workflows/testing/store/kiwi.py`, `store/json_store.py` | Bulk tag fetch; return cases without `extid:` as a counted drop instead of silent `continue` |
| `src/workflows/testing/flags.py` | New flags: `testing.ui_retry_skipped`, `testing.ui_repair`, `testing.ui_batch_size`, `testing.ui_hints` |
| `src/workflows/testing/skills/qa-test-case-generator/SKILL.md` | Replace "when in doubt tag weaker" with a `ui` vs `manual` rubric |
| `src/workflows/testing/status.py`, `progress.py` | Show the funnel line and repair phase |
| `tests/testing/test_ui_executors.py`, `tests/testing/test_ui_funnel.py` (new) | Unit tests |
| `src/workflows/testing/README.md`, `plugins/test-agent/commands/{test,execute}.md` | Document flags, funnel, repair |

## 3. Ordered steps

Legend: **[R]** routine, fine for a smaller model. **ASK** = needs your go-ahead before it runs.

0. **[R]** Copy this plan to `O1/PLAN.md`. *Check:* file exists with the five sections.

**Phase A — measure**

1. Add `ui_funnel.py` with a pure function `funnel(cases, plan, results) -> dict` (confirmed non-backend, by exec/surface; reached; written; verdict; converted; skips by code). *Check:* new unit test feeds the HIH numbers (8 → 5 → 3) and gets them back.
2. **[R]** `kiwi.py` `list_cases`: one bulk `Tag.filter({'case__plan': id})` call (pattern from `maestro-automation/scripts/fetch_kiwi_plan.py`); return no-`extid` cases in a separate list. *Check:* unit test with a fake RPC asserts one tag call and the dropped list.
3. derive_ui `_engine`: list all CONFIRMED cases, write `qa/ui/funnel.json` pre-model section and a `ui_cases_dropped` decision per reason. *Check:* unit test: 3 cases (ui/web, manual/web, http/backend) give reached = 1, dropped = 1, excluded = 1.
4. **[R]** `status.py`: milestone line `derive_ui: N of M confirmed non-backend cases converted (X%)`. *Check:* existing status test extended.
5. **ASK** Baseline: read-only Kiwi query for plans 5 and 9 to fill the real denominator. *Check:* `funnel.json` for both runs; baseline % written into `PLAN.md`.

**Phase B — stop losing cases before and inside the stage**

6. `ui_web.validate`: require `reason_code` on skips (`no_stable_locator`, `feature_not_in_source`, `native_only`, `needs_human`, `placeholder_value`, `needs_server_state`, `other`). *Check:* unit tests for accept/reject.
7. `ui_web._source_problem`: accept `file:12-18`; add `basis`. `dynamic` = cite the component line that renders the field, locator text comes from the case, entry flagged `unvalidated: true`. *Check:* unit tests; the four LABS skip cases' shape now validates.
8. `SKILL.md`: rewrite rules 2 and 7 — triage each case (automate / automate with declared mock / skip with code); before skipping, re-check whether a declared mock makes it automatable; `dynamic` locators allowed and marked. *Check:* diff review; skill still under ~130 lines.
9. derive_ui: also pass confirmed `exec:manual` + `surface:web` cases, labelled as candidates; skill says skip with `needs_human` when no rendered element can verify them. `execute_web`: attach converted candidates. *Check:* unit test: a manual/web case lands in `tests` and is attached. (Confirm `create_run(require_tag=…)` behaviour in `store/kiwi.py` first.)
10. **[R]** `testing.ui_retry_skipped` flag: in incremental mode, re-offer skips whose code is `no_stable_locator` or `feature_not_in_source`. *Check:* unit test on the `new` list.
11. **[R]** Generator `SKILL.md:144`: rubric — `ui` when a rendered element can prove the result; `manual` only for DB, cron, analytics, third-party console, human judgement. *Check:* diff review.

**Phase C — make written tests run correctly**

12. Hints: `testing.ui_hints` → `skills/qa-ui-playwright/hints/<app>.md`, appended to the prompt. Seed `mweb.md` from the hand fixes (city/location screen from `recordings/TC-DEMO-UI-01.spec.ts`; open search directly on jupiter). *Check:* unit test: prompt contains the hints path.
13. Shared setup: skill + check allow one `tests/app.setup.ts` (location selection etc.) that cases depend on, same pattern as `login.setup.ts` in `ui_web.CONFIG`. *Check:* unit test: plan with `setup` validates; `--list` loads it.
14. `ui_web.classify_failure(row, trace)` → `authoring` (locator timeout, strict-mode violation, no failed request), `environment` (existing `RE_BLOCKED`, 5xx), else `product`. `execute_web` stores it in `results.json`. *Check:* unit tests using the LABS and HIH `results.json` details as fixtures.
15. New stage `repair_ui` (flag `testing.ui_repair`, off by default): for `authoring` failures, give the model the error, screenshot path and trace page snapshot; it edits only those specs; rerun; max 2 cycles; anything still failing becomes skip `no_stable_locator`. *Check:* unit test with a fake engine: 1 authoring failure → 1 repair call, product failures untouched.

**Phase D — speed**

16. **[R]** `source_roots`: keep only snapshots that look like front ends (has `package.json` with a router/React dep), unless `testing.ui_source_roots` is set. *Check:* unit test with fake snap dirs.
17. Batching: `testing.ui_batch_size` (default 8) — split cases by module, run sessions in parallel, merge `plan.json`, run the check once. *Check:* unit test: 20 cases → 3 batches, merged plan validates.
18. **[R]** Stage `timeout_s` set explicitly; README and command docs updated. *Check:* docs mention every new flag.

**Phase E — prove it**

19. **[R]** Run the unit suite `tests/testing`. *Check:* all green. (Local unit tests only; I'll still ask first.)
20. **ASK** Re-run derive_ui + execute_web (+ repair) on HIH-1VOICE and one more real feature. *Check:* `funnel.json` shows ≥ 80% converted; if not, the skip codes say which cause to work next.

## 4. Risks and decisions

- **Decision 1 — 80% may be out of reach on this denominator with Playwright alone.** If most confirmed non-backend cases are truly `manual` (DB, cron, analytics) or app-only, no web test can cover them. Step 5 gives the ceiling. If it is under 80%, you choose: narrow the denominator to web-verifiable cases, or bring the Maestro stage into scope.
- **Decision 2 — repair loop touches a live environment.** `repair_ui` reruns failing tests against the named environment (up to 2 extra runs). Kept off by default; needs your OK per run.
- **Decision 3 — `dynamic` locators loosen the "wrong test is worse than none" rule.** They are marked unvalidated and only count once they run; without repair enabled they can add failing tests.
- **Risk — manual-tagged cases in a web run.** `execute_web` currently requires `exec:ui`; attaching converted `manual` cases without re-tagging Kiwi needs a small store change (step 9). If that is not acceptable, the alternative is re-tagging in Kiwi, which is out of scope here.
- **Risk — small sample.** The baseline rests on 14 cases, 6 of them demo cases. Numbers will move after step 5.
- **Risk — parallel batches** can write conflicting shared setup/login files; step 17 has the first batch own them.

## 5. Routine steps (smaller model)

0, 2, 4, 10, 11, 16, 18, 19 — marked **[R]** above. Also the unit tests inside steps 6, 7 and 14 once the function signatures exist.

Steps needing the main model: 1, 3, 7 (design of `basis`), 8, 9, 13, 15, 17.

## Verification

- Unit: `tests/testing/test_ui_executors.py`, `test_ui_funnel.py`, `test_progress.py`.
- End to end (**ASK** first): `/test-agent:execute web` on HIH-1VOICE with `--enable testing.ui_retry_skipped,testing.ui_repair`; read `qa/ui/funnel.json` and `results.json`.
