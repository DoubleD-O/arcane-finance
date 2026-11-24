"""Ready-to-fill input templates, generated from one table of columns per input.

The same table drives the data sheet, the Instructions sheet, the close-controls parsers and the canonical
controls mapping, so they cannot drift. Text cells are written as text, never as formulas.
"""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from openpyxl import Workbook
from openpyxl.styles import Font

from .config import ChartOfAccounts
from .release import safe_text

InputKind = Literal["submission", "accruals", "invoices", "gl"]
ColumnType = Literal["text", "period", "date", "amount"]
TYPE_LABELS: dict[ColumnType, str] = {
    "text": "Text",
    "period": "Period, YYYY-MM (typed as text)",
    "date": "Date, YYYY-MM-DD",
    "amount": "Amount: 1234.50, 1,234.50 or (1,234.50) for negative",
}
AMOUNT_FORMAT = "#,##0.00;(#,##0.00)"
TEXT_FORMAT = "@"  # so Excel keeps 2026-04 as typed instead of turning it into a date


@dataclass(frozen=True)
class Column:
    name: str
    type: ColumnType
    required: bool
    description: str
    example: str
    sign: str = ""


@dataclass(frozen=True)
class InputTemplate:
    kind: InputKind
    sheet: str
    what: str  # what one row is
    columns: tuple[Column, ...]

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(c.name for c in self.columns)

    def file_name(self, fmt: Literal["xlsx", "csv"] = "xlsx") -> str:
        return f"{self.kind}_template.{fmt}"


DEBIT_POSITIVE = "Debit-positive, local currency: an expense is positive, a credit to expense is negative."

TEMPLATES: dict[InputKind, InputTemplate] = {t.kind: t for t in (
    InputTemplate("submission", "Submission", "one P&L line of a country submission", (
        Column("Period", "period", True, "The month the figures are for.", "2026-10"),
        Column("Account", "text", True, "Regional account code from the chart of accounts.", "REV"),
        Column("Description", "text", False, "What the line is.", "Product revenue"),
        Column("Amount", "amount", True, "The line's amount in the market's local currency.", "800000.00",
               "As on the P&L: revenue and costs both positive. A credit to a cost (a rebate) is negative."),
    )),
    InputTemplate("accruals", "Accruals", "one month-end accrual", (
        Column("accrual_id", "text", True, "Unique accrual ID, also across earlier imports.", "A-001"),
        Column("market", "text", True, "Market code from close.toml.", "SG"),
        Column("period", "period", True, "The month the accrual is booked in. One period per file.", "2026-03"),
        Column("account", "text", True, "Regional account code from the chart of accounts.", "OTH"),
        Column("supplier", "text", True, "Supplier the invoice is expected from.", "Harbour Advisory"),
        Column("reference", "text", False, "PO or contract reference the invoice will quote; it links them.",
               "PO-1001"),
        Column("amount", "amount", True, "Accrued amount.", "100000.00", DEBIT_POSITIVE),
        Column("owner", "text", False, "Owner role from close.toml [controls] owners; blank raises NO_OWNER.",
               "SG finance analyst"),
        Column("description", "text", False, "What the accrual is for.", "March advisory work"),
    )),
    InputTemplate("invoices", "Invoices", "one supplier invoice", (
        Column("invoice_id", "text", True, "Unique invoice ID, also across earlier imports.", "INV-5501"),
        Column("market", "text", True, "Market code from close.toml.", "SG"),
        Column("supplier", "text", True, "Supplier on the invoice.", "Harbour Advisory"),
        Column("invoice_date", "date", True, "Invoice date.", "2026-04-08"),
        Column("period", "period", True, "The month the invoice is booked in. One period per file.", "2026-04"),
        Column("account", "text", True, "Regional account code from the chart of accounts.", "OTH"),
        Column("reference", "text", False, "PO or contract reference quoted on the invoice.", "PO-1001"),
        Column("amount", "amount", True, "Invoiced amount.", "110000.00", DEBIT_POSITIVE),
    )),
    InputTemplate("gl", "GL", "one general ledger journal line", (
        Column("journal_id", "text", True, "Journal ID.", "JE-0401"),
        Column("period", "period", True, "The month the line is posted in. One period per file.", "2026-04"),
        Column("market", "text", True, "Market code from close.toml.", "SG"),
        Column("account", "text", True, "Regional account code from the chart of accounts.", "OTH"),
        Column("amount", "amount", True, "Line amount.", "-100000.00",
               DEBIT_POSITIVE + " A reversal of an expense accrual is negative."),
        Column("reference", "text", False, "The accrual ID a reversal reverses.", "A-001"),
        Column("description", "text", False, "Journal line text.", "Reversal of A-001"),
    )),
)}


def _row(sheet, values) -> None:
    sheet.append([safe_text(v) if isinstance(v, str) else v for v in values])


def _bold_row(sheet, values) -> None:
    _row(sheet, values)
    for cell in sheet[sheet.max_row]:
        cell.font = Font(bold=True)


INSTRUCTION_COLUMNS = ("Column", "Type", "Required", "Description", "Sign convention", "Example")


def instructions(t: InputTemplate, coa: ChartOfAccounts) -> list[tuple[bool, tuple[str, ...]]]:
    """The Instructions sheet as (bold, cells) rows, from the column table and the configured chart of
    accounts, so it cannot drift from what the import accepts."""
    rows = [(True, (f"Fill the {t.sheet} sheet: {t.what} per row, from row 2. Keep its header row as it is.",)),
            (False, ("Amounts are plain numbers, never formulas. Blank rows are skipped.",))]
    rows += [(False, (f"Period format: YYYY-MM typed as text, like {c.example}. Format the {c.name} column as text "
                      "so Excel keeps it.",)) for c in t.columns if c.type == "period"]
    rows += [(False, (f"Sign convention: {c.sign}",)) for c in t.columns if c.sign]
    rows += [(False, ()), (True, INSTRUCTION_COLUMNS)]
    rows += [(False, (c.name, TYPE_LABELS[c.type], "yes" if c.required else "no", c.description, c.sign, c.example))
             for c in t.columns]
    rows += [(False, ()), (True, ("Regional accounts (chart of accounts)",)), (True, ("Code", "Name"))]
    rows += [(False, (a.code, a.name)) for a in coa.accounts.values()]
    rows += [(False, ()), (True, ("Example row",)), (True, t.names), (False, tuple(c.example for c in t.columns))]
    return rows


def workbook_bytes(t: InputTemplate, coa: ChartOfAccounts, rows: tuple[tuple, ...] = (),
                   source: tuple[tuple[str, str], ...] = ()) -> bytes:
    """The template workbook; rows, in the template's column order, fill its data sheet. `source`: (key, value)
    pairs naming where a filled file came from, at the top of the Instructions sheet, so they survive a rename."""
    wb = Workbook()
    wb.properties.creator = "finance-close-toolkit"
    data = wb.active
    data.title = t.sheet
    _bold_row(data, t.names)
    data.freeze_panes = "A2"
    for i, c in enumerate(t.columns, start=1):
        column = data.column_dimensions[data.cell(1, i).column_letter]
        column.width = max(14, len(c.name) + 4, len(c.example) + 4)
        column.number_format = AMOUNT_FORMAT if c.type == "amount" else TEXT_FORMAT
    for r in rows:
        _row(data, r)
        for i, c in enumerate(t.columns, start=1):
            data.cell(data.max_row, i).number_format = AMOUNT_FORMAT if c.type == "amount" else TEXT_FORMAT

    notes = wb.create_sheet("Instructions")
    if source:
        _bold_row(notes, ("Source",))
        for values in source:
            _row(notes, values)
        _row(notes, ())
    for bold, values in instructions(t, coa):
        (_bold_row if bold else _row)(notes, values)
    for letter, width in zip("ABCDEF", (16, 34, 10, 60, 60, 20)):
        notes.column_dimensions[letter].width = width
    out = io.BytesIO()
    wb.save(out)
    return out.getvalue()


def _csv_bytes(rows) -> bytes:
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\n")
    for r in rows:
        writer.writerow([safe_text(v) for v in r])
    return out.getvalue().encode("utf-8")


def csv_bytes(t: InputTemplate) -> bytes:
    return _csv_bytes([t.names])


def instructions_csv_bytes(t: InputTemplate, coa: ChartOfAccounts) -> bytes:
    return _csv_bytes([values for _, values in instructions(t, coa)])


def template_file(kind: InputKind, coa: ChartOfAccounts) -> tuple[str, bytes]:
    """The xlsx template's file name and bytes, for a download."""
    t = TEMPLATES[kind]
    return t.file_name(), workbook_bytes(t, coa)


def write_templates(out_dir: Path, coa: ChartOfAccounts, as_csv: bool = False) -> tuple[Path, ...]:
    """Every input template into out_dir. CSV: the header row, with the instructions in a second file."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    written = []
    for t in TEMPLATES.values():
        if as_csv:
            files = {t.file_name("csv"): csv_bytes(t), f"{t.kind}_instructions.csv": instructions_csv_bytes(t, coa)}
        else:
            files = {t.file_name(): workbook_bytes(t, coa)}
        for name, data in files.items():
            path = out_dir / name
            path.write_bytes(data)
            written.append(path)
    return tuple(written)
