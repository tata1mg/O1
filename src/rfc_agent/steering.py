"""Per-repo conventions steering, sourced from the org context layer (v7 postmortem, 2026-08-10).

Why: v7's worst review blocker was an INVENTED call shape — the composer wrote an unguarded
`FeatureFlags...evaluate(...)` while the repo's idiom (`is_feature_flagging_enabled()` gating,
svc_a/app/utils/utils.py:2438) appeared in none of its inputs, because no REQ anchor points at
conventions. The org's context-service already maintains exactly this knowledge per repo
(`get_repository_context`: coding_patterns, shared_libraries, important_classes — its svc_a entry
literally names the flag idiom), so we consume it instead of building a Kiro-style steering generator
of our own (probed 2026-08-10: 25 services indexed; state_feeder/signer/svc_d not yet).

Contract: steering is an ACCELERATOR, never a failure source — any fetch/parse problem silently yields
no steering, same as the evidence pack. Steering prose is guidance, never citable evidence: the compose
prompt requires each borrowed idiom to be anchored with a grep'd citation, and the resolve gate checks
those as usual, so a stale index self-corrects into a gate warning instead of a wrong RFC.

Cache: ~/.config/rfc-agent/steering/<repo>.json (raw tool result + fetched_at, 24h TTL) — repo
conventions rot slowly; the source_ref/generated_at stamps ride into steering.md for visibility.
"""

from __future__ import annotations

import json
import time
import urllib.request
from pathlib import Path

_CACHE_DIR = Path("~/.config/rfc-agent/steering").expanduser()
# TTL is a CONFIG KNOB (STEERING.TTL_HOURS, default 24): today the upstream index regenerates
# rarely, so 24h adds negligible staleness — but the context team is moving to commit-fresh
# indexing, at which point OUR cache becomes the dominant staleness source and the knob should
# drop to ~1h (or 0 to disable). Deliberately NOT HEAD-compare busting until then: local repos
# get daily commits while the index lags weeks, so HEAD-mismatch would re-fetch on nearly every
# run and receive the same old document — the exact waste the cache prevents.
_DEFAULT_TTL_H = 24
_HTTP_TIMEOUT_S = 12


def _mcp_call(url: str, headers: dict, method: str, params: dict,
              session: str | None = None) -> tuple[dict | None, str | None]:
    """One JSON-RPC call over MCP streamable HTTP. Returns (result, session_id). None on any failure."""
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
    req = urllib.request.Request(url, data=body, method="POST", headers={
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        **({"Mcp-Session-Id": session} if session else {}),
        **(headers or {}),
    })
    try:
        with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT_S) as resp:
            sid = resp.headers.get("Mcp-Session-Id") or session
            raw = resp.read().decode("utf-8", errors="replace")
    except Exception:
        return None, None
    # streamable HTTP may frame the JSON as SSE `data:` lines
    if raw.lstrip().startswith("event:") or "\ndata:" in raw or raw.startswith("data:"):
        payloads = [ln[5:].strip() for ln in raw.splitlines() if ln.startswith("data:")]
        raw = payloads[-1] if payloads else ""
    try:
        return json.loads(raw).get("result"), sid
    except (json.JSONDecodeError, AttributeError):
        return None, sid


def _tool_result(url: str, headers: dict, tool: str, arguments: dict) -> dict | None:
    """initialize -> initialized -> tools/call, one session. Raw tool result, None on any failure."""
    init, sid = _mcp_call(url, headers, "initialize", {
        "protocolVersion": "2025-03-26",
        "capabilities": {},
        "clientInfo": {"name": "rfc-agent-steering", "version": "1"},
    })
    if init is None:
        return None
    _mcp_call(url, headers, "notifications/initialized", {}, session=sid)
    result, _ = _mcp_call(url, headers, "tools/call",
                          {"name": tool, "arguments": arguments}, session=sid)
    return result or None


def _result_text(result: dict | None) -> str | None:
    """First text block of a tool result; None when absent."""
    try:
        return next(c.get("text", "") for c in result.get("content", []) if c.get("type") == "text")
    except (StopIteration, AttributeError):
        return None


def fetch_repo_context(url: str, headers: dict, repo: str) -> dict | None:
    """`get_repository_context` for one repo via the context-service MCP. None on any failure."""
    text = _result_text(_tool_result(url, headers, "get_repository_context",
                                     {"repository_id": f"repository:{repo}"}))
    if text is None:
        return None
    try:
        data = json.loads(text)
        # a miss echoes the expected-format help instead of a context document
        return data if isinstance(data, dict) and data.get("coding_patterns") else None
    except json.JSONDecodeError:
        return None


def _cached_context(url: str, headers: dict, repo: str, ttl_s: float,
                    services: list[str] | None = None) -> dict | None:
    _CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cf = _CACHE_DIR / f"{repo}.json"
    if cf.exists():
        try:
            entry = json.loads(cf.read_text())
            if time.time() - entry.get("fetched_at", 0) < ttl_s and entry.get("context") is not None:
                # nulls are NOT held for the TTL: a cached miss blinded runs for a full day after the
                # context team indexed a repo (observed 2026-08-12: lab_buddy/web_app_b nulls from the
                # previous evening served while they were the run's two load-bearing repos)
                return entry.get("context")
        except (json.JSONDecodeError, OSError):
            pass
    ctx = None
    for svc in services or [repo]:  # deployed variants in order; first indexed one answers for the repo
        ctx = fetch_repo_context(url, headers, svc)
        if ctx:
            break
    try:
        cf.write_text(json.dumps({"fetched_at": time.time(), "context": ctx}))
    except OSError:
        pass
    return ctx


def _distill(repo: str, data: dict) -> str:
    """Compact per-repo steering block from a repository-context document (~1-2KB)."""
    meta = data.get("_meta", {})
    out = [f"## {repo} (context-layer, source_ref {str(meta.get('source_ref', '?'))[:10]}, "
           f"generated {str(meta.get('generated_at', '?'))[:10]})"]
    pats = data.get("coding_patterns") or []
    if pats:
        out.append("Patterns:")
        out += [f"- {p.get('pattern')}: {p.get('description')} (see {', '.join(p.get('evidence', [])[:2])})"
                for p in pats[:8]]
    libs = data.get("shared_libraries") or []
    if libs:
        out.append("Shared libraries:")
        out += [f"- {lb.get('name')} ({lb.get('type')}): {lb.get('usage')}" for lb in libs[:10]]
    classes = data.get("important_classes") or []
    if classes:
        out.append("Key classes: " + "; ".join(
            f"{c.get('name')} ({c.get('path')})" for c in classes[:6]))
    return "\n".join(out)


