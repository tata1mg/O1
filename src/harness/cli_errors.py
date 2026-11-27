"""One argparse error for every command-line entry point: a wrong parameter says which one, what is supported and
what was probably meant, as a `[tag] STOP: …` line. argparse's own error is a usage dump that the Axon commands and the
harness do not relay, so a run ended as "stopped" with no reason (#29). Nothing here knows any option by name: the
supported list is read from the parser that was actually in use.

    class MyParser(FriendlyParser): ...        or     p = FriendlyParser(prog="rfc", command="/rfc")
"""

from __future__ import annotations

import argparse
import difflib
import re
import sys


def _options(parser: argparse.ArgumentParser) -> list[str]:
    return sorted({o for a in parser._actions for o in a.option_strings if o.startswith("--") and o != "--help"})


def _subparsers(parser: argparse.ArgumentParser) -> dict:
    for a in parser._actions:
        if isinstance(a, argparse._SubParsersAction):
            return a.choices
    return {}


def deepest(parser: argparse.ArgumentParser, argv: list[str]) -> tuple[argparse.ArgumentParser, str]:
    """The (sub)parser the command line selected (`harness run testing …` -> the `testing` parser) and its name."""
    name = parser.prog
    for tok in argv:
        sub = _subparsers(parser).get(tok)
        if sub is not None:
            parser, name = sub, f"{name} {tok}"
    return parser, name


def message_for(parser: argparse.ArgumentParser, message: str, argv: list[str], command: str | None = None) -> str:
    where, name = deepest(parser, argv)
    label = command or name
    supported = _options(where)
    m = re.match(r"unrecognized arguments: (.*)", message)
    if m:
        bad = [t.split("=")[0] for t in m.group(1).split() if t.startswith("-")] or [m.group(1)]
    else:
        # another complaint came first (a missing required option): a wrong option on the same line still gets named
        known = set(supported) | {"--help"}
        bad = [t.split("=")[0] for t in argv if t.startswith("--") and t.split("=")[0] not in known]
        if not bad:
            return f"{message} — check the supported parameters of {label}: {', '.join(supported)}"
    shown = []
    for b in bad:
        near = difflib.get_close_matches(b, supported, n=1)
        shown.append(f"{b} (did you mean {near[0]}?)" if near else b)
    text = (f"Invalid parameter{'s' if len(bad) > 1 else ''} found: {', '.join(shown)}, please check supported "
            f"parameters while running the {label} command. Supported: {', '.join(supported)}")
    return text if m else f"{text}. Also: {message}"


class FriendlyParser(argparse.ArgumentParser):
    """`exit_code`: what a usage error exits with (argparse's 2, unless the program's exit-code contract says
    otherwise). `command`: how the command is called by the person (`/rfc`), for the message."""

    exit_code = 2
    tag = "rfc"

    def __init__(self, *args, command: str | None = None, tag: str | None = None, **kw):
        super().__init__(*args, **kw)
        self.command = command
        if tag:
            self.tag = tag
        self._argv: list[str] = []

    def parse_known_args(self, args=None, namespace=None):
        self._argv = list(sys.argv[1:] if args is None else args)
        return super().parse_known_args(args, namespace)

    def error(self, message: str):
        text = message_for(self, message, self._argv, self.command)
        print(f"[{self.tag}] STOP: {text}", file=sys.stderr)
        raise SystemExit(self.exit_code)
