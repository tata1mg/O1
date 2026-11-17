# `testing` workflow

QA suite generation, evaluation, storage, and backend, web UI and app UI execution. This is `test_agent` (plugin `ai-sdlc`
1.1.0, HEAD `ca7d4ce`) re-expressed on the harness contracts.

What it replaces: one bash script (`tools/run_chain.sh`) that ran `rfc-agent` as a subprocess and then a
single headless `claude -p` turn which was trusted to build a spreadsheet at the right path and call a
storage MCP tool, with nothing checking either. There was no manifest for those steps, no resume, no cost
or latency accounting, and the evaluator skill was wired to nothing at all.

```
generate and upload:  sources -> generate_cases -> render_xlsx -> store_cases -> prerequisites
score, on demand:     evaluate                             rubric + 14 compliance checks (/test-agent:evaluate)
                      (cases stored PROPOSED; a reviewer approves or rejects each one in Kiwi)
run after review:     derive_spec -> execute_be            backend cases (exec:http)
                      derive_ui -> execute_web             web UI cases (exec:ui + surface:web), Playwright
                      publish_app                          app UI cases (exec:ui + surface:android/ios),
                                                           from a Maestro run on a device
```

The default pipeline stops at the upload. `derive_spec` reads only CONFIRMED cases, so it runs after review,
with `execute_be`, which issues real requests against a live service using a captured identity. Opt in with
`--enable testing.execute`, or run them on the upload's run dir with
`--stages derive_spec,execute_be --run-dir <run>` (`--stages` may name a registered stage the composed
pipeline left out).

### Review rules

- Every case is uploaded **PROPOSED**. Only a human sets CONFIRMED (approve) or DISABLED (reject), and only
  CONFIRMED cases are attached to a run.
- A regenerated suite **never overwrites a case a human has touched**: one whose status is no longer
  PROPOSED, or whose `TestCase.history` shows a save by any account other than the agent's. The store leaves
  it exactly as it is (status, fields, tags, plan membership) and comments the change it would have made,
  once. Each such case is a `case_kept_for_human` decision. When the agent's own account is unknown (no
  username in the flags or in `~/.tcms.conf`), every existing case counts as touched. That is why the agent
  needs an account of its own: see [Kiwi account](#kiwi-account).
- `testing.upload_status=CONFIRMED` skips review for the **JSON store only** (offline trials, tests); the
  Kiwi store refuses it. In the JSON store a reviewer edits `status` in `store/cases/<feature>.json`.

## Stages

| Stage | Engine | Requires | Outputs | Gate |
|---|---|---|---|---|
| `sources` | python | — | `qa/sources.json` | PRD and RFC present and non-empty; the inventory names a feature |
| `generate_cases` | model (`skills/qa-test-case-generator`) | `qa/sources.json` | `qa/cases.json`, `qa/scenarios.json` | every schema validator; every `execution: http` case's steps re-parsed through `be_spec.parse_case` with a route and a status. `retry_on_gate_failure=1`, `revise_on_retry=True` |
| `render_xlsx` | python | the two suite files | `qa/<FEATURE>_QA_Suite.xlsx` (conditional) | both sheets present, headers exactly the declared columns, row count equals `cases.json` |
| `evaluate` (on demand) | python + model (`skills/qa-test-case-evaluator`) | the suite + `qa/sources.json` | `qa/eval.mechanical.json`, `qa/eval.json` | schema valid; aggregates recomputed; every FAIL carries a violation naming a real id; `compliance_status` derived not asserted; the model's presence verdict equals the one Python computed. `retry_on_gate_failure=1`, `revise_on_retry=True` |
| `prerequisites` | python + model (`skills/qa-test-prerequisites`) | `qa/cases.json`, `qa/sources.json` | `qa/prerequisites.facts.json`, `qa/prerequisites.json`, `qa/prerequisites.md`, `qa/test_data.template.yaml` | every case is in some item's `used_by` or in `no_prerequisites` (never both); every cited id is a real case; a case carrying a placeholder or an invented value is never prerequisite-free; every item's fields filled; fixture ids `FX-nn` and unique; no secret carries a value. `retry_on_gate_failure=1`, `revise_on_retry=True` |
| `store_cases` | python | `qa/cases.json` | `qa/store.json` | `created + updated + unchanged + protected == total`, and `total` equals the number of cases in the suite |
| `derive_spec` | python | `qa/store.json` | `qa/spec.json`, `qa/spec.report.json` | non-empty; every entry has a path and asserts something. Dropped and status-only cases become decisions |
| `execute_be` | python | `qa/spec.json` | `qa/results.json` | every row carries extid/status/detail/http_status/duration_s; status in PASS/FAIL/BLOCKED/ERROR; the tally matches the rows |
| `derive_ui` | python + model (`skills/qa-ui-playwright`) | `qa/sources.json` | `qa/ui/cases.json`, `qa/ui/roots.json`, `qa/ui/settings.json`, `qa/ui/plan.json`, `qa/ui/tests/*.spec.ts` (+ `login.setup.ts` with `--ui-login`) | every CONFIRMED web UI case tested or skipped with a reason (never both); one test per spec, titled with its case id; no absolute URL, credential literal, fixed sleep or undeclared `page.route` in a spec; every locator cites a file:line that exists in the source snapshot; `playwright test --list` loads exactly the tested ids. No CONFIRMED web case = no model call. `retry_on_gate_failure=1`, `revise_on_retry=True` |
| `execute_web` | python | `qa/ui/plan.json` | `qa/ui/results.json` | as `execute_be`'s: every row complete, status in the four, tally matches |
| `execute_ui_agent` | python + one model session per case (`skills/qa-ui-agent`) | — | `qa/agent/results.json`, `qa/agent/results.md` (the per-case table: verdict, time, cost), `qa/agent/<extid>/verdict.json` + screenshots, `qa/agent/notes.md` | every result row's status is PASS/FAIL/BLOCKED/ERROR and matches its `verdict.json`; the tally matches; not every case ERROR. Each verdict is checked before it counts: every clause met / not met / not reached, evidence files exist, status recomputed from the parts, and at least as many browser calls in the session's trace as parts judged |
| `publish_app` | python | — | `qa/app/results.json` | at least one case recorded; status in the four; tally matches |

Decisions recorded (`logs/decisions.jsonl`): `source_missing`, `xlsx_skipped`,
`case_dropped_by_deriver`, `case_status_only`, `route_rewritten`, `case_not_attached`, `cases_stored`,
`case_kept_for_human`, `response_attached`, `presence_check_mismatch`, `prerequisites_listed`,
`ui_cases_selected`, `ui_case_not_automated`, `ui_evidence_attached`, `app_case_unattributed`.

`sources` also copies every present source into `qa/sources/`, and the persona prompts name those copies:
the persona runs with the run dir as its cwd, and a headless Claude child cannot read outside it without a
permission prompt nobody is there to answer. The persona's SKILL.md is staged into
`.claude/skills/<stage>/` for the same engine, and the request carries `native_ctx`, which the claude-code
adapter requires.

## Prerequisites and test data

`prerequisites` answers "what has to be on prestag before this suite can run", and closes the loop on the
**Test Data** source, which is `blocked` unless `--test-data` names a file.

- **Python** reads the Implementation Plan's `### CHANGE-n <service>: … [REQ-…]` blocks: the services to
  deploy and who owns them, deploy order (waves from each block's `depends on:`), migrations (from `files:`)
  and the staging smoke checks (each `verify:` that mentions staging). A case needs a service when they cite
  a REQ in common. It also lists, per case, the `<placeholders>` it left and whether its test data was marked
  invented. That is `qa/prerequisites.facts.json`.
- **The model** groups every case's precondition and test data into fixtures (one per distinct record
  state), config values, external systems and secrets, each naming the cases that need it, in
  `qa/prerequisites.json`. It can run `check_prerequisites.py`, the stage's own gate, in one call.
- **Python renders** `qa/prerequisites.md` (sections 1-3 and 8 from the plan, 4-7 from the model) and
  `qa/test_data.template.yaml`: one entry per fixture and config key, with the values the suite invented and
  an empty `real_value`. Secrets are listed by name only and never appear in the template.

The loop: the team provisions what `prerequisites.md` lists, fills the template with records that exist on
prestag, and passes it back as `--test-data`. The next generation uses those values instead of inventing
them, and the Test Data row is `present`.

## From chat: `/test-agent:test`

The `test-agent` plugin (`plugins/test-agent/`, listed in this repo's marketplace next to `rfc-agent`) runs
this workflow from Claude Code with Claude as the progress view and the question UI:

1. **Preflight** (`python -m workflows.testing.preflight check … --json`, seconds): the RFC run and its
   sources, Kiwi (settings, `~/.tcms.conf` and its mode, a live probe with a 15 s timeout), Langfuse, the
   `claude` CLI. A failed check stops the run before 40 minutes of generation are spent. An unreachable Kiwi
   is a question, not a stop: retry after the VPN, or generate now with `--disable testing.store` and upload
   later (`--stages store_cases,prerequisites`).
2. **Phase A** in the background, one chat line per stage (`python -m workflows.testing.status <run>
   --milestone <stage>`). In between, `python -m workflows.testing.progress <run> --watch` (the command's
   Monitor) posts what a long stage is doing — elapsed time against that stage's past runs, the phase read
   from the activity log, the count of cases written so far, the model's latest note — when the phase changes
   and as a heartbeat (4 minutes, then spacing out to 10). It hands over to the next Monitor with
   `WATCH rearm --from <offset>`.
3. **The handoff**: case counts, the evaluator's verdict, the Kiwi plan link, the prerequisites and their top
   blockers, the workbook and checklist as files, and the instruction to set approved cases to CONFIRMED.
4. **Phase B** only when the user says so, for the kinds they pick, each only once `status --kiwi` shows
   CONFIRMED cases of that kind: backend (`--stages derive_spec,execute_be --enable testing.execute --curl …`),
   web UI (`--stages derive_ui,execute_web --enable testing.execute_web --base-url … --ui-auth …`), app UI
   (`--stages publish_app --enable testing.publish_app --maestro-report … --maestro-flows …`), all with
   `--env … --build …`.

`/test-agent:test --run-dir <run>` resumes from the run dir alone: `status` decides the next step. Without
`--run-dir`, the command first lists unfinished runs (`preflight runs`) and offers to continue one, and
`preflight check` reports any unfinished run of the same feature and RFC run (`resolved.existing_runs`), so
a feature is not regenerated from scratch by accident. When a run stops, `status --json` carries
`next.why`: the reason in plain words, and whether re-running is the whole fix (a dropped connection is).
Each launch logs to its own `logs/run.<kind>.out` (`generate`, `store`, `backend`, `web`, `app`).

Kiwi settings live in `TESTING` in `~/.config/rfc-agent/config.json` (`preflight setup-kiwi` writes them:
`storage`, `kiwi_web`, `product_prefix`, `kiwi_user`, `kiwi_url`, `insecure_tls`). Any `testing.*` flag can
be set there; the password never is — it stays in `~/.tcms.conf`.

## UI execution: Playwright (web) and Maestro (app)

**Web.** `derive_ui` gives a model the CONFIRMED `exec:ui` + `surface:web` cases and the front-end source the
RFC was written against (the RFC run's repo snapshots, `_snap/*`). It writes one Playwright test per case in
`qa/ui/tests/<extid>.spec.ts`, titled with the case id, and `qa/ui/plan.json`: each tested case's locators,
each citing the `file:line` that renders the element, and each skipped case's reason (a native-app step, a
placeholder value, a human judgement). `execute_web` runs them with the pinned Playwright in
`tools/ui-runner` (installed once: `npm ci && npx playwright install chromium` there; each run's
`qa/ui/node_modules` links to it) against `--base-url`, emulating a phone. Login, one of two ways:
`--ui-login <file>` (`{"phone", "otp"}` of a test account whose number and OTP stay fixed on the
environment): `derive_ui` also writes `tests/login.setup.ts`, a Playwright setup step that logs in from
`UI_LOGIN_PHONE` / `UI_LOGIN_OTP` and saves the session the cases start from, so nothing expires; the values
reach the Playwright process only, and the login step is not a case, so its trace is never attached to the
store. A failed login records every case BLOCKED ("not run — the login step failed …"). Or `--ui-auth
<file>`, a session recorded with `npx playwright codegen --save-storage=<file> <base url>`, which expires.
The base URL can come from the environment: `testing.environments` (the names a run may target) and
`testing.base_url_template` (`https://{env}.1mg.com`) give `--env jupiter` the URL `https://jupiter.1mg.com`;
`preflight setup-web` stores both, and the login file's path, under `TESTING` in config.json, and preflight
checks that file (present, mode 600, `phone` and `otp` set — never printed). `testing.ui_headed` runs a
visible, slowed-down browser so a person can watch; every run also writes Playwright's HTML report
(`qa/ui/html-report`, open with `npx playwright show-report`), with each failure's screenshot and trace.
Status: passed -> PASS; an assertion failed on a loaded page -> FAIL; the page never loaded (`net::ERR_*`,
a navigation timeout) or the test skipped itself -> BLOCKED; the browser never ran -> ERROR. The failure's
screenshot and trace are attached to its execution. Skipped cases are attached to the run and left IDLE for
a person.

**App.** Maestro runs on a device, with the app built from druid and the flows on druid's `AND-106907`
branch (`.maestro/flows`, 198 flows: diagnostics, family, labs, radiology). `publish_app` records what such
a run produced: `maestro.py` (ported from test_agent's `tools/maestro_publish.py`) attributes each JUnit
testcase to the cases its flow's `# Covers:` block and labelled steps name, FAILs a case only when the failing
step names it, and takes the worst verdict across flows. Each execution gets the evidence as a comment (the
step timeline, the failing step, links to the attachments; Kiwi renders an execution's comments, not its
attachments) and the flow's screenshot, log and commands file. The case-id pattern is widened from
test_agent's `TC-[A-Z]+-\d+`, which matched none of the generator's ids.

All three executors write into the same store run, `<feature> · <env> · <build>`.

### Scoring a suite on demand: `/test-agent:evaluate`

`evaluate` is not in the default pipeline: it took ~17 of a run's ~45 minutes and nothing downstream reads its
output, so generation now goes straight to the Kiwi upload. `/test-agent:evaluate [--run-dir R]` (or
`python -m workflows.testing.evaluation`) runs that stage alone on an existing run (`--stages evaluate
--enable testing.evaluate`) and reports the score and the failed compliance checks; `/test-agent:test`'s
handoff shows them when they exist and points to the command when they do not. It scores `qa/cases.json` in
the run dir, not edits made in Kiwi. `status.next_step` never waits on it, so an unscored or a failed
evaluation does not block review or execution.

### Inside Axon (Pi): `/test`

The same three commands exist in Axon as one, `/test`, from the Pi extension
`src/rfc_agent/axon_ext/test-command.js` (installed with `/rfc`: see [docs/AXON.md](../../../docs/AXON.md) §2):

| Claude Code | Axon |
|---|---|
| `/test-agent:test [flags]` | `/test [flags]` |
| `/test-agent:execute [web\|backend\|all] [flags]` | `/test execute [web\|backend\|all] [flags]` |
| `/test-agent:evaluate [--run-dir R]` | `/test evaluate [--run-dir R]` |
| (ask Claude) | `/test status [run dir]`, `/test attach <run dir>`, `/test doctor`, the `test_status` tool |

No model drives it: the extension calls the same helpers the Claude commands name (`preflight`, `status`,
`progress --watch`, `execute`, `evaluation`), asks the same questions as Pi dialogs, and starts the harness
detached, so a run outlives the Axon session (`/test attach` follows it again). Agent stages run on Axon
(`--engine axon`; `execute`, `evaluation` and `preflight check` take `--engine` for this — on axon the check
skips the `claude` CLI) unless you pass `--engine` or `--stage-engine` yourself. Login files and curls are
only checked to exist, never read.

### Executing an existing run: `/test-agent:execute`

`/test-agent:execute [web|backend|all] [--only-existing] [--run-dir R] [--env E] [--build B] [--headed]
[--cases …]` (or `python -m workflows.testing.execute …`) builds the harness line from the run dir: feature
and RFC run from `qa/sources.json`, environment and build from the last results. `web` runs
`derive_ui,execute_web` with `testing.ui_only_new`: tests are written only for CONFIRMED web cases that have
neither a test nor a skip (no model call when there are none), then every test runs. `--only-existing` runs
`execute_web` alone — the tests already written, at once, no model. `--pending` lists which CONFIRMED web
cases have a test and which do not; the chat command uses it to offer that choice. `backend` is
`derive_spec,execute_be` (Python, seconds). The newest testing run is the default; `--print` shows the
command without running it. The backend curl's path can be stored once with
`preflight setup-web --curl <file>`.

### An agent in a browser instead of a script: `ui-agent`

`/test-agent:execute ui-agent` (stage `execute_ui_agent`, flag `testing.execute_ui_agent`) is the alternative
to `derive_ui -> execute_web`. Nothing is derived: one agent per CONFIRMED web UI case reads the case, looks
at the live page through Playwright MCP, acts, looks again, finds another route when the written one does
not work, and writes `qa/agent/<extid>/verdict.json` with a screenshot for every clause it judged.

- **Verdicts.** A case is split into parts; each clause of a part's expected result is `met`, `not met` or
  `not reached`. Any clause not met = FAIL; otherwise any not reached = BLOCKED; otherwise PASS. There is no
  partial pass — how close a case came is reported beside the status (`2 of 3 parts met`). FAIL and BLOCKED
  carry a cause: `product`, `environment`, `case` or `test data`. A FAIL is run a second time in a fresh
  browser (`testing.agent_confirm_fail`); it stays FAIL either way and is marked flaky when it did not
  repeat. ERROR means no usable verdict came back. A case with no expected result is not executed.
- **Batches.** `testing.agent_parallel` cases at a time (5), one browser each; one at a time with
  `--headed`. What a batch learns about the environment (`qa/agent/notes.md`, this run's only) is given to
  the next. Each batch is recorded in the store as it finishes.
- **Limits.** `testing.agent_max_actions` browser actions (60; looking at the page is free), enforced by
  the browser guard, and `testing.agent_case_timeout_s` (900). The agent writes its verdict file before its
  first browser call and rewrites it after each part, so a session cut off at the limit still leaves what
  it had judged (BLOCKED, cause `limit`).
- **Containment.** This is the one stage whose MCP tools act. Only the tools in
  `rfc_agent.mcp_tools.BROWSER_*` are granted (no script evaluation, upload, cookie or route tools);
  `rfc_agent/hooks/guard_browser.py` refuses a navigation off the run's base address; the base address must
  be a named test environment (`testing.agent_hosts`, or `testing.base_url_template` with an environment
  from `testing.environments`; `execute ui-agent --hosts <host>` passes it); and the agent is never given a
  credential — login is a recorded session (`--ui-auth`), `testing.ui_login` is ignored, and without a
  session the cases run logged out. Claude Code engine only: the guard is a Claude Code hook. The guard cannot see where a click leads, so none of this
  makes the stage safe to point at production.
- **Needs.** `npm ci` in `tools/ui-runner` (pins `@playwright/mcp`), and either that package's own Chromium
  build (`node tools/ui-runner/node_modules/@playwright/mcp/node_modules/playwright/cli.js install chromium`
  — not the runner's `npx playwright install`, which installs another revision) or
  `testing.agent_browser=chrome`. The stage checks this before any session starts.
- **Straight from Kiwi.** `execute ui-agent --feature <F> [--kiwi-plan <id>]` needs no earlier run: it
  creates the run folder and reads the plan as it is (`testing.kiwi_direct`): a CONFIRMED case with no
  `extid:` tag is `KIWI-<case id>`, and one with no `exec:` tag is treated as a UI case. Without the
  generated prerequisites a case must state its own preconditions, or it ends BLOCKED (`test data`).
- **Cost.** A model session per case on every run, where a script is written once. Not yet measured.

### Evidence and Kiwi's upload limit

Failure screenshots are uploaded as JPEG at 720 px wide (Pillow, else macOS `sips`): a 918 KB PNG becomes
~47 KB. Every file sent to Kiwi is capped at 700 KB because the request goes base64 through XML-RPC, and the
ingress in front of plutokiwi-agent rejects bodies over ~1 MB (ingress-nginx's default `proxy-body-size`),
well under Kiwi's own `FILE_UPLOAD_MAX_SIZE` (5 MB) and the container nginx's `client_max_body_size` (10 MB).
Traces stay in the local HTML report. A failed case's comment holds the reason, the failing requests and the
screenshot link; a property value is capped at 255 characters, Kiwi's column size.

## Inputs

`--feature` is required. Everything else is optional, and `--rfc-run-dir` is the normal way to supply the
sources.

| Input | Meaning |
|---|---|
| `--feature` | The store's product key, the run-dir name and the xlsx filename stem. NOT the ID prefix inside case ids — the generator picks that mnemonic itself |
| `--rfc-run-dir` | A completed RFC run dir. `sources` reads `raw/prd.md`, `out/rfc.md`, `out/contract.md`, `out/detail/plan.md` and `raw/figma.md` (or `raw/figma_index.md`) from it. Current rfc-agent runs have no contract or plan file: they are sections 5 and 6 of `out/rfc.md`, so `sources` cuts the `## … API contract` and `## … Implementation plan` sections out of the RFC instead and records a `source_extracted` decision |
| `--prd` `--rfc` `--contract` `--plan` `--figma` `--test-data` | Explicit paths, each overriding the corresponding file in `--rfc-run-dir` |
| `--from-cases` | Skip generation: load an existing `cases.json` or `.xlsx` as the suite. It is still held to the full `generate_cases` gate |
| `--curl` `--env` `--build` `--route-map` | `execute_be` only (`--env` and `--build` name the run for every executor) |
| `--base-url` `--ui-login` `--ui-auth` | `execute_web` |
| `--maestro-report` `--maestro-flows` `--maestro-map` `--maestro-blocked` | `publish_app` |

## Flags

All 47 are `testing.*`, registered in `flags.py` with help text, and declared on `WORKFLOW.flags`. Today
they are introspectable through the registry — after workflow discovery (`harness.workflows.names()`,
which imports each workflow package and so registers its flags), `harness.flags.describe(owner="testing")`
returns the machine-readable rows, including type, default, env var and help. `harness flags --workflow testing
--json` is the intended CLI surface for the same data once the harness CLI lands.

Layers, highest first: `--set`/`--enable`/`--disable`, the flag's env var, `TESTING.<name>` in
config.json (`workflows.testing.config_get`), the workflow default, the flag default. The config layer is
what keeps a resume on the same store: before it, a rerun that left out `--set testing.storage=kiwi`
silently used the JSON store.

`testing.evaluate`, `testing.store` and `testing.execute` compose the pipeline rather than gating a stage
from inside it: `compose_pipeline(flags)` maps them to a stage list, and the harness CLI calls it (as
`Workflow.compose_pipeline`) for every run without `--pipeline`. Turning `testing.store` off also
drops `derive_spec` and `execute_be`, since both read from the store. The harness-level equivalent is
`--disable-stage <name>` or `--stages <names>`, and both routes end at the same list.

| Flag | Type | Default | What it is for |
|---|---|---|---|
| `testing.storage` | `json`\|`kiwi` | `json` | Case store backend. `json` lives under `<run_dir>/store/` and never opens a socket |
| `testing.feature` | str | `""` | Pinned feature key when `--feature` is not passed on the CLI |
| `testing.product_prefix` | str | `"Diagno / "` | Store product-name prefix |
| `testing.plan_template` | str | `"{feature} — imported test cases"` | Test-plan name |
| `testing.run_template` | str | `"{feature} · {env} · {build}"` | Test-run summary, which is also the run's reuse key |
| `testing.upload_status` | `PROPOSED`\|`CONFIRMED` | `PROPOSED` | Status a case is stored with. CONFIRMED skips review and is accepted by the JSON store only |
| `testing.exec_tag` | str | `"exec:http"` | The tag a case must carry to be machine-executable |
| `testing.evaluate` | bool | `False` | Run the evaluator stage in the pipeline. Off: it runs on demand (`--stages evaluate --enable testing.evaluate`, or `/test-agent:evaluate`) |
| `testing.store` | bool | `True` | Run `store_cases` |
| `testing.prerequisites` | bool | `True` | Run `prerequisites` |
| `testing.execute` | bool | `False` | Run `execute_be` |
| `testing.execute_web` | bool | `False` | Run `derive_ui` and `execute_web` (Playwright, web UI cases) |
| `testing.publish_app` | bool | `False` | Run `publish_app` (a Maestro report, app UI cases) |
| `testing.execute_ui_agent` | bool | `False` | Run `execute_ui_agent` (an agent drives a browser through each web UI case) |
| `testing.agent_hosts` | str | `""` | `execute_ui_agent`: hosts the agent may drive, comma-separated; never production |
| `testing.agent_parallel` | int | `5` | `execute_ui_agent`: cases per batch, one browser each |
| `testing.agent_max_actions` | int | `60` | `execute_ui_agent`: browser actions per case |
| `testing.agent_case_timeout_s` | int | `900` | `execute_ui_agent`: wall time per case |
| `testing.agent_confirm_fail` | bool | `True` | `execute_ui_agent`: re-run a FAIL once in a fresh browser |
| `testing.agent_forbid` | str | (a regex) | `execute_ui_agent`: targets no browser action may touch — NeuCoins/wallet, applying coupons, checkout and payment, Care Plan add/remove, saving or deleting an address/patient, prescription upload, logout. Refused by the browser guard; the clause becomes `not reached`. Empty = nothing forbidden. A tripwire on the agent's own description of the element, not a sandbox |
| `testing.agent_read_only` | bool | `False` | `execute_ui_agent`: tell the agent to change nothing (`execute ui-agent --read-only`); an instruction, not a block |
| `testing.agent_include_manual` | bool | `False` | `execute_ui_agent`: also take CONFIRMED web cases tagged `exec:manual` |
| `testing.agent_code_context` | bool | `False` | `execute_ui_agent`: give the agent read-only code lookup (fleetlens), for finding a route only |
| `testing.agent_browser` | str | `""` | `execute_ui_agent`: browser or channel for Playwright MCP; empty = its bundled Chromium |
| `testing.kiwi_plan_id` | str | `""` | Read cases from, and record into, this Kiwi plan by id (`--kiwi-plan`) |
| `testing.kiwi_direct` | bool | `False` | Run a Kiwi plan this workflow did not upload; set by `execute --feature … --kiwi-plan …` |
| `testing.xlsx` | bool | `True` | Render the workbook (needs the optional extra) |
| `testing.require_case_per_scenario` | bool | `True` | Gate every scenario to at least one case |
| `testing.dry_run` | bool | `False` | Execute but write nothing to the store |
| `testing.offline` | bool | `False` | Refuse every network call: parse and validate only |
| `testing.insecure_tls` | bool | `True` | Skip TLS verification when connecting to the store. **Default `True` to match today's behaviour** (see below) |
| `testing.curl_path` | str | `/usr/bin/curl` | curl binary |
| `testing.timeout_s` | int | `30` | Per-request timeout |
| `testing.curl` / `.env` / `.build` / `.route_map` | str | `""` | `execute_be` inputs, also settable as CLI inputs |
| `testing.attach_responses` | bool | `False` | Attach each case's full JSON response to its execution. Off because Kiwi attachments are readable by every account |
| `testing.cohort_path` | str | `""` | Dotted response path recorded as the `cohort_observed` execution property. Empty = not recorded |
| `testing.base_url` | str | `""` | `execute_web`: the web app's origin on the target environment (`--base-url`) |
| `testing.ui_auth` | str | `""` | `execute_web`: Playwright storage-state file with a logged-in session (`--ui-auth`); passed through, never read |
| `testing.ui_only_new` | bool | `False` | `derive_ui`: keep written tests, derive only CONFIRMED web cases without one |
| `testing.environments` | str | `""` | Comma-separated environments a run may target; the command offers them |
| `testing.base_url_template` | str | `""` | `execute_web`: base URL with `{env}`, used when `--base-url` is absent |
| `testing.ui_headed` | bool | `False` | `execute_web`: a visible, slowed-down browser to watch the suite |
| `testing.ui_login` | str | `""` | `execute_web`: JSON `{"phone", "otp"}` of a test account; a login step runs before the cases (`--ui-login`) |
| `testing.ui_device` | str | `"Pixel 7"` | `execute_web`: device emulated (the web cases are mweb); empty = desktop |
| `testing.ui_timeout_s` | int | `60` | `execute_web`: per-test timeout |
| `testing.ui_source_roots` | str | `""` | `derive_ui`: comma-separated source trees for locators; empty = the RFC run's `_snap/*` |
| `testing.maestro_report` | str | `""` | `publish_app`: JUnit report of a Maestro run (`--maestro-report`) |
| `testing.maestro_flows` | str | `""` | `publish_app`: the flows folder that run used (`--maestro-flows`) |
| `testing.maestro_map` | str | `""` | `publish_app`: JSON `{flow: [case ids]}` overriding the derived mapping |
| `testing.maestro_blocked` | str | `""` | `publish_app`: curated exclusions, recorded BLOCKED |
| `testing.maestro_only_folder` | str | `""` | `publish_app`: publish only flows under `.maestro/flows/<this>/` |
| `testing.app_exec_tag` | str | `"exec:ui"` | `publish_app`: tag a case needs to be recorded; empty = any CONFIRMED case a flow covers |
| `testing.kiwi_url` / `.kiwi_user` / `.kiwi_password` | str | `""` | Kiwi credentials (env `TCMS_API_URL` / `TCMS_USERNAME` / `TCMS_PASSWORD`). Unless all three are set, the connection uses `~/.tcms.conf` and the agent is the username in that file |
| `testing.kiwi_web` | str | `https://localhost:8443` | Base URL used only to build the printed plan/run links (env `KIWI_WEB`) |

This workflow also re-defaults one harness flag: `hook_canary` is **off**. The canary checks that rfc-agent's `byte_budget` feedback reaches the model, and that hook only paces `rfc.md` / `contract.md` / `plan.md`, which no testing stage writes. `--enable hook_canary` runs it anyway.

### `testing.insecure_tls`

It defaults to `True` so a run behaves exactly as it does today, and that default is the only thing about
it that is unchanged. Disabling verification lets any machine on the network path read and rewrite
everything on the wire, credentials included. The fix is to trust the deployment's CA — add its
certificate to the system trust store, or point `SSL_CERT_FILE` at it — and then
`--disable testing.insecure_tls`. Flipping the default here would break every existing Kiwi deployment
silently, so the flag's help text carries the warning and the remedy.

What did change is the **scope**. `test_agent` implemented it as
`ssl._create_default_https_context = ssl._create_unverified_context`, in four modules, at import time.
That assignment is process-global and permanent: once any Kiwi code had run, every later HTTPS call in
the same process — Atlassian, Figma, an engine adapter fetching anything — stopped verifying
certificates, for the life of the process. Here an `ssl.SSLContext` with `check_hostname=False` and
`verify_mode=CERT_NONE` is built per connection and attached to that transport; the process default is
swapped only across `tcms-api`'s own constructor (which handshakes inside itself) and restored in a
`finally`, including when the connection raises. `health()` reports `tls_verified` so an operator can
see the posture, and `tests/testing/test_secrets_and_tls.py` asserts the global default is left as it was
found.

### Credentials

`testing.kiwi_password` is declared in `flags.SECRETS` and registered with `Flag(secret=True)` so it is
redacted from `flags.json`, `harness flags --json` and `--explain` tables. The marker is applied behind a
`dataclasses.fields` probe: the kernel's `secret` field is still landing, and passing an unsupported
kwarg would raise at import time — which, since workflow discovery imports this module, would break every
CLI invocation rather than just a Kiwi run. `SECRETS` is the workflow's declaration either way, and a
test fails if any `testing.*` flag whose name says credential is missing from it.

`testing.kiwi_user` is deliberately not redacted. It is not a secret on its own, it is already written to
the Kiwi TestRun's `manager` field where every account can read it, and "which account did this run use"
is the first question when a run's writes land in the wrong place.

### Kiwi account

`testing.storage=kiwi` needs one Kiwi account that only the agent uses. Call it **`test-agent`**.

- **Not an admin, not a superuser, not anyone's personal account.** The store protects every case saved by
  any account other than its own (see [Review rules](#review-rules)). If the agent shared an account with a
  reviewer, it would overwrite that reviewer's edits.
- **Reviewers use their own accounts.** Approving a case (CONFIRMED) is a save by someone other than
  `test-agent`, and that save is what tells the store a person has taken over.
- **Credentials go in `~/.tcms.conf`, mode 600**, never in the repo or on the command line. The store
  reads its own username from the same file, so a run needs no `testing.kiwi_user`:

  ```ini
  [tcms]
  url = https://<kiwi-host>/xml-rpc/
  username = test-agent
  password = <password>
  ```

  For CI, set all three of `TCMS_API_URL`, `TCMS_USERNAME` and `TCMS_PASSWORD`. If any one is missing,
  the store falls back to the file.

**Permissions.** Kiwi's `Tester` group is empty unless an admin has run `./manage.py refresh_permissions`.
Give `test-agent` (or a group it belongs to) exactly these. Each one is needed by an RPC call in
`store/kiwi.py`:

| App | Permissions |
|---|---|
| `attachments` | add, delete, view `attachment` |
| `auth` | view `user` (the store finds its own id) |
| `django_comments` | add, view `comment` |
| `management` | add + view `build`, `classification`, `product`, `tag`, `version`; view `priority` |
| `testcases` | add `category`, `testcase`, `testcaseplan`, `testcasetag`; change `testcase`; view `category`, `testcase`, `testcasestatus` |
| `testplans` | add `testplan`; view `plantype`, `testplan` |
| `testruns` | add `testexecution`, `testexecutionproperty`, `testrun`; change `testexecution`; view `testexecution`, `testexecutionstatus`, `testrun` |

That is 35 in all. Grant nothing for `auth` beyond view `user`, and no delete permission outside
attachments: the agent never deletes a case, plan or run. If a permission is missing, Kiwi reports
`Authentication failed when calling "<Api.method>"` even though the login itself worked. The method it
names tells you which permission to add.

The instance also needs the case statuses `PROPOSED` and `CONFIRMED` (Kiwi ships both) and at least one
plan type.

## Artifact durability

Every file this workflow declares canonical goes through `_io.atomic_write_text` (unique temp, `fsync`,
`os.replace`) rather than `Path.write_text`, which truncates before it writes. The repo had already paid
for this twice: a torn `manifest.json` bricked a run dir because every later invocation crashed at load,
and a fixed temp name then let two invocations interleave into one temp file. `qa/cases.json` is exactly
as load-bearing, so it gets the same treatment.

Readers are strict where a gate can report and tolerant where it cannot. `read_json_gated` returns
failures naming the file, and distinguishes an absent artifact from a truncated one from content that is
not JSON at all — the advice differs in each case. `read_json` stays tolerant, because prompt building
runs before any gate and must not explode on a half-written inventory. The JSON store raises a
`GateFailure` naming the file instead of letting `JSONDecodeError` escape out of `list_cases` and repeat
on every retry.

Every read-modify-write on the store — `upsert_cases`, `create_run`, `record_execution` — runs under an
advisory `flock` on a sidecar `<name>.lock`. The sidecar rather than the artifact itself, because the
atomic write replaces the inode and a lock on the old file would protect nothing once the rename landed.
Without it, two invocations sharing a run dir silently lost each other's executions, and nothing
downstream noticed because `qa/results.json` is written by the stage independently of the store.

TODO(harness): `atomic_write_text` and `locked` are not workflow-specific and belong beside the manifest
writer in the kernel, with `rfc_agent.runner._atomic_write` becoming a re-export. The implementation here
is deliberately identical so the move is a delete-and-import.

## What changed, and why

1. **JSON is canonical; the xlsx is a render** (D8). The generator writes `qa/cases.json` and
   `qa/scenarios.json` and the workbook is produced by deterministic Python. Before, the workbook was the
   only artifact and the model built it itself with unrestricted `Bash`, so no gate could check the sheet
   names, the columns, the enums, the ID convention or the step grammar.
2. **The evaluator is a stage.** Its deterministic half runs in Python and its model half is checked against it.
3. **The presence check is computed twice.** The evaluator SKILL calls it "the one thing that's never
   subjective" and then asks a model to do it. Python computes it independently; a disagreement fails the
   stage and shows both verdicts, because one of the two is wrong and which one matters.
4. **The http step grammar has one implementation.** It was prose in the SKILL, restated in the shell
   heredoc, and encoded a third time as regexes in `be_spec.py`. The `generate_cases` gate now runs
   `be_spec.parse_case` — the same parser `derive_spec` will use — so "executable" is decided once, at
   generation time, instead of silently at derivation time weeks later.
5. **The Excel importer keeps the `Execution` column.** `import_cases.parse_cases` had no `execution` key
   in its column map, so every case imported from a workbook lost its classification, never got an
   `exec:` tag, and was reported `skipped_wrong_execution` at run time. `xlsx.parse_cases` maps it.
6. **No password default in code.** `kiwi_runner.py:28-30` and `be_requirements.py:135-137` shipped the
   admin password as a literal, and `make import` wrote it into `~/.tcms.conf`. Credentials come from
   flags (env `TCMS_*`) or `~/.tcms.conf`; an incomplete triple falls back to `~/.tcms.conf` rather than
   guessing. **This is a behaviour change**: a deployment that relied on the literal must now set
   `TCMS_PASSWORD` (or configure `~/.tcms.conf`).
7. **One product-matching rule instead of three.** `kiwi_writer` matched the product by exact name,
   `kiwi_runner` and `be_spec` by `name.endswith("/ <feature>") or name.endswith(feature)`, and
   `be_requirements` by substring `feature in name` — so a product that resolved for the writer could
   resolve to a *different* product for a reader. The single rule is **exact match on
   `<product_prefix><feature>`** — the name the writer creates — with the `endswith("/ <feature>")` form
   kept only as a fallback for products created before the prefix was configurable. The substring rule is
   gone entirely; it was the loosest of the three and the only one that could match an unrelated product.
8. **Storage is a protocol** (`store/__init__.py`), with a JSON backend as the default. Docker plus a
   running Kiwi is no longer a prerequisite for testing generation, evaluation, derivation or execution.
9. **`--dry-run` and `--offline` are different things.** `be_execute --dry-run` still read Kiwi to derive
   a spec and still fired a live preflight request. `testing.dry_run` means "write nothing";
   `testing.offline` means "open no socket".
10. **The feature-specific field is a flag.** `cohort_observed` was hardcoded to
    `data.mix_panel_data.labs_careplan_variant` in the generic executor. It is `testing.cohort_path`, and
    empty means the property is not written.
11. **Results are persisted locally.** `qa/results.json` records every case's extid, status, detail, HTTP
    status and duration. Before, results went only into Kiwi and to stdout, so a run whose terminal
    scrolled away left no local evidence.
12. **Every step is in the manifest.** Status, attempts, gate verdict, cost, duration and inputs digest
    per stage, which is what makes the chain resumable and green-skippable. `--skip-rfc` was the only
    resume knob before.
13. **Hardcoded constants became flags.** curl path, request timeout, `EXEC_TAG`, product prefix, plan and
    run name templates, route-map path. The keep-awake `caffeinate` wrapper is the harness's `keep_awake`
    flag rather than a self-re-exec inside a workflow script.
14. **Two rules the port deliberately did not mechanise.** The generator SKILL grants a prose exception
    letting pure analytics scenarios stay scenario-only, and that cannot be detected from the artifact —
    so the gate requires a case per scenario and `testing.require_case_per_scenario` is the documented
    escape hatch. Likewise nine of the evaluator's fourteen compliance gates (3, 5, 6, 7, 9, 10, 12, 13,
    14) and all eight rubric dimensions need a requirement read and an opinion formed; they stay with the
    model.
15. **The Kiwi store keeps its own tags in step.** test_agent's writer only ever added tags, so a case
    retagged `manual` -> `http` carried both (NR-DIGICHEM, 2026-11-02: 19 cases), and one retagged the other
    way would still have been selected by `create_run`'s `exec:http` check. An upsert now removes a stale
    `scenario:`/`surface:`/`exec:` tag and adds the missing ones; anything else on the case is a person's
    tag and stays. A reviewed case is not retagged: the change goes into its suggestion comment.

## What still needs Kiwi, Docker or a live environment

Nothing in the default pipeline. With `testing.storage=json` the five default stages run entirely offline,
which is how the test suite exercises them.

| Needs | For what |
|---|---|
| A reachable Kiwi + `tcms-api` (extra `testing`: `uv sync --extra testing`) + the `test-agent` account ([Kiwi account](#kiwi-account)) | `testing.storage=kiwi` for `store_cases`, `derive_spec` and `execute_be`'s run/executions. The backend is unit-tested against a fake XML-RPC object (`tests/testing/test_kiwi_store.py`), which covers the logic but not whether `tcms-api`'s real client accepts the calls, nor the read-only `actual_duration` behaviour the start/stop-date trick works around |
| Docker | Only to *host* Kiwi. `test_agent`'s `docker-compose.yml` and `make build-arm64` are not ported; point `testing.kiwi_url` at whatever instance you have |
| A captured curl with live cookies, plus network reach to the target host | `execute_be`. Capture it from the browser's network tab; a hand-trimmed cookie jar returns 403 Bad CSRF where the full captured one returns 200 |
| `--env` and `--build` | `execute_be` records both on the run and on every execution property |
| `openpyxl` (extra `testing`) | `render_xlsx`. Absent, the stage records `xlsx_skipped` and moves on |
| A model engine (`ctx.engine`) | `generate_cases` and `evaluate`. Use `--from-cases` to run the rest of the chain over an existing suite with no model at all |

Not ported (and not needed by any stage): `tools/be_requirements.py` and the
`config/be-requirements/*.yaml` it produced. Nothing ever read those YAMLs back — `be_execute` did not
consume them, and two incompatible schemas coexisted in the repo. The MCP server (`mcp_server/kiwi_mcp.py`)
is also not ported: `store_cases` writes to the store directly, so there is no tool for a model to call.

## Parity with test_agent

| test_agent behaviour | Where it lives now |
|---|---|
| `run_chain.sh` investigate (`rfc-agent` as a subprocess) | Gone. The RFC run is a separate `harness run rfc`; `--rfc-run-dir` points at its output |
| `run_chain.sh:31-34` `caffeinate` self-re-exec | Harness flag `keep_awake` |
| `run_chain.sh:43-66` hand-rolled re-declaration of every rfc-agent flag | Gone. Each workflow owns its own inputs |
| `run_chain.sh:102-104` "no RFC at ..." exit 2 | `sources` gate |
| `run_chain.sh:124-129` prefer `raw/prd.md` / `raw/figma.md` over a URL | `stages/sources._resolve` |
| `run_chain.sh:131-178` the stage-2 prompt | `stages/generate_cases._build_prompt`, minus the storage call and asking for JSON |
| `run_chain.sh:174` "call kiwi_upsert_test_cases exactly once" | `store_cases`, in Python, with a checked summary |
| `run_chain.sh:162` `qa/<FEATURE>_QA_Suite.xlsx` | `stages/render_xlsx.workbook_rel` (same name, same space-to-underscore rule) |
| `skills/qa-test-case-generator/SKILL.md` | `skills/qa-test-case-generator/SKILL.md`, verbatim + a "Harness output contract" section |
| `skills/qa-test-case-evaluator/SKILL.md` | `skills/qa-test-case-evaluator/SKILL.md`, verbatim + a "Harness output contract" section |
| The ID convention, TYPE table, enums, mandatory columns, TS↔TC linkage, NEG+EDGE per module | `schema.py` validators, run by the `generate_cases` gate |
| Evaluator presence check + gates 1/2/4/8 | `mechanical.py`, run by the `evaluate` stage |
| Evaluator rubric + gates 3/5/6/7/9/10/12/13/14 | Still the model's, checked for shape and arithmetic by the `evaluate` gate |
| `be_spec.parse_case` grammar (`RE_SEND`, `RE_STATUS`, `RE_MATCHES`, `_undelimit`, drop reasons) | `be_spec.py`, unchanged |
| `be_spec.confirmed_http_cases` (direct XML-RPC scan) | `be_spec.confirmed_http_cases(store, ...)` via `CaseStore.list_cases` |
| `be_spec.derive` / `report` (stdout) | `stages/derive_spec`, writing `qa/spec.json` + `qa/spec.report.json` and recording decisions |
| `be_execute.run_case` classification order and every measured rule behind it | `be_execute.run_case`, unchanged |
| `be_execute.parse_curl`, `dig` | `be_execute.parse_curl`, `dig`, unchanged |
| `be_execute.apply_route_map` | `route_map.apply_route_map` (map path now always explicit) |
| `be_execute` preflight, exit 3 | `stages/execute_be`, raising `EngineFailure` |
| `be_execute` "nothing to run", exit 4 | `stages/execute_be`, raising `GateFailure` |
| `be_execute` per-case stdout + tally | `qa/results.json` + the stage's `final_text` |
| `EXEC_TAG`, `/usr/bin/curl`, timeout 30, `cohort_observed` path | Flags `testing.exec_tag`, `.curl_path`, `.timeout_s`, `.cohort_path` |
| `kiwi_writer.upsert_cases` (ensure-by-name walk, extid idempotency, re-CONFIRM after `add_case`) | `store/kiwi.py:upsert_cases`: uploads PROPOSED, drops the re-CONFIRM pass, never overwrites a human-touched case (see Review rules) |
| `kiwi_writer.render_text`, `case_tags`, `PRIORITY_MAP` | `schema.render_text`, `schema.case_tags`, `store/kiwi.PRIORITY_MAP` |
| `kiwi_runner.create_run` (CONFIRMED gate + `require_tag` gate, run reuse by summary) | `store/kiwi.py:create_run` and `store/json_store.py:create_run` |
| `kiwi_runner.record_execution` (start/stop dates because `actual_duration` is read-only, 500/900 truncation) | `store/kiwi.py:record_execution`, plus test_agent's later fix (0188771): dates carry an explicit offset and `finished_at` is the real end time |
| `kiwi_runner.add_attachment` / `attachment_url` (0188771, after the original port) | `CaseStore.attach` / `attachment_url` on both stores; scopes execution, case, run, plan |
| `import_cases.py` stdlib xlsx reader + header aliasing | `xlsx.read_sheets` / `parse_cases` / `parse_scenarios`, with `execution` added |
| `import_cases.py --dry-run` preview | `--from-cases` plus the `generate_cases` gate, which checks more than the preview showed |
| `tests/test_be_spec.py` (14 checks) | `tests/testing/test_be_spec.py`, same assertions, source lines cited |
| `tests/test_be_execute.py` (17 checks) | `tests/testing/test_be_execute.py`, same assertions and mocks |
| `tests/test_route_map.py` (6 checks) | `tests/testing/test_route_map.py`, same assertions |

The three converted files hold 37 original assertions, not the 39 the survey reports: `test_be_spec.py`
has 14 `check(...)` calls, not 16. Counted at `ca7d4ce` with `grep -c '^check(' tests/test_be_spec.py`.
| `commands/{ai-sdlc,be-execute,generate-test-cases,evaluate-test-cases}.md` | Not ported. The in-session slash commands remain a human affordance in `test_agent` (D14); the headless path is this workflow |
| `tools/be_requirements.py`, `config/be-requirements/*.yaml` | Not ported — nothing read them back (see above) |
| `mcp_server/kiwi_mcp.py`, `docker-compose*.yml`, `Makefile` | Not ported — deployment, not workflow |
