# RFC shape

The contract for the document Draft produces: `out/rfc.md`. The Draft prompt
([write.md](../src/rfc_agent/draft/prompts/write.md)) and the Draft gate
([gate.py](../src/rfc_agent/draft/gate.py)) implement exactly this. To change the shape, change this file, the
prompt and the gate together.

**Design principle: one document, sliced by flow.** The RFC has one design subsection per *flow* (a user
journey traced end to end), so a reviewer reads one journey from start to finish instead of hunting one
feature across generic sections. The API contract and the implementation plan are sections of the same file,
so they cannot contradict a separate document. The plan is a list of **CHANGE blocks**: each one is a
reviewable unit of work that names its repo, files, dependencies and verification.

**Readability contract.** The audience is a busy engineer. Three rules keep the document readable:

1. **Citations are `repo/path:line`, written where the claim is made.** "Current state" bullets end with the
   citation of the code they describe; CHANGE blocks name files with line numbers for `(modify)` files and
   without for `(new)` files. The repo name comes first, never a snapshot path (`_snap/…`).
2. **Plain words.** Numbered steps and tables; every sentence says what to build or why.
3. **The TL;DR stands alone**: at most 3 sentences — the problem, the chosen mechanism, the one number that
   matters.

## `out/rfc.md`

```markdown
# RFC: <feature>
| Jira | Status | Repos (commit) | PRD |
> TL;DR — ≤ 3 sentences

## 1. Context & problem
## 2. Requirements coverage
## 3. Options
## Recommendation: <option>
## 4. Design
### 4.<n> <flow id> <flow name>
## 5. API contract
## 6. Implementation plan
### CHANGE-<n> <repo>: <title> [REQ-…]
## 7. Rollout, monitoring, risks
## 8. Open questions
## References
```

| Section | Contains | Produced by |
|---|---|---|
| Header + TL;DR | Jira key, status (Draft), repos with the commit each snapshot was taken at, PRD link; the TL;DR | Lead (repos and links completed by Python) |
| **1. Context & problem** | Why now, from the PRD and Jira. No solutioning | Lead |
| **2. Requirements coverage** | A table: one row per REQ — id · summary · status | **Python**, from `out/coverage.json` |
| **3. Options** | 2–3 genuinely different overall approaches (Option · Idea · Pros · Cons), then `## Recommendation: <option>` with ≤ 4 reasons, judged in order: scalability → loose coupling → fit with existing code → operational simplicity → the PRD's constraints | Lead |
| **4. Design** | One subsection per flow in `flows.json`. Each has a Mermaid `sequenceDiagram`, **Current state** (≤ 5 bullets, each cited), **Change** (numbered steps: which repo does what, with which data) and **Edge cases & failure behaviour** | Lead |
| **5. API contract** | Every new or changed endpoint, internal ones included: request, response per outcome (exact nesting), errors table (status · code · when), callers today and whether they are affected | Lead |
| **6. Implementation plan** | CHANGE blocks in build order (below) | Lead |
| **7. Rollout, monitoring, risks** | How it ships, what is watched, what could go wrong | Lead |
| **8. Open questions** | Table: id · question · blocks (REQ ids) · suggested answer · owner. Answered questions are facts, not questions | Lead, starting from `open_questions.json` |
| **⚠ Unresolved review findings** | Only if Revise leaves findings open; inserted before §8 | **Python** (Revise) |
| **References** | PRD, Jira, fetched Confluence pages, Figma links, each repo with its snapshot commit | **Python** |

### Requirement coverage statuses

Each REQ in `reqs.json` appears in `out/coverage.json` with exactly one status:

| Status | Rendered as | Meaning |
|---|---|---|
| `covered` | ✅ Covered (§4.1) | The design settles it, and the sections named exist |
| `assumes` | 🟡 Covered, assumes Q2 (§4.1) | A concrete design that rests on an assumption; the assumption **must** be an open question in §8 |
| `deferred` | ⏸ Deferred — note | Descoped, with the reason |
| `open` | ❓ Open — Q1 | No design possible until the question in §8 is answered |

### CHANGE blocks

```markdown
### CHANGE-1 patient_service: opt-in duplicate gate on create            [REQ-3, REQ-4]
- repo: patient_service
- files: app/managers/patient_crud_manager.py:236 (modify) · app/utils/name_matching.py (modify) ·
         tests/test_duplicate_gate.py (new)
- what: numbered steps
- depends on: —
- verify: the test or check that proves it, including the unchanged behaviour for callers that don't opt in
```

Rules: blocks are numbered in build order; `depends on` names only earlier blocks and may not form a cycle;
`(modify)` files must exist in the repo's snapshot; `(new)` files must **not** already exist (a "new" file that
exists means the plan misread the code). Every REQ is planned in some CHANGE block, or is deferred or open in
the coverage table.

### Size and style

The size target is 25–40 KB. It is stated in the prompt and flagged by the gate, never enforced: cutting to
fit would cost the coverage table or the citations, which matter more.

## What enforces the shape

The Draft gate checks, mechanically: citations resolve; "not found" claims are not contradicted by a search;
coverage is complete and valid; sections 1–8 and a `Recommendation` heading exist, with one design subsection
per flow; CHANGE blocks are consistent; no secrets; Mermaid lint passes. It flags missing REQ or question ids,
uncited "Current state" bullets, and size or TL;DR overruns. Details: [DRAFT.md](DRAFT.md#the-gate).
