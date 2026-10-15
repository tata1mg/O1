"""The whole pipeline in one command: Scope → Summarize.

    uv run rfc --prd <confluence url | page id | file> [options]
    python -m rfc_agent.run --prd <…> [options]
    python -m rfc_agent.run --run-dir <existing run> --from review   # resume / redo from a stage

  scope        inputs + your one selection                    (python -m rfc_agent.scope)
  investigate  plan · trace in parallel · integrate           (python -m rfc_agent.investigate)
  draft        write the RFC + gate                           (python -m rfc_agent.draft)
  review       parallel reviewers                             (python -m rfc_agent.review)
  revise       fix + confirm (≤ 2 rounds), then summarize     (python -m rfc_agent.revise → summarize)
  summarize    the short human-readable copy                  (python -m rfc_agent.summarize)

Every stage resumes on its own, so rerunning the same command continues where it stopped. Scope is skipped
when the run already has its inputs (scope.json + raw/prd.md), unless --redo-scope or --from scope.
Tracing: Langfuse is on by default (config LANGFUSE.ENABLED); without keys the run stops before it starts —
add `--langfuse false` to run untraced. `--langfuse-verbose true` adds one span per tool call.
Exit code: 0 done · 1 a stage stopped (the summary names it; rerun to resume) · 2 tracing on but no Langfuse keys.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from . import observe
from .legacy_names import OLD_FROM, migrate_run_dir

STAGES = ("scope", "investigate", "draft", "review", "revise")
LABELS = {
    "scope": "Scope — inputs + selection",
    "investigate": "Investigate — plan, trace, integrate",
    "draft": "Draft — write + gate",
    "review": "Review",
    "revise": "Revise — fix, then Summarize",
}


def _mains():
    """Each stage's entry point (resolved at call time so tests can substitute them)."""
    from .draft.cli import main as draft
    from .investigate.cli import main as investigate
    from .review.cli import main as review
    from .revise.cli import main as revise
    from .scope.cli import main as scope

    return {"scope": scope, "investigate": investigate, "draft": draft, "review": review, "revise": revise}


def _read(p: Path):
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _stage_name(value: str) -> str:
    """A stage name for --from. The old numbers (0-4) still work."""
    name = OLD_FROM.get(value, value)
    if name not in STAGES:
        raise argparse.ArgumentTypeError(f"unknown stage {value!r} — use one of {', '.join(STAGES)}")
    return name


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="rfc", description="Run the whole RFC pipeline (Scope → Summarize).")
    g = p.add_argument_group("inputs (Scope)")
    g.add_argument("--prd", help="Confluence URL, page id, or a local markdown file (needed unless --run-dir resumes)")
    g.add_argument("--jira", help="Jira key or url")
    g.add_argument("--repos", help="repos you already know are in scope, comma list")
    g.add_argument("--repos-root", help="local checkouts root (default: config REPOS_ROOT)")
    g.add_argument("--no-figma", action="store_true", help="ignore Figma links")
    g.add_argument("--reuse-figma", choices=("ask", "always", "never"))
    g.add_argument("--fresh-clone", action="store_true", help="never touch your checkouts (repo cache only)")
    g = p.add_argument_group("stage options")
    g.add_argument("--preset", choices=("balanced", "fast"), help="Investigate preset (default from config: balanced)")
    g.add_argument("--req-granularity", choices=("grouped", "fine"), help="Investigate requirements (default: grouped)")
    g.add_argument("--ask", action="store_true", help="Investigate Pause 2 also asks the open questions")
    g.add_argument("--ask-review", action="store_true", help="Review: triage the review findings before fixing")
    g = p.add_argument_group("run control")
    g.add_argument("--run-dir", help="run folder (default: RUNS_ROOT/<jira key or prd-id>)")
    g.add_argument(
        "--from",
        dest="from_stage",
        type=_stage_name,
        metavar="{" + ",".join(STAGES) + "}",
        help="start at this stage; earlier stages must already be done",
    )
    g.add_argument("--redo-scope", action="store_true", help="run Scope again even if the inputs exist")
    g.add_argument("--redo-prep", dest="redo_scope", action="store_true", help=argparse.SUPPRESS)  # old name
    g.add_argument("--yes", action="store_true", help="accept every pause's default (no questions)")
    g = p.add_argument_group("observability (Langfuse)")
    g.add_argument(observe.FLAG, metavar="true|false", help="trace this run (default: config LANGFUSE.ENABLED, true)")
    g.add_argument(
        observe.VERBOSE_FLAG,
        metavar="true|false",
        help="also one span per tool call (default: config LANGFUSE.VERBOSE, false)",
    )
    g.add_argument(
        "--confirm-file",
        action="store_true",
        help="pauses through files (what /rfc-agent:rfc uses; automatic without a terminal)",
    )
    return p


def stage_args(args, stage: str, run_dir: Path) -> list[str]:
    a = ["--run-dir", str(run_dir)]
    if stage == "scope":
        a += ["--prd", args.prd]
        for flag, val in (
            ("--jira", args.jira),
            ("--repos", args.repos),
            ("--repos-root", args.repos_root),
            ("--reuse-figma", args.reuse_figma),
        ):
            if val:
                a += [flag, val]
        a += [
            f
            for f, on in (
                ("--no-figma", args.no_figma),
                ("--fresh-clone", args.fresh_clone),
                ("--yes", args.yes),
                ("--confirm-file", args.confirm_file),
            )
            if on
        ]
    elif stage == "investigate":
        for flag, val in (("--preset", args.preset), ("--req-granularity", args.req_granularity)):
            if val:
                a += [flag, val]
        a += [f for f, on in (("--ask", args.ask), ("--yes", args.yes), ("--confirm-file", args.confirm_file)) if on]
    elif stage == "review":
        a += [
            f
            for f, on in (("--ask-review", args.ask_review), ("--yes", args.yes), ("--confirm-file", args.confirm_file))
            if on
        ]
    return a


def _summary(run_dir: Path, ran: list[dict]) -> dict:
    scope = _read(run_dir / "scope.json") or {}
    costs = {"scope": scope.get("connector_cost_usd", 0)}
    for name in ("investigate", "draft", "review", "revise", "summarize"):
        costs[name] = (_read(run_dir / "logs" / f"{name}.json") or {}).get("cost_usd", 0)
    out = run_dir / "out"
    return {
        "stages": ran,
        "cost_usd_by_stage": costs,
        "cost_usd_total": round(sum(v or 0 for v in costs.values()), 2),
        "seconds_total": round(sum(r["seconds"] for r in ran), 1),
        "outputs": {
            "readable": str(out / "rfc.readable.md"),
            "full": str(out / "rfc.md"),
            "review": str(run_dir / "review" / "review.md"),
            "status": str(run_dir / "review" / "status.json"),
        },
    }


@observe.entry(None)
def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.run_dir:
        run_dir = Path(args.run_dir).expanduser().resolve()
    elif args.prd:
        from .config import get_config
        from .scope.cli import _run_dir

        run_dir = _run_dir(argparse.Namespace(prd=args.prd, jira=args.jira, run_dir=None, out=None), get_config())
        run_dir = run_dir.expanduser().resolve()
    else:
        print("rfc: pass --prd (new run) or --run-dir (existing run)", file=sys.stderr)
        return 1
    if run_dir.is_dir():
        migrate_run_dir(run_dir)
    has_inputs = (run_dir / "scope.json").exists() and (run_dir / "raw" / "prd.md").exists()
    start = STAGES.index(args.from_stage) if args.from_stage is not None else 0
    if start == 0 and has_inputs and not args.redo_scope:
        start = 1
    if start == 0 and not args.prd:
        print("rfc: Scope needs --prd", file=sys.stderr)
        return 1
    if start > 0 and not has_inputs:
        print(f"rfc: {run_dir} has no Scope output — run with --prd first", file=sys.stderr)
        return 1
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "logs").mkdir(exist_ok=True)
    print(f"  [rfc] run dir: {run_dir}", file=sys.stderr, flush=True)
    mains = _mains()
    ran: list[dict] = []
    code = 0
    for stage in STAGES[start:]:
        print(f"  [rfc] ── {LABELS[stage]} ──", file=sys.stderr, flush=True)
        t0 = time.monotonic()
        try:
            code = mains[stage](stage_args(args, stage, run_dir)) or 0
        except SystemExit as e:  # a stage stopping with SystemExit("message") or exit code
            code = e.code if isinstance(e.code, int) else 1
            if not isinstance(e.code, int) and e.code:
                print(f"  [rfc] {e.code}", file=sys.stderr)
        ran.append({"stage": stage, "exit": code, "seconds": round(time.monotonic() - t0, 1)})
        if code != 0:
            print(
                f"  [rfc] STOPPED at {LABELS[stage]} (exit {code}). Fix the cause and rerun the same command — "
                "finished work is kept.",
                file=sys.stderr,
            )
            break
    summary = _summary(run_dir, ran)
    summary["status"] = "done" if code == 0 else f"stopped at {ran[-1]['stage']}"
    (run_dir / "logs" / "run.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    if code == 0:
        status = _read(run_dir / "review" / "status.json") or {}
        unresolved = [k for k, v in status.items() if v.get("status") in ("unresolved", "not_reviewed")]
        mins = summary["seconds_total"] / 60
        print(
            f"\n  [rfc] done in {mins:.0f} min, ${summary['cost_usd_total']:.2f}"
            + (f" · ⚠ {len(unresolved)} unresolved review finding(s) — see the RFC's ⚠ section" if unresolved else "")
            + f"\n  [rfc] read:  {summary['outputs']['readable']}\n  [rfc] full:  {summary['outputs']['full']}",
            file=sys.stderr,
        )
    return code


if __name__ == "__main__":
    sys.exit(main())
