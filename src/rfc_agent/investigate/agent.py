"""One headless `claude -p` agent for Investigate (lead, tracer or UI reader).

Lean on purpose — not rfc_agent.engine.run_claude, which carries the old stages' machinery. What it does:
- built-in tools limited with --tools (no Bash, no Agent, no web);
- MCP: strict config with ONLY the context layer (fleetlens), read-only tools granted one by one;
- --setting-sources project: the operator's own hooks / CLAUDE.md never leak into an agent;
- the Investigate hook (writes confined to the agent's own files; one "wrap up" at the call budget);
- stream-json captured to logs/investigate.<label>.stream.jsonl; session id, cost, tool calls, time returned;
- live ticks to `on_event`: "start" (session id), "tool" (name + what it touched), "text", "end" — Progress
  turns them into logs/activity.log and one logs/agents/<stage>.<label>.<step>.log per agent (the cards
  /rfc-agent:rfc shows);
- hard timeout (process group killed);
- resume: continue a saved session (the lead across 1a → 1c → Draft; a tracer's one fix round).
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

from .. import observe
from ..mcp_tools import READONLY_TOOLS
from ..scope.context import SERVER

HOOK = Path(__file__).with_name("hook.py")

CONTEXT_TOOLS = READONLY_TOOLS[SERVER]
DENIED = "Bash,Agent,Task,WebFetch,WebSearch,NotebookEdit,TodoWrite,Artifact"


@dataclass
class AgentSpec:
    label: str  # lead | T1 | UI1 ...
    model: str
    effort: str | None
    builtin_tools: tuple[str, ...]  # e.g. ("Read", "Grep", "Glob", "Write", "Edit")
    write_allow: tuple[str, ...]  # run-dir-relative paths/prefixes this agent may write
    use_context: bool = True
    call_budget: int = 0  # 0 = no wrap-up nudge
    check_files: tuple[str, ...] = ()  # files whose citations the hook checks at write time
    timeout_s: int = 900


@dataclass
class AgentResult:
    label: str
    ok: bool
    session_id: str | None = None
    cost_usd: float = 0.0
    seconds: float = 0.0
    tool_calls: int = 0
    turns: int = 0
    error: str = ""
    final_text: str = ""
    stream: str = ""
    rate_limited: int = 0
    extra: dict = field(default_factory=dict)


def _mcp_config(run_dir: Path, cfg) -> Path | None:
    from ..scope.context import spec_of

    spec = spec_of(cfg)
    if not spec:
        return None
    p = run_dir / "logs" / "investigate.mcp.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"mcpServers": {SERVER: spec}}, indent=2))
    p.chmod(0o600)  # it carries the token
    return p


def _settings(run_dir: Path, label: str) -> Path:
    py = sys.executable
    p = run_dir / "logs" / f"investigate.{label}.settings.json"
    p.write_text(
        json.dumps(
            {
                "hooks": {
                    "PreToolUse": [
                        {
                            "matcher": "Write|Edit|MultiEdit|NotebookEdit",
                            "hooks": [{"type": "command", "command": f"{py} {HOOK} pre"}],
                        }
                    ],
                    "PostToolUse": [{"matcher": ".*", "hooks": [{"type": "command", "command": f"{py} {HOOK} post"}]}],
                }
            },
            indent=2,
        )
    )
    return p


def run(
    spec: AgentSpec,
    prompt: str,
    run_dir: Path,
    cfg,
    *,
    resume: str | None = None,
    on_event=None,
    stage: str = "investigate",
    step: str | None = None,
    trace_step: str | None = None,
) -> AgentResult:
    """Run (or resume) one agent to completion.
    `on_event(label, kind, data)` gets "start", "tool", "text" and "end" ticks.
    `trace_step` names the step on the Langfuse trace only (default: the log file's step).
    The stage's engine (`--engine` / AGENT.ENGINE, default claude-code) picks the backend: `axon` runs the
    same spec on the org's managed Pi (rfc_agent.axon_agent) and returns the same AgentResult."""
    from ..axon_agent import engine_for

    if engine_for(stage, cfg) == "axon":
        from .. import axon_agent

        return axon_agent.run(spec, prompt, run_dir, cfg, resume=resume, on_event=on_event, stage=stage,
                              step=step, trace_step=trace_step)
    run_dir = run_dir.resolve()
    binary = cfg.get("AGENT.CLAUDE_BIN", "claude") if cfg else "claude"
    if os.environ.get("RFC_AGENT_NO_REAL_CLAUDE") and Path(binary).name == "claude" and not Path(binary).is_file():
        raise RuntimeError(
            "test guard: refusing to start the real `claude` (RFC_AGENT_NO_REAL_CLAUDE is set) — "
            "stub get_config / AGENT.CLAUDE_BIN with a fake"
        )
    logs = run_dir / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    attempt = step or ("fix" if resume else "run")
    # every invocation gets its own log: the lead is resumed several times (1a fix, 1c, Draft, …)
    stream_path = logs / f"{stage}.{spec.label}.{attempt}.stream.jsonl"
    n = 2
    while stream_path.exists():
        stream_path = logs / f"{stage}.{spec.label}.{attempt}.{n}.stream.jsonl"
        n += 1
    counter = logs / f"investigate.{spec.label}.calls"
    if not resume:
        counter.write_text("0")
    cmd = [
        cfg.get("AGENT.CLAUDE_BIN", "claude") if cfg else "claude",
        "-p",
        "--output-format",
        "stream-json",
        "--verbose",
        "--permission-mode",
        "acceptEdits",
        "--setting-sources",
        "project",
        "--model",
        spec.model,
        "--tools",
        ",".join(spec.builtin_tools),
        "--disallowed-tools",
        DENIED,
        "--settings",
        str(_settings(run_dir, spec.label)),
    ]
    if spec.effort:
        cmd += ["--effort", spec.effort]
    mcp = _mcp_config(run_dir, cfg) if spec.use_context else None
    if mcp:
        cmd += [
            "--mcp-config",
            str(mcp),
            "--strict-mcp-config",
            "--allowedTools",
            ",".join(f"mcp__{SERVER}__{t}" for t in CONTEXT_TOOLS),
        ]
    else:
        empty = logs / "investigate.no-mcp.json"
        empty.write_text(json.dumps({"mcpServers": {}}))
        cmd += ["--strict-mcp-config", "--mcp-config", str(empty)]
    if resume:
        cmd += ["--resume", resume]
    env = {
        **os.environ,
        "INVESTIGATE_RUN_DIR": str(run_dir),
        "INVESTIGATE_WRITE_ALLOW": ",".join(spec.write_allow),
        "INVESTIGATE_CHECK_FILES": ",".join(spec.check_files),
        "INVESTIGATE_COUNTER": str(counter),
        "INVESTIGATE_CALL_BUDGET": str(spec.call_budget),
    }
    env.pop("CLAUDE_CODE_EFFORT_LEVEL", None)  # --effort decides

    res = AgentResult(label=spec.label, ok=False, stream=str(stream_path))
    trace = observe.agent_started(spec.label, stage=stage, step=trace_step or attempt, model=spec.model, prompt=prompt)
    try:
        _run_proc(cmd, prompt, run_dir, env, spec, stream_path, res, on_event, trace)
    finally:
        if on_event:
            on_event(spec.label, "end", {"ok": res.ok, "error": res.error, "session_id": res.session_id,
                                         "tool_calls": res.tool_calls, "seconds": res.seconds,
                                         "stage": stage, "step": attempt})
        if trace is not None:
            trace.finish(
                ok=res.ok,
                error=res.error or ("" if res.ok else "the agent did not finish"),
                model=res.extra.get("model") or spec.model,
                output=res.final_text,
                cost_usd=res.cost_usd,
                usage=res.extra.get("usage"),
                metadata={
                    "session_id": res.session_id,
                    "seconds": res.seconds,
                    "turns": res.turns,
                    "tool_calls": res.tool_calls,
                    "rate_limited": res.rate_limited,
                    "resumed": bool(resume),
                    "effort": spec.effort,
                    "stream": res.stream,
                },
            )
    return res


def _run_proc(cmd, prompt, run_dir, env, spec, stream_path, res, on_event, trace) -> None:
    t0 = time.monotonic()
    err_path = stream_path.with_name(stream_path.name.replace(".stream.jsonl", ".stderr"))
    err_f = open(err_path, "w", encoding="utf-8")
    proc = subprocess.Popen(
        cmd,
        cwd=run_dir,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=err_f,
        text=True,
        env=env,
        start_new_session=True,
    )
    try:
        proc.stdin.write(prompt)
        proc.stdin.close()
    except BrokenPipeError:
        pass
    deadline = t0 + spec.timeout_s
    timed_out = False
    with open(stream_path, "w", encoding="utf-8") as out:
        import selectors

        sel = selectors.DefaultSelector()
        sel.register(proc.stdout, selectors.EVENT_READ)
        buf = ""
        while True:
            if time.monotonic() > deadline:
                timed_out = True
                break
            if not sel.select(timeout=1.0):
                if proc.poll() is not None:
                    break
                continue
            chunk = proc.stdout.readline()
            if not chunk:
                if proc.poll() is not None:
                    break
                continue
            out.write(chunk)
            out.flush()
            buf = chunk.strip()
            _consume(buf, res, on_event, trace, run_dir)
    if timed_out:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            proc.kill()
        res.error = f"timed out after {spec.timeout_s}s"
    proc.wait(timeout=30)
    err_f.close()
    if not timed_out and proc.returncode not in (0, None) and not res.error:
        err = err_path.read_text(encoding="utf-8", errors="replace").strip().splitlines()
        res.error = f"exit {proc.returncode}: {err[-1][:300] if err else 'no stderr'}"
    res.seconds = round(time.monotonic() - t0, 1)
    if not res.error and res.extra.get("result_subtype") not in (None, "success"):
        res.error = f"ended with {res.extra.get('result_subtype')}"
    if not res.error and res.extra.get("result_is_error"):
        # claude refused the turn itself (usage limit, auth): say so instead of a silent "no file written"
        res.error = f"claude refused the request: {res.final_text[:300] or 'no reason given'}"
    res.ok = not res.error


def _consume(line: str, res: AgentResult, on_event, trace=None, run_dir: Path | None = None) -> None:
    try:
        ev = json.loads(line)
    except ValueError:
        return
    if trace is not None:
        trace.event(ev)
    t = ev.get("type")
    if t == "system" and ev.get("subtype") == "init":
        res.session_id = ev.get("session_id") or res.session_id
        res.extra["model"] = ev.get("model") or res.extra.get("model")
        res.extra["mcp"] = [(s.get("name"), s.get("status")) for s in ev.get("mcp_servers") or []]
        if on_event:
            log = Path(res.stream).name.removesuffix(".stream.jsonl") if res.stream else res.label
            on_event(res.label, "start", {"session_id": res.session_id, "model": res.extra["model"], "log": log})
    elif t == "assistant":
        for b in (ev.get("message") or {}).get("content") or []:
            if isinstance(b, dict) and b.get("type") == "text" and on_event and str(b.get("text") or "").strip():
                on_event(res.label, "text", {"text": str(b["text"]).strip()[:2000]})
            if isinstance(b, dict) and b.get("type") == "tool_use":
                res.tool_calls += 1
                if on_event:
                    on_event(res.label, "tool", {"n": res.tool_calls, "name": b.get("name"),
                                                 "target": target(b.get("input"), run_dir)})
    elif t == "result":
        res.session_id = ev.get("session_id") or res.session_id
        res.cost_usd += float(ev.get("total_cost_usd") or 0)
        res.turns = int(ev.get("num_turns") or 0)
        res.final_text = str(ev.get("result") or "")[:2000]
        res.extra["result_subtype"] = ev.get("subtype")
        res.extra["result_is_error"] = bool(ev.get("is_error"))
        res.extra["usage"] = ev.get("usage") or {}
    if "rate_limit" in line or '"overloaded' in line:
        res.rate_limited += 1


def target(inp, run_dir: Path | None = None, width: int = 90) -> str:
    """What a tool call touched, in one short line: the file, the pattern, or the MCP call's arguments."""
    if not isinstance(inp, dict):
        return ""
    path = inp.get("file_path") or inp.get("notebook_path")
    if path:
        out = _rel(path, run_dir)
    elif inp.get("pattern"):
        out = repr(str(inp["pattern"]))
        if inp.get("path"):
            out += " in " + _rel(inp["path"], run_dir)
    else:
        out = " ".join(f"{k}={v}" for k, v in inp.items() if isinstance(v, str | int | float) and str(v).strip())
    out = " ".join(str(out).split())
    return out if len(out) <= width else out[: width - 1] + "…"


def _rel(path, run_dir: Path | None) -> str:
    path = str(path)
    if run_dir:
        root = str(run_dir).rstrip("/") + "/"
        if path.startswith(root):
            path = path[len(root):]
    return path
