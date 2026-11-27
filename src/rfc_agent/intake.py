"""Free-text intake for `/rfc`: "Build an rfc referring this PRD <link> and pick the latest vsat flows" -> the flags
the pipeline takes (`--prd`, `--figma`, `--instructions`), so a person can talk to it instead of remembering them.

    python -m rfc_agent.intake --json <<'EOF'      # {"argv": [...]}, or exit 2 + a message on stderr
    <what the person typed>
    EOF
    python -m rfc_agent.intake --shell <<'EOF'     # the same as one shell-quoted line (what commands/rfc.md pastes)

Rules:
  - A prompt is any input whose first word is not a flag (or `doctor` / `attach` / `feedback` / `help`). Input that
    starts with a flag is passed through untouched.
  - The PRD is the first Confluence page link in the prompt. A prompt without one is discarded: nothing starts and
    nothing is traced (exit 2).
  - Figma links in the prompt become `--figma`. The rest of the prompt (links replaced by placeholders) becomes
    `--instructions`, unless nothing is left but "build an rfc for <link>".
  - The prompt itself and the instructions are saved under RUNS_ROOT/.intake/ and passed by path, so no quoting
    problem can reach the shell and long text never rides in a command line. The run records the prompt on its
    Langfuse trace (`--input-mode prompt`), clipped to 300 words.
"""

from __future__ import annotations

import argparse
import json
import re
import shlex
import sys
import time
from datetime import datetime
from pathlib import Path

PROMPT_WORDS = 300
SUBCOMMANDS = ("doctor", "attach", "feedback", "help")
_URL = re.compile(r"https?://[^\s<>()\[\]\"']+")
_TRAILING = ".,;:!?"
# words that only say "make an RFC from this": a prompt with nothing else carries no instructions
_BOILERPLATE = set("""build make create generate write draft produce run an a the rfc tech technical from for of on to
    this that these prd product requirement requirements document doc referring refer reference using use with and
    please can you me my link page here below above is it as at by in""".split())


def is_prompt(text: str) -> bool:
    words = text.split()
    return bool(words) and not words[0].startswith("-") and words[0] not in SUBCOMMANDS


def clip_words(text: str, limit: int = PROMPT_WORDS) -> str:
    words = text.split()
    return text.strip() if len(words) <= limit else " ".join(words[:limit]) + f" … [{len(words) - limit} more words]"


def read_text_arg(value: str | None) -> str:
    """`--instructions` / `--prompt-text`: a path to a file, or the text itself."""
    if not value:
        return ""
    try:
        p = Path(value).expanduser()
        if len(value) < 1024 and "\n" not in value and p.is_file():
            return p.read_text(encoding="utf-8").strip()
    except OSError:
        pass
    return value.strip()


def parse_prompt(text: str) -> dict | None:
    """{prd, figma: [url], instructions: str} from a free-text prompt, or None when it names no Confluence page."""
    from .scope import atlassian as at
    from .scope import figma

    urls = [u.rstrip(_TRAILING) for u in _URL.findall(text)]
    prd = next((u for u in urls if at.page_id_of(u)), None)
    if not prd:
        return None
    figma_urls = [lk["url"] for lk in figma.links_in("\n".join(urls))]
    rest = text
    for u in sorted(set(urls), key=len, reverse=True):
        rest = rest.replace(u, "[PRD link]" if u == prd else "[Figma link]" if u in figma_urls else "[link]")
    words = re.findall(r"[a-z][a-z'-]*", re.sub(r"\[[^\]]*\]", " ", rest.lower()))
    content = [w for w in words if w not in _BOILERPLATE]
    return {"prd": prd, "figma": figma_urls, "instructions": " ".join(rest.split()) if len(content) >= 3 else ""}


def to_argv(text: str, save_dir: Path | None = None) -> list[str] | None:
    """The flags for a prompt (None when it has no PRD link). The prompt and instructions are saved as files."""
    parsed = parse_prompt(text)
    if parsed is None:
        return None
    if save_dir is None:
        from .config import get_config

        save_dir = Path(get_config().get("RUNS_ROOT") or "~/rfc-runs").expanduser() / ".intake"
    save_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S") + f"-{int(time.monotonic() * 1000) % 100000}"
    prompt_file = save_dir / f"prompt-{stamp}.md"
    prompt_file.write_text(text.strip() + "\n", encoding="utf-8")
    argv = ["--prd", parsed["prd"]]
    for url in parsed["figma"]:
        argv += ["--figma", url]
    if parsed["instructions"]:
        instr_file = save_dir / f"instructions-{stamp}.md"
        instr_file.write_text(parsed["instructions"] + "\n", encoding="utf-8")
        argv += ["--instructions", str(instr_file)]
    return argv + ["--input-mode", "prompt", "--prompt-text", str(prompt_file)]


NO_PRD = ("rfc: I could not find a Confluence PRD link in that. Give the PRD page link in your message "
          "(e.g. `Build an rfc from <PRD link> and use the latest flows`), or use --prd <link>.")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m rfc_agent.intake", description=__doc__.split("\n")[0])
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--json", action="store_true", help='print {"argv": [...]}')
    g.add_argument("--shell", action="store_true", help="print one shell-quoted line")
    p.add_argument("text", nargs="*", help="the input (default: read stdin)")
    args = p.parse_args(argv)
    text = " ".join(args.text) if args.text else sys.stdin.read()
    text = text.strip()
    if not is_prompt(text):  # flags (or a subcommand): untouched
        out = shlex.split(text) if text else []
    else:
        out = to_argv(text)
        if out is None:
            print(NO_PRD, file=sys.stderr)
            return 2
    print(json.dumps({"argv": out}) if args.json else shlex.join(out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
