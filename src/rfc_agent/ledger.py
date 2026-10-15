"""The scan ledger: a machine-checkable record of what a stage's agent actually did, rendered from the
stage's stream.jsonl (the SDK event log — the ground truth, not the agent's own narrative).

Purpose (M2, per the architecture's ledger-first decision): turn honor-system rules into checked ones.
A `file:line` citation is only trustworthy if the agent demonstrably interacted with that file during the
scan — a path that never appears in any tool interaction was fabricated, however plausible it looks.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class Ledger:
    files_read: list[str] = field(default_factory=list)  # Read tool file_paths
    searches: list[dict] = field(default_factory=list)  # Grep/Glob/Bash-search inputs
    tool_counts: dict[str, int] = field(default_factory=dict)
    witness_text: str = ""  # TOOL interactions only (inputs + results) — the witness corpus

    def witnessed(self, rel_path: str) -> bool:
        """True if the cited path appears in the stage's READ-side tool interactions — a Read/Grep/Glob/Bash
        input, or a tool-result line that carries a `path:line` token (a grep -n hit).

        The corpus is deliberately narrow, tightened twice:
        - It used to be the whole stream, so the agent's own prose counted as witness — merely *writing a
          path* satisfied the check, the exact fabrication this gate exists to catch.
        - It then included every tool input and full result, which had two bypasses: the agent's own
          Write of findings.md put every citation in it into the corpus (self-witnessing), and one broad
          `find`/`ls` listing witnessed thousands of never-opened files. Now write-tool inputs are excluded
          and results witness only lines with a line number — "witnessed" means read or search-hit, not
          "listed once".
        - Finally, interactions with the run's OWN artifacts are excluded end-to-end (input and result, via
          tool_use_id correlation): revision mode requires the agent to READ its previous findings.md, and
          that read's result contains every citation — reading your own writing is not evidence.
        Also accepts the path minus its repo prefix: the agent may have seen it in intra-repo-relative
        form, with auto-qualification adding the prefix afterwards."""
        if rel_path in self.witness_text:
            return True
        suffix = rel_path.split("/", 1)[1] if "/" in rel_path else rel_path
        return bool(suffix) and suffix in self.witness_text

    def to_json(self) -> dict:
        return {
            "files_read": self.files_read,
            "searches": self.searches,
            "tool_counts": self.tool_counts,
            "stats": {"files_read": len(self.files_read), "searches": len(self.searches)},
        }


_SEARCH_TOOLS = {"Grep", "Glob"}
# read-side tools whose INPUTS witness: naming a path here is deliberate targeting. Write/Edit inputs are
# the model's own authored content — witnessing from them let findings.md witness its own citations.
_WITNESS_INPUT_TOOLS = {"Read", "Grep", "Glob", "Bash"}
# a result line witnesses only if it carries a path:line token (grep -n hit) — a bare listing (find/ls/
# Glob output) names paths without inspecting them
_CITED_LINE_RE = re.compile(r"[\w./-]+:\d+")

# The run's OWN artifact names. An interaction whose input references any of these is excluded from the
# witness corpus entirely (input AND result): findings/rfc/plan/review/guidance are FULL of repo citations,
# so reading your own writing would witness every citation in it — a self-witness loophole that becomes
# guaranteed under revision mode, where the agent must read its previous findings.md before editing it.
# Reading your own writing is not evidence.
#
# Matching is COMPONENT-BOUNDED, never bare substring (0.24.17). Bare substring classified the real
# repo file patient_service_torpedo/family_linking/family_linking_plan.md as the run's own plan.md —
# which disproved this comment's old "false positives are fail-SAFE" claim. The truth is split:
# a false positive on an INCIDENTAL file is fail-safe (missed witness -> gate failure -> re-read
# recovers), but a false positive on the CITED file itself is a LIVELOCK — every re-read is
# discarded for the same reason, the gate's "Read it now" remedy can never work, and the run burns
# its whole retry budget on identical failures (field incident 2026-08-20: 4x GROUND, ~$15).
# Boundary matching kills the name-collision class (family_linking_plan.md, security_findings.md,
# myreview.md all witness now). An EXACT own-artifact filename at any path (a repo's own docs/plan.md)
# is still excluded — deliberate residue, and check_citations_witnessed names that misclassification
# outright instead of advising a futile re-read.
#
# Heredocs stay covered: `cat > findings/... <<EOF` trips the findings/ segment. DELIBERATE:
# findings/ also excludes reads of findings/pregrep/ — the harness-generated evidence pack. Do NOT
# carve it out: the run dir is model-writable, so a whitelisted pregrep read would let the model
# Write fake hits and self-witness them (the ground gate re-derives pregrep text from the shard plan
# instead). .jsonl stays a bare suffix: stream/ledger logs are total self-witness bombs and the
# model can copy them anywhere inside the writable run dir.
_OWN_FILE_TOKENS = (
    "findings.md", "answers.md", "rfc.md", "plan.md", "contract.md", "review.md", "ledger",
    "requirements.json", "prd.md", "jira.md", "figma.md", "grooming.md",
)
_OWN_DIR_TOKENS = ("findings", "review", "guidance", "logs")
_B = r"[^a-z0-9_-]"  # chars that CONTINUE a filename: alnum/underscore/dash; anything else bounds it
_OWN_RE = re.compile(
    rf"(?:^|(?<={_B}))(?:{'|'.join(map(re.escape, _OWN_FILE_TOKENS))})(?:$|(?={_B}))"
    rf"|(?:^|(?<={_B}))(?:{'|'.join(_OWN_DIR_TOKENS)})/"
    rf"|\.jsonl(?:$|(?={_B}))"
)


def own_artifact_match(text) -> str | None:
    """The own-artifact token a string trips on, or None — exposed so the witness gate can NAME a
    misclassification (path present in files_read but excluded here) instead of advising a re-read
    that can never succeed."""
    m = _OWN_RE.search(str(text).lower())
    return m.group(0) if m else None


def _references_own_artifacts(inp) -> bool:
    return _OWN_RE.search(json.dumps(inp, default=str).lower()) is not None


def _result_text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(str(b.get("text", "")) for b in content if isinstance(b, dict))
    return json.dumps(content, default=str)


def build_ledger(stream_text: str) -> Ledger:
    led = Ledger()
    witness: list[str] = []  # read-side tool inputs + line-numbered result lines ONLY — never model prose
    # tool_use_id -> whether that call's RESULT may witness. A result witnesses only when its ORIGIN is
    # known and clean: unknown-origin results are excluded (fail-safe — real SDK streams always carry ids).
    result_witnesses: dict[str, bool] = {}
    for line in stream_text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(ev, dict):
            continue
        content = (ev.get("message") if isinstance(ev.get("message"), dict) else {}).get("content")
        blocks = [b for b in content if isinstance(b, dict)] if isinstance(content, list) else []
        if ev.get("type") == "assistant":
            for block in blocks:
                if block.get("type") != "tool_use":
                    continue  # a text block here is the model talking — not evidence of anything
                name = block.get("name") or "?"
                inp = block.get("input") or {}
                led.tool_counts[name] = led.tool_counts.get(name, 0) + 1
                clean = not _references_own_artifacts(inp)
                if block.get("id"):
                    # any tool's clean result may witness (MCP search results included) — the exclusions
                    # are write-tools' inputs (agent-authored content), own-artifact interactions, and
                    # Agent/Task results: a subagent's final report is model PROSE full of path:line
                    # claims, so letting it witness would be the prose-as-evidence bypass all over again
                    # (the subagent's actual tool calls appear in the stream and witness on their own)
                    result_witnesses[block["id"]] = clean and name not in (
                        "Write", "Edit", "NotebookEdit", "Agent", "Task")
                if name in _WITNESS_INPUT_TOOLS and clean:
                    witness.append(json.dumps(inp, default=str))
                if name == "Read" and inp.get("file_path"):
                    led.files_read.append(str(inp["file_path"]))
                elif name in _SEARCH_TOOLS:
                    led.searches.append({"tool": name, **{k: str(v)[:200] for k, v in inp.items()}})
                elif name == "Bash" and isinstance(inp.get("command"), str):
                    cmd = inp["command"]
                    if any(t in cmd for t in ("grep", "rg ", "cat ", "head ", "tail ", "sed ", "find ")):
                        led.searches.append({"tool": "Bash", "command": cmd[:200]})
        elif ev.get("type") == "user":
            for block in blocks:  # tool_result: what the tools actually returned, line-numbered lines only
                if block.get("type") == "tool_result" and result_witnesses.get(block.get("tool_use_id")):
                    witness += [
                        ln for ln in _result_text(block.get("content")).splitlines()
                        if _CITED_LINE_RE.search(ln)
                    ]
    led.witness_text = "\n".join(witness)
    return led


def build_ledger_from_streams(paths: list[Path]) -> Ledger:
    """One ledger over every attempt's stream (revision mode keeps prior attempts as rotated
    `<stage>.stream.<n>.jsonl` files): a citation carried over from attempt 1 was genuinely read during
    THIS run's scanning, so the witness corpus must span all attempts that produced the current output."""
    return build_ledger("\n".join(p.read_text(encoding="utf-8", errors="replace") for p in paths if p.exists()))


_WITNESS_CARRY_CAP = 30_000  # lines; a run's read evidence, not a transcript archive


def write_ledger(streams: list[Path], dest: Path) -> Ledger | None:
    """Render every existing logs/<stage>.stream*.jsonl -> logs/<stage>.ledger.json (None if no stream).
    Takes ALL the stage's streams — rotated attempts and parallel-phase/shard streams alike — so the
    manifest's ledger stats report what the stage really did, not just its last serial attempt (observed
    2026-07-28: attempt 1 did 33 reads/155 searches, the manifest reported attempt 2's 7/13)."""
    streams = [p for p in streams if p.exists()]
    if not streams:
        return None
    led = build_ledger_from_streams(streams)
    dest.write_text(json.dumps(led.to_json(), indent=2))
    return led
