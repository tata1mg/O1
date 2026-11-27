"""Scope's Atlassian fetch on Axon (scope/atlassian_axon.py), with `axon pi` stubbed out."""

import json
import subprocess
from types import SimpleNamespace

import pytest

from rfc_agent.scope import atlassian as at
from rfc_agent.scope import cli as scope_cli
from rfc_agent.scope.atlassian_axon import AxonConnectorSession

V2_PAGE = {"data": {"id": "7", "type": "live_doc", "title": "T", "body": {"format": "markdown", "value": "b" * 300},
                    "metadata": {"version": {"createdAt": "2026-10-26T05:37:35Z"}}}}


def _stream(calls, *, settled=True, stop="toolUse", cost=0.001):
    """Pi --mode json lines for one turn: an assistant message with tool calls, then their results."""
    ev = [{"type": "session", "version": 3, "id": "s"}]
    ev.append({"type": "message_end", "message": {
        "role": "assistant", "stopReason": stop, "usage": {"cost": {"total": cost}},
        "content": [{"type": "toolCall", "id": f"c{i}", "name": f"mcp__atlassian__{name}", "arguments": args}
                    for i, (name, args, _r) in enumerate(calls)]}})
    for i, (name, _args, result) in enumerate(calls):
        msg = {"role": "toolResult", "toolCallId": f"c{i}", "toolName": f"mcp__atlassian__{name}",
               "content": [{"type": "text", "text": result.get("text", "")}], "isError": result.get("error", False),
               "details": {"server": "atlassian", "tool": name, **result.get("details", {})}}
        ev.append({"type": "message_end", "message": msg})
    if settled:
        ev.append({"type": "agent_settled"})
    return "\n".join(json.dumps(e) for e in ev) + "\n"


@pytest.fixture
def fake(monkeypatch):
    """Queue of (stdout, returncode) per `axon pi -p` turn; records each argv/cwd. `mcp list` answers from .status."""
    state = SimpleNamespace(turns=[], seen=[], status={"servers": [{"name": "atlassian", "state": "connected"}]})

    def run(argv, **kw):
        if argv[1:4] == ["pi", "mcp", "list"]:
            return subprocess.CompletedProcess(argv, 0, json.dumps(state.status), "")
        state.seen.append({"argv": argv, "cwd": kw.get("cwd"), "input": kw.get("input"), "env": kw.get("env")})
        out, code = state.turns.pop(0)
        return subprocess.CompletedProcess(argv, code, out, "")

    monkeypatch.setattr(subprocess, "run", run)
    return state


def _session(tmp_path, **kw):
    s = AxonConnectorSession(tmp_path / "logs" / "scope.atlassian.stream.jsonl", model="gemini-3.8-flash",
                             provider="litellm", **kw)
    return s.__enter__()


def test_connect_uses_the_site_hostname_and_asks_the_model_nothing(tmp_path, fake):
    s = _session(tmp_path)
    res = s.connect("https://Acme.atlassian.net/")
    assert s.cloud_id == "acme.atlassian.net" and res.cost_usd == 0 and fake.seen == []  # no turn, no cost
    assert s.connector_status == "connected"


def test_one_turn_is_its_own_folder_and_only_the_v2_read_tools(tmp_path, fake):
    fake.turns.append((_stream([("getConfluenceContent", {"content_id": "7"}, {"text": json.dumps(V2_PAGE)})]), 0))
    s = _session(tmp_path)
    s.connect("https://acme.atlassian.net/")
    res = s.fetch(pages=["7"])
    assert res.cost_usd == pytest.approx(0.001)
    run = fake.seen[0]
    # its own folder, with a project override that only switches the user server to direct exposure
    override = json.loads((run["cwd"] / ".pi" / "mcp.json").read_text())
    assert override == {"mcpServers": {"atlassian": {"exposure": "direct"}}}
    argv = run["argv"]
    assert argv[:5] == ["axon", "pi", "--mode", "json", "-p"] and "--approve" in argv and "--no-session" in argv
    tools = argv[argv.index("--tools") + 1].split(",")
    assert tools == [f"mcp__atlassian__{t}" for t in at.TOOLS]  # nothing but the read tools
    assert at.TOOLS == ("getConfluenceContent", "executeRead", "getJiraIssue")
    assert "getConfluenceContent with cloudId acme.atlassian.net, content_id 7" in run["input"]
    assert argv[argv.index("--model") + 1] == "gemini-3.8-flash"
    assert (tmp_path / "logs" / "scope.atlassian.stream.jsonl").read_text().count("agent_settled") == 1


def test_fetch_reads_the_untruncated_file(tmp_path, fake):
    body = "x" * 30000
    full = tmp_path / "pi-mcp-full.txt"
    full.write_text(json.dumps({"data": {"id": "42", "title": "PRD", "body": {"format": "markdown", "value": body}}}))
    cut = "Warning: truncated output …" + body[:100]
    fake.turns.append((_stream([
        ("getConfluenceContent", {"content_id": "42"}, {"text": cut, "details": {"fullOutputPath": str(full)}}),
        ("getJiraIssue", {"issueIdOrKey": "AB-1"}, {"text": "{\"key\": \"AB-1\"}"}),
        ("executeRead", {"name": "getConfluenceContentDescendants", "inputs": {"contentId": "42"}},
         {"text": "nope", "error": True}),
    ]), 0))
    s = _session(tmp_path)
    s.cloud_id = "acme.atlassian.net"
    res = s.fetch(pages=["42"], page_descendants=["42"], jira=["AB-1"])
    page, jira, desc = res.calls
    assert at.parse_page(page)["body"] == body  # byte-exact, not the 20 KB cut
    assert jira.op == "getJiraIssue" and jira.target == "AB-1" and jira.text == '{"key": "AB-1"}'
    assert desc.is_error and desc.op == "getConfluenceContentDescendants" and page.target == "42"
    assert "cloudId acme.atlassian.net" in fake.seen[0]["input"]


def test_not_signed_in_is_not_retried(tmp_path, fake):
    fake.status = {"servers": [{"name": "atlassian", "state": "needs-auth"}]}
    with pytest.raises(at.AtlassianError) as e:
        _session(tmp_path).connect("https://acme.atlassian.net")
    assert not e.value.retryable and "axon pi mcp login atlassian" in str(e.value)


def test_missing_server_says_how_to_add_it_on_v2(tmp_path, fake):
    fake.status = {"servers": []}
    with pytest.raises(at.AtlassianError) as e:
        _session(tmp_path).connect("https://acme.atlassian.net")
    assert "axon pi mcp add atlassian --url https://mcp.atlassian.com/v2/mcp" in str(e.value)
    assert not e.value.retryable


def test_a_server_still_on_v1_is_refused_with_the_fix(tmp_path, fake):
    fake.status = {"servers": [{"name": "atlassian", "state": "connected", "url": "https://mcp.atlassian.com/v1/mcp"}]}
    with pytest.raises(at.AtlassianError) as e:
        _session(tmp_path).connect("https://acme.atlassian.net")
    msg = str(e.value)
    assert "v1/mcp" in msg and "axon pi mcp remove atlassian" in msg and "v2/mcp" in msg and not e.value.retryable


def test_fetch_with_no_connector_call_says_the_v2_tools_are_missing(tmp_path, fake):
    fake.turns.append((_stream([]), 0))
    s = _session(tmp_path)
    s.connect("https://acme.atlassian.net")
    with pytest.raises(at.AtlassianError) as e:
        s.fetch(pages=["7"])
    assert "v2" in str(e.value)


def test_unfinished_turn_and_refusal(tmp_path, fake):
    fake.turns.append((_stream([], settled=False), 1))
    with pytest.raises(at.AtlassianError) as e:
        _session(tmp_path)._turn("hi")
    assert e.value.retryable
    fake.turns.append((_stream([], stop="error"), 0))
    with pytest.raises(at.AtlassianError) as e:
        _session(tmp_path)._turn("hi")
    assert "refused" in str(e.value) and not e.value.retryable


def test_open_session_connects_without_a_model_turn(tmp_path, fake):
    import contextlib

    with contextlib.ExitStack() as st:
        s, cost = at.open_session(st, tmp_path / "x.jsonl", "https://acme.atlassian.net", wait_s=0,
                                  make=lambda: AxonConnectorSession(tmp_path / "x.jsonl"))
        assert s.cloud_id == "acme.atlassian.net" and cost == 0 and fake.seen == []


def test_scope_uses_axon_only_on_axon(tmp_path, monkeypatch):
    class Cfg:
        def get(self, k, d=None):
            return {"AGENT.AXON": {"ATLASSIAN_SERVER": "atl"}}.get(k, d)

    monkeypatch.delenv("RFC_AGENT_STAGE_ENGINES", raising=False)
    monkeypatch.setenv("RFC_AGENT_ENGINE", "claude-code")
    assert scope_cli._axon_atlassian(Cfg(), at.DEFAULT_MODEL, tmp_path) is None
    monkeypatch.setenv("RFC_AGENT_ENGINE", "axon")
    s = scope_cli._axon_atlassian(Cfg(), at.DEFAULT_MODEL, tmp_path)()
    assert isinstance(s, AxonConnectorSession) and s.server == "atl" and s.prefix == "mcp__atl__"
    assert s.model == "gemini-3.8-flash"  # haiku tier -> the gateway's cheap model
    monkeypatch.delenv("RFC_AGENT_ENGINE")


def test_parse_page_reads_the_v2_shape():
    """Atlassian MCP v2: {"data": {id, type, title, body: {format, value}, metadata: {version: {createdAt}}}}."""
    page = at.parse_page(at.ToolCall("getConfluenceContent", {"content_id": "7"}, json.dumps(V2_PAGE)))
    assert page["id"] == "7" and page["title"] == "T" and page["body"] == "b" * 300
    assert page["url"] == "" and page["last_modified"] == "2026-10-26T05:37:35Z"
    bare = {"data": {"title": "T", "body": {"format": "markdown", "value": "x"}}}
    got = at.parse_page(at.ToolCall("getConfluenceContent", {"content_id": "9"}, json.dumps(bare)))
    assert got["id"] == "9" and got["last_modified"] == ""
    assert at.parse_page(at.ToolCall("getConfluenceContent", {}, json.dumps({"id": "7", "body": "v1 shape"}))) is None


def test_write_page_falls_back_to_a_site_url(tmp_path):
    page = {"id": "7", "title": "T", "body": "x", "url": "", "last_modified": ""}
    scope_cli._write_page(tmp_path / "p.md", page, "https://acme.atlassian.net/")
    want = "source_url: https://acme.atlassian.net/wiki/pages/viewpage.action?pageId=7"
    assert want in (tmp_path / "p.md").read_text()
