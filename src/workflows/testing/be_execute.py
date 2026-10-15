"""BE test executor — runs cases against a live service and classifies each outcome.

Ported from test_agent tools/be_execute.py @ ca7d4ce. `run_case`, `parse_curl` and `dig` are unchanged in
behaviour — tests/testing/test_be_execute.py holds the same 17 assertions as the original script. What
changed: the curl binary and the request timeout are flags rather than literals, the Kiwi write side moved
behind `CaseStore`, and the run loop lives in `stages/execute_be` (which also persists `qa/results.json`,
something test_agent never wrote — stdout was the only local record).

The envelope (host, auth, geo, platform, version headers) comes from a captured curl rather than being
transcribed field by field. That was a measured decision: a hand-trimmed cookie jar returned 403 Bad CSRF
Token where the full captured one returned 200, and the difference was not obvious by inspection.

Assertions come from the CONTRACT, never from the implementation — an agent that reads the handler to
decide what correct means writes tests that always pass.

The classification rule the whole module exists to protect: **a FAIL must mean a defect.** Transport
problems are ERROR or BLOCKED; only a held-and-broken assertion is a FAIL.
"""

from __future__ import annotations

import json
import re
import shlex
import subprocess
import time

#: Default curl binary / timeout. Overridden by flags `testing.curl_path` / `testing.timeout_s`.
DEFAULT_CURL = "/usr/bin/curl"
DEFAULT_TIMEOUT_S = 30
#: subprocess gets this much slack over the curl -m timeout so curl's own timeout wins the race.
SUBPROCESS_SLACK_S = 10


def parse_curl(text: str) -> dict:
    """Pull the reusable envelope out of a captured curl: url, method, headers, cookies."""
    text = text.strip().replace("\\\n", " ").replace("\\\r\n", " ")
    try:
        parts = shlex.split(text)
    except ValueError:
        parts = text.split()
    env = {"url": None, "method": None, "headers": {}, "cookie": None, "body": None}
    i = 0
    while i < len(parts):
        p = parts[i]
        if p in ("-H", "--header") and i + 1 < len(parts):
            k, _, v = parts[i + 1].partition(":")
            env["headers"][k.strip().lower()] = v.strip()
            i += 2
        elif p in ("-b", "--cookie") and i + 1 < len(parts):
            env["cookie"] = parts[i + 1]
            i += 2
        elif p in ("-X", "--request") and i + 1 < len(parts):
            env["method"] = parts[i + 1]
            i += 2
        elif p in ("--data", "--data-raw", "-d") and i + 1 < len(parts):
            env["body"] = parts[i + 1]
            env["method"] = env["method"] or "POST"
            i += 2
        elif p.startswith("http"):
            env["url"] = p
            i += 1
        else:
            i += 1
    if env["cookie"] is None:
        env["cookie"] = env["headers"].pop("cookie", None)
    env["method"] = env["method"] or "GET"
    m = re.match(r"(https?://[^/]+)", env["url"] or "")
    env["origin"] = m.group(1) if m else None
    return env


def dig(obj, path: str):
    """data.mix_panel_data.labs_careplan_variant -> value, or a ("__MISSING__", path) miss marker."""
    cur = obj
    for seg in path.split("."):
        if isinstance(cur, list):
            try:
                cur = cur[int(seg)]
                continue
            except (ValueError, IndexError):
                return ("__MISSING__", path)
        if not isinstance(cur, dict) or seg not in cur:
            return ("__MISSING__", path)
        cur = cur[seg]
    return cur


def run_case(case: dict, env: dict, timeout: int = DEFAULT_TIMEOUT_S, curl_path: str = DEFAULT_CURL) -> dict:
    """Issue one request and evaluate its assertions. Never raises: classifies instead."""
    url = env["origin"] + case["path"] if case["path"].startswith("/") else case["path"]
    cmd = [curl_path, "-s", "-o", "-", "-w", "\n__STATUS__%{http_code}",
           "-m", str(timeout), "-X", case.get("method", env["method"]), url]
    for k, v in env["headers"].items():
        if k in ("content-length",):
            continue
        cmd += ["-H", f"{k}: {v}"]
    if env["cookie"]:
        cmd += ["-b", env["cookie"]]
    body = case.get("body", env.get("body"))
    if body:
        cmd += ["--data-raw", body]

    t0 = time.time()
    try:
        out = subprocess.run(cmd, capture_output=True, text=True,
                             timeout=timeout + SUBPROCESS_SLACK_S).stdout
    except subprocess.TimeoutExpired:
        return {"status": "ERROR", "detail": "request timed out", "elapsed": time.time() - t0}
    elapsed = round(time.time() - t0, 2)

    raw, _, code = out.rpartition("\n__STATUS__")
    code = (code or "0").strip()
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        payload = None

    # transport-level problems are ERROR/BLOCKED, never FAIL — a FAIL must mean a defect
    if code == "0":
        return {"status": "ERROR", "detail": "no response", "elapsed": elapsed}
    if code in ("401", "403"):
        return {"status": "BLOCKED", "detail": f"HTTP {code} - session or CSRF rejected; recapture the curl",
                "elapsed": elapsed, "http": code}
    if code == "404":
        return {"status": "BLOCKED", "detail": f"HTTP 404 - {case['path']} not reachable at this layer",
                "elapsed": elapsed, "http": code}
    # A 400 the case did not ask for almost always means the request was wrong — a missing required
    # parameter, the wrong identity — not that the service is broken. Measured 2026-08-26: the CP contract
    # documents "unchanged path/params" without listing them, so derived cases omitted types/type and four
    # came back FAIL against a service that was healthy enough to return 3 PASSes on the same endpoint
    # seconds later.
    if code == "400" and str(case.get("expect_status", "")) != "400":
        return {"status": "BLOCKED", "elapsed": elapsed, "http": code, "payload": payload,
                "detail": "HTTP 400 - the service rejected the request; check the parameters "
                          "and identity before suspecting the code"}
    if code == "429":
        return {"status": "BLOCKED", "detail": "HTTP 429 - rate limited; back off",
                "elapsed": elapsed, "http": code}
    # 502/503/504 mean the edge could not reach the origin. That is the environment, not the code, and
    # calling it FAIL is how a suite starts lying: measured 2026-08-21, a Cloudflare 503 was first
    # reported as two defects.
    if code in ("502", "503", "504"):
        return {"status": "ERROR", "detail": f"HTTP {code} - upstream unavailable (edge error, not a defect)",
                "elapsed": elapsed, "http": code}
    if payload is None:
        return {"status": "ERROR", "detail": f"HTTP {code} but body is not JSON - infrastructure page, not the API",
                "elapsed": elapsed, "http": code}

    # A pattern that will not compile cannot judge anything. The case is unusable, which is ERROR — the
    # fault is in the spec, not the service — and reporting it that way keeps the rest of the suite
    # running. Measured 2026-08-26: one mis-transcribed pattern (`^https:\`) raised re.error mid-suite and
    # took the whole run down with it, leaving 18 of 25 cases IDLE after 7 had already passed.
    for a in (*case.get("require", []), *case.get("assert", [])):
        if "matches" not in a:
            continue
        try:
            re.compile(a["matches"])
        except re.error as exc:
            return {"status": "ERROR", "http": code, "elapsed": elapsed, "payload": payload,
                    "detail": f"{a['path']}: /{a['matches']}/ is not a valid regular "
                              f"expression ({exc}) - the case's pattern was not "
                              f"transcribed correctly"}

    # Preconditions first. A case whose precondition is unmet is BLOCKED, never FAIL or PASS: it did not
    # test what it claims. Measured 2026-08-21 — omitting x-client made the experiment report disabled, so
    # the endpoint returned 200 with an empty cohort and a status-only assertion would have passed while
    # verifying the wrong branch.
    for req in case.get("require", []):
        got = dig(payload, req["path"]) if payload is not None else ("__MISSING__", req["path"])
        missing = isinstance(got, tuple) and got and got[0] == "__MISSING__"
        bad = (missing
               or (req.get("non_empty") and got in (None, "", [], {}))
               or ("equals" in req and got != req["equals"])
               or ("matches" in req and not re.search(req["matches"], str(got))))
        if bad:
            shown = "missing" if missing else repr(got)
            return {"status": "BLOCKED", "http": code, "elapsed": elapsed, "payload": payload,
                    "detail": f"precondition not met: {req['path']} = {shown}"
                              f"{' - ' + req['why'] if req.get('why') else ''}"}

    fails = []
    if "expect_status" in case and code != str(case["expect_status"]):
        fails.append(f"status: expected {case['expect_status']}, got {code}")
    for a in case.get("assert", []):
        got = dig(payload, a["path"]) if payload is not None else ("__MISSING__", a["path"])
        if isinstance(got, tuple) and got and got[0] == "__MISSING__":
            # `equals: null` is how a case says "this must NOT be there" — the fourth slot in a
            # three-slot response, a second department that should not exist. Absent satisfies it.
            # Failing here instead would report a defect for behaving exactly as specified, which is the
            # same lie as a false PASS, pointing the other way.
            if "equals" in a and a["equals"] is None:
                continue
            parent = a["path"].rsplit(".", 1)[0] if "." in a["path"] else ""
            sib = dig(payload, parent) if parent else payload
            avail = ", ".join(list(sib)[:8]) if isinstance(sib, dict) else "n/a"
            fails.append(f"{a['path']}: key not in response (contract drift? present at "
                         f"{parent or 'root'}: {avail})")
            continue
        if "equals" in a and got != a["equals"]:
            fails.append(f"{a['path']}: expected {a['equals']!r}, got {got!r}")
        if a.get("present") and (got is None or got == ""):
            fails.append(f"{a['path']}: present but empty")
        if "matches" in a and not re.search(a["matches"], str(got)):
            fails.append(f"{a['path']}: {got!r} does not match /{a['matches']}/")

    return {"status": "FAIL" if fails else "PASS",
            "detail": "; ".join(fails) or "all assertions held",
            "elapsed": elapsed, "http": code, "payload": payload}


def preflight(spec: list[dict], env: dict, timeout: int = DEFAULT_TIMEOUT_S,
              curl_path: str = DEFAULT_CURL) -> dict:
    """One real request against the first entry's path, to fail fast on a dead environment.

    Note (unchanged from test_agent): BLOCKED and 404 PASS preflight — only ERROR is fatal. The point is
    "can we reach anything at all", not "is this route right".
    """
    return run_case({"extid": "__preflight__", "path": spec[0]["path"],
                     "method": spec[0].get("method", "GET"), "body": spec[0].get("body")},
                    env, timeout=timeout, curl_path=curl_path)
