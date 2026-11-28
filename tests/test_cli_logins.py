"""`fct users`, `fct login|logout|whoami`, `--as` and `fct approvals` (docs/design-logins.md)."""

import os
import re
import subprocess
import sys
import stat
from datetime import datetime
from pathlib import Path

import pytest
from conftest import REPO

from finance_close_toolkit import cli, controls, identity
from finance_close_toolkit.config import load_workspace
from finance_close_toolkit.store import Store

CONFIG = REPO / "config" / "close.toml"
EX = REPO / "examples" / "controls"
PASSWORD = "correct horse battery"
LEAD = "Regional close lead"


@pytest.fixture
def sessions(tmp_path, monkeypatch):
    path = tmp_path / "sessions"
    monkeypatch.setenv("FCT_SESSIONS_DIR", str(path))
    monkeypatch.setenv("FCT_PASSWORD", PASSWORD)
    monkeypatch.setenv("FCT_NEW_PASSWORD", PASSWORD)
    return path


@pytest.fixture
def run(capsys, tmp_path, sessions):
    store = tmp_path / "s.duckdb"

    def fct(*args: str, code: int = 0) -> str:
        got = cli.main([*args, "--store", str(store), "--config", str(CONFIG)])
        out = capsys.readouterr()
        assert got == code, (args, out.out, out.err)
        return out.out + out.err

    fct.store = store
    return fct


def team(run) -> None:
    run("users", "add", "ada", "--name", "Ada Admin", "--role", "admin")
    run("login", "ada")
    run("users", "add", "cam", "--name", "Cam Accounts", "--role", "Accounting team")
    run("users", "add", "lee", "--name", "Lee Lead", "--role", LEAD, "--role", "Accounting team")
    run("users", "add", "pat", "--name", "Pat Payables", "--role", "AP team")


def test_open_mode_says_so_and_login_refuses(run):
    assert run("whoami").strip() == identity.OPEN_MODE
    assert identity.OPEN_MODE in run("login", "ada", code=1)


def test_first_user_switches_on_signed_in_mode_and_actions_need_sign_in(run):
    out = run("users", "add", "ada", "--name", "Ada Admin", "--role", "admin")
    assert "Signed-in mode is on" in out
    out = run("controls", "import", "--accruals", str(EX / "accruals_2026-03.csv"), code=1)
    assert out.strip() == f"Refused: {identity.SIGN_IN_FIRST}"
    assert run("whoami", code=1).startswith("Not signed in.")


def test_login_whoami_logout_and_no_secret_is_printed(run, sessions):
    team(run)
    out = run("login", "cam")
    assert out.startswith("Signed in as Cam Accounts (cam); roles: Accounting team.")
    [file] = list(sessions.iterdir())
    token = file.read_text()
    assert PASSWORD not in out and token not in out
    with Store.open(run.store) as s:
        assert s.session(token) is None and s.user_count() == 4
    if os.name == "posix":
        assert stat.S_IMODE(file.stat().st_mode) == 0o600 and stat.S_IMODE(sessions.stat().st_mode) == 0o700
    assert run("whoami").startswith("Cam Accounts (cam); roles: Accounting team; session until")
    assert run("logout").strip() == "Signed out."
    assert list(sessions.iterdir()) == []
    assert run("whoami", code=1).startswith("Not signed in.")


def test_wrong_password_is_refused(run, monkeypatch):
    team(run)
    monkeypatch.setenv("FCT_PASSWORD", "not the password")
    assert run("login", "cam", code=1).strip() == "Refused: Wrong username or password."


def test_users_need_an_admin_and_the_last_admin_stays(run):
    team(run)
    run("login", "cam")
    assert "Only an admin manages users" in run("users", "add", "eve", "--name", "Eve", "--role", "AP team", code=1)
    run("login", "ada")
    assert "last active admin" in run("users", "disable", "ada", code=1)
    run("users", "disable", "pat")
    listing = run("users", "list")
    assert "pat       Pat Payables" in listing and " no " in listing
    run("users", "set-roles", "pat", "--role", "AP team", "--role", "Accounting team")
    assert "pat now holds: AP team, Accounting team." in run("users", "set-roles", "pat", "--role", "AP team",
                                                             "--role", "Accounting team")


def test_as_picks_one_of_your_roles(run):
    team(run)
    run("login", "lee")
    run("controls", "import", "--accruals", str(EX / "accruals_2026-03.csv"), "--invoices",
        str(EX / "invoices_2026-04.csv"), "--gl", str(EX / "gl_2026-04.csv"), "--as", LEAD,
        "--as-of", "2026-05-04T09:00")
    assert "holds several roles" in run("controls", "assign", "A-006", "--owner", "SG finance analyst", code=1)
    assert "does not hold the role 'AP team'" in run("controls", "link", "INV-5504", "A-004", "--as", "AP team",
                                                      code=1)
    assert run("controls", "assign", "A-006", "--owner", "SG finance analyst", "--as", LEAD,
               "--as-of", "2026-05-04T12:00").strip() == "Assigned A-006 to SG finance analyst."


def test_a_resolution_goes_through_approvals_and_approved_by_is_refused(run):
    team(run)
    run("login", "cam")
    run("controls", "import", "--accruals", str(EX / "accruals_2026-03.csv"), "--invoices",
        str(EX / "invoices_2026-04.csv"), "--gl", str(EX / "gl_2026-04.csv"), "--as-of", "2026-05-04T09:00")
    resolve = ("controls", "resolve", "A-002", "--kind", "write_back", "--amount", "40000", "--reason",
               "Reversal posted manually as JE-0499", "--as-of", "2026-05-04T13:00")
    out = run(*resolve, "--approved-by", LEAD, code=1)
    assert out.strip() == f"Refused: {identity.OWN_SIGN_IN} Nothing recorded."
    assert run(*resolve).startswith("Request 1: Resolution of A-002 missing_reversal: write_back SGD 40,000.00.")
    assert "Refused: You requested 1" in run("approvals", "approve", "1", code=1)
    run("login", "lee")
    listing = run("approvals", "list")
    assert listing.startswith("Request 1: requested by Cam Accounts (Accounting team) (recorded ")
    assert ", effective 2026-05-04 13:00); waiting" in listing
    assert "  Reason: Reversal posted manually as JE-0499" in listing
    assert "holds several roles" in run("approvals", "approve", "1", code=1)
    assert run("approvals", "approve", "1", "--as", LEAD, "--as-of", "2026-05-04T13:30").strip() == (
        "Resolved A-002: write_back 40,000.00, approved by Lee Lead (Regional close lead).")
    assert run("approvals", "list").strip() == "Nothing waits for approval."
    assert re.search(r"; Approved by Lee Lead \(Regional close lead\) \(recorded [-0-9: ]+, effective 2026-05-04 "
                     r"13:30\)$", run("approvals", "list", "--all").splitlines()[0])
    show = run("controls", "show", "A-002", "--as-of", "2026-05-04T14:00")
    assert "Cam Accounts (Accounting team)" in show and "approved by Lee Lead (Regional close lead)" in show


def test_reject_needs_a_reason_and_is_listed(run):
    team(run)
    run("login", "cam")
    run("controls", "import", "--accruals", str(EX / "accruals_2026-03.csv"), "--invoices",
        str(EX / "invoices_2026-04.csv"), "--gl", str(EX / "gl_2026-04.csv"), "--as-of", "2026-05-04T09:00")
    run("controls", "resolve", "A-002", "--kind", "write_back", "--amount", "40000", "--reason", "manual JE",
        "--as-of", "2026-05-04T13:00")
    run("login", "lee")
    assert run("approvals", "reject", "1", "--as", LEAD, "--reason", "Wait for the extract").startswith(
        "Rejected request 1")
    assert "Rejected by Lee Lead (Regional close lead): Wait for the extract" in run("approvals", "list", "--all")


def test_publish_signed_in_records_the_person(run, tmp_path):
    run("users", "add", "ada", "--name", "Ada Admin", "--role", "admin")
    run("login", "ada")
    run("users", "add", "fran", "--name", "Fran Planner", "--role", "Regional FP&A lead")
    run("users", "add", "sam", "--name", "Sam Singapore", "--role", "SG country controller")
    run("login", "sam")
    ex = REPO / "examples" / "2026-10"
    for name, market, scenario in (("sg_actual_v1.csv", "SG", "actual"), ("sg_budget.csv", "SG", "budget"),
                                   ("au_budget.xlsx", "AU", "budget"), ("au_actual_v1.xlsx", "AU", "actual")):
        run("ingest", str(ex / name), "--market", market, "--period", "2026-10", "--scenario", scenario)
    assert "fran" not in run("publish", "--period", "2026-10")
    assert "--approve --expect" in run("publish", "--period", "2026-10")
    assert "Publishing needs someone acting as Regional FP&A lead" in run("publish", "--period", "2026-10",
                                                                           "--approve", code=1)
    run("login", "fran")
    assert "approved by Fran Planner (Regional FP&A lead)." in run("publish", "--period", "2026-10", "--approve")


def test_remove_all_needs_confirmation(run):
    team(run)
    assert "--yes" in run("users", "remove-all", code=1)
    assert run("users", "remove-all", "--yes").startswith("Removed 4 users.")
    assert run("whoami").strip() == identity.OPEN_MODE


def test_history_shows_the_recorded_clock_beside_a_claimed_time(run, monkeypatch):
    monkeypatch.setattr(identity, "clock", lambda: datetime(2026, 11, 27, 9, 0))
    team(run)
    run("login", "lee")
    run("controls", "import", "--accruals", str(EX / "accruals_2026-03.csv"), "--invoices",
        str(EX / "invoices_2026-04.csv"), "--gl", str(EX / "gl_2026-04.csv"), "--as", LEAD,
        "--as-of", "2026-05-04T09:00")
    run("controls", "assign", "A-006", "--owner", "SG finance analyst", "--as", LEAD, "--as-of", "2026-05-04T12:00")
    show = run("controls", "show", "A-006")
    assert "recorded 2026-11-27 09:00, effective 2026-05-04 12:00" in show
    assert "recorded 2026-11-27 09:00, effective 2026-05-04 09:00" in show


def test_a_corrupt_token_file_reads_as_signed_out_and_login_logout_still_work(run, sessions):
    team(run)
    [file] = list(sessions.iterdir())
    file.write_bytes(b"\xff\xfe\x00 not a token")
    assert run("whoami", code=1).startswith("Not signed in.")
    run("login", "cam")
    assert run("whoami").startswith("Cam Accounts (cam)")
    file.write_bytes(b"\xff\xfe")
    assert run("logout").strip() == "Not signed in."
    assert list(sessions.iterdir()) == []


def test_changing_your_password_signs_you_out(run):
    team(run)
    run("login", "cam")
    assert "Sign in again" in run("users", "passwd")
    assert run("whoami", code=1).startswith("Not signed in.")


def test_controls_import_prints_on_a_cp1252_console(tmp_path):
    store = tmp_path / "s.duckdb"
    changed = tmp_path / "accruals_v2.csv"
    changed.write_bytes((EX / "accruals_2026-03.csv").read_bytes().replace(b"100000.00", b"90000.00"))
    env = {**os.environ, "PYTHONIOENCODING": "cp1252", "PYTHONPATH": str(REPO / "src"),
           "FCT_SESSIONS_DIR": str(tmp_path / "sessions")}

    def fct(*args: str) -> subprocess.CompletedProcess:
        return subprocess.run([sys.executable, "-m", "finance_close_toolkit.cli", *args, "--store", str(store),
                               "--config", str(CONFIG)], env=env, capture_output=True, timeout=120)

    assert fct("controls", "import", "--accruals", str(EX / "accruals_2026-03.csv")).returncode == 0
    done = fct("controls", "import", "--accruals", str(changed))
    assert (done.returncode, done.stderr) == (0, b"")
    assert b"Changed: A-001 amount 100,000.00 -> 90,000.00" in done.stdout
