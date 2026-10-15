"""Claude Code hook scripts staged into headless stage runs (see guard_writes.py).

Deliberately import-light: `guard_writes` and `approve_readonly` run as subprocesses on every tool
call and now FAIL CLOSED if their imports break, so nothing heavy may sit on that path. The one thing
defined here is the broker command string, because two unrelated callers need to agree on it — the
Claude transport (`approve_readonly`, for its deny reasons) and the RFC workflow (which exports it to
the Pi guard as `HARNESS_POLICY_BROKER_HINT`). Two spellings of this path would mean pi denies the
harness's own sanctioned alternative with a reason naming a script that does not exist.
"""

from __future__ import annotations

import sys
from pathlib import Path

#: The ACI brokers a contained agent may invoke by exact form (harness.policy.allowlist.Broker).
BROKER_SCRIPTS: tuple[str, ...] = ("stage_tools.py", "repo_history.py")


def broker_command(script: str = "stage_tools.py") -> str:
    """`<interpreter> <abs path to the broker>` — the exact form the allowlist grants.

    The interpreter is `sys.executable`, i.e. the one running the harness: the broker imports
    rfc_agent, so a bare `python` would be the wrong one anywhere but a system install.
    """
    return f"{sys.executable} {Path(__file__).parent.resolve() / script}"
