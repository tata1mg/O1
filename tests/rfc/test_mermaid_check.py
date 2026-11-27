"""Mermaid diagrams in the RFC: parse, repair what is mechanical, show the rest as code (issue #18)."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from rfc_agent import observe
from rfc_agent.draft import gate
from rfc_agent.draft import mermaid_check as mc

BAD_18 = """sequenceDiagram
  participant DX
  participant SG
  DX->>SG: POST /x; resolve report key
  DX->>SG: POST /"""

RFC = f"""# RFC

```mermaid
{BAD_18}
```

between

```mermaid
sequenceDiagram
  A->>B: ok
```
"""


def fake_parser(monkeypatch, bad=lambda code: ";" in code and "#59;" not in code or "BROKEN" in code):
    """A stand-in for Mermaid: a block fails when `bad(code)` says so."""
    monkeypatch.setattr(mc, "parse_all", lambda codes: [{"ok": not bad(c), "error": "Parse error on line 4: nope"}
                                                        for c in codes])
    monkeypatch.setattr(mc, "parser_label", lambda: "mermaid@test")


def test_blocks_are_found_with_their_lines():
    bs = mc.blocks(RFC)
    assert [(b.index, b.line) for b in bs] == [(1, 3), (2, 13)]
    assert bs[0].code == BAD_18
    assert mc.blocks("```mermaid\nsequenceDiagram\n") == []  # never closed: the lint reports it


def test_semicolons_in_text_are_escaped_and_nothing_else_is_touched():
    code = ("sequenceDiagram\n  A->>B: one; two\n  Note over A,B: n1; n2\n  alt ok; or not\n"
            "  B-->>A: done #59; already\n  end")
    out = mc.repair(code)
    assert "A->>B: one#59; two" in out and "Note over A,B: n1#59; n2" in out and "alt ok#59; or not" in out
    assert "done #59; already" in out and out.count("#59;") == 4  # an existing escape is left alone
    assert mc.repair(out) == out  # idempotent
    flow = "flowchart TD\n  A[x; y] --> B"
    assert mc.repair(flow) == flow  # only sequence diagrams are repaired


def test_a_repairable_diagram_is_fixed_in_the_file(tmp_path, monkeypatch):
    fake_parser(monkeypatch)
    p = tmp_path / "rfc.md"
    p.write_text(RFC)
    rep = mc.repair_file(p)
    assert rep["total"] == 2 and rep["repaired"] == 1 and rep["failed_after_repair"] == []
    assert [f["line"] for f in rep["failed_initial"]] == [3]
    assert "POST /x#59; resolve report key" in p.read_text()
    assert mc.quarantine(p) == []  # nothing left to show as code


def test_what_still_fails_is_shown_as_code_under_a_note(tmp_path, monkeypatch):
    fake_parser(monkeypatch)
    p = tmp_path / "rfc.md"
    p.write_text(RFC.replace("A->>B: ok", "A->>B: BROKEN"))
    rep = mc.repair_file(p)
    assert rep["repaired"] == 1 and [f["index"] for f in rep["failed_after_repair"]] == [2]
    shown = mc.quarantine(p)
    assert [f["index"] for f in shown] == [2]
    out = p.read_text()
    assert out.count("```mermaid") == 1  # the good one stays a diagram
    assert mc.QUARANTINE_MARK in out and "mermaid.live" in out and "```text\nsequenceDiagram\n  A->>B: BROKEN" in out
    assert len(mc.quarantined(out)) == 1
    assert gate.mermaid(out)[0] and "shown as plain code" in "\n".join(gate.mermaid(out)[0])  # still a known issue


def test_a_marker_without_its_code_block_is_not_counted():
    assert mc.quarantined(f"{mc.QUARANTINE_MARK}\nsome other text\n") == []


def test_without_a_parser_the_semicolon_repair_still_runs(tmp_path):
    p = tmp_path / "rfc.md"
    p.write_text(RFC)
    rep = mc.repair_file(p)  # the suite turns the parser off
    assert rep["parser"].startswith("not parser-checked") and rep["repaired"] == 1
    assert "#59;" in p.read_text() and mc.quarantine(p) == []


def test_the_gate_names_a_bare_semicolon_even_without_the_parser():
    errs, n = gate.mermaid(RFC)
    assert n == 2 and any("a ';' in message text" in e and "#59;" in e for e in errs)


def test_the_gate_reports_what_the_parser_says(monkeypatch):
    monkeypatch.setattr(mc, "failures", lambda rfc: [{"index": 1, "line": 3, "error": "Parse error on line 4: x"}])
    errs, _ = gate.mermaid("```mermaid\nsequenceDiagram\n  A->>B: hi\n```\n")
    assert errs == ["diagram at line 3 does not render — Mermaid says: Parse error on line 4: x"]


def test_draft_check_only_repairs_and_records_the_diagram_stats(tmp_path, monkeypatch):
    run = tmp_path / "run"
    for d in ("work", "logs", "out"):
        (run / d).mkdir(parents=True)
    (run / "work" / "reqs.json").write_text(json.dumps({"granularity": "fine", "requirements": []}))
    (run / "work" / "understanding.md").write_text("x\n")
    (run / "scope.json").write_text(json.dumps({"prd": "https://x/pages/1", "repos": []}))
    (run / "out" / "rfc.md").write_text(RFC)
    (run / "logs" / "lead.session").write_text("lead-session")
    fake = tmp_path / "claude"  # the gate's one fix round asks the lead; this one answers and changes nothing
    fake.write_text("#!/usr/bin/env python3\nimport json, sys\nsys.stdin.read()\n"
                    "print(json.dumps({'type': 'system', 'subtype': 'init', 'session_id': 's', 'mcp_servers': []}))\n"
                    "print(json.dumps({'type': 'result', 'subtype': 'success', 'session_id': 's', "
                    "'total_cost_usd': 0.01, 'num_turns': 1}))\n")
    fake.chmod(0o755)

    class Cfg:
        def get(self, k, d=None):
            return {"AGENT.CLAUDE_BIN": str(fake)}.get(k, d)

    monkeypatch.setattr("rfc_agent.draft.cli.get_config", lambda: Cfg())
    seen = []
    monkeypatch.setattr(observe, "mermaid", lambda stats, **kw: seen.append((stats, kw)))
    from rfc_agent.draft import cli

    cli.main(["--run-dir", str(run), "--check-only"])
    assert "POST /x#59; resolve" in (run / "out" / "rfc.md").read_text()
    stats = json.loads((run / "out" / "check.json").read_text())["mermaid"]
    assert stats["total"] == 2 and stats["repaired_automatically"] == 1 and stats["shown_as_code"] == 0
    assert stats["parser"].startswith("not parser-checked")
    assert seen and seen[0][1] == {"stage": "draft", "round_label": "check-only"}


def test_langfuse_gets_counts_and_reasons(monkeypatch):
    scores, updates = {}, {}

    class Ev:
        def update(self, **kw):
            updates.update(kw)

        def score(self, name, value, **kw):
            scores[name] = value

    monkeypatch.setattr(observe, "active", lambda: True)
    monkeypatch.setattr(observe, "_child", lambda kind, name, **kw: Ev())
    monkeypatch.setattr(observe, "_end", lambda ev: None)
    observe.mermaid({"total": 5, "failed_first_parse": 2, "repaired_automatically": 1, "fixed_by_lead": 0,
                     "shown_as_code": 1, "parser": "mermaid@x", "reasons": [{"line": 9, "error": "Parse error"}]},
                    stage="draft")
    assert scores == {"mermaid_diagrams": 5.0, "mermaid_failed_first_parse": 2.0, "mermaid_repaired_automatically": 1.0,
                      "mermaid_fixed_by_lead": 0.0, "mermaid_shown_as_code": 1.0, "mermaid_failed": 1.0}
    assert updates["level"] == "ERROR" and updates["output"]["reasons"][0]["line"] == 9
    scores.clear()
    observe.mermaid({"total": 3, "failed_first_parse": 1, "repaired_automatically": 1, "fixed_by_lead": 0,
                     "shown_as_code": 0, "parser": "mermaid@x", "reasons": []}, stage="draft")
    assert scores["mermaid_failed"] == 0.0 and scores["mermaid_shown_as_code"] == 0.0  # repaired: not a failure


REAL = Path("~/.cache/rfc-agent/mermaid-check/node_modules/mermaid").expanduser()


@pytest.mark.skipif(shutil.which("node") is None or not REAL.is_dir(),
                    reason="the Mermaid parser is not installed (it installs itself on a real run)")
def test_the_real_parser_catches_issue_18_and_the_repair_fixes_it(monkeypatch, tmp_path):
    monkeypatch.setenv("RFC_AGENT_MERMAID_CHECK", "on")
    mc._runtime.clear()
    res = mc.parse_all([BAD_18, mc.repair(BAD_18), "flowchart TD\n  A[Start --> B"])
    assert [r["ok"] for r in res] == [False, True, False]
    assert "Parse error on line 4" in res[0]["error"]
    p = tmp_path / "rfc.md"
    p.write_text(RFC)
    rep = mc.repair_file(p)
    assert rep["parser"].startswith("mermaid@") and rep["repaired"] == 1 and rep["failed_after_repair"] == []
    mc._runtime.clear()
