"""Revise: fix-log / confirmation parsing, statuses, the ⚠ section, and a two-round run with a fake `claude`."""

from __future__ import annotations

import json
from pathlib import Path

from rfc_agent.revise import fixlog

REVIEW_MD = """# Review

## R-X — cross-cutting

### R-X.1 MAJOR: coverage says REQ-2 covered but §5 has no endpoint
- Evidence: §2 vs §5
- Failure scenario: …
- Fix: add the endpoint

## R-F1 — F1 Create patient

### R-F1.1 BLOCKER: edit double-nests the patient · ⚠ evidence unverified
- Evidence: svc/app/patient.py:2
- Fix: keep the dict

### R-F1.2 MINOR: wording

## R-F2 — F2 Edit patient

⚠ **Not reviewed** — the reviewer failed twice; this part has had no independent review.
"""


def test_parse_review_fixes_and_confirmations():
    f = fixlog.findings(REVIEW_MD)
    assert list(f) == ["R-X.1", "R-F1.1", "R-F1.2"]
    assert f["R-F1.1"]["claim"] == "edit double-nests the patient" and f["R-F1.1"]["reviewer"] == "R-F1"
    assert fixlog.not_reviewed(REVIEW_MD) == [("R-F2", "F2 Edit patient")]
    fixes = (
        "## Round 1\n\n### R-F1.1 FIXED\n- Changed: §4.2\n\n### R-X.1 DISAGREE\n- Why: x\n\n"
        "## Round 2\n\n### R-X.1 FIXED\n"
    )
    assert {k: v["status"] for k, v in fixlog.lead_entries(fixes, 1).items()} == {
        "R-F1.1": "FIXED",
        "R-X.1": "DISAGREE",
    }
    assert fixlog.lead_entries(fixes, 2)["R-X.1"]["status"] == "FIXED"
    v, new = fixlog.confirmations(
        "### R-F1.1 RESOLVED\n- ok\n\n### NEW BLOCKER: schema forbids action\n- Evidence: e", "R-F1", 1
    )
    assert v["R-F1.1"]["verdict"] == "RESOLVED" and new[0]["id"] == "R-F1.N11" and new[0]["severity"] == "BLOCKER"


def test_settle_matrix():
    lead = lambda s: {"status": s}  # noqa: E731
    ver = lambda s: {"verdict": s}  # noqa: E731
    assert fixlog.settle(lead("FIXED"), ver("RESOLVED")) == "fixed"
    assert fixlog.settle(lead("DISAGREE"), ver("ACCEPTED")) == "accepted_disagreement"
    assert fixlog.settle(lead("OPEN"), ver("ACCEPTED")) == "open_question"
    assert fixlog.settle(lead("DISAGREE"), ver("NOT RESOLVED")) is None
    assert fixlog.settle(None, ver("RESOLVED")) is None and fixlog.settle(lead("FIXED"), None) is None


def test_unresolved_section_is_idempotent():
    rfc = "# RFC\n## 7. Rollout\nr\n## 8. Open questions\n| Q1 |\n"
    sec = fixlog.unresolved_section(
        [
            {
                "id": "R-X.1",
                "severity": "MAJOR",
                "claim": "a | b",
                "reviewer_position": "still wrong",
                "lead_position": "it's fine",
            }
        ]
    )
    once = fixlog.insert_unresolved(rfc, sec)
    assert once.index("## ⚠ Unresolved") < once.index("## 8.") and "a \\| b" in once
    assert fixlog.insert_unresolved(once, sec) == once
    assert fixlog.insert_unresolved(once, None) == rfc


FAKE = r"""#!/usr/bin/env python3
import json, os, re, sys, uuid
prompt = sys.stdin.read(); args = sys.argv[1:]; cwd = os.getcwd()
sid = args[args.index("--resume") + 1] if "--resume" in args else str(uuid.uuid4())
def log(x): open(os.environ["FAKE_LOG"], "a").write(x + " resume=" + str("--resume" in args) + "\n")
def w(rel, text, mode="w"):
    p = os.path.join(cwd, rel); os.makedirs(os.path.dirname(p), exist_ok=True); open(p, mode).write(text)
print(json.dumps({"type": "system", "subtype": "init", "session_id": sid, "mcp_servers": []}))
if "The independent review found" in prompt:
    rnd = re.search(r'"## Round (\d+)"', prompt).group(1); log("fix" + rnd)
    if rnd == "1":
        w("review/fixes.md", "## Round 1\n\n### R-F1.1 FIXED\n- Changed: §4.1 keeps the dict\n\n"
          "### R-X.1 DISAGREE\n- Why: §5 already has it (svc/app/patient.py:1)\n")
        p = os.path.join(cwd, "out", "rfc.md"); t = open(p).read(); open(p, "w").write(t.replace("x\n", "x fixed\n", 1))
    else:
        w("review/fixes.md", "\n## Round 2\n\n### R-F1.N11 FIXED\n- Changed: dropped the new key\n\n"
          "### R-X.1 DISAGREE\n- Why: still there\n", "a")
elif "The lead has answered your review" in prompt:
    rnd = re.search(r"round (\d+)\)", prompt).group(1)
    out = re.search(r"write (review/\S+\.md)", prompt).group(1); log("confirm " + out)
    if "R-F1" in out and rnd == "1":
        w(out, "### R-F1.1 RESOLVED\n- ok\n\n### NEW BLOCKER: schema forbids action\n"
               "- Evidence: svc/app/patient.py:1\n")
    elif "R-F1" in out:
        w(out, "### R-F1.N11 RESOLVED\n- ok\n")
    else:
        w(out, "### R-X.1 NOT RESOLVED\n- §5 still lacks it\n")
elif "The gate found these problems" in prompt:
    log("gate-fix")
print(json.dumps({"type": "result", "subtype": "success", "session_id": sid, "total_cost_usd": 0.02, "num_turns": 2}))
"""


def test_revise_two_rounds_end_to_end(tmp_path, monkeypatch):
    run = tmp_path / "run"
    for d in ("out", "work", "logs", "review"):
        (run / d).mkdir(parents=True)
    (run / "out" / "rfc.md").write_text("# RFC\n## 4. Design\nx\n## 8. Open questions\n| Q1 |\n")
    (run / "out" / "coverage.json").write_text("{}")
    (run / "work" / "reqs.json").write_text(json.dumps({"requirements": []}))
    (run / "work" / "understanding.md").write_text("u")
    (run / "scope.json").write_text(json.dumps({"repos": []}))
    (run / "review" / "review.md").write_text(REVIEW_MD)
    (run / "logs" / "lead.session").write_text("lead-sess")
    (run / "work" / "review.state.json").write_text(
        json.dumps(
            {
                "reviewers": {
                    "R-F1": {"runs": [{"session_id": "rf1-sess"}]},
                    "R-X": {"runs": [{"session_id": "rx-sess"}]},
                }
            }
        )
    )
    fake = tmp_path / "claude"
    fake.write_text(FAKE)
    fake.chmod(0o755)
    log = tmp_path / "calls.log"

    class Cfg:
        def get(self, k, d=None):
            return {"AGENT.CLAUDE_BIN": str(fake)}.get(k, d)

    monkeypatch.setenv("FAKE_LOG", str(log))
    monkeypatch.setattr("rfc_agent.revise.cli.get_config", lambda: Cfg())
    monkeypatch.setattr("rfc_agent.draft.cli.get_config", lambda: Cfg())
    from rfc_agent.revise import cli

    assert cli.main(["--run-dir", str(run), "--no-readable"]) == 0
    calls = [c for c in log.read_text().splitlines() if not c.startswith("gate-fix")]
    assert calls[0] == "fix1 resume=True" and "fix2 resume=True" in calls
    assert (
        "confirm review/R-F1.confirm.md resume=True" in calls and "confirm review/R-F1.confirm2.md resume=True" in calls
    )
    status = json.loads((run / "review" / "status.json").read_text())
    assert status["R-F1.1"]["status"] == "fixed"
    assert status["R-F1.N11"]["status"] == "fixed" and status["R-F1.N11"]["raised_in_round"] == 1
    assert status["R-X.1"]["status"] == "unresolved"
    assert status["R-F1.2"]["status"] == "minor_noted" and status["R-F2"]["status"] == "not_reviewed"
    rfc = (run / "out" / "rfc.md").read_text()
    assert "## ⚠ Unresolved review findings" in rfc and "R-X.1" in rfc and "R-F2" in rfc
    assert rfc.index("## ⚠") < rfc.index("## 8.") and "x fixed" in rfc
    assert (run / "review" / "rfc.round1.diff").read_text().count("+x fixed") == 1
    kinds = [json.loads(x)["kind"] for x in (run / "logs" / "progress.jsonl").read_text().splitlines()]
    assert kinds[0] == "start" and kinds.count("confirmed") == 2 and kinds[-1] == "done"
    # rerun: nothing open → no fix / confirm calls, section unchanged
    n = len(calls)
    assert cli.main(["--run-dir", str(run), "--no-readable"]) == 0
    assert len([c for c in log.read_text().splitlines() if not c.startswith("gate-fix")]) == n
    assert (run / "out" / "rfc.md").read_text() == rfc


def test_revise_needs_review(tmp_path):
    from rfc_agent.revise import cli

    assert cli.main(["--run-dir", str(tmp_path)]) == 1



def _run_dir(tmp_path):
    run = tmp_path / "run"
    for d in ("out", "work", "logs", "review"):
        (run / d).mkdir(parents=True)
    (run / "out" / "rfc.md").write_text("# RFC\n## 4. Design\nx\n## 8. Open questions\n| Q1 |\n")
    (run / "out" / "coverage.json").write_text("{}")
    (run / "work" / "reqs.json").write_text(json.dumps({"requirements": []}))
    (run / "work" / "understanding.md").write_text("u")
    (run / "scope.json").write_text(json.dumps({"repos": []}))
    (run / "review" / "review.md").write_text(REVIEW_MD)
    (run / "logs" / "lead.session").write_text("lead-sess")
    (run / "work" / "review.state.json").write_text(json.dumps({"reviewers": {
        "R-F1": {"runs": [{"session_id": "rf1-sess"}]}, "R-X": {"runs": [{"session_id": "rx-sess"}]}}}))
    return run


def _revise(tmp_path, monkeypatch, section: dict) -> dict:
    run = _run_dir(tmp_path)
    fake = tmp_path / "claude"
    fake.write_text(FAKE)
    fake.chmod(0o755)

    class Cfg:
        def get(self, k, d=None):
            return {"AGENT.CLAUDE_BIN": str(fake), "REVISE": section}.get(k, d)

    monkeypatch.setenv("FAKE_LOG", str(tmp_path / "calls.log"))
    monkeypatch.setattr("rfc_agent.revise.cli.get_config", lambda: Cfg())
    monkeypatch.setattr("rfc_agent.draft.cli.get_config", lambda: Cfg())
    from rfc_agent.revise import cli

    cli.main(["--run-dir", str(run), "--no-readable"])
    return json.loads((run / "review" / "status.json").read_text())


def test_settings_defaults_and_overrides_merge():
    from rfc_agent.revise import cli

    class Cfg:
        def get(self, k, d=None):
            return {"REVISE": {"ROUNDS": 3, "_comment": "ignored"}}.get(k, d)

    s = cli.settings(Cfg())
    assert s["ROUNDS"] == 3 and s["MINORS"] == "apply_if_cheap" and s["LEAD_TIMEOUT_S"] == 1200
    assert "_comment" not in s


def test_minors_are_only_noted_by_default(tmp_path, monkeypatch):
    assert _revise(tmp_path, monkeypatch, {})["R-F1.2"]["status"] == "minor_noted"


def test_minors_fix_sends_minor_findings_to_the_lead(tmp_path, monkeypatch):
    status = _revise(tmp_path, monkeypatch, {"MINORS": "fix"})
    # the fake lead never answers R-F1.2, so it ends unresolved: it was worked on, not just noted
    assert status["R-F1.2"]["status"] == "unresolved"

_ = Path
