"""RFP Ingestion email intake: the review queue API under /rfp-emails.

The HTTP surface of docs/RFP_EMAIL_INGESTION.md section 7 plus the project
matching actions of docs/RFP_MATCHING.md section 7. Every state transition
lives in app/services/rfp_email_ingest.py and every rule validation in
app/services/rfp_email_auth.py; this module only validates the request, maps
the services' refusals onto HTTP statuses and shapes the responses the
/rfp-emails page is written against.

Audit: every human action on a message (review, continue, dismiss, set
method, block sender, and the match actions: merge, duplicate, reject, set
GC, reopen, unmerge) is audited by the SERVICE, inside the same conditional
update that performs it, so the log records the action exactly once and only
when it actually won its race. This module audits the two writes nothing else
covers, the authorized-sender rule create and delete.

Access, three locks deep:

- The env flag. `require_rfp_emails` is a ROUTER-level dependency, so while
  the email-intake switch is false every route 404s with the bare "Not Found"
  body before a token is ever read (the same contract as the Bid File
  Splitter and the ingestion sandbox: a deployment that does not serve the
  feature is indistinguishable from one where it was never implemented).
- Roles, in each endpoint signature. The review queue is
  `require_review_queue` (`RFP_REVIEW_ROLES` from app.core.roles: Estimating
  Admin, IT Admin, Executive and both Estimating Engineer focuses); the
  authorized-sender rules are `require_sender_admin` (Executive, Estimating
  Admin, IT Admin); unmerge is `require_unmerge` (the same three roles: it
  detaches a GC the system put on a project). Blocking and unblocking a
  sender is `require_block_admin`: those same three roles PLUS any dev
  account, whatever role it is wearing. The read-only accountant and the
  external estimator never reach any of them. Locked rules and locked
  (platform) blocks are IT Admin only, checked inside the handlers that can
  touch one, because the role set differs per ROW, not per route.
- Rate limits, in the decorators: the generous catch-all budget
  (`RateLimitScope.DEFAULT`) on every route, per section 9 of the doc. None
  of this surface is expensive; the limiter is only an abuse backstop.

Body safety: mail content is attacker-controlled. Only the plain-text body
is ever stored (the table has no HTML column) and only names/types/sizes of
attachments, so a response from here can never carry markup or bytes a
sender chose. The extracted facts and the model's reasoning are capped and
schema-checked by the service before storage and are returned as plain text.

Confidential dates: candidate breakdowns store date KINDS, never values, and
the detail route passes everything it assembles through
`rfp_match.redact_candidates` for roles outside ACTUAL_BID_VIEWER_ROLES, so a
sub-score taken from the actual bid date never reaches a role that may not
see that date. The detail route selects an explicit column list, never `*`.

Event-loop rule: every handler is a plain `def` - the Supabase SDK is
synchronous, so FastAPI must run these in its threadpool (see app/core/deps).

Service coupling: `app/services/rfp_email_ingest`, `app/services/rfp_match`
and `app/services/rfp_email_auth` are reached through `_ingest()` /
`_match()` / `_auth()`, which import on first use. That keeps this module
importable on its own and gives the tests one seam per service to stub.
"""

from __future__ import annotations

import logging
import uuid
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field

from app.core.config import get_settings
from app.core.deps import CurrentUser, get_current_user, require_role
from app.core.error_codes import ErrorCode, RateLimitScope
from app.core.ratelimit import rate_limit
from app.core.roles import (
    ACTUAL_BID_VIEWER_ROLES,
    RFP_REVIEW_ROLES,
    RFP_VIEW_ROLES,
    Role,
)
from app.core.supabase_client import get_supabase
from app.models.schemas import (
    RfpMatchDuplicateIn,
    RfpMatchGcIn,
    RfpMatchMergeIn,
    RfpMatchReopenIn,
    RfpMatchUnmergeIn,
)
from app.services import rfp_email_visibility as visibility
from app.services.notifications import audit
from app.services.proposal_send import ProposalSendError

logger = logging.getLogger(__name__)


# ── Feature gate ─────────────────────────────────────────────────────────


def rfp_emails_enabled() -> bool:
    """Whether this deployment serves the email-intake slice.

    `Settings.rfp_email_ingestion_enabled` is a computed property (the master
    RFP_INGEST_ENABLED switch AND a non-empty watched-mailbox list). It is
    read through `getattr`, under both spellings the setting has carried, so
    a rename in app/core/config.py can never turn this router into a 500 -
    and an absent flag fails CLOSED, never open.
    """
    settings = get_settings()
    for name in ("rfp_email_ingestion_enabled", "rfp_email_enabled"):
        value = getattr(settings, name, None)
        if value is not None:
            return bool(value)
    return False


def require_rfp_emails() -> None:
    """Router-level dependency gating /rfp-emails on its env flag.

    Same contract as `require_rfp_ingest` in app/core/features.py: while the
    switch is off every route 404s with the bare "Not Found" body before auth
    runs.
    """
    if not rfp_emails_enabled():
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not Found")


# ── Role sets ────────────────────────────────────────────────────────────

# Who works the queue (doc section 1, "Review queue roles"): the Estimating
# Admin primarily, with the IT Admin, the Executive and both engineer focuses
# able to act. The accountant is read-only and the estimator is external, so
# neither is here. The tuple itself lives in app.core.roles so the projects
# router can gate the email block of its match route on the same set without
# a router-to-router import (docs/RFP_MATCHING.md, refactors).
REVIEW_QUEUE_ROLES = RFP_REVIEW_ROLES
# Who may READ the queue (docs/RFP_EMAIL_VISIBILITY.md 3.3): the same set plus
# the read-only accountant. Since 0134 a read shows only the rows sighted in
# the viewer's own mailboxes (or a shared one), so opening the page to the
# accountant shows them their own mail, nothing more; every ACTION still
# demands REVIEW_QUEUE_ROLES, so they get a 403 the moment they try one.
VIEW_QUEUE_ROLES = RFP_VIEW_ROLES
# Who manages the authorization rules. A rule decides which outside senders
# the pipeline trusts, so it is a narrower set than the queue itself.
SENDER_ADMIN_ROLES = (Role.EXECUTIVE, Role.ESTIMATING_ADMIN, Role.IT_ADMIN)
# Who may reverse a merge (docs/RFP_MATCHING.md 3.7): unmerge detaches a GC
# the system put on a project, so it is the same three roles, not the queue.
UNMERGE_ROLES = (Role.ESTIMATING_ADMIN, Role.EXECUTIVE, Role.IT_ADMIN)
# Who may block and unblock senders (doc 3.1). The same three roles as the
# authorization rules - blocking is the mirror image of authorizing - plus
# any dev account whatever role it is currently wearing, which is what
# `require_block_admin` below adds on top of the role test.
BLOCK_ADMIN_ROLES = (Role.EXECUTIVE, Role.ESTIMATING_ADMIN, Role.IT_ADMIN)

# Bound once at import so the tests (and a reader) can identify the exact
# dependency on each route: require_role() builds a fresh closure per call.
require_review_queue = require_role(*REVIEW_QUEUE_ROLES)
require_view_queue = require_role(*VIEW_QUEUE_ROLES)
require_sender_admin = require_role(*SENDER_ADMIN_ROLES)
require_unmerge = require_role(*UNMERGE_ROLES)


async def require_block_admin(user: CurrentUser = Depends(get_current_user)) -> CurrentUser:
    """BLOCK_ADMIN_ROLES, or a dev account wearing a REVIEW-QUEUE role.

    Written out rather than built from `require_role` because it is the one
    gate on this router that admits a profile flag as well as a role: a dev
    switched into an engineer focus to reproduce something still needs to be
    able to block the sender that is flooding the queue.

    The dev flag does NOT open this to every role. Blocking a sender is a
    global, cross-user action: it parks that sender's mail for everybody and
    keeps parking it. The read-only accountant may never take it, and since
    0134 a dev may be wearing that role (or the external estimator's), so the
    flag only extends the gate across roles that could already act on the
    queue.
    """
    if user.role in BLOCK_ADMIN_ROLES or (user.is_dev and user.role in RFP_REVIEW_ROLES):
        return user
    raise HTTPException(status.HTTP_403_FORBIDDEN, "Insufficient role")


def _may_unlock(user: CurrentUser) -> bool:
    """Who may remove a LOCKED (platform) block: the IT Admin, or a dev.
    Everyone else sees the row and its Unblock button disabled, and is
    refused here too, because the role set differs per ROW, not per route."""
    return user.role == Role.IT_ADMIN or user.is_dev

# Reads and writes here are cheap, high-frequency clicks (a reviewer works
# through a stack in one sitting), so they ride the catch-all budget.
rfp_emails_rate_limit = rate_limit(
    RateLimitScope.DEFAULT, lambda: get_settings().default_rate_limit_per_min
)

router = APIRouter(
    prefix="/rfp-emails",
    tags=["rfp-emails"],
    dependencies=[Depends(require_rfp_emails)],
)


# ── Constants ────────────────────────────────────────────────────────────

_EMAIL_NOT_FOUND = "Email not found"
_RULE_NOT_FOUND = "Rule not found"
_PROJECT_NOT_FOUND = "Project not found"
_GC_NOT_FOUND = "GC not found"
_NO_MERGE_TO_UNDO = "This email has no merge to undo."
# Only the rule writes are audited here; see the module docstring.
_AUDIT_RULE = "rfp_authorized_sender"

_DEFAULT_LIMIT = 50
_MAX_LIMIT = 200

Tab = Literal["review", "matches", "unauthorized", "flagged", "processed"]

# Which statuses each tab holds (doc section 2's status vocabulary, widened
# by docs/RFP_MATCHING.md section 2). Matches is the human lane the match
# step feeds; Processed holds every row the pipeline finished with (parked,
# merged into a project, or a duplicate of a GC already on one), the status
# column telling them apart. Flagged is the read-only "nothing more will
# happen to this" bucket: the auth and keyword refusals, the model's
# confident no, a human's no, and the rows that ran out of attempts.
# Order is the tab order on /rfp-emails; the dashboard card's Open button
# links to the first non-empty tab in it.
TAB_STATUSES: dict[str, tuple[str, ...]] = {
    "review": ("review_llm",),
    "matches": ("review_match",),
    "unauthorized": ("flagged_unauthorized",),
    "flagged": (
        "flagged_auth",
        "flagged_no_keywords",
        "flagged_llm_no",
        "rejected_by_review",
        # 0133: parked because somebody blocked the sender while this message
        # was still in flight.
        "blocked_sender",
        "failed",
    ),
    # `create` and `created` (docs/RFP_CREATE.md 3): the creation step's
    # pending row and its exit, listed with the rest of the finished work.
    "processed": ("done", "merged", "duplicate", "harvest", "split", "create", "created"),
}

# The seven values the invitation_method check constraint allows (0120,
# narrowed by 0124 and 0128: buildingconnected, ngem and planhub mail is
# sanitized out at listing time and none is a method any more; widened by
# 0127: gc_portal for a GC that invites through its own bidding portal, by
# 0129: pipelinesuite for a GC whose plan room is a PipelineSuite portal,
# docs/RFP_PIPELINESUITE.md, and by 0148: smartbid for invitations sent
# through ConstructConnect's SmartBid, docs/RFP_SMARTBID.md).
INVITATION_METHODS = (
    "organic",
    "procore",
    "pipelinesuite",
    "smartbid",
    "gc_portal",
    "general",
    "nonorganic",
)

# List rows deliberately exclude body_text, attachments_meta, auth_raw and
# match_candidates: all four are large (the first three attacker-supplied),
# and the detail route serves them on expand. The match columns are what the
# Matches and Processed tabs render per row (docs/RFP_MATCHING.md 3.10).
_LIST_SELECT = (
    "id, received_at, from_address, from_name, subject, keyword_hits, "
    "llm_answer, llm_confidence, llm_reasoning, status, flag_reason, "
    "invitation_method, has_attachments, primary_mailbox, "
    "extracted_project_name, extracted_gc_name, match_project_id, match_score, "
    "resolved_gc_id, possible_rebid_project_id, match_review_decision, "
    "harvest_id, harvested_at, created_project_id, create_blocked_archive_id"
)
# The detail row: every column of rfp_emails (0120 plus 0122), named. Never
# `*`, so a column added by a later migration is a deliberate addition here
# rather than something that leaks into the drawer by default.
_DETAIL_SELECT = (
    # 0120: envelope, body, authentication, classification, decisions
    "id, internet_message_id, primary_mailbox, conversation_id, from_address, "
    "from_name, to_recipients, cc_recipients, subject, body_preview, body_text, "
    "received_at, has_attachments, attachments_meta, auth_spf, auth_dkim, "
    "auth_dmarc, auth_compauth, auth_raw, auth_verdict, keyword_hits, llm_answer, "
    # 0146 / 0147: the domains the SPF and DKIM passes were judged for (why
    # an unaligned pass failed auth, doc 3.3) and when the classify model
    # answered (what the per-sender classify budget counts by, doc 3.5).
    "auth_spf_domain, auth_dkim_domain, classified_at, "
    "llm_confidence, llm_reasoning, llm_model, llm_prompt_version, review_decision, "
    "review_by, review_at, authorization_kind, authorization_rule_id, continued_by, "
    "continued_at, invitation_method, status, flag_reason, decided_at_step, "
    "attempts, next_attempt_at, last_error, created_at, updated_at, "
    # 0122: extraction, GC resolution, match decision, review, exclusions
    "extracted_project_name, extracted_gc_name, extracted_bid_due_at, "
    "extracted_bid_due_has_time, extracted_bid_notes, extract_model, "
    "extract_prompt_version, extracted_at, sibling_of_email_id, resolved_gc_id, "
    "resolved_gc_contact_id, gc_match_kind, gc_match_score, gc_candidates, "
    "match_candidates, match_project_id, match_score, match_llm_model, "
    "match_llm_prompt_version, match_weights, matched_at, match_review_decision, "
    "match_review_by, match_review_at, match_review_agreed, excluded_project_ids, "
    "possible_rebid_project_id, possible_rebid_score, "
    # 0123: the harvest link
    "harvest_id, harvested_at, "
    # 0130: the project the creation step made
    "created_project_id, "
    # 0141: a project made from (or merged with) this email was deleted
    "create_blocked_archive_id"
)
_RULE_SELECT = "id, kind, value, method, locked, created_by, created_at"
_BLOCK_SELECT = "id, kind, value, reason, locked, created_by, created_at"
_BLOCK_NOT_FOUND = "Blocked sender not found"
_AUDIT_BLOCK = "rfp_blocked_sender"
# The note a blocker may leave with a block, capped so the list stays a list.
_BLOCK_REASON_MAX = 200
_PROJECT_REF_SELECT = "id, name, number"
_GC_REF_SELECT = "id, name"


# ── Service seams ────────────────────────────────────────────────────────


def _ingest():
    """app/services/rfp_email_ingest, imported on first use."""
    from app.services import rfp_email_ingest

    return rfp_email_ingest


def _match():
    """app/services/rfp_match (the pure scorer module), imported on first use."""
    from app.services import rfp_match

    return rfp_match


def _auth():
    """app/services/rfp_email_auth, imported on first use."""
    from app.services import rfp_email_auth

    return rfp_email_auth


def _harvest():
    """app/services/rfp_harvest, imported on first use."""
    from app.services import rfp_harvest

    return rfp_harvest


def _create():
    """app/services/rfp_create, imported on first use."""
    from app.services import rfp_create

    return rfp_create


# ── Helpers ──────────────────────────────────────────────────────────────


def _uuid_or_404(value: str | None, message: str) -> str:
    """Refuse an id PostgREST could not cast BEFORE any query runs, with the
    same 404 a missing row gets (a raw 22P02 would surface as a 500 that has
    lost its CORS headers). The original spelling is returned so the fake
    databases in tests and PostgREST agree."""
    try:
        uuid.UUID(str(value))
    except (ValueError, AttributeError, TypeError):
        raise HTTPException(status.HTTP_404_NOT_FOUND, message) from None
    return str(value)


def _mailboxes_by_email(sb, email_ids: list[str]) -> dict[str, list[str]]:
    """`{rfp_email_id: [mailbox, ...]}` from rfp_email_sightings.

    One extra query rather than a PostgREST embed: the same message can be
    seen by several watched mailboxes and the list has to show all of them.
    """
    if not email_ids:
        return {}
    rows = (
        sb.table("rfp_email_sightings")
        .select("rfp_email_id, mailbox")
        .in_("rfp_email_id", email_ids)
        .execute()
    ).data or []
    out: dict[str, list[str]] = {}
    for row in rows:
        # Lowercased and trimmed here as well as at every insert site: a row
        # written before 0134 normalized them would otherwise never match a
        # profile's (lowercased) rfp_mailboxes in _owners_by_mailbox, and the
        # Recipient column would show an owned mailbox as unowned.
        raw = row.get("mailbox")
        mailbox = raw.strip().lower() if isinstance(raw, str) else ""
        if not mailbox:
            continue
        seen = out.setdefault(row.get("rfp_email_id"), [])
        if mailbox not in seen:
            seen.append(mailbox)
    for seen in out.values():
        seen.sort()
    return out


def _projects_by_id(sb, ids) -> dict[str, dict]:
    """`{project_id: {id, name, number}}` for the ids given (one query, or
    none when there is nothing to look up). The three reference columns are
    all a list row, a candidate or a match record ever attaches: never a
    date, so nothing here needs redacting by role."""
    clean = sorted({i for i in ids if i})
    if not clean:
        return {}
    rows = (
        sb.table("projects").select(_PROJECT_REF_SELECT).in_("id", clean).execute()
    ).data or []
    return {
        r["id"]: {"id": r["id"], "name": r.get("name"), "number": r.get("number")}
        for r in rows
        if r.get("id")
    }


def _gcs_by_id(sb, ids) -> dict[str, dict]:
    """`{gc_id: {id, name}}` for the ids given."""
    clean = sorted({i for i in ids if i})
    if not clean:
        return {}
    rows = (
        sb.table("general_contractors").select(_GC_REF_SELECT).in_("id", clean).execute()
    ).data or []
    return {r["id"]: {"id": r["id"], "name": r.get("name")} for r in rows if r.get("id")}


def _harvest_status_by_id(sb, ids) -> dict[str, str]:
    """`{harvest_id: status}` for the page's linked harvests (one query)."""
    wanted = sorted({str(i) for i in ids if i})
    if not wanted:
        return {}
    rows = sb.table("rfp_harvests").select("id, status").in_("id", wanted).execute().data or []
    return {r["id"]: r.get("status") for r in rows if r.get("id")}


def _email_row_or_404(
    sb, email_id: str, select: str = _DETAIL_SELECT, user: CurrentUser | None = None
) -> dict:
    """The row, or the 404 an unknown id gets.

    Passing `user` also enforces the per-mailbox scope of
    docs/RFP_EMAIL_VISIBILITY.md: `mailboxes` is added to the select and a row
    outside the viewer's scope raises the SAME 404, so a caller can never tell
    "not yours" from "does not exist". Every /{email_id} route passes it, and
    passes it before anything else looks at the row.
    """
    if user is not None and "mailboxes" not in [c.strip() for c in select.split(",")]:
        select = f"{select}, mailboxes"
    rows = (
        sb.table("rfp_emails").select(select).eq("id", email_id).limit(1).execute()
    ).data or []
    if not rows:
        raise HTTPException(status.HTTP_404_NOT_FOUND, _EMAIL_NOT_FOUND)
    if user is not None:
        visibility.assert_visible(rows[0], user)
    return rows[0]


def _email_exists_or_404(sb, email_id: str, user: CurrentUser) -> None:
    """The cheap pre-check every action runs before touching the service, so
    a missing row is a 404 rather than the service's 409 - and so is a row in
    somebody else's mailbox (docs/RFP_EMAIL_VISIBILITY.md 3.3)."""
    _email_row_or_404(sb, email_id, select="id", user=user)


def _gc_exists_or_404(sb, gc_id: str) -> None:
    """A body gc_id (merge with a GC, set GC): malformed or unknown is a 404
    before the service writes anything."""
    _uuid_or_404(gc_id, _GC_NOT_FOUND)
    rows = (
        sb.table("general_contractors").select("id").eq("id", gc_id).limit(1).execute()
    ).data or []
    if not rows:
        raise HTTPException(status.HTTP_404_NOT_FOUND, _GC_NOT_FOUND)


def _rule_for(sb, rule_id: str | None) -> dict | None:
    """The authorization rule that admitted a message, when one did (a
    GC-domain match and a human override both leave rule_id null)."""
    if not rule_id:
        return None
    rows = (
        sb.table("rfp_authorized_senders")
        .select(_RULE_SELECT)
        .eq("id", rule_id)
        .limit(1)
        .execute()
    ).data or []
    if not rows:
        return None
    row = rows[0]
    return {
        "id": row.get("id"),
        "kind": row.get("kind"),
        "value": row.get("value"),
        "method": row.get("method"),
        "locked": bool(row.get("locked")),
    }


def _profile_names(sb, ids: set[str]) -> dict[str, str]:
    clean = sorted({i for i in ids if i})
    if not clean:
        return {}
    rows = (
        sb.table("profiles").select("id, full_name").in_("id", clean).execute()
    ).data or []
    return {r["id"]: r.get("full_name") for r in rows if r.get("id")}


def _owners_by_mailbox(sb, mailboxes: set[str]) -> dict[str, str | None]:
    """`{mailbox: full_name or None}` for the Recipient column.

    One query over `profiles.rfp_mailboxes` (0134) for the whole page. A
    mailbox mapped onto several people takes the first name alphabetically:
    the column labels a recipient, it is not a roster. An unmapped mailbox
    answers None and the page falls back to the address.
    """
    wanted = {m for m in mailboxes if m}
    if not wanted:
        return {}
    rows = (
        sb.table("profiles")
        .select("full_name, rfp_mailboxes")
        .overlaps("rfp_mailboxes", sorted(wanted))
        .execute()
    ).data or []
    out: dict[str, str | None] = {m: None for m in wanted}
    for row in sorted(rows, key=lambda r: (r.get("full_name") or "")):
        for raw in row.get("rfp_mailboxes") or []:
            mailbox = raw.strip().lower() if isinstance(raw, str) else ""
            if mailbox in out and out[mailbox] is None:
                out[mailbox] = row.get("full_name")
    return out


def _attach_match_context(sb, row: dict) -> None:
    """The matching slice's additions to the detail row (docs/RFP_MATCHING.md
    section 7, GET /rfp-emails/{id}), attached in place:

    - `candidates`: the stored `match_candidates` with each entry's name and
      number refreshed from the live project row (a renamed project reads
      right; the stored values stay as the fallback for a deleted one), plus
      `gc_ids`, the GC ids on that project (one project_gcs query over the
      candidate ids), so the drawer can offer "Already on this project"
      without a read per candidate.
    - `match_project`, `resolved_gc`: the same two references the list attaches.
    - `match`: the email's latest rfp_project_matches row, open or closed,
      with its project reference and the deciding / acknowledging /
      unmerging users' names. Null until the row has been decided once.
    - `excluded_projects`: the projects an unmerge took this email away from
      (id, name, number, unmerge_reason, unmerged_by as the user's full name
      or null, unmerged_at), never offered as targets again.
    - `possible_rebid`: the wider name-only lookup's hit, flat
      `{id, name, number, score}`, or null.

    Every project reference is id, name and number only. Date values never
    ride along: the candidate breakdowns already carry date KINDS only, and
    `_detail` runs the assembled row through `rfp_match.redact_candidates`
    for roles outside ACTUAL_BID_VIEWER_ROLES.
    """
    ingest = _ingest()
    email_id = row["id"]
    candidates = [dict(c) for c in (row.get("match_candidates") or []) if isinstance(c, dict)]
    match = ingest.latest_match_for_email(sb, email_id)
    match = dict(match) if match else None
    excluded = [dict(e) for e in (ingest.excluded_projects_for_email(sb, row) or [])]

    project_ids = {c.get("project_id") for c in candidates}
    project_ids.add(row.get("match_project_id"))
    project_ids.add(row.get("possible_rebid_project_id"))
    if match:
        project_ids.add(match.get("project_id"))
    projects = _projects_by_id(sb, project_ids)

    candidate_ids = sorted({c.get("project_id") for c in candidates if c.get("project_id")})
    gc_ids_by_project: dict[str, list[str]] = {}
    if candidate_ids:
        links = (
            sb.table("project_gcs")
            .select("project_id, gc_id")
            .in_("project_id", candidate_ids)
            .execute()
        ).data or []
        for link in links:
            if link.get("project_id") and link.get("gc_id"):
                gc_ids_by_project.setdefault(link["project_id"], []).append(link["gc_id"])
    for cand in candidates:
        live = projects.get(cand.get("project_id"))
        if live:
            cand["name"] = live.get("name") or cand.get("name")
            cand["number"] = live.get("number") or cand.get("number")
        cand["gc_ids"] = sorted(gc_ids_by_project.get(cand.get("project_id"), []))
    row["candidates"] = candidates
    row["match_project"] = projects.get(row.get("match_project_id"))

    gc_ids = {row.get("resolved_gc_id")}
    if match:
        gc_ids.add(match.get("gc_id"))
    gcs = _gcs_by_id(sb, gc_ids)
    row["resolved_gc"] = gcs.get(row.get("resolved_gc_id"))

    if match:
        names = _profile_names(
            sb,
            {match.get("decided_by"), match.get("acknowledged_by"), match.get("unmerged_by")},
        )
        match["project"] = projects.get(match.get("project_id"))
        match["gc"] = gcs.get(match.get("gc_id"))
        match["decided_by_name"] = names.get(match.get("decided_by"))
        match["acknowledged_by_name"] = names.get(match.get("acknowledged_by"))
        match["unmerged_by_name"] = names.get(match.get("unmerged_by"))
    row["match"] = match

    if excluded:
        # The service hands back the unmerging user's id; the drawer renders
        # a name, so `unmerged_by` becomes the full name (null when unknown)
        # and the id moves to `unmerged_by_id`.
        names = _profile_names(sb, {e.get("unmerged_by") for e in excluded})
        for entry in excluded:
            actor_id = entry.get("unmerged_by")
            entry["unmerged_by_id"] = actor_id
            entry["unmerged_by"] = names.get(actor_id)
            entry.setdefault("unmerged_at", None)
    row["excluded_projects"] = excluded

    rebid_id = row.get("possible_rebid_project_id")
    rebid_project = projects.get(rebid_id) if rebid_id else None
    row["possible_rebid"] = (
        {
            "id": rebid_id,
            "name": (rebid_project or {}).get("name"),
            "number": (rebid_project or {}).get("number"),
            "score": row.get("possible_rebid_score"),
        }
        if rebid_id
        else None
    )


def _attach_harvest_context(sb, row: dict) -> None:
    """`harvest` (the rfp_harvests row without raw, cookies or claim token),
    `harvest_job` (queue poll info for an active or latest job) and
    `harvest_available` (`{ok, reason}`: whether the button should show).
    docs/RFP_HARVEST.md section 5."""
    harvest = _harvest()
    row["harvest"] = harvest.harvest_for_email(sb, row)
    try:
        from app.services import llm_queue

        row["harvest_job"] = llm_queue.poll_info(harvest.JOB_TYPE, row["id"])
    except Exception:  # noqa: BLE001 - poll info is a convenience
        logger.debug("rfp harvest: poll info failed", exc_info=True)
        row["harvest_job"] = None
    ok, reason = harvest.can_harvest(row)
    if ok and row.get("status") not in harvest.MANUAL_STATUSES:
        ok, reason = False, "The email is not at a stage that can be harvested."
    row["harvest_available"] = {"ok": ok, "reason": reason}


def _attach_create_context(sb, row: dict) -> None:
    """`created_project` (`{id, number, name}` of the project the creation
    step made from this row, or null) and `create_available` (whether the
    "Create project" button shows: a `done` row with a name, not a sibling
    follower, no project yet). docs/RFP_CREATE.md section 8."""
    create = _create()
    row["created_project"] = create.created_project_ref(sb, row.get("created_project_id"))
    row["create_available"] = create.email_create_available(row)
    # The deleted project this email made or merged into, while it stays
    # deleted (docs/PROJECT_DELETE.md 5): who, when and why.
    from app.services import project_delete

    row["deleted_project"] = project_delete.archive_ref(sb, row.get("create_blocked_archive_id"))


def _detail(sb, email_id: str, role: Role, user: CurrentUser | None = None) -> dict:
    """The full row (explicit column list) plus its sighting mailboxes, its
    authorization rule and the match context, redacted by role.

    `user` additionally enforces the mailbox scope on the row as it is read,
    which is how GET /{email_id} checks visibility before anything else looks
    at the row. The action routes have already checked it and pass only the
    role, so the answer they build costs no extra query.

    Every action answers this, so one read on the frontend renders the drawer
    whether it arrived from a list click or from pressing a button.
    """
    row = _email_row_or_404(sb, email_id, user=user)
    row["mailboxes"] = _mailboxes_by_email(sb, [email_id]).get(email_id, [])
    row["authorization_rule"] = _rule_for(sb, row.get("authorization_rule_id"))
    row["rule"] = row["authorization_rule"]  # the frontend reads the short name
    _attach_match_context(sb, row)
    _attach_harvest_context(sb, row)
    _attach_create_context(sb, row)
    if role not in ACTUAL_BID_VIEWER_ROLES:
        # Drops the `actual` date kind and nulls any date sub-score taken from
        # the actual bid date, in the stored candidates, the match record's
        # breakdown and candidates alike (docs/RFP_MATCHING.md section 8).
        row = _match().redact_candidates(row, role)
    return row


def _conflict(exc: LookupError) -> HTTPException:
    """A human action that lost its race (the row moved on, or was never in
    the state the action belongs to). The service's sentence is app-authored,
    so it is safe to pass through verbatim; a bare LookupError falls back to
    a sentence of our own."""
    return HTTPException(
        status.HTTP_409_CONFLICT,
        str(exc) or "That message is no longer waiting for this decision.",
        headers={"X-Error-Code": ErrorCode.RFP_EMAIL_NOT_ACTIONABLE.value},
    )


def _match_error(exc: LookupError) -> HTTPException:
    """A match action the service refused. `RfpMatchError` (a LookupError
    carrying `.code`, docs/RFP_MATCHING.md section 12) maps to 409 under its
    own code, except `rfp_match_gc_required`, which is a 400: the request is
    missing something the reviewer can supply (pick the GC first), not a race
    lost. A bare LookupError is the ordinary "moved on" 409, under
    rfp_match_not_actionable since it came from a match action."""
    code = getattr(exc, "code", None)
    code = str(code) if code else ErrorCode.RFP_MATCH_NOT_ACTIONABLE.value
    http_status = (
        status.HTTP_400_BAD_REQUEST
        if code == ErrorCode.RFP_MATCH_GC_REQUIRED
        else status.HTTP_409_CONFLICT
    )
    return HTTPException(
        http_status,
        str(exc) or "That message is no longer waiting for this decision.",
        headers={"X-Error-Code": code},
    )


def _already_sent(exc: ProposalSendError) -> HTTPException:
    """The removal helper refused because a proposal to that GC is sent or
    sending (docs/RFP_MATCHING.md 3.7 step b): the GC stays, the match row is
    untouched."""
    return HTTPException(
        status.HTTP_409_CONFLICT,
        str(exc) or "A proposal has already been sent to this GC; it cannot be unmerged.",
        headers={"X-Error-Code": ErrorCode.RFP_MATCH_GC_ALREADY_SENT.value},
    )


# ── Review queue: reads ──────────────────────────────────────────────────


@router.get("", dependencies=[Depends(rfp_emails_rate_limit)])
def list_rfp_emails(
    tab: Tab = "review",
    limit: int = Query(default=_DEFAULT_LIMIT, ge=1, le=_MAX_LIMIT),
    offset: int = Query(default=0, ge=0, le=1_000_000),
    user: CurrentUser = Depends(require_view_queue),
) -> dict:
    """One tab of the queue, newest received first.

    `{items, total, offset, limit}`. Each row carries the columns the list
    renders plus `mailboxes` (every watched mailbox that received the
    message), `match_project` (`{id, name, number}` of the system's best
    candidate, or null) and `resolved_gc` (`{id, name}`, or null), attached
    the way the mailboxes are: one extra query each over the page's ids. The
    body, the attachment listing, the raw auth header and the candidate list
    stay in the detail route.

    Scoped per docs/RFP_EMAIL_VISIBILITY.md: only rows sighted in a mailbox
    the viewer owns, or in a shared mailbox, unless they are a dev wearing the
    IT Admin role, who sees everything and also gets `owners` (the row's
    mailboxes resolved to the people they are mapped onto) for the Recipient
    column. A viewer with nothing in scope gets an empty page without a query.

    `match_score` is null for roles outside ACTUAL_BID_VIEWER_ROLES: it is
    the best candidate's total, which may carry the actual-date bucket, and
    the list row does not select the candidates that would say whether it
    does, so it is withheld unconditionally there (the detail route redacts
    per candidate and keeps a name-only total).
    """
    sb = get_supabase()
    statuses = list(TAB_STATUSES[tab])
    scope = visibility.visible_mailboxes(user)
    if scope is not None and not scope:
        # Nothing mapped onto this person and no shared mailbox configured:
        # answer the empty page rather than sending an overlap that matches
        # nothing (see rfp_email_visibility).
        return {"items": [], "total": 0, "offset": offset, "limit": limit}
    query = (
        sb.table("rfp_emails")
        .select(_LIST_SELECT, count="exact")
        .in_("status", statuses)
    )
    resp = (
        visibility.apply_scope(query, user)
        .order("received_at", desc=True)
        .order("id", desc=True)
        .range(offset, offset + limit - 1)
        .execute()
    )
    rows = resp.data or []
    mailboxes = _mailboxes_by_email(sb, [r["id"] for r in rows if r.get("id")])
    projects = _projects_by_id(sb, {r.get("match_project_id") for r in rows})
    gcs = _gcs_by_id(sb, {r.get("resolved_gc_id") for r in rows})
    harvests = _harvest_status_by_id(sb, {r.get("harvest_id") for r in rows})
    hide_score = user.role not in ACTUAL_BID_VIEWER_ROLES
    # The Recipient column is the dev IT Admin's alone (spec 3.3): for every
    # other viewer the key is absent, not empty, because the rows they can see
    # are by definition their own.
    label_owners = visibility.sees_everything(user)
    owners = (
        _owners_by_mailbox(sb, {m for seen in mailboxes.values() for m in seen})
        if label_owners
        else {}
    )
    for row in rows:
        row["mailboxes"] = mailboxes.get(row.get("id"), [])
        if label_owners:
            row["owners"] = [
                {"mailbox": m, "name": owners.get(m)} for m in row["mailboxes"]
            ]
        row["match_project"] = projects.get(row.get("match_project_id"))
        row["resolved_gc"] = gcs.get(row.get("resolved_gc_id"))
        row["harvest_status"] = (
            "pending" if row.get("status") == "harvest" and not row.get("harvest_id")
            else harvests.get(row.get("harvest_id"))
        )
        if hide_score and "match_score" in row:
            row["match_score"] = None
    return {
        "items": rows,
        "total": resp.count or 0,
        "offset": offset,
        "limit": limit,
    }


@router.get("/counts", dependencies=[Depends(rfp_emails_rate_limit)])
def rfp_email_counts(user: CurrentUser = Depends(require_view_queue)) -> dict:
    """`{review, matches, unauthorized, flagged, processed}` for the nav
    badges and the Estimating Admin's dashboard card. Five count-only reads:
    no row is transferred. Counted inside the viewer's mailbox scope, so a
    badge never advertises a row its owner cannot open
    (docs/RFP_EMAIL_VISIBILITY.md 3.3)."""
    sb = get_supabase()
    scope = visibility.visible_mailboxes(user)
    if scope is not None and not scope:
        return {tab: 0 for tab in TAB_STATUSES}
    counts: dict[str, int] = {}
    for tab, statuses in TAB_STATUSES.items():
        query = (
            sb.table("rfp_emails")
            .select("id", count="exact")
            .in_("status", list(statuses))
        )
        resp = visibility.apply_scope(query, user).limit(1).execute()
        counts[tab] = resp.count or 0
    return counts


@router.get("/match-stats", dependencies=[Depends(rfp_emails_rate_limit)])
def rfp_match_stats(user: CurrentUser = Depends(require_view_queue)) -> dict:
    """`{confident: {pending, agreed, disagreed}, auto_merge_enabled}`: how
    often reviewers agreed with the system on the matches it was confident
    about (docs/RFP_MATCHING.md 3.8), the evidence for turning the auto-merge
    switch on, plus the switch's current position so the Matches tab header
    reads right. Three count-only reads in the service, over the viewer's
    mailboxes only (docs/RFP_EMAIL_VISIBILITY.md 3.3)."""
    stats = dict(
        _ingest().match_stats(get_supabase(), visibility.visible_mailboxes(user)) or {}
    )
    stats["auto_merge_enabled"] = bool(get_settings().rfp_match_auto_merge_enabled)
    return stats


# ── Authorized senders ───────────────────────────────────────────────────
# Registered BEFORE /{email_id} so the literal path is not shadowed by the
# parameterised one (the codebase's literal-before-param ordering rule).


class AuthorizedSenderIn(BaseModel):
    """`kind` and `value` are normalized by the service's `validate_rule`
    before anything is written. `locked` marks a platform rule and is IT
    Admin only. `method` is accepted only alongside `locked` (a platform
    rule names the source it represents: `procore`, or `pipelinesuite` for
    a GC whose plan room is a PipelineSuite portal, granted on that GC's
    own domain, or `smartbid` for ConstructConnect's SmartBid, granted on
    the platform's own domain (smartbidnet.com); `gc_portal` names a GC's
    own bespoke bidding portal, keyed by the rule's domain); every other
    rule is `general`."""

    kind: Literal["address", "domain"]
    value: str = Field(min_length=1, max_length=320)
    locked: bool = False
    method: Literal["procore", "pipelinesuite", "smartbid", "gc_portal", "general"] | None = None


@router.get("/authorized-senders", dependencies=[Depends(rfp_emails_rate_limit)])
def list_authorized_senders(_: CurrentUser = Depends(require_sender_admin)) -> dict:
    """Every rule, locked platform rules first, each with `created_by_name`
    for the settings table's "added by" column (null for the seed)."""
    sb = get_supabase()
    rows = (
        sb.table("rfp_authorized_senders")
        .select(_RULE_SELECT)
        .order("locked", desc=True)
        .order("kind")
        .order("value")
        .execute()
    ).data or []
    names = _profile_names(sb, {r.get("created_by") for r in rows})
    for row in rows:
        row["locked"] = bool(row.get("locked"))
        row["created_by_name"] = names.get(row.get("created_by"))
    return {"items": rows}


@router.post(
    "/authorized-senders",
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(rfp_emails_rate_limit)],
)
def create_authorized_sender(
    body: AuthorizedSenderIn,
    user: CurrentUser = Depends(require_sender_admin),
) -> dict:
    """Add an authorization rule, then re-run the recent unauthorized rows
    the new rule now covers (doc section 3.7, "Learn-back").

    Refusals: 400 when the value does not parse as an address or a bare
    domain (`rfp_rule_invalid`), 400 for a domain rule naming a public
    mailbox provider (`rfp_rule_public_domain`) - gmail.com and its kin can
    never authorize a whole provider, so that GC's colleagues must each be
    added as a contact instead, 403 when a non-IT-Admin asks for `locked`
    (`rfp_rule_locked_it_admin_only`), 409 when the rule already exists.

    The response is the created rule plus `rescanned`, the number of
    flagged-unauthorized messages the learn-back put back in flight.
    """
    if body.locked and user.role != Role.IT_ADMIN:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            ErrorCode.RFP_RULE_LOCKED_IT_ADMIN_ONLY.value,
            headers={"X-Error-Code": ErrorCode.RFP_RULE_LOCKED_IT_ADMIN_ONLY.value},
        )

    auth = _auth()
    try:
        kind, value = auth.validate_rule(body.kind, body.value)
    except ValueError as exc:
        # validate_rule refuses a public mailbox provider itself, with the
        # better sentence (it names the address-rule way out). Keep that
        # sentence and only sharpen the CODE, so the frontend can offer the
        # "switch this to an address rule" shortcut for that one case.
        public = body.kind == "domain" and auth.is_public_mailbox_domain(body.value)
        code = (
            ErrorCode.RFP_RULE_PUBLIC_DOMAIN if public else ErrorCode.RFP_RULE_INVALID
        )
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, str(exc), headers={"X-Error-Code": code.value}
        ) from exc

    # Belt and braces: a free-mail provider is never a company, so authorizing
    # gmail.com would trust every Gmail account on earth. validate_rule refuses
    # this first today; this stays so the rule can never slip through if that
    # check ever moves.
    if kind == "domain" and auth.is_public_mailbox_domain(value):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            (
                f"{value} is a public mailbox provider, so it cannot be trusted as a "
                "whole domain: that would authorize every account there. Add the "
                "exact email address instead, one rule per person."
            ),
            headers={"X-Error-Code": ErrorCode.RFP_RULE_PUBLIC_DOMAIN.value},
        )

    # Non-locked rules are always 'general' (doc section 3.8, precedence 3);
    # only a locked platform rule names the source it stands for.
    method = (body.method or "general") if body.locked else "general"
    payload = {
        "kind": kind,
        "value": value,
        "method": method,
        "locked": bool(body.locked),
        "created_by": user.id,
    }
    sb = get_supabase()
    try:
        rows = sb.table("rfp_authorized_senders").insert(payload).execute().data or []
    except Exception as exc:  # noqa: BLE001 - the unique index is the duplicate check
        if "23505" in str(exc) or "duplicate key" in str(exc).lower():
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                f"{value} is already an authorized {kind} rule.",
                headers={"X-Error-Code": ErrorCode.RFP_RULE_DUPLICATE.value},
            ) from exc
        raise
    rule = rows[0] if rows else payload
    rule["locked"] = bool(rule.get("locked"))

    # Learn-back. Never allowed to fail the write that already happened: the
    # rule is live either way and the sweep would catch up on its own.
    rescanned = 0
    try:
        rescanned = int(_ingest().rescan_after_rule_added(sb, rule) or 0)
    except Exception:  # noqa: BLE001
        logger.exception("rfp emails: rescan after rule %s failed", rule.get("id"))

    audit(
        user.id,
        "rfp_authorized_sender.create",
        _AUDIT_RULE,
        rule.get("id"),
        {
            "kind": kind,
            "value": value,
            "method": method,
            "locked": bool(body.locked),
            "rescanned": rescanned,
        },
    )
    return {"item": rule, "rescanned": rescanned}


@router.delete(
    "/authorized-senders/{rule_id}", dependencies=[Depends(rfp_emails_rate_limit)]
)
def delete_authorized_sender(
    rule_id: str, user: CurrentUser = Depends(require_sender_admin)
) -> dict:
    """Remove a rule. A locked platform rule is IT Admin only, enforced here
    rather than only in the UI (doc section 9)."""
    _uuid_or_404(rule_id, _RULE_NOT_FOUND)
    sb = get_supabase()
    rows = (
        sb.table("rfp_authorized_senders")
        .select(_RULE_SELECT)
        .eq("id", rule_id)
        .limit(1)
        .execute()
    ).data or []
    if not rows:
        raise HTTPException(status.HTTP_404_NOT_FOUND, _RULE_NOT_FOUND)
    rule = rows[0]
    if rule.get("locked") and user.role != Role.IT_ADMIN:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            ErrorCode.RFP_RULE_LOCKED_IT_ADMIN_ONLY.value,
            headers={"X-Error-Code": ErrorCode.RFP_RULE_LOCKED_IT_ADMIN_ONLY.value},
        )
    sb.table("rfp_authorized_senders").delete().eq("id", rule_id).execute()
    audit(
        user.id,
        "rfp_authorized_sender.delete",
        _AUDIT_RULE,
        rule_id,
        {
            "kind": rule.get("kind"),
            "value": rule.get("value"),
            "locked": bool(rule.get("locked")),
        },
    )
    return {"id": rule_id, "deleted": True}


# ── Blocked senders (doc 3.1) ────────────────────────────────────────────
# The mirror image of the rules above: a block stops the mail at the delta
# listing, before a row is written and before any model is called. The four
# platform domains (BuildingConnected, NGEM/IonWave, PlanHub) are seeded as
# LOCKED rows by 0133 and only an IT Admin or a dev can remove one; every
# other row is removable by any of BLOCK_ADMIN_ROLES.


class BlockedSenderIn(BaseModel):
    """`kind` is what the blocker chose: `address` blocks that one person,
    `domain` blocks everyone at the company. Both are normalized and
    validated by `rfp_email_auth.validate_block` before anything is written.
    `reason` is the optional note shown in the list."""

    kind: Literal["address", "domain"]
    value: str = Field(min_length=1, max_length=320)
    reason: str | None = Field(default=None, max_length=_BLOCK_REASON_MAX)


class BlockSenderIn(BaseModel):
    """The review queue's Block sender. `confirm` must equal the value being
    blocked, typed by hand: the whole point of the dialog is that a block is
    not something a misclick can do."""

    kind: Literal["address", "domain"] = "address"
    confirm: str = Field(min_length=1, max_length=320)
    reason: str | None = Field(default=None, max_length=_BLOCK_REASON_MAX)


def _block_row_or_404(sb, block_id: str) -> dict:
    _uuid_or_404(block_id, _BLOCK_NOT_FOUND)
    rows = (
        sb.table("rfp_blocked_senders")
        .select(_BLOCK_SELECT)
        .eq("id", block_id)
        .limit(1)
        .execute()
    ).data or []
    if not rows:
        raise HTTPException(status.HTTP_404_NOT_FOUND, _BLOCK_NOT_FOUND)
    return rows[0]


def _validated_block(kind: str, value: str) -> tuple[str, str]:
    """`validate_block`'s refusal as a 400 carrying its sentence, with the
    public-provider case sharpened into its own code so the frontend can
    offer the "block just this address instead" shortcut."""
    auth = _auth()
    try:
        return auth.validate_block(kind, value)
    except ValueError as exc:
        public = kind == "domain" and auth.is_public_mailbox_domain(value)
        code = ErrorCode.RFP_BLOCK_PUBLIC_DOMAIN if public else ErrorCode.RFP_BLOCK_INVALID
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, str(exc), headers={"X-Error-Code": code.value}
        ) from exc


def _refuse_internal_domain(kind: str, value: str) -> None:
    """Our own domain is already skipped at the listing (doc 3.1) and
    blocking it would read as "G3 is blocked", which is never what the
    blocker meant. Addresses are left alone: blocking one internal address
    is harmless and costs nothing to allow."""
    if kind != "domain":
        return
    internal = {d.strip().lower() for d in get_settings().rfp_email_ingestion_internal_domain_set}
    if value in internal:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            (
                f"{value} is our own domain. Mail from it is already ignored by the "
                "intake, so there is nothing to block."
            ),
            headers={"X-Error-Code": ErrorCode.RFP_BLOCK_INTERNAL_DOMAIN.value},
        )


def _block_payload(sb, block: dict) -> dict:
    block["locked"] = bool(block.get("locked"))
    names = _profile_names(sb, {block.get("created_by")})
    block["created_by_name"] = names.get(block.get("created_by"))
    return block


@router.get("/blocked-senders", dependencies=[Depends(rfp_emails_rate_limit)])
def list_blocked_senders(_: CurrentUser = Depends(require_block_admin)) -> dict:
    """Every block, locked platform rows first, each with `created_by_name`
    for the "blocked by" column (null for the 0133 seed).

    `env_domains` is whatever RFP_EMAIL_INGESTION_BLOCKED_DOMAINS still
    pins in this deployment's environment. Those are listed separately and
    read-only: the page cannot remove them, and saying so is better than an
    Unblock button that silently does nothing.
    """
    sb = get_supabase()
    rows = (
        sb.table("rfp_blocked_senders")
        .select(_BLOCK_SELECT)
        .order("locked", desc=True)
        .order("kind")
        .order("value")
        .execute()
    ).data or []
    names = _profile_names(sb, {r.get("created_by") for r in rows})
    for row in rows:
        row["locked"] = bool(row.get("locked"))
        row["created_by_name"] = names.get(row.get("created_by"))
    return {
        "items": rows,
        "env_domains": sorted(get_settings().rfp_email_ingestion_blocked_domain_set),
    }


@router.post(
    "/blocked-senders",
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(rfp_emails_rate_limit)],
)
def create_blocked_sender(
    body: BlockedSenderIn,
    user: CurrentUser = Depends(require_block_admin),
) -> dict:
    """Block a sender, then park everything from them still in flight.

    Refusals: 400 when the value does not parse (`rfp_block_invalid`), 400
    for a domain block naming a public mailbox provider
    (`rfp_block_public_domain`) - blocking gmail.com would silently drop
    every GC contact who uses Gmail - 400 for our own domain
    (`rfp_block_internal_domain`), 409 when it is already blocked
    (`rfp_block_duplicate`).

    The response is the created row plus `parked`, how many queued messages
    from that sender were ended at `blocked_sender`.
    """
    kind, value = _validated_block(body.kind, body.value)
    _refuse_internal_domain(kind, value)
    sb = get_supabase()
    ingest = _ingest()
    try:
        block, parked = ingest.add_block(
            sb, kind=kind, value=value, reason=body.reason, actor_id=user.id
        )
    except ingest.BlockDuplicate as exc:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            str(exc),
            headers={"X-Error-Code": ErrorCode.RFP_BLOCK_DUPLICATE.value},
        ) from exc
    return {"item": _block_payload(sb, block), "parked": parked}


@router.delete(
    "/blocked-senders/{block_id}", dependencies=[Depends(rfp_emails_rate_limit)]
)
def delete_blocked_sender(
    block_id: str, user: CurrentUser = Depends(require_block_admin)
) -> dict:
    """Unblock a sender. Mail from them flows again from the next poll; rows
    already parked at `blocked_sender` stay parked. A locked platform row is
    IT Admin (or dev) only, enforced here and not only in the UI."""
    sb = get_supabase()
    block = _block_row_or_404(sb, block_id)
    if block.get("locked") and not _may_unlock(user):
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            ErrorCode.RFP_BLOCK_LOCKED_IT_ADMIN_ONLY.value,
            headers={"X-Error-Code": ErrorCode.RFP_BLOCK_LOCKED_IT_ADMIN_ONLY.value},
        )
    _ingest().remove_block(sb, block, user.id)
    return {"id": block_id, "deleted": True}


# ── Review queue: detail and actions ─────────────────────────────────────


@router.get("/harvest-status", dependencies=[Depends(rfp_emails_rate_limit)])
def rfp_harvest_status(_: CurrentUser = Depends(require_sender_admin)) -> dict:
    """The settings tab's harvester blocks: Procore (configured, the account
    but never the password, last login, lock state, last error, active jobs)
    plus a `pipelinesuite` block listing every portal session (host, key
    fingerprint, last login, failures, lock, last error; never the key) and
    a `smartbid` block for the one SmartBid login (enabled, last login,
    last use, failures, lock, last error; no key, no token). Never the
    cookies (docs/RFP_HARVEST.md section 5, RFP_PIPELINESUITE.md section 4,
    RFP_SMARTBID.md section 4)."""
    return _harvest().session_status()


@router.get("/{email_id}", dependencies=[Depends(rfp_emails_rate_limit)])
def get_rfp_email(
    email_id: str, user: CurrentUser = Depends(require_view_queue)
) -> dict:
    """The whole row (plain-text body, attachment names/types/sizes, auth
    verdicts, model verdict, decisions, extracted facts, match decision) plus
    `mailboxes`, `authorization_rule`, `candidates`, `match`,
    `excluded_projects` and `possible_rebid`. No HTML and no attachment bytes
    exist to return; no date value from any candidate project does either.

    404s with the unknown-id body for a row outside the viewer's mailbox
    scope, checked before the row is read for anything else."""
    _uuid_or_404(email_id, _EMAIL_NOT_FOUND)
    return _detail(get_supabase(), email_id, user.role, user=user)


class HarvestIn(BaseModel):
    """`force` refreshes a complete harvest instead of reusing it."""

    force: bool = False


@router.post(
    "/{email_id}/harvest",
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[Depends(rfp_emails_rate_limit)],
)
def harvest_rfp_email(
    email_id: str,
    body: HarvestIn | None = None,
    user: CurrentUser = Depends(require_review_queue),
) -> dict:
    """Run (or re-run, with `force`) the project data harvest for this email
    by hand: 202 `{job}`. 409 `rfp_harvest_active` when a job is already
    queued or running; 409 `rfp_harvest_not_available` when the method has
    no harvester, the body has no platform link, or the status is not one a
    harvest may run from; 503 `rfp_harvest_locked` while the logins for the
    row's own platform are locked (or not configured): Procore's one
    session, the PipelineSuite portal named in the email's body
    (docs/RFP_PIPELINESUITE.md section 7), or SmartBid's one session
    (docs/RFP_SMARTBID.md section 7). Audited `rfp_harvest.run`."""
    _uuid_or_404(email_id, _EMAIL_NOT_FOUND)
    sb = get_supabase()
    harvest = _harvest()
    row = _email_row_or_404(
        sb, email_id,
        "id, status, invitation_method, body_text, harvest_id, attachments_meta, from_address, "
        "primary_mailbox",
        user=user,
    )
    ok, reason = harvest.can_harvest(row)
    if ok and row.get("status") not in harvest.MANUAL_STATUSES:
        ok, reason = False, "The email is not at a stage that can be harvested."
    if not ok:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            reason,
            headers={"X-Error-Code": ErrorCode.RFP_HARVEST_NOT_AVAILABLE.value},
        )
    # The lock is per platform session: Procore has one, PipelineSuite one
    # per portal host, SmartBid one, so the service reads the row to know
    # which to check.
    usable, why, until = harvest.availability_for(row)
    if not usable:
        detail = why or "The platform is not available."
        if until:
            detail = f"{detail} Locked until {until.isoformat()}."
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            detail,
            headers={"X-Error-Code": ErrorCode.RFP_HARVEST_LOCKED.value},
        )
    force = bool(body.force) if body else False
    try:
        from app.services import llm_queue

        job = harvest.enqueue(email_id, created_by=user.id, force=force)
    except llm_queue.JobAlreadyActive as exc:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "A harvest for this email is already queued or running.",
            headers={"X-Error-Code": ErrorCode.RFP_HARVEST_ACTIVE.value},
        ) from exc
    audit(user.id, "rfp_harvest.run", "rfp_email", email_id, {"force": force})
    return {"job": {"id": job.get("id"), "status": job.get("status")}}


# ── Project creation (docs/RFP_CREATE.md 8) ──────────────────────────────

# X-Error-Code values of the two refusals; not in app/core/error_codes yet
# (that module is shared with the numbering build), so they are named here.
CODE_CREATE_REFUSED = "rfp_create_refused"
CODE_CREATE_IN_PROGRESS = "rfp_create_in_progress"


def _create_refused(exc: Exception, code: str) -> HTTPException:
    return HTTPException(
        status.HTTP_409_CONFLICT,
        str(exc) or "A project cannot be created from this email right now.",
        headers={"X-Error-Code": code},
    )


@router.post("/{email_id}/create", dependencies=[Depends(rfp_emails_rate_limit)])
def create_rfp_email_project(
    email_id: str, user: CurrentUser = Depends(require_review_queue)
) -> dict:
    """"Create project": a bidding project from a `done` row, parked in
    Go/No-Go with its GC, name, bid date and harvested documents
    (docs/RFP_CREATE.md 4). `{project_id, number, linked, why, duplicate_of}`.
    `linked` is true when the project already existed and the row joined it,
    and `why` says which rule joined it: null when the row's harvest already
    carried the project, `sibling` when another copy of the same message made
    it (`duplicate_of` is that copy's email id, 4.6). The recent-project half
    of the 4.6 guard never links on this path: it refuses with a sentence
    naming the project, so a person decides. 409 `rfp_create_refused` with
    the service's sentence (no name, wrong status, a copy of the same
    message, already created, a project with this name made minutes ago),
    409 `rfp_create_in_progress` while another worker holds the harvest's
    claim. Audited `rfp_email.create` by the service."""
    _uuid_or_404(email_id, _EMAIL_NOT_FOUND)
    sb = get_supabase()
    _email_exists_or_404(sb, email_id, user)
    create = _create()
    try:
        made = _ingest().create_project(sb, email_id, user.id)
    except create.CreateInProgress as exc:
        raise _create_refused(exc, CODE_CREATE_IN_PROGRESS) from exc
    except create.CreateRefused as exc:
        raise _create_refused(exc, CODE_CREATE_REFUSED) from exc
    return {"project_id": made.project_id, "number": made.number, "linked": bool(made.linked),
            "why": made.why, "duplicate_of": made.duplicate_of}


class SetNameIn(BaseModel):
    """The project name for a nameless `done` row; the service strips
    control characters, collapses whitespace and caps it at 200."""

    name: str = Field(min_length=1, max_length=400)


@router.post("/{email_id}/set-name", dependencies=[Depends(rfp_emails_rate_limit)])
def set_rfp_email_project_name(
    email_id: str,
    body: SetNameIn,
    user: CurrentUser = Depends(require_review_queue),
) -> dict:
    """"Set project name" (docs/RFP_CREATE.md 8): writes the name (stamped
    as a person's, `extract_model = human`) and sends the row back through
    `match`, so it harvests and creates like any other. Allowed on `done`
    rows without a project that are not sibling followers; 409 otherwise,
    and 409 for a name that is only generic words or a reference number
    (the matcher's own emptiness rule); 400 when nothing usable is left of
    the name. Audited `rfp_email.set_name` with the old and the new name.
    Answers the detail."""
    _uuid_or_404(email_id, _EMAIL_NOT_FOUND)
    sb = get_supabase()
    _email_exists_or_404(sb, email_id, user)
    try:
        _ingest().set_project_name(sb, email_id, body.name, user.id)
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    except LookupError as exc:
        raise _conflict(exc) from exc
    return _detail(sb, email_id, user.role)


class ReviewIn(BaseModel):
    """The human verdict on a message the model was not confident about."""

    decision: Literal["yes", "no"]


@router.post("/{email_id}/review", dependencies=[Depends(rfp_emails_rate_limit)])
def review_rfp_email(
    email_id: str,
    body: ReviewIn,
    user: CurrentUser = Depends(require_review_queue),
) -> dict:
    """"Yes, this is an RFP" sends the row on to the authorize step; "No"
    ends it at rejected_by_review. Both are conditional on the row still
    sitting at review_llm, so a second reviewer (or the sweep) gets a 409
    rather than overwriting the first decision."""
    _uuid_or_404(email_id, _EMAIL_NOT_FOUND)
    sb = get_supabase()
    _email_exists_or_404(sb, email_id, user)
    try:
        _ingest().review(sb, email_id, body.decision, user.id)
    except LookupError as exc:
        raise _conflict(exc) from exc
    return _detail(sb, email_id, user.role)


@router.post("/{email_id}/continue", dependencies=[Depends(rfp_emails_rate_limit)])
def continue_rfp_email(
    email_id: str, user: CurrentUser = Depends(require_review_queue)
) -> dict:
    """Override an unauthorized sender for this one message: the row goes on
    to the extract step as `nonorganic` and records who continued it. 409
    once the row has left flagged_unauthorized."""
    _uuid_or_404(email_id, _EMAIL_NOT_FOUND)
    sb = get_supabase()
    _email_exists_or_404(sb, email_id, user)
    try:
        _ingest().continue_unauthorized(sb, email_id, user.id)
    except LookupError as exc:
        raise _conflict(exc) from exc
    return _detail(sb, email_id, user.role)


@router.post("/{email_id}/dismiss", dependencies=[Depends(rfp_emails_rate_limit)])
def dismiss_rfp_email(
    email_id: str, user: CurrentUser = Depends(require_review_queue)
) -> dict:
    """Dismiss a message waiting at review_llm or flagged_unauthorized:
    terminal at rejected_by_review. 409 once the row has left those states,
    including a row at review_match, whose only rejection exit is
    match/reject."""
    _uuid_or_404(email_id, _EMAIL_NOT_FOUND)
    sb = get_supabase()
    _email_exists_or_404(sb, email_id, user)
    try:
        _ingest().dismiss(sb, email_id, user.id)
    except LookupError as exc:
        raise _conflict(exc) from exc
    return _detail(sb, email_id, user.role)


@router.post("/{email_id}/block-sender", dependencies=[Depends(rfp_emails_rate_limit)])
def block_rfp_email_sender(
    email_id: str,
    body: BlockSenderIn,
    user: CurrentUser = Depends(require_block_admin),
) -> dict:
    """Block the open message's sender from the review queue (doc 3.1).

    `kind` says what the blocker chose: `address` is that one person,
    `domain` is everyone at their company. The value blocked is taken from
    the STORED `from_address`, never from the request, so the request cannot
    aim the block at a third party; `confirm` is what the blocker typed and
    must equal that value exactly (case and surrounding space ignored),
    which is the whole point of the dialog. A mismatch is a 400
    `rfp_block_confirm_mismatch` and nothing is written.

    The open message is parked with every other in-flight message from that
    sender, so the response is the fresh detail plus the block and `parked`.
    """
    _uuid_or_404(email_id, _EMAIL_NOT_FOUND)
    sb = get_supabase()
    row = _email_row_or_404(sb, email_id, "id, from_address", user=user)
    address = (row.get("from_address") or "").strip().lower()
    if not address:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "This message has no sender address to block.",
            headers={"X-Error-Code": ErrorCode.RFP_BLOCK_INVALID.value},
        )
    target = address if body.kind == "address" else _auth().address_domain(address)
    kind, value = _validated_block(body.kind, target)
    _refuse_internal_domain(kind, value)
    if body.confirm.strip().lower() != value:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            (
                f"That did not match. Type {value} exactly to confirm the block."
            ),
            headers={"X-Error-Code": ErrorCode.RFP_BLOCK_CONFIRM_MISMATCH.value},
        )
    ingest = _ingest()
    try:
        block, parked = ingest.block_email_sender(
            sb, email_id, kind=kind, value=value, reason=body.reason, actor_id=user.id
        )
    except ingest.BlockDuplicate as exc:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            str(exc),
            headers={"X-Error-Code": ErrorCode.RFP_BLOCK_DUPLICATE.value},
        ) from exc
    return {
        "item": _block_payload(sb, block),
        "parked": parked,
        "email": _detail(sb, email_id, user.role),
    }


class MethodPatchIn(BaseModel):
    """Correct the invitation method the pipeline picked. The later harvest
    step keys off it, so any review-queue role may fix it."""

    invitation_method: Literal[
        "organic",
        "procore",
        "pipelinesuite",
        "smartbid",
        "gc_portal",
        "general",
        "nonorganic",
    ]


@router.patch("/{email_id}", dependencies=[Depends(rfp_emails_rate_limit)])
def patch_rfp_email(
    email_id: str,
    body: MethodPatchIn,
    user: CurrentUser = Depends(require_review_queue),
) -> dict:
    """Set `invitation_method` on one message (doc section 3.8, last line).
    Succeeds on any row the method step has already passed, extract and
    match included."""
    _uuid_or_404(email_id, _EMAIL_NOT_FOUND)
    sb = get_supabase()
    _email_exists_or_404(sb, email_id, user)
    try:
        _ingest().set_method(sb, email_id, body.invitation_method, user.id)
    except LookupError as exc:
        raise _conflict(exc) from exc
    return _detail(sb, email_id, user.role)


# ── Match actions (docs/RFP_MATCHING.md 3.8) ─────────────────────────────
# Each pre-reads the row's existence (404), refuses a malformed body id and
# an unknown GC (404, before the service touches anything), hands the
# decision to the service (which pre-reads the status, writes conditionally
# and audits once) and answers the detail shape. A refusal is an
# RfpMatchError carrying its own code: 409, or 400 for
# rfp_match_gc_required; the removal helper's ProposalSendError is 409
# rfp_match_gc_already_sent, as on unmerge.


@router.post("/{email_id}/match/merge", dependencies=[Depends(rfp_emails_rate_limit)])
def merge_rfp_email_match(
    email_id: str,
    body: RfpMatchMergeIn,
    user: CurrentUser = Depends(require_review_queue),
) -> dict:
    """"Merge into X": add the email's GC to project X and end the row at
    `merged`. X may be any open project in the candidate window, not only a
    listed candidate (scored live then). Refused when X is excluded (an
    earlier unmerge took this email away from it), closed, or when no GC is
    resolved and none is given."""
    _uuid_or_404(email_id, _EMAIL_NOT_FOUND)
    _uuid_or_404(body.project_id, _PROJECT_NOT_FOUND)
    sb = get_supabase()
    _email_exists_or_404(sb, email_id, user)
    if body.gc_id is not None:
        _gc_exists_or_404(sb, body.gc_id)
    try:
        _ingest().merge_email(sb, email_id, body.project_id, body.gc_id, user.id)
    except ProposalSendError as exc:
        raise _already_sent(exc) from exc
    except LookupError as exc:
        raise _match_error(exc) from exc
    return _detail(sb, email_id, user.role)


@router.post(
    "/{email_id}/match/duplicate", dependencies=[Depends(rfp_emails_rate_limit)]
)
def duplicate_rfp_email_match(
    email_id: str,
    body: RfpMatchDuplicateIn,
    user: CurrentUser = Depends(require_review_queue),
) -> dict:
    """"Already on X": the resolved GC is on project X, so nothing is added;
    the row ends at `duplicate` with a match record linking it to X. Requires
    a resolved GC (400 otherwise: set it first) that is actually on X (409)."""
    _uuid_or_404(email_id, _EMAIL_NOT_FOUND)
    _uuid_or_404(body.project_id, _PROJECT_NOT_FOUND)
    sb = get_supabase()
    _email_exists_or_404(sb, email_id, user)
    try:
        _ingest().duplicate_email(sb, email_id, body.project_id, user.id)
    except ProposalSendError as exc:
        raise _already_sent(exc) from exc
    except LookupError as exc:
        raise _match_error(exc) from exc
    return _detail(sb, email_id, user.role)


@router.post("/{email_id}/match/reject", dependencies=[Depends(rfp_emails_rate_limit)])
def reject_rfp_email_match(
    email_id: str, user: CurrentUser = Depends(require_review_queue)
) -> dict:
    """"Not a match": the row ends at `done` with the candidates kept for
    training. The only rejection exit from review_match."""
    _uuid_or_404(email_id, _EMAIL_NOT_FOUND)
    sb = get_supabase()
    _email_exists_or_404(sb, email_id, user)
    try:
        _ingest().reject_match(sb, email_id, user.id)
    except LookupError as exc:
        raise _match_error(exc) from exc
    return _detail(sb, email_id, user.role)


@router.post("/{email_id}/match/gc", dependencies=[Depends(rfp_emails_rate_limit)])
def set_rfp_email_match_gc(
    email_id: str,
    body: RfpMatchGcIn,
    user: CurrentUser = Depends(require_review_queue),
) -> dict:
    """Pick the email's GC by hand (kind `human`); the row stays at
    review_match. The GC may have just been created through POST /gcs."""
    _uuid_or_404(email_id, _EMAIL_NOT_FOUND)
    sb = get_supabase()
    _email_exists_or_404(sb, email_id, user)
    _gc_exists_or_404(sb, body.gc_id)
    try:
        _ingest().set_match_gc(sb, email_id, body.gc_id, user.id)
    except LookupError as exc:
        raise _match_error(exc) from exc
    return _detail(sb, email_id, user.role)


@router.post("/{email_id}/match/reopen", dependencies=[Depends(rfp_emails_rate_limit)])
def reopen_rfp_email_match(
    email_id: str,
    body: RfpMatchReopenIn | None = None,
    user: CurrentUser = Depends(require_review_queue),
) -> dict:
    """Return a `done` or `duplicate` row to review_match (a wrong duplicate
    is reversed here; a duplicate's open match record is closed as
    `reopened`). Nothing on any project is touched."""
    _uuid_or_404(email_id, _EMAIL_NOT_FOUND)
    sb = get_supabase()
    _email_exists_or_404(sb, email_id, user)
    reason = body.reason if body else None
    try:
        _ingest().reopen_match(sb, email_id, reason, user.id)
    except LookupError as exc:
        raise _match_error(exc) from exc
    return _detail(sb, email_id, user.role)


@router.post("/{email_id}/match/unmerge", dependencies=[Depends(rfp_emails_rate_limit)])
def unmerge_rfp_email_match(
    email_id: str,
    body: RfpMatchUnmergeIn,
    user: CurrentUser = Depends(require_unmerge),
) -> dict:
    """Unmerge by email: resolve the email's latest `merged` match record
    (open or closed, so a crashed unmerge can be finished) and reverse it
    through the service (docs/RFP_MATCHING.md 3.7). 404 when the email was
    never merged; 409 rfp_match_gc_already_sent once a proposal to that GC is
    sent or sending."""
    _uuid_or_404(email_id, _EMAIL_NOT_FOUND)
    sb = get_supabase()
    _email_exists_or_404(sb, email_id, user)
    rows = (
        sb.table("rfp_project_matches")
        .select("id")
        .eq("rfp_email_id", email_id)
        .eq("kind", "merged")
        .order("decided_at", desc=True)
        .limit(1)
        .execute()
    ).data or []
    if not rows:
        raise HTTPException(status.HTTP_404_NOT_FOUND, _NO_MERGE_TO_UNDO)
    try:
        _ingest().unmerge(sb, rows[0]["id"], body.reason, user.id)
    except ProposalSendError as exc:
        raise _already_sent(exc) from exc
    except LookupError as exc:
        raise _match_error(exc) from exc
    return _detail(sb, email_id, user.role)
