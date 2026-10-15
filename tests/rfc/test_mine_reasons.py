"""scripts/mine_reasons.py pure helpers — the analysis passes over the why-layer (0.24.53).
Network fetch is not tested; the helpers take plain observation dicts."""

import importlib.util
import sys
from pathlib import Path

_spec = importlib.util.spec_from_file_location(
    "mine_reasons", Path(__file__).parents[2] / "scripts" / "mine_reasons.py")
mine = importlib.util.module_from_spec(_spec)
sys.modules["mine_reasons"] = mine
_spec.loader.exec_module(mine)


def _tool(oid, parent, name, fp, reason=None, scope=None, stage="generate", attempt=2):
    md = {"stage": stage, "attempt": attempt}
    if reason:
        md["reason"], md["reason_scope"] = reason, scope
    return {"id": oid, "parentObservationId": parent, "name": name,
            "input": {"file_path": fp}, "metadata": md}


def test_lane_resolution_falls_back_to_stage_metadata():
    agent = {"id": "a1", "name": "agent:generate#2", "metadata": {}}
    byid = {"a1": agent}
    assert mine.lane_of(_tool("t1", "a1", "tool:Read", "x.py"), byid) == "agent:generate#2"
    # in-flight parent (span not exported yet) -> metadata fallback
    assert mine.lane_of(_tool("t2", "missing", "tool:Read", "x.py"), byid) == "agent:generate#2"


def test_reason_coverage_and_scopes():
    tools = [_tool("t1", None, "tool:Read", "a.py", "why", "same_message"),
             _tool("t2", None, "tool:Bash", "b.py", "plan", "carried"),
             _tool("t3", None, "tool:Read", "c.py")]
    withr, total, scopes = mine.reason_coverage(tools)
    assert (withr, total) == (2, 3)
    assert scopes == {"same_message": 1, "carried": 1}


def test_knowing_doing_flags_only_cross_file_same_message_ops():
    byid = {}
    tools = [
        # reason names the file it touches -> agreement, no flag
        _tool("t1", None, "tool:Read", "/r/cart.py", "check cart.py guard", "same_message"),
        # reason names a DIFFERENT file -> flagged (reading list, not verdict)
        _tool("t2", None, "tool:Read", "/r/reducer.js", "verify action.js dispatch", "same_message"),
        # carried scope is excluded — the plan governs many files legitimately
        _tool("t3", None, "tool:Read", "/r/other.js", "verify action.js dispatch", "carried"),
        # reason with no file names -> nothing to compare
        _tool("t4", None, "tool:Edit", "/r/plan.md", "tighten wording", "same_message"),
    ]
    flags = mine.knowing_doing_flags(tools, byid)
    assert len(flags) == 1 and flags[0][2] == "reducer.js" and flags[0][3] == ["action.js"]


def test_uncertainty_table_counts_reasons_and_digest_blocks():
    obs = [
        _tool("t1", None, "tool:Read", "a.py", "not sure this is the caller", "same_message"),
        _tool("t2", None, "tool:Read", "b.py", "confirmed the pattern", "same_message"),
        {"id": "g1", "name": "llm:generate#2",
         "metadata": {"thinking_digest": "[10:00:01] couldn't find the widget\n---\n[10:02:09] ship it"}},
    ]
    per = mine.uncertainty_table(obs, {})
    assert per["agent:generate#2"] == [1, 2]
    assert per["llm:generate#2"] == [1, 2]


def test_anchor_sweep_finds_reasons_digests_and_gate_outputs():
    obs = [
        _tool("t1", None, "tool:Read", "a.py", "the svc_c proxy pattern exists", "same_message"),
        {"id": "g1", "name": "llm:generate#2",
         "metadata": {"thinking_digest": "[10:00:01] no proxy needed here"}},
        {"id": "gate", "name": "gate:review", "metadata": {},
         "output": ["review BLOCKER: misses the svc_c proxy pattern"]},
    ]
    hits = mine.anchor_hits(obs, {}, "proxy")
    assert {k for _, k, _ in hits} == {"reason", "deliberation", "gate output"}
    # never-saw shape: anchor only in the gate output
    hits2 = mine.anchor_hits([obs[2]], {}, "proxy")
    assert all(k == "gate output" for _, k, _ in hits2)
