"""Feed page: the line-classifier and per-agent colouring are the only logic worth pinning.

The page is a terminal-style log (one file, no build step). Its single behavioural addition over a plain
transcript is colour: each line is classed by what it IS (💬 narration / 💭 reasoning / ⛔ denial / gate
pass·fail / lifecycle / tool) and each agent label gets a stable colour. These check the regexes that
drive both, extracted so they can be tested without a browser.
"""

import re
from pathlib import Path

PAGE = (Path(__file__).parents[2] / "tools" / "feed_server.py").read_text()


def _classify(msg):
    """Mirror of the page's cls() — kept in lockstep by test_classifier_matches_page below."""
    if msg.startswith("⛔"):
        return "deny"
    if msg.startswith("gate PASSED"):
        return "pass"
    if msg.startswith("gate FAILED"):
        return "fail"
    if msg.startswith("💬"):
        return "say"
    if msg.startswith("💭"):
        return "think"
    if re.match(r"^(STAGE |agent |revision)", msg):
        return "mile"
    return "tool"


def test_every_line_kind_gets_its_own_class():
    cases = {
        "⛔ denied [permission]: Bash git ...": "deny",
        "gate PASSED — stage done (108s)": "pass",
        "gate FAILED (3 failure(s)) — retry budget exhausted": "fail",
        "💬 Now let's re-verify no bare citations remain": "say",
        "💭 thought for ~250 tokens (text withheld ...)": "think",
        "STAGE started": "mile",
        "agent finished": "mile",
        "revision — RESUMING the authoring session": "mile",
        "Bash grep -n foo /path": "tool",
        "Read app/utils/utils.py": "tool",
    }
    for msg, want in cases.items():
        assert _classify(msg) == want, f"{msg!r} -> {_classify(msg)} (want {want})"


def test_narration_and_failure_are_visually_strongest():
    """💬 and gate FAILED are the lines a human must not miss — both bold, fail is red."""
    assert ".say{color:#f5f3ff;font-weight:600}" in PAGE
    assert ".fail{color:#f87171;font-weight:600}" in PAGE
    assert ".tool{color:#8a8a8a}" in PAGE  # tool calls recede


def test_agent_colours_are_stable_and_distinct():
    """Same label -> same colour every time; a fresh label takes the next hue (up to 9 lanes/run)."""
    assert "seen.has(l)" in PAGE and "seen.size%AGENT.length" in PAGE
    palette = re.search(r"const AGENT=\[([^\]]+)\]", PAGE).group(1)
    assert len(re.findall(r"#[0-9a-f]{6}", palette)) >= 9


def test_classifier_matches_page():
    """The page's cls() and this file's mirror must stay identical — pin the source order."""
    for token in ("/^⛔/.test(m)?'deny'", "/^gate PASSED/.test(m)?'pass'",
                  "/^gate FAILED/.test(m)?'fail'", "/^💬/.test(m)?'say'",
                  "/^💭/.test(m)?'think'", "/^(STAGE |agent |revision)/.test(m)?'mile':'tool'"):
        assert token in PAGE, token


def test_page_ships_the_steering_inbox(tmp_path):
    """0.24.0: the feed page grows an operator input (lane + message) posting to /steer, and the
    server appends the same raw record shape `rfc-agent --steer` writes — RAW text, wrapped at
    delivery by the harness so the template lives in one place (rfc_agent/steer.py)."""
    assert "sendSteer" in PAGE and "fetch('/steer'" in PAGE
    assert 'id=lane' in PAGE and 'id=msg' in PAGE
    assert "do_POST" in PAGE and "steer.inbox.jsonl" in PAGE
    assert "operator_actions.jsonl" in PAGE  # every steer is an audited operator action
    assert "HUMAN STEERING" not in PAGE, "the template must live in steer.py only, never duplicated"


def _feed_server():
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "feed_server", Path(__file__).parents[2] / "tools" / "feed_server.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # import-safe: serve_forever is behind __main__ (0.24.1)
    return mod


def test_steer_csrf_boundary():
    """0.24.1: POST /steer was CSRF-able — a no-preflight simple POST from any web page (or a
    DNS-rebound tab) injected an authoritative steering turn. Three layers, each testable."""
    fs = _feed_server()
    own = f"http://127.0.0.1:{fs.PORT}"
    ok = {"X-Rfc-Steer": "1", "Host": f"127.0.0.1:{fs.PORT}"}
    assert fs.steer_request_ok(ok)[0], "curl / --steer shape (no Origin) must pass"
    assert fs.steer_request_ok({**ok, "Origin": own})[0], "the feed page's own same-origin fetch"
    assert fs.steer_request_ok({**ok, "Origin": f"http://localhost:{fs.PORT}"})[0]
    assert not fs.steer_request_ok({"Host": f"127.0.0.1:{fs.PORT}"})[0], "no custom header -> simple request -> reject"
    assert not fs.steer_request_ok({**ok, "Origin": "http://evil.example"})[0], "cross-origin"
    assert not fs.steer_request_ok({**ok, "Host": "evil.example"})[0], "DNS rebinding (non-loopback Host)"


def test_feed_has_no_cors_wildcard_and_page_sends_the_header():
    assert 'send_header("Access-Control-Allow-Origin"' not in PAGE, \
        "ACAO:* handed the live transcript to any page the operator had open"
    assert "'X-Rfc-Steer':'1'" in PAGE, "the page's own fetch must satisfy the custom-header layer"


def test_steer_bar_ux_contract():
    """0.24.6 steering-bar UX (field report): the four behaviors that made it feel dead, pinned.
    Lane suggestions come from the feed itself (labels + lifecycle lines), Enter-with-no-text is
    never a silent no-op, double-Enter can't double-send, and the send names its target."""
    # IN-PAGE dropdown, not <datalist>: the native popup renders in screen coordinates and lands
    # detached from the input inside embedded browser panes (field screenshot, 0.24.7)
    assert "<datalist id=" not in PAGE and "list=lanes" not in PAGE, \
        "native datalist popups detach from the input in embedded panes"
    assert 'id=lsug' in PAGE and "bottom:calc(100% + 8px)" in PAGE, "own DOM dropdown anchored above the box"
    assert "ArrowDown" in PAGE and "ev.key==='Enter'" in PAGE and "Escape" in PAGE, "keyboard navigation"
    assert "onmousedown=ev=>{ev.preventDefault();pick(v)}" in PAGE, "click-pick survives the input blur"
    assert "agent started" in PAGE and "agent finished" in PAGE, "live-lane tracking from lifecycle lines"
    assert "all '+p+' agents" in PAGE and "queued until stage starts" in PAGE, \
        "suggestions teach prefix-matching and queued future-stage lanes"
    assert "msg.focus();return false" in PAGE, "empty submit moves focus instead of doing nothing"
    assert "go.disabled=true" in PAGE, "in-flight send disables the button (no duplicate authoritative turns)"
    assert "all live agents" in PAGE, "status names the target"
    assert "maxlength=4000" in PAGE, "client cap matches the server's silent truncation"
    # steer outbox (0.24.8): every sent message walks the 🧭 lifecycle as a chip — sent → delivered
    # (amber, per-lane count) → read by the agent (green, from the replayed-turn ack) → pops
    assert "addChip(text,laneV,j.id,j.routing)" in PAGE and "chipDelivered" in PAGE and "chipRead" in PAGE
    assert "steering read by the agent" in PAGE, "chips key on the drain's read ack line"
    assert "setTimeout(()=>popChip(c),15000)" in PAGE, \
        "older-harness stages emit no read line — a delivered chip must still pop on a timer"


def test_footer_layout_and_dropdown_state_contract():
    """0.24.13 (field screenshot): four structural defects, each pinned.
    (1) the dropdown auto-opened on reload — trackLane sniffed lsug.style.display, which is ''
    (not 'none') on a fresh page since display:none lives in the stylesheet; state is now an
    explicit flag. (2) no open/close affordance — a chevron now rotates with aria-expanded.
    (3) chip strip at bottom:56px and jump button at 64px overlapped — one sticky footer now owns
    chips+bar and a ResizeObserver rides the ↓ above it at any height. (4) fixed-width inputs
    overflowed a collapsed pane — the lane flexes with a floor and the message can shrink."""
    assert "style.display!=='none'" not in PAGE, "state is the sugOpen flag, never style-sniffing"
    assert "let sel=0,sugOpen=false" in PAGE and "if(sugOpen)renderSug()" in PAGE
    assert "id=lchev" in PAGE and "aria-expanded" in PAGE and "rotate(180deg)" in PAGE, "chevron affordance"
    assert "id=foot" in PAGE and "new ResizeObserver" in PAGE and "foot.offsetHeight" in PAGE, \
        "one sticky footer + observer-driven jump button, no hardcoded bottom offsets"
    assert "bottom:56px" not in PAGE, "the overlapping hardcoded chip-strip offset is gone"
    assert "flex:0 1 190px;min-width:104px;max-width:38%" in PAGE and "flex:1;min-width:0" in PAGE, \
        "lane shrinks with a floor AND yields on narrow panes; the message input may shrink"
    assert "j.style.display='none'" in PAGE and "||sugOpen" in PAGE, "no ↓/dropdown overlap while open"


def test_composer_anatomy_contract():
    """0.24.14 (field screenshot, ultrawide window): the bar floated mid-screen over dead space and
    stretched across the full width with a labeled button. Converged agent-composer anatomy now:
    (1) full-height flex column so the footer sits at the VIEWPORT bottom even when the log is
    short — bare sticky only pins once content overflows; (2) content centered at a readable
    width; (3) ONE rounded container with the focus ring on it, a borderless field, and a circular
    icon send button inside; (4) queued chips are click-dismissable, never stranded."""
    assert "min-height:100vh;min-height:100dvh;display:flex;flex-direction:column" in PAGE
    assert "#f{flex:1 0 auto}" in PAGE
    assert "max-width:768px;margin:0 auto" in PAGE, "the converged 48rem composer column"
    assert "#composer:focus-within" in PAGE and "background:transparent" in PAGE, \
        "one container carries the focus ring; fields are borderless inside it"
    assert 'aria-label="send steering message"' in PAGE and "M8 13V3M4 7l4-4 4 4" in PAGE, \
        "icon send button (SVG arrow) with an accessible name, not a text label"
    # 0.24.25: click on an UNDELIVERED chip retracts the queued record (a cosmetic dismiss left
    # the record parked, primed to ambush a later run); delivered chips keep the cosmetic pop
    # Material input-chip anatomy (0.24.27): label + SEPARATE trailing \u2715 target; chip body
    # inert; retract executes immediately with an UNDO offer (NN/g: undo over confirmation for
    # reversible actions) — the two-step "click again" arm and the body-click retract are gone
    assert "fetch('/retract'" in PAGE and "offerUndo(c)" in PAGE
    assert "className='x'" in PAGE and "aria-label" in PAGE
    assert "click again to retract" not in PAGE


def test_addressing_language_and_dormant_lane_semantics():
    """0.24.15: the field is ADDRESSING, not routing — 'lane' was harness-internal jargon (the
    operator thinks 'agent'; messaging convention is To:), so the UI says 'to' while values stay
    the feed's own labels and the CLI keeps --lane. Finished lanes are DORMANT, not conversational:
    they sort last with the honest note (a steer delivers only if that agent re-runs — back-jump,
    retry, --force), and finished SHARDS collapse under their stage prefix."""
    assert 'placeholder="to · all live agents"' in PAGE and "CLI equivalent: --lane" in PAGE
    assert "lane · all live" not in PAGE, "the jargon placeholder is gone"
    assert "finished — delivers only if it re-runs" in PAGE, "dormant semantics stated, not implied"
    assert "rank={live:0,prefix:1,queued:2,finished:3}" in PAGE, "live first, dormant last"
    assert "if(!l.includes('.')&&!o.has(l))" in PAGE, "finished shards collapse under their prefix"


def test_lane_states_reads_the_whole_log_and_last_event_wins(tmp_path):
    """0.24.15 follow-up (field report): a fresh page mislabeled finished stages as queued because
    the SSE tail is a ~4KB window. /lanes computes lifecycle from the WHOLE log; last event per
    label wins, so a stage re-running after a back-jump reads live again."""
    fs = _feed_server()
    log = tmp_path / "activity.log"
    log.write_text("\n".join([
        "[10:00:00] [ingest] agent started",
        "[10:01:00] [ingest] agent finished",
        "[10:02:00] [ground.b1] agent started",
        "[10:03:00] [ground.b1] agent finished",
        "[10:04:00] [generate] agent started",
        "[10:05:00] [generate] agent finished",
        "[10:06:00] [generate] agent started",     # review back-jump: re-running => live again
        "[10:06:01] [generate] Bash rg -n 'agent started fake content'",  # non-lifecycle noise
    ]) + "\n")
    st = fs.lane_states(log)
    assert st == {"live": ["generate"], "finished": ["ground.b1", "ingest"]}
    assert fs.lane_states(tmp_path / "missing.log") == {"live": [], "finished": []}
    assert "fetch('/lanes')" in PAGE, "the page seeds lifecycle state from the full-log endpoint"


def test_feed_replays_history_and_resumes_without_duplicates():
    """0.24.16 (field report: 'why do previous logs get deleted on reload?'): the old handler
    started every SSE connection at the last 4KB, so a reload lost all history — and a server
    restart made the browser's auto-reconnect replay that tail AGAIN as duplicates. Now: fresh
    page = full history (capped), reconnect = exact byte resume via Last-Event-ID, and only
    COMPLETE lines are emitted (the old code tore the trailing partial line in two)."""
    fs = _feed_server()

    # resume_pos: fresh page -> capped history start; valid id -> exact resume; stale/garbage -> capped
    assert fs.resume_pos(None, 500_000) == 0, "a whole run's log replays in full"
    assert fs.resume_pos(None, fs._REPLAY_CAP + 10) == 10, "pathological logs start at the cap"
    assert fs.resume_pos("1234", 500_000) == 1234, "reconnect resumes at the exact byte offset"
    assert fs.resume_pos("999999999", 500) == 0, "id past EOF (rotated log) -> capped start"
    assert fs.resume_pos("junk", 500) == 0

    # sse_frames: ids are byte offsets AFTER each line; a partial tail is deferred, never torn
    frames, pos = fs.sse_frames(0, b"alpha\nbeta\npartial")
    assert pos == 11, "consumed exactly the two complete lines"
    assert frames == b"id: 6\ndata: alpha\n\nid: 11\ndata: beta\n\n"
    assert fs.sse_frames(pos, b"partial") == (b"", 11), "no newline yet -> emit nothing, hold position"
    frames2, pos2 = fs.sse_frames(11, b"partial done\n")
    assert pos2 == 24 and b"data: partial done" in frames2, "the completed line arrives whole"


def test_skipped_steer_gets_chip_feedback():
    """Field report 2026-08-20: a lane-'ground' steer during a --stages review run was skipped by
    every live agent (activity log + Langfuse decision both said so) while the feed chip pulsed
    'awaiting delivery' forever — the one surface the operator watches gave no feedback. The page
    must classify the skip line and move the chip to an honest state."""
    assert "steering skipped \\(lane '(.*)' does not match this agent\\)" in PAGE  # classifier branch
    assert "chipSkipped(mm[1],label)" in PAGE                    # wired to the lifecycle
    assert "x.lane===laneV&&x.state==='sent'" in PAGE            # only never-delivered chips flip to skip
    assert "stays queued and delivers if that lane runs later" in PAGE  # honest operator-level title
    assert ".chip.skip{" in PAGE                                 # distinct visual state


def _live_server(tmp_path, log_lines=""):
    """Real ThreadingHTTPServer on an ephemeral port, FEED pointed at a temp run dir's log."""
    import threading
    from http.server import ThreadingHTTPServer

    fs = _feed_server()
    logs = tmp_path / "run" / "logs"
    logs.mkdir(parents=True)
    (logs / "activity.log").write_text(log_lines)
    fs.FEED = logs / "activity.log"
    srv = ThreadingHTTPServer(("127.0.0.1", 0), fs.H)
    fs.PORT = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return fs, srv, tmp_path / "run"


def _post(port, path, body):
    import http.client
    import json as _json

    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    conn.request("POST", path, body=_json.dumps(body),
                 headers={"Content-Type": "application/json", "X-Rfc-Steer": "1",
                          "Host": f"127.0.0.1:{port}"})
    r = conn.getresponse()
    data = r.read()
    conn.close()
    import contextlib as _ctx
    out = None
    with _ctx.suppress(Exception):
        out = _json.loads(data)
    return r.status, out


def test_routing_is_informational_never_blocking(tmp_path):
    """Research round 2 (2026-08-21): with fate bounded (invocation TTL) and messages retractable
    (undo), every precedent — store-and-forward messengers, K8s Pending+FailedScheduling, RabbitMQ
    async returns, Cursor's confirm-free queue — accepts optimistically and shows status ON the
    message. POST /steer therefore ALWAYS enqueues; `routing` in the response is informational and
    seeds the chip's initial state (live / finished / unknown / prerun)."""
    log = ("[10:00:00] [ground.b1] agent started\n"
           "[10:05:00] [ground.b1] agent finished\n"
           "[10:06:00] [review.architect] agent started\n")
    fs, srv, run_dir = _live_server(tmp_path, log)
    try:
        inbox = run_dir / "logs" / "steer.inbox.jsonl"
        status, j = _post(fs.PORT, "/steer", {"lane": "ground", "text": "trim the scan"})
        assert status == 200 and j.get("id") and j["routing"] == "finished"
        assert "trim the scan" in inbox.read_text(), "always accepted — TTL bounds the fate"

        status, j = _post(fs.PORT, "/steer", {"lane": "review", "text": "verify CHANGE-6"})
        assert status == 200 and j.get("id") and j["routing"] == "live"

        status, j = _post(fs.PORT, "/steer", {"lane": "generate", "text": "tighten"})
        assert status == 200 and j.get("id") and j["routing"] == "unknown"
        assert "tighten" in inbox.read_text(), "never-seen lanes enqueue too (typo guard is UI-side)"
    finally:
        srv.shutdown()


def test_synthetic_marker_flows_from_form_to_records(tmp_path):
    """Fix (c) of the 2026-08-24 trace audit: test steers polluted radiology-v3's steering corpus
    with no way to filter them. The form's `test` toggle stamps synthetic=true onto the inbox
    record AND the operator action (both flow into the trace); unmarked steers stay unmarked."""
    import json
    fs, srv, run_dir = _live_server(tmp_path, "[10:00:00] [generate] agent started\n")
    try:
        status, j = _post(fs.PORT, "/steer", {"lane": "", "text": "just testing", "synthetic": True})
        assert status == 200 and j.get("id")
        inbox = [json.loads(ln) for ln in
                 (run_dir / "logs" / "steer.inbox.jsonl").read_text().splitlines()]
        assert inbox[-1]["synthetic"] is True
        ops = [json.loads(ln) for ln in
               (run_dir / "logs" / "operator_actions.jsonl").read_text().splitlines()]
        assert ops[-1]["detail"]["synthetic"] is True

        status, _ = _post(fs.PORT, "/steer", {"lane": "", "text": "real steer"})
        assert status == 200
        inbox = [json.loads(ln) for ln in
                 (run_dir / "logs" / "steer.inbox.jsonl").read_text().splitlines()]
        assert "synthetic" not in inbox[-1], "absence is the unmarked default — no synthetic:false noise"
    finally:
        srv.shutdown()

    # PAGE contract (0.24.40, operator decision): the marker is WIRE-ONLY — the composer carries
    # no test toggle (a control real operators never tick is clutter; programmatic test steers
    # pass synthetic:true in the POST body, which the server-side assertions above pin)
    assert "id=tst" not in _feed_server().PAGE
    assert "synthetic" not in _feed_server().PAGE


def test_prerun_steers_still_flow(tmp_path):
    fs, srv, run_dir = _live_server(tmp_path, "")  # empty log: nothing has run yet
    try:
        status, j = _post(fs.PORT, "/steer", {"lane": "extract", "text": "PRD only, no figma"})
        assert status == 200 and j.get("id") and j["routing"] == "prerun"
    finally:
        srv.shutdown()


def test_retract_tombstones_only_undelivered(tmp_path):
    """Dismiss must not lie: retract writes the ledger tombstone (future pumps' consumed_before
    excludes the id), and delivery wins any race — a settled id returns retracted:false."""
    import json as _json

    fs, srv, run_dir = _live_server(tmp_path, "[10:00:00] [review.architect] agent started\n")
    try:
        _, j = _post(fs.PORT, "/steer", {"lane": "review", "text": "hold on"})
        rid = j["id"]
        status, j = _post(fs.PORT, "/retract", {"id": rid})
        assert status == 200 and j["retracted"] is True
        ledger = (run_dir / "logs" / "steer.delivered.jsonl").read_text()
        entry = _json.loads(ledger.splitlines()[-1])
        assert entry["id"] == rid and entry["lane"] == "retracted"

        status, j = _post(fs.PORT, "/retract", {"id": rid})
        assert j["retracted"] is False, "already settled — delivery/retraction is once"
    finally:
        srv.shutdown()


def test_chip_states_and_commit_only_lane_contract():
    assert "steering expired \\(lane '(.*)'\\) — never delivered this invocation" in PAGE
    assert "chipExpired(mm[1])" in PAGE and ".chip.expired{" in PAGE
    # always-accept stays: no blocking decision anywhere
    assert "unrouted" not in PAGE and "queue_anyway" not in PAGE
    # 0.24.31: the lane is a select-only combobox (typing FILTERS, commits come from the known
    # set — entry-time constraint per the research; typos are structurally impossible), so the
    # did-you-mean fallback is dead code and a list-picked future stage gets honest queued copy
    assert "knownLanes().has(laneV)" in PAGE and "aria-invalid" in PAGE
    assert "nearestLane" not in PAGE and "did you mean" not in PAGE
    assert "delivers when '" in PAGE and "expires at run end" in PAGE
