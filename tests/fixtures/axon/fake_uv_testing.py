#!/usr/bin/env python3
"""A fake `uv` for the /test extension test: `uv run --project P <cmd…>`. Every call is appended to
$FAKE_UV_LOG (one JSON argv per line). The run folder is $FAKE_RUN_DIR; its `fake-state.json` says which
launches have finished, and `status` answers from it.

  … python -m rfc_agent.axon_agent doctor        READY
  … python -m workflows.testing.preflight …      runs (none), recent, features, check, envs, setup-web
  … python -m workflows.testing.status R …       --milestone S → one line; --json → next step from the state
  … python -m workflows.testing.progress R --watch --from N   tails logs/activity.log from byte N
  … python -m workflows.testing.execute …        --pending, --print, or a run (records an `execute` launch)
  … python -m workflows.testing.evaluation …     --print, or a run (writes an eval)
  … harness run testing …                        writes STAGE started / gate PASSED lines, records the launch
"""

import json
import os
import sys
import time
from pathlib import Path

RUN = Path(os.environ["FAKE_RUN_DIR"])
RFC = os.environ.get("FAKE_RFC_DIR", "/rfc/prd-1")
STATE = RUN / "fake-state.json"


def state() -> dict:
    try:
        return {"done": [], **json.loads(STATE.read_text())}
    except Exception:
        return {"done": []}


def record(kind: str) -> None:
    s = state()
    s["done"].append(kind)
    STATE.write_text(json.dumps(s))


def activity(stage: str, text: str) -> None:
    logs = RUN / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    with open(logs / "activity.log", "a") as f:
        f.write(f"[{time.strftime('%H:%M:%S')}] [{stage}] {text}\n")


def play(stages):
    for st in stages:
        activity(st, "STAGE started")
        time.sleep(0.3)
        s = state()  # the stage's outputs exist before its gate line, as in a real run
        s.setdefault("stages", []).append(st)
        STATE.write_text(json.dumps(s))
        activity(st, "gate PASSED (0 failures)")
        time.sleep(0.2)


def status_json(kiwi: bool) -> dict:
    done = state()["done"]
    out = {"run_dir": str(RUN), "feature": "HDPO", "stages": {"generate_cases": {"status": "ok", "gate_passed": True, "duration_s": 600}},
           "cases": {"total": 12, "modules": 3, "execution": {"http": 4, "ui": 3, "manual": 5}, "priority": {"High": 6, "Low": 6}},
           "eval": None, "store": {"total": 12, "product": "Sandbox / HDPO", "plan_url": "https://kiwi/plan/9/", "protected": 0},
           "prerequisites": {"services": ["svc_a"], "counts": {"fixtures": 2}, "secrets": [], "fixtures_unknown_how": 1,
                             "top_blockers": [{"id": "FX-01", "entity": "user", "used_by": 9}]},
           "test_data_supplied": False, "xlsx": str(RUN / "qa/HDPO.xlsx"), "duration_s": 700, "cost_usd": 3.2,
           "results": None, "results_web": None, "results_app": None, "ui_plan": None, "next": {"step": "start"}}
    if "generate" in done:
        out["next"] = {"step": "review"}
    if "web" in done or "execute" in done:
        out["next"] = {"step": "done"}
        out["results_web"] = {"tally": {"PASS": 2, "FAIL": 1}, "run_url": "https://kiwi/runs/3/", "html_report": str(RUN / "qa/ui/html-report"),
                              "not_passing": [{"extid": "TC-3", "status": "FAIL", "detail": "Error: locator not found\nmore"}]}
    if "agent" in done:
        out["results_agent"] = {"tally": {"PASS": 1, "FAIL": 1}, "parts_met": 3, "parts": 4, "run_url": "https://kiwi/runs/4/",
                                "table": "| Case | Verdict |\n|---|---|\n| TC-1 | PASS |", "not_executed": ["TC-7"],
                                "screenshots": [{"extid": "TC-2", "path": "/shots/tc2.png"}]}
    if "derive_ui" in state().get("stages", []):
        out["ui_plan"] = {"skipped": [{"extid": "TC-9", "reason": "feature not in the snapshot"}]}
    if "evaluate" in done:
        out["eval"] = {"overall": 3.8, "coverage": 3.6, "quality": 4.0, "compliance": "FAIL", "failed_checks": ["boundary_coverage"],
                       "missing_scenarios": 7}
    if kiwi:
        out["kiwi"] = {"by_status": {"CONFIRMED": {"ui": 3, "http": 1}, "PROPOSED": {"manual": 5}}, "confirmed_http": 1, "http_total": 4,
                       "confirmed_web_ui": 3, "confirmed_app_ui": 0, "stale": {}}
    return out


def main() -> int:
    args = sys.argv[1:]
    assert args[:2] == ["run", "--project"], args
    cmd = args[3:]
    with open(os.environ["FAKE_UV_LOG"], "a") as f:
        f.write(json.dumps({"argv": cmd, "pi_env": sorted(k for k in os.environ if k.startswith("PI_"))}) + "\n")
    if cmd[:3] == ["python", "-m", "rfc_agent.axon_agent"]:
        print("axon: READY")
        return 0
    if cmd[:2] == ["python", "-m"]:
        mod, rest = cmd[2].rsplit(".", 1)[-1], cmd[3:]
        if mod == "preflight":
            sub = rest[0]
            if sub == "runs":
                print("[]")
            elif sub == "recent":
                print(json.dumps([{"path": RFC, "name": "prd-1", "title": "RFC: HDPO"}]))
            elif sub == "features":
                print(json.dumps([{"key": "HDPO", "why": "the Jira key"}]))
            elif sub == "envs":
                print(json.dumps([{"env": "jupiter", "base_url": "https://jupiter.example.com"}]))
            elif sub == "check":
                print(json.dumps({"ok": True, "checks": [
                    {"id": "engine", "level": "info", "msg": "agents run on axon", "fix": ""},
                    {"id": "kiwi", "level": "ok", "msg": "Kiwi reachable", "fix": ""},
                    {"id": "test_data", "level": "warn", "msg": "Test data: not supplied", "fix": "pass --test-data"},
                    {"id": "ui_login", "level": "ok", "msg": "test-account login file", "fix": ""}],
                    "resolved": {"feature": "HDPO", "run_dir": str(RUN), "ui_login": "/secret/login.json", "existing_runs": []}}))
            elif sub == "setup-web":
                print("stored")
            return 0
        if mod == "status":
            if "--milestone" in rest:
                print(f"{rest[rest.index('--milestone') + 1]}: fine (1m, $0.10)")
            else:
                print(json.dumps(status_json("--kiwi" in rest)))
            return 0
        if mod == "progress":
            if "--now" in rest:
                print("idle")
                return 0
            log = Path(rest[0]) / "logs" / "activity.log"
            offset = int(rest[rest.index("--from") + 1])
            t0 = time.time()
            while time.time() - t0 < 60:
                if log.exists() and log.stat().st_size > offset:
                    with open(log, "rb") as f:
                        f.seek(offset)
                        data = f.read()
                    offset += len(data)
                    sys.stdout.write(data.decode())
                    sys.stdout.flush()
                time.sleep(0.1)
            return 0
        if mod == "execute":
            agent_kind = next((k for k in ("ui-agent", "android") if k in rest), "")
            if agent_kind and "--pending" in rest:
                out = {"run_dir": str(RUN), "kind": agent_kind, "error": ""}
                if agent_kind == "ui-agent":
                    out |= {"will_run": ["TC-1", "TC-2"], "no_expected_result": ["TC-7"], "needs_allow": {"coupons": ["TC-2"]},
                            "needs_session": [], "manual_left": [] if "--include-manual" in rest else ["TC-8"]}
                if agent_kind == "android" or "web" not in rest[rest.index("--platform") + 1:][:1]:
                    out["android"] = {"by_flow": [{"extid": "TC-4", "flow": "cart"}], "by_agent": [{"extid": "TC-5", "why": "no flow"}],
                                      "not_run": [{"extid": "TC-6", "why": "gated flow"}], "manual_left": [], "error": "",
                                      "untagged": [] if "--untagged" in rest else ["KIWI-9"], "both": []}
                print(json.dumps(out))
                return 0
            if agent_kind:
                print(f"uv run --project P harness run testing --feature HDPO --run-dir {RUN} --env jupiter --build b1 "
                      f"--no-interactive --stages execute_ui_agent --force --enable testing.execute_ui_agent")
                if "--print" in rest:
                    return 0
                play(["execute_ui_agent"])
                record("agent")
                return 0
            if "--pending" in rest:
                print(json.dumps({"run_dir": str(RUN), "tests": ["TC-1", "TC-2"], "skipped": [], "new": ["TC-3"], "error": ""}))
                return 0
            line = (f"uv run --project P harness run testing --feature HDPO --run-dir {RUN} --env jupiter --build b1 "
                    f"--no-interactive --stages execute_web --force --enable testing.execute_web")
            print(line)
            if "--print" in rest:
                return 0
            play(["execute_web"])
            record("execute")
            return 0
        if mod == "evaluation":
            print(f"uv run --project P harness run testing --feature HDPO --run-dir {RUN} --no-interactive --stages evaluate")
            if "--print" in rest:
                return 0
            play(["evaluate"])
            (RUN / "qa").mkdir(exist_ok=True)
            (RUN / "qa" / "eval.json").write_text(json.dumps({"compliance_checks": {"boundary_coverage": {"violations": [1, 2]}}}))
            record("evaluate")
            return 0
    if cmd[:3] == ["harness", "run", "testing"]:
        stages = cmd[cmd.index("--stages") + 1].split(",") if "--stages" in cmd else ["sources", "generate_cases", "store_cases"]
        play(stages)
        kind = "generate" if "--stages" not in cmd else ("web" if "execute_web" in stages else "backend" if "execute_be" in stages else "other")
        record(kind)
        return 0
    print(f"fake uv: unknown {cmd}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
