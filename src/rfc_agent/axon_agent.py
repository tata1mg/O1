"""The Axon backend for rfc-agent's agents: one headless `axon pi --mode json -p` per AgentSpec.

`investigate.agent.run` is the one place every stage starts an agent (lead, tracers, UI reader, drafter,
reviewers, fixers, summarizer). On the default engine it runs `claude -p` exactly as before; when the stage's
engine is `axon` it hands the same AgentSpec here and gets the same AgentResult back.

The trick that keeps the rest of the pipeline untouched: Pi's event stream is converted, line by line, into
the Claude stream-json shape (`PiToClaude`) and written to the SAME `logs/<stage>.<label>.<step>.stream.jsonl`
the Claude path writes. Every downstream reader — the live progress ticks (`agent._consume`), Langfuse tool
spans, `_code_reads`, the ledger — keeps reading Claude-shaped events and never learns an engine changed.
Pi's own stream is kept beside it as `<...>.pi.jsonl` for evidence.

Parity with the Claude invocation, piece by piece:
- built-in tools: `--tools` with the Pi spellings of spec.builtin_tools (no bash, no web);
- context layer: fleetlens registered for this one session by pi_ext/rfc-tools.js, its read-only tools
  granted by name (same `mcp__fleetlens__*` names the prompts use);
- the Investigate hook: the SAME hook.py, run by rfc-tools.js on tool_call (write allow-list) and tool_result
  (citation check + call budget, appended to the result the model reads);
- containment: pi_ext/harness-guard.js → `python -m harness.policy` (writes confined to the run dir);
- hermetic: no AGENTS.md/CLAUDE.md, skills, prompt templates or project trust from the analysed repos;
- resume: a session id minted here (`--session-id`), continued with `--session <file>`;
- the prompt goes on stdin, as with Claude (Pi prepends piped stdin to the first prompt).
"""

from __future__ import annotations

import json
import os
import sys
import time
import uuid
from pathlib import Path

from harness.engines.axon import RFC_TOOLS_JS, map_model, thinking_for, write_tools_config
from harness.engines.pi import (
    CLAUDE_TO_PI_TOOLS,
    GUARD_JS,
    HERMETIC_FLAGS,
    _assert_known_flags,
    _kill_group,
    _pgid_of,
    _read_lines,
    _session_file,
    policy_cmd,
)
from harness.engines.pi_translate import session_usage_cost

ENGINES = ("claude-code", "axon")
DEFAULT_ENGINE = "claude-code"
ENGINE_ENV = "RFC_AGENT_ENGINE"  # set by `rfc --engine` (and each stage CLI); beats config AGENT.ENGINE
STAGE_ENGINES_ENV = "RFC_AGENT_STAGE_ENGINES"  # JSON {stage: engine}, set by --stage-engine
_SRC = Path(__file__).resolve().parents[1]  # src/ — absolute, for the guard's python


# ---- engine selection ------------------------------------------------------------------------------


def stage_engines(cfg) -> dict[str, str]:
    """{stage: engine}: config AGENT.STAGE_ENGINES, with --stage-engine (env) on top."""
    out = dict((cfg.get("AGENT.STAGE_ENGINES", {}) if cfg else {}) or {})
    raw = os.environ.get(STAGE_ENGINES_ENV, "")
    if raw:
        try:
            out.update(json.loads(raw))
        except ValueError:
            raise ValueError(f"{STAGE_ENGINES_ENV} is not JSON: {raw[:120]!r}") from None
    return {str(k): str(v) for k, v in out.items()}


def engine_for(stage: str, cfg) -> str:
    """The engine a stage's agents run on: per-stage override > run engine > claude-code."""
    name = stage_engines(cfg).get(stage) or os.environ.get(ENGINE_ENV) or (
        cfg.get("AGENT.ENGINE") if cfg else None) or DEFAULT_ENGINE
    if name not in ENGINES:
        raise ValueError(f"unknown engine {name!r} for stage {stage!r}; known: {', '.join(ENGINES)}")
    return name


def parse_stage_engines(items: list[str] | None) -> dict[str, str]:
    """`--stage-engine review=axon` (repeatable, or comma lists) -> {"review": "axon"}."""
    out: dict[str, str] = {}
    for item in items or []:
        for part in str(item).split(","):
            part = part.strip()
            if not part:
                continue
            stage, sep, engine = part.partition("=")
            if not sep or not stage.strip() or engine.strip() not in ENGINES:
                raise ValueError(f"--stage-engine wants <stage>=<{'|'.join(ENGINES)}>, got {part!r}")
            out[stage.strip()] = engine.strip()
    return out


def apply_engine_args(engine: str | None, stage_items: list[str] | None) -> None:
    """Export the CLI choice for every agent this process starts (the stages run in-process)."""
    if engine:
        if engine not in ENGINES:
            raise ValueError(f"--engine must be one of {', '.join(ENGINES)}, got {engine!r}")
        os.environ[ENGINE_ENV] = engine
    extra = parse_stage_engines(stage_items)
    if extra:
        cur = {}
        if os.environ.get(STAGE_ENGINES_ENV):
            try:
                cur = json.loads(os.environ[STAGE_ENGINES_ENV])
            except ValueError:
                cur = {}
        os.environ[STAGE_ENGINES_ENV] = json.dumps({**cur, **extra})


def settings(cfg) -> dict:
    """AGENT.AXON from config: BIN, PROVIDER, MODELS (tier/id -> gateway id), THRESHOLD_COMPACTION."""
    s = (cfg.get("AGENT.AXON", {}) if cfg else {}) or {}
    return {
        "bin": str(s.get("BIN") or os.environ.get("AXON_BIN") or "axon"),
        "provider": str(s.get("PROVIDER") or "litellm"),
        "models": dict(s.get("MODELS") or {}),
        "threshold_compaction": bool(s.get("THRESHOLD_COMPACTION", False)),
    }


def gateway_model(model: str | None, cfg) -> str | None:
    return map_model(model, settings(cfg)["models"])


# ---- Pi stream -> Claude stream-json ---------------------------------------------------------------

# Pi built-in tool -> the Claude name downstream readers key on (e.g. `_code_reads` counts Read/Grep/Glob)
PI_TO_CLAUDE_TOOL = {"read": "Read", "write": "Write", "edit": "Edit", "multiedit": "MultiEdit",
                     "grep": "Grep", "find": "Glob", "ls": "LS", "bash": "Bash"}
_BAD_STOPS = ("error", "aborted")


def claude_tool_input(tool: str, args, cwd: Path | None) -> dict:
    """Pi tool arguments -> Claude's input keys (`path` -> absolute `file_path` for file tools)."""
    if not isinstance(args, dict):
        return {}
    out = dict(args)
    t = tool.lower()
    if t in ("read", "write", "edit", "multiedit") and "path" in out:
        p = str(out.pop("path"))
        out["file_path"] = str((cwd / p) if cwd and not os.path.isabs(p) else Path(p))
    elif t in ("grep", "find", "ls") and out.get("path") and cwd and not os.path.isabs(str(out["path"])):
        out["path"] = str(cwd / str(out["path"]))
    return out


def _text_of(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(str(b.get("text") or "") for b in content if isinstance(b, dict) and b.get("type") == "text")
    return ""


class PiToClaude:
    """Feed Pi `--mode json` events, get Claude stream-json events back (a list per input event)."""

    def __init__(self, *, session_id: str, model: str | None, cwd: Path | None = None, mcp: list[str] | None = None):
        self.session_id = session_id
        self.model = model
        self.cwd = cwd
        self.mcp = list(mcp or [])
        self.cost = 0.0
        self.turns = 0
        self.final_text = ""
        self.usage = {"input_tokens": 0, "output_tokens": 0, "cache_read_input_tokens": 0,
                      "cache_creation_input_tokens": 0}
        self.settled = False
        self.error = ""
        self._init_sent = False

    def _init(self) -> dict:
        self._init_sent = True
        return {"type": "system", "subtype": "init", "session_id": self.session_id, "model": self.model,
                "engine": "axon", "mcp_servers": [{"name": n, "status": "registered"} for n in self.mcp]}

    def feed(self, ev: dict) -> list[dict]:
        t = ev.get("type")
        out: list[dict] = []
        if t == "session":
            self.session_id = ev.get("id") or self.session_id
            return [self._init()]
        if not self._init_sent and t in ("agent_start", "message_end"):
            out.append(self._init())
        if t == "message_end":
            msg = ev.get("message") or {}
            role = msg.get("role")
            if role == "assistant":
                out.extend(self._assistant(msg))
            elif role == "toolResult":
                out.append({"type": "user", "message": {"role": "user", "content": [{
                    "type": "tool_result", "tool_use_id": msg.get("toolCallId"),
                    "content": _text_of(msg.get("content")), "is_error": bool(msg.get("isError"))}]}})
        elif t == "auto_retry_end" and ev.get("success") is False:
            self.error = f"provider retries exhausted: {str(ev.get('finalError') or '')[:300]}"
        elif t == "agent_settled":
            self.settled = True
            if ev.get("aborted"):
                self.error = self.error or "the agent was aborted"
        return out

    def _assistant(self, msg: dict) -> list[dict]:
        self.turns += 1
        self.model = msg.get("model") or self.model
        u = msg.get("usage") or {}
        self.cost += float(((u.get("cost") or {}).get("total")) or 0)
        self.usage["input_tokens"] += int(u.get("input") or 0)
        self.usage["output_tokens"] += int(u.get("output") or 0)
        self.usage["cache_read_input_tokens"] += int(u.get("cacheRead") or 0)
        self.usage["cache_creation_input_tokens"] += int(u.get("cacheWrite") or 0)
        stop = str(msg.get("stopReason") or "")
        if stop in _BAD_STOPS:
            self.error = f"assistant stopReason={stop}: {str(msg.get('errorMessage') or '')[:300]}".rstrip(": ")
        content = []
        for b in msg.get("content") or []:
            if not isinstance(b, dict):
                continue
            if b.get("type") == "text" and str(b.get("text") or "").strip():
                content.append({"type": "text", "text": b["text"]})
                self.final_text = b["text"]
            elif b.get("type") == "toolCall":
                name = str(b.get("name") or "")
                content.append({"type": "tool_use", "id": b.get("id"), "name": PI_TO_CLAUDE_TOOL.get(name, name),
                                "input": claude_tool_input(name, b.get("arguments"), self.cwd)})
        if not content:
            return []
        return [{"type": "assistant", "message": {"model": self.model, "content": content}}]

    def result(self, cost: float | None = None, error: str = "") -> dict:
        """The terminal Claude `result` event. A stream without `agent_settled` did not finish."""
        err = error or self.error or ("" if self.settled else "the Pi stream ended without agent_settled "
                                                             "(crashed, killed or truncated)")
        return {"type": "result", "subtype": "success" if not err else "error_during_execution",
                "is_error": bool(err), "session_id": self.session_id, "engine": "axon", "model": self.model,
                "total_cost_usd": round(self.cost if cost is None else cost, 6), "num_turns": self.turns,
                "result": err or self.final_text, "usage": dict(self.usage)}


# ---- one agent ---------------------------------------------------------------------------------------


def pi_tools(spec_tools, mcp_tools: list[str]) -> list[str]:
    out: list[str] = []
    for t in spec_tools or ():
        name = CLAUDE_TO_PI_TOOLS.get(t, t.lower())
        if name not in out:
            out.append(name)
    if any(t in out for t in ("read", "grep", "find")) and "ls" not in out:
        out.append("ls")  # Claude's Read lists directories; Pi splits that into its own tool
    return out + [t for t in mcp_tools if t not in out]


def build_argv(*, bin: str, provider: str, model: str | None, thinking: str | None, tools: list[str],
               session_dir: Path, session_id: str, resume_file: Path | None, name: str) -> list[str]:
    argv = [bin, "pi", "--mode", "json", "-p", *HERMETIC_FLAGS, "-e", str(GUARD_JS), "-e", str(RFC_TOOLS_JS)]
    if provider:
        argv += ["--provider", provider]
    if model:
        argv += ["--model", model]
    if thinking:
        argv += ["--thinking", thinking]
    argv += ["--tools", ",".join(tools)] if tools else ["--no-tools"]
    argv += ["--session-dir", str(session_dir)]
    argv += ["--session", str(resume_file)] if resume_file else ["--session-id", session_id]
    argv += ["--name", name]
    _assert_known_flags(argv[1:])  # argv[0] is the launcher; Pi ignores unknown flags silently
    return argv


def build_env(*, run_dir: Path, spec, counter: Path, tools_cfg: Path, denials: Path, granted: list[str],
              hook_env: dict[str, str]) -> dict[str, str]:
    env = {**os.environ, **hook_env}
    env["RFC_TOOLS_CONFIG"] = str(tools_cfg)
    env["HARNESS_POLICY_CMD"] = json.dumps(policy_cmd())
    env["HARNESS_POLICY_JSON"] = json.dumps({
        "cwd": str(run_dir), "writable_roots": [str(run_dir)], "readable_roots": [], "readonly_shell": True,
        "disabled_tools": [], "granted_tools": granted,
    })
    env["HARNESS_DENIALS_PATH"] = str(denials)
    env["HARNESS_DENIALS_ECHO"] = "0"
    # the guard and hook run `python -m harness.policy` / hook.py from the run dir: the package must import there
    parts = [str(_SRC)] + [p for p in (env.get("PYTHONPATH") or "").split(os.pathsep) if p]
    env["PYTHONPATH"] = os.pathsep.join(str(Path(p).resolve()) for p in parts)
    for k in ("CLAUDE_CODE_EFFORT_LEVEL", "PI_OFFLINE", "PI_CODING_AGENT_DIR"):
        env.pop(k, None)
    return env


def run(spec, prompt: str, run_dir: Path, cfg, *, resume: str | None = None, on_event=None,
        stage: str = "investigate", step: str | None = None, trace_step: str | None = None):
    """`investigate.agent.run` on Axon. Same arguments, same AgentResult, same log files."""
    from . import observe
    from .investigate import agent as ag

    run_dir = run_dir.resolve()
    s = settings(cfg)
    if os.environ.get("RFC_AGENT_NO_REAL_CLAUDE") and Path(s["bin"]).name == "axon" and not Path(s["bin"]).is_file():
        raise RuntimeError("test guard: refusing to start the real `axon` (RFC_AGENT_NO_REAL_CLAUDE is set) — "
                           "set AGENT.AXON.BIN to a fake")
    logs = run_dir / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    attempt = step or ("fix" if resume else "run")
    stream_path = logs / f"{stage}.{spec.label}.{attempt}.stream.jsonl"
    n = 2
    while stream_path.exists():
        stream_path = logs / f"{stage}.{spec.label}.{attempt}.{n}.stream.jsonl"
        n += 1
    base = stream_path.name.removesuffix(".stream.jsonl")
    counter = logs / f"investigate.{spec.label}.calls"
    if not resume:
        counter.write_text("0")

    session_dir = logs / "pi-sessions"
    session_dir.mkdir(parents=True, exist_ok=True)
    session_id = resume or str(uuid.uuid4())
    resume_file = None
    res = ag.AgentResult(label=spec.label, ok=False, stream=str(stream_path))
    if resume:
        found = _session_file(session_dir, resume)
        if not found.exists():
            # the Claude path fails the same way when a session is gone; every caller then reruns cold
            res.error = f"axon: no saved Pi session {resume} under {session_dir} — cannot resume"
            stream_path.write_text("", encoding="utf-8")
            return res
        resume_file = found

    from .scope.context import spec_of

    mcp_spec = spec_of(cfg) if spec.use_context else None
    granted = [f"mcp__{ag.SERVER}__{t}" for t in ag.CONTEXT_TOOLS] if mcp_spec else []
    tools_cfg = write_tools_config(
        logs / f"{base}.rfc-tools.json",
        mcp={ag.SERVER: mcp_spec} if mcp_spec else {},
        hook_cmd=[sys.executable, str(ag.HOOK)],
        threshold_compaction=s["threshold_compaction"],
    )
    model = gateway_model(spec.model, cfg)
    argv = build_argv(bin=s["bin"], provider=s["provider"], model=model, thinking=thinking_for(spec.effort),
                      tools=pi_tools(spec.builtin_tools, granted), session_dir=session_dir,
                      session_id=session_id, resume_file=resume_file, name=f"{stage}.{spec.label}.{attempt}")
    env = build_env(run_dir=run_dir, spec=spec, counter=counter, tools_cfg=tools_cfg,
                    denials=logs / f"{base}.denials.jsonl", granted=granted, hook_env={
                        "INVESTIGATE_RUN_DIR": str(run_dir),
                        "INVESTIGATE_WRITE_ALLOW": ",".join(spec.write_allow),
                        "INVESTIGATE_CHECK_FILES": ",".join(spec.check_files),
                        "INVESTIGATE_COUNTER": str(counter),
                        "INVESTIGATE_CALL_BUDGET": str(spec.call_budget),
                    })

    trace = observe.agent_started(spec.label, stage=stage, step=trace_step or attempt, model=model, prompt=prompt)
    shim = PiToClaude(session_id=session_id, model=model, cwd=run_dir, mcp=[ag.SERVER] if mcp_spec else [])
    try:
        _run_proc(argv, prompt, run_dir, env, spec, stream_path, res, shim, session_dir, on_event, trace,
                  fresh=resume_file is None)
    finally:
        if on_event:
            on_event(spec.label, "end", {"ok": res.ok, "error": res.error, "session_id": res.session_id,
                                         "tool_calls": res.tool_calls, "seconds": res.seconds,
                                         "stage": stage, "step": attempt})
        if trace is not None:
            trace.finish(
                ok=res.ok,
                error=res.error or ("" if res.ok else "the agent did not finish"),
                model=res.extra.get("model") or model,
                output=res.final_text,
                cost_usd=res.cost_usd,
                usage=res.extra.get("usage"),
                metadata={"session_id": res.session_id, "seconds": res.seconds, "turns": res.turns,
                          "tool_calls": res.tool_calls, "rate_limited": res.rate_limited, "resumed": bool(resume),
                          "effort": spec.effort, "stream": res.stream, "engine": "axon",
                          "requested_model": spec.model},
            )
    return res


def _run_proc(argv, prompt, run_dir, env, spec, stream_path, res, shim: PiToClaude, session_dir, on_event,
              trace, *, fresh: bool = True) -> None:
    import subprocess

    from .investigate import agent as ag

    t0 = time.monotonic()
    raw_path = stream_path.with_name(stream_path.name.replace(".stream.jsonl", ".pi.jsonl"))
    err_path = stream_path.with_name(stream_path.name.replace(".stream.jsonl", ".stderr"))
    deadline = t0 + spec.timeout_s
    timed_out = False
    proc = None
    pgid = None

    def emit(out, ev: dict) -> None:
        line = json.dumps(ev, ensure_ascii=False)
        out.write(line + "\n")
        out.flush()
        ag._consume(line, res, on_event, trace, run_dir)

    with open(err_path, "w", encoding="utf-8") as err, open(stream_path, "w", encoding="utf-8") as out, \
            open(raw_path, "w", encoding="utf-8") as raw:
        try:
            proc = subprocess.Popen(argv, cwd=run_dir, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=err,
                                    env=env, start_new_session=True)
        except FileNotFoundError:
            res.error = (f"`{argv[0]}` not found — install Axon (curl --proto '=https' --tlsv1.2 -fsSL "
                         f"https://deputydev.ai/axon/install.sh | sh) or set AGENT.AXON.BIN")
            emit(out, shim.result(error=res.error))
            res.seconds = round(time.monotonic() - t0, 1)
            return
        pgid = _pgid_of(proc)
        try:
            proc.stdin.write(prompt.encode("utf-8"))
            proc.stdin.close()  # Pi reads piped stdin to EOF before it starts: it must be closed
        except BrokenPipeError:
            pass
        try:
            for line in _read_lines(proc, deadline):
                if line is None:
                    timed_out = True
                    break
                raw.write(line)
                raw.flush()
                text = line.strip()
                if not text:
                    continue
                try:
                    ev = json.loads(text)
                except ValueError:
                    continue
                for cev in shim.feed(ev):
                    emit(out, cev)
        finally:
            _kill_group(proc, pgid)
        stderr_tail = ""
        try:
            stderr_tail = err_path.read_text(encoding="utf-8", errors="replace")[-2000:]
        except OSError:
            pass
        error = ""
        if timed_out:
            error = f"timed out after {spec.timeout_s}s"
        elif "Extension error" in stderr_tail:
            error = ("a harness extension errored under Pi (tools fail safe to BLOCKED) — see "
                     f"{err_path.name}: {stderr_tail.strip().splitlines()[-1][:300]}")
        elif not shim.settled and not shim.error:
            last = stderr_tail.strip().splitlines()[-1][:300] if stderr_tail.strip() else "no stderr"
            error = f"axon exited {proc.returncode} without finishing: {last}"
        # the session file is authoritative (survives a torn pipe, includes compaction spend) but holds EVERY
        # turn of the session: on a resume it would re-bill the earlier invocations, so there the stream sum
        # of this invocation's messages is the honest number
        cost = session_usage_cost(_session_file(session_dir, shim.session_id)) if fresh else None
        total = float(cost.get("total")) if isinstance(cost, dict) and cost.get("total") is not None else None
        emit(out, shim.result(cost=total, error=error))
    res.seconds = round(time.monotonic() - t0, 1)
    res.session_id = shim.session_id
    res.extra["engine"] = "axon"
    if not res.error and res.extra.get("result_is_error"):
        res.error = str(res.final_text or "the agent failed")[:400]
    res.ok = not res.error


def add_engine_args(p) -> None:
    """`--engine` / `--stage-engine` on the pipeline and on every stage CLI."""
    p.add_argument("--engine", choices=ENGINES,
                   help="agent engine for every stage (default: config AGENT.ENGINE, else claude-code)")
    p.add_argument("--stage-engine", action="append", metavar="STAGE=ENGINE",
                   help="engine for one stage, e.g. review=axon (repeatable; beats --engine)")


# ---- doctor ----------------------------------------------------------------------------------------

# every model the pipeline's stages name by default (investigate/config.py, review, summarize, scope)
_STAGE_MODELS = ("claude-opus-5-5[1m]", "claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-4-5-20251001")


def list_models(bin: str, timeout: int = 120) -> set[str] | None:
    """Gateway model ids `axon pi --list-models` reports, or None when it cannot be read."""
    import subprocess

    try:
        out = subprocess.run([bin, "pi", "--list-models"], capture_output=True, text=True, timeout=timeout,
                             stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return None
    ids = set()
    for line in (out.stdout or "").splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[0] != "provider":
            ids.add(parts[1])
    return ids or None


def doctor(cfg) -> list[str]:
    """Preflight for `--engine axon`: the launcher (installed, signed in, managed Pi >= 1.4), every mapped model
    exists on the gateway, and the context layer answers. [] = ready. Each problem names its fix."""
    from harness.engines.axon import AxonEngine

    from .scope.context import health, spec_of

    s = settings(cfg)
    problems = AxonEngine(bin=s["bin"]).doctor()
    if problems and "not found" in problems[0]:
        return problems
    models = list_models(s["bin"])
    if models is None:
        problems.append("could not list gateway models (`axon pi --list-models`) — check `axon auth status`, "
                        "the VPN/Zscaler notes in the Axon user guide, then `axon doctor`")
    else:
        for m in sorted({gateway_model(x, cfg) for x in _STAGE_MODELS}):
            if m not in models:
                problems.append(f"model {m!r} is not on the gateway (available: {', '.join(sorted(models))}) — "
                                f"set config AGENT.AXON.MODELS to one of those")
    ok, why = health(spec_of(cfg))
    if not ok:
        problems.append(f"fleetlens (the context layer): {why}")
    return problems


def main(argv: list[str] | None = None) -> int:
    import argparse

    from .config import get_config

    p = argparse.ArgumentParser(prog="python -m rfc_agent.axon_agent")
    p.add_argument("command", choices=("doctor", "models"))
    args = p.parse_args(argv)
    cfg = get_config()
    if args.command == "models":
        for m in _STAGE_MODELS:
            print(f"{m:32} -> {gateway_model(m, cfg)}")
        return 0
    problems = doctor(cfg)
    for line in problems:
        print(f"FAIL  {line}")
    print("axon: READY" if not problems else f"axon: BLOCKED ({len(problems)} problem(s))")
    return 0 if not problems else 1


if __name__ == "__main__":
    sys.exit(main())
