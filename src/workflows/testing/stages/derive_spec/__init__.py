"""`derive_spec` — transcribe the stored CONFIRMED `exec:http` cases into an execution spec.

`be_spec.derive` through the `CaseStore`, so this runs against the JSON store with no server. Every case
the deriver drops and every status-only (weak) entry becomes a decision record — in test_agent both lists
were printed to stdout and then lost, which is exactly the accounting a QA lead needs later ("why is this
case not in the run?").
"""

from __future__ import annotations

from harness.contracts import RunContext, Stage

from ... import be_spec
from ...store import get_store
from .. import feature_of, python_stage, read_json, read_json_gated, write_json

SPEC_OUT = "qa/spec.json"


@python_stage
def _run(stage: Stage, ctx: RunContext) -> str:
    from rfc_agent import decisions

    feature = feature_of(ctx)
    exec_tag = str(ctx.flag("testing.exec_tag", "exec:http"))
    store = get_store(ctx)
    spec, dropped, weak, total = be_spec.derive(store, feature, exec_tag=exec_tag)
    write_json(ctx.path(SPEC_OUT), spec)
    write_json(ctx.path("qa/spec.report.json"),
               {"derived": len(spec), "total_http_cases": total,
                "dropped": [{"extid": e, "why": w} for e, w in dropped], "status_only": weak,
                "report": be_spec.report(spec, dropped, weak, total)})
    for extid, why in dropped:
        decisions.record(ctx, stage.name, "case_dropped_by_deriver",
                         why=f"{extid} is tagged {exec_tag} but its steps could not be transcribed: {why}. "
                             f"Fix the case, not the parser — a spec entry guessed from prose is how a "
                             f"green result comes to prove nothing.",
                         extid=extid, reason=why)
    for extid in weak:
        decisions.record(ctx, stage.name, "case_status_only",
                         why=f"{extid} derived with no body assertion — it passes on a 200 whatever the "
                             f"body says",
                         extid=extid)
    return f"derived {len(spec)}/{total} {exec_tag} cases ({len(dropped)} dropped, {len(weak)} weak)"


def _gate(ctx: RunContext) -> list[str]:
    spec, bad = read_json_gated(ctx.path(SPEC_OUT), expect=list)
    if bad:
        return bad
    report = read_json(ctx.path("qa/spec.report.json"), {}) or {}
    exec_tag = str(ctx.flag("testing.exec_tag", "exec:http"))
    if not spec:
        return [
            f"nothing derivable from the store's CONFIRMED {exec_tag} cases "
            f"({report.get('total_http_cases', 0)} were considered). An {exec_tag} case must name its "
            f"route and its expected status as numbered steps. Fix the cases in the suite, not the "
            f"parser."
        ]
    out = []
    for entry in spec:
        if not entry.get("path"):
            out.append(f"{entry.get('extid')}: derived entry has no path")
        if "expect_status" not in entry and not entry.get("assert"):
            out.append(f"{entry.get('extid')}: derived entry asserts nothing")
    return out


STAGE = Stage(
    name="derive_spec",
    requires=("qa/store.json",),
    outputs=(SPEC_OUT, "qa/spec.report.json"),
    build_prompt=lambda ctx: "",
    gate=_gate,
    engine=_run,
)
