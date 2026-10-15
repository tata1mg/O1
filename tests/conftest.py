"""Test-suite defaults."""

import os

# Never let a test reach the real model: the Investigate-3 agent runner refuses to start the real `claude` binary
# while this is set (a chained Review once did, through an un-stubbed config).
os.environ.setdefault("RFC_AGENT_NO_REAL_CLAUDE", "1")

# Langfuse tracing is ON by default and a traced run with no keys refuses to start; the suite never traces
# (tests/test_observe.py turns it on explicitly, with a fake client).
os.environ.setdefault("RFC_AGENT_LANGFUSE", "false")
