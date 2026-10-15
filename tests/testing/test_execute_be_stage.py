"""The `execute_be` stage: the run loop, the results file, and the exit semantics.

A failed preflight is an `EngineFailure` and an unrunnable suite is a `GateFailure` — the two meanings the
harness already has (exit 3 = the machine could not do the work; exit 2 = the work was done and is wrong).
Every request is mocked: nothing here leaves the machine.
"""

from __future__ import annotations

import json
import unittest.mock as mock

import pytest

from harness.contracts import EngineFailure, GateFailure
from workflows.testing import be_execute
from workflows.testing.stages import execute_be, store_cases, write_json

CURL = "curl 'https://svc.test/anything' -H 'x-client: web' -b 'sid=abc'"


@pytest.fixture
def be_ctx(tmp_path, ctx_factory, clean_cases):
    """A run dir at the point derive_spec has finished, plus a captured curl on disk."""
    curl = tmp_path / "curl.txt"
    curl.write_text(CURL)
    c = ctx_factory(tmp_path / "run",
                    inputs={"feature": "RSE-1", "curl": str(curl), "env": "staging", "build": "b1"})
    write_json(c.path("qa/cases.json"), clean_cases)
    store_cases.STAGE.engine(store_cases.STAGE, c)
    write_json(c.path("qa/spec.json"), [
        {"extid": "TC-RSE-SLOT-FUNC-01", "path": "/radiology/v1/slots?lab_id=42", "method": "GET",
         "expect_status": 200, "assert": [{"path": "slots.0.remaining_capacity", "equals": 1}]},
        {"extid": "TC-RSE-SLOT-NEG-01", "path": "/radiology/v1/slots?lab_id=999999", "method": "GET",
         "expect_status": 404, "assert": [{"path": "error", "equals": "lab_not_found"}]},
    ])
    return c


def responder(by_path):
    """Fake subprocess.run: answer per requested path, defaulting to a 200 with an empty object."""
    def fake_run(cmd, **kw):
        url = next(p for p in cmd if str(p).startswith("http"))
        payload, code = by_path.get(url.split("svc.test", 1)[-1], ({}, "200"))
        body = json.dumps(payload) if payload is not None else "<html>"

        class R:
            stdout = f"{body}\n__STATUS__{code}"
        return R()
    return mock.patch.object(be_execute.subprocess, "run", fake_run)


HEALTHY = {
    "/radiology/v1/slots?lab_id=42": ({"slots": [{"remaining_capacity": 1}]}, "200"),
    "/radiology/v1/slots?lab_id=999999": ({"error": "lab_not_found"}, "404"),
}


def run_stage(ctx):
    result = execute_be.STAGE.engine(execute_be.STAGE, ctx)
    return result, execute_be.STAGE.gate(ctx)


# ---- the happy path ----------------------------------------------------------------------------------
def test_every_case_is_executed_and_recorded_locally(be_ctx):
    with responder(HEALTHY):
        result, failures = run_stage(be_ctx)
    assert failures == []
    data = json.loads(be_ctx.path("qa/results.json").read_text())
    assert data["tally"] == {"PASS": 1, "BLOCKED": 1}   # a 404 is BLOCKED, never FAIL
    assert {r["extid"] for r in data["results"]} == {"TC-RSE-SLOT-FUNC-01", "TC-RSE-SLOT-NEG-01"}
    for row in data["results"]:
        assert set(row) == {"extid", "path", "status", "detail", "http_status", "duration_s"}
    assert "PASS=1" in result.final_text


def test_results_are_also_recorded_in_the_store(be_ctx):
    """Results are written to both sinks: the run file and the store."""
    with responder(HEALTHY):
        run_stage(be_ctx)
    run_file = next((be_ctx.run_dir / "store" / "runs").rglob("*.json"))
    executions = json.loads(run_file.read_text())["executions"]
    assert executions["TC-RSE-SLOT-FUNC-01"]["status"] == "PASS"
    assert executions["TC-RSE-SLOT-FUNC-01"]["props"]["environment"] == "staging"
    assert executions["TC-RSE-SLOT-FUNC-01"]["props"]["endpoint"] == "/radiology/v1/slots?lab_id=42"


def test_the_cohort_property_is_only_recorded_when_a_path_is_configured(be_ctx, tmp_path, ctx_factory,
                                                                       clean_cases):
    """`cohort_observed` is recorded only when a cohort path is configured. Absent flag -> the property is not
    written at all."""
    with responder(HEALTHY):
        run_stage(be_ctx)
    run_file = next((be_ctx.run_dir / "store" / "runs").rglob("*.json"))
    props = json.loads(run_file.read_text())["executions"]["TC-RSE-SLOT-FUNC-01"]["props"]
    assert "cohort_observed" not in props

    c = ctx_factory(tmp_path / "run2",
                    inputs={**be_ctx.inputs}, flags={"testing.cohort_path": "slots.0.remaining_capacity"})
    write_json(c.path("qa/cases.json"), clean_cases)
    store_cases.STAGE.engine(store_cases.STAGE, c)
    write_json(c.path("qa/spec.json"), json.loads(be_ctx.path("qa/spec.json").read_text()))
    with responder(HEALTHY):
        run_stage(c)
    run_file = next((c.run_dir / "store" / "runs").rglob("*.json"))
    props = json.loads(run_file.read_text())["executions"]["TC-RSE-SLOT-FUNC-01"]["props"]
    assert props["cohort_observed"] == 1


def test_a_route_map_rewrite_is_applied_and_recorded(be_ctx, tmp_path):
    rmap = tmp_path / "map.json"
    rmap.write_text(json.dumps({"/radiology/v1/slots": "/edge/v2/slots?tenant=1"}))
    be_ctx.inputs["route_map"] = str(rmap)
    responses = {"/edge/v2/slots?tenant=1&lab_id=42": ({"slots": [{"remaining_capacity": 1}]}, "200"),
                 "/edge/v2/slots?tenant=1&lab_id=999999": ({"error": "lab_not_found"}, "404")}
    with responder(responses):
        _, failures = run_stage(be_ctx)
    assert failures == []
    data = json.loads(be_ctx.path("qa/results.json").read_text())
    assert data["rewrites"][0]["to"] == "/edge/v2/slots?tenant=1"
    lines = [json.loads(x) for x in be_ctx.path("logs/decisions.jsonl").read_text().splitlines()]
    assert any(x["decision"] == "route_rewritten" for x in lines)


# ---- exit semantics ----------------------------------------------------------------------------------
def test_a_failed_preflight_is_an_engine_failure_and_records_nothing(be_ctx):
    """A failed preflight creates no run and records nothing."""
    with responder({"/radiology/v1/slots?lab_id=42": (None, "503")}):
        with pytest.raises(EngineFailure, match="preflight failed"):
            execute_be.STAGE.engine(execute_be.STAGE, be_ctx)
    assert not be_ctx.path("qa/results.json").exists()
    assert not (be_ctx.run_dir / "store" / "runs").exists()


def test_preflight_tolerates_blocked_and_404(be_ctx):
    """Only ERROR is fatal. The question is reachability, not correctness."""
    responses = {"/radiology/v1/slots?lab_id=42": ({"error": "nope"}, "404"),
                 "/radiology/v1/slots?lab_id=999999": ({"error": "lab_not_found"}, "404")}
    with responder(responses):
        _, failures = run_stage(be_ctx)
    assert failures == []
    assert json.loads(be_ctx.path("qa/results.json").read_text())["tally"] == {"BLOCKED": 2}


def test_nothing_runnable_is_a_gate_failure(be_ctx, clean_cases):
    """Nothing runnable means the suite is wrong, not the machine — so it is a gate failure."""
    write_json(be_ctx.path("qa/cases.json"), [{**c, "execution": "manual"} for c in clean_cases])
    store_cases.STAGE.engine(store_cases.STAGE, be_ctx)
    with responder(HEALTHY):
        with pytest.raises(GateFailure, match="nothing to run"):
            execute_be.STAGE.engine(execute_be.STAGE, be_ctx)
    data = json.loads(be_ctx.path("qa/results.json").read_text())
    assert data["results"] == []
    assert any("exec:manual" in s for s in data["skipped_wrong_execution"])


def test_an_empty_spec_is_a_gate_failure(be_ctx):
    write_json(be_ctx.path("qa/spec.json"), [])
    with pytest.raises(GateFailure, match="qa/spec.json is empty"):
        execute_be.STAGE.engine(execute_be.STAGE, be_ctx)


def test_a_spec_entry_with_no_runnable_case_is_skipped_loudly(be_ctx):
    spec = json.loads(be_ctx.path("qa/spec.json").read_text())
    spec.append({"extid": "TC-RSE-SLOT-FUNC-99", "path": "/radiology/v1/slots?lab_id=1",
                 "expect_status": 200})
    write_json(be_ctx.path("qa/spec.json"), spec)
    with responder(HEALTHY):
        _, failures = run_stage(be_ctx)
    assert failures == []
    lines = [json.loads(x) for x in be_ctx.path("logs/decisions.jsonl").read_text().splitlines()]
    assert any(x["decision"] == "case_not_attached" and x["extid"] == "TC-RSE-SLOT-FUNC-99"
               for x in lines)


# ---- the missing-input errors name the fix ------------------------------------------------------------
def test_a_missing_curl_names_what_to_pass(be_ctx):
    be_ctx.inputs["curl"] = ""
    with pytest.raises(EngineFailure, match="pass --curl"):
        execute_be.STAGE.engine(execute_be.STAGE, be_ctx)


def test_a_curl_with_no_url_is_rejected(be_ctx, tmp_path):
    bad = tmp_path / "bad.txt"
    bad.write_text("curl -H 'a: b'")
    be_ctx.inputs["curl"] = str(bad)
    with pytest.raises(EngineFailure, match="no http\\(s\\) URL found"):
        execute_be.STAGE.engine(execute_be.STAGE, be_ctx)


def test_missing_env_or_build_is_rejected_before_any_request(be_ctx):
    be_ctx.inputs["build"] = ""
    with pytest.raises(EngineFailure, match="needs --env and --build"):
        execute_be.STAGE.engine(execute_be.STAGE, be_ctx)


# ---- dry run and offline ------------------------------------------------------------------------------
def test_dry_run_executes_but_writes_nothing_to_the_store(tmp_path, ctx_factory, be_ctx):
    c = ctx_factory(tmp_path / "dry", inputs={**be_ctx.inputs}, flags={"testing.dry_run": True})
    write_json(c.path("qa/spec.json"), json.loads(be_ctx.path("qa/spec.json").read_text()))
    with responder(HEALTHY):
        _, failures = run_stage(c)
    assert failures == []
    assert json.loads(c.path("qa/results.json").read_text())["tally"] == {"PASS": 1, "BLOCKED": 1}
    assert not (c.run_dir / "store").exists()


def test_offline_issues_no_request_at_all(tmp_path, ctx_factory, be_ctx):
    """Offline mode reads no store and fires no preflight."""
    c = ctx_factory(tmp_path / "off", inputs={**be_ctx.inputs}, flags={"testing.offline": True})
    write_json(c.path("qa/spec.json"), json.loads(be_ctx.path("qa/spec.json").read_text()))

    def boom(*a, **kw):
        raise AssertionError("offline mode must issue no request")

    with mock.patch.object(be_execute.subprocess, "run", boom):
        result, failures = run_stage(c)
    assert failures == []
    data = json.loads(c.path("qa/results.json").read_text())
    assert data["offline"] is True and data["results"] == []
    assert "issued no requests" in result.final_text


def test_offline_still_validates_the_spec_and_the_envelope(tmp_path, ctx_factory, be_ctx):
    c = ctx_factory(tmp_path / "off2", inputs={**be_ctx.inputs}, flags={"testing.offline": True})
    write_json(c.path("qa/spec.json"), [])
    with pytest.raises(GateFailure):
        execute_be.STAGE.engine(execute_be.STAGE, c)


# ---- the gate ------------------------------------------------------------------------------------------
def test_the_gate_rejects_a_results_row_missing_a_field(be_ctx):
    write_json(be_ctx.path("qa/results.json"),
               {"results": [{"extid": "TC-A", "status": "PASS"}], "tally": {"PASS": 1}})
    failures = execute_be.STAGE.gate(be_ctx)
    assert any("missing 'detail'" in f for f in failures)


def test_the_gate_rejects_an_unknown_status(be_ctx):
    write_json(be_ctx.path("qa/results.json"),
               {"results": [{"extid": "TC-A", "status": "SKIPPED", "detail": "", "http_status": "",
                             "duration_s": 0}], "tally": {"SKIPPED": 1}})
    assert any("is not PASS/FAIL/BLOCKED/ERROR" in f for f in execute_be.STAGE.gate(be_ctx))


def test_the_gate_rejects_a_tally_that_does_not_match_the_rows(be_ctx):
    write_json(be_ctx.path("qa/results.json"),
               {"results": [{"extid": "TC-A", "status": "PASS", "detail": "ok", "http_status": "200",
                             "duration_s": 0.1}], "tally": {"PASS": 3}})
    assert any("tally sums to 3 but there are 1 results" in f for f in execute_be.STAGE.gate(be_ctx))


def test_execute_be_is_not_in_the_default_pipeline():
    """It issues real requests with a captured identity, so it is opt-in."""
    from workflows.testing import WORKFLOW

    assert "execute_be" not in WORKFLOW.default_pipeline
    assert "execute_be" in [s.name for s in WORKFLOW.stages]
