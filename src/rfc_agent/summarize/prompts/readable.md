Write out/rfc.readable.md: a concise copy of out/rfc.md for the humans who will decide on and build this feature.
Read out/rfc.md (the full, final RFC). It is the only source: same decisions, nothing new, and no claim made stronger
or weaker. Aim for {{SIZE_TARGET_KB}} KB.

Structure:
  # <feature> — summary
  one line: Status · Repos · Full RFC: out/rfc.md
  > TL;DR — 2–3 sentences
  ## What changes          3–6 bullets, in plain words
  ## Decision              the chosen option and its 2–3 main reasons; each alternative in one line (why not)
  ## How it works          per flow: its sequence diagram copied unchanged, then 3–6 numbered steps in plain words
  ## API changes           table: endpoint · what changes · who is affected
  ## Build plan            table: # · repo · what · depends on   (one row per CHANGE, same order)
  ## Rollout and risks     short bullets
  ## Open questions        table: question · blocks · suggested answer — every one from the full RFC
  ## ⚠ Unresolved review findings   only if the full RFC has this section — keep every row, shortened

Rules:
- Drop code citations, file paths and line numbers — they stay in the full RFC. Keep names of endpoints, services
  and fields.
- Plain words, short sentences, tables over prose. No pipeline vocabulary (REQ ids, CHANGE ids, flow ids are fine
  in tables; no "tracer", "gate", "facts").
- Keep every open question and every unresolved review finding — never drop a risk to make it shorter.
Reply: done.
