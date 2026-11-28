"""The needs-attention workflow (docs/design-attention.md): who is handling an item, by when, what they said.

The condition stays derived from the data (attention.board, controls.load); this module never changes a signal.
It turns the current signals of both sources into Conditions, records when each one appears, changes evidence or
clears (observations), and folds those with people's actions into each item's state through STEPS.

Observations are recorded by the operations that change the data (`recording`), stamped with the data's effective
time and who acted. Reads never write: `load` folds the events recorded up to its as-of time and overlays, in
memory only, what the data shows then that nobody has recorded yet (after a config-only change, for example).
"""

from __future__ import annotations

import hashlib
import json
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta
from typing import TYPE_CHECKING, Callable, Iterator, Literal, Mapping

from . import attention, controls, identity
from .config import Workspace
from .identity import Actor

if TYPE_CHECKING:
    from .store import Store

Source = Literal["board", "controls"]
State = Literal["new", "acknowledged", "resolved", "reopened"]
Observation = Literal["appeared", "evidence", "cleared"]
ActionKind = Literal["ack", "assign", "due", "comment"]
EventKind = Literal["appeared", "evidence", "cleared", "ack", "assign", "due", "comment"]
Outcome = Literal["recorded", "no_op", "refused"]

ACTIONS: tuple[ActionKind, ...] = ("ack", "assign", "due", "comment")
STATES: tuple[State, ...] = ("new", "acknowledged", "reopened", "resolved")
OPEN = "open"  # stands for "new" on a first occurrence, "reopened" on a later one

# (state before, event) -> state after. Events missing from the table leave the state as it is.
STEPS: Mapping[tuple[State | None, EventKind], State | str] = {
    (None, "appeared"): "new",
    ("resolved", "appeared"): "reopened",
    ("new", "cleared"): "resolved",
    ("reopened", "cleared"): "resolved",
    ("acknowledged", "cleared"): "resolved",
    ("new", "ack"): "acknowledged",
    ("reopened", "ack"): "acknowledged",
    ("acknowledged", "evidence"): OPEN,  # new evidence lapses an acknowledgement
}


@dataclass(frozen=True)
class Condition:
    """A signal as the workflow sees it: what it is, whose it is, and the data it is derived from."""

    key: str
    source: Source
    type: str
    period: str
    scope: str  # "AU actual" on the close board; the accrual or invoice ID in close controls
    owner: str
    lead: str  # the close lead for this source
    default_due: date
    fingerprint: str
    trigger: str

    def data(self) -> dict[str, str]:
        return {k: str(v) for k, v in self.__dict__.items() if k != "key"}

    @staticmethod
    def from_data(key: str, d: Mapping[str, str]) -> Condition:
        return Condition(key, d["source"], d["type"], d["period"], d["scope"], d["owner"], d["lead"],
                         date.fromisoformat(d["default_due"]), d["fingerprint"], d["trigger"])


@dataclass(frozen=True)
class Event:
    kind: EventKind
    key: str
    at: datetime  # effective time; the store adds the real clock in signed-in mode
    by: Actor | None  # who acted; for an observation, whose data change it was (None when nobody was named)
    data: Mapping[str, str]
    seq: int = 0  # assigned by the store; 0 for an observation a read made in memory and nobody recorded
    logged_at: datetime | None = None

    @property
    def recorded(self) -> bool:
        return self.seq > 0


@dataclass(frozen=True)
class HistoryEntry:
    occurrence: int
    when: str
    who: str
    what: str


@dataclass(frozen=True)
class Item:
    condition: Condition  # the latest seen; for a resolved item, as it was before it cleared
    state: State
    occurrence: int
    assignee: str | None  # a username in signed-in mode, a role in open mode
    assignee_label: str | None
    assignee_eligible: bool  # False once the assignee is disabled or no longer holds a suitable role
    due: date
    overdue: bool
    acknowledged_by: str | None  # label of who acknowledged the current evidence
    new_evidence: bool  # the evidence changed during this occurrence
    cleared_at: datetime | None
    last_actor: str | None
    history: tuple[HistoryEntry, ...]

    @property
    def key(self) -> str:
        return self.condition.key

    @property
    def is_open(self) -> bool:
        return self.state != "resolved"

    def summary(self) -> str:
        """One line: state, assignee, due date, overdue in words."""
        state = self.state
        if state == "acknowledged" and self.acknowledged_by:
            state += f" by {self.acknowledged_by}"
        if state == "resolved" and self.cleared_at:
            state += f" {self.cleared_at:%Y-%m-%d %H:%M}" + (f" (last action: {self.last_actor})"
                                                             if self.last_actor else "")
        if self.new_evidence and self.is_open:
            state += ", new evidence"
        due = f"due {self.due.isoformat()}" + (" (overdue)" if self.overdue else "")
        return f"{state}; assignee {self.assignee_label or 'none'}; {due}"


@dataclass(frozen=True)
class ActionResult:
    outcome: Outcome
    message: str


# Conditions from both sources


def _fingerprint(*parts: object) -> str:
    return hashlib.sha256(json.dumps([str(p) for p in parts]).encode("utf-8")).hexdigest()[:16]


def board_key(period: str, type_: str, market: str, scenario: str) -> str:
    return f"board:{period}:{type_}:{market}:{scenario}"


def controls_key(type_: str, item: str) -> str:
    return f"controls:{type_}:{item}"


def _board_conditions(b: attention.Board, ws: Workspace) -> list[Condition]:
    out = []
    for s in b.signals:
        slot = b.slot(s.market, s.scenario).slot
        sub, v = slot.submission, slot.validation
        evidence = {
            "MISSING_SUBMISSION": ("no file",),
            "VALIDATION_FAILED": (sub and sub.submission_id, v and v.mapping_version, v and v.state,
                                  *(f"{e.code} {e.row} {e.account}" for e in s.exceptions)),
            "MATERIAL_CHANGE": (sub and sub.submission_id, v and v.mapping_version, s.release_id),
        }[s.type]
        out.append(Condition(board_key(b.period, s.type, s.market, s.scenario), "board", s.type, b.period,
                             f"{s.market} {s.scenario}", s.owner, ws.config.regional_owner, s.due,
                             _fingerprint(s.type, *evidence), s.trigger.removesuffix(" and it is overdue")))
    return out


def working_days_after(day: date, n: int) -> date:
    """The n-th working day after `day`; with n = 0, `day` itself, or the next working day on a weekend."""
    while n > 0:
        day += timedelta(days=1)
        n -= day.weekday() < 5
    while day.weekday() >= 5:
        day += timedelta(days=1)
    return day


def period_end(period: str) -> date:
    return attention.due_date(period, 1) - timedelta(days=1)


def _controls_period(c: controls.Controls, item: str) -> str:
    try:
        return c.accrual(item).accrual.period
    except controls.ControlsError:
        return c.invoice(item).period


def _controls_conditions(c: controls.Controls) -> list[Condition]:
    out = []
    for s in c.signals:
        period = _controls_period(c, s.item)
        out.append(Condition(controls_key(s.type, s.item), "controls", s.type, period, s.item, s.owner,
                             c.cfg.close_lead, working_days_after(period_end(period), c.cfg.due_days),
                             _fingerprint(s.type, s.amount_at_risk, s.trigger, *s.evidence), s.trigger))
    return out


# Folding events into items


@dataclass
class _Acc:
    condition: Condition
    state: State | None = None
    occurrence: int = 0
    assignee: str | None = None
    due: date | None = None
    acknowledged_by: str | None = None
    new_evidence: bool = False
    cleared_at: datetime | None = None
    last_actor: str | None = None
    history: list[HistoryEntry] | None = None


def _step(acc: _Acc, kind: EventKind) -> None:
    after = STEPS.get((acc.state, kind))
    if after == OPEN:
        after = "new" if acc.occurrence == 1 else "reopened"
    if after is not None:
        acc.state = after


def _what(e: Event) -> str:
    d = e.data
    return {
        "appeared": lambda: f"appeared: {d.get('trigger', '')}",
        "evidence": lambda: f"new evidence: {d.get('trigger', '')}",
        "cleared": lambda: "cleared: the condition is no longer in the data",
        "ack": lambda: "acknowledged" + (f": {d['note']}" if d.get("note") else ""),
        "assign": lambda: f"assigned to {d['to']}",
        "due": lambda: f"due date set to {d['date']}",
        "comment": lambda: f"comment: {d['note']}",
    }[e.kind]()


def _repeats(acc: _Acc, e: Event) -> bool:
    """An observation that changes nothing, as when two writers observed the same change."""
    open_ = acc.state not in (None, "resolved")
    return ((e.kind == "appeared" and open_) or (e.kind == "cleared" and not open_)
            or (e.kind == "evidence" and (not open_ or acc.condition.fingerprint == e.data.get("fingerprint"))))


def _who(e: Event) -> str:
    if e.kind in ACTIONS:
        return e.by.label if e.by else ""
    if not e.recorded:
        return "(the data, not recorded yet)"
    return f"(the data, from {e.by.label})" if e.by else "(the data)"


def _apply(acc: _Acc, e: Event) -> None:
    if _repeats(acc, e):
        return
    if e.kind == "appeared":
        acc.occurrence += 1
        acc.acknowledged_by, acc.new_evidence, acc.cleared_at = None, False, None
        acc.condition = Condition.from_data(e.key, e.data)
    if e.kind == "evidence":  # keeps the due date the occurrence was born with
        acc.condition = replace(Condition.from_data(e.key, e.data), default_due=acc.condition.default_due)
        acc.new_evidence = True
        acc.acknowledged_by = None
    if e.kind == "cleared":
        acc.cleared_at = e.at if e.recorded else None
    before = acc.state
    _step(acc, e.kind)
    if e.kind == "ack" and before != acc.state:
        acc.acknowledged_by = e.by.label if e.by else None
    if e.kind == "assign":
        acc.assignee = e.data["to"]
    if e.kind == "due":
        acc.due = date.fromisoformat(e.data["date"])
    if e.by is not None and e.kind in ACTIONS:
        acc.last_actor = e.by.label
    acc.history.append(HistoryEntry(acc.occurrence, identity.when_text(e.at, e.logged_at), _who(e), _what(e)))


def fold(events: tuple[Event, ...]) -> dict[str, _Acc]:
    accs: dict[str, _Acc] = {}
    for e in events:
        if e.key not in accs:
            if e.kind != "appeared":
                continue
            accs[e.key] = _Acc(Condition.from_data(e.key, e.data), history=[])
        _apply(accs[e.key], e)
    return accs


def _born(c: Condition, at: datetime, followup_days: int) -> Condition:
    """A close-board item first seen after its calendar due date is due `followup_days` working days after it
    appeared, so it is not overdue at birth. Close-controls items keep their period-end rule."""
    if c.source == "board" and at.date() > c.default_due:
        return replace(c, default_due=working_days_after(at.date(), followup_days))
    return c


def observations(accs: Mapping[str, _Acc], current: list[Condition], scope: Callable[[Condition], bool],
                 at: datetime, by: Actor | None = None, followup_days: int = 0) -> tuple[Event, ...]:
    """What changed since the last observation: items that appeared (or came back), whose evidence changed, and
    open items in scope that are no longer in the data. Nothing when nothing changed."""
    out = []
    now = {c.key: c for c in current}
    for c in current:
        acc = accs.get(c.key)
        if acc is None or acc.state == "resolved":
            out.append(Event("appeared", c.key, at, by, _born(c, at, followup_days).data()))
        elif acc.condition.fingerprint != c.fingerprint:
            out.append(Event("evidence", c.key, at, by, c.data()))
    for key, acc in accs.items():
        if acc.state != "resolved" and key not in now and scope(acc.condition):
            out.append(Event("cleared", key, at, by, {}))
    return tuple(out)


def _suitable(store: Store, c: Condition) -> tuple[str, ...]:
    """Who an item can be assigned to: users holding its owner or lead role, or in open mode those roles."""
    if identity.signed_in_mode(store):
        return tuple(u.username for u in store.users() if u.active and {c.owner, c.lead} & set(u.roles))
    return tuple(dict.fromkeys((c.owner, c.lead)))


def _label(store: Store, assignee: str | None, eligible: bool) -> str | None:
    if assignee is None:
        return None
    user = store.user(assignee) if identity.signed_in_mode(store) else None
    label = f"{user.display_name} ({assignee})" if user else assignee
    return label if eligible else f"{label}: assignee no longer eligible"


def _item(store: Store, acc: _Acc, as_of: datetime) -> Item:
    due = acc.due or acc.condition.default_due
    eligible = acc.assignee is not None and acc.assignee in _suitable(store, acc.condition)
    return Item(acc.condition, acc.state, acc.occurrence, acc.assignee, _label(store, acc.assignee, eligible),
                eligible, due, acc.state != "resolved" and as_of.date() > due, acc.acknowledged_by,
                acc.new_evidence, acc.cleared_at, acc.last_actor, tuple(acc.history))


# Recording observations where the data changes; loading without writing


@dataclass(frozen=True)
class Items:
    as_of: datetime
    items: tuple[Item, ...]  # open first (overdue, then by due date), then resolved

    def get(self, key: str) -> Item:
        for i in self.items:
            if i.key == key:
                return i
        raise WorkflowError(f"No needs-attention item {key!r}; see fct attention list.")

    def by_key(self) -> dict[str, Item]:
        return {i.key: i for i in self.items}


class WorkflowError(ValueError):
    pass


def _since(accs: Mapping[str, _Acc], store: Store, ws: Workspace, at: datetime, data_as_of: datetime,
           period: str | None, by: Actor | None) -> tuple[Event, ...]:
    """The observations, stamped `at`, that bring `accs` up to the data as of `data_as_of`."""
    periods = set(store.submission_periods()) | {period} - {None} | {
        a.condition.period for a in accs.values() if a.condition.source == "board" and a.state != "resolved"}
    current = []
    for p in sorted(periods):
        current += _board_conditions(attention.board(store, ws, p, at), ws)
    has_controls = ws.config.controls is not None
    if has_controls:
        current += _controls_conditions(controls.load(store, ws, data_as_of))
    return observations(accs, current, lambda c: c.period in periods if c.source == "board" else has_controls,
                        at, by, ws.config.followup_days)


def observe(store: Store, ws: Workspace, at: datetime, by: Actor | str | None = None,
            period: str | None = None) -> tuple[Event, ...]:
    """Record what the data, as it now stands, changes in the recorded items, stamped `at` (the change's
    effective time) and `by` (who made it). Idempotent: the same data records nothing. Returns what it recorded.
    Call it inside the data change's transaction (`recording`)."""
    by = identity.actor(by)
    events = _since(fold(store.attention_events()), store, ws, at, datetime.max, period, by if by.role else None)
    store.add_attention_events(events)
    return events


@contextmanager
def recording(store: Store, ws: Workspace, at: datetime, by: Actor | str | None = None,
              period: str | None = None) -> Iterator[None]:
    """Wrap an operation that changes the data: what it changes in the needs-attention items is recorded in the
    same transaction, stamped with its effective time and who acted. Nothing is recorded when it raises."""
    with store.atomic():
        yield
        observe(store, ws, at, by, period)


def load(store: Store, ws: Workspace, as_of: datetime | None = None, period: str | None = None) -> Items:
    """The items as of `as_of`: the events recorded up to then, plus, in memory only, what the data shows then
    that nobody has recorded. Writes nothing."""
    as_of = as_of or store.clock()
    events = tuple(e for e in store.attention_events() if e.at <= as_of)
    unrecorded = _since(fold(events), store, ws, as_of, as_of, period, None)
    items = [_item(store, a, as_of) for a in fold(events + unrecorded).values()]
    items.sort(key=lambda i: (not i.is_open, not i.overdue, i.due, i.key))
    return Items(as_of, tuple(items))


def refresh(store: Store, ws: Workspace, by: Actor | str | None, period: str | None = None) -> ActionResult:
    """Record what the data shows now, at the real time: for config-only changes (mapping, FX, owners, due days)
    that no data-changing operation has recorded yet. Signed in, it needs a sign-in."""
    try:
        by = identity.require_signed_in(store, by)
    except identity.IdentityError as e:
        return _refused(str(e))
    with store.atomic():
        events = observe(store, ws, store.clock(), by, period)
    if not events:
        return ActionResult("no_op", "The recorded items already match the data. Nothing recorded.")
    n = len(events)
    return ActionResult("recorded", f"Recorded {n} observation{'s' * (n != 1)}: "
                                    + "; ".join(f"{e.key} {e.kind}" for e in events) + ".")


def open_items(store: Store, ws: Workspace, period: str, as_of: datetime | None = None) -> tuple[Item, ...]:
    """The period's items that are open now: what the release manifest records at approval."""
    return tuple(i for i in load(store, ws, as_of, period).items if i.is_open and i.condition.period == period)


# Who may do what


def me(by: Actor) -> str:
    """How an assignee names this actor: the username when signed in, the role in open mode."""
    return by.person.username if by.person else by.role


def _lead_text(i: Item) -> str:
    return f"the close lead ({i.condition.lead})"


# action -> (may this actor take it on this item?, who may, in words)
PERMISSIONS: Mapping[ActionKind, tuple[Callable[[Item, Actor], bool], Callable[[Item], str]]] = {
    "ack": (lambda i, by: by.role in (i.condition.owner, i.condition.lead),
            lambda i: f"the owner ({i.condition.owner}) or {_lead_text(i)}"),
    "assign": (lambda i, by: by.role == i.condition.lead or (i.assignee_eligible and i.assignee == me(by)),
               lambda i: f"{_lead_text(i)} or the current assignee"),
    "due": (lambda i, by: by.role == i.condition.lead, _lead_text),
    "comment": (lambda i, by: True, lambda i: "anyone"),
}
ON_RESOLVED: frozenset[ActionKind] = frozenset({"comment"})


def refusal(i: Item, by: Actor, kind: ActionKind) -> str | None:
    if not by.role:
        return "Say who is acting (--by ROLE)."
    if i.state == "resolved" and kind not in ON_RESOLVED:
        return f"{i.key} is resolved: only the data opens it again."
    may, who = PERMISSIONS[kind]
    if not may(i, by):
        return f"{by.label} may not {_VERB[kind]} {i.key}; allowed: {who(i)}."
    return None


_VERB: Mapping[ActionKind, str] = {"ack": "acknowledge", "assign": "assign", "due": "set the due date of",
                                   "comment": "comment on"}


def permitted(i: Item, by: Actor) -> tuple[ActionKind, ...]:
    return tuple(k for k in ACTIONS if refusal(i, by, k) is None)


def suitable_assignees(store: Store, ws: Workspace, i: Item) -> tuple[str, ...]:
    return _suitable(store, i.condition)


# Actions


def _refused(message: str) -> ActionResult:
    return ActionResult("refused", message + " Nothing recorded.")


def act(store: Store, ws: Workspace, key: str, kind: ActionKind, by: Actor | str | None,
        as_of: datetime | None = None, note: str = "", to: str = "", due: date | None = None) -> ActionResult:
    """Acknowledge, assign, set the due date or comment. Refused with a readable message when not allowed;
    a repeat of what is already in place records nothing. Nothing here resolves an item: only the data can."""
    try:
        by = identity.require_signed_in(store, by)
    except identity.IdentityError as e:
        return _refused(str(e))
    at = as_of or store.clock()
    try:
        i = load(store, ws, at).get(key)
    except WorkflowError as e:
        return _refused(str(e))
    if why := refusal(i, by, kind):
        return _refused(why)
    note = note.strip()
    if kind == "ack":
        if i.state == "acknowledged":
            return ActionResult("no_op", f"{key} is already acknowledged by {i.acknowledged_by}. Nothing recorded.")
        data = {"note": note} if note else {}
    elif kind == "assign":
        to = to.strip()
        allowed = suitable_assignees(store, ws, i)
        if to not in allowed:
            return _refused(f"{to!r} cannot take {key}: assign it to someone holding {i.condition.owner} or "
                            f"{i.condition.lead} ({', '.join(allowed) or 'nobody holds them'}).")
        if i.assignee == to:
            return ActionResult("no_op", f"{key} is already assigned to {i.assignee_label}. Nothing recorded.")
        data = {"to": to}
    elif kind == "due":
        if due is None:
            return _refused("Give the due date (--date YYYY-MM-DD).")
        if i.due == due:
            return ActionResult("no_op", f"{key} is already due {due.isoformat()}. Nothing recorded.")
        data = {"date": due.isoformat()}
    else:
        if not note:
            return _refused("Give the comment (--note).")
        last = next((h for h in reversed(i.history) if h.what.startswith("comment: ")), None)
        if last is not None and last.what == f"comment: {note}" and last.who == by.label:
            return ActionResult("no_op", "The same comment is already the latest. Nothing recorded.")
        data = {"note": note}
    with store.atomic():  # an item only a read has seen so far is recorded first, so the action lands on it
        observe(store, ws, at, by, i.condition.period if i.condition.source == "board" else None)
        store.add_attention_events((Event(kind, key, at, by, data),))
    after = load(store, ws, at).get(key)
    return ActionResult("recorded", f"{key}: {after.summary()}")


def mine(items: Items, by: Actor) -> tuple[Item, ...]:
    """What is assigned to me, overdue first. An assignee no longer eligible counts as unassigned."""
    return tuple(i for i in items.items if i.is_open and i.assignee_eligible and i.assignee == me(by))


def with_state(items: Items, state: State) -> tuple[Item, ...]:
    return tuple(i for i in items.items if i.state == state)
