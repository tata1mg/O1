"""Follow one agent's log until that agent ends — the command behind each agent card in Claude Code.

    python -m rfc_agent.watch <run_dir> <log>     # <log> = investigate.T1.run (logs/agents/<log>.log)

/rfc-agent:rfc starts one of these per agent as a background shell, so every agent shows as its own task
card: running while the agent works, finished when it does, and its output is the agent's tool calls and
messages. Exits on the agent's "■" line, when the run ends (logs/run.json rewritten), or after MAX_S.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

POLL_S = 1.0
APPEAR_S = 600  # the agent's log must show up within this
MAX_S = 3 * 3600


def follow(run_dir: Path, log: str, *, out=sys.stdout, poll_s: float = POLL_S, max_s: float = MAX_S) -> int:
    path = run_dir / "logs" / "agents" / f"{log}.log"
    run_json = run_dir / "logs" / "run.json"
    t0 = time.time()
    ran_before = run_json.stat().st_mtime if run_json.exists() else None
    pos = 0
    while True:
        if path.exists():
            with open(path, encoding="utf-8", errors="replace") as f:
                f.seek(pos)
                chunk = f.read()
                pos = f.tell()
            if chunk:
                out.write(chunk)
                out.flush()
                if any(line.split("  ", 1)[-1].startswith("■") for line in chunk.splitlines()):
                    return 0
        elif time.time() - t0 > APPEAR_S:
            print(f"no log for {log} after {APPEAR_S}s ({path})", file=out)
            return 1
        if run_json.exists() and run_json.stat().st_mtime != ran_before:
            print("■ the run ended", file=out)
            return 0
        if time.time() - t0 > max_s:
            print(f"■ stopped watching after {max_s}s", file=out)
            return 0
        time.sleep(poll_s)


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) != 2:
        print(__doc__, file=sys.stderr)
        return 2
    return follow(Path(argv[0]).expanduser(), argv[1])


if __name__ == "__main__":
    raise SystemExit(main())
