"""A plan month as one market's budget submission on the regional template, so the close compares actuals
with the plan through the usual ingest, validation and releases."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from .config import ChartOfAccounts, ConfigError, Workspace
from .money import fmt_money
from .plan_config import BELOW_EBIT, Month, PlanError
from .planning import Model
from .templates import TEMPLATES, workbook_bytes

SIGN_NOTE = "Amounts as on the P&L, revenue and costs both positive (the regional template's sign)."


@dataclass(frozen=True)
class BudgetRow:
    account: str
    line: str  # the plan's P&L line
    amount: Decimal  # as on the template: revenue and costs both positive


@dataclass(frozen=True)
class BudgetSubmission:
    plan: str
    version: str
    scenario: str
    market: str
    currency: str
    period: Month
    rows: tuple[BudgetRow, ...]
    left_out: tuple[tuple[str, Decimal], ...]  # below-EBIT lines and their P&L amounts

    @property
    def file_name(self) -> str:
        return f"budget_{self.market}_{self.period}_{self.plan}_{self.version}_{self.scenario}.xlsx"

    @property
    def left_out_text(self) -> str:
        lines = ", ".join(f"{label} {fmt_money(v)}" for label, v in self.left_out)
        return (f"Left out: {lines}. Interest and tax are below EBIT and are not part of the regional P&L, so the "
                "submission stops at EBIT.")

    def xlsx_bytes(self, coa: ChartOfAccounts) -> bytes:
        rows = tuple((str(self.period), r.account, r.line, r.amount) for r in self.rows)
        return workbook_bytes(TEMPLATES["submission"], coa, rows, self.source)

    @property
    def source(self) -> tuple[tuple[str, str], ...]:
        return (("plan", self.plan), ("plan version", self.version), ("scenario", self.scenario),
                ("market", self.market), ("period", str(self.period)), ("currency", self.currency))


def _example(lines: dict[str, str]) -> str:
    def key(k: str) -> str:
        return f'"{k}"' if "." in k else k
    return "\n".join(["[budget]", 'market = "<market code from close.toml>"', "", "[budget.accounts]",
                      *(f'{key(k)} = "<regional account>"  # {label}' for k, label in lines.items())])


def _amounts(m: Model, period: int) -> dict[str, Decimal]:
    """Each line a submission maps, by plan line key, for one month, as on the template (costs positive)."""
    rev, opex = m.schedule("revenue"), m.schedule("opex")
    out = {"revenue": rev.line("Revenue").values[period], "cogs": rev.line("COGS").values[period],
           "payroll": m.schedule("payroll").line("Payroll").values[period]}
    out |= {f"opex.{o.id}": line.values[period] for o, line in zip(m.plan.opex, opex.lines)}
    out["depreciation"] = m.schedule("fixed_assets").line("Depreciation").values[period]
    return out


def budget_submission(m: Model, period: str, ws: Workspace) -> BudgetSubmission:
    """The plan's month as the [budget] market's submission; refuses, saying why, when it cannot be one."""
    plan = m.plan
    lines = plan.budget_lines
    if plan.budget is None:
        raise PlanError([f"plan {plan.name} has no [budget] section, so it cannot be written as a budget "
                         "submission. Add one naming the market and the regional account for each P&L line above "
                         "EBIT, for example:\n" + _example(lines)])
    try:
        month = Month.parse(period)
    except ValueError as e:
        raise PlanError([f"--period: {e}"]) from None
    if month not in m.months:
        raise PlanError([f"period {month} is outside the plan ({m.months[0]} to {m.months[-1]})"])
    b = plan.budget
    try:
        market = ws.config.market(b.market)
    except ConfigError as e:
        raise PlanError([f"budget.market: {e}"]) from None
    if plan.setup.currency != market.currency:
        raise PlanError([f"the plan is in {plan.setup.currency} but {market.code} reports in {market.currency}; a "
                         "budget submission must be in the market's currency"])
    unmapped = [f"{k} ({label})" for k, label in lines.items() if k not in b.accounts]
    if unmapped:
        raise PlanError([f"every P&L line above EBIT needs a regional account in [budget.accounts]; unmapped: "
                         f"{', '.join(unmapped)}"])
    unknown = sorted({code for code in b.accounts.values() if code not in ws.coa.accounts})
    if unknown:
        raise PlanError([f"[budget.accounts] uses accounts not in the chart of accounts: {', '.join(unknown)} "
                         f"(chart: {', '.join(ws.coa.accounts)})"])
    i = m.months.index(month)
    amounts = _amounts(m, i)
    rows = tuple(BudgetRow(b.accounts[k], label, amounts[k]) for k, label in lines.items())
    tax = next(x for x in m.pnl.lines if x.schedule == "tax")
    left_out = ((BELOW_EBIT["interest"], m.pnl.line("Interest").values[i]), (BELOW_EBIT["tax"], tax.values[i]))
    return BudgetSubmission(plan.name, plan.version, m.scenario, market.code, market.currency, month, rows, left_out)
