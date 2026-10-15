"""The `testing` workflow: PRD/RFC -> QA suite -> evaluated, stored, executed.

test_agent (plugin `ai-sdlc` 1.1.0) re-expressed on the harness contracts. The chain it replaces was one
bash script that ran rfc_agent as a subprocess and then a single headless `claude -p` turn which was
trusted to build a spreadsheet at the right path and call a storage tool, with nothing checking either
(survey §10.1). Here each step is a gated stage over files in the run dir, so the whole chain is
resumable, costed, traced and mechanically checked.

Default pipeline:

    sources -> generate_cases -> render_xlsx -> evaluate -> store_cases -> derive_spec

`execute_be` is registered but not in the default pipeline: it issues real requests against a live service
with a captured identity, so it is opt-in (`--enable testing.execute`, or `--stages execute_be`).

See README.md in this package for the stage table, the flag table, and the parity map back to test_agent.
"""

from __future__ import annotations

import json
from pathlib import Path

from harness.contracts import Workflow

from . import flags as _flags
from .stages import DECISIONS
from .stages.derive_spec import STAGE as DERIVE_SPEC
from .stages.evaluate import STAGE as EVALUATE
from .stages.execute_be import STAGE as EXECUTE_BE
from .stages.generate_cases import STAGE as GENERATE_CASES
from .stages.render_xlsx import STAGE as RENDER_XLSX
from .stages.sources import STAGE as SOURCES
from .stages.store_cases import STAGE as STORE_CASES

DEFAULT_PIPELINE: tuple[str, ...] = (
    "sources", "generate_cases", "render_xlsx", "evaluate", "store_cases", "derive_spec",
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
    """
    def on(name: str, default: bool) -> bool:
        try:
            return bool(flags.get(f"testing.{name}"))
        except (AttributeError, KeyError):
            return default

    stages = ["sources", "generate_cases", "render_xlsx"]
    if on("evaluate", True):
        stages.append("evaluate")
    if on("store", True):
        stages.append("store_cases")
        stages.append("derive_spec")
        if on("execute", False):
            stages.append("execute_be")
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
    g.add_argument("--from-cases", dest="from_cases",
                   help="Skip generation: load an existing cases.json or .xlsx as the suite. The xlsx "
                        "reader keeps the Execution column (test_agent's importer dropped it, so every "
                        "imported case was unrunnable).")


def resolve_inputs(args) -> dict:
    """argparse Namespace -> RunContext.inputs. Only this workflow's vocabulary."""
    keys = ("feature", "rfc_run_dir", "prd", "rfc", "contract", "plan", "figma", "test_data",
            "curl", "env", "build", "route_map", "from_cases")
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


WORKFLOW = Workflow(
    name="testing",
    stages=(SOURCES, GENERATE_CASES, RENDER_XLSX, EVALUATE, STORE_CASES, DERIVE_SPEC, EXECUTE_BE),
    default_pipeline=DEFAULT_PIPELINE,
    description="QA suite generation, evaluation, storage and backend execution (ported from test_agent).",
    add_inputs=add_inputs,
    resolve_inputs=resolve_inputs,
    flags=_flags.NAMES,
    decisions=DECISIONS,
    input_roots=("qa/sources.json",),
)
