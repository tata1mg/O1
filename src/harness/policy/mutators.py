"""Shell mutation / egress / redirect denial — the deny-list half of containment.

This is best-effort defence-in-depth, NOT a sandbox (docs/harness/ARCHITECTURE.md §6). It exists because a stage
runs with `--add-dir` mounts and auto-accepted edits, so an injected instruction in an ingested PRD could
otherwise plant a diff in a real repo or phone home. Every regex here was added after a live incident or a
live false positive; the comments record which, because a "simplification" that drops a lookbehind
re-opens a denial loop that cost real runs.

Pure functions: each takes the command plus the writable roots and returns findings, never exits.
"""

from __future__ import annotations

import re
import shlex
from pathlib import Path

from .paths import DEV_SINKS, inside

# mutation/egress verbs; only consulted for shell commands. Redirects are checked separately (any
# `>`/`>>` target — absolute OR relative — must land inside a writable root: `> ../x` used to sail
# through the old absolute-only pattern and reach sibling repos via the run-dir cwd).
# The (?<![\w-]) lookbehind keeps verbs from matching inside flags: `\bln\b` matched the -ln of
# `grep -ln` (word boundary sits after the hyphen) and put a shard in a denial loop; `-cp`/`-dd`
# flag forms are the same shape. A real verb only counts at a command position (start, space, ;|&).
MUTATORS = re.compile(
    r"((?<![\w-])(?:sed\s+-i|tee|rm|mv|cp|chmod|chown|truncate|dd|ln|install)\b|"
    r"(?<![\w-])(?:curl|wget|nc|ncat|netcat|scp|sftp|ssh|rsync)\b|"
    r"(?<![\w-])git\s+(?:commit|push|checkout|reset|clean|apply|add)\b)"
)
EGRESS = re.compile(r"\b(?:curl|wget|nc|ncat|netcat|scp|sftp|ssh|rsync)\b")
# sort is prefix-allowlisted as read-only, but GTFOBins-style flags break that: -o/--output writes an
# arbitrary file (no `>` for the redirect check to see) and --compress-program execs. Flat deny —
# `sort ... > file` inside a writable root covers the legitimate need and stays redirect-checked.
SORT_UNSAFE = re.compile(r"(?<![\w-])sort\b[^|;&]*(?:\s-o\b|\s-o\S|--output|--compress-program)")
# inline interpreter code (`python -c "open('/x','w')"`) can write anywhere without a mutator verb;
# when present, every absolute path in the command must be inside a writable root
INLINE_CODE = re.compile(r"\b(?:python[0-9.]*|node|perl|ruby|sh|bash|zsh)\b[^|;&]*\s-c\b")
# `(`/`)` excluded from bare targets: `$(grep ... 2>/dev/null)` captured `/dev/null)` — the paren
# glommed onto the target, which then failed the exact-match sink list and denied every subshell read
# (observed live 2026-08-07: two pure-grep shard commands denied)
REDIRECT_TARGET = re.compile(r"\d?>>?[ \t]*(\"[^\"]*\"|'[^']*'|[^\s;|&<>()]+)")
ABS_PATH = re.compile(r"(?<![\w@.:-])/[\w./-]+")

ESCAPE_REASON = (
    "Bash command mutates/exfiltrates outside the run dir: {targets}. "
    "Writes land only inside the run dir — use the Write tool, `>` to a run-dir path "
    "from an allowlisted command, or the stage-tools broker's --out."
)
SORT_REASON = ("sort -o/--output/--compress-program can write/exec arbitrarily — "
               "use `sort ... > <file inside the run dir>` instead.")
EGRESS_REASON = "Bash network egress is not allowed in this stage (use the stage's MCP tools)."


def _quoted_spans(cmd: str) -> list[tuple[int, int]]:
    return [m.span() for m in re.finditer(r"'[^']*'|\"[^\"]*\"", cmd)]


def escaping_targets(cmd: str, roots: tuple[Path, ...] | list[Path], cwd: Path | None = None) -> list[str]:
    """Paths this command would write/exfiltrate OUTSIDE `roots` ([] = nothing escapes).

    Three independent scans, because each catches what the others cannot:
      1. redirect targets — a `>` inside a quoted span is a literal character to the shell, never a
         redirect (the raw scan parsed awk's `$1>4300` as fd-redirect-to-file "4300" and denied a pure
         read-only pipeline: v7 review 2026-08-10, 2 denials). A redirect to a QUOTED TARGET
         (`> "/x/f"`, the `>` itself outside quotes) still scans normally.
      2. mutator/egress verb present -> every word naming a path must be inside;
      3. inline interpreter code -> every absolute path in the command must be inside.
    """
    outside: list[str] = []
    spans = _quoted_spans(cmd)
    for m in REDIRECT_TARGET.finditer(cmd):
        if any(a < m.start() < b for a, b in spans):
            continue
        tgt = m.group(1).strip("'\"")
        if tgt and tgt not in DEV_SINKS and not inside(tgt, roots, cwd):
            outside.append(tgt)
    if MUTATORS.search(cmd):
        try:
            words = shlex.split(cmd)
        except ValueError:
            words = cmd.split()
        outside += [w for w in words if w not in DEV_SINKS and not inside(w, roots, cwd)]
    if INLINE_CODE.search(cmd):
        outside += [p for p in ABS_PATH.findall(cmd) if p not in DEV_SINKS and not inside(p, roots, cwd)]
    return outside


def shell_denial(cmd: str, roots: tuple[Path, ...] | list[Path], cwd: Path | None = None) -> str | None:
    """The first containment reason to deny `cmd`, or None. Order matches the shipped hook exactly."""
    outside = escaping_targets(cmd, roots, cwd)
    if outside:
        return ESCAPE_REASON.format(targets=outside[:3])
    if SORT_UNSAFE.search(cmd):
        return SORT_REASON
    if EGRESS.search(cmd):
        return EGRESS_REASON
    return None
