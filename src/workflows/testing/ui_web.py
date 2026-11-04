"""Playwright for web UI cases — the runner seam `derive_ui` and `execute_web` share.

One test per CONFIRMED `exec:ui` + `surface:web` case, in `qa/ui/tests/<extid>.spec.ts`, titled with the
case id. The title IS the mapping back to the store: nothing else (file names, describe blocks) is trusted
to say which case a result belongs to.

The runner is the Node project in `tools/ui-runner` (pinned `@playwright/test`, installed once per machine
with `npm ci`). A run dir does not get its own `node_modules`: `qa/ui/node_modules` is a symlink to the
runner's, so a spec's `import { test } from '@playwright/test'` resolves without an install per run.

Status mapping, in the vocabulary `kiwi_runner` set (FAIL is a defect, BLOCKED is the environment, ERROR is
the harness):

  passed                                   -> PASS
  skipped (test.skip with a reason)        -> BLOCKED   a prerequisite the test checked for is missing
  failed / timedOut, the page never loaded -> BLOCKED   net::ERR_*, a navigation timeout, a 5xx document
  failed / timedOut, anything else         -> FAIL      the page loaded and an assertion did not hold
  interrupted, or the browser never ran    -> ERROR

Login. A recorded session (`--ui-auth`) expires; a test account whose number and OTP never change does not.
With `--ui-login <file>`, `tests/login.setup.ts` (written by `derive_ui`, titled `login`) runs first as a
Playwright setup project: it reads the number and OTP from `UI_LOGIN_PHONE` / `UI_LOGIN_OTP`, logs in, and
saves the session to `STORAGE_STATE`, which every case then starts from. The login step is not a case: it
is never recorded or attached to the store, so its trace — which holds what was typed — stays in the run
dir. If it fails, the cases never run and are recorded BLOCKED with the login error, not FAIL.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

#: The Node project holding the pinned Playwright (repo root / tools / ui-runner).
RUNNER_DIR = Path(__file__).resolve().parents[3] / "tools" / "ui-runner"
UI_DIR = "qa/ui"
TESTS_DIR = "tests"
PLAN = "plan.json"
CASES = "cases.json"
#: {"login_required": bool} — written by derive_ui, read by its gate and by check_ui.
SETTINGS = "settings.json"
REPORT = "report.json"
#: Playwright's HTML report of the last run: `npx playwright show-report <qa/ui>/html-report` (traces inside).
HTML_REPORT = "html-report"
LOGIN_SPEC = "login.setup.ts"
LOGIN_TITLE = "login"
#: The environment variables the login step reads; no other spec may touch them.
LOGIN_ENV = ("UI_LOGIN_PHONE", "UI_LOGIN_OTP")
#: Per-file ceiling for evidence attached to an execution (screenshots, traces).
MAX_ATTACH = 8 * 1024 * 1024

CONFIG = """\
// Written by the testing workflow (workflows/testing/ui_web.py) on every run. Edits are overwritten.
import { defineConfig, devices } from '@playwright/test';

const device = process.env.UI_DEVICE ? devices[process.env.UI_DEVICE] : {};
if (process.env.UI_DEVICE && !device) throw new Error(`unknown Playwright device ${process.env.UI_DEVICE}`);

// UI_LOGIN=1: tests/login.setup.ts logs in first and writes STORAGE_STATE; the cases depend on it.
const login = process.env.UI_LOGIN === '1';
const state = process.env.STORAGE_STATE || undefined;

export default defineConfig({
  testDir: './tests',
  timeout: Number(process.env.UI_TIMEOUT_MS || 60000),
  retries: 0,
  workers: 1,
  outputDir: './artifacts',
  reporter: [['json', { outputFile: './report.json' }], ['html', { outputFolder: './html-report', open: 'never' }],
             ['list']],
  use: {
    ...device,
    baseURL: process.env.BASE_URL,
    // UI_HEADED=1: a visible browser, slowed down, so a person can watch the suite run.
    headless: process.env.UI_HEADED !== '1',
    launchOptions: { slowMo: Number(process.env.UI_SLOWMO_MS || 0) },
    screenshot: 'only-on-failure',
    trace: 'retain-on-failure',
    ignoreHTTPSErrors: true,
  },
  projects: [
    ...(login ? [{ name: 'login', testMatch: /login\\.setup\\.ts$/ }] : []),
    { name: 'cases', testIgnore: /login\\.setup\\.ts$/, dependencies: login ? ['login'] : [],
      use: { storageState: state } },
  ],
});
"""

RE_TEST_TITLE = re.compile(r"""\btest\(\s*(['"`])(?P<title>[^'"`]+)\1""")
RE_ABSOLUTE_URL = re.compile(r"""['"`]https?://""")
RE_SECRET_LITERAL = re.compile(
    r"""(password|passwd|secret|api[_-]?key|token|authorization|cookie)\s*[:=]\s*['"`][^'"`]{4,}""", re.I)
RE_BLOCKED = re.compile(r"net::ERR_|page\.goto: Timeout|NS_ERROR_|ERR_NAME_NOT_RESOLVED|"
                        r"Navigation failed|status of 5\d\d", re.I)


def ui_dir(run_dir: Path) -> Path:
    return run_dir / UI_DIR


def spec_name(extid: str) -> str:
    return f"{extid}.spec.ts"


def runner_problem() -> str:
    """Why the runner cannot be used, or "" when it can."""
    if not shutil.which("node"):
        return "node is not installed (Playwright needs Node 18+)"
    if not (RUNNER_DIR / "node_modules" / "@playwright" / "test").is_dir():
        return f"Playwright is not installed: run `npm ci && npx playwright install chromium` in {RUNNER_DIR}"
    return ""


def prepare(ui: Path) -> None:
    """Write the config and link the runner's node_modules into a run's qa/ui."""
    (ui / TESTS_DIR).mkdir(parents=True, exist_ok=True)
    (ui / "playwright.config.mjs").write_text(CONFIG, encoding="utf-8")
    link = ui / "node_modules"
    target = RUNNER_DIR / "node_modules"
    if link.is_symlink() and Path(os.readlink(link)) != target:
        link.unlink()
    if not link.exists() and not link.is_symlink():
        link.symlink_to(target, target_is_directory=True)


def _playwright(ui: Path, args: list[str], env: dict | None = None, timeout: int = 120):
    cli = RUNNER_DIR / "node_modules" / "@playwright" / "test" / "cli.js"
    return subprocess.run(["node", str(cli), "test", "--config", "playwright.config.mjs", *args],
                          cwd=ui, capture_output=True, text=True, timeout=timeout,
                          env={**os.environ, **(env or {})})


def _walk_specs(suites: list[dict]):
    for suite in suites or []:
        yield from suite.get("specs") or []
        yield from _walk_specs(suite.get("suites") or [])


def list_titles(ui: Path, login: bool = False) -> tuple[list[str], str]:
    """(test titles Playwright can load, error). A spec that does not compile is an error here. With
    `login`, the login step is loaded too (as the title `login`)."""
    problem = runner_problem()
    if problem:
        return [], problem
    prepare(ui)
    try:
        proc = _playwright(ui, ["--list", "--reporter=json"], env={"UI_LOGIN": "1" if login else ""})
    except subprocess.TimeoutExpired:
        return [], "playwright --list timed out after 120s"
    try:
        report = json.loads(proc.stdout)
    except ValueError:
        return [], (proc.stderr or proc.stdout).strip()[-1500:] or f"playwright --list exited {proc.returncode}"
    errors = [e.get("message", "") for e in report.get("errors") or []]
    if errors:
        return [], "\n".join(errors)[-1500:]
    return [s.get("title", "") for s in _walk_specs(report.get("suites") or [])], ""


def run(ui: Path, *, base_url: str, storage_state: str = "", device: str = "", timeout_s: int = 60,
        wall_s: int = 1800, login: dict | None = None, headed: bool = False) -> tuple[dict | None, str]:
    """Run every spec. -> (the JSON report, error). A run whose assertions failed still returns its report.

    `login` ({"phone", "otp"}) runs the login step first; its values go to the Playwright process's
    environment and nowhere else. Otherwise `storage_state` is a recorded session to start from.
    """
    prepare(ui)
    (ui / REPORT).unlink(missing_ok=True)
    env = {"BASE_URL": base_url, "UI_TIMEOUT_MS": str(timeout_s * 1000), "UI_DEVICE": device,
           "STORAGE_STATE": str(Path(storage_state).expanduser()) if storage_state else "", "UI_LOGIN": "",
           "UI_HEADED": "1" if headed else "", "UI_SLOWMO_MS": "400" if headed else "0"}
    if login:
        state = ui / ".auth" / "state.json"
        state.parent.mkdir(exist_ok=True)
        env.update(UI_LOGIN="1", STORAGE_STATE=str(state), UI_LOGIN_PHONE=str(login["phone"]),
                   UI_LOGIN_OTP=str(login["otp"]))
    try:
        proc = _playwright(ui, [], env=env, timeout=wall_s)
    except subprocess.TimeoutExpired:
        return None, f"the Playwright run exceeded {wall_s}s"
    if not (ui / REPORT).is_file():
        return None, (proc.stderr or proc.stdout).strip()[-1500:] or f"playwright exited {proc.returncode}"
    try:
        return json.loads((ui / REPORT).read_text()), ""
    except ValueError as exc:
        return None, f"{REPORT} is not JSON: {exc}"


def classify(status: str, message: str) -> str:
    if status == "passed":
        return "PASS"
    if status == "skipped":
        return "BLOCKED"
    if status in ("failed", "timedOut"):
        if "browserType.launch" in message or "Executable doesn't exist" in message:
            return "ERROR"
        return "BLOCKED" if RE_BLOCKED.search(message) else "FAIL"
    return "ERROR"


def results(report: dict) -> list[dict]:
    """One row per CASE: {extid, status, detail, duration_s, attachments[{name, path, content_type}]}.

    The login step is not a case and gets no row. When it failed, the cases Playwright then skipped are
    BLOCKED with the login's error, so a dead login never reads as a product defect.
    """
    out = []
    login_error = ""
    for spec in _walk_specs(report.get("suites") or []):
        for test in spec.get("tests") or []:
            runs = test.get("results") or []
            last = runs[-1] if runs else {}
            status = last.get("status") or ("skipped" if test.get("status") == "skipped" else "interrupted")
            errors = [e.get("message", "") for e in (last.get("errors") or [])] or \
                     ([last["error"].get("message", "")] if last.get("error") else [])
            message = _strip_ansi("\n".join(errors)).strip()
            if spec.get("title") == LOGIN_TITLE:
                if status != "passed":
                    login_error = f"the login step {status}: {message or 'no error message'}"[:600]
                continue
            if status == "skipped":
                notes = [a.get("description", "") for a in test.get("annotations") or []
                         if a.get("type") == "skip"]
                message = "; ".join(n for n in notes if n) or "skipped by the test"
            out.append({
                "extid": spec.get("title", ""),
                "status": classify(status, message),
                "playwright_status": status,
                "detail": (message or "all assertions held")[:900],
                "duration_s": round((last.get("duration") or 0) / 1000, 2),
                "finished_at": _finished_at(last),
                "attachments": [{"name": a.get("name", ""), "path": a.get("path", ""),
                                 "content_type": a.get("contentType", "")}
                                for a in last.get("attachments") or [] if a.get("path")],
            })
    if login_error:
        for row in out:
            if row["playwright_status"] == "skipped":
                row.update(status="BLOCKED", detail=f"not run — {login_error}")
    return out


def _finished_at(result: dict):
    """When the test actually ended (its start + duration), tz-aware, or None when the report omits it."""
    from datetime import datetime, timedelta

    try:
        start = datetime.fromisoformat(str(result["startTime"]).replace("Z", "+00:00"))
    except (KeyError, ValueError):
        return None
    return start + timedelta(milliseconds=result.get("duration") or 0)


def _strip_ansi(text: str) -> str:
    return re.sub(r"\x1b\[[0-9;]*m", "", text or "")


# ---- the derive_ui contract --------------------------------------------------------------------------


def validate(plan: dict, cases: list[dict], ui: Path, titles: list[str] | None, list_error: str = "",
             roots: list[Path] = (), login_required: bool = False) -> list[str]:
    """Every gate failure for `qa/ui/plan.json` and the spec files it names.

    `titles` is what `playwright --list` loaded (None: not checked). `roots` are the repo snapshots a
    locator's `source` may point into. `login_required`: the run logs in with a test account, so the plan
    must carry the login step.
    """
    out: list[str] = []
    for section in ("tests", "skipped"):
        if not isinstance(plan.get(section), list):
            out.append(f"plan.json: `{section}` missing or not a list")
    if out:
        return out
    known = {c["extid"] for c in cases}
    tested = [t.get("extid") for t in plan["tests"] if isinstance(t, dict)]
    skipped = [s.get("extid") for s in plan["skipped"] if isinstance(s, dict)]
    for extid in sorted(set(tested) & set(skipped)):
        out.append(f"{extid}: in both `tests` and `skipped`")
    for extid in sorted({e for e in tested + skipped if e} - known):
        out.append(f"{extid}: not a CONFIRMED web UI case (see {CASES})")
    for extid in sorted(known - set(tested) - set(skipped)):
        out.append(f"{extid}: neither tested nor skipped — write its test, or skip it with a reason")
    for dup in sorted({e for e in tested if tested.count(e) > 1}):
        out.append(f"{dup}: listed twice in `tests`")
    for row in plan["skipped"]:
        if isinstance(row, dict) and not str(row.get("reason", "")).strip():
            out.append(f"{row.get('extid')}: skipped with no reason")
    for row in plan["tests"]:
        if isinstance(row, dict):
            out += _check_test(row, ui, roots)
    login = plan.get("login")
    if login_required and plan["tests"] and not isinstance(login, dict):
        out.append(f"plan.json: no `login` — this run logs in with a test account: write {TESTS_DIR}/"
                   f"{LOGIN_SPEC} and describe it under `login`")
    if isinstance(login, dict):
        out += _check_login(login, ui, roots)
    if not isinstance(login, dict) and (ui / TESTS_DIR / LOGIN_SPEC).is_file():
        out.append(f"{TESTS_DIR}/{LOGIN_SPEC} exists but plan.json has no `login` — delete it or describe it")
    on_disk = {p.name for p in (ui / TESTS_DIR).glob("*.spec.ts")} if (ui / TESTS_DIR).is_dir() else set()
    for stray in sorted(on_disk - {spec_name(e) for e in tested if e}):
        out.append(f"{TESTS_DIR}/{stray}: a spec file no `tests` entry names — delete it or list it")
    if list_error:
        out.append(f"Playwright could not load the specs: {list_error}")
    elif titles is not None:
        for extid in sorted(set(tested) - set(titles)):
            out.append(f"{extid}: Playwright did not load a test titled {extid!r}")
        if isinstance(login, dict) and LOGIN_TITLE not in titles:
            out.append(f"Playwright did not load the login step (a test titled {LOGIN_TITLE!r} in {LOGIN_SPEC})")
        for extra in sorted(set(titles) - set(tested) - {LOGIN_TITLE}):
            out.append(f"Playwright loaded a test titled {extra!r} that is not a case id in `tests`")
    return out


def _check_test(row: dict, ui: Path, roots: list[Path]) -> list[str]:
    extid = row.get("extid") or "<no extid>"
    spec = ui / TESTS_DIR / spec_name(extid)
    if not spec.is_file():
        return [f"{extid}: {TESTS_DIR}/{spec_name(extid)} does not exist"]
    text = spec.read_text(encoding="utf-8", errors="replace")
    out = []
    titles = [m.group("title") for m in RE_TEST_TITLE.finditer(text)]
    if titles != [extid]:
        out.append(f"{extid}: the spec must hold exactly one test, titled {extid!r} (found {titles or 'none'})")
    if RE_ABSOLUTE_URL.search(text):
        out.append(f"{extid}: an absolute URL in the spec — use paths (`page.goto('/route')`); the base URL "
                   f"is the run's --base-url")
    if RE_SECRET_LITERAL.search(text):
        out.append(f"{extid}: a credential-looking literal in the spec — login happens before the case "
                   f"(the login step, or a recorded session), never in it")
    if any(v in text for v in LOGIN_ENV):
        out.append(f"{extid}: reads the login credentials ({', '.join(LOGIN_ENV)}) — only {LOGIN_SPEC} may")
    if "waitForTimeout(" in text:
        out.append(f"{extid}: `waitForTimeout` is a fixed sleep — wait on the element with a web-first "
                   f"assertion (`await expect(locator).toBeVisible()`)")
    if "page.route(" in text and not row.get("mocks"):
        out.append(f"{extid}: the spec mocks the network (`page.route`) but `mocks` in plan.json does not "
                   f"declare it — list each mocked URL and the precondition it stands in for")
    locators = row.get("locators")
    if not isinstance(locators, list) or not locators:
        out.append(f"{extid}: no `locators` — every element the test touches must cite where it is defined")
        return out
    for loc in locators:
        if not isinstance(loc, dict) or not loc.get("locator") or not loc.get("source"):
            out.append(f"{extid}: a locator entry needs `locator` and `source`")
            continue
        why = _source_problem(str(loc["source"]), roots)
        if why:
            out.append(f"{extid}: locator {loc['locator']!r} cites {loc['source']!r} — {why}")
    return out


def _check_login(login: dict, ui: Path, roots: list[Path]) -> list[str]:
    spec = ui / TESTS_DIR / LOGIN_SPEC
    if not spec.is_file():
        return [f"login: {TESTS_DIR}/{LOGIN_SPEC} does not exist"]
    text = spec.read_text(encoding="utf-8", errors="replace")
    out = []
    titles = [m.group("title") for m in RE_TEST_TITLE.finditer(text)]
    if titles != [LOGIN_TITLE]:
        out.append(f"login: {LOGIN_SPEC} must hold exactly one test, titled {LOGIN_TITLE!r} (found "
                   f"{titles or 'none'})")
    for var in LOGIN_ENV:
        if f"process.env.{var}" not in text:
            out.append(f"login: {LOGIN_SPEC} must read the {'number' if 'PHONE' in var else 'OTP'} from "
                       f"process.env.{var}, never a literal")
    if "process.env.STORAGE_STATE" not in text or "storageState(" not in text:
        out.append(f"login: {LOGIN_SPEC} must save the session with "
                   f"`page.context().storageState({{ path: process.env.STORAGE_STATE }})`")
    if RE_ABSOLUTE_URL.search(text):
        out.append("login: an absolute URL in the login step — use a path; the base URL is the run's")
    if re.search(r"""['"`]\+?\d{6,}['"`]""", text):
        out.append("login: a number literal in the login step — the number and OTP come from the environment")
    locators = login.get("locators")
    if not isinstance(locators, list) or not locators:
        out.append("login: no `locators` — every element the login step touches must cite where it is defined")
        return out
    for loc in locators:
        if not isinstance(loc, dict) or not loc.get("locator") or not loc.get("source"):
            out.append("login: a locator entry needs `locator` and `source`")
            continue
        why = _source_problem(str(loc["source"]), roots)
        if why:
            out.append(f"login: locator {loc['locator']!r} cites {loc['source']!r} — {why}")
    return out


def _source_problem(source: str, roots: list[Path]) -> str:
    path, _, line = source.rpartition(":")
    if not path or not line.isdigit():
        return "write it as <file>:<line>"
    candidates = [Path(path).expanduser()] if Path(path).is_absolute() else [r / path for r in roots]
    hit = next((p for p in candidates if p.is_file()), None)
    if hit is None:
        return "no such file in the repo snapshot"
    n = sum(1 for _ in hit.open(encoding="utf-8", errors="replace"))
    return "" if 1 <= int(line) <= n else f"line {line} is past the end of the file ({n} lines)"
