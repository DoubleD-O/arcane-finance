"""Writes config/deck/template.pptx: the deck's starter template.

Two designs. "office" (the default output) is the shipped look: a navy and teal corporate finance theme
in Segoe UI, a navy title slide and white content slides with a slim footer band. "classic" is
python-pptx's default theme reshaped to the deck's built-in look; the test suite calls `build` with
other settings to make deliberately different templates from it.

    python scripts/make_deck_template.py [OUT] [--design office|classic]

The office design writes only theme colours, theme fonts, the Slide Master and the "Title Slide" and
"Title Only" layouts; style.toml carries the matching chart and table colours. docs/deck-styling.md
lists them.
"""

from __future__ import annotations

import argparse
import re
from pathlib import Path

from lxml import etree
from pptx import Presentation
from pptx.enum.shapes import PP_PLACEHOLDER
from pptx.opc.constants import RELATIONSHIP_TYPE as RT
from pptx.oxml.ns import qn
from pptx.util import Inches

DESIGN_W = 13.333  # inches: the deck's built-in slide width, the base for every position below
A = "http://schemas.openxmlformats.org/drawingml/2006/main"


def _xml(text: str):
    return etree.fromstring(f'<root xmlns:a="{A}">{text}</root>')[0]


def _place(shape, sx: float, left: float, top: float, width: float, height: float) -> None:
    shape.left, shape.top = Inches(left * sx), Inches(top)
    shape.width, shape.height = Inches(width * sx), Inches(height)


def _top_anchored(shape) -> None:
    shape._element.txBody.find(qn("a:bodyPr")).set("anchor", "t")


def _set_list_style(shape, lvl1: str) -> None:
    body = shape._element.txBody
    old = body.find(qn("a:lstStyle"))
    new = _xml(f"<a:lstStyle>{lvl1}</a:lstStyle>")
    old.addprevious(new)
    body.remove(old)


def _fill(color: str | None) -> str:
    return f'<a:solidFill><a:srgbClr val="{color}"/></a:solidFill>' if color else \
        '<a:solidFill><a:schemeClr val="tx1"/></a:solidFill>'


def _placeholder(container, kind):
    return next(ph for ph in container.placeholders if ph.placeholder_format.type == kind)


def _edit_theme(prs, font: str | None, accent1: str | None) -> None:
    part = prs.slide_master.part.part_related_by(RT.THEME)
    xml = part.blob.decode("utf-8")
    if font:
        xml = re.sub(r'(<a:(?:major|minor)Font><a:latin typeface=")[^"]*', rf"\g<1>{font}", xml)
    if accent1:
        xml = re.sub(r'<a:accent1><a:srgbClr val="[0-9A-Fa-f]{6}"/>', f'<a:accent1><a:srgbClr val="{accent1}"/>', xml)
    part._blob = xml.encode("utf-8")


def _new(width_in: float, height_in: float):
    """python-pptx's default presentation at this slide size, its master and layout shapes stretched across."""
    prs = Presentation()
    stretch = width_in / (prs.slide_width / 914400)
    prs.slide_width, prs.slide_height = Inches(width_in), Inches(height_in)
    for shape in [*prs.slide_master.shapes, *(s for layout in prs.slide_layouts for s in layout.shapes)]:
        if shape._element.xfrm is not None:
            shape.left, shape.width = round(shape.left * stretch), round(shape.width * stretch)
    return prs


def build(out: Path, *, width_in: float = DESIGN_W, height_in: float = 7.5, font: str | None = None,
          title_color: str | None = None, title_slide_size: int = 40, accent1: str | None = None, footer_top_in: float = 6.95,
          rename: dict[str, str] | None = None, sample_slide: bool = False) -> Path:
    prs = _new(width_in, height_in)
    master = prs.slide_master
    sx = width_in / DESIGN_W
    content_w = DESIGN_W - 1.0
    title = _placeholder(master, PP_PLACEHOLDER.TITLE)
    _place(title, sx, 0.5, 0.35, content_w, 0.8)
    _top_anchored(title)
    lvl1 = master._element.find(qn("p:txStyles")).find(qn("p:titleStyle")).find(qn("a:lvl1pPr"))
    lvl1.set("algn", "l")
    rpr = lvl1.find(qn("a:defRPr"))
    rpr.set("sz", "2800")
    rpr.set("b", "1")
    rpr.find(qn("a:solidFill")).addprevious(_xml(_fill(title_color)))
    rpr.remove(rpr.findall(qn("a:solidFill"))[1])
    _place(_placeholder(master, PP_PLACEHOLDER.FOOTER), sx, 0.5, footer_top_in, content_w, 0.35)

    layout = prs.slide_layouts.get_by_name("Title Slide")
    ctr = _placeholder(layout, PP_PLACEHOLDER.CENTER_TITLE)
    _place(ctr, sx, 0.5, 2.0, content_w, 1.0)
    _top_anchored(ctr)
    _set_list_style(ctr, f'<a:lvl1pPr algn="l"><a:defRPr sz="{title_slide_size * 100}" b="1">{_fill(title_color)}</a:defRPr></a:lvl1pPr>')
    sub = _placeholder(layout, PP_PLACEHOLDER.SUBTITLE)
    _place(sub, sx, 0.5, 3.2, content_w, 2.0)
    _top_anchored(sub)
    _set_list_style(sub, '<a:lvl1pPr marL="0" indent="0" algn="l"><a:spcBef><a:spcPts val="0"/></a:spcBef>'
                         f'<a:buNone/><a:defRPr sz="2000">{_fill(None)}</a:defRPr></a:lvl1pPr>')

    _edit_theme(prs, font, accent1)
    for old, new in (rename or {}).items():
        prs.slide_layouts.get_by_name(old).name = new
    if sample_slide:
        slide = prs.slides.add_slide(prs.slide_layouts[0])
        slide.shapes.title.text = "SAMPLE SLIDE: must not appear in a generated deck"

    out.parent.mkdir(parents=True, exist_ok=True)
    prs.save(str(out))
    return out


# The office design. Colours live in the theme, so the Slide Master's shapes and PowerPoint's own pickers
# follow a change made in Design > Variants > Colors. style.toml repeats the ones the deck code applies.

OFFICE_COLORS = {
    "dk1": "1E2530",      # body text: near-black
    "lt1": "FFFFFF",
    "dk2": "1F3864",      # navy: titles, title slide background
    "lt2": "F2F4F7",      # light grey: footer band
    "accent1": "1F3864",  # navy: table header, chart totals
    "accent2": "0F7C8C",  # teal: accent bars
    "accent3": "6B7785",  # mid grey: footer placeholders
    "accent4": "2F855A",  # favourable green (the bridge takes it from style.toml)
    "accent5": "C0392B",  # unfavourable red (likewise)
    "accent6": "8FA3BF",  # steel blue: title slide tag line
    "hlink": "0F7C8C",
    "folHlink": "6B7785",
}
OFFICE_FONTS = ("Segoe UI Semibold", "Segoe UI")  # headings, body
TABLE_STYLE_ID = "{6E25E649-3F16-4E02-A733-19D2CDBF48F0}"  # Medium Style 3 - Accent 1
BAND_TOP, FOOTER_TOP, FOOTER_H = 7.0, 7.1, 0.32  # inches on the 7.5 in slide
P = "http://schemas.openxmlformats.org/presentationml/2006/main"


def _office_theme(prs) -> None:
    part = prs.slide_master.part.part_related_by(RT.THEME)
    theme = etree.fromstring(part.blob)
    theme.set("name", "Comex Finance")
    scheme = theme.find(f".//{{{A}}}clrScheme")
    scheme.set("name", "Comex Finance")
    for slot in scheme:
        slot.clear()
        etree.SubElement(slot, f"{{{A}}}srgbClr", val=OFFICE_COLORS[etree.QName(slot).localname])
    fonts = theme.find(f".//{{{A}}}fontScheme")
    fonts.set("name", "Comex Finance")
    for tag, face in zip(("majorFont", "minorFont"), OFFICE_FONTS):
        fonts.find(f"{{{A}}}{tag}/{{{A}}}latin").set("typeface", face)
    part._blob = etree.tostring(theme, xml_declaration=True, encoding="UTF-8", standalone=True)


def _office_table_style(prs) -> None:
    """Make Medium Style 3 - Accent 1 the default table style: navy header, light grey banding, no grid lines.

    PowerPoint draws only its built-in table styles, so the template picks one rather than defining its own.
    """
    part = prs.part.part_related_by(RT.TABLE_STYLES)
    part._blob = (b'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
                  + f'<a:tblStyleLst xmlns:a="{A}" def="{TABLE_STYLE_ID}"/>'.encode("utf-8"))


def _add(container, name: str, rect: tuple[float, float, float, float], color: str,
         text: str | None = None, run_attrs: str = "") -> None:
    """A plain shape behind a master's or layout's placeholders: a scheme-coloured bar, or a line of text."""
    tree = container._element.find(f"{{{P}}}cSld/{{{P}}}spTree")
    shape_id = max(int(e.get("id")) for e in tree.iter(f"{{{P}}}cNvPr")) + 1
    x, y, w, h = (int(Inches(v)) for v in rect)
    if text is None:
        nv, look = "<p:cNvSpPr/>", f'<a:solidFill><a:schemeClr val="{color}"/></a:solidFill><a:ln><a:noFill/></a:ln>'
        body = '<a:bodyPr rtlCol="0" anchor="ctr"/><a:lstStyle/><a:p><a:endParaRPr lang="en-US"/></a:p>'
    else:
        nv, look = '<p:cNvSpPr txBox="1"/>', "<a:noFill/>"
        body = ('<a:bodyPr wrap="square" lIns="0" tIns="0" rIns="0" bIns="0" rtlCol="0" anchor="t"/><a:lstStyle/>'
                f'<a:p><a:r><a:rPr lang="en-US" {run_attrs}><a:solidFill><a:schemeClr val="{color}"/>'
                f"</a:solidFill></a:rPr><a:t>{text}</a:t></a:r></a:p>")
    sp = etree.fromstring(
        f'<p:sp xmlns:p="{P}" xmlns:a="{A}"><p:nvSpPr><p:cNvPr id="{shape_id}" name="{name}"/>{nv}'
        f'<p:nvPr userDrawn="1"/></p:nvSpPr><p:spPr><a:xfrm><a:off x="{x}" y="{y}"/><a:ext cx="{w}" cy="{h}"/>'
        f'</a:xfrm><a:prstGeom prst="rect"><a:avLst/></a:prstGeom>{look}</p:spPr><p:txBody>{body}</p:txBody></p:sp>')
    tree.find(f"{{{P}}}grpSpPr").addnext(sp)


def _background(container, color: str) -> None:
    csld = container._element.find(f"{{{P}}}cSld")
    old = csld.find(f"{{{P}}}bg")
    if old is not None:
        csld.remove(old)
    csld.insert(0, etree.fromstring(
        f'<p:bg xmlns:p="{P}" xmlns:a="{A}"><p:bgPr><a:solidFill><a:schemeClr val="{color}"/></a:solidFill>'
        "<a:effectLst/></p:bgPr></p:bg>"))


def _footer_area(master, kind, left: float, width: float, align: str) -> None:
    shape = _placeholder(master, kind)
    _place(shape, 1, left, FOOTER_TOP, width, FOOTER_H)
    _top_anchored(shape)
    _set_list_style(shape, f'<a:lvl1pPr algn="{align}"><a:defRPr sz="1000">'
                           '<a:solidFill><a:schemeClr val="accent3"/></a:solidFill></a:defRPr></a:lvl1pPr>')


def build_office(out: Path) -> Path:
    prs = _new(DESIGN_W, 7.5)
    master, w = prs.slide_master, DESIGN_W

    # Master: white, a slim light-grey footer band holding the footer, navy semibold titles top left.
    _background(master, "bg1")
    _add(master, "Footer band", (0, BAND_TOP, w, 7.5 - BAND_TOP), "bg2")
    title = _placeholder(master, PP_PLACEHOLDER.TITLE)
    _place(title, 1, 0.5, 0.3, w - 1.0, 0.68)
    title._element.txBody.find(qn("a:bodyPr")).set("anchor", "b")
    lvl1 = master._element.find(qn("p:txStyles")).find(qn("p:titleStyle")).find(qn("a:lvl1pPr"))
    lvl1.set("algn", "l")
    rpr = lvl1.find(qn("a:defRPr"))
    rpr.set("sz", "2600")
    rpr.set("b", "0")  # Segoe UI Semibold is already the weight; bold on top of it would fake a heavier one
    rpr.find(qn("a:solidFill")).find(qn("a:schemeClr")).set("val", "tx2")
    _footer_area(master, PP_PLACEHOLDER.FOOTER, 0.5, 9.33, "l")
    _footer_area(master, PP_PLACEHOLDER.DATE, 9.83, 2.0, "r")
    _footer_area(master, PP_PLACEHOLDER.SLIDE_NUMBER, 11.83, 1.0, "r")

    # Title Only: a short teal bar under the title, lined up with the title text.
    _add(prs.slide_layouts.get_by_name("Title Only"), "Accent bar", (0.6, 1.04, 0.9, 0.05), "accent2")

    # Title Slide: navy, a tag line, a teal bar over a large white title, light grey release and status lines.
    layout = prs.slide_layouts.get_by_name("Title Slide")
    _background(layout, "tx2")
    _add(layout, "Accent bar", (0.6, 2.2, 1.1, 0.08), "accent2")
    _add(layout, "Tag line", (0.6, 0.6, 8.0, 0.3), "accent6", "APAC FINANCE  |  EXECUTIVE COMMITTEE",
         'sz="1100" spc="150"')
    ctr = _placeholder(layout, PP_PLACEHOLDER.CENTER_TITLE)
    _place(ctr, 1, 0.5, 2.4, w - 1.0, 1.0)
    _top_anchored(ctr)
    _set_list_style(ctr, '<a:lvl1pPr algn="l"><a:defRPr sz="4000" b="0"><a:solidFill><a:schemeClr val="bg1"/>'
                         "</a:solidFill></a:defRPr></a:lvl1pPr>")
    sub = _placeholder(layout, PP_PLACEHOLDER.SUBTITLE)
    _place(sub, 1, 0.5, 3.55, w - 1.0, 2.4)
    _top_anchored(sub)
    _set_list_style(sub, '<a:lvl1pPr marL="0" indent="0" algn="l"><a:spcBef><a:spcPts val="600"/></a:spcBef>'
                         '<a:buNone/><a:defRPr sz="1800"><a:solidFill><a:srgbClr val="D3DBE6"/></a:solidFill>'
                         "</a:defRPr></a:lvl1pPr>")

    _office_theme(prs)
    _office_table_style(prs)
    out.parent.mkdir(parents=True, exist_ok=True)
    prs.save(str(out))
    return out


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Write the deck's starter template.")
    parser.add_argument("out", nargs="?", type=Path,
                        default=Path(__file__).resolve().parent.parent / "config" / "deck" / "template.pptx")
    parser.add_argument("--design", choices=("office", "classic"), default="office")
    args = parser.parse_args()
    print(f"Wrote {(build_office if args.design == 'office' else build)(args.out)}")
