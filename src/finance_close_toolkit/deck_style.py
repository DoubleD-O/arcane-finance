"""The deck's look: a PowerPoint template (theme, Slide Master, layouts) and style.toml (what a theme can't reach).

Both are parsed here, at the boundary, into frozen values; deck.py trusts them. See docs/deck-styling.md.
"""

from __future__ import annotations

import io
import re
import tomllib
import zipfile
from dataclasses import dataclass
from pathlib import Path

from pptx import Presentation
from pptx.dml.color import RGBColor
from pptx.enum.shapes import PP_PLACEHOLDER
from pptx.exc import PackageNotFoundError
from pptx.opc.constants import RELATIONSHIP_TYPE as RT
from pptx.oxml import parse_xml

from .config import ConfigError

TITLE_LAYOUT = "Title Slide"
CONTENT_LAYOUT = "Title Only"


@dataclass(frozen=True)
class DeckStyle:
    """Today's look is the default for every field. table_header None leaves the template's table style."""

    favourable: RGBColor = RGBColor(0x2E, 0x7D, 0x32)
    unfavourable: RGBColor = RGBColor(0xC6, 0x28, 0x28)
    total: RGBColor = RGBColor(0x44, 0x54, 0x6A)
    table_header: RGBColor | None = None
    footer_text: RGBColor = RGBColor(0x59, 0x59, 0x59)
    table_size: int = 14
    footer_size: int = 10


DEFAULT_STYLE = DeckStyle()
_SECTIONS = {"colors": ("favourable", "unfavourable", "total", "table_header", "footer_text"),
             "fonts": ("table_size", "footer_size")}
_HEX = re.compile(r"[0-9A-Fa-f]{6}")
_SIZES = range(6, 41)


def parse_style(text: str, source: str) -> DeckStyle:
    try:
        doc = tomllib.loads(text)
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"{source}: not valid TOML ({e})") from None
    values = {}
    for section, body in doc.items():
        if section not in _SECTIONS:
            raise ConfigError(f"{source}: unknown section [{section}]; expected [colors] or [fonts]")
        if not isinstance(body, dict):
            raise ConfigError(f"{source}: {section} must be a [{section}] section")
        for key, value in body.items():
            if key not in _SECTIONS[section]:
                raise ConfigError(f"{source}: unknown key {section}.{key}; expected one of "
                                  f"{', '.join(_SECTIONS[section])}")
            name = f"{section}.{key}"
            if section == "colors":
                if not isinstance(value, str) or not _HEX.fullmatch(value):
                    raise ConfigError(f'{source}: {name} must be a 6-digit hex colour like "2E7D32", got {value!r}')
                values[key] = RGBColor.from_string(value.upper())
            else:
                if type(value) is not int or value not in _SIZES:
                    raise ConfigError(f"{source}: {name} must be a whole number of points from "
                                      f"{_SIZES.start} to {_SIZES.stop - 1}, got {value!r}")
                values[key] = value
    return DeckStyle(**values)


def load_style(path: Path) -> DeckStyle:
    if not Path(path).is_file():
        raise ConfigError(f"{path}: style file not found")
    return parse_style(Path(path).read_text(encoding="utf-8"), str(path))


@dataclass(frozen=True)
class DeckTemplate:
    """A template file's bytes and the layouts the deck will use. Each render opens a fresh copy."""

    source: str
    data: bytes
    title_layout: str
    content_layout: str
    notes: tuple[str, ...]  # what the deck did differently from the rules, e.g. a layout fallback
    table_style: str | None  # the template's default table style id, given to every table


def _default_table_style(prs) -> str | None:
    try:
        part = prs.part.part_related_by(RT.TABLE_STYLES)
    except KeyError:
        return None
    return parse_xml(part.blob).get("def") or None


def _has_title(layout) -> bool:
    return any(ph.placeholder_format.type in (PP_PLACEHOLDER.TITLE, PP_PLACEHOLDER.CENTER_TITLE)
               for ph in layout.placeholders)


def load_template(path: Path) -> DeckTemplate:
    path = Path(path)
    if not path.is_file():
        raise ConfigError(f"{path}: template file not found")
    data = path.read_bytes()
    try:
        prs = Presentation(io.BytesIO(data))
    except (PackageNotFoundError, zipfile.BadZipFile, KeyError) as e:
        raise ConfigError(f"{path}: not a PowerPoint .pptx file ({e})") from None
    layouts = list(prs.slide_layouts)
    fallback = next((layout.name for layout in layouts if _has_title(layout)), None)
    if fallback is None:
        raise ConfigError(f"{path}: no slide layout has a title placeholder; add one in View > Slide Master")
    names = {layout.name for layout in layouts if _has_title(layout)}
    chosen, notes = [], []
    for wanted, use in ((TITLE_LAYOUT, "the title slide"), (CONTENT_LAYOUT, "the other slides")):
        if wanted in names:
            chosen.append(wanted)
        else:
            chosen.append(fallback)
            notes.append(f'Template has no "{wanted}" layout with a title; using "{fallback}" for {use}.')
    return DeckTemplate(str(path), data, chosen[0], chosen[1], tuple(notes), _default_table_style(prs))


def find_styling(config_dir: Path, template: Path | None = None, style: Path | None = None
                 ) -> tuple[DeckTemplate | None, DeckStyle]:
    """The given files, else config_dir/deck/template.pptx and style.toml when present, else the built-in look."""
    deck_dir = Path(config_dir) / "deck"
    template = template or (deck_dir / "template.pptx" if (deck_dir / "template.pptx").exists() else None)
    style = style or (deck_dir / "style.toml" if (deck_dir / "style.toml").exists() else None)
    return (load_template(template) if template else None), (load_style(style) if style else DEFAULT_STYLE)
