"""A submission can be received more than once; the slot's newest receipt decides its latest."""

from decimal import Decimal as D

from conftest import PERIOD, run_story

from finance_close_toolkit import release
from finance_close_toolkit.cli import main


def sg_ebitda_change(board):
    change = board.slot("SG", "actual").change
    return next((k.before, k.after) for k in change.kpis if (k.scope, k.kpi.key) == ("SG", "ebitda"))


def test_rereceiving_earlier_bytes_makes_them_latest(close):
    run_story(close, "B1")
    assert close.publish(approve=True).release_id == "2026-10-R3"
    submissions = close.store.count("submissions")

    r = close.ingest("sg_actual_v1.csv", "SG", "actual", "2026-11-16T09:00")

    assert r.repeat and r.rereceived
    assert r.latest.file_name == "sg_actual_v1.csv"
    assert r.previous.file_name == "sg_actual_v2.csv"
    assert close.store.count("submissions") == submissions
    board = close.board()
    assert board.slot("SG", "actual").status == "changed"
    assert sg_ebitda_change(board) == (D("169000"), D("170000"))
    assert close.store.latest_submission("SG", PERIOD, "actual").file_name == "sg_actual_v1.csv"


def test_rereceiving_the_latest_bytes_records_nothing(close):
    run_story(close, "E4")
    receipts = close.store.count("receipts")
    r = close.ingest("au_actual_v1.xlsx", "AU", "actual", "2026-11-09T15:00")
    assert r.repeat and not r.rereceived
    assert close.store.count("receipts") == receipts
    assert close.store.latest_submission("AU", PERIOD, "actual").received_at.isoformat() == "2026-11-07T15:00:00"


def test_ingest_older_than_latest_is_not_latest(close):
    run_story(close, "B1")
    r = close.ingest("sg_actual_duplicate.csv", "SG", "actual", "2026-11-10T09:00")
    assert r.latest.file_name == "sg_actual_v2.csv"
    assert r.latest.submission_id != r.submission.submission_id


def test_same_bytes_in_another_slot_are_their_own_submission(close):
    run_story(close, "E1")
    close.ingest("au_actual_v1.xlsx", "AU", "actual", "2026-11-07T15:00")
    sg_budget = close.store.latest_submission("SG", PERIOD, "budget")

    r = close.ingest("sg_budget.csv", "SG", "actual", "2026-11-08T09:00")

    assert not r.repeat
    assert r.submission.submission_id != sg_budget.submission_id
    assert close.store.latest_submission("SG", PERIOD, "budget") == sg_budget
    published = close.publish(approve=True)
    assert published.outcome == "published"
    assert release.verify_release(close.store, published.release_id).ok
    by_scenario = {f.scenario: f.submission_id for f in close.store.release_facts(published.release_id)
                   if f.market == "SG"}
    assert by_scenario == {"actual": r.submission.submission_id, "budget": sg_budget.submission_id}


def cli(close, capsys, *args: str) -> tuple[int, str, str]:
    code = main([*args, "--store", str(close.root / "cli.duckdb"), "--config", str(close.config_path)])
    out = capsys.readouterr()
    return code, out.out, out.err


def test_cli_reports_rereceipt_and_not_latest(close, capsys):
    def ingest(name: str, received: str) -> str:
        code, out, _ = cli(close, capsys, "ingest", str(close.file(name)), "--market", "SG", "--period", PERIOD,
                           "--scenario", "actual", "--received-at", received)
        assert code == 0
        return out

    ingest("sg_actual_v1.csv", "2026-11-04T10:00")
    ingest("sg_actual_v2.csv", "2026-11-14T09:00")
    out = ingest("sg_actual_v1.csv", "2026-11-16T09:00")
    assert "re-received; now the latest for SG actual 2026-10, replacing sg_actual_v2.csv" in out
    out = ingest("sg_actual_v2.csv", "2026-11-15T09:00")
    assert "NOT the latest for SG actual 2026-10: sg_actual_v1.csv (received 2026-11-16 09:00) is" in out


def test_malformed_period_is_a_clean_error_for_every_command(close, capsys):
    for command in ("status", "publish", "revalidate"):
        code, _, err = cli(close, capsys, command, "--period", "2026-x")
        assert code == 2, command
        assert "period must look like 2026-10, got '2026-x'" in err, command
