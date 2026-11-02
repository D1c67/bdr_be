"""Opt-in application calendar reads and deterministic reminders; no model calls."""
from __future__ import annotations

import asyncio
import hashlib
from datetime import datetime, timedelta, timezone
from urllib.parse import quote, urlsplit

from app.core.config import get_settings
from app.services import assistant_ms
from app.services.assistant_graph import GraphFailure, GraphTransport

POLL_SECONDS = 600
MAX_PAGES = 5


def _repo():
    from app.assistant import repo
    return repo


def configured() -> bool:
    return bool(getattr(get_settings(), "assistant_calendar_enabled", False) and assistant_ms.configured())


def preferences(user_id: str) -> dict:
    prefs = _repo().calendar_preferences_get(user_id)
    return {**prefs, "available": configured(),
            "unavailable_reason": None if configured() else "Calendar access is not enabled."}


def update_preferences(user_id: str, *, enabled: bool, mode: str, lead_minutes: int) -> dict:
    if (type(enabled) is not bool or mode not in {"context", "all"}
            or type(lead_minutes) is not int or not 5 <= lead_minutes <= 180):
        raise ValueError("Invalid calendar preferences.")
    if enabled:
        if not configured():
            raise ValueError("Calendar access is unavailable.")
        # An explicit opt-in performs a bounded capability check. Mail grants
        # alone do not establish calendar access; a 403 leaves prefs disabled.
        try:
            events(user_id, datetime.now(timezone.utc), horizon_minutes=1)
        except GraphFailure as error:
            if error.status_code in {401, 403}:
                _repo().calendar_preferences_set(user_id, enabled=False, mode=mode, lead_minutes=lead_minutes)
            raise
    saved = _repo().calendar_preferences_set(user_id, enabled=enabled, mode=mode, lead_minutes=lead_minutes)
    if saved.get("ok") is False:
        raise ValueError("Calendar preferences are unavailable.")
    return preferences(user_id)


def events(user_id: str, now: datetime, *, horizon_minutes: int = 180) -> list[dict]:
    if (not configured() or not isinstance(now, datetime) or now.tzinfo is None
            or type(horizon_minutes) is not int or not 1 <= horizon_minutes <= 180):
        raise ValueError("Calendar access is unavailable.")
    mailbox = assistant_ms.sender_mailbox(user_id)
    path = f"/v1.0/users/{quote(mailbox, safe='@')}/calendarView"
    params = {"startDateTime": now.isoformat(), "endDateTime": (now + timedelta(minutes=horizon_minutes)).isoformat(),
              "$select": "id,start,end,isCancelled,isAllDay,sensitivity,organizer,attendees", "$top": "50"}
    result = []
    next_path = path
    seen = set()
    for _ in range(MAX_PAGES):
        # Recheck primary mapping and BDR authority before each page.
        if assistant_ms.sender_mailbox(user_id) != mailbox:
            raise ValueError("Calendar access is unavailable.")
        _safe, token = assistant_ms.for_mailbox(user_id, mailbox)
        if next_path in seen:
            raise GraphFailure()
        seen.add(next_path)
        page = GraphTransport(token, reader=True).read_json(next_path, params=params)
        rows = page.get("value")
        if not isinstance(rows, list):
            raise GraphFailure()
        result.extend(row for row in rows[:50] if isinstance(row, dict))
        next_path = page.get("@odata.nextLink")
        if not next_path:
            return result[:250]
        parts = urlsplit(next_path)
        if parts.scheme != "https" or parts.netloc != "graph.microsoft.com" or parts.path != path or parts.fragment:
            raise GraphFailure()
        params = None
    return result[:250]


def starts_at(event: dict) -> datetime | None:
    start = event.get("start") or {}
    try:
        value = datetime.fromisoformat(str(start.get("dateTime") or "").replace("Z", "+00:00"))
        # Graph defaults to UTC when Prefer is absent. Reject unexpected zones
        # instead of guessing a local time at a DST transition.
        if value.tzinfo is None:
            if start.get("timeZone") not in {"UTC", "Etc/UTC"}:
                return None
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)
    except (ValueError, TypeError):
        return None


def context_match(event: dict, candidates: list[dict]) -> str | None:
    from app.assistant.mail_scope import normalize_address
    people = [event.get("organizer") or {}, *(event.get("attendees") or [])]
    addresses = {normalize_address((p.get("emailAddress") or {}).get("address"))
                 for p in people if isinstance(p, dict)} - {None}
    # Candidates are server-authorized projects with a bid due in seven days.
    matches = [str(p["id"]) for p in candidates if p.get("id") and addresses.intersection(
        normalize_address(x) for x in p.get("contact_emails", []))]
    return sorted(matches)[0] if matches else None


def tick(now: datetime | None = None) -> dict:
    if not configured():
        return {"reminders": 0, "unavailable": 0}
    now = now or datetime.now(timezone.utc)
    counts = {"reminders": 0, "unavailable": 0}
    for pref in _repo().calendar_owners(None, limit=100):
        uid = str(pref["user_id"])
        try:
            live = _repo().calendar_preferences_get(uid)
            if not live.get("enabled"):
                continue
            lead = int(live.get("lead_minutes", 15))
            if not 5 <= lead <= 180 or live.get("mode") not in {"context", "all"}:
                raise ValueError("Invalid calendar preferences.")
            expected_email = assistant_ms.sender_mailbox(uid)
            candidates = _repo().calendar_context_candidates(uid) if live.get("mode") == "context" else []
            for event in events(uid, now):
                start = starts_at(event)
                if (not start or event.get("isCancelled") or event.get("isAllDay") or not event.get("id")
                        or event.get("sensitivity") in {"private", "confidential"}):
                    continue
                if not now <= start <= now + timedelta(minutes=lead):
                    continue
                project = context_match(event, candidates) if live.get("mode") == "context" else None
                if live.get("mode") == "context" and project is None:
                    continue
                # No title/body/attendee data persists in unattended reminders.
                key = hashlib.sha256(str(event["id"]).encode()).hexdigest()
                if _repo().calendar_reminder_claim(uid, key, start.isoformat(), lead,
                                                   expected_mode=live["mode"], expected_email=expected_email):
                    counts["reminders"] += 1
        except GraphFailure as error:
            counts["unavailable"] += 1
            if error.status_code in {401, 403}:
                try:
                    _repo().calendar_preferences_set(uid, enabled=False, mode=live["mode"],
                                                      lead_minutes=live["lead_minutes"])
                except Exception:
                    pass
        except (assistant_ms.OutlookError, ValueError):
            counts["unavailable"] += 1
        except Exception:
            # One user's failure does not prevent reminders for other owners.
            counts["unavailable"] += 1
    return counts


async def polling_loop() -> None:
    while True:
        await asyncio.to_thread(tick)
        await asyncio.sleep(POLL_SECONDS)
