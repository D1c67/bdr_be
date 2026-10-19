"""PM RFI sync from the GC's Procore (docs/PM_RFI_SYNC.md, migration 0156).

A PM project linked to the GC's Procore project (pm_external_links) gets
every RFI on that project, all trades, imported into its RFI log. Read only
on both sides: imported rows (rfis.source = 'procore') cannot be edited in
BDR, and nothing is ever written to Procore (procore_client allows GETs on
RFI_ALLOWED_PATHS only, plus the login form posts it already makes).

How a run works (`sync_project`):

- The list endpoint returns every RFI (no paging). A row is fetched in
  detail only when it is new to BDR or its `updated_at` moved, and never
  once the local row is closed and complete (closed rows are never
  reopened either). A row is complete once its attachments landed: only
  then does it get Procore's `updated_at`.
  Procore drafts are skipped. Each revision is its own row keyed by its
  Procore id.
- A G3 RFI that is also in the GC's Procore is LINKED, not duplicated: the
  internal row gets the external_* fields, and later runs update its
  Procore status, answer and answer attachments, never its question. The
  link is automatic only when exactly one unlinked internal row matches by
  normalized subject, or by a question attachment named like "RFI 040 - ...".
- Attachments are downloaded once (rfi_attachments.external_file_id) into
  the project's Documents (pm_documents, category rfi). A file over
  PM_RFI_SYNC_ATTACHMENT_MAX_BYTES is skipped and recorded in the run stats.
- The first run of a project is a backfill: everything is imported and only
  one in-app "linked" notice is sent. Later runs send one summary per
  project (new, newly answered, newly closed) to Estimating Engineer Labor
  and Estimating Admin, bell plus branded email.

Runs (pm_rfi_sync_runs) are queued by the daily 06:30 Pacific slot, by
"Sync now" and by the email scan, and claimed under a lease so the two
uvicorn workers never run the same project twice. The daily unique index
is the slot ledger. The email scan reads filed PM emails for Procore RFI
links (`/{project}/project/rfi/show/{rfi}`), and email_classifications
rows labeled `rfis` when that table exists; an unlinked PM project whose
email carries an RFI link is linked automatically.

Every Supabase call here is the sync SDK: the module runs in worker
threads (asyncio.to_thread) and in sync FastAPI endpoints only.
"""

from __future__ import annotations

import asyncio
import html as html_lib
import logging
import os
import re
import socket
import tempfile
import time
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

import httpx

from app.core.config import Settings, get_settings
from app.core.roles import Role
from app.core.supabase_client import get_supabase
from app.services import procore_client as pc
from app.services.notifications import notify_role
from app.services.sanitize import rich_text_to_text, sanitize_rich_text

logger = logging.getLogger(__name__)

COMPANY_TZ = ZoneInfo("America/Los_Angeles")

LINKS_TABLE = "pm_external_links"
RUNS_TABLE = "pm_rfi_sync_runs"
SCAN_TABLE = "pm_rfi_email_scan_state"
SCAN_ROW_ID = "default"

NOTIFY_SUMMARY = "pm_rfi.sync_summary"
NOTIFY_LINKED = "pm_rfi.linked"
NOTIFY_ROLES: tuple[Role, ...] = (Role.ESTIMATING_ENGINEER_LABOR, Role.ESTIMATING_ADMIN)

READ_ONLY_DETAIL = "This RFI is imported from Procore and is read-only."

PLATFORMS = ("procore", "other", "none")
TRIGGERS = ("daily", "manual", "email")

# Columns a sync clears on Unlink (and moves on a manual Link).
EXTERNAL_FIELDS: tuple[str, ...] = (
    "external_id",
    "external_number",
    "external_revision",
    "external_status",
    "external_url",
    "responsible_contractor",
    "received_from",
    "initiated_at",
    "ball_in_court",
    "external_due_date",
    "external_updated_at",
    "external_synced_at",
    "external_raw",
    "source_email_id",
)

# Columns the sync reads from local rfis rows (never external_raw).
_RFI_COLUMNS = (
    "id, project_id, rfi_number, subject, question, answer, status, answered_at, "
    "answered_by, send_status, sent_via, sent_at, due_at, source, external_id, "
    "external_number, external_revision, external_status, external_updated_at, "
    "external_autolink_blocked"
)

_STATUS_RANK = {"open": 0, "answered": 1, "closed": 2}
_ERROR_MAX_CHARS = 500
_NOTIFY_LIST_MAX = 5
_LABEL_MAX = 90
_FILE_MARKER = "[procore-file:{id}]"

WORKER_ID = f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:6]}"


# ── Small helpers ─────────────────────────────────────────────────────────────


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime | None) -> str | None:
    return dt.astimezone(timezone.utc).isoformat() if dt else None


def _parse_ts(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _local_date(value: Any) -> str | None:
    """A Procore instant as the Pacific calendar date (rfis date columns)."""
    dt = _parse_ts(value)
    return dt.astimezone(COMPANY_TZ).date().isoformat() if dt else None


def _date_only(value: Any) -> str | None:
    if not value:
        return None
    try:
        return date.fromisoformat(str(value)[:10]).isoformat()
    except ValueError:
        return None


def _is_unique_violation(exc: BaseException) -> bool:
    msg = str(exc)
    return "23505" in msg or "duplicate key" in msg.lower()


def _short(text: Any, limit: int = _ERROR_MAX_CHARS) -> str:
    return str(text or "")[:limit]


def _name(value: Any) -> str | None:
    if isinstance(value, dict):
        name = value.get("name")
        return str(name).strip() or None if name else None
    if isinstance(value, str):
        return value.strip() or None
    return None


def normalize_subject(value: Any) -> str:
    """Lowercase words only: "EF3 Circuit  Clarification!" -> "ef3 circuit clarification"."""
    return " ".join(re.sub(r"[^0-9a-z]+", " ", str(value or "").lower()).split())


def natural_key(number: Any) -> tuple:
    """Sort key for Procore numbers: "Const-9" < "Const-97" < "Const-189" < "Const-189.1"."""
    parts = re.split(r"(\d+)", str(number or ""))
    return tuple((0, int(p)) if p.isdigit() else (1, p.lower()) for p in parts if p != "")


def _rev_int(value: Any) -> int:
    try:
        return int(str(value or "0"))
    except ValueError:
        return 0


def rfi_label(row: dict) -> str:
    """How a summary names an RFI: "Const-198 EF3 Circuit Clarification"."""
    number = row.get("external_number") or (
        str(row.get("rfi_number")).zfill(3) if row.get("rfi_number") is not None else ""
    )
    rev = str(row.get("external_revision") or "0")
    if rev != "0":
        number = f"{number} r{rev}"
    text = f"{number} {row.get('subject') or ''}".strip()
    return text if len(text) <= _LABEL_MAX else text[: _LABEL_MAX - 3].rstrip() + "..."


# ── Daily slot (Pacific HH:MM, DST aware) ─────────────────────────────────────


def daily_slots(now: datetime, settings: Settings) -> list[datetime]:
    """Yesterday's and today's PM_RFI_SYNC_DAILY_TIME as Pacific wall-clock
    instants in UTC, sorted (yesterday's so the catch-up survives midnight).
    ZoneInfo resolves the offset per day, so 06:30 is 13:30 UTC in summer
    and 14:30 UTC in winter."""
    hour, minute = settings.pm_rfi_sync_slot
    local = now.astimezone(COMPANY_TZ)
    out = []
    for day in (local.date() - timedelta(days=1), local.date()):
        slot = datetime(day.year, day.month, day.day, hour, minute, tzinfo=COMPANY_TZ)
        out.append(slot.astimezone(timezone.utc))
    return sorted(out)


def due_daily_slot(now: datetime, settings: Settings) -> datetime | None:
    """The daily slot to claim at `now`: the newest one whose catch-up window
    (PM_RFI_SYNC_CATCHUP_HOURS) contains `now`, else None."""
    window = timedelta(hours=int(settings.pm_rfi_sync_catchup_hours))
    due = [slot for slot in daily_slots(now, settings) if slot <= now < slot + window]
    return due[-1] if due else None


def next_daily_slot(now: datetime, settings: Settings) -> datetime:
    hour, minute = settings.pm_rfi_sync_slot
    local = now.astimezone(COMPANY_TZ)
    for offset in range(0, 3):
        day = local.date() + timedelta(days=offset)
        slot = datetime(day.year, day.month, day.day, hour, minute, tzinfo=COMPANY_TZ).astimezone(timezone.utc)
        if slot > now:
            return slot
    return now + timedelta(days=1)


# ── Links ─────────────────────────────────────────────────────────────────────


class LinkError(Exception):
    """A PUT /pm-link refusal: `status_code` is 409 or 422."""

    def __init__(self, status_code: int, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code


def get_link(sb, project_id: str) -> dict | None:
    rows = sb.table(LINKS_TABLE).select("*").eq("project_id", project_id).limit(1).execute().data or []
    return rows[0] if rows else None


def has_open_run(sb, project_id: str) -> bool:
    rows = (
        sb.table(RUNS_TABLE)
        .select("id")
        .eq("project_id", project_id)
        .in_("status", ["queued", "running"])
        .limit(1)
        .execute()
    ).data or []
    return bool(rows)


def link_view(link: dict | None, running: bool) -> dict:
    """The PmExternalLink the frontend reads (GET/PUT /pm-link)."""
    link = link or {}
    return {
        "platform": link.get("platform"),
        "host": link.get("host"),
        "external_company_id": link.get("external_company_id"),
        "external_project_id": link.get("external_project_id"),
        "external_url": link.get("external_url"),
        "sync_enabled": bool(link.get("sync_enabled", False)) if link else False,
        "linked_via": link.get("linked_via"),
        "last_synced_at": link.get("last_synced_at"),
        "last_sync_status": link.get("last_sync_status"),
        "last_error": link.get("last_error"),
        "backfilled_at": link.get("backfilled_at"),
        "running": bool(running),
    }


def _procore_conflict(sb, project_id: str, host: str, external_project_id: str) -> dict | None:
    rows = (
        sb.table(LINKS_TABLE)
        .select("project_id")
        .eq("platform", "procore")
        .eq("host", host)
        .eq("external_project_id", external_project_id)
        .execute()
    ).data or []
    for row in rows:
        if row.get("project_id") != project_id:
            return row
    return None


def _is_procore_link(link: dict | None) -> bool:
    return bool(link and link.get("platform") == "procore" and link.get("external_project_id"))


def purge_imported(sb, project_id: str) -> int:
    """Forget the Procore project a link pointed at: its imported rows go
    (their attachment rows cascade; the files stay in Documents) and linked
    G3 rows lose their external fields and Procore files, as on Unlink but
    still eligible for auto-linking. Run when a link moves to another
    Procore project or off Procore, so the old rows neither linger read only
    nor collide with the new project's numbers. Returns rows removed."""
    removed = (
        sb.table("rfis").delete().eq("project_id", project_id).eq("source", "procore").execute()
    ).data or []
    linked = [
        r for r in (
            sb.table("rfis").select("id, source, external_id").eq("project_id", project_id).execute()
        ).data or []
        if r.get("external_id") and (r.get("source") or "internal") == "internal"
    ]
    for row in linked:
        atts = (
            sb.table("rfi_attachments").select("id, external_file_id").eq("rfi_id", row["id"]).execute()
        ).data or []
        drop = [a["id"] for a in atts if a.get("external_file_id")]
        if drop:
            sb.table("rfi_attachments").delete().eq("rfi_id", row["id"]).in_("id", drop).execute()
        patch: dict = {k: None for k in EXTERNAL_FIELDS}
        patch["external_revision"] = "0"
        sb.table("rfis").update(patch).eq("id", row["id"]).eq("project_id", project_id).execute()
    if removed or linked:
        logger.info(
            "pm rfi sync: project %s link changed; removed %d imported RFI(s), unlinked %d G3 RFI(s)",
            project_id, len(removed), len(linked),
        )
    return len(removed)


def save_link(
    sb,
    project_id: str,
    *,
    platform: str,
    url: str | None,
    sync_enabled: bool | None,
    actor_id: str | None,
    resolve_company: Callable[[pc.ProcoreProjectRef], str | None] | None = None,
) -> tuple[dict, bool]:
    """Create or replace the project's link (PUT /pm-link). Returns (row,
    changed_procore_project). A Procore link whose project changed starts
    over as a backfill. `resolve_company` asks the session for the company
    id when the URL carried none (best effort: a failure leaves it null and
    the run resolves it)."""
    if platform not in PLATFORMS:
        raise LinkError(422, "Platform must be procore, other or none.")
    current = get_link(sb, project_id)
    now_iso = _iso(_now())
    if platform != "procore":
        row = {
            "project_id": project_id,
            "platform": platform,
            "host": None,
            "external_company_id": None,
            "external_project_id": None,
            "external_url": (url or "").strip()[:2000] or None if platform == "other" else None,
            "sync_enabled": False,
            "linked_by": actor_id,
            "linked_via": "manual",
            "last_sync_status": None,
            "last_error": None,
            "backfilled_at": None,
            "backfill_skipped_ids": [],
            "updated_at": now_iso,
        }
        saved = sb.table(LINKS_TABLE).upsert(row, on_conflict="project_id").execute().data or [row]
        if _is_procore_link(current):
            purge_imported(sb, project_id)
        return saved[0], False

    if not (url or "").strip():
        # Toggling sync on an existing Procore link needs no URL.
        if current and current.get("platform") == "procore" and current.get("external_project_id"):
            patch = {"updated_at": now_iso}
            if sync_enabled is not None:
                patch["sync_enabled"] = bool(sync_enabled)
            saved = (
                sb.table(LINKS_TABLE).update(patch).eq("project_id", project_id).execute()
            ).data or [{**current, **patch}]
            return saved[0], False
        raise LinkError(422, "Paste the Procore project URL (any page of the GC's project).")

    ref = pc.parse_project_url(url)
    if ref is None:
        raise LinkError(
            422,
            "That is not a Procore project URL. Paste a link like "
            "https://app.procore.com/1234567/project/home or an RFI link.",
        )
    if _procore_conflict(sb, project_id, ref.host, ref.project_id):
        raise LinkError(409, "Another BDR project is already linked to that Procore project.")

    same_project = bool(
        current
        and current.get("platform") == "procore"
        and current.get("host") == ref.host
        and str(current.get("external_project_id") or "") == ref.project_id
    )
    company = ref.company_id or (current.get("external_company_id") if same_project else None)
    if not company and resolve_company is not None:
        try:
            company = resolve_company(ref)
        except Exception as exc:  # noqa: BLE001 - best effort; the run resolves it
            logger.warning(
                "pm rfi sync: company id lookup for Procore project %s failed: %s",
                ref.project_id, _short(exc, 200),
            )
            company = None
    row: dict = {
        "project_id": project_id,
        "platform": "procore",
        "host": ref.host,
        "external_project_id": ref.project_id,
        "external_company_id": company,
        "external_url": ref.project_url,
        "sync_enabled": True if sync_enabled is None else bool(sync_enabled),
        "linked_by": actor_id,
        "linked_via": "manual",
        "updated_at": now_iso,
    }
    if not same_project:
        row.update(last_synced_at=None, last_sync_status=None, last_error=None, backfilled_at=None,
                   backfill_skipped_ids=[])
    try:
        saved = sb.table(LINKS_TABLE).upsert(row, on_conflict="project_id").execute().data or [row]
    except Exception as exc:  # noqa: BLE001 - the partial unique index lost a race
        if _is_unique_violation(exc):
            raise LinkError(409, "Another BDR project is already linked to that Procore project.") from exc
        raise
    if not same_project and _is_procore_link(current):
        purge_imported(sb, project_id)
    if ref.host != pc.APP_HOST:
        logger.warning(
            "pm rfi sync: project %s linked on %s (untested Procore host)", project_id, ref.host
        )
    return saved[0], not same_project


# ── Run queue ─────────────────────────────────────────────────────────────────


def enqueue_run(
    sb,
    project_id: str,
    trigger: str,
    *,
    requested_by: str | None = None,
    target_external_rfi_id: str | None = None,
    source_email_id: str | None = None,
    scheduled_for: datetime | None = None,
    fresh: bool = False,
) -> tuple[dict | None, bool]:
    """Queue a run. Returns (run, created). A manual run joins a queued or
    running full (untargeted) run of the same project instead of adding
    one; with `fresh` (the link just changed) it joins only a queued full
    run, since a running one loaded the old link. An email run joins a
    queued full run; daily and email runs that lose their unique index (the
    slot ledger, one run per email) return (None, False)."""
    if trigger not in TRIGGERS:
        raise ValueError(f"unknown trigger {trigger}")
    open_runs = (
        sb.table(RUNS_TABLE)
        .select("*")
        .eq("project_id", project_id)
        .in_("status", ["queued", "running"])
        .order("created_at")
        .execute()
    ).data or []
    if trigger == "manual":
        for run in open_runs:
            if run.get("target_external_rfi_id"):
                continue
            if fresh and run.get("status") != "queued":
                continue
            return run, False
    if trigger == "email":
        for run in open_runs:
            if run.get("status") == "queued" and not run.get("target_external_rfi_id"):
                return run, False
            if (
                target_external_rfi_id
                and run.get("status") == "queued"
                and run.get("target_external_rfi_id") == target_external_rfi_id
            ):
                return run, False
    row = {
        "project_id": project_id,
        "trigger": trigger,
        "status": "queued",
        "requested_by": requested_by,
        "target_external_rfi_id": target_external_rfi_id,
        "source_email_id": source_email_id,
        "scheduled_for": _iso(scheduled_for),
    }
    try:
        inserted = sb.table(RUNS_TABLE).insert(row).execute().data or []
    except Exception as exc:  # noqa: BLE001
        if _is_unique_violation(exc):
            return None, False
        raise
    return (inserted[0] if inserted else row), True


def schedule_daily(sb, settings: Settings, now: datetime) -> int:
    """Queue today's daily run for every linked, sync-enabled Procore project
    that has none for the slot yet. The (project, 'daily', slot) unique index
    makes the second worker's insert a no-op. Returns runs queued."""
    slot = due_daily_slot(now, settings)
    if slot is None:
        return 0
    links = (
        sb.table(LINKS_TABLE)
        .select("project_id")
        .eq("platform", "procore")
        .eq("sync_enabled", True)
        .execute()
    ).data or []
    if not links:
        return 0
    have = {
        r.get("project_id")
        for r in (
            sb.table(RUNS_TABLE)
            .select("project_id")
            .eq("trigger", "daily")
            .eq("scheduled_for", _iso(slot))
            .execute()
        ).data
        or []
    }
    queued = 0
    for link in links:
        pid = link.get("project_id")
        if not pid or pid in have:
            continue
        _, created = enqueue_run(sb, pid, "daily", scheduled_for=slot)
        queued += int(created)
    if queued:
        logger.info("pm rfi sync: queued %d daily run(s) for slot %s", queued, _iso(slot))
    return queued


def claim_next(sb, settings: Settings, now: datetime, *, worker_id: str = WORKER_ID) -> dict | None:
    """Claim one runnable run under a lease (compare-and-set on its status
    and old lease). A queued run waits out next_attempt_at; a running run is
    reclaimed only once its lease expired; a project with a live run is
    skipped so two workers never sync one project at once."""
    rows = (
        sb.table(RUNS_TABLE)
        .select("*")
        .in_("status", ["queued", "running"])
        .order("created_at")
        .limit(100)
        .execute()
    ).data or []
    busy: set[str] = set()
    for r in rows:
        lease = _parse_ts(r.get("lease_until"))
        if r.get("status") == "running" and lease and lease > now:
            busy.add(r.get("project_id"))
    lease_until = _iso(now + timedelta(seconds=settings.pm_rfi_sync_lease_seconds))
    for r in rows:
        if r.get("project_id") in busy:
            continue
        status = r.get("status")
        if status == "queued":
            wait = _parse_ts(r.get("next_attempt_at"))
            if wait and wait > now:
                continue
        else:
            lease = _parse_ts(r.get("lease_until"))
            if lease and lease > now:
                continue
            if int(r.get("attempts") or 0) >= settings.pm_rfi_sync_max_attempts:
                # A run whose worker died on it every time: stop reclaiming it.
                message = "The sync stopped repeatedly before finishing; it was not retried again."
                abandoned = sb.table(RUNS_TABLE).update({
                    "status": "failed", "lease_until": None, "lease_owner": None,
                    "finished_at": _iso(now),
                    "error": message,
                }).eq("id", r["id"]).eq("status", "running").eq("lease_owner", r.get("lease_owner")).execute()
                if abandoned.data:
                    # The worker that died never wrote the link's status; show it.
                    _update_link_status(sb, r["project_id"], "failed", message)
                logger.warning("pm rfi sync: run %s abandoned after %s attempts", r["id"], r.get("attempts"))
                continue
        patch = {
            "status": "running",
            "lease_until": lease_until,
            "lease_owner": worker_id,
            "attempts": int(r.get("attempts") or 0) + 1,
            "started_at": _iso(now),
        }
        query = sb.table(RUNS_TABLE).update(patch).eq("id", r["id"]).eq("status", status)
        if status == "running":
            query = query.eq("lease_owner", r.get("lease_owner"))
        claimed = query.execute().data or []
        if not claimed:
            # Another worker took this run, so it is syncing this project now.
            busy.add(r.get("project_id"))
            continue
        run = claimed[0]
        if _yield_to_rival(sb, run, r, now):
            busy.add(r.get("project_id"))
            continue
        return run
    return None


def _run_key(run: dict) -> tuple[str, str]:
    return (str(_parse_ts(run.get("created_at")) or run.get("created_at") or ""), str(run.get("id")))


def _yield_to_rival(sb, run: dict, before: dict, now: datetime) -> bool:
    """Two workers can claim two runs of one project in the same instant
    (each saw the project idle). After a claim, the run with the lowest
    (created_at, id) among the live running runs of the project keeps
    going; any other is put back as it was. True when ours was put back."""
    rivals = [
        r for r in (
            sb.table(RUNS_TABLE)
            .select("id, created_at, lease_until")
            .eq("project_id", run.get("project_id"))
            .eq("status", "running")
            .execute()
        ).data or []
        if r.get("id") != run["id"] and (_parse_ts(r.get("lease_until")) or now) > now
    ]
    if not any(_run_key(r) < _run_key(run) for r in rivals):
        return False
    sb.table(RUNS_TABLE).update({
        "status": "queued",
        "lease_until": None,
        "lease_owner": None,
        "attempts": int(before.get("attempts") or 0),
        "started_at": before.get("started_at"),
    }).eq("id", run["id"]).eq("lease_owner", run.get("lease_owner")).execute()
    return True


class _LeaseLost(Exception):
    """Another worker took the run over (our lease expired)."""


class SyncError(Exception):
    """A run failure with an app-authored, storable message."""


def _renewer(sb, run: dict, settings: Settings) -> Callable[[], None]:
    last = [0.0]

    def renew() -> None:
        now_mono = time.monotonic()
        if now_mono - last[0] < 30:
            return
        last[0] = now_mono
        until = _iso(_now() + timedelta(seconds=settings.pm_rfi_sync_lease_seconds))
        rows = (
            sb.table(RUNS_TABLE)
            .update({"lease_until": until})
            .eq("id", run["id"])
            .eq("lease_owner", run.get("lease_owner"))
            .eq("status", "running")
            .execute()
        ).data or []
        if not rows:
            raise _LeaseLost()

    return renew


def _finish(sb, run: dict, status: str, *, stats: dict | None = None, error: str | None = None,
            next_attempt_at: datetime | None = None) -> None:
    patch: dict = {"status": status, "lease_until": None, "lease_owner": None}
    if stats is not None:
        patch["stats"] = stats
    if error is not None or status == "done":
        patch["error"] = _short(error) if error else None
    if status in ("done", "failed"):
        patch["finished_at"] = _iso(_now())
    if status == "queued":
        patch["next_attempt_at"] = _iso(next_attempt_at)
    sb.table(RUNS_TABLE).update(patch).eq("id", run["id"]).eq(
        "lease_owner", run.get("lease_owner")
    ).execute()


def _update_link_status(sb, project_id: str, status: str, error: str | None, *,
                        backfilled: bool = False, skipped_ids: list[str] | None = None) -> None:
    patch: dict = {
        "last_synced_at": _iso(_now()),
        "last_sync_status": status,
        "last_error": _short(error) if error else None,
    }
    if backfilled:
        patch["backfilled_at"] = _iso(_now())
        patch["backfill_skipped_ids"] = sorted(set(skipped_ids or []))
    sb.table(LINKS_TABLE).update(patch).eq("project_id", project_id).execute()


# ── Session ───────────────────────────────────────────────────────────────────


def session_provider(host: str) -> str:
    """The rfp_harvest_sessions row holding a host's cookies: app.procore.com
    shares the RFP harvest's `procore` row; another host keeps its cookies
    in its own row (its logins still count against the shared row, see
    _SharedLockStore)."""
    return pc.PROVIDER if host == pc.APP_HOST else f"{pc.PROVIDER}:{host}"


class _SharedLockStore:
    """The session store for a non-app host (us02). Cookies live in the
    host's own row, but every login is the same PROCORE_LOGIN_EMAIL posted
    to login.procore.com, so the lock, the failure count and the spacing
    between login attempts come from (and are recorded on) the shared
    `procore` row as well: a lock or a recent attempt there stops a us02
    login, and a us02 failure counts toward the harvest's lockout."""

    def __init__(self, host_store, shared_store) -> None:
        self.host = host_store
        self.shared = shared_store

    def _merge(self, host: dict | None, shared: dict | None) -> dict | None:
        if not host and not shared:
            return None
        merged = dict(host or {})
        for key in ("locked_until", "last_login_attempt_at"):
            merged[key] = _later((host or {}).get(key), (shared or {}).get(key))
        return merged

    def load(self) -> dict | None:
        return self._merge(self.host.load(), self.shared.load())

    def save_cookies(self, account: str, cookies: list[dict]) -> None:
        self.host.save_cookies(account, cookies)

    def record_login(self, *, ok: bool, error: str | None) -> dict:
        shared = self.shared.record_login(ok=ok, error=error)
        host = self.host.record_login(ok=ok, error=error)
        return self._merge(host, shared) or {}

    def touch(self) -> None:
        self.host.touch()


def open_session(host: str, settings: Settings | None = None) -> pc.ProcoreSession:
    from app.services import rfp_harvest  # heavy module: imported on first use

    s = settings or get_settings()
    store = rfp_harvest._SessionStore(s, session_provider(host))
    if host != pc.APP_HOST:
        logger.warning("pm rfi sync: opening a Procore session on %s (untested host)", host)
        store = _SharedLockStore(store, rfp_harvest._SessionStore(s, pc.PROVIDER))
    return pc.ProcoreSession(
        rfp_harvest.procore_config(s),
        store,
        on_lock=rfp_harvest._notify_lock,
        app_host=host,
    )


# ── Mapping a Procore RFI ─────────────────────────────────────────────────────


def _html_to_text(html: str | None) -> str:
    if not html:
        return ""
    with_breaks = re.sub(r"(?i)</p\s*>|<br\s*/?>|</li\s*>|</div\s*>", "\n", html)
    text = rich_text_to_text(with_breaks).replace("\xa0", " ")
    lines = [" ".join(line.split()) for line in text.splitlines()]
    return "\n".join(line for line in lines if line).strip()


def _all_answers(detail: dict) -> list[dict]:
    seen: set[str] = set()
    out: list[dict] = []
    sources = list(detail.get("answers") or [])
    for q in detail.get("questions") or []:
        if isinstance(q, dict):
            sources.extend(q.get("answers") or [])
    for a in sources:
        if not isinstance(a, dict):
            continue
        key = str(a.get("id") or id(a))
        if key in seen:
            continue
        seen.add(key)
        out.append(a)
    return out


def _chosen_answers(detail: dict) -> list[dict]:
    """Official answers, or every answer when none is official; oldest first."""
    answers = _all_answers(detail)
    official = [a for a in answers if a.get("official")]
    chosen = official or answers
    return sorted(chosen, key=lambda a: str(a.get("answer_date") or a.get("created_at") or ""))


def _answer_text(a: dict) -> str:
    return _html_to_text(a.get("rich_text_body")) or " ".join(
        str(a.get("plain_text_body") or "").replace("\xa0", " ").split()
    )


def _question_html(detail: dict) -> str:
    blocks: list[str] = []
    for q in detail.get("questions") or []:
        if not isinstance(q, dict):
            continue
        rich = q.get("rich_text_body") or q.get("body")
        if rich:
            clean = sanitize_rich_text(str(rich))
        else:
            plain = str(q.get("plain_text_body") or "").strip()
            clean = f"<p>{html_lib.escape(plain)}</p>" if plain else ""
        if clean and rich_text_to_text(clean):
            blocks.append(clean)
    if not blocks:
        return "<p>(No question text in Procore.)</p>"
    return "".join(blocks)


def _people(value: Any) -> str | None:
    if isinstance(value, list):
        names = [n for n in (_name(v) for v in value) if n]
        return ", ".join(dict.fromkeys(names)) or None
    return _name(value)


def map_status(detail: dict) -> str:
    """Procore status -> rfi_status: closed* is closed; open with an answer
    is answered; anything else is open."""
    raw = str(detail.get("status") or "").lower()
    if raw.startswith("closed"):
        return "closed"
    if _all_answers(detail):
        return "answered"
    return "open"


_DIGITS_RE = re.compile(r"^\d{1,20}$")


def _rfi_url(detail: dict, host: str, project_id: str) -> str | None:
    """The "View in Procore" link, always built from the known app host and
    digit-checked ids (Procore's own `link` field is never stored as is)."""
    if host not in pc.APP_HOSTS or not _DIGITS_RE.match(str(project_id)):
        return None
    rfi_id = str(detail.get("id") or "")
    if not _DIGITS_RE.match(rfi_id):
        return f"https://{host}/{project_id}/project/rfi/list"
    return f"https://{host}/{project_id}/project/rfi/show/{rfi_id}"


def _compact_raw(detail: dict) -> dict:
    """What external_raw keeps: identifiers, status and people, never the
    bodies or the 70-name distribution list."""
    keep = (
        "id", "number", "full_number", "prefix", "revision", "current_revision",
        "has_revisions", "source_rfi_header_id", "previous_revision_id", "status",
        "translated_status", "private", "draft", "link", "created_at", "updated_at",
        "initiated_at", "time_resolved", "due_date", "drawing_number", "reference",
        "schedule_impact", "cost_impact",
    )
    raw = {k: detail.get(k) for k in keep if k in detail}
    raw["rfi_manager"] = _name(detail.get("rfi_manager"))
    raw["assignees"] = _people(detail.get("assignees"))
    created_by = detail.get("created_by") if isinstance(detail.get("created_by"), dict) else {}
    raw["created_by"] = _name(created_by)
    raw["created_by_vendor"] = _name((created_by or {}).get("vendor"))
    raw["question_count"] = len(detail.get("questions") or [])
    raw["answer_count"] = len(_all_answers(detail))
    return raw


def map_detail(detail: dict, host: str, project_id: str) -> dict:
    """The rfis columns a Procore RFI detail fills (imported rows), plus the
    external_* columns a linked internal row takes."""
    answers = _chosen_answers(detail)
    texts = []
    for a in answers:
        text = _answer_text(a)
        if not text:
            continue
        who = str(a.get("created_by") or "").strip()
        when = _local_date(a.get("answer_date"))
        head = ", ".join(p for p in (who, when) if p)
        texts.append(f"{head}:\n{text}" if head and len(answers) > 1 else text)
    answer = "\n\n".join(texts) or None
    answered_by = ", ".join(
        dict.fromkeys(str(a.get("created_by")).strip() for a in answers if a.get("created_by"))
    ) or None
    answered_at = None
    for a in answers:
        answered_at = _local_date(a.get("answer_date"))
        if answered_at:
            break
    status = map_status(detail)
    if status == "closed" and not answered_at:
        answered_at = _local_date(detail.get("time_resolved"))
    number = str(detail.get("full_number") or detail.get("number") or "").strip() or None
    revision = str(detail.get("revision") if detail.get("revision") is not None else "0")
    ball = _people(detail.get("ball_in_courts")) or _people(detail.get("ball_in_court"))
    subject = str(detail.get("subject") or detail.get("title") or number or "Procore RFI").strip()
    return {
        "subject": subject[:300],
        "question": _question_html(detail),
        "answer": answer,
        "answered_by": answered_by,
        "answered_at": answered_at,
        "status": status,
        "external_id": str(detail.get("id")),
        "external_number": number,
        "external_revision": revision,
        "external_status": str(detail.get("translated_status") or detail.get("status") or "")[:100] or None,
        "external_url": _rfi_url(detail, host, project_id),
        "responsible_contractor": _name(detail.get("responsible_contractor")),
        "received_from": _name(detail.get("received_from")),
        "initiated_at": _iso(_parse_ts(detail.get("initiated_at"))),
        "ball_in_court": ball,
        "external_due_date": _date_only(detail.get("due_date")),
        "external_updated_at": _iso(_parse_ts(detail.get("updated_at"))),
        "external_raw": _compact_raw(detail),
    }


def detail_attachments(detail: dict) -> list[tuple[str, dict]]:
    """[(kind, attachment)] in order: question files, then answer files.
    Each attachment carries id, filename/name and a signed url."""
    out: list[tuple[str, dict]] = []
    seen: set[tuple[str, str]] = set()

    def add(kind: str, items: Any) -> None:
        for att in items or []:
            if not isinstance(att, dict) or not att.get("id") or not att.get("url"):
                continue
            key = (kind, str(att["id"]))
            if key in seen:
                continue
            seen.add(key)
            out.append((kind, att))

    for q in detail.get("questions") or []:
        if isinstance(q, dict):
            add("question", q.get("attachments"))
    add("question", detail.get("attachments"))
    for a in _all_answers(detail):
        add("answer", a.get("attachments"))
    return out


def _attachment_name(att: dict) -> str:
    return str(att.get("filename") or att.get("name") or f"procore-file-{att.get('id')}").strip()


# ── Auto-link to G3's own RFI ─────────────────────────────────────────────────


def _filename_number_re(n: int) -> re.Pattern[str]:
    return re.compile(rf"^RFI[ #-]*0*{n}\b", re.IGNORECASE)


def match_internal(detail: dict, candidates: list[dict]) -> dict | None:
    """The one unlinked internal RFI this Procore RFI is, or None. An
    attachment named "RFI 040 - ..." names internal RFI 40 and wins; else a
    normalized subject equal to exactly one candidate's. Ambiguity links
    nothing."""
    if not candidates:
        return None
    # Question files only: answer files are the GC's and are often named
    # after the GC's own number ("Response RFI#-198-..."), and a number
    # equal to the Procore RFI's own is never read as G3's.
    names = [
        _attachment_name(att)
        for q in detail.get("questions") or [] if isinstance(q, dict)
        for att in q.get("attachments") or [] if isinstance(att, dict)
    ]
    own = {
        int(d) for d in re.findall(r"\d+", f"{detail.get('number') or ''} {detail.get('full_number') or ''}")
    }
    by_file = []
    for cand in candidates:
        n = cand.get("rfi_number")
        if n is None or int(n) in own:
            continue
        pattern = _filename_number_re(int(n))
        if any(pattern.match(name) for name in names):
            by_file.append(cand)
    if len(by_file) == 1:
        return by_file[0]
    if len(by_file) > 1:
        return None
    subject = normalize_subject(detail.get("subject") or detail.get("title"))
    if len(subject) < 3:
        return None
    by_subject = [c for c in candidates if normalize_subject(c.get("subject")) == subject]
    return by_subject[0] if len(by_subject) == 1 else None


# ── Attachments ───────────────────────────────────────────────────────────────


def _existing_doc(sb, project_id: str, file_id: str) -> dict | None:
    marker = _FILE_MARKER.format(id=file_id)
    rows = (
        sb.table("pm_documents")
        .select("id, filename")
        .eq("project_id", project_id)
        .like("note", f"%{marker}%")
        .limit(1)
        .execute()
    ).data or []
    return rows[0] if rows else None


def sync_attachments(
    sb,
    project_id: str,
    rfi_row: dict,
    detail: dict,
    session,
    settings: Settings,
    stats: dict,
    *,
    kinds: tuple[str, ...] = ("question", "answer"),
    renew: Callable[[], None] = lambda: None,
) -> bool:
    """Download each Procore file once into Documents and attach it to the
    row. Files already attached (external_file_id) are skipped; a file a
    previous link imported is reused from Documents rather than fetched
    again. Oversize and refused files are recorded in stats, never fatal.
    Returns False when a file failed for a reason a later run can fix (a
    storage hiccup): the caller then leaves the row to be fetched again."""
    wanted = [(kind, att) for kind, att in detail_attachments(detail) if kind in kinds]
    if not wanted:
        return True
    complete = True
    have = {
        (str(r.get("external_file_id")), r.get("kind"))
        for r in (
            sb.table("rfi_attachments")
            .select("external_file_id, kind")
            .eq("rfi_id", rfi_row["id"])
            .execute()
        ).data
        or []
        if r.get("external_file_id")
    }
    have_ids = {fid for fid, _ in have}
    label = rfi_label(rfi_row)
    for kind, att in wanted:
        file_id = str(att["id"])
        if file_id in have_ids:
            continue
        renew()
        filename = _attachment_name(att)
        doc = _existing_doc(sb, project_id, file_id)
        if doc is not None:
            stats["attachments_reused"] = stats.get("attachments_reused", 0) + 1
        else:
            doc = _download_to_documents(
                sb, project_id, label, file_id, filename, str(att.get("url")), session, settings, stats
            )
            if doc is None:
                continue
            if doc is _RETRY_LATER:
                complete = False
                continue
        try:
            sb.table("rfi_attachments").insert(
                {
                    "rfi_id": rfi_row["id"],
                    "doc_key": f"pm:{doc['id']}",
                    "kind": kind,
                    "external_file_id": file_id,
                    "created_by": None,
                }
            ).execute()
        except Exception as exc:  # noqa: BLE001
            if not _is_unique_violation(exc):
                raise
        have_ids.add(file_id)
    return complete


# _download_to_documents' answer for a file that failed for now (not refused).
_RETRY_LATER: dict = {}


def _download_to_documents(
    sb, project_id: str, label: str, file_id: str, filename: str, url: str, session,
    settings: Settings, stats: dict,
) -> dict | None:
    from app.services import storage

    cap = int(settings.pm_rfi_sync_attachment_max_bytes)
    size_hint = None
    with tempfile.TemporaryDirectory(prefix="pm-rfi-") as tmp:
        dest = Path(tmp) / "file.bin"
        try:
            size_hint = session.download(url, dest, max_bytes=cap)
        except pc.ProcoreForbidden as exc:
            reason = "oversize" if "larger" in str(exc) else "refused"
            stats.setdefault("skipped_files", []).append(
                {"rfi": label, "filename": filename, "file_id": file_id, "reason": reason,
                 "limit_bytes": cap if reason == "oversize" else None}
            )
            logger.info("pm rfi sync: skipped %s on %s (%s)", filename, label, reason)
            return None
        except pc.ProcoreTransient as exc:
            _record_retry(stats, label, filename, file_id, str(exc))
            return _RETRY_LATER
        content = dest.read_bytes()
    from app.routers.files import _safe_content_type  # the house rule for outside files

    mime = _safe_content_type(filename)
    path = storage.build_object_path(project_id, "pm/rfi", filename)
    try:
        storage.upload_file(path, content, mime)
    except Exception as exc:  # noqa: BLE001 - storage trouble: the next run fetches it again
        _record_retry(stats, label, filename, file_id, f"storage upload failed ({type(exc).__name__})")
        return _RETRY_LATER
    doc = (
        sb.table("pm_documents")
        .insert(
            {
                "project_id": project_id,
                "category": "rfi",
                "storage_path": path,
                "filename": filename[:255],
                "mime_type": mime,
                "size_bytes": size_hint if size_hint is not None else len(content),
                "note": f"Imported from Procore RFI {label} {_FILE_MARKER.format(id=file_id)}",
                "uploaded_by": None,
            }
        )
        .execute()
    ).data[0]
    stats["attachments_downloaded"] = stats.get("attachments_downloaded", 0) + 1
    return doc


def _record_retry(stats: dict, label: str, filename: str, file_id: str, error: str) -> None:
    stats["attachments_failed"] = stats.get("attachments_failed", 0) + 1
    stats.setdefault("skipped_files", []).append(
        {"rfi": label, "filename": filename, "file_id": file_id, "reason": "retry",
         "error": _short(error, 200), "limit_bytes": None}
    )
    logger.warning("pm rfi sync: %s on %s failed for now (%s); the next run retries it",
                   filename, label, _short(error, 200))


# ── The sync ──────────────────────────────────────────────────────────────────


def _new_stats() -> dict:
    return {
        "listed": 0,
        "details_fetched": 0,
        "created": 0,
        "updated": 0,
        "unchanged": 0,
        "skipped_closed": 0,
        "skipped_drafts": 0,
        "auto_linked": 0,
        "conflicts": 0,
        "refused": 0,
        "refused_ids": [],
        "retired": 0,
        "deferred": 0,
        "attachments_downloaded": 0,
        "attachments_reused": 0,
        "attachments_failed": 0,
        "skipped_files": [],
        "new": [],
        "answered": [],
        "closed": [],
    }


def _load_local(sb, project_id: str) -> tuple[dict[str, dict], list[dict]]:
    rows = sb.table("rfis").select(_RFI_COLUMNS).eq("project_id", project_id).execute().data or []
    by_ext = {str(r["external_id"]): r for r in rows if r.get("external_id")}
    candidates = [
        r for r in rows
        if (r.get("source") or "internal") == "internal"
        and not r.get("external_id")
        and not r.get("external_autolink_blocked")
    ]
    return by_ext, candidates


def _same_instant(a: Any, b: Any) -> bool:
    ta, tb = _parse_ts(a), _parse_ts(b)
    return bool(ta and tb and ta == tb)


def _is_draft(row: dict) -> bool:
    return bool(row.get("draft")) or str(row.get("status") or "").lower() == "draft"


def _progress(old: str | None, new: str) -> str:
    """Status never moves backwards (closed is terminal)."""
    old = old or "open"
    return new if _STATUS_RANK.get(new, 0) >= _STATUS_RANK.get(old, 0) else old


def _record_transition(stats: dict, before: dict | None, after: dict, backfill: bool) -> None:
    if backfill:
        return
    label = rfi_label(after)
    if before is None:
        stats["new"].append(label)
        return
    old, new = before.get("status") or "open", after.get("status") or "open"
    if old != "closed" and new == "closed":
        stats["closed"].append(label)
    elif old == "open" and new == "answered":
        stats["answered"].append(label)


def _complete(local: dict | None) -> bool:
    """A row whose last detail landed in full (attachments included):
    external_updated_at is written only then."""
    return bool(local and local.get("external_updated_at"))


def _finished(local: dict | None) -> bool:
    """A closed row that is complete is never fetched again."""
    return bool(local and local.get("status") == "closed" and _complete(local))


def _retire_stale(sb, project_id: str, mapped: dict, listed_ids: set[str] | None) -> bool:
    """An insert collided on (external_number, external_revision). When the
    row holding that number is no longer in Procore's list (deleted there,
    and the number re-used), it is removed so the live RFI can take its
    place. True when a row was removed."""
    if listed_ids is None:
        return False
    query = (
        sb.table("rfis").select("id, external_id").eq("project_id", project_id)
        .eq("source", "procore").eq("external_number", mapped["external_number"])
        .eq("external_revision", mapped["external_revision"])
    )
    stale = [r for r in query.execute().data or [] if str(r.get("external_id")) not in listed_ids]
    for r in stale:
        sb.table("rfis").delete().eq("id", r["id"]).eq("project_id", project_id).execute()
        logger.info(
            "pm rfi sync: removed imported RFI %s (Procore id %s, gone from Procore) on project %s",
            mapped["external_number"], r.get("external_id"), project_id,
        )
    return bool(stale)


def _mark_complete(sb, project_id: str, row: dict, updated_at: str | None) -> dict:
    sb.table("rfis").update({"external_updated_at": updated_at}).eq("id", row["id"]).eq(
        "project_id", project_id
    ).execute()
    return {**row, "external_updated_at": updated_at}


def apply_detail(
    sb,
    project_id: str,
    link: dict,
    detail: dict,
    local: dict | None,
    candidates: list[dict],
    session,
    settings: Settings,
    stats: dict,
    *,
    backfill: bool,
    source_email_id: str | None = None,
    renew: Callable[[], None] = lambda: None,
    listed_ids: set[str] | None = None,
) -> dict | None:
    """Create, update or link one RFI from its Procore detail. Returns the
    local row after the write, or None when nothing was written.

    The row is written with external_updated_at null and gets Procore's
    updated_at only after its attachments landed, so a file that failed for
    now (or a run that died mid-row) leaves the row to be fetched again,
    closed or not, instead of looking unchanged forever."""
    host = link.get("host") or pc.APP_HOST
    mapped = map_detail(detail, host, str(link.get("external_project_id")))
    now_iso = _iso(_now())
    if _finished(local):
        stats["skipped_closed"] += 1
        return None
    auto_linked = False
    if local is None:
        cand = match_internal(detail, candidates)
        if cand is not None:
            local = cand
            candidates.remove(cand)
            auto_linked = True
    external = {k: mapped[k] for k in (
        "external_id", "external_number", "external_revision", "external_status",
        "external_url", "responsible_contractor", "received_from", "initiated_at",
        "ball_in_court", "external_due_date", "external_raw",
    )}
    external["external_updated_at"] = None
    external["external_synced_at"] = now_iso

    if local is not None and (local.get("source") or "internal") == "internal":
        # G3's own RFI: Procore's view, its answer and status; never the question.
        patch = dict(external)
        new_status = _progress(local.get("status"), mapped["status"])
        patch["status"] = new_status
        if mapped["answer"]:
            patch["answer"] = mapped["answer"]
            patch["answered_by"] = mapped["answered_by"] or local.get("answered_by")
            patch["answered_at"] = local.get("answered_at") or mapped["answered_at"]
        elif new_status == "closed" and not local.get("answered_at"):
            patch["answered_at"] = mapped["answered_at"]
        if (local.get("send_status") or "not_sent") == "not_sent":
            patch["send_status"] = "sent_external"
            patch["sent_via"] = "procore"
        if auto_linked:
            patch["source_email_id"] = source_email_id
        updated = (
            sb.table("rfis").update(patch).eq("id", local["id"]).eq("project_id", project_id).execute()
        ).data or [{**local, **patch}]
        row = {**local, **updated[0]}
        if auto_linked:
            stats["auto_linked"] += 1
            logger.info(
                "pm rfi sync: linked Procore RFI %s to internal RFI %s on project %s",
                mapped["external_number"], local.get("rfi_number"), project_id,
            )
        stats["updated"] += 1
        _record_transition(stats, local, row, backfill)
        if sync_attachments(sb, project_id, row, detail, session, settings, stats,
                            kinds=("answer",), renew=renew):
            row = _mark_complete(sb, project_id, row, mapped["external_updated_at"])
        return row

    fields = {
        "subject": mapped["subject"],
        "question": mapped["question"],
        "answer": mapped["answer"],
        "answered_by": mapped["answered_by"],
        "answered_at": mapped["answered_at"],
        "due_at": mapped["external_due_date"],
        "sent_at": _local_date(mapped["initiated_at"]),
        **external,
    }
    if local is not None:
        fields["status"] = _progress(local.get("status"), mapped["status"])
        updated = (
            sb.table("rfis").update(fields).eq("id", local["id"]).eq("project_id", project_id).execute()
        ).data or [{**local, **fields}]
        row = {**local, **updated[0]}
        stats["updated"] += 1
        _record_transition(stats, local, row, backfill)
    else:
        payload = {
            **fields,
            "status": mapped["status"],
            "project_id": project_id,
            "source": "procore",
            "rfi_number": None,
            "created_by": None,
            "source_email_id": source_email_id,
        }
        inserted = None
        for attempt in (1, 2):
            try:
                inserted = sb.table("rfis").insert(payload).execute().data or []
                break
            except Exception as exc:  # noqa: BLE001
                if not _is_unique_violation(exc):
                    raise
                if attempt == 1 and _retire_stale(sb, project_id, mapped, listed_ids):
                    stats["retired"] = stats.get("retired", 0) + 1
                    continue
                stats["conflicts"] += 1
                logger.warning(
                    "pm rfi sync: Procore RFI %s r%s (id %s) collides with an existing row on "
                    "project %s; skipped", mapped["external_number"], mapped["external_revision"],
                    mapped["external_id"], project_id,
                )
                return None
        row = inserted[0] if inserted else payload
        stats["created"] += 1
        skipped = {str(x) for x in link.get("backfill_skipped_ids") or []}
        # An RFI the backfill could not read existed before the link: not "new".
        _record_transition(stats, None, row, backfill or mapped["external_id"] in skipped)
    if sync_attachments(sb, project_id, row, detail, session, settings, stats, renew=renew):
        row = _mark_complete(sb, project_id, row, mapped["external_updated_at"])
    return row


def _resolve_company(sb, link: dict, session) -> str:
    company = link.get("external_company_id")
    if company:
        return str(company)
    company = session.resolve_company_id(str(link["external_project_id"]))
    if not company:
        raise SyncError(
            "Could not find the Procore company for this project. Paste a Procore URL that "
            "includes the company (for example the RFIs tool page) in the link settings."
        )
    sb.table(LINKS_TABLE).update({"external_company_id": company}).eq(
        "project_id", link["project_id"]
    ).execute()
    link["external_company_id"] = company
    return company


def sync_project(
    sb,
    link: dict,
    session,
    settings: Settings,
    *,
    target_external_rfi_id: str | None = None,
    source_email_id: str | None = None,
    renew: Callable[[], None] = lambda: None,
    stats: dict | None = None,
) -> dict:
    """One run over one linked project. Returns the run stats (plus
    `backfill`). A target RFI id fetches just that RFI, except on the first
    run (the backfill always imports everything). `stats` lets the caller
    keep what a run did when it fails part way (process_run)."""
    project_id = link["project_id"]
    pid = str(link["external_project_id"])
    backfill = link.get("backfilled_at") is None
    if stats is None:
        stats = _new_stats()
    stats["backfill"] = backfill
    company = _resolve_company(sb, link, session)
    by_ext, candidates = _load_local(sb, project_id)

    if target_external_rfi_id and not backfill:
        to_fetch = [str(target_external_rfi_id)]
        stats["targeted"] = str(target_external_rfi_id)
        local = by_ext.get(to_fetch[0])
        if _finished(local):
            stats["skipped_closed"] += 1
            to_fetch = []
        listed_ids = None
    else:
        to_fetch = []
        rows = session.list_rfis(pid, company)
        stats["listed"] = len(rows)
        listed_ids = {str(r.get("id")) for r in rows if r.get("id")}
        for row in rows:
            renew()
            if _is_draft(row):
                stats["skipped_drafts"] += 1
                continue
            ext_id = str(row.get("id") or "")
            if not ext_id:
                continue
            local = by_ext.get(ext_id)
            if _finished(local):
                stats["skipped_closed"] += 1
                continue
            if local is not None and _same_instant(local.get("external_updated_at"), row.get("updated_at")):
                stats["unchanged"] += 1
                continue
            to_fetch.append(ext_id)
        cap = int(settings.pm_rfi_sync_max_details_per_run)
        if len(to_fetch) > cap:
            stats["deferred"] = len(to_fetch) - cap
            to_fetch = to_fetch[:cap]

    for ext_id in to_fetch:
        renew()
        try:
            detail = session.get_rfi(pid, ext_id, company)
        except pc.ProcoreForbidden:
            if target_external_rfi_id and ext_id == str(target_external_rfi_id):
                raise SyncError(
                    "Procore refused that RFI: the account cannot see it, or it was removed."
                ) from None
            stats["refused"] += 1
            stats["refused_ids"].append(ext_id)
            continue
        stats["details_fetched"] += 1
        if _is_draft(detail):
            stats["skipped_drafts"] += 1
            continue
        apply_detail(
            sb, project_id, link, detail, by_ext.get(ext_id), candidates, session, settings,
            stats, backfill=backfill, source_email_id=source_email_id, renew=renew,
            listed_ids=listed_ids,
        )
    return stats


# ── Notifications ─────────────────────────────────────────────────────────────


def _list_phrase(count_word: str, labels: list[str]) -> str:
    shown = labels[:_NOTIFY_LIST_MAX]
    more = len(labels) - len(shown)
    tail = f"; and {more} more" if more > 0 else ""
    return f"{len(labels)} {count_word}: " + "; ".join(shown) + tail


def summary_message(stats: dict) -> str | None:
    parts = []
    if stats.get("new"):
        parts.append(_list_phrase("new", stats["new"]))
    if stats.get("answered"):
        parts.append(_list_phrase("answered", stats["answered"]))
    if stats.get("closed"):
        parts.append(_list_phrase("closed", stats["closed"]))
    if not parts:
        return None
    return "RFI updates from the GC's Procore. " + ". ".join(parts) + "."


def notify_after_run(project_id: str, stats: dict, *, notify: Callable = notify_role) -> int:
    """One summary per project per run; the backfill sends at most one
    in-app notice. Returns notifications requested (one per role)."""
    sent = 0
    if stats.get("backfill"):
        # imported_total / linked_total (process_run, counted from the rows)
        # cover a backfill that finished on a later attempt; this attempt's
        # own counts miss what the earlier attempts saved.
        imported = int(stats.get("imported_total",
                                 int(stats.get("created", 0)) + int(stats.get("auto_linked", 0))))
        if not imported:
            return 0
        linked = int(stats.get("linked_total", stats.get("auto_linked", 0)))
        message = (
            f"Linked to the GC's Procore project: imported {imported} RFI"
            f"{'' if imported == 1 else 's'}"
            + (f", {linked} matched to G3's own RFIs" if linked else "")
            + "."
        )
        for role in NOTIFY_ROLES:
            notify(role, project_id, NOTIFY_LINKED, message, mirror_email=False,
                   metadata={"imported": imported})
            sent += 1
        return sent
    message = summary_message(stats)
    if not message:
        return 0
    metadata = {
        "new": len(stats.get("new") or []),
        "answered": len(stats.get("answered") or []),
        "closed": len(stats.get("closed") or []),
    }
    for role in NOTIFY_ROLES:
        notify(role, project_id, NOTIFY_SUMMARY, message, metadata=metadata)
        sent += 1
    return sent


# ── Running a claimed run ─────────────────────────────────────────────────────


_CARRIED = ("new", "answered", "closed")


def _carry_forward(stats: dict, previous: Any) -> None:
    """Seed this attempt's transitions with the ones an earlier attempt of
    the same run saved before it failed (those rows now look unchanged)."""
    if not isinstance(previous, dict):
        return
    for key in _CARRIED:
        merged = list(previous.get(key) or []) + list(stats.get(key) or [])
        stats[key] = list(dict.fromkeys(str(x) for x in merged))


def _same_target(a: dict | None, b: dict | None) -> bool:
    return bool(
        a and b and a.get("platform") == b.get("platform") and a.get("host") == b.get("host")
        and str(a.get("external_project_id") or "") == str(b.get("external_project_id") or "")
    )


def _imported_counts(sb, project_id: str) -> tuple[int, int]:
    """(rows carrying a Procore RFI, of which G3's own linked rows) on the
    project: what the backfill imported across all of its attempts."""
    rows = (
        sb.table("rfis").select("source").eq("project_id", project_id)
        .not_.is_("external_id", "null").execute()
    ).data or []
    linked = sum(1 for r in rows if (r.get("source") or "internal") == "internal")
    return len(rows), linked


def _notify_partial(project_id: str, stats: dict, notify: Callable, run_id: Any) -> None:
    """A run that failed for good still announces what it already saved."""
    if stats.get("backfill"):
        return
    try:
        notify_after_run(project_id, stats, notify=notify)
    except Exception:  # noqa: BLE001
        logger.exception("pm rfi sync: notifications for failed run %s failed", run_id)


def process_run(
    sb,
    run: dict,
    settings: Settings,
    *,
    session_factory: Callable[[str, Settings], Any] | None = None,
    notify: Callable = notify_role,
) -> str:
    """Run one claimed run to its end. Returns the run's final status
    ('done', 'failed', 'queued' for a retry, or 'lost')."""
    project_id = run["project_id"]
    link = get_link(sb, project_id)
    if not _is_procore_link(link):
        _finish(sb, run, "failed", error="The project is not linked to a Procore project.")
        return "failed"
    if not link.get("sync_enabled") and run.get("trigger") != "manual":
        _finish(sb, run, "failed", error="Sync is turned off for this project.")
        return "failed"
    if not settings.procore_configured:
        message = "Procore credentials are not configured (PROCORE_LOGIN_EMAIL / PROCORE_LOGIN_PASSWORD)."
        _finish(sb, run, "failed", error=message)
        _update_link_status(sb, project_id, "failed", message)
        return "failed"
    factory = session_factory or open_session
    renew = _renewer(sb, run, settings)
    stats = _new_stats()
    _carry_forward(stats, run.get("stats"))
    session = factory(link.get("host") or pc.APP_HOST, settings)
    try:
        sync_project(
            sb, link, session, settings,
            target_external_rfi_id=run.get("target_external_rfi_id"),
            source_email_id=run.get("source_email_id"),
            renew=renew,
            stats=stats,
        )
    except _LeaseLost:
        logger.warning("pm rfi sync: lost the lease on run %s; another worker has it", run["id"])
        return "lost"
    except (pc.ProcoreTransient, pc.ProcoreUnavailable, httpx.TransportError) as exc:
        # httpx.TransportError here is the database or storage connection
        # (Procore's own network errors arrive as ProcoreTransient): a dropped
        # network or a sleeping laptop, worth a retry rather than a failure.
        until = getattr(exc, "locked_until", None)
        locked = isinstance(exc, pc.ProcoreLoginLocked)
        attempts = int(run.get("attempts") or 1)
        message = str(exc)
        if isinstance(exc, httpx.TransportError):
            message = f"The connection dropped during the sync ({type(exc).__name__}); it will be retried."
        if not locked and attempts < settings.pm_rfi_sync_max_attempts:
            retry_at = until or (_now() + timedelta(seconds=settings.pm_rfi_sync_retry_seconds))
            # The transitions already saved ride along to the retry.
            _finish(sb, run, "queued", error=message, next_attempt_at=retry_at,
                    stats={k: stats.get(k) or [] for k in _CARRIED})
            logger.info("pm rfi sync: run %s will retry at %s: %s", run["id"], _iso(retry_at), message)
            return "queued"
        _finish(sb, run, "failed", error=message, stats=stats)
        _update_link_status(sb, project_id, "failed", message)
        _notify_partial(project_id, stats, notify, run["id"])
        logger.warning("pm rfi sync: run %s failed: %s", run["id"], message)
        return "failed"
    except (pc.ProcoreForbidden, pc.ProcoreSessionRejected, SyncError) as exc:
        message = str(exc)
        if isinstance(exc, pc.ProcoreForbidden):
            message = (
                "Procore refused the project's RFIs: the G3 Procore account cannot see this "
                "project, or the link is wrong."
            )
        _finish(sb, run, "failed", error=message, stats=stats)
        _update_link_status(sb, project_id, "failed", message)
        _notify_partial(project_id, stats, notify, run["id"])
        logger.warning("pm rfi sync: run %s failed: %s", run["id"], message)
        return "failed"
    except Exception as exc:  # noqa: BLE001 - one bad run must not kill the loop
        logger.exception("pm rfi sync: run %s crashed", run["id"])
        message = f"The sync stopped on an unexpected error ({type(exc).__name__})."
        _finish(sb, run, "failed", error=message, stats=stats)
        _update_link_status(sb, project_id, "failed", message)
        _notify_partial(project_id, stats, notify, run["id"])
        return "failed"
    finally:
        try:
            session.close()
        except Exception:  # noqa: BLE001
            pass
    _finish(sb, run, "done", stats=stats)
    current = get_link(sb, project_id)
    if not _same_target(link, current):
        # The link moved while this run worked: what it imported belongs to
        # the old Procore project. Drop it; the new link's own run (queued
        # by PUT /pm-link) does the backfill and owns the link status.
        if _is_procore_link(link):
            purge_imported(sb, project_id)
        logger.info("pm rfi sync: run %s finished after the link changed; its import was dropped", run["id"])
        return "done"
    # The backfill is complete only when no detail waits on the per-run cap;
    # RFIs it could not read are remembered so they never count as new.
    backfilled = bool(stats.get("backfill")) and not stats.get("deferred")
    _update_link_status(sb, project_id, "ok", None, backfilled=backfilled,
                        skipped_ids=stats.get("refused_ids"))
    if backfilled:
        try:
            stats["imported_total"], stats["linked_total"] = _imported_counts(sb, project_id)
        except Exception:  # noqa: BLE001 - the notice falls back to this attempt's counts
            logger.exception("pm rfi sync: counting the backfill of project %s failed", project_id)
    if not stats.get("backfill") or backfilled:
        try:
            notify_after_run(project_id, stats, notify=notify)
        except Exception:  # noqa: BLE001 - the import is done; a bell failure is logged only
            logger.exception("pm rfi sync: notifications for run %s failed", run["id"])
    logger.info(
        "pm rfi sync: run %s (%s) on project %s done: %s", run["id"], run.get("trigger"), project_id,
        {k: v for k, v in stats.items() if not isinstance(v, list)},
    )
    return "done"


def run_pending(sb, settings: Settings, *, max_runs: int = 5,
                session_factory: Callable[[str, Settings], Any] | None = None) -> int:
    """Claim and run up to `max_runs` runs. Returns runs processed."""
    done = 0
    for _ in range(max_runs):
        run = claim_next(sb, settings, _now())
        if run is None:
            break
        process_run(sb, run, settings, session_factory=session_factory)
        done += 1
    return done


# ── Email trigger scan ────────────────────────────────────────────────────────


def _load_scan_state(sb) -> dict:
    rows = sb.table(SCAN_TABLE).select("*").eq("id", SCAN_ROW_ID).limit(1).execute().data or []
    if rows:
        return rows[0]
    try:
        sb.table(SCAN_TABLE).insert({"id": SCAN_ROW_ID}).execute()
    except Exception as exc:  # noqa: BLE001
        if not _is_unique_violation(exc):
            raise
    return {"id": SCAN_ROW_ID}


def _claim_scan(sb, state: dict, now: datetime, seconds: int) -> bool:
    lease = _parse_ts(state.get("lease_until"))
    if lease and lease > now and state.get("lease_owner") != WORKER_ID:
        return False
    query = sb.table(SCAN_TABLE).update(
        {"lease_until": _iso(now + timedelta(seconds=seconds)), "lease_owner": WORKER_ID}
    ).eq("id", SCAN_ROW_ID)
    if state.get("lease_until"):
        query = query.eq("lease_until", state["lease_until"])
    else:
        query = query.is_("lease_until", "null")
    return bool(query.execute().data)


def _pm_projects(sb, ids: set[str]) -> set[str]:
    if not ids:
        return set()
    rows = sb.table("projects").select("id, pm_stage").in_("id", sorted(ids)).execute().data or []
    return {r["id"] for r in rows if r.get("pm_stage")}


def auto_link_from_email(sb, project_id: str, rfi_link: pc.ProcoreRfiLink, body: str,
                         email_id: str | None) -> dict | None:
    """Link an unlinked PM project to the Procore project an RFI email names.
    Refused (None) when another BDR project is linked to that Procore
    project. The company id comes from a storage link in the same email,
    else the first run resolves it."""
    if _procore_conflict(sb, project_id, rfi_link.host, rfi_link.project_id):
        logger.info(
            "pm rfi sync: email %s names Procore project %s, already linked elsewhere; not linking %s",
            email_id, rfi_link.project_id, project_id,
        )
        return None
    row = {
        "project_id": project_id,
        "platform": "procore",
        "host": rfi_link.host,
        "external_project_id": rfi_link.project_id,
        "external_company_id": pc.extract_company_id(body, rfi_link.project_id),
        "external_url": f"https://{rfi_link.host}/{rfi_link.project_id}/project/home",
        "sync_enabled": True,
        "linked_by": None,
        "linked_via": "email",
    }
    try:
        inserted = sb.table(LINKS_TABLE).insert(row).execute().data or [row]
    except Exception as exc:  # noqa: BLE001 - another worker linked it, or a conflict
        if _is_unique_violation(exc):
            return get_link(sb, project_id)
        raise
    logger.info(
        "pm rfi sync: project %s auto-linked to Procore project %s on %s from email %s",
        project_id, rfi_link.project_id, rfi_link.host, email_id,
    )
    return inserted[0]


_PROCORE_SENDER_RE = re.compile(r"@([a-z0-9-]+\.)*(procore\.com|procoretech\.com)$")


def _trusted_for_autolink(email: dict, rfi_link: pc.ProcoreRfiLink) -> bool:
    """Auto-link only on an email that is plainly Procore's own: sent from a
    procore.com / procoretech.com address (Procore notifications come from
    <gc>_notifications@procoretech.com), or carrying a signed
    storage.procore.com link whose projectId is the RFI link's project.
    Anything else (a forward, a vendor, a spoofed sender) waits for a
    person to link the project."""
    sender = str(email.get("from_address") or "").strip().lower()
    if _PROCORE_SENDER_RE.search(sender):
        return True
    for url in pc._links(email.get("body_text")):
        try:
            parsed = urlparse(url)
        except ValueError:
            continue
        if (parsed.hostname or "").lower() != pc.STORAGE_HOST:
            continue
        if pc._query_id(parsed, "projectId", "project_id") == rfi_link.project_id and pc._query_id(
            parsed, "companyId", "company_id"
        ):
            return True
    return False


def _email_trigger(sb, email: dict, links: list[pc.ProcoreRfiLink], stats: dict) -> None:
    project_id = email.get("project_id")
    link = get_link(sb, project_id)
    if link is None:
        if not _trusted_for_autolink(email, links[0]):
            stats["autolink_untrusted"] = stats.get("autolink_untrusted", 0) + 1
            logger.info(
                "pm rfi sync: email %s names Procore project %s but is not from Procore; "
                "project %s left for manual linking", email.get("id"), links[0].project_id, project_id,
            )
            return
        link = auto_link_from_email(sb, project_id, links[0], email.get("body_text") or "", email.get("id"))
        if link is None:
            return
        stats["auto_linked_projects"] += 1
    if link.get("platform") != "procore" or not link.get("sync_enabled"):
        return
    mine = sorted({
        lk.rfi_id for lk in links
        if lk.host == link.get("host") and lk.project_id == str(link.get("external_project_id"))
    })
    if not mine:
        stats["other_project_links"] += 1
        return
    target = mine[0] if len(mine) == 1 else None
    _, created = enqueue_run(
        sb, project_id, "email", target_external_rfi_id=target, source_email_id=email.get("id")
    )
    stats["runs_queued"] += int(created)


def _later(a: Any, b: Any) -> Any:
    """The later of two timestamps as stored (ISO strings), compared parsed."""
    ta, tb = _parse_ts(a), _parse_ts(b)
    if ta is None:
        return b
    if tb is None:
        return a
    return b if tb > ta else a


def _missing_table(exc: BaseException) -> bool:
    msg = str(exc)
    return any(s in msg for s in ("PGRST205", "42P01", "does not exist", "Could not find the table"))


def scan_emails(sb, settings: Settings, now: datetime | None = None, *, limit: int = 200) -> dict:
    """Queue targeted runs for filed PM emails that carry a Procore RFI link,
    and for email_classifications rows labeled `rfis` (when that table
    exists). Checkpointed on updated_at; the one-run-per-email index makes a
    re-read harmless."""
    now = now or _now()
    stats = {"emails": 0, "runs_queued": 0, "auto_linked_projects": 0, "other_project_links": 0,
             "classified": 0}
    state = _load_scan_state(sb)
    if not _claim_scan(sb, state, now, max(60, settings.pm_rfi_email_scan_interval_seconds)):
        return stats
    lookback = now - timedelta(hours=settings.pm_rfi_email_scan_lookback_hours)
    checkpoint = state.get("emails_checkpoint") or _iso(lookback)
    rows = (
        sb.table("ingested_emails")
        .select("id, project_id, direction, from_address, body_text, updated_at")
        .gte("updated_at", checkpoint)
        .eq("direction", "inbound")
        .ilike("body_text", "*project*rfi*show*")
        .order("updated_at")
        .limit(limit)
        .execute()
    ).data or []
    new_checkpoint = checkpoint
    for email in rows:
        # Past every row read, filed or not: an email filed later moves its
        # updated_at and is read again then.
        new_checkpoint = _later(new_checkpoint, email.get("updated_at"))
    rows = [r for r in rows if r.get("project_id")]
    pm_ids = _pm_projects(sb, {r["project_id"] for r in rows})
    for email in rows:
        if email["project_id"] not in pm_ids:
            continue
        links = pc.extract_rfi_links(email.get("body_text"))
        if not links:
            continue
        stats["emails"] += 1
        try:
            _email_trigger(sb, email, links, stats)
        except Exception:  # noqa: BLE001 - one email must not stop the scan
            logger.exception("pm rfi sync: email %s trigger failed", email.get("id"))

    class_checkpoint = state.get("classifications_checkpoint") or _iso(lookback)
    new_class_checkpoint = class_checkpoint
    try:
        classified = (
            sb.table("email_classifications")
            .select("email_id, project_id, status, ai_labels, user_labels, updated_at")
            .gte("updated_at", class_checkpoint)
            .eq("status", "done")
            .order("updated_at")
            .limit(limit)
            .execute()
        ).data or []
    except Exception as exc:  # noqa: BLE001 - the table ships with a separate migration
        if not _missing_table(exc):
            logger.warning("pm rfi sync: email_classifications read failed: %s", _short(exc, 200))
        classified = []
    if classified:
        email_ids = [c["email_id"] for c in classified if c.get("email_id")]
        emails = {
            e["id"]: e
            for e in (
                sb.table("ingested_emails").select("id, project_id").in_("id", email_ids).execute()
            ).data
            or []
        }
        pm_ids = _pm_projects(sb, {e.get("project_id") for e in emails.values() if e.get("project_id")})
        for c in classified:
            new_class_checkpoint = _later(new_class_checkpoint, c.get("updated_at"))
            labels = c.get("user_labels") if c.get("user_labels") is not None else c.get("ai_labels")
            if not any(isinstance(lb, dict) and lb.get("category") == "rfis" for lb in labels or []):
                continue
            email = emails.get(c.get("email_id")) or {}
            pid = email.get("project_id")
            if not pid or pid not in pm_ids:
                continue
            link = get_link(sb, pid)
            if not link or link.get("platform") != "procore" or not link.get("sync_enabled"):
                continue
            stats["classified"] += 1
            _, created = enqueue_run(sb, pid, "email", source_email_id=c["email_id"])
            stats["runs_queued"] += int(created)

    sb.table(SCAN_TABLE).update(
        {
            "emails_checkpoint": new_checkpoint,
            "classifications_checkpoint": new_class_checkpoint,
            "lease_until": None,
            "lease_owner": None,
        }
    ).eq("id", SCAN_ROW_ID).execute()
    if stats["runs_queued"] or stats["auto_linked_projects"]:
        logger.info("pm rfi sync: email scan %s", stats)
    return stats


# ── Manual link / unlink (routers) ────────────────────────────────────────────


@dataclass
class LinkActionError(Exception):
    status_code: int
    message: str

    def __str__(self) -> str:
        return self.message


def link_rows(sb, project_id: str, imported_id: str, internal_id: str) -> dict:
    """Merge an imported Procore row into G3's internal row: the external
    fields and every attachment move onto the internal row, the imported row
    is deleted. Returns the internal row."""
    def one(rid: str) -> dict | None:
        rows = sb.table("rfis").select("*").eq("id", rid).eq("project_id", project_id).execute().data or []
        return rows[0] if rows else None

    imported, internal = one(imported_id), one(internal_id)
    if imported is None or internal is None:
        raise LinkActionError(404, "RFI not found")
    if (imported.get("source") or "internal") != "procore":
        raise LinkActionError(409, "Only an RFI imported from Procore can be linked to a G3 RFI.")
    if (internal.get("source") or "internal") != "internal":
        raise LinkActionError(409, "Choose one of G3's own RFIs to link to.")
    if internal.get("external_id"):
        raise LinkActionError(409, "That G3 RFI is already linked to a Procore RFI.")

    # Attachments first (the delete below cascades). Same doc + kind already
    # on the internal row: drop the imported duplicate.
    have = {
        (r["doc_key"], r.get("kind"))
        for r in (
            sb.table("rfi_attachments").select("doc_key, kind").eq("rfi_id", internal_id).execute()
        ).data
        or []
    }
    for att in (
        sb.table("rfi_attachments").select("id, doc_key, kind").eq("rfi_id", imported_id).execute()
    ).data or []:
        if (att["doc_key"], att.get("kind")) in have:
            continue
        sb.table("rfi_attachments").update({"rfi_id": internal_id}).eq("id", att["id"]).execute()

    moved = {k: imported.get(k) for k in EXTERNAL_FIELDS}
    sb.table("rfis").delete().eq("id", imported_id).eq("project_id", project_id).execute()
    patch = {**moved, "external_autolink_blocked": False}
    new_status = _progress(internal.get("status"), imported.get("status") or "open")
    patch["status"] = new_status
    if imported.get("answer") and not internal.get("answer"):
        patch.update(
            answer=imported.get("answer"),
            answered_by=imported.get("answered_by"),
            answered_at=internal.get("answered_at") or imported.get("answered_at"),
        )
    if (internal.get("send_status") or "not_sent") == "not_sent":
        patch.update(send_status="sent_external", sent_via="procore")
    updated = (
        sb.table("rfis").update(patch).eq("id", internal_id).eq("project_id", project_id).execute()
    ).data or [{**internal, **patch}]
    return updated[0]


def unlink_row(sb, project_id: str, rfi_id: str) -> dict:
    """Split a linked G3 RFI from its Procore RFI: the external fields and
    the Procore-imported attachments come off the internal row, which is
    kept out of auto-linking; the next sync re-imports the Procore RFI as
    its own read-only row (its files are reused from Documents)."""
    rows = sb.table("rfis").select("*").eq("id", rfi_id).eq("project_id", project_id).execute().data or []
    if not rows:
        raise LinkActionError(404, "RFI not found")
    row = rows[0]
    if (row.get("source") or "internal") != "internal" or not row.get("external_id"):
        raise LinkActionError(409, "This RFI is not linked to a Procore RFI.")
    atts = (
        sb.table("rfi_attachments").select("id, external_file_id").eq("rfi_id", rfi_id).execute()
    ).data or []
    drop = [a["id"] for a in atts if a.get("external_file_id")]
    if drop:
        sb.table("rfi_attachments").delete().eq("rfi_id", rfi_id).in_("id", drop).execute()
    patch: dict = {k: None for k in EXTERNAL_FIELDS}
    patch["external_revision"] = "0"
    patch["external_autolink_blocked"] = True
    updated = (
        sb.table("rfis").update(patch).eq("id", rfi_id).eq("project_id", project_id).execute()
    ).data or [{**row, **patch}]
    return updated[0]


# ── The loop ──────────────────────────────────────────────────────────────────


_last_scan_at = 0.0


def tick(sb=None, settings: Settings | None = None) -> dict:
    """One scheduler tick: queue the daily slot, scan emails on their own
    cadence, then run what is claimable."""
    global _last_scan_at
    s = settings or get_settings()
    sb = sb or get_supabase()
    out = {"daily_queued": 0, "runs": 0}
    out["daily_queued"] = schedule_daily(sb, s, _now())
    if s.pm_rfi_email_scan_enabled:
        mono = time.monotonic()
        if mono - _last_scan_at >= s.pm_rfi_email_scan_interval_seconds:
            _last_scan_at = mono
            try:
                out["email_scan"] = scan_emails(sb, s)
            except Exception:  # noqa: BLE001 - the runs below still go
                logger.exception("pm rfi sync: email scan failed")
    out["runs"] = run_pending(sb, s)
    return out


async def polling_loop() -> None:
    settings = get_settings()
    logger.info(
        "pm rfi sync: scheduler started (daily %s PT, Procore account %s, email scan %s)",
        settings.pm_rfi_sync_daily_time,
        "configured" if settings.procore_configured else "NOT configured",
        "on" if settings.pm_rfi_email_scan_enabled else "off",
    )
    while True:
        try:
            await asyncio.to_thread(tick)
        except Exception:  # noqa: BLE001 - the loop must survive any tick failure
            logger.exception("pm rfi sync tick failed")
        await asyncio.sleep(settings.pm_rfi_sync_poll_seconds)
