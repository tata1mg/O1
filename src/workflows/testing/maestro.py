"""Maestro results -> per-case outcomes: the attribution half of test_agent's `tools/maestro_publish.py`.

Ported verbatim (HEAD b3bd9d1) except for three things: the case-id pattern (`RE_TC`) is widened to the
generator's multi-part ids, the Kiwi calls live in the `publish_app` stage behind the `CaseStore` seam
instead of here, and `main()` is gone. Everything that decides a verdict — which flow covers which case
(the header's `# Covers:` block and labelled steps, never free prose), how a failure is attributed (FAIL
only when the failing step names the case; partial execution is BLOCKED; no timeline is ERROR), worst-wins
across flows — is the original's, with its reasoning kept in the docstrings below.
"""

from __future__ import annotations

import hashlib
import os
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

from defusedxml import ElementTree as ET  # the report is a file from a device run: parse it without entity expansion

#: A case id. test_agent's `TC-[A-Z]+-\d+` matched its own sheets' two-part ids (`TC-AC-03`) and none of the
#: generator's (`TC-ONEVOICE-ENTRY-FUNC-01`), so every flow covering a generated case published nothing.
RE_TC = re.compile(r"TC-[A-Z0-9]+(?:-[A-Z0-9]+)*?-\d+\b")
# `  Label:    "<the step label>"` inside the enriched <failure> body.
RE_FAILED_LABEL = re.compile(r'^\s*Label:\s*"(.*)"\s*$', re.M)
# `  12:34:56.789  <desc>  [COMPLETED]` inside <system-out>.
RE_TIMELINE = re.compile(r"^\s+(\d\d:\d\d:\d\d\.\d+)\s+(.*?)\s+\[([A-Z]+)\]\s*$", re.M)

OK_STATES = {"COMPLETED", "WARNED"}      # WARNED = soft/optional step, not a defect
BAD_STATES = {"FAILED", "ERROR"}

# Worst-wins when several flows touch one case. A defect outranks everything an
# environment or harness problem can tell us.
PRECEDENCE = {"PASS": 0, "BLOCKED": 1, "ERROR": 2, "FAIL": 3}

MAX_ATTACH = 8 * 1024 * 1024   # per-artifact ceiling for run attachments
MARKER_PREFIX = "maestro-publish-id:"


# --------------------------------------------------------------------------- #
# the report                                                                  #
# --------------------------------------------------------------------------- #
_FIELD_RX = re.compile(r"^\s{2}([A-Za-z][\w ]*?):\s+(.+?)\s*$", re.M)


def _named_fields(body: str) -> dict[str, str]:
    """`  Command:  tapOn (...)` -> {"Command": "tapOn (...)"}.

    maestro_report.py writes the evidence as aligned `  Key:  value` lines, so the
    useful parts (command, source flow, git sha, app version, artifact paths) can be
    lifted out without re-deriving anything.
    """
    return {k.strip(): v.strip() for k, v in _FIELD_RX.findall(body)}


def parse_report(path: Path) -> list[dict]:
    """One dict per <testcase>: flow name, outcome, failing step, step timeline."""
    root = ET.parse(path).getroot()
    suites = root.iter("testsuite") if root.tag == "testsuites" else [root]
    out = []
    for suite in suites:
        device = suite.get("device") or ""
        for tc in suite.iter("testcase"):
            fail = tc.find("failure")
            err = tc.find("error")
            skip = tc.find("skipped")
            bad = fail if fail is not None else err
            body = (bad.text or "") if bad is not None else ""
            sysout = "".join(e.text or "" for e in tc.iter("system-out"))

            m = RE_FAILED_LABEL.search(body)
            timeline = [(t, d, s) for t, d, s in RE_TIMELINE.findall(sysout)]
            props = {p.get("name"): p.get("value")
                     for p in tc.iter("property") if p.get("name")}
            out.append({
                "flow": tc.get("id") or tc.get("name") or "?",
                "file": tc.get("file") or "",
                "time": float(tc.get("time") or 0),
                # Maestro writes this on EVERY <testcase>, pass or fail -- unlike the
                # screenshot-epoch/artifact-dir fallbacks in run_finished_at(), it needs no
                # enrichment (which only touches FAILED testcases) to be present.
                "timestamp": tc.get("timestamp") or "",
                "device": device,
                "skipped": skip is not None,
                # A <failure> is an assertion (a defect); an <error> is the harness.
                "outcome": ("SKIPPED" if skip is not None else
                            "PASS" if bad is None else
                            "FAIL" if fail is not None else "ERROR"),
                "failed_label": m.group(1) if m else None,
                "message": (bad.get("message") or body.strip().split("\n")[0]
                            if bad is not None else ""),
                "timeline": timeline,
                "failure_body": body,
                "sysout": sysout,
                "fields": _named_fields(body),
                "props": props,
                "enriched": bool(m or timeline),
                "mock": props.get("verified-with-mock-data"),
            })
    return out


# --------------------------------------------------------------------------- #
# flow -> case ids                                                            #
# --------------------------------------------------------------------------- #
def covers_block(header: str) -> set[str]:
    """Case ids from the header's `# Covers:` block only.

    The block runs from the `Covers:` line to the next bare `#` separator, which is
    how these headers already delimit their sections.
    """
    ids: set[str] = set()
    collecting = False
    for line in header.split("\n"):
        # The colon is OPTIONAL: these headers are written both ways -- "# Covers:" with the
        # ids on following lines, and "# Covers TC-OS-01 (A), TC-PH-02 ..." inline on the same
        # line. Requiring the colon silently returned NO claims for every file using the second
        # form (5 of the order-placing flows), which only went unnoticed because their step
        # labels carry the ids and did the work instead.
        if re.match(r"^#\s*Covers\b", line, re.I):
            collecting = True
            ids |= set(RE_TC.findall(line))
            continue
        if collecting:
            if re.match(r"^#\s*$", line) or not line.lstrip().startswith("#"):
                break
            ids |= set(RE_TC.findall(line))
    return ids


def label_cases(body: str) -> list[tuple[str, set[str]]]:
    """(label text, case ids) for every `label:` line that names a case.

    Labels are frequently written as multi-line double-quoted YAML scalars:

        label: "TC-AS-02: the 1mg Assured badge on the Cart's selected-lab widget
                (matched via its accessibility label 'Assured Lab')"

    Reading only the first physical line truncated the text mid-sentence and left the
    opening quote attached, so the rendered evidence was unreadable AND the text no
    longer matched what Maestro logs for that step. Continuation lines are joined until
    the quote closes.
    """
    out = []
    lines = body.split("\n")
    i = 0
    while i < len(lines):
        line = lines[i]
        if "label:" not in line:
            i += 1
            continue
        text = line.split("label:", 1)[1].strip()
        q = text[0] if text[:1] in ('"', "'") else ""
        if q:
            # keep consuming while the scalar is still open
            while not (len(text) >= 2 and text.endswith(q)) and i + 1 < len(lines):
                i += 1
                text = text + " " + lines[i].strip()
            if len(text) >= 2 and text[0] == text[-1] == q:
                text = text[1:-1]
        ids = set(RE_TC.findall(text))
        if ids:
            out.append((" ".join(text.split()), ids))
        i += 1
    return out


def flow_claims(flows_root: Path) -> dict[str, dict]:
    """{flow_basename: {"covers": set, "labels": [(label, ids)], "path": Path}}"""
    claims: dict[str, dict] = {}
    for path in sorted(flows_root.rglob("*.yaml")):
        txt = path.read_text(errors="ignore")
        head, _, body = txt.partition("\n---")
        claims[path.stem] = {
            "covers": covers_block(head),
            "labels": label_cases(body or txt),
            "path": path,
        }
    return claims


# --------------------------------------------------------------------------- #
# attribution                                                                 #
# --------------------------------------------------------------------------- #
def _norm(s: str) -> str:
    """Compare a YAML label to a logged step description without tripping on quoting."""
    return re.sub(r"[^a-z0-9]+", "", s.lower())


def _covered(declared: str, seen: set[str]) -> bool:
    return any(declared and (declared in n or n in declared) for n in seen)


def attribute(run: dict, claim: dict | None) -> dict[str, tuple[str, str]]:
    """{case_id: (status, detail)} for one flow's result.

    PASS requires that EVERY labelled step naming the case reached a terminal OK state,
    not merely that one of them did. That distinction is the whole point, and a real run
    showed why: `ad-add-edit-delete-paginate` declares 12 steps naming TC-AD-01 and died
    on an unlabelled step after 3 of them, with the failing step naming no case at all.
    "Some step naming it completed" scores that PASS, recording a case whose add/edit/
    delete half never executed as verified. Partial execution is BLOCKED -- the case was
    not proven, and it was not proven broken either.
    """
    if claim is None:
        return {}
    covers, labels = claim["covers"], claim["labels"]
    all_ids = set(covers) | {i for _, ids in labels for i in ids}
    if not all_ids:
        return {}

    if run["skipped"]:
        return {c: ("BLOCKED", f"flow {run['flow']} was skipped by the runner")
                for c in all_ids}

    # Without a parseable failing step we cannot say which case broke, and guessing
    # would file a defect against code that may be fine.
    if run["outcome"] == "ERROR" or (run["outcome"] != "PASS" and not run["enriched"]):
        why = ("harness/driver error" if run["outcome"] == "ERROR"
               else "report carries no failing step -- was maestro_report.py run?")
        return {c: ("ERROR", f"{why}: {run['message'][:200]}") for c in all_ids}

    # A passing flow has NO timeline to check its steps against: maestro_report.py
    # enriches only FAILED testcases ("Passing testcases are left untouched"), so a
    # passing <testcase> carries no <system-out> at all. Running the completeness check
    # here found 0 of N declared steps and scored every passing flow's cases BLOCKED --
    # a false negative on the most common outcome, and the exact inverse of the false
    # PASS the check was added to prevent. Verified on as-assured-sheet-close-outside:
    # the flow passed, its TC-AS-01 step COMPLETED in maestro.log, and the case was
    # recorded BLOCKED "no step naming TC-AS-01 executed".
    if run["outcome"] == "PASS":
        return {c: ("PASS", f"flow {run['flow']} passed ({run['time']:.0f}s)")
                for c in all_ids}

    declared: dict[str, set[str]] = {}
    for text, ids in labels:
        for cid in ids:
            declared.setdefault(cid, set()).add(_norm(text))

    ran_ok: dict[str, set[str]] = {}
    ran_bad: dict[str, set[str]] = {}
    for _t, desc, st in run["timeline"]:
        n = _norm(desc)
        for cid in RE_TC.findall(desc):
            if st in OK_STATES:
                ran_ok.setdefault(cid, set()).add(n)
            elif st in BAD_STATES:
                ran_bad.setdefault(cid, set()).add(n)

    failed_ids = set(RE_TC.findall(run["failed_label"] or ""))
    # Kiwi HTML-escapes EVERY quote character it stores in an execution property:
    # `"` comes back as `&quot;`, `'` as `&#x27;`. Switching quote style does not avoid
    # it, so this is cosmetic in the stored record, not something to work around here.
    at = f" (flow failed at '{run['failed_label']}')" if run["outcome"] != "PASS" else ""

    out: dict[str, tuple[str, str]] = {}
    for cid in sorted(all_ids):
        decl = declared.get(cid, set())
        done = {d for d in decl if _covered(d, ran_ok.get(cid, set()))}

        if cid in failed_ids:
            # message already starts with the label (maestro_report builds it as
            # "<label>: <hypothesis>"), so printing both repeated the whole sentence.
            msg = run["message"]
            lbl = run["failed_label"] or ""
            if lbl and msg.startswith(lbl):
                msg = msg[len(lbl):].lstrip(": ").strip()
            out[cid] = ("FAIL", f"failed at step '{lbl}'"
                                + (f" — {msg[:200]}" if msg else ""))
        elif ran_bad.get(cid):
            out[cid] = ("FAIL", f"a step naming {cid} reached FAILED/ERROR")
        elif decl and done == decl:
            out[cid] = ("PASS", f"all {len(decl)} step(s) naming {cid} completed")
        elif decl and done:
            out[cid] = ("BLOCKED", f"partially exercised: {len(done)} of {len(decl)} "
                                   f"steps naming {cid} ran{at}; the rest never executed")
        elif decl:
            out[cid] = ("BLOCKED", f"no step naming {cid} executed{at}")
        elif run["outcome"] == "PASS":
            out[cid] = ("PASS", f"flow {run['flow']} passed ({run['time']:.0f}s); "
                                f"claimed via Covers: with no labelled step")
        else:
            out[cid] = ("BLOCKED", f"claimed via Covers: with no labelled step, so the "
                                   f"failure cannot be attributed to it{at}")
    return out


def case_steps(run: dict, claim: dict | None) -> dict[str, tuple[list[str], list[str]] | None]:
    """{case: (ran, never_ran)} -- or {case: None} when the answer is UNKNOWABLE.

    Maestro's report enriches only FAILED testcases, so a passing run carries no step
    timeline at all. Computing "ran vs never ran" from an absent timeline reported every
    step of a PASSING case as NEVER RAN, directly contradicting the PASS verdict printed
    six lines above it. None means "no timeline; do not pretend to know".
    """
    if not claim:
        return {}
    have_timeline = bool(run["timeline"])
    ran_norm: dict[str, set[str]] = {}
    for _t, desc, _st in run["timeline"]:
        for cid in RE_TC.findall(desc):
            ran_norm.setdefault(cid, set()).add(_norm(desc))
    out: dict[str, tuple[list[str], list[str]] | None] = {}
    for text, ids in claim["labels"]:
        for cid in ids:
            if not have_timeline:
                out[cid] = None
                continue
            cur = out.get(cid) or ([], [])
            ran, missed = cur
            (ran if _covered(_norm(text), ran_norm.get(cid, set())) else missed).append(text)
            out[cid] = (ran, missed)
    return out


def run_finished_at(fields: dict, duration_s: float | None,
                    timestamp: str | None = None) -> datetime | None:
    """When the flow ACTUALLY ended, as tz-aware UTC -- not when we got round to publishing.

    Kiwi derives `actual_duration` from stop_date - start_date, and `record_execution`
    used to default stop to `now()`. Publishing happens after the fact, so the recorded
    window landed ~20s late on a single-flow publish (and much later for a suite): the
    duration was right while both endpoints were wrong, which is why it went unnoticed.

    Three sources, best first:
      * the <testcase timestamp="..."> JUnit attribute -- Maestro writes this on every
        testcase, PASS or FAIL, so unlike the two sources below it needs no enrichment
        (which only touches FAILED testcases, see parse_report) and no failure screenshot
        to exist. Confirmed 2026-09-10: with report enrichment broken on the installed
        Maestro CLI, this was the ONLY source below still returning a value at all, which
        is why a 2h+ dispatch run showed a Kiwi TestRun with `start_date`/`stop_date` both
        None -- neither of the other two sources ever fired, for a single case, all run.
        DEVICE-LOCAL wall clock for when that testcase started; add its own duration for
        the finish instant.
      * the failure screenshot's own epoch-ms, embedded in Maestro's filename
        (`screenshot-X-1788367623400-(flow).png`) -- the exact instant of failure;
      * else the artifact dir name (`2026-09-02_221556`), which is DEVICE-LOCAL wall clock
        for when maestro started, plus the flow duration. Less precise: the dir is created
        at CLI launch, a few seconds before the flow's first step.
    """
    if timestamp:
        try:
            local = datetime.strptime(timestamp[:19], "%Y-%m-%dT%H:%M:%S").astimezone()
            return (local + timedelta(seconds=float(duration_s or 0))).astimezone(UTC)
        except ValueError:
            pass
    shot = fields.get("screenshot") or ""
    m = re.search(r"-(\d{13})-", os.path.basename(shot))
    if m:
        return datetime.fromtimestamp(int(m.group(1)) / 1000, tz=UTC)
    stamp = Path(shot).parent.name if shot else ""
    m = re.fullmatch(r"(\d{4}-\d{2}-\d{2})_(\d{2})(\d{2})(\d{2})", stamp)
    if m and duration_s:
        local = datetime.strptime(f"{m.group(1)} {m.group(2)}:{m.group(3)}:{m.group(4)}",
                                  "%Y-%m-%d %H:%M:%S").astimezone()
        return (local + timedelta(seconds=float(duration_s))).astimezone(UTC)
    return None


def executed_steps_section(sysout: str, tzname: str = "") -> list[str]:
    """Every step the flow actually ran, as a readable list placed BEFORE the per-case one.

    A developer debugging a failure needs the sequence that led to it, not just the subset
    of steps that happen to carry a case id. This timeline was always being published --
    but inside a fenced block ~90 lines down, after the ranked hypotheses, while the top of
    the comment announced "Steps naming TC-PD-01 - 0 of 8 executed". That reads as "nothing
    ran" when in fact 25 steps ran and the 24th failed. Same data, useful position.

    Case labels still decide the STATUS (FAIL only when the failing step names the case, so
    a setup failure is never filed as a defect against an untested case) -- but they should
    not decide what evidence a human gets to see.
    """
    rows = RE_TIMELINE.findall(sysout or "")
    if not rows:
        return []
    mark = {"COMPLETED": "✅", "WARNED": "⚠️", "SKIPPED": "⏭️", "FAILED": "❌", "ERROR": "❌"}
    bad = sum(1 for _, _, st in rows if st in BAD_STATES)
    head = f"### Steps the flow ran — {len(rows)} total"
    if bad:
        head += f", {bad} failed"
    # Maestro stamps its log in DEVICE-LOCAL wall clock. Saying so inline is the difference
    # between a reader trusting these times and re-deriving them against Kiwi's own dates.
    if tzname:
        head += f" · times in {tzname}"
    out = [head, ""]
    # A cap, because Kiwi renders the whole comment on one page and a long suite flow can
    # run hundreds of steps. The tail is what matters when something failed, so keep it.
    LIMIT = 120
    shown = rows if len(rows) <= LIMIT else rows[: 20] + [("", "...", "")] + rows[-(LIMIT - 20):]
    for stamp, text, status in shown:
        if text == "..." and not status:
            out.append(f"- _… {len(rows) - LIMIT} steps omitted …_")
            continue
        icon = mark.get(status, "·")
        if status in BAD_STATES:
            out.append(f"- {icon} `{stamp}` **{text} — {status}**")
        else:
            out.append(f"- {icon} `{stamp}` {text}")
    out.append("")
    return out


def run_attachment_name(cid: str, flow: str, path: Path) -> str:
    """A self-describing filename for the RUN-level copy of one artifact.

    Evidence must land in a section Kiwi actually RENDERS, and only one per-case section
    qualifies. Probed against the running instance (2026-09-02):
      * `TestExecution.list_attachments` is called nowhere in Kiwi's UI bundle, and a run
        page's rendered HTML contains no `testruns_testexecution` reference, no
        `get_comments` and no JSON-RPC call -- so an attachment on the EXECUTION is stored
        correctly and displayed nowhere. That is why the screenshot looked missing.
      * `TestCase.list_attachments` IS called by the bundle, and `/case/<id>/` has an
        Attachments section -- so the case is where a human can actually see it.

    The name carries case + flow + the run's artifact-dir timestamp because that section
    shows nothing but filenames, and because a case's attachments are shared across every
    run and build: without the timestamp, successive failures would be indistinguishable,
    and a fixed name plus de-duplication would pin the section to the FIRST failure's
    screenshot and quietly go stale. Unique names also stop Kiwi mangling collisions into
    `maestro_qWGCM4Y.log`.
    """
    stamp = path.parent.name or "run"
    return f"{cid}--{flow}--{stamp}{path.suffix}"


def with_evidence_links(body: str, attached: list[tuple[str, str, Path]],
                        urls: dict[str, str]) -> str:
    """Append an evidence section carrying each artifact's server URL **and** local path.

    Both, deliberately. The URL is durable and shareable -- a reviewer on another machine
    can open the failure screenshot straight from the execution. The local path is still
    the fastest thing to open while debugging on the machine that produced it. And neither
    is sufficient alone: `~/.maestro/tests/` is pruned after 14 days, so the path rots,
    while the URL says nothing about where to look on this box.

    Why this exists at all: the ARTIFACTS block inside the enriched report's evidence dump
    prints local paths only, so an execution *looked* like it had nothing but a path even
    though the bytes were attached -- the attachment was simply never linked from the
    comment, and Kiwi's execution view does not surface attachments prominently.

    The section is inserted BEFORE the trailing `maestro-publish-id:` marker, because the
    idempotency check reads the comment's LAST line -- appending after it would make every
    re-publish look like fresh evidence and stack duplicates.
    """
    if not attached:
        return body
    label = {"screenshot": "screenshot", "log": "maestro.log", "commands": "commands JSON"}
    lines = ["", "### Evidence files (attached to this execution)", ""]
    for key, stored, path in attached:
        url = urls.get(key)
        name = label.get(key, key)
        # `stored` is the name the file went up under, not the generic on-disk one -- it is
        # what a reader will see in Kiwi's attachments panel, so the link has to match.
        if url:
            lines.append(f"- **{name}** — [{stored}]({url})")
        else:
            # Attached, but the URL read-back failed; say so rather than implying a link.
            lines.append(f"- **{name}** — `{stored}` (attached; URL read-back failed)")
        lines.append(f"  - local: `{path}`")
    shot = urls.get("screenshot")
    if shot:
        # Kiwi serves attachments as `text/plain`, not `image/png`, so this inline image
        # may not render in every browser. It is a bonus on top of the link above, which
        # is the form that always works -- never the only way to reach the screenshot.
        lines += ["", f"![failure screenshot]({shot})"]
    parts = body.rsplit("\n", 1)
    if len(parts) == 2:
        head, marker_line = parts
        return head + "\n" + "\n".join(lines) + "\n\n" + marker_line
    return body + "\n" + "\n".join(lines)


def evidence_comment(cid: str, status: str, detail: str, run: dict,
                     steps, build: str, env: str, report_name: str,
                     declared: list[str] | None = None) -> str:
    """The full evidence block, posted as an execution comment.

    Kiwi renders comments as Markdown -- headings, bullets and fenced blocks all come out
    properly, with syntax highlighting on the fences. Two hard-won constraints:

    * The idempotency marker goes at the END, after a blank line. Putting it on the first
      line, directly above the `##` heading, collapsed the ENTIRE comment into one
      run-on paragraph: this renderer does not let an ATX heading interrupt a paragraph,
      so every heading, bullet and code fence after it was swallowed. One missing blank
      line destroyed all the formatting.
    * Only constructs seen to render here are used: `##`/`###`, `-` bullets, `` ` ``
      spans and ``` fences. No Markdown tables and no `<details>` -- neither is verified
      against this renderer, and unrendered markup reads worse than the plain form.
    """
    f = run["fields"]
    props = run.get("props") or {}
    L = [f"## {cid} — {status}", "",
         f"- flow: `{run['flow']}` ({run['outcome']}, {run['time']:.0f}s)",
         f"- device/env: {run['device'] or '?'} / {env}",
         f"- build: `{build}`",
         f"- report: `{report_name}`",
         f"- flow file: `{run['file'] or '?'}`"]
    # `tags` is emitted by Maestro itself on every <testcase>; `login-account` is stamped by
    # this repo's dispatcher (scripts/lib/stamp_account_property.py) on a parallel-dispatch
    # run only -- a bare single-device `maestro test` never adds it, since there the account
    # is whatever the person running it already knows. Surfacing both here means a failure in
    # a dispatched run can be debugged from the Kiwi comment alone, without cross-referencing
    # dispatch.log by hand for which pool/cohort account was signed in when it ran.
    if props.get("tags"):
        L.append(f"- tag: {props['tags']}")
    if props.get("login-account"):
        L.append(f"- login-account: {props['login-account']}")
    # Spell out which clock each timestamp uses. Maestro names its artifact dirs in
    # DEVICE-LOCAL time (so the attachment filenames read e.g. 194730 = 19:47 IST) while
    # Kiwi stores and returns comment/execution dates in UTC -- a +5:30 gap here that reads
    # as "the report disagrees with my system clock" until someone says otherwise.
    fin = run_finished_at(f, run.get("time"), run.get("timestamp"))
    tzname = (fin.astimezone().strftime("%Z") if fin
              else datetime.now().astimezone().strftime("%Z"))
    if fin:
        loc = fin.astimezone()
        L.append(f"- finished: {loc.strftime('%Y-%m-%d %H:%M:%S %Z')} "
                 f"({fin.strftime('%H:%M:%S')} UTC)")
    L.append(f"- result: **{status}** for this case — flow outcome {run['outcome']}"
             + (f", {run['time']:.0f}s" if run.get("time") else ""))
    shot_dir = Path(f.get("screenshot", "")).parent.name if f.get("screenshot") else ""
    if shot_dir:
        L.append(f"- run artifacts: `{shot_dir}` — that stamp, and the attachment "
                 f"filenames, are DEVICE-LOCAL time; Kiwi's own dates on this page are UTC")
    if f.get("git"):
        L.append(f"- git: {f['git']}")
    if f.get("appId"):
        L.append(f"- appId: {f['appId']}")
    if run["mock"]:
        L.append(f"- ⚠️ VERIFIED WITH MOCK DATA: {run['mock']}")
    L += ["", f"**Why this verdict:** {detail}", ""]

    if steps is None:
        # A passing run has no timeline. Say what the flow asserts, and say plainly that
        # per-step confirmation is unavailable -- rather than inventing NEVER RAN marks.
        if declared:
            L += [f"### What this flow asserts for {cid}", ""]
            for t in declared:
                L.append(f"- {t}")
            L += ["", "_Per-step timing is not shown: Maestro enriches only FAILED "
                      "testcases, so a passing run records no step timeline. The flow "
                      "passed, which means every non-optional step above completed._", ""]
    else:
        ran, missed = steps
        # The full run first -- what a developer needs to debug -- then the per-case subset
        # that explains the verdict. Order matters: the subset alone misleads (see
        # executed_steps_section).
        L += executed_steps_section(run["sysout"], tzname)
        total_ran = len(RE_TIMELINE.findall(run["sysout"] or ""))
        L += [f"### Which of {cid}'s own assertions were reached — "
              f"{len(ran)} of {len(ran) + len(missed)}", ""]
        for t in ran:
            L.append(f"- ✅ {t}")
        for t in missed:
            L.append(f"- ⬜ **never reached** — {t}")
        if total_ran and not ran:
            L += ["", f"_The flow ran {total_ran} steps (listed above) and failed before "
                      f"reaching any step that carries {cid}'s id. That is why this is "
                      f"BLOCKED rather than FAIL: nothing this case asserts was tested. "
                      f"The failing step and its screen dump are below._"]
        L.append("")

    if run["failure_body"]:
        L += ["### Failure evidence (derived from Maestro's runtime artifacts)", "",
              "```", run["failure_body"].strip()[:12000], "```", ""]
    if run["sysout"]:
        L += [f"### Step timeline and on-screen text — times in {tzname} (device-local)", "",
              "```", run["sysout"].strip()[:12000], "```", ""]

    # Marker LAST, after a blank line — see the docstring. It carries a digest of the
    # body because flow@build alone is NOT a sufficient identity: the same build produces
    # different evidence once a flow is fixed (an uncommitted YAML edit leaves the sha
    # untouched). Keyed on flow@build, a re-run updated the execution to PASSED while
    # skipping the new comment as "already present", leaving a PASSED execution whose
    # only evidence described the earlier FAIL.
    body = "\n".join(L)
    digest = hashlib.sha1(body.encode()).hexdigest()[:8]
    return body + f"\n\n{MARKER_PREFIX} {run['flow']}@{build}@{digest}"


def merge(per_flow: list[tuple[str, dict[str, tuple[str, str]]]]
          ) -> dict[str, tuple[str, str, list[str], str]]:
    """Worst-wins across flows -> {case: (status, detail, [flows], deciding_flow)}

    `deciding_flow` matters because several flows can claim one case and disagree. In
    slot-selection, TC-SL-04 is claimed by both `sl-persist-committed-roundtrip` (green)
    and `sl-persist-on-back` (a defect reproducer that fails by design). Worst-wins picks
    the FAIL, but the caller previously attached evidence from whichever flow appeared
    first in the report -- so the record could carry a FAIL verdict next to the passing
    flow's failing step, duration and artifacts. Naming the winner keeps the verdict and
    the evidence describing the same run.
    """
    agg: dict[str, tuple[str, str, list[str], str]] = {}
    for flow, cases in per_flow:
        for cid, (st, detail) in cases.items():
            if cid not in agg:
                agg[cid] = (st, detail, [flow], flow)
                continue
            cur_st, cur_detail, flows, winner = agg[cid]
            flows = flows + [flow]
            if PRECEDENCE[st] > PRECEDENCE[cur_st]:
                agg[cid] = (st, detail, flows, flow)
            else:
                agg[cid] = (cur_st, cur_detail, flows, winner)
    return agg


def read_blocked(path: Path | None) -> dict[str, str]:
    """Curated exclusions: `TC-XX-NN  <reason>` per line, `#` comments ignored.

    A prose file is not accepted on purpose — grepping case ids out of narrative text
    is how a case that is merely *discussed* becomes a case that is *excluded*.
    """
    if not path:
        return {}
    out = {}
    for raw in path.read_text(errors="ignore").split("\n"):
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        m = re.match(rf"^({RE_TC.pattern})\s*[:\-\s]\s*(.*)$", line)
        if m:
            out[m.group(1)] = m.group(2).strip() or "curated exclusion"
        elif RE_TC.fullmatch(line):
            out[line] = "curated exclusion"
    return out
