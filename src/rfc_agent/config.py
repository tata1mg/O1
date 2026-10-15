"""Config for rfc_agent: the harness's own values, then everything you supply, then env vars.

Three layers, merged (later wins):
  1. <plugin>/config.harness.json           ships with the plugin — the harness's working values, read-only
  2. ~/.config/rfc-agent/config.json        everything you supply: team values (Atlassian site, fleetlens URL,
                                            …), your folders, keys and tokens
  3. environment variables (_ENV_MAP)
Dicts merge key by key, so you only write what differs. Nothing org-specific ships in the plugin, and nothing
you edit lives inside it (an update replaces it). Example: <plugin>/config.example.json.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]  # src/rfc_agent/config.py -> plugin root
HARNESS = _REPO_ROOT / "config.harness.json"
_CONFIG_DIR = Path("~/.config/rfc-agent").expanduser()

# Top-level sections the code reads. Anything else in config.json is reported once — a typo
# (REPO_ROOT) or a key left over from an older version, which would otherwise be silently ignored.
KNOWN_SECTIONS = frozenset({
    "AGENT", "REPOS_ROOT", "RUNS_ROOT", "REPOS_EXCLUDE", "MCP", "ATLASSIAN", "LANGFUSE", "OBS", "SERVICES_MAP",
    "STEERING", "SCOPE", "INVESTIGATE", "DRAFT", "REVIEW", "REVISE", "SUMMARIZE",
    "STAGE1", "STAGE2", "STAGE3", "STAGE4", "STAGE5",  # old names, still read (legacy_names.OLD_CONFIG)
})


def _find_local() -> Path:
    """Your config: RFC_AGENT_CONFIG env > ~/.config/rfc-agent/config.json."""
    env = os.environ.get("RFC_AGENT_CONFIG")
    return Path(env).expanduser() if env else _CONFIG_DIR / "config.json"


_LOCAL = _find_local()

# dotted config path -> env var that overrides it. CLAUDE_CONFIG_DIR maps to the exact env var Claude Code's
# CLI reads; CLAUDE_CODE_OAUTH_TOKEN_FILE is a harness-only knob holding a PATH to the secret, never the secret.
_ENV_MAP = {
    "AGENT.CLAUDE_BIN": "CLAUDE_BIN",
    "AGENT.CLAUDE_CONFIG_DIR": "CLAUDE_CONFIG_DIR",
    "AGENT.CLAUDE_CODE_OAUTH_TOKEN_FILE": "RFC_AGENT_CLAUDE_OAUTH_TOKEN_FILE",
    "AGENT.DEFAULT_MODEL": "RFC_AGENT_MODEL",
    "REPOS_ROOT": "RFC_AGENT_REPOS_ROOT",
    "ATLASSIAN.SITE": "ATLASSIAN_SITE",
    "LANGFUSE.PUBLIC_KEY": "LANGFUSE_PUBLIC_KEY",
    "LANGFUSE.SECRET_KEY": "LANGFUSE_SECRET_KEY",
    "LANGFUSE.HOST": "LANGFUSE_HOST",
    "LANGFUSE.ENABLED": "RFC_AGENT_LANGFUSE",
    "LANGFUSE.VERBOSE": "RFC_AGENT_LANGFUSE_VERBOSE",
    "GENERATE.EMIT": "RFC_AGENT_GENERATE_EMIT",
}


def _dig(d: dict, dotted: str):
    cur = d
    for part in dotted.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return None
        cur = cur[part]
    return cur


def _set(d: dict, dotted: str, value) -> None:
    parts = dotted.split(".")
    cur = d
    for part in parts[:-1]:
        cur = cur.setdefault(part, {})
    cur[parts[-1]] = value


def _coerce_like(config_value, raw: str):
    """Coerce an env-string to the config value's type so dotted gets stay typed."""
    if isinstance(config_value, bool):
        return raw.strip().lower() in ("1", "true", "yes", "on")
    if isinstance(config_value, int) and not isinstance(config_value, bool):
        try:
            return int(raw)
        except ValueError:
            return raw
    if isinstance(config_value, float):
        try:
            return float(raw)
        except ValueError:
            return raw
    return raw


def _merge(dst: dict, src: dict) -> dict:
    """Deep-merge src into dst: dicts key by key, anything else replaced."""
    for k, v in src.items():
        if isinstance(v, dict) and isinstance(dst.get(k), dict):
            _merge(dst[k], v)
        else:
            dst[k] = json.loads(json.dumps(v))
    return dst


def _read_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"{path} is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise RuntimeError(f"{path} must hold a JSON object")
    return data


_WARNED: set[str] = set()


def unknown_sections(data: dict) -> list[str]:
    return sorted(k for k in data if not k.startswith("_") and k not in KNOWN_SECTIONS)


def _load(local: Path, harness: Path = HARNESS) -> dict:
    if not local.exists():
        raise RuntimeError(
            f"{local} not found. Run /rfc-agent:setup in Claude Code, or copy "
            f"{_REPO_ROOT / 'config.example.json'} there and fill it in."
        )
    cfg = _read_json(harness)
    data = _read_json(local)
    stray = unknown_sections(data)
    if stray and str(local) not in _WARNED:
        _WARNED.add(str(local))
        print(f"  [config] {local}: unknown key(s) {stray} are ignored (typo, or left from an older version)",
              file=sys.stderr)
    _merge(cfg, data)
    for dotted, env_var in _ENV_MAP.items():
        if env_var in os.environ:  # presence, not truthiness — "" is a meaningful override
            _set(cfg, dotted, _coerce_like(_dig(cfg, dotted), os.environ[env_var]))
    # Propagate config-only CLAUDE_CONFIG_DIR into env so spawned `claude` subprocesses inherit the login.
    claude_config_dir = _dig(cfg, "AGENT.CLAUDE_CONFIG_DIR")
    if claude_config_dir and "CLAUDE_CONFIG_DIR" not in os.environ:
        os.environ["CLAUDE_CONFIG_DIR"] = str(Path(claude_config_dir).expanduser())
    # AUTH: token FILE (secret never sits in config.json). Ambient CLAUDE_CODE_OAUTH_TOKEN wins.
    token_file = _dig(cfg, "AGENT.CLAUDE_CODE_OAUTH_TOKEN_FILE")
    if token_file and "CLAUDE_CODE_OAUTH_TOKEN" not in os.environ:
        path = Path(token_file).expanduser()
        try:
            token = path.read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise RuntimeError(f"AGENT.CLAUDE_CODE_OAUTH_TOKEN_FILE={path} could not be read: {exc}") from exc
        if token:
            os.environ["CLAUDE_CODE_OAUTH_TOKEN"] = token
    return cfg


class Config:
    def __init__(self, local: Path | None = None, harness: Path | None = None) -> None:
        self._local = local or _LOCAL
        self.config = _load(self._local, harness or HARNESS)

    def get(self, dotted: str, default=None):
        val = _dig(self.config, dotted)
        return default if val is None else val

    def materialize_mcp(self, names: tuple[str, ...], dest: Path) -> Path:
        """Write the SUBSET of configured MCP servers named in `names` to `dest` in the claude
        {"mcpServers": {...}} schema and return dest. Always writes — an empty subset writes
        {"mcpServers": {}}, which with --strict-mcp-config guarantees a no-MCP stage.
        Unknown names fail loudly. `~` in stdio arg paths is expanded."""
        servers = (self.config.get("MCP") or {}).get("mcpServers") or {}
        servers = {k: v for k, v in servers.items() if not k.startswith("_")}
        unknown = [n for n in names if n not in servers]
        if unknown:
            raise RuntimeError(f"stage references MCP server(s) not in MCP.mcpServers "
                               f"(config.json): {unknown}")
        resolved = {}
        for name in names:
            spec = dict(servers[name])
            if name == "figma":
                # binding-race hardening (2026-08-14): the CLI freezes the deferred-tools list when the
                # first prompt is built (~1s) while servers connect in the background — a sleepy desktop
                # Dev Mode server (App Nap after idle) then binds ZERO figma tools for the whole run.
                # alwaysLoad pins its tools into the first prompt; 120s outlives a slow protocol probe.
                spec.setdefault("alwaysLoad", True)
                spec.setdefault("timeout", 120000)
            if isinstance(spec.get("args"), list):
                spec["args"] = [
                    str(Path(a).expanduser()) if isinstance(a, str) and ("/" in a or a.startswith("~")) else a
                    for a in spec["args"]
                ]
            resolved[name] = spec
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(json.dumps({"mcpServers": resolved}, indent=2))
        return dest


_CONFIG: Config | None = None


def get_config() -> Config:
    """Lazy singleton — importing this module never requires config.json (tests inject their own paths)."""
    global _CONFIG
    if _CONFIG is None:
        _CONFIG = Config()
    return _CONFIG
