import json
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Callable, Iterator

import duckdb

from . import controls, identity, workflow
from .identity import Actor, Credentials, Person, User, UserEvent
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
ALTER TABLE control_events ADD COLUMN IF NOT EXISTS by_user TEXT;
ALTER TABLE control_events ADD COLUMN IF NOT EXISTS by_name TEXT;
ALTER TABLE receipts ADD COLUMN IF NOT EXISTS by_user TEXT;
ALTER TABLE receipts ADD COLUMN IF NOT EXISTS by_name TEXT;
ALTER TABLE receipts ADD COLUMN IF NOT EXISTS by_role TEXT;
CREATE TABLE IF NOT EXISTS users (
    username TEXT PRIMARY KEY, display_name TEXT NOT NULL, roles TEXT NOT NULL, active BOOLEAN NOT NULL,
    password_hash TEXT NOT NULL, created_by TEXT NOT NULL, created_at TIMESTAMP NOT NULL,
    password_changed_at TIMESTAMP NOT NULL, failed_sign_ins INTEGER NOT NULL DEFAULT 0, locked_until TIMESTAMP
);
CREATE TABLE IF NOT EXISTS user_events (
    seq INTEGER PRIMARY KEY, kind TEXT NOT NULL, username TEXT NOT NULL, by_user TEXT, by_role TEXT NOT NULL,
    recorded_at TIMESTAMP NOT NULL, detail TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sessions (
    token_hash TEXT PRIMARY KEY, username TEXT NOT NULL, created_at TIMESTAMP NOT NULL,
    expires_at TIMESTAMP NOT NULL
);
CREATE TABLE IF NOT EXISTS approval_requests (
    request_id INTEGER PRIMARY KEY, action TEXT NOT NULL, item TEXT NOT NULL, args TEXT NOT NULL,
    summary TEXT NOT NULL, owner TEXT, by_user TEXT NOT NULL, by_name TEXT NOT NULL, by_role TEXT NOT NULL,
    requested_at TIMESTAMP NOT NULL
);
CREATE TABLE IF NOT EXISTS approval_decisions (
    request_id INTEGER PRIMARY KEY, decision TEXT NOT NULL CHECK (decision IN ('approved', 'rejected')),
    by_user TEXT NOT NULL, by_name TEXT NOT NULL, by_role TEXT NOT NULL, requester_user TEXT NOT NULL,
    decided_at TIMESTAMP NOT NULL, reason TEXT NOT NULL, CHECK (by_user <> requester_user)
);
ALTER TABLE approval_requests ADD COLUMN IF NOT EXISTS facts TEXT;
-- logged_at: the real clock when the row was written in signed-in mode (NULL in open mode). The older time
-- columns (received_at, imported_at, recorded_at, requested_at, decided_at) hold the effective time an action
-- claims with --as-of or --received-at, which derived state keeps using.
ALTER TABLE receipts ADD COLUMN IF NOT EXISTS logged_at TIMESTAMP;
ALTER TABLE control_imports ADD COLUMN IF NOT EXISTS logged_at TIMESTAMP;
ALTER TABLE control_receipts ADD COLUMN IF NOT EXISTS logged_at TIMESTAMP;
ALTER TABLE control_events ADD COLUMN IF NOT EXISTS logged_at TIMESTAMP;
ALTER TABLE approval_requests ADD COLUMN IF NOT EXISTS logged_at TIMESTAMP;
ALTER TABLE approval_decisions ADD COLUMN IF NOT EXISTS logged_at TIMESTAMP;
-- The needs-attention workflow (docs/design-attention.md): observations (appeared, evidence, cleared) and
-- actions (ack, assign, due, comment) on an item key, append-only.
CREATE TABLE IF NOT EXISTS attention_events (
    seq INTEGER PRIMARY KEY, kind TEXT NOT NULL, item_key TEXT NOT NULL, recorded_at TIMESTAMP NOT NULL,
    by_role TEXT, by_user TEXT, by_name TEXT, data TEXT NOT NULL, logged_at TIMESTAMP
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
        self._depth = 0  # nesting of atomic(); only the outermost one begins and commits
        self.clock: Callable[[], datetime] = identity.clock

    def _logged(self) -> datetime | None:
        """The real clock in signed-in mode; open mode records only the time an action gives."""
        return self.clock() if self.user_count() else None

    @classmethod
    def open(cls, path: Path | str) -> "Store":
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        con = duckdb.connect(str(path))
        con.execute(_SCHEMA)
        # attention_events.seq comes from a sequence, so concurrent writers never pick the same number.
        if not con.execute("SELECT 1 FROM duckdb_sequences() WHERE sequence_name = 'attention_seq'").fetchone():
            start = con.execute("SELECT coalesce(max(seq), 0) + 1 FROM attention_events").fetchone()[0]
            con.execute(f"CREATE SEQUENCE IF NOT EXISTS attention_seq START {int(start)}")
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

    @contextmanager
    def atomic(self) -> Iterator[None]:
        """One transaction around everything inside, however deeply nested: a data change and the needs-attention
        observations it causes commit together or not at all."""
        if self._depth:
            self._depth += 1
            try:
                yield
            finally:
                self._depth -= 1
            return
        self._con.execute("BEGIN TRANSACTION")
        self._depth = 1
        try:
            yield
        except BaseException:
            self._con.execute("ROLLBACK")
            raise
        else:
            self._con.execute("COMMIT")
        finally:
            self._depth = 0

    def _transaction(self, work: Callable[[], None]) -> None:
        with self.atomic():
            work()

    def _save_blob(self, version: str, kind: str, content: str) -> None:
        self._con.execute(
            "INSERT INTO config_blobs VALUES (?, ?, ?) ON CONFLICT DO NOTHING", [version, kind, content]
        )

    def config_blob(self, version: str) -> str | None:
        row = self._one("SELECT content FROM config_blobs WHERE version = ?", [version])
        return row[0] if row else None

    _NEWEST_RECEIPT = (
        "SELECT s.submission_id, s.market, s.period, s.scenario, s.file_name, s.sheet, r.received_at, r.logged_at"
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

    def _insert_receipt(self, submission_id: str, received_at: datetime, by: Actor | None = None) -> None:
        seq = self._one("SELECT coalesce(max(seq), 0) + 1 FROM receipts", [])[0]
        person = by.person if by else None
        self._con.execute(
            "INSERT INTO receipts (seq, submission_id, received_at, by_user, by_name, by_role, logged_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            [seq, submission_id, received_at, person.username if person else None,
             person.display_name if person else None, by.role if by and by.role else None, self._logged()],
        )

    def add_receipt(self, submission_id: str, received_at: datetime, by: Actor | None = None) -> None:
        self._transaction(lambda: self._insert_receipt(submission_id, received_at, by))

    def receipt_actors(self, submission_id: str) -> tuple[Actor | None, ...]:
        """Who sent each receipt of a submission, oldest first; None for a receipt recorded in open mode."""
        rows = self._all("SELECT by_role, by_user, by_name FROM receipts WHERE submission_id = ? ORDER BY seq",
                         [submission_id])
        return tuple(Actor(role, Person(user, name) if user else None) if role else None
                     for role, user, name in rows)

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
        by: Actor | None = None,
    ) -> None:
        def work() -> None:
            s = submission
            self._con.execute(
                "INSERT INTO submissions VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                [s.submission_id, content_hash, s.market, s.period, s.scenario, s.file_name, s.sheet,
                 json.dumps(list(headers))],
            )
            self._insert_receipt(s.submission_id, s.received_at, by)
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
            logged = self._logged()
            for f in files:
                seq = self._one("SELECT coalesce(max(seq), 0) + 1 FROM (SELECT seq FROM control_imports "
                                "UNION ALL SELECT seq FROM control_receipts)", [])[0]
                if self._one("SELECT 1 FROM control_imports WHERE import_id = ?", [f.content_hash]):
                    self._con.execute("INSERT INTO control_receipts (seq, import_id, received_at, logged_at) "
                                      "VALUES (?, ?, ?, ?)", [seq, f.content_hash, imported_at, logged])
                    continue
                self._con.execute("INSERT INTO control_imports (import_id, kind, period, file_name, imported_at, "
                                  "seq, mapping, logged_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                                  [f.content_hash, f.kind, f.period, f.file_name, imported_at, seq, f.mapping,
                                   logged])
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
        for import_id, kind, period, file_name, imported_at, seq, mapping, logged_at in self._all(
            "SELECT import_id, kind, period, file_name, imported_at, seq, mapping, logged_at FROM control_imports "
            "UNION ALL SELECT i.import_id, kind, period, file_name, r.received_at, r.seq, mapping, r.logged_at "
            "FROM control_receipts r JOIN control_imports i USING (import_id) ORDER BY seq", []
        ):
            table, columns = _CONTROL_TABLES[kind]
            build = controls.TEMPLATES[kind].build
            rows = tuple(
                build(**dict(zip(columns, r[1:])), source=controls.Source(file_name, r[0]))
                for r in self._all(f"SELECT row_number, {', '.join(columns)} FROM {table} WHERE import_id = ?"
                                   " ORDER BY row_number", [import_id])
            )
            imports.append(controls.ControlImport(import_id, kind, period, file_name, imported_at, seq, rows,
                                                  mapping, logged_at))
        return tuple(imports)

    def _insert_control_event(self, e: controls.Event) -> None:
        seq = self._one("SELECT coalesce(max(seq), 0) + 1 FROM control_events", [])[0]
        p = e.person
        self._con.execute("INSERT INTO control_events (seq, kind, accrual_id, by_role, recorded_at, evidence, data, "
                          "by_user, by_name, logged_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                          [seq, e.kind, e.accrual_id, e.by, e.at, e.evidence, e.payload(),
                           p.username if p else None, p.display_name if p else None, self._logged()])

    def add_control_event(self, e: controls.Event, decision: controls.Decision | None = None) -> None:
        """With `decision`, the approval that makes this event happen is stored in the same transaction."""

        def work() -> None:
            self._insert_control_event(e)
            if decision is not None:
                self._insert_decision(decision)

        self._transaction(work)

    def control_events(self) -> tuple[controls.Event, ...]:
        rows = self._all("SELECT seq, kind, accrual_id, by_role, recorded_at, evidence, data, by_user, by_name, "
                         "logged_at FROM control_events ORDER BY seq", [])
        return tuple(controls.Event(kind, accrual_id, by, at, evidence, json.loads(data), seq,
                                    Person(user, name) if user else None, logged)
                     for seq, kind, accrual_id, by, at, evidence, data, user, name, logged in rows)

    # Two-person approvals: requests and decisions, append-only

    def add_request(self, r: controls.Request) -> int:
        request_id = self._one("SELECT coalesce(max(request_id), 0) + 1 FROM approval_requests", [])[0]
        p = r.requester.person
        self._con.execute(
            "INSERT INTO approval_requests (request_id, action, item, args, summary, owner, by_user, by_name, by_role, "
            "requested_at, facts, logged_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [request_id, r.action, r.item, json.dumps(dict(r.args), sort_keys=True), json.dumps(list(r.summary)),
             r.owner, p.username, p.display_name, r.requester.role, r.at, json.dumps(dict(r.facts)),
             self._logged()],
        )
        return request_id

    def requests(self) -> tuple[controls.Request, ...]:
        rows = self._all("SELECT request_id, action, item, args, summary, owner, by_user, by_name, by_role, "
                         "requested_at, facts, logged_at FROM approval_requests ORDER BY request_id", [])
        # A request stored before facts were kept has none, so approving it is refused as changed facts.
        return tuple(controls.Request(i, action, item, json.loads(args), Actor(role, Person(user, name)), at,
                                      tuple(json.loads(summary)), owner, json.loads(facts or "{}"), logged)
                     for i, action, item, args, summary, owner, user, name, role, at, facts, logged in rows)

    def _insert_decision(self, d: controls.Decision) -> None:
        p = d.approver.person
        self._con.execute("INSERT INTO approval_decisions (request_id, decision, by_user, by_name, by_role, "
                          "requester_user, decided_at, reason, logged_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                          [d.request_id, "approved" if d.approved else "rejected", p.username, p.display_name,
                           d.approver.role, d.requester.username, d.at, d.reason, self._logged()])

    def add_decision(self, d: controls.Decision) -> None:
        self._transaction(lambda: self._insert_decision(d))

    def decisions(self) -> dict[int, controls.Decision]:
        rows = self._all("SELECT d.request_id, decision, d.by_user, d.by_name, d.by_role, r.by_user, r.by_name, "
                         "decided_at, reason, d.logged_at FROM approval_decisions d JOIN approval_requests r "
                         "USING (request_id)", [])
        return {i: controls.Decision(i, decision == "approved", Actor(role, Person(user, name)),
                                     Person(ruser, rname), at, reason, logged)
                for i, decision, user, name, role, ruser, rname, at, reason, logged in rows}

    # Needs-attention workflow events, append-only

    def add_attention_events(self, events: tuple[workflow.Event, ...]) -> None:
        def work() -> None:
            logged = self._logged()
            for e in events:
                p = e.by.person if e.by else None
                self._con.execute(
                    "INSERT INTO attention_events VALUES (nextval('attention_seq'), ?, ?, ?, ?, ?, ?, ?, ?)",
                    [e.kind, e.key, e.at, e.by.role if e.by else None, p.username if p else None,
                     p.display_name if p else None, json.dumps(dict(e.data), sort_keys=True), logged])

        if events:
            self._transaction(work)

    def attention_events(self) -> tuple[workflow.Event, ...]:
        rows = self._all("SELECT seq, kind, item_key, recorded_at, by_role, by_user, by_name, data, logged_at "
                         "FROM attention_events ORDER BY seq", [])
        return tuple(workflow.Event(kind, key, at, Actor(role, Person(user, name) if user else None)
                                    if role is not None else None, json.loads(data), seq, logged)
                     for seq, kind, key, at, role, user, name, data, logged in rows)

    def submission_periods(self) -> tuple[str, ...]:
        return tuple(r[0] for r in self._all("SELECT DISTINCT period FROM submissions ORDER BY period", []))

    # Users and sessions

    _USER_COLS = "username, display_name, roles, active, created_by, created_at, password_changed_at"

    @staticmethod
    def _user_row(row: tuple) -> User:
        username, name, roles, active, created_by, created_at, changed_at = row
        return User(username, name, tuple(json.loads(roles)), active, created_by, created_at, changed_at)

    def user_count(self) -> int:
        return self._one("SELECT count(*) FROM users", [])[0]

    def user(self, username: str) -> User | None:
        row = self._one(f"SELECT {self._USER_COLS} FROM users WHERE username = ?", [username])
        return self._user_row(row) if row else None

    def users(self) -> tuple[User, ...]:
        return tuple(self._user_row(r) for r in self._all(f"SELECT {self._USER_COLS} FROM users ORDER BY username",
                                                          []))

    def credentials(self, username: str) -> Credentials | None:
        row = self._one("SELECT password_hash, failed_sign_ins, locked_until FROM users WHERE username = ?",
                        [username])
        return Credentials(*row) if row else None

    def _insert_user_event(self, e: UserEvent) -> None:
        seq = self._one("SELECT coalesce(max(seq), 0) + 1 FROM user_events", [])[0]
        self._con.execute("INSERT INTO user_events VALUES (?, ?, ?, ?, ?, ?, ?)",
                          [seq, e.kind, e.username, e.by.person.username if e.by.person else None, e.by.role, e.at,
                           e.detail])

    def user_events(self) -> tuple[tuple, ...]:
        """(kind, username, by_user, by_role, recorded_at, detail), oldest first."""
        return tuple(self._all("SELECT kind, username, by_user, by_role, recorded_at, detail FROM user_events "
                               "ORDER BY seq", []))

    def add_user(self, u: User, password_hash: str, event: UserEvent) -> None:
        def work() -> None:
            self._con.execute(
                "INSERT INTO users VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, NULL)",
                [u.username, u.display_name, json.dumps(list(u.roles)), u.active, password_hash, u.created_by,
                 u.created_at, u.password_changed_at],
            )
            self._insert_user_event(event)

        self._transaction(work)

    def update_user(self, u: User, event: UserEvent, end_sessions: bool = False) -> None:
        def work() -> None:
            self._con.execute("UPDATE users SET display_name = ?, roles = ?, active = ? WHERE username = ?",
                              [u.display_name, json.dumps(list(u.roles)), u.active, u.username])
            self._insert_user_event(event)
            if end_sessions:
                self._con.execute("DELETE FROM sessions WHERE username = ?", [u.username])

        self._transaction(work)

    def set_password(self, username: str, password_hash: str, at: datetime, event: UserEvent) -> None:
        def work() -> None:
            self._con.execute("UPDATE users SET password_hash = ?, password_changed_at = ?, failed_sign_ins = 0, "
                              "locked_until = NULL WHERE username = ?", [password_hash, at, username])
            self._con.execute("DELETE FROM sessions WHERE username = ?", [username])
            self._insert_user_event(event)

        self._transaction(work)

    def record_sign_in_failure(self, username: str, failed: int, locked_until: datetime | None) -> None:
        self._con.execute("UPDATE users SET failed_sign_ins = ?, locked_until = ? WHERE username = ?",
                          [failed, locked_until, username])

    def remove_all_users(self, event: UserEvent) -> int:
        removed = self.user_count()

        def work() -> None:
            self._con.execute("DELETE FROM sessions")
            self._con.execute("DELETE FROM users")
            self._insert_user_event(event)

        self._transaction(work)
        return removed

    def add_session(self, token_hash: str, username: str, created_at: datetime, expires_at: datetime) -> None:
        self._con.execute("INSERT INTO sessions VALUES (?, ?, ?, ?)", [token_hash, username, created_at, expires_at])

    def session(self, token_hash: str) -> tuple[str, datetime] | None:
        return self._one("SELECT username, expires_at FROM sessions WHERE token_hash = ?", [token_hash])

    def end_session(self, token_hash: str) -> None:
        self._con.execute("DELETE FROM sessions WHERE token_hash = ?", [token_hash])

    def purge_sessions(self, now: datetime) -> None:
        """Expired sessions, and any of a user who is gone or disabled."""
        self._con.execute("DELETE FROM sessions WHERE expires_at <= ? OR username NOT IN "
                          "(SELECT username FROM users WHERE active)", [now])

    def session_count(self) -> int:
        return self._one("SELECT count(*) FROM sessions", [])[0]

    def count(self, table: str) -> int:
        if table not in {"submissions", "receipts", "validations", "releases", "release_facts", "release_rates",
                         "control_imports", "control_events"}:
            raise ValueError(f"unknown table {table!r}")
        return self._one(f"SELECT count(*) FROM {table}", [])[0]
