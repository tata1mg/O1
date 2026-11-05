"""Scope's Atlassian fetch on Axon (scope/atlassian_axon.py), with `axon pi` stubbed out."""

import json
import subprocess
from types import SimpleNamespace

import pytest

from rfc_agent.scope import atlassian as at
from rfc_agent.scope import cli as scope_cli
from rfc_agent.scope.atlassian_axon import AxonConnectorSession

SITES = [{"id": "cloud-1", "url": "https://acme.atlassian.net"}, {"id": "cloud-2", "url": "https://other.net"}]


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


def test_connect_picks_the_configured_site(tmp_path, fake):
    fake.turns.append((_stream([("getAccessibleAtlassianResources", {}, {"text": json.dumps(SITES)})]), 0))
    s = _session(tmp_path)
    res = s.connect("https://acme.atlassian.net/")
    assert s.cloud_id == "cloud-1" and res.cost_usd == pytest.approx(0.001)
    run = fake.seen[0]
    # its own folder, with a project override that only switches the user server to direct exposure
    override = json.loads((run["cwd"] / ".pi" / "mcp.json").read_text())
    assert override == {"mcpServers": {"atlassian": {"exposure": "direct"}}}
    argv = run["argv"]
    assert argv[:5] == ["axon", "pi", "--mode", "json", "-p"] and "--approve" in argv and "--no-session" in argv
    tools = argv[argv.index("--tools") + 1].split(",")
    assert tools == [f"mcp__atlassian__{t}" for t in at.TOOLS]  # nothing but the four fetch tools
    assert argv[argv.index("--model") + 1] == "gemini-3.8-flash"
    assert (tmp_path / "logs" / "scope.atlassian.stream.jsonl").read_text().count("agent_settled") == 1


def test_fetch_reads_the_untruncated_file(tmp_path, fake):
    body = "x" * 30000
    full = tmp_path / "pi-mcp-full.txt"
    full.write_text(json.dumps({"id": "42", "title": "PRD", "body": body, "webUrl": "u"}))
    cut = "Warning: truncated output …" + body[:100]
    fake.turns.append((_stream([
        ("getConfluencePage", {"pageId": "42"}, {"text": cut, "details": {"fullOutputPath": str(full)}}),
        ("getJiraIssue", {"issueIdOrKey": "AB-1"}, {"text": "{\"key\": \"AB-1\"}"}),
        ("getConfluencePageDescendants", {"pageId": "42"}, {"text": "nope", "error": True}),
    ]), 0))
    s = _session(tmp_path)
    s.cloud_id = "cloud-1"
    res = s.fetch(pages=["42"], page_descendants=["42"], jira=["AB-1"])
    page, jira, desc = res.calls
    assert at.parse_page(page)["body"] == body  # byte-exact, not the 20 KB cut
    assert jira.name == "getJiraIssue" and jira.text == '{"key": "AB-1"}'
    assert desc.is_error
    assert "cloudId cloud-1" in fake.seen[0]["input"]


def test_the_site_must_match_exactly(tmp_path, fake):
    lookalike = [{"id": "evil", "url": "https://notacme.atlassian.net"}, {"id": "other", "url": "https://other.net"}]
    fake.turns.append((_stream([("getAccessibleAtlassianResources", {}, {"text": json.dumps(lookalike)})]), 0))
    with pytest.raises(at.AtlassianError) as e:
        _session(tmp_path).connect("https://acme.atlassian.net")
    assert not e.value.retryable  # no substring match, no first-site pick
    assert str(e.value) == ("ATLASSIAN.SITE is acme.atlassian.net, but this Atlassian sign-in reaches only: "
                            "notacme.atlassian.net, other.net. Fix ATLASSIAN.SITE, or sign in with an account "
                            "that can see that site.")
    assert at.pick_cloud_id(json.dumps(SITES), "ACME.atlassian.net/") == "cloud-1"  # scheme, case, slash don't matter
    assert at.pick_cloud_id("[]", "https://acme.atlassian.net") is None  # nothing listed: the retryable path


def test_not_signed_in_is_not_retried(tmp_path, fake):
    fake.turns.append((_stream([]), 0))
    fake.status = {"servers": [{"name": "atlassian", "state": "needs-auth"}]}
    s = _session(tmp_path)
    with pytest.raises(at.AtlassianError) as e:
        s.connect("https://acme.atlassian.net")
    assert not e.value.retryable and "axon pi mcp login atlassian" in str(e.value)


def test_missing_server_says_how_to_add_it(tmp_path, fake):
    fake.turns.append((_stream([]), 0))
    fake.status = {"servers": []}
    with pytest.raises(at.AtlassianError) as e:
        _session(tmp_path).connect("https://acme.atlassian.net")
    assert "axon pi mcp add atlassian" in str(e.value) and not e.value.retryable


def test_connected_but_silent_is_retryable(tmp_path, fake):
    fake.turns.append((_stream([]), 0))
    with pytest.raises(at.AtlassianError) as e:
        _session(tmp_path).connect("https://acme.atlassian.net")
    assert e.value.retryable


def test_unfinished_turn_and_refusal(tmp_path, fake):
    fake.turns.append((_stream([], settled=False), 1))
    with pytest.raises(at.AtlassianError) as e:
        _session(tmp_path)._turn("hi")
    assert e.value.retryable
    fake.turns.append((_stream([], stop="error"), 0))
    with pytest.raises(at.AtlassianError) as e:
        _session(tmp_path)._turn("hi")
    assert "refused" in str(e.value) and not e.value.retryable


def test_open_session_retries_with_a_fresh_axon_session(tmp_path, fake):
    import contextlib

    ok = _stream([("getAccessibleAtlassianResources", {}, {"text": json.dumps(SITES)})])
    fake.turns += [(_stream([]), 0), (ok, 0)]
    with contextlib.ExitStack() as st:
        s, _ = at.open_session(st, tmp_path / "x.jsonl", "https://acme.atlassian.net", wait_s=0,
                               make=lambda: AxonConnectorSession(tmp_path / "x.jsonl"))
        assert s.cloud_id == "cloud-1" and len(fake.seen) == 2


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
    """Atlassian's own MCP server (Axon's, and Claude Code's user-scope `atlassian`) returns Confluence v2 JSON."""
    v2 = {"id": "7", "title": "T", "body": "b" * 300, "version": {"createdAt": "2026-10-26T05:37:35Z"},
          "_links": {"base": "https://acme.atlassian.net/wiki", "webui": "/spaces/X/pages/7/T"}}
    page = at.parse_page(at.ToolCall("getConfluencePage", {"pageId": "7"}, json.dumps(v2)))
    assert page["url"] == "https://acme.atlassian.net/wiki/spaces/X/pages/7/T"
    assert page["last_modified"] == "2026-10-26T05:37:35Z"
    bare = at.parse_page(at.ToolCall("getConfluencePage", {}, json.dumps({"id": "7", "title": "T", "body": "x"})))
    assert bare["url"] == "" and bare["last_modified"] == ""


def test_write_page_falls_back_to_a_site_url(tmp_path):
    page = {"id": "7", "title": "T", "body": "x", "url": "", "last_modified": ""}
    scope_cli._write_page(tmp_path / "p.md", page, "https://acme.atlassian.net/")
    want = "source_url: https://acme.atlassian.net/wiki/pages/viewpage.action?pageId=7"
    assert want in (tmp_path / "p.md").read_text()
