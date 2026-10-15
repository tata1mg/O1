"""Review: finding parsing, checks, merge, and end-to-end runs with a fake `claude`."""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

from rfc_agent.review import review

REVIEW = """## Review — F1 Create patient

### BLOCKER: edit double-nests the patient
- Evidence: svc/app/patient.py:2 returns {"patient": p}; §5 says flat
- Failure scenario: rename → blank row
- Fix: set action on the existing dict

### MAJOR: svc_a not handled
- Evidence: svc/app/gone.py:9 calls it
- Fix: add svc_a to callers

### Nitpick: wording
- Evidence: §1

Verdict: 1 blocker, 1 major, 1 minor
"""

RFC = """# RFC
## 4. Design
### 4.1 F1 Create patient
### 4.2 F2 Edit patient
## 6. Implementation plan
### CHANGE-1 svc: gate [REQ-1]
### CHANGE-2 svc: edit [REQ-2, REQ-3]
"""


def _snap(run: Path):
    d = run / "_snap" / "svc" / "app"
    d.mkdir(parents=True)
    (d / "patient.py").write_text("def create(p):\n    return {'patient': p}\n")


def test_parse_check_and_merge(tmp_path):
    _snap(tmp_path)
    repos = {"svc": {"path": str(tmp_path / "_snap" / "svc")}}
    f = review.parse(REVIEW)
    assert [(x["severity"], x["recognised"]) for x in f] == [("BLOCKER", True), ("MAJOR", True), ("MINOR", False)]
    flags = "\n".join(review.check(f, repos, tmp_path))
    assert "#2" in flags and "missing Failure scenario" in flags and "evidence unverified" in flags
    assert "no recognised severity" in flags and "#1" not in flags
    merged = review.merge({
        "R-F1": {"title": "F1 Create patient", "findings": f, "flags": [], "status": "reviewed"},
        "R-X": {"title": "cross-cutting", "findings": [], "flags": [], "status": "reviewed", "dismissed": "- x: ok"},
        "R-F2": {"title": "F2 Edit patient", "findings": [], "flags": [], "status": "not reviewed"}})
    assert merged.index("## R-X") < merged.index("## R-F1") < merged.index("## R-F2")
    assert "### R-F1.1 BLOCKER: edit double-nests the patient" in merged
    assert "### R-F1.2 MAJOR: svc_a not handled · ⚠ evidence unverified" in merged
    assert "**Total** | **1** | **1** | **1**" in merged
    assert "Not reviewed" in merged and "Dismissed gate items" in merged


def test_flow_parts():
    assert review.flow_parts(RFC, {"id": "F2", "reqs": ["REQ-3"]}) == ("§4.2", ["CHANGE-2"])
    assert review.flow_parts(RFC, {"id": "F9", "reqs": []})[0] == "the §4 subsection for F9"


FAKE = r'''#!/usr/bin/env python3
import json, os, re, sys, uuid
prompt = sys.stdin.read(); cwd = os.getcwd()
out = re.search(r"WRITE (review/R-[\w-]+\.md)", prompt).group(1)
open(os.environ["FAKE_LOG"], "a").write(out + "\n")
print(json.dumps({"type": "system", "subtype": "init", "session_id": str(uuid.uuid4()), "mcp_servers": []}))
flaky = os.path.join(cwd, "flaky.done")
if out.endswith("R-F2.md") and not os.path.exists(flaky):
    open(flaky, "w").write("1")          # first attempt: crash without writing
    print(json.dumps({"type": "result", "subtype": "error_during_execution", "total_cost_usd": 0.01}))
    sys.exit(1)
sound = "## Review — x\n\nSound — no findings.\n\nVerdict: 0 blocker, 0 major, 0 minor\n"
body = %REVIEW% if out.endswith("R-F1.md") else sound
os.makedirs(os.path.join(cwd, "review"), exist_ok=True)
open(os.path.join(cwd, out), "w").write(body)
print(json.dumps({"type": "result", "subtype": "success", "total_cost_usd": 0.05, "num_turns": 3}))
'''


def _setup(tmp_path: Path, monkeypatch):
    run = tmp_path / "run"
    for d in ("out", "work", "logs", "facts", "raw"):
        (run / d).mkdir(parents=True)
    _snap(run)
    (run / "out" / "rfc.md").write_text(RFC)
    (run / "work" / "flows.json").write_text(json.dumps({
        "flows": [{"id": "F1", "name": "Create patient", "reqs": ["REQ-1"]},
                  {"id": "F2", "name": "Edit patient", "reqs": ["REQ-2"]}],
        "tracers": [{"id": "T1", "flows": ["F1", "F2"], "repos": ["svc"]}]}))
    (run / "scope.json").write_text(json.dumps({"repos": [{"repo": "svc", "path": str(run / "_snap" / "svc")}]}))
    fake = tmp_path / "claude"
    fake.write_text(FAKE.replace("%REVIEW%", repr(REVIEW)))
    fake.chmod(0o755)
    log = tmp_path / "calls.log"

    class Cfg:
        def get(self, k, d=None):
            return {"AGENT.CLAUDE_BIN": str(fake)}.get(k, d)

    monkeypatch.setenv("FAKE_LOG", str(log))
    monkeypatch.setattr("rfc_agent.review.cli.get_config", lambda: Cfg())
    return run, log


def test_review_end_to_end_with_retry(tmp_path, monkeypatch):
    run, log = _setup(tmp_path, monkeypatch)
    from rfc_agent.review import cli

    assert cli.main(["--run-dir", str(run)]) == 0
    calls = sorted(log.read_text().splitlines())
    assert calls == ["review/R-F1.md", "review/R-F2.md", "review/R-F2.md", "review/R-X.md"]  # R-F2 retried once
    merged = (run / "review" / "review.md").read_text()
    assert "R-F1.1 BLOCKER" in merged and "## R-F2" in merged and "Not reviewed" not in merged
    summary = json.loads((run / "logs" / "review.json").read_text())
    assert summary["counts"] == {"BLOCKER": 1, "MAJOR": 1, "MINOR": 1}
    assert len(summary["reviewers"]["R-F2"]["runs"]) == 2
    kinds = [json.loads(x)["kind"] for x in (run / "logs" / "progress.jsonl").read_text().splitlines()]
    assert kinds[0] == "start" and "retry" in kinds and kinds[-1] == "done" and kinds.count("reviewer_done") == 3
    # rerun: everything reviewed → no agent call
    assert cli.main(["--run-dir", str(run)]) == 0
    assert len(log.read_text().splitlines()) == 4


def test_review_triage_pause_file_mode(tmp_path, monkeypatch):
    run, _ = _setup(tmp_path, monkeypatch)
    from rfc_agent.review import cli

    def answer():
        prop = run / "confirm" / "investigate-pause3.proposal.json"
        while not prop.exists():
            time.sleep(0.01)
        items = json.loads(prop.read_text())["items"]
        assert [(i["key"], i["include"]) for i in items] == [("R-F1.1", True), ("R-F1.2", True), ("R-F1.3", False)]
        (run / "confirm" / "investigate-pause3.answers.json").write_text(json.dumps(
            {"selected": [1], "notes": {"R-F1.2": "disagree: svc_a never edits"}}))

    threading.Thread(target=answer, daemon=True).start()
    assert cli.main(["--run-dir", str(run), "--ask-review", "--confirm-file"]) == 0
    triage = json.loads((run / "review" / "triage.json").read_text())["findings"]
    assert triage["R-F1.1"]["action"] == "fix" and triage["R-F1.2"] == {
        "action": "ignore", "severity": "MAJOR", "note": "disagree: svc_a never edits"}


def test_review_needs_an_rfc(tmp_path):
    from rfc_agent.review import cli

    assert cli.main(["--run-dir", str(tmp_path)]) == 1
