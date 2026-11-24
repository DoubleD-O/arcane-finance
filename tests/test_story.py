"""docs/fixture-expected.md, scenario E1-E16 and below-threshold B1-B2, through the library API."""

import csv
from datetime import date
from decimal import Decimal as D

from conftest import LEAD, PERIOD, run_story

from finance_close_toolkit import release
from finance_close_toolkit.kpi import KPIS, Diff, KpiDelta
from finance_close_toolkit.money import fmt_pct
from finance_close_toolkit.validation import revalidate


def kpi(d: Diff, scenario: str, scope: str, key: str) -> KpiDelta:
    return next(k for k in d.kpis if (k.scenario, k.scope, k.kpi.key) == (scenario, scope, key))


def statuses(board) -> dict[tuple[str, str], str]:
    return {(v.slot.market.code, v.slot.scenario): v.status for v in board.slots}


def assert_au_ebitda_change(d: Diff) -> None:
    au = kpi(d, "actual", "AU", "ebitda")
    assert (au.before, au.after, au.change) == (D("51600"), D("38700"), D("-12900"))
    assert fmt_pct(au.pct) == "-25.00%"
    assert au.material
    apac = kpi(d, "actual", "APAC", "ebitda")
    assert (apac.before, apac.after, apac.change) == (D("221600"), D("208700"), D("-12900"))
    assert fmt_pct(apac.pct) == "-5.82%"
    assert apac.material
    for scope in ("AU", "APAC"):
        for key in ("revenue", "gross_margin"):
            assert kpi(d, "actual", scope, key).change == 0
    [oth] = d.accounts
    assert (oth.scenario, oth.market, oth.account) == ("actual", "AU", "OTH")
    assert (oth.before, oth.after, oth.change) == (D("-8600"), D("-21500"), D("-12900"))


def test_e1_three_files_validate_with_zero_unmapped(close):
    results = [
        close.ingest("sg_actual_v1.csv", "SG", "actual", "2026-11-04T10:00"),
        close.ingest("sg_budget.csv", "SG", "budget", "2026-11-01T09:00"),
        close.ingest("au_budget.xlsx", "AU", "budget", "2026-11-01T09:00"),
    ]
    for r in results:
        v = r.validation
        assert not r.repeat
        assert v.state == "validated"
        assert v.exceptions == ()
        assert v.tie_out.unmapped_total == 0
        assert v.tie_out.source_total == v.tie_out.mapped_total


def test_e2_au_actual_missing_and_overdue(close):
    run_story(close, "E2")
    board = close.board("2026-11-06")
    assert statuses(board) == {
        ("SG", "actual"): "validated", ("SG", "budget"): "validated",
        ("AU", "actual"): "missing", ("AU", "budget"): "validated",
    }
    [s] = board.signals
    assert (s.type, s.market, s.scenario) == ("MISSING_SUBMISSION", "AU", "actual")
    assert s.owner == "AU country controller"
    assert s.due == date(2026, 11, 5)
    assert s.overdue is True


def test_e3_publish_refused_while_au_actual_missing(close):
    run_story(close, "E3")
    r = close.publish()
    assert r.outcome == "refused"
    assert r.proposal.blockers == ("AU actual: no submission received",)
    assert close.store.count("releases") == 0
    assert close.store.current_release(PERIOD) is None


def test_e4_au_actual_v1_validates(close):
    run_story(close, "E3")
    r = close.ingest("au_actual_v1.xlsx", "AU", "actual", "2026-11-07T15:00")
    assert r.validation.state == "validated"
    assert r.validation.exceptions == ()


def test_e5_repeat_ingest_is_a_noop(close):
    run_story(close, "E4")
    before = close.board("2026-11-07T16:00")
    r = close.ingest("au_actual_v1.xlsx", "AU", "actual", "2026-11-07T15:00")
    assert r.repeat is True and r.revalidated is False
    assert close.store.count("submissions") == 4
    assert len(close.store.validations(r.submission.submission_id)) == 1
    after = close.board("2026-11-07T16:00")
    assert statuses(after) == statuses(before)
    assert after.signals == before.signals == ()


def test_e6_publish_r1_with_expected_apac_totals(close):
    run_story(close, "E5")
    r = close.publish(approve=True)
    assert (r.outcome, r.release_id) == ("published", "2026-10-R1")
    rel = close.store.current_release(PERIOD)
    assert rel.release_id == "2026-10-R1" and rel.approved_by == LEAD and rel.previous_release_id is None
    assert rel.kpi_totals["actual"]["APAC"] == {
        "revenue": D("1430000"), "gross_margin": D("756400"), "ebitda": D("221600"),
    }
    assert rel.kpi_totals["budget"]["APAC"] == {
        "revenue": D("1407600"), "gross_margin": D("744400"), "ebitda": D("191000"),
    }
    board = close.board()
    assert set(statuses(board).values()) == {"approved"}
    assert board.signals == ()


def test_e7_au_v2_needs_review_with_unmapped_64000(close):
    run_story(close, "E6")
    v = close.ingest("au_actual_v2.xlsx", "AU", "actual", "2026-11-12T11:00").validation
    assert v.state == "needs_review"
    [e] = v.exceptions
    assert (e.code, e.row, e.account, e.amount) == ("UNMAPPED_ACCOUNT", 9, "64000", D("15000"))
    assert (v.tie_out.source_total, v.tie_out.mapped_total, v.tie_out.unmapped_total) == (
        D("963000"), D("948000"), D("15000"),
    )


def test_e8_validation_failed_signal_and_r1_stays_current(close):
    run_story(close, "E8")
    board = close.board()
    assert board.slot("AU", "actual").status == "needs_review"
    [s] = board.signals
    assert (s.type, s.market, s.scenario, s.owner) == ("VALIDATION_FAILED", "AU", "actual", "AU country controller")
    assert s.impact is None
    assert [e.code for e in s.exceptions] == ["UNMAPPED_ACCOUNT"]
    assert board.current_release_id == "2026-10-R1"


def test_e9_publish_refused_while_au_needs_review(close):
    run_story(close, "E9")
    r = close.publish()
    assert r.outcome == "refused"
    assert r.proposal.blockers == ("AU actual: au_actual_v2.xlsx is needs_review (1 exception)",)
    assert close.store.current_release(PERIOD).release_id == "2026-10-R1"
    assert close.store.count("releases") == 1


def test_e10_mapping_fix_and_revalidate(close):
    run_story(close, "E9")
    old_version = close.ws.mapping("AU").version
    close.map_consulting_to_oth()
    new_version = close.ws.mapping("AU").version
    assert new_version != old_version
    results = {(r.submission.market, r.submission.scenario): r for r in revalidate(close.store, close.ws, PERIOD)}
    au = results[("AU", "actual")]
    assert au.submission.file_name == "au_actual_v2.xlsx"
    assert au.new and au.validation.state == "validated" and au.validation.mapping_version == new_version
    assert not results[("SG", "actual")].new


def test_e11_material_change_signal(close):
    run_story(close, "E11")
    board = close.board()
    assert statuses(board) == {
        ("SG", "actual"): "approved", ("SG", "budget"): "approved",
        ("AU", "actual"): "changed", ("AU", "budget"): "approved",
    }
    [s] = board.signals
    assert (s.type, s.market, s.scenario, s.owner) == ("MATERIAL_CHANGE", "AU", "actual", LEAD)
    assert s.release_id == "2026-10-R1"
    assert_au_ebitda_change(s.impact)


def test_e12_dry_run_shows_delta_and_writes_nothing(close):
    run_story(close, "E12")
    r = close.publish()
    assert (r.outcome, r.release_id) == ("dry_run", "2026-10-R2")
    assert_au_ebitda_change(r.diff)
    assert close.store.count("releases") == 1
    assert close.store.current_release(PERIOD).release_id == "2026-10-R1"


def test_e13_publish_r2_with_v2_and_new_mapping_in_manifest(close):
    run_story(close, "E12")
    r = close.publish(approve=True)
    assert (r.outcome, r.release_id) == ("published", "2026-10-R2")
    rel = close.store.current_release(PERIOD)
    assert rel.release_id == "2026-10-R2" and rel.previous_release_id == "2026-10-R1"
    assert rel.kpi_totals["actual"]["APAC"]["ebitda"] == D("208700")
    au = rel.slot("AU", "actual")
    v2 = close.store.latest_submission("AU", PERIOD, "actual")
    assert (au.file_name, au.submission_id) == ("au_actual_v2.xlsx", v2.submission_id)
    assert au.mapping_version == close.ws.mapping("AU").version
    assert close.store.release("2026-10-R1").slot("AU", "actual").mapping_version != au.mapping_version


def test_e14_r1_still_verifies_and_exports(close, tmp_path):
    run_story(close, "E14")
    assert release.verify_release(close.store, "2026-10-R1").ok
    facts_path, manifest_path = release.export(close.store, "2026-10-R1", tmp_path / "out")
    with open(facts_path, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    ebitda_types = next(k for k in KPIS if k.key == "ebitda").types
    coa = close.ws.coa
    ebitda = sum(
        D(r["reporting_amount"]) for r in rows
        if r["scenario"] == "actual" and coa.type_of(r["account"]) in ebitda_types
    )
    assert ebitda == D("221600")
    files = {r["file_name"] for r in rows if (r["market"], r["scenario"]) == ("AU", "actual")}
    assert files == {"au_actual_v1.xlsx"}
    assert '"au_actual_v1.xlsx"' in manifest_path.read_text(encoding="utf-8")


def test_e15_diff_r1_to_r2(close):
    run_story(close, "E15")
    assert_au_ebitda_change(release.diff_releases(close.store, close.ws, "2026-10-R1", "2026-10-R2"))


def test_e16_trace_au_revenue_in_r1(close):
    run_story(close, "E16")
    [f] = release.trace(close.store, "2026-10-R1", "AU", "REV", "actual")
    assert (f.file_name, f.source_row) == ("au_actual_v1.xlsx", 4)
    assert (f.local_amount, f.currency, f.rate, f.reporting_amount) == (D("500000"), "AUD", D("0.86"), D("430000"))


def test_b1_sg_v2_validates(close):
    run_story(close, "E16")
    r = close.ingest("sg_actual_v2.csv", "SG", "actual", "2026-11-14T09:00")
    assert r.validation.state == "validated"


def test_b2_below_threshold_change_raises_no_signal(close):
    run_story(close, "B2")
    board = close.board()
    view = board.slot("SG", "actual")
    assert view.status == "changed"
    assert board.signals == ()
    sg = kpi(view.change, "actual", "SG", "ebitda")
    assert (sg.before, sg.after, sg.change) == (D("170000"), D("169000"), D("-1000"))
    assert fmt_pct(sg.pct) == "-0.59%" and not sg.material
    apac = kpi(view.change, "actual", "APAC", "ebitda")
    assert (apac.before, apac.after, apac.change) == (D("208700"), D("207700"), D("-1000"))
    assert fmt_pct(apac.pct) == "-0.48%" and not apac.material
    assert not view.change.material
    [mkt] = view.change.accounts
    assert (mkt.market, mkt.account, mkt.before, mkt.after) == ("SG", "MKT", D("-40000"), D("-41000"))


def test_material_change_next_action_names_no_command(close):
    run_story(close, "E10")
    [s] = close.board().signals
    assert s.next_action == "Review the change, then approve it or ask the market to resubmit"
