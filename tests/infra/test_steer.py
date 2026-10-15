"""Mid-run operator steering (0.24.0): inbox, per-child pump, and the stream-json spawn contract.

The integration test drives the REAL run_claude against a stub `claude` binary — the spawn contract
(prompt off argv onto stdin, steering as later user turns, stdin closed on the terminal result
event) is the risk center of this feature, and only a subprocess round-trip proves it."""

import json
import os
import stat
import sys
import time

from rfc_agent import steer
from rfc_agent.contracts import RunContext


class _Stdin:
    def __init__(self):
        self.lines: list[str] = []
        self.closed = False

    def write(self, s):
        self.lines.append(s)

    def flush(self):
        pass

    def close(self):
        self.closed = True


class _Proc:
    def __init__(self):
        self.stdin = _Stdin()

    def poll(self):
        return None


def _ctx(tmp_path):
    return RunContext(run_dir=tmp_path, inputs={}, config=None)


def test_post_writes_inbox_and_operator_action(tmp_path):
    rec = steer.post(tmp_path, "  check state_feeder_service, not labs_agg  ", lane="ground.b2")
    assert rec["text"] == "check state_feeder_service, not labs_agg" and rec["lane"] == "ground.b2"
    inbox = json.loads((tmp_path / "logs" / "steer.inbox.jsonl").read_text().strip())
    assert inbox["id"] == rec["id"] and inbox["text"] == rec["text"]
    op = json.loads((tmp_path / "logs" / "operator_actions.jsonl").read_text().strip())
    assert op["action"] == "steer" and op["detail"]["lane"] == "ground.b2"


def test_pump_delivers_wrapped_filters_lanes_and_dedupes(tmp_path):
    ctx = _ctx(tmp_path)
    steer.post(tmp_path, "stale note")  # delivered to a "previous" child below => consumed for later pumps
    p0 = _Proc()
    steer.Pump(p0, tmp_path / "logs" / "z.stream.jsonl", ctx, "extract").tick()
    assert "stale note" in "".join(p0.stdin.lines)
    steer.post(tmp_path, "for everyone")                      # broadcast
    steer.post(tmp_path, "for b2 only", lane="ground.b2")     # lane match
    steer.post(tmp_path, "for all of ground", lane="ground")  # prefix match (FULL-mode lanes)
    steer.post(tmp_path, "for the skeptic", lane="review.skeptic")  # other lane

    proc = _Proc()
    pump = steer.Pump(proc, tmp_path / "logs" / "x.stream.jsonl", ctx, "ground.b2")
    pump.tick()
    pump.tick()  # second tick must not redeliver
    texts = "".join(proc.stdin.lines)
    assert texts.count("HUMAN STEERING") == 3, "broadcast + exact lane + prefix lane, exactly once each"
    assert "for everyone" in texts and "for b2 only" in texts and "for all of ground" in texts
    assert "for the skeptic" not in texts and "stale note" not in texts
    # the lane miss is VISIBLE: silent evaporation was indistinguishable from a delivery bug
    assert "steering_skipped" in (tmp_path / "logs" / "decisions.jsonl").read_text()
    # every delivery is a valid stream-json user turn
    for ln in proc.stdin.lines:
        msg = json.loads(ln)
        assert msg["type"] == "user" and msg["message"]["content"][0]["type"] == "text"
    # ...and each is a first-class decision in the run's decision log (3 here + 1 to the extract pump)
    decisions = (tmp_path / "logs" / "decisions.jsonl").read_text()
    assert decisions.count("steering_delivered") == 4


def test_post_caps_text_length(tmp_path):
    """An uncapped steer could exceed the pipe buffer and block the engine's wait loop (which
    also freezes the stage-timeout check) — post() caps like the feed server does."""
    rec = steer.post(tmp_path, "x" * 100_000, lane="l" * 200)
    assert len(rec["text"]) == 4000 and len(rec["lane"]) == 80


def test_pump_closes_stdin_on_terminal_result(tmp_path):
    ctx = _ctx(tmp_path)
    log = tmp_path / "logs" / "x.stream.jsonl"
    log.parent.mkdir(parents=True, exist_ok=True)
    proc = _Proc()
    pump = steer.Pump(proc, log, ctx, "x")
    pump.tick()
    assert not proc.stdin.closed
    log.write_text(json.dumps({"type": "system", "subtype": "init"}) + "\n"
                   + json.dumps({"type": "result", "is_error": False, "result": "done"}) + "\n")
    pump.tick()
    assert proc.stdin.closed, "stream-json input ends on stdin EOF — the pump must supply it"
    steer.post(tmp_path, "too late")
    pump.tick()
    assert not proc.stdin.lines, "nothing is delivered after close"


def test_steering_never_leaks_into_the_next_child(tmp_path):
    """A record DELIVERED to stage N must not re-guide stage N+1 (the 0.24.0 grace window leaked
    it); a note posted BETWEEN children (which the grace window silently dropped) still delivers.
    Consumption = delivery, recorded in the append-only ledger, scoped by the pump's epoch."""
    ctx = _ctx(tmp_path)
    steer.post(tmp_path, "skip labs_agg, out of scope")
    p1 = _Proc()
    pump1 = steer.Pump(p1, tmp_path / "logs" / "a.stream.jsonl", ctx, "ground")
    pump1.tick()
    assert "skip labs_agg" in "".join(p1.stdin.lines)
    pump1.close()

    steer.post(tmp_path, "posted between stages")  # gate evaluation can exceed any time window
    p2 = _Proc()
    pump2 = steer.Pump(p2, tmp_path / "logs" / "b.stream.jsonl", ctx, "review.skeptic")
    pump2.tick()
    texts = "".join(p2.stdin.lines)
    assert "skip labs_agg" not in texts, "consumed steering must not re-guide the next child"
    assert "posted between stages" in texts, "a between-stage post must still deliver"


def test_lane_skipped_steering_survives_to_its_target_stage(tmp_path):
    """0.24.3 root fix: a steer targeting a LATER stage's lane, posted while another stage runs,
    must survive that stage — the 0.24.2 size-watermark consumed lane-skipped records, destroying
    the exact `--lane review.skeptic` workflow the CLI help advertises."""
    ctx = _ctx(tmp_path)
    steer.post(tmp_path, "weigh the migration path", lane="review.skeptic")
    pg = _Proc()
    pump_gen = steer.Pump(pg, tmp_path / "logs" / "g.stream.jsonl", ctx, "generate")
    pump_gen.tick()   # skips (lane mismatch) — must NOT consume
    pump_gen.close()
    assert not pg.stdin.lines

    ps = _Proc()
    steer.Pump(ps, tmp_path / "logs" / "s.stream.jsonl", ctx, "review.skeptic").tick()
    assert "weigh the migration path" in "".join(ps.stdin.lines), \
        "a lane-skipped record must reach its target stage"


def test_never_ticked_pump_does_not_consume(tmp_path):
    """A killed-pre-spend child (figma respawn, kickoff failure, Ctrl-C) whose pump never had a
    delivery opportunity must not consume steering — its replacement gets the record."""
    ctx = _ctx(tmp_path)
    steer.post(tmp_path, "use the checkout frame, not cart")
    p1 = _Proc()
    steer.Pump(p1, tmp_path / "logs" / "k.stream.jsonl", ctx, "ground").close()  # no tick
    p2 = _Proc()
    steer.Pump(p2, tmp_path / "logs" / "k2.stream.jsonl", ctx, "ground").tick()
    assert "checkout frame" in "".join(p2.stdin.lines)


def test_fallback_epoch_redelivers_acked_steering(tmp_path):
    """A resume->cold fallback passes the FAILED child's epoch: steering delivered (and acked) to
    the dead resumed child redelivers to the replacement; a later child with a fresh epoch skips."""
    ctx = _ctx(tmp_path)
    epoch = time.time()
    steer.post(tmp_path, "the booking.py blocker is already fixed")
    p1 = _Proc()
    pump1 = steer.Pump(p1, tmp_path / "logs" / "r1.stream.jsonl", ctx, "review.skeptic", epoch=epoch)
    pump1.tick()
    assert "booking.py" in "".join(p1.stdin.lines)  # delivered + acked to the (soon-dead) resume
    pump1.close()

    p2 = _Proc()  # the cold fallback, same epoch
    steer.Pump(p2, tmp_path / "logs" / "r2.stream.jsonl", ctx, "review.skeptic", epoch=epoch).tick()
    assert "booking.py" in "".join(p2.stdin.lines), "fallback child must re-receive acked steering"

    p3 = _Proc()  # a genuinely later child (fresh epoch) must NOT re-read it
    steer.Pump(p3, tmp_path / "logs" / "r3.stream.jsonl", ctx, "review.skeptic").tick()
    assert not p3.stdin.lines


def test_failed_close_is_retried_and_blocks_delivery(tmp_path):
    """0.24.3: result detection is single-shot (byte offset), so close() must be retried off the
    sticky _saw_result flag — and during a transiently-failed close, delivery stays blocked (a
    write there would start the post-result bonus turn)."""
    ctx = _ctx(tmp_path)
    log = tmp_path / "logs" / "x.stream.jsonl"
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text(json.dumps({"type": "result", "is_error": False, "result": "done"}) + "\n")
    steer.post(tmp_path, "racing message")

    proc = _Proc()
    fails = {"n": 2}
    real_close = proc.stdin.close

    def flaky_close():
        if fails["n"] > 0:
            fails["n"] -= 1
            raise OSError("transient EBADF")
        real_close()

    proc.stdin.close = flaky_close
    pump = steer.Pump(proc, log, ctx, "x")
    pump.tick()   # result seen; close fails (1st)
    assert not pump.closed and not proc.stdin.lines, "no delivery while the close is pending"
    pump.tick()   # close retried (fails 2nd), still no delivery
    assert not pump.closed and not proc.stdin.lines
    pump.tick()   # close retried and succeeds
    assert pump.closed and proc.stdin.closed and not proc.stdin.lines


def test_tick_closes_before_delivering_a_racing_steer(tmp_path):
    """0.24.1: a steer racing the terminal result must NOT start a bonus turn on a finished stage
    (live-verified the CLI runs a queued post-result turn to completion, and its second result
    event rewrote the stage's verdict). The result check runs first — and since consumption =
    delivery (0.24.3), the racing record genuinely carries to the next child."""
    ctx = _ctx(tmp_path)
    log = tmp_path / "logs" / "x.stream.jsonl"
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text(json.dumps({"type": "result", "is_error": False, "result": "done"}) + "\n")
    steer.post(tmp_path, "racing message")
    proc = _Proc()
    pump = steer.Pump(proc, log, ctx, "x")
    pump.tick()
    assert proc.stdin.closed and not proc.stdin.lines, "close first; never a post-result bonus turn"
    p2 = _Proc()  # the racing record was never consumed — the next child receives it
    steer.Pump(p2, tmp_path / "logs" / "y.stream.jsonl", ctx, "x").tick()
    assert "racing message" in "".join(p2.stdin.lines)


_STUB = """#!/usr/bin/env python3
import json, os, select, sys
log = open(os.environ["STUB_STDIN_LOG"], "w")
log.write(sys.stdin.readline()); log.flush()                      # kickoff
print(json.dumps({"type":"system","subtype":"init","model":"stub-model","session_id":"s1"}), flush=True)
r, _, _ = select.select([sys.stdin], [], [], 8)                    # wait for one steering turn
if r:
    log.write(sys.stdin.readline()); log.flush()
print(json.dumps({"type":"result","is_error":False,"result":"done","total_cost_usd":0.5,
                  "num_turns":2,"usage":{"input_tokens":1,"output_tokens":1}}), flush=True)
for ln in sys.stdin:                                               # mimic the CLI: exit on stdin EOF
    log.write(ln)
log.close()
"""


def test_run_claude_stream_json_spawn_contract(tmp_path):
    """End-to-end against a stub binary: kickoff arrives as the first stdin user turn (prompt is no
    longer argv), a queued steering message is delivered mid-run wrapped and authoritative, and the
    harness closes stdin after the result event — without that close this test HANGS (the stub, like
    the real CLI, exits only on stdin EOF)."""
    from rfc_agent.engine import run_claude
    from rfc_agent.runner import Stage

    stub = tmp_path / "fake_claude"
    stub.write_text(_STUB.replace("#!/usr/bin/env python3", f"#!{sys.executable}"))
    stub.chmod(stub.stat().st_mode | stat.S_IEXEC)
    stdin_log = tmp_path / "stub_stdin.log"
    os.environ["STUB_STDIN_LOG"] = str(stdin_log)

    class Cfg:
        def get(self, k, default=None):
            return {"AGENT.CLAUDE_BIN": str(stub)}.get(k, default)

        def materialize_mcp(self, names, dest):
            dest.write_text("{}")
            return dest

    ctx = RunContext(run_dir=tmp_path, inputs={"repos_root": str(tmp_path)}, config=Cfg())
    stage = Stage(name="stub", requires=(), outputs=(), build_prompt=lambda c: "p",
                  gate=lambda c: [])
    steer.post(tmp_path, "look at state_feeder_service instead")  # pre-spawn, no watermark: delivers

    res = run_claude(stage, ctx, "KICKOFF-PROMPT-TEXT", tmp_path / "logs" / "stub.stream.jsonl",
                     timeout=25, live=False)

    lines = stdin_log.read_text().splitlines()
    assert len(lines) == 2, f"kickoff + one steering turn, got {len(lines)}"
    kickoff = json.loads(lines[0])
    assert kickoff["type"] == "user" and "KICKOFF-PROMPT-TEXT" in lines[0]
    steering = json.loads(lines[1])["message"]["content"][0]["text"]
    assert steering.startswith("HUMAN STEERING") and "state_feeder_service" in steering
    assert "gates still apply" in steering.lower()
    assert res.cost_usd == 0.5 and res.model == "stub-model"
    assert "steering_delivered" in (tmp_path / "logs" / "decisions.jsonl").read_text()


def test_parse_stream_first_result_wins_and_counts_extras():
    """Verdict fields latch on the FIRST result (a post-result bonus turn must not rewrite the
    stage outcome); cost/usage/num_turns stay last-wins (cumulative), and the extras are counted
    so the engine can surface a failed bonus turn instead of hiding it."""
    from rfc_agent.streams import parse_stream

    stream = "\n".join([
        json.dumps({"type": "result", "is_error": False, "result": "the real answer",
                    "total_cost_usd": 1.0, "num_turns": 3}),
        json.dumps({"type": "result", "is_error": True, "terminal_reason": "api_error",
                    "total_cost_usd": 1.5, "num_turns": 4}),
    ])
    res = parse_stream(stream)
    assert res.is_error is False and res.final_text == "the real answer"
    assert res.error_reason is None, "the bonus turn's error must not become the stage's verdict"
    assert res.cost_usd == 1.5 and res.num_turns == 4, "accounting keeps the cumulative totals"
    assert res.extra_results == 1 and res.extra_result_error is True


_DEAD_STUB = """#!/usr/bin/env python3
import sys
sys.stdin.readline()                       # accept the kickoff, then die like a usage/auth error
print("boom: no conversation found with session ID", file=sys.stderr)
sys.exit(7)
"""


def test_run_claude_raises_with_stderr_tail_when_child_dies_before_result(tmp_path):
    """The dead-child backstop (0.24.2): nonzero exit + no result event must raise with the stderr
    tail instead of parsing an empty stream as a clean zero-output stage."""
    import pytest

    from rfc_agent.contracts import EngineFailure
    from rfc_agent.engine import run_claude
    from rfc_agent.runner import Stage

    stub = tmp_path / "dead_claude"
    stub.write_text(_DEAD_STUB.replace("#!/usr/bin/env python3", f"#!{sys.executable}"))
    stub.chmod(stub.stat().st_mode | stat.S_IEXEC)

    class Cfg:
        def get(self, k, default=None):
            return {"AGENT.CLAUDE_BIN": str(stub)}.get(k, default)

        def materialize_mcp(self, names, dest):
            dest.write_text("{}")
            return dest

    ctx = RunContext(run_dir=tmp_path, inputs={"repos_root": str(tmp_path)}, config=Cfg())
    stage = Stage(name="stub", requires=(), outputs=(), build_prompt=lambda c: "p", gate=lambda c: [])
    with pytest.raises(EngineFailure) as e:
        run_claude(stage, ctx, "PROMPT", tmp_path / "logs" / "dead.stream.jsonl", timeout=25, live=False)
    assert "exited 7 before emitting a result" in str(e.value)
    assert "no conversation found" in str(e.value), "the stderr tail must ride the error"


def test_unwrap_recovers_raw_text():
    """unwrap(TEMPLATE.format(text=x)) == x — the feed drain's 'read by the agent' ack depends on
    recovering the raw char count from the replayed turn; non-steering text returns None."""
    for raw in ("skip labs_agg", "multi\nline\nnote", "x" * 4000):
        assert steer.unwrap(steer.TEMPLATE.format(text=raw)) == raw
    assert steer.unwrap("just a normal user turn") is None
    assert steer.unwrap("") is None


def test_template_separates_guidance_from_run_control():
    """The wrapper itself ordered the 2026-08-21 mutiny: 'stop the run entirely' arrived under
    'authoritative — follow it', all three review personas halted without writing findings, and the
    harness recorded a fake engine crash ($3.61, review#7). Control/data-plane separation (Temporal
    cancel, K8s SIGTERM, Alexa StopIntent): steers are WORK guidance; stopping the run is the
    terminal's job (Ctrl-C), never something an agent executes off message text."""
    assert "Run control is not yours to execute" in steer.TEMPLATE
    assert "Ctrl-C" in steer.TEMPLATE
    # the amendment lives in the tail after {text} — the ack-matching round-trip must survive it
    assert steer.unwrap(steer.TEMPLATE.format(text="please stop the run entirely")) == \
        "please stop the run entirely"


def test_synthetic_steer_delivers_normally_but_marks_the_decision(tmp_path):
    """A feature-test steer must ride the real session (it tests nothing otherwise) — so it is
    MARKED, never dropped (OTel user_agent.synthetic.type / GA4 traffic_type shape): the delivery
    decision carries synthetic=true so steer analysis can exclude test traffic, which polluted
    radiology-v3 with chitchat delivered as authoritative instructions."""
    ctx = _ctx(tmp_path)
    (tmp_path / "logs").mkdir(parents=True, exist_ok=True)
    rec = {"ts": time.time(), "id": "syn1", "lane": "", "text": "test steer", "synthetic": True}
    (tmp_path / "logs" / "steer.inbox.jsonl").write_text(json.dumps(rec) + "\n")
    proc = _Proc()
    steer.Pump(proc, tmp_path / "logs" / "syn.stream.jsonl", ctx, "generate").tick()
    assert "test steer" in "".join(proc.stdin.lines), "delivered exactly like a real steer"
    delivered = [json.loads(ln) for ln in
                 (tmp_path / "logs" / "decisions.jsonl").read_text().splitlines()]
    d = [x for x in delivered if x["decision"] == "steering_delivered"]
    assert d and d[-1]["synthetic"] is True
    # a real steer's decision carries NO synthetic key — absence is the unmarked default
    steer.post(tmp_path, "real correction")
    p2 = _Proc()
    steer.Pump(p2, tmp_path / "logs" / "syn2.stream.jsonl", ctx, "generate").tick()
    delivered = [json.loads(ln) for ln in
                 (tmp_path / "logs" / "decisions.jsonl").read_text().splitlines()]
    assert "synthetic" not in [x for x in delivered if x["decision"] == "steering_delivered"][-1]


def test_drain_activity_emits_read_ack_for_replayed_steering(tmp_path):
    """--replay-user-messages echoes the injected steering turn into the stream; the drain turns
    that into the 🧭 'read by the agent' line the feed's steer chips key on."""
    from rfc_agent.engine import drain_activity

    raw = "only svc_a and svc_d are in scope"
    (tmp_path / "logs").mkdir()
    log = tmp_path / "x.stream.jsonl"
    log.write_text(json.dumps({"type": "user", "message": {"role": "user", "content": [
        {"type": "text", "text": steer.TEMPLATE.format(text=raw)}]}}) + "\n"
        + json.dumps({"type": "user", "message": {"role": "user", "content": [
            {"type": "text", "text": "an ordinary kickoff prompt"}]}}) + "\n")
    drain_activity(log, 0, tmp_path, "ground.b2")
    feed = (tmp_path / "logs" / "activity.log").read_text()
    assert f"🧭 operator steering read by the agent ({len(raw)} chars)" in feed
    assert feed.count("🧭") == 1, "ordinary user turns must not emit read acks"


def test_wait_loop_flushes_operator_actions_live(tmp_path, monkeypatch):
    """0.24.10: a steer posted mid-stage flushes to Langfuse within a tick of the serial wait loop
    (GENERATE alone is 20-30 min; the stage-boundary flush left the operator's words invisible for
    the whole stretch). Main thread only — fan-out lanes keep the boundary flush (0.22.66 class)."""
    from rfc_agent import engine, obs

    flushed = []
    monkeypatch.setattr(obs, "operator_actions_flush", lambda ctx: flushed.append(1))

    class P:
        args = ["stub"]
        def __init__(self):
            self.polls = 0
        def poll(self):
            self.polls += 1
            return 0 if self.polls > 2 else None

    (tmp_path / "logs").mkdir()
    (tmp_path / "logs" / "operator_actions.jsonl").write_text('{"action": "steer"}\n')
    log = tmp_path / "x.stream.jsonl"
    log.write_text("")
    ctx = RunContext(run_dir=tmp_path, inputs={}, config=None)
    engine._wait_with_progress(P(), log, 30, ctx, quiet=False, obs_parent=None)
    assert flushed, "the live wait loop must flush operator actions"

    flushed.clear()
    engine._wait_with_progress(P(), log, 30, ctx, quiet=True, obs_parent=None)
    assert not flushed, "worker-thread loops (quiet/fan-out) must NOT ambient-flush (orphan traces)"


def test_expire_undelivered_sweeps_only_unsettled(tmp_path):
    """Invocation-scope TTL (2026-08-21 research: control messages expire — MQ TTL+DLQ, Akka dead
    letters, Cursor run-scoped queues). Delivered records are untouched; undelivered ones get a
    ledger tombstone that every future pump's consumed_before excludes, plus a feed line so the
    operator's chip goes terminal. Pre-run steers survive by construction: no invocation ends
    between post and the next start."""
    import time as _time

    a = steer.post(tmp_path, "delivered one", lane="ground")
    b = steer.post(tmp_path, "never delivered", lane="ground")
    steer.mark_consumed(tmp_path, a["id"], "ground.b1")

    assert steer.expire_undelivered(tmp_path) == 1
    future = steer.consumed_before(tmp_path, _time.time() + 60)
    assert a["id"] in future and b["id"] in future, "expired id blocks every future pump"
    assert steer.undelivered(tmp_path) == []
    assert "steering expired (lane 'ground')" in (tmp_path / "logs" / "activity.log").read_text()

    # idempotent: a second sweep (crash-then-rerun path) finds nothing left to expire
    assert steer.expire_undelivered(tmp_path) == 0


def test_retraction_after_pump_spawn_is_honored(tmp_path):
    """Field incident 2026-08-21: pumps read the consumed ledger ONCE at spawn, so a retraction
    landing after spawn was invisible — the pump announced skips for (and, on a matching lane,
    would have DELIVERED) a message the operator already took back. _deliver must re-check the
    ledger for candidates its snapshot calls unconsumed."""
    import io

    rec = steer.post(tmp_path, "retract me mid-flight", lane="ground")

    class _Ctx:
        run_dir = tmp_path

    class _Proc:
        stdin = io.StringIO()
        def poll(self): return None

    pump = steer.Pump(_Proc(), tmp_path / "logs" / "x.log", _Ctx(), "ground.b1",
                      epoch=time.time())          # spawned BEFORE the retraction
    steer.mark_consumed(tmp_path, rec["id"], "retracted")  # operator retracts after spawn
    pump._deliver()
    assert rec["id"] not in pump.delivered, "a retracted steer must never deliver"
    assert "HUMAN STEERING" not in _Proc.stdin.getvalue()


def test_expiry_records_a_steering_expired_decision(tmp_path):
    """A steer's terminal fate is a signal: delivered/skipped already reach the trace as
    decisions — expiry must too. With a ctx, the TTL sweep records steering_expired per record
    (decisions.jsonl is the durable half; the Langfuse event rides the same call)."""
    steer.post(tmp_path, "never delivered", lane="ground")
    assert steer.expire_undelivered(tmp_path, _ctx(tmp_path)) == 1
    entries = [json.loads(ln) for ln in
               (tmp_path / "logs" / "decisions.jsonl").read_text().splitlines()]
    exp = [e for e in entries if e["decision"] == "steering_expired"]
    assert exp and exp[0]["lane"] == "ground" and exp[0]["chars"] == len("never delivered")
