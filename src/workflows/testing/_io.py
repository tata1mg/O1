"""Durable artifact I/O for the testing workflow: atomic writes, strict reads, advisory locking.

Every file this workflow declares canonical (D8) is written through `atomic_write_text` and read through
`parse_json`, and every read-modify-write on the JSON store happens under `locked`.

WHY, three separate regressions' worth:

  * `Path.write_text` truncates and then writes, so a SIGINT, OOM or full disk mid-write leaves a
    zero-length or half-written file. The repo already learned this the expensive way — a torn
    `manifest.json` BRICKED a run dir, because every later invocation crashed at load
    (`rfc_agent/runner.py:56-68`, 0.23.2, and again in 0.23.3 when a FIXED temp name let two invocations
    interleave into one temp file). `qa/cases.json` is exactly as load-bearing.
  * `json.loads` on a torn file raises `JSONDecodeError` out of whatever happened to be reading, which is
    a traceback rather than a verdict, and it repeats on every retry. A reader must NAME the file and say
    what to do about it.
  * A read-modify-write with no lock silently loses the other writer's work. For the run file that means
    a recorded execution vanishing, and nothing downstream notices because `qa/results.json` is written
    by the stage independently of the store.

TODO(harness): `atomic_write_text` and `locked` are not workflow-specific — they belong beside the
manifest writer in the kernel (a `harness.io` module), with `rfc_agent.runner._atomic_write` becoming a
re-export. Kept here for now so this workflow does not edit a file another builder owns; the
implementation is deliberately identical to the runner's so the move is a delete-and-import.
"""

from __future__ import annotations

import fcntl
import json
import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from harness.contracts import GateFailure


def atomic_write_text(path: Path, text: str) -> Path:
    """Unique-temp + fsync + os.replace, in the destination's own directory.

    Atomic against process death AND durable against power loss: `os.replace` alone only orders the
    rename, and some filesystems commit it ahead of the data, publishing a zero-length file. The temp
    name carries the pid so two invocations sharing a run dir cannot interleave writes into one temp file.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        # A failed write must not leave the temp file behind to be mistaken for an artifact, and must
        # never remove the PREVIOUS good version of `path` — os.replace not having run is what preserves it.
        tmp.unlink(missing_ok=True)
        raise
    return path


def atomic_write_json(path: Path, data: Any) -> Path:
    """Serialise first, then write. A value that cannot be serialised must not truncate the old file."""
    return atomic_write_text(Path(path), json.dumps(data, indent=2) + "\n")


def parse_json(path: Path) -> tuple[Any, str]:
    """-> (value, "") when readable, or (None, why) — never raises on a torn or unreadable file.

    The two failure modes are kept distinct by the caller's choice of default: an ABSENT file is usually
    "this stage has not run yet", while a present-but-unparseable one is damage that needs naming.
    """
    path = Path(path)
    if not path.exists():
        return None, f"{path.name} does not exist"
    try:
        text = path.read_text()
    except OSError as exc:
        return None, f"{path} could not be read ({exc})"
    try:
        return json.loads(text), ""
    except json.JSONDecodeError as exc:
        # The two causes need different advice, and the first bytes tell them apart: content that does
        # not even START like JSON is a writer that emitted the wrong thing (a fence, a preamble), while
        # content that starts like JSON and fails to parse is damage.
        head = text.lstrip()[:1]
        if text.strip() and head not in ("{", "["):
            return None, (f"{path} does not contain JSON ({exc}) — it begins {text.lstrip()[:20]!r}. "
                          f"Write the JSON alone: no markdown fence, no prose before or after.")
        return None, (f"{path} is not valid JSON ({exc}) — the file is empty or truncated, so it was "
                      f"written by a crashed process. Delete it and re-run the stage that produces it.")


def load_json_strict(path: Path, *, what: str = "artifact") -> Any:
    """Read or raise `GateFailure` naming the file. For code paths with no gate to return failures to."""
    value, why = parse_json(path)
    if why:
        raise GateFailure(f"{what}: {why}")
    return value


@contextmanager
def locked(path: Path) -> Iterator[None]:
    """Advisory exclusive lock for a read-modify-write on `path`, via flock on a sidecar `<name>.lock`.

    The lock lives on a sidecar rather than on `path` itself because `atomic_write_text` REPLACES the
    inode: a lock held on the old file would protect nothing once the rename landed. flock is advisory
    and per-open-file-description, so two threads or two processes each opening the sidecar contend
    correctly, and the lock is released by the OS if the holder dies.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_name(f"{path.name}.lock")
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)
