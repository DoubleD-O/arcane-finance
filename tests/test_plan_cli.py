"""`fct plan` check, run, compare and export: printed figures, exit codes and the values-only workbook."""

from decimal import Decimal as D
from pathlib import Path

from conftest import REPO
from openpyxl import load_workbook

from finance_close_toolkit.cli import main
from finance_close_toolkit.xlsx import AMOUNT, QUANTITY

AGENCY = str(REPO / "plans" / "agency_2027.toml")
MIX = str(REPO / "plans" / "mix_check.toml")


def fct(capsys, *args: str, code: int = 0) -> str:
    assert main(list(args)) == code, args
    return capsys.readouterr().out


def row(out: str, label: str) -> list[str]:
    line = next(x for x in out.splitlines() if x.startswith(label + "  "))
    return line[len(label):].split()


def test_run_prints_statements_annual_totals_and_checks(capsys):
    out = fct(capsys, "plan", "run", AGENCY)
    assert row(out, "Net income")[-1] == "25,190.50"
    assert row(out, "COGS")[0] == "(10,000.00)"
    assert row(out, "Total liabilities and equity")[-1] == "387,125.50"
    assert row(out, "Closing cash")[-1] == "295,125.50"
    assert "Balance sheet (SGD, month-end; FY2027 = Dec-27)" in out
    assert "  pass    Balance sheet balances in every month (assets = liabilities + equity): 12 of 12 months" in out
    assert "  Lowest month-end cash: 226,700.00 (Jan-27)." in out


def test_run_a_scenario(capsys):
    out = fct(capsys, "plan", "run", AGENCY, "--scenario", "delayed_hire")
    assert "scenario delayed_hire" in out and row(out, "EBITDA")[-1] == "101,000.00"
    assert "Changes against base: hires.H2.start_month: 2027-04 -> 2027-06" in out.splitlines()[1]


def test_run_with_a_failing_check_exits_1(capsys, tmp_path):
    bad = tmp_path / "bad.toml"
    bad.write_text(open(AGENCY, encoding="utf-8").read().replace('cash = "200000"', 'cash = "201000"'), encoding="utf-8")
    out = fct(capsys, "plan", "run", str(bad), code=1)
    assert "  FAIL    Opening balance sheet balances: assets 381,000.00, liabilities and equity 380,000.00: out by 1,000.00" in out


def test_compare(capsys):
    out = fct(capsys, "plan", "compare", AGENCY, "--scenario", "slow_collections")
    lines = out.splitlines()
    cash = lines.index(next(x for x in lines if x.startswith("Closing cash")))
    assert lines[cash + 2].split()[0] == "change" and lines[cash + 2].split()[2] == "(12,000.00)"
    assert "Lowest month-end cash: base 226,700.00 (Jan-27); slow_collections 226,700.00 (Jan-27)" in out
    assert "Checks: all pass in both" in out
    assert "Changes against base: collections.curve: 0/100 -> 0/50/50" in out.splitlines()[1]


def test_compare_pnl_only_does_not_claim_checks_pass(capsys):
    out = fct(capsys, "plan", "compare", MIX, "--scenario", "mix_shift")
    assert "Checks: balance sheet checks not run (P&L-only)" in out
    assert "all pass" not in out


def test_run_warns_of_a_cash_shortfall_in_plain_words(capsys, tmp_path):
    short = tmp_path / "short.toml"
    short.write_text(open(AGENCY, encoding="utf-8").read().replace('cash = "200000"', 'cash = "-90000"')
                     .replace('share_capital = "300000"', 'share_capital = "10000"'), encoding="utf-8")
    out = fct(capsys, "plan", "run", str(short))
    assert ("  CASH_SHORTFALL: Cash goes below zero from Jan-27 to Apr-27 and in Nov-27; lowest (63,300.00) in "
            "Jan-27.") in out


def test_check_lists_missing_inputs_and_problems(capsys, tmp_path):
    assert "Missing inputs: opening, collections, payments. The plan runs P&L only." in fct(capsys, "plan", "check", MIX)
    assert "Balance sheet inputs complete" in fct(capsys, "plan", "check", AGENCY)
    bad = tmp_path / "bad.toml"
    bad.write_text(open(MIX, encoding="utf-8").read().replace('mix_pct = "20"', 'mix_pct = "30"')
                   .replace('products.B.mix_pct', 'products.B.mix'), encoding="utf-8")
    out = fct(capsys, "plan", "check", str(bad), code=1)
    assert out.splitlines() == [f"{bad}: 1 problem", "  scenarios.mix_shift.products.B: unknown key 'mix'"]


def test_unknown_scenario_is_a_clean_error(capsys):
    assert main(["plan", "run", AGENCY, "--scenario", "nope"]) == 2
    assert "error: unknown scenario 'nope'" in capsys.readouterr().err


def test_pnl_only_run(capsys):
    out = fct(capsys, "plan", "run", MIX, "--scenario", "mix_shift")
    assert "P&L only: the opening balance sheet ([opening]) is missing" in out
    assert row(out, "Revenue")[0] == "14,000.00" and "Balance sheet (" not in out
    assert "  not run Opening balance sheet balances: not run: P&L only" in out


def test_export_is_values_only(capsys, tmp_path):
    out = fct(capsys, "plan", "export", AGENCY, "--out", str(tmp_path), "--scenario", "slow_collections")
    path = tmp_path / "plan_agency_2027_slow_collections.xlsx"
    assert out.strip() == f"Wrote {path}"
    wb = load_workbook(path)
    assert wb.sheetnames == ["Assumptions", "P&L", "Balance sheet", "Cash flow", "Schedules", "Checks"]
    cells = [c for ws in wb for r in ws.iter_rows() for c in r]
    assert not any(c.data_type == "f" for c in cells)
    bs = {r[0].value: r for r in wb["Balance sheet"].iter_rows(min_row=2)}
    assert D(str(bs["Accounts receivable"][12].value)) == D(36000)
    assert D(str(bs["Cash"][13].value)) == D("283125.5") and bs["Cash"][13].number_format == AMOUNT
    assumptions = [tuple(c.value for c in r) for r in wb["Assumptions"].iter_rows(min_row=2)]
    assert ("collections", None, "curve", "0; 50; 50") in assumptions
    assert ("plan", None, "scenario", "slow_collections") in assumptions
    checks = [r[1].value for r in wb["Checks"].iter_rows(min_row=2) if r[0].value != "Warning"]
    assert checks == ["pass"] * 5
    assert ("scenario change", None, "collections.curve", "0/100 -> 0/50/50") in assumptions
    pnl = wb["P&L"]
    assert [c.value for c in pnl[1]][:3] == ["Line", "Jan-27", "Feb-27"] and pnl[1][13].value == "FY2027"
    units = next(r for r in wb["Schedules"].iter_rows(min_row=2) if r[0].value == "Total units")
    assert units[1].value == 2000 and units[1].number_format == QUANTITY


def test_export_protects_formula_like_text(capsys, tmp_path):
    plan = tmp_path / "eq.toml"
    plan.write_text(open(AGENCY, encoding="utf-8").read().replace('"Synthetic Agency SG (demo)"', '"=HYPERLINK(1)"'),
                    encoding="utf-8")
    fct(capsys, "plan", "export", str(plan), "--out", str(tmp_path))
    ws = load_workbook(tmp_path / "plan_eq_base.xlsx")["Assumptions"]
    entity = next(r[3] for r in ws.iter_rows(min_row=2) if r[2].value == "entity")
    assert entity.data_type == "s" and entity.value == "'=HYPERLINK(1)"


PLAN36 = str(REPO / "plans" / "agency_2027_36m.toml")


def test_run_36_months_prints_year_1_by_month_then_the_fiscal_years(capsys):
    out = fct(capsys, "plan", "run", PLAN36)
    header = next(x for x in out.splitlines() if x.startswith(" ") and "Jan-27" in x).split()
    assert header == [*(f"{m}-27" for m in ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct",
                                            "Nov", "Dec")), "FY2027", "FY2028", "FY2029"]
    assert row(out, "Net income")[-3:] == ["25,190.50", "54,780.00", "142,760.00"]
    assert row(out, "Retained earnings brought forward")[-3:] == ["50,000.00", "75,190.50", "129,970.50"]
    assert "Balance sheet (SGD, month-end; FY2027 = Dec-27, FY2028 = Dec-28, FY2029 = Dec-29)" in out
    assert "By month: Jan-27 to Dec-27 (--months all prints every month); then one column per fiscal year." in out
    assert "36 of 36 months tie to the cent" in out


def test_run_months_all_prints_every_month(capsys):
    out = fct(capsys, "plan", "run", PLAN36, "--months", "all")
    header = next(x for x in out.splitlines() if x.startswith(" ") and "Jan-27" in x).split()
    assert len(header) == 39 and header[35] == "Dec-29" and header[36:] == ["FY2027", "FY2028", "FY2029"]
    assert row(out, "Cash")[35] == "560,125.50"
    assert "By month:" not in out


def test_compare_36_months(capsys):
    out = fct(capsys, "plan", "compare", PLAN36, "--scenario", "slow_collections")
    lines = out.splitlines()
    ar = lines.index(next(x for x in lines if x.startswith("AR ")))
    assert lines[ar + 2].split()[-3:] == ["12,000.00", "15,000.00", "18,000.00"]  # half of each December's revenue
    assert "By fiscal year: FY2027 base 226,700.00 (Jan-27), slow_collections 226,700.00 (Jan-27);" in out
    assert "Checks: all pass in both" in out


def test_export_36_months_has_every_month_and_the_fiscal_years(capsys, tmp_path):
    fct(capsys, "plan", "export", PLAN36, "--out", str(tmp_path))
    wb = load_workbook(tmp_path / "plan_agency_2027_36m_base.xlsx")
    header = [c.value for c in wb["Balance sheet"][1]]
    assert header[36] == "Dec-29" and header[37:] == ["FY2027", "FY2028", "FY2029", "Schedule"]
    bs = {r[0].value: [c.value for c in r] for r in wb["Balance sheet"].iter_rows(min_row=2)}
    assert [D(str(v)) for v in bs["Profit for the year"][37:40]] == [D("25190.5"), D("54780"), D("142760")]
    assumptions = [tuple(c.value for c in r) for r in wb["Assumptions"].iter_rows(min_row=2)]
    assert ("fiscal year", None, "FY2028", "Jan-28 to Dec-28") in assumptions
    assert ("volume", None, "annual_units", "2028 30000; 2029 36000") in assumptions
    assert ("budget", None, "accounts.opex.RENT", "RENT") in assumptions


def test_export_names_part_years_in_the_headers_and_the_checks(capsys, tmp_path):
    april = tmp_path / "april.toml"
    april.write_text(Path(PLAN36).read_text(encoding="utf-8")
                     .replace("months = 36", "months = 36\nfiscal_year_start = 4")
                     .replace('annual_units = { "2028" = "30000", "2029" = "36000" }',
                              'annual_units = { "2027" = "30000" }'),
                     encoding="utf-8")
    part = "Part years: FY2026 has 3 months (Jan-27 to Mar-27); FY2029 has 9 months (Apr-29 to Dec-29)."
    assert part in fct(capsys, "plan", "run", str(april))
    fct(capsys, "plan", "export", str(april), "--out", str(tmp_path))
    wb = load_workbook(next(tmp_path.glob("plan_*.xlsx")))
    assert [c.value for c in wb["P&L"][1]][37:] == ["FY2026 (3 mo)", "FY2027", "FY2028", "FY2029 (9 mo)", "Schedule"]
    assert ("Fiscal years", None, part) in [tuple(c.value for c in r) for r in wb["Checks"].iter_rows(min_row=2)]


def test_export_seasonality_index_has_no_annual_total(capsys, tmp_path):
    fct(capsys, "plan", "export", PLAN36, "--out", str(tmp_path))
    sheet = load_workbook(tmp_path / "plan_agency_2027_36m_base.xlsx")["Schedules"]
    [index] = [[c.value for c in r] for r in sheet.iter_rows(min_row=2) if r[0].value == "Seasonality index"]
    assert index[1] == 1 and index[37:40] == [None, None, None]
