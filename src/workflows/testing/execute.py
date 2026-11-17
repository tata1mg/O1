"""`execute` — run the CONFIRMED cases of an existing testing run, from the run dir alone.

The full `harness run testing …` line repeats what the run dir already knows (feature, RFC run, environment,
build) plus the flags that compose the executor's stages. This builds that line from the run:

    python -m workflows.testing.execute [web|backend|ui-agent|all] [--run-dir R] [--env E] [--build B] [--headed]
                                        [--only-existing] [--cases TC-…,TC-…] [--curl FILE] [--print]
                                        [--feature F --kiwi-plan ID]
    python -m workflows.testing.execute --pending [--run-dir R]      what has a web test, what does not
    python -m workflows.testing.execute ui-agent --pending           what the browser agent will run
    python -m workflows.testing.execute --lookup [--plan ID]         plans, a plan's cases, environments, sessions
    python -m workflows.testing.execute --resolve-cases "173, ct2" --plan ID    which case ids those words mean

- `web` -> `derive_ui,execute_web` with `testing.ui_only_new`: tests are written only for CONFIRMED web
           cases that have none yet (no model call when there are none), then every test runs.
           `--only-existing` -> `execute_web` alone: run the tests already written, now, no model.
- `backend` -> `derive_spec,execute_be`: derive_spec is Python and seconds, and re-reading the CONFIRMED
               cases picks up any confirmed since the last run.
- `ui-agent` -> `execute_ui_agent`: an agent drives a browser through each CONFIRMED web UI case; nothing
                is derived first, so `--only-existing` and `ui_only_new` do not apply.
- `all` -> `web` and `backend`, one after the other (never `ui-agent`).
- `--feature F [--kiwi-plan ID]` -> no earlier run needed: the run folder for F is created (or the newest
  one for F reused) and marked `origin: kiwi-direct`, so the cases are read from, and recorded into, that
  Kiwi plan as they are. Only `ui-agent` runs on such a run: there is no generated suite for the others.

Defaults: the run dir is the newest `<RUNS_ROOT>/*-testing-*`; feature and RFC run come from its
`qa/sources.json`; environment and build from `--env`/`--build`, else `testing.env`/`testing.build` in config
(`preflight setup-web --env/--build`), else the last results (`qa/ui/results.json`, `qa/results.json`); the
curl from `testing.curl` in config. `--print` shows the command instead of running it.
Exit code: the harness's (the last one, for `all`).
"""

from __future__ import annotations

import argparse
import json
import re
import shlex
import subprocess
import sys
from datetime import date
from pathlib import Path

if __package__ in (None, ""):  # run as a script: make `workflows.testing` importable from src/
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from workflows.testing.preflight import load_config, resolved_flags  # noqa: E402

PROJECT = Path(__file__).resolve().parents[3]
KINDS = {"web": (("derive_ui", "execute_web"), "testing.execute_web", "qa/ui/results.json"),
         "backend": (("derive_spec", "execute_be"), "testing.execute", "qa/results.json"),
         "ui-agent": (("execute_ui_agent",), "testing.execute_ui_agent", "qa/agent/results.json")}
#: `all` is the generated suite, both halves; the agent is its own choice.
ALL_KINDS = ("web", "backend")
DIRECT = "kiwi-direct"  # `origin` in qa/sources.json of a run bootstrapped from a Kiwi plan


def _load(path: Path) -> dict:
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def runs_root(cfg) -> Path:
    return Path((cfg.get("RUNS_ROOT") if cfg else None) or "~/rfc-runs").expanduser()


def latest_run(cfg) -> Path | None:
    root = runs_root(cfg)
    runs = [d for d in root.glob("*-testing-*") if (d / "qa" / "sources.json").is_file()]
    return max(runs, key=lambda d: (d / "manifest.json").stat().st_mtime if (d / "manifest.json").exists()
               else 0, default=None)


def bootstrap_run(root: Path, feature: str, plan_id: str = "", today: date | None = None) -> Path:
    """The run folder for `feature` under `root`, created when there is none — so a Kiwi plan can be run
    without a test-generation run before it.

    The newest existing `*-testing-*` folder for the feature is reused whatever its origin: it already holds
    that feature's results and logs. A new one is marked `origin: kiwi-direct` with the plan id. A reused
    kiwi-direct folder follows a newly given plan id; a run this workflow generated is never rewritten.
    """
    def stamp(d: Path) -> float:
        m = d / "manifest.json"
        return (m if m.exists() else d / "qa" / "sources.json").stat().st_mtime

    found = [d for d in root.glob("*-testing-*")
             if (d / "qa" / "sources.json").is_file() and _load(d / "qa" / "sources.json").get("feature") == feature]
    if found:
        run = max(found, key=stamp)
        sources = _load(run / "qa" / "sources.json")
        if sources.get("origin") == DIRECT and plan_id and str(sources.get("kiwi_plan_id") or "") != plan_id:
            (run / "qa" / "sources.json").write_text(json.dumps({**sources, "kiwi_plan_id": plan_id}, indent=2))
        return run
    run = root / f"{(today or date.today()).isoformat()}-testing-{feature}"
    (run / "qa").mkdir(parents=True, exist_ok=True)
    (run / "logs").mkdir(exist_ok=True)
    (run / "qa" / "sources.json").write_text(json.dumps(
        {"feature": feature, "rfc_run_dir": "", "sources": [], "origin": DIRECT, "kiwi_plan_id": plan_id}, indent=2))
    return run


def command(kind: str, run_dir: Path, *, env: str = "", build: str = "", headed: bool = False,
            cases: str = "", curl: str = "", flags=None, only_existing: bool = False,
            engine: str = "", base_url: str = "", ui_auth: str = "", hosts: str = "",
            browser: str = "", read_only: bool = False, parallel: int = 0, production: bool = False,
            test_account: bool = False) -> list[str]:
    """The harness command line for one kind, or SystemExit naming what is missing."""
    stages, enable, results = KINDS[kind]
    if kind == "web" and only_existing:
        stages = ("execute_web",)
    sources = _load(run_dir / "qa" / "sources.json")
    # The last run of this kind, else of the other kind: both name the same `<feature> · <env> · <build>` run.
    lasts = [_load(run_dir / results)] + [_load(run_dir / r) for _, _, r in KINDS.values() if r != results]
    feature, rfc_run = sources.get("feature"), sources.get("rfc_run_dir")
    # --env > the default you set (`TESTING.env` in config.json, via `preflight setup-web --env`) > the last run's
    env = env or str((flags.get("testing.env") if flags else "") or "") or \
        next((str(x["environment"]) for x in lasts if x.get("environment")), "")
    build = build or str((flags.get("testing.build") if flags else "") or "") or \
        next((str(x["build"]) for x in lasts if x.get("build")), "")
    missing = [n for n, v in (("feature (qa/sources.json)", feature), ("--env", env), ("--build", build)) if not v]
    if kind == "web" and only_existing and not (run_dir / "qa" / "ui" / "plan.json").is_file():
        missing.append("qa/ui/plan.json — no test is written yet; run without --only-existing to write them")
    direct = sources.get("origin") == DIRECT
    if direct and kind in ALL_KINDS:
        missing.append("a Kiwi-direct run has no generated suite: use `ui-agent`")
    if kind == "backend":
        curl = curl or str((flags.get("testing.curl") if flags else "") or "")
        if not curl:
            missing.append("--curl (or testing.curl in config)")
    if missing:
        raise SystemExit(f"execute {kind}: missing {', '.join(missing)}")
    cmd = ["uv", "run", "--project", str(PROJECT), "harness", "run", "testing", "--feature", feature,
           "--run-dir", str(run_dir), "--env", env, "--build", build, "--no-interactive",
           "--stages", ",".join(stages), "--force", "--enable", enable]
    if rfc_run:
        cmd += ["--rfc-run-dir", rfc_run]
    if direct:  # read and record the Kiwi plan as it is
        cmd += ["--enable", "testing.kiwi_direct"]
        if sources.get("kiwi_plan_id"):
            cmd += ["--set", f"testing.kiwi_plan_id={sources['kiwi_plan_id']}"]
    if kind in ("web", "ui-agent") and headed:
        cmd += ["--enable", "testing.ui_headed"]
    if kind == "web" and not only_existing:
        cmd += ["--enable", "testing.ui_only_new"]
    if kind == "backend":
        cmd += ["--curl", str(Path(curl).expanduser())]
    if cases:
        cmd += ["--set", f"testing.cases={cases}"]
    if engine:  # the agents' engine (derive_spec, derive_ui); `/test` inside Axon passes axon
        cmd += ["--engine", engine]
    # Where the browser goes and how it is logged in, for the two browser kinds. Without these the values
    # come from config (testing.base_url_template + --env, testing.ui_auth, testing.agent_hosts).
    if kind in ("web", "ui-agent"):
        if base_url:
            cmd += ["--base-url", base_url]
        if ui_auth:
            cmd += ["--ui-auth", str(Path(ui_auth).expanduser())]
    if kind == "ui-agent" and hosts:
        cmd += ["--set", f"testing.agent_hosts={hosts}"]
    if kind == "ui-agent" and production:
        cmd += ["--enable", "testing.agent_production"]
        if test_account:
            cmd += ["--enable", "testing.agent_test_account"]
    if kind == "ui-agent" and parallel:
        cmd += ["--set", f"testing.agent_parallel={parallel}"]
    if kind == "ui-agent" and read_only:
        cmd += ["--enable", "testing.agent_read_only"]
    if kind == "ui-agent" and browser:
        cmd += ["--set", f"testing.agent_browser={browser}"]
    return cmd


def pending(run_dir: Path) -> dict:
    """{tests, skipped, new, error}: web cases with a test, skipped ones, and CONFIRMED ones with neither —
    the choice between running now (`--only-existing`) and writing the new tests first."""
    import socket

    from workflows.testing.preflight import KIWI_TIMEOUT_S, store_for

    plan = _load(run_dir / "qa" / "ui" / "plan.json")
    tests = [r["extid"] for r in plan.get("tests") or [] if isinstance(r, dict) and r.get("extid")]
    skipped = [r["extid"] for r in plan.get("skipped") or [] if isinstance(r, dict) and r.get("extid")]
    out = {"tests": tests, "skipped": skipped, "new": [], "error": ""}
    feature = _load(run_dir / "qa" / "sources.json").get("feature")
    previous = socket.getdefaulttimeout()
    socket.setdefaulttimeout(KIWI_TIMEOUT_S)
    try:
        confirmed = [c.extid for c in store_for(resolved_flags(load_config()), run_dir).list_cases(
            feature, status="CONFIRMED", tags=("exec:ui", "surface:web"))]
    except Exception as exc:  # report, never raise: the command turns this into a message
        out["error"] = f"{type(exc).__name__}: {exc}"
        return out
    finally:
        socket.setdefaulttimeout(previous)
    out["new"] = [e for e in confirmed if e not in tests and e not in skipped]
    out["not_confirmed_any_more"] = [e for e in tests if e not in confirmed]
    return out


class _Overrides:
    """Resolved flags with a few values replaced — all `store.get_store` reads from them is `.get`."""

    def __init__(self, flags, over: dict) -> None:
        self._flags, self._over = flags, over

    def get(self, name: str, default=None):
        return self._over[name] if name in self._over else self._flags.get(name, default)


def pending_agent(run_dir: Path) -> dict:
    """{will_run, no_expected_result, skipped_backend, error} for `ui-agent`: the CONFIRMED web cases the
    agent will take, those of them with no Expected Result (the agent cannot give a verdict on them), and
    the CONFIRMED `exec:http` cases it leaves to the backend executor. Reads the Kiwi plan the run names."""
    import socket

    from workflows.testing import ui_agent
    from workflows.testing.preflight import KIWI_TIMEOUT_S, store_for

    out = {"will_run": [], "no_expected_result": [], "skipped_backend": 0, "error": ""}
    sources = _load(run_dir / "qa" / "sources.json")
    previous = socket.getdefaulttimeout()
    socket.setdefaulttimeout(KIWI_TIMEOUT_S)
    try:
        flags = resolved_flags(load_config())
        over = {}
        if sources.get("origin") == DIRECT:
            over = {"testing.kiwi_direct": True, "testing.kiwi_plan_id": str(sources.get("kiwi_plan_id") or "")}
        flags = _Overrides(flags, over) if over else flags
        store = store_for(flags, run_dir)
        feature = sources.get("feature")
        manual = bool(flags.get("testing.agent_include_manual", False))
        cases = ui_agent.select_cases(store, feature, include_manual=manual)
        out["no_expected_result"] = [c.extid for c in cases if not ui_agent.expected_result(c.text)]
        out["will_run"] = [c.extid for c in cases if ui_agent.expected_result(c.text)]
        out["skipped_backend"] = len(store.list_cases(feature, status="CONFIRMED", tags=("exec:http",)))
    except Exception as exc:  # report, never raise: the command turns this into a message
        out["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        socket.setdefaulttimeout(previous)
    return out


def _norm_name(text: str) -> str:
    """Lower-case, with runs of spaces, hyphens and underscores as one space: `Cart_Sanity` == `cart-sanity`."""
    return re.sub(r"[\s_-]+", " ", str(text)).strip().lower()


def match_plan(text: str, plans: list[dict]) -> list[dict]:
    """The plans `text` names: a bare number, `TP-3` or `plan 3` by id; else by name, ignoring case and
    separators. An exact name beats a substring, so `cart` is the plan CART and not also CART-SANITY. Every
    candidate comes back when it is ambiguous and [] when none fits — the caller asks, never guesses."""
    by_id = re.fullmatch(r"\s*(?:tp|plan)?[\s_#-]*(\d+)\s*", str(text), re.IGNORECASE)
    if by_id:
        return [p for p in plans if str(p.get("id")) == str(int(by_id.group(1)))]
    want = _norm_name(text)
    if not want:
        return []
    exact = [p for p in plans if _norm_name(p.get("name", "")) == want]
    return exact or [p for p in plans if want in _norm_name(p.get("name", ""))]


def _compact(text: str) -> str:
    return re.sub(r"[\s_-]+", "", str(text)).lower()


def _code(text: str) -> tuple[str, int] | None:
    """`CT002` and `ct2` are the same code: (letters, number). None when it is not letters then digits."""
    m = re.fullmatch(r"([A-Za-z]+)0*(\d+)", str(text).strip())
    return (m.group(1).lower(), int(m.group(2))) if m else None


def match_cases(wanted: list[str], cases: list[dict]) -> tuple[list[str], list[str]]:
    """(case ids in the order asked, tokens that matched nothing or more than one case).

    A token is the case id (any case), the tail of an id (`173` -> `KIWI-173`, `CT-04` or `ct04` ->
    `TC-CT-04`, cut at a hyphen), or the bracketed code that starts a summary (`ct2` -> `[CT002] …`). Zero or
    several matches is never resolved by a guess: the token is returned as unknown.
    """
    matched: list[str] = []
    unknown: list[str] = []
    for raw in wanted:
        token = str(raw).strip()
        if not token:
            continue
        hits: set[str] = set()
        for c in cases:
            cid = str(c.get("id", ""))
            parts = cid.split("-")
            tails = {_compact("".join(parts[i:])) for i in range(1, len(parts))}
            code = re.match(r"\s*\[([^\]]+)\]", str(c.get("summary", "")))
            want_code = _code(token)
            if cid.lower() == token.lower() or _compact(token) in tails or \
                    (code and want_code and _code(code.group(1)) == want_code):
                hits.add(cid)
        if len(hits) == 1:
            (only,) = hits
            if only not in matched:
                matched.append(only)
        elif token not in unknown:
            unknown.append(token)
    return matched, unknown


def lookup(flags, run_dir: Path, plan_id: str = "", store=None) -> dict:
    """The facts `/test-agent:execute` needs to read plain words, as plain data: the Kiwi plans, the cases of
    one plan, the environments, what "prod" means here, and the recorded login sessions by name.

    WHY here and not in the command: which plans exist, which cases a plan has and which session files are on
    disk are facts, and the model must not supply them from memory. Read-only; errors go in `error`, never
    raised. `store` is for tests; otherwise it is built as `pending_agent` builds it. Session files are
    listed by name only — their contents (a login) are never opened.
    """
    import socket

    from workflows.testing.preflight import KIWI_TIMEOUT_S, environments, store_for

    template = str(flags.get("testing.base_url_template") or "")
    out = {"plans": [], "cases": [],
           "environments": [e["env"] for e in environments(flags)],
           "base_url_template": template,
           "production": {"env": "prod", "base_url": str(flags.get("testing.production_base_url") or "")},
           "sessions": [], "defaults": {"env": str(flags.get("testing.env") or ""),
                                        "build": str(flags.get("testing.build") or ""),
                                        "browser": str(flags.get("testing.agent_browser") or "")},
           "today": date.today().isoformat(), "error": ""}
    folder = Path(str(flags.get("testing.sessions_dir") or "")).expanduser()
    try:  # names and paths only
        out["sessions"] = [{"name": f.stem, "path": str(f)} for f in sorted(folder.glob("*.json")) if f.is_file()]
    except OSError:
        pass
    if store is None and str(flags.get("testing.storage") or "json") == "json":
        out["error"] = "the case store is JSON (testing.storage=json): there are no Kiwi plans to list"
        return out
    previous = socket.getdefaulttimeout()
    socket.setdefaulttimeout(KIWI_TIMEOUT_S)
    try:
        if plan_id and not str(plan_id).isdigit():
            raise ValueError(f"plan id {plan_id!r} is not a number")
        plan_store = store if store is not None else store_for(flags, run_dir)
        rpc = plan_store._rpc()
        products = {p["id"]: p.get("name", "") for p in rpc.Product.filter({})}
        out["plans"] = [{"id": p["id"], "name": p.get("name", ""), "product": products.get(p.get("product"), "")}
                        for p in rpc.TestPlan.filter({})]
        if plan_id:
            case_store = store
            if case_store is None:
                case_store = store_for(_Overrides(flags, {"testing.kiwi_direct": True,
                                                          "testing.kiwi_plan_id": str(plan_id)}), run_dir)
            out["cases"] = [{"id": c.extid, "summary": c.summary, "status": c.status}
                            for c in case_store.list_cases("")]
    except Exception as exc:  # report, never raise: the command turns this into a message
        out["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        socket.setdefaulttimeout(previous)
    return out


def confirm_production(production: bool, test_account: bool, *, logged_in: bool, base_url: str,
                       ask=input, interactive: bool | None = None) -> bool:
    """Whether the run may go ahead as a logged-in TEST account on production -> the confirmed answer.

    Production is the operator's call, so it is one flag (`--production`). What the flag cannot know is
    whose account the saved session is: on a customer's account the agent's cart changes are someone
    else's cart. So a logged-in production run asks once — at a terminal, here; from the chat command, the
    command asks and passes `--test-account`. A guest run has no account and is not asked.
    """
    if not production or not logged_in:
        return False
    if test_account:
        return True
    if interactive is None:
        interactive = sys.stdin.isatty()
    if not interactive:
        raise SystemExit("execute ui-agent --production: this run is logged in. Confirm the session is a TEST "
                         "account, not a customer's, by adding --test-account — or drop --ui-auth to run as a "
                         "guest.")
    answer = ask(f"This will drive PRODUCTION ({base_url}) logged in. The agent adds to the cart and changes "
                 f"what the account sees. Is the session a TEST account? [y/N] ")
    if answer.strip().lower() not in ("y", "yes"):
        raise SystemExit("execute ui-agent --production: not confirmed as a test account — nothing was run.")
    return True


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="execute")
    ap.add_argument("kind", nargs="?", default="web", choices=(*KINDS, "all"))
    ap.add_argument("--run-dir")
    ap.add_argument("--env", default="")
    ap.add_argument("--build", default="")
    ap.add_argument("--headed", action="store_true")
    ap.add_argument("--cases", default="", help="comma-separated case ids (backend: narrows derive_spec)")
    ap.add_argument("--curl", default="")
    ap.add_argument("--base-url", default="", help="web / ui-agent: the web app's origin on the environment")
    ap.add_argument("--ui-auth", default="",
                    help="web / ui-agent: a Playwright storage-state file with a logged-in session")
    ap.add_argument("--production", action="store_true",
                    help="ui-agent: the base address is PRODUCTION. With a logged-in session you are asked to "
                         "confirm it is a test account")
    ap.add_argument("--test-account", action="store_true",
                    help="ui-agent with --production: I confirm the --ui-auth session is a TEST account, not a "
                         "customer's (answers the question in advance, for unattended use)")
    ap.add_argument("--parallel", type=int, default=0,
                    help="ui-agent: cases to run at once, one browser each (default 5; always 1 with --headed)")
    ap.add_argument("--read-only", action="store_true",
                    help="ui-agent: the agent may look, search, sort and filter, and change nothing")
    ap.add_argument("--browser", default="",
                    help="ui-agent: the browser Playwright MCP drives (chrome, msedge, …); default its own "
                         "Chromium build")
    ap.add_argument("--hosts", default="",
                    help="ui-agent: hosts the agent may drive, comma-separated (testing.agent_hosts); never "
                         "production")
    ap.add_argument("--engine", default="", help="engine for the agent stages (default: the harness's)")
    ap.add_argument("--feature", help="run a Kiwi plan with no earlier run: the run folder is created (or reused) "
                                      "for this feature")
    ap.add_argument("--kiwi-plan", default="", help="the Kiwi test plan id to read cases from (needs --feature)")
    ap.add_argument("--print", action="store_true", dest="print_only")
    ap.add_argument("--only-existing", action="store_true",
                    help="web: run only the tests already written — no derive_ui, no model, starts now")
    ap.add_argument("--pending", action="store_true",
                    help="print (JSON) which CONFIRMED web cases have a test and which do not, then exit")
    ap.add_argument("--lookup", action="store_true",
                    help="print (JSON) the Kiwi plans, the environments, what prod means and the recorded "
                         "login sessions by name (with --plan: that plan's cases), then exit")
    ap.add_argument("--resolve-cases", default="", metavar="TOKENS",
                    help="with --plan: print (JSON) which case ids words like '173, ct2' mean, then exit")
    ap.add_argument("--plan", default="", help="--lookup / --resolve-cases: the Kiwi test plan id")
    args = ap.parse_args(argv)
    cfg = load_config()
    if args.lookup or args.resolve_cases:
        run_dir = (Path(args.run_dir).expanduser() if args.run_dir else latest_run(cfg)) or runs_root(cfg)
        found = lookup(resolved_flags(cfg), run_dir, args.plan)
        if args.lookup:
            print(json.dumps(found, indent=2))
        else:
            tokens = [t for t in re.split(r"[,\s]+", args.resolve_cases) if t]
            matched, unknown = match_cases(tokens, found["cases"]) if args.plan else ([], tokens)
            error = found["error"] or ("" if args.plan else "--resolve-cases needs --plan")
            print(json.dumps({"matched": matched, "unknown": unknown, "error": error}, indent=2))
        return 0
    if args.run_dir:
        run_dir = Path(args.run_dir).expanduser()
    elif args.feature:
        run_dir = bootstrap_run(runs_root(cfg), args.feature, args.kiwi_plan)
    elif args.kiwi_plan:
        raise SystemExit("execute: --kiwi-plan needs --feature (it names the run folder and the Kiwi run)")
    else:
        run_dir = latest_run(cfg)
    if run_dir is None or not run_dir.is_dir():
        raise SystemExit("execute: no testing run found — pass --run-dir")
    if args.pending and args.kind == "ui-agent":
        print(json.dumps({"run_dir": str(run_dir), "kind": "ui-agent", **pending_agent(run_dir)}, indent=2))
        return 0
    if args.pending:
        print(json.dumps({"run_dir": str(run_dir), **pending(run_dir)}, indent=2))
        return 0
    flags = resolved_flags(cfg)
    test_account = confirm_production(args.production, args.test_account,
                                      logged_in=bool(args.ui_auth or (flags.get("testing.ui_auth") if flags else "")),
                                      base_url=args.base_url or "the configured address")
    code = 0
    for kind in (ALL_KINDS if args.kind == "all" else (args.kind,)):
        cmd = command(kind, run_dir, env=args.env, build=args.build, headed=args.headed, cases=args.cases,
                      curl=args.curl, flags=flags, only_existing=args.only_existing,
                      engine=args.engine, base_url=args.base_url, ui_auth=args.ui_auth, hosts=args.hosts,
                      browser=args.browser, read_only=args.read_only,
                      parallel=args.parallel, production=args.production, test_account=test_account)
        print(" ".join(shlex.quote(c) for c in cmd), flush=True)
        if not args.print_only:
            log = run_dir / "logs" / f"run.{kind}.out"  # one log per launch, as /test-agent:test writes them
            log.parent.mkdir(exist_ok=True)
            with log.open("w") as out:
                code = subprocess.run(cmd, stdout=out, stderr=subprocess.STDOUT).returncode
            print(f"{kind}: exit {code} (log: {log})", flush=True)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
