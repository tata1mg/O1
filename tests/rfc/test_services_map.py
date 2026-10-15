"""Repo<->service inventory mapping (research verdict 2026-08-15): the catalog keys by SERVICE
name, the pipeline by checkout dir — verified live: "web_app_b" -> null while the catalog indexes
web_desktop/web_mobile. The devops Ansible inventory is the authoritative map; aliases override it."""

import json
import time

import rfc_agent.services_map as sm
from rfc_agent.services_map import load_map, repo_for_service

_INV = """\
registry:

  - repo_name: vyked
    repo_owner: example-org
    service_name: registry

services:

  - port_end: 7010
    repo_name: svc_a
    run_command: python3 -m svc_a.service
    service_name: svc_a_service
    owner: "Pharma"

  - repo_name: svc_a
    service_name: svc_a_background_service

  - repo_name: web_app_a
    service_name: web_desktop
    hosts:
      - www.example.com:
          - web

  - repo_name: web_app_a
    service_name: web_mobile

  - repo_name: orphan_repo
    repo_owner: example-org

  - service_name: orphan_service
"""


class _Inv:
    """The org's inventory location (config.json SERVICES_MAP); extra keys layered on top."""

    def __init__(self, **extra):
        self.d = {"SERVICES_MAP": {"REPO": "devops", "PATH": "group_vars/services_k8s"}, **extra}

    def get(self, k, d=None):
        return self.d.get(k, d)


INV = _Inv()


def _root(tmp_path):
    inv = tmp_path / "devops" / "group_vars"
    inv.mkdir(parents=True)
    (inv / "services_k8s").write_text(_INV)
    return tmp_path


def test_multi_variant_repos_collect_all_service_names(tmp_path):
    m = load_map(_root(tmp_path), INV)
    assert m["svc_a"] == ("svc_a_background_service", "svc_a_service")
    assert m["web_app_a"] == ("web_desktop", "web_mobile")
    assert m["vyked"] == ("registry",)  # registry section entries map too


def test_orphan_entries_are_skipped_not_guessed(tmp_path):
    m = load_map(_root(tmp_path), INV)
    assert "orphan_repo" not in m
    assert all("orphan_service" not in svcs for svcs in m.values())


def test_repo_for_service_reverse_lookup_and_alias_override(tmp_path):
    root = _root(tmp_path)
    assert repo_for_service(root, "web_mobile", config=INV) == "web_app_a"
    assert repo_for_service(root, "never_heard_of", config=INV) == "never_heard_of"
    # user alias beats the inventory in reverse too
    assert repo_for_service(root, "web_mobile", {"my_checkout": "web_mobile"}, config=INV) == "my_checkout"


def test_no_inventory_configured_means_no_map_and_no_fetch(tmp_path, monkeypatch):
    """Nothing org-specific in the code: without SERVICES_MAP.REPO there is no map, even with a checkout."""
    monkeypatch.setattr(sm, "_stdio_tool_call", lambda *a: (_ for _ in ()).throw(AssertionError("no fetch")))
    assert load_map(_root(tmp_path), None) == {}
    assert load_map(tmp_path, _Inv(SERVICES_MAP={})) == {}


def test_remote_fetch_populates_and_reuses_disk_cache(tmp_path, monkeypatch):
    """Operator addition 2026-08-15: no local example-devops checkout -> fetch via the bitbucket MCP
    once, cache the PARSED map on disk (7d TTL); later runs read the cache, not the network."""
    cache = tmp_path / "cache.json"
    monkeypatch.setattr(sm, "_DISK_CACHE", cache)
    monkeypatch.setattr(sm, "_MEM", {})
    calls = []

    def fake_stdio(spec, tool, arguments):
        calls.append((tool, arguments["repo_slug"], arguments["path"]))
        return {"content": [{"type": "text", "text": _INV}]}

    monkeypatch.setattr(sm, "_stdio_tool_call", fake_stdio)

    def Cfg():  # noqa: N802 — reads like the class it replaces
        return _Inv(MCP={"mcpServers": {"bitbucket": {
            "command": "node", "env": {"BITBUCKET_DEFAULT_WORKSPACE": "acme"}}}})

    root = tmp_path / "no-checkout-here"
    root.mkdir()
    m = load_map(root, Cfg())
    assert m["svc_a"] == ("svc_a_background_service", "svc_a_service")
    assert calls == [("get_file_content", "devops", "group_vars/services_k8s")]
    entry = json.loads(cache.read_text())
    assert entry["map"]["web_app_a"] == ["web_desktop", "web_mobile"]

    # fresh process simulation: memo cleared, fetch broken — the disk cache answers
    monkeypatch.setattr(sm, "_MEM", {})
    monkeypatch.setattr(sm, "_stdio_tool_call", lambda *a: (_ for _ in ()).throw(AssertionError("no refetch")))
    assert load_map(root, Cfg())["svc_a"] == ("svc_a_background_service", "svc_a_service")

    # expired cache -> refetch path re-engages (and here it fails -> soft {})
    entry["fetched_at"] = time.time() - 8 * 86400
    cache.write_text(json.dumps(entry))
    monkeypatch.setattr(sm, "_MEM", {})
    monkeypatch.setattr(sm, "_stdio_tool_call", lambda *a: None)
    monkeypatch.setattr(sm, "_WARNED", False)
    assert load_map(root, Cfg()) == {}
