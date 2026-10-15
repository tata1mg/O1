"""The `claude-code` engine adapter — the existing `claude -p` primitive behind the Engine protocol (D7).

WHY this is a WRAPPER and not a rewrite: the CLI path is what 794 offline tests and months of A/B numbers
validate. `rfc_agent.engine.run_claude` carries a year of hard-won behaviour — the Figma tool-binding race
check, the resume/cold fallback, the steering pump on stdin, the dead-child backstop, effort resolution,
the kickoff-on-stdin protocol. Reimplementing any of it against the new protocol would trade verified
behaviour for tidiness. So this adapter TRANSLATES: EngineRequest -> run_claude arguments, StreamResult ->
canonical trace + EngineResult.

THE SEAM (documented per harness/__init__.py's rule that nothing under harness/ imports rfc_agent):
this module is the ONE exception, and the import is lazy and confined to `_impl()` below. It exists
because the Claude primitive has not moved into the kernel yet. When it does, only `_impl` changes.

Two entry points, deliberately:

  `run(req)`        ONE invocation, for stage-owned fan-out engines (GROUND shards, REVIEW personas) and
                    for any caller that has an EngineRequest. Writes the canonical trace.
  `run_stage(s, c)` the whole stage the way the runner has always run it, by delegating to
                    `rfc_agent.engine.claude_engine`. The runner uses THIS so a default `rfc-agent`
                    invocation produces byte-identical manifests (D9b) — the stage-level logic
                    (revision/patch/resume branching, emission mode, session save) lives there, and
                    moving it is a later slice. The canonical trace is written afterwards, from the raw
                    stream the stage already wrote.
"""

from __future__ import annotations

import shutil
import subprocess
import time
from pathlib import Path

from ..contracts import stream_path, stream_paths
from ..events import events_path, summarize
from . import Containment, EngineRequest, EngineResult

NAME = "claude-code"
# `claude --version` on a healthy install answers in well under a second; a hung binary is a doctor
# finding, not something to wait on.
_VERSION_TIMEOUT_S = 10


def _impl():
    """The rfc_agent Claude primitive. Lazy import = the ONE documented kernel->rfc_agent seam."""
    from rfc_agent import engine as _engine

    return _engine


class ClaudeCodeEngine:
    """Engine adapter over `claude -p --output-format stream-json`."""

    name = NAME

    def __init__(self, binary: str | None = None) -> None:
        # None -> whatever run_claude resolves from config (AGENT.CLAUDE_BIN, default "claude"); this
        # parameter exists for `doctor()` and for tests that point at a fake binary.
        self.binary = binary

    # ---- protocol ---------------------------------------------------------------------------------

    def supports_resume(self) -> bool:
        return True

    def doctor(self) -> list[str]:
        """Preflight: is the binary there, and does it answer? [] = healthy."""
        binary = self.binary or "claude"
        found = shutil.which(binary) or (binary if Path(binary).is_file() else None)
        if not found:
            return [f"`{binary}` not found on PATH — install Claude Code "
                    f"(https://claude.com/claude-code) or set AGENT.CLAUDE_BIN to its path."]
        try:
            res = subprocess.run([found, "--version"], capture_output=True, text=True,
                                 timeout=_VERSION_TIMEOUT_S)
        except (OSError, subprocess.SubprocessError) as exc:
            return [f"`{found} --version` failed: {exc}"]
        if res.returncode != 0:
            return [f"`{found} --version` exited {res.returncode}: "
                    f"{(res.stderr or res.stdout).strip()[:200]}"]
        return []

    def version(self) -> str:
        binary = shutil.which(self.binary or "claude") or (self.binary or "claude")
        try:
            res = subprocess.run([binary, "--version"], capture_output=True, text=True,
                                 timeout=_VERSION_TIMEOUT_S)
            return (res.stdout or res.stderr).strip()
        except (OSError, subprocess.SubprocessError):
            return "unknown"

    def run(self, req: EngineRequest) -> EngineResult:
        """One headless invocation. `req.native_ctx` must carry {"stage": Stage, "ctx": RunContext}.

        `raw_trace_path` defaults to the stream path the ledger already reads, so an adapter-driven
        invocation is indistinguishable on disk from a direct `run_claude` call — that is what keeps the
        witness gates working while the fan-out call sites migrate one at a time (D4).
        """
        eng = _impl()
        stage, ctx = self._native(req)
        raw = req.raw_trace_path or stream_path(ctx.run_dir, req.label)
        raw.parent.mkdir(parents=True, exist_ok=True)
        t0 = time.monotonic()
        stream = eng.run_claude(
            stage, ctx, req.prompt, raw, req.timeout_s,
            live=req.live,
            label=req.label,
            procs=req.procs,
            resume=req.resume_session,
            model=req.model,
            obs_parent=req.obs_parent,
            extra_env=dict(req.env) or None,
            steer_epoch=req.steer_epoch,
            effort=req.effort,
        )
        duration = time.monotonic() - t0
        # `run_claude` already wrote the trace at its default location, so this is usually the second
        # translation of the same stream. It is kept unconditional on purpose: skipping it when the
        # destination happens to match would make the adapter's postcondition depend on an inner
        # function's side effect, and the cost is one small file re-read next to a model call. The
        # write itself can never raise (see write_trace).
        want = req.trace_path or events_path(ctx.run_dir, req.label)
        trace = self.write_trace(raw, want) or want
        return EngineResult(
            summary=summarize(trace),
            duration_s=duration,
            engine_desc=self.describe(stream, ctx),
            prompt_sent=stream.prompt_sent or req.prompt,
            trace_path=trace,
            raw_trace_path=raw,
            native=stream,
        )

    # ---- stage-level delegation (byte-identical manifests) ----------------------------------------

    def run_stage(self, stage, ctx):
        """Run a whole stage exactly as `rfc-agent` always has, then write the canonical trace.

        Returns the legacy `StageResult` untouched — every manifest field is produced by the same code
        as before this adapter existed, which is what makes the parity suite's manifest golden hold.
        """
        eng = _impl()
        result = eng.claude_engine(stage, ctx)
        # Translate EVERY attempt's stream, rotations included: revision mode keeps prior attempts as
        # witness evidence, and a trace that covered only the last attempt would under-report the
        # corpus the witness gates check against.
        from .claude_translate import trace_label

        for raw in stream_paths(ctx.run_dir, stage.name):
            self.write_trace(raw, events_path(ctx.run_dir, trace_label(raw.name)))
        return result

    # ---- helpers ----------------------------------------------------------------------------------

    @staticmethod
    def write_trace(raw_stream: Path, trace_path: Path) -> Path | None:
        """Translate one raw stream, and NEVER raise.

        `run_stage`'s loop runs AFTER a successful stage, outside the runner's `except EngineFailure`,
        so an exception here skipped the ledger, the gate, the manifest record and the crash
        accounting, and exited with a traceback — losing a whole stage's spend to a failed write
        (review SHOULD-FIX 1.4). `rfc_agent.engine.write_canonical_trace` already owns this rule one
        layer down; routing through it keeps ONE guard rather than two half-guards.
        """
        eng = _impl()
        return eng.write_canonical_trace(trace_path.parent.parent, trace_path.name.split(".events")[0],
                                         raw_stream)

    @staticmethod
    def describe(stream, ctx) -> str:
        """`claude-code:<model the child ACTUALLY used>`.

        The model matters more than it looks: with no --model the child inherits the operator's ambient
        Claude Code default, and a manifest reading `claude:default` hides which model produced a run —
        fatal for comparing eval runs across branches (observed 2026-07-27).
        """
        model = (getattr(stream, "model", None)
                 or (ctx.inputs.get("model") if getattr(ctx, "inputs", None) else None)
                 or (ctx.config.get("AGENT.DEFAULT_MODEL") if getattr(ctx, "config", None) else None)
                 or "unknown")
        return f"{NAME}:{model}"

    @staticmethod
    def _native(req: EngineRequest):
        stage, ctx = req.native_ctx.get("stage"), req.native_ctx.get("ctx")
        if stage is None or ctx is None:
            raise ValueError(
                "the claude-code adapter needs native_ctx={'stage': Stage, 'ctx': RunContext} on the "
                "EngineRequest — `run_claude` still owns MCP materialisation, effort resolution and "
                "session bookkeeping, all of which read the stage and the run context. See this "
                "module's docstring for why the wrapper is shaped this way."
            )
        return stage, ctx


def containment_for(stage, ctx) -> Containment:
    """The Containment a stage runs under today, expressed in kernel terms.

    Nothing consumes this yet on the Claude path — the CLI transport is assembled inside `run_claude`
    from the same facts. It exists so the Pi adapter and `harness doctor` can ask ONE question
    ("what is this stage allowed to touch?") and get the same answer regardless of engine.
    """
    eng = _impl()
    repos = ctx.inputs.get("repos_root") if getattr(ctx, "inputs", None) else None
    readable = (Path(str(repos)).expanduser(),) if (repos and stage.add_repos_root) else ()
    servers = eng.stage_mcp_servers(stage, ctx)
    from rfc_agent.mcp_tools import allowed_tools

    deny = eng.deny_list(stage.disallowed_tools, ctx.config.get("AGENT.EXTRA_DISALLOWED_TOOLS"))
    return Containment(
        writable_roots=(ctx.run_dir.resolve(),),
        readable_roots=readable,
        allowed_tools=tuple(allowed_tools(servers)) if servers else (),
        disallowed_tools=tuple(t for t in deny.split(",") if t),
        mcp_servers={s: {} for s in servers},
        strict_mcp=not stage.inherit_user_mcp,
        readonly_shell=True,
        hooks_enabled=bool(ctx.flag("hooks", True)),
        disabled_hooks=tuple(ctx.flag("hooks_off", []) or ()),
    )
