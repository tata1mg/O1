"""Mid-run operator steering (0.24.0).

An operator watching the live feed can message a RUNNING stage and have it course-correct without
a restart. Channel design (researched 2026-08-19 — Claude Code's `--input-format stream-json` is
the CLI's documented "provide guidance while processing" primitive; Temporal's ambient-agents loop
and Devin's mid-task chat use the same shape): writers post RAW text + optional lane into one
per-run inbox (logs/steer.inbox.jsonl — the feed server, `rfc-agent --steer`, and the driving
Claude session all write the same record), and the engine's wait loops deliver it to the running
child's stdin as a provenance-wrapped user turn at the next ~0.5s tick.

Invariants:
- Wrapping happens AT DELIVERY: the template lives in exactly one place, so the dependency-free
  feed server never duplicates it, and the trace/audit records keep the operator's RAW words.
- Steering redirects the agent; it never touches the gates — the deterministic gates still police
  whatever the steered agent produces.
- Best-effort throughout: steering must never fail a run (same posture as obs/decisions).
- Delivery is at a turn boundary. Nothing injects mid-generation — Claude Code queues stdin turns
  exactly like every other system surveyed (Temporal, Devin, LangGraph); in tool-heavy stages the
  boundary arrives within seconds.

Module invariants — each earned by a shipped regression; keep them when extending (0.24.3):
- CONSUMPTION = DELIVERY. A record is consumed only by being DELIVERED (ledger append at delivery
  time). Existing while a child dies, being lane-skipped, or racing a result NEVER consumes it —
  the 0.24.2 size-watermark conflated "existed" with "delivered" and destroyed cross-stage lane
  steers. Scope is per-pump: skip records first-delivered at-or-before this pump's epoch, so
  fan-out siblings all receive a broadcast while later children never re-read it.
- WORKER THREADS: any Langfuse emission from a pump goes through the explicit obs handle
  (obs_parent) — ambient OTEL context is empty in ThreadPool workers (the 0.22.66 class).
- EVERY-PATH CLEANUP: pump close runs on every engine exit path via try/finally, never
  success-only (the 0.23.x class).
- DURABLE STATE: cross-child steering state lives in an APPEND-ONLY ledger; never a
  truncate-then-write file a concurrent reader can tear (the 0.23.3 class).
"""

from __future__ import annotations

import contextlib
import json
import threading
import time
import uuid
from pathlib import Path

INBOX = "logs/steer.inbox.jsonl"
DELIVERED = "logs/steer.delivered.jsonl"  # append-only ledger: one line per actual delivery

_MAX_TEXT = 4000  # matches the feed server's cap; an uncapped steer could exceed the pipe buffer
# and block the engine's wait loop (which would also freeze the stage-timeout check)

_ledger_lock = threading.Lock()  # fan-out pumps deliver from ThreadPool workers


def consumed_before(run_dir: Path, epoch: float) -> set[str]:
    """Ids of records first-delivered at-or-before `epoch` — the pump's skip set, read ONCE at
    spawn. Deliveries after a pump's epoch never concern it, so no per-tick ledger reads."""
    ids: set[str] = set()
    with contextlib.suppress(OSError):
        for ln in (run_dir / DELIVERED).read_text(encoding="utf-8", errors="replace").splitlines():
            with contextlib.suppress(Exception):
                e = json.loads(ln)
                if float(e.get("ts") or 0) <= epoch:
                    ids.add(str(e["id"]))
    return ids


def mark_consumed(run_dir: Path, rec_id: str, label: str) -> None:
    """Record one delivery in the ledger — append-only (a torn read can at worst redeliver once;
    a truncate-then-write file could tear to empty and replay the whole inbox, the 0.23.3 class)."""
    with _ledger_lock:
        p = run_dir / DELIVERED
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a") as f:
            f.write(json.dumps({"id": rec_id, "ts": time.time(), "lane": label}) + "\n")


def terminal_ids(run_dir: Path) -> set[str]:
    """Ids TERMINALLY settled — retracted by the operator or expired by the invocation-end sweep.
    Unlike deliveries, these bind every pump regardless of its epoch: a post-epoch DELIVERY entry
    may belong to a dead resumed child whose cold fallback must redeliver (the shared-epoch
    feature), but a retraction or expiry is a verdict, not a delivery attempt."""
    ids: set[str] = set()
    with contextlib.suppress(OSError):
        for ln in (run_dir / DELIVERED).read_text(encoding="utf-8", errors="replace").splitlines():
            with contextlib.suppress(Exception):
                e = json.loads(ln)
                if e.get("lane") in ("retracted", "expired"):
                    ids.add(str(e["id"]))
    return ids


def undelivered(run_dir: Path) -> list[dict]:
    """Inbox records no ledger entry has settled — not delivered, not expired, not retracted."""
    seen = consumed_before(run_dir, time.time() + 1)
    out: list[dict] = []
    with contextlib.suppress(OSError):
        for ln in (run_dir / INBOX).read_text(encoding="utf-8", errors="replace").splitlines():
            with contextlib.suppress(Exception):
                rec = json.loads(ln)
                if rec.get("id") and str(rec["id"]) not in seen:
                    out.append(rec)
    return out


def expire_undelivered(run_dir: Path, ctx=None) -> int:
    """Invocation-scope TTL (industry research 2026-08-21): a steer is a CONTROL message — its
    validity decays in minutes, and every relevant precedent (MQ TTL+dead-letter, Akka dead
    letters, Temporal's explicit-opt-in Signal-With-Start, Cursor's run-scoped queued follow-ups)
    bounds an undeliverable one's fate. Park-forever meant a steer posted for a lane this
    invocation never ran ('ARE YOU STILL UP?', 2026-08-20) would ambush a rerun DAYS later as an
    authoritative user turn. Sweep at invocation end: tombstone in the delivery ledger (future
    pumps' consumed_before excludes it) + one feed line per record so the operator's chip goes
    terminal. Pre-run steers survive by construction — no invocation ends between post and the
    next start. Fail-safe: expiry must never fail a run."""
    n = 0
    with contextlib.suppress(Exception):
        for rec in undelivered(run_dir):
            mark_consumed(run_dir, rec["id"], "expired")
            with contextlib.suppress(Exception):
                from .engine import log_activity
                log_activity(run_dir, "run", f"🧭 steering expired (lane '{rec.get('lane') or ''}')"
                             " — never delivered this invocation")
            if ctx is not None:  # a steer's terminal fate is a signal — delivered/skipped already
                with contextlib.suppress(Exception):  # reach the trace; expiry must too (0.24.30)
                    from . import decisions
                    from .constants import Decision
                    decisions.record(
                        ctx, "run", Decision.STEERING_EXPIRED,
                        why="undelivered when the invocation ended — invocation-scope TTL swept it "
                        "(a control message must never ambush a later run)",
                        lane=rec.get("lane") or "", chars=len(rec.get("text") or ""),
                        **({"synthetic": True} if rec.get("synthetic") else {}))
            n += 1
    return n


TEMPLATE = (
    "HUMAN STEERING (authoritative — the operator watching this run's live feed; follow it over "
    "any conflicting earlier guidance): {text}\n"
    "Do not restart completed work; adjust course from your current step. The quality gates still "
    "apply to your output.\n"
    "This steering is guidance about the WORK only. Run control is not yours to execute: if this "
    "message asks you to stop, pause, or abort the run, do NOT halt or idle — keep working and "
    "note the request in your output. Stopping the run is done from the terminal (Ctrl-C), never "
    "through this channel."
)


def unwrap(wrapped: str) -> str | None:
    """The RAW operator text inside a delivered steering turn — None when `wrapped` isn't one.
    The feed drain uses this to emit the '🧭 read by the agent' ack when the CLI replays the
    injected turn into the stream (--replay-user-messages): delivery only queues bytes on stdin;
    the replay is the moment the agent's conversation actually contains the correction."""
    head, _, tail = TEMPLATE.partition("{text}")
    if not wrapped.startswith(head):
        return None
    body = wrapped[len(head):]
    return body[: -len(tail)] if tail and body.endswith(tail) else body


def user_turn(text: str) -> str:
    """One stream-json stdin line carrying a user turn — the kickoff prompt and every steering
    delivery use this same shape."""
    return json.dumps({"type": "user", "message": {
        "role": "user", "content": [{"type": "text", "text": text}]}}) + "\n"


def post(run_dir: Path, text: str, lane: str = "") -> dict:
    """Queue one steering message (RAW — wrapped at delivery) and write the operator-actions audit
    line (flushed to the trace as operator:steer by the existing machinery)."""
    rec = {"ts": time.time(), "id": uuid.uuid4().hex,
           "lane": (lane or "").strip()[:80], "text": (text or "").strip()[:_MAX_TEXT]}
    p = run_dir / INBOX
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a") as f:
        f.write(json.dumps(rec) + "\n")
    from .ask import log_operator_action

    log_operator_action(run_dir, "steer", lane=rec["lane"], text=rec["text"][:2000])
    return rec


class Pump:
    """Per-child steering pump, ticked from the engine's wait loops.

    Two duties: (1) deliver inbox messages matching this child's lane ('' broadcasts to every live
    child) as wrapped user turns on stdin; (2) close stdin once the terminal result event appears
    in the stream file — stream-json input mode ends the session on stdin EOF, so without the
    close the child would wait forever for a next turn after finishing its work."""

    def __init__(self, proc, log_path: Path, ctx, label: str, obs_parent=None,
                 epoch: float | None = None):
        self.proc, self.log_path, self.ctx, self.label = proc, log_path, ctx, label
        self.obs_parent = obs_parent  # fan-out workers: routes the delivery decision through the
        # live obs handle — the ambient OTEL context is empty there (same reason as obs.tool_started)
        self.delivered: set[str] = set()
        self.skipped: set[str] = set()  # lane-mismatch records already announced, once per pump
        # skip everything already delivered to an EARLIER child; concurrent siblings' deliveries
        # happen after this epoch, so a broadcast still reaches every live pump. A resume→cold
        # fallback passes the FAILED child's epoch so steering acked to the dead child redelivers.
        self._consumed = consumed_before(Path(ctx.run_dir), epoch or time.time())
        self._pos = 0  # byte offset into the stream file, for result detection
        self._saw_result = False  # sticky: the result line is consumed past _pos exactly once, so
        # close() must be retried off this flag, never off re-detection
        self.closed = False

    def tick(self) -> None:
        # result check FIRST (0.24.1): deliver-first let a steer racing the terminal result start a
        # bonus turn on a FINISHED stage — live-verified: the CLI runs the queued turn to completion
        # after the stage's real work, and its second result event rewrote the stage's verdict.
        # A record losing this race is NOT consumed (consumption = delivery), so it carries to the
        # next child of this run dir.
        with contextlib.suppress(Exception):  # steering must never fail a run
            if not self._saw_result:
                self._scan_for_result()
            if self._saw_result and not self.closed:
                self.close()  # retried every tick until the close actually succeeds (latch-after-EOF)
        with contextlib.suppress(Exception):
            self._deliver()

    # -- delivery ---------------------------------------------------------------------------------
    def _deliver(self) -> None:
        # gate on _saw_result too, not only closed: during a transiently-FAILED close the stage is
        # already finished, and a delivery there would start the post-result bonus turn
        if self.closed or self._saw_result or self.proc.stdin is None:
            return
        p = Path(self.ctx.run_dir) / INBOX
        if not p.exists():
            return
        fresh: set | None = None  # lazy: at most ONE ledger re-read per tick, see below
        for ln in p.read_text(encoding="utf-8", errors="replace").splitlines():
            with contextlib.suppress(Exception):  # one bad record must not block the rest
                rec = json.loads(ln)
                if (not isinstance(rec, dict) or not rec.get("text")
                        or rec.get("id") in self.delivered
                        or rec.get("id") in self._consumed):
                    continue
                lane = str(rec.get("lane") or "")
                # RE-CHECK terminal verdicts the spawn-time snapshot can't see: the epoch set is
                # read once, so a retraction/expiry landing AFTER this pump spawned was invisible —
                # the pump would deliver a message the operator already took back (field incident
                # 2026-08-21: skips announced for a record retracted 70s earlier; a live-lane pump
                # would have DELIVERED it). Terminal ids ONLY — post-epoch delivery entries stay
                # invisible on purpose (a cold fallback sharing its dead predecessor's epoch must
                # redeliver acked steering). Lazy: one ledger read per tick at most.
                if fresh is None:
                    fresh = terminal_ids(Path(self.ctx.run_dir))
                if rec.get("id") in fresh:
                    self._consumed.add(rec["id"])  # settled — never re-examine this record
                    continue
                # prefix match: '--lane ground' reaches ground.b1..b5 AND FULL mode's plain 'ground';
                # exact match silently evaporated both (indistinguishable from a delivery bug).
                # A skipped record is NOT consumed — it stays queued for its target lane's stage.
                if lane and not (self.label == lane or self.label.startswith(lane + ".")):
                    self._announce_skip(rec, lane)
                    continue
                # mark BEFORE the write: at-most-once. Mark-after redelivered the same authoritative
                # turn every tick after a transient flush error — N identical operator interventions.
                # A lost message surfaces as a missing 🧭 ack the operator can re-send.
                self.delivered.add(rec["id"])
                mark_consumed(Path(self.ctx.run_dir), rec["id"], self.label)
                self.proc.stdin.write(user_turn(TEMPLATE.format(text=rec["text"])))
                self.proc.stdin.flush()
                from . import decisions
                from .constants import Decision
                from .engine import log_activity

                log_activity(self.ctx.run_dir, self.label,
                             f"🧭 operator steering delivered ({len(rec['text'])} chars)")
                decisions.record(
                    self.ctx, self.label.split(".")[0], Decision.STEERING_DELIVERED,
                    why="operator steering from the live inbox delivered to the running agent as "
                    "an authoritative user turn (queued to its next turn boundary)",
                    lane=self.label, chars=len(rec["text"]), obs_parent=self.obs_parent,
                    # synthetic = a feature-test steer (feed form's `test` toggle): kept in-band —
                    # a test must ride the real session to test anything — but marked, so steer-
                    # mining excludes it (OTel user_agent.synthetic.type / GA4 traffic_type shape)
                    **({"synthetic": True} if rec.get("synthetic") else {}),
                )

    def _announce_skip(self, rec: dict, lane: str) -> None:
        """Every non-delivery is visible (0.24.2): a mistyped lane used to evaporate silently while
        the operator watched the run believing they had corrected it."""
        if rec["id"] in self.skipped:
            return
        self.skipped.add(rec["id"])
        from . import decisions
        from .constants import Decision
        from .engine import log_activity

        log_activity(self.ctx.run_dir, self.label,
                     f"🧭 steering skipped (lane '{lane}' does not match this agent)")
        decisions.record(
            self.ctx, self.label.split(".")[0], Decision.STEERING_SKIPPED,
            why="steering record's lane does not match this agent (lanes prefix-match: "
            "'ground' reaches ground.b1..bN)",
            lane=self.label, target_lane=lane, obs_parent=self.obs_parent,
        )

    # -- lifecycle --------------------------------------------------------------------------------
    def _scan_for_result(self) -> None:
        """Set the sticky _saw_result flag when the terminal result event appears. Detection is
        single-shot by construction (_pos moves past the line), which is exactly why the CLOSE must
        key off the flag, not off re-detection — a transiently-failed close is retried by tick()."""
        if self.closed or self.proc.stdin is None or not self.log_path.exists():
            return
        with open(self.log_path, "rb") as f:
            f.seek(self._pos)
            chunk = f.read()
        nl = chunk.rfind(b"\n")
        if nl < 0:
            return
        lines, self._pos = chunk[: nl + 1], self._pos + nl + 1
        for raw in lines.splitlines():
            with contextlib.suppress(Exception):
                if json.loads(raw).get("type") == "result":
                    self._saw_result = True
                    return

    def close(self) -> None:
        if self.closed:
            return
        try:
            if self.proc.stdin is not None:
                self.proc.stdin.close()
            self.closed = True
        except BrokenPipeError:
            self.closed = True  # child already dead — its stdin no longer matters
        except Exception:
            return  # transient: DON'T latch — tick() retries off _saw_result until this succeeds
            # (the engine's finally is the last resort on kill paths)
