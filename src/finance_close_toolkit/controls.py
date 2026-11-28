"""Close controls: accrual (Finance) -> invoice (AP) -> reversal in the ledger (Accounting).

Inputs are three templates (templates.py), read as CSV or xlsx through an optional mapping and parsed at the
boundary into frozen rows that keep their file and row.
Every state is derived on read from the current imports plus the append-only event log (docs/design-step4.md).
Amounts are debit-positive local currency.
"""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass, field, replace
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Literal, Mapping

from . import identity
from .config import (ConfigError, ControlsConfig, ControlsMapping, SourceFormat, Workspace,
                     canonical_controls_mapping, load_controls_mapping, version_of)
from .identity import Actor, Person
from .intake import CellRow, IntakeError, closest_header, read_cells
from .money import ZERO, fmt_money, fmt_step
from .templates import TEMPLATES as INPUT_TEMPLATES, ColumnType
from .validation import PERIOD_RE, date_period_fix

if TYPE_CHECKING:
    from .store import Store

Kind = Literal["accruals", "invoices", "gl"]
KINDS: tuple[Kind, ...] = ("accruals", "invoices", "gl")
InvoiceState = Literal["awaiting_invoice", "partially_invoiced", "invoiced", "invoiced_with_variance"]
ReversalState = Literal["scheduled", "ledger_confirmed", "missing", "mismatch"]
Overall = Literal["open", "cleared", "resolved"]
OVERALL: tuple[Overall, ...] = ("open", "cleared", "resolved")
ResolveKind = Literal["re_accrue", "write_back", "not_expected"]
RESOLVE_KINDS: tuple[ResolveKind, ...] = ("re_accrue", "write_back", "not_expected")
# What keeps an accrual from being cleared, derived from the evidence.
Issue = Literal["awaiting_invoice", "partially_invoiced", "unexplained_variance", "reversal_scheduled",
                "reversal_missing", "reversal_mismatch"]
SignalType = Literal["INVOICE_VARIANCE", "PARTIAL_INVOICE", "MISSING_REVERSAL", "REVERSAL_MISMATCH",
                     "AMBIGUOUS_MATCH", "REFERENCE_MISMATCH", "NO_OWNER"]
# The issue a person names when resolving: one per accrual signal.
SignalIssue = Literal["invoice_variance", "partial_invoice", "missing_reversal", "reversal_mismatch", "no_owner"]
LinkMethod = Literal["reference", "only candidate", "AP link", "override"]


class ControlsError(ValueError):
    pass


def next_period(period: str) -> str:
    year, month = map(int, period.split("-"))
    year, month = (year + 1, 1) if month == 12 else (year, month + 1)
    return f"{year}-{month:02d}"


# Inputs


@dataclass(frozen=True)
class Source:
    file_name: str
    row: int

    def __str__(self) -> str:
        return f"{self.file_name} row {self.row}"


@dataclass(frozen=True)
class Accrual:
    accrual_id: str
    market: str
    period: str
    account: str
    supplier: str
    reference: str
    amount: Decimal
    owner: str
    description: str
    source: Source


@dataclass(frozen=True)
class Invoice:
    invoice_id: str
    market: str
    supplier: str
    invoice_date: date
    period: str
    account: str
    reference: str
    amount: Decimal
    source: Source


@dataclass(frozen=True)
class GlLine:
    journal_id: str
    period: str
    market: str
    account: str
    amount: Decimal
    reference: str
    description: str
    source: Source


Row = Accrual | Invoice | GlLine


def _text(row: CellRow, i: int, where: str, header: str) -> str:
    return row.text(i)


def _required(row: CellRow, i: int, where: str, header: str) -> str:
    value = row.text(i)
    if not value:
        raise ControlsError(f"{where}: is blank")
    return value


def _period(row: CellRow, i: int, where: str, header: str) -> str:
    value = row.text(i)
    if fix := date_period_fix(value, header):
        raise ControlsError(f"{where}: {fix}")
    if not PERIOD_RE.match(value):
        raise ControlsError(f"{where}: period must look like 2026-04, got {value!r}")
    return value


def _money(row: CellRow, i: int, where: str, header: str) -> Decimal:
    text, value = row.amount(i)
    if not text.strip():
        raise ControlsError(f"{where}: is blank")
    if value is None:
        raise ControlsError(f"{where}: not an amount: {text!r}")
    return value


def _date(row: CellRow, i: int, where: str, header: str) -> date:
    value = row.value(i)
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(row.text(i))
    except ValueError:
        raise ControlsError(f"{where}: date must look like 2026-04-08, got {row.text(i)!r}") from None


Parse = Callable[[CellRow, int, str, str], object]  # (row, column index, where, header)
PARSERS: dict[tuple[ColumnType, bool], Parse] = {
    ("text", True): _required, ("text", False): _text, ("period", True): _period, ("date", True): _date,
    ("amount", True): _money,
}


@dataclass(frozen=True)
class Template:
    """An input template: its columns in order, how each cell parses, and the row type it builds."""

    kind: Kind
    columns: Mapping[str, Parse]
    build: Callable[..., Row]
    id_column: str | None  # unique within a file and across current imports


def _template(kind: Kind, build: Callable[..., Row], id_column: str | None) -> Template:
    columns = {c.name: PARSERS[(c.type, c.required)] for c in INPUT_TEMPLATES[kind].columns}
    return Template(kind, columns, build, id_column)


TEMPLATES: dict[Kind, Template] = {
    "accruals": _template("accruals", Accrual, "accrual_id"),
    "invoices": _template("invoices", Invoice, "invoice_id"),
    "gl": _template("gl", GlLine, None),
}


def mapping_path(ws: Workspace, kind: Kind) -> Path:
    return ws.config.path.parent / "controls" / "mappings" / f"{kind}.toml"


def load_mapping(path: Path, kind: Kind) -> ControlsMapping:
    try:
        return load_controls_mapping(Path(path), tuple(TEMPLATES[kind].columns))
    except (ConfigError, OSError) as e:
        raise ControlsError(f"{kind} mapping: {e}") from None


# Which reader a file's extension implies, for every input (submissions and controls files).
SOURCE_FORMATS: dict[str, SourceFormat] = {".csv": "csv", ".xlsx": "xlsx", ".xlsm": "xlsx"}
UPLOAD_TYPES = tuple(ext[1:] for ext in SOURCE_FORMATS)


def source_format(file_name: str) -> SourceFormat | None:
    return SOURCE_FORMATS.get(Path(file_name).suffix.lower())


def default_mapping(ws: Workspace, kind: Kind, file_name: str) -> ControlsMapping:
    """config/controls/mappings/<kind>.toml when it exists; else the template's own columns, read as xlsx
    or csv by the file's extension."""
    path = mapping_path(ws, kind)
    if path.exists():
        return load_mapping(path, kind)
    return canonical_controls_mapping(tuple(TEMPLATES[kind].columns), source_format(file_name) or "csv")


@dataclass(frozen=True)
class MappedColumn:
    name: str  # the template column
    source: str  # the header it is read from
    found: bool
    closest: str | None


@dataclass(frozen=True)
class Reading:
    """One controls file as it will be read: what `inspect` shows and what an import stores."""

    kind: Kind
    file_name: str
    mapping: ControlsMapping
    sheets: tuple[str, ...]
    sheet: str | None
    headers: tuple[str, ...]
    columns: tuple[MappedColumn, ...]
    rows: tuple[Row, ...] = ()
    problems: tuple[str, ...] = ()  # none: the file imports
    missing: str | None = None  # the refusal when template columns are missing; then no row is read
    warnings: tuple[str, ...] = ()  # the file imports, but may be read the wrong way


HEADER_SEARCH_ROWS = 20


def _header_row_hint(data: bytes, file_name: str, mapping: ControlsMapping, kind: Kind) -> int | None:
    """When no mapped column is on the mapping's header row: the row among the first HEADER_SEARCH_ROWS that
    holds most of the mapped headers, at least half of them."""
    try:
        cells = read_cells(data, file_name, mapping.format, mapping.sheet, 1, f"the {kind} mapping")
    except IntakeError:
        return None
    sources = set(mapping.columns.values())
    top = [(1, set(cells.headers))] + [(r.row_number, {r.text(i) for i in range(len(r.values))})
                                       for r in cells.rows[:HEADER_SEARCH_ROWS - 1]]
    n, found = max(top, key=lambda t: len(sources & t[1]))
    return n if n != mapping.header_row and len(sources & found) * 2 >= len(sources) else None


def _missing_text(kind: Kind, file_name: str, columns: tuple[MappedColumn, ...], headers: tuple[str, ...],
                  header_row: int | None) -> str:
    missing = [c for c in columns if not c.found]
    labels = [(c.name if c.source == c.name else f'{c.name} (from "{c.source}")')
              + (f' (closest header "{c.closest}")' if c.closest else "") for c in missing]
    hint = f"; headers found on row {header_row}; set header_row = {header_row}" if header_row else ""
    return (f"{file_name} is missing {kind} column{'s' * (len(missing) != 1)} {', '.join(labels)} "
            f"(found: {', '.join(h for h in headers if h) or 'nothing'}){hint}")


def _warnings(kind: Kind, rows: list[Row]) -> tuple[str, ...]:
    if kind == "gl" and rows and all(r.amount > 0 for r in rows):
        return ("all GL amounts are positive; check amount_sign",)
    return ()


def _label(mapping: ControlsMapping, column: str) -> str:
    source = mapping.columns[column]
    return column if source == column else f'{column} ("{source}")'


def read(kind: Kind, data: bytes, file_name: str, ws: Workspace, mapping: ControlsMapping | None = None) -> Reading:
    """Read one controls file through its mapping, collecting every problem with its row. Stores nothing."""
    template = TEMPLATES[kind]
    mapping = mapping or default_mapping(ws, kind, file_name)
    try:
        cells = read_cells(data, file_name, mapping.format, mapping.sheet, mapping.header_row, f"the {kind} mapping")
    except IntakeError as e:
        raise ControlsError(str(e)) from None
    columns = tuple(MappedColumn(c, src, src in cells.headers,
                                 None if src in cells.headers else closest_header(src, cells.headers))
                    for c, src in mapping.columns.items())
    reading = Reading(kind, file_name, mapping, cells.sheets, cells.sheet, cells.headers, columns)
    if not any(cells.headers) and not cells.rows:
        return replace(reading, missing=f"{file_name} is empty; the {kind} template needs columns "
                                        f"{', '.join(template.columns)}")
    if not all(c.found for c in columns):
        hint = _header_row_hint(data, file_name, mapping, kind) if not any(c.found for c in columns) else None
        return replace(reading, missing=_missing_text(kind, file_name, columns, cells.headers, hint))
    index = {c.name: cells.headers.index(c.source) for c in columns}
    problems, rows, seen = [], [], {}
    for cell_row in cells.rows:
        if cell_row.blank:
            continue
        row_number, values, row_ok = cell_row.row_number, {}, True
        for column, parse in template.columns.items():
            try:
                values[column] = parse(cell_row, index[column], f"{file_name} row {row_number} {_label(mapping, column)}",
                                       mapping.columns[column])
            except ControlsError as e:
                problems.append(str(e))
                row_ok = False
        if not row_ok:
            continue
        if mapping.amount_sign == "negate":
            values["amount"] = -values["amount"]
        where = f"{file_name} row {row_number}"
        try:
            ws.config.market(values["market"])
        except ValueError as e:
            problems.append(f"{where}: {e}")
        if values["account"] not in ws.coa.accounts:
            problems.append(f"{where}: account {values['account']!r} is not in the chart of accounts")
        if template.id_column:
            key = values[template.id_column]
            if key in seen:
                problems.append(f"{where}: {template.id_column} {key} repeats row {seen[key]}")
            seen.setdefault(key, row_number)
        rows.append(template.build(**values, source=Source(file_name, row_number)))
    periods = sorted({r.period for r in rows})
    if not problems and not rows:
        problems.append(f"{file_name} has no {kind} rows")
    if len(periods) > 1:
        problems.append(f"{file_name} has rows for {', '.join(periods)}; one {kind} file covers one period")
    return replace(reading, rows=tuple(rows), problems=tuple(problems), warnings=_warnings(kind, rows))


def mapping_label(m: ControlsMapping) -> str:
    return f"mapping {m.name} {m.version}" if m.version else "template columns"


@dataclass(frozen=True)
class ControlFile:
    kind: Kind
    period: str
    file_name: str
    content_hash: str
    rows: tuple[Row, ...]
    mapping: str = "template columns"  # mapping_label of what read it


def content_hash(kind: Kind, data: bytes, mapping: ControlsMapping) -> str:
    """The file's bytes, plus what the mapping that read them says when there is one (its parsed meaning,
    so a comment-only edit is the same import): the same bytes read another way are another import."""
    mapped = b"\0" + mapping.meaning.encode("utf-8") if mapping.version else b""
    return version_of(kind.encode() + b"\0" + data + mapped)


def parse_file(kind: Kind, data: bytes, file_name: str, ws: Workspace,
               mapping: ControlsMapping | None = None) -> ControlFile:
    """Parse one controls file. Raises ControlsError listing every problem with its row."""
    r = read(kind, data, file_name, ws, mapping)
    if r.missing:
        raise ControlsError(r.missing)
    if r.problems:
        raise ControlsError(f"{file_name} refused:\n" + "\n".join(f"  - {p}" for p in r.problems))
    return ControlFile(kind, r.rows[0].period, file_name, content_hash(kind, data, r.mapping), r.rows,
                       mapping_label(r.mapping))


@dataclass(frozen=True)
class ControlImport:
    import_id: str  # the content hash
    kind: Kind
    period: str
    file_name: str
    imported_at: datetime
    seq: int  # of this receipt: a stored import received again is current again
    rows: tuple[Row, ...]
    mapping: str = "template columns"  # mapping_label of what read it
    logged_at: datetime | None = None  # the real clock in signed-in mode; imported_at is the effective time

    @property
    def label(self) -> str:
        return f"{self.kind} {self.period} {self.file_name} ({self.mapping})"


# Events (append-only)

EventKind = Literal["link", "explain", "assign_owner", "resolve", "drop"]


@dataclass(frozen=True)
class Event:
    kind: EventKind
    accrual_id: str  # for a dropped invoice with no link: the invoice ID
    by: str
    at: datetime
    evidence: str
    data: Mapping[str, str] = field(default_factory=dict)  # kind-specific fields, amounts as plain text
    seq: int = 0  # assigned by the store
    person: Person | None = None  # who acted in the role `by`; None for events recorded in open mode
    logged_at: datetime | None = None  # the real clock in signed-in mode; `at` is the effective time

    @property
    def by_label(self) -> str:
        return Actor(self.by, self.person).label

    @property
    def invoice_id(self) -> str:
        return self.data["invoice_id"]

    def payload(self) -> str:
        return json.dumps(dict(self.data), sort_keys=True)


# Rules as tables (docs/design-step4.md)


def _same(name: str) -> Callable[[Invoice, Accrual], str | None]:
    def check(inv: Invoice, a: Accrual) -> str | None:
        mine, theirs = getattr(inv, name), getattr(a, name)
        return None if mine == theirs else f"{name} {mine} vs accrual {theirs}"

    return check


# What an invoice must share with an accrual before it links, by reference or as a candidate. Each check
# returns None when it holds, else what differs.
LINK_CHECKS: tuple[Callable[[Invoice, Accrual], str | None], ...] = (
    _same("market"), _same("supplier"), _same("account"),
    lambda inv, a: None if inv.period > a.period else
    f"period {inv.period} is not after the accrual period {a.period}",
)


def link_mismatches(inv: Invoice, a: Accrual) -> tuple[str, ...]:
    return tuple(m for check in LINK_CHECKS if (m := check(inv, a)) is not None)


@dataclass(frozen=True)
class IssueRule:
    signal: SignalType
    kinds: tuple[ResolveKind, ...]  # resolutions allowed; empty: another action closes it
    instead: str  # that other action
    covers: Issue | None  # the derived issue an approved resolution covers


ISSUE_RULES: dict[SignalIssue, IssueRule] = {
    "missing_reversal": IssueRule("MISSING_REVERSAL", ("write_back", "not_expected"), "", "reversal_missing"),
    "partial_invoice": IssueRule("PARTIAL_INVOICE", ("re_accrue", "not_expected"), "", "partially_invoiced"),
    "invoice_variance": IssueRule("INVOICE_VARIANCE", (), "use explain", None),
    "no_owner": IssueRule("NO_OWNER", (), "use assign", None),
    "reversal_mismatch": IssueRule("REVERSAL_MISMATCH", (),
                                   "correct the reversal in the ledger and import the corrected GL extract", None),
}
SIGNAL_ISSUE: dict[SignalType, SignalIssue] = {rule.signal: issue for issue, rule in ISSUE_RULES.items()}

# The entry a resolution implies but the ledger does not show yet: +1 debits expense (re-accrue the remainder),
# -1 credits it (write the accrual back). Not listed: no entry.
PENDING_SIGN: dict[ResolveKind, int] = {"re_accrue": 1, "write_back": -1}

# What an explanation or a resolution is bound to when it is recorded. If any of these differ later, it no
# longer covers and its signal returns.
BOUND_FACTS: dict[EventKind, tuple[str, ...]] = {
    "explain": ("accrued", "invoiced"),
    "resolve": ("accrued", "invoiced", "reversal"),
}


# Derived state


@dataclass(frozen=True)
class Link:
    invoice: Invoice
    accrual_id: str
    method: LinkMethod
    event: Event | None


@dataclass(frozen=True)
class Lapse:
    """An explanation or resolution that no longer covers, because the facts it was bound to changed."""

    event: Event
    changes: tuple[str, ...]  # e.g. "accrued changed 100,000.00 → 10,000.00"

    @property
    def what(self) -> str:
        return {"explain": "Explanation", "resolve": "Resolution"}[self.event.kind] + " no longer applies"


@dataclass(frozen=True)
class AccrualState:
    accrual: Accrual
    owner: str | None  # None: blank or not an active role
    tolerance: Decimal
    links: tuple[Link, ...]
    invoice_state: InvoiceState
    reversal_period: str
    reversal_state: ReversalState
    reversals: tuple[GlLine, ...]  # every reversal-period line citing the accrual on its account and market
    gl_file: str | None  # the current GL extract for the reversal period, once imported
    explained: Event | None  # the explanation that still covers
    resolutions: tuple[Event, ...]  # resolutions that still cover, one per issue at most
    lapsed: tuple[Lapse, ...]
    issues: frozenset[Issue]  # what keeps it from cleared, before resolutions
    open_issues: frozenset[Issue]  # issues no resolution covers

    @property
    def accrual_id(self) -> str:
        return self.accrual.accrual_id

    @property
    def invoiced(self) -> Decimal:
        return sum((link.invoice.amount for link in self.links), ZERO)

    @property
    def difference(self) -> Decimal:
        return self.invoiced - self.accrual.amount

    @property
    def reversed_total(self) -> Decimal:
        """What the ledger reversed, as a positive amount for an expense accrual."""
        return -sum((line.amount for line in self.reversals), ZERO)

    @property
    def reversal(self) -> GlLine | None:
        """The reversal line when there is exactly one."""
        return self.reversals[0] if len(self.reversals) == 1 else None

    @property
    def facts(self) -> dict[str, str]:
        return _facts(self.accrual.amount, self.invoiced, self.gl_file, self.reversals)

    @property
    def overall(self) -> Overall:
        if not self.issues:
            return "cleared"
        return "open" if self.open_issues else "resolved"


def _facts(accrued: Decimal, invoiced: Decimal, gl_file: str | None, reversals: tuple[GlLine, ...]) -> dict[str, str]:
    """What explanations and resolutions are bound to (BOUND_FACTS), as stored in their events."""
    reversal = ("no GL extract" if gl_file is None else
                ", ".join(f"{l.journal_id} {fmt_money(l.amount)}" for l in reversals) or "no reversal line")
    return {"accrued": str(accrued), "invoiced": str(invoiced), "reversal": reversal}


@dataclass(frozen=True)
class Signal:
    type: SignalType
    item: str  # accrual or invoice ID
    market: str
    owner: str
    amount_at_risk: Decimal
    trigger: str
    evidence: tuple[str, ...]
    next_action: str


@dataclass(frozen=True)
class Match:
    """An invoice not linked by reference or event, and the open accruals it could belong to. When its
    reference names an accrual that fails LINK_CHECKS, it is not matched at all: `referenced` names that
    accrual and `mismatches` what differs."""

    invoice: Invoice
    candidates: tuple[str, ...]
    referenced: str | None = None
    mismatches: tuple[str, ...] = ()


def _issues(invoice_state: InvoiceState, explained: bool, reversal: ReversalState) -> frozenset[Issue]:
    issues: set[Issue] = set()
    if invoice_state in ("awaiting_invoice", "partially_invoiced"):
        issues.add(invoice_state)
    if invoice_state == "invoiced_with_variance" and not explained:
        issues.add("unexplained_variance")
    if reversal != "ledger_confirmed":
        issues.add(f"reversal_{reversal}")
    return frozenset(issues)


def _invoice_state(accrued: Decimal, invoiced: Decimal, n: int, tolerance: Decimal) -> InvoiceState:
    if n == 0:
        return "awaiting_invoice"
    difference = invoiced - accrued
    if abs(difference) <= tolerance:
        return "invoiced"
    return "invoiced_with_variance" if difference > 0 else "partially_invoiced"


def _reverses(line: GlLine, a: Accrual) -> bool:
    return line.reference == a.accrual_id and line.market == a.market and line.account == a.account


def _reversal_state(gl: ControlImport | None, lines: tuple[GlLine, ...], a: Accrual) -> ReversalState:
    if gl is None:
        return "scheduled"
    if not lines:
        return "missing"
    return "ledger_confirmed" if sum((l.amount for l in lines), ZERO) == -a.amount else "mismatch"


def current_imports(imports: tuple[ControlImport, ...]) -> dict[tuple[Kind, str], ControlImport]:
    """The newest import of each kind and period, in template then period order; older ones stay stored
    for history."""
    out: dict[tuple[Kind, str], ControlImport] = {}
    for imp in sorted(imports, key=lambda i: i.seq):
        out[(imp.kind, imp.period)] = imp
    return dict(sorted(out.items(), key=lambda kv: (KINDS.index(kv[0][0]), kv[0][1])))


@dataclass(frozen=True)
class Choice:
    """An accrual offered for an override link, with what differs from the invoice."""

    accrual_id: str
    candidate: bool
    mismatches: tuple[str, ...]


@dataclass(frozen=True)
class Controls:
    cfg: ControlsConfig
    as_of: datetime
    imports: tuple[ControlImport, ...]  # every import up to as_of, oldest first
    events: tuple[Event, ...]
    currencies: Mapping[str, str]  # market -> currency
    accruals: tuple[AccrualState, ...]
    unmatched: tuple[Match, ...]  # invoices without a link: reference mismatch, ambiguous (2+ candidates) or none
    signals: tuple[Signal, ...]

    def accrual(self, accrual_id: str) -> AccrualState:
        for a in self.accruals:
            if a.accrual_id == accrual_id:
                return a
        known = ", ".join(a.accrual_id for a in self.accruals) or "none imported"
        raise ControlsError(f"unknown accrual {accrual_id!r}; accruals: {known}")

    def invoice(self, invoice_id: str) -> Invoice:
        for inv in self.invoices:
            if inv.invoice_id == invoice_id:
                return inv
        raise ControlsError(f"unknown invoice {invoice_id!r}")

    @property
    def invoices(self) -> tuple[Invoice, ...]:
        return tuple(sorted((*(l.invoice for a in self.accruals for l in a.links),
                             *(m.invoice for m in self.unmatched)), key=lambda i: i.source.row))

    def link_of(self, invoice_id: str) -> Link | None:
        return next((l for a in self.accruals for l in a.links if l.invoice.invoice_id == invoice_id), None)

    def candidates(self, invoice_id: str) -> tuple[str, ...]:
        return next((m.candidates for m in self.unmatched if m.invoice.invoice_id == invoice_id), ())

    def currency(self, market: str) -> str:
        return self.currencies.get(market, "")

    def queues(self) -> dict[str, tuple[Signal, ...]]:
        """Signals by owner role: Finance owners in roster order, then AP, Accounting and the close lead,
        which are always present."""
        c = self.cfg
        fixed = (c.ap_role, c.accounting_role, c.close_lead)
        finance = [r for r in c.owners if r not in fixed and any(s.owner == r for s in self.signals)]
        return {role: tuple(s for s in self.signals if s.owner == role) for role in (*finance, *fixed)}

    def counts(self) -> dict[Overall, int]:
        return {o: sum(1 for a in self.accruals if a.overall == o) for o in OVERALL}

    def open_issues(self, accrual_id: str) -> tuple[SignalIssue, ...]:
        """The issues a person can name for an accrual: one per signal it raises now."""
        return tuple(sorted(SIGNAL_ISSUE[s.type] for s in self.signals if s.item == accrual_id))

    def signal(self, accrual_id: str, issue: SignalIssue) -> Signal | None:
        kind = ISSUE_RULES[issue].signal
        return next((s for s in self.signals if (s.item, s.type) == (accrual_id, kind)), None)


def override_choices(c: Controls, invoice_id: str) -> tuple[Choice, ...]:
    """Every accrual an override could link the invoice to: candidates first, then accruals that pass
    LINK_CHECKS, then the rest, each with what differs."""
    inv = c.invoice(invoice_id)
    candidates = c.candidates(invoice_id)
    choices = [Choice(s.accrual_id, s.accrual_id in candidates, link_mismatches(inv, s.accrual)) for s in c.accruals]
    return tuple(sorted(choices, key=lambda ch: (not ch.candidate, bool(ch.mismatches))))


def _links(accruals: tuple[Accrual, ...], invoices: tuple[Invoice, ...], events: tuple[Event, ...],
           resolved: set[str], cfg: ControlsConfig) -> tuple[dict[str, list[Link]], list[Match]]:
    by_id = {a.accrual_id: a for a in accruals}
    linked: dict[str, list[Link]] = {a.accrual_id: [] for a in accruals}
    pending, unmatched = [], []
    chosen = {e.invoice_id: e for e in events if e.kind == "link" and e.accrual_id in by_id}  # latest wins
    for inv in invoices:
        if inv.invoice_id in chosen:
            e = chosen[inv.invoice_id]
            linked[e.accrual_id].append(Link(inv, e.accrual_id, "override" if e.data.get("override") else "AP link", e))
            continue
        by_reference = [a for a in accruals if inv.reference and inv.reference in (a.reference, a.accrual_id)]
        if len(by_reference) != 1:
            pending.append(inv)
        elif mismatches := link_mismatches(inv, by_reference[0]):
            unmatched.append(Match(inv, (), by_reference[0].accrual_id, mismatches))
        else:
            linked[by_reference[0].accrual_id].append(Link(inv, by_reference[0].accrual_id, "reference", None))

    for inv in sorted(pending, key=lambda i: (i.period, i.invoice_date, i.invoice_id)):
        candidates = tuple(a.accrual_id for a in accruals if _is_candidate(inv, a, linked[a.accrual_id], resolved, cfg))
        if len(candidates) == 1:
            linked[candidates[0]].append(Link(inv, candidates[0], "only candidate", None))
        else:
            unmatched.append(Match(inv, candidates))
    return linked, unmatched


def _is_candidate(inv: Invoice, a: Accrual, links: list[Link], resolved: set[str], cfg: ControlsConfig) -> bool:
    if link_mismatches(inv, a) or a.accrual_id in resolved:
        return False
    invoiced = sum((l.invoice.amount for l in links), ZERO)
    tolerance = cfg.tolerance(a.amount)
    if _invoice_state(a.amount, invoiced, len(links), tolerance) not in ("awaiting_invoice", "partially_invoiced"):
        return False
    return abs(inv.amount - (a.amount - invoiced)) <= tolerance


def _latest(events: tuple[Event, ...], kind: EventKind, accrual_id: str) -> Event | None:
    return next((e for e in reversed(events) if e.kind == kind and e.accrual_id == accrual_id), None)


def _fact_text(name: str, value: str) -> str:
    return value if name == "reversal" else fmt_money(Decimal(value))


def _changes(e: Event, facts: Mapping[str, str]) -> tuple[str, ...]:
    """How the facts the event is bound to (BOUND_FACTS) differ now; empty while it still covers."""
    out = []
    for name in BOUND_FACTS[e.kind]:
        then, now = e.data[name], facts[name]
        differs = then != now if name == "reversal" else Decimal(then) != Decimal(now)
        if differs:
            out.append(f"{name} changed {_fact_text(name, then)} → {_fact_text(name, now)}")
    return tuple(out)


def derive(imports: tuple[ControlImport, ...], events: tuple[Event, ...], cfg: ControlsConfig,
           currencies: Mapping[str, str], as_of: datetime) -> Controls:
    imports = tuple(i for i in sorted(imports, key=lambda i: i.seq) if i.imported_at <= as_of)
    events = tuple(e for e in sorted(events, key=lambda e: e.seq) if e.at <= as_of)
    current = current_imports(imports)
    rows = {kind: tuple(r for (k, _), imp in sorted(current.items(), key=lambda kv: kv[0][1]) if k == kind
                        for r in imp.rows) for kind in KINDS}
    accruals: tuple[Accrual, ...] = rows["accruals"]
    # A resolution keeps an accrual out of candidate matching; whether it still covers is decided below.
    resolved = {e.accrual_id for e in events if e.kind == "resolve"}
    linked, unmatched = _links(accruals, rows["invoices"], events, resolved, cfg)

    states = []
    for a in accruals:
        links = tuple(linked[a.accrual_id])
        tolerance = cfg.tolerance(a.amount)
        invoiced = sum((l.invoice.amount for l in links), ZERO)
        invoice_state = _invoice_state(a.amount, invoiced, len(links), tolerance)
        reversal_period = next_period(a.period)
        gl = current.get(("gl", reversal_period))
        reversals = tuple(line for line in gl.rows if _reverses(line, a)) if gl else ()
        reversal_state = _reversal_state(gl, reversals, a)
        facts = _facts(a.amount, invoiced, gl.file_name if gl else None, reversals)
        lapsed = []
        explained = _latest(events, "explain", a.accrual_id)
        if explained is not None and (changes := _changes(explained, facts)):
            lapsed.append(Lapse(explained, changes))
            explained = None
        issues = _issues(invoice_state, explained is not None, reversal_state)
        resolutions = []
        for issue in ISSUE_RULES:
            r = next((e for e in reversed(events) if e.kind == "resolve" and e.accrual_id == a.accrual_id
                      and e.data["issue"] == issue), None)
            if r is None:
                continue
            if changes := _changes(r, facts):
                lapsed.append(Lapse(r, changes))
            else:
                resolutions.append(r)
        covered = {ISSUE_RULES[r.data["issue"]].covers for r in resolutions}
        assigned = _latest(events, "assign_owner", a.accrual_id)
        owner = assigned.data["owner"] if assigned else a.owner
        states.append(AccrualState(
            a, owner if owner in cfg.owners else None, tolerance, links, invoice_state, reversal_period,
            reversal_state, reversals, gl.file_name if gl else None, explained,
            tuple(r for r in resolutions if ISSUE_RULES[r.data["issue"]].covers in issues), tuple(lapsed),
            issues, issues - covered,
        ))
    states = tuple(states)
    by_id = {s.accrual_id: s for s in states}
    signals = [sig for s in states for rule in ACCRUAL_RULES if (sig := rule(s, cfg)) is not None]
    signals += [_ambiguous(m, by_id, cfg) for m in unmatched if len(m.candidates) > 1]
    signals += [_reference_mismatch(m, by_id, cfg) for m in unmatched if m.referenced]
    return Controls(cfg, as_of, imports, events, currencies, states, tuple(unmatched), tuple(signals))


# Signal rules: one function per signal type, each None when its condition does not hold.


def _accrual_evidence(s: AccrualState) -> list[str]:
    return [f"accrual {s.accrual_id}: {s.accrual.source}"]


def _invoice_evidence(s: AccrualState) -> list[str]:
    return [f"invoice {l.invoice.invoice_id}: {l.invoice.source} ({l.method})" for l in s.links]


def _reversal_evidence(s: AccrualState) -> list[str]:
    return [f"reversal {line.journal_id}: {line.source}" for line in s.reversals]


def _owner_or_lead(s: AccrualState, cfg: ControlsConfig) -> str:
    return s.owner or cfg.close_lead


def _invoice_variance(s: AccrualState, cfg: ControlsConfig) -> Signal | None:
    if "unexplained_variance" not in s.open_issues:
        return None
    return Signal(
        "INVOICE_VARIANCE", s.accrual_id, s.accrual.market, _owner_or_lead(s, cfg), s.difference,
        f"invoiced {fmt_money(s.invoiced)} against {fmt_money(s.accrual.amount)} accrued: "
        f"+{fmt_money(s.difference)} is over the {fmt_money(s.tolerance)} tolerance",
        (*_accrual_evidence(s), *_invoice_evidence(s)), "Explain the difference",
    )


def _partial_invoice(s: AccrualState, cfg: ControlsConfig) -> Signal | None:
    if "partially_invoiced" not in s.open_issues or s.reversal_state != "ledger_confirmed":
        return None
    return Signal(
        "PARTIAL_INVOICE", s.accrual_id, s.accrual.market, _owner_or_lead(s, cfg), -s.difference,
        f"invoiced {fmt_money(s.invoiced)} of {fmt_money(s.accrual.amount)} accrued and the accrual is reversed",
        (*_accrual_evidence(s), *_invoice_evidence(s), *_reversal_evidence(s)),
        "Re-accrue the remainder or confirm no further invoice",
    )


def _missing_reversal(s: AccrualState, cfg: ControlsConfig) -> Signal | None:
    if "reversal_missing" not in s.open_issues:
        return None
    return Signal(
        "MISSING_REVERSAL", s.accrual_id, s.accrual.market, cfg.accounting_role, s.accrual.amount,
        f"{s.gl_file} has no {s.reversal_period} line reversing {s.accrual_id} "
        f"({s.accrual.account}, {fmt_money(-s.accrual.amount)})",
        (*_accrual_evidence(s), f"GL extract {s.reversal_period}: {s.gl_file}"),
        "Post the reversal or record a resolution",
    )


def _reversal_mismatch(s: AccrualState, cfg: ControlsConfig) -> Signal | None:
    if "reversal_mismatch" not in s.open_issues:
        return None
    n = len(s.reversals)
    return Signal(
        "REVERSAL_MISMATCH", s.accrual_id, s.accrual.market, cfg.accounting_role,
        abs(s.reversed_total - s.accrual.amount),
        f"{s.gl_file} has {n} line{'s' * (n != 1)} reversing {s.accrual_id} "
        f"({', '.join(l.journal_id for l in s.reversals)}) totalling {fmt_money(-s.reversed_total)}; "
        f"the reversal must be exactly {fmt_money(-s.accrual.amount)}",
        (*_accrual_evidence(s), *_reversal_evidence(s)),
        "Correct the reversal in the ledger and import the corrected GL extract",
    )


def _no_owner(s: AccrualState, cfg: ControlsConfig) -> Signal | None:
    if s.owner is not None or s.overall != "open":
        return None
    named = s.accrual.owner
    return Signal(
        "NO_OWNER", s.accrual_id, s.accrual.market, cfg.close_lead, s.accrual.amount,
        f"owner {named!r} is not an active role" if named else "the accrual has no owner",
        tuple(_accrual_evidence(s)), "Assign an owner",
    )


ACCRUAL_RULES: tuple[Callable[[AccrualState, ControlsConfig], Signal | None], ...] = (
    _invoice_variance, _partial_invoice, _missing_reversal, _reversal_mismatch, _no_owner,
)


def _ambiguous(m: Match, by_id: Mapping[str, AccrualState], cfg: ControlsConfig) -> Signal:
    inv = m.invoice
    return Signal(
        "AMBIGUOUS_MATCH", inv.invoice_id, inv.market, cfg.ap_role, inv.amount,
        f"{len(m.candidates)} open accruals match {inv.supplier} {inv.account} {fmt_money(inv.amount)}: "
        f"{', '.join(m.candidates)}",
        (f"invoice {inv.invoice_id}: {inv.source}",
         *(f"candidate {c}: {by_id[c].accrual.source}" for c in m.candidates)),
        "Link the invoice to one accrual",
    )


def _reference_mismatch(m: Match, by_id: Mapping[str, AccrualState], cfg: ControlsConfig) -> Signal:
    inv = m.invoice
    return Signal(
        "REFERENCE_MISMATCH", inv.invoice_id, inv.market, cfg.ap_role, inv.amount,
        f"{inv.invoice_id} references {m.referenced} ({inv.reference}) but {'; '.join(m.mismatches)}",
        (f"invoice {inv.invoice_id}: {inv.source}", f"referenced {m.referenced}: {by_id[m.referenced].accrual.source}"),
        "Confirm the invoice belongs to this accrual or correct the reference",
    )


# Journal preview

ACCRUED_LIABILITIES = "Accrued liabilities"
ACCOUNTS_PAYABLE = "Accounts payable"


@dataclass(frozen=True)
class JournalEntry:
    period: str
    label: str
    debit: str
    credit: str
    amount: Decimal
    evidence: str  # where the entry is evidenced, or why it is not in the ledger


@dataclass(frozen=True)
class NetEffect:
    period: str
    pl_expense: Decimal
    accrued_liabilities: Decimal  # credit balance left for this accrual
    accounts_payable: Decimal  # credit balance from its invoices
    cash: Decimal


@dataclass(frozen=True)
class JournalPreview:
    entries: tuple[JournalEntry, ...]
    net: NetEffect


def journal_preview(s: AccrualState) -> JournalPreview:
    """The entries the ledger and the imports evidence, and their net effect. Pending entries are separate."""
    a, p = s.accrual, s.reversal_period
    reversals = [JournalEntry(p, "Reversal", ACCRUED_LIABILITIES, a.account, -line.amount,
                              f"{line.journal_id}, {line.source}") for line in s.reversals]
    entries = [
        JournalEntry(a.period, "Accrual", a.account, ACCRUED_LIABILITIES, a.amount, str(a.source)),
        *(reversals or [JournalEntry(p, "Reversal", ACCRUED_LIABILITIES, a.account, a.amount,
                                     f"not in the ledger: reversal {s.reversal_state}")]),
        *(JournalEntry(l.invoice.period, f"Invoice {l.invoice.invoice_id}", a.account, ACCOUNTS_PAYABLE,
                       l.invoice.amount, str(l.invoice.source)) for l in s.links),
    ]
    in_p = sum((l.invoice.amount for l in s.links if l.invoice.period == p), ZERO)
    to_p = sum((l.invoice.amount for l in s.links if l.invoice.period <= p), ZERO)
    reversed_amount = s.reversed_total
    return JournalPreview(tuple(entries), NetEffect(p, in_p - reversed_amount, a.amount - reversed_amount, to_p, ZERO))


@dataclass(frozen=True)
class PendingEntry:
    """An entry a covering resolution implies that the ledger does not show yet."""

    accrual_id: str
    period: str
    kind: ResolveKind
    debit: str
    credit: str
    amount: Decimal
    pl_effect: Decimal  # on P&L expense: + for a re-accrual, - for a write-back
    resolution: Event


def pending_entries(s: AccrualState) -> tuple[PendingEntry, ...]:
    out = []
    for r in s.resolutions:
        kind = r.data["resolve_kind"]
        if kind not in PENDING_SIGN:
            continue
        sign, amount, account = PENDING_SIGN[kind], Decimal(r.data["amount"]), s.accrual.account
        debit, credit = (account, ACCRUED_LIABILITIES) if sign > 0 else (ACCRUED_LIABILITIES, account)
        out.append(PendingEntry(s.accrual_id, s.reversal_period, kind, debit, credit, amount, sign * amount, r))
    return tuple(out)


def pending_text(e: PendingEntry) -> str:
    return (f"{e.accrual_id} {e.kind} {fmt_step(e.pl_effect)} in {e.period} (Dr {e.debit} / Cr {e.credit}; "
            f"approved by {approver_text(e.resolution.data)})")


@dataclass(frozen=True)
class PlLine:
    item: str
    invoices: Decimal
    reversals: Decimal  # negative: the reversal credits expense
    pending: Decimal = ZERO  # pending entries, not yet in the ledger

    @property
    def net(self) -> Decimal:
        """What the ledger and the imports show."""
        return self.invoices + self.reversals

    @property
    def after_pending(self) -> Decimal:
        return self.net + self.pending


def pl_expense(c: Controls, market: str, period: str) -> tuple[PlLine, ...]:
    """P&L expense in `period` from the controlled items: linked invoices minus every reversal line per accrual,
    then unlinked invoices. Pending entries are a separate column."""
    lines = []
    for s in c.accruals:
        if s.accrual.market != market:
            continue
        invoices = sum((l.invoice.amount for l in s.links if l.invoice.period == period), ZERO)
        reversals = sum((line.amount for line in s.reversals if line.period == period), ZERO)
        pending = sum((e.pl_effect for e in pending_entries(s) if e.period == period), ZERO)
        if any(l.invoice.period == period for l in s.links) or reversals or pending:
            lines.append(PlLine(s.accrual_id, invoices, reversals, pending))
    lines += [PlLine(m.invoice.invoice_id, m.invoice.amount, ZERO) for m in c.unmatched
              if (m.invoice.market, m.invoice.period) == (market, period)]
    return tuple(lines)


def pl_pending(c: Controls, market: str, period: str) -> tuple[PendingEntry, ...]:
    return tuple(e for s in c.accruals if s.accrual.market == market for e in pending_entries(s)
                 if e.period == period)


def pl_scopes(c: Controls) -> tuple[tuple[str, str], ...]:
    """(market, period) pairs with invoices, reversals or pending entries in them."""
    pairs = {(i.market, i.period) for i in c.invoices}
    pairs |= {(s.accrual.market, line.period) for s in c.accruals for line in s.reversals}
    pairs |= {(s.accrual.market, e.period) for s in c.accruals for e in pending_entries(s)}
    return tuple(sorted(pairs))


# Text shared by the CLI and the UI


def imports_line(c: Controls) -> str:
    """The current import of each kind and period, and the mapping that read it."""
    return "Imports: " + ("; ".join(imp.label for imp in current_imports(c.imports).values()) or "none")


def evidence_rows(s: AccrualState) -> list[tuple[str, str, str, str, str, str]]:
    """Kind, ID, file, row, amount, note: the accrual row, each linked invoice and each reversal line."""
    a = s.accrual
    rows = [("Accrual", a.accrual_id, a.source.file_name, str(a.source.row), fmt_money(a.amount), "")]
    rows += [("Invoice", l.invoice.invoice_id, l.invoice.source.file_name, str(l.invoice.source.row),
              fmt_money(l.invoice.amount), f"linked: {l.method}") for l in s.links]
    note = "" if s.reversal_state == "ledger_confirmed" else s.reversal_state
    rows += [("Reversal", r.journal_id, r.source.file_name, str(r.source.row), fmt_money(r.amount), note)
             for r in s.reversals]
    if not s.reversals:
        rows.append(("Reversal", "-", s.gl_file or f"no {s.reversal_period} GL extract yet", "-", "-",
                     s.reversal_state))
    return rows


def resolution_text(e: Event) -> str:
    d = e.data
    return (f"{d['issue']}: {d['resolve_kind']} {fmt_money(Decimal(d['amount']))} by {e.by_label}, approved by "
            f"{approver_text(d)}; reason: {d['reason']}")


def net_text(n: NetEffect) -> str:
    return (f"Net {n.period}: P&L expense {fmt_money(n.pl_expense)}; accrued liabilities "
            f"{fmt_money(n.accrued_liabilities)}; accounts payable {fmt_money(n.accounts_payable)}; "
            f"cash {fmt_money(n.cash)}")


PENDING_HEADING = "Pending entries, not yet in the ledger"


# History


@dataclass(frozen=True)
class HistoryItem:
    at: datetime
    what: str
    by: str  # the person and role, the role alone (open mode), or "import"
    detail: str
    logged_at: datetime | None = None  # when it was recorded, if that differs from `at` (signed-in mode)

    @property
    def when(self) -> str:
        return identity.when_text(self.at, self.logged_at)


def approver_text(d: Mapping[str, str]) -> str:
    """Who approved an event: the person and their role, or the role alone for an event from open mode."""
    return f"{d['approved_by_name']} ({d['approved_by']})" if d.get("approved_by_name") else d["approved_by"]


def _approval(d: Mapping[str, str]) -> str:
    return f"; approved by {approver_text(d)}" if d.get("approved_by") else ""


def _event_text(e: Event) -> tuple[str, str]:
    d = e.data
    if e.kind == "link":
        why = f"; override: {d['reason']}; differs: {d['mismatches']}" if d.get("override") else ""
        return f"Linked {d['invoice_id']}", f"{e.evidence}{why}{_approval(d)}"
    if e.kind == "explain":
        return ("Variance explained", f"{d['note']} (accrued {fmt_money(Decimal(d['accrued']))}, invoiced "
                                      f"{fmt_money(Decimal(d['invoiced']))}){_approval(d)}; {e.evidence}")
    if e.kind == "assign_owner":
        return f"Owner assigned: {d['owner']}", e.evidence
    if e.kind == "drop":
        return (f"{d['item_kind'].capitalize()} {d['item']} dropped by import",
                f"{d['file']} replaced {d['replaced']}; reason: {d['reason']}")
    return (f"Resolved: {d['resolve_kind']} {fmt_money(Decimal(d['amount']))}",
            f"{d['issue']}; approved by {approver_text(d)}; reason: {d['reason']}; {e.evidence}")


def history(c: Controls, accrual_id: str) -> tuple[HistoryItem, ...]:
    """Imports that bear on the accrual, every event citing it, and each explanation or resolution that no
    longer applies, in time order."""
    s = c.accrual(accrual_id)
    a = s.accrual
    linked = {l.invoice.invoice_id for l in s.links}
    items = []
    for imp in c.imports:
        if imp.kind == "accruals":
            row = next((r for r in imp.rows if r.accrual_id == accrual_id), None)
            if row is not None:
                items.append(HistoryItem(imp.imported_at, "Accrual imported", "import",
                                         f"{row.source}, {fmt_money(row.amount)}", imp.logged_at))
        elif imp.kind == "invoices":
            for r in imp.rows:
                if r.invoice_id in linked:
                    items.append(HistoryItem(imp.imported_at, f"Invoice {r.invoice_id} imported", "import",
                                             f"{r.source}, {fmt_money(r.amount)}", imp.logged_at))
        elif imp.period == s.reversal_period:
            lines = tuple(r for r in imp.rows if _reverses(r, a))
            state = _reversal_state(imp, lines, a)
            if state == "missing":
                items.append(HistoryItem(imp.imported_at, "Reversal missing", "import",
                                         f"{imp.file_name} has no line reversing {accrual_id}", imp.logged_at))
            else:
                items.append(HistoryItem(imp.imported_at, "Reversal confirmed" if state == "ledger_confirmed"
                                         else "Reversal mismatch", "import",
                                         "; ".join(f"{l.journal_id}, {l.source}" for l in lines), imp.logged_at))
    for e in c.events:
        if e.accrual_id == accrual_id:
            what, detail = _event_text(e)
            items.append(HistoryItem(e.at, what, e.by_label, detail, e.logged_at))
    for lapse in s.lapsed:
        # Shown when the newest import after the event arrived: the facts changed with an import.
        later = [imp.imported_at for imp in c.imports if imp.imported_at >= lapse.event.at]
        items.append(HistoryItem(max(later, default=lapse.event.at), lapse.what, "import",
                                 "; ".join(lapse.changes)))
    return tuple(sorted(items, key=lambda i: i.at))


# Loading and actions


def currencies(ws: Workspace) -> dict[str, str]:
    return {m.code: m.currency for m in ws.config.markets}


def load(store: Store, ws: Workspace, as_of: datetime | None = None) -> Controls:
    cfg = ws.config.require_controls()
    as_of = as_of or datetime.now().replace(microsecond=0)
    return derive(store.control_imports(), store.control_events(), cfg, currencies(ws), as_of)


Outcome = Literal["recorded", "no_op", "refused", "requested"]


@dataclass(frozen=True)
class ActionResult:
    outcome: Outcome
    message: str


@dataclass(frozen=True)
class ImportResult:
    file_name: str
    kind: Kind
    period: str
    rows: int
    # reimported: stored before but not current, so it is current again (recorded); repeat: already current
    outcome: Literal["imported", "reimported", "repeat"]
    replaced: ControlImport | None  # the import this one replaces as current
    mapping: str = "template columns"  # mapping_label of what read this file
    changes: tuple[str, ...] = ()  # against the import it replaces: dropped, added, changed

    @property
    def message(self) -> str:
        what = f"{self.kind} {self.period}"
        if self.outcome == "repeat":
            return f"Repeat: {self.file_name} is already imported as {what}. Nothing changed."
        if self.outcome == "reimported":
            return (f"Imported {self.file_name} again as {what} ({self.mapping}); it is current again, replacing "
                    f"{self.replaced.file_name} ({self.replaced.mapping}).")
        text = f"Imported {self.file_name} as {what} ({self.rows} rows)"
        return text + (f", replacing {self.replaced.file_name}." if self.replaced else ".")


# The fields a replacing import reports as changed, per kind; rows are keyed by the template's ID column.
DIFF_FIELDS: dict[Kind, tuple[str, ...]] = {
    "accruals": ("amount", "account", "supplier", "owner"),
    "invoices": ("amount", "account", "supplier", "reference"),
}


def _value_text(name: str, value: object) -> str:
    if name == "amount":
        return fmt_money(value)
    return str(value) if value != "" else "(blank)"


def _gl_key(line: GlLine) -> str:
    return f"{line.journal_id} {line.account} {line.reference or '-'} {fmt_money(line.amount)}"


def import_changes(kind: Kind, before: tuple[Row, ...], after: tuple[Row, ...]) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Report lines (dropped, added, changed) and the dropped row IDs for a file replacing another."""
    if kind == "gl":
        old, new = Counter(map(_gl_key, before)), Counter(map(_gl_key, after))
        lines = [f"GL lines dropped: {', '.join(sorted((old - new).elements()))}" if old - new else "",
                 f"GL lines added: {', '.join(sorted((new - old).elements()))}" if new - old else ""]
        return tuple(l for l in lines if l), ()
    column = TEMPLATES[kind].id_column
    old = {getattr(r, column): r for r in before}
    new = {getattr(r, column): r for r in after}
    dropped = tuple(k for k in old if k not in new)
    added = tuple(k for k in new if k not in old)
    lines = [f"Dropped: {', '.join(dropped)}" if dropped else "", f"Added: {', '.join(added)}" if added else ""]
    for key in (k for k in new if k in old):
        diffs = [f"{f} {_value_text(f, getattr(old[key], f))} → {_value_text(f, getattr(new[key], f))}"
                 for f in DIFF_FIELDS[kind] if getattr(old[key], f) != getattr(new[key], f)]
        if diffs:
            lines.append(f"Changed: {key} {'; '.join(diffs)}")
    return tuple(l for l in lines if l), dropped


def _drop_blockers(c: Controls, kind: Kind, item: str) -> tuple[str, tuple[str, ...]]:
    """The accrual an event about dropping `item` cites, and why dropping it loses evidence (empty: nothing)."""
    if kind == "accruals":
        s = next((s for s in c.accruals if s.accrual_id == item), None)
        if s is None:
            return item, ()
        why = [f"open issues {', '.join(sorted(s.open_issues))}" if s.open_issues else "",
               f"links {', '.join(l.invoice.invoice_id for l in s.links)}" if s.links else "",
               "events" if any(e.accrual_id == item for e in c.events) else ""]
        return item, tuple(w for w in why if w)
    link_ = c.link_of(item)
    why = [f"linked to {link_.accrual_id}" if link_ else "",
           "events" if any(e.kind == "link" and e.data.get("invoice_id") == item for e in c.events) else "",
           "open signal" if any(s.item == item for s in c.signals) else ""]
    return (link_.accrual_id if link_ else item), tuple(w for w in why if w)


def import_files(store: Store, ws: Workspace, files: Mapping[Kind, tuple[str, bytes]],
                 now: datetime | None = None, allow_drop: bool = False, reason: str = "",
                 by: Actor | str = "",
                 mappings: Mapping[Kind, ControlsMapping] | None = None) -> tuple[ImportResult, ...]:
    """Parse every given file first, each through `mappings[kind]` or its default mapping; store none unless
    all parse and none drops an accrual or invoice that has open issues, links or events (unless allowed, with
    a reason, recorded as a drop event). A file stored before but not current becomes current again, recorded
    as received now; one that is current already is a no-op."""
    cfg = ws.config.require_controls()
    now = now or datetime.now().replace(microsecond=0)
    by = identity.actor(by)
    if refusal := identity.sign_in_refusal(store, by):
        raise ControlsError(refusal)
    mappings = mappings or {}
    parsed = [parse_file(kind, data, name, ws, mappings.get(kind))
              for kind in KINDS if kind in files for name, data in [files[kind]]]
    existing = store.control_imports()
    current = current_imports(existing)
    known = {i.import_id: i for i in existing}
    for f in parsed:
        _check_unique_ids(f, current)
    before_state = derive(existing, store.control_events(), cfg, currencies(ws), datetime.max)
    results, new, drops = [], [], []
    for f in parsed:
        before = current.get((f.kind, f.period))
        if before is not None and before.import_id == f.content_hash:
            results.append(ImportResult(f.file_name, f.kind, f.period, len(f.rows), "repeat", None, f.mapping))
            continue
        changes, dropped = import_changes(f.kind, before.rows, f.rows) if before else ((), ())
        for item in dropped:
            cites, why = _drop_blockers(before_state, f.kind, item)
            if why:
                drops.append((f, before, item, cites, why))
        results.append(ImportResult(f.file_name, f.kind, f.period, len(f.rows),
                                    "reimported" if f.content_hash in known else "imported", before, f.mapping,
                                    changes))
        new.append(f)
    events = []
    if drops:
        listed = "\n".join(f"  - {f.file_name} drops {item} from {before.file_name} ({'; '.join(why)})"
                           for f, before, item, _, why in drops)
        if not allow_drop:
            raise ControlsError(f"The import would drop rows that carry evidence:\n{listed}\nTo drop them anyway, "
                                "allow drops with a reason and who you are (--allow-drop --reason --by).")
        if not reason.strip():
            raise ControlsError(f"Dropping needs a reason.\n{listed}")
        if by.role not in cfg.owners:
            raise ControlsError(f"Dropping needs who is acting (--by), one of: {', '.join(cfg.owners)}; "
                                f"got {by.role!r}.")
        events = [Event("drop", cites, by.role, now, f"{f.file_name} replacing {before.file_name}",
                        {"item": item, "item_kind": f.kind[:-1], "file": f.file_name, "replaced": before.file_name,
                         "reason": reason.strip(), "why": "; ".join(why)}, person=by.person)
                  for f, before, item, cites, why in drops]
    from . import workflow  # the workflow reads close controls

    with workflow.recording(store, ws, now, by):
        store.save_control_files(tuple(new), now, tuple(events))
    return tuple(results)


def _check_unique_ids(f: ControlFile, current: Mapping[tuple[Kind, str], ControlImport]) -> None:
    column = TEMPLATES[f.kind].id_column
    if column is None:
        return
    for (kind, period), imp in current.items():
        if kind != f.kind or period == f.period:
            continue
        clash = {getattr(r, column) for r in imp.rows} & {getattr(r, column) for r in f.rows}
        if clash:
            raise ControlsError(f"{f.file_name} refused: {', '.join(sorted(clash))} already imported from "
                                f"{imp.file_name} ({kind} {period})")


def _refused(message: str) -> ActionResult:
    return ActionResult("refused", message + " Nothing recorded.")


def _role_check(by: Actor, allowed: tuple[str, ...], action: str) -> ActionResult | None:
    if by.role in allowed:
        return None
    return _refused(f"{by.role!r} may not {action}; allowed: {', '.join(allowed)}.")


# Two-person approvals (docs/design-logins.md). In signed-in mode an action that needs a second person is recorded
# as a request; an approver approves it from their own sign-in, which records the action exactly as the one-step
# approved action of open mode, so derived states and figures are the same.

ApprovalAction = Literal["link", "explain", "resolve"]


@dataclass(frozen=True)
class Request:
    request_id: int  # assigned by the store
    action: ApprovalAction
    item: str  # the invoice (link) or the accrual (explain, resolve)
    args: Mapping[str, str]  # the action's own arguments, replayed when it is approved
    requester: Actor
    at: datetime
    summary: tuple[str, ...]  # what the approver needs to see: item, amount, evidence, reason
    owner: str | None = None  # the accrual's owner when requested
    facts: Mapping[str, str] = field(default_factory=dict)  # the figures in the summary; approval needs them unchanged
    logged_at: datetime | None = None  # the real clock; `at` is the effective time

    def __post_init__(self) -> None:
        if self.requester.person is None:
            raise ValueError("a request is made by a signed-in person")


@dataclass(frozen=True)
class Decision:
    request_id: int
    approved: bool
    approver: Actor
    requester: Person
    at: datetime
    reason: str = ""
    logged_at: datetime | None = None  # the real clock; `at` is the effective time

    def __post_init__(self) -> None:
        if self.approver.person is None or self.approver.person.username == self.requester.username:
            raise ValueError("a decision names two different people: the requester and the approver")


@dataclass(frozen=True)
class Approval:
    """`approved_by` in the approvals flow: an approver's own sign-in approving one request."""

    request: Request
    approver: Actor

    def decision(self, at: datetime) -> Decision:
        return Decision(self.request.request_id, True, self.approver, self.request.requester.person, at)


ApprovedBy = str | Approval  # a role typed in open mode, or an Approval in signed-in mode


def _approver(approved_by: ApprovedBy) -> Actor | None:
    if isinstance(approved_by, Approval):
        return approved_by.approver
    return Actor(approved_by) if approved_by else None


def _gate(store: Store, by: Actor, approved_by: ApprovedBy) -> ActionResult | None:
    """Signed-in mode: a person acts, and a second person approves only through an approval request."""
    if refusal := identity.sign_in_refusal(store, by):
        return _refused(refusal)
    if isinstance(approved_by, str) and approved_by and identity.signed_in_mode(store):
        return _refused(identity.OWN_SIGN_IN)
    return None


def _asks(store: Store, approved_by: ApprovedBy) -> bool:
    """True when the approval this action needs has to be requested rather than checked now."""
    return not isinstance(approved_by, Approval) and identity.signed_in_mode(store)


def allowed_approvers(cfg: ControlsConfig, by: str, owner: str | None = None) -> tuple[str, ...]:
    """Approver roles other than the person acting and the accrual's owner."""
    return tuple(r for r in cfg.approver_roles if r not in (by, owner))


def _approval_check(cfg: ControlsConfig, by: Actor, approver: Actor | None, actor: str, owner: str | None = None,
                    what: str = "This") -> ActionResult | None:
    """Segregation of duties: an approver from approver_roles, other than the actor (role and person) and the
    accrual's owner."""
    if approver is None:
        return _refused(f"{what} needs an approver; no approver given. It must be approved by one of: "
                        f"{', '.join(allowed_approvers(cfg, by.role, owner))}.")
    if approver.role == by.role:
        return _refused(f"The approver must differ from {actor} ({by.role}).")
    if approver.person and by.person and approver.person.username == by.person.username:
        return _refused(f"The approver must be another person than {actor} ({by.person.display_name}).")
    if approver.role not in cfg.approver_roles:
        return _refused(f"{approver.role!r} may not approve; approver roles: {', '.join(cfg.approver_roles)}.")
    if approver.role == owner:
        return _refused(f"The approver must differ from the accrual's owner ({owner}).")
    return None


def _approval_data(approved_by: ApprovedBy) -> dict[str, str]:
    approver = _approver(approved_by)
    if approver is None:
        return {}
    data = {"approved_by": approver.role}
    if approver.person:
        data |= {"approved_by_user": approver.person.username, "approved_by_name": approver.person.display_name}
    if isinstance(approved_by, Approval):
        data["request"] = str(approved_by.request.request_id)
    return data


def _facts_changed(approved_by: ApprovedBy, facts: Mapping[str, str]) -> ActionResult | None:
    """An approval applies to the facts its approver was shown; refused when they are no longer the facts."""
    if not isinstance(approved_by, Approval):
        return None
    shown = approved_by.request.facts
    changed = [f"{name} {shown.get(name, '(not recorded)')} → {value}" for name, value in facts.items()
               if shown.get(name) != value]
    if not changed:
        return None
    return _refused(f"The facts changed since the request: {'; '.join(changed)}. Reject it and request again.")


def _record(store: Store, ws: Workspace, event: Event, message: str, approved_by: ApprovedBy = "") -> ActionResult:
    from . import workflow

    decision = approved_by.decision(event.at) if isinstance(approved_by, Approval) else None
    with workflow.recording(store, ws, event.at, Actor(event.by, event.person)):
        store.add_control_event(event, decision)
    return ActionResult("recorded", message)


def _decide(store: Store, ws: Workspace, decision: Decision) -> None:
    from . import workflow

    with workflow.recording(store, ws, decision.at, decision.approver):
        store.add_decision(decision)


def approvals(store: Store) -> tuple[tuple[Request, Decision | None], ...]:
    """Every request with its decision (None while it waits), oldest first."""
    decided = store.decisions()
    return tuple((r, decided.get(r.request_id)) for r in store.requests())


def pending_requests(store: Store) -> tuple[Request, ...]:
    return tuple(r for r, d in approvals(store) if d is None)


def _request(store: Store, ws: Workspace, cfg: ControlsConfig, request: Request) -> ActionResult:
    same = next((r for r in pending_requests(store)
                 if (r.action, r.item, dict(r.args), r.requester) == (request.action, request.item,
                                                                    dict(request.args), request.requester)), None)
    if same is not None:
        return ActionResult("no_op", f"This already waits for approval as request {same.request_id}. "
                                     "Nothing changed.")
    from . import workflow

    with workflow.recording(store, ws, request.at, request.requester):
        request_id = store.add_request(request)
    who = ", ".join(allowed_approvers(cfg, request.requester.role, request.owner))
    return ActionResult("requested", f"Request {request_id}: {request.summary[0]}. It waits in Approvals for "
                                     f"another person ({who}) to approve it from their own sign-in.")


def link(store: Store, ws: Workspace, invoice_id: str, accrual_id: str, by: Actor | str, override: bool = False,
         reason: str = "", approved_by: ApprovedBy = "", now: datetime | None = None) -> ActionResult:
    now = now or datetime.now().replace(microsecond=0)
    by = identity.actor(by)
    if refusal := _gate(store, by, approved_by):
        return refusal
    c = load(store, ws, now)
    if refusal := _role_check(by, (c.cfg.ap_role,), "link invoices"):
        return refusal
    try:
        inv, s = c.invoice(invoice_id), c.accrual(accrual_id)
    except ControlsError as e:
        return _refused(f"{e}.")
    current = c.link_of(invoice_id)
    if current is not None and current.accrual_id == accrual_id:
        return ActionResult("no_op", f"{invoice_id} is already linked to {accrual_id} ({current.method}). "
                                     "Nothing changed.")
    if override and not reason.strip():
        return _refused("An override link needs a reason.")
    asks = override and _asks(store, approved_by)
    if override and not asks and (refusal := _approval_check(c.cfg, by, _approver(approved_by), "the person linking",
                                                             what="An override link")):
        return refusal
    if not override:
        if current is not None:
            return _refused(f"{invoice_id} is linked to {current.accrual_id} ({current.method}); relinking needs "
                            "an override with a reason and an approver.")
        candidates = c.candidates(invoice_id)
        if accrual_id not in candidates:
            return _refused(f"{accrual_id} is not a candidate for {invoice_id} (candidates: "
                            f"{', '.join(candidates) or 'none'}); linking it needs an override with a reason "
                            "and an approver.")
    mismatches = link_mismatches(inv, s.accrual)
    facts = {"invoice": f"{inv.supplier}, {c.currency(inv.market)} {fmt_money(inv.amount)}",
             "accrual": f"{s.accrual.supplier}, {fmt_money(s.accrual.amount)}",
             "differs": "; ".join(mismatches) or "none", "linked to": current.accrual_id if current else "none"}
    if asks:
        summary = (f"Override link of {invoice_id} to {accrual_id}",
                   f"Invoice {inv.source}: {facts['invoice']}", f"Accrual {s.accrual.source}: {facts['accrual']}",
                   f"Differs: {facts['differs']}",
                   *((f"Replaces the link to {current.accrual_id}",) if current else ()),
                   f"Reason: {reason.strip()}")
        return _request(store, ws, c.cfg, Request(0, "link", invoice_id, {"accrual_id": accrual_id,
                                                                      "reason": reason.strip()},
                                              by, now, summary, facts=facts))
    if refusal := _facts_changed(approved_by, facts):
        return refusal
    data = {"invoice_id": invoice_id,
            **({"override": "yes", "reason": reason.strip(), **_approval_data(approved_by),
                "mismatches": "; ".join(mismatches) or "none"} if override else {})}
    event = Event("link", accrual_id, by.role, now, f"invoice {inv.source}; accrual {s.accrual.source}", data,
                  person=by.person)
    return _record(store, ws, event, f"Linked {invoice_id} to {accrual_id}" + (" (override)." if override else "."),
                   approved_by)


def explain_needs_approval(cfg: ControlsConfig, s: AccrualState) -> bool:
    return abs(s.difference) > cfg.explain_approval_abs


def explain(store: Store, ws: Workspace, accrual_id: str, note: str, by: Actor | str, approved_by: ApprovedBy = "",
            now: datetime | None = None) -> ActionResult:
    now = now or datetime.now().replace(microsecond=0)
    by = identity.actor(by)
    if refusal := _gate(store, by, approved_by):
        return refusal
    c = load(store, ws, now)
    try:
        s = c.accrual(accrual_id)
    except ControlsError as e:
        return _refused(f"{e}.")
    if s.owner is None:
        return _refused(f"{accrual_id} has no owner to explain it; the close lead assigns one first.")
    if refusal := _role_check(by, (s.owner,), f"explain {accrual_id}"):
        return refusal
    note = note.strip()
    if not note:
        return _refused("The explanation note is blank.")
    if s.explained is not None and s.explained.data["note"] == note:
        return ActionResult("no_op", f"{accrual_id} is already explained with this note. Nothing changed.")
    if s.invoice_state != "invoiced_with_variance":
        return _refused(f"{accrual_id} has no invoice variance to explain (invoice state {s.invoice_state}).")
    approver = _approver(approved_by)
    needs = explain_needs_approval(c.cfg, s)
    asks = needs and approver is None and _asks(store, approved_by)
    if (needs or approver) and not asks:
        what = (f"A variance of {fmt_step(s.difference)} is over {fmt_money(c.cfg.explain_approval_abs)}; "
                "the explanation")
        if refusal := _approval_check(c.cfg, by, approver, "the person explaining", s.owner, what):
            return refusal
    evidence = "; ".join(f"{l.invoice.invoice_id} {l.invoice.source}" for l in s.links)
    facts = {"accrued": fmt_money(s.accrual.amount), "invoiced": fmt_money(s.invoiced),
             "difference": fmt_step(s.difference)}
    if asks:
        ccy = c.currency(s.accrual.market)
        summary = (f"Explanation of the {accrual_id} variance of {ccy} {facts['difference']}",
                   f"Accrued {facts['accrued']}, invoiced {facts['invoiced']}; over the "
                   f"{fmt_money(c.cfg.explain_approval_abs)} that needs an approver",
                   f"Evidence: {evidence or 'no linked invoices'}", f"Note: {note}")
        return _request(store, ws, c.cfg, Request(0, "explain", accrual_id, {"note": note}, by, now, summary, s.owner,
                                              facts))
    if refusal := _facts_changed(approved_by, facts):
        return refusal
    data = {"note": note, "accrued": s.facts["accrued"], "invoiced": s.facts["invoiced"],
            **_approval_data(approved_by)}
    event = Event("explain", accrual_id, by.role, now, evidence, data, person=by.person)
    return _record(store, ws, event, f"Explained the {accrual_id} variance of +{fmt_money(s.difference)}"
                                 + (f", approved by {approver.label}." if approver else "."), approved_by)


def assign(store: Store, ws: Workspace, accrual_id: str, owner: str, by: Actor | str,
           now: datetime | None = None) -> ActionResult:
    now = now or datetime.now().replace(microsecond=0)
    by = identity.actor(by)
    if refusal := _gate(store, by, ""):
        return refusal
    c = load(store, ws, now)
    if refusal := _role_check(by, (c.cfg.close_lead,), "assign owners"):
        return refusal
    try:
        s = c.accrual(accrual_id)
    except ControlsError as e:
        return _refused(f"{e}.")
    if owner not in c.cfg.owners:
        return _refused(f"{owner!r} is not an active role; roles: {', '.join(c.cfg.owners)}.")
    if s.owner == owner:
        return ActionResult("no_op", f"{accrual_id} is already owned by {owner}. Nothing changed.")
    event = Event("assign_owner", accrual_id, by.role, now, f"accrual {s.accrual.source}", {"owner": owner},
                  person=by.person)
    return _record(store, ws, event, f"Assigned {accrual_id} to {owner}.")


def resolve(store: Store, ws: Workspace, accrual_id: str, kind: str, amount: Decimal, reason: str, by: Actor | str,
            approved_by: ApprovedBy = "", issue: str = "", now: datetime | None = None) -> ActionResult:
    """Record an approved resolution of one issue (ISSUE_RULES), for exactly its amount at risk. In signed-in mode
    without an Approval it is requested instead."""
    now = now or datetime.now().replace(microsecond=0)
    by = identity.actor(by)
    if refusal := _gate(store, by, approved_by):
        return refusal
    c = load(store, ws, now)
    if refusal := _role_check(by, c.cfg.owners, "record a resolution"):
        return refusal
    if kind not in RESOLVE_KINDS:
        return _refused(f"Resolution kind must be one of {', '.join(RESOLVE_KINDS)}, got {kind!r}.")
    reason = reason.strip()
    if not reason:
        return _refused("A resolution needs a reason.")
    try:
        s = c.accrual(accrual_id)
    except ControlsError as e:
        return _refused(f"{e}.")
    approver = _approver(approved_by)
    asks = _asks(store, approved_by)
    if not asks and (refusal := _approval_check(c.cfg, by, approver, "the person resolving", s.owner,
                                                "A resolution")):
        return refusal
    if any((r.data["resolve_kind"], Decimal(r.data["amount"]), r.data["reason"], r.by)
           == (kind, amount, reason, by.role)
           and (approver is None or r.data["approved_by"] == approver.role) and issue in ("", r.data["issue"])
           for r in s.resolutions):
        return ActionResult("no_op", f"{accrual_id} already has this resolution. Nothing changed.")
    open_issues = c.open_issues(accrual_id)
    if not issue:
        if not open_issues:
            return _refused(f"{accrual_id} is {s.overall}; there is nothing open to resolve.")
        if len(open_issues) > 1:
            return _refused(f"{accrual_id} has {len(open_issues)} open issues: {', '.join(open_issues)}; "
                            "name the one this resolves (--issue).")
        issue = open_issues[0]
    if issue not in open_issues:
        return _refused(f"{issue} is not open on {accrual_id}; open issues: {', '.join(open_issues) or 'none'}.")
    rule = ISSUE_RULES[issue]
    if not rule.kinds:
        return _refused(f"{issue} is not closed by a resolution; {rule.instead}.")
    if kind not in rule.kinds:
        return _refused(f"{issue} may be resolved by {', '.join(rule.kinds)}, not {kind}.")
    expected = c.signal(accrual_id, issue).amount_at_risk
    facts = {"issue": issue, "amount at risk": fmt_money(expected), "accrued": fmt_money(s.accrual.amount),
             "invoiced": fmt_money(s.invoiced), "reversal": s.facts["reversal"]}
    if refusal := _facts_changed(approved_by, facts):
        return refusal
    if amount != expected:
        return _refused(f"The amount must equal the {issue} amount at risk, {fmt_money(expected)}; "
                        f"got {fmt_money(amount)}.")
    if asks:
        ccy = c.currency(s.accrual.market)
        summary = (f"Resolution of {accrual_id} {issue}: {kind} {ccy} {fmt_money(amount)}",
                   f"Accrual {s.accrual.source}: {s.accrual.supplier}, {facts['accrued']}; owner "
                   f"{s.owner or 'none'}",
                   f"Evidence: invoiced {facts['invoiced']}, reversal {facts['reversal']}",
                   f"Reason: {reason}")
        return _request(store, ws, c.cfg, Request(0, "resolve", accrual_id, {"kind": kind, "amount": str(amount),
                                                                        "reason": reason, "issue": issue},
                                              by, now, summary, s.owner, facts))
    data = {"issue": issue, "resolve_kind": kind, "amount": str(amount), "reason": reason,
            **_approval_data(approved_by), **s.facts}
    event = Event("resolve", accrual_id, by.role, now, f"accrual {s.accrual.source}", data, person=by.person)
    return _record(store, ws, event, f"Resolved {accrual_id}: {kind} {fmt_money(amount)}, approved by {approver.label}.",
                   approved_by)


_APPROVE: dict[ApprovalAction, Callable[[Store, Workspace, Request, Approval, datetime], ActionResult]] = {
    "link": lambda store, ws, r, a, now: link(store, ws, r.item, r.args["accrual_id"], r.requester, override=True,
                                              reason=r.args["reason"], approved_by=a, now=now),
    "explain": lambda store, ws, r, a, now: explain(store, ws, r.item, r.args["note"], r.requester, a, now),
    "resolve": lambda store, ws, r, a, now: resolve(store, ws, r.item, r.args["kind"], Decimal(r.args["amount"]),
                                                    r.args["reason"], r.requester, a, r.args["issue"], now),
}


def _decider_check(store: Store, cfg: ControlsConfig, request_id: int, by: Actor) -> Request | ActionResult:
    """The pending request, if `by` may decide it: a signed-in approver who is not the requester."""
    if not identity.signed_in_mode(store):
        return _refused("Approvals are for signed-in mode; in open mode the approver is given with the action.")
    if refusal := identity.sign_in_refusal(store, by):
        return _refused(refusal)
    found = next(((r, d) for r, d in approvals(store) if r.request_id == request_id), None)
    if found is None:
        return _refused(f"No approval request {request_id}.")
    request, decision = found
    if decision is not None:
        return _refused(f"Request {request_id} was already {'approved' if decision.approved else 'rejected'} by "
                        f"{decision.approver.label}.")
    if by.person == request.requester.person:
        return _refused(f"You requested {request_id}; another person must decide it from their own sign-in.")
    if by.role not in cfg.approver_roles:
        return _refused(f"{by.role!r} may not approve; approver roles: {', '.join(cfg.approver_roles)}.")
    return request


def approve(store: Store, ws: Workspace, request_id: int, by: Actor, now: datetime | None = None) -> ActionResult:
    """Records the requested action as approved by `by`, after the action's own checks pass again now and its facts
    are those the request showed. When the action is already in place, the approval closes the request with
    nothing to record."""
    now = now or datetime.now().replace(microsecond=0)
    checked = _decider_check(store, ws.config.require_controls(), request_id, by)
    if isinstance(checked, ActionResult):
        return checked
    requester = store.user(checked.requester.person.username)
    if requester is None or not requester.active or checked.requester.role not in requester.roles:
        return _refused(f"The requester {checked.requester.label} no longer holds that role; reject the request.")
    result = _APPROVE[checked.action](store, ws, checked, Approval(checked, by), now)
    if result.outcome != "no_op":
        return result
    already = result.message.removesuffix(" Nothing changed.")
    _decide(store, ws, Decision(request_id, True, by, checked.requester.person, now, f"Nothing to record: {already}"))
    return ActionResult("recorded", f"Approved request {request_id} with nothing to record: {already} "
                                    "The request is closed.")


def reject(store: Store, ws: Workspace, request_id: int, by: Actor, reason: str,
           now: datetime | None = None) -> ActionResult:
    now = now or datetime.now().replace(microsecond=0)
    checked = _decider_check(store, ws.config.require_controls(), request_id, by)
    if isinstance(checked, ActionResult):
        return checked
    if not reason.strip():
        return _refused("A rejection needs a reason.")
    _decide(store, ws, Decision(request_id, False, by, checked.requester.person, now, reason.strip()))
    return ActionResult("recorded", f"Rejected request {request_id}: {checked.summary[0]}.")


def decision_text(d: Decision) -> str:
    verdict = "Approved" if d.approved else "Rejected"
    return f"{verdict} by {d.approver.label}" + (f": {d.reason}" if d.reason else "")


# Previews


def _cell_text(value: object) -> str:
    return fmt_money(value) if isinstance(value, Decimal) else str(value)


@dataclass(frozen=True)
class Preview:
    """What `controls inspect` and the UI's import preview show, in the same words."""

    lines: tuple[str, ...]  # the file, sheet, header row and sign
    columns: tuple[dict[str, str], ...]  # each template column: where it is read from, found or not
    rows_heading: str
    rows: tuple[dict[str, str], ...]  # the first rows as they will be stored
    warnings: tuple[str, ...]
    verdict: str  # would import, or would be refused with every problem
    imports: bool


def preview(r: Reading, limit: int = 10) -> Preview:
    lines = [f"File: {r.file_name} ({r.mapping.format}, {r.kind}, read with {mapping_label(r.mapping)})"]
    if r.sheets:
        lines.append(f"Sheets: {', '.join(r.sheets)}; using {r.sheet}")
    lines.append(f"Header row {r.mapping.header_row}: {' | '.join(h for h in r.headers if h) or '(empty)'}")
    if r.mapping.amount_sign == "negate":
        lines.append("Amounts are negated: the file shows them credit-positive.")
    columns = tuple({"Column": c.name, "Read from": c.source, "Found": "yes" if c.found else "MISSING",
                     "Closest header": c.closest or ""} for c in r.columns)
    names = TEMPLATES[r.kind].columns
    rows = tuple({"Row": str(row.source.row), **{c: _cell_text(getattr(row, c)) for c in names}}
                 for row in r.rows[:limit])
    problems = [r.missing] if r.missing else list(r.problems)
    verdict = ("Would be refused:\n" + "\n".join(f"  - {p}" for p in problems) if problems else
               f"Would import as {r.kind} {r.rows[0].period} ({len(r.rows)} rows). Nothing imported yet.")
    return Preview(tuple(lines), columns, f"First {len(rows)} of {len(r.rows)} rows as they will be read:", rows,
                   tuple(f"Warning: {w}" for w in r.warnings), verdict, not problems)
