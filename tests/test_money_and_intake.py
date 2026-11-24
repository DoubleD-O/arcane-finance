from decimal import Decimal as D

import pytest
from openpyxl import Workbook

from finance_close_toolkit.intake import IntakeError, inspect
from finance_close_toolkit.money import fmt_amount, fmt_pct, parse_amount
from finance_close_toolkit.validation import tie_out


@pytest.mark.parametrize(
    "text, expected",
    [
        ("1,234.50", D("1234.50")),
        ("(1,234.50)", D("-1234.50")),
        ("-1234", D("-1234")),
        ("-", D("0")),
        ("", D("0")),
        ("   ", D("0")),
        ("1,234,567.89", D("1234567.89")),
        ("(1,234)", D("-1234")),
        ("-1,000", D("-1000")),
        ("1234.5", D("1234.5")),
    ],
)
def test_parse_amount_accepts(text, expected):
    assert parse_amount(text) == expected


@pytest.mark.parametrize("text", ["abc", "1.2.3", "NaN", "Infinity", "12abc", "1.234,50", "1,5", "12,34,56", "1,23", ",123", "1,234.5,0"])
def test_parse_amount_rejects(text):
    with pytest.raises(ValueError):
        parse_amount(text)


def test_display_rounds_half_up_and_marks_zero_base():
    assert fmt_amount(D("-12900")) == "-12,900.00"
    assert fmt_amount(D("0.005")) == "0.01"
    assert fmt_amount(D("-0.001")) == "0.00"
    assert fmt_pct(D("-5.8212996")) == "-5.82%"
    assert fmt_pct(None) == "n/m"


def test_bad_amount_and_formula_without_cached_value_need_review(close):
    wb = Workbook()
    ws = wb.active
    ws.title = "P&L"
    ws.append(["Synthetic AU entity"])
    ws.append(["Profit and loss, October 2026"])
    ws.append(["Account", "Account Name", "Amount AUD"])
    ws.append(["41000", "Sales", "(1,234.50)"])
    ws.append(["50000", "Cost of sales", "abc"])
    ws.append(["61000", "Salaries", "=1+1"])
    ws.append([None, None, None])
    ws.append(["62000", "Occupancy", "-"])
    wb.save(close.file("au_bad_amounts.xlsx"))

    v = close.ingest("au_bad_amounts.xlsx", "AU", "actual", "2026-11-07T15:00").validation
    assert v.state == "needs_review"
    assert [(e.code, e.row) for e in v.exceptions] == [("BAD_AMOUNT", 5), ("BAD_AMOUNT", 6)]
    assert {f.source_row: f.amount for f in v.facts} == {4: D("-1234.50"), 8: D("0")}


@pytest.mark.parametrize(
    "name, market, message",
    [
        ("sg_actual_v1.csv", "AU", "sg_actual_v1.csv is not an Excel workbook; the AU mapping expects xlsx"),
        ("au_budget.xlsx", "SG", "au_budget.xlsx is not a UTF-8 CSV file; the SG mapping expects csv"),
    ],
)
def test_file_of_the_wrong_format_is_refused_with_a_readable_error(close, name, market, message):
    with pytest.raises(IntakeError) as e:
        close.ingest(name, market, "actual", "2026-11-04T10:00")
    assert str(e.value) == message
    assert close.store.count("submissions") == 0


def test_preview_tie_out_matches_the_validation_after_ingest(close):
    path = close.file("au_actual_v2.xlsx")
    report = inspect(path, close.ws.mapping("AU"))
    preview = tie_out(report.table.rows, report.mapping)
    [consulting] = [r for r in report.table.rows if r.source_account == "64000"]
    assert preview.unmapped_total == consulting.amount != 0
    assert preview.source_total == preview.mapped_total + preview.unmapped_total
    assert close.ingest("au_actual_v2.xlsx", "AU", "actual", "2026-11-12T11:00").validation.tie_out == preview
