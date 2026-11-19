---
description: QA test cases from an RFC run, end to end from chat — preflight, generate, upload to Kiwi for review, prerequisites; then backend, web UI (Playwright) and app UI (Maestro) execution once you have confirmed the cases in Kiwi
argument-hint: [--feature KEY] [--rfc-run-dir <rfc run>] [--run-dir <testing run, to resume>] [--contract F] [--plan F] [--test-data F] [--figma F]
---

Run the harness `testing` workflow for: `$ARGUMENTS`

YOU are its progress view and its question UI. The run has two phases split by a human review in Kiwi:

- **A. Generate** (~30 min, background): sources → generate_cases → render_xlsx → store_cases →
  prerequisites. Cases land in Kiwi as PROPOSED. Scoring the suite (`evaluate`: the rubric and the 14
  compliance checks, ~15 min) is not part of it — it runs on demand with `/test-agent:evaluate`.
- **Review**: the user approves cases in Kiwi (status CONFIRMED). This can take a day; nothing waits on it.
- **B. Execute** (background), on CONFIRMED cases only: backend (`exec:http`, derive_spec → execute_be), web UI
  (Playwright: derive_ui → execute_web) and app UI (publish_app records a Maestro run from a device).

Ask with AskUserQuestion, never in prose. Never answer a question for the user. No browser pane.

## 0. Locate the plugin's project

This plugin runs the harness shipped by the `rfc-agent` plugin of the same marketplace. Resolve it once:

```
python3 -c 'import json,os; print(os.environ.get("TEST_AGENT_PROJECT") or json.load(open(os.path.expanduser("~/.claude/plugins/known_marketplaces.json")))["rfc-agent-marketplace"]["installLocation"])'
```

Call the printed path `PROJ` and write it out literally in every command below (no `PROJ=` assignments, no
`cd` in front). If it fails, tell the user to install rfc-agent first (`/rfc-agent:setup`) and stop.

Every helper is `uv run --project <PROJ> python -m workflows.testing.<module> …`.

## 1. Resume or start

If `$ARGUMENTS` has `--run-dir R` and `R/manifest.json` exists, this is a **resume**: run
`… status R --json` and go by its `next.step`:

| `next.step` | Go to |
|---|---|
| `start` / `resume_generate` | section 3 (launch phase A with the same `--run-dir`; finished stages are skipped) |
| `fix` | Post `next.why.message` as it is (never the raw gate failures). When `next.why.retry` is true, ask `Resume from <stage>` (section 3, same `--run-dir`) or `Later`; otherwise also show the stage's `stages.<stage>.failures` and offer `Rerun` or `Stop` |
| `store` | section 4b |
| `review` | section 5 (the handoff, then the execute question) |
| `execute` | section 6 |
| `done` | section 7 (report the last results) |

With no `--run-dir`, first run `… preflight runs --json --limit 3` (unfinished testing runs, newest first;
add `--rfc-run-dir D` / `--feature F` when those were given). If it lists any, ask **"Continue a testing run,
or start a new one?"** with one option per run (label = `feature` and the folder's date, description =
`label`, e.g. "waiting for review in Kiwi") and `Start a new run`. A run picked is a resume of its `path`, as
above. Never skip this question when runs are listed: a new run regenerates the whole suite (~30 minutes).

Otherwise it is a **new run**: continue with section 2.

## 2. Preflight and inputs (seconds)

Ask for the two inputs one at a time, RFC run first: the feature keys offered depend on the run chosen.
An argument given with no value (`--feature` alone) counts as not given.

1. **RFC run.** From `--rfc-run-dir`, else run `… preflight recent --json` and ask which one, one option per
   row (label = folder name, description = the RFC title), newest first, at most 4.
2. **Feature key.** From `--feature`, else run `… preflight features --rfc-run-dir D --json` — keys from
   THAT run only: one an earlier testing run of it already used (reusing it updates that Kiwi plan instead
   of creating a second one), its Jira key, the RFC title's short name. Ask with one option per row
   (label = `key`, description = `why`), in the order given; "Other" takes any key. Never offer keys from
   other RFC runs. When the list is empty, ask with free text only.
3. **Check.** Run `… preflight check --feature F --rfc-run-dir D [--contract …] [--plan …] [--test-data …]
   [--figma …] --json` (pass the overrides only when given). Post the checks as a short checklist, one line
   each: ✅ ok, ℹ️ info, ⚠️ warn, ❌ fail. Then act on the failures:

| Failed check | What to do |
|---|---|
| `config`, `claude`, `langfuse`, `kiwi_client`, `rfc`, `prd`, `rfc_run_dir` | Relay its `fix` and stop. Nothing has run. |
| `kiwi` with "testing.storage is 'json'" | Kiwi is not set up on this machine. Ask for the Kiwi web URL and the product prefix (e.g. `Sandbox / `), and the agent's Kiwi username, then run `… preflight setup-kiwi --kiwi-web URL --product-prefix "P" [--kiwi-user U]`. It writes those non-secret settings to `TESTING` in config.json — say so. Then re-run the check. |
| `kiwi_conf` | There is no Kiwi login. The user creates `~/.tcms.conf` themselves (`[tcms]` url / username / password, then `chmod 600`). **Never** ask for, read, write or echo the password. Stop until they say it is done, then re-run the check. |
| `kiwi` unreachable | Ask **"Kiwi is unreachable — how do you want to go on?"** with: `Retry` (after connecting the VPN; re-run the check), `Generate now, store later` (phase A without the upload — section 3 with `--disable testing.store`; the handoff offers the upload), `Stop`. |

   Warnings do not stop the run. Say in one line what each costs: no contract → backend cases come out
   `manual` and nothing is executable; no test data → invented values, and the prerequisites stage writes a
   template to fill.
4. Use `resolved.feature` as `F` from here on: the check normalises the key (`hdpo` → `HDPO`) so every run of
   a feature names the same Kiwi product.
5. If `resolved.existing_runs` is non-empty, this feature already has an unfinished run from this RFC run.
   Ask **"<F> already has a run from this RFC — continue it?"** with `Continue <folder name>` (description =
   its `label`; a resume of its `path`, section 1) and `Start fresh` (description: regenerates the suite,
   ~30 min; cases already reviewed in Kiwi stay as they are).
6. Use `resolved.run_dir` from the check as `R` (a fresh `~/rfc-runs/<date>-testing-<feature>` folder).

## 3. Phase A — launch

Say once: *"Generating the suite in the background — about 30 minutes. I'll post each stage as it
finishes."* Then:

```
mkdir -p "<R>/logs"
```

and, with `run_in_background`, ONE command that begins with the label (the task list names a background
shell by the start of its command):

```
TASK="testing · <R folder name>" uv run --project <PROJ> harness run testing --feature F --rfc-run-dir D --run-dir <R> --no-interactive [overrides] [--disable testing.store] > "<R>/logs/run.generate.out" 2>&1
```

In the same turn, start a **Monitor** (`timeout_ms` 1800000, description `testing run events`):

```
uv run --project <PROJ> python -m workflows.testing.progress <R> --watch
```

Don't poll and don't read its `run.<kind>.out` while the run is live (its progress spinner is one endless line).

## 4. Relay progress

Each Monitor event is an activity line `[hh:mm:ss] [<stage>] …`, a `PROGRESS …` line, or `WATCH rearm …`:

- `STAGE started` → one line: `▶ <stage>` with what it does (sources: collecting inputs · generate_cases:
  writing the suite, the long one, ~20 min · render_xlsx: workbook · store_cases: uploading to Kiwi · prerequisites: what prestag needs).
- `PROGRESS <stage> · …` → post the rest of the line as it is, with ⏳ in front, e.g. `⏳ generate_cases · 14m
  of usually 19–22m · writing cases: 61 so far · “Now batch 4: VALSVC module (19 cases)”`. Never add a guess
  of your own about how far along it is. A line that says "no output for … look at logs/<stage>.stderr" is
  the one to act on: read the last 20 lines of that file and say in one line what it shows.
- `gate PASSED` → run `… status <R> --milestone <stage>` and post its line with ✓ in front.
- `gate FAILED … automatic revision next` → `↻ <stage> gate failed (<n> failures) — revising once
  automatically`. Not an error yet.
- `ENGINE FAILURE` / a `gate FAILED` with no retry left → wait for the background shell to exit (section 4a).

Group events that arrive together into one message. A Monitor lives 30 min and phase A takes longer, so the
watcher ends itself after 25 with `WATCH rearm --from <offset>`: if the run is still going, start a new
Monitor with the same command plus `--from <offset>` (nothing is missed or repeated). Say nothing about the
re-arm. Stop the Monitor (TaskStop) when the run exits.

### 4a. The run exited

Run `… status <R> --json`. Each launch writes its own log (`<R>/logs/run.<kind>.out`: `generate`,
`store`, `backend`, `web`, `app`), so an earlier launch's log is still there to read.

- `next.step` is `review` or `store` → section 5.
- `next.step` is `fix` → post `next.why.message` as it is: it says in plain words why the stage stopped
  (a dropped connection reads as one, not as "declared output missing"). Read the last ~30 lines of that
  launch's log (`tail -n 30 … | tr '\r' '\n' | tail -n 30`) only when `next.why.kind` is `engine` or the
  message is not enough. If `store_cases` failed on a connection error, Kiwi dropped mid-run: offer
  `Retry the upload` (section 4b) or `Later`. When `next.why.retry` is true offer `Resume from <stage>`
  (section 3, same command: finished stages are skipped) or `Later`; otherwise `Rerun` or `Stop`.

### 4b. Upload to Kiwi (phase A ran without it, or the upload failed)

Re-run `… preflight check … --json`; if `kiwi` still fails, say so and stop (the user can resume later with
`/test-agent:test --run-dir <R>`). Otherwise, in the background and monitored as in section 3:

```
TASK="testing · <R folder name> · store" uv run --project <PROJ> harness run testing --feature F --rfc-run-dir D --run-dir <R> --stages store_cases,prerequisites --no-interactive > "<R>/logs/run.store.out" 2>&1
```

(`prerequisites` is skipped when already green.) Then section 5.

## 5. 🔔 The handoff — cases are ready for review

From `… status <R> --kiwi --json`, post ONE message, in this order:

1. **Cases are generated.** `<total>` cases in `<modules>` modules — by execution (`http` / `manual` / `ui`)
   and priority. Time and cost of phase A (`duration_s`, `cost_usd`).
2. **Quality.** When `eval` is set (the suite was scored earlier with `/test-agent:evaluate`): evaluator
   score `<overall>/5` (coverage, quality) and compliance `<compliance>`, naming the failed checks in plain
   words (`structural_completeness` → "some preconditions are a bare n/a"). Otherwise one line: *"Not scored
   yet — `/test-agent:evaluate` rates it against the rubric and the 14 compliance checks (~15 min), any
   time."*
3. **In Kiwi.** `<store.total>` cases in `<store.product>` as PROPOSED — link `store.plan_url`. If
   `store.protected` > 0: that many cases were already reviewed and were left as they are (a suggestion
   comment shows what changed). If `kiwi.stale` is non-empty: the plan also holds that many cases from
   earlier runs that this suite no longer has — they can be set to DISABLED.
4. **Prerequisites before anything can run.** Services to deploy (`prerequisites.services`), the counts
   (fixtures, config, external systems, secrets — secrets by name only), how many fixtures nobody knows how to
   create yet (`fixtures_unknown_how`), and the top blockers (`top_blockers`: id, entity, how many cases need
   it). Point to `qa/prerequisites.md` for the full checklist.
5. **Test data.** If `test_data_supplied` is false: the suite uses invented values. To use real ones, fill
   `qa/test_data.template.yaml` and re-run generation with `--test-data <filled file>` (cases already reviewed
   in Kiwi stay protected), or correct the values in Kiwi while reviewing.
6. **What to do now:** *"Review the cases in Kiwi and set the ones you approve to **CONFIRMED** (reject with
   DISABLED). Only CONFIRMED cases run: `http` cases against the API, web `ui` cases in a browser
   (Playwright), app `ui` cases from a Maestro run on your device. `manual` cases are yours to run by hand
   in the Kiwi test run — or an agent can run the ones tagged `surface:web` / `surface:android` (`--include-manual`)."*

Send the files with SendUserFile (one call): the `xlsx`, `qa/prerequisites.md`, `qa/test_data.template.yaml`.

Then ask **"Ready to execute the confirmed cases?"** with: `I've confirmed — execute`, `Check Kiwi status`,
`Later`.

- `Check Kiwi status` → re-run `… status <R> --kiwi --json`, post `kiwi.by_status` as one line per status
  (e.g. `CONFIRMED: 41 http · 2 manual`), and ask the same question again.
- `Later` → say they can come back any time with `/test-agent:test --run-dir <R>`, which lands here.
- `I've confirmed — execute` → section 6.

## 6. Phase B — execute

Three kinds of case can run. Each reads only CONFIRMED cases from Kiwi and records one execution per case
in the SAME Kiwi test run, `<feature> · <env> · <build>`:

| Kind | Cases | Stages | Needs |
|---|---|---|---|
| Backend | `exec:http` | derive_spec → execute_be | a captured curl |
| Web UI | `exec:ui` + `surface:web` | derive_ui (an agent writes one Playwright test per case, locators cited from the RFC's repo snapshot) → execute_web | the web app's base URL, a recorded login |
| App UI | `exec:ui` + `surface:android`/`ios` | publish_app (records a Maestro run you did on a device) | the Maestro JUnit report, the flows folder |
| Manual, by an agent | `exec:manual` + `surface:web`/`android` | `/test-agent:execute ui-agent --include-manual` (an agent drives a browser or the emulator through the case) | the same as the web / Android agent |

1. **Gate.** `… status <R> --kiwi --json`. If `kiwi.error`: Kiwi is unreachable — every kind needs it. Say so
   and stop; offer `Retry` once. If `kiwi.by_status` has a `conflict` exec tag: those cases carry two
   `exec:` tags from an older upload; re-running the upload (section 4b) fixes the ones nobody reviewed.
   Post one line per kind: `backend: <confirmed_http> of <http_total> CONFIRMED · web UI:
   <confirmed_web_ui> CONFIRMED · app UI: <confirmed_app_ui> CONFIRMED · manual an agent can run:
   <confirmed_manual_agent>`. If all four are 0, nothing would run: say how many are still PROPOSED and go
   back to the question in section 5. If only `confirmed_manual_agent` is above 0, do not stop: ask
   **"The confirmed cases are manual. Run them with an agent, or leave them for a person?"** with
   `Run with an agent` (hand over to `/test-agent:execute ui-agent --include-manual --run-dir <R>`, which asks
   for the environment and login itself) and `Leave them` (stop; they are in the Kiwi test run to do by hand).
   A manual case with no `surface:` tag cannot be picked up: say to tag it `surface:web` or `surface:android`.
2. **Which kinds.** One AskUserQuestion, `multiSelect: true`, one option per kind with a CONFIRMED count above
   0 (description = the count and what it needs). Then one more AskUserQuestion with two questions:
   - the **environment**: run `… preflight envs --json` and offer up to 4 of its `env` names (description =
     its `base_url`; "Other" takes any name). For an app-only run, the device instead (e.g. `emulator-5554`).
     When no environments are configured, offer to set them up once:
     `… preflight setup-web --environments <a,b,…> --base-url-template "https://{env}.<domain>"`.
   - the **build** (a version or git sha, or `unknown`) — free text via "Other".
3. **Inputs, only for the kinds chosen.** Every file below carries a login or a session: **never read,
   print or copy one** — check only that it exists and is non-empty (`test -s <path> && echo ok`).
   - **Backend:** the path of a captured curl against the target host (DevTools → Network → a request to
     that host → Copy as cURL → save to a file, e.g. `~/.secrets/curls/prestag.txt`).
   - **Web UI:**
     - **Base URL:** the chosen environment's `base_url` from `preflight envs`; ask only when it has none.
     - **Login:** when preflight's `ui_login` check is ✅, use that file (`--ui-login`) without asking. When
       it is ⚠️ (none set), ask how the run logs in — `Test account (number + OTP)` (recommended: the run
       logs itself in every time, nothing expires) or `Recorded session`. When it is ❌, relay its fix. **Never
       ask for the number or OTP in chat**: the user writes them to a file themselves; you pass only its path.
       - Test account: give them these to run, then store the path once with
         `… preflight setup-web --ui-login <file>` and re-run the check:

         ```bash
         mkdir -p ~/.secrets/playwright && chmod 700 ~/.secrets ~/.secrets/playwright
         ```

         ```bash
         printf '{"phone": "<test number>", "otp": "<otp>"}\n' > ~/.secrets/playwright/<env>-login.json && chmod 600 ~/.secrets/playwright/<env>-login.json
         ```

       - Recorded session → `--ui-auth <file>`, recorded with
         `cd <PROJ>/tools/ui-runner && npx playwright codegen --save-storage=$HOME/.secrets/playwright/<env>.json <base url>`
         (a browser opens; they log in, then close it).
     - **Watch it?** Ask `Watch the browser` (a visible, slowed-down browser on this Mac, one test at a time:
       add `--enable testing.ui_headed`) or `Run in the background`.

     If preflight warned `playwright`, relay its fix first.
   - **App UI:** the run happens on their device, not here. If they have not run it yet, give them the
     steps (the flows live on druid's `AND-106907` branch; a worktree keeps their checkout as it is):

     ```bash
     git -C ~/o_1/druid worktree add ~/o_1/druid-maestro origin/AND-106907
     cd ~/o_1/druid-maestro && maestro test .maestro/flows/<suite> --format junit --output report.xml
     python3 scripts/maestro_report.py --report report.xml
     ```

     Then ask for the **report** path and the **flows folder** (`~/o_1/druid-maestro/.maestro/flows`).
4. **Launch** each chosen kind in turn, in the background, monitored exactly as in section 3 (one Monitor
   per launch). Same `TASK=` label rule; `<common>` is `--feature F --rfc-run-dir D --run-dir <R> --env <env>
   --build <build> --no-interactive`:

   - Backend: `TASK="testing · <R folder name> · backend" uv run --project <PROJ> harness run testing <common> --stages derive_spec,execute_be --enable testing.execute --curl <path> > "<R>/logs/run.backend.out" 2>&1`
   - Web UI: `TASK="testing · <R folder name> · web" uv run --project <PROJ> harness run testing <common> --stages derive_ui,execute_web --enable testing.execute_web [--base-url <url>] [--ui-login <path> | --ui-auth <path>] [--enable testing.ui_headed] > "<R>/logs/run.web.out" 2>&1`
   - App UI: `TASK="testing · <R folder name> · app" uv run --project <PROJ> harness run testing <common> --stages publish_app --enable testing.publish_app --maestro-report <report> --maestro-flows <flows> > "<R>/logs/run.app.out" 2>&1`

   Relay as in section 4. `derive_ui` is an agent (a few minutes): after its ✓ line, post the skipped cases
   from `status --json` → `ui_plan.skipped` (id: reason) — those stay in the Kiwi run for a person.
   If execute_be stops with "preflight failed", the target host was not reachable with that curl (VPN, an
   expired session): offer `Retry with a fresh curl`. If execute_web's cases come back mostly BLOCKED with
   `net::ERR_` or a navigation timeout, the base URL is unreachable from here (VPN). If they are BLOCKED
   with "not run — the login step failed", the login failed (a wrong number/OTP, or the login page
   changed): show that detail; with a recorded session, offer `Retry with a fresh login`.

## 7. Finish

From `… status <R> --json` (`results`, `results_web`, `results_app` — report each kind that ran):

- the tally (PASS / FAIL / BLOCKED / ERROR) and the Kiwi test run link (`run_url`); for web UI also the
  local HTML report, with each failure's screenshot and step-by-step trace:
  `cd <PROJ>/tools/ui-runner && npx playwright show-report <results_web.html_report>`;
- each case that did not pass: id, status, one-line detail — FAIL is a defect, BLOCKED is missing data or
  environment (check `qa/prerequisites.md`), ERROR is the harness. For web UI, the failure screenshot and
  trace are attached to the execution in Kiwi; for app UI, the step timeline is in the execution's comment;
- skipped: `skipped_unconfirmed` (not CONFIRMED), `skipped_wrong_execution` (wrong `exec:` tag), web cases
  derive_ui left for a person, app cases no flow covered (`results_app.unattributed`);
- time and cost of the whole run.

Then one line: manual cases, and UI cases left for a person, are in the same Kiwi test run to execute by
hand.
