# ruff: noqa: E501  (table rows and assertion strings read better on one line)
"""`/rfc doctor` on Axon as a table (doctor_table.py): what was checked, its status, how to fix a failure."""

from types import SimpleNamespace

from rfc_agent import doctor_table as dt
from rfc_agent.doctor_table import FAIL, NA, PASS, WARN, Row


def test_render_is_a_table_with_a_fix_for_every_failure_and_a_verdict():
    rows = [Row("Axon installed", PASS, "axon"),
            Row("Node.js for Axon's Pi", WARN, "node 22.14.0 is older than 22.19", "`nvm install 22`"),
            Row("Atlassian MCP v2", FAIL, "'atlassian' is on the deprecated v1 endpoint", "`axon pi mcp remove atlassian`"),
            Row("ATLASSIAN.SITE", NA, "not needed")]
    out = dt.render(rows).splitlines()
    assert out[0] == "| Check | Status | Detail | How to fix |"
    assert "| Axon installed | ✅ PASS | axon | — |" in out
    assert "| Atlassian MCP v2 | ❌ FAIL | 'atlassian' is on the deprecated v1 endpoint | `axon pi mcp remove atlassian` |" in out
    assert out[-1] == "axon: BLOCKED (1 problem(s)) — first: Atlassian MCP v2: 'atlassian' is on the deprecated v1 endpoint"
    assert dt.blocked(rows)
    ok = dt.render([Row("a", PASS), Row("b", WARN, "w", "f")])
    assert ok.splitlines()[-1] == "axon: READY (1 warning(s))" and not dt.blocked([Row("b", WARN)])


def test_a_pipe_in_a_cell_does_not_break_the_table():
    assert "a\\|b" in dt.render([Row("c", FAIL, "a|b", "x")])


def test_axon_problems_become_rows_with_their_fix(monkeypatch):
    problems = ["Axon is not signed in — run `axon` once in a terminal and choose 'Login from browser'",
                "Axon's managed Pi 1.2.0 is older than 1.4.0 (needed for built-in MCP) — run `axon update`"]
    import harness.engines.axon as engine

    monkeypatch.setattr(engine.AxonEngine, "doctor", lambda self: problems)
    rows = {r.check: r for r in dt._axon_rows(None, {"bin": "axon"})}
    assert rows["Axon installed"].status == PASS and rows["Axon version"].status == PASS
    assert rows["Axon signed in"].status == FAIL and "Login from browser" in rows["Axon signed in"].fix
    assert rows["Managed Pi runtime"].status == FAIL and rows["Managed Pi runtime"].fix == "run `axon update`"
    monkeypatch.setattr(engine.AxonEngine, "doctor", lambda self: ["`axon` not found on PATH — install Axon: curl …"])
    only = dt._axon_rows(None, {"bin": "axon"})
    assert [r.check for r in only] == ["Axon installed"] and only[0].status == FAIL and only[0].fix.startswith("install")


def test_atlassian_row_names_the_v1_fix(monkeypatch):
    from rfc_agent.scope import atlassian_axon

    s = {"bin": "axon", "atlassian_server": "atlassian"}
    for info, status, word in ((("connected", "https://mcp.atlassian.com/v1/mcp"), FAIL, "axon pi mcp remove atlassian"),
                               (("missing", ""), FAIL, "axon pi mcp add atlassian"),
                               (("needs-auth", "https://mcp.atlassian.com/v2/mcp"), FAIL, "axon pi mcp login atlassian"),
                               (("connected", "https://mcp.atlassian.com/v2/mcp"), PASS, "")):
        monkeypatch.setattr(atlassian_axon.AxonConnectorSession, "server_info", lambda self, i=info: i)
        [row] = dt._atlassian_rows(None, s, True)
        assert row.status == status and word in row.fix
    assert dt._atlassian_rows(None, s, False)[0].status == NA


def test_node_row_warns_below_the_pi_minimum(monkeypatch):
    monkeypatch.setattr(dt, "node_version", lambda: (22, 14, 0))
    row = dt._node_row()
    assert row.status == FAIL and "nvm alias default" in row.fix  # Pi 1.6 will not start
    monkeypatch.setattr(dt, "node_version", lambda: (22, 23, 5))
    assert dt._node_row().status == PASS
    monkeypatch.setattr(dt, "node_version", lambda: None)
    assert dt._node_row().status == WARN


def test_langfuse_and_folders(monkeypatch, tmp_path):
    from rfc_agent import observe

    cfg = SimpleNamespace(get=lambda k, d=None: {"REPOS_ROOT": str(tmp_path / "nope"), "RUNS_ROOT": str(tmp_path)}.get(k, d))
    monkeypatch.setattr(observe, "settings", lambda c=None: {"enabled": True, "verbose": False})
    monkeypatch.setattr(observe, "_keys", lambda c: ("", ""))
    assert dt._langfuse_row(cfg).status == FAIL and "LANGFUSE" in dt._langfuse_row(cfg).fix.upper()
    repos = dt._folder_rows(cfg)
    assert repos[0].status == FAIL and "REPOS_ROOT" in repos[0].fix
    (tmp_path / "repo" / ".git").mkdir(parents=True)
    cfg2 = SimpleNamespace(get=lambda k, d=None: {"REPOS_ROOT": str(tmp_path), "RUNS_ROOT": str(tmp_path / "runs")}.get(k, d))
    rows = dt._folder_rows(cfg2)
    assert rows[0].status == PASS and "1 checkout" in rows[0].detail
    assert rows[1].status == WARN and "outside REPOS_ROOT" in rows[1].fix  # runs inside the repos folder
