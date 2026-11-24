"""36-month planning against docs/fixture-expected.md "Planning part two" (exact Decimal): fiscal-year columns,
fiscal-year tax, annual units spread by seasonality, and the year-end equity rollover."""

from decimal import Decimal as D

import pytest
from conftest import REPO

from finance_close_toolkit import planning
from finance_close_toolkit.money import cents
from finance_close_toolkit.plan_config import PlanError, load_plan, parse_plan

PLAN36 = REPO / "plans" / "agency_2027_36m.toml"
PLAN36_TEXT = PLAN36.read_text(encoding="utf-8")
SCENARIOS = (None, "slow_collections", "delayed_hire")


def plan_with(*edits: tuple[str, str]):
    text = PLAN36_TEXT
    for old, new in edits:
        assert old in text, old
        text = text.replace(old, new)
    return parse_plan(text, "edited", "test")


@pytest.fixture(scope="module")
def plan():
    return load_plan(PLAN36)


@pytest.fixture(scope="module")
def base(plan):
    return planning.run(plan)


def annual(m: planning.Model, line: planning.Line) -> list[D]:
    return [line.for_year(y) for y in m.years]


def test_three_fiscal_years(base):
    assert [(y.label, y.months[0].label, y.months[-1].label) for y in base.years] == [
        ("FY2027", "Jan-27", "Dec-27"), ("FY2028", "Jan-28", "Dec-28"), ("FY2029", "Jan-29", "Dec-29")]
    assert len(base.months) == 36 and base.year_one == base.months[:12]


FY_PNL = {
    "Revenue": ("288000", "360000", "432000"),
    "COGS": ("-120000", "-150000", "-180000"),
    "Gross margin": ("168000", "210000", "252000"),
    "Payroll": ("-59400", "-66000", "-66000"),
    "Office rent": ("-12000", "-12000", "-12000"),
    "EBITDA": ("96600", "132000", "174000"),
    "Depreciation": ("-64000", "-66000", "-2000"),
    "EBIT": ("32600", "66000", "172000"),
    "Interest": ("-2250", "0", "0"),
    "PBT": ("30350", "66000", "172000"),
    "Tax (17% of year-to-date profit)": ("-5159.50", "-11220.00", "-29240.00"),
    "Net income": ("25190.50", "54780.00", "142760.00"),
}


@pytest.mark.parametrize("label", FY_PNL)
def test_fiscal_year_pnl(base, label):
    assert annual(base, base.pnl.line(label)) == [D(x) for x in FY_PNL[label]]


def test_pnl_lines_in_order(base):
    assert [x.label for x in base.pnl.lines] == list(FY_PNL)


def test_2027_is_identical_to_the_12_month_plan(plan):
    """Every line of every statement and schedule, every month of 2027, in base and both scenarios."""
    twelve = load_plan(REPO / "plans" / "agency_2027.toml")
    for scenario in SCENARIOS:
        long, short = planning.run(plan, scenario), planning.run(twelve, scenario)
        for a, b in zip((*long.statements, *long.schedules), (*short.statements, *short.schedules), strict=True):
            assert [x.label for x in a.lines] == [x.label for x in b.lines]
            for x in a.lines:
                assert x.values[:12] == b.line(x.label).values, (scenario, a.title, x.label)


def test_monthly_units_and_revenue_from_annual_units(base):
    rev = base.schedule("revenue")
    assert rev.line("Total units").values[12:24] == (D("2500.00"),) * 12
    assert rev.line("Total units").values[24:] == (D("3000.00"),) * 12
    assert rev.line("P1 units").values[12] == D(2000) and rev.line("P2 units").values[12] == D(500)
    assert rev.line("P1 units").values[24] == D(2400) and rev.line("P2 units").values[24] == D(600)
    assert annual(base, rev.line("Total units")) == [D(24000), D(30000), D(36000)]
    assert "FY2028, FY2029: annual units × seasonality index ÷ the year's sum of indices" in rev.basis


def test_monthly_tax(base):
    pbt, tax = base.pnl.line("PBT").values, base.pnl.line("Tax (17% of year-to-date profit)").values
    assert pbt[12:24] == (D(5500),) * 12 and tax[12:24] == (D("-935.00"),) * 12
    assert pbt[24:28] == (D(14000),) * 4 and tax[24:28] == (D("-2380.00"),) * 4
    assert pbt[28:] == (D(14500),) * 8 and tax[28:] == (D("-2465.00"),) * 8


def test_tax_paid_the_month_after_each_quarter(base):
    paid = dict(zip((m.label for m in base.months), base.schedule("tax").line("Tax paid").values))
    in_2028 = {k: v for k, v in paid.items() if k.endswith("-28") and v}
    in_2029 = {k: v for k, v in paid.items() if k.endswith("-29") and v}
    assert in_2028 == {"Jan-28": D("935.00"), "Apr-28": D("2805.00"), "Jul-28": D("2805.00"), "Oct-28": D("2805.00")}
    assert sum(in_2028.values()) == D("9350.00")
    assert in_2029 == {"Jan-29": D("2805.00"), "Apr-29": D("7140.00"), "Jul-29": D("7310.00"), "Oct-29": D("7395.00")}
    assert sum(in_2029.values()) == D("24650.00")


DECEMBER = {  # Dec-2027, Dec-2028, Dec-2029
    "Cash": ("295125.50", "414275.50", "560125.50"),
    "Accounts receivable": ("24000", "30000", "36000"),
    "Fixed assets, net": ("68000", "2000", "0"),
    "Total assets": ("387125.50", "446275.50", "596125.50"),
    "Accounts payable": ("11000", "13500", "16000"),
    "Tax payable": ("935.00", "2805.00", "7395.00"),
    "Debt": ("0", "0", "0"),
    "Share capital": ("300000", "300000", "300000"),
    "Retained earnings brought forward": ("50000", "75190.50", "129970.50"),
    "Profit for the year": ("25190.50", "54780.00", "142760.00"),
    "Total liabilities and equity": ("387125.50", "446275.50", "596125.50"),
}


@pytest.mark.parametrize("label", DECEMBER)
def test_december_balance_sheets(base, label):
    line = base.balance_sheet.line(label)
    assert [line.values[i] for i in (11, 23, 35)] == [D(x) for x in DECEMBER[label]]
    assert annual(base, line) == [D(x) for x in DECEMBER[label]]  # a balance's year column is its closing month


def test_fixed_assets_depreciate_to_zero(base):
    fa = base.schedule("fixed_assets")
    opening, fitout = fa.line("Depreciation, opening assets").values, fa.line("Depreciation, FITOUT").values
    assert opening[23] == D(5000) and set(opening[24:]) == {D(0)}  # 24 months of life, last in Dec-28
    assert fitout[27] == D(500) and set(fitout[28:]) == {D(0)}  # 24 months from May-27, last in Apr-29


def test_year_end_rollover(base):
    bs = base.balance_sheet
    bf, profit = bs.line("Retained earnings brought forward").values, bs.line("Profit for the year").values
    equity = bs.line("Total equity").values
    net = base.pnl.line("Net income").values
    for dec, jan, brought in ((11, 12, "75190.50"), (23, 24, "129970.50")):
        assert bf[jan] == D(brought) == bf[dec] + profit[dec]
        assert profit[jan] == net[jan]  # the profit for the year restarts
        assert equity[jan] == equity[dec] + net[jan]  # the rollover itself moves nothing out of equity
    moved = base.schedule("equity").line("Previous year's profit moved in").values
    assert {i: v for i, v in enumerate(moved) if v} == {12: D("25190.50"), 24: D("54780.00")}


@pytest.mark.parametrize("scenario", SCENARIOS)
def test_every_check_passes_in_all_36_months(plan, scenario):
    m = planning.run(plan, scenario)
    assert [c.status for c in m.checks] == ["pass"] * 5
    details = {c.name: c.detail for c in m.checks}
    assert details["Balance sheet balances in every month (assets = liabilities + equity)"] == \
        "36 of 36 months tie to the cent"
    assert details["Cash-flow closing cash equals balance-sheet cash in every month"] == "36 of 36 months tie to the cent"
    bs = m.balance_sheet
    assert bs.line("Total assets").values == bs.line("Total liabilities and equity").values
    assert m.cash_flow.line("Closing cash").values == bs.line("Cash").values


def test_lowest_cash_overall_and_by_year(base):
    assert planning.lowest_text(base) == "226,700.00 (Jan-27)"
    assert [(y.label, m.label, c) for y, m, c in base.lowest_cash_by_year] == [
        ("FY2027", "Jan-27", D("226700.00")), ("FY2028", "Jan-28", D("301690.50")),
        ("FY2029", "Jan-29", D("422470.50"))]
    assert ("Lowest month-end cash by fiscal year: FY2027 226,700.00 (Jan-27); FY2028 301,690.50 (Jan-28); "
            "FY2029 422,470.50 (Jan-29).") in base.warnings


def test_annual_units_follow_seasonality_and_the_last_month_takes_the_remainder():
    m = planning.run(plan_with(('seasonality = ["1", "1", "1", "1", "1", "1", "1", "1", "1", "1", "1", "1"]',
                                'seasonality = ["2", "1", "1", "1", "1", "1", "1", "1", "1", "1", "1", "1"]')))
    units = m.schedule("revenue").line("Total units").values
    # FY2028: 30,000 × 2/13 = 4,615.384... -> 4,615.38; × 1/13 = 2,307.69; Dec takes the remainder
    assert units[12] == D("4615.38") and units[13:23] == (D("2307.69"),) * 10
    assert units[23] == D(30000) - D("4615.38") - 10 * D("2307.69") == D("2307.72")
    assert sum(units[12:24]) == D(30000) and sum(units[24:]) == D(36000)
    assert units[0] == D(4000) and units[1] == D(2000)  # FY2027 keeps the monthly rule: base units × index
    assert m.passed


def test_a_year_without_annual_units_keeps_the_monthly_growth_rule():
    m = planning.run(plan_with(('annual_units = { "2028" = "30000", "2029" = "36000" }',
                                'annual_units = { "2028" = "30000" }'), ('growth_pct_month = "0"', 'growth_pct_month = "1"')))
    units = m.schedule("revenue").line("Total units").values
    assert units[24] == D(2000) * D("1.01") ** 24
    assert sum(units[12:24]) == D(30000)
    assert m.passed


def test_fiscal_year_start_moves_the_columns_and_the_tax_year():
    m = planning.run(plan_with(('months = 36', 'months = 36\nfiscal_year_start = 4'),
                               ('annual_units = { "2028" = "30000", "2029" = "36000" }', 'annual_units = { "2027" = "30000" }')))
    assert [(y.label, len(y.months)) for y in m.years] == [("FY2026", 3), ("FY2027", 12), ("FY2028", 12), ("FY2029", 9)]
    assert m.schedule("revenue").line("Total units").values[3:15] == (D("2500.00"),) * 12  # Apr-27 to Mar-28
    pbt, tax = m.pnl.line("PBT"), m.pnl.line("Tax (17% of year-to-date profit)")
    for y in m.years:  # tax restarts each April: each fiscal year's tax is 17% of that year's profit
        assert -tax.for_year(y) == cents(D("0.17") * pbt.for_year(y))
    assert m.balance_sheet.line("Retained earnings brought forward").values[3] == \
        D(50000) + m.pnl.line("Net income").for_year(m.years[0])
    assert m.passed


@pytest.mark.parametrize("edits, message", [
    ((('months = 36', 'months = 37'),), "setup.months: expected 1 to 36 months, got 37"),
    ((('months = 36', 'months = 30'),),
     "volume.annual_units: FY2029 is only partly in the plan (Jan-29 to Jun-29); annual units need a whole fiscal year"),
    ((('"2029" = "36000"', '"2030" = "36000"'),), "volume.annual_units: FY2030 is not in the plan"),
    ((('"2029" = "36000"', '"29" = "36000"'),),
     'volume.annual_units: expected a year like "2028" as the key, got \'29\''),
    ((('months = 36', 'months = 36\nfiscal_year_start = 13'),),
     "setup.fiscal_year_start: expected a month number from 1 to 12, got 13"),
    ((('seasonality = ["1", "1", "1", "1", "1", "1", "1", "1", "1", "1", "1", "1"]', 'seasonality = ["1", "1"]'),),
     "volume.seasonality has 2 entries, needs one per month (36) or 12, repeated each year"),
])
def test_horizon_and_annual_inputs_are_validated(edits, message):
    with pytest.raises(PlanError) as e:
        plan_with(*edits)
    assert message in e.value.problems
