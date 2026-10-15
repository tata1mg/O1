Now write the RFC. You have everything: work/reqs.json, work/flows.json, facts/, work/understanding.md,
work/open_questions.json{{ANSWERS}}. Read code yourself only to settle a doubt (about {{CODE_READS}} reads at
most). Do not start new investigations: a gap is an open question.

Write out/rfc.md — plain words, numbered steps, tables; every sentence tells the reader what to build or why.
Aim for {{SIZE_TARGET_KB}} KB.
  # RFC: <feature>
  header table (Jira · Status: Draft · Repos · PRD) · TL;DR (≤ 3 sentences: the problem, the chosen mechanism,
  the one number that matters)
  ## 1. Context & problem — why now; no solutioning
  ## 2. Requirements coverage — heading only; the table is generated from out/coverage.json
  ## 3. Options — 2–3 genuinely different overall approaches as a table (Option · Idea · Pros · Cons),
     then "## Recommendation: <option>" with ≤ 4 reasons (scalability → loose coupling → fit with existing
     code → operational simplicity → the PRD's constraints). Smaller choices inside the design are stated where
     they apply, with a one-line reason.
  ## 4. Design — one subsection per flow, numbered "### 4.<n> <flow id> <name>":
     a Mermaid sequenceDiagram (participants = the repos/services on the flow) ·
     **Current state** (≤ 5 bullets, each ending with its citation repo/path:line) ·
     **Change** (numbered steps: which repo does what, with which data) ·
     **Edge cases & failure behaviour**
  ## 5. API contract — every new or changed endpoint (internal ones too): request · response per outcome ·
     errors table (status · code · when) · callers today and whether they are affected. JSON examples. Shapes
     must match the facts — nesting matters; say what each existing caller reads.
  ## 6. Implementation plan — "### CHANGE-<n> <repo>: <title> [REQ-…]" blocks in build order, each with:
     repo · files (existing files as repo/path:line (modify); new files without a line number (new)) · what
     (numbered steps) · depends on (earlier CHANGE ids or —) · verify (the test/check that proves it)
  ## 7. Rollout, monitoring, risks
  ## 8. Open questions — table: id · question · blocks (REQ ids) · suggested answer · owner. Start from
     open_questions.json; add new ones, remove ones the design settled; answered ones are facts, not questions.
  Do not write a References section — it is generated.

Write out/coverage.json — one entry per REQ id in work/reqs.json:
  {"REQ-1": {"status": "covered", "sections": ["4.1"]},
   "REQ-4": {"status": "assumes", "sections": ["4.1"], "question": "Q2", "note": "what is assumed"},
   "REQ-9": {"status": "deferred", "note": "why"},
   "REQ-12": {"status": "open", "question": "Q1"}}
  status: covered | assumes | deferred | open. "assumes" and "open" name a question id that appears in §8;
  "sections" are design/contract/plan section numbers that exist as headings (e.g. "4.1", "5", "6").

Cite code as repo/path:line (repo name first, never _snap/…). Citations are checked as you write out/rfc.md; fix
anything the check reports straight away.
Every constraint in work/understanding.md (seams, shared callers, settled conflicts) must be visibly handled in
the design. A flow marked UNTRACED: say so in its section; its REQs are "assumes" or "open".
Reply: done.
