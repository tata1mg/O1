"""The one interactive checkpoint of the run: repos, extra pages, Jira, Figma sections, branches — and,
when no design came through, whether to go on without one.

Everything is numbered; the operator toggles numbers, adds repos, overrides branches, then presses Enter.
After this nothing in the run asks a human anything.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field


@dataclass
class Item:
    kind: str          # repo | page | jira | figma
    key: str           # repo name | page id | jira key | "<export idx>:<section name>"
    label: str
    note: str = ""
    include: bool = True
    extra: dict = field(default_factory=dict)


_HEADINGS = {"repo": "Repos", "page": "Child / linked Confluence pages (fetched only if ticked)",
             "jira": "Jira referenced in the PRD (fetched only if ticked)", "figma": "Figma sections"}

HELP = ("  numbers toggle (e.g. `3 7 9`) · `+repo_a,repo_b` adds repos · `b 2=feature/x` sets repo 2's branch\n"
        "  Enter accepts · `q` aborts")


def render(items: list[Item]) -> str:
    out, last = [], None
    for i, it in enumerate(items, 1):
        if it.kind != last:
            out.append(f"\n  {_HEADINGS[it.kind]}")
            last = it.kind
        box = "[x]" if it.include else "[ ]"
        note = f"  — {it.note}" if it.note else ""
        out.append(f"   {box} {i:>2}. {it.label}{note}")
    return "\n".join(out)


def apply(items: list[Item], answer: str, add_repo) -> str | None:
    """Apply one line of operator input. Returns an error message, or None."""
    a = answer.strip()
    if a.startswith("+"):
        for name in [n.strip() for n in a[1:].split(",") if n.strip()]:
            if not any(it.kind == "repo" and it.key == name for it in items):
                pos = max((i for i, it in enumerate(items) if it.kind == "repo"), default=-1) + 1
                items.insert(pos, add_repo(name))
        return None
    if a.startswith("b "):
        try:
            num, branch = a[2:].split("=", 1)
            it = items[int(num.strip()) - 1]
        except (ValueError, IndexError):
            return "branch override is `b <number>=<branch>`"
        if it.kind != "repo":
            return f"{num.strip()} is not a repo"
        it.extra["branch"] = branch.strip()
        it.extra["branch_from"] = "operator override"
        it.note = f"{branch.strip()} (your override)"
        return None
    for tok in a.replace(",", " ").split():
        if not tok.isdigit() or not 1 <= int(tok) <= len(items):
            return f"not a valid number: {tok}"
        it = items[int(tok) - 1]
        it.include = not it.include
    return None


def no_design_stop(reason: str) -> SystemExit:
    return SystemExit(f"scope: STOP: no design available ({reason}) — add the Figma link to the PRD and rerun, "
                      "or rerun with --no-figma to go on without one")


def ask(items: list[Item], add_repo, header: str, no_design: str | None = None) -> list[Item]:
    """`no_design`: why no Figma screens are available (None when there are some, or Figma was skipped on
    purpose) — the operator is asked whether to go on without a design before the run continues."""
    if not sys.stdin.isatty():
        raise SystemExit("scope: the confirm step needs a terminal — rerun interactively, or pass --yes "
                         "to accept the proposal as shown")
    while True:
        print(f"\n{header}{render(items)}\n\n{HELP}", file=sys.stderr)
        try:
            answer = input("  > ")
        except EOFError:
            answer = ""
        if answer.strip().lower() == "q":
            raise SystemExit("scope: aborted by operator")
        if not answer.strip():
            if not any(it.kind == "repo" and it.include for it in items):
                print("  ! at least one repo is needed", file=sys.stderr)
                continue
            if no_design:
                try:
                    go = input(f"  No design available ({no_design}). Continue without a design? [y/N] ")
                except EOFError:
                    go = ""
                if go.strip().lower() not in ("y", "yes"):
                    raise no_design_stop(no_design)
            return items
        err = apply(items, answer, add_repo)
        if err:
            print(f"  ! {err}", file=sys.stderr)


# ---- file-based confirm (the /rfc-agent:scope command shows it as selection questions) ---------------

PROPOSAL = "confirm/proposal.json"
ANSWERS = "confirm/answers.json"


def via_file(items: list[Item], add_repo, header: str, run_dir, timeout_s: int = 3600,
             no_design: str | None = None) -> list[Item]:
    """Write the proposal, wait for the answers file, apply it.

    proposal.json: {"header", "items": [{"n", "kind", "key", "label", "note", "include"}],
                    "no_design": "why"}                   only when no design is available
    answers.json:  {"selected": [n, ...],                 every item number to KEEP (repos/pages/jira/figma)
                    "add_repos": ["name", ...],           optional — repos to add (kept unless Bitbucket lacks them)
                    "branches": {"repo": "branch"},       optional — branch overrides
                    "proceed_without_design": true}       required to go on when the proposal has `no_design`
    The orchestrating Claude session renders the proposal as AskUserQuestion multi-selects and writes the
    answers; a human can also edit the file by hand."""
    import json
    import time
    from pathlib import Path

    run_dir = Path(run_dir)
    prop, ans = run_dir / PROPOSAL, run_dir / ANSWERS
    prop.parent.mkdir(parents=True, exist_ok=True)
    ans.unlink(missing_ok=True)
    tmp = prop.with_suffix(".tmp")  # atomic: a reader never sees a half-written proposal
    proposal = {"header": header, "items": [
        {"n": i, "kind": it.kind, "key": it.key, "label": it.label, "note": it.note, "include": it.include}
        for i, it in enumerate(items, 1)]}
    if no_design:
        proposal["no_design"] = no_design
    tmp.write_text(json.dumps(proposal, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(prop)
    print(f"  [scope] WAITING FOR SELECTION: proposal at {prop} — answers expected at {ans}", file=sys.stderr,
          flush=True)
    t0 = time.monotonic()
    while not ans.exists():
        if time.monotonic() - t0 > timeout_s:
            raise SystemExit(f"scope: no selection arrived in {ans} within {timeout_s // 60} min")
        time.sleep(1)
    time.sleep(0.3)  # let the writer finish
    data = json.loads(ans.read_text(encoding="utf-8"))
    if no_design and data.get("proceed_without_design") is not True:
        raise no_design_stop(no_design)
    keep = {int(n) for n in data.get("selected") or []}
    for i, it in enumerate(items, 1):
        it.include = i in keep
    for name in data.get("add_repos") or []:
        apply(items, f"+{name}", add_repo)
    for repo, branch in (data.get("branches") or {}).items():
        for i, it in enumerate(items, 1):
            if it.kind == "repo" and it.key == repo:
                apply(items, f"b {i}={branch}", add_repo)
    if not any(it.kind == "repo" and it.include for it in items):
        raise SystemExit("scope: the selection kept no repos — at least one is needed")
    return items
