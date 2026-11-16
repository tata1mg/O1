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
                          ui_auth=str(tmp_path / "state.json"), hosts="web.test")
    joined = " ".join(cmd)
    assert "--base-url https://web.test" in joined and f"--ui-auth {tmp_path / 'state.json'}" in joined
    assert "--set testing.agent_hosts=web.test" in joined
