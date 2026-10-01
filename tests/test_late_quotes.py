"""Late quotes (0138): quotes recorded after the bid was submitted.

Once the send_out lane head is `submitted` or `bid_outcome` (it stays there
after a win/loss and after the PM handoff) vendors may still quote. Those
quotes are recorded on Receive Quotes for the record only:

  * the quote each category was sent with is fully locked (amount, tax,
    delete, approval) and selection cannot change at all,
  * nothing quote-side bounces the bid back to Verify,
  * a late quote must carry its sales-tax answer, is tagged
    received_after_submission, notifies both Estimating engineer focuses,
  * General Material's figure is locked when it is the sent basis,
  * analytics never see a late quote.

Between Verify and sending (head `send_out`) the old behavior is unchanged:
quote edits still bounce to Verify.

Handlers are called directly against the in-memory fake, so `Depends` (auth,
rate limits) never runs.
"""

from datetime import datetime, timezone
from decimal import Decimal

import pytest
from fastapi import HTTPException

from app.core.deps import CurrentUser
from app.core.roles import Role
from app.models.schemas import (
    GeneralMaterialIn,
    ManualQuoteIn,
    QuoteApprovalIn,
    QuoteIn,
    QuoteOverrideIn,
    ReplyManualQuoteIn,
    RfqQuotesConfirmIn,
    TaxIn,
)
from app.routers import general_material as gm_router
from app.routers import rfqs as rfqs_router
from app.services import analytics_metrics as m
from app.services import general_material as gm
from app.services import notification_email, rfq_inbox, workflow
from tests.test_workflow import FakeDB as _BaseFakeDB
from tests.test_workflow import _Query

PID = "p1"


class _IdQuery(_Query):
    """Inserts get an id, as Postgres would give them."""

    _n = 0

    def insert(self, payload):
        rows = payload if isinstance(payload, list) else [payload]
        for r in rows:
            if "id" not in r:
                _IdQuery._n += 1
                r["id"] = f"new-{_IdQuery._n}"
        return super().insert(payload)


class FakeDB(_BaseFakeDB):
    def table(self, name):
        return _IdQuery(self, name)


def _user(uid="u1"):
    return CurrentUser(
        id=uid, email="mats@g3.com", role=Role.ESTIMATING_ENGINEER_MATERIALS, is_active=True
    )


def _rfq(rfq_id, name, *, is_general=False):
    return {
        "id": rfq_id,
        "project_id": PID,
        "status": "quotes_in",
        "material_category_id": f"mc-{rfq_id}",
        "material_categories": {
            "name": name,
            "is_general": is_general,
            "pricing_section": "materials",
            "sort_order": 1,
        },
    }


def _quote(qid, rfq_id, amount, *, origin="vendor", selected=False, vendor_id="v1", late=False):
    return {
        "id": qid,
        "rfq_id": rfq_id,
        "vendor_id": vendor_id,
        "amount": amount,
        "origin": origin,
        "source": "manual",
        "is_approved": True,
        "is_selected": selected,
        "tax_included": True,
        "tax_rate": "8.375",
        "received_at": None,
        "notes": None,
        "quote_file_id": None,
        "rfq_message_id": None,
        "received_after_submission": late,
    }


def _lanes(send_out_head="submitted"):
    spec = {
        "intake": ("to_estimator", "complete"),
        "material_numbers": ("receive_quotes", "complete"),
        "labor_numbers": ("labor_numbers", "complete"),
        "select_vendors": ("select_vendors", "complete"),
        "markup": ("markup", "complete"),
        "send_out": (send_out_head, "active"),
    }
    return [
        {
            "project_id": PID,
            "category": cat,
            "current_task": task,
            "status": st,
            "owner_role": None,
            "completed_at": "2026-09-01T00:00:00Z" if st == "complete" else None,
        }
        for cat, (task, st) in spec.items()
    ]


def _db(head="submitted", **extra):
    tables = {
        "projects": [
            {
                "id": PID,
                "name": "Test Plaza",
                "number": "26.9.7001",
                "current_stage": head,
                "abandoned_at": None,
                "reverify_return_stage": None,
            }
        ],
        "rfqs": [_rfq("r-fix", "Fixtures"), _rfq("r-gen", "General Material", is_general=True)],
        "quotes": [
            _quote("q-sent", "r-fix", "1000", selected=True),
            _quote("q-other", "r-fix", "1200"),
            _quote("q-gen-est", "r-gen", "500", origin="estimate", selected=True, vendor_id=None),
        ],
        "vendors": [{"id": "v1", "name": "Acme Electric Supply"}, {"id": "v2", "name": "Late Co"}],
        "project_category_state": _lanes(head),
        "project_files": [],
        "audit_log": [],
        "rfq_messages": [],
        "rfq_sends": [],
        "general_material_estimates": [
            {"project_id": PID, "amount": "500", "tax_included": True, "tax_rate": "8.375",
             "status": "done", "source": "extracted"}
        ],
        "verifications": [],
        "stage_events": [],
    }
    tables.update(extra)
    return FakeDB(tables)


@pytest.fixture
def env(monkeypatch):
    """Fake DB wired into the routers; bounces, notifications and audits recorded."""
    state = {"db": _db(), "bounces": [], "notes": [], "audits": []}

    def _set(head="submitted", **extra):
        state["db"] = _db(head, **extra)
        return state["db"]

    monkeypatch.setattr(rfqs_router, "get_supabase", lambda: state["db"])
    monkeypatch.setattr(gm_router, "get_supabase", lambda: state["db"])
    monkeypatch.setattr(gm, "get_supabase", lambda: state["db"])
    monkeypatch.setattr(rfqs_router, "dismiss_notifications", lambda **k: None)
    monkeypatch.setattr(
        rfqs_router, "audit", lambda actor, action, *a, **k: state["audits"].append(action)
    )
    monkeypatch.setattr(
        gm_router, "audit", lambda actor, action, *a, **k: state["audits"].append(action)
    )
    monkeypatch.setattr(
        rfqs_router,
        "notify_role",
        lambda role, pid, type_, msg, **k: state["notes"].append((role, type_, msg, k)),
    )
    monkeypatch.setattr(
        workflow,
        "maybe_reopen_verify_after_edit",
        lambda pid, uid, reason, stale: state["bounces"].append(reason),
    )
    state["set"] = _set
    return state


def _q(db, qid):
    return next(q for q in db.tables["quotes"] if q["id"] == qid)


def _409(fn, *args):
    with pytest.raises(HTTPException) as exc:
        fn(*args)
    assert exc.value.status_code == 409
    return exc.value.detail


# ── 1. Window detection ──────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "head, expected",
    [
        ("gc_pricing", False),
        ("verify", False),
        ("send_out", False),
        ("submitted", True),
        ("bid_outcome", True),
    ],
)
def test_window_is_submitted_or_bid_outcome(head, expected):
    assert workflow.in_post_submission_window(PID, _db(head)) is expected


def test_window_stays_open_after_a_win_hands_off_to_pm():
    # Recording the outcome parks the send_out head at bid_outcome for good;
    # the PM handoff never touches the bidding lanes.
    db = _db("bid_outcome")
    db.tables["projects"][0]["pm_stage"] = "precon"
    assert workflow.in_post_submission_window(PID, db) is True


def test_no_lane_rows_means_no_window():
    assert workflow.in_post_submission_window(PID, FakeDB({})) is False


# ── 2. The sent quote is locked ──────────────────────────────────────────────


def test_sent_quote_amount_edit_is_refused(env):
    detail = _409(
        rfqs_router.override_quote, PID, "r-fix", "q-sent", QuoteOverrideIn(amount=Decimal("1")), _user()
    )
    assert "sent with" in detail
    assert _q(env["db"], "q-sent")["amount"] == "1000"


def test_sent_quote_tax_change_is_refused(env):
    _409(
        rfqs_router.set_quote_tax, PID, "r-fix", "q-sent", TaxIn(tax_included=False), _user()
    )
    assert _q(env["db"], "q-sent")["tax_included"] is True


def test_sent_quote_delete_is_refused(env):
    _409(rfqs_router.delete_quote, PID, "r-fix", "q-sent", _user())
    assert any(q["id"] == "q-sent" for q in env["db"].tables["quotes"])


def test_sent_quote_approval_withdraw_is_refused(env):
    _409(
        rfqs_router.set_quote_approval, PID, "r-fix", "q-sent", QuoteApprovalIn(approved=False), _user()
    )
    assert _q(env["db"], "q-sent")["is_selected"] is True


def test_select_is_refused_in_the_window(env):
    detail = _409(rfqs_router.select_quote, PID, "r-fix", "q-other", _user())
    assert "selected vendor" in detail
    assert _q(env["db"], "q-sent")["is_selected"] is True
    assert _q(env["db"], "q-other")["is_selected"] is False


def test_clear_is_refused_in_the_window(env):
    _409(rfqs_router.clear_selected_quote, PID, "r-fix", _user())
    assert _q(env["db"], "q-sent")["is_selected"] is True


def test_selecting_a_late_quote_is_refused(env):
    env["db"].tables["quotes"].append(_quote("q-late", "r-fix", "900", late=True))
    _409(rfqs_router.select_quote, PID, "r-fix", "q-late", _user())
    assert _q(env["db"], "q-late")["is_selected"] is False


# ── 3. Non-winning quotes stay editable, with no bounce ──────────────────────


def test_non_winning_quote_edits_never_bounce_in_the_window(env):
    rfqs_router.override_quote(PID, "r-fix", "q-other", QuoteOverrideIn(amount=Decimal("1100")), _user())
    rfqs_router.set_quote_tax(PID, "r-fix", "q-other", TaxIn(tax_included=False), _user())
    rfqs_router.set_quote_approval(PID, "r-fix", "q-other", QuoteApprovalIn(approved=False), _user())
    rfqs_router.set_quotes_confirmed(PID, "r-fix", RfqQuotesConfirmIn(confirmed=True), _user())
    rfqs_router.delete_quote(PID, "r-fix", "q-other", _user())
    assert env["bounces"] == []
    assert _q(env["db"], "q-sent")["is_selected"] is True


def test_the_same_edits_still_bounce_at_send_out(env):
    env["set"]("send_out")
    rfqs_router.set_quote_tax(PID, "r-fix", "q-sent", TaxIn(tax_included=False), _user())
    rfqs_router.override_quote(PID, "r-fix", "q-sent", QuoteOverrideIn(amount=Decimal("990")), _user())
    rfqs_router.set_quotes_confirmed(PID, "r-fix", RfqQuotesConfirmIn(confirmed=True), _user())
    rfqs_router.select_quote(PID, "r-fix", "q-other", _user())
    assert env["bounces"] == [
        "Quote tax setting changed",
        "Vendor quote amount changed",
        "Quotes-confirmed flag changed",
        "Quote selection changed",
    ]


def test_real_bounce_hook_moves_send_out_but_not_a_submitted_bid(monkeypatch):
    """End to end through the real maybe_reopen_verify_after_edit: a winner's
    tax edit at send_out bounces to verify; a late quote's tax edit after
    submission leaves the head where it is."""
    for head, moved in (("send_out", True), ("submitted", False)):
        db = _db(head)
        db.tables["quotes"].append(_quote("q-late", "r-fix", "900", late=True))
        monkeypatch.setattr(rfqs_router, "get_supabase", lambda db=db: db)
        monkeypatch.setattr(workflow, "get_supabase", lambda db=db: db)
        monkeypatch.setattr(rfqs_router, "audit", lambda *a, **k: None)
        monkeypatch.setattr(workflow.notifications, "notify_role", lambda *a, **k: None)
        monkeypatch.setattr(workflow.notifications, "dismiss_notifications", lambda **k: None)
        target = "q-sent" if head == "send_out" else "q-late"
        rfqs_router.set_quote_tax(PID, "r-fix", target, TaxIn(tax_included=False), _user())
        so = next(r for r in db.tables["project_category_state"] if r["category"] == "send_out")
        assert (so["current_task"] == "verify") is moved, head


# ── 4. Adding a late quote ───────────────────────────────────────────────────


def test_late_vendor_quote_requires_a_tax_answer(env):
    with pytest.raises(HTTPException) as exc:
        rfqs_router.add_quote(PID, "r-fix", QuoteIn(vendor_id="v2", amount=Decimal("900")), _user())
    assert exc.value.status_code == 422
    assert not any(q.get("vendor_id") == "v2" for q in env["db"].tables["quotes"])


def test_tax_is_optional_before_submission(env):
    env["set"]("send_out")
    row = rfqs_router.add_quote(PID, "r-fix", QuoteIn(vendor_id="v2", amount=Decimal("900")), _user())
    assert "tax_included" not in row
    assert not row.get("received_after_submission")
    assert env["notes"] == []


def test_late_vendor_quote_is_tagged_approved_and_notified(env):
    row = rfqs_router.add_quote(
        PID,
        "r-fix",
        QuoteIn(vendor_id="v2", amount=Decimal("900"), tax_included=False, tax_rate=Decimal("8.375")),
        _user(),
    )
    assert row["received_after_submission"] is True
    assert row["is_approved"] is True and row["tax_included"] is False
    assert not row.get("is_selected")
    # Both engineer focuses hear about it, with the agreed wording.
    roles = {n[0] for n in env["notes"]}
    assert roles == {Role.ESTIMATING_ENGINEER_MATERIALS, Role.ESTIMATING_ENGINEER_LABOR}
    assert all(n[1] == "late_quote.received" for n in env["notes"])
    assert env["notes"][0][2] == (
        "New quote received for 26.9.7001 Test Plaza from Late Co (Fixtures), "
        "after the bid was submitted."
    )
    assert "quote.late_add" in env["audits"]
    assert env["bounces"] == []


def test_late_hand_entered_quote_is_tagged_and_notified(env):
    row = rfqs_router.add_manual_quote(
        PID, "r-gen", ManualQuoteIn(amount=Decimal("450"), tax_included=True), _user()
    )
    assert row["received_after_submission"] is True
    assert len(env["notes"]) == 2
    assert "from a hand-entered price (General Material)" in env["notes"][0][2]
    assert "quote.late_add" in env["audits"]


def test_manual_quote_before_submission_is_not_tagged(env):
    env["set"]("send_out")
    row = rfqs_router.add_manual_quote(
        PID, "r-fix", ManualQuoteIn(amount=Decimal("450"), tax_included=True), _user()
    )
    assert "received_after_submission" not in row
    assert env["notes"] == []


def _reply_db(head):
    return _db(
        head,
        rfq_messages=[
            {
                "id": "m1",
                "extraction_status": "failed",
                "rfq_sends": {
                    "id": "s1",
                    "rfq_id": "r-fix",
                    "quote_received_at": None,
                    "vendor_contacts": {"id": "c2", "name": "Pat", "email": "pat@late.co",
                                        "vendor_id": "v2", "vendors": {"name": "Late Co"}},
                    "rfqs": {"id": "r-fix", "project_id": PID, "material_category_id": "mc-r-fix",
                             "material_categories": {"name": "Fixtures"}},
                },
            }
        ],
    )


def test_late_reply_quote_requires_tax_then_tags(env):
    env["db"] = _reply_db("submitted")
    with pytest.raises(HTTPException) as exc:
        rfqs_router.add_reply_manual_quote(PID, "m1", ReplyManualQuoteIn(amount=Decimal("800")), _user())
    assert exc.value.status_code == 422
    row = rfqs_router.add_reply_manual_quote(
        PID, "m1", ReplyManualQuoteIn(amount=Decimal("800"), tax_included=True), _user()
    )
    assert row["received_after_submission"] is True
    assert {n[1] for n in env["notes"]} == {"late_quote.received"}


def test_late_quotes_never_count_as_lowest(env):
    env["db"].tables["quotes"].append(_quote("q-late", "r-fix", "10", late=True))
    out = rfqs_router.list_quotes(PID, "r-fix", _user())
    assert Decimal(out["lowest_amount"]) == Decimal("1000")


def test_vendor_selection_carries_the_tag(env):
    env["db"].tables["quotes"].append(_quote("q-late", "r-fix", "10", late=True))
    fix = next(c for c in rfqs_router.get_vendor_selection(PID, _user()) if c["rfq_id"] == "r-fix")
    tags = {c["id"]: c["received_after_submission"] for c in fix["quotes"]}
    assert tags["q-late"] is True and tags["q-sent"] is False
    assert fix["selected_quote_id"] == "q-sent"


# ── 5. Ingestion ────────────────────────────────────────────────────────────


def _ingest(monkeypatch, head):
    db = _db(head)
    monkeypatch.setattr(rfq_inbox, "extract_quote_from_pdf",
                        lambda *a, **k: {"total_amount": 777, "confidence": 0.99})
    monkeypatch.setattr(rfq_inbox, "_valid_extracted_amount", lambda r: True)
    monkeypatch.setattr(rfq_inbox, "notify_role", lambda *a, **k: None)
    send = {
        "id": "s1",
        "rfqs": {"id": "r-fix", "project_id": PID, "material_categories": {"name": "Fixtures"},
                 "projects": {"id": PID, "name": "Test Plaza", "number": "26.9.7001"}},
        "vendor_contacts": {"id": "c2", "name": "Pat", "vendor_id": "v2", "vendors": {"name": "Late Co"}},
    }
    status_ = rfq_inbox._run_extraction(db, send, {"id": "m1"}, [({"id": "f1", "filename": "q.pdf"}, b"%PDF")])
    assert status_ == "done"
    new = [q for q in db.tables["quotes"] if q.get("vendor_id") == "v2"]
    assert len(new) == 1
    so = next(r for r in db.tables["project_category_state"] if r["category"] == "send_out")
    return new[0], so


def test_an_ingested_quote_after_submission_is_tagged_and_never_bounces(monkeypatch):
    quote, so = _ingest(monkeypatch, "submitted")
    assert quote["received_after_submission"] is True
    assert not quote.get("is_selected")
    assert so["current_task"] == "submitted"


def test_an_ingested_quote_before_submission_is_not_tagged(monkeypatch):
    quote, _ = _ingest(monkeypatch, "send_out")
    assert "received_after_submission" not in quote


# ── 6. General Material ──────────────────────────────────────────────────────


def test_general_figure_is_locked_when_it_is_the_sent_estimate(env):
    _409(gm_router.set_general_material, PID, GeneralMaterialIn(amount=Decimal("1")), _user())
    _409(gm_router.set_general_material_tax, PID, TaxIn(tax_included=False), _user())
    _409(gm_router.rerun_extraction, PID, None, _user())
    assert env["db"].tables["general_material_estimates"][0]["amount"] == "500"


def test_general_figure_is_locked_when_pricing_synthesizes_it(env):
    db = env["set"]("submitted")
    db.tables["rfqs"] = [r for r in db.tables["rfqs"] if r["id"] != "r-gen"]
    assert gm.figure_locked(PID) is True


def test_general_figure_stays_editable_when_another_quote_won(env):
    db = env["set"]("submitted")
    _q(db, "q-gen-est")["is_selected"] = False
    db.tables["quotes"].append(_quote("q-gen-v", "r-gen", "480", selected=True))
    gm_router.set_general_material(PID, GeneralMaterialIn(amount=Decimal("510")), _user())
    gm_router.set_general_material_tax(PID, TaxIn(tax_included=True), _user())
    assert env["bounces"] == []


def test_general_figure_still_bounces_before_submission(env):
    env["set"]("send_out")
    gm_router.set_general_material(PID, GeneralMaterialIn(amount=Decimal("510")), _user())
    assert env["bounces"] == ["General material price changed"]


def test_locked_general_extraction_is_skipped(env):
    gm.execute(PID)
    row = env["db"].tables["general_material_estimates"][0]
    assert row["amount"] == "500" and row["status"] == "done"


# ── 7. Analytics ─────────────────────────────────────────────────────────────


def test_analytics_never_see_a_late_quote(monkeypatch):
    db = _db("bid_outcome")
    db.tables["quotes"].append(_quote("q-late", "r-fix", "10", late=True))
    monkeypatch.setattr(m, "get_supabase", lambda: db)
    w = m.WindowData(
        date_from=datetime(2026, 1, 1, tzinfo=timezone.utc),
        date_to=datetime(2027, 1, 1, tzinfo=timezone.utc),
        project_id=PID,
        projects={PID: {"id": PID, "name": "Test Plaza"}},
    )
    data = m._quotes_data(w)
    ids = {q.get("amount") for q in data["quotes_by_proj"][PID]}
    assert "10" not in ids and "1000" in ids


# ── 8. Registration ──────────────────────────────────────────────────────────


def test_late_quote_type_is_registered_for_email_and_deep_link(monkeypatch):
    assert "late_quote.received" in notification_email._TYPE_META
    assert "quote.late_add" in m.ACTIVITY_ACTION_LABELS
    monkeypatch.setattr(notification_email, "is_enabled", lambda app: True)
    link = notification_email._deep_link(PID, Role.ESTIMATING_ENGINEER_LABOR.value, "late_quote.received")
    assert link.endswith(f"/projects/{PID}?step=receive_quotes")
