"""Android execution: which flow covers a case, flow-or-agent per case, the `execute_app` stage end to end
(fake device, fake agent, JSON store), the draft flow and `convert_flows`, and the `execute` command line.

No device, no Maestro and no model here: `FakeAndroid` plays adb + `maestro test`, `AppAgent` the session.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from workflows.testing import app_agent, devices, execute, maestro_index, ui_agent
from workflows.testing.schema import render_text
from workflows.testing.stages import convert_flows as conv_mod
from workflows.testing.stages import execute_app as stage_mod
from workflows.testing.stages import store_cases, write_json
from workflows.testing.store import Case
from workflows.testing.store.kiwi import KiwiCaseStore

STAGE = stage_mod.STAGE
APP = devices.APP_IDS["dev-release"]
SERIAL = "emulator-5554"
REAL_FLOWS = Path("~/o_1/maestro-automation/.maestro/flows").expanduser()


def write_flow(root: Path, rel: str, header: str, body: str = "- tapOn: Cart\n- assertVisible: Cart\n",
               tags: tuple[str, ...] = ("cart",)) -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    tag_lines = "".join(f"  - {t}\n" for t in tags)
    path.write_text(f"{header.rstrip()}\nappId: ${{APP_ID}}\ntags:\n{tag_lines}---\n{body}")
    return path


@pytest.fixture
def repo(tmp_path) -> Path:
    """A maestro-automation checkout with one flow of each kind."""
    root = tmp_path / "maestro-automation"
    flows = root / ".maestro" / "flows"
    write_flow(flows, "cart/ct001-landing.yaml", "# Covers: TC-AND-01 landing on the cart\n#\n"
                                                 "# STATUS: VALIDATED (green) on emulator-5554, 2026-10-01. FROZEN.")
    write_flow(flows, "cart/ct019-coupon.yaml", "# Covers: Kiwi Pharmacy › Cart #191 — [CT019] coupon\n#\n"
                                                "# STATUS: ✅ GREEN 2026-10-30")
    write_flow(flows, "cart/ct020-draft.yaml", "# Covers: TC-AND-03\n#\n# STATUS: DRAFT — UNVALIDATED")
    write_flow(flows, "cart/ct021-nostatus.yaml", "# Covers: TC-AND-04 no status line")
    write_flow(flows, "cart/ct022-mock.yaml", "# Covers: TC-AND-05\n#\n# STATUS: VALIDATED (green)",
               tags=("cart", "mock-required"))
    write_flow(flows, "cart/ct023-order.yaml", "# Covers: TC-AND-06\n#\n# STATUS: VALIDATED (green)",
               tags=("cart", "manual-order-placing"))
    write_flow(flows, "cp/cp-01.yaml", "# Covers: TC-AND-07\n#\n# STATUS: VALIDATED (green)",
               tags=("cp", "needs-cp-tiering-account"))
    write_flow(flows, "cart/common/launch.yaml", "# Covers: TC-AND-01 (a sub-flow naming a case)")
    write_flow(flows, "cart/sub.yaml", "# Covers: TC-AND-01\n# STATUS: VALIDATED", tags=("sub-flow",))
    (root / "scripts").mkdir()
    return root


def android_case(extid: str, expected: str = "The cart opens.", store_id: str = "", tags=None) -> Case:
    return Case(extid=extid, summary=f"summary of {extid}", store_id=store_id,
                tags=tuple(tags or app_agent.ANDROID_UI_TAGS),
                text=render_text({"precondition": "signed in", "steps": "1. Open the cart", "expected": expected}))


# ---- which flow covers which case --------------------------------------------------------------------------


def test_index_reads_covers_status_gates_and_profiles(repo):
    flows = maestro_index.index(repo / ".maestro" / "flows")
    assert set(flows) == {"ct001-landing", "ct019-coupon", "ct020-draft", "ct021-nostatus", "ct022-mock",
                          "ct023-order", "cp-01"}               # common/ and sub-flows are building blocks
    assert flows["ct001-landing"].validated and flows["ct019-coupon"].validated      # ✅ GREEN counts
    assert flows["ct019-coupon"].cases == {"KIWI-191"}                              # Kiwi case number
    assert flows["ct020-draft"].status == maestro_index.NOT_VALIDATED
    assert flows["ct021-nostatus"].status == maestro_index.NO_STATUS and not flows["ct021-nostatus"].validated
    assert "mock" in flows["ct022-mock"].gate() and "order" in flows["ct023-order"].gate()
    assert flows["cp-01"].requires == "care_plan_user"


@pytest.mark.parametrize("line, status", [
    ("# STATUS: VALIDATED (green) on emulator-5554", maestro_index.VALIDATED),
    ("# STATUS: ✅ VALIDATED (green)", maestro_index.VALIDATED),
    ("# STATUS: GREEN on emulator-5556", maestro_index.VALIDATED),
    ("# STATUS: RE-VALIDATED 2026-11-01", maestro_index.VALIDATED),
    ("# STATUS: UNFROZEN 2026-09-23 — pending re-validation", maestro_index.NOT_VALIDATED),
    ("# STATUS: BLOCKED on account state", maestro_index.NOT_VALIDATED),
    ("# STATUS: NOT runnable", maestro_index.NOT_VALIDATED),
    ("# STATUS: still UNVALIDATED", maestro_index.NOT_VALIDATED),
    ("# nothing here", maestro_index.NO_STATUS),
])
def test_the_flows_own_word_decides(line, status):
    assert maestro_index.status_of(f"# Covers: TC-A-01\n{line}\n# STATUS: VALIDATED later line")[0] == status \
        if "STATUS" in line else maestro_index.status_of(line)[0] == status


def test_flows_for_matches_extid_or_kiwi_number(repo):
    flows = maestro_index.index(repo / ".maestro" / "flows")
    assert [f.name for f in maestro_index.flows_for(flows, "TC-CT-019", store_id="191")] == ["ct019-coupon"]
    assert [f.name for f in maestro_index.flows_for(flows, "KIWI-191")] == ["ct019-coupon"]
    assert maestro_index.flows_for(flows, "TC-CT-019") == []


@pytest.mark.skipif(not REAL_FLOWS.is_dir(), reason="no maestro-automation checkout on this machine")
def test_the_real_repo_indexes():
    flows = maestro_index.index(REAL_FLOWS)
    validated = [f for f in flows.values() if f.validated]
    assert len(validated) > 100
    assert any(c.startswith("KIWI-") for f in flows.values() for c in f.cases)    # the cart flows map


# ---- the store: two surfaces, and where an untagged direct case runs ------------------------------------------


@pytest.mark.parametrize("surface, expected", [
    ("web", ("surface:web",)), ("android", ("surface:android",)), ("both", ("surface:web", "surface:android")),
    ("none", ()),
])
def test_direct_mode_surface_for_an_untagged_case(surface, expected):
    store = KiwiCaseStore(direct=True, direct_surface=surface, rpc=object())
    extid, tags = store._identity(42, ["feature:x"])
    assert extid == "KIWI-42" and tuple(t for t in tags if t.startswith("surface:")) == expected
    _, tagged = store._identity(43, ["surface:android", "exec:ui"])
    assert [t for t in tagged if t.startswith("surface:")] == ["surface:android"]     # never over a tag
    _, both = store._identity(44, ["surface:web", "surface:android"])
    assert {"surface:web", "surface:android"} <= set(both)


# ---- flow or agent, per case -------------------------------------------------------------------------------


def test_plan_per_case(repo):
    flows = maestro_index.index(repo / ".maestro" / "flows")
    cases = [android_case("TC-AND-01"), android_case("TC-CT-019", store_id="191"), android_case("TC-AND-03"),
             android_case("TC-AND-04"), android_case("TC-AND-05"), android_case("TC-AND-06"),
             android_case("TC-NEW-01"), android_case("TC-NEW-02", expected="")]
    got = {p["case"].extid: (p["engine"], p["flow"].name if p["flow"] else None) for p in app_agent.plan(cases, flows)}
    assert got == {"TC-AND-01": ("flow", "ct001-landing"), "TC-CT-019": ("flow", "ct019-coupon"),
                   "TC-AND-03": ("agent", None),              # DRAFT
                   "TC-AND-04": ("agent", None),              # no status line
                   "TC-AND-05": ("not run", None),            # validated but mock-required
                   "TC-AND-06": ("not run", None),            # real order: never in v1
                   "TC-NEW-01": ("agent", None),
                   "TC-NEW-02": ("not run", None)}            # no flow, nothing for an agent to judge
    flow_only = {p["case"].extid: p["engine"] for p in app_agent.plan(cases, flows, "flow")}
    assert flow_only["TC-AND-03"] == "not run" and flow_only["TC-AND-01"] == "flow"
    agent_only = {p["case"].extid: p["engine"] for p in app_agent.plan(cases, flows, "agent")}
    assert agent_only["TC-AND-01"] == "agent" and agent_only["TC-AND-06"] == "not run"


# ---- the draft --------------------------------------------------------------------------------------------


DRAFT = f"appId: {APP}\n---\n- tapOn:\n    id: menu_item_cart\n- takeScreenshot: a-cart\n- assertVisible: Cart\n"


def test_lint_draft():
    assert app_agent.lint_draft(DRAFT, APP) == ([], [])
    errors, _ = app_agent.lint_draft(DRAFT.replace("    id: menu_item_cart", "    point: 50%,50%"), APP)
    assert any("coordinate" in e for e in errors)
    errors, _ = app_agent.lint_draft(f"appId: {APP}\n---\n- tapOn: Cart\n", APP)
    assert any("asserts nothing" in e for e in errors)
    errors, _ = app_agent.lint_draft(DRAFT.replace(APP, "com.other"), APP)
    assert any("appId" in e for e in errors)
    errors, _ = app_agent.lint_draft(DRAFT + "- runScript: x.js\n", APP)
    assert errors
    _, warnings = app_agent.lint_draft(DRAFT.replace("    id: menu_item_cart", "    text: Cart\n    index: 1"), APP)
    assert any("index" in w for w in warnings)


def test_flow_text_is_house_style_and_indexes_back_to_its_case(tmp_path):
    case = android_case("KIWI-191", store_id="191")
    text = app_agent.flow_text(DRAFT, case, run_label="Kiwi run 7", profile="default")
    assert "appId: ${APP_ID}" in text and "takeScreenshot" not in text and "agent-draft" in text
    assert "# STATUS: DRAFT" in text and "# REQUIRES: default" in text
    path = tmp_path / "flows" / "x" / "kiwi-191.yaml"
    path.parent.mkdir(parents=True)
    path.write_text(text)
    flow = maestro_index.read_flow(path)
    assert flow.cases == {"KIWI-191"} and flow.status == maestro_index.NOT_VALIDATED and flow.requires == "default"
    green = conv_mod.mark_validated(text, SERIAL, "2026-11-17")
    path.write_text(green)
    assert maestro_index.read_flow(path).validated


# ---- the stage end to end ----------------------------------------------------------------------------------


def junit(flow: str, ok: bool, label: str = "assert the cart") -> str:
    if ok:
        return (f'<testsuites><testsuite device="{SERIAL}"><testcase id="{flow}" name="{flow}" time="3.0"/>'
                '</testsuite></testsuites>')
    return (f'<testsuites><testsuite device="{SERIAL}"><testcase id="{flow}" name="{flow}" time="4.0">'
            f'<failure message="Element not found: Cart">\n  Label:    "{label}"\n  Command:  assertVisible\n'
            f'</failure></testcase></testsuite></testsuites>')


class FakeAndroid(devices.Android):
    """adb and maestro, played: `outcomes[flow name]` is a list of True/False per run (default pass)."""

    def __init__(self, repo, variant="dev-release", outcomes=None, serials=(SERIAL,), sign_in_code=0):
        super().__init__(repo, variant)
        self.outcomes, self.serials, self.code = outcomes or {}, list(serials), sign_in_code
        self.runs, self.signed = [], []

    def problem(self):
        return ""

    def ready(self, wanted=None):
        return [s for s in self.serials if not wanted or s in wanted]

    def app_version(self, serial):
        return "15.2.0 (1520)"

    def sign_in(self, serial, profile):
        self.signed.append((serial, profile))
        return self.code, "signed in" if self.code == 0 else "OTP is ASK"

    def run_flow(self, flow, serial, out_dir, timeout):
        self.runs.append((flow.stem, serial))
        seq = self.outcomes.get(flow.stem, [True])
        ok = seq.pop(0) if len(seq) > 1 else seq[0]
        out_dir.mkdir(parents=True, exist_ok=True)
        report = out_dir / f"{flow.stem}.xml"
        report.write_text(junit(flow.stem, ok))
        return devices.FlowRun(flow=flow.stem, serial=serial, report=report, exit_code=0 if ok else 1,
                               duration_s=3.0, enriched=True, output="")


DEVICE_CALL = {"name": "mcp__maestro__inspect_screen", "kind": "mcp", "input": {"device_id": SERIAL}}


def agent_pass(extid: str) -> dict:
    return {"extid": extid, "status": "PASS", "reason": "the cart shows the coupon widget",
            "parts": [{"label": "A", "expected": [{"text": "the cart opens", "state": "met", "observed": "Cart",
                                                    "evidence": "a.png", "on_target": True}]}],
            "actions": [{"step": "A1", "action": "tap the cart icon", "saw": "the cart"}],
            "deviations": [], "observations": [], "blocked_on": ""}


class AppAgent:
    """Plays an Android agent session: writes the verdict, its screenshot and (on PASS) a draft."""

    def __init__(self, script=None, draft=DRAFT):
        self.script, self.draft, self.requests = script or {}, draft, []

    def run(self, req):
        from harness.engines import EngineResult
        from harness.events import TraceSummary

        self.requests.append(req)
        folder = Path(req.env["RFC_AGENT_DEVICE_CASE_DIR"])
        name = req.label.split(".", 1)[1]
        step = self.script.get(name.removesuffix(".confirm"))
        if callable(step):
            step(folder, req)
        elif step is not None:
            (folder / "a.png").write_bytes(b"png")
            (folder / ui_agent.VERDICT).write_text(json.dumps(step))
            if step["status"] == "PASS" and self.draft:
                (folder / app_agent.DRAFT).write_text(self.draft)
        return EngineResult(summary=TraceSummary(final_text="done", got_result=True, cost_usd=0.4,
                                                 tool_calls=[DEVICE_CALL] * 3),
                            duration_s=0.1, engine_desc="fake", prompt_sent=req.prompt, trace_path=req.trace_path)


@pytest.fixture
def app_ctx(tmp_path, ctx_factory, repo, clean_cases, approve, monkeypatch):
    """A run with six CONFIRMED cases: two Android cases with flows, one with a draft flow (agent), one new
    (agent), one order-placing (not run), and one web case that is not Android's."""
    ctx = ctx_factory(tmp_path / "run", flags={"testing.maestro_repo": str(repo)},
                      inputs={"feature": "RSE-1", "env": "prestag"})
    rows = []
    for extid, surface in (("TC-AND-01", "android"), ("TC-AND-07", "android"), ("TC-AND-03", "android"),
                           ("TC-NEW-01", "android"), ("TC-AND-06", "android"), ("TC-WEB-01", "web")):
        row = dict(clean_cases[0])
        row.update(id=extid, execution="ui", surface=surface)
        rows.append(row)
    write_json(ctx.path("qa/cases.json"), rows)
    store_cases.STAGE.engine(store_cases.STAGE, ctx)
    approve(ctx.run_dir / "store")
    return ctx


def run_stage(ctx, android, engine) -> dict:
    ctx.engine = engine
    stage_mod.Android = lambda repo, variant: android          # the stage builds its own; hand it ours
    try:
        STAGE.engine(STAGE, ctx)
    finally:
        stage_mod.Android = devices.Android
    return json.loads(ctx.path(stage_mod.RESULTS_OUT).read_text())


def by_id(res):
    return {r["extid"]: r for r in res["results"]}


def stored_run(ctx) -> dict:
    return json.loads(next((ctx.run_dir / "store" / "runs").rglob("*.json")).read_text())


def test_flows_run_as_flows_agents_take_the_rest_and_kiwi_records_both(app_ctx, repo):
    android = FakeAndroid(repo, outcomes={"cp-01": [False]})
    engine = AppAgent({"TC-AND-03": agent_pass("TC-AND-03"), "TC-NEW-01": agent_pass("TC-NEW-01")})
    res = run_stage(app_ctx, android, engine)
    got = by_id(res)

    assert set(got) == {"TC-AND-01", "TC-AND-07", "TC-AND-03", "TC-NEW-01"}          # no web case, no order case
    assert (got["TC-AND-01"]["engine"], got["TC-AND-01"]["status"]) == ("flow", "PASS")
    assert (got["TC-AND-07"]["engine"], got["TC-AND-07"]["status"]) == ("flow", "FAIL")  # reported as it is
    assert got["TC-AND-07"]["profile"] == "care_plan_user" and (SERIAL, "care_plan_user") in android.signed
    assert {r["extid"] for r in res["not_run"]} == {"TC-AND-06"}
    assert got["TC-AND-03"]["engine"] == "agent" and got["TC-NEW-01"]["status"] == "PASS"
    assert sorted(d["extid"] for d in res["drafts"]) == ["TC-AND-03", "TC-NEW-01"]
    assert len(engine.requests) == 2                              # flows cost no session; a flow FAIL is not re-run
    assert sorted(f for f, _ in android.runs) == ["cp-01", "ct001-landing"]
    assert res["environment"] == "prestag" and res["kiwi_environment"] == "android-prestag"
    assert res["build"] == "15.2.0 (1520)"
    assert STAGE.gate(app_ctx) == []

    stored = stored_run(app_ctx)
    ex = stored["executions"]
    assert ex["TC-AND-01"]["props"]["executor"] == "maestro-flow"
    assert ex["TC-AND-01"]["props"]["flow"] == "ct001-landing"
    assert ex["TC-NEW-01"]["props"]["executor"] == "maestro-agent" and ex["TC-NEW-01"]["props"]["device"] == SERIAL
    assert ex["TC-AND-07"]["status"] == "FAIL" and ex["TC-AND-07"]["comments"][0].startswith("## TC-AND-07 — FAIL")
    assert ex["TC-NEW-01"]["comments"][0].startswith("## TC-NEW-01 — PASS")
    assert all(r["execution_id"] for r in res["results"])
    table = app_ctx.path(stage_mod.TABLE_OUT).read_text()
    assert "| Case | Engine | Verdict | Time | Cost | What was found |" in table and "Not run:" in table


def test_an_agent_gets_one_device_one_app_and_the_guard(app_ctx, repo):
    from rfc_agent.hooks import guard_device as gd

    engine = AppAgent({"TC-AND-03": agent_pass("TC-AND-03"), "TC-NEW-01": agent_pass("TC-NEW-01")})
    run_stage(app_ctx, FakeAndroid(repo), engine)
    req = next(r for r in engine.requests if r.label.endswith("TC-NEW-01"))
    folder = app_agent.case_dir(app_ctx.run_dir, "TC-NEW-01")
    assert req.env[gd.SERIAL_ENV] == SERIAL and req.env[gd.APP_ID_ENV] == APP
    assert req.env[gd.CASE_DIR_ENV] == str(folder) and req.env[gd.VERDICT_FILE_ENV] == str(folder / ui_agent.VERDICT)
    assert req.env[gd.SUBFLOWS_ENV] == str(repo / ".maestro" / "flows") and req.env[gd.FORBID_ENV]
    spec = req.containment.mcp_servers["maestro"]
    assert spec["command"] == "/bin/sh" and spec["args"][0] == "-c"
    assert spec["args"][1].startswith(f"cd {folder} && exec ") and " mcp --no-viewer " in spec["args"][1]
    assert req.native_ctx["stage"].mcp_servers == ("maestro",)
    assert f'device_id "{SERIAL}"' in req.prompt and "draft.yaml" in req.prompt and "assertVisible" in req.prompt


def test_flow_mode_runs_no_agent_and_an_account_that_cannot_sign_in_is_blocked(app_ctx, repo):
    app_ctx.flags._v["testing.android_mode"] = "flow"
    android = FakeAndroid(repo, sign_in_code=devices.SKIPPED_EXIT)
    engine = AppAgent()
    res = run_stage(app_ctx, android, engine)
    got = by_id(res)
    assert engine.requests == [] and set(got) == {"TC-AND-01", "TC-AND-07"}
    assert got["TC-AND-07"]["status"] == "BLOCKED" and "unattended" in got["TC-AND-07"]["reason"]
    assert {r["extid"] for r in res["not_run"]} == {"TC-AND-03", "TC-NEW-01", "TC-AND-06"}


def test_a_bad_draft_is_rejected_and_a_failing_agent_case_leaves_none(app_ctx, repo):
    failing = agent_pass("TC-NEW-01") | {"status": "FAIL", "cause": "product"}
    failing["parts"][0]["expected"][0]["state"] = "not met"
    app_ctx.flags._v["testing.agent_confirm_fail"] = False
    engine = AppAgent({"TC-AND-03": agent_pass("TC-AND-03"), "TC-NEW-01": failing},
                      draft=f"appId: {APP}\n---\n- tapOn:\n    point: 10,10\n- assertVisible: Cart\n")
    res = run_stage(app_ctx, FakeAndroid(repo), engine)
    got = by_id(res)
    assert got["TC-AND-03"]["draft"]["state"] == "rejected" and "coordinate" in got["TC-AND-03"]["draft"]["why"]
    assert got["TC-NEW-01"]["status"] == "FAIL" and got["TC-NEW-01"]["draft"] == {"state": "none"}
    assert res["drafts"] == []


def test_no_device_is_an_engine_failure_naming_it(app_ctx, repo):
    from harness.contracts import EngineFailure

    with pytest.raises(EngineFailure, match="no Android device is ready"):
        run_stage(app_ctx, FakeAndroid(repo, serials=()), AppAgent())


def test_a_flow_covering_one_case_that_fails_failed_that_case(repo):
    flows = maestro_index.index(repo / ".maestro" / "flows")
    from workflows.testing import maestro

    report = repo / "r.xml"
    report.write_text(junit("ct019-coupon", ok=False))
    run = maestro.parse_report(report)[0]
    case = android_case("TC-CT-019", store_id="191")
    got = stage_mod.flow_verdicts(run, flows["ct019-coupon"], [case], None)["TC-CT-019"]
    assert got["status"] == "FAIL" and "assert the cart" in got["reason"] and got["cause"] == "needs triage"


# ---- convert_flows -----------------------------------------------------------------------------------------


def executed(app_ctx, repo, android=None):
    engine = AppAgent({"TC-AND-03": agent_pass("TC-AND-03"), "TC-NEW-01": agent_pass("TC-NEW-01")})
    run_stage(app_ctx, android or FakeAndroid(repo), engine)


def run_convert(ctx, android, engine) -> dict:
    ctx.engine = engine
    conv_mod.Android = lambda repo, variant: android
    try:
        conv_mod.STAGE.engine(conv_mod.STAGE, ctx)
    finally:
        conv_mod.Android = devices.Android
    return {r["extid"]: r for r in json.loads(ctx.path(conv_mod.RESULTS_OUT).read_text())["results"]}


class Fixer:
    """Plays a fix round: `edit(flow_path)` changes the flow (or not)."""

    def __init__(self, edit=None):
        self.edit, self.requests = edit, []

    def run(self, req):
        from harness.engines import EngineResult
        from harness.events import TraceSummary

        self.requests.append(req)
        if self.edit:
            self.edit(Path(req.env["RFC_AGENT_DEVICE_CASE_DIR"]) / app_agent.FLOW)
        return EngineResult(summary=TraceSummary(final_text="fixed", got_result=True, cost_usd=0.2, tool_calls=[]),
                            duration_s=0.1, engine_desc="fake", prompt_sent=req.prompt, trace_path=req.trace_path)


def test_convert_green_first_time_needs_no_model_and_comments_once(app_ctx, repo):
    executed(app_ctx, repo)
    fixer = Fixer()
    got = run_convert(app_ctx, FakeAndroid(repo), fixer)
    assert {k: v["state"] for k, v in got.items()} == {"TC-AND-03": "green", "TC-NEW-01": "green"}
    assert fixer.requests == []
    flow = Path(got["TC-NEW-01"]["flow"])
    assert maestro_index.read_flow(flow).validated and "TC-NEW-01" in maestro_index.read_flow(flow).cases
    ex = stored_run(app_ctx)["executions"]["TC-NEW-01"]
    assert ex["status"] == "PASS"                                     # the verdict is untouched
    assert ex["comments"][-1].startswith("## TC-NEW-01 — draft flow: a validated flow is ready")
    assert conv_mod.STAGE.gate(app_ctx) == []


def test_convert_fixes_in_bounded_rounds_and_stops_without_progress(app_ctx, repo):
    executed(app_ctx, repo)
    app_ctx.flags._v["testing.cases"] = "TC-NEW-01"
    # fails, the fixer edits, fails at the same step again -> stuck after one round
    android = FakeAndroid(repo, outcomes={"flow": [False, False, False, False]})
    fixer = Fixer(edit=lambda p: p.write_text(p.read_text() + "- waitForAnimationToEnd\n"))
    got = run_convert(app_ctx, android, fixer)
    assert got["TC-NEW-01"]["state"] == "stuck" and "no progress" in got["TC-NEW-01"]["why"]
    assert len(fixer.requests) == 1 and set(got) == {"TC-NEW-01"}
    assert all("Kiwi" not in str(r) for r in android.runs)
    # fails once, the fix lands, green
    android = FakeAndroid(repo, outcomes={"flow": [False, True]})
    got = run_convert(app_ctx, android, Fixer(edit=lambda p: p.write_text(p.read_text() + "- back\n")))
    assert got["TC-NEW-01"]["state"] == "green" and got["TC-NEW-01"]["rounds"] == 1


def test_convert_with_a_fixer_that_changes_nothing_is_stuck(app_ctx, repo):
    executed(app_ctx, repo)
    app_ctx.flags._v["testing.cases"] = "TC-AND-03"
    got = run_convert(app_ctx, FakeAndroid(repo, outcomes={"flow": [False]}), Fixer())
    assert got["TC-AND-03"]["state"] == "stuck" and "changed nothing" in got["TC-AND-03"]["why"]


def test_promote_shows_then_copies_onto_a_worktree_branch(app_ctx, repo, tmp_path):
    executed(app_ctx, repo)
    run_convert(app_ctx, FakeAndroid(repo), Fixer())
    plan = conv_mod.promote(app_ctx.run_dir, repo, feature_folder="Cart", today="20261117")
    assert plan["applied"] is False and plan["branch"] == "agent-flows/cart-20261117"
    assert sorted(Path(f["to"]).name for f in plan["files"]) == ["tc-and-03.yaml", "tc-new-01.yaml"]
    assert all("/.maestro/flows/cart/agent/" in f["to"] for f in plan["files"])


# ---- the command line --------------------------------------------------------------------------------------


@pytest.fixture
def run_dir(tmp_path):
    d = tmp_path / "2026-11-17-testing-CART"
    (d / "qa").mkdir(parents=True)
    (d / "qa" / "sources.json").write_text(json.dumps({"feature": "CART", "rfc_run_dir": "", "sources": [],
                                                       "origin": execute.DIRECT, "kiwi_plan_id": "3"}))
    return d


def test_android_command_needs_no_build_and_passes_its_options(run_dir):
    cmd = execute.command("android", run_dir, env="prestag", untagged="both", android_mode="flow",
                          app_variant="debug", devices="emulator-5554,emulator-5556", maestro_repo="/r")
    s = " ".join(cmd)
    assert "--stages execute_app" in s and "--enable testing.execute_app" in s and "--build from-device" in s
    assert "testing.kiwi_direct_surface=both" in s and "testing.android_mode=flow" in s
    assert "testing.app_variant=debug" in s and "testing.devices=emulator-5554,emulator-5556" in s
    assert "testing.maestro_repo=/r" in s and "testing.kiwi_direct" in s


def test_convert_command_and_platforms(run_dir):
    s = " ".join(execute.command("convert", run_dir, env="prestag", devices="emulator-5554"))
    assert "--stages convert_flows" in s and "--enable testing.convert_flows" in s
    assert execute.PLATFORMS["both"] == ("ui-agent", "android") and execute.PLATFORMS["web"] == ("ui-agent",)
    assert execute.UNTAGGED["skip"] == "none"


def test_ui_agent_platform_both_launches_web_then_android(run_dir, monkeypatch, capsys):
    monkeypatch.setattr(execute, "load_config", lambda: None)
    monkeypatch.setattr(execute, "resolved_flags", lambda cfg: None)
    code = execute.main(["ui-agent", "--run-dir", str(run_dir), "--env", "prestag", "--build", "b1",
                         "--platform", "both", "--untagged", "skip", "--base-url", "https://web.test", "--print"])
    out = capsys.readouterr().out.strip().splitlines()
    assert code == 0 and len(out) == 2
    assert "--stages execute_ui_agent" in out[0] and "--stages execute_app" in out[1]
    assert all("testing.kiwi_direct_surface=none" in line for line in out)


def test_adb_and_dumpsys_parsing():
    assert devices.parse_adb_devices("List of devices attached\nemulator-5554\tdevice\nemulator-5556\toffline\n"
                                     "R58\tunauthorized\n") == ["emulator-5554"]
    assert devices.parse_version("    versionCode=1520 minSdk=24\n    versionName=15.2.0\n") == "15.2.0 (1520)"
    assert devices.parse_version("Unable to find package") == ""


def test_env_prod_is_production_whatever_the_build(app_ctx, repo):
    from harness.contracts import EngineFailure

    app_ctx.inputs["env"] = "prod"
    with pytest.raises(EngineFailure, match="PRODUCTION"):
        run_stage(app_ctx, FakeAndroid(repo, variant="debug"), AppAgent())
    app_ctx.flags._v["testing.agent_production"] = True
    with pytest.raises(EngineFailure, match="TEST account"):
        run_stage(app_ctx, FakeAndroid(repo, variant="debug"), AppAgent())


def test_android_on_production_passes_the_test_account_statement(run_dir, monkeypatch, capsys):
    monkeypatch.setattr(execute, "load_config", lambda: None)
    monkeypatch.setattr(execute, "resolved_flags", lambda cfg: None)
    execute.main(["android", "--run-dir", str(run_dir), "--env", "prod", "--production", "--test-account",
                  "--app-variant", "debug", "--print"])
    line = capsys.readouterr().out.strip().splitlines()[-1]
    assert "testing.agent_production" in line and "testing.agent_test_account" in line


# ---- device roles ----------------------------------------------------------------------------------------

def test_device_roles_from_the_manifest_or_the_flag():
    manifest = devices.parse_manifest("- id: dev-1\n  serial: emulator-5554\n  role: guest\n"
                                      "# - id: dev-9\n#   serial: emulator-5570\n#   role: pool\n"
                                      "- id: dev-2\n  serial: emulator-5556\n  role: care_plan_user\n")
    assert manifest == {"emulator-5554": "guest", "emulator-5556": "care_plan_user"}
    assert devices.assign_roles(["emulator-5554"], manifest, {}) == {"emulator-5554": "pool"}   # a lone emulator
    assert devices.assign_roles(["emulator-5554", "emulator-5558"], manifest, {}) == \
        {"emulator-5554": "guest", "emulator-5558": "pool"}
    override = devices.parse_role_flag(" emulator-5554=care_plan_user , junk")
    assert devices.assign_roles(["emulator-5554"], manifest, override) == {"emulator-5554": "care_plan_user"}
    assert devices.accepts("guest", "") and not devices.accepts("guest", "care_plan_user")
    assert devices.accepts("care_plan_user", "care_plan_user") and not devices.accepts("care_plan_user", "")
    assert devices.accepts("pool", "") and devices.accepts("pool", "family_user")


def test_guest_and_logged_in_cases_go_to_their_own_devices_without_signing_in(app_ctx, repo):
    app_ctx.flags._v["testing.android_mode"] = "flow"
    app_ctx.flags._v["testing.device_roles"] = "emulator-5554=guest,emulator-5556=care_plan_user"
    android = FakeAndroid(repo, serials=("emulator-5554", "emulator-5556"))
    res = run_stage(app_ctx, android, AppAgent())
    got = by_id(res)
    assert (got["TC-AND-01"]["device"], got["TC-AND-07"]["device"]) == ("emulator-5554", "emulator-5556")
    assert android.signed == []                       # the care_plan_user device is kept signed in by hand
    assert STAGE.gate(app_ctx) == []


def test_a_case_no_device_role_accepts_is_not_run_and_says_why(app_ctx, repo):
    app_ctx.flags._v["testing.android_mode"] = "flow"
    app_ctx.flags._v["testing.device_roles"] = f"{SERIAL}=guest"
    res = run_stage(app_ctx, FakeAndroid(repo), AppAgent())
    assert set(by_id(res)) == {"TC-AND-01"}
    why = {r["extid"]: r["reason"] for r in res["not_run"]}["TC-AND-07"]
    assert "'care_plan_user' account" in why and f"{SERIAL}=guest" in why


def test_a_pool_device_leaves_a_dedicated_devices_work_to_it():
    flows = {"g": maestro_index.Flow(name="g", path=Path("g.yaml")),
             "cp": maestro_index.Flow(name="cp", path=Path("cp.yaml"), requires="care_plan_user")}
    work = [("flow", flows["cp"], []), ("flow", flows["g"], [])]
    roles = {"a": "pool", "b": "care_plan_user"}
    assert stage_mod.take(work, roles, "a", flows)[1].name == "g"
    assert stage_mod.take(work, roles, "a", flows)[1].name == "cp"     # nothing else left for it
    assert stage_mod.take(work, roles, "b", flows) is None


def test_each_run_keeps_a_dated_copy_of_its_results(app_ctx, repo):
    app_ctx.flags._v["testing.android_mode"] = "flow"
    run_stage(app_ctx, FakeAndroid(repo), AppAgent())
    folder = app_ctx.path(stage_mod.RESULTS_OUT).parent
    assert len(list(folder.glob("results.2*.json"))) == 1 and len(list(folder.glob("results.2*.md"))) == 1
