"""The staged pipeline: run loop, manifest, resume. The Stage contract itself lives in contracts.py.

Stages are disk-in/disk-out and idempotent: a stage always fully regenerates its declared outputs; a green
stage (manifest ok + outputs still on disk) is skipped on rerun; `--stages <name>` re-runs exactly that stage
against whatever is on disk. Stages communicate ONLY via files under the run dir — that plus the swappable
`engine` callable is the modularity seam (an M2 stage with a different engine is a folder + a registry edit).

The domain types are re-exported below so `from .runner import Stage` (used across stages/, engine, tests)
keeps working after the contracts.py split.
"""

from __future__ import annotations

import contextlib
import json
import re
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

from harness.events import events_path, events_paths

from .constants import MANIFEST, CheckpointAction, Decision, StageStatus
from .contracts import (
    EngineFailure,
    GateFailure,
    PipelineConfigError,
    RunContext,
    RunStopped,
    Stage,
    StageResult,
    guidance_path,
    ledger_path,
    rotated_stream_path,
    stream_path,
    stream_paths,
)

__all__ = [
    "MANIFEST",
    "EngineFailure",
    "GateFailure",
    "PipelineConfigError",
    "RunContext",
    "RunStopped",
    "Stage",
    "StageResult",
    "run_pipeline",
]


def _load_manifest(run_dir: Path) -> dict:
    p = run_dir / MANIFEST
    return json.loads(p.read_text()) if p.exists() else {}


def _atomic_write(path: Path, text: str) -> None:
    """Unique-temp + fsync + os.replace, same directory. Atomic against process death AND durable
    against power loss (fsync before rename — os.replace alone only orders the rename, and some
    filesystems commit it ahead of the data, publishing a zero-length file). The temp name carries
    the pid so two invocations sharing a run dir cannot interleave writes into one temp file
    (0.23.3 review — the fixed name reintroduced the torn-file class atomically)."""
    import os

    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def _write_manifest(run_dir: Path, manifest: dict) -> None:
    """Atomic + durable (see _atomic_write): the manifest is the run dir's accounting source of
    truth, and a truncate-then-write torn by a kill left unparseable JSON that BRICKED the run dir —
    every later invocation crashed at load (0.23.2; the git/SQLite protect-before-work pattern).
    Concurrent invocations on one run dir remain unsupported, but can no longer corrupt the file."""
    _atomic_write(run_dir / MANIFEST, json.dumps(manifest, indent=2))


def _dumpable_flags(workflow: str) -> set[str] | None:
    """Flag names whose values belong in THIS run's flags.json.

    Owners, not name prefixes: `harness` (the core loop), every registered engine adapter, and the
    workflow being run. None means "no restriction", for a caller that passed no workflow name — the
    honest answer when we do not know which workflow this is.
    """
    from harness import engines as _engines
    from harness import flags as _flags

    if not workflow:
        return None
    return _flags.names_for({"harness", workflow, *_engines.names()})


def _rotate_trace(run_dir: Path, stage_name: str, n: int) -> None:
    """Move `logs/<stage>.events.jsonl` aside as attempt `n`, mirroring the stream rotation.

    Best-effort: the trace is evidence, and failing to preserve it must not abort a revision that is
    otherwise ready to run (the gates degrade loudly on a thin corpus, which is the same signal).
    """
    with contextlib.suppress(OSError):
        cur = events_path(run_dir, stage_name)
        if cur.exists():
            cur.rename(events_path(run_dir, f"{stage_name}.attempt{n}"))


def _stage_green(stage: Stage, run_dir: Path, manifest: dict) -> bool:
    entry = manifest.get(stage.name)
    return bool(entry and entry.get("status") == StageStatus.OK and all((run_dir / o).exists() for o in stage.outputs))


def _inputs_digest(stages: list[Stage], stage: Stage, run_dir: Path) -> str:
    """Digest of everything this stage's judgment anchors on: every EARLIER stage's declared outputs,
    plus the whole raw/ tree when ingest is upstream (extract reads ALL raw files, not just ingest's
    one declared output). Green-skip compares this against the digest recorded when the stage went
    green — without it, `--stages extract --force` then a resume let a green GROUND carry findings
    tagged against the OLD REQ numbering (the same poisoning class the fragment cache closed in
    0.22.37, still open via the manifest until now)."""
    import hashlib

    names = [s.name for s in stages]
    idx = names.index(stage.name) if stage.name in names else len(names)
    earlier = stages[:idx]
    rels = sorted({o for s in earlier for o in s.outputs})
    files = [run_dir / r for r in rels]
    if any(o.startswith("raw/") for s in earlier for o in s.outputs):
        files += sorted((run_dir / "raw").glob("*.md"))
    h = hashlib.sha256()
    for f in sorted(set(files)):
        if f.is_file():
            h.update(str(f.relative_to(run_dir)).encode() + b"\0")
            h.update(f.read_bytes())
            h.update(b"\0")
    return h.hexdigest()[:16]


_SEAM_MARKER = "TODO(harness-engine-seam)"


def _has_unmigrated_sites(stage: Stage) -> bool:
    """Does this stage still invoke the Claude primitive directly somewhere?

    Read off the CODE, which is the only source of truth that cannot drift: the migration marker lives
    beside each unmigrated call site, in the same module as the stage's fan-out engine. When the last
    site in a module moves to `engine_call`/`engine_stage`, the marker goes with it and this returns
    False on its own — no per-stage migration table to forget to update.

    Any failure to read the source answers False: a warning is a courtesy, and a stage must never fail
    to run because introspection did not work.
    """
    import inspect

    try:
        module = sys.modules.get(getattr(stage.engine, "__module__", "") or "")
        return module is not None and _SEAM_MARKER in inspect.getsource(module)
    except (OSError, TypeError):
        return False


def _resolve_stage_engine(stage: Stage, ctx: RunContext, default_engine):
    """Which callable runs THIS stage: `(stage, ctx) -> StageResult`.

    Three cases, in precedence order (D4 — swapping an engine is an argument, never a code edit):

      1. `stage.engine` is set    a stage-owned fan-out engine (GROUND shards, REVIEW personas). It gets
                                  the resolved adapter through `ctx.engine` and calls THAT per child.
      2. flag `stage_engines`     per-stage override, e.g. --stage-engine review=pi.
      3. the run's engine         the adapter the caller resolved from the `engine` flag.

    With no adapter passed at all (every pre-harness caller, and tests/test_runner.py) this returns
    `claude_engine` — the exact callable the runner has always used. That is what keeps a default
    `rfc-agent` invocation byte-identical in the manifest.
    """
    if stage.engine is not None:
        # D4's migration is per CALL SITE, and a fan-out stage has several (a main pass plus N
        # children). A site that still names `run_claude`/`claude_engine` is bound to the Claude
        # primitive no matter what --engine says; `engine_call`/`engine_stage` route through the
        # adapter. Warn only when this stage still has unmigrated sites AND a non-default engine was
        # asked for: a silent no-op would let an operator (or an A/B) believe a different engine ran
        # the stage, while warning about a fully migrated stage would train people to ignore it.
        wanted = (ctx.flag("stage_engines", {}) or {}).get(stage.name)
        run_engine = str(ctx.flag("engine", "claude-code") or "claude-code")
        if (wanted or run_engine != "claude-code") and _has_unmigrated_sites(stage):
            print(f"[{stage.name}] WARN engine={wanted or run_engine!r} is only PARTLY honoured: this "
                  f"stage still has call sites bound to the Claude primitive. Those run on claude-code "
                  f"regardless — grep `TODO(harness-engine-seam)` in {stage.engine.__module__} for the "
                  f"remaining work (D4).", file=sys.stderr)
        return stage.engine
    from .engine import claude_engine  # local import: engine pulls subprocess machinery only when needed

    engine = default_engine
    override = (ctx.flag("stage_engines", {}) or {}).get(stage.name)
    if override:
        from harness.engines import engine_from_flags

        engine = engine_from_flags(override, ctx.flags)
    if engine is None:
        return claude_engine
    run_stage = getattr(engine, "run_stage", None)
    if run_stage is None:
        raise PipelineConfigError(
            f"engine {getattr(engine, 'name', engine)!r} cannot run stage {stage.name!r}: an engine "
            f"driving a whole stage must expose run_stage(stage, ctx). Give the stage its own "
            f"`engine=` callable, or use an adapter that implements run_stage."
        )
    return run_stage


def run_pipeline(stages: list[Stage], ctx: RunContext, selected: list[str] | None = None,
                 force: bool = False, *, engine=None, flags=None, workflow: str = "") -> None:
    """Run the selected stages in registry order. Raises GateFailure / EngineFailure.

    `engine` is the resolved engine adapter for this run (harness.engines.Engine), `flags` the resolved
    feature flags (harness.flags.Flags) and `workflow` the workflow name. All three are OPTIONAL and
    default to exactly the behaviour this function had before the harness kernel existed: no adapter
    (claude_engine), no flags (every toggle resolves to its default through RunContext.flag's config
    fallback), no workflow tag. That is deliberate — the strangler pattern (D2) requires the legacy
    entry point to keep working untouched while `harness run` grows beside it.
    """
    from . import ask, decisions, obs
    from .ledger import write_ledger

    ctx.run_dir.mkdir(parents=True, exist_ok=True)
    # the run's resolved context, so stages and fan-out engines read toggles from ctx, never os.environ
    if engine is not None:
        ctx.engine = engine
    if flags is not None:
        ctx.flags = flags
        # flags.json: WHAT WAS IN EFFECT for this invocation, with each value's winning layer (D5).
        # Written before any stage runs — a run that dies in stage one must still be reproducible.
        #
        # SCOPED to the flags that could have affected THIS run: the harness core, the engine adapters
        # and the workflow being run. Workflow discovery imports every package under workflows/, so the
        # unscoped dump put another workflow's flags — including its store CREDENTIALS — into every run
        # dir, `rfc-agent` runs included (review BLOCKER 1.1). Secret values are redacted on top of
        # that, in `value` and in every `layers` entry.
        _atomic_write(ctx.run_dir / "flags.json", flags.dump(only=_dumpable_flags(workflow)))
    if workflow:
        ctx.workflow = workflow
    # guidance is per-INVOCATION feedback (this run's gate/review failures + human checkpoint notes), not
    # durable state. Clear it at the start of every invocation: otherwise a later --force / --stages rerun
    # finds last run's guidance + outputs still on disk and silently enters REVISION MODE against STALE
    # blockers instead of regenerating cleanly (and the file keeps compounding across runs).
    gdir = ctx.run_dir / "guidance"
    if gdir.is_dir():
        for f in gdir.glob("*.md"):
            f.unlink()
    manifest = _load_manifest(ctx.run_dir)
    to_run = [s for s in stages if selected is None or s.name in selected]
    # flag `disabled_stages`: drop stages from THIS invocation. Their requires/outputs contract still
    # applies to their neighbours, so a disabled stage whose outputs are missing fails the next stage's
    # requires check loudly rather than producing a quietly degraded run.
    disabled = set(ctx.flag("disabled_stages", []) or ())
    if disabled:
        for s in [s for s in to_run if s.name in disabled]:
            decisions.record(ctx, s.name, Decision.STAGE_DISABLED,
                             why="flag `disabled_stages` removed this stage from the pipeline for this "
                                 "invocation; downstream `requires` checks still apply")
            print(f"[{s.name}] disabled by flag — skipping", file=sys.stderr)
        to_run = [s for s in to_run if s.name not in disabled]
    if selected is not None:
        unknown = set(selected) - {s.name for s in stages}
        if unknown:
            raise PipelineConfigError(f"unknown stage(s): {sorted(unknown)} — known: {[s.name for s in stages]}")

    names = [s.name for s in to_run]
    must_rerun: set[str] = set()  # stages a back-jump forces past the green-skip check
    seeded_revise: set[str] = set()  # resume-from-red already recorded REVISE_IN_PLACE — don't double-emit
    back_jumps_used: dict[str, int] = {}  # persists across re-visits — bounds the generate<->review cycle
    bonus_patch_used: set[str] = set()  # one bonus mechanical-residue patch per stage per invocation
    # mint/read run identity BEFORE the trace opens: identity.json is the harness's own record (the
    # ids build/test will inherit), so it must exist whether or not observability is configured.
    from . import identity

    identity.load(ctx)
    # ExitStack callback: trace output is written on FAILED runs too (those are the ones people
    # investigate), and still inside the run trace so it lands on the right span.
    ran_stages: set[str] = set()  # stages THIS invocation executes — rework scores emit only for these
    with obs.run_trace(ctx), contextlib.ExitStack() as stack:
        stack.callback(obs.finish_run, ctx, manifest, ran_stages)

        # operator-wait accounting on EVERY exit path (LIFO: runs before finish_run, so the trace
        # sees it too): the sum of every blocking ask this run dir has held, folded into the
        # manifest — previously ~28 min of a 109-min run was excavatable only from four log files
        def _flush_operator_wait() -> None:
            summary = ask.operator_wait_summary(ctx.run_dir)
            if summary:
                manifest["operator_wait"] = summary
                _write_manifest(ctx.run_dir, manifest)
        stack.callback(_flush_operator_wait)
        # invocation-scope steering TTL: sweep undelivered steers on EVERY exit path (gate failure
        # and crash included) — an unswept control message would deliver to a future run out of
        # context. LIFO: this runs before finish_run, so the expiry feed lines exist pre-flush.
        from . import steer as _steer
        stack.callback(_steer.expire_undelivered, Path(ctx.run_dir), ctx)
        # the operator's choices since the LAST invocation (answers given, fix-vs-proceed, ratings)
        # land as events on THIS trace — the human half of every signal loop
        obs.operator_actions_flush(ctx)
        # crash-only recovery (0.23.2): a previous invocation that died without recording its
        # attempt (SIGKILL, second Ctrl-C, power loss) left an intent breadcrumb — reconcile it into
        # an engine_failed entry NOW, from durable evidence, instead of trusting a dying process to
        # write state in a signal handler (PEP 419; the Airflow/Temporal detect-side pattern)
        _reconcile_dead_attempt(ctx, stages, manifest)
        idx = 0
        while idx < len(to_run):
            stage = to_run[idx]
            # Green-skip applies to --stages subsets too: `--stages` selects WHICH stages are eligible,
            # `--force` is the knob that re-runs a green one. It used to require `selected is None`, so the
            # plugin flow — which always passes --stages — could never resume: an exit-3 retry re-ran every
            # listed stage from scratch (measured 2026-07-27: $1.05 re-fetching a PRD already on disk, and it
            # would have re-paid the whole GROUND scan). Review mode already spells `--stages X --force` for
            # a deliberate re-run, which only makes sense under this reading.
            inputs_digest = _inputs_digest(stages, stage, ctx.run_dir)
            if (
                stage.name not in must_rerun
                and not force
                and ctx.flag("green_skip", True)  # False -> re-run every stage, green or not
                and _stage_green(stage, ctx.run_dir, manifest)
            ):
                # freshness: a green stage only skips when the inputs it was built from are byte-identical
                # now. Missing digest = legacy manifest, grandfathered (skip as before).
                recorded = (manifest.get(stage.name) or {}).get("inputs_digest")
                if recorded in (None, inputs_digest):
                    print(f"[{stage.name}] up to date — skipping (use --force to rerun)", file=sys.stderr)
                    # a skipped stage used to be trace-INVISIBLE (absence ambiguous with "not
                    # selected") while finish_run's summary still listed it as ok — this marker plus
                    # summary.ran_this_invocation makes the skip explicit (audit 2026-08-19)
                    decisions.record(ctx, stage.name, Decision.GREEN_SKIP,
                                     why="outputs green and inputs digest unchanged — skipped, not run")
                    idx += 1
                    continue
                from .engine import log_activity
                log_activity(ctx.run_dir, stage.name,
                             "inputs changed since this stage went green (digest mismatch) — re-running "
                             "instead of skipping, so downstream never anchors on stale upstream")
                print(f"[{stage.name}] inputs changed since green — re-running", file=sys.stderr)
                decisions.record(ctx, stage.name, Decision.DIGEST_MISMATCH_RERUN,
                                 why="green stage's inputs changed since it went green — re-running "
                                 "so downstream never anchors on stale upstream")
            must_rerun.discard(stage.name)
            missing = [r for r in stage.requires if not ctx.path(r).exists()]
            if missing:
                raise EngineFailure(
                    f"stage '{stage.name}' requires {missing} which do not exist in {ctx.run_dir} — "
                    f"run the earlier stages first (or drop --stages)."
                )
            # flags `retries` / `back_jumps` / `revise_on_retry` each default to today's behaviour;
            # turning one off narrows the loop for a benchmark or a fail-fast eval arm.
            retries_on = bool(ctx.flag("retries", True))
            back_jumps_on = bool(ctx.flag("back_jumps", True))
            revise_on = bool(ctx.flag("revise_on_retry", True))
            gate_retries_left = stage.retry_on_gate_failure if retries_on else 0
            prev_entry = manifest.get(stage.name) or {}
            if (
                stage.revise_on_retry
                and revise_on
                # ENGINE_FAILED counts too: a crash on the RETRY attempt now writes a manifest entry
                # (0.23.0), and the recorded failures from the earlier red gate are still the truth
                and prev_entry.get("status") in (StageStatus.GATE_FAILED, StageStatus.ENGINE_FAILED)
                and (prev_entry.get("gate") or {}).get("failures")
                and all(ctx.path(o).exists() for o in stage.outputs)
                and not guidance_path(ctx.run_dir, stage.name).exists()
            ):
                # a red stage resumes as a REVISION across invocations: without this, re-running the
                # command after a budget-exhausted exit recomposed from scratch (measured: a ~12min
                # recompose to fix 4 recorded citation failures a warm revision clears in ~2min)
                _write_guidance(ctx, stage.name, stage.name, prev_entry["gate"]["failures"])
                decisions.record(
                    ctx, stage.name, Decision.REVISE_IN_PLACE,
                    why="resume-from-red: previous invocation left this stage red (gate_failed, or a "
                    "crash after a red gate) with recorded failures; seeding guidance from the "
                    "manifest for a targeted revision instead of a fresh rewrite",
                    attempt=(prev_entry.get("attempts") or 0) + 1,
                )
                seeded_revise.add(stage.name)
            if (
                stage.retry_stage
                and back_jumps_on
                and stage.retry_stage != stage.name
                and stage.retry_stage in names[:idx]
                and prev_entry.get("status") in (StageStatus.GATE_FAILED, StageStatus.ENGINE_FAILED)
                and (prev_entry.get("gate") or {}).get("failures")
                and not force
                and back_jumps_used.get(stage.name, 0) < stage.retry_on_gate_failure
            ):
                # cross-stage resume-from-red: a red REVIEW's recorded failures belong to GENERATE
                # (retry_stage), not to review itself. Without this, resume green-skipped generate and
                # re-ran review FROM SCRATCH (~10min, ~$3) — and because a fresh panel re-samples the
                # defect population, verified findings got LOST across the stop (v10 2026-08-11: the
                # booking.py:2214 forwarding blocker was filed at 11:56, discarded by the resume's fresh
                # review at 12:03, and re-discovered at 12:14 — two stops for one already-written fix).
                # Resume must behave exactly like the back-jump the budget exhaustion interrupted.
                back_jumps_used[stage.name] = back_jumps_used.get(stage.name, 0) + 1
                _write_guidance(ctx, stage.retry_stage, stage.name, prev_entry["gate"]["failures"])
                decisions.record(
                    ctx, stage.name, Decision.BACK_JUMP,
                    why=f"resume-from-red: previous invocation left '{stage.name}' red (gate_failed, or a crash "
                    f"after a red gate); "
                    f"feeding the RECORDED failures straight to '{stage.retry_stage}' instead of paying "
                    f"a fresh {stage.name} pass that re-derives them",
                    failures=prev_entry["gate"]["failures"][:10],
                    attempt=prev_entry.get("attempts") or 1,
                )
                must_rerun.add(stage.retry_stage)
                must_rerun.add(stage.name)
                from .engine import log_activity
                log_activity(ctx.run_dir, stage.name,
                             f"resume-from-red — recorded failures sent straight to '{stage.retry_stage}' "
                             f"(no fresh {stage.name} pass)")
                idx = names.index(stage.retry_stage)
                continue
            jump_to: int | None = None
            while True:  # rerun loop — the boundary checkpoint may send the stage around again
                # the attempt number THIS run is about to record — _record increments it below, so read
                # it before that. Threaded into every observation that repeats per attempt (stage, agent,
                # generation, gate, decisions) so retries stay distinguishable instead of collapsing.
                attempt = ((manifest.get(stage.name) or {}).get("attempts") or 0) + 1
                # rerun hygiene: remove the stage's own stale outputs so the agent can't crib from a
                # previous attempt (observed: a rerun recycled citations from its old findings.md).
                # EXCEPT in revision mode: guidance + previous outputs => targeted edits, not a rewrite.
                revising = (
                    stage.revise_on_retry
                    and revise_on
                    and guidance_path(ctx.run_dir, stage.name).exists()
                    and all(ctx.path(o).exists() for o in stage.outputs)
                )
                if revising:
                    # keep the previous attempt's stream as ROTATED evidence: trace-backed gates (GROUND's
                    # witness check) must span every attempt that produced the current outputs, or every
                    # citation carried over by a targeted edit would look unwitnessed. Rotation only ever
                    # happens inside one invocation — guidance is cleared at invocation start, so a fresh
                    # invocation's first attempt always takes the else-branch and wipes the rotations.
                    cur = stream_path(ctx.run_dir, stage.name)
                    if cur.exists():
                        n = 1
                        while rotated_stream_path(ctx.run_dir, stage.name, n).exists():
                            n += 1
                        cur.rename(rotated_stream_path(ctx.run_dir, stage.name, n))
                        # the CANONICAL trace rotates with its stream, under the same attempt number
                        # (harness.engines.claude_translate.trace_label's `<stage>.attemptN` shape, which
                        # events_paths' `<stage>.*.events.jsonl` glob still collects). Without this the
                        # next attempt's trace would overwrite this one and the witness corpus would
                        # cover only the last attempt — exactly what rotating the stream prevents.
                        _rotate_trace(ctx.run_dir, stage.name, n)
                    if stage.name not in seeded_revise:  # resume-from-red already recorded this decision
                        decisions.record(
                            ctx, stage.name, Decision.REVISE_IN_PLACE,
                            why="guidance + previous outputs exist; targeted edits instead of a from-scratch "
                            "rewrite (gates re-police the revised whole; witness spans the rotated streams)",
                            attempt=attempt,
                        )
                    seeded_revise.discard(stage.name)
                else:
                    for o in stage.outputs:
                        ctx.path(o).unlink(missing_ok=True)
                    # a fresh attempt regenerates from zero: stale streams (this invocation's or a previous
                    # one's) must not witness citations the new scan never made — and neither must the
                    # canonical traces derived from them, which are what the trace-backed gates read
                    for s in stream_paths(ctx.run_dir, stage.name):
                        s.unlink(missing_ok=True)
                    for t in events_paths(ctx.run_dir, stage.name):
                        t.unlink(missing_ok=True)
                    # ... and neither must harness-side state ACCUMULATED from those streams: the witness
                    # carry store would re-witness the wiped attempts' reads (relaxing the anti-fabrication
                    # gate exactly when the operator asked for a from-scratch regeneration), and consumed
                    # byte-budget warnings would silence the fresh session's accounting
                    ctx.path(f"logs/{stage.name}.witness.txt").unlink(missing_ok=True)
                    ctx.path("logs/byte_budget.json").unlink(missing_ok=True)
                print(f"[{stage.name}] starting", file=sys.stderr)
                from .engine import log_activity
                log_activity(ctx.run_dir, stage.name, "STAGE started")
                started = datetime.now(UTC).isoformat(timespec="seconds")
                t0 = time.monotonic()
                attempt_epoch = time.time()  # wall clock — crash recovery filters streams by mtime
                # durable INTENT before the money is spent (0.23.2, the git-lockfile shape): if this
                # process dies without recording the attempt — Ctrl-C mid-cleanup, SIGTERM, SIGKILL,
                # power loss — the NEXT invocation's _reconcile_dead_attempt turns this breadcrumb
                # into an engine_failed entry with recovered accounting. Removed after every record.
                _write_attempt_intent(ctx, stage.name, attempt, attempt_epoch)
                stage_engine = _resolve_stage_engine(stage, ctx, engine)
                # the stage span covers engine + ledger + gate; the generation wraps the LIVE engine call
                # so its start/end are the agent's real wall-clock and live tool events nest under it
                with obs.stage_span(ctx, stage.name, attempt, mode="revision" if revising else "fresh"):
                    with obs.agent_generation(ctx, stage.name, attempt) as agen:
                        try:
                            result = stage_engine(stage, ctx)
                        except EngineFailure as ef:
                            # the failure must reach the LIVE FEED (field incident 2026-09-06: a scope-
                            # consent stop printed only to stdout; the operator watched 20 minutes of
                            # feed silence exactly when a decision was needed)
                            from .engine import log_activity
                            log_activity(ctx.run_dir, stage.name, f"ENGINE FAILURE: {ef}")
                            # a crashed attempt's spend must not vanish (audit 2026-08-19: the
                            # generation carried no cost, the manifest no entry, run_total_cost_usd
                            # under-reported — the one run class an engineer most needs to price had
                            # no price). Recover best-effort accounting from the stream files.
                            ran_stages.add(stage.name)  # it RAN (and spent) this invocation —
                            # without this the crashed stage read ran_this_invocation: false and its
                            # rework score was suppressed (review finding #3, 2026-08-19)
                            crashed = _partial_result_from_streams(
                                ctx, stage, time.monotonic() - t0, since=attempt_epoch)
                            obs.finish_generation(agen, crashed)
                            decisions.record(
                                ctx, stage.name, Decision.STAGE_CRASHED,
                                why="engine failed pre-gate (timeout/crash/missing outputs); partial "
                                "accounting recovered from the on-disk stream files",
                                attempt=attempt, cost_usd=crashed.cost_usd,
                                tool_calls=len(crashed.tool_calls),
                            )
                            # availability signal (xUnit failure≠error): the crash is visible to
                            # dashboards WITHOUT poisoning gate_*_passed, which stays conditional
                            # on the gate actually evaluating
                            obs.record_attempt_outcome(ctx, stage.name, "engine_failed", attempt)
                            _record_crash(manifest, stage, started, crashed, inputs_digest)
                            _write_manifest(ctx.run_dir, manifest)
                            _clear_attempt_intent(ctx, stage.name)  # recorded live — startup must not re-reconcile
                            raise
                        result.duration_s = result.duration_s or (time.monotonic() - t0)
                        obs.finish_generation(agen, result)

                    # gate observation opens BEFORE the verdict machinery (ledger render + gate fn):
                    # its latency is real evaluation cost (a witness re-scan ran ~91s and read as 0.00s)
                    gate_obs = obs.gate_started(ctx, stage.name, attempt)
                    # M2: render the scan ledger from ALL the stage's streams (rotated attempts + parallel
                    # shard/phase streams) BEFORE gating — trace-backed gates read the same surface
                    led = write_ledger(
                        stream_paths(ctx.run_dir, stage.name), ledger_path(ctx.run_dir, stage.name)
                    )

                    from .gates import drain_inert
                    drain_inert()  # clear anything stale before THIS evaluation
                    missing_out = [o for o in stage.outputs if not ctx.path(o).exists()]
                    failures = [f"declared output missing: {o}" for o in missing_out] + (
                        stage.gate(ctx) if not missing_out else []
                    )
                    # checks that disabled themselves mid-gate (gates.note_inert): a PASS with the
                    # witness/REQ checks inert must never be indistinguishable from a policed PASS
                    inert = drain_inert()
                    obs.record_gate(ctx, stage.name, failures, attempt, handle=gate_obs, inert=inert)
                ran_stages.add(stage.name)
                _record(manifest, stage, started, result, failures, inert=inert)
                manifest[stage.name]["inputs_digest"] = inputs_digest
                if led:
                    manifest[stage.name]["ledger"] = led.to_json()["stats"]
                _write_manifest(ctx.run_dir, manifest)
                _clear_attempt_intent(ctx, stage.name)  # the attempt is recorded — intent fulfilled
                if failures:
                    target = stage.retry_stage or stage.name
                    _will_jump = (target != stage.name and back_jumps_on
                                  and back_jumps_used.get(stage.name, 0) < stage.retry_on_gate_failure
                                  and target in names[:idx])
                    _will_retry = target == stage.name and gate_retries_left > 0
                    # bonus patch before stopping (runtime audit 2026-09-03): a run died with 2
                    # mechanical residual failures, cost an 8-minute human resume, and the resumed
                    # patch pass then fixed them in 18 seconds. A mechanical-only residue never
                    # justifies stopping the run — grant ONE bonus constrained pass instead.
                    from .engine import failures_all_mechanical
                    _will_bonus = (target == stage.name and not _will_retry
                                   and stage.name not in bonus_patch_used
                                   and stage.revise_on_retry and revise_on
                                   and all(ctx.path(o).exists() for o in stage.outputs)
                                   and failures_all_mechanical(failures))
                    log_activity(ctx.run_dir, stage.name,
                                 f"gate FAILED ({len(failures)} failure(s)) — "
                                 + (f"sending failures back to '{target}'" if _will_jump
                                    else "automatic revision next" if _will_retry
                                    else "budget exhausted but every residual failure is mechanical — "
                                         "one bonus patch pass instead of stopping" if _will_bonus
                                    else "retry budget exhausted; run stops (re-run the same command to resume)"))
                    if target != stage.name and back_jumps_on:  # cross-stage feedback (REVIEW -> GENERATE)
                        used = back_jumps_used.get(stage.name, 0)
                        if used < stage.retry_on_gate_failure and target in names[:idx]:
                            back_jumps_used[stage.name] = used + 1
                            _write_guidance(ctx, target, stage.name, failures)
                            decisions.record(
                                ctx, stage.name, Decision.BACK_JUMP,
                                why=f"gate failed; feeding failures back to '{target}' and re-running "
                                f"{target} -> {stage.name}",
                                failures=failures[:10],
                                attempt=attempt,
                            )
                            must_rerun.add(target)
                            jump_to = names.index(target)
                            print(
                                f"[{stage.name}] gate failed — jumping back to '{target}' with failures fed back",
                                file=sys.stderr,
                            )
                            break
                    elif gate_retries_left > 0:
                        gate_retries_left -= 1
                        _write_guidance(ctx, stage.name, stage.name, failures)
                        decisions.record(
                            ctx, stage.name, Decision.AUTO_RETRY_ON_GATE_FAILURE,
                            why="deterministic gate failed; failures fed back as guidance for a bounded rerun",
                            failures=failures[:10],
                            attempt=attempt,
                        )
                        print(f"[{stage.name}] gate failed — auto-retrying with failures fed back", file=sys.stderr)
                        continue
                    elif _will_bonus:
                        bonus_patch_used.add(stage.name)
                        _write_guidance(ctx, stage.name, stage.name, failures)
                        decisions.record(
                            ctx, stage.name, Decision.BONUS_PATCH_RETRY,
                            why="retry budget exhausted but every residual failure is mechanical "
                            "(citations/tags — one-Edit fixes); one bonus constrained patch pass "
                            "instead of stopping the run (measured 2026-09-02: a stop here cost an "
                            "8-minute human resume for what the patch pass then fixed in 18s)",
                            failures=failures[:10],
                            attempt=attempt,
                        )
                        print(f"[{stage.name}] budget exhausted, residue mechanical — bonus patch pass",
                              file=sys.stderr)
                        continue
                    _cost, _secs, _tries = run_totals(manifest)
                    print(
                        f"[{stage.name}] consumed so far: ${_cost:.2f}, {_secs / 60:.0f}m, "
                        f"{_tries} stage-attempt(s)",
                        file=sys.stderr,
                    )
                    raise GateFailure(f"gate failed after stage '{stage.name}': {failures}")
                _cost, _secs, _tries = run_totals(manifest)
                log_activity(ctx.run_dir, stage.name, f"gate PASSED — stage done ({result.duration_s:.0f}s)")
                # flush operator actions at the STAGE boundary, not only at trace open/run end
                # (0.24.5): a steer posted mid-stage was invisible in Langfuse until the whole
                # invocation exited, while its steering_delivered decision showed live — the
                # operator's own words were the only missing half of the loop
                obs.operator_actions_flush(ctx)
                print(
                    f"[{stage.name}] ok  (cost=${result.cost_usd or 0:.2f}, {result.duration_s:.0f}s)"
                    f"  · run so far: ${_cost:.2f}, {_secs / 60:.0f}m, {_tries} stage-attempt(s)",
                    file=sys.stderr,
                )
                if stage.ask_after:
                    stage.ask_after(ctx)  # itself a no-op when ctx.inputs['interactive'] is falsy
                if not ctx.inputs.get("interactive"):
                    break
                action = ask.boundary_checkpoint(stage, ctx)
                if action != CheckpointAction.CONTINUE:  # human overrides at a green gate are decisions worth signals
                    decisions.record(ctx, stage.name, f"checkpoint_{action}",
                                     why="human decision at the boundary checkpoint")
                if action == CheckpointAction.CONTINUE:
                    break
                if action == CheckpointAction.QUIT:
                    print(f"stopped at user request after '{stage.name}' — resume with --run-dir {ctx.run_dir}",
                          file=sys.stderr)
                    raise RunStopped(stage.name)
                # action == "rerun": loop; guidance/<stage>.md (if written) is appended to the prompt by the engine
            idx = jump_to if jump_to is not None else idx + 1


def _write_guidance(ctx: RunContext, target: str, source: str, failures: list[str]) -> None:
    """Write gate failures to guidance/<target>.md — the engine folds it into <target>'s next prompt.

    The LATEST same-origin block replaces the previous one instead of accumulating (v8/v9 field cost,
    2026-09-08: revisers received stale already-fixed blocks alongside the fresh one and burned ~1.5
    min per round proving the old block stale — one nearly re-fixed fixed failures). Prior blocks
    collapse to a one-line count so 'don't reintroduce past failure classes' survives without the
    stale specifics; human-authored guidance and other-origin blocks are preserved untouched."""
    origin = "your previous attempt" if target == source else f"the '{source}' stage reviewing your output"
    header = f"- GATE FAILURES from {origin} — fix each one:"
    summary_re = re.compile(rf"^- \((\d+) earlier gate failure\(s\) from {re.escape(origin)}\b")
    gpath = guidance_path(ctx.run_dir, target)
    gpath.parent.mkdir(parents=True, exist_ok=True)
    old = gpath.read_text(encoding="utf-8", errors="replace").splitlines() if gpath.exists() else []
    kept: list[str] = []
    superseded = 0
    i = 0
    while i < len(old):
        ln = old[i]
        m = summary_re.match(ln)
        if m:  # collapse prior summaries into one running count
            superseded += int(m.group(1))
            i += 1
            continue
        if ln == header:  # a previous same-origin block: count its bullets, drop the block
            i += 1
            while i < len(old) and old[i].startswith("  "):
                if old[i].lstrip().startswith("- "):
                    superseded += 1
                i += 1
            continue
        kept.append(ln)
        i += 1
    if superseded:
        kept.append(f"- ({superseded} earlier gate failure(s) from {origin} were addressed in prior "
                    f"attempts — history only; do NOT look for, re-fix, or re-verify them)")
    kept.append(header)
    kept += [f"  - {x}" for x in failures]
    if target != source:
        # cross-stage feedback carries only each blocker's FIRST line (the bullet regex is
        # single-line); the reviewers' full evidence chains live in their findings files. Point
        # the reviser there instead of stranding the richer context (2026-08-11 audit finding).
        kept.append("  - NOTE: these are the distilled blockers; the reviewers' full findings with "
                    "complete evidence chains are in review/persona_*.md — read them when a "
                    "failure's inline evidence isn't enough to locate the exact fix.")
    gpath.write_text("\n".join(kept) + "\n")


def _attempt_intent_path(run_dir: Path, stage_name: str) -> Path:
    """PER-STAGE breadcrumb (0.23.3): a single shared slot was overwritten-then-cleared by the next
    stage that ran, so a dead attempt kept for a fuller `--pipeline` composition was destroyed by
    the very invocation that was supposed to preserve it (caught by its own test)."""
    return run_dir / "logs" / f".attempt.{stage_name}.json"


def _attempt_intent_paths(run_dir: Path) -> list[Path]:
    return sorted((run_dir / "logs").glob(".attempt.*.json"))


def _write_attempt_intent(ctx: RunContext, stage_name: str, attempt: int, epoch: float) -> None:
    """Best-effort by design (0.23.3): a full disk or read-only run dir must degrade to 'no crash
    recovery for this attempt', never abort a run that would otherwise proceed — the recovery
    mechanism must not be a new crash source. Atomic like the manifest (shared helper)."""
    with contextlib.suppress(OSError):
        p = _attempt_intent_path(ctx.run_dir, stage_name)
        p.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write(p, json.dumps({"stage": stage_name, "attempt": attempt, "epoch": epoch}))


def _clear_attempt_intent(ctx: RunContext, stage_name: str) -> None:
    with contextlib.suppress(OSError):
        _attempt_intent_path(ctx.run_dir, stage_name).unlink(missing_ok=True)


def _reconcile_dead_attempt(ctx: RunContext, stages: list[Stage], manifest: dict) -> None:
    """An intent breadcrumb without a matching manifest record = a previous invocation died without
    recording its attempt (nothing but EngineFailure is caught live — SIGKILL/power loss can't be).
    Reconcile from durable evidence: the attempt's own stream files, filtered by the breadcrumb's
    epoch.

    Posture rules (0.23.3 review): recovery must NEVER fail a run — the whole body is fenced, and a
    breadcrumb that crashes reconciliation is cleared so it cannot crash every future startup. The
    breadcrumb is consumed AFTER the manifest write (a raise mid-recovery keeps the evidence; the
    attempts>=N guard supplies idempotence), EXCEPT when it is unusable (unparseable, zero epoch,
    already-recorded attempt). One deliberate keep: a stage missing from the CURRENT --pipeline
    composition leaves the breadcrumb for a later full-pipeline invocation to reconcile — clearing
    it there permanently ate the dead attempt's accounting."""
    for p in _attempt_intent_paths(ctx.run_dir):
        try:
            _reconcile_one(ctx, stages, manifest, p)
        except Exception as exc:  # noqa: BLE001 — recovery must never fail a run
            print(f"[reconcile] WARN startup recovery failed for {p.name} ({exc}) — continuing "
                  f"without it", file=sys.stderr)
            with contextlib.suppress(OSError):  # must not crash every future startup either
                p.unlink(missing_ok=True)


def _reconcile_one(ctx: RunContext, stages: list[Stage], manifest: dict, p: Path) -> None:
    from . import decisions

    try:
        rec = json.loads(p.read_text(encoding="utf-8", errors="replace"))
    except (OSError, json.JSONDecodeError):
        rec = None
    if not isinstance(rec, dict):
        p.unlink(missing_ok=True)  # unusable evidence — never re-examine it forever
        return
    try:
        attempt = int(rec.get("attempt") or 0)
        epoch = float(rec.get("epoch") or 0)
    except (TypeError, ValueError):
        attempt, epoch = 0, 0.0
    if not epoch or attempt < 1:
        p.unlink(missing_ok=True)  # no reliable stream filter / attempt id — unrecoverable,
        return  # and never risk re-counting rotated streams whose cost is already banked
    stage = next((s for s in stages if s.name == rec.get("stage")), None)
    if stage is None:
        return  # stage not in THIS pipeline composition — keep the breadcrumb for a fuller run
    if (manifest.get(stage.name) or {}).get("attempts", 0) >= attempt:
        p.unlink(missing_ok=True)  # the attempt was recorded live before the process died
        return
    # the dead attempt's wall-clock, from evidence: intent write -> last stream activity
    last = epoch
    for s in stream_paths(ctx.run_dir, stage.name):
        with contextlib.suppress(OSError):  # a file may vanish between glob and stat
            m = s.stat().st_mtime
            if m >= epoch:
                last = max(last, m)
    crashed = _partial_result_from_streams(ctx, stage, duration_s=max(last - epoch, 0.0),
                                           since=epoch)
    decisions.record(
        ctx, stage.name, Decision.STAGE_CRASHED,
        why="reconciled at startup: a previous invocation died without recording this attempt "
        "(killed/interrupted); accounting recovered from its stream files",
        attempt=attempt, cost_usd=crashed.cost_usd, tool_calls=len(crashed.tool_calls),
    )
    from . import obs as _obs
    _obs.record_attempt_outcome(ctx, stage.name, "engine_failed", attempt)
    _record_crash(manifest, stage,
                  datetime.fromtimestamp(epoch, UTC).isoformat(timespec="seconds"), crashed)
    _write_manifest(ctx.run_dir, manifest)
    p.unlink(missing_ok=True)  # consumed only now that the recovery is durably recorded
    print(f"[{stage.name}] previous invocation died mid-attempt — reconciled as engine_failed "
          f"(cost recovered: ${crashed.cost_usd or 0:.2f})", file=sys.stderr)


def _partial_result_from_streams(ctx: RunContext, stage: Stage, duration_s: float,
                                 since: float | None = None) -> StageResult:
    """Best-effort accounting for a CRASHED attempt, recovered from whatever stream files exist on
    disk — stream_paths covers fan-out worker streams (ground.stream.b*, review.stream.*) too.

    `since` = the attempt's wall-clock start: ONLY files written during this attempt count. Revision
    mode ROTATES the prior attempt's stream (witness evidence) instead of deleting it, and that
    attempt's cost is already banked in cost_usd_total — summing every glob hit double-billed a
    revision crash by the full history of rotated attempts (review finding #1, 2026-08-19; rename
    preserves mtime, so the filter is exact). Cost/usage exist only when a terminal result event was
    written: a hard timeout has none, so the entry may honestly carry cost None (the tokens spent
    are still visible via tool/turn counts)."""
    from .engine import merge_stream_results
    from .streams import parse_stream

    streams, primary = [], None
    main = stream_path(ctx.run_dir, stage.name)
    for p in stream_paths(ctx.run_dir, stage.name):
        with contextlib.suppress(OSError):
            # STRICT compare, no slack: rotation happens before attempt_epoch is taken and rename
            # preserves mtime, so a rotated file is always strictly older — a slack window would
            # re-admit it whenever a fast gate started the retry within the window
            if since is not None and p.stat().st_mtime < since:
                continue
            parsed = parse_stream(p.read_text(encoding="utf-8", errors="replace"))
            streams.append(parsed)
            if p == main:
                primary = parsed  # sorted glob put a rotated file first — model/final_text/session
                # must come from THIS attempt's main stream, not attempt 1's (review finding #1)
    if not streams:
        return StageResult(duration_s=duration_s, engine_desc="crashed (no stream recovered)")
    model = next((s.model for s in streams if s.model), "unknown")
    return merge_stream_results(streams, primary or streams[0], duration_s=duration_s,
                                engine_desc=f"claude:{model} (crashed)")


def _record_crash(manifest: dict, stage: Stage, started: str, result: StageResult,
                  inputs_digest: str | None = None) -> None:
    """Record a crashed attempt so its spend and attempt count survive — on the documented manifest
    shape (requires/outputs/gate always present; a first-attempt crash used to omit all three).
    The previous gate record is preserved ONLY when it holds failures (resume-from-red reads them);
    a stale PASSING gate is replaced by an honest no-verdict stub — `--force` + crash used to leave
    `status: engine_failed` beside `gate.passed: true` for outputs that no longer existed."""
    prev = manifest.get(stage.name) or {}
    prev_gate = prev.get("gate") or {}
    gate = prev_gate if prev_gate.get("failures") else {
        "passed": False, "failures": [],
        "note": "engine crashed before the gate ran; no verdict this attempt",
    }
    manifest[stage.name] = {
        "status": StageStatus.ENGINE_FAILED,
        "started_at": started,
        "duration_s": round(result.duration_s, 1),
        "engine": result.engine_desc,
        "cost_usd": result.cost_usd,
        "usage": result.usage,
        "attempts": (prev.get("attempts") or 0) + 1,
        "cost_usd_total": round((prev.get("cost_usd_total") or 0) + (result.cost_usd or 0), 6),
        "duration_s_total": round((prev.get("duration_s_total") or 0) + result.duration_s, 1),
        # rework = time in attempts beyond the first (retries, revisions, back-jump reruns) — 33% of
        # one audited run's machine time; a run must self-report it (runtime audit 2026-09-03)
        "rework_s": round((prev.get("rework_s") or 0)
                          + (result.duration_s if (prev.get("attempts") or 0) >= 1 else 0), 1),
        "requires": list(stage.requires),
        "outputs": list(stage.outputs),
        "tool_call_count": len(result.tool_calls),
        "multi_call_turn_pct": _multi_call_pct(result),
        "gate": gate,
        **({"inputs_digest": inputs_digest or prev.get("inputs_digest")}
           if (inputs_digest or prev.get("inputs_digest")) else {}),
    }


def _multi_call_pct(result: StageResult) -> float | None:
    """% of tool-bearing turns that batched >1 call — did engine.parallel_dispatch_note take?
    (measured 2026-09-03: 0% across every agent of a full run; each unbatched independent call
    is a full model round-trip, ~12.7s/turn in that run's generate)."""
    return round(100 * result.multi_call_turns / result.tool_turns, 1) if result.tool_turns else None

def _record(manifest: dict, stage: Stage, started: str, result: StageResult, failures: list[str],
            inert: list[str] | None = None) -> None:
    """Record this attempt. `cost_usd`/`duration_s` stay LAST-ATTEMPT (the documented manifest contract the
    eval harness reads); `attempts` + `*_total` are the cumulative truth.

    Why both: the entry was overwritten wholesale on every retry, so a stage that ran twice reported only
    its second attempt and the earlier spend vanished — the accounting went lossy exactly when a run got
    expensive (observed 2026-07-27: GROUND ran twice in one invocation and the manifest kept one attempt,
    under-reporting the run). Anything asking "what did this run dir consume" must read the totals."""
    prev = manifest.get(stage.name) or {}
    manifest[stage.name] = {
        "status": StageStatus.OK if not failures else StageStatus.GATE_FAILED,
        "started_at": started,
        "duration_s": round(result.duration_s, 1),
        "engine": result.engine_desc,
        "cost_usd": result.cost_usd,
        "usage": result.usage,
        "attempts": (prev.get("attempts") or 0) + 1,
        "cost_usd_total": round((prev.get("cost_usd_total") or 0) + (result.cost_usd or 0), 6),
        "duration_s_total": round((prev.get("duration_s_total") or 0) + result.duration_s, 1),
        # rework = time in attempts beyond the first (retries, revisions, back-jump reruns) — 33% of
        # one audited run's machine time; a run must self-report it (runtime audit 2026-09-03)
        "rework_s": round((prev.get("rework_s") or 0)
                          + (result.duration_s if (prev.get("attempts") or 0) >= 1 else 0), 1),
        "requires": list(stage.requires),
        "outputs": list(stage.outputs),
        "tool_call_count": len(result.tool_calls),
        "multi_call_turn_pct": _multi_call_pct(result),
        "gate": {"passed": not failures, "failures": failures,
                 **({"inert_checks": inert} if inert else {})},
    }


def run_totals(manifest: dict) -> tuple[float, float, int]:
    """(cost, seconds, attempts) actually consumed by this run dir — every attempt, not just the last."""
    entries = [e for e in manifest.values() if isinstance(e, dict) and "status" in e]  # stage
    # entries only — the manifest also carries run-level summaries (operator_wait) with no attempts
    return (
        round(sum(e.get("cost_usd_total") or e.get("cost_usd") or 0 for e in entries), 4),
        round(sum(e.get("duration_s_total") or e.get("duration_s") or 0 for e in entries), 1),
        sum(e.get("attempts") or 1 for e in entries),
    )
