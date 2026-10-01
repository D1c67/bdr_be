"""Branded email mirror of every in-app notification.

Whenever a notification row is created (role broadcast, single user, due-date
reminder, or estimator note), the same recipient also gets a G3-branded HTML
email with a button that deep-links straight to the page the notification is
about. The bell and the inbox stay in sync.

Design notes:
- Fire-and-forget: `queue()` hands off to a daemon thread so a notification
  never adds Graph latency to the request that created it, and a send failure
  can never break the originating flow (e.g. the due-reminder ledger rollback).
- Best-effort: every send is wrapped; one bad recipient never stops the rest.
- Self-gating: sends only when `notification_emails_enabled` is set AND Graph
  credentials exist. Tests force the flag off (tests/conftest.py), so no test
  ever spawns a thread or touches the network.

The deep link resolves by recipient role: estimators go to the estimator
portal (`/estimator/projects/{id}`), everyone else to the internal project hub
(`/projects/{id}`); a notification with no project falls back to the role's
home page.
"""

import logging
import threading
from urllib.parse import quote, urlsplit

from app.core.config import get_settings
from app.core.features import SubApp, home_path, is_enabled, notification_sub_app
from app.core.roles import Role
from app.core.supabase_client import get_supabase
from app.services import graph_email
from app.services.email_branding import (
    LOGO_CONTENT_ID,
    LOGO_FILENAME,
    logo_bytes,
    render_notification_email,
)

logger = logging.getLogger(__name__)

# type (or its first dotted segment) -> (email heading, button label). The
# heading is a short human title; the message body is the same text the bell
# shows. due.* matches on the "due" segment; the per-offset/expired wording
# lives in the message itself.
_TYPE_META: dict[str, tuple[str, str]] = {
    "verified": ("Pricing committed — ready to send out", "Open Send Out"),
    "stage_handoff": ("A project moved to your stage", "Open project"),
    "drawing_changed": ("Project drawings changed", "Review drawings"),
    "gono_go": ("Project accepted — send to estimator", "Open project"),
    "assigned": ("You were assigned to a project", "Open your assignment"),
    "files_updated": ("Project files updated", "View the files"),
    "estimate_submitted": ("Estimate submitted", "Review the estimate"),
    # Revision rounds send their own high-importance email (revision_email.py);
    # the bell rows are created with mirror_email=False, so this entry only
    # matters if a future caller re-enables the mirror for this type.
    "estimate_revised": ("Estimator sent changes/revisions", "Review changes"),
    "rfq.reply_received": ("A vendor replied to an RFQ", "View quotes"),
    "quote.received": ("A vendor quote came in", "View quotes"),
    # A quote recorded after the bid was submitted (0138): on record only, the
    # sent price is unchanged. Both Estimating engineer focuses hear it.
    "late_quote.received": ("A quote came in after the bid was submitted", "View quotes"),
    "bid_outcome": ("Bid outcome recorded", "Open project"),
    "submitted": ("Bid submitted", "Open project"),
    "proposal_send_failed": ("A proposal send failed", "Open Send Out"),
    "security_alert": ("Security alert", "Review activity"),
    "estimator_note": ("New note on a project", "Open the conversation"),
    "nudge": ("You've been nudged about a to-do", "Open your to-dos"),
    "due": ("Deadline approaching", "Open project"),
    # Project Management. pm_* types deep-link to the PM view (see _deep_link).
    "pm_activated": ("Project won — entered Preconstruction", "Open project"),
    "pm_stage_change": ("Project moved PM stages", "Open project"),
    "pm_outcome_conflict": ("Outcome change attempted on an active PM project", "Review project"),
    # Types that reached here only through _DEFAULT_META until the notification
    # log started rendering these same headings as its entry titles.
    "reverify_required": ("Pricing changed — re-verification required", "Open project"),
    "project_abandoned": ("Project abandoned", "Open project"),
    "project_withdrawn": ("Project withdrawn", "Open your assignments"),
    "project_reactivated": ("Project reactivated", "Open your assignment"),
    "submittal.response_received": ("A vendor returned a submittal", "View submittals"),
    # A GC added after the bid went out (0118): the sender asked for a per-GC
    # price change (Executives), and the decision back to the sender. All three
    # deep-link to that GC's modal in the project side menu (see _deep_link).
    "gc_pricing.approval_requested": ("GC pricing change needs your approval", "Review pricing"),
    "gc_pricing.approved": ("GC pricing change approved", "Send the proposal"),
    "gc_pricing.rejected": ("GC pricing change rejected", "Open project"),
    # Mark submitted from the side menu (0140): a non-approver's permission
    # request to the approvers, the decision back to them, and the undo
    # notice. All deep-link to the project's Mark submitted box.
    "external_submission.requested": (
        "Permission requested to mark a bid submitted",
        "Review the request",
    ),
    "external_submission.granted": ("Mark submitted permission granted", "Open Mark submitted"),
    "external_submission.denied": ("Mark submitted permission denied", "Open project"),
    "external_submission.revoked": ("Mark submitted permission revoked", "Open project"),
    "external_submission.undone": ("A marked submission was undone", "Open project"),
    # RFP matching (docs/RFP_MATCHING.md 3.9): one bell row per sweep tick that
    # merged anything. Created with mirror_email=False and no project, so this
    # entry only gives the type a title if the mirror is ever enabled.
    "rfp_match.merged": ("New RFP matches merged", "Open RFP emails"),
    # NGEM portal invitations (docs/RFP_NGEM_PORTAL.md section 5): bell rows
    # only (mirror_email=False); these entries give the types a title in the
    # notification log and a heading if the mirror is ever enabled.
    "rfp_ngem.login_failed": ("NGEM login failed", "Open RFP ingestion settings"),
    "rfp_ngem.new_invitations": ("New NGEM invitations", "Open the NGEM tab"),
    "rfp_ngem.invitation_changed": ("An NGEM invitation changed", "Open the NGEM tab"),
    "rfp_ngem.scan_failed": ("An NGEM scan failed", "Open RFP ingestion settings"),
    # BuildingConnected Bid Board (docs/RFP_BUILDINGCONNECTED.md): the first
    # three are bell rows only; `rfp_bc.disconnected` also mirrors to email
    # (IT Admins and Executives, who can reconnect from Settings).
    "rfp_bc.new_invitations": (
        "New BuildingConnected invitations",
        "Open the BuildingConnected tab",
    ),
    "rfp_bc.invitation_changed": (
        "A BuildingConnected invitation changed",
        "Open the BuildingConnected tab",
    ),
    "rfp_bc.scan_failed": ("A BuildingConnected scan failed", "Open RFP ingestion settings"),
    "rfp_bc.disconnected": ("BuildingConnected disconnected", "Open RFP ingestion settings"),
    # RFP project creation (docs/RFP_CREATE.md section 7): the Executive's
    # notice of a project parked in Go/No-Go and the Estimating Admin's
    # intake task, both mirrored. The intake subject names the missing
    # fields in plain words (see _subject and intake_missing_phrase).
    "rfp_create.created": ("Project created from an RFP invitation", "Open project"),
    "rfp_create.intake_needed": ("Intake details needed", "Open project"),
    # RFP processing alerts to IT Admin (docs/RFP_EMAIL_INGESTION.md,
    # "Failures and alerts"): a row that ran out of retries, and the model
    # being away long enough to matter, then back. All land on /rfp-processing.
    "rfp_processing.step_failed": ("An RFP item failed after every retry", "Open RFP processing"),
    "rfp_processing.model_away": ("The RFP AI model is unavailable", "Open RFP processing"),
    "rfp_processing.model_back": ("The RFP AI model is back", "Open RFP processing"),
    # Calling In (docs/CALLING_IN.md section 5): once when a project lands on
    # each call list, to Executives and Estimating Engineers (Labor). Both
    # deep-link to the Calling In page opened on the project (see _deep_link).
    "call_in_pre_bid": ("A project is ready for before-bid calls", "Open Calling In"),
    "call_in_post_bid": ("A project is ready for after-bid calls", "Open Calling In"),
}

# Calling In notification type -> the call round its link opens.
_CALL_IN_ROUNDS = {"call_in_pre_bid": "pre_bid", "call_in_post_bid": "post_bid"}

_DEFAULT_META = ("BDR notification", "Open BDR")

# Plain-word labels for the intake fields the RFP creation step can leave
# empty (project_intake.missing_intake_fields names them by projects column).
# The nine Go/No-Go rubric answers collapse into one phrase: the subject line
# is a to-do list, not a form.
INTAKE_FIELD_LABELS: dict[str, str] = {
    "internal_bid_at": "internal bid date",
    "due_from_estimator_at": "estimator due",
    "due_from_vendors_at": "vendor due",
    "bid_time": "bid time",
}
INTAKE_RUBRIC_LABEL = "Go/No-Go answers"
_INTAKE_TYPE = "rfp_create.intake_needed"
# Types whose message is mostly a quote of an outside sender (the project name
# came from an RFP email or a portal, a failure alert names the email's
# subject): rendered as plain text, no URL ever linkified.
_NO_LINKIFY_PREFIX = ("rfp_create.", "rfp_processing.")


def linkify_for(type_: str | None) -> bool:
    """Whether a notification type's message gets the linkify pass at all:
    the RFP creation notices (`rfp_create.*`) and processing alerts
    (`rfp_processing.*`) never do. For every other type the pass is keyed by
    the SOURCE of each URL, not by the type (see `link_hosts_for`): any
    message can interpolate outside text (an RFP-derived project name, an
    estimator note preview, a vendor name), so only app links are anchored."""
    return not (type_ or "").startswith(_NO_LINKIFY_PREFIX)


def link_hosts_for() -> frozenset[str]:
    """The only hosts a notification mirror email renders as a link: the
    app's own frontend. The house writes no other URL into a notification
    message, so any other URL came from an outside source and stays plain
    escaped text."""
    host = (urlsplit(get_settings().frontend_url or "").hostname or "").lower()
    return frozenset({host}) if host else frozenset()


def intake_missing_phrase(missing) -> str:
    """The missing intake fields as one comma-separated phrase in the order
    the list carries them: the dates by their labels, the rubric answers
    collapsed into one phrase at the position of the first, anything
    unknown by its column name. Empty list -> empty string."""
    from app.services import project_intake

    rubric = set(project_intake.RUBRIC_KEYS)
    words: list[str] = []
    for key in missing or []:
        if key in rubric:
            label = INTAKE_RUBRIC_LABEL
        else:
            label = INTAKE_FIELD_LABELS.get(str(key), str(key).replace("_", " "))
        if label not in words:
            words.append(label)
    return ", ".join(words)


def heading_for(type_: str) -> str:
    """The short human title for a notification type — the email heading, reused
    by the per-project notification log as an entry title so both surfaces name
    the same event the same way."""
    return _meta(type_)[0]


def _meta(type_: str) -> tuple[str, str]:
    base = _TYPE_META.get(type_) or _TYPE_META.get(type_.split(".", 1)[0], _DEFAULT_META)
    heading, cta = base
    # The due poller emits a single past-due "expired" notice — reflect that.
    if type_.startswith("due.") and type_.endswith(".expired"):
        heading = "Deadline passed"
    return heading, cta


def _deep_link(
    project_id: str | None,
    role: str | None,
    type_: str | None = None,
    metadata: dict | None = None,
) -> str:
    """Where this notification's email button lands.

    Flag-aware, unlike an in-app link: these URLs are permanent and arrive in a
    mailbox, so one built for a module this deployment doesn't serve is a dead
    link forever rather than a redirect the shell can quietly fix.

    `metadata` is the notification row's optional detail (0118): a gc_pricing.*
    row names its GC, and the link then opens that GC's modal in the project
    side menu instead of the bare project page.
    """
    base = get_settings().frontend_url.rstrip("/")
    is_estimator = role == Role.ESTIMATOR.value
    if project_id:
        # PM notifications land on the PM view of the project. Estimators never
        # receive them (notify_role refuses estimator broadcasts), but the
        # estimator check stays first as a defense-in-depth link floor.
        if not is_estimator:
            if notification_sub_app(type_) is SubApp.PM and is_enabled(SubApp.PM):
                return f"{base}/pm/projects/{project_id}"
            # Bidding's project page is the fallback for everything else — and
            # for a PM row on a PM-less deployment, since the project is still
            # there and its bid history is what remains readable.
            if not is_enabled(SubApp.BIDDING):
                return f"{base}{home_path()}"
            gc_id = (metadata or {}).get("gc_id") if (type_ or "").startswith("gc_pricing.") else None
            if gc_id:
                return f"{base}/projects/{project_id}?box=gcs&gc={gc_id}"
            # Mark submitted (0140) opens its side-menu box on arrival.
            if (type_ or "").startswith("external_submission."):
                return f"{base}/projects/{project_id}?box=mark_submitted"
            # A late quote (0138) lands on Receive Quotes, where it was recorded.
            if type_ == "late_quote.received":
                return f"{base}/projects/{project_id}?step=receive_quotes"
            # Calling In (0142) opens the list on that project's detail.
            if type_ in _CALL_IN_ROUNDS:
                return (
                    f"{base}/calling-in?round={_CALL_IN_ROUNDS[type_]}"
                    f"&project={quote(str(project_id))}"
                )
        prefix = "/estimator/projects" if is_estimator else "/projects"
        return f"{base}{prefix}/{project_id}"
    # RFP processing alerts: the Stuck lane, opened on the failed row when the
    # alert names one (an email row, or a portal invitation).
    if (type_ or "").startswith("rfp_processing.") and not is_estimator:
        meta = metadata or {}
        row_id = meta.get("row_id")
        if type_ == "rfp_processing.step_failed" and row_id:
            param = "invitation" if meta.get("source") == "portal" else "email"
            return f"{base}/rfp-processing?lane=stuck&{param}={quote(str(row_id))}"
        return f"{base}/rfp-processing?lane=stuck"
    # A nudge isn't tied to a project — send the recipient straight to their list.
    if type_ == "nudge":
        return f"{base}/todos"
    return f"{base}/estimator" if is_estimator else f"{base}{home_path()}"


def _project_label(project: dict | None) -> str | None:
    if not project:
        return None
    number, name = project.get("number"), project.get("name")
    if number and name:
        return f"#{number} · {name}"
    return name or (f"#{number}" if number else None)


def _subject(
    heading: str,
    project: dict | None,
    type_: str | None = None,
    metadata: dict | None = None,
) -> str:
    if type_ == _INTAKE_TYPE:
        # The intake task's subject is the task itself: which fields the
        # Estimating Admin still has to fill on which project, so the mail
        # reads complete in an inbox list without being opened.
        meta = metadata or {}
        number = (project or {}).get("number") or meta.get("number")
        name = (project or {}).get("name")
        label = " ".join(str(part) for part in (number, name) if part) or "a new project"
        phrase = intake_missing_phrase(meta.get("missing"))
        return f"G3 BDR · {heading} for {label}" + (f": {phrase}" if phrase else "")
    if project:
        number, name = project.get("number"), project.get("name")
        tag = f"#{number} {name}" if number and name else (name or (f"#{number}" if number else ""))
        if tag:
            return f"G3 BDR · {heading} — {tag}"
    return f"G3 BDR · {heading}"


def queue(rows: list[dict]) -> None:
    """Schedule branded emails for freshly-created notification rows.

    Each row is a dict with `user_id`, `project_id` (nullable), `type`,
    `message` — the same shape inserted into the notifications table. No-op
    (and never raises) when disabled or when Graph isn't configured.

    Pass the rows RETURNED BY the insert (they carry `id`) rather than the ones
    handed to it: an `id` lets each send record its email_log row back onto the
    notification (0091), which is what lets the per-project notification log
    collapse a bell row and its mirror email into one entry. Rows without an
    `id` still send — they just leave the link NULL.
    """
    rows = [r for r in (rows or []) if r.get("user_id")]
    if not rows:
        return
    settings = get_settings()
    if not settings.notification_emails_enabled or not settings.ms_client_id:
        return
    threading.Thread(target=_run, args=(rows,), daemon=True).start()


def _run(rows: list[dict]) -> None:
    """Resolve recipients/projects once, then send one personalized email per
    notification. Each step is isolated so a single failure can't lose the rest."""
    try:
        sb = get_supabase()
        user_ids = sorted({r["user_id"] for r in rows})
        profiles = (
            sb.table("profiles")
            .select("id, full_name, email, role, is_active")
            .in_("id", user_ids)
            .execute()
        ).data or []
        profile_by_id = {p["id"]: p for p in profiles}

        project_ids = sorted({r["project_id"] for r in rows if r.get("project_id")})
        project_by_id: dict[str, dict] = {}
        if project_ids:
            projects = (
                sb.table("projects")
                .select("id, name, number")
                .in_("id", project_ids)
                .execute()
            ).data or []
            project_by_id = {p["id"]: p for p in projects}
    except Exception:  # noqa: BLE001 — background work must never crash the worker
        logger.exception("Notification-email batch setup failed")
        return

    for row in rows:
        try:
            _send_one(row, profile_by_id.get(row["user_id"]), project_by_id.get(row.get("project_id")))
        except Exception:  # noqa: BLE001
            logger.exception(
                "Notification email failed (type=%s user=%s project=%s)",
                row.get("type"), row.get("user_id"), row.get("project_id"),
            )


def _send_one(row: dict, profile: dict | None, project: dict | None) -> None:
    if not profile or not profile.get("is_active") or not profile.get("email"):
        return  # no active recipient / no address — nothing to send
    type_ = row.get("type") or ""
    heading, cta_label = _meta(type_)
    html = render_notification_email(
        recipient_name=profile.get("full_name"),
        heading=heading,
        message=row.get("message") or "",
        cta_label=cta_label,
        cta_url=_deep_link(
            row.get("project_id"), profile.get("role"), type_, row.get("metadata")
        ),
        project_label=_project_label(project),
        linkify=linkify_for(type_),
        link_hosts=link_hosts_for(),
    )
    log = graph_email.send_mail(
        to=[profile["email"]],
        subject=_subject(heading, project, type_, row.get("metadata")),
        body_html=html,
        inline_images=[(LOGO_CONTENT_ID, LOGO_FILENAME, logo_bytes(), "image/jpeg")],
        project_id=row.get("project_id"),
    )
    _link_email_log(row.get("id"), log)


def _link_email_log(notification_id: str | None, log: dict | None) -> None:
    """Record which email delivered this notification (0091). Best-effort: the
    mail is already sent, so a lost link only costs the notification log its
    delivery badge — never the send, and never the bell row."""
    if not notification_id or not log or not log.get("id"):
        return
    try:
        get_supabase().table("notifications").update({"email_log_id": log["id"]}).eq(
            "id", notification_id
        ).execute()
    except Exception:  # noqa: BLE001
        logger.warning(
            "Could not link notification %s to its mirror email", notification_id, exc_info=True
        )
