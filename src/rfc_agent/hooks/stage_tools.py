#!/usr/bin/env python3
"""stage-tools: the ONE safe path for disk-parsing chores agents otherwise solve with awk/sed/redirection.

Same ACI pattern as repo_history.py (pinned absolute-path allowlist entry). Why a broker instead of
allowlisting the shell verbs (security verdict 2026-08-14; Flatt "Pwning Claude Code", GTFOBins):
Claude Code allow rules are string-PREFIX matches and the PreToolUse write-guard sees only the command
STRING, never spawned children — awk `system()`/`print >` and sed's `e`/`w`/`-i` escapes are invisible
to both layers, and INGEST holds untrusted PRD text, so prompt injection is the assumed threat. Observed
need (PROJ-3394-figma-v2 run): slicing a multi-MB persisted Figma metadata XML, hashing it for a future
cache key, and decoding base64 screenshot payloads — three inert jobs, so this wrapper does exactly
three inert jobs. It is stage-agnostic: the allowlist that carries it is shared by every stage's child.

Subcommands:
    slice <file> <N-M | regex> [--out <run-dir-relative>]   line range (raw) or matching lines (numbered)
    hash <file>                                             sha256 hex digest
    save-image <b64-file-or-literal> <run-dir-relative>     decode base64 (data-URI ok) to bytes

Confinement: every destination must land inside the run dir, which is the process CWD — stages run with
cwd=run_dir. Deliberately NOT an env var: the model can export env in a Bash call, but it cannot move
this process's cwd past the allowlisted exact command form. Reads are unrestricted (the sandbox already
allows cat/grep anywhere).
"""

from __future__ import annotations

import base64
import hashlib
import os
import re
import sys

_USAGE = (
    "usage: stage-tools slice <file> <N-M|regex> [--out <run-dir-relative>]\n"
    "       stage-tools hash <file>\n"
    "       stage-tools save-image <b64-file-or-literal> <run-dir-relative>"
)


def _confined(dest: str) -> str:
    """Resolve dest against the run dir (cwd); refuse anything that escapes it, symlinks included."""
    run_dir = os.path.realpath(os.getcwd())
    p = os.path.realpath(os.path.join(run_dir, dest))
    if p != run_dir and not p.startswith(run_dir + os.sep):
        print(f"stage-tools: destination '{dest}' escapes the run dir ({run_dir})", file=sys.stderr)
        raise SystemExit(2)
    return p


def _slice(path: str, selector: str, out: str | None) -> int:
    with open(path, encoding="utf-8", errors="replace") as f:
        lines = f.readlines()
    m = re.fullmatch(r"(\d+)-(\d+)", selector)
    if m:
        a, b = int(m.group(1)), int(m.group(2))
        picked = lines[max(a, 1) - 1 : b]
    else:
        try:
            rx = re.compile(selector)
        except re.error as exc:
            print(f"stage-tools: bad regex: {exc}", file=sys.stderr)
            return 2
        picked = [f"{i}:{ln}" for i, ln in enumerate(lines, 1) if rx.search(ln)]
    text = "".join(picked)
    if out:
        dest = _confined(out)
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        with open(dest, "w", encoding="utf-8") as f:
            f.write(text)
        print(f"wrote {len(picked)} lines to {out}")
    else:
        sys.stdout.write(text)
    return 0


def _hash(path: str) -> int:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    print(h.hexdigest())
    return 0


def _save_image(src: str, out: str) -> int:
    data = open(src, encoding="ascii", errors="replace").read() if os.path.isfile(src) else src
    # tolerate data-URI prefixes and whitespace/newlines from tool-result payloads
    data = re.sub(r"^data:[\w/+.-]+;base64,", "", data.strip())
    try:
        raw = base64.b64decode(re.sub(r"\s+", "", data), validate=True)
    except (base64.binascii.Error, ValueError) as exc:
        print(f"stage-tools: not valid base64: {exc}", file=sys.stderr)
        return 2
    dest = _confined(out)
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    with open(dest, "wb") as f:
        f.write(raw)
    print(f"wrote {len(raw)} bytes to {out}")
    return 0


def main() -> int:
    args = sys.argv[1:]
    if args[:1] in (["help"], ["--help"], ["-h"], ["explain"]):
        # self-describing boundary: deny reasons point here so agents can query what is sanctioned
        # in-band instead of probing the permission wall empirically
        print(_USAGE)
        print("\nslice reads ANY path (reads are unconfined); every --out/destination must resolve"
              "\ninside the run dir (= your working directory) — traversal and symlinks refuse."
              "\nWrites in this sandbox happen ONLY via the Write tool, `>` to a run-dir path from an"
              "\nallowlisted command, or this broker. awk/sed/find/interpreters are never approved.")
        return 0
    try:
        if args[:1] == ["slice"] and len(args) in (3, 5):
            out = args[4] if args[3:4] == ["--out"] else None
            if len(args) == 5 and out is None:
                raise IndexError
            return _slice(args[1], args[2], out)
        if args[:1] == ["hash"] and len(args) == 2:
            return _hash(args[1])
        if args[:1] == ["save-image"] and len(args) == 3:
            return _save_image(args[1], args[2])
    except OSError as exc:
        print(f"stage-tools: {exc}", file=sys.stderr)
        return 2
    print(_USAGE, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
