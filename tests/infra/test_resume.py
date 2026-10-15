"""Retry-by-resume: a revision continues the session that wrote the outputs (--resume, warm context)
instead of launching a fresh-context rerun. Measured 2026-07-28: rework was ~40% of a run's wall-clock,
almost all of it a cold agent re-reading everything; a resumed targeted edit took 12.7s vs 400s+ cold.
Fail-safe by construction: any resumed-run error falls back to the fresh-context revision."""

import json

import rfc_agent.engine as engine_mod
from rfc_agent.contracts import RunContext
from rfc_agent.engine import claude_engine, session_path
from rfc_agent.streams import StreamResult, parse_stream


class _Cfg:
    def get(self, dotted, default=None):
        return {"AGENT.RUN_TIMEOUT": 60}.get(dotted, default)


def _ctx(tmp_path):
    (tmp_path / "logs").mkdir(parents=True, exist_ok=True)
    return RunContext(run_dir=tmp_path, inputs={"repos_root": str(tmp_path)}, config=_Cfg())


def _stage(tmp_path, **over):
    from rfc_agent.contracts import Stage

    defaults = dict(
        name="ground", requires=(), outputs=("findings/findings.md",),
        build_prompt=lambda ctx: "full prompt", gate=lambda ctx: [], revise_on_retry=True,
    )
    defaults.update(over)
    return Stage(**defaults)


def test_parse_stream_captures_session_id():
    line = json.dumps({"type": "system", "subtype": "init", "model": "m", "session_id": "sid-123"})
    assert parse_stream(line).session_id == "sid-123"


def test_fresh_run_records_session_and_revision_resumes_it(tmp_path, monkeypatch):
    ctx = _ctx(tmp_path)
    stage = _stage(tmp_path)
    seen = []

    def fake_run(stg, c, prompt, log_path, timeout, *, resume=None, **kw):
        seen.append((prompt, resume))
        c.path("findings").mkdir(exist_ok=True)
        c.path("findings/findings.md").write_text("out")
        log_path.write_text("")
        return StreamResult(session_id="sid-1", model="m")

    monkeypatch.setattr(engine_mod, "run_claude", fake_run)
    monkeypatch.setattr(engine_mod, "_stage_skill", lambda *a: None)
    monkeypatch.setattr("rfc_agent.decisions.record", lambda *a, **k: None)

    claude_engine(stage, ctx)  # fresh run: no resume, session recorded
    assert seen[0][1] is None
    assert engine_mod.load_session(session_path(tmp_path, "ground")) == ("sid-1", 0, 0)

    (tmp_path / "guidance").mkdir()
    (tmp_path / "guidance" / "ground.md").write_text("- fix REQ-2")
    claude_engine(stage, ctx)  # revision: resumes sid-1 with a guidance-only prompt
    prompt, resume = seen[1]
    assert resume == "sid-1"
    assert "RESUMED" in prompt and "fix REQ-2" in prompt and "full prompt" not in prompt


def test_resume_error_falls_back_to_fresh_revision(tmp_path, monkeypatch):
    ctx = _ctx(tmp_path)
    stage = _stage(tmp_path)
    ctx.path("findings").mkdir()
    ctx.path("findings/findings.md").write_text("out")
    (tmp_path / "guidance").mkdir()
    (tmp_path / "guidance" / "ground.md").write_text("- fix X")
    session_path(tmp_path, "ground").write_text("sid-dead")
    seen = []

    def fake_run(stg, c, prompt, log_path, timeout, *, resume=None, **kw):
        seen.append(resume)
        log_path.write_text("")
        if resume:  # the resumed session is gone/expired
            return StreamResult(is_error=True, error_reason="session_not_found")
        return StreamResult(session_id="sid-2", model="m")

    monkeypatch.setattr(engine_mod, "run_claude", fake_run)
    monkeypatch.setattr(engine_mod, "_stage_skill", lambda *a: None)
    monkeypatch.setattr("rfc_agent.decisions.record", lambda *a, **k: None)

    claude_engine(stage, ctx)
    assert seen == ["sid-dead", None]  # tried warm, fell back cold — never raised
    assert engine_mod.load_session(session_path(tmp_path, "ground"))[0] == "sid-2"  # new owner recorded


def test_stage_model_override_resolution(tmp_path, monkeypatch):
    """CLI --model > AGENT.STAGE_MODELS[stage] > AGENT.DEFAULT_MODEL (the fast-write-leg knob)."""
    import rfc_agent.engine as em
    from rfc_agent.contracts import RunContext

    class Cfg:
        def get(self, k, default=None):
            return {"AGENT.STAGE_MODELS": {"generate": "fast-writer"},
                    "AGENT.DEFAULT_MODEL": "default-model", "AGENT.RUN_TIMEOUT": 60,
                    "AGENT.CLAUDE_BIN": "claude"}.get(k, default)

        def materialize_mcp(self, names, dest):
            dest.write_text("{}")
            return dest

    captured = {}

    class FakeStdin:  # the 0.24.0 spawn contract: kickoff + steering ride stdin (stream-json)
        def write(self, s): pass
        def flush(self): pass
        def close(self): pass

    class FakeProc:
        args = []
        returncode = 0
        stdin = FakeStdin()
        def poll(self): return 0
        def wait(self, timeout=None): return 0

    def fake_popen(cmd, **kw):
        captured["cmd"] = cmd
        return FakeProc()

    monkeypatch.setattr(em.subprocess, "Popen", fake_popen)
    (tmp_path / "logs").mkdir()

    def run(stage_name, cli_model):
        ctx = RunContext(run_dir=tmp_path, inputs={"model": cli_model, "repos_root": str(tmp_path)}, config=Cfg())
        stage = _stage(tmp_path, name=stage_name, revise_on_retry=False)
        log = tmp_path / "logs" / f"{stage_name}.s.jsonl"
        log.write_text("")
        em.run_claude(stage, ctx, "p", log, 5, live=False)
        i = captured["cmd"].index("--model")
        return captured["cmd"][i + 1]

    assert run("generate", None) == "fast-writer"      # stage override beats default
    assert run("ground", None) == "default-model"      # other stages untouched
    assert run("generate", "cli-model") == "cli-model" # CLI wins over everything


def _write_gate_block(tmp_path, failures):
    (tmp_path / "guidance").mkdir(exist_ok=True)
    with (tmp_path / "guidance" / "ground.md").open("a") as f:
        f.write("- GATE FAILURES from your previous attempt — fix each one:\n")
        f.writelines(f"  - {x}\n" for x in failures)


def test_patch_mode_engages_only_when_all_latest_failures_are_mechanical(tmp_path):
    """Patch pass (comparison-doc fix #1): free-form revisions on mechanical failures converged
    34->18->2 over 3 full attempts ($22.77, 2026-08-04) with each attempt leaking a new small class.
    All-mechanical latest block -> constrained pass; ONE design failure disqualifies the set."""
    from rfc_agent.engine import latest_gate_failures, patchable_revision

    ctx = _ctx(tmp_path)
    stage = _stage(tmp_path)
    ctx.path("findings").mkdir(exist_ok=True)
    ctx.path("findings/findings.md").write_text("out")

    _write_gate_block(tmp_path, [
        "out/rfc.md: bare citation with no path — cannot be verified: experiment.py:225",
        "out/rfc.md: citation line out of range: a/b.py:1020 (file has 1005 lines)",
    ])
    assert patchable_revision(stage, ctx)

    # a later block containing a design blocker must disqualify (latest block wins)
    _write_gate_block(tmp_path, [
        "out/rfc.md: bare citation with no path — cannot be verified: x.py:1",
        "review BLOCKER: eligibility rules asserted that exist in neither PRD nor code",
    ])
    assert latest_gate_failures(tmp_path, "ground")[-1].startswith("review BLOCKER")
    assert not patchable_revision(stage, ctx)


def test_patch_mode_prompt_and_model_override(tmp_path, monkeypatch):
    """Patch revisions carry the one-Edit-per-failure contract and honor AGENT.STAGE_MODELS['patch']."""
    class _PatchCfg:
        def get(self, dotted, default=None):
            return {"AGENT.RUN_TIMEOUT": 60, "AGENT.STAGE_MODELS": {"patch": "claude-haiku-4-5"}}.get(dotted, default)

    ctx = RunContext(run_dir=tmp_path, inputs={"repos_root": str(tmp_path)}, config=_PatchCfg())
    (tmp_path / "logs").mkdir(exist_ok=True)
    stage = _stage(tmp_path)
    ctx.path("findings").mkdir(exist_ok=True)
    ctx.path("findings/findings.md").write_text("out")
    session_path(tmp_path, "ground").write_text("sid-9")
    _write_gate_block(tmp_path, ["out/rfc.md: bare citation with no path — cannot be verified: e.py:2"])

    seen = {}

    def fake_run(stg, c, prompt, log_path, timeout, *, resume=None, model=None, **kw):
        seen.update(prompt=prompt, resume=resume, model=model)
        log_path.write_text("")
        return StreamResult(session_id="sid-9", model=model or "m")

    monkeypatch.setattr(engine_mod, "run_claude", fake_run)
    monkeypatch.setattr(engine_mod, "_stage_skill", lambda *a: None)
    monkeypatch.setattr("rfc_agent.decisions.record", lambda *a, **k: None)

    claude_engine(stage, ctx)
    assert "PATCH MODE" in seen["prompt"]
    assert "one Edit per" in seen["prompt"].replace("roughly one Edit per", "one Edit per")
    assert seen["resume"] == "sid-9"
    assert seen["model"] == "claude-haiku-4-5"


def test_cold_patch_revision_uses_slim_prompt(tmp_path, monkeypatch):
    """A patch revision with no session to resume must NOT get the full build_prompt (which orders a
    corpus re-read) — measured 27s/fix cold vs 13s/fix warm (2026-08-07). Failures name their targets."""
    ctx = _ctx(tmp_path)
    stage = _stage(tmp_path, build_prompt=lambda c: "FULL CORPUS PROMPT")
    ctx.path("findings").mkdir(exist_ok=True)
    ctx.path("findings/findings.md").write_text("out")
    _write_gate_block(tmp_path, ["out/rfc.md: bare citation with no path — cannot be verified: e.py:2"])
    # no session file on disk -> cold revision path

    seen = {}

    def fake_run(stg, c, prompt, log_path, timeout, *, resume=None, model=None, **kw):
        seen.update(prompt=prompt, resume=resume)
        log_path.write_text("")
        return StreamResult(session_id="s", model="m")

    monkeypatch.setattr(engine_mod, "run_claude", fake_run)
    monkeypatch.setattr(engine_mod, "_stage_skill", lambda *a: None)
    monkeypatch.setattr("rfc_agent.decisions.record", lambda *a, **k: None)

    claude_engine(stage, ctx)
    assert seen["resume"] is None
    assert "PATCH MODE" in seen["prompt"] and "FULL CORPUS PROMPT" not in seen["prompt"]


def test_fat_session_goes_fresh_instead_of_resuming(tmp_path, monkeypatch):
    """Conditional resume (2026-08-11, TASK-29403): a revision resumed a 171,625-token session and paid
    2m24s of auto-compaction at startup plus 3m43s mid-flight — 6.1 min of a 19.2-min revision. Above
    the cap, a fresh revision (files + guidance carry the state) is strictly cheaper; below it, the
    measured warm-resume wins (12.7s vs 400s+) still apply."""
    ctx = _ctx(tmp_path)
    stage = _stage(tmp_path)
    ctx.path("findings").mkdir()
    ctx.path("findings/findings.md").write_text("out")
    (tmp_path / "guidance").mkdir()
    (tmp_path / "guidance" / "ground.md").write_text("- fix X")
    seen = []

    def fake_run(stg, c, prompt, log_path, timeout, *, resume=None, **kw):
        seen.append(resume)
        log_path.write_text("")
        return StreamResult(session_id="sid-new", model="m", context_tokens=171_625)

    monkeypatch.setattr(engine_mod, "run_claude", fake_run)
    monkeypatch.setattr(engine_mod, "_stage_skill", lambda *a: None)
    monkeypatch.setattr("rfc_agent.decisions.record", lambda *a, **k: None)

    # saved session sits past the cap -> revision must go FRESH (no resume id passed)
    engine_mod.save_session(session_path(tmp_path, "ground"),
                            StreamResult(session_id="sid-fat", context_tokens=171_625))
    claude_engine(stage, ctx)
    assert seen == [None]

    # small session -> warm resume as before
    engine_mod.save_session(session_path(tmp_path, "ground"),
                            StreamResult(session_id="sid-slim", context_tokens=40_000))
    claude_engine(stage, ctx)
    assert seen[1] == "sid-slim"


def test_load_session_tolerates_old_single_line_format(tmp_path):
    p = tmp_path / "x.session"
    p.write_text("sid-old")  # pre-0.22.9 files carry no token count
    assert engine_mod.load_session(p) == ("sid-old", 0, 0)  # 0 = always resume, the old behavior


def test_mixed_failure_set_splits_mechanical_subset(tmp_path):
    """v8 ground (2026-09-08): 5 lead-claim failures dragged 17 one-edit citation fixes into a
    14.7-min full-model free-form revision, because ONE non-mechanical failure escalated the whole
    set. Mixed sets now yield their mechanical subset for a constrained cheap pre-pass; pure sets
    yield nothing (all-mechanical belongs to patch mode, all-substantive to the plain revision)."""
    from rfc_agent.engine import drop_mechanical_guidance, split_mechanical_failures

    ctx = _ctx(tmp_path)
    stage = _stage(tmp_path)
    ctx.path("findings").mkdir(exist_ok=True)
    ctx.path("findings/findings.md").write_text("out")

    mech1 = "findings.md: bare citation with no path (index.js:144) — cite the full repo-qualified path"
    mech2 = "findings.md: citation line out of range: a/b.py:52 (file has 51 lines)"
    subst = "findings.md: LEAD token `x` points elsewhere, but it hits IN-SCOPE repo 'web' in uncited file(s)"
    _write_gate_block(tmp_path, [mech1, mech2, subst])
    assert split_mechanical_failures(stage, ctx) == [mech1, mech2]

    _write_gate_block(tmp_path, [mech1, mech2])      # all mechanical: patch mode's case, no split
    assert split_mechanical_failures(stage, ctx) == []
    _write_gate_block(tmp_path, [subst])             # all substantive: plain revision, no split
    assert split_mechanical_failures(stage, ctx) == []

    # after the pre-pass, guidance drops the patched lines and says so
    _write_gate_block(tmp_path, [mech1, mech2, subst])
    drop_mechanical_guidance(tmp_path, "ground", 2)
    g = (tmp_path / "guidance" / "ground.md").read_text()
    assert "bare citation" not in g and "out of range" not in g
    assert "LEAD token `x`" in g and "already fixed by an automated patch pass" in g


def test_split_prepass_wiring():
    """The pre-pass must rotate its stream (witness evidence), rewrite guidance, and exclude generate
    (the emission drain reads only the CURRENT stream — a rotated pre-pass's edits would be lost)."""
    import inspect

    from rfc_agent import engine

    src = inspect.getsource(engine.claude_engine)
    assert "split_mechanical_failures" in src and "drop_mechanical_guidance" in src
    assert 'stage.name != "generate"' in src and "rotated_stream_path" in src


def test_guidance_latest_block_replaces_stale_same_origin(tmp_path):
    """v8/v9 field cost (2026-09-08): guidance APPENDED gate blocks per attempt, so revisers received
    stale already-fixed failures alongside fresh ones and burned ~1.5 min/round proving them stale.
    The latest same-origin block now replaces prior ones (collapsed to a running count); human notes
    and other-origin blocks survive."""
    from rfc_agent.runner import _write_guidance

    ctx = _ctx(tmp_path)
    g = tmp_path / "guidance" / "ground.md"
    g.parent.mkdir(exist_ok=True)
    g.write_text("- HUMAN: prefer the labs naming convention\n")

    _write_guidance(ctx, "ground", "ground", ["fail A", "fail B", "fail C"])
    _write_guidance(ctx, "ground", "ground", ["fail D"])
    text = g.read_text()
    assert "HUMAN: prefer the labs" in text                      # human guidance survives
    assert "fail A" not in text and "fail C" not in text          # stale bullets gone
    assert "(3 earlier gate failure(s)" in text and "fail D" in text
    assert text.count("- GATE FAILURES") == 1                     # exactly one live block

    _write_guidance(ctx, "ground", "ground", ["fail E"])
    text = g.read_text()
    assert "(4 earlier gate failure(s)" in text                   # counts collapse, not stack
    assert text.count("earlier gate failure(s)") == 1

    # a cross-stage (review->ground) block is a DIFFERENT origin: it must survive ground's own writes
    _write_guidance(ctx, "ground", "review", ["review blocker R1"])
    _write_guidance(ctx, "ground", "ground", ["fail F"])
    text = g.read_text()
    assert "review blocker R1" in text and "fail F" in text
    assert "fail E" not in text
