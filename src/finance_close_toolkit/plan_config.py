"""Plan TOML (`plans/<name>.toml`) to a typed, validated Plan; scenarios are typed patches over a copy of it."""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass, field, fields, is_dataclass, replace
from decimal import Decimal
from pathlib import Path
from typing import Callable, Iterable

from .config import version_of
from .money import plain

HUNDRED = Decimal(100)
_MONTH = re.compile(r"^(\d{4})-(\d{2})$")
_ABBR = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


class PlanError(ValueError):
    def __init__(self, problems: list[str]):
        self.problems = tuple(problems)
        super().__init__("\n".join(problems))


@dataclass(frozen=True, order=True)
class Month:
    year: int
    month: int

    @classmethod
    def parse(cls, text: str) -> Month:
        m = _MONTH.match(text)
        if not m or not 1 <= int(m.group(2)) <= 12:
            raise ValueError(f"expected a month like 2027-01, got {text!r}")
        return cls(int(m.group(1)), int(m.group(2)))

    def __str__(self) -> str:
        return f"{self.year}-{self.month:02d}"

    @property
    def index(self) -> int:
        return self.year * 12 + self.month - 1

    def plus(self, n: int) -> Month:
        i = self.index + n
        return Month(i // 12, i % 12 + 1)

    def since(self, other: Month) -> int:
        """Months from other to self: 2027-03 since 2027-01 is 2."""
        return self.index - other.index

    def fiscal_year(self, start_month: int) -> int:
        """The calendar year in which this month's fiscal year starts, for fiscal years starting in start_month."""
        return self.year if self.month >= start_month else self.year - 1

    @property
    def starts_quarter(self) -> bool:
        return self.month in (1, 4, 7, 10)

    @property
    def label(self) -> str:
        return f"{_ABBR[self.month - 1]}-{self.year % 100:02d}"


# Field parsers: raw TOML value and where it is -> typed value, or ValueError with a readable reason.

def _number(v: object) -> Decimal:
    if isinstance(v, bool) or not isinstance(v, (int, str, Decimal)):
        raise ValueError(f"expected a number, got {v!r}")
    try:
        d = Decimal(str(v))
    except ArithmeticError:
        raise ValueError(f"expected a number, got {v!r}") from None
    if not d.is_finite():
        raise ValueError(f"expected a number, got {v!r}")
    return d


def _percent(v: object) -> Decimal:
    d = _number(v)
    if not 0 <= d <= 100:
        raise ValueError(f"expected a percentage from 0 to 100, got {v!r}")
    return d


def _amount(v: object) -> Decimal:
    d = _number(v)
    if d < 0:
        raise ValueError(f"expected an amount of 0 or more, got {v!r}")
    return d


def _count(v: object) -> int:
    if isinstance(v, bool) or not isinstance(v, int) or v < 1:
        raise ValueError(f"expected a whole number of 1 or more, got {v!r}")
    return v


MAX_MONTHS = 36


def _horizon(v: object) -> int:
    n = _count(v)
    if n > MAX_MONTHS:
        raise ValueError(f"expected 1 to {MAX_MONTHS} months, got {v!r}")
    return n


def _month_number(v: object) -> int:
    if isinstance(v, bool) or not isinstance(v, int) or not 1 <= v <= 12:
        raise ValueError(f"expected a month number from 1 to 12, got {v!r}")
    return v


def _text(v: object) -> str:
    if not isinstance(v, str) or not v.strip():
        raise ValueError(f"expected text, got {v!r}")
    return v


def _month(v: object) -> Month:
    if not isinstance(v, str):
        raise ValueError(f"expected a month like 2027-01, got {v!r}")
    return Month.parse(v)


def _list(v: object) -> list:
    if not isinstance(v, list):
        raise ValueError(f"expected a list, got {v!r}")
    return v


def _numbers(v: object) -> tuple[Decimal, ...]:
    return tuple(_number(x) for x in _list(v))


def _curve(v: object) -> tuple[Decimal, ...]:
    """Shares settled in month +0, +1, ...; each 0-100 and together exactly 100."""
    shares = tuple(_percent(x) for x in _list(v))
    if not shares:
        raise ValueError("a settlement curve needs at least one share")
    if sum(shares) != HUNDRED:
        raise ValueError(f"settlement curve sums to {sum(shares)}%, must be 100%")
    return shares


@dataclass(frozen=True)
class Flow:
    month: Month
    amount: Decimal


def _table(v: object) -> dict:
    if not isinstance(v, dict):
        raise ValueError(f"expected a table, got {v!r}")
    return v


def _annual_units(v: object) -> dict[int, Decimal]:
    """Units for a whole fiscal year, keyed by the calendar year in which that fiscal year starts."""
    out = {}
    for key, value in _table(v).items():
        if not re.fullmatch(r"\d{4}", key):
            raise ValueError(f'expected a year like "2028" as the key, got {key!r}')
        out[int(key)] = _amount(value)
    return dict(sorted(out.items()))


def _flows(v: object) -> tuple[Flow, ...]:
    out = []
    for x in _list(v):
        t = _table(x)
        if set(t) != {"month", "amount"}:
            raise ValueError(f"each entry needs exactly month and amount, got {sorted(t)}")
        out.append(Flow(_month(t["month"]), _amount(t["amount"])))
    return tuple(out)


@dataclass(frozen=True)
class Spec:
    """One TOML key: the dataclass attribute it fills, its parser and, when optional, its default."""
    attr: str
    parse: Callable[[object], object]
    default: object = ...

    @property
    def required(self) -> bool:
        return self.default is ...


def _specs(*items: Spec | tuple) -> dict[str, Spec]:
    out = {}
    for s in items:
        spec = s if isinstance(s, Spec) else Spec(*s)
        out[spec.attr] = spec
    return out


# Sections


@dataclass(frozen=True)
class Setup:
    entity: str
    currency: str
    first_month: Month
    months: int
    fiscal_year_start: int | None = None  # month number; None: the month of first_month

    @property
    def fy_start(self) -> int:
        return self.fiscal_year_start or self.first_month.month


@dataclass(frozen=True)
class FiscalYear:
    """The plan months in one fiscal year; first is the index of its first month in the plan."""
    start_year: int  # the calendar year in which the fiscal year starts
    months: tuple[Month, ...]
    first: int

    @property
    def label(self) -> str:
        return f"FY{self.start_year}"

    @property
    def part(self) -> bool:
        return len(self.months) < 12

    @property
    def heading(self) -> str:
        """The column heading: the label, with the months for a part year ("FY2029 (6 mo)")."""
        return f"{self.label} ({len(self.months)} mo)" if self.part else self.label

    @property
    def span(self) -> slice:
        return slice(self.first, self.first + len(self.months))


def part_years_text(years: Iterable[FiscalYear]) -> str | None:
    """"Part years: FY2029 has 6 months (Jan-29 to Jun-29)."; None when every year is whole."""
    part = [y for y in years if y.part]
    if not part:
        return None
    return "Part years: " + "; ".join(f"{y.label} has {len(y.months)} months ({y.months[0].label} to "
                                      f"{y.months[-1].label})" for y in part) + "."


@dataclass(frozen=True)
class Opening:
    """Balance sheet at the day before first_month."""
    cash: Decimal
    accounts_receivable: Decimal
    fixed_assets_cost: Decimal
    accumulated_depreciation: Decimal
    fixed_assets_remaining_life_months: int
    accounts_payable: Decimal
    tax_payable: Decimal
    debt: Decimal
    share_capital: Decimal
    retained_earnings: Decimal

    @property
    def total_assets(self) -> Decimal:
        return self.cash + self.accounts_receivable + self.fixed_assets_cost - self.accumulated_depreciation

    @property
    def total_liabilities_and_equity(self) -> Decimal:
        return self.accounts_payable + self.tax_payable + self.debt + self.share_capital + self.retained_earnings


@dataclass(frozen=True)
class OpeningAssets:
    cost: Decimal
    accumulated_depreciation: Decimal
    remaining_life_months: int


@dataclass(frozen=True)
class PartialOpening:
    """The fields an incomplete [opening] does give: enough, when present, for depreciation of the opening
    fixed assets and interest on the opening debt in a P&L-only run."""
    given: dict[str, object]

    @property
    def assets(self) -> OpeningAssets | None:
        keys = ("fixed_assets_cost", "accumulated_depreciation", "fixed_assets_remaining_life_months")
        return OpeningAssets(*(self.given[k] for k in keys)) if all(k in self.given for k in keys) else None

    @property
    def debt(self) -> Decimal | None:
        return self.given.get("debt")


@dataclass(frozen=True)
class Volume:
    base_units: Decimal
    growth_pct_month: Decimal
    seasonality: tuple[Decimal, ...]  # one index per plan month, or 12 repeated each year of a longer plan
    annual_units: dict[int, Decimal] = field(default_factory=dict)  # fiscal start year -> that year's units

    def index(self, i: int) -> Decimal:
        """Seasonality index of the plan's month i."""
        return self.seasonality[i % 12 if len(self.seasonality) == 12 else i]


@dataclass(frozen=True)
class ProductChange:
    """Terms that apply from a month on; unset terms keep their earlier value."""
    from_month: Month
    list_price: Decimal | None = None
    discount_pct: Decimal | None = None
    unit_cost: Decimal | None = None
    mix_pct: Decimal | None = None


@dataclass(frozen=True)
class Terms:
    list_price: Decimal
    discount_pct: Decimal
    unit_cost: Decimal
    mix_pct: Decimal

    @property
    def net_price(self) -> Decimal:
        return self.list_price * (1 - self.discount_pct / HUNDRED)


@dataclass(frozen=True)
class Product:
    id: str
    name: str
    list_price: Decimal
    discount_pct: Decimal
    unit_cost: Decimal
    mix_pct: Decimal
    changes: tuple[ProductChange, ...] = ()

    def terms(self, month: Month) -> Terms:
        t = Terms(self.list_price, self.discount_pct, self.unit_cost, self.mix_pct)
        for c in sorted(self.changes, key=lambda c: c.from_month):
            if c.from_month <= month:
                t = replace(t, **{f.name: getattr(c, f.name) for f in fields(Terms) if getattr(c, f.name) is not None})
        return t


@dataclass(frozen=True)
class Hire:
    id: str
    role: str
    fte: Decimal
    monthly_salary: Decimal
    on_cost_pct: Decimal
    start_month: Month
    end_month: Month | None = None

    def active(self, month: Month) -> bool:
        return self.start_month <= month and (self.end_month is None or month <= self.end_month)


@dataclass(frozen=True)
class Opex:
    id: str
    name: str
    monthly_amount: Decimal
    start_month: Month


@dataclass(frozen=True)
class Settlement:
    """curve: shares of each month's amount settled in month +0, +1, ...; opening: shares of the
    opening balance settled in the first month, the second, ..."""
    curve: tuple[Decimal, ...]
    opening: tuple[Decimal, ...]


@dataclass(frozen=True)
class Capex:
    id: str
    name: str
    amount: Decimal
    spend_month: Month
    in_service_month: Month
    useful_life_months: int


@dataclass(frozen=True)
class Debt:
    rate_pct: Decimal = Decimal(0)
    drawdowns: tuple[Flow, ...] = ()
    repayments: tuple[Flow, ...] = ()


@dataclass(frozen=True)
class Tax:
    rate_pct: Decimal = Decimal(0)


@dataclass(frozen=True)
class Equity:
    injections: tuple[Flow, ...] = ()
    dividends: tuple[Flow, ...] = ()


@dataclass(frozen=True)
class Budget:
    """The plan as one market's budget submission: plan line key (revenue, opex.RENT ...) -> regional account."""
    market: str
    accounts: dict[str, str]


# Below EBIT, so not part of the regional P&L a budget submission feeds.
BELOW_EBIT = {"interest": "Interest", "tax": "Tax"}


def _changes(v: object) -> tuple[ProductChange, ...]:
    out = []
    for x in _list(v):
        t = dict(_table(x))
        unknown = set(t) - {"from_month", *CHANGE_TERMS}
        if unknown:
            raise ValueError(f"unknown key {sorted(unknown)[0]!r} in a product change")
        if "from_month" not in t:
            raise ValueError("each product change needs from_month")
        out.append(ProductChange(_month(t.pop("from_month")),
                                 **{k: CHANGE_TERMS[k](v) for k, v in t.items()}))
    return tuple(out)


CHANGE_TERMS = {"list_price": _amount, "discount_pct": _percent, "unit_cost": _amount, "mix_pct": _percent}

SINGLE: dict[str, tuple[type, dict[str, Spec]]] = {
    "setup": (Setup, _specs(("entity", _text), ("currency", _text), ("first_month", _month), ("months", _horizon),
                            ("fiscal_year_start", _month_number, None))),
    "opening": (Opening, _specs(
        ("cash", _number), ("accounts_receivable", _amount), ("fixed_assets_cost", _amount),
        ("accumulated_depreciation", _amount), ("fixed_assets_remaining_life_months", _count),
        ("accounts_payable", _amount), ("tax_payable", _amount), ("debt", _amount), ("share_capital", _number),
        ("retained_earnings", _number))),
    "volume": (Volume, _specs(("base_units", _amount), ("growth_pct_month", _number), ("seasonality", _numbers),
                              ("annual_units", _annual_units, {}))),
    "debt": (Debt, _specs(("rate_pct", _percent, Decimal(0)), ("drawdowns", _flows, ()),
                          ("repayments", _flows, ()))),
    "tax": (Tax, _specs(("rate_pct", _percent, Decimal(0)))),
    "equity": (Equity, _specs(("injections", _flows, ()), ("dividends", _flows, ()))),
}
# Settlement sections name their opening schedule after the balance it settles.
SETTLEMENT_KEYS = {"collections": "opening_ar_schedule", "payments": "opening_ap_schedule"}
SETTLEMENT = {section: (Settlement, {"curve": Spec("curve", _curve), key: Spec("opening", _curve)})
              for section, key in SETTLEMENT_KEYS.items()}
ITEMS: dict[str, tuple[type, dict[str, Spec]]] = {
    "products": (Product, _specs(("name", _text), ("list_price", _amount), ("discount_pct", _percent, Decimal(0)),
                                 ("unit_cost", _amount), ("mix_pct", _percent), ("changes", _changes, ()))),
    "hires": (Hire, _specs(("role", _text), ("fte", _amount), ("monthly_salary", _amount),
                           ("on_cost_pct", _percent, Decimal(0)), ("start_month", _month),
                           ("end_month", _month, None))),
    "opex": (Opex, _specs(("name", _text), ("monthly_amount", _amount), ("start_month", _month))),
    "capex": (Capex, _specs(("name", _text), ("amount", _amount), ("spend_month", _month),
                            ("in_service_month", _month), ("useful_life_months", _count))),
}
OPTIONAL_SINGLE = {"debt", "tax", "equity"}
TOP_LEVEL = {*SINGLE, *SETTLEMENT, *ITEMS, "scenarios", "budget"}
# Scenarios patch assumptions, never the entity setup or the opening balance sheet.
OVERRIDABLE = {**{k: v[1] for k, v in SINGLE.items() if k not in ("setup", "opening")},
               **{k: v[1] for k, v in SETTLEMENT.items()}}


@dataclass(frozen=True)
class Override:
    """One scenario patch: `hires.H2.start_month = "2027-06"` is section hires, item H2."""
    section: str
    item: str | None
    attr: str
    value: object

    @property
    def path(self) -> str:
        return ".".join(p for p in (self.section, self.item, self.attr) if p)


@dataclass(frozen=True)
class Scenario:
    name: str
    overrides: tuple[Override, ...]


@dataclass(frozen=True)
class Plan:
    name: str
    version: str
    setup: Setup
    opening: Opening | None
    volume: Volume
    products: tuple[Product, ...]
    hires: tuple[Hire, ...]
    opex: tuple[Opex, ...]
    collections: Settlement | None
    payments: Settlement | None
    capex: tuple[Capex, ...]
    debt: Debt
    tax: Tax
    equity: Equity
    scenarios: tuple[Scenario, ...]
    # Balance-sheet inputs that are absent: whole sections ("opening") or fields ("opening.cash").
    missing: tuple[str, ...]
    partial_opening: PartialOpening | None = None  # what an incomplete [opening] gives
    budget: Budget | None = None

    @property
    def opening_assets(self) -> OpeningAssets | None:
        if self.opening is not None:
            o = self.opening
            return OpeningAssets(o.fixed_assets_cost, o.accumulated_depreciation, o.fixed_assets_remaining_life_months)
        return self.partial_opening.assets if self.partial_opening else None

    @property
    def opening_debt(self) -> Decimal | None:
        if self.opening is not None:
            return self.opening.debt
        return self.partial_opening.debt if self.partial_opening else None

    @property
    def months(self) -> tuple[Month, ...]:
        return tuple(self.setup.first_month.plus(i) for i in range(self.setup.months))

    @property
    def fiscal_years(self) -> tuple[FiscalYear, ...]:
        start, out = self.setup.fy_start, []
        for i, m in enumerate(self.months):
            y = m.fiscal_year(start)
            if out and out[-1].start_year == y:
                out[-1] = replace(out[-1], months=(*out[-1].months, m))
            else:
                out.append(FiscalYear(y, (m,), i))
        return tuple(out)

    @property
    def budget_lines(self) -> dict[str, str]:
        """The P&L lines above EBIT, which a budget submission must map: key -> P&L label."""
        return {"revenue": "Revenue", "cogs": "COGS", "payroll": "Payroll",
                **{f"opex.{o.id}": o.name for o in self.opex}, "depreciation": "Depreciation"}

    @property
    def pnl_only(self) -> bool:
        return bool(self.missing)

    @property
    def scenario_names(self) -> tuple[str, ...]:
        return tuple(s.name for s in self.scenarios)

    def scenario(self, name: str | None) -> Plan:
        """The plan with a scenario's patches applied to a copy; None is the base."""
        if name is None or name == "base":
            return self
        found = next((s for s in self.scenarios if s.name == name), None)
        if found is None:
            known = ", ".join(self.scenario_names) or "none"
            raise PlanError([f"unknown scenario {name!r} (scenarios: {known})"])
        return _validated(apply_overrides(self, found.overrides), f"scenario {name}: ")


def apply_overrides(plan: Plan, overrides: tuple[Override, ...]) -> Plan:
    for o in overrides:
        current = getattr(plan, o.section)
        if o.item is None:
            patched = replace(current, **{o.attr: o.value})
        else:
            patched = tuple(replace(x, **{o.attr: o.value}) if x.id == o.item else x for x in current)
        plan = replace(plan, **{o.section: patched})
    return plan


# Parsing


def _build(cls: type, specs: dict[str, Spec], raw: object, where: str, problems: list[str], **extra):
    """cls from a TOML table; unknown keys, missing keys and bad values go to problems."""
    if not isinstance(raw, dict):
        problems.append(f"{where}: expected a table")
        return None
    kwargs, ok = dict(extra), True
    for key in raw:
        if key not in specs and key not in extra and key != "id":
            problems.append(f"{where}: unknown key {key!r}")
            ok = False
    for key, spec in specs.items():
        if key not in raw:
            if spec.required:
                problems.append(f"{where}: missing {key}")
                ok = False
            else:
                kwargs[spec.attr] = spec.default
            continue
        try:
            kwargs[spec.attr] = spec.parse(raw[key])
        except ValueError as e:
            problems.append(f"{where}.{key}: {e}")
            ok = False
    return cls(**kwargs) if ok else None


def _items(section: str, raw: object, problems: list[str]) -> tuple:
    cls, specs = ITEMS[section]
    if raw is None:
        return ()
    if not isinstance(raw, list):
        problems.append(f"{section}: expected [[{section}]] entries")
        return ()
    out, seen = [], set()
    for i, entry in enumerate(raw, 1):
        item_id = entry.get("id") if isinstance(entry, dict) else None
        if not isinstance(item_id, str) or not item_id:
            problems.append(f"{section} entry {i}: missing id")
            continue
        if item_id in seen:
            problems.append(f"{section}: id {item_id!r} appears twice")
            continue
        seen.add(item_id)
        built = _build(cls, specs, entry, f"{section}.{item_id}", problems, id=item_id)
        if built is not None:
            out.append(built)
    return tuple(out)


def _opening(raw: object, problems: list[str]) -> tuple[Opening | None, PartialOpening | None, tuple[str, ...]]:
    """An incomplete opening balance sheet is not an error: it is reported and the plan runs P&L only.
    The fields it does give are still validated and kept."""
    if raw is None:
        return None, None, ("opening",)
    _, specs = SINGLE["opening"]
    if not isinstance(raw, dict):
        problems.append("opening: expected a table")
        return None, None, ()
    absent = tuple(f"opening.{k}" for k in specs if k not in raw)
    if not absent:
        return _build(Opening, specs, raw, "opening", problems), None, ()
    given = {}
    for key, value in raw.items():
        if key not in specs:
            problems.append(f"opening: unknown key {key!r}")
            continue
        try:
            given[key] = specs[key].parse(value)
        except ValueError as e:
            problems.append(f"opening.{key}: {e}")
    return None, PartialOpening(given), absent


def _scenario_overrides(name: str, raw: object, ids: dict[str, set[str]], problems: list[str]) -> tuple[Override, ...]:
    where = f"scenarios.{name}"
    if not isinstance(raw, dict):
        problems.append(f"{where}: expected a table")
        return ()
    out = []
    for section, body in raw.items():
        if section in OVERRIDABLE:
            specs, targets = OVERRIDABLE[section], [(None, body)]
        elif section in ITEMS:
            specs = ITEMS[section][1]
            targets = list(body.items()) if isinstance(body, dict) else [(None, body)]
        else:
            problems.append(f"{where}: unknown key {section!r} "
                            f"(overridable: {', '.join([*OVERRIDABLE, *ITEMS])})")
            continue
        for item, keys in targets:
            at = ".".join(p for p in (where, section, item) if p)
            if section in ITEMS and item not in ids[section]:
                problems.append(f"{at}: no {section} entry with id {item!r}")
                continue
            if not isinstance(keys, dict):
                problems.append(f"{at}: expected keys to override")
                continue
            for key, value in keys.items():
                spec = specs.get(key)
                if spec is None:
                    problems.append(f"{at}: unknown key {key!r}")
                    continue
                try:
                    out.append(Override(section, item, spec.attr, spec.parse(value)))
                except ValueError as e:
                    problems.append(f"{at}.{key}: {e}")
    return tuple(out)


def _check(plan: Plan) -> list[str]:
    """Rules across fields, run on the base and on every scenario."""
    problems = []
    months, v = plan.months, plan.volume
    if len(v.seasonality) != len(months) and not (len(months) > 12 and len(v.seasonality) == 12):
        problems.append(f"volume.seasonality has {len(v.seasonality)} entries, needs one per month "
                        f"({len(months)})" + (" or 12, repeated each year" if len(months) > 12 else ""))
    else:
        years = {fy.start_year: fy for fy in plan.fiscal_years}
        for y in v.annual_units:
            fy = years.get(y)
            if fy is None:
                problems.append(f"volume.annual_units: FY{y} is not in the plan")
            elif fy.part:
                problems.append(f"volume.annual_units: FY{y} is only partly in the plan ({fy.months[0].label} to "
                                f"{fy.months[-1].label}); annual units need a whole fiscal year")
            elif sum(v.index(i) for i in range(fy.first, fy.first + 12)) <= 0:
                problems.append(f"volume.annual_units: the FY{y} seasonality indices sum to zero or less, so its "
                                "units cannot be spread")
    if not plan.products:
        problems.append("products: at least one product is needed")
    bad_mix = [(m, sum(p.terms(m).mix_pct for p in plan.products)) for m in months]
    bad_mix = [(m, total) for m, total in bad_mix if plan.products and total != HUNDRED]
    if bad_mix:
        first, total = bad_mix[0]
        problems.append(f"products: mix shares sum to {total}% in {first}, must be 100% in every month"
                        + (f" ({len(bad_mix)} months affected)" if len(bad_mix) > 1 else ""))
    for h in plan.hires:
        if h.end_month is not None and h.end_month < h.start_month:
            problems.append(f"hires.{h.id}: end_month {h.end_month} is before start_month {h.start_month}")
    for c in plan.capex:
        if c.spend_month < plan.setup.first_month:
            problems.append(f"capex.{c.id}: spend_month {c.spend_month} is before the plan starts; "
                            "include it in the opening fixed assets instead")
        if c.in_service_month < c.spend_month:
            problems.append(f"capex.{c.id}: in_service_month {c.in_service_month} is before spend_month "
                            f"{c.spend_month}")
    return problems


def _budget(raw: object, plan: Plan, problems: list[str]) -> Budget | None:
    """[budget]: the market and an account for plan lines above EBIT. Unmapped lines are not a load error;
    `fct plan to-budget` refuses and lists them."""
    if raw is None:
        return None
    if not isinstance(raw, dict):
        problems.append("budget: expected a table")
        return None
    problems += [f"budget: unknown key {k!r} (expected market and accounts)" for k in raw if k not in ("market", "accounts")]
    market, accounts = raw.get("market"), raw.get("accounts", {})
    if not isinstance(market, str) or not market.strip():
        problems.append("budget: missing market (a market code from close.toml)")
    if not isinstance(accounts, dict):
        problems.append("budget.accounts: expected a table of plan line = regional account")
        return None
    lines, out = plan.budget_lines, {}
    for key, code in accounts.items():
        if key in BELOW_EBIT:
            problems.append(f"budget.accounts.{key}: {BELOW_EBIT[key].lower()} is below EBIT and not part of the "
                            "regional P&L; remove it")
        elif key not in lines:
            problems.append(f"budget.accounts: unknown plan line {key!r} (plan lines: {', '.join(lines)})")
        elif not isinstance(code, str) or not code.strip():
            problems.append(f"budget.accounts.{key}: expected a regional account code, got {code!r}")
        else:
            out[key] = code
    return Budget(market, out) if isinstance(market, str) and market.strip() else None


def _validated(plan: Plan, prefix: str = "") -> Plan:
    problems = _check(plan)
    if problems:
        raise PlanError([prefix + p for p in problems])
    return plan


def parse_plan(text: str, name: str, version: str) -> Plan:
    try:
        raw = tomllib.loads(text, parse_float=Decimal)
    except tomllib.TOMLDecodeError as e:
        raise PlanError([f"{name}: {e}"]) from None
    problems: list[str] = [f"unknown section [{k}]" for k in raw if k not in TOP_LEVEL]

    def single(section: str):
        cls, specs = SINGLE[section]
        if section not in raw:
            if section in OPTIONAL_SINGLE:
                return cls()
            problems.append(f"missing section [{section}]")
            return None
        return _build(cls, specs, raw[section], section, problems)

    def settlement(section: str) -> Settlement | None:
        if section not in raw:
            return None
        cls, specs = SETTLEMENT[section]
        return _build(cls, specs, raw[section], section, problems)

    setup, volume, debt, tax, equity = (single(s) for s in ("setup", "volume", "debt", "tax", "equity"))
    opening, partial_opening, missing = _opening(raw.get("opening"), problems)
    collections, payments = settlement("collections"), settlement("payments")
    missing += tuple(s for s in ("collections", "payments") if s not in raw)
    items = {s: _items(s, raw.get(s), problems) for s in ITEMS}
    scenarios_raw = raw.get("scenarios", {})
    if not isinstance(scenarios_raw, dict):
        problems.append("scenarios: expected [scenarios.<name>] tables")
        scenarios_raw = {}
    if "base" in scenarios_raw:
        problems.append("scenarios.base: 'base' is the plan itself and cannot be a scenario name")
    ids = {s: {x.id for x in items[s]} for s in ITEMS}
    scenarios = tuple(Scenario(n, _scenario_overrides(n, body, ids, problems)) for n, body in scenarios_raw.items())
    if problems:
        raise PlanError(problems)
    plan = Plan(name, version, setup, opening, volume, items["products"], items["hires"], items["opex"],
                collections, payments, items["capex"], debt, tax, equity, scenarios, missing, partial_opening)
    plan = replace(plan, budget=_budget(raw.get("budget"), plan, problems))
    if problems:
        raise PlanError(problems)
    _validated(plan)
    problems = []
    for s in scenarios:
        problems += [f"scenario {s.name}: {p}" for p in _check(apply_overrides(plan, s.overrides))]
    if problems:
        raise PlanError(problems)
    return plan


def load_plan(path: Path) -> Plan:
    path = Path(path)
    data = path.read_bytes()
    return parse_plan(data.decode("utf-8"), path.stem, version_of(data))


def _shown(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, Month):
        return str(value)
    if isinstance(value, Decimal):
        return plain(value)
    if isinstance(value, tuple):
        return "; ".join(_shown(v) for v in value)
    if isinstance(value, dict):
        return "; ".join(f"{k} {_shown(v)}" for k, v in value.items())
    if is_dataclass(value):
        return ", ".join(f"{f.name} {_shown(getattr(value, f.name))}" for f in fields(value)
                         if getattr(value, f.name) is not None)
    return str(value)


def assumption_rows(plan: Plan) -> list[tuple[str, str, str, str]]:
    """(section, item, key, value) for every assumption the plan was run with, as text."""
    rows = []
    for section in ("setup", "opening", "volume", "collections", "payments", "debt", "tax", "equity"):
        body = getattr(plan, section)
        if body is None:
            rows.append((section, "", "", "missing"))
            continue
        rows += [(section, "", f.name, _shown(getattr(body, f.name))) for f in fields(body)]
    for section in ITEMS:
        for item in getattr(plan, section):
            rows += [(section, item.id, f.name, _shown(getattr(item, f.name))) for f in fields(item) if f.name != "id"]
    if plan.budget is not None:
        rows.append(("budget", "", "market", plan.budget.market))
        rows += [("budget", "", f"accounts.{k}", v) for k, v in plan.budget.accounts.items()]
    return rows


def _diff_shown(value: object) -> str:
    """A curve or schedule as 0/50/50; anything else as in the assumption rows."""
    if isinstance(value, tuple) and value and all(isinstance(v, Decimal) for v in value):
        return "/".join(plain(v) for v in value)
    return _shown(value) or "none"


def _toml_key(o: Override) -> str:
    specs = OVERRIDABLE.get(o.section) or ITEMS[o.section][1]
    return next(k for k, s in specs.items() if s.attr == o.attr)


@dataclass(frozen=True)
class Change:
    """One scenario override against base, as text: path collections.curve, before 0/100, after 0/50/50."""
    path: str
    before: str
    after: str

    def __str__(self) -> str:
        return f"{self.path}: {self.before} -> {self.after}"


def scenario_changes(plan: Plan, name: str | None) -> tuple[Change, ...]:
    """What a scenario changes against the base plan, one entry per override; () for base."""
    if name is None or name == "base":
        return ()
    found = next((s for s in plan.scenarios if s.name == name), None)
    if found is None:
        raise PlanError([f"unknown scenario {name!r}"])
    out = []
    for o in found.overrides:
        body = getattr(plan, o.section)
        if o.item is not None:
            body = next(x for x in body if x.id == o.item)
        before = getattr(body, o.attr) if body is not None else None
        path = ".".join(p for p in (o.section, o.item, _toml_key(o)) if p)
        out.append(Change(path, _diff_shown(before), _diff_shown(o.value)))
    return tuple(out)
