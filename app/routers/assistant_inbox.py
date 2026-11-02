"""Phase 7 proactive router: inbox, alerts and admin routes
(docs/ASSISTANT_PHASE7_PLAN.md 9 and 10.1).

Every route is a plain `def`, sits behind `require_internal` and refuses the
turn principal (the model has no tool for any button; the firewall deny-lists
every pair too). Two gates:

  user routes   everything under /assistant/inbox and /assistant/alerts except
                the admin routes: 404 (code inbox_off) unless
                `features_flag(user)`, which also narrows by the pilot
                allowlist;
  admin routes  /assistant/alerts/company, /assistant/alerts/defaults* and
                /assistant/inbox/feedback-report: Executive or IT Admin plus
                `proactive_admin_flag(user)`, which never reads the company
                switch or the allowlist, so the route that turns the switch
                back on stays reachable.

Every admin write (switch, role default, reset) writes its audit row first, ids
and enums only. Assistant tables are reached only through
app/assistant/repo.py. The one request handler allowed a `None` system read is
`feedback_report` (9.3); the cursor reseed on an off to on flip goes through
`assistant_proactive.reseed_tails`, which owns the system call and counts the
skipped rows (10.1, 4.1). Errors use the
`_error(status, code, message)` shape of assistant_routines.py.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field

from app.assistant import access, proactive, repo
from app.assistant.tools._common import is_uuid
from app.core.config import get_settings
from app.core.deps import CurrentUser, require_internal
from app.core.features import SubApp, enabled_map
from app.core.roles import INTERNAL_ROLES, Role
from app.routers.assistant import _detail_error, _parse_ts, _project_refs, _refuse_assistant_principal

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/assistant", tags=["assistant"], dependencies=[Depends(require_internal)])



async def require_inbox_admin(user: CurrentUser = Depends(require_internal)) -> CurrentUser:
    """Executive or IT Admin. With ASSISTANT_PROACTIVE_ENABLED off the admin
    routes answer the flag-off 404 for every role, before the role check."""
    if not getattr(get_settings(), "assistant_proactive_enabled", False):
        raise _error(status.HTTP_404_NOT_FOUND, INBOX_OFF)
    if user.role not in (Role.EXECUTIVE, Role.IT_ADMIN):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Insufficient role")
    return user


# Error codes (10.1).
NOT_FOUND = "not_found"
NOT_MUTABLE = "not_mutable"
SNOOZE_LIMIT = "snooze_limit"
INVALID_FEEDBACK = "invalid_feedback"
INVALID_MODE = "invalid_mode"
UNKNOWN_KIND = "unknown_kind"
FORBIDDEN = "forbidden"
INBOX_OFF = "inbox_off"
INVALID_ROLE = "invalid_role"
INVALID_CURSOR = "invalid_cursor"

_MESSAGES = {
    NOT_FOUND: "That item was not found.",
    NOT_MUTABLE: "That change does not apply to this item.",
    SNOOZE_LIMIT: "This item was already moved to the next digest three times.",
    INVALID_FEEDBACK: "That feedback is not one of the choices.",
    INVALID_MODE: "That mode is not one of the choices.",
    UNKNOWN_KIND: "That kind of item was not found.",
    FORBIDDEN: "You cannot do that.",
    INBOX_OFF: "Not Found",
    INVALID_ROLE: "That role was not found.",
    INVALID_CURSOR: "That page marker is not one this inbox gave.",
}
_CODE_STATUS = {
    NOT_FOUND: status.HTTP_404_NOT_FOUND,
    UNKNOWN_KIND: status.HTTP_404_NOT_FOUND,
    NOT_MUTABLE: status.HTTP_409_CONFLICT,
    SNOOZE_LIMIT: status.HTTP_409_CONFLICT,
    INVALID_FEEDBACK: status.HTTP_422_UNPROCESSABLE_CONTENT,
    INVALID_MODE: status.HTTP_422_UNPROCESSABLE_CONTENT,
    INVALID_ROLE: status.HTTP_422_UNPROCESSABLE_CONTENT,
    INVALID_CURSOR: status.HTTP_422_UNPROCESSABLE_CONTENT,
}

LIST_MAX = 50
IDS_MAX = 200
SCAN_FETCHES = 4
UNREAD_CANDIDATES = 1000
SNOOZE_MAX = 3
REPORT_DAYS_MAX = 90
CURSOR_SEP = "|"
SWITCH_WORKER = "admin_switch"
SWITCH_AUDIT = "assistant.proactive_switch"
DEFAULT_UPDATE_AUDIT = "assistant.alert_default.update"
DEFAULT_RESET_AUDIT = "assistant.alert_default.reset"
AUDIT_ENTITY = "assistant_subscriptions"
SETTINGS_ENTITY = "assistant_settings"

# Kinds the phase 5 and 6 writers put straight in the inbox (delivery direct).
DIRECT_LABELS = {
    "assistant.calendar": "Calendar reminder",
    "assistant.mailbox_access": "Mailbox notice",
    "assistant.browser": "Browser task",
}
# The modes a person may pick per kind (a record kind never pushes, so any
# non-off pick is stored as inbox only).
ROUTER_MODES = ("alert", "digest", "inbox", "off")
RECORD_MODES = ("inbox", "off")
OTHER_LABEL = "Assistant notice"
OTHER_GROUP = "Assistant"

_DEFAULT_HEALTH: dict[str, Any] = {
    "events_pending": 0, "events_failed_7d": 0, "events_lease_exhausted_7d": 0, "slot_failures_7d": 0,
    "workers": [], "cursors": [], "sends_7d": [],
}


def _now() -> datetime:
    """The route clock (one seam for the tests)."""
    return datetime.now(timezone.utc)


def _error(status_code: int, code: str, message: str | None = None, **detail: Any) -> HTTPException:
    return _detail_error(status_code, code, message or _MESSAGES.get(code, "That request was refused."), **detail)


def _coded(code: str) -> HTTPException:
    return _error(_CODE_STATUS.get(code, status.HTTP_422_UNPROCESSABLE_CONTENT), code)


# -- The two feature flags (GET /features) ------------------------------------


def _allowlist() -> list[str]:
    raw = getattr(get_settings(), "assistant_proactive_user_allowlist", "") or ""
    return [part.strip().lower() for part in raw.split(",") if part.strip()]


def _on_allowlist(user: CurrentUser) -> bool:
    """True with no allowlist set; otherwise the caller's profile id or email
    must be listed (a pilot narrows exposure as well as delivery, 10.1)."""
    entries = _allowlist()
    if not entries:
        return True
    return str(user.id).lower() in entries or str(getattr(user, "email", "") or "").lower() in entries


def features_flag(user: CurrentUser) -> bool:
    """The `assistant_inbox` key of GET /features (10.1): the assistant is on
    for the caller and ASSISTANT_PROACTIVE_ENABLED and
    assistant_settings.proactive_enabled and, when
    ASSISTANT_PROACTIVE_USER_ALLOWLIST is set, the caller is on it. Fails
    closed on any read error."""
    if not getattr(get_settings(), "assistant_proactive_enabled", False):
        return False
    if user is None or user.role not in INTERNAL_ROLES:
        return False
    try:
        settings_row = repo.settings_row(user.id)
        if not access.assistant_enabled_for(user, settings_row, repo.profile_row(user.id)):
            return False
        if not bool((settings_row or {}).get("proactive_enabled")):
            return False
        return _on_allowlist(user)
    except Exception:  # noqa: BLE001 - a flag read must never break the shell
        logger.exception("assistant inbox features flag: read failed")
        return False


def proactive_admin_flag(user: CurrentUser) -> bool:
    """The `assistant_proactive_admin` key of GET /features (10.1): the caller
    is Executive or IT Admin and the assistant is on for them and
    ASSISTANT_PROACTIVE_ENABLED is on. Never reads the DB switch or the
    allowlist, so the company switch can be turned back on. Fails closed."""
    if not getattr(get_settings(), "assistant_proactive_enabled", False):
        return False
    if user is None or user.role not in (Role.EXECUTIVE, Role.IT_ADMIN):
        return False
    try:
        return access.assistant_enabled_for(user, repo.settings_row(user.id), repo.profile_row(user.id))
    except Exception:  # noqa: BLE001 - a flag read must never break the shell
        logger.exception("assistant proactive admin flag: read failed")
        return False


def _user_gate(user: CurrentUser) -> None:
    _refuse_assistant_principal(user)
    if not features_flag(user):
        raise _error(status.HTTP_404_NOT_FOUND, INBOX_OFF)


def _admin_gate(user: CurrentUser) -> None:
    _refuse_assistant_principal(user)
    if not proactive_admin_flag(user):
        raise _error(status.HTTP_404_NOT_FOUND, INBOX_OFF)


def _audit(user: CurrentUser, action: str, payload: dict[str, Any]) -> None:
    """The audit row an admin write leads with. Not best effort: a write the
    log cannot record does not happen."""
    from app.services import notifications

    notifications.audit(user.id, action, AUDIT_ENTITY if action != SWITCH_AUDIT else SETTINGS_ENTITY, None, payload)


# -- Inbox rows ---------------------------------------------------------------


def _catalog(event_type: str | None) -> dict[str, Any] | None:
    return proactive.EVENT_TYPES.get(event_type or "")


def _kind_label(event_type: str | None) -> str:
    entry = _catalog(event_type)
    if entry is not None:
        return entry["label"]
    return DIRECT_LABELS.get(event_type or "", OTHER_LABEL)


def _kind_group(event_type: str | None) -> str:
    entry = _catalog(event_type)
    return entry["group"] if entry is not None else OTHER_GROUP


def _is_router_owned(row: dict) -> bool:
    return row.get("delivery") in ("immediate", "digest")


def _bell_ids(rows: list[dict]) -> list[str]:
    out: list[str] = []
    for row in rows:
        key = "notification_id" if _is_router_owned(row) else "source_notification_id"
        if row.get(key):
            out.append(str(row[key]))
    return out


def _bell_states(user: CurrentUser, rows: list[dict]) -> dict[str, dict]:
    ids = _bell_ids(rows)
    return repo.notification_states(user.id, ids) if ids else {}


def _hidden(row: dict, states: dict[str, dict]) -> bool:
    """A record row whose source BDR bell was dismissed is left out (10.1)."""
    if row.get("delivery") != "record" or not row.get("source_notification_id"):
        return False
    state = states.get(str(row["source_notification_id"]))
    return bool(state and state.get("dismissed_at"))


def _read(row: dict, states: dict[str, dict]) -> bool:
    """Router rows: read_at, or the router's own bell read. Record rows: the
    source bell's read_at (a bell that is gone cannot be unread). Direct rows
    are read in their own bell, so always true (10.1)."""
    delivery = row.get("delivery")
    if delivery == "direct":
        return True
    if delivery == "record":
        sid = row.get("source_notification_id")
        state = states.get(str(sid)) if sid else None
        return True if state is None else bool(state.get("read_at"))
    if row.get("read_at"):
        return True
    nid = row.get("notification_id")
    state = states.get(str(nid)) if nid else None
    return bool(state and state.get("read_at"))


def _actions(row: dict) -> list[str]:
    """Which footer buttons apply (9.1). Direct rows offer Useful, Not useful
    and Dismiss only; record rows add Mute this kind; delivered router rows
    add Remind me (at most three times); a row with a project offers Mute
    this project."""
    if row.get("delivery") == "direct":
        return ["useful", "not_useful", "dismiss"]
    out = ["useful", "not_useful"]
    et = row.get("event_type")
    if et and (et == proactive.APPROVAL_EXPIRING or not str(et).startswith("assistant.")):
        out.append("mute")
    if row.get("project_id"):
        out.append("mute_project")
    if _is_router_owned(row) and row.get("delivered_at") and int(row.get("snooze_count") or 0) < SNOOZE_MAX:
        out.append("snooze")
    out.append("dismiss")
    return out


def _item(row: dict, states: dict[str, dict], projects: dict[str, dict], role: Any = None) -> dict[str, Any]:
    pid = str(row["project_id"]) if row.get("project_id") else None
    # A due line reads for the Pacific day it was delivered on (a snoozed row
    # returning in a later digest says "overdue", S9-3), as in the email.
    title, why = proactive.digest_text(row, row.get("delivered_at"), role)
    return {
        "id": str(row["id"]),
        "event_type": row.get("event_type"),
        "kind_label": _kind_label(row.get("event_type")),
        "group": _kind_group(row.get("event_type")),
        "severity": row.get("severity"),
        "title": title,
        "why": why,
        "delivery": row.get("delivery"),
        "precedence": row.get("precedence"),
        "project": projects.get(pid) if pid else None,
        "digest_id": str(row["digest_id"]) if row.get("digest_id") else None,
        "created_at": row.get("created_at"),
        "delivered_at": row.get("delivered_at"),
        "read": _read(row, states),
        "feedback": row.get("feedback"),
        "actions": _actions(row),
    }


def _items(user: CurrentUser, rows: list[dict], states: dict[str, dict] | None = None) -> list[dict[str, Any]]:
    states = _bell_states(user, rows) if states is None else states
    projects = _project_refs(user, [r.get("project_id") for r in rows])
    return [_item(r, states, projects, user.role) for r in rows]


def _one_item(user: CurrentUser, inbox_id: str) -> dict[str, Any]:
    row = repo.inbox_row(user.id, inbox_id)
    if row is None:
        raise _coded(NOT_FOUND)
    return _items(user, [row])[0]


def _digest_out(row: dict, notices: list[str] | None = None) -> dict[str, Any]:
    return {
        "id": str(row["id"]),
        "slot": row.get("slot"),
        "digest_date": str(row.get("digest_date")),
        "slot_at": row.get("slot_at"),
        "since_at": row.get("since_at"),
        "item_count": int(row.get("item_count") or 0),
        "record_count": int(row.get("record_count") or 0),
        "created_at": row.get("created_at"),
        # The fixed auto-downgrade lines this digest carries (9.1), the same
        # as its email; the summary's last_digest leaves them out.
        "notices": list(notices or []),
    }


def _notices(user: CurrentUser, row: dict) -> list[str]:
    return proactive.downgrade_notices(repo.digest_auto_downgrades(user.id, row))


def _parse_cursor(value: str | None) -> tuple[str, str] | None:
    if value is None or not value.strip():
        return None
    stamp, sep, ident = value.strip().partition(CURSOR_SEP)
    parsed = _parse_ts(stamp)
    if not sep or parsed is None or not is_uuid(ident):
        raise _coded(INVALID_CURSOR)
    return parsed.astimezone(timezone.utc).isoformat(), str(uuid.UUID(ident))


def _cursor_of(row: dict) -> str:
    """"<UTC time>|<id>", with a Z suffix so no character needs escaping in a
    query string (a bare plus sign would read back as a space)."""
    stamp = _parse_ts(row["created_at"])
    text = stamp.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ") if stamp else str(row["created_at"])
    return f"{text}{CURSOR_SEP}{row['id']}"


def _page(user: CurrentUser, segment: str, cursor: tuple[str, str] | None, limit: int) -> tuple[list[dict], dict, str | None]:
    """Up to `limit` visible rows, newest first, the bell states they were
    judged on and the cursor of the next page. Hidden rows (a record row whose
    source bell was dismissed) and, in the unread segment, router rows whose
    own bell was read are skipped; the scan is bounded, and a bounded scan that
    ends short still hands out a cursor past what it covered."""
    batch = min(201, limit * 3 + 1)
    out: list[dict] = []
    states: dict[str, dict] = {}
    last: dict | None = None
    key = cursor
    exhausted = False
    more = False
    for _ in range(SCAN_FETCHES):
        rows = repo.inbox_list(user.id, segment=segment, before=key, limit=batch)
        batch_states = _bell_states(user, rows)
        states.update(batch_states)
        for row in rows:
            if _hidden(row, batch_states) or (segment == "unread" and _read(row, batch_states)):
                last = row
                continue
            if len(out) == limit:
                more = True
                break
            out.append(row)
            last = row
        if more:
            break
        if len(rows) < batch:
            exhausted = True
            break
        key = (str(rows[-1]["created_at"]), str(rows[-1]["id"]))
    if not more and not exhausted:
        more = True
    return out, states, (_cursor_of(last) if more and last is not None else None)


@router.get("/inbox")
def list_inbox(
    segment: str = Query(default="unread", pattern="^(unread|all|digests)$"),
    before: str | None = Query(default=None, max_length=96),
    limit: int = Query(default=20, ge=1, le=LIST_MAX),
    user: CurrentUser = Depends(require_internal),
) -> dict[str, Any]:
    _user_gate(user)
    rows, states, nxt = _page(user, segment, _parse_cursor(before), limit)
    out: dict[str, Any] = {"items": _items(user, rows, states), "next": nxt}
    if segment == "digests":
        seen: dict[str, dict] = {}
        for row in rows:
            did = str(row["digest_id"]) if row.get("digest_id") else None
            if did and did not in seen:
                found = repo.digest(user.id, did)
                if found is not None:
                    seen[did] = _digest_out(found, _notices(user, found))
        out["digests"] = list(seen.values())
    return out


@router.get("/inbox/summary")
def inbox_summary(user: CurrentUser = Depends(require_internal)) -> dict[str, Any]:
    """The badge numbers: router-owned unread rows only (record and direct
    rows never count)."""
    _user_gate(user)
    rows = repo.inbox_unread_rows(user.id, limit=UNREAD_CANDIDATES)
    nids = [str(r["notification_id"]) for r in rows if r.get("notification_id")]
    states = repo.notification_states(user.id, nids) if nids else {}
    unread = [r for r in rows if not (r.get("notification_id") and (states.get(str(r["notification_id"])) or {}).get("read_at"))]
    latest = repo.digests_recent(user.id, limit=1)
    return {
        "unread": len(unread),
        "high_unread": sum(1 for r in unread if r.get("severity") in ("high", "urgent")),
        "last_digest": _digest_out(latest[0]) if latest else None,
    }


@router.get("/inbox/digests/{digest_id}")
def inbox_digest(digest_id: str, user: CurrentUser = Depends(require_internal)) -> dict[str, Any]:
    _user_gate(user)
    if not is_uuid(digest_id):
        raise _coded(NOT_FOUND)
    found = repo.digest(user.id, digest_id)
    if found is None:
        raise _coded(NOT_FOUND)
    rows = [r for r in repo.inbox_digest_items(user.id, str(found["id"])) if not r.get("dismissed_at")]
    return {"digest": _digest_out(found, _notices(user, found)), "items": _items(user, rows)}


class IdsIn(BaseModel):
    ids: list[str] | None = Field(default=None, max_length=IDS_MAX)


class DismissIn(BaseModel):
    ids: list[str] = Field(default_factory=list, max_length=IDS_MAX)


class FeedbackIn(BaseModel):
    value: str = Field(max_length=32)


@router.post("/inbox/read")
def inbox_read(body: IdsIn, user: CurrentUser = Depends(require_internal)) -> dict[str, int]:
    """Mark router-owned delivered rows (and their own bells) read; `ids`
    null is every such row. A record row's source bell is never written."""
    _user_gate(user)
    return {"updated": repo.inbox_mark_read(user.id, body.ids, now=_now())}


@router.post("/inbox/dismiss")
def inbox_dismiss(body: DismissIn, user: CurrentUser = Depends(require_internal)) -> dict[str, int]:
    _user_gate(user)
    return {"updated": repo.inbox_dismiss(user.id, body.ids, "user", now=_now())}


@router.post("/inbox/{inbox_id}/feedback")
def inbox_feedback(inbox_id: str, body: FeedbackIn, user: CurrentUser = Depends(require_internal)) -> dict[str, Any]:
    _user_gate(user)
    if not is_uuid(inbox_id):
        raise _coded(NOT_FOUND)
    if body.value not in repo.FEEDBACK_VALUES:
        raise _coded(INVALID_FEEDBACK)
    result = repo.inbox_feedback(user.id, inbox_id, body.value, now=_now())
    if not result.get("ok"):
        raise _coded(str(result.get("code") or NOT_FOUND))
    return {"item": _one_item(user, inbox_id), "auto_downgraded": bool(result.get("auto_downgraded"))}


def _holidays(user: CurrentUser) -> list[Any]:
    return list((repo.settings_row(user.id) or {}).get("holidays") or [])


def _slot_times() -> dict[str, str]:
    """{"am": "07:00", "pm": "13:00"} from ASSISTANT_PROACTIVE_DIGEST_TIMES
    (the config validator guarantees exactly two)."""
    raw = [p.strip() for p in str(getattr(get_settings(), "assistant_proactive_digest_times", "07:00,13:00")).split(",")]
    ordered = sorted((p for p in raw if p), key=lambda p: (int(p.split(":")[0]), int(p.split(":")[1])))
    if len(ordered) != 2:
        return {}
    return {"am": ordered[0], "pm": ordered[1]}


@router.post("/inbox/{inbox_id}/snooze")
def inbox_snooze(inbox_id: str, user: CurrentUser = Depends(require_internal)) -> dict[str, Any]:
    """Remind me next digest: the row returns, unread, in the next digest
    slot (a workday, holidays skipped), at most three times."""
    _user_gate(user)
    if not is_uuid(inbox_id):
        raise _coded(NOT_FOUND)
    now = _now()
    slot = proactive.next_slot(now, _holidays(user), slot_times=_slot_times() or None).astimezone(timezone.utc)
    result = repo.inbox_snooze(user.id, inbox_id, slot, now=now)
    if not result.get("ok"):
        raise _coded(str(result.get("code") or NOT_FOUND))
    return {"item": _one_item(user, inbox_id), "next_slot": slot.isoformat()}


# -- Alerts: the person's own modes -------------------------------------------


def _kind_visible(user: CurrentUser, event_type: str) -> bool:
    """A kind the caller's sub-app switches allow (Bidding off hides the bid
    and record kinds, PM off hides the pm bell)."""
    entry = _catalog(event_type)
    if entry is None:
        return False
    sub_app = entry.get("sub_app")
    if sub_app in {s.value for s in SubApp}:
        return bool(enabled_map(user.role).get(sub_app, False))
    return True


def _kind_row(user: CurrentUser, event_type: str, user_rows: dict[str, dict], role_rows: dict[str, dict]) -> dict[str, Any] | None:
    """One Alerts row, or None when the caller's role does not receive the
    kind. Record kinds are always inbox only or off (they never push); the
    row carries `kind` ("router" or "record") and `modes`, the picks the
    Select should offer."""
    entry = _catalog(event_type)
    if entry is None:
        return None
    user_row, role_row = user_rows.get(event_type), role_rows.get(event_type)
    base = proactive.effective_subscription(event_type, user.role, None, role_row)
    if base["source"] == "none":
        return None
    effective = proactive.effective_subscription(event_type, user.role, user_row, role_row)
    record = entry["kind"] == "record"

    def mode(sub: dict) -> str:
        if record and sub["enabled"]:
            return "inbox"
        return proactive.mode_for_severity(sub["severity"], sub["enabled"])

    override = None
    if user_row is not None:
        override = {"mode": mode({"enabled": bool(user_row.get("enabled")), "severity": user_row.get("severity")}),
                    "source": str(user_row.get("source") or "user")}
    return {
        "event_type": event_type,
        "group": entry["group"],
        "label": entry["label"],
        "description": entry["description"],
        "kind": entry["kind"],
        "modes": list(RECORD_MODES if record else ROUTER_MODES),
        "role_default": mode(base),
        "override": override,
        "effective_mode": mode(effective),
    }


def _sub_maps(user: CurrentUser) -> tuple[dict[str, dict], dict[str, dict]]:
    users = {str(r["event_type"]): r for r in repo.user_subscriptions(user.id)}
    roles = {str(r["event_type"]): r for r in repo.role_subscriptions(user.id, role=user.role.value)}
    return users, roles


def _kinds(user: CurrentUser) -> list[dict[str, Any]]:
    users, roles = _sub_maps(user)
    rows = (_kind_row(user, et, users, roles) for et in proactive.EVENT_TYPES if _kind_visible(user, et))
    return [r for r in rows if r is not None]


def _kind_or_404(user: CurrentUser, event_type: str) -> dict[str, Any]:
    users, roles = _sub_maps(user)
    row = _kind_row(user, event_type, users, roles) if _kind_visible(user, event_type) else None
    if row is None:
        raise _coded(UNKNOWN_KIND)
    return row


def _channels(user: CurrentUser) -> dict[str, Any]:
    """The read-only channel state, from assistant_delivery (which reads the
    Teams status without its error text). A failed read shows both channels as
    unavailable instead of failing the card."""
    out: dict[str, Any] = {
        "teams": {"available": False, "enabled": False, "connected": False, "last_sent_at": None, "last_error_at": None},
        "email": {"available": False},
    }
    try:
        from app.services import assistant_delivery

        found = assistant_delivery.channels(user.id) or {}
        teams = found.get("teams") or {}
        for key in out["teams"]:
            if key in teams:
                out["teams"][key] = teams[key]
        out["email"]["available"] = bool((found.get("email") or {}).get("available"))
    except Exception:  # noqa: BLE001 - the card still renders without channel state
        logger.exception("assistant alerts: channel state could not be read")
    external = bool(getattr(get_settings(), "assistant_proactive_external_enabled", False))
    out["teams"]["available"] = bool(out["teams"]["available"]) and external
    out["email"]["available"] = bool(out["email"]["available"]) and external
    return out


@router.get("/alerts")
def get_alerts(user: CurrentUser = Depends(require_internal)) -> dict[str, Any]:
    _user_gate(user)
    settings = get_settings()
    mutes = repo.project_mutes(user.id)
    refs = _project_refs(user, [m.get("project_id") for m in mutes])
    times = _slot_times()
    return {
        "quiet_hours": {
            "start": str(getattr(settings, "assistant_proactive_quiet_start", "18:00")),
            "end": str(getattr(settings, "assistant_proactive_quiet_end", "06:00")),
            "tz": "America/Los_Angeles",
        },
        "digest_times": [times[k] for k in ("am", "pm") if k in times],
        "channels": _channels(user),
        "kinds": _kinds(user),
        "project_mutes": [
            {"project_id": str(m["project_id"]), "number": (refs.get(str(m["project_id"])) or {}).get("number")}
            for m in mutes
        ],
    }


class ModeIn(BaseModel):
    mode: str = Field(max_length=16)


class CompanyIn(BaseModel):
    enabled: bool


# Declared above /alerts/{event_type}: Starlette takes the first full match.
@router.get("/alerts/company")
def get_company(user: CurrentUser = Depends(require_inbox_admin)) -> dict[str, bool]:
    """The company switch's saved state, so the admin block never has to infer
    it from features.assistant_inbox (an allowlist can hide a live switch)."""
    _admin_gate(user)
    return {"enabled": bool((repo.settings_row(user.id) or {}).get("proactive_enabled"))}


@router.put("/alerts/company")
def set_company(body: CompanyIn, user: CurrentUser = Depends(require_inbox_admin)) -> dict[str, bool]:
    """The company switch (assistant_settings.proactive_enabled). Reachable
    with the switch off. A flip from off to on reseeds the two tail cursors to
    now minus 2 minutes (4.1), best effort: the tails keep advancing while the
    switch is off, so the reseed only guards a long gap."""
    _admin_gate(user)
    was_on = bool((repo.settings_row(user.id) or {}).get("proactive_enabled"))
    enabled = bool(body.enabled)
    _audit(user, SWITCH_AUDIT, {"enabled": enabled})
    saved = repo.set_proactive_enabled(user.id, enabled)
    if enabled and not was_on:
        try:
            from app.services import assistant_proactive

            assistant_proactive.reseed_tails(_now(), worker=SWITCH_WORKER, reason="switch")
        except Exception:  # noqa: BLE001 - the switch is saved; the lag guard covers a stale cursor
            logger.exception("assistant proactive: cursor reseed failed")
    return {"enabled": bool(saved.get("proactive_enabled"))}


@router.get("/alerts/defaults")
def get_alert_defaults(
    role: str | None = Query(default=None, max_length=48),
    user: CurrentUser = Depends(require_inbox_admin),
) -> dict[str, Any]:
    _admin_gate(user)
    return _grid(user, _role_or_422(role) if role else None)


@router.put("/alerts/defaults/{role}/{event_type}")
def set_alert_default(role: str, event_type: str, body: ModeIn, user: CurrentUser = Depends(require_inbox_admin)) -> dict[str, Any]:
    """One role's default for one router kind. `urgent` is accepted for all
    three router kinds, assistant.approval_expiring included (owner question
    39, b); the audit row leads."""
    _admin_gate(user)
    target = _role_or_422(role)
    _router_kind_or_404(event_type)
    if body.mode not in proactive.ADMIN_MODES:
        raise _coded(INVALID_MODE)
    enabled = body.mode != "off"
    severity = proactive.severity_for_mode(body.mode) or _role_off_severity(target, event_type)
    _audit(user, DEFAULT_UPDATE_AUDIT, {"role": target.value, "event_type": event_type, "mode": body.mode})
    result = repo.role_subscription_set(user.id, target.value, event_type, enabled=enabled, severity=severity, actor=user.id)
    if not result.get("ok"):
        raise _coded(str(result.get("code") or INVALID_MODE))
    return _grid(user, target)


@router.post("/alerts/defaults/{role}/{event_type}/reset")
def reset_alert_default(role: str, event_type: str, user: CurrentUser = Depends(require_inbox_admin)) -> dict[str, Any]:
    _admin_gate(user)
    target = _role_or_422(role)
    _router_kind_or_404(event_type)
    _audit(user, DEFAULT_RESET_AUDIT, {"role": target.value, "event_type": event_type})
    repo.role_subscription_reset(user.id, target.value, event_type, actor=user.id)
    return _grid(user, target)


def _role_or_422(value: str) -> Role:
    try:
        role = Role(value)
    except ValueError:
        raise _coded(INVALID_ROLE) from None
    if role not in INTERNAL_ROLES:
        raise _coded(INVALID_ROLE)
    return role


def _router_kind_or_404(event_type: str) -> None:
    if event_type not in proactive.ROUTER_KINDS:
        raise _coded(UNKNOWN_KIND)


def _role_off_severity(role: Role, event_type: str) -> str:
    """The severity stored beside enabled = false: the code default when the
    role has one, else normal (the router reads only `enabled` of an off row)."""
    return proactive.default_severity(event_type, role) or "normal"


def _grid(admin: CurrentUser, only: Role | None = None) -> dict[str, Any]:
    """The role-defaults grid for the three router kinds (the Assistant column
    of Settings > Notifications, 10.2). A cell exists where the role is in the
    kind's default audience or an admin row already exists for it."""
    order = [r for r in Role if r in INTERNAL_ROLES]
    roles = [only] if only is not None else order
    rows = repo.role_subscriptions(admin.id, role=only.value if only is not None else None)
    by_key = {(str(r["role"]), str(r["event_type"])): r for r in rows}
    kinds: list[dict[str, Any]] = []
    for et in proactive.ROUTER_KINDS:
        entry = proactive.EVENT_TYPES[et]
        cells: dict[str, dict[str, str]] = {}
        for role in roles:
            row = by_key.get((role.value, et))
            default_sev = proactive.default_severity(et, role)
            if default_sev is None and row is None:
                continue
            default_mode = proactive.mode_for_severity(default_sev, True) if default_sev else "off"
            eff = proactive.effective_subscription(et, role, None, row)
            cells[role.value] = {"mode": proactive.mode_for_severity(eff["severity"], eff["enabled"]), "default_mode": default_mode}
        kinds.append({"event_type": et, "label": entry["label"], "group": entry["group"], "cells": cells})
    return {"roles": [r.value for r in roles], "kinds": kinds}



@router.put("/alerts/{event_type}")
def set_alert_mode(event_type: str, body: ModeIn, user: CurrentUser = Depends(require_internal)) -> dict[str, Any]:
    """The person's own mode for a kind. A user row beats the role default;
    urgent is an admin-only mode (invalid_mode here). Record kinds never push,
    so any non-off mode is stored as inbox only."""
    _user_gate(user)
    entry = _catalog(event_type)
    _kind_or_404(user, event_type)
    if body.mode not in proactive.USER_MODES:
        raise _coded(INVALID_MODE)
    enabled = body.mode != "off"
    severity = "normal" if not enabled else ("low" if entry["kind"] == "record" else proactive.severity_for_mode(body.mode))
    result = repo.subscription_set(user.id, event_type, enabled=enabled, severity=severity, source="user", actor=user.id)
    if not result.get("ok"):
        raise _coded(str(result.get("code") or INVALID_MODE))
    return _kind_or_404(user, event_type)


@router.post("/alerts/{event_type}/reset")
def reset_alert_mode(event_type: str, user: CurrentUser = Depends(require_internal)) -> dict[str, Any]:
    """Drop the person's own row: the role default applies again."""
    _user_gate(user)
    _kind_or_404(user, event_type)
    repo.subscription_reset(user.id, event_type, actor=user.id)
    return _kind_or_404(user, event_type)


@router.post("/alerts/mutes/{project_id}/remove")
def remove_project_mute(project_id: str, user: CurrentUser = Depends(require_internal)) -> dict[str, bool]:
    _user_gate(user)
    if not is_uuid(project_id):
        raise _coded(NOT_FOUND)
    return {"removed": repo.project_mute_remove(user.id, project_id)}


# -- Feedback report (9.3) ----------------------------------------------------


def _report_out(report: dict) -> dict[str, Any]:
    out = dict(report)
    out["health"] = {**_DEFAULT_HEALTH, **(report.get("health") or {})}
    out.setdefault("kinds", [])
    return out


@router.get("/inbox/feedback-report")
def feedback_report(
    days: int = Query(default=30, ge=1, le=REPORT_DAYS_MAX),
    user: CurrentUser = Depends(require_inbox_admin),
) -> dict[str, Any]:
    """Counts only: per kind and role, every cell under 3 distinct people
    suppressed, direct rows excluded, plus the router health block. The one
    request handler allowed a `None` system read (9.3)."""
    _admin_gate(user)
    now = _now()
    return _report_out(repo.inbox_feedback_report(None, now - timedelta(days=days), now=now))
