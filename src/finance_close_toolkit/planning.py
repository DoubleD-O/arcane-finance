"""Driver-based three-statement engine: schedules roll the plan's assumptions forward month by month,
and the P&L, balance sheet and cash flow are assembled from them. Each computed line is rounded to
cents where it is created and every total is a sum of rounded lines, so nothing needs a plug."""

from __future__ import annotations

from dataclasses import dataclass, replace
from decimal import Decimal
from typing import Literal

from .money import ZERO, cents, fmt_money
from .plan_config import HUNDRED, Change, FiscalYear, Flow, Month, Plan, PlanError, Settlement, scenario_changes

Series = tuple[Decimal, ...]
Annual = Literal["sum", "first", "last", "none"]


@dataclass(frozen=True)
class Line:
    label: str
    values: Series
    schedule: str | None = None  # the schedule this line comes from, for drill-down
    total: bool = False
    annual: Annual = "sum"  # flows sum over the year; balances show the closing month, openings the first;
    # "none" for a line with no meaningful annual figure (the seasonality index)
    quantity: bool = False  # units, not money

    def for_year(self, fy: FiscalYear) -> Decimal | None:
        if self.annual == "none":
            return None
        values = self.values[fy.span]
        if self.annual == "first":
            return values[0]
        if self.annual == "last":
            return values[-1]
        return sum(values, ZERO)


@dataclass(frozen=True)
class Statement:
    title: str
    lines: tuple[Line, ...]

    def line(self, label: str) -> Line:
        return next(x for x in self.lines if x.label == label)


@dataclass(frozen=True)
class Schedule(Statement):
    key: str = ""
    basis: str = ""  # the assumptions the schedule is driven by


@dataclass(frozen=True)
class Check:
    name: str
    passed: bool | None  # None: not run (P&L-only mode)
    detail: str

    @property
    def status(self) -> str:
        return {True: "pass", False: "FAIL", None: "not run"}[self.passed]


@dataclass(frozen=True)
class Model:
    plan: Plan  # with the scenario applied
    scenario: str
    changes: tuple[Change, ...]  # what the scenario changes against base
    months: tuple[Month, ...]
    years: tuple[FiscalYear, ...]
    pnl: Statement
    balance_sheet: Statement | None
    cash_flow: Statement | None
    schedules: tuple[Schedule, ...]
    checks: tuple[Check, ...]
    warnings: tuple[str, ...]
    lowest_cash: tuple[Month, Decimal] | None
    lowest_cash_by_year: tuple[tuple[FiscalYear, Month, Decimal], ...]
    cash_shortfall: str | None  # in plain words, when month-end cash goes below zero
    pnl_only_reason: str | None

    @property
    def year_one(self) -> tuple[Month, ...]:
        """The months shown by default: the first twelve."""
        return self.months[:12]

    @property
    def statements(self) -> tuple[Statement, ...]:
        return tuple(s for s in (self.pnl, self.balance_sheet, self.cash_flow) if s is not None)

    def schedule(self, key: str) -> Schedule:
        return next(s for s in self.schedules if s.key == key)

    @property
    def passed(self) -> bool:
        return all(c.passed is not False for c in self.checks)


def _zeros(n: int) -> list[Decimal]:
    return [ZERO] * n


def add(*series: Series) -> Series:
    return tuple(sum(xs, ZERO) for xs in zip(*series, strict=True))


def neg(series: Series) -> Series:
    return tuple(-x for x in series)


def _by_month(flows: tuple[Flow, ...], months: tuple[Month, ...]) -> Series:
    out = _zeros(len(months))
    for f in flows:
        if months[0] <= f.month <= months[-1]:
            out[f.month.since(months[0])] += f.amount
    return tuple(out)


def _roll(opening: Decimal, *moves: Series) -> tuple[Series, Series]:
    """Opening and closing balance per month for an opening balance and signed monthly movements."""
    net = add(*moves)
    opens, closes, balance = [], [], opening
    for x in net:
        opens.append(balance)
        balance += x
        closes.append(balance)
    return tuple(opens), tuple(closes)


def _split(amount: Decimal, weights: tuple[Decimal, ...]) -> list[Decimal]:
    """amount split in proportion to weights (percentage shares, or seasonality indices), each part to two
    decimals; the last part takes the remainder, so the parts always add back to the amount."""
    total = sum(weights, ZERO)
    parts = [cents(amount * w / total) for w in weights[:-1]]
    return parts + [amount - sum(parts, ZERO)]


def _settle(amounts: Series, curve: tuple[Decimal, ...]) -> Series:
    n = len(amounts)
    out = _zeros(n)
    for k, a in enumerate(amounts):
        for j, part in enumerate(_split(a, curve)):
            if k + j < n:
                out[k + j] += part
    return tuple(out)


def _straight_line(amount: Decimal, life: int, start: int, n: int) -> Series:
    """Monthly straight-line charge from month index start for life months; the last month takes the
    rounding remainder so the asset depreciates to exactly zero."""
    out = _zeros(n)
    monthly = cents(amount / life)
    for i in range(life):
        if 0 <= start + i < n:
            out[start + i] = monthly if i < life - 1 else amount - monthly * (life - 1)
    return tuple(out)


def _ytd_tax(rate_pct: Decimal, pbt: Series, years: tuple[FiscalYear, ...]) -> Series:
    """Tax for a month = rate × max(year-to-date PBT, 0) less the tax already booked in that fiscal year,
    so a loss month reverses earlier tax in the same year. Each fiscal year starts again from zero."""
    out = []
    for fy in years:
        ytd_pbt, booked = ZERO, ZERO
        for x in pbt[fy.span]:
            ytd_pbt += x
            due = cents(rate_pct / HUNDRED * max(ytd_pbt, ZERO))
            out.append(due - booked)
            booked = due
    return tuple(out)


def _loss_years(pbt: Series, years: tuple[FiscalYear, ...]) -> list[str]:
    totals = [(fy, sum(pbt[fy.span], ZERO)) for fy in years]
    return [f"Tax: {fy.label} has a loss before tax of {fmt_money(v)}; no tax is booked for it. "
            "No loss relief across fiscal years." for fy, v in totals if v < 0]


def _shortfall(months: tuple[Month, ...], cash: Series) -> str | None:
    runs: list[list[Month]] = []
    for i, (m, c) in enumerate(zip(months, cash)):
        if c < 0:
            if i and cash[i - 1] < 0:
                runs[-1].append(m)
            else:
                runs.append([m])
    if not runs:
        return None
    spans = [f"in {r[0].label}" if len(r) == 1 else f"from {r[0].label} to {r[-1].label}" for r in runs]
    low = min(range(len(cash)), key=lambda i: (cash[i], i))
    return f"Cash goes below zero {' and '.join(spans)}; lowest {fmt_money(cash[low])} in {months[low].label}."


def _quarterly_payment(opening: Decimal, expense: Series, months: tuple[Month, ...]) -> Series:
    """Tax payable is paid in full in the month after each calendar quarter ends."""
    paid, balance = [], opening
    for m, x in zip(months, expense):
        pay = balance if m.starts_quarter else ZERO
        paid.append(pay)
        balance += x - pay
    return tuple(paid)


class _Builder:
    def __init__(self, plan: Plan, scenario: str):
        self.plan, self.scenario = plan, scenario
        self.months = plan.months
        self.n = len(self.months)
        self.schedules: list[Schedule] = []

    def schedule(self, key: str, title: str, basis: str, lines: list[Line]) -> dict[str, Line]:
        lines = [replace(x, schedule=key) for x in lines]
        self.schedules.append(Schedule(title, tuple(lines), key=key, basis=basis))
        return {x.label: x for x in lines}

    def month_index(self, m: Month) -> int:
        return m.since(self.months[0])

    def units(self) -> tuple[Series, Series, str]:
        """Total units per month, the seasonality index per month, and how each year's units were set."""
        v = self.plan.volume
        growth = 1 + v.growth_pct_month / HUNDRED
        index = tuple(v.index(i) for i in range(self.n))
        units = [v.base_units * s * growth ** i for i, s in enumerate(index)]
        spread = [fy for fy in self.plan.fiscal_years if fy.start_year in v.annual_units]
        for fy in spread:
            units[fy.span] = _split(v.annual_units[fy.start_year], index[fy.span])
        basis = "units = base units × seasonality × (1 + growth)^month"
        if spread:
            basis += (f"; {', '.join(fy.label for fy in spread)}: annual units × seasonality index ÷ the year's sum "
                      "of indices, to two decimals, the last month taking the remainder")
        return tuple(units), index, basis

    def revenue(self) -> tuple[Series, Series]:
        products = self.plan.products
        units, index, basis = self.units()
        lines = [Line("Total units", units, quantity=True),
                 Line("Seasonality index", index, annual="none", quantity=True)]
        revenue, cogs = [], []
        for p in products:
            terms = [p.terms(m) for m in self.months]
            u = tuple(total * t.mix_pct / HUNDRED for total, t in zip(units, terms))
            r = tuple(cents(x * t.net_price) for x, t in zip(u, terms))
            c = tuple(cents(x * t.unit_cost) for x, t in zip(u, terms))
            lines += [Line(f"{p.id} units", u, quantity=True), Line(f"{p.id} revenue", r), Line(f"{p.id} COGS", c)]
            revenue.append(r)
            cogs.append(c)
        total_r, total_c = add(*revenue), add(*cogs)
        lines += [Line("Revenue", total_r, total=True), Line("COGS", total_c, total=True)]
        self.schedule("revenue", "Revenue by product",
                      f"{basis}; split by mix share; revenue = units × list price × (1 − discount); "
                      "COGS = units × unit cost", lines)
        return total_r, total_c

    def payroll(self) -> Series:
        lines = []
        for h in self.plan.hires:
            cost = cents(h.fte * h.monthly_salary * (1 + h.on_cost_pct / HUNDRED))
            lines.append(Line(f"{h.id} {h.role}", tuple(cost if h.active(m) else ZERO for m in self.months)))
        total = add(*(x.values for x in lines)) if lines else tuple(_zeros(self.n))
        self.schedule("payroll", "Payroll by hire",
                      "FTE × monthly salary × (1 + on-cost) in active months; paid in the month",
                      [*lines, Line("Payroll", total, total=True)])
        return total

    def opex(self) -> list[Line]:
        lines = [Line(o.name, tuple(o.monthly_amount if m >= o.start_month else ZERO for m in self.months))
                 for o in self.plan.opex]
        self.schedule("opex", "Other operating expenses", "fixed monthly amount from the start month",
                      [*lines, Line("Other opex", add(*(x.values for x in lines)) if lines else tuple(_zeros(self.n)),
                                    total=True)])
        return lines

    def fixed_assets(self) -> tuple[Series, dict[str, Line] | None]:
        o, a, n = self.plan.opening, self.plan.opening_assets, self.n
        lines, charges = [], []
        if a is not None:
            net = a.cost - a.accumulated_depreciation
            d = _straight_line(net, a.remaining_life_months, 0, n) if net else tuple(_zeros(n))
            lines.append(Line("Depreciation, opening assets", d))
            charges.append(d)
        additions = []
        for c in self.plan.capex:
            spend = _zeros(n)
            if 0 <= self.month_index(c.spend_month) < n:
                spend[self.month_index(c.spend_month)] = c.amount
            additions.append(tuple(spend))
            d = _straight_line(c.amount, c.useful_life_months, self.month_index(c.in_service_month), n)
            lines += [Line(f"{c.id} spend", tuple(spend)), Line(f"Depreciation, {c.id}", d)]
            charges.append(d)
        dep = add(*charges) if charges else tuple(_zeros(n))
        capex = add(*additions) if additions else tuple(_zeros(n))
        lines.append(Line("Depreciation", dep, total=True))
        basis = ("opening net book value ÷ remaining life; capex straight line over its useful life from the "
                 "in-service month; cost added in the spend month")
        if o is None:
            note = " (P&L only: no cost or net book value roll)" if a else " (opening assets not available)"
            self.schedule("fixed_assets", "Fixed assets", basis + note,
                          [*lines, Line("Capex", capex, total=True)])
            return dep, None
        cost_open, cost_close = _roll(o.fixed_assets_cost, capex)
        acc_open, acc_close = _roll(o.accumulated_depreciation, dep)
        lines += [Line("Cost, opening", cost_open, annual="first"), Line("Capex", capex),
                  Line("Cost, closing", cost_close, total=True, annual="last"),
                  Line("Accumulated depreciation, opening", acc_open, annual="first"),
                  Line("Accumulated depreciation, closing", acc_close, total=True, annual="last"),
                  Line("Net book value", add(cost_close, neg(acc_close)), total=True, annual="last")]
        return dep, self.schedule("fixed_assets", "Fixed assets", basis, lines)

    def debt(self) -> dict[str, Line]:
        d = self.plan.debt
        opening = self.plan.opening_debt or ZERO
        draws, repays = _by_month(d.drawdowns, self.months), _by_month(d.repayments, self.months)
        opens, closes = _roll(opening, draws, neg(repays))
        for m, close in zip(self.months, closes):
            if close < 0:
                raise PlanError([f"debt: repayments exceed the balance in {m} (balance {fmt_money(close)})"])
        interest = tuple(cents(d.rate_pct / HUNDRED / 12 * b) for b in opens)
        basis = "interest = annual rate ÷ 12 × balance at the start of the month, paid in the month"
        if self.plan.opening_debt is None:
            basis += " (opening debt not available; balance starts at zero)"
        return self.schedule("debt", "Debt", basis, [
            Line("Opening balance", opens, annual="first"), Line("Drawdowns", draws), Line("Repayments", repays),
            Line("Closing balance", closes, total=True, annual="last"), Line("Interest", interest)])

    def settlement(self, key: str, title: str, balance: str, opening: Decimal, s: Settlement,
                   arising: Series, cash_label: str) -> dict[str, Line]:
        from_opening = _settle_opening(opening, s.opening, self.n)
        from_month = _settle(arising, s.curve)
        cash = add(from_opening, from_month)
        opens, closes = _roll(opening, arising, neg(cash))
        curve = ", ".join(f"{x}% at +{i}" for i, x in enumerate(s.curve))
        sched = ", ".join(f"{x}% in month {i + 1}" for i, x in enumerate(s.opening))
        return self.schedule(key, title, f"curve {curve}; opening {balance} settled {sched}", [
            Line(f"Opening {balance}", opens, annual="first"), Line("Arising", arising),
            Line(f"{cash_label}, opening balance", from_opening), Line(f"{cash_label}, in the plan", from_month),
            Line(cash_label, cash, total=True), Line(f"Closing {balance}", closes, total=True, annual="last")])


def _settle_opening(opening: Decimal, schedule: tuple[Decimal, ...], n: int) -> Series:
    out = _zeros(n)
    for i, part in enumerate(_split(opening, schedule)):
        if i < n:
            out[i] = part
    return tuple(out)


def _pnl_only_reason(plan: Plan) -> str:
    whole = [m for m in plan.missing if "." not in m]
    fields_ = [m.split(".", 1)[1] for m in plan.missing if m.startswith("opening.")]
    parts = []
    if "opening" in whole:
        parts.append("the opening balance sheet ([opening]) is missing")
    elif fields_:
        parts.append(f"the opening balance sheet is incomplete (missing {', '.join(fields_)})")
    others = [f"[{m}]" for m in whole if m != "opening"]
    if others:
        parts.append(f"{' and '.join(others)} {'is' if len(others) == 1 else 'are'} missing")
    return "P&L only: " + "; ".join(parts) + ". No balance sheet or cash flow is produced. " + _opening_in_pnl(plan)


def _opening_in_pnl(plan: Plan) -> str:
    """Whether the P&L carries depreciation of the opening assets and interest on the opening debt."""
    debt_in = plan.opening_debt is not None
    if plan.opening_assets is not None:
        return ("Depreciation of opening fixed assets and interest on opening debt are included." if debt_in
                else "Depreciation of opening fixed assets is included. "
                     "Interest on opening debt is not included (opening debt is missing).")
    if plan.partial_opening is None:
        why = "[opening] is missing"
    else:
        keys = ("fixed_assets_cost", "accumulated_depreciation", "fixed_assets_remaining_life_months")
        why = "missing " + ", ".join(k for k in keys if k not in plan.partial_opening.given)
    return (f"Depreciation of opening fixed assets is not included ({why}); interest on opening debt is "
            + ("included." if debt_in else "not included (opening debt is missing)."))


def run(plan: Plan, scenario: str | None = None) -> Model:
    name = scenario or "base"
    plan, changes = plan.scenario(scenario), scenario_changes(plan, scenario)
    b = _Builder(plan, name)
    months, n, years = b.months, b.n, plan.fiscal_years

    revenue, cogs = b.revenue()
    payroll = b.payroll()
    opex = b.opex()
    dep, fa = b.fixed_assets()
    debt = b.debt()
    interest = debt["Interest"].values

    gm = add(revenue, neg(cogs))
    ebitda = add(gm, neg(payroll), *(neg(x.values) for x in opex))
    ebit = add(ebitda, neg(dep))
    pbt = add(ebit, neg(interest))
    rate = plan.tax.rate_pct
    tax_expense = _ytd_tax(rate, pbt, years)
    net_income = add(pbt, neg(tax_expense))
    pnl = Statement("P&L", (
        Line("Revenue", revenue, "revenue"), Line("COGS", neg(cogs), "revenue"),
        Line("Gross margin", gm, total=True), Line("Payroll", neg(payroll), "payroll"),
        *(Line(x.label, neg(x.values), "opex") for x in opex),
        Line("EBITDA", ebitda, total=True), Line("Depreciation", neg(dep), "fixed_assets"),
        Line("EBIT", ebit, total=True), Line("Interest", neg(interest), "debt"),
        Line("PBT", pbt, total=True), Line(f"Tax ({rate}% of year-to-date profit)", neg(tax_expense), "tax"),
        Line("Net income", net_income, total=True)))

    mix = Check("Mix shares sum to 100% in every month (validated on load)", True,
                f"a plan that fails this does not load; {len(plan.products)} products over {n} months")
    tax_basis = (f"tax for a month = {rate}% × max(fiscal-year-to-date PBT, 0) less the tax already booked in that "
                 "fiscal year, so a loss month can reverse earlier tax; no loss relief across fiscal years")
    tax_notes = tuple(_loss_years(pbt, years))
    if plan.pnl_only:
        b.schedule("tax", "Tax", tax_basis, [Line("PBT", pbt), Line("Tax expense", tax_expense, total=True)])
        not_run = "not run: P&L only"
        checks = (Check("Opening balance sheet balances", None, not_run),
                  Check("Balance sheet balances in every month (assets = liabilities + equity)", None, not_run),
                  Check("Cash-flow closing cash equals balance-sheet cash in every month", None, not_run),
                  mix, Check("Settlement curves sum to 100% (validated on load)", None, not_run))
        return Model(plan, name, changes, months, years, pnl, None, None, tuple(b.schedules), checks, tax_notes,
                     None, (), None, _pnl_only_reason(plan))

    o = plan.opening
    tax_paid = _quarterly_payment(o.tax_payable, tax_expense, months)
    tax_open, tax_close = _roll(o.tax_payable, tax_expense, neg(tax_paid))
    b.schedule("tax", "Tax payable",
               tax_basis + "; the payable is paid (or, when negative, refunded) in the month after each "
               "calendar quarter ends",
               [Line("PBT", pbt), Line("Opening tax payable", tax_open, annual="first"),
                Line("Tax expense", tax_expense), Line("Tax paid", tax_paid),
                Line("Closing tax payable", tax_close, total=True, annual="last")])

    ar = b.settlement("receivables", "Accounts receivable", "AR", o.accounts_receivable, plan.collections,
                      revenue, "Receipts")
    purchases = add(cogs, *(x.values for x in opex))
    ap = b.settlement("payables", "Accounts payable (COGS and other opex)", "AP", o.accounts_payable,
                      plan.payments, purchases, "Supplier payments")

    e = plan.equity
    injections, dividends = _by_month(e.injections, months), _by_month(e.dividends, months)
    sc_open, sc_close = _roll(o.share_capital, injections)
    # Year-end rollover: in each fiscal year's first month the previous year's profit moves to retained earnings.
    rollover = _zeros(n)
    for prev, fy in zip(years, years[1:]):
        rollover[fy.first] = sum(net_income[prev.span], ZERO)
    rollover = tuple(rollover)
    re_open, re_close = _roll(o.retained_earnings, rollover, neg(dividends))
    py_open, py_close = _roll(ZERO, net_income, neg(rollover))
    b.schedule("equity", "Equity",
               "share capital + injections; retained earnings brought forward = opening + previous fiscal years' "
               "profit − dividends; profit for the year = net income since the fiscal year started, moved to "
               "retained earnings in the first month of the next fiscal year", [
                   Line("Share capital, opening", sc_open, annual="first"), Line("Injections", injections),
                   Line("Share capital, closing", sc_close, total=True, annual="last"),
                   Line("Retained earnings brought forward, opening", re_open, annual="first"),
                   Line("Previous year's profit moved in", rollover), Line("Dividends", dividends),
                   Line("Retained earnings brought forward, closing", re_close, total=True, annual="last"),
                   Line("Profit for the year, opening", py_open, annual="first"), Line("Net income", net_income),
                   Line("Moved to retained earnings", neg(rollover)),
                   Line("Profit for the year, closing", py_close, total=True, annual="last")])

    capex = fa["Capex"].values
    draws, repays = debt["Drawdowns"].values, debt["Repayments"].values
    receipts, supplier = ar["Receipts"].values, ap["Supplier payments"].values
    cash_moves = (receipts, neg(supplier), neg(payroll), neg(interest), neg(tax_paid), neg(capex), draws,
                  neg(repays), injections, neg(dividends))
    cash_open, cash_close = _roll(o.cash, *cash_moves)
    b.schedule("cash", "Cash receipts and payments", "every cash movement from the schedules", [
        Line("Opening cash", cash_open, annual="first"), Line("Receipts from customers", receipts),
        Line("Supplier payments", neg(supplier)), Line("Payroll", neg(payroll)), Line("Interest", neg(interest)),
        Line("Tax paid", neg(tax_paid)), Line("Capex", neg(capex)), Line("Debt drawdowns", draws),
        Line("Debt repayments", neg(repays)), Line("Equity injections", injections), Line("Dividends", neg(dividends)),
        Line("Closing cash", cash_close, total=True, annual="last")])

    ar_close, ap_close = ar["Closing AR"].values, ap["Closing AP"].values
    nbv, debt_close = fa["Net book value"].values, debt["Closing balance"].values
    assets = add(cash_close, ar_close, nbv)
    liabilities = add(ap_close, tax_close, debt_close)
    equity = add(sc_close, re_close, py_close)
    l_and_e = add(liabilities, equity)
    bs = Statement("Balance sheet", tuple(Line(*x, annual="last") for x in (
        ("Cash", cash_close, "cash"), ("Accounts receivable", ar_close, "receivables"),
        ("Fixed assets, cost", fa["Cost, closing"].values, "fixed_assets"),
        ("Accumulated depreciation", neg(fa["Accumulated depreciation, closing"].values), "fixed_assets"),
        ("Fixed assets, net", nbv, "fixed_assets", True), ("Total assets", assets, None, True),
        ("Accounts payable", ap_close, "payables"), ("Tax payable", tax_close, "tax"),
        ("Debt", debt_close, "debt"), ("Total liabilities", liabilities, None, True),
        ("Share capital", sc_close, "equity"), ("Retained earnings brought forward", re_close, "equity"),
        ("Profit for the year", py_close, "equity"),
        ("Total equity", equity, None, True), ("Total liabilities and equity", l_and_e, None, True))))

    # Indirect method from the P&L and balance movements; checked against the cash schedule above.
    def change(opens: Series, closes: Series) -> Series:
        return add(closes, neg(opens))

    ar_open, ap_open = ar["Opening AR"].values, ap["Opening AP"].values
    operating = add(net_income, dep, neg(change(ar_open, ar_close)), change(ap_open, ap_close),
                    change(tax_open, tax_close))
    investing = neg(capex)
    financing = add(draws, neg(repays), injections, neg(dividends))
    net_change = add(operating, investing, financing)
    cf_open, cf_close = _roll(o.cash, net_change)
    cf = Statement("Cash flow", (
        Line("Net income", net_income), Line("Depreciation", dep, "fixed_assets"),
        Line("Change in receivables", neg(change(ar_open, ar_close)), "receivables"),
        Line("Change in payables", change(ap_open, ap_close), "payables"),
        Line("Change in tax payable", change(tax_open, tax_close), "tax"),
        Line("Operating cash flow", operating, total=True),
        Line("Capex", investing, "fixed_assets"), Line("Investing cash flow", investing, total=True),
        Line("Debt drawdowns", draws, "debt"), Line("Debt repayments", neg(repays), "debt"),
        Line("Equity injections", injections, "equity"), Line("Dividends", neg(dividends), "equity"),
        Line("Financing cash flow", financing, total=True),
        Line("Net change in cash", net_change, total=True),
        Line("Opening cash", cf_open, annual="first"), Line("Closing cash", cf_close, total=True, annual="last")))

    checks = (
        _opening_check(o.total_assets, o.total_liabilities_and_equity),
        _monthly_check("Balance sheet balances in every month (assets = liabilities + equity)", months, assets,
                       l_and_e, "assets", "liabilities and equity"),
        _monthly_check("Cash-flow closing cash equals balance-sheet cash in every month", months, cf_close,
                       cash_close, "cash-flow closing cash", "balance-sheet cash"),
        mix,
        Check("Settlement curves sum to 100% (validated on load)", True,
              "a plan that fails this does not load; collections, payments and both opening schedules"))
    lowest = _lowest(months, cash_close, range(n))
    by_year = tuple((fy, *_lowest(months, cash_close, range(fy.first, fy.first + len(fy.months)))) for fy in years)
    warnings = [f"Lowest month-end cash: {fmt_money(lowest[1])} ({lowest[0].label})."]
    if len(years) > 1:
        warnings.append("Lowest month-end cash by fiscal year: "
                        + "; ".join(f"{fy.label} {fmt_money(c)} ({m.label})" for fy, m, c in by_year) + ".")
    shortfall = _shortfall(months, cash_close)
    warnings.append(f"CASH_SHORTFALL: {shortfall}" if shortfall
                    else "No cash shortfall: month-end cash stays at or above zero.")
    return Model(plan, name, changes, months, years, pnl, bs, cf, tuple(b.schedules), checks, (*warnings, *tax_notes),
                 lowest, by_year, shortfall, None)


def _lowest(months: tuple[Month, ...], cash: Series, within: range) -> tuple[Month, Decimal]:
    i = min(within, key=lambda i: (cash[i], i))
    return months[i], cash[i]


def _opening_check(assets: Decimal, l_and_e: Decimal) -> Check:
    diff = assets - l_and_e
    detail = f"assets {fmt_money(assets)}, liabilities and equity {fmt_money(l_and_e)}"
    if diff:
        detail += f": out by {fmt_money(abs(diff))}"
    return Check("Opening balance sheet balances", diff == 0, detail)


def _monthly_check(name: str, months: tuple[Month, ...], left: Series, right: Series, left_name: str,
                   right_name: str) -> Check:
    off = [(m, a - b) for m, a, b in zip(months, left, right) if a != b]
    if not off:
        return Check(name, True, f"{len(months)} of {len(months)} months tie to the cent")
    m, diff = off[0]
    more = f" and {len(off) - 1} other month{'s' * (len(off) != 2)}" if len(off) > 1 else ""
    return Check(name, False, f"{len(off)} of {len(months)} months do not tie; first {m.label}: {left_name} "
                              f"{'exceed' if diff > 0 else 'fall short of'} {right_name} by "
                              f"{fmt_money(abs(diff))}{more}")


def lowest_text(m: Model) -> str:
    return "n/a (P&L only)" if m.lowest_cash is None else f"{fmt_money(m.lowest_cash[1])} ({m.lowest_cash[0].label})"


# Scenario vs base


@dataclass(frozen=True)
class CompareRow:
    label: str
    base: Line
    scenario: Line

    @property
    def change(self) -> Line:
        return Line("Change", add(self.scenario.values, neg(self.base.values)), annual=self.base.annual)


@dataclass(frozen=True)
class Comparison:
    base: Model
    scenario: Model
    rows: tuple[CompareRow, ...]


KEY_LINES = (("Revenue", "pnl"), ("EBITDA", "pnl"), ("Net income", "pnl"), ("Cash", "balance_sheet"),
             ("Accounts receivable", "balance_sheet"))
KEY_LABELS = {"Cash": "Closing cash", "Accounts receivable": "AR"}


def compare(base: Model, scenario: Model) -> Comparison:
    rows = []
    for label, where in KEY_LINES:
        a, b = getattr(base, where), getattr(scenario, where)
        if a is not None and b is not None:
            rows.append(CompareRow(KEY_LABELS.get(label, label), a.line(label), b.line(label)))
    return Comparison(base, scenario, tuple(rows))
