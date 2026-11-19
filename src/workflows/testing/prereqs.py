"""What has to be on prestag before the suite can run — the deterministic half, and the renderer.

The NR-DIGICHEM run (2026-11-02) produced 102 cases whose preconditions name records nobody had created
(job 98771 "with no saved ETA", hdpo_orders rows in PLACED, pincode 110055 in NRCH.SERVICEABLE_PINCODES) and
an HMAC signature nobody could compute. Whoever runs the suite had to rebuild that list from 102 rows by
hand, and the Test Data source stayed "blocked" because nothing told the team what data to supply.

Half of the answer needs no model. The Implementation Plan's `### CHANGE-n <service>: … [REQ-…]` blocks say
which services change, who owns them, what depends on what, which migrations ship and what to smoke-check
on staging; each case cites the REQs it covers, so REQ overlap says which services a case needs deployed.
`facts()` computes that. The other half — grouping 102 free-text preconditions into the fixtures, config
values, secrets and external systems they need — is judgment, and the `prerequisites` stage's model writes
it as `qa/prerequisites.json`. `validate()` is that file's gate; `render_md()` / `render_template()` turn
both halves into the documents a person reads and fills.
"""

from __future__ import annotations

import json
import re

#: `### CHANGE-4 merchant_aggregator: HDPO intake … [REQ-1, REQ-2]` — the plan's block heading.
RE_CHANGE = re.compile(r"^###\s+(CHANGE-\d+)\s+(.*)$", re.M)
#: The heading's tail: service, an optional `(…)` note (owner/external), a title, optional REQ list.
RE_HEAD = re.compile(r"^(?P<service>[^:(]+?)\s*(?:\((?P<paren>[^)]*)\))?\s*:\s*(?P<title>.*?)\s*"
                     r"(?:\[(?P<reqs>[^\]]*)\])?\s*$")
#: A block's keyed line: `- repo: …`, `- files: …`, `- depends on: …`, `- verify: …`.
RE_KEY = re.compile(r"^- (?P<key>repo|files|what|depends on|verify):\s*(?P<rest>.*)$")
RE_REQ = re.compile(r"\bREQ-\d+\b")
RE_CHANGE_ID = re.compile(r"\bCHANGE-\d+\b")
RE_OWNER = re.compile(r"owner:\s*([^;)]+)", re.I)
#: A placeholder a case left for the tester: `<valid-hmac-sha256-of-body>`, `<test_id>`.
RE_PLACEHOLDER = re.compile(r"<[a-z][\w .-]{1,40}>", re.I)
#: The generator's own words for a value it made up (SKILL.md "Test Data must come from…").
RE_INVENTED = re.compile(r"unconfirmed|invented|assumed|placeholder|sample value|not supplied", re.I)
RE_FIXTURE_ID = re.compile(r"^FX-\d{2,}$")

#: The model-written sections and the fields each item must fill (`used_by` is checked separately).
SECTIONS: dict[str, tuple[str, ...]] = {
    "fixtures": ("id", "entity", "state", "how_to_create"),
    "config": ("key", "service", "needed"),
    "external_systems": ("name", "needed_for"),
    "secrets": ("name", "provide_via"),
}
#: A secret is listed by name only. A key like these in a secrets row is a value about to leak into
#: a file that is copied into the store and the trace.
SECRET_VALUE_KEYS = ("value", "real_value", "secret", "key_value", "password", "token")


# ---- the plan --------------------------------------------------------------------------------------
def parse_plan(text: str) -> list[dict]:
    """Every `### CHANGE-n` block -> {id, service, title, reqs, external, owner, repo, files, migrations,
    depends_on, verify, verify_on_staging}. Blocks end at the next `###` or `##` heading."""
    out: list[dict] = []
    heads = list(RE_CHANGE.finditer(text or ""))
    for i, m in enumerate(heads):
        end = heads[i + 1].start() if i + 1 < len(heads) else len(text)
        nxt = re.search(r"^##\s", text[m.end():end], re.M)
        body = text[m.end(): m.end() + nxt.start()] if nxt else text[m.end():end]
        h = RE_HEAD.match(m.group(2).strip())
        service = (h.group("service") if h else m.group(2)).strip()
        paren = (h.group("paren") or "") if h else ""
        keyed = _keyed(body)
        repo = keyed.get("repo", "")
        files = [re.sub(r"\s*\((?:new|modify|delete)\)$", "", f.strip())
                 for f in keyed.get("files", "").split("·") if f.strip()]
        owner = RE_OWNER.search(paren) or RE_OWNER.search(repo)
        verify = keyed.get("verify", "")
        out.append({
            "id": m.group(1),
            "service": service,
            "title": (h.group("title") if h else "").strip(),
            "reqs": RE_REQ.findall((h.group("reqs") or "") if h else m.group(2)),
            "external": "external" in paren.lower() or "outside this rfc" in repo.lower(),
            "owner": owner.group(1).strip() if owner else "",
            "repo": repo,
            "files": files,
            "migrations": [f for f in files if "migration" in f.lower()],
            "depends_on": RE_CHANGE_ID.findall(keyed.get("depends on", "")),
            "verify": verify,
            "verify_on_staging": "staging" in verify.lower(),
        })
    return out


def _keyed(body: str) -> dict[str, str]:
    """`- key: rest` lines plus the indented lines under them, per key."""
    out: dict[str, list[str]] = {}
    key = None
    for line in body.splitlines():
        m = RE_KEY.match(line)
        if m:
            key = m.group("key")
            out[key] = [m.group("rest").strip()] if m.group("rest").strip() else []
        elif key and line.startswith((" ", "\t")) and line.strip():
            out[key].append(line.rstrip())
        elif line.startswith("- "):
            key = None
    return {k: "\n".join(v).strip() for k, v in out.items()}


def deploy_waves(changes: list[dict]) -> list[list[str]]:
    """CHANGE ids grouped by dependency depth: wave 1 depends on nothing, wave n on waves < n. A dependency
    on a CHANGE the plan does not define is ignored; a cycle lands its members in the last wave."""
    known = {c["id"]: c for c in changes}
    depth: dict[str, int] = {}

    def level(cid: str, seen: frozenset = frozenset()) -> int:
        if cid in depth:
            return depth[cid]
        if cid in seen:
            return len(known)
        deps = [d for d in known[cid]["depends_on"] if d in known and d != cid]
        depth[cid] = 0 if not deps else 1 + max(level(d, seen | {cid}) for d in deps)
        return depth[cid]

    for cid in known:
        level(cid)
    waves: dict[int, list[str]] = {}
    for cid in known:
        waves.setdefault(depth[cid], []).append(cid)
    return [waves[k] for k in sorted(waves)]


# ---- the facts -------------------------------------------------------------------------------------
def facts(cases: list[dict], plan_text: str) -> dict:
    """The deterministic half: services, deploy order, migrations, smoke checks, and per case the
    services its REQs touch plus the placeholders / invented values it carries."""
    changes = parse_plan(plan_text)
    by_req: dict[str, list[dict]] = {}
    for ch in changes:
        for r in ch["reqs"]:
            by_req.setdefault(r, []).append(ch)
    rows = []
    need: dict[str, set[str]] = {}
    for c in cases:
        if not isinstance(c, dict):
            continue
        cid = str(c.get("id") or "")
        reqs = RE_REQ.findall(str(c.get("requirement") or ""))
        touched = {ch["service"] for r in reqs for ch in by_req.get(r, [])}
        for s in touched:
            need.setdefault(s, set()).add(cid)
        text = " ".join(str(c.get(k) or "") for k in ("precondition", "steps", "test_data"))
        rows.append({
            "id": cid,
            "execution": str(c.get("execution") or ""),
            "reqs": reqs,
            "services": sorted(touched),
            "placeholders": sorted(set(RE_PLACEHOLDER.findall(text))),
            "invented": bool(RE_INVENTED.search(f"{c.get('test_data') or ''} {c.get('qa_comment') or ''}")),
        })
    services: dict[str, dict] = {}
    for ch in changes:
        s = services.setdefault(ch["service"], {"service": ch["service"], "changes": [], "external": False,
                                                "owner": ""})
        s["changes"].append(ch["id"])
        s["external"] = s["external"] or ch["external"]
        s["owner"] = s["owner"] or ch["owner"]
    for s in services.values():
        s["cases"] = sorted(need.get(s["service"], set()))
    return {
        "changes": changes,
        "waves": deploy_waves(changes),
        "services": list(services.values()),
        "migrations": [{"change": ch["id"], "service": ch["service"], "file": f}
                       for ch in changes for f in ch["migrations"]],
        "cases": rows,
        "cases_without_service": [r["id"] for r in rows if not r["services"]],
    }


# ---- the model's half: the gate --------------------------------------------------------------------
def validate(doc, cases: list[dict], facts_: dict | None = None) -> list[str]:
    """Failures in `qa/prerequisites.json`, each naming the item or case to fix. [] = pass."""
    if not isinstance(doc, dict):
        return ["prerequisites.json is not a JSON object"]
    out: list[str] = []
    ids = [str(c.get("id")) for c in cases if isinstance(c, dict) and c.get("id")]
    known = set(ids)
    covered: set[str] = set()
    for section, fields in SECTIONS.items():
        items = doc.get(section)
        if not isinstance(items, list):
            out.append(f"{section} is missing or not a list (an empty list is fine)")
            continue
        for n, item in enumerate(items):
            label = f"{section}[{n}]" + (f" ({item.get(fields[0])})" if isinstance(item, dict) and
                                         item.get(fields[0]) else "")
            if not isinstance(item, dict):
                out.append(f"{label} is not an object")
                continue
            for f in fields:
                if not str(item.get(f) or "").strip():
                    out.append(f"{label}: `{f}` is empty")
            used = item.get("used_by")
            if not isinstance(used, list) or not used:
                out.append(f"{label}: `used_by` must list the case ids that need it")
                used = []
            unknown = [u for u in used if str(u) not in known]
            if unknown:
                out.append(f"{label}: used_by names ids that are not cases in the suite: {_ids(unknown)}")
            covered |= {str(u) for u in used}
            if section == "secrets":
                leaked = [k for k in item if k.lower() in SECRET_VALUE_KEYS]
                if leaked:
                    out.append(f"{label}: a secret is listed by name only — remove {leaked}")
    fx = [i.get("id") for i in doc.get("fixtures") or [] if isinstance(i, dict)]
    bad = [f for f in fx if not RE_FIXTURE_ID.match(str(f or ""))]
    if bad:
        out.append(f"fixture ids must look like FX-01: {_ids(bad)}")
    dup = sorted({f for f in fx if fx.count(f) > 1})
    if dup:
        out.append(f"duplicate fixture ids: {_ids(dup)}")
    none = doc.get("no_prerequisites")
    if not isinstance(none, list):
        out.append("no_prerequisites is missing or not a list (an empty list is fine)")
        none = []
    none = {str(n) for n in none}
    unknown = sorted(none - known)
    if unknown:
        out.append(f"no_prerequisites names ids that are not cases in the suite: {_ids(unknown)}")
    both = sorted(none & covered)
    if both:
        out.append(f"listed in no_prerequisites AND in an item's used_by: {_ids(both)} — pick one")
    missing = [i for i in ids if i not in covered and i not in none]
    if missing:
        out.append(f"{len(missing)} case(s) are in no item's used_by and not in no_prerequisites: "
                   f"{_ids(missing)}")
    for row in (facts_ or {}).get("cases", []):
        if row["id"] in none and (row["placeholders"] or row["invented"]):
            why = f"placeholders {row['placeholders']}" if row["placeholders"] else "an invented value"
            out.append(f"{row['id']} is in no_prerequisites but carries {why} — it needs data")
    return out


def _ids(xs, limit: int = 12) -> str:
    xs = [str(x) for x in xs]
    return ", ".join(xs[:limit]) + (f" (+{len(xs) - limit} more)" if len(xs) > limit else "")


# ---- rendering -------------------------------------------------------------------------------------
def _cell(v) -> str:
    return " ".join(str(v or "").split()).replace("|", "\\|")


def _items(doc, section: str) -> list[dict]:
    items = doc.get(section) if isinstance(doc, dict) else None
    return [i for i in items if isinstance(i, dict)] if isinstance(items, list) else []


def render_md(feature: str, facts_: dict, doc) -> str:
    """`qa/prerequisites.md`: sections 1-3 and 8 from the plan (Python), 4-7 from the model's file."""
    changes = {c["id"]: c for c in facts_.get("changes", [])}
    L = [f"# Prerequisites — {feature}", "",
         "What has to be on prestag before this suite can run. Services, deploy order, migrations and smoke "
         "checks are computed from the Implementation Plan; config, external systems, secrets and test data "
         "are the `prerequisites` stage's reading of every case's precondition and test data, gate-checked "
         "against the suite. Fill `qa/test_data.template.yaml` with real values and pass it as "
         "`--test-data` on the next run.", ""]
    L += ["## 1. Services to deploy", ""]
    if facts_.get("services"):
        L += ["| Service | Changes | Owner | Cases that need it |", "|---|---|---|---|"]
        for s in facts_["services"]:
            owner = s["owner"] or ("external" if s["external"] else "this RFC")
            L.append(f"| {_cell(s['service'])} | {', '.join(s['changes'])} | {_cell(owner)} | "
                     f"{len(s['cases'])} |")
    else:
        L.append("No Implementation Plan in this run — services could not be derived.")
    L += ["", "## 2. Deploy order", ""]
    for n, wave in enumerate(facts_.get("waves", []), 1):
        L.append(f"{n}. " + ", ".join(f"{cid} ({changes[cid]['service']})" for cid in wave))
    L += ["", "## 3. Migrations", ""]
    L += [f"- {m['change']} {m['service']}: `{m['file']}`" for m in facts_.get("migrations", [])] or ["None."]

    L += ["", "## 4. Config and feature flags", ""]
    cfg = _items(doc, "config")
    if cfg:
        L += ["| Key | Service | Needed for testing | Cases |", "|---|---|---|---|"]
        L += [f"| `{_cell(c.get('key'))}` | {_cell(c.get('service'))} | {_cell(c.get('needed'))} | "
              f"{_cell(_ids(c.get('used_by') or [], 4))} |" for c in cfg]
    else:
        L.append("None.")
    L += ["", "## 5. External systems", ""]
    L += [f"- **{_cell(e.get('name'))}** — {_cell(e.get('needed_for'))} ({len(e.get('used_by') or [])} cases)"
          for e in _items(doc, "external_systems")] or ["None."]
    L += ["", "## 6. Secrets (names only — supply each as a secret flag, never in a file)", ""]
    L += [f"- **{_cell(s.get('name'))}** — {_cell(s.get('provide_via'))} ({len(s.get('used_by') or [])} cases)"
          for s in _items(doc, "secrets")] or ["None."]
    L += ["", "## 7. Test data fixtures", ""]
    fx = _items(doc, "fixtures")
    if fx:
        L += ["| Id | Entity | Required state | How to create | Suite uses today | Cases |",
              "|---|---|---|---|---|---|"]
        L += [f"| {_cell(f.get('id'))} | {_cell(f.get('entity'))} | {_cell(f.get('state'))} | "
              f"{_cell(f.get('how_to_create'))} | {_cell(', '.join(map(str, f.get('suite_values') or [])))} | "
              f"{_cell(_ids(f.get('used_by') or [], 4))} |" for f in fx]
    else:
        L.append("None.")
    L += ["", "## 8. Smoke checks after deploy", ""]
    smoke = [c for c in facts_.get("changes", []) if c["verify_on_staging"]]
    for c in smoke:
        L.append(f"- **{c['id']} {c['service']}**: " + " ".join(c["verify"].split()))
    if not smoke:
        L.append("The plan names no staging checks.")
    none = doc.get("no_prerequisites") if isinstance(doc, dict) else None
    L += ["", "## 9. Cases with no prerequisites", "",
          f"{len(none or [])}: {_ids(none or [], 30) or 'none'}", ""]
    notes = doc.get("notes") if isinstance(doc, dict) else None
    if isinstance(notes, list) and notes:
        L += ["## Notes", ""] + [f"- {_cell(n)}" for n in notes] + [""]
    return "\n".join(L)


def render_template(feature: str, doc) -> str:
    """`qa/test_data.template.yaml`: one entry per fixture and config key, `real_value` left empty for
    the team. JSON scalars are valid YAML, so values are emitted with `json.dumps` and need no YAML lib.
    Secrets are deliberately absent."""
    q = json.dumps
    L = [f"# Test data for {feature}. Fill every real_value with a record that exists on prestag, then",
         "# pass this file as --test-data on the next run. Secrets are NOT listed here: supply them as",
         "# secret flags. Generated from qa/prerequisites.json.", "", "fixtures:"]
    for f in _items(doc, "fixtures"):
        L += [f"  {f.get('id')}:",
              f"    entity: {q(str(f.get('entity') or ''))}",
              f"    state: {q(str(f.get('state') or ''))}",
              f"    used_by: {q([str(u) for u in f.get('used_by') or []])}",
              f"    suite_values: {q([str(v) for v in f.get('suite_values') or []])}",
              "    real_value: \"\""]
    L += ["", "config:"]
    for c in _items(doc, "config"):
        L += [f"  {q(str(c.get('key') or ''))}:",
              f"    service: {q(str(c.get('service') or ''))}",
              f"    needed: {q(str(c.get('needed') or ''))}",
              "    real_value: \"\""]
    return "\n".join(L) + "\n"
