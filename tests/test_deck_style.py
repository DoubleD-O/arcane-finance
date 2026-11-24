"""Deck styling: the built-in look is unchanged, a template and style.toml flow through, bad files fail clearly."""

import hashlib
import io
import zipfile
from pathlib import Path

import pytest
from conftest import REPO, TEST_TEMPLATE, build_deck_template, run_story
from pptx import Presentation
from pptx.dml.color import RGBColor
from pptx.enum.dml import MSO_FILL
from pptx.opc.constants import RELATIONSHIP_TYPE as RT
from pptx.oxml.ns import qn
from pptx.util import Inches

from finance_close_toolkit import deck
from finance_close_toolkit.cli import main
from finance_close_toolkit.commentary import load_commentary
from finance_close_toolkit.config import ConfigError
from finance_close_toolkit.variance import release_report

COMMENTARY = REPO / "examples" / "commentary" / "2026-10.toml"
STARTER = REPO / "config" / "deck" / "template.pptx"


def content_digest(pptx: bytes) -> str:
    """Every part of the deck, and of the chart's embedded workbook, except the workbook's own save time."""
    def parts(blob: bytes, nested: bool) -> dict[str, bytes]:
        z = zipfile.ZipFile(io.BytesIO(blob))
        out = {}
        for name in z.namelist():
            data = z.read(name)
            if nested and name.endswith(".xlsx"):
                out |= {f"{name}!{k}": v for k, v in parts(data, False).items()}
            elif nested or name != "docProps/core.xml":
                out[name] = data
        return out

    h = hashlib.sha256()
    for name, data in sorted(parts(pptx, True).items()):
        h.update(name.encode() + b"\0" + data)
    return h.hexdigest()


@pytest.fixture
def r2(close):
    run_story(close, "E13")
    return release_report(close.store, close.ws, "2026-10-R2")


# The R2 deck with commentary as commit 014223a wrote it, before templates existed. A python-pptx upgrade
# that changes its XML would change this too; regenerate only after checking the deck renders the same.
BUILT_IN_R2 = "667cc32d174151c282340a7458138b71ea8b068af9cad260c24d3ce554bc9438"


def test_built_in_look_is_unchanged(r2):
    assert content_digest(deck.render(r2, load_commentary(COMMENTARY))) == BUILT_IN_R2
    assert content_digest(deck.render(r2, load_commentary(COMMENTARY), style=deck.DEFAULT_STYLE)) == BUILT_IN_R2


@pytest.fixture
def office_deck(r2):
    """The R2 deck with commentary, made from the shipped config/deck files."""
    style = deck.load_style(REPO / "config" / "deck" / "style.toml")
    data = deck.render(r2, load_commentary(COMMENTARY), deck.load_template(STARTER), style)
    return Presentation(io.BytesIO(data)), style


def footer_band(prs):
    [band] = [s for s in prs.slide_master.shapes if s.name == "Footer band"]
    return band


def test_office_starter_fits_every_slide_above_its_footer_band(office_deck):
    prs, _ = office_deck
    assert deck.load_template(STARTER).notes == ()
    band = footer_band(prs)
    assert (band.left, band.width, band.top + band.height) == (0, prs.slide_width, prs.slide_height)
    assert len(prs.slides) == 7
    for n, slide in enumerate(prs.slides, 1):
        [footer] = [s for s in slide.shapes if s.has_text_frame and " · checksum " in s.text_frame.text]
        assert band.top <= footer.top and footer.top + footer.height <= band.top + band.height, n
        assert band.left <= footer.left and footer.left + footer.width <= band.left + band.width, n
        for s in slide.shapes:
            assert 0 <= s.left and s.left + s.width <= prs.slide_width, (n, s.name)
            if s.shape_id != footer.shape_id:
                assert 0 <= s.top and s.top + s.height <= band.top, (n, s.name)


def test_office_starter_titles_take_the_theme_font(office_deck):
    prs, _ = office_deck
    theme = prs.slide_master.part.part_related_by(RT.THEME).blob
    assert b'<a:majorFont><a:latin typeface="Segoe UI Semibold"' in theme
    assert b'<a:minorFont><a:latin typeface="Segoe UI"' in theme
    for slide in prs.slides:
        title = slide.shapes.title
        assert title is not None and title.is_placeholder
        assert all(r.font.name is None and r.font.size is None and r.font.color.type is None
                   for p in title.text_frame.paragraphs for r in p.runs)


def test_office_starter_tables_and_bridge_take_style_colours(office_deck):
    prs, style = office_deck
    slides = list(prs.slides)
    tables = [s for slide in slides for s in slide.shapes if s.has_table]
    assert len(tables) == 6
    for shape in tables:
        assert all(c.fill.fore_color.rgb == style.table_header for c in shape.table.rows[0].cells)
        # The template's default table style (Medium Style 3 - Accent 1), not python-pptx's.
        assert shape.table._tbl.tblPr.find(qn("a:tableStyleId")).text == "{6E25E649-3F16-4E02-A733-19D2CDBF48F0}"
    chart = next(s.chart for s in slides[4].shapes if s.has_chart)
    fills = {s.name: s.format.fill.fore_color.rgb for s in chart.plots[0].series
             if s.format.fill.type == MSO_FILL.SOLID}
    assert (fills["Total"], fills["Favourable"], fills["Unfavourable"]) == (style.total, style.favourable,
                                                                           style.unfavourable)


def test_other_template_shapes_fit_above_its_footer(r2, tmp_path):
    template = deck.load_template(build_deck_template(tmp_path / "t.pptx", **TEST_TEMPLATE))
    prs = Presentation(io.BytesIO(deck.render(r2, load_commentary(COMMENTARY), template)))
    assert (prs.slide_width, prs.slide_height) == (Inches(10), Inches(7.5))
    footer_top = Inches(TEST_TEMPLATE["footer_top_in"])
    assert len(prs.slides) == 7
    for n, slide in enumerate(prs.slides, 1):
        assert "SAMPLE" not in "".join(s.text_frame.text for s in slide.shapes if s.has_text_frame)
        [footer] = [s for s in slide.shapes if s.has_text_frame and " · checksum " in s.text_frame.text]
        assert footer.top == footer_top and footer.left + footer.width <= prs.slide_width
        for s in slide.shapes:
            assert 0 <= s.left and s.left + s.width <= prs.slide_width, (n, s.name)
            if s.shape_id != footer.shape_id:
                assert 0 <= s.top and s.top + s.height <= footer_top, (n, s.name)
    # The titles are the template's placeholders, so its theme font and title colour reach them unaltered.
    for slide in prs.slides:
        title = slide.shapes.title
        assert title is not None and title.is_placeholder
        assert all(r.font.name is None and r.font.size is None and r.font.color.type is None
                   for p in title.text_frame.paragraphs for r in p.runs)
    theme = prs.slide_master.part.part_related_by(RT.THEME).blob
    assert b'<a:latin typeface="Georgia"' in theme and b'val="5B0E2D"' in prs.slide_master.part.blob


def test_style_colours_reach_bridge_table_header_and_footer(r2, tmp_path):
    style = deck.parse_style("""
[colors]
favourable = "1565c0"
unfavourable = "EF6C00"
total = "000000"
table_header = "6A1B9A"
footer_text = "FF0000"
[fonts]
table_size = 16
footer_size = 8
""", "test")
    slides = list(Presentation(io.BytesIO(deck.render(r2, load_commentary(COMMENTARY), style=style))).slides)
    chart = next(s.chart for s in slides[4].shapes if s.has_chart)
    fills = {s.name: s.format.fill.fore_color.rgb for s in chart.plots[0].series
             if s.format.fill.type == MSO_FILL.SOLID}
    assert fills == {"Total": RGBColor(0, 0, 0), "Total below zero": RGBColor(0, 0, 0),
                     "Favourable": RGBColor(0x15, 0x65, 0xC0), "Favourable below zero": RGBColor(0x15, 0x65, 0xC0),
                     "Unfavourable": RGBColor(0xEF, 0x6C, 0x00), "Unfavourable below zero": RGBColor(0xEF, 0x6C, 0x00)}
    labels = [p.data_label.text_frame.paragraphs[0].runs[0].font.color.rgb
              for s in chart.plots[0].series for p in (s.points[i] for i in range(4))
              if p.data_label.has_text_frame and p.data_label.text_frame.text]
    assert labels == [RGBColor(0, 0, 0), RGBColor(0x15, 0x65, 0xC0), RGBColor(0xEF, 0x6C, 0x00), RGBColor(0, 0, 0)]
    for slide in slides:
        footer = next(s for s in slide.shapes if s.has_text_frame and " · checksum " in s.text_frame.text)
        run = footer.text_frame.paragraphs[0].runs[0]
        assert (run.font.color.rgb, run.font.size.pt) == (RGBColor(0xFF, 0, 0), 8)
        for table in (s.table for s in slide.shapes if s.has_table):
            assert all(c.fill.fore_color.rgb == RGBColor(0x6A, 0x1B, 0x9A) for c in table.rows[0].cells)
            assert all(c.fill.type is None for c in table.rows[1].cells)
    [scorecard] = [s.table for s in slides[1].shapes if s.has_table]
    assert scorecard.cell(1, 1).text_frame.paragraphs[0].runs[0].font.size.pt == 16


def test_without_table_header_the_template_table_style_shows(r2):
    slides = Presentation(io.BytesIO(deck.render(r2, None))).slides
    [scorecard] = [s.table for s in slides[1].shapes if s.has_table]
    assert all(c.fill.type is None for c in scorecard.rows[0].cells)


@pytest.mark.parametrize("text, message", [
    ('[colors]\nfavourable = "green"\n', 'colors.favourable must be a 6-digit hex colour like "2E7D32", got \'green\''),
    ('[colors]\nfavourable = "#2E7D32"\n', "colors.favourable must be a 6-digit hex colour"),
    ('[colors]\nfavorable = "2E7D32"\n', "unknown key colors.favorable; expected one of favourable, unfavourable"),
    ('[colours]\nfavourable = "2E7D32"\n', r"unknown section \[colours\]; expected \[colors\] or \[fonts\]"),
    ("[fonts]\ntable_size = 14.5\n", "fonts.table_size must be a whole number of points from 6 to 40, got 14.5"),
    ("[fonts]\nfooter_size = 0\n", "fonts.footer_size must be a whole number of points from 6 to 40, got 0"),
    ('[fonts]\ntable_size = "14"\n', "fonts.table_size must be a whole number"),
    ('colors = "2E7D32"\n', r"colors must be a \[colors\] section"),
    ("[colors\n", "not valid TOML"),
])
def test_invalid_style_is_a_clear_error(text, message):
    with pytest.raises(ConfigError, match=message):
        deck.parse_style(text, "style.toml")


def test_partial_style_keeps_the_rest_of_the_built_in_look():
    assert deck.parse_style('[colors]\ntotal = "000000"\n', "s") == deck.DeckStyle(total=RGBColor(0, 0, 0))
    assert deck.parse_style("", "s") == deck.DEFAULT_STYLE


def test_missing_layout_falls_back_and_says_which(r2, tmp_path):
    path = build_deck_template(tmp_path / "t.pptx", rename={"Title Only": "Heading only"})
    template = deck.load_template(path)
    assert template.content_layout == "Title Slide"
    assert template.notes == ('Template has no "Title Only" layout with a title; using "Title Slide" for the '
                              "other slides.",)
    prs = Presentation(io.BytesIO(deck.render(r2, None, template)))
    assert {s.slide_layout.name for s in prs.slides} == {"Title Slide"}
    # The fallback layout's subtitle is not used on content slides, so it is removed rather than left empty.
    assert all(len(s.placeholders) == 1 for s in list(prs.slides)[1:])
    assert [s.shapes.title.text_frame.text for s in prs.slides][1:3] == ["KPI scorecard, APAC", "P&L vs budget, APAC"]
    # Every content slide's title sits above the tables and charts, not under them.
    for s in list(prs.slides)[1:]:
        title = s.shapes.title
        others = [sh for sh in s.shapes if sh.shape_id != title.shape_id and (sh.has_table or sh.has_chart)]
        assert all(title.top + title.height <= sh.top for sh in others)


def test_missing_template_or_style_file_is_a_clear_error(tmp_path):
    with pytest.raises(ConfigError, match="template file not found"):
        deck.load_template(tmp_path / "nope.pptx")
    with pytest.raises(ConfigError, match="style file not found"):
        deck.load_style(tmp_path / "nope.toml")


def test_not_a_pptx_is_a_clear_error(tmp_path):
    bad = tmp_path / "template.pptx"
    bad.write_text("not a deck")
    with pytest.raises(ConfigError, match="not a PowerPoint .pptx file"):
        deck.load_template(bad)


def test_cli_uses_config_deck_files_and_overrides(close, capsys, tmp_path):
    run_story(close, "E13")
    common = ["--store", str(close.root / "store.duckdb"), "--config", str(close.config_path)]

    def fct(*args: str, code: int = 0) -> str:
        assert main([*args, *common]) == code
        out = capsys.readouterr()
        return out.out + out.err

    # config/deck/ (copied from the repo) is the starter template: today's 16:9 slide.
    out = fct("deck", "--release", "2026-10-R2", "--out", str(tmp_path / "a"))
    assert Presentation(str(tmp_path / "a" / "Comex_2026-10_2026-10-R2.pptx")).slide_width == Inches(13.333)
    assert "Template has no" not in out

    other = build_deck_template(tmp_path / "other.pptx", **TEST_TEMPLATE | {"rename": {"Title Only": "Heading"}})
    out = fct("deck", "--release", "2026-10-R2", "--out", str(tmp_path / "b"), "--template", str(other))
    assert 'Template has no "Title Only" layout with a title; using "Title Slide" for the other slides.' in out
    assert Presentation(str(tmp_path / "b" / "Comex_2026-10_2026-10-R2.pptx")).slide_width == Inches(10)

    bad = tmp_path / "style.toml"
    bad.write_text('[colors]\ntotal = "navy"\n')
    out = fct("deck", "--release", "2026-10-R2", "--out", str(tmp_path / "c"), "--style", str(bad), code=2)
    assert "colors.total must be a 6-digit hex colour" in out
    assert not (tmp_path / "c").exists()


def test_without_config_deck_files_the_cli_writes_the_built_in_look(close, tmp_path):
    run_story(close, "E13")
    # Moved, not deleted: copies of OneDrive files keep a reparse-point attribute that rmtree refuses on Windows.
    (close.root / "config" / "deck").rename(close.root / "deck_moved_away")
    args = ["deck", "--release", "2026-10-R2", "--out", str(tmp_path), "--commentary", str(COMMENTARY),
            "--store", str(close.root / "store.duckdb"), "--config", str(close.config_path)]
    assert main(args) == 0
    assert content_digest((tmp_path / "Comex_2026-10_2026-10-R2.pptx").read_bytes()) == BUILT_IN_R2
