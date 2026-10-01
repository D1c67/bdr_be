"""RFP Ingestion: the BuildingConnected Bid Board portal
(docs/RFP_BUILDINGCONNECTED.md; scratchpad BUILD_CONTRACT.md section 3.4).

The second entry behind `rfp_portal_ingest.PORTALS`: the same
rfp_portal_invitations / rfp_portal_runs tables, the same sweep, the same
human actions and the same matcher, with everything BuildingConnected-shaped
kept here and reached through duck-typed hooks the generic code looks up
with `_hook` (NGEM defines none of them and keeps its behaviour byte for
byte):

  scheduler   `due_slots`: the newest 15-minute incremental slot (no catch-up
              burst after downtime) and the 02:30 Pacific full sync inside
              its 4-hour catch-up window (D3); one queue job type, the run
              row's `kind` says which (D2).
  scan        `scan`: the API pull (incremental on updatedAt since the high
              water mark minus the overlap, or the whole board), the upsert
              by opportunity id with the entry rule on insert (3.3), the
              payload hash short-circuit, the tracked-field change log and
              its bell, the scan-time package sibling link (D12a), the
              system ignore for declined / archived rows (D24), and, on a
              full sync only, missing marking, withdrawal and expiry (3.9).
              The high water mark moves only from the attempt that won the
              run's completion (`after_complete`). The pull is worked one
              API page (100 rows) at a time: one select loads the page's
              stored rows, the page's unchanged rows (same payload hash)
              share one chunked update of their seen stamps, a changed row
              costs one update, a new row one insert; the sibling index
              loads once, on the first insert that needs it, and the
              missing marking is a set difference in Python written in
              chunks. A board of 2,500 rows is a few dozen round trips,
              never one per row.
  match       `facts_for`, `gc_for`, `route_params`, `refine`, `on_exists`
              and the small status hooks: the GC resolved through the
              alias table (gc_aliases), an unconfirmed GC parks every
              confident match for a person (D11), no candidate means
              `create` (nothing to harvest), an NDA-masked row parks before
              scoring, a merge links the GC, the lead contact and the files
              flag on the project (D13, D19).
  create      `before_create` / `sibling_guard`: a package sibling that
              already has a project is linked instead of created (D12b);
              the auto-create gate parks rows that must not auto-create (D18).
  actions     `status`, `confirm_gc`, `apply_dates`, `restore` for the
              routers; `remove_link` for reopen.

The scan never writes `status` on a known row except through the CAS
primitives (system ignore, the NDA un-masking, a due date appearing on a
`no_due_date` row, a GC swapped on the board, expiry, withdrawal); the
resolution columns (gc_*, project_gc_id, sibling_of) belong to the match
step and the GC card and a later pull touches them only when the board
swaps the GC company (cleared on an unlinked row, reopened as a question
on a linked one; gc_id and project_gc_id of a linked row are never
rewritten). Tokens, the
client secret and the Settings object never reach a log line or a row
other than rfp_oauth_connections (bc_client's rule); the raw `payload` is
stored and read back only through the detail select, where the router
redacts it by role (D33).

Everything here uses the sync Supabase client: the queue worker, the
scheduler thread and plain `def` routes, never an `async def`.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

from app.core.config import Settings, get_settings
from app.core.roles import RFP_REVIEW_ROLES, Role
from app.services import bc_client, bc_facts, gc_aliases, rfp_create, rfp_email_ingest, rfp_match
from app.services import rfp_portal_ingest as pi
from app.services import rfp_test

logger = logging.getLogger(__name__)

COMPANY_TZ = pi.COMPANY_TZ

PORTAL = pi.PORTAL_BUILDINGCONNECTED
INVITATIONS_TABLE = pi.INVITATIONS_TABLE
STATE_TABLE = "rfp_portal_state"
KIND_INCREMENTAL = pi.RUN_KIND_INCREMENTAL
KIND_FULL = pi.RUN_KIND_FULL

NOTIFY_NEW_INVITATIONS = "rfp_bc.new_invitations"
NOTIFY_INVITATION_CHANGED = "rfp_bc.invitation_changed"
NOTIFY_SCAN_FAILED = "rfp_bc.scan_failed"
NOTIFY_DISCONNECTED = bc_client.NOTIFY_DISCONNECTED

# Flag reasons this portal adds to the vocabulary (D25); the FE catalogs
# carry the same keys under rfpPortal.flagReason.
FLAG_NDA_REQUIRED = "nda_required"
FLAG_NO_DUE_DATE = "no_due_date"
FLAG_SUBMITTED_UNMATCHED = "submitted_unmatched"
FLAG_SIBLING_PACKAGE = "sibling_package"
FLAG_NO_PROJECT_NAME = rfp_match.REASON_NO_PROJECT_NAME
FLAG_GC_UNRESOLVED = rfp_match.REASON_GC_UNRESOLVED

# Error codes carried by RfpPortalError from this module; the values equal
# the ErrorCode members of the same name (contract 3.6).
CODE_NOT_CONNECTED = "rfp_bc_not_connected"
CODE_GC_REQUIRED = "rfp_portal_gc_required"
CODE_DATES_UNCHANGED = "rfp_portal_dates_unchanged"

IGNORE_SOURCE_USER = "user"
IGNORE_SOURCE_SYSTEM = "system"

# The statuses the nightly pass may expire or withdraw (3.9): the pipeline
# plus `done` (no project yet; the Create button acts there). Rows with a
# project are never touched by the system, whatever their status.
PIPELINE_STATUSES = (pi.STATUS_MATCH, pi.STATUS_REVIEW_MATCH, pi.STATUS_CREATE, pi.STATUS_DONE)
# What apply-dates may write (D20); never internal_bid_at.
APPLY_DATE_FIELDS = ("actual_bid_at", "job_walk_at", "est_start_date", "est_finish_date")

_CHANGE_LOG_CAP = pi._CHANGE_LOG_CAP
_RENEW_EVERY_ROWS = 100
_NOTES_HTML_CAP = 20_000
_ERROR_MAX_CHARS = pi._ERROR_MAX_CHARS
_FIELD_LABELS = {
    "close_at": "due date",
    "job_walk_at": "job walk",
    "expected_start_at": "expected start",
    "expected_finish_at": "expected finish",
    "title": "name",
    "submission_state": "state",
    "is_archived": "archived",
    "address": "location",
    "gc_external_id": "GC company",
    "gc_external_name": "GC company name",
    "gc_alias": "GC alias",
}
# The tracked fields whose move rings rfp_bc.invitation_changed (design
# 3.6: "due date, job walk, start or finish moved", plus the GC swapped
# on the board, fix round 3). Every other tracked change (a rename of the
# same company included) goes to change_log and the project block only.
BELL_FIELDS = ("close_at", "job_walk_at", "expected_start_at", "expected_finish_at", "gc_external_id")
_DATE_FIELDS = ("close_at", "job_walk_at", "expected_start_at", "expected_finish_at")
# What a board GC change clears on an unlinked row so the next match step
# resolves the new company from scratch (gc_for keeps a confirmed GC, so
# the confirmation goes too).
_GC_RESOLUTION_CLEAR = {
    "gc_id": None,
    "gc_kind": None,
    "gc_candidates": None,
    "gc_confirmed_at": None,
    "gc_confirmed_by": None,
}
# The statuses an unlinked row leaves for `match` when its GC changed on
# the board (the match step is the one place the GC is resolved).
_REMATCH_ON_GC_CHANGE = (pi.STATUS_REVIEW_MATCH, pi.STATUS_CREATE, pi.STATUS_DONE)

_MSG_NOT_CONFIGURED = "BuildingConnected is not configured (client id and secret)."
_MSG_NOT_CONNECTED = "BuildingConnected is not connected. Connect it from RFP ingestion settings."
_MSG_NOT_BC = "This invitation did not come from BuildingConnected."
_MSG_GC_MASKED = "BuildingConnected does not name the GC on this invitation (an NDA masks it)."
_MSG_GC_PICK = "Pick the GC this BuildingConnected company is, or add one."
_MSG_GC_GONE = "That GC no longer exists."
_MSG_GC_CONFIRM_FIRST = (
    "Confirm which GC this BuildingConnected company is (the GC card) before marking the "
    "invitation as the same project."
)
_MSG_NO_PROJECT = "This invitation is not linked to a project."
_MSG_NO_DATES = "None of the picked dates has a value on the invitation."
_MSG_PICK_DATES = "Pick at least one date to apply."
_REASON_DECLINED = "declined in BuildingConnected"
_REASON_ARCHIVED = "archived in BuildingConnected"
_REASON_EXPIRED = "due date passed on {day}"
_REASON_WITHDRAWN = "absent from the Bid Board since {day}"
# The note a run completes with when the pull stopped at RFP_BC_MAX_PAGES
# before the end of the board: an incomplete view marks nothing missing,
# expires or withdraws nothing and never moves the high water mark.
_MSG_TRUNCATED = (
    "The {kind} pull stopped at the RFP_BC_MAX_PAGES cap ({pages} pages) before the end of the "
    "Bid Board: unseen rows were not marked missing, nothing was expired or withdrawn and the "
    "high water mark did not move. Raise the cap and run again."
)

# The columns the scan reads for every stored row (the tracked fields, the
# hash, the link state); never the payload (the hash stands in for it).
_SCAN_SELECT = (
    "id, external_id, bid_number, status, flag_reason, payload_hash, seen_count, change_log, "
    "missing_since, match_project_id, created_project_id, gc_external_id, gc_external_name, gc_id, "
    "gc_kind, title, agency, "
    "bid_number_raw, close_at, job_walk_at, expected_start_at, expected_finish_at, "
    "submission_state, is_archived, address, is_nda_required, first_seen_at, sibling_of"
)
_SWEEP_STATE_SELECT = (
    "id, status, close_at, missing_since, match_project_id, created_project_id, title"
)
# The columns the full sync's missing marking needs for every stored row
# (the set difference against the pull is computed in Python).
_MISSING_SELECT = "id, external_id, bid_number, status, missing_since"
# A value safe inside a PostgREST `or=(...)` filter without quoting: the
# platform's 24-hex company id and a GC's UUID both pass; anything else
# falls back to the single-column filter.
_SAFE_FILTER_TOKEN = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_SIBLING_SELECT = (
    "id, title, gc_id, gc_external_id, match_project_id, created_project_id, status, first_seen_at"
)


# ── Small helpers ────────────────────────────────────────────────────────────


def _now() -> datetime:
    return pi._now()


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt else None


def _parse_ts(value: Any) -> datetime | None:
    return bc_facts.parse_ts(value)


def _project_id(row: dict) -> str | None:
    """The project a row is really linked to: the one it created, or the
    one it sits on at `exists` / `created`. A `match_project_id` on any
    other status is the matcher's best candidate (the match step stores it
    on review_match rows too) and never a link: the sibling rules, the
    nightly pass, the system ignore, the changed bell, the GC card and
    apply-dates all key on this and not on the raw column."""
    created = row.get("created_project_id")
    if created:
        return str(created)
    if row.get("status") in (pi.STATUS_EXISTS, pi.STATUS_CREATED) and row.get("match_project_id"):
        return str(row["match_project_id"])
    return None


def _linked(row: dict) -> bool:
    return _project_id(row) is not None


_LINKED_STATUSES = (pi.STATUS_EXISTS, pi.STATUS_CREATED)


def _files_needed():
    """The files flag service (agent F's module), imported on use: this
    module must load, and the merge must still link the GC, before it
    lands. Tests replace this seam."""
    try:
        from app.services import files_needed
    except ImportError:  # pragma: no cover - the slice may land after this one
        logger.warning("rfp bc: files_needed service is not available; the files flag is skipped")
        return None
    return files_needed


def _ensure_lead_contact(sb, gc_id: str, lead: dict | None) -> tuple[str | None, bool]:
    """rfp_create.ensure_lead_contact (D16) through the module attribute so
    the tests patch it and a build without it degrades to no contact."""
    fn = getattr(rfp_create, "ensure_lead_contact", None)
    if fn is None:  # pragma: no cover - the slice may land after this one
        logger.warning("rfp bc: rfp_create.ensure_lead_contact is not available; no lead contact attached")
        return None, False
    return fn(sb, gc_id, lead)


def _remove_gc_link(project_id: str, gc_id: str, *, link_id: str, refuse_if_sent: bool, via_unmerge: bool) -> bool:
    """proposal_send.remove_gc_link, imported on use (the proposal service
    pulls in the whole sending stack). A refusal (a proposal went to the
    GC) becomes the portal's own error."""
    from app.services import proposal_send

    try:
        return bool(
            proposal_send.remove_gc_link(
                project_id, gc_id, link_id=link_id, refuse_if_sent=refuse_if_sent, via_unmerge=via_unmerge
            )
        )
    except proposal_send.ProposalSendError as exc:
        raise pi.RfpPortalError(pi.CODE_NOT_ACTIONABLE, str(exc) or "The GC link cannot be removed.") from exc


def _swap_project_gc(sb, project_id: str, old_gc_id: str | None, new_gc_id: str, lead: dict | None, actor_id) -> dict:
    fn = getattr(rfp_create, "swap_project_gc", None)
    if fn is None:  # pragma: no cover - the slice may land after this one
        logger.warning("rfp bc: rfp_create.swap_project_gc is not available; the project keeps its GC")
        return {"removed_old": False, "project_gc_id": None}
    return fn(sb, project_id, old_gc_id, new_gc_id, lead, actor_id)


def _apply_project_dates(sb, project_id: str, patch: dict, actor_id, *, source: str) -> dict:
    fn = getattr(rfp_create, "apply_project_dates", None)
    if fn is None:  # pragma: no cover - the slice may land after this one
        raise pi.RfpPortalError(pi.CODE_NOT_AVAILABLE, "Applying dates is not available yet.", http_status=503)
    return fn(sb, project_id, patch, actor_id, source=source)


# ── State (rfp_portal_state) ─────────────────────────────────────────────────


def load_state(sb) -> dict | None:
    rows = (sb.table(STATE_TABLE).select("*").eq("portal", PORTAL).limit(1).execute()).data or []
    return rows[0] if rows else None


def _save_state(sb, fields: dict) -> None:
    payload = {"portal": PORTAL, **fields, "updated_at": _iso(_now())}
    sb.table(STATE_TABLE).upsert(payload, on_conflict="portal").execute()


# ── Scheduler (D3) ───────────────────────────────────────────────────────────


def incremental_slot(now: datetime, settings: Settings) -> datetime:
    """floor(now, RFP_BC_POLL_MINUTES): the instant floored to the grid
    (the grid divides the hour, so the floor is the same in any zone)."""
    minutes = max(1, int(settings.rfp_bc_poll_minutes))
    utc = now.astimezone(timezone.utc)
    return utc.replace(minute=(utc.minute // minutes) * minutes, second=0, microsecond=0)


def full_sync_slots(now: datetime, settings: Settings) -> list[datetime]:
    """Yesterday's and today's RFP_BC_FULL_SYNC_TIME as Pacific wall-clock
    instants, sorted (yesterday's so the catch-up window survives midnight)."""
    hour, minute = settings.rfp_bc_full_sync_slot
    local = now.astimezone(COMPANY_TZ)
    out = []
    for day in (local.date() - timedelta(days=1), local.date()):
        slot = datetime(day.year, day.month, day.day, hour, minute, tzinfo=COMPANY_TZ)
        out.append(slot.astimezone(timezone.utc))
    return sorted(out)


def due_slots(now: datetime, settings: Settings) -> list[tuple[datetime, str]]:
    """The slots to claim at `now`: the full sync slot inside its catch-up
    window (first, so it wins the active index over the grid when both are
    due) and the newest incremental slot only (the catch-up window of the
    grid is one slot: no burst after downtime)."""
    out: list[tuple[datetime, str]] = []
    window = timedelta(hours=int(settings.rfp_bc_full_sync_catchup_hours))
    for slot in full_sync_slots(now, settings):
        if slot <= now < slot + window:
            out.append((slot, KIND_FULL))
    out.append((incremental_slot(now, settings), KIND_INCREMENTAL))
    return out


def next_slots(now: datetime, settings: Settings) -> tuple[datetime, datetime | None]:
    """(next incremental instant, next full sync instant) for the status."""
    minutes = max(1, int(settings.rfp_bc_poll_minutes))
    next_incremental = incremental_slot(now, settings) + timedelta(minutes=minutes)
    hour, minute = settings.rfp_bc_full_sync_slot
    local = now.astimezone(COMPANY_TZ)
    next_full: datetime | None = None
    for day in (local.date(), local.date() + timedelta(days=1)):
        slot = datetime(day.year, day.month, day.day, hour, minute, tzinfo=COMPANY_TZ).astimezone(timezone.utc)
        if slot > now:
            next_full = slot
            break
    return next_incremental, next_full


# ── Bells (D7) ───────────────────────────────────────────────────────────────


def notify_new(sb, run_id: str, count: int) -> int:
    """One bell per review-role user for this run (deduped per run)."""
    if count <= 0:
        return 0
    noun = "invitation" if count == 1 else "invitations"
    return pi._notify_roles_once(
        sb, RFP_REVIEW_ROLES, NOTIFY_NEW_INVITATIONS,
        pi._bell_message(f"{count} new BuildingConnected {noun}"),
        {"run_id": run_id, "count": count, "portal": PORTAL},
        key="run_id",
    )


def _describe_value(field: str, value: Any) -> str:
    if value is None or value == "":
        return "none"
    if field in _DATE_FIELDS:
        dt = _parse_ts(value)
        return rfp_match.format_pacific(dt, has_time=True) if dt else pi._bell_text(value)
    return pi._bell_text(value)


def _describe_change(change: dict) -> str:
    """One change as bell text. The GC change names the companies: the
    `gc_external_id` entry the scan hands the bell carries `old_name` /
    `new_name` beside the ids (the change log itself keeps the ids), and a
    bare id stands in when a name is missing."""
    field = change.get("field")
    if field == "gc_external_id":
        old = change.get("old_name") or change.get("old")
        new = change.get("new_name") or change.get("new")
        return f"GC changed: {_describe_value(field, old)} to {_describe_value(field, new)}"
    if field == "gc_external_name":
        return (
            f"GC name changed: {_describe_value(field, change.get('old'))} to "
            f"{_describe_value(field, change.get('new'))}"
        )
    if field == "gc_alias":
        # gc_aliases.repoint / delete: old and new are BDR GC ids (new is
        # none on a delete).
        return (
            f"GC alias changed: {_describe_value(field, change.get('old'))} to "
            f"{_describe_value(field, change.get('new'))}"
        )
    label = _FIELD_LABELS.get(field, field)
    return f"{label} {_describe_value(field, change.get('old'))} -> {_describe_value(field, change.get('new'))}"


def _refresh_unread_changed(sb, invitation_id: str, message: str, metadata: dict) -> set[str]:
    """Every unread, undismissed rfp_bc.invitation_changed bell for this
    invitation rewritten in place to the latest change (message, metadata,
    and created_at so it reads as the newest bell). Returns the users whose
    bell was refreshed; they get no second row."""
    rows = (
        sb.table("notifications")
        .update({"message": message, "metadata": metadata, "created_at": _iso(_now())})
        .eq("type", NOTIFY_INVITATION_CHANGED)
        .is_("read_at", "null")
        .is_("dismissed_at", "null")
        .eq("metadata->>invitation_id", str(invitation_id))
        .execute()
    ).data or []
    return {str(r["user_id"]) for r in rows if r.get("user_id")}


def notify_changed(sb, row: dict, changes: list[dict], run_id: str) -> int:
    """The GC moved something on a row linked to a project: one bell per
    Estimating Admin. A user who still holds an unread bell for the same
    invitation gets that bell rewritten to describe this change (a second
    move is never silent, and the text is never stale); everyone else gets
    a new row. Returns how many rows were added."""
    what = "; ".join(_describe_change(c) for c in changes)
    title = pi._bell_text(row.get("title"), pi._BELL_TITLE_MAX_CHARS)
    head = f"BuildingConnected invitation changed: {pi._bell_text(row.get('agency'))}"
    if title:
        head = f"{head} ({title})"
    message = pi._bell_message(f"{head}: {what}.")
    metadata = {
        "invitation_id": row["id"],
        "run_id": run_id,
        "portal": PORTAL,
        "project_id": _project_id(row),
        "fields": [c.get("field") for c in changes],
    }
    refreshed = _refresh_unread_changed(sb, row["id"], message, metadata)
    added = 0
    for user_id in pi._role_user_ids(sb, (Role.ESTIMATING_ADMIN,)):
        if user_id in refreshed:
            continue
        pi.notify_user(user_id, None, NOTIFY_INVITATION_CHANGED, message, mirror_email=False, metadata=metadata)
        added += 1
    return added


def notify_scan_failed(sb, run: dict, error: str | None) -> None:
    """One bell to every IT Admin when a scan run ends failed, deduped while
    an unread one exists (per type). Wrapped by the caller."""
    if pi._unread_pending(sb, NOTIFY_SCAN_FAILED):
        return
    detail = pi._bell_text(error or pi._MSG_RUN_LOST, pi._BELL_MESSAGE_MAX_CHARS // 2)
    pi.notify_role(
        Role.IT_ADMIN,
        None,
        NOTIFY_SCAN_FAILED,
        pi._bell_message(
            f"The BuildingConnected scan failed: {detail} Check the BuildingConnected block in "
            "RFP ingestion settings."
        ),
        mirror_email=False,
        metadata={
            "run_id": run.get("id"),
            "kind": run.get("kind"),
            "trigger": run.get("trigger"),
            "portal": PORTAL,
            "error": (error or "")[:_ERROR_MAX_CHARS],
        },
    )


# ── System transitions (D24, 3.9) ────────────────────────────────────────────


def system_ignore(sb, row: dict, reason: str) -> bool:
    """A pipeline row the GC declined or archived: `ignored` by the system
    (ignored_by null, ignore_source system); unignore tolerates it."""
    expected = row.get("status")
    if expected not in (pi.STATUS_MATCH, pi.STATUS_REVIEW_MATCH):
        return False
    return pi._cas(sb, row["id"], expected, {
        "status": pi.STATUS_IGNORED,
        "ignore_source": IGNORE_SOURCE_SYSTEM,
        "ignored_by": None,
        "ignored_at": _iso(_now()),
        "ignore_reason": reason,
        "decided_at_step": expected,
        "next_attempt_at": None,
    })


def expire_and_withdraw(sb, settings: Settings, now: datetime, run_id: str | None) -> tuple[int, int]:
    """The nightly pass: unlinked pipeline rows more than RFP_BC_EXPIRE_DAYS
    past due park as `expired`; rows absent from the board for
    RFP_BC_MISSING_DAYS park as `withdrawn`. Both restorable. Rows with a
    project are never touched. Returns (expired, withdrawn)."""
    expire_before = now - timedelta(days=int(settings.rfp_bc_expire_days))
    missing_before = now - timedelta(days=int(settings.rfp_bc_missing_days))
    rows = pi._page_all(
        lambda: sb.table(INVITATIONS_TABLE)
        .select(_SWEEP_STATE_SELECT)
        .eq("portal", PORTAL)
        .in_("status", list(PIPELINE_STATUSES))
        .order("id", desc=False)
    )
    expired = withdrawn = 0
    for row in rows:
        if _linked(row):
            continue
        close_at = _parse_ts(row.get("close_at"))
        if close_at is not None and close_at < expire_before:
            day = bc_facts.pacific_date(close_at)
            moved = pi._cas(sb, row["id"], row["status"], {
                "status": pi.STATUS_EXPIRED,
                "last_error": _REASON_EXPIRED.format(day=day.isoformat() if day else "?"),
                "decided_at_step": row["status"],
                "next_attempt_at": None,
            })
            expired += int(moved)
            continue
        missing = _parse_ts(row.get("missing_since"))
        if missing is not None and missing <= missing_before:
            day = bc_facts.pacific_date(missing)
            moved = pi._cas(sb, row["id"], row["status"], {
                "status": pi.STATUS_WITHDRAWN,
                "last_error": _REASON_WITHDRAWN.format(day=day.isoformat() if day else "?"),
                "decided_at_step": row["status"],
                "next_attempt_at": None,
            })
            withdrawn += int(moved)
    if expired or withdrawn:
        logger.info("rfp bc: run %s expired %s and withdrew %s invitation(s)", run_id, expired, withdrawn)
    return expired, withdrawn


def _linked_gc_reopen(row: dict) -> dict:
    """The GC columns of a LINKED row whose company changed on the board:
    the confirmation cleared, the GC the project holds kept in gc_id (the
    GC card swaps from it) as a provisional guess, the stale candidates
    dropped."""
    out: dict = {"gc_confirmed_at": None, "gc_confirmed_by": None, "gc_candidates": []}
    if row.get("gc_id"):
        out["gc_kind"] = gc_aliases.KIND_PROVISIONAL
    return out


# ── Package siblings (D12) ───────────────────────────────────────────────────


def _linked_rows(sb) -> list[dict]:
    """Every BuildingConnected row that sits on a project (`_linked`), in
    id order, from two indexed selects: the rows at `exists` / `created`
    and the rows that created a project (whatever their status now)."""
    by_id: dict[str, dict] = {}
    factories = (
        lambda: sb.table(INVITATIONS_TABLE)
        .select(_SIBLING_SELECT)
        .eq("portal", PORTAL)
        .in_("status", list(_LINKED_STATUSES))
        .order("id", desc=False),
        lambda: sb.table(INVITATIONS_TABLE)
        .select(_SIBLING_SELECT)
        .eq("portal", PORTAL)
        .not_.is_("created_project_id", "null")
        .order("id", desc=False),
    )
    for factory in factories:
        for row in pi._page_all(factory):
            by_id.setdefault(str(row.get("id")), row)
    return [by_id[key] for key in sorted(by_id)]


class _SiblingIndex:
    """(gc_external_id, normalized name) -> the row with a project (at
    `exists` or `created`, never a review_match candidate), over the
    portal's linked rows plus the ones the scan inserts (D12a). The table
    is read once, on the first lookup: a pull with nothing to insert never
    reads it. The oldest row (by id) leads a key."""

    def __init__(self, sb=None, rows: list[dict] | None = None) -> None:
        self._sb = sb
        self._by_key: dict[tuple[str, str], dict] = {}
        self._loaded = rows is not None
        for row in rows or []:
            self._add(row)

    @staticmethod
    def key(gc_external_id: Any, title: Any) -> tuple[str, str] | None:
        gc = str(gc_external_id or "").strip()
        name = bc_facts.normalize_name(title)
        if not gc or not name:
            return None
        return gc, name

    def _ensure_loaded(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        for row in _linked_rows(self._sb):
            self._add(row)

    def _add(self, row: dict) -> None:
        if not _linked(row):
            return
        key = self.key(row.get("gc_external_id"), row.get("title"))
        if key is not None and key not in self._by_key:
            self._by_key[key] = row

    def add(self, row: dict) -> None:
        self._ensure_loaded()
        self._add(row)

    def find(self, gc_external_id: Any, title: Any) -> dict | None:
        key = self.key(gc_external_id, title)
        if key is None:
            return None
        self._ensure_loaded()
        return self._by_key.get(key)


def _sibling_fields(sibling: dict, now: datetime, decided_at_step: str) -> dict:
    return {
        "status": pi.STATUS_EXISTS,
        "match_project_id": _project_id(sibling),
        "flag_reason": FLAG_SIBLING_PACKAGE,
        "resolution": "system",
        "resolved_by": None,
        "resolved_at": _iso(now),
        "sibling_of": sibling.get("id"),
        "decided_at_step": decided_at_step,
        "next_attempt_at": None,
        "last_error": None,
    }


def find_sibling(sb, row: dict) -> dict | None:
    """Another BuildingConnected row with the same normalized name and the
    same GC (by gc_id or by the platform's company id) that already has a
    project (at `exists` or `created`: a review_match row's candidate is
    not a project); the oldest wins (D12b)."""
    name = bc_facts.normalize_name(row.get("title"))
    if not name:
        return None
    gc_id = row.get("gc_id")
    external = str(row.get("gc_external_id") or "").strip()
    if not gc_id and not external:
        return None
    query = (
        sb.table(INVITATIONS_TABLE)
        .select(_SIBLING_SELECT)
        .eq("portal", PORTAL)
        .neq("id", row["id"])
        .in_("status", list(_LINKED_STATUSES))
    )
    if external and gc_id and _SAFE_FILTER_TOKEN.match(external) and _SAFE_FILTER_TOKEN.match(str(gc_id)):
        # D12b keys on (gc_id OR gc_external_id): two BuildingConnected
        # company records aliased to one GC are the same GC, and a twin
        # keyed by the company id counts even before its gc_id is resolved.
        query = query.or_(f"gc_external_id.eq.{external},gc_id.eq.{gc_id}")
    elif external:
        query = query.eq("gc_external_id", external)
    else:
        query = query.eq("gc_id", gc_id)
    hits = []
    for candidate in (query.execute()).data or []:
        if not _linked(candidate):
            continue
        if bc_facts.normalize_name(candidate.get("title")) != name:
            continue
        same_gc = (external and str(candidate.get("gc_external_id") or "") == external) or (
            gc_id and str(candidate.get("gc_id") or "") == str(gc_id)
        )
        if same_gc:
            hits.append(candidate)
    if not hits:
        return None
    hits.sort(key=lambda r: (str(r.get("first_seen_at") or ""), str(r.get("id") or "")))
    return hits[0]


# ── The portal adapter ───────────────────────────────────────────────────────


class BuildingConnectedPortal:
    """The `buildingconnected` entry of rfp_portal_ingest.PORTALS."""

    key = PORTAL

    def __init__(self) -> None:
        self._pages: int | None = None

    # -- the generic adapter surface ------------------------------------------

    def configured(self, settings: Settings) -> bool:
        return bool(getattr(settings, "bc_configured", False))

    def account(self, settings: Settings) -> str | None:
        try:
            conn = bc_client.connection_status(pi.get_supabase())
        except Exception:  # noqa: BLE001 - the status block must not fail on the read
            logger.debug("rfp bc: connection read failed", exc_info=True)
            return None
        return conn.get("external_user_name") or conn.get("external_user_email")

    def entry_url(self, settings: Settings) -> str | None:
        return None

    def availability(self, settings: Settings) -> tuple[bool, str | None, datetime | None]:
        """(usable, reason, None) from the connection row: configured and
        connected, or not."""
        if not self.configured(settings):
            return False, _MSG_NOT_CONFIGURED, None
        conn = bc_client.connection_status(pi.get_supabase())
        if conn.get("status") != "connected":
            return False, conn.get("last_error") or _MSG_NOT_CONNECTED, None
        return True, None, None

    def _on_page(self) -> None:
        """The client's `renew` seam: before every page after the first,
        the queue lease and the page count."""
        if self._pages is not None:
            self._pages += 1
        pi._renew()

    def open_session(self, settings: Settings):
        self._pages = 0
        return bc_client.BcClient(
            pi.get_supabase(), bc_client.config_from_settings(settings), renew=self._on_page
        )

    def classify(self, exc: BaseException) -> str:
        if isinstance(exc, bc_client.BcDisconnected):
            return pi.KIND_UNAVAILABLE
        if isinstance(exc, (bc_client.BcRateLimited, bc_client.BcTransient)):
            return pi.KIND_TRANSIENT
        if isinstance(exc, bc_client.BcPermanent):
            return pi.KIND_PERMANENT
        if isinstance(exc, httpx.TransportError):
            return pi.KIND_TRANSIENT
        return pi.KIND_UNKNOWN

    def is_client_error(self, exc: BaseException) -> bool:
        return isinstance(exc, bc_client.BcError)

    def is_stale_link(self, exc: BaseException) -> bool:
        return False

    def parse_bid_number(self, raw: Any) -> tuple[str | None, int | None]:
        text = str(raw or "").strip()
        return (text or None), None

    def unavailable_error(self, reason: str | None, until: datetime | None) -> pi.RfpPortalError:
        """Run now while disconnected: 409 rfp_bc_not_connected."""
        return pi.RfpPortalError(CODE_NOT_CONNECTED, reason or _MSG_NOT_CONNECTED, http_status=409)

    # -- the scan (3.2) -------------------------------------------------------

    def scan(self, sb, settings: Settings, run: dict, session, renew) -> tuple[int, dict]:
        """The API pull and the upsert. Returns (pages, counts) for
        `_complete_run`: seen / rows_pulled = results; new = rows entering
        the tab's active lanes (match, or exists through a sibling link);
        skipped_closed = mirror-only inserts (historical); changed = known
        rows whose tracked fields moved; rows_pipeline = inserts at match;
        rows_expired / rows_withdrawn from the nightly pass (full sync);
        high_water_before / high_water_at for the state row.

        The request budget, per API page of 100 rows: one select for the
        page's stored rows, then one insert per new row, one update per
        changed row, and ONE chunked update for all the page's unchanged
        rows together (their `last_seen_at` / `last_seen_run_id` move and
        a missing mark is cleared, so "Last seen" stays truthful and the
        withdrawal rule sees a returned row). `seen_count` is the one stamp
        a bulk write cannot carry (a per-row increment), so it counts the
        pulls that WROTE the row (insert, change), not every pull that saw
        it. The sibling index is read once per scan, on the first insert
        that needs it; a full sync adds one narrow paged select for the
        missing marking and the nightly pass's own select."""
        now = _now()
        kind = run.get("kind") or KIND_INCREMENTAL
        state = load_state(sb) or {}
        high_water = _parse_ts(state.get("high_water_at"))
        text_max = int(settings.rfp_bc_text_max_chars)
        updated_since: datetime | None = None
        if kind == KIND_INCREMENTAL and high_water is not None:
            updated_since = high_water - timedelta(minutes=int(settings.rfp_bc_overlap_minutes))
        siblings = _SiblingIndex(sb)
        counts: dict[str, Any] = {
            "seen": 0, "new": 0, "changed": 0, "skipped_closed": 0,
            "rows_pulled": 0, "rows_pipeline": 0, "rows_expired": 0, "rows_withdrawn": 0,
            "high_water_before": high_water, "high_water_at": now, "kind": kind,
        }
        seen_ids: set[str] = set()
        logger.info(
            "rfp bc: run %s %s scan starting (since %s)",
            run.get("id"), kind, _iso(updated_since) or "the beginning",
        )
        stream = session.iter_opportunities(updated_since=updated_since, max_pages=int(settings.rfp_bc_max_pages))
        for page in _batched(stream, bc_client.PAGE_LIMIT_MAX):
            fresh: list[tuple[str, dict]] = []
            for opp in page:
                counts["rows_pulled"] += 1
                if counts["rows_pulled"] % _RENEW_EVERY_ROWS == 0 and renew is not None:
                    renew()
                oid = str(opp.get("id") or "").strip()
                if not oid or oid in seen_ids:
                    continue
                seen_ids.add(oid)
                fresh.append((oid, opp))
            stored = _stored_rows(sb, [oid for oid, _opp in fresh])
            unchanged: list[str] = []
            for oid, opp in fresh:
                fields = bc_facts.invitation_fields(opp, now, text_max_chars=text_max)
                current = stored.get(oid)
                if current is None:
                    if self._insert(sb, run, opp, fields, now, siblings, counts) is not None:
                        continue
                    current = _find_by_external_id(sb, oid)
                    if current is None:
                        continue
                if current.get("payload_hash") and current.get("payload_hash") == fields.get("payload_hash"):
                    # The hash short-circuit: nothing moved, no write of
                    # its own; the seen stamps land below in one chunk
                    # with the page's other unchanged rows.
                    unchanged.append(current["id"])
                    continue
                if self._update(sb, run, current, opp, fields, now, text_max):
                    counts["changed"] += 1
            self._touch_seen(sb, run, unchanged, now)
        counts["seen"] = counts["rows_pulled"]
        truncated = bool(getattr(session, "truncated", False))
        counts["truncated"] = truncated
        if truncated:
            # An incomplete view of the board: what was pulled is upserted,
            # but nothing unseen is missing, nothing is expired or
            # withdrawn on it and the high water mark stays where the run
            # started; the run completes with the note (last_error) and the
            # scan log carries it.
            note = _MSG_TRUNCATED.format(kind=kind, pages=int(settings.rfp_bc_max_pages))
            counts["last_error"] = note
            counts["high_water_at"] = high_water
            logger.warning("rfp bc: run %s: %s", run.get("id"), note)
        elif kind == KIND_FULL:
            self._mark_missing(sb, _unseen_rows(sb, seen_ids), now)
            expired, withdrawn = expire_and_withdraw(sb, settings, now, run.get("id"))
            counts["rows_expired"], counts["rows_withdrawn"] = expired, withdrawn
        if self._pages is not None:
            pages = self._pages + 1
        else:
            pages = max(1, -(-int(counts["rows_pulled"]) // bc_client.PAGE_LIMIT_MAX))
        return pages, counts

    def _insert(self, sb, run: dict, opp: dict, fields: dict, now: datetime, siblings: _SiblingIndex, counts: dict) -> dict | None:
        status = bc_facts.entry_status(opp, now)
        extra: dict = {}
        if status == pi.STATUS_MATCH:
            sibling = siblings.find(fields.get("gc_external_id"), fields.get("title"))
            if sibling is not None:
                status = pi.STATUS_EXISTS
                extra = _sibling_fields(sibling, now, pi.STATUS_MATCH)
        inserted = pi._insert_invitation(sb, run, {**fields, **extra}, now, status=status)
        if inserted is None:
            return None
        if status == pi.STATUS_MATCH:
            counts["new"] += 1
            counts["rows_pipeline"] += 1
        elif status == pi.STATUS_EXISTS:
            counts["new"] += 1
            siblings.add(inserted)
        else:
            counts["skipped_closed"] += 1
        return inserted

    def _update(self, sb, run: dict, current: dict, opp: dict, fields: dict, now: datetime, text_max: int) -> bool:
        """A known row whose payload moved (the hash short-circuit in `scan`
        keeps an unchanged row away from here; `_touch_seen` stamps those):
        the seen bookkeeping (seen_count included, the per-row write is the
        one place it can increment), the mirror, the volatile columns and
        the change log in one update; then the status transitions the pull
        implies (`_unlinked_transitions`: system ignore, NDA un-masking, a
        due date appearing, a GC swapped on the board) through their own
        CAS. A GC swap on a linked row reopens the GC question on the
        project instead (gc_confirm_pending) and rings the change bell with
        the date moves. Returns True when tracked fields changed."""
        update: dict = {
            "last_seen_at": _iso(now),
            "last_seen_run_id": run["id"],
            "seen_count": int(current.get("seen_count") or 0) + 1,
            "missing_since": None,
        }
        changes = bc_facts.tracked_changes(current, opp, text_max_chars=text_max)
        update.update(fields)
        entries = [
            {"at": _iso(now), "field": c["field"], "old": c["old"], "new": c["new"], "run_id": run["id"]}
            for c in changes
        ]
        if entries:
            log = list(current.get("change_log") or []) + entries
            update["change_log"] = log[-_CHANGE_LOG_CAP:]
        status = current.get("status")
        linked = _linked(current)
        # The GC swapped on the board (a new company id; a rename of the
        # same company is `gc_external_name` alone and only logged).
        gc_changed = any(e["field"] == "gc_external_id" for e in entries)
        if gc_changed and linked and fields.get("gc_external_id"):
            # A linked row: the project keeps its GC (nothing is rewritten
            # silently); the question opens again on the project (the GC
            # card, gc_confirm_pending) and on the row (no longer confirmed,
            # the old GC is now a guess for the new company, the candidates
            # were scored for the old one). The project flag is written
            # before the row so a failure leaves the payload hash unmoved
            # and the next pull tries again.
            update.update(_linked_gc_reopen(current))
            sb.table("projects").update({"gc_confirm_pending": True}).eq("id", _project_id(current)).execute()
        sb.table(INVITATIONS_TABLE).update(update).eq("id", current["id"]).execute()
        if not linked:
            self._unlinked_transitions(sb, current, fields, gc_changed)
        # The bell is for date moves and a GC swap (design 3.6, fix round
        # 3): a renamed, archived, re-stated or relocated invitation still
        # logs the change and shows it on the project block, but rings
        # nobody. The GC entry carries the company names for the text.
        rung: list[dict] = []
        for entry in entries:
            if entry.get("field") not in BELL_FIELDS:
                continue
            if entry.get("field") == "gc_external_id":
                entry = {
                    **entry,
                    "old_name": current.get("gc_external_name"),
                    "new_name": fields.get("gc_external_name"),
                }
            rung.append(entry)
        if rung and linked and status != pi.STATUS_IGNORED:
            try:
                notify_changed(sb, {**current, **fields}, rung, run["id"])
            except Exception:  # noqa: BLE001 - the bell must never fail the scan
                logger.exception("rfp bc: change bell failed for %s", current.get("id"))
        return bool(entries)

    def _unlinked_transitions(self, sb, current: dict, fields: dict, gc_changed: bool) -> None:
        """The status moves a pull implies for a row with no project, each
        through its own CAS on the status read: declined or archived ->
        `ignored` by the system (pipeline rows only, unchanged); a
        review_match row parked for a missing detail that the board now
        has (the NDA lifted, or a due date appeared on a `no_due_date`
        row) -> back to `match`; the GC swapped on the board -> the GC
        resolution cleared (gc_for keeps a confirmed GC, so the
        confirmation goes too) and a review_match / create / done row back
        to `match`, where the sweep resolves the new company. A row at
        `match` only loses its resolution (the sweep picks it up as it
        is); an ignored or parked row keeps its status and is resolved
        afresh if it is ever restored."""
        status = current.get("status")
        closed_reason = None
        if fields.get("submission_state") == bc_facts.STATE_DECLINED:
            closed_reason = _REASON_DECLINED
        elif fields.get("is_archived"):
            closed_reason = _REASON_ARCHIVED
        rematch = False
        if closed_reason and status in (pi.STATUS_MATCH, pi.STATUS_REVIEW_MATCH):
            system_ignore(sb, current, closed_reason)
        elif status == pi.STATUS_REVIEW_MATCH and current.get("flag_reason") == FLAG_NDA_REQUIRED and fields.get("gc_external_id"):
            # The NDA lifted: the details are there now, match again.
            rematch = True
        elif (
            status == pi.STATUS_REVIEW_MATCH
            and current.get("flag_reason") == FLAG_NO_DUE_DATE
            and _parse_ts(current.get("close_at")) is None
            and _parse_ts(fields.get("close_at")) is not None
        ):
            # The GC added the due date: match again (the refinement that
            # parked it no longer holds).
            rematch = True
        elif gc_changed and not closed_reason and status in _REMATCH_ON_GC_CHANGE:
            rematch = True
        clear = dict(_GC_RESOLUTION_CLEAR) if gc_changed else {}
        if rematch:
            pi._cas(
                sb, current["id"], status,
                {"status": pi.STATUS_MATCH, "flag_reason": None, "decided_at_step": None, **pi._RESET, **clear},
                extra={"created_project_id": None} if clear else None,
            )
        elif clear:
            # Whatever the status is now, as long as the row still has no
            # project (a create that won the race keeps its GC).
            (
                sb.table(INVITATIONS_TABLE)
                .update(clear)
                .eq("id", current["id"])
                .is_("created_project_id", "null")
                .neq("status", pi.STATUS_EXISTS)
                .neq("status", pi.STATUS_CREATED)
                .execute()
            )

    def _mark_missing(self, sb, rows: list[dict], now: datetime) -> None:
        """The stored rows a complete full pull did not return: missing
        since now, in chunks of at most 200 ids, never one write per row.
        Ignored rows and rows already marked are left alone."""
        ids = [r["id"] for r in rows if r.get("status") != pi.STATUS_IGNORED and not r.get("missing_since")]
        for chunk in pi._chunks(ids):
            sb.table(INVITATIONS_TABLE).update({"missing_since": _iso(now)}).in_("id", chunk).is_(
                "missing_since", "null"
            ).execute()

    def _touch_seen(self, sb, run: dict, ids: list[str], now: datetime) -> None:
        """The seen stamps of one API page's unchanged rows in one update
        per chunk of at most 200 ids (a page is one request): last_seen_at
        and last_seen_run_id move, a missing mark is cleared (the
        withdrawal rule keys on it, so the clear cannot wait for the payload
        to move). seen_count is not here: a bulk write cannot increment per
        row, so it stays the count of the pulls that wrote the row."""
        if not ids:
            return
        fields = {"last_seen_at": _iso(now), "last_seen_run_id": run["id"], "missing_since": None}
        for chunk in pi._chunks(ids):
            sb.table(INVITATIONS_TABLE).update(fields).in_("id", chunk).execute()

    def after_complete(self, sb, settings: Settings, run: dict, counts: dict) -> None:
        """Only from the attempt that won `_complete_run`: the high water
        mark and the last-success stamps on rfp_portal_state. A pull cut
        short by the page cap (`counts["truncated"]`) is not a success:
        the state row is left as it was."""
        if counts.get("truncated"):
            logger.warning(
                "rfp bc: run %s completed on an incomplete pull; the high water mark and the "
                "last-success stamps are left as they were", run.get("id"),
            )
            return
        fields: dict = {"high_water_at": _iso(counts.get("high_water_at"))}
        if (counts.get("kind") or run.get("kind") or KIND_INCREMENTAL) == KIND_FULL:
            fields["last_full_sync_at"] = _iso(_now())
        else:
            fields["last_incremental_at"] = _iso(_now())
        _save_state(sb, fields)

    def notify_new(self, sb, run_id: str, count: int) -> int:
        return notify_new(sb, run_id, count)

    def notify_scan_failed(self, sb, run: dict, error: str | None) -> None:
        notify_scan_failed(sb, run, error)

    # -- the match step hooks (3.5, D11) ---------------------------------------

    def auto_merge_enabled(self, settings: Settings) -> bool:
        return bool(getattr(settings, "rfp_bc_auto_resolve_enabled", True))

    def no_match_status(self) -> str:
        return pi.STATUS_CREATE

    def no_name_status(self) -> str:
        return pi.STATUS_REVIEW_MATCH

    def has_name(self, title: Any) -> bool:
        return bool(rfp_match.has_project_name(title))

    def pre_match_park(self, row: dict) -> tuple[str, str] | None:
        if row.get("is_nda_required") and not row.get("gc_external_id"):
            return pi.STATUS_REVIEW_MATCH, FLAG_NDA_REQUIRED
        return None

    def gc_from_row(self, row: dict) -> gc_aliases.GcResolve | None:
        """The GC as the row carries it (a confirmed one, or the last
        resolution), for the human paths that do not resolve again."""
        gc_id = row.get("gc_id")
        kind = row.get("gc_kind") or (gc_aliases.KIND_NONE if not gc_id else gc_aliases.KIND_PROVISIONAL)
        if row.get("gc_confirmed_at") and gc_id:
            kind = gc_aliases.KIND_ALIAS
        candidates = row.get("gc_candidates")
        return gc_aliases.GcResolve(
            kind=kind, gc_id=str(gc_id) if gc_id else None, gc_name=None, contact_id=None,
            candidates=[c for c in candidates if isinstance(c, dict)] if isinstance(candidates, list) else [],
        )

    def guard_resolve_exists(self, row: dict) -> None:
        """"Same as project" on a row whose GC is still a guess (provisional,
        or none while the platform names the company) is refused with
        rfp_portal_gc_required: the GC card in the same dialog answers first
        (D11), so a merge never files the lead under a similarity guess. An
        NDA-masked row (no company to confirm) resolves as a marker only."""
        if not str(row.get("gc_external_id") or "").strip():
            return
        gc = self.gc_from_row(row)
        if gc is not None and gc.kind in gc_aliases.CONFIRMED_KINDS and gc.gc_id:
            return
        raise pi.RfpPortalError(CODE_GC_REQUIRED, _MSG_GC_CONFIRM_FIRST)

    def gc_for(self, sb, row: dict, bundle, settings: Settings) -> gc_aliases.GcResolve:
        """The alias table first, the lead's address, then the most similar
        name (gc_aliases.resolve). A GC a person confirmed on the row is
        taken as is (the sweep never rewrites it)."""
        if row.get("gc_confirmed_at") and row.get("gc_id"):
            return self.gc_from_row(row)
        lead = row.get("lead") if isinstance(row.get("lead"), dict) else {}
        return gc_aliases.resolve(
            sb,
            source=gc_aliases.SOURCE_BC,
            external_id=row.get("gc_external_id"),
            external_name=row.get("gc_external_name"),
            lead_email=lead.get("email"),
            bundle=bundle,
            settings=settings,
        )

    def facts_for(self, row: dict) -> rfp_match.ExtractedFacts:
        close_at = _parse_ts(row.get("close_at"))
        payload = row.get("payload") if isinstance(row.get("payload"), dict) else {}
        trade = bc_facts.html_to_text(bc_facts.effective(payload, "tradeSpecificInstructions"), max_chars=_NOTES_HTML_CAP)
        info = bc_facts.html_to_text(bc_facts.effective(payload, "projectInformation"), max_chars=_NOTES_HTML_CAP)
        notes = trade or info
        if notes and len(notes) > bc_facts.BID_NOTES_MAX_CHARS:
            notes = notes[: bc_facts.BID_NOTES_MAX_CHARS].rstrip()
        return rfp_match.ExtractedFacts(
            project_name=row.get("title"),
            gc_name=row.get("gc_external_name"),
            bid_due_at=close_at,
            has_time=bool(close_at) and not bc_facts.is_midnight_pacific(close_at),
            bid_notes=notes or None,
            reasoning="",
        )

    def route_params(self, gc: gc_aliases.GcResolve | None, best_for_gc: dict | None, projects_by_id: dict, settings: Settings) -> dict:
        gc_id = gc.gc_id if gc is not None else None
        resolved = gc is not None and gc.kind in gc_aliases.CONFIRMED_KINDS and bool(gc_id)
        on_project = bool(best_for_gc) and rfp_email_ingest._gc_in_links(
            projects_by_id.get(best_for_gc["project_id"]) or {}, gc_id
        )
        return {
            "gc_resolved": resolved,
            "gc_on_project": on_project,
            "sender_verified": True,
            "auto_merge": self.auto_merge_enabled(settings),
        }

    def refine(self, row: dict, status: str, flag_reason: str | None) -> tuple[str, str | None]:
        """D11: a `create` outcome is withheld for a row with no due date or
        one already submitted in the portal; an `exists` is never overridden."""
        if status != pi.STATUS_CREATE:
            return status, flag_reason
        if not row.get("close_at"):
            return pi.STATUS_REVIEW_MATCH, FLAG_NO_DUE_DATE
        if row.get("submission_state") == bc_facts.STATE_SUBMITTED:
            return pi.STATUS_REVIEW_MATCH, FLAG_SUBMITTED_UNMATCHED
        return status, flag_reason

    def on_exists(
        self, sb, row: dict, project_id: str, gc: gc_aliases.GcResolve | None, settings: Settings, *,
        merged: bool, bundle: dict | None = None,
    ) -> dict:
        """D13 and D19 after an `exists`: the project must still be open
        (rfp_email_ingest._open_project raises RfpMatchError otherwise); a
        merge whose GC is confirmed (alias, contact or domain, D11) and new
        to the project inserts a plain project_gcs link (needs_by = the
        Pacific day of the due date), attaches the lead as the GC bid
        contact and stores the link id on the row; a provisional guess is
        never linked (the GC card answers first); then the files flag when
        the project has no drawing or specification."""
        project = rfp_email_ingest._open_project(sb, project_id, settings)
        out = {"project_gc_id": None, "contact_id": None, "files_needed": False}
        if not merged:
            return out
        if gc is None:
            gc = self.gc_from_row(row)
        gc_id = gc.gc_id if gc.kind in gc_aliases.CONFIRMED_KINDS else None
        if gc_id and not rfp_email_ingest._gc_in_links(project, str(gc_id)):
            needs_by = rfp_create._pacific_date(row.get("close_at"))
            link_id = rfp_email_ingest._insert_link(sb, project_id, str(gc_id), needs_by, None)
            if link_id:
                # The link id lands on the row at once, before the contact
                # steps, so a failure there never strands a project_gcs
                # row that reopen (which keys on project_gc_id) could not
                # take back; if that write itself fails the fresh link is
                # removed again and the failure propagates.
                try:
                    sb.table(INVITATIONS_TABLE).update({"project_gc_id": link_id}).eq("id", row["id"]).execute()
                except Exception:
                    logger.exception(
                        "rfp bc: %s could not store its link %s on project %s; removing the link",
                        row.get("id"), link_id, project_id,
                    )
                    sb.table("project_gcs").delete().eq("id", link_id).eq("project_id", project_id).execute()
                    raise
                out["project_gc_id"] = link_id
                rfp_email_ingest._bundle_add_link(bundle, project_id, str(gc_id), link_id, needs_by)
                try:
                    contact_id, _inserted = _ensure_lead_contact(sb, str(gc_id), row.get("lead"))
                    if contact_id:
                        rfp_email_ingest._insert_ignore(
                            sb, "project_gc_contacts",
                            {"project_gc_id": link_id, "gc_contact_id": contact_id},
                            on_conflict="project_gc_id,gc_contact_id",
                        )
                        out["contact_id"] = contact_id
                except Exception:  # noqa: BLE001 - the bid contact is best effort; the link stands
                    logger.exception(
                        "rfp bc: %s linked GC %s to project %s but the lead contact could not be attached",
                        row.get("id"), gc_id, project_id,
                    )
        files = _files_needed()
        if files is not None:
            try:
                if not files.project_has_files(sb, project_id):
                    files.set_needed(sb, project_id, source=PORTAL, url=row.get("external_url"))
                    out["files_needed"] = True
            except Exception:  # noqa: BLE001 - the flag is best effort (D19)
                logger.exception("rfp bc: files flag failed for project %s", project_id)
        return out

    # -- the create step hooks (D12b, D18) --------------------------------------

    def sibling_guard(self, sb, row: dict) -> bool:
        """A package sibling with a project: this row links to it (`exists`,
        sibling_package) instead of creating a second project. True when
        the row moved."""
        sibling = find_sibling(sb, row)
        if sibling is None:
            return False
        expected = row.get("status")
        if expected not in (pi.STATUS_CREATE, pi.STATUS_DONE):
            return False
        moved = pi._cas(sb, row["id"], expected, _sibling_fields(sibling, _now(), expected))
        if moved:
            logger.info("rfp bc: %s linked to project %s as a package sibling of %s", row["id"], _project_id(sibling), sibling.get("id"))
        return moved

    def before_create(self, sb, row: dict, settings: Settings) -> bool:
        """The sweep's create step, before anything is made: the sibling
        guard, then (automatic creation on) the auto-create gate: no due
        date, no GC at all or an NDA-masked row parks for a person. A row a
        person sent here through "Not a match" is never gated again."""
        if self.sibling_guard(sb, row):
            return True
        if not settings.rfp_create_auto_enabled or row.get("resolution") == "human":
            return False
        park: str | None = None
        if row.get("flag_reason") == FLAG_NDA_REQUIRED or (row.get("is_nda_required") and not row.get("gc_external_id")):
            park = FLAG_NDA_REQUIRED
        elif not row.get("close_at"):
            park = FLAG_NO_DUE_DATE
        elif (row.get("gc_kind") or gc_aliases.KIND_NONE) == gc_aliases.KIND_NONE:
            park = FLAG_GC_UNRESOLVED
        if park is None:
            return False
        return pi._cas(sb, row["id"], pi.STATUS_CREATE, {
            "status": pi.STATUS_REVIEW_MATCH,
            "flag_reason": park,
            "decided_at_step": pi.STATUS_CREATE,
            **pi._RESET,
        })

    # -- reopen (D13) ---------------------------------------------------------

    def remove_link(self, sb, row: dict) -> str | None:
        """The project_gcs link a merge added, removed by its id; refused
        (RfpPortalError) when a proposal went to that GC. Returns the id
        removed, or None when nothing was there to remove."""
        project_id, gc_id, link_id = row.get("match_project_id"), row.get("gc_id"), row.get("project_gc_id")
        if not (project_id and gc_id and link_id):
            return None
        removed = _remove_gc_link(str(project_id), str(gc_id), link_id=str(link_id), refuse_if_sent=True, via_unmerge=True)
        return str(link_id) if removed else None


def _batched(stream, size: int):
    """The client's row iterator in lists of at most `size` rows (one API
    page at PAGE_LIMIT_MAX). A failure in the pull (a 429 that outlived its
    retries, a lost connection, a lost lease) is raised only after the rows
    already pulled were handed over, so the pages that did arrive are
    upserted before the run parks or retries: the same progress a
    row-at-a-time loop kept."""
    batch: list[dict] = []
    failure: Exception | None = None
    try:
        for row in stream:
            batch.append(row)
            if len(batch) >= size:
                yield batch
                batch = []
    except Exception as exc:  # noqa: BLE001 - re-raised below, after the flush
        failure = exc
    if batch:
        yield batch
    if failure is not None:
        raise failure


def _stored_rows(sb, external_ids: list[str]) -> dict[str, dict]:
    """The stored rows for one API page's opportunity ids, by external id:
    one select per 200 ids over rfp_portal_invitations_external_idx
    (a page of 100 is one request)."""
    out: dict[str, dict] = {}
    for chunk in pi._chunks(external_ids):
        rows = (
            sb.table(INVITATIONS_TABLE)
            .select(_SCAN_SELECT)
            .eq("portal", PORTAL)
            .in_("external_id", chunk)
            .execute()
        ).data or []
        for row in rows:
            key = str(row.get("external_id") or "").strip()
            if key:
                out.setdefault(key, row)
    return out


def _unseen_rows(sb, seen_ids: set[str]) -> list[dict]:
    """The stored rows a complete full pull did not return: one narrow
    paged select of the portal's rows, the set difference in Python. A
    row with neither an external id nor a bid number cannot be matched to
    the pull and is never marked."""
    rows = pi._page_all(
        lambda: sb.table(INVITATIONS_TABLE).select(_MISSING_SELECT).eq("portal", PORTAL).order("id", desc=False)
    )
    unseen: list[dict] = []
    for row in rows:
        key = str(row.get("external_id") or row.get("bid_number") or "").strip()
        if key and key not in seen_ids:
            unseen.append(row)
    return unseen


def _find_by_external_id(sb, external_id: str) -> dict | None:
    """The stored row for one opportunity, after an insert lost the unique
    race to a concurrent attempt (the only per-row read left in the scan)."""
    rows = (
        sb.table(INVITATIONS_TABLE)
        .select(_SCAN_SELECT)
        .eq("portal", PORTAL)
        .eq("external_id", external_id)
        .limit(1)
        .execute()
    ).data or []
    return rows[0] if rows else None


# ── Status (contract section 4) ──────────────────────────────────────────────


def _files_needed_projects(sb) -> int:
    try:
        resp = (
            sb.table("projects")
            .select("id", count="exact", head=True)
            .eq("files_needed_source", PORTAL)
            .not_.is_("files_needed_set_at", "null")
            .is_("files_needed_cleared_at", "null")
            .execute()
        )
        return int(resp.count) if resp.count is not None else len(resp.data or [])
    except Exception:  # noqa: BLE001 - a count, never the status block
        logger.debug("rfp bc: files-needed count failed", exc_info=True)
        return 0


def status(sb, settings: Settings | None = None) -> dict:
    """GET /rfp-portal/buildingconnected/status: the connection (never the
    tokens), the switches, the schedule, the state row, the runs and the
    lane counts. `paused_by_test_session` is true while an RFP test session
    is active: rfp_portal_ingest.poll_once returns before claiming any slot
    then, so the scheduled scans never fire. Same call poll_once makes
    (rfp_test.active_session), free while rfp_testing_enabled is off."""
    s = settings or get_settings()
    connection = bc_client.connection_status(sb)
    connected_by = connection.get("connected_by")
    names = pi._profile_names(sb, {connected_by})
    connection["connected_by"] = (
        {"id": str(connected_by), "name": names.get(str(connected_by))} if connected_by else None
    )
    state = load_state(sb) or {}
    now = _now()
    last_run = pi._latest_run(sb, PORTAL)
    active_run = pi._active_run(sb, PORTAL)
    run_names = pi._profile_names(sb, {(r or {}).get("requested_by") for r in (last_run, active_run)})
    next_incremental, next_full = next_slots(now, s)
    active = bool(getattr(s, "rfp_bc_active", False))
    counts = pi.view_counts(sb, PORTAL)
    counts["files_needed_projects"] = _files_needed_projects(sb)
    paused = rfp_test.active_session(sb) is not None
    return {
        "connection": connection,
        "configured": bool(getattr(s, "bc_configured", False)),
        "enabled": bool(s.rfp_ingest_enabled and getattr(s, "rfp_bc_enabled", False)),
        "active": active,
        "redirect_url": getattr(s, "rfp_bc_redirect_url", "") or None,
        "poll_minutes": int(s.rfp_bc_poll_minutes),
        "full_sync_time": s.rfp_bc_full_sync_time,
        "next_incremental_at": _iso(next_incremental) if active else None,
        "next_full_sync_at": _iso(next_full) if active and next_full else None,
        "high_water_at": state.get("high_water_at"),
        "last_full_sync_at": state.get("last_full_sync_at"),
        "last_incremental_at": state.get("last_incremental_at"),
        "last_run": pi._run_view(last_run, run_names),
        "active_run": pi._run_view(active_run, run_names),
        "counts": counts,
        "paused_by_test_session": paused,
    }


# ── Human actions ────────────────────────────────────────────────────────────


def restore(sb, invitation_id: str, actor_id: str) -> dict:
    """D23: historical / expired / withdrawn -> match. Generic, so it lives
    in rfp_portal_ingest; this is the portal-side name."""
    return pi.restore(sb, invitation_id, actor_id)


def _require_bc(sb, invitation_id: str) -> dict:
    row = pi._get(sb, invitation_id)
    if row.get("portal") != PORTAL:
        raise pi.RfpPortalError(pi.CODE_NOT_ACTIONABLE, _MSG_NOT_BC)
    return row


# The change-log fields that reopen a linked row's GC question on its
# project (gc_confirm_pending): the company swapped on the board (the scan,
# fix round 3) and the alias behind the row repointed or deleted
# (gc_aliases.repoint / delete).
GC_QUESTION_FIELDS = ("gc_external_id", "gc_alias")


def open_gc_question_at(row: dict) -> datetime | None:
    """When the row's own GC question was (last) raised, while it is still
    open: the newest GC_QUESTION_FIELDS entry in its change log, when that
    is newer than the row's GC confirmation (or the row has none). None
    when nothing raised a question, the card answered it since, or the
    platform no longer names the company (a masked row cannot be answered
    on the card). The project GC card serves this row, and its answer
    clears the project's flag (confirm_gc)."""
    if not str(row.get("gc_external_id") or "").strip():
        return None
    newest: datetime | None = None
    for entry in row.get("change_log") or []:
        if not isinstance(entry, dict) or entry.get("field") not in GC_QUESTION_FIELDS:
            continue
        at = _parse_ts(entry.get("at"))
        if at is not None and (newest is None or at > newest):
            newest = at
    if newest is None:
        return None
    confirmed = _parse_ts(row.get("gc_confirmed_at"))
    if confirmed is not None and confirmed >= newest:
        return None
    return newest


def card_invitation(rows: list[dict], project_id: str) -> dict | None:
    """The row the project GC card must serve when one of the project's
    linked rows has an open GC question of its own (open_gc_question_at: a
    board swap or an alias change it has not answered): the one raised
    last, ties by id. None when no linked row asks, and the caller falls
    back to the creator row (routers/projects._bc_invitation_for_project).
    A review_match row's best candidate is not a link and never asks."""
    pid = str(project_id)
    asking: list[tuple[datetime, str, dict]] = []
    for row in rows or []:
        if _project_id(row) != pid:
            continue
        at = open_gc_question_at(row)
        if at is not None:
            asking.append((at, str(row.get("id") or ""), row))
    if not asking:
        return None
    asking.sort(key=lambda item: (item[0], item[1]), reverse=True)
    return asking[0][2]


def _creator_question_open(sb, project_id: str) -> bool:
    """A BuildingConnected row that created this project still has a GC
    question open: its GC unconfirmed (a provisional or no GC, never
    answered on the card), or a board swap / alias change it has not
    answered since (open_gc_question_at)."""
    rows = (
        sb.table(INVITATIONS_TABLE)
        .select("id, gc_kind, gc_confirmed_at, gc_external_id, change_log")
        .eq("portal", PORTAL)
        .eq("created_project_id", project_id)
        .execute()
    ).data or []
    return any(
        (not r.get("gc_confirmed_at") and r.get("gc_kind") not in gc_aliases.CONFIRMED_KINDS)
        or open_gc_question_at(r) is not None
        for r in rows
    )


def confirm_gc(sb, invitation_id: str, actor_id: str, *, gc_id=None, create: dict | None = None) -> dict:
    """The GC card (3.4, D11 last sentence): the alias is written for the
    platform company (an existing GC by id, or a new one from `create`),
    the row's gc_* columns follow it, a row parked for the unresolved GC
    goes back to `match` (re-matched with the resolved GC, no second
    decision), and a project the row already has (created, or `exists`
    with the link the merge added) swaps its GC link and bid contact
    through rfp_create.swap_project_gc. "Same GC" on the row that created
    the project answers that project's open question
    (gc_confirm_pending), "same GC" there included (it goes through the swap,
    which attaches the lead the creation withheld from a provisional GC);
    so does a row whose own board GC swap or alias change reopened the
    question (open_gc_question_at) while no creator row's question is still
    open; any other row leaves the flag alone, since it was another
    package's question. A review_match row's best candidate is
    not a project and is never touched here."""
    row = _require_bc(sb, invitation_id)
    external_id = str(row.get("gc_external_id") or "").strip()
    if not external_id:
        raise pi.RfpPortalError(CODE_GC_REQUIRED, _MSG_GC_MASKED)
    external_name = row.get("gc_external_name")
    contact_id: str | None = None
    reused = None
    if isinstance(create, dict) and create:
        out = gc_aliases.create_gc_and_confirm(
            sb,
            source=gc_aliases.SOURCE_BC,
            external_id=external_id,
            external_name=external_name,
            name=create.get("name"),
            contact_name=create.get("contact_name"),
            contact_email=create.get("contact_email"),
            contact_phone=create.get("contact_phone"),
            actor_id=actor_id,
        )
        new_gc_id = str(out["gc_id"])
        contact_id = out.get("contact_id")
        reused = out.get("reused")
    else:
        new_gc_id = pi._valid_uuid(gc_id) or ""
        if not new_gc_id:
            raise pi.RfpPortalError(CODE_GC_REQUIRED, _MSG_GC_PICK)
        try:
            gc_aliases.confirm(
                sb, source=gc_aliases.SOURCE_BC, external_id=external_id, external_name=external_name,
                gc_id=new_gc_id, actor_id=actor_id,
            )
        except gc_aliases.GcNotFound as exc:
            raise pi.RfpPortalError(CODE_GC_REQUIRED, _MSG_GC_GONE) from exc
    old_gc_id = str(row.get("gc_id")) if row.get("gc_id") else None
    now_iso = _iso(_now())
    sb.table(INVITATIONS_TABLE).update({
        "gc_id": new_gc_id,
        "gc_kind": gc_aliases.KIND_ALIAS,
        "gc_confirmed_at": now_iso,
        "gc_confirmed_by": actor_id,
    }).eq("id", invitation_id).execute()
    rematch = False
    if row.get("status") == pi.STATUS_REVIEW_MATCH and row.get("flag_reason") == FLAG_GC_UNRESOLVED:
        rematch = pi._cas(sb, invitation_id, pi.STATUS_REVIEW_MATCH, {
            "status": pi.STATUS_MATCH, "flag_reason": None, "decided_at_step": None, **pi._RESET,
        })
    project_id = _project_id(row)
    swap: dict | None = None
    if project_id:
        owns_link = row.get("status") == pi.STATUS_CREATED or bool(row.get("project_gc_id"))
        creator = str(row.get("created_project_id") or "") == project_id
        # "Same GC" on the creator row goes through the swap too: creation
        # never files the lead under a provisional GC, so the answer is
        # what attaches it (swap_project_gc takes an unchanged GC: the
        # lead as bid contact, the pending flag cleared).
        same_on_creator = creator and old_gc_id == new_gc_id
        if (old_gc_id != new_gc_id and owns_link) or same_on_creator:
            swap = _swap_project_gc(sb, project_id, old_gc_id, new_gc_id, row.get("lead"), actor_id)
            if swap and swap.get("project_gc_id"):
                sb.table(INVITATIONS_TABLE).update({"project_gc_id": swap["project_gc_id"]}).eq(
                    "id", invitation_id
                ).execute()
        elif creator or (open_gc_question_at(row) is not None and not _creator_question_open(sb, project_id)):
            # The creator answers the project's question; so does a row
            # whose own GC question (a board swap, an alias repointed or
            # deleted) raised it, unless the creator's question is still
            # open (that one stays asked).
            sb.table("projects").update({"gc_confirm_pending": False}).eq("id", project_id).execute()
    pi.audit(actor_id, "rfp_portal.confirm_gc", pi.AUDIT_ENTITY, invitation_id, {
        "external_id": external_id,
        "external_name": external_name,
        "gc_id": new_gc_id,
        "was_gc_id": old_gc_id,
        "created": bool(create),
        "reused": reused,
        "contact_id": contact_id,
        "rematch": rematch,
        "project_id": project_id,
        "swap": swap,
    })
    return pi.detail(sb, invitation_id)


def apply_dates(sb, invitation_id: str, actor_id: str, fields: list[str] | None) -> dict:
    """D20: the ticked dates from the invitation onto its project through
    rfp_create.apply_project_dates (the PATCH semantics, audited there),
    plus project_gcs.needs_by for the row's GC link when the due date moved.
    Returns {project, applied}."""
    row = _require_bc(sb, invitation_id)
    project_id = _project_id(row)
    if not project_id:
        raise pi.RfpPortalError(pi.CODE_PROJECT_REQUIRED, _MSG_NO_PROJECT)
    ticked = {f for f in (fields or []) if isinstance(f, str)}
    wanted = [f for f in APPLY_DATE_FIELDS if f in ticked]
    if not wanted:
        raise pi.RfpPortalError(CODE_DATES_UNCHANGED, _MSG_PICK_DATES)
    patch: dict = {}
    for name in wanted:
        if name == "actual_bid_at":
            value = _iso(_parse_ts(row.get("close_at")))
        elif name == "job_walk_at":
            value = _iso(_parse_ts(row.get("job_walk_at")))
        elif name == "est_start_date":
            day = bc_facts.pacific_date(row.get("expected_start_at"))
            value = day.isoformat() if day else None
        else:
            day = bc_facts.pacific_date(row.get("expected_finish_at"))
            value = day.isoformat() if day else None
        if value is not None:
            patch[name] = value
    if not patch:
        raise pi.RfpPortalError(CODE_DATES_UNCHANGED, _MSG_NO_DATES)
    project = _apply_project_dates(sb, project_id, patch, actor_id, source=PORTAL)
    needs_by_moved = False
    if "actual_bid_at" in patch and row.get("gc_id"):
        needs_by = rfp_create._pacific_date(patch["actual_bid_at"])
        query = sb.table("project_gcs").update({"needs_by": needs_by})
        if row.get("project_gc_id"):
            query = query.eq("id", row["project_gc_id"])
        else:
            query = query.eq("project_id", project_id).eq("gc_id", row["gc_id"])
        needs_by_moved = bool((query.execute()).data or [])
    pi.audit(actor_id, "rfp_portal.apply_dates", pi.AUDIT_ENTITY, invitation_id, {
        "project_id": project_id, "applied": list(patch.keys()), "needs_by_moved": needs_by_moved,
    })
    return {"project": project, "applied": list(patch.keys())}
