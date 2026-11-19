"""Which Maestro flow covers which Kiwi case, and whether that flow may be trusted to run it.

`execute_app` asks one question per CONFIRMED Android case: is there a VALIDATED flow for it? If so, the
flow runs (no model, no cost); if not, an agent drives the emulator. This module answers from the flows'
own headers in the maestro-automation repo — the same two structured sources `maestro.py` attributes
results from — and nothing else:

  * the `# Covers:` block and step `label:` lines name the cases (`maestro.covers_block` /
    `maestro.label_cases`, whose `TC-…` pattern is reused as is);
  * the cart flows name the Kiwi case NUMBER instead (`# Covers: Kiwi Pharmacy › Cart #191 — [CT019]`).
    Those are read as `KIWI-191`, the id a case without an `extid:` tag gets in direct mode, and matched to
    any case whose Kiwi id is 191 whatever its extid (`flows_for`);
  * the first `# STATUS:` line of the header says whether the flow is validated. The flows' own word is
    taken (decided 2026-11-17): VALIDATED / GREEN / RE-VALIDATED, with or without a leading ✅, is
    validated; anything else (DRAFT, UNFROZEN, BLOCKED, PARTIAL, "NOT runnable", …) is not, and a flow with
    no STATUS line does not say it is validated, so it is not;
  * tags and the `# REQUIRES: <profile>` line are the gates `run_agent.py` applies: `mock-required` needs a
    staged fixture, `manual-order-placing` places a real order, a profile needs that account signed in.

Read-only: nothing here touches a device or the repo's files.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from . import maestro

#: `Kiwi Pharmacy › Cart #191` in a Covers block -> the Kiwi case id 191.
RE_KIWI_NUM = re.compile(r"\bKiwi\b[^#\n]*#(\d+)")
RE_STATUS = re.compile(r"^#\s*STATUS:?\s*(.*)$", re.I)
RE_REQUIRES = re.compile(r"^#\s*REQUIRES:\s*(?:account profile\s*)?`?([a-z][a-z0-9_]*)`?", re.I)
VALIDATED_WORDS = ("VALIDATED", "GREEN", "RE-VALIDATED", "REVALIDATED")

VALIDATED, NOT_VALIDATED, NO_STATUS = "validated", "not validated", "no status"
MOCK_TAG, ORDER_TAG, SUBFLOW_TAG = "mock-required", "manual-order-placing", "sub-flow"
#: The account tags `config.yaml` excludes from a bare suite run, and the profile each one needs (the flows
#: carrying them do not all have a `# REQUIRES:` line).
TAG_PROFILES = {"needs-cp-tiering-account": "care_plan_user", "needs-family-account": "family_user",
                "needs-default-account": "default"}


@dataclass
class Flow:
    name: str                      # the file's stem, what Maestro reports as the testcase id
    path: Path
    cases: set[str] = field(default_factory=set)
    status: str = NO_STATUS        # VALIDATED / NOT_VALIDATED / NO_STATUS
    status_line: str = ""          # the header's own words, for a reader
    tags: tuple[str, ...] = ()
    requires: str = ""             # an account profile from .maestro/users.yaml, or ""

    @property
    def validated(self) -> bool:
        return self.status == VALIDATED

    def gate(self) -> str:
        """Why this flow may not be run by `execute_app` in v1, or ""."""
        if MOCK_TAG in self.tags:
            return "needs a staged mock fixture (mock-required)"
        if ORDER_TAG in self.tags:
            return "places a real order (manual-order-placing) — not run in v1"
        return ""


def header_of(text: str) -> str:
    """The comment header: every line before `appId:` (where the flow's config begins)."""
    out = []
    for line in text.split("\n"):
        if line.startswith("appId:"):
            break
        out.append(line)
    return "\n".join(out)


def status_of(header: str) -> tuple[str, str]:
    """(VALIDATED | NOT_VALIDATED | NO_STATUS, the first STATUS line's text)."""
    for line in header.split("\n"):
        m = RE_STATUS.match(line.strip())
        if not m:
            continue
        words = m.group(1).replace("✅", " ").replace("*", " ").split()
        first = (words[0] if words else "").strip(".,:;—-").upper()
        return (VALIDATED if first in VALIDATED_WORDS else NOT_VALIDATED), m.group(1).strip()
    return NO_STATUS, ""


def _tags(text: str) -> tuple[str, ...]:
    """The flow's `tags:` list (config section, before `---`)."""
    config = text.split("\n---", 1)[0]
    out, inside = [], False
    for line in config.split("\n"):
        if line.startswith("tags:"):
            inside = True
            continue
        if inside:
            m = re.match(r"^\s+-\s*['\"]?([\w.-]+)['\"]?\s*$", line)
            if m:
                out.append(m.group(1))
            elif line.strip() and not line.startswith(" "):
                break
    return tuple(out)


def _covers_kiwi(header: str) -> set[str]:
    """`KIWI-<n>` for every `Kiwi … #<n>` inside the header's Covers block."""
    ids: set[str] = set()
    collecting = False
    for line in header.split("\n"):
        if re.match(r"^#\s*Covers\b", line, re.I):
            collecting = True
        elif collecting and (re.match(r"^#\s*$", line) or not line.lstrip().startswith("#")):
            break
        if collecting:
            ids |= {f"KIWI-{n}" for n in RE_KIWI_NUM.findall(line)}
    return ids


def read_flow(path: Path) -> Flow:
    text = path.read_text(encoding="utf-8", errors="ignore")
    header = header_of(text)
    _, _, body = text.partition("\n---")
    cases = maestro.covers_block(header) | _covers_kiwi(header)
    cases |= {cid for _, ids in maestro.label_cases(body or text) for cid in ids}
    status, line = status_of(header)
    tags = _tags(text)
    requires = next((m.group(1) for m in map(RE_REQUIRES.match, (s.strip() for s in header.split("\n"))) if m), "")
    requires = requires or next((TAG_PROFILES[t] for t in tags if t in TAG_PROFILES), "")
    return Flow(name=path.stem, path=path, cases=cases, status=status, status_line=line, tags=tags,
                requires=requires)


def index(flows_root: Path) -> dict[str, Flow]:
    """{flow name: Flow} for every flow under `flows_root` that names at least one case. Sub-flows (tag
    `sub-flow`, or under a `common/` folder) are building blocks, never a case's flow."""
    out: dict[str, Flow] = {}
    for path in sorted(Path(flows_root).rglob("*.yaml")):
        if "common" in path.relative_to(flows_root).parts[:-1]:
            continue
        flow = read_flow(path)
        if flow.cases and SUBFLOW_TAG not in flow.tags:
            out[flow.name] = flow
    return out


def flows_for(flows: dict[str, Flow], extid: str, store_id: str = "") -> list[Flow]:
    """The flows that name this case, by its extid or by its Kiwi id (`KIWI-<store_id>`)."""
    keys = {extid} | ({f"KIWI-{store_id}"} if store_id else set())
    return [f for f in flows.values() if f.cases & keys]


def choose(flows: list[Flow]) -> Flow | None:
    """The flow a case runs as: a validated one with no gate; with several, the one covering fewest
    cases (most specific), then by name so the choice is stable."""
    runnable = [f for f in flows if f.validated and not f.gate()]
    return min(runnable, key=lambda f: (len(f.cases), f.name)) if runnable else None
