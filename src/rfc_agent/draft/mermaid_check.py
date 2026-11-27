"""Mermaid diagrams in the RFC: parse them with Mermaid's own parser, repair what is mechanical, and never ship a
diagram that will not render as if it did (issue #18: `A->>B: POST /x; resolve report key` — an unescaped `;` ends a
statement, so GitHub / mermaid.live / VS Code showed a parse error instead of the diagram).

Steps (`repair_file`, then `finalize` after the gate's fix round):
  1. parse every ```mermaid block with Mermaid itself (Node + the mermaid package, installed once into
     ~/.cache/rfc-agent/mermaid-check, version pinned in mermaid_tool/package.json);
  2. repair what is mechanical: `;` in message text becomes `#59;` (always safe), then parse again;
  3. what still fails goes to the gate as a must-fix (with the parser's own message) for the lead's one fix round;
  4. what still fails after that is shown as plain code under a note — never as a ```mermaid block that renders as an
     error — so a person can paste it into mermaid.live. It stays a known issue.
Without Node/npm (or with RFC_AGENT_MERMAID_CHECK=off) steps 1 and 3 are skipped, the semicolon repair still runs, and
the report says the diagrams were not parser-checked.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

TOOL = Path(__file__).with_name("mermaid_tool")
PARSE_TIMEOUT_S = 90
INSTALL_TIMEOUT_S = 300
QUARANTINE_MARK = "<!-- rfc-agent: this diagram failed to render -->"

_runtime: dict = {}  # one install attempt per process: {"dir": Path | None, "why": str}


@dataclass
class Block:
    index: int  # 1-based, in document order
    line: int  # 1-based line of the opening fence
    start: int  # index of the first body line
    end: int  # index of the closing fence line
    code: str


def blocks(rfc: str) -> list[Block]:
    lines = rfc.splitlines()
    out: list[Block] = []
    i = 0
    while i < len(lines):
        if lines[i].strip().startswith("```mermaid"):
            j = i + 1
            while j < len(lines) and not lines[j].strip().startswith("```"):
                j += 1
            if j >= len(lines):
                break  # never closed: the gate's lint reports it
            out.append(Block(len(out) + 1, i + 1, i + 1, j, "\n".join(lines[i + 1 : j])))
            i = j + 1
            continue
        i += 1
    return out


# ---- the parser ------------------------------------------------------------------------------------------


def _cache_dir() -> Path:
    return Path(os.environ.get("RFC_AGENT_MERMAID_DIR") or "~/.cache/rfc-agent/mermaid-check").expanduser()


def enabled() -> bool:
    return os.environ.get("RFC_AGENT_MERMAID_CHECK", "on").lower() not in ("off", "false", "0", "no")


def runtime() -> tuple[Path | None, str]:
    """(folder with check.mjs and node_modules, why not). Installs on first use; the answer is kept for the process."""
    if not enabled():
        return None, "turned off (RFC_AGENT_MERMAID_CHECK=off)"
    if "dir" in _runtime:
        return _runtime["dir"], _runtime["why"]
    d = _cache_dir()
    why = ""
    node, npm = shutil.which("node"), shutil.which("npm")
    if not node or not npm:
        why = "Node.js / npm not found"
    else:
        try:
            d.mkdir(parents=True, exist_ok=True)
            for name in ("package.json", "package-lock.json", "check.mjs"):
                src, dst = TOOL / name, d / name
                if not dst.exists() or dst.read_bytes() != src.read_bytes():
                    shutil.copyfile(src, dst)
                    if name == "package-lock.json":
                        shutil.rmtree(d / "node_modules", ignore_errors=True)  # new pinned versions: reinstall
            if not (d / "node_modules" / "mermaid").is_dir():
                r = subprocess.run([npm, "install", "--no-audit", "--no-fund", "--loglevel=error"], cwd=d,
                                   capture_output=True, text=True, timeout=INSTALL_TIMEOUT_S)
                if r.returncode != 0:
                    why = f"npm install failed: {(r.stderr or r.stdout).strip().splitlines()[-1:] or ['?']}"
        except (OSError, subprocess.TimeoutExpired) as e:
            why = f"could not set up the Mermaid parser: {e}"
    _runtime.update(dir=None if why else d, why=why)
    return _runtime["dir"], why


def parse_all(codes: list[str]) -> list[dict] | None:
    """[{ok, error?}] per source, or None when the parser is not available."""
    if not codes:
        return []
    d, _ = runtime()
    if d is None:
        return None
    try:
        r = subprocess.run([shutil.which("node") or "node", "check.mjs"], cwd=d, input=json.dumps(codes),
                           capture_output=True, text=True, timeout=PARSE_TIMEOUT_S)
        out = json.loads(r.stdout.strip().splitlines()[-1])
    except (OSError, ValueError, IndexError, subprocess.TimeoutExpired):
        return None
    return out if isinstance(out, list) and len(out) == len(codes) else None


def parser_label() -> str:
    d, why = runtime()
    if d is None:
        return f"not parser-checked: {why}"
    try:
        v = json.loads((d / "node_modules" / "mermaid" / "package.json").read_text(encoding="utf-8"))["version"]
    except (OSError, ValueError, KeyError):
        v = "?"
    return f"mermaid@{v}"


def short(error: str, limit: int = 240) -> str:
    """The part of the parser's message a person needs: the failing line and what it expected."""
    e = " ".join((error or "").split())
    return e if len(e) <= limit else e[:limit] + "…"


def failures(rfc: str) -> list[dict] | None:
    """[{index, line, error}] for every ```mermaid block that does not parse; None when not parser-checked."""
    bs = blocks(rfc)
    res = parse_all([b.code for b in bs])
    if res is None:
        return None
    return [{"index": b.index, "line": b.line, "error": short(r.get("error", ""))}
            for b, r in zip(bs, res, strict=True) if not r.get("ok")]


# ---- repair ----------------------------------------------------------------------------------------------

_ARROW = r"(?:-->>|->>|--x|-x|--\)|-\)|-->|->)"
_TEXT_LINE = re.compile(rf"^(\s*(?:[^:\n]*?{_ARROW}[^:\n]*|[Nn]ote\s[^:\n]*)):(.*)$")
_BLOCK_LINE = re.compile(r"^(\s*(?:alt|opt|loop|par|critical|break|rect|else|and|option|title))\b(.*)$")
_SEMI = re.compile(r"#\d+;|;")


def _escape(text: str) -> str:
    return _SEMI.sub(lambda m: m.group(0) if len(m.group(0)) > 1 else "#59;", text)


def repair(code: str) -> str:
    """Escape `;` in sequence-diagram text (messages, notes, alt/loop titles). Nothing else is guessed."""
    if not code.lstrip().startswith("sequenceDiagram"):
        return code
    out = []
    for ln in code.split("\n"):
        m = _TEXT_LINE.match(ln)
        if m:
            ln = f"{m.group(1)}:{_escape(m.group(2))}"
        else:
            m = _BLOCK_LINE.match(ln)
            if m:
                ln = f"{m.group(1)}{_escape(m.group(2))}"
        out.append(ln)
    return "\n".join(out)


def _replace_body(rfc: str, b: Block, new_code: str) -> str:
    lines = rfc.splitlines()
    lines[b.start : b.end] = new_code.split("\n")
    return "\n".join(lines) + ("\n" if rfc.endswith("\n") else "")


def repair_file(path: Path) -> dict:
    """Parse, repair what is mechanical, parse again. Rewrites the file when something was repaired.
    -> {total, parser, failed_initial: [{index, line, error}], repaired: n, failed_after_repair: [...]}"""
    rfc = path.read_text(encoding="utf-8")
    bs = blocks(rfc)
    report = {"total": len(bs), "parser": parser_label(), "failed_initial": [], "repaired": 0,
              "failed_after_repair": []}
    if not bs:
        return report
    first = failures(rfc)
    report["failed_initial"] = first or []
    changed = False
    for b in reversed(bs):  # bottom-up, so earlier blocks' line numbers hold
        fixed = repair(b.code)
        if fixed != b.code and (first is None or any(f["index"] == b.index for f in first)):
            rfc = _replace_body(rfc, b, fixed)
            changed = True
    if changed:
        path.write_text(rfc, encoding="utf-8")
    after = failures(rfc)
    if first is None:  # no parser: say how many blocks the semicolon repair touched, not that they now render
        report["repaired"] = sum(1 for b in bs if repair(b.code) != b.code)
    else:
        report["failed_after_repair"] = after or []
        report["repaired"] = len(first) - len(after or [])
    return report


# ---- what still fails: shown as code, never as a diagram that errors ----------------------------------------


def quarantine(path: Path) -> list[dict]:
    """Turn every ```mermaid block that still does not parse into a plain `text` block under a note a person can act on.
    -> [{index, line, error}] of the blocks changed (empty when all parse, or when there is no parser)."""
    rfc = path.read_text(encoding="utf-8")
    bad = failures(rfc)
    if not bad:
        return []
    by_index = {f["index"]: f for f in bad}
    lines = rfc.splitlines()
    for b in reversed(blocks(rfc)):
        f = by_index.get(b.index)
        if not f:
            continue
        note = (f"> ⚠ This diagram did not render ({f['error']}). Its code is below as text (line numbers in the "
                "message count inside the diagram): paste it into https://mermaid.live to preview it, or fix the line "
                "the error names.")
        lines[b.line - 1 : b.line] = [QUARANTINE_MARK, note, "", "```text"]
    path.write_text("\n".join(lines) + ("\n" if rfc.endswith("\n") else ""), encoding="utf-8")
    return bad


def quarantined(rfc: str) -> list[int]:
    """Lines (1-based) of diagrams shown as code because they failed earlier and have not been fixed since."""
    lines = rfc.splitlines()
    return [i + 1 for i, ln in enumerate(lines)
            if ln.strip() == QUARANTINE_MARK and i + 3 < len(lines) and lines[i + 3].strip() == "```text"]
