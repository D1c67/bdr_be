"""Deterministic mailbox-review notices, weekly summaries and exclusion audit.

No model, Graph or outgoing email. Each delivery's durable claim and inbox/bell
insert are one database transaction. Readers never see email bodies or reasons.
"""
from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from app.assistant import repo
from app.core.config import get_settings

PACIFIC = ZoneInfo("America/Los_Angeles")


def _week(now: datetime) -> datetime:
    local = now.astimezone(PACIFIC)
    return (local - timedelta(days=local.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)


def first_target_notice(user_id: str, target_upn: str, *, now: datetime | None = None) -> int:
    """Notify Executive/IT Admin reviewers once per target/Pacific week.

    The deployment has no distinct reviewer-owner config; all active reviewers
    receive the notice, ensuring an IT Admin is not the only reviewer of self.
    Call only after a successful durable sensitive-read claim.
    """
    now = now or datetime.now(timezone.utc)
    target = str(target_upn).strip().lower()
    digest = hashlib.sha256(target.encode()).hexdigest()
    key = f"mailbox-first:{_week(now).date()}:{digest}"
    count = 0
    for reviewer in repo.mail_reviewers(None):
        count += int(repo.mail_notice_deliver(
            str(reviewer["id"]), key, "A mailbox was opened for assistant review.",
            "First review of this mailbox this Pacific week.", {"target_upn": target, "reader_user_id": user_id},
        ))
    return count


def tick(*, now: datetime | None = None) -> dict:
    """Maintenance rider. Repeated calls are safe across workers and restarts."""
    settings = get_settings()
    if not getattr(settings, "assistant_enabled", False):
        return {"enabled": False}
    now = now or datetime.now(timezone.utc)
    changed = repo.mail_exclusions_sync(None, sorted(settings.mailbox_exclusion_set))
    end = _week(now)
    start = end - timedelta(days=7)
    delivered = 0
    for reviewer in repo.mail_reviewers(None):
        user_id = str(reviewer["id"])
        summary = repo.mail_audit_summary(user_id, start.isoformat(), end.isoformat())
        if not summary.get("ok"):
            continue
        counts = {name: int(summary.get(name) or 0) for name in ("accesses", "targets", "readers")}
        delivered += int(repo.mail_notice_deliver(
            user_id, f"mailbox-weekly:{start.date()}",
            f"Weekly mailbox review: {counts['accesses']} accesses.",
            "Review activity for the previous Pacific week; details are in the authorized access log.",
            {**counts, "start": start.isoformat(), "end": end.isoformat()},
        ))
    return {"enabled": True, "exclusions_changed": bool(changed.get("changed")), "digests": delivered}
