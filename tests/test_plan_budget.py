"""`fct plan to-budget`: a plan month as a budget submission on the regional template, against
docs/fixture-expected.md "Plan as budget", ingested like any budget file; and its refusals."""

import shutil
from datetime import datetime
from decimal import Decimal as D

import pytest
from conftest import REPO
from openpyxl import load_workbook

from finance_close_toolkit import planning, validation
from finance_close_toolkit.cli import main
from finance_close_toolkit.config import load_workspace
from finance_close_toolkit.kpi import KPI, kpi_value
from finance_close_toolkit.plan_budget import budget_submission
from finance_close_toolkit.plan_config import PlanError, load_plan, parse_plan
from finance_close_toolkit.store import Store
from finance_close_toolkit.templates import TEMPLATES

PLAN36 = REPO / "plans" / "agency_2027_36m.toml"
PLAN36_TEXT = PLAN36.read_text(encoding="utf-8")
CLOSE = REPO / "config" / "close.toml"


def to_budget(capsys, tmp_path, period: str, plan=PLAN36, code: int = 0, *extra: str) -> tuple[str, str]:
    assert main(["plan", "to-budget", str(plan), "--period", period, "--out", str(tmp_path / "out"),
                 "--config", str(CLOSE), *extra]) == code
    out = capsys.readouterr()
    return out.out, out.err


def submission_rows(path) -> list[tuple]:
    sheet = load_workbook(path)["Submission"]
    return [tuple(c.value for c in r) for r in sheet.iter_rows(min_row=1)]


def test_march_2027_as_sg_budget(capsys, tmp_path):
    out, _ = to_budget(capsys, tmp_path, "2027-03")
    version = load_plan(PLAN36).version
    path = tmp_path / "out" / f"budget_SG_2027-03_agency_2027_36m_{version}_base.xlsx"
    assert out.splitlines()[0] == f"Wrote {path}"
    assert submission_rows(path) == [
        TEMPLATES["submission"].names,
        ("2027-03", "REV", "Revenue", 24000), ("2027-03", "COGS", "COGS", 10000), ("2027-03", "PAY", "Payroll", 3300),
        ("2027-03", "RENT", "Office rent", 1000), ("2027-03", "DEP", "Depreciation", 5000)]
    assert "Left out: Interest (250.00), Tax (756.50). Interest and tax are below EBIT and are not part of the " \
           "regional P&L" in out
    assert "Amounts as on the P&L, revenue and costs both positive (the regional template's sign)." in out
    assert load_workbook(path).sheetnames == ["Submission", "Instructions"]
    notes = [tuple(c.value for c in r) for r in load_workbook(path)["Instructions"].iter_rows(max_row=8)]
    assert [n[:2] for n in notes[:7]] == [
        ("Source", None), ("plan", "agency_2027_36m"), ("plan version", version), ("scenario", "base"),
        ("market", "SG"), ("period", "2027-03"), ("currency", "SGD")]


def test_january_2029_as_sg_budget(capsys, tmp_path):
    to_budget(capsys, tmp_path, "2029-01")
    [path] = (tmp_path / "out").iterdir()
    assert path.name.startswith("budget_SG_2029-01_agency_2027_36m_") and path.name.endswith("_base.xlsx")
    assert submission_rows(path)[1:] == [
        ("2029-01", "REV", "Revenue", 36000), ("2029-01", "COGS", "COGS", 15000), ("2029-01", "PAY", "Payroll", 5500),
        ("2029-01", "RENT", "Office rent", 1000), ("2029-01", "DEP", "Depreciation", 500)]


def test_a_scenario_is_named_in_the_file(capsys, tmp_path):
    out, _ = to_budget(capsys, tmp_path, "2027-04", PLAN36, 0, "--scenario", "delayed_hire")
    [path] = (tmp_path / "out").iterdir()
    assert path.name.endswith("_delayed_hire.xlsx")
    assert ("2027-04", "PAY", "Payroll", 3300) in submission_rows(path)  # H2 starts in June, not April


def test_the_file_ingests_as_sg_budget_through_the_regional_template_mapping(tmp_path):
    shutil.copytree(REPO / "config", tmp_path / "config")
    shutil.copytree(REPO / "fx", tmp_path / "fx")
    config = tmp_path / "config" / "close.toml"
    text = config.read_text(encoding="utf-8")
    config.write_text(text.replace('mapping = "mappings/sg.toml"', 'mapping = "mappings/_regional_template.toml"'),
                      encoding="utf-8")
    ws = load_workspace(config)
    sub = budget_submission(planning.run(load_plan(PLAN36)), "2027-03", ws)
    path = tmp_path / sub.file_name
    path.write_bytes(sub.xlsx_bytes(ws.coa))
    with Store.open(tmp_path / "store.duckdb") as store:
        r = validation.ingest(store, ws, path, "SG", "2027-03", "budget", datetime(2027, 2, 20, 9))
    assert r.validation.state == "validated" and r.validation.exceptions == ()
    amounts: dict[str, D] = {}
    for f in r.validation.facts:
        amounts[f.account] = amounts.get(f.account, D(0)) + f.amount
    kpis = {k: kpi_value(amounts, ws.coa, KPI[k]) for k in ("revenue", "gross_margin", "ebitda")}
    assert kpis == {"revenue": D(24000), "gross_margin": D(14000), "ebitda": D(9700)}


def test_a_plan_without_budget_says_what_to_add(capsys, tmp_path):
    _, err = to_budget(capsys, tmp_path, "2027-03", REPO / "plans" / "agency_2027.toml", 2)
    assert err.startswith("error: plan agency_2027 has no [budget] section, so it cannot be written as a budget "
                          "submission. Add one naming the market and the regional account for each P&L line above "
                          "EBIT, for example:\n[budget]\n")
    assert '"opex.RENT" = "<regional account>"  # Office rent' in err
    assert not (tmp_path / "out").exists()


def edited(*edits: tuple[str, str]) -> planning.Model:
    text = PLAN36_TEXT
    for old, new in edits:
        assert old in text, old
        text = text.replace(old, new)
    return planning.run(parse_plan(text, "edited", "test"))


WS = load_workspace(CLOSE)


@pytest.mark.parametrize("model, period, message", [
    (lambda: edited(('"opex.RENT" = "RENT"\n', '')), "2027-03",
     "every P&L line above EBIT needs a regional account in [budget.accounts]; unmapped: opex.RENT (Office rent)"),
    (lambda: edited(('[[opex]]\nid = "RENT"', '[[opex]]\nid = "TRAVEL"\nname = "Travel"\nmonthly_amount = "100"\n'
                     'start_month = "2027-01"\n\n[[opex]]\nid = "RENT"')), "2027-03",
     "every P&L line above EBIT needs a regional account in [budget.accounts]; unmapped: opex.TRAVEL (Travel)"),
    (lambda: edited(('currency = "SGD"', 'currency = "AUD"')), "2027-03",
     "the plan is in AUD but SG reports in SGD; a budget submission must be in the market's currency"),
    (lambda: edited(), "2030-01", "period 2030-01 is outside the plan (2027-01 to 2029-12)"),
    (lambda: edited(), "2026-12", "period 2026-12 is outside the plan (2027-01 to 2029-12)"),
    (lambda: edited(), "March", "--period: expected a month like 2027-01, got 'March'"),
    (lambda: edited(('market = "SG"', 'market = "NZ"')), "2027-03",
     "budget.market: unknown market 'NZ'; configured: SG, AU"),
    (lambda: edited(('depreciation = "DEP"', 'depreciation = "AMORT"')), "2027-03",
     "[budget.accounts] uses accounts not in the chart of accounts: AMORT (chart: REV, COGS, PAY, RENT, MKT, OTH, DEP)"),
])
def test_refusals(model, period, message):
    with pytest.raises(PlanError) as e:
        budget_submission(model(), period, WS)
    assert e.value.problems == (message,)


@pytest.mark.parametrize("edit, message", [
    (('depreciation = "DEP"', 'depreciation = "DEP"\ninterest = "OTH"'),
     "budget.accounts.interest: interest is below EBIT and not part of the regional P&L; remove it"),
    (('depreciation = "DEP"', 'depreciation = "DEP"\n"opex.NOPE" = "OTH"'),
     "budget.accounts: unknown plan line 'opex.NOPE' (plan lines: revenue, cogs, payroll, opex.RENT, depreciation)"),
    (('market = "SG"\n', ''), "budget: missing market (a market code from close.toml)"),
])
def test_budget_section_is_validated_on_load(edit, message):
    with pytest.raises(PlanError) as e:
        parse_plan(PLAN36_TEXT.replace(*edit), "edited", "test")
    assert message in e.value.problems


def test_a_refusal_writes_nothing_and_exits_2(capsys, tmp_path):
    _, err = to_budget(capsys, tmp_path, "2031-01", PLAN36, 2)
    assert err == "error: period 2031-01 is outside the plan (2027-01 to 2029-12)\n"
    assert not (tmp_path / "out").exists()
