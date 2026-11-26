"""`preflight` — everything `/test-agent:test` checks before a run starts, as one JSON verdict.

WHY a module and not a list of shell probes in the command prompt: the expensive failures of this workflow
land late. Kiwi is first touched by `store_cases`, 35 minutes in (2026-11-02, NR-DIGICHEM: the first run
generated, evaluated and then could not store). A missing contract silently makes every backend case
`manual`. Each of these is a seconds-long check, and the command needs one machine-readable answer it can
turn into a checklist and a question, not six ad-hoc greps.

    python -m workflows.testing.preflight check --feature F --rfc-run-dir D [--json] [--no-network]
    python -m workflows.testing.preflight recent [--limit N] [--json]
    python -m workflows.testing.preflight runs [--rfc-run-dir D] [--feature F] [--all] [--json]
    python -m workflows.testing.preflight features --rfc-run-dir D [--json]
    python -m workflows.testing.preflight setup-kiwi --kiwi-web URL --product-prefix P [--kiwi-user U]
                                                    [--kiwi-url X] [--insecure-tls true|false]
    python -m workflows.testing.preflight setup-web [--environments a,b,c] [--base-url-template T]
                                                   [--ui-login FILE] [--curl FILE] [--env E] [--build B]
                                                   [--production-base-url URL] [--sessions-dir DIR]
    python -m workflows.testing.preflight envs [--json]
    python -m workflows.testing.preflight setup-android (--check | --path DIR | --clone [--into DIR] [--url URL])
                                                       [--json]

`check` exits 0 when nothing failed (warnings allowed), 1 when a check failed. Each check is
{id, level: ok|info|warn|fail, msg, fix}, and a `fail` also has `kind`: `setup` (not done yet — shown ⏳) or
`error` (broken now — shown ❌). `testing` adds a `summary` {ready, to_set_up, errors, notes, next} and ends with a
footer built from it, so a re-run mid-setup says what is left rather than only NOT READY. The Kiwi check is
`fail` when Kiwi is unreachable: the command, not this module, decides whether the user may go on without it
(generate now, store later).

`setup-kiwi` writes the NON-secret Kiwi settings to `TESTING` in ~/.config/rfc-agent/config.json, the
config layer `workflows.testing.config_get` reads, so every later invocation — a resume, the execute
phase — uses the same store. It never writes a password: that stays in ~/.tcms.conf (chmod 600).
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import re
import shutil
import socket
import stat
import sys
from datetime import date
from pathlib import Path

if __package__ in (None, ""):  # run as a script: make `workflows.testing` importable from src/
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

#: Seconds before a Kiwi probe gives up. XML-RPC over a dropped VPN otherwise hangs for minutes.
KIWI_TIMEOUT_S = 15
#: The non-secret `testing.*` flags `setup-kiwi` / `setup-web` may write. `ui_login` is a PATH to the
#: credentials file, never its contents.
KIWI_SETTINGS = ("storage", "kiwi_web", "product_prefix", "kiwi_user", "kiwi_url", "insecure_tls")
ANDROID_SETTINGS = ("maestro_repo",)
WEB_SETTINGS = ("environments", "base_url_template", "ui_login", "curl", "env", "build",
                "production_base_url", "sessions_dir")


# ---- shared with `status`: flags and the store, resolved the way a run resolves them ---------------


def load_config():
    """The rfc-agent Config, or None when config.json is missing (the check reports it)."""
    from rfc_agent.config import Config

    try:
        return Config()
    except RuntimeError:
        return None


def resolved_flags(cfg=None):
    """Every flag at the value a `harness run testing` with no `--set` would see: env > config > defaults."""
    from harness import flags as flagmod

    from . import WORKFLOW, config_get

    return flagmod.resolve(config_get=config_get(cfg) if cfg is not None else None,
                           workflow_defaults=WORKFLOW.flag_defaults)


class _FlagCtx:
    """Just enough of RunContext for `store.get_store`."""

    def __init__(self, flags, run_dir: Path) -> None:
        self._flags, self.run_dir = flags, run_dir

    def flag(self, name: str, default=None):
        return self._flags.get(name, default)


def store_for(flags, run_dir: Path):
    from .store import get_store

    return get_store(_FlagCtx(flags, run_dir))


# ---- checks ------------------------------------------------------------------------------------------


def _check(id_: str, level: str, msg: str, fix: str = "", kind: str = "") -> dict:
    """One row. A `fail` row also says which kind (issue #21): `setup` — not done yet on this machine, the
    normal state while installing — or `error` — set up, but broken right now. Untagged fails count as errors."""
    row = {"id": id_, "level": level, "msg": msg, "fix": fix}
    if level == "fail":
        row["kind"] = kind if kind in ("setup", "error") else "error"
    return row


def _source(key: str, explicit: str | None, rfc_run: Path | None) -> tuple[str, str]:
    """(status, where) for one source, resolved like the `sources` stage: explicit path, the RFC run's
    file, then (contract/plan) the RFC's own section."""
    from .stages.sources import RFC_RUN_ALTERNATES, RFC_SECTIONS, SOURCE_SPECS, rfc_section

    if explicit:
        p = Path(explicit).expanduser()
        return ("present", str(p)) if p.is_file() and p.stat().st_size else ("missing", str(p))
    rel = next((r for _, k, r, _ in SOURCE_SPECS if k == key), None)
    if rfc_run and rel:
        for candidate in (rel, *RFC_RUN_ALTERNATES.get(key, ())):
            p = rfc_run / candidate
            if p.is_file() and p.stat().st_size:
                return "present", str(p)
    if rfc_run and key in RFC_SECTIONS and (rfc_run / "out/rfc.md").is_file():
        text = (rfc_run / "out/rfc.md").read_text(encoding="utf-8", errors="replace")
        if rfc_section(text, RFC_SECTIONS[key]).strip():
            return "present", f"out/rfc.md § {RFC_SECTIONS[key]}"
    return "missing", ""


def check_sources(feature: str, rfc_run_dir: str | None, overrides: dict) -> list[dict]:
    out = []
    if not feature:
        out.append(_check("feature", "fail", "no feature key", "ask for it, e.g. NR-DIGICHEM"))
    rfc_run = Path(rfc_run_dir).expanduser() if rfc_run_dir else None
    if rfc_run and not rfc_run.is_dir():
        return out + [_check("rfc_run_dir", "fail", f"{rfc_run} does not exist",
                             "pick one from `preflight recent`")]
    for key, label, missing_level, missing_msg in (
        ("rfc", "RFC", "fail", "the generator has nothing to work from"),
        ("prd", "PRD", "fail", "the generator needs the PRD next to the RFC"),
        ("contract", "API contract", "warn",
         "no exact routes, so every backend case is tagged `manual` and nothing is executable"),
        ("plan", "Implementation plan", "warn",
         "prerequisites cannot list services, deploy order or migrations"),
        ("figma", "Figma", "info", "not supplied; UI cases come from the PRD and RFC only"),
        ("test_data", "Test data", "warn",
         "not supplied; the suite uses invented values, and the prerequisites stage writes "
         "qa/test_data.template.yaml for the team to fill"),
    ):
        status, where = _source(key, overrides.get(key), rfc_run)
        if status == "present":
            out.append(_check(key, "ok", f"{label}: {where}"))
        else:
            fix = f"pass --{key.replace('_', '-')} <file>" if key != "figma" else ""
            out.append(_check(key, missing_level, f"{label}: {missing_msg}", fix))
    return out


def check_kiwi(flags, *, network: bool = True) -> list[dict]:
    storage = flags.get("testing.storage")
    if storage != "kiwi":
        return [_check("kiwi", "fail", f"testing.storage is {storage!r}: cases would stay in a local JSON "
                       "store, with no Kiwi review and nothing to confirm",
                       "run `preflight setup-kiwi` once (writes TESTING in config.json)", kind="setup")]
    out = []
    web = flags.get("testing.kiwi_web")
    explicit = all(flags.get(f"testing.{k}") for k in ("kiwi_url", "kiwi_user", "kiwi_password"))
    if not explicit:
        conf = Path("~/.tcms.conf").expanduser()
        if not conf.is_file():
            return [_check("kiwi_conf", "fail", "~/.tcms.conf is missing: there is no Kiwi login",
                           "create it yourself with [tcms] url/username/password, then chmod 600 ~/.tcms.conf",
                           kind="setup")]
        if conf.stat().st_mode & (stat.S_IRWXG | stat.S_IRWXO):
            out.append(_check("kiwi_conf", "warn", "~/.tcms.conf is readable by other users",
                              "chmod 600 ~/.tcms.conf"))
        else:
            out.append(_check("kiwi_conf", "ok", "~/.tcms.conf present, mode 600"))
    try:
        import tcms_api  # noqa: F401
    except ImportError:
        return out + [_check("kiwi_client", "fail", "tcms-api is not installed",
                             "uv sync --extra testing (in the plugin folder)", kind="setup")]
    if not network:
        return out + [_check("kiwi", "warn", f"Kiwi at {web} not probed (--no-network)")]
    previous = socket.getdefaulttimeout()
    socket.setdefaulttimeout(KIWI_TIMEOUT_S)
    try:
        health = store_for(flags, Path.cwd()).health()
    finally:
        socket.setdefaulttimeout(previous)
    if health.get("ok"):
        user = flags.get("testing.kiwi_user") or "the ~/.tcms.conf user"
        return out + [_check("kiwi", "ok", f"Kiwi reachable at {web} as {user}")]
    error = str(health.get("error", "?"))
    fix = ("the Kiwi server itself is down or restarting — retry in a few minutes" if
           re.search(r"\b50[234]\b|Service Unavailable|Bad Gateway", error) else
           "connect the VPN and retry")
    return out + [_check("kiwi", "fail", f"Kiwi at {web} is unreachable: {error}",
                         f"{fix}; or generate now and store in Kiwi later", kind="error")]


def check_langfuse() -> list[dict]:
    from rfc_agent import observe

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        code = observe.preflight()
    text = buf.getvalue().strip()
    if code == 0:
        return [_check("langfuse", "ok", text.splitlines()[0] if text else "langfuse ok")]
    return [_check("langfuse", "fail", text, "add LANGFUSE keys to config.json, or pass --langfuse false",
                   kind="setup" if code == observe.EXIT_NO_KEYS else "error")]


def check_claude(cfg) -> list[dict]:
    binary = (cfg.get("AGENT.CLAUDE_BIN") if cfg else None) or "claude"
    if shutil.which(str(Path(binary).expanduser())) or Path(binary).expanduser().is_file():
        return [_check("claude", "ok", f"claude CLI: {binary}")]
    return [_check("claude", "fail", f"claude CLI not found ({binary})", "set AGENT.CLAUDE_BIN in config.json",
                   kind="setup")]


def check_executors() -> list[dict]:
    """The UI runners. Never `fail`: they matter only for phase B, and only for the kinds chosen there."""
    from . import ui_web

    out = []
    problem = ui_web.runner_problem() or ui_web.browser_problem()
    out.append(_check("playwright", "warn", f"web UI cases cannot run: {problem}",
                      f"cd {ui_web.RUNNER_DIR} && npm ci && npx playwright install chromium") if problem else
               _check("playwright", "ok", f"Playwright ready ({ui_web.RUNNER_DIR})"))
    missing = [b for b in ("maestro", "java", "adb") if not shutil.which(b)]
    out.append(_check("maestro", "info", f"app UI cases run on your device: {', '.join(missing)} not found "
                      "here — results can still be published from a report", "") if missing else
               _check("maestro", "ok", "Maestro, Java and adb found"))
    return out


def check_ui_login(path: str) -> list[dict]:
    """The test-account file web UI runs log in with. Its values are read to check their shape, never shown."""
    if not path:
        return [_check("ui_login", "warn", "no test-account login set: web UI runs will ask for a recorded "
                       "session instead", "preflight setup-web --ui-login <file with {\"phone\", \"otp\"}>")]
    p = Path(path).expanduser()
    if not p.is_file():
        return [_check("ui_login", "fail", f"login file {p} does not exist",
                       f'create it yourself: {{"phone": "…", "otp": "…"}}, then chmod 600 {p}')]
    try:
        data = json.loads(p.read_text())
    except (OSError, ValueError):
        return [_check("ui_login", "fail", f"login file {p} is not JSON", '{"phone": "…", "otp": "…"}')]
    missing = [k for k in ("phone", "otp") if not (isinstance(data, dict) and str(data.get(k) or "").strip())]
    if missing:
        return [_check("ui_login", "fail", f"login file {p} has no {' / '.join(missing)}",
                       '{"phone": "…", "otp": "…"}')]
    if p.stat().st_mode & (stat.S_IRWXG | stat.S_IRWXO):
        return [_check("ui_login", "warn", f"login file {p} is readable by other users", f"chmod 600 {p}")]
    return [_check("ui_login", "ok", f"test-account login file {p} (phone and otp set, mode 600)")]


def environments(flags) -> list[dict]:
    """[{env, base_url}] from `testing.environments` and `testing.base_url_template`."""
    template = str(flags.get("testing.base_url_template") or "")
    names = [e.strip() for e in str(flags.get("testing.environments") or "").split(",") if e.strip()]
    return [{"env": e, "base_url": template.format(env=e) if template else ""} for e in names]


def suggest_run_dir(cfg, feature: str) -> str:
    root = Path((cfg.get("RUNS_ROOT") if cfg else None) or "~/rfc-runs").expanduser()
    base = root / f"{date.today().isoformat()}-testing-{feature or 'run'}"
    candidate, n = base, 1
    while candidate.exists():
        n += 1
        candidate = base.with_name(f"{base.name}-v{n}")
    return str(candidate)


def check(feature: str, rfc_run_dir: str | None, overrides: dict | None = None, *,
          network: bool = True, engine: str = "") -> dict:
    # One spelling per feature: a key typed as "hdpo" and one picked as "HDPO" would otherwise name two Kiwi
    # products and two run folders for the same thing.
    feature = feature_key(feature) if feature else feature
    cfg = load_config()
    checks = []
    if cfg is None:
        checks.append(_check("config", "fail", "~/.config/rfc-agent/config.json not found",
                             "run /rfc-agent:setup"))
    if engine and engine != "claude-code":
        # the claude CLI is not used; `/test` inside Axon runs `rfc_agent.axon_agent doctor` before this
        checks.append(_check("engine", "info", f"agents run on {engine}"))
    else:
        checks += check_claude(cfg)
    checks += check_sources(feature, rfc_run_dir, overrides or {})
    flags = resolved_flags(cfg)
    checks += check_kiwi(flags, network=network)
    if cfg is not None:
        checks += check_langfuse()
    checks += check_executors()
    checks += check_ui_login(str((overrides or {}).get("ui_login") or flags.get("testing.ui_login") or ""))
    return {
        "ok": not any(c["level"] == "fail" for c in checks),
        "checks": checks,
        "resolved": {
            "feature": feature,
            "rfc_run_dir": str(Path(rfc_run_dir).expanduser()) if rfc_run_dir else "",
            "run_dir": suggest_run_dir(cfg, feature),
            "storage": flags.get("testing.storage"),
            "kiwi_web": flags.get("testing.kiwi_web"),
            "product": flags.get("testing.product_prefix") + (feature or "<feature>"),
            "ui_login": str((overrides or {}).get("ui_login") or flags.get("testing.ui_login") or ""),
            "environments": environments(flags),
            # unfinished runs of this same feature and RFC run: the command offers to continue one
            "existing_runs": runs(rfc_run_dir, feature, limit=3) if feature and rfc_run_dir else [],
        },
    }


# ---- recent RFC runs (the picker) -----------------------------------------------------------------------


def recent(limit: int = 8) -> list[dict]:
    cfg = load_config()
    root = Path((cfg.get("RUNS_ROOT") if cfg else None) or "~/rfc-runs").expanduser()
    rows = []
    for d in root.iterdir() if root.is_dir() else ():
        rfc = d / "out" / "rfc.md"
        if rfc.is_file():
            title = next((ln.lstrip("# ").strip() for ln in rfc.read_text(errors="replace").splitlines()
                          if ln.startswith("# ")), d.name)
            rows.append({"path": str(d), "name": d.name, "title": title[:90], "mtime": rfc.stat().st_mtime})
    rows.sort(key=lambda r: r["mtime"], reverse=True)
    return rows[:limit]


# ---- earlier testing runs (resume instead of starting a duplicate) --------------------------------------

#: `status.next_step` in the words the resume question uses.
STEP_LABELS = {
    "start": "nothing ran yet",
    "resume_generate": "generation stopped part-way",
    "fix": "stopped at {stage}",
    "store": "generated, not uploaded to Kiwi yet",
    "review": "waiting for review in Kiwi",
    "execute": "execution part-done",
    "done": "finished",
}


def runs(rfc_run_dir: str | None = None, feature: str = "", *, include_done: bool = False,
         limit: int = 8) -> list[dict]:
    """Earlier testing runs, newest first: {path, name, feature, rfc_run_dir, next, label}.

    WHY: every new start used to get a fresh `<date>-testing-<feature>` folder, so one feature was generated
    three times over two days (NR-DIGICHEM, 2026-11-02/-v2/11-04), each paying ~40 minutes and ~$8 that a
    resume of the first would have skipped. Filtered to one RFC run and/or feature when given; finished runs
    are left out unless asked for, since there is nothing to continue in them.
    """
    from .status import next_step, stages, store_summary

    cfg = load_config()
    root = Path((cfg.get("RUNS_ROOT") if cfg else None) or "~/rfc-runs").expanduser()
    want_rfc = Path(rfc_run_dir).expanduser().resolve() if rfc_run_dir else None
    want_key = feature_key(feature) if feature else ""
    rows = []
    for d in root.glob("*-testing-*") if root.is_dir() else ():
        if not (d / "manifest.json").is_file():
            continue
        try:
            inv = json.loads((d / "qa" / "sources.json").read_text())
        except (OSError, ValueError):
            inv = {}
        used = inv.get("rfc_run_dir") or ""
        if want_rfc and (not used or Path(used).expanduser().resolve() != want_rfc):
            continue
        if want_key and feature_key(str(inv.get("feature") or "")) != want_key:
            continue
        nxt = next_step(stages(d), store_summary(d))
        if nxt["step"] == "done" and not include_done:
            continue
        rows.append({"path": str(d), "name": d.name, "feature": inv.get("feature") or "",
                     "rfc_run_dir": used, "next": nxt,
                     "label": STEP_LABELS[nxt["step"]].format(stage=nxt.get("stage", "")),
                     "mtime": (d / "manifest.json").stat().st_mtime})
    rows.sort(key=lambda r: r["mtime"], reverse=True)
    return rows[:limit]


# ---- feature keys for ONE RFC run (the second picker) ------------------------------------------------------

RE_JIRA_KEY = re.compile(r"\b[A-Z][A-Z0-9]+-\d+\b")
RE_RFC_JIRA_ROW = re.compile(r"^\|\s*Jira\s*\|(.*)\|\s*$", re.MULTILINE | re.IGNORECASE)


def feature_key(text: str) -> str:
    """A string as a feature key: uppercase, runs of anything else collapsed to one hyphen, at most 30
    characters, cut at a hyphen rather than mid-word."""
    key = re.sub(r"[^A-Z0-9]+", "-", text.upper()).strip("-")
    return key if len(key) <= 30 else key[:31].rsplit("-", 1)[0]


def feature_suggestions(rfc_run_dir: str, limit: int = 3) -> list[dict]:
    """Feature keys for THIS RFC run only, best first: {key, why}.

    WHY per run: the picker used to offer generic keys unrelated to the RFC chosen a step earlier. In order:
    a key an earlier testing run of this same RFC run already used (reusing it updates that Kiwi plan instead
    of starting a second one), the Jira key Scope linked, the RFC header's Jira row, then the RFC title's
    short name (the part before " — ").
    """
    run = Path(rfc_run_dir).expanduser().resolve()
    out: list[dict] = []

    def add(key: str, why: str) -> None:
        key = feature_key(key)
        if key and all(r["key"] != key for r in out):
            out.append({"key": key, "why": why})

    cfg = load_config()
    root = Path((cfg.get("RUNS_ROOT") if cfg else None) or "~/rfc-runs").expanduser()
    earlier = sorted(root.glob("*-testing-*"), key=lambda d: d.stat().st_mtime, reverse=True) \
        if root.is_dir() else []
    for d in earlier:
        try:
            inv = json.loads((d / "qa" / "sources.json").read_text())
        except (OSError, ValueError):
            continue
        used = inv.get("rfc_run_dir")
        if used and Path(used).expanduser().resolve() == run and inv.get("feature"):
            add(inv["feature"], f"used by the earlier testing run {d.name}: re-running updates its Kiwi plan")
    try:
        scope = json.loads((run / "scope.json").read_text())
    except (OSError, ValueError):
        scope = {}
    for key in [scope.get("jira"), *(scope.get("jira_included") or [])]:
        if isinstance(key, str) and key:
            add(key, "the Jira issue this RFC run was scoped from")
    rfc = run / "out" / "rfc.md"
    text = rfc.read_text(encoding="utf-8", errors="replace") if rfc.is_file() else ""
    row = RE_RFC_JIRA_ROW.search(text)
    for key in RE_JIRA_KEY.findall(row.group(1) if row else ""):
        add(key, "the Jira issue in the RFC header")
    title = next((ln[2:].strip() for ln in text.splitlines() if ln.startswith("# ")), "")
    short = re.split(r"\s+[—–-]\s+|:\s+", re.sub(r"^RFC:\s*", "", title), maxsplit=1)[0]
    if short:
        add(short, f"the RFC title ({title[:70]})")
    return out[:limit]


# ---- one-time Kiwi settings ----------------------------------------------------------------------------


def setup_kiwi(values: dict, config_path: Path | None = None, allowed: tuple[str, ...] = KIWI_SETTINGS,
               default_storage: bool = True) -> Path:
    """Merge the given non-secret settings into `TESTING` in config.json; return the file written."""
    from rfc_agent import config as config_mod

    path = config_path or config_mod._LOCAL
    data = json.loads(path.read_text()) if path.is_file() else {}
    section = data.setdefault("TESTING", {})
    for key, value in values.items():
        if key not in allowed:
            raise ValueError(f"{key!r} is not a setting this command writes; known: {allowed}")
        if value is not None:
            section[key] = value
    if default_storage:
        section.setdefault("storage", "kiwi")
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n")
    os.chmod(tmp, path.stat().st_mode & 0o777 if path.exists() else 0o600)
    tmp.replace(path)
    return path


# ---- the machine check /rfc-agent:setup runs for test-agent ---------------------------------------------


def testing_setup(*, network: bool = True, engine: str = "") -> dict:
    """What test-agent needs on this machine, with no feature or RFC run: the config, the claude CLI, Langfuse,
    Kiwi (settings, ~/.tcms.conf, tcms-api, reachability), the Playwright runner and the ui-agent browser.
    Android is reported, never failed: it is optional (`setup-android --check` has the detail)."""
    from . import ui_agent

    cfg = load_config()
    checks = []
    if cfg is None:
        checks.append(_check("config", "fail", "~/.config/rfc-agent/config.json not found", "run /rfc-agent:setup",
                             kind="setup"))
    if engine and engine != "claude-code":
        checks.append(_check("engine", "info", f"agents run on {engine}"))
    else:
        checks += check_claude(cfg)
    flags = resolved_flags(cfg)
    checks += check_kiwi(flags, network=network)
    if cfg is not None:
        checks += check_langfuse()
    checks += [c for c in check_executors() if c["id"] == "playwright"]
    problem = ui_agent.browser_problem(str(flags.get("testing.agent_browser") or ""))
    checks.append(_check("agent_browser", "warn", "ui-agent cannot start its browser", problem)
                  if problem else _check("agent_browser", "ok", "ui-agent browser (Playwright MCP) ready"))
    a = android_status()
    if a["configured"] and a["checkout_ok"]:
        tools = [n for n in ("adb", "maestro") if not a[n]]
        checks.append(_check("android", "ok" if not tools else "info",
                             f"Android: maestro-automation at {a['path']}" +
                             (f"; {' and '.join(tools)} not found" if tools else ""),
                             "install the Android SDK platform-tools and the Maestro CLI" if tools else ""))
    else:
        checks.append(_check("android", "info", "Android runs are not set up (optional)",
                             "preflight setup-android --clone, or --path <your maestro-automation checkout>"))
    ok = not any(c["level"] == "fail" for c in checks)
    return {"ok": ok, "checks": checks, "summary": summarize(checks)}


#: what each row is about, in the words the summary uses
_LABEL = {"config": "config.json", "claude": "claude CLI", "engine": "engine", "kiwi": "Kiwi", "kiwi_conf": "Kiwi",
          "kiwi_client": "Kiwi", "langfuse": "Langfuse keys", "playwright": "Playwright (web runs)",
          "agent_browser": "ui-agent browser", "android": "Android", "maestro": "Maestro"}


def summarize(checks: list[dict]) -> dict:
    """What a person re-running the check mid-setup needs (issue #21): what is ready, what is still to set up,
    what is broken, what is only a note, and the one next step — the first error's fix, else the first
    setup step's. Kiwi's three rows count as one item."""
    def names(rows):
        out: list[str] = []
        for r in rows:
            label = _LABEL.get(r["id"], r["id"])
            if label not in out:
                out.append(label)
        return out

    fails = [c for c in checks if c["level"] == "fail"]
    errors = [c for c in fails if c.get("kind") != "setup"]
    setup = [c for c in fails if c.get("kind") == "setup"]
    notes = [c for c in checks if c["level"] in ("warn", "info") and c["id"] != "engine"]
    blocked = set(names(fails)) | set(names([c for c in checks if c["level"] == "warn"]))
    ready = [n for n in names([c for c in checks if c["level"] == "ok"]) if n not in blocked]
    first = (errors or setup or [None])[0]
    return {"ready": ready, "to_set_up": names(setup), "errors": names(errors),
            "notes": [f"{_LABEL.get(c['id'], c['id'])}: {c['msg']}" for c in notes],
            "next": (first["fix"] or first["msg"]) if first else ""}


def footer(verdict: dict) -> str:
    """The last line of `preflight testing`. Starts with TESTING READY / TESTING NOT READY as before (the install
    guides look for it), then says what is done, what is left, and the next step."""
    s = verdict.get("summary") or summarize(verdict["checks"])
    head = "TESTING READY" if verdict["ok"] else "TESTING NOT READY"
    parts = [f"{len(s['ready'])} ready"] if s["ready"] else []
    if s["errors"]:
        parts.append(f"error: {', '.join(s['errors'])}")
    if s["to_set_up"]:
        parts.append(f"still to set up: {', '.join(s['to_set_up'])}")
    if verdict["ok"] and s["notes"]:
        parts.append(f"notes: {', '.join(n.split(':')[0] for n in s['notes'])}")
    line = f"{head} — {' · '.join(parts)}" if parts else head
    return f"{line}\n  next: {s['next']}" if s["next"] and not verdict["ok"] else line


def row_line(row: dict) -> str:
    """One printed row: a setup step not done yet is ⏳, not ❌ — it is pending, not broken."""
    icon = "⏳" if row.get("kind") == "setup" else _ICON[row["level"]]
    return f"{icon} {row['msg']}" + (f"  → {row['fix']}" if row["fix"] else "")


# ---- one-time Android setup ----------------------------------------------------------------------------


def android_status(config_path: Path | None = None) -> dict:
    """What Android runs need on this machine, and what is there. Prints no secret; changes nothing."""
    from . import devices

    flags = resolved_flags(load_config())
    repo = devices.maestro_repo(str(flags.get("testing.maestro_repo") or ""))
    return {"configured": repo is not None, "path": str(repo or ""),
            "checkout_ok": bool(repo and devices.is_checkout(repo)),
            "adb": bool(devices.adb_bin()), "maestro": bool(devices.maestro_bin()),
            "suggested_path": str(_default_clone_dir())}


def _default_clone_dir() -> Path:
    """Next to the user's other checkouts (REPOS_ROOT, where the pipeline clones repos), else ~/repos."""
    from . import devices

    root = ""
    with contextlib.suppress(Exception):
        from rfc_agent.config import get_config
        root = str(get_config().get("REPOS_ROOT") or "")
    return Path(root or "~/repos").expanduser() / devices.MAESTRO_REPO_NAME


def setup_android(path: str = "", clone: bool = False, into: str = "", url: str = "",
                  config_path: Path | None = None, run=None) -> dict:
    """Make Android runs usable: save the path of an existing checkout (`path`), or clone maestro-automation
    (`clone`, into `into` or next to the user's other repos) and save that. A folder that already holds a
    checkout is used as it is, never cloned over. -> {ok, path, cloned, saved_to, error}."""
    import subprocess

    from . import devices

    run = run or (lambda cmd: subprocess.run(cmd, capture_output=True, text=True, timeout=600))
    out = {"ok": False, "path": "", "cloned": False, "saved_to": "", "error": ""}
    if path:
        target = Path(path).expanduser()
        if not devices.is_checkout(target):
            out["error"] = (f"{target} is not a maestro-automation checkout (it needs .maestro/flows and "
                            f"scripts/ensure_account.sh)")
            return out
    elif clone:
        target = Path(into).expanduser() if into else _default_clone_dir()
        if devices.is_checkout(target):
            pass                                    # already cloned: use it as it is
        elif target.exists() and any(target.iterdir()):
            out["error"] = f"{target} exists and is not a maestro-automation checkout; pass --into <empty folder>"
            return out
        else:
            if not url:
                from rfc_agent.scope.repos import configured_remote, remote_template

                template = remote_template(target.parent, configured_remote())
                url = template.format(repo=devices.MAESTRO_REPO_NAME) if template else ""
            if not url:
                out["error"] = ("no clone URL: GIT_REPOS_REMOTE is not set in config.json and there is no git "
                                "checkout next to it to take the clone address from; set GIT_REPOS_REMOTE, or "
                                "pass --url git@bitbucket.org:<workspace>/maestro-automation.git")
                return out
            target.parent.mkdir(parents=True, exist_ok=True)
            done = run(["git", "clone", url, str(target)])
            if done.returncode != 0 or not devices.is_checkout(target):
                out["error"] = (f"git clone {url} failed (exit {done.returncode}): "
                                f"{(done.stderr or done.stdout or '').strip()[-300:]} — check Bitbucket SSH access")
                return out
            out["cloned"] = True
    else:
        out["error"] = "say --path <checkout> or --clone"
        return out
    out["path"] = str(target)
    out["saved_to"] = str(setup_kiwi({"maestro_repo": str(target)}, config_path, allowed=ANDROID_SETTINGS,
                                     default_storage=False))
    out["ok"] = True
    return out


# ---- CLI ------------------------------------------------------------------------------------------------

_ICON = {"ok": "✅", "info": "ℹ️", "warn": "⚠️", "fail": "❌"}


def _bool(text: str) -> bool:
    if text.lower() in ("true", "1", "yes"):
        return True
    if text.lower() in ("false", "0", "no"):
        return False
    raise argparse.ArgumentTypeError(f"expected true/false, got {text!r}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="preflight")
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("check")
    c.add_argument("--feature", default="")
    c.add_argument("--rfc-run-dir")
    for key in ("prd", "rfc", "contract", "plan", "figma", "test-data", "ui-login"):
        c.add_argument(f"--{key}")
    c.add_argument("--no-network", action="store_true")
    c.add_argument("--engine", default="", help="the engine the agents run on (axon: skip the claude CLI check)")
    c.add_argument("--json", action="store_true")
    r = sub.add_parser("recent")
    r.add_argument("--limit", type=int, default=8)
    r.add_argument("--json", action="store_true")
    u = sub.add_parser("runs")
    u.add_argument("--rfc-run-dir")
    u.add_argument("--feature", default="")
    u.add_argument("--all", action="store_true", help="include finished runs")
    u.add_argument("--limit", type=int, default=8)
    u.add_argument("--json", action="store_true")
    f = sub.add_parser("features")
    f.add_argument("--rfc-run-dir", required=True)
    f.add_argument("--json", action="store_true")
    w = sub.add_parser("setup-web")
    w.add_argument("--environments", help="comma-separated environment names, e.g. dione,earth,jupiter")
    w.add_argument("--base-url-template", help="web base URL with {env}, e.g. https://{env}.1mg.com")
    w.add_argument("--ui-login", help='path of the {"phone", "otp"} file (the path is stored, not the values)')
    w.add_argument("--curl", help="path of the captured curl the backend executor uses (the path, not its contents)")
    w.add_argument("--env", help="default environment for /test-agent:execute, e.g. jupiter")
    w.add_argument("--build", help="default build label for /test-agent:execute, e.g. unknown")
    w.add_argument("--production-base-url", help="the live site's address, so \"on prod\" needs no typing; "
                                                 "running there still needs --production")
    w.add_argument("--sessions-dir", help="folder of recorded login sessions (*.json) /test-agent:execute may "
                                          "offer by name (the folder, not the files' contents)")
    e = sub.add_parser("envs")
    e.add_argument("--json", action="store_true")
    a = sub.add_parser("setup-android")
    how = a.add_mutually_exclusive_group(required=True)
    how.add_argument("--check", action="store_true", help="what Android runs need here, and what is there")
    how.add_argument("--path", help="an existing maestro-automation checkout to use")
    how.add_argument("--clone", action="store_true", help="clone maestro-automation and use that")
    a.add_argument("--into", default="", help="--clone: the folder to clone into (default: next to REPOS_ROOT's repos)")
    a.add_argument("--url", default="", help="--clone: the clone URL (default: from the Bitbucket workspace)")
    a.add_argument("--json", action="store_true")
    t = sub.add_parser("testing", help="what test-agent needs on this machine (no feature or RFC run needed)")
    t.add_argument("--no-network", action="store_true")
    t.add_argument("--engine", default="", help="the engine the agents run on (axon: skip the claude CLI check)")
    t.add_argument("--json", action="store_true")
    s = sub.add_parser("setup-kiwi")
    s.add_argument("--kiwi-web", required=True)
    s.add_argument("--product-prefix", required=True)
    s.add_argument("--kiwi-user")
    s.add_argument("--kiwi-url")
    s.add_argument("--insecure-tls", type=_bool)
    args = ap.parse_args(argv)

    if args.cmd == "recent":
        rows = recent(args.limit)
        print(json.dumps(rows, indent=2) if args.json else
              "\n".join(f"{r['name']}  {r['title']}" for r in rows) or "no RFC runs found")
        return 0
    if args.cmd == "runs":
        rows = runs(args.rfc_run_dir, args.feature, include_done=args.all, limit=args.limit)
        print(json.dumps(rows, indent=2) if args.json else
              "\n".join(f"{r['name']}  {r['label']}" for r in rows) or "no testing runs to continue")
        return 0
    if args.cmd == "features":
        rows = feature_suggestions(args.rfc_run_dir)
        print(json.dumps(rows, indent=2) if args.json else
              "\n".join(f"{r['key']}  ({r['why']})" for r in rows) or "no key found in this RFC run")
        return 0
    if args.cmd == "setup-web":
        values = {"environments": args.environments, "base_url_template": args.base_url_template,
                  "ui_login": args.ui_login, "curl": args.curl, "env": args.env, "build": args.build,
                  "production_base_url": args.production_base_url, "sessions_dir": args.sessions_dir}
        if args.base_url_template and "{env}" not in args.base_url_template:
            ap.error("--base-url-template needs {env}, e.g. https://{env}.1mg.com")
        path = setup_kiwi(values, allowed=WEB_SETTINGS, default_storage=False)
        print(f"wrote TESTING web settings to {path} ({', '.join(k for k, v in values.items() if v)})")
        return 0
    if args.cmd == "envs":
        rows = environments(resolved_flags(load_config()))
        print(json.dumps(rows, indent=2) if args.json else
              "\n".join(f"{r['env']}  {r['base_url']}" for r in rows) or "no environments configured")
        return 0
    if args.cmd == "setup-android":
        res = android_status() if args.check else setup_android(args.path or "", args.clone, args.into, args.url)
        if args.json:
            print(json.dumps(res, indent=2))
        elif args.check:
            print("\n".join(f"{k}: {v}" for k, v in res.items()))
        else:
            print(f"maestro-automation: {res['path']} ({'cloned, ' if res['cloned'] else ''}saved to {res['saved_to']})"
                  if res["ok"] else f"setup-android failed: {res['error']}")
        return 0 if args.check or res["ok"] else 1
    if args.cmd == "setup-kiwi":
        path = setup_kiwi({"storage": "kiwi", "kiwi_web": args.kiwi_web, "product_prefix": args.product_prefix,
                           "kiwi_user": args.kiwi_user, "kiwi_url": args.kiwi_url,
                           "insecure_tls": args.insecure_tls})
        print(f"wrote TESTING Kiwi settings to {path} (no password: that stays in ~/.tcms.conf)")
        return 0
    if args.cmd == "testing":
        verdict = testing_setup(network=not args.no_network, engine=args.engine)
        if args.json:
            print(json.dumps(verdict, indent=2))
        else:
            for row in verdict["checks"]:
                print(row_line(row))
            print(footer(verdict))
        return 0 if verdict["ok"] else 1
    overrides = {k: getattr(args, k) for k in ("prd", "rfc", "contract", "plan", "figma", "test_data", "ui_login")}
    verdict = check(args.feature, args.rfc_run_dir, overrides, network=not args.no_network, engine=args.engine)
    if args.json:
        print(json.dumps(verdict, indent=2))
    else:
        for row in verdict["checks"]:
            print(row_line(row))
        print("PREFLIGHT OK" if verdict["ok"] else "PREFLIGHT FAILED")
    return 0 if verdict["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
