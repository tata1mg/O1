"""`status` — where a testing run stands and what the user does next, read from its files.

`/test-agent:test` reports progress, the post-generation handoff and the pre-execution check from this
instead of re-reading half a dozen JSON files in chat. It also decides `next`, so a resumed command (the
user comes back the day after reviewing in Kiwi) lands on the right step from the run dir alone.

    python -m workflows.testing.status <run_dir> [--json] [--kiwi]     the whole picture
    python -m workflows.testing.status <run_dir> --milestone <stage>   one line for a finished stage

`--kiwi` asks the case store how many of this feature's cases each status holds, split by `exec:` tag —
the check before execution, since only CONFIRMED `exec:http` cases run. It is the only network call, and
it uses the flags a fresh invocation would resolve (config layer included), not the run's flags.json,
which the latest invocation overwrites.

Read-only: writes nothing.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

if __package__ in (None, ""):  # run as a script: make `workflows.testing` importable from src/
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

#: Phase A ends at the human review; phase B starts after it. Order is the default composition's.
GENERATE_STAGES = ("sources", "generate_cases", "render_xlsx", "store_cases", "prerequisites")
#: Runs on demand (`/test-agent:evaluate`); never blocks or resumes generation, review or execution.
EVALUATE_STAGE = "evaluate"
EXECUTE_STAGES = ("derive_spec", "execute_be", "derive_ui", "execute_web", "execute_ui_agent", "publish_app")
#: Where each executor writes its results, by kind.
RESULTS = {"backend": "qa/results.json", "web": "qa/ui/results.json", "app": "qa/app/results.json",
           "agent": "qa/agent/results.json"}


def _load(path: Path, default=None):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def _feature(run_dir: Path) -> str:
    identity = _load(run_dir / "identity.json", {}) or {}
    sources = _load(run_dir / "qa/sources.json", {}) or {}
    return str(identity.get("feature") or (identity.get("inputs") or {}).get("feature")
               or sources.get("feature") or "")


def stages(run_dir: Path) -> dict[str, dict]:
    manifest = _load(run_dir / "manifest.json", {}) or {}
    out = {}
    for name, row in manifest.items():
        if not isinstance(row, dict):
            continue
        gate = row.get("gate") or {}
        out[name] = {"status": row.get("status"), "attempts": row.get("attempts"),
                     "duration_s": round(row.get("duration_s_total") or 0),
                     "cost_usd": row.get("cost_usd_total"),
                     "gate_passed": gate.get("passed"), "failures": (gate.get("failures") or [])[:5]}
    return out


def cases_summary(run_dir: Path) -> dict:
    cases = _load(run_dir / "qa/cases.json", []) or []
    cases = [c for c in cases if isinstance(c, dict)]
    count = lambda key: dict(Counter(str(c.get(key) or "?") for c in cases).most_common())  # noqa: E731
    return {"total": len(cases), "execution": count("execution"), "surface": count("surface"),
            "priority": count("priority"), "modules": len({c.get("module") for c in cases})}


def eval_summary(run_dir: Path) -> dict | None:
    ev = _load(run_dir / "qa/eval.json")
    if not isinstance(ev, dict):
        return None
    checks = ev.get("compliance_checks") or {}
    failed = [k for k, v in checks.items() if isinstance(v, dict) and v.get("status") == "FAIL"]
    return {"overall": ev.get("overall_score"), "coverage": ev.get("coverage_score"),
            "quality": ev.get("quality_score"), "compliance": ev.get("compliance_status"),
            "checks": len(checks), "failed_checks": failed,
            "missing_scenarios": len(ev.get("missing_scenarios") or [])}


def store_summary(run_dir: Path) -> dict | None:
    st = _load(run_dir / "qa/store.json")
    if not isinstance(st, dict):
        return None
    return {k: st.get(k) for k in ("backend", "total", "created", "updated", "unchanged", "protected",
                                   "product", "plan_url", "status")}


def prerequisites_summary(run_dir: Path) -> dict | None:
    doc = _load(run_dir / "qa/prerequisites.json")
    if not isinstance(doc, dict):
        return None
    facts = _load(run_dir / "qa/prerequisites.facts.json", {}) or {}

    def unknown(item: dict) -> bool:
        return str(item.get("how_to_create", "")).lower().startswith("unknown")

    fixtures = [f for f in doc.get("fixtures") or [] if isinstance(f, dict)]
    # The likeliest first-run blockers: the fixtures the most cases need, ones nobody knows how to create first.
    blockers = sorted(fixtures, key=lambda f: (-len(f.get("used_by") or []), not unknown(f)))[:3]
    return {
        "counts": {s: len(doc.get(s) or []) for s in ("fixtures", "config", "external_systems", "secrets",
                                                     "no_prerequisites")},
        "services": [s.get("service") for s in facts.get("services") or []],
        "secrets": [s.get("name") for s in doc.get("secrets") or [] if isinstance(s, dict)],
        "fixtures_unknown_how": sum(1 for f in fixtures if unknown(f)),
        "top_blockers": [{"id": f.get("id"), "entity": f.get("entity"),
                          "used_by": len(f.get("used_by") or []), "how_known": not unknown(f)}
                         for f in blockers],
        "md": str(run_dir / "qa/prerequisites.md"),
        "template": str(run_dir / "qa/test_data.template.yaml"),
    }


def test_data_supplied(run_dir: Path) -> bool:
    inv = _load(run_dir / "qa/sources.json", {}) or {}
    return any(r.get("key") == "test_data" and r.get("status") == "present" for r in inv.get("sources") or [])


def results_summary(run_dir: Path, rel: str = RESULTS["backend"]) -> dict | None:
    res = _load(run_dir / rel)
    if not isinstance(res, dict):
        return None
    # an agent's row carries `summary` (its one-line breakdown) where a script's carries `detail`
    failing = [{"extid": r.get("extid"), "status": r.get("status"),
                "detail": str(r.get("detail") or r.get("summary") or "")[:160]}
               for r in res.get("results") or [] if r.get("status") != "PASS"]
    return {"tally": res.get("tally") or {}, "run_url": res.get("run_url"), "html_report": res.get("html_report"),
            # execute_ui_agent only: how close the cases came, and the ones with nothing to judge against
            "parts_met": res.get("parts_met"), "parts": res.get("parts"),
            # every case with the time it took (a FAIL's includes its second run), and the same as a table
            "cases": [{"extid": r.get("extid"), "status": r.get("status"), "duration_s": r.get("duration_s"),
                       "cost_usd": r.get("cost_usd")} for r in res.get("results") or []],
            "table": _agent_table(res),
            "not_executed": [r.get("extid") for r in res.get("not_executed") or []],
            "skipped_unconfirmed": len(res.get("skipped_unconfirmed") or []),
            "skipped_wrong_execution": len(res.get("skipped_wrong_execution") or []),
            "not_passing": failing[:20]}


def _agent_table(res: dict) -> str | None:
    """execute_ui_agent's per-case table (Case | Verdict | Time | Cost | What the agent found), or None for the
    script executors, whose rows carry no summary."""
    rows = res.get("results") or []
    if not rows or "summary" not in rows[0]:
        return None
    from . import ui_agent

    return ui_agent.results_table(rows, res.get("not_executed") or [], res.get("cost_unknown_for") or [])


def ui_plan_summary(run_dir: Path) -> dict | None:
    plan = _load(run_dir / "qa/ui/plan.json")
    if not isinstance(plan, dict):
        return None
    return {"tests": len(plan.get("tests") or []),
            "skipped": [{"extid": r.get("extid"), "reason": r.get("reason")}
                        for r in plan.get("skipped") or [] if isinstance(r, dict)]}


def next_step(st: dict[str, dict], store: dict | None) -> dict:
    """What the command does next. `step` is one of: start, fix, resume_generate, store, review, execute, done."""
    for name in (*GENERATE_STAGES, *EXECUTE_STAGES):
        row = st.get(name)
        if row and row["status"] not in ("ok", None):
            return {"step": "fix", "stage": name}
    if not st:
        return {"step": "start"}
    ran = [s for s in EXECUTE_STAGES if s in st]
    if ran:
        # Done means every kind that was started finished — a deriver without its executor did not. Which
        # kinds to run at all is the user's choice, so a kind never started does not keep a run open.
        half_done = any(d in st and e not in st for d, e in (("derive_spec", "execute_be"),
                                                               ("derive_ui", "execute_web")))
        return {"step": "execute"} if half_done or any(st[s]["status"] != "ok" for s in ran) \
            else {"step": "done"}
    missing = [s for s in GENERATE_STAGES if s not in st and s != "store_cases"]
    if missing:
        return {"step": "resume_generate", "stage": missing[0]}
    if "store_cases" not in st or not store or store.get("backend") != "kiwi":
        return {"step": "store"}
    return {"step": "review"}


#: Words in an engine error that mean the network or the API, not the run's inputs.
_TRANSIENT = ("api_error", "overloaded", "connection", "timed out", "timeout", "nameresolution",
              "network", "unreachable", "rate_limit", "503", "529")


def _last_terminal_reason(run_dir: Path, stage: str) -> str:
    """`terminal_reason` of the stage's last model result, "" when there is none."""
    stream = run_dir / "logs" / f"{stage}.stream.jsonl"
    try:
        lines = stream.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return ""
    for line in reversed(lines):
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        if isinstance(ev, dict) and ev.get("type") == "result":
            return str(ev.get("terminal_reason") or "") if ev.get("is_error") else ""
    return ""


def explain_failure(run_dir: Path, stage: str, row: dict) -> dict:
    """Why `stage` stopped, in words for the user: {kind: connection|gate|engine, message, retry}.

    `retry` is true when re-running the same command is the whole fix. The gate's own wording ("declared
    output missing: qa/cases.json") reads as a broken suite; a run that stopped on that line was a dropped
    connection (2026-11-03, hdpo), and the user abandoned it instead of re-running.
    """
    engine_line = ""
    try:
        for line in (run_dir / "logs" / "activity.log").read_text(errors="replace").splitlines():
            if f"[{stage}] ENGINE FAILURE:" in line:
                engine_line = line.split("ENGINE FAILURE:", 1)[1].strip()
    except OSError:
        pass
    failures = row.get("failures") or []
    only_missing = bool(failures) and all(f.startswith("declared output missing") for f in failures)
    if row.get("status") == "engine_failed" or (only_missing and _last_terminal_reason(run_dir, stage)):
        text = engine_line or _last_terminal_reason(run_dir, stage) or "the model session failed"
        transient = only_missing or any(w in text.lower() for w in _TRANSIENT)
        return {"kind": "connection" if transient else "engine",
                "message": (f"{stage} lost its connection to the model before it finished "
                            f"({text[:200]}). Nothing is wrong with the inputs: re-run to resume from {stage}."
                            if transient else f"{stage} failed to run: {text[:300]}"),
                "retry": transient}
    if only_missing:
        files = ", ".join(f.split(":", 1)[1].strip() for f in failures)
        return {"kind": "gate", "message": f"{stage} finished without writing {files}.", "retry": True}
    return {"kind": "gate",
            "message": f"{stage} produced output that failed {len(failures)} check(s) after its automatic "
                       f"revision: " + "; ".join(str(f)[:160] for f in failures[:3]),
            "retry": False}


def kiwi_counts(feature: str, run_dir: Path) -> dict:
    """{status: {exec: n}} for this feature's plan, from the live store; {"error": …} when unreachable."""
    import socket

    from .preflight import KIWI_TIMEOUT_S, load_config, resolved_flags, store_for

    flags = resolved_flags(load_config())
    if flags.get("testing.storage") != "kiwi":
        return {"error": f"testing.storage is {flags.get('testing.storage')!r}, not kiwi"}
    previous = socket.getdefaulttimeout()
    socket.setdefaulttimeout(KIWI_TIMEOUT_S)
    try:
        cases = store_for(flags, run_dir).list_cases(feature)
    except Exception as exc:  # report, never raise: the command turns this into a message
        return {"error": f"{type(exc).__name__}: {exc}"}
    finally:
        socket.setdefaulttimeout(previous)
    # Only this run's suite counts. A plan also holds every earlier run's cases for the feature (ids the
    # generator no longer produces); they are reported as `stale`, never as cases waiting for review.
    suite = {c.get("id") for c in _load(run_dir / "qa/cases.json", []) or [] if isinstance(c, dict)}
    out: dict[str, dict[str, int]] = {}
    stale: dict[str, int] = {}
    ui: dict[str, int] = {}
    for c in cases:
        if suite and c.extid not in suite:
            stale[c.status] = stale.get(c.status, 0) + 1
            continue
        # Two `exec:` tags on one case is a store that only ever added tags; "conflict" says so instead of
        # counting it under whichever tag came first.
        execs = [t.split(":", 1)[1] for t in c.tags if t.startswith("exec:")]
        exec_ = execs[0] if len(execs) == 1 else ("conflict" if execs else "?")
        if c.status == "CONFIRMED" and exec_ == "ui":
            kind = "web" if "surface:web" in c.tags else "app" if any(
                t in c.tags for t in ("surface:android", "surface:ios")) else None
            if kind:
                ui[kind] = ui.get(kind, 0) + 1
        out.setdefault(c.status, {}).setdefault(exec_, 0)
        out[c.status][exec_] += 1
    return {"by_status": out, "confirmed_http": out.get("CONFIRMED", {}).get("http", 0),
            "http_total": sum(v.get("http", 0) for v in out.values()),
            "confirmed_web_ui": ui.get("web", 0), "confirmed_app_ui": ui.get("app", 0),
            "not_in_kiwi": len(suite) - sum(sum(v.values()) for v in out.values()) if suite else 0,
            "stale": stale}


def report(run_dir: Path, *, kiwi: bool = False) -> dict:
    st = stages(run_dir)
    store = store_summary(run_dir)
    feature = _feature(run_dir)
    out = {
        "run_dir": str(run_dir), "feature": feature, "stages": st, "next": next_step(st, store),
        "cases": cases_summary(run_dir), "eval": eval_summary(run_dir), "store": store,
        "prerequisites": prerequisites_summary(run_dir), "test_data_supplied": test_data_supplied(run_dir),
        "results": results_summary(run_dir),
        "results_web": results_summary(run_dir, RESULTS["web"]),
        "results_app": results_summary(run_dir, RESULTS["app"]),
        "results_agent": results_summary(run_dir, RESULTS["agent"]),
        "ui_plan": ui_plan_summary(run_dir),
        "xlsx": next((str(p) for p in sorted((run_dir / "qa").glob("*.xlsx"))), None),
        "cost_usd": round(sum(r["cost_usd"] or 0 for r in st.values()), 2),
        "duration_s": sum(r["duration_s"] for r in st.values()),
    }
    if out["next"]["step"] == "fix":
        out["next"]["why"] = explain_failure(run_dir, out["next"]["stage"], st[out["next"]["stage"]])
    if kiwi:
        out["kiwi"] = kiwi_counts(feature, run_dir)
    return out


def milestone(run_dir: Path, stage: str) -> str:
    """One chat line for a stage that just finished."""
    st = stages(run_dir).get(stage) or {}
    took = f" ({st.get('duration_s', 0) // 60}m{st.get('duration_s', 0) % 60:02d}s"
    took += f", ${st['cost_usd']:.2f})" if st.get("cost_usd") else ")"
    retry = f", passed on attempt {st['attempts']}" if (st.get("attempts") or 1) > 1 else ""
    if stage == "sources":
        inv = (_load(run_dir / "qa/sources.json", {}) or {}).get("sources") or []
        have = [r["label"] for r in inv if r.get("status") == "present"]
        lack = [r["label"] for r in inv if r.get("status") != "present"]
        return f"sources: {', '.join(have)}" + (f" · missing {', '.join(lack)}" if lack else "") + took
    if stage == "generate_cases":
        c = cases_summary(run_dir)
        mix = " · ".join(f"{n} {k}" for k, n in c["execution"].items())
        return f"generate_cases: {c['total']} cases in {c['modules']} modules ({mix}){retry}{took}"
    if stage == "render_xlsx":
        x = next((p.name for p in sorted((run_dir / "qa").glob("*.xlsx"))), "no workbook")
        return f"render_xlsx: {x}{took}"
    if stage == "evaluate":
        e = eval_summary(run_dir) or {}
        failed = len(e.get("failed_checks") or [])
        return (f"evaluate: score {e.get('overall')}/5 (coverage {e.get('coverage')}, quality "
                f"{e.get('quality')}) · compliance {e.get('compliance')}, {failed} of {e.get('checks')} "
                f"checks failed{retry}{took}")
    if stage == "store_cases":
        s = store_summary(run_dir) or {}
        return (f"store_cases: {s.get('total')} cases in {s.get('backend')} {s.get('plan_url') or ''} "
                f"({s.get('created')} new, {s.get('updated')} updated, {s.get('protected')} kept as "
                f"reviewed) as {s.get('status')}{took}")
    if stage == "prerequisites":
        p = prerequisites_summary(run_dir) or {"counts": {}}
        k = p["counts"]
        return (f"prerequisites: {k.get('fixtures')} fixtures · {k.get('config')} config · "
                f"{k.get('external_systems')} external systems · {k.get('secrets')} secrets{retry}{took}")
    if stage == "derive_spec":
        rep = _load(run_dir / "qa/spec.report.json", {}) or {}
        spec = _load(run_dir / "qa/spec.json", []) or []
        n = len(spec) if isinstance(spec, list) else len(spec.get("cases") or [])
        return f"derive_spec: {n} executable cases" + (f" · {rep['skipped']} skipped"
                                                      if isinstance(rep.get("skipped"), int) else "") + took
    if stage in ("execute_be", "execute_web", "publish_app"):
        kind = {"execute_be": "backend", "execute_web": "web", "publish_app": "app"}[stage]
        r = results_summary(run_dir, RESULTS[kind]) or {}
        tally = " · ".join(f"{v} {k}" for k, v in sorted((r.get("tally") or {}).items()))
        return f"{stage}: {tally or 'nothing ran'} · {r.get('run_url') or ''}{took}"
    if stage == "execute_ui_agent":
        r = results_summary(run_dir, RESULTS["agent"]) or {}
        tally = " · ".join(f"{v} {k}" for k, v in sorted((r.get("tally") or {}).items()))
        parts = f" · {r['parts_met']} of {r['parts']} parts met" if r.get("parts") else ""
        idle = f" · {len(r['not_executed'])} not executed (no expected result)" if r.get("not_executed") else ""
        spent = sum(c.get("duration_s") or 0 for c in r.get("cases") or [])
        each = f" · {int(spent // 60)}m {int(spent % 60):02d}s of agent time over {len(r['cases'])} cases" \
            if r.get("cases") else ""
        return f"execute_ui_agent: {tally or 'nothing ran'}{parts}{idle}{each} · {r.get('run_url') or ''}{took}"
    if stage == "derive_ui":
        p = ui_plan_summary(run_dir) or {"tests": 0, "skipped": []}
        return (f"derive_ui: {p['tests']} Playwright tests written, {len(p['skipped'])} cases left for a person"
                f"{retry}{took}")
    return f"{stage}: {st.get('status', '?')}{took}"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="status")
    ap.add_argument("run_dir")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--kiwi", action="store_true")
    ap.add_argument("--milestone", metavar="STAGE")
    args = ap.parse_args(argv)
    run_dir = Path(args.run_dir).expanduser()
    if args.milestone:
        print(milestone(run_dir, args.milestone))
        return 0
    data = report(run_dir, kiwi=args.kiwi)
    print(json.dumps(data, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
