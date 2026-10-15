"""The broker allowlist: the small set of EXACT command forms a contained agent may run.

WHY brokers exist: the read-only classifier can prove `grep -n x f` is safe but can never prove an
`awk`/`sed`/`python -c` form is, because those are arbitrary exec+write engines. Rather than leave the
agent to discover that wall by crashing into it, the harness ships Agent-Computer-Interface wrappers
(slice/hash/save-image, repo git history, the composer's self-check) that do the legitimate job with
destinations confined inside the script itself. Only their EXACT invocation form is permitted.

WHY this list lives in the kernel: a Claude transport expresses it as `--allowedTools` prefix rules;
Pi's extension has no allowlist mechanism at all and must ask `python -m harness.policy check` whether a
command is a sanctioned broker call. Two transports, one list. The Claude engine keeps its own
`_READONLY_BASH_ALLOW` (its prompt text is derived from that literal); `tests/test_harness_policy.py`
pins the two against each other so they cannot drift silently.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# Simple read-only commands (no flag can make them write). Mirrors shell.SAFE minus the shell builtins
# that are not separately allowlistable, plus the two inert utilities the Claude engine allows.
READONLY_COMMANDS: tuple[str, ...] = (
    "grep", "rg", "cat", "head", "tail", "wc", "ls", "echo", "sort", "uniq", "cut", "tr",
    "diff", "stat", "file", "basename", "dirname", "cd", "pwd", "which", "jq", "shasum",
)
# git is subcommand-scoped — never `git:*`, which would admit push and sidestep the mutator regex.
READONLY_GIT_SUBCOMMANDS: tuple[str, ...] = (
    "log", "show", "diff", "grep", "rev-parse", "status", "blame", "branch",
)


@dataclass(frozen=True)
class Broker:
    """One sanctioned wrapper. `argv_prefix` is matched against the command's leading tokens."""

    name: str
    argv_prefix: tuple[str, ...]
    help: str


@dataclass
class BrokerAllowlist:
    """The brokers a transport grants. Built by the adapter (it knows the interpreter + script paths)."""

    brokers: tuple[Broker, ...] = field(default_factory=tuple)

    def match(self, tokens: list[str]) -> Broker | None:
        for b in self.brokers:
            n = len(b.argv_prefix)
            if len(tokens) >= n and tuple(tokens[:n]) == b.argv_prefix:
                return b
        return None

    def claude_allow_rules(self) -> list[str]:
        """`--allowedTools` prefix rules for a Claude-shaped transport (exact form + wildcard args)."""
        return [f"Bash({' '.join(b.argv_prefix)}:*)" for b in self.brokers]
