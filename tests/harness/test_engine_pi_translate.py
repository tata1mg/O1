"""Pi -> canonical event translation, with no subprocess anywhere.

The adapter tests drive the whole child; these pin the translation rules themselves, so a change in the
Pi wire format shows up as a small, readable failure rather than as one broken end-to-end test.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from harness.engines.pi_translate import (
    BAD_STOP_REASONS,
    IGNORED,
    OUTPUT_CLIP,
    SESSION_VERSION,
    Translator,
    message_cost_breakdown,
    normalise_input,
    result_text,
    session_usage_cost,
    tool_kind,
)
from harness.events import EventType, EventWriter, ToolKind, read_events, summarize

FIXTURES = Path(__file__).parents[1] / "fixtures" / "pi"


def translate(tmp_path: Path, stream_file: Path, **kw) -> tuple[Translator, list[dict]]:
    trace = tmp_path / "logs" / "t.events.jsonl"
    writer = EventWriter(trace)
    tr = Translator(writer, **kw)
    for line in stream_file.read_text(encoding="utf-8").splitlines():
        if line.strip():
            tr.feed(json.loads(line.replace("__SESSION_ID__", "sess-1").replace("__CWD__", str(tmp_path))))
    tr.finish(exit_code=0)
    writer.close()
    return tr, list(read_events(trace))


# ---- the mapping tables ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name,kind",
    [
        ("read", ToolKind.READ),
        ("grep", ToolKind.SEARCH),
        ("find", ToolKind.SEARCH),
        ("ls", ToolKind.LIST),
        ("write", ToolKind.WRITE),
        ("edit", ToolKind.EDIT),
        ("bash", ToolKind.SHELL),
        ("powershell", ToolKind.SHELL),
        ("deploy", ToolKind.OTHER),  # an extension tool must not be guessed at
        ("", ToolKind.OTHER),
    ],
)
def test_tool_kind_mapping(name, kind):
    assert tool_kind(name) is kind


def test_normalise_input_lands_paths_patterns_and_commands_where_the_ledger_looks():
    assert normalise_input("read", {"path": "raw/prd.md", "offset": 10}) == {"path": "raw/prd.md", "offset": 10}
    assert normalise_input("grep", {"pattern": "REQ-\\d+", "path": "raw"}) == {"pattern": "REQ-\\d+", "path": "raw"}
    assert normalise_input("bash", {"command": "wc -l x", "timeout": 5}) == {"command": "wc -l x", "timeout": 5}
    assert normalise_input("write", {"path": "out/a.json"}) == {"path": "out/a.json"}
    # an unknown tool's args survive verbatim rather than being dropped
    assert normalise_input("deploy", {"target": "prod"}) == {"target": "prod"}
    assert normalise_input("read", "not-a-dict") == {"raw": "not-a-dict"}


def test_result_text_flattens_the_content_block_shape():
    blocks = [{"type": "text", "text": "a"}, {"type": "image"}, {"type": "text", "text": "b"}]
    assert result_text({"content": blocks}) == "a\nb"
    assert result_text("plain") == "plain"
    assert result_text(None) == ""
    assert json.loads(result_text({"exitCode": 0})) == {"exitCode": 0}




def test_message_cost_breakdown_distinguishes_absent_from_zero():
    b = message_cost_breakdown({"usage": {"cost": {"input": 1.0, "output": 2.0, "cacheRead": 0.5,
                                                   "cacheWrite": 0.0, "total": 3.5}}})
    assert b == {"input": 1.0, "output": 2.0, "cacheRead": 0.5, "cacheWrite": 0.0, "total": 3.5}
    assert message_cost_breakdown({"usage": {"input": 10}}) is None  # usage, but no cost
    assert message_cost_breakdown({"usage": {"cost": {"total": 0}}}) == {"total": 0.0}  # measured zero
    assert message_cost_breakdown(None) is None
    # categories but no total: sum them rather than drop the cost
    assert message_cost_breakdown({"usage": {"cost": {"input": 1.0, "output": 2.0}}})["total"] == 3.0


# ---- the recorded happy path -----------------------------------------------------------------------


def test_the_sample_stream_translates_to_the_expected_event_shape(tmp_path):
    tr, evs = translate(tmp_path, FIXTURES / "session.sample.jsonl", prompt="Extract.")
    kinds = [e["t"] for e in evs]
    assert kinds[0] == EventType.INIT and kinds[1] == EventType.USER
    assert kinds[-1] == EventType.RESULT
    assert kinds.count(EventType.TOOL_START) == 4
    assert kinds.count(EventType.TOOL_END) == 4
    assert kinds.count(EventType.THINKING) == 1
    assert kinds.count(EventType.DENIAL) == 0

    assert tr.settled is True and tr.errors == []
    assert tr.num_turns == 4
    assert tr.session_id == "sess-1"
    assert tr.model == "claude-sonnet-5"
    assert tr.stream_cost == pytest.approx(0.041385)
    assert tr.unknown_types == set()

    s = summarize(tmp_path / "logs" / "t.events.jsonl")
    assert s.reads == ["raw/prd.md"] and s.writes == ["out/extract.json"]
    assert [c["name"] for c in s.tool_calls] == ["read", "grep", "bash", "write"]
    assert s.cost_usd == pytest.approx(0.041385)  # the stream sum, when no session file was given


def test_message_update_is_ignored_so_zero_usage_cannot_zero_the_cost(tmp_path):
    """pi's wire `message_update` is delta-only and its usage "may remain zero when a provider only
    reports usage at completion" — reading cost from it would under-report every run."""
    assert "message_update" in IGNORED
    tr, _ = translate(tmp_path, FIXTURES / "session.sample.jsonl")
    assert tr.stream_cost > 0


def test_thinking_and_text_blocks_become_separate_events(tmp_path):
    _, evs = translate(tmp_path, FIXTURES / "session.sample.jsonl")
    thinking = [e for e in evs if e["t"] == EventType.THINKING]
    assert "requirement ids" in thinking[0]["text"]
    texts = [e["text"] for e in evs if e["t"] == EventType.TEXT]
    assert texts[-1].startswith("Extracted 2 requirements")


def test_tool_output_is_clipped(tmp_path):
    trace = tmp_path / "logs" / "clip.events.jsonl"
    writer = EventWriter(trace)
    tr = Translator(writer)
    tr.feed({"type": "session", "id": "s"})
    tr.feed({"type": "tool_execution_end", "toolCallId": "c", "toolName": "read", "isError": False,
             "result": {"content": [{"type": "text", "text": "x" * 5000}]}})
    writer.close()
    end = [e for e in read_events(trace) if e["t"] == EventType.TOOL_END][0]
    assert len(end["output"]) < 5000 and "clipped" in end["output"]
    assert end["output"].startswith("x" * OUTPUT_CLIP)


# ---- failure signals -------------------------------------------------------------------------------


def test_the_error_stream_is_detected_as_a_failure(tmp_path):
    tr, evs = translate(tmp_path, FIXTURES / "session.error.jsonl")
    assert tr.settled is True  # it DID settle — the failure is in the content, not the framing
    assert any("stopReason=error" in e for e in tr.errors)
    assert any("overloaded" in e for e in tr.errors)
    assert any("provider retries exhausted" in e for e in tr.errors)
    assert tr.compactions == 1
    result = [e for e in evs if e["t"] == EventType.RESULT][0]
    assert result["ok"] is False
    assert summarize(tmp_path / "logs" / "t.events.jsonl").compactions == 1


@pytest.mark.parametrize("stop", sorted(BAD_STOP_REASONS))
def test_every_bad_stop_reason_fails(tmp_path, stop):
    trace = tmp_path / f"logs/{stop}.events.jsonl"
    writer = EventWriter(trace)
    tr = Translator(writer)
    tr.feed({"type": "session", "id": "s"})
    tr.feed({"type": "message_end", "message": {"role": "assistant", "stopReason": stop, "content": []}})
    tr.feed({"type": "agent_settled"})
    tr.finish(exit_code=0)
    writer.close()
    assert tr.errors and f"stopReason={stop}" in tr.errors[0]


def test_no_agent_settled_is_a_failure_even_on_a_clean_exit(tmp_path):
    trace = tmp_path / "logs" / "trunc.events.jsonl"
    writer = EventWriter(trace)
    tr = Translator(writer)
    tr.feed({"type": "session", "id": "s"})
    tr.feed({"type": "message_end", "message": {"role": "assistant", "stopReason": "stop", "content": []}})
    tr.finish(exit_code=0)
    writer.close()
    assert any("without agent_settled" in e for e in tr.errors)


def test_agent_end_with_will_retry_is_not_terminal(tmp_path):
    trace = tmp_path / "logs" / "retry.events.jsonl"
    writer = EventWriter(trace)
    tr = Translator(writer)
    tr.feed({"type": "session", "id": "s"})
    tr.feed({"type": "agent_end", "messages": [], "willRetry": True})
    assert tr.settled is False
    tr.feed({"type": "agent_settled"})
    assert tr.settled is True
    writer.close()


def test_an_unknown_event_type_is_recorded_not_swallowed(tmp_path):
    trace = tmp_path / "logs" / "u.events.jsonl"
    writer = EventWriter(trace)
    tr = Translator(writer)
    tr.feed({"type": "session", "id": "s"})
    tr.feed({"type": "some_new_pi_event", "detail": 1})
    writer.close()
    assert tr.unknown_types == {"some_new_pi_event"}
    assert any("unknown pi event type" in (e.get("text") or "") for e in read_events(trace))


def test_an_unexpected_session_format_version_is_flagged_but_not_fatal(tmp_path):
    """The session file is where cost comes from, so a format bump matters. It is a LOG rather than a
    failure: the parse may still be right, and hard-failing every stage on a cosmetic bump costs more."""
    trace = tmp_path / "logs" / "v.events.jsonl"
    writer = EventWriter(trace)
    tr = Translator(writer)
    tr.feed({"type": "session", "id": "s", "version": 4})
    tr.feed({"type": "agent_settled"})
    tr.finish(exit_code=0)
    writer.close()
    evs = list(read_events(trace))
    assert evs[0]["extra"]["session_version"] == 4
    assert any("session format version is 4" in (e.get("text") or "") for e in evs)
    assert [e for e in evs if e["t"] == EventType.RESULT][0]["ok"] is True
    assert tr.errors == []


def test_the_researched_session_version_produces_no_noise(tmp_path):
    _, evs = translate(tmp_path, FIXTURES / "session.sample.jsonl")
    assert SESSION_VERSION == 3
    assert not any("session format version" in (e.get("text") or "") for e in evs)


def test_a_stream_missing_its_header_still_translates(tmp_path):
    """A header is guaranteed to be line 1 at 0.85.1, but losing events over a version change would be
    a worse failure than an INIT with no session id."""
    trace = tmp_path / "logs" / "h.events.jsonl"
    writer = EventWriter(trace)
    tr = Translator(writer, session_id="minted-by-us")
    tr.feed({"type": "tool_execution_start", "toolCallId": "c", "toolName": "read", "args": {"path": "a"}})
    tr.feed({"type": "agent_settled"})
    tr.finish(exit_code=0)
    writer.close()
    evs = list(read_events(trace))
    assert evs[0]["t"] == EventType.INIT and evs[0]["session_id"] == "minted-by-us"
    assert evs[1]["t"] == EventType.TOOL_START


def test_an_unpaired_or_duplicated_tool_call_id_is_translated_verbatim(tmp_path):
    """The translator does no start/end pairing, and `events.summarize` does not either — a tool_end with
    no start, or a repeated toolCallId, must therefore pass through as its own event rather than being
    dropped or merged. Pinned because a future pairing optimisation would silently change it."""
    trace = tmp_path / "logs" / "pair.events.jsonl"
    writer = EventWriter(trace)
    tr = Translator(writer)
    tr.feed({"type": "session", "id": "s"})
    tr.feed({"type": "tool_execution_end", "toolCallId": "orphan", "toolName": "read",
             "isError": False, "result": "no start was ever emitted"})
    tr.feed({"type": "tool_execution_start", "toolCallId": "dup", "toolName": "read", "args": {"path": "a"}})
    tr.feed({"type": "tool_execution_start", "toolCallId": "dup", "toolName": "read", "args": {"path": "b"}})
    tr.feed({"type": "tool_execution_end", "toolCallId": "dup", "toolName": "read", "isError": True,
             "result": "boom"})
    tr.feed({"type": "agent_settled"})
    tr.finish(exit_code=0)
    writer.close()

    evs = list(read_events(trace))
    assert [e["call_id"] for e in evs if e["t"] == EventType.TOOL_END] == ["orphan", "dup"]
    assert [e["call_id"] for e in evs if e["t"] == EventType.TOOL_START] == ["dup", "dup"]
    s = summarize(trace)
    assert s.reads == ["a", "b"], "both starts count as reads; neither is deduped away"
    assert [e["ok"] for e in evs if e["t"] == EventType.TOOL_END] == [True, False]
    assert tr.errors == [], "an isError tool result is the model's problem, not a stage failure"


def test_a_non_json_stdout_line_becomes_an_unknown_event_not_a_crash(tmp_path):
    trace = tmp_path / "logs" / "nj.events.jsonl"
    writer = EventWriter(trace)
    tr = Translator(writer)
    tr.feed({"type": "session", "id": "s"})
    tr.feed({"type": "__nonjson__", "raw": "npm warn EBADENGINE something"})
    tr.feed({"type": "agent_settled"})
    tr.finish(exit_code=0)
    writer.close()
    assert tr.unknown_types == {"__nonjson__"}
    assert [e for e in read_events(trace) if e["t"] == EventType.RESULT][0]["ok"] is True


def test_the_denial_cursor_is_a_byte_offset_so_crlf_cannot_drift_it(tmp_path):
    """The cursor advances by bytes on disk. Read through a TEXT handle, universal-newline translation
    makes a \r\n record one byte shorter than it is on disk, and the cursor slides into the next record."""
    denials = tmp_path / "crlf.jsonl"
    recs = [{"type": "harness_denial", "tool": t, "reason": "r"} for t in ("write", "bash", "edit")]
    denials.write_bytes(b"".join(json.dumps(r).encode() + b"\r\n" for r in recs))
    trace = tmp_path / "logs" / "crlf.events.jsonl"
    writer = EventWriter(trace)
    tr = Translator(writer, denials_path=denials)
    tr.feed({"type": "session", "id": "s"})
    assert tr.drain_denials() == 3
    assert tr.drain_denials() == 0
    writer.close()
    assert [e["tool"] for e in read_events(trace) if e["t"] == EventType.DENIAL] == ["write", "bash", "edit"]


# ---- denials ---------------------------------------------------------------------------------------


def test_denials_are_tailed_incrementally_and_partial_lines_wait(tmp_path):
    denials = tmp_path / "d.jsonl"
    denials.write_text(json.dumps({"type": "harness_denial", "tool": "write", "toolCallId": "c1",
                                   "reason": "outside the run dir"}) + "\n", encoding="utf-8")
    trace = tmp_path / "logs" / "d.events.jsonl"
    writer = EventWriter(trace)
    tr = Translator(writer, denials_path=denials)
    tr.feed({"type": "session", "id": "s"})
    assert tr.drain_denials() == 1
    assert tr.drain_denials() == 0, "a second drain must not re-emit what it already read"

    with open(denials, "a", encoding="utf-8") as fh:  # a half-written record
        fh.write('{"type":"harness_denial","tool":"bash"')
    assert tr.drain_denials() == 0
    with open(denials, "a", encoding="utf-8") as fh:
        fh.write(',"reason":"mutator"}\n')
    assert tr.drain_denials() == 1

    tr.feed({"type": "agent_settled"})
    tr.finish(exit_code=0)
    writer.close()
    evs = [e for e in read_events(trace) if e["t"] == EventType.DENIAL]
    assert [e["tool"] for e in evs] == ["write", "bash"]
    assert summarize(trace).denials == 2


def test_a_denial_arriving_in_the_stream_is_also_accepted(tmp_path):
    """Measured against 0.85.1, an extension's console.log goes to stderr, so a denial cannot reach the
    stream today and the side file is the only channel. This branch is what a future stream-native
    channel would land in, and it costs one `elif` to keep the translator ready for it."""
    trace = tmp_path / "logs" / "ds.events.jsonl"
    writer = EventWriter(trace)
    tr = Translator(writer)
    tr.feed({"type": "session", "id": "s"})
    tr.feed({"type": "harness_denial", "tool": "write", "toolCallId": "c", "reason": "nope"})
    writer.close()
    assert tr.denials == 1


# ---- cost from the session file --------------------------------------------------------------------


def test_session_usage_cost_returns_the_summed_breakdown(tmp_path):
    f = tmp_path / "b.jsonl"
    f.write_text(
        "\n".join(
            [
                json.dumps({"type": "session", "version": 3, "id": "s", "cwd": "/x"}),
                json.dumps({"type": "message", "id": "1", "parentId": None, "message": {
                    "role": "assistant", "usage": {"cost": {"input": 1.0, "cacheRead": 0.25, "total": 1.25}}}}),
                json.dumps({"type": "message", "id": "2", "parentId": "1", "message": {
                    "role": "toolResult", "usage": {"cost": {"input": 0.5, "cacheRead": 0.0, "total": 0.5}}}}),
            ]
        ),
        encoding="utf-8",
    )
    assert session_usage_cost(f) == {"input": 1.5, "cacheRead": 0.25, "total": 1.75}
    assert session_usage_cost(tmp_path / "absent.jsonl") is None






def test_finish_prefers_the_session_file_breakdown_and_labels_the_source(tmp_path):
    trace = tmp_path / "logs" / "c.events.jsonl"
    writer = EventWriter(trace)
    tr = Translator(writer)
    tr.feed({"type": "session", "id": "s"})
    tr.feed({"type": "agent_settled"})
    tr.finish(exit_code=0, cost={"input": 1.0, "output": 0.25, "total": 1.25})
    writer.close()
    result = [e for e in read_events(trace) if e["t"] == EventType.RESULT][0]
    assert result["cost_usd"] == 1.25
    assert result["extra"]["cost_source"] == "session_file"
    assert result["extra"]["cost"]["input"] == 1.0


def test_finish_reports_an_unknown_cost_as_none_never_zero(tmp_path):
    """"no cost was reported" and "the cost was zero" are different facts. Emitting 0.0 for the first
    would make an unmeasured stage look free in every downstream total."""
    trace = tmp_path / "logs" / "n.events.jsonl"
    writer = EventWriter(trace)
    tr = Translator(writer)
    tr.feed({"type": "session", "id": "s"})
    tr.feed({"type": "message_end", "message": {"role": "assistant", "stopReason": "stop",
                                                "usage": {"input": 10, "output": 2}, "content": []}})
    tr.feed({"type": "agent_settled"})
    tr.finish(exit_code=0)
    writer.close()
    assert tr.stream_cost_breakdown is None
    result = [e for e in read_events(trace) if e["t"] == EventType.RESULT][0]
    assert "cost_usd" not in result
    assert result["extra"]["cost_source"] == "none"
    assert summarize(trace).cost_usd is None


def test_a_genuine_zero_cost_is_still_reported_as_zero(tmp_path):
    """The mirror image: a provider that really did report {total: 0} has MEASURED zero, and that is a
    fact worth keeping distinct from silence."""
    trace = tmp_path / "logs" / "z.events.jsonl"
    writer = EventWriter(trace)
    tr = Translator(writer)
    tr.feed({"type": "session", "id": "s"})
    tr.feed({"type": "message_end", "message": {"role": "assistant", "stopReason": "stop", "content": [],
                                                "usage": {"cost": {"input": 0, "output": 0, "total": 0}}}})
    tr.feed({"type": "agent_settled"})
    tr.finish(exit_code=0)
    writer.close()
    result = [e for e in read_events(trace) if e["t"] == EventType.RESULT][0]
    assert result["cost_usd"] == 0.0
    assert result["extra"]["cost_source"] == "event_stream"


def test_the_stream_sum_accumulates_every_category(tmp_path):
    tr, _ = translate(tmp_path, FIXTURES / "session.sample.jsonl")
    b = tr.stream_cost_breakdown
    assert b["total"] == pytest.approx(0.041385)
    assert b["input"] == pytest.approx(0.0276)
    assert b["cacheWrite"] == pytest.approx(0.003)
