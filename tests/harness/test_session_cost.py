"""A resumed Claude session reports the whole session's cost so far; each call must be booked only what it added.

Measured on the order-filters comparison (claude-1): reviewer R-F4 reported $1.83 in Review, then $2.44 and
$2.56 at its two Revise confirms, all in one session. Its real cost is $2.56; the run used to book $6.83."""

from __future__ import annotations

import json
import threading

from harness.session_cost import charge, ledger_path


def test_a_resumed_session_is_charged_only_its_increase(tmp_path):
    assert charge(tmp_path, "rf4", 1.83) == 1.83
    assert charge(tmp_path, "rf4", 2.44, resumed="rf4") == 0.61
    assert charge(tmp_path, "rf4", 2.56, resumed="rf4") == 0.12
    assert json.loads(ledger_path(tmp_path).read_text()) == {"rf4": 2.56}


def test_fresh_calls_and_unknown_resumes_are_charged_in_full(tmp_path):
    assert charge(tmp_path, "a", 1.0) == 1.0
    assert charge(tmp_path, "b", 2.0) == 2.0  # another session: nothing to subtract
    assert charge(tmp_path, "c", 3.0, resumed="never-seen") == 3.0
    assert charge(tmp_path, "a", None, resumed="a") is None  # no cost reported stays unknown


def test_a_resume_that_gets_a_new_session_id_still_finds_the_old_total(tmp_path):
    charge(tmp_path, "old", 1.0)
    assert charge(tmp_path, "new", 1.5, resumed="old") == 0.5
    assert charge(tmp_path, "new", 2.0, resumed="new") == 0.5


def test_a_lower_total_means_the_counter_restarted(tmp_path):
    charge(tmp_path, "s", 5.0)
    assert charge(tmp_path, "s", 0.4, resumed="s") == 0.4


def test_parallel_reviewers_do_not_lose_each_others_totals(tmp_path):
    ts = [threading.Thread(target=charge, args=(tmp_path, f"r{i}", float(i))) for i in range(20)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert len(json.loads(ledger_path(tmp_path).read_text())) == 20


FAKE = r'''#!/usr/bin/env python3
import json, os, sys
sys.stdin.read()
args = sys.argv[1:]
sid = args[args.index("--resume") + 1] if "--resume" in args else "S1"
n = int(open(os.environ["FAKE_N"]).read()) if os.path.exists(os.environ["FAKE_N"]) else 0
open(os.environ["FAKE_N"], "w").write(str(n + 1))
total = [1.83, 2.44, 2.56][n]
print(json.dumps({"type": "system", "subtype": "init", "session_id": sid, "mcp_servers": []}))
print(json.dumps({"type": "result", "subtype": "success", "session_id": sid, "total_cost_usd": total,
                  "num_turns": 1, "result": "ok"}))
'''


def test_agent_run_books_each_resume_at_its_own_cost(tmp_path, monkeypatch):
    from rfc_agent.investigate import agent

    fake = tmp_path / "claude"
    fake.write_text(FAKE)
    fake.chmod(0o755)
    monkeypatch.setenv("FAKE_N", str(tmp_path / "n"))

    class Cfg:
        def get(self, k, d=None):
            return {"AGENT.CLAUDE_BIN": str(fake)}.get(k, d)

    run = tmp_path / "run"
    run.mkdir()
    spec = agent.AgentSpec(label="R-F4", model="m", effort=None, builtin_tools=("Read",), write_allow=(),
                           use_context=False)
    first = agent.run(spec, "review", run, Cfg(), stage="review")
    assert first.ok and first.cost_usd == 1.83
    costs = [agent.run(spec, "confirm", run, Cfg(), resume=first.session_id, stage="revise").cost_usd
             for _ in range(2)]
    assert costs == [0.61, 0.12]
    assert round(first.cost_usd + sum(costs), 2) == 2.56


def test_atlassian_session_turns_are_charged_their_own_share():
    from rfc_agent.scope.atlassian import ConnectorSession

    s = ConnectorSession.__new__(ConnectorSession)
    s._billed = 0.0
    # totals from a real scope.atlassian stream: one open session, three turns
    assert [round(s._share(t), 4) for t in (0.0133, 0.0221, 0.0426)] == [0.0133, 0.0088, 0.0205]
