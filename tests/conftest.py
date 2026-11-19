"""Test-suite defaults."""

import atexit
import os
import shutil
import tempfile
from pathlib import Path

# The suite must not need, or read, a developer's own ~/.config/rfc-agent/config.json: CI has none, and every test
# that went through get_config() failed there with "config.json not found". Point the config at a copy of the
# example one (a test that wants another config sets RFC_AGENT_CONFIG itself, as test_config.py does).
if "RFC_AGENT_CONFIG" not in os.environ:
    _cfg_dir = Path(tempfile.mkdtemp(prefix="rfc-agent-tests-"))
    shutil.copyfile(Path(__file__).resolve().parent.parent / "config.example.json", _cfg_dir / "config.json")
    (_cfg_dir / "config.json").chmod(0o600)
    os.environ["RFC_AGENT_CONFIG"] = str(_cfg_dir / "config.json")
    atexit.register(shutil.rmtree, _cfg_dir, ignore_errors=True)

# Never let a test reach the real model: the Investigate-3 agent runner refuses to start the real `claude` binary
# while this is set (a chained Review once did, through an un-stubbed config).
os.environ.setdefault("RFC_AGENT_NO_REAL_CLAUDE", "1")

# Langfuse tracing is ON by default and a traced run with no keys refuses to start; the suite never traces
# (tests/test_observe.py turns it on explicitly, with a fake client).
os.environ.setdefault("RFC_AGENT_LANGFUSE", "false")

# A developer's own GIT_REPOS_REMOTE must not change what the repo tests infer from their temp checkouts.
os.environ.setdefault("RFC_AGENT_GIT_REPOS_REMOTE", "")
