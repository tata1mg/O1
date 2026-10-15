"""The python-only stages, each driven directly through its `Stage.engine` and then its gate.

Covers `sources`, `store_cases`, `derive_spec` and `render_xlsx`. `execute_be` has its own file because it
needs the subprocess mock.
"""

from __future__ import annotations

import json

import pytest

from workflows.testing import xlsx
from workflows.testing.stages import derive_spec, render_xlsx, sources, store_cases


def run_stage(stage, ctx):
    """What the runner does around a stage, minus the manifest: engine, then gate."""
    result = stage.engine(stage, ctx)
    return result, stage.gate(ctx)


# ---- sources -----------------------------------------------------------------------------------------
def test_sources_inventories_an_rfc_run_dir(ctx, tmp_path):
    rfc_run = tmp_path / "rfc-run"
    (rfc_run / "raw").mkdir(parents=True)
    (rfc_run / "out" / "detail").mkdir(parents=True)
    (rfc_run / "raw" / "prd.md").write_text("# PRD\n")
    (rfc_run / "out" / "rfc.md").write_text("# RFC\n")
    (rfc_run / "out" / "contract.md").write_text("# Contract\nGET /x\n")
    (rfc_run / "out" / "detail" / "plan.md").write_text("verify: call GET /x\n")
    ctx.inputs["rfc_run_dir"] = str(rfc_run)

    result, failures = run_stage(sources.STAGE, ctx)
    assert failures == []
    assert result.engine_desc == "python"
    inv = json.loads(ctx.path("qa/sources.json").read_text())
    by_label = {r["label"]: r for r in inv["sources"]}
    assert by_label["PRD"]["status"] == "present"
    assert by_label["PRD"]["path"] == str(rfc_run / "raw" / "prd.md")
    assert by_label["API Contract"]["bytes"] > 0
    # Test Data is always its own row and blocked when absent (SKILL.md:18-37)
    assert by_label["Test Data"]["status"] == "blocked"
    assert by_label["Figma"]["status"] == "missing"


def test_an_explicit_path_overrides_the_rfc_run_dir(ctx, tmp_path):
    rfc_run = tmp_path / "rfc-run"
    (rfc_run / "raw").mkdir(parents=True)
    (rfc_run / "out").mkdir(parents=True)
    (rfc_run / "raw" / "prd.md").write_text("from the run dir\n")
    (rfc_run / "out" / "rfc.md").write_text("# RFC\n")
    mine = tmp_path / "mine.md"
    mine.write_text("the one I passed\n")
    ctx.inputs.update({"rfc_run_dir": str(rfc_run), "prd": str(mine)})

    run_stage(sources.STAGE, ctx)
    inv = json.loads(ctx.path("qa/sources.json").read_text())
    assert next(r for r in inv["sources"] if r["label"] == "PRD")["path"] == str(mine)


def test_the_gate_fails_when_the_prd_or_rfc_is_missing(ctx, tmp_path):
    only_prd = tmp_path / "prd.md"
    only_prd.write_text("# PRD\n")
    ctx.inputs["prd"] = str(only_prd)
    _, failures = run_stage(sources.STAGE, ctx)
    assert len(failures) == 1
    assert failures[0].startswith("RFC is blocked")
    assert "--rfc-run-dir" in failures[0]


def test_an_empty_source_file_is_blocked_not_present(ctx, tmp_path):
    prd, rfc = tmp_path / "prd.md", tmp_path / "rfc.md"
    prd.write_text("")
    rfc.write_text("# RFC\n")
    ctx.inputs.update({"prd": str(prd), "rfc": str(rfc)})
    _, failures = run_stage(sources.STAGE, ctx)
    assert any("PRD is blocked" in f for f in failures)


def test_a_mistyped_path_is_reported_with_the_path(ctx, tmp_path):
    """The operator needs to see the typo, not "no source given"."""
    rfc = tmp_path / "rfc.md"
    rfc.write_text("# RFC\n")
    ctx.inputs.update({"prd": str(tmp_path / "typo.md"), "rfc": str(rfc)})
    run_stage(sources.STAGE, ctx)
    row = next(r for r in json.loads(ctx.path("qa/sources.json").read_text())["sources"]
               if r["label"] == "PRD")
    assert row["status"] == "missing" and row["path"].endswith("typo.md")


def test_a_missing_source_is_recorded_as_a_decision(ctx, tmp_path):
    prd, rfc = tmp_path / "prd.md", tmp_path / "rfc.md"
    prd.write_text("# PRD\n")
    rfc.write_text("# RFC\n")
    ctx.inputs.update({"prd": str(prd), "rfc": str(rfc)})
    run_stage(sources.STAGE, ctx)
    lines = [json.loads(x) for x in ctx.path("logs/decisions.jsonl").read_text().splitlines()]
    labels = {x["label"] for x in lines if x["decision"] == "source_missing"}
    assert {"API Contract", "Test Data", "Figma", "Implementation Plan"} == labels


# ---- store_cases -------------------------------------------------------------------------------------
def test_store_cases_upserts_and_summarises(sourced_ctx, clean_cases):
    result, failures = run_stage(store_cases.STAGE, sourced_ctx)
    assert failures == []
    summary = json.loads(sourced_ctx.path("qa/store.json").read_text())
    assert summary["backend"] == "json"
    assert summary["created"] == len(clean_cases) and summary["updated"] == 0
    assert "created 6" in result.final_text


def test_store_cases_is_idempotent(sourced_ctx, clean_cases):
    run_stage(store_cases.STAGE, sourced_ctx)
    run_stage(store_cases.STAGE, sourced_ctx)
    summary = json.loads(sourced_ctx.path("qa/store.json").read_text())
    assert (summary["created"], summary["updated"]) == (0, len(clean_cases))


def test_store_cases_records_the_upsert_decision(sourced_ctx):
    run_stage(store_cases.STAGE, sourced_ctx)
    lines = [json.loads(x) for x in sourced_ctx.path("logs/decisions.jsonl").read_text().splitlines()]
    stored = next(x for x in lines if x["decision"] == "cases_stored")
    assert stored["backend"] == "json" and stored["total"] == 6


def test_dry_run_writes_nothing_to_the_store(sourced_ctx, tmp_path, ctx_factory, clean_cases):
    from workflows.testing.stages import write_json

    c = ctx_factory(tmp_path / "dry", inputs={"feature": "RSE-1"}, flags={"testing.dry_run": True})
    write_json(c.path("qa/cases.json"), clean_cases)
    result, failures = run_stage(store_cases.STAGE, c)
    assert failures == []
    assert "NOT written" in result.final_text
    assert not (c.run_dir / "store").exists()


def test_the_gate_catches_arithmetic_that_does_not_add_up(sourced_ctx):
    from workflows.testing.stages import write_json

    write_json(sourced_ctx.path("qa/store.json"),
               {"created": 2, "updated": 2, "total": 6, "backend": "json"})
    failures = store_cases.STAGE.gate(sourced_ctx)
    assert any("created(2) + updated(2) != total(6)" in f for f in failures)


def test_the_gate_catches_a_store_that_holds_fewer_cases_than_the_suite(sourced_ctx):
    from workflows.testing.stages import write_json

    write_json(sourced_ctx.path("qa/store.json"),
               {"created": 3, "updated": 0, "total": 3, "backend": "json"})
    failures = store_cases.STAGE.gate(sourced_ctx)
    assert any("reports 3 cases but cases.json has 6" in f for f in failures)


# ---- derive_spec -------------------------------------------------------------------------------------
def test_derive_spec_transcribes_the_stored_http_cases(sourced_ctx):
    run_stage(store_cases.STAGE, sourced_ctx)
    result, failures = run_stage(derive_spec.STAGE, sourced_ctx)
    assert failures == []
    spec = json.loads(sourced_ctx.path("qa/spec.json").read_text())
    assert [e["extid"] for e in spec] == ["TC-RSE-CAP-FUNC-01", "TC-RSE-SLOT-FUNC-01",
                                          "TC-RSE-SLOT-NEG-01"]
    slots = next(e for e in spec if e["extid"] == "TC-RSE-SLOT-FUNC-01")
    assert slots["path"] == "/radiology/v1/slots?lab_id=42"
    assert slots["method"] == "GET" and slots["expect_status"] == 200
    assert {"path": "slots.0.departments", "present": True} in slots["assert"]
    assert "derived 3/3" in result.final_text


def test_derive_spec_drops_the_unparseable_case_and_records_why(sourced_ctx, sample_cases):
    from workflows.testing.stages import write_json

    write_json(sourced_ctx.path("qa/cases.json"), sample_cases)
    run_stage(store_cases.STAGE, sourced_ctx)
    _, failures = run_stage(derive_spec.STAGE, sourced_ctx)
    assert failures == []
    report = json.loads(sourced_ctx.path("qa/spec.report.json").read_text())
    dropped = {d["extid"]: d["why"] for d in report["dropped"]}
    assert "TC-RSE-CAP-FUNC-02" in dropped
    assert "route is not stated" in dropped["TC-RSE-CAP-FUNC-02"]
    lines = [json.loads(x) for x in sourced_ctx.path("logs/decisions.jsonl").read_text().splitlines()]
    assert any(x["decision"] == "case_dropped_by_deriver" and x["extid"] == "TC-RSE-CAP-FUNC-02"
               for x in lines)


def test_derive_spec_flags_a_status_only_case_as_weak(sourced_ctx, clean_cases):
    from workflows.testing.stages import write_json

    weak = {**clean_cases[0], "id": "TC-RSE-SLOT-FUNC-07", "scenario_id": "TS-RSE-SLOT-FUNC-07",
            "steps": "1. Send GET /x\n2. Assert status == 200"}
    write_json(sourced_ctx.path("qa/cases.json"), [weak])
    run_stage(store_cases.STAGE, sourced_ctx)
    run_stage(derive_spec.STAGE, sourced_ctx)
    report = json.loads(sourced_ctx.path("qa/spec.report.json").read_text())
    assert report["status_only"] == ["TC-RSE-SLOT-FUNC-07"]
    lines = [json.loads(x) for x in sourced_ctx.path("logs/decisions.jsonl").read_text().splitlines()]
    assert any(x["decision"] == "case_status_only" for x in lines)


def test_the_gate_says_to_fix_the_cases_not_the_parser(sourced_ctx):
    from workflows.testing.stages import write_json

    write_json(sourced_ctx.path("qa/cases.json"), [])
    write_json(sourced_ctx.path("qa/store.json"),
               {"created": 0, "updated": 0, "total": 0, "backend": "json"})
    run_stage(derive_spec.STAGE, sourced_ctx)
    failures = derive_spec.STAGE.gate(sourced_ctx)
    assert len(failures) == 1
    assert "Fix the cases in the suite, not the parser" in failures[0]


def test_the_exec_tag_is_a_flag(tmp_path, ctx_factory, clean_cases):
    """The tag that marks a case machine-executable is a flag: selecting on another tag needs no code edit."""
    from workflows.testing.stages import write_json

    c = ctx_factory(tmp_path / "run", inputs={"feature": "RSE-1"},
                    flags={"testing.exec_tag": "exec:ui"})
    write_json(c.path("qa/cases.json"), clean_cases)
    run_stage(store_cases.STAGE, c)
    run_stage(derive_spec.STAGE, c)
    report = json.loads(c.path("qa/spec.report.json").read_text())
    assert report["total_http_cases"] == 2  # the two exec:ui cases in the clean suite


# ---- render_xlsx -------------------------------------------------------------------------------------
def test_the_render_is_skipped_with_a_decision_when_openpyxl_is_absent(sourced_ctx, monkeypatch):
    monkeypatch.setattr(xlsx, "available", lambda: False)
    result, failures = run_stage(render_xlsx.STAGE, sourced_ctx)
    assert failures == []  # a missing spreadsheet renderer must not stop the run
    assert "workbook skipped" in result.final_text
    lines = [json.loads(x) for x in sourced_ctx.path("logs/decisions.jsonl").read_text().splitlines()]
    skipped = next(x for x in lines if x["decision"] == "xlsx_skipped")
    assert skipped["fix"] == "uv sync --extra testing"


def test_the_render_is_skipped_when_the_flag_is_off(tmp_path, ctx_factory, clean_cases, clean_scenarios):
    from workflows.testing.stages import write_json

    c = ctx_factory(tmp_path / "run", inputs={"feature": "RSE-1"}, flags={"testing.xlsx": False})
    write_json(c.path("qa/cases.json"), clean_cases)
    write_json(c.path("qa/scenarios.json"), clean_scenarios)
    result, failures = run_stage(render_xlsx.STAGE, c)
    assert failures == [] and "testing.xlsx is off" in result.final_text


def test_the_workbook_name_matches_run_chains_shape(ctx):
    """`qa/<FEATURE>_QA_Suite.xlsx`, spaces to underscores (run_chain.sh:162)."""
    ctx.inputs["feature"] = "Family Hub"
    assert render_xlsx.workbook_rel(ctx) == "qa/Family_Hub_QA_Suite.xlsx"


@pytest.mark.skipif(not xlsx.available(), reason="openpyxl not installed (optional extra `testing`)")
def test_the_render_produces_both_sheets_with_the_declared_columns(sourced_ctx, clean_cases):
    result, failures = run_stage(render_xlsx.STAGE, sourced_ctx)
    assert failures == []
    assert "wrote RSE-1_QA_Suite.xlsx" in result.final_text
    sheets = xlsx.read_sheets(sourced_ctx.path(render_xlsx.workbook_rel(sourced_ctx)))
    assert set(sheets) == {xlsx.SCENARIO_SHEET, xlsx.CASE_SHEET}
    assert sheets[xlsx.CASE_SHEET][0] == [h for h, _ in xlsx.CASE_COLUMNS]
    assert sheets[xlsx.SCENARIO_SHEET][0] == [h for h, _ in xlsx.SCENARIO_COLUMNS]
    assert len(sheets[xlsx.CASE_SHEET]) - 1 == len(clean_cases)


@pytest.mark.skipif(not xlsx.available(), reason="openpyxl not installed")
def test_the_gate_catches_a_render_that_is_not_a_faithful_projection(sourced_ctx, clean_cases):
    from workflows.testing.stages import write_json

    run_stage(render_xlsx.STAGE, sourced_ctx)
    write_json(sourced_ctx.path("qa/cases.json"), clean_cases + [clean_cases[0]])
    failures = render_xlsx.STAGE.gate(sourced_ctx)
    assert any("not a faithful projection" in f for f in failures)


@pytest.mark.skipif(not xlsx.available(), reason="openpyxl not installed")
def test_a_rendered_workbook_reads_back_with_its_execution_column(sourced_ctx):
    """The `execution` column must survive a render and re-read: without it every imported case loses its
    classification and becomes unrunnable."""
    run_stage(render_xlsx.STAGE, sourced_ctx)
    sheets = xlsx.read_sheets(sourced_ctx.path(render_xlsx.workbook_rel(sourced_ctx)))
    cases = xlsx.parse_cases(sheets)
    assert {c["id"]: c["execution"] for c in cases}["TC-RSE-SLOT-FUNC-01"] == "http"
    assert all("execution" in c for c in cases)


@pytest.mark.skipif(not xlsx.available(), reason="openpyxl not installed")
def test_scenarios_round_trip_too(sourced_ctx, clean_scenarios):
    run_stage(render_xlsx.STAGE, sourced_ctx)
    sheets = xlsx.read_sheets(sourced_ctx.path(render_xlsx.workbook_rel(sourced_ctx)))
    parsed = xlsx.parse_scenarios(sheets)
    assert [s["id"] for s in parsed] == [s["id"] for s in clean_scenarios]
    assert parsed[0]["description"] == clean_scenarios[0]["description"]


def test_write_workbook_raises_a_fixable_error_without_openpyxl(tmp_path, monkeypatch):
    """The message must name the fix (`uv sync --extra testing`), per the harness's error convention."""
    import builtins

    real_import = builtins.__import__

    def no_openpyxl(name, *a, **kw):
        if name.startswith("openpyxl"):
            raise ImportError("no openpyxl")
        return real_import(name, *a, **kw)

    monkeypatch.setattr(builtins, "__import__", no_openpyxl)
    with pytest.raises(xlsx.OpenpyxlMissing, match="uv sync --extra testing"):
        xlsx.write_workbook(tmp_path / "x.xlsx", [], [])
