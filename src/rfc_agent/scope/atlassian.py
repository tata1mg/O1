"""Fetch Confluence/Jira through the operator's claude.ai Atlassian connector, byte-exact.

Python cannot ride the connector's OAuth, so a headless `claude -p` makes the calls — but the model only
CALLS the tools. Every file under raw/ is written by Python straight from the tool_result in the stream,
so no model ever re-types (or trims, or tidies) a PRD.

Connector timing (probed 2026-10-06, CLI 2.1.291): claude.ai connectors are still `pending` when the
first turn starts, so a single-turn `claude -p` sees no Atlassian tools at all. The session therefore
opens with a throwaway warm-up turn; by the second turn the connector reports `connected` and its tools
are bound. The session stays open (stream-json stdin) so the confirm step's extra pages are fetched as
later turns in the same session, without paying the warm-up again.
"""

from __future__ import annotations

import json
import re
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from .. import observe

# the claude.ai connector, or the user-scope `atlassian` server SETUP.md adds (same tools, same OAuth host)
PREFIXES = ("mcp__claude_ai_Atlassian__", "mcp__atlassian__")
TOOLS = ("getAccessibleAtlassianResources", "getConfluencePage", "getConfluencePageDescendants", "getJiraIssue")
DEFAULT_MODEL = "claude-haiku-4-5"
TURN_TIMEOUT_S = 180

PAGE_ID_RE = re.compile(r"/pages/(\d+)|[?&]pageId=(\d+)")
JIRA_KEY_RE = re.compile(r"\b([A-Z][A-Z0-9]{1,9}-\d+)\b")


# Atlassian's MCP server is sometimes slow to list its tools on a new connection: `claude mcp list` then
# says "tools fetch failed — Request timed out" and the session never binds them (seen 2026-10-12, about one
# connection in three). A fresh connection usually works, so connecting retries before it gives up.
# The usual case is plainer: the server takes 10-30 s to connect, longer than the warm-up turn, so the
# session waits, re-checking with a cheap turn, until it reports `connected` (2026-10-12: still `pending`
# 2 s in, `connected` at 12 s).
CONNECT_ATTEMPTS = 3
RETRY_WAIT_S = 5
PENDING_WAIT_S = 90
PENDING_POLL_S = 4


class AtlassianError(RuntimeError):
    def __init__(self, message: str, *, retryable: bool = False):
        super().__init__(message)
        self.retryable = retryable  # a fresh connection may work (a slow server), unlike a missing sign-in


@dataclass
class ToolCall:
    name: str
    input: dict
    text: str = ""
    is_error: bool = False


@dataclass
class TurnResult:
    calls: list[ToolCall] = field(default_factory=list)
    cost_usd: float = 0.0
    seconds: float = 0.0


def page_id_of(url_or_id: str) -> str | None:
    s = str(url_or_id).strip()
    if s.isdigit():
        return s
    m = PAGE_ID_RE.search(s)
    return (m.group(1) or m.group(2)) if m else None


def jira_key_of(s: str) -> str | None:
    m = JIRA_KEY_RE.search(str(s).upper())
    return m.group(1) if m else None


def _result_text(content) -> str:
    if isinstance(content, str):
        return content
    return "".join(b.get("text", "") for b in content or [] if isinstance(b, dict) and b.get("type") == "text")


class ConnectorSession:
    """One live headless session bound to the Atlassian connector. Use as a context manager."""

    def __init__(self, log_path: Path, *, claude_bin: str = "claude", model: str = DEFAULT_MODEL,
                 cwd: Path | None = None):
        self.log_path = log_path
        self.cmd = [claude_bin, "-p", "--input-format", "stream-json", "--output-format", "stream-json",
                    "--verbose", "--model", model,
                    "--tools", "",  # no built-in tools at all: it cannot read, write or run anything
                    "--allowedTools", ",".join(p + t for p in PREFIXES for t in TOOLS)]
        self.cwd = cwd
        self.model = model
        self.proc: subprocess.Popen | None = None
        self.connector_status = "unknown"
        self.cloud_id: str | None = None
        self._lines: list[dict] = []
        self._cv = threading.Condition()
        self._results = 0

    # ---- process plumbing --------------------------------------------------------------------
    def __enter__(self) -> ConnectorSession:
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        self.proc = subprocess.Popen(self.cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=subprocess.DEVNULL, text=True, cwd=self.cwd)
        threading.Thread(target=self._reader, daemon=True).start()
        return self

    def __exit__(self, *exc) -> None:
        if self.proc and self.proc.poll() is None:
            try:
                self.proc.stdin.close()
                self.proc.wait(timeout=10)
            except Exception:  # noqa: BLE001
                self.proc.kill()

    def _reader(self) -> None:
        with open(self.log_path, "a", encoding="utf-8") as log:
            for line in self.proc.stdout:
                log.write(line)
                try:
                    ev = json.loads(line)
                except ValueError:
                    continue
                with self._cv:
                    if ev.get("subtype") == "init":
                        found = [s.get("status", "unknown") for s in ev.get("mcp_servers") or []
                                 if "tlassian" in s.get("name", "")]
                        if found:  # either server works: one connected is enough
                            self.connector_status = "connected" if "connected" in found else found[0]
                    self._lines.append(ev)
                    if ev.get("type") == "result":
                        self._results += 1
                    self._cv.notify_all()
        with self._cv:
            self._results = 10**6  # process ended: release every waiter
            self._cv.notify_all()

    def _turn(self, text: str) -> TurnResult:
        """One user turn, recorded on the trace as `agent:atlassian` (tool spans when verbose)."""
        res, err = TurnResult(), ""
        try:
            res = self._turn_raw(text)
            return res
        except AtlassianError as e:
            err = str(e)
            raise
        finally:
            observe.llm_call(
                "atlassian", model=self.model, prompt=text, output=f"{len(res.calls)} connector call(s)",
                cost_usd=res.cost_usd, seconds=res.seconds, error=err,
                tool_calls=[{"name": c.name, "input": c.input, "output": c.text, "is_error": c.is_error}
                            for c in res.calls])

    def _turn_raw(self, text: str) -> TurnResult:
        if not self.proc or self.proc.poll() is not None:
            raise AtlassianError("the claude session is not running")
        with self._cv:
            start_idx, want = len(self._lines), self._results + 1
        t0 = time.monotonic()
        self.proc.stdin.write(json.dumps({"type": "user", "message": {"role": "user", "content": text}}) + "\n")
        self.proc.stdin.flush()
        with self._cv:
            if not self._cv.wait_for(lambda: self._results >= want, timeout=TURN_TIMEOUT_S):
                raise AtlassianError(f"the connector call did not finish within {TURN_TIMEOUT_S}s", retryable=True)
            events = self._lines[start_idx:]
        res = TurnResult(seconds=round(time.monotonic() - t0, 1))
        by_id: dict[str, ToolCall] = {}
        for ev in events:
            msg = ev.get("message")
            msg = msg if isinstance(msg, dict) else {}  # system events (e.g. permission_denied) carry a string
            for b in msg.get("content") or [] if isinstance(msg.get("content"), list) else []:
                name = str(b.get("name", ""))
                prefix = next((p for p in PREFIXES if name.startswith(p)), None)
                if b.get("type") == "tool_use" and prefix:
                    by_id[b["id"]] = ToolCall(name[len(prefix):], b.get("input") or {})
                    res.calls.append(by_id[b["id"]])
                elif b.get("type") == "tool_result" and b.get("tool_use_id") in by_id:
                    call = by_id[b["tool_use_id"]]
                    call.text = _result_text(b.get("content"))
                    call.is_error = bool(b.get("is_error"))
            if ev.get("type") == "result":
                res.cost_usd += float(ev.get("total_cost_usd") or 0)
                if ev.get("is_error"):
                    # claude itself refused the turn (usage limit, auth, overload): the connector was never asked
                    raise AtlassianError(f"headless claude refused the request: {ev.get('result') or ev.get('subtype')}"
                                         " — not a connector problem; rerun once that is resolved")
        return res

    # ---- public API ----------------------------------------------------------------------------
    def connect(self, site: str) -> TurnResult:
        """Warm-up turn, then resolve the cloudId. Raises when the connector is not authorised."""
        warm = self._turn("Reply with the single word: ok")
        waited = time.monotonic() + PENDING_WAIT_S
        while self.connector_status == "pending" and time.monotonic() < waited:
            time.sleep(PENDING_POLL_S)  # each turn's init reports the server's status as it is now
            more = self._turn("Reply with the single word: ok")
            warm.cost_usd += more.cost_usd
            warm.seconds += more.seconds
        if self.connector_status in ("needs-auth", "failed"):
            raise AtlassianError(
                f"no Atlassian MCP server is connected (status '{self.connector_status}'). Authenticate one "
                "once in Claude Code (run /mcp, pick 'atlassian' or 'claude.ai Atlassian'), then rerun scope.")
        res = self._turn("Call getAccessibleAtlassianResources once. Do not call anything else. Reply: done.")
        for c in res.calls:
            if c.name == "getAccessibleAtlassianResources" and not c.is_error:
                try:
                    sites = json.loads(c.text)
                except ValueError:
                    sites = []
                host = site.split("//")[-1].rstrip("/")
                for s in sites if isinstance(sites, list) else []:
                    if host in str(s.get("url", "")):
                        self.cloud_id = s.get("id")
                if not self.cloud_id and isinstance(sites, list) and sites:
                    self.cloud_id = sites[0].get("id")
        if not self.cloud_id:
            raise AtlassianError(
                "the Atlassian connector is not usable from headless claude (no getAccessibleAtlassianResources "
                f"result; connector status '{self.connector_status}'). Atlassian's server is often just slow to "
                "answer: rerun in a minute. If it keeps failing, re-authenticate it via /mcp and rerun.",
                retryable=True)
        res.cost_usd += warm.cost_usd
        res.seconds += warm.seconds
        return res

    def fetch(self, pages: list[str] = (), page_descendants: list[str] = (), jira: list[str] = ()) -> TurnResult:
        """One turn that makes every requested call (the model may batch them). Results are byte-exact."""
        steps = [f"getConfluencePage with cloudId {self.cloud_id}, pageId {p}, contentFormat markdown" for p in pages]
        steps += [f"getConfluencePageDescendants with cloudId {self.cloud_id}, pageId {p}" for p in page_descendants]
        steps += [f"getJiraIssue with cloudId {self.cloud_id}, issueIdOrKey {k}" for k in jira]
        if not steps:
            return TurnResult()
        prompt = ("Make exactly these tool calls (in parallel where you can), nothing else, and do not "
                  "summarise their output:\n- " + "\n- ".join(steps) + "\nThen reply: done.")
        return self._turn(prompt)


# ---- turning tool results into raw/ files ---------------------------------------------------------

def parse_page(call: ToolCall) -> dict | None:
    """getConfluencePage result -> {id, title, body, url, last_modified}. None when it is not a page."""
    try:
        data = json.loads(call.text)
    except ValueError:
        return None
    node = None
    if isinstance(data, dict):
        nodes = ((data.get("content") or {}).get("nodes") if isinstance(data.get("content"), dict) else None)
        node = nodes[0] if nodes else (data if "body" in data else None)
    if not isinstance(node, dict) or not isinstance(node.get("body"), str):
        return None
    return {"id": str(node.get("id") or call.input.get("pageId") or ""), "title": node.get("title") or "",
            "body": node["body"], "url": node.get("webUrl") or "", "last_modified": node.get("lastModified") or ""}


def parse_descendants(call: ToolCall) -> list[dict]:
    try:
        data = json.loads(call.text)
    except ValueError:
        return []
    rows = data.get("results") if isinstance(data, dict) else data
    return [{"id": str(r.get("id")), "title": r.get("title") or f"page {r.get('id')}", "depth": r.get("depth")}
            for r in rows or [] if isinstance(r, dict) and r.get("type", "page") == "page" and r.get("id")]


def provenance(source: str, title: str, fetched_at: str, extra: dict | None = None) -> str:
    lines = ["---", f"source_url: {source}", f"fetched_at: {fetched_at}", f"title: {title}"]
    lines += [f"{k}: {v}" for k, v in (extra or {}).items() if v]
    return "\n".join(lines + ["---", ""]) + "\n"


def linked_page_ids(body: str, exclude: set[str]) -> list[str]:
    """Confluence page ids linked from a page body (sibling references, not just descendants)."""
    out: list[str] = []
    for m in re.finditer(r"/wiki/[^\s)>\]]*?/pages/(\d+)", body):
        pid = m.group(1)
        if pid not in exclude and pid not in out:
            out.append(pid)
    return out


def jira_keys(body: str) -> list[str]:
    out: list[str] = []
    for k in JIRA_KEY_RE.findall(body):
        if k not in out:
            out.append(k)
    return out


def open_session(stack, log_path: Path, site: str, *, claude_bin: str = "claude", model: str = DEFAULT_MODEL,
                 cwd: Path | None = None, say=None, attempts: int = CONNECT_ATTEMPTS,
                 wait_s: float = RETRY_WAIT_S) -> tuple[ConnectorSession, float]:
    """A connected session, entered on `stack` (an ExitStack), and the cost of connecting. A slow Atlassian
    server gets a fresh connection, up to `attempts` in all; a missing sign-in or a refusal stops at once."""
    cost = 0.0
    for attempt in range(1, attempts + 1):
        session = ConnectorSession(log_path, claude_bin=claude_bin, model=model, cwd=cwd)
        session.__enter__()
        try:
            res = session.connect(site)
        except AtlassianError as e:
            session.__exit__(None, None, None)
            if not e.retryable:
                raise
            if attempt == attempts:
                raise AtlassianError(f"tried {attempts} fresh connections: {e}", retryable=True) from e
            if say:
                say(f"Atlassian did not answer in time (try {attempt} of {attempts}) — retrying with a fresh "
                    "connection …")
            time.sleep(wait_s)
            continue
        stack.callback(session.__exit__, None, None, None)
        return session, cost + res.cost_usd
    raise AssertionError("unreachable")
