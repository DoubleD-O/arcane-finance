"""Comex deck: parity with the VarianceReport, the deck checks D1-D5, and file safety."""

from datetime import datetime
from decimal import Decimal as D
from pathlib import Path

import pytest
from conftest import REPO, TEST_TEMPLATE, build_deck_template, run_story
from pptx import Presentation
from pptx.enum.dml import MSO_FILL

from finance_close_toolkit import deck
from finance_close_toolkit.commentary import load_commentary, parse_commentary
from finance_close_toolkit.config import ConfigError, Market
from finance_close_toolkit.money import fmt_money, fmt_percent, fmt_pp, fmt_step
from finance_close_toolkit.release import ReleaseFact
from finance_close_toolkit.variance import build_report, proposed_report, release_report

COMMENTARY = REPO / "examples" / "commentary" / "2026-10.toml"


def tables(slide) -> list[list[list[str]]]:
    return [[[c.text_frame.text for c in row.cells] for row in s.table.rows] for s in slide.shapes if s.has_table]


def texts(slide) -> list[str]:
    return [s.text_frame.text for s in slide.shapes if s.has_text_frame]


def footer(slide) -> str:
    [f] = [t for t in texts(slide) if " · checksum " in t]
    return f


def all_text(slide) -> str:
    return "\n".join(texts(slide) + [c for t in tables(slide) for row in t for c in row])


def open_deck(path: Path):
    return list(Presentation(str(path)).slides)


# The report in the deck's display conventions (0 dp amounts, 1 dp percentages, 2 dp percentage
# points), written from the spec rather than through deck.py's own helpers.
def expected_row(row) -> list[str]:
    if row.line.kind == "ratio":
        return [row.line.label, fmt_percent(row.actual, 1), fmt_percent(row.budget, 1), fmt_pp(row.variance), "",
                row.label]
    return [row.line.label, fmt_money(row.actual, 0), fmt_money(row.budget, 0), fmt_money(row.variance, 0),
            fmt_percent(row.variance_pct, 1), row.label]


@pytest.fixture(scope="module")
def other_template(tmp_path_factory):
    return deck.load_template(build_deck_template(tmp_path_factory.mktemp("tpl") / "test.pptx", **TEST_TEMPLATE))


TEST_STYLE = deck.parse_style("""
[colors]
favourable = "1565C0"
unfavourable = "EF6C00"
total = "000000"
table_header = "6A1B9A"
[fonts]
table_size = 12
""", "test")


@pytest.fixture(params=["built-in", "test-template"])
def look(request, other_template) -> dict:
    """write_deck's styling arguments: none (the built-in look), or the test template with a non-default style."""
    return {} if request.param == "built-in" else {"template": other_template, "style": TEST_STYLE}


@pytest.fixture
def released(close):
    run_story(close, "E13")
    return close


def test_r2_deck_parity_with_report(released, tmp_path, look):
    report = release_report(released.store, released.ws, "2026-10-R2")
    path = deck.write_deck(report, load_commentary(COMMENTARY), tmp_path, **look)
    assert path.name == "Comex_2026-10_2026-10-R2.pptx"
    slides = open_deck(path)
    assert len(slides) == 7
    for s in slides:
        assert footer(s) == f"2026-10-R2 · 2026-10 · SGD · checksum {report.facts_checksum[:12]}"

    [scorecard] = tables(slides[1])
    keys = ["REV", "gross_margin", "gm_pct", "ebitda", "ebitda_pct"]
    assert scorecard[1:] == [expected_row(report.row("APAC", k)) for k in keys]

    [pnl] = tables(slides[2])
    assert pnl[1:] == [expected_row(r) for r in report.scope("APAC")]
    assert len(pnl) == 13

    [markets] = tables(slides[3])
    for cells, scope in zip(markets[1:], ["SG", "AU", "APAC"], strict=True):
        expected = [scope]
        for key in ("REV", "gross_margin", "ebitda"):
            r = report.row(scope, key)
            expected += [fmt_money(r.actual, 0), fmt_money(r.variance, 0)]
        assert cells == expected
    assert "AU translated at actual 0.86 / budget 0.88 SGD per AUD; variance includes FX." in texts(slides[3])

    b = report.bridge
    [bridge] = tables(slides[4])
    assert bridge[1] == [fmt_money(b.budget, 0), *(fmt_step(s.amount, 0) for s in b.steps), fmt_money(b.actual, 0)]
    chart = next(s for s in slides[4].shapes if s.has_chart).chart
    assert list(chart.plots[0].categories) == ["Budget", "SG", "AU", "Actual"]
    assert_bridge_drawn_right(report, chart)

    [changes] = tables(slides[5])
    assert changes[0][3:5] == ["2026-10-R1", "2026-10-R2"]
    assert changes[1:] == [
        [k.scenario, k.scope, k.kpi.label, fmt_money(k.before, 0), fmt_money(k.after, 0), fmt_money(k.change, 0),
         fmt_percent(k.pct, 1), "Material" if k.material else "Below threshold"]
        for k in report.changes.kpis
    ]


def test_d1_r2_scorecard_ebitda(released, tmp_path, look):
    report = release_report(released.store, released.ws, "2026-10-R2")
    slides = open_deck(deck.write_deck(report, load_commentary(COMMENTARY), tmp_path, **look))
    rows = {r[0]: r for r in tables(slides[1])[0]}
    assert rows["EBITDA"] == ["EBITDA", "208,700", "191,000", "17,700", "9.3%", "Favourable"]
    # fixture-expected D1 says "+1.0 pp" under the old 1 dp rule; points are now 2 dp everywhere.
    assert rows["EBITDA %"][3] == "+1.03 pp"
    assert rows["GM %"][3:] == ["+0.01 pp", "", "Favourable"]


def test_d2_r2_changes_since_r1(released, tmp_path, look):
    report = release_report(released.store, released.ws, "2026-10-R2")
    slides = open_deck(deck.write_deck(report, load_commentary(COMMENTARY), tmp_path, **look))
    assert "Changes since 2026-10-R1" in texts(slides[5])
    rows = {(r[1], r[2]): r for r in tables(slides[5])[0][1:]}
    assert rows[("AU", "EBITDA")][5:] == ["(12,900)", "-25.0%", "Material"]
    assert rows[("APAC", "EBITDA")][5:] == ["(12,900)", "-5.8%", "Material"]


def test_d3_r1_deck_has_no_changes_slide(released, tmp_path, look):
    report = release_report(released.store, released.ws, "2026-10-R1")
    slides = open_deck(deck.write_deck(report, load_commentary(COMMENTARY), tmp_path, **look))
    assert len(slides) == 6
    assert not any("Changes since" in all_text(s) for s in slides)
    [bridge] = tables(slides[4])
    assert bridge[0] == ["EBITDA budget", "SG", "AU", "EBITDA actual"]
    assert bridge[1] == ["191,000", "+45,000", "(14,400)", "221,600"]


def test_d4_no_commentary_is_draft(released, tmp_path, look):
    report = release_report(released.store, released.ws, "2026-10-R2")
    slides = open_deck(deck.write_deck(report, None, tmp_path, **look))
    assert deck.NO_COMMENTARY in texts(slides[6])
    assert tables(slides[6]) == []
    for s in slides:
        assert footer(s).endswith(" · DRAFT")
    title = all_text(slides[0])
    assert "Figures approved (release 2026-10-R2). Pack DRAFT: outlook and actions not provided." in title
    assert "Status:" not in title and "not approved" not in title


def test_commentary_appears_and_deck_is_final(released, tmp_path, look):
    report = release_report(released.store, released.ws, "2026-10-R2")
    slides = open_deck(deck.write_deck(report, load_commentary(COMMENTARY), tmp_path, **look))
    text = all_text(slides[6])
    assert "Q4 outlook holds." in text
    assert tables(slides[6]) == [[
        ["Action", "Owner", "Due"],
        ["Confirm AU consulting fees are one-off and agree November accrual", "AU country controller", "2026-12-05"],
        ["Review AU pricing against budget rate assumptions", "Regional FP&A lead", "2026-12-12"],
    ]]
    assert "Status: FINAL" in all_text(slides[0])
    assert not any("DRAFT" in footer(s) for s in slides)


def test_commentary_text_is_inserted_verbatim(released, tmp_path, look):
    report = release_report(released.store, released.ws, "2026-10-R2")
    hostile = parse_commentary('outlook = "=HYPERLINK(\\"http://x\\") {{name}} <b>"\n', "test")
    slides = open_deck(deck.write_deck(report, hostile, tmp_path, **look))
    assert '=HYPERLINK("http://x") {{name}} <b>' in texts(slides[6])


def test_d5_proposed_deck_after_b1_is_draft(close, tmp_path, look):
    run_story(close, "B1")
    report = proposed_report(close.store, close.ws, "2026-10")
    path = deck.write_deck(report, load_commentary(COMMENTARY), tmp_path, **look)
    assert path.name == "Comex_2026-10_PROPOSED.pptx"
    slides = open_deck(path)
    for s in slides:
        assert footer(s).startswith("PROPOSED · 2026-10 · SGD") and footer(s).endswith(" · DRAFT")
    assert "Status: DRAFT (proposed release, not approved)" in all_text(slides[0])
    slides = open_deck(deck.write_deck(report, None, tmp_path, force=True, **look))
    title = all_text(slides[0])
    assert "Status: DRAFT (proposed release, not approved; no commentary)" in title
    assert "Figures approved" not in title
    rows = {r[0]: r for r in tables(slides[1])[0]}
    assert rows["EBITDA"][1] == "207,700"
    assert "Changes since 2026-10-R2" in texts(slides[5])


def test_r1_deck_untouched_by_r2_deck_and_refuses_overwrite(released, tmp_path, look):
    r1 = release_report(released.store, released.ws, "2026-10-R1")
    r2 = release_report(released.store, released.ws, "2026-10-R2")
    r1_path = deck.write_deck(r1, None, tmp_path, **look)
    before = r1_path.read_bytes()
    r2_path = deck.write_deck(r2, None, tmp_path, **look)
    assert r2_path != r1_path
    assert r1_path.read_bytes() == before

    with pytest.raises(deck.DeckExists):
        deck.write_deck(r1, load_commentary(COMMENTARY), tmp_path, **look)
    assert r1_path.read_bytes() == before
    deck.write_deck(r1, load_commentary(COMMENTARY), tmp_path, force=True, **look)
    assert "DRAFT" not in footer(open_deck(r1_path)[0])


def test_core_properties_carry_approval_time(released, tmp_path, look):
    report = release_report(released.store, released.ws, "2026-10-R2")
    props = Presentation(str(deck.write_deck(report, None, tmp_path, **look))).core_properties
    assert props.title == "APAC Comex pack, October 2026"
    assert props.created == props.modified == datetime(2026, 11, 20, 9, 0)


def test_commentary_without_outlook_is_a_config_error():
    with pytest.raises(ConfigError, match="outlook"):
        parse_commentary('[[actions]]\naction = "x"\nowner = "y"\ndue = "2026-12-01"\n', "c.toml")
    with pytest.raises(ConfigError, match="due must be a date"):
        parse_commentary('outlook = "ok"\n[[actions]]\naction = "x"\nowner = "y"\ndue = "soon"\n', "c.toml")


# The bridge chart, read back as PowerPoint draws a stacked column: positive values stack up from
# zero and negative values stack down from zero, each in series order. Only filled series are seen.


def stacked_points(chart) -> list[list[tuple[object, D, D]]]:
    """Per category: (series, low, high) for every series point, where the point sits on the axis."""
    plot = chart.plots[0]
    out = []
    for i in range(len(plot.categories)):
        up = down = D(0)
        points = []
        for s in plot.series:
            v = D(str(s.values[i]))
            if v >= 0:
                points.append((s, up, up + v))
                up += v
            else:
                points.append((s, down + v, down))
                down += v
        out.append(points)
    return out


def visible(series) -> bool:
    return series.format.fill.type == MSO_FILL.SOLID


def visible_range(points) -> tuple[D, D]:
    segments = sorted((lo, hi) for s, lo, hi in points if visible(s) and hi > lo)
    for (_, end), (start, _) in zip(segments, segments[1:]):
        assert end == start, f"gap or overlap in {segments}"
    return segments[0][0], segments[-1][1]


def assert_bridge_drawn_right(report, chart):
    b = report.bridge
    ranges = [
        (min(D(0), b.budget), max(D(0), b.budget)),
        *((min(s.before, s.after), max(s.before, s.after)) for s in b.steps),
        (min(D(0), b.actual), max(D(0), b.actual)),
    ]
    texts_ = [fmt_money(b.budget, 0), *(fmt_step(s.amount, 0) for s in b.steps), fmt_money(b.actual, 0)]
    for i, (points, (lo, hi), text) in enumerate(zip(stacked_points(chart), ranges, texts_, strict=True)):
        assert visible_range(points) == (lo, hi), chart.plots[0].categories[i]
        labelled = []
        for s, plo, phi in points:
            dl = s.points[i].data_label
            if dl.has_text_frame and dl.text_frame.text:
                labelled.append((dl.text_frame.text, s, plo, phi))
        [(label, carrier, plo, phi)] = labelled
        assert label == text
        assert not visible(carrier)
        # The label's box touches its bar from outside: on top when the bar reaches above zero, else underneath.
        if hi > 0:
            assert plo == hi < phi, (text, plo, phi, lo, hi)
        else:
            assert plo < phi == lo, (text, plo, phi, lo, hi)
    # Totals below zero stay filled: no inverted (white) fill for negative values.
    assert all(s.invert_if_negative is False for s in chart.plots[0].series if visible(s))


def bridge_chart_of(slides):
    [chart] = [s.chart for s in slides[4].shapes if s.has_chart]
    return chart


def synthetic_report(ws, budget: dict[str, int], actual: dict[str, int]):
    """A release whose markets report one EBITDA amount each (as revenue), all in SGD."""
    markets = [Market(code, code, "SGD", "owner", Path("unused.toml")) for code in budget]
    facts = [
        ReleaseFact(m, scenario, "REV", D(v), "SGD", D(1), D(v), f"{m}-{scenario}", "synthetic.csv", 1)
        for scenario, amounts in (("budget", budget), ("actual", actual))
        for m, v in amounts.items()
    ]
    when = datetime(2026, 11, 20, 9, 0)
    return build_report(
        facts, ws.coa, markets, ws.config.materiality, release_id="2026-10-RX", period="2026-10",
        reporting_currency="SGD", approved_by="Test", approved_at=when, as_of=when,
        rates={"actual": {"SGD": D(1)}, "budget": {"SGD": D(1)}}, previous_release_id=None, previous_facts=None,
    )


CROSSING_ZERO = ({"SG": 100_000, "AU": 60_000, "NZ": 30_000}, {"SG": 90_000, "AU": -390_000, "NZ": 0})
ALL_NEGATIVE = ({"SG": -50_000, "AU": -30_000}, {"SG": -20_000, "AU": -90_000})


@pytest.mark.parametrize("budget, actual", [CROSSING_ZERO, ALL_NEGATIVE], ids=["crossing-zero", "all-negative"])
def test_bridge_chart_bars_span_running_totals_around_zero(close, tmp_path, budget, actual, look):
    report = synthetic_report(close.ws, budget, actual)
    steps = [(s.market, s.before, s.after) for s in report.bridge.steps]
    if budget is CROSSING_ZERO[0]:
        assert (report.bridge.budget, report.bridge.actual) == (D(190_000), D(-300_000))
        assert steps == [("SG", 190_000, 180_000), ("AU", 180_000, -270_000), ("NZ", -270_000, -300_000)]
    else:
        assert (report.bridge.budget, report.bridge.actual) == (D(-80_000), D(-110_000))
    assert_bridge_drawn_right(report, bridge_chart_of(open_deck(deck.write_deck(report, None, tmp_path, **look))))


def commentary_toml(outlook: str, actions: list[str]) -> str:
    out = f'outlook = "{outlook}"\n'
    for a in actions:
        out += f'[[actions]]\naction = "{a}"\nowner = "Regional FP&A lead"\ndue = "2026-12-01"\n'
    return out


def test_commentary_within_slide_limits_is_accepted():
    c = parse_commentary(commentary_toml("o" * 600, ["a" * 120] * 6), "c.toml")
    assert len(c.outlook) == 600 and len(c.actions) == 6


@pytest.mark.parametrize("outlook, actions, message", [
    ("o" * 601, [], "outlook is 601 characters; at most 600 fit on the slide"),
    ("ok", ["a"] * 7, "7 actions; at most 6 fit on the slide"),
    ("ok", ["a", "a" * 121], "action 2 is 121 characters; at most 120 fit on the slide"),
])
def test_commentary_over_slide_limits_is_a_config_error(outlook, actions, message):
    with pytest.raises(ConfigError, match=message):
        parse_commentary(commentary_toml(outlook, actions), "c.toml")
