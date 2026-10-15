The tracers are done. Their facts are in {{FACTS_FILES}}, the UI facts in {{UI_FILES}}.
Check results per agent (must-fix items that stayed unfixed = UNVERIFIED claims, and flags) are in
{{CHECK_FILES}}.
{{UNTRACED}}
Compare them across flows:
- seams: does one hop's output shape match what the next hop (or the UI) expects?
- conflicts between tracers; shared callers hit by more than one flow;
- a flow covered by two UI readers: merge their blocks; screens listed as Unmapped: place or drop them;
- unknowns the code can answer — check the code yourself (Read / Grep on _snap/) and settle them;
- unverified claims — verify or drop each one; flags (empty sections, unanswered verify items) — resolve
  from code or turn into open questions;
- REQ coverage: every REQ with flows has facts behind it; list gaps.

Write:
- work/understanding.md — sections: Cross-flow constraints · Settled conflicts · Shared blast radius ·
  Coverage notes · Corrections to the plan. Cite code as repo/path:line.
- work/open_questions.json — {"questions": [{"id": "Q1", "question": "...", "blocks": ["REQ-3"],
  "suggested_answer": "... or null", "why_open": "..."}]}

Do not design yet. Reply: done.
