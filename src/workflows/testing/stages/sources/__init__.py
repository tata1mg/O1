"""`sources` — the Step-0 Resource Sufficiency Check, as data.

The generator SKILL demands an inventory table before anything else (SKILL.md:18-37): every source gets a
row, API Contract and Test Data are always their own rows and are "Blocked" when absent, and the report is
delivered before generation proceeds. In test_agent that table existed only in the chat transcript, so
nothing downstream could know which sources were actually available — and `run_chain.sh:124-129` did the
same path resolution in bash, unrecorded.

Here it is `qa/sources.json`, which is (a) the gate's input, (b) what the generator prompt is built from,
and (c) the evaluator's requirement list. The path-preference rule is ported verbatim: prefer the RFC
run's NORMALIZED local copy over the original URL/path. Measured 2026-08-11 (run_chain.sh:122-123):
passing URLs made the generator stop mid-run to ask for Confluence/Figma permissions, killing draft
after investigate had fully succeeded.
"""

from __future__ import annotations

from pathlib import Path

from harness.contracts import RunContext, Stage

from .. import DECISIONS, feature_of, python_stage, read_json_gated, write_json  # noqa: F401

#: (label, inputs key, run-dir-relative location inside an --rfc-run-dir, always-a-row).
#: "always-a-row" marks the two sources the SKILL requires to be listed even when absent, because their
#: absence changes what the suite can claim: no contract -> no verbatim routes -> BE cases are guesswork;
#: no test data -> every Test Data cell must be flagged as invented.
SOURCE_SPECS: tuple[tuple[str, str, str | None, bool], ...] = (
    ("PRD", "prd", "raw/prd.md", True),
    ("RFC", "rfc", "out/rfc.md", True),
    ("API Contract", "contract", "out/contract.md", True),
    ("Implementation Plan", "plan", "out/detail/plan.md", False),
    ("Figma", "figma", "raw/figma.md", False),
    ("Test Data", "test_data", None, True),
)
#: A source that must be present for generation to be worth attempting.
REQUIRED_LABELS: tuple[str, ...] = ("PRD", "RFC")


def _resolve(ctx: RunContext, key: str, rfc_rel: str | None) -> Path | None:
    """Explicit --<key> wins; otherwise the RFC run dir's normalized copy."""
    explicit = ctx.inputs.get(key)
    if explicit:
        p = Path(str(explicit)).expanduser()
        if p.exists():
            return p
    rfc_run = ctx.inputs.get("rfc_run_dir")
    if rfc_run and rfc_rel:
        p = Path(str(rfc_run)).expanduser() / rfc_rel
        if p.exists():
            return p
    # An explicit path that does not exist is still reported (as missing, with the path) rather than
    # silently becoming "no source given" — the operator needs to see the typo.
    return Path(str(explicit)).expanduser() if explicit else None


def _inventory(ctx: RunContext) -> dict:
    rows = []
    for label, key, rfc_rel, always in SOURCE_SPECS:
        path = _resolve(ctx, key, rfc_rel)
        if path is None:
            status = "blocked" if always else "missing"
            rows.append({"label": label, "key": key, "path": "", "status": status, "bytes": 0,
                         "note": "not supplied" + (" — always reported, its absence weakens the suite"
                                                   if always else "")})
            continue
        if path.is_file():
            size = path.stat().st_size
            rows.append({"label": label, "key": key, "path": str(path),
                         "status": "present" if size else "blocked", "bytes": size,
                         "note": "" if size else "file is empty"})
        else:
            rows.append({"label": label, "key": key, "path": str(path), "status": "missing",
                         "bytes": 0, "note": "path does not exist"})
    return {"feature": feature_of(ctx), "rfc_run_dir": str(ctx.inputs.get("rfc_run_dir") or ""),
            "sources": rows}


@python_stage
def _run(stage: Stage, ctx: RunContext) -> str:
    from rfc_agent import decisions

    inv = _inventory(ctx)
    write_json(ctx.path("qa/sources.json"), inv)
    for row in inv["sources"]:
        if row["status"] != "present":
            decisions.record(ctx, stage.name, "source_missing",
                             why=f"{row['label']} is {row['status']} — recorded so the suite's coverage "
                                 f"claim can be read against what it actually had",
                             label=row["label"], path=row["path"], note=row["note"])
    present = [r["label"] for r in inv["sources"] if r["status"] == "present"]
    return f"{len(present)}/{len(inv['sources'])} sources present: {', '.join(present) or 'none'}"


def _gate(ctx: RunContext) -> list[str]:
    inv, bad = read_json_gated(ctx.path("qa/sources.json"), expect=dict)
    if bad:
        return bad
    if not inv.get("sources"):
        return ["qa/sources.json holds no source rows — the inventory is empty"]
    if not inv.get("feature"):
        return ["qa/sources.json has no feature — pass --feature"]
    by_label = {r["label"]: r for r in inv["sources"]}
    out = []
    for label in REQUIRED_LABELS:
        row = by_label.get(label) or {}
        if row.get("status") != "present":
            out.append(f"{label} is {row.get('status', 'absent')} ({row.get('path') or 'no path given'}) — "
                       f"a suite cannot be generated without it. Pass --{row.get('key', label.lower())} "
                       f"or --rfc-run-dir pointing at a completed RFC run.")
    return out


STAGE = Stage(
    name="sources",
    requires=(),
    outputs=("qa/sources.json",),
    build_prompt=lambda ctx: "",  # python stage: no model, no prompt
    gate=_gate,
    engine=_run,
)
