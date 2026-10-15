"""Draft's gate: each check, and --check-only runs with a fake `claude` (fixed → clean; unfixable → known issues)."""

from __future__ import annotations

import json
from pathlib import Path

from rfc_agent.draft import gate as checks

FLOWS = {"flows": [{"id": "F1", "name": "Create patient"}, {"id": "F2", "name": "Edit patient"}]}
REQS = {"requirements": [{"id": "REQ-1", "text": "nudge"}]}

GOOD = """# RFC: Patients
> TL;DR — One. Two.
## 1. Context & problem
x
## 2. Requirements coverage
t
## 3. Options
| A | B |
## Recommendation: A
- because
## 4. Design
### 4.1 F1 Create patient
```mermaid
sequenceDiagram
  participant M as mweb
  participant T as torpedo
  M->>T: POST /patients
  alt similar found
    T-->>M: {"action": "nudge"}
  else none
    T-->>M: {"patient": {}}
  end
```
**Current state**
- create merges same-name patients (svc/app/patient.py:1)
**Change**
1. x
### 4.2 F2 Edit patient
**Current state**
- edit returns wrapped (svc/app/patient.py:2)
## 5. API contract
c
## 6. Implementation plan
### CHANGE-1 svc: gate [REQ-1]
- repo: svc
- files: app/patient.py:1 (modify) · tests/test_gate.py (new)
- depends on: —
### CHANGE-2 svc: edit [REQ-1]
- repo: svc
- files: app/patient.py (modify)
- depends on: CHANGE-1
## 7. Rollout, monitoring, risks
r
## 8. Open questions
| Q1 | who owns the BFF? |
"""


def _snap(run: Path):
    d = run / "_snap" / "svc" / "app"
    d.mkdir(parents=True)
    (d / "patient.py").write_text("def create():\n    return {'patient': 1}\n")


def _scope(run: Path):
    return {"repos": [{"repo": "svc", "path": str(run / "_snap" / "svc"), "sha": "abc", "branch": "master",
                       "source": "snapshot"}]}


def test_good_rfc_has_no_must_fix(tmp_path):
    _snap(tmp_path)
    cov = {"REQ-1": {"status": "covered", "sections": ["4.1"]}}
    res = checks.run_all(tmp_path, GOOD, cov, "", REQS, FLOWS, _scope(tmp_path), "0–100")
    assert res["must_fix"] == {}, res["must_fix"]
    assert res["counts"] == {"citations": 2, "diagrams": 1, "changes": 2}


def test_structure():
    bad = GOOD.replace("## 5. API contract", "## Five").replace("### 4.2 F2 Edit patient", "### 4.2 Something")
    errs = "\n".join(checks.structure(bad, FLOWS))
    assert "'## 5. API contract'" in errs and "flow F2" in errs


def test_change_blocks(tmp_path):
    _snap(tmp_path)
    repos = {r["repo"]: r for r in _scope(tmp_path)["repos"]}
    bad = (GOOD.replace("app/patient.py:1 (modify)", "app/gone.py:1 (modify)")
           .replace("tests/test_gate.py (new)", "app/patient.py (new)")
           .replace("- depends on: —", "- depends on: CHANGE-2 · CHANGE-9"))
    errs = "\n".join(checks.change_blocks(bad, tmp_path, repos))
    assert "gone.py is marked (modify) but does not exist" in errs
    assert "app/patient.py is marked (new) but already exists" in errs
    assert "depends on CHANGE-9, which does not exist" in errs and "cycle" in errs


def test_secrets_skip_placeholders():
    text = ('token = "abcd1234efgh5678ijkl"\napi_key: "<your-key>"\nAKIAABCDEFGHIJKLMNOP\n'
            "postgres://admin:hunter2@db.internal/x\npassword = 'changeme'\n")
    errs = checks.secrets(text)
    assert len(errs) == 3 and all("line" in e for e in errs)


def test_mermaid_lint():
    bad = GOOD.replace("sequenceDiagram", "flowchart TD", 1)
    assert "must start with 'sequenceDiagram'" in checks.mermaid(bad)[0][0]
    broken = GOOD.replace("  M->>T: POST /patients", "  M -> > T POST\n  X->>T: hi").replace("  end\n```", "```")
    errs = "\n".join(checks.mermaid(broken)[0])
    assert "malformed message" in errs and "not closed with 'end'" in errs and "not declared: X" in errs
    assert "never closed" in checks.mermaid("```mermaid\nsequenceDiagram\n")[0][0]


def test_flags():
    flags = checks.id_refs(GOOD + "\nsee REQ-7 and Q4", REQS)
    assert any("REQ-7" in f for f in flags)
    unc = checks.uncited_current_state(GOOD.replace("(svc/app/patient.py:2)", ""))
    assert unc and "4.2" in unc[0]
    assert checks.size_and_tldr("> TL;DR — A. B. C. D.\n\n", "25–40")[1].startswith("TL;DR has 4")


FAKE = r'''#!/usr/bin/env python3
import json, os, sys, uuid
prompt = sys.stdin.read(); args = sys.argv[1:]; cwd = os.getcwd()
sid = args[args.index("--resume") + 1] if "--resume" in args else str(uuid.uuid4())
open(os.environ["FAKE_LOG"], "a").write("call resume=" + str("--resume" in args) + "\n")
print(json.dumps({"type": "system", "subtype": "init", "session_id": sid, "mcp_servers": []}))
if "The gate found these problems" in prompt and os.environ.get("FAKE_FIXES") == "1":
    p = os.path.join(cwd, "out", "rfc.md"); t = open(p).read()
    t = t.replace("svc/app/patient.py:99", "svc/app/patient.py:1").replace("flowchart TD", "sequenceDiagram")
    open(p, "w").write(t)
print(json.dumps({"type": "result", "subtype": "success", "session_id": sid, "total_cost_usd": 0.03, "num_turns": 2}))
'''


def _run(tmp_path: Path, monkeypatch, fixes: bool):
    run = tmp_path / "run"
    for d in ("work", "logs", "out"):
        (run / d).mkdir(parents=True)
    _snap(run)
    (run / "work" / "reqs.json").write_text(json.dumps(REQS))
    (run / "work" / "understanding.md").write_text("## Cross-flow constraints\n")
    (run / "work" / "flows.json").write_text(json.dumps(FLOWS))
    (run / "scope.json").write_text(json.dumps(_scope(run)))
    (run / "logs" / "lead.session").write_text("warm-lead")
    bad = GOOD.replace("(svc/app/patient.py:1)", "(svc/app/patient.py:99)")
    bad = bad.replace("sequenceDiagram", "flowchart TD", 1)
    (run / "out" / "rfc.md").write_text(bad)
    (run / "out" / "coverage.json").write_text(json.dumps({"REQ-1": {"status": "covered", "sections": ["4.1"]}}))
    fake = tmp_path / "claude"
    fake.write_text(FAKE)
    fake.chmod(0o755)
    log = tmp_path / "calls.log"

    class Cfg:
        def get(self, k, d=None):
            return {"AGENT.CLAUDE_BIN": str(fake), "DRAFT": {"SIZE_TARGET_KB": "0–100"}}.get(k, d)

    monkeypatch.setenv("FAKE_LOG", str(log))
    monkeypatch.setenv("FAKE_FIXES", "1" if fixes else "0")
    monkeypatch.setattr("rfc_agent.draft.cli.get_config", lambda: Cfg())
    from rfc_agent.draft import cli

    assert cli.main(["--run-dir", str(run), "--check-only"]) == 0
    return run, log


def test_gate_fix_round_makes_it_clean(tmp_path, monkeypatch):
    run, log = _run(tmp_path, monkeypatch, fixes=True)
    out = json.loads((run / "out" / "check.json").read_text())
    assert out["status"] == "fixed" and out["known_issues"] == {}
    assert set(out["must_fix_before_fix"]) == {"citations", "mermaid"}
    assert log.read_text().splitlines() == ["call resume=True"]  # one round, warm lead
    assert "## References" in (run / "out" / "rfc.md").read_text()  # re-rendered after the fix


def test_gate_leftovers_become_known_issues_once(tmp_path, monkeypatch):
    run, log = _run(tmp_path, monkeypatch, fixes=False)
    out = json.loads((run / "out" / "check.json").read_text())
    assert out["status"] == "known_issues" and set(out["known_issues"]) == {"citations", "mermaid"}
    from rfc_agent.draft import cli

    # rerun: RFC unchanged since the fix round → no new round
    assert cli.main(["--run-dir", str(run), "--check-only"]) == 0
    assert len(log.read_text().splitlines()) == 1


def test_check_only_needs_an_rfc(tmp_path):
    from rfc_agent.draft import cli

    (tmp_path / "work").mkdir()
    (tmp_path / "work" / "understanding.md").write_text("x")
    (tmp_path / "work" / "reqs.json").write_text("{}")
    assert cli.main(["--run-dir", str(tmp_path), "--check-only"]) == 1


def test_test_guard_blocks_the_real_claude(tmp_path):
    import pytest

    from rfc_agent.investigate import agent as ag

    spec = ag.AgentSpec("x", "m", None, ("Read",), ())
    with pytest.raises(RuntimeError, match="test guard"):
        ag.run(spec, "hi", tmp_path, None)
