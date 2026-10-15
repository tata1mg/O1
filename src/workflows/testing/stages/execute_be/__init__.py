"""`execute_be` — the run loop, with a results file.

Ported from test_agent tools/be_execute.py:235-335. Behaviour preserved, including the order of gates and
the exit semantics, which are mapped onto the harness's typed failures:

  * exit 3, preflight ERROR -> `EngineFailure` ("no run created and nothing recorded")
  * exit 4, nothing runnable -> `GateFailure` (the suite is wrong, not the machine)

Two additions. `qa/results.json` records every case's outcome locally — test_agent wrote results only into
Kiwi and printed them to stdout, so a run whose terminal scrolled away left no local evidence at all
(survey §9). And `testing.offline` refuses to make any request: test_agent's `--dry-run` still derived
from Kiwi and still fired a live preflight (be_execute.py:256-284), so there was no parse-and-validate
mode.

OFF by default (`testing.execute`): this stage issues real requests against a live service with a captured
identity. Opt in with `--enable testing.execute` or `--stages execute_be`.
"""

from __future__ import annotations

from pathlib import Path

from harness.contracts import EngineFailure, GateFailure, RunContext, Stage

from ... import be_execute, route_map
from ...store import get_store
from .. import feature_of, python_stage, read_json, read_json_gated, write_json

RESULTS_OUT = "qa/results.json"


@python_stage
def _run(stage: Stage, ctx: RunContext) -> str:
    from rfc_agent import decisions

    feature = feature_of(ctx)
    exec_tag = str(ctx.flag("testing.exec_tag", "exec:http"))
    dry_run = bool(ctx.flag("testing.dry_run", False))
    offline = bool(ctx.flag("testing.offline", False))
    timeout = int(ctx.flag("testing.timeout_s", be_execute.DEFAULT_TIMEOUT_S))
    curl_path = str(ctx.flag("testing.curl_path", be_execute.DEFAULT_CURL))
    cohort_path = str(ctx.flag("testing.cohort_path", "") or "")

    spec = read_json(ctx.path("qa/spec.json"), []) or []
    if not spec:
        raise GateFailure("qa/spec.json is empty — run derive_spec first, or fix the cases it dropped.")

    curl_file = ctx.inputs.get("curl") or ctx.flag("testing.curl", "")
    if not curl_file:
        raise EngineFailure(
            "execute_be needs a captured curl for the request envelope (host, auth, headers): "
            "pass --curl <file>. Capture it from the browser's network tab against the target host."
        )
    curl_text = Path(str(curl_file)).expanduser().read_text()
    env = be_execute.parse_curl(curl_text)
    if not env.get("origin"):
        raise EngineFailure(f"no http(s) URL found in {curl_file} — is it a captured curl command?")

    # Route rewriting: contract path -> deployed path.
    spec, rewrites = route_map.apply_route_map(
        spec, path=(ctx.inputs.get("route_map") or ctx.flag("testing.route_map", "") or None))
    for extid, frm, to in rewrites:
        decisions.record(ctx, stage.name, "route_rewritten",
                         why="the contract states the in-service path; the edge exposes another. The map "
                             "is explicit and reviewed, never inferred.",
                         extid=extid, **{"from": frm, "to": to})

    env_name = str(ctx.inputs.get("env") or ctx.flag("testing.env", "") or "")
    build = str(ctx.inputs.get("build") or ctx.flag("testing.build", "") or "")
    header = {
        "feature": feature, "environment": env_name, "build": build, "origin": env["origin"],
        "headers": len(env["headers"]), "cookie": bool(env["cookie"]),
        "cases": len(spec), "dry_run": dry_run, "offline": offline,
        "rewrites": [{"extid": e, "from": f, "to": t} for e, f, t in rewrites],
    }
    if offline:
        # Parse-and-validate only: the spec was read, the envelope parsed, the routes rewritten, and no
        # socket was opened.
        write_json(ctx.path(RESULTS_OUT),
                   {**header, "results": [], "tally": {},
                    "note": "testing.offline — validated the spec and the envelope, issued no requests"})
        return f"offline: validated {len(spec)} entries, issued no requests"

    if not env_name or not build:
        raise EngineFailure("execute_be needs --env and --build: they name the run and are recorded on "
                            "every execution property.")

    # Preflight: one real request against the first entry. BLOCKED and 404 pass — the question is
    # "can we reach anything at all", not "is this route right".
    probe = be_execute.preflight(spec, env, timeout=timeout, curl_path=curl_path)
    if probe["status"] == "ERROR":
        raise EngineFailure(
            f"preflight failed: {probe['detail']}. No run was created and nothing was recorded. "
            f"Fix the environment (VPN, host reachability, a fresh curl), then re-run the same command."
        )

    store = None if dry_run else get_store(ctx)
    run = None
    to_run = spec
    if store is not None:
        run = store.create_run(feature, build, env_name, [c["extid"] for c in spec], require_tag=exec_tag)
        header["run_id"] = run.run_id
        header["run_url"] = run.url
        header["skipped_unconfirmed"] = list(run.skipped_unconfirmed)
        header["skipped_wrong_execution"] = list(run.skipped_wrong_execution)
        # An unattached case has nowhere to record its outcome, so executing it would produce a result
        # nobody ever sees. Skip it loudly instead.
        attached = set(run.attached)
        to_run = [c for c in spec if c["extid"] in attached]
        for orphan in [c["extid"] for c in spec if c["extid"] not in attached]:
            decisions.record(ctx, stage.name, "case_not_attached",
                             why="a spec entry with no runnable case in the store: either it was never "
                                 f"CONFIRMED, or it is not tagged {exec_tag}",
                             extid=orphan)
        if not to_run:
            write_json(ctx.path(RESULTS_OUT), {**header, "results": [], "tally": {}})
            raise GateFailure(
                f"nothing to run: no spec entry maps to a CONFIRMED {exec_tag} case. Either the cases "
                f"were never classified (the generator did not set `execution`), or they are tagged "
                f"ui/manual and are not the executor's to run."
            )

    results, tally = [], {}
    for c in to_run:
        r = be_execute.run_case(c, env, timeout=timeout, curl_path=curl_path)
        tally[r["status"]] = tally.get(r["status"], 0) + 1
        row = {"extid": c["extid"], "path": c["path"], "status": r["status"], "detail": r["detail"],
               "http_status": r.get("http", ""), "duration_s": r.get("elapsed")}
        results.append(row)
        if store is not None:
            props = {"http_status": r.get("http", ""), "environment": env_name, "build": build,
                     "endpoint": c["path"]}
            if cohort_path:
                props["cohort_observed"] = (be_execute.dig(r.get("payload") or {}, cohort_path)
                                            if r.get("payload") else "")
            store.record_execution(run.run_id, c["extid"], r["status"], actual=r["detail"],
                                   duration_s=r.get("duration_s", r.get("elapsed")), props=props)
    write_json(ctx.path(RESULTS_OUT), {**header, "results": results, "tally": tally})
    return "  ".join(f"{k}={v}" for k, v in sorted(tally.items()))


def _gate(ctx: RunContext) -> list[str]:
    data, bad = read_json_gated(ctx.path(RESULTS_OUT), expect=dict)
    if bad:
        return bad
    if data.get("offline"):
        return []
    results = data.get("results")
    if not isinstance(results, list):
        return [f"{RESULTS_OUT} has no results array"]
    out = []
    if not results:
        out.append("no case was executed — the run recorded nothing")
    for row in results:
        # Every case must carry the four fields a reader needs; an outcome with no detail is not evidence.
        for field in ("extid", "status", "detail", "http_status", "duration_s"):
            if field not in row:
                out.append(f"{row.get('extid', '<no extid>')}: results row is missing {field!r}")
        if row.get("status") not in ("PASS", "FAIL", "BLOCKED", "ERROR"):
            out.append(f"{row.get('extid')}: status {row.get('status')!r} is not "
                       f"PASS/FAIL/BLOCKED/ERROR")
    tally = data.get("tally") or {}
    if isinstance(tally, dict) and sum(tally.values()) != len(results):
        out.append(f"tally sums to {sum(tally.values())} but there are {len(results)} results")
    return out


STAGE = Stage(
    name="execute_be",
    requires=("qa/spec.json",),
    outputs=(RESULTS_OUT,),
    build_prompt=lambda ctx: "",
    gate=_gate,
    engine=_run,
)
