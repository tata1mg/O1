"""The domain contracts every layer shares: Workflow, Stage, RunContext, StageResult and the typed failures.

HARNESS KERNEL (extracted from rfc_agent 2026-09-08). Nothing in this module may import an engine, a
workflow, or Claude-Code-specific code. `rfc_agent.contracts` re-exports everything here (shim).

Split out of runner.py (AR-5, 2026-07-27): the types used to live beside the run loop, so every edge module
that needed them (engine, obs, decisions, the stage registry) either imported the loop module or deferred
the import to dodge a cycle — engine imports these types while the runner imports engine. Types have no
dependencies of their own, so a module holding only them breaks the cycle at the root instead of per-caller.

`runner` re-exports everything here, so `from .runner import Stage` keeps working.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from rfc_agent.config import Config  # TODO(harness): becomes harness.config once the loader moves
    from rfc_agent.streams import StreamResult  # any object with the same attribute names works (duck-typed)


# ---- The run-dir layout contract -------------------------------------------------------------------
# Files are this system's API (README "public contract"), so the path SHAPES are contracts too. Each one
# below used to be re-derived as an f-string at every call site (the stream shape in six places across
# three modules), and several are load-bearing for invariants: rotation, hygiene-wipe and the witness gate
# all agree on what a stream file is called ONLY because they call the same function. Module-private
# constants stay in their modules; enums and shared VALUES live in constants.py; the path SHAPES live
# here because the file layout is part of the domain contract.


def stream_path(run_dir: Path, stage_name: str) -> Path:
    """logs/<stage>.stream.jsonl — the engine's per-attempt event log (claude_engine postcondition)."""
    return run_dir / "logs" / f"{stage_name}.stream.jsonl"


def rotated_stream_path(run_dir: Path, stage_name: str, n: int) -> Path:
    """logs/<stage>.stream.<n>.jsonl — a prior attempt's stream, kept by revision mode as witness evidence."""
    return run_dir / "logs" / f"{stage_name}.stream.{n}.jsonl"


def stream_paths(run_dir: Path, stage_name: str) -> list[Path]:
    """Every attempt's stream (rotations + current) — the surface trace-backed gates witness over."""
    return sorted((run_dir / "logs").glob(f"{stage_name}.stream*.jsonl"))


def ledger_path(run_dir: Path, stage_name: str) -> Path:
    return run_dir / "logs" / f"{stage_name}.ledger.json"


def guidance_path(run_dir: Path, stage_name: str) -> Path:
    """guidance/<stage>.md — per-invocation human/gate feedback folded into the stage's next prompt."""
    return run_dir / "guidance" / f"{stage_name}.md"


class GateFailure(Exception):
    """A deterministic post-stage gate failed (CLI exit 2)."""


class EngineFailure(Exception):
    """The stage engine errored / timed out / produced no outputs (CLI exit 3)."""


class PipelineConfigError(Exception):
    """The requested pipeline/stage composition is invalid (CLI exit 1). Typed — never SystemExit —
    so orchestration stays embeddable; only cli.main terminates the process."""


class RunStopped(Exception):
    """The human stopped the run at a checkpoint (CLI exit 0, resumable via --run-dir)."""


@dataclass
class RunContext:
    run_dir: Path
    inputs: dict  # resolved CLI args (workflow-defined keys; the RFC workflow uses prd, jira, figma, ...)
    config: Config
    # The resolved engine adapter for this run (harness.engines.Engine). Stage-owned engines call
    # `ctx.engine.run(EngineRequest(...))` for every child. None only in legacy tests that never spawn.
    engine: object | None = None
    # Resolved feature flags (harness.flags.Flags). Stages read toggles here, never from os.environ.
    flags: object | None = None
    workflow: str = ""  # the workflow name this run belongs to (manifest + obs tag)

    def path(self, rel: str) -> Path:
        return self.run_dir / rel

    def flag(self, name: str, default=None):
        """Typed flag lookup, with a config fallback so unmigrated stages and embedders keep working.

        Three readings, in order:
          1. the resolved flags for this run (the normal path — `harness.cli` always resolves them);
          2. the flag's LEGACY config key, when it declares one. This is the case that matters for an
             embedder calling `run_pipeline` directly, and for every test that builds a RunContext by
             hand: without it, `ctx.flag("hook_canary")` looked up `config.get("hook_canary")`, found
             nothing, and silently ignored a config.json carrying `AGENT.HOOK_CANARY: false`;
          3. the name as-is, which is how an UNMIGRATED stage reading a dotted key still resolves.

        The `harness.flags` import is deferred: `contracts` sits below `flags` in the dependency order
        this package documents, and a module-level import would invert it for a lookup that only some
        calls need.
        """
        if self.flags is not None:
            try:
                return self.flags.get(name)
            except KeyError:
                pass
        if self.config is None:
            return default
        legacy = None
        try:
            from harness.flags import legacy_config_key

            legacy = legacy_config_key(name)
        except Exception:  # noqa: BLE001 — a flag lookup must never be the thing that breaks a run
            legacy = None
        for key in ([legacy] if legacy else []) + [name]:
            val = self.config.get(key)
            if val is not None:
                return val
        return default


@dataclass
class StageResult:
    final_text: str = ""
    tool_calls: list[dict] = field(default_factory=list)
    cost_usd: float | None = None
    usage: dict | None = None
    duration_s: float = 0.0
    engine_desc: str = "unknown"
    prompt: str = ""  # the kickoff prompt actually sent (for observability)
    num_turns: int | None = None
    denials: int = 0  # permission/write-guard denials (see StreamResult.denials)
    compactions: int = 0  # auto-compaction events — context memory was summarized away mid-stage
    context_tokens: int | None = None  # session context size at end of run (resume-cost signal)
    deliberation: list[str] = field(default_factory=list)  # non-tool thinking blocks (see StreamResult)
    tool_turns: int = 0  # assistant messages with >=1 tool_use (see StreamResult)
    multi_call_turns: int = 0  # of those, batched >1 — parallel-dispatch gauge (see StreamResult)

    @classmethod
    def from_stream(cls, stream: StreamResult, duration_s: float, engine_desc: str) -> StageResult:
        return cls(
            final_text=stream.final_text,
            tool_calls=stream.tool_calls,
            cost_usd=stream.cost_usd,
            usage=stream.usage,
            duration_s=duration_s,
            engine_desc=engine_desc,
            num_turns=stream.num_turns,
            denials=getattr(stream, "denials", 0),
            compactions=getattr(stream, "compactions", 0),
            context_tokens=getattr(stream, "context_tokens", None),
            deliberation=list(getattr(stream, "deliberation", []) or []),
            tool_turns=getattr(stream, "tool_turns", 0),
            multi_call_turns=getattr(stream, "multi_call_turns", 0),
        )


@dataclass(frozen=True)
class Stage:
    name: str  # also the skill dir name + manifest key
    requires: tuple[str, ...]  # run-dir-relative files that must exist BEFORE
    outputs: tuple[str, ...]  # run-dir-relative files that must exist AFTER (unconditional ones)
    build_prompt: Callable[[RunContext], str]
    gate: Callable[[RunContext], list[str]]  # returns failure strings; [] = pass
    mcp_servers: tuple[str, ...] = ()
    # THE swap point. None -> the run's default engine (flag `engine`, default "claude-code"; per-stage
    # override via flag `stage_engines`). A callable is a stage-owned engine (fan-out stages: GROUND shards,
    # REVIEW personas) — it receives the resolved engine adapter through `ctx.engine` and must call THAT
    # for every child invocation instead of a hard-bound primitive. Contract for any engine: write every
    # declared output, return a StageResult, and write the canonical trace `logs/<label>.events.jsonl`
    # (harness.events) — the ledger and trace-backed witness gates read it and degrade to a loud WARN when
    # absent. (The Claude adapter additionally keeps the raw `logs/<stage>.stream.jsonl` for back-compat.)
    engine: Callable[[Stage, RunContext], StageResult] | None = None
    disallowed_tools: str | None = None
    timeout_s: int | None = None  # None -> config AGENT.RUN_TIMEOUT. The runaway guard: there is no
    # per-stage cost cap by design — see the budget note in engine.claude_engine.
    add_repos_root: bool = False  # GROUND: expose local checkouts read-only via --add-dir
    # INGEST: drop --strict-mcp-config so the run inherits the installing user's MCP scope — that's where
    # already-authed OAuth servers (claude.ai Atlassian connector, figma plugin) live; a materialized server
    # def has no stored token headless (probed 2026-07-27). Inherited servers stay UNCALLABLE unless their
    # tools are in the read-only allowlist (headless MCP calls outside --allowedTools are auto-denied).
    inherit_user_mcp: bool = False
    ask_after: Callable[[RunContext], None] | None = None  # boundary ask hook (no-op when non-interactive)
    # optional SKILL.<variant>.md selector (e.g. extract's quote-grounding contract behind a config flag);
    # None -> SKILL.md. Lets one stage ship two prompt contracts that an A/B can switch per run.
    skill_variant: Callable[[RunContext], str | None] | None = None
    retry_on_gate_failure: int = 0  # bounded auto-retry: gate failures fed back via the guidance mechanism
    # cross-stage feedback: on gate failure, feed the failures to THIS earlier stage's guidance and re-run
    # from there (REVIEW's blockers re-run GENERATE). None -> same-stage retry. Bounded by retry_on_gate_failure.
    retry_stage: str | None = None
    # latency: when guidance exists AND previous outputs are on disk, keep them and let the agent make
    # targeted edits instead of a from-scratch rewrite (measured: a full GENERATE rewrite is ~46K output
    # tokens ≈ 8min; a revision is a fraction). Safe only where gates fully re-police the revised output.
    revise_on_retry: bool = False


@dataclass(frozen=True)
class Workflow:
    """A workflow = an ordered set of stages + the inputs they need + its own decision/flag vocabulary.

    The harness knows nothing about RFCs or test cases; a workflow package (src/workflows/<name>/) exposes
    exactly one `WORKFLOW` instance and the harness composes/runs it. Everything a workflow adds is
    additive: flags register under `<name>.*`, decisions extend the core Decision vocabulary, CLI inputs
    are added by `add_inputs(parser)`. Turning any stage off is `--disable-stage <stage>` (harness-level),
    never a code edit."""

    name: str
    stages: tuple[Stage, ...]  # every stage this workflow knows (variants included)
    default_pipeline: tuple[str, ...]  # ordered stage names run by default
    description: str = ""
    # argparse hook: add workflow-specific input flags (e.g. --prd, --feature); returns nothing
    add_inputs: Callable[[object], None] | None = None
    # resolve argparse Namespace -> RunContext.inputs dict (workflow owns its input vocabulary)
    resolve_inputs: Callable[[object], dict] | None = None
    # names of flags (harness.flags) this workflow registered — for `harness flags --workflow <name>`
    flags: tuple[str, ...] = ()
    # {flag name: value} — a workflow may RE-DEFAULT a harness flag for its own runs (D5's "workflow"
    # layer, above the flag default and below config/env/CLI). This is how a workflow keeps a stricter
    # posture than the kernel's: the RFC workflow re-defaults `obs_required` to True because an untraced
    # RFC run cannot be evaluated, while the harness default stays False for workflows with no sink.
    flag_defaults: dict = field(default_factory=dict)
    # decision names beyond harness.constants.CoreDecision that this workflow may record
    decisions: tuple[str, ...] = ()
    # run-dir-relative files that seed the inputs digest for stage 1 (RFC: raw/*.md)
    input_roots: tuple[str, ...] = ()
    # Engine-neutral env a workflow exports to EVERY child it spawns, computed from the RunContext.
    #
    # WHY a workflow hook and not kernel code: the containment POLICY needs three things only a
    # workflow can know — which package name marks a pinned harness entrypoint (so the broker is
    # exempt from the never-approvable classes), the exact broker command its deny reasons should
    # teach, and where its read-only checkouts live. Naming any of those in the kernel would be the
    # layering violation D2/D3 forbids and tests/test_harness_layering.py pins. The Claude transport
    # gets them from RFC_AGENT_* env vars its own hooks read; an engine-neutral adapter (pi) reads
    # HARNESS_POLICY_* from the child env. This hook is how a workflow supplies both.
    #
    # Returns {env var: value}; merged into EngineRequest.env by `rfc_agent.engine.engine_call`.
    containment_env: Callable[[RunContext], dict[str, str]] | None = None

    def stage(self, name: str) -> Stage:
        for s in self.stages:
            if s.name == name:
                return s
        raise PipelineConfigError(f"workflow {self.name!r} has no stage {name!r}; has {[s.name for s in self.stages]}")
