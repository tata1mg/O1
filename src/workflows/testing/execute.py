"""`execute` — run the CONFIRMED cases of an existing testing run, from the run dir alone.

The full `harness run testing …` line repeats what the run dir already knows (feature, RFC run, environment,
build) plus the flags that compose the executor's stages. This builds that line from the run:

    python -m workflows.testing.execute [web|backend|all] [--run-dir R] [--env E] [--build B] [--headed]
                                        [--only-existing] [--cases TC-…,TC-…] [--curl FILE] [--print]
    python -m workflows.testing.execute --pending [--run-dir R]      what has a web test, what does not

- `web` -> `derive_ui,execute_web` with `testing.ui_only_new`: tests are written only for CONFIRMED web
           cases that have none yet (no model call when there are none), then every test runs.
           `--only-existing` -> `execute_web` alone: run the tests already written, now, no model.
- `backend` -> `derive_spec,execute_be`: derive_spec is Python and seconds, and re-reading the CONFIRMED
               cases picks up any confirmed since the last run.
- `all` -> both, one after the other.

Defaults: the run dir is the newest `<RUNS_ROOT>/*-testing-*`; feature and RFC run come from its
`qa/sources.json`; environment and build from `--env`/`--build`, else `testing.env`/`testing.build` in config
(`preflight setup-web --env/--build`), else the last results (`qa/ui/results.json`, `qa/results.json`); the
curl from `testing.curl` in config. `--print` shows the command instead of running it.
Exit code: the harness's (the last one, for `all`).
"""

from __future__ import annotations

import argparse
import json
import shlex
import subprocess
import sys
from pathlib import Path

if __package__ in (None, ""):  # run as a script: make `workflows.testing` importable from src/
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from workflows.testing.preflight import load_config, resolved_flags  # noqa: E402

PROJECT = Path(__file__).resolve().parents[3]
KINDS = {"web": (("derive_ui", "execute_web"), "testing.execute_web", "qa/ui/results.json"),
         "backend": (("derive_spec", "execute_be"), "testing.execute", "qa/results.json")}


def _load(path: Path) -> dict:
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def latest_run(cfg) -> Path | None:
    root = Path((cfg.get("RUNS_ROOT") if cfg else None) or "~/rfc-runs").expanduser()
    runs = [d for d in root.glob("*-testing-*") if (d / "qa" / "sources.json").is_file()]
    return max(runs, key=lambda d: (d / "manifest.json").stat().st_mtime if (d / "manifest.json").exists()
               else 0, default=None)


def command(kind: str, run_dir: Path, *, env: str = "", build: str = "", headed: bool = False,
            cases: str = "", curl: str = "", flags=None, only_existing: bool = False) -> list[str]:
    """The harness command line for one kind, or SystemExit naming what is missing."""
    stages, enable, results = KINDS[kind]
    if kind == "web" and only_existing:
        stages = ("execute_web",)
    sources = _load(run_dir / "qa" / "sources.json")
    # The last run of this kind, else of the other kind: both name the same `<feature> · <env> · <build>` run.
    lasts = [_load(run_dir / results)] + [_load(run_dir / r) for _, _, r in KINDS.values() if r != results]
    feature, rfc_run = sources.get("feature"), sources.get("rfc_run_dir")
    # --env > the default you set (`TESTING.env` in config.json, via `preflight setup-web --env`) > the last run's
    env = env or str((flags.get("testing.env") if flags else "") or "") or \
        next((str(x["environment"]) for x in lasts if x.get("environment")), "")
    build = build or str((flags.get("testing.build") if flags else "") or "") or \
        next((str(x["build"]) for x in lasts if x.get("build")), "")
    missing = [n for n, v in (("feature (qa/sources.json)", feature), ("--env", env), ("--build", build)) if not v]
    if kind == "web" and only_existing and not (run_dir / "qa" / "ui" / "plan.json").is_file():
        missing.append("qa/ui/plan.json — no test is written yet; run without --only-existing to write them")
    if kind == "backend":
        curl = curl or str((flags.get("testing.curl") if flags else "") or "")
        if not curl:
            missing.append("--curl (or testing.curl in config)")
    if missing:
        raise SystemExit(f"execute {kind}: missing {', '.join(missing)}")
    cmd = ["uv", "run", "--project", str(PROJECT), "harness", "run", "testing", "--feature", feature,
           "--run-dir", str(run_dir), "--env", env, "--build", build, "--no-interactive",
           "--stages", ",".join(stages), "--force", "--enable", enable]
    if rfc_run:
        cmd += ["--rfc-run-dir", rfc_run]
    if kind == "web" and headed:
        cmd += ["--enable", "testing.ui_headed"]
    if kind == "web" and not only_existing:
        cmd += ["--enable", "testing.ui_only_new"]
    if kind == "backend":
        cmd += ["--curl", str(Path(curl).expanduser())]
    if cases:
        cmd += ["--set", f"testing.cases={cases}"]
    return cmd


def pending(run_dir: Path) -> dict:
    """{tests, skipped, new, error}: web cases with a test, skipped ones, and CONFIRMED ones with neither —
    the choice between running now (`--only-existing`) and writing the new tests first."""
    import socket

    from workflows.testing.preflight import KIWI_TIMEOUT_S, store_for

    plan = _load(run_dir / "qa" / "ui" / "plan.json")
    tests = [r["extid"] for r in plan.get("tests") or [] if isinstance(r, dict) and r.get("extid")]
    skipped = [r["extid"] for r in plan.get("skipped") or [] if isinstance(r, dict) and r.get("extid")]
    out = {"tests": tests, "skipped": skipped, "new": [], "error": ""}
    feature = _load(run_dir / "qa" / "sources.json").get("feature")
    previous = socket.getdefaulttimeout()
    socket.setdefaulttimeout(KIWI_TIMEOUT_S)
    try:
        confirmed = [c.extid for c in store_for(resolved_flags(load_config()), run_dir).list_cases(
            feature, status="CONFIRMED", tags=("exec:ui", "surface:web"))]
    except Exception as exc:  # report, never raise: the command turns this into a message
        out["error"] = f"{type(exc).__name__}: {exc}"
        return out
    finally:
        socket.setdefaulttimeout(previous)
    out["new"] = [e for e in confirmed if e not in tests and e not in skipped]
    out["not_confirmed_any_more"] = [e for e in tests if e not in confirmed]
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="execute")
    ap.add_argument("kind", nargs="?", default="web", choices=("web", "backend", "all"))
    ap.add_argument("--run-dir")
    ap.add_argument("--env", default="")
    ap.add_argument("--build", default="")
    ap.add_argument("--headed", action="store_true")
    ap.add_argument("--cases", default="", help="comma-separated case ids (backend: narrows derive_spec)")
    ap.add_argument("--curl", default="")
    ap.add_argument("--print", action="store_true", dest="print_only")
    ap.add_argument("--only-existing", action="store_true",
                    help="web: run only the tests already written — no derive_ui, no model, starts now")
    ap.add_argument("--pending", action="store_true",
                    help="print (JSON) which CONFIRMED web cases have a test and which do not, then exit")
    args = ap.parse_args(argv)
    cfg = load_config()
    run_dir = Path(args.run_dir).expanduser() if args.run_dir else latest_run(cfg)
    if run_dir is None or not run_dir.is_dir():
        raise SystemExit("execute: no testing run found — pass --run-dir")
    if args.pending:
        print(json.dumps({"run_dir": str(run_dir), **pending(run_dir)}, indent=2))
        return 0
    flags = resolved_flags(cfg)
    code = 0
    for kind in (("web", "backend") if args.kind == "all" else (args.kind,)):
        cmd = command(kind, run_dir, env=args.env, build=args.build, headed=args.headed, cases=args.cases,
                      curl=args.curl, flags=flags, only_existing=args.only_existing)
        print(" ".join(shlex.quote(c) for c in cmd), flush=True)
        if not args.print_only:
            log = run_dir / "logs" / f"run.{kind}.out"  # one log per launch, as /test-agent:test writes them
            log.parent.mkdir(exist_ok=True)
            with log.open("w") as out:
                code = subprocess.run(cmd, stdout=out, stderr=subprocess.STDOUT).returncode
            print(f"{kind}: exit {code} (log: {log})", flush=True)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
