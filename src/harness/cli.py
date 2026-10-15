"""`harness` — the workflow-agnostic command line. Exit codes: 0 ok · 1 config · 2 gate · 3 engine.

    harness run <workflow> [workflow inputs] [--engine NAME] [--stage-engine s=NAME] ...
    harness flags [--json] [--explain NAME] [--workflow W]
    harness engines [--json]
    harness workflows [--json]
    harness doctor [--json] [--workflow W]
    harness record <run_dir> <recording_dir>

Human AND agent friendly, which are different requirements and both are load-bearing:
  * humans get tables, `flags --explain` showing which layer won, and errors that name the fix;
  * agents get `--json` on every read command, so a delegatee can discover what it may toggle instead
    of guessing env vars, and every run writes `flags.json` recording what was in effect.

In every JSON payload, a cost of `null` means NO COST WAS REPORTED (a timeout writes no terminal
result event; some engines have no cost event at all). It never means zero — "nothing ran" and "ran
for free" are different facts and a roll-up that conflates them is wrong.

WHY the RunHooks seam exists (D13): a HARNESS must not launch desktop apps, write durable memory or post
notifications by default. The RFC workflow does all three today and must keep doing them, so those
side effects are passed IN by the caller that wants them and default to nothing.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from . import engines as engines_mod
from . import flags as flagmod
from . import workflows as workflows_mod
from .constants import MANIFEST, ExitCode
from .contracts import (
    EngineFailure,
    GateFailure,
    PipelineConfigError,
    RunContext,
    RunStopped,
    Workflow,
)

PROG = "harness"


# ---- the seam for workflow-owned side effects ------------------------------------------------------


@dataclass
class RunHooks:
    """Optional callbacks a workflow's own CLI supplies. Every one defaults to nothing happening.

    early_exit   handle a mode that does not run the pipeline (rfc's --plan-scope/--steer/--decide);
                 return an exit code to stop, None to continue into the run. Called BEFORE the config
                 is loaded, because `--steer` at a live run must work on a machine with no config.json.
    prepare      entry prompts that MUTATE args (rfc asks for the PRD). Runs before the run dir is
                 named, since the slug reads the resolved inputs. Gets the parser so a missing hard
                 input can still exit through `parser.error`.
    slug         name an auto-created run dir from the inputs (rfc derives it from the Jira key / PRD id).
    before_run   operator waivers, the --watch terminal. Runs once the run dir exists.
    after_run    the outcome banner, notifications, post-run learning. `outcome` is one of
                 "ok" | "gate_failed" | "engine_failed" | "config_error" | "stopped".
    load_config  the config object RunContext carries. Defaults to rfc_agent.config.get_config — the
                 documented seam until the config loader itself moves into the kernel.
    compose      names -> [Stage]. Defaults to harness.workflows.compose. A workflow supplies its own
                 when it has a composition rule of its own (the RFC workflow composes in stage-registry
                 order and raises its own error naming the registered stages).
    run          the run loop itself. Defaults to rfc_agent.runner.run_pipeline — which is exactly why
                 it is a hook: harness/__init__.py's rule is that nothing under harness/ imports
                 rfc_agent, and the run loop has not moved into the kernel yet (D2). Passing it in keeps
                 the kernel free of that import instead of hiding it in a function body.
    """

    early_exit: Callable[[argparse.Namespace], int | None] | None = None
    prepare: Callable[[argparse.Namespace, object], None] | None = None
    slug: Callable[[argparse.Namespace], str] | None = None
    before_run: Callable[[argparse.Namespace, Path], None] | None = None
    after_run: Callable[[argparse.Namespace, Path, str], None] | None = None
    load_config: Callable[[], object] | None = None
    compose: Callable[[list[str]], list] | None = None
    run: Callable[..., None] | None = None


def _default_config():
    """TODO(harness): becomes harness.config once the loader moves (see harness/__init__.py)."""
    from rfc_agent.config import get_config

    return get_config()


def _default_run(*args, **kwargs) -> None:
    """TODO(harness): becomes harness.runner.run_pipeline once the run loop moves (D2)."""
    from rfc_agent.runner import run_pipeline

    return run_pipeline(*args, **kwargs)


def _run_totals(manifest: dict) -> tuple[float, float, int]:
    from rfc_agent.runner import run_totals

    return run_totals(manifest)


# ---- shared argument groups ------------------------------------------------------------------------


def add_harness_args(parser: argparse.ArgumentParser) -> None:
    """The arguments EVERY workflow gets. A workflow adds only its own input vocabulary."""
    parser.add_argument("--engine", help=f"engine adapter for every stage (default: flag `engine`; "
                                        f"see `{PROG} engines`)")
    parser.add_argument("--stage-engine", action="append", default=[], metavar="STAGE=NAME",
                        help="per-stage engine override, repeatable (e.g. --stage-engine review=pi)")
    parser.add_argument("--disable-stage", action="append", default=[], metavar="STAGE",
                        help="skip this stage; repeatable. Downstream `requires` checks still apply, so "
                             "a hole fails loudly instead of degrading the run quietly.")
    parser.add_argument("--set", action="append", default=[], metavar="NAME=VALUE",
                        help=f"set any flag (`{PROG} flags` lists them; `--explain NAME` shows layers)")
    parser.add_argument("--enable", action="append", default=[], metavar="NAME",
                        help="turn a boolean flag ON")
    parser.add_argument("--disable", action="append", default=[], metavar="NAME",
                        help="turn a boolean flag OFF")
    parser.add_argument("--pipeline", help="ordered stage names to compose (default: the workflow's)")
    parser.add_argument("--stages", help="comma-separated subset to run (requires --run-dir); default: all")
    parser.add_argument("--run-dir", help="existing run dir to resume / write into")
    parser.add_argument("--force", action="store_true", help="rerun stages even if green")
    parser.add_argument("--no-interactive", action="store_true", help="never prompt (eval mode)")
    parser.add_argument("--model", help="model for every engine invocation (default: the engine's)")
    parser.add_argument("--json", action="store_true", dest="json_out",
                        help="machine-readable result on stdout (for agents driving the harness)")


def cli_sets(args: argparse.Namespace) -> dict:
    """Fold the convenience arguments into the one flag-override dict `flags.resolve` takes.

    `--engine`, `--stage-engine`, `--disable-stage` and `--model` are sugar for `--set`: every toggle in
    the system is a flag (D5), and the CLI must not grow a second, parallel way to express one.

    Every argument is read with getattr: a workflow ALIAS may have a frozen surface that predates these
    flags (`rfc-agent` does — its --help is pinned byte-for-byte), and on that surface the flag layers
    below CLI (env, config, workflow default) are the only ones in play. Absent, not defaulted to
    something: a missing argument must not silently look like an explicit override.
    """
    sets = list(getattr(args, "set", None) or [])
    if getattr(args, "engine", None):
        sets.append(f"engine={args.engine}")
    if getattr(args, "model", None):
        sets.append(f"model={args.model}")
    per_stage: dict[str, str] = {}
    for item in getattr(args, "stage_engine", None) or []:
        if "=" not in item:
            raise ValueError(f"--stage-engine expects STAGE=NAME, got {item!r}")
        stage, _, name = item.partition("=")
        per_stage[stage.strip()] = name.strip()
    if per_stage:
        sets.append("stage_engines=" + json.dumps(per_stage))
    if getattr(args, "disable_stage", None):
        sets.append("disabled_stages=" + ",".join(args.disable_stage))
    return flagmod.parse_cli_sets(sets, getattr(args, "enable", None), getattr(args, "disable", None))


def resolve_flags(wf: Workflow | None, args: argparse.Namespace, cfg) -> flagmod.Flags:
    """Resolve every flag for this invocation: CLI > env > config > workflow default > flag default."""
    config_get = None
    if wf is not None:
        with contextlib.suppress(ModuleNotFoundError, AttributeError):
            # dynamic by necessity: the workflow name is data. Reaching `workflows` BY NAME is the
            # whole point of discovery; what the layering guard forbids is depending on it statically.
            mod = __import__(f"workflows.{wf.name}", fromlist=["config_get"])
            if cfg is not None and hasattr(mod, "config_get"):
                config_get = mod.config_get(cfg)
    return flagmod.resolve(
        cli_sets=cli_sets(args),
        config_get=config_get,
        workflow_defaults=(wf.flag_defaults if wf is not None else None),
    )


# ---- run -------------------------------------------------------------------------------------------


def run_workflow(name: str, args: argparse.Namespace, hooks: RunHooks | None = None,
                 parser: object | None = None) -> int:
    """Run one workflow. Returns an ExitCode; never calls sys.exit (so this stays embeddable).

    The order below is deliberate: entry prompts before the run dir is
    named (the slug reads the resolved inputs), the observability precondition before any spend, and
    inputs.json written before the first stage so a crash is still reproducible.
    """
    hooks = hooks or RunHooks()
    wf = workflows_mod.get(name)
    # BEFORE the config load: `--steer`/`--decide` against a live run must work on a machine that has
    # no config.json (they touch only the run dir), and loading config would raise there.
    if hooks.early_exit is not None:
        code = hooks.early_exit(args)
        if code is not None:
            return int(code)
    cfg = (hooks.load_config or _default_config)()
    flags = resolve_flags(wf, args, cfg)

    # Observability is a run PRECONDITION, not an option (operator decision 2026-09-03): an untraced run
    # cannot be evaluated, annotated or improved, so a missing sink halts before any spend. The RFC
    # workflow re-defaults `obs_required` to True; a workflow with no sink leaves it False.
    have_sink = bool(cfg is not None and cfg.get("LANGFUSE.PUBLIC_KEY") and cfg.get("LANGFUSE.SECRET_KEY"))
    if flags.get("obs_required") and not have_sink:
        from rfc_agent import config as _config

        print(f"{PROG}: observability credentials missing — every run of this workflow must be traced. "
              f"Fill LANGFUSE.PUBLIC_KEY and LANGFUSE.SECRET_KEY in "
              f"{getattr(_config, '_LOCAL', 'config.json')} (or export LANGFUSE_PUBLIC_KEY / "
              f"LANGFUSE_SECRET_KEY), then re-run. Nothing was spent. To run without a sink: "
              f"--disable obs_required.", file=sys.stderr)
        return int(ExitCode.PIPELINE_CONFIG_ERROR)

    no_interactive = bool(getattr(args, "no_interactive", False))
    args.interactive = sys.stdin.isatty() and not no_interactive and bool(flags.get("interactive"))
    if hooks.prepare is not None:
        hooks.prepare(args, parser)
    stages_arg = getattr(args, "stages", None)
    selected = [s.strip() for s in stages_arg.split(",") if s.strip()] if stages_arg else None
    if selected and not getattr(args, "run_dir", None):
        # A USAGE error, routed through the caller's own parser when it has one. `rfc-agent` must keep
        # exiting 2 here: argparse's `parser.error` is what it did before the extraction, and 2 is a
        # frozen public surface even though it collides with GATE_FAILURE (review SHOULD-FIX 1.3).
        # `harness` has no such history and uses 1, which is the code this condition should have.
        msg = "--stages (subset/resume) requires --run-dir pointing at the run to resume"
        if parser is not None and hasattr(parser, "error"):
            parser.error(msg)  # exits: 2 under argparse's default, 1 under harness's _Parser
        print(f"{PROG}: {msg}", file=sys.stderr)
        return int(ExitCode.PIPELINE_CONFIG_ERROR)

    run_dir = resolve_run_dir(args, cfg, hooks)
    run_dir.mkdir(parents=True, exist_ok=True)
    args.run_dir = str(run_dir)
    if hooks.before_run is not None:
        hooks.before_run(args, run_dir)

    inputs = wf.resolve_inputs(args) if wf.resolve_inputs else {}
    print(f"run dir: {run_dir}", file=sys.stderr)
    if not args.interactive:
        # say it out loud: a non-TTY run silently skips every ask, and nothing on screen used to say so
        why = "--no-interactive" if no_interactive else "stdin is not a TTY"
        print(f"non-interactive ({why}): entry prompts, per-stage checkpoints and open-question asks are "
              f"DISABLED — questions stay open in the deliverables (see logs/asks_skipped.jsonl)",
              file=sys.stderr)
        with contextlib.suppress(Exception):
            from rfc_agent import ask

            ask.log_skipped(run_dir, f"run is non-interactive ({why}): all asks disabled")

    pipeline_arg = getattr(args, "pipeline", None)
    names = ([s.strip() for s in pipeline_arg.split(",") if s.strip()] if pipeline_arg
             else list(wf.default_pipeline))
    print(f"pipeline: {' -> '.join(names)}", file=sys.stderr)

    disabled = list(flags.get("disabled_stages") or [])
    try:
        stages = (hooks.compose(names) if hooks.compose is not None
                  else workflows_mod.compose(wf, names, disabled))
        if hooks.compose is not None and disabled:
            stages = [s for s in stages if s.name not in set(disabled)]
    except PipelineConfigError as exc:
        print(f"PIPELINE CONFIG ERROR: {exc}", file=sys.stderr)
        return int(ExitCode.PIPELINE_CONFIG_ERROR)
    # compose() already removed the disabled stages; the runner is told about them too so it records the
    # decision — the removal must be visible in the trace, not just absent from the pipeline.
    engine = None
    engine_name = str(flags.get("engine") or "")
    if engine_name:
        try:
            engine = engines_mod.engine_from_flags(engine_name, flags)
        except KeyError as exc:
            print(f"{PROG}: {exc.args[0] if exc.args else exc}", file=sys.stderr)
            return int(ExitCode.PIPELINE_CONFIG_ERROR)

    ctx = RunContext(run_dir=run_dir, inputs=inputs, config=cfg)
    outcome, code = "ok", int(ExitCode.OK)
    try:
        (hooks.run or _default_run)(stages, ctx, selected=selected, force=getattr(args, "force", False),
                                                   engine=engine, flags=flags, workflow=wf.name)
    except GateFailure as exc:
        print(f"GATE FAILURE: {exc}", file=sys.stderr)
        outcome, code = "gate_failed", int(ExitCode.GATE_FAILURE)
    except EngineFailure as exc:
        print(f"ENGINE FAILURE: {exc}", file=sys.stderr)
        outcome, code = "engine_failed", int(ExitCode.ENGINE_FAILURE)
    except PipelineConfigError as exc:
        print(f"PIPELINE CONFIG ERROR: {exc}", file=sys.stderr)
        outcome, code = "config_error", int(ExitCode.PIPELINE_CONFIG_ERROR)
    except RunStopped:
        # a human stopping at a checkpoint is not a failure: exit 0, resumable via --run-dir
        outcome, code = "stopped", int(ExitCode.OK)
    if hooks.after_run is not None:
        hooks.after_run(args, run_dir, outcome)
    if getattr(args, "json_out", False):
        manifest = {}
        with contextlib.suppress(OSError, json.JSONDecodeError):
            manifest = json.loads((run_dir / MANIFEST).read_text())
        # None, not 0.0, when there is no manifest: "nothing ran" and "ran for free" are different
        # facts, and an agent reading this JSON must be able to tell them apart.
        cost, secs, tries = _run_totals(manifest) if manifest else (None, None, 0)
        print(json.dumps({
            "workflow": wf.name, "outcome": outcome, "exit_code": code, "run_dir": str(run_dir),
            "engine": engine_name, "pipeline": [s.name for s in stages],
            "cost_usd": cost, "duration_s": secs, "stage_attempts": tries,
            "stages": {k: v.get("status") for k, v in manifest.items()
                       if isinstance(v, dict) and "status" in v},
        }, indent=2))
    return code


def resolve_run_dir(args: argparse.Namespace, cfg, hooks: RunHooks) -> Path:
    """Explicit --run-dir > explicit --out > config RUNS_ROOT > ~/rfc-runs (the same default Scope uses).

    The config anchor exists because "wherever the shell happened to be" scattered deliverables across
    the filesystem (three run-dir trees the owner didn't know about, one inside REPOS_ROOT — where
    GROUND's candidate-repo fallback would iterate over them as if they were repos).
    """
    if getattr(args, "run_dir", None):
        return Path(args.run_dir).expanduser()
    from datetime import date

    runs_root = Path(getattr(args, "out", None) or (cfg.get("RUNS_ROOT") if cfg else None) or "~/rfc-runs")
    slug = hooks.slug(args) if hooks.slug else "run"
    return runs_root.expanduser() / f"{date.today().isoformat()}-{slug}"


# ---- read-only commands ----------------------------------------------------------------------------


def cmd_flags(args: argparse.Namespace) -> int:
    load_registries()
    if args.explain:
        try:
            # Resolve the SAME way a run does, config layer included: for every bridged flag the value
            # comes from config.json, so a command whose whole job is "show which layer won" was
            # naming the wrong winner (review SHOULD-FIX 6.2). `--workflow` selects whose bridge and
            # whose re-defaults apply; without it we still load config, which is the common case.
            fl = _resolve_for_inspection(args.workflow)
            res = fl.explain(args.explain)
        except KeyError:
            print(f"{PROG}: unknown flag {args.explain!r} — run `{PROG} flags` to list them",
                  file=sys.stderr)
            return int(ExitCode.PIPELINE_CONFIG_ERROR)
        spec = flagmod.registry()[args.explain]
        shown = flagmod.redact(args.explain, res.value)
        payload = {"name": args.explain, "value": shown, "winning_layer": res.source,
                   "layers": {k: flagmod.redact(args.explain, v) for k, v in res.layers.items()},
                   "type": spec.type.__name__,
                   "default": flagmod.redact(args.explain, spec.default),
                   "env": spec.env_var, "scope": str(spec.scope), "owner": spec.owner,
                   "secret": spec.secret, "wired": spec.wired,
                   "legacy_config_key": spec.legacy_config_key, "help": spec.help}
        if args.json_out:
            print(json.dumps(payload, indent=2, default=str))
        else:
            print(f"{args.explain} = {shown!r}   (from: {res.source})")
            for layer, val in res.layers.items():
                mark = "<-- wins" if layer == res.source else ""
                print(f"  {layer:<9} {flagmod.redact(args.explain, val)!r} {mark}")
            print(f"\n  type {spec.type.__name__} · env {spec.env_var} · owner {spec.owner}")
            if spec.legacy_config_key:
                wired = "read via ctx.flag()" if spec.wired else "BRIDGED — stages still read the config key"
                print(f"  config key {spec.legacy_config_key} ({wired})")
            if spec.secret:
                print("  secret: the value is never written to flags.json, --json or this table")
            print(f"  {spec.help}")
        return int(ExitCode.OK)
    rows = flagmod.describe()
    if args.workflow:
        wf = workflows_mod.get(args.workflow)
        rows = [r for r in rows if r["name"] in set(wf.flags)]
    if args.json_out:
        print(json.dumps(rows, indent=2, default=str))
        return int(ExitCode.OK)
    _table(["NAME", "TYPE", "DEFAULT", "OWNER", "READ", "ENV"],
           [[r["name"], r["type"], _short(r["default"]), r["owner"],
             "flag" if r["wired"] else "bridged", r["env"]] for r in rows])
    bridged = [r["name"] for r in rows if not r["wired"]]
    print(f"\n{len(rows)} flag(s). `{PROG} flags --explain <name>` shows which layer wins.")
    if bridged:
        print(f"{len(bridged)} marked `bridged`: the VALUE reaches the stage (through its config key), "
              f"but nothing reads the flag by this name yet — setting it changes nothing until the "
              f"stage is migrated to ctx.flag().")
    return int(ExitCode.OK)


def _resolve_for_inspection(workflow: str | None):
    """Resolve every flag the way a RUN would, for the read-only commands.

    Loads the config (so the config layer is visible) and the workflow's re-defaults when one is
    named. Config failures degrade to "no config layer" rather than an error: `harness flags` must
    stay usable on a machine with no config.json, which is exactly where someone runs it first.
    """
    import argparse as _argparse

    wf = workflows_mod.get(workflow) if workflow else None
    cfg = None
    with contextlib.suppress(Exception):
        cfg = _default_config()
    return resolve_flags(wf, _argparse.Namespace(), cfg)


def cmd_engines(args: argparse.Namespace) -> int:
    rows = []
    for name in engines_mod.names():
        problems, built = [], None
        try:
            built = engines_mod.get_engine(name)
        except Exception as exc:  # noqa: BLE001 — an adapter that cannot even be built IS the finding
            problems = [f"cannot be constructed: {type(exc).__name__}: {exc}"]
        if built is not None:
            with contextlib.suppress(Exception):
                problems = built.doctor()
        rows.append({"name": name, "healthy": not problems, "problems": problems,
                     "supports_resume": bool(built and built.supports_resume()) if built else None})
    if args.json_out:
        print(json.dumps(rows, indent=2))
        return int(ExitCode.OK)
    _table(["ENGINE", "STATUS", "RESUME", "NOTES"],
           [[r["name"], "ok" if r["healthy"] else "problem",
             "yes" if r["supports_resume"] else "no",
             (r["problems"][0][:80] if r["problems"] else "")] for r in rows])
    return int(ExitCode.OK)


def cmd_workflows(args: argparse.Namespace) -> int:
    load_registries()
    rows = []
    for name in workflows_mod.names():
        wf = workflows_mod.get(name)
        rows.append({"name": wf.name, "description": wf.description,
                     "default_pipeline": list(wf.default_pipeline),
                     "stages": sorted(s.name for s in wf.stages),
                     "flags": list(wf.flags), "decisions": list(wf.decisions)})
    if args.json_out:
        print(json.dumps(rows, indent=2))
        return int(ExitCode.OK)
    _table(["WORKFLOW", "PIPELINE", "DESCRIPTION"],
           [[r["name"], " -> ".join(r["default_pipeline"]), r["description"][:60]] for r in rows])
    return int(ExitCode.OK)


def cmd_doctor(args: argparse.Namespace) -> int:
    """Everything that must be true BEFORE a run spends money, in one place.

    A doctor finding always names the fix. "claude not found" is useless; "install Claude Code or set
    AGENT.CLAUDE_BIN" is actionable — and an agent reading --json can act on it.
    """
    load_registries()
    report: dict = {"config": {}, "engines": [], "workflows": [], "mcp": {}, "problems": []}
    cfg = None
    try:
        cfg = _default_config()
        # a plain import, not __import__: tests/test_harness_layering.py reads this file's imports with
        # ast, so a dynamically-named seam would hide from the very guard that is meant to police it
        from rfc_agent import config as _cfgmod

        report["config"] = {"ok": True, "path": str(getattr(_cfgmod, "_LOCAL", "?"))}
    except Exception as exc:  # noqa: BLE001 — a missing/invalid config is the most common finding
        report["config"] = {"ok": False, "error": str(exc)}
        report["problems"].append(f"config: {exc}")
    if cfg is not None:
        for key, why in (("LANGFUSE.PUBLIC_KEY", "observability is a run precondition"),
                         ("LANGFUSE.SECRET_KEY", "observability is a run precondition"),
                         ("REPOS_ROOT", "GROUND needs local checkouts to read")):
            if not cfg.get(key):
                report["problems"].append(f"config {key} is empty — {why}")
    for name in engines_mod.names():
        try:
            problems = engines_mod.get_engine(name).doctor()
        except Exception as exc:  # noqa: BLE001
            problems = [f"cannot be constructed: {exc}"]
        report["engines"].append({"name": name, "problems": problems})
    wanted = [args.workflow] if args.workflow else workflows_mod.names()
    for name in wanted:
        wf = workflows_mod.get(name)
        servers = sorted({s for stage in wf.stages for s in stage.mcp_servers})
        report["workflows"].append({"name": name, "stages": len(wf.stages),
                                    "mcp_servers_referenced": servers})
        configured = set((cfg.config.get("MCP") or {}).get("mcpServers", {}) if cfg is not None else {})
        missing = [s for s in servers if s not in configured]
        report["mcp"][name] = {"referenced": servers, "configured": sorted(configured),
                               "missing": missing}
        if missing:
            report["problems"].append(
                f"workflow {name}: stages reference MCP server(s) {missing} that config.json's "
                f"MCP.mcpServers does not define — those stages will fail at materialisation")
    # `claude-code` being unhealthy is only a problem if it is the DEFAULT engine
    default_engine = str(flagmod.resolve().get("engine") or "")
    for e in report["engines"]:
        if e["name"] == default_engine and e["problems"]:
            report["problems"].extend(f"engine {e['name']}: {p}" for p in e["problems"])
    report["ok"] = not report["problems"]
    if args.json_out:
        print(json.dumps(report, indent=2))
    else:
        print(f"config: {'ok' if report['config'].get('ok') else 'FAILED — ' + report['config'].get('error', '')}")
        for e in report["engines"]:
            print(f"engine {e['name']:<12} {'ok' if not e['problems'] else 'problem: ' + e['problems'][0][:70]}")
        for w in report["workflows"]:
            print(f"workflow {w['name']:<10} {w['stages']} stage(s); mcp: "
                  f"{', '.join(w['mcp_servers_referenced']) or 'none'}")
        if report["problems"]:
            print("\nproblems:")
            for p in report["problems"]:
                print(f"  - {p}")
        else:
            print("\nno problems found.")
    return int(ExitCode.OK if report["ok"] else ExitCode.PIPELINE_CONFIG_ERROR)


def cmd_record(args: argparse.Namespace) -> int:
    from .engines.replay import RecordingError, record

    try:
        dest = record(Path(args.run_dir).expanduser(), Path(args.recording_dir).expanduser())
    except RecordingError as exc:
        print(f"{PROG}: {exc}", file=sys.stderr)
        return int(ExitCode.PIPELINE_CONFIG_ERROR)
    labels = json.loads((dest / "recording.json").read_text())["labels"]
    if args.json_out:
        print(json.dumps({"recording_dir": str(dest), "labels": labels}, indent=2))
    else:
        print(f"recorded {len(labels)} stage(s) to {dest}: {', '.join(labels)}\n"
              f"replay it with: {PROG} run <workflow> --engine replay "
              f"--set replay.recording={dest}")
    return int(ExitCode.OK)


# ---- plumbing --------------------------------------------------------------------------------------


def load_registries() -> None:
    """Register every flag in the system before a parser or a listing is built.

    Discovery has the side effect of registering each workflow's flags, and importing each ENGINE
    module registers its `<engine>.*` construction flags. Both halves are required: without the engine
    half, `harness flags` omitted every `pi.*` flag and `--set pi.provider=anthropic` was rejected as
    an unknown flag — the CLI half of the same bug that had `engine_from_flags` dropping `flags`.

    An adapter that cannot be imported (missing optional dependency) is skipped rather than fatal: not
    being able to run `pi` must not stop `harness flags` from listing the rest.
    """
    workflows_mod.names()
    for name in engines_mod.names():
        with contextlib.suppress(Exception):
            engines_mod.get_engine(name)


def _short(value, width: int = 22) -> str:
    text = json.dumps(value, default=str) if isinstance(value, (dict, list)) else str(value)
    return text if len(text) <= width else text[: width - 1] + "…"


def _table(headers: list[str], rows: list[list[str]]) -> None:
    widths = [max(len(str(h)), *(len(str(r[i])) for r in rows)) if rows else len(str(h))
              for i, h in enumerate(headers)]
    print("  ".join(str(h).ljust(w) for h, w in zip(headers, widths, strict=True)).rstrip())
    print("  ".join("-" * w for w in widths))
    for r in rows:
        print("  ".join(str(c).ljust(w) for c, w in zip(r, widths, strict=True)).rstrip())


class _Parser(argparse.ArgumentParser):
    """argparse exits 2 on a usage error; 2 is GATE_FAILURE in the public exit-code contract.

    A tool reading exit codes (the eval harness, the plugin command, an orchestrating agent) would read
    a typo'd subcommand as a phantom gate failure and stop a healthy run. Usage errors are exit 1
    (PIPELINE_CONFIG_ERROR) here — the same reasoning rfc-agent's --steer path already applies.
    """

    def error(self, message: str):  # noqa: D102
        self.print_usage(sys.stderr)
        print(f"{self.prog}: error: {message}", file=sys.stderr)
        raise SystemExit(int(ExitCode.PIPELINE_CONFIG_ERROR))


def build_parser() -> argparse.ArgumentParser:
    load_registries()
    parser = _Parser(
        prog=PROG, description="Run agent workflows on the agnostic harness kernel.")
    sub = parser.add_subparsers(dest="command", required=True, parser_class=_Parser)

    run = sub.add_parser("run", help="run a workflow")
    run_sub = run.add_subparsers(dest="workflow", required=True, parser_class=_Parser)
    for name in workflows_mod.names():
        wf = workflows_mod.get(name)
        wp = run_sub.add_parser(name, help=wf.description or f"run the {name} workflow")
        if wf.add_inputs:
            wf.add_inputs(wp)
        add_harness_args(wp)

    fp = sub.add_parser("flags", help="list/explain feature flags")
    fp.add_argument("--json", action="store_true", dest="json_out")
    fp.add_argument("--explain", metavar="NAME", help="show every layer's value and which one wins")
    fp.add_argument("--workflow", metavar="W", help="only the flags this workflow registered")

    ep = sub.add_parser("engines", help="list engine adapters with their doctor status")
    ep.add_argument("--json", action="store_true", dest="json_out")

    wp2 = sub.add_parser("workflows", help="list discovered workflows")
    wp2.add_argument("--json", action="store_true", dest="json_out")

    dp = sub.add_parser("doctor", help="preflight: config, engines, MCP servers stages reference")
    dp.add_argument("--json", action="store_true", dest="json_out")
    dp.add_argument("--workflow", metavar="W", help="check only this workflow")

    rp = sub.add_parser("record", help="build a replay recording from a finished run dir")
    rp.add_argument("run_dir")
    rp.add_argument("recording_dir")
    rp.add_argument("--json", action="store_true", dest="json_out")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "run":
            return run_workflow(args.workflow, args, _hooks_for(args.workflow), parser=parser)
        return {"flags": cmd_flags, "engines": cmd_engines, "workflows": cmd_workflows,
                "doctor": cmd_doctor, "record": cmd_record}[args.command](args)
    except ValueError as exc:  # bad --set / --stage-engine syntax, unknown flag name
        print(f"{PROG}: {exc}", file=sys.stderr)
        return int(ExitCode.PIPELINE_CONFIG_ERROR)
    except PipelineConfigError as exc:
        print(f"PIPELINE CONFIG ERROR: {exc}", file=sys.stderr)
        return int(ExitCode.PIPELINE_CONFIG_ERROR)


def _hooks_for(workflow: str) -> RunHooks:
    """A workflow package may expose `RUN_HOOKS` for its operator-machine side effects (D13)."""
    with contextlib.suppress(ModuleNotFoundError, AttributeError):
        mod = __import__(f"workflows.{workflow}", fromlist=["RUN_HOOKS"])
        hooks = getattr(mod, "RUN_HOOKS", None)
        if isinstance(hooks, RunHooks):
            return hooks
    return RunHooks()


if __name__ == "__main__":
    raise SystemExit(main())
