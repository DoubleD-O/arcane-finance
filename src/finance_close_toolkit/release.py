from __future__ import annotations

import csv
import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Literal

from .config import ChartOfAccounts, FxRates, Market, Workspace, parse_chart_of_accounts, parse_mapping, version_of
from .intake import Submission
from .kpi import Diff, balances, diff, totals
from .money import canonical, plain
from .validation import MappedFact, Validation, check_period, map_rows

if TYPE_CHECKING:
    from .store import Store


class ReleaseError(ValueError):
    pass


@dataclass(frozen=True)
class ReleaseFact:
    market: str
    scenario: str
    account: str
    local_amount: Decimal
    currency: str
    rate: Decimal
    reporting_amount: Decimal
    submission_id: str
    file_name: str
    source_row: int

    def canonical_row(self) -> str:
        return "|".join(
            (
                self.market,
                self.scenario,
                self.account,
                canonical(self.local_amount),
                self.currency,
                canonical(self.rate),
                canonical(self.reporting_amount),
                self.submission_id,
                self.file_name,
                str(self.source_row),
            )
        )


def facts_checksum(facts: tuple[ReleaseFact, ...]) -> str:
    rows = sorted(f.canonical_row() for f in facts)
    return hashlib.sha256("\n".join(rows).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class SlotManifest:
    market: str
    scenario: str
    submission_id: str
    file_name: str
    received_at: datetime
    mapping_version: str


@dataclass(frozen=True)
class Release:
    release_id: str
    period: str
    previous_release_id: str | None
    approved_by: str
    approved_at: datetime
    reporting_currency: str
    slots: tuple[SlotManifest, ...]
    rates: dict[str, dict[str, Decimal]]
    coa_version: str
    kpi_totals: dict[str, dict[str, dict[str, Decimal]]]
    facts_checksum: str

    @property
    def seq(self) -> int:
        return int(self.release_id.rsplit("-R", 1)[1])

    def manifest(self) -> dict:
        return {
            "release_id": self.release_id,
            "period": self.period,
            "previous_release_id": self.previous_release_id,
            "approved_by": self.approved_by,
            "approved_at": self.approved_at.isoformat(),
            "reporting_currency": self.reporting_currency,
            "slots": [
                {
                    "market": s.market,
                    "scenario": s.scenario,
                    "submission_id": s.submission_id,
                    "file_name": s.file_name,
                    "received_at": s.received_at.isoformat(),
                    "mapping_version": s.mapping_version,
                }
                for s in self.slots
            ],
            "rates": {sc: {c: str(r) for c, r in t.items()} for sc, t in self.rates.items()},
            "coa_version": self.coa_version,
            "kpi_totals": {
                sc: {scope: {k: plain(v) for k, v in kp.items()} for scope, kp in by.items()}
                for sc, by in self.kpi_totals.items()
            },
            "facts_checksum": self.facts_checksum,
        }

    @staticmethod
    def from_manifest(m: dict) -> Release:
        return Release(
            release_id=m["release_id"],
            period=m["period"],
            previous_release_id=m["previous_release_id"],
            approved_by=m["approved_by"],
            approved_at=datetime.fromisoformat(m["approved_at"]),
            reporting_currency=m["reporting_currency"],
            slots=tuple(
                SlotManifest(
                    s["market"],
                    s["scenario"],
                    s["submission_id"],
                    s["file_name"],
                    datetime.fromisoformat(s["received_at"]),
                    s["mapping_version"],
                )
                for s in m["slots"]
            ),
            rates={sc: {c: Decimal(r) for c, r in t.items()} for sc, t in m["rates"].items()},
            coa_version=m["coa_version"],
            kpi_totals={
                sc: {scope: {k: Decimal(v) for k, v in kp.items()} for scope, kp in by.items()}
                for sc, by in m["kpi_totals"].items()
            },
            facts_checksum=m["facts_checksum"],
        )

    def slot(self, market: str, scenario: str) -> SlotManifest | None:
        return next((s for s in self.slots if (s.market, s.scenario) == (market, scenario)), None)


def translate(
    facts: tuple[MappedFact, ...], submission: Submission, currency: str, rate: Decimal
) -> tuple[ReleaseFact, ...]:
    return tuple(
        ReleaseFact(
            submission.market,
            submission.scenario,
            f.account,
            f.amount,
            currency,
            rate,
            f.amount * rate,
            submission.submission_id,
            submission.file_name,
            f.source_row,
        )
        for f in facts
    )


@dataclass(frozen=True)
class SlotState:
    """One expected (market, scenario) slot: what was submitted and what would be published."""

    market: Market
    scenario: str
    submission: Submission | None
    validation: Validation | None
    stale: bool  # the validation shown predates the current mapping version
    rate: Decimal | None
    proposed: tuple[ReleaseFact, ...] | None  # set only when publishable
    released: tuple[ReleaseFact, ...]

    @property
    def changes_release(self) -> bool:
        """Publishable, and its facts differ from what the current release holds for this slot."""
        return self.proposed is not None and (
            sorted(f.canonical_row() for f in self.proposed) != sorted(f.canonical_row() for f in self.released)
        )

    def blocker(self, fx_name: str) -> str | None:
        where = f"{self.market.code} {self.scenario}"
        if self.submission is None:
            return f"{where}: no submission received"
        assert self.validation is not None
        if self.stale:
            return f"{where}: {self.submission.file_name} was validated under an older mapping; run revalidate"
        if self.validation.state != "validated":
            n = len(self.validation.exceptions)
            return f"{where}: {self.submission.file_name} is {self.validation.state} ({n} exception{'s' * (n != 1)})"
        if self.rate is None:
            return f"{where}: no {self.scenario} rate for {self.market.currency} in {fx_name}"
        return None


@dataclass(frozen=True)
class Proposal:
    period: str
    current: Release | None
    current_facts: tuple[ReleaseFact, ...]
    slots: tuple[SlotState, ...]
    blockers: tuple[str, ...]
    rates: dict[str, dict[str, Decimal]]

    @property
    def facts(self) -> tuple[ReleaseFact, ...] | None:
        if self.blockers:
            return None
        return tuple(f for s in self.slots for f in (s.proposed or ()))

    def preview_facts(self) -> tuple[ReleaseFact, ...]:
        """Current release with every publishable slot swapped in, for impact previews."""
        return tuple(f for s in self.slots for f in (s.proposed if s.proposed is not None else s.released))

    def diff(self, ws: Workspace) -> Diff:
        return diff(
            balances(self.current_facts),
            balances(self.preview_facts()),
            ws.coa,
            ws.config.materiality,
            [m.code for m in ws.config.markets],
        )


def propose(store: Store, ws: Workspace, period: str) -> Proposal:
    check_period(period)
    fx_name = ws.fx_path(period).name
    fx = ws.fx(period)
    if fx is None:
        fx, fx_name = FxRates({}), f"{fx_name} (file not found)"
    current = store.current_release(period)
    current_facts = store.release_facts(current.release_id) if current else ()
    slots, blockers = [], []
    for market in ws.config.markets:
        mapping = ws.mapping(market.code)
        for scenario in ws.config.scenarios:
            released = tuple(f for f in current_facts if (f.market, f.scenario) == (market.code, scenario))
            rate = fx.rate(scenario, market.currency)
            submission = store.latest_submission(market.code, period, scenario)
            validation, stale = None, False
            if submission is not None:
                validation = store.validation(submission.submission_id, mapping.version)
                if validation is None:
                    validation, stale = store.latest_validation(submission.submission_id), True
            proposed = None
            if validation is not None and not stale and validation.state == "validated" and rate is not None:
                proposed = translate(validation.facts, submission, market.currency, rate)
            slot = SlotState(market, scenario, submission, validation, stale, rate, proposed, released)
            slots.append(slot)
            if blocker := slot.blocker(fx_name):
                blockers.append(blocker)
    # Every rate in the file, not only the markets' currencies, so the release can be shown in any of them.
    rates = {scenario: dict(fx.rates.get(scenario, {})) for scenario in ws.config.scenarios}
    return Proposal(period, current, current_facts, tuple(slots), tuple(blockers), rates)


PROPOSAL_CHANGED = "The proposal changed since you reviewed it; review it again."


@dataclass(frozen=True)
class PublishResult:
    outcome: Literal["refused", "dry_run", "published", "unchanged"]
    release_id: str | None  # new, would-be or existing ID; None when refused
    proposal: Proposal
    diff: Diff | None
    facts_checksum: str | None = None  # of the proposed facts; None when the proposal is blocked
    reasons: tuple[str, ...] = ()  # why publish was refused

    @property
    def ok(self) -> bool:
        return self.outcome != "refused"


def publish(
    store: Store,
    ws: Workspace,
    period: str,
    approved_by: str | None = None,
    now: datetime | None = None,
    fault: Callable[[], None] | None = None,
    expected_checksum: str | None = None,
) -> PublishResult:
    """Dry run unless approved_by is given. With `expected_checksum` (the facts checksum of the
    proposal the approver reviewed), anything else is refused and nothing is written.
    `fault` runs inside the transaction after the facts are written and before the pointer
    moves, so tests can prove the rollback."""
    proposal = propose(store, ws, period)
    facts = proposal.facts
    if facts is None:
        return PublishResult("refused", None, proposal, None, reasons=proposal.blockers)
    checksum = facts_checksum(facts)
    if expected_checksum is not None and expected_checksum != checksum:
        return PublishResult("refused", None, proposal, None, checksum, (PROPOSAL_CHANGED,))
    current = proposal.current
    if current is not None and current.facts_checksum == checksum:
        return PublishResult("unchanged", current.release_id, proposal, proposal.diff(ws), checksum)
    release_id = f"{period}-R{store.next_release_seq(period)}"
    change = proposal.diff(ws)
    if approved_by is None:
        return PublishResult("dry_run", release_id, proposal, change, checksum)

    release = Release(
        release_id=release_id,
        period=period,
        previous_release_id=current.release_id if current else None,
        approved_by=approved_by,
        approved_at=now or datetime.now().replace(microsecond=0),
        reporting_currency=ws.config.reporting_currency,
        slots=tuple(
            SlotManifest(
                s.market.code,
                s.scenario,
                s.submission.submission_id,
                s.submission.file_name,
                s.submission.received_at,
                s.validation.mapping_version,
            )
            for s in proposal.slots
        ),
        rates=proposal.rates,
        coa_version=ws.coa.version,
        kpi_totals=totals(facts, ws.coa),
        facts_checksum=checksum,
    )
    store.publish_release(release, facts, ws.coa, fault=fault)
    return PublishResult("published", release_id, proposal, change, checksum)


def _require(store: Store, release_id: str) -> Release:
    release = store.release(release_id)
    if release is None:
        raise ReleaseError(f"no release {release_id!r}")
    return release


def diff_releases(store: Store, ws: Workspace, from_id: str, to_id: str) -> Diff:
    _require(store, from_id)
    _require(store, to_id)
    return diff(
        balances(store.release_facts(from_id)),
        balances(store.release_facts(to_id)),
        ws.coa,
        ws.config.materiality,
        [m.code for m in ws.config.markets],
    )


def trace(
    store: Store, release_id: str, market: str, account: str, scenario: str | None = None
) -> tuple[ReleaseFact, ...]:
    _require(store, release_id)
    return tuple(
        f
        for f in store.release_facts(release_id)
        if f.market == market and f.account == account and (scenario is None or f.scenario == scenario)
    )


@dataclass(frozen=True)
class VerifyResult:
    release_id: str
    manifest_checksum: str
    stored_checksum: str
    recomputed_checksum: str | None
    problems: tuple[str, ...]

    @property
    def ok(self) -> bool:
        return not self.problems


def stored_coa(store: Store, version: str) -> ChartOfAccounts | None:
    content = store.config_blob(version)
    return parse_chart_of_accounts(content, version) if content is not None else None


def verify_release(store: Store, release_id: str) -> VerifyResult:
    """Rebuild the release from stored raw rows, the mapping and chart content stored at
    publish time, and the release's own rates. Needs no config files."""
    release = _require(store, release_id)
    stored = store.release_facts(release_id)
    stored_checksum = facts_checksum(stored)
    problems = []
    if stored_checksum != release.facts_checksum:
        problems.append("stored facts do not match the manifest checksum")
    coa = stored_coa(store, release.coa_version)
    if coa is None:
        problems.append(f"chart of accounts {release.coa_version} is not stored")
    rebuilt: list[ReleaseFact] = []
    for slot in release.slots:
        submission = store.submission(slot.submission_id)
        content = store.config_blob(slot.mapping_version)
        if submission is None or content is None or coa is None:
            problems.append(f"{slot.market} {slot.scenario}: submission or mapping {slot.mapping_version} is not stored")
            continue
        if version_of(content.encode("utf-8")) != slot.mapping_version:
            problems.append(f"{slot.market} {slot.scenario}: stored mapping content does not hash to {slot.mapping_version}")
        mapping = parse_mapping(content, slot.mapping_version)
        currency = next((f.currency for f in stored if (f.market, f.scenario) == (slot.market, slot.scenario)), None)
        rate = release.rates.get(slot.scenario, {}).get(currency) if currency else None
        if rate is None:
            problems.append(f"{slot.market} {slot.scenario}: no stored rate")
            continue
        _, rows = store.raw_rows(slot.submission_id)
        rebuilt.extend(translate(map_rows(rows, mapping, coa), submission, currency, rate))
    recomputed = facts_checksum(tuple(rebuilt)) if not problems else None
    if recomputed is not None and recomputed != release.facts_checksum:
        problems.append("facts rebuilt from stored rows do not match the manifest checksum")
    return VerifyResult(release_id, release.facts_checksum, stored_checksum, recomputed, tuple(problems))


_FORMULA_START = ("=", "+", "-", "@")


def safe_text(value: str) -> str:
    if value.startswith(_FORMULA_START):
        try:
            Decimal(value)
            return value
        except ArithmeticError:
            return "'" + value
    return value


FACT_COLUMNS = (
    "release_id",
    "market",
    "scenario",
    "account",
    "local_amount",
    "currency",
    "rate",
    "reporting_amount",
    "submission_id",
    "file_name",
    "source_row",
)


def export_order(facts: tuple[ReleaseFact, ...]) -> list[ReleaseFact]:
    return sorted(facts, key=lambda x: (x.scenario, x.market, x.account, x.source_row))


def export(store: Store, release_id: str, out_dir: Path) -> tuple[Path, Path]:
    release = _require(store, release_id)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    facts_path = out_dir / "facts.csv"
    with open(facts_path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(FACT_COLUMNS)
        for fact in export_order(store.release_facts(release_id)):
            w.writerow(
                safe_text(v)
                for v in (
                    release_id,
                    fact.market,
                    fact.scenario,
                    fact.account,
                    plain(fact.local_amount),
                    fact.currency,
                    plain(fact.rate),
                    plain(fact.reporting_amount),
                    fact.submission_id,
                    fact.file_name,
                    str(fact.source_row),
                )
            )
    manifest_path = out_dir / "manifest.json"
    manifest_path.write_text(json.dumps(release.manifest(), indent=2), encoding="utf-8")
    return facts_path, manifest_path

