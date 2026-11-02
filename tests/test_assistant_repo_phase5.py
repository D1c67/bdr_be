"""Phase 5 adapter fences; transaction behavior is tested against PostgreSQL."""
from types import SimpleNamespace

import pytest

from app.assistant import repo


class Client:
    def __init__(self):
        self.calls = []

    def rpc(self, name, params):
        self.calls.append((name, params))
        return self

    def execute(self):
        return SimpleNamespace(data={"ok": True, "claimed": True, "rows": []})


@pytest.fixture
def client(monkeypatch):
    value = Client()
    monkeypatch.setattr(repo, "_sb", lambda: value)
    return value


def test_send_claim_owner_and_hash(client):
    result = repo.mail_send_claim("owner", "approval", "run", "me@example.com", "a" * 64, ["to@example.com"])
    assert result["claimed"]
    name, args = client.calls[-1]
    assert name == "assistant_mail_send_claim"
    assert args["p_user_id"] == "owner"
    assert args["p_payload_sha256"] == "a" * 64
    assert args["p_effective_recipients"] == ["to@example.com"]


def test_no_owner_refused_before_database(client):
    with pytest.raises(ValueError):
        repo.mailbox_access_claim("", "a", "r", "x@example.com", "hash")
    assert not client.calls


def test_review_outcome_not_mutation(client):
    repo.mailbox_access_outcome("owner", "access", "succeeded", ["message"])
    name, args = client.calls[-1]
    assert name == "assistant_mailbox_access_outcome"
    assert args["p_access_id"] == "access"
    assert args["p_message_ids"] == ["message"]


def test_calendar_atomic_claim_and_admin_limits(client):
    assert repo.calendar_reminder_claim("owner", "hash", "2026-10-27T20:00:00Z", 15)
    assert client.calls[-1][0] == "assistant_calendar_reminder_claim"
    assert repo.mailbox_access_list("admin", limit=10000) == []
    assert client.calls[-1][1]["p_limit"] == 200


def test_audit_cursor_keeps_tied_timestamp_id(client):
    value = "2026-10-27T20:00:00Z|00000000-0000-0000-0000-000000000001"
    repo.mailbox_access_list("admin", before=value)
    name, args = client.calls[-1]
    assert name == "assistant_mailbox_access_page"
    assert args["p_before"] == "2026-10-27T20:00:00+00:00"
    assert args["p_before_id"] == value.split("|")[1]
    with pytest.raises(ValueError):
        repo.mailbox_access_list("admin", before="bad|not-a-uuid")


def test_access_page_enriches_only_authorized_ids(monkeypatch):
    page = [{"id": "approved-one", "user_id": "reader"}, {"id": "approved-two", "user_id": "other"}]
    monkeypatch.setattr(repo, "_mail_rpc", lambda *args, **kwargs: {"ok": True, "rows": page})
    calls = []

    class OutcomeQuery:
        def table(self, name):
            calls.append(("table", name))
            return self

        def select(self, columns):
            calls.append(("select", columns))
            return self

        def in_(self, column, values):
            calls.append(("in", column, values))
            return self

        def limit(self, value):
            calls.append(("limit", value))
            return self

        def execute(self):
            return SimpleNamespace(data=[{"access_id": "approved-one", "status": "succeeded"}])

    monkeypatch.setattr(repo, "_sb", OutcomeQuery)
    rows = repo.mailbox_access_list("admin")
    assert rows == [{**page[0], "outcome": "succeeded"}, {**page[1], "outcome": None}]
    assert ("select", "access_id,status") in calls
    assert ("in", "access_id", ["approved-one", "approved-two"]) in calls
    assert ("limit", 2) in calls


def test_denied_access_page_never_reads_outcomes(monkeypatch):
    monkeypatch.setattr(repo, "_mail_rpc", lambda *args, **kwargs: {"ok": False, "code": "forbidden"})
    monkeypatch.setattr(repo, "_sb", lambda: pytest.fail("outcome read after denial"))
    with pytest.raises(ValueError, match="access log unavailable"):
        repo.mailbox_access_list("not-admin")
