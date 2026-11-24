"""docs/fixture-expected.md failure cases F1-F6, each against a fresh store."""

from decimal import Decimal as D

import pytest
from conftest import PERIOD, run_story

from finance_close_toolkit import release


def test_f1_wrong_period_is_rejected(close):
    v = close.ingest("sg_actual_wrong_period.csv", "SG", "actual", "2026-11-04T10:00").validation
    assert v.state == "rejected"
    assert {e.code for e in v.exceptions} == {"WRONG_PERIOD"}
    [e] = v.exceptions
    assert '"2026-09"' in e.detail and e.row == 2


def test_f2_duplicate_row_needs_review(close):
    v = close.ingest("sg_actual_duplicate.csv", "SG", "actual", "2026-11-04T10:00").validation
    assert v.state == "needs_review"
    assert [(e.code, e.row, e.account, e.amount) for e in v.exceptions] == [
        ("DUPLICATE_ROW", 7, "6200", D("40000.00")),
        ("DUPLICATE_ROW", 10, "6200", D("40000.00")),
    ]
    assert all('"Marketing" 40,000.00' in e.detail for e in v.exceptions)


def test_f3_renamed_column_is_rejected_with_closest_header(close):
    v = close.ingest("au_actual_drift.xlsx", "AU", "actual", "2026-11-07T15:00").validation
    assert v.state == "rejected"
    [e] = v.exceptions
    assert e.code == "MISSING_COLUMN"
    assert e.detail == 'column "Amount AUD" not in header row; closest header "Amount (AUD)"'


def test_f4_missing_rate_refuses_publish(close):
    fx = close.root / "fx" / f"{PERIOD}.toml"
    fx.write_text(fx.read_text(encoding="utf-8").replace('AUD = "0.86"\n', ""), encoding="utf-8")
    assert close.ws.fx(PERIOD).rate("actual", "AUD") is None
    run_story(close, "E5")
    for approve in (False, True):
        r = close.publish(approve=approve)
        assert r.outcome == "refused"
        assert r.proposal.blockers == ("AU actual: no actual rate for AUD in 2026-10.toml",)
    assert close.store.count("releases") == 0
    assert close.store.count("release_facts") == 0
    assert close.store.current_release(PERIOD) is None


class InjectedFailure(Exception):
    pass


def test_f5_failure_after_facts_rolls_back(close):
    run_story(close, "E6")
    close.ingest("sg_actual_v2.csv", "SG", "actual", "2026-11-14T09:00")
    facts_before = close.store.count("release_facts")
    reached = []

    def fail() -> None:
        reached.append(close.store.count("release_facts"))
        raise InjectedFailure

    with pytest.raises(InjectedFailure):
        close.publish(approve=True, fault=fail)
    assert reached == [facts_before * 2], "fault ran after the new facts were written"
    assert close.store.current_release(PERIOD).release_id == "2026-10-R1"
    assert [r.release_id for r in close.store.releases(PERIOD)] == ["2026-10-R1"]
    assert close.store.count("release_facts") == facts_before
    assert release.verify_release(close.store, "2026-10-R1").ok
    assert close.publish(approve=True).release_id == "2026-10-R2"


def test_f6_republishing_unchanged_facts_is_a_noop(close):
    run_story(close, "E6")
    r = close.publish(approve=True)
    assert (r.outcome, r.release_id) == ("unchanged", "2026-10-R1")
    assert [x.release_id for x in close.store.releases(PERIOD)] == ["2026-10-R1"]


def test_stale_mapping_blocks_publish_until_revalidate(close):
    run_story(close, "E6")
    close.map_consulting_to_oth()
    r = close.publish(approve=True)
    assert r.outcome == "refused"
    assert r.proposal.blockers == (
        "AU actual: au_actual_v1.xlsx was validated under an older mapping; run revalidate",
        "AU budget: au_budget.xlsx was validated under an older mapping; run revalidate",
    )
    assert close.board().slot("AU", "actual").note == "validated under an older mapping; run revalidate"


def test_approving_a_proposal_that_changed_since_review_writes_nothing(close):
    run_story(close, "E11")
    reviewed = close.publish()
    assert reviewed.outcome == "dry_run"
    close.ingest("sg_actual_v2.csv", "SG", "actual", "2026-11-14T09:00")
    for approve in (False, True):
        r = close.publish(approve=approve, expected_checksum=reviewed.facts_checksum)
        assert (r.outcome, r.release_id) == ("refused", None)
        assert r.reasons == ("The proposal changed since you reviewed it; review it again.",)
    assert [x.release_id for x in close.store.releases(PERIOD)] == ["2026-10-R1"]

    again = close.publish()
    assert again.facts_checksum != reviewed.facts_checksum
    r = close.publish(approve=True, expected_checksum=again.facts_checksum)
    assert (r.outcome, r.release_id) == ("published", "2026-10-R2")
    assert close.store.current_release(PERIOD).facts_checksum == again.facts_checksum
