"""The live feed: agent ticks → logs/activity.log, one card log per agent, logs/sessions.json, and the
`rfc_agent.watch` follower behind each card."""

import io
import json
import threading
import time

from rfc_agent import watch
from rfc_agent.investigate import agent as ag
from rfc_agent.investigate.progress import Progress


def test_target_names_the_file_relative_to_the_run(tmp_path):
    assert ag.target({"file_path": f"{tmp_path}/_snap/api/app.py"}, tmp_path) == "_snap/api/app.py"
    assert ag.target({"pattern": "coupon", "path": f"{tmp_path}/_snap"}, tmp_path) == "'coupon' in _snap"
    assert ag.target({"service": "cart", "depth": 2, "opts": {"x": 1}}) == "service=cart depth=2"
    assert ag.target(None) == ""
    long = ag.target({"query": "x" * 500})
    assert len(long) == 90 and long.endswith("…")


def test_consume_sends_start_and_tool_ticks(tmp_path):
    seen = []
    res = ag.AgentResult(label="T1", ok=False, stream=f"{tmp_path}/logs/investigate.T1.run.stream.jsonl")
    tick = lambda label, kind, data: seen.append((label, kind, data))  # noqa: E731
    ag._consume(json.dumps({"type": "system", "subtype": "init", "session_id": "s-1", "model": "m"}), res, tick)
    ag._consume(
        json.dumps({"type": "assistant", "message": {"content": [
            {"type": "text", "text": "Reading the cart.\n"},
            {"type": "tool_use", "name": "Read", "input": {"file_path": f"{tmp_path}/a.py"}}]}}),
        res, tick, run_dir=tmp_path,
    )
    assert seen[0] == ("T1", "start", {"session_id": "s-1", "model": "m", "log": "investigate.T1.run"})
    assert seen[1] == ("T1", "text", {"text": "Reading the cart."})
    assert seen[2] == ("T1", "tool", {"n": 1, "name": "Read", "target": "a.py"})


def test_progress_writes_the_feed_and_the_sessions(tmp_path):
    p = Progress(tmp_path, stage="review")
    p.tick("R1", "start", {"session_id": "s-9", "model": "m", "log": "review.R1.run"})
    p.tick("R1", "text", {"text": "Checking refunds."})
    p.tick("R1", "tool", {"n": 1, "name": "Grep", "target": "'refund'"})
    p.tick("R1", "end", {"ok": True, "session_id": "s-9", "tool_calls": 1, "seconds": 4.2, "step": "run"})
    p.tick("R2", "end", {"ok": False, "error": "timed out after 900s", "session_id": None, "step": "run"})

    lines = (tmp_path / "logs" / "activity.log").read_text().splitlines()
    assert "review" in lines[0] and "R1 ▶ started · session s-9 · card review.R1.run" in lines[0]
    assert lines[1].endswith("R1 #1 Grep 'refund'")
    assert lines[2].endswith("R1 ■ done · 1 calls, 4.2s")
    assert "R2 ■ FAILED: timed out after 900s" in lines[3]

    sessions = json.loads((tmp_path / "logs" / "sessions.json").read_text())
    assert sessions == {"cwd": str(tmp_path.resolve()), "sessions": {"review.R1.run": "s-9"}}
    p.step("done")  # status.json is throttled to one write per 2 s; a step forces it
    agent = json.loads((tmp_path / "logs" / "status.json").read_text())["agents"][0]
    assert agent["last"] == "Grep 'refund'" and agent["session_id"] == "s-9"


def test_each_agent_gets_its_own_card_log(tmp_path):
    p = Progress(tmp_path, stage="review")
    p.tick("R1", "start", {"session_id": "s-9", "model": "m", "log": "review.R1.run"})
    p.tick("R2", "start", {"session_id": "s-8", "model": "m", "log": "review.R2.run"})
    p.tick("R1", "text", {"text": "Checking refunds.\nThen coupons."})
    p.tick("R1", "tool", {"n": 1, "name": "Grep", "target": "'refund'"})
    p.tick("R1", "end", {"ok": True, "session_id": "s-9", "tool_calls": 1, "seconds": 4.2, "step": "run"})

    card = (tmp_path / "logs" / "agents" / "review.R1.run.log").read_text()
    assert "▶ review · R1 started · model m · session s-9" in card
    assert "💬 Checking refunds.\n   Then coupons." in card
    assert "#1 Grep 'refund'" in card
    assert f"■ done · 1 calls, 4.2s · open it: cd {tmp_path.resolve()} && claude --resume s-9 --fork-session" in card
    assert "refund" not in (tmp_path / "logs" / "agents" / "review.R2.run.log").read_text()


def test_watch_follows_one_agent_until_it_ends(tmp_path):
    p = Progress(tmp_path, stage="draft")
    out = io.StringIO()
    def run():
        out.write(f"exit={watch.follow(tmp_path, 'draft.lead.write', out=out, poll_s=0.02)}")

    t = threading.Thread(target=run)
    t.start()
    time.sleep(0.1)  # the watcher starts before the agent's log exists
    p.tick("lead", "start", {"session_id": "s-1", "model": "m", "log": "draft.lead.write"})
    p.tick("lead", "tool", {"n": 1, "name": "Write", "target": "out/rfc.md"})
    time.sleep(0.1)
    p.tick("lead", "end", {"ok": True, "session_id": "s-1", "tool_calls": 1, "seconds": 1, "step": "write"})
    t.join(timeout=5)
    assert not t.is_alive()
    text = out.getvalue()
    assert "▶ draft · lead started" in text and "#1 Write out/rfc.md" in text and "■ done" in text
    assert text.endswith("exit=0")


def test_watch_stops_when_the_run_ends(tmp_path):
    (tmp_path / "logs").mkdir()
    out = io.StringIO()
    t = threading.Thread(target=lambda: watch.follow(tmp_path, "investigate.T9.run", out=out, poll_s=0.02))
    t.start()
    time.sleep(0.1)
    (tmp_path / "logs" / "run.json").write_text("{}")
    t.join(timeout=5)
    assert not t.is_alive() and "the run ended" in out.getvalue()
