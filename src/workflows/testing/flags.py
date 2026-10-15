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
    _f("exec_tag", str, "exec:http",
       "The tag a case must carry to be machine-executable (was EXEC_TAG in be_execute.py:25)."),
    # ---- pipeline shape ------------------------------------------------------------------------------
    _f("evaluate", bool, True,
       "Run the evaluator stage (presence checks in Python, rubric checked by the model); ON by default."),
    _f("store", bool, True, "Run the store_cases stage (upsert the suite into the case store)."),
    _f("execute", bool, False,
       "Run the execute_be stage. OFF by default because it issues real requests against a live "
       "service and records results; opt in with --enable testing.execute."),
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
    _f("cohort_path", str, "",
       "Dotted response path recorded as the `cohort_observed` execution property. Empty = do not record "
       "it. In test_agent this was hardcoded to `data.mix_panel_data.labs_careplan_variant` "
       "(be_execute.py:330) — one feature's experiment field baked into the generic executor."),
    # ---- Kiwi connection ------------------------------------------------------------------------------
    _f("kiwi_url", str, "", "Kiwi XML-RPC endpoint. Empty = fall back to ~/.tcms.conf (tcms-api's default).",
       env="TCMS_API_URL"),
    # secret=True: never serialised into flags.json, `flags --json` or `--explain` (harness.flags).
    # Marked by the kernel builder as part of review BLOCKER 1.1 — a credential in every run dir is not
    # something to leave to builder sequencing. Ownership of this file otherwise unchanged.
    _f("kiwi_user", str, "", "Kiwi username. Empty = ~/.tcms.conf.", env="TCMS_USERNAME"),
    _f("kiwi_password", str, "",
       "Kiwi password. Empty = ~/.tcms.conf. There is deliberately NO default: test_agent shipped the "
       "admin password as a literal in two modules and wrote it into ~/.tcms.conf from the Makefile.",
       env="TCMS_PASSWORD", secret=True),
    _f("kiwi_web", str, "https://localhost:8443",
       "Kiwi web base URL, used only to build the printed plan/run links.", env="KIWI_WEB"),
)
