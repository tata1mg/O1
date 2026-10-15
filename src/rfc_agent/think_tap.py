"""Thinking tap: a per-child localhost sidecar proxy that makes the agents' REAL reasoning text visible.

Why this exists (probed 2026-08-07, CLI 2.1.224): on the 5-family models the API defaults thinking
`display` to "omitted" — thinking blocks stream with an EMPTY text field. The documented opt-back-in is a
request-body field, `thinking: {"type": "adaptive", "display": "summarized"}` (billing identical either
way, per the platform docs). No CLI surface sets it headless: `showThinkingSummaries` was probed inert in
BOTH settings scopes for `-p` runs, and a wire tap proved the CLI blanks thinking text client-side even
when the API returns it. So the only place the text exists is ON THE WIRE — and that is where gateways
(LiteLLM et al.) capture it. This module does the same, minimally:

  child --ANTHROPIC_BASE_URL--> [this proxy, 127.0.0.1 bind + local_request_ok] --TLS--> api.anthropic.com

The loopback bind alone is NOT a request boundary (0.24.1 lesson, tools/feed_server.py): any web
page the operator has open can POST to a loopback port without a preflight, and a hostile page
hitting this proxy would have its response run through ThinkExtractor — attacker-authored 💭 lines
appended to the run's live feed and thinking audit. Every request is therefore gated by
rfc_agent.localnet.local_request_ok (reject any Origin, require a loopback Host) — the tap's only
legitimate client is the local claude child, which sends neither an Origin nor a foreign Host.

The proxy forwards requests verbatim except for setting `display: "summarized"` on non-disabled thinking
configs, extracts `thinking_delta` text from the SSE response as it streams THROUGH (the child still gets
every byte), appends throttled 💭 lines to the run's unified activity feed, and saves the full text to
logs/<label>.thinking.txt for post-run forensics. If the proxy cannot start — or the operator already
routes through a gateway via ANTHROPIC_BASE_URL — the engine launches the child without it: behavior is
exactly today's (depth heartbeats only), never a broken run.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

# absolute import, not relative: this file also runs as a bare script (start_tap spawns
# `sys.executable __file__`), where a relative import has no package context
from rfc_agent.localnet import local_request_ok

_UPSTREAM = "api.anthropic.com"
# flush a 💭 line per this many chars of thinking — cadence only, for liveness; the text is never
# truncated (mirrors engine._MIND_STEP). Full reasoning also lands verbatim in <label>.thinking.txt.
_STEP = 400


def mutate_body(body: bytes) -> tuple[bytes, bool]:
    """Set thinking.display='summarized' on a /v1/messages request body. Never raises."""
    try:
        j = json.loads(body)
        th = j.get("thinking") or {"type": "adaptive"}
        if not isinstance(th, dict) or th.get("type") == "disabled":
            return body, False
        th["display"] = "summarized"
        j["thinking"] = th
        return json.dumps(j).encode(), True
    except Exception:
        return body, False


class ThinkExtractor:
    """Incremental SSE scanner: feed raw response bytes, get throttled 💭 snippet lines back.

    Parses only complete `data: {...}` lines (partial tail is buffered), so chunk boundaries anywhere —
    even mid-UTF-8 — never corrupt a snippet."""

    def __init__(self) -> None:
        self._buf = b""
        self._text = ""
        self._emitted = 0

    def feed(self, chunk: bytes) -> list[str]:
        self._buf += chunk
        out: list[str] = []
        while b"\n" in self._buf:
            line, self._buf = self._buf.split(b"\n", 1)
            if not line.startswith(b"data:"):
                continue
            try:
                ev = json.loads(line[5:])
            except Exception:
                continue
            d = ev.get("delta") or {}
            if d.get("type") == "thinking_delta" and d.get("thinking"):
                self._text += d["thinking"]
                if len(self._text) - self._emitted >= _STEP:
                    out.append(self._flush())          # periodic, for liveness
            elif ev.get("type") == "content_block_stop" and len(self._text) > self._emitted:
                out.append(self._flush(final=True))    # the block's tail
        return [s for s in out if s]

    def _flush(self, final: bool = False) -> str:
        """Emit the reasoning since the last flush — NOT truncated. _STEP only sets the cadence so a
        long block streams instead of landing all at once; the full text is always shown (and also
        saved verbatim to <label>.thinking.txt). Mid-block flushes stop at the last sentence boundary
        so a line reads as whole thoughts; the final flush takes everything that remains."""
        fresh = self._text[self._emitted:]
        cut = len(fresh)
        if not final:
            b = max(fresh.rfind(". "), fresh.rfind("\n"))
            if b >= 40:  # a boundary exists — leave the unfinished sentence for the next flush
                cut = b + 1
        piece = " ".join(fresh[:cut].split())
        self._emitted += cut
        return f"💭 {piece}" if piece else ""

    @property
    def full_text(self) -> str:
        return self._text


# pragma-no-cover rationale: exercised via subprocess in tests, invisible to coverage
def _serve(port: int, activity_log: Path, label: str, raw_out: Path) -> None:  # pragma: no cover
    import http.client
    import http.server

    class H(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *a):  # silence per-request stderr noise
            pass

        def _log(self, line: str) -> None:
            try:
                with open(activity_log, "a", encoding="utf-8") as f:
                    f.write(f"[{time.strftime('%H:%M:%S')}] [{label}] {line}\n")
            except Exception:
                pass

        def _boundary_ok(self) -> bool:
            """local_request_ok gate on EVERY verb — a rejected request must not reach the
            upstream, the extractor, or the run's feed. Reject before reading the body, and
            close the connection (an unread body would poison the next keep-alive request)."""
            ok, _why = local_request_ok(self.headers)
            if not ok:
                self.send_response(403)
                self.send_header("Connection", "close")
                self.end_headers()
                self.close_connection = True
            return ok

        def do_POST(self):
            if not self._boundary_ok():
                return
            body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
            if "/v1/messages" in self.path:
                body, _ = mutate_body(body)
            conn = http.client.HTTPSConnection(_UPSTREAM, timeout=600)
            hdrs = {k: v for k, v in self.headers.items()
                    if k.lower() not in ("host", "content-length", "accept-encoding", "connection")}
            hdrs["Host"] = _UPSTREAM
            hdrs["Content-Length"] = str(len(body))
            conn.request("POST", self.path, body=body, headers=hdrs)
            resp = conn.getresponse()
            self.send_response(resp.status)
            for k, v in resp.getheaders():
                if k.lower() not in ("transfer-encoding", "content-encoding", "content-length", "connection"):
                    self.send_header(k, v)
            self.send_header("Connection", "close")
            self.end_headers()
            ex = ThinkExtractor()
            while True:
                chunk = resp.read1(65536)  # read1: forward whatever arrived — no full-buffer latency
                if not chunk:
                    break
                for line in ex.feed(chunk):
                    self._log(line)
                self.wfile.write(chunk)
                self.wfile.flush()
            conn.close()
            if ex.full_text:
                try:
                    with open(raw_out, "a", encoding="utf-8") as f:
                        f.write(ex.full_text + "\n---\n")
                except Exception:
                    pass
            self.close_connection = True

        def do_GET(self):
            if not self._boundary_ok():
                return
            conn = http.client.HTTPSConnection(_UPSTREAM, timeout=60)
            hdrs = {k: v for k, v in self.headers.items() if k.lower() not in ("host", "connection")}
            hdrs["Host"] = _UPSTREAM
            conn.request("GET", self.path, headers=hdrs)
            resp = conn.getresponse()
            data = resp.read()
            self.send_response(resp.status)
            for k, v in resp.getheaders():
                if k.lower() not in ("transfer-encoding", "content-encoding", "content-length", "connection"):
                    self.send_header(k, v)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            conn.close()

        do_HEAD = do_GET

    # loopback bind + per-request local_request_ok gate: the bind alone is not a boundary —
    # browser pages and DNS-rebound tabs reach loopback ports (see rfc_agent/localnet.py)
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", port), H)
    print(f"PORT {srv.server_address[1]}", flush=True)  # handshake line the engine waits for
    srv.serve_forever()


@dataclass
class Tap:
    proc: subprocess.Popen
    base_url: str

    def stop(self) -> None:
        try:
            self.proc.terminate()
        except Exception:
            pass


def start_tap(activity_log: Path, label: str, raw_out: Path) -> Tap | None:
    """Spawn the sidecar; return None (engine falls back to no-tap) rather than ever failing a run."""
    # a value pointing at api.anthropic.com is just the default spelled out (harnesses set it;
    # measured 2026-08-07: treating it as a gateway silently disabled the tap on every run)
    base = os.environ.get("ANTHROPIC_BASE_URL", "")
    if base and _UPSTREAM not in base:
        return None  # operator routes through a REAL gateway — don't get between them
    try:
        proc = subprocess.Popen(
            [sys.executable, __file__, "0", str(activity_log), label, str(raw_out)],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
        )
        line = ""
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            line = proc.stdout.readline()
            if line:
                break
        if not line.startswith("PORT "):
            proc.terminate()
            return None
        port = int(line.split()[1])
        # verify it accepts connections before handing it a child's whole API traffic
        with socket.create_connection(("127.0.0.1", port), timeout=2):
            pass
        return Tap(proc=proc, base_url=f"http://127.0.0.1:{port}")
    except Exception:
        return None


if __name__ == "__main__":
    _serve(int(sys.argv[1]), Path(sys.argv[2]), sys.argv[3], Path(sys.argv[4]))
