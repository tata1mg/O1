"""Durable memory: the scorer-blind learning loop (m13). The Factorio lesson is the design constraint:
a learning pass that can see its scorer learns to game it — so promotion reads process signals only,
and eval-mode runs never write memory at all (enforced in cli, pinned here at the module level)."""

import json

from rfc_agent.contracts import RunContext


def _seed_run(tmp_path):
    (tmp_path / "logs").mkdir()
    (tmp_path / "review").mkdir()
    (tmp_path / "manifest.json").write_text(json.dumps({
        "ground": {"status": "ok", "attempts": 2, "duration_s_total": 1200,
                   "gate": {"passed": True, "failures": []}},
        "generate": {"status": "gate_failed", "attempts": 3, "duration_s_total": 900,
                     "gate": {"passed": False, "failures": [
                         "out/rfc.md: bare citation with no path (booking.py:12) — cite the full repo-qualified path",
                         "review BLOCKER: the design contradicts the max_llm_calls invariant",
                     ]}},
    }))
    (tmp_path / "logs" / "decisions.jsonl").write_text(json.dumps(
        {"stage": "generate", "decision": "patch_revision", "why": "mechanical failures only"}) + "\n")
    (tmp_path / "logs" / "activity.log").write_text(
        "[17:05:33] [ground] \u26d4 denied [permission]: Bash python3 script.py — requires approval\n")
    # scorer territory that must NEVER reach the promotion prompt
    (tmp_path / "review" / "review.md").write_text("## Verdict\nFAIL — 3 blockers. SECRET-VERDICT-TOKEN")
    return RunContext(run_dir=tmp_path, inputs={}, config=None)








def test_memory_text_folds_into_prompt_below_guidance(tmp_path, monkeypatch):
    from rfc_agent.contracts import Stage
    from rfc_agent.engine import _memory_text

    monkeypatch.setattr("rfc_agent.memory.MEMORY_ROOT", tmp_path / "mem")
    (tmp_path / "mem").mkdir()
    (tmp_path / "mem" / "ground.md").write_text("- [2026-08-07 run:x] cite full paths (outcome: pending)\n")
    stage = Stage(name="ground", requires=(), outputs=(), build_prompt=lambda c: "p", gate=lambda c: [])
    text = _memory_text(stage)
    assert "DURABLE LESSONS" in text and "cite full paths" in text
    assert "human guidance above contradicts" in text  # authority ordering stated in the prompt itself


def _audit_run(tmp_path, denial_lines: str, stages=("ground", "generate")):
    (tmp_path / "logs").mkdir(exist_ok=True)
    (tmp_path / "manifest.json").write_text(json.dumps({s: {"status": "ok"} for s in stages}))
    (tmp_path / "logs" / "activity.log").write_text(denial_lines)
    (tmp_path / "logs" / "decisions.jsonl").write_text(json.dumps(
        {"stage": "generate", "decision": "patch_revision",
         "why": "every failure is mechanical (citations/coverage tags)"}) + "\n")
    return RunContext(run_dir=tmp_path, inputs={}, config=None)


def test_audit_absorbed_violated_held_and_unmeasurable(tmp_path, monkeypatch):
    """v5 postmortem: outcome must move. Absorbed classes retire their prose; a python-denial note
    whose signal fired is violated; a quiet measurable note is held; an unmeasurable note is never
    stamped; retracted notes are left alone."""
    from rfc_agent.memory import audit

    mem = tmp_path / "mem"
    mem.mkdir()
    monkeypatch.setattr("rfc_agent.memory.MEMORY_ROOT", mem)
    (mem / "ground.md").write_text(
        "# ground\n\n"
        "- [2026-08-07 run:v3] avoid broad filesystem find operations; never search from root (outcome: pending)\n"
        "- [2026-08-07 run:m13] use python single-step invocations, not subprocess wrappers (outcome: pending)\n"
        "- [2026-08-07 run:v4] be extra careful about scope tables (outcome: pending)\n"
        "- [2026-08-10 run:v5] old lesson (outcome: retracted — misdiagnosis)\n"
    )
    (mem / "generate.md").write_text(
        "# generate\n\n"
        "- [2026-08-07 run:m13] validate RFC citations before output (outcome: pending)\n"
        "- [2026-08-07 run:v3] avoid piping or chaining git commands (outcome: pending)\n"
    )
    ctx = _audit_run(tmp_path, "[10:00:01] [ground] ⛔ denied [permission]: Bash python3 -c 'x'\n")
    audit(ctx)

    g = (mem / "ground.md").read_text()
    assert "absorbed — approve_readonly hook denies root-find" in g          # mechanism owns the class
    assert "violated ×1" in g and "python single-step" in g.split("violated")[0] + "violated"  # signal fired
    assert "be extra careful about scope tables (outcome: pending)" in g     # unmeasurable: untouched
    assert "(outcome: retracted — misdiagnosis)" in g                        # never re-stamped
    gen = (mem / "generate.md").read_text()
    assert "absorbed — qualify_citations" in gen                             # citation class retired
    assert "absorbed — approve_readonly hook auto-approves" in gen           # compound class retired


def test_audit_never_downgrades_violated(tmp_path, monkeypatch):
    from rfc_agent.memory import audit

    mem = tmp_path / "mem"
    mem.mkdir()
    monkeypatch.setattr("rfc_agent.memory.MEMORY_ROOT", mem)
    (mem / "ground.md").write_text(
        "- [2026-08-07 run:m13] use python single-step invocations (outcome: violated ×2 — last run:v5; "
        "prose is not binding, needs a mechanism)\n"
    )
    audit(_audit_run(tmp_path, ""))  # quiet run: no denials
    assert "violated ×2" in (mem / "ground.md").read_text()  # tally survives a quiet run


