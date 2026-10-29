#!/usr/bin/env python3
"""A fake `uv` for the /rfc extension test: `uv run --project P <cmd…>`.

  … python -m rfc_agent.axon_agent doctor   prints READY (FAKE_UV_DOCTOR_FAIL=1: a FAIL line, exit 1)
  … python -m rfc_agent.scope run-dir …     prints $FAKE_UV_RUN_DIR
  … rfc <args>                              plays a short pipeline: Scope selection, Investigate pause 1, Review
                                            triage, done. Each pause writes a proposal, prints the WAITING line and
                                            waits for the answers, which it copies to <run>/got-<name>.json.
"""

import json
import os
import sys
import time
from pathlib import Path


def wait(path: Path, timeout=30):
    t0 = time.time()
    while not path.exists():
        if time.time() - t0 > timeout:
            print(f"  [fake] no answers at {path}", file=sys.stderr, flush=True)
            sys.exit(3)
        time.sleep(0.2)
    time.sleep(0.1)
    return json.loads(path.read_text())


def pause(run: Path, name: str, line: str, proposal: dict) -> dict:
    conf = run / "confirm"
    conf.mkdir(parents=True, exist_ok=True)
    prop, ans = conf / f"{name}.proposal.json", conf / f"{name}.answers.json"
    if name == "scope":
        prop, ans = conf / "proposal.json", conf / "answers.json"
    prop.write_text(json.dumps(proposal))
    print(f"  {line}: proposal at {prop} — answers expected at {ans}", file=sys.stderr, flush=True)
    got = wait(ans)
    (run / f"got-{name}.json").write_text(json.dumps(got))
    return got


def main() -> int:
    args = sys.argv[1:]
    assert args[:2] == ["run", "--project"], args
    cmd = args[3:]
    if cmd[:3] == ["python", "-m", "rfc_agent.axon_agent"]:
        if os.environ.get("FAKE_UV_DOCTOR_FAIL"):
            print("FAIL  axon: not signed in — run `axon` and log in\naxon: BLOCKED (1 problem(s))")
            return 1
        print("axon: READY")
        return 0
    if cmd[:3] == ["python", "-m", "rfc_agent.scope"]:
        print(os.environ["FAKE_UV_RUN_DIR"])
        return 0
    assert cmd[0] == "rfc", cmd
    run = Path(cmd[cmd.index("--run-dir") + 1])
    (run / "argv.json").write_text(json.dumps({"argv": cmd[1:], "pi_env": sorted(k for k in os.environ if k.startswith("PI_"))}))
    logs = run / "logs"
    err = sys.stderr
    print(f"  [rfc] run dir: {run}", file=err, flush=True)
    print("  [rfc] ── Scope — PRD, repos, snapshots ──", file=err, flush=True)
    pause(run, "scope", "[scope] WAITING FOR SELECTION", {"header": "Context layer ok — 2 repos proposed", "items": [
        {"n": 1, "kind": "repo", "key": "svc_a", "label": "svc_a", "note": "main, local", "include": True},
        {"n": 2, "kind": "repo", "key": "svc_b", "label": "svc_b", "note": "main, cached", "include": True},
        {"n": 3, "kind": "page", "key": "123", "label": "Design doc", "note": "", "include": False},
    ]})
    print("  [rfc] ── Investigate — plan, trace, integrate ──", file=err, flush=True)
    with open(logs / "progress.jsonl", "a") as f:
        f.write(json.dumps({"kind": "1a_done", "text": "Plan: 2 flows, 2 tracers"}) + "\n")
    with open(logs / "activity.log", "a") as f:
        f.write("10:00:00  investigate lead ▶ started · session x · card investigate.lead.plan\n")
    pause(run, "investigate-pause1", "[investigate] WAITING FOR PAUSE 1", {"header": "Plan: 2 flows", "items": [
        {"n": 1, "kind": "retry", "key": "svc_c", "label": "svc_c", "note": "no snapshot", "include": False},
        {"n": 2, "kind": "question", "key": "Q1", "label": "Who owns cart?", "note": "", "include": False,
         "question": {"question": "Who owns cart?", "blocks": "F1", "suggested_answer": "cart team"}},
    ]})
    print("  [rfc] ── Review ──", file=err, flush=True)
    pause(run, "investigate-pause3", "[investigate] WAITING FOR PAUSE 3", {"header": "2 findings", "items": [
        {"n": 1, "kind": "finding", "key": "R-F1.2", "label": "BLOCKER wrong table", "note": "", "include": True},
        {"n": 2, "kind": "finding", "key": "R-X.1", "label": "MINOR wording", "note": "", "include": False},
    ]})
    print("\n  [rfc] done in 1 min, $0.10\n  [rfc] read:  " + str(run / "out/rfc.readable.md")
          + "\n  [rfc] full:  " + str(run / "out/rfc.md"), file=err, flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
