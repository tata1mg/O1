"""Translate Claude Code's `stream-json` into the canonical trace (harness.events). D3's one translator.

WHY a translation instead of adopting the wire format (D3): `rfc_agent.streams.StreamResult` IS Claude's
stream-json, and six modules consume it. Making Pi the second engine would mean Pi forging Claude events.
A ten-type neutral schema plus one translator per engine is the smaller, honest seam. The raw stream stays
exactly where it is (`logs/<label>.stream.jsonl`) as evidence; nothing downstream of this file parses it.

The mapping is one-way and lossless-where-it-matters: anything with no canonical home rides in `extra`,
which the kernel never reads. Every clip limit is a named constant — a trace is read by humans in a
terminal and by gates that only need the shape.

Stream-json shapes this handles (verified against src/rfc_agent/streams.py and the CLI's output):
  system/init                  -> init          (model, session_id, tools, cwd)
  system/compact_boundary      -> compaction
  system/permission_denied     -> denial        (tool_use_id)
  assistant.content[].text     -> text
  assistant.content[].thinking -> thinking
  assistant.content[].tool_use -> tool_start    (call_id = block id, kind = NORMALISED tool kind)
  user.content[].tool_result   -> tool_end, or denial when the result is a harness-shaped refusal
  user.content (plain text)    -> user          (--replay-user-messages echoes kickoff + steering)
  result                       -> result        (ok, cost_usd, usage, num_turns, duration_ms, session_id)
"""

from __future__ import annotations

import json
import time
from datetime import datetime
from pathlib import Path

from ..events import Event, EventType, EventWriter, ToolKind

# One assistant tool_result can be a 100KB file dump; the canonical trace is a TRACE, not a cache of
# tool output. Gates read shapes (which paths, ok/not), humans read the head. The raw stream keeps the
# full payload for anyone who needs it.
OUTPUT_CLIP = 2000
# Deliberation and prose blocks are kept whole: they are the only record of WHY a stage did what it did,
# and the digest that replaced a head+tail clip covered 4.6% of a 130KB stream (see streams.py).
TEXT_CLIP = 0  # 0 = no clipping

ENGINE_NAME = "claude-code"

# Claude tool name -> engine-neutral ToolKind. `mcp__<server>__<tool>` and the Agent/Task delegation
# tools are matched by prefix below, so this table stays the exact list the CLI ships.
TOOL_KINDS: dict[str, ToolKind] = {
    "Read": ToolKind.READ,
    "NotebookRead": ToolKind.READ,
    "Grep": ToolKind.SEARCH,
    "Glob": ToolKind.SEARCH,
    "LS": ToolKind.LIST,
    "Write": ToolKind.WRITE,
    "Edit": ToolKind.EDIT,
    "MultiEdit": ToolKind.EDIT,
    "NotebookEdit": ToolKind.EDIT,
    "Bash": ToolKind.SHELL,
    "BashOutput": ToolKind.SHELL,
    "KillShell": ToolKind.SHELL,
    "WebFetch": ToolKind.FETCH,
    "WebSearch": ToolKind.FETCH,
    "Agent": ToolKind.SUBAGENT,
    "Task": ToolKind.SUBAGENT,
    "AskUserQuestion": ToolKind.ASK,
}


def tool_kind(name: str) -> ToolKind:
    """Normalise a Claude tool name. Unknown names are OTHER, never a guess: the ledger would rather
    see `other` than classify a write as a read."""
    if not name:
        return ToolKind.OTHER
    if name in TOOL_KINDS:
        return TOOL_KINDS[name]
    if name.startswith("mcp__"):
        return ToolKind.MCP
    return ToolKind.OTHER


def _mcp_parts(name: str) -> dict:
    """`mcp__figma__get_screenshot` -> {"server": "figma", "tool": "get_screenshot"} (schema's MCP note)."""
    parts = name.split("__", 2)
    return {"server": parts[1], "tool": parts[2]} if len(parts) == 3 else {}


def _ts(ev: dict, fallback: float) -> float | None:
    """The event's own ISO `timestamp` as unix seconds, or None when it has none.

    Only `assistant` and `user` lines carry one (verified against a live CLI 2.1.263 stream,
    2026-09-08: 156 lines, timestamps on those two types only). `system` and `result` have none, so the
    caller supplies the fallback — see `_timestamps`.
    """
    raw = ev.get("timestamp")
    if isinstance(raw, str) and raw:
        try:
            return datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp()
        except ValueError:
            return fallback
    return None


def _first_timestamp(stdout: str) -> float | None:
    """The earliest real timestamp in the stream, used as the seed fallback.

    WHY (live smoke, 2026-09-08): `system/init` carries no timestamp, so stamping it with "now" put the
    session's FIRST event after its last one — anything ordering the trace by time saw the run start
    after it finished. Seeding from the first real stamp keeps the sequence monotone instead.
    """
    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(ev, dict):
            ts = _ts(ev, 0.0)
            if ts:
                return ts
    return None


def _blocks(ev: dict) -> list[dict]:
    msg = ev.get("message") if isinstance(ev.get("message"), dict) else {}
    raw = msg.get("content", [])
    if isinstance(raw, str):  # a string message body is legal in the stream (pinned by test_streams)
        return [{"type": "text", "text": raw}]
    return [b for b in raw if isinstance(b, dict)] if isinstance(raw, list) else []


def _is_denial_result(block: dict) -> bool:
    """Harness-shaped refusal echoed as an errored tool_result.

    The precision rules are streams.py's, verbatim and for its reason: bare "denied"/"permission" counted
    filesystem EACCES and content mentions as harness denials. Only the write-guard naming itself and the
    CLI's auto-deny phrasing count. Head-bounded — denial messages are short, error payloads are not.
    """
    if not block.get("is_error"):
        return False
    txt = str(block.get("content"))[:400].lower()
    return "guard_writes" in txt or "auto-den" in txt or ("permission" in txt and "granted" in txt)


def _paths_touched(inp: dict) -> list[str]:
    """Files a tool call named — the witness corpus seed. Only explicit path fields, never a guess."""
    out: list[str] = []

    def add(val) -> None:
        # deduped, order preserved: MultiEdit repeats one file per edit, and the normalised `path` key
        # this module adds would otherwise double every entry
        if isinstance(val, str) and val and val not in out:
            out.append(val)

    for key in ("file_path", "notebook_path", "path"):
        add(inp.get(key))
    edits = inp.get("edits")
    if isinstance(edits, list):
        for e in edits:
            if isinstance(e, dict):
                add(e.get("file_path"))
    return out


def translate_events(stdout: str, *, engine: str = ENGINE_NAME) -> list[Event]:
    """stream-json text -> canonical Events, in order. Pure: no I/O, so it is trivially testable.

    Torn or non-JSON lines are skipped exactly as `parse_stream` skips them: a killed child leaves a
    partial last line, and a translator that raised there would turn a recoverable stage into a crash.
    """
    events: list[Event] = []
    # Timestamp policy: an event's own stamp when it has one, otherwise the last real stamp seen. The
    # seed is the stream's FIRST real stamp, because `system/init` carries none and stamping it with
    # "now" put the session's first event after its last (live smoke, 2026-09-08). Wall-clock is the
    # last resort, for a stream with no stamps at all.
    #
    # `ts` is NOT guaranteed monotone and is deliberately not clamped: the CLI's own stamps can arrive
    # a few ms out of order (observed live: a replayed user turn stamped 3ms before the tool_result
    # emitted ahead of it). `seq` is the ordering authority — the schema says so — and altering a real
    # timestamp to enforce an ordering `seq` already carries would be a lie for no gain.
    now = time.time()
    carry = _first_timestamp(stdout) or now
    calls: dict[str, dict] = {}  # tool_use id -> {"tool", "kind", "input"} for pairing tool_end
    saw_result = False
    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(ev, dict):
            continue
        typ = ev.get("type")
        own = _ts(ev, None)
        if own is not None:
            carry = max(carry, own)
        ts = own if own is not None else carry
        if typ == "stream_event":
            # partial-message deltas exist for the live feed's heartbeat only; the complete events that
            # follow carry the same content. Translating them would double every tool call.
            continue
        if typ == "system":
            sub = str(ev.get("subtype") or "")
            if "compact" in sub:
                events.append(Event(t=EventType.COMPACTION, ts=ts, engine=engine,
                                    reason=sub, extra={"trigger": ev.get("trigger")} if ev.get("trigger") else None))
            elif sub == "permission_denied":
                events.append(Event(t=EventType.DENIAL, ts=ts, engine=engine,
                                    call_id=str(ev.get("tool_use_id") or "") or None,
                                    tool=str(ev.get("tool_name") or "") or None,
                                    reason=str(ev.get("reason") or ev.get("message") or "permission_denied")))
            elif sub == "init":
                events.append(Event(
                    t=EventType.INIT, ts=ts, engine=engine,
                    model=str(ev["model"]) if ev.get("model") else None,
                    session_id=str(ev["session_id"]) if ev.get("session_id") else None,
                    input={"tools": ev.get("tools") or [], "cwd": ev.get("cwd") or "",
                           "mcp_servers": ev.get("mcp_servers") or []},
                ))
            else:
                # an unrecognised system subtype is still evidence; the kernel ignores unknown keys
                events.append(Event(t=EventType.LOG, ts=ts, engine=engine,
                                    reason=f"unhandled system subtype {sub!r}"))
            continue
        if typ == "assistant":
            for block in _blocks(ev):
                bt = block.get("type")
                if bt == "text" and str(block.get("text") or "").strip():
                    events.append(Event(t=EventType.TEXT, ts=ts, engine=engine, text=block["text"]))
                elif bt == "thinking" and str(block.get("thinking") or "").strip():
                    events.append(Event(t=EventType.THINKING, ts=ts, engine=engine, text=block["thinking"]))
                elif bt == "tool_use":
                    name = str(block.get("name") or "")
                    kind = tool_kind(name)
                    inp = block.get("input") if isinstance(block.get("input"), dict) else {}
                    payload = dict(inp)
                    # the schema names `input.path` as the canonical file field (see harness.events
                    # ToolKind); Claude spells it file_path/notebook_path per tool. Normalise while
                    # KEEPING the native keys, so `summarize` classifies reads/writes without a tool
                    # table and an engine-aware reader still sees exactly what was sent.
                    touched = _paths_touched(inp)
                    if touched and not payload.get("path") and kind in (
                            ToolKind.READ, ToolKind.WRITE, ToolKind.EDIT, ToolKind.LIST):
                        payload["path"] = touched[0]
                    if kind == ToolKind.MCP:
                        payload.update(_mcp_parts(name))
                    call_id = str(block.get("id") or "") or None
                    calls[call_id or ""] = {"tool": name, "kind": kind, "input": payload}
                    events.append(Event(t=EventType.TOOL_START, ts=ts, engine=engine, tool=name,
                                        kind=kind, input=payload, call_id=call_id))
            continue
        if typ == "user":
            blocks = _blocks(ev)
            results = [b for b in blocks if b.get("type") == "tool_result"]
            for block in results:
                call_id = str(block.get("tool_use_id") or "") or None
                origin = calls.get(call_id or "", {})
                if _is_denial_result(block):
                    events.append(Event(t=EventType.DENIAL, ts=ts, engine=engine, call_id=call_id,
                                        tool=origin.get("tool"),
                                        reason=str(block.get("content"))[:OUTPUT_CLIP]))
                    continue
                content = block.get("content")
                text = content if isinstance(content, str) else json.dumps(content, ensure_ascii=False)
                events.append(Event(
                    t=EventType.TOOL_END, ts=ts, engine=engine, call_id=call_id,
                    tool=origin.get("tool"), kind=origin.get("kind"),
                    ok=not block.get("is_error"), output=text[:OUTPUT_CLIP],
                    paths=_paths_touched(origin.get("input") or {}) or None,
                ))
            if not results:
                # --replay-user-messages echoes the kickoff prompt and every mid-run steering turn
                texts = [str(b.get("text") or "") for b in blocks if b.get("type") == "text"]
                joined = "\n".join(t for t in texts if t.strip())
                if joined:
                    events.append(Event(t=EventType.USER, ts=ts, engine=engine, text=joined))
            continue
        if typ == "result":
            is_error = bool(ev.get("is_error"))
            ev_out = Event(
                t=EventType.RESULT, ts=ts, engine=engine, ok=not is_error,
                text="" if is_error else (ev.get("result") if isinstance(ev.get("result"), str) else ""),
                cost_usd=float(ev["total_cost_usd"]) if isinstance(ev.get("total_cost_usd"), (int, float)) else None,
                usage=ev["usage"] if isinstance(ev.get("usage"), dict) else None,
                num_turns=ev["num_turns"] if isinstance(ev.get("num_turns"), int) else None,
                duration_ms=ev["duration_ms"] if isinstance(ev.get("duration_ms"), int) else None,
                session_id=str(ev["session_id"]) if ev.get("session_id") else None,
                error=str(ev.get("terminal_reason") or ev.get("subtype") or "unknown") if is_error else None,
            )
            if saw_result:
                # a post-result bonus turn (a steer racing the terminal result). First-result-wins is
                # the verdict rule everywhere else, so mark this one rather than let a reader average
                # two terminal results.
                ev_out.extra = {"extra_result": True}
            saw_result = True
            events.append(ev_out)
            continue
        # unknown event type: keep it as a log line rather than drop evidence silently
        events.append(Event(t=EventType.LOG, ts=ts, engine=engine, reason=f"unhandled stream event {typ!r}",
                            extra={"subtype": ev.get("subtype")} if ev.get("subtype") else None))
    return events


def trace_label(raw_stream_name: str) -> str:
    """`logs/<raw stream file name>` -> the canonical trace LABEL for it.

    `generate.stream.jsonl`   -> `generate`            (the current attempt)
    `generate.stream.3.jsonl` -> `generate.attempt3`   (a rotation kept as witness evidence)

    Both shapes are picked up by `harness.events.events_paths(run_dir, "generate")`, whose glob is
    `<stage>.events.jsonl` plus `<stage>.*.events.jsonl` — so a revision's whole corpus stays visible
    to the witness gates instead of only its last attempt.
    """
    stem = raw_stream_name.removesuffix(".jsonl")
    base, sep, rest = stem.partition(".stream")
    if not sep:
        return stem
    suffix = rest.lstrip(".")
    return f"{base}.attempt{suffix}" if suffix else base


def translate_file(stream_path: Path, trace_path: Path, *, engine: str = ENGINE_NAME) -> Path:
    """Read a raw stream file and write the canonical trace beside it. Returns `trace_path`.

    A missing or unreadable stream yields an EMPTY trace file rather than an exception: the ledger and
    the trace-backed witness gates degrade to a loud WARN on an absent trace, and a crashed child must
    not also crash its own accounting.
    """
    try:
        text = stream_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        text = ""
    trace_path.parent.mkdir(parents=True, exist_ok=True)
    # truncate: a re-translated attempt must not append to the previous attempt's trace
    trace_path.write_text("", encoding="utf-8")
    with EventWriter(trace_path) as w:
        for ev in translate_events(text, engine=engine):
            w.emit(ev)
    return trace_path
