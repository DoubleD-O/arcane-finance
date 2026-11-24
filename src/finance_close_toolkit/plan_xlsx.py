"""plan_<name>_<scenario>.xlsx: assumptions, the three statements and schedules for every month with one
column per fiscal year, and the checks. Values only, no formulas."""

from __future__ import annotations

from pathlib import Path
from typing import Iterable

from openpyxl import Workbook
from openpyxl.styles import Font

from .plan_config import assumption_rows, part_years_text
from .planning import Model, Statement
from .xlsx import AMOUNT, QUANTITY, _append, _header


def _months_sheet(sheet, m: Model, blocks: Iterable[Statement]) -> None:
    """Each block: its title row, then one row per line with a value per month and per fiscal year."""
    width = len(m.months) + len(m.years)
    _header(sheet, ("Line", *(x.label for x in m.months), *(y.heading for y in m.years), "Schedule"))
    for block in blocks:
        _append(sheet, [block.title])
        sheet.cell(sheet.max_row, 1).font = Font(bold=True)
        for line in block.lines:
            _append(sheet, [line.label, *line.values, *(line.for_year(y) for y in m.years), line.schedule or ""],
                    {c: QUANTITY if line.quantity else AMOUNT for c in range(2, width + 2)})
            if line.total:
                sheet.cell(sheet.max_row, 1).font = Font(bold=True)
    sheet.column_dimensions["A"].width = 36


def write_plan_xlsx(path: Path, m: Model) -> Path:
    wb = Workbook()
    wb.properties.creator = "finance-close-toolkit"
    ws = wb.active
    ws.title = "Assumptions"
    _header(ws, ("Section", "Item", "Key", "Value"))
    _append(ws, ["plan", "", "name", m.plan.name])
    _append(ws, ["plan", "", "version", m.plan.version])
    _append(ws, ["plan", "", "scenario", m.scenario])
    for y in m.years:
        _append(ws, ["fiscal year", "", y.label, f"{y.months[0].label} to {y.months[-1].label}"])
    for c in m.changes:
        _append(ws, ["scenario change", "", c.path, f"{c.before} -> {c.after}"])
    for row in assumption_rows(m.plan):
        _append(ws, row)
    for col, width in zip("ABCD", (14, 10, 36, 60)):
        ws.column_dimensions[col].width = width
    for statement in m.statements:
        _months_sheet(wb.create_sheet(statement.title), m, [statement])
    _months_sheet(wb.create_sheet("Schedules"), m, m.schedules)
    checks = wb.create_sheet("Checks")
    _header(checks, ("Check", "Result", "Detail"))
    if m.pnl_only_reason:
        _append(checks, ["Mode", "P&L only", m.pnl_only_reason])
    for c in m.checks:
        _append(checks, [c.name, c.status, c.detail])
    part = part_years_text(m.years)
    if part:
        _append(checks, ["Fiscal years", "", part])
    for w in m.warnings:
        _append(checks, ["Warning", "", w])
    for col, width in zip("ABC", (64, 10, 90)):
        checks.column_dimensions[col].width = width
    wb.save(path)
    return path
