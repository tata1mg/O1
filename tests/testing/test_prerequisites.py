"""The `prerequisites` stage: plan facts in Python, the model's grouping gated, both rendered.

From the NR-DIGICHEM run (2026-11-02): 102 cases whose preconditions named records nobody had created and
an HMAC signature nobody could compute, with "Test Data: blocked" and nothing telling the team what to
supply.
"""

from __future__ import annotations

import json

import pytest

from workflows.testing import WORKFLOW, check_prerequisites, prereqs
from workflows.testing.scores import run_scores
from workflows.testing.stages import prerequisites, write_json

PLAN = """## 6. Implementation plan

### CHANGE-0 corporate service (config, external owner): `MERCHANT_CONFIG.nrch` [REQ-4]
- repo: corporate service config (outside this RFC's repos; owner: Corporate team)
- files: corporate-service `/corporates/config` data (new row)
- depends on: —
- verify: on staging, `get_key_value(MERCHANT_CONFIG)` returns `nrch`.

### CHANGE-1 merchant_aggregator: HDPO data model [REQ-1, REQ-2]
- repo: merchant_aggregator
- files: db/migrations/<ts>_create_hdpo.sql (new) · app/models/hdpo_order.py (new)
- depends on: CHANGE-0
- verify:
  - the migration applies on staging;
  - a unit test round-trips a row.

### CHANGE-2 validation_ui (external, owner: validation_ui team): portal [REQ-2]
- depends on: CHANGE-1 (queue), CHANGE-9
- verify: unit tests only.

## 7. Open questions
"""

CASES = [
    {"id": "TC-A-FUNC-01", "execution": "http", "requirement": "REQ-1",
     "precondition": "Job 98771 exists.", "steps": "1. Send POST /x with header S: <valid-hmac>",
     "test_data": "job_id=98771", "qa_comment": ""},
    {"id": "TC-A-NEG-01", "execution": "http", "requirement": "REQ-2", "precondition": "n/a",
     "steps": "1. Send GET /y", "test_data": "phone 9876543210 — sample value, unconfirmed", "qa_comment": ""},
    {"id": "TC-A-EDGE-01", "execution": "manual", "requirement": "REQ-9", "precondition": "n/a",
     "steps": "1. Read the doc", "test_data": "n/a", "qa_comment": ""},
]

DOC = {
    "fixtures": [{"id": "FX-01", "entity": "HDPO job", "state": "COMPLETED", "how_to_create": "CHANGE-1 seed",
                  "suite_values": ["job_id=98771"], "used_by": ["TC-A-FUNC-01"]},
                 {"id": "FX-02", "entity": "customer", "state": "test phone", "how_to_create": "unknown",
                  "used_by": ["TC-A-NEG-01"]}],
    "config": [{"key": "NRCH.SERVICEABLE_PINCODES", "service": "merchant_aggregator", "needed": "110055",
                "used_by": ["TC-A-FUNC-01"]}],
    "external_systems": [{"name": "HMIS sandbox", "needed_for": "callbacks", "used_by": ["TC-A-NEG-01"]}],
    "secrets": [{"name": "NRCH HMAC key", "provide_via": "secret flag", "used_by": ["TC-A-FUNC-01"]}],
    "no_prerequisites": ["TC-A-EDGE-01"],
}


@pytest.fixture
def facts():
    return prereqs.facts(CASES, PLAN)


# ---- the plan ---------------------------------------------------------------------------------------
def test_each_change_block_is_parsed_from_its_heading_and_keyed_lines():
    c0, c1, c2 = prereqs.parse_plan(PLAN)
    assert (c0["id"], c0["service"], c0["external"], c0["owner"]) == ("CHANGE-0", "corporate service", True,
                                                                     "Corporate team")
    assert c0["reqs"] == ["REQ-4"] and c0["verify_on_staging"]
    assert c1["service"] == "merchant_aggregator" and c1["reqs"] == ["REQ-1", "REQ-2"]
    assert c1["migrations"] == ["db/migrations/<ts>_create_hdpo.sql"]      # the "(new)" marker is dropped
    assert c1["depends_on"] == ["CHANGE-0"] and c1["verify_on_staging"]   # an indented verify list counts
    assert (c2["service"], c2["owner"], c2["external"]) == ("validation_ui", "validation_ui team", True)
    assert not c2["verify_on_staging"]


def test_the_last_block_stops_at_the_next_section():
    assert "Open questions" not in prereqs.parse_plan(PLAN)[-1]["verify"]


def test_deploy_waves_follow_depends_on_and_ignore_unknown_changes():
    assert prereqs.deploy_waves(prereqs.parse_plan(PLAN)) == [["CHANGE-0"], ["CHANGE-1"], ["CHANGE-2"]]


def test_a_dependency_cycle_does_not_hang():
    cyc = [{"id": "CHANGE-1", "depends_on": ["CHANGE-2"]}, {"id": "CHANGE-2", "depends_on": ["CHANGE-1"]}]
    assert sorted(sum(prereqs.deploy_waves(cyc), [])) == ["CHANGE-1", "CHANGE-2"]


# ---- the facts --------------------------------------------------------------------------------------
def test_a_case_needs_the_services_whose_changes_share_its_reqs(facts):
    rows = {r["id"]: r for r in facts["cases"]}
    assert rows["TC-A-FUNC-01"]["services"] == ["merchant_aggregator"]
    assert rows["TC-A-NEG-01"]["services"] == ["merchant_aggregator", "validation_ui"]
    assert facts["cases_without_service"] == ["TC-A-EDGE-01"]
    assert {s["service"]: len(s["cases"]) for s in facts["services"]}["merchant_aggregator"] == 2


def test_placeholders_and_invented_values_are_found(facts):
    rows = {r["id"]: r for r in facts["cases"]}
    assert rows["TC-A-FUNC-01"]["placeholders"] == ["<valid-hmac>"]
    assert rows["TC-A-NEG-01"]["invented"] and not rows["TC-A-EDGE-01"]["invented"]


def test_no_plan_still_yields_facts():
    f = prereqs.facts(CASES, "")
    assert f["services"] == [] and len(f["cases"]) == 3


# ---- the gate ---------------------------------------------------------------------------------------
def test_a_complete_file_passes(facts):
    assert prereqs.validate(DOC, CASES, facts) == []


def _with(**changes):
    doc = json.loads(json.dumps(DOC))
    doc.update(changes)
    return doc


@pytest.mark.parametrize("doc,needle", [
    (_with(no_prerequisites=[]), "1 case(s) are in no item's used_by"),
    (_with(no_prerequisites=["TC-A-EDGE-01", "TC-A-FUNC-01"]), "AND in an item's used_by"),
    (_with(no_prerequisites=["TC-A-EDGE-01", "TC-NOPE-01"]), "not cases in the suite: TC-NOPE-01"),
    (_with(secrets=[{"name": "k", "provide_via": "flag", "used_by": ["TC-A-FUNC-01"], "value": "abc"}]),
     "a secret is listed by name only"),
    (_with(fixtures=[dict(DOC["fixtures"][0]), dict(DOC["fixtures"][0], used_by=["TC-A-NEG-01"])]),
     "duplicate fixture ids: FX-01"),
    (_with(fixtures=[dict(DOC["fixtures"][0], id="F1"), DOC["fixtures"][1]]), "must look like FX-01"),
    (_with(fixtures=[dict(DOC["fixtures"][0], how_to_create=""), DOC["fixtures"][1]]), "`how_to_create` is empty"),
    (_with(config=[dict(DOC["config"][0], used_by=[])]), "`used_by` must list"),
    (_with(external_systems="none"), "external_systems is missing or not a list"),
])
def test_each_gate_rule_names_what_to_fix(facts, doc, needle):
    assert any(needle in f for f in prereqs.validate(doc, CASES, facts)), prereqs.validate(doc, CASES, facts)


def test_a_case_with_invented_data_cannot_be_declared_prerequisite_free(facts):
    doc = _with(fixtures=[DOC["fixtures"][0]], external_systems=[],
                no_prerequisites=["TC-A-EDGE-01", "TC-A-NEG-01"])
    assert any("TC-A-NEG-01 is in no_prerequisites but carries an invented value" in f
               for f in prereqs.validate(doc, CASES, facts))


# ---- rendering --------------------------------------------------------------------------------------
def test_the_markdown_carries_both_halves(facts):
    md = prereqs.render_md("NR", facts, DOC)
    for heading in ("## 1. Services to deploy", "## 2. Deploy order", "## 3. Migrations",
                    "## 4. Config and feature flags", "## 6. Secrets", "## 7. Test data fixtures",
                    "## 8. Smoke checks after deploy", "## 9. Cases with no prerequisites"):
        assert heading in md
    assert "1. CHANGE-0 (corporate service)" in md and "`db/migrations/<ts>_create_hdpo.sql`" in md
    assert "| FX-01 | HDPO job | COMPLETED |" in md and "NRCH HMAC key" in md


def test_the_template_lists_fixtures_and_config_but_never_secrets():
    text = prereqs.render_template("NR", DOC)
    assert "FX-01:" in text and '"NRCH.SERVICEABLE_PINCODES":' in text and 'real_value: ""' in text
    assert "HMAC" not in text
    yaml = pytest.importorskip("yaml")
    data = yaml.safe_load(text)
    assert data["fixtures"]["FX-01"]["suite_values"] == ["job_id=98771"]


def test_a_malformed_file_still_renders():
    assert "## 7. Test data fixtures" in prereqs.render_md("NR", prereqs.facts(CASES, PLAN), {"fixtures": "x"})
    assert "fixtures:" in prereqs.render_template("NR", [])


# ---- the stage --------------------------------------------------------------------------------------
class _Engine:
    def __init__(self, doc):
        self.doc, self.prompts = doc, []

    def run(self, req):
        from harness.engines import EngineResult
        from harness.events import TraceSummary

        self.prompts.append(req.prompt)
        (req.cwd / "qa" / "prerequisites.json").write_text(json.dumps(self.doc))
        return EngineResult(summary=TraceSummary(final_text="ok", got_result=True), duration_s=0.1,
                            engine_desc="fake", prompt_sent=req.prompt, trace_path=req.trace_path)


@pytest.fixture
def stage_ctx(tmp_path, ctx_factory):
    ctx = ctx_factory(tmp_path / "run", inputs={"feature": "NR"}, engine=_Engine(DOC))
    plan = tmp_path / "plan.md"
    plan.write_text(PLAN)
    write_json(ctx.path("qa/cases.json"), CASES)
    write_json(ctx.path("qa/sources.json"), {"sources": [
        {"label": "Implementation Plan", "key": "plan", "status": "present", "path": str(plan)}]})
    return ctx


def test_the_stage_writes_facts_then_renders_what_the_model_wrote(stage_ctx):
    prerequisites.STAGE.engine(prerequisites.STAGE, stage_ctx)
    for rel in prerequisites.STAGE.outputs:
        assert stage_ctx.path(rel).exists(), rel
    assert json.loads(stage_ctx.path(prerequisites.FACTS_OUT).read_text())["waves"][0] == ["CHANGE-0"]
    assert prerequisites.STAGE.gate(stage_ctx) == []
    prompt = stage_ctx.engine.prompts[0]
    assert prerequisites.CHECK_PREREQS in prompt and str(stage_ctx.path(prerequisites.FACTS_OUT)) in prompt
    assert "2 carry a placeholder" in prompt


def test_a_bad_file_fails_the_gate_but_is_still_rendered_for_revision(stage_ctx):
    stage_ctx.engine.doc = _with(no_prerequisites=[])
    prerequisites.STAGE.engine(prerequisites.STAGE, stage_ctx)
    assert stage_ctx.path(prerequisites.MD_OUT).exists()
    assert any("TC-A-EDGE-01" in f for f in prerequisites.STAGE.gate(stage_ctx))


def test_the_checker_is_the_gate_and_is_allowlisted(stage_ctx):
    assert prerequisites.CHECK_PREREQS in prerequisites.STAGE.allow_commands
    prerequisites.STAGE.engine(prerequisites.STAGE, stage_ctx)
    code, text = check_prerequisites.report(stage_ctx.run_dir)
    assert code == 0 and text.endswith("GATE WOULD PASS") and "fixtures=2" in text
    write_json(stage_ctx.path(prerequisites.DOC_OUT), _with(no_prerequisites=[]))
    code, text = check_prerequisites.report(stage_ctx.run_dir)
    assert code == 1 and all(f"  - {f}" in text for f in prerequisites.STAGE.gate(stage_ctx))


def test_the_checker_names_a_missing_file(tmp_path):
    code, text = check_prerequisites.report(tmp_path)
    assert code == 2 and "qa/cases.json" in text


def test_the_counts_become_scores(stage_ctx):
    write_json(stage_ctx.path(prerequisites.DOC_OUT), DOC)
    scores = run_scores(stage_ctx, {"prerequisites"})
    assert scores["prereq_fixtures"]["value"] == 2.0 and scores["prereq_secrets"]["value"] == 1.0
    assert "prereq_fixtures" not in run_scores(stage_ctx, {"store_cases"})


def test_the_stage_is_registered_after_the_upload():
    pipe = WORKFLOW.default_pipeline
    assert pipe.index("prerequisites") == pipe.index("store_cases") + 1
    assert WORKFLOW.stage("prerequisites") is prerequisites.STAGE
