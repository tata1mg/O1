"""Flat numbers about a run — how big the input was, what the agents consumed, how the context layer behaved.

Read from files the pipeline already wrote (scope.json, work/, logs/, out/rfc.md) and from each agent's saved
event stream (logs/*.stream.jsonl). Nothing here runs inside an agent: it is computed after the fact, so a run
and its resumes can be measured the same way and the agents stay untouched. run_facts.collect merges the result
into the facts stamped on the `rfc-run` observation and on every feedback score.

Every field is optional: a missing or unreadable file just drops that field, and `collect` never raises.

Scope's Atlassian connector agents write no stream file, so the token totals cover the agents that do (Scope's
repo discovery, Investigate, Draft, Review, Revise, Summarize).
"""

from __future__ import annotations

import contextlib
import json
import re
from datetime import UTC, datetime
from pathlib import Path

CONTEXT_SERVER = "fleetlens"  # the context layer; its tools reach agents as mcp__fleetlens__*
_TOKEN_KEYS = ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")
_CHARS_PER_TOKEN = 4  # rough English/markdown estimate; the field name says "est"
_EMPTY_RESULTS = {"", "[]", "{}", "null", "none"}


def _json(path: Path):
    with contextlib.suppress(OSError, json.JSONDecodeError):
        return json.loads(path.read_text(encoding="utf-8"))
    return None


def _int(v) -> int:
    return int(v) if isinstance(v, int | float) and not isinstance(v, bool) else 0


def _result_text(content) -> str:
    """A tool_result's content is a string or a list of {type: text, text} blocks."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(str(b.get("text") or "") for b in content if isinstance(b, dict))
    return ""


def scan_stream(path: Path) -> dict:
    """One agent invocation's event stream -> tokens, compactions, peak context, model and context-layer calls."""
    out = {
        "usage": {},
        "turns": 0,
        "compactions": 0,
        "peak_context": 0,
        "model": None,
        "context_calls": 0,
        "context_errors": 0,
        "context_empty": 0,
        "rate_limit_rejections": 0,
    }
    names: dict[str, str] = {}  # tool_use id -> tool name
    prefix = f"mcp__{CONTEXT_SERVER}__"
    with contextlib.suppress(OSError), path.open(encoding="utf-8", errors="replace") as f:
        for line in f:
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            if not isinstance(ev, dict):
                continue
            kind = ev.get("type")
            if kind == "system":
                if "compact" in str(ev.get("subtype") or ""):
                    out["compactions"] += 1
                if ev.get("subtype") == "init" and ev.get("model"):
                    out["model"] = str(ev["model"])
            elif kind == "assistant":
                msg = ev.get("message") if isinstance(ev.get("message"), dict) else {}
                u = msg.get("usage")
                if isinstance(u, dict):
                    out["peak_context"] = max(
                        out["peak_context"],
                        sum(
                            _int(u.get(k))
                            for k in ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")
                        ),
                    )
                for b in msg.get("content") or []:
                    if isinstance(b, dict) and b.get("type") == "tool_use" and str(b.get("name")).startswith(prefix):
                        out["context_calls"] += 1
                        names[str(b.get("id"))] = str(b.get("name"))
            elif kind == "user":
                msg = ev.get("message") if isinstance(ev.get("message"), dict) else {}
                content = msg.get("content")
                for b in content if isinstance(content, list) else []:
                    if not (isinstance(b, dict) and b.get("type") == "tool_result"):
                        continue
                    if str(b.get("tool_use_id")) not in names:
                        continue
                    if b.get("is_error"):
                        out["context_errors"] += 1
                    elif _result_text(b.get("content")).strip().lower() in _EMPTY_RESULTS:
                        out["context_empty"] += 1
            elif kind == "rate_limit_event":  # the CLI reports every check; only a refusal is a hit
                status = str((ev.get("rate_limit_info") or {}).get("status") or "allowed")
                if status not in ("allowed", "allowed_warning"):
                    out["rate_limit_rejections"] += 1
            elif kind == "result":  # cumulative for the invocation: the last one wins
                if isinstance(ev.get("usage"), dict):
                    out["usage"] = ev["usage"]
                out["turns"] = _int(ev.get("num_turns")) or out["turns"]
    return out


def _agents(run_dir: Path) -> dict:
    scans = [scan_stream(p) for p in sorted((run_dir / "logs").glob("*.stream.jsonl"))]
    if not scans:
        return {}
    tok = {k: sum(_int(s["usage"].get(k)) for s in scans) for k in _TOKEN_KEYS}
    fresh = tok["input_tokens"] + tok["cache_read_input_tokens"] + tok["cache_creation_input_tokens"]
    out = {
        "agent_runs": len(scans),
        "tokens_input": tok["input_tokens"],
        "tokens_output": tok["output_tokens"],
        "tokens_cache_read": tok["cache_read_input_tokens"],
        "tokens_cache_write": tok["cache_creation_input_tokens"],
        "tokens_total": sum(tok.values()),
        "cache_hit_ratio": round(tok["cache_read_input_tokens"] / fresh, 3) if fresh else None,
        "agent_turns": sum(s["turns"] for s in scans),
        "compactions": sum(s["compactions"] for s in scans),
        "peak_context_tokens": max(s["peak_context"] for s in scans),
        "context_calls": sum(s["context_calls"] for s in scans),
        "context_errors": sum(s["context_errors"] for s in scans),
        "context_empty": sum(s["context_empty"] for s in scans),
        "rate_limit_rejections": sum(s["rate_limit_rejections"] for s in scans),
        "models": sorted({s["model"] for s in scans if s["model"]}),
    }
    return {k: v for k, v in out.items() if v not in (None, [])}


def _inputs(run_dir: Path, scope: dict) -> dict:
    out: dict = {}
    with contextlib.suppress(OSError):
        prd = (run_dir / "raw" / "prd.md").read_text(encoding="utf-8")
        out["prd_words"] = len(prd.split())
        out["prd_tokens_est"] = len(prd) // _CHARS_PER_TOKEN
    secs = [
        s
        for ex in scope.get("figma") or []
        if isinstance(ex, dict)
        for s in ex.get("sections") or []
        if isinstance(s, dict)
    ]
    if secs:
        picked = [s for s in secs if s.get("include")]
        out["figma_sections"] = len(secs)
        out["figma_sections_kept"] = len(picked)
        out["figma_frames"] = sum(_int(s.get("frames")) for s in secs)
        out["figma_sections_skipped"] = len(secs) - len(picked)  # left out: little PRD vocabulary in common
        out["figma_frames_in_scope"] = sum(_int(s.get("frames")) for s in picked)
    ages = []
    now = datetime.now(UTC)
    for r in scope.get("repos") or []:  # an index-only repo has no snapshot, so no commit date
        with contextlib.suppress(AttributeError, ValueError, TypeError):
            if r.get("commit_date"):
                ages.append((now - datetime.fromisoformat(r["commit_date"])).days)
    if ages:
        out["snapshot_age_days"] = max(ages)
    return out


def _scope_choices(run_dir: Path, scope: dict) -> dict:
    """What Scope proposed against what the person kept: a long gap means discovery over- or under-proposes."""
    out: dict = {}
    items = [i for i in (_json(run_dir / "confirm" / "proposal.json") or {}).get("items") or [] if isinstance(i, dict)]
    for kind, kept in (("repo", scope.get("repos")), ("page", scope.get("pages_included"))):
        mine = [i for i in items if i.get("kind") == kind]
        if mine:
            out[f"{kind}s_proposed"] = len(mine)
            out[f"{kind}s_preticked"] = sum(1 for i in mine if i.get("include"))
            out[f"{kind}s_kept"] = len(kept or [])
    return out


def _review(run_dir: Path) -> dict:
    """Review and Revise, from the findings' final state: how many, how serious, what became of them, the rounds."""
    status = _json(run_dir / "review" / "status.json")
    if not isinstance(status, dict):
        return {}
    recs = [r for r in status.values() if isinstance(r, dict)]
    out: dict = {}
    for name, sev in (("blockers", "BLOCKER"), ("majors", "MAJOR"), ("minors", "MINOR")):
        out[f"review_{name}"] = sum(1 for r in recs if r.get("severity") == sev)
    out["review_fixed"] = sum(1 for r in recs if r.get("status") == "fixed")
    out["review_ignored"] = sum(1 for r in recs if r.get("status") == "ignored")  # the person chose not to fix it
    rounds = [r["round"] for r in recs if isinstance(r.get("round"), int)]
    if rounds:
        out["revise_rounds"] = max(rounds)
    changed = 0
    for diff in (run_dir / "review").glob("rfc.round*.diff"):
        with contextlib.suppress(OSError):
            lines = diff.read_text(encoding="utf-8", errors="replace").splitlines()
            changed += sum(1 for ln in lines if ln[:1] in "+-" and not ln.startswith(("+++", "---")))
    if changed:
        out["revise_lines_changed"] = changed
    return out


def _plan(run_dir: Path) -> dict:
    out: dict = {}
    flows = (_json(run_dir / "work" / "flows.json") or {}).get("flows")
    if isinstance(flows, list):
        out["flows"] = len(flows)
        out["flows_without_screens"] = sum(
            1 for f in flows if isinstance(f, dict) and not ((f.get("trigger") or {}).get("screens") or [])
        )
    reqs = (_json(run_dir / "work" / "reqs.json") or {}).get("requirements")
    if isinstance(reqs, list):
        out["reqs"] = len(reqs)
    qs = (_json(run_dir / "work" / "open_questions.json") or {}).get("questions")
    if isinstance(qs, list):
        out["open_questions"] = len(qs)
    return out


def _rfc(run_dir: Path) -> dict:
    out: dict = {}
    draft = _json(run_dir / "logs" / "draft.json") or {}
    sts = draft.get("statuses")
    if isinstance(sts, dict):
        for k in ("covered", "assumes", "deferred", "open"):
            out[f"req_{k}"] = _int(sts.get(k))
    with contextlib.suppress(OSError):
        rfc = (run_dir / "out" / "rfc.md").read_text(encoding="utf-8")
        with contextlib.suppress(OSError):  # Summarize: how much shorter is the readable copy
            readable = (run_dir / "out" / "rfc.readable.md").read_text(encoding="utf-8")
            out["readable_chars"] = len(readable)
            out["readable_ratio"] = round(len(readable) / len(rfc), 3) if rfc else None
        m = re.search(r"^##\s+3\.?\s.*?$(.*?)^##\s+(?!3\b)", rfc + "\n## end", re.MULTILINE | re.DOTALL)
        if m:
            rows = [ln for ln in m.group(1).splitlines() if ln.lstrip().startswith("|")]
            body = [ln for ln in rows if not re.fullmatch(r"[\s|:-]+", ln)]
            if len(body) > 1:  # first table row is the header
                out["alternatives"] = len(body) - 1
    return out


def _waits(run_dir: Path, scope: dict) -> dict:
    """Seconds a person kept the run waiting: Scope's confirm screen, Investigate's two pauses, Review's triage."""
    parts: list[float] = []
    confirm = (scope.get("timings_s") or {}).get("confirm_wait")
    if isinstance(confirm, int | float):
        parts.append(float(confirm))
    inv = (_json(run_dir / "logs" / "investigate.json") or {}).get("pause_wait_s") or {}
    parts += [float(v) for v in inv.values() if isinstance(v, int | float)]
    review = _json(run_dir / "logs" / "review.json") or {}
    triage = review.get("triage_wait_s")
    if isinstance(triage, int | float):
        parts.append(float(triage))
    out: dict = {}
    if parts:
        out["operator_wait_s"] = round(sum(parts), 1)
    if isinstance(triage, int | float):
        out["triage_wait_s"] = round(float(triage), 1)
    return out


def collect(run_dir: Path) -> dict:
    """All fields that can be read from `run_dir`; absent files drop their fields. Never raises."""
    out: dict = {}
    scope: dict = {}
    with contextlib.suppress(Exception):
        scope = _json(run_dir / "scope.json") or {}
    for part in (
        lambda: _inputs(run_dir, scope),
        lambda: _scope_choices(run_dir, scope),
        lambda: _review(run_dir),
        lambda: _plan(run_dir),
        lambda: _rfc(run_dir),
        lambda: _waits(run_dir, scope),
        lambda: _agents(run_dir),
    ):
        with contextlib.suppress(Exception):  # one unreadable source must not drop the others
            out.update(part())
    return {k: v for k, v in out.items() if v is not None}
