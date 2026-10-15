"""Progress for Claude Code to relay:
- logs/status.json — current state per agent (calls so far, last action, session id);
- logs/progress.jsonl — milestones;
- logs/activity.log — one plain line per agent tool call, start and end (the live feed /rfc-agent:rfc relays;
  `tail -f` works too);
- logs/agents/<stage>.<label>.<step>.log — one agent's tool calls and messages, ending with a "■" line;
- logs/sessions.json — every agent's Claude Code session, so it can be opened with `claude --resume`."""

from __future__ import annotations

import json
import sys
import threading
import time
from datetime import UTC, datetime
from pathlib import Path


class Progress:
    def __init__(self, run_dir: Path, stage: str = "investigate"):
        self.run_dir = run_dir
        self.logs = run_dir / "logs"
        self.logs.mkdir(parents=True, exist_ok=True)
        self.state = {"stage": stage, "started_at": _now(), "step": "", "agents": {}}
        self._lock = threading.Lock()
        self._last_write = 0.0
        self._cards: dict[str, str] = {}  # agent label → its current log name under logs/agents/

    def step(self, step: str) -> None:
        with self._lock:
            self.state["step"] = step
        self._flush(force=True)

    def agent(self, label: str, **fields) -> None:
        with self._lock:
            self.state["agents"].setdefault(label, {"id": label}).update(fields)
        self._flush(force=fields.get("state") is not None)

    def tick(self, label: str, kind: str, data: dict) -> None:  # on_event callback for agent.run
        stage = self.state["stage"]
        if kind == "tool":
            last = f"{data.get('name')} {data.get('target') or ''}".strip()
            self.agent(label, tool_calls=data.get("n"), last=last)
            self._activity(f"{label} #{data.get('n')} {last}")
            self._card(label, f"#{data.get('n')} {last}")
        elif kind == "text":
            self._card(label, "💬 " + data.get("text", "").replace("\n", "\n   "))
        elif kind == "start":
            log = data.get("log") or f"{stage}.{label}"
            with self._lock:
                self._cards[label] = log
            self.agent(label, session_id=data.get("session_id"), card=log)
            self._activity(f"{label} ▶ started · session {data.get('session_id')} · card {log}")
            sid, model = data.get("session_id"), data.get("model")
            self._card(label, f"▶ {stage} · {label} started · model {model} · session {sid}")
        elif kind == "end":
            ok = "done" if data.get("ok") else f"FAILED: {data.get('error')}"
            self._activity(f"{label} ■ {ok} · {data.get('tool_calls')} calls, {data.get('seconds')}s")
            sid = data.get("session_id")
            how = f" · open it: cd {self.run_dir.resolve()} && claude --resume {sid} --fork-session" if sid else ""
            self._card(label, f"■ {ok} · {data.get('tool_calls')} calls, {data.get('seconds')}s{how}")
            if sid:
                self._session(f"{stage}.{label}.{data.get('step')}", sid)

    def _activity(self, text: str) -> None:
        self._append(self.logs / "activity.log", f"{self.state['stage']:<11} {text}")

    def _card(self, label: str, text: str) -> None:
        """One agent's own log — what its card in Claude Code shows (`python -m rfc_agent.watch`)."""
        with self._lock:
            log = self._cards.get(label)
        if log:
            self._append(self.logs / "agents" / f"{log}.log", text)

    def _append(self, path: Path, text: str) -> None:
        try:
            with self._lock:
                path.parent.mkdir(parents=True, exist_ok=True)
                with open(path, "a", encoding="utf-8") as f:
                    f.write(f"{datetime.now().strftime('%H:%M:%S')}  {text}\n")
        except OSError:
            pass  # the feed is a convenience: never fail a run over it

    def _session(self, key: str, session_id: str) -> None:
        path = self.logs / "sessions.json"
        try:
            with self._lock:
                try:
                    data = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    data = {"cwd": str(self.run_dir.resolve()), "sessions": {}}
                data["sessions"][key] = session_id
                tmp = path.with_suffix(".json.tmp")
                tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
                tmp.replace(path)
        except OSError:
            pass

    def milestone(self, kind: str, text: str, **data) -> None:
        rec = {"ts": _now(), "kind": kind, "text": text, **data}
        with self._lock, open(self.logs / "progress.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        print(f"  [{self.state['stage']}] {text}", file=sys.stderr, flush=True)

    def _flush(self, force: bool = False) -> None:
        now = time.monotonic()
        if not force and now - self._last_write < 2:
            return
        with self._lock:  # agents finish concurrently: serialize the snapshot + atomic replace
            data = {**self.state, "agents": list(self.state["agents"].values()), "updated_at": _now()}
            tmp = self.logs / "status.json.tmp"
            tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
            tmp.replace(self.logs / "status.json")
            self._last_write = now


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")
