"""Langfuse tracing for the new pipeline (Scope → Summarize).

The old stages trace through obs.py, which is built around their RunContext/StageResult. The new stages share
none of that, so this module is the new pipeline's own thin layer. It reuses obs.py's client-free helpers
(payload clipping, plugin release, operator identity) and privacy.py's export-time secret scrub.

Trace shape (one trace per CLI invocation; a resumed run lands in the same Langfuse session):

    rfc-run                                  root; session = the run folder's identity.json
    ├─ stage:scope                            one `chain` per stage main()
    │   ├─ llm:prd-summary                   one-shot model calls (generation)
    │   ├─ agent:atlassian → llm:atlassian
    │   └─ operator:confirm                  the user's selection
    ├─ stage:investigate
    │   ├─ agent:lead:1a → llm:lead:1a       every `claude -p` agent: agent + generation (cost, tokens)
    │   │   └─ tool:Read …                   one span per tool call — only when LANGFUSE.VERBOSE
    │   ├─ gate:T1 …                         every mechanical check, with a pass/fail score
    │   └─ operator:investigate-pause1
    └─ …

Config (config.json, env wins, a CLI flag wins over both):

    LANGFUSE.ENABLED  true   RFC_AGENT_LANGFUSE          --langfuse true|false
    LANGFUSE.VERBOSE  false  RFC_AGENT_LANGFUSE_VERBOSE  --langfuse-verbose true|false

ENABLED with no keys FAILS the run before anything is spent, and says how to turn tracing off. Once a run is
going, tracing never fails it: an SDK or network error is one stderr warning and the run carries on.

One run per process: the state below is module-global, like obs.py's.
"""

from __future__ import annotations

import contextlib
import functools
import re
import sys
import threading
from pathlib import Path

FLAG = "--langfuse"
VERBOSE_FLAG = "--langfuse-verbose"
ENV = "RFC_AGENT_LANGFUSE"
VERBOSE_ENV = "RFC_AGENT_LANGFUSE_VERBOSE"
EXIT_NO_KEYS = 2

_MAX_TOOL_SPANS = 3000  # per invocation — a runaway agent must not flood the trace
_GEN_INPUT_LIMIT = 20_000

_overrides: dict[str, bool] = {}  # set by the CLI flags
_lock = threading.Lock()
_client = None
_verbose = False
_root = None  # the open root observation, or None
_root_otel_ctx = None  # main-thread OTel context: carries the propagated identity into worker threads
_stages: list = []  # open stage observations, innermost last
_tool_spans_left = _MAX_TOOL_SPANS
_warned = False


# ---- settings ---------------------------------------------------------------------------------------------


def _as_bool(v) -> bool | None:
    if isinstance(v, bool):
        return v
    if v is None:
        return None
    s = str(v).strip().lower()
    if s in ("1", "true", "yes", "on"):
        return True
    if s in ("0", "false", "no", "off"):
        return False
    return None


def _setting(key: str, env: str, default: bool, cfg) -> bool:
    import os

    if key in _overrides:
        return _overrides[key]
    if env in os.environ and _as_bool(os.environ[env]) is not None:
        return bool(_as_bool(os.environ[env]))
    val = _as_bool(cfg.get(f"LANGFUSE.{key}") if cfg is not None else None)
    return default if val is None else val


def _load_cfg():
    from .config import get_config

    return get_config()


def settings(cfg=None) -> dict:
    """{"enabled", "verbose"} after flags > env > config > defaults (true, false). The config is only loaded
    when the flags and env don't already decide `enabled` (tests run with tracing off and no config)."""
    if cfg is None:
        if _setting("ENABLED", ENV, True, None) is False:  # off by flag or env: no config needed
            return {"enabled": False, "verbose": False}
        cfg = _load_cfg()
    return {
        "enabled": _setting("ENABLED", ENV, True, cfg),
        "verbose": _setting("VERBOSE", VERBOSE_ENV, False, cfg),
    }


def take_flags(argv: list[str]) -> list[str]:
    """Strip --langfuse / --langfuse-verbose (`--x val` or `--x=val`) from argv into the overrides."""
    out: list[str] = []
    i = 0
    while i < len(argv):
        a = argv[i]
        for flag, key in ((FLAG, "ENABLED"), (VERBOSE_FLAG, "VERBOSE")):
            val = None
            if a == flag and i + 1 < len(argv):
                val, i = argv[i + 1], i + 1
            elif a.startswith(flag + "="):
                val = a.split("=", 1)[1]
            else:
                continue
            b = _as_bool(val)
            if b is None:
                raise SystemExit(f"{flag} takes true or false, got {val!r}")
            _overrides[key] = b
            break
        else:
            out.append(a)
        i += 1
    return out


def _keys(cfg) -> tuple[str, str]:
    return (str(cfg.get("LANGFUSE.PUBLIC_KEY") or ""), str(cfg.get("LANGFUSE.SECRET_KEY") or ""))


def missing_keys_message(cfg) -> str:
    from .config import _LOCAL

    where = getattr(cfg, "_local", None) or _LOCAL
    return (
        "Langfuse observability is ON but the Langfuse keys are missing.\n"
        f"  Either add LANGFUSE.PUBLIC_KEY and LANGFUSE.SECRET_KEY to {where}\n"
        "  (or export LANGFUSE_PUBLIC_KEY / LANGFUSE_SECRET_KEY),\n"
        f"  or run without tracing: add `{FLAG} false` to the command, or set LANGFUSE.ENABLED to false in the config."
    )


# ---- client -----------------------------------------------------------------------------------------------


def _warn(msg: str) -> None:
    global _warned
    if not _warned:
        print(f"[observe] langfuse: {msg} — tracing off for the rest of this run", file=sys.stderr)
        _warned = True


def _make_client(cfg):
    import os

    from langfuse import Langfuse

    from .obs import _release

    os.environ.setdefault("OTEL_SERVICE_NAME", "rfc-agent")
    if cfg.get("LANGFUSE.ENVIRONMENT"):
        os.environ.setdefault("LANGFUSE_TRACING_ENVIRONMENT", str(cfg.get("LANGFUSE.ENVIRONMENT")))
    kwargs = {}
    if cfg.get("LANGFUSE.MASK_SECRETS", True):
        from . import privacy

        kwargs["mask_otel_spans"] = privacy.mask_otel_spans
    public, secret = _keys(cfg)
    return Langfuse(
        public_key=public, secret_key=secret, base_url=cfg.get("LANGFUSE.HOST") or None, release=_release(), **kwargs
    )


def active() -> bool:
    return _root is not None


def verbose() -> bool:
    return active() and _verbose


def _clip(obj, limit: int = 2000):
    from .obs import _clip as clip

    return clip(obj, limit)


def _bare_model(model: str | None) -> str | None:
    """Langfuse model analytics key on exact names: drop the `[1m]` context suffix."""
    return re.sub(r"\[[^\]]*\]$", "", model) if model else None


@contextlib.contextmanager
def _in_root_ctx():
    """Worker threads have no OTel context, so spans they create would lose the propagated identity
    (session, user, run ids). Attach the main thread's context for the creation call; the explicit parent
    handle still decides the nesting."""
    token = None
    if _root_otel_ctx is not None and threading.current_thread() is not threading.main_thread():
        with contextlib.suppress(Exception):
            from opentelemetry import context as octx

            token = octx.attach(_root_otel_ctx)
    try:
        yield
    finally:
        if token is not None:
            with contextlib.suppress(Exception):
                from opentelemetry import context as octx

                octx.detach(token)


def _parent():
    return _stages[-1] if _stages else _root


def _child(as_type: str, name: str, *, parent=None, **kw):
    """Start a child observation under `parent` (default: the innermost open stage). None when off/failed."""
    p = parent if parent is not None else _parent()
    if p is None:
        return None
    try:
        with _in_root_ctx():
            return p.start_observation(as_type=as_type, name=name, **kw)
    except Exception as exc:  # noqa: BLE001 — tracing never fails a run
        _warn(str(exc))
        return None


def _end(handle, **update) -> None:
    if handle is None:
        return
    with contextlib.suppress(Exception):
        if update:
            handle.update(**update)
        handle.end()


# ---- run + stage ------------------------------------------------------------------------------------------


class _IdCtx:
    """The two fields identity.load reads off a RunContext."""

    def __init__(self, run_dir: Path, jira: str | None) -> None:
        self.run_dir = run_dir
        self.inputs = {"jira": jira} if jira else {}
        self.config = None


def _arg(argv: list[str], flag: str) -> str | None:
    for i, a in enumerate(argv):
        if a == flag and i + 1 < len(argv):
            return argv[i + 1]
        if a.startswith(flag + "="):
            return a.split("=", 1)[1]
    return None


def _run_dir_of(argv: list[str], cfg) -> Path | None:
    rd = _arg(argv, "--run-dir")
    if rd:
        return Path(rd).expanduser().resolve()
    if _arg(argv, "--prd"):
        with contextlib.suppress(Exception):
            import argparse

            from .scope.cli import _run_dir

            ns = argparse.Namespace(
                prd=_arg(argv, "--prd"), jira=_arg(argv, "--jira"), run_dir=None, out=_arg(argv, "--out")
            )
            return _run_dir(ns, cfg).expanduser().resolve()
    return None


TRACES_FILE = "traces.jsonl"
_facts_src: tuple[Path, str | None] | None = None  # (run dir, original --prd) for run_facts at close


def _record_trace_id(run_dir: Path, entry: str, trace_id: str | None, prd: str | None = None) -> None:
    """Append `{trace_id, entry, ts}` to <run>/logs/traces.jsonl: one line per traced invocation, so a rating
    given later can be scored against the exact trace (`rfc` = the whole run; a stage name = that stage alone)."""
    if not trace_id:
        return
    import json
    import time

    (run_dir / "logs").mkdir(parents=True, exist_ok=True)
    with (run_dir / "logs" / TRACES_FILE).open("a", encoding="utf-8") as f:
        rec = {"trace_id": trace_id, "entry": entry, "ts": time.time(), **({"prd": prd} if prd else {})}
        f.write(json.dumps(rec) + "\n")


def _open_root(cfg, run_dir: Path | None, entry: str, argv: list[str]):
    """Open the root observation + trace attributes. Returns the start-as-current context manager."""
    global _client, _root, _root_otel_ctx, _facts_src
    from langfuse import propagate_attributes

    from . import identity
    from .obs import _user_id

    _client = _make_client(cfg)
    cm = _client.start_as_current_observation(as_type="chain", name="rfc-run")
    try:
        _root = cm.__enter__()
    except Exception:
        _client = None
        raise
    prop = None
    with contextlib.suppress(Exception):
        jira = _arg(argv, "--jira")
        if not jira and run_dir is not None:
            with contextlib.suppress(Exception):
                import json

                jira = json.loads((run_dir / "scope.json").read_text(encoding="utf-8")).get("jira")
        md = {"entry": entry, "observability": "verbose" if _verbose else "standard"}
        tags = []
        session = None
        if run_dir is not None:
            ident = identity.load(_IdCtx(run_dir, jira))
            session = ident["session_id"]
            md |= {"run_dir": str(run_dir), **identity.trace_metadata(ident)}
            if ident.get("workstream_source") == "jira":
                tags.append(f"ticket:{ident['workstream_id']}")
        md = {k: v for k, v in md.items() if v is not None}
        prop = propagate_attributes(
            session_id=session,
            user_id=_user_id(_IdCtx(run_dir or Path("."), None)),
            trace_name="rfc-run",
            tags=tags or None,
            metadata={k: str(v) for k, v in md.items()},
        )
        prop.__enter__()
    with contextlib.suppress(Exception):
        _root.set_trace_io(input={"entry": entry, "argv": argv})
    if run_dir is not None:  # feedback (rfc_agent.feedback) scores the trace this run produced
        _facts_src = (run_dir, _arg(argv, "--prd"))
        with contextlib.suppress(Exception):
            _record_trace_id(run_dir, entry, _client.get_current_trace_id(), _arg(argv, "--prd"))
    with contextlib.suppress(Exception):
        from opentelemetry import context as octx

        _root_otel_ctx = octx.get_current()
    return cm, prop


def _close_root(cm, prop, code) -> None:
    global _client, _root, _root_otel_ctx, _tool_spans_left, _warned, _facts_src
    with contextlib.suppress(Exception):
        out = {"exit": code}
        _root.update(output=out, level="ERROR" if code else "DEFAULT")
        _root.set_trace_io(output=out)
    if _facts_src is not None:  # which PRD / repos / how big the RFC / how the review ended — filterable later
        with contextlib.suppress(Exception):
            from . import run_facts

            _root.update(metadata={"run_facts": run_facts.collect(*_facts_src)})
        _facts_src = None
    if prop is not None:
        with contextlib.suppress(Exception):
            prop.__exit__(None, None, None)
    with contextlib.suppress(Exception):
        cm.__exit__(None, None, None)
    with contextlib.suppress(Exception):
        _client.flush()
    _client = _root = _root_otel_ctx = None
    _stages.clear()
    _tool_spans_left = _MAX_TOOL_SPANS
    _warned = False


def _exit_code(e: SystemExit) -> int:
    return e.code if isinstance(e.code, int) else (1 if e.code else 0)


def entry(stage: str | None):
    """Decorator for a CLI `main(argv)`. stage=None is the whole-run entry (rfc_agent.run).

    Strips the --langfuse flags, opens the trace when no trace is open yet (a standalone stage gets its own),
    and wraps a stage main in its `stage:<name>` span. Fails with exit 2 — before the stage starts — when
    tracing is on and the keys are missing."""

    def deco(fn):
        @functools.wraps(fn)
        def wrapper(argv: list[str] | None = None):
            global _verbose
            argv = take_flags(list(sys.argv[1:] if argv is None else argv))
            if stage == "scope" and argv[:1] in (["run-dir"], ["cleanup"]) or {"-h", "--help"} & set(argv):
                return fn(argv)  # utilities and help: nothing to trace
            if _root is not None:  # inside an open trace (a stage under `rfc`, Summarize under Revise)
                return _stage_call(fn, argv)
            s = settings()
            if not s["enabled"]:
                return fn(argv)
            cfg = _load_cfg()
            if not all(_keys(cfg)):
                print(missing_keys_message(cfg), file=sys.stderr)
                return EXIT_NO_KEYS
            _verbose = s["verbose"]
            try:
                cm, prop = _open_root(cfg, _run_dir_of(argv, cfg), stage or "rfc", argv)
            except Exception as exc:  # noqa: BLE001 — an SDK problem must not stop the run
                _warn(str(exc))
                return fn(argv)
            code = 1
            try:
                code = _stage_call(fn, argv) if stage else (fn(argv) or 0)
                return code
            except SystemExit as e:
                code = _exit_code(e)
                raise
            finally:
                _close_root(cm, prop, code)

        def _stage_call(fn, argv):
            if stage is None:
                return fn(argv)
            span = _child("chain", f"stage:{stage}", input={"stage": stage, "argv": argv}, metadata={"stage": stage})
            if span is not None:
                _stages.append(span)
            code = 1
            try:
                code = fn(argv) or 0
                return code
            except SystemExit as e:
                code = _exit_code(e)
                if span is not None and not isinstance(e.code, int) and e.code:
                    with contextlib.suppress(Exception):
                        span.update(status_message=str(e.code)[:500])
                raise
            finally:
                if span is not None:
                    if _stages and _stages[-1] is span:
                        _stages.pop()
                    _end(span, output={"exit": code}, level="ERROR" if code else "DEFAULT")

        return wrapper

    return deco


# ---- agents (one `claude -p` process) ---------------------------------------------------------------------


class AgentTrace:
    """Handles for one agent run: the `agent` node, its `generation` (model, tokens, cost), and — verbose only —
    one `tool` span per tool call, opened at tool_use and closed at its tool_result."""

    def __init__(self, agent, gen) -> None:
        self.agent = agent
        self.gen = gen
        self.tools: dict[str, object] = {}

    def event(self, ev: dict) -> None:
        """Feed one stream-json event (verbose: tool spans)."""
        global _tool_spans_left
        if not _verbose or self.agent is None:
            return
        msg = ev.get("message") or {}
        blocks = msg.get("content") if isinstance(msg.get("content"), list) else []
        for b in blocks:
            if not isinstance(b, dict):
                continue
            if b.get("type") == "tool_use" and b.get("id"):
                with _lock:
                    if _tool_spans_left <= 0:
                        continue
                    _tool_spans_left -= 1
                h = _child("tool", f"tool:{b.get('name')}", parent=self.agent, input=_clip(b.get("input")))
                if h is not None:
                    self.tools[b["id"]] = h
            elif b.get("type") == "tool_result" and b.get("tool_use_id") in self.tools:
                h = self.tools.pop(b["tool_use_id"])
                _end(h, output=_clip(b.get("content")), **({"level": "ERROR"} if b.get("is_error") else {}))

    def finish(
        self,
        *,
        ok: bool,
        error: str = "",
        model: str | None = None,
        output="",
        cost_usd: float = 0.0,
        usage: dict | None = None,
        metadata: dict | None = None,
    ) -> None:
        for h in list(self.tools.values()):
            _end(h, output="(no result: the agent ended first)")
        self.tools.clear()
        level = {} if ok else {"level": "ERROR", "status_message": error[:500]}
        u = usage or {}
        _end(
            self.gen,
            **({"model": _bare_model(model)} if model else {}),
            output=_clip(output, 20_000),
            usage_details={
                "input": int(u.get("input_tokens") or 0),
                "output": int(u.get("output_tokens") or 0),
                "cache_read_input_tokens": int(u.get("cache_read_input_tokens") or 0),
                "cache_creation_input_tokens": int(u.get("cache_creation_input_tokens") or 0),
            },
            cost_details={"total": float(cost_usd or 0)},
            metadata=metadata or {},
            **level,
        )
        _end(
            self.agent,
            output=_clip(output, 2000) if ok else f"FAILED: {error}"[:2000],
            metadata={"model": model, **(metadata or {})},
            **level,
        )


def agent_started(label: str, *, stage: str, step: str | None, model: str | None, prompt: str) -> AgentTrace | None:
    """Open `agent:<label>[:<step>]` + its generation under the current stage. Safe from worker threads."""
    if not active():
        return None
    name = label + (f":{step}" if step and step != "run" else "")
    md = {"stage": stage, "step": step or "run", "label": label}
    agent = _child("agent", f"agent:{name}", input=_clip(prompt, 2000), metadata=md)
    if agent is None:
        return None
    gen = _child(
        "generation",
        f"llm:{name}",
        parent=agent,
        input=_clip(prompt, _GEN_INPUT_LIMIT),
        model=_bare_model(model),
        metadata=md,
    )
    return AgentTrace(agent, gen)


def llm_call(
    name: str,
    *,
    model: str | None,
    prompt: str,
    output,
    cost_usd: float = 0.0,
    usage: dict | None = None,
    seconds: float | None = None,
    error: str = "",
    tool_calls: list[dict] | None = None,
) -> None:
    """Record a call that already finished (Scope's one-shot calls) as agent + generation [+ tool spans]."""
    t = agent_started(name, stage="scope", step=None, model=model, prompt=prompt)
    if t is None:
        return
    if _verbose:
        for c in tool_calls or []:
            h = _child("tool", f"tool:{c.get('name')}", parent=t.agent, input=_clip(c.get("input")))
            _end(h, output=_clip(c.get("output")), **({"level": "ERROR"} if c.get("is_error") else {}))
    t.finish(
        ok=not error,
        error=error,
        model=model,
        output=output,
        cost_usd=cost_usd,
        usage=usage,
        metadata={"seconds": seconds} if seconds is not None else None,
    )


# ---- gates + operator -------------------------------------------------------------------------------------


def gate(
    name: str, must_fix, flags=None, *, kind: str, round_label: str | None = None, stage: str | None = None
) -> None:
    """A mechanical check as an `evaluator` observation + scores on it. `must_fix` is a list, or a
    {check: [problems]} dict (Draft's gate). `kind` names the scores (`gate_<kind>_passed`, `..._must_fix`):
    one stable name per check type, so every tracer's check aggregates into one dashboard series."""
    if not active():
        return
    if isinstance(must_fix, dict):
        failures = [f"{k}: {x}" for k, v in must_fix.items() for x in (v or [])]
        by_check = {k: len(v or []) for k, v in must_fix.items() if v}
    else:
        failures = [str(x) for x in must_fix or []]
        by_check = {}
    flags = list(flags or [])
    full = f"gate:{name}" + (f":{round_label}" if round_label else "")
    ev = _child(
        "evaluator",
        full,
        input={"must_fix_count": len(failures), "flag_count": len(flags)},
        metadata={"stage": stage or "", "kind": kind, **({"failures_by_check": by_check} if by_check else {})},
    )
    if ev is None:
        return
    with contextlib.suppress(Exception):
        verdict = {"must_fix": _clip(failures[:30]), "flags": _clip(flags[:30])} if failures or flags else "PASS"
        ev.update(output=verdict, level="ERROR" if failures else ("WARNING" if flags else "DEFAULT"))
    with contextlib.suppress(Exception):
        ev.score(name=f"gate_{kind}_passed", value=0.0 if failures else 1.0, data_type="BOOLEAN")
    with contextlib.suppress(Exception):
        ev.score(name=f"gate_{kind}_must_fix", value=float(len(failures)))
    _end(ev)


def operator(action: str, data: dict | None = None) -> None:
    """A human decision (a pause answer, the Scope selection) as a `chain` node — events don't show in the
    agent graph."""
    if not active():
        return
    _end(_child("chain", f"operator:{action}", input=_clip(data or {}, 4000), metadata={"actor": "operator"}))


def preflight() -> int:
    """`python -c "from rfc_agent import observe; raise SystemExit(observe.preflight())"` — one status line for
    /rfc-agent:setup, :doctor and :rfc. Exit 2 when tracing is on and the keys are missing. Prints no secrets."""
    s = settings()
    if not s["enabled"]:
        print("langfuse=off")
        return 0
    cfg = _load_cfg()
    ok = all(_keys(cfg))
    print(f"langfuse=on keys={'ok' if ok else 'MISSING'} verbose={str(s['verbose']).lower()}")
    if not ok:
        print(missing_keys_message(cfg))
        return EXIT_NO_KEYS
    return 0
