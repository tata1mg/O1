"""The `execute_ui_agent` stage: one agent and one browser per case, in batches, each verdict held to its
evidence and to the session's trace, then recorded in the store.

No browser and no model here: `AgentEngine` stands in for the session and writes what an agent would.
"""

from __future__ import annotations

import json
import os
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
                                                    "observed": "listing shown", "evidence": "a.png",
                                                    "on_target": True}]}],
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
                step(folder, req)
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
    monkeypatch.setattr(ui_agent, "browser_problem", lambda browser="": "")
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
    table = agent_ctx.path(stage_mod.TABLE_OUT).read_text()
    assert "| Case | Verdict | Time | Cost | What the agent found |" in table and f"| {WEB} | PASS |" in table
    assert f"| {WEB2} | FAIL (confirmed) |" in table and "| **Total** | 2 run |" in table
    kept = json.loads((agent_ctx.path(ui_agent.AGENT_DIR) / WEB / ui_agent.VERDICT).read_text())
    assert isinstance(kept["duration_s"], float) and kept["cost_usd"] == 0.5         # the case's own file
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
    assert req.env[guard_browser.MAX_ACTIONS_ENV] == "60" and req.env[guard_browser.COUNT_FILE_ENV]
    spec = req.containment.mcp_servers[ui_agent.SERVER]
    assert spec["command"] == "node" and "--isolated" in spec["args"] and "--headless" in spec["args"]
    assert spec["args"][spec["args"].index("--output-dir") + 1] == str(folder)
    assert "--allowed-origins" not in spec["args"] and "--storage-state" not in spec["args"]
    assert req.native_ctx["stage"].mcp_servers == (ui_agent.SERVER,)
    assert req.timeout_s == 900 + stage_mod.GRACE_S and req.label == f"{STAGE.name}.{WEB}"
    assert "browser_snapshot" in req.prompt and "BEFORE your first" in req.prompt
    assert req.env[guard_browser.VERDICT_FILE_ENV] == str(folder / ui_agent.VERDICT)
    assert req.env[guard_browser.VERDICT_EVERY_ENV] == str(stage_mod.VERDICT_EVERY)
    assert f"{stage_mod.CHECK_VERDICT} {ui_agent.AGENT_DIR}/{WEB}" in req.prompt
    assert STAGE.allow_commands == (stage_mod.CHECK_VERDICT,) and req.native_ctx["stage"].allow_commands
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

    def learns(folder: Path, req) -> None:
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
    def spends(folder: Path, req) -> None:      # what the browser guard's counter would hold by then
        Path(req.env[guard_browser.COUNT_FILE_ENV]).write_text("60")

    res = run(agent_ctx, AgentEngine({WEB: spends}))
    row = by_id(res)[WEB]
    assert row["status"] == "BLOCKED" and row["blocked_on"] == "limit" and "60 actions" in row["reason"]
    assert row["cause"] == "limit"
    assert STAGE.gate(agent_ctx) == []


def test_a_session_cut_off_keeps_what_the_agent_had_written(agent_ctx, monkeypatch):
    """Second pilot: a case judged three of its five parts, hit the time limit, and recorded none of it."""
    given(monkeypatch, case(WEB))
    partial = passing(WEB) | {"status": "BLOCKED", "cause": "limit", "blocked_on": "not finished"}
    partial["parts"].append({"label": "B", "expected": [{"text": "the filter sheet opens", "state": "not reached",
                                                         "blocker": "not yet checked"}]})

    def cut_off(folder: Path, req) -> None:
        (folder / "a.png").write_bytes(b"png")
        (folder / ui_agent.VERDICT).write_text(json.dumps(partial))
        raise TimeoutError("no result after 960s")

    row = by_id(run(agent_ctx, AgentEngine({WEB: cut_off})))[WEB]
    assert row["status"] == "BLOCKED" and row["cause"] == "limit" and (row["parts_met"], row["parts"]) == (1, 2)
    assert "cut off" in row["reason"] and "TimeoutError" in row["reason"]
    assert not (agent_ctx.path(ui_agent.AGENT_DIR) / WEB / "verdict.rejected.json").exists()


def test_a_cut_off_sessions_calls_are_recovered_from_its_stream(agent_ctx, monkeypatch):
    from harness.events import TraceSummary

    given(monkeypatch, case(WEB))

    def cut_off(folder: Path, req) -> None:
        raise TimeoutError("no result")

    engine = AgentEngine({WEB: cut_off})
    engine.write_trace = lambda raw, want: want
    (agent_ctx.run_dir / "logs").mkdir(exist_ok=True)
    (agent_ctx.run_dir / "logs" / f"{STAGE.name}.{WEB}.stream.jsonl").write_text("{}\n")
    monkeypatch.setattr("harness.events.summarize",
                        lambda path: TraceSummary(tool_calls=[BROWSER_CALL] * 7, cost_usd=0.4))
    row = by_id(run(agent_ctx, engine))[WEB]
    assert row["browser_calls"] == 7 and row["cost_usd"] == 0.4 and row["status"] == "ERROR"


def test_a_cost_that_was_never_reported_is_called_unknown_not_zero(agent_ctx, monkeypatch):
    """Third pilot: a cut-off session's spend was silently left out of the total."""
    given(monkeypatch, case(WEB), case(WEB2))

    def cut_off(folder: Path, req) -> None:
        raise TimeoutError("no result")

    res = run(agent_ctx, AgentEngine({WEB: cut_off, WEB2: passing(WEB2)}))
    assert res["cost_unknown_for"] == [WEB] and res["cost_usd"] == 0.5


def test_a_verdict_without_its_evidence_is_rejected_and_kept(agent_ctx, monkeypatch):
    given(monkeypatch, case(WEB))

    def claims(folder: Path, req) -> None:
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

    def dies(folder: Path, req) -> None:
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


def test_the_second_run_is_given_the_route_and_not_the_result(agent_ctx, monkeypatch):
    """Pilot, 2026-11-16: the confirm run never found the page the first run had failed on."""
    given(monkeypatch, case(WEB))
    first = failing(WEB) | {
        "deviations": ["Reached the listing through /labs/search: the home widgets do not render."],
        "actions": [{"step": "A1", "action": "press Enter in the search box to reach the results page",
                     "saw": "the price shown is 1899, not 800"}]}

    def fails_and_notes(folder: Path, req) -> None:
        (folder / "a.png").write_bytes(b"png")
        (folder / ui_agent.VERDICT).write_text(json.dumps(first))
        (folder / ui_agent.NOTES).write_text("- Change Address does NOT open a sign-in sheet\n")

    blocked = ui_agent.blocked(WEB, "home page", "the home widgets do not render")
    engine = AgentEngine({WEB: [fails_and_notes, blocked]})
    row = by_id(run(agent_ctx, engine))[WEB]
    second = engine.requests[1].prompt
    assert "press Enter in the search box" in second                       # what it did
    assert "Reached the listing through /labs/search" not in second       # a deviation is prose: it can carry findings
    assert "400 Bad Request" not in second and "the page shows an error" not in second     # not what it concluded
    assert "1899" not in second and "does NOT open a sign-in sheet" not in second          # nor what it saw or noted
    assert "It is a route, not a result" in second and "It is a route" not in engine.requests[0].prompt
    assert row["status"] == "FAIL" and row["confirmed"] is False and row["flaky"] is False
    assert row["summary"].startswith("FAIL (not confirmed)") and "NOT confirmed" in row["confirmation"]


def test_cases_run_in_the_order_they_were_asked_for(agent_ctx, monkeypatch):
    agent_ctx.flags._v.update({"testing.cases": "TC-B,TC-A", "testing.agent_parallel": 1})
    given(monkeypatch, case("TC-A"), case("TC-B"))
    engine = AgentEngine({"TC-A": passing("TC-A"), "TC-B": passing("TC-B")})
    res = run(agent_ctx, engine)
    assert [r["extid"] for r in res["results"]] == ["TC-B", "TC-A"]


def test_a_note_about_the_outcome_is_not_passed_on(agent_ctx, monkeypatch):
    agent_ctx.flags._v["testing.agent_parallel"] = 1
    given(monkeypatch, case(WEB), case(WEB2))

    def steers(folder: Path, req) -> None:
        (folder / "a.png").write_bytes(b"png")
        (folder / ui_agent.VERDICT).write_text(json.dumps(passing(WEB)))
        (folder / ui_agent.NOTES).write_text("- any case starting from the home widgets will be BLOCKED\n"
                                             "- the home widgets do not render\n")

    engine = AgentEngine({WEB: steers, WEB2: passing(WEB2)})
    run(agent_ctx, engine)
    assert "the home widgets do not render" in engine.requests[1].prompt
    assert "will be BLOCKED" not in engine.requests[1].prompt


def test_the_stage_reports_its_sessions_tool_calls(agent_ctx, monkeypatch):
    given(monkeypatch, case(WEB), case(WEB2))
    agent_ctx.engine = AgentEngine({WEB: passing(WEB), WEB2: passing(WEB2)}, calls=3)
    result = STAGE.engine(STAGE, agent_ctx)
    assert len(result.tool_calls) == 6 and result.cost_usd == 1.0
    assert "| Case | Verdict | Time | Cost | What the agent found |" in result.final_text     # every report has it


def test_a_read_only_run_tells_the_agent_to_change_nothing(agent_ctx, monkeypatch):
    given(monkeypatch, case(WEB))
    engine = AgentEngine({WEB: passing(WEB)})
    run(agent_ctx, engine)
    assert "READ-ONLY" not in engine.requests[0].prompt
    agent_ctx.flags._v["testing.agent_read_only"] = True
    run(agent_ctx, engine)
    second = engine.requests[1].prompt
    assert "THIS RUN IS READ-ONLY" in second and "leave the cart as it is" in second


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


def test_a_store_that_is_down_is_said_plainly_before_any_session(agent_ctx, monkeypatch):
    """Third pilot: Kiwi answered 503 and the run ended in a traceback."""
    def down(store, feature, **kw):
        raise OSError("<ProtocolError for kiwi/xml-rpc/: 503 Service Unavailable>")

    monkeypatch.setattr(ui_agent, "select_cases", down)
    engine = AgentEngine()
    agent_ctx.engine = engine
    with pytest.raises(EngineFailure, match="could not read the cases from the case store.*503.*re-run"):
        STAGE.engine(STAGE, agent_ctx)
    assert engine.requests == []


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


def test_the_stage_refuses_an_engine_without_the_browser_guard(agent_ctx, monkeypatch):
    given(monkeypatch, case(WEB))
    engine = AgentEngine({WEB: passing(WEB)})
    engine.name = "axon"
    agent_ctx.engine = engine
    with pytest.raises(EngineFailure, match="claude-code engine only"):
        STAGE.engine(STAGE, agent_ctx)
    assert engine.requests == []


def test_a_missing_browser_build_is_named_before_any_session(agent_ctx, monkeypatch):
    monkeypatch.setattr(ui_agent, "browser_problem", lambda browser="": "" if browser else "needs Chromium build 1")
    given(monkeypatch, case(WEB))
    engine = AgentEngine({WEB: passing(WEB)})
    agent_ctx.engine = engine
    with pytest.raises(EngineFailure, match="needs Chromium build"):
        STAGE.engine(STAGE, agent_ctx)
    agent_ctx.flags._v["testing.agent_browser"] = "chrome"
    assert by_id(run(agent_ctx, engine))[WEB]["status"] == "PASS"


def test_a_login_file_is_ignored_and_the_cases_run_logged_out(agent_ctx, monkeypatch):
    agent_ctx.inputs["ui_login"] = "/some/login.json"
    given(monkeypatch, case(WEB))
    engine = AgentEngine({WEB: passing(WEB)})
    run(agent_ctx, engine)
    assert "NOT logged in" in engine.requests[0].prompt and "/some/login.json" not in engine.requests[0].prompt


def test_a_visible_browser_runs_one_case_at_a_time(agent_ctx, monkeypatch):
    agent_ctx.flags._v["testing.ui_headed"] = True
    ids = [f"TC-X-{i:02d}" for i in range(4)]
    given(monkeypatch, *[case(e) for e in ids])
    engine = AgentEngine({e: passing(e) for e in ids}, sleep=0.02)
    run(agent_ctx, engine)
    assert engine.peak == 1 and len(engine.requests) == 4


def test_a_case_id_that_cannot_name_a_folder_is_not_executed(agent_ctx, monkeypatch):
    given(monkeypatch, case(WEB), case(".."), case("a/b"))
    engine = AgentEngine({WEB: passing(WEB)})
    res = run(agent_ctx, engine)
    assert len(engine.requests) == 1 and sorted(r["extid"] for r in res["not_executed"]) == ["..", "a/b"]
    assert agent_ctx.path("qa").is_dir() and STAGE.gate(agent_ctx) == []


def test_the_agent_cannot_reach_its_own_action_counter(agent_ctx, monkeypatch):
    given(monkeypatch, case(WEB))
    engine = AgentEngine({WEB: passing(WEB)})
    run(agent_ctx, engine)
    count = Path(engine.requests[0].env[guard_browser.COUNT_FILE_ENV]).resolve()
    assert not count.is_relative_to(agent_ctx.run_dir.resolve())


def test_last_runs_notes_do_not_reach_this_run(agent_ctx, monkeypatch):
    stale = agent_ctx.path(stage_mod.NOTES_OUT)
    stale.parent.mkdir(parents=True)
    stale.write_text("- from another build: ignore the city picker\n")
    given(monkeypatch, case(WEB))
    engine = AgentEngine({WEB: passing(WEB)})
    run(agent_ctx, engine)
    assert "another build" not in engine.requests[0].prompt


def test_evidence_named_by_its_run_dir_path_is_found(tmp_path):
    folder = ui_agent.case_dir(tmp_path, WEB)
    folder.mkdir(parents=True)
    (folder / "a.png").write_bytes(b"png")
    v = passing(WEB)
    v["parts"][0]["expected"][0]["evidence"] = f"{ui_agent.AGENT_DIR}/{WEB}/a.png"
    assert ui_agent.validate_verdict(v, WEB, folder) == []


def test_the_screenshot_path_in_the_prompt_is_inside_the_case_folder(agent_ctx, monkeypatch):
    given(monkeypatch, case(WEB))
    engine = AgentEngine({WEB: passing(WEB)})
    run(agent_ctx, engine)
    assert f"`{ui_agent.AGENT_DIR}/{WEB}/a-listing.png`" in engine.requests[0].prompt
    assert "one browser call per message" in engine.requests[0].prompt


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


def test_the_browser_will_not_act_without_a_verdict_file_or_past_a_stale_one(tmp_path):
    """Third pilot: both agents wrote the file once at the start and never again."""
    verdict_file, state = tmp_path / "verdict.json", tmp_path / "count.verdict"
    actions = ("browser_click",)

    def gap(tool="browser_click"):
        return guard_browser.verdict_gap(tool, actions, str(verdict_file), "2", str(state))

    assert "write your verdict file first" in gap()
    assert gap("browser_snapshot") == ""                                  # looking is never refused
    verdict_file.write_text("{}")
    assert gap() == "" and gap() == ""
    assert "since you last wrote your verdict file" in gap()
    assert gap("browser_snapshot") == ""
    os.utime(verdict_file, ns=(1, verdict_file.stat().st_mtime_ns + 5_000_000_000))   # the agent rewrites it
    assert gap() == "" and gap() == "" and "since you last wrote" in gap()
    assert guard_browser.verdict_gap("browser_click", actions, "", "2", str(state)) == ""     # not set: no rule


@pytest.mark.parametrize("tool, inp, refused", [
    ("browser_click", {"element": "Neucoins redeem widget", "ref": "e9"}, True),       # the care-plan run's mistake
    ("browser_click", {"element": "Apply coupon FLAT3", "ref": "e1"}, True),
    ("browser_click", {"element": "Proceed to pay button"}, True),
    ("browser_click", {"element": "Place Order"}, True),
    ("browser_click", {"element": "Remove button in the Remove Care Plan sheet"}, True),
    ("browser_click", {"element": "Add Care Plan card CTA"}, True),
    ("browser_click", {"element": "Save address"}, True),
    ("browser_click", {"element": "Logout"}, True),
    ("browser_click", {"element": "View all coupons"}, False),                         # reading is fine
    ("browser_click", {"element": "Apply button in the filter sheet"}, False),
    ("browser_click", {"element": "ADD Telma 40 Tablet"}, False),
    ("browser_click", {"element": "Remove in the quantity dialog"}, False),            # the agent's own item
    ("browser_click", {"element": "Change Address"}, False),
    ("browser_click", {"element": "Go to cart"}, False),
    ("browser_type", {"element": "search box", "text": "telma"}, False),
    ("browser_snapshot", {}, False),
])
def test_actions_that_spend_or_change_the_account_are_refused(agent_ctx, tool, inp, refused):
    pattern = agent_ctx.flag("testing.agent_forbid")
    reason = guard_browser.forbidden(tool, inp, pattern)
    assert bool(reason) is refused, reason
    if refused:
        assert "not reached" in reason and "another way" in reason


def test_no_pattern_forbids_nothing_and_a_broken_one_forbids_everything():
    click = {"element": "Neucoins redeem widget"}
    assert guard_browser.forbidden("browser_click", click, "") == ""
    assert "not a valid regex" in guard_browser.forbidden("browser_click", click, "(")


def test_the_forbidden_pattern_reaches_the_guard_and_the_prompt(agent_ctx, monkeypatch):
    given(monkeypatch, case(WEB))
    engine = AgentEngine({WEB: passing(WEB)})
    run(agent_ctx, engine)
    req = engine.requests[0]
    assert req.env[guard_browser.FORBID_ENV] == agent_ctx.flag("testing.agent_forbid")
    assert "refused in this run whatever the case says" in req.prompt


def test_the_stage_composes_only_on_request():
    def flags(on: dict):
        return type("F", (), {"get": lambda self, name: on.get(name, name == "testing.store")})()

    assert "execute_ui_agent" not in compose_pipeline(flags({}))
    assert compose_pipeline(flags({"testing.execute_ui_agent": True}))[-1] == "execute_ui_agent"
