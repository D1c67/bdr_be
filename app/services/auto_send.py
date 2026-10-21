"""Auto send (docs/AUTO_SEND.md): at 5:05 PM Pacific on a project's internal
bid date, if nobody sent or marked the proposal submitted, email every GC a
proposal priced at the external estimator's estimate plus the markup.

What this module never does: write proposal_sends, proposal_send_events,
stage_events or project_category_state. The normal Send Out flow stays
exactly as it was; the only bridge is the explicit "Accept as submitted"
(apply_auto_send_acceptance, section 9). Its own records are:
  auto_send_settings   the IT Admin switch + the markup %
  auto_send_runs       one per (project, bid date): the claim two uvicorn
                       workers race on, and the frozen record of what went out
  auto_send_gcs        one per GC of a run (recipients, document, email)

Isolation is the same as Send Out's: every document is rendered for one GC,
validated against the other GCs' names, converted to PDF and re-validated on
the PDF text before its own email. Before each email the project is re-read
for a human send (proposal_sends sent/sending or an active external
submission): a person who sends at 5:04 wins and auto send stops.

Outside production GC emails are refused unless AUTO_SEND_GC_REDIRECT is set;
then every GC email goes ONLY to that address (no CC) with the original To in
the subject.

Sync code only: callers run it in the poller thread or a sync route.
"""

from __future__ import annotations

import io
import logging
import threading
import time as _time
import zipfile
from dataclasses import replace
from datetime import date, datetime, time, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from zoneinfo import ZoneInfo

from docx.oxml.ns import qn
from lxml import etree
from postgrest.exceptions import APIError

from app.core.config import get_settings
from app.core.roles import (
    AUTO_SEND_ACTION_ROLES,
    AUTO_SEND_NOTIFY_ROLES,
    AUTO_SEND_PCT_ROLES,
    Role,
)
from app.core.supabase_client import get_supabase
from app.services import (
    email_branding,
    external_submission,
    graph_email,
    office_preview,
    proposal_docx,
    proposal_send,
    storage,
    workflow,
)
from app.services.due_reminders import _page_all, _pg_ts
from app.services.notifications import (
    audit,
    dismiss_notifications,
    notify_role,
    notify_roles,
)

logger = logging.getLogger(__name__)

TZ = ZoneInfo("America/Los_Angeles")

SETTINGS_TABLE = "auto_send_settings"
DEFAULT_MARKUP_PCT = Decimal("10")
MAX_MARKUP_PCT = Decimal("100")
_CENT = Decimal("0.01")

# Never auto sent and never reported: the bid is out of the pipeline, or
# already went out.
SILENT_STAGES = ("pm_only", "cp_only", "declined", "submitted", "bid_outcome")

# A run whose last heartbeat (auto_send_runs.updated_at, bumped before every
# GC step, or any of its GC rows' updated_at) is older than this is presumed
# crashed (reclaim_stuck). It must stay well above the longest single step
# between heartbeats (one GC: two PDF conversion attempts plus the Graph
# send), which _stuck_minutes() enforces from the live converter timeout.
STUCK_MINUTES = 15
_GRAPH_SEND_SECONDS = 30

RUN_SENDABLE_STATUSES = ("missed", "failed", "partial")
# GC row statuses a send may claim. 'unknown' only with force_unknown.
_GC_RETRY_STATUSES = ("pending", "failed", "skipped")

# Owner-facing reasons (plain English). The estimate parser's codes live in
# auto_send_estimate.REASON_MESSAGES; estimate_message() reads both.
REASON_MESSAGES = {
    "not_go": "Go/No-Go has not been decided as Go.",
    "no_estimator": "No estimator is assigned to this project.",
    "estimate_not_received": "The estimator has not submitted an estimate yet.",
    "dev_estimator": "The estimator on this project is the dev account, so it is not auto sent.",
    "no_scope_lines": "There are no scope lines yet. Add them in the Auto send panel.",
    "missing_project_fields": "The project is missing its address, labor time, or wage type.",
    "bad_characters": (
        "Remove the < and > characters from the project name, number, address, or GC names."
    ),
    "no_gcs": "No GCs are on this project.",
    "no_gc_emails": "None of the GCs have a contact with an email address.",
    "gc_no_email": "{gc} has no contact with an email address.",
    "render_failed": "The proposal for {gc} could not be built: {detail}",
    "convert_failed": (
        "The PDF converter is not responding, so the proposal could not be attached."
    ),
    "pdf_too_large": "The proposal PDF for {gc} is too large to email.",
    "send_failed": "The email to {gc} failed: {detail}",
    "outcome_unknown": (
        "The email to {gc} timed out and may or may not have gone out. "
        "Check the bids mailbox before sending again."
    ),
    "human_sent": (
        "A proposal was sent or marked submitted from the project while auto send "
        "was running, so auto send stopped."
    ),
    "interrupted": (
        "The send was interrupted before it finished. Open the project to check it "
        "and send now."
    ),
    "missed": (
        "The server was not running at 5:05 PM, so this was not sent. "
        "Open the project to send it now."
    ),
    "gc_emails_blocked": (
        "GC emails are blocked outside production. Set AUTO_SEND_GC_REDIRECT to test."
    ),
    "human_send_unknown": (
        "A proposal email from Send Out timed out and may have reached the GC. Check "
        "Sent Items, then finish the send from Send Out or mark it submitted."
    ),
    "switched_off": (
        "Auto send was turned off in Settings before this GC was emailed."
    ),
    "earlier_outcome_unknown": (
        "An earlier auto send to {gc} timed out and may already have gone out, so it "
        "was not sent again. Check the bids mailbox, then confirm Send now to send it."
    ),
    "earlier_run_in_progress": (
        "Another auto send for this project was still sending, so this one did not send. "
        "Open the Auto send panel to send it now."
    ),
}

# Why a project is silently ignored (evaluate's silent_reason). Shown only
# when a scheduled claim loses a race to one of them.
SILENT_MESSAGES = {
    "stage": "The project is not being bid any more, or the bid already went out.",
    "abandoned": "The project was abandoned.",
    "proposal_sent": "A proposal was already sent or marked submitted for this project.",
    "external_submission": "The bid was already marked submitted from the project side menu.",
    "gone_out": "The bid already went out.",
    "already_auto_sent": "This project was already auto sent.",
}

# Refusals of Send now / Accept as submitted, by code. The accept codes are the
# 'auto_send_accept:<code>' values apply_auto_send_acceptance raises (with a
# few aliases), plus the same checks run here first for a clean message.
ACTION_MESSAGES = {
    "not_found": "Project not found.",
    "project_not_found": "Project not found.",
    "disabled": "Auto send is turned off. An IT Admin can turn it on in Settings.",
    "nothing_to_send": (
        "There is nothing to send now. Only a missed, failed, or partly sent auto send "
        "can be sent again."
    ),
    "unknown_needs_force": (
        "The GCs left to send timed out earlier and may already have the proposal. "
        "Check the bids mailbox, then confirm to send again."
    ),
    "busy": "Auto send is sending this project right now. Wait for it to finish.",
    "date_moved": (
        "The internal bid date moved to a later day. Auto send will run on the new date "
        "instead."
    ),
    "run_not_for_project": "That auto send was not found on this project.",
    "run_not_sent": "Only an auto send that went out can be accepted as submitted.",
    "already_accepted": "This auto send was already accepted as submitted.",
    "already_sent": "A proposal was already sent or marked submitted for this project.",
    "gc_already_sent": "A GC's proposal was sent while you were working. Reopen and try again.",
    "already_submitted": "This bid is already submitted.",
    "external_submission_active": (
        "The bid was already marked submitted from the project side menu."
    ),
    "outcome_recorded": "A win/loss outcome is already recorded for this bid.",
    "never_bid": "This project was never bid in the app.",
    "declined": "This project was declined at Go/No-Go.",
    "abandoned": "This project has been abandoned.",
    "stale": "The project changed while you were working. Reopen Auto send and try again.",
    "no_sent_gcs": "No GC received this auto send, so there is nothing to accept.",
}


class AutoSendError(Exception):
    """User-actionable failure; the router surfaces .args[0] as the detail and
    `code` as the X-Error-Code header."""

    def __init__(self, message: str, status_code: int = 409, code: str | None = None):
        super().__init__(message)
        self.status_code = status_code
        self.code = code


def _refuse(code: str, status_code: int = 409) -> AutoSendError:
    return AutoSendError(ACTION_MESSAGES.get(code, "Auto send refused."), status_code, code)


# ── small helpers ──────────────────────────────────────────────────────────


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


def _parse_ts(v) -> datetime | None:
    return external_submission._parse_ts(v)


def _dec(v) -> Decimal | None:
    return Decimal(str(v)) if v is not None else None


def _pause() -> None:
    _time.sleep(1)  # Exchange throttling courtesy between GCs (Send Out precedent)


def _bookkeep(label: str, fn, *args, **kwargs) -> None:
    """Run a follow-up (audit, bell) that must never undo the work it records."""
    try:
        fn(*args, **kwargs)
    except Exception:  # noqa: BLE001 - see docstring
        logger.exception("auto send %s failed", label)


def _reason(code: str, gc_id: str | None = None, **fmt) -> dict:
    message = REASON_MESSAGES.get(code) or estimate_message(code)
    try:
        message = message.format(**fmt) if fmt else message
    except (KeyError, IndexError):
        pass
    out = {"code": code, "message": message}
    if gc_id:
        out["gc_id"] = gc_id
    return out


def auto_subject(project: dict) -> str:
    """The auto send subject. It must differ from Send Out's
    (proposal_send.build_cover_email) so Send Out's crash reclaim, which
    proves a send by email_log subject + To equality, never mistakes an auto
    send email for its own."""
    return f"[G3 Electrical] Proposal: {project['name']} ({project['number']})"


def _project_tag(project: dict) -> str:
    return external_submission._project_tag(project)


# ── the estimate parser (app/services/auto_send_estimate.py) ───────────────
# Imported lazily so this module (and its tests) load without it; tests
# monkeypatch these three names.


def load_estimate(project_id: str, *, sb=None) -> dict:
    from app.services import auto_send_estimate

    return auto_send_estimate.load_estimate(project_id, sb=sb)


def apply_markup(materials: Decimal, labor: Decimal, pct: Decimal):
    from app.services import auto_send_estimate

    return auto_send_estimate.apply_markup(materials, labor, pct)


def estimate_message(code: str | None) -> str:
    try:
        from app.services.auto_send_estimate import REASON_MESSAGES as parser_messages
    except ImportError:
        parser_messages = {}
    return (
        parser_messages.get(code or "")
        or REASON_MESSAGES.get(code or "")
        or "The estimate could not be read."
    )


# ── settings row ───────────────────────────────────────────────────────────


def get_settings_row(sb=None) -> dict:
    """The single settings row, created with the column defaults if missing
    (0159 seeds it). A read failure raises: the poller does nothing that tick
    (fail closed)."""
    sb = sb or get_supabase()
    rows = (sb.table(SETTINGS_TABLE).select("*").eq("id", 1).limit(1).execute()).data or []
    if rows:
        return rows[0]
    sb.table(SETTINGS_TABLE).upsert({"id": 1}, ignore_duplicates=True).execute()
    rows = (sb.table(SETTINGS_TABLE).select("*").eq("id", 1).limit(1).execute()).data or []
    if rows:
        return rows[0]
    logger.warning("auto_send_settings row missing after insert; using defaults (off)")
    return {
        "id": 1,
        "enabled": False,
        "markup_pct": str(DEFAULT_MARKUP_PCT),
        "enabled_changed_at": None,
        "updated_by": None,
        "updated_at": None,
    }


def markup_pct_of(row: dict) -> Decimal:
    try:
        pct = Decimal(str(row.get("markup_pct")))
    except (InvalidOperation, ValueError):
        return DEFAULT_MARKUP_PCT.quantize(_CENT)
    if not pct.is_finite():
        return DEFAULT_MARKUP_PCT.quantize(_CENT)
    return pct.quantize(_CENT, rounding=ROUND_HALF_UP)


def validate_markup_pct(pct) -> Decimal:
    """Parse and range-check a markup %, rounded to 2 places. Every bad input
    raises ValueError (range checked before quantize, so 1e30 never reaches
    it)."""
    try:
        value = Decimal(str(pct))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError("The markup must be a percentage between 0 and 100.") from exc
    if not value.is_finite() or value < 0 or value > MAX_MARKUP_PCT:
        raise ValueError("The markup must be a percentage between 0 and 100.")
    return value.quantize(_CENT, rounding=ROUND_HALF_UP)


def set_enabled(enabled: bool, actor_id: str) -> dict:
    """The IT Admin switch. Stamps enabled_changed_at when the value flips:
    missed detection ignores send instants before the latest flip to ON."""
    sb = get_supabase()
    row = get_settings_row(sb)
    now = _iso(_now())
    was = bool(row.get("enabled"))
    update = {"enabled": bool(enabled), "updated_by": actor_id, "updated_at": now}
    if was != bool(enabled):
        update["enabled_changed_at"] = now
    sb.table(SETTINGS_TABLE).update(update).eq("id", 1).execute()
    # audit_log.entity_id is a uuid column: the one-row table's id rides in
    # the payload instead.
    _bookkeep(
        "enabled audit", audit, actor_id, "auto_send.enabled_changed", SETTINGS_TABLE, None,
        {"id": 1, "from": was, "to": bool(enabled)},
    )
    return get_settings_row(sb)


def set_markup_pct(pct: Decimal, actor_id: str) -> dict:
    """The markup % (0..100, 2 places). Raises ValueError outside the range."""
    value = validate_markup_pct(pct)
    sb = get_supabase()
    row = get_settings_row(sb)
    old = markup_pct_of(row)
    sb.table(SETTINGS_TABLE).update(
        {"markup_pct": str(value), "updated_by": actor_id, "updated_at": _iso(_now())}
    ).eq("id", 1).execute()
    _bookkeep(
        "markup audit", audit, actor_id, "auto_send.markup_changed", SETTINGS_TABLE, None,
        {"id": 1, "from": str(old), "to": str(value)},
    )
    return get_settings_row(sb)


def _names(sb, ids) -> dict[str, str | None]:
    return external_submission._names(sb, ids)


def settings_summary(user, sb=None) -> dict:
    """GET /auto-send/settings."""
    sb = sb or get_supabase()
    row = get_settings_row(sb)
    s = get_settings()
    names = _names(sb, [row.get("updated_by")])
    return {
        "enabled": bool(row.get("enabled")),
        "markup_pct": str(markup_pct_of(row)),
        "enabled_changed_at": row.get("enabled_changed_at"),
        "updated_at": row.get("updated_at"),
        "updated_by_name": names.get(row.get("updated_by")),
        "can_toggle": user.role == Role.IT_ADMIN,
        "can_edit_pct": user.role in AUTO_SEND_PCT_ROLES,
        "send_time": s.auto_send_send_time,
        "preflight_time": s.auto_send_preflight_time,
    }


# ── which projects ─────────────────────────────────────────────────────────


def bid_date_of(project: dict) -> date | None:
    """The Pacific calendar date of projects.internal_bid_at."""
    dt = _parse_ts(project.get("internal_bid_at"))
    return dt.astimezone(TZ).date() if dt else None


def day_bounds(day: date) -> tuple[datetime, datetime]:
    """[start, end) of a Pacific calendar day, in UTC (DST safe: each end is
    its own local midnight, never start + 24h)."""
    start = datetime.combine(day, time.min, tzinfo=TZ).astimezone(timezone.utc)
    end = datetime.combine(day + timedelta(days=1), time.min, tzinfo=TZ).astimezone(timezone.utc)
    return start, end


def due_projects(day: date, sb=None) -> list[dict]:
    """Projects whose internal bid date is `day` (Pacific), not abandoned and
    not in a silent stage."""
    sb = sb or get_supabase()
    start, end = day_bounds(day)
    return _page_all(
        lambda lo, hi: sb.table("projects")
        .select("id, name, number, internal_bid_at, current_stage, abandoned_at, created_at")
        .not_.in_("current_stage", list(SILENT_STAGES))
        .is_("abandoned_at", "null")
        .gte("internal_bid_at", _pg_ts(start))
        .lt("internal_bid_at", _pg_ts(end))
        .order("internal_bid_at")
        .range(lo, hi)
    )


# ── DB reads ───────────────────────────────────────────────────────────────


def _is_bad_uuid(exc: APIError) -> bool:
    """PostgREST's 22P02: a malformed id on a uuid column (a hand-made
    request). It means "no such row", never a 500."""
    return getattr(exc, "code", None) == "22P02"


def _project(sb, project_id: str) -> dict | None:
    try:
        rows = (
            sb.table("projects").select("*").eq("id", project_id).limit(1).execute()
        ).data or []
    except APIError as exc:
        if _is_bad_uuid(exc):
            return None
        raise
    return rows[0] if rows else None


def _human_sent(sb, project_id: str) -> str | None:
    """The live "someone already sent it" check: a proposal_sends row at
    sent/sending (emailed or marked submitted), or an active external
    submission. Returns the silent reason code or None."""
    sends = (
        sb.table("proposal_sends").select("id, gc_id, status").eq("project_id", project_id)
        .in_("status", ["sent", "sending"]).limit(1).execute()
    ).data or []
    if sends:
        return "proposal_sent"
    if external_submission._active_submission(sb, project_id):
        return "external_submission"
    return None


def _human_send_unknown(sb, project_id: str) -> bool:
    """A Send Out email that timed out (proposal_sends 'failed' with the
    outcome-unknown error): Graph may have delivered it, so Send Out itself
    refuses to retry it without force. Auto send must not email on top of it."""
    rows = (
        sb.table("proposal_sends").select("id, status, error").eq("project_id", project_id)
        .eq("status", "failed").execute()
    ).data or []
    return any(
        (r.get("error") or "").startswith(proposal_send.OUTCOME_UNKNOWN_PREFIX) for r in rows
    )


def _switched_off(sb) -> bool:
    """The IT Admin switch, re-read before every email. A failed read counts
    as off (fail closed)."""
    try:
        return not get_settings_row(sb).get("enabled")
    except Exception:  # noqa: BLE001 - see docstring
        logger.exception("auto send settings re-read failed; treating the switch as off")
        return True


def _stop_reason(sb, project_id: str) -> str | None:
    """Why the run must stop before the next GC email, re-checked right before
    each claim and again right before each send_mail: a human sent (or marked
    submitted), a human Send Out timed out, or the switch went off."""
    if _human_sent(sb, project_id):
        return "human_sent"
    if _human_send_unknown(sb, project_id):
        return "human_send_unknown"
    if _switched_off(sb):
        return "switched_off"
    return None


def _runs(sb, project_id: str) -> list[dict]:
    rows = (
        sb.table("auto_send_runs").select("*").eq("project_id", project_id)
        .order("bid_date", desc=True).execute()
    ).data or []
    return sorted(
        rows, key=lambda r: (str(r.get("bid_date") or ""), str(r.get("created_at") or "")),
        reverse=True,
    )


def _run_gcs(sb, run_id: str) -> list[dict]:
    rows = (sb.table("auto_send_gcs").select("*").eq("run_id", run_id).execute()).data or []
    return sorted(rows, key=lambda r: (r.get("gc_name") or "").lower())


def _latest_draft_with_lines(sb, project_id: str) -> dict | None:
    """The newest proposal_drafts row at 'done' with non-empty lines, approved
    or not (owner decision: unapproved lines still go out)."""
    rows = (
        sb.table("proposal_drafts").select("id, status, lines_json, approved_at, created_at")
        .eq("project_id", project_id).eq("status", "done")
        .order("created_at", desc=True).execute()
    ).data or []
    rows = [r for r in rows if r.get("lines_json")]
    if not rows:
        return None
    return max(rows, key=lambda r: str(r.get("created_at") or ""))


def _newest_draft(sb, project_id: str) -> dict | None:
    rows = (
        sb.table("proposal_drafts").select("id, status, lines_json, approved_at, created_at")
        .eq("project_id", project_id).order("created_at", desc=True).limit(1).execute()
    ).data or []
    if not rows:
        return None
    return max(rows, key=lambda r: str(r.get("created_at") or ""))


def _active_estimator_ids(sb, project_id: str) -> set[str]:
    rows = (
        sb.table("estimator_assignments").select("estimator_id, revoked_at")
        .eq("project_id", project_id).is_("revoked_at", "null").execute()
    ).data or []
    return {r["estimator_id"] for r in rows if r.get("estimator_id")}


def _dev_ids(sb, ids: set[str]) -> set[str]:
    ids = {i for i in ids if i}
    if not ids:
        return set()
    rows = (
        sb.table("profiles").select("id, is_dev").in_("id", sorted(ids)).execute()
    ).data or []
    return {r["id"] for r in rows if r.get("is_dev")}


# How far before the workbook's upload an impersonated write still counts
# (the audit row is written just before the upload handler runs).
_IMPERSONATION_SLACK = timedelta(minutes=10)


def _impersonated_estimate(sb, project_id: str, estimate: dict) -> bool:
    """Whether the dev account priced this estimate through "View as"
    (X-Impersonate-Estimator). Impersonated writes land under the real
    estimator's id, so the dev check on uploaded_by can never see them:
      - the workbook row or its sealed round carries impersonated_by (0159);
      - older rows: an audit_log 'impersonation.write' by a dev acting as
        that estimator on this project's paths (the upload or the submit),
        at or after the workbook's upload.
    Read errors propagate: the caller fails the run, it never sends."""
    if estimate.get("impersonated_by"):
        return True
    uploaded_by = estimate.get("uploaded_by")
    round_no = estimate.get("round")
    if round_no is not None:
        rows = (
            sb.table("estimator_submissions").select("id, impersonated_by")
            .eq("project_id", project_id).eq("round", round_no).execute()
        ).data or []
        if any(r.get("impersonated_by") for r in rows):
            return True
    if not uploaded_by:
        return False
    since = _parse_ts(estimate.get("uploaded_at"))
    query = (
        sb.table("audit_log").select("id, payload, created_at")
        .eq("action", "impersonation.write").eq("entity_id", uploaded_by)
    )
    if since is not None:
        # audit_log_action_idx (action, created_at) keeps this a range read.
        query = query.gte("created_at", _iso(since - _IMPERSONATION_SLACK))
    rows = (query.execute()).data or []
    marker = f"/projects/{project_id}/"
    for r in rows:
        path = str((r.get("payload") or {}).get("path") or "")
        if marker not in path:
            continue
        at = _parse_ts(r.get("created_at"))
        if since is None or at is None or at >= since - _IMPERSONATION_SLACK:
            return True
    return False


# ── recipients ─────────────────────────────────────────────────────────────


def gc_recipients(live_gc: dict, bid_contact_ids: list[str]) -> tuple[list[str], str]:
    """Pure: (To list, source) for one GC. The project's bid contacts (0110),
    kept only while they are still live contacts of this GC with an email;
    otherwise every contact of the GC with an email. Sorted, de-duplicated."""
    usable = {c["id"] for c in (live_gc.get("contacts") or []) if c.get("email")}
    chosen = [cid for cid in bid_contact_ids if cid in usable]
    if chosen:
        return proposal_send.resolve_recipients(live_gc, chosen), "bid_contacts"
    return proposal_send.resolve_recipients(live_gc, None), "all_contacts"


def _redirect_address() -> str:
    return (get_settings().auto_send_gc_redirect or "").strip()


def email_envelope(project: dict, recipients: list[str]) -> tuple[list[str], list[str], str]:
    """(to, cc, subject) for one GC email. In production: the GC's recipients,
    the bids desk CC and the auto subject. Outside production with
    AUTO_SEND_GC_REDIRECT set: ONLY the redirect address, no CC, and the
    original To in the subject. (Outside production without a redirect the
    project never reaches here: evaluate blocks it with gc_emails_blocked.)"""
    subject = auto_subject(project)
    redirect = "" if get_settings().is_production else _redirect_address()
    if redirect:
        return (
            [redirect], [],
            f"[AUTO SEND TEST to {proposal_send.join_recipients(recipients)}] {subject}",
        )
    return recipients, proposal_send.cc_recipients(recipients), subject


# ── evaluate ───────────────────────────────────────────────────────────────


def _silent_reason(sb, project: dict, state: dict, runs: list[dict]) -> str | None:
    if project.get("current_stage") in SILENT_STAGES:
        return "stage"
    if project.get("abandoned_at"):
        return "abandoned"
    human = _human_sent(sb, project["id"])
    if human:
        return human
    head = state.get("send_out", {}).get("current_task")
    if proposal_send.gone_out_rule(head, project.get("reverify_return_stage")):
        return "gone_out"
    if any(r.get("status") in ("sent", "partial") for r in runs):
        return "already_auto_sent"
    return None


def _evaluate(sb, project_id: str) -> tuple[dict, dict]:
    """evaluate() plus the working objects run_send needs (project, live GCs,
    Decimal amounts, lines). Never sends; writes only the estimate cache."""
    project = _project(sb, project_id)
    if not project:
        raise _refuse("not_found", 404)
    settings_row = get_settings_row(sb)
    pct = markup_pct_of(settings_row)
    bid_day = bid_date_of(project)
    result: dict = {
        "project_id": project_id,
        "bid_date": bid_day.isoformat() if bid_day else None,
        "outcome": "ready",
        "reasons": [],
        "silent_reason": None,
        "markup_pct": str(pct),
        "estimate": None,
        "amounts": None,
        "draft": None,
        "gcs": [],
    }
    work: dict = {"project": project, "gcs": [], "amounts": None, "lines": None,
                  "draft_id": None, "all_names": []}

    state, _head = external_submission._state_and_send_out_head(sb, project_id)
    silent = _silent_reason(sb, project, state, _runs(sb, project_id))
    if silent:
        result["outcome"] = "silent"
        result["silent_reason"] = silent
        return result, work

    estimate = load_estimate(project_id, sb=sb)
    result["estimate"] = estimate
    assigned = _active_estimator_ids(sb, project_id)
    if _dev_ids(sb, assigned | {estimate.get("uploaded_by")}) or _impersonated_estimate(
        sb, project_id, estimate
    ):
        result["outcome"] = "skipped"
        result["reasons"] = [_reason("dev_estimator")]
        return result, work

    reasons: list[dict] = []
    if _human_send_unknown(sb, project_id):
        reasons.append(_reason("human_send_unknown"))
    if not workflow.category_past(state, "go_no_go"):
        reasons.append(_reason("not_go"))

    no_workbook = not estimate.get("file_id") and estimate.get("code") in (
        None, "estimate_not_received",
    )
    if not assigned and no_workbook:
        reasons.append(_reason("no_estimator"))
    elif not estimate.get("ok"):
        code = estimate.get("code") or "estimate_not_received"
        reasons.append({"code": code, "message": estimate.get("message") or estimate_message(code)})
    else:
        base_m, base_l = Decimal(str(estimate["materials"])), Decimal(str(estimate["labor"]))
        m, labor, total = apply_markup(base_m, base_l, pct)
        work["amounts"] = {"material": m, "labor": labor, "total": total}
        result["amounts"] = {
            "material": str(m), "labor": str(labor), "total": str(total),
            "base_material": str(base_m), "base_labor": str(base_l),
        }

    draft = _latest_draft_with_lines(sb, project_id)
    if draft:
        work["lines"] = list(draft["lines_json"])
        work["draft_id"] = draft["id"]
        result["draft"] = {"id": draft["id"], "lines": list(draft["lines_json"]),
                           "approved": bool(draft.get("approved_at"))}
    else:
        reasons.append(_reason("no_scope_lines"))

    if not (project.get("address") and project.get("labor_time") and project.get("wage_type")):
        reasons.append(_reason("missing_project_fields"))

    live_gcs = sorted(proposal_send._project_gcs(project_id), key=lambda g: g["name"].lower())
    bid_contacts = proposal_send.selected_contacts_by_gc(project_id)
    work["all_names"] = [g["name"] for g in live_gcs]
    bracketed = any(
        v and ("<" in str(v) or ">" in str(v))
        for v in (project.get("name"), project.get("number"), project.get("address"),
                  *work["all_names"])
    )
    if bracketed:
        reasons.append(_reason("bad_characters"))

    sendable = 0
    for gc in live_gcs:
        recipients, source = gc_recipients(gc, bid_contacts.get(gc["id"], []))
        reason = None if recipients else _reason("gc_no_email", gc=gc["name"])
        if recipients:
            sendable += 1
        result["gcs"].append({
            "gc_id": gc["id"], "gc_name": gc["name"], "recipients": recipients,
            "source": source,
            "reason": {"code": reason["code"], "message": reason["message"]} if reason else None,
        })
        work["gcs"].append({**gc, "recipients": recipients, "source": source,
                            "reason": reason})
    if not live_gcs:
        reasons.append(_reason("no_gcs"))
    elif not sendable:
        reasons.append(_reason("no_gc_emails"))

    if not get_settings().is_production and not _redirect_address():
        reasons.append(_reason("gc_emails_blocked"))

    if reasons:
        result["outcome"] = "blocked"
        result["reasons"] = reasons
    return result, work


def evaluate(project_id: str, *, sb=None) -> dict:
    """What auto send would do for this project right now (section 5)."""
    return _evaluate(sb or get_supabase(), project_id)[0]


# ── rendering ──────────────────────────────────────────────────────────────


def assert_pricing_table_clean(docx_bytes: bytes, total_amount: str) -> None:
    """The auto send document carries materials, labor and total only. The
    stock validators' removed-section check scans the whole body, and scope
    lines may legitimately say "Underground", so it is run here on the
    pricing table alone: the rendered table that carries TOTAL and this
    total. Raises ProposalRenderError."""
    with zipfile.ZipFile(io.BytesIO(docx_bytes)) as zf:
        root = etree.fromstring(zf.read("word/document.xml"))
    tables = [
        "".join(t.text or "" for t in tbl.iter(qn("w:t"))) for tbl in root.iter(qn("w:tbl"))
    ]
    pricing = [t for t in tables if "TOTAL" in t and total_amount in t]
    if not pricing:
        raise proposal_docx.ProposalRenderError(
            "Pricing table not found in the rendered proposal."
        )
    for text in pricing:
        for label in proposal_docx.SECTION_LABELS.values():
            if label in text:
                raise proposal_docx.ProposalRenderError(
                    f"Section row {label!r} is in the pricing table of an auto send proposal."
                )


def _figures(amounts: dict) -> tuple[str, ...]:
    return proposal_send.stamp_figures(
        {"material": amounts["material"], "gear": None, "underground": None,
         "low_voltage": None, "labor": amounts["labor"]}
    )


def render_for_gc(base_ctx, gc_name: str, other_names: list[str], amounts: dict,
                  template: bytes) -> bytes:
    """One GC's validated docx (raises ProposalRenderError)."""
    ctx = replace(base_ctx, gc_name=gc_name)
    docx = proposal_docx.render_proposal(template, ctx)
    proposal_docx.validate_output(
        docx,
        gc_name=gc_name,
        scope_lines=ctx.scope_lines,
        other_gc_names=tuple(n for n in other_names if n != gc_name),
        amounts=_figures(amounts),
        includes_generator=False,
        removed_sections=(),
    )
    assert_pricing_table_clean(docx, ctx.total_amount)
    return docx


def _queue_preview(file_id: str) -> None:
    """Best-effort PDF preview of a stored auto send document (no request
    BackgroundTasks here: the poller thread and the route both call this)."""
    threading.Thread(target=office_preview.generate_preview, args=(file_id,), daemon=True).start()


def _store_document(sb, project_id: str, gc_id: str, filename: str, docx: bytes) -> dict:
    path = storage.build_object_path(project_id, "proposal", filename)
    storage.upload_file(path, docx, proposal_docx.DOCX_MIME)
    row = (
        sb.table("project_files").insert({
            "project_id": project_id,
            "category": "proposal",
            "storage_path": path,
            "filename": filename,
            "gc_id": gc_id,
            "uploaded_by": None,
            "mime_type": proposal_docx.DOCX_MIME,
            "size_bytes": len(docx),
            "preview_status": "pending",
        }).execute()
    ).data[0]
    if office_preview.is_convertible(filename, "proposal"):
        _bookkeep("preview", _queue_preview, row["id"])
    return row


# ── the send ───────────────────────────────────────────────────────────────


def sendable_gc_ids(gc_rows: list[dict], project_gc_ids, *, force_unknown: bool) -> set[str]:
    """Pure: the project GCs a (re)send may attempt. Never a GC already
    'sent'; a GC at 'unknown' (timed out, may have gone out) only with
    force_unknown; a GC at 'sending' never (another worker holds it)."""
    by_gc = {r["gc_id"]: r for r in gc_rows}
    out: set[str] = set()
    for gid in project_gc_ids:
        status = (by_gc.get(gid) or {}).get("status")
        if status is None or status in _GC_RETRY_STATUSES:
            out.add(gid)
        elif status == "unknown" and force_unknown:
            out.add(gid)
    return out


def final_status(gc_rows: list[dict]) -> str:
    """Pure: the run status from every GC row of the run."""
    sent = sum(1 for r in gc_rows if r.get("status") == "sent")
    if not sent:
        return "failed"
    return "sent" if sent == len(gc_rows) else "partial"


# The reason code of a GC row from an earlier pass, where only its status
# and message survive (auto_send_gcs keeps the message, not the code).
_CODE_OF_STATUS = {"unknown": "outcome_unknown", "skipped": "human_sent", "failed": "send_failed"}


def _gc_reasons(gc_rows: list[dict], codes: dict[str, str] | None = None) -> list[dict]:
    """Pure: the run's per-GC reasons, one per GC row that did not go out."""
    out = []
    for r in gc_rows:
        if r.get("status") in ("sent", "pending") or not r.get("error"):
            continue
        code = (codes or {}).get(r["gc_id"]) or _CODE_OF_STATUS.get(r.get("status"), "send_failed")
        out.append({"code": code, "message": r["error"], "gc_id": r["gc_id"]})
    return out


def _update_gc(sb, row_id: str, fields: dict) -> dict | None:
    rows = (sb.table("auto_send_gcs").update(fields).eq("id", row_id).execute()).data or []
    return rows[0] if rows else None


def _claim_run(sb, project_id: str, bid_date: date, trigger: str,
               actor_id: str | None) -> dict | None:
    now = _iso(_now())
    if trigger == "scheduled":
        try:
            rows = (
                sb.table("auto_send_runs").insert({
                    "project_id": project_id,
                    "bid_date": bid_date.isoformat(),
                    "status": "sending",
                    "trigger": "scheduled",
                    "started_at": now,
                }).execute()
            ).data or []
        except APIError as exc:
            if getattr(exc, "code", None) == "23505":
                return None  # the other worker has it
            raise
        return rows[0] if rows else None
    existing = (
        sb.table("auto_send_runs").select("*").eq("project_id", project_id)
        .eq("bid_date", bid_date.isoformat()).limit(1).execute()
    ).data or []
    if not existing or existing[0].get("status") not in RUN_SENDABLE_STATUSES:
        if existing and existing[0].get("status") == "sending":
            raise _refuse("busy")
        raise _refuse("nothing_to_send")
    run = existing[0]
    if run.get("accepted_at"):
        raise _refuse("already_accepted")
    rows = (
        sb.table("auto_send_runs").update({
            "status": "sending", "trigger": "manual", "triggered_by": actor_id,
            "started_at": now, "finished_at": None, "updated_at": now,
        }).eq("id", run["id"]).in_("status", list(RUN_SENDABLE_STATUSES))
        # An acceptance landing between the read and this write wins.
        .is_("accepted_at", "null").execute()
    ).data or []
    if not rows:
        raise _refuse("busy")
    return rows[0]


def _beat(sb, run: dict) -> bool:
    """The run's heartbeat: a compare-and-set that bumps updated_at while the
    run is still at 'sending'. reclaim_stuck measures staleness from it, so a
    live run is never reclaimed; False means this worker lost the run (it was
    reclaimed), and it must send nothing more."""
    rows = (
        sb.table("auto_send_runs").update({"status": "sending", "updated_at": _iso(_now())})
        .eq("id", run["id"]).eq("status", "sending").execute()
    ).data or []
    return bool(rows)


def _stuck_minutes() -> float:
    """STUCK_MINUTES, raised if the converter timeout grows: at least twice
    the longest single step between heartbeats."""
    convert = float(getattr(get_settings(), "preview_convert_timeout_seconds", 120) or 120)
    step_minutes = (2 * convert + 2 + _GRAPH_SEND_SECONDS + 1) / 60
    return max(float(STUCK_MINUTES), 2 * step_minutes)


def _sending_to_unknown(sb, gc_rows: list[dict]) -> bool:
    """A GC row left at 'sending' by a crash may have gone out: it becomes
    'unknown' (never retried without force_unknown). Updates `gc_rows` in
    place; True when any row moved."""
    moved = False
    for r in gc_rows:
        if r.get("status") != "sending":
            continue
        moved = True
        fields = {"status": "unknown",
                  "error": _reason("outcome_unknown", gc=r.get("gc_name") or "the GC")["message"]}
        r.update(_update_gc(sb, r["id"], fields) or fields)
    return moved


def _finish(sb, run: dict, status: str, reasons: list[dict], extra: dict | None = None) -> dict:
    fields = {"status": status, "reasons": reasons, "finished_at": _iso(_now()), **(extra or {})}
    rows = (sb.table("auto_send_runs").update(fields).eq("id", run["id"]).execute()).data or []
    return rows[0] if rows else {**run, **fields}


def run_send(project_id: str, bid_date: date, *, trigger: str = "scheduled",
             actor_id: str | None = None, force_unknown: bool = False, sb=None) -> dict | None:
    """Claim the (project, bid date) run and email every GC (section 5).

    scheduled: inserts the claim; a unique violation means the other worker
    has it, so None. manual (Send now): compare-and-set an existing missed /
    failed / partial run back to 'sending' (409 otherwise); only GCs not
    already sent are attempted, 'unknown' ones only with force_unknown.
    Returns the finished run row (with "gcs")."""
    sb = sb or get_supabase()
    run = _claim_run(sb, project_id, bid_date, trigger, actor_id)
    if run is None:
        return None
    try:
        return _send_claimed(sb, run, project_id, trigger=trigger, actor_id=actor_id,
                             force_unknown=force_unknown)
    except Exception as exc:
        # Never strand the claim at 'sending': record what is known. A GC row
        # left at 'sending' may have gone out, so it becomes 'unknown'.
        logger.exception("auto send failed for project %s", project_id)
        gc_rows = _run_gcs(sb, run["id"])
        _sending_to_unknown(sb, gc_rows)
        detail = str(exc)[:300]
        reasons = [{"code": "interrupted", "message": f"{REASON_MESSAGES['interrupted']} "
                    f"({detail})"}, *_gc_reasons(gc_rows)]
        finished = _finish(sb, run, final_status(gc_rows) if gc_rows else "failed", reasons)
        # The same bell (and, for Send now or after the digest, the email) as
        # a run that finished normally: nobody else would learn that some GCs
        # got the proposal and others did not. _after_run writes the audit.
        _bookkeep("failure bell", _after_run, sb, _project_or_stub(sb, project_id), finished,
                  gc_rows, trigger, actor_id, sent_now=_sent_this_pass(run, gc_rows))
        return {**finished, "gcs": gc_rows}


def _project_or_stub(sb, project_id: str) -> dict:
    """The project for a notification on a failure path, where the database
    may be what failed: {"id"} when it cannot be read."""
    try:
        rows = (
            sb.table("projects").select("id, name, number").eq("id", project_id)
            .limit(1).execute()
        ).data or []
    except Exception:  # noqa: BLE001 - see docstring
        logger.exception("auto send project read failed for %s", project_id)
        rows = []
    return rows[0] if rows else {"id": project_id}


def _sent_this_pass(run: dict, gc_rows: list[dict]) -> int:
    """GC rows of `run` sent since its claim (started_at), for a pass that did
    not finish normally."""
    started = _parse_ts(run.get("started_at"))
    out = 0
    for r in gc_rows:
        if r.get("status") != "sent":
            continue
        at = _parse_ts(r.get("sent_at"))
        if started is None or at is None or at >= started:
            out += 1
    return out


def _send_claimed(sb, run: dict, project_id: str, *, trigger: str, actor_id: str | None,
                  force_unknown: bool) -> dict:
    result, work = _evaluate(sb, project_id)
    prior_rows = _run_gcs(sb, run["id"])
    project = work["project"]

    if result["outcome"] != "ready":
        if result["outcome"] == "silent":
            reasons = [{"code": result["silent_reason"],
                        "message": SILENT_MESSAGES.get(result["silent_reason"], "")}]
        else:
            reasons = result["reasons"]
        if any(r.get("status") == "sent" for r in prior_rows):
            # A partly sent run stays partial and keeps what it sent.
            status, extra = final_status(prior_rows), None
        else:
            status = "failed" if result["outcome"] == "blocked" else "skipped"
            extra = {"estimate": result["estimate"], "markup_pct": result["markup_pct"]}
        finished = _finish(sb, run, status, [*reasons, *_gc_reasons(prior_rows)], extra)
        _after_run(sb, project, finished, prior_rows, trigger, actor_id, sent_now=0)
        return {**finished, "gcs": prior_rows}

    # A partly sent run keeps the numbers and lines it already sent: one bid,
    # the same to every GC, even if the estimate changed in between.
    frozen = any(r.get("status") == "sent" for r in prior_rows) and run.get("lines_json")
    if frozen:
        amounts = {"material": _dec(run["material_amount"]), "labor": _dec(run["labor_amount"]),
                   "total": _dec(run["total_amount"])}
        lines = list(run["lines_json"])
        run_fields: dict = {}
    else:
        amounts = work["amounts"]
        lines = work["lines"]
        run_fields = {
            "markup_pct": result["markup_pct"],
            "estimate": result["estimate"],
            "material_amount": str(amounts["material"]),
            "labor_amount": str(amounts["labor"]),
            "total_amount": str(amounts["total"]),
            "draft_id": work["draft_id"],
            "lines_json": lines,
            "lines_hash": proposal_send.lines_hash(lines),
        }
    redirect = "" if get_settings().is_production else _redirect_address()
    run_fields["redirected_to"] = redirect or None
    sb.table("auto_send_runs").update(run_fields).eq("id", run["id"]).execute()
    run = {**run, **run_fields}

    # GC rows: one per project GC. A GC added since a partial run gets a row.
    # A GC whose email in an EARLIER run (another bid date) timed out or was
    # cut off mid-send may already hold a proposal: its new row starts at
    # 'unknown', so only a person (Send now with force_unknown) can send it
    # again. Graph timeouts are never retried automatically.
    by_gc = {r["gc_id"]: r for r in prior_rows}
    carried = _earlier_unknown_gc_ids(sb, project_id, run["id"])
    new_rows = []
    for gc in work["gcs"]:
        if gc["id"] in by_gc:
            continue
        row = {
            "run_id": run["id"], "project_id": project_id, "gc_id": gc["id"],
            "gc_name": gc["name"], "status": "pending",
            "recipients": proposal_send.join_recipients(gc["recipients"]) or None,
        }
        if gc["id"] in carried:
            row["status"] = "unknown"
            row["error"] = _reason("earlier_outcome_unknown", gc=gc["name"])["message"]
        new_rows.append(row)
    if new_rows:
        for r in (sb.table("auto_send_gcs").insert(new_rows).execute()).data or []:
            by_gc[r["gc_id"]] = r
    targets = sendable_gc_ids(list(by_gc.values()), [g["id"] for g in work["gcs"]],
                              force_unknown=force_unknown)
    if trigger == "manual":
        # Send now only retries GCs it can reach; a GC with no email keeps its
        # earlier failure instead of failing (and alerting) again.
        targets &= {g["id"] for g in work["gcs"] if g["recipients"]}

    base_ctx = proposal_send.build_base_context(
        project, {"lines_json": lines},
        {"material": amounts["material"], "gear": None, "underground": None,
         "low_voltage": None, "labor": amounts["labor"], "total": amounts["total"]},
        includes_generator=False,
    )
    template = proposal_docx.load_template_bytes()
    _, cover_body = proposal_send.build_cover_email(project)

    # Render and store every target first: a document that cannot be built
    # fails only its own GC, before any email goes out.
    prepared: list[tuple[dict, dict, bytes, dict]] = []
    codes: dict[str, str] = {}  # gc_id -> this pass's reason code
    for gc in work["gcs"]:
        if gc["id"] in carried and by_gc[gc["id"]].get("status") == "unknown":
            codes.setdefault(gc["id"], "earlier_outcome_unknown")
        if gc["id"] not in targets:
            continue
        if not _beat(sb, run):
            return _lost(sb, run, project_id, list(by_gc.values()))
        row = by_gc[gc["id"]]
        base = {"recipients": proposal_send.join_recipients(gc["recipients"]) or None,
                "gc_name": gc["name"], "material_amount": str(amounts["material"]),
                "labor_amount": str(amounts["labor"]), "total_amount": str(amounts["total"])}
        if gc["reason"]:
            codes[gc["id"]] = gc["reason"]["code"]
            by_gc[gc["id"]] = _update_gc(sb, row["id"], {
                **base, "status": "failed", "error": gc["reason"]["message"]}) or row
            continue
        try:
            filename = proposal_docx.build_filename(project["number"], gc["name"])
            docx = render_for_gc(base_ctx, gc["name"], work["all_names"], amounts, template)
            file_row = _store_document(sb, project_id, gc["id"], filename, docx)
        except Exception as exc:  # noqa: BLE001 - isolate failures per GC
            logger.exception("auto send render failed for gc %s", gc["id"])
            codes[gc["id"]] = "render_failed"
            by_gc[gc["id"]] = _update_gc(sb, row["id"], {
                **base, "status": "failed",
                "error": _reason("render_failed", gc=gc["name"], detail=str(exc)[:300])["message"],
            }) or row
            continue
        row = _update_gc(sb, row["id"], {**base, "file_id": file_row["id"]}) or row
        by_gc[gc["id"]] = row
        prepared.append((gc, row, docx, file_row))

    sent_now = 0
    stopped: str | None = None  # the stop reason code, once the run stops
    first_sent_at = run.get("sent_at")
    for i, (gc, row, docx, file_row) in enumerate(prepared):
        if not _beat(sb, run):
            return _lost(sb, run, project_id, list(by_gc.values()))
        stopped = stopped or _stop_reason(sb, project_id)
        if stopped:
            # A person sent (or marked submitted, or a Send Out timed out)
            # while this run was going, or the switch went off: nothing more
            # goes out from this run.
            codes[gc["id"]] = stopped
            by_gc[gc["id"]] = _update_gc(sb, row["id"], {
                "status": "skipped", "error": REASON_MESSAGES[stopped]}) or row
            continue
        to, cc, subject = email_envelope(project, gc["recipients"])
        allowed = [*_GC_RETRY_STATUSES, *(["unknown"] if force_unknown else [])]
        claimed = (
            sb.table("auto_send_gcs").update({
                "status": "sending", "attempts": int(row.get("attempts") or 0) + 1,
                "cc": cc, "error": None,
            }).eq("id", row["id"]).in_("status", allowed).execute()
        ).data or []
        if not claimed:
            continue
        row = claimed[0]
        status, code, fields = _send_one(sb, project, gc, docx, file_row, work["all_names"],
                                         amounts, to=to, cc=cc, subject=subject,
                                         body=cover_body, actor_id=actor_id)
        if code:
            codes[gc["id"]] = code
        if code in _STOP_CODES:
            stopped = code
        by_gc[gc["id"]] = _update_gc(sb, row["id"], {"status": status, **fields}) or row
        if status == "sent":
            sent_now += 1
            first_sent_at = first_sent_at or fields["sent_at"]
        if i < len(prepared) - 1:
            _pause()

    # A GC taken off the project since an earlier pass no longer counts
    # against the run unless it was sent.
    live_ids = {g["id"] for g in work["gcs"]}
    gc_rows = sorted(
        (r for r in by_gc.values() if r["gc_id"] in live_ids or r.get("status") == "sent"),
        key=lambda r: (r.get("gc_name") or "").lower(),
    )
    reasons = [_reason(stopped)] if stopped else []
    reasons.extend(_gc_reasons(gc_rows, codes))
    finished = _finish(sb, run, final_status(gc_rows), reasons,
                       {"sent_at": first_sent_at} if first_sent_at else None)
    _after_run(sb, project, finished, gc_rows, trigger, actor_id, sent_now=sent_now)
    return {**finished, "gcs": gc_rows}


# _stop_reason codes: the GC is skipped and the rest of the run stops.
_STOP_CODES = ("human_sent", "human_send_unknown", "switched_off")


def _earlier_unknown_gc_ids(sb, project_id: str, run_id: str) -> set[str]:
    """GCs whose auto send email in another run of this project timed out or
    was cut off mid-send ('unknown' / 'sending') and was never sent since."""
    rows = (
        sb.table("auto_send_gcs").select("id, run_id, gc_id, status")
        .eq("project_id", project_id).execute()
    ).data or []
    sent = {r["gc_id"] for r in rows if r.get("status") == "sent"}
    return {
        r["gc_id"] for r in rows
        if r.get("run_id") != run_id and r.get("status") in ("unknown", "sending")
        and r["gc_id"] not in sent
    }


def _lost(sb, run: dict, project_id: str, gc_rows: list[dict]) -> dict:
    """This worker's run was reclaimed (or re-claimed) elsewhere: stop at once,
    send nothing more and leave the run record to whoever holds it now."""
    logger.warning("auto send run %s was reclaimed mid-send; this worker stops", run["id"])
    _bookkeep("lost audit", audit, None, "auto_send.run_lost", "auto_send_run", run["id"],
              {"project_id": project_id})
    rows = (sb.table("auto_send_runs").select("*").eq("id", run["id"]).limit(1)
            .execute()).data or [run]
    return {**rows[0], "gcs": gc_rows}


def _send_one(sb, project: dict, gc: dict, docx: bytes, file_row: dict, all_names: list[str],
              amounts: dict, *, to: list[str], cc: list[str], subject: str, body: str,
              actor_id: str | None) -> tuple[str, str | None, dict]:
    """Convert, re-validate and email one GC. Returns (status, reason code,
    row fields). The human-send / switch check runs again right before
    send_mail: the conversion can take minutes, and a Send Out that starts in
    that window must win (docs/AUTO_SEND.md section 2)."""
    others = tuple(n for n in all_names if n != gc["name"])
    try:
        pdf = office_preview.convert_for_send(docx, file_row["filename"])
    except office_preview.ConversionError:
        return "failed", "convert_failed", {"error": REASON_MESSAGES["convert_failed"]}
    if len(pdf) >= graph_email._INLINE_ATTACHMENT_LIMIT:
        return "failed", "pdf_too_large", {
            "error": _reason("pdf_too_large", gc=gc["name"])["message"]}
    try:
        proposal_docx.validate_pdf_isolation(
            office_preview.extract_pdf_text(pdf),
            gc_name=gc["name"],
            other_gc_names=others,
            amounts=_figures(amounts),
            includes_generator=False,
            # Scope lines may say "Underground": the section-row check runs
            # on the docx pricing table instead (assert_pricing_table_clean).
            removed_sections=(),
        )
    except Exception as exc:  # noqa: BLE001
        return "failed", "render_failed", {"error": _reason(
            "render_failed", gc=gc["name"], detail=str(exc)[:300])["message"]}
    stop = _stop_reason(sb, project["id"])
    if stop:
        return "skipped", stop, {"error": REASON_MESSAGES[stop]}
    try:
        log = graph_email.send_mail(
            to=to,
            cc=cc,
            subject=subject,
            body_html=email_branding.render_proposal_email(
                body.replace(proposal_send.GC_NAME_TOKEN, gc["name"])
            ),
            attachments=[(office_preview.pdf_filename(file_row["filename"]), pdf)],
            inline_images=[(
                email_branding.LOGO_CONTENT_ID,
                email_branding.LOGO_FILENAME,
                email_branding.logo_bytes(),
                "image/jpeg",
            )],
            project_id=project["id"],
            sent_by=actor_id,
        )
    except proposal_send._OUTCOME_UNKNOWN_EXC:
        # Graph may have accepted it: never retried automatically.
        return "unknown", "outcome_unknown", {
            "error": _reason("outcome_unknown", gc=gc["name"])["message"]}
    except Exception as exc:  # noqa: BLE001 - isolate failures per GC
        logger.exception("auto send email failed for gc %s", gc["id"])
        return "failed", "send_failed", {"error": _reason(
            "send_failed", gc=gc["name"], detail=str(exc)[:300])["message"]}
    return "sent", None, {"sent_at": _iso(_now()), "email_log_id": (log or {}).get("id"),
                          "error": None}


def _digest_already_out(sb, run: dict) -> bool:
    """Whether the day's digest already went out (any claim row for the run's
    bid date): a scheduled run finishing after it would otherwise reach no
    email at all. A failed read answers True (tell people)."""
    try:
        return bool(
            (sb.table("auto_send_email_log").select("id")
             .eq("send_date", str(run.get("bid_date") or "")[:10]).eq("kind", "digest")
             .limit(1).execute()).data
        )
    except Exception:  # noqa: BLE001 - see docstring
        logger.exception("auto send digest claim read failed for run %s", run.get("id"))
        return True


def _after_run(sb, project: dict, run: dict, gc_rows: list[dict], trigger: str,
               actor_id: str | None, *, sent_now: int) -> None:
    """Bell rows (one insert) and the audit row. A manual run mirrors the bell
    to email; a scheduled run only once that day's digest has already gone out
    (before it, the digest is the email)."""
    status = run.get("status")
    tag = _project_tag(project)
    payload = {"project_id": project["id"], "status": status, "trigger": trigger,
               "sent_now": sent_now, "reasons": run.get("reasons") or []}
    if status == "skipped":
        _bookkeep("skip audit", audit, actor_id, "auto_send.skipped", "auto_send_run",
                  run["id"], payload)
        return
    sent_total = sum(1 for r in gc_rows if r.get("status") == "sent")
    if sent_now:
        msg = (f"Auto send emailed the proposal for {tag} to {sent_now} "
               f"GC{'s' if sent_now != 1 else ''}.")
        if status == "partial":
            msg += " Some GCs could not be sent. Open Auto send for details."
        type_, action = "auto_send.sent", "auto_send.sent"
    else:
        first = (run.get("reasons") or [{}])[0].get("message") or "Open Auto send for details."
        msg = f"Auto send could not send the proposal for {tag}. {first}"
        type_, action = "auto_send.failed", "auto_send.send_failed"
    mirror = trigger == "manual" or _digest_already_out(sb, run)
    _bookkeep("bell", notify_roles, AUTO_SEND_NOTIFY_ROLES, project["id"], type_, msg,
              mirror_email=mirror, metadata={"run_id": run["id"]})
    _bookkeep("audit", audit, actor_id, action, "auto_send_run", run["id"],
              {**payload, "sent_total": sent_total})


def record_missed(project_id: str, bid_date: date, reasons: list[dict] | None = None,
                  sb=None) -> dict | None:
    """Record a run the server missed (status 'missed'). None when a run for
    that (project, bid date) already exists. The caller notifies."""
    sb = sb or get_supabase()
    now = _iso(_now())
    try:
        rows = (
            sb.table("auto_send_runs").insert({
                "project_id": project_id,
                "bid_date": bid_date.isoformat(),
                "status": "missed",
                "trigger": "scheduled",
                "reasons": reasons or [_reason("missed")],
                "started_at": now,
                "finished_at": now,
            }).execute()
        ).data or []
    except APIError as exc:
        if getattr(exc, "code", None) == "23505":
            return None
        raise
    return rows[0] if rows else None


def _last_activity(run: dict, gc_rows: list[dict]) -> datetime | None:
    """The newest of the run's claim time, its heartbeat (updated_at) and its
    GC rows' updated_at."""
    stamps = [_parse_ts(run.get("started_at") or run.get("created_at")),
              _parse_ts(run.get("updated_at")),
              *(_parse_ts(r.get("updated_at")) for r in gc_rows)]
    stamps = [s for s in stamps if s is not None]
    return max(stamps) if stamps else None


def reclaim_stuck(sb=None) -> int:
    """Runs still 'sending' with no heartbeat for _stuck_minutes() (a crashed
    worker): their GC rows at 'sending' become 'unknown' (they may have gone
    out), the run becomes partial / failed with reason outcome_unknown
    (interrupted when no GC was mid-send), and the usual result bell goes
    out. A live run beats before every GC step, so it is never reclaimed.
    Returns how many."""
    sb = sb or get_supabase()
    cutoff = _now() - timedelta(minutes=_stuck_minutes())
    rows = (
        sb.table("auto_send_runs").select("*").eq("status", "sending").execute()
    ).data or []
    count = 0
    for run in rows:
        started = _parse_ts(run.get("started_at") or run.get("created_at"))
        if started and started > cutoff:
            continue
        gc_rows = _run_gcs(sb, run["id"])
        last = _last_activity(run, gc_rows)
        if last and last > cutoff:
            continue  # still beating: a slow run, not a crashed one
        mid_send = any(r.get("status") == "sending" for r in gc_rows)
        # What the rows will say once the mid-send ones are 'unknown'; a row
        # at 'sending' is not 'sent', so the run status is the same either way.
        projected = [
            {**r, "status": "unknown",
             "error": _reason("outcome_unknown", gc=r.get("gc_name") or "the GC")["message"]}
            if r.get("status") == "sending" else r
            for r in gc_rows
        ]
        status = final_status(gc_rows) if gc_rows else "failed"
        lead = (
            {"code": "outcome_unknown", "message": REASON_MESSAGES["interrupted"]}
            if mid_send else _reason("interrupted")
        )
        # The run's compare-and-set comes first so two workers never both
        # reclaim it.
        cas = (
            sb.table("auto_send_runs").update({
                "status": status, "reasons": [lead, *_gc_reasons(projected)],
                "finished_at": _iso(_now()),
            }).eq("id", run["id"]).eq("status", "sending")
        )
        if run.get("updated_at"):
            # A heartbeat landing between the read and this write wins.
            cas = cas.eq("updated_at", run["updated_at"])
        done = (cas.execute()).data or []
        if not done:
            continue
        count += 1
        _sending_to_unknown(sb, gc_rows)
        _bookkeep("reclaim audit", audit, None, "auto_send.reclaimed", "auto_send_run",
                  run["id"], {"project_id": run["project_id"], "status": status})
        _bookkeep("reclaim bell", _after_run, sb, _project_or_stub(sb, run["project_id"]),
                  done[0], gc_rows, run.get("trigger") or "scheduled", run.get("triggered_by"),
                  sent_now=_sent_this_pass(run, gc_rows))
    return count


# ── red pulse + Revised Proposal ───────────────────────────────────────────


def _chunks(ids: list[str], size: int = 150):
    for i in range(0, len(ids), size):
        yield ids[i : i + size]


def pending_project_ids(project_ids: list[str], sb=None) -> set[str]:
    """Projects with an auto send that went out (sent / partial), not
    accepted, and no real proposal sent since: the red pulse."""
    ids = sorted({p for p in project_ids if p})
    if not ids:
        return set()
    sb = sb or get_supabase()
    candidates: set[str] = set()
    for chunk in _chunks(ids):
        rows = (
            sb.table("auto_send_runs").select("project_id, status, accepted_at")
            .in_("project_id", chunk).in_("status", ["sent", "partial"]).execute()
        ).data or []
        candidates |= {r["project_id"] for r in rows if not r.get("accepted_at")}
    if not candidates:
        return set()
    sent: set[str] = set()
    for chunk in _chunks(sorted(candidates)):
        rows = (
            sb.table("proposal_sends").select("project_id, status")
            .in_("project_id", chunk).eq("status", "sent").execute()
        ).data or []
        sent |= {r["project_id"] for r in rows}
    return candidates - sent


def auto_sent_gc_ids(project_id: str, sb=None) -> set[str]:
    """GCs that received an auto send on this project (Send Out then says
    "Revised Proposal" to them)."""
    sb = sb or get_supabase()
    rows = (
        sb.table("auto_send_gcs").select("gc_id, status").eq("project_id", project_id)
        .eq("status", "sent").execute()
    ).data or []
    return {r["gc_id"] for r in rows}


# ── the panel ──────────────────────────────────────────────────────────────


def _role_of(user) -> Role | None:
    try:
        return Role(user.role)
    except ValueError:
        return None


def _run_wire(sb, run: dict, gc_rows: list[dict]) -> dict:
    names = _names(sb, [run.get("accepted_by"), run.get("triggered_by")])
    file_ids = sorted({r["file_id"] for r in gc_rows if r.get("file_id")})
    filenames: dict[str, str] = {}
    if file_ids:
        files = (
            sb.table("project_files").select("id, filename").in_("id", file_ids).execute()
        ).data or []
        filenames = {f["id"]: f.get("filename") for f in files}
    return {
        "id": run["id"],
        "bid_date": run.get("bid_date"),
        "status": run.get("status"),
        "trigger": run.get("trigger"),
        "reasons": run.get("reasons") or [],
        "markup_pct": run.get("markup_pct"),
        "estimate": run.get("estimate"),
        "material_amount": run.get("material_amount"),
        "labor_amount": run.get("labor_amount"),
        "total_amount": run.get("total_amount"),
        "lines": run.get("lines_json") or [],
        "lines_hash": run.get("lines_hash"),
        "redirected_to": run.get("redirected_to"),
        "started_at": run.get("started_at"),
        "finished_at": run.get("finished_at"),
        "sent_at": run.get("sent_at"),
        "accepted_at": run.get("accepted_at"),
        "accepted_by_name": names.get(run.get("accepted_by")),
        "triggered_by_name": names.get(run.get("triggered_by")),
        "gcs": [
            {
                "gc_id": r["gc_id"],
                "gc_name": r.get("gc_name"),
                "status": r.get("status"),
                "error": r.get("error"),
                "recipients": r.get("recipients"),
                "cc": r.get("cc") or [],
                "file_id": r.get("file_id"),
                "filename": filenames.get(r.get("file_id")),
                "email_log_id": r.get("email_log_id"),
                "material_amount": r.get("material_amount"),
                "labor_amount": r.get("labor_amount"),
                "total_amount": r.get("total_amount"),
                "sent_at": r.get("sent_at"),
            }
            for r in gc_rows
        ],
    }


def _reachable_gc_ids(project_id: str) -> list[str]:
    """Project GCs a send could reach right now: at least one live contact
    with an email (bid contacts first, like evaluate). A GC that failed only
    for having no email is not "left to send" until someone adds one."""
    bid_contacts = proposal_send.selected_contacts_by_gc(project_id)
    return [
        gc["id"] for gc in proposal_send._project_gcs(project_id)
        if gc_recipients(gc, bid_contacts.get(gc["id"], []))[0]
    ]


def _has_sent_proposal(sb, project_id: str) -> bool:
    return bool(
        (sb.table("proposal_sends").select("id").eq("project_id", project_id)
         .eq("status", "sent").limit(1).execute()).data
    )


def project_summary(project_id: str, user) -> dict:
    """GET /projects/{id}/auto-send (section 8)."""
    sb = get_supabase()
    project = _project(sb, project_id)
    if not project:
        raise _refuse("not_found", 404)
    settings_row = get_settings_row(sb)
    enabled = bool(settings_row.get("enabled"))
    runs = _runs(sb, project_id)
    latest = runs[0] if runs else None
    gc_rows = _run_gcs(sb, latest["id"]) if latest else []
    has_sent = _has_sent_proposal(sb, project_id)
    went_out = any(r.get("status") in ("sent", "partial") for r in runs)
    pending = bool(
        latest and latest.get("status") in ("sent", "partial")
        and not latest.get("accepted_at") and not has_sent
    )
    acts = _role_of(user) in AUTO_SEND_ACTION_ROLES

    can_send_now = False
    unknown_gcs = 0
    if latest and acts and enabled and latest.get("status") in RUN_SENDABLE_STATUSES:
        project_gcs = _reachable_gc_ids(project_id)
        # The button shows while any GC is left (timed out ones included:
        # the panel then confirms with force_unknown).
        can_send_now = bool(sendable_gc_ids(gc_rows, project_gcs, force_unknown=True)) and (
            not has_sent and not latest.get("accepted_at") and not _human_sent(sb, project_id)
            and not _human_send_unknown(sb, project_id)
        )
        unknown_gcs = sum(1 for r in gc_rows if r.get("status") == "unknown")
    can_accept = bool(
        acts and latest and latest.get("status") in ("sent", "partial")
        and not latest.get("accepted_at") and not has_sent
    )

    preview = None
    if not went_out:
        try:
            preview = evaluate(project_id, sb=sb)
        except Exception:  # noqa: BLE001 - the panel still shows the run
            logger.exception("auto send preview failed for project %s", project_id)

    newest = _newest_draft(sb, project_id)
    lines_review = (
        {"draft_id": newest["id"], "status": newest.get("status"),
         "approved": bool(newest.get("approved_at")),
         "lines_count": len(newest.get("lines_json") or [])}
        if newest else None
    )
    bid_day = bid_date_of(project)
    return {
        "enabled": enabled,
        "markup_pct": str(markup_pct_of(settings_row)),
        "send_time": get_settings().auto_send_send_time,
        "bid_date": bid_day.isoformat() if bid_day else None,
        "pending": pending,
        "latest_run": _run_wire(sb, latest, gc_rows) if latest else None,
        "preview": preview,
        "can_send_now": can_send_now,
        "can_accept": can_accept,
        "unknown_gcs": unknown_gcs,
        "lines_review": lines_review,
    }


# ── Send now ───────────────────────────────────────────────────────────────


def send_now(project_id: str, actor_id: str, force_unknown: bool = False) -> dict:
    """Manual run_send on the project's latest missed / failed / partial run."""
    sb = get_supabase()
    if not get_settings_row(sb).get("enabled"):
        raise _refuse("disabled")
    project = _project(sb, project_id)
    if not project:
        raise _refuse("not_found", 404)
    runs = _runs(sb, project_id)
    latest = runs[0] if runs else None
    if not latest:
        raise _refuse("nothing_to_send")
    # Any run of the project still sending (another bid date included) would
    # email the same GCs alongside this one.
    if any(r.get("status") == "sending" for r in runs):
        raise _refuse("busy")
    if latest.get("status") not in RUN_SENDABLE_STATUSES:
        raise _refuse("nothing_to_send")
    if latest.get("accepted_at"):
        raise _refuse("already_accepted")
    if _has_sent_proposal(sb, project_id) or _human_sent(sb, project_id):
        raise _refuse("already_sent")
    gc_rows = _run_gcs(sb, latest["id"])
    project_gcs = _reachable_gc_ids(project_id)
    if not sendable_gc_ids(gc_rows, project_gcs, force_unknown=force_unknown):
        if sendable_gc_ids(gc_rows, project_gcs, force_unknown=True):
            raise _refuse("unknown_needs_force")
        raise _refuse("nothing_to_send")
    bid_day = date.fromisoformat(str(latest["bid_date"])[:10])
    # A run for an old date must not go out early once the internal date moved
    # to a later day: the scheduler sends on the new date.
    current_day = bid_date_of(project)
    if current_day and current_day > bid_day:
        raise _refuse("date_moved")
    out = run_send(project_id, bid_day, trigger="manual", actor_id=actor_id,
                   force_unknown=force_unknown, sb=sb)
    if out and out.get("status") in ("sent", "partial"):
        _bookkeep("dismiss missed", dismiss_notifications, project_id=project_id,
                  types=["auto_send.missed"])
    return out or {}


# ── Accept as submitted (section 9) ────────────────────────────────────────


def accept_refusal(run: dict | None, project_id: str, *, has_sending_or_sent: bool,
                   has_active_submission: bool, has_outcome: bool) -> str | None:
    """Pure: the refusal code for accepting `run`, or None. The codes are
    the database function's own ('auto_send_accept:<code>'), which re-checks
    all of it under its locks."""
    if not run or run.get("project_id") != project_id:
        return "run_not_for_project"
    if run.get("accepted_at"):
        return "already_accepted"
    if run.get("status") not in ("sent", "partial"):
        return "run_not_sent"
    if has_sending_or_sent:
        return "already_sent"
    if has_active_submission:
        return "external_submission_active"
    if has_outcome:
        return "outcome_recorded"
    return None


def accept_error_code(exc: Exception) -> str | None:
    """The 'auto_send_accept:<code>' apply_auto_send_acceptance raised, if any."""
    text = getattr(exc, "message", None) or str(exc)
    marker = "auto_send_accept:"
    if marker not in text:
        return None
    return text.split(marker, 1)[1].split()[0].strip("'\",.}")


def _accept_db_error(exc: Exception) -> AutoSendError:
    code = accept_error_code(exc)
    if code is None:
        logger.error("auto send accept database error: %s", exc)
        return AutoSendError(
            "The acceptance could not be saved. Try again; if it keeps failing, contact IT.",
            500, "db_error",
        )
    status_code = 404 if code in ("not_found", "project_not_found", "run_not_for_project") else 409
    return AutoSendError(
        ACTION_MESSAGES.get(code, "The auto send could not be accepted. Refresh and try again."),
        status_code, code,
    )


def acceptance_note(who: str, gc_names: list[str]) -> str:
    parts = [f"Auto send accepted as submitted by {who}."]
    if gc_names:
        parts.append("Sent to: " + ", ".join(gc_names) + ".")
    return " ".join(parts)


def accept_as_submitted(project_id: str, run_id: str, actor_id: str) -> dict:
    """Record the auto sent numbers as the bid, in one transaction
    (apply_auto_send_acceptance), then the same follow-ups as Mark submitted."""
    sb = get_supabase()
    project = _project(sb, project_id)
    if not project:
        raise _refuse("not_found", 404)
    try:
        rows = (
            sb.table("auto_send_runs").select("*").eq("id", run_id).limit(1).execute()
        ).data or []
    except APIError as exc:
        if not _is_bad_uuid(exc):
            raise
        rows = []
    run = rows[0] if rows else None
    sends = (
        sb.table("proposal_sends").select("id, status").eq("project_id", project_id)
        .in_("status", ["sent", "sending"]).limit(1).execute()
    ).data or []
    code = accept_refusal(
        run, project_id,
        has_sending_or_sent=bool(sends),
        has_active_submission=bool(external_submission._active_submission(sb, project_id)),
        has_outcome=external_submission._has_outcome(sb, project_id),
    )
    if code:
        raise _refuse(code, 404 if code == "run_not_for_project" else 409)
    gc_rows = _run_gcs(sb, run_id)
    sent_names = [r["gc_name"] for r in gc_rows if r.get("status") == "sent"]
    if not sent_names:
        raise _refuse("no_sent_gcs")

    state, head = external_submission._state_and_send_out_head(sb, project_id)
    xcode = external_submission.eligibility(project, state, has_outcome=False,
                                            has_active_submission=False)
    if xcode:
        raise AutoSendError(
            external_submission.REASON_MESSAGES.get(xcode, ACTION_MESSAGES["stale"]), 409, xcode
        )
    lanes, events = external_submission.target_lanes(state)
    note = acceptance_note(external_submission._display_name(sb, actor_id), sent_names)
    events[-1]["note"] = note
    owner = workflow.owner_role_for("submitted")
    doc = {
        "project_id": project_id,
        "run_id": run_id,
        "actor_id": actor_id,
        # The head the lanes were computed from: the database compare-and-set
        # guards exactly that state.
        "expected_send_out_head": head,
        "lanes": lanes,
        "events": events,
        "note": note,
        "headline": {"current_stage": "submitted",
                     "current_owner_role": owner.value if owner else None},
    }
    try:
        data = sb.rpc("apply_auto_send_acceptance", {"p": doc}).execute().data
    except APIError as exc:
        raise _accept_db_error(exc) from exc

    _bookkeep("accept audit", audit, actor_id, "auto_send.accepted", "project", project_id,
              {"run_id": run_id, "gcs": sent_names, "total_amount": run.get("total_amount")})
    _after_accept(project, project_id)
    return {"run_id": run_id, "result": data, "gcs": sent_names}


def _after_accept(project: dict, project_id: str) -> None:
    """Best-effort follow-ups once the transaction committed: the bell
    cleanup a walk through every lane would have done, and the submission
    news (external_submission._after_submit's set)."""
    try:
        state = workflow.load_category_state(project_id)
        for cat in workflow.CATEGORY_ORDER:
            workflow._dismiss_stale_notifications(project_id, cat, state)
        dismiss_notifications(project_id=project_id, types=["proposal_send_failed"],
                              type_prefixes=["auto_send."])
        for r in (
            Role.ESTIMATING_ENGINEER_MATERIALS,
            Role.ESTIMATING_ENGINEER_LABOR,
            Role.EXECUTIVE,
        ):
            notify_role(
                r, project_id, "submitted",
                f"Auto send accepted as the submitted bid for {project['name']}",
            )
    except Exception:  # noqa: BLE001 (the acceptance is committed)
        logger.exception("post-accept follow-ups failed for project %s", project_id)

