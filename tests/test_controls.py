"""Close controls against docs/fixture-expected.md, section "Step 4" (C1-C10)."""

from datetime import datetime
from decimal import Decimal
from pathlib import Path

import pytest
from conftest import REPO

from finance_close_toolkit import cli, controls, demo
from finance_close_toolkit.config import load_workspace
from finance_close_toolkit.store import Store

EX = REPO / "examples" / "controls"
CONFIG = REPO / "config" / "close.toml"
LATER = datetime(2026, 6, 1)  # after every demo step
AP, ANALYST, ACCOUNTING, LEAD = "AP team", "SG finance analyst", "Accounting team", "Regional close lead"


@pytest.fixture
def ws():
    return load_workspace(CONFIG)


@pytest.fixture
def store(tmp_path):
    s = Store.open(tmp_path / "controls.duckdb")
    yield s
    s.close()


def at(store, ws, step: str, when: datetime = LATER) -> controls.Controls:
    """Replay the demo story through `step` (when given), then read the state."""
    if step:
        demo.replay_controls(store, ws, step)
    return controls.load(store, ws, when)


def states(c: controls.Controls) -> dict[str, tuple]:
    return {s.accrual_id: (s.invoiced, s.invoice_state, s.reversal_state, s.overall) for s in c.accruals}


def signals(c: controls.Controls) -> list[tuple]:
    return [(s.type, s.item, s.owner, s.amount_at_risk) for s in c.signals]


def files(**names: str) -> dict:
    return {kind: (name, (EX / name).read_bytes()) for kind, name in names.items()}


NOW = datetime(2026, 5, 10, 9, 0)


def test_c1_states_signals_owners_and_amounts_at_risk(store, ws):
    c = at(store, ws, "C1")
    assert states(c) == {
        "A-001": (110000, "invoiced_with_variance", "ledger_confirmed", "open"),
        "A-002": (40000, "invoiced", "missing", "open"),
        "A-003": (25000, "partially_invoiced", "ledger_confirmed", "open"),
        "A-004": (0, "awaiting_invoice", "ledger_confirmed", "open"),
        "A-005": (0, "awaiting_invoice", "ledger_confirmed", "open"),
        "A-006": (0, "awaiting_invoice", "ledger_confirmed", "open"),
    }
    links = {s.accrual_id: [(l.invoice.invoice_id, l.method) for l in s.links] for s in c.accruals}
    assert links == {"A-001": [("INV-5501", "reference")], "A-002": [("INV-5502", "reference")],
                     "A-003": [("INV-5503", "reference")], "A-004": [], "A-005": [], "A-006": []}
    reversals = {s.accrual_id: s.reversal.journal_id if s.reversal else None for s in c.accruals}
    assert reversals == {"A-001": "JE-0401", "A-002": None, "A-003": "JE-0402", "A-004": "JE-0403",
                         "A-005": "JE-0404", "A-006": "JE-0405"}
    assert c.accrual("A-001").tolerance == 5000
    assert signals(c) == [
        ("INVOICE_VARIANCE", "A-001", ANALYST, 10000),
        ("MISSING_REVERSAL", "A-002", ACCOUNTING, 40000),
        ("PARTIAL_INVOICE", "A-003", ANALYST, 35000),
        ("NO_OWNER", "A-006", LEAD, 8000),
        ("AMBIGUOUS_MATCH", "INV-5504", AP, 12000),
    ]
    assert c.candidates("INV-5504") == ("A-004", "A-005")
    assert {role: [s.item for s in sigs] for role, sigs in c.queues().items()} == {
        ANALYST: ["A-001", "A-003"], AP: ["INV-5504"], ACCOUNTING: ["A-002"], LEAD: ["A-006"]}


def test_c1_april_pl_expense_is_minus_5000(store, ws):
    c = at(store, ws, "C1")
    assert controls.pl_scopes(c) == (("SG", "2026-04"),)
    lines = {l.item: l.net for l in controls.pl_expense(c, "SG", "2026-04")}
    assert lines == {"A-001": 10000, "A-002": 40000, "A-003": -35000, "A-004": -12000, "A-005": -12000,
                     "A-006": -8000, "INV-5504": 12000}
    assert sum(lines.values()) == -5000


def test_c1_journal_preview_for_a001(store, ws):
    p = controls.journal_preview(at(store, ws, "C1").accrual("A-001"))
    assert [(e.period, e.debit, e.credit, e.amount) for e in p.entries] == [
        ("2026-03", "OTH", "Accrued liabilities", 100000),
        ("2026-04", "Accrued liabilities", "OTH", 100000),
        ("2026-04", "OTH", "Accounts payable", 110000),
    ]
    n = p.net
    assert (n.period, n.pl_expense, n.accrued_liabilities, n.accounts_payable, n.cash) == (
        "2026-04", 10000, 0, 110000, 0)


def test_c2_ap_link_clears_a004_and_ends_the_ambiguity(store, ws):
    at(store, ws, "C1")
    r = controls.link(store, ws, "INV-5504", "A-004", AP, now=NOW)
    assert (r.outcome, r.message) == ("recorded", "Linked INV-5504 to A-004.")
    c = controls.load(store, ws, LATER)
    assert states(c)["A-004"] == (12000, "invoiced", "ledger_confirmed", "cleared")
    assert states(c)["A-005"] == (0, "awaiting_invoice", "ledger_confirmed", "open")
    assert [s.type for s in c.signals] == ["INVOICE_VARIANCE", "MISSING_REVERSAL", "PARTIAL_INVOICE", "NO_OWNER"]
    assert not [s for s in c.signals if s.item in ("A-005", "INV-5504")]
    assert sum(l.net for l in controls.pl_expense(c, "SG", "2026-04")) == -5000


def test_c3_the_same_link_again_is_a_no_op(store, ws):
    at(store, ws, "C2")
    r = controls.link(store, ws, "INV-5504", "A-004", AP, now=NOW)
    assert (r.outcome, r.message) == ("no_op", "INV-5504 is already linked to A-004 (AP link). Nothing changed.")
    assert len(controls.load(store, ws, LATER).signals) == 4
    assert [e.kind for e in store.control_events()] == ["link"]


def test_c4_only_ap_may_link(store, ws):
    at(store, ws, "C3")
    r = controls.link(store, ws, "INV-5504", "A-005", ANALYST, now=NOW)
    assert r.outcome == "refused"
    assert r.message == "'SG finance analyst' may not link invoices; allowed: AP team. Nothing recorded."
    assert store.count("control_events") == 1
    assert states(controls.load(store, ws, LATER))["A-005"][1] == "awaiting_invoice"


def test_c5_explaining_the_variance_clears_a001(store, ws):
    at(store, ws, "C4")
    r = controls.explain(store, ws, "A-001", "Scope extension approved by SG GM", ANALYST, now=NOW)
    assert r.outcome == "recorded"
    c = controls.load(store, ws, LATER)
    assert states(c)["A-001"] == (110000, "invoiced_with_variance", "ledger_confirmed", "cleared")
    assert len(c.signals) == 3
    again = controls.explain(store, ws, "A-001", "Scope extension approved by SG GM", ANALYST, now=NOW)
    assert again.outcome == "no_op"
    assert store.count("control_events") == 2


def test_explain_is_for_the_owner_only(store, ws):
    at(store, ws, "C1")
    r = controls.explain(store, ws, "A-001", "note", AP, now=NOW)
    assert (r.outcome, r.message) == ("refused", "'AP team' may not explain A-001; allowed: SG finance analyst. "
                                                 "Nothing recorded.")
    assert store.count("control_events") == 0


def test_c6_assigning_an_owner_ends_no_owner(store, ws):
    at(store, ws, "C5")
    r = controls.assign(store, ws, "A-006", ANALYST, LEAD, now=NOW)
    assert r.outcome == "recorded"
    c = controls.load(store, ws, LATER)
    a6 = c.accrual("A-006")
    assert (a6.overall, a6.invoice_state, a6.owner) == ("open", "awaiting_invoice", ANALYST)
    assert not [s for s in c.signals if s.item == "A-006"]
    assert len(c.signals) == 2
    assert controls.assign(store, ws, "A-006", ANALYST, LEAD, now=NOW).outcome == "no_op"
    assert controls.assign(store, ws, "A-006", ANALYST, ANALYST, now=NOW).outcome == "refused"


def test_c7_the_second_gl_confirms_the_a002_reversal(store, ws):
    at(store, ws, "C6")
    [r] = controls.import_files(store, ws, files(gl="gl_2026-04_v2.csv"), NOW)
    assert r.message == "Imported gl_2026-04_v2.csv as gl 2026-04 (6 rows), replacing gl_2026-04.csv."
    c = controls.load(store, ws, LATER)
    a2 = c.accrual("A-002")
    assert (a2.reversal_state, a2.reversal.journal_id, a2.overall) == ("ledger_confirmed", "JE-0406", "cleared")
    assert signals(c) == [("PARTIAL_INVOICE", "A-003", ANALYST, 35000)]
    # The older extract stays stored; status uses the newest.
    assert [i.file_name for i in store.control_imports() if i.kind == "gl"] == ["gl_2026-04.csv", "gl_2026-04_v2.csv"]


def test_c8_the_approver_must_differ_from_the_preparer(store, ws):
    at(store, ws, "C7")
    r = controls.resolve(store, ws, "A-003", "re_accrue", Decimal(35000), "Phase 2 invoice due May", ANALYST,
                         ANALYST, now=NOW)
    assert (r.outcome, r.message) == (
        "refused", "The approver must differ from the person resolving (SG finance analyst). Nothing recorded.")
    assert [e.kind for e in store.control_events()] == ["link", "explain", "assign_owner"]


def test_c9_an_approved_resolution_resolves_a003_and_ends_the_story(store, ws):
    at(store, ws, "C8")
    r = controls.resolve(store, ws, "A-003", "re_accrue", Decimal(35000), "Phase 2 invoice due May", ANALYST,
                         LEAD, now=NOW)
    assert r.outcome == "recorded"
    c = controls.load(store, ws, LATER)
    assert c.signals == ()
    assert {s.accrual_id: s.overall for s in c.accruals} == {
        "A-001": "cleared", "A-002": "cleared", "A-003": "resolved", "A-004": "cleared", "A-005": "open",
        "A-006": "open"}
    assert {s.accrual_id: s.invoice_state for s in c.accruals if s.overall == "open"} == {
        "A-005": "awaiting_invoice", "A-006": "awaiting_invoice"}
    [event] = [h for h in controls.history(c, "A-003") if h.what.startswith("Resolved")]
    assert (event.what, event.by) == ("Resolved: re_accrue 35,000.00", ANALYST)
    assert "approved by Regional close lead" in event.detail and "Phase 2 invoice due May" in event.detail
    again = controls.resolve(store, ws, "A-003", "re_accrue", Decimal(35000), "Phase 2 invoice due May", ANALYST,
                             LEAD, now=NOW)
    assert again.outcome == "no_op"


def test_a002_history_shows_missing_then_confirmed(store, ws):
    c = at(store, ws, "C9")
    assert [(h.what, h.detail) for h in controls.history(c, "A-002")] == [
        ("Accrual imported", "accruals_2026-03.csv row 3, 40,000.00"),
        ("Invoice INV-5502 imported", "invoices_2026-04.csv row 3, 40,000.00"),
        ("Reversal missing", "gl_2026-04.csv has no line reversing A-002"),
        ("Reversal confirmed", "JE-0406, gl_2026-04_v2.csv row 7"),
    ]


def test_c10_write_back_resolves_a002_while_the_reversal_stays_missing(store, ws):
    at(store, ws, "C1")
    r = controls.resolve(store, ws, "A-002", "write_back", Decimal(40000),
                         "Reversal posted manually as JE-0499, extract pending", ACCOUNTING, LEAD, now=NOW)
    assert r.outcome == "recorded"
    c = controls.load(store, ws, LATER)
    a2 = c.accrual("A-002")
    assert (a2.reversal_state, a2.overall) == ("missing", "resolved")
    assert "MISSING_REVERSAL" not in [s.type for s in c.signals]
    assert len(c.signals) == 4


def test_status_as_of_ignores_later_imports_and_events(store, ws):
    at(store, ws, "C9")
    c = controls.load(store, ws, demo.control_step("C1").at)
    assert len(c.signals) == 5 and c.events == ()


def test_importing_the_same_files_again_changes_nothing(store, ws):
    at(store, ws, "C1")
    results = controls.import_files(store, ws, files(accruals="accruals_2026-03.csv", gl="gl_2026-04.csv"), NOW)
    assert [r.outcome for r in results] == ["repeat", "repeat"]
    assert results[0].message == "Repeat: accruals_2026-03.csv is already imported as accruals 2026-03. Nothing changed."
    assert store.count("control_imports") == 3


def test_an_override_link_needs_a_reason_and_an_approver_and_is_recorded_as_one(store, ws):
    at(store, ws, "C1")
    plain = controls.link(store, ws, "INV-5501", "A-003", AP, now=NOW)
    assert plain.outcome == "refused" and "relinking needs an override" in plain.message
    assert controls.link(store, ws, "INV-5501", "A-003", AP, override=True, now=NOW).outcome == "refused"
    assert controls.link(store, ws, "INV-5501", "A-003", AP, override=True, reason="PO keyed wrong",
                         now=NOW).outcome == "refused"
    r = controls.link(store, ws, "INV-5501", "A-003", AP, override=True, reason="PO keyed wrong", approved_by=LEAD,
                      now=NOW)
    assert r.outcome == "recorded"
    c = controls.load(store, ws, LATER)
    assert [(l.invoice.invoice_id, l.method) for l in c.accrual("A-003").links] == [
        ("INV-5501", "override"), ("INV-5503", "reference")]
    assert c.accrual("A-001").invoice_state == "awaiting_invoice"


@pytest.mark.parametrize("text, message", [
    ("accrual_id,market,period,account,supplier,reference,amount,owner\n",
     "bad.csv is missing accruals column description (found: accrual_id, market, period, account, supplier, "
     "reference, amount, owner)"),
    ("accrual_id,market,period,account,supplier,reference,amount,owner,description\n"
     "A-1,SG,2026-03,OTH,S,,12k,x,d\n",
     "bad.csv refused:\n  - bad.csv row 2 amount: not an amount: '12k'"),
    ("accrual_id,market,period,account,supplier,reference,amount,owner,description\n"
     "A-1,SG,March 2026,OTH,S,,100,x,d\n",
     "bad.csv refused:\n  - bad.csv row 2 period: period must look like 2026-04, got 'March 2026'"),
])
def test_a_bad_csv_is_a_readable_refusal_and_stores_nothing(store, ws, text, message):
    with pytest.raises(controls.ControlsError) as e:
        controls.import_files(store, ws, {"accruals": ("bad.csv", text.encode())}, NOW)
    assert str(e.value) == message
    assert store.count("control_imports") == 0


def test_one_bad_file_refuses_the_whole_import(store, ws, tmp_path):
    bad = {"gl": ("gl.csv", b"journal_id,period,market,account,amount,reference,description\n"
                            b"J1,2026-04,XX,OTH,1,,d\n")}
    with pytest.raises(controls.ControlsError, match="row 2: unknown market 'XX'"):
        controls.import_files(store, ws, {**files(accruals="accruals_2026-03.csv"), **bad}, NOW)
    assert store.count("control_imports") == 0


# Through the CLI


def fct(capsys, store_path: Path, *args: str, code: int = 0) -> str:
    assert cli.main(["controls", *args, "--store", str(store_path), "--config", str(CONFIG)]) == code, args
    return capsys.readouterr().out


IMPORT_C1 = ("import", "--accruals", str(EX / "accruals_2026-03.csv"), "--invoices", str(EX / "invoices_2026-04.csv"),
             "--gl", str(EX / "gl_2026-04.csv"), "--as-of", "2026-05-04T09:00")
# The state-changing commands of the controls part of scripts/demo.sh, in its order (C1-C9).
CLI_STORY = [
    (0, IMPORT_C1),
    (0, ("link", "INV-5504", "A-004", "--by", AP, "--as-of", "2026-05-04T10:00")),
    (0, ("link", "INV-5504", "A-004", "--by", AP, "--as-of", "2026-05-04T10:05")),
    (1, ("link", "INV-5504", "A-005", "--by", ANALYST, "--as-of", "2026-05-04T10:10")),
    (0, ("explain", "A-001", "--note", "Scope extension approved by SG GM", "--by", ANALYST,
         "--as-of", "2026-05-04T11:00")),
    (0, ("assign", "A-006", "--owner", ANALYST, "--by", LEAD, "--as-of", "2026-05-04T12:00")),
    (0, ("import", "--gl", str(EX / "gl_2026-04_v2.csv"), "--as-of", "2026-05-05T09:00")),
    (1, ("resolve", "A-003", "--kind", "re_accrue", "--amount", "35000", "--reason", "Phase 2 invoice due May",
         "--by", ANALYST, "--approved-by", ANALYST, "--as-of", "2026-05-05T10:00")),
    (0, ("resolve", "A-003", "--kind", "re_accrue", "--amount", "35000", "--reason", "Phase 2 invoice due May",
         "--by", ANALYST, "--approved-by", LEAD, "--as-of", "2026-05-05T10:05")),
]


def test_cli_status_and_show_at_c1(capsys, tmp_path):
    path = tmp_path / "s.duckdb"
    assert "Imported accruals_2026-03.csv as accruals 2026-03 (6 rows)." in fct(capsys, path, *IMPORT_C1)
    out = fct(capsys, path, "status", "--as-of", "2026-05-04T09:30")
    assert "Needs attention: 5 signals" in out
    assert "Queues: SG finance analyst 2, AP team 1, Accounting team 1, Regional close lead 1" in out
    assert "3. AMBIGUOUS_MATCH  INV-5504  SGD 12,000.00 at risk" in out
    assert "Total     187,000.00  (192,000.00)   (5,000.00)" in out
    out = fct(capsys, path, "show", "A-001", "--as-of", "2026-05-04T09:30")
    assert "Net 2026-04: P&L expense 10,000.00; accrued liabilities 0.00; accounts payable 110,000.00; cash 0.00" in out
    assert "Invoice   INV-5501  invoices_2026-04.csv    2    110,000.00  linked: reference" in out
    assert fct(capsys, path, "show", "A-999", code=1).startswith("Refused: unknown accrual 'A-999'")


def test_cli_story_matches_the_library_replay(capsys, tmp_path, ws):
    path = tmp_path / "cli.duckdb"
    for code, args in CLI_STORY:
        fct(capsys, path, *args, code=code)
    with Store.open(path) as cli_store, Store.open(tmp_path / "lib.duckdb") as lib_store:
        demo.replay_controls(lib_store, ws, "C9")
        cli_c, lib_c = controls.load(cli_store, ws, LATER), controls.load(lib_store, ws, LATER)
        assert states(cli_c) == states(lib_c)
        assert [(e.kind, e.accrual_id, e.by, e.at, dict(e.data)) for e in cli_c.events] == [
            (e.kind, e.accrual_id, e.by, e.at, dict(e.data)) for e in lib_c.events]
        assert lib_c.signals == () and len(lib_c.events) == 4


def test_cli_bad_csv_is_refused_with_exit_1(capsys, tmp_path):
    bad = tmp_path / "accruals.csv"
    bad.write_text("accrual_id,market\nA-1,SG\n", encoding="utf-8")
    out = fct(capsys, tmp_path / "s.duckdb", "import", "--accruals", str(bad), code=1)
    assert out.startswith("Import refused. Nothing imported.\naccruals.csv is missing accruals columns period, ")
