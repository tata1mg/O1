"""AGENT.STAGE_EFFORT: per-stage `--effort` for headless children (Layer 2 of the extract redesign)."""
from __future__ import annotations

import inspect

import pytest

from rfc_agent import engine
from rfc_agent.engine import EFFORT_LEVELS, EngineFailure, stage_effort


class _Cfg:
    def __init__(self, **kv):
        self.kv = kv

    def get(self, key, default=None):
        return self.kv.get(key, default)


def test_unconfigured_stage_runs_as_before_and_keeps_operator_env():
    env = {"CLAUDE_CODE_EFFORT_LEVEL": "low", "PATH": "/bin"}
    level, out, notes = stage_effort(_Cfg(), "review", env)
    assert level is None and out == env and notes == []
    level, out, _ = stage_effort(_Cfg(**{"AGENT.STAGE_EFFORT": {"extract": "low"}}), "review", env)
    assert level is None and out["CLAUDE_CODE_EFFORT_LEVEL"] == "low"  # other stages untouched


def test_configured_stage_gets_level_and_env_override_is_stripped():
    cfg = _Cfg(**{"AGENT.STAGE_EFFORT": {"extract": "Low"}, "EXTRACT.QUOTE_GROUNDING": True})
    level, out, notes = stage_effort(cfg, "extract", {"CLAUDE_CODE_EFFORT_LEVEL": "max", "PATH": "/bin"})
    assert level == "low"
    assert "CLAUDE_CODE_EFFORT_LEVEL" not in out and out["PATH"] == "/bin"  # the CLI would let env win
    assert any("ignored for extract" in n for n in notes)


@pytest.mark.parametrize("level", EFFORT_LEVELS)
def test_every_cli_level_is_accepted(level):
    cfg = _Cfg(**{"AGENT.STAGE_EFFORT": {"ground": level}})
    assert stage_effort(cfg, "ground", {})[0] == level


def test_unknown_level_or_shape_fails_loudly():
    with pytest.raises(EngineFailure, match="allowed"):
        stage_effort(_Cfg(**{"AGENT.STAGE_EFFORT": {"extract": "turbo"}}), "extract", {})
    with pytest.raises(EngineFailure, match="object"):
        stage_effort(_Cfg(**{"AGENT.STAGE_EFFORT": "low"}), "extract", {})


def test_run_claude_passes_effort_flag_and_child_env():
    """Source-level wiring check (same style as the dispatch-note test): the level reaches `--effort` after
    `--model`, applies to resumed sessions too, and the child env is built from the stripped copy."""
    src = inspect.getsource(engine.run_claude)
    assert 'stage_effort(cfg, stage.name, dict(os.environ))' in src
    after_model = src.split('cmd += ["--model", model]')[1]
    assert 'cmd += ["--effort", effort]' in after_model.split("if resume:")[0]
    assert 'env = {**child_env_base' in src
