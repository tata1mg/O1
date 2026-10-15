"""Revise — fix the review findings. Design: docs/REVISE.md.

    python -m rfc_agent.revise --run-dir <run dir after Review> [--fresh]

Per round (up to REVISE.ROUNDS, default 2): the warm lead fixes / disagrees / opens a question per finding and logs
it in review/fixes.md → Draft's gate re-runs → the reviewers that had findings, resumed, confirm against the fix
log and the diff. Still open after the last round → "⚠ Unresolved review findings" section in out/rfc.md.
"""

from __future__ import annotations

import argparse
import difflib
import json
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from .. import observe
from ..config import get_config
from ..draft.cli import settings as draft_settings
from ..investigate import agent as ag
from ..investigate.cli import _mmss, _read
from ..investigate.progress import Progress
from ..legacy_names import config_section, migrate_run_dir
from ..review.cli import settings as review_settings
from . import fixlog

PROMPTS = Path(__file__).with_name("prompts")
DEFAULTS = {"ROUNDS": 2, "LEAD_TIMEOUT_S": 1200, "CONFIRM_TIMEOUT_S": 600, "MINORS": "apply_if_cheap"}
DIFF_CAP = 60_000


def settings(cfg) -> dict:
    user = {k: v for k, v in config_section(cfg, "REVISE").items() if not str(k).startswith("_")}
    return {**DEFAULTS, **user}


def _prompt(name: str, **slots: str) -> str:
    text = (PROMPTS / name).read_text(encoding="utf-8")
    for k, v in slots.items():
        text = text.replace("{{" + k + "}}", v)
    return text


@observe.entry("revise")
def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m rfc_agent.revise", description="Revise: fix the review findings.")
    p.add_argument("--run-dir", required=True, help="the run folder, after Review")
    p.add_argument("--fresh", action="store_true", help="start over (keeps review/review.md)")
    p.add_argument("--no-readable", action="store_true", help="don't write the readable copy (Summarize) afterwards")
    args = p.parse_args(argv)
    run_dir = Path(args.run_dir).expanduser().resolve()
    migrate_run_dir(run_dir)
    review_md_path = run_dir / "review" / "review.md"
    rfc_path = run_dir / "out" / "rfc.md"
    if not review_md_path.exists() or not rfc_path.exists():
        print(f"revise: {review_md_path} is missing — run Review first", file=sys.stderr)
        return 1
    cfg = get_config()
    s = settings(cfg)
    s2 = draft_settings(cfg)
    s3 = review_settings(cfg, None)
    prog = Progress(run_dir, stage="revise")
    t0 = time.monotonic()
    state_path = run_dir / "work" / "revise.state.json"
    state = {} if args.fresh else (_read(state_path) or {})
    lock = threading.Lock()
    runs: list[dict] = []

    def save() -> None:
        with lock:
            tmp = state_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
            tmp.replace(state_path)

    review_md = review_md_path.read_text(encoding="utf-8")
    all_f = fixlog.findings(review_md)
    triage = (_read(run_dir / "review" / "triage.json") or {}).get("findings") or {}
    status: dict[str, dict] = state.get("status") or {}
    if not status:
        for fid, f in all_f.items():
            if triage:
                act = (triage.get(fid) or {}).get("action", "ignore")
                status[fid] = {
                    "status": "open" if act == "fix" else "ignored",
                    "severity": f["severity"],
                    "note": (triage.get(fid) or {}).get("note", ""),
                }
            elif f["severity"] in ("BLOCKER", "MAJOR") or s["MINORS"] == "fix":
                status[fid] = {"status": "open", "severity": f["severity"]}
            else:
                status[fid] = {"status": "minor_noted", "severity": f["severity"]}
        for rid, title in fixlog.not_reviewed(review_md):
            status[rid] = {"status": "not_reviewed", "severity": "—", "claim": f"{title}: not independently reviewed"}
        state["status"] = status
        save()

    open_ids = [fid for fid, v in status.items() if v["status"] == "open"]
    if not open_ids:
        prog.milestone("skip", "Revise: nothing to fix (clean review, or everything ignored at triage)")
    else:
        sev = {x: sum(1 for i in open_ids if status[i]["severity"] == x) for x in ("BLOCKER", "MAJOR", "MINOR")}
        prog.milestone(
            "start",
            f"Revise: fixing {len(open_ids)} finding(s) ({sev['BLOCKER']} blocker, "
            f"{sev['MAJOR']} major{', ' + str(sev['MINOR']) + ' minor' if sev['MINOR'] else ''})",
        )

    reviewer_sessions = {
        rid: next((r.get("session_id") for r in reversed(v.get("runs") or []) if r.get("session_id")), None)
        for rid, v in ((_read(run_dir / "work" / "review.state.json") or {}).get("reviewers") or {}).items()
    }
    lead_session_file = run_dir / "logs" / "lead.session"

    def run_agent(spec: ag.AgentSpec, prompt: str, session: str | None, step: str, fallback_intro: str):
        r = ag.run(spec, prompt, run_dir, cfg, resume=session, on_event=prog.tick, stage="revise", step=step)
        if not r.ok and session:
            r = ag.run(
                spec, fallback_intro + "\n\n" + prompt, run_dir, cfg, on_event=prog.tick, stage="revise", step=step
            )
        with lock:
            runs.append(
                {
                    "agent": spec.label,
                    "step": step,
                    "ok": r.ok,
                    "seconds": r.seconds,
                    "cost_usd": round(r.cost_usd, 4),
                    "tool_calls": r.tool_calls,
                    "error": r.error,
                }
            )
        return r

    for rnd in range(1, int(s["ROUNDS"]) + 1):
        open_ids = [fid for fid, v in status.items() if v["status"] == "open"]
        if not open_ids:
            break
        rs = state.setdefault("rounds", {}).setdefault(str(rnd), {})
        prog.step(f"4.{rnd}")
        # ---- 4a: lead fixes ----
        if rs.get("fixed") != "done":
            rs["rfc_before"] = rfc_path.read_text(encoding="utf-8")
            notes = [f"- {i}: the human noted: {status[i]['note']}" for i in open_ids if status[i].get("note")]
            ignored = [i for i, v in status.items() if v["status"] == "ignored"]
            triage_txt = ("\n".join(notes) + "\n" if notes else "") + (
                f"Ignored by the human at triage (don't fix): {', '.join(ignored)}\n" if ignored else ""
            )
            minors = [i for i, v in status.items() if v["status"] == "minor_noted"]
            minor_txt = (
                f"MINOR findings {', '.join(minors)}: apply if cheap; no entry needed."
                if minors and s["MINORS"] == "apply_if_cheap" and rnd == 1
                else ""
            )
            items = []
            for i in open_ids:
                f = all_f.get(i) or status[i]
                items.append(
                    f"- {i} · {f.get('severity')} · {f.get('claim')}"
                    + (f"\n  {f['body'][:1200]}" if f.get("body") and i not in all_f else "")
                )
            if rnd > 1:
                items.append(
                    "(round 2: these were NOT RESOLVED or newly raised — the reviewers' reasons are in "
                    "review/*.confirm.md; re-argue a disagreement once at most)"
                )
            spec = ag.AgentSpec(
                "lead",
                s2["LEAD_MODEL"],
                s2["LEAD_EFFORT"],
                ("Read", "Grep", "Glob", "Write", "Edit"),
                ("out/rfc.md", "out/coverage.json", "review/fixes.md"),
                timeout_s=int(s["LEAD_TIMEOUT_S"]),
                check_files=("out/rfc.md",),
            )
            prog.agent("lead", state="running", step=f"fix round {rnd}")
            session = lead_session_file.read_text().strip() if lead_session_file.exists() else None
            r = run_agent(
                spec,
                _prompt("fix.md", TRIAGE=triage_txt, ROUND=str(rnd), MINORS=minor_txt, FINDINGS="\n".join(items)),
                session,
                f"fix{rnd}",
                "First read out/rfc.md and review/review.md.",
            )
            if r.session_id:
                lead_session_file.write_text(r.session_id)
            prog.agent("lead", state="done" if r.ok else "failed", seconds=r.seconds, cost_usd=round(r.cost_usd, 3))
            rs["fixed"] = "done"
            save()
        fixes_md = (
            (run_dir / "review" / "fixes.md").read_text(encoding="utf-8")
            if (run_dir / "review" / "fixes.md").exists()
            else ""
        )
        lead = fixlog.lead_entries(fixes_md, rnd)
        counts = {
            k: sum(1 for i in open_ids if (lead.get(i) or {}).get("status") == k) for k in ("FIXED", "DISAGREE", "OPEN")
        }
        missing = [i for i in open_ids if i not in lead]
        # ---- 4b: gate ----
        if rs.get("gated") != "done":
            from ..draft.cli import main as draft_main

            draft_main(["--run-dir", str(run_dir), "--check-only"])
            rs["gated"] = "done"
            save()
        gate = (_read(run_dir / "out" / "check.json") or {}).get("status", "?")
        before = rs.get("rfc_before", "")
        after = rfc_path.read_text(encoding="utf-8")
        diff = "".join(
            difflib.unified_diff(
                before.splitlines(True), after.splitlines(True), "rfc.md (before)", "rfc.md (after)", n=2
            )
        )
        (run_dir / "review" / f"rfc.round{rnd}.diff").write_text(diff, encoding="utf-8")
        reviewers = sorted({i.rsplit(".", 1)[0] for i in open_ids if i in lead})
        prog.milestone(
            "fixed",
            f"Fix log (round {rnd}): {counts['FIXED']} fixed, {counts['DISAGREE']} disagree, "
            f"{counts['OPEN']} open question"
            + (f", {len(missing)} without an entry" if missing else "")
            + f" · gate {gate} · confirming with {', '.join(reviewers) or 'nobody'}",
        )
        # ---- 4c: reviewers confirm ----
        suffix = "" if rnd == 1 else str(rnd)
        new_found: list[dict] = []

        def confirm(rid: str, rnd=rnd, suffix=suffix, rs=rs, open_ids=open_ids, lead=lead, diff=diff) -> None:
            out = f"review/{rid}.confirm{suffix}.md"
            if not (rs.get("confirmed") or {}).get(rid) == "done" or not (run_dir / out).exists():
                mine = [i for i in open_ids if i.startswith(rid + ".") and i in lead]
                items = []
                for i in mine:
                    f = all_f.get(i) or status[i]
                    items.append(
                        f"- {i} · {f.get('severity')} · {f.get('claim')}\n  lead: {lead[i]['status']} — "
                        f"{lead[i]['text'][:1500]}"
                    )
                spec = ag.AgentSpec(
                    rid,
                    s3["MODEL"],
                    s3["EFFORT"],
                    ("Read", "Grep", "Glob", "Write", "Edit"),
                    (out,),
                    timeout_s=int(s["CONFIRM_TIMEOUT_S"]),
                    check_files=(out,),
                )
                prog.agent(rid, state="running", step=f"confirm round {rnd}")
                for attempt in (1, 2):
                    r = run_agent(
                        spec,
                        _prompt(
                            "confirm.md",
                            ROUND=str(rnd),
                            OUT_FILE=out,
                            REVIEWER=rid,
                            ITEMS="\n".join(items),
                            DIFF=diff[:DIFF_CAP] or "(no changes)",
                        ),
                        reviewer_sessions.get(rid) if attempt == 1 else None,
                        f"confirm{rnd}",
                        f"You reviewed this RFC earlier — your review is in review/{rid}.md.",
                    )
                    if r.session_id:
                        reviewer_sessions[rid] = r.session_id
                    if (run_dir / out).exists():
                        break
                prog.agent(rid, state="done" if (run_dir / out).exists() else "failed")
                with lock:
                    rs.setdefault("confirmed", {})[rid] = "done"
                save()

        with ThreadPoolExecutor(max_workers=max(1, len(reviewers))) as pool:
            list(pool.map(confirm, reviewers))
        verdict_counts = {"RESOLVED": 0, "NOT RESOLVED": 0, "ACCEPTED": 0}
        for rid in reviewers:
            path = run_dir / "review" / f"{rid}.confirm{suffix}.md"
            text = path.read_text(encoding="utf-8") if path.exists() else ""
            verdicts, new = fixlog.confirmations(text, rid, rnd)
            for i in [x for x in open_ids if x.startswith(rid + ".")]:
                v = verdicts.get(i)
                if v:
                    verdict_counts[v["verdict"]] += 1
                settled = fixlog.settle(lead.get(i), v)
                status[i].update(
                    round=rnd,
                    lead=(lead.get(i) or {}).get("status"),
                    lead_text=(lead.get(i) or {}).get("text", "")[:600],
                    verdict=(v or {}).get("verdict") or ("confirmation failed" if not text else "no verdict"),
                    verdict_text=(v or {}).get("text", "")[:600],
                )
                if settled:
                    status[i]["status"] = settled
            for nb in new:
                all_f[nb["id"]] = nb
                status[nb["id"]] = {
                    "status": "open",
                    "severity": "BLOCKER",
                    "claim": nb["claim"],
                    "body": nb["body"],
                    "raised_in_round": rnd,
                }
                new_found.append(nb)
        for i in missing:
            status[i].update(round=rnd, lead=None, verdict="no fix-log entry")
        state["status"] = status
        save()
        still = [i for i, v in status.items() if v["status"] == "open"]
        nxt = " → round 2" if still and rnd < int(s["ROUNDS"]) else ""
        prog.milestone(
            "confirmed",
            f"Round {rnd}: {verdict_counts['RESOLVED']} resolved, {verdict_counts['ACCEPTED']} "
            f"accepted, {verdict_counts['NOT RESOLVED']} not resolved"
            + (f", {len(new_found)} new blocker(s)" if new_found else "")
            + nxt,
        )

    # ---- 4d: unresolved section ----
    unresolved = []
    for i, v in status.items():
        if v["status"] == "open":
            v["status"] = "unresolved"
        if v["status"] in ("unresolved", "not_reviewed"):
            f = all_f.get(i) or v
            unresolved.append(
                {
                    "id": i,
                    "severity": f.get("severity", "—"),
                    "claim": f.get("claim", v.get("claim", "")),
                    "reviewer_position": v.get("verdict_text") or (f.get("body") or "")[:300],
                    "lead_position": v.get("lead_text") or (v.get("verdict") or ""),
                }
            )
    rfc = rfc_path.read_text(encoding="utf-8")
    rfc_path.write_text(
        fixlog.insert_unresolved(rfc, fixlog.unresolved_section(unresolved) if unresolved else None), encoding="utf-8"
    )
    state["status"] = status
    state["done"] = True
    save()
    (run_dir / "review" / "status.json").write_text(json.dumps(status, indent=2), encoding="utf-8")
    secs = round(time.monotonic() - t0, 1)
    cost = sum(x["cost_usd"] for x in runs)
    prev = _read(run_dir / "logs" / "revise.json") or {}
    tally: dict[str, int] = {}
    for v in status.values():
        tally[v["status"]] = tally.get(v["status"], 0) + 1
    (run_dir / "logs" / "revise.json").write_text(
        json.dumps(
            {
                "settings": s,
                "seconds_this_invocation": secs,
                "cost_usd": round(cost, 3),
                "statuses": tally,
                "runs": (prev.get("runs") or []) + runs,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    n_unres = len(unresolved)
    prog.milestone(
        "done",
        (
            "Revise done: all resolved"
            if not unresolved
            else f"Revise done: {n_unres} unresolved (published in the ⚠ section)"
        )
        + " · "
        + ", ".join(f"{k} {v}" for k, v in sorted(tally.items()))
        + f" · {_mmss(secs)}, ${cost:.2f} → {run_dir / 'review' / 'status.json'}",
    )
    prog.step("done")
    if args.no_readable:
        return 0
    from ..summarize.cli import main as summarize  # the concise human-readable copy of the final RFC

    return summarize(["--run-dir", str(run_dir)])


if __name__ == "__main__":
    sys.exit(main())
