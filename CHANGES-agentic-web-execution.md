# Agent-driven web execution — what changed

Branch `feat/test_agent`. Unit suite at the last run: 1672 passed, 1 skipped.

## What it does

`/test-agent:execute ui-agent` runs each CONFIRMED web UI case by giving it to an agent with a real browser. No Playwright script is written first. The agent returns PASS, FAIL or BLOCKED with a screenshot for every clause it judged, and the result is recorded in Kiwi.

## How to run it

One case, browser visible:

    /test-agent:execute ui-agent --feature CART-SANITY --kiwi-plan 3 --cases KIWI-172 --env prod --build <label> --base-url https://<host> --hosts <host> --browser chrome --headed

A plan, five at a time, no window:

    /test-agent:execute ui-agent --feature <name> --kiwi-plan <id> --env <env> --build <label> --base-url https://<host> --hosts <host> --browser chrome --parallel 5

- `--ui-auth <session file>` runs logged in; without it the run is a guest.
- `--pending` lists what would run. `--read-only` tells the agent to change nothing.
- `--headed` always runs one case at a time.
- A case with no id tag in Kiwi is `KIWI-<case number>`.

## Read these first

| File | What to look at |
|---|---|
| `src/workflows/testing/skills/qa-ui-agent/SKILL.md` | The agent's instructions: the look-act-look loop, clause states, causes, account rules, the "never" list |
| `src/workflows/testing/ui_agent.py` | The verdict rules and `validate_verdict`; browser server arguments; the results table |
| `src/workflows/testing/stages/execute_ui_agent/__init__.py` | The stage: selection, batches, limits, the FAIL re-run, recording |
| `src/rfc_agent/hooks/guard_browser.py` | The guard: stays on the run's address, refuses money and account actions, enforces the action limit and verdict updates |
| `src/workflows/testing/README.md` | Section "An agent in a browser instead of a script: `ui-agent`", and the flag table |

## Rules worth checking

- **No partial pass.** Each clause is met, not met or not reached. Any not met = FAIL; else any not reached = BLOCKED; else PASS.
- **Judged on the named element.** Every met / not met clause must state `on_target: true`; a similar element elsewhere is a substitute and the clause is not reached.
- **A FAIL is run twice.** It stays FAIL either way. The second run is given only what the first run did, never what it saw.
- **ERROR** means no usable verdict came back. It is never recorded as FAIL.
- **Not executed:** a case with no expected result (or a placeholder such as "FLAGGED — …"), or an id that cannot name a folder.
- **Limits:** 60 browser actions and 15 minutes per case; batches of 5.
- **Verdict on disk.** The browser refuses to act until `verdict.json` exists and again after 12 actions without an update, so a cut-off session keeps what it judged. The agent checks its own verdict with `check_verdict.py`.
- **Every run reports a table:** Case, Verdict, Time, Cost, what was found. Also saved as `qa/agent/results.md`.

## Safety

- The base address must be a named test environment (`--hosts`, or a configured environment). Nothing in the code stops production being named.
- The agent is never given a credential; login is a recorded session only.
- Only listed browser tools are granted: no script evaluation, upload, cookie or route tools.
- `testing.agent_forbid` (on by default) refuses actions aimed at NeuCoins or wallet, applying coupons, checkout and payment, Care Plan add/remove, saving or deleting an address or patient, prescription upload, logout. It matches the agent's own description of the element, so it stops honest mistakes, not a determined workaround.
- The agent is told to leave the cart as it found it and never to create a condition ("if added") in order to judge it. These are instructions, not blocks.

## Real runs so far

| Run | Where | Cases | Outcome |
|---|---|---|---|
| 1 | staging (jupiter), logged in | 4 diagnostics | 3 BLOCKED, 1 weak FAIL |
| 2 | production, read-only | 2 diagnostics | 2 BLOCKED |
| 3 | production, read-only | same 2 | 1 ERROR (a spelling), 1 cut off |
| 4 | production, guest | 3 pharmacy cart | 2 PASS, 1 FAIL — 10 min, $4.29 |
| 5 | production, care plan user | 3 pharmacy cart | 2 PASS, 1 FAIL — 13 min, $4.51 |

- The diagnostics cases are written for the mobile app; several steps do not exist on the website. The cart cases fit the web.
- About $1 and 3 to 4 minutes per case; a FAIL costs double because it is run twice.

### Incident in run 5

The agent applied the account's NeuCoins to the cart to see a "Neucoins used (if added)" row and could not undo it. It also removed an item that was already in the cart and left Telma ×3 behind. No order was placed and nothing was paid. This led to `testing.agent_forbid` and the leave-as-found rules. A subagent watching the log did not catch it: its monitor delivered no events.

## Not done or not proven

- The forbid guard and the leave-as-found rule have not been seen working in a real session.
- Evidence screenshots: fixed by instruction after run 4; run 5 was better but one crop was still weak.
- Resume after a crash: a re-run starts every case again.
- Axon: `/test` does not offer this mode and the stage refuses the Axon engine.
- `preflight setup-web` cannot store the host or session file.
- Langfuse receives only a run-level summary for this stage, and every run logged an export warning.
- The decision to remove `derive_ui` / `execute_web`.
- A flaky, unrelated test: `tests/rfc/test_scope.py::test_confirm_via_file_without_design_goes_on_only_when_the_user_says_so` (a thread race).
