"""Phase 7 proactive router: the pure catalog and rules module
(docs/ASSISTANT_PHASE7_PLAN.md sections 1, 2, 4, 6.2, 6.3, 7, 8 and 9.2).

No I/O, no clock reads, no model, no Supabase: everything here is
deterministic and takes `now` (or the slot) as an argument. The router service
(`app/services/assistant_proactive.py`) reads the world, puts the answers in a
`facts` dict and calls in here; the delivery module (`assistant_delivery.py`)
uses the same templates for Teams and email text.

What lives here:
  EVENT_TYPES   the catalog (6.2 step 1): exactly three router-owned kinds
                (`bid.due_not_ready`, `todo.due`, `assistant.approval_expiring`),
                the `bell.<routing_event>` record kinds (4.2 allowlist) and
                `project.failed_to_bid`. Each has a sub-app, a payload
                allowlist, title and why templates, role defaults and, for
                bell kinds, the 0177 audience it can never widen.
  PRECEDENCE    the precedence table as data, evaluated in table order, first
                match wins (7). Ids: R4, R4b, R5 to R13, R16 to R26. There is
                no R1, R1a, R2, R3, R14, R15 or R15b (removed 2026-10-30).
  SEVERITY_TABLE, MODE_SEVERITY   severity to delivery and channel (6.3).
  clean_payload, email_key, bell_inbox_key   allowlists, taint, collapse (4).
  slot_instant, next_slot, previous_slot, due_window, in_quiet_hours,
  next_quiet_end, delivery_plan, fold_params   slots and quiet hours (8).
  render, digest_lines, teams_message, digest_subject   server text (4.4, 8.5).
  digest_text   a delivered row's title and why for the day it is shown on.
  downgrade_notices   the 9.1 auto-downgrade lines a digest carries.

Facts the service supplies to `render` and `evaluate_precedence` (all
optional; a missing key never matches a rule):
  now, slot_at, prev_slot_at, holidays, severity, project_number,
  teams_enabled, subscription_enabled, project_muted, project_closed,
  card_pending, card_sub_app, reminder_1h_logged_at, reminder_logged_at,
  missing_unchanged, reminder_1h_expected, routines, auto_send,
  call_in_overlap, nudged_today, bdr_external, bucket_key, delivery,
  bell_type, routine_recipe, due_digest_day, slot_times, quiet_start,
  quiet_end, catchup_hours.
`reminder_1h_expected` is True only when `effective_prefs` gives this user the
`internal_bid` reminder at offset 1h; R4 promises a 1-hour reminder only then.
`slot_times` (`{"am": "07:00", "pm": "13:00"}`), `quiet_start`, `quiet_end`
(`time` or `"HH:MM"`) and `catchup_hours` are the configured digest and
quiet-hours values; a missing one means the default.
The router never reads `due_digest_log` (owner question 8): a fact of that
name is not part of the contract and changes nothing.

Guiding-principle calls made here (the app owns notifications; the router must
not duplicate what BDR already sends):
  * Titles and whys carry counts, enums, Pacific times and the project number
    only. Quote categories are shown as a count, never by name, because
    category names are admin-typed text.
  * A digest-bound row belongs to the slot it was emitted for (`slot_at` fact
    or the payload's slot), not to `next_slot(now)`: a sweeper that routes its
    events a few seconds after 7:00 AM must still land them in the 7:00 AM
    digest. `next_slot(now)` is used when no slot is known.
  * Record rows are quiet by definition: severity low, delivery record, no
    Teams, no email, whatever a caller passes.
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from collections.abc import Iterable, Mapping
from datetime import date, datetime, time, timedelta, timezone
from typing import Any

from app.assistant.rrules import PT
from app.assistant.rrules import _holiday_set as _holidays
from app.core.roles import INTERNAL_ROLES, Role

UTC = timezone.utc

# ── constants ────────────────────────────────────────────────────────────────

BID_DUE = "bid.due_not_ready"
TODO_DUE = "todo.due"
APPROVAL_EXPIRING = "assistant.approval_expiring"
FAILED_TO_BID = "project.failed_to_bid"
ROUTER_KINDS = (BID_DUE, TODO_DUE, APPROVAL_EXPIRING)

SEVERITIES = ("low", "normal", "high", "urgent")
DELIVERIES = ("direct", "immediate", "digest", "record")
OUTCOMES = ("push", "record", "merge", "note", "suppress", "none")

# Company digest slots and quiet hours (8.1, 8.4). The config knobs
# (`ASSISTANT_PROACTIVE_DIGEST_TIMES`, `_QUIET_START`, `_QUIET_END`,
# `_DIGEST_CATCHUP_HOURS`) carry the same values; this module is pure and
# takes the defaults, with keyword overrides (`slot_times`, `quiet_start`,
# `quiet_end`, `catchup_hours`) on every slot and quiet-hours function and the
# same names as `render` facts, where a caller holds the config.
SLOT_TIMES: dict[str, time] = {"am": time(7, 0), "pm": time(13, 0)}
SLOT_ORDER = ("am", "pm")
CATCHUP_HOURS = 3
QUIET_START = time(18, 0)
QUIET_END = time(6, 0)

PAYLOAD_MAX_BYTES = 4096
STRING_MAX = 64
ISO_MAX = 40
LIST_MAX = 50
SEARCH_DAYS = 40

EMAIL_BUCKET_SECONDS = 120

_UUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
_SLUG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:\-]{0,63}$")
_HASH_RE = re.compile(r"^[0-9a-f]{64}$")
_NUMBER_RE = re.compile(r"^[0-9][0-9A-Za-z.\-]{0,23}$")
_DIGITS_RE = re.compile(r"^[0-9]{1,18}$")

# Payload keys may never name money (4.4); a test scans every allowlist.
FORBIDDEN_KEY_PARTS = ("amount", "price", "total", "actual_bid", "markup_amount", "labor_amount")

# The reasons of the failed-to-bid enum (app/services/failed_to_bid_email.py);
# only the code travels, never the typed note.
FAILED_TO_BID_REASONS = frozenset(
    {
        "missed_deadline",
        "late_plans",
        "missing_quotes",
        "estimate_not_finished",
        "capacity",
        "submission_problem",
        "other",
    }
)

# Fixed labels for the phase 3 recipes a routine can cover (R6). A routine's
# own title is user-visible text, so the router names the recipe instead.
RECIPE_LABELS: dict[str, str] = {
    "due_today": "Bids due today",
    "due_this_week": "Bids due this week",
    "missing_quotes": "Bids missing vendor quotes",
    "labor_not_entered": "Bids with no labor entered",
    "quote_readiness": "Key quotes for the bid week",
}

_EXE = Role.EXECUTIVE
_EA = Role.ESTIMATING_ADMIN
_LAB = Role.ESTIMATING_ENGINEER_LABOR
_MAT = Role.ESTIMATING_ENGINEER_MATERIALS
_IT = Role.IT_ADMIN
_ACC = Role.ACCOUNTANT
_ALL = frozenset({_EXE, _EA, _LAB, _MAT, _IT, _ACC})

_DAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")

# ── payload allowlist specs (4.1, 4.2, 4.4) ──────────────────────────────────
# A spec is a string or a tuple. Values that do not fit their spec are dropped,
# so a hostile string can never ride in under an allowed key.
_UUID = "uuid"
_ID = "id"
_SLUG = "slug"
_ISO = "iso"
_BOOL = "bool"
_INT = "int"
_HASH = "hash64"
_UUID_LIST = "uuid_list"


def _enum(*values: str) -> tuple:
    return ("enum", frozenset(values))


_BID_PAYLOAD = {
    "internal_bid_at": _ISO,
    "slot": _enum("am", "pm"),
    "slot_date": _ISO,
    "missing_quote_category_ids": _UUID_LIST,
    "labor_missing": _BOOL,
    "markups_missing": _BOOL,
    "readiness": _enum("red", "yellow"),
}
_TODO_PAYLOAD = {"todo_id": _ID, "due": _ISO, "overdue": _BOOL}
_APPROVAL_PAYLOAD = {"approval_id": _UUID, "expires_at": _ISO, "action_kind": _SLUG}
_BELL_PAYLOAD = {
    "notification_id": _UUID,
    "notification_type": _SLUG,
    "routing_event": _SLUG,
    "rfq_id": _UUID,
    "stage": _SLUG,
    "round": _INT,
    "entry_id": _ID,
    "gc_id": _ID,
    "request_id": _ID,
    "revision_id": _ID,
    "approval_id": _ID,
    "rfq_message_id": _UUID,
    "email_key": _HASH,
}
_FAILED_PAYLOAD = {"audit_id": _ID, "reason": ("enum", FAILED_TO_BID_REASONS)}

# ── the bell mirror allowlist (4.2) ──────────────────────────────────────────
# routing_event -> (label, group, audience). Labels, groups and audiences are
# copies of the 0177 grid (notification_routing.EVENTS); a test pins the copy.
_BELL_EVENTS: dict[str, tuple[str, str, frozenset[Role]]] = {
    "go_no_go": ("Waiting on Go/No-Go", "Bidding", frozenset({_EXE, _IT})),
    "intake_needed": ("Intake details needed", "Bidding", frozenset({_EA})),
    "rfp_review": ("RFP emails to review", "Bidding", frozenset({_EA})),
    "send_to_estimator": ("Accepted, send to estimator", "Bidding", frozenset({_EA})),
    "stage_handoff": ("Project moved to your stage", "Bidding", frozenset({_EA, _LAB, _MAT})),
    "estimate_submitted": ("Estimate delivered", "Bidding", frozenset({_EA, _LAB, _MAT})),
    "quote_received": ("Vendor quotes", "Bidding", frozenset({_LAB, _MAT})),
    "late_quote": ("Late vendor quotes", "Bidding", frozenset({_LAB, _MAT})),
    "verify": ("Ready for Verify", "Bidding", frozenset({_EXE, _IT})),
    "verified": ("Numbers verified", "Bidding", frozenset({_EA, _LAB})),
    "call_in": ("Calling In", "Bidding", frozenset({_EXE, _LAB})),
    "scope_lines_review": ("Review scope lines", "Bidding", frozenset({_LAB})),
    "auto_send_sent": ("Auto sent", "Bidding", frozenset({_EXE, _EA, _IT, _ACC})),
    "auto_send_problem": ("Auto send problem", "Bidding", frozenset({_EXE, _EA, _IT, _ACC})),
    "proposal_send_failed": ("Proposal send failed", "Bidding", frozenset({_EA})),
    "submitted": ("Bid submitted", "Bidding", frozenset({_EXE, _LAB, _MAT})),
    "bid_outcome": ("Win / loss", "Bidding", frozenset({_EXE, _LAB, _MAT})),
    "project_abandoned": ("Project abandoned", "Bidding", frozenset({_EXE})),
    "due": ("Deadline reminders", "Bidding", _ALL),
    "approval_requested": ("Approval requested", "Approvals", frozenset({_EXE, _EA, _IT})),
    "revision_approval": ("Revised proposal approval", "Approvals", frozenset({_EXE})),
    "revision_round": ("Estimator revisions", "Revisions", frozenset({_EXE, _EA, _LAB, _MAT})),
    "drawing_changed": ("Drawings changed", "Revisions", frozenset({_LAB, _MAT})),
    "reverify_required": ("Re-verify required", "Revisions", frozenset({_EXE, _LAB})),
    "personal": ("Mentions, nudges and decisions", "Personal", _ALL),
    "estimator_note": ("Project notes", "Personal", _ALL),
    "pm": ("Project Management", "Other", _ALL),
}
BELL_EVENTS = tuple(_BELL_EVENTS)
# Never mirrored (P19). `other` also catches the unmapped assistant.calendar,
# rfp_ngem.* and bid_split.finished types. Every `assistant.*` type is a loop
# guard: the router's own bells are assistant.inbox and assistant.digest.
BELL_EXCLUDED_EVENTS = ("rfq_initial_send", "tech_outage", "security_alert", "other")
BELL_EXCLUDED_PREFIXES = ("assistant.",)

# Which alert says the most when several bells come from one email (4.2a).
RECORD_RANK = {"late_quote": 2, "quote_received": 1}


def _role_defaults(roles: Iterable[Role], severity: str) -> dict[Role, str]:
    return {r: severity for r in roles}


def _router_entry(
    source: str, sub_app: str, label: str, group: str, description: str,
    payload: dict[str, Any], defaults: dict[Role, str], templates: dict[str, str],
) -> dict[str, Any]:
    return {
        "kind": "router",
        "source": source,
        "sub_app": sub_app,
        "label": label,
        "group": group,
        "description": description,
        "payload": payload,
        "defaults": defaults,
        "audience": frozenset(defaults),
        "templates": templates,
        "urgent_allowed": True,
    }


def _record_entry(
    source: str, sub_app: str, label: str, group: str, description: str,
    payload: dict[str, Any], audience: frozenset[Role], templates: dict[str, str],
) -> dict[str, Any]:
    return {
        "kind": "record",
        "source": source,
        "sub_app": sub_app,
        "label": label,
        "group": group,
        "description": description,
        "payload": payload,
        "defaults": _role_defaults(audience, "low"),
        "audience": audience,
        "templates": templates,
        "urgent_allowed": False,
    }


_RECORD_TEMPLATES = {
    "title": "{label}",
    "title_project": "{label} on project {number}",
    "why": "BDR already sent you this notification. It is kept here quietly and nothing new is sent.",
}

_BID_TEMPLATES = {
    "title": "Bid is due {when}",
    "title_project": "Bid {number} is due {when}",
    # A line shown on a later day than its slot (a returning snooze) whose
    # due time is on an earlier Pacific day (digest_text).
    "title_overdue": "Bid was due {when}",
    "title_project_overdue": "Bid {number} was due {when}",
    "why_quotes": "Vendor quotes are still missing in {count} {categories}.",
    "why_quotes_unknown": "Vendor quotes are still missing.",
    "why_labor": "Labor is not entered yet.",
    "why_markups": "Markups are not entered yet.",
    "why_all": "Still missing: {parts}.",
    "why_none": "Check the readiness list before it is due.",
}
_TODO_TEMPLATES = {
    "title": "A to-do is {state}",
    "title_project": "A to-do on project {number} is {state}",
    "why_due": "Due {date}.",
    "why_overdue": "Overdue since {date}.",
}
_APPROVAL_TEMPLATES = {
    "title": "An assistant approval expires at {time}",
    "title_project": "An assistant approval on project {number} expires at {time}",
    "why": "An assistant card ({action}) is waiting for your approval. It expires at {time} Pacific.",
    "why_plain": "An assistant card is waiting for your approval. It expires at {time} Pacific.",
}

EVENT_TYPES: dict[str, dict[str, Any]] = {
    BID_DUE: _router_entry(
        "sweeper", "bidding", "Bid due and your part is missing", "Bidding",
        "A bid is due soon and a part your role owns is still missing.",
        _BID_PAYLOAD,
        {_EXE: "normal", _EA: "normal", _MAT: "normal", _LAB: "normal", _IT: "low"},
        _BID_TEMPLATES,
    ),
    TODO_DUE: _router_entry(
        "sweeper", "none", "To-do due", "Personal",
        "A to-do you own is due today or overdue.",
        _TODO_PAYLOAD,
        _role_defaults(INTERNAL_ROLES, "normal"),
        _TODO_TEMPLATES,
    ),
    APPROVAL_EXPIRING: _router_entry(
        "sweeper", "card", "Assistant approval expiring", "Approvals",
        "An assistant approval card you created is about to expire.",
        _APPROVAL_PAYLOAD,
        _role_defaults(INTERNAL_ROLES, "high"),
        _APPROVAL_TEMPLATES,
    ),
}
for _key, (_label, _group, _audience) in _BELL_EVENTS.items():
    EVENT_TYPES["bell." + _key] = _record_entry(
        "bell_tail", "pm" if _key == "pm" else "bidding", _label, _group,
        "A BDR notification you already received, recorded in your inbox.",
        _BELL_PAYLOAD, _audience, _RECORD_TEMPLATES,
    )
EVENT_TYPES[FAILED_TO_BID] = _record_entry(
    "audit_tail", "bidding", "Failed to bid", "Bidding",
    "A bid was marked Failed to bid; BDR already emailed the team.",
    _FAILED_PAYLOAD, INTERNAL_ROLES, _RECORD_TEMPLATES,
)
del _key, _label, _group, _audience

# Which part of a due bid each role owns (6.2 step 6). Executive and IT Admin
# count as owning every part (owner question 30, a); IT Admin stays inbox only.
PARTS = ("quotes", "labor", "markups")
ROLE_PARTS: dict[Role, frozenset[str]] = {
    _MAT: frozenset({"quotes"}),
    _LAB: frozenset({"labor"}),
    _EA: frozenset({"markups"}),
    _EXE: frozenset(PARTS),
    _IT: frozenset(PARTS),
}

# ── severity, mode and channel (6.3) ─────────────────────────────────────────
# `bell` is the per-item in-app bell; a normal row rings only the digest bell.
SEVERITY_TABLE: dict[str, dict[str, Any]] = {
    "urgent": {
        "delivery": "immediate", "bell": True, "teams": True, "email": False,
        "quiet_hours": "breaks_through", "digest": "never", "badge": True,
    },
    "high": {
        "delivery": "immediate", "bell": True, "teams": True, "email": False,
        "quiet_hours": "held", "digest": "fold", "badge": True,
    },
    "normal": {
        "delivery": "digest", "bell": False, "teams": False, "email": "digest",
        "quiet_hours": "waits", "digest": "listed", "badge": True,
    },
    "low": {
        "delivery": "record", "bell": False, "teams": False, "email": False,
        "quiet_hours": "n/a", "digest": "counted", "badge": False,
    },
    "record": {
        "delivery": "record", "bell": False, "teams": False, "email": False,
        "quiet_hours": "n/a", "digest": "counted", "badge": False,
    },
}
# What a user picks per kind (Alert, Digest, Inbox only, Off); an admin role
# row may also pick urgent for the three router kinds (10.1).
MODE_SEVERITY = {"urgent": "urgent", "alert": "high", "digest": "normal", "inbox": "low"}
USER_MODES = ("alert", "digest", "inbox", "off")
ADMIN_MODES = ("urgent", "alert", "digest", "inbox", "off")


def severity_for_mode(mode: str) -> str | None:
    """The severity a mode stands for; `off` (or an unknown mode) is None."""
    return MODE_SEVERITY.get(mode)


def mode_for_severity(severity: str, enabled: bool = True) -> str:
    """The mode label of an effective (enabled, severity) pair."""
    if not enabled:
        return "off"
    for mode, sev in MODE_SEVERITY.items():
        if sev == severity:
            return mode
    return "digest"


def delivery_for(event_type: str, severity: str) -> str:
    """`record` for record kinds and low, `digest` for normal, `immediate` for
    high and urgent (6.2 step 8)."""
    entry = EVENT_TYPES.get(event_type)
    if entry is None or entry["kind"] == "record":
        return "record"
    return SEVERITY_TABLE[severity]["delivery"]


def channels_for(event_type: str, severity: str) -> dict[str, Any]:
    """The severity row of 6.3 for one kind: delivery, bell, teams, email,
    quiet-hours behaviour, digest behaviour and whether it counts toward the
    unread badge. Record kinds are always the quiet `record` row."""
    entry = EVENT_TYPES.get(event_type)
    if entry is None or entry["kind"] == "record":
        return dict(SEVERITY_TABLE["record"])
    return dict(SEVERITY_TABLE[severity])


def default_severity(event_type: str, role: Role | str) -> str | None:
    """The code default for a role (D2), or None when the role is outside the
    kind's default audience."""
    entry = EVENT_TYPES.get(event_type)
    if entry is None:
        return None
    return entry["defaults"].get(Role(role)) if _is_role(role) else None


def _is_role(role: Any) -> bool:
    try:
        Role(role)
    except ValueError:
        return False
    return True


def effective_subscription(
    event_type: str,
    role: Role | str,
    user_row: Mapping[str, Any] | None = None,
    role_row: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """User row over admin role row over code default (6.2 step 5, 9.2).
    Returns `{enabled, severity, source}`; source is `user`, `admin`,
    `feedback_auto` (a user row written by the third not useful mark, passed
    through from the row), `admin`, `default` or `none` (the role is outside
    the kind's audience). An admin role row can add a role to a router kind; it
    never widens a record kind past its 0177 audience, and a disabled admin row
    is final (a user row cannot turn it back on). A user row cannot grant a
    kind the role does not receive, and a user's severity is clamped to high
    (never urgent)."""
    entry = EVENT_TYPES.get(event_type)
    off = {"enabled": False, "severity": "normal", "source": "none"}
    if entry is None or not _is_role(role):
        return off
    role = Role(role)
    base_sev = entry["defaults"].get(role)
    base = {"enabled": True, "severity": base_sev, "source": "default"} if base_sev else None
    if role_row is not None and (entry["kind"] == "router" or role in entry["audience"]):
        sev = role_row.get("severity")
        if not role_row.get("enabled"):
            # An admin switch-off is final: no user row turns the role back on.
            return {
                "enabled": False, "severity": sev if sev in SEVERITIES else (base_sev or "normal"),
                "source": "admin",
            }
        if sev in SEVERITIES and (sev != "urgent" or entry["urgent_allowed"]):
            base = {"enabled": True, "severity": sev, "source": "admin"}
    if base is None:
        return off
    if user_row is not None:
        sev = user_row.get("severity")
        sev = "high" if sev == "urgent" else (sev if sev in SEVERITIES else base["severity"])
        # `feedback_auto` keeps its own badge in the Settings grid (9.2).
        source = "feedback_auto" if user_row.get("source") == "feedback_auto" else "user"
        return {"enabled": bool(user_row.get("enabled")), "severity": sev, "source": source}
    return base


def parts_missing(payload: Mapping[str, Any]) -> frozenset[str]:
    """The missing parts a `bid.due_not_ready` payload names."""
    out: set[str] = set()
    if payload.get("missing_quote_category_ids"):
        out.add("quotes")
    if payload.get("labor_missing"):
        out.add("labor")
    if payload.get("markups_missing"):
        out.add("markups")
    return frozenset(out)


def role_owns_missing_part(role: Role | str, payload: Mapping[str, Any]) -> bool:
    """6.2 step 6: keep a user only if a part their role owns is missing."""
    if not _is_role(role):
        return False
    return bool(ROLE_PARTS.get(Role(role), frozenset()) & parts_missing(payload))


def bell_recordable(notification_type: str | None, routing_event: str | None) -> bool:
    """The bell tail's filter (4.2, P19): not an `assistant.*` type, and the
    routing event is on the mirror allowlist."""
    if any((notification_type or "").startswith(p) for p in BELL_EXCLUDED_PREFIXES):
        return False
    return routing_event in _BELL_EVENTS


def bell_event_type(routing_event: str) -> str:
    return "bell." + routing_event


def record_rank(routing_event: str | None) -> int:
    """The A11.11 rank of a bell: late_quote above quote_received, others 0."""
    return RECORD_RANK.get(routing_event or "", 0)


# ── time helpers ─────────────────────────────────────────────────────────────


def _dt(value: Any) -> datetime | None:
    """A datetime, an ISO string or None, as an aware UTC datetime."""
    if isinstance(value, datetime):
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
    if isinstance(value, str) and value:
        try:
            parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError:
            return None
        return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)
    return None


def _date(value: Any) -> date | None:
    """A Pacific calendar date from a date, a datetime or an ISO string."""
    if isinstance(value, datetime):
        return _dt(value).astimezone(PT).date()  # type: ignore[union-attr]
    if isinstance(value, date):
        return value
    if isinstance(value, str) and value:
        if len(value) <= 10:
            try:
                return date.fromisoformat(value)
            except ValueError:
                return None
        parsed = _dt(value)
        return parsed.astimezone(PT).date() if parsed else None
    return None


def _clock(dt: datetime, suffix: bool = True) -> str:
    local = dt.astimezone(PT)
    hour12 = local.hour % 12 or 12
    text = f"{hour12}:{local.minute:02d} {'AM' if local.hour < 12 else 'PM'}"
    return text + " PT" if suffix else text


def _day(d: date) -> str:
    return f"{_DAYS[d.weekday()]} {_MONTHS[d.month - 1]} {d.day}"


def pt_label(value: datetime) -> str:
    """"Thu Oct 29, 1:00 PM PT": a Pacific wall time with its date."""
    local = value.astimezone(PT)
    return f"{_day(local.date())}, {_clock(local)}"


def slot_label(slot: str, slot_times: Mapping[str, Any] | None = None) -> str:
    """"7:00 AM" or "1:00 PM" for a digest slot."""
    return _clock(datetime.combine(date(2000, 1, 3), _slot_time(slot, slot_times), tzinfo=PT), suffix=False)


def _as_time(value: Any) -> time:
    """A wall time from a `time` or an `"HH:MM"` string (the config shape)."""
    if isinstance(value, datetime):
        raise ValueError(f"not a wall time: {value!r}")
    if isinstance(value, time):
        return value.replace(tzinfo=None)
    if isinstance(value, str):
        m = re.match(r"^(\d{1,2}):(\d{2})$", value.strip())
        if m and int(m.group(1)) < 24 and int(m.group(2)) < 60:
            return time(int(m.group(1)), int(m.group(2)))
    raise ValueError(f"not a wall time: {value!r}")


def _slot_time(slot: str, slot_times: Mapping[str, Any] | None = None) -> time:
    if slot not in SLOT_TIMES:
        raise ValueError(f"unknown digest slot {slot!r}")
    if slot_times is None or slot_times.get(slot) is None:
        return SLOT_TIMES[slot]
    return _as_time(slot_times[slot])


def _when(due: datetime, ref: date) -> str:
    """"today at 2:30 PM PT", "tomorrow at 10:00 AM PT" or "Mon Nov 2 at ..."."""
    local = due.astimezone(PT)
    days = (local.date() - ref).days
    word = "today" if days == 0 else "tomorrow" if days == 1 else _day(local.date())
    return f"{word} at {_clock(local)}"


def is_workday(d: date, holidays: Iterable[Any] | None = None) -> bool:
    """Monday to Friday and not a company holiday (8.1, P6)."""
    return d.weekday() < 5 and d not in _holidays(holidays)


def slot_instant(d: date, slot: str, *, slot_times: Mapping[str, Any] | None = None) -> datetime:
    """The Pacific wall-time instant of digest slot `am` or `pm` on date `d`
    (8.1). Neither slot falls in the 02:00 to 03:00 DST gap or the repeated
    01:00 hour, so fold 0 is exact; tested across both DST changes."""
    return datetime.combine(d, _slot_time(slot, slot_times), tzinfo=PT)


def _slot_instants(d: date, slot_times: Mapping[str, Any] | None = None) -> list[datetime]:
    return [slot_instant(d, s, slot_times=slot_times) for s in SLOT_ORDER]


def next_slot(
    now: datetime, holidays: Iterable[Any] | None = None, *, slot_times: Mapping[str, Any] | None = None
) -> datetime:
    """The next digest slot instant strictly after `now`, skipping weekends
    and company holidays (8.1, 8.2); the instant a normal row belongs to and
    the snooze target. `holidays` is `assistant_settings.holidays` (dates or
    ISO strings); the module reads no settings itself. Returned in Pacific
    time; callers store `.astimezone(timezone.utc)`."""
    moment = _dt(now)
    if moment is None:
        raise ValueError("next_slot needs a datetime")
    skip = _holidays(holidays)
    day = moment.astimezone(PT).date()
    for _ in range(SEARCH_DAYS):
        if is_workday(day, skip):
            for instant in _slot_instants(day, slot_times):
                if instant > moment:
                    return instant
        day += timedelta(days=1)
    raise ValueError("no digest slot within the search horizon")


def previous_slot(
    at: datetime, holidays: Iterable[Any] | None = None, *, slot_times: Mapping[str, Any] | None = None
) -> datetime:
    """The digest slot instant strictly before `at` (R4b's "previous slot
    instant"): the same walk as `next_slot`, backwards."""
    moment = _dt(at)
    if moment is None:
        raise ValueError("previous_slot needs a datetime")
    skip = _holidays(holidays)
    day = moment.astimezone(PT).date()
    for _ in range(SEARCH_DAYS):
        if is_workday(day, skip):
            for instant in reversed(_slot_instants(day, slot_times)):
                if instant < moment:
                    return instant
        day -= timedelta(days=1)
    raise ValueError("no digest slot within the search horizon")


def due_window(
    slot_date: date, slot: str, holidays: Iterable[Any] | None = None, *,
    slot_times: Mapping[str, Any] | None = None,
) -> tuple[datetime, datetime]:
    """The `bid.due_not_ready` window of 5.3: from the slot instant to the end
    of the next digest day after `slot_date`, so a Friday slot covers Friday
    and Monday bids and a Thursday before a Friday holiday covers Monday."""
    skip = _holidays(holidays)
    start = slot_instant(slot_date, slot, slot_times=slot_times)
    nxt = slot_date + timedelta(days=1)
    for _ in range(SEARCH_DAYS):
        if is_workday(nxt, skip):
            break
        nxt += timedelta(days=1)
    return start, datetime.combine(nxt + timedelta(days=1), time(0, 0), tzinfo=PT)


def in_quiet_hours(now: datetime, *, start: Any = QUIET_START, end: Any = QUIET_END) -> bool:
    """True inside the router's quiet window (18:00 to 06:00 Pacific by
    default, start inclusive, end exclusive; 8.4)."""
    moment = _dt(now)
    if moment is None:
        raise ValueError("in_quiet_hours needs a datetime")
    t = moment.astimezone(PT).time()
    start, end = _as_time(start), _as_time(end)
    if start > end:
        return t >= start or t < end
    return start <= t < end


def next_quiet_end(now: datetime, *, end: Any = QUIET_END) -> datetime:
    """The next 06:00 Pacific instant strictly after `now`, the release time
    of a held high row and of a quiet-hours Teams deferral (8.4). Pacific."""
    moment = _dt(now)
    if moment is None:
        raise ValueError("next_quiet_end needs a datetime")
    day = moment.astimezone(PT).date()
    end = _as_time(end)
    candidate = datetime.combine(day, end, tzinfo=PT)
    if candidate <= moment:
        candidate = datetime.combine(day + timedelta(days=1), end, tzinfo=PT)
    return candidate


def fold_params(
    now: datetime, holidays: Iterable[Any] | None = None, *, catchup_hours: int = CATCHUP_HOURS,
    slot_times: Mapping[str, Any] | None = None,
) -> tuple[date, datetime, datetime] | None:
    """The `(p_am_date, p_am_slot_at, p_fold_until)` of A11.14 for a held row
    released at `now`, or None on a weekend or holiday (no digest, so the row
    is delivered alone, owner question 6). `p_fold_until` is the AM slot plus
    the catch-up window: 10:00 by default."""
    moment = _dt(now)
    if moment is None:
        raise ValueError("fold_params needs a datetime")
    day = moment.astimezone(PT).date()
    if not is_workday(day, holidays):
        return None
    am = slot_instant(day, "am", slot_times=slot_times)
    return day, am, am + timedelta(hours=catchup_hours)


def delivery_plan(
    event_type: str,
    severity: str,
    now: datetime,
    holidays: Iterable[Any] | None = None,
    *,
    slot_at: datetime | None = None,
    slot_times: Mapping[str, Any] | None = None,
    quiet_start: Any = QUIET_START,
    quiet_end: Any = QUIET_END,
) -> dict[str, Any]:
    """When a row is delivered and when its Teams message may go (6.2 step 8,
    8.4). Keys: `delivery`, `deliver_after` (None for record rows), `held`
    (a high row held by quiet hours, which may fold into the 7:00 AM digest)
    and `teams_not_before` (the next 06:00 only for a non-urgent
    `assistant.approval_expiring` row delivered in quiet hours, P31; an urgent
    one is at once, P39). `slot_at` is the slot a digest-bound row was
    emitted for; without it a digest row belongs to `next_slot(now)`.
    `slot_times`, `quiet_start` and `quiet_end` are the configured values."""
    delivery = delivery_for(event_type, severity)
    plan: dict[str, Any] = {
        "delivery": delivery, "deliver_after": None, "held": False, "teams_not_before": None,
    }
    if delivery == "immediate":
        quiet = in_quiet_hours(now, start=quiet_start, end=quiet_end)
        free = severity == "urgent" or event_type == APPROVAL_EXPIRING
        plan["held"] = quiet and not free
        plan["deliver_after"] = next_quiet_end(now, end=quiet_end) if plan["held"] else _dt(now)
        if quiet and event_type == APPROVAL_EXPIRING and severity != "urgent":
            plan["teams_not_before"] = next_quiet_end(now, end=quiet_end)
    elif delivery == "digest":
        plan["deliver_after"] = (
            _dt(slot_at).astimezone(PT) if slot_at is not None else next_slot(now, holidays, slot_times=slot_times)
        )
    return plan


def is_held(row: Mapping[str, Any]) -> bool:
    """8.4: a row is held when it is an immediate row of severity high, its kind
    is not `assistant.approval_expiring` and `deliver_after > created_at`. Only
    the quiet-hours hold sets `deliver_after` past the insert instant, so a
    crash leftover (`deliver_after = created_at`), an urgent row and an
    approval-expiring row are never held and never fold."""
    if row.get("delivery", "immediate") != "immediate":
        return False
    if row.get("severity") != "high" or row.get("event_type") == APPROVAL_EXPIRING:
        return False
    after, created = _dt(row.get("deliver_after")), _dt(row.get("created_at"))
    return after is not None and created is not None and after > created


# ── payload allowlist, taint and amounts (4.1, 4.4) ──────────────────────────


def _coerce(spec: Any, value: Any) -> tuple[bool, Any]:
    kind = spec[0] if isinstance(spec, tuple) else spec
    if kind == _BOOL:
        return isinstance(value, bool), value
    if kind == _INT:
        ok = isinstance(value, int) and not isinstance(value, bool) and -1_000_000 <= value <= 1_000_000
        return ok, value
    if kind == "enum":
        return isinstance(value, str) and value in spec[1], value
    if kind == _UUID:
        text = str(value) if isinstance(value, uuid.UUID) else value
        return isinstance(text, str) and bool(_UUID_RE.match(text)), text
    if kind == _ID:
        if isinstance(value, bool):
            return False, None
        if isinstance(value, int):
            return 0 <= value <= 2**53, value
        text = str(value) if isinstance(value, uuid.UUID) else value
        ok = isinstance(text, str) and bool(_UUID_RE.match(text) or _DIGITS_RE.match(text))
        return ok, text
    if kind == _SLUG:
        return isinstance(value, str) and bool(_SLUG_RE.match(value)), value
    if kind == _HASH:
        return isinstance(value, str) and bool(_HASH_RE.match(value)), value
    if kind == _ISO:
        if isinstance(value, datetime):
            return True, (_dt(value) or value).isoformat()
        if isinstance(value, date):
            return True, value.isoformat()
        if isinstance(value, str):
            text = value.strip()
            return len(text) <= ISO_MAX and (_dt(text) is not None or _date(text) is not None), text
        return False, None
    if kind == _UUID_LIST:
        if not isinstance(value, (list, tuple)) or len(value) > LIST_MAX:
            return False, None
        items = [_coerce(_UUID, v) for v in value]
        if not all(ok for ok, _ in items):
            return False, None
        return True, [v for _, v in items]
    return False, None


def clean_payload_report(event_type: str, payload: Mapping[str, Any] | None) -> tuple[dict, list]:
    """`(clean, dropped_keys)`: the payload reduced to the kind's allowlist.
    A key that is not allowlisted, or whose value does not fit its spec (a
    string over 64 characters that is not an ISO time, a nested object, a
    wrong type), is dropped and named so the caller can log it. The result is
    capped at 4 KB by dropping the largest values first. Raises KeyError for
    an unknown kind."""
    spec = EVENT_TYPES[event_type]["payload"]
    clean: dict[str, Any] = {}
    dropped: list[str] = []
    for key, value in (payload or {}).items():
        if key not in spec or value is None:
            if key not in spec:
                dropped.append(str(key))
            continue
        ok, cooked = _coerce(spec[key], value)
        if ok:
            clean[key] = cooked
        else:
            dropped.append(key)
    while len(json.dumps(clean, sort_keys=True, default=str).encode()) > PAYLOAD_MAX_BYTES:
        biggest = max(clean, key=lambda k: len(json.dumps(clean[k], default=str)))
        dropped.append(biggest)
        del clean[biggest]
    return clean, dropped


def clean_payload(event_type: str, payload: Mapping[str, Any] | None) -> dict[str, Any]:
    """The payload reduced to the kind's allowlist: unknown keys dropped, 4 KB
    cap, no string over 64 characters except ISO times (4.1, 4.4)."""
    return clean_payload_report(event_type, payload)[0]


# ── email-ID collapse (4.2a) ─────────────────────────────────────────────────


def _text(value: Any) -> str | None:
    if value is None or isinstance(value, bool):
        return None
    text = str(value).strip()
    return text or None


def email_message_id(metadata: Mapping[str, Any] | None) -> str | None:
    """`rfq_message_id`, else `graph_message_id`, else `internet_message_id`
    (the uuid is preferred, 4.2a rule 2), or None."""
    if not isinstance(metadata, Mapping):
        return None
    for key in ("rfq_message_id", "graph_message_id", "internet_message_id"):
        found = _text(metadata.get(key))
        if found:
            return found
    return None


def email_key(metadata: Mapping[str, Any] | None) -> str | None:
    """sha256 hex of `"msg:" + (rfq_message_id or graph_message_id or
    internet_message_id)`, or None when the bell carries no message ID; the
    conversation ID alone never yields a key (4.2a, rules 2 and 3). The hash
    is what enters a payload, never the raw id."""
    found = email_message_id(metadata)
    if found is None:
        return None
    return hashlib.sha256(("msg:" + found).encode()).hexdigest()


def _bucket(created_at: Any) -> int:
    moment = _dt(created_at)
    if moment is None:
        raise ValueError("a bucket key needs the bell's created_at")
    return int(moment.timestamp() // EMAIL_BUCKET_SECONDS)


def conversation_key(metadata: Mapping[str, Any] | None, created_at: Any) -> str | None:
    """The fallback key (4.2a rule 4): sha256 of `"conv:" + conversation_id +
    ":" + bucket`, used only inside the 120 s bucket and only when the bell
    has no message ID. Two replies far apart in one conversation never match."""
    if not isinstance(metadata, Mapping):
        return None
    conv = _text(metadata.get("conversation_id"))
    if conv is None:
        return None
    return hashlib.sha256(f"conv:{conv}:{_bucket(created_at)}".encode()).hexdigest()


def bell_inbox_key(
    notification_id: str,
    routing_event: str | None,
    metadata: Mapping[str, Any] | None,
    *,
    user_id: str | None = None,
    rfq_id: str | None = None,
    created_at: Any = None,
) -> str:
    """The inbox (and event) dedupe key of a bell (4.2, 4.2a):
      1. a message ID: `p7:bell:email:{email_key}`, so every bell from that
         one email for that user lands on one row;
      2. else, for a `quote_received` bell only, the 120 s bucket fallback:
         `p7:bell:email:{conversation hash}` when only a conversation ID is
         present, else `p7:bell:quote_received:{user}:{rfq}:{bucket}`;
      3. else its own row, `p7:bell:{notification_id}`.
    `conversation_id` alone never collapses two bells outside the bucket."""
    key = email_key(metadata)
    if key:
        return f"p7:bell:email:{key}"
    if routing_event == "quote_received" and created_at is not None:
        conv = conversation_key(metadata, created_at)
        if conv:
            return f"p7:bell:email:{conv}"
        if user_id and rfq_id:
            return f"p7:bell:quote_received:{user_id}:{rfq_id}:{_bucket(created_at)}"
    if not notification_id:
        raise ValueError("a bell dedupe key needs the notification id")
    return f"p7:bell:{notification_id}"


def event_dedupe_key(event_type: str, payload: Mapping[str, Any], project_id: str | None = None) -> str:
    """The `p7:`-prefixed dedupe key of an event (4.1, 4.2). Bell kinds use
    the bell id here; the collapse keys of `bell_inbox_key` apply to inbox
    rows. Pass the cleaned payload. Raises ValueError when a part of the key is
    missing, so two malformed events never collapse onto one key (inserts
    ignore duplicate keys, 4.1); raises KeyError for an unknown kind."""
    values = dict(payload, project_id=project_id)

    def need(*parts: str) -> str:
        missing = [k for k in parts if values.get(k) is None or values.get(k) == ""]
        if missing:
            raise ValueError(f"{event_type} dedupe key is missing {', '.join(missing)}")
        return ":".join(str(values[k]) for k in parts)

    if event_type == BID_DUE:
        return f"p7:{BID_DUE}:" + need("project_id", "internal_bid_at", "slot_date", "slot")
    if event_type == TODO_DUE:
        return f"p7:{TODO_DUE}:" + need("todo_id", "due")
    if event_type == APPROVAL_EXPIRING:
        return f"p7:{APPROVAL_EXPIRING}:" + need("approval_id", "expires_at")
    if event_type == FAILED_TO_BID:
        return f"p7:{FAILED_TO_BID}:" + need("audit_id")
    if event_type.startswith("bell."):
        return "p7:bell:" + need("notification_id")
    raise KeyError(event_type)


# ── precedence (7) ───────────────────────────────────────────────────────────
# Each row: id, the existing channel it concerns, the kinds it covers (exact
# names, "bell." for every bell kind, or "*"), a one-line check, the outcomes
# it can give and `match(event_type, payload, facts)`, which returns None (no
# match) or a dict with an `outcome` and optional `note`, `reason` and `teams`.
# Outcomes: push (deliver per severity), record, merge (the line becomes the
# note), note (the note is added), suppress (write nothing), none (no
# interaction, the existing behaviour stays).


def _facts_dt(facts: Mapping[str, Any], key: str) -> datetime | None:
    return _dt(facts.get(key))


def _slot_at(payload: Mapping[str, Any], facts: Mapping[str, Any]) -> datetime | None:
    given = _facts_dt(facts, "slot_at")
    if given is not None:
        return given
    day, slot = _date(payload.get("slot_date")), payload.get("slot")
    if day is None or slot not in SLOT_TIMES:
        return None
    return slot_instant(day, slot, slot_times=facts.get("slot_times")).astimezone(UTC)


def _r_direct(et: str, p: Mapping, f: Mapping) -> dict | None:  # R21
    return {"outcome": "none", "reason": "direct_row"} if f.get("delivery") == "direct" else None


def _r_routine_bell(et: str, p: Mapping, f: Mapping) -> dict | None:  # R20
    # Bell kinds only: a stray bell_type fact never suppresses a router kind.
    if et.startswith("bell.") and str(f.get("bell_type") or "").startswith("assistant."):
        return {"outcome": "none", "reason": "assistant_bell"}
    return None


def _r_stub(et: str, p: Mapping, f: Mapping) -> dict | None:  # R7
    # Documentation only: the Phase 3 stub concerns Phase 3 code and has no
    # interaction with router items (7), so it never matches a router kind.
    return None


def _r_mutes(et: str, p: Mapping, f: Mapping) -> dict | None:  # R25
    if f.get("subscription_enabled") is False:
        return {"outcome": "suppress", "reason": "muted"}
    if f.get("project_muted"):
        return {"outcome": "suppress", "reason": "project_muted"}
    return None


def _r_closed(et: str, p: Mapping, f: Mapping) -> dict | None:  # R24
    if et in ROUTER_KINDS and f.get("project_closed"):
        return {"outcome": "suppress", "reason": "project_closed"}
    return None


def _r_card_gone(et: str, p: Mapping, f: Mapping) -> dict | None:  # R19
    if et == APPROVAL_EXPIRING and f.get("card_pending") is False:
        return {"outcome": "suppress", "reason": "superseded"}
    return None


def _r_reminder_1h(et: str, p: Mapping, f: Mapping) -> dict | None:  # R4
    if et != BID_DUE:
        return None
    now, logged = _facts_dt(f, "now"), _facts_dt(f, "reminder_1h_logged_at")
    if now is not None and logged is not None and timedelta(0) <= now - logged <= timedelta(minutes=60):
        return {"outcome": "suppress", "reason": "reminder_just_fired"}
    due, slot_at = _dt(p.get("internal_bid_at")), _slot_at(p, f)
    if due is not None and slot_at is not None:
        fires = due - timedelta(hours=1)
        if f.get("reminder_1h_expected") is True and slot_at <= fires < slot_at + timedelta(minutes=60):
            return {"outcome": "note", "note": f"A 1-hour reminder follows at {_clock(fires)}."}
    return None


def _r_reminder_early(et: str, p: Mapping, f: Mapping) -> dict | None:  # R4b
    if et != BID_DUE:
        return None
    due, slot_at = _dt(p.get("internal_bid_at")), _slot_at(p, f)
    logged, prev = _facts_dt(f, "reminder_logged_at"), _facts_dt(f, "prev_slot_at")
    if None in (due, slot_at, logged, prev) or not f.get("missing_unchanged"):
        return None
    if due.astimezone(PT).date() <= slot_at.astimezone(PT).date() or logged < prev:
        return None
    return {"outcome": "merge", "note": f"You were reminded at {_clock(logged)}."}


def _r_routine(et: str, p: Mapping, f: Mapping) -> dict | None:  # R6
    if et != BID_DUE:
        return None
    now = _facts_dt(f, "now")
    for routine in f.get("routines") or ():
        label = RECIPE_LABELS.get(str(routine.get("recipe")))
        if label is None:
            continue
        if routine.get("delivered_today"):
            return {"outcome": "merge", "note": f'Your routine "{label}" covered this today.'}
        nxt = _dt(routine.get("next_run_at"))
        if nxt is not None and now is not None and nxt >= now and nxt.astimezone(PT).date() == now.astimezone(PT).date():
            return {"outcome": "merge", "note": f'Your routine "{label}" covers this at {_clock(nxt)}.'}
    return None


def _r_nudge(et: str, p: Mapping, f: Mapping) -> dict | None:  # R17
    if et == TODO_DUE and f.get("nudged_today"):
        return {"outcome": "merge", "note": "Due today (you were nudged)."}
    return None


def _r_auto_send(et: str, p: Mapping, f: Mapping) -> dict | None:  # R10
    auto = f.get("auto_send")
    if et != BID_DUE or not isinstance(auto, Mapping):
        return None
    if not auto.get("enabled") or auto.get("silent_reason") is not None:
        return None
    sent_at = auto.get("send_time")
    if not isinstance(sent_at, str) or not re.match(r"^\d{1,2}:\d{2}$", sent_at):
        return None
    hh, mm = (int(x) for x in sent_at.split(":"))
    shown = _clock(datetime.combine(date(2000, 1, 3), time(hh % 24, mm % 60), tzinfo=PT), suffix=False)
    return {
        "outcome": "note",
        "note": (
            f"Auto send will email the GCs at {shown} Pacific at the estimator's estimate "
            "plus markup unless someone sends first."
        ),
    }


def _r_call_in_overlap(et: str, p: Mapping, f: Mapping) -> dict | None:  # R9
    if et == BID_DUE and f.get("call_in_overlap"):
        return {"outcome": "push", "reason": "call_in_overlap"}
    return None


def _r_teams_off(et: str, p: Mapping, f: Mapping) -> dict | None:  # R23
    if et in ROUTER_KINDS and f.get("severity") in ("high", "urgent") and f.get("teams_enabled") is False:
        return {"outcome": "push", "reason": "teams_off", "teams": False}
    return None


def _record(kinds: tuple[str, ...], reason: str):
    def match(et: str, p: Mapping, f: Mapping) -> dict | None:
        return {"outcome": "record", "reason": reason} if et in kinds else None

    return match


def _r_email_collapse(et: str, p: Mapping, f: Mapping) -> dict | None:  # R13
    if et in ("bell.quote_received", "bell.late_quote") and (p.get("email_key") or f.get("bucket_key")):
        return {"outcome": "record", "reason": "email_collapse"}
    return None


def _r_bdr_external(et: str, p: Mapping, f: Mapping) -> dict | None:  # R26
    if et.startswith("bell.") and f.get("bdr_external"):
        return {"outcome": "record", "reason": "bdr_sent_external"}
    return None


def _r_bell(et: str, p: Mapping, f: Mapping) -> dict | None:  # R12
    return {"outcome": "record", "reason": "bell_source"} if et.startswith("bell.") else None


def _row(id_: str, channel: str, kinds: tuple[str, ...], check: str, outcomes: tuple[str, ...], match) -> dict:
    return {"id": id_, "channel": channel, "kinds": kinds, "check": check, "outcomes": outcomes, "match": match}


PRECEDENCE: tuple[dict[str, Any], ...] = (
    _row("R21", "Phase 5 and 6 direct writers (0184, 0187)", ("*",),
         "delivery is direct", ("none",), _r_direct),
    _row("R20", "Phase 3 routine bells and the 10-unread pause", ("bell.",),
         "the bell type starts with assistant.", ("none",), _r_routine_bell),
    _row("R7", "Phase 3 precedence stub (due_today routines on 6:03 days)", (),
         "documentation only: never matches a router item", ("none",), _r_stub),
    _row("R25", "mutes and subscriptions (section 9)", ("*",),
         "user row disabled or project muted", ("suppress",), _r_mutes),
    _row("R24", "project closed (abandon, failed to bid, outcome)", ROUTER_KINDS,
         "project state at delivery", ("suppress",), _r_closed),
    _row("R19", "Phase 4 approval bells (expired, failed, superseded)", (APPROVAL_EXPIRING,),
         "the card is no longer pending at delivery", ("suppress",), _r_card_gone),
    _row("R4", "due_reminders 1h", (BID_DUE,),
         "a 1h reminder fired in the last 60 min or fires within 60 min of the slot",
         ("suppress", "note"), _r_reminder_1h),
    _row("R4b", "due_reminders 1d and 2d", (BID_DUE,),
         "reminded since the previous slot and the missing set is unchanged",
         ("merge",), _r_reminder_early),
    _row("R6", "Phase 3 recipes (routines)", (BID_DUE,),
         "an active routine covers this today", ("merge",), _r_routine),
    _row("R17", "todo nudge (event personal)", (TODO_DUE,),
         "last_nudged_at is today (Pacific)", ("merge",), _r_nudge),
    _row("R10", "auto_send", (BID_DUE,),
         "auto send is on and will send for the project", ("note",), _r_auto_send),
    _row("R9", "calling_in", (BID_DUE,),
         "a send_out project with a GC sent can also be on the pre-bid call list",
         ("push",), _r_call_in_overlap),
    _row("R23", "Teams per person (teams_alert_users.enabled)", ROUTER_KINDS,
         "high or urgent row and the person's Teams switch is off", ("push",), _r_teams_off),
    _row("R5", "due_reminders bells", ("bell.due",),
         "the reminder bell is the source", ("record",), _record(("bell.due",), "bell_source")),
    _row("R8", "calling_in bells", ("bell.call_in",),
         "the call-in bell", ("record",), _record(("bell.call_in",), "bell_source")),
    _row("R11", "auto_send bells", ("bell.auto_send_sent", "bell.auto_send_problem", "bell.scope_lines_review"),
         "the auto send bell", ("record",),
         _record(("bell.auto_send_sent", "bell.auto_send_problem", "bell.scope_lines_review"), "bell_source")),
    _row("R13", "bells from one vendor email", ("bell.quote_received", "bell.late_quote"),
         "same user and same email message ID, or the 120 s bucket without one", ("record",),
         _r_email_collapse),
    _row("R16", "failed-to-bid email", (FAILED_TO_BID,),
         "the email went to every internal user", ("record",), _record((FAILED_TO_BID,), "bdr_emailed")),
    _row("R18", "note mentions", ("bell.personal",),
         "the mention bell", ("record",), _record(("bell.personal",), "bell_source")),
    _row("R22", "PM RFI sync summary", ("bell.pm",),
         "the PM bell", ("record",), _record(("bell.pm",), "bell_source")),
    _row("R26", "BDR's own email and Teams for the same fact", ("bell.",),
         "the fact reached the person as a BDR bell and 0177 also sent email or Teams",
         ("record",), _r_bdr_external),
    _row("R12", "0177 routed bells in general", ("bell.",),
         "the bell row is the source and names the user", ("record",), _r_bell),
)
RULE_IDS = tuple(row["id"] for row in PRECEDENCE)


def evaluate_precedence(
    event_type: str, payload: Mapping[str, Any] | None = None, facts: Mapping[str, Any] | None = None
) -> dict[str, Any]:
    """Walk `PRECEDENCE` in order; the first rule whose match returns a result
    wins. Returns `{id, outcome, note, reason, teams}`; with no match the
    result is `{id: None, outcome: "push"}` (deliver per severity)."""
    p, f = payload or {}, facts or {}
    for row in PRECEDENCE:
        hit = row["match"](event_type, p, f)
        if hit is not None:
            return {
                "id": row["id"], "outcome": hit["outcome"], "note": hit.get("note"),
                "reason": hit.get("reason"), "teams": hit.get("teams"),
            }
    return {"id": None, "outcome": "push", "note": None, "reason": None, "teams": None}


# ── server text (4.4, 8.2, 8.5) ──────────────────────────────────────────────
# Titles, whys, digest lines, Teams text and the email subject are built only
# from ids, enums, counts, Pacific times and the project number.


def project_number(value: Any) -> str | None:
    """A project number safe to show, or None. Numbers are system-made
    (`26.10.7301`); anything else (a name typed where a number belongs) is
    refused so a free-text value can never reach a title."""
    text = _text(value)
    return text if text and _NUMBER_RE.match(text) else None


def _plural(count: int, one: str, many: str) -> str:
    return one if count == 1 else many


def _bid_why(role: Role | str | None, payload: Mapping[str, Any], tpl: Mapping[str, str]) -> str:
    missing = parts_missing(payload)
    mine = missing & ROLE_PARTS.get(Role(role), frozenset(PARTS)) if role and _is_role(role) else missing
    mine = mine or missing
    count = len(payload.get("missing_quote_category_ids") or [])
    if len(mine) == 1:
        (only,) = mine
        if only == "quotes":
            if count:
                return tpl["why_quotes"].format(
                    count=count, categories=_plural(count, "category", "categories"))
            return tpl["why_quotes_unknown"]
        return tpl["why_labor" if only == "labor" else "why_markups"]
    if not mine:
        return tpl["why_none"]
    names = []
    if "quotes" in mine:
        names.append(f"vendor quotes ({count} {_plural(count, 'category', 'categories')})" if count else "vendor quotes")
    if "labor" in mine:
        names.append("labor")
    if "markups" in mine:
        names.append("markups")
    return tpl["why_all"].format(parts=", ".join(names))


def _humanize(slug: Any) -> str | None:
    text = _text(slug)
    if text is None or not _SLUG_RE.match(text):
        return None
    return text.replace("_", " ").replace(".", " ")


def _titles(
    event_type: str, role: Role | str | None, payload: Mapping[str, Any], number: str | None,
    facts: Mapping[str, Any],
) -> tuple[str, str]:
    entry = EVENT_TYPES[event_type]
    tpl = entry["templates"]
    if event_type == BID_DUE:
        due = _dt(payload.get("internal_bid_at"))
        ref = _date(payload.get("slot_date")) or _date(facts.get("now"))
        if due is None:
            when = "soon"
        else:
            when = _when(due, ref or due.astimezone(PT).date())
        title = tpl["title_project"].format(number=number, when=when) if number else tpl["title"].format(when=when)
        return title, _bid_why(role, payload, tpl)
    if event_type == TODO_DUE:
        overdue = bool(payload.get("overdue"))
        state = "overdue" if overdue else "due today"
        title = tpl["title_project"].format(number=number, state=state) if number else tpl["title"].format(state=state)
        day = _date(payload.get("due"))
        label = _day(day) if day else "today"
        return title, tpl["why_overdue" if overdue else "why_due"].format(date=label)
    if event_type == APPROVAL_EXPIRING:
        expires = _dt(payload.get("expires_at"))
        clock = _clock(expires).removesuffix(" PT") if expires else "soon"
        title = tpl["title_project"].format(number=number, time=clock) if number else tpl["title"].format(time=clock)
        action = _humanize(payload.get("action_kind"))
        why = tpl["why"].format(action=action, time=clock) if action else tpl["why_plain"].format(time=clock)
        return title, why
    label = entry["label"]
    title = tpl["title_project"].format(label=label, number=number) if number else tpl["title"].format(label=label)
    return title, tpl["why"]


def render(event: dict[str, Any], recipient_role: str, facts: dict[str, Any]) -> dict[str, Any]:
    """Title, why, payload (ids plus server labels), severity, delivery
    (`record`, `digest` or `immediate`) and the precedence rule id for one
    event and one recipient role (6.2 step 8). Titles name a project by
    number only; no free text and no amounts reach the output (4.4).

    `event` carries `event_type`, `payload` and optionally `project_id`.
    `facts` is described in the module docstring. The result also holds the
    precedence `outcome` (`suppress` and `none` rows have `delivery` None and
    write nothing), `note`, `reason`, whether a router Teams message applies
    (`teams`), the delivery plan (`deliver_after`, `held`, `teams_not_before`)
    when `facts` has `now`, the A11.11 `rank` for bell kinds and `dedupe_key`
    (None for `suppress` and `none` rows, which write nothing).
    """
    event_type = event["event_type"]
    entry = EVENT_TYPES.get(event_type)
    if entry is None:
        raise KeyError(event_type)
    payload = clean_payload(event_type, event.get("payload"))
    number = project_number(facts.get("project_number"))
    is_record = entry["kind"] == "record"

    if is_record:
        severity = "low"
    else:
        severity = facts.get("severity") or default_severity(event_type, recipient_role) or "normal"
        if severity not in SEVERITIES:
            severity = default_severity(event_type, recipient_role) or "normal"

    rule_facts = dict(facts)
    rule_facts["severity"] = severity
    verdict = evaluate_precedence(event_type, payload, rule_facts)
    outcome = verdict["outcome"]

    title, why = _titles(event_type, recipient_role, payload, number, facts)
    if verdict["note"]:
        why = verdict["note"] if outcome == "merge" else f"{why} {verdict['note']}"

    quiet_row = outcome in ("suppress", "none")
    delivery = None if quiet_row else delivery_for(event_type, severity)
    teams = (
        delivery == "immediate"
        and outcome in ("push", "note")
        and verdict["teams"] is not False
        and facts.get("teams_enabled") is not False
    )
    result: dict[str, Any] = {
        "event_type": event_type,
        "title": title,
        "why": why,
        "payload": dict(payload, **({"project_number": number} if number else {})),
        "severity": severity,
        "delivery": delivery,
        "precedence": verdict["id"],
        "outcome": outcome,
        "note": verdict["note"],
        "reason": verdict["reason"],
        "teams": teams,
        "kind_label": entry["label"],
        "deliver_after": None,
        "held": False,
        "teams_not_before": None,
        "rank": record_rank(event_type.removeprefix("bell.")) if event_type.startswith("bell.") else None,
        # Only a row that is written needs a key; a malformed event that would
        # be written raises ValueError (the service fails the event).
        "dedupe_key": None if quiet_row else event_dedupe_key(event_type, payload, event.get("project_id")),
    }
    now = _dt(facts.get("now"))
    if delivery is not None and now is not None:
        plan = delivery_plan(
            event_type, severity, now, facts.get("holidays"),
            slot_at=_slot_at(payload, facts) if delivery == "digest" else None,
            slot_times=facts.get("slot_times"),
            quiet_start=facts.get("quiet_start") or QUIET_START,
            quiet_end=facts.get("quiet_end") or QUIET_END,
        )
        result["deliver_after"] = plan["deliver_after"]
        result["held"] = plan["held"]
        result["teams_not_before"] = plan["teams_not_before"] if teams else None
    return result


def bell_message(title: str) -> str:
    """The text of a router bell for an immediate item (A11.3 `p_bell_message`,
    300 characters at most): the server title."""
    return title[:300]


def bell_metadata(event_type: str, number: str | None = None) -> dict[str, Any]:
    """Metadata of an `assistant.inbox` bell: the kind, the project number and
    `actor_kind: system` (the reserved kind). SQL adds `inbox_id`."""
    meta: dict[str, Any] = {"event_type": event_type, "actor_kind": "system"}
    if project_number(number):
        meta["project_number"] = number
    return meta


def teams_message(title: str, why: str) -> str:
    """The Teams card message of an immediate item (8.5 step 5)."""
    return f"{title}. {why}"


def teams_summary(heading: str, number: str | None, when: datetime | None = None) -> str:
    """The Teams toast text: the heading plus the project number, or the Pacific
    time when the row has no project. Never BDR's own subject-based summary."""
    tail = project_number(number) or (pt_label(when) if when is not None else "")
    return f"{heading} · {tail}" if tail else heading


def digest_slot_label(slot: str, digest_date: date, slot_times: Mapping[str, Any] | None = None) -> str:
    """"7:00 AM PT, Thu Oct 29": the Pacific slot label of a digest."""
    return f"{slot_label(slot, slot_times)} PT, {_day(digest_date)}"


def digest_subject(
    heading: str, slot: str, digest_date: date, slot_times: Mapping[str, Any] | None = None
) -> str:
    """The digest email subject, `G3 BDR · <heading> · <slot label>`
    (the `notification_email._subject` shape for assistant types)."""
    return f"G3 BDR · {heading} · {digest_slot_label(slot, digest_date, slot_times)}"


def digest_sort_key(item: Mapping[str, Any]) -> tuple:
    """Overnight high items first, then highest severity, then oldest (8.2)."""
    held_high = item.get("precedence") == "quiet_hours" and item.get("severity") in ("high", "urgent")
    created = _dt(item.get("created_at")) or datetime.min.replace(tzinfo=UTC)
    return (0 if held_high else 1, -SEVERITIES.index(item.get("severity") or "normal"), created)


def _shown_from(item: Mapping[str, Any], payload: Mapping[str, Any]) -> date | None:
    """The Pacific day a stored title and why were rendered for: the slot
    date of a bid line, else the day the row was written."""
    if item.get("event_type") == BID_DUE:
        return _date(payload.get("slot_date")) or _date(item.get("created_at"))
    return _date(item.get("created_at"))


def digest_text(item: Mapping[str, Any], day: Any = None, role: Role | str | None = None) -> tuple[str, str]:
    """(title, why) of a delivered row as shown on Pacific `day` (a date, or
    a time or ISO string read as its Pacific date; S9-3). The
    stored text of `todo.due` and `bid.due_not_ready` is relative to the day
    it was written ("due today", "due tomorrow at ..."), so a row shown on a
    later day (a snoozed row returning, an item read days later) is rendered
    again from its payload facts with the same templates: a to-do past its
    due date reads "overdue", a bid reads "due <day>" or "was due <day>".
    The bid why is rendered again for `role` (it drops a precedence note
    about the earlier day); without a role the stored why stays. Every
    other row, a row shown on its own day and a row without the facts keep
    the stored text."""
    title, why = str(item.get("title") or ""), str(item.get("why") or "")
    payload = item.get("payload")
    event_type = item.get("event_type")
    day = _date(day)
    if day is None or event_type not in (TODO_DUE, BID_DUE) or not isinstance(payload, Mapping):
        return title, why
    since = _shown_from(item, payload)
    if since is None or day <= since:
        return title, why
    number = project_number(payload.get("project_number"))
    tpl = EVENT_TYPES[event_type]["templates"]
    if event_type == TODO_DUE:
        due = _date(payload.get("due"))
        if due is None:
            return title, why
        facts = {"todo_id": payload.get("todo_id"), "due": due.isoformat(), "overdue": due < day}
        return _titles(TODO_DUE, None, facts, number, {})
    due_at = _dt(payload.get("internal_bid_at"))
    if due_at is None:
        return title, why
    when = _when(due_at, day)
    past = due_at.astimezone(PT).date() < day
    key = ("title_project" if number else "title") + ("_overdue" if past else "")
    title = tpl[key].format(number=number, when=when) if number else tpl[key].format(when=when)
    return title, (_bid_why(role, payload, tpl) if role else why)


def digest_line(item: Mapping[str, Any], day: date | None = None, role: Role | str | None = None) -> str:
    """One digest line: the title and why (digest_text, for the digest's
    Pacific `day` when given), marked High for an overnight high item held
    by quiet hours."""
    held_high = item.get("precedence") == "quiet_hours" and item.get("severity") in ("high", "urgent")
    prefix = "High: " if held_high else ""
    title, why = digest_text(item, day, role)
    why = why.strip()
    return f"{prefix}{title}. {why}".strip() if why else f"{prefix}{title}."


def other_updates_line(count: int, since_at: datetime | None) -> str | None:
    """The single "N other updates" line for record rows (8.2), or None."""
    if count <= 0:
        return None
    since = f" since {pt_label(since_at)}" if since_at is not None else ""
    return f"{count} other {_plural(count, 'update', 'updates')} reached your bell{since}."


def digest_lines(
    items: Iterable[Mapping[str, Any]], other_count: int = 0, since_at: datetime | None = None, *,
    day: date | None = None, role: Role | str | None = None, notices: Iterable[str] = (),
) -> list[str]:
    """The digest as plain text lines, the same in the Inbox and the email:
    every item (no render limit, owner question 16) rendered for the
    digest's Pacific `day`, then the count line, then the fixed
    auto-downgrade lines (`notices`, from downgrade_notices, 9.1)."""
    lines = [digest_line(i, day, role) for i in sorted(items, key=digest_sort_key)]
    extra = other_updates_line(other_count, since_at)
    return lines + ([extra] if extra else []) + [str(n) for n in notices]


def auto_downgrade_line(event_type: str) -> str:
    """The fixed line the next digest carries after the third not useful mark
    moves a kind to inbox only (9.1, P12)."""
    what = {
        TODO_DUE: "To-do reminders",
        BID_DUE: "Bid due reminders",
        APPROVAL_EXPIRING: "Approval expiry reminders",
    }.get(event_type, "These items")
    return f"{what} now stay in your inbox only. Change this in Settings > My assistant."


def downgrade_notices(rows: Iterable[Mapping[str, Any]], after: Any = None, upto: Any = None) -> list[str]:
    """The auto-downgrade lines one digest carries (9.1, S7-1): one per kind
    among the person's `feedback_auto` override rows written after the
    previous digest that rang its bell (`after`, None when there is none)
    and up to this digest's own `created_at` (`upto`), oldest first. The
    windows of two digests never overlap, so each downgrade is announced
    exactly once, by whichever worker builds the text, and an empty slot
    (no digest) leaves the line for the next digest that has items. A row
    the person has since changed in Settings is no longer `feedback_auto`
    and says nothing."""
    after, upto = _dt(after), _dt(upto)
    picked: list[tuple[datetime, str]] = []
    for row in rows:
        made = _dt(row.get("created_at"))
        kind = row.get("event_type")
        if (row.get("source") != "feedback_auto" or row.get("role") is not None or made is None
                or not isinstance(kind, str) or (after is not None and made <= after)
                or (upto is not None and made > upto)):
            continue
        picked.append((made, kind))
    out: list[str] = []
    for _made, kind in sorted(picked):
        line = auto_downgrade_line(kind)
        if line not in out:
            out.append(line)
    return out


def all_templates() -> list[str]:
    """Every fixed template string in the catalog, for the no em dash test."""
    out: list[str] = []
    for entry in EVENT_TYPES.values():
        out.extend(entry["templates"].values())
        out.append(entry["label"])
        out.append(entry["description"])
    out.extend(RECIPE_LABELS.values())
    return out
