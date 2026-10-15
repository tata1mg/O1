"""Derive a BE execution spec from the cases the generator already wrote.

Ported from test_agent tools/be_spec.py @ ca7d4ce. The step grammar (every regex, every drop reason, the
`_undelimit` escaped-slash fix) is UNCHANGED — tests/testing/test_be_spec.py holds the same 16 assertions
as the original script, so a grammar change would show up as a parity break. What changed:

  * the Kiwi read side (`confirmed_http_cases`, :222-240) now goes through the `CaseStore` protocol, so a
    spec derives from the JSON store with no server and no network;
  * `derive` takes a store rather than an `rpc` object, so callers no longer know about tcms;
  * there is no `main()` — the stage is `stages/derive_spec`.

`be_execute.run_case` consumes {extid, path, method, expect_status, assert[], require[]}.

This is a TRANSCRIPTION, not an interpretation. The generator's Test Step Granularity bar already requires
each step to be a line an SDET could transcribe into an assertion; this reads those lines back. It never
invents a route, a field or an expected value: a case whose steps cannot be parsed is reported and
dropped, because a spec entry guessed from prose is exactly the failure mode that produces a green result
proving nothing.
"""

from __future__ import annotations

import html
import re

# ---------------------------------------------------------------- step grammar
#
# Two shapes are accepted. The canonical one is what the skill asks for:
#
#     1. Send GET /radiology/v1/slots?lab_id=42
#     2. Assert status == 200
#     3. Assert body.departments is present
#     4. Assert body.departments.0.available == true
#
# The second is the Python-ish form earlier suites used, kept so existing features keep deriving:
#
#     2. Assert response.status_code == 200.
#     3. Assert response.json()["renew"] == true.
#     4. Assert response.json().get("is_renewal", false) == false.

_VERB = r"(GET|POST|PUT|PATCH|DELETE|HEAD)"

# The request line is unambiguous, so accept the phrasings suites actually use:
#   "Send GET /x"  ·  "POST /x with body {...}"  ·  "Issue a GET /x"  ·  "Call DELETE /x"
RE_SEND = re.compile(rf"^(?:\w+\s+)?(?:Send|Issue|Call|Make|Hit)?\s*(?:a\s+)?{_VERB}\s+(/\S*)", re.I)
# Same for the status: "Assert status == 200" · "Assert response.status_code == 200" · "Assert HTTP 200"
RE_STATUS = re.compile(
    r"\bAssert\s+(?:that\s+)?(?:the\s+)?(?:response[.\s]+)?"
    r"(?:HTTP\s+status\s+code|HTTP\s+status|status\s+code|status_code|status|HTTP)"
    r"\s*(?:==|=|is|of)?\s*(\d{3})\b", re.I)

# response.json()["a"]["b"]  /  response.json().get("a", <default>)  /  body.a.b
RE_PYPATH = re.compile(r"response\.json\(\)\s*((?:\[\s*[\"'][^\"']+[\"']\s*\]|\.get\(\s*[\"'][^\"']+[\"'][^)]*\))+)")
RE_BODYPATH = re.compile(r"\bbody\.([A-Za-z_][\w.\[\]0-9]*)")
RE_BAREPATH = re.compile(r"\bAssert\s+(?:that\s+)?([A-Za-z_][\w]*(?:\[\d+\])*(?:\.[\w]+(?:\[\d+\])*)*)", re.I)
# English that can start an assertion but is never a field name. Without this, "Assert the headline field
# == ..." would derive a field called `the`.
_STOPWORDS = {"the", "a", "an", "no", "none", "response", "body", "http", "status",
              "that", "there", "it", "this", "each", "every", "all", "both", "and", "or", "not"}
RE_SEGMENT = re.compile(r"\[\s*[\"']([^\"']+)[\"']\s*\]|\.get\(\s*[\"']([^\"']+)[\"']")

RE_PRESENT = re.compile(r"\bis\s+present\b", re.I)
# "is present and non-empty" must derive the stronger check, so do not require the "is".
RE_NONEMPTY = re.compile(r"\bnon[- ]?empty\b", re.I)
# The delimiter is /, so the pattern ends at the first / that is not escaped. A plain non-greedy .+? stops
# inside /^https:\/\// and transcribes `^https:\` — a trailing backslash that will not compile. Measured
# 2026-08-26: two cases derived that way and the executor aborted the run on the first of them, leaving
# 18 cases unrun.
RE_MATCHES = re.compile(r"\bmatches\s+/((?:[^/\\]|\\.)*)/", re.I)
RE_EQUALS = re.compile(r"==\s*(.+?)\s*$")

# a step that establishes state rather than asserting on the response
RE_REQUIRE = re.compile(r"^\s*(?:\d+\.\s*)?Require\b", re.I)

# "Send GET /radiology/v1/slots with lab_id=1042, skus=[...], start_date=2026-08-26"
# Params stated after the route instead of inside it. Deriving the bare path would issue a request missing
# required parameters — a 400 the executor would report as a FAIL that is not a defect. So they are either
# folded into the query string or the case is dropped.
RE_WITH_PARAMS = re.compile(r"\bwith\s+(.+)$", re.I)
RE_KV = re.compile(r"([A-Za-z_][\w.]*)\s*=\s*([^,]+?)(?=\s*,\s*[A-Za-z_][\w.]*\s*=|\s*$)")


def _clean(line: str) -> str:
    """Drop the leading number, markdown emphasis, the trailing period and trailing parens."""
    s = re.sub(r"^\s*\d+\.\s*", "", line.strip())
    s = s.replace("`", "").replace("**", "")         # real suites write `GET /x` and `body.a`
    s = re.sub(r"\s*\([^()]*\)\s*$", "", s)          # "(labs_agg/app/routes/cart.py:118)"
    return s.rstrip(". ").strip()


def _undelimit(pat: str) -> str:
    r"""Drop the backslash from `\/`. Inside /.../ that escape belongs to the delimiter, not to the
    pattern; every other escape is the case's own and is left untouched."""
    return re.sub(r"\\(.)", lambda m: "/" if m.group(1) == "/" else m.group(0), pat)


class _NoLit:
    def __repr__(self):
        return "<not-a-literal>"


_NOLIT = _NoLit()


def _literal(raw: str):
    """`true` -> True, `"x"` -> 'x', `12` -> 12. Returns _NOLIT when it isn't a literal."""
    s = raw.strip().rstrip(".").strip()
    s = re.sub(r"\s*\([^()]*\)\s*$", "", s).strip()
    low = s.lower()
    if low in ("true", "false"):
        return low == "true"
    if low in ("null", "none", "nil"):
        return None
    if len(s) >= 2 and s[0] in "\"'" and s[-1] == s[0]:
        return s[1:-1]
    try:
        return int(s)
    except ValueError:
        pass
    try:
        return float(s)
    except ValueError:
        return _NOLIT


_ROOTS = ("response.", "resp.", "body.", "payload.", "json.")


def _normalise_path(tok: str) -> str | None:
    """`response.slots[0].available` -> `slots.0.available`. None when it is transport, not payload."""
    low = tok.lower()
    for r in _ROOTS:
        if low.startswith(r):
            tok = tok[len(r):]
            break
    # `response.status_code` is the HTTP status, handled by RE_STATUS — never a body field.
    if tok.lower().split(".", 1)[0] in ("status_code", "status", "http_status", "code"):
        return None
    tok = tok.replace("[", ".").replace("]", "").strip(".")
    return tok or None


def _json_path(line: str) -> str | None:
    """Pull a dotted JSON path out of either accepted form."""
    m = RE_PYPATH.search(line)
    if m:
        segs = [a or b for a, b in RE_SEGMENT.findall(m.group(1))]
        return ".".join(segs) if segs else None
    m = RE_BODYPATH.search(line)
    if m:
        return _normalise_path(m.group(1))
    m = RE_BAREPATH.search(line)
    if m:
        tok = m.group(1)
        # Only accept something that actually looks like a field: dotted, or snake_case.
        if tok.lower() in _STOPWORDS or ("." not in tok and "_" not in tok):
            return None
        return _normalise_path(tok)
    return None


def parse_case(extid: str, text: str) -> tuple[dict | None, str]:
    """One case's step text -> one spec entry, or (None, reason it could not be derived)."""
    text = html.unescape(text or "")
    steps = [ln for ln in text.splitlines() if re.match(r"^\s*\d+\.", ln)]
    if not steps:
        return None, "no numbered steps"

    entry: dict = {"extid": extid, "assert": [], "require": []}
    for raw in steps:
        line = _clean(raw)

        m = RE_SEND.search(line)
        if m and "path" not in entry:
            entry["method"], entry["path"] = m.group(1).upper(), m.group(2)
            if "?" not in entry["path"]:
                mp = RE_WITH_PARAMS.search(line[m.end():])
                if mp:
                    pairs = RE_KV.findall(mp.group(1))
                    # a list or object value has no single unambiguous encoding — do not guess
                    if any(("[" in v or "{" in v) for _, v in pairs):
                        return None, ("request parameters are stated in prose and include a "
                                      "list/object — put the full query string in the path")
                    if pairs:
                        entry["path"] += "?" + "&".join(
                            f"{k}={v.strip().strip(chr(34) + chr(39))}" for k, v in pairs)
            continue

        m = RE_STATUS.search(line)
        if m:
            entry["expect_status"] = int(m.group(1))
            continue

        path = _json_path(line)
        if not path:
            continue

        target = entry["require"] if RE_REQUIRE.match(raw) else entry["assert"]
        if RE_NONEMPTY.search(line):
            target.append({"path": path, "non_empty": True})
        elif RE_PRESENT.search(line):
            target.append({"path": path, "present": True})
        elif (mm := RE_MATCHES.search(line)):
            target.append({"path": path, "matches": _undelimit(mm.group(1))})
        elif (mm := RE_EQUALS.search(line)):
            val = _literal(mm.group(1))
            if val is _NOLIT:
                continue                    # prose comparison — not transcribable
            target.append({"path": path, "equals": val})

    if "path" not in entry:
        return None, "no request step — the route is not stated"
    if "expect_status" not in entry and not entry["assert"]:
        return None, "no transcribable assertion (no status code, no field check)"
    if not entry["assert"]:
        # Status-only is legal but weak: a 200 carrying the wrong branch still passes.
        entry["_status_only"] = True
    return entry, ""


# ---------------------------------------------------------------- store read side
def confirmed_http_cases(store, feature: str, exec_tag: str = "exec:http") -> list[tuple[str, str]]:
    """(extid, text) for every CONFIRMED `exec:http` case in the feature's plan.

    Replaces the direct XML-RPC scan (test_agent be_spec.py:222-240). The two gates are the same two
    kiwi_runner applies, so a spec can never contain a case the executor would refuse.
    """
    return [(c.extid, c.text) for c in store.list_cases(feature, status="CONFIRMED", tags=(exec_tag,))]


def derive(store, feature: str, exec_tag: str = "exec:http",
           ) -> tuple[list[dict], list[tuple[str, str]], list[str], int]:
    """-> (spec, [(extid, why it was dropped)], [status-only extids], total exec:http cases)"""
    cases = confirmed_http_cases(store, feature, exec_tag=exec_tag)
    spec, dropped, weak = [], [], []
    for extid, text in cases:
        entry, why = parse_case(extid, text)
        if entry is None:
            dropped.append((extid, why))
            continue
        if entry.pop("_status_only", False):
            weak.append(extid)
        if not entry["require"]:
            entry.pop("require")
        spec.append(entry)
    return spec, dropped, weak, len(cases)


def report(spec: list[dict], dropped: list[tuple[str, str]], weak: list[str], total: int) -> list[str]:
    """The stdout report test_agent printed, as lines (so a stage can log AND persist them)."""
    lines = [f"derived : {len(spec)}/{total} exec:http cases"]
    if weak:
        lines.append("  status-only (no body assertion — passes on a 200 whatever the body says):")
        lines += [f"    {e}" for e in weak]
    if dropped:
        lines.append(f"  not derivable ({len(dropped)}):")
        lines += [f"    {extid:16s} {why}" for extid, why in dropped]
    return lines
