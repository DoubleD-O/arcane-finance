"""Comex deck: renders a VarianceReport (and optional commentary) with python-pptx, slide by slide as a pack
(deck_pack.py) lists them. Computes no figures."""

from __future__ import annotations

import io
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Callable, Sequence

from pptx import Presentation
from pptx.chart.data import CategoryChartData
from pptx.dml.color import RGBColor
from pptx.enum.chart import XL_CHART_TYPE, XL_LABEL_POSITION, XL_TICK_LABEL_POSITION
from pptx.enum.shapes import PP_PLACEHOLDER
from pptx.enum.text import PP_ALIGN
from pptx.oxml.ns import qn
from pptx.util import Inches, Pt

from .commentary import Commentary
from .config import ConfigError
from .deck_pack import (DEFAULT_PACK, EACH_MARKET, SCORECARD_KPIS, SLIDE_TYPES, BridgeSlide, ChangesSlide, DeckPack,
                        TYPE_NAMES, MarketsSlide, OutlookSlide, PnlSlide, ScorecardSlide, Slide, TitleSlide)
from .deck_style import (DEFAULT_STYLE, DeckStyle, DeckTemplate, find_styling, load_style, load_template,  # noqa: F401
                         parse_style)
from .kpi import APAC, KPI
from .money import ZERO, fmt_money, fmt_percent, fmt_pp, fmt_step
from .variance import BRIDGE_LINES, MARKET_SUMMARY_LINES, PNL_LINES, BridgeStep, VarianceReport, VarianceRow

NO_COMMENTARY = "Outlook and actions not provided for this release."
LINE_LABELS = {line.key: line.label for line in PNL_LINES}

class DeckExists(FileExistsError):
    pass


def money(value) -> str:
    return fmt_money(value, 0)


def pct(value) -> str:
    return fmt_percent(value, 1)


# Table contents: the report in the deck's display conventions. The parity test reads these back.


def row_cells(row: VarianceRow) -> list[str]:
    if row.line.kind == "ratio":
        return [row.line.label, pct(row.actual), pct(row.budget), fmt_pp(row.variance), "", row.label]
    return [row.line.label, money(row.actual), money(row.budget), money(row.variance), pct(row.variance_pct), row.label]


PNL_HEADER = ["", "Actual", "Budget", "Variance", "Var %", "Fav / Unfav"]


def scorecard_table(report: VarianceReport, kpis: Sequence[str] = tuple(SCORECARD_KPIS)) -> list[list[str]]:
    return [PNL_HEADER, *(row_cells(report.row(APAC, SCORECARD_KPIS[k])) for k in kpis)]


def pnl_table(report: VarianceReport, scope: str = APAC) -> list[list[str]]:
    return [PNL_HEADER, *(row_cells(r) for r in report.scope(scope))]


def markets_table(report: VarianceReport) -> list[list[str]]:
    header = ["Market"]
    for key in MARKET_SUMMARY_LINES:
        header += [LINE_LABELS[key], f"{LINE_LABELS[key]} var"]
    rows = [header]
    for scope in (*(m.code for m in report.markets), APAC):
        cells = [scope]
        for key in MARKET_SUMMARY_LINES:
            r = report.row(scope, key)
            cells += [money(r.actual), money(r.variance)]
        rows.append(cells)
    return rows


def bridge_table(report: VarianceReport, kpi: str = "ebitda") -> list[list[str]]:
    b, label = report.bridges[kpi], KPI[kpi].label
    return [
        [f"{label} budget", *(s.market for s in b.steps), f"{label} actual"],
        [money(b.budget), *(fmt_step(s.amount, 0) for s in b.steps), money(b.actual)],
        ["", *(report.row(s.market, BRIDGE_LINES[kpi]).label for s in b.steps), ""],
    ]


def changes_table(report: VarianceReport) -> list[list[str]]:
    assert report.changes is not None
    rows = [["Scenario", "Scope", "KPI", report.previous_release_id, report.name, "Change", "Change %", "Materiality"]]
    for k in report.changes.kpis:
        rows.append([k.scenario, k.scope, k.kpi.label, money(k.before), money(k.after), money(k.change),
                     pct(k.pct), "Material" if k.material else "Below threshold"])
    return rows


def actions_table(commentary: Commentary) -> list[list[str]]:
    return [["Action", "Owner", "Due"], *([a.action, a.owner, a.due.isoformat()] for a in commentary.actions)]


@dataclass(frozen=True)
class ChartSeries:
    name: str
    values: tuple[float, ...]
    color: RGBColor | None  # None: invisible


@dataclass(frozen=True)
class BridgeChart:
    """The waterfall as a stacked column chart.

    PowerPoint stacks positive values up from zero and negative values down from zero, each in
    series order. So each bar [lo, hi] is split at zero: an invisible base fills the gap between
    zero and a bar that does not touch zero, then a visible part above zero and one below. Each
    label is centred in an invisible box stacked just outside its bar: above it when the bar
    reaches above zero, below it otherwise.
    """

    categories: tuple[str, ...]
    series: tuple[ChartSeries, ...]
    labels: tuple[str, ...]
    label_colors: tuple[RGBColor, ...]
    label_series: tuple[int, ...]  # per category, the index of the series carrying its label


@dataclass(frozen=True)
class _Bar:
    kind: str  # Total, Favourable or Unfavourable
    color: RGBColor
    lo: Decimal
    hi: Decimal


def bridge_chart(report: VarianceReport, style: DeckStyle = DEFAULT_STYLE, kpi: str = "ebitda") -> BridgeChart:
    b = report.bridges[kpi]

    def total(v: Decimal) -> _Bar:
        return _Bar("Total", style.total, min(ZERO, v), max(ZERO, v))

    def step(s: BridgeStep) -> _Bar:
        kind, color = ("Favourable", style.favourable) if s.amount > 0 else ("Unfavourable", style.unfavourable)
        return _Bar(kind, color, min(s.before, s.after), max(s.before, s.after))

    bars = [total(b.budget), *(step(s) for s in b.steps), total(b.actual)]
    series = [ChartSeries("Base", tuple(float(x.lo if x.lo > 0 else x.hi if x.hi < 0 else 0) for x in bars), None)]
    for kind, color in (("Total", style.total), ("Favourable", style.favourable),
                        ("Unfavourable", style.unfavourable)):
        mine = [x if x.kind == kind else None for x in bars]
        series.append(ChartSeries(kind, tuple(
            float(x.hi - max(x.lo, ZERO)) if x and x.hi > 0 else 0.0 for x in mine), color))
        series.append(ChartSeries(f"{kind} below zero", tuple(
            float(x.lo - min(x.hi, ZERO)) if x and x.lo < 0 else 0.0 for x in mine), color))
    span = max(ZERO, *(x.hi for x in bars)) - min(ZERO, *(x.lo for x in bars))
    room = float(span / 10) or 1.0  # a label box a tenth of the chart tall
    above, below = len(series), len(series) + 1
    series.append(ChartSeries("Label above", tuple(room if x.hi > 0 else 0.0 for x in bars), None))
    series.append(ChartSeries("Label below", tuple(0.0 if x.hi > 0 else -room for x in bars), None))
    return BridgeChart(
        categories=("Budget", *(s.market for s in b.steps), "Actual"),
        series=tuple(series),
        labels=(money(b.budget), *(fmt_step(s.amount, 0) for s in b.steps), money(b.actual)),
        label_colors=tuple(x.color for x in bars),
        label_series=tuple(above if x.hi > 0 else below for x in bars),
    )


def draft_reasons(report: VarianceReport, commentary: Commentary | None,
                  pack: DeckPack = DEFAULT_PACK) -> tuple[str, ...]:
    reasons = []
    if report.status == "draft":
        reasons.append("proposed release, not approved")
    if commentary is None and pack.needs_commentary:
        reasons.append("no commentary")
    return tuple(reasons)


def status_line(report: VarianceReport, commentary: Commentary | None, pack: DeckPack = DEFAULT_PACK) -> str:
    """The title slide's status. Approved figures in a pack without commentary must not read as unapproved."""
    if report.release_id is not None and commentary is None and pack.needs_commentary:
        return f"Figures approved (release {report.release_id}). Pack DRAFT: outlook and actions not provided."
    reasons = draft_reasons(report, commentary, pack)
    return f"Status: DRAFT ({'; '.join(reasons)})" if reasons else "Status: FINAL"


def footer_text(report: VarianceReport, commentary: Commentary | None, pack: DeckPack = DEFAULT_PACK) -> str:
    parts = [report.name, report.period, report.currency_label, f"checksum {report.facts_checksum[:12]}"]
    if draft_reasons(report, commentary, pack):
        parts.append("DRAFT")
    return " · ".join(parts)


def title_values(report: VarianceReport) -> dict[str, str]:
    """What a pack's title placeholders stand for (deck_pack.PLACEHOLDERS)."""
    return {"period": report.period, "period_long": datetime.strptime(report.period, "%Y-%m").strftime("%B %Y"),
            "release": report.name, "currency": report.currency}


def deck_title(report: VarianceReport, pack: DeckPack = DEFAULT_PACK) -> str:
    return pack.title.format(**title_values(report))


def deck_file_name(report: VarianceReport, pack: DeckPack = DEFAULT_PACK) -> str:
    suffix = f"_{report.currency}" if report.conversion else ""
    return f"{pack.file_prefix}_{report.period}_{report.name}{suffix}.pptx"


MAX_TITLE = 100


def pnl_scopes(report: VarianceReport, spec: PnlSlide) -> list[str]:
    return [m.code for m in report.markets] if spec.scope == EACH_MARKET else [spec.scope]


def slide_titles(report: VarianceReport, spec: Slide) -> tuple[str, ...]:
    """The title of each slide `spec` adds: none for changes without a previous release, one per market for a
    P&L on each_market."""
    values = title_values(report)
    if isinstance(spec, ChangesSlide):
        return () if report.changes is None else (spec.title.format(**values, previous=report.previous_release_id),)
    if isinstance(spec, PnlSlide):
        return tuple(spec.title.format(**values, market=scope) for scope in pnl_scopes(report, spec))
    if isinstance(spec, BridgeSlide):
        return (spec.title.format(**values, kpi=KPI[spec.kpi].label),)
    return (spec.title.format(**values),)


def plan(report: VarianceReport, pack: DeckPack) -> list[tuple[Slide, tuple[str, ...]]]:
    """Each slide spec with the titles it renders. Raises ConfigError, naming the slide, for a title over
    MAX_TITLE characters, and when no slide renders for this release."""
    out = []
    for n, spec in enumerate(pack.slides, 1):
        titles = slide_titles(report, spec)
        for title in titles:
            if len(title) > MAX_TITLE:
                raise ConfigError(f"{pack.source}: slide {n} ({TYPE_NAMES[type(spec)]}) title is {len(title)} "
                                  f"characters after placeholders; the most is {MAX_TITLE}: {title!r}")
        out.append((spec, titles))
    if not any(titles for _, titles in out):
        raise ConfigError(f"{pack.source}: no slides to render for this release ({report.name}); every slide in "
                          "the pack is skipped")
    return out


# Rendering. Positions are designed for a 13.333 x 7.5 in slide with the footer at 6.95 in, then scaled
# to the template's slide: across by its width, down by where its footer starts.

_W, _H = Inches(13.333), Inches(7.5)
_MARGIN = Inches(0.5)
_CONTENT_W = _W - 2 * _MARGIN
_TOP = Inches(1.3)
_ROW_H = Inches(0.36)
_FOOTER = (_MARGIN, Inches(6.95), _CONTENT_W, Inches(0.35))

Rect = tuple[int, int, int, int]


@dataclass(frozen=True)
class _Page:
    prs: object
    style: DeckStyle
    template: DeckTemplate | None  # None: the built-in look, every title a plain text box
    sx: float
    sy: float
    footer: Rect

    def x(self, v: int) -> int:
        return round(v * self.sx)

    def y(self, v: int) -> int:
        return round(v * self.sy)

    def rect(self, left: int, top: int, width: int, height: int) -> Rect:
        return self.x(left), self.y(top), self.x(width), self.y(height)


def _footer_placeholder(prs, layout_name: str) -> Rect | None:
    """The content layout's footer area, else the Slide Master's, if it sits in the bottom half of the slide."""
    for owner in (prs.slide_layouts.get_by_name(layout_name), prs.slide_master):
        for ph in owner.placeholders:
            if ph.placeholder_format.type == PP_PLACEHOLDER.FOOTER:
                if prs.slide_height / 2 < ph.top and ph.top + ph.height <= prs.slide_height:
                    return ph.left, ph.top, ph.width, ph.height
                return None
    return None


def _open(template: DeckTemplate | None, style: DeckStyle) -> _Page:
    if template is None:
        prs = Presentation()
        prs.slide_width, prs.slide_height = _W, _H
        return _Page(prs, style, None, 1.0, 1.0, _FOOTER)
    prs = Presentation(io.BytesIO(template.data))
    slide_ids = prs.slides._sldIdLst
    for sld in list(slide_ids):
        slide_ids.remove(sld)
        prs.part.rels.pop(sld.rId)
    sx = prs.slide_width / _W
    footer = _footer_placeholder(prs, template.content_layout)
    if footer is None:
        sy = prs.slide_height / _H
        footer = (round(_FOOTER[0] * sx), round(_FOOTER[1] * sy), round(_FOOTER[2] * sx), round(_FOOTER[3] * sy))
    return _Page(prs, style, template, sx, footer[1] / _FOOTER[1], footer)


def _write(tf, lines: Sequence[str], size: int | None, bold: bool | None = None, color: RGBColor | None = None):
    for i, line in enumerate(lines):
        p = tf.paragraphs[0] if i == 0 else tf.add_paragraph()
        run = p.add_run()
        run.text = line
        if size is not None:
            run.font.size = Pt(size)
        if bold is not None:
            run.font.bold = bold
        if color is not None:
            run.font.color.rgb = color


def _text_box(slide, rect: Rect, lines: Sequence[str], size: int, bold: bool = False,
              color: RGBColor | None = None):
    box = slide.shapes.add_textbox(*rect)
    tf = box.text_frame
    tf.word_wrap = True
    _write(tf, lines, size, bold, color)
    return box


def _template_slide(page: _Page, title: str, lines: Sequence[str] | None):
    """A slide on the template's layout: the title (and title-slide lines) in its placeholders, the rest removed."""
    t = page.template
    layout = page.prs.slide_layouts.get_by_name(t.title_layout if lines else t.content_layout)
    slide = page.prs.slides.add_slide(layout)
    filled = [slide.shapes.title]
    _write(slide.shapes.title.text_frame, [title], None)
    heading = page.rect(_MARGIN, Inches(0.35), _CONTENT_W, Inches(0.8))
    if not lines and slide.shapes.title.top + slide.shapes.title.height > page.y(_TOP):
        # A fallback layout (e.g. "Title Slide") puts its title where the tables go.
        slide.shapes.title.left, slide.shapes.title.top, slide.shapes.title.width, slide.shapes.title.height = heading
    if lines:
        body = next((ph for ph in slide.placeholders
                     if ph.placeholder_format.type in (PP_PLACEHOLDER.SUBTITLE, PP_PLACEHOLDER.BODY)), None)
        if body is None:
            _text_box(slide, page.rect(_MARGIN, Inches(3.2), _CONTENT_W, Inches(2.0)), lines, 20)
        else:
            _write(body.text_frame, lines, None)
            filled.append(body)
    for ph in list(slide.placeholders):
        if ph not in filled:
            ph._element.getparent().remove(ph._element)
    return slide


@dataclass(frozen=True)
class _Deck:
    """One render: the page and what every slide renderer reads."""

    page: _Page
    report: VarianceReport
    commentary: Commentary | None
    pack: DeckPack


def _footer(d: _Deck, slide) -> None:
    _text_box(slide, d.page.footer, [footer_text(d.report, d.commentary, d.pack)], d.page.style.footer_size,
              color=d.page.style.footer_text)


def _slide(d: _Deck, title: str):
    page = d.page
    if page.template is not None:
        slide = _template_slide(page, title, None)
    else:
        slide = page.prs.slides.add_slide(page.prs.slide_layouts[6])
        _text_box(slide, page.rect(_MARGIN, Inches(0.35), _CONTENT_W, Inches(0.8)), [title], 28, bold=True)
    _footer(d, slide)
    return slide


def _table(page: _Page, slide, rows: list[list[str]], top, widths: Sequence[float], smaller: int = 0,
           bold_rows: Sequence[int] = (), left_cols: Sequence[int] = (0,)):
    """`smaller`: points below the style's table size, for the denser tables."""
    total = sum(widths)
    left = _MARGIN + int((_CONTENT_W - Inches(total)) / 2)
    shape = slide.shapes.add_table(len(rows), len(widths), *page.rect(left, top, Inches(total), _ROW_H * len(rows)))
    table = shape.table
    if page.template is not None and page.template.table_style:
        table._tbl.tblPr.find(qn("a:tableStyleId")).text = page.template.table_style
    for j, w in enumerate(widths):
        table.columns[j].width = page.x(Inches(w))
    header = page.style.table_header
    for i, cells in enumerate(rows):
        table.rows[i].height = page.y(_ROW_H)
        for j, text in enumerate(cells):
            cell = table.cell(i, j)
            cell.margin_top = cell.margin_bottom = Inches(0.03)
            if i == 0 and header is not None:
                cell.fill.solid()
                cell.fill.fore_color.rgb = header
            p = cell.text_frame.paragraphs[0]
            p.alignment = PP_ALIGN.LEFT if j in left_cols else PP_ALIGN.RIGHT
            run = p.add_run()
            run.text = text
            run.font.size = Pt(page.style.table_size - smaller)
            run.font.bold = i == 0 or i in bold_rows
    return shape


# One renderer per slide type in deck_pack.SLIDE_TYPES. Each adds one slide per title `plan` gives it: none
# (changes without a previous release) or several (a P&L for each market).


def _title_slide(d: _Deck, spec: TitleSlide, titles: tuple[str, ...]) -> None:
    page, report, commentary = d.page, d.report, d.commentary
    if report.release_id is None:
        release = "Proposed release, not approved"
        approval = f"Latest data received {report.as_of:%Y-%m-%d %H:%M}."
        if report.previous_release_id:
            approval += f" Current release: {report.previous_release_id}."
    else:
        release = f"Release {report.release_id}"
        approval = f"Approved by {report.approved_by} on {report.approved_at:%Y-%m-%d %H:%M}."
    currency = f"Currency: {report.currency_label}" if report.conversion else         f"Reporting currency: {report.reporting_currency}"
    lines = [release, approval, currency, *filter(None, [report.translation_note]),
             status_line(report, commentary, d.pack)]
    [title] = titles
    if page.template is not None:
        _footer(d, _template_slide(page, title, lines))
        return
    slide = page.prs.slides.add_slide(page.prs.slide_layouts[6])
    _footer(d, slide)
    _text_box(slide, page.rect(_MARGIN, Inches(2.0), _CONTENT_W, Inches(1.0)), [title], 40, bold=True)
    _text_box(slide, page.rect(_MARGIN, Inches(3.2), _CONTENT_W, Inches(2.0)), lines, 20)


def _scorecard_slide(d: _Deck, spec: ScorecardSlide, titles: tuple[str, ...]) -> None:
    slide = _slide(d, titles[0])
    _table(d.page, slide, scorecard_table(d.report, spec.kpis), Inches(1.6), [3.0, 2.0, 2.0, 2.0, 1.4, 2.0],
           left_cols=(0, 5))


def _pnl_slide(d: _Deck, spec: PnlSlide, titles: tuple[str, ...]) -> None:
    report = d.report
    subtotals = [i for i, line in enumerate(PNL_LINES, 1) if line.kind == "subtotal"]
    for scope, title in zip(pnl_scopes(report, spec), titles, strict=True):
        slide = _slide(d, title)
        rows = pnl_table(report, scope)
        _table(d.page, slide, rows, _TOP, [3.2, 1.9, 1.9, 1.9, 1.3, 1.9], bold_rows=subtotals, left_cols=(0, 5))
        notes = (*filter(None, [report.translation_note]), *(report.fx_notes(scope) if scope != APAC else ()))
        if notes:
            top = _TOP + _ROW_H * len(rows) + Inches(0.1)
            _text_box(slide, d.page.rect(_MARGIN, top, _CONTENT_W, Inches(0.5)), notes, 12)


def _markets_slide(d: _Deck, spec: MarketsSlide, titles: tuple[str, ...]) -> None:
    slide = _slide(d, titles[0])
    rows = markets_table(d.report)
    _table(d.page, slide, rows, Inches(1.6), [1.3] + [1.75] * 6, bold_rows=[len(rows) - 1])
    notes = d.report.fx_notes()
    if notes:
        top = Inches(1.6) + _ROW_H * len(rows) + Inches(0.4)
        _text_box(slide, d.page.rect(_MARGIN, top, _CONTENT_W, Inches(1.0)), notes, 14)


def _bridge_slide(d: _Deck, spec: BridgeSlide, titles: tuple[str, ...]) -> None:
    page, report = d.page, d.report
    slide = _slide(d, titles[0])
    c = bridge_chart(report, page.style, spec.kpi)
    data = CategoryChartData(number_format="#,##0")
    data.categories = c.categories
    for series_spec in c.series:
        data.add_series(series_spec.name, series_spec.values)
    frame = slide.shapes.add_chart(XL_CHART_TYPE.COLUMN_STACKED,
                                   *page.rect(_MARGIN + Inches(1.0), _TOP, _CONTENT_W - Inches(2.0), Inches(4.0)), data)
    chart = frame.chart
    chart.has_legend = False
    chart.font.size = Pt(page.style.table_size)
    plot = chart.plots[0]
    plot.gap_width = 60
    plot.overlap = 100
    for series, series_spec in zip(plot.series, c.series, strict=True):
        series.invert_if_negative = False
        if series_spec.color is None:
            series.format.fill.background()
            series.format.line.fill.background()
        else:
            series.format.fill.solid()
            series.format.fill.fore_color.rgb = series_spec.color
    for i, (text, color, carrier) in enumerate(zip(c.labels, c.label_colors, c.label_series, strict=True)):
        dl = plot.series[carrier].points[i].data_label
        dl.position = XL_LABEL_POSITION.CENTER
        dl.text_frame.text = text
        run = dl.text_frame.paragraphs[0].runs[0]
        run.font.size = Pt(page.style.table_size)
        run.font.bold = True
        run.font.color.rgb = color
    # Category names along the bottom, not on the zero line where bars below zero would cover them.
    chart.category_axis.tick_label_position = XL_TICK_LABEL_POSITION.LOW
    chart.value_axis.has_major_gridlines = False
    chart.value_axis.tick_labels.number_format = "#,##0"
    chart.value_axis.tick_labels.number_format_is_linked = False
    steps = len(report.bridges[spec.kpi].steps)
    _table(page, slide, bridge_table(report, spec.kpi), Inches(5.45), [2.2] + [1.8] * steps + [2.2],
           smaller=2, left_cols=())


def _changes_slide(d: _Deck, spec: ChangesSlide, titles: tuple[str, ...]) -> None:
    if not titles:
        return
    slide = _slide(d, titles[0])
    rows = changes_table(d.report)
    _table(d.page, slide, rows, _TOP, [1.2, 1.0, 1.7, 1.6, 1.6, 1.6, 1.3, 2.2], smaller=1,
           left_cols=(0, 1, 2, 7))
    notes = d.report.changes_notes
    if notes:
        top = _TOP + _ROW_H * len(rows) + Inches(0.1)
        _text_box(slide, d.page.rect(_MARGIN, top, _CONTENT_W, Inches(0.5)), notes, 12)


def _outlook_slide(d: _Deck, spec: OutlookSlide, titles: tuple[str, ...]) -> None:
    page, commentary = d.page, d.commentary
    slide = _slide(d, titles[0])
    if commentary is None:
        _text_box(slide, page.rect(_MARGIN, Inches(2.0), _CONTENT_W, Inches(1.0)), [NO_COMMENTARY], 20)
        return
    _text_box(slide, page.rect(_MARGIN, _TOP, _CONTENT_W, Inches(0.45)), ["Outlook"], 18, bold=True)
    # Sized for the commentary limits: a 600-character outlook, six 120-character actions.
    _text_box(slide, page.rect(_MARGIN, Inches(1.7), _CONTENT_W, Inches(1.3)), [commentary.outlook], 16)
    if commentary.actions:
        _text_box(slide, page.rect(_MARGIN, Inches(3.2), _CONTENT_W, Inches(0.45)), ["Actions"], 18, bold=True)
        _table(page, slide, actions_table(commentary), Inches(3.62), [7.9, 2.8, 1.5], smaller=2,
               left_cols=(0, 1, 2))


_RENDERERS: dict[type, Callable[[_Deck, Slide, tuple[str, ...]], None]] = {
    TitleSlide: _title_slide,
    ScorecardSlide: _scorecard_slide,
    PnlSlide: _pnl_slide,
    MarketsSlide: _markets_slide,
    BridgeSlide: _bridge_slide,
    ChangesSlide: _changes_slide,
    OutlookSlide: _outlook_slide,
}
assert set(_RENDERERS) == {t.spec for t in SLIDE_TYPES.values()}, "every slide type needs a renderer"


def render(report: VarianceReport, commentary: Commentary | None, template: DeckTemplate | None = None,
           style: DeckStyle | None = None, pack: DeckPack = DEFAULT_PACK) -> bytes:
    """Raises ConfigError (plan) before rendering anything when the pack cannot make a deck for this release."""
    slides = plan(report, pack)
    page = _open(template, style or DEFAULT_STYLE)
    props = page.prs.core_properties
    props.title = deck_title(report, pack)
    props.subject = f"{report.name} {report.period}"
    props.author = props.last_modified_by = "finance-close-toolkit"
    props.created = props.modified = report.as_of
    props.revision = 1
    d = _Deck(page, report, commentary, pack)
    for spec, titles in slides:
        _RENDERERS[type(spec)](d, spec, titles)
    out = io.BytesIO()
    page.prs.save(out)
    return out.getvalue()


def write_deck(report: VarianceReport, commentary: Commentary | None, out_dir: Path, force: bool = False,
               template: DeckTemplate | None = None, style: DeckStyle | None = None,
               pack: DeckPack = DEFAULT_PACK) -> Path:
    """Writes DIR/<Pack>_<period>_<release>.pptx (Comex_... for the default pack), with _<currency> added for a
    converted report. Refuses to replace an existing file unless `force`.

    No template and no style is the built-in look; `find_styling` loads a workspace's config/deck files and
    `find_pack` its packs.
    """
    out_dir = Path(out_dir)
    path = out_dir / deck_file_name(report, pack)
    if path.exists() and not force:
        raise DeckExists(f"{path} already exists; use --force to replace it")
    data = render(report, commentary, template, style, pack)
    out_dir.mkdir(parents=True, exist_ok=True)
    with open(path, "wb" if force else "xb") as f:
        f.write(data)
    return path
