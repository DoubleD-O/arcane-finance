import re
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

ZERO = Decimal(0)
_CENT = Decimal("0.01")
STORE_SCALE = Decimal("1e-10")
_THOUSANDS = re.compile(r"^-?\d{1,3}(,\d{3})+(\.\d+)?$")


def parse_amount(text: str) -> Decimal:
    """Parse a submitted amount. Blank and "-" are zero; "(1,234.50)" is negative.
    Commas count only as thousands separators, so "1.234,50" and "1,5" are not numbers.

    Raises ValueError for anything else that is not a number.
    """
    s = text.strip()
    if s in ("", "-"):
        return ZERO
    negative = s.startswith("(") and s.endswith(")")
    if negative:
        s = s[1:-1].strip()
    if "," in s:
        if not _THOUSANDS.match(s):
            raise ValueError(f"not a number: {text!r}")
        s = s.replace(",", "")
    try:
        value = Decimal(s)
    except InvalidOperation:
        raise ValueError(f"not a number: {text!r}") from None
    if not value.is_finite():
        raise ValueError(f"not a number: {text!r}")
    return -value if negative else value


def canonical(value: Decimal) -> str:
    """Fixed 10-dp text, the store's scale, so equal amounts hash equally."""
    return format(value.quantize(STORE_SCALE), "f")


def cents(value: Decimal) -> Decimal:
    """A computed amount rounded to cents, half up, where it is created."""
    return value.quantize(_CENT, rounding=ROUND_HALF_UP)


def rate_text(rate: Decimal) -> str:
    """An exchange rate as quoted, with at least 2 dp: 1.30, 0.8612."""
    return format(rate if rate.as_tuple().exponent <= -2 else rate.quantize(_CENT), "f")


def plain(value: Decimal) -> str:
    """Shortest exact text without exponent, for exports."""
    if value == 0:
        return "0"
    text = format(value, "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


def _round(value: Decimal, dp: int) -> Decimal:
    rounded = value.quantize(Decimal(1).scaleb(-dp), rounding=ROUND_HALF_UP)
    return abs(rounded) if rounded == 0 else rounded


def fmt_amount(value: Decimal) -> str:
    return f"{_round(value, 2):,.2f}"


def fmt_pct(value: Decimal | None) -> str:
    return fmt_percent(value, 2)


def fmt_money(value: Decimal, dp: int = 2) -> str:
    """Report style: thousands separators, negatives in parentheses: (450,000)."""
    rounded = _round(value, dp)
    text = f"{abs(rounded):,.{dp}f}"
    return f"({text})" if rounded < 0 else text


def fmt_quantity(value: Decimal) -> str:
    """Units: thousands separators, up to two decimals, no trailing zeros: 2,000 or 1,627.2."""
    return f"{_round(value, 2):,.2f}".rstrip("0").rstrip(".")


def fmt_step(value: Decimal, dp: int = 2) -> str:
    """A bridge step: +45,000 or (27,300)."""
    text = fmt_money(value, dp)
    return f"+{text}" if _round(value, dp) > 0 else text


def fmt_percent(value: Decimal | None, dp: int = 2) -> str:
    if value is None:
        return "n/m"
    return f"{_round(value, dp):.{dp}f}%"


PP_DP = 2  # percentage points: one precision on every surface, and for the label


def round_pp(value: Decimal) -> Decimal:
    return _round(value, PP_DP)


def fmt_pp(value: Decimal | None) -> str:
    """Percentage points, always signed unless zero: +1.03 pp, -1.04 pp, 0.00 pp."""
    if value is None:
        return "n/m"
    rounded = round_pp(value)
    return f"{'+' if rounded > 0 else ''}{rounded:.{PP_DP}f} pp"
