"""Repo <-> deployed-service mapping from the devops Ansible inventory (research verdict 2026-08-15).

The org catalog (the context layer, now fleetlens) keys everything by SERVICE name while this pipeline works in
REPO (checkout-dir) names. Verified live 2026-08-15: steering queried a web repo by its checkout name
-> null while the catalog indexes that repo's deployments under two service names. The devops inventory
is the org's own authoritative repo->service map (~200 entries; one repo may deploy several variants),
so we parse it instead of hand-maintaining aliases. Where it lives is the org's config, never the code:
config.json SERVICES_MAP.REPO (the devops repo) + SERVICES_MAP.PATH (the file in it). No REPO -> no map.
STEERING.REPO_ALIASES stays as a manual override layer ON TOP — user config wins.

Source resolution (operator addition 2026-08-15 — the devops checkout is NOT guaranteed local):
1. local checkout: <repos_root>/<SERVICES_MAP.REPO>/<SERVICES_MAP.PATH> (cheapest, parse per process);
2. disk cache of the PARSED map (~/.config/rfc-agent/services_map.json, 7d TTL — the file changes
   rarely; steering lesson kept: a FAILED fetch is never cached, so misses retry next run);
3. bitbucket MCP get_file_content, harness-side over the pipeline's configured stdio server,
   read-only; on success the parsed map is written to the disk cache;
4. soft-fail: warn once and return {} — callers degrade to STEERING.REPO_ALIASES / identity.
A mapping miss must never block a run.

Trust order (research: the catalog is a MAP, not testimony — code > catalog): this module only
routes lookups; nothing resolved through it is ever citable evidence.

Design grounding: LocAgent arXiv:2503.09089 (graph-guided localization, -86% cost) and Agentless
arXiv:2407.01489 (hierarchical narrowing) — a cheap structural prior routes exploration; it never
substitutes for reading code.

Parsing is line-wise on purpose: no YAML dependency exists in this project, and the inventory is
machine-shaped Ansible (top-level list items at 2-space indent, keys one level deeper; nested
host lists sit deeper and never carry repo_name/service_name). Any read/parse problem soft-fails
to an empty map.
"""

from __future__ import annotations

import json
import re
import sys
import time
from functools import lru_cache
from pathlib import Path

_DEFAULT_INVENTORY_PATH = "group_vars/services_k8s"


def inventory_location(config) -> tuple[str, str] | None:
    """(devops repo, file path in it) from config SERVICES_MAP, or None when the org configured none."""
    sec = (config.get("SERVICES_MAP") if config else None) or {}
    repo = str(sec.get("REPO") or "").strip()
    return (repo, str(sec.get("PATH") or _DEFAULT_INVENTORY_PATH)) if repo else None
_ITEM_RE = re.compile(r"^ {0,2}- ")  # top-level list item; nested lists (hosts) indent deeper
_KEY_RE = re.compile(r"""^\s+(?:- )?(repo_name|service_name):\s*["']?([\w.-]+)""")

_DISK_CACHE = Path("~/.config/rfc-agent/services_map.json").expanduser()  # steering's cache home
_CACHE_TTL_S = 7 * 86400
_STDIO_TIMEOUT_S = 45

_MEM: dict[str, dict[str, tuple[str, ...]]] = {}  # per-process memo for the non-local path
_WARNED = False


def _parse_text(text: str) -> dict[str, tuple[str, ...]]:
    out: dict[str, set[str]] = {}
    repo: str | None = None
    svc: str | None = None
    for ln in text.splitlines() + ["- "]:  # sentinel flushes the last item
        if _ITEM_RE.match(ln):
            if repo and svc:  # orphans (missing either key) are skipped, not guessed
                out.setdefault(repo, set()).add(svc)
            repo = svc = None
        m = _KEY_RE.match(ln)
        if m:
            if m.group(1) == "repo_name":
                repo = m.group(2)
            else:
                svc = m.group(2)
    return {r: tuple(sorted(s)) for r, s in out.items()}


@lru_cache(maxsize=8)  # per-run cache: the pipeline is one process; keyed by resolved path
def _parse_file(path_str: str) -> dict[str, tuple[str, ...]]:
    try:
        return _parse_text(Path(path_str).read_text(encoding="utf-8", errors="replace"))
    except OSError:
        return {}


def _stdio_tool_call(spec: dict, tool: str, arguments: dict) -> dict | None:
    """One tool call over a stdio MCP server (spawn -> initialize -> tools/call -> kill).
    None on any failure; a watchdog kills a hung server so the harness never blocks."""
    import os
    import subprocess
    import threading

    cmd = [spec.get("command", "")] + [str(a) for a in (spec.get("args") or [])]
    if not cmd[0]:
        return None
    env = {**os.environ, **{k: str(v) for k, v in (spec.get("env") or {}).items()}}
    try:
        proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL, env=env, text=True)
    except OSError:
        return None
    watchdog = threading.Timer(_STDIO_TIMEOUT_S, proc.kill)
    watchdog.start()
    try:
        def send(obj: dict) -> None:
            proc.stdin.write(json.dumps(obj) + "\n")
            proc.stdin.flush()

        def read_result(want_id: int) -> dict | None:
            while True:
                line = proc.stdout.readline()  # '' once the watchdog kills the server
                if not line:
                    return None
                try:
                    msg = json.loads(line)
                except json.JSONDecodeError:
                    continue  # servers may log non-JSON lines to stdout; skip them
                if msg.get("id") == want_id:
                    return msg.get("result")

        send({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": "2025-03-26", "capabilities": {},
            "clientInfo": {"name": "rfc-agent-services-map", "version": "1"}}})
        if read_result(1) is None:
            return None
        send({"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}})
        send({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
              "params": {"name": tool, "arguments": arguments}})
        return read_result(2)
    except Exception:  # noqa: BLE001 — accelerator, never a failure source
        return None
    finally:
        watchdog.cancel()
        proc.kill()


def _fetch_inventory(config) -> str | None:
    """Inventory text via the configured bitbucket MCP (read-only get_file_content); None on any
    failure or when the response doesn't look like the inventory. stdio servers only — that is
    what the pipeline configures for bitbucket."""
    spec = ((config.get("MCP", {}) or {}).get("mcpServers", {}) or {}).get("bitbucket") if config else None
    if not (isinstance(spec, dict) and spec.get("command")):
        return None
    workspace = (spec.get("env") or {}).get("BITBUCKET_DEFAULT_WORKSPACE")
    where = inventory_location(config)
    if not (workspace and where):
        return None
    result = _stdio_tool_call(spec, "get_file_content", {
        "workspace": workspace, "repo_slug": where[0], "path": where[1]})
    if not result or result.get("isError"):
        return None
    text = next((c.get("text", "") for c in result.get("content", []) if c.get("type") == "text"), "")
    try:  # some servers wrap file bytes in a JSON envelope
        data = json.loads(text)
        if isinstance(data, dict):
            text = next((v for v in (data.get("content"), data.get("data"), data.get("file_content"))
                         if isinstance(v, str)), text)
    except json.JSONDecodeError:
        pass
    return text if "repo_name" in text else None  # error echoes are not the inventory


def load_map(repos_root: Path | str, config=None) -> dict[str, tuple[str, ...]]:
    """repo checkout name -> ALL deployed service-name variants. Empty when no source resolves."""
    global _WARNED
    where = inventory_location(config)
    if where is None:
        return {}
    local = Path(repos_root).expanduser() / where[0] / where[1]
    if local.exists():
        return _parse_file(str(local.resolve()))
    key = str(Path(repos_root).expanduser().resolve())
    if key in _MEM:
        return _MEM[key]
    try:  # disk cache of the PARSED map — fetch/parse once, not per run
        entry = json.loads(_DISK_CACHE.read_text())
        if time.time() - entry.get("fetched_at", 0) < _CACHE_TTL_S and entry.get("map"):
            _MEM[key] = {r: tuple(s) for r, s in entry["map"].items()}
            return _MEM[key]
    except (OSError, json.JSONDecodeError, AttributeError):
        pass
    text = _fetch_inventory(config)
    mapping = _parse_text(text) if text else {}
    if mapping:  # a FAILED fetch is never cached — it must retry next run (steering lesson)
        try:
            _DISK_CACHE.parent.mkdir(parents=True, exist_ok=True)
            _DISK_CACHE.write_text(json.dumps(
                {"fetched_at": time.time(), "map": {r: list(s) for r, s in mapping.items()}}))
        except OSError:
            pass
    elif not _WARNED:
        _WARNED = True
        print(f"  [services-map] WARN no repo<->service inventory (no local {where[0]} checkout, "
              "bitbucket fetch failed) — catalog lookups fall back to STEERING.REPO_ALIASES only",
              file=sys.stderr)
    _MEM[key] = mapping
    return mapping


def repo_for_service(repos_root: Path | str, service: str, aliases: dict | None = None,
                     config=None) -> str:
    """Local checkout name for a catalog service name; identity when unmapped."""
    rev = {s: r for r, svcs in load_map(repos_root, config).items() for s in svcs}
    if aliases:
        rev.update({v: k for k, v in aliases.items()})  # user config wins over the inventory
    return rev.get(service, service)
