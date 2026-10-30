"""rfc_agent.run_metrics: numbers read from a run folder — input size, tokens, coverage counts, context layer."""

from __future__ import annotations

import json
from pathlib import Path

from rfc_agent import run_facts, run_metrics

RFC = """# RFC
## 3. Options

| Option | Idea | Pros | Cons |
|---|---|---|---|
| A | extend the API | small | coupling |
| B | new service | clean | cost |

## Recommendation: A
## 4. Design
"""


def _w(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _stream(path: Path, *events: dict) -> None:
    _w(path, "\n".join(json.dumps(e) for e in events) + "\n")


def _tool(i: str, name: str) -> dict:
    return {"type": "tool_use", "id": i, "name": name, "input": {}}


def _result(i: str, content, error: bool = False) -> dict:
    return {"type": "tool_result", "tool_use_id": i, "content": content, "is_error": error}


def _run(tmp_path: Path) -> Path:
    _w(tmp_path / "raw" / "prd.md", "word " * 400)
    _w(
        tmp_path / "scope.json",
        json.dumps(
            {
                "timings_s": {"confirm_wait": 30.0, "total": 90.0},
                "repos": [{"repo": "api", "commit_date": "2020-01-01T00:00:00+00:00"}],
                "figma": [
                    {
                        "sections": [
                            {"name": "a", "frames": 5, "include": True},
                            {"name": "b", "frames": 3, "include": False},
                        ]
                    }
                ],
            }
        ),
    )
    _w(
        tmp_path / "work" / "flows.json",
        json.dumps(
            {"flows": [{"id": "F1", "trigger": {"screens": ["Home"]}}, {"id": "F2", "trigger": {"screens": []}}]}
        ),
    )
    _w(
        tmp_path / "work" / "reqs.json",
        json.dumps({"requirements": [{"id": "REQ-1"}, {"id": "REQ-2"}, {"id": "REQ-3"}]}),
    )
    _w(tmp_path / "work" / "open_questions.json", json.dumps({"questions": [{"id": "Q1"}]}))
    _w(
        tmp_path / "logs" / "draft.json",
        json.dumps({"statuses": {"covered": 2, "assumes": 1, "deferred": 0, "open": 0}}),
    )
    _w(
        tmp_path / "logs" / "investigate.json",
        json.dumps({"pause_wait_s": {"pause1_wait_s": 20.0, "pause2_wait_s": None}}),
    )
    _w(tmp_path / "logs" / "review.json", json.dumps({"triage_wait_s": 12.5}))
    _w(tmp_path / "out" / "rfc.md", RFC)
    usage = {"input_tokens": 10, "cache_read_input_tokens": 1000, "cache_creation_input_tokens": 50}
    _stream(
        tmp_path / "logs" / "investigate.lead.1a.stream.jsonl",
        {"type": "system", "subtype": "init", "model": "claude-opus-5-5"},
        {
            "type": "assistant",
            "message": {
                "usage": usage,
                "content": [
                    _tool("t1", "mcp__fleetlens__find_symbol"),
                    _tool("t2", "mcp__fleetlens__get_source"),
                    _tool("t3", "mcp__fleetlens__get_callers"),
                    _tool("t4", "Read"),
                ],
            },
        },
        {
            "type": "user",
            "message": {
                "content": [
                    _result("t1", [{"type": "text", "text": "found"}]),
                    _result("t2", "boom", error=True),
                    _result("t3", "[]"),
                    _result("t4", "[]"),  # not the context layer: ignored
                ]
            },
        },
        {"type": "system", "subtype": "compact_boundary"},
        {"type": "rate_limit_event", "rate_limit_info": {"status": "allowed"}},  # informational: not a hit
        {"type": "rate_limit_event", "rate_limit_info": {"status": "rejected"}},
        {
            "type": "result",
            "usage": {
                "input_tokens": 20,
                "output_tokens": 30,
                "cache_read_input_tokens": 1000,
                "cache_creation_input_tokens": 50,
            },
            "num_turns": 4,
        },
    )
    _stream(
        tmp_path / "logs" / "draft.lead.write.stream.jsonl",
        {"type": "system", "subtype": "init", "model": "claude-sonnet-5-5"},
        {
            "type": "assistant",
            "message": {"usage": {"input_tokens": 5, "cache_read_input_tokens": 3000}, "content": []},
        },
        {
            "type": "result",
            "usage": {"input_tokens": 5, "output_tokens": 10, "cache_read_input_tokens": 3000},
            "num_turns": 2,
        },
    )
    return tmp_path


def test_collect_reads_every_source(tmp_path):
    m = run_metrics.collect(_run(tmp_path))
    assert m["prd_words"] == 400 and m["prd_tokens_est"] == len("word " * 400) // 4
    assert (m["figma_sections"], m["figma_frames"]) == (2, 8)
    assert (m["figma_sections_skipped"], m["figma_frames_in_scope"]) == (1, 5)
    assert m["snapshot_age_days"] > 365
    assert (m["flows"], m["flows_without_screens"], m["reqs"], m["open_questions"]) == (2, 1, 3, 1)
    assert (m["req_covered"], m["req_assumes"], m["req_deferred"], m["req_open"]) == (2, 1, 0, 0)
    assert m["alternatives"] == 2
    assert m["operator_wait_s"] == 62.5 and m["triage_wait_s"] == 12.5  # confirm 30 + pause1 20 + triage 12.5


def test_collect_sums_agent_streams(tmp_path):
    m = run_metrics.collect(_run(tmp_path))
    assert m["agent_runs"] == 2 and m["agent_turns"] == 6
    assert (m["tokens_input"], m["tokens_output"]) == (25, 40)
    assert (m["tokens_cache_read"], m["tokens_cache_write"]) == (4000, 50)
    assert m["tokens_total"] == 25 + 40 + 4000 + 50
    assert m["cache_hit_ratio"] == round(4000 / (25 + 4000 + 50), 3)
    assert m["compactions"] == 1 and m["peak_context_tokens"] == 3005
    assert m["rate_limit_rejections"] == 1
    assert (m["context_calls"], m["context_errors"], m["context_empty"]) == (3, 1, 1)
    assert m["models"] == ["claude-opus-5-5", "claude-sonnet-5-5"]


def test_collect_drops_what_is_missing_and_never_raises(tmp_path):
    assert run_metrics.collect(tmp_path) == {}
    assert run_metrics.collect(tmp_path / "nope") == {}
    _w(tmp_path / "scope.json", "{not json")
    _w(tmp_path / "logs" / "bad.stream.jsonl", "garbage\n[1]\n")
    assert (
        run_metrics.collect(tmp_path)["agent_runs"] == 1
    )  # an unreadable stream still counts as a run, with no tokens


def test_run_facts_include_the_metrics(tmp_path):
    facts = run_facts.collect(_run(tmp_path))
    assert facts["tokens_total"] and facts["prd_words"] == 400 and facts["req_covered"] == 2


def test_one_bad_source_does_not_drop_the_rest(tmp_path):
    _w(tmp_path / "raw" / "prd.md", "word " * 10)
    # an index-only repo has no commit_date, and a figma entry is not a dict: neither may hide the PRD size
    _w(
        tmp_path / "scope.json",
        json.dumps({"repos": [{"repo": "api", "source": "context-index-only"}], "figma": ["x", None]}),
    )
    _w(tmp_path / "work" / "flows.json", "{not json")
    m = run_metrics.collect(tmp_path)
    assert m["prd_words"] == 10 and "snapshot_age_days" not in m


def test_scope_proposals_against_what_was_kept(tmp_path):
    items = (
        [{"kind": "repo", "include": True}] * 5
        + [{"kind": "repo", "include": False}] * 2
        + [{"kind": "page", "include": True}]
    )
    _w(tmp_path / "confirm" / "proposal.json", json.dumps({"items": items}))
    _w(
        tmp_path / "scope.json",
        json.dumps(
            {
                "repos": [{"repo": "a"}],
                "pages_included": ["p1"],
                "figma": [
                    {
                        "sections": [
                            {"name": "s", "frames": 4, "include": True},
                            {"name": "t", "frames": 1, "include": False},
                        ]
                    }
                ],
            }
        ),
    )
    m = run_metrics.collect(tmp_path)
    assert (m["repos_proposed"], m["repos_preticked"], m["repos_kept"]) == (7, 5, 1)
    assert (m["pages_proposed"], m["pages_kept"]) == (1, 1)
    assert (m["figma_sections"], m["figma_sections_kept"], m["figma_sections_skipped"]) == (2, 1, 1)
    assert "repos_kept" not in run_metrics.collect(_empty(tmp_path / "no-proposal"))  # no proposal: nothing to compare


def _empty(p):
    p.mkdir()
    _w(p / "scope.json", json.dumps({"repos": [{"repo": "a"}]}))
    return p


def test_readable_copy_length_against_the_full_rfc(tmp_path):
    _w(tmp_path / "out" / "rfc.md", "x" * 1000)
    _w(tmp_path / "out" / "rfc.readable.md", "y" * 250)
    m = run_metrics.collect(tmp_path)
    assert (m["readable_chars"], m["readable_ratio"]) == (250, 0.25)


def test_review_and_revise_from_the_findings_final_state(tmp_path):
    status = {
        "R-X.1": {"status": "fixed", "severity": "BLOCKER", "round": 1},
        "R-F1.1": {"status": "fixed", "severity": "MAJOR", "round": 2},
        "R-F1.2": {"status": "ignored", "severity": "MAJOR", "round": None},
        "R-F1.3": {"status": "minor_noted", "severity": "MINOR"},
        "R-F2": {"status": "not_reviewed", "severity": "—"},
    }
    _w(tmp_path / "review" / "status.json", json.dumps(status))
    _w(tmp_path / "review" / "rfc.round1.diff", "--- a\n+++ b\n@@\n-old\n+new\n+more\n context\n")
    _w(tmp_path / "review" / "rfc.round2.diff", "--- a\n+++ b\n-gone\n")
    m = run_metrics.collect(tmp_path)
    assert (m["review_blockers"], m["review_majors"], m["review_minors"]) == (1, 2, 1)
    assert (m["review_fixed"], m["review_ignored"], m["revise_rounds"]) == (2, 1, 2)
    assert m["revise_lines_changed"] == 4  # -old +new +more -gone; headers and context are not changes
