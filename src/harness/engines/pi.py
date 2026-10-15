"""Pi engine adapter — `pi --mode json -p` as one headless harness invocation (D7, engine offshoot A).

Pi (`@earendil-works/pi-coding-agent`, 0.85.x) is the second engine behind `harness.engines.Engine`. It is
driven exactly the way the Claude adapter is driven: spawn a child in its own process group, read JSONL
off stdout, translate into the canonical trace (`harness.events`), account cost, kill the group on timeout.

WHY subprocess `--mode json` and not Pi's RPC mode or its TypeScript SDK: our stages are one-shot (system
prompt in, artifacts + trace out), so RPC's persistent bidirectional session buys a second framing
implementation and a request/response correlation layer for zero new capability, and a Node shim would add
a build artifact plus a second place for tool policy to live. Revisit RPC only if a stage must ask a human
mid-run — `--mode json` sets `ctx.hasUI === false` and extension dialogs are no-ops there.

THREE SHARP EDGES of pi 0.85.1 that this adapter exists to absorb (research report 04):

1. `pi --mode json` EXITS 0 EVEN WHEN THE AGENT ERRORED OR WAS ABORTED — its exit-code branch is only
   wired for `--mode text`. Success is therefore derived from the event stream (`agent_settled` terminal,
   plus stopReason / retry / compaction signals) by `pi_translate`, never from the return code.
2. THERE IS NO TERMINAL COST EVENT. Per-message `Usage.cost` is richer than Claude's single total, but
   nobody adds it up. We sum the session JSONL after exit (authoritative: survives a truncated pipe and
   includes compaction spend) and fall back to the in-stream sum.
3. UNKNOWN FLAGS ARE SILENTLY IGNORED, not rejected — Pi collects them for extensions to claim. A typo in
   a `--no-*` flag would disable nothing and the run would proceed unhermetically. `_assert_known_flags`
   turns that class of bug into a loud failure on OUR side of the boundary.

Containment (D6): Pi ships no permission system at all. The policy lives once in `harness.policy`, and the
transport is `pi_ext/harness-guard.js`, loaded with `--no-extensions -e <path>` (a CLI `-e` extension loads
BEFORE the project-trust decision, so the guard is active even in an untrusted repo). The guard shells out
to `python -m harness.policy check`. See pi_ext/README.md for the contract and the known gaps.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import select
import shutil
import signal
import subprocess
import sys
import time
import uuid
from pathlib import Path

from .. import flags as F
from ..contracts import EngineFailure, PipelineConfigError
from ..events import EventWriter, TraceSummary, summarize
from . import Containment, EngineRequest, EngineResult
from .pi_translate import Translator, session_usage_cost

_EXT_DIR = Path(__file__).parent / "pi_ext"
GUARD_JS = _EXT_DIR / "harness-guard.js"

# Every Pi flag this adapter is allowed to emit, pinned to the researched version. Pi does not reject
# unknown flags, so this list is the only thing standing between a typo and a silently unhermetic run.
KNOWN_FLAGS: frozenset[str] = frozenset(
    {
        "--mode", "-p", "--print", "--provider", "--model", "--thinking", "--api-key",
        "--session", "--session-id", "--session-dir", "--no-session", "--name", "--fork", "--continue",
        "--tools", "--exclude-tools", "--no-builtin-tools", "--no-tools",
        "-e", "--extension", "--no-extensions", "--skill", "--no-skills",
        "--prompt-template", "--no-prompt-templates", "--no-themes", "--no-context-files",
        "--system-prompt", "--append-system-prompt", "--approve", "--no-approve", "--offline",
        "--version", "--help", "--",
    }
)

# The `--no-*` set that makes a stage hermetic: no discovered extension, skill, prompt template, theme or
# AGENTS.md/CLAUDE.md, and no trust in the analysed repo's own `.pi/` (which could otherwise inject a
# system prompt into our stage — the prompt-injection vector `--strict-mcp-config` taught us to close).
HERMETIC_FLAGS: tuple[str, ...] = (
    "--no-approve", "--no-skills", "--no-prompt-templates", "--no-themes", "--no-context-files",
)

# Pi's seven thinking levels (richer than Claude's effort knob). Anything else is refused rather than
# silently dropped, because an unknown value would be swallowed by Pi's unknown-flag handling.
THINKING_LEVELS: frozenset[str] = frozenset({"off", "minimal", "low", "medium", "high", "xhigh", "max"})

# Claude-native tool spellings -> Pi's. A Containment authored for the Claude adapter (which is where they
# all come from today) then still means something here instead of allowlisting nothing.
CLAUDE_TO_PI_TOOLS: dict[str, str] = {
    "Read": "read", "Write": "write", "Edit": "edit", "MultiEdit": "edit",
    "Bash": "bash", "Grep": "grep", "Glob": "find", "LS": "ls", "NotebookRead": "read",
}

# Provider env vars doctor() looks for. Presence only — never the value.
PROVIDER_ENV_VARS: tuple[str, ...] = (
    "ANTHROPIC_API_KEY", "ANTHROPIC_OAUTH_TOKEN", "OPENAI_API_KEY", "GEMINI_API_KEY", "OPENROUTER_API_KEY",
)

# Engine-neutral env keys carrying the three policy fields only a workflow can know. A workflow exports
# them for its stages (or sets the `pi.policy_extra` flag); the kernel never derives them, because naming
# a workflow's broker script or package here would be exactly the layering violation D2/D3 forbids.
POLICY_EXTRA_ENV: dict[str, str] = {
    "broker_hint": "HARNESS_POLICY_BROKER_HINT",
    "repos_root": "HARNESS_POLICY_REPOS_ROOT",
    "exempt_marker": "HARNESS_POLICY_EXEMPT_MARKER",
}


_VERSION_RE = re.compile(r"(\d+)\.(\d+)\.(\d+)")
_POLL_S = 0.5  # watchdog tick; also the denial-drain cadence


# ---- flags -----------------------------------------------------------------------------------------
# Every knob is a Flag, never a bare env read (D5). All are engine-scoped and owned by "pi".
def _f(name: str, type_: type, default, help_: str) -> None:
    F.define(F.Flag(f"pi.{name}", type_, default, help_, F.Scope.ENGINE, owner="pi"))


_f("bin", str, "pi", "Path to the `pi` executable (0.85.x). Install: "
   "npm install -g --ignore-scripts @earendil-works/pi-coding-agent")
_f("node_bin", str, "node", "Node executable used only by `doctor()` to check pi's runtime requirement.")
_f("min_version", str, "0.85.0", "Minimum pi version doctor() accepts (the researched version is 0.85.1).")
_f("min_node", str, "22.19.0", "Minimum Node version pi 0.85.x declares in engines.node.")
_f("provider", str, "", "--provider for every invocation ('' = omit). NOTE pi's own default provider is "
   "google, so leaving both this and the model empty does NOT give you Anthropic.")
_f("thinking", str, "", "Default --thinking level when the request sets no effort "
   "(off|minimal|low|medium|high|xhigh|max; '' = omit).")
_f("hermetic", bool, True, f"Pass {' '.join(HERMETIC_FLAGS)} so a stage inherits nothing from the machine "
   "or the analysed repo. Turning this OFF lets the target repo inject a system prompt — benchmarks only.")
_f("guard", bool, True, "Load pi_ext/harness-guard.js as the containment transport. OFF removes ALL "
   "containment (pi has no permission system of its own) — benchmarks only.")
_f("guard_path", str, "", "Override the guard extension path ('' = the shipped harness-guard.js; pi 0.85.1 "
   "loads a plain .js extension — measured). Point it at the sibling harness-guard.ts if that ever changes.")
_f("denials_echo", bool, False, "Also have the guard console.log each denial. In --mode json pi routes "
   "extension console output to STDERR, so the echo lands in logs/<label>.pi.stderr, never in the event "
   "stream; the JSONL side file is always written and is what the translator reads.")
_f("offline", bool, True, "Set PI_OFFLINE=1 + PI_SKIP_VERSION_CHECK=1 so no stage run phones pi.dev.")
_f("telemetry", bool, False, "Allow pi's install/update telemetry and provider attribution headers.")
_f("private_home", bool, True, "Give each run a private PI_CODING_AGENT_DIR (<cwd>/.pi-home) so no "
   "inherited auth.json/settings.json can override the run — auth.json otherwise beats the environment.")
_f("compaction", bool, False, "Allow pi's auto-compaction. Default OFF: compaction is lossy, and a stage "
   "that silently summarises its evidence away makes the witness gate lie. Off = overflow fails loudly.")
_f("policy_extra", dict, {}, "Policy fields only a workflow knows, merged into the guard's request: "
   "{\"broker_hint\": \"<python> <broker.py>\", \"repos_root\": \"/abs\", \"exempt_marker\": \"<pkg>\"}. "
   "Without them a pinned broker is not exempt and the policy's teaching strings name no alternative. "
   f"Also readable from the environment as {sorted(POLICY_EXTRA_ENV.values())}.")
_f("session_dir", str, "logs/pi-sessions", "Session storage dir, relative to the run dir. Private per run, "
   "so nothing lands in the operator's ~/.pi and the session path is known before launch.")


class PiEngine:
    """The `pi` adapter. Construct via `harness.engines.get_engine("pi", ...)`."""

    name = "pi"

    def __init__(self, bin: str | None = None, node_bin: str | None = None, flags: object | None = None,
                 **_ignored) -> None:
        self.flags = flags
        self.bin = bin or self._flag("pi.bin")
        self.node_bin = node_bin or self._flag("pi.node_bin")

    def _flag(self, name: str):
        """Flag value for this run: the resolved Flags object when the runner passed one, else the
        registered default (so a directly-constructed engine in a test behaves like a default run)."""
        if self.flags is not None:
            with contextlib.suppress(KeyError, AttributeError):
                return self.flags.get(name)  # type: ignore[attr-defined]
        return F.registry()[name].default

    # ---- protocol ----------------------------------------------------------------------------------

    def supports_resume(self) -> bool:
        """True: Pi's headless resume primitive is `--session <path|partial-id>` against a session id we
        minted ourselves with `--session-id`, in a session dir we own. (Its `-r`/`--resume` flag is an
        INTERACTIVE picker and is never used here.)"""
        return True

    def doctor(self) -> list[str]:
        problems: list[str] = []
        exe = shutil.which(self.bin) or (self.bin if os.path.isabs(self.bin) and os.access(self.bin, os.X_OK) else None)
        if not exe:
            problems.append(
                f"`{self.bin}` not found on PATH — install pi with "
                f"`npm install -g --ignore-scripts @earendil-works/pi-coding-agent`, or set --set pi.bin=<path>"
            )
        else:
            got = _version_of([exe, "--version"])
            want = str(self._flag("pi.min_version"))
            if got is None:
                problems.append(f"`{self.bin} --version` printed no parseable version — cannot verify >= {want}")
            elif _older(got, want):
                problems.append(f"pi {got} is older than {want} — upgrade with `pi update --self`")
        node = shutil.which(self.node_bin)
        if not node:
            problems.append(f"`{self.node_bin}` not found on PATH — pi 0.85.x requires Node "
                            f">= {self._flag('pi.min_node')}")
        else:
            got = _version_of([node, "--version"])
            want = str(self._flag("pi.min_node"))
            if got is None:
                problems.append(f"`{self.node_bin} --version` printed no parseable version")
            elif _older(got, want):
                problems.append(
                    f"Node {got} is older than pi's requirement {want} — pi 0.85.x declares "
                    f"engines.node >= {want} (npm dist-tag `legacy-node20` pins 0.74.2 for Node 20)"
                )
        if not self._auth_present():
            problems.append(
                "no provider credential found — set one of " + ", ".join(PROVIDER_ENV_VARS) +
                ", or run `pi` once and `/login`. (Credential VALUES are never read or printed here.)"
            )
        return problems

    def _auth_present(self) -> bool:
        """Is SOME provider credential reachable? Keys and tokens are never read, only counted.

        Existence of `auth.json` is not enough: pi creates it as an empty `{}` on its first run (observed
        2026-09-08 after a bare `--version` probe), so an empty file would report a healthy run that then
        fails at the first provider call.
        """
        if any(os.environ.get(v) for v in PROVIDER_ENV_VARS):
            return True
        home = os.environ.get("PI_CODING_AGENT_DIR")
        candidates = [Path(home) / "auth.json"] if home else []
        candidates.append(Path.home() / ".pi" / "agent" / "auth.json")
        for path in candidates:
            if not path.exists():
                continue
            try:
                creds = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if isinstance(creds, dict) and creds:
                return True  # at least one provider entry; its VALUE is never touched
        return False

    def run(self, req: EngineRequest) -> EngineResult:
        started = time.monotonic()
        if not req.timeout_s or req.timeout_s <= 0:
            # inventing a deadline here used to kill every stage instantly with "timed out after 0s"
            raise PipelineConfigError(
                f"stage '{req.label}': timeout_s must be a positive number of seconds (got "
                f"{req.timeout_s!r}) — set the stage's timeout_s or the `timeout_s` flag")
        cwd = Path(req.cwd)
        logs = cwd / "logs"
        logs.mkdir(parents=True, exist_ok=True)
        raw_path = req.raw_trace_path or logs / f"{req.label}.pi.jsonl"
        err_path = logs / f"{req.label}.pi.stderr"
        denials_path = logs / f"{req.label}.denials.jsonl"
        session_dir = cwd / str(self._flag("pi.session_dir"))
        session_dir.mkdir(parents=True, exist_ok=True)
        denials_path.touch()  # the guard appends; the translator tails. Exists from the start = no race.

        session_id = req.resume_session or str(uuid.uuid4())
        # Resume means "continue THAT session file": `--session <path>` is pi's headless resume primitive,
        # and because the id was minted by us with `--session-id` on the first pass, the path is derivable
        # without parsing anything. A missing file is a cold start, not a crash — see _argv.
        resume_file = _session_file(session_dir, session_id) if req.resume_session else None
        if resume_file is not None and not resume_file.exists():
            resume_file = None
        containment = req.containment or Containment()
        # Pi has no MCP, by design and with no upstream extension. A stage that names servers cannot
        # possibly succeed here, so it is refused BEFORE any model spend rather than run and failed at its
        # output check — and the error names the override that fixes it (docs/harness/ENGINES.md §3).
        if containment.mcp_servers:
            raise PipelineConfigError(
                f"stage '{req.label}' needs MCP server(s) {sorted(containment.mcp_servers)} but the `pi` "
                f"engine has no MCP support (an explicit pi non-feature). Either fetch those inputs in the "
                f"harness before the stage and hand it local files, or pin the stage to an engine that has "
                f"MCP: --set stage_engines='{{\"{req.label}\": \"claude-code\"}}'"
            )
        writable = tuple(containment.writable_roots) or (cwd,)
        argv = self._argv(req, session_id=session_id, session_dir=session_dir, resume_file=resume_file,
                          containment=containment)
        _assert_known_flags(argv)
        env = self._env(req, cwd=cwd, session_dir=session_dir, denials_path=denials_path,
                        containment=containment, writable=writable)
        if self._guard_on(containment):
            # Preflight the ONE thing whose failure is invisible: the guard fails safe to blocking, so an
            # unreachable policy CLI presents as "the model mysteriously could not use any tool" AFTER a
            # full stage of spend. One ~50 ms probe turns that into a startup error.
            _assert_policy_reachable(env, cwd)

        writer = EventWriter(req.trace_path)
        tr = Translator(writer, model=req.model, session_id=session_id, denials_path=denials_path,
                        prompt=req.prompt)
        self._preflight_notes(tr, req, containment, writable, session_id, resume_file)

        # A cap of 0.0 means "spend nothing", which is a real instruction — `or None` would have turned
        # it into "no cap at all", the opposite.
        budget = req.max_budget_usd if req.max_budget_usd is not None else None
        if budget is not None and budget < 0:
            raise PipelineConfigError(
                f"max_budget_usd must not be negative (got {budget}); omit it for no cap, 0 to spend nothing")
        timed_out = False
        over_budget: list[float] = []
        proc: subprocess.Popen | None = None
        pgid: int | None = None
        deadline = time.monotonic() + req.timeout_s
        try:
            with open(err_path, "w", encoding="utf-8") as err, open(raw_path, "a", encoding="utf-8") as raw:
                try:
                    proc = subprocess.Popen(
                        argv,
                        cwd=str(cwd),
                        stdin=subprocess.DEVNULL,  # the kickoff turn is a positional arg after `--`
                        stdout=subprocess.PIPE,
                        stderr=err,  # NEVER merged: pi writes non-JSON diagnostics to stderr and merging
                        # them corrupts the JSONL parse. Extension errors land here and nowhere else.
                        start_new_session=True,  # own process group -> we can kill children too
                        env=env,
                    )
                except FileNotFoundError:
                    raise EngineFailure(
                        f"`{self.bin}` not found — install pi with `npm install -g --ignore-scripts "
                        f"@earendil-works/pi-coding-agent` (Node >= {self._flag('pi.min_node')}), "
                        f"or set --set pi.bin=<path>. Run `harness doctor` for the full preflight."
                    ) from None
                # Captured NOW, while the leader is alive. After the leader is reaped `os.getpgid` raises
                # ProcessLookupError, which is what made the SIGKILL escalation unreachable: a grandchild
                # that ignores SIGTERM then outlived every attempt to kill it.
                pgid = _pgid_of(proc)
                if req.procs is not None:
                    req.procs.append(proc)
                last_drain = 0.0
                for line in _read_lines(proc, deadline):
                    if line is None:
                        # the deadline passed with the pipe merely idle — no line to process
                        timed_out = True
                        break
                    raw.write(line)
                    raw.flush()
                    text = line.strip()
                    if text:
                        try:
                            tr.feed(json.loads(text))
                        except json.JSONDecodeError:
                            # stray non-JSON on stdout is a Pi surprise worth recording, not worth dying for
                            tr.feed({"type": "__nonjson__", "raw": text[:500]})
                    now = time.monotonic()
                    if now - last_drain > _POLL_S:
                        tr.drain_denials()
                        last_drain = now
                    if budget is not None and tr.stream_cost > budget:
                        over_budget.append(tr.stream_cost)
                        break
        finally:
            if proc is not None:
                # ALWAYS, on every path: the read loop can stop while a grandchild that inherited stdout
                # is still running, and abandoning it would leak the process AND (before this) wedge the
                # next read forever.
                _kill_group(proc, pgid)

        stderr_tail = _tail(err_path)
        cost = session_usage_cost(_session_file(session_dir, session_id))
        extra = None
        if over_budget:
            extra = (f"killed: summed cost ${over_budget[0]:.4f} exceeded max_budget_usd "
                     f"${budget:.4f} (best-effort — see the budget note)")
        elif "Extension error" in stderr_tail:
            extra = "the guard extension errored (pi reports extension errors only on stderr in --mode " \
                    "json) — tools were failing safe to BLOCKED; treat this stage as unrun"
        tr.finish(exit_code=proc.returncode if proc else None, timed_out=timed_out,
                  cost=cost, extra_error=extra, stderr_tail=stderr_tail)
        writer.close()

        duration = time.monotonic() - started
        if timed_out:
            raise EngineFailure(
                f"'{req.label}' timed out after {req.timeout_s}s — process group killed "
                f"(trace: {req.trace_path}) — stderr tail: {stderr_tail or '<empty>'}")
        if over_budget:
            raise EngineFailure(f"'{req.label}' {extra} (trace: {req.trace_path})")

        summary: TraceSummary = summarize(req.trace_path)
        return EngineResult(
            summary=summary,
            duration_s=duration,
            engine_desc=f"pi:{summary.model or req.model or 'default'}",
            prompt_sent=req.prompt,
            trace_path=req.trace_path,
            raw_trace_path=raw_path,
            exit_code=proc.returncode if proc else None,
        )

    # ---- argv / env --------------------------------------------------------------------------------

    def _argv(self, req: EngineRequest, *, session_id: str, session_dir: Path, resume_file: Path | None,
              containment: Containment) -> list[str]:
        argv: list[str] = [self.bin, "--mode", "json", "-p"]
        if self._flag("pi.hermetic"):
            argv += list(HERMETIC_FLAGS)
        if self._guard_on(containment):
            # discovery off, exactly one extension: ours. A CLI `-e` extension loads BEFORE the
            # project-trust decision, so the guard is active even in an untrusted repo.
            argv += ["--no-extensions", "-e", str(self._guard_path())]
        elif self._flag("pi.hermetic"):
            argv += ["--no-extensions"]
        provider = str(self._flag("pi.provider") or "")
        if provider:
            argv += ["--provider", provider]
        if req.model:
            argv += ["--model", req.model]
        thinking = str(req.effort or self._flag("pi.thinking") or "")
        if thinking:
            if thinking not in THINKING_LEVELS:
                raise EngineFailure(
                    f"pi thinking level {thinking!r} is not one of {sorted(THINKING_LEVELS)} — pi would "
                    f"silently ignore the unknown value (it does not reject unknown flags)")
            argv += ["--thinking", thinking]
        if req.skill_path:
            # --system-prompt REPLACES the base prompt and accepts a FILE PATH (both prompt flags run
            # through pi's resolvePromptInput, which reads the value when it exists on disk). Passing the
            # path avoids any argv-size limit on a large SKILL.md.
            argv += ["--system-prompt", str(Path(req.skill_path).resolve())]
        if req.system_prompt_append.strip():
            append_file = Path(req.cwd) / "logs" / f"{req.label}.append-system.md"
            append_file.parent.mkdir(parents=True, exist_ok=True)
            append_file.write_text(req.system_prompt_append, encoding="utf-8")
            argv += ["--append-system-prompt", str(append_file)]
        allowed = _pi_tool_names(containment.allowed_tools)
        if allowed:
            argv += ["--tools", ",".join(allowed)]
        denied = _pi_tool_names(containment.disallowed_tools)
        if denied:
            argv += ["--exclude-tools", ",".join(denied)]
        argv += ["--session-dir", str(session_dir)]
        # `--session <path>` continues an existing session file (warm context). `--session-id <id>` picks
        # the id for a NEW one — "use this exact id, creating it if missing" — so minting the id up front
        # makes the resume handle known before the process starts and a revision rerun an idempotent
        # re-invocation. A resume whose file has vanished falls back to the second form: same id, cold.
        argv += (["--session", str(resume_file)] if resume_file else ["--session-id", session_id])
        argv += ["--name", req.label]
        # `--` ALWAYS: a prompt beginning with "-" would otherwise be parsed as flags.
        argv += ["--", req.prompt]
        return argv

    def _guard_on(self, containment: Containment) -> bool:
        """The guard extension is the ONLY containment pi has, so it is on unless something explicitly
        turns containment off: the engine flag `pi.guard`, or the run-level `hooks` flag arriving as
        `Containment.hooks_enabled`."""
        return bool(self._flag("pi.guard")) and bool(containment.hooks_enabled)

    def _guard_path(self) -> Path:
        override = str(self._flag("pi.guard_path") or "")
        path = Path(override).expanduser().resolve() if override else GUARD_JS
        if not path.exists():
            raise EngineFailure(f"pi guard extension missing at {path} — containment cannot be transported")
        return path

    def _env(self, req: EngineRequest, *, cwd: Path, session_dir: Path, denials_path: Path,
             containment: Containment, writable: tuple[Path, ...]) -> dict[str, str]:
        env = os.environ.copy()
        env.update({k: str(v) for k, v in (req.env or {}).items()})
        if self._flag("pi.private_home"):
            home = cwd / ".pi-home"
            (home).mkdir(parents=True, exist_ok=True)
            # compaction OFF by default: it is lossy, and a stage that summarises its own evidence away
            # makes every trace-backed gate lie. Written as settings rather than a flag because pi exposes
            # compaction only through settings.json.
            (home / "settings.json").write_text(
                json.dumps({"compaction": {"enabled": bool(self._flag("pi.compaction"))}}, indent=2),
                encoding="utf-8",
            )
            env["PI_CODING_AGENT_DIR"] = str(home)
        env["PI_CODING_AGENT_SESSION_DIR"] = str(session_dir)  # --session-dir wins; belt and braces
        if self._flag("pi.offline"):
            env["PI_OFFLINE"] = "1"
            env["PI_SKIP_VERSION_CHECK"] = "1"
        if not self._flag("pi.telemetry"):
            env["PI_TELEMETRY"] = "0"
        env["HARNESS_POLICY_JSON"] = json.dumps(
            {
                "cwd": str(cwd),
                "writable_roots": [str(p) for p in writable],
                "readable_roots": [str(p) for p in containment.readable_roots],
                "readonly_shell": bool(containment.readonly_shell),
                "disabled_tools": list(_pi_tool_names(containment.disallowed_tools)),
                # The same three the Claude transport passes. Without them the policy's teaching strings
                # interpolate an empty broker path and a literal "<repos_root>", and — worse — a pinned
                # harness entrypoint stops being exempt, so a bash stage that works on claude-code has its
                # sanctioned alternative denied on pi with an unactionable reason.
                #
                # They are sourced from the ENVIRONMENT under engine-neutral names, never derived here: the
                # broker path and the exempt marker are things only a WORKFLOW knows, and the kernel may not
                # import or name workflow code (D2/D3, pinned by tests/test_harness_layering.py). A workflow
                # that wants the exemption exports these for its stages; absent them the policy simply has
                # no exemption to apply, which is a missing grant rather than a wrong verdict.
                **_policy_extra(env, dict(self._flag("pi.policy_extra") or {})),
            }
        )
        # the interpreter path is explicit: the guard is spawned by pi's Node host, which inherits no venv
        env["HARNESS_POLICY_CMD"] = json.dumps(policy_cmd())
        # A RELATIVE PYTHONPATH entry (`PYTHONPATH=src`, the src-layout habit) resolves against the
        # PROCESS's cwd — and the guard's process runs in the run dir, where `src` does not exist. Every
        # policy call would then fail and the guard would fail safe to blocking EVERY tool, visible only
        # as a pile of denials after the spend. Absolutising here keeps the meaning the operator intended.
        pypath = env.get("PYTHONPATH")
        if pypath:
            env["PYTHONPATH"] = os.pathsep.join(
                str(Path(part).resolve()) if part and not os.path.isabs(part) else part
                for part in pypath.split(os.pathsep)
            )
        env["HARNESS_DENIALS_PATH"] = str(denials_path)
        env["HARNESS_DENIALS_ECHO"] = "1" if self._flag("pi.denials_echo") else "0"
        return env

    def _preflight_notes(self, tr: Translator, req: EngineRequest, containment: Containment,
                         writable: tuple[Path, ...], session_id: str, resume_file: Path | None) -> None:
        """Record, in the trace, every place this adapter is knowingly not equivalent to the Claude one.
        A gap the ledger can see is a gap somebody can close; a silent one is a lie."""
        if not containment.writable_roots:
            tr.note(f"containment declared no writable_roots — defaulting to the run dir ({writable[0]})")
        dropped = [t for t in tuple(containment.allowed_tools) + tuple(containment.disallowed_tools)
                   if "mcp__" in t]
        if dropped:
            tr.note(f"dropped {len(dropped)} MCP tool name(s) from the pi tool lists: {sorted(dropped)}")
        if containment.readable_roots:
            tr.note("pi has no --add-dir equivalent: readable_roots are enforced by the harness guard "
                    "extension, not by pi (pi never restricted paths, so extra roots were already reachable)")
        if not self._guard_on(containment):
            why = "pi.guard is OFF" if not self._flag("pi.guard") else "containment.hooks_enabled is False"
            tr.note(f"{why} — the guard extension is NOT loaded, so NO containment is in force "
                    f"(pi ships no permission system of its own)")
        if containment.disabled_hooks:
            tr.note(f"containment.disabled_hooks {list(containment.disabled_hooks)} has no pi equivalent: "
                    f"the guard transports ONE policy check, not named hooks — ignored")
        if req.steer_epoch is not None:
            tr.note("pi has no steering channel in --mode json (stdin carries no user turns) — "
                    "steering for this invocation is dropped")
        if not self._flag("pi.hermetic"):
            tr.note("pi.hermetic is OFF — the analysed repo's .pi/ settings, skills and SYSTEM.md may load")
        if req.resume_session and resume_file is not None:
            tr.note(f"resuming pi session {req.resume_session} from {resume_file} via --session")
        elif req.resume_session:
            tr.note(f"resume of pi session {req.resume_session} requested but no session file exists under "
                    f"{req.cwd / str(self._flag('pi.session_dir'))} — starting COLD with the same id "
                    f"(--session-id), so the context of the previous attempt is NOT carried over")


# ---- module helpers --------------------------------------------------------------------------------


def _pi_tool_names(names) -> tuple[str, ...]:
    """Normalise a Containment tool list to Pi spellings, dropping MCP tools (Pi has none)."""
    out: list[str] = []
    for n in names or ():
        if "mcp__" in n:
            continue
        mapped = CLAUDE_TO_PI_TOOLS.get(n, n)
        if mapped not in out:
            out.append(mapped)
    return tuple(out)


def _assert_known_flags(argv: list[str]) -> None:
    """Fail loudly on a flag this adapter is not pinned to. Pi collects unknown flags for extensions
    instead of rejecting them, so `--no-skils` would disable nothing and the run would look fine.

    Only the slice BEFORE `--` is validated. Everything after the terminator is the prompt, and the whole
    reason `--` is passed is that a prompt may legitimately begin with `-` — a markdown bullet, or a
    `--- context ---` fence. Scanning past the terminator rejected exactly the prompts `--` exists to
    protect, and blamed a flag typo for it.
    """
    head = argv[1:argv.index("--")] if "--" in argv else argv[1:]
    unknown = [a for a in head if a.startswith("-") and a not in KNOWN_FLAGS]
    if unknown:
        raise EngineFailure(
            f"pi argv contains flag(s) this adapter is not pinned to: {unknown}. Pi SILENTLY IGNORES "
            f"unknown flags, so this would run unhermetically — add them to harness.engines.pi.KNOWN_FLAGS "
            f"after checking them against the pinned pi version."
        )


# Grace between SIGTERM and the unconditional SIGKILL. Long enough for pi to flush its session file,
# short enough that a wedged stage is not a wedged run.
KILL_GRACE_S = 15


def _pgid_of(proc: subprocess.Popen) -> int:
    """The child's process-group id, captured while it is alive. `start_new_session=True` makes the child
    its own group leader, so this equals its pid; the call is kept because the equality is an
    implementation detail of Popen, and `proc.pid` alone would silently kill the WRONG group if that ever
    changed."""
    try:
        return os.getpgid(proc.pid)
    except OSError:
        return proc.pid


def _read_lines(proc: subprocess.Popen, deadline: float):
    """Yield decoded stdout lines until the child's stdout closes or `deadline` passes; yield None once
    when the deadline passes with nothing to read.

    WHY not `for line in proc.stdout`: that blocks until the write end of the pipe is closed by EVERY
    process holding it, and pi's bash tool spawns grandchildren that inherit it. A grandchild outliving
    its parent therefore blocked the reader forever — past the stage's own timeout, with no RESULT event
    and no exception. Polling the fd against a deadline makes the timeout ours to enforce, and needs no
    second thread to enforce it.
    """
    fd = proc.stdout.fileno()
    poller = select.poll()
    poller.register(fd, select.POLLIN | select.POLLHUP | select.POLLERR)
    buf = b""
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            yield None
            return
        events = poller.poll(min(remaining, _POLL_S) * 1000)
        if not events:
            continue  # idle pipe: loop back and re-check the deadline
        try:
            chunk = os.read(fd, 65536)
        except OSError:
            chunk = b""
        if not chunk:
            if buf:  # a torn last line from a killed child is still evidence
                yield buf.decode("utf-8", errors="replace")
            return
        buf += chunk
        while b"\n" in buf:
            raw_line, buf = buf.split(b"\n", 1)
            yield raw_line.decode("utf-8", errors="replace") + "\n"


def _kill_group(proc: subprocess.Popen, pgid: int | None = None) -> None:
    """SIGTERM the child's whole process group, then SIGKILL it UNCONDITIONALLY after a grace period.

    Two things this gets right that the obvious version does not. The group matters: pi's bash tool
    spawns grandchildren, and signalling only the leader orphans them. And the escalation must not be
    conditional on the LEADER surviving — pi dies politely on SIGTERM while a node dev server or `npm`
    it started ignores it, and by then the leader has been reaped so `os.getpgid` raises and there is no
    group left to look up. Hence the pgid is captured at spawn and passed in.
    """
    if pgid is None:
        pgid = _pgid_of(proc)
    with contextlib.suppress(OSError):
        os.killpg(pgid, signal.SIGTERM)
    with contextlib.suppress(subprocess.TimeoutExpired):
        proc.wait(timeout=KILL_GRACE_S)
    # unconditional: the leader being gone says nothing about its children
    with contextlib.suppress(OSError):
        os.killpg(pgid, signal.SIGKILL)
    with contextlib.suppress(subprocess.TimeoutExpired):
        proc.wait(timeout=5)
    # the pipe may still be held open by a process we could not reach; closing our end stops any
    # subsequent read from blocking on it
    if proc.stdout is not None:
        with contextlib.suppress(OSError):
            proc.stdout.close()


def _tail(path: Path, n: int = 2000) -> str:
    with contextlib.suppress(OSError):
        return path.read_text(encoding="utf-8", errors="replace")[-n:].strip()
    return ""


def _session_file(session_dir: Path, session_id: str) -> Path:
    """`<session-dir>/--<mangled-cwd>--/<timestamp>_<session-id>.jsonl`. The timestamp prefix is Pi's, so
    the id is what we match on — as a full filename suffix, never a substring, so one session id that
    happens to contain another's cannot shadow it. The newest match wins if a resumed id has two files.

    Never raises: a session file can vanish between the glob and the stat (a concurrent run's hygiene
    wipe), and losing the cost sum is not a reason to lose the stage.
    """
    def mtime(p: Path) -> float:
        try:
            return p.stat().st_mtime
        except OSError:
            return -1.0

    try:
        candidates = [p for p in session_dir.rglob("*.jsonl")
                      if p.name == f"{session_id}.jsonl" or p.name.endswith(f"_{session_id}.jsonl")]
    except OSError:
        candidates = []
    return max(candidates, key=mtime) if candidates else session_dir / f"{session_id}.jsonl"


def policy_cmd() -> list[str]:
    """The argv the guard shells out to. One place, so the preflight probes exactly what the guard runs."""
    return [sys.executable, "-m", "harness.policy", "check"]


def _policy_extra(env: dict[str, str], override: dict) -> dict:
    """The optional policy fields, from the environment, with the `pi.policy_extra` flag taking precedence.
    Only keys that are actually set are sent, so an unset field keeps the policy's own default."""
    out = {}
    for key, var in POLICY_EXTRA_ENV.items():
        value = override.get(key, env.get(var))
        if value:
            out[key] = str(value)
    return out


def _assert_policy_reachable(env: dict[str, str], cwd: Path) -> None:
    """Prove `python -m harness.policy check` answers, from the guard's cwd and with the guard's env."""
    cmd = policy_cmd()
    probe = json.dumps({"tool": "Read", "input": {}, "cwd": str(cwd)})
    try:
        proc = subprocess.run(cmd, input=probe, capture_output=True, text=True, timeout=60,
                              cwd=str(cwd), env=env)
    except (OSError, subprocess.SubprocessError) as exc:
        raise EngineFailure(
            f"the containment policy CLI is not runnable ({' '.join(cmd)}): {exc}. The pi guard fails "
            f"SAFE, so every tool call would be denied after a full stage of spend."
        ) from None
    verdict = (proc.stdout or "").strip().splitlines()
    try:
        ok = bool(verdict) and "decision" in json.loads(verdict[-1])
    except json.JSONDecodeError:
        ok = False
    if not ok:
        raise EngineFailure(
            f"the containment policy CLI gave no verdict (`{' '.join(cmd)}` exited {proc.returncode}): "
            f"{(proc.stderr or proc.stdout or '<silence>')[-400:]}. `harness` must be importable by THAT "
            f"interpreter — install the project (`uv sync`) or set an ABSOLUTE PYTHONPATH; a relative one "
            f"resolves against the run dir. The pi guard fails SAFE, so every tool call would be denied."
        )


def _version_of(cmd: list[str]) -> str | None:
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=30).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    m = _VERSION_RE.search(out or "")
    return m.group(0) if m else None


def _older(got: str, want: str) -> bool:
    def parts(v: str) -> tuple[int, ...]:
        m = _VERSION_RE.search(v)
        return tuple(int(x) for x in m.groups()) if m else (0, 0, 0)

    return parts(got) < parts(want)
