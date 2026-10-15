"""claude_engine: run one stage as a headless Claude Code invocation. Ported from evals_repo/evals/agent.py.

Memory-safe by construction (proven in the eval harness):
  - stream stdout to a FILE, not RAM (a full stream-json trace over a long run is large);
  - run the child in its own process group (start_new_session) and kill the WHOLE group on timeout, so
    nested MCP server subprocesses can't leak.

Isolation: every stage gets `--mcp-config <materialized subset>` plus `--strict-mcp-config` by default —
an empty subset materializes {"mcpServers": {}}, guaranteeing a no-MCP stage. Stages with
inherit_user_mcp=True (INGEST) omit the strict flag so already-authed user-scope OAuth servers are
reachable; the read-only --allowedTools grant is still the only thing callable. The per-stage skill is staged into
<run_dir>/.claude/skills/<stage>/ and the agent runs with cwd=run_dir, so artifacts land in the run dir
under --permission-mode acceptEdits.

Parallelism: `run_claude` is the single-invocation primitive. claude_engine runs one; a stage that wants
fan-out (GROUND shards, REVIEW personas) supplies its own Stage.engine that calls run_claude from several
threads. Concurrency is guaranteed by the harness (Popen), NEVER requested via prompt instructions —
measured 2026-07-28: the model neither batches tool calls (batch size stayed 1.0 across 207 calls under an
explicit skill instruction) nor spawns Agent subagents concurrently (5 spawns arrived in 5 separate turns).
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

from . import figma_snapshot, steer, think_tap
from .constants import Decision
from .contracts import EngineFailure, RunContext, Stage, StageResult, guidance_path, rotated_stream_path, stream_path
from .hooks.guard_writes import _WRITE_TOOLS, DIFF_ONLY_ENV
from .mcp_tools import allowed_tools
from .streams import StreamResult, parse_stream, stream_event_label

_STAGES_DIR = Path(__file__).parent / "stages"
_GUARD = Path(__file__).parent / "hooks" / "guard_writes.py"
_APPROVE_RO = Path(__file__).parent / "hooks" / "approve_readonly.py"
_REPO_HISTORY = Path(__file__).parent / "hooks" / "repo_history.py"
_STAGE_TOOLS = Path(__file__).parent / "hooks" / "stage_tools.py"
_BYTE_BUDGET = Path(__file__).parent / "hooks" / "byte_budget.py"
_CANARY_PROBED = False  # hooks_canary runs at most once per process (it caches per CLI version on disk)


# Read-only Bash allow rules for the headless children. Why they exist: CLI 2.1.224 decomposes compound
# commands and headless-auto-denies any part that matches no rule, and a known analyzer bug (#6660,
# closed "not planned") parses `|` inside QUOTED REGEX as a shell pipe — measured cost: 13 denial
# round-trips in one run, every one a pure text-reading command. Why this is safe: allow rules only skip
# the ASK; the PreToolUse write-guard still runs on every Bash call and independently denies redirects
# outside the run dir, mutator verbs, and egress. Deliberately ABSENT (the CLI's strictness is correct
# for these — allow rules are string-PREFIX matches and neither the CLI nor the guard sees spawned
# children, so these stay unallowlistable in ANY flag form): awk (system()/print> are arbitrary
# exec+write), sed (-i, w-command, GNU e-flag exec), find (-delete/-exec), interpreters and script
# execution, xargs with flags. Disk-parsing needs that used to tempt agents toward awk/sed go through
# the stage_tools broker below instead. Residual risk accepted on sort: `sort -o`/--compress-program
# are write/exec vectors a prefix rule admits — guard_writes denies those flag forms explicitly.
_READONLY_BASH_ALLOW = [
    "Bash(grep:*)", "Bash(rg:*)", "Bash(cat:*)", "Bash(head:*)", "Bash(tail:*)", "Bash(wc:*)",
    "Bash(ls:*)", "Bash(echo:*)", "Bash(sort:*)", "Bash(uniq:*)", "Bash(cut:*)", "Bash(tr:*)",
    "Bash(diff:*)", "Bash(stat:*)", "Bash(file:*)", "Bash(basename:*)", "Bash(dirname:*)",
    "Bash(cd:*)", "Bash(pwd)", "Bash(which:*)",
    # jq/shasum: inert (no exec/write escape routes — jq writes only to stdout, shasum only reads)
    "Bash(jq:*)", "Bash(shasum:*)",
    # git entries are subcommand-scoped (never `git:*` — that admits push and sidesteps the guard's
    # mutator regex). Residual risk: a pager could exec (`PAGER=x git log` / `git -c core.pager=`),
    # but neither form matches these prefixes, and headless children have no TTY so git never spawns
    # a pager on its own.
    "Bash(git log:*)", "Bash(git show:*)", "Bash(git diff:*)", "Bash(git grep:*)",
    "Bash(git rev-parse:*)", "Bash(git status)", "Bash(git blame:*)", "Bash(git branch)",
    # ACI wrapper: the ONE safe path to repo git history (raw forms are unallowlistable — cd&&git is
    # CLI-blocked, git -C:* would admit push and sidestep the guard's mutator regex). The wrapper
    # validates repo + path and can only run `git log`.
    f"Bash({sys.executable} {_REPO_HISTORY}:*)",
    # ACI broker for disk-parsing (slice/hash/save-image): the sanctioned replacement for the
    # awk/sed/redirection reach observed live (PROJ-3394-figma-v2). Destinations are confined to the
    # run dir inside the script itself — see its docstring for the threat model.
    f"Bash({sys.executable} {_STAGE_TOOLS}:*)",
]


def sandbox_affordances(repos_root: str | None = None) -> str:
    """Terse sandbox contract appended (centrally, in run_claude) to every child's kickoff prompt.
    Agents used to discover the permission boundary by crashing into it — the denial census over 3 real
    runs (21 denials) shows the same classes relearned per session: awk/redirection in INGEST, `find /`
    repo discovery in GROUND (5 events, 2 degraded review verdicts), ad-hoc python in REVIEW personas.
    The allowed-command list is DERIVED from _READONLY_BASH_ALLOW so prompt and enforcement cannot
    drift. Kept terse deliberately: the enforcement layers (allowlist + hooks) are what bind behavior;
    this block just spares the discovery probes, and the instructive deny reasons catch what it doesn't."""
    simple = ", ".join(m.group(1) for e in _READONLY_BASH_ALLOW if (m := re.match(r"Bash\(([a-z]+)[:)]", e)))
    gits = ", ".join(m.group(1) for e in _READONLY_BASH_ALLOW if (m := re.match(r"Bash\(git ([a-z-]+)", e)))
    block = (
        "\n\n## Bash sandbox (headless: denials are FINAL — no human can approve; a denied form never "
        "works on retry, rephrase with the tools below)\n"
        f"- Allowed commands: {simple}; git (read-only): {gits}.\n"
        "- Chains (pipes/&&/;) auto-approve only when EVERY part is verifiably read-only; one unlisted "
        "part denies the whole call.\n"
        "- `>` redirection: only from an allowlisted command, only to a path inside this working directory.\n"
        f"- Disk parsing (line/regex slicing, sha256, base64 decode — the awk/sed replacement): "
        f"`{sys.executable} {_STAGE_TOOLS} slice <file> <N-M|regex> [--out <rel>]` | `hash <file>` | "
        f"`save-image <b64-file> <rel>` — exact form allowlisted; `... help` explains. jq for JSON.\n"
        "- Never approved, do not attempt: awk/sed writes, find -delete/-exec or `find /`, ad-hoc "
        "interpreters (python/node/perl, -c or scripts), xargs, curl/wget/network.\n"
        "- All other writes: the Write/Edit tools.\n"
    )
    if repos_root:
        block += (f"- Local repo checkouts live under {repos_root} (read-only): enumerate files with "
                  f"`rg --files {repos_root}/<repo>` — never `find /`.\n")
    return block


def parallel_dispatch_note() -> str:
    """Appended (centrally, in run_claude, beside sandbox_affordances) to every fresh child's kickoff
    prompt. Measured 2026-09-03 across a full run (srp-bottomsheet-chat-history): EVERY agent — stages,
    GROUND shards, REVIEW personas — emitted exactly one tool call per assistant turn (117/117
    single-call turns in the generate compose), so orientation reads and independent verification probes
    each paid a full model round-trip (~12.7s/turn there). The wording is Anthropic's documented
    parallel-tool-use nudge (platform.claude.com, Claude-4 best practices), plus the independence
    guardrail its docs recommend so dependent chains stay sequential. Manifest `multi_call_turn_pct`
    (streams.parse_stream) is this note's compliance gauge."""
    return (
        "\n\n## Parallel tool dispatch\n"
        "For maximum efficiency, whenever you perform multiple independent operations, invoke all "
        "relevant tools simultaneously — several tool calls in ONE message — rather than sequentially. "
        "Reading N already-known files is ONE message with N Read calls (orientation inputs, a batch of "
        "citation checks); independent greps batch the same way. Only batch calls that are independent "
        "of each other: when the next call's input depends on the previous call's result, or edits "
        "touch the SAME file, stay sequential.\n"
    )


# The hook set a child runs under. A dict per PreToolUse/PostToolUse entry, keyed by the hook NAME the
# `hooks_off` flag uses, so turning one off is an argument rather than an edit here.
def _hook_settings(disabled: set[str]) -> dict:
    """The `hooks` section of a child's settings, minus any hook named in `disabled` (flag `hooks_off`)."""
    entries = {
        # derived from the guard's own tool list so the two can never drift: a matcher
        # missing a write tool is a hole in containment the guard never gets to veto
        "guard_writes": ("PreToolUse", {
            "matcher": "|".join((*_WRITE_TOOLS, "Bash")),
            "hooks": [{"type": "command", "command": f"{sys.executable} {_GUARD}"}],
        }),
        # auto-approve verifiably read-only Bash compounds (pipes/&&/for) that the
        # prefix allowlist cannot express (v5: 14 denials, all text-reading).
        # Composes safely with the guard: most-restrictive-wins, so its deny beats
        # this allow on every call both fire on.
        "approve_readonly": ("PreToolUse", {
            "matcher": "Bash",
            "hooks": [{"type": "command", "command": f"{sys.executable} {_APPROVE_RO}"}],
        }),
        # continuous byte-budget accounting: the 50KB ceiling used to be visible
        # only at the terminal gate (v1 binary-searched it for 4.3 min, v2's
        # revision died at it). Warns once per file per threshold; never blocks.
        # continueOnBlock (CLI 2.1.139+): a PostToolUse exit-2 defaults to a
        # silent drop — the reason never reaches the model, and the "block"
        # even aborts sibling hooks in the same entry. Reproduced 2026-09-03
        # (v2 run: a full 25-min compose with ZERO hook feedback delivered;
        # scratch repro: feedback text absent without the key, delivered and
        # both hooks' state files written with it). The key restores the
        # documented feed-back-and-continue semantics both hooks are built on.
    }
    # The two PostToolUse feedback hooks share ONE entry, as shipped: `matcher` is identical and the
    # settings shape is what the CLI has been verified against. continueOnBlock neutralises the
    # same-entry sibling-abort hazard that shape otherwise carries.
    post = {"byte_budget": _BYTE_BUDGET}
    out: dict[str, list] = {}
    for name, (event, entry) in entries.items():
        if name in disabled:
            continue
        out.setdefault(event, []).append(entry)
    post_hooks = [{"type": "command", "command": f"{sys.executable} {script}", "continueOnBlock": True}
                  for name, script in post.items() if name not in disabled]
    if post_hooks:
        out["PostToolUse"] = [{"matcher": "|".join(_WRITE_TOOLS), "hooks": post_hooks}]
    return out


def _write_guard_settings(run_dir: Path, dest: Path, *, hooks_enabled: bool = True,
                          disabled_hooks: tuple[str, ...] | list[str] = ()) -> Path:
    """Settings JSON for one headless child: the PreToolUse write-guard (containment) + the curated
    read-only Bash allowlist (throughput). The guard is the enforcement layer; the allowlist only stops
    text-reading commands from dying in headless auto-deny.

    `hooks_enabled` / `disabled_hooks` materialize only the hooks a run asked for (flags `hooks` and
    `hooks_off`, Containment.hooks_enabled/disabled_hooks). Defaults materialize ALL of them, so the
    shipped behaviour is unchanged. Turning them off is for BENCHMARKS — measuring what containment
    costs — and it removes containment; the run dir is still the cwd but nothing stops a write outside
    it, so never do this on a run that ingests untrusted text."""
    if not _GUARD.exists():  # fail loudly: a missing guard must not silently downgrade to "no containment"
        raise EngineFailure(f"write-guard hook missing at {_GUARD} — refusing to run with repo write access")
    off = set(disabled_hooks or ())
    allow = list(_READONLY_BASH_ALLOW)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(
        json.dumps(
            {
                # thinking text is suppressed on newer models (display=omitted). This key requests the
                # SUMMARIZED display — VERIFIED INERT on CLI 2.1.224 (probe: 31 deltas, 0 chars, with
                # and without CLAUDE_CODE_DISABLE_ADAPTIVE_THINKING) but harmless, kept for future CLI
                # builds. Until it activates, the feed's honest ceiling is 💬 narration + 💭 ~N-token
                # depth heartbeats.
                "showThinkingSummaries": True,
                "permissions": {"allow": allow},
                # crossSessionInbound is deliberately NOT pre-accepted (0.24.2 — was "accept" in
                # 0.24.0): that converted the CLI's fail-closed hold into fail-open delivery, letting
                # ANY local Claude session steer a child running acceptEdits over ~90 checkouts — a
                # different socket than the feed server, untouched by its CSRF hardening. The stdin
                # stream-json channel is the steering primary; re-add "accept" only if stdin proves
                # insufficient AND the trust boundary is documented for operators.
                "hooks": _hook_settings(off) if hooks_enabled else {},
            },
            indent=2,
        )
    )
    return dest.resolve()


def _kill_group(proc: subprocess.Popen) -> None:
    """SIGTERM the child's whole process group (nested MCP servers included), escalating to SIGKILL."""
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        proc.wait(timeout=15)
    except Exception:
        with contextlib.suppress(Exception):
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)


def _stderr_tail(err_path: Path, n: int = 2000) -> str:
    with contextlib.suppress(OSError):
        return err_path.read_text(encoding="utf-8", errors="replace")[-n:].strip() or "<empty>"
    return "<unreadable>"


_KICKOFF_TIMEOUT_S = 300  # generous: the child may not drain stdin until its MCP init completes


def _send_kickoff(proc: subprocess.Popen, prompt: str, label: str, err_path: Path) -> None:
    """Write the kickoff turn from a bounded thread (0.24.1). A plain write blocked the caller
    forever when the prompt exceeded the pipe buffer against a child stalled in init (no timeout
    exists yet at that point, and in fan-out it held a ThreadPool slot with Ctrl-C uncatchable);
    a live-child write failure degraded to a promptless agent silently burning RUN_TIMEOUT."""
    failed: list[BaseException] = []

    def _write() -> None:
        try:
            proc.stdin.write(steer.user_turn(prompt))
            proc.stdin.flush()
        except BaseException as e:  # noqa: BLE001 — reported by the joiner below
            failed.append(e)

    t = threading.Thread(target=_write, daemon=True, name=f"kickoff-{label}")
    t.start()
    t.join(_KICKOFF_TIMEOUT_S)
    if t.is_alive():
        _kill_group(proc)
        raise EngineFailure(
            f"'{label}' never accepted its kickoff prompt within {_KICKOFF_TIMEOUT_S}s — killed "
            f"(child stalled before reading stdin) — stderr tail: {_stderr_tail(err_path)}")
    if failed:
        # capture the child's state BEFORE the kill: post-kill poll() reports the SIGTERM we just
        # sent, not why the write failed, and sends the operator chasing the wrong cause
        rc = proc.poll()
        _kill_group(proc)
        state = f"exit {rc}" if rc is not None else "child was still alive"
        raise EngineFailure(
            f"'{label}' kickoff prompt write failed: {failed[0]!r} ({state}) — "
            f"stderr tail: {_stderr_tail(err_path)}")


def _stage_skill(stage_name: str, run_dir: Path, variant: str | None = None) -> None:
    """Copy the stage's SKILL.md (or SKILL.<variant>.md) into the run workspace so the headless agent
    auto-discovers it. The destination name is always SKILL.md — the agent sees one skill either way."""
    src = _STAGES_DIR / stage_name / (f"SKILL.{variant}.md" if variant else "SKILL.md")
    if not src.exists():
        raise EngineFailure(f"stage '{stage_name}' has no {src.name} at {src}")
    dst = run_dir / ".claude" / "skills" / stage_name
    dst.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(src, dst / "SKILL.md")


def _tool_target(name: str, inp: dict) -> str:
    """One human-readable token for what a tool call touched — the live-feed payload."""
    for key in ("file_path", "pattern", "path", "url"):
        if inp.get(key):
            val = " ".join(str(inp[key]).split())
            if "/" in val:  # paths: last 3 segments read naturally; a mid-path chop reads as garbage
                val = "/".join(val.split("/")[-3:])
            return val[:80]
    if isinstance(inp.get("command"), str):
        return " ".join(inp["command"].split())[:80]  # collapse newlines — one feed line per call
    return ""


def log_activity(run_dir: Path, label: str, message: str) -> None:
    """One lifecycle line in the unified live feed — industry stream protocols (AG-UI, Vercel data
    stream, LangChain agent-protocol) all carry explicit run/step boundaries so consumers never have to
    INFER when something started or finished; ours does too."""
    with contextlib.suppress(Exception):
        (run_dir / "logs").mkdir(parents=True, exist_ok=True)
        with open(run_dir / "logs" / "activity.log", "a", encoding="utf-8") as f:
            f.write(f"[{time.strftime('%H:%M:%S')}] [{label}] {message}\n")


# progress-line throttles: a delta line lands at most every N bytes of streamed payload — enough for a
# human "it's typing, 24KB in" heartbeat without turning the feed into a token firehose
_TOOL_DELTA_STEP = 16384
_TEXT_DELTA_STEP = 8192
# reasoning-trace throttle: one 💭/💬 snippet per this many streamed chars of thinking/narration
# (measured ~34KB of thinking per run -> ~85 trace lines across all agents at 400)
_MIND_STEP = 400
_FILE_PATH_SNIFF = re.compile(r'"file_path"\s*:\s*"([^"]+)"')


def drain_activity(log_path: Path, pos: int, run_dir: Path, label: str, state: dict | None = None) -> int:
    """Append human-readable lines for NEW events in `log_path` (from byte `pos`) to logs/activity.log —
    the run's unified live feed. Every invocation (parent, shard, persona) writes to the same file, so
    `tail -f logs/activity.log` — or the plugin orchestrator relaying its tail into the chat — shows what
    all the agents are doing in real time.

    Two event classes feed it: completed tool calls (one line each), and — when the child runs with
    --include-partial-messages — streaming deltas, rendered as throttled progress heartbeats ("… Write
    streaming 24KB into out/rfc.md"). The deltas are what kill the 3-6 minute silences during big
    document writes, the single worst perceived-latency window. `state` (caller-held, per invocation)
    carries delta accumulation across drain calls. POSIX O_APPEND keeps concurrent shard appends
    line-atomic at these sizes. Returns the new byte offset."""
    if not log_path.exists():
        return pos
    if state is None:
        state = {}
    with open(log_path, "rb") as f:
        f.seek(pos)
        chunk = f.read()
    nl = chunk.rfind(b"\n")
    if nl < 0:
        return pos
    out_lines = []

    def emit(msg: str, ts: str = "") -> None:
        out_lines.append(f"[{ts or time.strftime('%H:%M:%S')}] [{label}] {msg}".rstrip())

    for line in chunk[: nl + 1].decode("utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(ev, dict):
            continue
        if ev.get("type") == "assistant":
            content = (ev.get("message") if isinstance(ev.get("message"), dict) else {}).get("content", [])
            for b in content if isinstance(content, list) else []:
                if isinstance(b, dict) and b.get("type") == "tool_use":
                    # wall clock, NOT the event's timestamp: stream timestamps are UTC while lifecycle
                    # lines use local time — one feed must not show the same moment 6 hours apart
                    target = _tool_target(b.get("name"), b.get("input") or {})
                    if b.get("id"):  # id->call map so a later denial names WHAT was denied
                        state.setdefault("calls", {})[b["id"]] = f"{b.get('name') or 'tool'} {target}".strip()
                    emit(f"{b.get('name') or 'tool'} {target}")
        elif ev.get("type") == "system" and ev.get("subtype") == "permission_denied":
            # first-class denial event (CLI >= 2.1.224; its `message` is a plain STRING — the shape that
            # crashed the runner mid-stage before the isinstance guards). Carries tool + reason directly,
            # so render it instead of waiting for the tool_result heuristic below.
            call = state.get("calls", {}).get(ev.get("tool_use_id"), ev.get("tool_name") or "tool")
            reason = " ".join(str(ev.get("message") or "").split())[:100]
            emit(f"⛔ denied [permission]: {str(call)[:80]} — {reason}")
        elif ev.get("type") == "user":
            # denials are instant (headless children auto-deny anything that would prompt — nothing ever
            # WAITS), but each one silently costs a reformulation round-trip; measured 2-9 per run and the
            # feed showed only the mysterious retries, never the reason. Surface them.
            content = (ev.get("message") if isinstance(ev.get("message"), dict) else {}).get("content", [])
            for b in content if isinstance(content, list) else []:
                if isinstance(b, dict) and b.get("type") == "tool_result" and b.get("is_error"):
                    txt = str(b.get("content"))
                    if "denied" in txt.lower() or "permission" in txt.lower():
                        call = state.get("calls", {}).get(b.get("tool_use_id"), "tool call")
                        why = "write-guard (containment)" if "guard_writes" in txt else "permission auto-deny"
                        emit(f"⛔ denied [{why}]: {call[:90]}")
                elif isinstance(b, dict) and b.get("type") == "text":
                    raw = steer.unwrap(b.get("text") or "")
                    if raw is not None:
                        # the CLI replayed the injected steering turn (--replay-user-messages): the
                        # agent's conversation now CONTAINS the correction — the 🧭 lifecycle's
                        # "read" state, distinct from "delivered" (bytes queued on stdin). The feed
                        # page's steer chips turn green and pop on this line.
                        emit(f"🧭 operator steering read by the agent ({len(raw)} chars)")
        elif ev.get("type") == "stream_event":
            sub = ev.get("event") or {}
            st = sub.get("type")
            if st == "content_block_start":
                block = sub.get("content_block") or {}
                if block.get("type") == "tool_use":
                    state.update(tool=block.get("name") or "tool", buf="", nbytes=0, emitted=0)
                elif block.get("type") in ("thinking", "text"):
                    # reasoning channel: the operator asked to SEE the agents think, not just act.
                    # The streams always carried it (572 thinking_delta events in one measured run) —
                    # the drain just never rendered it. Industry pattern: reasoning is its own event
                    # class, surfaced as a throttled trace, never a raw firehose.
                    state.update(mind=block["type"], mind_buf="", mind_emitted=0)
            elif st == "content_block_delta":
                d = sub.get("delta") or {}
                if d.get("type") == "input_json_delta" and state.get("tool"):
                    part = d.get("partial_json") or ""
                    if len(state.get("buf", "")) < 300:  # keep just enough prefix to sniff the file path
                        state["buf"] = (state.get("buf", "") + part)[:300]
                    state["nbytes"] = state.get("nbytes", 0) + len(part)
                    if state["nbytes"] - state.get("emitted", 0) >= _TOOL_DELTA_STEP:
                        state["emitted"] = state["nbytes"]
                        m = _FILE_PATH_SNIFF.search(state.get("buf", ""))
                        into = f" into {m.group(1).rsplit('/', 1)[-1]}" if m else ""
                        emit(f"… {state['tool']} streaming ({state['nbytes'] // 1024}KB{into})")
                elif d.get("type") in ("thinking_delta", "text_delta") and state.get("mind"):
                    state["mind_buf"] = state.get("mind_buf", "") + (d.get("thinking") or d.get("text") or "")
                    # newer models ship thinking with display=omitted: empty text, but estimated_tokens
                    # still arrives — accumulate it so the feed can at least show DEPTH honestly
                    state["mind_est"] = state.get("mind_est", 0) + (d.get("estimated_tokens") or 0)
                    buf = state["mind_buf"]
                    # emit one snippet per _MIND_STEP chars, cut at a sentence/newline boundary so lines
                    # read as thoughts, not mid-word shrapnel
                    if len(buf) - state.get("mind_emitted", 0) >= _MIND_STEP:
                        chunk = buf[state.get("mind_emitted", 0):]
                        cut = max(chunk.rfind(". "), chunk.rfind("\n"))
                        if cut < 40:  # no boundary yet — take the whole chunk
                            cut = len(chunk) - 1
                        # _MIND_STEP is cadence only — emit the whole sentence-bounded chunk, NOT capped
                        snippet = " ".join(chunk[: cut + 1].split())
                        state["mind_emitted"] = state.get("mind_emitted", 0) + cut + 1
                        # tap active + thinking block: the sidecar already emits this exact reasoning
                        # from the wire — a second stream-side emission duplicated ~45% of 💭 lines
                        # (v6, 2026-08-10: display=summarized now flows through to the CLI stream too)
                        if snippet and not (state.get("tap") and state["mind"] == "thinking"):
                            emit(f"{'💭' if state['mind'] == 'thinking' else '💬'} {snippet}")
            elif st == "content_block_stop":
                state.pop("tool", None)
                # flush the tail of a reasoning block if it never crossed the throttle — every thought
                # block leaves at least its gist in the feed
                buf = state.pop("mind_buf", "")
                kind = state.pop("mind", None)
                est = state.pop("mind_est", 0)
                rest = " ".join(buf[state.pop("mind_emitted", 0):].split())
                if kind and len(rest) >= 40 and not (state.get("tap") and kind == "thinking"):
                    emit(f"{'💭' if kind == 'thinking' else '💬'} {rest}")
                elif kind == "thinking" and not buf.strip() and est >= 100 and not state.get("tap"):
                    # the CLI blanks thinking text in headless streams (display=omitted end to end:
                    # empty string + signature + estimated_tokens), so this is a DEPTH heartbeat, not
                    # the reasoning. Say why the text is absent — dropping that suffix left the default
                    # (tap-off) feed with a bare token count and no explanation. With AGENT.THINKING_TAP
                    # on, the sidecar emits the real 💭 <text> lines and these beats are the exception.
                    emit(f"💭 thought for ~{est} tokens (text withheld by the model's display policy)")
    if out_lines:
        with open(run_dir / "logs" / "activity.log", "a", encoding="utf-8") as f:
            f.write("\n".join(out_lines) + "\n")
    return pos + nl + 1


def _wait_with_progress(proc: subprocess.Popen, log_path: Path, timeout: int, ctx: RunContext,
                        activity_label: str | None = None, tap_active: bool = False,
                        quiet: bool = False, obs_parent=None, pump=None) -> None:
    """Block until the agent exits, tailing the stream log for a live heartbeat on stderr AND emitting
    each completed tool call as a Langfuse event at its real time (per Saurav: capture all agent turns).
    An event is emitted when its tool_result arrives, so it carries input AND output; the caller's open
    generation observation is the OTEL-current span, so events nest under it — unless `obs_parent` is
    given (worker threads: ambient context is empty there, tools parent through the handle instead).
    `quiet` drops the stderr ticker (concurrent personas would interleave \\r lines into noise)."""
    from . import obs

    start = time.monotonic()
    deadline = start + timeout
    pos = 0
    act_pos = 0
    act_state: dict = {"tap": tap_active}
    events = 0
    tool_events = 0
    last_label = ""
    # tool_use_id -> live obs handle: the tool observation OPENS when tool_use appears and CLOSES when its
    # result arrives, so its span duration is the tool's real execution time (was a single point-in-time event)
    pending: dict[str, object] = {}
    # latest thinking block (text + owning message id): per Anthropic's docs a message's thinking block
    # is "the reasoning that led to the tool requests" beside it, so each tool observation carries its
    # WHY — same_message when the tool_use shares the message, carried when the reasoning is the most
    # recent earlier block (measured PROJ-3394-v7: 75%/25%, 571/571 tools covered). Text exists only
    # when display=summarized reaches the stream (THINKING_TAP on); otherwise reasons are absent.
    think: dict = {}

    def drain() -> None:
        nonlocal pos, events, tool_events, last_label
        if not log_path.exists():
            return
        with open(log_path, "rb") as f:  # binary: pos is an exact byte offset, no text-cookie ambiguity
            f.seek(pos)
            chunk = f.read()
        # consume only COMPLETE lines — a line split mid-write would json-fail and be skipped forever
        nl = chunk.rfind(b"\n")
        if nl < 0:
            return
        pos += nl + 1
        for line in chunk[: nl + 1].decode("utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line:
                continue
            events += 1
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(ev, dict):
                continue
            content = (ev.get("message") if isinstance(ev.get("message"), dict) else {}).get("content", [])
            blocks = [b for b in content if isinstance(b, dict)] if isinstance(content, list) else []
            if ev.get("type") == "assistant":
                mid = (ev.get("message") if isinstance(ev.get("message"), dict) else {}).get("id")
                for b in blocks:
                    if b.get("type") == "thinking" and (b.get("thinking") or "").strip():
                        think["text"], think["msg"] = b["thinking"], mid
                    elif b.get("type") == "tool_use":
                        tool_events += 1
                        if b.get("id"):
                            # the event's own ms timestamp rides along so the span's DURATION is
                            # stream-true instead of quantised to this drain loop's poll
                            pending[b["id"]] = obs.tool_started(
                                ctx, b.get("name") or "tool", b.get("input", {}), parent=obs_parent,
                                event_ts=ev.get("timestamp"), reason=think.get("text"),
                                reason_scope="same_message" if think.get("msg") == mid else "carried")
            elif ev.get("type") == "user":
                for b in blocks:
                    if b.get("type") == "tool_result" and b.get("tool_use_id") in pending:
                        obs.tool_finished(pending.pop(b["tool_use_id"]), output=b.get("content"),
                                          event_ts=ev.get("timestamp"))
            label = stream_event_label(ev)
            if label:
                last_label = label

    ops_size = -1  # live operator-action flush watermark; -1 forces one flush on the first tick
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise subprocess.TimeoutExpired(proc.args, timeout)
        exited = proc.poll() is not None
        drain()
        if not quiet and obs_parent is None:
            # LIVE operator-action flush (0.24.10): a steer posted mid-stage reaches Langfuse within
            # a tick instead of at the stage boundary — GENERATE alone is 20-30 min, and the 0.24.5
            # boundary flush left the operator's own words invisible for that whole stretch. MAIN
            # THREAD only (this branch is the serial-stage path): fan-out lanes tick in ThreadPool
            # workers, where an ambient-context flush would mint orphan traces (the 0.22.66 class) —
            # those keep the runner's stage-boundary flush.
            # ...and BEFORE pump.tick() (0.24.12): the tick emits decision:steering_delivered, so
            # flushing after it created the operator:steer observation AFTER its own consequence —
            # an inverted causal timeline in Langfuse (field screenshot, 2026-08-19)
            with contextlib.suppress(OSError):
                sz = (ctx.run_dir / "logs" / "operator_actions.jsonl").stat().st_size
                if sz != ops_size:
                    ops_size = sz
                    with contextlib.suppress(Exception):
                        obs.operator_actions_flush(ctx)
        if pump is not None:
            pump.tick()  # deliver operator steering; close stdin once the result event lands
        if activity_label:
            act_pos = drain_activity(log_path, act_pos, ctx.run_dir, activity_label, act_state)
        elapsed = int(time.monotonic() - start)
        hint = f", last: {last_label}" if last_label else ""
        if not quiet:
            print(
                f"  agent working: {events} events, {tool_events} tool calls, {elapsed}s elapsed{hint}",
                end="\r",
                file=sys.stderr,
                flush=True,
            )
        if exited:  # poll BEFORE drain, so the post-exit tail was fully consumed
            for handle in pending.values():  # tool calls the run ended before answering
                obs.tool_finished(handle, output=None)
            if not quiet:
                print(file=sys.stderr)
            return
        # 0.5s: tool DURATIONS are stream-true since 0.22.55 (each event's own ms timestamp), so the
        # poll only affects span wall-clock position (≤ one poll late) and narration latency — the
        # 0.1s poll existed solely to shrink duration quantisation and just multiplied ticker spam
        time.sleep(min(0.5, max(0.05, remaining)))


# Failure classes a constrained patch pass can fix: each names an exact defect at an exact location and
# the fix is local (qualify/correct/remove one citation, add one REQ tag, align one table row). Measured
# 2026-08-04 (44-REQ PRD): the free-form revision converged 34->18->2 over 3 full attempts / $22.77,
# each attempt fixing its class but re-editing the same files 13-16x and leaking a new small class.
# Anything not matching — review BLOCKERs, missing sections, design defects — keeps the full revision.
_MECHANICAL_FAILURE_RE = re.compile(
    r"bare citation|unqualified citation|citation line out of range|cited (?:file|input file) does not exist"
    r"|input citation line out of range|appears neither in its capability subsection"
    r"|citation NOT witnessed|cross-doc|absence claim looks FALSE"
)


def latest_gate_failures(run_dir: Path, stage_name: str) -> list[str]:
    """The bullet lines of the LAST 'GATE FAILURES' block in guidance/<stage>.md (guidance appends one
    block per attempt; earlier blocks describe already-revised output)."""
    g = guidance_path(run_dir, stage_name)
    if not g.exists():
        return []
    lines = g.read_text(encoding="utf-8", errors="replace").splitlines()
    last = None
    for i, ln in enumerate(lines):
        if ln.startswith("- GATE FAILURES"):
            last = i
    if last is None:
        return []
    out = []
    for ln in lines[last + 1:]:
        if not ln.startswith("  - "):
            break
        out.append(ln[4:])
    return out


def failures_all_mechanical(fails: list[str]) -> bool:
    """EVERY failure is in the mechanical class (citations/coverage tags — one-Edit fixes). One
    non-mechanical failure disqualifies the whole set: a design fix's ripple effects need the
    unconstrained revision."""
    return bool(fails) and all(_MECHANICAL_FAILURE_RE.search(f) for f in fails)


def patchable_revision(stage: Stage, ctx: RunContext) -> bool:
    """True when this revision qualifies for patch mode: outputs on disk and EVERY failure in the
    latest gate block is mechanical."""
    if not (stage.revise_on_retry and all((ctx.run_dir / o).exists() for o in stage.outputs)):
        return False
    return failures_all_mechanical(latest_gate_failures(ctx.run_dir, stage.name))


def split_mechanical_failures(stage: Stage, ctx: RunContext) -> list[str]:
    """The mechanical SUBSET of a mixed failure set — non-empty only when the set is genuinely mixed
    (some mechanical, some not) and the outputs are on disk. One non-mechanical failure used to
    escalate the ENTIRE set to a free-form full-model revision (v8 ground, 2026-09-08: 5 lead-claim
    failures dragged 17 one-edit citation fixes into a 14.7-minute revision); mixed sets now patch
    the mechanical subset in a constrained cheap pre-pass first."""
    if not (stage.revise_on_retry and all((ctx.run_dir / o).exists() for o in stage.outputs)):
        return []
    fails = latest_gate_failures(ctx.run_dir, stage.name)
    mech = [f for f in fails if _MECHANICAL_FAILURE_RE.search(f)]
    return mech if mech and len(mech) < len(fails) else []


def drop_mechanical_guidance(run_dir: Path, stage_name: str, patched: int) -> None:
    """After the mechanical pre-pass, rewrite the guidance file so the free-form revision sees only
    the substantive failures — a fixed failure left in guidance is an invitation to re-fix it.
    Line-filter by the mechanical regex (a human guidance sentence matching these gate phrasings is
    theoretically filterable too; accepted — the phrasings are gate-generated jargon)."""
    g = guidance_path(run_dir, stage_name)
    if not g.exists():
        return
    kept = [ln for ln in g.read_text(encoding="utf-8", errors="replace").splitlines()
            if not _MECHANICAL_FAILURE_RE.search(ln)]
    kept.append(f"  - ({patched} mechanical failure(s) were already fixed by an automated patch "
                f"pass — do not look for or re-fix them)")
    g.write_text("\n".join(kept) + "\n")


def _guidance_text(stage: Stage, ctx: RunContext) -> str:
    """Guidance + revision-mode addendum for this stage's next prompt ('' when no guidance exists)."""
    guidance = guidance_path(ctx.run_dir, stage.name)
    if not guidance.exists():  # written by gate retries / the boundary checkpoint's rerun path
        return ""
    text = (
        "\n\nHUMAN GUIDANCE (authoritative — from a human who reviewed this stage's previous output; "
        "follow it over any conflicting default):\n" + guidance.read_text(encoding="utf-8", errors="replace")
    )
    if patchable_revision(stage, ctx):
        text += (
            "\n\nPATCH MODE: every failure in the LATEST gate-failures block above is mechanical. Fix "
            "each with the smallest possible targeted Edit: locate the exact defect it names, verify the "
            "correction against the actual file (read the specific cited lines — never guess a path or "
            "line number), apply ONE edit, move to the next. If a citation cannot be verified, delete or "
            "soften the claim rather than re-citing it. Do NOT restructure sections, re-read the corpus, "
            "rewrite prose, or touch anything no failure names. Expected effort: roughly one Edit per "
            "failure, then stop."
        )
    elif stage.revise_on_retry and all((ctx.run_dir / o).exists() for o in stage.outputs):
        text += (
            "\n\nREVISION MODE: your previous attempt's output files are on disk. Do NOT rewrite them "
            "from scratch — read them plus the guidance above, then make TARGETED edits (Edit tool) "
            "that fix every listed failure and their ripple effects (coverage table rows, cross-doc "
            "consistency). Leave unaffected content untouched. All hard rules still apply to the "
            "revised whole."
            "\n\nFIX THE CLASS, NOT THE INSTANCE: a failure names where the defect was CAUGHT, not "
            "every place it lives. Before you consider it fixed, sweep for the rest of its class: "
            "for a broken parameter/data thread, trace the WHOLE chain hop-by-hop from entry point to "
            "sink (grep each function for its callers and callees) and verify every seam forwards it — "
            "a thread fixed at one seam and severed at the next is the same blocker again next round "
            "(v10: order.py:4401 was fixed while booking.py:2214, one hop down, cost two more review "
            "cycles). And when a fix ADDS a mechanism (a constructor, an init, a config read), state "
            "WHERE it runs and check that placement against how the codebase already runs it — a "
            "correct call in the wrong place (per-request init on a hot path) is a new blocker, not "
            "a fix."
        )
    return text


def _memory_text(stage: Stage) -> str:
    """Durable per-stage lessons from previous runs ('' when none). Below human guidance in authority —
    appended after it so conflicting human direction wins by recency and by label."""
    from .memory import load_notes

    notes = load_notes(stage.name)
    if not notes:
        return ""
    return (
        "\n\nDURABLE LESSONS (evidence-backed notes promoted from previous runs' process signals; "
        "follow them unless human guidance above contradicts them):\n" + notes
    )


def assemble_prompt(stage: Stage, ctx: RunContext) -> str:
    """The stage's kickoff prompt: build_prompt + guidance/revision addenda + durable memory."""
    return stage.build_prompt(ctx) + _guidance_text(stage, ctx) + _memory_text(stage)


EFFORT_LEVELS = ("low", "medium", "high", "xhigh", "max")
_EFFORT_ENV = "CLAUDE_CODE_EFFORT_LEVEL"
_effort_noted: set[str] = set()


def stage_effort(cfg, stage_name: str, env: dict[str, str]) -> tuple[str | None, dict[str, str], list[str]]:
    """Per-stage reasoning effort (AGENT.STAGE_EFFORT, e.g. {"extract": "low"}) -> the `--effort` level for
    this stage's children, the child env, and operator notes. Layer 2 of the extract redesign (2026-09-03
    matrix: quote grounding + low effort = 1.5-2.5x faster extract at ~2 reference atoms of recall; the same
    low effort on the OLD extract skill dropped a quarter of the REQs, so the knob is per stage and the
    extract/low pairing without EXTRACT.QUOTE_GROUNDING gets a loud note).

    Precedence: a configured stage level wins for that stage — the CLI lets CLAUDE_CODE_EFFORT_LEVEL override
    `--effort`, so the env var is stripped from the child when a stage level is set. Stages with no
    configured level run exactly as before (the operator's env var, if any, passes through — that is how a
    whole-process A/B is run on purpose)."""
    notes: list[str] = []
    levels = cfg.get("AGENT.STAGE_EFFORT") or {}
    if not isinstance(levels, dict):
        raise EngineFailure(f"AGENT.STAGE_EFFORT must be an object of stage -> level, got {levels!r}")
    level = levels.get(stage_name)
    if level is None:
        return None, env, notes
    level = str(level).strip().lower()
    if level not in EFFORT_LEVELS:
        raise EngineFailure(f"AGENT.STAGE_EFFORT.{stage_name}={level!r} — allowed: {', '.join(EFFORT_LEVELS)}")
    out = dict(env)
    if _EFFORT_ENV in out:
        notes.append(f"{_EFFORT_ENV}={out[_EFFORT_ENV]!r} ignored for {stage_name}: AGENT.STAGE_EFFORT sets {level}")
        del out[_EFFORT_ENV]
    return level, out, notes


# Denied for EVERY stage child, unconditionally. Artifact publishes run content to a HOSTED claude.ai
# page — an egress surface no headless stage has any business reaching, and the pipeline's contract is
# disk-in/disk-out (downstream gates/review read out/*.md, never a web page). Observed live (v9
# generate, 2026-09-08): the composer called Artifact(action="publish") on its deliverables — "native
# and fast" is exactly the rational incentive that makes a prompt-only prohibition insufficient (the
# publish failed only on a missing favicon parameter). Fourth instance of the class; deny mechanically.
_BASELINE_DISALLOWED = "Artifact"


def deny_list(stage_disallowed: str | None, extra: str | None) -> str:
    """Baseline denies + stage denies + AGENT.EXTRA_DISALLOWED_TOOLS (operator-named, applied to EVERY
    stage). The knob exists because allowed_tools() pre-emptively allowlists mcp__claude_ai_* ALIASES
    (so the inherited Atlassian connector stays callable in non-strict stages) — which lets an agent
    pick a claude.ai connector over the pipeline's configured server (measured 2026-08-11: ingest
    called mcp__claude_ai_Figma__get_metadata instead of the local figma server and the design fetch
    failed). Deny wins over allow, so naming the connector servers here fences them off."""
    return ",".join(p for p in (_BASELINE_DISALLOWED, stage_disallowed, extra) if p)


# ---- Figma Dev Mode binding-race hardening (2026-08-14) -----------------------------------------
# The CLI freezes the deferred-tools list when the first prompt is built (~1s) while MCP servers
# connect in the background: a sleepy desktop Figma server (App Nap after idle) can mean ZERO
# mcp__figma__* tools for the whole run even though the server later reports connected. Measured on
# PROJ-3394: attempt 1 lost the race ($1.67 wasted, gate fail); attempt 2 minutes later bound all
# tools. Two layers, both pre-spend: a $0 probe/restart BEFORE the spawn, and an init-event
# tool-count check right AFTER it with one free respawn.


def _figma_url(cfg) -> str:
    return ((cfg.get("MCP.mcpServers") or {}).get("figma") or {}).get("url") or ""


def _figma_probe(url: str, timeout: float = 5.0) -> bool:
    """One MCP initialize round-trip — a protocol handshake, not a tool call, so quota-exempt."""
    import urllib.request

    body = json.dumps({
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": "2025-03-26", "capabilities": {},
                   "clientInfo": {"name": "rfc-agent-prewarm", "version": "1"}},
    }).encode()
    req = urllib.request.Request(url, data=body, headers={
        "Content-Type": "application/json", "Accept": "application/json, text/event-stream"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return 200 <= resp.status < 300
    except Exception:
        return False


def stage_mcp_servers(stage: Stage, ctx: RunContext) -> tuple[str, ...]:
    """The servers a child actually binds. A snapshot run (--figma <export dir>) drops `figma`: the
    designs are already on disk, so the desktop server would only add the prewarm/binding-race
    ceremony and hand the agent a quota'd tool it must not use."""
    if "figma" in stage.mcp_servers and figma_snapshot.snapshot_dir(ctx) is not None:
        return tuple(s for s in stage.mcp_servers if s != "figma")
    return stage.mcp_servers


def figma_prewarm(ctx: RunContext, *, restart: bool = False) -> None:
    """$0 pre-flight for figma-bound stages: probe the local Dev Mode server; on failure (or when a
    failed binding demands `restart`) osascript-restart the Figma app, deep-link the target file, and
    poll ~60s — all BEFORE model spend. Never a hard stop: an unreachable server degrades to the
    stage's own FETCH FAILED handling (and its gate)."""
    url = _figma_url(ctx.config)
    if "127.0.0.1" not in url and "localhost" not in url:
        return  # remote figma server (or none configured): nothing to warm
    if not restart and _figma_probe(url):
        return
    if sys.platform != "darwin":
        log_activity(ctx.run_dir, "figma",
                     f"dev-mode server unreachable at {url}, no macOS restart path — proceeding degraded")
        return
    m = re.search(r"figma\.com/design/([A-Za-z0-9]+)", str(ctx.inputs.get("figma") or ""))
    target = f"figma://file/{m.group(1)}" if m else None
    log_activity(ctx.run_dir, "figma",
                 "dev-mode server unreachable — restarting the Figma app" + (f" onto {target}" if target else ""))
    with contextlib.suppress(Exception):
        subprocess.run(["osascript", "-e", 'tell application "Figma" to quit'],
                       capture_output=True, timeout=15)
        time.sleep(2)
    try:
        subprocess.run(["open", "-a", "Figma", *((target,) if target else ())],
                       capture_output=True, timeout=15, check=True)
    except Exception:
        log_activity(ctx.run_dir, "figma",
                     "could not launch the Figma app — proceeding degraded (stage will record FETCH FAILED)")
        return
    for _ in range(20):  # ~60s of polling while the app boots and the server binds
        time.sleep(3)
        if _figma_probe(url):
            log_activity(ctx.run_dir, "figma", "dev-mode server is up after restart")
            return
    log_activity(ctx.run_dir, "figma",
                 "dev-mode server still unreachable after restart + 60s — proceeding degraded")


def _figma_tools_bound(init_ev: dict) -> bool:
    """True when the stream's init event bound the configured figma server's tools."""
    return any(isinstance(t, str) and t.startswith("mcp__figma__") for t in init_ev.get("tools") or [])


def _await_init(proc: subprocess.Popen, log_path: Path, deadline_s: float = 90.0) -> dict | None:
    """The stream's init event (arrives ~1s in), or None if the child exits / deadline passes first."""
    end = time.monotonic() + deadline_s
    while time.monotonic() < end:
        if log_path.exists():
            for line in log_path.read_text(encoding="utf-8", errors="replace").splitlines():
                try:
                    ev = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(ev, dict) and ev.get("type") == "system" and ev.get("subtype") == "init":
                    return ev
        if proc.poll() is not None:
            return None
        time.sleep(0.5)
    return None


def write_canonical_trace(run_dir: Path, label: str, stream_file: Path) -> Path | None:
    """Translate one finished raw stream into the canonical trace `logs/<label>.events.jsonl` (D3).

    Called from `run_claude` — i.e. after EVERY Claude invocation, whichever of the fifteen call sites
    made it, main pass and fan-out child and resumed revision alike. That is the point: the trace is a
    postcondition of the invocation, not of the adapter, so it cannot go missing just because a stage
    still binds the primitive directly. (Verified missing on a live `rfc-agent --stages ingest` smoke,
    2026-09-08: gates passed, `logs/` held zero `*.events.jsonl`, because the RFC stages reach
    run_claude without going through an adapter's run()/run_stage().)

    The LABEL, not the stream file's name, names the trace: the raw stream files are named
    inconsistently across call sites (`extract.stream.requote.jsonl`, `ingest.figma.b1.stream.jsonl`,
    `ground.stream.lead1.jsonl`) while `label` is already the canonical namespacing key, and
    `events_paths(run_dir, stage)` globs `<stage>.events.jsonl` + `<stage>.*.events.jsonl` to collect a
    stage's whole corpus.

    NEVER fails the stage. A stage that did its work must not die because its accounting could not be
    written; the ledger and the trace-backed witness gates already degrade to a loud WARN on an absent
    trace, so a logged failure lands in the same place a missing trace would.
    """
    from harness.engines.claude_translate import translate_file
    from harness.events import events_path

    dest = events_path(run_dir, label)
    try:
        return translate_file(stream_file, dest)
    except Exception as exc:  # noqa: BLE001 — accounting must never kill a completed stage
        msg = f"WARN canonical trace not written for {label!r}: {type(exc).__name__}: {exc}"
        print(f"  [{label}] {msg}", file=sys.stderr)
        with contextlib.suppress(Exception):
            log_activity(run_dir, label, msg)
        return None


def run_claude(
    stage: Stage,
    ctx: RunContext,
    prompt: str,
    log_path: Path,
    timeout: int,
    *,
    live: bool = True,
    label: str | None = None,
    procs: list[subprocess.Popen] | None = None,
    resume: str | None = None,
    model: str | None = None,
    obs_parent=None,
    extra_env: dict[str, str] | None = None,
    steer_epoch: float | None = None,
    effort: str | None = None,
) -> StreamResult:
    """ONE headless claude invocation under the stage's full containment (materialized MCP subset,
    write-guard, read-only allowlist, --setting-sources project).

    `label` namespaces the per-invocation side files (stderr/mcp/settings) so concurrent calls never race
    on a shared path. `live=False` skips the stream-tailing ticker; pass `obs_parent` (a live observation
    handle) to still emit per-tool obs events from a worker thread — the handle replaces the thread-local
    OTEL context that would otherwise mis-nest spans (pre-0.22.36 personas emitted nothing). The stream
    FILE always captures every event for the ledger/witness gates. `procs` collects the Popen so a fan-out
    caller can kill stragglers when a sibling fails. The caller stages the skill (once, before any
    fan-out — a concurrent copyfile to the same destination is a race)."""
    cfg = ctx.config
    label = label or stage.name
    if not resume:
        # every fresh child (stages, GROUND shards, REVIEW personas) learns the sandbox contract up
        # front instead of probing it; resumed sessions already carry it in context
        prompt += sandbox_affordances(
            str(Path(ctx.inputs["repos_root"]).expanduser()) if stage.add_repos_root else None
        )
        prompt += parallel_dispatch_note()
    logs = ctx.run_dir / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    err_path = logs / f"{label}.stderr"
    mcp_servers = stage_mcp_servers(stage, ctx)
    mcp_path = cfg.materialize_mcp(mcp_servers, logs / f"{label}.mcp.json")

    # per-stage model override (AGENT.STAGE_MODELS, e.g. {"generate": "<fast-writer>"}): the write leg's
    # speed is a MODEL property (Cursor's whole Composer thesis — a fast model for the high-token leg);
    # opt-in per stage so a speed experiment never silently changes the other stages. CLI --model wins.
    stage_model = (cfg.get("AGENT.STAGE_MODELS") or {}).get(stage.name)
    # explicit `model` param (a stage engine's per-invocation choice, e.g. a fast reader for shards)
    # sits between the CLI override and the per-stage config
    model = ctx.inputs.get("model") or model or stage_model or cfg.get("AGENT.DEFAULT_MODEL") or None
    # Budget is OPT-IN (--max-budget-usd only). There is deliberately no default: `--max-budget-usd` is a
    # HARD KILL, not a graceful stop — the agent is terminated mid-work with terminal_reason=budget_exhausted,
    # which surfaces as EngineFailure/exit 3. Measured 2026-07-27: EXTRACT died at $1.05 against a $1 cap with
    # its typing half-done, so the cap manufactured a failure instead of preventing one, and the spend still
    # happened. Per-stage caps also needed re-tuning per PRD size (GROUND 2.5->6.0, EXTRACT 1->3) with no principled
    # way to set them. Cost is explicitly not a concern for this pipeline (owner, 2026-07-27).
    budget = ctx.inputs.get("max_budget_usd")

    cmd = [
        cfg.get("AGENT.CLAUDE_BIN", "claude"),
        "-p",
        # the prompt moves off argv onto stdin (0.24.0): stream-json input is the CLI's documented
        # "provide guidance while the model is processing" channel — the kickoff prompt is the
        # first stdin user turn, and mid-run operator steering (steer.Pump) arrives as later turns.
        # The session ends on stdin EOF, so the pump closes the pipe when the terminal result
        # event lands. --replay-user-messages echoes stdin turns into the stream file, so steering
        # is auditable evidence exactly like every other turn.
        "--input-format",
        "stream-json",
        "--replay-user-messages",
        "--output-format",
        "stream-json",
        # deltas feed the live activity heartbeat during long document writes (the 3-6 min silences);
        # parse_stream/build_ledger key on complete event types and skip stream_event lines untouched
        "--include-partial-messages",
        "--verbose",
        # Headless: the agent MUST be able to write its artifacts without a permission prompt no human can
        # answer. Safe: throwaway run dir, strict MCP subset, Agent tool disallowed per stage config.
        "--permission-mode",
        "acceptEdits",
        # Persona isolation (probed 2026-07-27): WITHOUT this, the stage inherits the operator's USER-scope
        # settings — a global SessionStart hook was injecting a "lazy senior dev / YAGNI" persona into every
        # stage (a correctness risk for EXTRACT, whose job is to not drop requirements) and made runs
        # non-reproducible across operators. `project` keeps the run dir's .claude/skills (verified: skill
        # auto-discovery works), the explicit --settings write-guard below still loads (verified), and
        # user-scope OAuth MCP connectors still authenticate (verified live against Atlassian) — so this
        # severs ambient hooks/CLAUDE.md without touching auth.
        "--setting-sources",
        "project",
        "--mcp-config",
        str(mcp_path.resolve()),  # absolute — agent cwd is the run dir
    ]
    if budget:
        cmd += ["--max-budget-usd", str(budget)]
    if not stage.inherit_user_mcp:
        # default: strict isolation — user-scope servers are invisible, empty subset => guaranteed no-MCP.
        # INGEST opts out to reach the user's already-authed OAuth servers (see Stage.inherit_user_mcp);
        # inherited servers remain uncallable outside the read-only allowlist below.
        cmd.append("--strict-mcp-config")
    if mcp_servers:
        # acceptEdits only auto-allows file edits; MCP tool calls would be auto-DENIED headless (verified).
        # READ-ONLY tool grants only — never `mcp__<server>` (whole-server), which would hand write tools
        # (create_pull_request, editJiraIssue, ...) to an agent whose context holds untrusted ingested text.
        cmd += ["--allowedTools", ",".join(allowed_tools(mcp_servers))]
    # write containment: --add-dir targets are WRITABLE under acceptEdits (verified empirically 2026-07-25),
    # so the guard hook is what actually keeps the ~90 checkouts read-only. Passed via --settings rather than
    # a project .claude/settings.json so it can't be skipped by directory-trust prompts.
    # hook transport is flag-controlled (D6: `hooks` master switch, `hooks_off` per hook). RunContext.flag
    # falls back to config and then the default, so a run with no flags resolved materializes every hook
    # exactly as before.
    settings_path = _write_guard_settings(
        ctx.run_dir, logs / f"{label}.settings.json",
        hooks_enabled=bool(ctx.flag("hooks", True)),
        disabled_hooks=tuple(ctx.flag("hooks_off", []) or ()),
    )
    cmd += ["--settings", str(settings_path)]
    # feedback-channel canary (2026-09-03): once per process, once per CLI version per machine — a
    # real child under these settings must get a hook's additionalContext back, or the run is told
    # loudly that per-edit lints/pacing are dead (a 25-min compose once ran blind without anyone knowing)
    global _CANARY_PROBED
    # MIGRATED to the flag registry (D5 worked example): `hook_canary`, env HARNESS_HOOK_CANARY, with
    # AGENT.HOOK_CANARY bridged as its config key (workflows.rfc.LEGACY_CONFIG_KEYS) so existing
    # config.json files and the `--disable hook_canary` CLI toggle both work.
    if not _CANARY_PROBED and ctx.flag("hook_canary", True):
        _CANARY_PROBED = True
        from . import hooks_canary
        hooks_canary.ensure_hooks_alive(ctx, settings_path, cfg.get("AGENT.CLAUDE_BIN", "claude"))
    if stage.add_repos_root:
        cmd += ["--add-dir", str(Path(ctx.inputs["repos_root"]).expanduser())]
    deny = deny_list(stage.disallowed_tools, ctx.config.get("AGENT.EXTRA_DISALLOWED_TOOLS"))
    if deny:
        cmd += ["--disallowed-tools", deny]
    if model:
        cmd += ["--model", model]
    stage_level, child_env_base, effort_notes = stage_effort(cfg, stage.name, dict(os.environ))
    if effort:  # per-invocation override (e.g. extract's reconcile child at medium under a low stage)
        if effort not in EFFORT_LEVELS:
            raise EngineFailure(f"effort={effort!r} — allowed: {', '.join(EFFORT_LEVELS)}")
        child_env_base = {k: v for k, v in child_env_base.items() if k != _EFFORT_ENV}
        effort_notes = []
    else:
        effort = stage_level
    if effort:
        # per-stage reasoning effort (AGENT.STAGE_EFFORT); applies to resumed sessions too — a revision
        # pass on a low-effort stage stays low-effort
        cmd += ["--effort", effort]
        if stage.name not in _effort_noted:
            _effort_noted.add(stage.name)
            log_activity(ctx.run_dir, label, f"effort={effort} (AGENT.STAGE_EFFORT)")
            for note in effort_notes:
                print(f"  [{label}] WARN {note}", file=sys.stderr)
                log_activity(ctx.run_dir, label, f"WARN {note}")
    if resume:
        # continue the session that produced the current outputs — its context (skill, inputs, everything
        # it read) is already loaded, so a revision is applied in seconds instead of a fresh-context rerun
        # (probed 2026-07-28: resumed targeted edit = 12.7s / 2 turns vs 400s+ cold)
        cmd += ["--resume", resume]

    # thinking tap (see think_tap.py): the 5-family API omits thinking text unless the request carries
    # display="summarized", and the CLI blanks it headless even then — so a localhost sidecar sets the
    # field and reads the text off the wire into the feed. None => child runs exactly as before.
    # OFF by default and deliberately so: it sits in the path of AUTHENTICATED traffic (the child's
    # OAuth headers transit the sidecar process), which is the operator's call to make, not a default.
    tap = None
    if cfg.get("AGENT.THINKING_TAP", False):
        tap = think_tap.start_tap(logs / "activity.log", label, logs / f"{label}.thinking.txt")

    env = {**child_env_base, "RFC_AGENT_RUN_DIR": str(ctx.run_dir.resolve()),
           # the guard hook reads RFC_AGENT_RUN_DIR to know the one writable tree
           "RFC_AGENT_REPOS_ROOT": str(Path(ctx.inputs.get("repos_root") or "").expanduser()),
           **(extra_env or {})}
    if tap:
        env["ANTHROPIC_BASE_URL"] = tap.base_url
    from . import obs

    # hand the live trace context down (W3C traceparent): grandchild processes the agent spawns
    # (`python -m rfc_agent.selfcheck`) attach their decision events to THIS run's trace instead of
    # minting orphan traces with no session/user. Worker threads have no ambient context — derive
    # from the persona's obs handle there.
    tp = None
    if obs_parent is not None:
        with contextlib.suppress(Exception):
            tp = f"00-{obs_parent.trace_id}-{obs_parent.id}-01"
    tp = tp or obs.traceparent()
    if tp:
        env[obs.TRACEPARENT_ENV] = tp

    figma_bound = "figma" in mcp_servers
    if figma_bound:
        figma_prewarm(ctx)  # $0 pre-flight: probe/restart the desktop server BEFORE any model spend
    try:
        for spawn_attempt in (1, 2):
            with open(log_path, "w") as out, open(err_path, "w") as err:
                try:
                    proc = subprocess.Popen(
                        cmd,
                        cwd=str(ctx.run_dir),
                        stdin=subprocess.PIPE,  # kickoff + steering ride stdin (stream-json input)
                        stdout=out,
                        stderr=err,
                        # explicit — text=True alone uses the locale encoding, so a non-ASCII prompt
                        # under a C locale raised UnicodeEncodeError (a ValueError, which escaped the
                        # old suppress(OSError) and killed the run)
                        encoding="utf-8",
                        start_new_session=True,
                        env=env,
                    )
                except FileNotFoundError:
                    raise EngineFailure("`claude` CLI not found on PATH — install/enable Claude Code.") from None
                if procs is not None:
                    procs.append(proc)
                # steer_epoch: a resume→cold fallback passes the FAILED child's epoch so steering
                # already delivered (and 🧭-acked) to the dead child redelivers to its replacement
                pump = steer.Pump(proc, log_path, ctx, label, obs_parent=obs_parent,
                                  epoch=steer_epoch)
                print(f"  [{label}] agent started -> {log_path}", file=sys.stderr)
                try:
                    log_activity(ctx.run_dir, label, "agent started")
                    # inside the guard: Ctrl-C mid-write now kills the group instead of orphaning
                    _send_kickoff(proc, prompt, label, err_path)
                    if figma_bound and spawn_attempt == 1:
                        # binding-race check: the init event names the tools frozen into the first
                        # prompt. Zero figma tools now = zero for the whole run — kill pre-spend and
                        # respawn once instead of paying a full stage that can only FETCH FAIL.
                        init_ev = _await_init(proc, log_path)
                        if init_ev is not None and not _figma_tools_bound(init_ev):
                            errs = init_ev.get("mcp_server_errors") or [
                                s for s in init_ev.get("mcp_servers") or []
                                if isinstance(s, dict) and s.get("status") not in (None, "connected")
                            ]
                            _kill_group(proc)
                            log_activity(ctx.run_dir, label,
                                         f"init event bound ZERO mcp__figma__ tools "
                                         f"({errs or 'no server error reported'}) — killed pre-spend; "
                                         f"restarting Figma and respawning once")
                            figma_prewarm(ctx, restart=True)
                            continue
                    if live:
                        _wait_with_progress(proc, log_path, timeout, ctx, activity_label=label,
                                            tap_active=bool(tap), pump=pump)
                    elif obs_parent is not None:
                        # worker-thread invocation with an explicit obs parent: full stream tail (live tool
                        # events under the parent handle), just no stderr ticker — concurrent \r lines interleave
                        _wait_with_progress(proc, log_path, timeout, ctx, activity_label=label,
                                            tap_active=bool(tap), quiet=True, obs_parent=obs_parent,
                                            pump=pump)
                    else:
                        # quiet mode still feeds the live activity log — shards/personas used to be invisible
                        # until done, which is exactly the "what is it doing?" gap the feed exists to close
                        deadline = time.monotonic() + timeout
                        act_pos, act_state = 0, {"tap": bool(tap)}
                        while proc.poll() is None:
                            if time.monotonic() > deadline:
                                raise subprocess.TimeoutExpired(proc.args, timeout)
                            act_pos = drain_activity(log_path, act_pos, ctx.run_dir, label, act_state)
                            pump.tick()
                            time.sleep(1.0)
                        drain_activity(log_path, act_pos, ctx.run_dir, label, act_state)
                    log_activity(ctx.run_dir, label, "agent finished")
                except subprocess.TimeoutExpired:
                    _kill_group(proc)
                    raise EngineFailure(
                        f"'{label}' timed out after {timeout}s (stream: {log_path}) — "
                        f"stderr tail: {_stderr_tail(err_path)}") from None
                except KeyboardInterrupt:
                    # the child runs in its own session (start_new_session), so the terminal's SIGINT never
                    # reaches it — without this, Ctrl-C orphans the claude child + its nested MCP servers
                    _kill_group(proc)
                    raise
                finally:
                    # EVERY path (0.24.2): success, timeout, Ctrl-C, figma-respawn's first proc.
                    # Close-on-success-only left a dead child's stdin TextIOWrapper to flush at GC
                    # ("Exception ignored in __del__" mid-feed). Close never consumes steering —
                    # consumption is per-DELIVERY in the ledger (0.24.3), so a killed child's
                    # undelivered records carry to its replacement.
                    pump.close()
            break
    finally:
        if tap:
            tap.stop()

    res = parse_stream(log_path.read_text(encoding="utf-8", errors="replace"))
    if not res.got_result and proc.returncode:
        # the child died before emitting its terminal result event — auth expiry, a malformed
        # --settings file, an MCP config error, OOM, a CLI usage error. An empty stream used to
        # parse as a clean zero-output stage, sending the runner into identical output-gate retries
        # with the real cause sitting unread in stderr (the Agent SDK's ProcessError pattern, plus
        # the stderr tail the SDK itself omits).
        raise EngineFailure(
            f"'{label}' exited {proc.returncode} before emitting a result — stderr tail: "
            f"{_stderr_tail(err_path)} (full: {err_path})")
    if res.extra_results:
        # a post-result bonus turn ran (a steer racing the terminal result — file-poll lag window).
        # Its verdict is deliberately discarded (first-result-wins), but its file edits are on disk,
        # so surface the mismatch instead of letting a failed bonus turn pass silently.
        from . import decisions
        log_activity(ctx.run_dir, label,
                     f"⚠ {res.extra_results} post-result turn(s) ran"
                     + (" and ERRORED — verdict kept from the stage's own result; artifacts go to "
                        "the gate as usual" if res.extra_result_error else ""))
        decisions.record(
            ctx, label.split(".")[0], Decision.POST_RESULT_TURN,
            why="a queued turn ran after the stage's terminal result (steer racing the result); "
            "verdict fields keep the first result — the gate remains the authority on artifacts",
            lane=label, extra_results=res.extra_results, errored=res.extra_result_error,
        )
    # the EXACT prompt sent (post sandbox-affordances append): the pre-append copy recorded upstream
    # differed from what the model received on every stage — fatal for dataset items built from a
    # generation's input, and it hid warm-vs-cold on resumed workers (audit 2026-08-19)
    res.prompt_sent = prompt
    write_canonical_trace(ctx.run_dir, label, log_path)
    return res


def stream_from_summary(summary, engine_desc: str = "") -> StreamResult:
    """A canonical TraceSummary -> the StreamResult shape the unmigrated fan-out bodies still read.

    WHY this bridge exists: `engine_call` below lets a fan-out call site route through ANY engine
    adapter, but the nine call-site bodies were written against `StreamResult` (they append to a list
    that `merge_stream_results` sums, read `.session_id` to save a resume point, branch on
    `.is_error`). An engine whose native result is not a StreamResult — `replay`, and Pi — would
    otherwise be unusable at those sites even after the one-word migration. Translating the canonical
    summary is cheaper and more honest than making every adapter forge Claude's wire format (D3).

    `tool_calls` is re-keyed to `tool_name`, which is the key the drain/obs readers use.
    """
    res = StreamResult(
        final_text=summary.final_text,
        tool_calls=[{"tool_name": t.get("name"), "input": t.get("input") or {}}
                    for t in (summary.tool_calls or [])],
        cost_usd=summary.cost_usd,  # None stays None: a stage that reported no cost must not read $0
        usage=summary.usage,
        is_error=summary.is_error,
        num_turns=summary.num_turns,
        model=summary.model or (engine_desc.partition(":")[2] or None),
        error_reason=summary.error_reason,
        session_id=summary.session_id,
        compactions=summary.compactions,
        denials=summary.denials,
        got_result=summary.got_result,
    )
    return res


def workflow_containment_env(ctx: RunContext) -> dict[str, str]:
    """The env this run's WORKFLOW exports to every child (Workflow.containment_env).

    Resolved by name off `ctx.workflow` rather than passed down, so no call site has to thread a
    Workflow object through. Returns {} for a context with no workflow — an embedder calling
    run_pipeline directly gets the policy's own defaults, which is a missing grant, not a wrong verdict.
    """
    name = getattr(ctx, "workflow", "") or ""
    if not name:
        return {}
    try:
        from harness.workflows import get as _get_workflow

        hook = _get_workflow(name).containment_env
    except Exception:  # noqa: BLE001 — a missing workflow must not stop a child from spawning
        return {}
    if hook is None:
        return {}
    try:
        return {k: str(v) for k, v in (hook(ctx) or {}).items()}
    except Exception as exc:  # noqa: BLE001
        print(f"  [{name}] WARN workflow containment env not computed: {exc}", file=sys.stderr)
        return {}


def engine_stage(stage: Stage, ctx: RunContext) -> StageResult:
    """The WHOLE stage through the run's engine adapter (D4), signature-identical to `claude_engine`.

    The companion to `engine_call`: a stage-owned fan-out engine typically runs a MAIN pass plus N
    children, and both halves have to move for `--engine` to mean anything for that stage. This is the
    main-pass half — one word, `claude_engine` -> `engine_stage`.

    Falls back to `claude_engine` when no adapter is resolved. No recursion risk: the runner picks the
    stage's OWN engine for a fan-out stage, so `run_stage` is only ever reached from inside it.
    """
    engine = getattr(ctx, "engine", None)
    if engine is None:
        return claude_engine(stage, ctx)
    run_stage = getattr(engine, "run_stage", None)
    if run_stage is None:
        # Falling back to claude_engine HERE would be a lie: the operator asked for another engine and
        # the main pass would silently run on Claude while the stage's migrated children ran on the
        # chosen one — a split run reported under one engine name. Refuse instead.
        raise EngineFailure(
            f"engine {getattr(engine, 'name', engine)!r} cannot run stage {stage.name!r}'s main pass: "
            f"an engine driving a whole stage must expose run_stage(stage, ctx). Use --stage-engine "
            f"{stage.name}=claude-code, or add run_stage to the adapter."
        )
    return run_stage(stage, ctx)


def engine_call(
    stage: Stage,
    ctx: RunContext,
    prompt: str,
    log_path: Path,
    timeout: int,
    *,
    live: bool = True,
    label: str | None = None,
    procs: list[subprocess.Popen] | None = None,
    resume: str | None = None,
    model: str | None = None,
    obs_parent=None,
    extra_env: dict[str, str] | None = None,
    steer_epoch: float | None = None,
    effort: str | None = None,
) -> StreamResult:
    """ONE child invocation through the RUN'S ENGINE ADAPTER (D4's seam), signature-identical to
    `run_claude`.

    This is the migration unit. A stage-owned fan-out engine that still names `run_claude` is bound to
    the Claude primitive no matter what `--engine` says; changing that one word to `engine_call` makes
    the site honour the adapter. The signature is identical precisely so the migration is a one-word
    diff and the call site's body needs no review.

    Falls back to `run_claude` when no adapter is resolved (`ctx.engine is None`) — every legacy caller
    and every test that builds a RunContext by hand keeps working unchanged.

    Remaining unmigrated sites carry `# TODO(harness-engine-seam)`; grep for it to find the work.
    """
    engine = getattr(ctx, "engine", None)
    if engine is None or not hasattr(engine, "run"):
        return run_claude(stage, ctx, prompt, log_path, timeout, live=live, label=label, procs=procs,
                          resume=resume, model=model, obs_parent=obs_parent, extra_env=extra_env,
                          steer_epoch=steer_epoch, effort=effort)
    from harness.engines import EngineRequest
    from harness.events import events_path

    label = label or stage.name
    result = engine.run(EngineRequest(
        prompt=prompt,
        cwd=ctx.run_dir,
        label=label,
        trace_path=events_path(ctx.run_dir, label),
        raw_trace_path=log_path,  # the raw stream stays exactly where the ledger looks for it
        timeout_s=timeout,
        model=model,
        effort=effort,
        resume_session=resume,
        # the workflow's policy grants first, so an explicit extra_env from a call site still wins
        env={**workflow_containment_env(ctx), **(extra_env or {})},
        live=live,
        obs_parent=obs_parent,
        procs=procs,
        steer_epoch=steer_epoch,
        native_ctx={"stage": stage, "ctx": ctx},
    ))
    if isinstance(result.native, StreamResult):
        return result.native  # claude-code: the identical object run_claude would have returned
    stream = stream_from_summary(result.summary, result.engine_desc)
    stream.prompt_sent = result.prompt_sent or prompt
    return stream


def session_path(run_dir: Path, stage_name: str) -> Path:
    """logs/<stage>.session — the session id able to revise this stage's current outputs (warm context)."""
    return run_dir / "logs" / f"{stage_name}.session"


# Resume-cap policy (window-aware since 0.22.42; operator-specified design 2026-08-15).
# History: the fixed 100K cap was calibrated on an old-CLI incident — resuming a 171,625-token session
# on a 200K-window model (claude-sonnet-5, 2026-08-11, TASK-29403) paid 2m24s of auto-compaction at
# startup + 3m43s mid-flight = 6.1 min of a 19.2-min revision. Two facts have since changed: CLI
# >=2.1.223 compacts near the model's REAL window (~967K on 1M models, configurable via
# CLAUDE_CODE_AUTO_COMPACT_WINDOW), and the >200K long-context price premium was abolished (GA 2026-03).
# What did NOT change: useful context ends well before capacity (context rot — NoLiMa: <50% of
# short-context baseline by 32K on latent-association retrieval; Chroma: degradation well before 200K;
# a failed attempt's transcript is made of distractors), and fresh costs nothing here because files are
# the API (outputs + guidance + evidence pack live on disk). So: the cap scales with the VERIFIED
# window but is bounded by a quality ceiling — capacity and useful context are different things.
# The window is verified by OBSERVATION (a session provably ran past the legacy 200K regime without
# compacting), never by CLI-version inference — active claude-code reports of resume/auto-compaction
# misbehavior (#43989/#53801-class regressions silently dropping 1M thresholds) make observation the
# only trustworthy signal. Unverifiable -> the legacy 100K cap stays.
_LEGACY_RESUME_CAP = 100_000
_SAFETY_BUFFER = 70_000  # a revision's observed new-token budget: room to work before any ceiling
_QUALITY_CEILING = 200_000  # default AGENT.RESUME_CONTEXT_CAP — a HYPOTHESIS to tune per workload
# (bucket experiment: 100/150/200/250/300K+ resume sizes vs fresh, on revision success + cost + TTFU),
# not a law; the per-revision resume_policy decision log accumulates the data passively.
_VERIFIED_WINDOW = 1_000_000  # what observation/static checks verify (the [1m] model family)
_AUTOCOMPACT_PIN = "900000"  # pinned on resumed children of a verified window: regression-proofs the
# mechanics term against CLI builds that silently fall back to ~200-400K triggers on 1M models


def save_session(sid_file: Path, stream) -> None:
    """Persist session id, end-of-run context size, and compaction count for the resume-cost decision."""
    sid_file.write_text(f"{stream.session_id}\n{stream.context_tokens or 0}\n{stream.compactions}")


def load_session(sid_file: Path) -> tuple[str, int, int]:
    """(session_id, context_tokens, compactions). Tolerates older 1- and 2-line formats
    (missing tokens -> 0 = always resume; missing compactions -> 0 = assume none observed)."""
    lines = sid_file.read_text().strip().splitlines()
    sid = lines[0].strip() if lines else ""
    try:
        tokens = int(lines[1]) if len(lines) > 1 else 0
    except (ValueError, IndexError):
        tokens = 0
    try:
        compactions = int(lines[2]) if len(lines) > 2 else 0
    except (ValueError, IndexError):
        compactions = 0
    return sid, tokens, compactions


def detect_effective_window(run_dir: Path, model: str | None) -> int | None:
    """The child's effective context window, or None when it cannot be verified.

    Strongest signal first — observation: any saved session in this run that exceeded the legacy
    200K regime with zero compaction events proves the window is big (a 200K-window session cannot
    reach those sizes; compaction fires first). Weaker fallback — static checks: the model id carries
    [1m] and nothing in the environment downgrades it (CLAUDE_CODE_DISABLE_1M_CONTEXT, or a gateway
    via ANTHROPIC_BASE_URL, which budgets 200K). Ambiguous -> None -> legacy cap."""
    with contextlib.suppress(OSError):
        for f in sorted((run_dir / "logs").glob("*.session")):
            _, tokens, compactions = load_session(f)
            if tokens > _QUALITY_CEILING and compactions == 0:
                return _VERIFIED_WINDOW
    base_url = os.environ.get("ANTHROPIC_BASE_URL") or ""
    if (
        model
        and "[1m]" in model
        and not os.environ.get("CLAUDE_CODE_DISABLE_1M_CONTEXT")
        # only a NON-official base URL indicates a gateway (which budgets 200K); the harness
        # itself exports the official endpoint, which must not disqualify the window (observed
        # 2026-08-15: ANTHROPIC_BASE_URL=https://api.anthropic.com in the parent env dropped a
        # [1m] revision to the legacy 100K cap)
        and (not base_url or "api.anthropic.com" in base_url)
    ):
        return _VERIFIED_WINDOW
    return None


def resume_cap(cfg, effective_window: int | None) -> int:
    """The resume cap for this revision: legacy 100K when the window is unknown or absurdly small,
    else min(window - safety buffer, quality ceiling). The ceiling (AGENT.RESUME_CONTEXT_CAP,
    default 200K) is the operator's tunable — see the bucket-experiment note above."""
    ceiling = (cfg.get("AGENT.RESUME_CONTEXT_CAP") if cfg else None) or _QUALITY_CEILING
    if effective_window is None or effective_window <= _SAFETY_BUFFER:
        return _LEGACY_RESUME_CAP
    return min(effective_window - _SAFETY_BUFFER, ceiling)


def resumable(sid_file: Path, cfg, effective_window: int | None = None) -> tuple[str | None, int, int]:
    """(session id to resume or None, saved context tokens, cap applied) — id is None when resuming
    would cost more than it saves under the window-aware policy."""
    cap = resume_cap(cfg, effective_window)
    if not sid_file.exists():
        return None, 0, cap
    sid, tokens, _ = load_session(sid_file)
    if tokens > cap:
        return None, tokens, cap
    return sid or None, tokens, cap


def claude_engine(stage: Stage, ctx: RunContext) -> StageResult:
    _stage_skill(stage.name, ctx.run_dir, stage.skill_variant(ctx) if stage.skill_variant else None)
    timeout = stage.timeout_s or ctx.config.get("AGENT.RUN_TIMEOUT", 2400)
    log_path = stream_path(ctx.run_dir, stage.name)

    t0 = time.monotonic()
    stream = None
    revising = (
        guidance_path(ctx.run_dir, stage.name).exists()
        and stage.revise_on_retry
        and all((ctx.run_dir / o).exists() for o in stage.outputs)
    )
    # emission mode: deliverable tool-writes are hook-denied on EVERY generate invocation (compose and
    # revision) — the <emit>/<edits> protocol is the only write path, so a prompt-only prohibition is
    # an escape hatch (v7, 2026-09-07: compose rationalized a direct Write past it)
    _emit_env = {}
    if stage.name == "generate":
        from .emit import emit_enabled
        from .hooks.guard_writes import EMIT_DELIVERABLES_ENV

        if emit_enabled(ctx.config):
            _emit_env = {EMIT_DELIVERABLES_ENV: "1"}
    sid_file = session_path(ctx.run_dir, stage.name)
    # patch mode: all-mechanical failure sets get a constrained one-edit-per-failure pass, optionally on
    # a cheaper model (AGENT.STAGE_MODELS["patch"]) — the design-revision model is overkill for
    # qualify-this-citation work
    patching = revising and patchable_revision(stage, ctx)
    patch_model = (ctx.config.get("AGENT.STAGE_MODELS", {}) or {}).get("patch") if patching else None
    if patching:
        from . import decisions

        decisions.record(
            ctx, stage.name, Decision.PATCH_REVISION,
            why="every failure in the latest gate block is mechanical (citations/coverage tags) — "
            "constrained patch pass instead of a free-form revision",
            failures=len(latest_gate_failures(ctx.run_dir, stage.name)),
        )
    # MIXED failure sets: patch the mechanical subset in a constrained cheap pre-pass, then scope the
    # free-form revision to the substantive rest (v8 ground, 2026-09-08). GENERATE is excluded: its
    # emission engine drains only the CURRENT stream, so a rotated pre-pass's <emit>/<edits> blocks
    # would be silently lost — lift the exclusion when the drain learns to walk rotated streams.
    split_mech = (split_mechanical_failures(stage, ctx)
                  if revising and not patching and stage.name != "generate" else [])
    if split_mech:
        from . import decisions

        pre_prompt = (
            "PATCH PASS — mechanical fixes only. Your previous attempt's output files are on disk in "
            "this working directory; a separate revision handles the substantive failures, so touch "
            "NOTHING beyond the list below. Fix EACH failure with the smallest targeted Edit: locate "
            "the exact defect it names, verify against the actual file (read the specific cited lines "
            "— never guess a path or line number), apply ONE edit, move on. If a citation cannot be "
            "verified, delete or soften the claim rather than re-citing it. Byte ceilings/budgets are "
            "WAIVED. Then stop.\n\n- GATE FAILURES (mechanical subset):\n"
            + "\n".join(f"  - {f}" for f in split_mech)
        )
        try:
            run_claude(stage, ctx, pre_prompt, log_path, timeout,
                       model=(ctx.config.get("AGENT.STAGE_MODELS", {}) or {}).get("patch"),
                       extra_env={DIFF_ONLY_ENV: "1", **_emit_env})
            if log_path.exists():  # keep the patch stream as witness evidence, like the runner's rotation
                n = 1
                while rotated_stream_path(ctx.run_dir, stage.name, n).exists():
                    n += 1
                log_path.rename(rotated_stream_path(ctx.run_dir, stage.name, n))
                # ... and its canonical trace with it, same attempt number, for the same reason the
                # stream is kept: the pre-pass is part of the corpus that produced the current outputs
                from .runner import _rotate_trace

                _rotate_trace(ctx.run_dir, stage.name, n)
            drop_mechanical_guidance(ctx.run_dir, stage.name, len(split_mech))
            decisions.record(
                ctx, stage.name, Decision.PATCH_REVISION,
                why="mixed failure set: mechanical subset patched in a constrained pre-pass; the "
                "free-form revision is scoped to the substantive failures",
                mechanical=len(split_mech),
            )
            log_activity(ctx.run_dir, stage.name,
                         f"patched {len(split_mech)} mechanical failure(s) in a pre-pass — "
                         f"revision now covers only the substantive ones")
        except EngineFailure as e:
            print(f"  [{stage.name}] WARN mechanical pre-pass died ({e}) — "
                  f"the full revision covers everything", file=sys.stderr)
    _stage_model = (
        ctx.inputs.get("model")
        or (ctx.config.get("AGENT.STAGE_MODELS") or {}).get(stage.name)
        or ctx.config.get("AGENT.DEFAULT_MODEL")
    )
    _window = detect_effective_window(ctx.run_dir, _stage_model) if revising else None
    resume_sid, _saved_tokens, _cap = (
        resumable(sid_file, ctx.config, _window) if revising else (None, 0, 0)
    )
    if revising and sid_file.exists() and resume_sid is None:
        # conditional resume: the saved session is past the window-aware cap — a fresh revision
        # (files + guidance carry the state) beats dragging a distractor-heavy transcript along
        log_activity(ctx.run_dir, stage.name,
                     f"revision going FRESH — saved session ≈{_saved_tokens // 1000}K tokens exceeds the "
                     f"resume cap ({_cap // 1000}K; window "
                     f"{'verified ' + str(_window // 1000) + 'K' if _window else 'unverified'})")
        from . import decisions
        # RESUME_SKIPPED, not a second REVISE_IN_PLACE: the runner already recorded the revise
        # decision for this attempt, and the duplicate node pair (3ms apart, verified live on
        # PROJ-3394-v5, 2026-08-19) read as a double-emission bug in the graph
        decisions.record(
            ctx, stage.name, Decision.RESUME_SKIPPED,
            why=f"resume skipped: saved session at ~{_saved_tokens} tokens exceeds the window-aware "
            f"resume cap — fresh context with the on-disk evidence pack instead",
            resume_policy={"branch": "fresh", "saved_tokens": _saved_tokens,
                           "effective_window": _window, "cap": _cap},
        )
    # shared steering epoch across the resume attempt AND its cold fallback: a steer delivered
    # (and 🧭-acked) to a failed resumed child must redeliver to the fallback child that does the
    # real work — otherwise the operator holds a positive ack for a correction nobody applied
    _steer_epoch = time.time()
    if revising and resume_sid:
        # retry-by-resume: measured 2026-07-28 that rework was ~40% of a run's wall-clock, and almost all
        # of it was a fresh-context agent RE-READING everything before minutes-of-actual-fixes. Resuming
        # the session that wrote the outputs skips the re-read. Fail-safe: any error falls back to the
        # fresh-context revision below (outputs are untouched on disk either way).
        prompt = (
            "You are RESUMED in the session whose outputs are on disk. Byte ceilings/budgets are "
            "WAIVED for revisions — never trim, restructure, or spend a single action on document "
            "size; fix the failures and stop." + _guidance_text(stage, ctx)
        )
        log_activity(ctx.run_dir, stage.name,
                     "patch revision — mechanical fixes only (one Edit per failure)" if patching else
                     "revision — RESUMING the authoring session (targeted edits, not a rewrite)")
        try:
            stream = run_claude(
                stage, ctx, prompt, log_path, timeout, resume=resume_sid, model=patch_model,
                steer_epoch=_steer_epoch,
                # verified-window resumes pin the compaction trigger explicitly: regression-proof against
                # CLI builds that silently fall back to ~200-400K triggers on 1M models
                extra_env={DIFF_ONLY_ENV: "1", **_emit_env,
                           **({"CLAUDE_CODE_AUTO_COMPACT_WINDOW": _AUTOCOMPACT_PIN} if _window else {})},
            )
        except EngineFailure as e:
            # a dead resume (session GC'd/rotated: nonzero exit, no result event) raises since the
            # 0.24.2 dead-child backstop — honor this path's own fail-safe promise instead of
            # aborting the run on a condition whose designed remedy is the cold revision below
            print(f"  [{stage.name}] WARN resumed revision died ({e}) — "
                  f"falling back to a fresh-context revision", file=sys.stderr)
            log_activity(ctx.run_dir, stage.name,
                         "resumed revision died — falling back to a fresh-context revision")
            stream = None
        if stream is not None and stream.is_error:
            print(
                f"  [{stage.name}] WARN resumed revision errored ({stream.error_reason or 'unknown'}) — "
                f"falling back to a fresh-context revision",
                file=sys.stderr,
            )
            stream = None
        elif stream is not None:  # not the EngineFailure fallback above
            from . import decisions

            decisions.record(
                ctx, stage.name, Decision.RESUMED_REVISION,
                why="revision applied by resuming the authoring session (warm context) instead of a "
                "fresh-context rerun",
                resume_policy={"branch": "resume", "saved_tokens": _saved_tokens,
                               "effective_window": _window, "cap": _cap},
            )
    if stream is None:
        if patching:
            # slim prompt for cold patch revisions: the full build_prompt tells the agent to re-read the
            # entire corpus (raw/ + findings), which for a one-edit-per-failure pass is pure tax —
            # measured 27s/fix cold vs 13s/fix warm (2026-08-07). The failures name their own targets.
            prompt = ("Your previous attempt's output files are on disk in this working directory. "
                      "Byte ceilings/budgets are WAIVED for revisions — never trim, restructure, or "
                      "spend a single action on document size; fix the failures and stop."
                      + _guidance_text(stage, ctx))
        else:
            prompt = assemble_prompt(stage, ctx)
            if revising:
                # the FOURTH pressure point (field find, PROJ-3394-v7 generate#4 2026-08-26): a
                # revision that can't warm-resume (resume_skipped — saved session over the cap, the
                # COMMON case for big late-round back_jumps) lands here on the composer's prompt,
                # whose SKILL states the ceilings as hard rules — the agent spent ~3.5 min trimming
                # work the gate and hook had both already waived. Same waiver sentence as the other
                # two revision branches, prepended so it outranks the skill's compose-time framing.
                prompt = ("Byte ceilings/budgets are WAIVED for revisions — never trim, restructure, "
                          "or spend a single action on document size (the skill's byte rules are "
                          "compose-time only); fix the failures and stop.\n\n" + prompt)
        # diff-only applies to the cold revision too: the fresh-context path is exactly where a
        # "rewrite it properly this time" full Write is most tempting, and most expensive
        stream = run_claude(stage, ctx, prompt, log_path, timeout, model=patch_model,
                            steer_epoch=_steer_epoch,
                            extra_env={**({DIFF_ONLY_ENV: "1"} if revising else {}), **_emit_env} or None)
    if stream.is_error:
        # An error AFTER the agent wrote its outputs is not necessarily lost work: a transient api_error
        # can land on the last turn. The deterministic gate — not the transport-level error flag — is this
        # architecture's authority on whether output is usable, and it catches truncation anyway (missing
        # sections, unresolvable/unwitnessed citations). So only fail hard when an output is actually
        # missing; otherwise hand the artifacts to the gate, which either passes them or produces
        # actionable failures for the bounded retry.
        # Measured 2026-07-27: GROUND died with terminal_reason=api_error on its final Write, and the
        # orphaned findings.md (25KB, 5/5 sections, 130 resolving+witnessed citations) was ONE fixable
        # gate failure from green — $8.01 of completed work discarded by a hard raise here.
        missing = [o for o in stage.outputs if not (ctx.run_dir / o).exists()]
        if missing:
            raise EngineFailure(
                f"stage '{stage.name}' claude run errored (cost=${stream.cost_usd or 0:.2f}) with "
                f"output(s) missing: {missing} (stream: {log_path})"
            )
        from . import decisions

        print(
            f"  [{stage.name}] agent errored ({stream.error_reason or 'unknown'}) BUT all declared outputs "
            f"exist — handing them to the gate rather than discarding the run",
            file=sys.stderr,
        )
        decisions.record(
            ctx, stage.name, Decision.GATE_DESPITE_ENGINE_ERROR,
            why=f"stream reported an error ({stream.error_reason or 'unknown'}) after every declared output "
                f"was written; the deterministic gate decides usability, not the transport error flag",
            cost_usd=stream.cost_usd, outputs=list(stage.outputs),
        )
    # record the model the child ACTUALLY used, not the one we asked for: with no --model the child
    # inherits the operator's ambient default, and `claude:default` in the manifest/Langfuse hides which
    # model produced a run — fatal for comparing eval runs across branches.
    res = StageResult.from_stream(
        stream, duration_s=time.monotonic() - t0,
        engine_desc="claude:" + str(stream.model or ctx.inputs.get('model')
                              or ctx.config.get('AGENT.DEFAULT_MODEL') or 'unknown'),
    )
    res.prompt = stream.prompt_sent or prompt  # the as-sent form, sandbox block included
    if stream.session_id:  # the session that now owns these outputs — a later revision resumes it
        save_session(sid_file, stream)  # id + context size: the resume-cost decision needs both
    return res


def merge_stream_results(streams: list[StreamResult], primary: StreamResult, duration_s: float,
                         engine_desc: str) -> StageResult:
    """StageResult over a fan-out: `primary` supplies final_text/model semantics, totals sum every call —
    the manifest's accounting must never under-report what a parallel stage actually consumed.

    Usage sums too (0.23.0): it used to be the FIRST-FINISHED worker's alone, so a fan-out generation
    reported the sum of every worker's cost against one worker's tokens (audit 2026-08-19). Per-worker
    truth lives on each persona/shard node via subagent_finished(stream=...)."""
    res = StageResult.from_stream(primary, duration_s=duration_s, engine_desc=engine_desc)
    res.cost_usd = round(sum(s.cost_usd or 0 for s in streams), 6) or None
    res.num_turns = sum(s.num_turns or 0 for s in streams) or None
    res.tool_calls = [t for s in streams for t in s.tool_calls]
    # per-value type guard: the CLI already nests SOME usage keys (cache_creation is an object);
    # a bare int() over a stream-parsed value would kill a fully-successful fan-out stage at the
    # accounting step — and the crash-recovery path calls this same function (review finding #9)
    def _tok(v) -> int:
        return int(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else 0
    merged = {k: sum(_tok((s.usage or {}).get(k)) for s in streams)
              for k in ("input_tokens", "output_tokens",
                        "cache_read_input_tokens", "cache_creation_input_tokens")}
    if any(merged.values()):
        res.usage = merged
    res.denials = sum(getattr(s, "denials", 0) for s in streams)
    res.compactions = sum(getattr(s, "compactions", 0) for s in streams)
    res.context_tokens = max((s.context_tokens or 0 for s in streams), default=0) or None
    return res
