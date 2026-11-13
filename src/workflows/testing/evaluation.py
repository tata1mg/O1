"""`evaluation` — score an existing testing run's suite on demand: the `evaluate` stage, outside the pipeline.

`evaluate` is off in the default pipeline (`testing.evaluate`): the rubric and the 14 compliance checks took
~17 of a run's ~45 minutes and nothing downstream reads them, so they no longer stand between generation and
the Kiwi review. This runs that stage alone on a run dir, whenever the score is wanted — before review, or
after the suite was edited:

    python -m workflows.testing.evaluation [--run-dir R] [--print]

The run dir defaults to the newest `<RUNS_ROOT>/*-testing-*`; feature and RFC run come from its
`qa/sources.json`. It scores `qa/cases.json` — the suite as generated (and as hand-edited in the run dir), not
edits a reviewer made in Kiwi. `--print` shows the command without running it. Exit code: the harness's.
"""

from __future__ import annotations

import argparse
import shlex
import subprocess
import sys
from pathlib import Path

if __package__ in (None, ""):  # run as a script: make `workflows.testing` importable from src/
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from workflows.testing.execute import PROJECT, _load, latest_run  # noqa: E402
from workflows.testing.preflight import load_config  # noqa: E402


def command(run_dir: Path, engine: str = "") -> list[str]:
    """The harness command line, or SystemExit naming what is missing."""
    sources = _load(run_dir / "qa" / "sources.json")
    feature, rfc_run = sources.get("feature"), sources.get("rfc_run_dir")
    missing = [n for n, ok in (("feature (qa/sources.json)", feature),
                               ("qa/cases.json — the suite has not been generated yet",
                                (run_dir / "qa" / "cases.json").is_file())) if not ok]
    if missing:
        raise SystemExit(f"evaluate: missing {', '.join(missing)}")
    cmd = ["uv", "run", "--project", str(PROJECT), "harness", "run", "testing", "--feature", feature,
           "--run-dir", str(run_dir), "--no-interactive", "--stages", "evaluate", "--force",
           "--enable", "testing.evaluate"]
    if rfc_run:
        cmd += ["--rfc-run-dir", rfc_run]
    if engine:  # `/test evaluate` inside Axon passes axon
        cmd += ["--engine", engine]
    return cmd


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="evaluation")
    ap.add_argument("--run-dir")
    ap.add_argument("--print", action="store_true", dest="print_only")
    ap.add_argument("--engine", default="", help="engine for the evaluator (default: the harness's)")
    args = ap.parse_args(argv)
    run_dir = Path(args.run_dir).expanduser() if args.run_dir else latest_run(load_config())
    if run_dir is None or not run_dir.is_dir():
        raise SystemExit("evaluate: no testing run found — pass --run-dir")
    cmd = command(run_dir, args.engine)
    print(" ".join(shlex.quote(c) for c in cmd), flush=True)
    if args.print_only:
        return 0
    log = run_dir / "logs" / "run.evaluate.out"
    log.parent.mkdir(exist_ok=True)
    with log.open("w") as out:
        code = subprocess.run(cmd, stdout=out, stderr=subprocess.STDOUT).returncode
    print(f"evaluate: exit {code} (log: {log})", flush=True)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
