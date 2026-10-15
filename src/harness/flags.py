"""Feature flags — one registry, layered resolution, introspectable by humans AND agents.

Every toggle in the system (harness, engine, workflow, stage) is a `Flag` registered here with a name,
type, default, scope, help text and an optional env var. Resolution precedence (highest wins):

    CLI  (--set name=value, --enable name, --disable name)
  > env  (the flag's declared env var, or HARNESS_<NAME_WITH_DOTS_AS_UNDERSCORES>)
  > config file (the same dotted key in config.json — so today's rfc_agent keys keep working)
  > workflow defaults (a workflow may re-default a harness flag for its runs)
  > flag default

Human-friendly: `harness flags` prints a table; `harness flags --explain <name>` shows every layer's value
and which one won. Agent-friendly: `harness flags --json` emits the full registry with types/defaults/help
as machine-readable JSON, and every resolved run writes `flags.json` into the run dir (what was in effect).

Naming: dotted, lower-case, scope-prefixed — `engine`, `hooks`, `stage_engines`, `rfc.extract.samples`,
`testing.storage`. Booleans are toggles (`--enable/--disable`); everything else is `--set`.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class Scope(StrEnum):
    HARNESS = "harness"  # runner behaviour: engine, hooks, retries, obs
    ENGINE = "engine"  # adapter-specific knobs: claude.*, pi.*
    WORKFLOW = "workflow"  # rfc.*, testing.*
    STAGE = "stage"  # per-stage toggles: stage_engines, disabled_stages


REDACTED = "<redacted>"


@dataclass(frozen=True)
class Flag:
    name: str
    type: type  # bool | int | float | str | list | dict
    default: Any
    help: str
    scope: Scope = Scope.HARNESS
    env: str | None = None  # explicit env var; None -> HARNESS_<NAME>
    choices: tuple | None = None
    # who owns it (docs + `flags --owner`): "harness", an engine name, or a workflow name
    owner: str = "harness"
    # A CREDENTIAL. Its value is never serialised: not into flags.json, not into `flags --json`, not
    # into `--explain`'s layer table, not into a captured CI log. Only the fact that it is set is
    # observable (`"<redacted>"`). Every run dir is a file someone will paste into a ticket, and a
    # store password is exactly the value that must not be in one.
    secret: bool = False
    # Is the flag actually READ through `ctx.flag(name)` yet? A registered-but-unread flag still WORKS —
    # its value reaches the stage through the legacy config key below — but toggling it by its new name
    # is not the mechanism, and D5's promise is a machine-readable list an agent can act on. `False`
    # means "bridged, not yet wired"; `harness flags` marks it and describe() exposes it, so an agent
    # never reads a knob as live when flipping it would change nothing.
    wired: bool = True
    # The dotted config.json key this flag replaced, if any. Two readers depend on it: the CLI's
    # config layer (`workflows.<wf>.config_get`) and `RunContext.flag`'s fallback for callers that
    # never resolved flags at all — without it a config.json carrying the legacy spelling is ignored.
    legacy_config_key: str | None = None

    @property
    def env_var(self) -> str:
        return self.env or "HARNESS_" + self.name.upper().replace(".", "_").replace("-", "_")


_REGISTRY: dict[str, Flag] = {}


def define(flag: Flag) -> Flag:
    existing = _REGISTRY.get(flag.name)
    if existing is not None and existing != flag:
        raise RuntimeError(f"flag {flag.name!r} already defined with a different spec")
    _REGISTRY[flag.name] = flag
    return flag


def registry() -> dict[str, Flag]:
    return dict(_REGISTRY)


def legacy_config_key(name: str) -> str | None:
    """The dotted config.json key a flag replaced, or None. One source of truth for both the CLI's
    config layer and `RunContext.flag`'s fallback."""
    flag = _REGISTRY.get(name)
    return flag.legacy_config_key if flag is not None else None


def bridge_legacy_key(name: str, key: str) -> None:
    """Attach a legacy config key to an ALREADY-DEFINED flag.

    A core flag is defined by the kernel, which cannot know a workflow's config spelling — the kernel
    has no business knowing that `obs_required` used to be `LANGFUSE.REQUIRED`. The workflow that owns
    that history calls this at import time so `RunContext.flag`'s fallback honours an existing
    config.json even for a caller that never resolved flags. Idempotent; refuses to change an
    existing, different mapping, because two workflows claiming one core flag's history is a bug.
    """
    import dataclasses

    flag = _REGISTRY.get(name)
    if flag is None:
        raise KeyError(f"cannot bridge unknown flag {name!r}")
    if flag.legacy_config_key not in (None, key):
        raise RuntimeError(
            f"flag {name!r} already bridges {flag.legacy_config_key!r}; refusing to re-map it to {key!r}"
        )
    _REGISTRY[name] = dataclasses.replace(flag, legacy_config_key=key)


def names_for(owners: set[str]) -> set[str]:
    """Flag names owned by any of `owners` — how a run dir records only the flags that could have
    affected it, instead of every workflow the discovery scan imported."""
    return {n for n, f in _REGISTRY.items() if f.owner in owners}


TRUE_WORDS = ("1", "true", "yes", "on")
FALSE_WORDS = ("0", "false", "no", "off")


def coerce(flag: Flag, raw: Any) -> Any:
    if raw is None:
        return None
    if flag.type is bool:
        if isinstance(raw, bool):
            return raw
        if isinstance(raw, (int, float)) and not isinstance(raw, bool):
            return bool(raw)
        word = str(raw).strip().lower()
        if word in TRUE_WORDS:
            return True
        if word in FALSE_WORDS:
            return False
        # Anything else is a MISTAKE, not a False. The old rule was "true unless it looks true", so
        # `--set hooks=fasle` and `HARNESS_HOOKS=disabled` both silently resolved to False — and
        # `hooks`'s own help calls that "removes containment". A typo must never disarm a guard.
        raise ValueError(
            f"flag {flag.name}={raw!r} is not a boolean — use one of "
            f"{'/'.join(TRUE_WORDS)} or {'/'.join(FALSE_WORDS)}, or the "
            f"--enable {flag.name} / --disable {flag.name} pair."
        )
    if flag.type in (list, dict) and isinstance(raw, str):
        raw = raw.strip()
        if flag.type is list:
            return [x for x in (p.strip() for p in raw.split(",")) if x] if not raw.startswith("[") else json.loads(raw)
        return json.loads(raw) if raw else {}
    if flag.type is dict and isinstance(raw, dict):
        return raw
    if flag.type is list and isinstance(raw, (list, tuple)):
        return list(raw)
    try:
        return flag.type(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"flag {flag.name}: cannot coerce {raw!r} to {flag.type.__name__}") from exc


@dataclass
class Resolved:
    value: Any
    source: str  # "cli" | "env" | "config" | "workflow" | "default"
    layers: dict[str, Any] = field(default_factory=dict)  # every layer that had an opinion


def is_secret(name: str) -> bool:
    flag = _REGISTRY.get(name)
    return bool(flag is not None and flag.secret)


def redact(name: str, value: Any) -> Any:
    """`value` unless the flag is a credential, in which case the fact-of-being-set, never the value."""
    if not is_secret(name):
        return value
    return REDACTED if value not in (None, "", False) else value


class Flags:
    """Resolved view for one run. Construct via `resolve(...)`.

    `get()` returns the REAL value — the code that needs a password needs the password. Every
    SERIALISING path (`as_dict`, `dump`, the CLI) redacts instead, because those outputs land in run
    dirs, CI logs and tickets.
    """

    def __init__(self, resolved: dict[str, Resolved]) -> None:
        self._r = resolved

    def get(self, name: str, default: Any = ...) -> Any:
        if name in self._r:
            return self._r[name].value
        if default is not ...:
            return default
        raise KeyError(f"unknown flag {name!r}; run `harness flags` to list them")

    def explain(self, name: str) -> Resolved:
        return self._r[name]

    def names(self) -> list[str]:
        return sorted(self._r)

    def as_dict(self) -> dict[str, Any]:
        """Redacted — this feeds observability and `--json`. Use `get()` for a real value."""
        return {k: redact(k, v.value) for k, v in sorted(self._r.items())}

    def dump(self, only: set[str] | None = None) -> str:
        """The `flags.json` payload: what was in effect, and which layer won.

        `only` restricts it to a set of names — the run dir records the flags that could have affected
        THIS run (harness + engines + the workflow being run), not every workflow the discovery scan
        happened to import. Secret values are redacted in `value` AND in every `layers` entry: a
        credential that leaked only through `layers.env` would leak just as completely.
        """
        rows = {}
        for k, v in sorted(self._r.items()):
            if only is not None and k not in only:
                continue
            rows[k] = {
                "value": redact(k, v.value),
                "source": v.source,
                "layers": {layer: redact(k, val) for layer, val in v.layers.items()},
            }
            if is_secret(k):
                rows[k]["secret"] = True
        return json.dumps(rows, indent=2, default=str)


def resolve(
    *,
    cli_sets: dict[str, Any] | None = None,
    env: dict[str, str] | None = None,
    config_get: Callable[[str], Any] | None = None,
    workflow_defaults: dict[str, Any] | None = None,
    only: set[str] | None = None,
) -> Flags:
    env = os.environ if env is None else env
    cli_sets = cli_sets or {}
    workflow_defaults = workflow_defaults or {}
    out: dict[str, Resolved] = {}
    for name, flag in _REGISTRY.items():
        if only is not None and name not in only:
            continue
        layers: dict[str, Any] = {"default": flag.default}
        value, source = flag.default, "default"
        if name in workflow_defaults:
            value, source = coerce(flag, workflow_defaults[name]), "workflow"
            layers["workflow"] = value
        if config_get is not None:
            cv = config_get(name)
            if cv is not None:
                value, source = coerce(flag, cv), "config"
                layers["config"] = value
        if flag.env_var in env:
            value, source = coerce(flag, env[flag.env_var]), "env"
            layers["env"] = value
        if name in cli_sets:
            value, source = coerce(flag, cli_sets[name]), "cli"
            layers["cli"] = value
        if flag.choices and value not in flag.choices:
            raise ValueError(f"flag {name}={value!r} not in {flag.choices}")
        out[name] = Resolved(value=value, source=source, layers=layers)
    return Flags(out)


def parse_cli_sets(sets: list[str] | None, enables: list[str] | None, disables: list[str] | None) -> dict[str, Any]:
    """--set a.b=1 --set c=x,y  --enable hooks  --disable rfc.generate.gapfill  ->  {name: raw}"""
    out: dict[str, Any] = {}
    for item in sets or []:
        if "=" not in item:
            raise ValueError(f"--set expects name=value, got {item!r}")
        k, _, v = item.partition("=")
        out[k.strip()] = v
    for k in enables or []:
        out[k.strip()] = True
    for k in disables or []:
        out[k.strip()] = False
    unknown = [k for k in out if k not in _REGISTRY]
    if unknown:
        raise ValueError(f"unknown flag(s) {unknown}; known: {sorted(_REGISTRY)}")
    return out


def describe(scope: Scope | None = None, owner: str | None = None) -> list[dict]:
    """Machine-readable registry (for `harness flags --json` and for agents deciding what to toggle)."""
    rows = []
    for f in sorted(_REGISTRY.values(), key=lambda f: (f.scope, f.name)):
        if scope and f.scope != scope:
            continue
        if owner and f.owner != owner:
            continue
        rows.append(
            {
                "name": f.name,
                "type": f.type.__name__,
                # a secret's DEFAULT is redacted too: a default credential is still a credential
                "default": REDACTED if (f.secret and f.default not in (None, "", False)) else f.default,
                "scope": str(f.scope),
                "owner": f.owner,
                "env": f.env_var,
                "choices": list(f.choices) if f.choices else None,
                "secret": f.secret,
                # False = the value reaches the stage through `legacy_config_key`, but nothing reads the
                # flag by its new name yet, so toggling it by that name changes nothing (see Flag.wired)
                "wired": f.wired,
                "legacy_config_key": f.legacy_config_key,
                "help": f.help,
            }
        )
    return rows


# ---- Core harness flags (every workflow inherits these) ------------------------------------------------
define(Flag("engine", str, "claude-code", "Default engine adapter for every stage (see `harness engines`).",
            Scope.HARNESS, choices=None))
define(Flag("stage_engines", dict, {}, "Per-stage engine override, e.g. {\"review\": \"pi\"} or --set "
            "stage_engines='{\"review\":\"pi\"}'.", Scope.STAGE))
define(Flag("disabled_stages", list, [], "Stage names to skip this run (their requires/outputs contract still "
            "applies to neighbours, so a disabled stage whose outputs are missing fails loudly).", Scope.STAGE))
define(Flag("hooks", bool, True, "Master switch for engine-side hook transport (write-guard, read-only "
            "approver, post-tool feedback). Turning this OFF removes containment — for benchmarks only.",
            Scope.HARNESS))
define(Flag("hooks_off", list, [], "Individual hooks to disable by name (guard_writes, approve_readonly, "
            "byte_budget).", Scope.HARNESS))
define(Flag("hook_canary", bool, True, "Probe once per CLI version that hook feedback reaches the model.",
            Scope.HARNESS, legacy_config_key="AGENT.HOOK_CANARY"))
define(Flag("retries", bool, True, "Honour stages' retry_on_gate_failure budgets (False = fail on first red gate).",
            Scope.HARNESS))
define(Flag("back_jumps", bool, True, "Allow cross-stage retry (retry_stage) on gate failure.", Scope.HARNESS))
define(Flag("revise_on_retry", bool, True, "Honour stages' revise_on_retry (targeted edits instead of rewrite).",
            Scope.HARNESS))
define(Flag("resume_sessions", bool, True, "Revision reruns resume the session that wrote the outputs "
            "(engines without sessions fall back to cold).", Scope.HARNESS))
define(Flag("green_skip", bool, True, "Skip stages whose manifest is green and inputs digest unchanged.",
            Scope.HARNESS))
define(Flag("obs", bool, True, "Emit observability (Langfuse) when keys are configured.", Scope.HARNESS))
define(Flag("obs_required", bool, False, "Refuse to run without a working observability sink.",
            Scope.HARNESS, env="HARNESS_OBS_REQUIRED", legacy_config_key="LANGFUSE.REQUIRED"))
define(Flag("interactive", bool, True, "Allow TTY checkpoints/asks (False = eval mode; --no-interactive).",
            Scope.HARNESS))
define(Flag("live_feed", bool, True, "Tail engine traces into logs/activity.log while running.", Scope.HARNESS))
define(Flag("keep_awake", bool, True, "Wrap long runs in caffeinate on macOS (from test_agent run_chain.sh).",
            Scope.HARNESS))
define(Flag("model", str, "", "Model for every engine invocation ('' = engine/config default).", Scope.ENGINE))
define(Flag("stage_models", dict, {}, "Per-stage model override {stage: model}.", Scope.ENGINE))
define(Flag("timeout_s", int, 2400, "Default per-invocation timeout when a stage sets none.", Scope.ENGINE))
define(Flag("max_budget_usd", float, 0.0, "Hard per-invocation budget (0 = none; it is a KILL, not a graceful stop).",
            Scope.ENGINE))
define(Flag("replay.recording", str, "", "Recording directory for `--engine replay` (build one with "
            "`harness record <run_dir> <recording_dir>`).", Scope.ENGINE, owner="replay"))
