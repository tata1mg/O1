"""Read-only shell classifier: can we PROVE this command only reads?

WHY (v5 postmortem, 2026-08-10): the Claude CLI decomposes compound commands and headless auto-denies any
segment matching no allow rule. A curated PREFIX allowlist must omit awk/sed/find (as prefixes they would
admit `sed -i`, `find -delete`, `awk system()`), so pure text-reading pipelines like
`grep -n "def " file | awk -F: '$1<5900'` died at the ask — 14 denials in one run, and three runs' worth of
"avoid piping/chaining" memory notes that demonstrably never changed model behaviour. Facts about
permission walls belong in the environment, not in advisory prose. This classifier inspects the FULL
command (flags and all), which prefix rules cannot, and approves only what it can prove is read-only.

Precision contract (SWE-agent ACI rule — a guardrail must only fire when it is definitely right):
  ALLOW   every segment of the compound is in the verified read-only set;
  DENY    `find /` (root-scoped walk — denied anyway; instant instructive feedback beats a silent
          headless timeout), and, inside a sandbox, the classes that are NEVER approvable headless;
  ASK     everything else — no opinion; the transport's own allowlist/ask flow decides.

Pure functions only. `never_reason` takes the broker command and repos root as ARGUMENTS because the
reason strings must name the sanctioned alternative, and only the transport knows where it lives.
"""

from __future__ import annotations

import re
import shlex

# simple commands whose read-only-ness doesn't depend on flags
SAFE = frozenset({
    "grep", "egrep", "fgrep", "rg", "cat", "head", "tail", "wc", "ls", "echo", "printf",
    "sort", "uniq", "cut", "tr", "diff", "comm", "paste", "stat", "file", "basename",
    "dirname", "pwd", "which", "nl", "column", "true", "date", "test", "[", "cd",
})
GIT_READ = frozenset({"log", "show", "diff", "grep", "status", "blame", "branch", "rev-parse",
                      "shortlog", "describe", "ls-files"})
# awk write/exec escapes: system(), print/printf redirection or piping inside the program, gawk in-place
_AWK_UNSAFE = re.compile(r"system\s*\(|print\w*\s*>|>\s*\"|\|\s*\"|(?:^|\s)-i\b|--in-place")
_FIND_UNSAFE = re.compile(r"(?:^|\s)-(?:delete|exec|execdir|ok|okdir|fprint\w*|fls)\b")
# command substitution ($( but not $(( arithmetic) and backticks run arbitrary commands
CMD_SUBST = re.compile(r"\$\((?!\()|`")
# stderr-silencing redirects are read-only-harmless and ubiquitous (`2>/dev/null`, `2>&1`)
_STDERR_NOISE = re.compile(r"\s*\d?>&\d\b|\s*\d>\s*/dev/null\b")

FIND_ROOT_REASON = (
    "find over / is always denied — scope it to a repo checkout or the run dir "
    "(e.g. `find <repos_root>/<repo> -name ...`), or use Glob/rg --files."
)


def strip_stderr_noise(cmd: str) -> str:
    """Drop `2>/dev/null` / `2>&1` so they don't trip the conservative redirect defer in `segments`."""
    return _STDERR_NOISE.sub("", cmd)


def segments(cmd: str) -> list[str] | None:
    """Split a compound on |, &&, ||, ;, newline outside quotes. None = unparseable -> no opinion.

    A `<`/`>` anywhere returns None: redirects and heredocs are not this classifier's call — the
    allowlist and the write guard decide those.
    """
    segs: list[str] = []
    cur: list[str] = []
    q: str | None = None
    i, n = 0, len(cmd)
    while i < n:
        c = cmd[i]
        if q:
            if c == q:
                q = None
            cur.append(c)
        elif c in "'\"":
            q = c
            cur.append(c)
        elif c in ";\n" or (c == "|" and cmd[i + 1 : i + 2] != "|") or cmd[i : i + 2] in ("&&", "||"):
            segs.append("".join(cur).strip())
            cur = []
            if cmd[i : i + 2] in ("&&", "||"):
                i += 1
        elif c in "<>":
            return None
        else:
            cur.append(c)
        i += 1
    if q:
        return None
    segs.append("".join(cur).strip())
    return [s for s in segs if s]


def _strip_expansions(s: str) -> str:
    return re.sub(r"\$\(\([^)]*\)\)|\$\{[^}]*\}|\$\w+", "", s)


def segment_readonly(seg: str) -> tuple[bool, str | None]:
    """(read_only, deny_reason). deny_reason is set only for the hard `find /` case."""
    if seg in ("do", "done"):
        return True, None
    if seg.startswith("do "):
        seg = seg[3:]
    if re.match(r"for\s+\w+\s+in\s+[\w\s./\"'*-]+$", seg):
        return True, None  # loop head over plain words — the body segments are checked on their own
    try:
        toks = shlex.split(seg)
    except ValueError:
        return False, None
    if not toks or "=" in toks[0]:
        return False, None
    head = toks[0].rsplit("/", 1)[-1]
    if head in SAFE:
        return True, None
    if head == "git":
        sub = next((t for t in toks[1:] if not t.startswith("-")), "")
        return sub in GIT_READ, None
    if head in ("awk", "gawk"):
        return not _AWK_UNSAFE.search(seg), None
    if head == "sed":
        flags = [t for t in toks[1:] if t.startswith("-")]
        if "-n" not in flags or any(f.startswith("-i") for f in flags):
            return False, None
        script = next((t for t in toks[1:] if not t.startswith("-")), "")
        # print-only numeric/address scripts ("100,200p", "$((a)),$((b))p"); anything fancier defers
        return bool(re.fullmatch(r"[0-9,;$\s+\-]*p?", _strip_expansions(script))), None
    if head == "find":
        if _FIND_UNSAFE.search(seg):
            return False, None
        # path operands stop at the first predicate; tokens after a predicate are its arguments
        first_pred = next((i for i, t in enumerate(toks[1:]) if t.startswith("-")), len(toks) - 1)
        if "/" in toks[1 : 1 + first_pred]:
            return False, FIND_ROOT_REASON
        return True, None
    return False, None


def never_reason(text: str, *, broker: str = "", repos_root: str = "<repos_root>",
                 exempt_marker: str | None = None) -> str | None:
    """Instructive deny reason for command classes NEVER approvable headless, or None.

    The outcome is identical to the transport's own auto-deny — the win is the MESSAGE: the reason
    string reaches the child as a tool_result (verified live, ground shard 2026-08-11: the find-/ hint
    arrived verbatim and both shards recovered in one turn), so recovery costs 1 turn instead of a 3-5
    denial probe.

    `exempt_marker`: a substring marking a pinned harness entrypoint (the broker itself) — those ARE the
    sanctioned alternative and are allowlisted by exact form, so they are never denied here.
    Class heads inside quoted spans are data (`grep 'awk' f`), not commands — stripped before matching.
    """
    if exempt_marker and exempt_marker in text:
        return None
    bare = re.sub(r"'[^']*'|\"[^\"]*\"", " ", text)
    if re.search(r"(?<![\w-])(?:g?awk|sed)\b", bare):
        return (f"awk/sed in this form is never approved in this headless sandbox — for line ranges, "
                f"regex slices and saved outputs use `{broker} slice <file> <N-M|regex> "
                f"[--out <run-dir-relative>]`, or jq for JSON. Read-only awk/sed work only inside a "
                f"pipeline with no redirection.")
    if re.search(r"(?<![\w-])find\b", bare):
        return (f"this find form is never approved — local repo checkouts are under {repos_root}; enumerate "
                f"with `rg --files {repos_root}/<repo>` (or scope find to one directory, no -delete/-exec).")
    if re.search(r"(?<![\w-])(?:python[0-9.]*|node|perl|ruby|xargs)\b", bare):
        return (f"ad-hoc interpreters and xargs are never approved headless — use the allowlisted "
                f"brokers from your prompt (`{broker} slice|hash|save-image`, run `{broker} help`), "
                f"jq, or plain read-only commands; write files only via the Write tool or the broker's --out.")
    return None
