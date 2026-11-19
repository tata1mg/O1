# PLAN — agent-driven web execution (no generated Playwright script)

On approval this is saved as `/Users/prabhashishsingh/o_1/O1/PLAN-agentic-web-execution.md`. It is a third plan, separate from `PLAN.md` (derive_ui conversion) and `PLAN-execute-without-rfc.md`.

## Context

Today a CONFIRMED web case is first turned into a fixed Playwright script (`derive_ui`), then the script is run (`execute_web`). The script is written from source code without seeing the page, so 8 of 11 written tests failed when run, and any change in copy or layout breaks them.

You want the opposite: no script. An agent reads the Kiwi case, drives a real browser itself, decides each next step from what it sees, corrects itself when something doesn't work, and ends with PASS or FAIL plus reasons.

## How it works

One agent session per case, each with its own fresh browser:

1. **Python prepares.** Reads the CONFIRMED cases from Kiwi, logs in once and saves the session, starts a Playwright MCP browser locked to the test environment's address.
2. **The agent loops.** It reads the case (precondition, steps, test data, expected result), then repeats: look at the page (accessibility snapshot) → choose one action (open, click, type, scroll, wait) → look again → compare with what the step expects.
3. **It self-corrects.** If an element isn't there, it tries other routes: dismiss an overlay, pick a city, use search, scroll, go back. The written steps are the intent, not a script; every departure from them is logged.
4. **It stops on a verdict or a limit.** Limits per case: number of browser actions, wall time, and "same obstacle three times".
5. **It writes a verdict file.** Python checks the file, then records the result in the same Kiwi test run `execute_web` uses.

Verdict rules the agent must follow:

| Verdict | When |
|---|---|
| PASS | Every expected result was observed, each tied to a screenshot or snapshot excerpt taken at that moment |
| FAIL | The agent reached the point the case tests and the product showed something different; the reason states expected vs seen |
| BLOCKED | It could not reach that point (environment down, missing test data, login expired, limit hit). Never reported as FAIL |

**No "partial pass" status.** A case with several parts (A, B, C…) gets one strict verdict plus a per-part breakdown:

- Each part, and each clause of its expected result, is marked `met`, `not met`, or `not reached` (with the blocker). "Not checked" with no blocker is not allowed: the agent must go and check it, and the stage check rejects the verdict otherwise.
- Case verdict by precedence: any part `not met` → FAIL; else any part `not reached` → BLOCKED; else PASS.
- A case with no expected result is not executed: it stays IDLE in Kiwi with a comment "no expected result — needs QA input" and is counted as a case defect.
- Every FAIL and BLOCKED carries a `cause`: `product`, `environment` (e.g. pages returning 400/5xx, flaky between visits), `case` (the case expects something the product no longer has), or `test data`.
- Things the agent notices that the case doesn't ask about (e.g. a filter left in the page address after "Clear all") go under `observations`. They never change the verdict.
- The Kiwi comment and `results.json` show the breakdown, e.g. "FAIL — 2 of 3 parts met; B not met (product page returned 400, cause: environment)". The run summary reports parts met as a second number beside the case tally.

"Playwright window": with `--headed` the MCP browser is visible, so you can watch the agent work.

What already exists and is reused:
- Harness stages can be given MCP servers: `Stage.mcp_servers` (`src/harness/contracts.py:170`), defined in config `MCP.mcpServers`.
- Kiwi recording: `store.create_run`, `record_execution`, `attach`, `comment_execution` (`stages/execute_web/__init__.py:80-129`).
- Flags `testing.ui_auth`, `ui_login`, `ui_device`, `ui_headed`, `base_url`, `env`, `build` (`flags.py:84-162`).
- The trace file every engine stage writes (`logs/<stage>.events.jsonl`), usable to prove the agent really performed the actions it claims.

## 1. Scope

**In**
- A new stage `execute_ui_agent` behind flag `testing.execute_ui_agent`, and `/test-agent:execute ui-agent`.
- A new skill `qa-ui-agent` with the loop and verdict rules.
- Playwright MCP wired into the harness with a guarded tool allowlist.
- Verdict file, evidence, a check on both, and recording to Kiwi.
- Per-case limits, parallel cases, a run-level notes file the agent learns into.
- Running a Kiwi plan directly, with no RFC run and no earlier testing run: `execute ui-agent --feature <F>` or `--kiwi-plan <id>`.
- Optional code lookup through the fleetlens MCP server, off by default.
- Unit tests and docs.

**Out**
- Direct execution for backend cases (stays in `PLAN-execute-without-rfc.md`; the web half of that plan is replaced by this one).
- Removing `derive_ui` / `execute_web`; they stay as the scripted mode.
- Android / Maestro.
- Production environments: the stage refuses any address not named as a test environment.
- Real payments and other irreversible actions outside what a case explicitly asks for on a test environment.
- Any run without asking first (steps marked **ASK**).

## 2. Files to touch (under `/Users/prabhashishsingh/o_1/O1`)

| File | Why |
|---|---|
| `src/workflows/testing/stages/execute_ui_agent/__init__.py` (new) | The stage: select cases, prepare session, fan out one agent per case, check verdicts, record to Kiwi |
| `src/workflows/testing/ui_agent.py` (new) | Verdict schema, `validate_verdict`, prompt builder, limit settings, trace cross-check |
| `src/workflows/testing/skills/qa-ui-agent/SKILL.md` (new) | Agent instructions: loop, self-correction, verdict rules, what never to do |
| `src/workflows/testing/ui_record.py` (new, extracted) | The Kiwi recording and evidence-comment code moved out of `execute_web` so both stages share it |
| `src/workflows/testing/stages/execute_web/__init__.py` | Call the extracted recorder; no behaviour change |
| `src/workflows/testing/__init__.py`, `stages/__init__.py` | Add the stage to the pipeline when the flag is on |
| `src/workflows/testing/flags.py` | `testing.execute_ui_agent`, `agent_max_actions`, `agent_case_timeout_s`, `agent_parallel`, `agent_confirm_fail` |
| `src/rfc_agent/mcp_tools.py` | The allowlist is read-only by design. Add a `playwright` entry as a named, guarded exception listing only the browser tools needed |
| `src/rfc_agent/hooks/` (guard) | Pre-tool check: every navigation stays on the run's base address |
| `config.harness.json` (+ `~/.config/rfc-agent/config.json` at setup) | Define the `playwright` MCP server |
| `tools/ui-runner/package.json` | Pin `@playwright/mcp` beside `@playwright/test` |
| `src/workflows/testing/execute.py`, `plugins/test-agent/commands/execute.md` | New kind `ui-agent`; `--feature`, `--kiwi-plan`; `bootstrap_run()` creating a run folder with a minimal `qa/sources.json` when none exists |
| `src/workflows/testing/store/kiwi.py`, `store/__init__.py` | Resolve a plan by id (`testing.kiwi_plan_id`); give cases without an `extid:` tag a fallback id `KIWI-<case id>` so hand-written plans can run |
| `src/workflows/testing/status.py`, `progress.py` | Per-case progress and the result summary |
| `tests/testing/test_ui_agent.py` (new), `test_ui_executors.py` | Unit tests |
| `src/workflows/testing/README.md` | Document the mode |

## 3. Ordered steps

Legend: **[R]** routine, fine for a smaller model. **ASK** = needs your go-ahead before it runs.

**Phase A — foundations (no browser yet)**

1. **[R]** Move the Kiwi recording and evidence-comment code from `execute_web` into `ui_record.py`. *Check:* existing `test_ui_executors.py` passes unchanged.
2. `ui_agent.py`: define the verdict schema — `extid`, `status`, `cause`, `reason`, `parts[]` (each: label, `expected[]` with text, observed, evidence file, and state `met` / `not met` / `not reached`), `actions[]` (step, action, what was seen), `deviations[]`, `observations[]`, `blocked_on`, `limits`. Add `validate_verdict`, which also recomputes the case status from the parts by the precedence rule and rejects a mismatch. *Check:* unit tests: PASS with an expected result lacking evidence is rejected; FAIL with no expected-vs-seen is rejected; BLOCKED needs `blocked_on`; parts met/not met/met with status PASS is rejected; the three sample cases (TC-CT-01, -02, -05) produce FAIL, FAIL and "not executed".
3. Write `skills/qa-ui-agent/SKILL.md` (loop, self-correction moves, verdict table above, limits, never type credentials, never leave the base address, never place a real payment). *Check:* review; under ~120 lines.
4. **[R]** `flags.py`: add the five flags with defaults (40 actions, 480 s, 5 parallel, confirm-fail on). *Check:* flags listed by `harness` flag help.

**Phase B — browser tools**

5. Read `@playwright/mcp --help` for the pinned version and record the exact option names for: saved session, allowed addresses, device, headed, output folder, trace. *Check:* options written into this file; no guessing in later steps.
   **Step 5 findings (2026-11-16, from the project README on GitHub; the package is not installed yet, so these must be re-checked against `--help` of the pinned version, 0.0.85 being the latest published):**
   - Saved session: `--storage-state <file>` (with `--isolated`, which keeps the profile in memory).
   - Allowed addresses: `--allowed-origins` (semicolon-separated). The README says it "does not serve as a security boundary" and does not affect redirects, so the harness's own guard (step 8) is the real control.
   - Device: `--device "<name>"`. Headed is the default; `--headless` turns the window off.
   - Output folder: `--output-dir <dir>` (screenshots land there).
   - Trace: there is no `--save-trace`; `--save-session` saves the MCP session to the output folder, and tracing is only available as tools under `--caps=devtools`. The plan's trace cross-check (step 12) therefore uses the harness's own event trace, not a Playwright trace.
   - Also useful: `--secrets <dotenv>` (redacts values from tool responses), `--timeout-action`, `--timeout-navigation`, `--image-responses omit`.
   - Tools granted: `browser_snapshot`, `browser_take_screenshot`, `browser_console_messages`, `browser_network_requests` (read-only) and `browser_navigate`, `browser_navigate_back`, `browser_click`, `browser_hover`, `browser_type`, `browser_fill_form`, `browser_select_option`, `browser_press_key`, `browser_wait_for`, `browser_handle_dialog`, `browser_tabs` (guarded). Left out: `browser_evaluate`, `browser_run_code_unsafe`, `browser_file_upload`, drag/drop, and every cookie, storage, route and devtools tool.
   - Change to step 6: the server's arguments differ per case (session file, output folder), so it cannot be a fixed entry in the config file. The stage builds the server definition itself for each case (moved to step 10). Step 6 is now only the package pin, which needs `npm install` (a download) because `npm ci` fails when `package.json` and the lock file disagree.

   **Checked against the installed package (0.0.85, 2026-11-16):** every option above exists in `--help`, and all 15 granted tools exist. The server's entry point is `tools/ui-runner/node_modules/@playwright/mcp/cli.js`. It bundles its own Playwright (1.66 alpha), separate from the runner's 1.64.1, and needs Chromium build 1254; this machine has builds 1223 and 1248. Before the first browser run either download build 1254 or start the server with `--browser chrome` to use the installed Google Chrome.

6. **[R]** Pin `@playwright/mcp` in `tools/ui-runner/package.json`; add the `playwright` server to `config.harness.json`. *Check:* `harness doctor` lists it as configured (read-only command).
7. `mcp_tools.py`: add the `playwright` allowlist (snapshot, navigate, click, type, select, press key, wait, screenshot, tabs, go back) as a guarded exception with its guard named. Leave out file upload, run-code and install tools. *Check:* the existing security test for the allowlist passes and names the new exception.
8. Guard hook: reject any navigation whose address is outside the run's base address. *Check:* unit test: in-origin allowed, other origin denied.

**Phase C — the stage**

9. `execute_ui_agent` engine, Python part: select CONFIRMED cases (`exec:ui` + `surface:web`; `exec:manual` web cases too when `testing.agent_include_manual`), resolve env/build/base address exactly as `execute_web` does, refuse without a saved session when the feature needs login. *Check:* unit test with the JSON store: right cases selected; missing base address raises the same error text as `execute_web`.
10. Fan-out: cases run in batches of `agent_parallel` (default 5) — one agent per case, five agents at a time, each with its own browser profile and output folder `qa/agent/<extid>/`. The next batch starts when the current one finishes, so later batches get the notes earlier ones learned (step 15). Prompt = the case text + base address + run notes. *Check:* unit test with a fake engine: 12 cases → 12 calls in batches of 5, 5, 2; never more than 5 at once.
11. Limits: stop a case at `agent_max_actions` or `agent_case_timeout_s`; Python writes a BLOCKED verdict (`blocked_on: limit`) if the agent left none. *Check:* unit test: fake engine that never writes a verdict → BLOCKED, not ERROR, not FAIL.
12. Stage check: every case has a valid verdict; every evidence file exists; the trace shows at least one browser action per claimed step (no verdict without work). *Check:* unit tests for each rejection.
13. Confirm-fail: a FAIL is re-run once in a fresh browser; FAIL twice → FAIL, otherwise → reported as flaky with both reasons. *Check:* unit test with a fake engine returning FAIL then PASS.
14. Record: one Kiwi run `<feature> · <env> · <build>`, one execution per case, comment = reason + expected-vs-seen table + deviations + screenshot; write `qa/agent/results.json`. *Check:* unit test with the JSON store: statuses, comments and attachments recorded.
15. Run notes: the agent may append short facts to `qa/agent/notes.md` ("city picker appears first; choose Gurgaon"); later cases get them in their prompt. *Check:* unit test: second case's prompt contains the first case's note.

**Phase C2 — run a Kiwi plan directly (no RFC run)**

The agent stage needs only the case text, the environment address and a logged-in session. It reads no code snapshot, so nothing from the RFC run is required. `execute_ui_agent` declares `requires=()`.

15a. `execute.py`: `--feature` and `--kiwi-plan`; `bootstrap_run()` creates `<RUNS_ROOT>/<date>-testing-<feature>/qa/sources.json` (`{"feature", "rfc_run_dir": "", "sources": [], "origin": "kiwi-direct"}`) when no run folder exists, and reuses it afterwards. *Check:* unit test: folder created once; built command has no `--rfc-run-dir`.
15b. `store/kiwi.py`: with `testing.kiwi_plan_id` set, resolve the plan by id and take the product from it. *Check:* unit test with the fake RPC: resolved by id, name lookup not called.
15c. `store/kiwi.py`: in direct mode, a CONFIRMED case with no `extid:` tag gets the id `KIWI-<case id>`; a case with no `exec:` tag is treated as a UI case unless tagged `exec:http`. Recording uses the Kiwi case id directly. *Check:* unit test: an untagged hand-written case is selected, executed by a fake engine, and recorded.
15d. Pre-run report in `execute --pending`: counts of cases that will run, cases skipped as backend, and cases with no expected result. *Check:* unit test with a fake store.
15e. Optional code context: `testing.agent_code_context` adds the `fleetlens` MCP server (already on the read-only allowlist in `mcp_tools.py`) to the stage. The skill limits its use to finding where a feature lives or which route opens it, when the agent is stuck; it may never be used to decide what the expected result is. First confirm the stage's MCP list can vary per run (`stage_mcp_servers(stage, ctx)` in the engine). *Check:* unit test: flag off → no fleetlens in the request; flag on → present.

**Phase D — command and docs**

16. **[R]** `execute.py`: kind `ui-agent` → stages `execute_ui_agent`, `--headed` passes through. *Check:* unit test on the built command.
17. **[R]** `commands/execute.md`, README, `status.py` / `progress.py` lines. *Check:* every new flag documented; status test extended.
18. **[R]** Run the unit suite `tests/testing`. *Check:* all green. (Local unit tests; I'll still ask first.)

**Phase E — prove it**

19. **ASK** Pilot on HIH-1VOICE's 8 confirmed web cases, headed, against the test environment. *Check:* 8 verdicts; compare with the scripted results (3 pass, 2 fail, 3 skipped); you read 3 verdicts against what you watched.
20. **ASK** Second pilot on the 6 LABS demo cases (all 6 scripted tests failed there). *Check:* cost and time per case recorded in this file; decide the default mode.

## 4. Risks and decisions

- **Naming.** The stage is `execute_ui_agent` (underscores, like `execute_web` and `execute_be`); the command is `/test-agent:execute ui-agent`. The existing `execute_web` is not renamed: it runs scripts, which is a different thing, and its name is in past run folders, status output and docs.
- **Decision 1 — removing `derive_ui` and `execute_web`.** Not in this plan. They stay until the two pilots (steps 19–20) show the agent's verdicts, cost and time are acceptable. If they are, removal is a small follow-up: drop the two stages from the pipeline, delete `skills/qa-ui-playwright`, `check_ui.py` and the derive half of `ui_web.py`, make `web` an alias of `ui-agent` in `execute.py`, and update docs and tests. `PLAN.md` (raising derive_ui conversion) would then be dropped rather than implemented, so it is worth deciding this before starting that plan.
- **Decision 2 — manual-tagged cases.** The agent needs no source code, so it can also attempt confirmed `manual` web cases. This helps the 80% target on your wider denominator, but those cases must attach to the Kiwi run without an `exec:ui` tag (same store change as in `PLAN.md` step 9).
- **Risk — wrong verdicts.** An open-ended agent can call a pass it didn't verify, or call FAIL when it simply got lost. Guards: evidence required per expected result, trace cross-check, "couldn't reach it" is BLOCKED, and FAIL is confirmed by a second fresh run. These lower the risk; they don't remove it.
- **Risk — results vary between runs.** The same case can take different paths. Confirm-fail and the logged deviations make this visible; it won't be as repeatable as a script.
- **Risk — cost and time recur on every run.** A script is paid for once; the agent is paid for each execution. No measured figure exists yet. For reference, scripted derivation cost $1.38–3.15 per converted case. Step 19 measures it.
- **Risk — an acting agent on a live site.** The harness deliberately gives agents read-only tools. This stage is an exception: it reads untrusted page content and can click. Guards: locked to the test address, login done by Python so the agent never sees credentials, no upload or code-run tools, refusal of production addresses.
- **Risk — direct runs lose what the RFC run supplied.** The RFC-based flow produces a prerequisites list (test accounts, test data, feature flags). Without it the agent only knows what the case says. Cases whose preconditions aren't written into the case will end BLOCKED with cause `test data`. The pre-run report (15d) and the BLOCKED reasons show which cases need their preconditions filled in.
- **Risk — code context can bias the verdict.** If the agent reads the code to learn what "should" happen, it ends up confirming the implementation instead of the requirement. That is why fleetlens is off by default and limited to navigation help. It also indexes the repositories, which may not match the build deployed on the environment.
- **Risk — untagged cases.** Treating untagged cases as UI cases (15c) can send a backend or truly manual case to the browser agent. It will end BLOCKED, which costs one agent session per such case.
- **Risk — five agents share one test account.** Cases that change shared state (cart, addresses, patients) can interfere when run at the same time. Each agent gets its own browser, but the account is the same. If the pilot shows interference, state-changing cases run one at a time or each batch slot gets its own test account.
- **Risk — login.** Phase 1 needs a saved session (`--ui-auth`, or the existing login step). If the session expires mid-run, remaining cases are BLOCKED, not FAIL.

## 5. Routine steps (smaller model)

1, 4, 6, 16, 17, 18 — marked **[R]** above. Also the unit tests in steps 2, 9, 12 and 14 once the signatures exist.

Also routine: 15a, 15b, 15d.

Steps needing the main model: 2, 3, 5, 7, 8, 10, 11, 12, 13, 15, 15c, 15e.

## Verification

- Unit: `tests/testing/test_ui_agent.py`, `test_ui_executors.py`.
- End to end (**ASK** first): `/test-agent:execute ui-agent --headed` on HIH-1VOICE; read `qa/agent/results.json`, the per-case `verdict.json`, and the Kiwi run.
