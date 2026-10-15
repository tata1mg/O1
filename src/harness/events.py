"""Canonical trace events — the engine boundary.

Every engine adapter writes `logs/<label>.events.jsonl`: one JSON object per line, schema below. The
ledger (witness corpus), crash-recovery accounting, the live activity feed and observability read ONLY
this file. Engine-native traces (Claude's stream-json, Pi's --mode json) are kept next to it as raw
evidence but nothing downstream parses them (the Claude raw stream is still written for back-compat with
the existing ledger until ledger v2 lands).

Why a schema of our own instead of adopting one wire format: rfc_agent's `StreamResult` is Claude's
stream-json and is consumed by six modules; a second engine would have to forge Claude events. A small
neutral schema + one translator per engine is the cheaper, honest seam.

Design rules: flat, additive (readers ignore unknown keys), every event has `t` (type), `ts` (unix s),
`seq` (monotonic per file). Tool events carry a NORMALISED `kind` so the ledger can classify reads,
searches and writes without knowing engine tool names.
"""

from __future__ import annotations

import json
import time
from collections.abc import Iterator
from dataclasses import asdict, dataclass, field
from enum import StrEnum
from pathlib import Path

SCHEMA_VERSION = 1


class EventType(StrEnum):
    INIT = "init"  # engine/session started: engine, model, session_id, tools, cwd
    TEXT = "text"  # assistant prose (final or intermediate)
    THINKING = "thinking"  # deliberation block (may be summarised)
    TOOL_START = "tool_start"  # tool call issued: tool, kind, input, call_id
    TOOL_END = "tool_end"  # tool result: call_id, ok, output (clipped), paths_touched
    DENIAL = "denial"  # a tool call was refused by policy/permissions: call_id, tool, reason
    USER = "user"  # a user/operator turn injected mid-run (steering, kickoff)
    COMPACTION = "compaction"  # context was summarised away
    RESULT = "result"  # terminal: ok, cost_usd, usage, num_turns, duration_ms, session_id, error
    LOG = "log"  # engine/harness note (stderr tail, retries), never model content


class ToolKind(StrEnum):
    """Engine-neutral classification the ledger keys on. Adapters map native tool names here."""

    READ = "read"  # read a file (path in input.path)
    SEARCH = "search"  # grep/glob/rg (pattern in input.pattern, root in input.path)
    LIST = "list"  # directory listing
    WRITE = "write"  # create/overwrite a file
    EDIT = "edit"  # partial edit of a file
    SHELL = "shell"  # arbitrary command (input.command); adapters may also set input.readonly=True/False
    FETCH = "fetch"  # network read (url)
    MCP = "mcp"  # MCP tool call (input.server, input.tool)
    SUBAGENT = "subagent"  # delegated agent
    ASK = "ask"  # asked the operator
    OTHER = "other"


@dataclass
class Event:
    t: str
    seq: int = 0
    ts: float = field(default_factory=time.time)
    v: int = SCHEMA_VERSION
    # optional payload — flat, documented per type above
    engine: str | None = None
    model: str | None = None
    session_id: str | None = None
    call_id: str | None = None
    tool: str | None = None
    kind: str | None = None
    input: dict | None = None
    output: str | None = None
    ok: bool | None = None
    text: str | None = None
    reason: str | None = None
    cost_usd: float | None = None
    usage: dict | None = None
    num_turns: int | None = None
    duration_ms: int | None = None
    error: str | None = None
    paths: list[str] | None = None  # files touched (tool_end), for the witness corpus
    extra: dict | None = None  # engine-specific leftovers, never read by the kernel

    def to_json(self) -> str:
        return json.dumps({k: v for k, v in asdict(self).items() if v is not None}, ensure_ascii=False)


class EventWriter:
    """Append-only writer with a per-file monotonic `seq`. Safe for one writer per file; fan-out engines
    give every child its own label (hence its own file), exactly like the Claude stream files today."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._seq = 0
        self._fh = open(self.path, "a", encoding="utf-8")

    def emit(self, ev: Event) -> Event:
        self._seq += 1
        ev.seq = self._seq
        self._fh.write(ev.to_json() + "\n")
        self._fh.flush()
        return ev

    def close(self) -> None:
        self._fh.close()

    def __enter__(self) -> EventWriter:
        return self

    def __exit__(self, *exc) -> None:
        self.close()


def read_events(path: Path) -> Iterator[dict]:
    """Tolerant reader: skips blank/corrupt lines (a killed child can leave a torn last line)."""
    if not path.exists():
        return
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue


def events_path(run_dir: Path, label: str) -> Path:
    """logs/<label>.events.jsonl — the canonical trace for one engine invocation."""
    return run_dir / "logs" / f"{label}.events.jsonl"


def events_paths(run_dir: Path, stage_name: str) -> list[Path]:
    """Every canonical trace a stage produced (fan-out children use `<stage>.<suffix>` labels)."""
    logs = run_dir / "logs"
    return sorted(logs.glob(f"{stage_name}.events.jsonl")) + sorted(logs.glob(f"{stage_name}.*.events.jsonl"))


@dataclass
class TraceSummary:
    """What `harness.engines.EngineResult` derives from a canonical trace — engine-agnostic accounting."""

    final_text: str = ""
    tool_calls: list[dict] = field(default_factory=list)
    cost_usd: float | None = None
    usage: dict | None = None
    num_turns: int | None = None
    model: str | None = None
    session_id: str | None = None
    denials: int = 0  # UNIQUE denials, keyed on call_id — both signals fire for one refusal
    compactions: int = 0
    got_result: bool = False
    is_error: bool = False
    error_reason: str | None = None
    # result events past the first: a post-result bonus turn ran (a steer racing the terminal result).
    # Verdict fields ignore them; carried so a non-Claude engine can record the same `post_result_turn`
    # decision the Claude path does (StreamResult.extra_results is the counterpart).
    extra_results: int = 0
    extra_result_error: bool = False
    reads: list[str] = field(default_factory=list)  # paths read (witness corpus seed)
    searches: list[dict] = field(default_factory=list)
    writes: list[str] = field(default_factory=list)


def summarize(path: Path) -> TraceSummary:
    """Engine-agnostic accounting for one canonical trace.

    Two rules are inherited verbatim from `rfc_agent.streams.parse_stream`, and both were review
    findings there before they were findings here:

    DENIALS ARE ID-KEYED. One refusal fires TWO signals on CLI >= 2.1.224 — the first-class
    `system/permission_denied` event and the errored `tool_result` echo of the same call — so counting
    events double-counts every denial. `denials` is charted as a bad-run predictor.

    COST/USAGE/TURNS ARE LAST-WINS, VERDICT FIELDS ARE FIRST-WINS. A post-result bonus turn appends a
    second terminal event; its verdict must not rewrite the stage's outcome, but its spend still
    happened and must stay observable. Taking cost from the first result made a bonus turn's spend
    disappear — under-reporting spend is exactly the accounting error the 2026-08-19 audit was about.
    """
    s = TraceSummary()
    denied_ids: set[str] = set()
    for ev in read_events(path):
        t = ev.get("t")
        if t == EventType.INIT:
            s.model = ev.get("model") or s.model
            s.session_id = ev.get("session_id") or s.session_id
        elif t == EventType.TOOL_START:
            s.tool_calls.append({"name": ev.get("tool"), "kind": ev.get("kind"), "input": ev.get("input") or {}})
            kind, inp = ev.get("kind"), ev.get("input") or {}
            if kind == ToolKind.READ and inp.get("path"):
                s.reads.append(inp["path"])
            elif kind == ToolKind.SEARCH:
                s.searches.append(inp)
            elif kind in (ToolKind.WRITE, ToolKind.EDIT) and inp.get("path"):
                s.writes.append(inp["path"])
        elif t == EventType.TOOL_END:
            for p in ev.get("paths") or []:
                if p not in s.reads:
                    s.reads.append(p)
        elif t == EventType.DENIAL:
            # a synthetic id when the engine gave none, so two anonymous denials still count as two
            denied_ids.add(str(ev.get("call_id") or f"anon-{len(denied_ids)}"))
        elif t == EventType.COMPACTION:
            s.compactions += 1
        elif t == EventType.RESULT:
            if not s.got_result:
                s.got_result = True
                s.final_text = ev.get("text") or ""
                s.is_error = ev.get("ok") is False
                s.error_reason = ev.get("error")
            else:
                s.extra_results += 1
                if ev.get("ok") is False:
                    s.extra_result_error = True
            # LAST-wins: cumulative session totals, and a cut-off run still spent money
            if ev.get("cost_usd") is not None:
                s.cost_usd = ev.get("cost_usd")
            if ev.get("usage") is not None:
                s.usage = ev.get("usage")
            if ev.get("num_turns") is not None:
                s.num_turns = ev.get("num_turns")
            s.session_id = ev.get("session_id") or s.session_id
    s.denials = len(denied_ids)
    return s
