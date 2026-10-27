"""Axon engine adapter — Pi 1.4 run through the org's managed launcher, `axon pi --mode json -p`.

Axon (DeputyDev) is how the org runs Pi: it owns sign-in, the LiteLLM gateway (provider `litellm`), the
managed extensions (OTEL, terminal policy, compaction, session backup) and Pi's home (`~/.axon/pi`). The
event stream is plain Pi, so this is the `pi` adapter with five deltas, each measured against Axon 0.2.6 /
Pi 1.4.0 on 2026-10-27 (decisions.md):

1. argv starts `axon pi`, everything after `pi` is passed to Pi untouched.
2. NO `--no-extensions`: it unloads `pi-provider-litellm` and the run dies with `Unknown provider "litellm"`.
   Axon's managed extensions stay loaded; ours are added with `-e` (the guard, plus rfc-tools.js).
3. NO private home: Axon ignores PI_CODING_AGENT_DIR (auth lives in its encrypted session, not auth.json).
4. NO PI_OFFLINE: model discovery through the gateway is a startup network operation.
5. MCP exists (Pi 1.4 has it built in). Stage servers are registered for the one session by rfc-tools.js
   (`pi.registerMcpServer`), never written to the operator's ~/.axon/pi/mcp.json, and the granted tool
   names are allowed through the guard.

Model names: stages are authored with Claude ids. `map_model` turns them into gateway ids by tier (opus /
sonnet / haiku, and a 1M-context variant), so a stage needs no edit to run here; a non-Claude id passes
through unchanged.
"""

from __future__ import annotations

import dataclasses
import json
import os
import re
import shutil
import subprocess
from pathlib import Path

from .. import flags as F
from . import Containment, EngineRequest, EngineResult
from .pi import _EXT_DIR, PiEngine, _older, _version_of

RFC_TOOLS_JS = _EXT_DIR / "rfc-tools.js"

# Claude tier -> gateway model. Chosen 2026-10-27 from the gateway's list and measured prices (decisions.md
# D-07): the 1M-context tiers need a >= ~900K window because the Investigate lead is resumed across steps.
DEFAULT_MODELS: dict[str, str] = {
    "opus[1m]": "Kimi-k3.1-pro",
    "opus": "openai/gpt-6.1-sol",
    "sonnet[1m]": "openai/gpt-6.1-terra",
    "sonnet": "openai/gpt-6.1-terra",
    "haiku": "gemini-3.8-flash",
}

# Claude's effort knob -> Pi's thinking level (Pi clamps to what the model supports).
EFFORT_TO_THINKING: dict[str, str] = {
    "low": "low", "medium": "medium", "high": "high", "xhigh": "xhigh", "max": "max",
    "minimal": "minimal", "off": "off",
}


def _f(name: str, type_: type, default, help_: str) -> None:
    F.define(F.Flag(f"axon.{name}", type_, default, help_, F.Scope.ENGINE, owner="axon"))


_f("bin", str, "axon", "Path to the `axon` launcher (installs and runs the org's managed Pi). Install: "
   "curl --proto '=https' --tlsv1.2 -fsSL https://deputydev.ai/axon/install.sh | sh")
_f("min_version", str, "0.2.6", "Minimum Axon launcher version doctor() accepts (the measured one).")
_f("min_pi", str, "1.4.0", "Minimum managed Pi version: built-in MCP and registerMcpServer arrived in 1.x.")
_f("provider", str, "litellm", "--provider for every invocation: Axon's gateway provider.")
_f("models", dict, {}, "Model map merged over the defaults: {\"opus\": \"<gateway id>\", \"opus[1m]\": ..., "
   "\"sonnet\": ..., \"haiku\": ...}, or an exact Claude id as the key. Unmapped non-Claude ids pass through.")
_f("threshold_compaction", bool, False, "Allow Axon's early (threshold) compaction. Default OFF: compaction is "
   "lossy and summarised-away evidence makes the citation gates lie. Overflow recovery is never blocked.")


def model_tier(model: str) -> str | None:
    """`claude-opus-5-5[1m]` -> `opus[1m]`; `claude-haiku-4-5-20251001` -> `haiku`; non-Claude -> None."""
    m = (model or "").lower()
    if "claude" not in m and not any(t == m.split("[")[0] for t in ("opus", "sonnet", "haiku")):
        return None
    for tier in ("opus", "sonnet", "haiku"):
        if tier in m:
            return f"{tier}[1m]" if "[1m]" in m else tier
    return None


def map_model(model: str | None, overrides: dict | None = None) -> str | None:
    """A stage's model name -> the gateway id to pass as --model. Exact-id overrides win, then the tier map
    (a `[1m]` tier falls back to its plain tier), and a non-Claude id passes through unchanged."""
    if not model:
        return None
    table = {**DEFAULT_MODELS, **(overrides or {})}
    if model in table and model not in DEFAULT_MODELS:
        return str(table[model])
    tier = model_tier(model)
    if tier is None:
        return model
    return str(table.get(tier) or table.get(tier.replace("[1m]", "")) or model)


def thinking_for(effort: str | None) -> str | None:
    return EFFORT_TO_THINKING.get(str(effort or "").lower()) if effort else None


def write_tools_config(path: Path, *, mcp: dict | None = None, hook_cmd: list[str] | None = None,
                       threshold_compaction: bool = False) -> Path:
    """The one file rfc-tools.js reads (RFC_TOOLS_CONFIG). 0600: an MCP spec may carry a bearer token."""
    servers = {name: {k: v for k, v in (spec or {}).items() if k != "type"} for name, spec in (mcp or {}).items()}
    body = {"mcp": servers, "threshold_compaction": bool(threshold_compaction)}
    if hook_cmd:
        body["hook"] = {"cmd": list(hook_cmd)}
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(body, f, indent=2)
    path.chmod(0o600)
    return path


class AxonEngine(PiEngine):
    """The `axon` adapter. Construct via `harness.engines.get_engine("axon", flags=...)`."""

    name = "axon"
    supports_mcp = True

    def __init__(self, bin: str | None = None, flags: object | None = None, **_ignored) -> None:
        super().__init__(flags=flags)
        self.bin = bin or str(self._flag("axon.bin"))

    # ---- protocol ----------------------------------------------------------------------------------

    def doctor(self) -> list[str]:
        problems: list[str] = []
        exe = shutil.which(self.bin) or (self.bin if os.path.isabs(self.bin) and os.access(self.bin, os.X_OK) else None)
        if not exe:
            return [f"`{self.bin}` not found on PATH — install Axon: curl --proto '=https' --tlsv1.2 -fsSL "
                    f"https://deputydev.ai/axon/install.sh | sh  (then open a new terminal)"]
        got = _version_of([exe, "--version"])
        want = str(self._flag("axon.min_version"))
        if got is None:
            problems.append(f"`{self.bin} --version` printed no parseable version — cannot verify >= {want}")
        elif _older(got, want):
            problems.append(f"Axon {got} is older than {want} — run `axon update`")
        try:
            auth = subprocess.run([exe, "auth", "status"], capture_output=True, text=True, timeout=60)
            if auth.returncode != 0:
                problems.append("Axon is not signed in — run `axon` once in a terminal and choose "
                                "'Login from browser' (`axon auth status` must say the session is valid)")
        except (OSError, subprocess.SubprocessError) as exc:
            problems.append(f"`{self.bin} auth status` did not run: {exc}")
        pi = _managed_pi_version(exe)
        want_pi = str(self._flag("axon.min_pi"))
        if pi is None:
            problems.append("could not read the managed Pi version from `axon doctor pi` — run "
                            "`axon setup pi --sync-packages`, then `axon doctor pi`")
        elif _older(pi, want_pi):
            problems.append(f"Axon's managed Pi {pi} is older than {want_pi} (needed for built-in MCP) — "
                            f"run `axon update`")
        return problems

    def run(self, req: EngineRequest) -> EngineResult:
        containment = req.containment or Containment()
        cwd = Path(req.cwd)
        cfg_path = write_tools_config(
            cwd / "logs" / f"{req.label}.rfc-tools.json",
            mcp=dict(containment.mcp_servers or {}),
            threshold_compaction=bool(self._flag("axon.threshold_compaction")),
        )
        env = {**(req.env or {}), "RFC_TOOLS_CONFIG": str(cfg_path)}
        model = map_model(req.model, dict(self._flag("axon.models") or {}))
        effort = thinking_for(req.effort) if req.effort else None
        return super().run(dataclasses.replace(req, env=env, model=model, effort=effort))

    # ---- PiEngine seams ----------------------------------------------------------------------------

    def _cmd_prefix(self) -> list[str]:
        return [self.bin, "pi"]

    def _provider(self) -> str:
        return str(self._flag("axon.provider") or "")

    def _extension_args(self, req: EngineRequest, containment: Containment) -> list[str]:
        # NOT --no-extensions: that unloads Axon's gateway provider (measured). Axon's managed extensions stay;
        # ours are added. A CLI -e extension loads before the trust decision, so the guard is active even in an
        # untrusted repo, exactly as on plain pi.
        args: list[str] = []
        if self._guard_on(containment):
            args += ["-e", str(self._guard_path())]
        return args + ["-e", str(RFC_TOOLS_JS)]

    def _private_home(self) -> bool:
        return False  # Axon pins PI_CODING_AGENT_DIR to ~/.axon/pi and ignores an override (measured)

    def _env(self, req, **kw) -> dict[str, str]:
        env = super()._env(req, **kw)
        # gateway model discovery and Axon's session check are startup network operations
        env.pop("PI_OFFLINE", None)
        env.pop("PI_SKIP_VERSION_CHECK", None)
        env.pop("PI_CODING_AGENT_DIR", None)
        return env

    def _preflight_notes(self, tr, req, containment, writable, session_id, resume_file) -> None:
        super()._preflight_notes(tr, req, containment, writable, session_id, resume_file)
        tr.note("axon: running under Axon's managed Pi — its extensions (gateway, OTEL, terminal policy, "
                "compaction, session backup) are loaded alongside the harness guard")


_PI_VERSION_RE = re.compile(r"pi: version (\d+\.\d+\.\d+)")


def _managed_pi_version(exe: str) -> str | None:
    try:
        out = subprocess.run([exe, "doctor", "pi"], capture_output=True, text=True, timeout=120)
    except (OSError, subprocess.SubprocessError):
        return None
    m = _PI_VERSION_RE.search((out.stdout or "") + (out.stderr or ""))
    return m.group(1) if m else None
