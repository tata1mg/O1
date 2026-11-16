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

A run is started with --prd and always gets its own new folder, RUNS_ROOT/<name>-<timestamp>, even for a PRD that
was run before. It is continued only with --run-dir <folder> and no --prd: the same folder, the options it started
with (logs/run_args.json), and the stages already done are skipped. Every attempt is one line in logs/attempts.jsonl,
and run.json says how many attempts (and resumes) the run has taken. Every stage resumes on its own, so a resume
continues where the run stopped. Scope is skipped when the run already has its inputs (scope.json + raw/prd.md),
unless --redo-scope or --from scope.
Tracing: Langfuse tracing is required; without keys the run stops before it starts (exit 2). `--langfuse-verbose
true` adds one span per tool call, for that call only. A resume inherits --yes and --confirm-file with the other
options the run started with.
Exit code: 0 done · 1 a stage stopped (the summary names it; resume with --run-dir) · 2 no Langfuse keys.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import signal
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

from . import feedback, observe, run_facts
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


def _engines(ran: list[dict]) -> dict:
    """Which engine each stage that ran in this attempt used (run.json records it, so a run says what ran it).
    Stages skipped by --from or a resume, or never reached after a stop, are left out. Summarize runs inside
    Revise, so it is listed whenever Revise ran."""
    from .axon_agent import engine_for
    from .config import get_config

    cfg = get_config()
    stages = [r["stage"] for r in ran]
    if "revise" in stages:
        stages.append("summarize")
    return {stage: engine_for(stage, cfg) for stage in stages}


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
    g.add_argument(
        "--prd", help="Confluence URL, page id, or a local markdown file: starts a NEW run in its own folder"
    )
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
    g.add_argument("--run-dir", help="continue the run in this folder (use without --prd)")
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
    from .axon_agent import add_engine_args

    add_engine_args(p.add_argument_group("engine (claude-code | axon | pi)"))
    g = p.add_argument_group("observability (Langfuse)")
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


# the options a run starts with; a resume restores any the new call leaves out
RUN_ARGS = ("prd", "jira", "repos", "repos_root", "no_figma", "reuse_figma", "fresh_clone", "preset",
            "req_granularity", "ask", "ask_review", "yes", "confirm_file")  # fmt: skip


def _stop_on_sigterm() -> None:
    """A plain stop (kill, or Claude Code stopping its background task) ends the run the way Ctrl-C does: the agents are
    stopped and the attempt is recorded, so the run can be continued with --run-dir."""

    def handler(signum, frame):
        raise KeyboardInterrupt

    try:
        signal.signal(signal.SIGTERM, handler)
    except ValueError:  # not the main thread (a test): nothing to install
        pass


def _has_run(run_dir: Path) -> bool:
    """A folder that holds a run: Scope wrote its inputs, or a first attempt at least recorded its options."""
    return (run_dir / "scope.json").exists() or (run_dir / "logs" / "run_args.json").exists()


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
    from .axon_agent import apply_engine_args

    apply_engine_args(args.engine, args.stage_engine)
    _stop_on_sigterm()
    if args.run_dir:
        run_dir = Path(args.run_dir).expanduser().resolve()
        if not args.prd and not _has_run(run_dir):
            print(f"rfc: nothing to resume in {run_dir} — start a run with --prd", file=sys.stderr)
            return 1
        if args.prd and _has_run(run_dir):
            print(
                f"rfc: {run_dir} already holds a run. Continue it with --run-dir alone (add --redo-scope to run Scope "
                "again on its saved PRD); for a new run of this PRD pass --prd without --run-dir and it gets its own "
                "folder.",
                file=sys.stderr,
            )
            return 1
    elif args.prd:
        from .config import get_config
        from .scope.cli import _run_dir

        run_dir = _run_dir(argparse.Namespace(prd=args.prd, jira=args.jira, run_dir=None, out=None), get_config())
        run_dir = run_dir.expanduser().resolve()
    else:
        print("rfc: pass --prd (a new run) or --run-dir (continue a run)", file=sys.stderr)
        return 1
    if run_dir.is_dir():
        migrate_run_dir(run_dir)
    existed = _has_run(run_dir)
    if existed:  # a resume: the options the run started with, unless this call gives its own
        saved = _read(run_dir / "logs" / "run_args.json") or {}
        for key in RUN_ARGS:
            if not getattr(args, key, None) and saved.get(key):
                setattr(args, key, saved[key])
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
    if not existed:  # remember how the run started, so a resume needs only the folder
        saved = {k: getattr(args, k) for k in RUN_ARGS if getattr(args, k, None)}
        (run_dir / "logs" / "run_args.json").write_text(json.dumps(saved, indent=2), encoding="utf-8")
    print(f"  [rfc] {'resuming' if existed else 'new run'}: {run_dir}", file=sys.stderr, flush=True)
    mains = _mains()
    ran: list[dict] = []
    code = 0
    for stage in STAGES[start:]:
        print(f"  [rfc] ── {LABELS[stage]} ──", file=sys.stderr, flush=True)
        t0 = time.monotonic()
        try:
            code = mains[stage](stage_args(args, stage, run_dir)) or 0
        except KeyboardInterrupt:  # Ctrl-C or a stop: end the agents, then record the attempt like any other stop
            from .investigate.agent import kill_live_agents

            n = kill_live_agents()
            print(f"  [rfc] interrupted{f' — stopped {n} running agent(s)' if n else ''}", file=sys.stderr)
            code = 130
        except SystemExit as e:  # a stage stopping with SystemExit("message") or exit code
            code = e.code if isinstance(e.code, int) else 1
            if not isinstance(e.code, int) and e.code:
                print(f"  [rfc] {e.code}", file=sys.stderr)
        ran.append({"stage": stage, "exit": code, "seconds": round(time.monotonic() - t0, 1)})
        if code != 0:
            print(
                f"  [rfc] STOPPED at {LABELS[stage]} (exit {code}). Fix the cause, then resume this run with "
                f"--run-dir {run_dir} — finished work is kept.",
                file=sys.stderr,
            )
            break
    summary = _summary(run_dir, ran)
    summary["engines"] = _engines(ran)
    summary["status"] = "done" if code == 0 else f"stopped at {ran[-1]['stage']}"
    prev = _read(run_dir / "logs" / "run.json") or {}  # read before it is overwritten below
    with (run_dir / "logs" / "attempts.jsonl").open("a", encoding="utf-8") as f:
        f.write(
            json.dumps(
                {
                    "ts": datetime.now(UTC).isoformat(timespec="seconds"),
                    "kind": "resume" if existed else "new",
                    "from": STAGES[start],
                    "exit": code,
                    "status": summary["status"],
                }
            )
            + "\n"
        )
    attempts = len((run_dir / "logs" / "attempts.jsonl").read_text(encoding="utf-8").splitlines())
    summary["attempts"], summary["resumes"] = attempts, attempts - 1  # one run, however many tries it took
    summary["seconds_all_attempts"] = round(
        (prev.get("seconds_all_attempts") or prev.get("seconds_total") or 0) + summary["seconds_total"], 1
    )
    (run_dir / "logs" / "run.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    with contextlib.suppress(Exception):  # the same text goes on the trace's Output pane (observe._close_root)
        text = feedback.run_summary(run_dir, run_facts.collect(run_dir))
        (run_dir / "logs" / "run_summary.md").write_text(text + "\n", encoding="utf-8")
        print("\n" + "\n".join(f"  [rfc] {x}" for x in text.splitlines()), file=sys.stderr)
    if code == 0:
        status = _read(run_dir / "review" / "status.json") or {}
        unresolved = [k for k, v in status.items() if v.get("status") in ("unresolved", "not_reviewed")]
        mins = summary["seconds_all_attempts"] / 60
        print(
            f"\n  [rfc] done in {mins:.0f} min, ${summary['cost_usd_total']:.2f}"
            + (f" · {summary['resumes']} resume(s)" if summary["resumes"] else "")
            + (f" · ⚠ {len(unresolved)} unresolved review finding(s) — see the RFC's ⚠ section" if unresolved else "")
            + f"\n  [rfc] read:  {summary['outputs']['readable']}\n  [rfc] full:  {summary['outputs']['full']}",
            file=sys.stderr,
        )
    return code


if __name__ == "__main__":
    sys.exit(main())
