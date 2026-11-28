from __future__ import annotations

import hashlib
import re
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Literal

from . import identity
from .config import ChartOfAccounts, Mapping, Workspace
from .intake import IntakeError, RawRow, Submission, closest_header, read_source
from .money import ZERO, fmt_amount

if TYPE_CHECKING:
    from .store import Store

State = Literal["validated", "needs_review", "rejected"]
_RANK: dict[State, int] = {"validated": 0, "needs_review": 1, "rejected": 2}
PERIOD_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")
EXCEL_DATE_RE = re.compile(r"^(\d{4}-\d{2})-\d{2}( 00:00:00)?$")  # 2026-10 typed into a date-formatted cell


def date_period_fix(value: str, column: str) -> str | None:
    """How to fix a period cell Excel turned into a date, or None when it does not look like one."""
    m = EXCEL_DATE_RE.match(value)
    if m is None:
        return None
    return f"Excel turned the period into a date ({value[:10]}); format the {column} column as text and type {m[1]}"


@dataclass(frozen=True)
class ValidationException:
    code: str
    row: int | None
    detail: str
    account: str | None = None
    amount: Decimal | None = None


@dataclass(frozen=True)
class MappedFact:
    source_row: int
    account: str
    amount: Decimal  # local currency, profit sign


@dataclass(frozen=True)
class TieOut:
    """Totals of the amounts as they appear in the file (source sign, local currency)."""

    source_total: Decimal
    mapped_total: Decimal
    unmapped_total: Decimal


@dataclass(frozen=True)
class Validation:
    submission_id: str
    mapping_version: str
    state: State
    exceptions: tuple[ValidationException, ...]
    facts: tuple[MappedFact, ...]
    tie_out: TieOut


@dataclass(frozen=True)
class RuleInput:
    period: str
    currency: str
    headers: tuple[str, ...]
    rows: tuple[RawRow, ...]
    mapping: Mapping


def _missing_column(i: RuleInput) -> list[ValidationException]:
    out = []
    for col in i.mapping.layout.columns:
        if col in i.headers:
            continue
        closest = closest_header(col, i.headers)
        hint = f'closest header "{closest}"' if closest else "no similar header"
        out.append(
            ValidationException(
                "MISSING_COLUMN", i.mapping.layout.header_row, f'column "{col}" not in header row; {hint}'
            )
        )
    return out


def _wrong_period(i: RuleInput) -> list[ValidationException]:
    if not i.mapping.layout.period_column:
        return []
    rows_by_value: dict[str, list[int]] = defaultdict(list)
    for r in i.rows:
        if r.period_value != i.period:
            rows_by_value[r.period_value or ""].append(r.row_number)
    return [
        ValidationException(
            "WRONG_PERIOD", rows[0], f'period "{value}" on {len(rows)} row(s), expected {i.period}'
            + (f"; {fix}" if (fix := date_period_fix(value, i.mapping.layout.period_column)) else "")
        )
        for value, rows in rows_by_value.items()
    ]


def _bad_amount(i: RuleInput) -> list[ValidationException]:
    return [
        ValidationException("BAD_AMOUNT", r.row_number, f'amount "{r.amount_text}" is not a number', r.source_account)
        for r in i.rows
        if r.amount is None
    ]


def _unmapped_account(i: RuleInput) -> list[ValidationException]:
    return [
        ValidationException(
            "UNMAPPED_ACCOUNT",
            r.row_number,
            f'source account {r.source_account or "(blank)"} "{r.description}" '
            f"{fmt_amount(r.amount or ZERO)} {i.currency} is not in the {i.mapping.market} mapping",
            r.source_account,
            r.amount,
        )
        for r in i.rows
        if r.source_account not in i.mapping.accounts
    ]


def _duplicate_row(i: RuleInput) -> list[ValidationException]:
    groups: dict[tuple[str, str, Decimal], list[RawRow]] = defaultdict(list)
    for r in i.rows:
        if r.amount is not None:
            groups[(r.source_account, r.description, r.amount)].append(r)
    out = []
    for (account, desc, amount), rows in groups.items():
        if len(rows) < 2:
            continue
        where = ", ".join(str(r.row_number) for r in rows)
        for r in rows:
            out.append(
                ValidationException(
                    "DUPLICATE_ROW",
                    r.row_number,
                    f'account {account} "{desc}" {fmt_amount(amount)} appears on rows {where}',
                    account,
                    amount,
                )
            )
    return out


@dataclass(frozen=True)
class Rule:
    code: str
    check: Callable[[RuleInput], list[ValidationException]]
    state: State


RULES: tuple[Rule, ...] = (
    Rule("MISSING_COLUMN", _missing_column, "rejected"),
    Rule("WRONG_PERIOD", _wrong_period, "rejected"),
    Rule("BAD_AMOUNT", _bad_amount, "needs_review"),
    Rule("UNMAPPED_ACCOUNT", _unmapped_account, "needs_review"),
    Rule("DUPLICATE_ROW", _duplicate_row, "needs_review"),
)


def canonical_amount(amount: Decimal, account: str, mapping: Mapping, coa: ChartOfAccounts) -> Decimal:
    if mapping.sign == "tb":
        return -amount
    return amount if coa.type_of(account) == "revenue" else -amount


def map_rows(rows: tuple[RawRow, ...], mapping: Mapping, coa: ChartOfAccounts) -> tuple[MappedFact, ...]:
    facts = []
    for r in rows:
        account = mapping.accounts.get(r.source_account)
        if account is not None and r.amount is not None:
            facts.append(MappedFact(r.row_number, account, canonical_amount(r.amount, account, mapping, coa)))
    return tuple(facts)


def tie_out(rows: tuple[RawRow, ...], mapping: Mapping) -> TieOut:
    numeric = [r for r in rows if r.amount is not None]
    mapped = sum((r.amount for r in numeric if r.source_account in mapping.accounts), ZERO)
    unmapped = sum((r.amount for r in numeric if r.source_account not in mapping.accounts), ZERO)
    return TieOut(mapped + unmapped, mapped, unmapped)


def validate(
    submission_id: str,
    period: str,
    currency: str,
    headers: tuple[str, ...],
    rows: tuple[RawRow, ...],
    mapping: Mapping,
    coa: ChartOfAccounts,
) -> Validation:
    rule_input = RuleInput(period, currency, headers, rows, mapping)
    state: State = "validated"
    exceptions: list[ValidationException] = []
    for rule in RULES:
        found = rule.check(rule_input)
        if found and _RANK[rule.state] > _RANK[state]:
            state = rule.state
        exceptions.extend(found)
    return Validation(
        submission_id, mapping.version, state, tuple(exceptions), map_rows(rows, mapping, coa), tie_out(rows, mapping)
    )


@dataclass(frozen=True)
class IngestResult:
    submission: Submission
    validation: Validation
    repeat: bool  # the same file bytes were already stored for this slot
    revalidated: bool  # a new validation was recorded for a stored submission
    previous: Submission | None  # the slot's latest submission before this ingest
    latest: Submission  # the slot's latest submission after this ingest

    @property
    def rereceived(self) -> bool:
        return self.repeat and self.previous is not None and self.previous.submission_id != self.submission.submission_id

    @property
    def is_latest(self) -> bool:
        return self.latest.submission_id == self.submission.submission_id


def check_period(period: str) -> None:
    if not PERIOD_RE.match(period):
        raise IntakeError(f"period must look like 2026-10, got {period!r}")


def _check_slot(ws: Workspace, market: str, period: str, scenario: str) -> None:
    ws.config.market(market)
    if scenario not in ws.config.scenarios:
        raise IntakeError(f"unknown scenario {scenario!r}; configured: {', '.join(ws.config.scenarios)}")
    check_period(period)


def submission_id(market: str, period: str, scenario: str, content_hash: str) -> str:
    return hashlib.sha256(f"{market}|{period}|{scenario}|{content_hash}".encode("utf-8")).hexdigest()


def ingest(
    store: Store,
    ws: Workspace,
    path: Path,
    market: str,
    period: str,
    scenario: str,
    received_at: datetime | None = None,
    by: identity.Actor | str | None = None,
) -> IngestResult:
    """`by` is who sends the file; in signed-in mode a signed-in person (IdentityError otherwise). What the file
    changes in the needs-attention items is recorded with it, at the receipt time."""
    from . import workflow  # the workflow reads the close board, which reads validations

    by = identity.require_signed_in(store, by)
    _check_slot(ws, market, period, scenario)
    path = Path(path)
    content_hash = hashlib.sha256(path.read_bytes()).hexdigest()
    sid = submission_id(market, period, scenario, content_hash)
    mapping = ws.mapping(market)
    received_at = received_at or datetime.now().replace(microsecond=0)
    previous = store.latest_submission(market, period, scenario)

    existing = store.submission(sid)
    if existing is not None:
        # Bytes that are already the slot's latest change nothing; older bytes sent again are a new receipt.
        validation = store.validation(sid, mapping.version)
        revalidated = validation is None
        with workflow.recording(store, ws, received_at, by, period):
            if previous.submission_id != sid:
                store.add_receipt(sid, received_at, by)
            if revalidated:
                validation = _validate_stored(store, ws, existing)
                store.save_validation(validation, mapping)
        latest = store.latest_submission(market, period, scenario)
        return IngestResult(store.submission(sid), validation, True, revalidated, previous, latest)

    table = read_source(path, mapping)
    submission = Submission(sid, market, period, scenario, path.name, table.sheet, received_at)
    currency = ws.config.market(market).currency
    validation = validate(sid, period, currency, table.headers, table.rows, mapping, ws.coa)
    with workflow.recording(store, ws, received_at, by, period):
        store.save_submission(submission, content_hash, table.headers, table.rows, validation, mapping, by)
    latest = store.latest_submission(market, period, scenario)
    return IngestResult(submission, validation, False, False, previous, latest)


def _validate_stored(store: Store, ws: Workspace, submission: Submission) -> Validation:
    headers, rows = store.raw_rows(submission.submission_id)
    return validate(
        submission.submission_id,
        submission.period,
        ws.config.market(submission.market).currency,
        headers,
        rows,
        ws.mapping(submission.market),
        ws.coa,
    )


@dataclass(frozen=True)
class RevalidateResult:
    submission: Submission
    validation: Validation
    new: bool


def revalidate(store: Store, ws: Workspace, period: str, by: identity.Actor | str | None = None,
               at: datetime | None = None) -> tuple[RevalidateResult, ...]:
    """What the new validations change in the needs-attention items is recorded with them, at `at` (default:
    the store's clock)."""
    from . import workflow

    by = identity.require_signed_in(store, by)
    check_period(period)
    with workflow.recording(store, ws, at or store.clock(), by, period):
        return _revalidate(store, ws, period)


def _revalidate(store: Store, ws: Workspace, period: str) -> tuple[RevalidateResult, ...]:
    results = []
    for market in ws.config.markets:
        for scenario in ws.config.scenarios:
            submission = store.latest_submission(market.code, period, scenario)
            if submission is None:
                continue
            mapping = ws.mapping(market.code)
            stored = store.validation(submission.submission_id, mapping.version)
            if stored is not None:
                results.append(RevalidateResult(submission, stored, new=False))
                continue
            validation = _validate_stored(store, ws, submission)
            store.save_validation(validation, mapping)
            results.append(RevalidateResult(submission, validation, new=True))
    return tuple(results)
