"""Security review: the 0138 post-submission quote lock must hold while a
re-verify bounce parks the send_out head at `verify`.

A post-submission pricing edit (PUT /labor, PUT /markup) calls
maybe_reopen_verify_after_edit, which moves a submitted bid's send_out head to
`verify` and records projects.reverify_return_stage = 'submitted'. The bid has
still gone out (proposal_send.gone_out_rule), so selection must stay locked,
the sent quote must stay frozen and new quotes must be tagged late.
"""

from decimal import Decimal

import pytest

from app.models.schemas import QuoteApprovalIn, QuoteOverrideIn
from app.routers import rfqs as rfqs_router
from app.services import workflow
from tests.test_late_quotes import PID, _409, _db, _ingest, _q, _user


def _so(db):
    return next(r for r in db.tables["project_category_state"] if r["category"] == "send_out")


def _bounce_submitted_bid(monkeypatch, head="submitted"):
    """Run the real labor-edit bounce on a submitted bid; return the parked db."""
    db = _db(head)
    monkeypatch.setattr(workflow, "get_supabase", lambda: db)
    monkeypatch.setattr(rfqs_router, "get_supabase", lambda: db)
    monkeypatch.setattr(rfqs_router, "audit", lambda *a, **k: None)
    monkeypatch.setattr(rfqs_router, "dismiss_notifications", lambda **k: None)
    monkeypatch.setattr(rfqs_router, "notify_role", lambda *a, **k: None)
    monkeypatch.setattr(workflow.notifications, "notify_role", lambda *a, **k: None)
    monkeypatch.setattr(workflow.notifications, "dismiss_notifications", lambda **k: None)
    workflow.maybe_reopen_verify_after_edit(PID, "u1", "Labor numbers edited", stale="labor")
    assert _so(db)["current_task"] == "verify"
    assert db.tables["projects"][0]["reverify_return_stage"] == head
    return db


@pytest.mark.parametrize("head", ["submitted", "bid_outcome"])
def test_window_holds_while_parked_at_verify(monkeypatch, head):
    db = _bounce_submitted_bid(monkeypatch, head)
    assert workflow.in_post_submission_window(PID, db) is True


def test_plain_verify_is_not_the_window():
    # A pre-send verify (or a bounce from send_out) is not post-submission.
    for return_stage in (None, "send_out"):
        db = _db("verify")
        db.tables["projects"][0]["reverify_return_stage"] = return_stage
        assert workflow.in_post_submission_window(PID, db) is False


def test_selection_stays_locked_after_a_labor_bounce(monkeypatch):
    db = _bounce_submitted_bid(monkeypatch)
    _409(rfqs_router.select_quote, PID, "r-fix", "q-other", _user())
    _409(rfqs_router.clear_selected_quote, PID, "r-fix", _user())
    assert _q(db, "q-sent")["is_selected"] is True
    assert _q(db, "q-other")["is_selected"] is False


def test_sent_quote_stays_frozen_after_a_labor_bounce(monkeypatch):
    db = _bounce_submitted_bid(monkeypatch)
    _409(
        rfqs_router.override_quote, PID, "r-fix", "q-sent",
        QuoteOverrideIn(amount=Decimal("1")), _user(),
    )
    _409(rfqs_router.delete_quote, PID, "r-fix", "q-sent", _user())
    _409(
        rfqs_router.set_quote_approval, PID, "r-fix", "q-sent",
        QuoteApprovalIn(approved=False), _user(),
    )
    assert _q(db, "q-sent")["amount"] == "1000"
    assert _q(db, "q-sent")["is_selected"] is True


def test_ingested_quote_while_parked_is_tagged_late(monkeypatch):
    db = _db("verify")
    db.tables["projects"][0]["reverify_return_stage"] = "submitted"
    monkeypatch.setattr("tests.test_late_quotes._db", lambda head: db)
    quote, so = _ingest(monkeypatch, "verify")
    assert quote["received_after_submission"] is True
    assert not quote.get("is_selected")
    assert so["current_task"] == "verify"
