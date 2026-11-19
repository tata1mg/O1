"""`execute ui-agent` and the Kiwi-direct run: a Kiwi plan run with no generated suite before it."""

from __future__ import annotations

import json
from datetime import date
from types import SimpleNamespace

import pytest

from workflows.testing import execute


def _write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data))


def _generated_run(tmp_path, feature="F-1"):
    run = tmp_path / "2026-11-05-testing-F-1"
    _write(run / "qa/sources.json", {"feature": feature, "rfc_run_dir": "/rfc"})
    _write(run / "qa/ui/results.json", {"environment": "jupiter", "build": "b7"})
    return run


def test_ui_agent_command_has_its_own_stage_and_no_derive_flags(tmp_path):
    run = _generated_run(tmp_path)
    cmd = execute.command("ui-agent", run)
    joined = " ".join(cmd)
    assert "--stages execute_ui_agent " in joined and "--enable testing.execute_ui_agent" in joined
    assert "testing.ui_headed" not in joined and "ui_only_new" not in joined and "--curl" not in cmd
    assert "--rfc-run-dir /rfc" in joined and "kiwi_direct" not in joined
    assert "--enable testing.ui_headed" in " ".join(execute.command("ui-agent", run, headed=True))
    only = " ".join(execute.command("ui-agent", run, only_existing=True, cases="TC-1,TC-2"))
    assert "ui_only_new" not in only and "--set testing.cases=TC-1,TC-2" in only


def test_all_stays_web_and_backend():
    assert execute.ALL_KINDS == ("web", "backend") and "ui-agent" in execute.KINDS


def test_bootstrap_creates_once_and_reuses(tmp_path):
    run = execute.bootstrap_run(tmp_path, "F-9", "42", today=date(2026, 11, 16))
    assert run == tmp_path / "2026-11-16-testing-F-9" and (run / "logs").is_dir()
    sources = json.loads((run / "qa/sources.json").read_text())
    assert sources == {"feature": "F-9", "rfc_run_dir": "", "sources": [], "origin": "kiwi-direct",
                       "kiwi_plan_id": "42"}
    assert execute.bootstrap_run(tmp_path, "F-9", "42", today=date(2026, 12, 1)) == run
    assert len(list(tmp_path.glob("*-testing-*"))) == 1
    execute.bootstrap_run(tmp_path, "F-9", "43")
    assert json.loads((run / "qa/sources.json").read_text())["kiwi_plan_id"] == "43"


def test_bootstrap_reuses_a_generated_run_untouched(tmp_path):
    run = _generated_run(tmp_path)
    before = (run / "qa/sources.json").read_text()
    assert execute.bootstrap_run(tmp_path, "F-1", "42") == run
    assert (run / "qa/sources.json").read_text() == before
    assert execute.bootstrap_run(tmp_path, "F-other", "42") != run


def test_bootstrapped_command_is_direct_without_rfc_run(tmp_path):
    run = execute.bootstrap_run(tmp_path, "F-9", "42")
    cmd = execute.command("ui-agent", run, env="e", build="b")
    joined = " ".join(cmd)
    assert "--rfc-run-dir" not in cmd and "--feature F-9" in joined
    assert "--enable testing.kiwi_direct" in joined and "--set testing.kiwi_plan_id=42" in joined
    no_plan = " ".join(execute.command("ui-agent", execute.bootstrap_run(tmp_path, "F-8"), env="e", build="b"))
    assert "kiwi_direct" in no_plan and "kiwi_plan_id" not in no_plan


@pytest.mark.parametrize("kind", ["web", "backend"])
def test_generated_suite_kinds_refuse_a_direct_run(tmp_path, kind):
    run = execute.bootstrap_run(tmp_path, "F-9", "42")
    with pytest.raises(SystemExit, match="use `ui-agent`"):
        execute.command(kind, run, env="e", build="b", curl="c")


def test_kiwi_plan_needs_a_feature(tmp_path, monkeypatch):
    monkeypatch.setattr(execute, "load_config", lambda: {"RUNS_ROOT": str(tmp_path)})
    with pytest.raises(SystemExit, match="--feature"):
        execute.main(["ui-agent", "--kiwi-plan", "42", "--print"])


def test_feature_bootstraps_the_run_in_main(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(execute, "load_config", lambda: {"RUNS_ROOT": str(tmp_path)})
    monkeypatch.setattr(execute, "resolved_flags", lambda cfg=None: SimpleNamespace(get=lambda n, d=None: d))
    assert execute.main(["ui-agent", "--feature", "F-9", "--kiwi-plan", "42", "--env", "e", "--build", "b",
                         "--print"]) == 0
    assert "testing.kiwi_plan_id=42" in capsys.readouterr().out
    assert len(list(tmp_path.glob("*-testing-F-9"))) == 1


class _Store:
    def __init__(self, cases):
        self.cases = cases

    def list_cases(self, feature, *, status=None, tags=()):
        return [c for c in self.cases if all(t in c.tags for t in tags)]


def test_pending_for_ui_agent_counts_what_will_run(tmp_path, monkeypatch):
    from workflows.testing import preflight
    from workflows.testing.store import Case

    def case(extid, tags, expected):
        text = f"**Steps**\n1. Open\n\n**Expected Result**\n{expected}\n" if expected else "**Steps**\n1. Open\n"
        return Case(extid=extid, text=text, tags=tuple(tags))

    store = _Store([case("A", ("exec:ui", "surface:web"), "It shows"), case("B", ("exec:ui", "surface:web"), ""),
                    case("C", ("exec:http", "surface:web"), "ok"), case("D", ("exec:http",), "ok")])
    seen = {}
    monkeypatch.setattr(execute, "load_config", lambda: {})
    monkeypatch.setattr(execute, "resolved_flags", lambda cfg=None: SimpleNamespace(get=lambda n, d=None: d))
    monkeypatch.setattr(preflight, "store_for", lambda flags, run_dir: seen.update(flags=flags) or store)
    run = execute.bootstrap_run(tmp_path, "F-9", "42")
    out = execute.pending_agent(run)
    assert out["error"] == "" and out["skipped_backend"] == 2
    assert out["no_expected_result"] == ["B"] and out["will_run"] == ["A"]
    assert seen["flags"].get("testing.kiwi_plan_id") == "42" and seen["flags"].get("testing.kiwi_direct") is True
    assert out["manual_included"] is False and out["manual_left"] == []


def test_pending_names_the_manual_cases_an_agent_could_run_and_takes_them_when_asked(tmp_path, monkeypatch):
    from workflows.testing import preflight
    from workflows.testing.store import Case

    text = "**Steps**\n1. Open\n\n**Expected Result**\nIt shows\n"
    store = _Store([Case(extid="M", text=text, tags=("exec:manual", "surface:web")),
                    Case(extid="N", text=text, tags=("exec:manual",))])     # no surface: no agent can take it
    monkeypatch.setattr(execute, "load_config", lambda: {})
    monkeypatch.setattr(execute, "resolved_flags", lambda cfg=None: SimpleNamespace(get=lambda n, d=None: d))
    monkeypatch.setattr(preflight, "store_for", lambda flags, run_dir: store)
    run = execute.bootstrap_run(tmp_path, "F-9", "42")
    left = execute.pending_agent(run)
    assert left["will_run"] == [] and left["manual_left"] == ["M"] and left["manual_included"] is False
    taken = execute.pending_agent(run, include_manual=True)
    assert taken["will_run"] == ["M"] and taken["manual_left"] == [] and taken["manual_included"] is True


def test_pending_for_ui_agent_reports_errors_not_raises(tmp_path, monkeypatch):
    from workflows.testing import preflight

    def boom(flags, run_dir):
        raise RuntimeError("no kiwi")

    monkeypatch.setattr(execute, "load_config", lambda: {})
    monkeypatch.setattr(execute, "resolved_flags", lambda cfg=None: SimpleNamespace(get=lambda n, d=None: d))
    monkeypatch.setattr(preflight, "store_for", boom)
    assert execute.pending_agent(execute.bootstrap_run(tmp_path, "F-9"))["error"] == "RuntimeError: no kiwi"


def test_the_browser_settings_can_be_given_on_the_command_line(tmp_path):
    """Dry run, 2026-11-16: `execute ui-agent` had no way to name the host, the address or the session."""
    from workflows.testing import execute

    run = execute.bootstrap_run(tmp_path, "DRY-1")
    cmd = execute.command("ui-agent", run, env="prestag", build="b1", base_url="https://web.test",
                          ui_auth=str(tmp_path / "state.json"), hosts="web.test", browser="chrome", parallel=2)
    joined = " ".join(cmd)
    assert "--base-url https://web.test" in joined and f"--ui-auth {tmp_path / 'state.json'}" in joined
    assert "--set testing.agent_hosts=web.test" in joined and "--set testing.agent_browser=chrome" in joined
    assert "--set testing.agent_parallel=2" in joined


def test_production_is_one_flag_and_a_logged_in_run_carries_the_confirmation(tmp_path):
    from workflows.testing import execute

    run = execute.bootstrap_run(tmp_path, "PROD-1")
    guest = " ".join(execute.command("ui-agent", run, env="prod", build="b1", production=True))
    assert "--enable testing.agent_production" in guest and "agent_test_account" not in guest
    member = " ".join(execute.command("ui-agent", run, env="prod", build="b1", production=True, test_account=True))
    assert "--enable testing.agent_production" in member and "--enable testing.agent_test_account" in member
    assert "agent_production" not in " ".join(execute.command("ui-agent", run, env="prod", build="b1"))


def test_a_logged_in_production_run_asks_whether_the_account_is_a_test_account():
    import pytest

    from workflows.testing.execute import confirm_production

    kw = {"logged_in": True, "base_url": "https://www.example.test"}
    assert confirm_production(False, False, **kw) is False                              # not production: no question
    assert confirm_production(True, False, logged_in=False, base_url="x") is False     # a guest: no account
    assert confirm_production(True, True, **kw) is True                                 # answered in advance
    asked = []
    assert confirm_production(True, False, **kw, interactive=True, ask=lambda q: asked.append(q) or "y") is True
    assert "PRODUCTION (https://www.example.test)" in asked[0] and "TEST account" in asked[0]
    with pytest.raises(SystemExit, match="not confirmed as a test account"):
        confirm_production(True, False, **kw, interactive=True, ask=lambda q: "")       # Enter = no
    with pytest.raises(SystemExit, match="adding --test-account"):
        confirm_production(True, False, **kw, interactive=False)                        # nobody to ask


# ---- plain words: match_plan, match_cases, lookup ---------------------------------------------------------

PLANS = [{"id": 3, "name": "CART-SANITY", "product": "Pharmacy"}, {"id": 4, "name": "CART", "product": "Pharmacy"},
         {"id": 5, "name": "LOGIN-SANITY", "product": "Core"}]


@pytest.mark.parametrize("text", ["cart sanity", "CART_SANITY", "cart-sanity", " Cart  Sanity "])
def test_match_plan_ignores_case_and_separators(text):
    assert [p["id"] for p in execute.match_plan(text, PLANS)] == [3]


@pytest.mark.parametrize("text", ["3", "TP-3", "plan 3", "tp3"])
def test_match_plan_by_id(text):
    assert [p["id"] for p in execute.match_plan(text, PLANS)] == [3]
    assert execute.match_plan("99", PLANS) == []


def test_match_plan_ambiguous_exact_and_none():
    assert [p["id"] for p in execute.match_plan("sanity", PLANS)] == [3, 5]        # every candidate
    assert [p["id"] for p in execute.match_plan("cart", PLANS)] == [4]             # exact beats substring
    assert execute.match_plan("checkout", PLANS) == [] and execute.match_plan("", PLANS) == []


CASES = [{"id": "KIWI-173", "summary": "[CT002] Verify the selected address", "status": "CONFIRMED"},
         {"id": "KIWI-172", "summary": "[CT001] Verify the cart", "status": "CONFIRMED"},
         {"id": "TC-CT-04", "summary": "no code here", "status": "CONFIRMED"},
         {"id": "TC-CT-05", "summary": "[CT-5] dashed code", "status": "PROPOSED"}]


def test_match_cases_by_id_number_and_summary_code():
    assert execute.match_cases(["173"], CASES) == (["KIWI-173"], [])
    assert execute.match_cases(["kiwi-172"], CASES) == (["KIWI-172"], [])
    assert execute.match_cases(["CT002", "ct1"], CASES) == (["KIWI-173", "KIWI-172"], [])
    assert execute.match_cases(["ct2"], CASES) == (["KIWI-173"], [])
    assert execute.match_cases(["CT-04", "ct05"], CASES) == (["TC-CT-04", "TC-CT-05"], [])
    assert execute.match_cases(["999", "nope"], CASES) == ([], ["999", "nope"])


def test_match_cases_never_guesses_and_keeps_order():
    twin = [*CASES, {"id": "XX-CT-04", "summary": "", "status": "CONFIRMED"}]
    assert execute.match_cases(["CT-04"], twin) == ([], ["CT-04"])                  # two ids end that way
    assert execute.match_cases(["172", "173", "172", "173"], CASES) == (["KIWI-172", "KIWI-173"], [])
    assert execute.match_cases(["173", "999", "ct1"], CASES) == (["KIWI-173", "KIWI-172"], ["999"])


class _Rpc:
    def __init__(self, fail=False):
        self.fail = fail
        self.Product = SimpleNamespace(filter=lambda q: [{"id": 1, "name": "Pharmacy"}, {"id": 2, "name": "Core"}])
        self.TestPlan = SimpleNamespace(filter=self._plans)

    def _plans(self, query):
        if self.fail:
            raise OSError("kiwi unreachable")
        return [{"id": 3, "name": "CART-SANITY", "product": 1}, {"id": 5, "name": "LOGIN", "product": 2}]


class _KiwiStore:
    def __init__(self, fail=False):
        self.rpc = _Rpc(fail)
        self.asked = []

    def _rpc(self):
        return self.rpc

    def list_cases(self, feature, *, status=None, tags=()):
        self.asked.append((feature, status, tags))
        return [SimpleNamespace(extid="KIWI-173", summary="[CT002] Verify", status="PROPOSED", text="", tags=())]


def _flags(tmp_path, **over):
    values = {"testing.storage": "kiwi", "testing.environments": "dione, earth", "testing.base_url_template":
              "https://{env}.example", "testing.production_base_url": "https://www.example.com",
              "testing.sessions_dir": str(tmp_path / "sessions"), "testing.env": "earth", "testing.build": "b1",
              "testing.agent_browser": "chrome", **over}
    return SimpleNamespace(get=lambda name, default=None: values.get(name, default))


def test_lookup_lists_plans_cases_environments_and_defaults(tmp_path):
    store = _KiwiStore()
    out = execute.lookup(_flags(tmp_path), tmp_path, "3", store=store)
    assert out["plans"] == [{"id": 3, "name": "CART-SANITY", "product": "Pharmacy"},
                            {"id": 5, "name": "LOGIN", "product": "Core"}]
    assert out["cases"] == [{"id": "KIWI-173", "summary": "[CT002] Verify", "status": "PROPOSED"}]
    assert store.asked == [("", None, ())]                                          # every status, no tag filter
    assert out["environments"] == ["dione", "earth"] and out["base_url_template"] == "https://{env}.example"
    assert out["production"] == {"env": "prod", "base_url": "https://www.example.com"}
    assert out["defaults"] == {"env": "earth", "build": "b1", "browser": "chrome"}
    assert out["today"] == date.today().isoformat() and out["error"] == ""
    json.dumps(out)
    assert execute.lookup(_flags(tmp_path), tmp_path, store=_KiwiStore())["cases"] == []   # no plan -> no cases
    assert execute.lookup(_flags(tmp_path, **{"testing.production_base_url": ""}), tmp_path,
                          store=_KiwiStore())["production"]["base_url"] == ""


def test_lookup_json_store_has_no_plans(tmp_path):
    out = execute.lookup(_flags(tmp_path, **{"testing.storage": "json"}), tmp_path)
    assert out["plans"] == [] and "JSON" in out["error"] and out["environments"] == ["dione", "earth"]


def test_lookup_reports_errors_instead_of_raising(tmp_path):
    assert "kiwi unreachable" in execute.lookup(_flags(tmp_path), tmp_path, store=_KiwiStore(fail=True))["error"]
    assert "not a number" in execute.lookup(_flags(tmp_path), tmp_path, "abc", store=_KiwiStore())["error"]


def test_lookup_sessions_are_names_only_and_never_opened(tmp_path, monkeypatch):
    folder = tmp_path / "sessions"
    folder.mkdir()
    secret = folder / "a-state.json"
    secret.write_text('{"cookies": "SECRET"}')
    (folder / "notes.txt").write_text("not a session")
    secret.chmod(0)                                                                 # opening it would fail
    real_open = type(secret).open

    def guard(self, *a, **k):
        assert self.suffix != ".json", f"opened {self}"
        return real_open(self, *a, **k)

    monkeypatch.setattr(type(secret), "open", guard)
    monkeypatch.setattr(type(secret), "read_text", lambda self, *a, **k: pytest.fail(f"read {self}"))
    try:
        out = execute.lookup(_flags(tmp_path), tmp_path, store=_KiwiStore())
    finally:
        secret.chmod(0o600)
    assert out["sessions"] == [{"name": "a-state", "path": str(secret)}]
    assert execute.lookup(_flags(tmp_path, **{"testing.sessions_dir": str(tmp_path / "none")}), tmp_path,
                          store=_KiwiStore())["sessions"] == []


def test_lookup_and_resolve_cases_on_the_command_line(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(execute, "load_config", lambda: {"RUNS_ROOT": str(tmp_path)})
    monkeypatch.setattr(execute, "resolved_flags", lambda cfg: _flags(tmp_path))
    fake = _KiwiStore()
    import workflows.testing.preflight as preflight
    monkeypatch.setattr(preflight, "store_for", lambda flags, run_dir: fake)
    assert execute.main(["--lookup", "--plan", "3"]) == 0
    assert json.loads(capsys.readouterr().out)["cases"][0]["id"] == "KIWI-173"
    assert execute.main(["--resolve-cases", "173, ct2, 999", "--plan", "3"]) == 0
    assert json.loads(capsys.readouterr().out) == {"matched": ["KIWI-173"], "unknown": ["999"], "error": ""}


def test_production_and_sessions_flags_exist_with_their_defaults():
    from harness.flags import registry
    from workflows.testing.flags import NAMES

    assert "testing.production_base_url" in NAMES and "testing.sessions_dir" in NAMES
    assert registry()["testing.production_base_url"].default == ""
    assert registry()["testing.sessions_dir"].default == "~/.secrets/playwright"


def test_allow_reaches_the_agent_allow_flag(tmp_path):
    cmd = execute.command("ui-agent", _generated_run(tmp_path), allow="coupons,neucoins")
    assert "testing.agent_allow=coupons,neucoins" in cmd


def test_include_manual_reaches_both_agents_and_nothing_else(tmp_path):
    run = _generated_run(tmp_path)
    for kind in ("ui-agent", "android"):
        assert "testing.agent_include_manual" in execute.command(kind, run, env="e", build="b",
                                                                 include_manual=True)
    assert "testing.agent_include_manual" not in execute.command("ui-agent", run)
