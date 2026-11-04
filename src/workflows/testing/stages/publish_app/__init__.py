"""`publish_app` — a Maestro run's JUnit report -> one execution per case in the store.

Maestro runs where the device is: an emulator or phone, the app built from druid, the flows on disk. None of
that is in a run dir, so this stage does not run Maestro. It publishes what a run produced, the device half
of the results contract `execute_be` and `execute_web` provide:

    maestro test <druid>/.maestro/flows/<suite> --format junit --output report.xml
    python3 <druid>/scripts/maestro_report.py --report report.xml      # the per-step timeline
    harness run testing … --stages publish_app --enable testing.publish_app \\
        --maestro-report report.xml --maestro-flows <druid>/.maestro/flows --env emulator-5554 --build <ver>

Attribution is `..maestro` (ported from test_agent): a flow covers the cases its `# Covers:` block and
labelled steps name; a case FAILs only when the failing step names it. The store run is the same
`<feature> · <env> · <build>` run the other executors use. Each execution gets the evidence comment (the
step timeline, the failing step, links to the attachments: Kiwi renders comments, not attachments) and the
flow's screenshot, log and commands file.

OFF by default (`testing.publish_app`): it writes results into the shared store.
"""

from __future__ import annotations

import json
from pathlib import Path

from harness.contracts import EngineFailure, GateFailure, RunContext, Stage

from ... import maestro
from ...store import get_store
from .. import feature_of, python_stage, read_json_gated, write_json

RESULTS_OUT = "qa/app/results.json"
APP_SURFACES = ("surface:android", "surface:ios")


def _path_input(ctx: RunContext, key: str, flag: str) -> Path | None:
    raw = ctx.inputs.get(key) or ctx.flag(flag, "")
    return Path(str(raw)).expanduser() if raw else None


@python_stage
def _run(stage: Stage, ctx: RunContext) -> str:
    from rfc_agent import decisions

    feature = feature_of(ctx)
    report = _path_input(ctx, "maestro_report", "testing.maestro_report")
    flows_root = _path_input(ctx, "maestro_flows", "testing.maestro_flows")
    env_name = str(ctx.inputs.get("env") or ctx.flag("testing.env", "") or "")
    build = str(ctx.inputs.get("build") or ctx.flag("testing.build", "") or "")
    if report is None or not report.is_file():
        raise EngineFailure(f"publish_app needs --maestro-report <junit.xml> from `maestro test --format junit` "
                            f"({report or 'not given'} is not a file).")
    if flows_root is None or not flows_root.is_dir():
        raise EngineFailure(f"publish_app needs --maestro-flows <druid>/.maestro/flows, the folder the flows ran "
                            f"from ({flows_root or 'not given'} is not a folder).")
    if not env_name or not build:
        raise EngineFailure("publish_app needs --env (the device, e.g. emulator-5554) and --build (the app "
                            "version): they name the run and are recorded on every execution.")

    runs = maestro.parse_report(report)
    only = str(ctx.flag("testing.maestro_only_folder", "") or "").strip("/")
    if only:
        runs = [r for r in runs if (r["file"] or "").startswith(f".maestro/flows/{only}/")]
    if not runs:
        raise GateFailure(f"{report.name} holds no <testcase>" + (f" under flows/{only}/" if only else ""))
    claims = maestro.flow_claims(flows_root)
    map_file = _path_input(ctx, "maestro_map", "testing.maestro_map")
    override = json.loads(map_file.read_text()) if map_file else {}
    blocked = maestro.read_blocked(_path_input(ctx, "maestro_blocked", "testing.maestro_blocked"))

    per_flow, evidence, unmatched, no_claim, unenriched = [], [], [], [], []
    for r in runs:
        claim = ({"covers": set(override[r["flow"]]), "labels": [], "path": None}
                 if r["flow"] in override else claims.get(r["flow"]))
        if claim is None:
            unmatched.append(r["flow"])
            continue
        cases = maestro.attribute(r, claim)
        if not cases:
            no_claim.append(r["flow"])
            continue
        if not r["enriched"] and r["outcome"] != "PASS":
            unenriched.append(r["flow"])
        per_flow.append((r["flow"], cases))
        evidence.append((r, maestro.case_steps(r, claim), cases))
    agg = maestro.merge(per_flow)
    for cid, reason in blocked.items():
        if cid in agg:
            agg[cid] = ("BLOCKED", f"curated exclusion: {reason}", agg[cid][2], agg[cid][3])
    if not agg:
        raise GateFailure("no flow in the report covers a case id (no `# Covers:` block or labelled step "
                          f"names one; unmatched flows: {', '.join(unmatched) or 'none'}). Pass --maestro-map.")

    store = get_store(ctx)
    require = str(ctx.flag("testing.app_exec_tag", "exec:ui") or "") or None
    run = store.create_run(feature, build, env_name, sorted(agg), require_tag=require)
    attached = set(run.attached)
    confirmed_app = {c.extid for c in store.list_cases(feature, status="CONFIRMED")
                     if any(t in c.tags for t in APP_SURFACES)}
    for cid in sorted(confirmed_app - set(agg)):
        decisions.record(ctx, stage.name, "app_case_unattributed",
                         why="a CONFIRMED app case no flow in this report covers: it stays IDLE for a person",
                         extid=cid)

    device = next((r["device"] for r in runs if r["device"]), "")
    results, tally = [], {}
    for cid, (status, detail, flows, winner) in sorted(agg.items()):
        if cid not in attached:
            continue
        src = next((e for e in evidence if e[0]["flow"] == winner and cid in e[2]),
                   next((e for e in evidence if cid in e[2]), None))
        r0 = src[0] if src else None
        fields = r0["fields"] if r0 else {}
        props = {"flows": ", ".join(flows), "environment": env_name, "build": build, "report": report.name,
                 "harness": "maestro", "device": device}
        for key, prop in (("Command", "failing_command"), ("Source", "flow_source"), ("git", "git"),
                          ("appId", "app"), ("Awaited", "awaited_selector")):
            if fields.get(key):
                props[prop] = fields[key]
        if r0 and r0["failed_label"]:
            props["failing_step"] = r0["failed_label"]
        if r0 and r0["mock"]:
            props["verified_with_mock_data"] = str(r0["mock"])
        duration = r0["time"] if r0 and r0["time"] else None
        finished = maestro.run_finished_at(fields, duration, r0.get("timestamp")) if r0 else None
        execution_id = store.record_execution(run.run_id, cid, status, actual=detail, duration_s=duration,
                                              props=props, finished_at=finished)
        tally[status] = tally.get(status, 0) + 1
        results.append({"extid": cid, "status": status, "detail": detail, "flows": flows,
                        "duration_s": duration})
        if src:
            _evidence(ctx, stage.name, store, decisions, execution_id, cid, src, claims, build, env_name,
                      report.name)

    write_json(ctx.path(RESULTS_OUT), {
        "feature": feature, "environment": env_name, "build": build, "device": device,
        "report": str(report), "flows_root": str(flows_root), "run_id": run.run_id, "run_url": run.url,
        "results": results, "tally": tally, "unmatched_flows": unmatched, "flows_claiming_nothing": no_claim,
        "unenriched_flows": unenriched, "not_attached": sorted(set(agg) - attached),
        "skipped_unconfirmed": list(run.skipped_unconfirmed),
        "skipped_wrong_execution": list(run.skipped_wrong_execution),
        "unattributed": sorted(confirmed_app - set(agg)),
    })
    return "  ".join(f"{k}={v}" for k, v in sorted(tally.items())) or "nothing recorded"


def _evidence(ctx, stage_name, store, decisions, execution_id, cid, src, claims, build, env_name, report_name):
    """The evidence comment and its artifacts, for the flow that decided this case's verdict."""
    r_ev, steps_ev, cases_ev = src
    declared = [t for t, ids in ((claims.get(r_ev["flow"]) or {}).get("labels") or []) if cid in ids]
    body = maestro.evidence_comment(cid, cases_ev[cid][0], cases_ev[cid][1], r_ev, steps_ev.get(cid), build,
                                    env_name, report_name, declared)
    attached_now = []
    for key in ("screenshot", "log", "commands"):
        raw = r_ev["fields"].get(key)
        path = Path(raw) if raw else None
        if not path or not path.is_file() or path.stat().st_size > maestro.MAX_ATTACH:
            continue
        name = maestro.run_attachment_name(cid, r_ev["flow"], path)
        if store.attach("execution", execution_id, name, path.read_bytes()):
            attached_now.append((key, name, path))
            decisions.record(ctx, stage_name, "ui_evidence_attached",
                             why="the flow's artifact kept beside the execution it explains",
                             extid=cid, name=name, bytes=path.stat().st_size)
    urls = {key: store.attachment_url("execution", execution_id, name) for key, name, _ in attached_now}
    store.comment_execution(execution_id, maestro.with_evidence_links(body, attached_now,
                                                                      {k: v for k, v in urls.items() if v}))


def _gate(ctx: RunContext) -> list[str]:
    data, bad = read_json_gated(ctx.path(RESULTS_OUT), expect=dict)
    if bad:
        return bad
    results = data.get("results")
    if not isinstance(results, list):
        return [f"{RESULTS_OUT} has no results array"]
    out = [] if results else [
        "no app case was recorded: no attributed case is a CONFIRMED app UI case in the store "
        f"(not attached: {', '.join(data.get('not_attached') or []) or 'none'})"]
    for row in results:
        if row.get("status") not in ("PASS", "FAIL", "BLOCKED", "ERROR"):
            out.append(f"{row.get('extid')}: status {row.get('status')!r} is not PASS/FAIL/BLOCKED/ERROR")
    tally = data.get("tally") or {}
    if isinstance(tally, dict) and sum(tally.values()) != len(results):
        out.append(f"tally sums to {sum(tally.values())} but there are {len(results)} results")
    return out


STAGE = Stage(
    name="publish_app",
    requires=(),
    outputs=(RESULTS_OUT,),
    build_prompt=lambda ctx: "",
    gate=_gate,
    engine=_run,
)
