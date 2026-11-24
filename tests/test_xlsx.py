"""release.xlsx: the P&L sheet equals the report, facts and manifest are there, text never becomes a formula."""

from datetime import datetime
from decimal import Decimal as D

from conftest import PERIOD, run_story
from openpyxl import load_workbook

from finance_close_toolkit import release
from finance_close_toolkit.variance import release_report
from finance_close_toolkit.xlsx import AMOUNT, POINTS, export_xlsx


def close_to(stored: float, value: D) -> bool:
    """Excel keeps about 15 significant digits; ratios and percentages are stored to that precision."""
    return abs(D(str(stored)) - value) < D("1e-12")


def test_pnl_sheet_round_trips_the_report(close, tmp_path):
    run_story(close, "E13")
    report = release_report(close.store, close.ws, "2026-10-R2")
    wb = load_workbook(export_xlsx(close.store, close.ws, "2026-10-R2", tmp_path))
    assert wb.sheetnames == ["P&L vs budget", "Facts", "Manifest"]
    rows = list(wb["P&L vs budget"].iter_rows(min_row=2))
    assert [(r[0].value, r[1].value) for r in rows] == [(s, x.line.label) for s in ("APAC", "SG", "AU") for x in report.scope(s)]
    for cells, row in zip(rows, report.rows, strict=True):
        actual, budget, variance, pct, label = cells[2:]
        assert label.value == row.label
        assert all(c.data_type == "n" for c in (actual, budget, variance))
        if row.line.kind == "ratio":
            # Ratios are fractions under Excel's percent format; the variance is a number of points.
            assert close_to(actual.value, row.actual / 100) and close_to(budget.value, row.budget / 100)
            assert close_to(variance.value, row.variance)
            assert (actual.number_format, budget.number_format, variance.number_format) == ("0.00%", "0.00%", POINTS)
            assert pct.value is None
        else:
            assert [D(str(c.value)) for c in (actual, budget, variance)] == [row.actual, row.budget, row.variance]
            assert (actual.number_format, pct.number_format) == (AMOUNT, "0.00%")
            assert close_to(pct.value, row.variance_pct / 100)
    apac_ebitda = next(r for r in rows if (r[0].value, r[1].value) == ("APAC", "EBITDA"))
    assert [c.value for c in apac_ebitda[2:5]] == [208700, 191000, 17700]
    apac_revenue = next(r for r in rows if (r[0].value, r[1].value) == ("APAC", "Revenue"))
    assert round(apac_revenue[5].value, 6) == 0.015914  # 22,400 / 1,407,600, shown by Excel as 1.59%


def test_facts_and_manifest_sheets(close, tmp_path):
    run_story(close, "E13")
    wb = load_workbook(export_xlsx(close.store, close.ws, "2026-10-R2", tmp_path))
    facts = list(wb["Facts"].iter_rows(values_only=True))
    assert facts[0] == release.FACT_COLUMNS
    assert len(facts) - 1 == len(close.store.release_facts("2026-10-R2"))
    au_oth = [r for r in facts[1:] if r[1:4] == ("AU", "actual", "OTH")]
    assert sum(D(str(r[7])) for r in au_oth) == D("-21500")
    manifest = dict(wb["Manifest"].iter_rows(min_row=2, values_only=True))
    assert manifest["release_id"] == "2026-10-R2"
    assert manifest["previous_release_id"] == "2026-10-R1"
    assert manifest["kpi_totals.actual.APAC.ebitda"] == "208700"


def test_formula_like_text_stays_text(close, tmp_path):
    run_story(close, "E5")
    release.publish(close.store, close.ws, PERIOD, approved_by="=HYPERLINK(\"http://x\",\"ok\")",
                    now=datetime(2026, 11, 20, 9, 0))
    wb = load_workbook(export_xlsx(close.store, close.ws, "2026-10-R1", tmp_path))
    cell = next(r[1] for r in wb["Manifest"].iter_rows(min_row=2) if r[0].value == "approved_by")
    assert cell.data_type == "s"
    assert cell.value == "'=HYPERLINK(\"http://x\",\"ok\")"
    assert not [c for ws in wb for row in ws.iter_rows() for c in row if c.data_type == "f"]
