"""Suite 3: cold-start cost — `uv run rfc --help` wall time, and the bare import time of the three
kernel-ish modules a stage/gate touches (`rfc_agent.runner`, `rfc_agent.engine`, `rfc_agent.gates`).

Also times `uv run harness --help` and `import harness` when the `harness` console script and package are
present. Guarded so the suite degrades to 'not available' rather than failing.
"""

from __future__ import annotations

import subprocess
import sys

from bench.timing import Sample, timed


def _run_cmd(argv: list[str]) -> float:
    dt, _ = timed(subprocess.run, argv, capture_output=True, text=True, timeout=60)
    return dt


def _import_time(module_names: list[str]) -> float:
    code = "import " + ", ".join(module_names)
    dt, proc = timed(subprocess.run, [sys.executable, "-c", code], capture_output=True, text=True, timeout=60)
    if proc.returncode != 0:
        raise RuntimeError(f"import failed: {proc.stderr[-2000:]}")
    return dt


def run(repeat: int = 10) -> dict:
    results = []
    notes = []

    rfc_help = Sample(name="cli_startup.rfc_help", unit="s")
    for _ in range(repeat):
        rfc_help.values.append(_run_cmd(["uv", "run", "rfc", "--help"]))
    results.append(rfc_help.to_result())

    rfc_import = Sample(name="cli_startup.rfc_agent_import", unit="s")
    for _ in range(repeat):
        rfc_import.values.append(_import_time(["rfc_agent.runner", "rfc_agent.engine", "rfc_agent.gates"]))
    results.append(rfc_import.to_result())

    # AFTER-only: harness --help / import harness
    try:
        import harness.cli  # noqa: F401  # not present pre-kernel-CLI; ImportError expected today

        harness_help = Sample(name="cli_startup.harness_help", unit="s")
        for _ in range(repeat):
            harness_help.values.append(_run_cmd(["uv", "run", "harness", "--help"]))
        results.append(harness_help.to_result())

        harness_import = Sample(name="cli_startup.harness_import", unit="s")
        for _ in range(repeat):
            harness_import.values.append(_import_time(["harness"]))
        results.append(harness_import.to_result())
    except ImportError:
        notes.append("harness.cli not available — skipped")

    return {"results": results, "notes": notes}
