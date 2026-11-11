---
description: Execute the CONFIRMED cases of an existing testing run — web UI (Playwright), backend (HTTP), or both — recorded in Kiwi; run the already-written web tests at once, or write tests for newly confirmed cases first
argument-hint: [web|backend|all] [--only-existing] [--run-dir <testing run>] [--env E] [--build B] [--headed] [--cases TC-…,TC-…] [--curl FILE]
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

3. **Preview.** Run `… execute $ARGUMENTS [--only-existing] --print`. It picks the newest testing run (or
   `--run-dir`) and takes the feature, RFC run, environment and build from it.
   - `missing …` → ask for exactly what it names (AskUserQuestion; free text via "Other"). For a missing
     curl, ask for the file's **path** and never read or print it; offer to store it once with
     `… python -m workflows.testing.preflight setup-web --curl <path>`.
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
   (after derive_ui, also the cases it skipped, from `status --json` → `ui_plan.skipped`). Post each
   `PROGRESS <stage> · …` line as it is with ⏳ in front (derive_ui's says how many tests it has written).
   On `WATCH rearm --from <offset>`, if the command is still running, start the same Monitor again with
   `--from <offset>`. Stop the Monitor when the command exits.

5. **Report** from `… python -m workflows.testing.status <run dir> --json` (`results_web` / `results`): the
   tally, the Kiwi run link, each case that did not pass (id, status, one-line detail; for web, the failed
   requests are in its Kiwi comment), and for web the HTML report:
   `cd <PROJ>/tools/ui-runner && npx playwright show-report <results_web.html_report>`.

Every run records into the same Kiwi test run (`<feature> · <env> · <build>`) and overwrites each execution's
status. `--cases TC-…,…` narrows a run to those ids.
