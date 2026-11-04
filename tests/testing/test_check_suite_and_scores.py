"""The model-runnable gate (`check_suite`), the testing workflow's trace scores, and how its traces are named.

From the NR-DIGICHEM run (2026-11-02): 13 hand-rolled jq checks (4 denied) after the suite was written,
a trace named `rfc-run` stamped `harness_stage: plan`, and no score that told a 4/5 suite from a 2/5 one.
"""

from __future__ import annotations

import json

from workflows.testing import WORKFLOW, check_suite
from workflows.testing.scores import run_scores
from workflows.testing.stages import generate_cases, write_json


# ---- check_suite: the gate, one call ----------------------------------------------------------------
def test_a_clean_suite_reports_that_the_gate_would_pass(sourced_ctx):
    code, text = check_suite.report(sourced_ctx.run_dir)
    assert code == 0 and text.endswith("GATE WOULD PASS")
    assert "6 cases, 6 scenarios" in text and "execution:" in text and "requirements cited" in text


def test_it_says_exactly_what_the_gate_says(sourced_ctx, sample_cases, sample_scenarios):
    write_json(sourced_ctx.path("qa/cases.json"), sample_cases)
    write_json(sourced_ctx.path("qa/scenarios.json"), sample_scenarios)
    code, text = check_suite.report(sourced_ctx.run_dir)
    gate = generate_cases.STAGE.gate(sourced_ctx)
    assert code == 1 and gate
    assert all(f"  - {f}" in text for f in gate)


def test_a_missing_file_is_named(tmp_path):
    code, text = check_suite.report(tmp_path)
    assert code == 2 and "qa/cases.json" in text


def test_the_command_is_allowlisted_for_the_generator_and_named_in_its_prompt(sourced_ctx):
    assert generate_cases.CHECK_SUITE in generate_cases.STAGE.allow_commands
    prompt = generate_cases.STAGE.build_prompt(sourced_ctx)
    assert generate_cases.CHECK_SUITE in prompt and "Do not hand-roll jq" in prompt


def test_an_allowed_command_becomes_a_prefix_rule_in_the_child_settings(tmp_path):
    from rfc_agent.engine import _write_guard_settings

    s = json.loads(_write_guard_settings(tmp_path, tmp_path / "s.json",
                                         allow_commands=("/py /x/check_suite.py",)).read_text())
    assert "Bash(/py /x/check_suite.py:*)" in s["permissions"]["allow"]


# ---- trace scores -----------------------------------------------------------------------------------
EVAL = {"overall_score": 4.0, "coverage_score": 4.0, "quality_score": 3.5, "compliance_status": "fail",
        "missing_scenarios": [{}, {}], "incorrect_cases": [], "duplicate_cases": [{}],
        "dimension_scores": {"negative_scenario_coverage": 3}}


def test_the_evaluation_and_suite_shape_become_scores(sourced_ctx):
    write_json(sourced_ctx.path("qa/eval.json"), EVAL)
    scores = run_scores(sourced_ctx, None)
    assert scores["eval_overall_score"]["value"] == 4.0
    assert scores["eval_compliance"] == {"value": "FAIL", "data_type": "CATEGORICAL"}
    assert scores["eval_missing_scenarios"]["value"] == 2.0
    assert scores["eval_dim_negative_scenario_coverage"]["value"] == 3.0
    assert scores["cases_total"]["value"] == 6.0
    assert {"cases_http", "cases_ui", "cases_manual"} <= scores.keys()


def test_a_resume_does_not_re_score_stages_it_did_not_run(sourced_ctx):
    write_json(sourced_ctx.path("qa/eval.json"), EVAL)
    scores = run_scores(sourced_ctx, {"store_cases"})
    assert not any(k.startswith(("eval_", "cases_")) for k in scores)


def test_the_workflow_hands_its_scores_to_the_trace(sourced_ctx, monkeypatch):
    from rfc_agent import obs

    sent = {}

    class _FakeClient:
        def set_current_trace_io(self, **kw): pass
        def score_current_trace(self, **kw): sent[kw["name"]] = kw

    write_json(sourced_ctx.path("qa/eval.json"), EVAL)
    monkeypatch.setattr(obs, "_get_client", lambda ctx: _FakeClient())
    assert WORKFLOW.run_scores is run_scores
    obs.finish_run(sourced_ctx, {"evaluate": {"status": "ok"}}, ran=None)
    assert sent["eval_overall_score"]["value"] == 4.0
    assert sent["eval_compliance"]["data_type"] == "CATEGORICAL"


# ---- trace naming -----------------------------------------------------------------------------------
def test_a_testing_run_is_named_and_staged_as_one(sourced_ctx):
    from rfc_agent import identity, obs

    assert identity.trace_name(sourced_ctx) == "testing-run"
    assert identity.stage_of(sourced_ctx) == "test"
    assert "workflow:testing" in obs._run_tags(sourced_ctx, {"workstream_source": "label"})


def test_an_rfc_run_keeps_its_name_stage_and_tags(tmp_path):
    from harness.contracts import RunContext
    from rfc_agent import identity, obs

    for wf in ("", "rfc"):
        ctx = RunContext(run_dir=tmp_path, inputs={}, config=None, workflow=wf)
        assert identity.trace_name(ctx) == "rfc-run" and identity.stage_of(ctx) == "plan"
        assert obs._run_tags(ctx, {"workstream_source": "label"}) == []


# ---- the hook must let the declared command through (2026-11-02: denied 3x as an "ad-hoc interpreter") --
def _hook(cmd, allowed, monkeypatch, capsys):
    import io
    import sys

    from rfc_agent.hooks import approve_readonly

    monkeypatch.setenv("RFC_AGENT_RUN_DIR", "/tmp/run")
    monkeypatch.setenv("RFC_AGENT_ALLOWED_COMMANDS", allowed)
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({"tool_name": "Bash",
                                                              "tool_input": {"command": cmd}})))
    try:
        approve_readonly.main()
    except SystemExit:
        pass
    out = capsys.readouterr().out
    return json.loads(out)["hookSpecificOutput"]["permissionDecision"] if out else None


def test_the_declared_command_is_allowed_with_plain_arguments(monkeypatch, capsys):
    cmd = generate_cases.CHECK_SUITE
    assert _hook(cmd, cmd, monkeypatch, capsys) == "allow"
    assert _hook(cmd + " /some/run --allow-unrealised", cmd, monkeypatch, capsys) == "allow"


def test_it_cannot_be_extended_into_a_second_command(monkeypatch, capsys):
    cmd = generate_cases.CHECK_SUITE
    for bad in (cmd + "; rm -rf x", cmd + " && echo hi", cmd + " | tee x", cmd + " > x", cmd + " $(id)",
                cmd + "x.py", "/usr/bin/python3 -c 1"):
        # never "allow"; `$(...)` gets no verdict at all, which leaves it to the CLI's own headless deny
        assert _hook(bad, cmd, monkeypatch, capsys) != "allow", bad


def test_without_a_declaration_the_interpreter_stays_denied(monkeypatch, capsys):
    assert _hook(generate_cases.CHECK_SUITE, "", monkeypatch, capsys) == "deny"
