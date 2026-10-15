"""The canonical suite schema — and the validators that turn the generator SKILL's prose into gates.

`qa/cases.json` and `qa/scenarios.json` are the workflow's primary artifacts (D8). Everything the
generator SKILL.md states as a rule and nothing enforced ("every column is mandatory", the ID convention,
the TYPE table, the enums, the prefix-swap linkage, "every module needs a Negative and an Edge Case") is
mechanised here. WHY: in test_agent those rules lived only in a 350-line prompt and a shell heredoc that
restated half of them, so a suite that broke them was delivered, upserted and executed with nothing
noticing (survey §3.1: "Nothing in this skill is enforced by code").

Field names are kiwi_writer's canonical case dict (importer/kiwi_writer.py:10-22) plus the three columns
both of test_agent's write paths silently dropped — `requirement`, `qa_comment`, `type` (survey §6.1).
"""

from __future__ import annotations

import re
from typing import Any

# ---- vocabulary ------------------------------------------------------------------------------------
#: Scenario Type -> ID TYPE code. Verbatim from skills/qa-test-case-generator/SKILL.md:52-60.
TYPE_CODES: dict[str, str] = {
    "Functional": "FUNC",
    "Negative": "NEG",
    "Edge Case": "EDGE",
    "Validation": "VALID",
    "UI/UX": "UI",
    "Security": "SEC",
    "Error Handling": "ERR",
    "Permission": "PERM",
    "Data Validation": "DVAL",
    "Boundary": "BOUND",
    "Regression": "REGR",
    "API": "API",
    "Performance": "PERF",
    "State Handling": "STATE",
}
TYPE_ENUM: tuple[str, ...] = tuple(TYPE_CODES.values())
SCENARIO_TYPES: tuple[str, ...] = tuple(TYPE_CODES)

EXECUTION_ENUM: tuple[str, ...] = ("http", "ui", "manual")  # SKILL.md:96
SURFACE_ENUM: tuple[str, ...] = ("backend", "android", "ios", "web")  # SKILL.md:98
STATUS_ENUM: tuple[str, ...] = ("Passed", "Failed", "Blocked", "Not Tested")  # SKILL.md:94
PRIORITY_ENUM: tuple[str, ...] = ("Critical", "High", "Medium", "Low")

#: TS-<FEATURE>-<MODULE>-<TYPE>-<NN> / TC-... (SKILL.md:39-68). FEATURE is a 2-6 letter mnemonic OR a full
#: epic key including its number (`LABS26734`), so the segment is alphanumeric and starts with a letter.
ID_RE = re.compile(rf"^T[SC]-[A-Z][A-Z0-9]{{1,11}}-[A-Z0-9]+-(?:{'|'.join(TYPE_ENUM)})-\d{{2}}$")

#: Every Section-2 column is mandatory on every row ("n/a" allowed, blank never — SKILL.md:101).
CASE_FIELDS: tuple[str, ...] = (
    "id", "scenario_id", "module", "title", "priority", "execution", "surface",
    "precondition", "steps", "test_data", "expected", "type", "status", "qa_comment",
)
#: Carried through the whole pipeline but not required to be non-empty (not a delivered column).
CASE_OPTIONAL_FIELDS: tuple[str, ...] = ("requirement",)

SCENARIO_FIELDS: tuple[str, ...] = (
    "id", "module", "feature", "description", "type", "priority",
    "precondition", "expected", "qa_comment",
)
SCENARIO_OPTIONAL_FIELDS: tuple[str, ...] = ("surface",)


def type_code(scenario_type: str) -> str | None:
    """'Edge Case' -> 'EDGE'. None when the value is not in the SKILL's table."""
    return TYPE_CODES.get((scenario_type or "").strip())


def id_type(case_id: str) -> str | None:
    """The TYPE segment of a well-formed id, else None."""
    parts = (case_id or "").split("-")
    return parts[-2] if len(parts) >= 5 and ID_RE.match(case_id or "") else None


def id_module(case_id: str) -> str | None:
    """The MODULE segment of a well-formed id, else None. MODULE may not itself contain '-'."""
    parts = (case_id or "").split("-")
    return parts[2] if len(parts) == 5 and ID_RE.match(case_id or "") else None


def swap_prefix(case_id: str) -> str:
    """TC-X-Y-FUNC-01 <-> TS-X-Y-FUNC-01. The linkage rule is a pure prefix swap (SKILL.md:66)."""
    if case_id.startswith("TC-"):
        return "TS-" + case_id[3:]
    if case_id.startswith("TS-"):
        return "TC-" + case_id[3:]
    return case_id


def _blank(v: Any) -> bool:
    return not str(v or "").strip()


# ---- validators ------------------------------------------------------------------------------------
# Each returns a list of human-readable violations; [] means pass. Every violation names the offending
# id, because a gate failure is fed back to the model as guidance and "some case is wrong" is unactionable.


def validate_case(case: dict) -> list[str]:
    """One case dict against the schema: shape, id convention, enums, mandatory fields, linkage."""
    cid = str(case.get("id") or "<no id>")
    out: list[str] = []
    if not ID_RE.match(str(case.get("id") or "")):
        out.append(f"{cid}: id does not match TC-<FEATURE>-<MODULE>-<TYPE>-<NN> "
                   f"with TYPE in {list(TYPE_ENUM)}")
    elif not str(case["id"]).startswith("TC-"):
        out.append(f"{cid}: a test case id must start with 'TC-'")
    for field in CASE_FIELDS:
        if field not in case:
            out.append(f"{cid}: missing field {field!r}")
        elif _blank(case[field]):
            out.append(f"{cid}: {field!r} is blank — every column is mandatory "
                       f"(write an accurate 'n/a' where the field truly does not apply)")
    if (ex := case.get("execution")) and ex not in EXECUTION_ENUM:
        out.append(f"{cid}: execution={ex!r} not in {list(EXECUTION_ENUM)}")
    if (sf := case.get("surface")) and sf not in SURFACE_ENUM:
        out.append(f"{cid}: surface={sf!r} not in {list(SURFACE_ENUM)}")
    if (st := case.get("status")) and st not in STATUS_ENUM:
        out.append(f"{cid}: status={st!r} not in {list(STATUS_ENUM)}")
    if (pr := case.get("priority")) and pr not in PRIORITY_ENUM:
        out.append(f"{cid}: priority={pr!r} not in {list(PRIORITY_ENUM)}")
    if (ty := case.get("type")) and ty not in SCENARIO_TYPES:
        out.append(f"{cid}: type={ty!r} not in {list(SCENARIO_TYPES)}")
    elif ty and (code := id_type(str(case.get("id") or ""))) and code != TYPE_CODES[ty]:
        out.append(f"{cid}: type={ty!r} implies TYPE code {TYPE_CODES[ty]}, but the id says {code}")
    sid = str(case.get("scenario_id") or "")
    if sid and sid != swap_prefix(str(case.get("id") or "")):
        out.append(f"{cid}: scenario_id={sid!r} is not this id with TS-/TC- swapped "
                   f"({swap_prefix(str(case.get('id') or ''))!r}) — the linkage must be a pure prefix swap")
    return out


def validate_scenario(scenario: dict) -> list[str]:
    """One scenario dict: shape, id convention, enums."""
    sid = str(scenario.get("id") or "<no id>")
    out: list[str] = []
    if not ID_RE.match(str(scenario.get("id") or "")):
        out.append(f"{sid}: id does not match TS-<FEATURE>-<MODULE>-<TYPE>-<NN> "
                   f"with TYPE in {list(TYPE_ENUM)}")
    elif not str(scenario["id"]).startswith("TS-"):
        out.append(f"{sid}: a scenario id must start with 'TS-'")
    for field in SCENARIO_FIELDS:
        if field not in scenario:
            out.append(f"{sid}: missing field {field!r}")
        elif _blank(scenario[field]):
            out.append(f"{sid}: {field!r} is blank — every scenario column is mandatory")
    if (ty := scenario.get("type")) and ty not in SCENARIO_TYPES:
        out.append(f"{sid}: type={ty!r} not in {list(SCENARIO_TYPES)}")
    elif ty and (code := id_type(str(scenario.get("id") or ""))) and code != TYPE_CODES[ty]:
        out.append(f"{sid}: type={ty!r} implies TYPE code {TYPE_CODES[ty]}, but the id says {code}")
    if (pr := scenario.get("priority")) and pr not in PRIORITY_ENUM:
        out.append(f"{sid}: priority={pr!r} not in {list(PRIORITY_ENUM)}")
    if (sf := scenario.get("surface")) and sf not in SURFACE_ENUM:
        out.append(f"{sid}: surface={sf!r} not in {list(SURFACE_ENUM)}")
    return out


def validate_linkage(cases: list[dict], scenarios: list[dict], *, require_case_per_scenario: bool = True) -> list[str]:
    """Cross-sheet invariants: no orphan case, no unrealised scenario, no duplicate id."""
    out: list[str] = []
    sids = {str(s.get("id") or "") for s in scenarios}
    for dup in sorted(_duplicates(str(s.get("id") or "") for s in scenarios)):
        out.append(f"{dup}: duplicate scenario id")
    for dup in sorted(_duplicates(str(c.get("id") or "") for c in cases)):
        out.append(f"{dup}: duplicate test case id")
    for c in cases:
        sid = str(c.get("scenario_id") or "")
        if sid and sid not in sids:
            out.append(f"{c.get('id')}: linked scenario {sid} is not in scenarios.json")
    if require_case_per_scenario:
        linked = {str(c.get("scenario_id") or "") for c in cases}
        for sid in sorted(sids - linked):
            out.append(f"{sid}: scenario has no test case — every scenario must be realised "
                       f"(SKILL.md Output Format Rule 6)")
    return out


def validate_neg_edge_per_module(scenarios: list[dict]) -> list[str]:
    """Every module needs >=1 Negative and >=1 Edge Case scenario (SKILL.md Output Format Rule 8)."""
    by_module: dict[str, set[str]] = {}
    for s in scenarios:
        mod = str(s.get("module") or id_module(str(s.get("id") or "")) or "<no module>")
        by_module.setdefault(mod, set()).add(type_code(str(s.get("type") or "")) or "")
    out = []
    for mod in sorted(by_module):
        missing = [want for want in ("NEG", "EDGE") if want not in by_module[mod]]
        if missing:
            out.append(f"module {mod!r}: no {' and no '.join(missing)} scenario — a module with only "
                       f"happy-path coverage is incomplete")
    return out


def validate_suite(cases: list[dict], scenarios: list[dict], *, require_case_per_scenario: bool = True) -> list[str]:
    """Every deterministic schema rule, in one call. This is what the generate_cases gate runs."""
    out: list[str] = []
    if not cases:
        out.append("cases.json is empty — the suite has no test cases")
    if not scenarios:
        out.append("scenarios.json is empty — the suite has no scenarios")
    for c in cases:
        out += validate_case(c)
    for s in scenarios:
        out += validate_scenario(s)
    out += validate_linkage(cases, scenarios, require_case_per_scenario=require_case_per_scenario)
    out += validate_neg_edge_per_module(scenarios)
    return out


def _duplicates(values) -> set[str]:
    seen, dup = set(), set()
    for v in values:
        if v in seen:
            dup.add(v)
        seen.add(v)
    return dup


def http_cases(cases: list[dict]) -> list[dict]:
    """The subset the BE executor would select — `execution: http` (SKILL.md:157-159)."""
    return [c for c in cases if str(c.get("execution") or "").lower() == "http"]


def render_text(case: dict) -> str:
    """The case's steps as one markdown blob — the store's transport for the step grammar.

    Verbatim shape from importer/kiwi_writer.py:29-39, because `be_spec.parse_case` reads it back and the
    Kiwi backend must round-trip byte-identically with the JSON one.
    """
    parts = []
    if case.get("precondition"):
        parts.append(f"**Precondition**\n\n{case['precondition']}")
    if case.get("steps"):
        parts.append(f"**Test Steps**\n\n{case['steps']}")
    if case.get("test_data"):
        parts.append(f"**Test Data**\n\n{case['test_data']}")
    if case.get("expected"):
        parts.append(f"**Expected Result**\n\n{case['expected']}")
    return "\n\n".join(parts)


def case_tags(case: dict, feature: str) -> list[str]:
    """extid:/feature:/scenario:/surface:/exec: — verbatim from kiwi_writer.case_tags (:42-54)."""
    tags = [f"extid:{case['id']}", f"feature:{feature}"]
    if case.get("scenario_id"):
        tags.append(f"scenario:{case['scenario_id']}")
    if case.get("surface"):
        tags.append(f"surface:{str(case['surface']).lower()}")
    # How the case is executed — NOT the same question as `surface`. Absent = the generator did not
    # classify it, and an unclassified case is not runnable at all (never "runs by default").
    if case.get("execution"):
        tags.append(f"exec:{str(case['execution']).lower()}")
    return tags
