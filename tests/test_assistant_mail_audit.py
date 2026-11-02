from datetime import datetime, timezone
from types import SimpleNamespace

from app.services import assistant_mail_audit as audit


def test_weekly_digest_and_exclusion_change_are_deterministic(monkeypatch):
    monkeypatch.setattr(audit, "get_settings", lambda: SimpleNamespace(assistant_enabled=True, mailbox_exclusion_set={"x@example.com"}))
    monkeypatch.setattr(audit.repo, "mail_exclusions_sync", lambda owner, values: {"changed": values == ["x@example.com"]})
    monkeypatch.setattr(audit.repo, "mail_reviewers", lambda owner: [{"id": "reviewer"}])
    windows = []
    monkeypatch.setattr(audit.repo, "mail_audit_summary", lambda owner, start, end: windows.append((start, end)) or {"ok": True, "accesses": 3, "targets": 2, "readers": 1})
    sent = []
    monkeypatch.setattr(audit.repo, "mail_notice_deliver", lambda *args: sent.append(args) or True)
    result = audit.tick(now=datetime(2026, 11, 3, 18, tzinfo=timezone.utc))
    assert result["digests"] == 1
    assert windows == [("2026-10-26T00:00:00-07:00", "2026-11-02T00:00:00-08:00")]
    assert sent[0][1] == "mailbox-weekly:2026-10-26"
    assert set(sent[0][-1]) == {"accesses", "targets", "readers", "start", "end"}


def test_first_target_key_is_same_across_readers(monkeypatch):
    monkeypatch.setattr(audit.repo, "mail_reviewers", lambda owner: [{"id": "reviewer"}])
    keys = []
    monkeypatch.setattr(audit.repo, "mail_notice_deliver", lambda *args: keys.append(args[1]) or True)
    now = datetime(2026, 10, 27, 18, tzinfo=timezone.utc)
    audit.first_target_notice("one", "Person@example.com", now=now)
    audit.first_target_notice("two", "person@example.com", now=now)
    assert keys[0] == keys[1]


def test_disabled_does_not_query(monkeypatch):
    monkeypatch.setattr(audit, "get_settings", lambda: SimpleNamespace(assistant_enabled=False))
    assert audit.tick() == {"enabled": False}
