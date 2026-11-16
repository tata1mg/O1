#!/usr/bin/env python3
"""Mine one trace's WHY layer (per-tool reasons + deliberation digests) into triage reading lists.

Analysis passes over data the 0.24.50+ emission puts on the wire — deliberately a SCRIPT, not
harness code: outputs are reading lists for a human, precision is triage-grade, and nothing here
has authority over runs. Graduation path: a detector that proves itself across runs becomes an
emission or gate, with evidence.

Usage (from the repo root):
    uv run python scripts/mine_reasons.py <trace_id>
    uv run python scripts/mine_reasons.py <trace_id> --anchor "svc_c proxy"
    uv run python scripts/mine_reasons.py <trace_id> --days 90   # trace older than 30 days

Default report: reason-coverage sanity (the canary for the whole why-layer), scope distribution,
the knowing-doing reading list (reason names file X, action touches file Y — mostly benign
cross-referencing; read skeptically), and the per-lane uncertainty table (the abort-heuristic
feeder once enough runs exist).

--anchor TEXT: the Q2 "did the agent know?" sweep — greps every reason, deliberation digest, and
gate output for the anchor fact and prints the hits, so the never-saw / saw-and-misread /
knowing-doing verdict is evidence instead of clicking through 160 tool nodes.
"""

from __future__ import annotations

import argparse
import base64
import datetime as dt
import json
import re
import sys
import urllib.parse
import urllib.request
from collections import Counter, defaultdict
from pathlib import Path

FILE_RE = re.compile(r"[\w./-]+\.(?:py|js|rb|md|json|sql|kt|tsx?|java)")
UNCERTAIN_RE = re.compile(
    r"\b(not sure|unsure|unclear|uncertain|might be|may be wrong|guess|cannot determine|"
    r"can't determine|couldn't find|unable to find|not groundable|don't know|no idea|"
    r"ambiguous|risky|hmm)\b", re.I)


# Observations API v2 (Langfuse v4; the v1 `GET /api/public/observations` is deprecated). Differences
# that matter here: only the requested field groups come back; metadata values over 200 chars are
# truncated unless their key is listed in expandMetadata (reasons and digests are long); input/output
# are raw strings; pagination is a cursor that is absent on the last page.
V2_FIELDS = "core,basic,io,metadata"
EXPAND_METADATA = "reason,thinking_digest"
PAGE_LIMIT = 500


def _json_or_raw(value):
    """v2 returns input/output (and possibly metadata) as raw strings; v1 parsed them."""
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return value
    return value


def normalize(row: dict) -> dict:
    """One v2 row in the shape the analyses read: parsed input/output, metadata as a dict."""
    row = dict(row)
    for key in ("input", "output"):
        if key in row:
            row[key] = _json_or_raw(row[key])
    md = _json_or_raw(row.get("metadata"))
    row["metadata"] = md if isinstance(md, dict) else {}
    return row


def observations_url(host: str, trace_id: str, start: str, end: str, cursor: str | None) -> str:
    params = {"traceId": trace_id, "fields": V2_FIELDS, "expandMetadata": EXPAND_METADATA,
              "limit": PAGE_LIMIT, "fromStartTime": start, "toStartTime": end}
    if cursor:
        params["cursor"] = cursor
    return f"{host}/api/public/v2/observations?{urllib.parse.urlencode(params)}"


def fetch_observations(trace_id: str, start: str, end: str) -> list[dict]:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
    from rfc_agent.config import Config

    cfg = Config()
    public, secret = cfg.get("LANGFUSE.PUBLIC_KEY"), cfg.get("LANGFUSE.SECRET_KEY")
    if not (public and secret):
        sys.exit("no LANGFUSE keys in config/env — nothing to fetch")
    host = (cfg.get("LANGFUSE.HOST") or "https://cloud.langfuse.com").rstrip("/")
    auth = base64.b64encode(f"{public}:{secret}".encode()).decode()
    out: list[dict] = []
    cursor = None
    while True:
        req = urllib.request.Request(observations_url(host, trace_id, start, end, cursor),
                                     headers={"Authorization": "Basic " + auth})
        data = json.loads(urllib.request.urlopen(req, timeout=30).read())
        out += [normalize(o) for o in data.get("data") or []]
        cursor = (data.get("meta") or {}).get("cursor")
        if not cursor:  # an empty page can still carry a cursor; only its absence ends the list
            break
    return out


def _iso(d: dt.datetime) -> str:
    return d.astimezone(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def lane_of(o: dict, byid: dict) -> str:
    """Tool -> its lane (agent:<stage>#n or persona:<slug>#n). In-flight parents haven't exported
    yet (spans export on close) — fall back to the observation's own stage/attempt metadata."""
    p = byid.get(o.get("parentObservationId") or "")
    if p:
        return p.get("name") or "?"
    md = o.get("metadata") or {}
    return f"agent:{md.get('stage', '?')}#{md.get('attempt', '?')}"


def reason_coverage(tools: list[dict]) -> tuple[int, int, Counter]:
    """(with_reason, total, scope distribution) — the canary: a collapse here means thinking
    stopped reaching the stream (tap off, CLI change), not that the agent stopped thinking."""
    withr = [t for t in tools if (t.get("metadata") or {}).get("reason")]
    scopes = Counter((t["metadata"]).get("reason_scope") for t in withr)
    return len(withr), len(tools), scopes


def knowing_doing_flags(tools: list[dict], byid: dict) -> list[tuple[str, str, str, list, str]]:
    """same_message file-ops whose reason names specific file(s) the action does NOT touch.
    v0 precision note (measured rc5): most flags are benign cross-referencing (reason names the
    claim's source, action checks it against code) — this is a READING LIST, never a verdict."""
    flags = []
    for o in tools:
        md = o.get("metadata") or {}
        if md.get("reason_scope") != "same_message":
            continue
        if o.get("name") not in ("tool:Read", "tool:Edit", "tool:Write"):
            continue
        target = (o.get("input") or {}).get("file_path") or ""
        named = {Path(f).name for f in FILE_RE.findall(md.get("reason", ""))}
        if target and named and Path(target).name not in named:
            flags.append((lane_of(o, byid), o["name"], Path(target).name,
                          sorted(named)[:3], md["reason"][:140]))
    return flags


def uncertainty_table(obs: list[dict], byid: dict) -> dict[str, list[int]]:
    """lane -> [uncertain_units, total_units] over tool reasons + digest blocks. Meaningful as a
    cross-run join against <stage>_attempt_outcome once ~10 runs exist; per-run it's a read hint."""
    per: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    for o in obs:
        name = o.get("name") or ""
        md = o.get("metadata") or {}
        if name.startswith("tool:") and md.get("reason"):
            row = per[lane_of(o, byid)]
            row[1] += 1
            row[0] += bool(UNCERTAIN_RE.search(md["reason"]))
        elif (name.startswith(("llm:", "persona:", "shard:"))) and md.get("thinking_digest"):
            row = per[name]
            for part in md["thinking_digest"].split("\n---\n"):
                row[1] += 1
                row[0] += bool(UNCERTAIN_RE.search(part))
    return dict(per)


def anchor_hits(obs: list[dict], byid: dict, anchor: str) -> list[tuple[str, str, str]]:
    """(where, kind, excerpt) for every reason / digest block / gate output containing the anchor
    (case-insensitive). Empty reasons+digests but present gate output = the never-saw verdict."""
    a = anchor.lower()
    hits = []

    def _grab(text: str) -> str:
        i = text.lower().find(a)
        return " ".join(text[max(0, i - 80): i + len(a) + 160].split())

    for o in obs:
        name = o.get("name") or ""
        md = o.get("metadata") or {}
        if name.startswith("tool:") and a in (md.get("reason") or "").lower():
            hits.append((f"{lane_of(o, byid)} {name}", "reason", _grab(md["reason"])))
        if a in (md.get("thinking_digest") or "").lower():
            hits.append((name, "deliberation", _grab(md["thinking_digest"])))
        if name.startswith("gate:"):
            out = o.get("output")
            text = "\n".join(out) if isinstance(out, list) else str(out or "")
            if a in text.lower():
                hits.append((name, "gate output", _grab(text)))
    return hits


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("trace_id")
    ap.add_argument("--anchor", help='fact to sweep for (Q2 "did the agent know?")')
    ap.add_argument("--days", type=int, default=30,
                    help="how far back the trace may have started (Observations API v2 queries are "
                         "bounded by start time; default 30)")
    args = ap.parse_args()

    now = dt.datetime.now(dt.UTC)
    obs = fetch_observations(args.trace_id, _iso(now - dt.timedelta(days=args.days)),
                             _iso(now + dt.timedelta(hours=1)))
    byid = {o["id"]: o for o in obs}
    tools = [o for o in obs if (o.get("name") or "").startswith("tool:")]
    print(f"trace {args.trace_id}: {len(obs)} observations, {len(tools)} tool calls")
    if not obs:
        print(f"  no observations in the last {args.days} days — an older trace needs a larger --days")

    withr, total, scopes = reason_coverage(tools)
    print(f"\nREASON COVERAGE (why-layer canary): {withr}/{total} tools carry a reason "
          f"| scopes: {dict(scopes)}")
    if total and withr < total:
        print("  !! uncovered tools — check THINKING_TAP / CLI stream before trusting any analysis below")

    if args.anchor:
        hits = anchor_hits(obs, byid, args.anchor)
        print(f"\nANCHOR SWEEP: {args.anchor!r} — {len(hits)} hit(s)")
        for where, kind, excerpt in hits:
            print(f"  [{kind}] {where}\n    …{excerpt}…")
        if not any(k in ("reason", "deliberation") for _, k, _ in hits):
            print("  verdict hint: NEVER-SAW — the fact appears in no reasoning; look upstream")
        return

    flags = knowing_doing_flags(tools, byid)
    print(f"\nKNOWING-DOING READING LIST ({len(flags)} candidates — mostly benign cross-referencing, "
          "read skeptically):")
    for lane, name, target, named, reason in flags:
        print(f"  {lane} {name} -> {target} | reason names {named}\n    {reason}")

    print("\nUNCERTAINTY PER LANE (uncertain units / total reasoning units):")
    for lane, (u, t) in sorted(uncertainty_table(obs, byid).items()):
        print(f"  {lane:32s} {u:3d}/{t:3d}  ({100 * u / t:.0f}%)")


if __name__ == "__main__":
    main()
