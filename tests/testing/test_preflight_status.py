"""`/test-agent:test`'s helpers: the config layer, `preflight` and `status`.

Everything here is offline: Kiwi is a fake store, config.json a temp file. What these tests cannot cover is
the command prompt itself (plugins/test-agent/commands/test.md) — that is exercised by running it.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from workflows.testing import config_get, preflight, status
from workflows.testing.store import Case

RFC = """# RFC: Thing

## 5. API contract

### POST /v1/thing

## 6. Implementation plan

### CHANGE-1 svc: do it [REQ-1]
"""


class Cfg:
    def __init__(self, data: dict) -> None:
        self.data = data

    def get(self, dotted: str, default=None):
        node = self.data
        for part in dotted.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node


@pytest.fixture
def rfc_run(tmp_path) -> Path:
    run = tmp_path / "rfc-run"
    (run / "out").mkdir(parents=True)
    (run / "raw").mkdir()
    (run / "out/rfc.md").write_text(RFC)
    (run / "raw/prd.md").write_text("# PRD\n")
    return run


def by_id(checks: list[dict]) -> dict[str, dict]:
    return {c["id"]: c for c in checks}


# ---- the config layer ----------------------------------------------------------------------------------


def test_testing_flags_come_from_the_testing_section():
    get = config_get(Cfg({"TESTING": {"storage": "kiwi", "kiwi_web": "https://kiwi.test"}}))
    assert get("testing.storage") == "kiwi"
    assert get("testing.kiwi_web") == "https://kiwi.test"
    assert get("testing.product_prefix") is None
    assert get("engine") is None                       # not ours


def test_the_kiwi_password_never_comes_from_config():
    get = config_get(Cfg({"TESTING": {"kiwi_password": "hunter2"}}))
    assert get("testing.kiwi_password") is None


def test_a_resume_keeps_the_store_the_config_names():
    """The NR-DIGICHEM-v2 regression: a rerun without `--set testing.storage=kiwi` switched to JSON."""
    flags = preflight.resolved_flags(Cfg({"TESTING": {"storage": "kiwi"}}))
    assert flags.get("testing.storage") == "kiwi"
    assert flags.explain("testing.storage").source == "config"


# ---- preflight: sources ---------------------------------------------------------------------------------


def test_contract_and_plan_are_found_in_the_rfc_sections(rfc_run):
    checks = by_id(preflight.check_sources("F-1", str(rfc_run), {}))
    assert checks["rfc"]["level"] == checks["prd"]["level"] == "ok"
    assert checks["contract"]["level"] == "ok" and "§ API contract" in checks["contract"]["msg"]
    assert checks["plan"]["level"] == "ok"
    assert checks["test_data"]["level"] == "warn" and "template" in checks["test_data"]["msg"]
    assert checks["figma"]["level"] == "info"


def test_a_missing_contract_warns_what_it_costs(rfc_run):
    (rfc_run / "out/rfc.md").write_text("# RFC\n\n## 1. Summary\n")
    checks = by_id(preflight.check_sources("F-1", str(rfc_run), {}))
    assert checks["contract"]["level"] == "warn" and "manual" in checks["contract"]["msg"]


def test_no_feature_and_no_rfc_fail(tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    checks = by_id(preflight.check_sources("", str(empty), {}))
    assert checks["feature"]["level"] == "fail"
    assert checks["rfc"]["level"] == "fail" and checks["prd"]["level"] == "fail"


def test_an_explicit_source_wins(rfc_run, tmp_path):
    data = tmp_path / "data.yaml"
    data.write_text("fixtures: {}\n")
    checks = by_id(preflight.check_sources("F-1", str(rfc_run), {"test_data": str(data)}))
    assert checks["test_data"]["level"] == "ok"


# ---- preflight: Kiwi -------------------------------------------------------------------------------------


def flags_with(**values):
    defaults = {"testing.storage": "kiwi", "testing.kiwi_web": "https://kiwi.test", "testing.kiwi_url": "",
                "testing.kiwi_user": "agent", "testing.kiwi_password": ""}
    defaults.update({f"testing.{k}": v for k, v in values.items()})
    return SimpleNamespace(get=lambda name, default=None: defaults.get(name, default))


def test_the_json_store_fails_with_the_setup_fix():
    checks = by_id(preflight.check_kiwi(flags_with(storage="json")))
    assert checks["kiwi"]["level"] == "fail" and "setup-kiwi" in checks["kiwi"]["fix"]


def test_no_tcms_conf_fails_without_touching_the_network(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(preflight, "store_for", lambda *a: pytest.fail("probed Kiwi without a login"))
    checks = by_id(preflight.check_kiwi(flags_with()))
    assert checks["kiwi_conf"]["level"] == "fail" and "chmod 600" in checks["kiwi_conf"]["fix"]


def _conf(home: Path, mode: int) -> None:
    conf = home / ".tcms.conf"
    conf.write_text("[tcms]\nurl = https://kiwi.test/xml-rpc/\nusername = agent\n")
    conf.chmod(mode)


@pytest.mark.parametrize("healthy", [True, False])
def test_kiwi_reachability_is_reported(tmp_path, monkeypatch, healthy):
    pytest.importorskip("tcms_api")
    monkeypatch.setenv("HOME", str(tmp_path))
    _conf(tmp_path, 0o600)
    health = {"ok": True} if healthy else {"ok": False, "error": "ConnectionRefusedError: refused"}
    monkeypatch.setattr(preflight, "store_for", lambda *a: SimpleNamespace(health=lambda: health))
    checks = by_id(preflight.check_kiwi(flags_with()))
    assert checks["kiwi_conf"]["level"] == "ok"
    if healthy:
        assert checks["kiwi"]["level"] == "ok" and "as agent" in checks["kiwi"]["msg"]
    else:
        assert checks["kiwi"]["level"] == "fail" and "refused" in checks["kiwi"]["msg"]
        assert "store in Kiwi later" in checks["kiwi"]["fix"]


def test_a_world_readable_tcms_conf_warns(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    _conf(tmp_path, 0o644)
    checks = by_id(preflight.check_kiwi(flags_with(), network=False))
    assert checks["kiwi_conf"]["level"] == "warn"


# ---- preflight: setup-kiwi and the run-dir suggestion ------------------------------------------------------


def test_setup_kiwi_merges_non_secret_settings(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"LANGFUSE": {"HOST": "h"}, "TESTING": {"timeout_s": 5}}))
    path.chmod(0o600)
    preflight.setup_kiwi({"kiwi_web": "https://kiwi.test", "product_prefix": "Sandbox / ", "kiwi_user": None},
                         config_path=path)
    data = json.loads(path.read_text())
    assert data["LANGFUSE"] == {"HOST": "h"}
    assert data["TESTING"] == {"timeout_s": 5, "kiwi_web": "https://kiwi.test", "product_prefix": "Sandbox / ",
                               "storage": "kiwi"}
    assert path.stat().st_mode & 0o777 == 0o600


def test_setup_kiwi_refuses_a_password(tmp_path):
    path = tmp_path / "config.json"
    path.write_text("{}")
    with pytest.raises(ValueError, match="not a setting this command writes"):
        preflight.setup_kiwi({"kiwi_password": "x"}, config_path=path)


def test_the_suggested_run_dir_never_reuses_one(tmp_path):
    cfg = Cfg({"RUNS_ROOT": str(tmp_path)})
    first = Path(preflight.suggest_run_dir(cfg, "F-1"))
    first.mkdir()
    assert Path(preflight.suggest_run_dir(cfg, "F-1")).name == first.name + "-v2"


# ---- status ---------------------------------------------------------------------------------------------


def manifest(run_dir: Path, rows: dict[str, str]) -> None:
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "manifest.json").write_text(json.dumps(
        {name: {"status": st, "attempts": 1, "duration_s_total": 61, "cost_usd_total": 1.5,
                "gate": {"passed": st == "ok", "failures": [] if st == "ok" else ["bad"]}}
         for name, st in rows.items()}))


GEN_OK = {s: "ok" for s in status.GENERATE_STAGES}


@pytest.mark.parametrize("rows, store, step", [
    ({}, None, "start"),
    ({"sources": "ok", "generate_cases": "ok"}, None, "resume_generate"),
    ({**GEN_OK, "evaluate": "gate_failed"}, None, "fix"),
    ({k: v for k, v in GEN_OK.items() if k != "store_cases"}, None, "store"),
    (GEN_OK, {"backend": "json"}, "store"),
    (GEN_OK, {"backend": "kiwi"}, "review"),
    ({**GEN_OK, "derive_spec": "ok"}, {"backend": "kiwi"}, "execute"),
    ({**GEN_OK, "derive_spec": "ok", "execute_be": "ok"}, {"backend": "kiwi"}, "done"),
])
def test_next_step(tmp_path, rows, store, step):
    manifest(tmp_path, rows)
    assert status.next_step(status.stages(tmp_path), store)["step"] == step


def test_milestones_read_the_stage_outputs(tmp_path, clean_cases):
    manifest(tmp_path, GEN_OK)
    (tmp_path / "qa").mkdir()
    (tmp_path / "qa/cases.json").write_text(json.dumps(clean_cases))
    (tmp_path / "qa/store.json").write_text(json.dumps(
        {"backend": "kiwi", "total": 3, "created": 2, "updated": 1, "protected": 0,
         "plan_url": "https://kiwi.test/plan/4/", "status": "PROPOSED"}))
    line = status.milestone(tmp_path, "generate_cases")
    assert line.startswith(f"generate_cases: {len(clean_cases)} cases") and "http" in line
    assert "(1m01s, $1.50)" in line
    assert "https://kiwi.test/plan/4/" in status.milestone(tmp_path, "store_cases")


def test_kiwi_counts_this_suite_only_and_flags_conflicting_tags(tmp_path, monkeypatch):
    (tmp_path / "qa").mkdir()
    (tmp_path / "qa/cases.json").write_text(json.dumps([{"id": "TC-A-1"}, {"id": "TC-A-2"}, {"id": "TC-A-3"}]))
    kiwi = [Case("TC-A-1", "", "CONFIRMED", ("exec:http",)),
            Case("TC-A-2", "", "PROPOSED", ("exec:manual", "exec:http")),
            Case("TC-OLD-9", "", "PROPOSED", ("exec:http",))]
    monkeypatch.setattr(preflight, "load_config", lambda: None)
    monkeypatch.setattr(preflight, "resolved_flags", lambda cfg: flags_with())
    monkeypatch.setattr(preflight, "store_for", lambda *a: SimpleNamespace(list_cases=lambda feature: kiwi))
    counts = status.kiwi_counts("F-1", tmp_path)
    assert counts["by_status"] == {"CONFIRMED": {"http": 1}, "PROPOSED": {"conflict": 1}}
    assert counts["confirmed_http"] == 1
    assert counts["stale"] == {"PROPOSED": 1}
    assert counts["not_in_kiwi"] == 1


def test_kiwi_counts_report_an_unreachable_store(tmp_path, monkeypatch):
    def boom(*a):
        raise ConnectionRefusedError("refused")

    monkeypatch.setattr(preflight, "load_config", lambda: None)
    monkeypatch.setattr(preflight, "resolved_flags", lambda cfg: flags_with())
    monkeypatch.setattr(preflight, "store_for", lambda *a: SimpleNamespace(list_cases=boom))
    assert "refused" in status.kiwi_counts("F-1", tmp_path)["error"]


# ---- feature keys for one RFC run ----------------------------------------------------------------------


@pytest.mark.parametrize("text, key", [
    ("1Voice", "1VOICE"),
    ("nr digichem", "NR-DIGICHEM"),
    ("Northern Railway Digi-Rail-Chemist", "NORTHERN-RAILWAY-DIGI-RAIL"),
])
def test_feature_key(text, key):
    assert preflight.feature_key(text) == key


def test_features_come_from_the_chosen_run_only(tmp_path, rfc_run, monkeypatch):
    monkeypatch.setattr(preflight, "load_config", lambda: Cfg({"RUNS_ROOT": str(tmp_path)}))
    (rfc_run / "out/rfc.md").write_text("# RFC: 1Voice — voice assistant\n\n| Jira | ABC-12 |\n")
    (rfc_run / "scope.json").write_text(json.dumps({"jira": "LABS-7", "jira_included": []}))
    other = tmp_path / "other-rfc"
    for name, used, feature in (("2026-11-01-testing-OLD", rfc_run, "VOICE-OLD"),
                                ("2026-11-02-testing-ELSE", other, "SOMETHING-ELSE")):
        (tmp_path / name / "qa").mkdir(parents=True)
        (tmp_path / name / "qa/sources.json").write_text(
            json.dumps({"feature": feature, "rfc_run_dir": str(used)}))
    rows = preflight.feature_suggestions(str(rfc_run), limit=5)
    assert [r["key"] for r in rows] == ["VOICE-OLD", "LABS-7", "ABC-12", "1VOICE"]
    assert "updates its Kiwi plan" in rows[0]["why"]


def test_no_hint_gives_no_keys(tmp_path, monkeypatch):
    monkeypatch.setattr(preflight, "load_config", lambda: Cfg({"RUNS_ROOT": str(tmp_path / "none")}))
    bare = tmp_path / "bare"
    bare.mkdir()
    assert preflight.feature_suggestions(str(bare)) == []


# ---- web settings: the login file, environments ------------------------------------------------------------


def test_the_login_file_is_checked_without_showing_it(tmp_path):
    path = tmp_path / "login.json"
    assert preflight.check_ui_login("")[0]["level"] == "warn"
    assert preflight.check_ui_login(str(path))[0]["level"] == "fail"
    path.write_text('{"phone": "9000000001"}')
    path.chmod(0o600)
    row = preflight.check_ui_login(str(path))[0]
    assert row["level"] == "fail" and "otp" in row["msg"] and "9000000001" not in json.dumps(row)
    path.write_text('{"phone": "9000000001", "otp": "4321"}')
    row = preflight.check_ui_login(str(path))[0]
    assert row["level"] == "ok" and "4321" not in json.dumps(row)
    path.chmod(0o644)
    assert preflight.check_ui_login(str(path))[0]["level"] == "warn"


def test_environments_give_their_base_url():
    flags = flags_with(environments="jupiter, pluto", base_url_template="https://{env}.1mg.com")
    assert preflight.environments(flags) == [{"env": "jupiter", "base_url": "https://jupiter.1mg.com"},
                                             {"env": "pluto", "base_url": "https://pluto.1mg.com"}]


def test_setup_web_stores_the_login_path_not_the_values(tmp_path):
    path = tmp_path / "config.json"
    path.write_text("{}")
    preflight.setup_kiwi({"environments": "jupiter", "ui_login": "~/.secrets/playwright/prestag-login.json"},
                         config_path=path, allowed=preflight.WEB_SETTINGS, default_storage=False)
    assert json.loads(path.read_text())["TESTING"] == {
        "environments": "jupiter", "ui_login": "~/.secrets/playwright/prestag-login.json"}


# ---- earlier runs: resume instead of a duplicate ----------------------------------------------------------


def make_testing_run(root: Path, name: str, rfc_run: Path, feature: str, rows: dict[str, str]) -> Path:
    run = root / name
    manifest(run, rows)
    (run / "qa").mkdir()
    (run / "qa/sources.json").write_text(json.dumps({"feature": feature, "rfc_run_dir": str(rfc_run)}))
    return run


def test_runs_lists_unfinished_runs_of_this_rfc_and_feature(tmp_path, rfc_run, monkeypatch):
    """NR-DIGICHEM was generated three times over two days because every start made a new folder."""
    monkeypatch.setattr(preflight, "load_config", lambda: Cfg({"RUNS_ROOT": str(tmp_path)}))
    make_testing_run(tmp_path, "2026-11-02-testing-F-1", rfc_run, "F-1", {"sources": "ok", "generate_cases": "ok"})
    make_testing_run(tmp_path, "2026-11-03-testing-F-1", rfc_run, "F-1",
                {**GEN_OK, "derive_spec": "ok", "execute_be": "ok"})          # finished
    make_testing_run(tmp_path, "2026-11-03-testing-G-2", rfc_run, "G-2", {"sources": "ok"})
    other = tmp_path / "other-rfc"
    make_testing_run(tmp_path, "2026-11-04-testing-F-1", other, "F-1", {"sources": "ok"})

    rows = preflight.runs(str(rfc_run), "f-1")
    assert [r["name"] for r in rows] == ["2026-11-02-testing-F-1"]
    assert rows[0]["label"] == "generation stopped part-way"
    assert len(preflight.runs(str(rfc_run), "F-1", include_done=True)) == 2
    assert {r["feature"] for r in preflight.runs()} == {"F-1", "G-2"}


def test_check_normalises_the_feature_and_offers_the_earlier_run(tmp_path, rfc_run, monkeypatch):
    monkeypatch.setattr(preflight, "load_config", lambda: Cfg({"RUNS_ROOT": str(tmp_path)}))
    make_testing_run(tmp_path, "2026-11-03-testing-HDPO", rfc_run, "HDPO", {"sources": "ok"})
    resolved = preflight.check("hdpo", str(rfc_run), network=False)["resolved"]
    assert resolved["feature"] == "HDPO"
    assert [r["name"] for r in resolved["existing_runs"]] == ["2026-11-03-testing-HDPO"]


# ---- status: why a run stopped, in words -------------------------------------------------------------------


def test_a_dropped_connection_is_not_reported_as_a_broken_suite(tmp_path):
    """The hdpo run on disk: gate_failed with 'declared output missing', its stream ending in api_error."""
    manifest(tmp_path, {"sources": "ok", "generate_cases": "gate_failed"})
    m = json.loads((tmp_path / "manifest.json").read_text())
    m["generate_cases"]["gate"]["failures"] = ["declared output missing: qa/cases.json"]
    (tmp_path / "manifest.json").write_text(json.dumps(m))
    (tmp_path / "logs").mkdir()
    (tmp_path / "logs/generate_cases.stream.jsonl").write_text(
        json.dumps({"type": "result", "is_error": True, "terminal_reason": "api_error"}) + "\n")
    why = status.report(tmp_path)["next"]["why"]
    assert why["kind"] == "connection" and why["retry"] is True
    assert "declared output missing" not in why["message"]


def test_an_engine_failure_is_explained_from_the_activity_log(tmp_path):
    manifest(tmp_path, {"sources": "ok", "generate_cases": "engine_failed"})
    (tmp_path / "logs").mkdir()
    (tmp_path / "logs/activity.log").write_text(
        "[10:00:00] [generate_cases] ENGINE FAILURE: claude exited 1: permission denied\n")
    why = status.report(tmp_path)["next"]["why"]
    assert why == {"kind": "engine", "retry": False,
                   "message": "generate_cases failed to run: claude exited 1: permission denied"}


def test_a_real_gate_failure_names_its_checks(tmp_path):
    manifest(tmp_path, {"sources": "ok", "generate_cases": "gate_failed"})
    why = status.report(tmp_path)["next"]["why"]
    assert why["kind"] == "gate" and why["retry"] is False and "bad" in why["message"]
