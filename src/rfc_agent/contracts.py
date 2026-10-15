"""Compatibility shim (harness extraction, 2026-09-08): the domain contracts now live in `harness.contracts`.
Every `from rfc_agent.contracts import X` and `from .contracts import X` keeps working unchanged.
New code imports from `harness.contracts` directly. Remove this shim once no in-repo caller uses it."""

from harness.contracts import *  # noqa: F401,F403
from harness.contracts import (  # noqa: F401  (explicit re-exports for `from .contracts import (...)` callers)
    EngineFailure,
    GateFailure,
    PipelineConfigError,
    RunContext,
    RunStopped,
    Stage,
    StageResult,
    Workflow,
    guidance_path,
    ledger_path,
    rotated_stream_path,
    stream_path,
    stream_paths,
)
