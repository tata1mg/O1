"""Every `testing.*` toggle, registered once.

Ported from test_agent, where the same knobs were (a) hardcoded constants in `tools/be_execute.py` and
`importer/kiwi_*.py`, (b) bare `os.environ` reads, or (c) shell variables in `tools/run_chain.sh`. The
survey's "hardcoded constants that should be flags" list (02-test_agent-survey.md §7) is the source of
this module; each flag below names the constant it replaces so the parity check is mechanical.

WHY a registry instead of env reads: an agent driving this workflow has to know what it may toggle, and a
human debugging a run has to know which layer won. `harness flags --json` answers both; `os.environ.get`
answers neither.
"""

from __future__ import annotations

import dataclasses

from harness.flags import Flag, Scope, define

OWNER = "testing"

#: Flag names this workflow registers — surfaced through `Workflow.flags` for `harness flags --workflow`.
NAMES: tuple[str, ...] = ()

#: Flags whose VALUE is a credential and must never reach `flags.json`, `harness flags --json`, an
#: `--explain` table or a CI log. This tuple is the workflow's declaration and stands whether or not the
#: kernel can honour it yet (see `_SECRET_SUPPORTED`).
#:
#: `testing.kiwi_user` is deliberately NOT here. A username is not a secret on its own, it is written to
#: the Kiwi TestRun's `manager` field where every account can already read it, and "which account did
#: this run use" is the first question when a run's writes land in the wrong place. Redacting it would
#: cost real debuggability for no confidentiality gain.
SECRETS: tuple[str, ...] = ("testing.kiwi_password",)

#: `harness.flags.Flag` grew a `secret` marker as part of review blocker 1.1. Until that lands in the
#: merged kernel, passing the kwarg would be a TypeError at import time — and this module is imported by
#: workflow discovery, so that would break every CLI invocation, not just a kiwi run. Detect instead of
#: assume; `SECRETS` remains the source of truth either way.
_SECRET_SUPPORTED: bool = any(f.name == "secret" for f in dataclasses.fields(Flag))


def _f(name: str, type_: type, default, help_: str, **kw) -> str:
    full = f"testing.{name}"
    if full in SECRETS and _SECRET_SUPPORTED:
        kw["secret"] = True
    define(Flag(full, type_, default, help_, Scope.WORKFLOW, owner=OWNER, **kw))
    return full


NAMES = (
    # ---- storage ------------------------------------------------------------------------------------
    _f("storage", str, "json",
       "Case store backend. 'json' keeps everything under <run_dir>/store/ and never touches the network; "
       "'kiwi' talks XML-RPC to a Kiwi TCMS instance (needs tcms-api + a reachable server).",
       choices=("json", "kiwi")),
    _f("feature", str, "",
       "Feature key: the store's product key, the run-dir name and the xlsx filename stem. "
       "Normally set from --feature; the flag exists so a config file can pin it."),
    _f("product_prefix", str, "Diagno / ",
       "Prefix for the store's product name (was hardcoded 'Diagno / ' in kiwi_writer.py:82)."),
    _f("plan_template", str, "{feature} — imported test cases",
       "Test-plan name template (was hardcoded in kiwi_writer.py:97). Placeholder: {feature}."),
    _f("run_template", str, "{feature} · {env} · {build}",
       "Test-run summary template; also the run's identity for reuse (was kiwi_runner.py:62). "
       "Placeholders: {feature} {env} {build}."),
    _f("upload_status", str, "PROPOSED",
       "Status a case is stored with. PROPOSED (default): a human approves it (CONFIRMED) or rejects it "
       "(DISABLED) before it can run. CONFIRMED skips that review and is accepted by the JSON store only, "
       "for offline trials and tests; the Kiwi store refuses it.",
       choices=("PROPOSED", "CONFIRMED")),
    _f("exec_tag", str, "exec:http",
       "The tag a case must carry to be machine-executable (was EXEC_TAG in be_execute.py:25)."),
    # ---- pipeline shape ------------------------------------------------------------------------------
    _f("evaluate", bool, False,
       "Run the evaluator stage (presence checks in Python, rubric checked by the model) in the pipeline. "
       "OFF by default: it runs on demand on an existing run (`--stages evaluate --enable testing.evaluate`, "
       "or /test-agent:evaluate), so ~17 minutes of scoring no longer delay the upload for review."),
    _f("store", bool, True, "Run the store_cases stage (upsert the suite into the case store)."),
    _f("prerequisites", bool, True,
       "Run the prerequisites stage: qa/prerequisites.md (services, deploy order, config, secrets, test "
       "data fixtures prestag needs) and qa/test_data.template.yaml for the team to fill."),
    _f("execute", bool, False,
       "Run the execute_be stage. OFF by default because it issues real requests against a live "
       "service and records results; opt in with --enable testing.execute."),
    _f("execute_web", bool, False,
       "Run derive_ui and execute_web: Playwright tests for the CONFIRMED web UI cases, run against "
       "--base-url with the tester's recorded session. OFF by default: it drives a browser against a live "
       "environment as a real user and records results."),
    _f("publish_app", bool, False,
       "Run publish_app: record a Maestro run's JUnit report as one execution per CONFIRMED app UI case. "
       "OFF by default: it writes results into the shared store."),
    _f("xlsx", bool, True,
       "Render the derived two-sheet workbook. Needs the optional extra `testing` (openpyxl); when that "
       "is absent the stage records a decision and skips instead of failing the run."),
    _f("require_case_per_scenario", bool, True,
       "Gate: every scenario must have at least one test case. The generator SKILL grants a prose "
       "exception for pure analytics scenarios, which cannot be detected mechanically — disable this "
       "flag for a suite that deliberately ships scenario-only analytics rows."),
    # ---- safety ---------------------------------------------------------------------------------------
    _f("dry_run", bool, False,
       "Execute but write nothing to the case store (be_execute --dry-run). Requests still go out."),
    _f("offline", bool, False,
       "Refuse every network call: no store connection, no preflight, no curl. Parse-and-validate only. "
       "test_agent had no such mode — its --dry-run still read Kiwi and still made a live preflight "
       "request (be_execute.py:256-284)."),
    _f("insecure_tls", bool, True,
       "INSECURE: skip TLS certificate and hostname verification when connecting to the case store. Any "
       "machine on the network path can then read and rewrite everything on the wire, including the "
       "credentials. Default True only to match today's behaviour (the Kiwi deployment uses a "
       "self-signed cert and all four test_agent modules monkeypatched ssl globally). The fix is to "
       "trust the deployment's CA — add its cert to the system trust store, or point SSL_CERT_FILE / "
       "REQUESTS_CA_BUNDLE at it — then '--disable testing.insecure_tls'. When on, the unverified "
       "context is built per connection and the process-wide default is left untouched."),
    # ---- BE execution ---------------------------------------------------------------------------------
    _f("curl_path", str, "/usr/bin/curl",
       "curl binary used by the executor (was hardcoded /usr/bin/curl in be_execute.py:115)."),
    _f("timeout_s", int, 30,
       "Per-request timeout for the executor, in seconds (was hardcoded 30 in be_execute.py:112)."),
    _f("curl", str, "", "Path to a captured curl file supplying the request envelope (host, auth, headers)."),
    _f("env", str, "", "Environment name recorded on the run and on every execution property."),
    _f("build", str, "", "Build identifier recorded on the run."),
    _f("route_map", str, "",
       "Path to a JSON {contract_path: deployed_path} map. Empty = no rewriting (test_agent defaulted to "
       "config/route-map/<feature>.json inside its own repo, which a run dir has no business reading)."),
    _f("attach_responses", bool, False,
       "Attach each case's full JSON response to its execution (`response-<extid>.json`), beyond the "
       "900-character `actual` property. OFF by default: Kiwi attachments are readable by every account, "
       "so only turn it on where responses carry no personal data or secrets."),
    _f("cohort_path", str, "",
       "Dotted response path recorded as the `cohort_observed` execution property. Empty = do not record "
       "it. In test_agent this was hardcoded to `data.mix_panel_data.labs_careplan_variant` "
       "(be_execute.py:330) — one feature's experiment field baked into the generic executor."),
    # ---- UI execution ---------------------------------------------------------------------------------
    _f("base_url", str, "", "execute_web: the web app's origin on the target environment (--base-url)."),
    _f("ui_auth", str, "",
       "execute_web: a Playwright storage-state file holding a logged-in session (--ui-auth), recorded "
       "with `npx playwright codegen --save-storage=<file> <base url>`. Passed to Playwright, never read."),
    _f("cases", str, "",
       "Only these case ids (comma-separated) are derived and run by derive_spec and derive_ui — to run one "
       "case end to end. Empty = every CONFIRMED case of the kind."),
    _f("ui_only_new", bool, False,
       "derive_ui: keep the tests already written and derive only the CONFIRMED web cases that have neither a "
       "test nor a skip; nothing new = no model call. What /test-agent:execute uses by default."),
    _f("environments", str, "",
       "The environments a run can target, comma-separated (e.g. dione,earth,jupiter,…). The command offers "
       "them as choices; the chosen one is --env and, with base_url_template, gives the web base URL."),
    _f("base_url_template", str, "",
       "execute_web: the web base URL for an environment, with {env} (e.g. https://{env}.1mg.com). Used when "
       "--base-url is not given."),
    _f("ui_headed", bool, False,
       "execute_web: run the browser visibly, one test at a time, so a person can watch the suite. Off for "
       "unattended runs."),
    _f("ui_login", str, "",
       "execute_web: a JSON file {\"phone\", \"otp\"} for a test account whose number and OTP stay fixed "
       "on the environment (--ui-login). derive_ui writes a login step that runs before every test run, so "
       "no session expires; the values reach the Playwright process's environment and nothing else."),
    _f("ui_device", str, "Pixel 7",
       "execute_web: the Playwright device to emulate. Default a phone, because the web cases are mweb; "
       "empty = a desktop browser."),
    _f("ui_timeout_s", int, 60, "execute_web: per-test timeout, in seconds."),
    _f("ui_source_roots", str, "",
       "derive_ui: comma-separated source trees to take locators from. Empty = every repo snapshot of the "
       "RFC run (<rfc run>/_snap/*)."),
    # ---- agent-driven UI execution ---------------------------------------------------------------------
    _f("execute_ui_agent", bool, False,
       "Run the execute_ui_agent stage: an agent drives a real browser through each CONFIRMED web UI case "
       "and returns a verdict with evidence — no Playwright script is derived. OFF by default: it acts on a "
       "live test environment as a real user, and costs a model session per case on every run."),
    _f("agent_max_actions", int, 40,
       "execute_ui_agent: browser actions one case may take before it stops and marks what is left "
       "`not reached`."),
    _f("agent_case_timeout_s", int, 480, "execute_ui_agent: wall time for one case, in seconds."),
    _f("agent_parallel", int, 5,
       "execute_ui_agent: cases run in batches of this many, one agent and one browser per case."),
    _f("agent_confirm_fail", bool, True,
       "execute_ui_agent: re-run a FAIL once in a fresh browser. FAIL twice = FAIL; otherwise the case is "
       "reported as flaky with both reasons."),
    _f("agent_hosts", str, "",
       "execute_ui_agent: hosts the agent may drive, comma-separated (e.g. prestag.1mg.com). The stage acts "
       "on a live site, so it refuses a base address that is neither listed here nor the "
       "base_url_template of an environment named in `environments`. Never list production."),
    _f("agent_include_manual", bool, False,
       "execute_ui_agent: also hand the agent CONFIRMED web cases tagged exec:manual. The agent needs no "
       "source code, so a case a script could not be written for may still be one a browser can verify."),
    _f("agent_code_context", bool, False,
       "execute_ui_agent: give the agent the fleetlens MCP server (read-only code lookup), to find where a "
       "feature lives or which route opens it when it is stuck. Never to decide what the expected result is."),
    _f("agent_browser", str, "",
       "execute_ui_agent: the browser or channel Playwright MCP drives (chrome, msedge, firefox, webkit). "
       "Empty = its bundled Chromium, which must be installed for the pinned @playwright/mcp version."),
    _f("kiwi_plan_id", str, "",
       "The Kiwi test plan to read cases from and record into, by id (--kiwi-plan). Empty = the plan named "
       "by plan_template under the feature's product. Set to run a plan this workflow did not create."),
    _f("kiwi_direct", bool, False,
       "Run a Kiwi plan this workflow did not upload: a CONFIRMED case with no extid: tag is identified as "
       "KIWI-<case id>, and a case with no exec: tag is treated as a UI case. Set by `execute --kiwi-plan`."),
    _f("maestro_report", str, "", "publish_app: the JUnit report of a Maestro run (--maestro-report)."),
    _f("maestro_flows", str, "",
       "publish_app: the flows folder the run used, <druid>/.maestro/flows (--maestro-flows). Its headers' "
       "`# Covers:` blocks and step labels say which case each result belongs to."),
    _f("maestro_map", str, "", "publish_app: JSON {flow: [case ids]} overriding the derived mapping."),
    _f("maestro_blocked", str, "",
       "publish_app: curated exclusions, `TC-…  reason` per line; those cases are recorded BLOCKED."),
    _f("maestro_only_folder", str, "",
       "publish_app: publish only testcases whose flow sits under .maestro/flows/<this>/ (a whole-suite "
       "report spans suites that belong to different features)."),
    _f("app_exec_tag", str, "exec:ui",
       "publish_app: the tag a case needs to be recorded. Empty = any CONFIRMED case a flow covers "
       "(cases test_agent imported may carry no exec: tag)."),
    # ---- Kiwi connection ------------------------------------------------------------------------------
    _f("kiwi_url", str, "", "Kiwi XML-RPC endpoint. Empty = fall back to ~/.tcms.conf (tcms-api's default).",
       env="TCMS_API_URL"),
    # secret=True: never serialised into flags.json, `flags --json` or `--explain` (harness.flags).
    # Marked by the kernel builder as part of review BLOCKER 1.1 — a credential in every run dir is not
    # something to leave to builder sequencing. Ownership of this file otherwise unchanged.
    _f("kiwi_user", str, "",
       "Kiwi username, used with kiwi_url + kiwi_password. Empty = ~/.tcms.conf, and when the connection "
       "falls back to that file its username is the agent's account (README 'Kiwi account').",
       env="TCMS_USERNAME"),
    _f("kiwi_password", str, "",
       "Kiwi password. Empty = ~/.tcms.conf. There is deliberately NO default: test_agent shipped the "
       "admin password as a literal in two modules and wrote it into ~/.tcms.conf from the Makefile.",
       env="TCMS_PASSWORD", secret=True),
    _f("kiwi_web", str, "https://localhost:8443",
       "Kiwi web base URL, used only to build the printed plan/run links.", env="KIWI_WEB"),
)
