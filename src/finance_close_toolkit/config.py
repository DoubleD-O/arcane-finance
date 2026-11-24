import hashlib
import json
import tomllib
from dataclasses import dataclass, replace
from decimal import Decimal
from pathlib import Path
from typing import Literal, Mapping as MappingT

AccountType = Literal["revenue", "cogs", "opex", "below_ebitda"]
ACCOUNT_TYPES: tuple[AccountType, ...] = ("revenue", "cogs", "opex", "below_ebitda")
Sign = Literal["tb", "pl_natural"]
SourceFormat = Literal["csv", "xlsx"]


class ConfigError(ValueError):
    pass


def version_of(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()[:12]


def _toml(text: str, where: str) -> dict:
    try:
        return tomllib.loads(text, parse_float=Decimal)
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"{where}: {e}") from None


def _decimal(value: object, where: str) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (int, str, Decimal)):
        raise ConfigError(f"{where}: expected a number, got {value!r}")
    try:
        return Decimal(str(value))
    except ArithmeticError:
        raise ConfigError(f"{where}: expected a number, got {value!r}") from None


@dataclass(frozen=True)
class Account:
    code: str
    name: str
    type: AccountType


@dataclass(frozen=True)
class ChartOfAccounts:
    accounts: MappingT[str, Account]
    version: str
    content: str

    def type_of(self, code: str) -> AccountType:
        return self.accounts[code].type


def parse_chart_of_accounts(text: str, version: str) -> ChartOfAccounts:
    raw = _toml(text, "chart of accounts")
    accounts = {}
    for code, spec in raw.get("accounts", {}).items():
        if spec.get("type") not in ACCOUNT_TYPES:
            raise ConfigError(f"chart of accounts: {code} has unknown type {spec.get('type')!r}")
        accounts[code] = Account(code, spec.get("name", code), spec["type"])
    return ChartOfAccounts(accounts, version, text)


@dataclass(frozen=True)
class Layout:
    format: SourceFormat
    sheet: str | None
    header_row: int
    account_column: str
    description_column: str
    amount_column: str
    period_column: str | None

    @property
    def columns(self) -> tuple[str, ...]:
        cols = (self.account_column, self.description_column, self.amount_column)
        return cols + ((self.period_column,) if self.period_column else ())


@dataclass(frozen=True)
class Mapping:
    market: str | None  # None in a shared layout (_regional_template.toml) until a market points at it
    sign: Sign
    layout: Layout
    accounts: MappingT[str, str]
    version: str
    content: str


def parse_mapping(text: str, version: str) -> Mapping:
    raw = _toml(text, "mapping")
    lay = raw.get("layout", {})
    if lay.get("format") not in ("csv", "xlsx"):
        raise ConfigError(f"mapping: layout.format must be csv or xlsx, got {lay.get('format')!r}")
    if raw.get("sign") not in ("tb", "pl_natural"):
        raise ConfigError(f"mapping: sign must be tb or pl_natural, got {raw.get('sign')!r}")
    try:
        layout = Layout(
            format=lay["format"],
            sheet=lay.get("sheet"),
            header_row=int(lay.get("header_row", 1)),
            account_column=lay["account_column"],
            description_column=lay["description_column"],
            amount_column=lay["amount_column"],
            period_column=lay.get("period_column"),
        )
    except KeyError as e:
        raise ConfigError(f"mapping: layout is missing {e.args[0]}") from None
    accounts = {str(k): str(v) for k, v in raw.get("accounts", {}).items()}
    return Mapping(raw.get("market"), raw["sign"], layout, accounts, version, text)


def _read_versioned(path: Path) -> tuple[str, str]:
    data = path.read_bytes()
    return data.decode("utf-8"), version_of(data)


def load_mapping(path: Path) -> Mapping:
    return parse_mapping(*_read_versioned(path))


AmountSign = Literal["as_is", "negate"]


@dataclass(frozen=True)
class ControlsMapping:
    """How one close-controls export is laid out (config/controls/mappings/<kind>.toml)."""

    format: SourceFormat
    sheet: str | None
    header_row: int
    columns: MappingT[str, str]  # every template column -> the header it is read from
    amount_sign: AmountSign  # negate: the file shows amounts credit-positive
    name: str = ""  # the mapping file's stem; "" for the template's own columns

    @property
    def meaning(self) -> str:
        """What the mapping says, independent of comments, key order and layout of its file."""
        return json.dumps({"format": self.format, "sheet": self.sheet, "header_row": self.header_row,
                           "columns": dict(sorted(self.columns.items())), "amount_sign": self.amount_sign},
                          sort_keys=True)

    @property
    def version(self) -> str:
        """A hash of `meaning`; "" for the template's own columns."""
        return version_of(self.meaning.encode("utf-8")) if self.name else ""


def canonical_controls_mapping(columns: tuple[str, ...], format: SourceFormat) -> ControlsMapping:
    """No mapping file: the template's own columns, header on row 1."""
    return ControlsMapping(format, None, 1, {c: c for c in columns}, "as_is")


def parse_controls_mapping(text: str, name: str, columns: tuple[str, ...], where: str) -> ControlsMapping:
    raw = _toml(text, where)
    lay = raw.get("layout", {})
    if lay.get("format") not in ("csv", "xlsx"):
        raise ConfigError(f"{where}: layout.format must be csv or xlsx, got {lay.get('format')!r}")
    sign = raw.get("amount_sign", "as_is")
    if sign not in ("as_is", "negate"):
        raise ConfigError(f"{where}: amount_sign must be as_is or negate, got {sign!r}")
    header_row = lay.get("header_row", 1)
    if isinstance(header_row, bool) or not isinstance(header_row, int) or header_row < 1:
        raise ConfigError(f"{where}: layout.header_row must be a row number from 1, got {header_row!r}")
    sources = lay.get("columns", {})
    unknown = sorted(set(sources) - set(columns))
    if unknown:
        raise ConfigError(f"{where}: layout.columns has {', '.join(unknown)}, not template columns; "
                          f"columns: {', '.join(columns)}")
    if not all(isinstance(v, str) and v.strip() for v in sources.values()):
        raise ConfigError(f"{where}: every layout.columns entry must name a header")
    mapped = {c: sources.get(c, c).strip() for c in columns}
    return ControlsMapping(lay["format"], lay.get("sheet"), header_row, mapped, sign, name or "mapping")


def load_controls_mapping(path: Path, columns: tuple[str, ...]) -> ControlsMapping:
    return parse_controls_mapping(path.read_bytes().decode("utf-8"), path.stem, columns, str(path))


@dataclass(frozen=True)
class Market:
    code: str
    name: str
    currency: str
    owner: str
    mapping_path: Path


@dataclass(frozen=True)
class Materiality:
    abs: Decimal
    pct: Decimal


@dataclass(frozen=True)
class ControlsConfig:
    """Who may act on close controls, and when an invoiced total is in line with its accrual."""

    owners: tuple[str, ...]  # active roles
    ap_role: str
    accounting_role: str
    close_lead: str
    tolerance_abs: Decimal
    tolerance_pct: Decimal
    approver_roles: tuple[str, ...]  # who may approve a resolution, an override link or a large explanation
    explain_approval_abs: Decimal  # an explanation of a larger variance (local currency) needs an approver

    def tolerance(self, accrued: Decimal) -> Decimal:
        return max(self.tolerance_abs, abs(accrued) * self.tolerance_pct / 100)


DEFAULT_APPROVER_ROLES = ["Regional close lead", "Accounting team"]
DEFAULT_EXPLAIN_APPROVAL_ABS = 25000


def parse_controls(raw: dict) -> ControlsConfig:
    where = "[controls]"
    owners = raw.get("owners")
    if not isinstance(owners, list) or not owners or not all(isinstance(o, str) and o.strip() for o in owners):
        raise ConfigError(f"{where} owners must be a non-empty list of role names")
    roles = {}
    for key in ("ap_role", "accounting_role", "close_lead"):
        value = raw.get(key)
        if not isinstance(value, str) or value not in owners:
            raise ConfigError(f"{where} {key} must be one of the owners, got {value!r}")
        roles[key] = value
    approvers = raw.get("approver_roles", DEFAULT_APPROVER_ROLES)
    if not isinstance(approvers, list) or not approvers or not all(a in owners for a in approvers):
        raise ConfigError(f"{where} approver_roles must be a non-empty list of owners, got {approvers!r}")
    return ControlsConfig(
        owners=tuple(owners),
        tolerance_abs=_decimal(raw.get("tolerance_abs"), f"{where} tolerance_abs"),
        tolerance_pct=_decimal(raw.get("tolerance_pct"), f"{where} tolerance_pct"),
        approver_roles=tuple(approvers),
        explain_approval_abs=_decimal(raw.get("explain_approval_abs", DEFAULT_EXPLAIN_APPROVAL_ABS),
                                      f"{where} explain_approval_abs"),
        **roles,
    )


@dataclass(frozen=True)
class CloseConfig:
    path: Path
    reporting_currency: str
    markets: tuple[Market, ...]
    scenarios: tuple[str, ...]
    due_day: int
    regional_owner: str
    materiality: MappingT[str, Materiality]
    coa_path: Path
    controls: ControlsConfig | None = None

    def require_controls(self) -> ControlsConfig:
        if self.controls is None:
            raise ConfigError(f"{self.path} has no [controls] section")
        return self.controls

    def market(self, code: str) -> Market:
        for m in self.markets:
            if m.code == code:
                return m
        raise ConfigError(f"unknown market {code!r}; configured: {', '.join(m.code for m in self.markets)}")


def load_close_config(path: Path) -> CloseConfig:
    path = Path(path)
    raw = _toml(path.read_text(encoding="utf-8"), str(path))
    base = path.parent
    markets = tuple(
        Market(m["code"], m["name"], m["currency"], m["owner"], base / m["mapping"]) for m in raw["markets"]
    )
    materiality = {
        kpi: Materiality(_decimal(v["abs"], f"materiality.{kpi}.abs"), _decimal(v["pct"], f"materiality.{kpi}.pct"))
        for kpi, v in raw.get("materiality", {}).items()
    }
    return CloseConfig(
        path=path,
        reporting_currency=raw["reporting_currency"],
        markets=markets,
        scenarios=tuple(raw["scenarios"]),
        due_day=int(raw["due_day"]),
        regional_owner=raw["regional_owner"],
        materiality=materiality,
        coa_path=base / raw["chart_of_accounts"],
        controls=parse_controls(raw["controls"]) if "controls" in raw else None,
    )


@dataclass(frozen=True)
class FxRates:
    """Reporting currency per 1 unit of local currency, by scenario then currency."""

    rates: MappingT[str, MappingT[str, Decimal]]

    def rate(self, scenario: str, currency: str) -> Decimal | None:
        return self.rates.get(scenario, {}).get(currency)


def load_fx(path: Path) -> FxRates:
    raw = _toml(path.read_text(encoding="utf-8"), str(path))
    return FxRates(
        {
            scenario: {ccy: _decimal(v, f"{path.name} [{scenario}] {ccy}") for ccy, v in table.items()}
            for scenario, table in raw.items()
            if isinstance(table, dict)
        }
    )


@dataclass(frozen=True)
class Workspace:
    """Everything loaded from configuration files for one run."""

    config: CloseConfig
    coa: ChartOfAccounts
    mappings: MappingT[str, Mapping]
    fx_dir: Path

    def mapping(self, market: str) -> Mapping:
        self.config.market(market)
        return self.mappings[market]

    def fx_path(self, period: str) -> Path:
        return self.fx_dir / f"{period}.toml"

    def fx(self, period: str) -> FxRates | None:
        path = self.fx_path(period)
        return load_fx(path) if path.exists() else None


def load_workspace(config_path: Path, fx_dir: Path | None = None) -> Workspace:
    config = load_close_config(config_path)
    coa = parse_chart_of_accounts(*_read_versioned(config.coa_path))
    mappings = {}
    for m in config.markets:
        mapping = load_mapping(m.mapping_path)
        if mapping.market is None:
            mapping = replace(mapping, market=m.code)
        if mapping.market != m.code:
            raise ConfigError(f"{m.mapping_path} declares market {mapping.market!r}, expected {m.code!r}")
        unknown = sorted(set(mapping.accounts.values()) - set(coa.accounts))
        if unknown:
            raise ConfigError(f"{m.mapping_path} maps to accounts not in the chart: {', '.join(unknown)}")
        mappings[m.code] = mapping
    return Workspace(config, coa, mappings, Path(fx_dir) if fx_dir else config.path.parent.parent / "fx")
