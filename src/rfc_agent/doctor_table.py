"""`/rfc doctor` on Axon: every preflight check as a table row — what was checked, PASS / FAIL / WARN, and for a
failure how to fix it — the same view /rfc-agent:doctor gives on Claude Code (commands/doctor.md).

    python -m rfc_agent.axon_agent doctor --table

PASS and n/a never block; FAIL blocks (BLOCKED); WARN is worth knowing but does not stop a run. Read-only, except
that the Figma check refreshes the plugin's fixed folder, as the Claude Code doctor does.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

PASS, FAIL, WARN, NA = "PASS", "FAIL", "WARN", "n/a"
MIN_NODE = (22, 19)  # Axon 1.1.0 pins Pi 1.6.0, which needs it


@dataclass
class Row:
    check: str
    status: str
    detail: str = ""
    fix: str = ""


def _split(problem: str) -> tuple[str, str]:
    """"what is wrong — how to fix it" -> (what, fix)."""
    what, _, fix = problem.partition(" — ")
    return what.strip(), fix.strip()


def node_version() -> tuple[int, ...] | None:
    exe = shutil.which("node")
    if not exe:
        return None
    try:
        out = subprocess.run([exe, "--version"], capture_output=True, text=True, timeout=20).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    m = re.match(r"v?(\d+)\.(\d+)\.(\d+)", out.strip())
    return tuple(int(x) for x in m.groups()) if m else None


def _axon_rows(cfg, s: dict) -> list[Row]:
    from harness.engines.axon import AxonEngine

    problems = AxonEngine(bin=s["bin"]).doctor()
    if problems and "not found" in problems[0]:
        what, fix = _split(problems[0])
        return [Row("Axon installed", FAIL, what, fix)]

    def pick(*words: str, none: tuple[str, ...] = ()) -> str | None:
        return next((p for p in problems if all(w in p for w in words) and not any(n in p for n in none)), None)

    rows = [Row("Axon installed", PASS, s["bin"])]
    for label, hit, ok in (
        ("Axon signed in", pick("not signed in"), "`axon auth status` is valid"),
        ("Axon version", pick("older than", none=("Pi",)), "new enough"),
        ("Managed Pi runtime", pick("Pi"), "new enough for built-in MCP"),
    ):
        if hit:
            what, fix = _split(hit)
            rows.append(Row(label, FAIL, what, fix or hit))
        else:
            rows.append(Row(label, PASS, ok))
    used = {p for p in (pick("not signed in"), pick("older than", none=("Pi",)), pick("Pi")) if p}
    for extra in (p for p in problems if p not in used and "could not list gateway" not in p):
        what, fix = _split(extra)
        rows.append(Row("Axon", FAIL, what, fix))
    return rows


def _models_row(cfg, s: dict) -> Row:
    from . import axon_agent as ax

    models = ax.list_models(s["bin"])
    if models is None:
        return Row("Gateway models", FAIL, "could not list them (`axon pi --list-models`)",
                   "check `axon auth status`, the VPN/Zscaler notes in the Axon user guide, then `axon doctor`")
    wanted = sorted({ax.gateway_model(x, cfg) for x in ax._STAGE_MODELS})
    missing = [m for m in wanted if m not in models]
    if missing:
        return Row("Gateway models", FAIL, f"not on the gateway: {', '.join(missing)}",
                   "set config AGENT.AXON.MODELS to models the gateway has: " + ", ".join(sorted(models)))
    return Row("Gateway models", PASS, f"{len(wanted)} mapped models are on the gateway")


def _fleetlens_row(cfg) -> Row:
    from .scope.context import health, spec_of

    ok, why = health(spec_of(cfg))
    return Row("Context layer (fleetlens)", PASS if ok else FAIL, "answers" if ok else str(why),
               "" if ok else "check the VPN and MCP.mcpServers.fleetlens in the config (CONFIG.md)")


def _atlassian_rows(cfg, s: dict, needed: bool) -> list[Row]:
    if not needed:
        return [Row("Atlassian MCP v2", NA, "skipped (--no-atlassian: a local PRD file needs none)")]
    from .scope.atlassian_axon import AxonConnectorSession

    name = s["atlassian_server"]
    state, url = AxonConnectorSession(Path("."), axon_bin=s["bin"], server=name).server_info()
    add = f"`axon pi mcp add {name} --url https://mcp.atlassian.com/v2/mcp`, then `axon pi mcp login {name}`"
    if state == "missing":
        return [Row("Atlassian MCP v2", FAIL, f"Axon has no MCP server '{name}'", add)]
    if "/v1/" in url:
        return [Row("Atlassian MCP v2", FAIL, f"'{name}' is on the deprecated v1 endpoint ({url})",
                    f"`axon pi mcp remove {name}`, then " + add)]
    if state != "connected":
        return [Row("Atlassian MCP v2", FAIL, f"'{name}' is {state}", f"`axon pi mcp login {name}`")]
    return [Row("Atlassian MCP v2", PASS, f"'{name}' is connected on {url or 'v2'}")]


def _config_rows(cfg, atlassian: bool) -> list[Row]:
    from . import config as cfgmod

    local = getattr(cfg, "_local", None) or cfgmod._LOCAL
    rows = [Row("Config file", PASS, str(local))]
    try:
        mode = os.stat(local).st_mode & 0o777
        if mode & 0o077:
            rows.append(Row("Config permissions", WARN, f"{local} is mode {mode:o}; it holds secrets",
                            f"`chmod 600 {local}`"))
    except OSError:
        pass
    site = str(cfg.get("ATLASSIAN.SITE") or "")
    if not atlassian:
        rows.append(Row("ATLASSIAN.SITE", NA, "not needed for a local PRD file"))
    elif not site or "your-company" in site:
        rows.append(Row("ATLASSIAN.SITE", FAIL, "not set", "set ATLASSIAN.SITE (e.g. yourcompany.atlassian.net) in "
                        f"{local}"))
    else:
        rows.append(Row("ATLASSIAN.SITE", PASS, site))
    return rows


def _langfuse_row(cfg) -> Row:
    from . import observe

    if not observe.settings(cfg)["enabled"]:
        return Row("Langfuse keys", NA, "tracing is off")
    if all(observe._keys(cfg)):
        return Row("Langfuse keys", PASS, "present (tracing is required)")
    return Row("Langfuse keys", FAIL, "LANGFUSE.PUBLIC_KEY / LANGFUSE.SECRET_KEY are missing; the run would stop",
               "add both to the config file (or export LANGFUSE_PUBLIC_KEY / LANGFUSE_SECRET_KEY)")


def _folder_rows(cfg) -> list[Row]:
    repos = Path(str(cfg.get("REPOS_ROOT") or "~/Documents")).expanduser()
    runs = Path(str(cfg.get("RUNS_ROOT") or "~/rfc-runs")).expanduser()
    rows: list[Row] = []
    if not repos.is_dir():
        rows.append(Row("Repos folder (REPOS_ROOT)", FAIL, f"{repos} does not exist",
                        "set REPOS_ROOT to the folder holding your repo checkouts"))
    else:
        n = sum(1 for p in repos.iterdir() if (p / ".git").exists())
        rows.append(Row("Repos folder (REPOS_ROOT)", PASS if n else WARN, f"{repos}: {n} checkout(s)",
                        "" if n else "clone the repos the PRD touches here, or point REPOS_ROOT at the right folder"))
    try:
        inside = runs.resolve().is_relative_to(repos.resolve())
    except OSError:
        inside = False
    rows.append(Row("Runs folder (RUNS_ROOT)", WARN if inside else PASS,
                    f"{runs} is inside REPOS_ROOT: tracers could cite earlier runs as source" if inside else str(runs),
                    "move RUNS_ROOT outside REPOS_ROOT (default ~/rfc-runs)" if inside else ""))
    return rows


def _figma_rows() -> list[Row]:
    app = Path("/Applications/Figma.app")
    rows = [Row("Figma desktop app", PASS if app.exists() else FAIL, str(app) if app.exists() else "not installed",
                "" if app.exists() else "install the Figma desktop app (needed to export screens)")]
    try:
        from .scope import figma

        not_imported, manifest, _ = figma.ensure_plugin()
    except Exception as e:  # noqa: BLE001 — a doctor reports, never crashes
        return rows + [Row("Figma snapshot plugin", WARN, f"could not check: {e}", "run a Scope once, then re-check")]
    rows.append(Row("Figma snapshot plugin", FAIL if not_imported else PASS,
                    "not imported in Figma" if not_imported else "imported",
                    f"in Figma: Plugins → Development → Import plugin from manifest… → {manifest}"
                    if not_imported else ""))
    return rows


def _mermaid_row() -> Row:
    node, npm = shutil.which("node"), shutil.which("npm")
    if node and npm:
        return Row("Mermaid check tools (Node + npm)", PASS, "the parser installs itself on first Draft")
    return Row("Mermaid check tools (Node + npm)", WARN, "Node.js / npm not found: the diagram check is skipped",
               "install Node.js (it brings npm); the RFC is still written without the check")


def _node_row() -> Row:
    v = node_version()
    want = ".".join(str(x) for x in MIN_NODE)
    if v is None:
        return Row("Node.js for Axon's Pi", WARN, "node not found on PATH", f"install Node {want} or newer")
    if v[:2] < MIN_NODE:
        return Row("Node.js for Axon's Pi", FAIL, f"node {'.'.join(map(str, v))} is older than {want}; Pi 1.6 needs it",
                   f"`nvm install 22 && nvm alias default 22` (or any Node >= {want}), then open a new terminal")
    return Row("Node.js for Axon's Pi", PASS, f"node {'.'.join(map(str, v))}")


def checks(cfg, *, atlassian: bool = True) -> list[Row]:
    from . import axon_agent as ax

    s = ax.settings(cfg)
    rows = _axon_rows(cfg, s)
    if rows[0].status == FAIL:  # no Axon: the model, gateway and server checks cannot run
        return rows
    rows += [_node_row(), _models_row(cfg, s), _fleetlens_row(cfg)]
    rows += _atlassian_rows(cfg, s, atlassian)
    rows += _config_rows(cfg, atlassian)
    rows += [_langfuse_row(cfg)]
    rows += _folder_rows(cfg)
    rows += _figma_rows()
    rows += [_mermaid_row()]
    return rows


ICON = {PASS: "✅ PASS", FAIL: "❌ FAIL", WARN: "⚠️ WARN", NA: "➖ n/a"}


def _cell(text: str) -> str:
    return " ".join(str(text).split()).replace("|", "\\|")


def render(rows: list[Row]) -> str:
    out = ["| Check | Status | Detail | How to fix |", "|---|---|---|---|"]
    for r in rows:
        out.append(f"| {_cell(r.check)} | {ICON[r.status]} | {_cell(r.detail)} | {_cell(r.fix) or '—'} |")
    fails = [r for r in rows if r.status == FAIL]
    warns = [r for r in rows if r.status == WARN]
    out.append("")
    if fails:
        out.append(f"axon: BLOCKED ({len(fails)} problem(s)) — first: {fails[0].check}: {fails[0].detail}")
    else:
        out.append("axon: READY" + (f" ({len(warns)} warning(s))" if warns else ""))
    return "\n".join(out)


def blocked(rows: list[Row]) -> bool:
    return any(r.status == FAIL for r in rows)
