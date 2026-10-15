"""Deterministic gate helpers (M1 gates are simple Python asserts, not the full G0-G6 machinery).

Citation resolution: the RFC pipeline's claims cite `repo/path/to/file.py:123`. For repos that exist locally
under repos_root we verify the file exists and the line number is within the file; citations into non-local
repos are counted but only warned (can't verify without cloning — that's M2 ledger territory).
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

# Matches `some/path/file.py:123` AND bare `file.py:123`. The bare form MUST be matched: while this regex
# required a slash, a citation like `handlers.py:143` was invisible to every citation gate (resolution,
# qualification, provenance, witnessing) — i.e. dropping the path prefix was a free fabrication channel.
# Bare citations are now seen, and fail check_citations_qualified as unqualified.
CITATION_RE = re.compile(r"\b([A-Za-z0-9_\-]+(?:/[A-Za-z0-9_\-.]+)*\.[A-Za-z0-9_]+):(\d+)\b")

# extensions that make a `name.ext:line` token a real source citation — without this the bare-name branch
# would also match prose like "v1.2:3" or "Figure 4.1:2"
_CITE_EXTS = frozenset(
    "py js jsx ts tsx go java kt rb rs c h cc cpp hpp cs php swift m mm scala sql sh bash zsh yaml yml json "
    "toml ini cfg conf xml html css scss md rst txt tf tfvars gradle properties proto graphql lua pl r "
    "dart vue svelte erb haml jinja j2 tpl mk cmake dockerfile gemfile rakefile".split()
)


def _is_citation(rel: str) -> bool:
    """A `path:line` token counts as a citation only if its extension looks like source/config."""
    return rel.rsplit(".", 1)[-1].lower() in _CITE_EXTS


_CITE_TAIL_RE = re.compile(r"(?:[-,]\d+)+")


def iter_citations(text: str):
    """Every (rel_path, line_no, match) citation in text — the single place the citation shape is decided.

    Agents write multi-line citations (`builder.py:56,68-76,3022-3031`); CITATION_RE captures only the
    first number, so the tail's line numbers rode through every gate unvalidated — review caught six
    citations pointing past end-of-file that the deterministic gate had already passed (2026-08-12).
    Parse the [-,]-separated tail and yield EVERY endpoint, so out-of-range checks see them all."""
    for m in CITATION_RE.finditer(text):
        if not _is_citation(m.group(1)):
            continue
        yield m.group(1), int(m.group(2)), m
        tail = _CITE_TAIL_RE.match(text, m.end())
        if tail:
            for n in re.findall(r"\d+", tail.group(0)):
                yield m.group(1), int(n), m


# ---- Silent-omission scan (PROJ-3394 postmortem, 2026-08-14) --------------------------------------
# INGEST marks every degraded capture with one of these canonical markers (the ingest skill's
# contract). Case-sensitive on purpose: `UNREADABLE`/`FETCH FAILED` are the skill's exact spellings,
# and a case-insensitive match would fire on ordinary PRD prose ("the text was unreadable").
DEGRADED_MARKER_RE = re.compile(r"FETCH FAILED|\(not fetched\)|UNREADABLE|\[unverified(?:-copy)?\]")


# run-dir-relative artifact dirs: citations into these are INPUT citations (e.g. raw/prd.md:12 = PRD
# provenance), not repo citations — they resolve against the run dir and are exempt from repo checks
RUN_LOCAL_DIRS = ("raw", "findings", "_inputs", "out")

# first segments that are intra-repo top dirs, never repo names — a citation starting with one of these is
# UNQUALIFIED (missing its repo prefix), which breaks downstream provenance (found in the M2 smoke A/B)
_INTRA_REPO_TOPDIRS = ("app", "src", "lib", "db", "config", "gateway", "tests", "scripts", "mweb", "core")


_SKIP_DIRS = {".git", "node_modules", ".venv", "venv", "__pycache__", "dist", "build", ".mypy_cache", ".tox"}


def _find_by_basename(repos_root: Path, candidate_repos: list[str], basename: str, cache: dict) -> list[str]:
    """Repos-root-relative paths of every file named `basename` under the candidate repos (cached).
    Used to repair BARE citations (`search_manager.py:462`) — real but path-less, and dangerous precisely
    when the basename exists in several services."""
    if basename in cache:
        return cache[basename]
    hits: list[str] = []
    for r in candidate_repos:
        root = repos_root / r
        if not root.is_dir():
            continue
        for p in root.rglob(basename):
            if p.is_file() and not (_SKIP_DIRS & set(p.relative_to(root).parts)):
                hits.append(str(p.relative_to(repos_root)))
    cache[basename] = hits
    return hits


def qualify_citations(
    text: str, repos_root: Path, candidate_repos: list[str], evidence_paths: list[str] | None = None,
    witness_paths: list[str] | None = None,
) -> tuple[str, int, list[str]]:
    """Deterministically repair unqualified citations (linter --fix style): if `app/x.py` exists under
    exactly ONE candidate repo, rewrite it `<repo>/app/x.py`. When several candidates own the path, break
    the tie with LINE CONTEXT: if exactly one owner repo is named on the same line, it's that one (findings
    lines routinely name the repo beside an unqualified path). Returns (new_text, fixed_count, unresolved).
    Prompt compliance proved unreliable here; code carries the invariant. Still-ambiguous stays for the gate.

    SELF-PROVENANCE FIRST: before searching the filesystem, check whether THIS document already cites the
    same basename in qualified form. If it names exactly one such path, the bare token is shorthand for a
    path the agent itself already established, so resolve to it. That beats a filesystem guess — the doc's
    own citation was witnessed and verified, whereas a basename search ranges over repos the agent may
    never have opened. Measured 2026-07-27: a run failed on bare `order.rb:1134` / `order_manager.rb:1787`
    while the same file cited `svc_d/app/models/order.rb` 5x and
    `svc_d/app/services/order_module/order_manager.rb` 6x; the filesystem had 6 and 2 candidates
    respectively, so only the in-document evidence could resolve them.

    PROXIMITY over globality (2026-09-02, srp-bottomsheet-chat-history: 23 bare-citation gate failures,
    each with its qualified form nearby; industry basis: flexible deterministic resolution — aider's
    FLEXIBLE principle, Factory.ai's deterministic-autofix-first): when the DOCUMENT names several
    qualified paths for a basename (defeating the exactly-one rule above), the PARAGRAPH usually names
    exactly one — a bare cite is shorthand for the nearest preceding qualified citation of the same
    basename in the same paragraph (blank-line/heading delimited). Cross-paragraph proximity is
    deliberately NOT trusted (another paragraph's `index.js` may be a different file); the one
    sanctioned cross-paragraph repair is EXACT-LINE AGREEMENT — a qualified citation anywhere in the
    doc citing the same basename AT THE SAME LINE NUMBER names the same referent (the real residual
    specimen: bare `index.js:144-151` with `<full>/index.js:144-151` three sections up).

    PARTIAL intra-repo paths (`BookingAssistant/index.js:261` — first segment neither a repo nor a
    known top dir) previously rode through as 'non-local repo' warnings, i.e. unverified citations in
    a gated artifact. A UNIQUE suffix match under the in-scope repos repairs them; ambiguity stays
    for the gate's warning. Every repair branch keeps the hard line-count constraint."""
    fixed = 0
    unresolved: list[str] = []
    out_lines: list[str] = []
    basename_cache: dict[str, list[str]] = {}

    # basename -> the distinct qualified paths this document itself uses for it
    self_qualified: dict[str, set[str]] = {}
    # (basename, line_no) -> qualified paths cited at exactly that line somewhere in the doc
    exact_line: dict[tuple[str, int], set[str]] = {}
    for _rel, _n, _m in iter_citations(text):
        if "/" in _rel and _rel.split("/", 1)[0] not in _INTRA_REPO_TOPDIRS:
            self_qualified.setdefault(_rel.rsplit("/", 1)[1], set()).add(_rel)
            exact_line.setdefault((_rel.rsplit("/", 1)[1], _n), set()).add(_rel)
    # basename -> (path, lo, hi) for every qualified RANGE citation in the doc — feeds the
    # range-containment tier below (exact-line equality alone declined all 13 of v8's bare cites,
    # whose qualified forms were cited as spans like `.../index.js:116-140`)
    ranged: dict[str, list[tuple[str, int, int]]] = {}
    for _m in _CITE_SPEC_RE.finditer(text):
        _rel, _spec = _m.group(1), _m.group(2)
        if "/" not in _rel or _rel.split("/", 1)[0] in _INTRA_REPO_TOPDIRS or not _is_citation(_rel):
            continue
        for seg in _spec.split(","):
            lo, _, hi = seg.partition("-")
            ranged.setdefault(_rel.rsplit("/", 1)[1], []).append((_rel, int(lo), int(hi or lo)))

    # basename -> repo-relative paths the authoring run actually READ (its tool trace). Normalized
    # here so callers can pass ledger paths verbatim (absolute under repos_root, or already relative).
    witness_rel: dict[str, set[str]] = {}
    for _wp in witness_paths or []:
        try:
            _rp = str(Path(_wp).resolve().relative_to(repos_root.resolve()))
        except (ValueError, OSError):
            _rp = str(_wp).lstrip("/")
        if "/" in _rp:
            witness_rel.setdefault(_rp.rsplit("/", 1)[1], set()).add(_rp)

    # (basename, path) citations of the CURRENT paragraph, in reading order — the sub callback
    # appends as it walks each line left-to-right, so within-line ordering is naturally correct
    para_cites: list[tuple[str, str]] = []

    for line in text.splitlines(keepends=True):
        if not line.strip() or line.lstrip().startswith("#"):
            para_cites.clear()

        def _fix(m: re.Match, line: str = line) -> str:  # bind loop var (B023)
            nonlocal fixed
            rel, line_no = m.group(1), m.group(2)
            if not _is_citation(rel):
                return m.group(0)  # prose token like "v1.2:3"
            if "/" not in rel:
                # BARE citation: repair when exactly one file in the in-scope repos has that basename.
                # Observed 15 of these in a run that passed every gate — mostly real files written
                # path-lessly, but `search_manager.py:462` existed in 3 repos including two different
                # services, which is unresolvable shorthand exactly where precision matters.
                # EVERY branch filters candidates by line-count first: a candidate shorter than the
                # cited line cannot be the referent. v5 (2026-08-10) hit both failure shapes this
                # prevents — the self-provenance branch (then missing the check) rewrote contract.md's
                # `order.py:960` to the 232-line routes/svc_a/order.py (out-of-range gate failure the
                # harness itself injected), and rfc.md's own-set {managers, routes} made the old
                # exactly-one bail-out forfeit 33 resolvable citations to an LLM patch pass.
                def _fits(p: str) -> bool:
                    f = repos_root / p
                    return f.is_file() and _line_count(f) >= int(line_no)

                # PARAGRAPH PROXIMITY: the paragraph's own preceding qualified citations of this
                # basename — the author's most local witness beats every global rule. UNIQUE only:
                # a paragraph citing TWO different order.py files leaves a bare order.py genuinely
                # ambiguous, and nearest-preceding would be a guess (ambiguity stays honest).
                near = {p for bn, p in para_cites if bn == rel and _fits(p)}
                if len(near) == 1:
                    fixed += 1
                    return f"{next(iter(near))}:{line_no}"
                own = {p for p in self_qualified.get(rel, set()) if _fits(p)}
                if len(own) == 1:
                    fixed += 1
                    para_cites.append((rel, next(iter(own))))
                    return f"{next(iter(own))}:{line_no}"
                # EXACT-LINE AGREEMENT: a qualified citation elsewhere cites this basename at this
                # very line — same basename + same line = same referent, across paragraphs.
                ex = {p for p in exact_line.get((rel, int(line_no)), set()) if _fits(p)}
                if len(ex) == 1:
                    fixed += 1
                    para_cites.append((rel, next(iter(ex))))
                    return f"{next(iter(ex))}:{line_no}"
                # RANGE CONTAINMENT: exactly one qualified citation of this basename whose cited span
                # CONTAINS this line — same basename + line inside an established span = same referent
                # (v8 ground 2026-09-08: 13 bare `index.js:144`-style failures rode a 14.7-min full-model
                # revision while the doc cited each referent qualified as a range nearby; exact-line
                # equality declined them all). Unique-only; two containing spans stay honestly ambiguous.
                rc = {p for p, lo, hi in ranged.get(rel, []) if lo <= int(line_no) <= hi and _fits(p)}
                if len(rc) == 1:
                    fixed += 1
                    para_cites.append((rel, next(iter(rc))))
                    return f"{next(iter(rc))}:{line_no}"
                # WITNESS-TRACE resolution: the authoring run READ exactly one file with this
                # basename — the author's own trace beats document geometry (measured 2026-09-03,
                # srp-bottomsheet-chat-history-v2: 23 bare cites in constraint prose whose paragraph
                # qualified a DIFFERENT basename while the document carried several qualified paths
                # for this one, so every earlier branch honestly declined — yet the shard's Read
                # ledger named the referent uniquely in all 23). Unique-only, line-count hard.
                wt = {p for p in witness_rel.get(rel, set()) if _fits(p)}
                if len(wt) == 1:
                    fixed += 1
                    para_cites.append((rel, next(iter(wt))))
                    return f"{next(iter(wt))}:{line_no}"
                # EVIDENCE-TABLE resolution: when the run's verified evidence pack cites exactly one
                # path with this basename (that can hold the line), the shorthand means that path —
                # what the scan established beats file-system geometry.
                if evidence_paths:
                    ev = {p for p in evidence_paths if p.rsplit("/", 1)[-1] == rel and _fits(p)}
                    if len(ev) == 1:
                        fixed += 1
                        para_cites.append((rel, next(iter(ev))))
                        return f"{next(iter(ev))}:{line_no}"
                # the line-count constraint is HARD (a file shorter than the cited line cannot be the
                # referent — 24 gate failures, a 12-min LLM patch pass, 2026-08-07), so it filters the
                # candidate set before any tie-break, single hits included.
                hits = [h for h in _find_by_basename(repos_root, candidate_repos, rel, basename_cache)
                        if _fits(h)]
                if len(hits) > 1:
                    in_line = [h for h in hits if h.split("/", 1)[0] in line]
                    if len(in_line) == 1:
                        hits = in_line
                if len(hits) > 1:
                    # DOC-CITED tie-break: prefer the candidate the document itself names anywhere in
                    # qualified form. Kills geometry poisoning by never-mentioned files — v5's line-count
                    # break was globally defeated by a 9,688-line test mock (state_feeder_service
                    # tests/mock_service_clients/order.py) that out-lengthed every real candidate.
                    in_doc = [h for h in hits if h in text]
                    if len(in_doc) == 1:
                        hits = in_doc
                if len(hits) == 1:
                    fixed += 1
                    para_cites.append((rel, hits[0]))
                    return f"{hits[0]}:{line_no}"
                unresolved.append(
                    f"{rel}:{line_no} (bare; {'ambiguous: ' + ','.join(hits[:4]) if hits else 'no file found'})"
                )
                return m.group(0)
            if rel.split("/", 1)[0] not in _INTRA_REPO_TOPDIRS:
                if is_run_local(rel):
                    return m.group(0)
                if (repos_root / rel).is_file():
                    # already repo-qualified into a local checkout — register as a proximity witness
                    para_cites.append((rel.rsplit("/", 1)[1], rel))
                    return m.group(0)
                if (repos_root / rel.split("/", 1)[0]).is_dir():
                    return m.group(0)  # local repo but bad path — the gate's exists-check owns this
                # PARTIAL intra-repo path: repair on a unique suffix match under the in-scope repos
                base = rel.rsplit("/", 1)[1]
                suffix_hits = [
                    h for h in _find_by_basename(repos_root, candidate_repos, base, basename_cache)
                    if h.endswith("/" + rel) and _line_count(repos_root / h) >= int(line_no)
                ]
                if len(suffix_hits) == 1:
                    fixed += 1
                    para_cites.append((base, suffix_hits[0]))
                    return f"{suffix_hits[0]}:{line_no}"
                return m.group(0)  # ambiguous/absent/non-local: the gate's warning stays honest
            # same hard line-count constraint as the bare branch: an owner whose copy is shorter than
            # the cited line cannot be the referent, single owners included
            owners = [r for r in candidate_repos
                      if (repos_root / r / rel).is_file() and _line_count(repos_root / r / rel) >= int(line_no)]
            if len(owners) > 1:
                in_line = [r for r in owners if r in line]
                if len(in_line) == 1:
                    owners = in_line
            if len(owners) == 1:
                fixed += 1
                return f"{owners[0]}/{rel}:{line_no}"
            unresolved.append(f"{rel}:{line_no} ({'ambiguous: ' + ','.join(owners) if owners else 'no owner found'})")
            return m.group(0)

        out_lines.append(CITATION_RE.sub(_fix, line))
    return "".join(out_lines), fixed, unresolved


_BARE_PATH_RE = re.compile(r"`([A-Za-z0-9_\-]+(?:/[A-Za-z0-9_\-.]+)+)`")


def check_citations_qualified(text: str, source: str) -> list[str]:
    """Every repo citation must start with the repo name. 'app/foo.py:12' is unqualified — downstream
    stages can't resolve which repo it belongs to, and provenance matching breaks. A BARE filename
    ('foo.py:12') is worse: it names no path at all and used to slip past every citation gate."""
    failures: list[str] = []
    for rel, line_no, _ in iter_citations(text):
        if "/" not in rel:
            failures.append(
                f"{source}: bare citation with no path ({rel}:{line_no}) — cite the full "
                f"repo-qualified path (repo/dir/file.ext:line)"
            )
        elif rel.split("/", 1)[0] in _INTRA_REPO_TOPDIRS:
            failures.append(f"{source}: unqualified citation (missing repo prefix): {rel}:{line_no}")
    # multi-line citations now yield once per endpoint — identical failure lines would double the
    # retry guidance (the exact bloat report_bare=False exists to prevent)
    return list(dict.fromkeys(failures))


def is_run_local(rel: str) -> bool:
    return rel.split("/", 1)[0] in RUN_LOCAL_DIRS


def _line_count(f: Path) -> int:
    return sum(1 for _ in f.open(encoding="utf-8", errors="replace"))


# a citation's FULL line spec (`x.py:56,68-76`) — CITATION_RE alone stops at the first number
_CITE_SPEC_RE = re.compile(r"\b([A-Za-z0-9_\-]+(?:/[A-Za-z0-9_\-.]+)*\.[A-Za-z0-9_]+):(\d+(?:[-,]\d+)*)\b")


def check_citations(
    text: str, repos_root: Path, source: str, run_dir: Path | None = None, report_bare: bool = True
) -> tuple[list[str], list[str], int]:
    """Return (failures, warnings, resolved_count) for every `path.ext:line` citation in text.

    Run-local citations (raw/, findings/, ...) resolve against run_dir (when given). Otherwise the first
    path segment is the repo name: local repo -> file must exist and line <= file length; non-local repo ->
    warning only.

    `report_bare=False` suppresses the bare-citation failure for callers that already run
    check_citations_qualified over the same text — otherwise ONE bare citation yields TWO failure lines
    saying the same thing, and the retry guidance doubles in length (observed live: 17 defects -> 35 lines).
    Leave it True anywhere the qualified check does not run, or the bypass it closes reopens.
    """
    failures: list[str] = []
    warnings: list[str] = []
    resolved = 0
    for rel, line_no, _ in iter_citations(text):
        if "/" not in rel:
            # a bare filename names no repo and no path: unverifiable by construction. Failing here (not
            # only in check_citations_qualified) is what closes the bypass for stages that skip that check.
            if report_bare:
                failures.append(
                    f"{source}: bare citation with no path — cannot be verified: {rel}:{line_no}"
                )
            continue
        if is_run_local(rel):
            if run_dir is None:
                continue
            f = run_dir / rel
            if not f.is_file():
                failures.append(f"{source}: cited input file does not exist in the run dir: {rel}")
            elif line_no > _line_count(f):
                failures.append(f"{source}: input citation line out of range: {rel}:{line_no}")
            else:
                resolved += 1
            continue
        repo = rel.split("/", 1)[0]
        if not (repos_root / repo).is_dir():
            warnings.append(f"{source}: citation into non-local repo (unverified): {rel}:{line_no}")
            continue
        f = repos_root / rel
        if not f.is_file():
            failures.append(f"{source}: cited file does not exist: {rel}")
            continue
        try:
            n_lines = _line_count(f)
        except OSError as exc:
            failures.append(f"{source}: cited file unreadable: {rel} ({exc})")
            continue
        if line_no > n_lines:
            failures.append(f"{source}: citation line out of range: {rel}:{line_no} (file has {n_lines} lines)")
        else:
            resolved += 1
    # per-endpoint yields (multi-line citations) can repeat exists/bare failures verbatim — dedupe so
    # one defective citation stays one guidance line
    return list(dict.fromkeys(failures)), list(dict.fromkeys(warnings)), resolved


def check_citations_witnessed(text: str, repos_root: Path, ledger, source: str) -> list[str]:
    """Trace-backed citation check (M2): every LOCAL repo citation must have been witnessed in the scan
    trace — the cited path must appear somewhere in the stage's tool interactions. Run alongside
    check_citations (which verifies the file/line resolves); this catches fabrication the resolve check
    can't (a real file the agent never actually looked at)."""
    failures: list[str] = []
    for rel, _line_no, _ in iter_citations(text):
        if is_run_local(rel) or "/" not in rel:
            continue  # input citations (raw/prd.md) aren't code claims; bare paths fail check_citations
        repo = rel.split("/", 1)[0]
        if not (repos_root / repo).is_dir():
            continue  # non-local: resolve check already warns; can't demand trace-witness either
        if not ledger.witnessed(rel):
            # Distinguish "never touched" from "touched, but every receipt was discarded by the
            # own-artifact filter" (files_read records ALL reads, pre-filter). For the second case
            # the standard remedy is a LIVELOCK — re-reading is discarded for the same reason every
            # time (field incident 2026-08-20: a repo file named *_plan.md, 4 identical failures) —
            # so name the misclassification instead of advising a re-read that cannot succeed.
            from .ledger import own_artifact_match

            reads = [p for p in getattr(ledger, "files_read", ())
                     if str(p) == rel or str(p).endswith("/" + rel)]
            tok = own_artifact_match(rel) if reads else None
            if tok:
                failures.append(
                    f"{source}: citation witness MISCLASSIFIED by the harness (not your error): {rel} "
                    f"was read {len(reads)}x in the trace, but every receipt was discarded because the "
                    f"path trips the own-artifact filter (token {tok!r}). Re-reading CANNOT fix this — "
                    f"do not retry it; keep the citation only if a sibling file can witness the same "
                    f"claim, else leave it and flag. Operator: ledger._OWN_RE needs a carve-out here."
                )
            else:
                failures.append(
                    f"{source}: citation NOT witnessed in the scan trace (agent never touched it): {rel} — "
                    f"remedy: Read that file now and verify the claim (keeping or fixing the citation); do "
                    f"NOT try to inspect the scan trace or stream logs, they are not yours to parse"
                )
    return failures


def _appears_as_path(needle: str, corpus: str) -> bool:
    """True if `needle` occurs in `corpus` as a whole path token, not as an arbitrary substring. Guards the
    provenance check: a raw `needle in corpus` test let `foo/bar.py` match inside `barfoo/bar.py`, granting
    provenance a citation never actually had. The char before the match must not extend the path leftward."""
    start = 0
    while (i := corpus.find(needle, start)) != -1:
        before = corpus[i - 1] if i > 0 else ""
        if before not in _PATH_CHARS:
            return True
        start = i + 1
    return False


_PATH_CHARS = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-./")


def check_citations_provenanced(text: str, allowed_corpus: str, source: str) -> list[str]:
    """Provenance rule for no-code-access stages (GENERATE): every repo citation must already appear in the
    upstream artifacts (findings/) — a downstream stage may report citations, never mint them."""
    failures: list[str] = []
    for rel, _line_no, _ in iter_citations(text):
        if is_run_local(rel) or "/" not in rel:
            continue  # citing the inputs themselves is legitimate provenance; bare paths fail check_citations
        if _appears_as_path(rel, allowed_corpus):
            continue
        # distinguish minting from re-qualification: the path minus its repo prefix present upstream means
        # the upstream citation was unqualified (GROUND's defect, caught by check_citations_qualified there)
        suffix = rel.split("/", 1)[1] if "/" in rel else rel
        if suffix and _appears_as_path(suffix, allowed_corpus):
            failures.append(
                f"{source}: citation re-qualified downstream — upstream cited it WITHOUT the repo prefix: {rel}"
            )
        else:
            failures.append(f"{source}: citation has no upstream provenance (not in findings/raw): {rel}")
    return failures


# The original pattern only caught `key: "quoted16+"` — it missed every real-world credential shape
# (bearer tokens, JWTs, cloud keys, private-key blocks, connection strings), i.e. the ones that actually
# leak. Each alternative is a distinct credential family so a hit names what was found.
_SECRET_PATTERNS: tuple[tuple[str, re.Pattern], ...] = (
    ("assigned secret", re.compile(
        r"(?:api[_-]?key|secret|passwd|password|token|credential|authorization)"
        r"\s*[=:]\s*['\"]?[A-Za-z0-9+/_\-\.]{16,}['\"]?", re.IGNORECASE)),
    ("bearer token", re.compile(r"\bBearer\s+[A-Za-z0-9\-._~+/]{16,}", re.IGNORECASE)),
    ("JWT", re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}")),
    ("private key block", re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |PGP )?PRIVATE KEY-----")),
    ("AWS access key id", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("Atlassian/Bitbucket token", re.compile(r"\bATATT[A-Za-z0-9_\-=]{20,}")),
    ("Langfuse secret key", re.compile(r"\bsk-lf-[0-9a-f-]{8,}")),
    ("generic provider key", re.compile(r"\b(?:sk|rk|ghp|gho|ghs|xox[baprs])[-_][A-Za-z0-9]{16,}\b")),
    ("URL with inline credentials", re.compile(r"\b[a-z][a-z0-9+.\-]*://[^/\s:@]+:[^/\s@]{4,}@")),
)

# `Authorization: Bearer <TOKEN>` style placeholders are contract documentation, not leaks
_SECRET_PLACEHOLDER = re.compile(
    r"^(?:x{3,}|\.{3,}|<[^>]*>|\{\{?[^}]*\}?\}|\$\{?[A-Z_]+\}?|[A-Z_]*(?:TOKEN|SECRET|KEY|PASSWORD)[A-Z_]*|"
    r"your[-_a-z]*|redacted|example|placeholder|dummy|sample|changeme|todo|tbd|null|none)$",
    re.IGNORECASE,
)


# ---- Absence-claim + answer-symbol verification (radiology-run postmortem, 2026-07-28) -----------------
# The witness gate polices PRESENCE (a citation must be backed by a read in the trace) but nothing policed
# ABSENCE: findings claimed "confirmed absent by repo-wide grep" for three identifiers that exist
# (labs_agg DEFAULT_DAYS_FOR_TIME_SLOT, svc_d immediate_slot_without_privilege?, MultiSlotBookingManager
# #v5_book). One false absence poisoned a whole RFC section into conditional hedging and manufactured a
# spurious blocker-grade open question. These checks re-run the claim mechanically — milliseconds of
# git grep — so a false absence becomes a gate failure carrying the actual hits.

_ABSENCE_MARK_RE = re.compile(
    r"(?i)confirmed absent|zero (?:hits|matches)|no (?:matches|hits)|not found|does not exist|no such\b"
    r"|never (?:appears|mentioned|called)|nowhere in|absent from|nothing (?:matches|implements)"
    r"|not implemented anywhere|no existing"
)
_TICKED_TOKEN_RE = re.compile(r"`([A-Za-z][\w.?!#]{4,60})`")
_CAMEL_RE = re.compile(r"[a-z][A-Z]")


def _specific_identifier(tok: str) -> bool:
    """Only tokens that can plausibly name ONE thing are re-grepped: `DEFAULT_DAYS_FOR_TIME_SLOT` yes,
    `buffer`/`ceil`/`visibility` no — common words appear everywhere and would make every absence claim
    'false'. Specific = contains _ or . or is CamelCase, and is not a path:line citation (those have
    their own gates)."""
    if CITATION_RE.fullmatch(tok):
        return False
    return "_" in tok or "." in tok or bool(_CAMEL_RE.search(tok))


def _git_grep_hits(repos_root: Path, repos: list[str], token: str, per_repo: int = 2) -> list[str]:
    """repo-qualified `path:line` hits for a FIXED string across local checkouts. git grep over the index:
    fast (uses git's own pack machinery), tracked files only (skips build junk), immune to regex injection
    via -F."""
    import subprocess

    hits: list[str] = []
    for repo in repos:
        d = repos_root / repo
        if not (d / ".git").exists():
            continue
        try:
            out = subprocess.run(
                ["git", "-C", str(d), "grep", "-n", "-F", "-I", "--max-count", str(per_repo), token],
                capture_output=True, text=True, timeout=10,
            )
        except Exception:
            continue  # a broken checkout must not fail the gate machinery itself
        for ln in out.stdout.splitlines()[:per_repo + 2]:
            parts = ln.split(":", 2)
            # dotfile hits (.gitignore, .vscode/...) are configuration noise, never refutation of a
            # code-existence claim (measured FP: `smart_slots` "refuted" by its own .gitignore)
            if len(parts) >= 2 and not any(seg.startswith(".") for seg in parts[0].split("/")):
                hits.append(f"{repo}/{parts[0]}:{parts[1]}")
        hits = hits[: per_repo * 4]
    return hits


# the absence phrase must be ADJACENT to the token, not merely on the same line: precision was measured
# on the real radiology findings — line-level co-occurrence produced 10/10 false positives, every one a
# correct RELATIONAL claim ("no validation ON `employee_code`", "no check TIES `x` to `y`") whose named
# tokens rightly exist. Only "`TOKEN` ... absent/zero hits/does not exist" (within 60 chars, no other
# backtick between) or "no/no such `TOKEN`" assert the token ITSELF is absent.
# the window excludes `?`: "…an orchestration layer above `X`? Not found here" is a QUESTION whose
# answer follows — the absence is about the question's subject, never about X (measured FP, m11)
_ADJACENT_ABSENCE = (
    # `not found`/`absent` carry negative lookbehinds for quote characters: a QUOTED phrase after the
    # token is an error-message literal the code raises, not an absence claim about the token itself
    # (measured FP 2026-08-15: 'raising `BadRequestException` on "not found"/"not eligible"' matched,
    # scope widened past the prose repo, and a git-grep hit in auth failed a correct finding)
    r"`%s`[^`\n?]{0,60}?(?:does\s*not\s+exist|doesn't\s+exist|confirmed absent|(?<![\"'“])not found"
    r"|never (?:appears|mentioned|called)|nowhere|zero (?:hits|matches)|(?<![\"'“])absent)"
)
# (?!-\w): "no `order_group_id`-level grouping" is a claim about GROUPING at that level, not the token
_LEADING_NO = r"\bno(?:\s+such)?\s+`%s`(?!-\w)"
# a claim that narrows its own scope ("appears nowhere IN THIS REPO", "absent in DROPLET's vocabulary")
# cannot be refuted by a hit in some OTHER repo (measured: every residual false positive on the three
# real findings docs was scope-worded). Scope resolution: repo names in PROSE (not the `repo/` of a
# citation) narrow the grep to those repos; "this repo"/"its" resolves to the repo(s) the line's own
# citations point at; an unresolvable scope skips the line — precision over recall for a gate.
_SCOPED_CLAIM_RE = re.compile(r"(?i)\bthis repo|\bin its\b|\bwithin its\b|\bthat repo\b")


def _line_scope(line: str, repos: list[str]) -> list[str] | None:
    prose = [r for r in repos if re.search(rf"\b{re.escape(r)}\b(?!/)", line)]
    if prose:
        return prose
    if _SCOPED_CLAIM_RE.search(line):
        cited = {m.group(1).split("/")[0] for m in CITATION_RE.finditer(line) if "/" in m.group(1)}
        return sorted(cited & set(repos)) or None  # None: scope named but unresolvable — skip the line
    return repos


_VERIFY_HEADER = "## Symbol verification (deterministic, auto-generated)"


# ---- pre-review lint: the mechanically-checkable review-blocker classes (v7 postmortem, 2026-08-10) --
# Two of v7's five review blockers were pure lookups the composer skipped and a persona re-derived at
# ~10x the cost: a config/flag key referenced by the design that nothing provisions, and a symbol cited
# at a file that doesn't contain it. Checking them at GENERATE's own gate converts a ~20-minute review
# back-jump into a one-line patch item. Both follow the gate precision contract: fire only on proof.

# string literal inside a config/flag read — call forms AND kwarg forms (v7's actual blocker shape was
# `FeatureFlags.get_instance().evaluate(flag_key="LABS_29403_L1_UNSIGNED_URLS", ...)`)
_KEY_REF_RE = re.compile(
    r"(?:config\.get|has_flag|get_flag|CONFIG\.config\[)\s*\(?\s*[\"']([A-Z][A-Z0-9_.]{3,})[\"']"
    r"|(?:flag_key|config_key|feature_flag|flag)\s*=\s*[\"']([A-Z][A-Z0-9_.]{3,})[\"']"
)
# a nearby provisioning mention rescues the key: a CHANGE that adds/defines it, or names a config file
_PROVISION_RE = re.compile(
    r"(?i)\b(?:add|define|introduce|create|register|new key|provision)\b[^\n]{0,120}?%s"
    r"|%s[^\n]{0,120}?\b(?:config_template\.json|settings\.ya?ml|hydra|\(new key\)|— add)\b"
)


# ---- Gate self-description (0.23.0) -----------------------------------------------------------------
# Two facts every gate evaluation must carry beyond pass/fail (2026-08-19 observability audit):
# WHICH checks were silently disabled (a PASS with the witness gate inert was byte-identical in the
# trace to a fully-policed PASS), and WHAT CLASS each failure is (free-text failures made "how many
# runs failed on bare citations vs missing sections" a regex-archaeology question).

_inert_checks: list[str] = []


def note_inert(check: str) -> None:
    """A gate check disabled itself this evaluation (missing prerequisite artifact, e.g. no
    extract/requirements.json, no stream file for the witness gate). Drained by the runner into the
    evaluator observation + a `gate_<stage>_inert_checks` score."""
    _inert_checks.append(check)


def drain_inert() -> list[str]:
    """The inert checks noted since the last drain (deduped, order kept). Runner-called around each
    gate evaluation; gates run only on the main thread."""
    out = list(dict.fromkeys(_inert_checks))
    _inert_checks.clear()
    return out


# ordered, first match wins — severity/specificity first: secrets outrank everything (the audit
# found the harness's highest-severity failure classifying as `other`, indistinguishable from
# unparsed noise on a taxonomy dashboard), review classes before citation wording they may quote.
# COHERENCE CONTRACT: every wording engine._MECHANICAL_FAILURE_RE matches must classify to a
# non-"other" class here (pinned by test_gates.test_mechanical_failures_never_classify_as_other) —
# the two families drifting is how the patch fast-path and the trace taxonomy end up disagreeing
# about the same run.
_FAILURE_CLASSES: tuple[tuple[str, re.Pattern], ...] = (
    ("secrets_found", re.compile(r"possible secret")),
    ("review_blocker", re.compile(r"^review BLOCKER", re.IGNORECASE)),
    ("review_verdict", re.compile(r"review [Vv]erdict|verdict and findings must agree")),
    ("missing_output", re.compile(r"declared output missing")),
    ("missing_section", re.compile(r"required section missing")),
    ("missing_artifact", re.compile(r"\bmissing$|suspiciously thin|failure markers|FETCH FAILED",
                                    re.IGNORECASE)),
    ("citation_witness", re.compile(r"NOT witnessed")),
    ("citation_bare", re.compile(r"bare citation|unqualified citation|re-qualified downstream|"
                                 r"no upstream provenance")),
    ("citation_resolve", re.compile(r"line out of range|cited (?:file|input file) (?:does not exist|"
                                    r"unreadable)|does not appear anywhere in that")),
    ("absence_claim", re.compile(r"absence claim looks FALSE")),
    ("cross_doc", re.compile(r"cross-doc")),
    # output-shape lints (0.24.34 family) — previously fell into `other`, hiding the lint-churn
    # class from aggregation (PROJ-3394-v6: the same shape lint failed gate:generate#3/#4/#5 in one
    # revision loop, invisible as {other: 1} each time). Patterns quote the emitters verbatim
    # (generate/__init__.py): word caps, inline-citation-in-prose, self-narration, TL;DR lints,
    # unresolved optionality, and the compose-time byte ceiling.
    ("output_shape", re.compile(
        r"words \(cap \d+|inline citation .* in prose|composer self-narration|"
        r"TL;DR (?:is|carries)|unresolved optionality|bytes exceeds the [\d,]+-byte ceiling")),
    ("req_coverage", re.compile(r"REQ\(s\) never mentioned|capability subsection|capability_map|"
                                r"not groundable|no resolving local citation|coverage table")),
    ("key_provisioning", re.compile(r"provisioning step")),
)


def classify_failure(failure: str) -> str:
    """Machine class for one gate-failure string ('other' when nothing matches — the taxonomy grows
    by adding patterns, never by guessing)."""
    for cls, pat in _FAILURE_CLASSES:
        if pat.search(failure):
            return cls
    return "other"


def failure_classes(failures: list[str]) -> dict[str, int]:
    """{class: count} over a gate's failure list — the aggregable failure-taxonomy signal."""
    out: dict[str, int] = {}
    for f in failures:
        cls = classify_failure(f)
        out[cls] = out.get(cls, 0) + 1
    return out


_ANCHOR_RE = re.compile(r"\b(?:CHANGE|REQ)-\d+\b")


def blocker_anchors(failures: list[str]) -> list[str]:
    """Sorted unique CHANGE/REQ ids named in a gate's failure strings — the anchor-level grouping
    key. Exists because review's failure_classes is DEGENERATE (every persona blocker matches the
    single class `review_blocker`, so the histogram only restates the count — team, 2026-08-26,
    reading a live gate). Blockers are id-addressed by the review protocol, so a deterministic
    parse recovers the grouping: same-anchor across consecutive rounds = the blind-retry /
    descent signal, no text archaeology. Empty on gates whose failures carry no ids."""
    seen = {m for f in failures for m in _ANCHOR_RE.findall(f)}
    return sorted(seen, key=lambda a: (a.split("-")[0], int(a.split("-")[1])))


# ---- Seam checks (v2 forensics, 2026-09-03) --------------------------------------------------------
# Seven of the run's ten review blockers were one discipline the composer skipped: it designed three
# CHANGEs to `AIOverviewBottomsheet.js` from the evidence pack's 10-line header excerpt without ever
# opening the file, and its one grep for the component was scoped to the SRP flow directory — so it
# wrote "Blast radius: none" for a component two other flows also render. The reviewer found both
# with `cat -n` + one repo-wide `rg`. Both are deterministic; they belong to the harness, not prose.

# `- files: repo/path/x.js:13,57 (modify) · repo/path/new.js (new) · repo/y.py:105 (modify — note)`
_MODIFY_RE = re.compile(
    r"(?<![\w/])([A-Za-z0-9_\-]+(?:/[A-Za-z0-9_\-.]+)+\.[A-Za-z0-9_]+)(?::[\d,\-]+)?\s*\((?:modify|edit|change)\b",
    re.IGNORECASE,
)
# tests/stories are not mount points; neither is package plumbing (`__init__.py` re-exports — live replay
# 2026-09-03 flagged lab_buddy/widgets/__init__.py as an unmentioned "mount" of builder.py)
_IMPORTER_SKIP = re.compile(
    r"(^|/)(__tests__|tests?|specs?|stories|__mocks__|fixtures)(/|$)|\.(test|spec|stories)\.|(^|/)__init__\.py$", re.I
)


def plan_modified_files(plan_text: str) -> list[str]:
    """Repo-relative paths plan.md marks (modify), in order, deduped."""
    return list(dict.fromkeys(m.group(1) for m in _MODIFY_RE.finditer(plan_text)))


def file_importers(repos_root: Path, rel: str, cap: int = 12) -> list[str] | None:
    """Files in the same repo that RENDER the component in `rel` (JSX `<Stem`), repo-relative,
    tests/stories excluded. None = no verdict (not a component file, short/generic stem, >25 hits, no rg)."""
    repo = rel.split("/", 1)[0]
    root = repos_root / repo
    stem = Path(rel).stem
    if len(stem) < 4 or not root.is_dir():
        return None
    if Path(rel).suffix.lower() not in {".js", ".jsx", ".ts", ".tsx", ".vue", ".svelte"}:
        return None  # backend modules: importing a module is not depending on the changed symbol —
        # replayed 2026-09-03 on svc_a/labs_agg/athena plans, plain-import ledgers flagged 8-12 files
        # per manager; check_modified_symbol_callers owns that class
    q = re.escape(stem)
    # RENDER sites only: a JSX render is a mount point; a bare import (a hook, a util) is a symbol
    # dependency and belongs to the symbol-level ledger
    patterns = [rf"<{q}[\s/>]"]
    cmd = ["rg", "-l", "--no-messages"]
    for pat in patterns:
        cmd += ["-e", pat]
    cmd.append(str(root))
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=20).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    hits: set[str] = set()
    for line in out.splitlines():
        try:
            r = str(Path(line).relative_to(repos_root))
        except ValueError:
            continue
        if r != rel and not _IMPORTER_SKIP.search(r):
            hits.add(r)
    if len(hits) > 25:
        return None  # a generic stem (utils, index…) is noise, not a mount-point ledger
    return sorted(hits)[:cap]


# ---- Symbol-level callers (cross-run survey 2026-09-03) ---------------------------------------------
# The file-import ledger above covers "who renders this component". The backend form of the same
# blocker class is symbol-shaped: a shared route modified "with no caller-scoping" (PROJ-3394 x2), a
# resolver whose "two named call sites are NOT both wired" (radiology), a test double left on the old
# signature (task-29403), an unguarded .get() on "the shared, all-traffic GET /search/all" (ai-overview).
# So: for each symbol a CHANGE names in backticks that is DEFINED in one of the files it modifies,
# list its call sites elsewhere in the repo and require the plan/RFC to mention each caller file.
_SYMBOL_RE = re.compile(r"`([A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)?)(\([^`]*\))?`")
_CODE_SUFFIXES = {".py", ".rb", ".js", ".jsx", ".ts", ".tsx", ".go", ".java", ".kt", ".scala", ".php", ".swift"}
_SYMBOL_STOP = frozenset(
    "get set post put delete update create init main data value result response request item items list "
    "index config state props type name key id url path file text json true false none null self this "
    "error errors status code message payload options params args kwargs".split()
)
_CHANGE_SPLIT_RE = re.compile(r"^##\s+CHANGE-\w+\s*:", re.MULTILINE)


def _defined_in(text: str, symbol: str) -> bool:
    """`symbol` (last dotted segment) is DEFINED AS A CALLABLE in this file: def/class/function/method,
    or a const/let/var/export bound to a function or arrow. Plain assignments, object keys and YAML keys
    do not count — replay 2026-09-03: `skill_ids`, `params_attributes`, `DEFAULT` (settings.yml) and
    `entryPoint` were all "defined" by assignment and "called" from a dozen files each: pure noise."""
    name = re.escape(symbol.rsplit(".", 1)[-1])
    return re.search(
        rf"(?:^|\s)(?:async\s+)?(?:def|class|function)\s+{name}\b"
        rf"|(?:^|\s)(?:export\s+(?:default\s+)?)?(?:const|let|var)\s+{name}\s*=\s*(?:async\s*)?(?:\(|function\b|\w+\s*=>)"
        rf"|^\s*(?:async\s+)?{name}\s*\([^)]*\)\s*\{{",  # class-body method (JS)
        text, re.MULTILINE,
    ) is not None


def symbol_callers(repos_root: Path, rel: str, symbol: str, cap: int = 12) -> list[str] | None:
    """Repo-relative files (same repo, excluding `rel`, tests/stories/__init__) that call or reference
    `symbol`. None = no verdict (stoplisted/short symbol, >25 hits, rg unavailable)."""
    name = symbol.rsplit(".", 1)[-1]
    if len(name) < 6 or name.lower() in _SYMBOL_STOP:
        return None
    repo = rel.split("/", 1)[0]
    root = repos_root / repo
    if not root.is_dir():
        return None
    cmd = ["rg", "-l", "--no-messages", "-e", rf"\b{re.escape(name)}\s*\(", "-e", rf"dispatch\(\s*{re.escape(name)}\b",
           str(root)]  # call form only — passing a same-named variable is not a call site
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=20).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    hits: set[str] = set()
    for line in out.splitlines():
        try:
            r = str(Path(line).relative_to(repos_root))
        except ValueError:
            continue
        if r != rel and not _IMPORTER_SKIP.search(r):
            hits.add(r)
    if len(hits) > 25:
        return None
    return sorted(hits)[:cap]


def check_modified_symbol_callers(plan_text: str, rfc_text: str, repos_root: Path,
                                  source: str = "out/detail/plan.md") -> list[str]:
    """SYMBOL-LEVEL CALLER LEDGER: per CHANGE, every backticked symbol defined in one of its (modify)
    files has its in-repo call sites listed; each caller file must be mentioned in the plan or RFC."""
    corpus = plan_text + "\n" + rfc_text
    failures: list[str] = []
    chunks = _CHANGE_SPLIT_RE.split(plan_text)[1:]
    heads = re.findall(r"^##\s+CHANGE-(\w+)\s*:", plan_text, re.MULTILINE)
    for num, body in zip(heads, chunks, strict=False):
        mods = plan_modified_files(body)
        if not mods:
            continue
        symbols = list(dict.fromkeys(
            m.group(1) for m in _SYMBOL_RE.finditer(body)
            # callable-shaped only: written with parens, dotted (Class.method), or CamelCase (a class)
            if m.group(2) or "." in m.group(1) or re.match(r"[A-Z][a-z]+[A-Z]", m.group(1))
        ))
        reported = 0
        for rel in mods:
            f = repos_root / rel
            if not f.is_file() or f.suffix.lower() not in _CODE_SUFFIXES:
                continue
            try:
                ftext = f.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            for sym in symbols:
                if not _defined_in(ftext, sym):
                    continue
                callers = symbol_callers(repos_root, rel, sym)
                if not callers:
                    continue
                missing = [c for c in callers if c not in corpus and Path(c).name not in corpus]
                if missing:
                    failures.append(
                        f"{source}: CHANGE-{num} modifies `{sym}` in {rel}, which is also called from "
                        f"{len(callers)} file(s) the plan never mentions: {missing[:5]} — state what happens at "
                        f"EACH caller (unchanged, adapted, or why it is unaffected); cross-run 2026-09-03: a shared "
                        f"route/resolver modified without its callers scoped is the most repeated blocker class"
                    )
                    reported += 1
                    if reported >= 3:
                        break
            if reported >= 3:
                break
    return failures
