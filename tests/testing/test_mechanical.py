"""The evaluator's deterministic half.

The evaluator SKILL calls its presence check "the one thing that's never subjective" (SKILL.md:29) and then
asks a model to do it. These tests pin the Python version against the fixture suite, one violation per
gate, plus the clean suite as the control that proves no gate is simply always red.
"""

from __future__ import annotations

from workflows.testing import mechanical


# ---- the Deterministic Eval (Presence Check), SKILL.md:17-29 -----------------------------------------
def test_presence_check_passes_on_the_clean_suite(clean_cases, clean_scenarios):
    presence = mechanical.presence_checks(clean_cases, clean_scenarios)
    assert presence["expected_result"]["status"] == "PASS", presence["expected_result"]["violations"]
    assert presence["precondition"]["status"] == "PASS", presence["precondition"]["violations"]


def test_an_empty_expected_result_is_flagged(sample_cases, sample_scenarios):
    v = mechanical.presence_checks(sample_cases, sample_scenarios)["expected_result"]
    assert v["status"] == "FAIL"
    assert any("TC-RSE-SLOT-FUNC-02: no Expected Result" in x for x in v["violations"])


def test_a_restated_action_is_not_an_expected_result(sample_cases, sample_scenarios):
    v = mechanical.presence_checks(sample_cases, sample_scenarios)["expected_result"]
    assert any("TC-RSE-SLOT-FUNC-03" in x and "restates the action" in x for x in v["violations"])


def test_an_expected_result_copied_from_the_steps_is_flagged():
    case = {"id": "TC-A-M-FUNC-01", "steps": "1. Submit the form and the record is saved",
            "expected": "the record is saved", "precondition": "n/a"}
    v = mechanical.presence_checks([case], [])["expected_result"]
    assert any("copy of the steps" in x for x in v["violations"])


def test_implied_setup_without_a_precondition_is_flagged(sample_cases, sample_scenarios):
    v = mechanical.presence_checks(sample_cases, sample_scenarios)["precondition"]
    assert v["status"] == "FAIL"
    assert any("TC-RSE-SLOT-VALID-01" in x and "logged in" in x for x in v["violations"])


def test_an_accurate_na_precondition_is_not_a_violation():
    """"A row that genuinely needs no setup is not a violation" (SKILL.md:28)."""
    case = {"id": "TC-A-M-FUNC-01", "steps": "1. Send GET /ping\n2. Assert status == 200",
            "expected": "The service answers 200 with an empty body.", "precondition": "n/a"}
    assert mechanical.presence_checks([case], [])["precondition"]["status"] == "PASS"


def test_the_sweep_covers_scenarios_too():
    """Both sheets are in scope, "not just Test Cases" (SKILL.md:19)."""
    scenario = {"id": "TS-A-M-FUNC-01", "description": "x", "expected": "", "precondition": "n/a"}
    v = mechanical.presence_checks([], [scenario])["expected_result"]
    assert v["violations"] == ["TS-A-M-FUNC-01: no Expected Result/Outcome"]


# ---- gate 1: traceable requirement reference ---------------------------------------------------------
def test_gate_1_passes_when_every_case_cites_a_requirement(clean_cases):
    assert mechanical.gate_traceable_requirement(clean_cases)["status"] == "PASS"


def test_gate_1_flags_the_case_with_no_reference(sample_cases):
    v = mechanical.gate_traceable_requirement(sample_cases)
    assert v["status"] == "FAIL"
    assert [x.split(":")[0] for x in v["violations"]] == ["TC-RSE-SLOT-FUNC-04"]


def test_gate_1_accepts_a_ticket_key_or_a_contract_citation():
    for ref in ("TASK-29354", "AC-3", "cart.py:118", "REQ 12"):
        case = {"id": "TC-A-M-FUNC-01", "requirement": ref, "qa_comment": "", "title": "", "expected": ""}
        assert mechanical.gate_traceable_requirement([case])["status"] == "PASS", ref


# ---- gate 2 (mechanical half): structural completeness ------------------------------------------------
def test_gate_2_passes_on_the_clean_suite(clean_cases):
    v = mechanical.gate_structural_completeness(clean_cases)
    assert v["status"] == "PASS", v["violations"]


def test_gate_2_flags_the_empty_field_and_the_vague_navigation(sample_cases):
    v = mechanical.gate_structural_completeness(sample_cases)
    joined = "\n".join(v["violations"])
    assert "TC-RSE-SLOT-FUNC-02: expected is empty" in joined
    assert "TC-RSE-SLOT-FUNC-05" in joined and "names no URL/route/deep link" in joined


def test_gate_2_accepts_a_navigation_step_that_names_a_deep_link():
    case = {"id": "TC-A-M-FUNC-01", "steps": "1. Open diagno://radiology/slots", "qa_comment": "REQ-1"}
    filled = {k: "x" for k in mechanical.CASE_FIELDS}
    v = mechanical.gate_structural_completeness([{**filled, **case}])
    assert not any("URL/route" in x for x in v["violations"])


def test_gate_2_accepts_a_vague_navigation_step_that_flags_the_missing_url():
    """"...with no QA Comment explaining that the exact URL/route was unavailable" (gate 2's own wording)."""
    filled = {k: "x" for k in mechanical.CASE_FIELDS}
    case = {**filled, "id": "TC-A-M-FUNC-01", "steps": "1. Navigate to the settings page",
            "qa_comment": "exact route not stated in the PRD; url unavailable"}
    assert not any("URL/route" in x for x in mechanical.gate_structural_completeness([case])["violations"])


def test_gate_2_flags_a_placeholder_as_well_as_a_blank():
    filled = {k: "x" for k in mechanical.CASE_FIELDS}
    v = mechanical.gate_structural_completeness([{**filled, "id": "TC-A-M-FUNC-01", "steps": "TBD"}])
    assert any("steps is a placeholder" in x for x in v["violations"])


def test_gate_2_rejects_na_in_a_field_where_it_cannot_be_accurate():
    filled = {k: "x" for k in mechanical.CASE_FIELDS}
    v = mechanical.gate_structural_completeness([{**filled, "id": "TC-A-M-FUNC-01", "expected": "n/a"}])
    assert any("expected is 'n/a'" in x for x in v["violations"])


# ---- gate 4: surface coverage -------------------------------------------------------------------------
def test_gate_4_flags_a_declared_surface_no_case_covers():
    scenarios = [{"id": "TS-A-M-FUNC-01", "surface": "ios"}]
    cases = [{"id": "TC-A-M-FUNC-01", "surface": "backend"}]
    v = mechanical.gate_surface_coverage(cases, scenarios)
    assert v["violations"] == ["surface 'ios' is declared on a scenario but no test case covers it"]


def test_gate_4_passes_when_every_declared_surface_is_covered(clean_cases, clean_scenarios):
    assert mechanical.gate_surface_coverage(clean_cases, clean_scenarios)["status"] == "PASS"


# ---- gate 8: duplicate consolidation ------------------------------------------------------------------
def test_gate_8_flags_the_identical_title_on_the_same_surface(sample_cases):
    v = mechanical.gate_duplicate_consolidation(sample_cases)
    assert v["status"] == "FAIL"
    assert any("TC-RSE-CAP-FUNC-01, TC-RSE-CAP-FUNC-03" in x for x in v["violations"])


def test_gate_8_does_not_flag_the_same_title_on_a_different_surface():
    """"cases differing by surface or by distinct condition are NOT duplicates — do not flag those"."""
    cases = [{"id": "TC-A-M-FUNC-01", "title": "Same title", "surface": "web"},
             {"id": "TC-A-M-FUNC-02", "title": "Same title", "surface": "ios"}]
    assert mechanical.gate_duplicate_consolidation(cases)["status"] == "PASS"


# ---- the whole scorecard -------------------------------------------------------------------------------
def test_the_clean_suite_is_a_mechanical_pass(clean_cases, clean_scenarios):
    result = mechanical.evaluate(clean_cases, clean_scenarios)
    assert result["compliance_status"] == "PASS", result


def test_the_sample_suite_fires_every_gate_it_was_built_to_fire(sample_cases, sample_scenarios):
    result = mechanical.evaluate(sample_cases, sample_scenarios)
    assert result["compliance_status"] == "FAIL"
    failed = {name for name, v in result["mechanical_checks"].items() if v["status"] == "FAIL"}
    assert failed == {
        "traceable_requirement_reference", "structural_completeness", "duplicate_consolidation",
        "id_convention", "scenario_case_linkage", "negative_and_edge_per_module",
    }
    assert result["mechanical_checks"]["surface_coverage"]["status"] == "PASS"
    assert {k: v["status"] for k, v in result["presence_checks"].items()} == {
        "expected_result": "FAIL", "precondition": "FAIL"}


def test_the_scorecard_counts_are_reported(sample_cases, sample_scenarios):
    counts = mechanical.evaluate(sample_cases, sample_scenarios)["counts"]
    assert counts["cases"] == len(sample_cases)
    assert counts["scenarios"] == len(sample_scenarios)
    assert counts["http_cases"] == 5
    assert counts["modules"] == 4  # SLOT, CAP, BAD, REP


def test_every_violation_names_an_id(sample_cases, sample_scenarios):
    """A gate failure becomes the model's guidance; "some case is wrong" is unactionable."""
    result = mechanical.evaluate(sample_cases, sample_scenarios)
    ids = {c["id"] for c in sample_cases} | {s["id"] for s in sample_scenarios}
    for name, verdict in {**result["mechanical_checks"], **result["presence_checks"]}.items():
        for violation in verdict["violations"]:
            assert any(i in violation for i in ids) or violation.startswith("module "), \
                f"{name}: {violation!r} names no id"
