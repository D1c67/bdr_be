"""Unit tests for inbound RFQ reply matching — pure guard paths (no DB / Graph)."""

import pytest

from app.services import graph_inbox, rfq_inbox


def _msg(from_addr: str, conversation_id: str = "conv-1", **extra) -> dict:
    return {
        "id": "msg-1",
        "conversationId": conversation_id,
        "from": {"emailAddress": {"address": from_addr}},
        "subject": "RE: 26-104 - Riverside Plaza - BOM",
        "bodyPreview": "Quote attached",
        "receivedDateTime": "2026-06-10T12:00:00Z",
        "hasAttachments": True,
        **extra,
    }


SEND = {
    "id": "send-1",
    "conversation_id": "conv-1",
    "vendor_contacts": {"id": "c1", "name": "Jane", "email": "jane@vendor.com", "vendor_id": "v1"},
    "rfqs": {
        "id": "rfq-1",
        "project_id": "p1",
        "material_category_id": "mc1",
        "material_categories": {"name": "Switchgear"},
        "projects": {"id": "p1", "name": "Riverside Plaza", "number": "26-104"},
    },
}


# sb=None proves these paths never touch the database.


def test_own_sent_mail_is_skipped():
    rfq_inbox._ingest_message(None, _msg("bids@g3electrical.com"), {"conv-1": SEND})


def test_unmatched_conversation_is_skipped():
    rfq_inbox._ingest_message(None, _msg("jane@vendor.com", "other-conv"), {"conv-1": SEND})


def test_missing_from_address_is_skipped():
    rfq_inbox._ingest_message(None, {"id": "m", "conversationId": "conv-1"}, {"conv-1": SEND})


def test_sender_mismatch_is_audited_and_not_ingested(monkeypatch):
    calls = []
    monkeypatch.setattr(rfq_inbox, "audit", lambda *a, **k: calls.append((a, k)))
    rfq_inbox._ingest_message(None, _msg("stranger@elsewhere.com"), {"conv-1": SEND})
    assert len(calls) == 1
    assert calls[0][0][1] == "rfq.reply_sender_mismatch"
    assert calls[0][0][3] == "send-1"


def test_sender_match_is_case_insensitive(monkeypatch):
    # Reaching the idempotency check (first sb access) proves the guards passed.
    monkeypatch.setattr(rfq_inbox, "audit", lambda *a, **k: pytest.fail("should not audit"))
    with pytest.raises(AttributeError):  # sb=None -> .table() blows up at the DB step
        rfq_inbox._ingest_message(None, _msg("Jane@Vendor.COM"), {"conv-1": SEND})


def test_initial_delta_url_targets_inbox_with_window():
    url = graph_inbox.initial_delta_url()
    assert "/mailFolders/inbox/messages/delta" in url
    assert "$filter=receivedDateTime ge " in url
    assert "conversationId" in url  # in the $select list
