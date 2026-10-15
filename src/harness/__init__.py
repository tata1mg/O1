"""harness — the agent-agnostic, workflow-agnostic stage runner extracted from rfc_agent (2026-09-08).

Layers (dependency direction is strictly downward):
  contracts  → Workflow / Stage / RunContext / StageResult / failures / run-dir path shapes
  events     → the canonical trace-event schema every engine writes (logs/<label>.events.jsonl)
  engines    → Engine protocol + registry: "claude-code" (default), "pi", "replay" (tests/benchmarks)
  flags      → layered feature flags (defaults < workflow < config < env < CLI), introspectable as JSON
  workflows  → discovery of src/workflows/<name>/ packages exposing WORKFLOW
  runner, ledger, decisions, obs, cli  → arrive here progressively from rfc_agent

Rule: nothing under harness/ imports rfc_agent or workflows. Where a piece still lives in rfc_agent
(runner, ledger, obs, config), harness reaches it through an explicit, documented seam until it moves.
"""

__version__ = "0.1.0"
