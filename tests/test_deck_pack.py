"""Deck packs: the default pack is today's deck, every shipped pack matches the report, bad pack files fail clearly."""

import io
import re
from decimal import Decimal as D

import pytest
from conftest import REPO, run_story
from pptx import Presentation
from test_deck import all_text, expected_row, footer, stacked_points, tables, texts, visible, visible_range
from test_deck_style import BUILT_IN_R2, content_digest

from finance_close_toolkit import deck
from finance_close_toolkit.cli import main
from finance_close_toolkit.commentary import load_commentary
from finance_close_toolkit.config import ConfigError
from finance_close_toolkit.deck_pack import (DEFAULT_PACK, BridgeSlide, ChangesSlide, MarketsSlide, OutlookSlide,
                                             PnlSlide, ScorecardSlide, TitleSlide, find_pack, load_pack, pack_keys,
                                             parse_pack)
from finance_close_toolkit.money import fmt_money, fmt_percent, fmt_step
from finance_close_toolkit.variance import BRIDGE_LINES, release_report

COMMENTARY = REPO / "examples" / "commentary" / "2026-10.toml"
PACKS = REPO / "config" / "deck" / "packs"
CONFIG = REPO / "config"
MARKETS = ("SG", "AU")
SCORECARD_LINE = {"revenue": "REV", "gross_margin": "gross_margin", "gm_pct": "gm_pct", "ebitda": "ebitda",
                  "ebitda_pct": "ebitda_pct"}
BRIDGE_LABEL = {"revenue": "Revenue", "gross_margin": "Gross margin", "ebitda": "EBITDA"}


@pytest.fixture
def released(close):
    run_story(close, "E13")
    return close


@pytest.fixture(scope="module")
def office():
    return deck.load_template(CONFIG / "deck" / "template.pptx"), deck.load_style(CONFIG / "deck" / "style.toml")


def test_shipped_comex_pack_is_the_default_pack():
    assert load_pack(PACKS / "comex.toml", MARKETS) == DEFAULT_PACK
    assert find_pack(CONFIG, None, MARKETS) == DEFAULT_PACK
    assert pack_keys(CONFIG) == ("comex", "board", "market_review")


def test_default_pack_reproduces_todays_deck(released):
    report = release_report(released.store, released.ws, "2026-10-R2")
    pack = find_pack(CONFIG, "comex", MARKETS)
    assert content_digest(deck.render(report, load_commentary(COMMENTARY), pack=pack)) == BUILT_IN_R2


def test_default_pack_without_its_file_is_still_the_default(tmp_path):
    assert pack_keys(tmp_path) == ("comex",)
    assert find_pack(tmp_path, None, MARKETS) is DEFAULT_PACK
    with pytest.raises(ConfigError, match="no deck pack 'board'; available: comex"):
        find_pack(tmp_path, "board", MARKETS)


# Parity: each slide of a rendered pack, read back, against the report formatted from the spec.


def expected_slides(report, pack, commentary):
    """(title, check) per slide the pack should give, where check(slide) asserts its tables and chart."""
    long = "October 2026"
    out = []
    for spec in pack.slides:
        if isinstance(spec, TitleSlide):
            out.append((spec.title.replace("{period_long}", long), lambda s: None))
        elif isinstance(spec, ScorecardSlide):
            rows = [expected_row(report.row("APAC", SCORECARD_LINE[k])) for k in spec.kpis]
            out.append((spec.title, lambda s, rows=rows: _assert_one_table(s, rows)))
        elif isinstance(spec, PnlSlide):
            scopes = list(MARKETS) if spec.scope == "each_market" else [spec.scope]
            for scope in scopes:
                rows = [expected_row(r) for r in report.scope(scope)]
                out.append((spec.title.replace("{market}", scope), lambda s, rows=rows: _assert_one_table(s, rows)))
        elif isinstance(spec, MarketsSlide):
            rows = []
            for scope in (*MARKETS, "APAC"):
                cells = [scope]
                for key in ("REV", "gross_margin", "ebitda"):
                    r = report.row(scope, key)
                    cells += [fmt_money(r.actual, 0), fmt_money(r.variance, 0)]
                rows.append(cells)
            out.append((spec.title, lambda s, rows=rows: _assert_one_table(s, rows)))
        elif isinstance(spec, BridgeSlide):
            title = spec.title.replace("{kpi}", BRIDGE_LABEL[spec.kpi])
            out.append((title, lambda s, kpi=spec.kpi: assert_bridge_slide(report, kpi, s)))
        elif isinstance(spec, ChangesSlide):
            if report.changes is None:
                continue
            rows = [[k.scenario, k.scope, k.kpi.label, fmt_money(k.before, 0), fmt_money(k.after, 0),
                     fmt_money(k.change, 0), fmt_percent(k.pct, 1), "Material" if k.material else "Below threshold"]
                    for k in report.changes.kpis]
            out.append((spec.title.replace("{previous}", report.previous_release_id),
                        lambda s, rows=rows: _assert_one_table(s, rows)))
        elif isinstance(spec, OutlookSlide):
            def check(s):
                assert (commentary.outlook if commentary else deck.NO_COMMENTARY) in texts(s)
            out.append((spec.title, check))
    return out


def _assert_one_table(slide, rows):
    [table] = tables(slide)
    assert table[1:] == rows


def assert_bridge_slide(report, kpi, slide):
    b = report.bridges[kpi]
    label = BRIDGE_LABEL[kpi]
    [table] = tables(slide)
    assert table == [
        [f"{label} budget", *MARKETS, f"{label} actual"],
        [fmt_money(b.budget, 0), *(fmt_step(s.amount, 0) for s in b.steps), fmt_money(b.actual, 0)],
        ["", *(report.row(m, BRIDGE_LINES[kpi]).label for m in MARKETS), ""],
    ]
    [chart] = [s.chart for s in slide.shapes if s.has_chart]
    assert list(chart.plots[0].categories) == ["Budget", *MARKETS, "Actual"]
    ranges = [(min(D(0), b.budget), max(D(0), b.budget)),
              *((min(s.before, s.after), max(s.before, s.after)) for s in b.steps),
              (min(D(0), b.actual), max(D(0), b.actual))]
    labels = [fmt_money(b.budget, 0), *(fmt_step(s.amount, 0) for s in b.steps), fmt_money(b.actual, 0)]
    for i, (points, rng, text) in enumerate(zip(stacked_points(chart), ranges, labels, strict=True)):
        assert visible_range(points) == rng
        [shown] = [s.points[i].data_label.text_frame.text for s, _, _ in points
                   if s.points[i].data_label.has_text_frame and s.points[i].data_label.text_frame.text]
        assert shown == text


def assert_pack_matches_report(data: bytes, report, pack, commentary):
    slides = list(Presentation(io.BytesIO(data)).slides)
    expected = expected_slides(report, pack, commentary)
    assert len(slides) == len(expected)
    for slide, (title, check) in zip(slides, expected, strict=True):
        assert title in texts(slide)
        check(slide)
        assert footer(slide).startswith(f"{report.name} · 2026-10 · SGD · checksum {report.facts_checksum[:12]}")


@pytest.mark.parametrize("key", ["comex", "board", "market_review"])
@pytest.mark.parametrize("release_id", ["2026-10-R1", "2026-10-R2"])
@pytest.mark.parametrize("styled", [False, True], ids=["built-in", "office"])
def test_every_shipped_pack_matches_the_report(released, office, key, release_id, styled):
    report = release_report(released.store, released.ws, release_id)
    pack = find_pack(CONFIG, key, MARKETS)
    commentary = load_commentary(COMMENTARY)
    template, style = office if styled else (None, None)
    assert_pack_matches_report(deck.render(report, commentary, template, style, pack), report, pack, commentary)


def test_shipped_packs_have_the_slides_the_spec_lists(released):
    r2 = release_report(released.store, released.ws, "2026-10-R2")
    r1 = release_report(released.store, released.ws, "2026-10-R1")

    def titles(key, report):
        slides = Presentation(io.BytesIO(deck.render(report, None, pack=find_pack(CONFIG, key, MARKETS)))).slides
        return [next(t for t in texts(s) if " · checksum " not in t) for s in slides]

    assert titles("board", r2) == ["APAC Board pack, October 2026", "Headline KPIs, APAC",
                                   "EBITDA bridge, budget to actual", "Outlook and actions"]
    # One P&L slide per market, in config order (SG before AU, not alphabetical).
    assert titles("market_review", r2) == ["APAC market review, October 2026", "Markets", "P&L vs budget, SG",
                                           "P&L vs budget, AU", "Changes since 2026-10-R1"]
    assert titles("market_review", r1)[-1] == "P&L vs budget, AU"


def test_a_market_pnl_slide_notes_its_fx_translation(released):
    report = release_report(released.store, released.ws, "2026-10-R2")
    slides = Presentation(io.BytesIO(deck.render(report, None, pack=find_pack(CONFIG, "market_review", MARKETS))))
    sg, au = list(slides.slides)[2:4]
    assert "AU translated at actual 0.86 / budget 0.88 SGD per AUD; variance includes FX." in texts(au)
    assert not any("translated" in t for t in texts(sg))


@pytest.mark.parametrize("kpi", ["revenue", "gross_margin", "ebitda"])
def test_a_bridge_on_any_kpi_sums_exactly_and_draws_right(released, kpi):
    report = release_report(released.store, released.ws, "2026-10-R2")
    b = report.bridges[kpi]
    assert b.budget + sum(s.amount for s in b.steps) == b.actual
    pack = parse_pack(f'name = "Bridges"\n[[slides]]\ntype = "bridge"\nkpi = "{kpi}"\n', "p.toml", "bridges", MARKETS)
    [slide] = Presentation(io.BytesIO(deck.render(report, None, pack=pack))).slides
    assert f"{BRIDGE_LABEL[kpi]} bridge, budget to actual" in texts(slide)
    assert_bridge_slide(report, kpi, slide)


def test_draft_only_when_the_pack_has_an_outlook_slide(released, tmp_path):
    report = release_report(released.store, released.ws, "2026-10-R2")
    board = find_pack(CONFIG, "board", MARKETS)
    review = find_pack(CONFIG, "market_review", MARKETS)
    assert deck.draft_reasons(report, None, board) == ("no commentary",)
    assert deck.draft_reasons(report, None, review) == ()

    path = deck.write_deck(report, None, tmp_path, pack=review)
    assert path.name == "Market_review_2026-10_2026-10-R2.pptx"
    slides = list(Presentation(str(path)).slides)
    assert "Status: FINAL" in all_text(slides[0])
    assert not any(footer(s).endswith("DRAFT") for s in slides)

    path = deck.write_deck(report, None, tmp_path, pack=board)
    assert path.name == "Board_2026-10_2026-10-R2.pptx"
    slides = list(Presentation(str(path)).slides)
    assert all(footer(s).endswith(" · DRAFT") for s in slides)
    assert "Figures approved (release 2026-10-R2). Pack DRAFT: outlook and actions not provided." in all_text(slides[0])


def test_titles_take_placeholders(released):
    report = release_report(released.store, released.ws, "2026-10-R2")
    pack = parse_pack("""
name = "Custom {pack}"
[[slides]]
type = "title"
title = "{release} for {period} in {currency}, {{draft}}"
[[slides]]
type = "kpi_scorecard"
title = "KPIs {period_long}"
kpis = ["ebitda", "revenue"]
""", "p.toml", "custom", MARKETS)
    assert pack.title == "APAC Custom {{pack}}, {period_long}"
    assert deck.deck_title(report, pack) == "APAC Custom {pack}, October 2026"
    title, scorecard = Presentation(io.BytesIO(deck.render(report, None, pack=pack))).slides
    assert "2026-10-R2 for 2026-10 in SGD, {draft}" in texts(title)
    assert "KPIs October 2026" in texts(scorecard)
    assert tables(scorecard)[0][1:] == [expected_row(report.row("APAC", k)) for k in ("ebitda", "REV")]


SLIDE = '[[slides]]\ntype = "title"\n'


@pytest.mark.parametrize("text, message", [
    ('name = "P"\n[[slides]]\ntype = "chart"\n',
     "p.toml: slide 1 has unknown type 'chart'; expected one of title, kpi_scorecard, pnl, markets, bridge, "
     "changes, outlook"),
    ('name = "P"\n' + SLIDE + '[[slides]]\ntitle = "x"\n', "p.toml: slide 2 has no type"),
    ('name = "P"\n' + SLIDE + '[[slides]]\ntype = "bridge"\nscope = "AU"\n',
     "p.toml: slide 2 (bridge): unknown key 'scope'; expected title, kpi"),
    ('name = "P"\n[[slides]]\ntype = "kpi_scorecard"\nkpis = ["revenue", "ebit"]\n',
     "p.toml: slide 1 (kpi_scorecard): unknown KPI 'ebit'; expected one of revenue, gross_margin, gm_pct, "
     "ebitda, ebitda_pct"),
    ('name = "P"\n[[slides]]\ntype = "kpi_scorecard"\nkpis = []\n', "slide 1 (kpi_scorecard): kpis must be a list"),
    ('name = "P"\n[[slides]]\ntype = "kpi_scorecard"\nkpis = ["ebitda", "ebitda"]\n', "lists a KPI twice"),
    ('name = "P"\n[[slides]]\ntype = "bridge"\nkpi = "ebit"\n',
     "slide 1 (bridge): unknown KPI 'ebit' for a bridge; expected one of revenue, gross_margin, ebitda"),
    ('name = "P"\n' + SLIDE * 2 + '[[slides]]\ntype = "pnl"\nscope = "NZ"\n',
     "p.toml: slide 3 (pnl): unknown scope 'NZ'; expected one of APAC, SG, AU, each_market"),
    ('name = "P"\n[[slides]]\ntype = "pnl"\ntitle = "P&L {market} {quarter}"\n',
     "slide 1 (pnl): title 'P&L {market} {quarter}' has an unknown placeholder {quarter}; expected "
     "{period}, {period_long}, {release}, {currency}, {market}"),
    ('name = "P"\n[[slides]]\ntype = "markets"\ntitle = "Markets {market}"\n',
     "slide 1 (markets): title 'Markets {market}' has an unknown placeholder {market}"),
    ('name = "P"\n[[slides]]\ntype = "outlook"\ntitle = "Outlook {period:>10}"\n', "unknown placeholder {period:>10}"),
    ('name = "P"\n[[slides]]\ntype = "outlook"\ntitle = "Outlook {period"\n', "has an unmatched { or }"),
    ('name = "P"\ntitle = "{pack} deck"\n' + SLIDE, "p.toml: title '{pack} deck' has an unknown placeholder"),
    ('name = "P"\n[[slides]]\ntype = "outlook"\ntitle = ""\n', "slide 1 (outlook): title must be text"),
    ('title = "T"\n' + SLIDE, "p.toml: name must be text"),
    ('name = "P"\n', "p.toml: no slides"),
    ('name = "P"\nslide = []\n' + SLIDE, "p.toml: unknown key 'slide'; expected name, title and"),
    ('name = "P"\nslides = ["title"]\n', "p.toml: slide 1 must be a [[slides]] table"),
    ('name = "P\n', "p.toml: not valid TOML"),
])
def test_invalid_pack_is_a_clear_error(text, message):
    with pytest.raises(ConfigError, match=re.escape(message)):
        parse_pack(text, "p.toml", "p", MARKETS)


def test_pack_files_not_found_or_not_text_are_clear_errors(tmp_path):
    with pytest.raises(ConfigError, match="pack file not found"):
        find_pack(CONFIG, str(tmp_path / "nope.toml"), MARKETS)
    bad = tmp_path / "bad.toml"
    bad.write_bytes(b"\xff\xfe")
    with pytest.raises(ConfigError, match="bad.toml: not UTF-8 text"):
        load_pack(bad, MARKETS)


def test_cli_deck_pack_by_name_and_by_file_and_listing(released, capsys, tmp_path):
    common = ["--store", str(released.root / "store.duckdb"), "--config", str(released.config_path)]

    def fct(*args: str, code: int = 0) -> str:
        assert main([*args, *common]) == code
        out = capsys.readouterr()
        return out.out + out.err

    out = fct("deck", "--release", "2026-10-R2", "--out", str(tmp_path), "--pack", "board")
    assert f"Wrote {tmp_path / 'Board_2026-10_2026-10-R2.pptx'} (DRAFT: no commentary)." in out
    out = fct("deck", "--release", "2026-10-R2", "--out", str(tmp_path), "--pack", "market_review")
    assert "Market_review_2026-10_2026-10-R2.pptx (FINAL)." in out

    custom = tmp_path / "Revenue view.toml"
    custom.write_text('name = "Revenue view"\n[[slides]]\ntype = "bridge"\nkpi = "revenue"\n', encoding="utf-8")
    out = fct("deck", "--release", "2026-10-R2", "--out", str(tmp_path), "--pack", str(custom))
    assert "Revenue_view_2026-10_2026-10-R2.pptx (FINAL)." in out
    [slide] = Presentation(str(tmp_path / "Revenue_view_2026-10_2026-10-R2.pptx")).slides
    assert "Revenue bridge, budget to actual" in texts(slide)

    custom.write_text('name = "Bad"\n[[slides]]\ntype = "pnl"\nscope = "NZ"\n', encoding="utf-8")
    out = fct("deck", "--release", "2026-10-R2", "--out", str(tmp_path / "none"), "--pack", str(custom), code=2)
    assert "slide 1 (pnl): unknown scope 'NZ'; expected one of APAC, SG, AU, each_market" in out
    assert not (tmp_path / "none").exists()
    assert "no deck pack 'boardroom'; available: comex, board, market_review" in fct(
        "deck", "--release", "2026-10-R2", "--out", str(tmp_path), "--pack", "boardroom", code=2)

    out = fct("deck", "packs")
    assert "comex  Comex pack (default)" in out
    assert "  1. title  \"APAC Comex pack, {period_long}\"" in out
    assert "  3. pnl  scope = 'each_market'  \"P&L vs budget, {market}\"" in out
    assert "board  Board pack" in out and "market_review  Market review" in out
    assert "error: --out is required" in fct("deck", "--release", "2026-10-R2", code=2)


def test_a_pack_whose_slides_all_skip_is_refused(released, tmp_path):
    first = release_report(released.store, released.ws, "2026-10-R1")
    assert first.changes is None
    pack = parse_pack('name = "What changed"\n[[slides]]\ntype = "changes"\n', "p.toml", "changed", MARKETS)
    with pytest.raises(ConfigError, match=re.escape("p.toml: no slides to render for this release (2026-10-R1)")):
        deck.write_deck(first, None, tmp_path / "out", pack=pack)
    assert not (tmp_path / "out").exists(), "no 0-slide deck is written"


def test_a_title_over_100_characters_after_placeholders_names_the_slide(released, tmp_path):
    report = release_report(released.store, released.ws, "2026-10-R2")
    title = "x" * 91 + "{release}"
    assert len(title) == 100
    pack = parse_pack(f'name = "P"\n[[slides]]\ntype = "title"\n[[slides]]\ntype = "outlook"\ntitle = "{title}"\n',
                      "p.toml", "p", MARKETS)
    with pytest.raises(ConfigError, match=re.escape("p.toml: slide 2 (outlook) title is 101 characters after "
                                                    "placeholders; the most is 100: 'xxx")):
        deck.write_deck(report, None, tmp_path / "out", pack=pack)
    assert not (tmp_path / "out").exists()


def test_pack_lookup_by_name_is_an_exact_file_stem_match():
    with pytest.raises(ConfigError, match="no deck pack 'Board'; available: comex, board, market_review"):
        find_pack(CONFIG, "Board", MARKETS)
    assert find_pack(CONFIG, "board", MARKETS).name == "Board pack"


def test_deck_packs_lists_the_valid_packs_and_names_each_broken_file(released, capsys):
    packs = released.config_path.parent / "deck" / "packs"
    (packs / "broken.toml").write_text('name = "Broken"\n[[slides]]\ntype = "chart"\n', encoding="utf-8")
    code = main(["deck", "packs", "--store", str(released.root / "store.duckdb"), "--config",
                 str(released.config_path)])
    out = capsys.readouterr()
    assert code == 1
    assert "board  Board pack" in out.out and "market_review  Market review" in out.out
    assert (f"broken  BROKEN: {packs / 'broken.toml'}: slide 1 has unknown type 'chart'; expected one of title, "
            "kpi_scorecard, pnl, markets, bridge, changes, outlook") in out.out
