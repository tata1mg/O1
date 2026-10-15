"""The schema validators — every rule the generator SKILL states as prose, now checkable.

Each test names the SKILL.md rule it mechanises. Together with the fixture suite these are the gate
`generate_cases` runs, so a rule that stops being enforced fails here rather than in production.
"""

from __future__ import annotations

import pytest

from workflows.testing import schema


# ---- the ID convention (SKILL.md:39-68) --------------------------------------------------------------
@pytest.mark.parametrize("cid", [
    "TC-FH-ADDFAM-FUNC-01",     # the SKILL's own example
    "TS-FH-ADDFAM-NEG-01",
    "TC-FH-ENTRY-EDGE-02",
    "TC-LABS26734-CART-API-11",  # a full epic key is allowed; a bare project prefix is not the issue
])
def test_well_formed_ids_match(cid):
    assert schema.ID_RE.match(cid)


@pytest.mark.parametrize("cid,why", [
    ("TC-ADDFAM-01", "legacy 3-segment id"),
    ("TC-FH-ADDFAM-FUNC-1", "sequence is not zero-padded to two digits"),
    ("TC-FH-ADDFAM-SMOKE-01", "SMOKE is not in the TYPE table"),
    ("TX-FH-ADDFAM-FUNC-01", "prefix is neither TS- nor TC-"),
    ("TC-fh-ADDFAM-FUNC-01", "FEATURE must be upper case"),
    ("TC-FH-ADDFAM-FUNC-011", "three-digit sequence"),
])
def test_malformed_ids_do_not_match(cid, why):
    assert not schema.ID_RE.match(cid), why


def test_every_type_code_in_the_skill_table_is_accepted():
    """The 14-code table is the vocabulary; a code outside it must fail the regex."""
    assert len(schema.TYPE_ENUM) == 14
    for code in schema.TYPE_ENUM:
        assert schema.ID_RE.match(f"TC-FH-MOD-{code}-01")


def test_type_code_maps_scenario_type_names():
    assert schema.type_code("Edge Case") == "EDGE"
    assert schema.type_code("State Handling") == "STATE"
    assert schema.type_code("Smoke") is None


def test_id_segment_helpers():
    assert schema.id_type("TC-FH-ADDFAM-EDGE-02") == "EDGE"
    assert schema.id_module("TC-FH-ADDFAM-EDGE-02") == "ADDFAM"
    assert schema.id_type("TC-ADDFAM-01") is None


# ---- linkage is a pure prefix swap (SKILL.md:66) -----------------------------------------------------
def test_swap_prefix_round_trips():
    assert schema.swap_prefix("TC-FH-A-FUNC-01") == "TS-FH-A-FUNC-01"
    assert schema.swap_prefix(schema.swap_prefix("TC-FH-A-FUNC-01")) == "TC-FH-A-FUNC-01"


def test_a_case_pointing_at_another_scenario_is_a_violation(clean_cases):
    case = {**clean_cases[0], "scenario_id": "TS-RSE-CAP-FUNC-01"}
    violations = schema.validate_case(case)
    assert any("pure prefix swap" in v for v in violations)


# ---- mandatory fields ("n/a" allowed, blank never — SKILL.md:101) ------------------------------------
def test_a_blank_mandatory_field_is_a_violation(clean_cases):
    case = {**clean_cases[0], "expected": "   "}
    assert any("'expected' is blank" in v for v in schema.validate_case(case))


def test_an_na_value_is_not_blank(clean_cases):
    """An explicit 'n/a' is populated; an empty cell is not. The schema check accepts 'n/a'."""
    case = {**clean_cases[0], "test_data": "n/a"}
    assert not any("test_data" in v for v in schema.validate_case(case))


def test_a_missing_key_is_named(clean_cases):
    case = {k: v for k, v in clean_cases[0].items() if k != "surface"}
    assert any("missing field 'surface'" in v for v in schema.validate_case(case))


# ---- enums (SKILL.md:94-98) ---------------------------------------------------------------------------
@pytest.mark.parametrize("field,bad,enum_name", [
    ("execution", "api", "execution"),
    ("surface", "mweb", "surface"),
    ("status", "Pass", "status"),
    ("priority", "P1", "priority"),
    ("type", "Smoke", "type"),
])
def test_out_of_enum_values_are_violations(clean_cases, field, bad, enum_name):
    case = {**clean_cases[0], field: bad}
    assert any(f"{enum_name}={bad!r}" in v for v in schema.validate_case(case))


def test_type_must_agree_with_the_ids_type_segment(clean_cases):
    """A scenario and its case always carry the same TYPE code — so must the `type` column."""
    case = {**clean_cases[0], "type": "Negative"}
    assert any("implies TYPE code NEG" in v for v in schema.validate_case(case))


# ---- cross-sheet invariants ----------------------------------------------------------------------------
def test_the_clean_suite_passes_every_validator(clean_cases, clean_scenarios):
    assert schema.validate_suite(clean_cases, clean_scenarios) == []


def test_the_sample_suite_fails_the_expected_rules(sample_cases, sample_scenarios):
    failures = schema.validate_suite(sample_cases, sample_scenarios)
    joined = "\n".join(failures)
    assert "TC-RSE-BAD-FUNC-1: id does not match" in joined
    assert "TC-RSE-SLOT-FUNC-02: 'expected' is blank" in joined
    assert "TC-RSE-SLOT-FUNC-04" in joined and "pure prefix swap" in joined
    assert "TS-RSE-SLOT-REGR-01: scenario has no test case" in joined
    assert "module 'REP'" in joined and "no NEG and no EDGE scenario" in joined


def test_orphan_scenarios_can_be_allowed_by_flag(sample_cases, sample_scenarios):
    """The SKILL's analytics-only exception is prose and cannot be detected mechanically, so it is a
    flag rather than a silent hole."""
    strict = schema.validate_suite(sample_cases, sample_scenarios, require_case_per_scenario=True)
    lax = schema.validate_suite(sample_cases, sample_scenarios, require_case_per_scenario=False)
    assert any("has no test case" in v for v in strict)
    assert not any("has no test case" in v for v in lax)


def test_duplicate_ids_are_caught(clean_cases, clean_scenarios):
    failures = schema.validate_linkage([*clean_cases, clean_cases[0]], clean_scenarios)
    assert any("duplicate test case id" in v for v in failures)


def test_a_case_linking_to_an_unknown_scenario_is_caught(clean_cases):
    case = {**clean_cases[0], "id": "TC-RSE-SLOT-FUNC-09", "scenario_id": "TS-RSE-SLOT-FUNC-09"}
    failures = schema.validate_linkage([case], [])
    assert any("is not in scenarios.json" in v for v in failures)


def test_neg_and_edge_are_required_per_module():
    scenarios = [{"id": "TS-A-M-FUNC-01", "module": "M", "type": "Functional"}]
    failures = schema.validate_neg_edge_per_module(scenarios)
    assert failures == ["module 'M': no NEG and no EDGE scenario — a module with only happy-path "
                        "coverage is incomplete"]


def test_an_empty_suite_is_a_violation_not_a_pass():
    assert schema.validate_suite([], []) [:2] == [
        "cases.json is empty — the suite has no test cases",
        "scenarios.json is empty — the suite has no scenarios",
    ]


# ---- the store transport --------------------------------------------------------------------------------
def test_render_text_sections_match_kiwi_writers_shape(clean_cases):
    text = schema.render_text(clean_cases[0])
    assert text.startswith("**Precondition**")
    for header in ("**Test Steps**", "**Test Data**", "**Expected Result**"):
        assert header in text
    # the steps survive as numbered lines, which is all be_spec.parse_case reads
    assert "1. Send GET /radiology/v1/slots?lab_id=42" in text


def test_case_tags_carry_exec_and_surface(clean_cases):
    tags = schema.case_tags(clean_cases[0], "RSE-1")
    assert tags == ["extid:TC-RSE-SLOT-FUNC-01", "feature:RSE-1",
                    "scenario:TS-RSE-SLOT-FUNC-01", "surface:backend", "exec:http"]


def test_case_tags_omit_exec_when_the_generator_did_not_classify(clean_cases):
    """Absent is not "runs by default" — it is "never runs and nobody notices"."""
    tags = schema.case_tags({**clean_cases[0], "execution": ""}, "RSE-1")
    assert not any(t.startswith("exec:") for t in tags)


def test_http_cases_selects_only_the_executable_subset(sample_cases):
    ids = [c["id"] for c in schema.http_cases(sample_cases)]
    assert ids == ["TC-RSE-SLOT-FUNC-01", "TC-RSE-SLOT-NEG-01", "TC-RSE-CAP-FUNC-01",
                   "TC-RSE-CAP-FUNC-02", "TC-RSE-CAP-FUNC-03"]
