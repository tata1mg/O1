from pathlib import Path

from rfc_agent.streams import parse_stream

FIXTURES = Path(__file__).parents[1] / "fixtures"


def test_parse_ok_stream():
    res = parse_stream((FIXTURES / "stream_ok.jsonl").read_text())
    assert res.final_text == "final answer text"
    assert not res.is_error
    assert [t["tool_name"] for t in res.tool_calls] == ["Grep", "Read"]
    assert res.cost_usd == 0.42
    assert res.usage == {"input_tokens": 1000, "output_tokens": 200}
    assert res.num_turns == 7


def test_parse_error_stream_keeps_cost_but_no_text():
    res = parse_stream((FIXTURES / "stream_error.jsonl").read_text())
    assert res.is_error
    assert res.final_text == ""  # errored run never yields a final answer
    assert res.cost_usd == 1.99  # money spent is still observable
    assert len(res.tool_calls) == 1


def test_parses_actual_model_and_error_reason():
    """The child inherits the operator's ambient model when --model isn't passed, so the manifest must
    record what RAN, not what we asked for (it used to log the literal 'default'). Error reason likewise
    distinguishes a transient api_error from a budget kill."""
    stream = (
        '{"type":"system","subtype":"init","model":"claude-sonnet-5"}\n'
        '{"type":"result","is_error":true,"terminal_reason":"api_error","total_cost_usd":8.01}'
    )
    res = parse_stream(stream)
    assert res.model == "claude-sonnet-5"
    assert res.error_reason == "api_error"
    assert res.is_error and res.cost_usd == 8.01


def test_parse_garbage_lines_ignored():
    res = parse_stream("not json\n\n[1,2]\n" + '{"type":"result","is_error":false,"result":"x"}')
    assert res.final_text == "x"
    assert res.tool_calls == []


def test_drain_activity_appends_human_lines(tmp_path):
    """The live feed: one readable line per tool call, incremental byte offsets, concurrent-safe appends."""
    import json as _json

    from rfc_agent.engine import drain_activity
    (tmp_path / "logs").mkdir()
    sp = tmp_path / "logs" / "ground.stream.b1.jsonl"
    ev = {"type": "assistant", "timestamp": "2026-08-03T16:00:01.000Z",
          "message": {"content": [{"type": "tool_use", "name": "Read",
                                   "input": {"file_path": "svc_d/app/models/skill.rb"}}]}}
    sp.write_text(_json.dumps(ev) + "\n")
    pos = drain_activity(sp, 0, tmp_path, "ground.b1")
    ev2 = {"type": "assistant", "timestamp": "2026-08-03T16:00:05.000Z",
           "message": {"content": [{"type": "tool_use", "name": "Bash",
                                    "input": {"command": "grep -rn smart_slot app/"}}]}}
    with open(sp, "a") as f:
        f.write(_json.dumps(ev2) + "\n")
    pos = drain_activity(sp, pos, tmp_path, "ground.b1")
    import re as _re
    feed = (tmp_path / "logs" / "activity.log").read_text().splitlines()
    # wall-clock timestamps (stream timestamps are UTC; the feed must be one consistent clock) and
    # last-3-segment path rendering — content asserted, exact time not
    assert len(feed) == 2
    assert _re.match(r"^\[\d\d:\d\d:\d\d\] \[ground\.b1\] Read app/models/skill\.rb$", feed[0])
    assert _re.match(r"^\[\d\d:\d\d:\d\d\] \[ground\.b1\] Bash grep -rn smart_slot app/$", feed[1])
    # incremental: draining again from pos adds nothing
    assert drain_activity(sp, pos, tmp_path, "ground.b1") == pos
    assert len((tmp_path / "logs" / "activity.log").read_text().splitlines()) == 2


def test_drain_activity_renders_delta_progress(tmp_path):
    """--include-partial-messages deltas become throttled progress heartbeats — the fix for 3-6 min
    silences during big document writes (industry pattern: stream deltas, not just completed events)."""
    import json as _json

    from rfc_agent.engine import drain_activity
    (tmp_path / "logs").mkdir()
    sp = tmp_path / "logs" / "generate.stream.jsonl"
    events = [
        {"type": "stream_event", "event": {"type": "content_block_start",
                                           "content_block": {"type": "tool_use", "name": "Write"}}},
        {"type": "stream_event", "event": {"type": "content_block_delta",
                                           "delta": {"type": "input_json_delta",
                                                     "partial_json":
                                                         '{"file_path": "out/rfc.md", "content": "' + "x" * 100}}},
    ]
    # 20KB more of payload in 4KB chunks -> crosses the 16KB throttle exactly once
    events += [{"type": "stream_event", "event": {"type": "content_block_delta",
                "delta": {"type": "input_json_delta", "partial_json": "y" * 4096}}} for _ in range(5)]
    sp.write_text("\n".join(_json.dumps(e) for e in events) + "\n")
    state = {}
    drain_activity(sp, 0, tmp_path, "generate", state)
    feed = (tmp_path / "logs" / "activity.log").read_text()
    assert feed.count("… Write streaming") == 1
    assert "into rfc.md" in feed


def test_drain_activity_surfaces_denials(tmp_path):
    """Auto-denials cost a silent reformulation round-trip each (measured 2-9/run) — the feed must show
    WHAT was denied and WHY (write-guard containment vs generic permission auto-deny)."""
    import json as _json

    from rfc_agent.engine import drain_activity
    (tmp_path / "logs").mkdir()
    sp = tmp_path / "logs" / "ground.stream.b1.jsonl"
    events = [
        {"type": "assistant", "message": {"content": [{"type": "tool_use", "id": "t1", "name": "Bash",
                                                       "input": {"command": "rm -rf /tmp/x"}}]}},
        {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "t1", "is_error": True,
                                                  "content":
                                                      "PreToolUse:Bash hook error: [guard_writes.py]: DENIED"}]}},
    ]
    sp.write_text("\n".join(_json.dumps(e) for e in events) + "\n")
    drain_activity(sp, 0, tmp_path, "ground.b1", {})
    feed = (tmp_path / "logs" / "activity.log").read_text()
    assert "⛔ denied [write-guard (containment)]: Bash rm -rf /tmp/x" in feed


def test_drain_survives_and_renders_string_message_events(tmp_path):
    """CLI 2.1.224 added system/permission_denied events whose `message` is a plain STRING — the old
    `(ev.get("message") or {}).get(...)` crashed the whole runner mid-stage (live, 2026-08-07). The
    drain must survive ANY message shape and render the new event as a first-class denial line."""
    import json as _json

    from rfc_agent.engine import drain_activity
    (tmp_path / "logs").mkdir()
    sp = tmp_path / "logs" / "extract.stream.jsonl"
    events = [
        {"type": "assistant", "message": {"content": [{"type": "tool_use", "id": "t1", "name": "Bash",
                                                       "input": {"command": "python3 -m json.tool x.json"}}]}},
        {"type": "system", "subtype": "permission_denied", "tool_name": "Bash", "tool_use_id": "t1",
         "message": "This Bash command contains multiple operations. The following part requires approval: python3"},
        {"type": "assistant", "message": "a bare string here must not crash parsing"},
        {"type": "user", "message": "nor here"},
    ]
    sp.write_text("\n".join(_json.dumps(e) for e in events) + "\n")
    drain_activity(sp, 0, tmp_path, "extract", {})
    feed = (tmp_path / "logs" / "activity.log").read_text()
    assert "⛔ denied [permission]: Bash python3 -m json.tool x.json" in feed
    assert "requires approval" in feed


def test_parse_stream_survives_string_message():
    from rfc_agent.streams import parse_stream
    stream = ('{"type":"system","subtype":"permission_denied","message":"plain string"}\n'
              '{"type":"assistant","message":"also a string"}\n'
              '{"type":"result","is_error":false,"result":"ok","total_cost_usd":0.1}')
    res = parse_stream(stream)
    assert res.final_text == "ok" and not res.is_error


def test_drain_renders_throttled_reasoning_trace(tmp_path):
    """The reasoning channel (operator ask 2026-08-07: 'stream the thinking, not just tool calls').
    thinking_delta/text_delta become throttled 💭/💬 snippets cut at sentence boundaries; short blocks
    flush their gist at content_block_stop; the raw firehose never reaches the feed."""
    import json as _json

    from rfc_agent.engine import drain_activity
    (tmp_path / "logs").mkdir()
    sp = tmp_path / "logs" / "review.stream.skeptic.jsonl"
    events = [
        {"type": "stream_event", "event": {"type": "content_block_start",
                                           "content_block": {"type": "thinking"}}},
    ]
    # 3 x 200-char thoughts with sentence boundaries -> crosses the 400-char throttle once mid-stream
    for i in range(3):
        events.append({"type": "stream_event", "event": {"type": "content_block_delta",
                       "delta": {"type": "thinking_delta",
                                 "thinking":
                                     f"Thought {i}: the gating boundary wraps too much code. " + "x" * 150 + ". "}}})
    events.append({"type": "stream_event", "event": {"type": "content_block_stop"}})
    # a short narration block -> flushed as gist at stop
    events.append({"type": "stream_event", "event": {"type": "content_block_start",
                                                     "content_block": {"type": "text"}}})
    events.append({"type": "stream_event", "event": {"type": "content_block_delta",
                   "delta": {"type": "text_delta",
                             "text": "Confirmed — it only reads partial_report_available, not partial_report_state."}}})
    events.append({"type": "stream_event", "event": {"type": "content_block_stop"}})
    sp.write_text("\n".join(_json.dumps(e) for e in events) + "\n")
    drain_activity(sp, 0, tmp_path, "review.skeptic", {})
    feed = (tmp_path / "logs" / "activity.log").read_text()
    assert "💭" in feed and "gating boundary wraps too much code" in feed
    assert "💬 Confirmed — it only reads partial_report_available" in feed
    assert feed.count("💭") <= 3  # still throttled by cadence, not one line per delta
    assert "x" * 150 in feed      # but NOT truncated — the whole thought is shown (no cap)


def test_drain_renders_thinking_depth_when_text_withheld(tmp_path):
    """Newer models ship thinking with display=omitted: empty text + signature + estimated_tokens
    (measured live: 52 thinking blocks, 0 chars). The feed shows depth honestly instead of nothing."""
    import json as _json

    from rfc_agent.engine import drain_activity
    (tmp_path / "logs").mkdir()
    sp = tmp_path / "logs" / "generate.stream.jsonl"
    events = [
        {"type": "stream_event", "event": {"type": "content_block_start",
                                           "content_block": {"type": "thinking", "thinking": "", "signature": ""}}},
        {"type": "stream_event", "event": {"type": "content_block_delta",
                                           "delta": {"type": "thinking_delta", "thinking": "",
                                                     "estimated_tokens": 150}}},
        {"type": "stream_event", "event": {"type": "content_block_delta",
                                           "delta": {"type": "thinking_delta", "thinking": "",
                                                     "estimated_tokens": 80}}},
        {"type": "stream_event", "event": {"type": "content_block_stop"}},
        # a tiny withheld block below the 100-token floor stays silent (no noise)
        {"type": "stream_event", "event": {"type": "content_block_start",
                                           "content_block": {"type": "thinking", "thinking": "", "signature": ""}}},
        {"type": "stream_event", "event": {"type": "content_block_delta",
                                           "delta": {"type": "thinking_delta", "thinking": "",
                                                     "estimated_tokens": 30}}},
        {"type": "stream_event", "event": {"type": "content_block_stop"}},
    ]
    sp.write_text("\n".join(_json.dumps(e) for e in events) + "\n")
    drain_activity(sp, 0, tmp_path, "generate", {})
    feed = (tmp_path / "logs" / "activity.log").read_text()
    assert "💭 thought for ~230 tokens" in feed
    # the beat MUST say why there's no text — a bare token count left the default (tap-off) feed
    # unexplained (regressed once, restored)
    assert "text withheld" in feed
    assert feed.count("💭") == 1


def test_drain_suppresses_thinking_when_tap_active(tmp_path):
    """v6 (2026-08-10): with the thinking tap on, display=summarized flows through to the CLI stream
    too, so the engine's stream-side 💭 duplicated ~45% of the sidecar's wire-side lines (same text,
    0-1s apart). Tap active => the sidecar owns 💭; the engine still emits 💬 narration (the tap never
    does) and never emits the depth heartbeat."""
    import json as _json

    from rfc_agent.engine import drain_activity
    (tmp_path / "logs").mkdir()
    sp = tmp_path / "logs" / "ground.stream.jsonl"
    events = [
        {"type": "stream_event", "event": {"type": "content_block_start",
                                           "content_block": {"type": "thinking"}}},
        {"type": "stream_event", "event": {"type": "content_block_delta",
         "delta": {"type": "thinking_delta",
                   "thinking": "A long thought about the signing fan-out. " + "y" * 400 + ". "}}},
        {"type": "stream_event", "event": {"type": "content_block_stop"}},
        {"type": "stream_event", "event": {"type": "content_block_start",
                                           "content_block": {"type": "text"}}},
        {"type": "stream_event", "event": {"type": "content_block_delta",
         "delta": {"type": "text_delta", "text": "Verified the fan-out is bounded by the semaphore."}}},
        {"type": "stream_event", "event": {"type": "content_block_stop"}},
    ]
    sp.write_text("\n".join(_json.dumps(e) for e in events) + "\n")
    drain_activity(sp, 0, tmp_path, "ground", {"tap": True})
    feed = (tmp_path / "logs" / "activity.log").read_text()
    assert "💭" not in feed                                   # the sidecar owns thinking lines
    assert "💬 Verified the fan-out is bounded" in feed       # narration still flows


def test_context_tokens_tracks_last_assistant_usage():
    """The resume-cost signal (2026-08-11, TASK-29403): resuming a 171,625-token session paid a 2m24s
    auto-compaction before any work. The parser must report the session's END-of-run context size."""
    import json

    from rfc_agent.streams import parse_stream

    lines = [
        {"type": "system", "subtype": "init", "session_id": "sid-1", "model": "claude-test"},
        {"type": "assistant", "message": {"usage": {"input_tokens": 10, "cache_read_input_tokens": 5000,
                                                    "cache_creation_input_tokens": 100}, "content": []}},
        {"type": "assistant", "message": {"usage": {"input_tokens": 2, "cache_read_input_tokens": 150000,
                                                    "cache_creation_input_tokens": 20000}, "content": []}},
        {"type": "result", "is_error": False, "result": "done"},
    ]
    res = parse_stream("\n".join(json.dumps(x) for x in lines))
    assert res.context_tokens == 170002  # the LAST message wins — that's what a resume would inherit


def test_denials_counted_once_per_tool_use_id():
    """The SAME denial fires both signals on new CLIs (system permission_denied + the tool_result
    error echo) — id-keyed dedupe must count it once; distinct denials count separately."""
    import json

    from rfc_agent.streams import parse_stream

    lines = [
        {"type": "system", "subtype": "permission_denied", "tool_use_id": "t1", "message": "no"},
        {"type": "user", "message": {"content": [
            {"type": "tool_result", "tool_use_id": "t1", "is_error": True,
             "content": "Permission denied by write-guard"}]}},
        {"type": "user", "message": {"content": [
            {"type": "tool_result", "tool_use_id": "t2", "is_error": True,
             "content": "tool call denied: guard_writes"}]}},
        {"type": "user", "message": {"content": [  # an ordinary error is NOT a denial
            {"type": "tool_result", "tool_use_id": "t3", "is_error": True,
             "content": "file not found"}]}},
        {"type": "result", "is_error": False, "result": "done"},
    ]
    res = parse_stream("\n".join(json.dumps(x) for x in lines))
    assert res.denials == 2
    assert res.prompt_sent == ""  # set by run_claude, never by the parser


def test_merge_stream_results_sums_usage_and_denials():
    """Fan-out honesty (audit 2026-08-19): usage used to be the FIRST-FINISHED worker's alone while
    cost summed every worker — a generation reporting all the dollars against one worker's tokens."""
    from rfc_agent.engine import merge_stream_results
    from rfc_agent.streams import StreamResult

    a = StreamResult(cost_usd=1.0, usage={"input_tokens": 10, "output_tokens": 5,
                                          "cache_read_input_tokens": 100,
                                          "cache_creation_input_tokens": 1},
                     num_turns=2, denials=1, compactions=1, context_tokens=50)
    b = StreamResult(cost_usd=2.0, usage={"input_tokens": 20, "output_tokens": 10,
                                          "cache_read_input_tokens": 200,
                                          "cache_creation_input_tokens": 2},
                     num_turns=3, denials=2, context_tokens=80)
    res = merge_stream_results([a, b], a, duration_s=1.0, engine_desc="claude:test (2-way)")
    assert res.cost_usd == 3.0
    assert res.usage == {"input_tokens": 30, "output_tokens": 15,
                         "cache_read_input_tokens": 300, "cache_creation_input_tokens": 3}
    assert res.denials == 3 and res.compactions == 1 and res.context_tokens == 80


def test_denials_ignore_filesystem_permission_errors():
    """Review finding #7: bare 'denied'/'permission' substrings counted a repo file's own EACCES as
    a harness denial. Only harness-shaped denials count: write-guard, auto-deny, permission+granted."""
    import json

    from rfc_agent.streams import parse_stream

    def _res(tool_id, text):
        return {"type": "user", "message": {"content": [
            {"type": "tool_result", "tool_use_id": tool_id, "is_error": True, "content": text}]}}

    lines = [
        _res("t1", "PermissionError: [Errno 13] Permission denied: '/etc/shadow'"),  # EACCES: no
        _res("t2", "grep: warning: permission denied while reading .git/objects"),   # EACCES: no
        _res("t3", "Edit denied by guard_writes hook: path outside the run dir"),    # harness: yes
        _res("t4", "Claude requested permissions to use Bash, but you haven't granted it."),  # yes
        {"type": "system", "subtype": "permission_denied", "tool_use_id": "t5", "message": "no"},  # yes
        {"type": "result", "is_error": False, "result": "done"},
    ]
    res = parse_stream("\n".join(json.dumps(x) for x in lines))
    assert res.denials == 3


def test_merge_usage_survives_non_scalar_values():
    """Review finding #9: an unguarded int() over a stream-parsed value would kill a fully
    successful fan-out stage at the accounting step (and the crash path calls the same function)."""
    from rfc_agent.engine import merge_stream_results
    from rfc_agent.streams import StreamResult

    a = StreamResult(cost_usd=1.0, usage={"input_tokens": {"nested": 5}, "output_tokens": "12",
                                          "cache_read_input_tokens": 7})
    b = StreamResult(cost_usd=2.0, usage={"input_tokens": 3})
    res = merge_stream_results([a, b], a, duration_s=1.0, engine_desc="claude:test (2-way)")
    assert res.usage == {"input_tokens": 3, "output_tokens": 0,
                         "cache_read_input_tokens": 7, "cache_creation_input_tokens": 0}


def test_parse_stream_collects_deliberation_only_from_toolless_messages():
    """P1 (0.24.50): thinking blocks with NO tool_use in the same assistant message are the
    deliberation turns (analysis/pivots/conclusions) — collected for the generation digest. Thinking
    beside a tool_use is that tool's `reason` (engine drain) and must NOT be double-collected."""
    import json as _json

    def _msg(mid, blocks):
        return _json.dumps({"type": "assistant", "message": {"id": mid, "content": blocks}})

    stream = "\n".join([
        _msg("m1", [{"type": "thinking", "thinking": "plan: read the cart builder first"}]),
        _msg("m2", [{"type": "thinking", "thinking": "the guard belongs in update_patients"},
                    {"type": "tool_use", "id": "t1", "name": "Read", "input": {}}]),
        _msg("m3", [{"type": "thinking", "thinking": ""}]),
        _msg("m4", [{"type": "thinking", "thinking": "conclusion: ship the flag"},
                    {"type": "text", "text": "done"}]),
        '{"type":"result","is_error":false,"result":"x"}',
    ])
    res = parse_stream(stream)
    assert res.deliberation == ["plan: read the cart builder first", "conclusion: ship the flag"]


def test_deliberation_blocks_carry_stream_time_prefix():
    """0.24.52 + 0.24.55: each deliberation block is prefixed [HH:MM:SS] in LOCAL time — the Langfuse
    UI renders span timestamps in the viewer's timezone, so a UTC stamp visually disagreed with the
    tool times beside it (field find: [08:17:18] shown against 13:47 IST tools). No timestamp on
    the event -> no prefix, text unchanged."""
    import json as _json
    from datetime import datetime as _dt

    stamped = _json.dumps({"type": "assistant", "timestamp": "2026-08-25T14:03:09.731Z",
                           "message": {"id": "m1", "content": [
                               {"type": "thinking", "thinking": "decision: keep the class attr"}]}})
    bare = _json.dumps({"type": "assistant",
                        "message": {"id": "m2", "content": [
                            {"type": "thinking", "thinking": "final pass"}]}})
    res = parse_stream(stamped + "\n" + bare)
    want = _dt.fromisoformat("2026-08-25T14:03:09.731+00:00").astimezone().strftime("%H:%M:%S")
    assert res.deliberation == [f"[{want}] decision: keep the class attr", "final pass"]
    assert want != "14:03:09" or _dt.now().astimezone().utcoffset().total_seconds() == 0


def test_multi_call_turn_accounting():
    """The parallel-dispatch gauge: turns batching >1 tool_use are counted apart from single-call
    turns (measured 2026-09-03: 0 multi-call turns across a whole run — engine.parallel_dispatch_note
    exists to move this, and manifest multi_call_turn_pct reports it)."""
    stream = (
        '{"type":"assistant","message":{"content":[{"type":"tool_use","name":"Read","input":{}},'
        '{"type":"tool_use","name":"Read","input":{}},{"type":"tool_use","name":"Grep","input":{}}]}}\n'
        '{"type":"assistant","message":{"content":[{"type":"tool_use","name":"Edit","input":{}}]}}\n'
        '{"type":"assistant","message":{"content":[{"type":"text","text":"no tools this turn"}]}}\n'
        '{"type":"result","total_cost_usd":0.1}'
    )
    res = parse_stream(stream)
    assert res.tool_turns == 2          # the text-only turn is not a tool turn
    assert res.multi_call_turns == 1    # only the 3-call batch counts
    assert len(res.tool_calls) == 4     # per-call accounting unchanged


def test_multi_call_counts_group_split_events_by_message_id():
    """stream-json emits one assistant event PER CONTENT BLOCK sharing message.id — a parallel batch
    of two Reads arrives as two events. Per-event counting scored the v2 run's 50-57%-batched shards
    as 0% (2026-09-03). The gauge must group by message."""
    stream = (
        '{"type":"assistant","message":{"id":"m1","content":[{"type":"thinking","thinking":"plan"}]}}\n'
        '{"type":"assistant","message":{"id":"m1","content":[{"type":"tool_use","name":"Read","input":{}}]}}\n'
        '{"type":"assistant","message":{"id":"m1","content":[{"type":"tool_use","name":"Read","input":{}}]}}\n'
        '{"type":"assistant","message":{"id":"m2","content":[{"type":"tool_use","name":"Edit","input":{}}]}}\n'
        '{"type":"result","total_cost_usd":0.1}'
    )
    res = parse_stream(stream)
    assert res.tool_turns == 2, "m1 and m2 — three events, two messages"
    assert res.multi_call_turns == 1, "m1 batched two Reads"
    assert len(res.tool_calls) == 3
