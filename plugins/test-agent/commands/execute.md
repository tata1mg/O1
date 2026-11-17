---
description: Execute the CONFIRMED cases of an existing testing run — web UI (Playwright), backend (HTTP), or both — recorded in Kiwi; run the already-written web tests at once, or write tests for newly confirmed cases first; or have an agent drive a browser through each web case (ui-agent), also straight from a Kiwi plan with no earlier run
argument-hint: [web|backend|ui-agent|all] [--only-existing] [--run-dir <testing run>] [--feature F --kiwi-plan ID] [--env E] [--build B] [--base-url U] [--ui-auth FILE] [--hosts H] [--headed] [--cases TC-…,TC-…] [--curl FILE]
---

Execute cases for: `$ARGUMENTS`

1. **Project.** Resolve `PROJ` once and write it literally below:

   ```
   python3 -c 'import json,os; print(os.environ.get("TEST_AGENT_PROJECT") or json.load(open(os.path.expanduser("~/.claude/plugins/known_marketplaces.json")))["rfc-agent-marketplace"]["installLocation"])'
   ```

   Every helper is `uv run --project <PROJ> python -m workflows.testing.execute …`. With no kind, the kind is
   `web`.

2. **Web: run now, or write new tests first?** When the kind includes `web` and `--only-existing` was not
   given, run `… execute --pending [--run-dir R]` (it reads Kiwi). Then:
   - `error` set → Kiwi is unreachable; say so and stop.
   - `tests` empty and `new` empty → no CONFIRMED web case; say so and stop.
   - `new` empty → nothing to write: run as is (derive_ui makes no model call).
   - `new` non-empty → ask **"N newly confirmed web cases have no test yet — run the existing tests now, or
     write theirs first?"** with `Run the <len(tests)> existing now` (add `--only-existing`; no model, starts
     at once) and `Write <len(new)> new tests first` (~1–2 min per case, then everything runs). When `tests`
     is empty there is nothing to run yet: say the tests are being written, and go on without asking.

2a. **`ui-agent`: what will run.** When the kind is `ui-agent`, skip step 2 and run
   `… execute ui-agent --pending [--run-dir R | --feature F [--kiwi-plan ID]]` (it reads Kiwi). Then:
   - `error` set → Kiwi is unreachable, or the plan id does not exist; say so and stop.
   - `will_run` empty → no CONFIRMED web case with an expected result; say so and stop.
   - Otherwise post one line: `<len(will_run)>` cases an agent will execute in a browser, in batches of 5;
     and, when non-empty, the `no_expected_result` ids — these are not executed and stay IDLE in the Kiwi run
     until someone writes what they expect.
   - Say once, before the first `ui-agent` run of a session: each case is a model session on every run
     (cost and time are per case), the agent acts on the environment as a real user, and it logs in only
     from a recorded session (`testing.ui_auth` / `--ui-auth`), never from a number and OTP — without one the
     cases run logged out. `--headed` shows the browser and runs the cases one at a time.
   With `--feature` (and optionally `--kiwi-plan <id>`) no earlier run is needed: the run folder is created
   for that feature and the Kiwi plan is read as it is — a case with no `extid:` tag is shown as
   `KIWI-<case id>`. Only `ui-agent` works on such a run.

3. **Preview.** Run `… execute $ARGUMENTS [--only-existing] --print`. It picks the newest testing run (or
   `--run-dir`) and takes the feature, RFC run, environment and build from it.
   - `missing …` → ask for exactly what it names (AskUserQuestion; free text via "Other"). For a missing
     curl, ask for the file's **path** and never read or print it; offer to store it once with
     `… python -m workflows.testing.preflight setup-web --curl <path>`.
   - `ENGINE FAILURE … not a named test environment` (after the run starts, `ui-agent` only) → the base
     address is not one the agent may drive. Ask which host it is and re-run with `--hosts <host>`; never
     accept production. `--base-url <origin>` and `--ui-auth <session file>` are passed the same way when
     config does not already hold them.
   - `ENGINE FAILURE … needs Chromium build` → post the two fixes the message names and ask which.
   - Otherwise post the run dir, kind(s), env and build in one line. When the kind includes `web` and
     `--headed` was not given, ask once: `Watch the browser` (add `--headed`) or `Run in the background`.

4. **Run** in the background (`run_in_background`), one command starting with the label:

   ```
   TASK="testing · execute · <kind>" uv run --project <PROJ> python -m workflows.testing.execute <same arguments>
   ```

   and in the same turn a **Monitor** (`timeout_ms` 1800000) on the run's activity log:

   ```
   uv run --project <PROJ> python -m workflows.testing.progress <run dir> --watch
   ```

   On each `gate PASSED`, post `… python -m workflows.testing.status <run dir> --milestone <stage>` with ✓
   (after derive_ui, also the cases it skipped, from `status --json` → `ui_plan.skipped`). For `ui-agent`
   the `PROGRESS execute_ui_agent · …` line says how many verdicts are in. Post each
   `PROGRESS <stage> · …` line as it is with ⏳ in front (derive_ui's says how many tests it has written).
   On `WATCH rearm --from <offset>`, if the command is still running, start the same Monitor again with
   `--from <offset>`. Stop the Monitor when the command exits.

5. **Report** from `… python -m workflows.testing.status <run dir> --json` (`results_web` / `results`): the
   tally, the Kiwi run link, each case that did not pass (id, status, one-line detail; for web, the failed
   requests are in its Kiwi comment), and for web the HTML report:
   `cd <PROJ>/tools/ui-runner && npx playwright show-report <results_web.html_report>`.

For `ui-agent`, report from `results_agent`: the tally, `parts_met` of `parts`, the Kiwi run link, and
ALWAYS the per-case table in `results_agent.table` as it is — Case | Verdict | Time | Cost | What was
found, with its Total row. Never drop the Time column or summarise the table away: how long each case took
is part of the result (a FAIL's time includes its second run; a cost of `unknown` means the session was cut
off before reporting it). The same table is in `<run dir>/qa/agent/results.md`. Each row's last cell is one
line of the form `FAIL — 2 of 3 parts met; B not met (…); cause: environment`. There is no partial pass: a case is PASS only when every part is met; say how close
it came with the parts figure, never by softening the status. A FAIL was run twice: say when the second
run did not repeat it (flaky). ERROR means the agent returned no usable verdict (the harness's, not the
product's). The full verdict and screenshots of a case are in `<run dir>/qa/agent/<case id>/`.

Every run records into the same Kiwi test run (`<feature> · <env> · <build>`) and overwrites each execution's
status. `--cases TC-…,…` narrows a run to those ids.
