"""Machine + repo identity stamped into every result JSON, so a BEFORE/AFTER delta can be judged next to
what produced it (D9 parity methodology wants a mechanical diff, not a claimed one)."""

from __future__ import annotations

import platform
import subprocess
from datetime import UTC, datetime
from pathlib import Path


def git_sha(repo_root: Path | None = None) -> str:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"], cwd=repo_root, capture_output=True, text=True, timeout=5
        )
        return out.stdout.strip() or "unknown"
    except (OSError, subprocess.SubprocessError):
        return "unknown"


def machine_info() -> dict:
    return {
        "system": platform.system(),
        "machine": platform.machine(),
        "python": platform.python_version(),
        "processor": platform.processor() or platform.machine(),
        "cpu_count": _cpu_count(),
    }


def _cpu_count() -> int | None:
    import os

    return os.cpu_count()


def envelope(suite: str, results: list[dict], repo_root: Path | None = None) -> dict:
    return {
        "suite": suite,
        "git_sha": git_sha(repo_root),
        "timestamp": datetime.now(UTC).isoformat(),
        "machine": machine_info(),
        "results": results,
    }
