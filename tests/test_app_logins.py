"""The UI in signed-in mode through AppTest: the sign-in form, the role picker, publishing and the Approvals page."""

from datetime import datetime
from pathlib import Path

import pytest
from conftest import REPO, copy_inputs
from streamlit.testing.v1 import AppTest
from test_app import APP, assert_clean, button, errors, shown

from finance_close_toolkit import controls, demo, identity
from finance_close_toolkit.config import load_workspace
from finance_close_toolkit.store import Store

CONFIG = REPO / "config" / "close.toml"
EX = REPO / "examples" / "controls"
PASSWORD = "correct horse battery"
T0 = datetime(2026, 5, 4, 9, 0)
LEAD, ACCOUNTING = "Regional close lead", "Accounting team"


def add_team(store, ws) -> None:
    admin = identity.act(identity.add_user(store, ws, None, "ada", "Ada Admin", ["admin"], PASSWORD, T0))
    for username, name, roles in (("cam", "Cam Accounts", [ACCOUNTING]), ("lee", "Lee Lead", [LEAD, ACCOUNTING]),
                                  ("pat", "Pat Payables", ["AP team"]),
                                  ("fran", "Fran Planner", ["Regional FP&A lead"]),
                                  ("sam", "Sam Singapore", ["SG country controller"]),
                                  ("alex", "Alex Australia", ["AU country controller"])):
        identity.add_user(store, ws, admin, username, name, roles, PASSWORD, T0)


@pytest.fixture
def controls_store(tmp_path, monkeypatch):
    """Signed-in mode with the C1 files imported by AP."""
    path = tmp_path / "s.duckdb"
    ws = load_workspace(CONFIG)
    with Store.open(path) as store:
        add_team(store, ws)
        pat = identity.act(store.user("pat"))
        controls.import_files(store, ws, {kind: (name, (EX / name).read_bytes()) for kind, name in (
            ("accruals", "accruals_2026-03.csv"), ("invoices", "invoices_2026-04.csv"), ("gl", "gl_2026-04.csv"))},
            T0, by=pat)
    monkeypatch.setenv("FCT_STORE", str(path))
    monkeypatch.setenv("FCT_CONFIG", str(CONFIG))
    return path


def open_app() -> AppTest:
    at = AppTest.from_file(str(APP), default_timeout=60)
    at.run()
    assert_clean(at)
    return at


def sign_in(at: AppTest, username: str, password: str = PASSWORD) -> None:
    at.text_input(key="sign_in_username").set_value(username)
    at.text_input(key="sign_in_password").set_value(password)
    at.button(key="sign_in_submit").click().run()


def go(at: AppTest, page: str) -> None:
    at.sidebar.radio(key="page").set_value(page).run()
    assert_clean(at)


def test_open_mode_says_anyone_can_act_as_any_role():
    at = open_app()
    assert identity.OPEN_MODE in [shown(c.value) for c in at.sidebar.caption]


def test_sign_in_wrong_password_role_picker_and_sign_out(controls_store):
    at = open_app()
    assert at.header[0].value == "Sign in"
    sign_in(at, "lee", "not the password")
    assert errors(at) == ["Wrong username or password."]
    assert at.header[0].value == "Sign in"

    sign_in(at, "lee")
    assert_clean(at)
    assert at.header[0].value != "Sign in"
    assert "Signed in as Lee Lead (lee)" in [shown(m.value) for m in at.sidebar.markdown]
    assert at.sidebar.selectbox(key="acting_role").options == [LEAD, ACCOUNTING]
    assert PASSWORD not in repr({k: at.session_state[k] for k in at.session_state})

    go(at, "Close controls")
    assert [s.key for s in at.selectbox if s.key == "controls_role"] == []
    assert "Acting as Lee Lead (Regional close lead); change the role in the sidebar." in [
        shown(c.value) for c in at.caption]

    button(at.sidebar, "Sign out").click().run()
    assert at.header[0].value == "Sign in"


def test_a_resolution_is_requested_in_the_ui_and_approved_on_the_approvals_page(controls_store):
    at = open_app()
    sign_in(at, "cam")
    go(at, "Close controls")
    assert [s for s in at.selectbox if s.key == "resolve_approver_A-002"] == []
    at.text_input(key="resolve_reason_A-002").set_value("Reversal posted manually as JE-0499")
    at.button(key="resolve_A-002").click().run()
    assert_clean(at)
    assert shown(at.success[0].value).startswith("Request 1: Resolution of A-002 missing_reversal: write_back "
                                                 "SGD 40,000.00.")
    go(at, "Approvals")
    assert at.header[0].value == "Approvals"
    at.button(key="approve_1").click().run()
    assert errors(at) == ["You requested 1; another person must decide it from their own sign-in. "
                          "Nothing recorded."]
    button(at.sidebar, "Sign out").click().run()

    sign_in(at, "lee")
    go(at, "Approvals")
    page = "\n".join(shown(m.value) for m in at.markdown)
    assert "Request 1: Resolution of A-002 missing_reversal: write_back SGD 40,000.00" in page
    assert "- Requested by Cam Accounts (Accounting team)" in page
    assert "- Reason: Reversal posted manually as JE-0499" in page
    at.button(key="reject_1").click().run()
    assert errors(at) == ["A rejection needs a reason. Nothing recorded."]
    at.button(key="approve_1").click().run()
    assert_clean(at)
    assert shown(at.success[0].value) == ("Resolved A-002: write_back 40,000.00, approved by Lee Lead "
                                          "(Regional close lead).")
    assert any(shown(m.value).startswith("Waiting for approval: 0.") for m in at.markdown)
    with Store.open(controls_store) as store:
        c = controls.load(store, load_workspace(CONFIG))
        assert c.accrual("A-002").overall == "resolved"
        [(_, decision)] = controls.approvals(store)
        assert (decision.approver.label, decision.requester.username) == ("Lee Lead (Regional close lead)", "cam")


def test_publishing_signed_in_records_the_person(tmp_path, monkeypatch):
    copy_inputs(tmp_path)
    config = tmp_path / "config" / "close.toml"
    ws = load_workspace(config)
    path = tmp_path / "s.duckdb"
    with Store.open(path) as store:
        add_team(store, ws)
        actors = {"SG country controller": identity.act(store.user("sam")),
                  "AU country controller": identity.act(store.user("alex"))}
        demo.replay(store, tmp_path, "E5", examples=tmp_path / "examples", actors=actors)
    monkeypatch.setenv("FCT_STORE", str(path))
    monkeypatch.setenv("FCT_CONFIG", str(config))
    at = open_app()
    sign_in(at, "fran")
    at.sidebar.text_input(key="period").set_value("2026-10").run()
    go(at, "Review and publish")
    assert [t for t in at.text_input if t.key == "approved_by"] == []
    button(at, "Approve and publish").click().run()
    assert_clean(at)
    assert shown(at.success[0].value) == ("Published 2026-10-R1 (previous none), approved by Fran Planner "
                                          "(Regional FP&A lead).")
    with Store.open(path) as store:
        assert store.current_release("2026-10").approved_by == "Fran Planner (Regional FP&A lead)"


def test_signed_out_the_sidebar_hides_the_pages_and_the_store_path(controls_store):
    at = open_app()
    assert at.header[0].value == "Sign in"
    assert [r.key for r in at.sidebar.radio] == []
    assert "store_path" not in [t.key for t in at.sidebar.text_input]
    sign_in(at, "lee")
    assert [r.key for r in at.sidebar.radio] == ["page"]
    assert at.sidebar.text_input(key="store_path").value == str(controls_store)


def test_the_ui_fails_closed_when_the_account_cannot_be_read(controls_store, monkeypatch):
    def broken(store):
        raise OSError("the store is locked by another process")

    monkeypatch.setattr(identity, "signed_in_mode", broken)
    at = AppTest.from_file(str(APP), default_timeout=60)
    at.run()
    assert errors(at) == ["Cannot read who is signed in to this store (OSError: the store is locked by another "
                          "process). Nothing is shown until it can be read."]
    assert identity.OPEN_MODE not in [shown(c.value) for c in at.sidebar.caption]
    assert [r.key for r in at.sidebar.radio] == [] and at.header == []
