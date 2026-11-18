"""`python -m workflows.testing.convert_flows promote <run dir> --folder <flows folder> [--apply]`

Copies the flows `convert_flows` ran green onto a new branch of the maestro-automation repo (a git worktree
beside the checkout; nothing committed or pushed). See `stages/convert_flows`.
"""

from __future__ import annotations

import sys
from pathlib import Path

if __package__ in (None, ""):  # run as a script: make `workflows.testing` importable from src/
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from workflows.testing.stages.convert_flows import main, promote  # noqa: E402,F401

if __name__ == "__main__":
    raise SystemExit(main())
