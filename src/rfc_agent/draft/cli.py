"""Draft — write the RFC, then gate it. Design: docs/DRAFT.md.

    python -m rfc_agent.draft --run-dir <run dir after Investigate> [--fresh]
    python -m rfc_agent.draft --run-dir <run dir> --check-only      # re-run the gate (e.g. after a hand edit)

The Investigate lead is resumed (warm) and writes out/rfc.md + out/coverage.json; citations are checked as it writes
(write-time hook). Python renders the §2 coverage table and References, then runs the gate (coverage, structure,
CHANGE blocks, secrets, Mermaid lint, citations as a final assertion): must-fix items go back to the lead once;
whatever is still wrong becomes a known issue for review (out/check.json) and the run continues.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from pathlib import Path

from .. import observe
from ..config import get_config
from ..investigate import agent as ag
from ..investigate.cli import _code_reads, _mmss, _read
from ..investigate.config import resolve as resolve_investigate
from ..investigate.plan import load_json
from ..investigate.progress import Progress
from ..legacy_names import config_section, migrate_run_dir
from . import coverage, gate

PROMPT = Path(__file__).with_name("prompts") / "write.md"
WRITES = ("out/rfc.md", "out/coverage.json")
DEFAULTS = {"TIMEOUT_S": 1200, "SIZE_TARGET_KB": "25–40", "CODE_READS": 20}


def settings(cfg) -> dict:
    s1 = resolve_investigate(cfg)
    user = {k: v for k, v in config_section(cfg, "DRAFT").items() if not str(k).startswith("_")}
    return {**DEFAULTS, "LEAD_MODEL": s1["LEAD_MODEL"], "LEAD_EFFORT": s1["LEAD_EFFORT"], **user}


@observe.entry("draft")
def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m rfc_agent.draft", description="Draft: write the RFC.")
    p.add_argument("--run-dir", required=True, help="the run folder, after Investigate")
    p.add_argument("--fresh", action="store_true", help="write again even if out/rfc.md exists")
    p.add_argument("--check-only", action="store_true", help="don't write — re-run the gate on out/rfc.md")
    from ..axon_agent import add_engine_args, apply_engine_args

    add_engine_args(p)
    args = p.parse_args(argv)
    apply_engine_args(args.engine, args.stage_engine)
    run_dir = Path(args.run_dir).expanduser().resolve()
    migrate_run_dir(run_dir)
    work = run_dir / "work"
    if not (work / "understanding.md").exists() or not (work / "reqs.json").exists():
        print(f"draft: {run_dir} has no work/understanding.md — run Investigate first", file=sys.stderr)
        return 1
    cfg = get_config()
    s = settings(cfg)
    prog = Progress(run_dir, stage="draft")
    state_path = work / "draft.state.json"
    state = {} if args.fresh else (_read(state_path) or {})
    t0 = time.monotonic()
    runs: list[dict] = []
    session = (
        (run_dir / "logs" / "lead.session").read_text().strip()
        if (run_dir / "logs" / "lead.session").exists()
        else (_read(work / "investigate.state.json") or {}).get("lead_session")
    )
    (run_dir / "out").mkdir(exist_ok=True)

    def lead(prompt: str, step: str) -> ag.AgentResult:
        nonlocal session
        spec = ag.AgentSpec(
            "lead",
            s["LEAD_MODEL"],
            s["LEAD_EFFORT"],
            ("Read", "Grep", "Glob", "Write", "Edit"),
            WRITES,
            timeout_s=int(s["TIMEOUT_S"]),
            check_files=("out/rfc.md",),  # citations checked at write time
        )
        prog.agent("lead", state="running", step=step)
        r = ag.run(spec, prompt, run_dir, cfg, resume=session, on_event=prog.tick, stage="draft", step=step)
        if not r.ok and session:  # not resumable → a fresh lead that reads the files first
            prog.milestone("fallback", "the lead's session could not be resumed — starting a fresh lead")
            r = ag.run(
                spec,
                "First read work/reqs.json, work/flows.json, facts/, work/understanding.md and "
                "work/open_questions.json.\n\n" + prompt,
                run_dir,
                cfg,
                on_event=prog.tick,
                stage="draft",
                step=step,
            )
        if r.session_id:
            session = r.session_id
            (run_dir / "logs" / "lead.session").write_text(r.session_id)
        runs.append(
            {
                "step": step,
                "ok": r.ok,
                "seconds": r.seconds,
                "cost_usd": round(r.cost_usd, 4),
                "tool_calls": r.tool_calls,
                "code_reads": _code_reads(Path(r.stream)),
                "error": r.error,
                "stream": Path(r.stream).name,
            }
        )
        prog.agent("lead", state="done" if r.ok else "failed", seconds=r.seconds, cost_usd=round(r.cost_usd, 3))
        return r

    rfc_path, cov_path = run_dir / "out" / "rfc.md", run_dir / "out" / "coverage.json"
    if args.check_only and not rfc_path.exists():
        print(f"draft: {rfc_path} is missing — run Draft without --check-only first", file=sys.stderr)
        return 1
    if not args.check_only and (state.get("written") != "done" or not rfc_path.exists()):
        prog.step("2")
        prog.milestone("start", "Draft: the lead is writing the RFC")
        answers = (
            ", work/answers.md — the human's answers, treat them as fact" if (work / "answers.md").exists() else ""
        )
        prompt = (
            PROMPT.read_text(encoding="utf-8")
            .replace("{{ANSWERS}}", answers)
            .replace("{{CODE_READS}}", str(s["CODE_READS"]))
            .replace("{{SIZE_TARGET_KB}}", str(s["SIZE_TARGET_KB"]))
        )
        r = lead(prompt, "write")
        if not r.ok and not rfc_path.exists():
            print(f"draft: the lead failed: {r.error}", file=sys.stderr)
            return 1
        missing = [w for w in WRITES if not (run_dir / w).exists()]
        if missing or not r.ok:
            prog.milestone("fix", f"{', '.join(missing) or 'the RFC'} not finished — asking the lead once")
            lead(
                "You did not finish: write "
                + (", ".join(missing) or "the rest of out/rfc.md")
                + " now, as instructed, then reply done.",
                "finish",
            )
            if not rfc_path.exists():
                print("draft: out/rfc.md was not written", file=sys.stderr)
                return 1
        state["written"] = "done"
        state_path.write_text(json.dumps(state, indent=2), encoding="utf-8")

    reqs, _ = load_json(work / "reqs.json")
    flows, _ = load_json(work / "flows.json")
    reqs, flows = reqs or {}, flows or {}
    scope = _read(run_dir / "scope.json") or {}

    def render() -> dict:
        cov, _ = load_json(cov_path)
        rfc = rfc_path.read_text(encoding="utf-8")
        rfc = coverage.insert_table(rfc, coverage.render_table(cov or {}, reqs))
        rfc = coverage.insert_references(rfc, coverage.references(scope))
        rfc_path.write_text(rfc, encoding="utf-8")
        return cov or {}

    def check() -> dict:
        cov, cerr = load_json(cov_path)
        return gate.run_all(
            run_dir,
            rfc_path.read_text(encoding="utf-8"),
            cov,
            cerr,
            reqs,
            flows,
            scope,
            str(s.get("SIZE_TARGET_KB", "25–40")),
        )

    def content_hash() -> str:
        h = hashlib.sha256()
        for rel in WRITES:
            f = run_dir / rel
            h.update(f.read_bytes() if f.exists() else b"")
        return h.hexdigest()[:16]

    # ---- the gate ------------------------------------------------------------------------------------
    render()
    res = check()
    observe.gate(
        "draft",
        res["must_fix"],
        res["flags"],
        kind="draft",
        round_label="check-only" if args.check_only else None,
        stage="draft",
    )
    before = res
    c = res["counts"]
    head = f"Gate: {c['citations']} citations, {c['changes']} CHANGEs, {c['diagrams']} diagrams checked"
    fixed = False
    if res["must_fix"] and state.get("gate_fixed_for") != content_hash():
        summary_line = ", ".join(f"{len(v)} {k.replace('_', ' ')}" for k, v in res["must_fix"].items())
        prog.milestone("gate", f"{head} · {summary_line} → back to the lead once")
        listing = []
        for k, items in res["must_fix"].items():
            listing.append(f"{k.replace('_', ' ').capitalize()}:")
            listing += [f"- {x}" for x in items]
        lead(
            "The gate found these problems in out/rfc.md / out/coverage.json. Fix each one in place — don't "
            "rewrite untouched sections (the §2 coverage table and References are regenerated) — then reply "
            "done:\n" + "\n".join(listing),
            "gate-fix",
        )
        render()
        state["gate_fixed_for"] = content_hash()
        res = check()
        observe.gate("draft", res["must_fix"], res["flags"], kind="draft", round_label="after-fix", stage="draft")
        fixed = True
    status = "known_issues" if res["must_fix"] else ("fixed" if before["must_fix"] else "clean")
    (run_dir / "out" / "check.json").write_text(
        json.dumps(
            {
                "status": status,
                "must_fix_before_fix": before["must_fix"] if fixed else {},
                "known_issues": res["must_fix"],
                "flags": res["flags"],
                "counts": res["counts"],
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    state["gate_status"] = status
    state_path.write_text(json.dumps(state, indent=2), encoding="utf-8")

    cov, _ = load_json(cov_path)
    st = coverage.stats(rfc_path.read_text(encoding="utf-8"), cov or {})
    total = round(time.monotonic() - t0, 1)
    cost = sum(x["cost_usd"] for x in runs)
    prev = _read(run_dir / "logs" / "draft.json") or {}
    (run_dir / "logs" / "draft.json").write_text(
        json.dumps(
            {
                "settings": s,
                "seconds_this_invocation": total,
                "cost_usd": round(cost, 3),
                "runs": (prev.get("runs") or []) + runs,
                "gate": status,
                **st,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    sts = st["statuses"]
    if not args.check_only:
        prog.milestone(
            "written",
            f"RFC written: {st['size_kb']} KB, {st['design_sections']} flow section(s), {st['changes']} CHANGE(s), "
            f"{sum(sts.values())} REQs ({sts['covered']} covered, {sts['assumes']} assume a question, "
            f"{sts['deferred']} deferred, {sts['open']} open)",
        )
    gate_line = {"clean": f"{head} · clean", "fixed": "Gate: clean after the fix"}.get(
        status,
        f"Gate: {sum(len(v) for v in res['must_fix'].values())} known issue(s) left for review ("
        + ", ".join(f"{len(v)} {k.replace('_', ' ')}" for k, v in res["must_fix"].items())
        + ")",
    )
    prog.milestone(
        "done", f"{gate_line} · {len(res['flags'])} flag(s) for review · {_mmss(total)}, ${cost:.2f} → {rfc_path}"
    )
    prog.step("done")
    return 0


if __name__ == "__main__":
    sys.exit(main())
