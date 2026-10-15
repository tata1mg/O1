"""The lead's plan (work/reqs.json + work/flows.json): load, check, render.

The lead writes JSON only. Python checks it (docs/INVESTIGATE.md "Checks on the plan") and renders what humans
and tracers read: work/requirements.md, work/flows.md, work/briefs/<tracer>.md.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

PROMPTS = Path(__file__).with_name("prompts")


def load_json(path: Path) -> tuple[dict | None, str]:
    """(data, error). Tolerates a ```json fence around the object."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None, f"{path.name} was not written"
    text = re.sub(r"^\s*```(?:json)?\s*|\s*```\s*$", "", text.strip())
    try:
        data = json.loads(text)
    except ValueError as e:
        return None, f"{path.name} is not valid JSON ({e})"
    return (data, "") if isinstance(data, dict) else (None, f"{path.name} must be a JSON object")


def scope_repos(scope: dict) -> dict[str, dict]:
    return {r["repo"]: r for r in scope.get("repos") or []}


def check(reqs: dict, flows: dict, scope: dict, max_tracers: int) -> list[str]:
    """Plan errors (empty = valid). Each message is written for the lead to fix in one pass."""
    errs: list[str] = []
    req_ids = [r.get("id") for r in reqs.get("requirements") or []]
    known = set(req_ids)
    if not req_ids:
        errs.append("reqs.json has no requirements")
    if len(known) != len(req_ids):
        errs.append("reqs.json has duplicate REQ ids")
    fl = flows.get("flows") or []
    flow_ids = [f.get("id") for f in fl]
    if not fl:
        errs.append("flows.json has no flows")
    if len(set(flow_ids)) != len(flow_ids):
        errs.append("flows.json has duplicate flow ids")
    placed: set[str] = set(flows.get("no_flow_reqs") or [])
    for f in fl:
        fid = f.get("id", "?")
        for r in f.get("reqs") or []:
            if r not in known:
                errs.append(f"{fid}: unknown REQ id {r}")
            placed.add(r)
        if not [v for v in f.get("verify") or [] if str(v).strip()]:
            errs.append(f"{fid}: 'verify' needs at least one item")
        if not f.get("hops"):
            errs.append(f"{fid}: no hops")
        for h in f.get("hops") or []:
            if h.get("confidence") not in ("map", "prd", "inferred"):
                errs.append(f"{fid} hop {h.get('n')}: confidence must be map, prd or inferred")
    for r in flows.get("no_flow_reqs") or []:
        if r not in known:
            errs.append(f"no_flow_reqs: unknown REQ id {r}")
    missing = [r for r in req_ids if r not in placed]
    if missing:
        errs.append(f"REQs in no flow and not in no_flow_reqs: {', '.join(missing)}")
    tr = flows.get("tracers") or []
    if not tr:
        errs.append("flows.json has no tracers")
    if len(tr) > max_tracers:
        errs.append(f"{len(tr)} tracers, limit {max_tracers}: merge flows that share code")
    tids = [t.get("id") for t in tr]
    if len(set(tids)) != len(tids):
        errs.append("duplicate tracer ids")
    covered: set[str] = set()
    by_flow = {f.get("id"): f for f in fl}
    in_scope = scope_repos(scope)
    for t in tr:
        tid = t.get("id", "?")
        if not t.get("flows"):
            errs.append(f"{tid}: covers no flows")
        if not t.get("repos"):
            errs.append(f"{tid}: no repos")
        hop_repos: set[str] = set()
        for fid in t.get("flows") or []:
            if fid not in by_flow:
                errs.append(f"{tid}: unknown flow {fid}")
                continue
            covered.add(fid)
            hop_repos |= {h.get("repo") for h in by_flow[fid].get("hops") or []}
        stray = [r for r in t.get("repos") or [] if r not in hop_repos]
        if stray:
            errs.append(f"{tid}: repos {', '.join(stray)} are not in the hops of its flows")
        outside = [r for r in t.get("repos") or [] if r not in in_scope]
        if outside:
            errs.append(f"{tid}: repos {', '.join(outside)} are not in scope — a tracer reads only scope repos")
    # a flow with no hop in scope has nothing to trace: it needs no tracer
    traceable = {f.get("id") for f in fl if any(h.get("repo") in in_scope for h in f.get("hops") or [])}
    uncovered = [f for f in flow_ids if f not in covered and f in traceable]
    if uncovered:
        errs.append(f"flows covered by no tracer: {', '.join(uncovered)}")
    return errs


def repos_needing_attention(flows: dict, scope: dict) -> tuple[dict[str, list[str]], dict[str, list[str]]]:
    """({repo not in scope: [flows]}, {repo in scope without a snapshot: [flows]})."""
    in_scope = scope_repos(scope)
    missing: dict[str, list[str]] = {}
    no_snap: dict[str, list[str]] = {}
    for f in flows.get("flows") or []:
        for h in f.get("hops") or []:
            repo = h.get("repo")
            if not repo:
                continue
            rec = in_scope.get(repo)
            target = missing if rec is None else (no_snap if not rec.get("path") else None)
            if target is not None and f.get("id") not in target.setdefault(repo, []):
                target[repo].append(f.get("id"))
    return missing, no_snap


# ---- rendering ------------------------------------------------------------------------------------


def render_requirements(reqs: dict) -> str:
    lines = [
        f"# Requirements ({reqs.get('granularity', '?')})",
        "",
        "| id | priority | requirement | flows | PRD quote |",
        "|---|---|---|---|---|",
    ]
    for r in reqs.get("requirements") or []:
        q = str(r.get("quote", "")).replace("|", "\\|").replace("\n", " ")[:160]
        text = str(r.get("text", "")).replace("|", "\\|")
        parent = f" (of {r['parent']})" if r.get("parent") else ""
        lines.append(
            f"| {r.get('id')}{parent} | {r.get('priority', '')} | {text} | "
            f"{', '.join(r.get('flows') or []) or '—'} | {q} |"
        )
    return "\n".join(lines) + "\n"


def _flow_block(f: dict, in_scope: dict) -> list[str]:
    trig = f.get("trigger") or {}
    out = [
        f"## {f.get('id')} {f.get('name', '')}",
        f"- trigger: {trig.get('surface', '?')} · screens: {', '.join(trig.get('screens') or []) or '—'} · "
        f"{trig.get('action', '')}",
        f"- REQs: {', '.join(f.get('reqs') or []) or '—'}",
        "- hops:",
    ]
    for h in f.get("hops") or []:
        repo = h.get("repo")
        tag = "" if repo in in_scope else "  ⚠ not in scope — not traced"
        if repo in in_scope and not in_scope[repo].get("path"):
            tag = "  ⚠ no snapshot"
        out.append(f"  {h.get('n')}. **{repo}** — {h.get('what')} · {h.get('confidence')}: {h.get('anchor', '')}{tag}")
    out.append("- verify:")
    out += [f"  - V{i} {v}" for i, v in enumerate(f.get("verify") or [], 1)]
    if f.get("ignore"):
        out.append("- ignore: " + "; ".join(f["ignore"]))
    return out


def render_flows(flows: dict, scope: dict, preset: str, max_tracers: int, ui_note: str = "") -> str:
    in_scope = scope_repos(scope)
    lines = ["# Flows and tracer plan", ""]
    for f in flows.get("flows") or []:
        lines += _flow_block(f, in_scope) + [""]
    tr = flows.get("tracers") or []
    lines += [f"## Tracer plan ({len(tr)} tracers, preset {preset}, limit {max_tracers})", ""]
    for t in tr:
        lines.append(
            f"- **{t.get('id')}** — flows {', '.join(t.get('flows') or [])} · repos "
            f"{', '.join(t.get('repos') or [])} · {t.get('why', '')}"
        )
    if ui_note:
        lines.append(f"- **UI** — {ui_note}")
    if flows.get("no_flow_reqs"):
        lines += ["", f"REQs with no code path: {', '.join(flows['no_flow_reqs'])}"]
    if flows.get("notes"):
        lines += ["", f"Notes: {flows['notes']}"]
    return "\n".join(lines) + "\n"


def render_brief(
    tracer: dict, flows: dict, reqs: dict, scope: dict, index_info: dict[str, dict], entry_file: str | None = None
) -> str:
    in_scope = scope_repos(scope)
    by_flow = {f.get("id"): f for f in flows.get("flows") or []}
    req_text = {r.get("id"): r.get("text") for r in reqs.get("requirements") or []}
    lines = [
        f"# Brief for tracer {tracer.get('id')}",
        "",
        f"Why this tracer: {tracer.get('why', '')}",
        "",
        "## Repos you may read",
    ]
    for repo in tracer.get("repos") or []:
        rec = in_scope.get(repo) or {}
        info = index_info.get(repo) or {}
        snap = rec.get("path")
        where = f"_snap/{repo}" if snap else "NO SNAPSHOT — cannot read its code; report its hops as untraced"
        lines.append(
            f"- **{repo}** — {where} · commit {str(rec.get('sha') or '?')[:10]} · "
            f"code index: {'yes' if info.get('code_index') else 'no'} · "
            f"index age: {info.get('age_note', 'unknown')}"
        )
    for fid in tracer.get("flows") or []:
        f = by_flow.get(fid)
        if not f:
            continue
        lines += [""] + _flow_block(f, in_scope)
        lines.append("- REQs carried:")
        lines += [f"  - {r}: {req_text.get(r, '')}" for r in f.get("reqs") or []]
    if entry_file:
        lines += ["", f"Starting points (rg pre-pass): {entry_file}"]
    return "\n".join(lines) + "\n"


def prompt(name: str, **slots: str) -> str:
    text = (PROMPTS / name).read_text(encoding="utf-8")
    for k, v in slots.items():
        text = text.replace("{{" + k + "}}", v)
    return text


GRANULARITY_TEXT = {
    "grouped": "Granularity: GROUPED — one REQ per capability-level behaviour (typically 15-30). Group related "
    "statements; don't split one behaviour into many REQs.",
    "fine": "Granularity: FINE — one REQ per testable statement (typically 50-100). Every fine REQ has a "
    '"parent": the id of a grouped, capability-level REQ it belongs to (include those parents as '
    'REQs too, with "parent": null).',
}
SPLIT_TEXT = {
    "flow": "one tracer per flow; split a flow only if it crosses more than 4 repos.",
    "hop": "split every flow by repo (one tracer per flow x repo); merge only identical code paths.",
}
