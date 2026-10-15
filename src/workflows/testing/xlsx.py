"""The two-sheet workbook — a DERIVED render of cases.json/scenarios.json, plus a reader for importing one.

D8: JSON is canonical, the xlsx is a render. In test_agent the workbook was the ONLY artifact and the model
built it itself with unrestricted Bash, so nothing checked the sheet names, the columns or the enums. Here
the render is deterministic Python and the gate can check the file it just wrote.

Writing needs openpyxl (optional extra `testing`); the stage skips with a recorded decision when it is
absent. READING needs nothing — the reader is stdlib zip+XML, ported from test_agent
importer/import_cases.py:40-145, because `--from-cases` on an existing workbook has to work anywhere.

The reader carries the survey §6.1 BUG FIX: `import_cases.parse_cases` had no `execution` key in its
column map, so every case imported from a workbook lost its `Execution` column, never got an `exec:` tag,
and was reported `skipped_wrong_execution` at run time. Verified offline against a synthetic workbook whose
Execution column said `http`: the tags came back as extid/feature/scenario/surface only.
"""

from __future__ import annotations

import re
import zipfile
from pathlib import Path

import defusedxml.ElementTree as ET  # the workbook is user-supplied; defusedxml blocks entity-expansion attacks

from .schema import (
    EXECUTION_ENUM,
    PRIORITY_ENUM,
    SCENARIO_TYPES,
    STATUS_ENUM,
    SURFACE_ENUM,
)

SCENARIO_SHEET = "Test Scenarios"
CASE_SHEET = "Test Cases"

#: (header, case-dict key) in delivered column order — SKILL.md:91.
CASE_COLUMNS: tuple[tuple[str, str], ...] = (
    ("Test Case ID", "id"),
    ("Linked Scenario ID", "scenario_id"),
    ("Module", "module"),
    ("Test Case Title", "title"),
    ("Priority", "priority"),
    ("Execution", "execution"),
    ("Surface", "surface"),
    ("Precondition", "precondition"),
    ("Test Steps", "steps"),
    ("Test Data", "test_data"),
    ("Expected Result", "expected"),
    ("Scenario Type", "type"),
    ("Status", "status"),
    ("QA Comment", "qa_comment"),
)
#: SKILL.md:76.
SCENARIO_COLUMNS: tuple[tuple[str, str], ...] = (
    ("Scenario ID", "id"),
    ("Module", "module"),
    ("Feature", "feature"),
    ("Scenario Description", "description"),
    ("Scenario Type", "type"),
    ("Priority", "priority"),
    ("Pre-condition", "precondition"),
    ("Expected Outcome", "expected"),
    ("QA Comment", "qa_comment"),
)
#: Data-validation dropdowns the SKILL's Deliverable Format section requires.
_DROPDOWNS = {
    "Priority": PRIORITY_ENUM,
    "Execution": EXECUTION_ENUM,
    "Surface": SURFACE_ENUM,
    "Scenario Type": SCENARIO_TYPES,
    "Status": STATUS_ENUM,
}
FONT_NAME = "Arial"
_WIDE_COLUMNS = {"Test Steps", "Expected Result", "Scenario Description", "Expected Outcome",
                 "QA Comment", "Test Data", "Precondition", "Pre-condition"}
_WIDE_WIDTH, _NARROW_WIDTH = 60, 18


class OpenpyxlMissing(RuntimeError):
    """openpyxl is not installed — install the optional extra: `uv sync --extra testing`."""


def available() -> bool:
    try:
        import openpyxl  # noqa: F401
    except ImportError:
        return False
    return True


def write_workbook(path: Path, cases: list[dict], scenarios: list[dict]) -> Path:
    """Render both sheets. Raises OpenpyxlMissing when the extra is absent."""
    try:
        from openpyxl import Workbook
        from openpyxl.styles import Alignment, Font
        from openpyxl.utils import get_column_letter
        from openpyxl.worksheet.datavalidation import DataValidation
    except ImportError as exc:
        raise OpenpyxlMissing(
            "openpyxl is required to render the workbook — install the optional extra "
            "(`uv sync --extra testing`) or disable the render (`--disable testing.xlsx`)."
        ) from exc

    wb = Workbook()
    for sheet_name, columns, rows in (
        (SCENARIO_SHEET, SCENARIO_COLUMNS, scenarios),
        (CASE_SHEET, CASE_COLUMNS, cases),
    ):
        ws = wb.active if sheet_name == SCENARIO_SHEET else wb.create_sheet()
        ws.title = sheet_name
        ws.append([h for h, _ in columns])
        for row in rows:
            ws.append([str(row.get(key, "") or "") for _, key in columns])
        for cell in ws[1]:
            cell.font = Font(name=FONT_NAME, bold=True)
        for r in ws.iter_rows(min_row=2):
            for cell in r:
                cell.font = Font(name=FONT_NAME)
                cell.alignment = Alignment(wrap_text=True, vertical="top")
        ws.freeze_panes = "A2"
        ws.auto_filter.ref = ws.dimensions
        for idx, (header, _) in enumerate(columns, start=1):
            letter = get_column_letter(idx)
            ws.column_dimensions[letter].width = _WIDE_WIDTH if header in _WIDE_COLUMNS else _NARROW_WIDTH
            allowed = _DROPDOWNS.get(header)
            if allowed and rows:
                dv = DataValidation(type="list", formula1='"' + ",".join(allowed) + '"', allow_blank=False)
                ws.add_data_validation(dv)
                dv.add(f"{letter}2:{letter}{len(rows) + 1}")
    path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(path)
    return path


# ---- stdlib reader (no dependency) -----------------------------------------------------------------
_NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"


def _shared_strings(z: zipfile.ZipFile) -> list[str]:
    if "xl/sharedStrings.xml" not in z.namelist():
        return []
    root = ET.fromstring(z.read("xl/sharedStrings.xml"))
    return ["".join(t.text or "" for t in si.iter(f"{_NS}t")) for si in root.findall(f"{_NS}si")]


def _sheet_map(z: zipfile.ZipFile) -> list[tuple[str, str]]:
    wb = ET.fromstring(z.read("xl/workbook.xml"))
    rels = ET.fromstring(z.read("xl/_rels/workbook.xml.rels"))
    rns = "{http://schemas.openxmlformats.org/package/2006/relationships}"
    tgt = {r.get("Id"): r.get("Target") for r in rels.findall(f"{rns}Relationship")}
    out = []
    for s in wb.iter(f"{_NS}sheet"):
        rid = s.get("{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id")
        path = tgt.get(rid, "").lstrip("/")
        if not path.startswith("xl/"):
            path = "xl/" + path
        out.append((s.get("name"), path))
    return out


def _col_row(ref: str) -> tuple[int, int]:
    m = re.match(r"([A-Z]+)(\d+)", ref)
    letters, row = m.group(1), int(m.group(2))
    c = 0
    for ch in letters:
        c = c * 26 + (ord(ch) - 64)
    return c - 1, row


def _read_sheet(z: zipfile.ZipFile, ss: list[str], path: str) -> list[list[str]]:
    root = ET.fromstring(z.read(path))
    rows: dict[int, dict[int, str]] = {}
    for c in root.iter(f"{_NS}c"):
        ref, typ = c.get("r"), c.get("t")
        v = c.find(f"{_NS}v")
        val = ""
        if v is not None and v.text is not None:
            val = ss[int(v.text)] if typ == "s" else v.text
        else:
            isn = c.find(f"{_NS}is")
            if isn is not None:
                val = "".join(t.text or "" for t in isn.iter(f"{_NS}t"))
        col, row = _col_row(ref)
        rows.setdefault(row, {})[col] = val
    if not rows:
        return []
    width = max(max(r) for r in rows.values()) + 1
    return [[rows.get(rn, {}).get(i, "") for i in range(width)] for rn in sorted(rows)]


def read_sheets(path: str | Path) -> dict[str, list[list[str]]]:
    with zipfile.ZipFile(path) as z:
        ss = _shared_strings(z)
        return {name: _read_sheet(z, ss, p) for name, p in _sheet_map(z)}


def _norm(h: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", (h or "").lower()).strip("_")


def _pick(header: list[str], *candidates: str) -> int | None:
    idx = {_norm(h): i for i, h in enumerate(header)}
    for cand in candidates:
        if _norm(cand) in idx:
            return idx[_norm(cand)]
    return None


def _rows_as_dicts(sheet: list[list[str]], colmap: dict[str, int | None]) -> list[dict]:
    out = []
    for row in sheet[1:]:
        if not any((c or "").strip() for c in row):
            continue
        out.append({k: (row[i].strip() if (i is not None and i < len(row)) else "")
                    for k, i in colmap.items()})
    return out


def parse_cases(sheets: dict[str, list[list[str]]]) -> list[dict]:
    """The Test Cases sheet as canonical case dicts. Header aliases match test_agent's."""
    name = next((n for n in sheets if "case" in n.lower()), None)
    if not name:
        raise ValueError("no 'Test Cases' sheet in the workbook")
    sheet = sheets[name]
    if not sheet:
        return []
    h = sheet[0]
    colmap = {
        "id": _pick(h, "Test Case ID", "Case ID", "TC ID"),
        "scenario_id": _pick(h, "Linked Scenario ID", "Scenario ID"),
        "module": _pick(h, "Module"),
        "title": _pick(h, "Test Case Title", "Title", "Summary"),
        "priority": _pick(h, "Priority"),
        # THE FIX (survey §6.1): absent from test_agent's colmap, so `exec:` was never tagged and every
        # imported case was unrunnable.
        "execution": _pick(h, "Execution"),
        "surface": _pick(h, "Surface"),
        "precondition": _pick(h, "Precondition", "Pre-condition"),
        "steps": _pick(h, "Test Steps", "Steps"),
        "test_data": _pick(h, "Test Data"),
        "expected": _pick(h, "Expected Result", "Expected Outcome"),
        "type": _pick(h, "Scenario Type", "Type"),
        "status": _pick(h, "Status"),
        "requirement": _pick(h, "Requirement Ref", "Requirement"),
        "qa_comment": _pick(h, "QA Comment", "Comment"),
    }
    return _rows_as_dicts(sheet, colmap)


def parse_scenarios(sheets: dict[str, list[list[str]]]) -> list[dict]:
    """The Test Scenarios sheet as canonical scenario dicts."""
    name = next((n for n in sheets if "scenario" in n.lower()), None)
    if not name:
        return []
    sheet = sheets[name]
    if not sheet:
        return []
    h = sheet[0]
    colmap = {
        "id": _pick(h, "Scenario ID"),
        "module": _pick(h, "Module"),
        "feature": _pick(h, "Feature"),
        "description": _pick(h, "Scenario Description", "Description"),
        "type": _pick(h, "Scenario Type", "Type"),
        "priority": _pick(h, "Priority"),
        "precondition": _pick(h, "Pre-condition", "Precondition"),
        "expected": _pick(h, "Expected Outcome", "Expected Result"),
        "qa_comment": _pick(h, "QA Comment", "Comment"),
        "surface": _pick(h, "Surface"),
    }
    return _rows_as_dicts(sheet, colmap)
