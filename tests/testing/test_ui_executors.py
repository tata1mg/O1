"""The UI executors: `derive_ui` + `execute_web` (Playwright) and `publish_app` (Maestro).

Offline: the JSON store, a fake engine, and Playwright's own JSON report / Maestro's JUnit XML as fixtures.
The real runner is exercised once, against a page served from this machine, and skipped when Playwright is
not installed (`tools/ui-runner`).
"""

from __future__ import annotations

import http.server
import json
import threading
from pathlib import Path

import pytest

from harness.contracts import EngineFailure, GateFailure
from workflows.testing import compose_pipeline, maestro, status, ui_web
from workflows.testing.stages import derive_ui, execute_web, publish_app, store_cases, write_json

WEB = "TC-RSE-UI-FUNC-01"
WEB2 = "TC-RSE-UI-NEG-01"
APP = "TC-RSE-APP-FUNC-01"
COMPONENT = 'export const Talk = () => (\n  <button aria-label="Talk to 1Voice">Talk</button>\n);\n'


def spec_text(extid: str, body: str = "") -> str:
    body = body or ("  await page.goto('/talk');\n"
                    "  await expect(page.getByRole('button', { name: 'Talk to 1Voice' })).toBeVisible();\n")
    head = "import { test, expect } from '@playwright/test';\n\n"
    return f"{head}test('{extid}', async ({{ page }}) => {{\n{body}}});\n"


@pytest.fixture
def ui_ctx(tmp_path, ctx_factory, clean_cases, approve):
    """A stored, reviewed suite with two web UI cases and one app UI case, and an RFC run snapshot."""
    rfc_run = tmp_path / "rfc-run"
    src = rfc_run / "_snap" / "web" / "src"
    src.mkdir(parents=True)
    (src / "Talk.js").write_text(COMPONENT)
    cases = [dict(c) for c in clean_cases[:3]]
    for case, (extid, surface) in zip(cases, ((WEB, "web"), (WEB2, "web"), (APP, "android")), strict=True):
        case.update(id=extid, execution="ui", surface=surface)
    c = ctx_factory(tmp_path / "run", inputs={"feature": "RSE-1", "rfc_run_dir": str(rfc_run),
                                              "env": "prestag", "build": "b1",
                                              "base_url": "https://web.test"})
    write_json(c.path("qa/sources.json"), {"feature": "RSE-1", "rfc_run_dir": str(rfc_run), "sources": []})
    write_json(c.path("qa/cases.json"), cases)
    store_cases.STAGE.engine(store_cases.STAGE, c)
    approve(c.run_dir / "store")
    return c


def good_plan(root: Path) -> dict:
    return {"tests": [{"extid": WEB, "route": "/talk",
                       "locators": [{"locator": "getByRole('button', { name: 'Talk to 1Voice' })",
                                     "source": "src/Talk.js:2"}], "mocks": []}],
            "skipped": [{"extid": WEB2, "reason": "needs a human to judge the copy"}]}


def write_specs(ui: Path, plan: dict, texts: dict | None = None) -> None:
    (ui / ui_web.TESTS_DIR).mkdir(parents=True, exist_ok=True)
    for row in plan["tests"]:
        (ui / ui_web.TESTS_DIR / ui_web.spec_name(row["extid"])).write_text(
            (texts or {}).get(row["extid"], spec_text(row["extid"])))


# ---- the derive_ui contract ------------------------------------------------------------------------------


def validate(tmp_path, plan, texts=None, titles=..., cases=(WEB, WEB2)):
    ui = tmp_path / "ui"
    root = tmp_path / "snap"
    (root / "src").mkdir(parents=True, exist_ok=True)
    (root / "src" / "Talk.js").write_text(COMPONENT)
    write_specs(ui, plan, texts)
    loaded = [r["extid"] for r in plan["tests"]] if titles is ... else titles
    return ui_web.validate(plan, [{"extid": e} for e in cases], ui, loaded, "", [root])


def test_a_good_plan_passes(tmp_path):
    assert validate(tmp_path, good_plan(tmp_path)) == []


@pytest.mark.parametrize("mutate, expect", [
    (lambda p, t: p["skipped"].clear(), "neither tested nor skipped"),
    (lambda p, t: p["skipped"].append({"extid": WEB, "reason": "x"}), "in both"),
    (lambda p, t: p["skipped"][0].update(reason=""), "skipped with no reason"),
    (lambda p, t: p["tests"][0].update(locators=[]), "no `locators`"),
    (lambda p, t: p["tests"][0]["locators"][0].update(source="src/Talk.js:99"), "past the end"),
    (lambda p, t: p["tests"][0]["locators"][0].update(source="src/Nope.js:1"), "no such file"),
    (lambda p, t: t.update({WEB: spec_text(WEB, "  await page.goto('https://prestag.test/talk');\n")}),
     "absolute URL"),
    (lambda p, t: t.update({WEB: spec_text(WEB, "  const password = 'hunter22';\n")}), "credential"),
    (lambda p, t: t.update({WEB: spec_text(WEB, "  await page.waitForTimeout(500);\n")}), "fixed sleep"),
    (lambda p, t: t.update({WEB: spec_text(WEB, "  await page.route('**/x', r => r.fulfill({}));\n")}),
     "mocks the network"),
    (lambda p, t: t.update({WEB: spec_text(WEB) + spec_text("TC-OTHER-1").split("\n", 2)[2]}),
     "exactly one test"),
])
def test_the_gate_names_each_problem(tmp_path, mutate, expect):
    plan, texts = good_plan(tmp_path), {}
    mutate(plan, texts)
    failures = validate(tmp_path, plan, texts)
    assert any(expect in f for f in failures), failures


def test_a_spec_playwright_did_not_load_fails(tmp_path):
    assert any("did not load" in f for f in validate(tmp_path, good_plan(tmp_path), titles=[]))


def test_a_stray_spec_file_fails(tmp_path):
    plan = good_plan(tmp_path)
    write_specs(tmp_path / "ui", {"tests": [{"extid": "TC-STRAY-1"}]})
    assert any("no `tests` entry names" in f for f in validate(tmp_path, plan))


# ---- Playwright report -> statuses -------------------------------------------------------------------------


def pw_report(*tests) -> dict:
    specs = [{"title": extid, "tests": [{"status": "expected", "annotations": notes,
                                         "results": [{"status": st, "duration": 1500,
                                                      "startTime": "2026-11-03T10:00:00.000Z",
                                                      "errors": [{"message": msg}] if msg else [],
                                                      "attachments": att}]}]}
             for extid, st, msg, att, notes in tests]
    return {"suites": [{"title": "x.spec.ts", "specs": specs, "suites": []}]}


def test_report_statuses_follow_the_fail_blocked_error_split():
    rows = {r["extid"]: r for r in ui_web.results(pw_report(
        ("A", "passed", "", [], []),
        ("B", "failed", "\x1b[31mError: expect(locator).toBeVisible() failed\x1b[39m", [], []),
        ("C", "failed", "page.goto: net::ERR_NAME_NOT_RESOLVED at https://x", [], []),
        ("D", "skipped", "", [], [{"type": "skip", "description": "FX-01 missing"}]),
        ("E", "timedOut", "Test timeout of 60000ms exceeded.", [], []),
        ("F", "interrupted", "", [], []),
    ))}
    assert {k: v["status"] for k, v in rows.items()} == {
        "A": "PASS", "B": "FAIL", "C": "BLOCKED", "D": "BLOCKED", "E": "FAIL", "F": "ERROR"}
    assert "\x1b" not in rows["B"]["detail"] and rows["D"]["detail"] == "FX-01 missing"
    assert rows["A"]["finished_at"].isoformat() == "2026-11-03T10:00:01.500000+00:00"


# ---- derive_ui -----------------------------------------------------------------------------------------------


class SpecEngine:
    """Writes the plan and specs the way the persona would."""

    def __init__(self):
        self.requests = []

    def run(self, req):
        from harness.engines import EngineResult
        from harness.events import TraceSummary

        self.requests.append(req)
        ui = req.cwd / ui_web.UI_DIR
        plan = good_plan(ui)
        write_specs(ui, plan)
        (ui / ui_web.PLAN).write_text(json.dumps(plan))
        return EngineResult(summary=TraceSummary(final_text="ok", got_result=True), duration_s=0.1,
                            engine_desc="fake", prompt_sent=req.prompt, trace_path=req.trace_path)


def test_derive_ui_gives_the_model_the_confirmed_web_cases_and_the_snapshot(ui_ctx, monkeypatch):
    monkeypatch.setattr(ui_web, "runner_problem", lambda: "")
    monkeypatch.setattr(ui_web, "prepare", lambda ui: (ui / ui_web.TESTS_DIR).mkdir(parents=True, exist_ok=True))
    engine = SpecEngine()
    ui_ctx.engine = engine
    derive_ui.STAGE.engine(derive_ui.STAGE, ui_ctx)
    cases = json.loads(ui_ctx.path(derive_ui.CASES_OUT).read_text())
    assert sorted(c["extid"] for c in cases) == [WEB, WEB2]          # the app case is not a web case
    roots = json.loads(ui_ctx.path(derive_ui.ROOTS_OUT).read_text())
    assert roots == [str(Path(ui_ctx.inputs["rfc_run_dir"]) / "_snap" / "web")]
    assert Path(roots[0]) in engine.requests[0].containment.readable_roots
    monkeypatch.setattr(ui_web, "list_titles", lambda ui, login=False: ([WEB], ""))
    assert derive_ui.STAGE.gate(ui_ctx) == []


def test_derive_ui_grants_the_snapshot_through_add_dir_and_needs_one(ui_ctx, monkeypatch, tmp_path):
    monkeypatch.setattr(ui_web, "runner_problem", lambda: "")
    monkeypatch.setattr(ui_web, "prepare", lambda ui: (ui / ui_web.TESTS_DIR).mkdir(parents=True, exist_ok=True))
    ui_ctx.engine = SpecEngine()
    derive_ui.STAGE.engine(derive_ui.STAGE, ui_ctx)
    assert derive_ui.STAGE.add_repos_root
    assert ui_ctx.inputs["repos_root"] == str(Path(ui_ctx.inputs["rfc_run_dir"]) / "_snap" / "web")
    empty = tmp_path / "no-snap"
    empty.mkdir()
    write_json(ui_ctx.path("qa/sources.json"), {"feature": "RSE-1", "rfc_run_dir": str(empty), "sources": []})
    with pytest.raises(EngineFailure, match="no front-end source"):
        derive_ui.STAGE.engine(derive_ui.STAGE, ui_ctx)


def test_derive_ui_with_nothing_confirmed_calls_no_model(ui_ctx, approve, monkeypatch):
    monkeypatch.setattr(ui_web, "runner_problem", lambda: "")
    monkeypatch.setattr(ui_web, "prepare", lambda ui: ui.mkdir(parents=True, exist_ok=True))
    approve(ui_ctx.run_dir / "store", status="PROPOSED")
    ui_ctx.engine = None                                          # would raise if it were used
    result = derive_ui.STAGE.engine(derive_ui.STAGE, ui_ctx)
    assert "nothing to derive" in result.final_text
    assert derive_ui.STAGE.gate(ui_ctx) == []


def test_derive_ui_refuses_without_a_runner(ui_ctx, monkeypatch):
    monkeypatch.setattr(ui_web, "runner_problem", lambda: "Playwright is not installed")
    with pytest.raises(EngineFailure, match="not installed"):
        derive_ui.STAGE.engine(derive_ui.STAGE, ui_ctx)


# ---- execute_web ----------------------------------------------------------------------------------------------


def test_execute_web_records_each_case_and_attaches_the_evidence(ui_ctx, monkeypatch, tmp_path):
    ui = ui_ctx.path(ui_web.UI_DIR)
    plan = {"tests": [{"extid": WEB}], "skipped": [{"extid": WEB2, "reason": "human"}]}
    write_json(ui / ui_web.PLAN, plan)
    shot = tmp_path / "shot.png"
    shot.write_bytes(b"png")
    report = pw_report((WEB, "failed", "Error: expect(locator).toBeVisible() failed",
                        [{"name": "screenshot", "path": str(shot), "contentType": "image/png"}], []))
    seen = {}

    def fake_run(ui_, **kw):
        seen.update(kw)
        return report, ""

    monkeypatch.setattr(ui_web, "runner_problem", lambda: "")
    monkeypatch.setattr(ui_web, "run", fake_run)
    execute_web.STAGE.engine(execute_web.STAGE, ui_ctx)
    res = json.loads(ui_ctx.path(execute_web.RESULTS_OUT).read_text())
    assert res["tally"] == {"FAIL": 1} and res["not_automated"] == [WEB2]
    assert seen["base_url"] == "https://web.test" and seen["device"] == "Pixel 7"
    assert execute_web.STAGE.gate(ui_ctx) == []
    run = json.loads(next((ui_ctx.run_dir / "store" / "runs").rglob("*.json")).read_text())
    assert set(run["attached"]) == {WEB, WEB2}                     # the skipped case is in the run, IDLE
    assert run["executions"][WEB]["status"] == "FAIL" and WEB2 not in run["executions"]
    assert any(p.name.startswith("screenshot-") for p in (ui_ctx.run_dir / "store" / "attachments").rglob("*"))


@pytest.mark.parametrize("change, match", [
    ({"base_url": ""}, "--base-url"),
    ({"build": ""}, "--env and --build"),
    ({"ui_auth": "/nope/state.json"}, "does not exist"),
])
def test_execute_web_names_a_missing_input(ui_ctx, monkeypatch, change, match):
    write_json(ui_ctx.path(ui_web.UI_DIR) / ui_web.PLAN, {"tests": [{"extid": WEB}], "skipped": []})
    monkeypatch.setattr(ui_web, "runner_problem", lambda: "")
    ui_ctx.inputs.update(change)
    with pytest.raises(EngineFailure, match=match):
        execute_web.STAGE.engine(execute_web.STAGE, ui_ctx)


def test_execute_web_with_no_test_is_a_gate_failure(ui_ctx):
    write_json(ui_ctx.path(ui_web.UI_DIR) / ui_web.PLAN, {"tests": [], "skipped": [{"extid": WEB, "reason": "x"}]})
    with pytest.raises(GateFailure, match="no Playwright test"):
        execute_web.STAGE.engine(execute_web.STAGE, ui_ctx)


@pytest.mark.skipif(bool(ui_web.runner_problem()), reason="Playwright not installed in tools/ui-runner")
def test_the_real_runner_against_a_local_page(tmp_path):
    site = tmp_path / "site"
    (site / "talk").mkdir(parents=True)          # /talk -> /talk/index.html, served as text/html
    (site / "talk" / "index.html").write_text(
        '<html><body><button aria-label="Talk to 1Voice">Talk</button></body></html>')
    handler = lambda *a, **kw: http.server.SimpleHTTPRequestHandler(*a, directory=str(site), **kw)  # noqa: E731
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        ui = tmp_path / "qa" / "ui"
        write_specs(ui, {"tests": [{"extid": WEB}, {"extid": WEB2}]},
                    {WEB2: spec_text(WEB2, "  await page.goto('/talk');\n  await expect(page.getByText('Nope'))"
                                           ".toBeVisible({ timeout: 1000 });\n")})
        assert sorted(ui_web.list_titles(ui)[0]) == [WEB, WEB2]
        report, err = ui_web.run(ui, base_url=f"http://127.0.0.1:{server.server_port}", device="Pixel 7",
                                 timeout_s=20)
        assert err == ""
        rows = {r["extid"]: r for r in ui_web.results(report)}
        assert rows[WEB]["status"] == "PASS" and rows[WEB2]["status"] == "FAIL"
        assert {a["name"] for a in rows[WEB2]["attachments"]} >= {"screenshot", "trace"}
    finally:
        server.shutdown()


# ---- Maestro: attribution and publish_app ----------------------------------------------------------------------

FLOW = """# Flow: talk button
#
# Covers:
#   {app}  the talk button opens the call sheet
#
---
- launchApp
- tapOn:
    id: "talk"
    label: "{app}: tap the talk button"
- assertVisible:
    text: "Listening"
    label: "{app}: the call sheet shows Listening"
"""


def junit(flow: str, outcome: str, *, timeline: str = "", label: str = "") -> str:
    bad = ""
    if outcome == "FAIL":
        bad = (f'<failure message="{label}: element not found">  Label:    "{label}"\n'
               f"  Command:  assertVisible\n</failure>")
    return (f'<testsuites><testsuite name="s" device="emulator-5554"><testcase id="{flow}" name="{flow}" '
            f'time="12.5" timestamp="2026-11-03T10:00:00">{bad}'
            f"<system-out>{timeline}</system-out></testcase></testsuite></testsuites>")


def test_the_case_id_pattern_matches_generated_and_sheet_ids():
    text = "TC-AC-03 (B), TC-ONEVOICE-ENTRY-FUNC-01, TC-NRDC-INTAKE-FUNC-01: x"
    assert maestro.RE_TC.findall(text) == ["TC-AC-03", "TC-ONEVOICE-ENTRY-FUNC-01", "TC-NRDC-INTAKE-FUNC-01"]


def test_a_failing_step_fails_only_the_case_it_names(tmp_path):
    flows = tmp_path / "flows"
    flows.mkdir()
    (flows / "talk.yaml").write_text(FLOW.format(app=APP))
    label = f"{APP}: the call sheet shows Listening"
    timeline = (f"\n  10:00:01.000  {APP}: tap the talk button  [COMPLETED]\n"
                f"  10:00:02.000  {label}  [FAILED]\n")
    (tmp_path / "r.xml").write_text(junit("talk", "FAIL", timeline=timeline, label=label))
    run = maestro.parse_report(tmp_path / "r.xml")[0]
    verdict = maestro.attribute(run, maestro.flow_claims(flows)["talk"])
    assert verdict[APP][0] == "FAIL" and "the call sheet shows Listening" in verdict[APP][1]


@pytest.fixture
def app_ctx(ui_ctx, tmp_path):
    flows = tmp_path / "druid" / ".maestro" / "flows"
    flows.mkdir(parents=True)
    (flows / "talk.yaml").write_text(FLOW.format(app=APP))
    (flows / "other.yaml").write_text(FLOW.format(app="TC-NOT-IN-STORE-01"))
    report = tmp_path / "report.xml"
    report.write_text('<testsuites><testsuite name="s" device="emulator-5554">'
                      '<testcase id="talk" name="talk" time="12.5" timestamp="2026-11-03T10:00:00"/>'
                      '<testcase id="other" name="other" time="3"/>'
                      '<testcase id="ghost" name="ghost" time="1"/></testsuite></testsuites>')
    ui_ctx.inputs.update(maestro_report=str(report), maestro_flows=str(flows), env="emulator-5554")
    return ui_ctx


def test_publish_app_records_covered_cases_and_reports_the_rest(app_ctx):
    publish_app.STAGE.engine(publish_app.STAGE, app_ctx)
    res = json.loads(app_ctx.path(publish_app.RESULTS_OUT).read_text())
    assert res["tally"] == {"PASS": 1} and res["results"][0]["extid"] == APP
    assert res["unmatched_flows"] == ["ghost"]
    assert res["not_attached"] == ["TC-NOT-IN-STORE-01"]
    assert publish_app.STAGE.gate(app_ctx) == []
    run = json.loads(next((app_ctx.run_dir / "store" / "runs").rglob("*.json")).read_text())
    execution = run["executions"][APP]
    assert execution["status"] == "PASS" and execution["props"]["harness"] == "maestro"
    assert any(f"## {APP} — PASS" in c for c in execution["comments"])


def test_publish_app_names_a_missing_report(app_ctx):
    app_ctx.inputs["maestro_report"] = "/nope.xml"
    with pytest.raises(EngineFailure, match="--maestro-report"):
        publish_app.STAGE.engine(publish_app.STAGE, app_ctx)


# ---- composition and status ---------------------------------------------------------------------------------------


class Flags(dict):
    def get(self, name, default=None):
        return super().get(name, {"testing.evaluate": True, "testing.store": True,
                                  "testing.prerequisites": True}.get(name, False))


@pytest.mark.parametrize("on, tail", [
    ({"testing.execute_web": True}, ("prerequisites", "derive_ui", "execute_web")),
    ({"testing.publish_app": True}, ("prerequisites", "publish_app")),
    ({"testing.execute_web": True, "testing.store": False}, ("render_xlsx", "evaluate", "prerequisites")),
])
def test_ui_stages_compose_only_on_request_and_only_with_the_store(on, tail):
    assert compose_pipeline(Flags(on))[-len(tail):] == tail


def test_status_reports_web_results_and_the_ui_plan(tmp_path):
    run = tmp_path / "run"
    (run / "qa" / "ui").mkdir(parents=True)
    (run / "manifest.json").write_text(json.dumps({
        s: {"status": "ok", "attempts": 1, "duration_s_total": 5, "gate": {"passed": True}}
        for s in (*status.GENERATE_STAGES, "derive_ui", "execute_web")}))
    write_json(run / "qa/ui/plan.json", {"tests": [{"extid": WEB}], "skipped": [{"extid": WEB2, "reason": "r"}]})
    write_json(run / "qa/ui/results.json", {"results": [{"extid": WEB, "status": "PASS"}], "tally": {"PASS": 1},
                                            "run_url": "https://kiwi.test/runs/9/"})
    assert status.next_step(status.stages(run), {"backend": "kiwi"})["step"] == "done"
    assert status.milestone(run, "derive_ui").startswith("derive_ui: 1 Playwright tests written, 1 cases")
    assert "1 PASS" in status.milestone(run, "execute_web")
    assert status.report(run)["ui_plan"]["skipped"] == [{"extid": WEB2, "reason": "r"}]


# ---- the login step --------------------------------------------------------------------------------------------

LOGIN_PAGE = """<html><body>
<input placeholder="Enter mobile number"><input placeholder="Enter OTP">
<button onclick="const p=document.querySelectorAll('input');
  if (p[0].value==='9000000001' && p[1].value==='4321') {localStorage.setItem('user','ok'); location='/talk/';}
  else document.body.insertAdjacentHTML('beforeend','<p>Wrong OTP</p>')">Verify</button>
</body></html>"""
TALK_PAGE = """<html><body><script>
if (localStorage.getItem('user')) document.write('<button aria-label="Talk to 1Voice">Talk</button>');
else document.write('<p>Please log in</p>');
</script></body></html>"""
LOGIN_SPEC = """import { test, expect } from '@playwright/test';

test('login', async ({ page }) => {
  await page.goto('/login/');
  await page.getByPlaceholder('Enter mobile number').fill(process.env.UI_LOGIN_PHONE!);
  await page.getByPlaceholder('Enter OTP').fill(process.env.UI_LOGIN_OTP!);
  await page.getByRole('button', { name: 'Verify' }).click();
  await expect(page.getByRole('button', { name: 'Talk to 1Voice' })).toBeVisible({ timeout: 3000 });
  await page.context().storageState({ path: process.env.STORAGE_STATE });
});
"""


def login_plan() -> dict:
    plan = good_plan(Path("."))
    plan["login"] = {"route": "/login", "locators": [{"locator": "getByPlaceholder('Enter OTP')",
                                                      "source": "src/Talk.js:2"}]}
    return plan


def test_a_login_step_is_required_when_the_run_logs_in(tmp_path):
    failures = ui_web.validate(good_plan(tmp_path), [{"extid": WEB}, {"extid": WEB2}], tmp_path / "ui", [WEB],
                               roots=[], login_required=True)
    assert any("no `login`" in f for f in failures)


@pytest.mark.parametrize("text, expect", [
    (LOGIN_SPEC, None),
    (LOGIN_SPEC.replace("process.env.UI_LOGIN_OTP!", "'4321'"), "process.env.UI_LOGIN_OTP"),
    (LOGIN_SPEC.replace("process.env.UI_LOGIN_PHONE!", "'9000000001'"), "number literal"),
    (LOGIN_SPEC.replace("await page.context().storageState({ path: process.env.STORAGE_STATE });\n", ""),
     "save the session"),
    (LOGIN_SPEC.replace("test('login'", "test('log in'"), "titled 'login'"),
])
def test_the_login_step_is_checked(tmp_path, text, expect):
    ui = tmp_path / "ui"
    (ui / ui_web.TESTS_DIR).mkdir(parents=True)
    (ui / ui_web.TESTS_DIR / ui_web.LOGIN_SPEC).write_text(text)
    failures = validate(tmp_path, login_plan(), titles=[WEB, "login"])
    login_failures = [f for f in failures if f.startswith("login")]
    assert (login_failures == []) if expect is None else any(expect in f for f in login_failures), failures


def test_only_the_login_step_reads_the_credentials(tmp_path):
    plan = good_plan(tmp_path)
    failures = validate(tmp_path, plan, {WEB: spec_text(WEB, "  console.log(process.env.UI_LOGIN_OTP);\n")})
    assert any("only login.setup.ts may" in f for f in failures)


def test_a_failed_login_blocks_the_cases_it_skipped():
    report = pw_report(("login", "failed", "Error: expect(locator).toBeVisible() failed", [], []),
                       (WEB, "skipped", "", [], []))
    rows = ui_web.results(report)
    assert [r["extid"] for r in rows] == [WEB]                      # the login step is not a case
    assert rows[0]["status"] == "BLOCKED" and rows[0]["detail"].startswith("not run — the login step failed")


def test_execute_web_reads_the_login_file_and_hands_it_to_playwright_only(ui_ctx, monkeypatch, tmp_path):
    ui = ui_ctx.path(ui_web.UI_DIR)
    write_json(ui / ui_web.PLAN, {"tests": [{"extid": WEB}], "skipped": []})
    (ui / ui_web.TESTS_DIR).mkdir(parents=True, exist_ok=True)
    (ui / ui_web.TESTS_DIR / ui_web.LOGIN_SPEC).write_text(LOGIN_SPEC)
    creds = tmp_path / "login.json"
    creds.write_text(json.dumps({"phone": "9000000001", "otp": "4321"}))
    ui_ctx.inputs["ui_login"] = str(creds)
    seen = {}

    def fake_run(ui_, **kw):
        seen.update(kw)
        return pw_report((WEB, "failed", "Error: typed 4321 into the box", [], [])), ""

    monkeypatch.setattr(ui_web, "runner_problem", lambda: "")
    monkeypatch.setattr(ui_web, "run", fake_run)
    execute_web.STAGE.engine(execute_web.STAGE, ui_ctx)
    assert seen["login"] == {"phone": "9000000001", "otp": "4321"}
    written = ui_ctx.path(execute_web.RESULTS_OUT).read_text() + \
        next((ui_ctx.run_dir / "store" / "runs").rglob("*.json")).read_text()
    assert "4321" not in written and "9000000001" not in written
    assert json.loads(ui_ctx.path(execute_web.RESULTS_OUT).read_text())["login"] == "test account (login step)"


@pytest.mark.parametrize("content, match", [("{not json", "not a readable JSON"), ('{"phone": "1"}', '"otp"')])
def test_a_bad_login_file_is_named(ui_ctx, monkeypatch, tmp_path, content, match):
    write_json(ui_ctx.path(ui_web.UI_DIR) / ui_web.PLAN, {"tests": [{"extid": WEB}], "skipped": []})
    creds = tmp_path / "login.json"
    creds.write_text(content)
    ui_ctx.inputs["ui_login"] = str(creds)
    monkeypatch.setattr(ui_web, "runner_problem", lambda: "")
    with pytest.raises(EngineFailure, match=match):
        execute_web.STAGE.engine(execute_web.STAGE, ui_ctx)


@pytest.mark.skipif(bool(ui_web.runner_problem()), reason="Playwright not installed in tools/ui-runner")
@pytest.mark.parametrize("otp, status", [("4321", "PASS"), ("0000", "BLOCKED")])
def test_the_real_runner_logs_in_first(tmp_path, otp, status):
    site = tmp_path / "site"
    for route, page in (("login", LOGIN_PAGE), ("talk", TALK_PAGE)):
        (site / route).mkdir(parents=True)
        (site / route / "index.html").write_text(page)
    handler = lambda *a, **kw: http.server.SimpleHTTPRequestHandler(*a, directory=str(site), **kw)  # noqa: E731
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        ui = tmp_path / "qa" / "ui"
        write_specs(ui, {"tests": [{"extid": WEB}]}, {WEB: spec_text(WEB, "  await page.goto('/talk/');\n"
            "  await expect(page.getByRole('button', { name: 'Talk to 1Voice' })).toBeVisible();\n")})
        (ui / ui_web.TESTS_DIR / ui_web.LOGIN_SPEC).write_text(LOGIN_SPEC)
        assert sorted(ui_web.list_titles(ui, login=True)[0]) == [WEB, "login"]
        report, err = ui_web.run(ui, base_url=f"http://127.0.0.1:{server.server_port}", timeout_s=15,
                                 login={"phone": "9000000001", "otp": otp})
        assert err == ""
        rows = ui_web.results(report)
        assert [(r["extid"], r["status"]) for r in rows] == [(WEB, status)]
    finally:
        server.shutdown()


def test_execute_web_takes_the_base_url_from_the_environment(ui_ctx, monkeypatch, tmp_path):
    write_json(ui_ctx.path(ui_web.UI_DIR) / ui_web.PLAN, {"tests": [{"extid": WEB}], "skipped": []})
    ui_ctx.inputs.update(base_url="", env="jupiter")
    ui_ctx.flags._v.update({"testing.base_url_template": "https://{env}.1mg.com", "testing.ui_headed": True})
    seen = {}
    monkeypatch.setattr(ui_web, "runner_problem", lambda: "")
    report = pw_report((WEB, "passed", "", [], []))
    monkeypatch.setattr(ui_web, "run", lambda ui_, **kw: (seen.update(kw), (report, ""))[1])
    execute_web.STAGE.engine(execute_web.STAGE, ui_ctx)
    assert seen["base_url"] == "https://jupiter.1mg.com" and seen["headed"] is True


def test_testing_cases_narrows_derive_ui_to_the_named_case(ui_ctx, monkeypatch):
    monkeypatch.setattr(ui_web, "runner_problem", lambda: "")
    monkeypatch.setattr(ui_web, "prepare", lambda ui: (ui / ui_web.TESTS_DIR).mkdir(parents=True, exist_ok=True))
    ui_ctx.flags._v["testing.cases"] = WEB
    ui_ctx.engine = SpecEngine()
    derive_ui.STAGE.engine(derive_ui.STAGE, ui_ctx)
    assert [c["extid"] for c in json.loads(ui_ctx.path(derive_ui.CASES_OUT).read_text())] == [WEB]
