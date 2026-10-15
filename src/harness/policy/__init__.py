"""harness.policy — the containment POLICY as pure functions, shared by every engine transport (D6).

The policy answers one question: *may this tool call proceed?* It knows nothing about how an engine asks.
Transports translate:

  * Claude Code — `rfc_agent.hooks.guard_writes` / `approve_readonly` run as PreToolUse hooks and turn a
    verdict into an exit code or a `hookSpecificOutput` JSON envelope. Those two scripts are now thin
    transports over this package; their exit codes, env vars and reason strings are unchanged.
  * Pi — an extension whose `tool_call` handler shells out to the CLI below.

Layers: `paths` (confinement), `shell` (read-only classifier), `mutators` (mutation/egress/redirect
deny-list), `allowlist` (sanctioned broker forms).


THE CLI CONTRACT  (`python -m harness.policy check`)
-----------------------------------------------------
This is the seam the Pi extension calls. It is stable; change it only additively.

stdin — ONE JSON object (unknown keys ignored)::

    {
      "tool":            "Bash",             # engine-native tool name
      "input":           {"command": "..."}, # engine-native tool input
      "cwd":             "/abs/run/dir",     # the agent's working directory (relative paths resolve here)
      "writable_roots":  ["/abs/run/dir"],   # trees the agent may write; [] = nothing is writable
      "readable_roots":  ["/abs/repos"],     # extra read-only mounts; [] = read confinement not declared
      "readonly_shell":  true                # run the read-only shell classifier (Containment.readonly_shell)
    }

stdout — ONE JSON object; the process ALWAYS exits 0 (a crashed policy check must not kill a run, and a
malformed request is reported as a verdict, not a traceback)::

    {"decision": "allow" | "deny" | "ask", "reason": "..."}

Verdict meanings, and what a transport must do with each:

  ``deny``   Refuse the call and relay ``reason`` to the model verbatim. The reason strings are written to
             teach the sanctioned alternative in one turn; paraphrasing them costs denial round-trips.
  ``allow``  Provably safe — skip any human/interactive ask. It is NOT a licence to bypass other guards;
             a transport running several policies keeps the most restrictive verdict (deny > ask > allow).
  ``ask``    No opinion. The transport's own allowlist/permission flow decides. In a headless run with no
             human, ``ask`` is effectively a denial — that is intended, and it is why the never-approvable
             classes return ``deny`` with an instructive reason instead of falling through to ``ask``.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from . import allowlist, mutators, paths, shell
from .allowlist import Broker, BrokerAllowlist
from .paths import DEV_SINKS, WRITE_TOOLS, inside, resolve

__all__ = [
    "Broker", "BrokerAllowlist", "DEV_SINKS", "Decision", "Request", "WRITE_TOOLS",
    "allowlist", "check", "inside", "mutators", "paths", "resolve", "shell",
]

# Engine-native names for "run a shell command". A tool whose input carries a `command` key is treated as
# a shell call too, so an unlisted engine's spelling still gets contained.
SHELL_TOOLS: tuple[str, ...] = ("Bash", "bash", "shell", "run_command", "execute_command", "terminal")
# Engine-native names for "read a file", used for read confinement when readable_roots are declared.
READ_TOOLS: tuple[str, ...] = ("Read", "read_file", "read", "NotebookRead")

ALLOW, DENY, ASK = "allow", "deny", "ask"


@dataclass(frozen=True)
class Decision:
    decision: str  # ALLOW | DENY | ASK
    reason: str = ""

    def as_dict(self) -> dict:
        return {"decision": self.decision, "reason": self.reason}


@dataclass
class Request:
    """One tool call, engine-neutral. Mirrors the stdin object documented in the module docstring."""

    tool: str = ""
    input: dict = None  # type: ignore[assignment]
    cwd: Path | None = None
    writable_roots: tuple[Path, ...] = ()
    readable_roots: tuple[Path, ...] = ()
    readonly_shell: bool = True
    broker_allowlist: BrokerAllowlist | None = None
    broker_hint: str = ""  # command string named in reasons as the sanctioned alternative
    repos_root: str = "<repos_root>"
    exempt_marker: str | None = None  # substring marking a pinned harness entrypoint (never denied)

    def __post_init__(self) -> None:
        if self.input is None:
            self.input = {}

    @classmethod
    def from_json(cls, obj: dict) -> Request:
        def roots(key: str) -> tuple[Path, ...]:
            return tuple(Path(str(p)).expanduser().resolve() for p in (obj.get(key) or []))

        cwd = obj.get("cwd")
        return cls(
            tool=str(obj.get("tool") or ""),
            input=dict(obj.get("input") or {}),
            cwd=Path(str(cwd)).expanduser() if cwd else None,
            writable_roots=roots("writable_roots"),
            readable_roots=roots("readable_roots"),
            readonly_shell=bool(obj.get("readonly_shell", True)),
            broker_hint=str(obj.get("broker_hint") or ""),
            repos_root=str(obj.get("repos_root") or "<repos_root>"),
            exempt_marker=obj.get("exempt_marker") or None,
        )

    def is_shell(self) -> bool:
        return self.tool in SHELL_TOOLS or ("command" in self.input and self.tool not in WRITE_TOOLS)

    def target_path(self) -> str:
        return str(self.input.get("file_path") or self.input.get("notebook_path") or self.input.get("path") or "")


def check_write(req: Request) -> Decision:
    """Path confinement for a write tool. A target outside every writable root is denied.

    The reason string keeps the shipped wording ("outside the run dir") when there is exactly one
    writable root, because that string is tuned prompt-facing text the model recovers from in one turn.
    """
    target = req.target_path()
    if target and not inside(target, req.writable_roots, req.cwd):
        if len(req.writable_roots) == 1:
            where = f"outside the run dir ({req.writable_roots[0]})"
        else:
            where = f"outside the writable root(s) ({', '.join(str(r) for r in req.writable_roots) or 'none'})"
        return Decision(DENY, f"{req.tool} targets {target}, {where}. Write only under the run dir.")
    return Decision(ALLOW, "write inside the run dir")


def check_shell(req: Request) -> Decision:
    """Mutation/egress/redirect denial, then (optionally) the read-only classifier."""
    cmd = str(req.input.get("command") or "")
    if not cmd:
        return Decision(ASK, "no command in the tool input")
    reason = mutators.shell_denial(cmd, req.writable_roots, req.cwd)
    if reason:
        return Decision(DENY, reason)
    if not req.readonly_shell:
        return Decision(ASK, "read-only shell classification disabled for this stage")
    if req.broker_allowlist is not None:
        try:
            import shlex

            hit = req.broker_allowlist.match(shlex.split(cmd))
        except ValueError:
            hit = None
        if hit is not None:
            return Decision(ALLOW, f"sanctioned broker: {hit.name}")
    return classify_readonly(cmd, broker_hint=req.broker_hint, repos_root=req.repos_root,
                             exempt_marker=req.exempt_marker)


def classify_readonly(cmd: str, *, broker_hint: str = "", repos_root: str = "<repos_root>",
                      exempt_marker: str | None = None) -> Decision:
    """ALLOW when every segment is provably read-only; DENY the never-approvable classes; else ASK.

    Split out of `check_shell` because the Claude `approve_readonly` transport needs exactly this half
    (the mutation deny-list is the other hook's job, and the CLI composes the two verdicts itself).
    """
    cmd = shell.strip_stderr_noise(cmd)
    if shell.CMD_SUBST.search(cmd):
        return Decision(ASK, "command substitution can run anything — no opinion")
    segs = shell.segments(cmd)
    if not segs:
        # unparseable (redirect/heredoc present) — the real PROJ-3394 denial shape (`awk ... > out.txt`,
        # `cd x && awk > f`, heredoc python): still teach the alternative instead of deferring silently
        return _never_or_ask(cmd, broker_hint, repos_root, exempt_marker)
    for seg in segs:
        ok, deny = shell.segment_readonly(seg)
        if deny:
            return Decision(DENY, deny)
        if not ok:
            return _never_or_ask(seg, broker_hint, repos_root, exempt_marker)
    return Decision(ALLOW, "read-only pipeline (approve_readonly hook)")


def _never_or_ask(text: str, broker_hint: str, repos_root: str, exempt_marker: str | None) -> Decision:
    reason = shell.never_reason(text, broker=broker_hint, repos_root=repos_root, exempt_marker=exempt_marker)
    return Decision(DENY, reason) if reason else Decision(ASK, "not provably read-only — no opinion")


def check_read(req: Request) -> Decision:
    """Read confinement. Only enforced when readable_roots are DECLARED: an empty tuple means the
    transport did not declare its mounts (the Claude adapter relies on `--add-dir` for this), and the
    policy must not invent a denial it cannot justify."""
    if not req.readable_roots and not req.writable_roots:
        return Decision(ASK, "no roots declared — read confinement not enforced")
    target = req.target_path()
    if not target:
        return Decision(ASK, "no path in the tool input")
    if inside(target, tuple(req.readable_roots) + tuple(req.writable_roots), req.cwd):
        return Decision(ALLOW, "read inside a declared root")
    if not req.readable_roots:
        return Decision(ASK, "no readable roots declared — read confinement not enforced")
    roots = ", ".join(str(r) for r in tuple(req.readable_roots) + tuple(req.writable_roots))
    return Decision(DENY, f"Read of {target} is outside the declared readable root(s) ({roots}).")


def check(req: Request) -> Decision:
    """The single entry point: route one tool call to the rule that governs it.

    Order matters — a write tool is judged by path confinement even when its input also carries a
    `command` key, and shell calls are judged by the deny-list BEFORE the read-only classifier so a
    provably-read-only-looking pipeline with an escaping redirect still denies.
    """
    if req.tool in WRITE_TOOLS:
        return check_write(req)
    if req.is_shell():
        return check_shell(req)
    if req.tool in READ_TOOLS:
        return check_read(req)
    return Decision(ASK, f"no policy rule for tool {req.tool!r}")
