"""Review. Design: docs/REVIEW.md.

    python -m rfc_agent.review --run-dir <run dir after Draft> [--ask-review] [--yes] [--confirm-file] [--fresh]

One fresh reviewer per flow plus one cross-cutting reviewer, in parallel; each never sees the lead's reasoning.
Findings are merged into review/review.md. With --ask-review, you triage them before Revise.
"""

from __future__ import annotations

import argparse
import json
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from .. import observe
from ..config import get_config
from ..investigate import agent as ag
from ..investigate import pauses
from ..investigate.cli import _mmss, _read
from ..investigate.plan import load_json
from ..investigate.progress import Progress
from ..legacy_names import config_section, migrate_run_dir
from ..scope import confirm
from . import review

PROMPTS = Path(__file__).with_name("prompts")
DEFAULTS = {
    "MODEL": "claude-opus-5-5[1m]",
    "EFFORT": "high",
    "TIMEOUT_S": 1200,
    "CALL_BUDGET": 150,
    "ASK": False,
    "CROSS_CUTTING": True,
}
TOOLS = ("Read", "Grep", "Glob", "Write", "Edit")


def settings(cfg, ask: bool | None) -> dict:
    user = {k: v for k, v in config_section(cfg, "REVIEW").items() if not str(k).startswith("_")}
    s = {**DEFAULTS, **user}
    if ask is not None:
        s["ASK"] = ask
    return s


def _prompt(name: str, **slots: str) -> str:
    text = (PROMPTS / name).read_text(encoding="utf-8")
    for k, v in slots.items():
        text = text.replace("{{" + k + "}}", v)
    return text


def build_reviewers(run_dir: Path, flows: dict, scope: dict, cross: bool) -> list[dict]:
    rfc = (run_dir / "out" / "rfc.md").read_text(encoding="utf-8")
    repos = ", ".join(r["repo"] for r in scope.get("repos") or [] if r.get("path")) or "(none)"
    fmt = (PROMPTS / "finding_format.md").read_text(encoding="utf-8")
    ui_files = (
        ", ".join(
            f"facts/{p.name}" for p in sorted((run_dir / "facts").glob("UI*.md")) if not p.name.endswith(".check.json")
        )
        or "(no UI facts)"
    )
    untraced = {
        p.name.split(".")[0]
        for p in (run_dir / "facts").glob("*.check.json")
        if (_read(p) or {}).get("status") == "untraced"
    }
    out = []
    for f in flows.get("flows") or []:
        fid = f.get("id")
        tracers = [t.get("id") for t in flows.get("tracers") or [] if fid in (t.get("flows") or [])]
        section, changes = review.flow_parts(rfc, f)
        rid = f"R-{fid}"
        prompt = _prompt(
            "flow_reviewer.md",
            FLOW_ID=fid,
            FLOW_NAME=f.get("name", ""),
            SECTION=section,
            CHANGES=", ".join(changes) or "(none tagged with its REQs — find the ones that touch it)",
            REQS=", ".join(f.get("reqs") or []) or "(none)",
            TRACER_FILES=", ".join(f"facts/{t}.md" for t in tracers) or "(no tracer facts)",
            UI_FILES=ui_files,
            REPOS=repos,
            OUT_FILE=f"review/{rid}.md",
            UNTRACED=(
                "This flow was UNTRACED in Investigate (its tracer failed): trace the essentials yourself and say "
                "what you could not verify.\n"
                if set(tracers) & untraced
                else ""
            ),
            FINDING_FORMAT=fmt.replace("{{TITLE}}", f"{fid} {f.get('name', '')}"),
        )
        out.append({"id": rid, "title": f"{fid} {f.get('name', '')}", "prompt": prompt})
    if cross:
        out.append(
            {
                "id": "R-X",
                "title": "cross-cutting",
                "prompt": _prompt(
                    "cross_reviewer.md",
                    REPOS=repos,
                    OUT_FILE="review/R-X.md",
                    FINDING_FORMAT=fmt.replace("{{TITLE}}", "cross-cutting"),
                ),
            }
        )
    return out


@observe.entry("review")
def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m rfc_agent.review", description="Review: review the RFC.")
    p.add_argument("--run-dir", required=True, help="the run folder, after Draft")
    p.add_argument("--ask-review", action="store_true", default=None, help="triage findings before Revise")
    p.add_argument("--yes", action="store_true", help="with --ask-review: accept the default triage")
    p.add_argument("--confirm-file", action="store_true", help="pause through files (what /rfc-agent:review uses)")
    p.add_argument("--fresh", action="store_true", help="review again even if reviews exist")
    args = p.parse_args(argv)
    run_dir = Path(args.run_dir).expanduser().resolve()
    migrate_run_dir(run_dir)
    if not (run_dir / "out" / "rfc.md").exists():
        print(f"review: {run_dir / 'out' / 'rfc.md'} is missing — run Draft first", file=sys.stderr)
        return 1
    cfg = get_config()
    s = settings(cfg, args.ask_review)
    flows, _ = load_json(run_dir / "work" / "flows.json")
    scope = _read(run_dir / "scope.json") or {}
    repos = {r["repo"]: r for r in scope.get("repos") or []}
    prog = Progress(run_dir, stage="review")
    (run_dir / "review").mkdir(exist_ok=True)
    state_path = run_dir / "work" / "review.state.json"
    state = {} if args.fresh else (_read(state_path) or {})
    done: dict = state.setdefault("reviewers", {})
    lock = threading.Lock()
    t0 = time.monotonic()
    reviewers = build_reviewers(run_dir, flows or {}, scope, bool(s["CROSS_CUTTING"]))
    todo = [
        r
        for r in reviewers
        if done.get(r["id"], {}).get("status") != "reviewed" or not (run_dir / "review" / f"{r['id']}.md").exists()
    ]
    prog.step("3")
    prog.milestone(
        "start",
        f"Review: {len(reviewers)} reviewer(s) running ({', '.join(r['id'] for r in reviewers)})"
        + (f" — {len(reviewers) - len(todo)} already done" if len(todo) < len(reviewers) else ""),
    )

    def save() -> None:
        with lock:
            tmp = state_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
            tmp.replace(state_path)

    def run_one(r: dict) -> None:
        rid, out = r["id"], f"review/{r['id']}.md"
        spec = ag.AgentSpec(
            rid,
            s["MODEL"],
            s["EFFORT"],
            TOOLS,
            (out,),
            call_budget=int(s["CALL_BUDGET"]),
            timeout_s=int(s["TIMEOUT_S"]),
            check_files=(out,),
        )
        runs = []
        prog.agent(rid, state="running")
        for attempt in (1, 2):
            res = ag.run(
                spec,
                r["prompt"],
                run_dir,
                cfg,
                on_event=prog.tick,
                stage="review",
                step="run" if attempt == 1 else "retry",
            )
            runs.append(
                {
                    "ok": res.ok,
                    "seconds": res.seconds,
                    "cost_usd": round(res.cost_usd, 4),
                    "tool_calls": res.tool_calls,
                    "rate_limited": res.rate_limited,
                    "error": res.error,
                    "stream": Path(res.stream).name,
                    "session_id": res.session_id,
                }
            )
            if (run_dir / out).exists():
                break
            if attempt == 1:
                prog.milestone("retry", f"{rid}: wrote no review ({res.error or 'no file'}) — retrying once")
        ok = (run_dir / out).exists()
        cost = sum(x["cost_usd"] for x in runs)
        secs = sum(x["seconds"] for x in runs)
        with lock:
            done[r["id"]] = {"status": "reviewed" if ok else "not reviewed", "runs": runs}
        save()
        prog.agent(rid, state="done" if ok else "failed", seconds=secs, cost_usd=round(cost, 3))
        if ok:
            c = review.counts(review.parse((run_dir / out).read_text(encoding="utf-8")))
            prog.milestone(
                "reviewer_done",
                f"{rid} done ({_mmss(secs)}, ${cost:.2f}): {c['BLOCKER']} blocker, "
                f"{c['MAJOR']} major, {c['MINOR']} minor",
            )
        else:
            prog.milestone("not_reviewed", f"{rid} failed twice — its part is NOT reviewed")

    for r in todo:
        prog.agent(r["id"], state="queued")
    if todo:
        with ThreadPoolExecutor(max_workers=len(todo)) as pool:
            list(pool.map(run_one, todo))

    merged: dict[str, dict] = {}
    for r in reviewers:
        path = run_dir / "review" / f"{r['id']}.md"
        text = path.read_text(encoding="utf-8") if path.exists() else ""
        findings = review.parse(text)
        merged[r["id"]] = {
            "title": r["title"],
            "findings": findings,
            "status": done.get(r["id"], {}).get("status", "reviewed" if text else "not reviewed"),
            "flags": review.check(findings, repos, run_dir),
            "dismissed": review.dismissed(text) if r["id"] == "R-X" else "",
        }
    (run_dir / "review" / "review.md").write_text(review.merge(merged), encoding="utf-8")
    total = {sev: sum(review.counts(m["findings"])[sev] for m in merged.values()) for sev in review.SEVERITIES}

    triage = None
    if s["ASK"]:
        items, ids = [], {}
        for rid in sorted(merged, key=lambda x: (x != "R-X", x)):
            for i, f in enumerate(merged[rid]["findings"], 1):
                fid = f"{rid}.{i}"
                ids[fid] = f
                items.append(
                    confirm.Item(
                        "finding",
                        fid,
                        f"{fid} {f['severity']}: {f['claim'][:90]}",
                        include=f["severity"] in ("BLOCKER", "MAJOR"),
                    )
                )
        if items:
            pauses.HEADINGS.setdefault("finding", "Review findings — ticked ones go to the lead (Revise)")
            prog.milestone(
                "pause",
                f"Review: {total['BLOCKER']} blocker, {total['MAJOR']} major, {total['MINOR']} "
                "minor — waiting for your triage",
            )
            ans = pauses.ask(
                3,
                run_dir,
                "── Review · triage the review findings ──",
                items,
                file_mode=args.confirm_file,
                accept_defaults=args.yes,
            )
            notes = dict(ans.get("notes") or {})
            if ans["feedback"]:
                notes["_all"] = ans["feedback"]
            triage = {
                "findings": {
                    it.key: {
                        "action": "fix" if it.include else "ignore",
                        "severity": ids[it.key]["severity"],
                        **({"note": notes[it.key]} if it.key in notes else {}),
                    }
                    for it in ans["items"]
                    if it.kind == "finding"
                },
                "notes": {k: v for k, v in notes.items() if k == "_all"},
            }
            (run_dir / "review" / "triage.json").write_text(json.dumps(triage, indent=2), encoding="utf-8")

    secs = round(time.monotonic() - t0, 1)
    cost = sum(x["cost_usd"] for d in done.values() for x in d.get("runs", []))
    throttled = sum(x.get("rate_limited", 0) for d in done.values() for x in d.get("runs", []))
    (run_dir / "logs" / "review.json").write_text(
        json.dumps(
            {
                "settings": s,
                "seconds_this_invocation": secs,
                "cost_usd": round(cost, 3),
                "rate_limit_events": throttled,
                "counts": total,
                "reviewers": done,
                "triage": bool(triage),
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    not_rev = [rid for rid, m in merged.items() if m["status"] == "not reviewed"]
    fixing = (
        sum(1 for v in triage["findings"].values() if v["action"] == "fix")
        if triage
        else total["BLOCKER"] + total["MAJOR"]
    )
    prog.milestone(
        "done",
        f"Review: {total['BLOCKER']} blocker, {total['MAJOR']} major, {total['MINOR']} minor"
        + (f" · ⚠ not reviewed: {', '.join(not_rev)}" if not_rev else "")
        + f" · {fixing} to fix in Revise · {_mmss(secs)}, ${cost:.2f} → {run_dir / 'review' / 'review.md'}",
    )
    prog.step("done")
    return 0


if __name__ == "__main__":
    sys.exit(main())
