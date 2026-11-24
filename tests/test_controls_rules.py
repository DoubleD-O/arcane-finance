"""Controller-review rules for close controls (docs/design-step4.md): reference checks, replacing imports,
segregation of duties on resolutions and explanations, and exact reversals."""

from datetime import datetime
from decimal import Decimal

import pytest
from test_controls import AP, ACCOUNTING, ANALYST, CONFIG, EX, LATER, LEAD, NOW, at, fct, signals, states

from finance_close_toolkit import controls
from finance_close_toolkit.config import load_workspace
from finance_close_toolkit.store import Store

LATER_STILL = datetime(2026, 5, 20, 9, 0)


@pytest.fixture
def ws():
    return load_workspace(CONFIG)


@pytest.fixture
def store(tmp_path):
    s = Store.open(tmp_path / "controls.duckdb")
    yield s
    s.close()


def edited(name: str, *replacements: tuple[str, str], drop: str = "", add: str = "") -> bytes:
    """An example CSV with text replaced, the line containing `drop` removed and `add` appended."""
    text = (EX / name).read_text(encoding="utf-8")
    for old, new in replacements:
        assert old in text, old
        text = text.replace(old, new)
    lines = [l for l in text.splitlines() if not (drop and drop in l)]
    return ("\n".join(lines + ([add] if add else [])) + "\n").encode()


def imp(store, ws, when=NOW, **files: tuple[str, bytes]):
    return controls.import_files(store, ws, files, when)


# MAJOR 1: a reference link must also agree on market, supplier, account and a later period


def test_a_reference_to_the_wrong_supplier_is_not_linked_and_goes_to_ap(store, ws):
    invoices = edited("invoices_2026-04.csv", ("INV-5502,SG,Lumen Media", "INV-5502,SG,Lumina Print"))
    imp(store, ws, accruals=("accruals_2026-03.csv", (EX / "accruals_2026-03.csv").read_bytes()),
        invoices=("invoices_2026-04.csv", invoices), gl=("gl_2026-04.csv", (EX / "gl_2026-04.csv").read_bytes()))
    c = controls.load(store, ws, LATER)
    assert c.accrual("A-002").links == ()
    [sig] = [s for s in c.signals if s.type == "REFERENCE_MISMATCH"]
    assert (sig.item, sig.owner, sig.amount_at_risk) == ("INV-5502", AP, 40000)
    assert "supplier" in sig.trigger and "Lumina Print" in sig.trigger and "Lumen Media" in sig.trigger
    assert "account" not in sig.trigger and "market" not in sig.trigger
    assert sig.next_action == "Confirm the invoice belongs to this accrual or correct the reference"


def test_a_reference_in_the_accrual_period_is_a_mismatch(store, ws):
    march = (b"invoice_id,market,supplier,invoice_date,period,account,reference,amount\n"
             b"INV-5501,SG,Harbour Advisory,2026-03-28,2026-03,OTH,PO-1001,100000.00\n")
    imp(store, ws, accruals=("accruals_2026-03.csv", (EX / "accruals_2026-03.csv").read_bytes()),
        invoices=("invoices_2026-03.csv", march))
    c = controls.load(store, ws, LATER)
    assert c.accrual("A-001").links == ()
    [sig] = [s for s in c.signals if s.type == "REFERENCE_MISMATCH"]
    assert sig.item == "INV-5501" and "period 2026-03 is not after the accrual period 2026-03" in sig.trigger


def test_ap_links_a_reference_mismatch_only_by_an_approved_override(store, ws):
    invoices = edited("invoices_2026-04.csv", ("INV-5502,SG,Lumen Media", "INV-5502,SG,Lumina Print"))
    imp(store, ws, accruals=("accruals_2026-03.csv", (EX / "accruals_2026-03.csv").read_bytes()),
        invoices=("invoices_2026-04.csv", invoices))
    assert controls.link(store, ws, "INV-5502", "A-002", AP, now=NOW).outcome == "refused"
    r = controls.link(store, ws, "INV-5502", "A-002", AP, override=True, reason="Lumen trades as Lumina", now=NOW)
    assert r.outcome == "refused" and "approved" in r.message
    r = controls.link(store, ws, "INV-5502", "A-002", AP, override=True, reason="Lumen trades as Lumina",
                      approved_by=AP, now=NOW)
    assert r.outcome == "refused"
    r = controls.link(store, ws, "INV-5502", "A-002", AP, override=True, reason="Lumen trades as Lumina",
                      approved_by=LEAD, now=NOW)
    assert r.outcome == "recorded"
    c = controls.load(store, ws, LATER)
    assert [(l.invoice.invoice_id, l.method) for l in c.accrual("A-002").links] == [("INV-5502", "override")]
    assert "REFERENCE_MISMATCH" not in [s.type for s in c.signals]
    [linked] = [e for e in store.control_events() if e.kind == "link"]
    assert linked.data["approved_by"] == LEAD and "supplier" in linked.data["mismatches"]


def test_override_choices_list_candidates_first_and_name_mismatches(store, ws):
    at(store, ws, "C1")
    c = controls.load(store, ws, LATER)
    choices = controls.override_choices(c, "INV-5504")
    assert [ch.accrual_id for ch in choices][:2] == ["A-004", "A-005"]
    by_id = {ch.accrual_id: ch for ch in choices}
    assert by_id["A-004"].mismatches == ()
    assert any("supplier" in m for m in by_id["A-001"].mismatches)
    assert any("account" in m for m in by_id["A-001"].mismatches)


# MAJOR 2: replacing imports report changes, refuse silent drops, and lapse explanations and resolutions


def test_a_replacing_import_reports_dropped_added_and_changed_rows(store, ws):
    imp(store, ws, accruals=("accruals_2026-03.csv", (EX / "accruals_2026-03.csv").read_bytes()))
    v2 = edited("accruals_2026-03.csv", ("PO-1001,100000.00,SG finance analyst", "PO-1001,90000.00,SG office manager"),
                add="A-007,SG,2026-03,OTH,Harbour Advisory,PO-1007,5000.00,SG finance analyst,April extra")
    [r] = imp(store, ws, when=LATER_STILL, accruals=("accruals_v2.csv", v2))
    assert r.outcome == "imported"
    assert r.changes == ("Added: A-007",
                         "Changed: A-001 amount 100,000.00 → 90,000.00; owner SG finance analyst → SG office manager")


def test_dropping_an_accrual_with_history_is_refused_unless_allowed_with_a_reason(store, ws, tmp_path, capsys):
    at(store, ws, "C2")
    v2 = edited("accruals_2026-03.csv", drop="A-004,")
    with pytest.raises(controls.ControlsError, match="drops A-004") as e:
        imp(store, ws, when=LATER_STILL, accruals=("accruals_v2.csv", v2))
    assert "--allow-drop" in str(e.value)
    assert store.count("control_imports") == 3 and store.count("control_events") == 1
    with pytest.raises(controls.ControlsError, match="reason"):
        controls.import_files(store, ws, {"accruals": ("accruals_v2.csv", v2)}, LATER_STILL, allow_drop=True,
                              by=LEAD)
    [r] = controls.import_files(store, ws, {"accruals": ("accruals_v2.csv", v2)}, LATER_STILL, allow_drop=True,
                                reason="A-004 raised in error", by=LEAD)
    assert r.changes[0] == "Dropped: A-004"
    drop = store.control_events()[-1]
    assert (drop.kind, drop.accrual_id, drop.by, drop.data["reason"]) == ("drop", "A-004", LEAD,
                                                                           "A-004 raised in error")


def test_cli_refuses_a_dropping_import_with_exit_1(capsys, tmp_path):
    path = tmp_path / "s.duckdb"
    fct(capsys, path, "import", "--accruals", str(EX / "accruals_2026-03.csv"), "--invoices",
        str(EX / "invoices_2026-04.csv"), "--as-of", "2026-05-04T09:00")
    v2 = tmp_path / "accruals_v2.csv"
    v2.write_bytes(edited("accruals_2026-03.csv", drop="A-001,"))
    out = fct(capsys, path, "import", "--accruals", str(v2), "--as-of", "2026-05-04T10:00", code=1)
    assert out.startswith("Import refused. Nothing imported.") and "drops A-001" in out
    out = fct(capsys, path, "import", "--accruals", str(v2), "--allow-drop", "--reason", "raised twice",
              "--by", LEAD, "--as-of", "2026-05-04T10:00")
    assert "Dropped: A-001" in out


def test_an_explanation_stops_covering_when_the_accrued_amount_changes(store, ws):
    at(store, ws, "C5")
    assert controls.load(store, ws, LATER).accrual("A-001").overall == "cleared"
    v2 = edited("accruals_2026-03.csv", ("PO-1001,100000.00", "PO-1001,10000.00"))
    imp(store, ws, when=LATER_STILL, accruals=("accruals_v2.csv", v2))
    c = controls.load(store, ws, LATER)
    assert c.accrual("A-001").overall == "open"
    assert ("INVOICE_VARIANCE", "A-001", ANALYST, 100000) in signals(c)
    assert ("Explanation no longer applies", "accrued changed 100,000.00 → 10,000.00") in [
        (h.what, h.detail) for h in controls.history(c, "A-001")]


def test_a_resolution_stops_covering_when_the_invoiced_total_changes(store, ws):
    at(store, ws, "C9")
    more = edited("invoices_2026-04.csv", add="INV-5505,SG,Northpoint Research,2026-04-28,2026-04,OTH,PO-1003,5000.00")
    imp(store, ws, when=LATER_STILL, invoices=("invoices_v2.csv", more))
    c = controls.load(store, ws, LATER)
    assert c.accrual("A-003").overall == "open"
    assert ("PARTIAL_INVOICE", "A-003", ANALYST, 30000) in signals(c)
    assert ("Resolution no longer applies", "invoiced changed 25,000.00 → 30,000.00") in [
        (h.what, h.detail) for h in controls.history(c, "A-003")]


# MAJOR 3: resolution rules


def test_the_approver_must_hold_an_approver_role(store, ws):
    at(store, ws, "C1")
    r = controls.resolve(store, ws, "A-002", "write_back", Decimal(40000), "manual JE", ACCOUNTING,
                         "SG office manager", now=NOW)
    assert r.outcome == "refused" and "Regional close lead, Accounting team" in r.message


def test_the_approver_must_differ_from_the_accrual_owner(store, ws):
    at(store, ws, "C1")
    assert controls.assign(store, ws, "A-002", LEAD, LEAD, now=NOW).outcome == "recorded"
    r = controls.resolve(store, ws, "A-002", "write_back", Decimal(40000), "manual JE", ACCOUNTING, LEAD, now=NOW)
    assert (r.outcome, r.message) == (
        "refused", "The approver must differ from the accrual's owner (Regional close lead). Nothing recorded.")


def test_the_amount_must_equal_the_amount_at_risk(store, ws):
    at(store, ws, "C1")
    r = controls.resolve(store, ws, "A-002", "write_back", Decimal("39999.99"), "manual JE", ACCOUNTING, LEAD,
                         now=NOW)
    assert r.outcome == "refused" and "40,000.00" in r.message
    assert store.count("control_events") == 0


@pytest.mark.parametrize("accrual, kind, issue, needle", [
    ("A-002", "re_accrue", "", "write_back, not_expected"),
    ("A-003", "write_back", "", "re_accrue, not_expected"),
    ("A-001", "not_expected", "", "explain"),
    ("A-006", "not_expected", "", "assign"),
    ("A-002", "write_back", "partial_invoice", "missing_reversal"),
])
def test_each_issue_allows_only_its_resolution_kinds(store, ws, accrual, kind, issue, needle):
    at(store, ws, "C1")
    amount = {"A-001": 10000, "A-002": 40000, "A-003": 35000, "A-006": 8000}[accrual]
    r = controls.resolve(store, ws, accrual, kind, Decimal(amount), "why", ACCOUNTING, LEAD, issue=issue, now=NOW)
    assert r.outcome == "refused" and needle in r.message, r.message


def test_two_open_issues_need_the_issue_named(store, ws):
    accruals = b"".join([
        b"accrual_id,market,period,account,supplier,reference,amount,owner,description\n",
        b"X-1,SG,2026-03,OTH,Harbour Advisory,PO-9,10000.00,,Two issues\n"])
    invoices = b"".join([
        b"invoice_id,market,supplier,invoice_date,period,account,reference,amount\n",
        b"INV-9,SG,Harbour Advisory,2026-04-03,2026-04,OTH,PO-9,2000.00\n"])
    gl = b"journal_id,period,market,account,amount,reference,description\nJE-9,2026-04,SG,OTH,-10000.00,X-1,rev\n"
    imp(store, ws, accruals=("a.csv", accruals), invoices=("i.csv", invoices), gl=("g.csv", gl))
    r = controls.resolve(store, ws, "X-1", "re_accrue", Decimal(8000), "May invoice", ACCOUNTING, LEAD, now=NOW)
    assert r.outcome == "refused" and "no_owner, partial_invoice" in r.message
    r = controls.resolve(store, ws, "X-1", "re_accrue", Decimal(8000), "May invoice", ACCOUNTING, LEAD,
                         issue="partial_invoice", now=NOW)
    assert r.outcome == "recorded"
    c = controls.load(store, ws, LATER)
    assert c.accrual("X-1").overall == "resolved" and c.signals == ()


def test_a_re_accrual_shows_as_a_pending_entry_not_in_the_ledger(store, ws, capsys, tmp_path):
    at(store, ws, "C9")
    c = controls.load(store, ws, LATER)
    [a3] = [l for l in controls.pl_expense(c, "SG", "2026-04") if l.item == "A-003"]
    assert (a3.net, a3.pending) == (-35000, 35000)
    [entry] = controls.pending_entries(c.accrual("A-003"))
    assert (entry.period, entry.debit, entry.credit, entry.amount) == ("2026-04", "OTH", "Accrued liabilities", 35000)


def test_a_write_back_shows_as_a_pending_reversal(store, ws):
    at(store, ws, "C1")
    controls.resolve(store, ws, "A-002", "write_back", Decimal(40000), "JE-0499 posted", ACCOUNTING, LEAD, now=NOW)
    c = controls.load(store, ws, LATER)
    [a2] = [l for l in controls.pl_expense(c, "SG", "2026-04") if l.item == "A-002"]
    assert (a2.net, a2.pending) == (40000, -40000)


def test_cli_status_and_show_label_pending_entries(capsys, tmp_path):
    from test_controls import CLI_STORY
    path = tmp_path / "s.duckdb"
    for code, args in CLI_STORY:
        fct(capsys, path, *args, code=code)
    out = fct(capsys, path, "status", "--as-of", "2026-05-05T11:00")
    assert "Pending entries, not yet in the ledger" in out and "A-003 re_accrue +35,000.00 in 2026-04" in out
    out = fct(capsys, path, "show", "A-003", "--as-of", "2026-05-05T11:00")
    assert "Pending entries, not yet in the ledger" in out


# MINOR 2: a large explanation needs an approver


def test_an_explanation_over_the_threshold_needs_an_approver(store, ws):
    big = edited("invoices_2026-04.csv", ("PO-1001,110000.00", "PO-1001,130000.00"))
    imp(store, ws, accruals=("accruals_2026-03.csv", (EX / "accruals_2026-03.csv").read_bytes()),
        invoices=("invoices_2026-04.csv", big))
    r = controls.explain(store, ws, "A-001", "Scope extension", ANALYST, now=NOW)
    assert r.outcome == "refused" and "Regional close lead, Accounting team" in r.message
    assert controls.explain(store, ws, "A-001", "Scope extension", ANALYST, approved_by=ANALYST,
                            now=NOW).outcome == "refused"
    r = controls.explain(store, ws, "A-001", "Scope extension", ANALYST, approved_by=LEAD, now=NOW)
    assert r.outcome == "recorded"
    assert store.control_events()[-1].data["approved_by"] == LEAD


# MINOR 3: reversals must total exactly minus the accrual


def test_a_duplicate_reversal_line_is_a_reversal_mismatch(store, ws):
    gl = edited("gl_2026-04.csv", add="JE-0402B,2026-04,SG,OTH,-60000.00,A-003,Reversal of A-003 (again)")
    imp(store, ws, accruals=("accruals_2026-03.csv", (EX / "accruals_2026-03.csv").read_bytes()),
        invoices=("invoices_2026-04.csv", (EX / "invoices_2026-04.csv").read_bytes()), gl=("gl.csv", gl))
    c = controls.load(store, ws, LATER)
    a3 = c.accrual("A-003")
    assert a3.reversal_state == "mismatch"
    assert ("REVERSAL_MISMATCH", "A-003", ACCOUNTING, 60000) in signals(c)
    [line] = [l for l in controls.pl_expense(c, "SG", "2026-04") if l.item == "A-003"]
    assert line.reversals == -120000
