---
description: Execute the CONFIRMED cases of an existing testing run — web UI (Playwright), backend (HTTP), or both — recorded in Kiwi; run the already-written web tests at once, or write tests for newly confirmed cases first; or have an agent drive a browser through each web case and run each Android case on an emulator — its validated Maestro flow, else an agent through Maestro MCP (ui-agent --platform web|android|both|auto, by the cases' Kiwi surface tags), also straight from a Kiwi plan with no earlier run. Takes flags, or plain words such as "cart-sanity on prod, cases 173,172, logged in as careplan, show the browser"
argument-hint: [web|backend|ui-agent|android|all] [--platform auto|web|android|both] [--untagged web|android|both|skip] [--android-mode auto|flow|agent] [--app-variant V] [--devices S,S] [--device-roles S=guest,S=care_plan_user] [--allow coupons,neucoins] [--only-existing] [--run-dir <testing run>] [--feature F --kiwi-plan ID] [--env E] [--build B] [--base-url U] [--ui-auth FILE] [--hosts H] [--production] [--headed] [--cases TC-…,TC-…] [--curl FILE]  — or plain words: "cart-sanity on prod, cases 173,172, logged in as careplan, show the browser"
---

Execute cases for: `$ARGUMENTS`

1. **Project.** Resolve `PROJ` once and write it literally below:

   ```
   python3 -c 'import json,os; print(os.environ.get("TEST_AGENT_PROJECT") or json.load(open(os.path.expanduser("~/.claude/plugins/known_marketplaces.json")))["rfc-agent-marketplace"]["installLocation"])'
   ```

   Every helper is `uv run --project <PROJ> python -m workflows.testing.execute …`. With no kind, the kind is
   `web`.

1a. **Plain words.** When `$ARGUMENTS` is empty or starts with a kind (`web`, `backend`, `ui-agent`, `all`) or
   a `--flag`, skip this step: flags work as before. Otherwise it is a request in words. Run
   `… execute --lookup` and build the flags from it, never from memory (`error` set → say so and stop):
   - **Plan.** The phrase naming a plan, matched against `plans` ignoring case, spaces and hyphens
     (`cart sanity` = `CART-SANITY`; a bare number, `TP-3` or `plan 3` is an id; an exact name beats a
     partial one). Exactly one → `--feature <its name> --kiwi-plan <its id>` and kind `ui-agent`. None or
     several → ask which (AskUserQuestion with the candidates).
   - **Cases.** Numbers / ids / codes after "case(s)", "test case(s)", "tc" → run
     `… execute --resolve-cases "<tokens>" --plan <id>`; `matched` → `--cases a,b`; anything in `unknown` →
     say which token matched nothing and ask; never drop it silently. No cases named → the whole plan: say
     how many cases that is (from `--pending`, step 2a) and confirm before running more than 10.
   - **Where.** "prod", "production", "live" or the production address → `--production --env
     <production.env> --base-url <production.base_url>`; when `production.base_url` is empty, ask for the
     address once and offer to store it with `… python -m workflows.testing.preflight setup-web
     --production-base-url <url>`. A name in `environments` → `--env <name>` (the address comes from
     `base_url_template`). Nothing said → `defaults.env`; when that is empty, ask.
   - **Who.** "guest", "logged out", "not logged in" → no `--ui-auth`. "logged in as X", "with the X
     user/account", "as X" → the one entry of `sessions` whose name contains X (ignore case) → `--ui-auth
     <its path>`; none or several → ask, listing the session names (names only; never open a session file).
     Nothing said → ask "guest or logged in?" only when the plan's cases could need login; otherwise guest.
     Never invent a path.
   - **Platform.** "android", "app", "on the emulator", "mobile app" → `--platform android`; "web", "browser",
     "mweb", "dweb" → `--platform web`; "both", "web and android" → `--platform both`. Nothing said →
     `--platform auto` (each case on the surfaces its Kiwi tags name). "flows only" → `--android-mode flow`;
     "agent only" → `--android-mode agent`. "debug build" → `--app-variant debug`. "5554 as guest, 5556
     logged in as careplan" → `--device-roles emulator-5554=guest,emulator-5556=care_plan_user` (a device
     whose role is an account is kept signed in by hand and only runs that account's cases; a guest device
     only the no-account ones). Not said → the maestro repo's `.maestro/devices.yaml` when 2+ devices are up.
   - **How.** "show the browser", "headed", "watch", "visible" → `--headed`; "N at a time", "in parallel" →
     `--parallel N` (not with `--headed`: say a visible browser runs one case at a time); "read only",
     "don't change anything" → `--read-only`.
   - **Build and browser.** `--build <today>` unless one is given; `--browser <defaults.browser>` when set,
     otherwise `--browser chrome`.
   - Words that map to nothing → say so; do not guess a flag.

   Then SHOW what was understood before anything runs: one line per mapping (`"cart-sanity" -> plan
   CART-SANITY (TP-3)`, `"173" -> KIWI-173 [CT002] Verify the selected address…`, `"on prod" -> production,
   https://…`, `"careplan" -> session prod-careplan-state`) and the full flag-form command it will run, and
   ask once to confirm (AskUserQuestion: `Run it` / `Change something`). The production test-account question
   in the Preview step ("Production.") still applies after this and is asked separately — do not merge or
   skip it. From here continue with the steps below, using the built flags as `$ARGUMENTS`.

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
   - `needs_session` non-empty and no `--ui-auth` → say these cases need a logged-in user (their
     Precondition, or an `auth:user` tag) and will be BLOCKED with no session started; offer to add
     `--ui-auth`. Guest cases (Precondition says guest / not logged in, or `auth:guest`) run without the
     session even when one is given, so one run covers both.
   - `needs_allow.coupons` / `needs_allow.neucoins` non-empty → these cases apply a coupon or redeem
     NeuCoins, which the guard refuses by default (they come back BLOCKED). Ask (AskUserQuestion)
     **"These cases <apply coupons / redeem or toggle NeuCoins> on <host>: allow it for this run?"** with
     `Yes, allow` (add `--allow coupons` and/or `--allow neucoins`, comma-joined) and `No` (they stay BLOCKED).
     NeuCoins on a logged-in run also needs `--test-account` (step 3's question). The agent is told to undo
     what it applied. Never add `--allow` yourself.
   - Say once, before the first `ui-agent` run of a session: each case is a model session on every run
     (cost and time are per case), the agent acts on the environment as a real user, and it logs in only
     from a recorded session (`testing.ui_auth` / `--ui-auth`), never from a number and OTP — without one the
     cases run logged out. `--headed` shows the browser and runs the cases one at a time.
   - **Platform.** Run `--pending` with the same `--platform` (default `auto`). It reports the web side as
     above and, for Android, `android.by_flow` (cases that run as their VALIDATED Maestro flow — no model),
     `android.by_agent` (no validated flow: an agent drives the app), `android.not_run` (with why: a gated
     flow, no expected result) and `android.error`. Post one line per platform: `web: N by agent ·
     android: F by flow, A by agent, R not run`. Cases in `android.both` carry both surface tags and run on
     each.
   - **Untagged.** `android.untagged` non-empty → these CONFIRMED cases have no `surface:` tag, so nothing
     says where they run. Ask (AskUserQuestion): **"N cases have no platform tag — where should they run?"**
     with `Web`, `Android`, `Both` and `Skip them`, and add `--untagged web|android|both|skip`. Never pick
     for the user; suggest tagging them in Kiwi (`surface:web` / `surface:android`) so the question goes away.
   - Say once, before the first Android run of a session: a case with a validated flow costs nothing and
     runs the same every time; a case without one is a model session on every run, acting in the app as a
     real user on the device's signed-in account; the run needs `adb`, the Maestro CLI, a booted emulator
     with the app installed and the maestro-automation checkout (`testing.maestro_repo`). Order-placing
     flows are not run in this version.
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
   - `ENGINE FAILURE execute_app: adb is not installed` / `no Android emulator or device is running` / `the
     Maestro CLI is not installed` / `no maestro-automation checkout` (Android only) → post the message as it
     is: the web half still runs; the Android half needs that machine set up. Never try to install or boot
     anything.
   - `ENGINE FAILURE execute_app: <serial> has the <variant> build …, not the <other> build` → the device
     carries another build of the app. Ask (AskUserQuestion) `Use the <variant> build` (re-run with
     `--app-variant <variant>`; `prod` also needs `--production`) or `Stop`.
   - **Production app.** `--app-variant prod` drives the production app: add `--production` and ask, as for
     the web, whether the device is signed in to a TEST account; only on yes add `--test-account`.
   - **Production.** When the user asks to run on production, or the address is the live site, add
     `--production` (then `--hosts` is not needed). If the run is logged in (`--ui-auth`, or a session in
     config), FIRST ask (AskUserQuestion): **"This will drive production (<host>) logged in. The agent adds
     to the cart and changes what the account sees. Is the session a test account?"** with `Yes, a test
     account` and `No — stop`. Only on yes add `--test-account`; on no, stop, or offer to run as a guest
     (no `--ui-auth`). Never add `--test-account` yourself. A guest run on production is not asked. Say
     once that checkout, payment, coupons, NeuCoins, Care Plan and address changes stay refused there
     unless `--allow` names them.
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

For Android, report from `results_android` the same way: the tally, the Kiwi run link
(`<feature> · android-<env> · <app version>`), and ALWAYS `results_android.table` as it is — Case | Engine |
Verdict | Time | Cost | What was found — then `not_run` with reasons. A flow FAIL is reported FAIL as it is
(no second run; the flow's owners triage it). Then list `results_android.drafts`: the cases whose agent PASS
left a draft flow, and say that `/test-agent:convert-flows` turns them into validated flows — this command
stops at the verdicts and never converts.

For `ui-agent`, report from `results_agent`: the tally, `parts_met` of `parts`, the Kiwi run link, and
ALWAYS the per-case table in `results_agent.table` as it is — Case | Verdict | Time | Cost | What the agent
found, with its Total row. Never drop the Time or the What-the-agent-found column or summarise the table
away: how long each case took
is part of the result (a FAIL's time includes its second run; a cost of `unknown` means the session was cut
off before reporting it). The same table is in `<run dir>/qa/agent/results.md`. Each row's last cell is the
agent's own sentence about what it saw, then the parts met and, when it did not pass, the cause. There is no partial pass: a case is PASS only when every part is met; say how close
it came with the parts figure, never by softening the status. A FAIL was run twice: say when the second
run did not repeat it (flaky). ERROR means the agent returned no usable verdict (the harness's, not the
product's). The full verdict and screenshots of a case are in `<run dir>/qa/agent/<case id>/`.

Every web run records into the same Kiwi test run (`<feature> · <env> · <build>`), every Android run into
`<feature> · android-<env> · <app version>`; a re-run overwrites each execution's status. `--cases TC-…,…` narrows a run to those ids.
