"""Thinking tap: the sidecar that recovers reasoning text the headless CLI blanks.

Ground truth (wire-tapped 2026-08-07, CLI 2.1.224, sonnet-5): with display="summarized" injected the API
streams real thinking_delta text, and the CLI still emits empty thinking in stream-json — so the ONLY
place to read it is between the CLI and the API. These tests pin the three load-bearing pieces: the body
mutation (documented API field, nothing else touched), the incremental SSE extraction (chunk boundaries
anywhere), and the never-break-a-run contract of start_tap.
"""

import json
import os
from pathlib import Path
from unittest import mock

from rfc_agent import think_tap
from rfc_agent.think_tap import ThinkExtractor, mutate_body, start_tap


def _sse(events):
    return b"".join(b"data: " + json.dumps(e).encode() + b"\n\n" for e in events)


def test_tap_is_opt_in_and_off_by_default():
    """The tap intercepts AUTHENTICATED traffic, so it must never turn itself on.

    Pins both halves of the contract: the shipped template says false, and the engine's own default —
    the value used when a config predates the key — is false too."""
    import inspect

    from rfc_agent import engine

    tmpl = json.loads((Path(__file__).parents[2] / "config.harness.json").read_text())
    assert tmpl["AGENT"]["THINKING_TAP"] is False
    src = inspect.getsource(engine.run_claude)
    assert 'cfg.get("AGENT.THINKING_TAP", False)' in src


def test_mutate_body_sets_display_and_nothing_else():
    body = json.dumps({"model": "claude-sonnet-5", "max_tokens": 8, "messages": []}).encode()
    out, changed = mutate_body(body)
    assert changed
    j = json.loads(out)
    assert j["thinking"] == {"type": "adaptive", "display": "summarized"}
    assert j["model"] == "claude-sonnet-5" and j["max_tokens"] == 8  # everything else verbatim

    # explicit config gains only the display field
    body2 = json.dumps({"thinking": {"type": "adaptive"}, "messages": []}).encode()
    assert json.loads(mutate_body(body2)[0])["thinking"]["display"] == "summarized"

    # disabled thinking and non-JSON pass through untouched
    body3 = json.dumps({"thinking": {"type": "disabled"}}).encode()
    assert mutate_body(body3) == (body3, False)
    assert mutate_body(b"\x00notjson") == (b"\x00notjson", False)


def test_extractor_survives_arbitrary_chunk_boundaries():
    text = "First I check the flag boundary. " * 20  # well past one _STEP
    stream = _sse([
        {"type": "content_block_delta", "delta": {"type": "thinking_delta", "thinking": text[:100]}},
        {"type": "content_block_delta", "delta": {"type": "thinking_delta", "thinking": text[100:]}},
        {"type": "content_block_stop"},
        {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "answer"}},
    ])
    for size in (1, 7, 4096):  # byte-at-a-time to whole-buffer
        ex = ThinkExtractor()
        lines = []
        for i in range(0, len(stream), size):
            lines.extend(ex.feed(stream[i:i + size]))
        assert ex.full_text == text
        assert lines and all(ln.startswith("💭 ") for ln in lines)
        # NO cap: the emitted lines together reproduce the whole thought, nothing truncated
        joined = " ".join(ln.removeprefix("💭 ") for ln in lines)
        assert " ".join(joined.split()) == " ".join(text.split())
        assert "answer" not in "".join(lines)  # text_delta is not reasoning


def test_extractor_empty_thinking_stays_silent():
    ex = ThinkExtractor()
    assert ex.feed(_sse([
        {"type": "content_block_delta", "delta": {"type": "thinking_delta", "thinking": ""}},
        {"type": "content_block_stop"},
    ])) == []


def test_start_tap_respects_existing_gateway(tmp_path):
    with mock.patch.dict(os.environ, {"ANTHROPIC_BASE_URL": "https://gateway.corp"}):
        assert start_tap(tmp_path / "a.log", "x", tmp_path / "raw.txt") is None


def test_start_tap_returns_none_on_broken_sidecar(tmp_path, monkeypatch):
    monkeypatch.setattr(think_tap, "__file__", str(tmp_path / "nope.py"))
    assert start_tap(tmp_path / "a.log", "x", tmp_path / "raw.txt") is None


def test_default_base_url_is_not_a_gateway(tmp_path, monkeypatch):
    # harnesses export ANTHROPIC_BASE_URL=https://api.anthropic.com — the default spelled out, not a
    # gateway; the guard must not disable the tap for it (it did: every harness-launched run lost the tap)
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://api.anthropic.com")
    tap = start_tap(tmp_path / "activity.log", "probe", tmp_path / "raw.txt")
    assert tap is not None
    tap.stop()


def test_tap_handshake_and_loopback_only(tmp_path, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_BASE_URL", raising=False)
    tap = start_tap(tmp_path / "activity.log", "probe", tmp_path / "raw.txt")
    assert tap is not None, "sidecar failed to start on a healthy host"
    try:
        # loopback only — OAuth headers never leave the box except to the upstream
        assert tap.base_url.startswith("http://127.0.0.1:")
    finally:
        tap.stop()


def test_local_request_ok_boundary():
    """Mirror of test_feed_server.test_steer_csrf_boundary for the local-process-only policy:
    the tap's only legit client (the claude child) sends no Origin and a loopback Host, so ANY
    Origin is rejected — the opposite of the feed page's own-origin allowlist."""
    from rfc_agent.localnet import local_request_ok

    assert local_request_ok({"Host": "127.0.0.1:9999"})[0], "the claude child's shape must pass"
    assert local_request_ok({"Host": "localhost:9999"})[0]
    assert not local_request_ok({"Host": "127.0.0.1:9999", "Origin": "http://evil.example"})[0]
    assert not local_request_ok({"Host": "127.0.0.1:9999", "Origin": "http://127.0.0.1:9999"})[0], \
        "even a same-origin-looking Origin means a browser sent it — no browser is a legit client"
    assert not local_request_ok({"Host": "evil.example"})[0], "DNS rebinding (non-loopback Host)"
    assert not local_request_ok({})[0], "no Host at all is not loopback"


def test_tap_rejects_browser_shaped_requests_without_touching_feed(tmp_path, monkeypatch):
    """Live round-trip: a no-preflight browser POST (Origin set) and a DNS-rebound GET (foreign
    Host) must 403 at the tap — never reach the upstream, ThinkExtractor, or the run's live feed
    (a hostile page could otherwise append attacker-authored 💭 lines to the thinking audit)."""
    import http.client

    monkeypatch.delenv("ANTHROPIC_BASE_URL", raising=False)
    activity = tmp_path / "activity.log"
    tap = start_tap(activity, "probe", tmp_path / "raw.txt")
    assert tap is not None, "sidecar failed to start on a healthy host"
    try:
        port = int(tap.base_url.rsplit(":", 1)[1])

        c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        c.request("POST", "/v1/messages", body=b'{"thinking_injection":"fake"}',
                  headers={"Origin": "http://evil.example", "Content-Type": "application/json"})
        assert c.getresponse().status == 403
        c.close()

        c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        c.request("GET", "/v1/models", headers={"Host": "evil.example"})
        assert c.getresponse().status == 403
        c.close()
    finally:
        tap.stop()
    feed = activity.read_text() if activity.exists() else ""
    assert "💭" not in feed and "fake" not in feed, "rejected requests must leave no feed trace"
