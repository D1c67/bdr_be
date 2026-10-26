"""The initial notice follows a successful batch, never individual vendors."""

from types import SimpleNamespace

import pytest

from app.services import rfq_sending as rs
from tests.test_rfq_sending import _RecordingQuery


@pytest.mark.parametrize("statuses", [[], ["failed"], ["sent"], ["sent", "failed", "sent"]])
def test_bulk_send_announces_once_after_success(monkeypatch, statuses):
    import app.core.supabase_client as sbc

    project = {"id": "p1", "name": "Tower", "number": "42",
               "due_from_vendors_at": "2026-10-25T12:00:00Z"}
    contacts = [{"id": str(i), "name": "Vendor", "email": f"v{i}@example.com"}
                for i in range(len(statuses))]

    def table(name):
        def query(calls):
            if name == "projects":
                return project
            if name == "rfqs":
                return [{"id": "r1", "material_categories": {"name": "Lighting"}}]
            if name == "vendor_contacts":
                return contacts
            raise AssertionError(name)
        return _RecordingQuery(query)

    sb = SimpleNamespace(table=table)
    monkeypatch.setattr(sbc, "get_supabase", lambda: sb)
    monkeypatch.setattr(rs, "_prepare_explicit_attachments", lambda *a: SimpleNamespace(
        for_group=lambda ids: ([], None), used_link=False,
    ))
    monkeypatch.setattr(rs.time, "sleep", lambda _: None)
    sends, notices = [], []

    def send_one(sb, **kwargs):
        result = {"status": statuses[len(sends)], "rfq_id": "r1"}
        sends.append(result)
        return result

    monkeypatch.setattr(rs, "_send_one", send_one)
    monkeypatch.setattr(rs, "_notify_initial_send", lambda *a: notices.append(a))
    result = rs.bulk_send("p1", [{"rfq_id": "r1", "attachment_file_ids": [],
                                  "vendor_contact_ids": [c["id"] for c in contacts]}], "actor")
    assert result["results"] == sends
    assert len(notices) == int("sent" in statuses)
    if notices:
        assert notices[0] == (sb, project, sends)


def test_only_rows_returned_by_atomic_claim_are_delivered(monkeypatch):
    delivered, calls = [], []
    rows = [{"id": "n1", "user_id": "admin", "type": "rfq.initial_send"}]
    responses = iter([rows, [], []])  # another batch/worker has already claimed it

    def rpc(name, params):
        calls.append((name, params))
        return SimpleNamespace(execute=lambda: SimpleNamespace(data=next(responses)))

    monkeypatch.setattr(rs, "deliver", lambda rows: delivered.append(rows))
    for _ in range(3):
        rs._notify_initial_send(SimpleNamespace(rpc=rpc), {"id": "p1", "name": "Tower"},
                                [{"status": "sent"}, {"status": "failed"}])
    assert delivered == [rows]
    assert all(name == "notify_initial_rfq_send" for name, _ in calls)
    assert calls[0][1]["p_project_id"] == "p1"
    assert "1 email(s) sent" in calls[0][1]["p_message"]
    assert "1 email(s) failed" in calls[0][1]["p_message"]


def test_notification_failure_does_not_turn_vendor_delivery_into_failure(monkeypatch):
    def fail(*a):
        raise RuntimeError("database unavailable")
    rs._notify_initial_send(SimpleNamespace(rpc=fail), {"id": "p1", "name": "Tower"},
                            [{"status": "sent"}])


def test_failed_only_batch_does_not_claim_notification():
    rs._notify_initial_send(SimpleNamespace(), {"id": "p1", "name": "Tower"},
                            [{"status": "failed"}])
