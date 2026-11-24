"""docs/fixture-expected.md, "Step 2": actual vs budget at the report level."""

from decimal import Decimal as D

import pytest
from conftest import run_story

from finance_close_toolkit.money import fmt_percent, fmt_pp
from finance_close_toolkit.release import ReleaseError
from finance_close_toolkit.variance import (
    PNL_LINES,
    TRACEABLE_LINES,
    Bridge,
    VarianceRow,
    proposed_report,
    release_report,
    trace_line,
)

# Copied from docs/fixture-expected.md (Step 2), unicode minus kept as written there.
EXPECTED = {
    "APAC": """
Revenue | 1,430,000 | 1,407,600 | 22,400 | 1.59% | Favourable
Direct costs | (673,600) | (663,200) | (10,400) | −1.57% | Unfavourable
Gross margin | 756,400 | 744,400 | 12,000 | 1.61% | Favourable
GM % | 52.90% | 52.88% | +0.01 pp | | Favourable
Staff costs | (353,200) | (361,200) | 8,000 | 2.21% | Favourable
Rent and facilities | (85,800) | (86,400) | 600 | 0.69% | Favourable
Marketing | (57,200) | (72,000) | 14,800 | 20.56% | Favourable
Other operating costs | (51,500) | (33,800) | (17,700) | −52.37% | Unfavourable
EBITDA | 208,700 | 191,000 | 17,700 | 9.27% | Favourable
EBITDA % | 14.59% | 13.57% | +1.03 pp | | Favourable
Depreciation | (26,880) | (27,040) | 160 | 0.59% | Favourable
Operating profit (EBIT) | 181,820 | 163,960 | 17,860 | 10.89% | Favourable
""",
    "SG": """
Revenue | 1,000,000 | 950,000 | 50,000 | 5.26% | Favourable
Direct costs | (450,000) | (430,000) | (20,000) | −4.65% | Unfavourable
Gross margin | 550,000 | 520,000 | 30,000 | 5.77% | Favourable
GM % | 55.00% | 54.74% | +0.26 pp | | Favourable
Staff costs | (250,000) | (260,000) | 10,000 | 3.85% | Favourable
Rent and facilities | (60,000) | (60,000) | 0 | 0.00% | On plan
Marketing | (40,000) | (50,000) | 10,000 | 20.00% | Favourable
Other operating costs | (30,000) | (25,000) | (5,000) | −20.00% | Unfavourable
EBITDA | 170,000 | 125,000 | 45,000 | 36.00% | Favourable
EBITDA % | 17.00% | 13.16% | +3.84 pp | | Favourable
Depreciation | (20,000) | (20,000) | 0 | 0.00% | On plan
Operating profit (EBIT) | 150,000 | 105,000 | 45,000 | 42.86% | Favourable
""",
    "AU": """
Revenue | 430,000 | 457,600 | (27,600) | −6.03% | Unfavourable
Direct costs | (223,600) | (233,200) | 9,600 | 4.12% | Favourable
Gross margin | 206,400 | 224,400 | (18,000) | −8.02% | Unfavourable
GM % | 48.00% | 49.04% | −1.04 pp | | Unfavourable
Staff costs | (103,200) | (101,200) | (2,000) | −1.98% | Unfavourable
Rent and facilities | (25,800) | (26,400) | 600 | 2.27% | Favourable
Marketing | (17,200) | (22,000) | 4,800 | 21.82% | Favourable
Other operating costs | (21,500) | (8,800) | (12,700) | −144.32% | Unfavourable
EBITDA | 38,700 | 66,000 | (27,300) | −41.36% | Unfavourable
EBITDA % | 9.00% | 14.42% | −5.42 pp | | Unfavourable
Depreciation | (6,880) | (7,040) | 160 | 2.27% | Favourable
Operating profit (EBIT) | 31,820 | 58,960 | (27,140) | −46.03% | Unfavourable
""",
}


def amount(text: str) -> D:
    t = text.replace(",", "")
    return -D(t[1:-1]) if t.startswith("(") else D(t)


def expected_rows(scope: str) -> list[list[str]]:
    return [[c.strip().replace("−", "-") for c in line.split("|")] for line in EXPECTED[scope].strip().splitlines()]


@pytest.fixture
def r2(close):
    run_story(close, "E13")
    return release_report(close.store, close.ws, "2026-10-R2")


@pytest.mark.parametrize("scope", ["APAC", "SG", "AU"])
def test_r2_pnl_vs_budget(r2, scope):
    rows = r2.scope(scope)
    expected = expected_rows(scope)
    assert [r.line.label for r in rows] == [e[0] for e in expected]
    for row, (label, actual, budget, variance, pct, fav) in zip(rows, expected):
        where = f"{scope} {label}"
        if row.line.kind == "ratio":
            assert fmt_percent(row.actual) == actual, where
            assert fmt_percent(row.budget) == budget, where
            assert fmt_pp(row.variance) == variance, where
            assert row.variance_pct is None and pct == "", where
        else:
            assert (row.actual, row.budget, row.variance) == (amount(actual), amount(budget), amount(variance)), where
            assert fmt_percent(row.variance_pct) == pct, where
        assert row.label == fav, where


def test_ratio_variance_uses_unrounded_ratios(r2):
    # 14.59% - 13.57% would print +1.02 pp; the unrounded ratios give +1.03 pp.
    row = r2.row("APAC", "ebitda_pct")
    assert row.variance == D("208700") / D("1430000") * 100 - D("191000") / D("1407600") * 100


def test_r2_bridge_sums_exactly(r2):
    b = r2.bridge
    assert b.budget == D("191000") and b.actual == D("208700")
    assert [(s.market, s.amount) for s in b.steps] == [("SG", D("45000")), ("AU", D("-27300"))]
    assert b.budget + sum(s.amount for s in b.steps) == b.actual
    assert [(s.before, s.after) for s in b.steps] == [(D("191000"), D("236000")), (D("236000"), D("208700"))]


def test_r1_bridge_and_ebitda_variance(close):
    run_story(close, "E6")
    r1 = release_report(close.store, close.ws, "2026-10-R1")
    b = r1.bridge
    assert [(s.market, s.amount) for s in b.steps] == [("SG", D("45000")), ("AU", D("-14400"))]
    assert (b.budget, b.actual) == (D("191000"), D("221600"))
    ebitda = r1.row("APAC", "ebitda")
    assert ebitda.variance == D("30600")
    assert fmt_percent(ebitda.variance_pct) == "16.02%"
    assert ebitda.label == "Favourable"
    assert r1.previous_release_id is None and r1.changes is None


def test_bridge_that_does_not_close_is_refused():
    with pytest.raises(ValueError, match="does not close"):
        Bridge.build(D("100"), [("SG", D("10"))], D("111"))


def test_r2_metadata_and_changes_since_r1(r2):
    assert (r2.release_id, r2.status, r2.period, r2.reporting_currency) == ("2026-10-R2", "final", "2026-10", "SGD")
    assert r2.approved_by == "Regional FP&A lead"
    assert r2.rates["actual"]["AUD"] == D("0.86") and r2.rates["budget"]["AUD"] == D("0.88")
    assert r2.fx_notes() == ("AU translated at actual 0.86 / budget 0.88 SGD per AUD; variance includes FX.",)
    assert r2.fx_notes("SG") == ()
    assert r2.previous_release_id == "2026-10-R1"
    changed = {(k.scope, k.kpi.key): (k.change, k.material) for k in r2.changes.kpis if k.change}
    assert changed == {("AU", "ebitda"): (D("-12900"), True), ("APAC", "ebitda"): (D("-12900"), True)}


def test_r2_checksum_is_the_release_checksum(close, r2):
    assert r2.facts_checksum == close.store.release("2026-10-R2").facts_checksum


def test_proposed_after_b1_is_draft(close):
    run_story(close, "B1")
    p = proposed_report(close.store, close.ws, "2026-10")
    assert (p.release_id, p.status, p.name) == (None, "draft", "PROPOSED")
    assert p.row("APAC", "ebitda").actual == D("207700")
    assert p.previous_release_id == "2026-10-R2"
    changed = {(k.scope, k.kpi.key): (k.change, k.material) for k in p.changes.kpis if k.change}
    assert changed == {("SG", "ebitda"): (D("-1000"), False), ("APAC", "ebitda"): (D("-1000"), False)}


def test_proposed_is_refused_while_publish_would_be(close):
    run_story(close, "E7")
    with pytest.raises(ReleaseError, match="needs_review"):
        proposed_report(close.store, close.ws, "2026-10")


@pytest.mark.parametrize("actual, shown, label", [
    ("50.004", "0.00 pp", "On plan"),
    ("49.996", "0.00 pp", "On plan"),
    ("50.005", "+0.01 pp", "Favourable"),
    ("48.96", "-1.04 pp", "Unfavourable"),
])
def test_ratio_label_agrees_with_the_points_shown(actual, shown, label):
    gm_pct = next(line for line in PNL_LINES if line.key == "gm_pct")
    row = VarianceRow("APAC", gm_pct, D(actual), D("50"))
    assert (fmt_pp(row.variance), row.label) == (shown, label)


@pytest.mark.parametrize("scope", ["APAC", "SG", "AU"])
def test_traced_facts_add_up_to_every_report_line(close, r2, scope):
    for line in TRACEABLE_LINES:
        t = trace_line(close.store, "2026-10-R2", scope, line.key)
        row = r2.row(scope, line.key)
        assert (t.total("actual"), t.total("budget")) == (row.actual, row.budget), line.key


def test_trace_au_revenue_down_to_the_source_row(close, r2):
    t = trace_line(close.store, "2026-10-R1", "AU", "REV", "actual")
    [f] = t.facts
    assert (f.file_name, f.source_row, f.local_amount, f.rate, f.reporting_amount) == (
        "au_actual_v1.xlsx", 4, D("500000"), D("0.86"), D("430000"))


def test_ratio_lines_cannot_be_traced(close, r2):
    with pytest.raises(ReleaseError, match="cannot trace 'ebitda_pct'"):
        trace_line(close.store, "2026-10-R2", "AU", "ebitda_pct")


@pytest.mark.parametrize("kpi, line", [("revenue", "REV"), ("gross_margin", "gross_margin"), ("ebitda", "ebitda")])
def test_r2_bridge_per_kpi_sums_exactly_to_its_line(r2, kpi, line):
    b = r2.bridges[kpi]
    apac = r2.row("APAC", line)
    assert (b.budget, b.actual) == (apac.budget, apac.actual)
    assert [(s.market, s.amount) for s in b.steps] == [(m, r2.row(m, line).variance) for m in ("SG", "AU")]
    assert b.budget + sum(s.amount for s in b.steps) == b.actual
    assert all(prev.after == step.before for prev, step in zip(b.steps, b.steps[1:]))


def test_r2_revenue_bridge_figures(r2):
    b = r2.bridges["revenue"]
    assert (b.budget, b.actual) == (D("1407600"), D("1430000"))
    assert sum(s.amount for s in b.steps) == D("22400")


def test_a_kpi_bridge_that_does_not_close_names_the_kpi():
    with pytest.raises(ValueError, match="Gross margin bridge does not close"):
        Bridge.build(D("100"), [("SG", D("10"))], D("111"), "Gross margin")
