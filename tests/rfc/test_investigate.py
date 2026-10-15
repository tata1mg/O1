"""Investigate: plan checks, rendering, post-agent checks, hook, UI assignment, pauses, and an end-to-end run with a
fake `claude` that plays the lead, tracers and 1c by writing the files they would write (no tokens spent)."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
import threading
import time
from pathlib import Path

import pytest

from rfc_agent.investigate import checks, pauses, plan, ui
from rfc_agent.investigate.config import resolve

# ---- fixtures -------------------------------------------------------------------------------------


def _reqs():
    return {"granularity": "grouped", "requirements": [
        {"id": "REQ-1", "priority": "must", "text": "nudge on similar name", "quote": "If similar…", "flows": ["F1"]},
        {"id": "REQ-2", "priority": "nice", "text": "copy", "quote": "Copy…", "flows": []}]}


def _flows():
    return {"flows": [{"id": "F1", "name": "Create patient",
                       "trigger": {"surface": "mweb", "screens": ["Add patient"], "action": "Save"},
                       "reqs": ["REQ-1"],
                       "hops": [{"n": 1, "repo": "svc", "what": "POST /v4/users/{id}/patients", "anchor": "x",
                                 "confidence": "map"},
                                {"n": 2, "repo": "bff", "what": "BFF", "anchor": "y", "confidence": "inferred"}],
                       "verify": ["same-name behaviour"], "ignore": []}],
            "tracers": [{"id": "T1", "flows": ["F1"], "repos": ["svc"], "why": "create path"}],
            "no_flow_reqs": ["REQ-2"]}


def _scope(run_dir: Path, figma=None):
    return {"repos_root": str(run_dir / "root"),
            "repos": [{"repo": "svc", "path": str(run_dir / "_snap" / "svc"), "source": "snapshot", "sha": "abc"}],
            "figma": figma or []}


def _snapshot(run_dir: Path):
    d = run_dir / "_snap" / "svc" / "app"
    d.mkdir(parents=True)
    (d / "patient.py").write_text("def create_new_patient(p):\n    exclude_patient_id = 1\n    return {'patient': p}\n")


# ---- config & plan --------------------------------------------------------------------------------


def test_presets_and_precedence():
    class C:
        def get(self, k, d=None):
            return {"INVESTIGATE": {"MAX_TRACERS": 9}}.get(k, d)

    s = resolve(C(), {"PRESET": "fast"})
    assert s["TRACER_SPLIT"] == "hop" and s["MAX_TRACERS"] == 9 and s["REQ_GRANULARITY"] == "grouped"
    assert resolve(None)["PRESET"] == "balanced" and resolve(None)["MAX_TRACERS"] == 6
    with pytest.raises(SystemExit):
        resolve(None, {"REQ_GRANULARITY": "huge"})


def test_each_preset_sets_its_own_tracer_knobs():
    fast, balanced = resolve(None, {"PRESET": "fast"}), resolve(None)
    assert (fast["ENTRY_PREPASS"], fast["SCREENS_PER_READER"], fast["TRACER_EFFORT"], fast["TRACER_CALL_BUDGET"]) == (
        True, 15, "low", 40)
    assert (balanced["ENTRY_PREPASS"], balanced["SCREENS_PER_READER"], balanced["TRACER_EFFORT"],
            balanced["TRACER_CALL_BUDGET"]) == (False, 50, "medium", 80)


def test_config_overrides_models_efforts_and_timeouts():
    class C:
        def get(self, k, d=None):
            return {"INVESTIGATE": {"LEAD_MODEL": "m-lead", "UI_EFFORT": "low", "TRACER_TIMEOUT_S": 5,
                                    "_note": "ignored"}}.get(k, d)

    s = resolve(C())
    assert (s["LEAD_MODEL"], s["UI_EFFORT"], s["TRACER_TIMEOUT_S"]) == ("m-lead", "low", 5)
    assert s["LEAD_TIMEOUT_S"] == 1200 and "_note" not in s  # untouched keys keep the base value


def test_plan_check_catches_each_rule(tmp_path):
    assert plan.check(_reqs(), _flows(), _scope(tmp_path), 6) == []
    f = _flows()
    f["flows"][0]["verify"] = []
    f["flows"][0]["reqs"] = ["REQ-9"]
    f["no_flow_reqs"] = []
    f["tracers"] = [{"id": "T1", "flows": ["F1"], "repos": ["nope"], "why": ""}] * 2
    errs = "\n".join(plan.check(_reqs(), f, _scope(tmp_path), 1))
    for needle in ("verify", "unknown REQ id REQ-9", "REQ-1, REQ-2", "limit 1", "duplicate tracer", "nope"):
        assert needle in errs


def test_plan_check_tracers_read_only_scope_repos(tmp_path):
    f = _flows()
    f["tracers"][0]["repos"] = ["svc", "bff"]  # bff is a hop of F1 but not in scope
    assert plan.check(_reqs(), f, _scope(tmp_path), 6) == [
        "T1: repos bff are not in scope — a tracer reads only scope repos"]
    f = _flows()
    f["flows"].append({"id": "F2", "name": "App only", "reqs": [], "verify": ["x"],
                       "hops": [{"n": 1, "repo": "android app", "what": "UI", "anchor": "", "confidence": "prd"}]})
    assert plan.check(_reqs(), f, _scope(tmp_path), 6) == []  # nothing in scope to trace → no tracer needed


def test_repos_needing_attention_and_render(tmp_path):
    scope = _scope(tmp_path)
    missing, no_snap = plan.repos_needing_attention(_flows(), scope)
    assert missing == {"bff": ["F1"]} and no_snap == {}
    scope["repos"].append({"repo": "bff", "path": None, "source": "context-index-only"})
    assert plan.repos_needing_attention(_flows(), scope) == ({}, {"bff": ["F1"]})
    md = plan.render_flows(_flows(), scope, "balanced", 6)
    assert "⚠ no snapshot" in md and "T1" in md
    assert "not in scope — not traced" in plan.render_flows(_flows(), _scope(tmp_path), "balanced", 6)
    brief = plan.render_brief(_flows()["tracers"][0], _flows(), _reqs(), scope,
                              {"svc": {"code_index": False, "age_note": "contracts from commit 6db00de"}})
    assert "code index: no" in brief and "V1 same-name behaviour" in brief and "REQ-1: nudge" in brief


def test_load_json_tolerates_fence(tmp_path):
    p = tmp_path / "x.json"
    p.write_text('```json\n{"a": 1}\n```')
    assert plan.load_json(p) == ({"a": 1}, "")
    p.write_text("not json")
    assert plan.load_json(p)[0] is None


# ---- checks ---------------------------------------------------------------------------------------

FACTS = """## F1 Create patient
### Hop chain
- svc/app/patient.py:1 create_new_patient
### Shapes
- returns {"patient": ...} (svc/app/patient.py:3)
### Callers
### Existing behaviour
- none found — searched: merge
### Data model
### Out-of-scope calls
- calls bff_service
### Verify answers
### Unknowns
"""


def test_tracer_checks_split_must_fix_and_flags(tmp_path):
    _snapshot(tmp_path)
    t = _flows()["tracers"][0]
    facts = tmp_path / "facts"
    facts.mkdir()
    (facts / "T1.md").write_text(FACTS + "\n- svc/app/patient.py:99 bad line\n- svc/app/gone.py:1 x\n"
                                 "- `exclude_patient_id` not found in svc\n")
    res = checks.check_tracer(t, _flows(), plan.scope_repos(_scope(tmp_path)), tmp_path)
    mf = "\n".join(res["must_fix"])
    assert "line out of range" in mf and "file not found" in mf and "exclude_patient_id" in mf
    flags = "\n".join(res["flags"])
    assert "Callers has no citation" in flags and "verify items unanswered" in flags
    assert "Shapes" not in flags  # Shapes has a citation
    (facts / "T1.md").unlink()
    assert checks.check_tracer(t, _flows(), {}, tmp_path)["must_fix"] == ["facts/T1.md was not written"]


def test_ui_checks(tmp_path):
    (tmp_path / "facts").mkdir()
    (tmp_path / "facts" / "UI1.md").write_text('## F1 Create\n- "Save patient" (1:2) (9:9) REQ-1 REQ-7\n')
    r = {"id": "UI1", "flows": ["F1", "F2"], "node_ids": {"1:2"}, "texts": {"Save"}}
    res = checks.check_ui(r, _flows(), _reqs(), tmp_path)
    assert res["must_fix"] == ["node ids not in the export: 9:9"]
    flags = "\n".join(res["flags"])
    assert "F2" in flags and "REQ-7" in flags and "quoted string" in flags


# ---- hook -----------------------------------------------------------------------------------------


def _hook(mode, event, env):
    return subprocess.run([sys.executable, "-m", "rfc_agent.investigate.hook", mode], input=json.dumps(event),
                          capture_output=True, text=True, env={**os.environ, **env})


def test_hook_confines_writes_and_nudges_once(tmp_path):
    env = {"INVESTIGATE_RUN_DIR": str(tmp_path), "INVESTIGATE_WRITE_ALLOW": "facts/T1.md,work/ui/*",
           "INVESTIGATE_COUNTER": str(tmp_path / "n"), "INVESTIGATE_CALL_BUDGET": "2"}
    ok = _hook("pre", {"tool_input": {"file_path": str(tmp_path / "facts" / "T1.md")}}, env)
    assert ok.returncode == 0
    assert _hook("pre", {"tool_input": {"file_path": str(tmp_path / "work/ui/a.md")}}, env).returncode == 0
    bad = _hook("pre", {"tool_input": {"file_path": str(tmp_path / "_snap" / "svc" / "x.py")}}, env)
    assert bad.returncode == 2 and "read-only" in bad.stderr
    assert _hook("pre", {"tool_input": {"file_path": "/etc/hosts"}}, env).returncode == 2
    outs = [_hook("post", {}, env).stdout for _ in range(3)]
    assert outs[0] == "" and "Wrap up now" in outs[1] and outs[2] == ""


# ---- UI assignment --------------------------------------------------------------------------------


def _export(run_dir: Path):
    snap = run_dir / "_inputs" / "figma" / "K-1-2"
    snap.mkdir(parents=True)
    frames = []
    for i, (name, sec) in enumerate([("Add patient", "Patient"), ("Add patient error", "Patient"),
                                     ("Cart", "Cart")], 1):
        doc = {"document": {"type": "FRAME", "absoluteBoundingBox": {"x": 0, "y": 0, "width": 360, "height": 800},
                            "children": [{"type": "TEXT", "characters": f"text {i}",
                                          "absoluteBoundingBox": {"x": 10, "y": 10 * i, "width": 50, "height": 10}}]}}
        (snap / f"1-{i}.json").write_text(json.dumps(doc))
        frames.append({"id": f"1:{i}", "name": name, "section": sec, "json": f"1-{i}.json", "png": f"1-{i}.png"})
    (snap / "index.json").write_text(json.dumps({"frames": frames}))
    return [{"run_dir_rel": "_inputs/figma/K-1-2", "sections": [{"name": "Patient", "include": True},
                                                                  {"name": "Cart", "include": True}]}]


def test_ui_readers_keep_a_flows_section_together(tmp_path):
    scope = _scope(tmp_path, _export(tmp_path))
    readers = ui.plan_readers(tmp_path, scope, _flows(), per_reader=2)
    owner = next(r for r in readers if "Patient" in r["sections"])
    assert owner["flows"] == ["F1"] and len(owner["frames"]) == 2  # the section was not split
    path, ids, texts = ui.write_reader_inputs(tmp_path, owner)
    assert ids == {"1:1", "1:2"} and "text 1" in texts and "render: _inputs/figma/K-1-2/1-1.png" in path.read_text()


# ---- pauses (file mode) ---------------------------------------------------------------------------


def test_pause_file_mode_returns_picks_feedback_and_answers(tmp_path):
    from rfc_agent.scope import confirm

    items = [confirm.Item("retry", "bff", "bff"), confirm.Item("question", "Q1", "who owns the BFF?")]

    def answer():
        prop = tmp_path / "confirm" / "investigate-pause2.proposal.json"
        while not prop.exists():
            time.sleep(0.01)
        (tmp_path / "confirm" / "investigate-pause2.answers.json").write_text(json.dumps(
            {"selected": [1], "add_repos": ["labs_agg"], "feedback": "merge F2", "answers": {"Q1": "cart team"}}))

    threading.Thread(target=answer, daemon=True).start()
    out = pauses.ask(2, tmp_path, "hdr", items, file_mode=True, accept_defaults=False,
                     questions={"Q1": {"question": "who owns the BFF?"}}, timeout_s=10)
    # add_repos is ignored: Scope's repos are final
    assert [(i.key, i.include) for i in out["items"]] == [("bff", True), ("Q1", False)]
    assert out["feedback"] == "merge F2" and out["answers"] == {"Q1": "cart team"}


# ---- end to end with a fake claude ----------------------------------------------------------------

FAKE_CLAUDE = r'''#!/usr/bin/env python3
import json, os, re, sys, uuid
prompt = sys.stdin.read()
args = sys.argv[1:]
cwd = os.getcwd()
sid = args[args.index("--resume") + 1] if "--resume" in args else str(uuid.uuid4())
def w(rel, text):
    p = os.path.join(cwd, rel); os.makedirs(os.path.dirname(p), exist_ok=True); open(p, "w").write(text)
print(json.dumps({"type": "system", "subtype": "init", "session_id": sid, "mcp_servers": []}))
log = os.environ.get("FAKE_LOG")
if log: open(log, "a").write(prompt.split("\n", 1)[0][:60] + " | resume=" + str("--resume" in args) + "\n")
if "you only PLAN" in prompt:
    w("work/reqs.json", json.dumps(%REQS%))
    w("work/flows.json", json.dumps(%FLOWS%))
elif "You trace how one part" in prompt:
    out = re.search(r"write (facts/\S+\.md)", prompt).group(1)
    w(out, %FACTS%)
elif "You read product designs" in prompt:
    out = re.search(r"write (facts/\S+\.md)", prompt).group(1)
    w(out, "## F1 Create patient\n- screens (1:1)\n")
elif "These problems were found" in prompt:
    out = re.search(r"in (facts/\S+\.md)", prompt).group(1)
    p = os.path.join(cwd, out); t = open(p).read().replace("svc/app/patient.py:99", "svc/app/patient.py:2")
    open(p, "w").write(t)
elif "The tracers are done" in prompt:
    w("work/understanding.md", "## Cross-flow constraints\n- svc returns {patient} (svc/app/patient.py:3)\n")
    w("work/open_questions.json", json.dumps({"questions": [{"id": "Q1", "question": "who owns the BFF?",
                                                            "blocks": ["REQ-1"], "suggested_answer": None}]}))
print(json.dumps({"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Write", "input": {}}]}}))
print(json.dumps({"type": "result", "subtype": "success", "session_id": sid, "total_cost_usd": 0.01,
                  "num_turns": 2, "result": "done"}))
'''


def test_investigate_end_to_end_with_fake_claude(tmp_path, monkeypatch):
    run = tmp_path / "run"
    (run / "raw").mkdir(parents=True)
    (run / "raw" / "prd.md").write_text("# PRD\nIf similar…")
    _snapshot(run)
    flows = _flows()
    # F1 keeps its bff hop (not in scope): reported, never asked about → no pause
    scope = _scope(run, _export(run))
    (run / "scope.json").write_text(json.dumps(scope))
    facts = FACTS + "- svc/app/patient.py:99 off by a lot\n"
    fake = tmp_path / "claude"
    fake.write_text(FAKE_CLAUDE.replace("%REQS%", repr(_reqs())).replace("%FLOWS%", repr(flows))
                    .replace("%FACTS%", repr(facts)))
    fake.chmod(0o755)
    log = tmp_path / "calls.log"

    class Cfg:
        def get(self, k, d=None):
            return {"AGENT.CLAUDE_BIN": str(fake)}.get(k, d)

    monkeypatch.setenv("FAKE_LOG", str(log))
    monkeypatch.setattr("rfc_agent.investigate.cli.get_config", lambda: Cfg())
    monkeypatch.setattr("rfc_agent.investigate.repo_info.index_info", lambda *a, **k: {})
    from rfc_agent.investigate import cli

    assert cli.main(["--run-dir", str(run), "--yes"]) == 0
    calls = log.read_text().splitlines()
    assert any("ROLE" in c and "resume=False" in c for c in calls)
    chk = json.loads((run / "facts" / "T1.check.json").read_text())
    assert chk["status"] == "flagged" and chk["before_fix"]["must_fix"]  # broken citation fixed by the tracer
    assert (run / "facts" / "UI1.md").exists() and (run / "work" / "briefs" / "T1.md").exists()
    assert (run / "work" / "understanding.md").exists() and (run / "logs" / "lead.session").exists()
    status = json.loads((run / "logs" / "status.json").read_text())
    assert status["step"] == "done" and {a["id"] for a in status["agents"]} >= {"lead", "T1", "UI1"}
    kinds = [json.loads(x)["kind"] for x in (run / "logs" / "progress.jsonl").read_text().splitlines()]
    assert kinds[0] == "config" and "1a_done" in kinds and "1c_done" in kinds and kinds[-1] == "done"
    assert "outside" in kinds and "pause1" not in kinds and "pause2" not in kinds
    assert not (run / "confirm").exists()
    # 1c resumed the lead's session from 1a
    assert any("The tracers are done" in c or "resume=True" in c for c in calls)
    # rerun resumes: nothing runs again
    n = len(calls)
    assert cli.main(["--run-dir", str(run), "--yes"]) == 0
    assert len(log.read_text().splitlines()) == n


def test_investigate_needs_stage0_output(tmp_path):
    from rfc_agent.investigate import cli

    assert cli.main(["--run-dir", str(tmp_path)]) == 1


_ = textwrap  # (kept for readability of fixtures above)


def test_hook_checks_citations_at_write_time(tmp_path):
    _snapshot(tmp_path)
    src = tmp_path / "_snap" / "svc" / "app" / "patient.py"
    src.write_text(src.read_text() + "".join(f"# filler {i}\n" for i in range(20)))
    (tmp_path / "scope.json").write_text(json.dumps(_scope(tmp_path)))
    facts = tmp_path / "facts" / "T1.md"
    facts.parent.mkdir()
    facts.write_text("- svc/app/gone.py:1 x\n- `create_new_patient` returns (svc/app/patient.py:15)\n"
                     "- `exclude_patient_id` not found in svc\n- svc/app/patient.py:1 ok `create_new_patient`\n"
                     "- `create_new_patient` (_snap/svc/app/patient.py:16)\n")
    env = {"INVESTIGATE_RUN_DIR": str(tmp_path), "INVESTIGATE_CHECK_FILES": "facts/T1.md"}
    ev = {"tool_name": "Write", "tool_input": {"file_path": str(facts)}}
    out = _hook("post", ev, env).stdout
    ctx = json.loads(out)["hookSpecificOutput"]["additionalContext"]
    assert "gone.py:1 — file not found" in ctx
    assert "patient.py:15 doesn't mention `create_new_patient`" in ctx  # off-by-some line number
    assert "claimed not found: `exclude_patient_id`" in ctx
    assert "patient.py:1 doesn't mention" not in ctx  # the correct line is not reported
    assert "start with '_snap/'" in ctx and "patient.py:16 doesn't mention" in ctx  # prefixed form still checked
    assert _hook("post", {"tool_name": "Read", "tool_input": {"file_path": str(facts)}}, env).stdout == ""
    other = {"tool_name": "Write", "tool_input": {"file_path": str(tmp_path / "work" / "x.md")}}
    assert _hook("post", other, env).stdout == ""  # not a checked file
