"""Logins and two-person approvals (docs/design-logins.md): users, sessions, lockout, and the stories replayed in
signed-in mode with real people."""

from dataclasses import replace
from datetime import datetime, timedelta
from decimal import Decimal

import pytest
from conftest import APPROVED_AT, PERIOD, REPO, copy_inputs

from finance_close_toolkit import attention, controls, demo, identity, release, validation
from finance_close_toolkit.config import load_workspace
from finance_close_toolkit.identity import Actor, IdentityError
from finance_close_toolkit.store import Store

CONFIG = REPO / "config" / "close.toml"
EX = REPO / "examples" / "controls"
T0 = datetime(2026, 5, 4, 8, 0)
LATER = datetime(2026, 6, 1)
PASSWORD = "correct horse battery"
AP, ANALYST, ACCOUNTING, LEAD = "AP team", "SG finance analyst", "Accounting team", "Regional close lead"

# username, name, roles
PEOPLE = (
    ("ada", "Ada Admin", ("admin",)),
    ("pat", "Pat Payables", (AP,)),
    ("ana", "Ana Analyst", (ANALYST,)),
    ("cam", "Cam Accounts", (ACCOUNTING,)),
    ("lee", "Lee Lead", (LEAD,)),
    ("dual", "Dee Dual", (ACCOUNTING, LEAD)),
    ("sam", "Sam Singapore", ("SG country controller",)),
    ("alex", "Alex Australia", ("AU country controller",)),
    ("fran", "Fran Planner", ("Regional FP&A lead",)),
)


@pytest.fixture
def ws():
    return load_workspace(CONFIG)


@pytest.fixture
def store(tmp_path):
    s = Store.open(tmp_path / "logins.duckdb")
    yield s
    s.close()


def admin_store(store, ws) -> Actor:
    user = identity.add_user(store, ws, None, "ada", "Ada Admin", ["admin"], PASSWORD, T0)
    return identity.act(user, "admin")


def people(store, ws) -> dict[str, Actor]:
    """Every PEOPLE user, signed in; keyed by username, each acting in their first role."""
    admin = admin_store(store, ws)
    for username, name, roles in PEOPLE[1:]:
        identity.add_user(store, ws, admin, username, name, roles, PASSWORD, T0)
    return {u: identity.act(identity.sign_in(store, u, PASSWORD, T0), roles[0]) for u, _, roles in PEOPLE}


def files(**names: str) -> dict:
    return {kind: (name, (EX / name).read_bytes()) for kind, name in names.items()}


# Users and signing in


def test_open_mode_until_the_first_user_who_must_be_an_admin(store, ws):
    assert not identity.signed_in_mode(store)
    with pytest.raises(IdentityError, match="first user must include the admin role"):
        identity.add_user(store, ws, None, "pat", "Pat", [AP], PASSWORD, T0)
    admin = admin_store(store, ws)
    assert identity.signed_in_mode(store)
    assert admin.label == "Ada Admin (admin)"
    with pytest.raises(IdentityError, match="Sign in first"):
        identity.add_user(store, ws, None, "pat", "Pat", [AP], PASSWORD, T0)
    with pytest.raises(IdentityError, match="Unknown role 'Janitor'"):
        identity.add_user(store, ws, admin, "pat", "Pat", ["Janitor"], PASSWORD, T0)
    with pytest.raises(IdentityError, match="at least 12 characters"):
        identity.add_user(store, ws, admin, "pat", "Pat", [AP], "short", T0)


def test_only_an_admin_acting_as_admin_manages_users(store, ws):
    p = people(store, ws)
    with pytest.raises(IdentityError, match="Only an admin manages users"):
        identity.add_user(store, ws, p["pat"], "eve", "Eve", [AP], PASSWORD, T0)
    with pytest.raises(IdentityError, match="Only an admin"):
        identity.set_active(store, p["lee"], "pat", False, T0)


def test_passwords_are_stored_as_salted_scrypt_and_never_in_clear(store, ws):
    admin_store(store, ws)
    stored = store.credentials("ada").password_hash
    assert stored.startswith("scrypt$16384$8$1$") and PASSWORD not in stored
    assert identity.password_matches(PASSWORD, stored) and not identity.password_matches("wrong password!", stored)
    assert identity.hash_password(PASSWORD) != identity.hash_password(PASSWORD)


def test_wrong_password_and_unknown_user_give_the_same_refusal(store, ws):
    admin_store(store, ws)
    for username in ("ada", "nobody"):
        with pytest.raises(IdentityError, match=r"^Wrong username or password\.$"):
            identity.sign_in(store, username, "not the password", T0)


def test_five_wrong_passwords_lock_the_account_for_fifteen_minutes(store, ws):
    admin_store(store, ws)
    for _ in range(4):
        with pytest.raises(IdentityError, match=r"^Wrong username or password\.$"):
            identity.sign_in(store, "ada", "not the password", T0)
    with pytest.raises(IdentityError, match="now locked for 15 minutes"):
        identity.sign_in(store, "ada", "not the password", T0)
    with pytest.raises(IdentityError, match="locked after 5 wrong passwords until 2026-05-04 08:15"):
        identity.sign_in(store, "ada", PASSWORD, T0 + timedelta(minutes=14, seconds=59))
    assert identity.sign_in(store, "ada", PASSWORD, T0 + timedelta(minutes=15)).username == "ada"
    assert store.credentials("ada").failed_sign_ins == 0


def test_an_admin_reset_lifts_a_lockout(store, ws):
    p = people(store, ws)
    for _ in range(5):
        with pytest.raises(IdentityError):
            identity.sign_in(store, "pat", "not the password", T0)
    identity.reset_password(store, p["ada"], "pat", "a brand new password", T0)
    assert identity.sign_in(store, "pat", "a brand new password", T0).username == "pat"


def test_a_role_not_held_and_a_disabled_user_are_refused(store, ws):
    p = people(store, ws)
    pat = store.user("pat")
    with pytest.raises(IdentityError, match="does not hold the role 'Regional close lead'"):
        identity.act(pat, LEAD)
    with pytest.raises(IdentityError, match="holds several roles"):
        identity.act(store.user("dual"))
    token = identity.start_session(store, pat, T0)
    identity.set_active(store, p["ada"], "pat", False, T0)
    with pytest.raises(IdentityError, match="pat is disabled"):
        identity.sign_in(store, "pat", PASSWORD, T0)
    assert identity.session(store, token, T0) is None
    with pytest.raises(IdentityError, match="disabled"):
        identity.act(store.user("pat"), AP)


def test_a_session_lasts_twelve_hours_and_only_its_hash_is_stored(store, ws):
    admin_store(store, ws)
    token = identity.start_session(store, store.user("ada"), T0)
    assert len(bytes.fromhex(token)) == 32
    assert store.session(token) is None
    assert identity.session(store, token, T0 + timedelta(hours=11, minutes=59)).user.username == "ada"
    assert identity.session(store, token, T0 + timedelta(hours=12)) is None
    identity.end_session(store, token)
    assert identity.session(store, token, T0) is None


def test_the_last_active_admin_cannot_be_disabled_or_lose_admin(store, ws):
    admin = admin_store(store, ws)
    with pytest.raises(IdentityError, match="last active admin"):
        identity.set_active(store, admin, "ada", False, T0)
    with pytest.raises(IdentityError, match="last active admin"):
        identity.set_roles(store, ws, admin, "ada", [LEAD], T0)
    identity.add_user(store, ws, admin, "bo", "Bo Backup", ["admin"], PASSWORD, T0)
    identity.set_roles(store, ws, admin, "ada", [LEAD], T0)
    assert store.user("ada").roles == (LEAD,)
    assert [e[0] for e in store.user_events()] == ["add", "add", "set_roles"]


def test_removing_every_user_returns_to_open_mode(store, ws):
    admin = admin_store(store, ws)
    assert identity.remove_all_users(store, admin, T0) == 1
    assert not identity.signed_in_mode(store)


def test_change_password_needs_the_current_one(store, ws):
    admin_store(store, ws)
    with pytest.raises(IdentityError, match="current password is wrong"):
        identity.change_password(store, "ada", "not the password", "another long password", T0)
    identity.change_password(store, "ada", PASSWORD, "another long password", T0)
    assert identity.sign_in(store, "ada", "another long password", T0).username == "ada"


# Every action needs a signed-in user


def test_actions_without_sign_in_are_refused_in_signed_in_mode(store, ws, tmp_path):
    admin_store(store, ws)
    r = controls.link(store, ws, "INV-5504", "A-004", AP, now=T0)
    assert (r.outcome, r.message) == ("refused", identity.SIGN_IN_FIRST + " Nothing recorded.")
    with pytest.raises(controls.ControlsError, match="Sign in first"):
        controls.import_files(store, ws, files(accruals="accruals_2026-03.csv"), T0)
    with pytest.raises(IdentityError, match="Sign in first"):
        validation.ingest(store, ws, REPO / "examples" / PERIOD / "sg_budget.csv", "SG", PERIOD, "budget", T0)
    r = release.publish(store, ws, PERIOD, approved_by="Regional FP&A lead")
    assert (r.outcome, r.reasons) == ("refused", (identity.SIGN_IN_FIRST,))
    assert store.count("control_events") == store.count("submissions") == store.count("releases") == 0


def test_publish_needs_someone_acting_as_the_regional_owner(store, ws):
    p = people(store, ws)
    r = release.publish(store, ws, PERIOD, approved_by=p["lee"])
    assert r.reasons == ("Publishing needs someone acting as Regional FP&A lead; Lee Lead (Regional close lead) "
                         "is not.",)


# Two-person approvals


@pytest.fixture
def c1(store, ws):
    """Signed in, the C1 files imported by AP."""
    p = people(store, ws)
    controls.import_files(store, ws, files(accruals="accruals_2026-03.csv", invoices="invoices_2026-04.csv",
                                           gl="gl_2026-04.csv"), T0, by=p["pat"])
    return p


WRITE_BACK = dict(kind="write_back", amount=Decimal(40000), reason="Reversal posted manually as JE-0499")


def test_approved_by_is_refused_in_signed_in_mode(store, ws, c1):
    r = controls.resolve(store, ws, "A-002", by=c1["cam"], approved_by=LEAD, now=T0, **WRITE_BACK)
    assert (r.outcome, r.message) == ("refused", identity.OWN_SIGN_IN + " Nothing recorded.")


def test_a_resolution_is_requested_then_approved_by_another_person(store, ws, c1):
    r = controls.resolve(store, ws, "A-002", by=c1["cam"], now=T0, **WRITE_BACK)
    assert r.outcome == "requested" and r.message.startswith("Request 1: Resolution of A-002 missing_reversal: "
                                                             "write_back SGD 40,000.00.")
    assert controls.load(store, ws, LATER).accrual("A-002").overall == "open"
    [request] = controls.pending_requests(store)
    assert (request.requester, request.owner) == (c1["cam"], ANALYST)
    assert "Reason: Reversal posted manually as JE-0499" in request.summary
    again = controls.resolve(store, ws, "A-002", by=c1["cam"], now=T0, **WRITE_BACK)
    assert again.outcome == "no_op" and len(controls.pending_requests(store)) == 1

    done = controls.approve(store, ws, 1, c1["lee"], T0 + timedelta(minutes=5))
    assert (done.outcome, done.message) == (
        "recorded", "Resolved A-002: write_back 40,000.00, approved by Lee Lead (Regional close lead).")
    c = controls.load(store, ws, LATER)
    assert c.accrual("A-002").overall == "resolved" and controls.pending_requests(store) == ()
    [(_, decision)] = controls.approvals(store)
    assert (decision.approved, decision.approver, decision.requester) == (True, c1["lee"], c1["cam"].person)
    [h] = [h for h in controls.history(c, "A-002") if h.what.startswith("Resolved")]
    assert h.by == "Cam Accounts (Accounting team)"
    assert "approved by Lee Lead (Regional close lead)" in h.detail
    late = controls.approve(store, ws, 1, identity.act(store.user("dual"), LEAD), T0)
    assert late.message.startswith("Request 1 was already approved by Lee Lead")


def test_the_requester_cannot_approve_even_in_another_role(store, ws, c1):
    dual_accounting = identity.act(store.user("dual"), ACCOUNTING)
    assert controls.resolve(store, ws, "A-002", by=dual_accounting, now=T0, **WRITE_BACK).outcome == "requested"
    for role in (ACCOUNTING, LEAD):
        r = controls.approve(store, ws, 1, identity.act(store.user("dual"), role), T0)
        assert (r.outcome, r.message) == (
            "refused", "You requested 1; another person must decide it from their own sign-in. Nothing recorded.")
    assert controls.approve(store, ws, 1, c1["lee"], T0).outcome == "recorded"


def test_an_approver_acting_in_the_owners_role_is_refused(store, ws, c1):
    assert controls.assign(store, ws, "A-002", LEAD, c1["lee"], T0).outcome == "recorded"
    controls.resolve(store, ws, "A-002", by=c1["cam"], now=T0, **WRITE_BACK)
    r = controls.approve(store, ws, 1, c1["lee"], T0)
    assert (r.outcome, r.message) == (
        "refused", "The approver must differ from the accrual's owner (Regional close lead). Nothing recorded.")
    assert controls.pending_requests(store) != ()


def test_a_rejection_is_recorded_with_both_people_and_changes_nothing(store, ws, c1):
    controls.resolve(store, ws, "A-002", by=c1["cam"], now=T0, **WRITE_BACK)
    assert controls.reject(store, ws, 1, c1["lee"], "", T0).outcome == "refused"
    r = controls.reject(store, ws, 1, c1["lee"], "Extract arrives tomorrow", T0)
    assert (r.outcome, r.message) == ("recorded", "Rejected request 1: Resolution of A-002 missing_reversal: "
                                                  "write_back SGD 40,000.00.")
    [(request, decision)] = controls.approvals(store)
    assert (decision.approved, decision.approver.label, decision.requester.display_name, decision.reason) == (
        False, "Lee Lead (Regional close lead)", "Cam Accounts", "Extract arrives tomorrow")
    assert controls.decision_text(decision) == "Rejected by Lee Lead (Regional close lead): Extract arrives tomorrow"
    assert store.count("control_events") == 0
    assert controls.approve(store, ws, 1, identity.act(store.user("dual"), LEAD), T0).outcome == "refused"


def test_a_decision_always_names_two_different_people(c1):
    with pytest.raises(ValueError, match="two different people"):
        controls.Decision(1, True, c1["cam"], c1["cam"].person, T0)
    with pytest.raises(ValueError, match="two different people"):
        controls.Decision(1, True, Actor(LEAD), c1["cam"].person, T0)


def test_an_override_link_is_requested_and_approved(store, ws, c1):
    r = controls.link(store, ws, "INV-5504", "A-001", c1["pat"], override=True, reason="Same supplier group",
                      now=T0)
    assert r.outcome == "requested"
    [request] = controls.pending_requests(store)
    assert request.summary[0] == "Override link of INV-5504 to A-001"
    done = controls.approve(store, ws, request.request_id, c1["cam"], T0)
    assert (done.outcome, done.message) == ("recorded", "Linked INV-5504 to A-001 (override).")
    [event] = store.control_events()
    assert (event.by_label, event.data["approved_by"], event.data["approved_by_name"], event.data["request"]) == (
        "Pat Payables (AP team)", ACCOUNTING, "Cam Accounts", "1")


def test_a_large_explanation_is_requested_and_approved(store, ws, c1):
    strict = replace(ws, config=replace(ws.config, controls=replace(ws.config.controls,
                                                                    explain_approval_abs=Decimal(0))))
    r = controls.explain(store, strict, "A-001", "Scope extension approved by SG GM", c1["ana"], now=T0)
    assert r.outcome == "requested"
    done = controls.approve(store, strict, 1, c1["lee"], T0)
    assert done.message.endswith("approved by Lee Lead (Regional close lead).")
    assert controls.load(store, strict, LATER).accrual("A-001").explained is not None


# The stories in signed-in mode give the same states and figures as in open mode


def board_view(store, ws, step: demo.Step) -> tuple:
    b = attention.board(store, ws, demo.PERIOD, step.as_of)
    return (b.current_release_id, tuple((v.slot.market.code, v.slot.scenario, v.status, v.note) for v in b.slots),
            tuple((s.type, s.market, s.scenario, s.owner, s.trigger) for s in b.signals))


def test_the_october_story_signed_in_matches_open_mode(tmp_path, ws):
    """Each step applied to an open-mode store and to a signed-in one; after each the close boards match."""
    contexts = []
    for mode in ("open", "signed_in"):
        root = tmp_path / mode
        copy_inputs(root)
        contexts.append(demo.Context(Store.open(root / "store.duckdb"), root, root / "examples", APPROVED_AT))
    open_ctx, signed_ctx = contexts
    p = people(signed_ctx.store, ws)
    signed_ctx = replace(signed_ctx, actors={"SG country controller": p["sam"], "AU country controller": p["alex"],
                                             "Regional FP&A lead": p["fran"]})
    try:
        for s in demo.STEPS:
            if s.action is not None:
                s.action(open_ctx)
                s.action(signed_ctx)
            assert (board_view(signed_ctx.store, load_workspace(signed_ctx.config_path), s)
                    == board_view(open_ctx.store, load_workspace(open_ctx.config_path), s)), s.id
        signed, opened = signed_ctx.store.releases(PERIOD), open_ctx.store.releases(PERIOD)
        assert [(r.release_id, r.facts_checksum, r.kpi_totals) for r in signed] == [
            (r.release_id, r.facts_checksum, r.kpi_totals) for r in opened]
        assert [r.approved_by for r in signed] == ["Fran Planner (Regional FP&A lead)"] * 2
        assert [r.approved_by for r in opened] == ["Regional FP&A lead"] * 2
        sid = signed_ctx.store.latest_submission("AU", PERIOD, "actual").submission_id
        assert [a.label for a in signed_ctx.store.receipt_actors(sid)] == ["Alex Australia (AU country controller)"]
    finally:
        open_ctx.store.close()
        signed_ctx.store.close()


def control_view(c: controls.Controls) -> tuple:
    return ({s.accrual_id: (s.invoiced, s.invoice_state, s.reversal_state, s.overall, s.owner) for s in c.accruals},
            [(s.type, s.item, s.owner, s.amount_at_risk) for s in c.signals],
            {(m, p): [(l.item, l.net, l.pending, l.after_pending) for l in controls.pl_expense(c, m, p)]
             for m, p in controls.pl_scopes(c)},
            [(e.accrual_id, e.kind, e.debit, e.credit, e.pl_effect) for s in c.accruals
             for e in controls.pending_entries(s)])


def signed_in_controls_story(store, ws, p: dict[str, Actor]) -> None:
    """C1-C9 with real people: C8 is a request its preparer cannot approve, C9 the close lead approving it."""
    step = {s.id: s.at for s in demo.CONTROL_STEPS}
    ok = lambda r, outcome: r.outcome == outcome or pytest.fail(r.message)
    controls.import_files(store, ws, files(accruals="accruals_2026-03.csv", invoices="invoices_2026-04.csv",
                                           gl="gl_2026-04.csv"), step["C1"], by=p["pat"])
    ok(controls.link(store, ws, "INV-5504", "A-004", p["pat"], now=step["C2"]), "recorded")
    ok(controls.link(store, ws, "INV-5504", "A-004", p["pat"], now=step["C3"]), "no_op")
    ok(controls.link(store, ws, "INV-5504", "A-005", p["ana"], now=step["C4"]), "refused")
    ok(controls.explain(store, ws, "A-001", "Scope extension approved by SG GM", p["ana"], now=step["C5"]),
       "recorded")
    ok(controls.assign(store, ws, "A-006", ANALYST, p["lee"], now=step["C6"]), "recorded")
    controls.import_files(store, ws, files(gl="gl_2026-04_v2.csv"), step["C7"], by=p["pat"])
    ok(controls.resolve(store, ws, "A-003", "re_accrue", Decimal(35000), "Phase 2 invoice due May", p["ana"],
                        now=step["C8"]), "requested")
    ok(controls.approve(store, ws, 1, p["ana"], step["C8"]), "refused")
    ok(controls.approve(store, ws, 1, p["lee"], step["C9"]), "recorded")


def test_the_controls_story_signed_in_matches_open_mode(tmp_path, ws):
    with Store.open(tmp_path / "open.duckdb") as open_store, Store.open(tmp_path / "signed.duckdb") as signed:
        demo.replay_controls(open_store, ws, "C9")
        signed_in_controls_story(signed, ws, people(signed, ws))
        for when in (demo.control_step("C9").at, LATER):
            assert control_view(controls.load(signed, ws, when)) == control_view(controls.load(open_store, ws, when))
        c = controls.load(signed, ws, LATER)
        [h] = [h for h in controls.history(c, "A-003") if h.what.startswith("Resolved")]
        assert h.by == "Ana Analyst (SG finance analyst)" and "approved by Lee Lead (Regional close lead)" in h.detail


def test_c10_signed_in_matches_open_mode(tmp_path, ws):
    at = datetime(2026, 5, 4, 13, 0)
    with Store.open(tmp_path / "open.duckdb") as open_store, Store.open(tmp_path / "signed.duckdb") as signed:
        demo.replay_controls(open_store, ws, "C1")
        controls.resolve(open_store, ws, "A-002", by=ACCOUNTING, approved_by=LEAD, now=at, **WRITE_BACK)
        p = people(signed, ws)
        controls.import_files(signed, ws, files(accruals="accruals_2026-03.csv", invoices="invoices_2026-04.csv",
                                                gl="gl_2026-04.csv"), demo.control_step("C1").at, by=p["pat"])
        assert controls.resolve(signed, ws, "A-002", by=p["cam"], now=at, **WRITE_BACK).outcome == "requested"
        assert controls.approve(signed, ws, 1, p["lee"], at).outcome == "recorded"
        assert control_view(controls.load(signed, ws, LATER)) == control_view(controls.load(open_store, ws, LATER))


# The review round: facts bound to requests, the real clock, sessions and the admin/approver split


def strict_ws(ws):
    return replace(ws, config=replace(ws.config, controls=replace(ws.config.controls,
                                                                  explain_approval_abs=Decimal(0))))


def reimport_invoices(store, ws, by, old: bytes, new: bytes, at) -> None:
    data = (EX / "invoices_2026-04.csv").read_bytes().replace(old, new)
    controls.import_files(store, ws, {"invoices": ("invoices_2026-04_v2.csv", data)}, at, by=by)


def test_an_approval_is_refused_when_the_facts_changed_since_the_request(store, ws, c1):
    strict = strict_ws(ws)
    r = controls.explain(store, strict, "A-001", "Scope extension approved by SG GM", c1["ana"], now=T0)
    assert r.outcome == "requested"
    [request] = controls.pending_requests(store)
    assert dict(request.facts) == {"accrued": "100,000.00", "invoiced": "110,000.00", "difference": "+10,000.00"}
    reimport_invoices(store, strict, c1["pat"], b"110000.00", b"190000.00", T0 + timedelta(minutes=1))
    done = controls.approve(store, strict, 1, c1["lee"], T0 + timedelta(minutes=2))
    assert (done.outcome, done.message) == (
        "refused", "The facts changed since the request: invoiced 110,000.00 → 190,000.00; difference +10,000.00 → "
                   "+90,000.00. Reject it and request again. Nothing recorded.")
    assert controls.pending_requests(store) == (request,)
    assert [e for e in store.control_events() if e.kind == "explain"] == []
    assert controls.reject(store, strict, 1, c1["lee"], "Facts changed", T0).outcome == "recorded"


def test_resolve_and_link_approvals_are_bound_to_their_facts_too(store, ws, c1):
    controls.resolve(store, ws, "A-002", by=c1["cam"], now=T0, **WRITE_BACK)
    controls.link(store, ws, "INV-5503", "A-001", c1["pat"], override=True, reason="Same supplier group", now=T0)
    resolve_request, link_request = controls.pending_requests(store)
    assert dict(resolve_request.facts) == {"issue": "missing_reversal", "amount at risk": "40,000.00",
                                           "accrued": "40,000.00", "invoiced": "40,000.00",
                                           "reversal": "no reversal line"}
    assert dict(link_request.facts) == {"invoice": "Northpoint Research, SGD 25,000.00",
                                        "accrual": "Harbour Advisory, 100,000.00",
                                        "differs": link_request.facts["differs"], "linked to": "A-003"}
    reimport_invoices(store, ws, c1["pat"], b"40000.00", b"45000.00", T0)
    r = controls.approve(store, ws, resolve_request.request_id, c1["lee"], T0)
    assert r.outcome == "refused" and "invoiced 40,000.00 → 45,000.00" in r.message
    reimport_invoices(store, ws, c1["pat"], b"25000.00", b"26000.00", T0)
    r = controls.approve(store, ws, link_request.request_id, c1["cam"], T0)
    assert r.outcome == "refused"
    assert "invoice Northpoint Research, SGD 25,000.00 → Northpoint Research, SGD 26,000.00" in r.message
    assert len(controls.pending_requests(store)) == 2


def test_approving_a_request_that_would_change_nothing_closes_it(store, ws, c1):
    controls.link(store, ws, "INV-5504", "A-004", c1["pat"], override=True, reason="Rent for March", now=T0)
    assert controls.link(store, ws, "INV-5504", "A-004", c1["pat"], now=T0).outcome == "recorded"
    r = controls.approve(store, ws, 1, c1["cam"], T0)
    assert r.outcome == "recorded"
    assert r.message.startswith("Approved request 1 with nothing to record: INV-5504 is already linked to A-004")
    assert r.message.endswith(" The request is closed.")
    assert controls.pending_requests(store) == ()
    [(_, decision)] = controls.approvals(store)
    assert decision.approved and decision.reason.startswith("Nothing to record: INV-5504 is already linked")
    assert len(store.control_events()) == 1


NOW = datetime(2026, 11, 27, 9, 0)


def test_signed_in_mode_records_the_real_clock_and_keeps_the_claimed_time_as_effective(store, ws, c1, tmp_path):
    store.clock = lambda: NOW
    assert controls.assign(store, ws, "A-006", ANALYST, c1["lee"], now=T0).outcome == "recorded"
    [event] = store.control_events()
    assert (event.at, event.logged_at) == (T0, NOW)
    c = controls.load(store, ws, T0)
    assert c.accrual("A-006").owner == ANALYST
    [h] = [h for h in controls.history(c, "A-006") if h.what.startswith("Owner")]
    assert identity.when_text(h.at, h.logged_at) == "recorded 2026-11-27 09:00, effective 2026-05-04 08:00"

    controls.resolve(store, ws, "A-002", by=c1["cam"], now=T0, **WRITE_BACK)
    controls.approve(store, ws, 1, c1["lee"], T0)
    [(request, decision)] = controls.approvals(store)
    assert (request.at, request.logged_at, decision.at, decision.logged_at) == (T0, NOW, T0, NOW)

    copy_inputs(tmp_path)
    validation.ingest(store, ws, tmp_path / "examples" / "sg_actual_v1.csv", "SG", PERIOD, "actual", T0,
                      by=c1["sam"])
    sub = store.latest_submission("SG", PERIOD, "actual")
    assert (sub.received_at, sub.logged_at) == (T0, NOW)
    controls.import_files(store, ws, files(gl="gl_2026-04_v2.csv"), T0, by=c1["pat"])
    assert store.control_imports()[-1].logged_at == NOW


def test_open_mode_records_no_separate_clock(store, ws):
    demo.replay_controls(store, ws, "C9")
    assert {e.logged_at for e in store.control_events()} == {None}
    assert {i.logged_at for i in store.control_imports()} == {None}
    c = controls.load(store, ws, LATER)
    assert all(identity.when_text(h.at, h.logged_at) == h.at.strftime("%Y-%m-%d %H:%M")
               for h in controls.history(c, "A-003"))


def test_wrong_current_passwords_count_toward_the_lockout(store, ws):
    admin_store(store, ws)
    for _ in range(5):
        with pytest.raises(IdentityError, match="current password is wrong"):
            identity.change_password(store, "ada", "not the password", "a brand new password", T0)
    with pytest.raises(IdentityError, match="locked after 5 wrong passwords"):
        identity.change_password(store, "ada", PASSWORD, "a brand new password", T0)
    with pytest.raises(IdentityError, match="locked after 5 wrong passwords"):
        identity.sign_in(store, "ada", PASSWORD, T0)


def test_password_changes_disable_and_role_removal_end_sessions(store, ws):
    admin = admin_store(store, ws)
    identity.add_user(store, ws, admin, "dual", "Dee Dual", [ACCOUNTING, LEAD], PASSWORD, T0)
    signed = lambda: identity.start_session(store, identity.sign_in(store, "dual", PASSWORD, T0), T0)
    alive = lambda token: identity.session(store, token, T0) is not None

    token = signed()
    identity.set_roles(store, ws, admin, "dual", [ACCOUNTING, LEAD], T0)
    assert alive(token)
    identity.set_roles(store, ws, admin, "dual", [ACCOUNTING], T0)
    assert not alive(token)

    token = signed()
    identity.set_active(store, admin, "dual", False, T0)
    identity.set_active(store, admin, "dual", True, T0)
    assert not alive(token)

    token = signed()
    identity.reset_password(store, admin, "dual", PASSWORD, T0)
    assert not alive(token)

    token = signed()
    identity.change_password(store, "dual", PASSWORD, PASSWORD, T0)
    assert not alive(token)


def test_signing_in_purges_expired_and_ended_sessions(store, ws):
    admin = admin_store(store, ws)
    identity.add_user(store, ws, admin, "pat", "Pat Payables", [AP], PASSWORD, T0)
    ada = store.user("ada")
    identity.start_session(store, ada, T0 - timedelta(hours=13))
    store.add_session("orphan", "nobody", T0, T0 + timedelta(hours=1))
    keep = identity.start_session(store, ada, T0)
    identity.start_session(store, store.user("pat"), T0 + timedelta(minutes=1))
    assert store.session_count() == 2
    assert identity.session(store, keep, T0) is not None


def test_an_admin_cannot_hold_an_approver_role(store, ws):
    admin = admin_store(store, ws)
    with pytest.raises(IdentityError, match=r"^A user holding admin cannot also hold an approver role "
                                            r"\(Accounting team\)"):
        identity.add_user(store, ws, admin, "boss", "Bo Boss", ["admin", ACCOUNTING], PASSWORD, T0)
    with pytest.raises(IdentityError, match=r"^An approver role \(Regional close lead\) cannot be given to an "
                                            r"admin"):
        identity.set_roles(store, ws, admin, "ada", ["admin", LEAD], T0)
    identity.add_user(store, ws, admin, "cam", "Cam Accounts", [ACCOUNTING], PASSWORD, T0)
    with pytest.raises(IdentityError, match=r"^A user holding an approver role \(Accounting team\) cannot also "
                                            r"be made admin"):
        identity.set_roles(store, ws, admin, "cam", [ACCOUNTING, "admin"], T0)
    assert store.user("cam").roles == (ACCOUNTING,) and store.user("ada").roles == ("admin",)


@pytest.mark.parametrize("content", [b"\xff\xfe\x00garbage", b"not-a-token", "café".encode()])
def test_a_corrupt_token_file_is_not_signed_in(tmp_path, monkeypatch, content):
    monkeypatch.setenv("FCT_SESSIONS_DIR", str(tmp_path / "sessions"))
    path = identity.session_file(tmp_path / "s.duckdb")
    path.parent.mkdir(parents=True)
    path.write_bytes(content)
    assert identity.read_token(tmp_path / "s.duckdb") is None
