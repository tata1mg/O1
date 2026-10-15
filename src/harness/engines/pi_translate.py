"""Pi `--mode json` events -> canonical `harness.events`.

This is the whole engine boundary for Pi (D3): downstream (ledger, witness gates, live feed, obs) reads
`logs/<label>.events.jsonl` and never Pi's own stream. Keeping the translation in its own module means it
is testable against recorded fixtures with no subprocess at all.

What the Pi stream looks like at 0.85.1 (research report 04, §1.5), one JSON object per line on stdout:

    {"type":"session","version":3,"id":"<uuid>","cwd":"..."}      <- ALWAYS line 1
    {"type":"agent_start"} {"type":"turn_start"}
    {"type":"message_start","message":{...}}
    {"type":"message_update","usage":{...},"assistantMessageEvent":{...}}   <- delta-only, IGNORED
    {"type":"message_end","message":{...}}                                  <- authoritative message
    {"type":"tool_execution_start","toolCallId":"..","toolName":"read","args":{...}}
    {"type":"tool_execution_end","toolCallId":"..","toolName":"read","result":{...},"isError":false}
    {"type":"turn_end",...} {"type":"agent_end","messages":[...],"willRetry":false}
    {"type":"agent_settled"}                                                <- the TRUE terminal event

Three properties of that stream drive the design here:

* `message_update` is deliberately delta-only on the wire (no cumulative `message`, no `partial`), and its
  `usage` "may remain zero when a provider only reports usage at completion". So it is ignored entirely;
  cost and text come from `message_end`.
* `agent_end` carries `willRetry` and can fire BEFORE a retry. `agent_settled` is the terminal signal
  ("no retry/compaction/follow-up left"). A translator that treats the first `agent_end` as the end
  truncates runs.
* `pi --mode json` exits 0 even when the agent errored or was aborted (its exit-code branch only exists
  for `--mode text`). Success is therefore derived HERE, from the stream, never from the exit code.

Denials arrive out of band, from the guard extension's JSONL side channel — see pi_ext/README.md.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ..events import Event, EventType, EventWriter, ToolKind

# Pi built-in tool names (0.85.1: read, bash, powershell, edit, write, grep, find, ls) -> the neutral
# classification the ledger keys on. Unknown names (extension/custom tools) fall through to OTHER rather
# than guessing, so a mis-mapped tool is visible in the trace instead of silently counted as a read.
TOOL_KINDS: dict[str, ToolKind] = {
    "read": ToolKind.READ,
    "grep": ToolKind.SEARCH,
    "find": ToolKind.SEARCH,
    "ls": ToolKind.LIST,
    "write": ToolKind.WRITE,
    "edit": ToolKind.EDIT,
    "multiedit": ToolKind.EDIT,
    "bash": ToolKind.SHELL,
    "powershell": ToolKind.SHELL,
}

# Tool output is evidence, not content: the ledger wants to know a call succeeded and what it touched, not
# to re-store a 200 KB file. Same clip the Claude adapter applies.
OUTPUT_CLIP = 2000

# Pi stream event types this translator deliberately drops. Listed (rather than silently defaulted) so a
# new Pi event type shows up as an unknown in `Translator.unknown_types` instead of vanishing.
IGNORED = frozenset(
    {
        "agent_start",
        "turn_end",
        "message_start",
        "message_update",  # delta-only on the wire, and its usage may be zero — see the module docstring
        "tool_execution_update",
        "bash_execution_update",
        "queue_update",
        "entry_appended",
        "session_info_changed",
        "thinking_level_changed",
        "summarization_retry_attempt_start",
        "summarization_retry_finished",
    }
)

# stopReason values that mean the turn went wrong. "pending" should never be persisted (Pi replaces it
# before persistence), so seeing it is corruption; "length" means the model was cut off mid-answer.
BAD_STOP_REASONS = frozenset({"error", "aborted", "length", "pending"})

# The session-format version this translator was written against (pi 0.85.1: CURRENT_SESSION_VERSION = 3).
# v1 was linear, v2 introduced the id/parentId tree, v3 renamed the `hookMessage` role to `custom`.
SESSION_VERSION = 3


def tool_kind(name: str) -> ToolKind:
    return TOOL_KINDS.get(str(name or "").lower(), ToolKind.OTHER)


def normalise_input(tool: str, args: Any) -> dict:
    """Pi tool args -> the flat, engine-neutral shape `harness.events` documents.

    `harness.events.summarize` seeds the witness corpus from `input.path` / `input.pattern` /
    `input.command`, so those keys have to be where Pi's `args.path` / `args.pattern` / `args.command`
    land. Anything else is kept verbatim under `extra_args` so nothing is lost.
    """
    if not isinstance(args, dict):
        return {"raw": _clip(args, 500)}
    t = str(tool or "").lower()
    out: dict = {}
    if t in ("bash", "powershell"):
        out["command"] = str(args.get("command") or "")
        if args.get("timeout") is not None:
            out["timeout"] = args["timeout"]
        return out
    if t == "grep":
        out["pattern"] = str(args.get("pattern") or "")
        if args.get("path"):
            out["path"] = str(args["path"])
        return out
    if t in ("read", "write", "edit", "multiedit", "ls", "find"):
        if args.get("path") or args.get("file_path"):
            out["path"] = str(args.get("path") or args.get("file_path"))
        if t == "find" and args.get("pattern"):
            out["pattern"] = str(args["pattern"])
        for k in ("offset", "limit"):
            if args.get(k) is not None:
                out[k] = args[k]
        return out
    return {k: _clip(v, 500) for k, v in args.items()}


def result_text(result: Any) -> str:
    """Flatten a Pi tool result into text. `result` is tool-specific: a string, or `{content: [...]}`
    whose blocks are `{type:"text", text}` / `{type:"image"}` (ToolResultMessage.content in the session
    format), or an arbitrary object."""
    if result is None:
        return ""
    if isinstance(result, str):
        return result
    if isinstance(result, dict):
        content = result.get("content")
        if isinstance(content, list):
            parts = [str(b.get("text") or "") for b in content if isinstance(b, dict) and b.get("type") == "text"]
            joined = "\n".join(p for p in parts if p)
            if joined:
                return joined
        for key in ("output", "text", "message"):
            if isinstance(result.get(key), str):
                return result[key]
    return json.dumps(result, ensure_ascii=False, default=str)


def message_cost(message: Any) -> float:
    """`Usage.cost.total` for one message, or 0.0. Pi computes a per-message, per-category dollar cost
    (`{input, output, cacheRead, cacheWrite, total}`); `--mode json` emits no terminal totals record, so
    summing these is the only in-stream source (§8.2 of the research report)."""
    if not isinstance(message, dict):
        return 0.0
    usage = message.get("usage")
    if not isinstance(usage, dict):
        return 0.0
    cost = usage.get("cost")
    if isinstance(cost, dict) and isinstance(cost.get("total"), (int, float)):
        return float(cost["total"])
    return 0.0


def _clip(value: Any, n: int) -> Any:
    s = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
    return s if len(s) <= n else s[:n] + f"… <clipped {len(s) - n} chars>"


COST_CATEGORIES: tuple[str, ...] = ("input", "output", "cacheRead", "cacheWrite", "cacheWrite1h")


def message_cost_breakdown(message: Any) -> dict | None:
    """Pi's per-category dollar cost for one message, or None when the message carried no cost at all.

    Richer than Claude's single total: `{input, output, cacheRead, cacheWrite, total}` per message, so a
    stage's cache-read versus cache-write spend is attributable without any extra bookkeeping.
    """
    if not isinstance(message, dict):
        return None
    usage = message.get("usage")
    if not isinstance(usage, dict):
        return None
    cost = usage.get("cost")
    if not isinstance(cost, dict):
        return None
    out = {k: float(cost[k]) for k in COST_CATEGORIES if isinstance(cost.get(k), (int, float))}
    if isinstance(cost.get("total"), (int, float)):
        out["total"] = float(cost["total"])
    elif out:
        out["total"] = sum(out.values())  # a provider that reported categories but no total
    return out or None


def add_cost(into: dict, breakdown: dict | None) -> dict:
    """Accumulate a per-category breakdown in place. Kept separate so the stream and the session file
    build the same shape."""
    for k, v in (breakdown or {}).items():
        into[k] = round(into.get(k, 0.0) + float(v), 10)
    return into


def session_usage_cost(session_file: Path) -> dict | None:
    """Per-category cost summed over a persisted session JSONL — the authoritative per-stage cost.

    Preferred over the stream sum because it survives a truncated stdout pipe and includes entries the
    stream does not replay (compaction / branch-summary generation is real spend attributable to the
    stage). Returns None when the file is absent or NO message carried a cost, so the caller reports an
    honest "unknown" rather than a confident $0 — a zero is a measurement, an absence is not.
    """
    if not session_file.exists():
        return None
    total: dict = {}
    seen = False
    with open(session_file, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue  # a killed child can leave a torn last line
            for msg in _messages_in(obj):
                b = message_cost_breakdown(msg)
                if b is not None:
                    seen = True
                    add_cost(total, b)
    return total if seen else None


def session_cost(session_file: Path) -> float | None:
    """`session_usage_cost`'s total, or None when no message carried a cost."""
    b = session_usage_cost(session_file)
    return None if b is None else b.get("total", 0.0)


def _messages_in(entry: dict) -> list[dict]:
    """Session entries wrap the message differently by type (`{type:"message", message:{...}}` for
    assistant/toolResult rows) — accept both the wrapper and a bare message."""
    if not isinstance(entry, dict):
        return []
    msg = entry.get("message")
    if isinstance(msg, dict):
        return [msg]
    if entry.get("role") in ("assistant", "toolResult"):
        return [entry]
    return []


class Translator:
    """Streams raw Pi events into canonical events and accumulates the terminal verdict.

    Usage: `feed()` every raw stream line as it arrives, `drain_denials()` whenever convenient (it is
    cheap and idempotent), then `finish()` exactly once after the child exits — `finish` is what writes
    the canonical RESULT, because the authoritative cost is only readable from the session file after the
    process is gone.
    """

    def __init__(
        self,
        writer: EventWriter,
        *,
        engine: str = "pi",
        model: str | None = None,
        session_id: str | None = None,
        denials_path: Path | None = None,
        prompt: str = "",
    ) -> None:
        self.w = writer
        self.engine = engine
        self.model = model
        self.session_id = session_id
        self.denials_path = denials_path
        self._denial_pos = 0
        self._init_written = False
        self._prompt = prompt
        # terminal state, all derived from the stream (never from the exit code)
        self.settled = False
        self.errors: list[str] = []
        self.num_turns = 0
        self.stream_cost = 0.0
        # None until some message actually reported a cost. `--mode json` emits no terminal totals record,
        # and several providers report usage only at completion, so "no cost seen" and "cost was zero" are
        # genuinely different facts — a 0.0 would be a measurement we never made.
        self.stream_cost_breakdown: dict | None = None
        self.final_text = ""
        self.compactions = 0
        self.denials = 0
        self.unknown_types: set[str] = set()
        self.usage: dict | None = None

    # ---- ingest -------------------------------------------------------------------------------------

    def feed(self, raw: dict) -> None:
        t = str(raw.get("type") or "")
        if t == "session":
            self.session_id = raw.get("id") or self.session_id
            version = raw.get("version")
            self._write_init(cwd=raw.get("cwd"), session_version=version)
            if version != SESSION_VERSION:
                # A bump means the persisted session shape changed under us, and the session file is where
                # cost comes from. Loud, but a LOG rather than a failure: the parse may well still be
                # correct, and hard-failing every stage on a cosmetic bump costs more than it saves.
                self.note(f"pi session format version is {version!r}, not the researched "
                          f"{SESSION_VERSION} — re-read pi's session-format docs before trusting the "
                          f"cost sum or the session file layout")
            return
        if not self._init_written:
            # a stream that starts without its header is a Pi-version surprise, not a reason to drop events
            self._write_init()
        if t == "turn_start":
            self.num_turns += 1
        elif t == "message_end":
            self._message_end(raw.get("message"))
        elif t == "tool_execution_start":
            self._tool_start(raw)
        elif t == "tool_execution_end":
            self._tool_end(raw)
        elif t == "compaction_start":
            self.compactions += 1
            self.w.emit(
                Event(
                    t=EventType.COMPACTION,
                    reason=str(raw.get("reason") or ""),
                    text="context was summarised away — evidence may be missing from the model's view",
                )
            )
        elif t == "compaction_end":
            if raw.get("aborted") or raw.get("errorMessage"):
                self._fail(f"compaction failed/aborted: {raw.get('errorMessage') or 'aborted'}")
        elif t == "auto_retry_start":
            self._log(
                f"provider retry {raw.get('attempt')}/{raw.get('maxAttempts')} in "
                f"{raw.get('delayMs')}ms: {_clip(raw.get('errorMessage') or '', 300)}"
            )
        elif t == "auto_retry_end":
            if raw.get("success") is False:
                self._fail(f"provider retries exhausted: {_clip(raw.get('finalError') or '', 300)}")
        elif t == "summarization_retry_scheduled":
            self._log(f"summarization retry scheduled (attempt {raw.get('attempt')})")
        elif t == "agent_end":
            if raw.get("willRetry"):
                self._log("agent_end with willRetry=true — not terminal, waiting for agent_settled")
        elif t == "agent_settled":
            self.settled = True
        elif t == "harness_denial":
            # Kept for completeness, not because the guard can reach it: measured against 0.85.1, an
            # extension's console.log in --mode json goes to STDERR, so a denial never arrives in the
            # stream today. This branch is what a future stream-native channel would land in.
            self._denial(raw)
        elif t not in IGNORED:
            self.unknown_types.add(t)
            self._log(f"unknown pi event type {t!r} (ignored)")

    def drain_denials(self) -> int:
        """Read new records from the guard's side channel and emit DENIAL events. Returns how many."""
        if not self.denials_path or not self.denials_path.exists():
            return 0
        n = 0
        # BINARY, deliberately: the cursor is a byte offset, and a text handle applies universal-newline
        # translation, so a record ending "\r\n" reads back one byte shorter than it is on disk. The
        # cursor then drifts and later denials are re-emitted or sliced mid-record.
        with open(self.denials_path, "rb") as fh:
            fh.seek(self._denial_pos)
            for raw in fh:
                if not raw.endswith(b"\n"):  # partial write — pick it up next drain
                    break
                self._denial_pos += len(raw)
                text = raw.decode("utf-8", errors="replace").strip()
                if not text:
                    continue
                try:
                    self._denial(json.loads(text))
                    n += 1
                except json.JSONDecodeError:
                    continue
        return n

    # ---- terminal -----------------------------------------------------------------------------------

    def finish(
        self,
        *,
        exit_code: int | None = None,
        timed_out: bool = False,
        cost: dict | None = None,
        extra_error: str | None = None,
        stderr_tail: str = "",
    ) -> Event:
        """Emit the canonical RESULT.

        `cost` is the session file's per-category breakdown when the caller could read it; the in-stream
        sum is the fallback. When NEITHER source reported a cost the event's `cost_usd` is **None**, not
        0.0: pi emits no terminal totals record, so an unmeasured run must not be indistinguishable from
        a free one. The per-category breakdown rides in `extra.cost` for whoever wants the cache split.
        """
        self.drain_denials()
        if extra_error:
            self._fail(extra_error)
        if timed_out:
            self._fail("timed out — process group killed")
        if not self.settled and not timed_out:
            # the honest reading of a stream with no agent_settled: we do not know that the agent finished
            self._fail("stream ended without agent_settled (truncated, crashed, or killed)")
        if exit_code not in (0, None) and not timed_out:
            self._fail(f"pi exited {exit_code}")
        if stderr_tail:
            self._log(f"stderr tail: {_clip(stderr_tail, 2000)}")
        ok = not self.errors
        breakdown = cost if cost is not None else self.stream_cost_breakdown
        source = "session_file" if cost is not None else ("event_stream" if breakdown is not None else "none")
        extra: dict = {"cost_source": source}
        if breakdown is not None:
            extra["cost"] = {k: round(v, 8) for k, v in breakdown.items()}
        return self.w.emit(
            Event(
                t=EventType.RESULT,
                ok=ok,
                text=self.final_text,
                cost_usd=round(breakdown["total"], 8) if breakdown and "total" in breakdown else None,
                usage=self.usage,
                num_turns=self.num_turns or None,
                session_id=self.session_id,
                model=self.model,
                error="; ".join(self.errors) or None,
                extra=extra,
            )
        )

    # ---- internals ----------------------------------------------------------------------------------

    def _write_init(self, cwd: str | None = None, session_version: Any = None) -> None:
        self._init_written = True
        self.w.emit(
            Event(
                t=EventType.INIT,
                engine=self.engine,
                model=self.model,
                session_id=self.session_id,
                input={"cwd": cwd} if cwd else None,
                extra={"session_version": session_version} if session_version is not None else None,
            )
        )
        if self._prompt:
            self.w.emit(Event(t=EventType.USER, text=self._prompt))

    def _message_end(self, message: Any) -> None:
        if not isinstance(message, dict):
            return
        self.model = message.get("model") or message.get("responseModel") or self.model
        breakdown = message_cost_breakdown(message)
        if breakdown is not None:
            self.stream_cost_breakdown = add_cost(self.stream_cost_breakdown or {}, breakdown)
            self.stream_cost = self.stream_cost_breakdown.get("total", 0.0)
        usage = message.get("usage")
        if isinstance(usage, dict):
            self.usage = {k: v for k, v in usage.items() if k != "cost"} or None
        stop = str(message.get("stopReason") or "")
        if stop in BAD_STOP_REASONS:
            detail = _clip(message.get("errorMessage"), 300) if message.get("errorMessage") else ""
            self._fail(f"assistant stopReason={stop}" + (f": {detail}" if detail else ""))
        for block in message.get("content") or []:
            if not isinstance(block, dict):
                continue
            bt = str(block.get("type") or "")
            if bt == "text":
                text = str(block.get("text") or "")
                if text.strip():
                    self.w.emit(Event(t=EventType.TEXT, text=text))
                    self.final_text = text
            elif bt == "thinking":
                # field name is `thinking` in the session format; accept `text` too (0.85.1 ships both
                # shapes across providers and the report did not pin which the wire uses)
                text = str(block.get("thinking") or block.get("text") or "")
                if text.strip():
                    self.w.emit(Event(t=EventType.THINKING, text=text))

    def _tool_start(self, raw: dict) -> None:
        tool = str(raw.get("toolName") or "")
        self.w.emit(
            Event(
                t=EventType.TOOL_START,
                call_id=raw.get("toolCallId"),
                tool=tool,
                kind=str(tool_kind(tool)),
                input=normalise_input(tool, raw.get("args")),
            )
        )

    def _tool_end(self, raw: dict) -> None:
        tool = str(raw.get("toolName") or "")
        text = result_text(raw.get("result"))
        kind = tool_kind(tool)
        paths = None
        details = raw.get("result").get("details") if isinstance(raw.get("result"), dict) else None
        # `paths` on a tool_end feeds `TraceSummary.reads` (the witness corpus seed), so it is only set for
        # tools that READ. A write's target is already recorded from its tool_start input.path; repeating it
        # here would file the write as evidence the agent had read the file.
        if isinstance(details, dict) and kind not in (ToolKind.WRITE, ToolKind.EDIT):
            found = [str(p) for p in (details.get("paths") or []) if p]
            paths = found or None
        self.w.emit(
            Event(
                t=EventType.TOOL_END,
                call_id=raw.get("toolCallId"),
                tool=tool,
                kind=str(kind),
                ok=not bool(raw.get("isError")),
                output=_clip(text, OUTPUT_CLIP) if text else None,
                paths=paths,
            )
        )

    def _denial(self, rec: dict) -> None:
        self.denials += 1
        self.w.emit(
            Event(
                t=EventType.DENIAL,
                call_id=rec.get("toolCallId"),
                tool=rec.get("tool"),
                reason=str(rec.get("reason") or "blocked by harness policy"),
            )
        )

    def note(self, text: str) -> None:
        """Record a harness-side note in the trace (never model content). Public because the adapter
        writes its own preflight notes — every knowing divergence from the Claude adapter is logged."""
        self.w.emit(Event(t=EventType.LOG, text=text))

    def _log(self, text: str) -> None:
        self.note(text)

    def _fail(self, reason: str) -> None:
        if reason not in self.errors:
            self.errors.append(reason)
        self._log(f"FAILURE SIGNAL: {reason}")
