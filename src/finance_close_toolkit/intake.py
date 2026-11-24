import csv
import difflib
import io
import zipfile
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from openpyxl import load_workbook
from openpyxl.utils.exceptions import InvalidFileException

from .config import Mapping, SourceFormat
from .money import ZERO, parse_amount

MAX_FILE_BYTES = 20 * 1024 * 1024
MAX_ROWS = 100_000


class IntakeError(ValueError):
    pass


@dataclass(frozen=True)
class RawRow:
    row_number: int
    source_account: str
    description: str
    period_value: str | None
    amount_text: str
    amount: Decimal | None  # None when the cell is not a number


@dataclass(frozen=True)
class SourceTable:
    sheets: tuple[str, ...]
    sheet: str | None
    headers: tuple[str, ...]
    rows: tuple[RawRow, ...]


@dataclass(frozen=True)
class Submission:
    submission_id: str
    market: str
    period: str
    scenario: str
    file_name: str
    sheet: str | None
    received_at: datetime


def closest_header(name: str, headers: tuple[str, ...]) -> str | None:
    matches = difflib.get_close_matches(name, [h for h in headers if h], n=1, cutoff=0.6)
    return matches[0] if matches else None


def _text(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value).strip()


def _amount(value: object) -> tuple[str, Decimal | None]:
    if value is None:
        return "", ZERO
    if isinstance(value, bool):
        return str(value), None
    if isinstance(value, (int, float)):
        return str(value), Decimal(str(value))
    if isinstance(value, str):
        try:
            return value, parse_amount(value)
        except ValueError:
            return value, None
    return str(value), None


def _at(cells: tuple[object, ...], i: int) -> object:
    return cells[i] if i < len(cells) else None


@dataclass(frozen=True)
class CellRow:
    """One row below the header, as stored in the file. `formulas` holds the formula text of an xlsx row."""

    row_number: int
    values: tuple[object, ...]
    formulas: tuple[object, ...] | None

    @property
    def blank(self) -> bool:
        return all(_text(v) == "" for v in self.values)

    def value(self, i: int) -> object:
        return _at(self.values, i)

    def text(self, i: int) -> str:
        return _text(self.value(i))

    def amount(self, i: int) -> tuple[str, Decimal | None]:
        """The cell as written and its number; None when it is not one. A formula with no cached value
        is not a number: we never evaluate it."""
        raw = self.value(i)
        formula = _at(self.formulas, i) if self.formulas is not None else None
        if raw is None and isinstance(formula, str) and formula.startswith("="):
            return formula, None
        return _amount(raw)


@dataclass(frozen=True)
class Cells:
    """A CSV file or one worksheet: its header row and every row below it."""

    sheets: tuple[str, ...]
    sheet: str | None
    headers: tuple[str, ...]
    rows: tuple[CellRow, ...]

    def index(self, columns: tuple[str, ...]) -> dict[str, int] | None:
        """Where each column is, or None when any is missing from the header row."""
        if not all(c in self.headers for c in columns):
            return None
        return {c: self.headers.index(c) for c in columns}


def _bounded(rows, name: str) -> list[tuple[object, ...]]:
    out = []
    for r in rows:
        if len(out) >= MAX_ROWS:
            raise IntakeError(f"{name} has more than {MAX_ROWS:,} rows")
        out.append(tuple(r))
    return out


def _sheet_rows(data: bytes, name: str, sheet: str | None,
                data_only: bool) -> tuple[tuple[str, ...], str, list[tuple[object, ...]]]:
    wb = load_workbook(io.BytesIO(data), read_only=True, data_only=data_only)
    try:
        sheets = tuple(wb.sheetnames)
        chosen = sheet or sheets[0]
        if chosen not in sheets:
            raise IntakeError(f"sheet {chosen!r} not found in {name}; sheets: {', '.join(sheets)}")
        return sheets, chosen, _bounded(wb[chosen].iter_rows(values_only=True), name)
    finally:
        wb.close()


def read_cells(data: bytes, name: str, format: SourceFormat, sheet: str | None, header_row: int,
               reader: str) -> Cells:
    """Read a CSV or xlsx file the way every input is read. `reader` names what expects the format,
    for errors: "the SG mapping"."""
    if len(data) > MAX_FILE_BYTES:
        raise IntakeError(f"{name} is over {MAX_FILE_BYTES // (1024 * 1024)} MB")
    sheets: tuple[str, ...] = ()
    chosen: str | None = None
    formula_rows: list[tuple[object, ...]] | None = None
    if format == "csv":
        try:
            text = data.decode("utf-8-sig")
        except UnicodeDecodeError:
            raise IntakeError(f"{name} is not a UTF-8 CSV file; {reader} expects csv") from None
        value_rows = _bounded(csv.reader(io.StringIO(text, newline="")), name)
    else:
        # read_only + data_only never evaluates formulas or runs macros. The formula-text pass
        # only tells us which cells are formulas with no cached value.
        try:
            sheets, chosen, value_rows = _sheet_rows(data, name, sheet, data_only=True)
        except (InvalidFileException, zipfile.BadZipFile):
            raise IntakeError(f"{name} is not an Excel workbook; {reader} expects xlsx") from None
        formula_rows = _sheet_rows(data, name, chosen, data_only=False)[2]
    headers = tuple(_text(c) for c in value_rows[header_row - 1]) if len(value_rows) >= header_row else ()
    rows = tuple(
        CellRow(n, cells, formula_rows[n - 1] if formula_rows is not None else None)
        for n, cells in enumerate(value_rows, start=1)
        if n > header_row
    )
    return Cells(sheets, chosen, headers, rows)


def read_source(path: Path, mapping: Mapping) -> SourceTable:
    path, layout = Path(path), mapping.layout
    if path.stat().st_size > MAX_FILE_BYTES:
        raise IntakeError(f"{path.name} is over {MAX_FILE_BYTES // (1024 * 1024)} MB")
    cells = read_cells(path.read_bytes(), path.name, layout.format, layout.sheet, layout.header_row,
                       f"the {mapping.market} mapping")
    index = cells.index(layout.columns)
    rows: list[RawRow] = []
    for row in cells.rows if index is not None else ():
        account = row.text(index[layout.account_column])
        amount_text, amount = row.amount(index[layout.amount_column])
        if not account and not amount_text.strip():
            continue
        rows.append(
            RawRow(
                row_number=row.row_number,
                source_account=account,
                description=row.text(index[layout.description_column]),
                period_value=row.text(index[layout.period_column]) if layout.period_column else None,
                amount_text=amount_text,
                amount=amount,
            )
        )
    return SourceTable(cells.sheets, cells.sheet, cells.headers, tuple(rows))


@dataclass(frozen=True)
class ColumnCheck:
    name: str
    found: bool
    closest: str | None


@dataclass(frozen=True)
class InspectReport:
    file_name: str
    table: SourceTable
    header_row: int
    columns: tuple[ColumnCheck, ...]
    mapping: Mapping

    def regional_account(self, row: RawRow) -> str | None:
        return self.mapping.accounts.get(row.source_account)


def inspect(path: Path, mapping: Mapping) -> InspectReport:
    table = read_source(path, mapping)
    columns = tuple(
        ColumnCheck(col, col in table.headers, None if col in table.headers else closest_header(col, table.headers))
        for col in mapping.layout.columns
    )
    return InspectReport(Path(path).name, table, mapping.layout.header_row, columns, mapping)
