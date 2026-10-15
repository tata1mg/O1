"""Investigate's two pauses, on Scope's confirm mechanism (terminal list, or files for /rfc-agent:investigate).

File mode: writes confirm/investigate-pause<N>.proposal.json and waits for confirm/investigate-pause<N>.answers.json:
  {"selected": [n, ...], "feedback": "free text", "answers": {"Q1": "text"}, "notes": {"<item key>": "text"}}
Scope's repos are final: no pause adds a repo.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

from .. import observe
from ..scope import confirm

HEADINGS = {
    "retry": "Repos in scope WITHOUT a snapshot (cannot be traced) — tick to retry the snapshot",
    "question": "Open questions (tick to answer)",
}


def _render(items: list[confirm.Item]) -> str:
    out, last = [], None
    for i, it in enumerate(items, 1):
        if it.kind != last:
            out.append(f"\n  {HEADINGS.get(it.kind, it.kind)}")
            last = it.kind
        out.append(f"   [{'x' if it.include else ' '}] {i:>2}. {it.label}" + (f"  — {it.note}" if it.note else ""))
    return "\n".join(out)


def ask(n: int, run_dir: Path, header: str, items: list[confirm.Item], **kw) -> dict:
    """Returns {"items": [...], "feedback": str, "answers": {qid: text}, "notes": {item key: text}}. The answer is
    recorded on the trace as `operator:pause<n>`."""
    ans = _ask(n, run_dir, header, items, **kw)
    observe.operator(
        f"pause{n}",
        {
            "header": header.strip().splitlines()[0] if header.strip() else "",
            "accepted_defaults": bool(kw.get("accept_defaults")),
            "selected": [f"{it.kind}:{it.key}" for it in ans["items"] if it.include],
            "not_selected": [f"{it.kind}:{it.key}" for it in ans["items"] if not it.include],
            "feedback": ans.get("feedback", ""),
            "answers": ans.get("answers", {}),
            "notes": ans.get("notes", {}),
        },
    )
    return ans


def _ask(
    n: int,
    run_dir: Path,
    header: str,
    items: list[confirm.Item],
    *,
    file_mode: bool,
    accept_defaults: bool,
    questions: dict[str, dict] | None = None,
    timeout_s: int = 3600,
) -> dict:
    """Returns {"items": [...], "feedback": str, "answers": {qid: text}, "notes": {item key: text}}."""
    questions = questions or {}
    if accept_defaults:
        print(f"\n{header}{_render(items)}\n", file=sys.stderr)
        return {"items": items, "feedback": "", "answers": {}, "notes": {}}
    if file_mode or not sys.stdin.isatty():
        base = run_dir / "confirm"
        base.mkdir(parents=True, exist_ok=True)
        prop, ans = base / f"investigate-pause{n}.proposal.json", base / f"investigate-pause{n}.answers.json"
        ans.unlink(missing_ok=True)
        tmp = prop.with_suffix(".tmp")  # atomic for the reader
        tmp.write_text(
            json.dumps(
                {
                    "header": header,
                    "items": [
                        {
                            "n": i,
                            "kind": it.kind,
                            "key": it.key,
                            "label": it.label,
                            "note": it.note,
                            "include": it.include,
                            **({"question": questions[it.key]} if it.kind == "question" else {}),
                        }
                        for i, it in enumerate(items, 1)
                    ],
                },
                indent=2,
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        tmp.replace(prop)
        print(
            f"  [investigate] WAITING FOR PAUSE {n}: proposal at {prop} — answers expected at {ans}",
            file=sys.stderr,
            flush=True,
        )
        t0 = time.monotonic()
        while not ans.exists():
            if time.monotonic() - t0 > timeout_s:
                raise SystemExit(f"investigate: no answers for pause {n} within {timeout_s // 60} min")
            time.sleep(1)
        time.sleep(0.3)
        data = json.loads(ans.read_text(encoding="utf-8"))
        keep = {int(x) for x in data.get("selected") or []}
        for i, it in enumerate(items, 1):
            it.include = i in keep
        return {
            "items": items,
            "feedback": str(data.get("feedback") or ""),
            "answers": {k: str(v) for k, v in (data.get("answers") or {}).items() if str(v).strip()},
            "notes": {k: str(v) for k, v in (data.get("notes") or {}).items() if str(v).strip()},
        }
    # terminal
    feedback = ""
    notes: dict[str, str] = {}
    while True:
        print(
            f"\n{header}{_render(items)}\n\n  numbers toggle · f <text> = feedback · "
            "n <number> <text> = note on an item · Enter accepts · q aborts",
            file=sys.stderr,
        )
        a = input("  > ").strip()
        if a.lower() == "q":
            raise SystemExit("investigate: aborted by operator")
        if not a:
            break
        if a.startswith("f "):
            feedback = a[2:].strip()
            continue
        if a.startswith("n "):
            parts = a[2:].strip().split(None, 1)
            if len(parts) == 2 and parts[0].isdigit() and 1 <= int(parts[0]) <= len(items):
                notes[items[int(parts[0]) - 1].key] = parts[1]
            else:
                print("  ! note is `n <number> <text>`", file=sys.stderr)
            continue
        err = (
            "repos are fixed at Scope — rerun /rfc-agent:scope to change them"
            if a.startswith("+")
            else confirm.apply(items, a, None)
        )
        if err:
            print(f"  ! {err}", file=sys.stderr)
    answers = {}
    for it in items:
        if it.kind == "question" and it.include:
            q = questions.get(it.key, {})
            hint = f" [Enter = suggested: {q.get('suggested_answer')}]" if q.get("suggested_answer") else ""
            text = input(f"  {it.key}: {q.get('question', it.label)}{hint}\n  answer> ").strip()
            answers[it.key] = text or str(q.get("suggested_answer") or "")
    return {"items": items, "feedback": feedback, "answers": {k: v for k, v in answers.items() if v}, "notes": notes}
