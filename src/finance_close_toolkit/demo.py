"""The October 2026 close story (docs/fixture-expected.md E1-E16, B1-B2) and the close-controls story (C1-C9)
as library calls.

`scripts/demo.sh` tells the same story through the CLI; tests/test_demo.py proves both reach the same state.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Callable

from . import controls, release, validation
from .config import Workspace, load_workspace

if TYPE_CHECKING:
    from .store import Store

REPO = Path(__file__).resolve().parents[2]
EXAMPLES = REPO / "examples" / "2026-10"
PERIOD = "2026-10"
APPROVER = "Regional FP&A lead"


@dataclass(frozen=True)
class Context:
    store: Store
    workdir: Path  # holds config/ and fx/, which the story edits
    examples: Path
    now: datetime | None  # approval time; None means the wall clock

    @property
    def config_path(self) -> Path:
        return self.workdir / "config" / "close.toml"

    def ingest(self, name: str, market: str, scenario: str, received: str) -> None:
        ws = load_workspace(self.config_path)
        validation.ingest(self.store, ws, self.examples / name, market, PERIOD, scenario,
                          datetime.fromisoformat(received))

    def publish(self) -> None:
        release.publish(self.store, load_workspace(self.config_path), PERIOD, approved_by=APPROVER, now=self.now)

    def map_consulting_to_oth(self) -> None:
        with open(self.workdir / "config" / "mappings" / "au.toml", "a", encoding="utf-8", newline="\n") as f:
            f.write('"64000" = "OTH"\n')

    def revalidate(self) -> None:
        validation.revalidate(self.store, load_workspace(self.config_path), PERIOD)


def _e1(c: Context) -> None:
    c.ingest("sg_actual_v1.csv", "SG", "actual", "2026-11-04T10:00")
    c.ingest("sg_budget.csv", "SG", "budget", "2026-11-01T09:00")
    c.ingest("au_budget.xlsx", "AU", "budget", "2026-11-01T09:00")


def _e10(c: Context) -> None:
    c.map_consulting_to_oth()
    c.revalidate()


@dataclass(frozen=True)
class Step:
    id: str
    title: str
    as_of: datetime  # when the story looks at the close board after this step
    action: Callable[[Context], None] | None  # None for a step that only looks


def _step(id_: str, title: str, as_of: str, action: Callable[[Context], None] | None = None) -> Step:
    return Step(id_, title, datetime.fromisoformat(as_of), action)


STEPS: tuple[Step, ...] = (
    _step("E1", "SG actual v1, SG budget and AU budget received", "2026-11-04T10:00", _e1),
    _step("E2", "The day after actuals were due", "2026-11-06T00:00"),
    _step("E3", "Publish refused: AU actual missing", "2026-11-06T00:00"),
    _step("E4", "AU actual v1 received late", "2026-11-07T15:00",
          lambda c: c.ingest("au_actual_v1.xlsx", "AU", "actual", "2026-11-07T15:00")),
    _step("E5", "The same AU file sent again", "2026-11-07T16:00",
          lambda c: c.ingest("au_actual_v1.xlsx", "AU", "actual", "2026-11-07T15:00")),
    _step("E6", "R1 approved and published", "2026-11-08T00:00", Context.publish),
    _step("E7", "AU resubmits with a new account", "2026-11-12T11:00",
          lambda c: c.ingest("au_actual_v2.xlsx", "AU", "actual", "2026-11-12T11:00")),
    _step("E8", "AU actual needs review", "2026-11-12T12:00"),
    _step("E9", "Publish refused while AU actual needs review", "2026-11-12T12:00"),
    _step("E10", "64000 Consulting fees mapped to OTH, revalidated", "2026-11-12T13:00", _e10),
    _step("E11", "AU change is material and waits for approval", "2026-11-12T13:00"),
    _step("E12", "Publish dry run shows the delta", "2026-11-12T13:00"),
    _step("E13", "R2 approved and published", "2026-11-12T13:00", Context.publish),
    _step("E14", "R1 is still reproducible", "2026-11-12T13:00"),
    _step("E15", "Diff R1 to R2", "2026-11-12T13:00"),
    _step("E16", "Trace AU revenue in R1", "2026-11-12T13:00"),
    _step("B1", "SG resubmits with marketing 1,000 higher", "2026-11-14T09:00",
          lambda c: c.ingest("sg_actual_v2.csv", "SG", "actual", "2026-11-14T09:00")),
    _step("B2", "A change below threshold raises no signal", "2026-11-14T10:00"),
)
STEP_IDS = tuple(s.id for s in STEPS)


def step(step_id: str) -> Step:
    for s in STEPS:
        if s.id == step_id:
            return s
    raise ValueError(f"unknown demo step {step_id!r}; steps: {', '.join(STEP_IDS)}")


def prepare(workdir: Path) -> Path:
    """Copy the repository's config/ and fx/ into an empty `workdir`; returns its close.toml path."""
    workdir = Path(workdir)
    shutil.copytree(REPO / "config", workdir / "config")
    shutil.copytree(REPO / "fx", workdir / "fx")
    return workdir / "config" / "close.toml"


def replay(
    store: Store, workdir: Path, until: str, examples: Path = EXAMPLES, now: datetime | None = None
) -> Step:
    """Apply every state-changing step up to and including `until` to a fresh store and a
    `workdir` prepared with `prepare`. Returns the step reached."""
    last = step(until)
    context = Context(store, Path(workdir), Path(examples), now)
    for s in STEPS[: STEPS.index(last) + 1]:
        if s.action is not None:
            s.action(context)
    return last


# The close-controls story (docs/fixture-expected.md, Step 4: C1-C9)

CONTROLS_EXAMPLES = REPO / "examples" / "controls"
AP, ANALYST, LEAD = "AP team", "SG finance analyst", "Regional close lead"


@dataclass(frozen=True)
class ControlStep:
    id: str
    title: str
    at: datetime  # when the action is recorded; the story looks at status at this time
    action: Callable[[Store, Workspace, datetime], object]


def _import(**names: str) -> Callable[[Store, Workspace, datetime], object]:
    def run(store: Store, ws: Workspace, at: datetime) -> object:
        files = {kind: (name, (CONTROLS_EXAMPLES / name).read_bytes()) for kind, name in names.items()}
        return controls.import_files(store, ws, files, at)

    return run


def _cstep(id_: str, title: str, at: str, action: Callable[[Store, Workspace, datetime], object]) -> ControlStep:
    return ControlStep(id_, title, datetime.fromisoformat(at), action)


_RESOLVE_A003 = dict(kind="re_accrue", amount=Decimal(35000), reason="Phase 2 invoice due May", by=ANALYST)

CONTROL_STEPS: tuple[ControlStep, ...] = (
    _cstep("C1", "Accruals, invoices and the April GL imported", "2026-05-04T09:00",
           _import(accruals="accruals_2026-03.csv", invoices="invoices_2026-04.csv", gl="gl_2026-04.csv")),
    _cstep("C2", "AP links INV-5504 to A-004", "2026-05-04T10:00",
           lambda s, ws, at: controls.link(s, ws, "INV-5504", "A-004", AP, now=at)),
    _cstep("C3", "The same link again: a no-op", "2026-05-04T10:05",
           lambda s, ws, at: controls.link(s, ws, "INV-5504", "A-004", AP, now=at)),
    _cstep("C4", "SG finance analyst tries to link: refused", "2026-05-04T10:10",
           lambda s, ws, at: controls.link(s, ws, "INV-5504", "A-005", ANALYST, now=at)),
    _cstep("C5", "A-001 variance explained", "2026-05-04T11:00",
           lambda s, ws, at: controls.explain(s, ws, "A-001", "Scope extension approved by SG GM", ANALYST, now=at)),
    _cstep("C6", "A-006 assigned to SG finance analyst", "2026-05-04T12:00",
           lambda s, ws, at: controls.assign(s, ws, "A-006", ANALYST, LEAD, now=at)),
    _cstep("C7", "Second April GL with the late A-002 reversal", "2026-05-05T09:00",
           _import(gl="gl_2026-04_v2.csv")),
    _cstep("C8", "A-003 resolution approved by its own preparer: refused", "2026-05-05T10:00",
           lambda s, ws, at: controls.resolve(s, ws, "A-003", approved_by=ANALYST, now=at, **_RESOLVE_A003)),
    _cstep("C9", "A-003 resolution approved by the close lead", "2026-05-05T10:05",
           lambda s, ws, at: controls.resolve(s, ws, "A-003", approved_by=LEAD, now=at, **_RESOLVE_A003)),
)
CONTROL_STEP_IDS = tuple(s.id for s in CONTROL_STEPS)


def control_step(step_id: str) -> ControlStep:
    for s in CONTROL_STEPS:
        if s.id == step_id:
            return s
    raise ValueError(f"unknown controls demo step {step_id!r}; steps: {', '.join(CONTROL_STEP_IDS)}")


def replay_controls(store: Store, ws: Workspace, until: str) -> ControlStep:
    """Apply every controls step up to and including `until` to a fresh store. Returns the step reached."""
    last = control_step(until)
    for s in CONTROL_STEPS[: CONTROL_STEPS.index(last) + 1]:
        s.action(store, ws, s.at)
    return last
