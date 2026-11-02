"""Phase 7 WI-10 (docs/ASSISTANT_PHASE7_PLAN.md 4.5): an AI-extracted vendor
quote that arrives after the bid was submitted is belled as `late_quote.received`
(once, never also `quote.received`), carries the email ids in bell metadata, and
writes the `quote.late_add` audit with via "ai". A bell or audit failure never
loses the quote or skips the message status write."""

import pytest

from app.core.roles import Role
from app.routers import rfqs as rfqs_router
from app.services import late_quote, notification_routing, rfq_inbox, workflow
from tests.test_late_quotes import PID, _db

PROJECT = {"id": PID, "name": "Test Plaza", "number": "26.9.7001"}


def _send(conversation_id="conv-1"):
    send = {
        "id": "s1",
        "rfqs": {"id": "r-fix", "project_id": PID, "material_categories": {"name": "Fixtures"},
                 "projects": dict(PROJECT)},
        "vendor_contacts": {"id": "c2", "name": "Pat", "vendor_id": "v2", "vendors": {"name": "Late Co"}},
    }
    if conversation_id:
        send["conversation_id"] = conversation_id
    return send


MESSAGE = {"id": "m1", "graph_message_id": "graph-1", "from_addr": "pat@late.co"}
PDFS = [({"id": "f1", "filename": "q.pdf"}, b"%PDF")]


@pytest.fixture
def rig(monkeypatch):
    state = {"bells": [], "audits": [], "notify_raises": False, "audit_raises": False}

    def _notify(roles, project_id, type_, message, **kw):
        if state["notify_raises"]:
            raise RuntimeError("bell down")
        state["bells"].append({"roles": roles, "type": type_, "message": message, **kw})

    def _audit(actor, action, entity=None, entity_id=None, payload=None):
        if state["audit_raises"]:
            raise RuntimeError("audit down")
        state["audits"].append((actor, action, entity, entity_id, payload))

    monkeypatch.setattr(rfq_inbox, "extract_quote_from_pdf",
                        lambda *a, **k: {"total_amount": 777, "confidence": 0.99})
    monkeypatch.setattr(rfq_inbox, "_valid_extracted_amount", lambda r: True)
    monkeypatch.setattr(rfq_inbox, "notify_roles", _notify)
    monkeypatch.setattr(rfq_inbox, "audit", _audit)

    def run(head="submitted", send=None, message=None, **kw):
        db = _db(head, rfq_messages=[dict(message or MESSAGE)])
        status = rfq_inbox._run_extraction(db, send or _send(), dict(message or MESSAGE), PDFS, **kw)
        state["db"] = db
        state["status"] = status
        return status

    state["run"] = run
    return state


def _new_quotes(db):
    return [q for q in db.tables["quotes"] if q.get("vendor_id") == "v2"]


def test_late_ai_quote_bells_late_quote_once_with_the_ids(rig):
    assert rig["run"](internet_message_id="<inet-1@vendor.com>") == "done"
    [bell] = rig["bells"]
    assert bell["type"] == "late_quote.received"
    assert tuple(bell["roles"]) == rfq_inbox.QUOTE_NEWS_ROLES
    assert bell["rfq_id"] == "r-fix"
    assert bell["mirror_email"] is True
    assert bell["metadata"] == {
        "rfq_message_id": "m1",
        "graph_message_id": "graph-1",
        "conversation_id": "conv-1",
        "internet_message_id": "<inet-1@vendor.com>",
    }
    assert "quote.received" not in {b["type"] for b in rig["bells"]}
    [quote] = _new_quotes(rig["db"])
    assert quote["received_after_submission"] is True


def test_on_time_ai_quote_keeps_the_plain_bell_and_gains_the_ids(rig):
    rig["run"]("send_out")
    [bell] = rig["bells"]
    assert bell["type"] == "quote.received"
    assert bell["message"].startswith("Quote received from ")
    assert bell["metadata"]["rfq_message_id"] == "m1"
    assert bell["metadata"]["graph_message_id"] == "graph-1"
    assert bell["metadata"]["conversation_id"] == "conv-1"
    assert "internet_message_id" not in bell["metadata"]
    assert rig["audits"] == []
    [quote] = _new_quotes(rig["db"])
    assert "received_after_submission" not in quote


def test_open_revision_gives_the_plain_bell(rig, monkeypatch):
    monkeypatch.setattr(workflow, "_revision_open", lambda project_id, client: True)
    rig["run"]("submitted")
    assert [b["type"] for b in rig["bells"]] == ["quote.received"]
    assert rig["audits"] == []
    [quote] = _new_quotes(rig["db"])
    assert "received_after_submission" not in quote


def test_late_audit_says_via_ai_and_names_the_inserted_quote(rig):
    rig["run"]()
    [(actor, action, entity, entity_id, payload)] = rig["audits"]
    [quote] = _new_quotes(rig["db"])
    assert (actor, action, entity) == (None, "quote.late_add", "quote")
    assert entity_id == quote["id"]
    assert payload["via"] == "ai"
    assert payload["rfq_id"] == "r-fix"
    assert payload["vendor_id"] == "v2"
    assert payload["quote_file_id"] == "f1"


def test_ids_are_read_with_get_so_missing_ones_are_left_out(rig):
    # The shape tests/test_late_quotes.py uses: a bare message row and a send
    # without a conversation id.
    rig["run"](send=_send(conversation_id=None), message={"id": "m1"})
    [bell] = rig["bells"]
    assert bell["metadata"] == {"rfq_message_id": "m1"}


def test_message_matches_what_the_manual_paths_build(monkeypatch):
    class _Table:
        def __init__(self, rows):
            self.rows = rows

        def select(self, *a, **k):
            return self

        def eq(self, *a, **k):
            return self

        def execute(self):
            return type("R", (), {"data": self.rows})()

    class _SB:
        def table(self, name):
            return _Table({"vendors": [{"name": "Late Co"}],
                           "projects": [{"name": "Test Plaza", "number": "26.9.7001"}]}[name])

    bells = []
    monkeypatch.setattr(rfqs_router, "notify_role", lambda role, pid, type_, msg, **k: bells.append(msg))
    monkeypatch.setattr(rfqs_router, "audit", lambda *a, **k: None)
    rfq = {"id": "r-fix", "material_categories": {"name": "Fixtures"}}
    rfqs_router._notify_late_quote(_SB(), PID, rfq, {"id": "q1", "vendor_id": "v2"}, "u1", "ai")
    ours = late_quote.late_quote_message(PROJECT, "Late Co", "Fixtures")
    assert bells == [ours, ours]
    # The fallbacks match too (no vendor, no project, no category).
    assert late_quote.late_quote_message(None, None, None) == (
        "New quote received for a project from a vendor (a category), after the bid was submitted."
    )


def test_a_bell_failure_never_loses_the_quote_or_the_status(rig):
    rig["notify_raises"] = True
    assert rig["run"]() == "done"
    assert len(_new_quotes(rig["db"])) == 1
    assert rig["db"].tables["rfq_messages"][0]["extraction_status"] == "done"


def test_a_plain_bell_failure_is_contained_too(rig):
    rig["notify_raises"] = True
    assert rig["run"]("send_out") == "done"
    assert len(_new_quotes(rig["db"])) == 1
    assert rig["db"].tables["rfq_messages"][0]["extraction_status"] == "done"


def test_an_audit_failure_never_loses_the_quote_or_the_status(rig):
    rig["audit_raises"] = True
    assert rig["run"]() == "done"
    assert len(_new_quotes(rig["db"])) == 1
    assert [b["type"] for b in rig["bells"]] == ["late_quote.received"]
    assert rig["db"].tables["rfq_messages"][0]["extraction_status"] == "done"


def test_reply_bell_carries_the_ids(monkeypatch):
    """_ingest_message: the rfq.reply_received bell gets the same keys, with the
    internet message id from the Graph message."""
    from app.core.config import Settings
    from app.services import graph_inbox
    from tests.test_email_ingest import FakeDB
    from tests.test_rfq_inbox import SEND, _msg

    db = FakeDB({
        "vendor_contacts": [{"id": "c1", "name": "Jane", "email": "jane@vendor.com", "vendor_id": "v1"}],
    })
    bells = []
    monkeypatch.setattr(rfq_inbox, "get_settings",
                        lambda: Settings(_env_file=None, ms_sender="bids@g3electrical.com"))
    monkeypatch.setattr(graph_inbox, "get_message", lambda mid, **k: {"body": {"content": "<p>hi</p>"}})
    monkeypatch.setattr(rfq_inbox, "audit", lambda *a, **k: None)
    monkeypatch.setattr(rfq_inbox, "notify_roles", lambda *a, **k: bells.append((a, k)))
    rfq_inbox._ingest_message(
        db,
        _msg("jane@vendor.com", hasAttachments=False, internetMessageId="<inet-9@vendor.com>"),
        {"conv-1": SEND},
    )
    [(args, kw)] = bells
    assert args[2] == "rfq.reply_received"
    [message] = db.tables["rfq_messages"]
    assert kw["metadata"] == {
        "rfq_message_id": message["id"],
        "graph_message_id": "msg-1",
        "conversation_id": "conv-1",
        "internet_message_id": "<inet-9@vendor.com>",
    }


def test_late_quote_routes_to_both_focuses_with_labor_email_and_teams():
    key = notification_routing.event_for("late_quote.received")
    assert key == "late_quote"
    event = next(e for e in notification_routing.EVENTS if e.key == key)
    for role in (Role.ESTIMATING_ENGINEER_MATERIALS, Role.ESTIMATING_ENGINEER_LABOR):
        assert role in event.audience
    assert notification_routing.default(event, Role.ESTIMATING_ENGINEER_LABOR, "email") is True
    assert notification_routing.default(event, Role.ESTIMATING_ENGINEER_LABOR, "teams") is True
