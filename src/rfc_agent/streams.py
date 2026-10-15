"""Parse `claude -p --output-format stream-json` output. Ported from evals_repo/evals/streams.py
(claude branch only — cursor engine dropped), extended to pull cost/usage off the terminal result event.

final_text = the agent's FINAL answer only (the terminal `result` event), "" on an errored/cut-off run.
We deliberately do NOT accumulate intermediate assistant text — the `Skill` tool echoes SKILL.md into the
stream, and harvesting that would surface skill boilerplate instead of the answer.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field


@dataclass
class StreamResult:
    final_text: str = ""
    tool_calls: list[dict] = field(default_factory=list)
    cost_usd: float | None = None
    usage: dict | None = None
    is_error: bool = False
    num_turns: int | None = None
    # the model the subprocess ACTUALLY used, off the init event. Needed because the harness passes
    # --model only when one is configured; with none, the child silently inherits the operator's ambient
    # Claude Code default, and the manifest/Langfuse recorded the literal string "default" (observed
    # 2026-07-27: a whole run executed on claude-fable-5 with nothing in the manifest saying so, which
    # makes cross-branch eval comparisons meaningless).
    model: str | None = None
    error_reason: str | None = None  # terminal_reason/subtype off the result event (e.g. api_error)
    session_id: str | None = None  # off the init event — lets a revision --resume this session (warm
    # context: measured 12.7s for a targeted-edit revision vs 400s+ for a fresh-context rerun)
    context_tokens: int | None = None  # the LAST assistant message's input+cache tokens ≈ the session's
    # context size at end of run. The resume-cost signal: resuming a session near the CLI's auto-compact
    # trigger (~150K configured, ~168-172K observed) pays a multi-minute summarization pass before any
    # work starts (measured 2026-08-11: 2m24s at 171,625 tokens on resume, then 3m43s again mid-flight).
    compactions: int = 0  # auto-compaction events observed in the stream. A session that ran PAST the
    # legacy 200K regime with zero compactions is observational proof of a big (1M) window — the
    # resume-cap policy trusts this over CLI-version inference (engine.detect_effective_window).
    denials: int = 0  # sandbox/permission denials (unique tool_use ids across both denial signals) —
    # each one silently costs a reformulation round-trip; measured 2-9 per run, previously visible
    # only as prose in the activity feed
    tool_turns: int = 0  # assistant messages carrying >=1 tool_use
    multi_call_turns: int = 0  # of those, messages batching >1 call — the parallel-dispatch gauge
    # (measured 2026-09-03: 0 across every stage/shard/persona of a full run; engine.parallel_dispatch_note
    # is the countermeasure and manifest multi_call_turn_pct tracks whether it took)
    prompt_sent: str = ""  # the EXACT prompt run_claude sent (post sandbox-affordances append) — the
    # pre-append copy recorded in Langfuse differed from what the model actually received, corrupting
    # every dataset item built from a generation's input
    got_result: bool = False  # a terminal result event was seen. False + nonzero exit = the child
    # died before finishing (auth expiry, bad settings, usage error) — the engine raises instead of
    # letting an empty stream read as a clean zero-output stage (0.24.2)
    extra_results: int = 0  # result events past the first — a post-result bonus turn ran (a steer
    # racing the terminal result). Verdict fields ignore them; the engine records the mismatch as
    # a decision so a bonus turn's discarded is_error is visible instead of silent (0.24.3)
    extra_result_error: bool = False  # any ignored extra result carried is_error=True
    deliberation: list[str] = field(default_factory=list)  # thinking blocks NOT paired with a
    # same-message tool_use — the analysis/pivot/conclusion turns. Tool-paired thinking rides each
    # tool observation as `reason` (engine drain); this list is what the generation-level digest
    # shows, replacing the head+tail clip that deleted the mid-stream decisions (measured: 4.6%
    # coverage of a 130KB stream, with the blocker-resolving design decision in the deleted middle).
    # Text present only when thinking display=summarized reaches the stream (THINKING_TAP on).


def parse_stream(stdout: str) -> StreamResult:
    res = StreamResult()
    denied_ids: set = set()  # the SAME denial can fire both signals (system event + tool_result text)
    tools_by_msg: dict[str, int] = {}  # message.id -> tool_use count (events are per-block, see below)
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
        if ev.get("type") == "system" and "compact" in str(ev.get("subtype") or ""):
            # compact_boundary (and any future compact-* subtype): count, don't parse further
            res.compactions += 1
        if ev.get("type") == "system" and ev.get("subtype") == "permission_denied":
            # first-class denial event (CLI >= 2.1.224); id-keyed so the tool_result echo below
            # never double-counts the same denial
            denied_ids.add(str(ev.get("tool_use_id") or f"sys-{len(denied_ids)}"))
        if ev.get("type") == "system" and ev.get("subtype") == "init":
            if ev.get("model"):
                res.model = str(ev["model"])
            if ev.get("session_id"):
                res.session_id = str(ev["session_id"])
        if ev.get("type") == "user":
            content = (ev.get("message") if isinstance(ev.get("message"), dict) else {}).get("content", [])
            for b in content if isinstance(content, list) else []:
                if isinstance(b, dict) and b.get("type") == "tool_result" and b.get("is_error"):
                    # precision over recall for a metric charted as a bad-run predictor: bare
                    # "denied"/"permission" counted filesystem EACCES ("PermissionError: [Errno 13]
                    # Permission denied") and content mentions as harness denials (review finding
                    # #7). Match only harness-shaped denials: the write-guard hook names itself,
                    # and the CLI's auto-deny phrasing pairs "permission" with "granted"/"auto-den".
                    # Head-bounded — denial messages are short; error payloads run 10-100KB.
                    txt = str(b.get("content"))[:400].lower()
                    if ("guard_writes" in txt or "auto-den" in txt
                            or ("permission" in txt and "granted" in txt)):
                        denied_ids.add(str(b.get("tool_use_id") or f"res-{len(denied_ids)}"))
        if ev.get("type") == "assistant":
            msg = ev.get("message") if isinstance(ev.get("message"), dict) else {}
            raw = msg.get("content", [])
            content = [b for b in raw if isinstance(b, dict)] if isinstance(raw, list) else []
            n_tools = sum(1 for b in content if b.get("type") == "tool_use")
            has_tool = n_tools > 0
            if n_tools:
                # stream-json emits ONE assistant event PER CONTENT BLOCK, all sharing message.id —
                # a parallel batch of 3 Reads arrives as 3 events. Counting per event scored every
                # batch as three single-call turns (measured 2026-09-03: the v2 run's shards were
                # 50-57% batched by message, reported as 0%). Group by message id; the totals are
                # derived after the loop.
                mid = str(msg.get("id") or f"ev-{len(tools_by_msg)}")
                tools_by_msg[mid] = tools_by_msg.get(mid, 0) + n_tools
            for block in content:
                if block.get("type") == "tool_use":
                    res.tool_calls.append({"tool_name": block.get("name"), "input": block.get("input", {})})
                elif block.get("type") == "thinking" and not has_tool and (block.get("thinking") or "").strip():
                    # deliberation = thinking with no tool_use in the SAME assistant message (the
                    # API-sanctioned pairing boundary — Anthropic docs: a message's thinking block is
                    # "the reasoning that led to the tool requests" beside it).
                    # [HH:MM:SS] stream-time prefix (0.24.52): the digest blocks carried no position,
                    # so interleaving them with tool startTimes was string-matching archaeology — the
                    # stamp makes the join numeric in both directions (a tool's startTime slots
                    # between two stamped blocks). LOCAL time, not the stream's UTC (0.24.55,
                    # a teammate reading a live digest): the Langfuse UI renders every span timestamp
                    # in the viewer's timezone, so a UTC stamp visually disagreed with the tool
                    # times beside it by the UTC offset (+5:30 here).
                    stamp = ""
                    try:
                        from datetime import datetime as _dt
                        raw_ts = str(ev.get("timestamp") or "")
                        if raw_ts:
                            local = _dt.fromisoformat(raw_ts.replace("Z", "+00:00")).astimezone()
                            stamp = f"[{local:%H:%M:%S}] "
                    except ValueError:
                        stamp = ""
                    res.deliberation.append(stamp + block["thinking"])
            u = msg.get("usage")
            if isinstance(u, dict):  # later messages overwrite — we want the session's FINAL size
                res.context_tokens = sum(
                    int(u.get(k) or 0)
                    for k in ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")
                )
        if ev.get("type") == "result":
            if not res.got_result:
                # verdict fields are FIRST-result-wins (defense-in-depth for the 0.24.1 pump tick
                # swap): a bonus turn racing stage completion appends a second result event whose
                # verdict must not rewrite the stage's real outcome
                res.is_error = bool(ev.get("is_error"))
                res.final_text = "" if res.is_error else (
                    ev.get("result") if isinstance(ev.get("result"), str) else "")
                if res.is_error:
                    res.error_reason = str(ev.get("terminal_reason") or ev.get("subtype") or "unknown")
            else:
                res.extra_results += 1
                if ev.get("is_error"):
                    res.extra_result_error = True
            res.got_result = True
            # cost/usage/num_turns stay LAST-wins — cumulative session totals, and a cut-off run
            # still spent money and must be observable
            if isinstance(ev.get("total_cost_usd"), (int, float)):
                res.cost_usd = float(ev["total_cost_usd"])
            if isinstance(ev.get("usage"), dict):
                res.usage = ev["usage"]
            if isinstance(ev.get("num_turns"), int):
                res.num_turns = ev["num_turns"]
    res.denials = len(denied_ids)
    res.tool_turns = sum(1 for n in tools_by_msg.values() if n >= 1)
    res.multi_call_turns = sum(1 for n in tools_by_msg.values() if n > 1)
    return res


def stream_event_label(ev: dict) -> str:
    """One-line human hint for the live progress ticker (last event seen)."""
    t = ev.get("type")
    if t == "result":
        return "error" if ev.get("is_error") else "finished"
    if t == "assistant":
        for block in (ev.get("message") if isinstance(ev.get("message"), dict) else {}).get("content", []):
            if isinstance(block, dict) and block.get("type") == "tool_use":
                return str(block.get("name") or "tool")
        return "writing"
    return ""
