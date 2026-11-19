# PLAN — execute a Kiwi test plan directly, without an RFC run

Separate from `PLAN.md` (derive_ui conversion). The two can ship independently.

## Context

`/test-agent:execute` runs the CONFIRMED cases of an **existing testing run folder**. That folder is normally created by `/test-agent:test`, which starts from an RFC run. A QA who already has a plan in Kiwi cannot execute it without going through generation first.

What blocks it today (the RFC run itself is already optional in code):

| Blocker | Where |
|---|---|
| `execute` only finds folders named `*-testing-*` that contain `qa/sources.json`; otherwise "no testing run found" | `src/workflows/testing/execute.py:51-55, 146-147` |
| The feature name is read only from `qa/sources.json`; there is no `--feature` argument | `execute.py:65-68, 74` |
| `derive_spec` (backend) requires the file `qa/store.json` to exist. It does not read it — cases come from the store — so only the file's presence matters | `stages/derive_spec/__init__.py:26-27, 76` |
| `derive_ui` (web) takes locators from the RFC run's `_snap/`; without it, `testing.ui_source_roots` must be set | `stages/derive_ui/__init__.py:43-51, 163-167` |
| The Kiwi plan is looked up by name: product `<product_prefix><feature>`, plan `{feature} — imported test cases`, falling back to the product's first plan. There is no way to name a plan id | `store/kiwi.py:218-242` |
| Cases must carry `extid:`, `exec:` and `surface:` tags and be CONFIRMED; a hand-written Kiwi plan without them yields zero cases, silently | `store/kiwi.py:399-407` |

## 1. Scope

**In**
- `execute --feature <F>` creates a minimal testing run folder when none exists, then runs as today.
- Optional `--kiwi-plan <id>` to target a plan by id instead of by name.
- A clear pre-run report when the plan's cases lack the required tags or CONFIRMED status.
- Command doc and unit tests.

**Out**
- Generating, uploading or editing cases in Kiwi (including adding missing tags).
- Changes to `derive_ui` conversion (that is `PLAN.md`).
- Maestro / `publish_app`.
- The `O1-session-fix` copy.
- Any test run without asking first (steps marked **ASK**).

## 2. Files to touch (under `/Users/prabhashishsingh/o_1/O1`)

| File | Why |
|---|---|
| `src/workflows/testing/execute.py` | New `--feature`, `--kiwi-plan`, `--source-roots`; `bootstrap_run()` that creates the folder and the two marker files; pass the new flags to the harness command |
| `src/workflows/testing/store/kiwi.py` | `_plan` (and `_product`) honour a plan id when given |
| `src/workflows/testing/store/__init__.py` | Pass the new flag into the Kiwi store constructor |
| `src/workflows/testing/flags.py` | New flag `testing.kiwi_plan_id` (default empty) |
| `plugins/test-agent/commands/execute.md` | Document the direct path and what to ask for when a value is missing |
| `src/rfc_agent/axon_ext/test-command.js` | Same wiring for the Axon version, only if it builds the execute command itself (check first) |
| `tests/testing/test_ui_executors.py`, `tests/testing/test_kiwi_store.py` | Unit tests |
| `src/workflows/testing/README.md` | One section: "Execute an existing Kiwi plan" |

## 3. Ordered steps

Legend: **[R]** routine, fine for a smaller model. **ASK** = needs your go-ahead before it runs.

1. **[R]** Confirm nothing else reads fields from `qa/store.json` or `qa/sources.json` that a minimal file would lack (grep `status.py`, `progress.py`, `preflight.py`, `evaluation.py`). *Check:* a list of every reader and the keys it uses, added to this file.
2. `execute.py`: add `bootstrap_run(root, feature, plan_id) -> Path`. Creates `<RUNS_ROOT>/<YYYY-MM-DD>-testing-<feature>/`, writes `qa/sources.json` as `{"feature": F, "rfc_run_dir": "", "sources": [], "origin": "kiwi-direct"}` and `qa/store.json` as `{"feature": F, "origin": "kiwi-direct", "plan_id": id}`. Reuses an existing folder for the same feature. *Check:* unit test: folder and both files exist; a second call returns the same folder.
3. `execute.py` `main`: add `--feature`, `--kiwi-plan`, `--source-roots`. With `--feature` and no matching run, call `bootstrap_run`; otherwise behave exactly as today. *Check:* unit test: `command("web", run_dir, …)` on a bootstrapped folder returns a harness line with `--feature F` and no `--rfc-run-dir`.
4. **[R]** `flags.py`: add `testing.kiwi_plan_id`. `execute.command` adds `--set testing.kiwi_plan_id=<id>` and `--set testing.ui_source_roots=<paths>` when given. *Check:* unit test on the built command.
5. `store/kiwi.py`: when `kiwi_plan_id` is set, `_plan` returns `TestPlan.filter({"id": id})[0]` and `_product` takes the product from that plan; raise a clear error when the id doesn't exist. *Check:* unit test with the fake RPC in `test_kiwi_store.py`: plan resolved by id, name lookup not called.
6. `execute.py`: web without an RFC run and without `--source-roots` / `testing.ui_source_roots` adds `--source-roots (the web repo checkout)` to the `missing` list. *Check:* unit test: `SystemExit` message names it.
7. `execute.py --pending`: also report cases in the plan that are not usable — not CONFIRMED, no `extid:`, no `exec:` tag — as counts with up to 5 example case ids. *Check:* unit test with a fake store: 1 good, 1 untagged, 1 unconfirmed → counts 1/1/1.
8. **[R]** `commands/execute.md`: step 3 gains "no testing run and `--feature` given → bootstrap"; missing source roots are asked for like the curl path. *Check:* doc review.
9. **[R]** README section and argument-hint line updated. *Check:* every new argument is documented.
10. **[R]** Run the unit suite `tests/testing`. *Check:* all green. (Local unit tests; I'll still ask first.)
11. **ASK** One real direct run: `execute web --feature <F> --kiwi-plan <id> --source-roots <web repo> --env <E> --build <B> --print` first, then the real run if the printed command looks right. *Check:* a Kiwi run `<feature> · <env> · <build>` exists with executions; `qa/ui/results.json` is written.

## 4. Risks and decisions

- **Decision 1 — untagged plans.** A plan written by hand in Kiwi has no `extid:` / `exec:` / `surface:` tags, so nothing runs. This plan only reports that (step 7). The alternative is a one-off tagging helper that writes to Kiwi; say if you want it added.
- **Decision 2 — which web repo.** Without an RFC run there is no code snapshot. You pass a local checkout; tests are derived against whatever branch it is on, which may not match the build on the environment.
- **Risk — backend needs a captured curl** exactly as today (`--curl` or `testing.curl`); direct mode doesn't change that.
- **Risk — `--stages` on a fresh folder.** `execute` already passes `--force`, and the harness only checks each stage's `requires` files, so a bootstrapped folder should be accepted. Not yet proven; step 11's `--print` plus a dry look at `harness/cli.py:127, 229` settles it before any real run.
- **Risk — plan-id and feature disagree.** If `--kiwi-plan` points at a plan under a different product, runs would be named after the wrong feature. Step 5 takes the product from the plan to avoid this.

## 5. Routine steps (smaller model)

1, 4, 8, 9, 10 — marked **[R]** above. Also the unit tests in steps 2, 3, 6 and 7 once the function signatures exist.

Steps needing the main model: 2, 3, 5, 7.

## Where results appear (unchanged)

- Kiwi: test run `<feature> · <env> · <build>`; failed executions carry a comment with the reason and screenshot; skipped cases stay IDLE.
- Run folder: `qa/ui/results.json` (web, includes the Kiwi run link), `qa/results.json` (backend), `qa/ui/html-report/`, `logs/run.web.out`, `logs/run.backend.out`.
