"""Scope repo discovery: the Opus discovery agent over fleetlens (two tiers), PRD mentions, fallbacks."""

from __future__ import annotations

import json
import os

import pytest

from rfc_agent.scope import context as c

PRD = "---\nsource_url: x\n---\n# PRD: Patient selection in Labs checkout\nUsers pick a patient before slot selection."

FAKE = r'''#!/usr/bin/env python3
import json, os, sys
prompt = sys.stdin.read()
mode = os.environ.get("FAKE_MODE", "ok")
if "stream-json" in sys.argv:  # the discovery agent
    open(os.environ["FAKE_PROMPT"] + ".disc", "w").write(prompt)
    open(os.environ["FAKE_PROMPT"] + ".argv", "w").write(json.dumps(sys.argv[1:]))
    mcp = json.load(open(sys.argv[sys.argv.index("--mcp-config") + 1]))
    use = {"type": "assistant", "message": {"content": [
        {"type": "tool_use", "id": "t1", "name": "mcp__fleetlens__get_service_graph", "input": {}}]}}
    res = {"type": "user", "message": {"content": [
        {"type": "tool_result", "tool_use_id": "t1", "content": "graph", "is_error": mode == "failedlookups"}]}}
    if mode == "nolookups":  # the #16 run: no tool reachable, services guessed from the PRD
        use = res = {"type": "assistant", "message": {"content": [{"type": "text", "text": "No tools here."}]}}
    services = {"services": [{"service": "ns_gateway", "tier": "maybe", "why": "may need a template"},
                             {"service": "service:API-Gateway", "tier": "changes", "why": "routes the new endpoint"},
                             {"service": "svc_a", "tier": "CHANGES", "why": "  owns the order\n patient name "},
                             {"service": "svc_a_admin_service", "why": "same repo"}]}
    draft = json.dumps({"services": [{"service": "draft"}]})
    answer = f"Looked around. {draft} then {json.dumps(services)}"
    text = {"nojson": "I could not decide.", "limit": ""}.get(mode, answer)
    final = {"type": "result", "result": text, "total_cost_usd": 0.42, "is_error": mode == "limit",
             "usage": {"input_tokens": 9}}
    if mode == "limit":
        final["result"] = "You've hit your weekly limit"
    for ev in ({"type": "system", "subtype": "init", "mcp_servers": [{"name": k, "status": "connected"}
                                                                     for k in mcp["mcpServers"]]}, use, res, final):
        print(json.dumps(ev))
    sys.exit(0)
sys.exit(3)  # only the discovery agent (stream-json) exists now
'''


class Cfg:
    def __init__(self, **kw):
        self.d = kw

    def get(self, k, d=None):
        return self.d.get(k, d)


def _fake(tmp_path, monkeypatch, mode="ok"):
    fake = tmp_path / "claude"
    fake.write_text(FAKE)
    fake.chmod(0o755)
    monkeypatch.setenv("FAKE_PROMPT", str(tmp_path / "prompt.txt"))
    monkeypatch.setenv("FAKE_MODE", mode)
    return Cfg(**{"AGENT.CLAUDE_BIN": str(fake)})


def test_discovery_agent_is_isolated_and_parsed(tmp_path, monkeypatch):
    cfg = _fake(tmp_path, monkeypatch)
    spec = {"type": "http", "url": "http://fl/mcp", "headers": {"Authorization": "Bearer t"}}
    d = c.discover(PRD, cfg, spec, log_dir=tmp_path / "logs")
    assert d["ok"] and d["cost_usd"] == 0.42 and d["tool_calls"] == 1 and d["error"] is None
    assert d["services"] == [{"service": "API-Gateway", "tier": "changes", "why": "routes the new endpoint"},
                             {"service": "svc_a", "tier": "changes", "why": "owns the order patient name"},
                             {"service": "svc_a_admin_service", "tier": "changes", "why": "same repo"},
                             {"service": "ns_gateway", "tier": "maybe", "why": "may need a template"}], \
        "the LAST services JSON wins; a missing tier counts as changes; maybe goes last"
    argv = json.loads((tmp_path / "prompt.txt.argv").read_text())
    assert argv[argv.index("--tools") + 1] == "" and "--strict-mcp-config" in argv, "no built-ins, no other MCP"
    grants = argv[argv.index("--allowedTools") + 1].split(",")
    assert grants and all(g.startswith("mcp__fleetlens__") for g in grants)
    assert "mcp__fleetlens__get_service_graph" in grants
    mcp = tmp_path / "logs" / "scope.discovery.mcp.json"
    assert json.loads(mcp.read_text())["mcpServers"] == {"fleetlens": spec}
    assert mcp.stat().st_mode & 0o777 == 0o600, "the file carries the token"
    assert len((tmp_path / "logs" / "scope.discovery.stream.jsonl").read_text().splitlines()) == 4
    card = (tmp_path / "logs" / "agents" / "scope.discovery.log").read_text()
    assert "▶ scope · discovery started" in card and "#1 mcp__fleetlens__get_service_graph" in card
    assert "■ done · 1 calls" in card, "the card ends, so its watcher (and the task card) finishes"
    assert "discovery ▶ started" in (tmp_path / "logs" / "activity.log").read_text()
    prompt = (tmp_path / "prompt.txt.disc").read_text()
    assert "Labs checkout" in prompt and "source_url" not in prompt and "untrusted" in prompt
    assert "frontend" in prompt, "the agent is asked for UI repos too (no surface table any more)"
    assert '"maybe"' in prompt and "recall" in prompt and "passes through unchanged" not in prompt
    assert argv[argv.index("--model") + 1] == c.DISCOVERY_MODEL == "claude-opus-5-5"


def test_discovery_model_from_config(tmp_path, monkeypatch):
    cfg = _fake(tmp_path, monkeypatch)
    cfg.d["SCOPE.DISCOVERY_MODEL"] = "claude-sonnet-5-5"
    assert c.discover(PRD, cfg, {"url": "u"}, log_dir=tmp_path / "logs")["ok"]
    argv = json.loads((tmp_path / "prompt.txt.argv").read_text())
    assert argv[argv.index("--model") + 1] == "claude-sonnet-5-5"


def test_discovery_failures_are_reported_not_raised(tmp_path, monkeypatch):
    spec = {"url": "u"}
    cfg = _fake(tmp_path, monkeypatch, "nojson")
    d = c.discover(PRD, cfg, spec, log_dir=tmp_path)
    assert not d["ok"] and d["services"] == [] and "no services JSON" in d["error"] and d["cost_usd"] == 0.42
    monkeypatch.setenv("FAKE_MODE", "limit")
    d = c.discover(PRD, cfg, spec, log_dir=tmp_path)
    assert not d["ok"] and "weekly limit" in d["error"]
    assert c.discover(PRD, Cfg(), spec)["error"].startswith("test guard")  # the real claude is never started


def test_agent_services_become_repos(tmp_path, monkeypatch):
    monkeypatch.setattr(c, "repo_for_service", lambda root, svc, aliases, config=None:
                        "svc_a" if svc.startswith("svc_a_") else svc)
    found = [{"service": "API-Gateway", "why": "routes it"}, {"service": "svc_a", "why": "owns orders"},
             {"service": "svc_a_admin_service", "why": "variant"}, {"service": "b", "why": "x"},
             {"service": "c", "why": ""}]
    got = c.context_candidates(found, tmp_path, None, {"b"})
    assert [p["repo"] for p in got] == ["api-gateway", "svc_a", "c"], "lower-cased slug, variants merged, b excluded"
    assert got[0]["why"] == "context layer: routes it" and got[2]["why"] == "context layer: service c"


def test_maybe_only_when_every_service_behind_the_repo_is_maybe(tmp_path, monkeypatch):
    monkeypatch.setattr(c, "repo_for_service", lambda root, svc, aliases, config=None:
                        "svc_a" if svc.startswith("svc_a") else svc)
    found = [{"service": "svc_a_admin_service", "tier": "maybe", "why": "variant"},
             {"service": "svc_a", "tier": "changes", "why": "owns orders"},
             {"service": "email", "tier": "maybe", "why": "attachments"}]
    got = c.context_candidates(found, tmp_path, None, set())
    assert got == [{"repo": "svc_a", "why": "context layer: owns orders", "maybe": False},
                   {"repo": "email", "why": "context layer (maybe): attachments", "maybe": True}]


def test_gather_order_and_meta(tmp_path, monkeypatch):
    cfg = _fake(tmp_path, monkeypatch)
    monkeypatch.setattr(c, "_repo_for", lambda root, svc, aliases, cfg: svc.lower())
    monkeypatch.setattr(c, "prd_mentioned", lambda *a: [{"repo": "named_in_prd", "why": "the PRD mentions it"}])
    meta: dict = {}
    out = c.gather(cfg, {"url": "u"}, True, PRD, tmp_path, ["mine"], meta=meta, log_dir=tmp_path / "logs")
    named = c.gather(cfg, {"url": "u"}, True, PRD, tmp_path, ["ns_gateway"], log_dir=tmp_path / "logs")
    assert all(o["include"] for o in named if o["repo"] == "ns_gateway"), "named by you wins over maybe"
    assert [o["repo"] for o in out] == ["mine", "api-gateway", "svc_a", "svc_a_admin_service", "ns_gateway",
                                        "named_in_prd"]
    assert [o["repo"] for o in out if not o["include"]] == ["ns_gateway"], "a maybe is proposed unticked"
    assert meta["discovery"]["ok"] and meta["discovery"]["tool_calls"] == 1 and meta["discovery_error"] is None
    assert meta["cost_usd"] == 0.42 and "summary" not in meta and "surfaces" not in meta
    json.dumps(meta)  # goes into scope.json


def test_gather_without_the_layer_has_named_and_mentioned_repos(tmp_path, monkeypatch):
    cfg = _fake(tmp_path, monkeypatch)
    monkeypatch.setattr(c, "discover", lambda *a, **k: pytest.fail("no agent when the layer is down"))
    monkeypatch.setattr(c, "prd_mentioned", lambda *a: [{"repo": "named_in_prd", "why": "the PRD mentions it"}])
    meta: dict = {}
    out = c.gather(cfg, {"url": "u"}, False, PRD, tmp_path, ["mine"], meta=meta)
    assert [o["repo"] for o in out] == ["mine", "named_in_prd"] and meta["discovery"] is None


def test_discovery_timeout_kills_the_agent_and_closes_its_card(tmp_path, monkeypatch):
    cfg = _fake(tmp_path, monkeypatch)
    slow = tmp_path / "slow"
    slow.write_text("#!/bin/sh\ncat >/dev/null\necho '{\"type\":\"system\",\"subtype\":\"init\"}'\nsleep 30\n")
    slow.chmod(0o755)
    cfg.d["AGENT.CLAUDE_BIN"] = str(slow)
    monkeypatch.setattr(c, "DISCOVERY_TIMEOUT_S", 1)
    d = c.discover(PRD, cfg, {"url": "u"}, log_dir=tmp_path / "logs")
    assert not d["ok"] and "TimeoutExpired" in d["error"] and d["seconds"] < 10
    assert "■ FAILED: timed out after 1s" in (tmp_path / "logs" / "agents" / "scope.discovery.log").read_text()


def test_scratch_folder_with_the_token_is_removed(tmp_path, monkeypatch):
    """No run folder → the MCP config (it carries the fleetlens token) goes in a scratch folder that is deleted."""
    import tempfile

    cfg = _fake(tmp_path, monkeypatch)
    made: list[str] = []
    real = tempfile.mkdtemp
    monkeypatch.setattr(tempfile, "mkdtemp", lambda **kw: made.append(real(dir=tmp_path, **kw)) or made[-1])
    assert c.discover(PRD, cfg, {"url": "u", "headers": {"Authorization": "Bearer t"}})["ok"]
    assert made and not any(os.path.exists(d) for d in made), "the token-bearing scratch folder is gone"


def test_exit_zero_error_keeps_its_reason_on_the_card(tmp_path, monkeypatch):
    """Claude reports a usage limit and exits 0: the card says why, not `exit 0`."""
    cfg = _fake(tmp_path, monkeypatch, "limit")
    d = c.discover(PRD, cfg, {"url": "u"}, log_dir=tmp_path / "logs")
    assert not d["ok"] and "weekly limit" in d["error"]
    card = (tmp_path / "logs" / "agents" / "scope.discovery.log").read_text()
    assert "FAILED" in card and "weekly limit" in card and "exit 0" not in card


def test_a_failing_callback_kills_the_agent(tmp_path, monkeypatch):
    """A progress callback that raises must not leave the agent running (and spending) in the background."""
    import time

    from rfc_agent.investigate import agent as ag

    cfg = _fake(tmp_path, monkeypatch)
    slow = tmp_path / "slow"
    pid = tmp_path / "child.pid"
    slow.write_text(f"#!/bin/sh\necho $$ > {pid}\ncat >/dev/null\n"
                    "echo '{\"type\":\"system\",\"subtype\":\"init\"}'\nsleep 30\n")
    slow.chmod(0o755)
    cfg.d["AGENT.CLAUDE_BIN"] = str(slow)

    def boom(*a, **k):
        raise RuntimeError("callback failed")

    monkeypatch.setattr(ag, "_consume", boom)
    t0 = time.monotonic()
    with pytest.raises(RuntimeError, match="callback failed"):
        c.discover(PRD, cfg, {"url": "u"}, log_dir=tmp_path / "logs")
    assert time.monotonic() - t0 < 10, "did not wait for the 30 s sleep"
    child = int(pid.read_text())
    with pytest.raises(ProcessLookupError):
        os.kill(child, 0)  # gone


def test_model_names_that_are_not_plain_repo_names_are_dropped(tmp_path, monkeypatch):
    monkeypatch.setattr(c, "repo_for_service", lambda root, svc, aliases, config=None: svc)
    found = [{"service": s, "why": "x"} for s in ("..", "../other", "a/b", ".hidden", "-rf", "ok_repo", "Good-Repo.v2")]
    got = c.context_candidates(found, tmp_path, None, set())
    assert [p["repo"] for p in got] == ["ok_repo", "good-repo.v2"]


def test_plus_name_rejects_path_like_names():
    from rfc_agent.scope import confirm
    items = [confirm.Item("repo", "svc_a", "svc_a")]
    err = confirm.apply(items, "+../x, fine", lambda n: confirm.Item("repo", n, n))
    assert err and "../x" in err and [it.key for it in items] == ["svc_a"], "nothing is added when one name is bad"
    assert confirm.apply(items, "+fine", lambda n: confirm.Item("repo", n, n)) is None
    assert [it.key for it in items] == ["svc_a", "fine"]


@pytest.mark.parametrize("mode, calls", [("nolookups", 0), ("failedlookups", 1)])
def test_services_named_without_a_successful_lookup_are_not_discovery(tmp_path, monkeypatch, mode, calls):
    """Issue #16: the agent could reach no fleetlens tool and named eight repos from the PRD's words alone,
    none of which existed. Such an answer is a failed discovery, never a list of candidates."""
    cfg = _fake(tmp_path, monkeypatch, mode)
    d = c.discover(PRD, cfg, {"url": "u"}, log_dir=tmp_path)
    assert not d["ok"] and d["services"] == [] and d["lookups"] == 0 and d["tool_calls"] == calls
    assert "0 successful fleetlens lookups" in d["error"]
    meta: dict = {}
    got = c.gather(cfg, {"url": "u"}, True, PRD, tmp_path, [], meta=meta, log_dir=tmp_path)
    assert got == [] and "0 successful" in meta["discovery_error"]


def test_a_successful_lookup_is_counted(tmp_path, monkeypatch):
    d = c.discover(PRD, _fake(tmp_path, monkeypatch), {"url": "u"}, log_dir=tmp_path)
    assert d["ok"] and d["lookups"] == 1


# ---- health: usable, not just reachable ------------------------------------------------------------


class FakeMcp:
    """steering._mcp_call stand-in: answers by method, per scenario."""

    def __init__(self, init=True, tools=None, probe=None):
        self.init, self.tools, self.probe = init, tools, probe
        self.methods: list[str] = []
        self.timeouts: list = []

    def __call__(self, url, headers, method, params, session=None, timeout=None):
        self.methods.append(method)
        self.timeouts.append(timeout)
        if method == "initialize":
            return ({"serverInfo": {}} if self.init else None), "sid"
        if method == "tools/list":
            return ({"tools": [{"name": n} for n in self.tools]} if self.tools is not None else None), "sid"
        if method == "tools/call":
            return self.probe, "sid"
        return {}, "sid"


ALL = list(c.READONLY_TOOLS["fleetlens"])
SPEC = {"url": "http://fl.example:9995/mcp", "headers": {"Authorization": "Bearer t"}}


@pytest.mark.parametrize("fake, ok, words", [
    (FakeMcp(tools=ALL, probe={"content": [{"type": "text", "text": "{}"}]}), True,
     "15/15 tools · get_index_info answered"),
    (FakeMcp(tools=ALL[1:], probe={"content": []}), True, "missing: get_service_graph"),
    (FakeMcp(tools=None), False, "lists no tools"),
    (FakeMcp(tools=["something_else"]), False, "none of the 15 read tools"),
    (FakeMcp(tools=[t for t in ALL if t != "get_index_info"]), False, "no get_index_info tool"),
    (FakeMcp(tools=ALL, probe=None), False, "get_index_info call failed"),
    (FakeMcp(tools=ALL, probe={"isError": True, "content": [{"type": "text", "text": "unauthorized"}]}), False,
     "call failed: unauthorized"),
])
def test_health_proves_the_tools_work(monkeypatch, fake, ok, words):
    from rfc_agent import steering

    monkeypatch.setattr(steering, "_mcp_call", fake)
    got_ok, why = c.health(SPEC, attempts=1)
    assert got_ok is ok and words in why, why


@pytest.mark.parametrize("probe, words", [
    ((401, ""), "refused the token (HTTP 401)"), ((404, ""), "HTTP 404"),
    ((None, "no answer"), "did not answer within 30s (VPN off?)"),
    ((None, "TLS failed: certificate verify failed"), "fl.example:9995: TLS failed: certificate verify failed"),
    ((None, "bad URL: unknown url type"), "bad URL"),
])
def test_health_says_why_the_server_did_not_greet(monkeypatch, probe, words):
    from rfc_agent import steering

    monkeypatch.setattr(steering, "_mcp_call", FakeMcp(init=False))
    monkeypatch.setattr(c, "_http_status", lambda url, headers, timeout=5: probe)
    ok, why = c.health(SPEC, attempts=1)
    assert not ok and words in why
    if probe[1].startswith(("TLS", "bad URL")):
        assert "VPN" not in why  # not misreported as an outage


def test_health_has_one_overall_deadline(monkeypatch):
    """A host that swallows packets: each step gets only what is left of one deadline, not a full timeout each."""
    from rfc_agent import steering

    fake = FakeMcp(init=False)
    clock = {"t": 0.0}

    def slow(*a, **kw):  # every initialize burns its whole timeout
        clock["t"] += kw.get("timeout") or 12
        return fake(*a, **kw)

    monkeypatch.setattr(steering, "_mcp_call", slow)
    monkeypatch.setattr(c.time, "monotonic", lambda: clock["t"])
    monkeypatch.setattr(c.time, "sleep", lambda s: clock.__setitem__("t", clock["t"] + s))
    seen = []
    def status(url, headers, timeout=5):
        seen.append(timeout)
        return None, "no answer"

    monkeypatch.setattr(c, "_http_status", status)
    ok, why = c.health(SPEC, attempts=3, deadline_s=30)
    assert not ok and clock["t"] <= 31 and seen and seen[0] <= 5
    assert all(t <= 12 for t in fake.timeouts)


@pytest.mark.parametrize("raised, want", [
    ("http401", (401, "")), ("tls", (None, "TLS failed")), ("refused", (None, "no answer")),
    ("timeout", (None, "no answer")), ("badurl", (None, "bad URL")),
])
def test_http_status_names_the_failure(monkeypatch, raised, want):
    import ssl
    import urllib.error
    import urllib.request

    def boom(req, timeout):
        if raised == "http401":
            raise urllib.error.HTTPError(req.full_url, 401, "no", {}, None)
        if raised == "tls":
            raise urllib.error.URLError(ssl.SSLError("certificate verify failed"))
        if raised == "refused":
            raise urllib.error.URLError(ConnectionRefusedError("refused"))
        if raised == "timeout":
            raise TimeoutError("timed out")
        raise ValueError("unknown url type")

    monkeypatch.setattr(urllib.request, "urlopen", boom)
    status, why = c._http_status("http://fl.example/mcp", {})
    assert status == want[0] and why.startswith(want[1])


def test_an_axon_only_setup_still_runs_discovery(tmp_path, monkeypatch):
    """PR #31 review: no fleetlens in O1's config, the operator's own Axon entry in effect — the health check
    said ok, and discovery was skipped because it was gated on O1's config alone."""
    from rfc_agent import axon_agent

    monkeypatch.setattr(c, "_on_axon", lambda cfg: True)
    monkeypatch.setattr(axon_agent, "context_access", lambda cfg: {"mode": "codemode"})
    ran = []
    monkeypatch.setattr(c, "discover", lambda prd, cfg, spec, log_dir=None: ran.append(spec) or {
        "ok": False, "services": [], "error": "x", "tool_calls": 0, "lookups": 0, "seconds": 0, "cost_usd": 0})
    c.gather(Cfg(), None, True, PRD, tmp_path, [], meta={}, log_dir=tmp_path)
    assert ran == [None]
    monkeypatch.setattr(axon_agent, "context_access", lambda cfg: {"mode": "off"})
    c.gather(Cfg(), None, True, PRD, tmp_path, [], meta={}, log_dir=tmp_path)
    assert ran == [None]  # nothing usable: no discovery
    monkeypatch.setattr(c, "_on_axon", lambda cfg: False)
    c.gather(Cfg(), None, True, PRD, tmp_path, [], meta={}, log_dir=tmp_path)
    assert ran == [None]  # Claude still needs O1's config
