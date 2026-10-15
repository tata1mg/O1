"""Continuous byte-budget accounting: the ceiling is visible DURING authoring, not only at the
terminal gate (v1 binary-searched it for 4.3 min; v2's revision died at it). Two regimes
(2026-08-18): below the ceiling warn once per threshold; PAST the ceiling report exact bytes on
every edit — silence there forced 27 manual `wc -c` calls in one observed revision."""

import json
import subprocess
import sys
from pathlib import Path

from rfc_agent.hooks.feedback import parse as _fb

HOOK = Path(__file__).parents[2] / "src" / "rfc_agent" / "hooks" / "byte_budget.py"


def _run(run_dir, target):
    return subprocess.run(
        [sys.executable, str(HOOK)],
        input=json.dumps({"tool_name": "Write", "tool_input": {"file_path": str(target)}}),
        capture_output=True, text=True, timeout=10,
        env={"RFC_AGENT_RUN_DIR": str(run_dir), "PATH": "/usr/bin:/bin",
             # feedback is OFF by default since 2026-09-07 (trim-churn removal); these tests cover
             # the re-enabled path — the default-silent contract has its own test below
             "RFC_AGENT_GENERATE_BYTE_BUDGET": "1"},
    )


def _rfc(run_dir, kb: int) -> Path:
    p = run_dir / "out" / "rfc.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("## 1. Context\n" + "x" * (kb * 1024) + "\n## 8. Open Questions\nnone\n")
    return p


def test_below_the_first_pacing_threshold_is_silent(tmp_path):
    assert _run(tmp_path, _rfc(tmp_path, 10)).returncode == 0  # 10KB = 33% of a 30,720-byte ceiling


def test_pacing_thresholds_report_once_each_with_fill_allowance(tmp_path):
    """Write-to-budget (trim-spiral forensics 2026-09-03): 40%/70% hand the model the arithmetic it
    cannot do while writing — remaining target budget split across still-unfilled skeleton sections."""
    p = tmp_path / "out" / "rfc.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("## 1. Context\n" + "x" * (13 * 1024) + "\n## 5. Design\n<FILL:5.1>\n<FILL:5.2>\n")
    first = _run(tmp_path, p)  # ~43% of the ceiling
    assert first.returncode == 0 and "pacing" in _fb(first.stdout)
    assert "compose target ~25,600" in _fb(first.stdout)
    assert "2 skeleton section(s) still unfilled" in _fb(first.stdout)
    assert "per unfilled section" in _fb(first.stdout)
    assert "never trim" in _fb(first.stdout).lower()
    again = _run(tmp_path, p)
    assert again.returncode == 0 and _fb(again.stdout) is None, "one report per threshold — never a below-ceiling loop"
    grown = _rfc(tmp_path, 22)  # ~73%: the next threshold reports once
    second = _run(tmp_path, grown)
    assert second.returncode == 0 and "pacing" in _fb(second.stdout)
    assert "Remaining budget to the compose target" in _fb(second.stdout), "no FILL markers left"
    assert _run(tmp_path, grown).returncode == 0


def test_crossing_ninety_percent_warns_once_with_section_sizes(tmp_path):
    p = _rfc(tmp_path, 28)
    first = _run(tmp_path, p)
    assert first.returncode == 0 and _fb(first.stdout), "the warning is fed back to the model"
    assert "BYTE BUDGET" in _fb(first.stdout) and "30,720-byte" in _fb(first.stdout)
    assert "1. Context" in _fb(first.stdout), "per-section sizes name where the bytes went"
    assert _run(tmp_path, p).returncode == 0, "one warning per threshold — never a trim loop below the ceiling"


def test_past_the_ceiling_reports_exact_bytes_on_every_edit(tmp_path):
    _run(tmp_path, _rfc(tmp_path, 28))
    over = _run(tmp_path, _rfc(tmp_path, 35))
    assert over.returncode == 0 and "PAST its 30,720-byte hard ceiling" in _fb(over.stdout)
    assert "bytes PAST" in _fb(over.stdout), "over-amount is stated in exact bytes"
    # every subsequent edit reports again, with the edit's delta — the model never self-measures
    trimmed = _run(tmp_path, _rfc(tmp_path, 32))
    assert trimmed.returncode == 0 and _fb(trimmed.stdout)
    assert "changed it by -3,072 bytes" in _fb(trimmed.stdout)
    assert "never run wc" in _fb(trimmed.stdout).lower() or "NEVER run wc" in _fb(trimmed.stdout)


def test_dropping_back_under_the_ceiling_goes_silent_again(tmp_path):
    _run(tmp_path, _rfc(tmp_path, 35))
    assert _run(tmp_path, _rfc(tmp_path, 29)).returncode == 0, "back under: the 90% band already warned"


def test_files_without_a_budget_are_ignored(tmp_path):
    other = tmp_path / "findings" / "findings.md"
    other.parent.mkdir(parents=True)
    other.write_text("y" * 200_000)
    assert _run(tmp_path, other).returncode == 0
    assert _run(tmp_path, Path("/etc/hosts")).returncode == 0  # outside the run dir


def test_default_is_silent_but_writes_the_canary_marker(tmp_path):
    # owner decision 2026-09-07: no byte feedback unless RFC_AGENT_GENERATE_BYTE_BUDGET=1 — but the
    # state file must still appear, because ensure_hooks_alive keys on its existence
    import json as _json
    import subprocess as _sp
    import sys as _sys

    p = _rfc(tmp_path, 40)  # far past the ceiling — would have screamed before
    r = _sp.run([_sys.executable, str(HOOK)], input=_json.dumps(
        {"tool_input": {"file_path": str(p)}}), capture_output=True, text=True,
        env={"RFC_AGENT_RUN_DIR": str(tmp_path), "PATH": "/usr/bin:/bin"})
    assert r.returncode == 0 and not r.stdout.strip()  # silent: no feedback JSON at all
    assert (tmp_path / "logs" / "byte_budget.json").exists()  # canary marker written
