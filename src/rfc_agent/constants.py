"""The repo's shared constants and enums — the vocabulary, in one place.

Membership rule (so this never becomes the fleet's constants-dumping-ground coupling hub): a value lives
here iff it is (a) shared by more than one module, or (b) part of a recorded/public vocabulary someone
greps for (manifest statuses, decision names, exit codes). Module-private implementation details
(regexes, allowlists, marker strings) stay in their owning module; run-dir path SHAPES are functions in
contracts.py; MCP tool allowlists stay in mcp_tools.py (they are a security surface with their own rules).

Everything is a StrEnum or int so JSON serialization, f-strings and comparisons against on-disk values
work unchanged: json.dumps(StageStatus.OK) == '"ok"', f"decision:{Decision.BACK_JUMP}" == "decision:back_jump".
"""

from __future__ import annotations

from enum import StrEnum

# ONE definition of the public exit-code contract (the eval harness and the plugin command key off it):
# two IntEnums with the same members but different identities is exactly how an exit code drifts.
from harness.constants import MANIFEST, ExitCode  # noqa: F401  (re-exported: `from .constants import ExitCode`)

__all__ = ["MANIFEST", "CheckpointAction", "Decision", "ExitCode", "StageStatus"]


class StageStatus(StrEnum):
    """A stage's manifest verdict — what _stage_green and every manifest consumer key on."""

    OK = "ok"
    GATE_FAILED = "gate_failed"
    # the engine died (timeout/crash) before the gate could run: recorded so the crashed attempt's
    # spend and attempt count survive in the manifest — previously the one run class an engineer most
    # needs to price (the $40 run that died at minute 40) had no manifest entry at all
    ENGINE_FAILED = "engine_failed"


class CheckpointAction(StrEnum):
    """What the human chose at a boundary checkpoint (ask.boundary_checkpoint -> runner)."""

    CONTINUE = "continue"
    RERUN = "rerun"
    QUIT = "quit"


class Decision(StrEnum):
    """Every recorded harness decision (logs/decisions.jsonl + Langfuse `decision:<name>` events).

    This enum IS the decision vocabulary — per the project's decision-signals rule ("record what was
    decided and why"), a decision name that isn't listed here doesn't exist. Checkpoint overrides are the
    one dynamic family: `checkpoint_{CheckpointAction}` (e.g. checkpoint_rerun, checkpoint_quit)."""

    BACK_JUMP = "back_jump"
    AUTO_RETRY_ON_GATE_FAILURE = "auto_retry_on_gate_failure"
    REVISE_IN_PLACE = "revise_in_place"
    GATE_DESPITE_ENGINE_ERROR = "gate_despite_engine_error"
    AUTO_QUALIFIED_CITATIONS = "auto_qualified_citations"
    QUALIFIED_BARE_CITATIONS = "qualified_bare_citations"
    REPAIRED_CITATIONS_FROM_FINDINGS = "repaired_citations_from_findings"
    GROUND_FANOUT = "ground_fanout"  # harness-level shard fan-out engaged (shard names attached)
    GROUND_LEAD_WAVE = "ground_lead_wave"  # cross-scope leads routed to a second-wave shard
    GROUND_LEADS_OUT_OF_SCOPE = "ground_leads_out_of_scope"  # tips naming unapproved repos: surfaced, never dispatched
    GENERATE_GAPFILL = "generate_gapfill"  # composer-recorded gaps researched by harness children
    GROUND_FANOUT_FALLBACK = "ground_fanout_fallback"  # DISCOVER output unusable -> single FULL scan
    RESUMED_REVISION = "resumed_revision"  # revision applied via --resume (warm context, not a cold rerun)
    GROUND_ANCHOR_PREPASS = "ground_anchor_prepass"  # deterministic rg anchor->repo map fed to DISCOVER
    GROUND_DETERMINISTIC_ASSEMBLY = "ground_deterministic_assembly"  # findings.md composed in Python; MERGE edits
    INGEST_FIGMA_FANOUT = "ingest_figma_fanout"  # snapshot renders read by parallel children; figma.md merged in Python
    PATCH_REVISION = "patch_revision"  # all latest gate failures mechanical -> constrained one-edit-per-failure pass
    BONUS_PATCH_RETRY = "bonus_patch_retry"  # budget exhausted, residue all-mechanical -> one bonus pass, not a stop
    RELABELED_DOCS = "relabeled_docs"  # symbolic CHANGE/Q ids topo-sorted + numbered harness-side
    HEALED_CITATION_LINES = "healed_citation_lines"  # out-of-range line number repaired to the symbol's true line
    FAST_PATH = "fast_path"  # small spec (<= FAST_PATH_REQ_MAX REQs) -> 2 shards / 2 personas; gates unchanged
    STAGE_CRASHED = "stage_crashed"  # engine died pre-gate; partial spend recovered from the stream files
    GREEN_SKIP = "green_skip"  # stage up to date (green + inputs digest unchanged) — skipped, not run
    DIGEST_MISMATCH_RERUN = "digest_mismatch_rerun"  # green stage re-run because its inputs changed
    RESUME_SKIPPED = "resume_skipped"  # revision went fresh: saved session exceeded the resume cap
    STEERING_DELIVERED = "steering_delivered"  # operator mid-run steering handed to the running agent
    STEERING_SKIPPED = "steering_skipped"  # steering record seen but not delivered (lane mismatch)
    STEERING_EXPIRED = "steering_expired"  # undelivered at invocation end — TTL swept it (0.24.30)
    POST_RESULT_TURN = "post_result_turn"  # a turn ran after the stage's terminal result event
    EXTRACT_FANOUT = "extract_fanout"  # Layer 3: N parallel quote-grounded samples (EXTRACT.SAMPLES)
    EXTRACT_RECONCILE = "extract_reconcile"  # one list-wise reconcile child over harness-built blocks
    EXTRACT_REQUOTE = "extract_requote"  # pre-gate targeted repair of paraphrased quotes (EXTRACT.REQUOTE)
    EXTRACT_HEDGE = "extract_hedge"  # hedged sampling: first K samples unioned, stragglers killed (EXTRACT.UNION_FIRST)
    EXTRACT_RECONCILE_FALLBACK = "extract_reconcile_fallback"  # reconcile child failed -> plain union, no merging
    HOOK_CANARY = "hook_canary"  # end-to-end PostToolUse feedback probe: alive/dead per CLI version
    STAGE_DISABLED = "stage_disabled"  # flag `disabled_stages` removed this stage from the pipeline
    SCOPE_CONSENT_REQUIRED = "scope_consent_required"  # discovered repos + explicit --repos + no
    # recorded approval on a non-interactive run -> GROUND stops instead of deciding scope itself
