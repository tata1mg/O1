"""`store_cases` — the upsert, as a stage with a checked summary.

In test_agent this was a sentence in a prompt: "call kiwi_upsert_test_cases(feature=..., cases=[...])
exactly once" (run_chain.sh:174). Nothing verified the tool had been called, nothing recorded what it
returned, and a model that skipped it left a chain that reported success (survey §1: "a soft failure is
invisible"). Here the upsert is Python, the summary is `qa/store.json`, and the gate checks the arithmetic
the store itself reports.
"""

from __future__ import annotations

from harness.contracts import RunContext, Stage

from ...store import get_store
from .. import feature_of, python_stage, read_json, read_json_gated, write_json

STORE_OUT = "qa/store.json"


@python_stage
def _run(stage: Stage, ctx: RunContext) -> str:
    from rfc_agent import decisions

    feature = feature_of(ctx)
    cases = read_json(ctx.path("qa/cases.json"), []) or []
    if ctx.flag("testing.dry_run", False):
        write_json(ctx.path(STORE_OUT), {"created": 0, "updated": 0, "total": len(cases),
                                         "backend": "none", "feature": feature, "dry_run": True})
        return f"dry run — {len(cases)} cases NOT written"
    store = get_store(ctx)
    summary = store.upsert_cases(feature, cases)
    write_json(ctx.path(STORE_OUT), summary)
    decisions.record(ctx, stage.name, "cases_stored",
                     why="the suite was upserted into the case store; the summary is the record the "
                         "chain never kept",
                     backend=summary.get("backend"), created=summary.get("created"),
                     updated=summary.get("updated"), total=summary.get("total"))
    return (f"{summary.get('backend')}: created {summary.get('created')}, "
            f"updated {summary.get('updated')} of {summary.get('total')}")


def _gate(ctx: RunContext) -> list[str]:
    summary, bad = read_json_gated(ctx.path(STORE_OUT), expect=dict)
    if bad:
        return bad
    if summary.get("dry_run"):
        return []  # nothing was written, so there is no arithmetic to check
    cases = read_json(ctx.path("qa/cases.json"), []) or []
    out = []
    if summary.get("error"):
        out.append(f"the store refused the upsert: {summary['error']}")
    created, updated, total = (summary.get("created"), summary.get("updated"), summary.get("total"))
    if not all(isinstance(v, int) for v in (created, updated, total)):
        out.append(f"{STORE_OUT} must report integer created/updated/total, got "
                   f"{created!r}/{updated!r}/{total!r}")
    elif created + updated != total:
        out.append(f"created({created}) + updated({updated}) != total({total}) — some cases were "
                   f"neither created nor updated, so the suite in the store is incomplete")
    if isinstance(total, int) and total != len(cases):
        out.append(f"the store reports {total} cases but cases.json has {len(cases)}")
    return out


STAGE = Stage(
    name="store_cases",
    requires=("qa/cases.json",),
    outputs=(STORE_OUT,),
    build_prompt=lambda ctx: "",
    gate=_gate,
    engine=_run,
)
