"""Due-date reminder poller.

A background loop scans non-terminal projects and creates in-app notifications
as their four deadline timestamps approach (and once when they pass), honoring
each user's notification preferences (services/due_reminder_prefs).

Firing model — truthful windows, no catch-up: each offset owns the window
[due - offset, due - next_smaller_offset), computed against the FULL kind
palette (never a user's enabled subset). A project created 3 days before its
due date never gets a stale "2 weeks out" notice, and a notice can fire
anywhere inside its window — hence messages say "due within X", never "in X".
"expired" fires once, only while the due date is within a lookback horizon so
old data can't flood the bell when the poller is first enabled.

Idempotency: due_reminder_log rows are upserted with ignore-duplicates against
the 5-column unique index; notifications are created only for rows that were
genuinely inserted. A changed due date changes due_at_snapshot, re-arming every
offset for the new date. Concurrent ticks are safe via the index — no lease.
"""

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from app.core.config import get_settings
from app.core.roles import INTERNAL_ROLES, Role
from app.core.supabase_client import get_supabase
from app.services.datetime_format import _parse_ts, format_bid_datetime
from app.services import notification_email
from app.services.due_reminder_prefs import NotificationPrefsDoc, effective_prefs
from app.services.workflow import STAGES

logger = logging.getLogger(__name__)

TERMINAL_STAGES = ("submitted", "declined")
ESTIMATOR_DELIVERABLE_CATEGORIES = ("estimate", "boq", "markup")

# Descending durations; each offset's window ends where the next one starts.
TASK_PALETTE: tuple[tuple[str, timedelta], ...] = (
    ("2w", timedelta(weeks=2)),
    ("1w", timedelta(weeks=1)),
    ("2d", timedelta(days=2)),
    ("1d", timedelta(days=1)),
    ("1h", timedelta(hours=1)),
)
ACTUAL_BID_PALETTE: tuple[tuple[str, timedelta], ...] = (
    ("24h", timedelta(hours=24)),
    ("8h", timedelta(hours=8)),
    ("1h", timedelta(hours=1)),
)

_OFFSET_PHRASES = {
    "2w": "2 weeks",
    "1w": "1 week",
    "2d": "2 days",
    "1d": "1 day",
    "1h": "1 hour",
    "24h": "24 hours",
    "8h": "8 hours",
}


@dataclass(frozen=True)
class KindDef:
    key: str          # prefs key, ledger kind, notification-type segment
    column: str       # projects column holding the deadline
    label: str        # message subject ("Internal bid for …")
    verb: str         # "is"/"are" — vendor quotes are plural
    palette: tuple[tuple[str, timedelta], ...]
    has_expired: bool
    include_assigned_estimators: bool


KINDS: dict[str, KindDef] = {
    "internal_bid": KindDef(
        "internal_bid", "internal_bid_at", "Internal bid", "is",
        TASK_PALETTE, True, False,
    ),
    "due_from_estimator": KindDef(
        "due_from_estimator", "due_from_estimator_at",
        "Estimate from the estimator", "is", TASK_PALETTE, True, True,
    ),
    "due_from_vendors": KindDef(
        "due_from_vendors", "due_from_vendors_at", "Vendor quotes", "are",
        TASK_PALETTE, True, False,
    ),
    # The actual (to-GC) bid date is confidential (ACTUAL_BID_VIEWER_ROLES) and
    # these alerts are PA-only by business rule — both enforced via prefs
    # resolution plus the explicit role check in _internal_recipients.
    "actual_bid": KindDef(
        "actual_bid", "actual_bid_at", "Actual bid", "is",
        ACTUAL_BID_PALETTE, False, False,
    ),
}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def fire_key(
    due_at: datetime,
    now: datetime,
    palette: tuple[tuple[str, timedelta], ...],
    include_expired: bool,
    expired_lookback: timedelta,
) -> str | None:
    """The offset whose window contains `now`, or None.

    Pre-due windows are [due - O_i, due - O_{i+1}) with the smallest offset's
    window ending at the due date itself. Past due returns "expired" only
    within the lookback horizon.
    """
    if now >= due_at:
        if include_expired and now < due_at + expired_lookback:
            return "expired"
        return None
    for i, (key, delta) in enumerate(palette):
        start = due_at - delta
        end = due_at - palette[i + 1][1] if i + 1 < len(palette) else due_at
        if start <= now < end:
            return key
    return None


def is_complete(
    kind: str, stage: str, has_deliverables: bool, rfq_statuses: list[str]
) -> bool:
    """Whether the task tied to a deadline kind is already done (no reminder)."""
    defn = STAGES.get(stage)
    if defn is None:
        # Enum drift — fail toward reminding rather than silently going quiet.
        logger.warning("Unknown stage %r — treating reminder task as incomplete", stage)
        return False
    if kind in ("internal_bid", "actual_bid"):
        return stage in TERMINAL_STAGES
    if kind == "due_from_estimator":
        return defn.order > STAGES["to_estimator"].order or has_deliverables
    if kind == "due_from_vendors":
        return defn.order > STAGES["receive_quotes"].order or (
            bool(rfq_statuses)
            and all(s in ("quotes_in", "closed") for s in rfq_statuses)
        )
    raise ValueError(f"Unknown reminder kind {kind!r}")


def build_message(kind_def: KindDef, project: dict, offset_key: str, due_raw) -> str:
    """Self-contained text — the bell renders only `message`, never `type`."""
    label = project["name"]
    if project.get("number"):
        label = f"{label} (#{project['number']})"
    due_str = format_bid_datetime(due_raw)
    if offset_key == "expired":
        return f"{kind_def.label} for {label} {kind_def.verb} past due — was due {due_str}"
    phrase = _OFFSET_PHRASES[offset_key]
    if kind_def.key == "actual_bid":
        return f"Bid for {label} is due to the GC within {phrase} — {due_str}"
    return f"{kind_def.label} for {label} {kind_def.verb} due within {phrase} — {due_str}"


def _internal_recipients(
    kind_def: KindDef,
    offset_key: str,
    internal_profiles: list[dict],
    eff_by_user: dict[str, NotificationPrefsDoc],
) -> set[str]:
    out: set[str] = set()
    for profile in internal_profiles:
        eff = eff_by_user[profile["id"]]
        if kind_def.key == "actual_bid":
            # Hard rule, fire-time layer: effective_prefs already strips
            # actual_bid for non-PA; the role check is defense-in-depth.
            if profile["role"] != Role.PA.value or eff.actual_bid is None:
                continue
            if offset_key in eff.actual_bid.offsets:
                out.add(profile["id"])
        else:
            pref = getattr(eff, kind_def.key)
            if pref.enabled and offset_key in pref.offsets:
                out.add(profile["id"])
    return out


_PAGE = 1000


def _page_all(build) -> list[dict]:
    """Drain a query past PostgREST's silent max-rows response cap (~1000).

    `build(lo, hi)` must return a fresh ORDERED builder restricted to that
    inclusive range. Background code must not silently miss rows the way an
    interactive list page can afford to.
    """
    out: list[dict] = []
    lo = 0
    while True:
        rows = (build(lo, lo + _PAGE - 1).execute()).data or []
        out.extend(rows)
        if len(rows) < _PAGE:
            return out
        lo += _PAGE


def _pg_ts(dt: datetime) -> str:
    # Z-suffixed UTC: no '+' (which URL-decodes to a space) and no commas
    # (which delimit PostgREST or=() conditions).
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def poll_once() -> None:
    settings = get_settings()
    sb = get_supabase()
    now = _now()
    expired_lookback = timedelta(days=settings.due_reminder_expired_horizon_days)

    # 0. Non-terminal projects with at least one deadline inside the widest
    # possible window — [now - expired horizon, now + largest offset]. The SQL
    # bound keeps the candidate set small by construction (projects parked
    # forever at early stages never accumulate into the scan), and gte/lte
    # are never true for null dates. At terminal stages every kind is
    # complete, so they are excluded outright. Abandoned projects keep their
    # (non-terminal) stage but should stop nagging the team, so they're excluded
    # via `abandoned_at is null` until reactivated.
    window_lo = _pg_ts(now - expired_lookback)
    window_hi = _pg_ts(now + max(delta for _, delta in TASK_PALETTE))
    window_or = ",".join(
        f"and({k.column}.gte.{window_lo},{k.column}.lte.{window_hi})"
        for k in KINDS.values()
    )
    projects = _page_all(
        lambda lo, hi: sb.table("projects")
        .select(
            "id, name, number, current_stage, internal_bid_at, actual_bid_at, "
            "due_from_estimator_at, due_from_vendors_at"
        )
        .not_.in_("current_stage", list(TERMINAL_STAGES))
        .is_("abandoned_at", "null")
        .or_(window_or)
        .order("id")
        .range(lo, hi)
    )
    if not projects:
        return

    # 1. Window pass (pure) before touching any other table.
    candidates: list[tuple[dict, KindDef, str, str]] = []
    for project in projects:
        for kind_def in KINDS.values():
            due_raw = project.get(kind_def.column)
            if not due_raw:
                continue
            offset_key = fire_key(
                _parse_ts(due_raw), now, kind_def.palette,
                kind_def.has_expired, expired_lookback,
            )
            if offset_key:
                candidates.append((project, kind_def, offset_key, due_raw))
    if not candidates:
        return

    # 2. Completion data, batched for just the projects that need it.
    estimator_pids = sorted(
        {p["id"] for p, k, _, _ in candidates if k.key == "due_from_estimator"}
    )
    deliverable_pids: set[str] = set()
    if estimator_pids:
        rows = _page_all(
            lambda lo, hi: sb.table("project_files")
            .select("project_id")
            .in_("project_id", estimator_pids)
            .in_("category", list(ESTIMATOR_DELIVERABLE_CATEGORIES))
            .order("id")
            .range(lo, hi)
        )
        deliverable_pids = {r["project_id"] for r in rows}
    vendor_pids = sorted(
        {p["id"] for p, k, _, _ in candidates if k.key == "due_from_vendors"}
    )
    rfq_statuses_by_pid: dict[str, list[str]] = {}
    if vendor_pids:
        # Paging matters most here: a truncated response could drop a
        # project's 'sent' rows but keep its 'quotes_in' rows, flipping
        # is_complete to a false "all quotes in" and silencing the reminder.
        rows = _page_all(
            lambda lo, hi: sb.table("rfqs")
            .select("project_id, status")
            .in_("project_id", vendor_pids)
            .order("id")
            .range(lo, hi)
        )
        for r in rows:
            rfq_statuses_by_pid.setdefault(r["project_id"], []).append(r["status"])

    events = [
        (p, k, off, due)
        for p, k, off, due in candidates
        if not is_complete(
            k.key,
            p["current_stage"],
            p["id"] in deliverable_pids,
            rfq_statuses_by_pid.get(p["id"], []),
        )
    ]
    if not events:
        return

    # 3. Audience data: active profiles, stored prefs, estimator assignments.
    profiles = _page_all(
        lambda lo, hi: sb.table("profiles")
        .select("id, role")
        .eq("is_active", True)
        .order("id")
        .range(lo, hi)
    )
    internal_values = {r.value for r in INTERNAL_ROLES}
    internal_profiles = [p for p in profiles if p["role"] in internal_values]
    active_ids = {p["id"] for p in profiles}
    prefs_rows = _page_all(
        lambda lo, hi: sb.table("notification_prefs")
        .select("user_id, prefs")
        .order("user_id")
        .range(lo, hi)
    )
    prefs_map = {r["user_id"]: r["prefs"] for r in prefs_rows}
    eff_by_user = {
        p["id"]: effective_prefs(Role(p["role"]), prefs_map.get(p["id"]))
        for p in internal_profiles
    }

    # Estimators get fixed full-palette reminders via active assignments only —
    # never via prefs (and only while their account is still active).
    assignments: dict[str, set[str]] = {}
    assigned_pids = sorted(
        {p["id"] for p, k, _, _ in events if k.include_assigned_estimators}
    )
    if assigned_pids:
        rows = _page_all(
            lambda lo, hi: sb.table("estimator_assignments")
            .select("project_id, estimator_id")
            .in_("project_id", assigned_pids)
            .is_("revoked_at", "null")
            .or_("expires_at.is.null,expires_at.gt.now()")
            .order("id")
            .range(lo, hi)
        )
        for r in rows:
            if r["estimator_id"] in active_ids:
                assignments.setdefault(r["project_id"], set()).add(r["estimator_id"])

    # 4. Fan out. Each event isolated so one failure can't stall the tick.
    for project, kind_def, offset_key, due_raw in events:
        try:
            recipients = _internal_recipients(
                kind_def, offset_key, internal_profiles, eff_by_user
            )
            if kind_def.include_assigned_estimators:
                recipients |= assignments.get(project["id"], set())
            if not recipients:
                continue
            ledger_rows = [
                {
                    "project_id": project["id"],
                    "user_id": uid,
                    "kind": kind_def.key,
                    "offset_key": offset_key,
                    "due_at_snapshot": due_raw,
                }
                for uid in sorted(recipients)
            ]
            inserted = (
                sb.table("due_reminder_log")
                .upsert(
                    ledger_rows,
                    on_conflict="project_id,user_id,kind,offset_key,due_at_snapshot",
                    ignore_duplicates=True,
                )
                .execute()
            ).data or []
            if not inserted:
                continue  # every recipient already reminded for this exact due date
            message = build_message(kind_def, project, offset_key, due_raw)
            notif_rows = [
                {
                    "user_id": row["user_id"],
                    "project_id": project["id"],
                    "type": f"due.{kind_def.key}.{offset_key}",
                    "message": message,
                }
                for row in inserted
            ]
            try:
                sb.table("notifications").insert(notif_rows).execute()
            except Exception:
                # Roll the ledger back so the next tick retries — otherwise this
                # offset (possibly the final "expired" notice) is lost forever.
                sb.table("due_reminder_log").delete().in_(
                    "id", [row["id"] for row in inserted]
                ).execute()
                raise
            # Only mirror to email once the bell rows are safely persisted.
            notification_email.queue(notif_rows)
        except Exception:  # noqa: BLE001
            logger.exception(
                "Due-reminder event failed (project=%s kind=%s offset=%s)",
                project["id"], kind_def.key, offset_key,
            )


async def polling_loop() -> None:
    interval = get_settings().due_reminder_poll_interval_seconds
    while True:
        try:
            await asyncio.to_thread(poll_once)
        except Exception:  # noqa: BLE001 — the loop must survive any tick failure
            logger.exception("Due-reminder poll failed")
        await asyncio.sleep(interval)
