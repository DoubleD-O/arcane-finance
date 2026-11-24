"""docs/fixture-expected.md, "Reporting currency": a release shown in another currency than the base."""

import json
from dataclasses import replace
from decimal import Decimal as D

import pytest
from conftest import REPO, run_story
from openpyxl import load_workbook
from test_deck import assert_bridge_drawn_right, expected_row, footer, open_deck, tables, texts
from test_deck_style import BUILT_IN_R2, content_digest

from finance_close_toolkit import deck, release, xlsx
from finance_close_toolkit.cli import main
from finance_close_toolkit.commentary import load_commentary
from finance_close_toolkit.money import fmt_money, fmt_percent, fmt_step
from finance_close_toolkit.release import ReleaseError
from finance_close_toolkit.variance import BRIDGE_LINES, release_report

COMMENTARY = REPO / "examples" / "commentary" / "2026-10.toml"
LABEL = "USD, converted from SGD at actual 1.30 / budget 1.32 SGD per USD"
# Release 2026-10-R2's facts checksum; BUILT_IN_R2, which predates stored USD rates, prints it in every footer.
R2_CHECKSUM_PREFIX = "14293ec6e2df"


@pytest.fixture
def released(close):
    run_story(close, "E13")
    return close


def test_publish_stores_every_rate_in_the_rate_file(released):
    r2 = released.store.release("2026-10-R2")
    assert r2.rates == {"actual": {"SGD": D("1"), "AUD": D("0.86"), "USD": D("1.30")},
                        "budget": {"SGD": D("1"), "AUD": D("0.88"), "USD": D("1.32")}}
    stored = released.store._all("SELECT scenario, currency, rate FROM release_rates WHERE release_id = ?",
                                 ["2026-10-R2"])
    assert sorted((s, c, D(r).normalize()) for s, c, r in stored) == sorted(
        (sc, c, r.normalize()) for sc, t in r2.rates.items() for c, r in t.items())
    assert r2.facts_checksum.startswith(R2_CHECKSUM_PREFIX)
    assert all(f.currency in ("SGD", "AUD") for f in released.store.release_facts("2026-10-R2"))
    assert release.verify_release(released.store, "2026-10-R2").ok


def test_r2_in_usd_matches_the_hand_derived_numbers(released, capsys):
    r = release_report(released.store, released.ws, "2026-10-R2", "USD")
    assert r.currency == "USD" and r.currency_label == LABEL
    assert r.release_id == "2026-10-R2" and r.facts_checksum.startswith(R2_CHECKSUM_PREFIX)
    rev, ebitda = r.row("APAC", "REV"), r.row("APAC", "ebitda")
    # Full precision at report level: within the store scale of the exact quotient.
    assert abs(rev.actual - D(1430000) / D("1.30")) < D("1e-9")
    assert abs(rev.budget - D(1407600) / D("1.32")) < D("1e-9")
    assert abs(ebitda.actual - D(208700) / D("1.30")) < D("1e-9")
    assert abs(ebitda.budget - D(191000) / D("1.32")) < D("1e-9")
    cents = D("0.01")
    assert [rev.actual.quantize(cents), rev.budget.quantize(cents), rev.variance.quantize(cents)] == [
        D("1100000.00"), D("1066363.64"), D("33636.36")]
    assert [ebitda.actual.quantize(cents), ebitda.budget.quantize(cents), ebitda.variance.quantize(cents)] == [
        D("160538.46"), D("144696.97"), D("15841.49")]
    assert fmt_percent(rev.variance_pct) == "3.15%" and fmt_percent(ebitda.variance_pct) == "10.95%"
    # Ratios recomputed from converted amounts.
    gm = r.row("APAC", "gm_pct")
    assert gm.actual == r.row("APAC", "gross_margin").actual / rev.actual * 100

    common = ["--store", str(released.root / "store.duckdb"), "--config", str(released.config_path)]
    assert main(["variance", "--release", "2026-10-R2", "--currency", "USD", *common]) == 0
    out = capsys.readouterr().out
    assert out.startswith("Release 2026-10-R2 (2026-10), final.")
    assert f"Amounts in {LABEL}." in out.splitlines()[0]
    assert "Revenue                  1,100,000.00  1,066,363.64    33,636.36    3.15%  Favourable" in out
    assert "EBITDA                     160,538.46    144,696.97    15,841.49   10.95%  Favourable" in out


def test_bridges_and_totals_sum_exactly_in_usd(released):
    r = release_report(released.store, released.ws, "2026-10-R2", "USD")
    for kpi, line in BRIDGE_LINES.items():
        b = r.bridges[kpi]
        assert b.budget + sum(s.amount for s in b.steps) == b.actual
        assert b.budget == r.row("APAC", line).budget and b.actual == r.row("APAC", line).actual
    for key in ("REV", "COGS", "PAY", "ebitda", "ebit"):
        for scenario in ("actual", "budget"):
            apac = getattr(r.row("APAC", key), scenario)
            assert apac == sum(getattr(r.row(m.code, key), scenario) for m in r.markets)
    assert r.row("APAC", "ebitda").actual == sum(r.row("APAC", k).actual for k in ("REV", "COGS", "PAY", "RENT",
                                                                                     "MKT", "OTH"))


def test_base_currency_outputs_are_unchanged(released):
    base = release_report(released.store, released.ws, "2026-10-R2")
    assert release_report(released.store, released.ws, "2026-10-R2", "SGD") == base
    assert base.conversion is None and base.currency_label == "SGD"
    assert content_digest(deck.render(base, load_commentary(COMMENTARY))) == BUILT_IN_R2


def test_a_currency_not_stored_is_refused_with_the_available_ones(released, capsys):
    with pytest.raises(ReleaseError, match=r"2026-10-R2 has no stored EUR rate; available: SGD, AUD, USD\."):
        release_report(released.store, released.ws, "2026-10-R2", "EUR")
    common = ["--store", str(released.root / "store.duckdb"), "--config", str(released.config_path)]
    for args in (["variance", "--release", "2026-10-R2"], ["deck", "--release", "2026-10-R2", "--out", "x"],
                 ["export", "2026-10-R2", "--out", "x"]):
        assert main([*args, "--currency", "EUR", *common]) == 2
        assert "available: SGD, AUD, USD" in capsys.readouterr().err


def test_a_release_published_without_the_usd_rate_refuses_usd(close):
    """A release published before every rate was stored holds only its markets' rates."""
    fx = close.root / "fx" / "2026-10.toml"
    rate_file = fx.read_text(encoding="utf-8")
    fx.write_text("\n".join(line for line in rate_file.splitlines() if not line.startswith("USD")),
                  encoding="utf-8")
    run_story(close, "E6")
    fx.write_text(rate_file, encoding="utf-8")  # the current rate file quotes USD again
    assert "USD" not in close.store.release("2026-10-R1").rates["actual"]
    with pytest.raises(ReleaseError, match=r"2026-10-R1 has no stored USD rate; available: SGD, AUD\."):
        release_report(close.store, close.ws, "2026-10-R1", "USD")


def test_converted_deck_matches_the_converted_report(released, tmp_path):
    r = release_report(released.store, released.ws, "2026-10-R2", "USD")
    path = deck.write_deck(r, load_commentary(COMMENTARY), tmp_path)
    assert path.name == "Comex_2026-10_2026-10-R2_USD.pptx"
    slides = open_deck(path)
    for s in slides:
        assert footer(s) == f"2026-10-R2 · 2026-10 · {LABEL} · checksum {r.facts_checksum[:12]}"
    assert f"Currency: {LABEL}" in [line for t in texts(slides[0]) for line in t.splitlines()]

    [scorecard] = tables(slides[1])
    assert scorecard[1:] == [expected_row(r.row("APAC", k)) for k in ("REV", "gross_margin", "gm_pct", "ebitda",
                                                                       "ebitda_pct")]
    assert ["EBITDA", "160,538", "144,697", "15,841", "10.9%", "Favourable"] in scorecard
    [pnl] = tables(slides[2])
    assert pnl[1:] == [expected_row(row) for row in r.scope("APAC")]
    [markets] = tables(slides[3])
    for cells, scope in zip(markets[1:], ["SG", "AU", "APAC"], strict=True):
        assert cells == [scope, *(v for key in ("REV", "gross_margin", "ebitda")
                                  for v in (fmt_money(r.row(scope, key).actual, 0),
                                            fmt_money(r.row(scope, key).variance, 0)))]
    b = r.bridge
    [bridge] = tables(slides[4])
    assert bridge[1] == [fmt_money(b.budget, 0), *(fmt_step(s.amount, 0) for s in b.steps), fmt_money(b.actual, 0)]
    assert_bridge_drawn_right(r, next(s for s in slides[4].shapes if s.has_chart).chart)
    [changes] = tables(slides[5])
    assert changes[1:] == [
        [k.scenario, k.scope, k.kpi.label, fmt_money(k.before, 0), fmt_money(k.after, 0), fmt_money(k.change, 0),
         fmt_percent(k.pct, 1), "Material" if k.material else "Below threshold"]
        for k in r.changes.kpis
    ]


def test_converted_changes_keep_materiality_judged_in_the_base_currency(released):
    base = release_report(released.store, released.ws, "2026-10-R2")
    usd = release_report(released.store, released.ws, "2026-10-R2", "USD")
    assert [(k.scenario, k.scope, k.kpi.key, k.material) for k in usd.changes.kpis] == [
        (k.scenario, k.scope, k.kpi.key, k.material) for k in base.changes.kpis]
    au_rev = next(k for k in usd.changes.kpis if (k.scope, k.kpi.key) == ("AU", "revenue"))
    base_au_rev = next(k for k in base.changes.kpis if (k.scope, k.kpi.key) == ("AU", "revenue"))
    assert abs(au_rev.change - base_au_rev.change / D("1.30")) < D("1e-9")


def test_export_adds_converted_columns_and_says_so_in_the_manifest(released, capsys, tmp_path):
    common = ["--store", str(released.root / "store.duckdb"), "--config", str(released.config_path)]
    assert main(["export", "2026-10-R2", "--out", str(tmp_path), "--currency", "USD", *common]) == 0
    assert f"release.xlsx adds amounts in {LABEL}." in capsys.readouterr().out
    wb = load_workbook(tmp_path / "release.xlsx")
    pnl = list(wb[xlsx.PNL_SHEET].values)
    assert pnl[0] == ("Scope", "Line", "Actual (SGD)", "Budget (SGD)", "Variance (SGD)", "Variance % (SGD)",
                      "Label (SGD)", "Actual (USD)", "Budget (USD)", "Variance (USD)", "Variance % (USD)",
                      "Label (USD)")
    rows = {(row[0], row[1]): row for row in pnl[1:]}
    usd = release_report(released.store, released.ws, "2026-10-R2", "USD")
    rev = rows[("APAC", "Revenue")]
    assert rev[2:4] == (1430000, 1407600)
    assert [D(str(v)) for v in rev[7:10]] == pytest.approx(
        [usd.row("APAC", "REV").actual, usd.row("APAC", "REV").budget, usd.row("APAC", "REV").variance])
    assert rev[11] == "Favourable"
    manifest = dict(wb["Manifest"].values)
    assert manifest["presentation_currency"] == "USD"
    assert manifest["presentation_conversion"] == LABEL
    assert manifest["presentation_translation"] == TRANSLATION
    assert json.loads((tmp_path / "manifest.json").read_text(encoding="utf-8"))["facts_checksum"].startswith(
        R2_CHECKSUM_PREFIX)


def test_base_export_has_no_converted_columns(released, tmp_path):
    xlsx.export_xlsx(released.store, released.ws, "2026-10-R2", tmp_path)
    wb = load_workbook(tmp_path / "release.xlsx")
    assert next(wb[xlsx.PNL_SHEET].values) == xlsx.PNL_COLUMNS
    assert "presentation_currency" not in dict(wb["Manifest"].values)


TRANSLATION = ("Variance in USD includes the translation effect of actual 1.30 vs budget 1.32 SGD per USD; a line "
               "on plan in SGD can show a USD variance.")
AU_CONVERTED = "AU: local AUD translated to SGD at actual 0.86 / budget 0.88 SGD per AUD, then to USD as above."
AU_BASE = "AU translated at actual 0.86 / budget 0.88 SGD per AUD; variance includes FX."
CHANGES_NOTES = ("Materiality is judged in SGD, where the thresholds are set.",
                 "2026-10-R1 restated at 2026-10-R2's rates.")


def test_converted_variance_states_the_translation_effect_under_the_heading(released, capsys):
    common = ["--store", str(released.root / "store.duckdb"), "--config", str(released.config_path)]
    assert main(["variance", "--release", "2026-10-R2", "--currency", "USD", *common]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines[1] == TRANSLATION
    assert AU_CONVERTED in lines and AU_BASE not in lines
    assert main(["variance", "--release", "2026-10-R2", *common]) == 0
    out = capsys.readouterr().out
    assert TRANSLATION not in out and AU_BASE in out.splitlines()


def test_converted_deck_states_the_translation_effect_and_the_changes_basis(released, tmp_path):
    r = release_report(released.store, released.ws, "2026-10-R2", "USD")
    slides = open_deck(deck.write_deck(r, load_commentary(COMMENTARY), tmp_path))
    assert TRANSLATION in [line for t in texts(slides[0]) for line in t.splitlines()]
    assert TRANSLATION in [line for t in texts(slides[2]) for line in t.splitlines()]  # the APAC P&L
    assert AU_CONVERTED in "\n".join(texts(slides[3])) and AU_BASE not in "\n".join(texts(slides[3]))
    assert "\n".join(CHANGES_NOTES) in texts(slides[5])


def test_converted_changes_name_the_materiality_currency_and_the_restating_rates(released):
    r = release_report(released.store, released.ws, "2026-10-R2", "USD")
    assert r.changes_notes == CHANGES_NOTES
    assert release_report(released.store, released.ws, "2026-10-R2").changes_notes == ()
    no_usd = {sc: {c: v for c, v in t.items() if c != "USD"} for sc, t in r.previous_rates.items()}
    assert replace(r, previous_rates=no_usd).changes_notes == (*CHANGES_NOTES,
                                                               "2026-10-R1 did not store a USD rate.")
    other = {sc: {**t, "USD": D("1.31")} for sc, t in r.previous_rates.items()}
    assert replace(r, previous_rates=other).changes_notes == (
        *CHANGES_NOTES, "2026-10-R1 stored actual 1.31 / budget 1.31 SGD per USD; its amounts here use "
                        "2026-10-R2's actual 1.30 / budget 1.32.")


def test_currency_codes_are_case_insensitive(released, capsys):
    usd = release_report(released.store, released.ws, "2026-10-R2", "USD")
    assert release_report(released.store, released.ws, "2026-10-R2", "usd") == usd
    common = ["--store", str(released.root / "store.duckdb"), "--config", str(released.config_path)]
    assert main(["variance", "--release", "2026-10-R2", "--currency", "usd", *common]) == 0
    assert f"Amounts in {LABEL}." in capsys.readouterr().out.splitlines()[0]
