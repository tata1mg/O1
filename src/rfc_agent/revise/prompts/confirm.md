The lead has answered your review (round {{ROUND}}). For each of YOUR findings below, check the lead's fix-log
entry and the diff of out/rfc.md — verify in the code where it matters — and write {{OUT_FILE}}:

## Confirm — {{REVIEWER}} (round {{ROUND}})

### <id> RESOLVED
- one line: where / how
### <id> NOT RESOLVED
- why, with evidence (repo/path:line)
### <id> ACCEPTED
- the lead's disagreement or open question holds — one line why

You may add NEW BLOCKERs only — and only for problems in the changed parts (the diff). No new MAJOR or MINOR:
### NEW BLOCKER: <one-line claim>
- Evidence: … · Failure scenario: … · Fix: …

Cite code as repo/path:line (never _snap/…); citations are checked as you write.

YOUR FINDINGS AND THE LEAD'S ENTRIES
{{ITEMS}}

DIFF OF out/rfc.md (this round)
{{DIFF}}

Reply: done.
