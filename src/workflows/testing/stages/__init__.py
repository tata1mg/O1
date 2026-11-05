"""The testing workflow's stages, plus the two helpers every stage shares.

Two kinds of stage live here:

  * **python stages** (`sources`, `render_xlsx`, `store_cases`, `derive_spec`, `execute_be`,
    `execute_web`, `publish_app`) — the whole step is deterministic code. Their `Stage.engine` is the
    function that does the work and returns `StageResult(engine_desc="python")`; no model is invoked, so
    there is nothing to prompt and nothing to retry.
  * **engine stages** (`generate_cases`, `evaluate`, `prerequisites`, `derive_ui`) — a headless agent runs
    with the stage's SKILL.md staged as its persona (D14). They call `ctx.engine.run(EngineRequest(...))`
    rather than binding a Claude primitive, so `--engine pi` works for this workflow the day the adapter
    lands (D4).
"""

from __future__ import annotations

import shutil
import time
from pathlib import Path
from typing import Any

from harness.contracts import EngineFailure, RunContext, Stage, StageResult

from .._io import atomic_write_json, parse_json

#: Decision names this workflow records. Declared on `Workflow.decisions` (D12).
DECISIONS: tuple[str, ...] = (
    "source_missing",            # a Step-0 inventory row came back missing/blocked
    "source_extracted",          # a contract/plan taken from the RFC's own section (no file of its own)
    "xlsx_skipped",              # openpyxl absent -> render skipped rather than failing the run
    "case_dropped_by_deriver",   # an exec:http case whose steps could not be transcribed
    "case_status_only",          # derived but weak: no body assertion
    "route_rewritten",           # route map turned a contract path into the deployed one
    "case_not_attached",         # spec entry with no runnable case in the store
    "cases_stored",              # upsert summary
    "case_kept_for_human",       # a reviewed/edited case the upload left alone (suggestion offered)
    "response_attached",         # the full response body was attached to the execution as evidence
    "presence_check_mismatch",   # model's presence verdict != the mechanical one
    "prerequisites_listed",      # prerequisites.md rendered: counts per section
    "ui_cases_selected",         # derive_ui: the CONFIRMED web UI cases it was given, and the snapshots
    "ui_case_not_automated",     # derive_ui: a case skipped with a reason (stays in the run for a person)
    "ui_evidence_attached",      # execute_web / publish_app: a screenshot, trace or timeline on an execution
    "app_case_unattributed",     # publish_app: a CONFIRMED app case no flow in the report covers
)


def read_json(path: Path, default: Any = None) -> Any:
    """Parse a run-dir JSON artifact tolerantly — for prompt building, where absence is normal.

    Gates use `read_json_gated` instead: a torn file must be NAMED, not silently read as absent.
    """
    value, why = parse_json(path)
    return default if why else value


def read_json_gated(path: Path, *, expect: type | None = None) -> tuple[Any, list[str]]:
    """-> (value, failures). The reader every gate uses, so damage is reported instead of guessed at.

    A missing file and a truncated one produce DIFFERENT messages: the first says the stage did not
    write it, the second says the bytes on disk are not JSON and which file to delete.
    """
    value, why = parse_json(path)
    if why:
        return None, [why]
    if expect is not None and not isinstance(value, expect):
        kind = "array" if expect is list else "object" if expect is dict else expect.__name__
        return None, [f"{path.name} is not a JSON {kind} (write the schema alone — no envelope object, "
                      f"no markdown fence)"]
    return value, []


def write_json(path: Path, data: Any) -> Path:
    """Atomically. Every artifact D8 calls canonical goes through here — see `_io` for why."""
    return atomic_write_json(path, data)


def feature_of(ctx: RunContext) -> str:
    """The feature key. --feature is required, so this is only empty in a malformed run."""
    return str(ctx.inputs.get("feature") or ctx.flag("testing.feature", "") or "")


def only_cases(ctx: RunContext) -> set[str]:
    """`testing.cases` as a set; empty = no restriction."""
    return {c.strip() for c in str(ctx.flag("testing.cases", "") or "").split(",") if c.strip()}


def python_stage(fn):
    """Wrap a `(stage, ctx) -> None | str` worker as a Stage.engine callable with timing + engine_desc."""

    def _engine(stage: Stage, ctx: RunContext) -> StageResult:
        t0 = time.monotonic()
        text = fn(stage, ctx) or ""
        return StageResult(final_text=text, duration_s=time.monotonic() - t0, engine_desc="python")

    return _engine


def run_engine_stage(stage: Stage, ctx: RunContext, skill_path: Path,
                     extra_roots: tuple[Path, ...] = ()) -> StageResult:
    """Invoke the resolved engine adapter for a persona stage.

    The adapter itself is wired by the runner (`ctx.engine`); this function only builds the request. That
    is the D4 contract: a stage-owned engine must call the injected adapter, never a hard-bound primitive.
    """
    from harness.engines import Containment, EngineRequest
    from harness.events import events_path

    if ctx.engine is None:
        raise EngineFailure(
            f"stage {stage.name!r} needs a model engine but none was resolved — run it through the "
            f"harness CLI (or inject one via RunContext.engine)."
        )
    if not skill_path.exists():
        raise EngineFailure(f"stage {stage.name!r}: persona missing at {skill_path}")
    _stage_persona(skill_path, ctx.run_dir, stage.name)
    req = EngineRequest(
        prompt=stage.build_prompt(ctx),
        cwd=ctx.run_dir,
        label=stage.name,
        trace_path=events_path(ctx.run_dir, stage.name),
        timeout_s=stage.timeout_s or int(ctx.flag("timeout_s", 2400)),
        skill_path=skill_path,
        model=(ctx.flag("model", "") or None),
        # The persona reads the sources (which live outside the run dir, in the RFC run) and writes only
        # into the run dir. Extra readable roots are the source files' parents.
        containment=Containment(
            writable_roots=(ctx.run_dir,),
            readable_roots=tuple(_source_roots(ctx)) + tuple(extra_roots),
        ),
        # The claude-code adapter (the default engine) still delegates to `run_claude`, which reads the
        # stage and the run context for MCP materialisation, effort and session bookkeeping. Without
        # this it refuses the request outright, so neither persona stage could run on the default engine.
        native_ctx={"stage": stage, "ctx": ctx},
    )
    absent = [o for o in stage.outputs if not (ctx.run_dir / o).exists()]
    res = ctx.engine.run(req)
    summary = res.summary
    # An errored session that wrote nothing is a dropped connection or an API outage, not a bad suite. Left
    # to the gate it read "declared output missing: qa/cases.json" and spent the stage's one automatic
    # revision on a second call into the same outage (2026-11-03, hdpo: two attempts, $1.46, no suite).
    # Raising stops the run at once with the real reason; re-running resumes this stage. "Wrote nothing" is
    # judged on the outputs absent before the call — some are Python's, written before or after it — and
    # anything the session did write still goes to the gate, as `rfc_agent.engine.claude_engine` does.
    if getattr(summary, "is_error", False) and absent and not any((ctx.run_dir / o).exists() for o in absent):
        reason = getattr(summary, "error_reason", None) or "unknown"
        raise EngineFailure(
            f"the model session for {stage.name!r} ended with an error ({reason}) before it wrote "
            f"{', '.join(absent)} — usually a dropped connection or an API outage, not the inputs; "
            f"re-run the same command to resume this stage (trace: {res.trace_path})"
        )
    return StageResult(
        final_text=getattr(summary, "final_text", "") or "",
        tool_calls=list(getattr(summary, "tool_calls", []) or []),
        cost_usd=getattr(summary, "cost_usd", None),
        usage=getattr(summary, "usage", None),
        duration_s=res.duration_s,
        engine_desc=res.engine_desc,
        prompt=res.prompt_sent or req.prompt,
        num_turns=getattr(summary, "num_turns", None),
        denials=getattr(summary, "denials", 0),
        compactions=getattr(summary, "compactions", 0),
    )


def _stage_persona(skill_path: Path, run_dir: Path, stage_name: str) -> None:
    """Copy the persona to `<run_dir>/.claude/skills/<stage>/SKILL.md`.

    `EngineRequest.skill_path` is how the pi adapter gets it (`--system-prompt`). The claude-code adapter
    ignores that field: a headless child discovers skills from its cwd's `.claude/skills/`, and
    `run_claude` leaves the staging to its caller. Staging here serves both.
    """
    dst = run_dir / ".claude" / "skills" / stage_name / "SKILL.md"
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(skill_path, dst)


def _source_roots(ctx: RunContext) -> list[Path]:
    """Directories holding the sources, so a containment policy can mount them read-only."""
    roots: list[Path] = []
    inv = read_json(ctx.path("qa/sources.json"), {}) or {}
    for row in inv.get("sources", []):
        p = row.get("path")
        if p:
            roots.append(Path(p).expanduser().parent)
    return sorted(set(roots))


def guidance_note(ctx: RunContext, stage_name: str) -> str:
    """The retry feedback the runner wrote for this stage, ready to append to a prompt."""
    from harness.contracts import guidance_path

    p = guidance_path(ctx.run_dir, stage_name)
    if not p.exists():
        return ""
    return ("\n\n## Feedback on your previous attempt (fix exactly these, keep everything else)\n\n"
            + p.read_text().strip() + "\n")
