"""scripts/mine_reasons.py pure helpers — the analysis passes over the why-layer (0.24.53).
The helpers take plain observation dicts; the fetch is tested against a fake Observations API v2."""

import importlib.util
import json
import sys
import urllib.parse
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


# ---- Observations API v2 fetch (Langfuse v4; the v1 /api/public/observations is deprecated) ----

def test_fetch_uses_observations_v2_bounded_with_long_metadata_expanded():
    url = mine.observations_url("https://jp.cloud.langfuse.com", "tr-1", "2026-10-01T00:00:00Z",
                                "2026-11-10T00:00:00Z", None)
    parsed = urllib.parse.urlparse(url)
    q = dict(urllib.parse.parse_qsl(parsed.query))
    assert parsed.path == "/api/public/v2/observations"
    assert q["traceId"] == "tr-1" and "cursor" not in q
    assert q["fromStartTime"] == "2026-10-01T00:00:00Z" and q["toStartTime"] == "2026-11-10T00:00:00Z"
    # every field the analyses read: parentObservationId (core), name (basic), io, metadata
    assert set(q["fields"].split(",")) >= {"core", "basic", "io", "metadata"}
    # v2 truncates metadata values over 200 chars unless expanded — reasons and digests are long
    assert set(q["expandMetadata"].split(",")) >= {"reason", "thinking_digest"}
    assert int(q["limit"]) <= 1000 and "page" not in q and "parseIoAsJson" not in q


def test_normalize_parses_raw_string_io_like_v1_did():
    row = mine.normalize({"id": "t1", "name": "tool:Read", "input": '{"file_path": "/r/a.py"}',
                          "output": '["review BLOCKER: x"]', "metadata": {"reason": "why"}})
    assert row["input"] == {"file_path": "/r/a.py"} and row["output"] == ["review BLOCKER: x"]
    assert mine.normalize({"id": "x", "output": "plain text"})["output"] == "plain text"
    assert mine.normalize({"id": "y"})["metadata"] == {}
    assert mine.normalize({"id": "z", "metadata": '{"stage": "draft"}'})["metadata"] == {"stage": "draft"}


def test_fetch_follows_the_cursor_until_it_is_absent(monkeypatch):
    """An empty page can still carry a cursor; only a missing cursor ends the listing."""
    pages = [
        {"data": [{"id": "a", "input": '{"file_path": "x.py"}'}], "meta": {"cursor": "c1"}},
        {"data": [], "meta": {"cursor": "c2"}},
        {"data": [{"id": "b"}], "meta": {}},
    ]
    seen = []

    class _Resp:
        def __init__(self, body): self.body = body
        def read(self): return json.dumps(self.body).encode()

    def _urlopen(req, timeout):
        seen.append(dict(urllib.parse.parse_qsl(urllib.parse.urlparse(req.full_url).query)).get("cursor"))
        assert req.get_header("Authorization").startswith("Basic ")
        return _Resp(pages[len(seen) - 1])

    class _Cfg:
        def get(self, key):
            return {"LANGFUSE.PUBLIC_KEY": "pk-lf-x", "LANGFUSE.SECRET_KEY": "sk-lf-x",
                    "LANGFUSE.HOST": "https://jp.cloud.langfuse.com"}.get(key)

    import rfc_agent.config as config_mod

    monkeypatch.setattr(config_mod, "Config", _Cfg)
    monkeypatch.setattr(mine.urllib.request, "urlopen", _urlopen)
    out = mine.fetch_observations("tr-1", "2026-10-01T00:00:00Z", "2026-11-10T00:00:00Z")
    assert seen == [None, "c1", "c2"]
    assert [o["id"] for o in out] == ["a", "b"] and out[0]["input"] == {"file_path": "x.py"}
