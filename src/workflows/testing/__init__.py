"""The `testing` workflow: PRD/RFC -> QA suite -> stored, executed (and evaluated, on demand).

test_agent (plugin `ai-sdlc` 1.1.0) re-expressed on the harness contracts. The chain it replaces was one
bash script that ran rfc_agent as a subprocess and then a single headless `claude -p` turn which was
trusted to build a spreadsheet at the right path and call a storage tool, with nothing checking either
(survey §10.1). Here each step is a gated stage over files in the run dir, so the whole chain is
resumable, costed, traced and mechanically checked.

Default pipeline (generate and upload for review):

    sources -> generate_cases -> render_xlsx -> store_cases -> prerequisites

`evaluate` (the rubric and the 14 compliance checks) is opt-in and separate: `--stages evaluate --enable
testing.evaluate --run-dir <run>`, or `/test-agent:evaluate`. It took ~17 of a run's ~45 minutes and nothing
downstream reads it, so it no longer stands between generation and review.

Cases are stored PROPOSED, and only a human approves them (CONFIRMED). `derive_spec` reads CONFIRMED cases
only, so it belongs to the RUN pipeline, after review, together with `execute_be`, which issues real
requests against a live service with a captured identity. Both are opt-in: `--enable testing.execute`, or
`--stages derive_spec,execute_be --run-dir <run>`. The UI executors ride the same way:
`testing.execute_web` adds `derive_ui -> execute_web` (Playwright, web cases) and `testing.publish_app` adds
`publish_app` (a Maestro run's report, app cases).

See README.md in this package for the stage table, the flag table, and the parity map back to test_agent.
"""

from __future__ import annotations

import json
from pathlib import Path

from harness.contracts import Workflow

from . import flags as _flags
from .scores import run_scores
from .stages import DECISIONS
from .stages.convert_flows import STAGE as CONVERT_FLOWS
from .stages.derive_spec import STAGE as DERIVE_SPEC
from .stages.derive_ui import STAGE as DERIVE_UI
from .stages.evaluate import STAGE as EVALUATE
from .stages.execute_app import STAGE as EXECUTE_APP
from .stages.execute_be import STAGE as EXECUTE_BE
from .stages.execute_ui_agent import STAGE as EXECUTE_UI_AGENT
from .stages.execute_web import STAGE as EXECUTE_WEB
from .stages.generate_cases import STAGE as GENERATE_CASES
from .stages.prerequisites import STAGE as PREREQUISITES
from .stages.publish_app import STAGE as PUBLISH_APP
from .stages.render_xlsx import STAGE as RENDER_XLSX
from .stages.sources import STAGE as SOURCES
from .stages.store_cases import STAGE as STORE_CASES

DEFAULT_PIPELINE: tuple[str, ...] = (
    "sources", "generate_cases", "render_xlsx", "store_cases", "prerequisites",
)


def compose_pipeline(flags) -> tuple[str, ...]:
    """The pipeline the `testing.*` composition flags ask for.

    `testing.evaluate`, `testing.store` and `testing.execute` are the workflow's own names for turning
    three steps on and off, because that is how test_agent's operators thought about them
    (`--evaluate/--no-evaluate`, `--upsert`, `--execute` in the survey's implied-flags list). They compose
    a pipeline; the harness-level equivalent is `--disable-stage <name>` / `--stages <names>`, and either
    route ends at the same list.

    Dropping `store_cases` also drops `derive_spec` and `execute_be`: both read from the store, and a
    pipeline that derived a spec from a store nothing had written to would fail on an empty spec rather
    than telling the operator what they actually did.

    `derive_spec` rides with `execute_be` rather than with `store_cases`: it reads only CONFIRMED cases,
    and a freshly stored suite is PROPOSED until someone reviews it, so deriving straight after the upload
    could only ever fail.

    `prerequisites` reads only the suite, so it runs with or without the store; it comes after the upload
    so its gate never blocks it, and before `derive_spec` because the run needs what it lists.

    The UI executors follow the same rule as `execute_be`: off by default, and only with the store, since
    each reads CONFIRMED cases from it. `testing.execute_web` adds `derive_ui -> execute_web`,
    `testing.publish_app` adds `publish_app`, and `testing.execute_ui_agent` adds `execute_ui_agent` — an
    agent driving a browser through each web case, with no script derived first.

    The harness calls this (`Workflow.compose_pipeline`) for every run that does not pass --pipeline.
    """
    def on(name: str, default: bool) -> bool:
        try:
            return bool(flags.get(f"testing.{name}"))
        except (AttributeError, KeyError):
            return default

    stages = ["sources", "generate_cases", "render_xlsx"]
    if on("evaluate", False):
        stages.append("evaluate")
    store = on("store", True)
    if store:
        stages.append("store_cases")
    if on("prerequisites", True):
        stages.append("prerequisites")
    if store and on("execute", False):
        stages += ["derive_spec", "execute_be"]
    if store and on("execute_web", False):
        stages += ["derive_ui", "execute_web"]
    if store and on("execute_ui_agent", False):
        stages.append("execute_ui_agent")
    if store and on("publish_app", False):
        stages.append("publish_app")
    if store and on("execute_app", False):
        stages.append("execute_app")
    if store and on("convert_flows", False):
        stages.append("convert_flows")
    return tuple(stages)


def add_inputs(parser) -> None:
    """Workflow-specific CLI inputs. Every rfc_agent/harness flag stays where it is (test_agent's
    run_chain.sh re-declared all of them by hand, which is how they drifted)."""
    g = parser.add_argument_group("testing workflow inputs")
    g.add_argument("--feature", required=True,
                   help="Feature key: the store's product key, the run-dir name, the xlsx filename stem. "
                        "NOT the ID prefix inside case ids — the generator picks that mnemonic itself.")
    g.add_argument("--rfc-run-dir",
                   help="A completed RFC run dir. The sources stage takes raw/prd.md, out/rfc.md, "
                        "out/contract.md, out/detail/plan.md and raw/figma.md from it — the NORMALIZED "
                        "local copies, never the original URLs (a URL made the generator stop mid-run to "
                        "ask for MCP permissions; measured 2026-08-11).")
    g.add_argument("--prd", help="PRD file, overriding --rfc-run-dir's raw/prd.md.")
    g.add_argument("--rfc", help="RFC file, overriding --rfc-run-dir's out/rfc.md.")
    g.add_argument("--contract", help="API contract file. The only source with exact routes.")
    g.add_argument("--plan", help="Implementation plan file (its CHANGE blocks carry `verify:` lines).")
    g.add_argument("--figma", help="Figma export/markdown file.")
    g.add_argument("--test-data", dest="test_data",
                   help="Real test data the team supplied. Absent = every Test Data cell must be flagged "
                        "as invented.")
    g.add_argument("--curl", help="execute_be: file holding a captured curl — the request envelope.")
    g.add_argument("--env", help="execute_be: environment name recorded on the run.")
    g.add_argument("--build", help="execute_be: build identifier recorded on the run.")
    g.add_argument("--route-map", dest="route_map",
                   help="execute_be: JSON {contract_path: deployed_path}. Keys starting '_' are notes.")
    g.add_argument("--base-url", dest="base_url",
                   help="execute_web: the web app's origin on the target environment.")
    g.add_argument("--ui-auth", dest="ui_auth",
                   help="execute_web: Playwright storage-state file with a logged-in session.")
    g.add_argument("--ui-login", dest="ui_login",
                   help='execute_web: JSON file {"phone", "otp"} for a test account the run logs in as.')
    g.add_argument("--maestro-report", dest="maestro_report",
                   help="publish_app: JUnit report from `maestro test --format junit`.")
    g.add_argument("--maestro-flows", dest="maestro_flows",
                   help="publish_app: the flows folder the Maestro run used (<druid>/.maestro/flows).")
    g.add_argument("--maestro-map", dest="maestro_map",
                   help="publish_app: JSON {flow: [case ids]} overriding the derived mapping.")
    g.add_argument("--maestro-blocked", dest="maestro_blocked",
                   help="publish_app: curated exclusions, `TC-…  reason` per line.")
    g.add_argument("--from-cases", dest="from_cases",
                   help="Skip generation: load an existing cases.json or .xlsx as the suite. The xlsx "
                        "reader keeps the Execution column (test_agent's importer dropped it, so every "
                        "imported case was unrunnable).")


def resolve_inputs(args) -> dict:
    """argparse Namespace -> RunContext.inputs. Only this workflow's vocabulary."""
    keys = ("feature", "rfc_run_dir", "prd", "rfc", "contract", "plan", "figma", "test_data",
            "curl", "env", "build", "route_map", "from_cases", "base_url", "ui_auth", "ui_login", "maestro_report",
            "maestro_flows", "maestro_map", "maestro_blocked")
    return {k: getattr(args, k, None) for k in keys}


def load_from_cases(path: str | Path) -> tuple[list[dict], list[dict]]:
    """`--from-cases` -> (cases, scenarios). Accepts the canonical JSON or a delivered workbook.

    A .json file may be either the cases array alone or an object with `cases`/`scenarios` keys; a
    workbook is read with the stdlib reader in `xlsx` (no openpyxl needed) INCLUDING the Execution
    column, which is the survey §6.1 bug fix.
    """
    p = Path(str(path)).expanduser()
    if p.suffix.lower() in (".xlsx", ".xlsm"):
        from . import xlsx

        sheets = xlsx.read_sheets(p)
        return xlsx.parse_cases(sheets), xlsx.parse_scenarios(sheets)
    data = json.loads(p.read_text())
    if isinstance(data, list):
        return data, []
    return list(data.get("cases") or []), list(data.get("scenarios") or [])


def config_get(cfg):
    """The config layer for `testing.*`: `TESTING.<name>` in ~/.config/rfc-agent/config.json.

    WHY: without it the only layers were `--set` and a handful of env vars, so a resume that forgot the
    `--set testing.storage=kiwi` of the first invocation silently switched to the JSON store (2026-11-02,
    NR-DIGICHEM-v2: the prerequisites-only rerun rewrote flags.json with storage=json while the cases sat
    in Kiwi). A secret never comes from here: the Kiwi password stays in ~/.tcms.conf or the env.
    """
    section = cfg.get("TESTING") or {}

    def get(name: str):
        if not name.startswith("testing.") or name in _flags.SECRETS:
            return None
        return section.get(name.split(".", 1)[1])

    return get


WORKFLOW = Workflow(
    name="testing",
    stages=(SOURCES, GENERATE_CASES, RENDER_XLSX, EVALUATE, STORE_CASES, PREREQUISITES, DERIVE_SPEC,
            EXECUTE_BE, DERIVE_UI, EXECUTE_WEB, EXECUTE_UI_AGENT, PUBLISH_APP, EXECUTE_APP, CONVERT_FLOWS),
    default_pipeline=DEFAULT_PIPELINE,
    description="QA suite generation, evaluation, storage, and backend / web UI / app UI execution (ported "
                "from test_agent).",
    add_inputs=add_inputs,
    resolve_inputs=resolve_inputs,
    flags=_flags.NAMES,
    decisions=DECISIONS,
    input_roots=("qa/sources.json",),
    compose_pipeline=compose_pipeline,
    # The canary proves rfc-agent's byte_budget feedback reaches the model, and byte_budget only paces
    # rfc.md / contract.md / plan.md, which no testing stage writes. On by default it cost every run up to
    # 240s (a failure is never cached) to prove nothing about this workflow. `--enable hook_canary` still runs it.
    flag_defaults={"hook_canary": False},
    run_scores=run_scores,
)
