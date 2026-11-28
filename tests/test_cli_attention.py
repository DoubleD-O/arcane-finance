"""fct attention and the workflow line in status, through argument parsing and printing."""

import pytest
from conftest import PERIOD

from finance_close_toolkit.cli import main

KEY = f"board:{PERIOD}:MISSING_SUBMISSION:AU:actual"
AU, LEAD = "AU country controller", "Regional FP&A lead"


@pytest.fixture
def fct(close, capsys, tmp_path, monkeypatch):
    monkeypatch.setenv("FCT_SESSIONS_DIR", str(tmp_path / "sessions"))
    monkeypatch.setenv("FCT_NEW_PASSWORD", "a-long-password-1")
    monkeypatch.setenv("FCT_PASSWORD", "a-long-password-1")
    common = ["--store", str(close.root / "cli.duckdb"), "--config", str(close.config_path)]

    def run(*args: str, code: int = 0) -> str:
        assert main([*args, *common]) == code, args
        return capsys.readouterr().out

    for name, market, scenario, received in (("sg_actual_v1.csv", "SG", "actual", "2026-11-04T10:00"),
                                             ("sg_budget.csv", "SG", "budget", "2026-11-01T09:00"),
                                             ("au_budget.xlsx", "AU", "budget", "2026-11-01T09:00")):
        run("ingest", str(close.file(name)), "--market", market, "--period", PERIOD, "--scenario", scenario,
            "--received-at", received)
    return run


def test_status_adds_the_workflow_line_and_keeps_the_signal(fct):
    out = fct("status", "--period", PERIOD, "--as-of", "2026-11-06")
    assert "1. MISSING_SUBMISSION  AU actual  (overdue)" in out
    assert "   Due:      2026-11-05" in out
    assert f"   Workflow: new; assignee none; due 2026-11-05 (overdue) [{KEY}]" in out


def test_ack_assign_due_comment_list_and_show(fct):
    when = ("--as-of", "2026-11-06T09:00")
    assert "acknowledged by AU country controller" in fct("attention", "ack", KEY, "--by", AU, "--note", "chasing",
                                                          *when)
    assert "assignee AU country controller" in fct("attention", "assign", KEY, "--to", AU, "--by", LEAD, *when)
    assert "due 2026-11-09" in fct("attention", "due", KEY, "--date", "2026-11-09", "--by", LEAD, *when)
    fct("attention", "comment", KEY, "--note", "AU says Friday", "--by", "SG finance analyst", *when)
    out = fct("attention", "list", "--mine", "--by", AU, "--as-of", "2026-11-10")
    assert KEY in out and "2026-11-09 overdue" in out
    assert "none." in fct("attention", "list", "--overdue", "--as-of", "2026-11-08")
    out = fct("attention", "show", KEY, "--as-of", "2026-11-08")
    assert "acknowledged: chasing" in out and "comment: AU says Friday" in out
    assert "Workflow: acknowledged by AU country controller; assignee AU country controller; due 2026-11-09" in \
        fct("status", "--period", PERIOD, "--as-of", "2026-11-08")


def test_refusals_exit_1_and_record_nothing(fct, close):
    out = fct("attention", "due", KEY, "--date", "2026-11-09", "--by", AU, "--as-of", "2026-11-06", code=1)
    assert out.startswith(f"Refused: {AU} may not set the due date of {KEY}; allowed: the close lead")
    assert "Refused: No needs-attention item" in fct("attention", "show", "board:nope", code=1)


def test_signed_in_mode_needs_a_sign_in(fct):
    fct("users", "add", "ada", "--name", "Ada Admin", "--role", "admin")
    assert fct("attention", "ack", KEY, "--by", AU, code=1).startswith("Refused: Sign in first")


def test_refresh_records_and_needs_a_sign_in_when_signed_in(fct):
    assert fct("attention", "refresh").startswith("The recorded items already match the data.")
    fct("users", "add", "ada", "--name", "Ada Admin", "--role", "admin")
    assert fct("attention", "refresh", code=1).startswith("Refused: Sign in first")
