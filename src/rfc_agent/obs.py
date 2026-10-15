"""Langfuse observability at the harness level: one trace per run, one span per stage, one generation
per agent run (opened BEFORE the engine so its wall-clock is real), tool events emitted LIVE by the
engine's stream tail (real timestamps, input AND output), and the online gate as a scored event.

Posture (cribbed from animus/integrations/langfuse.py): observability must NEVER fail a run — unconfigured
keys, a missing package, or any SDK/network error degrade to a no-op with a single stderr warning.

SDK note: langfuse 4.14 removed `span.update_trace()`; trace attributes go through
`propagate_attributes(session_id=...)` (must wrap the root span's lifetime) and `span.set_trace_io()`.
The old call raised AttributeError on every run and the suppress() ate it — session/input were silently
null on all traces. Anything suppressed here must first be proven present in a real trace (tests can't).

EMBEDDING PRECONDITION: this module assumes one run per process — the ambient stage/attempt, step
counter, tool budget and client cache are module globals, and the SDK adopts any pre-registered
global TracerProvider (foreign samplers would then drop spans; Langfuse FAQ "existing-otel-setup").
If rfc_agent is ever run in-process inside a host service: fix the globals first and pass an
isolated `tracer_provider` to the client. As a standalone CLI, neither hazard is reachable.
"""
from __future__ import annotations

import contextlib
import json
import re
import sys
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .contracts import RunContext, StageResult

_client = None
_warned = False
# per stage — a runaway agent must not flood the trace. 600, not 300: review's 3 personas emit their
# tool events live (0.22.36) and share this stage budget; a 2-attempt persona panel measured ~210 calls.
_MAX_TOOL_EVENTS = 600
_tool_events_left = _MAX_TOOL_EVENTS

# the stage attempt agent_generation is currently running — subagent_span reads it so persona/shard
# nodes inherit the attempt suffix (without it, `shard:lab_buddy` from attempts 1 and 2 collapsed
# into one aggregated node with a (1/2) counter — seen live on the HEA-TAX12 smoke, 2026-08-16)
_current_attempt = 1

# ...and the stage itself, so EVERY observation carries `metadata.{stage, attempt}` of its own
# (0.22.64): tool/subagent/decision observations seen in isolation — an annotation-queue item, an
# API result, a dataset item's provenance — were anonymous, attributable only by walking the parent
# chain in the tree view. NOT propagate_attributes: that writes TRACE-level metadata (verified in
# the SDK's merge code), where generate-then-review would leave the trace's `stage` key churning.
_current_stage: str | None = None


def _stage_md() -> dict | None:
    """Ambient `{stage, attempt}` for observations created inside a stage's engine run, or None
    outside one. Plain globals, not contextvars: fan-out worker THREADS must see them too."""
    return {"stage": _current_stage, "attempt": _current_attempt} if _current_stage else None


# Declared graph structure (0.22.67, node-rich since 0.22.68): Langfuse's Aggregated agent graph
# has TWO renderers. The generalized one infers steps from timing overlap and draws a cartesian
# product of arrows between adjacent concurrency groups of NAME buckets — with 455 tools that is
# the unreadable spaghetti, and it is unfixable from the emission side because one `tool:Bash`
# bucket merges calls from every stage and grows arrows across all of them. But when ANY
# observation carries `langgraph_node` (string) + non-zero `langgraph_step` (number) in its
# metadata, the UI switches to the framework path (TraceGraphView.tsx: hasStepData →
# transformLanggraphToGeneralized → buildGraphFromStepData, source-verified 2026-08-18): nodes are
# OUR declared node names (repeats collapse, with a count), edges connect CONSECUTIVE DECLARED
# STEPS only (steps with no observations are skipped), and observations without a node are absent.
#
# So we declare the graph the old view SHOWED but with sane arrows: every observation keeps its own
# box, node names are LANE-UNIQUE (tool:Bash@generate#4) so nothing merges across stages, and steps
# are pure EXECUTION ORDER — each node's step is minted at its FIRST APPEARANCE (0.22.70; the
# earlier fixed sub-step layout put all of a lane's tool names on ONE step, drawing a parallel fan
# that implied concurrency that never happened). A generate lane reads
#   stage:<s>#n → agent:<s>#n → llm:<s>#n → tool:Skill@… → tool:Bash@… → tool:Read@… → gate:<s>#n
# in first-use order; later calls of the same tool join its bucket (the position holds). The ONLY
# shared step is the persona/shard worker row — they genuinely run concurrently. Between-lane
# runner decisions (back_jump, revise_in_place) sequence naturally (gate:review#4 →
# decision:back_jump → stage:generate#5). Operator events form one node at flush. Root + subprocess
# decisions stay off the graph (metadata-promotion hazard).
_step_counter = 0
_node_steps: dict = {}  # step key -> step, minted at FIRST APPEARANCE in execution order
import threading as _threading  # noqa: E402

_steps_lock = _threading.Lock()  # tools mint steps from CONCURRENT persona/shard drain threads


def _step_for(key: str) -> int:
    """The step for a graph node (or a shared-step group), minted at first appearance. Steps are
    pure EXECUTION ORDER — the graph chain reads exactly as the run unfolded (team, 2026-08-18:
    a same-step parallel fan of tool names implied concurrency that never happened; the honest
    aggregated rendering is first-use order: llm → tool:Skill → tool:Bash → tool:Read → …). Only
    genuinely concurrent things share a step: persona/shard workers pass a lane-scoped group key.
    Later calls of the same node join its existing step (the bucket keeps its position)."""
    global _step_counter
    with _steps_lock:
        if key not in _node_steps:
            _step_counter += 1
            _node_steps[key] = _step_counter
        return _node_steps[key]


def _next_step() -> int:
    global _step_counter
    with _steps_lock:
        _step_counter += 1
        return _step_counter


def _graph_md(node: str | None, step: int | None) -> dict:
    """`{langgraph_node, langgraph_step}` or {} — an observation without them is simply absent
    from the Aggregated graph, never wrongly placed."""
    return {"langgraph_node": node, "langgraph_step": step} if node and step else {}


def _mark(ctx: RunContext, as_type: str, node: str, step: int | None,
          stage: str, attempt: int) -> None:
    """Zero-duration `start:<node>` marker, emitted for the STAGE node only, so the lane exists in
    the graph WHILE the real spans are still open. Spans export on CLOSE, so a 20-minute in-flight
    stage otherwise renders as an orphan tool row — `__start__ → [tools] → __end__` (a teammate's
    screenshots, 2026-08-18). The marker carries the same node/step; the real span joins the same
    bucket when it closes. Deliberately NOT emitted for agent/llm: mid-run those two missing boxes
    are harmless (empty steps are skipped), and the extra tree rows bought nothing (team)."""
    client = _get_client(ctx)
    if client is None or step is None:
        return
    with contextlib.suppress(Exception):
        client.start_observation(as_type=as_type, name=f"start:{node}",
                                 metadata={"stage": stage, "attempt": attempt,
                                           "langgraph_node": node, "langgraph_step": step}).end()


# Snapshot of the MAIN thread's OTel context, captured at engine start (0.22.66). Why: the
# propagated identity attributes (run_id, session, user — `propagate_attributes` at trace open)
# live in the main thread's context via contextvars, which do NOT cross into ThreadPoolExecutor
# workers. Worker-created tool spans (review personas, ground shards) therefore exported with NO
# identity attributes — invisible in the public API (it strips trace-scoped keys from observation
# records) but visible in the UI, which renders raw span attributes (caught by a teammate comparing a
# generate tool, 17 metadata items, against a persona tool, 9 items). Attaching this snapshot
# around worker-side span creation restores them; parenting is unaffected because the SDK layers
# the explicit parent handle ON TOP of the current context (`use_span(self._otel_span)`).
_stage_otel_ctx = None

# Cross-process trace attachment (W3C traceparent, the OTEL convention for env propagation):
# run_claude exports this for the agent subprocess, so grandchildren like `python -m rfc_agent.selfcheck`
# can attach their decision events to the LIVE trace instead of minting orphan traces (observed: 11
# session-less `decision:qualified_bare_citations` traces from one run, 2026-08-12).
TRACEPARENT_ENV = "RFC_AGENT_TRACEPARENT"

# A generation's INPUT is the training half of every dataset item Saurav's annotate->dataset loop
# produces ("Add to dataset" copies the observation's input/expected_output), so a clipped prompt
# silently corrupts the pair rather than merely shortening a display. Measured 2026-08-16 on
# PROJ-3394-v3: the largest generate prompt was 18,564 chars against the old 20,000 cap — 7% headroom,
# i.e. one larger evidence pack from truncating. 100K gives ~5x. Outputs keep the 20K default: they
# hold the agent's closing message, not the deliverable (that lives on disk).
_GEN_INPUT_LIMIT = 100_000


def _warn_once(msg: str) -> None:
    global _warned
    if not _warned:
        print(f"[obs] langfuse disabled: {msg}", file=sys.stderr)
        _warned = True


def _release() -> str | None:
    """The installed plugin version — Langfuse `release` segments every metric (gate pass-rate, cost,
    latency) by the code that produced it, which is the whole cross-version benchmark story."""
    import contextlib as _ct
    import json as _json
    from pathlib import Path as _P
    with _ct.suppress(Exception):
        pj = _P(__file__).resolve().parents[2] / ".claude-plugin" / "plugin.json"
        return str(_json.loads(pj.read_text())["version"])
    return None


_git_email: str | None = None


def _user_id(ctx: RunContext) -> str:
    """Operator identity for the trace — NEVER the Claude account (a single Max plan shared by the
    team). Precedence: OBS.USER_ID (explicit — CI/shared boxes) > `git config user.email` (the
    engineer's canonical org identity, configured on every machine that commits) > $USER (laptop
    login) > unknown."""
    cfg = ctx.config
    explicit = cfg.get("OBS.USER_ID") if cfg else None
    if explicit:
        return str(explicit)
    global _git_email
    if _git_email is None:
        import subprocess as _sp
        try:
            r = _sp.run(["git", "config", "--get", "user.email"], capture_output=True, text=True, timeout=5)
            _git_email = r.stdout.strip() if r.returncode == 0 else ""
        except Exception:  # noqa: BLE001 — identity must never fail a run
            _git_email = ""
    import os as _os
    return _git_email or str(_os.environ.get("USER") or "unknown")


_claude_email: str | None = None


def _claude_account(ctx: RunContext) -> str:
    """The logged-in Claude account, as trace METADATA — never user_id (the team shares one Max plan,
    so as an identity it collapses everyone; as metadata it attributes cost/limits to the right
    subscription and distinguishes runs on different accounts). Source: the CLI's own record,
    `<config-dir>/.claude.json` -> oauthAccount.emailAddress."""
    global _claude_email
    if _claude_email is None:
        import json as _json
        import os as _os
        from pathlib import Path as _P
        _claude_email = ""
        cfg_dir = (ctx.config.get("AGENT.CLAUDE_CONFIG_DIR") if ctx.config else None) or \
            _os.environ.get("CLAUDE_CONFIG_DIR") or "~"
        with contextlib.suppress(Exception):
            data = _json.loads((_P(cfg_dir).expanduser() / ".claude.json").read_text())
            _claude_email = str((data.get("oauthAccount") or {}).get("emailAddress") or "")
    return _claude_email or "unknown"


def _get_client(ctx: RunContext):
    global _client
    if _client is not None:
        return _client or None
    cfg = ctx.config
    public = cfg.get("LANGFUSE.PUBLIC_KEY") if cfg else None
    secret = cfg.get("LANGFUSE.SECRET_KEY") if cfg else None
    if not (public and secret):
        # DELIBERATELY no direct env-var read here (0.23.3 revert of a 0.23.2 mistake): env
        # credentials already reach cfg via config._ENV_MAP (env-wins, at load) — so through any
        # real Config this branch means "genuinely no keys anywhere". Reading env here made the
        # TEST SUITE construct real clients whenever a developer had LANGFUSE_* exported
        # (fixtures pass config=None), shipping synthetic traces to the production project.
        _warn_once("no keys configured (config LANGFUSE.* — env LANGFUSE_PUBLIC_KEY/SECRET_KEY "
                   "reach it via config._ENV_MAP)")
        _client = False
        return None
    try:
        import os

        os.environ.setdefault("OTEL_SERVICE_NAME", "rfc-agent")  # trace attribution in shared projects
        # environment partitioning was a documented CONVENTION set by no code (eval and operator runs
        # shared one pool) — LANGFUSE.ENVIRONMENT makes it enforceable; the env var still wins (smokes)
        if cfg.get("LANGFUSE.ENVIRONMENT"):
            os.environ.setdefault("LANGFUSE_TRACING_ENVIRONMENT", str(cfg.get("LANGFUSE.ENVIRONMENT")))
        from langfuse import Langfuse

        kwargs = {}
        if cfg.get("LANGFUSE.MASK_SECRETS", True):
            # export-stage secret scrub (privacy.py): every span attribute this client ships —
            # tool io, prompts, metadata — passes through the gitleaks-derived ruleset first.
            # Fail-closed: a scrub error masks the span / drops the batch, never ships raw.
            from . import privacy

            kwargs["mask_otel_spans"] = privacy.mask_otel_spans
        # base_url, not the deprecated host= (SDK docstring): base_url is what the SDK derives the
        # OTLP span-export endpoint from — cribbed from animus, which switched for exactly that
        _client = Langfuse(public_key=public, secret_key=secret, base_url=cfg.get("LANGFUSE.HOST") or None,
                           release=_release(), **kwargs)
    except Exception as exc:  # noqa: BLE001 — observability never fails a run
        _warn_once(str(exc))
        _client = False
        return None
    return _client


# a base64 data URI in a tool payload (the agent Reading a PNG, a screenshot tool result). Clipping
# one mid-blob made the Langfuse SDK's media parser traceback on every occurrence (binascii padding +
# non-ASCII '…' inside what still looks like a data URI — observed 22x per snapshot ingest,
# 2026-09-02), so replace the blob with its length BEFORE clipping can cut it.
_DATA_URI_RE = re.compile(r"data:([\w.+-]+/[\w.+-]+);base64,[A-Za-z0-9+/=]{64,}")


_MEDIA_TYPE_KEYS = ("media_type", "mime_type", "mimeType")


def _clip(obj, limit: int = 2000):
    """Recursively truncate long strings so payloads stay dicts (machine-parseable in the UI) instead of
    pre-serialized JSON strings cut mid-token (observed: truncated json.dumps rendered as an opaque blob)."""
    if isinstance(obj, str):
        obj = _DATA_URI_RE.sub(lambda m: f"data:{m.group(1)};base64,[{len(m.group(0))} chars omitted]", obj)
        return obj if len(obj) <= limit else obj[:limit] + f"…[+{len(obj) - limit} chars]"
    if isinstance(obj, dict):
        # an inline base64 media block (the Read tool's image result: {"type":"base64","media_type":...,
        # "data":"iVBOR..."}). Langfuse's exporter detects `"data"` + a media-type key in the serialized
        # payload and REBUILDS a data URI from it — so a clipped `data` string tracebacks in its parser
        # (twice per screenshot Read, observed 2026-09-03 even after the data-URI scrub above). Drop the
        # `data` key entirely: the block's identity and size are the observability value, not the bytes.
        if isinstance(obj.get("data"), str) and any(k in obj for k in _MEDIA_TYPE_KEYS):
            return {k: (f"[{len(v)} base64 chars omitted]" if k == "data" else _clip(v, limit))
                    for k, v in obj.items() if k != "data"} | {"omitted_base64_chars": len(obj["data"])}
        return {k: _clip(v, limit) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_clip(v, limit) for v in obj]
    return obj


def _run_tags(ctx: RunContext, ident: dict) -> list[str]:
    """Trace TAGS — Langfuse's first-class filter chips, which metadata is not. Two axes today:

    `ticket:<key>` groups the sibling experiments of one ticket (PROJ-3394 v1/v2/v3) WITHOUT merging
    their sessions: separate sessions keep per-experiment cost and gate comparison intact, while the
    tag answers "show me everything tried for this ticket". Emitted only when the workstream really
    came from --jira; a run dir named after nothing is not a ticket.

    `ctx:1m` marks a 1M-context run. Read from config at run start because tags ride
    propagate_attributes, which wraps the root's lifetime and cannot learn the model later — the
    precise per-leg truth stays on each generation's metadata.engine_desc.

    Precedence mirrors engine.run_claude: a CLI `--model` overrides DEFAULT_MODEL and STAGE_MODELS
    for every invocation, so when it is set it is the ONLY model judged — OR-ing across all config
    sources tagged a 200K `--model` run as 1M whenever the ignored default carried `[1m]`
    (found in the 2026-08-16 audit)."""
    tags: list[str] = []
    if ident.get("workstream_source") == "jira" and ident.get("workstream_id"):
        tags.append(f"ticket:{ident['workstream_id']}")
    cli = (ctx.inputs or {}).get("model")
    if cli:
        models = [cli]
    else:
        models = []
        cfg = ctx.config
        if cfg is not None:
            with contextlib.suppress(Exception):
                models.append(cfg.get("AGENT.DEFAULT_MODEL") or "")
                models.extend((cfg.get("AGENT.STAGE_MODELS") or {}).values())
    if any("[1m]" in str(m) for m in models):
        tags.append("ctx:1m")
    return tags


def _config_snapshot(ctx: RunContext) -> str | None:
    """The behavior-changing knobs as one compact JSON string, or None. Too long for
    propagate_attributes (its 200-char value cap silently DROPPED the snapshot on every run,
    0.24.19 field observation: "Propagated attribute ... Dropping value") — the caller must emit
    it via root-observation metadata instead, which the backend promotes to trace metadata."""
    with contextlib.suppress(Exception):
        cfg = ctx.config
        if cfg is not None:
            snap = {k: cfg.get(f"AGENT.{k}") for k in (
                "DEFAULT_MODEL", "STAGE_MODELS", "RESUME_CONTEXT_CAP", "RUN_TIMEOUT",
                "THINKING_TAP") if cfg.get(f"AGENT.{k}") is not None}
            if snap:
                s = json.dumps(snap, default=str)
                # parseable or absent — a mid-token [:2000] slice shipped unparseable JSON
                return s if len(s) <= 2000 else json.dumps(
                    {"_omitted": f"snapshot exceeded 2000 chars ({len(s)})"})
    return None


def _provenance(ctx: RunContext) -> dict:
    """Version provenance beyond `release` (2026-08-19 audit: everything that actually varies
    run-to-run — skills, config knobs — was invisible, so cross-`release` benchmarking was only sound
    when plugin.json happened to be bumped). Flat SHORT strings only: these ride propagate_attributes,
    which caps values at 200 chars and silently drops longer ones — the full config snapshot lives on
    the root observation (see _config_snapshot).

    `skills_hash` — one short hash over every stage's SKILL.md (the prompts as shipped): a prompt
    edit between runs of the same release is now a visible metadata change.
    `config_hash` — same comparability signal for the config snapshot; propagated so even a
    hard-killed run (root span never exports) can answer "did the knobs differ"."""
    out: dict = {}
    with contextlib.suppress(Exception):
        import hashlib
        from pathlib import Path as _P
        h = hashlib.sha256()
        root = _P(__file__).resolve().parent
        # profiles/*.md are conditional prompt addenda (fe_profile) — part of "the prompts as
        # shipped" exactly like the stage skills, so they must move the hash too
        for sk in sorted(list((root / "stages").glob("*/SKILL.md")) + list((root / "profiles").glob("*.md"))):
            h.update(sk.name.encode() + b"\0" + sk.read_bytes() + b"\0")
        out["skills_hash"] = h.hexdigest()[:12]
    with contextlib.suppress(Exception):
        import hashlib
        snap = _config_snapshot(ctx)
        if snap:
            out["config_hash"] = hashlib.sha256(snap.encode()).hexdigest()[:12]
    return out


def _attempt_name(base: str, attempt: int) -> str:
    """`stage:generate` on the first try, `stage:generate#2` after that. Langfuse's Aggregated graph
    view is "one node per step name", so identically-named retries COLLAPSE into a single node and the
    rework story — the thing the retry signals exist to show — disappears. Attempt 1 stays unsuffixed
    so the common single-attempt run reads clean (Airflow's try_number displays the same way)."""
    return base if attempt <= 1 else f"{base}#{attempt}"


@contextlib.contextmanager
def _observed_span(ctx: RunContext, name: str, *, as_type: str = "span", is_run_root: bool = False,
                   metadata: dict | None = None, input: dict | None = None):
    """Open an observation that NEVER interferes with pipeline control flow: obs/SDK errors degrade to a
    no-op, but pipeline exceptions (GateFailure etc.) propagate untouched. The naive try/except-around-yield
    pattern swallowed a GateFailure and crashed with 'generator didn't stop after throw()' — this is the
    correct shape: suppress only around SDK calls, yield exactly once, close in finally."""
    client = _get_client(ctx)
    cm = span = prop_cm = None
    if client is not None:
        try:
            cm = client.start_as_current_observation(as_type=as_type, name=name, metadata=metadata,
                                                     input=input)
            span = cm.__enter__()
            if is_run_root:
                with contextlib.suppress(Exception):
                    from langfuse import propagate_attributes

                    from . import identity

                    # session groups crashed/resumed attempts of one run dir; must wrap the root's lifetime.
                    # trace_name rides on EVERY child export — a killed run (root span never ends, so it
                    # alone would carry the name) no longer leaves a nameless trace in the session.
                    # session_id comes from identity.json, NOT run_dir.name: the name is a mutable
                    # filesystem artifact, so renaming a dir used to split one run across two sessions and
                    # two same-named dirs under different roots used to merge into one.
                    ident = identity.load(ctx)
                    prop_cm = propagate_attributes(
                        session_id=ident["session_id"], user_id=_user_id(ctx), trace_name="rfc-run",
                        tags=_run_tags(ctx, ident) or None,
                        metadata={"run_dir": str(ctx.run_dir), "claude_account": _claude_account(ctx),
                                  **identity.trace_metadata(ident), **_provenance(ctx)},
                    )
                    prop_cm.__enter__()
                with contextlib.suppress(Exception):
                    # full config snapshot rides ROOT-observation metadata, which the backend promotes
                    # to trace metadata (observed live, 0.22.64) — propagate_attributes drops any value
                    # over 200 chars, so putting it there silently lost the snapshot on every run.
                    # Deliberate promotion of this ONE key; the root otherwise stays metadata-free
                    # (see run_trace: unintended promotion leaked `stage: "run"` trace-wide).
                    snap = _config_snapshot(ctx)
                    if snap:
                        span.update(metadata={"config_snapshot": snap})
                with contextlib.suppress(Exception):
                    span.set_trace_io(input=_clip(json.loads(json.dumps(ctx.inputs, default=str))))
        except Exception as exc:  # noqa: BLE001 — observability never fails a run
            _warn_once(str(exc))
            cm = span = None
    try:
        yield span
    finally:
        if prop_cm is not None:
            with contextlib.suppress(Exception):
                prop_cm.__exit__(*sys.exc_info())
        if cm is not None:
            with contextlib.suppress(Exception):
                cm.__exit__(*sys.exc_info())
        if is_run_root and client is not None:
            with contextlib.suppress(Exception):
                client.flush()


def run_trace(ctx: RunContext):
    """The run root. `chain`, not `span`: Langfuse builds its agent graph from every observation type
    EXCEPT span/event/generation, so a span-typed root and span-typed stages were excluded outright —
    the graph showed 340 tool nodes and no skeleton to hang them on."""
    # NO observation metadata here: the ROOT span's metadata is promoted to TRACE metadata by the
    # backend (observed live, 0.22.64: `stage: "run"` appeared as a trace-level key on every run) —
    # the same promotion hazard subprocess decisions dodge. Root/operator are run-scoped by
    # definition; only operator events (children, never promoted) carry the explicit `stage: "run"`.
    return _observed_span(ctx, "rfc-run", as_type="chain", is_run_root=True)


@contextlib.contextmanager
def stage_span(ctx: RunContext, name: str, attempt: int = 1, mode: str | None = None):
    """One stage attempt. `chain`-typed so the stage skeleton appears in the graph (see run_trace).
    Carries io since 0.22.61: input identifies the attempt (stage/attempt/mode), output is the gate
    verdict one-liner set by record_gate — stage nodes showed Input/Output: undefined to annotators,
    the largest empty-io class in the 2026-08-17 census (13/13 stage spans).
    Since 0.22.67 it also mints the attempt's GRAPH lane (node = its own name at the lane base;
    children declare sub-steps off the same base) — see _next_lane."""
    node = _attempt_name(f"stage:{name}", attempt)
    step = _step_for(node)
    inp = {"stage": name, "attempt": attempt}
    if mode:
        inp["mode"] = mode
    with _observed_span(ctx, node, as_type="chain", input=inp,
                        metadata={"stage": name, "attempt": attempt,
                                  "langgraph_node": node, "langgraph_step": step}) as span:
        _mark(ctx, "chain", node, step, name, attempt)
        yield span


class AgentRun:
    """Handles for one live stage-agent run: the `agent`-typed observation (drives the Agent Graph) and
    its child `generation` (the ONLY type that carries model/usage/cost — Langfuse best practice splits
    the two; a generation-typed subagent hid it from the graph, an agent-typed one lost cost analytics).
    Both are opened at engine start so their durations are the agent's REAL wall-clock (the old post-hoc
    replay recorded 354s scans as 3ms generations)."""

    def __init__(self, agent=None, gen=None, thinking_path=None, thinking_offset=0):
        self.agent = agent
        self.gen = gen
        self.thinking_path = thinking_path  # logs/<stage>.thinking.txt — the tap's full-text capture
        self.thinking_offset = thinking_offset  # file size at attempt start — the tap appends forever


@contextlib.contextmanager
def agent_generation(ctx: RunContext, stage_name: str, attempt: int = 1):
    """Wrap the LIVE engine run: an `agent` observation as the current context (so live tool observations
    nest under it, siblings of the generation — the documented subagent shape), plus the child generation.
    Both carry the attempt suffix: they repeat once per retry and would otherwise collapse in the
    Aggregated graph exactly as the stage spans did."""
    global _tool_events_left, _current_attempt, _current_stage, _stage_otel_ctx
    _tool_events_left = _MAX_TOOL_EVENTS  # per-stage budget reset
    _current_attempt = attempt  # ambient for subagent_span/tools — fan-out helpers run inside this context
    _current_stage = stage_name
    with contextlib.suppress(Exception):  # identity for worker-thread spans — see _stage_otel_ctx
        from opentelemetry import context as _octx
        _stage_otel_ctx = _octx.get_current()
    # `agent:<stage>` / `llm:<stage>`, NOT `<stage>:agent` (renamed 0.22.67): every observation
    # name is now uniformly `<category>:<instance>` — stage:generate, agent:generate, llm:generate,
    # gate:generate, tool:Bash, persona:skeptic, decision:back_jump
    agent_name = _attempt_name(f"agent:{stage_name}", attempt)
    llm_name = _attempt_name(f"llm:{stage_name}", attempt)
    with _observed_span(ctx, agent_name, as_type="agent",
                        metadata={"stage": stage_name, "attempt": attempt,
                                  **_graph_md(agent_name, _step_for(agent_name))}
                        ) as agent:
        gen = None
        if agent is not None:
            with contextlib.suppress(Exception):
                gen = agent.start_observation(
                    as_type="generation", name=llm_name,
                    metadata={"stage": stage_name, "attempt": attempt,
                              **_graph_md(llm_name, _step_for(llm_name))})
        tp = ctx.run_dir / "logs" / f"{stage_name}.thinking.txt"
        toff = 0
        with contextlib.suppress(OSError):
            toff = tp.stat().st_size if tp.exists() else 0
        try:
            yield AgentRun(agent, gen, tp, toff)
        finally:
            _current_attempt = 1  # hygiene: never leak an attempt number into the next stage
            _current_stage = None
            _stage_otel_ctx = None
            if gen is not None:
                with contextlib.suppress(Exception):
                    gen.end()


def subagent_span(ctx: RunContext, name: str, detail: str = "", thinking_path=None):
    """An `agent`-typed observation for one fan-out worker (review persona, ground shard) — created in
    the MAIN thread (so it nests under the ambient <stage>:agent), handed into the worker thread as the
    explicit parent for its live tool events. Returns a handle with .end(), or None. This is the
    documented subagent shape: the Agent Graph renders each worker as its own node, tools beneath it.
    The name inherits the ambient stage attempt (`shard:lab_buddy#2` on a retry) so the Aggregated
    view keeps attempts distinct instead of collapsing same-named workers across attempts."""
    client = _get_client(ctx)
    if client is None:
        return None
    with contextlib.suppress(Exception):
        worker_name = _attempt_name(name, _current_attempt)
        md = _stage_md() or {}
        # ONE shared step for all of a lane's workers: personas/shards genuinely run concurrently,
        # so the parallel row is honest here (and only here)
        md.update(_graph_md(worker_name, _step_for(f"@workers:{_current_stage}#{_current_attempt}")))
        handle = client.start_observation(as_type="agent", name=worker_name,
                                          input={"detail": detail} if detail else None,
                                          metadata=md or None)
        if thinking_path is not None:
            # breadcrumb for subagent_finished (same pattern as _rfc_stream_ns): the worker's OWN
            # thinking tap + where this round starts in it — fan-out stages' richest reasoning lived
            # only on disk (548KB across 3 persona files while llm:review carried a 0-char digest)
            with contextlib.suppress(Exception):
                off = thinking_path.stat().st_size if thinking_path.exists() else 0
                handle._rfc_thinking = (thinking_path, off)  # noqa: SLF001
        return handle
    return None


def subagent_finished(handle, output=None, stream=None) -> None:
    """Close a fan-out worker's observation WITH an outcome — an ended agent node whose output is
    `undefined` reads as unfinished in the UI (seen live on the HEA-TAX12 smoke). Callers pass a short
    outcome string ("wrote review/persona_skeptic.md, 4,213 bytes" / "no fragment — failed").

    `stream` = the worker's OWN StreamResult (0.23.0): fan-out generations carried only the
    first-finished worker's usage/model and a synthetic shared prompt, so the per-worker truth —
    this worker's cost, tokens, model, denials, and the EXACT prompt it received (warm-resume
    prompts included) — now lands on the worker's own node."""
    if handle is None:
        return
    with contextlib.suppress(Exception):
        kw = {}
        if output is not None:
            kw["output"] = _clip(output)
        md: dict = {}
        # digest preference mirrors finish_generation: the worker's own deliberation blocks (whole
        # decision turns from its stream), tap-file head+tail as the fallback
        delib = _deliberation_digest(getattr(stream, "deliberation", None) if stream is not None else None)
        tp = getattr(handle, "_rfc_thinking", None)
        digest = delib or (_thinking_digest(tp[0], start=tp[1]) if tp is not None else "")
        if digest:
            md["thinking_digest"] = digest
            md["thinking_digest_source"] = "deliberation" if delib else "tap_headtail"
        if stream is not None:
            u = stream.usage or {}
            md.update({
                "cost_usd": stream.cost_usd,
                "model": stream.model,
                "num_turns": stream.num_turns,
                "denials": getattr(stream, "denials", 0),
                "compactions": getattr(stream, "compactions", 0),
                "usage": {k: u.get(k, 0) for k in (
                    "input_tokens", "output_tokens",
                    "cache_read_input_tokens", "cache_creation_input_tokens")},
                "prompt": _clip(getattr(stream, "prompt_sent", "") or "", 4000),
            })
        if md:
            kw["metadata"] = md
        if kw:
            handle.update(**kw)
        handle.end()


def _deliberation_digest(blocks, cap: int = 20000) -> str:
    """Turn-separated digest of the NON-tool thinking blocks (StreamResult.deliberation) — the
    analysis/pivot/conclusion turns, which is where the decisions live. Replaces head+tail clipping
    of the whole stream: head+tail deterministically deleted the middle (4.6% coverage of a 130KB
    stream measured on PROJ-3394-v7), and mid-stream is where the blocker-resolving design decisions
    sat. Whole blocks only, most recent kept first under the cap (per-tool `reason` carries the
    earlier acting context), elisions named so the reader knows to open logs/*.thinking.txt."""
    kept: list[str] = []
    total = 0
    for b in reversed(blocks or []):
        t = " ".join(str(b).split())
        if not t:
            continue
        if kept and total + len(t) > cap:
            break
        if len(t) > cap:  # a single oversized block still lands, clipped
            t = t[:cap] + "…"
        kept.append(t)
        total += len(t)
    out = "\n---\n".join(reversed(kept))
    elided = len(blocks or []) - len(kept)
    if elided > 0:
        out = (f"[{elided} earlier deliberation block(s) elided — full text in "
               f"logs/*.thinking.txt]\n---\n" + out)
    return out


def _thinking_digest(path, cap: int = 6000, start: int = 0) -> str:
    """Head+tail clip of the captured thinking text ('' when absent) — bounded so a 38KB-per-leg
    reasoning stream never bloats the trace, while opening moves and final conclusions both survive.

    `start` = byte offset where THIS attempt's thinking begins: the tap APPENDS across attempts and
    invocations (observed 356KB after 9 generate attempts), so an un-offset digest opened with
    attempt-1's thinking on every later attempt — only the tail half was current (team,
    2026-08-19). A file smaller than `start` was recreated; digest it from the top."""
    try:
        if path is None or not path.exists():
            return ""
        raw = path.read_bytes()
        if 0 < start < len(raw):
            raw = raw[start:]
        t = raw.decode("utf-8", errors="replace").strip()
        if len(t) <= cap:
            return t
        half = cap // 2
        return t[:half] + "\n[... thinking clipped ...]\n" + t[-half:]
    except OSError:
        return ""


def finish_generation(run: AgentRun | None, result: StageResult) -> None:
    """Attach model/usage/cost/io to the still-open handles (per Saurav: capture all agent turns)."""
    if run is None or (run.agent is None and run.gen is None):
        return
    try:
        usage = result.usage or {}
        engine_kind, _, model = result.engine_desc.partition(":")  # "claude:claude-sonnet-5"
        # BARE model name — Langfuse model analytics key on exact names, so decorations must go:
        # "claude-opus-5 (3-persona parallel)" and "claude-sonnet-5[1m]" each become their own bogus
        # model. Suffix info survives in metadata.engine_desc below.
        import re as _re
        bare_model = _re.sub(r"\[[^\]]*\]$", "", (model or engine_kind).split(" ")[0])
        # non-model sentinels must never enter Langfuse's per-model analytics: "cache" (all-cache
        # fan-out) poisoned it in the 2026-08-19 audit, and "crashed" (no-stream crash recovery)
        # would have re-introduced the same class one release later (review finding #5)
        if bare_model in ("cache", "crashed"):
            bare_model = None
        prompt, final = result.prompt or "", result.final_text or ""
        io = {"input": _clip(prompt, _GEN_INPUT_LIMIT), "output": _clip(final, 20000)}
        if run.agent is not None:
            # the agent node gets an EXCERPT, not the full prompt: it exists to identify the leg at a
            # glance and drive the graph, while the generation one level down carries the training-grade
            # copy. Duplicating the raw prompt doubled the payload of the largest field on every stage
            # attempt (measured 200,004 bytes for one attempt at the cap) and bought a reviewer nothing.
            run.agent.update(input=_clip(prompt, 2000), output=_clip(final, 2000))
        if run.gen is not None:
            delib = _deliberation_digest(getattr(result, "deliberation", None))
            digest = delib or _thinking_digest(run.thinking_path, start=run.thinking_offset)
            run.gen.update(
                **({"model": bare_model} if bare_model else {}),
                **io,
                # FULL token accounting — cache reads dominate agentic runs (observed: 4.79M cache-read
                # vs 110 "input" on one scan); dropping them under-reports volume by ~4 orders of magnitude
                usage_details={
                    "input": usage.get("input_tokens", 0),
                    "output": usage.get("output_tokens", 0),
                    "cache_read_input_tokens": usage.get("cache_read_input_tokens", 0),
                    "cache_creation_input_tokens": usage.get("cache_creation_input_tokens", 0),
                },
                cost_details={"total": result.cost_usd or 0.0},
                metadata={
                    # the WHY layer: reasoning summaries are the only signal carrying cause attribution
                    # and intent (the 2214 finding-loss was diagnosable ONLY from a thinking line).
                    # Preferred source = the stream's deliberation blocks (non-tool thinking — the
                    # decision turns, whole and turn-separated); tap-file head+tail is the fallback
                    # for streams without thinking text. Tool-paired thinking rides each tool
                    # observation as `reason`. Evidence, not testimony (CoT faithfulness).
                    "thinking_digest": digest,
                    "thinking_digest_source": (
                        "deliberation" if delib else ("tap_headtail" if digest else "absent")),
                    "tool_call_count": len(result.tool_calls),
                    "num_turns": result.num_turns,
                    "engine": engine_kind,
                    "engine_desc": result.engine_desc,  # full form incl. "[1m]" / "(3-persona parallel)"
                    # the two best predictors of a bad run (2026-08-19 audit) — previously they died
                    # in activity.log/.session files: denials cost silent reformulation round-trips,
                    # a compaction means the session's memory was summarized away mid-stage
                    "denials": result.denials,
                    "compactions": result.compactions,
                    **({"context_tokens": result.context_tokens}
                       if result.context_tokens is not None else {}),
                },
            )
    except Exception as exc:  # noqa: BLE001
        _warn_once(str(exc))


def _event_ns(ts) -> int | None:
    """Epoch nanoseconds from a stream event's ISO-ms timestamp ('2026-08-14T16:53:13.699Z').
    None on anything unparseable — callers fall back to drain-clock timing."""
    if not ts:
        return None
    with contextlib.suppress(Exception):
        from datetime import UTC, datetime, timedelta

        # integer arithmetic, not timestamp()*1e9: float multiplication at 2026-epoch magnitude
        # drifts ~100ns, which leaks straight into the duration delta
        micros = (datetime.fromisoformat(ts) - datetime(1970, 1, 1, tzinfo=UTC)) // timedelta(microseconds=1)
        return micros * 1_000
    return None


def tool_started(ctx: RunContext, tool_name: str, tool_input, parent=None, event_ts=None,
                 reason=None, reason_scope=None):
    """Open a `tool`-typed observation the moment the tool_use appears in the stream tail, typed so
    evaluators/the Agent Graph can filter tools (an untyped event could not). `parent` (a live
    observation handle) routes the child through the handle instead of the ambient OTEL context —
    REQUIRED from worker threads, where the ambient context is empty and a context-parented observation
    would mint its own trace (why review's 200+ persona tool calls were invisible pre-0.22.36).

    `event_ts` is the enclosing stream event's own timestamp (the CLI stamps every assistant/user
    event at millisecond precision — verified 56/56 and 27/27 on a real stream, 2026-08-16 audit; this
    doc previously claimed no timestamps existed). It is remembered on the handle so tool_finished can
    set an EXACT duration: the span's wall-clock position is still the drain read (the SDK exposes no
    start_time), but position error ≤ one poll while duration becomes stream-true.

    `reason` = the thinking block governing this call (engine drain), the per-action WHY beside the
    action it explains; `reason_scope` says how tight the pairing is ("same_message" = the tool_use
    shares the thinking block's assistant message — Anthropic-doc-sanctioned attribution — vs
    "carried" = most recent earlier block). METADATA, deliberately not input: the tool input must
    stay a faithful copy of the tool's args (the annotate->dataset loop copies observation io).
    Evidence, not testimony (CoT faithfulness 25-39% under hint injection): read it against the
    action and result beside it. Clipped to 800 chars — batched tool_use blocks repeat their shared
    reason so each observation stays attributable in isolation (the 0.22.64 principle).

    Returns an opaque handle for tool_finished, or None (budget spent / obs disabled)."""
    global _tool_events_left
    client = _get_client(ctx)
    if client is None or _tool_events_left <= 0:
        return None
    _tool_events_left -= 1
    with contextlib.suppress(Exception):
        src = parent if parent is not None else client
        # worker threads have no OTel context, so spans created here lost the propagated identity
        # attributes (run_id/session/user) — attach the main thread's snapshot for the creation call;
        # the explicit parent handle still owns the nesting (see _stage_otel_ctx)
        token = None
        if parent is not None and _stage_otel_ctx is not None:
            with contextlib.suppress(Exception):
                from opentelemetry import context as _octx
                token = _octx.attach(_stage_otel_ctx)
        try:
            md = _stage_md() or {}
            if md:
                # per-TOOL-NAME node per lane: `tool:Bash@generate#4` — the per-tool counts the old
                # graph showed survive, but nothing merges across stages (the arrow spaghetti's
                # cause). Step minted at the name's FIRST USE, so tool nodes chain in the order the
                # run actually reached them; later calls of the same tool join the bucket.
                tool_node = _attempt_name(f"tool:{tool_name}@{_current_stage}", _current_attempt)
                md.update(_graph_md(tool_node, _step_for(tool_node)))
            if reason:
                md["reason"] = _clip(" ".join(str(reason).split()), 800)
                md["reason_scope"] = reason_scope or "carried"
            handle = src.start_observation(as_type="tool", name=f"tool:{tool_name}", input=_clip(tool_input),
                                           metadata=md or None)
        finally:
            if token is not None:
                with contextlib.suppress(Exception):
                    _octx.detach(token)
        ns = _event_ns(event_ts)
        if ns is not None:
            with contextlib.suppress(Exception):
                handle._rfc_stream_ns = ns  # noqa: SLF001 — our own breadcrumb for tool_finished
        return handle
    return None


def tool_finished(handle, output=None, event_ts=None) -> None:
    """Close a tool observation when its result arrives (or at run end for never-answered calls).

    When BOTH stream timestamps are known (tool_use event on the handle, tool_result event here), the
    span is ended at `otel_start + (ts_result - ts_use)` so its DURATION is the stream-true execution
    time to the millisecond, independent of the drain poll. Any missing/garbled timestamp — including
    the run-end sweep of never-answered calls — falls back to the drain clock, exactly the old
    behaviour. Negative deltas (clock weirdness) also fall back rather than emit a lying span."""
    if handle is None:
        return
    with contextlib.suppress(Exception):
        handle.update(output=_clip(output))
        end_ns = _event_ns(event_ts)
        start_ns = getattr(handle, "_rfc_stream_ns", None)
        otel_start = getattr(getattr(handle, "_otel_span", None), "start_time", None)
        if end_ns is not None and start_ns is not None and otel_start is not None and end_ns >= start_ns:
            handle.end(end_time=otel_start + (end_ns - start_ns))
        else:
            handle.end()


def traceparent() -> str | None:
    """The current trace context as a W3C traceparent (`00-<trace_id>-<span_id>-01`), for handing to
    subprocesses via TRACEPARENT_ENV. None when no client or no span is current."""
    if not _client:
        return None
    with contextlib.suppress(Exception):
        tid, sid = _client.get_current_trace_id(), _client.get_current_observation_id()
        if tid and sid:
            return f"00-{tid}-{sid}-01"
    return None


def _env_trace_context() -> dict | None:
    """Parse TRACEPARENT_ENV into a Langfuse trace_context — how a grandchild process (selfcheck) points
    its events at the run's live trace."""
    import os
    with contextlib.suppress(Exception):
        parts = (os.environ.get(TRACEPARENT_ENV) or "").split("-")
        if len(parts) == 4 and parts[1] and parts[2]:
            return {"trace_id": parts[1], "parent_span_id": parts[2]}
    return None


def decision_event(ctx: RunContext, stage: str, decision: str, why: str, details: dict | None = None,
                   attempt: int | None = None, parent=None) -> None:
    """A harness decision as a first-class signal (per Saurav: record what was decided and why).

    Attachment order: ambient span (main process, mid-run) > TRACEPARENT_ENV (a subprocess like selfcheck,
    attaching to the run trace that spawned it — flushed immediately, the process is short-lived) > SKIP.
    Never create_event with no context: that mints an orphan trace with no session/user (observed 11/run),
    and decisions.jsonl already holds the record.

    The env path carries its payload in METADATA, not input: the SDK stamps `as_root` on trace_context
    observations and the server promotes a root span's present fields to trace level — an input here
    overwrote the run's trace input (verified live, smoke 0.22.36). Absent fields are not promoted.

    Typed `chain`, not `event`: Langfuse's agent graph excludes span/event/generation, so every
    decision — back_jump, revise_in_place, patch_revision, the whole control-flow story — was invisible
    in the one view meant to show control flow. A zero-duration chain observation carries the same
    payload and renders. (The provenance literature rates decision points with rationale the highest-
    value nodes in an agent trace; ours were the ones being dropped.)"""
    client = _get_client(ctx)
    if client is None:
        return
    with contextlib.suppress(Exception):
        payload = _clip({"stage": stage, "why": why, **(details or {})})
        # attempt-suffixed like stages/gates/agents: unsuffixed repeating decisions were the graph's
        # time-fusion anchors — one decision:revise_in_place node held instances at steps 2 AND 195
        # (T3 replay, 2026-08-17), so the layout pulled attempt-4 work above attempt-1 work. Explicit
        # attempt wins (runner call sites, where ambient is stale); ambient covers engine/stage sites.
        att = attempt if attempt is not None else _current_attempt
        name = _attempt_name(f"decision:{decision}", att)
        if parent is not None:
            # worker-thread emission (0.24.2 — fan-out pumps): ambient context is empty there and
            # the env-traceparent path reads the PARENT's env (never set), so steering deliveries
            # from ground shards / review personas were invisible in the trace. Route through the
            # live handle exactly like tool_started: explicit parent owns the nesting, the main
            # thread's context snapshot restores the propagated identity attributes.
            token = None
            if _stage_otel_ctx is not None:
                with contextlib.suppress(Exception):
                    from opentelemetry import context as _octx
                    token = _octx.attach(_stage_otel_ctx)
            try:
                gmd = _graph_md(name, _step_for(name))
                parent.start_observation(as_type="chain", name=name, input=payload,
                                         metadata={"stage": stage, "attempt": att, **gmd}).end()
            finally:
                if token is not None:
                    with contextlib.suppress(Exception):
                        _octx.detach(token)
            return
        if client.get_current_trace_id():
            # main-process path only: the subprocess path below must NOT carry a bare `stage` key —
            # as_root promotion would write it into TRACE metadata (same hazard as `input` there).
            # Graph identity: an ENGINE decision (ambient stage running) sits mid-lane at the
            # decision sub-step; a RUNNER decision between stages (back_jump, revise_in_place,
            # auto_retry — ambient cleared) gets its own sequential step, so the control flow reads
            # gate:review#4 -> decision:back_jump -> stage:generate#5.
            gmd = _graph_md(name, _step_for(name))
            client.start_observation(as_type="chain", name=name, input=payload,
                                     metadata={"stage": stage, "attempt": att, **gmd}).end()
            return
        tc = _env_trace_context()
        if tc:
            # nested under one SELF-DESCRIBING key: as_root promotion ALSO merges this metadata into
            # the TRACE's metadata (verified live), where a bare `decision` key reads as a trace-level
            # fact and is silently overwritten by the next subprocess decision
            client.start_observation(trace_context=tc, as_type="chain", name=name,
                                     metadata={"last_subprocess_decision": payload}).end()
            client.flush()


def gate_started(ctx: RunContext, stage_name: str, attempt: int = 1):
    """Open the gate's evaluator observation BEFORE evaluation runs, so its latency is the true
    verdict cost — gate evaluation is real work (ground#2's witness re-scan ran ~91s) and the old
    create-and-end-instant emission rendered every gate as 0.00s. Returns None when unconfigured."""
    client = _get_client(ctx)
    if client is None:
        return None
    with contextlib.suppress(Exception):
        gate_name = _attempt_name(f"gate:{stage_name}", attempt)
        return client.start_observation(
            as_type="evaluator", name=gate_name,
            metadata={"stage": stage_name, "attempt": attempt,
                      **_graph_md(gate_name, _step_for(gate_name))},
        )
    return None


def record_gate(ctx: RunContext, stage_name: str, failures: list[str], attempt: int = 1,
                handle=None, inert: list[str] | None = None) -> None:
    """The online gate: an `evaluator`-typed observation (gates ARE deterministic evaluators — the type
    makes them filterable and correctly shaped in the Agent Graph) + a numeric trace score (per Saurav:
    signals at each gate). Attempt-suffixed like the stage and agent nodes: a gate that failed then
    passed is the recovery signal, and two same-named nodes collapse it into one. Pass the handle from
    gate_started for true latency; without it the observation is instant (legacy shape).

    `inert` = checks that DISABLED themselves this evaluation (gates.drain_inert): a PASS with the
    witness gate inert was byte-identical in the trace to a fully-policed PASS — the most dangerous
    false green found in the 2026-08-19 audit. `failure_classes` makes the failure taxonomy
    aggregable (transition matrices, per-class counts) instead of free-text archaeology."""
    client = _get_client(ctx)
    if client is None:
        return
    ev = None
    with contextlib.suppress(Exception):
        from .gates import blocker_anchors as _ba
        from .gates import failure_classes as _fc

        gate_name = _attempt_name(f"gate:{stage_name}", attempt)
        ev = handle or client.start_observation(
            as_type="evaluator", name=gate_name,
            metadata={"stage": stage_name, "attempt": attempt,
                      **_graph_md(gate_name, _step_for(gate_name))}
        )
        fail_md = {}
        if failures:
            # blocker_anchors beside failure_classes (0.24.54): review's failure_classes is the
            # single degenerate class review_blocker, so the CHANGE/REQ ids the blockers name are
            # the real grouping key — and the same-anchor-across-rounds join is the blind-retry
            # signal (v7: CHANGE-10 in two consecutive review rounds)
            fail_md["failure_classes"] = _fc(failures)
            anchors = _ba(failures)
            if anchors:
                fail_md["blocker_anchors"] = anchors
        ev.update(input={"failure_count": len(failures), "checks_inert": inert or []},
                  output=failures[:20] or "PASS", level="ERROR" if failures else "DEFAULT",
                  **({"metadata": fail_md} if fail_md else {}))
        ev.end()

    if stage_name == "ground" and not failures:
        # the evidence pack as a first-class trace artifact (0.24.57, team: "where is the
        # evidence pack here?"): the GROUND->GENERATE handoff reads findings.md from DISK, so the
        # trace only ever held a pointer (the kickoff prompt) and 2K-clipped tool:Read outputs —
        # "did GENERATE receive the fact" (Q2 row 6) required the laptop that ran the pipeline.
        # On every PASSING ground evaluation the assembled findings.md ships whole as its own
        # chain observation under the still-open stage span: full-text Ctrl-F from any machine,
        # survives run-dir archival. ~57KB typical, 300K clip cap vs the ~1MB event budget.
        # Deliberately NO langgraph keys — a snapshot is not a step in the control flow.
        with contextlib.suppress(Exception):
            txt = (ctx.run_dir / "findings" / "findings.md").read_text(
                encoding="utf-8", errors="replace")
            if txt.strip():
                client.start_observation(
                    as_type="chain", name="artifact:findings.md",
                    output=_clip(txt, 300_000),
                    metadata={"stage": stage_name, "attempt": attempt,
                              "path": "findings/findings.md", "bytes": len(txt.encode())},
                ).end()

    def _gate_score(**kw):
        # ANCHOR the score to the evaluator observation (ev.score) rather than bare trace level:
        # one score either way — it still lists on the trace and aggregates in dashboards — but an
        # anchored score also shows on the gate node's own Scores tab. Field finding 2026-08-20:
        # every score had observationId null, so clicking ANY node showed "No scores found" and the
        # verdicts looked missing unless you knew to open the trace header. Trace-level fallback
        # keeps the score flowing if the evaluator observation failed to open.
        if ev is not None:
            ev.score(**kw)
        else:
            client.score_current_trace(**kw)

    if inert:
        with contextlib.suppress(Exception):
            _gate_score(
                name=f"gate_{stage_name}_inert_checks", value=float(len(inert)),
                comment=f"checks silently disabled this evaluation: {', '.join(inert)} — "
                "a PASS here enforces a strictly weaker contract than the gate declares")
    with contextlib.suppress(Exception):
        # ...and onto the STAGE span, which is still the current observation here (the agent and its
        # generation closed before the gate ran). Reviewers annotating a single observation cannot see
        # the rest of the trace — a documented Langfuse limitation — so the attempt's verdict has to
        # travel WITH the attempt or an annotator is judging an output without knowing if it shipped.
        # Deliberately not stamped on the generation: that would mean holding it open past the engine
        # call it measures, which is the dangling-span class this module already fixed once.
        client.update_current_span(
            # the stage node's OUTPUT is the verdict one-liner — self-describing for annotators
            output=("gate passed" if not failures
                    else f"gate_failed: {len(failures)} failure(s) — {failures[0][:300]}"),
            metadata={
            "gate_passed": not failures,
            "gate_failure_count": len(failures),
            "gate_failures": _clip(failures[:5]),
        })
    with contextlib.suppress(Exception):
        # BOOLEAN, not bare numeric (score-format audit 2026-08-21): Langfuse's standard for
        # binaries — renders True/False, still averages as 0/1 in dashboards
        _gate_score(name=f"gate_{stage_name}_passed", value=0.0 if failures else 1.0,
                    data_type="BOOLEAN")
    with contextlib.suppress(Exception):
        # the pass/fail flattened a 1-blocker fail and a 12-blocker fail into the same 0 — the
        # per-evaluation failure COUNT is the convergence gauge (is review finding fewer blockers
        # per round?), so it must be a score, not observation metadata. 0 on pass, deliberately:
        # a chart needs the zeros.
        _gate_score(name=f"gate_{stage_name}_failure_count", value=float(len(failures)))
    with contextlib.suppress(Exception):
        # attempt TERMINAL STATE (score-format research 2026-08-24, xUnit failure≠error): every
        # attempt emits exactly one — passed / gate_failed here, engine_failed from the runner's
        # crash paths. This is the availability denominator gate_*_passed lacks:
        # crashed attempts previously emitted NO score at all, so pass-rate averages silently
        # excluded the worst outcomes (5 of radiology-v3's 7 review attempts were invisible).
        _gate_score(name=f"{stage_name}_attempt_outcome",
                    value="gate_failed" if failures else "passed", data_type="CATEGORICAL")


def record_attempt_outcome(ctx: RunContext, stage_name: str, outcome: str, attempt: int = 1) -> None:
    """Terminal state for an attempt that never reached its gate — `engine_failed` (crash/timeout).
    Same score family record_gate emits for evaluated attempts, so `<stage>_attempt_outcome` covers
    EVERY attempt: pass rate and crash rate come from one categorical, and neither contaminates
    gate_<stage>_passed (which stays conditional on the gate actually evaluating — the JUnit
    failure-vs-error line)."""
    client = _get_client(ctx)
    if client is None:
        return
    with contextlib.suppress(Exception):
        client.score_current_trace(
            name=f"{stage_name}_attempt_outcome", value=outcome, data_type="CATEGORICAL",
            comment=f"attempt {attempt} ended without a gate evaluation: {outcome}")


def operator_actions_flush(ctx: RunContext) -> None:
    """Emit the operator's choices (recorded by the command layer to logs/operator_actions.jsonl —
    open-question answers, fix-and-refetch vs proceed-degraded, resume decisions, ratings) as
    first-class events on the CURRENT trace. The human's response to a signal is itself a signal:
    without it the loop reads signal -> harness reaction -> outcome with the person edited out.
    An offset-marker file makes the flush idempotent across invocations."""
    client = _get_client(ctx)
    if client is None:
        return
    emitted = _flush_jsonl_events(ctx, client, "operator_actions.jsonl", ".operator_actions.flushed",
                                  lambda rec: f"operator:{rec.get('action', 'action')}")
    # skipped asks are the operator channel's NEGATIVE space (0.23.0): a non-interactive run's owed-
    # but-unasked questions previously existed only in logs/asks_skipped.jsonl — the trace showed the
    # answers humans gave but never the questions nobody was there to answer
    emitted += _flush_jsonl_events(ctx, client, "asks_skipped.jsonl", ".asks_skipped.flushed",
                                   lambda rec: "operator:ask_skipped")
    if emitted:
        # force the SDK's batch exporter NOW: measured 2026-08-19 — without this the OTEL batch
        # delay added ~4s of the ~5s end-to-end steer→Langfuse latency (with it, server createdAt
        # lands ~0.2s after the observation). Operator actions are a few per HOUR, so a per-batch
        # network flush is costless; same pattern as decision_event's short-lived-subprocess path.
        with contextlib.suppress(Exception):
            client.flush()


def _rating_value(rec: dict):
    """The 1-5 rating inside an rfc_rating record, wherever the command layer put it — None if absent."""
    detail = rec.get("detail") if isinstance(rec.get("detail"), dict) else {}
    for v in (detail.get("rating"), detail.get("value"), rec.get("rating"), rec.get("value")):
        with contextlib.suppress(TypeError, ValueError):
            f = float(v)
            if 1 <= f <= 5:
                return f
    return None


def _rating_comment(rec: dict, det: dict) -> str:
    """Grounding provenance ON the score itself (feedback-timing research, 2026-08-20): judge
    calibration must be able to EXCLUDE ungrounded labels — a rating collected before the operator
    opened the document rates the report, not the artifact, and a low-effort label poisons the
    golden set. The comment therefore always states what engagement evidence exists (or that none
    was recorded) plus the rating's latency since delivery, so a calibration query can filter on it."""
    bits = []
    note = det.get("worst_problem") or det.get("note")
    if note:
        bits.append(str(note)[:300])
    eng = det.get("engagement") if isinstance(det.get("engagement"), dict) else {}
    seen = sorted(k for k, v in eng.items() if v)
    bits.append("engagement: " + (", ".join(seen) if seen else "NONE RECORDED — ungrounded label"))
    with contextlib.suppress(Exception):
        from datetime import datetime as _dt
        lat = (_dt.fromisoformat(str(rec.get("ts")).replace("Z", "+00:00"))
               - _dt.fromisoformat(str(det["delivered_at"]).replace("Z", "+00:00"))).total_seconds()
        bits.append(f"rated {lat:.0f}s after delivery")
    return "; ".join(bits)


def _flush_jsonl_events(ctx: RunContext, client, filename: str, marker_name: str, name_fn) -> int:
    """Returns the number of NEW records emitted this call (0 = nothing to export)."""
    src = ctx.run_dir / "logs" / filename
    if not src.exists():
        return 0
    marker = ctx.run_dir / "logs" / marker_name
    emitted = 0
    with contextlib.suppress(Exception):
        done = int(marker.read_text()) if marker.exists() else 0
        lines = src.read_text(encoding="utf-8", errors="replace").splitlines()
        for ln in lines[done:]:
            with contextlib.suppress(Exception):
                rec = json.loads(ln)
                # `chain`, not `event`, for the same reason decisions are (0.22.47): Langfuse's agent
                # graph excludes `event`, and the human's response to a signal is the one thing Saurav
                # asked to keep visible — an invisible operator node reads as "the harness did this".
                # PER-ACTION graph nodes (0.24.5): the old single collapsed "operator" node hid WHAT
                # the operator did — every action folded into one anonymous box at __start__. Now
                # stage-boundary flushing places each action at its real position in the flow
                # (… stage:extract → operator:steer → stage:ground …); same-named actions share a
                # node, exactly like tool nodes.
                node = name_fn(rec)
                h = client.start_observation(
                    as_type="chain", name=node, input=_clip(rec),
                    # run-scoped, not stage-scoped: actions flush at the NEXT boundary, so the
                    # ambient stage at flush time would misattribute them — "run" is the honest value
                    metadata={"stage": "run", "langgraph_node": node,
                              "langgraph_step": _step_for(node)},
                )
                # Position the observation at the moment the human ACTED (the record's own ts), not
                # when the batcher ran: flushed-later actions sorted AFTER their consequences —
                # decision:steering_delivered rendered BEFORE the operator:steer that caused it
                # (field screenshot, 2026-08-19), and boundary-flushed fan-out steers landed minutes
                # late. The public API has no start_time (verified 4.14.1: only generations'
                # completion_start_time), so this is a best-effort write to the underlying OTEL
                # span — on failure the observation keeps its creation-time position, and end_time
                # is only passed when the start write SUCCEEDED (an end before start would render
                # a lying negative-duration span).
                backdated = False
                ts_ns = _event_ns(rec.get("ts"))
                if ts_ns is not None:
                    with contextlib.suppress(Exception):
                        h._otel_span._start_time = ts_ns  # noqa: SLF001 — same access tool_finished reads
                        backdated = True
                if backdated:
                    h.end(end_time=ts_ns)
                else:
                    h.end()
                emitted += 1
                # the human's quality judgment as a SCORE, not just an event: as an observation input
                # it could never be charted or joined against gate_* — which is exactly the
                # judge-calibration comparison the annotate->dataset loop needs (audit 2026-08-19).
                # Scored against the trace that was RATED (logs/.last_trace_id, written by finish_run),
                # never the current one: the rating is collected after a run finishes and flushed on
                # the NEXT invocation, so score_current_trace attached the 2/5 to the improved rerun
                # — or nowhere at all when the rated run was the last (review finding #2). No stored
                # id -> no score (a misattributed calibration point is worse than a missing one).
                if rec.get("action") == "rfc_rating":
                    det = rec.get("detail") if isinstance(rec.get("detail"), dict) else {}
                    # the record's OWN trace id (stamped at delivery by the command layer) wins:
                    # .last_trace_id is overwritten by EVERY invocation, so a rating deferred past
                    # even one green-skip would otherwise score the wrong trace (found 2026-08-20 —
                    # deferral is now the NORM, since ratings are engagement-gated, never immediate)
                    rated = str(det.get("trace_id") or "").strip()
                    if not rated:
                        with contextlib.suppress(OSError):
                            rated = (ctx.run_dir / "logs" / ".last_trace_id").read_text().strip()
                    if rated:
                        comment = _rating_comment(rec, det)
                        # provenance as score METADATA too (audit 2026-08-21): comments are for
                        # humans; a calibration query must not parse prose to exclude ungrounded
                        # labels
                        smd = {k: det.get(k) for k in ("engagement", "delivered_at", "trace_id")
                               if det.get(k) is not None}
                        val = _rating_value(rec)
                        if val is not None:
                            client.create_score(
                                name="human_rfc_rating", value=val, trace_id=rated,
                                comment=comment, metadata=smd or None)
                        # binary per-dimension verdicts (Hamel/Braintrust shape: Likert points are
                        # inconsistent between raters; decomposed pass/fail calibrates) — each its
                        # own score so gate-vs-human joins stay one filter per dimension
                        for score_name, key in (("human_ship_asis", "ship_asis"),
                                                ("human_direction_ok", "direction_ok")):
                            if isinstance(det.get(key), bool):
                                client.create_score(
                                    name=score_name, value=float(det[key]), trace_id=rated,
                                    data_type="BOOLEAN", comment=comment, metadata=smd or None)
        marker.write_text(str(len(lines)))
    return emitted


def finish_run(ctx: RunContext, manifest: dict, ran: set | None = None) -> None:
    """Trace-level output: per-stage verdicts + total cost (was null — the run's outcome wasn't on the trace).

    `ran` = the stages THIS invocation executed. Rework scores are emitted only for those: the manifest
    is cumulative across resumes, so re-scoring every entry made each later invocation of a lineage
    re-emit every earlier stage's rework (audit 2026-08-16: `ingest_rework_attempts` 5x on one lineage,
    values [2,2,2,2,2] — any dashboard SUM/AVG double-counts). None = emit all (pre-fix behaviour)."""
    client = _get_client(ctx)
    if client is None:
        return
    # flush AGAIN at run end (idempotent via the offset markers): CLI-native operator actions
    # recorded mid-run — answers_given, checkpoint notes — previously reached a trace only if the
    # run dir was ever re-invoked, and then the WRONG trace (review finding #4). Flushing here
    # lands them on the invocation that produced them, while its trace is still open.
    with contextlib.suppress(Exception):
        operator_actions_flush(ctx)
    # persist THIS trace's id: the post-run rfc_rating flushes on the NEXT invocation and must
    # score the run it rated, not the run doing the flushing (review finding #2)
    with contextlib.suppress(Exception):
        tid = client.get_current_trace_id()
        if tid:
            (ctx.run_dir / "logs").mkdir(parents=True, exist_ok=True)
            (ctx.run_dir / "logs" / ".last_trace_id").write_text(str(tid))
    with contextlib.suppress(Exception):
        # per-stage entries are CUMULATIVE MANIFEST STATE (whenever each stage last ran), not this
        # invocation's work — a resume's trace showed ingest/ground costs it never touched and read
        # as "ran in this trace" (verified live on PROJ-3394-v5 run 6, 2026-08-19).
        # `ran_this_invocation` makes the distinction explicit.
        summary = {
            name: {"status": e.get("status"), "cost_usd": e.get("cost_usd"),
                   "duration_s": e.get("duration_s"),
                   **({"inputs_digest": e["inputs_digest"]} if e.get("inputs_digest") else {}),
                   **({"ran_this_invocation": name in ran} if ran is not None else {})}
            for name, e in manifest.items()
            if isinstance(e, dict)
        }
        # totals must count EVERY attempt: summing last-attempt cost_usd under-reported any retried stage
        from .runner import run_totals

        cost, secs, tries = run_totals(manifest)
        summary["total_cost_usd"] = cost
        summary["total_duration_s"] = secs
        summary["stage_attempts"] = tries
        # which code this RFC was grounded against — computed by evidence.py since 0.22.x, previously
        # never emitted: without it no trace was reproducible ("which HEADs?" had no answer)
        with contextlib.suppress(Exception):
            heads = json.loads(
                (ctx.run_dir / "findings" / "evidence.json").read_text(encoding="utf-8")).get("heads")
            if heads:
                summary["repo_heads"] = heads
        # re-assert INPUT alongside output: a trace_context observation from a subprocess is stamped
        # as_root by the SDK and its present fields get promoted to trace level mid-run — the final
        # write here makes the completed run's trace io authoritative regardless
        client.set_current_trace_io(  # root span is current here (stage spans closed)
            input=_clip(json.loads(json.dumps(ctx.inputs, default=str))), output=summary)
        # mirror the same io onto the ROOT SPAN observation: trace io lives on the trace header, but
        # clicking the rfc-run node in the graph opens the root OBSERVATION, which showed
        # Input/Output: undefined (operator confusion, 2026-08-17). Own suppress: a failure here
        # must never eat the scores below.
        with contextlib.suppress(Exception):
            client.update_current_span(
                input=_clip(json.loads(json.dumps(ctx.inputs, default=str))), output=summary)
        # signal RESOLUTION as aggregable scores: a gate that failed and later passed is a recovery —
        # attempts>1 quantifies the rework a signal produced (dashboards: rework rate per release/user)
        client.score_current_trace(
            # "total": the manifest sums EVERY attempt across crashes/resumes of this run dir, so it
            # exceeds the trace's own cost (this attempt's generations only) — the two are different
            # numbers on purpose, and the unlabeled pair was read as a bug (Saurav review, 2026-08-12)
            name="run_total_cost_usd", value=float(cost or 0),
            comment="sum of every attempt in this run dir (incl. crashed/resumed traces); "
            "the trace-level cost covers only this attempt's generations — the SESSION-scoped "
            "score of the same name is the authoritative lineage total",
        )
        # ...and the same number at SESSION scope (score-format audit 2026-08-21): the lineage
        # total's structural home — summing traces isn't even possible there, which kills the
        # "never SUM across the lineage" foot-gun. Stable score_id = upsert: one score per
        # lineage, updated by every invocation, never accumulated duplicates.
        with contextlib.suppress(Exception):
            import uuid as _uuid

            from . import identity as _identity

            ident = _identity.load(ctx)
            if ident.get("session_id"):
                client.create_score(
                    name="run_total_cost_usd", value=float(cost or 0),
                    session_id=ident["session_id"],
                    score_id=str(_uuid.uuid5(_uuid.NAMESPACE_URL,
                                             f"run-total:{ident.get('session_uuid') or ident['session_id']}")),
                    comment="lineage total — session-scoped, safe to read directly")
        for name, e in manifest.items():
            if ran is not None and name not in ran:
                continue  # a resume must not re-score stages it never touched
            if isinstance(e, dict) and (e.get("attempts") or 1) > 1:
                client.score_current_trace(name=f"{name}_rework_attempts", value=float(e["attempts"]))
        # deliverable sizes as scores: verbosity drift is tracked across releases here, never
        # policed by the composer at exit (the SKILL budget is a compose-time target only)
        for rel in ("out/rfc.md", "out/contract.md", "out/detail/plan.md"):
            p = ctx.run_dir / rel
            if p.exists():
                client.score_current_trace(name=f"{p.stem}_bytes", value=float(p.stat().st_size))
        # the coverage DENOMINATOR (2026-08-19 audit): every "did the RFC cover the requirements"
        # question needs the REQ count, which previously never left extract/requirements.json
        with contextlib.suppress(Exception):
            reqs = json.loads((ctx.run_dir / "extract" / "requirements.json")
                              .read_text(encoding="utf-8", errors="replace")).get("requirements")
            if isinstance(reqs, list) and reqs:
                client.score_current_trace(name="req_count", value=float(len(reqs)))
