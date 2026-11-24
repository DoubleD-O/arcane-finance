"""release.xlsx: the VarianceReport, the release facts and the manifest as a workbook."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Iterable

from openpyxl import Workbook
from openpyxl.styles import Font

from .release import FACT_COLUMNS, Release, ReleaseFact, export_order, safe_text
from .variance import VarianceReport, release_report

if TYPE_CHECKING:
    from .config import Workspace
    from .store import Store

AMOUNT = "#,##0.00;(#,##0.00)"
QUANTITY = "#,##0;(#,##0)"  # units; the cell keeps the exact value
PERCENT = "0.00%"  # Excel percent: the cell holds the fraction (0.0159 shows as 1.59%)
POINTS = '+0.00" pp";-0.00" pp";0.00" pp"'
PNL_SHEET = "P&L vs budget"
PNL_COLUMNS = ("Scope", "Line", "Actual", "Budget", "Variance", "Variance %", "Label")


def _append(sheet, values: Iterable[object], formats: dict[int, str] | None = None) -> None:
    """Text is always written as a string value, never a formula."""
    sheet.append([safe_text(v) if isinstance(v, str) else v for v in values])
    for col, fmt in (formats or {}).items():
        sheet.cell(sheet.max_row, col).number_format = fmt


def _header(sheet, columns: Iterable[str]) -> None:
    _append(sheet, columns)
    for cell in sheet[1]:
        cell.font = Font(bold=True)
    sheet.freeze_panes = "A2"


def _or_nm(value: Decimal | None) -> Decimal | str:
    return "n/m" if value is None else value


def _fraction(percent: Decimal | None) -> Decimal | str:
    """A report percentage (1.59) as the fraction Excel's percent format expects (0.0159)."""
    return "n/m" if percent is None else percent / 100


def _pnl_cells(r, first: int) -> tuple[list[object], dict[int, str]]:
    """Actual, budget, variance, variance % and label, with their formats from column `first`."""
    ratio = r.line.kind == "ratio"
    pct = None if ratio else _fraction(r.variance_pct)
    if ratio:
        values, fmt = [_fraction(r.actual), _fraction(r.budget), _or_nm(r.variance)], PERCENT
    else:
        values, fmt = [r.actual, r.budget, r.variance], AMOUNT
    formats = {first: fmt, first + 1: fmt, first + 2: POINTS if ratio else AMOUNT, first + 3: PERCENT}
    return [*values, pct, r.label], formats


def converted_columns(currency: str) -> tuple[str, ...]:
    return tuple(f"{c} ({currency})" for c in PNL_COLUMNS[2:])


def pnl_columns(report: VarianceReport, converted: VarianceReport | None) -> tuple[str, ...]:
    """PNL_COLUMNS alone; with converted columns beside them, both sets name their currency."""
    if converted is None:
        return PNL_COLUMNS
    return PNL_COLUMNS[:2] + converted_columns(report.currency) + converted_columns(converted.currency)


def _pnl(sheet, report: VarianceReport, converted: VarianceReport | None = None) -> None:
    """`converted`: the same report in another currency, as extra columns beside each row."""
    _header(sheet, pnl_columns(report, converted))
    for scope in report.scopes:
        for r in report.scope(scope):
            values, formats = _pnl_cells(r, 3)
            if converted is not None:
                more, more_formats = _pnl_cells(converted.row(scope, r.line.key), 3 + len(values))
                values, formats = values + more, formats | more_formats
            _append(sheet, [scope, r.line.label, *values], formats)
    for col, width in zip("ABCDEFGHIJKL", (8, 26, 16, 16, 16, 12, 14, 16, 16, 16, 12, 14)):
        sheet.column_dimensions[col].width = width


def _facts(sheet, release_id: str, facts: Iterable[ReleaseFact]) -> None:
    _header(sheet, FACT_COLUMNS)
    for f in facts:
        _append(
            sheet,
            [release_id, f.market, f.scenario, f.account, f.local_amount, f.currency, f.rate, f.reporting_amount,
             f.submission_id, f.file_name, f.source_row],
            {5: AMOUNT, 8: AMOUNT},
        )


def _flatten(value: object, prefix: str = "") -> Iterable[tuple[str, object]]:
    if isinstance(value, dict):
        for k, v in value.items():
            yield from _flatten(v, f"{prefix}.{k}" if prefix else str(k))
    elif isinstance(value, list):
        for i, v in enumerate(value, 1):
            yield from _flatten(v, f"{prefix}.{i}")
    else:
        yield prefix, "" if value is None else str(value)


def _manifest(sheet, release: Release, converted: VarianceReport | None = None) -> None:
    _header(sheet, ("Key", "Value"))
    for key, value in _flatten(release.manifest()):
        _append(sheet, [key, value])
    if converted is not None:
        _append(sheet, ["presentation_currency", converted.currency])
        _append(sheet, ["presentation_conversion", converted.currency_label])
        _append(sheet, ["presentation_translation", converted.translation_note])
    sheet.column_dimensions["A"].width = 40
    sheet.column_dimensions["B"].width = 70


def write_release_xlsx(path: Path, report: VarianceReport, release: Release, facts: Iterable[ReleaseFact],
                       converted: VarianceReport | None = None) -> Path:
    wb = Workbook()
    wb.properties.creator = "finance-close-toolkit"
    wb.properties.created = wb.properties.modified = report.as_of
    _pnl(wb.active, report, converted)
    wb.active.title = PNL_SHEET
    _facts(wb.create_sheet("Facts"), release.release_id, facts)
    _manifest(wb.create_sheet("Manifest"), release, converted)
    wb.save(path)
    return path


def export_xlsx(store: Store, ws: Workspace, release_id: str, out_dir: Path,
                converted: VarianceReport | None = None) -> Path:
    """`converted`: the release's report in another currency, added beside the base-currency P&L."""
    report = release_report(store, ws, release_id)
    if converted is not None and converted.conversion is None:
        converted = None
    release = store.release(release_id)
    facts = export_order(store.release_facts(release_id))
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    return write_release_xlsx(out_dir / "release.xlsx", report, release, facts, converted)
