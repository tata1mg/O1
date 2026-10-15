"""Scope repo discovery: the fleetlens discovery agent, the Haiku PRD summary, the surface table, fallbacks."""

from __future__ import annotations

import json

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
        {"type": "tool_result", "tool_use_id": "t1", "content": "graph", "is_error": False}]}}
    services = {"services": [{"service": "service:API-Gateway", "why": "routes the new endpoint"},
                             {"service": "svc_a", "why": "  owns the order\n patient name "},
                             {"service": "svc_a_admin_service", "why": "same repo"}]}
    draft = json.dumps({"services": [{"service": "draft"}]})
    text = {"ok": f"Looked around. {draft} then {json.dumps(services)}", "nojson": "I could not decide."}.get(mode, "")
    final = {"type": "result", "result": text, "total_cost_usd": 0.42, "is_error": mode == "limit",
             "usage": {"input_tokens": 9}}
    if mode == "limit":
        final["result"] = "You've hit your weekly limit"
    for ev in ({"type": "system", "subtype": "init", "mcp_servers": [{"name": k, "status": "connected"}
                                                                     for k in mcp["mcpServers"]]}, use, res, final):
        print(json.dumps(ev))
    sys.exit(0)
open(os.environ["FAKE_PROMPT"], "w").write(prompt)
if mode in ("ok", "nojson"):
    summary = "Consumer web checkout lets users pick an existing patient; patient service checks similar names."
    res = {"result": json.dumps({"summary": summary, "surfaces": ["consumer_web", "phlebo_app", "nope"]}),
           "total_cost_usd": 0.012}
elif mode == "limit":
    res = {"is_error": True, "result": "You've hit your weekly limit"}
else:
    res = {"result": "sorry, no json"}
print(json.dumps(res))
'''


# the org's surface table (config.json SCOPE.SURFACES) — nothing org-specific ships in the code
SURFACES = {
    "consumer_web": {"what": "customer website: storefront, cart, checkout", "repos": ["web_repo"]},
    "phlebo_app": {"what": "home sample collection app used by field staff", "repos": []},
}


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
    return Cfg(**{"AGENT.CLAUDE_BIN": str(fake), "SCOPE.SURFACES": SURFACES})


def test_summary_and_known_surfaces_only(tmp_path, monkeypatch):
    cfg = _fake(tmp_path, monkeypatch)
    s = c.summarise(PRD, cfg)
    assert s["ok"] and s["summary"].startswith("Consumer web checkout") and s["cost_usd"] == 0.012
    assert s["surfaces"] == ["consumer_web", "phlebo_app"]  # unknown names dropped
    prompt = (tmp_path / "prompt.txt").read_text()
    assert "- phlebo_app:" in prompt and "source_url" not in prompt and "Labs checkout" in prompt


def test_summary_failures_are_reported_not_raised(tmp_path, monkeypatch):
    cfg = _fake(tmp_path, monkeypatch, "limit")
    s = c.summarise(PRD, cfg)
    assert not s["ok"] and "weekly limit" in s["error"]
    monkeypatch.setenv("FAKE_MODE", "garbage")
    s = c.summarise(PRD, cfg)
    assert not s["ok"] and s["error"] == "no summary in the reply"
    assert c.summarise(PRD, Cfg())["error"].startswith("test guard")  # the real claude is never started in tests


def test_surface_table_from_config_and_unmapped():
    table = {"_note": "x", "consumer_web": {"what": "w", "repos": ["web_a", "web_b"]}, "phlebo_app": {"what": "p"}}
    cfg = Cfg(**{"SCOPE.SURFACES": table})
    hits, unmapped = c.surface_candidates(["consumer_web", "phlebo_app"], cfg, exclude={"web_b"})
    assert [h["repo"] for h in hits] == ["web_a"] and unmapped == ["phlebo_app"]
    assert set(c.surfaces_of(cfg)) == {"consumer_web", "phlebo_app"}
    assert c.surfaces_of(None) == {}, "no org table -> no surfaces (nothing org-specific in the code)"


def test_discovery_agent_is_isolated_and_parsed(tmp_path, monkeypatch):
    cfg = _fake(tmp_path, monkeypatch)
    spec = {"type": "http", "url": "http://fl/mcp", "headers": {"Authorization": "Bearer t"}}
    d = c.discover(PRD, cfg, spec, log_dir=tmp_path / "logs")
    assert d["ok"] and d["cost_usd"] == 0.42 and d["tool_calls"] == 1 and d["error"] is None
    assert d["services"] == [{"service": "API-Gateway", "why": "routes the new endpoint"},
                             {"service": "svc_a", "why": "owns the order patient name"},
                             {"service": "svc_a_admin_service", "why": "same repo"}], "the LAST services JSON wins"
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


def test_gather_order_and_meta(tmp_path, monkeypatch):
    cfg = _fake(tmp_path, monkeypatch)
    cfg.d["SCOPE.SURFACES"] = {"consumer_web": {"what": "w", "repos": ["web"]}, "phlebo_app": {"what": "p"}}
    monkeypatch.setattr(c, "_repo_for", lambda root, svc, aliases, cfg: svc.lower())
    monkeypatch.setattr(c, "prd_mentioned", lambda *a: [])
    meta: dict = {}
    out = c.gather(cfg, {"url": "u"}, True, PRD, tmp_path, ["mine"], meta=meta, log_dir=tmp_path / "logs")
    assert [o["repo"] for o in out] == ["mine", "api-gateway", "svc_a", "svc_a_admin_service", "web"]
    assert meta["discovery"]["ok"] and meta["discovery"]["tool_calls"] == 1 and meta["discovery_error"] is None
    assert meta["surfaces"] == ["consumer_web", "phlebo_app"] and meta["unmapped_surfaces"] == ["phlebo_app"]
    assert meta["cost_usd"] == 0.432  # agent + summary
    json.dumps(meta)  # goes into scope.json


def test_gather_without_the_layer_still_uses_surfaces(tmp_path, monkeypatch):
    cfg = _fake(tmp_path, monkeypatch)
    monkeypatch.setattr(c, "discover", lambda *a, **k: pytest.fail("no agent when the layer is down"))
    monkeypatch.setattr(c, "prd_mentioned", lambda *a: [])
    meta: dict = {}
    out = c.gather(cfg, {"url": "u"}, False, PRD, tmp_path, [], meta=meta)
    assert [o["repo"] for o in out] == ["web_repo"] and meta["discovery"] is None


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
