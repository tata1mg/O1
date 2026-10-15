"""Draft: coverage check and rendering, references, and an end-to-end run with a fake `claude`."""

from __future__ import annotations

import json
from pathlib import Path

from rfc_agent.draft import coverage

REQS = {"granularity": "fine", "requirements": [
    {"id": "REQ-1", "priority": "must", "text": "nudge on similar name", "parent": None},
    {"id": "REQ-2", "priority": "must", "text": "show matches", "parent": "REQ-1"},
    {"id": "REQ-3", "priority": "nice", "text": "analytics", "parent": None}]}

RFC = """# RFC: Patients
## 1. Context & problem
x
## 2. Requirements coverage
(placeholder the lead wrote)
## 4. Design
### 4.1 F1 Create patient
```mermaid
sequenceDiagram
  A->>B: hi
```
## 6. Implementation plan
### CHANGE-1 svc: gate
## 8. Open questions
| Q1 | who owns the BFF? |
"""


def test_coverage_check_rules():
    good = {"REQ-1": {"status": "covered", "sections": ["4.1"]},
            "REQ-2": {"status": "assumes", "sections": ["4.1", "CHANGE-1"], "question": "Q1"},
            "REQ-3": {"status": "deferred", "note": "phase 2"}}
    assert coverage.check(good, REQS, RFC) == []
    bad = {"REQ-1": {"status": "covered", "sections": ["9.9"]}, "REQ-2": {"status": "open", "question": "Q7"},
           "REQ-9": {"status": "maybe"}}
    errs = "\n".join(coverage.check(bad, REQS, RFC))
    for needle in ("no entry for: REQ-3", "unknown REQ ids: REQ-9", "9.9", "Q7", "status must be"):
        assert needle in errs


def test_render_and_insert_are_idempotent():
    cov = {"REQ-1": {"status": "covered", "sections": ["4.1"]},
           "REQ-2": {"status": "assumes", "sections": ["4.1"], "question": "Q1", "note": "BFF passes through"},
           "REQ-3": {"status": "open", "question": "Q1"}}
    table = coverage.render_table(cov, REQS)
    assert "| ↳ REQ-2 |" in table and "🟡 Covered, assumes Q1 (§4.1) — BFF passes through" in table
    assert "❓ Open — Q1" in table and "3 requirements (fine)" in table
    once = coverage.insert_table(RFC, table)
    assert "(placeholder the lead wrote)" not in once and "## 4. Design" in once
    assert coverage.insert_table(once, table) == once
    scope = {"prd": "https://x/pages/1", "repos": [{"repo": "svc", "branch": "master", "sha": "abcdef1234567",
                                                    "source": "snapshot"}]}
    refs = coverage.references(scope)
    doc = coverage.insert_references(once, refs)
    assert "| svc | master | abcdef123456 | snapshot |" in doc
    assert coverage.insert_references(doc, refs) == doc
    st = coverage.stats(doc, cov)
    assert st["design_sections"] == 1 and st["changes"] == 1 and st["mermaid_blocks"] == 1


FAKE = r'''#!/usr/bin/env python3
import json, os, sys, uuid
prompt = sys.stdin.read(); args = sys.argv[1:]; cwd = os.getcwd()
sid = args[args.index("--resume") + 1] if "--resume" in args else str(uuid.uuid4())
open(os.environ["FAKE_LOG"], "a").write(prompt[:40].replace("\n", " ") + " | resume=" + str("--resume" in args) + "\n")
def w(rel, text):
    p = os.path.join(cwd, rel); os.makedirs(os.path.dirname(p), exist_ok=True); open(p, "w").write(text)
print(json.dumps({"type": "system", "subtype": "init", "session_id": sid, "mcp_servers": []}))
if "Now write the RFC" in prompt:
    w("out/rfc.md", %RFC%)
    w("out/coverage.json", json.dumps({"REQ-1": {"status": "covered", "sections": ["4.1"]}}))
elif "The gate found these problems" in prompt:
    w("out/coverage.json", json.dumps({"REQ-1": {"status": "covered", "sections": ["4.1"]},
                                       "REQ-2": {"status": "assumes", "sections": ["4.1"], "question": "Q1"},
                                       "REQ-3": {"status": "deferred", "note": "phase 2"}}))
print(json.dumps({"type": "result", "subtype": "success", "session_id": sid, "total_cost_usd": 0.02, "num_turns": 3}))
'''


def test_draft_end_to_end_with_fake_claude(tmp_path, monkeypatch):
    run = tmp_path / "run"
    for d in ("work", "logs", "raw"):
        (run / d).mkdir(parents=True)
    (run / "work" / "reqs.json").write_text(json.dumps(REQS))
    (run / "work" / "understanding.md").write_text("## Cross-flow constraints\n")
    (run / "logs" / "lead.session").write_text("lead-session-from-investigate")
    (run / "scope.json").write_text(json.dumps({"prd": "https://x/pages/1", "repos": []}))
    fake = tmp_path / "claude"
    fake.write_text(FAKE.replace("%RFC%", repr(RFC)))
    fake.chmod(0o755)
    log = tmp_path / "calls.log"

    class Cfg:
        def get(self, k, d=None):
            return {"AGENT.CLAUDE_BIN": str(fake)}.get(k, d)

    monkeypatch.setenv("FAKE_LOG", str(log))
    monkeypatch.setattr("rfc_agent.draft.cli.get_config", lambda: Cfg())
    from rfc_agent.draft import cli

    assert cli.main(["--run-dir", str(run)]) == 0
    calls = log.read_text().splitlines()
    assert len(calls) == 2 and all("resume=True" in c for c in calls)  # warm lead, then one gate fix round
    rfc = (run / "out" / "rfc.md").read_text()
    assert "(placeholder the lead wrote)" not in rfc and "⏸ Deferred — phase 2" in rfc and "## References" in rfc
    summary = json.loads((run / "logs" / "draft.json").read_text())
    assert summary["statuses"]["covered"] == 1
    assert [r["step"] for r in summary["runs"]] == ["write", "gate-fix"]
    check = json.loads((run / "out" / "check.json").read_text())
    assert "coverage" in check["must_fix_before_fix"] and "coverage" not in check["known_issues"]
    assert check["status"] == "known_issues"  # the minimal test RFC lacks §3/§5/§7 → structure stays known
    kinds = [json.loads(x)["kind"] for x in (run / "logs" / "progress.jsonl").read_text().splitlines()]
    assert kinds == ["start", "gate", "written", "done"]
    # rerun: nothing to write again; the gate's fix round already ran for this content → no agent call
    assert cli.main(["--run-dir", str(run)]) == 0
    assert len(log.read_text().splitlines()) == 2


def test_draft_needs_investigate(tmp_path):
    from rfc_agent.draft import cli

    assert cli.main(["--run-dir", str(tmp_path)]) == 1


_ = Path
