The independent review found the problems below (review/review.md has the full text and evidence for each id).
{{TRIAGE}}
Fix them in out/rfc.md / out/coverage.json, in place — don't rewrite untouched sections. A design change must be
carried into every part it affects: the flow's §4 section, the §5 contract, the §6 CHANGE blocks, coverage, §8.
Cite code as repo/path:line; citations are checked as you write.

For EACH finding listed under "Findings" write an entry in review/fixes.md under the heading "## Round {{ROUND}}"
(append to the file; keep earlier rounds):
  ### <id> FIXED     then "- Changed: …" — what changed, where (sections / CHANGE ids)
  ### <id> DISAGREE  then "- Why: …" — with a code citation (repo/path:line) or a PRD quote; nothing changed
  ### <id> OPEN      then "- Question: Qn …" — the open question you added to §8; only when a human must decide
{{MINORS}}
Findings:
{{FINDINGS}}
Reply: done.
