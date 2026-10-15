"""Window-aware resume-cap policy (0.22.42): the cap scales with the VERIFIED context window,
bounded by the configurable quality ceiling; anything unverifiable keeps the legacy 100K cap."""

from pathlib import Path

import rfc_agent.engine as engine_mod
from rfc_agent.engine import detect_effective_window, resumable, resume_cap
from rfc_agent.streams import StreamResult, parse_stream


class _Cfg(dict):
    def get(self, key, default=None):  # mimics config.get("A.B") flat access used in engine
        return super().get(key, default)


def _session(tmp_path: Path, name: str, tokens: int, compactions: int = 0) -> Path:
    logs = tmp_path / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    f = logs / f"{name}.session"
    f.write_text(f"sid-{name}\n{tokens}\n{compactions}")
    return f


def test_unknown_window_keeps_legacy_cap():
    assert resume_cap(_Cfg(), None) == 100_000


def test_window_smaller_than_buffer_keeps_legacy_cap():
    assert resume_cap(_Cfg(), 50_000) == 100_000


def test_verified_1m_window_yields_quality_ceiling():
    assert resume_cap(_Cfg(), 1_000_000) == 200_000


def test_config_ceiling_override_respected():
    assert resume_cap(_Cfg({"AGENT.RESUME_CONTEXT_CAP": 300_000}), 1_000_000) == 300_000


def test_old_200k_regime_reproduces_original_calibration():
    # a 200K window minus the 70K safety buffer ≈ 130K — same order as the original 100K cap,
    # and the mechanics term (window - buffer) binds before the quality ceiling does
    assert resume_cap(_Cfg(), 200_000) == 130_000


def test_detect_by_observation_beats_static_checks(tmp_path):
    # a sibling session ran past the legacy 200K regime with zero compactions -> window verified,
    # even though the model string carries no [1m]
    _session(tmp_path, "ingest", 265_846, compactions=0)
    assert detect_effective_window(tmp_path, "claude-sonnet-5") == 1_000_000


def test_detect_ignores_big_sessions_that_compacted(tmp_path):
    _session(tmp_path, "ingest", 240_000, compactions=2)
    assert detect_effective_window(tmp_path, "claude-sonnet-5") is None


def test_detect_static_1m_model(tmp_path, monkeypatch):
    monkeypatch.delenv("CLAUDE_CODE_DISABLE_1M_CONTEXT", raising=False)
    monkeypatch.delenv("ANTHROPIC_BASE_URL", raising=False)
    assert detect_effective_window(tmp_path, "claude-sonnet-5[1m]") == 1_000_000


def test_detect_static_downgraded_by_env(tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "http://gateway.local")
    assert detect_effective_window(tmp_path, "claude-sonnet-5[1m]") is None


def test_detect_static_official_base_url_is_not_a_gateway(tmp_path, monkeypatch):
    # the harness exports the official endpoint; it must not disqualify the window
    monkeypatch.delenv("CLAUDE_CODE_DISABLE_1M_CONTEXT", raising=False)
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://api.anthropic.com")
    assert detect_effective_window(tmp_path, "claude-sonnet-5[1m]") == 1_000_000


def test_resumable_150k_session_resumes_on_verified_window(tmp_path):
    # the exact case the old fixed cap wrongly forced FRESH: 100K < tokens <= 200K on a 1M window
    sid_file = _session(tmp_path, "generate", 150_000)
    sid, tokens, cap = resumable(sid_file, _Cfg(), 1_000_000)
    assert (sid, tokens, cap) == ("sid-generate", 150_000, 200_000)


def test_resumable_fat_session_still_goes_fresh(tmp_path):
    sid_file = _session(tmp_path, "generate", 317_963)
    sid, tokens, cap = resumable(sid_file, _Cfg(), 1_000_000)
    assert sid is None and cap == 200_000


def test_resumable_unverified_window_uses_legacy_cap(tmp_path):
    sid_file = _session(tmp_path, "generate", 150_000)
    sid, _, cap = resumable(sid_file, _Cfg(), None)
    assert sid is None and cap == 100_000


def test_stream_parser_counts_compaction_events():
    lines = (
        '{"type":"system","subtype":"init","session_id":"s1","model":"m"}\n'
        '{"type":"system","subtype":"compact_boundary"}\n'
        '{"type":"system","subtype":"compact_boundary"}\n'
        '{"type":"result","is_error":false,"result":"ok"}\n'
    )
    assert parse_stream(lines).compactions == 2


def test_session_file_roundtrip_with_compactions(tmp_path):
    f = tmp_path / "x.session"
    engine_mod.save_session(f, StreamResult(session_id="s", context_tokens=42, compactions=1))
    assert engine_mod.load_session(f) == ("s", 42, 1)
