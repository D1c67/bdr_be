from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from app.services import assistant_calendar as cal
from app.services.assistant_graph import GraphFailure

NOW = datetime(2026, 11, 1, 8, 55, tzinfo=timezone.utc)


def test_calendar_utc_occurrences_across_dst_are_distinct():
    a = cal.starts_at({"start": {"dateTime": "2026-11-01T08:30:00", "timeZone": "UTC"}})
    b = cal.starts_at({"start": {"dateTime": "2026-11-01T09:30:00", "timeZone": "UTC"}})
    assert b - a == timedelta(hours=1)
    assert cal.starts_at({"start": {"dateTime": "2026-11-01T01:30:00", "timeZone": "Pacific Standard Time"}}) is None


def test_context_is_exact_attendee_contact_no_subject_guess():
    candidates = [{"id": "p", "contact_emails": ["gc@example.test"]}]
    assert cal.context_match({"subject": "gc@example.test"}, candidates) is None
    assert cal.context_match({"organizer": {"emailAddress": {"address": "GC@example.test"}}}, candidates) == "p"


def test_unavailable_permission_never_enables(monkeypatch):
    writes = []
    monkeypatch.setattr(cal, "configured", lambda: True)
    monkeypatch.setattr(cal, "events", lambda *a, **k: (_ for _ in ()).throw(GraphFailure(403)))
    monkeypatch.setattr(cal, "_repo", lambda: SimpleNamespace(calendar_preferences_set=lambda *a, **k: writes.append(k)))
    with pytest.raises(GraphFailure):
        cal.update_preferences("u", enabled=True, mode="context", lead_minutes=15)
    assert writes == [{"enabled": False, "mode": "context", "lead_minutes": 15}]


def test_tick_no_model_deduplicates_and_skips_cancelled(monkeypatch):
    claimed = set()
    def claim(uid, key, start, lead, **kwargs):
        marker = (uid, key, start, lead)
        if marker in claimed:
            return False
        claimed.add(marker)
        return True
    prefs = {"enabled": True, "mode": "all", "lead_minutes": 15}
    fake = SimpleNamespace(calendar_owners=lambda *a, **k: [{"user_id": "u"}],
                           calendar_preferences_get=lambda u: prefs, calendar_reminder_claim=claim)
    monkeypatch.setattr(cal, "_repo", lambda: fake)
    monkeypatch.setattr(cal, "configured", lambda: True)
    monkeypatch.setattr(cal.assistant_ms, "sender_mailbox", lambda u: "owner@example.test")
    event = {"id": "event", "start": {"dateTime": (NOW + timedelta(minutes=10)).isoformat()}}
    monkeypatch.setattr(cal, "events", lambda *a, **k: [event, {**event, "id": "cancelled", "isCancelled": True}])
    assert cal.tick(NOW)["reminders"] == 1
    assert cal.tick(NOW)["reminders"] == 0
    prefs["enabled"] = False
    assert cal.tick(NOW)["reminders"] == 0


def test_calendar_pagination_cannot_change_mailbox(monkeypatch):
    monkeypatch.setattr(cal, "configured", lambda: True)
    monkeypatch.setattr(cal.assistant_ms, "sender_mailbox", lambda u: "owner@example.test")
    monkeypatch.setattr(cal.assistant_ms, "for_mailbox", lambda *a: ("owner@example.test", "token"))
    monkeypatch.setattr(cal, "GraphTransport", lambda *a, **k: SimpleNamespace(read_json=lambda *a, **k: {
        "value": [], "@odata.nextLink": "https://graph.microsoft.com/v1.0/users/other@example.test/calendarView"}))
    with pytest.raises(GraphFailure):
        cal.events("u", NOW)


def test_permission_revocation_disables_existing_calendar_preference(monkeypatch):
    writes = []
    prefs = {"enabled": True, "mode": "all", "lead_minutes": 15}
    fake = SimpleNamespace(calendar_owners=lambda *a, **k: [{"user_id": "u"}],
        calendar_preferences_get=lambda uid: prefs,
        calendar_preferences_set=lambda uid, **kwargs: writes.append(kwargs))
    monkeypatch.setattr(cal, "_repo", lambda: fake)
    monkeypatch.setattr(cal, "configured", lambda: True)
    monkeypatch.setattr(cal.assistant_ms, "sender_mailbox", lambda uid: "owner@example.test")
    monkeypatch.setattr(cal, "events", lambda *a, **k: (_ for _ in ()).throw(GraphFailure(403)))
    assert cal.tick(NOW) == {"reminders": 0, "unavailable": 1}
    assert writes == [{"enabled": False, "mode": "all", "lead_minutes": 15}]


def test_pagination_rechecks_live_mapping_before_next_page(monkeypatch):
    monkeypatch.setattr(cal, "configured", lambda: True)
    addresses = iter(["owner@example.test", "owner@example.test", "new@example.test"])
    monkeypatch.setattr(cal.assistant_ms, "sender_mailbox", lambda uid: next(addresses))
    monkeypatch.setattr(cal.assistant_ms, "for_mailbox", lambda *args: ("owner@example.test", "token"))
    calls = []
    def read(path, **kwargs):
        calls.append(path)
        return {"value": [], "@odata.nextLink": "https://graph.microsoft.com/v1.0/users/owner@example.test/calendarView?$skiptoken=next"}
    monkeypatch.setattr(cal, "GraphTransport", lambda *a, **k: SimpleNamespace(read_json=read))
    with pytest.raises(ValueError):
        cal.events("u", NOW)
    assert len(calls) == 1


def test_tick_claim_carries_live_mode_and_primary_email_and_no_event_details(monkeypatch):
    claims = []
    prefs = {"enabled": True, "mode": "all", "lead_minutes": 15}
    def claim(*args, **kwargs):
        claims.append((args, kwargs))
        return True
    fake = SimpleNamespace(calendar_owners=lambda *a, **k: [{"user_id": "u"}],
        calendar_preferences_get=lambda uid: prefs, calendar_reminder_claim=claim)
    monkeypatch.setattr(cal, "_repo", lambda: fake)
    monkeypatch.setattr(cal, "configured", lambda: True)
    monkeypatch.setattr(cal.assistant_ms, "sender_mailbox", lambda uid: "owner@example.test")
    event = {"id": "stable-event", "subject": "Private event details", "body": "secret",
             "start": {"dateTime": (NOW + timedelta(minutes=10)).isoformat()}}
    monkeypatch.setattr(cal, "events", lambda *a, **k: [event, {**event, "id": "private", "sensitivity": "private"}])
    assert cal.tick(NOW)["reminders"] == 1
    assert claims[0][1] == {"expected_mode": "all", "expected_email": "owner@example.test"}
    assert "Private event details" not in str(claims) and "secret" not in str(claims)


@pytest.mark.parametrize("horizon", [True, 0, 181, "180"])
def test_calendar_window_validation_precedes_token_acquisition(monkeypatch, horizon):
    monkeypatch.setattr(cal, "configured", lambda: True)
    monkeypatch.setattr(cal.assistant_ms, "sender_mailbox", lambda *a: pytest.fail("No token work for invalid window"))
    with pytest.raises(ValueError):
        cal.events("u", NOW, horizon_minutes=horizon)


def test_disabled_calendar_has_no_owner_or_provider_access(monkeypatch):
    monkeypatch.setattr(cal, "configured", lambda: False)
    monkeypatch.setattr(cal, "_repo", lambda: pytest.fail("Disabled worker must not query owners"))
    assert cal.tick(NOW) == {"reminders": 0, "unavailable": 0}
