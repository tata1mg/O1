# PLAN — run CONFIRMED Kiwi cases on Android (Maestro) and web from one `execute` (FINAL v3)

A fourth plan, beside `PLAN-agentic-web-execution.md` (web agent, built), `PLAN.md` and
`PLAN-execute-without-rfc.md`. Nothing here is built yet. v3 = v2 + an independent architecture review
(Fable plan agent, 2026-11-17), whose code claims were spot-checked before being adopted.

## Decided

1. **Platform** is chosen by flag (`--platform`), by the user when asked, or by the case's Kiwi
   `surface:` tag.
2. **Android, per case:** a validated Maestro flow that covers the case → run the flow. **No validated
   flow → the combination:** an agent drives the emulator live for today's verdict; on PASS the agent's
   proven steps become a draft flow, which is validated to green so the next run uses the flow.
3. **Kiwi integration matches `execute ui-agent`:** cases read from Kiwi (CONFIRMED + tags, `--feature` /
   `--kiwi-plan` direct mode, `KIWI-<id>` ids), one Kiwi run per platform, executions recorded per batch
   with a comment and screenshot, `results.json` / `results.md`, `--pending` from Kiwi.

## Decided in discussion (2026-11-17) — these override anything below

1. **Validated = the flow says so.** A flow whose header says VALIDATED / GREEN / RE-VALIDATED (or starts
   `✅`) runs as a flow. A flow with no status line does not say so and goes to the agent.
2. **A validated flow that FAILs is reported FAIL** — no agent confirmation run; the maestro team triages
   by hand.
3. **Draft → validated flow is a separate command** (`/test-agent:convert-flows <run dir>`), not a stage of
   `execute`. Promotion into maestro-automation stays a further explicit step.
4. **`execute` stops after the verdicts** and lists the cases whose agent PASS left a `draft.yaml`.
5. **Order-placing cases**: undecided. v1 runs none of them (not as flow, not as agent) and lists them.
6. **Device capacity / account limits**: accepted for v1.
7. **Platform comes from the Kiwi tags.** A case may carry both `surface:web` and `surface:android` (one
   Kiwi case for a journey that is the same on both) and then runs on both. A case with no `surface:` tag
   → the command asks the user (web / android / both / skip).
8. **Build it.** No Android SDK on the build machine (2026-11-17): device steps (spike, pilots) wait for a
   machine with emulators; everything else is built and unit-tested with fakes.

## Built (2026-11-17, branch `feat/android-maestro`, not committed)

| Piece | Where |
|---|---|
| Flow index (Covers / labels / `Kiwi #id`, first STATUS line, gates, profiles) | `src/workflows/testing/maestro_index.py` |
| Devices: adb, app version, device lock, sign-in, `maestro test` + report enrichment | `src/workflows/testing/devices.py` |
| Plan per case, Maestro MCP server, prompt, draft lint, flow file | `src/workflows/testing/app_agent.py` |
| Stage `execute_app` (flows + agent, Kiwi run `android-<env>`, drafts listed) | `src/workflows/testing/stages/execute_app/` |
| Stage `convert_flows` + `promote` | `src/workflows/testing/stages/convert_flows/`, `convert_flows.py` |
| Device guard (allow-list, device, app, links, sub-flows, forbid, command limit) | `src/rfc_agent/hooks/guard_device.py`, `mcp_tools.py`, `engine.py` |
| Skills | `skills/qa-app-agent/`, `skills/qa-flow-fixer/` |
| Store: `Case.store_id`, `testing.kiwi_direct_surface`, two surface tags | `store/__init__.py`, `store/kiwi.py` |
| `record_row`: `executor` + `props`, keeps `execution_id` | `ui_record.py` |
| `execute`: `--platform`, `--untagged`, Android options, `android` and `convert` kinds, pending per platform | `execute.py` |
| Commands | `plugins/test-agent/commands/execute.md`, `convert-flows.md` |
| Status / progress / README | `status.py`, `progress.py`, `README.md` |
| Tests | `tests/testing/test_android.py` (33), `tests/infra/test_guard_device.py` (34) |

**Still needs a machine with an Android SDK and a booted emulator (phase 0 spike, then the pilots):**
- `maestro mcp --working-dir=<case folder>`: does `takeScreenshot: <name>` land there as `<name>.png`?
- How a failed `run` shows in the trace (`is_error` or text), and whether `run` needs the `appId` header.
- One `maestro mcp` process per case driving its own `device_id` while another case drives another device.
- `maestro_report.py --test-dir` picks the right artifacts when two devices run at once.
- Token cost of `inspect_screen` per case.
- Not built: generator emitting two surface tags for one case (hand-tag in Kiwi for now); order-placing cases
  (decision deferred); `publish_app` still defaults to the druid flows path.

## What the review changed (and why)

| v2 said | v3 does | Evidence |
|---|---|---|
| Convert by mining the agent's trace into YAML | The agent proves each `met` clause with an `assertVisible` sent through Maestro, and on PASS writes `draft.yaml` from its proven snippets; Python lints it and adds the header | trace `tool_calls` carry no ok flag (`harness/events.py:191`); clauses hold prose, not selectors (`ui_agent.py:21-29`) |
| Pin the MCP server to one device | The guard checks `device_id` on every call against the serial assigned to the session | `run`, `inspect_screen` and `take_screenshot` each require `device_id` (checked on `maestro mcp` 2.12.1) |
| Guard = deny-list | Guard = allow-list of commands; deny `files`/`dir`/`env`; `runFlow` only into the repo's `common/` subflows; count commands, not calls | `run` takes `yaml` *or* `files`/`dir`; the agent can write files in the run dir |
| Hard-deny `point:` / `index:` | Allowed while running (115 flow files use `index:`, 16 use `point:`); the draft lint rejects `point:` and flags `index:` | repo practice |
| Background validation after the run | A foreground stage `convert_flows` after `execute_app`, adding a second Kiwi comment to the stored execution | a stage cannot outlive its `StageResult` |
| Record flow rows through `ui_record.record_row` | Extend `record_row` with `props` + `executor` (agent rows); lift `publish_app`'s recording into a shared function for flow rows | `record_row` props are fixed and rows need `playwright_status` (`ui_record.py:44-45,68`) |
| Not-executed cases IDLE with a comment | Not recorded, listed in results and `--pending` — exactly as the web stage does | a comment needs an execution, which sets a status |
| `schema.py` accepts two surfaces | One surface per case stays; `--platform both` runs the web and Android stages, each on its own tagged cases | `schema.py:116` validates a single value |
| Refactor `execute_ui_agent` into drivers first | Build `execute_app` beside it, importing `ui_agent`/`ui_record` helpers; extract a shared core only after the Android pilot | avoid breaking a working 551-line stage |
| Validated flow FAILs → report FAIL | Flow FAIL → one agent run of that case confirms: agent FAIL = product FAIL; agent PASS = stale flow (reported, flow flagged) | mirrors the web stage's confirm-fail |
| Direct mode: untagged case gets no default | Keep `surface:web` default unless `--platform android/both` is passed (new `direct_surface` in `get_store`) | do not change today's web-only plans |

Also adopted: `#id` attribution (the cart flows name `Kiwi … #191`, and `RE_TC` only matches `TC-…`,
`maestro.py:22`); many-to-many flow↔case (run each flow once, attribute to all its cases); call the
maestro scripts with `--no-upload` so they never publish to Kiwi themselves; a spike before the agent
work. `guard_browser`'s hook matcher is hard-coded to `mcp__playwright__.*` (`rfc_agent/engine.py:162`),
so a `maestro` matcher and a `mcp_tools` allowlist entry are needed. O1's environment has no YAML parser:
add `pyyaml`.

## Design

### 1. Platform selection

`--platform auto|web|android|both` (default `auto`).
- `auto`: Android stage takes `surface:android` cases, web stage takes `surface:web` cases; a stage with
  no cases is a no-op.
- `web` / `android`: only that stage.
- `both`: both stages, one after the other, each into its own Kiwi run.
- No flag and the plan holds cases for both surfaces → the command asks once:
  *Web only (N) · Android only (M) · Both*.
- A case meant for both platforms is, for now, two Kiwi cases (one per surface), as the generator already
  writes when behaviour differs. Multi-surface tags are a later change (schema + generator + store).

### 2. Android, per case

```
CONFIRMED surface:android case
 ├─ index: VALIDATED flow covers it ─▶ FLOW: gate → maestro test → maestro_report → attribution
 │                                        └─ FAIL ─▶ AGENT confirm run ─▶ product FAIL | stale flow
 └─ no validated flow ─────────────────▶ AGENT: Maestro MCP session → verdict
                                          └─ PASS + draft.yaml ─▶ convert_flows: lint → maestro test
                                                ├─ green ─▶ "validated draft" (run dir) → promote (ASK)
                                                └─ red ─▶ bounded fix loop (draft-to-green rules) → green | stuck
Every verdict → Kiwi (same run, per batch).  convert_flows outcome → second comment on that execution.
```

- `--android-mode auto|flow|agent`; `--no-convert` skips `convert_flows`.
- **Index** (extend `maestro.flow_claims` / `covers_block`, `maestro.py:105-181`): case ids from
  `# Covers:` and `label:` (`TC-…` and `Kiwi … #<id>` → `KIWI-<id>`), tags, `# REQUIRES:`, and a status:
  VALIDATED/GREEN/RE-VALIDATED or a leading `✅` → validated; DRAFT/BLOCKED/UNFROZEN/PARTIAL → not.
  Headers with no STATUS (66) or conflicting ones are listed in `--pending` as "unknown" and treated as
  not validated until someone stamps them.
- **Gates** (as `run_agent.py`): `mock-required` → not run (reported); `manual-order-placing` → flow only
  and only with `--confirm-orders`, never to the agent in v1; `# REQUIRES:` profile → `ensure_account.sh`
  on a device that can hold it; exit 78 → BLOCKED.

### 3. The agent on Android (`execute_app`, agent path)

- One model session per case, Maestro MCP (`maestro mcp`, pinned Maestro version), one device per
  session from the device pool; skill `qa-app-agent` = `qa-ui-agent`'s loop and verdict rules, plus:
  - look with `inspect_screen`; act with `run` (inline `yaml`, one or a few commands);
  - prove every `met` clause by sending an `assertVisible` through `run` and storing that selector and
    a screenshot path in the clause (screenshot via `- takeScreenshot: <case folder>/…` inside `run`, so
    `validate_verdict`'s evidence-file check holds);
  - prefer `id` > content-desc > exact text; avoid `point:`; on PASS write `draft.yaml` = the snippets
    that worked, in order, ending with the proving asserts.
- `ui_agent.browser_calls` / `browser_actions` generalised to device calls; the action cap counts Maestro
  commands inside `run`, not tool calls.
- Images: inline screenshots and hierarchy JSON are expensive. The skill asks for screenshots only as
  evidence; measure cost in the spike.

### 4. Guard (`rfc_agent/hooks/guard_device.py`)

PreToolUse on `mcp__maestro__.*` (new matcher in `engine.py`; `maestro` entry in `mcp_tools.py`):
- every call: `device_id` == the session's assigned serial; cloud tools and `open_maestro_viewer` denied;
- `run`: only `yaml` (deny `files`, `dir`, `env`); parse with PyYAML; allow-list of commands (tapOn,
  inputText, eraseText, scroll, scrollUntilVisible, swipe, back, pressKey, hideKeyboard, waitForAnimationToEnd,
  extendedWaitUntil, assertVisible, assertNotVisible, takeScreenshot into the case folder, launchApp/
  openLink for the run's appId/scheme only, runFlow only into `<maestro_repo>/.maestro/flows/**/common/`);
  everything else denied (clearState, clearKeychain, runScript, evalScript, stopApp of other apps, …);
- money/order taps (same forbid list as web: Place order, Pay, Confirm order, …) denied;
- action cap and verdict nudges reuse `guard_browser`'s counter/verdict-file mechanics.

### 5. Devices and accounts (Python, never the agent)

- `testing.maestro_repo` (default `~/o_1/maestro-automation`); `publish_app` moves to it as well.
- Preflight: `adb devices` (ready only), app installed for `--app-variant` (`app_id.sh`), version from
  `dumpsys package` → the Kiwi build. No device → Android stage reports BLOCKED (`environment`) and the
  web stage still runs.
- **Device lock**: one owner per serial at a time across flow runs, agent sessions, `ensure_account.sh`
  and `convert_flows` (a lock file per serial in the run dir). Concurrency = ready devices (1–4 here).
- Accounts: profile from `# REQUIRES:` / Kiwi tag `account:<profile>`, else a pool account per device
  (`assign_parallel_accounts.sh`). Sign-in before the session; static dev OTP only.
- `--app-variant prod` behaves like web `--production` (confirmation, test account only).

### 6. Kiwi

- **Select**: `store.list_cases(feature, status="CONFIRMED", tags=("exec:ui", "surface:android"))`;
  `testing.agent_include_manual` adds `exec:manual`; `--cases` narrows.
- **Direct mode**: `--feature F [--kiwi-plan ID]`, `bootstrap_run()` as today; `direct_surface` from
  `--platform` decides the default for untagged cases.
- **Run**: `create_run(feature, build=<app versionName (code)>, env="android-<env>", extids,
  require_tag="exec:ui")` (+ `exec:manual` when included) — the name is applied only here, not to the
  `env` used elsewhere.
- **Rows**: agent rows through `record_row` extended with `props` (device, variant, profile, engine) and
  `executor: agent`; flow rows through the function lifted from `publish_app` (its evidence comment:
  timeline, failing step, screenshot/log). Each row states its engine. Recorded per batch.
- **convert_flows** adds a second comment to the same execution (ids kept in `qa/app/results.json`):
  "draft green / stuck at step N / rejected by lint (point: tap)".
- **Files**: `qa/app/results.json`, `qa/app/results.md` (Case | Engine | Verdict | Time | Cost | What was
  found, with Total), `qa/app/<extid>/verdict.json` + evidence + `draft.yaml`, `qa/app/conversions.json`.
- **`--pending`** per platform: by flow, by agent, not run (no expected result, mock-required, order
  confirmation needed, account no device can hold, unknown flow status, no device).

### 7. Command

```
/test-agent:execute ui-agent [--platform auto|web|android|both] [--android-mode auto|flow|agent]
    [--no-convert] [--app-variant dev-release|debug|prod] [--devices FILE] [--confirm-orders]
    [--feature F --kiwi-plan ID] [--cases …] [--headed]
```

Promotion of validated drafts into maestro-automation is a separate, explicit step:
`python -m workflows.testing.promote_flows <run dir>` copies them onto a branch in a maestro-automation
worktree with a `# STATUS: VALIDATED (green) … from agent run` header — **ASK** before it runs; the user
reviews and merges.

## Phases

**0 — spike (read-only on code, one device, ASK)**
1. Start `maestro mcp`, drive one case by hand through the harness: confirm how a failed `run` shows in
   the trace (`is_error` vs text), that `takeScreenshot` writes into the case folder, how one server
   handles several devices, and the token cost of `inspect_screen` + screenshots. Write findings here.

**A — foundations**
2. Index: `#id` attribution, status parsing, gates, many-to-many (extend `maestro.py`). *Check:* unit
   test over the real flows folder; every VALIDATED flow maps to ≥1 case; unknowns listed.
3. Device preflight + device lock. *Check:* read-only run on this Mac.
4. `direct_surface` in `get_store`, `--platform` resolution, `--pending` per platform. *Check:* web-only
   plans unchanged in tests.

**B — flow engine (no model)**
5. `execute_app` flow path: group cases by flow, run each flow once via the maestro scripts with
   `--no-upload`, enrich, attribute, record. One device, then N.
6. **ASK** Pilot: CART-SANITY plan; compare with the last manual dispatch in Kiwi.

**C — agent engine**
7. `guard_device.py`, matcher + allowlist entry, PyYAML. *Check:* unit tests for every allow/deny rule.
8. `qa-app-agent` skill, MCP server per case, agent path in `execute_app`, generalised call counting,
   `record_row` extension. Flow FAIL → agent confirm run.
9. **ASK** Pilot: 5–8 cases with no flow; record cost and time per case beside the web pilot's.

**D — conversion**
10. `convert_flows` stage: lint (`point:` reject, `index:` flag, appId `${APP_ID}`, header), plain
    `maestro test`, bounded fix loop under the draft-to-green rules, second Kiwi comment.
11. `promote_flows` helper. **ASK** Pilot: convert the passing cases from step 9, promote one batch.

**E — unify and tidy**
12. `execute.py` / `execute.md`: `--platform`, the ask, status/progress, docs.
13. Extract the shared core of `execute_ui_agent` and `execute_app` (web tests stay green).
14. **ASK** End-to-end on a feature with cases on both surfaces.

## Later (not in this plan)

Multi-surface cases (one Kiwi case, two surfaces); auto-promotion after N green runs; iOS through the
same path; running stuck drafts back through the flow-drafter.

## Risks

- **Device capacity**: emulators already drop under ~400 MB free; Android concurrency 1–4.
- **Shared account state** across devices; cohort accounts (care_plan_user, family_user) are one device each.
- **Maestro MCP is young** (server v1.0.0) and `run` is broad; pin the Maestro version and re-check
  tool schemas on upgrade.
- **Cost**: hierarchy JSON and images make Android sessions dearer than web; flow-first keeps the agent
  to the long tail, and conversion shrinks the tail over time.
- **Converted flows** can carry the agent's detours or session assumptions; lint, validation and human
  review before promotion are the controls.
- **Flow status is prose**; 66 flows have no STATUS line and run by agent until stamped.
- **Two publishers**: the integrated path uses only O1's store; the scripts always run with `--no-upload`.
