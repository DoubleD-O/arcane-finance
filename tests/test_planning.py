"""Step 5 planning engine against docs/fixture-expected.md "Step 5" (exact Decimal), and the original brief's
planning behaviours: each driver moves only what it should, and the statements tie with no plug."""

from decimal import Decimal as D

import pytest
from conftest import REPO

from finance_close_toolkit import planning
from finance_close_toolkit.money import fmt_money
from finance_close_toolkit.plan_config import Month, PlanError, load_plan, parse_plan

AGENCY = REPO / "plans" / "agency_2027.toml"
MIX = REPO / "plans" / "mix_check.toml"
AGENCY_TEXT = AGENCY.read_text(encoding="utf-8")
Y1 = load_plan(AGENCY).fiscal_years[0]  # FY2027, Jan-27 to Dec-27


def plan_with(*edits: tuple[str, str]):
    text = AGENCY_TEXT
    for old, new in edits:
        assert old in text, old
        text = text.replace(old, new)
    return parse_plan(text, "edited", "test")


def months(jan_apr: list, may_oct: str, nov: str, dec: str) -> list[D]:
    return [D(x) for x in (*jan_apr, *[may_oct] * 6, nov, dec)]


@pytest.fixture(scope="module")
def plan():
    return load_plan(AGENCY)


@pytest.fixture(scope="module")
def base(plan):
    return planning.run(plan)


BASE_PNL = {
    "Revenue": (months(["24000"] * 4, "24000", "24000", "24000"), "288000"),
    "COGS": (months(["-10000"] * 4, "-10000", "-10000", "-10000"), "-120000"),
    "Gross margin": (months(["14000"] * 4, "14000", "14000", "14000"), "168000"),
    "Payroll": (months(["-3300", "-3300", "-3300", "-5500"], "-5500", "-5500", "-5500"), "-59400"),
    "Office rent": (months(["-1000"] * 4, "-1000", "-1000", "-1000"), "-12000"),
    "EBITDA": (months(["9700", "9700", "9700", "7500"], "7500", "7500", "7500"), "96600"),
    "Depreciation": (months(["-5000"] * 4, "-5500", "-5500", "-5500"), "-64000"),
    "EBIT": (months(["4700", "4700", "4700", "2500"], "2000", "2000", "2000"), "32600"),
    "Interest": (months(["0", "0", "-250", "-250"], "-250", "-250", "0"), "-2250"),
    "PBT": (months(["4700", "4700", "4450", "2250"], "1750", "1750", "2000"), "30350"),
    "Tax (17% of year-to-date profit)": (months(["-799.00", "-799.00", "-756.50", "-382.50"], "-297.50", "-297.50", "-340.00"), "-5159.50"),
    "Net income": (months(["3901.00", "3901.00", "3693.50", "1867.50"], "1452.50", "1452.50", "1660.00"),
                   "25190.50"),
}


@pytest.mark.parametrize("label", BASE_PNL)
def test_base_pnl_by_month_and_year(base, label):
    values, year = BASE_PNL[label]
    line = base.pnl.line(label)
    assert list(line.values) == values
    assert line.for_year(Y1) == D(year)


def test_pnl_lines_in_order(base):
    assert [x.label for x in base.pnl.lines] == list(BASE_PNL)


CASH = [  # receipts, payments (AP + payroll + interest + tax + capex), debt, closing cash
    ("60000", "33300", "0", "226700.00"), ("24000", "14300", "50000", "286400.00"),
    ("24000", "26550", "0", "283850.00"), ("24000", "19104.50", "0", "288745.50"),
    ("24000", "16750", "0", "295995.50"), ("24000", "16750", "0", "303245.50"),
    ("24000", "17727.50", "0", "309518.00"), ("24000", "16750", "0", "316768.00"),
    ("24000", "16750", "0", "324018.00"), ("24000", "17642.50", "0", "330375.50"),
    ("24000", "16750", "-50000", "287625.50"), ("24000", "16500", "0", "295125.50"),
]


def test_cash_table(base):
    cash = base.schedule("cash")
    line = lambda label: cash.line(label).values  # noqa: E731
    payments = planning.add(*(line(x) for x in ("Supplier payments", "Payroll", "Interest", "Tax paid", "Capex")))
    debt = planning.add(line("Debt drawdowns"), line("Debt repayments"))
    got = list(zip(line("Receipts from customers"), planning.neg(payments), debt, line("Closing cash")))
    assert got == [tuple(D(x) for x in row) for row in CASH]
    assert list(base.balance_sheet.line("Cash").values) == [D(r[3]) for r in CASH]


def test_tax_paid_the_month_after_each_quarter(base):
    tax = base.schedule("tax")
    paid = {m.label: v for m, v in zip(base.months, tax.line("Tax paid").values) if v}
    assert paid == {"Apr-27": D("2354.50"), "Jul-27": D("977.50"), "Oct-27": D("892.50")}
    assert tax.line("Closing tax payable").values[-1] == D("935.00")


def test_december_balance_sheet(base):
    bs = base.balance_sheet
    dec = {x.label: x.values[-1] for x in bs.lines}
    assert dec == {
        "Cash": D("295125.50"), "Accounts receivable": D("24000"), "Fixed assets, cost": D("132000"),
        "Accumulated depreciation": D("-64000"), "Fixed assets, net": D("68000"), "Total assets": D("387125.50"),
        "Accounts payable": D("11000"), "Tax payable": D("935.00"), "Debt": D("0"),
        "Total liabilities": D("11935.00"), "Share capital": D("300000"),
        "Retained earnings brought forward": D("50000"), "Profit for the year": D("25190.50"),
        "Total equity": D("375190.50"), "Total liabilities and equity": D("387125.50"),
    }
    assert all(x.for_year(Y1) == x.values[-1] for x in bs.lines)  # the annual column is the closing month


def test_opening_balance_sheet(plan):
    assert plan.opening.total_assets == D("380000")
    assert plan.opening.accounts_payable == D("30000")
    assert plan.opening.total_liabilities_and_equity - plan.opening.accounts_payable == D("350000")


SCENARIOS = {  # revenue, EBITDA, net income (year), Dec AR, Dec cash, lowest cash
    None: ("288000", "96600", "25190.50", "24000", "295125.50", "226700.00"),
    "slow_collections": ("288000", "96600", "25190.50", "36000", "283125.50", "226700.00"),
    "delayed_hire": ("288000", "101000", "28842.50", "24000", "298777.50", "226700.00"),
}


@pytest.mark.parametrize("scenario", SCENARIOS)
def test_scenario_rows(plan, scenario):
    m = planning.run(plan, scenario)
    got = (m.pnl.line("Revenue").for_year(Y1), m.pnl.line("EBITDA").for_year(Y1), m.pnl.line("Net income").for_year(Y1),
           m.balance_sheet.line("Accounts receivable").for_year(Y1), m.balance_sheet.line("Cash").for_year(Y1), m.lowest_cash[1])
    assert got == tuple(D(x) for x in SCENARIOS[scenario])
    assert m.lowest_cash[0] == Month(2027, 1)
    assert m.warnings == ("Lowest month-end cash: 226,700.00 (Jan-27).",
                          "No cash shortfall: month-end cash stays at or above zero.")


@pytest.mark.parametrize("scenario", SCENARIOS)
def test_every_check_passes(plan, scenario):
    m = planning.run(plan, scenario)
    assert [(c.name, c.passed) for c in m.checks] == [
        ("Opening balance sheet balances", True),
        ("Balance sheet balances in every month (assets = liabilities + equity)", True),
        ("Cash-flow closing cash equals balance-sheet cash in every month", True),
        ("Mix shares sum to 100% in every month (validated on load)", True),
        ("Settlement curves sum to 100% (validated on load)", True),
    ]
    assert m.passed


def test_collection_timing_moves_ar_and_cash_only(plan, base):
    slow = planning.run(plan, "slow_collections")
    assert [x.values for x in slow.pnl.lines] == [x.values for x in base.pnl.lines]
    cash_gap = planning.add(base.balance_sheet.line("Cash").values, planning.neg(slow.balance_sheet.line("Cash").values))
    assert list(cash_gap) == [D(0)] + [D(12000)] * 11
    ar_gap = planning.add(slow.balance_sheet.line("Accounts receivable").values,
                          planning.neg(base.balance_sheet.line("Accounts receivable").values))
    assert list(ar_gap) == list(cash_gap)


def test_delayed_hire_moves_payroll_in_april_and_may(plan, base):
    late = planning.run(plan, "delayed_hire")
    gap = planning.add(late.pnl.line("Payroll").values, planning.neg(base.pnl.line("Payroll").values))
    assert list(gap) == [D(0)] * 3 + [D(2200)] * 2 + [D(0)] * 7
    tax_gap = planning.add(late.schedule("tax").line("Tax paid").values,
                           planning.neg(base.schedule("tax").line("Tax paid").values))
    assert {late.months[i].label: v for i, v in enumerate(tax_gap) if v} == {"Jul-27": D("748.00")}
    assert late.balance_sheet.line("Cash").for_year(Y1) - base.balance_sheet.line("Cash").for_year(Y1) == D("3652.00")


def test_capex_depreciates_from_in_service_not_spend(base):
    fa = base.schedule("fixed_assets")
    assert list(fa.line("Depreciation, FITOUT").values) == [D(0)] * 4 + [D(500)] * 8
    assert list(fa.line("FITOUT spend").values) == [D(0)] * 2 + [D(12000)] + [D(0)] * 9
    assert list(base.cash_flow.line("Capex").values) == [D(0)] * 2 + [D(-12000)] + [D(0)] * 9
    later = planning.run(plan_with(('in_service_month = "2027-05"', 'in_service_month = "2027-09"')))
    assert list(later.schedule("fixed_assets").line("Depreciation, FITOUT").values) == [D(0)] * 8 + [D(500)] * 4
    assert later.balance_sheet.line("Fixed assets, cost").for_year(Y1) == D(132000)


def test_interest_is_on_the_balance_at_the_start_of_the_month(base):
    debt = base.schedule("debt")
    assert list(debt.line("Opening balance").values) == [D(0)] * 2 + [D(50000)] * 9 + [D(0)]
    assert list(debt.line("Interest").values) == [D(0)] * 2 + [D(250)] * 9 + [D(0)]
    early = planning.run(plan_with(('{ month = "2027-02", amount = "50000" }', '{ month = "2027-01", amount = "50000" }')))
    assert list(early.pnl.line("Interest").values) == [D(0)] + [D(-250)] * 10 + [D(0)]


def test_unbalanced_opening_balance_sheet_is_reported_not_forced():
    m = planning.run(plan_with(('cash = "200000"', 'cash = "201000"')))
    checks = {c.name: c for c in m.checks}
    opening = checks["Opening balance sheet balances"]
    assert opening.passed is False
    assert opening.detail == "assets 381,000.00, liabilities and equity 380,000.00: out by 1,000.00"
    monthly = checks["Balance sheet balances in every month (assets = liabilities + equity)"]
    assert monthly.passed is False
    assert monthly.detail.startswith("12 of 12 months do not tie; first Jan-27: assets exceed liabilities and "
                                     "equity by 1,000.00")
    assert m.balance_sheet.line("Cash").values[0] == D("227700.00")  # cash carries the extra 1,000, no plug
    assert m.balance_sheet.line("Profit for the year").values[-1] == D("25190.50")
    assert not m.passed


def test_cash_shortfall_is_a_warning():
    m = planning.run(plan_with(('cash = "200000"', 'cash = "-30000"'), ('share_capital = "300000"', 'share_capital = "70000"')))
    assert m.passed
    assert m.lowest_cash == (Month(2027, 1), D("-3300.00"))
    assert m.cash_shortfall == "Cash goes below zero in Jan-27; lowest (3,300.00) in Jan-27."
    assert m.warnings[1] == "CASH_SHORTFALL: Cash goes below zero in Jan-27; lowest (3,300.00) in Jan-27."


def test_cash_shortfall_names_each_run_of_months():
    # base closing cash less 290,000: negative Jan-Apr (-63,300 lowest) and again in Nov (-2,374.50)
    m = planning.run(plan_with(('cash = "200000"', 'cash = "-90000"'), ('share_capital = "300000"', 'share_capital = "10000"')))
    assert m.passed
    assert m.cash_shortfall == "Cash goes below zero from Jan-27 to Apr-27 and in Nov-27; lowest (63,300.00) in Jan-27."
    assert planning.run(load_plan(AGENCY)).cash_shortfall is None


def test_rounding_ties_without_a_plug():
    m = planning.run(plan_with(
        ('list_price = "10"', 'list_price = "9.99"'), ('discount_pct = "0"\nunit_cost = "4"', 'discount_pct = "7.5"\nunit_cost = "4.13"'),
        ('curve = ["0", "100"]\nopening_ar_schedule', 'curve = ["33.33", "33.33", "33.34"]\nopening_ar_schedule'),
        ('growth_pct_month = "0"', 'growth_pct_month = "1.7"'), ('rate_pct = "6"', 'rate_pct = "7.25"')))
    assert m.passed
    revenue = m.schedule("revenue")
    assert all(v == v.quantize(D("0.01")) for x in revenue.lines if "units" not in x.label for v in x.values)
    assert m.pnl.line("Revenue").values == planning.add(revenue.line("P1 revenue").values, revenue.line("P2 revenue").values)


def test_mix_check_runs_pnl_only():
    plan = load_plan(MIX)
    for scenario, revenue in ((None, "12000"), ("mix_shift", "14000")):
        m = planning.run(plan, scenario)
        assert m.balance_sheet is None and m.cash_flow is None and m.lowest_cash is None
        assert list(m.pnl.line("Revenue").values) == [D(revenue)] * 12
        assert m.pnl_only_reason.startswith("P&L only: the opening balance sheet ([opening]) is missing")
        assert [c.status for c in m.checks] == ["not run", "not run", "not run", "pass", "not run"]
    assert planning.run(plan).schedule("revenue").line("A units").values[0] == D(800)


def test_incomplete_opening_names_the_missing_fields():
    m = planning.run(plan_with(('cash = "200000"\n', ''), ('debt = "0"\n', '')))
    assert m.balance_sheet is None
    assert "the opening balance sheet is incomplete (missing cash, debt)" in m.pnl_only_reason
    assert m.pnl_only_reason.endswith("Interest on opening debt is not included (opening debt is missing).")


def test_pnl_only_keeps_opening_depreciation_and_interest_when_their_fields_are_given(base):
    m = planning.run(plan_with(('cash = "200000"\n', ''), ('debt = "0"', 'debt = "10000"')))
    assert m.balance_sheet is None
    assert m.pnl.line("Depreciation").values == base.pnl.line("Depreciation").values  # 5,000 a month from opening assets
    assert list(m.pnl.line("Interest").values)[:2] == [D(-50), D(-50)]  # 6% / 12 of the opening 10,000
    assert m.pnl_only_reason.endswith("Depreciation of opening fixed assets and interest on opening debt are included.")


def test_pnl_only_discloses_opening_depreciation_left_out():
    m = planning.run(plan_with(('fixed_assets_cost = "120000"\n', '')))
    assert list(m.pnl.line("Depreciation").values) == [D(0)] * 4 + [D(-500)] * 8  # the fit-out only
    assert m.pnl_only_reason.endswith("Depreciation of opening fixed assets is not included (missing "
                                      "fixed_assets_cost); interest on opening debt is included.")


def test_partial_opening_values_are_still_validated():
    with pytest.raises(PlanError, match=r"opening\.debt: expected an amount of 0 or more"):
        plan_with(('cash = "200000"\n', ''), ('debt = "0"', 'debt = "-1"'))


def test_mix_not_summing_to_100_is_an_error():
    with pytest.raises(PlanError) as e:
        plan_with(('mix_pct = "20"', 'mix_pct = "10"'))
    assert e.value.problems == ("products: mix shares sum to 90% in 2027-01, must be 100% in every month "
                                "(12 months affected)",)


def test_mix_change_from_a_month_is_checked_every_month():
    with pytest.raises(PlanError, match="mix shares sum to 110% in 2027-07"):
        plan_with(('mix_pct = "80"', 'mix_pct = "80"\nchanges = [{ from_month = "2027-07", mix_pct = "90" }]'))
    p = plan_with(('mix_pct = "80"', 'mix_pct = "80"\nchanges = [{ from_month = "2027-07", mix_pct = "60" }]'),
                  ('mix_pct = "20"', 'mix_pct = "20"\nchanges = [{ from_month = "2027-07", mix_pct = "40" }]'))
    revenue = planning.run(p).pnl.line("Revenue").values
    assert list(revenue) == [D(24000)] * 6 + [D(28000)] * 6


def test_curve_not_summing_to_100_is_an_error():
    with pytest.raises(PlanError) as e:
        plan_with(('curve = ["0", "100"]\nopening_ar', 'curve = ["0", "90"]\nopening_ar'))
    assert e.value.problems == ("collections.curve: settlement curve sums to 90%, must be 100%",)
    with pytest.raises(PlanError, match=r"^scenarios\.slow_collections\.collections\.curve: settlement curve sums to 110%"):
        plan_with(('collections.curve = ["0", "50", "50"]', 'collections.curve = ["0", "60", "50"]'))


@pytest.mark.parametrize("override, message", [
    ('hires.H2.start = "2027-06"', "scenarios.delayed_hire.hires.H2: unknown key 'start'"),
    ('hires.H9.start_month = "2027-06"', "scenarios.delayed_hire.hires.H9: no hires entry with id 'H9'"),
    ('headcount.H2.start_month = "2027-06"', "scenarios.delayed_hire: unknown key 'headcount'"),
    ('opening.cash = "1"', "scenarios.delayed_hire: unknown key 'opening'"),
    ('hires.H2.start_month = "June"', "scenarios.delayed_hire.hires.H2.start_month: expected a month like 2027-01"),
])
def test_bad_override_is_an_error(override, message):
    with pytest.raises(PlanError) as e:
        plan_with(('hires.H2.start_month = "2027-06"', override))
    assert len(e.value.problems) == 1 and e.value.problems[0].startswith(message)


def test_unknown_keys_are_errors():
    with pytest.raises(PlanError) as e:
        plan_with(('[tax]\nrate_pct = "17"', '[tax]\nrate_pct = "17"\nrate = "17"'), ('[[opex]]', '[[opexx]]'))
    assert set(e.value.problems) == {"tax: unknown key 'rate'", "unknown section [opexx]"}


def test_unknown_scenario_is_an_error(plan):
    with pytest.raises(PlanError, match="unknown scenario 'fast' \\(scenarios: slow_collections, delayed_hire\\)"):
        planning.run(plan, "fast")


def test_scenario_does_not_mutate_the_base(plan, base):
    before = (plan.hires, plan.collections)
    planning.run(plan, "delayed_hire")
    planning.run(plan, "slow_collections")
    assert (plan.hires, plan.collections) == before
    assert plan.hires[1].start_month == Month(2027, 4)
    assert planning.run(plan) == base


def test_every_statement_line_drills_to_a_schedule(base):
    keys = {s.key for s in base.schedules}
    for statement in base.statements:
        for line in statement.lines:
            assert line.total or line.schedule in keys or line.label in ("Net income", "Opening cash"), line.label
    assert base.schedule("revenue").basis.startswith("units = base units")


def test_scenario_changes_name_each_override_against_base(plan):
    from finance_close_toolkit.plan_config import scenario_changes as changes

    def scenario_changes(p, name):
        return tuple(map(str, changes(p, name)))

    assert scenario_changes(plan, "slow_collections") == ("collections.curve: 0/100 -> 0/50/50",)
    assert scenario_changes(plan, "delayed_hire") == ("hires.H2.start_month: 2027-04 -> 2027-06",)
    assert scenario_changes(plan, None) == scenario_changes(plan, "base") == ()
    assert scenario_changes(load_plan(MIX), "mix_shift") == ("products.A.mix_pct: 80 -> 60", "products.B.mix_pct: 20 -> 40")
    edited = plan_with(('collections.curve = ["0", "50", "50"]',
                        'collections.opening_ar_schedule = ["50", "50"]\ntax.rate_pct = "20"'))
    assert scenario_changes(edited, "slow_collections") == ("collections.opening_ar_schedule: 100 -> 50/50",
                                                            "tax.rate_pct: 17 -> 20")
    assert planning.run(plan, "delayed_hire").changes == changes(plan, "delayed_hire")  # against base, not itself


def test_compare_slow_collections(plan, base):
    c = planning.compare(base, planning.run(plan, "slow_collections"))
    rows = {r.label: r for r in c.rows}
    assert list(rows) == ["Revenue", "EBITDA", "Net income", "Closing cash", "AR"]
    assert rows["Revenue"].change.for_year(Y1) == 0 and rows["EBITDA"].change.for_year(Y1) == 0
    assert rows["Closing cash"].change.for_year(Y1) == D(-12000)
    assert rows["AR"].change.for_year(Y1) == D(12000)


def seasonality(*values: str) -> tuple[str, str]:
    return ('seasonality = ["1", "1", "1", "1", "1", "1", "1", "1", "1", "1", "1", "1"]',
            "seasonality = [" + ", ".join(f'"{v}"' for v in values) + "]")


def pbt_and_tax(m):
    return m.pnl.line("PBT"), m.schedule("tax").line("Tax expense")


def at_17pct(x: D) -> D:
    return (D("0.17") * x).quantize(D("0.01"))


def test_tax_on_a_year_mixing_profit_and_loss_months_is_rate_times_annual_pbt():
    # April at 10% volume: PBT (10,350), so the year-to-date profit falls from 13,850 to 3,500
    m = planning.run(plan_with(seasonality("1", "1", "1", "0.1", *["1"] * 8)))
    pbt, tax = pbt_and_tax(m)
    assert pbt.values[3] == D(-10350) and pbt.for_year(Y1) > 0
    assert tax.for_year(Y1) == at_17pct(pbt.for_year(Y1))
    assert tax.values[3] == D("595.00") - D("2354.50")  # 17% of 3,500 less the 2,354.50 booked Jan-Mar
    assert m.pnl.line("Tax (17% of year-to-date profit)").values[3] == D("1759.50")  # a credit in April
    assert m.passed


def test_a_loss_year_books_no_tax_and_says_there_is_no_loss_relief():
    m = planning.run(plan_with(seasonality("1", "1", *["0.1"] * 10)))
    pbt, tax = pbt_and_tax(m)
    assert pbt.values[0] > 0 and tax.values[0] > 0 and pbt.for_year(Y1) < 0
    assert tax.for_year(Y1) == 0
    assert m.warnings[-1].startswith("Tax: FY2027 has a loss before tax of (")
    assert m.warnings[-1].endswith("No loss relief across fiscal years.")
    assert m.passed


def test_tax_payable_rolls_and_ties_when_tax_reverses():
    m = planning.run(plan_with(seasonality("1", "1", "1", "0.1", "0.1", *["1"] * 7)))
    tax = m.schedule("tax")
    expense, paid = tax.line("Tax expense").values, tax.line("Tax paid").values
    opening, closing = tax.line("Opening tax payable").values, tax.line("Closing tax payable").values
    assert any(x < 0 for x in expense)
    assert all(c == o + e - p for o, e, p, c in zip(opening, expense, paid, closing))
    assert closing[-1] == sum(expense, D(0)) - sum(paid, D(0))
    assert m.balance_sheet.line("Tax payable").values == closing
    assert m.passed


JULY_START = (('first_month = "2027-01"', 'first_month = "2027-07"'),
              ('spend_month = "2027-03"', 'spend_month = "2027-08"'),
              ('in_service_month = "2027-05"', 'in_service_month = "2027-09"'),
              ('{ month = "2027-02", amount = "50000" }', '{ month = "2027-08", amount = "50000" }'),
              ('{ month = "2027-11", amount = "50000" }', '{ month = "2028-05", amount = "50000" }'))


def test_losses_do_not_carry_into_the_next_fiscal_year():
    # Jul-27 to Jun-28 with calendar fiscal years: losses in FY2027 at 10% volume, profit in FY2028; FY2028 is
    # taxed on its own profit alone
    m = planning.run(plan_with(
        ('first_month = "2027-01"', 'first_month = "2027-07"\nfiscal_year_start = 1'),
        seasonality(*["0.1"] * 6, *["1"] * 6),
        ('spend_month = "2027-03"', 'spend_month = "2027-08"'),
        ('in_service_month = "2027-05"', 'in_service_month = "2027-09"'),
        ('{ month = "2027-02", amount = "50000" }', '{ month = "2027-08", amount = "50000" }'),
        ('{ month = "2027-11", amount = "50000" }', '{ month = "2028-05", amount = "50000" }')))
    pbt, tax = pbt_and_tax(m)
    assert sum(pbt.values[:6], D(0)) < 0 and sum(tax.values[:6], D(0)) == 0
    assert tax.values[6] == at_17pct(pbt.values[6]) > 0
    assert sum(tax.values[6:], D(0)) == at_17pct(sum(pbt.values[6:], D(0)))
    assert [fy.label for fy in m.years] == ["FY2027", "FY2028"]
    assert m.warnings[-1].startswith("Tax: FY2027 has a loss before tax of (")
    assert m.passed


def test_the_fiscal_year_defaults_to_the_first_month_so_losses_offset_within_it():
    # The same Jul-27 to Jun-28 plan with no fiscal_year_start: one fiscal year, FY2027, so the 2028 profit
    # months are offset by the 2027 losses and the year, a loss overall, books no tax
    m = planning.run(plan_with(*JULY_START, seasonality(*["0.1"] * 6, *["1"] * 6)))
    pbt, tax = pbt_and_tax(m)
    [fy] = m.years
    assert (fy.label, fy.months[0].label, fy.months[-1].label) == ("FY2027", "Jul-27", "Jun-28")
    assert pbt.values[6] > 0 and sum(pbt.values, D(0)) < 0
    assert set(tax.values) == {D(0)}
    assert [w for w in m.warnings if w.startswith("Tax: ")] == [
        f"Tax: FY2027 has a loss before tax of {fmt_money(sum(pbt.values, D(0)))}; no tax is booked for it. "
        "No loss relief across fiscal years."]


def test_month_arithmetic():
    m = Month.parse("2027-11")
    assert str(m.plus(2)) == "2028-01" and m.plus(2).since(m) == 2 and m.label == "Nov-27"
    assert [Month(2027, x).starts_quarter for x in (1, 2, 4, 12)] == [True, False, True, False]
    with pytest.raises(ValueError):
        Month.parse("2027-13")
