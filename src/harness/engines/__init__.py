"""Engine protocol + registry. An ENGINE runs one headless agent invocation under a containment policy and
writes a canonical trace. It knows nothing about stages' meaning; the runner and stage-owned fan-out
engines drive it. Adapters: `claude_code` (the existing `claude -p` primitive, default), `pi` (badlogic
pi-coding-agent, `--mode json`), `axon` (the org's managed Pi, `axon pi --mode json`), `replay` (re-emits a
recorded trace + fixture outputs: parity + benchmarks).

Selection is by NAME through flags (`engine`, `stage_engines`), so switching an engine — globally or for one
stage — is an argument, not a code edit: `harness run rfc --engine pi --stage-engine review=claude-code`.
"""

from __future__ import annotations

import importlib
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol, runtime_checkable

from ..events import TraceSummary


@dataclass
class Containment:
    """Engine-neutral containment policy. Each adapter TRANSPORTS it with its native mechanism
    (Claude: PreToolUse hooks + --allowedTools/--disallowedTools/--strict-mcp-config;
    Pi: an extension whose tool_call handler consults `python -m harness.policy`). The POLICY itself
    (path confinement, read-only shell classifier, egress denial) lives once, in harness.policy."""

    writable_roots: tuple[Path, ...] = ()  # only these may be written (the run dir)
    readable_roots: tuple[Path, ...] = ()  # extra read-only mounts (REPOS_ROOT via --add-dir)
    allowed_tools: tuple[str, ...] = ()  # explicit grants (MCP read tools) — engine-native names
    disallowed_tools: tuple[str, ...] = ()  # explicit denies — engine-native names
    mcp_servers: dict = field(default_factory=dict)  # {name: spec} subset to expose; {} = none
    strict_mcp: bool = True  # False -> inherit the operator's user-scope servers (INGEST)
    readonly_shell: bool = True  # allow only provably read-only shell segments without asking
    hooks_enabled: bool = True  # master switch for adapter-side hook transport (flag `hooks`)
    disabled_hooks: tuple[str, ...] = ()  # names of individual hooks turned off (flag `hooks_off`)


@dataclass
class EngineRequest:
    prompt: str
    cwd: Path  # the run dir
    label: str  # namespaces every side file: logs/<label>.events.jsonl, .stderr, ...
    trace_path: Path  # canonical trace destination (usually events_path(cwd, label))
    timeout_s: int
    skill_path: Path | None = None  # SKILL.md to stage as the persona (system-prompt equivalent)
    system_prompt_append: str = ""  # engine-neutral extra system text (sandbox affordances etc.)
    model: str | None = None
    effort: str | None = None
    resume_session: str | None = None  # warm-context revision; adapters without sessions ignore + log
    max_budget_usd: float | None = None
    env: dict[str, str] = field(default_factory=dict)
    containment: Containment = field(default_factory=Containment)
    live: bool = True  # tail the trace into the activity feed
    obs_parent: object | None = None  # observability handle (opaque to engines)
    procs: list | None = None  # fan-out callers collect child handles here to kill stragglers
    steer_epoch: float | None = None  # steering inbox cursor (adapters without a stdin channel ignore)
    raw_trace_path: Path | None = None  # engine-native trace (Claude stream-json) for back-compat readers
    # Engine-native context the kernel does not model YET, keyed by the adapter that reads it. The
    # claude-code adapter takes {"stage": Stage, "ctx": RunContext} this way because `run_claude` still
    # owns MCP materialisation, effort resolution, the Figma binding race and session bookkeeping —
    # rewriting it was explicitly out of scope for this slice (the god-module split is a later one).
    # The KERNEL never reads this dict; treat a request that needs it as an adapter-specific call.
    native_ctx: dict = field(default_factory=dict)


@dataclass
class EngineResult:
    summary: TraceSummary
    duration_s: float
    engine_desc: str  # e.g. "claude-code:claude-sonnet-5" / "pi:anthropic/claude-sonnet-5"
    prompt_sent: str = ""
    trace_path: Path | None = None
    raw_trace_path: Path | None = None
    exit_code: int | None = None
    # the legacy StreamResult when the adapter is claude-code (lets unmigrated callers keep working)
    native: object | None = None


@runtime_checkable
class Engine(Protocol):
    name: str

    def run(self, req: EngineRequest) -> EngineResult: ...

    def supports_resume(self) -> bool: ...

    def doctor(self) -> list[str]:
        """Preflight problems ([] = healthy): binary present, auth, version."""
        ...


# name -> "module:factory" — lazy so importing the kernel never imports an adapter's dependencies
_BUILTIN: dict[str, str] = {
    "claude-code": "harness.engines.claude_code:ClaudeCodeEngine",
    "pi": "harness.engines.pi:PiEngine",
    "axon": "harness.engines.axon:AxonEngine",
    "replay": "harness.engines.replay:ReplayEngine",
}
_REGISTRY: dict[str, Callable[..., Engine]] = {}


def register(name: str, factory: Callable[..., Engine]) -> None:
    if name in _REGISTRY and _REGISTRY[name] is not factory:
        raise RuntimeError(f"duplicate engine name {name!r}")
    _REGISTRY[name] = factory


def names() -> list[str]:
    return sorted(set(_BUILTIN) | set(_REGISTRY))


def engine_from_flags(name: str, flags=None) -> Engine:
    """Build an engine by NAME, taking its construction arguments from the flag registry.

    The point of D4 is that swapping an engine is an argument, not a code edit — which only holds if
    the caller never has to know what a particular adapter needs to be built. `replay` needs a
    recording directory (flag `replay.recording`); `claude-code` and `pi` need nothing. A new adapter
    with its own construction flag adds a line here and a Flag, not a branch at every call site.
    """
    kwargs: dict = {}
    if name == "replay":
        rec = flags.get("replay.recording", "") if flags is not None else ""
        if not rec:
            raise KeyError(
                "engine 'replay' needs a recording: --set replay.recording=<dir> "
                "(build one with `harness record <run_dir> <recording_dir>`)."
            )
        kwargs["recording_dir"] = rec
    return get_engine(name, flags=flags, **kwargs)


def _accepts_flags(factory: Callable[..., Engine]) -> bool:
    """Does this adapter's constructor take `flags`?

    Adapters differ on purpose: `ClaudeCodeEngine` needs only a binary path, while `pi` reads a dozen
    `pi.*` construction flags. Passing `flags` unconditionally would `TypeError` on the former, and
    dropping it (the bug this replaces) made every `pi.*` flag inert — `--set pi.provider=anthropic`
    was silently ignored. So: ask, then pass. A `**kwargs` constructor counts as accepting it.
    """
    import inspect

    try:
        sig = inspect.signature(factory)
    except (TypeError, ValueError):
        return False
    if "flags" in sig.parameters:
        return True
    return any(prm.kind is inspect.Parameter.VAR_KEYWORD for prm in sig.parameters.values())


def get_engine(name: str, **kwargs) -> Engine:
    """Build an adapter by name. `flags=` is dropped for adapters whose constructor does not take it,
    so a caller can pass the resolved flags without knowing each adapter's construction surface."""
    if name in _REGISTRY:
        factory = _REGISTRY[name]
    else:
        spec = _BUILTIN.get(name)
        if spec is None:
            raise KeyError(f"unknown engine {name!r}; known: {names()}")
        mod, _, attr = spec.partition(":")
        factory = getattr(importlib.import_module(mod), attr)
    if "flags" in kwargs and not _accepts_flags(factory):
        kwargs = {k: v for k, v in kwargs.items() if k != "flags"}
    return factory(**kwargs)
