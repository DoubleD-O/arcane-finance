"""Deck packs: which slides a deck has, in what order, with what titles and options.

A pack file (config/deck/packs/<key>.toml) is parsed here, at the boundary, into frozen values; deck.py
renders them and trusts them. See docs/deck-styling.md, "Choosing and editing packs".
"""

from __future__ import annotations

import re
import string
import tomllib
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Callable, Mapping, Sequence

from .config import ConfigError
from .kpi import APAC
from .variance import BRIDGE_LINES

PLACEHOLDERS = ("period", "period_long", "release", "currency")
# The scorecard's KPI names -> the P&L line each shows.
SCORECARD_KPIS = {"revenue": "REV", "gross_margin": "gross_margin", "gm_pct": "gm_pct", "ebitda": "ebitda",
                  "ebitda_pct": "ebitda_pct"}
EACH_MARKET = "each_market"
DEFAULT_KEY = "comex"


@dataclass(frozen=True)
class TitleSlide:
    title: str


@dataclass(frozen=True)
class ScorecardSlide:
    title: str = "KPI scorecard, APAC"
    kpis: tuple[str, ...] = tuple(SCORECARD_KPIS)


@dataclass(frozen=True)
class PnlSlide:
    title: str = "P&L vs budget, {market}"
    scope: str = APAC  # APAC, a market code, or EACH_MARKET


@dataclass(frozen=True)
class MarketsSlide:
    title: str = "Markets"


@dataclass(frozen=True)
class BridgeSlide:
    title: str = "{kpi} bridge, budget to actual"
    kpi: str = "ebitda"


@dataclass(frozen=True)
class ChangesSlide:
    title: str = "Changes since {previous}"


@dataclass(frozen=True)
class OutlookSlide:
    title: str = "Outlook and actions"


Slide = TitleSlide | ScorecardSlide | PnlSlide | MarketsSlide | BridgeSlide | ChangesSlide | OutlookSlide
OptionParser = Callable[[object, Sequence[str]], object]  # (value, market codes) -> parsed, or raises ValueError


@dataclass(frozen=True)
class SlideType:
    spec: type
    placeholders: tuple[str, ...]  # beyond PLACEHOLDERS, for this type's title
    options: Mapping[str, OptionParser]  # every key but title and type


def _kpis(value, markets) -> tuple[str, ...]:
    if not isinstance(value, list) or not value or not all(isinstance(k, str) for k in value):
        raise ValueError(f"kpis must be a list of KPI names, got {value!r}")
    for k in value:
        if k not in SCORECARD_KPIS:
            raise ValueError(f"unknown KPI {k!r}; expected one of {', '.join(SCORECARD_KPIS)}")
    if len(set(value)) != len(value):
        raise ValueError(f"kpis lists a KPI twice: {value!r}")
    return tuple(value)


def _scope(value, markets) -> str:
    choices = (APAC, *markets, EACH_MARKET)
    if value not in choices:
        raise ValueError(f"unknown scope {value!r}; expected one of {', '.join(choices)}")
    return value


def _bridge_kpi(value, markets) -> str:
    if value not in BRIDGE_LINES:
        raise ValueError(f"unknown KPI {value!r} for a bridge; expected one of {', '.join(BRIDGE_LINES)}")
    return value


SLIDE_TYPES: dict[str, SlideType] = {
    "title": SlideType(TitleSlide, (), {}),
    "kpi_scorecard": SlideType(ScorecardSlide, (), {"kpis": _kpis}),
    "pnl": SlideType(PnlSlide, ("market",), {"scope": _scope}),
    "markets": SlideType(MarketsSlide, (), {}),
    "bridge": SlideType(BridgeSlide, ("kpi",), {"kpi": _bridge_kpi}),
    "changes": SlideType(ChangesSlide, ("previous",), {}),
    "outlook": SlideType(OutlookSlide, (), {}),
}
TYPE_NAMES = {t.spec: name for name, t in SLIDE_TYPES.items()}


@dataclass(frozen=True)
class DeckPack:
    key: str  # the file name without .toml: chooses the pack and starts the deck's file name
    name: str
    title: str  # the deck title, with placeholders
    slides: tuple[Slide, ...]
    source: str = field(default="", compare=False)

    @property
    def file_prefix(self) -> str:
        return self.key[:1].upper() + self.key[1:]

    @property
    def needs_commentary(self) -> bool:
        """Only an outlook slide shows commentary, so only a pack with one is DRAFT without it."""
        return any(isinstance(s, OutlookSlide) for s in self.slides)


def _check_title(text, allowed: Sequence[str]) -> str:
    if not isinstance(text, str) or not text.strip():
        raise ValueError(f"title must be text, got {text!r}")
    try:
        parts = list(string.Formatter().parse(text))
    except ValueError:
        raise ValueError(f"title {text!r} has an unmatched {{ or }}; write {{{{ or }}}} for a literal brace") from None
    for _, name, spec, conversion in parts:
        if name is None:
            continue
        if name not in allowed or spec or conversion:
            raise ValueError(f"title {text!r} has an unknown placeholder {{{name}{'!' + conversion if conversion else ''}"
                             f"{':' + spec if spec else ''}}}; expected {', '.join('{' + a + '}' for a in allowed)}")
    return text


def _slide(raw, n: int, source: str, markets: Sequence[str], pack_title: str) -> Slide:
    if not isinstance(raw, dict):
        raise ConfigError(f"{source}: slide {n} must be a [[slides]] table")
    kind = raw.get("type")
    if kind not in SLIDE_TYPES:
        what = "has no type" if kind is None else f"has unknown type {kind!r}"
        raise ConfigError(f"{source}: slide {n} {what}; expected one of {', '.join(SLIDE_TYPES)}")
    t = SLIDE_TYPES[kind]
    where = f"{source}: slide {n} ({kind})"
    values = {}
    try:
        for key, value in raw.items():
            if key == "type":
                continue
            if key == "title":
                values["title"] = _check_title(value, (*PLACEHOLDERS, *t.placeholders))
            elif key in t.options:
                values[key] = t.options[key](value, markets)
            else:
                expected = ", ".join(("title", *t.options))
                raise ValueError(f"unknown key {key!r}; expected {expected}")
    except ValueError as e:
        raise ConfigError(f"{where}: {e}") from None
    if t.spec is TitleSlide:
        values.setdefault("title", pack_title)
    return t.spec(**values)


def parse_pack(text: str, source: str, key: str, markets: Sequence[str]) -> DeckPack:
    """`markets`: the configured market codes a P&L slide may name."""
    try:
        doc = tomllib.loads(text)
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"{source}: not valid TOML ({e})") from None
    for k in doc:
        if k not in ("name", "title", "slides"):
            raise ConfigError(f"{source}: unknown key {k!r}; expected name, title and [[slides]]")
    name = doc.get("name")
    if not isinstance(name, str) or not name.strip():
        raise ConfigError(f'{source}: name must be text, like name = "Board pack"')
    title = doc.get("title", "APAC " + name.replace("{", "{{").replace("}", "}}") + ", {period_long}")
    try:
        _check_title(title, PLACEHOLDERS)
    except ValueError as e:
        raise ConfigError(f"{source}: {e}") from None
    slides = doc.get("slides")
    if not isinstance(slides, list) or not slides:
        raise ConfigError(f'{source}: no slides; add at least one [[slides]] table with a type, like type = "title"')
    return DeckPack(re.sub(r"[^A-Za-z0-9_-]", "_", key), name, title,
                    tuple(_slide(raw, n, source, markets, title) for n, raw in enumerate(slides, 1)), source)


def load_pack(path: Path, markets: Sequence[str]) -> DeckPack:
    path = Path(path)
    if not path.is_file():
        raise ConfigError(f"{path}: pack file not found")
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        raise ConfigError(f"{path}: not UTF-8 text") from None
    return parse_pack(text, str(path), path.stem, markets)


DEFAULT_PACK = DeckPack(DEFAULT_KEY, "Comex pack", "APAC Comex pack, {period_long}", (
    TitleSlide("APAC Comex pack, {period_long}"), ScorecardSlide(), PnlSlide(), MarketsSlide(), BridgeSlide(),
    ChangesSlide(), OutlookSlide()), "built-in")


def packs_dir(config_dir: Path) -> Path:
    return Path(config_dir) / "deck" / "packs"


def pack_keys(config_dir: Path) -> tuple[str, ...]:
    """The packs in config_dir/deck/packs, the default first. The default is there even without its file."""
    found = sorted(p.stem for p in packs_dir(config_dir).glob("*.toml")) if packs_dir(config_dir).is_dir() else []
    return (DEFAULT_KEY, *(k for k in found if k != DEFAULT_KEY))


def find_pack(config_dir: Path, choice: str | None, markets: Sequence[str]) -> DeckPack:
    """A pack by key (exactly a file stem in config_dir/deck/packs, the same on every OS) or a path to a .toml
    file; None is the default pack."""
    choice = choice or DEFAULT_KEY
    if choice.lower().endswith(".toml"):
        return load_pack(Path(choice), markets)
    path = packs_dir(config_dir) / f"{choice}.toml"
    if choice in pack_keys(config_dir) and path.is_file():
        return load_pack(path, markets)
    if choice == DEFAULT_KEY:
        return DEFAULT_PACK
    raise ConfigError(f"no deck pack {choice!r}; available: {', '.join(pack_keys(config_dir))} "
                      f"(or give a .toml file)")


def describe(slide: Slide) -> str:
    """One line for a pack listing: the type, its options, and its title."""
    parts = [TYPE_NAMES[type(slide)]]
    for f in fields(slide):
        value = getattr(slide, f.name)
        if f.name != "title":
            parts.append(f"{f.name} = {', '.join(value) if isinstance(value, tuple) else repr(value)}")
    return "  ".join([*parts, f'"{slide.title}"'])
