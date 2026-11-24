"""Actual vs budget for one release (or the proposed one): the single object every view renders."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import TYPE_CHECKING, Iterable, Literal, Mapping, Sequence

from .config import ChartOfAccounts, Market, Materiality, Workspace
from .kpi import APAC, EBIT, KPI, Balances, Diff, Kpi, balances, diff, kpi_value, scope_amounts
from .money import STORE_SCALE, ZERO, plain, rate_text, round_pp
from .release import ReleaseError, ReleaseFact, facts_checksum, propose, stored_coa

if TYPE_CHECKING:
    from .store import Store

Label = Literal["Favourable", "Unfavourable", "On plan", "n/m"]
LineKind = Literal["account", "subtotal", "ratio"]


@dataclass(frozen=True)
class AccountLine:
    key: str  # regional account code
    label: str
    kind: LineKind = "account"

    def value(self, amounts: Mapping[str, Decimal], coa: ChartOfAccounts) -> Decimal:
        return amounts.get(self.key, ZERO)


@dataclass(frozen=True)
class SubtotalLine:
    kpi: Kpi
    kind: LineKind = "subtotal"

    @property
    def key(self) -> str:
        return self.kpi.key

    @property
    def label(self) -> str:
        return self.kpi.label

    def value(self, amounts: Mapping[str, Decimal], coa: ChartOfAccounts) -> Decimal:
        return kpi_value(amounts, coa, self.kpi)


@dataclass(frozen=True)
class RatioLine:
    key: str
    label: str
    numerator: Kpi
    denominator: Kpi
    kind: LineKind = "ratio"

    def value(self, amounts: Mapping[str, Decimal], coa: ChartOfAccounts) -> Decimal | None:
        """Percent; None when the denominator is zero."""
        den = kpi_value(amounts, coa, self.denominator)
        return None if den == 0 else kpi_value(amounts, coa, self.numerator) / den * 100


PnlLine = AccountLine | SubtotalLine | RatioLine

PNL_LINES: tuple[PnlLine, ...] = (
    AccountLine("REV", "Revenue"),
    AccountLine("COGS", "Direct costs"),
    SubtotalLine(KPI["gross_margin"]),
    RatioLine("gm_pct", "GM %", KPI["gross_margin"], KPI["revenue"]),
    AccountLine("PAY", "Staff costs"),
    AccountLine("RENT", "Rent and facilities"),
    AccountLine("MKT", "Marketing"),
    AccountLine("OTH", "Other operating costs"),
    SubtotalLine(KPI["ebitda"]),
    RatioLine("ebitda_pct", "EBITDA %", KPI["ebitda"], KPI["revenue"]),
    AccountLine("DEP", "Depreciation"),
    SubtotalLine(EBIT),
)

TRACEABLE_LINES: tuple[AccountLine | SubtotalLine, ...] = tuple(l for l in PNL_LINES if l.kind != "ratio")
SCORECARD_LINES = ("REV", "gross_margin", "gm_pct", "ebitda", "ebitda_pct")
MARKET_SUMMARY_LINES = ("REV", "gross_margin", "ebitda")
# A bridge per KPI: the KPI's key -> the P&L line it bridges (budget, one step per market, actual).
BRIDGE_LINES = {"revenue": "REV", "gross_margin": "gross_margin", "ebitda": "ebitda"}
UNAVAILABLE_DRILL_DOWNS = ("client", "supplier", "cost-center")


@dataclass(frozen=True)
class VarianceRow:
    scope: str
    line: PnlLine
    actual: Decimal | None  # None only for a ratio over zero revenue
    budget: Decimal | None

    @property
    def variance(self) -> Decimal | None:
        """actual - budget; percentage points for a ratio line."""
        if self.actual is None or self.budget is None:
            return None
        return self.actual - self.budget

    @property
    def variance_pct(self) -> Decimal | None:
        """Variance as a % of |budget|. None for ratio lines and for a zero budget ("n/m")."""
        if self.line.kind == "ratio" or self.budget is None or self.budget == 0:
            return None
        return self.variance / abs(self.budget) * 100

    @property
    def label(self) -> Label:
        # Amounts are in profit sign, so a positive variance is favourable for revenue and costs alike.
        # Points are judged as shown (2 dp), so "0.00 pp" always reads "On plan".
        v = self.variance
        if v is None:
            return "n/m"
        if self.line.kind == "ratio":
            v = round_pp(v)
        return "Favourable" if v > 0 else "Unfavourable" if v < 0 else "On plan"


@dataclass(frozen=True)
class BridgeStep:
    market: str
    amount: Decimal
    before: Decimal  # running total before this step
    after: Decimal


@dataclass(frozen=True)
class Bridge:
    """A KPI's budget, one step per market (its variance on that KPI), the KPI's actual."""

    budget: Decimal
    steps: tuple[BridgeStep, ...]
    actual: Decimal

    @staticmethod
    def build(budget: Decimal, steps: Iterable[tuple[str, Decimal]], actual: Decimal, label: str = "EBITDA") -> Bridge:
        running, built = budget, []
        for market, amount in steps:
            built.append(BridgeStep(market, amount, running, running + amount))
            running += amount
        if running != actual:
            raise ValueError(f"{label} bridge does not close: budget {budget} + steps = {running}, actual {actual}")
        return Bridge(budget, tuple(built), actual)


def available_currencies(base: str, rates: Mapping[str, Mapping[str, Decimal]]) -> tuple[str, ...]:
    """The base currency, then every currency with a rate in each scenario, in rate-file order."""
    tables = list(rates.values())
    quoted = [c for c in (tables[0] if tables else ()) if all(c in t for t in tables)]
    return (base, *(c for c in quoted if c != base))


@dataclass(frozen=True)
class Conversion:
    """Base-currency amounts shown in another currency: each scenario divided by that scenario's rate."""

    currency: str
    base: str
    rates: Mapping[str, Decimal]  # scenario -> base currency per 1 unit of `currency`

    @staticmethod
    def of(currency: str, base: str, rates: Mapping[str, Mapping[str, Decimal]], where: str) -> Conversion | None:
        """None for the base currency. Refuses a currency `rates` does not quote in every scenario."""
        if currency == base:
            return None
        available = available_currencies(base, rates)
        if currency not in available:
            raise ReleaseError(f"{where} has no stored {currency} rate; available: {', '.join(available)}. "
                               "A release stores the rates in the period's rate file when it is published.")
        return Conversion(currency, base, {sc: t[currency] for sc, t in rates.items()})

    @property
    def quoted(self) -> str:
        """"actual 1.30 / budget 1.32"."""
        return " / ".join(f"{sc} {rate_text(r)}" for sc, r in self.rates.items())

    @property
    def label(self) -> str:
        return f"{self.currency}, converted from {self.base} at {self.quoted} {self.base} per {self.currency}"

    @property
    def translation_note(self) -> str:
        """Shown wherever converted figures are: the variance mixes performance with the rate difference."""
        rates = " vs ".join(f"{sc} {rate_text(r)}" for sc, r in self.rates.items())
        return (f"Variance in {self.currency} includes the translation effect of {rates} {self.base} per "
                f"{self.currency}; a line on plan in {self.base} can show a {self.currency} variance.")

    def balances(self, bal: Balances) -> dict[tuple[str, str, str], Decimal]:
        """Each balance divided at the store's scale, so subtotals, APAC totals and bridges sum exactly."""
        return {k: (v / self.rates[k[0]]).quantize(STORE_SCALE, ROUND_HALF_UP) for k, v in bal.items()}


@dataclass(frozen=True)
class VarianceReport:
    release_id: str | None  # None for the proposed (unapproved) release
    period: str
    reporting_currency: str
    approved_by: str | None
    approved_at: datetime | None
    as_of: datetime  # approval time, or the newest receipt behind a proposed release
    markets: tuple[Market, ...]
    rows: tuple[VarianceRow, ...]
    bridges: Mapping[str, Bridge]  # BRIDGE_LINES key -> its bridge
    rates: Mapping[str, Mapping[str, Decimal]]  # scenario -> currency -> rate
    facts_checksum: str
    previous_release_id: str | None
    changes: Diff | None  # KPI changes vs previous_release_id
    conversion: Conversion | None = None  # set when amounts are shown in another currency than the base
    previous_rates: Mapping[str, Mapping[str, Decimal]] | None = None  # stored with previous_release_id

    @property
    def currency(self) -> str:
        """The currency every amount in the report is in."""
        return self.conversion.currency if self.conversion else self.reporting_currency

    @property
    def currency_label(self) -> str:
        """"SGD", or "USD, converted from SGD at actual 1.30 / budget 1.32 SGD per USD"."""
        return self.conversion.label if self.conversion else self.reporting_currency

    @property
    def status(self) -> Literal["final", "draft"]:
        return "draft" if self.release_id is None else "final"

    @property
    def bridge(self) -> Bridge:
        return self.bridges["ebitda"]

    @property
    def name(self) -> str:
        return self.release_id or "PROPOSED"

    @property
    def scopes(self) -> tuple[str, ...]:
        return (APAC, *(m.code for m in self.markets))

    def scope(self, scope: str) -> tuple[VarianceRow, ...]:
        return tuple(r for r in self.rows if r.scope == scope)

    def row(self, scope: str, key: str) -> VarianceRow:
        return next(r for r in self.rows if r.scope == scope and r.line.key == key)

    def fx_notes(self, scope: str | None = None) -> tuple[str, ...]:
        """One line per market not reporting in the reporting currency (only `scope`'s, if a market). In a
        converted view the line says the market goes through the base currency on its way to the presentation
        currency, so it does not read as if the table were in the base currency."""
        ccy = self.reporting_currency
        notes = []
        for m in self.markets:
            if m.currency == ccy or scope not in (None, APAC, m.code):
                continue
            actual, budget = (plain(self.rates[sc][m.currency]) for sc in ("actual", "budget"))
            rates = f"actual {actual} / budget {budget}"
            if self.conversion is None:
                notes.append(f"{m.code} translated at {rates} {ccy} per {m.currency}; variance includes FX.")
            else:
                notes.append(f"{m.code}: local {m.currency} translated to {ccy} at {rates} {ccy} per {m.currency}, "
                             f"then to {self.conversion.currency} as above.")
        return tuple(notes)

    @property
    def translation_note(self) -> str | None:
        return self.conversion.translation_note if self.conversion else None

    @property
    def changes_notes(self) -> tuple[str, ...]:
        """Under a converted changes table: where materiality is judged and at which rates the previous release is
        restated, naming a previous release that stored no rate, or another one, for the presentation currency."""
        c = self.conversion
        if c is None or self.changes is None:
            return ()
        previous = self.previous_release_id
        now_rates = f"{self.release_id}'s" if self.release_id else "the proposed release's"
        notes = [f"Materiality is judged in {c.base}, where the thresholds are set.",
                 f"{previous} restated at {now_rates} rates."]
        stored = self.previous_rates or {}
        theirs = {sc: t.get(c.currency) for sc, t in stored.items()}
        if not stored or any(r is None for r in theirs.values()):
            notes.append(f"{previous} did not store a {c.currency} rate.")
        elif theirs != dict(c.rates):
            quoted = " / ".join(f"{sc} {rate_text(r)}" for sc, r in theirs.items())
            notes.append(f"{previous} stored {quoted} {c.base} per {c.currency}; its amounts here use "
                         f"{now_rates} {c.quoted}.")
        return tuple(notes)


def build_report(
    facts: Sequence[ReleaseFact],
    coa: ChartOfAccounts,
    markets: Sequence[Market],
    materiality: Mapping[str, Materiality],
    *,
    release_id: str | None,
    period: str,
    reporting_currency: str,
    approved_by: str | None,
    approved_at: datetime | None,
    as_of: datetime,
    rates: Mapping[str, Mapping[str, Decimal]],
    previous_release_id: str | None,
    previous_facts: Sequence[ReleaseFact] | None,
    currency: str | None = None,
    previous_rates: Mapping[str, Mapping[str, Decimal]] | None = None,
) -> VarianceReport:
    """`currency`: show amounts in this currency rather than the reporting (base) currency."""
    conversion = Conversion.of((currency or reporting_currency).upper(), reporting_currency, rates,
                               release_id or "the proposed release")
    bal = balances(facts)
    order = [m.code for m in markets]
    changes = None
    if previous_facts is not None:
        before = balances(previous_facts)
        changes = diff(before, bal, coa, materiality, order)
        if conversion is not None:
            converted = diff(conversion.balances(before), conversion.balances(bal), coa, materiality, order)
            changes = _converted_changes(changes, converted)
    if conversion is not None:
        bal = conversion.balances(bal)
    scopes = (APAC, *(m.code for m in markets))
    rows = []
    for scope in scopes:
        actual = scope_amounts(bal, "actual", scope)
        budget = scope_amounts(bal, "budget", scope)
        rows += [VarianceRow(scope, line, line.value(actual, coa), line.value(budget, coa)) for line in PNL_LINES]
    bridges = {}
    for kpi, line_key in BRIDGE_LINES.items():
        by_scope = {r.scope: r for r in rows if r.line.key == line_key}
        bridges[kpi] = Bridge.build(by_scope[APAC].budget, ((m.code, by_scope[m.code].variance) for m in markets),
                                    by_scope[APAC].actual, KPI[kpi].label)
    return VarianceReport(
        release_id=release_id,
        period=period,
        reporting_currency=reporting_currency,
        approved_by=approved_by,
        approved_at=approved_at,
        as_of=as_of,
        markets=tuple(markets),
        rows=tuple(rows),
        bridges=bridges,
        rates=rates,
        facts_checksum=facts_checksum(tuple(facts)),
        previous_release_id=previous_release_id,
        changes=changes,
        conversion=conversion,
        previous_rates=previous_rates,
    )


def _converted_changes(base: Diff, converted: Diff) -> Diff:
    """The converted amounts, with materiality as judged in the base currency the thresholds are set in."""
    material = {(k.scenario, k.scope, k.kpi.key): k.material for k in base.kpis}
    return Diff(tuple(replace(k, material=material[(k.scenario, k.scope, k.kpi.key)]) for k in converted.kpis),
                converted.accounts)


def release_report(store: Store, ws: Workspace, release_id: str, currency: str | None = None) -> VarianceReport:
    """Report on an approved release, under the chart of accounts it was published with, in `currency`
    (default the base currency) at the rates stored with the release."""
    release = store.release(release_id)
    if release is None:
        raise ReleaseError(f"no release {release_id!r}")
    coa = stored_coa(store, release.coa_version)
    if coa is None:
        raise ReleaseError(f"{release_id}: chart of accounts {release.coa_version} is not stored")
    previous = release.previous_release_id
    return build_report(
        store.release_facts(release_id),
        coa,
        ws.config.markets,
        ws.config.materiality,
        release_id=release_id,
        period=release.period,
        reporting_currency=release.reporting_currency,
        approved_by=release.approved_by,
        approved_at=release.approved_at,
        as_of=release.approved_at,
        rates=release.rates,
        previous_release_id=previous,
        previous_facts=store.release_facts(previous) if previous else None,
        currency=currency,
        previous_rates=store.release(previous).rates if previous else None,
    )


def proposed_report(store: Store, ws: Workspace, period: str, currency: str | None = None) -> VarianceReport:
    """Report on what `publish` would release now. Refuses while publish would."""
    proposal = propose(store, ws, period)
    facts = proposal.facts
    if facts is None:
        raise ReleaseError(f"no proposed release for {period}: " + "; ".join(proposal.blockers))
    current = proposal.current
    return build_report(
        facts,
        ws.coa,
        ws.config.markets,
        ws.config.materiality,
        release_id=None,
        period=period,
        reporting_currency=ws.config.reporting_currency,
        approved_by=None,
        approved_at=None,
        as_of=max(s.submission.received_at for s in proposal.slots),
        rates=proposal.rates,
        previous_release_id=current.release_id if current else None,
        previous_facts=proposal.current_facts if current else None,
        currency=currency,
        previous_rates=current.rates if current else None,
    )


@dataclass(frozen=True)
class Trace:
    """The release facts behind one P&L line for one scope, down to file and row."""

    release_id: str
    scope: str
    line: AccountLine | SubtotalLine
    facts: tuple[ReleaseFact, ...]

    def total(self, scenario: str) -> Decimal:
        return sum((f.reporting_amount for f in self.facts if f.scenario == scenario), ZERO)


def trace_line(store: Store, release_id: str, scope: str, line_key: str, scenario: str | None = None) -> Trace:
    """Facts behind a P&L line (an account or a subtotal such as EBITDA) for a market or APAC,
    under the chart of accounts the release was published with."""
    release = store.release(release_id)
    if release is None:
        raise ReleaseError(f"no release {release_id!r}")
    coa = stored_coa(store, release.coa_version)
    if coa is None:
        raise ReleaseError(f"{release_id}: chart of accounts {release.coa_version} is not stored")
    line = next((l for l in TRACEABLE_LINES if l.key == line_key), None)
    if line is None:
        raise ReleaseError(f"cannot trace {line_key!r}; lines: {', '.join(l.key for l in TRACEABLE_LINES)}")
    if isinstance(line, AccountLine):
        accounts = {line.key}
    else:
        accounts = {a for a in coa.accounts if coa.type_of(a) in line.kpi.types}
    facts = tuple(
        f
        for f in store.release_facts(release_id)
        if f.account in accounts and scope in (APAC, f.market) and scenario in (None, f.scenario)
    )
    return Trace(release_id, scope, line, facts)
