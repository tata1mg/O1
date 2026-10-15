## Review — {{TITLE}}

### BLOCKER: <one-line claim>
- Evidence: <repo/path:line — what the code does> ; <where the RFC says otherwise (§ / CHANGE / endpoint)>
- Failure scenario: <concrete: input / user action → what happens → visible result>
- Fix: <what to change in the RFC, where>

### MAJOR: <one-line claim>
- Evidence: …
- Failure scenario: …
- Fix: …

### MINOR: <one-line claim>
- Evidence: …
- Fix: …

Verdict: <n> blocker, <n> major, <n> minor

Severity: BLOCKER = building it as written breaks something, loses data, or fails a must-have REQ ·
MAJOR = will cause rework (a gap, an inconsistency, an unhandled caller or state) ·
MINOR = clarity or completeness that doesn't change what gets built.
