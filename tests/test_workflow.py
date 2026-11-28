"""The needs-attention workflow (docs/design-attention.md): acknowledge, assign, due dates, reopen."""

from datetime import date, datetime

import pytest
from conftest import LEAD, PERIOD

from finance_close_toolkit import controls, demo, identity, release, workflow
from finance_close_toolkit.identity import Actor
from finance_close_toolkit.validation import revalidate

AU = "AU country controller"
MISSING = workflow.board_key(PERIOD, "MISSING_SUBMISSION", "AU", "actual")
FAILED = workflow.board_key(PERIOD, "VALIDATION_FAILED", "AU", "actual")
MATERIAL = workflow.board_key(PERIOD, "MATERIAL_CHANGE", "AU", "actual")


def at(text: str) -> datetime:
    return datetime.fromisoformat(text)


def items(close, when: str) -> workflow.Items:
    return workflow.load(close.store, close.ws, at(when), PERIOD)


def act(close, key, kind, by, when, **kw) -> workflow.ActionResult:
    return workflow.act(close.store, close.ws, key, kind, by, at(when), **kw)


def e1(close):
    close.ingest("sg_actual_v1.csv", "SG", "actual", "2026-11-04T10:00")
    close.ingest("sg_budget.csv", "SG", "budget", "2026-11-01T09:00")
    close.ingest("au_budget.xlsx", "AU", "budget", "2026-11-01T09:00")


def test_e_story_through_the_workflow(close):
    e1(close)
    # E2: AU actual missing and overdue, acknowledged by the AU controller
    i = items(close, "2026-11-06T00:00").get(MISSING)
    assert (i.state, i.occurrence, i.due, i.overdue, i.assignee) == ("new", 1, date(2026, 11, 5), True, None)
    r = act(close, MISSING, "ack", AU, "2026-11-06T09:00", note="chasing the file")
    assert r.outcome == "recorded"
    i = items(close, "2026-11-06T10:00").get(MISSING)
    assert (i.state, i.acknowledged_by) == ("acknowledged", AU)

    # E4: the file arrives, the item resolves
    close.ingest("au_actual_v1.xlsx", "AU", "actual", "2026-11-07T15:00")
    i = items(close, "2026-11-07T15:00").get(MISSING)
    assert (i.state, i.overdue, i.cleared_at, i.last_actor) == ("resolved", False, at("2026-11-07T15:00"), AU)
    close.publish(approve=True)

    # E7: VALIDATION_FAILED, assigned to the AU controller by the lead
    close.ingest("au_actual_v2.xlsx", "AU", "actual", "2026-11-12T11:00")
    assert items(close, "2026-11-12T12:00").get(FAILED).state == "new"
    assert act(close, FAILED, "assign", LEAD, "2026-11-12T12:05", to=AU).outcome == "recorded"
    assert items(close, "2026-11-12T12:10").get(FAILED).assignee == AU

    # E10: mapping fixed and revalidated: resolved, and the material change appears
    close.map_consulting_to_oth()
    revalidate(close.store, close.ws, PERIOD, at=at("2026-11-12T13:00"))
    now = items(close, "2026-11-12T13:00")
    assert (now.get(FAILED).state, now.get(FAILED).cleared_at) == ("resolved", at("2026-11-12T13:00"))
    assert now.get(MATERIAL).state == "new"

    # E13: approval resolves it, and the manifest records it as open at approval
    r = close.publish(approve=True)
    assert r.release_id == "2026-10-R2"
    assert items(close, "2026-11-20T10:00").get(MATERIAL).state == "resolved"
    [open_at] = close.store.release("2026-10-R2").open_items
    assert (open_at.key, open_at.state) == (MATERIAL, "new")
    assert close.store.release("2026-10-R1").open_items == ()


def test_no_signal_or_figure_changes_and_checksums_stay(close):
    e1(close)
    before = close.board("2026-11-06T00:00").signals
    items(close, "2026-11-06T00:00")
    act(close, MISSING, "ack", AU, "2026-11-06T09:00")
    assert close.board("2026-11-06T00:00").signals == before
    close.ingest("au_actual_v1.xlsx", "AU", "actual", "2026-11-07T15:00")
    r = close.publish(approve=True)
    rel = close.store.release(r.release_id)
    assert rel.facts_checksum == r.facts_checksum
    assert release.verify_release(close.store, r.release_id).ok


def test_repeated_imports_and_reads_create_no_duplicates(close):
    e1(close)
    items(close, "2026-11-06T00:00")
    n = len(close.store.attention_events())
    e1(close)
    items(close, "2026-11-06T00:00")
    items(close, "2026-11-06T01:00")
    assert len(close.store.attention_events()) == n
    assert [i.key for i in items(close, "2026-11-06T02:00").items if i.is_open] == [MISSING]


def test_reopen_is_a_new_occurrence_with_the_old_history(close):
    e1(close)
    close.ingest("au_actual_v1.xlsx", "AU", "actual", "2026-11-07T15:00")
    close.publish(approve=True)
    close.ingest("au_actual_v2.xlsx", "AU", "actual", "2026-11-12T11:00")
    act(close, FAILED, "ack", AU, "2026-11-12T12:00", note="first time")
    close.ingest("au_actual_v1.xlsx", "AU", "actual", "2026-11-12T14:00")  # the good file again
    assert items(close, "2026-11-12T14:00").get(FAILED).state == "resolved"
    close.ingest("au_actual_v2.xlsx", "AU", "actual", "2026-11-12T15:00")  # and the bad one returns
    i = items(close, "2026-11-12T15:00").get(FAILED)
    assert (i.state, i.occurrence) == ("reopened", 2)
    assert [h.occurrence for h in i.history if h.what == "acknowledged: first time"] == [1]
    assert act(close, FAILED, "ack", AU, "2026-11-12T15:05").outcome == "recorded"
    assert items(close, "2026-11-12T15:10").get(FAILED).state == "acknowledged"


def test_new_evidence_lapses_an_acknowledgement(close):
    e1(close)
    close.ingest("au_actual_v1.xlsx", "AU", "actual", "2026-11-07T15:00")
    close.publish(approve=True)
    close.ingest("au_actual_v2.xlsx", "AU", "actual", "2026-11-12T11:00")
    act(close, FAILED, "ack", AU, "2026-11-12T12:00")
    # The same file again is the same evidence: the acknowledgement stands
    close.ingest("au_actual_v2.xlsx", "AU", "actual", "2026-11-12T11:00")
    assert items(close, "2026-11-12T12:30").get(FAILED).state == "acknowledged"
    # A mapping change that leaves the file in review is new evidence on the open item
    with open(close.root / "config" / "mappings" / "au.toml", "a", encoding="utf-8", newline="\n") as f:
        f.write('"99999" = "OTH"\n')
    revalidate(close.store, close.ws, PERIOD)
    i = items(close, "2026-11-12T13:00").get(FAILED)
    assert (i.state, i.occurrence, i.new_evidence, i.acknowledged_by) == ("new", 1, True, None)


def test_overdue_follows_the_clock_and_due_date_changes(close):
    e1(close)
    assert items(close, "2026-11-05T12:00").get(MISSING).overdue is False
    assert items(close, "2026-11-06T00:00").get(MISSING).overdue is True
    assert act(close, MISSING, "due", LEAD, "2026-11-06T09:00", due=date(2026, 11, 9)).outcome == "recorded"
    i = items(close, "2026-11-06T10:00").get(MISSING)
    assert (i.due, i.overdue) == (date(2026, 11, 9), False)
    assert items(close, "2026-11-10T00:00").get(MISSING).overdue is True


def test_injected_store_clock_drives_overdue(close):
    e1(close)
    close.store.clock = lambda: at("2026-11-04T12:00")
    assert workflow.load(close.store, close.ws, period=PERIOD).get(MISSING).overdue is False
    close.store.clock = lambda: at("2026-11-06T12:00")
    assert workflow.load(close.store, close.ws, period=PERIOD).get(MISSING).overdue is True


def test_controls_due_default_is_period_end_plus_working_days():
    assert workflow.working_days_after(workflow.period_end("2026-10"), 5) == date(2026, 11, 6)
    assert workflow.working_days_after(date(2026, 11, 6), 1) == date(2026, 11, 9)


@pytest.mark.parametrize("kind, by, kw, allowed", [
    ("ack", AU, {}, True),
    ("ack", LEAD, {}, True),
    ("ack", "SG finance analyst", {}, False),
    ("assign", LEAD, {"to": AU}, True),
    ("assign", AU, {"to": AU}, False),
    ("due", LEAD, {"due": date(2026, 11, 9)}, True),
    ("due", AU, {"due": date(2026, 11, 9)}, False),
    ("comment", "SG finance analyst", {"note": "seen"}, True),
    ("comment", "", {"note": "seen"}, False),
])
def test_permissions_open_mode(close, kind, by, kw, allowed):
    e1(close)
    r = act(close, MISSING, kind, by, "2026-11-06T09:00", **kw)
    assert (r.outcome == "recorded") is allowed, r.message
    if not allowed:
        assert r.message.endswith("Nothing recorded.")


def test_assignee_hands_over_and_repeats_are_no_ops(close):
    e1(close)
    assert act(close, MISSING, "assign", LEAD, "2026-11-06T09:00", to=AU).outcome == "recorded"
    assert act(close, MISSING, "assign", LEAD, "2026-11-06T09:01", to=AU).outcome == "no_op"
    assert act(close, MISSING, "assign", AU, "2026-11-06T09:02", to=LEAD).outcome == "recorded"
    assert act(close, MISSING, "assign", "SG finance analyst", "2026-11-06T09:03", to=AU).outcome == "refused"
    assert act(close, MISSING, "assign", LEAD, "2026-11-06T09:04", to="SG finance analyst").outcome == "refused"
    assert act(close, MISSING, "ack", AU, "2026-11-06T09:05").outcome == "recorded"
    assert act(close, MISSING, "ack", AU, "2026-11-06T09:06").outcome == "no_op"
    mine = workflow.mine(items(close, "2026-11-06T10:00"), Actor(LEAD))
    assert [i.key for i in mine] == [MISSING]


def test_nothing_resolves_an_item_but_the_data(close):
    e1(close)
    items(close, "2026-11-06T00:00")
    close.ingest("au_actual_v1.xlsx", "AU", "actual", "2026-11-07T15:00")
    items(close, "2026-11-07T15:00")
    r = act(close, MISSING, "ack", AU, "2026-11-07T16:00")
    assert (r.outcome, r.message) == ("refused", f"{MISSING} is resolved: only the data opens it again. "
                                                 "Nothing recorded.")
    r = act(close, MISSING, "comment", AU, "2026-11-07T16:00", note="arrived")
    assert r.outcome == "recorded", r.message
    assert workflow.ACTIONS == ("ack", "assign", "due", "comment")


def _users(close):
    ws = close.ws
    identity.add_user(close.store, ws, None, "admin", "Admin", ["admin"], "a-long-password-1")
    admin = identity.act(close.store.user("admin"))
    for name, roles in (("lead", [LEAD]), ("aucc", [AU]), ("sgfa", ["SG finance analyst"])):
        identity.add_user(close.store, ws, admin, name, name.upper(), roles, "a-long-password-1")
    return {u: identity.act(close.store.user(u)) for u in ("lead", "aucc", "sgfa")}


def test_signed_in_mode_needs_a_person_and_assigns_users(close):
    e1(close)
    people = _users(close)
    close.store.clock = lambda: at("2026-11-27T09:00")
    assert act(close, MISSING, "ack", AU, "2026-11-06T09:00").message.startswith("Sign in first")
    assert act(close, MISSING, "ack", people["sgfa"], "2026-11-06T09:00").outcome == "refused"
    assert act(close, MISSING, "ack", people["aucc"], "2026-11-06T09:00").outcome == "recorded"
    assert act(close, MISSING, "assign", people["lead"], "2026-11-06T09:01", to=AU).outcome == "refused"
    assert act(close, MISSING, "assign", people["lead"], "2026-11-06T09:01", to="aucc").outcome == "recorded"
    i = items(close, "2026-11-06T10:00").get(MISSING)
    assert (i.assignee, i.assignee_label) == ("aucc", "AUCC (aucc)")
    assert [x.key for x in workflow.mine(items(close, "2026-11-06T10:00"), people["aucc"])] == [MISSING]
    assert "AUCC (AU country controller)" in [h.who for h in i.history]
    assert any(h.when.startswith("recorded 2026-11-27 09:00") for h in i.history)
    assert act(close, MISSING, "assign", people["aucc"], "2026-11-06T09:02", to="lead").outcome == "recorded"


def test_controls_items_follow_the_c_story(close):
    store, ws = close.store, close.ws
    for s in demo.CONTROL_STEPS:
        s.action(store, ws, s.at)
        workflow.load(store, ws, s.at)
    lead = ws.config.controls.close_lead
    by = {i.key: i for i in workflow.load(store, ws, at("2026-05-05T11:00")).items}
    variance = by[workflow.controls_key("INVOICE_VARIANCE", "A-001")]
    assert (variance.state, variance.condition.owner, variance.condition.lead) == ("resolved", "SG finance analyst",
                                                                                  lead)
    assert variance.due == date(2026, 4, 7)  # 31 March plus 5 working days
    assert by[workflow.controls_key("NO_OWNER", "A-006")].state == "resolved"
    assert by[workflow.controls_key("AMBIGUOUS_MATCH", "INV-5504")].state == "resolved"
    still_open = [i.key for i in workflow.load(store, ws, at("2026-05-05T11:00")).items if i.is_open]
    c = controls.load(store, ws, at("2026-05-05T11:00"))
    assert sorted(still_open) == sorted(workflow.controls_key(s.type, s.item) for s in c.signals)


# Reads are pure: observations are recorded by the operations that change the data


def _event_rows(store) -> list[tuple]:
    return store._all("SELECT * FROM attention_events ORDER BY seq", [])


def test_as_of_reads_leave_the_event_table_unchanged(close):
    store, ws = close.store, close.ws
    demo.replay_controls(store, ws, "C1")
    key = workflow.controls_key("NO_OWNER", "A-006")
    lead = ws.config.controls.close_lead
    assert workflow.act(store, ws, key, "ack", lead, at("2026-05-04T09:30")).outcome == "recorded"
    before = _event_rows(store)
    workflow.load(store, ws, at("2026-05-01T00:00"))  # before anything was imported
    after = workflow.load(store, ws, at("2026-05-04T09:45")).get(key)
    assert _event_rows(store) == before
    assert (after.state, after.occurrence, after.acknowledged_by) == ("acknowledged", 1, lead)


def test_resolved_at_is_when_the_data_changed_not_the_first_read(close):
    e1(close)
    items(close, "2026-11-06T00:00")
    close.ingest("au_actual_v1.xlsx", "AU", "actual", "2026-11-07T15:00")
    i = items(close, "2026-11-09T08:00").get(MISSING)
    assert (i.state, i.cleared_at) == ("resolved", at("2026-11-07T15:00"))
    close.publish(approve=True)
    close.ingest("au_actual_v2.xlsx", "AU", "actual", "2026-11-12T11:00")
    close.map_consulting_to_oth()
    revalidate(close.store, close.ws, PERIOD, at=at("2026-11-12T13:00"))
    later = items(close, "2026-11-13T08:00")
    assert (later.get(FAILED).state, later.get(FAILED).cleared_at) == ("resolved", at("2026-11-12T13:00"))
    assert [h.when for h in later.get(MATERIAL).history][:1] == ["2026-11-12 13:00"]


def test_a_problem_that_appears_and_clears_between_reads_is_recorded(close):
    e1(close)
    close.ingest("au_actual_v1.xlsx", "AU", "actual", "2026-11-07T15:00")
    close.publish(approve=True)
    items(close, "2026-11-12T09:00")
    close.ingest("au_actual_v2.xlsx", "AU", "actual", "2026-11-12T11:00")
    close.ingest("au_actual_v1.xlsx", "AU", "actual", "2026-11-12T14:00")
    i = items(close, "2026-11-13T09:00").get(FAILED)
    assert (i.state, i.cleared_at) == ("resolved", at("2026-11-12T14:00"))
    assert [h.when for h in i.history] == ["2026-11-12 11:00", "2026-11-12 14:00"]


def test_signed_out_reads_write_nothing(close):
    e1(close)
    _users(close)
    before = _event_rows(close.store)
    workflow.load(close.store, close.ws, at("2026-11-06T00:00"), PERIOD)
    workflow.load(close.store, close.ws, at("2026-11-08T00:00"), PERIOD)
    assert _event_rows(close.store) == before


def test_two_concurrent_observers_do_not_collide(close, tmp_path):
    from finance_close_toolkit.store import Store
    other = Store.open(tmp_path / "store.duckdb")
    try:
        first = workflow.Event("comment", MISSING, at("2026-11-06T09:00"), Actor(AU), {"note": "one"})
        second = workflow.Event("comment", MISSING, at("2026-11-06T09:01"), Actor(LEAD), {"note": "two"})
        logged = close.store._logged

        def interleave():
            close.store._logged = logged
            now = logged()
            other.add_attention_events((second,))  # commits while the first writer's transaction is open
            return now

        close.store._logged = interleave
        close.store.add_attention_events((first,))
        notes = sorted(e.data["note"] for e in close.store.attention_events())
        assert notes == ["one", "two"]
    finally:
        other.close()


def test_a_board_item_that_appears_after_its_due_date_gets_followup_days(close):
    close.ingest("sg_actual_v1.csv", "SG", "actual", "2026-11-10T10:00")  # Tuesday, after the 5 Nov due date
    i = items(close, "2026-11-10T11:00").get(MISSING)
    assert (i.due, i.overdue) == (date(2026, 11, 12), False)  # two working days later
    assert items(close, "2026-11-13T00:00").get(MISSING).overdue is True


def test_followup_days_is_configurable(close):
    text = close.config_path.read_text(encoding="utf-8")
    close.config_path.write_text(text + "\n[attention]\nfollowup_days = 4\n", encoding="utf-8")
    close.ingest("sg_actual_v1.csv", "SG", "actual", "2026-11-10T10:00")
    assert items(close, "2026-11-10T11:00").get(MISSING).due == date(2026, 11, 16)


def test_zero_working_days_on_a_weekend_is_the_next_working_day():
    assert workflow.working_days_after(date(2026, 11, 7), 0) == date(2026, 11, 9)  # Saturday -> Monday
    assert workflow.working_days_after(date(2026, 11, 6), 0) == date(2026, 11, 6)


@pytest.mark.parametrize("change", ["disable", "drop_role"])
def test_an_assignee_no_longer_eligible_counts_as_unassigned(close, change):
    e1(close)
    people = _users(close)
    close.store.clock = lambda: at("2026-11-27T09:00")
    assert act(close, MISSING, "assign", people["lead"], "2026-11-06T09:00", to="aucc").outcome == "recorded"
    admin = identity.act(close.store.user("admin"))
    if change == "disable":
        identity.set_active(close.store, admin, "aucc", False)
    else:
        identity.set_roles(close.store, close.ws, admin, "aucc", ["SG finance analyst"])
    i = items(close, "2026-11-06T10:00").get(MISSING)
    assert i.assignee_label == "AUCC (aucc): assignee no longer eligible"
    assert "assignee AUCC (aucc): assignee no longer eligible" in i.summary()
    assert workflow.mine(items(close, "2026-11-06T10:00"), people["aucc"]) == ()


def test_a_config_only_change_shows_unrecorded_until_refresh_records_it(close):
    e1(close)
    close.ingest("au_actual_v1.xlsx", "AU", "actual", "2026-11-07T15:00")
    close.publish(approve=True)
    close.ingest("sg_actual_v2.csv", "SG", "actual", "2026-11-14T09:00")  # below materiality: no signal
    sg = workflow.board_key(PERIOD, "MATERIAL_CHANGE", "SG", "actual")
    text = close.config_path.read_text(encoding="utf-8")
    close.config_path.write_text(text.replace("abs = 10000", "abs = 1").replace("pct = 5.0", "pct = 0.01"),
                                 encoding="utf-8")
    before = _event_rows(close.store)
    seen = items(close, "2026-11-14T10:00").get(sg)
    assert (seen.state, seen.history[-1].who) == ("new", "(the data, not recorded yet)")
    assert _event_rows(close.store) == before
    close.store.clock = lambda: at("2026-11-14T10:30")
    r = workflow.refresh(close.store, close.ws, LEAD)
    assert r.outcome == "recorded" and sg in r.message
    assert items(close, "2026-11-14T11:00").get(sg).history[-1].when == "2026-11-14 10:30"
    assert workflow.refresh(close.store, close.ws, LEAD).outcome == "no_op"


def test_signed_in_observations_carry_the_person_and_the_real_time(close):
    e1(close)
    people = _users(close)
    close.store.clock = lambda: at("2026-11-27T09:00")
    assert workflow.refresh(close.store, close.ws, AU).message.startswith("Sign in first")
    from finance_close_toolkit import validation
    validation.ingest(close.store, close.ws, close.file("au_actual_v1.xlsx"), "AU", PERIOD, "actual",
                      at("2026-11-07T15:00"), by=people["aucc"])
    [cleared] = [e for e in close.store.attention_events() if e.kind == "cleared" and e.key == MISSING]
    assert (cleared.at, cleared.by.person.username, cleared.logged_at) == (
        at("2026-11-07T15:00"), "aucc", at("2026-11-27T09:00"))
    i = items(close, "2026-11-08T00:00").get(MISSING)
    assert i.history[-1].who == "(the data, from AUCC (AU country controller))"
    assert i.history[-1].when == "recorded 2026-11-27 09:00, effective 2026-11-07 15:00"
