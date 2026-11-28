"""The needs-attention workflow in the UI through AppTest: cards show state, assignee and due date; acknowledge and
assign from the cards; My items."""

from conftest import REPO

from test_app import assert_clean, load, load_controls, shown, texts
from test_app_logins import add_team, go, open_app, sign_in

from finance_close_toolkit import identity, workflow
from finance_close_toolkit.config import load_workspace
from finance_close_toolkit.store import Store

MISSING = workflow.board_key("2026-10", "MISSING_SUBMISSION", "AU", "actual")


def key(item: str) -> str:
    from finance_close_toolkit import app
    return app.item_key(item)


def test_board_card_shows_state_assignee_and_due_and_acknowledges():
    at = load("E2", "Close board")
    body = texts(at)
    assert "- State: New" in body and "- Assignee: none" in body
    at.selectbox(key="board_role").set_value("AU country controller").run()
    at.text_input(key=f"att_note_{key(MISSING)}").set_value("chasing AU")
    at.button(key=f"att_ack_{key(MISSING)}").click().run()
    assert_clean(at)
    assert "- State: Acknowledged by AU country controller" in texts(at)
    assert [b.key for b in at.button if b.key == f"att_ack_{key(MISSING)}"] == []


def test_only_permitted_actions_are_offered():
    at = load("E2", "Close board")
    at.selectbox(key="board_role").set_value("SG finance analyst").run()
    keys = {b.key for b in at.button}
    assert f"att_comment_{key(MISSING)}" in keys
    assert not {f"att_ack_{key(MISSING)}", f"att_assign_{key(MISSING)}", f"att_setdue_{key(MISSING)}"} & keys


def test_assign_from_the_board_then_my_items_lists_it():
    at = load("E2", "Close board")
    at.selectbox(key="board_role").set_value("Regional FP&A lead").run()
    at.selectbox(key=f"att_to_{key(MISSING)}").set_value("AU country controller")
    at.button(key=f"att_assign_{key(MISSING)}").click().run()
    assert_clean(at)
    assert "- Assignee: AU country controller" in texts(at)
    at.sidebar.radio(key="page").set_value("My items").run()
    at.selectbox(key="my_items_role").set_value("AU country controller").run()
    assert_clean(at)
    body = texts(at)
    assert "Assigned to AU country controller: 1 open item, overdue first." in body
    assert "**Missing submission: AU actual (2026-10)** (overdue)" in body


def test_controls_cards_show_the_workflow():
    at = load_controls("C1")
    finance = "\n".join(shown(m.value) for m in at.tabs[0].markdown)
    assert "State: New" in finance and "Item due: 2026-04-07" in finance


def test_signed_in_my_items_shows_what_is_assigned_to_me(tmp_path, monkeypatch):
    path = tmp_path / "s.duckdb"
    config = REPO / "config" / "close.toml"
    ws = load_workspace(config)
    with Store.open(path) as store:
        add_team(store, ws)
        from datetime import datetime
        from finance_close_toolkit import validation
        validation.ingest(store, ws, REPO / "examples" / "2026-10" / "sg_actual_v1.csv", "SG", "2026-10", "actual",
                          datetime(2026, 11, 4, 10), by=identity.act(store.user("sam")))
        fran = identity.act(store.user("fran"))
        assert workflow.act(store, ws, MISSING, "assign", fran, datetime(2026, 11, 6), to="alex").outcome == "recorded"
    monkeypatch.setenv("FCT_STORE", str(path))
    monkeypatch.setenv("FCT_CONFIG", str(config))
    at = open_app()
    sign_in(at, "alex")
    go(at, "My items")
    body = texts(at)
    assert "Assigned to Alex Australia (AU country controller): 1 open item, overdue first." in body
    assert at.button(key=f"att_ack_{key(MISSING)}") is not None
