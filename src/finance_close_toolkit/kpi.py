from collections import defaultdict
from dataclasses import dataclass
from decimal import Decimal
from typing import Iterable, Mapping, Protocol

from .config import ACCOUNT_TYPES, AccountType, ChartOfAccounts, Materiality
from .money import ZERO

APAC = "APAC"


@dataclass(frozen=True)
class Kpi:
    key: str
    label: str
    types: frozenset[AccountType]


KPIS: tuple[Kpi, ...] = (
    Kpi("revenue", "Revenue", frozenset({"revenue"})),
    Kpi("gross_margin", "Gross margin", frozenset({"revenue", "cogs"})),
    Kpi("ebitda", "EBITDA", frozenset({"revenue", "cogs", "opex"})),
)
KPI = {k.key: k for k in KPIS}

# A P&L subtotal, not a tracked KPI: no materiality and not in release manifests.
EBIT = Kpi("ebit", "Operating profit (EBIT)", frozenset(ACCOUNT_TYPES))


class ReportingFact(Protocol):
    market: str
    scenario: str
    account: str
    reporting_amount: Decimal


Balances = Mapping[tuple[str, str, str], Decimal]


def balances(facts: Iterable[ReportingFact]) -> dict[tuple[str, str, str], Decimal]:
    out: dict[tuple[str, str, str], Decimal] = defaultdict(lambda: ZERO)
    for f in facts:
        out[(f.scenario, f.market, f.account)] += f.reporting_amount
    return dict(out)


def kpi_value(amounts: Mapping[str, Decimal], coa: ChartOfAccounts, kpi: Kpi) -> Decimal:
    """Sum of the account amounts whose type the KPI covers; `amounts` is account -> amount."""
    return sum((v for acct, v in amounts.items() if coa.type_of(acct) in kpi.types), ZERO)


def scope_amounts(bal: Balances, scenario: str, scope: str) -> dict[str, Decimal]:
    """Account -> amount for one market, or every market when scope is APAC."""
    amounts: dict[str, Decimal] = defaultdict(lambda: ZERO)
    for (s, market, account), v in bal.items():
        if s == scenario and (scope == APAC or market == scope):
            amounts[account] += v
    return dict(amounts)


def scope_totals(bal: Balances, coa: ChartOfAccounts, scenario: str, scope: str) -> dict[str, Decimal]:
    amounts = scope_amounts(bal, scenario, scope)
    return {k.key: kpi_value(amounts, coa, k) for k in KPIS}


def totals(facts: Iterable[ReportingFact], coa: ChartOfAccounts) -> dict[str, dict[str, dict[str, Decimal]]]:
    """scenario -> scope (each market, then APAC) -> KPI -> amount."""
    bal = balances(facts)
    out: dict[str, dict[str, dict[str, Decimal]]] = {}
    for scenario in sorted({s for s, _, _ in bal}):
        markets = sorted({m for s, m, _ in bal if s == scenario})
        out[scenario] = {scope: scope_totals(bal, coa, scenario, scope) for scope in [*markets, APAC]}
    return out


@dataclass(frozen=True)
class KpiDelta:
    scenario: str
    scope: str
    kpi: Kpi
    before: Decimal
    after: Decimal
    material: bool

    @property
    def change(self) -> Decimal:
        return self.after - self.before

    @property
    def pct(self) -> Decimal | None:
        """Change as a % of the absolute base; None ("n/m") when the base is zero."""
        if self.before == 0:
            return None
        return self.change / abs(self.before) * 100


def is_material(before: Decimal, after: Decimal, threshold: Materiality) -> bool:
    change = after - before
    if abs(change) >= threshold.abs:
        return True
    return before != 0 and abs(change / abs(before) * 100) >= threshold.pct


@dataclass(frozen=True)
class AccountDelta:
    scenario: str
    market: str
    account: str
    before: Decimal
    after: Decimal

    @property
    def change(self) -> Decimal:
        return self.after - self.before


@dataclass(frozen=True)
class Diff:
    kpis: tuple[KpiDelta, ...]
    accounts: tuple[AccountDelta, ...]

    @property
    def material(self) -> bool:
        return any(d.material for d in self.kpis)

    def for_slot(self, scenario: str, market: str) -> "Diff":
        """The slot's market-level deltas, its scenario's APAC deltas, and its accounts."""
        return Diff(
            tuple(d for d in self.kpis if d.scenario == scenario and d.scope in (market, APAC)),
            tuple(a for a in self.accounts if a.scenario == scenario and a.market == market),
        )


def diff(
    before: Balances,
    after: Balances,
    coa: ChartOfAccounts,
    materiality: Mapping[str, Materiality],
    market_order: Iterable[str],
) -> Diff:
    order = {m: i for i, m in enumerate(market_order)}
    accounts = tuple(
        sorted(
            (
                AccountDelta(s, m, a, before.get((s, m, a), ZERO), after.get((s, m, a), ZERO))
                for (s, m, a) in set(before) | set(after)
                if before.get((s, m, a), ZERO) != after.get((s, m, a), ZERO)
            ),
            key=lambda d: (d.scenario, order.get(d.market, len(order)), d.market, d.account),
        )
    )
    kpis = []
    for scenario in sorted({a.scenario for a in accounts}):
        changed = sorted(
            {a.market for a in accounts if a.scenario == scenario}, key=lambda m: (order.get(m, len(order)), m)
        )
        for scope in [*changed, APAC]:
            b = scope_totals(before, coa, scenario, scope)
            a = scope_totals(after, coa, scenario, scope)
            for k in KPIS:
                kpis.append(KpiDelta(scenario, scope, k, b[k.key], a[k.key], is_material(b[k.key], a[k.key], materiality[k.key])))
    return Diff(tuple(kpis), accounts)
