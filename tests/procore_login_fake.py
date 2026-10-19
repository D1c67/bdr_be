"""Python twins of the 0157 shared-login RPCs for the in-memory FakeDBs.

`procore_reserve_login` and `procore_record_login_failure` act on the
rfp_harvest_sessions row of one provider exactly as the SQL in
supabase/migrations/0157_browser_agents.sql does (insert the row when
missing; one conditional update), so every FakeDB that mounts them runs the
same contract. `now` is the fake database clock.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any


def _ts(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _row(rows: list[dict], provider: str) -> dict:
    for r in rows:
        if r.get("provider") == provider:
            return r
    row = {"provider": provider, "account": None, "cookies": None, "logged_in_at": None,
           "last_used_at": None, "last_login_attempt_at": None, "login_failures": 0,
           "locked_until": None, "last_error": None, "last_login_source": None}
    rows.append(row)
    return row


def reserve_login(rows: list[dict], now: datetime, p_provider: str, p_min_interval: int, p_source: str) -> dict:
    row = _row(rows, p_provider)
    interval = timedelta(seconds=max(int(p_min_interval), 0))
    locked = _ts(row.get("locked_until"))
    last = _ts(row.get("last_login_attempt_at"))
    if (locked is None or locked <= now) and (last is None or last < now - interval):
        row.update(last_login_attempt_at=now.isoformat(), last_login_source=p_source, updated_at=now.isoformat())
        return {"granted": True, "locked_until": None, "retry_at": None}
    if locked is not None and locked > now:
        return {"granted": False, "reason": "locked", "locked_until": locked.isoformat(),
                "retry_at": locked.isoformat()}
    return {"granted": False, "reason": "spacing", "locked_until": None,
            "retry_at": (last + interval).isoformat()}


def record_login_failure(rows: list[dict], now: datetime, p_provider: str, p_error: str,
                         p_max_failures: int, p_lock_seconds: int) -> dict:
    row = _row(rows, p_provider)
    failures = int(row.get("login_failures") or 0) + 1
    row["login_failures"] = failures
    row["last_error"] = (p_error or "")[:500]
    if failures >= p_max_failures:
        row["locked_until"] = (now + timedelta(seconds=p_lock_seconds)).isoformat()
    row["updated_at"] = now.isoformat()
    return dict(row)


def dispatch(rows: list[dict], now: datetime, name: str, params: dict) -> Any:
    """The RPC by name, or raise KeyError when it is not one of these two."""
    if name == "procore_reserve_login":
        return reserve_login(rows, now, **params)
    if name == "procore_record_login_failure":
        return record_login_failure(rows, now, **params)
    raise KeyError(name)
