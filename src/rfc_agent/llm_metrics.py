"""Opt-in, content-free metrics for visible assistant responses (not hidden provider requests).

Claude's message_start arrives AFTER response headers; Pi's message_start describes the assistant
lifecycle. Neither is a portable HTTP request-start clock. Compare observed_turn_s, keeping its
boundary explicit; do not advertise event_span_s/time_to_first_delta_s as provider latency/TTFT.
"""
from __future__ import annotations

import contextlib
import json
import time
from pathlib import Path

from .observe import _as_bool


def enabled(cfg) -> bool:
    return _as_bool(cfg.get("LANGFUSE.PER_CALL") if cfg is not None else None) is True


class CallRecorder:
    def __init__(self, stream: Path, *, engine: str, model: str, label: str, stage: str,
                 step: str = "run", trace=None, clock=None, requested_model: str | None = None):
        self.path = stream.with_name(stream.name.replace(".stream.jsonl", ".llm-calls.jsonl"))
        self.summary_path = self.path.with_name(self.path.name.replace(".llm-calls.jsonl", ".llm-metrics.json"))
        self.identity = {"engine": engine, "model": model, "requested_model": requested_model or model,
                         "label": label, "stage": stage, "step": step}
        self.trace = trace
        self.clock = clock or time.monotonic
        self.started = self.clock()
        self.wall_started = time.time()
        self.boundary = self.started
        self.boundary_kind = "process_start_including_startup"
        self.previous_end = None
        self.current: dict | None = None
        self.seen: set[str] = set()
        self.count = 0
        self.errors = 0
        self.first_event = None
        self.first_assistant = None
        self.settled = None
        self.retries = 0
        self.compactions = 0
        self.event_seconds = 0.0
        self.finished = False
        # The stream is replaced on a rerun; its metrics must describe the same invocation, even with no calls.
        for path in (self.path, self.summary_path):
            try:
                path.unlink(missing_ok=True)
            except OSError:
                self.errors += 1

    def feed_json(self, line: str) -> None:
        with contextlib.suppress(ValueError):
            self.feed(json.loads(line))

    def feed(self, ev: dict) -> None:
        # Observability must never fail a run, including on a future/partial CLI event shape.
        try:
            if isinstance(ev, dict):
                self._feed(ev, self.clock())
        except Exception:  # noqa: BLE001
            self.errors += 1

    def _begin(self, msg: dict, now: float, *, streamed: bool = False) -> None:
        mid = str(msg.get("id") or "")
        if mid and mid in self.seen:
            return
        if self.current and mid and mid == self.current["message_id"]:
            self.current["streamed"] |= streamed
            self._merge(msg)
            return
        self._commit(now)
        if self.first_assistant is None:
            self.first_assistant = now
        self.current = {"message_id": mid or None, "model": msg.get("model") or self.identity["model"],
                        "provider": msg.get("provider"), "start": now, "end": None, "first_delta": None,
                        "boundary": self.boundary, "boundary_kind": self.boundary_kind, "usage": {},
                        "previous_end": self.previous_end, "next_tool_boundary": None,
                        "stop_reason": None, "complete": False, "streamed": streamed}
        self._merge(msg)
        if self.trace is not None:
            self.trace.call_started({**self.identity, "call_index": self.count + 1,
                                     "message_id": mid or None,
                                     "timing_scope": ("response_stream_only" if self.identity["engine"] == "claude-code"
                                                      else "assistant_message_lifecycle")})

    def _merge(self, msg: dict) -> None:
        if not self.current:
            return
        for key in ("model", "provider"):
            if msg.get(key):
                self.current[key] = msg[key]
        if isinstance(msg.get("usage"), dict):
            usage = dict(msg["usage"])
            output, previous = usage.get("output_tokens"), self.current["usage"].get("output_tokens")
            if isinstance(output, int) and isinstance(previous, int):
                # Assistant blocks can repeat preliminary usage after a final stream usage snapshot.
                usage["output_tokens"] = max(output, previous)
            self.current["usage"].update(usage)  # cumulative snapshots, never sum deltas
        if msg.get("stop_reason") or msg.get("stopReason"):
            self.current["stop_reason"] = msg.get("stop_reason") or msg.get("stopReason")

    def _feed(self, ev: dict, now: float) -> None:
        if self.first_event is None:
            self.first_event = now
        typ = ev.get("type")
        if typ in ("auto_retry_start", "auto_compaction_start"):
            self.retries += typ == "auto_retry_start"
            self.compactions += typ == "auto_compaction_start"
        if typ == "system" and ev.get("subtype") == "compact_boundary":
            self.compactions += 1
        if typ == "agent_settled" or typ == "result":
            self.settled = now
            self._commit(now)
        if self.identity["engine"] == "claude-code":
            if typ == "stream_event":
                e = ev.get("event") or {}
                et = e.get("type")
                if et == "message_start":
                    self._begin(e.get("message") or {}, now, streamed=True)
                elif self.current:
                    if et == "content_block_delta" and self.current["first_delta"] is None:
                        self.current["first_delta"] = now
                    elif et == "message_delta":
                        self._merge({**(e.get("delta") or {}), "usage": e.get("usage") or {}})
                    elif et == "message_stop":
                        self.current.update(end=now, complete=True)
                        self._commit(now)
            elif typ == "assistant":
                msg = ev.get("message") or {}
                mid = str(msg.get("id") or "")
                if mid and mid in self.seen:
                    return
                if (not self.current and (mid or msg.get("usage"))) or (
                        self.current and mid and mid != self.current["message_id"]):
                    self._begin(msg, now)
                self._merge(msg)
                if self.current and not self.current["streamed"]:
                    # Without partial events, retain all same-id blocks until the next response/result.
                    self.current.update(end=now, complete=True)
            elif typ == "user" and any(b.get("type") == "tool_result" for b in
                    (ev.get("message") or {}).get("content") or [] if isinstance(b, dict)):
                # A fast tool can finish before message_delta/message_stop delivers final output usage.
                # This is the NEXT response's timing boundary, not the current response's completion.
                if self.current:
                    self.current["next_tool_boundary"] = now
                self.boundary, self.boundary_kind = now, "last_tool_result"
        else:
            msg = ev.get("message") or {}
            mid = str(msg.get("id") or "")
            if msg.get("role") == "assistant" and mid and mid in self.seen:
                return  # replayed events must not reopen a call or overwrite a different active response
            if typ == "message_start" and msg.get("role") == "assistant":
                self._begin(msg, now)
            elif typ == "message_update" and self.current:
                delta = ev.get("assistantMessageEvent") or {}
                if str(delta.get("type") or "").endswith("_delta") and self.current["first_delta"] is None:
                    self.current["first_delta"] = now
            elif typ == "message_end":
                if msg.get("role") == "assistant":
                    if not self.current or (mid and mid != self.current["message_id"]):
                        self._begin(msg, now)
                    self._merge(msg)
                    self.current.update(end=now, complete=True)
                    self._commit(now)
                elif msg.get("role") == "toolResult":
                    self.boundary, self.boundary_kind = now, "last_tool_result"

    def _commit(self, now: float) -> None:
        c = self.current
        if not c:
            return
        self.current = None
        self.count += 1
        end = c["end"] if c["end"] is not None else now
        u = c["usage"]
        pi = self.identity["engine"] != "claude-code"
        names = ("input", "output", "cacheRead", "cacheWrite") if pi else (
            "input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")
        values = [int(u[n]) if u.get(n) is not None else None for n in names]
        inp, out, read, write = values
        # Only a provider-supplied complete input breakdown supports a cache-hit ratio. Unknown != zero.
        total = inp + read + write if all(v is not None for v in (inp, read, write)) else None
        cost = (u.get("cost") or {}).get("total") if pi else None
        details = u.get("output_tokens_details") or u.get("completion_tokens_details") or {}
        reasoning = u.get("reasoning_tokens", details.get("reasoning_tokens"))
        delta = c["first_delta"]
        record = {"schema_version": 1, **self.identity, "call_index": self.count,
                  "message_id": c["message_id"], "model": c["model"], "provider": c["provider"],
                  "started_at": self.wall_started + c["start"] - self.started,
                  "ended_at": self.wall_started + end - self.started,
                  "complete": c["complete"], "stop_reason": c["stop_reason"],
                  "observed_turn_s": round(max(0, end - c["boundary"]), 6),
                  "turn_boundary": c["boundary_kind"],
                  "tool_and_harness_gap_s": (round(max(0, c["boundary"] - c["previous_end"]), 6)
                                             if c["previous_end"] is not None else None),
                  "event_span_s": round(max(0, end - c["start"]), 6),
                  "event_span_scope": "response_stream_only" if not pi else "assistant_message_lifecycle",
                  "time_to_first_delta_s": round(delta - c["start"], 6) if delta is not None else None,
                  "boundary_to_first_delta_s": round(delta - c["boundary"], 6) if delta is not None else None,
                  "provider_request_latency_s": None,
                  "input_tokens": inp, "output_tokens": out,
                  "reasoning_tokens": int(reasoning) if reasoning is not None else None,
                  "cache_read_input_tokens": read,
                  "cache_creation_input_tokens": write, "total_input_tokens": total,
                  "cache_hit_ratio": round(read / total, 6) if total else None,
                  "cost_usd": float(cost) if cost is not None else None,
                  "cost_source": "reported" if cost is not None else "unavailable",
                  "coverage": "visible_assistant_responses_only"}
        self.event_seconds += record["event_span_s"]
        self.previous_end = end
        if c["next_tool_boundary"] is not None:
            self.boundary, self.boundary_kind = c["next_tool_boundary"], "last_tool_result"
        else:
            self.boundary, self.boundary_kind = end, "previous_response_end"
        if c["message_id"]:
            self.seen.add(c["message_id"])
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(record) + "\n")
        finally:
            if self.trace is not None:
                self.trace.call_finished(record)

    def finish(self) -> dict:
        if self.finished:
            return self.summary
        self.finished = True
        now = self.clock()
        try:
            self._commit(now)
        except Exception:  # noqa: BLE001
            self.errors += 1
        self.summary = {"schema_version": 1, **self.identity, "visible_calls": self.count,
                        "process_seconds": round(now - self.started, 6),
                        "first_event_delay_s": (self.first_event - self.started
                                                if self.first_event is not None else None),
                        "first_assistant_delay_s": (self.first_assistant - self.started
                                                    if self.first_assistant is not None else None),
                        "settled_to_exit_s": now - self.settled if self.settled is not None else None,
                        "event_span_seconds_sum": round(self.event_seconds, 6),
                        "retry_events": self.retries, "compaction_events": self.compactions,
                        "metrics_errors": self.errors, "calls_file": str(self.path),
                        "coverage": "visible_assistant_responses_only",
                        "hidden_provider_retries_or_compaction_calls": "not_observable"}
        with contextlib.suppress(OSError):
            self.summary_path.parent.mkdir(parents=True, exist_ok=True)
            self.summary_path.write_text(json.dumps(self.summary, indent=2) + "\n", encoding="utf-8")
        return self.summary
