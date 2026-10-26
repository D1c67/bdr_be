"""Who gets which notification by email and in Teams, per role (0177).

Every notification still lands in the bell. On top of that, each internal
role has an Email switch and a Teams switch per notification EVENT (a
group of notification types, or a daily report). The defaults below were
set with G3 on 2026-10-23: Teams carries only the few things each role must
act on, email keeps everything it sent before. Executives and IT Admins
override single cells from Settings > Notification routing; an override is
one notification_channel_prefs row (NULL column = the default).

What each switch can and cannot do:
- It only filters people who already receive the event (its `audience`).
  Turning Teams on for a role outside the audience does nothing, so the
  API refuses it and the Settings grid shows those cells as not sent.
- Email is a filter on the email a notification already sends: a bell-only
  notification never gains an email by switching email on (email_available
  False marks the events that never send one).
- The person's own Teams switch (Settings > Microsoft Teams alerts) still
  wins: a role routed to Teams reaches only the people who left theirs on.

Reads are cached per process for a short time; a change in Settings takes
effect on both workers within CACHE_SECONDS.
"""

import logging
import threading
import time
from dataclasses import dataclass, field

from app.core.roles import Role
from app.core.supabase_client import get_supabase

logger = logging.getLogger(__name__)

TABLE = "notification_channel_prefs"
CACHE_SECONDS = 30.0

EXE = Role.EXECUTIVE
EA = Role.ESTIMATING_ADMIN
LAB = Role.ESTIMATING_ENGINEER_LABOR
MAT = Role.ESTIMATING_ENGINEER_MATERIALS
IT = Role.IT_ADMIN
ACC = Role.ACCOUNTANT

# The roles the grid shows, in its column order. The external estimator is
# never routed: estimators never get Teams, and their email follows their
# assignment, not a role setting.
ROUTED_ROLES: tuple[Role, ...] = (EXE, EA, LAB, MAT, IT, ACC)
ALL = frozenset(ROUTED_ROLES)

CHANNELS = ("email", "teams")


@dataclass(frozen=True)
class Event:
    key: str
    group: str
    label: str
    description: str
    audience: frozenset[Role]
    teams: frozenset[Role] = frozenset()
    # Roles in the audience whose EMAIL is off by default (the audiences
    # added on 2026-10-23 get the bell and Teams, not an email).
    email_off: frozenset[Role] = frozenset()
    email_available: bool = True
    types: tuple[str, ...] = field(default_factory=tuple)
    prefixes: tuple[str, ...] = field(default_factory=tuple)


EVENTS: tuple[Event, ...] = (
    # ── Daily reports ──
    Event(
        "digest.bids_due_today", "Daily reports", "Bids due today",
        "The 6:03 AM list of projects bidding today and what is still missing.",
        audience=frozenset({EXE, EA, LAB, MAT, IT}),
        teams=frozenset({EXE, EA, LAB, IT}),
    ),
    Event(
        "digest.auto_send_preview", "Daily reports", "Auto send preview",
        "Before the auto send: what will send today and what will not.",
        audience=frozenset({EXE, EA, IT, ACC}),
    ),
    Event(
        "digest.auto_send", "Daily reports", "Auto send results",
        "The 5:10 PM report of what auto send sent today.",
        audience=frozenset({EXE, EA, IT, ACC}),
        teams=frozenset({EA, ACC}),
    ),
    # ── Bidding ──
    Event(
        "go_no_go", "Bidding", "Waiting on Go/No-Go",
        "A project reached Go/No-Go (new bid or created from an RFP).",
        audience=frozenset({EXE, IT}),
        teams=frozenset({EXE, IT}),
        email_off=frozenset({IT}),
        types=("rfp_create.created",),
    ),
    Event(
        "intake_needed", "Bidding", "Intake details needed",
        "An RFP project needs its dates or Go/No-Go answers filled in.",
        audience=frozenset({EA}), teams=frozenset({EA}),
        types=("rfp_create.intake_needed",),
    ),
    Event(
        "rfp_review", "Bidding", "RFP emails to review",
        "RFP emails are waiting in the review queue.",
        audience=frozenset({EA}), teams=frozenset({EA}),
        types=("rfp_email.review",),
    ),
    Event(
        "send_to_estimator", "Bidding", "Accepted, send to estimator",
        "A project got a Go and is ready to go to the estimator.",
        audience=frozenset({EA}), teams=frozenset({EA}),
        types=("gono_go",),
    ),
    Event(
        "stage_handoff", "Bidding", "Project moved to your stage",
        "A project advanced to a step this role owns (not Go/No-Go or Verify).",
        audience=frozenset({EA, LAB, MAT}), teams=frozenset({EA}),
    ),
    Event(
        "estimate_submitted", "Bidding", "Estimate delivered",
        "The external estimator delivered the estimate.",
        audience=frozenset({EA, LAB, MAT}), teams=frozenset({LAB, MAT}),
        types=("estimate_submitted",),
    ),
    Event(
        "rfq_initial_send", "Bidding", "RFQs initially sent",
        "Once per project, after the first successful batch of vendor RFQs.",
        audience=frozenset({EXE, EA, IT}),
        types=("rfq.initial_send",),
    ),
    Event(
        "quote_received", "Bidding", "Vendor quotes",
        "A vendor quote or RFQ reply came in.",
        audience=frozenset({LAB, MAT}), teams=frozenset({LAB}),
        email_off=frozenset({LAB}),
        types=("quote.received", "rfq.reply_received"),
    ),
    Event(
        "late_quote", "Bidding", "Late vendor quotes",
        "A quote came in after the bid was submitted.",
        audience=frozenset({LAB, MAT}), teams=frozenset({LAB}),
        types=("late_quote.received",),
    ),
    Event(
        "verify", "Bidding", "Ready for Verify",
        "A project reached the Verify step.",
        audience=frozenset({EXE, IT}), teams=frozenset({EXE, IT}),
        email_off=frozenset({IT}),
    ),
    Event(
        "verified", "Bidding", "Numbers verified",
        "Pricing was committed at Verify and is ready to send out.",
        audience=frozenset({EA, LAB}), teams=frozenset({EA, LAB}),
        email_off=frozenset({LAB}),
        types=("verified",),
    ),
    Event(
        "call_in", "Bidding", "Calling In",
        "A project is ready for before-bid or after-bid calls.",
        audience=frozenset({EXE, LAB}), teams=frozenset({EXE}),
        types=("call_in_pre_bid", "call_in_post_bid"),
    ),
    Event(
        "scope_lines_review", "Bidding", "Review scope lines",
        "Auto send drafted scope lines that need a check.",
        audience=frozenset({LAB}),
        types=("auto_send.lines_review",),
    ),
    Event(
        "auto_send_sent", "Bidding", "Auto sent",
        "Auto send sent a proposal.",
        audience=frozenset({EXE, EA, IT, ACC}), teams=frozenset({EA}),
        email_available=False,
        types=("auto_send.sent",),
    ),
    Event(
        "auto_send_problem", "Bidding", "Auto send problem",
        "Auto send could not send, or did not run.",
        audience=frozenset({EXE, EA, IT, ACC}), teams=frozenset({EA}),
        types=("auto_send.failed", "auto_send.missed"),
    ),
    Event(
        "proposal_send_failed", "Bidding", "Proposal send failed",
        "A proposal email to a GC failed.",
        audience=frozenset({EA}), teams=frozenset({EA}),
        types=("proposal_send_failed",),
    ),
    Event(
        "submitted", "Bidding", "Bid submitted",
        "Proposals went out for a project.",
        audience=frozenset({EXE, LAB, MAT}),
        types=("submitted",),
    ),
    Event(
        "bid_outcome", "Bidding", "Win / loss",
        "A bid outcome was recorded.",
        audience=frozenset({EXE, LAB, MAT}),
        types=("bid_outcome",),
    ),
    Event(
        "project_abandoned", "Bidding", "Project abandoned",
        "A project was abandoned.",
        audience=frozenset({EXE}),
        types=("project_abandoned",),
    ),
    Event(
        "due", "Bidding", "Deadline reminders",
        "Due-date reminders (internal bid, estimator due, vendor due).",
        audience=ALL, prefixes=("due.",),
    ),
    # ── Approvals ──
    Event(
        "approval_requested", "Approvals", "Approval requested",
        "Someone asked for approval: a GC price change or Mark submitted permission.",
        audience=frozenset({EXE, EA, IT}), teams=frozenset({EXE}),
        types=("gc_pricing.approval_requested", "external_submission.requested"),
    ),
    Event(
        "revision_approval", "Approvals", "Revised proposal approval",
        "Costs changed in an open revised proposal and need an Executive's approval.",
        audience=frozenset({EXE}), teams=frozenset({EXE}),
        types=("proposal_revision.approval_requested",),
    ),
    # ── Revisions ──
    Event(
        "revision_round", "Revisions", "Estimator revisions",
        "The estimator sent changes after delivering the estimate.",
        audience=frozenset({EXE, EA, LAB, MAT}), teams=frozenset({EXE, EA, LAB, MAT}),
        types=("estimate_revised",),
    ),
    Event(
        "drawing_changed", "Revisions", "Drawings changed",
        "Project drawings or addenda changed.",
        audience=frozenset({LAB, MAT}), teams=frozenset({LAB, MAT}),
        types=("drawing_changed",),
    ),
    Event(
        "reverify_required", "Revisions", "Re-verify required",
        "Pricing changed after Verify and needs verifying again.",
        audience=frozenset({EXE, LAB}), teams=frozenset({EXE, LAB}),
        types=("reverify_required",),
    ),
    # ── Personal ──
    Event(
        "personal", "Personal", "Mentions, nudges and decisions",
        "Aimed at one person: an @mention in a note, a nudge, an approval decision.",
        audience=ALL, teams=ALL,
        types=(
            "note_mention",
            "nudge",
            "gc_pricing.approved",
            "gc_pricing.rejected",
            "external_submission.granted",
            "external_submission.denied",
            "external_submission.revoked",
            "external_submission.undone",
        ),
    ),
    Event(
        "estimator_note", "Personal", "Project notes",
        "A new note on a project you follow (no @mention).",
        audience=ALL, types=("estimator_note",),
    ),
    # ── System ──
    Event(
        "tech_outage", "System", "Tech problems",
        "The AI model is away or back, an RFP item failed, email or portal "
        "ingestion failed, BuildingConnected disconnected, the AI spend cap hit.",
        audience=frozenset({EXE, IT}), teams=frozenset({IT}),
        types=(
            "rfp_bc.disconnected",
            "rfp_bc.scan_failed",
            "rfp_bc.account_changed",
            "rfp_ngem.login_failed",
            "rfp_ngem.scan_failed",
            "rfp_harvest.login_failed",
            "ai_spend.cap_hit",
        ),
        prefixes=("rfp_processing.", "email_ingest."),
    ),
    Event(
        "security_alert", "System", "Security alerts",
        "Unusual account or file activity.",
        audience=frozenset({IT}), teams=frozenset({IT}),
        types=("security_alert",),
    ),
    # ── Other ──
    Event(
        "pm", "Other", "Project Management",
        "Project Management stage changes and won projects.",
        audience=ALL, prefixes=("pm_",),
    ),
    Event(
        "other", "Other", "Everything else",
        "Any notification not listed above (RFP invitations, merges, submittals, finished jobs).",
        audience=ALL,
    ),
)

EVENT_BY_KEY: dict[str, Event] = {e.key: e for e in EVENTS}
_BY_TYPE: dict[str, str] = {t: e.key for e in EVENTS for t in e.types}
_BY_PREFIX: tuple[tuple[str, str], ...] = tuple(
    (p, e.key) for e in EVENTS for p in e.prefixes
)
# stage_handoff splits by the stage it announces (metadata.stage).
_HANDOFF_EVENTS = {"go_no_go": "go_no_go", "verify": "verify"}
# The handoffs the IT Admin hears on top of the stage owner (2026-10-23).
IT_ADMIN_HANDOFF_STAGES = frozenset(_HANDOFF_EVENTS)


def event_for(type_: str | None, metadata: dict | None = None) -> str:
    """The routing event a notification row belongs to."""
    type_ = type_ or ""
    if type_ == "stage_handoff":
        stage = (metadata or {}).get("stage")
        return _HANDOFF_EVENTS.get(str(stage), "stage_handoff")
    if type_ in _BY_TYPE:
        return _BY_TYPE[type_]
    for prefix, key in _BY_PREFIX:
        if type_.startswith(prefix):
            return key
    return "other"


def default(event: Event, role: Role, channel: str) -> bool:
    if role not in event.audience:
        return False
    if channel == "teams":
        return role in event.teams
    return event.email_available and role not in event.email_off


# ── Overrides (cached) ───────────────────────────────────────────────────────

_cache_lock = threading.Lock()
_cache: tuple[float, dict[tuple[str, str], dict]] | None = None


def _overrides() -> dict[tuple[str, str], dict]:
    """(role, event) -> {"email": bool|None, "teams": bool|None}. A failed
    read falls back to the defaults (and is retried on the next call)."""
    global _cache
    with _cache_lock:
        if _cache and time.monotonic() - _cache[0] < CACHE_SECONDS:
            return _cache[1]
    try:
        rows = (get_supabase().table(TABLE).select("role, event, email, teams").execute()).data or []
    except Exception:  # noqa: BLE001 - routing must never break a notification
        logger.warning("Notification routing read failed; using defaults", exc_info=True)
        return {}
    found = {(r["role"], r["event"]): r for r in rows}
    with _cache_lock:
        _cache = (time.monotonic(), found)
    return found


def clear_cache() -> None:
    global _cache
    with _cache_lock:
        _cache = None


def wants(role: str | Role | None, event_key: str, channel: str) -> bool:
    """Whether `role` gets `event_key` on `channel` ("email" or "teams").
    Roles outside the grid (the estimator) are not routed here: True, so the
    callers' own rules decide (Teams already refuses estimators)."""
    try:
        role = Role(role) if role else None
    except ValueError:
        role = None
    event = EVENT_BY_KEY.get(event_key)
    if role is None or role not in ROUTED_ROLES or event is None:
        return True
    # These oversight roles hear only the initial RFQ send by email. Keep
    # this explicit because the compatibility fallback below permits email
    # outside an event's audience (including an older queued bell row).
    if (
        channel == "email"
        and role in {EXE, EA, IT}
        and event_key in {"quote_received", "late_quote"}
    ):
        return False
    if role not in event.audience:
        # Only reachable when code sends an event to a role the catalog does
        # not list: keep today's behavior for email, never add Teams.
        return channel == "email"
    value = (_overrides().get((role.value, event_key)) or {}).get(channel)
    return default(event, role, channel) if value is None else bool(value)


def wants_row(role: str | Role | None, row: dict, channel: str) -> bool:
    """`wants` for a notification row."""
    return wants(role, event_for(row.get("type"), row.get("metadata")), channel)


# ── Settings grid ────────────────────────────────────────────────────────────


def grid() -> dict:
    overrides = _overrides()
    events = []
    for e in EVENTS:
        cells = {}
        for role in ROUTED_ROLES:
            if role not in e.audience:
                continue
            row = overrides.get((role.value, e.key)) or {}
            cell = {}
            for ch in CHANNELS:
                d = default(e, role, ch)
                value = row.get(ch)
                cell[ch] = d if value is None else bool(value)
                cell[f"{ch}_default"] = d
            cells[role.value] = cell
        events.append(
            {
                "key": e.key,
                "group": e.group,
                "label": e.label,
                "description": e.description,
                "email_available": e.email_available,
                "cells": cells,
            }
        )
    return {"roles": [r.value for r in ROUTED_ROLES], "events": events}


class RoutingError(ValueError):
    """A grid write that cannot apply (shown to the user as a 400)."""


def set_cell(role: str, event_key: str, channel: str, enabled: bool, actor_id: str) -> None:
    try:
        r = Role(role)
    except ValueError:
        raise RoutingError("Unknown role") from None
    event = EVENT_BY_KEY.get(event_key)
    if r not in ROUTED_ROLES or event is None or channel not in CHANNELS:
        raise RoutingError("Unknown role, event or channel")
    if r not in event.audience:
        raise RoutingError("This role does not receive that notification")
    if channel == "email" and not event.email_available:
        raise RoutingError("That notification never sends an email")
    sb = get_supabase()
    existing = (
        sb.table(TABLE).select("email, teams").eq("role", r.value).eq("event", event_key)
        .limit(1).execute()
    ).data or []
    row = {"email": None, "teams": None, **(existing[0] if existing else {})}
    # Back at the default = NULL, so a later change of the default reaches it.
    row[channel] = None if enabled == default(event, r, channel) else enabled
    if row["email"] is None and row["teams"] is None:
        sb.table(TABLE).delete().eq("role", r.value).eq("event", event_key).execute()
    else:
        sb.table(TABLE).upsert(
            {
                "role": r.value,
                "event": event_key,
                "email": row["email"],
                "teams": row["teams"],
                "updated_by": actor_id,
                "updated_at": "now()",
            },
            on_conflict="role,event",
        ).execute()
    clear_cache()


def reset_all() -> None:
    get_supabase().table(TABLE).delete().neq("role", "").execute()
    clear_cache()
