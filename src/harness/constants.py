"""Kernel constants — the vocabulary every workflow and CLI shares.

Membership rule (inherited from rfc_agent.constants): a value lives here iff it is shared by more than
one module OR part of a recorded/public vocabulary someone greps for. Module-private details stay in
their module; run-dir path SHAPES are functions in contracts.py.

`rfc_agent.constants` re-exports ExitCode from here, so the public CLI contract has ONE definition:
two IntEnums with the same members but different identities is exactly how an exit code drifts.

Not here yet: `CoreDecision` (D12 splits rfc_agent's single Decision enum into a core vocabulary plus
per-workflow extensions). Until that slice lands, the single enum stays in rfc_agent.constants.
"""

from __future__ import annotations

from enum import IntEnum

MANIFEST = "manifest.json"
FLAGS_FILE = "flags.json"  # what was in effect for one invocation (harness.flags.Flags.dump)


class ExitCode(IntEnum):
    """The public CLI contract (README): the eval harness and the plugin command key off these.

    0 ok · 1 the composition is invalid · 2 a deterministic gate failed · 3 the engine failed/timed out.
    A human stopping at a checkpoint is 0 and resumable, NOT a failure.
    """

    OK = 0
    PIPELINE_CONFIG_ERROR = 1
    GATE_FAILURE = 2
    ENGINE_FAILURE = 3
