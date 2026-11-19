"""What one Claude call really cost, when the call resumed an earlier session.

`claude -p` puts `total_cost_usd` on its result event, and that figure is the running total of the
whole SESSION, not of this call. A resumed session (`--resume <id>`) therefore reports what every
earlier call in it cost plus this one. Adding those figures up counted the earlier calls again: on the
order-filters comparison a reviewer that really cost $2.56 was booked at $6.83, and Claude runs printed
about twice their real cost.

The fix is a small ledger per run, `logs/session_costs.json` ({session id: last total seen}). A call is
charged its total minus the total its session had already reported. A fresh call has nothing to
subtract. A resume whose earlier total is unknown (a run folder from before the ledger) is charged in
full, as before, since there is nothing better to go on.
"""

from __future__ import annotations

import contextlib
import json
import os
import threading
from pathlib import Path

LEDGER = Path("logs") / "session_costs.json"
_LOCK = threading.Lock()


def ledger_path(run_dir: Path) -> Path:
    return Path(run_dir) / LEDGER


@contextlib.contextmanager
def _locked(path: Path):
    """One writer at a time: threads in this process (parallel reviewers) and other processes alike."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with _LOCK, open(path.with_suffix(".lock"), "w") as fh:
        try:
            import fcntl

            fcntl.flock(fh, fcntl.LOCK_EX)
        except (ImportError, OSError):
            pass
        yield


def _load(path: Path) -> dict[str, float]:
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    return {str(k): float(v) for k, v in data.items() if isinstance(v, (int, float))} if isinstance(data, dict) else {}


def charge(run_dir: Path, session_id: str | None, total: float | None, *, resumed: str | None = None) -> float | None:
    """This call's own cost, given the session total its result event reported.

    `resumed` is the session id the call was started with (`--resume`); None for a fresh call. The new
    total is recorded under both ids, so a later resume of either finds it."""
    if total is None:
        return None
    total = float(total)
    ids = [s for s in dict.fromkeys((resumed, session_id)) if s]
    if not ids:
        return total
    path = ledger_path(run_dir)
    with _locked(path):
        data = _load(path)
        before = data.get(resumed) if resumed else None
        for sid in ids:
            data[sid] = total
        tmp = path.with_name(f"{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
        tmp.write_text(json.dumps(data, indent=1, sort_keys=True))
        os.replace(tmp, path)
    if before is None:
        return total
    # a total lower than before means the CLI started counting again; then the total is this call's
    return round(total - before, 6) if total >= before else total
