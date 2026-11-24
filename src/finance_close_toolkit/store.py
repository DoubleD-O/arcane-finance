import json
from datetime import datetime
from pathlib import Path
from typing import Callable

import duckdb

from . import controls
from .config import ChartOfAccounts, Mapping
from .intake import RawRow, Submission
from .release import Release, ReleaseFact
from .validation import MappedFact, TieOut, Validation, ValidationException

DEFAULT_STORE = Path.home() / ".arcane-finance" / "store.duckdb"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS config_blobs (
    version TEXT PRIMARY KEY, kind TEXT NOT NULL, content TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS submissions (
    submission_id TEXT PRIMARY KEY, content_hash TEXT NOT NULL, market TEXT NOT NULL, period TEXT NOT NULL,
    scenario TEXT NOT NULL, file_name TEXT NOT NULL, sheet TEXT, headers TEXT NOT NULL,
    UNIQUE (market, period, scenario, content_hash)
);
CREATE TABLE IF NOT EXISTS receipts (
    seq INTEGER PRIMARY KEY, submission_id TEXT NOT NULL, received_at TIMESTAMP NOT NULL
);
CREATE TABLE IF NOT EXISTS raw_rows (
    submission_id TEXT NOT NULL, row_number INTEGER NOT NULL, source_account TEXT NOT NULL,
    description TEXT NOT NULL, period_value TEXT, amount_text TEXT NOT NULL, amount DECIMAL(38,10),
    PRIMARY KEY (submission_id, row_number)
);
CREATE TABLE IF NOT EXISTS validations (
    submission_id TEXT NOT NULL, mapping_version TEXT NOT NULL, state TEXT NOT NULL,
    source_total DECIMAL(38,10) NOT NULL, mapped_total DECIMAL(38,10) NOT NULL,
    unmapped_total DECIMAL(38,10) NOT NULL, seq INTEGER NOT NULL,
    PRIMARY KEY (submission_id, mapping_version)
);
CREATE TABLE IF NOT EXISTS validation_exceptions (
    submission_id TEXT NOT NULL, mapping_version TEXT NOT NULL, seq INTEGER NOT NULL, code TEXT NOT NULL,
    row_number INTEGER, detail TEXT NOT NULL, account TEXT, amount DECIMAL(38,10)
);
CREATE TABLE IF NOT EXISTS validation_facts (
    submission_id TEXT NOT NULL, mapping_version TEXT NOT NULL, source_row INTEGER NOT NULL,
    account TEXT NOT NULL, amount DECIMAL(38,10) NOT NULL
);
CREATE TABLE IF NOT EXISTS releases (
    release_id TEXT PRIMARY KEY, period TEXT NOT NULL, seq INTEGER NOT NULL, previous_release_id TEXT,
    approved_by TEXT NOT NULL, approved_at TIMESTAMP NOT NULL, facts_checksum TEXT NOT NULL,
    manifest TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS release_facts (
    release_id TEXT NOT NULL, market TEXT NOT NULL, scenario TEXT NOT NULL, account TEXT NOT NULL,
    local_amount DECIMAL(38,10) NOT NULL, currency TEXT NOT NULL, rate DECIMAL(38,10) NOT NULL,
    reporting_amount DECIMAL(38,10) NOT NULL, submission_id TEXT NOT NULL, file_name TEXT NOT NULL,
    source_row INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS release_rates (
    release_id TEXT NOT NULL, scenario TEXT NOT NULL, currency TEXT NOT NULL, rate DECIMAL(38,10) NOT NULL
);
CREATE TABLE IF NOT EXISTS current_release (
    period TEXT PRIMARY KEY, release_id TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS control_imports (
    import_id TEXT PRIMARY KEY, kind TEXT NOT NULL, period TEXT NOT NULL, file_name TEXT NOT NULL,
    imported_at TIMESTAMP NOT NULL, seq INTEGER NOT NULL UNIQUE
);
ALTER TABLE control_imports ADD COLUMN IF NOT EXISTS mapping TEXT DEFAULT 'mapping not recorded';
CREATE TABLE IF NOT EXISTS control_receipts (
    seq INTEGER PRIMARY KEY, import_id TEXT NOT NULL, received_at TIMESTAMP NOT NULL
);
CREATE TABLE IF NOT EXISTS control_accruals (
    import_id TEXT NOT NULL, row_number INTEGER NOT NULL, accrual_id TEXT NOT NULL, market TEXT NOT NULL,
    period TEXT NOT NULL, account TEXT NOT NULL, supplier TEXT NOT NULL, reference TEXT NOT NULL,
    amount DECIMAL(38,10) NOT NULL, owner TEXT NOT NULL, description TEXT NOT NULL,
    PRIMARY KEY (import_id, row_number)
);
CREATE TABLE IF NOT EXISTS control_invoices (
    import_id TEXT NOT NULL, row_number INTEGER NOT NULL, invoice_id TEXT NOT NULL, market TEXT NOT NULL,
    supplier TEXT NOT NULL, invoice_date DATE NOT NULL, period TEXT NOT NULL, account TEXT NOT NULL,
    reference TEXT NOT NULL, amount DECIMAL(38,10) NOT NULL,
    PRIMARY KEY (import_id, row_number)
);
CREATE TABLE IF NOT EXISTS control_gl_lines (
    import_id TEXT NOT NULL, row_number INTEGER NOT NULL, journal_id TEXT NOT NULL, period TEXT NOT NULL,
    market TEXT NOT NULL, account TEXT NOT NULL, amount DECIMAL(38,10) NOT NULL, reference TEXT NOT NULL,
    description TEXT NOT NULL,
    PRIMARY KEY (import_id, row_number)
);
CREATE TABLE IF NOT EXISTS control_events (
    seq INTEGER PRIMARY KEY, kind TEXT NOT NULL, accrual_id TEXT NOT NULL, by_role TEXT NOT NULL,
    recorded_at TIMESTAMP NOT NULL, evidence TEXT NOT NULL, data TEXT NOT NULL
);
"""

# Columns after (import_id, row_number), in each row type's field order; the row's Source is rebuilt from them.
_CONTROL_TABLES = {
    "accruals": ("control_accruals", ("accrual_id", "market", "period", "account", "supplier", "reference", "amount",
                                      "owner", "description")),
    "invoices": ("control_invoices", ("invoice_id", "market", "supplier", "invoice_date", "period", "account",
                                      "reference", "amount")),
    "gl": ("control_gl_lines", ("journal_id", "period", "market", "account", "amount", "reference", "description")),
}


class Store:
    def __init__(self, con: duckdb.DuckDBPyConnection):
        self._con = con

    @classmethod
    def open(cls, path: Path | str) -> "Store":
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        con = duckdb.connect(str(path))
        con.execute(_SCHEMA)
        return cls(con)

    def close(self) -> None:
        self._con.close()

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def _one(self, sql: str, params: list) -> tuple | None:
        return self._con.execute(sql, params).fetchone()

    def _all(self, sql: str, params: list) -> list[tuple]:
        return self._con.execute(sql, params).fetchall()

    def _transaction(self, work: Callable[[], None]) -> None:
        self._con.execute("BEGIN TRANSACTION")
        try:
            work()
        except BaseException:
            self._con.execute("ROLLBACK")
            raise
        self._con.execute("COMMIT")

    def _save_blob(self, version: str, kind: str, content: str) -> None:
        self._con.execute(
            "INSERT INTO config_blobs VALUES (?, ?, ?) ON CONFLICT DO NOTHING", [version, kind, content]
        )

    def config_blob(self, version: str) -> str | None:
        row = self._one("SELECT content FROM config_blobs WHERE version = ?", [version])
        return row[0] if row else None

    _NEWEST_RECEIPT = (
        "SELECT s.submission_id, s.market, s.period, s.scenario, s.file_name, s.sheet, r.received_at"
        " FROM submissions s JOIN receipts r USING (submission_id) WHERE {} ORDER BY r.received_at DESC, r.seq DESC"
        " LIMIT 1"
    )

    def submission(self, submission_id: str) -> Submission | None:
        row = self._one(self._NEWEST_RECEIPT.format("s.submission_id = ?"), [submission_id])
        return Submission(*row) if row else None

    def latest_submission(self, market: str, period: str, scenario: str) -> Submission | None:
        row = self._one(
            self._NEWEST_RECEIPT.format("s.market = ? AND s.period = ? AND s.scenario = ?"), [market, period, scenario]
        )
        return Submission(*row) if row else None

    def _insert_receipt(self, submission_id: str, received_at: datetime) -> None:
        seq = self._one("SELECT coalesce(max(seq), 0) + 1 FROM receipts", [])[0]
        self._con.execute("INSERT INTO receipts VALUES (?, ?, ?)", [seq, submission_id, received_at])

    def add_receipt(self, submission_id: str, received_at: datetime) -> None:
        self._transaction(lambda: self._insert_receipt(submission_id, received_at))

    def raw_rows(self, submission_id: str) -> tuple[tuple[str, ...], tuple[RawRow, ...]]:
        headers = self._one("SELECT headers FROM submissions WHERE submission_id = ?", [submission_id])
        rows = self._all(
            "SELECT row_number, source_account, description, period_value, amount_text, amount"
            " FROM raw_rows WHERE submission_id = ? ORDER BY row_number",
            [submission_id],
        )
        return tuple(json.loads(headers[0])) if headers else (), tuple(RawRow(*r) for r in rows)

    def save_submission(
        self,
        submission: Submission,
        content_hash: str,
        headers: tuple[str, ...],
        rows: tuple[RawRow, ...],
        validation: Validation,
        mapping: Mapping,
    ) -> None:
        def work() -> None:
            s = submission
            self._con.execute(
                "INSERT INTO submissions VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                [s.submission_id, content_hash, s.market, s.period, s.scenario, s.file_name, s.sheet,
                 json.dumps(list(headers))],
            )
            self._insert_receipt(s.submission_id, s.received_at)
            if rows:
                self._con.executemany(
                    "INSERT INTO raw_rows VALUES (?, ?, ?, ?, ?, ?, ?)",
                    [[s.submission_id, r.row_number, r.source_account, r.description, r.period_value,
                      r.amount_text, r.amount] for r in rows],
                )
            self._insert_validation(validation, mapping)

        self._transaction(work)

    def _insert_validation(self, v: Validation, mapping: Mapping) -> None:
        self._save_blob(mapping.version, "mapping", mapping.content)
        seq = self._one("SELECT coalesce(max(seq), 0) + 1 FROM validations", [])[0]
        t = v.tie_out
        self._con.execute(
            "INSERT INTO validations VALUES (?, ?, ?, ?, ?, ?, ?)",
            [v.submission_id, v.mapping_version, v.state, t.source_total, t.mapped_total, t.unmapped_total, seq],
        )
        if v.exceptions:
            self._con.executemany(
                "INSERT INTO validation_exceptions VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                [[v.submission_id, v.mapping_version, i, e.code, e.row, e.detail, e.account, e.amount]
                 for i, e in enumerate(v.exceptions)],
            )
        if v.facts:
            self._con.executemany(
                "INSERT INTO validation_facts VALUES (?, ?, ?, ?, ?)",
                [[v.submission_id, v.mapping_version, f.source_row, f.account, f.amount] for f in v.facts],
            )

    def save_validation(self, validation: Validation, mapping: Mapping) -> None:
        self._transaction(lambda: self._insert_validation(validation, mapping))

    def _load_validation(self, row: tuple) -> Validation:
        submission_id, version, state, source, mapped, unmapped = row
        key = [submission_id, version]
        exceptions = self._all(
            "SELECT code, row_number, detail, account, amount FROM validation_exceptions"
            " WHERE submission_id = ? AND mapping_version = ? ORDER BY seq",
            key,
        )
        facts = self._all(
            "SELECT source_row, account, amount FROM validation_facts"
            " WHERE submission_id = ? AND mapping_version = ? ORDER BY source_row",
            key,
        )
        return Validation(
            submission_id,
            version,
            state,
            tuple(ValidationException(*e) for e in exceptions),
            tuple(MappedFact(*f) for f in facts),
            TieOut(source, mapped, unmapped),
        )

    _VALIDATION_COLS = "submission_id, mapping_version, state, source_total, mapped_total, unmapped_total"

    def validation(self, submission_id: str, mapping_version: str) -> Validation | None:
        row = self._one(
            f"SELECT {self._VALIDATION_COLS} FROM validations WHERE submission_id = ? AND mapping_version = ?",
            [submission_id, mapping_version],
        )
        return self._load_validation(row) if row else None

    def latest_validation(self, submission_id: str) -> Validation | None:
        row = self._one(
            f"SELECT {self._VALIDATION_COLS} FROM validations WHERE submission_id = ? ORDER BY seq DESC LIMIT 1",
            [submission_id],
        )
        return self._load_validation(row) if row else None

    def validations(self, submission_id: str) -> tuple[Validation, ...]:
        rows = self._all(
            f"SELECT {self._VALIDATION_COLS} FROM validations WHERE submission_id = ? ORDER BY seq", [submission_id]
        )
        return tuple(self._load_validation(r) for r in rows)

    def release(self, release_id: str) -> Release | None:
        row = self._one("SELECT manifest FROM releases WHERE release_id = ?", [release_id])
        return Release.from_manifest(json.loads(row[0])) if row else None

    def releases(self, period: str) -> tuple[Release, ...]:
        rows = self._all("SELECT manifest FROM releases WHERE period = ? ORDER BY seq", [period])
        return tuple(Release.from_manifest(json.loads(r[0])) for r in rows)

    def current_release(self, period: str) -> Release | None:
        row = self._one("SELECT release_id FROM current_release WHERE period = ?", [period])
        return self.release(row[0]) if row else None

    def next_release_seq(self, period: str) -> int:
        return self._one("SELECT coalesce(max(seq), 0) + 1 FROM releases WHERE period = ?", [period])[0]

    def release_facts(self, release_id: str) -> tuple[ReleaseFact, ...]:
        rows = self._all(
            "SELECT market, scenario, account, local_amount, currency, rate, reporting_amount, submission_id,"
            " file_name, source_row FROM release_facts WHERE release_id = ?"
            " ORDER BY scenario, market, account, source_row",
            [release_id],
        )
        return tuple(ReleaseFact(*r) for r in rows)

    def publish_release(
        self,
        release: Release,
        facts: tuple[ReleaseFact, ...],
        coa: ChartOfAccounts,
        fault: Callable[[], None] | None = None,
    ) -> None:
        def work() -> None:
            r = release
            self._save_blob(coa.version, "chart_of_accounts", coa.content)
            self._con.execute(
                "INSERT INTO releases VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                [r.release_id, r.period, r.seq, r.previous_release_id, r.approved_by, r.approved_at,
                 r.facts_checksum, json.dumps(r.manifest())],
            )
            self._con.executemany(
                "INSERT INTO release_facts VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [[r.release_id, f.market, f.scenario, f.account, f.local_amount, f.currency, f.rate,
                  f.reporting_amount, f.submission_id, f.file_name, f.source_row] for f in facts],
            )
            self._con.executemany(
                "INSERT INTO release_rates VALUES (?, ?, ?, ?)",
                [[r.release_id, sc, ccy, rate] for sc, table in r.rates.items() for ccy, rate in table.items()],
            )
            if fault is not None:
                fault()
            self._con.execute(
                "INSERT INTO current_release VALUES (?, ?) ON CONFLICT (period) DO UPDATE SET release_id = excluded.release_id",
                [r.period, r.release_id],
            )

        self._transaction(work)

    def save_control_files(self, files: tuple[controls.ControlFile, ...], imported_at: datetime,
                           events: tuple[controls.Event, ...] = ()) -> None:
        """Store parsed control files, and the events the import records, in one transaction. A file already
        stored (same content hash) is received again: it becomes the newest receipt, so current again."""

        def work() -> None:
            for f in files:
                seq = self._one("SELECT coalesce(max(seq), 0) + 1 FROM (SELECT seq FROM control_imports "
                                "UNION ALL SELECT seq FROM control_receipts)", [])[0]
                if self._one("SELECT 1 FROM control_imports WHERE import_id = ?", [f.content_hash]):
                    self._con.execute("INSERT INTO control_receipts VALUES (?, ?, ?)",
                                      [seq, f.content_hash, imported_at])
                    continue
                self._con.execute("INSERT INTO control_imports (import_id, kind, period, file_name, imported_at, "
                                  "seq, mapping) VALUES (?, ?, ?, ?, ?, ?, ?)",
                                  [f.content_hash, f.kind, f.period, f.file_name, imported_at, seq, f.mapping])
                table, columns = _CONTROL_TABLES[f.kind]
                marks = ", ".join("?" * (len(columns) + 2))
                self._con.executemany(
                    f"INSERT INTO {table} VALUES ({marks})",
                    [[f.content_hash, r.source.row, *(getattr(r, c) for c in columns)] for r in f.rows],
                )
            for e in events:
                self._insert_control_event(e)

        self._transaction(work)

    def control_imports(self) -> tuple[controls.ControlImport, ...]:
        """Every receipt of every import, oldest first: the first import, then each time it was received again."""
        imports = []
        for import_id, kind, period, file_name, imported_at, seq, mapping in self._all(
            "SELECT import_id, kind, period, file_name, imported_at, seq, mapping FROM control_imports UNION ALL "
            "SELECT i.import_id, kind, period, file_name, r.received_at, r.seq, mapping FROM control_receipts r "
            "JOIN control_imports i USING (import_id) ORDER BY seq", []
        ):
            table, columns = _CONTROL_TABLES[kind]
            build = controls.TEMPLATES[kind].build
            rows = tuple(
                build(**dict(zip(columns, r[1:])), source=controls.Source(file_name, r[0]))
                for r in self._all(f"SELECT row_number, {', '.join(columns)} FROM {table} WHERE import_id = ?"
                                   " ORDER BY row_number", [import_id])
            )
            imports.append(controls.ControlImport(import_id, kind, period, file_name, imported_at, seq, rows,
                                                  mapping))
        return tuple(imports)

    def _insert_control_event(self, e: controls.Event) -> None:
        seq = self._one("SELECT coalesce(max(seq), 0) + 1 FROM control_events", [])[0]
        self._con.execute("INSERT INTO control_events VALUES (?, ?, ?, ?, ?, ?, ?)",
                          [seq, e.kind, e.accrual_id, e.by, e.at, e.evidence, e.payload()])

    def add_control_event(self, e: controls.Event) -> None:
        self._transaction(lambda: self._insert_control_event(e))

    def control_events(self) -> tuple[controls.Event, ...]:
        rows = self._all("SELECT seq, kind, accrual_id, by_role, recorded_at, evidence, data FROM control_events ORDER BY seq", [])
        return tuple(controls.Event(kind, accrual_id, by, at, evidence, json.loads(data), seq)
                     for seq, kind, accrual_id, by, at, evidence, data in rows)

    def count(self, table: str) -> int:
        if table not in {"submissions", "receipts", "validations", "releases", "release_facts", "release_rates",
                         "control_imports", "control_events"}:
            raise ValueError(f"unknown table {table!r}")
        return self._one(f"SELECT count(*) FROM {table}", [])[0]
