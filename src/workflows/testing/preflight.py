"""`preflight` — everything `/test-agent:test` checks before a run starts, as one JSON verdict.

WHY a module and not a list of shell probes in the command prompt: the expensive failures of this workflow
land late. Kiwi is first touched by `store_cases`, 35 minutes in (2026-11-02, NR-DIGICHEM: the first run
generated, evaluated and then could not store). A missing contract silently makes every backend case
`manual`. Each of these is a seconds-long check, and the command needs one machine-readable answer it can
turn into a checklist and a question, not six ad-hoc greps.

    python -m workflows.testing.preflight check --feature F --rfc-run-dir D [--json] [--no-network]
    python -m workflows.testing.preflight recent [--limit N] [--json]
    python -m workflows.testing.preflight features --rfc-run-dir D [--json]
    python -m workflows.testing.preflight setup-kiwi --kiwi-web URL --product-prefix P [--kiwi-user U]
                                                    [--kiwi-url X] [--insecure-tls true|false]
    python -m workflows.testing.preflight setup-web [--environments a,b,c] [--base-url-template T]
                                                   [--ui-login FILE]
    python -m workflows.testing.preflight envs [--json]

`check` exits 0 when nothing failed (warnings allowed), 1 when a check failed. Each check is
{id, level: ok|info|warn|fail, msg, fix}. The Kiwi check is `fail` when Kiwi is unreachable: the command, not
this module, decides whether the user may go on without it (generate now, store later).

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
WEB_SETTINGS = ("environments", "base_url_template", "ui_login", "curl")


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


def _check(id_: str, level: str, msg: str, fix: str = "") -> dict:
    return {"id": id_, "level": level, "msg": msg, "fix": fix}


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
                       "run `preflight setup-kiwi` once (writes TESTING in config.json)")]
    out = []
    web = flags.get("testing.kiwi_web")
    explicit = all(flags.get(f"testing.{k}") for k in ("kiwi_url", "kiwi_user", "kiwi_password"))
    if not explicit:
        conf = Path("~/.tcms.conf").expanduser()
        if not conf.is_file():
            return [_check("kiwi_conf", "fail", "~/.tcms.conf is missing: there is no Kiwi login",
                           "create it yourself with [tcms] url/username/password, then chmod 600 ~/.tcms.conf")]
        if conf.stat().st_mode & (stat.S_IRWXG | stat.S_IRWXO):
            out.append(_check("kiwi_conf", "warn", "~/.tcms.conf is readable by other users",
                              "chmod 600 ~/.tcms.conf"))
        else:
            out.append(_check("kiwi_conf", "ok", "~/.tcms.conf present, mode 600"))
    try:
        import tcms_api  # noqa: F401
    except ImportError:
        return out + [_check("kiwi_client", "fail", "tcms-api is not installed",
                             "uv sync --extra testing (in the plugin folder)")]
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
                         f"{fix}; or generate now and store in Kiwi later")]


def check_langfuse() -> list[dict]:
    from rfc_agent import observe

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        code = observe.preflight()
    text = buf.getvalue().strip()
    if code == 0:
        return [_check("langfuse", "ok", text.splitlines()[0] if text else "langfuse ok")]
    return [_check("langfuse", "fail", text, "add LANGFUSE keys to config.json, or pass --langfuse false")]


def check_claude(cfg) -> list[dict]:
    binary = (cfg.get("AGENT.CLAUDE_BIN") if cfg else None) or "claude"
    if shutil.which(str(Path(binary).expanduser())) or Path(binary).expanduser().is_file():
        return [_check("claude", "ok", f"claude CLI: {binary}")]
    return [_check("claude", "fail", f"claude CLI not found ({binary})", "set AGENT.CLAUDE_BIN in config.json")]


def check_executors() -> list[dict]:
    """The UI runners. Never `fail`: they matter only for phase B, and only for the kinds chosen there."""
    from . import ui_web

    out = []
    problem = ui_web.runner_problem()
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
          network: bool = True) -> dict:
    cfg = load_config()
    checks = []
    if cfg is None:
        checks.append(_check("config", "fail", "~/.config/rfc-agent/config.json not found",
                             "run /rfc-agent:setup"))
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
    c.add_argument("--json", action="store_true")
    r = sub.add_parser("recent")
    r.add_argument("--limit", type=int, default=8)
    r.add_argument("--json", action="store_true")
    f = sub.add_parser("features")
    f.add_argument("--rfc-run-dir", required=True)
    f.add_argument("--json", action="store_true")
    w = sub.add_parser("setup-web")
    w.add_argument("--environments", help="comma-separated environment names, e.g. dione,earth,jupiter")
    w.add_argument("--base-url-template", help="web base URL with {env}, e.g. https://{env}.1mg.com")
    w.add_argument("--ui-login", help='path of the {"phone", "otp"} file (the path is stored, not the values)')
    w.add_argument("--curl", help="path of the captured curl the backend executor uses (the path, not its contents)")
    e = sub.add_parser("envs")
    e.add_argument("--json", action="store_true")
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
    if args.cmd == "features":
        rows = feature_suggestions(args.rfc_run_dir)
        print(json.dumps(rows, indent=2) if args.json else
              "\n".join(f"{r['key']}  ({r['why']})" for r in rows) or "no key found in this RFC run")
        return 0
    if args.cmd == "setup-web":
        values = {"environments": args.environments, "base_url_template": args.base_url_template,
                  "ui_login": args.ui_login, "curl": args.curl}
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
    if args.cmd == "setup-kiwi":
        path = setup_kiwi({"storage": "kiwi", "kiwi_web": args.kiwi_web, "product_prefix": args.product_prefix,
                           "kiwi_user": args.kiwi_user, "kiwi_url": args.kiwi_url,
                           "insecure_tls": args.insecure_tls})
        print(f"wrote TESTING Kiwi settings to {path} (no password: that stays in ~/.tcms.conf)")
        return 0
    overrides = {k: getattr(args, k) for k in ("prd", "rfc", "contract", "plan", "figma", "test_data", "ui_login")}
    verdict = check(args.feature, args.rfc_run_dir, overrides, network=not args.no_network)
    if args.json:
        print(json.dumps(verdict, indent=2))
    else:
        for row in verdict["checks"]:
            print(f"{_ICON[row['level']]} {row['msg']}" + (f"  → {row['fix']}" if row["fix"] else ""))
        print("PREFLIGHT OK" if verdict["ok"] else "PREFLIGHT FAILED")
    return 0 if verdict["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
