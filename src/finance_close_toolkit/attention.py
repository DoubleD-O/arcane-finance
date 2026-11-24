from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import TYPE_CHECKING, Literal

from .config import Workspace
from .kpi import Diff
from .release import SlotState, propose
from .validation import ValidationException, check_period

if TYPE_CHECKING:
    from .store import Store

SlotStatus = Literal["missing", "rejected", "needs_review", "validated", "approved", "changed"]
SignalType = Literal["MISSING_SUBMISSION", "VALIDATION_FAILED", "MATERIAL_CHANGE"]


def due_date(period: str, due_day: int) -> date:
    year, month = map(int, period.split("-"))
    year, month = (year + 1, 1) if month == 12 else (year, month + 1)
    return date(year, month, due_day)


@dataclass(frozen=True)
class SlotView:
    slot: SlotState
    status: SlotStatus
    change: Diff | None  # set when status is "changed"

    @property
    def note(self) -> str:
        s = self.slot
        if s.stale:
            return "validated under an older mapping; run revalidate"
        if s.validation is not None and s.validation.state == "validated" and s.rate is None:
            return f"no {s.scenario} rate for {s.market.currency}"
        if self.change is not None:
            return "material change" if self.change.material else "change below threshold"
        return ""


def _status(slot: SlotState, has_release: bool) -> SlotStatus:
    if slot.submission is None:
        return "missing"
    assert slot.validation is not None
    if slot.validation.state != "validated":
        return slot.validation.state
    if slot.proposed is None or not has_release or not slot.released:
        return "validated"
    return "changed" if slot.changes_release else "approved"


@dataclass(frozen=True)
class Signal:
    type: SignalType
    market: str
    scenario: str
    period: str
    owner: str
    due: date
    overdue: bool
    trigger: str
    release_id: str | None
    impact: Diff | None  # None means the data cannot support an impact ("unknown")
    exceptions: tuple[ValidationException, ...]
    next_action: str


@dataclass(frozen=True)
class Board:
    period: str
    as_of: datetime
    due: date
    current_release_id: str | None
    slots: tuple[SlotView, ...]
    signals: tuple[Signal, ...]
    changes: Diff | None  # all changed slots together, when any slot is "changed"

    def slot(self, market: str, scenario: str) -> SlotView:
        return next(v for v in self.slots if (v.slot.market.code, v.slot.scenario) == (market, scenario))


def board(store: Store, ws: Workspace, period: str, as_of: datetime | None = None) -> Board:
    check_period(period)
    as_of = as_of or datetime.now().replace(microsecond=0)
    due = due_date(period, ws.config.due_day)
    proposal = propose(store, ws, period)
    release_id = proposal.current.release_id if proposal.current else None
    overall = proposal.diff(ws) if proposal.current else None

    views, signals = [], []
    for slot in proposal.slots:
        status = _status(slot, proposal.current is not None)
        change = overall.for_slot(slot.scenario, slot.market.code) if status == "changed" and overall else None
        views.append(SlotView(slot, status, change))
        m = slot.market

        def signal(type_: SignalType, owner: str, trigger: str, impact: Diff | None, next_action: str,
                   overdue: bool = False, exceptions: tuple[ValidationException, ...] = ()) -> Signal:
            return Signal(type_, m.code, slot.scenario, period, owner, due, overdue, trigger, release_id,
                          impact, exceptions, next_action)

        if status == "missing":
            overdue = as_of.date() > due
            signals.append(signal(
                "MISSING_SUBMISSION", m.owner,
                f"no {slot.scenario} file received for {period}" + (" and it is overdue" if overdue else ""),
                None, f"Chase {m.owner} for the {m.code} {slot.scenario} file", overdue=overdue,
            ))
        elif status in ("rejected", "needs_review"):
            v = slot.validation
            n = len(v.exceptions)
            signals.append(signal(
                "VALIDATION_FAILED", m.owner,
                f"{slot.submission.file_name} is {status} with {n} exception{'s' * (n != 1)}",
                None,
                "Correct the file and resubmit, or fix the mapping and run revalidate"
                + (f"; {release_id} stays current" if release_id else ""),
                exceptions=v.exceptions,
            ))
        elif status == "changed" and change is not None and change.material:
            signals.append(signal(
                "MATERIAL_CHANGE", ws.config.regional_owner,
                f"{slot.submission.file_name} would change {release_id} materially",
                change, "Review the change, then approve it or ask the market to resubmit",
            ))
    any_changed = any(v.status == "changed" for v in views)
    return Board(period, as_of, due, release_id, tuple(views), tuple(signals), overall if any_changed else None)
