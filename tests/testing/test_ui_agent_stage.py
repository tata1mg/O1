"""The `execute_ui_agent` stage: one agent and one browser per case, in batches, each verdict held to its
evidence and to the session's trace, then recorded in the store.

No browser and no model here: `AgentEngine` stands in for the session and writes what an agent would.
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import pytest

from harness.contracts import EngineFailure
from rfc_agent.hooks import guard_browser
from workflows.testing import compose_pipeline, ui_agent
from workflows.testing.schema import render_text
from workflows.testing.stages import execute_ui_agent as stage_mod
from workflows.testing.stages import store_cases, write_json
from workflows.testing.store import Case

STAGE = stage_mod.STAGE
WEB, WEB2, APP = "TC-RSE-UI-FUNC-01", "TC-RSE-UI-NEG-01", "TC-RSE-APP-FUNC-01"
BROWSER_CALL = {"name": "mcp__playwright__browser_snapshot", "kind": "mcp", "input": {}}


def passing(extid: str) -> dict:
    return {"extid": extid, "status": "PASS", "reason": "the listing opens",
            "parts": [{"label": "A", "expected": [{"text": "the listing opens", "state": "met",
                                                    "observed": "listing shown", "evidence": "a.png"}]}],
            "actions": [{"step": "A1", "action": "open /labs", "saw": "the listing"}],
            "deviations": [], "observations": [], "blocked_on": ""}


def failing(extid: str) -> dict:
    v = passing(extid) | {"status": "FAIL", "cause": "product", "reason": "the page shows an error"}
    v["parts"][0]["expected"][0] |= {"state": "not met", "observed": "400 Bad Request"}
    return v


class AgentEngine:
    """Plays the session for each case. `script[extid]` is a verdict dict, a list of them (one per run of
    that case, so a FAIL can be followed by its confirmation), a callable `(folder) -> None`, or absent
    (the agent writes nothing)."""

    def __init__(self, script: dict | None = None, calls: int = 3, sleep: float = 0.0):
        self.script, self.calls, self.sleep = script or {}, calls, sleep
        self.requests, self.live, self.peak = [], 0, 0
        self._lock = threading.Lock()

    def run(self, req):
        from harness.engines import EngineResult
        from harness.events import TraceSummary

        with self._lock:
            self.requests.append(req)
            self.live += 1
            self.peak = max(self.peak, self.live)
        try:
            time.sleep(self.sleep)
            name = req.label.removeprefix(f"{STAGE.name}.")           # <extid> or <extid>.confirm
            extid = name.removesuffix(".confirm")
            folder = req.cwd / ui_agent.AGENT_DIR / name
            step = self.script.get(extid)
            if isinstance(step, list):
                step = step.pop(0) if step else None
            if callable(step):
                step(folder)
            elif step is not None:
                (folder / "a.png").write_bytes(b"png")
                (folder / ui_agent.VERDICT).write_text(json.dumps(step))
        finally:
            with self._lock:
                self.live -= 1
        return EngineResult(summary=TraceSummary(final_text="done", got_result=True, cost_usd=0.5,
                                                 tool_calls=[BROWSER_CALL] * self.calls),
                            duration_s=0.1, engine_desc="fake", prompt_sent=req.prompt, trace_path=req.trace_path)


def case(extid: str, expected: str = "The listing opens.") -> Case:
    return Case(extid=extid, summary=f"summary of {extid}", tags=ui_agent.WEB_UI_TAGS,
                text=render_text({"precondition": "logged in", "steps": "1. Open the listing",
                                  "expected": expected}))


@pytest.fixture
def agent_ctx(tmp_path, ctx_factory, monkeypatch):
    """A run that may drive https://web.test, with the browser server taken as installed."""
    monkeypatch.setattr(ui_agent, "browser_problem", lambda: "")
    return ctx_factory(tmp_path / "run", flags={"testing.agent_hosts": "web.test"},
                       inputs={"feature": "RSE-1", "env": "prestag", "build": "b1",
                               "base_url": "https://web.test"})


def given(monkeypatch, *cases: Case) -> None:
    monkeypatch.setattr(ui_agent, "select_cases", lambda store, feature, **kw: list(cases))


def run(ctx, engine) -> dict:
    ctx.engine = engine
    STAGE.engine(STAGE, ctx)
    return json.loads(ctx.path(stage_mod.RESULTS_OUT).read_text())


def by_id(res: dict) -> dict:
    return {r["extid"]: r for r in res["results"]}


# ---- end to end against the JSON store ------------------------------------------------------------------


def test_each_confirmed_web_case_gets_an_agent_and_is_recorded(agent_ctx, clean_cases, approve):
    cases = [dict(c) for c in clean_cases[:3]]
    for row, (extid, surface) in zip(cases, ((WEB, "web"), (WEB2, "web"), (APP, "android")), strict=True):
        row.update(id=extid, execution="ui", surface=surface)
    write_json(agent_ctx.path("qa/cases.json"), cases)
    store_cases.STAGE.engine(store_cases.STAGE, agent_ctx)
    approve(agent_ctx.run_dir / "store")
    engine = AgentEngine({WEB: passing(WEB), WEB2: [failing(WEB2), failing(WEB2)]})
    res = run(agent_ctx, engine)

    assert res["tally"] == {"PASS": 1, "FAIL": 1} and (res["parts_met"], res["parts"]) == (1, 2)
    assert sorted(by_id(res)) == [WEB, WEB2]                        # the app case is not a web case
    assert by_id(res)[WEB2]["confirmation"] == "FAIL again — confirmed"
    assert STAGE.gate(agent_ctx) == []
    stored = json.loads(next((agent_ctx.run_dir / "store" / "runs").rglob("*.json")).read_text())
    assert stored["executions"][WEB]["status"] == "PASS" and stored["executions"][WEB2]["status"] == "FAIL"
    comment = stored["executions"][WEB2]["comments"][0]
    assert "0 of 1 parts met" in comment and "[not met]" in comment and "400 Bad Request" in comment


def test_an_agent_gets_its_own_browser_held_to_the_runs_address(agent_ctx, monkeypatch):
    given(monkeypatch, case(WEB))
    engine = AgentEngine({WEB: passing(WEB)})
    run(agent_ctx, engine)
    req = engine.requests[0]
    folder = agent_ctx.path(ui_agent.AGENT_DIR) / WEB
    assert req.env[guard_browser.ORIGIN_ENV] == "https://web.test"
    assert req.env[guard_browser.MAX_ACTIONS_ENV] == "40" and req.env[guard_browser.COUNT_FILE_ENV]
    spec = req.containment.mcp_servers[ui_agent.SERVER]
    assert spec["command"] == "node" and "--isolated" in spec["args"] and "--headless" in spec["args"]
    assert spec["args"][spec["args"].index("--output-dir") + 1] == str(folder)
    assert "--allowed-origins" not in spec["args"] and "--storage-state" not in spec["args"]
    assert req.native_ctx["stage"].mcp_servers == (ui_agent.SERVER,)
    assert req.timeout_s == 480 + stage_mod.GRACE_S and req.label == f"{STAGE.name}.{WEB}"
    assert WEB in req.prompt and "The listing opens." in req.prompt and "NOT logged in" in req.prompt


def test_a_recorded_session_a_visible_window_and_code_lookup_are_opt_in(agent_ctx, monkeypatch, tmp_path):
    auth = tmp_path / "state.json"
    auth.write_text("{}")
    agent_ctx.inputs["ui_auth"] = str(auth)
    agent_ctx.flags._v.update({"testing.ui_headed": True, "testing.agent_code_context": True,
                                    "testing.agent_browser": "chrome"})
    given(monkeypatch, case(WEB))
    engine = AgentEngine({WEB: passing(WEB)})
    run(agent_ctx, engine)
    req = engine.requests[0]
    args = req.containment.mcp_servers[ui_agent.SERVER]["args"]
    assert args[args.index("--storage-state") + 1] == str(auth) and "--headless" not in args
    assert args[args.index("--browser") + 1] == "chrome"
    assert req.native_ctx["stage"].mcp_servers == (ui_agent.SERVER, "fleetlens")
    assert "already logged in" in req.prompt and "fleetlens" in req.prompt


# ---- batches, notes --------------------------------------------------------------------------------------


def test_cases_run_five_at_a_time(agent_ctx, monkeypatch):
    ids = [f"TC-X-{i:02d}" for i in range(12)]
    given(monkeypatch, *[case(e) for e in ids])
    engine = AgentEngine({e: passing(e) for e in ids}, sleep=0.05)
    res = run(agent_ctx, engine)
    assert len(engine.requests) == 12 and engine.peak == 5
    assert [r["extid"] for r in res["results"]] == ids and res["tally"] == {"PASS": 12}
    started = [r.label.removeprefix(f"{STAGE.name}.") for r in engine.requests]
    assert set(started[:5]) == set(ids[:5]) and set(started[5:10]) == set(ids[5:10])   # 5, 5, 2 — in order


def test_what_one_batch_learns_reaches_the_next(agent_ctx, monkeypatch):
    agent_ctx.flags._v["testing.agent_parallel"] = 1
    given(monkeypatch, case(WEB), case(WEB2))

    def learns(folder: Path) -> None:
        (folder / "a.png").write_bytes(b"png")
        (folder / ui_agent.VERDICT).write_text(json.dumps(passing(WEB)))
        (folder / ui_agent.NOTES).write_text("- a city picker appears first; choose Gurgaon\n")

    engine = AgentEngine({WEB: learns, WEB2: passing(WEB2)})
    run(agent_ctx, engine)
    assert "city picker" not in engine.requests[0].prompt
    assert "a city picker appears first; choose Gurgaon" in engine.requests[1].prompt


# ---- a verdict has to stand ------------------------------------------------------------------------------


def test_no_verdict_is_the_harnesss_error_never_a_fail(agent_ctx, monkeypatch):
    given(monkeypatch, case(WEB))
    res = run(agent_ctx, AgentEngine({}))
    row = by_id(res)[WEB]
    assert row["status"] == "ERROR" and "wrote no verdict.json" in row["reason"]
    assert any("every case ended in ERROR" in p for p in STAGE.gate(agent_ctx))


def test_running_out_of_actions_is_blocked(agent_ctx, monkeypatch):
    given(monkeypatch, case(WEB))
    res = run(agent_ctx, AgentEngine({WEB: lambda folder: (folder / ".actions").write_text("40")}))
    row = by_id(res)[WEB]
    assert row["status"] == "BLOCKED" and row["blocked_on"] == "limit" and "40 actions" in row["reason"]
    assert STAGE.gate(agent_ctx) == []


def test_a_verdict_without_its_evidence_is_rejected_and_kept(agent_ctx, monkeypatch):
    given(monkeypatch, case(WEB))

    def claims(folder: Path) -> None:
        (folder / ui_agent.VERDICT).write_text(json.dumps(passing(WEB)))     # names a.png, saves none

    res = run(agent_ctx, AgentEngine({WEB: claims}))
    assert by_id(res)[WEB]["status"] == "ERROR" and "does not exist" in by_id(res)[WEB]["reason"]
    assert (agent_ctx.path(ui_agent.AGENT_DIR) / WEB / "verdict.rejected.json").is_file()


def test_a_verdict_the_session_did_not_work_for_is_rejected(agent_ctx, monkeypatch):
    given(monkeypatch, case(WEB))
    res = run(agent_ctx, AgentEngine({WEB: passing(WEB)}, calls=0))
    assert by_id(res)[WEB]["status"] == "ERROR" and "not worked for" in by_id(res)[WEB]["reason"]


def test_a_session_that_dies_costs_only_its_own_case(agent_ctx, monkeypatch):
    given(monkeypatch, case(WEB), case(WEB2))

    def dies(folder: Path) -> None:
        raise RuntimeError("connection dropped")

    res = run(agent_ctx, AgentEngine({WEB: dies, WEB2: passing(WEB2)}))
    assert by_id(res)[WEB]["status"] == "ERROR" and "connection dropped" in by_id(res)[WEB]["reason"]
    assert by_id(res)[WEB2]["status"] == "PASS"


# ---- a FAIL is run twice ---------------------------------------------------------------------------------


def test_a_failure_that_does_not_repeat_stays_a_fail_and_is_called_flaky(agent_ctx, monkeypatch):
    given(monkeypatch, case(WEB))
    engine = AgentEngine({WEB: [failing(WEB), passing(WEB)]})
    row = by_id(run(agent_ctx, engine))[WEB]
    assert len(engine.requests) == 2 and engine.requests[1].label.endswith(".confirm")
    assert row["status"] == "FAIL" and row["flaky"] is True and "did not repeat" in row["confirmation"]
    assert (agent_ctx.path(ui_agent.AGENT_DIR) / f"{WEB}.confirm" / ui_agent.VERDICT).is_file()


def test_confirmation_can_be_turned_off(agent_ctx, monkeypatch):
    agent_ctx.flags._v["testing.agent_confirm_fail"] = False
    given(monkeypatch, case(WEB))
    engine = AgentEngine({WEB: failing(WEB)})
    row = by_id(run(agent_ctx, engine))[WEB]
    assert len(engine.requests) == 1 and row["status"] == "FAIL" and not row["confirmation"]


# ---- what never reaches an agent -------------------------------------------------------------------------


def test_a_case_with_no_expected_result_is_not_executed(agent_ctx, monkeypatch):
    given(monkeypatch, case(WEB), case("TC-CT-05", expected=""))
    engine = AgentEngine({WEB: passing(WEB)})
    res = run(agent_ctx, engine)
    assert len(engine.requests) == 1 and [r["extid"] for r in res["results"]] == [WEB]
    assert [r["extid"] for r in res["not_executed"]] == ["TC-CT-05"]
    kept = json.loads((agent_ctx.path(ui_agent.AGENT_DIR) / "TC-CT-05" / ui_agent.VERDICT).read_text())
    assert kept["status"] == ui_agent.NOT_EXECUTED and STAGE.gate(agent_ctx) == []


def test_nothing_confirmed_calls_no_agent(agent_ctx, monkeypatch):
    given(monkeypatch)
    engine = AgentEngine()
    res = run(agent_ctx, engine)
    assert engine.requests == [] and res["results"] == [] and STAGE.gate(agent_ctx) == []


@pytest.mark.parametrize("change, match", [
    ({"flags": {"testing.agent_hosts": ""}}, "not a named test environment"),
    ({"flags": {"testing.agent_hosts": "other.test"}}, "not a named test environment"),
    ({"inputs": {"base_url": ""}}, "needs --base-url"),
    ({"inputs": {"build": ""}}, "needs --env and --build"),
    ({"inputs": {"ui_auth": "/no/such/state.json"}}, "does not exist"),
    ({"inputs": {"ui_login": "/some/login.json"}}, "recorded session only"),
])
def test_the_stage_refuses_before_any_browser_starts(agent_ctx, monkeypatch, change, match):
    agent_ctx.flags._v.update(change.get("flags", {}))
    agent_ctx.inputs.update(change.get("inputs", {}))
    given(monkeypatch, case(WEB))
    engine = AgentEngine({WEB: passing(WEB)})
    agent_ctx.engine = engine
    with pytest.raises(EngineFailure, match=match):
        STAGE.engine(STAGE, agent_ctx)
    assert engine.requests == []


def test_an_environment_named_in_config_is_a_test_environment():
    kw = {"environments": "prestag,jupiter", "template": "https://{env}.1mg.test"}
    assert ui_agent.host_problem("https://jupiter.1mg.test", **kw) == ""
    assert "not a named test environment" in ui_agent.host_problem("https://www.1mg.test", **kw)
    assert ui_agent.host_problem("https://qa.example", hosts="QA.example, other") == ""
    assert ui_agent.host_problem("https://qa.example.evil.test", hosts="qa.example")


def test_the_browser_guard_counts_actions_and_stops_at_the_limit(tmp_path):
    count = tmp_path / ".actions"
    actions = ("browser_click",)
    assert guard_browser.over_limit("browser_snapshot", actions, "2", str(count)) == "" and not count.exists()
    assert guard_browser.over_limit("browser_click", actions, "2", str(count)) == ""
    assert guard_browser.over_limit("browser_click", actions, "2", str(count)) == ""
    assert "used its 2 browser actions" in guard_browser.over_limit("browser_click", actions, "2", str(count))
    assert count.read_text() == "2"
    assert guard_browser.over_limit("browser_click", actions, "", str(count)) == ""       # no limit set


def test_the_stage_composes_only_on_request():
    def flags(on: dict):
        return type("F", (), {"get": lambda self, name: on.get(name, name == "testing.store")})()

    assert "execute_ui_agent" not in compose_pipeline(flags({}))
    assert compose_pipeline(flags({"testing.execute_ui_agent": True}))[-1] == "execute_ui_agent"
