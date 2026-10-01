"""RFP Ingestion: project creation (docs/RFP_CREATE.md sections 3, 4 and 7).

After the pipeline has classified, matched and harvested an invitation and
found no existing project, this module turns what it collected into a
bidding project: name, actual bid date, address, bidding link, bid notes,
invitation date, the GC and its contact, `is_ngem` for a portal invitation,
parked in Go/No-Go (`intake` advanced with the `review` entry action, never
`score`: an empty rubric would be an automatic No-Go). The verified
documents follow through the `rfp_create_files` queue job
(services/rfp_create_files). The intake fields nobody could fill (the
internal bid date, the two due dates, the nine rubric answers) stay null
and become the Estimating Admin's task.

Two callers: the pipeline's `create` step (`automatic=True`, actor None,
the row at `create`) and the "Create project" button (`automatic=False`,
the clicking user, the row at `done`). Both run the same `_create`.

Idempotency is per harvest, not per name (the matcher is the duplicate
guard, a user decision): `rfp_harvests.project_id` is set once, and every
later email or invitation sharing the harvest links to that project instead
of creating another. Two workers on two rows of the same harvest are
fenced by `create_claim_token` on the harvest row; a row with no harvest
is fenced by its own status CAS at the end of creation, and the loser of
either race deletes the project it just inserted (the cascades clean the
children) and audits `rfp_create.lost_race`.

Order of writes (4.5): the project insert with an app-assigned number
(services/project_numbers), the `rfp_created_projects` record (right
behind the project, so the page can always see what exists), the GC
links, the stage event and category seed. Those four are the atomic core,
compensated as routers/projects.create_project does (delete the project,
cascades clean the rest; a GC and contact this call inserted are taken
back too). Everything after (the Go/No-Go advance, the source row CAS,
the harvest link and the mates, the files job, the bells, the audit row,
the learn-back rescan) is best effort: a failure is logged and recorded
on `rfp_created_projects.last_error`, and never deletes the project. The
one exception is the source row CAS, whose miss IS the lost race above.

BuildingConnected rows (docs/RFP_BUILDINGCONNECTED.md 3.7, contract 3.5)
take the same `_create` through `create_from_portal`'s second branch: the
facts come off the mirrored opportunity (`facts_for_bc`), the GC off the
invitation's resolution (`gc_plan_for_bc`: resolved, or `provisional` with
`projects.gc_confirm_pending` set), the lead becomes the bid contact inside
the atomic core (`ensure_lead_contact`; a confirmed GC only, never a
provisional one), the number carries the B suffix for
a BUDGET request, and the files flag is raised because the platform hands
over no file. The project side of a BuildingConnected row lives here too:
`apply_project_dates` (a moved due date applied with the PATCH's semantics)
and `swap_project_gc` (the GC confirmation on the project page).

`project_numbers` and `project_intake` are imported inside the functions
that use them, so this module imports on its own.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import uuid
from dataclasses import dataclass, replace
from datetime import datetime, time, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from app.core.config import Settings, get_settings
from app.core.roles import Role
from app.services import (
    directory,
    gono,
    llm_queue,
    notification_email,
    rfp_match,
    rfp_split,
    rfp_test,
    workflow,
)
from app.services.notifications import audit, dismiss_notifications, notify_role
from app.services.rfp_email_auth import (
    METHOD_NONORGANIC,
    address_domain,
    is_public_mailbox_domain,
)

logger = logging.getLogger(__name__)

TABLE = "rfp_created_projects"
EMAILS_TABLE = "rfp_emails"
INVITATIONS_TABLE = "rfp_portal_invitations"
HARVESTS_TABLE = "rfp_harvests"

SOURCE_EMAIL = "rfp_email"
SOURCE_PORTAL = "rfp_portal"

# The two pipeline statuses this slice adds on both source tables.
STATUS_CREATE = "create"
STATUS_CREATED = "created"
STATUS_DONE = "done"
# The statuses a creation (or a link to an existing project) starts from.
STATUS_SPLIT = "split"
CREATABLE_STATUSES = (STATUS_CREATE, STATUS_DONE)
# Rows sharing a harvest that the leader's creation links (`created`): the
# creatable ones and a mate still waiting on the split step.
MATE_STATUSES = (STATUS_CREATE, STATUS_DONE, STATUS_SPLIT)
FLAG_SIBLING = "sibling"

GC_RESOLVED = "resolved"
GC_LIKELY = "likely"
GC_CREATED = "created"
GC_NONE = "none"
# BuildingConnected (docs/RFP_BUILDINGCONNECTED.md 3.7, contract D15): the
# most similar GC when no alias confirms the platform's company. Links the
# GC like `resolved` but WITHOUT the lead contact (a guess must never file
# the lead as that GC's contact: gc_aliases would then resolve the company
# to it as confirmed), and sets `projects.gc_confirm_pending` so the
# project page asks "same GC?". The answer (rfp_bc_portal.confirm_gc and
# swap_project_gc) attaches the lead.
GC_PROVISIONAL = "provisional"

# The portal key of the BuildingConnected slice (rfp_portal_invitations.portal
# and rfp_created_projects.invitation_method for its rows, contract D14).
PORTAL_BC = "buildingconnected"
# The invitation gc_kind values that count as a confirmed GC (gc_aliases.CONFIRMED_KINDS).
_BC_CONFIRMED_KINDS = ("alias", "contact", "domain")
_BC_GC_KIND_PROVISIONAL = "provisional"
_BC_GC_KIND_NONE = "none"
FLAG_NDA_REQUIRED = "nda_required"

# The project date columns apply_project_dates may write (contract D20);
# internal_bid_at is never among them (the team-wide deadline is a person's).
APPLY_DATE_FIELDS = ("actual_bid_at", "job_walk_at", "est_start_date", "est_finish_date")
_APPLY_DATE_ONLY_FIELDS = ("est_start_date", "est_finish_date")
AUDIT_APPLY_DATES_ACTION = "project.apply_bc_dates"
AUDIT_GC_SWAP_ACTION = "project.gc_swap"

FILES_NONE = "none"
# The `rfp_created_projects.files_status` values a fresh promotion job may be
# enqueued from (rfp_create_files.RETRYABLE_STATUSES plus a finished run):
# `pending` and `running` belong to a job that is already in the queue.
_FILES_ENQUEUEABLE = (FILES_NONE, "complete", "failed")

NOTIFY_CREATED = "rfp_create.created"
NOTIFY_INTAKE_NEEDED = "rfp_create.intake_needed"
JOB_FILES = "rfp_create_files"
AUDIT_ACTION = "rfp_create.create"
AUDIT_LINK_ACTION = "rfp_create.link"
CONTINUE_AUDIT_ACTION = "rfp_email.continue"
# `rfp_emails.extract_model` when a person typed the name ("Set project
# name"): the facts then prefer it over anything a harvest found.
EXTRACT_MODEL_HUMAN = "human"

# How many gc_contacts rows the likely-GC domain lookup reads at most.
_DOMAIN_CONTACTS_CAP = 500

# Harvest file entries that put bytes into the sandbox (a promotion job is
# worth enqueuing only when at least one exists).
_ENTRY_STATUSES_WITH_FILES = ("accepted", "reused")
# Email-harvester link statuses whose URL is a bidding link a person can open.
_LINK_STATUSES = ("listed", "needs_sign_in")

# How deep a `sibling_of_email_id` chain is walked when linking followers.
# Matches rfp_email_ingest._SIBLING_CHAIN_MAX_HOPS.
_SIBLING_CHAIN_MAX_HOPS = 8

_NAME_MAX_CHARS = 200
_SUBJECT_MAX_CHARS = 200
_ERROR_MAX_CHARS = 500
_NOTES_JOIN = "\n\n"
_DATE_ONLY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_IN_CHUNK = 200

# Company wall clock (docs: Pacific time everywhere). A date-only bid due
# date is stored at midnight Pacific; needs_by is the actual date's Pacific day.
_COMPANY_TZ = ZoneInfo("America/Los_Angeles")

_MSG_WRONG_STATUS = "This invitation is not at a stage a project can be created from."
_MSG_SIBLING = (
    "This email is another copy of the same message; the project is created from the copy "
    "that leads the group."
)
_MSG_NO_NAME = "This invitation has no project name; set one first."
_MSG_ALREADY_CREATED = "A project was already created from this invitation."
_MSG_IN_PROGRESS = "Another worker is creating the project for this invitation; try again shortly."
_MSG_LOST_RACE = "Someone else decided this invitation first."
_MSG_NO_NUMBER = "No free project number could be assigned; try again."
_MSG_FILES_BUSY = (
    "This invitation's documents are waiting: the project is already promoting another "
    "invitation's. Use Retry documents once that finishes."
)
_MSG_NDA_REQUIRED = (
    "BuildingConnected has not shared this invitation's details yet (an NDA is required); "
    "a project cannot be created until it does."
)
_MSG_NO_GC = (
    "No general contractor could be matched for this invitation; confirm the GC first."
)
_MSG_NO_DATE_FIELDS = "Nothing to apply: no date field was given."
_MSG_GC_REQUIRED = "A GC is required."


# ── Public types ─────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Created:
    project_id: str
    number: str
    linked: bool          # True when the project already existed (no new project)
    files_job: bool       # True when a promotion job was enqueued
    # Why it linked, for the API and the page: None (the harvest carried the
    # project), `sibling` or `recent_project` (the 4.6 guard). `duplicate_of`
    # is the copy that made it, when a copy did.
    why: str | None = None
    duplicate_of: str | None = None


class CreateInProgress(Exception):
    """Another worker holds the harvest's create claim."""


class CreateRefused(Exception):
    """A user-facing sentence: no name, wrong status, sibling, already created."""


class CreateWaitingForSplit(CreateRefused):
    """The Bid File Splitter is still going to file this harvest's documents
    (`rfp_split.creation_must_wait`, docs/RFP_SPLIT.md 10.5): the button
    answers 409 with the sentence; the sweep's create step waits without
    spending an attempt and tries again."""


class CreateBlocked(CreateRefused):
    """A project made from this source (or from a copy of it, from a harvest
    it shares, or for the bid the matcher would link it to) was deleted
    (docs/PROJECT_DELETE.md 5): no creation, on either path, until an IT
    Admin restores it. `archive_id` is the `deleted_projects` row."""

    def __init__(self, message: str, archive_id: str):
        super().__init__(message)
        self.archive_id = archive_id


@dataclass(frozen=True)
class Facts:
    """What the project row is made of (4.3). Pure: no ids, no I/O."""

    name: str
    actual_bid_at: str | None
    bid_time_unknown: bool
    invitation_at: str | None
    address: str | None
    bidding_url: str | None
    no_bidding_url: bool
    bid_notes: str | None
    notes: str
    is_ngem: bool
    # The BuildingConnected columns (docs/RFP_BUILDINGCONNECTED.md 3.7,
    # contract 3.5). Defaults so the email and NGEM builders stay untouched:
    # `job_walk_at` is an instant, the two `est_*` are Pacific calendar days
    # (DATE columns), the two text fields keep their newlines (never through
    # `_text`), `is_budgetary` is the number's B suffix, the `files_needed_*`
    # pair raises the files flag at creation, `sender_display` names the lead.
    job_walk_at: str | None = None
    est_start_date: str | None = None
    est_finish_date: str | None = None
    project_information: str | None = None
    trade_instructions: str | None = None
    is_budgetary: bool = False
    files_needed_source: str | None = None
    files_needed_url: str | None = None
    gc_confirm_pending: bool = False
    sender_display: str | None = None


@dataclass(frozen=True)
class GcPlan:
    """How the GC is decided (4.4): `resolved` attaches gc_id and contact_id;
    `likely` attaches nothing and shows the inference; `create` makes the GC
    and a contact from the sender; `none` attaches nothing; `provisional`
    (BuildingConnected, D15) links the GC with no contact and asks for
    confirmation on the project. `lead` is the BuildingConnected lead
    ({first_name, last_name, email, phone}): for `resolved` only, its
    gc_contacts row is looked up or made at create time
    (`ensure_lead_contact`), inside the atomic core, so a failed creation
    takes a contact it inserted back."""

    kind: str
    gc_id: str | None = None
    contact_id: str | None = None
    name: str | None = None
    contact_name: str | None = None
    contact_email: str | None = None
    lead: dict | None = None


@dataclass(frozen=True)
class _Source:
    """The row a creation starts from, whichever table it lives in."""

    kind: str
    table: str
    row: dict
    expected_status: str


# ── Small helpers ────────────────────────────────────────────────────────────


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def _parse_ts(value: Any) -> datetime | None:
    if not value or not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _pacific_midnight(day) -> str:
    """Midnight Pacific of the calendar day, as a UTC instant."""
    return _iso(datetime.combine(day, time.min, tzinfo=_COMPANY_TZ).astimezone(timezone.utc))


def _pacific_date(value: Any) -> str | None:
    parsed = _parse_ts(value)
    return parsed.astimezone(_COMPANY_TZ).date().isoformat() if parsed else None


def _text(value: Any) -> str | None:
    """A single-line value: trimmed, whitespace runs collapsed, None when empty."""
    if value is None:
        return None
    out = " ".join(str(value).split())
    return out or None


def _cap(value: Any, limit: int) -> str | None:
    out = _text(value)
    return out[:limit] if out else None


def _chunks(items: list, size: int = _IN_CHUNK):
    for i in range(0, len(items), size):
        yield items[i:i + size]


def _is_unique_violation(exc: Exception) -> bool:
    text = str(exc)
    return "23505" in text or "duplicate key" in text


def has_project_name(row: dict) -> bool:
    """The one emptiness rule for an email row's project name, shared by the
    match step, the create step, the preconditions and the button: the
    name must survive `rfp_match.normalize_project_name` ("Invitation to
    Bid" or "RFP 2026-01" is no name at all)."""
    return rfp_match.has_project_name(row.get("extracted_project_name"))


# ── Facts (pure) ─────────────────────────────────────────────────────────────


def bid_due_from(harvest_value: Any, extracted_at: Any, extracted_has_time: Any) -> tuple[str | None, bool]:
    """`(actual_bid_at, bid_time_unknown)` from the harvest's `bid_due_at`
    (an ISO instant, or a date-only `YYYY-MM-DD` from a platform field
    without a time) else the extracted due date (an instant whose
    `extracted_bid_due_has_time` says whether the time is real). A
    date-only value is stored at midnight Pacific and marked unknown."""
    if isinstance(harvest_value, str) and harvest_value.strip():
        raw = harvest_value.strip()
        if _DATE_ONLY_RE.match(raw):
            try:
                return _pacific_midnight(datetime.strptime(raw, "%Y-%m-%d").date()), True
            except ValueError:
                pass
        else:
            parsed = _parse_ts(raw)
            if parsed is not None:
                return _iso(parsed), False
    parsed = _parse_ts(extracted_at)
    if parsed is None:
        return None, False
    if extracted_has_time is False or extracted_has_time is None:
        # The extract stored a date-only value at midnight Pacific already;
        # re-deriving it keeps the rule in one place whatever the store holds.
        return _pacific_midnight(parsed.astimezone(_COMPANY_TZ).date()), True
    return _iso(parsed), False


def bidding_url_from(harvest: dict | None) -> str | None:
    """The bidding link (4.3): the harvest's `external_url` (the Procore bid
    sheet, the PipelineSuite portal), else the first email-harvester link
    that listed or asked for a sign-in; cleaned like the New Project form
    cleans it, and None when nothing usable exists."""
    from app.models.schemas import _clean_bidding_url

    if not harvest:
        return None
    candidates: list[Any] = [harvest.get("external_url")]
    data = harvest.get("data") or {}
    for link in (data.get("links") or []) if isinstance(data, dict) else []:
        if isinstance(link, dict) and link.get("status") in _LINK_STATUSES:
            candidates.append(link.get("url"))
    for raw in candidates:
        if not isinstance(raw, str) or not raw.strip():
            continue
        try:
            cleaned = _clean_bidding_url(raw)
        except ValueError:
            continue
        if cleaned:
            return cleaned
    return None


def _join_notes(parts: list[Any], limit: int) -> str | None:
    texts = []
    for part in parts:
        if isinstance(part, str) and part.strip():
            texts.append(part.strip())
    if not texts:
        return None
    return _NOTES_JOIN.join(texts)[:limit]


def facts_for_email(row: dict, harvest: dict | None, *, notes_max_chars: int = 4000) -> Facts:
    """The 4.3 table's email column. Pure. A name a person typed ("Set
    project name" stamps `extract_model = "human"`) wins over the harvest's;
    otherwise the platform's name wins over the model's extraction."""
    data = (harvest or {}).get("data") or {}
    if not isinstance(data, dict):
        data = {}
    extracted = _cap(row.get("extracted_project_name"), _NAME_MAX_CHARS)
    harvested = _cap(data.get("project_name"), _NAME_MAX_CHARS)
    if row.get("extract_model") == EXTRACT_MODEL_HUMAN and extracted:
        name = extracted
    else:
        name = harvested or extracted
    actual_bid_at, time_unknown = bid_due_from(
        data.get("bid_due_at"), row.get("extracted_bid_due_at"), row.get("extracted_bid_due_has_time")
    )
    url = bidding_url_from(harvest)
    received = _pacific_date(row.get("received_at")) or "an unknown date"
    subject = _cap(row.get("subject"), _SUBJECT_MAX_CHARS) or "(no subject)"
    method = row.get("invitation_method") or "unknown method"
    return Facts(
        name=name or "",
        actual_bid_at=actual_bid_at,
        bid_time_unknown=time_unknown,
        invitation_at=row.get("received_at"),
        address=_cap(data.get("project_address"), 500),
        bidding_url=url,
        no_bidding_url=url is None,
        bid_notes=_join_notes(
            [
                row.get("extracted_bid_notes"),
                (harvest or {}).get("instructions_text"),
                (harvest or {}).get("description_text"),
            ],
            notes_max_chars,
        ),
        notes=f"Created from RFP invitation {subject} received {received} ({method})",
        is_ngem=False,
    )


def facts_for_portal(inv: dict, harvest: dict | None, *, notes_max_chars: int = 4000) -> Facts:
    """The 4.3 table's portal column. Pure. The portal URL is session-bound,
    so the project records "no bidding link"."""
    close_at = _parse_ts(inv.get("close_at"))
    portal = str(inv.get("portal") or "ngem").upper()
    agency = _cap(inv.get("agency"), 200) or "an agency"
    number = _cap(inv.get("bid_number_raw"), 200) or _cap(inv.get("bid_number"), 200) or "(no number)"
    return Facts(
        name=_cap(inv.get("title"), _NAME_MAX_CHARS) or "",
        actual_bid_at=_iso(close_at) if close_at else None,
        bid_time_unknown=False,
        invitation_at=inv.get("first_seen_at"),
        address=None,
        bidding_url=None,
        no_bidding_url=True,
        bid_notes=_join_notes([(harvest or {}).get("description_text")], notes_max_chars),
        notes=f"Created from {portal} {agency} bid {number}",
        is_ngem=True,
    )


def _bc_link(url: str | None) -> str | None:
    """The deep link as the New Project form would store it (the scheme
    allow-list), None when it does not survive."""
    from app.models.schemas import _clean_bidding_url

    if not isinstance(url, str) or not url.strip():
        return None
    try:
        return _clean_bidding_url(url)
    except ValueError:
        return None


def facts_for_bc(inv: dict, *, text_max_chars: int, notes_max_chars: int) -> Facts:
    """The BuildingConnected column of the facts table (docs/
    RFP_BUILDINGCONNECTED.md 3.7, contract D17), off the invitation row and
    its payload through `bc_facts.project_facts`. Pure. The deep link is the
    bidding link (`no_bidding_url` False); every other value may be null
    (no due date, no address, no text). The files flag is raised on every
    creation: BuildingConnected never hands over a file."""
    from app.services import bc_facts

    facts = bc_facts.project_facts(inv, text_max_chars=text_max_chars, notes_max_chars=notes_max_chars)
    url = _bc_link(facts.get("bidding_url"))
    return Facts(
        name=_cap(facts.get("name"), _NAME_MAX_CHARS) or "",
        actual_bid_at=facts.get("actual_bid_at"),
        bid_time_unknown=bool(facts.get("bid_time_unknown")),
        invitation_at=facts.get("invitation_at"),
        address=_cap(facts.get("address"), 500),
        bidding_url=url,
        no_bidding_url=url is None,
        bid_notes=facts.get("bid_notes") or None,
        notes=facts.get("notes") or "",
        is_ngem=False,
        job_walk_at=facts.get("job_walk_at"),
        est_start_date=facts.get("est_start_date"),
        est_finish_date=facts.get("est_finish_date"),
        project_information=facts.get("project_information") or None,
        trade_instructions=facts.get("trade_instructions") or None,
        is_budgetary=bool(facts.get("is_budgetary")),
        files_needed_source=PORTAL_BC,
        files_needed_url=url,
        gc_confirm_pending=False,
        sender_display=facts.get("sender_display"),
    )


# ── GC plan (4.4) ────────────────────────────────────────────────────────────


def _gc_name(sb, gc_id: str | None) -> str | None:
    if not gc_id:
        return None
    rows = (
        sb.table("general_contractors").select("id, name").eq("id", gc_id).limit(1).execute()
    ).data or []
    return rows[0].get("name") if rows else None


def _like_literal(text: str) -> str:
    """`text` as a literal inside a LIKE / ILIKE pattern: the wildcards `%`
    and `_` (and the escape itself) backslash-escaped."""
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


# A match-step GC candidate is only "likely" above this score: below it the
# fuzzy matcher is naming whatever is nearest in a short directory (0.16 on
# dev pointed a Monument Construction invitation at a test GC).
LIKELY_GC_MIN_SCORE = 0.6


def _likely_score(candidate: dict) -> bool:
    score = candidate.get("score")
    try:
        return float(score) >= LIKELY_GC_MIN_SCORE
    except (TypeError, ValueError):
        return False


def _likely_by_domain(sb, from_address: str | None) -> tuple[str | None, str | None]:
    """The GC whose contacts own the sender's domain (most contacts wins,
    ties by name), or (None, None). Public mailbox domains never infer.
    Filtered server-side (`email ilike '%@<domain>'`, the domain escaped as
    a literal) and capped, then re-checked here so the match is the exact
    domain and never a pattern accident."""
    domain = address_domain(from_address)
    if not domain or is_public_mailbox_domain(domain):
        return None, None
    contacts = (
        sb.table("gc_contacts")
        .select("id, gc_id, email")
        .ilike("email", f"%@{_like_literal(domain)}")
        .limit(_DOMAIN_CONTACTS_CAP)
        .execute()
    ).data or []
    counts: dict[str, int] = {}
    for contact in contacts:
        if contact.get("gc_id") and address_domain(contact.get("email")) == domain:
            counts[contact["gc_id"]] = counts.get(contact["gc_id"], 0) + 1
    if not counts:
        return None, None
    ids = sorted(counts)
    names: dict[str, str] = {}
    for chunk in _chunks(ids):
        rows = sb.table("general_contractors").select("id, name").in_("id", chunk).execute().data or []
        names.update({r["id"]: r.get("name") or "" for r in rows if r.get("id")})
    best = sorted(ids, key=lambda gid: (-counts[gid], names.get(gid, "").lower(), gid))[0]
    return best, names.get(best) or None


def gc_plan_for(sb, row: dict, harvest: dict | None) -> GcPlan:
    """resolved | likely | create | none, in that order of preference."""
    if row.get("resolved_gc_id"):
        return GcPlan(
            kind=GC_RESOLVED,
            gc_id=row["resolved_gc_id"],
            contact_id=row.get("resolved_gc_contact_id"),
            name=_gc_name(sb, row["resolved_gc_id"]),
        )
    candidates = [c for c in (row.get("gc_candidates") or []) if isinstance(c, dict)]
    if candidates and (candidates[0].get("name") or candidates[0].get("gc_id")) and _likely_score(candidates[0]):
        top = candidates[0]
        name = _text(top.get("name")) or _gc_name(sb, top.get("gc_id"))
        return GcPlan(kind=GC_LIKELY, gc_id=top.get("gc_id"), name=name)
    gc_id, name = _likely_by_domain(sb, row.get("from_address"))
    if gc_id:
        return GcPlan(kind=GC_LIKELY, gc_id=gc_id, name=name)
    if row.get("invitation_method") == METHOD_NONORGANIC:
        data = (harvest or {}).get("data") or {}
        gc = data.get("gc") if isinstance(data, dict) else None
        name = (
            _cap((gc or {}).get("name") if isinstance(gc, dict) else None, _NAME_MAX_CHARS)
            or _cap(row.get("extracted_gc_name"), _NAME_MAX_CHARS)
            or _cap(row.get("from_name"), _NAME_MAX_CHARS)
            or address_domain(row.get("from_address"))
        )
        if name:
            return GcPlan(
                kind=GC_CREATED,
                name=directory.clean_company_name(name),
                contact_name=_cap(row.get("from_name"), _NAME_MAX_CHARS)
                or _text(row.get("from_address")),
                contact_email=(row.get("from_address") or "").strip().lower() or None,
            )
    return GcPlan(kind=GC_NONE)


def gc_plan_for_bc(sb, inv: dict) -> GcPlan:
    """The BuildingConnected plan (contract 3.5) off the invitation's GC
    resolution: a confirmed kind (alias, contact, domain) is `resolved`,
    `provisional` is the new kind that attaches and asks, `none` (or no
    resolution at all) attaches nothing. The lead rides along so the
    contact is made at create time, never before the project exists."""
    kind = inv.get("gc_kind")
    gc_id = inv.get("gc_id")
    lead = inv.get("lead") if isinstance(inv.get("lead"), dict) else None
    if gc_id and kind in _BC_CONFIRMED_KINDS:
        return GcPlan(kind=GC_RESOLVED, gc_id=gc_id, name=_gc_name(sb, gc_id), lead=lead)
    if gc_id and kind == _BC_GC_KIND_PROVISIONAL:
        return GcPlan(kind=GC_PROVISIONAL, gc_id=gc_id, name=_gc_name(sb, gc_id), lead=lead)
    return GcPlan(kind=GC_NONE, lead=lead)


def _lead_email(lead: dict | None) -> str | None:
    if not isinstance(lead, dict):
        return None
    email = lead.get("email")
    if not isinstance(email, str):
        return None
    email = email.strip().lower()
    return email or None


def _lead_name(lead: dict) -> str | None:
    parts = [_text(lead.get("first_name")), _text(lead.get("last_name"))]
    name = " ".join(p for p in parts if p)
    return name or None


def _find_lead_contact_row(sb, gc_id: str, email: str) -> dict | None:
    """`{id, created_at}` of the oldest gc_contacts row under `gc_id` whose
    email is `email` (case insensitive, the address matched as a literal),
    else None. The one lookup `ensure_lead_contact` and the GC swap's
    cleanup share."""
    rows = (
        sb.table("gc_contacts")
        .select("id, created_at")
        .eq("gc_id", gc_id)
        .ilike("email", _like_literal(email))
        .order("created_at")
        .order("id")
        .limit(1)
        .execute()
    ).data or []
    return rows[0] if rows else None


def _find_lead_contact(sb, gc_id: str, email: str) -> str | None:
    """The id `_find_lead_contact_row` finds, else None."""
    row = _find_lead_contact_row(sb, gc_id, email)
    return row["id"] if row else None


def ensure_lead_contact(sb, gc_id: str, lead: dict | None) -> tuple[str | None, bool]:
    """`(contact_id, inserted)` for the BuildingConnected lead under `gc_id`
    (contract D16): the oldest gc_contacts row whose email is the lead's
    (case insensitive, the address matched as a literal), else a new one
    named "First Last" (the company name when the lead has no name), with
    the lowercased email and the phone. A lead with no email is nobody to
    attach: (None, False). `inserted` tells the caller what a failed
    creation must take back; a pre-existing contact is never deleted."""
    email = _lead_email(lead)
    if not gc_id or not email:
        return None, False
    existing = _find_lead_contact(sb, gc_id, email)
    if existing:
        return existing, False
    name = _lead_name(lead) or _gc_name(sb, gc_id) or email
    phone = _text(lead.get("phone")) if isinstance(lead, dict) else None
    inserted = (
        sb.table("gc_contacts")
        .insert({"gc_id": gc_id, "name": name, "email": email, "phone": phone})
        .execute()
    ).data[0]
    return inserted["id"], True


@dataclass(frozen=True)
class _GcWrite:
    """What the `create` plan wrote (4.4 step 3). `inserted_gc_id` and
    `inserted_contact_id` name the rows this call added, so a compensation
    can take them back; both None when an existing GC was reused."""

    gc_id: str
    gc_name: str | None
    contact_id: str | None
    inserted_gc_id: str | None = None
    inserted_contact_id: str | None = None

    @property
    def reused(self) -> bool:
        return self.inserted_gc_id is None


def _ensure_gc(sb, plan: GcPlan, *, test_session_id: str | None = None) -> _GcWrite:
    """The `create` plan's writes. An EXISTING GC on a near-duplicate name
    (the directory guard) is reused as is: attached to the project with NO
    contact, and recorded as a likely GC rather than a created one (an
    outside sender's address is never added to a real GC's contacts by the
    app). Only when no GC matches is one inserted, with a contact from the
    sender. A test session's rows carry its tag (docs/RFP_TESTING.md 4.4)
    so cleanup can take them back; a reused GC is never tagged."""
    existing = directory.find_duplicate_company(sb, "general_contractors", plan.name or "")
    if existing:
        return _GcWrite(gc_id=existing["id"], gc_name=existing.get("name") or plan.name, contact_id=None)
    tag = {"test_session_id": test_session_id} if test_session_id else {}
    gc_id = sb.table("general_contractors").insert({"name": plan.name, **tag}).execute().data[0]["id"]
    contact_id = None
    if plan.contact_email:
        contact_id = (
            sb.table("gc_contacts")
            .insert({
                "gc_id": gc_id,
                "name": plan.contact_name or plan.contact_email,
                "email": plan.contact_email,
                **tag,
            })
            .execute()
        ).data[0]["id"]
    return _GcWrite(
        gc_id=gc_id, gc_name=plan.name, contact_id=contact_id,
        inserted_gc_id=gc_id, inserted_contact_id=contact_id,
    )


def _delete_inserted_gc(sb, inserted_gc_id: str | None, inserted_contact_id: str | None) -> None:
    """Take back the GC and contact `_ensure_gc` inserted in this call (the
    contact first, then the GC unless another project has linked it
    meanwhile). Never raises."""
    if inserted_contact_id:
        try:
            sb.table("gc_contacts").delete().eq("id", inserted_contact_id).execute()
        except Exception:  # noqa: BLE001
            logger.exception("rfp create: compensating contact delete failed for %s", inserted_contact_id)
    if not inserted_gc_id:
        return
    try:
        links = (
            sb.table("project_gcs").select("id").eq("gc_id", inserted_gc_id).limit(1).execute()
        ).data or []
        if links:
            logger.warning("rfp create: GC %s kept; another project links it", inserted_gc_id)
            return
        sb.table("general_contractors").delete().eq("id", inserted_gc_id).execute()
    except Exception:  # noqa: BLE001
        logger.exception("rfp create: compensating GC delete failed for %s", inserted_gc_id)


def _contact_at_gc(sb, contact_id: str | None, gc_id: str) -> bool:
    if not contact_id:
        return False
    rows = (
        sb.table("gc_contacts").select("id").eq("id", contact_id).eq("gc_id", gc_id).limit(1).execute()
    ).data or []
    return bool(rows)


def _attach_gc(sb, project_id: str, gc_id: str, contact_id: str | None, needs_by: str | None) -> None:
    link = (
        sb.table("project_gcs")
        .insert({"project_id": project_id, "gc_id": gc_id, "needs_by": needs_by})
        .execute()
    ).data[0]
    if contact_id and _contact_at_gc(sb, contact_id, gc_id):
        sb.table("project_gc_contacts").insert(
            {"project_gc_id": link["id"], "gc_contact_id": contact_id}
        ).execute()


# ── The unauthorized marker (4.4) ────────────────────────────────────────────


def sender_marker(sb, row: dict) -> tuple[bool, str | None]:
    """`(sender_was_unauthorized, sender_allowed_by)`: the row passed through
    `flagged_unauthorized` and a person let it continue when its
    `continued_by` is set or its audit trail carries `rfp_email.continue`;
    the allowing user is `continued_by`, else that audit row's actor. The
    method alone never counts: a `nonorganic` a person set by hand on a
    row that was never flagged is a correction, not an override."""
    allowed_by = row.get("continued_by")
    actor = None
    rows: list[dict] = []
    if not allowed_by:
        try:
            rows = (
                sb.table("audit_log")
                .select("actor_id, created_at")
                .eq("entity", "rfp_email")
                .eq("entity_id", row.get("id"))
                .eq("action", CONTINUE_AUDIT_ACTION)
                .order("created_at", desc=True)
                .limit(1)
                .execute()
            ).data or []
            actor = rows[0].get("actor_id") if rows else None
        except Exception:  # noqa: BLE001 - the marker is advisory
            logger.exception("rfp create: audit lookup failed for %s", row.get("id"))
            rows = []
    unauthorized = bool(allowed_by) or bool(rows)
    return unauthorized, (allowed_by or actor) if unauthorized else None


# ── Harvest claim (4.2) ──────────────────────────────────────────────────────


def _load_harvest(sb, harvest_id: str | None) -> dict | None:
    if not harvest_id:
        return None
    rows = sb.table(HARVESTS_TABLE).select("*").eq("id", harvest_id).limit(1).execute().data or []
    return rows[0] if rows else None


def _claim_harvest(sb, harvest_id: str, token: str, settings: Settings) -> bool:
    now = _now()
    cutoff = _iso(now - timedelta(seconds=settings.rfp_create_claim_seconds))
    rows = (
        sb.table(HARVESTS_TABLE)
        .update({"create_claim_token": token, "create_claimed_at": _iso(now)})
        .eq("id", harvest_id)
        .is_("project_id", "null")
        .or_(f"create_claim_token.is.null,create_claimed_at.lt.{cutoff}")
        .execute()
    ).data or []
    return bool(rows)


def _release_claim(sb, harvest_id: str | None, token: str | None, *, project_id: str | None) -> None:
    """Hand the claim back, setting the harvest's project link on success
    (None leaves it as it is). Fenced on the token."""
    if not harvest_id or not token:
        return
    fields: dict = {"create_claim_token": None, "create_claimed_at": None}
    if project_id:
        fields["project_id"] = project_id
    try:
        sb.table(HARVESTS_TABLE).update(fields).eq("id", harvest_id).eq(
            "create_claim_token", token
        ).execute()
    except Exception:  # noqa: BLE001 - a stale claim expires on its own
        logger.exception("rfp create: claim release failed for harvest %s", harvest_id)


# ── Source rows ──────────────────────────────────────────────────────────────


def created_fields(project_id: str) -> dict:
    return {
        "status": STATUS_CREATED,
        "created_project_id": project_id,
        "decided_at_step": STATUS_CREATE,
        "next_attempt_at": None,
        "last_error": None,
        "updated_at": _iso(_now()),
    }


def _cas_source(sb, source: _Source, project_id: str, *, extra: dict | None = None) -> bool:
    rows = (
        sb.table(source.table)
        .update({**created_fields(project_id), **(extra or {})})
        .eq("id", source.row["id"])
        .eq("status", source.expected_status)
        .execute()
    ).data or []
    return bool(rows)


def _link_sibling_followers(sb, root_id: str, fields: dict) -> int:
    """Every row that follows `root_id` through `sibling_of_email_id`, at any
    depth. A chain forms when a follower is itself the earliest DECIDED copy
    on a later tick and a third copy follows IT (docs/RFP_MATCHING.md 3.1);
    one hop would strand the far end with `flag_reason = sibling` and no
    project. The frontier is read before it is written, so a level already at
    `created` still leads to its own followers. Bounded and cycle-safe.

    Each frontier read is capped like `rfp_match.read_siblings`: copies of one
    message are a handful, so a full page means something else is going on and
    is logged rather than silently truncated."""
    moved = 0
    frontier = [str(root_id)]
    seen = {str(root_id)}
    for _ in range(_SIBLING_CHAIN_MAX_HOPS):
        children: list[str] = []
        for chunk in _chunks(frontier):
            rows = (
                sb.table(EMAILS_TABLE)
                .select("id")
                .in_("sibling_of_email_id", chunk)
                .limit(rfp_match.SIBLING_READ_CAP)
                .execute()
            ).data or []
            if len(rows) >= rfp_match.SIBLING_READ_CAP:
                logger.warning(
                    "rfp create: sibling follower read under %s hit the %s row cap; "
                    "followers beyond it are not linked to %s",
                    root_id, rfp_match.SIBLING_READ_CAP, fields.get("created_project_id"),
                )
            for r in rows:
                child = str(r.get("id") or "")
                if child and child not in seen:
                    seen.add(child)
                    children.append(child)
        if not children:
            return moved
        for chunk in _chunks(children):
            moved += len(
                (
                    sb.table(EMAILS_TABLE)
                    .update(fields)
                    .in_("id", chunk)
                    .in_("status", list(CREATABLE_STATUSES))
                    .execute()
                ).data or []
            )
        frontier = children
    logger.warning("rfp create: sibling chain under %s is deeper than %s hops",
                   root_id, _SIBLING_CHAIN_MAX_HOPS)
    return moved


def link_harvest_mates(sb, harvest_id: str | None, project_id: str, *, sibling_of: str | None = None,
                       exclude_id: str | None = None) -> int:
    """Every other email or invitation sharing the harvest at `done` or
    `create`, and every sibling follower of the source email at any depth,
    becomes `created` with the same project. `flag_reason` is untouched (a
    follower keeps `sibling`). Returns how many rows moved."""
    moved = 0
    fields = created_fields(project_id)
    if harvest_id:
        for table in (EMAILS_TABLE, INVITATIONS_TABLE):
            query = (
                sb.table(table)
                .update(fields)
                .eq("harvest_id", harvest_id)
                .in_("status", list(MATE_STATUSES))
            )
            if exclude_id:
                query = query.neq("id", exclude_id)
            moved += len(query.execute().data or [])
    if sibling_of:
        moved += _link_sibling_followers(sb, str(sibling_of), fields)
    return moved


def _link_existing(sb, source: _Source, project_id: str, actor_id: str | None) -> Created:
    """The harvest already has a project: the row joins it (4.1)."""
    if not _cas_source(sb, source, project_id):
        raise CreateRefused(_MSG_LOST_RACE)
    number = _project_number(sb, project_id)
    audit(actor_id, AUDIT_LINK_ACTION, source.kind, source.row["id"],
          {"project_id": project_id, "number": number, "harvest_id": source.row.get("harvest_id")})
    return Created(project_id=project_id, number=number, linked=True, files_job=False)


def _project_number(sb, project_id: str) -> str:
    rows = sb.table("projects").select("id, number").eq("id", project_id).limit(1).execute().data or []
    return (rows[0].get("number") if rows else None) or ""


# ── Create-time duplicate guard (4.6) ────────────────────────────────────────

# How many recently created projects the name check reads at most.
_DUPLICATE_PROJECT_CAP = 200

WHY_SIBLING = "sibling"            # another copy of this same email made it
WHY_RECENT_PROJECT = "recent_project"   # same name, same GC, made minutes ago


@dataclass(frozen=True)
class _Duplicate:
    """What the guard found (4.6). `project` is the matched `projects` row,
    carried so the manual path's refusal can name it."""

    project_id: str
    why: str
    sibling_email_id: str | None = None
    project: dict | None = None


def normalized_name(value: Any) -> str:
    """A project name compared for equality: whitespace collapsed, casefolded."""
    return " ".join(str(value or "").split()).casefold()


def _project_joinable(sb, project_id: str, excluded: set[str]) -> bool:
    """A project this row may be linked to: not one an unmerge excluded
    (docs/RFP_MATCHING.md 3.8 put it in `excluded_project_ids` precisely so
    this row never lands there again), still present, and not abandoned."""
    if str(project_id) in excluded:
        return False
    rows = (
        sb.table("projects")
        .select("id, abandoned_at")
        .eq("id", project_id)
        .limit(1)
        .execute()
    ).data or []
    return bool(rows) and not rows[0].get("abandoned_at")


def _sibling_created_project(sb, row: dict, settings: Settings) -> _Duplicate | None:
    """The project another copy of this same email has already made, looked
    for in EITHER direction inside the sibling window
    (docs/RFP_MATCHING.md 3.1). The oldest such copy wins, so two copies
    racing here land on the same project. A copy at `created` with no
    `created_project_id` has nothing to join, and a project that was
    excluded by an unmerge or abandoned is skipped."""
    window = int(getattr(settings, "rfp_match_sibling_window_minutes", 0) or 0)
    excluded = {str(x) for x in (row.get("excluded_project_ids") or [])}
    candidates = rfp_match.sibling_candidates(
        row, rfp_match.read_siblings(sb, row, window), settings,
    )
    for cand in candidates:
        project_id = cand.get("created_project_id")
        if not project_id or not _project_joinable(sb, str(project_id), excluded):
            continue
        return _Duplicate(project_id=str(project_id), why=WHY_SIBLING,
                          sibling_email_id=str(cand["id"]))
    return None


def _recent_duplicate_project(sb, row: dict, settings: Settings) -> _Duplicate | None:
    """The project a moment ago that IS this invitation: created inside
    `RFP_CREATE_DUPLICATE_WINDOW_MINUTES` (0 disables), the same normalized
    name as this row's extracted name, and already linked to this row's
    resolved GC. A row with no resolved GC never matches here: a name alone
    is the matcher's job, not a blind guard's. An excluded or abandoned
    project is never a hit, and the read stays inside the row's test-bench
    scope the way the sweep does."""
    minutes = int(getattr(settings, "rfp_create_duplicate_window_minutes", 0) or 0)
    gc_id = row.get("resolved_gc_id")
    name = normalized_name(row.get("extracted_project_name"))
    if minutes <= 0 or not gc_id or not name:
        return None
    excluded = {str(x) for x in (row.get("excluded_project_ids") or [])}
    cutoff = _iso(_now() - timedelta(minutes=minutes))
    query = (
        sb.table("projects")
        .select("id, name, number, created_at")
        .gte("created_at", cutoff)
        .is_("abandoned_at", "null")
    )
    session_id = row.get("test_session_id")
    if session_id:
        query = query.eq("test_session_id", session_id)
    elif "test_session_id" in row:
        # Only a deployment with the bench on has the column; a real row must
        # never join a project a test session made, or the other way round.
        query = query.is_("test_session_id", "null")
    projects = (
        query
        # Newest first, so the cap cuts the far end of the window rather than
        # the project that was very likely made seconds ago.
        .order("created_at", desc=True)
        .limit(_DUPLICATE_PROJECT_CAP)
        .execute()
    ).data or []
    matched = [
        p for p in projects
        if normalized_name(p.get("name")) == name and str(p["id"]) not in excluded
    ]
    # Oldest wins, so two copies racing here choose the same project.
    matched.sort(key=lambda p: (str(p.get("created_at") or ""), str(p["id"])))
    if not matched:
        return None
    by_id = {str(p["id"]): p for p in matched}
    linked: set[str] = set()
    for chunk in _chunks(list(by_id)):
        rows = (
            sb.table("project_gcs")
            .select("project_id, gc_id")
            .in_("project_id", chunk)
            .eq("gc_id", gc_id)
            .execute()
        ).data or []
        linked.update(str(r["project_id"]) for r in rows if r.get("project_id"))
    hit = next((p for p in matched if str(p["id"]) in linked), None)
    if hit is None:
        return None
    return _Duplicate(project_id=str(hit["id"]), why=WHY_RECENT_PROJECT, project=hit)


def duplicate_project_for(sb, row: dict, settings: Settings) -> _Duplicate | None:
    """The project for this email, when it already exists (4.6). Read last,
    right before the insert: the sweep is serialized by one lease, but two
    copies of one invitation can reach the create step on different ticks,
    and the arrival order of the copies is Graph's, not ours."""
    return (
        _sibling_created_project(sb, row, settings)
        or _recent_duplicate_project(sb, row, settings)
    )


def recent_project_sentence(project: dict | None) -> str:
    """The manual path's refusal for the recent-project half (4.6). A person
    is told what exists and what to do; the app never silently attaches an
    email a person is creating from to a project it guessed at."""
    created = _parse_ts((project or {}).get("created_at"))
    minutes = max(int((_now() - created).total_seconds() // 60), 0) if created else 0
    label = " ".join(
        part for part in [(project or {}).get("number"), (project or {}).get("name")] if part
    ) or "another project"
    return (
        f"A project with this name for this GC was created {minutes} minutes ago: {label}. "
        "Merge this email into it, or rename the project, before creating another."
    )


def _link_duplicate(
    sb, source: _Source, duplicate: _Duplicate, actor_id: str | None, *,
    test_session_id: str | None,
) -> Created:
    """The guard's outcome: the row joins the project that already exists
    instead of making a second one. A sibling link also stamps
    `sibling_of_email_id` and `flag_reason = sibling`, the same marks the
    extract step's short circuit and `link_harvest_mates` leave. A lost CAS
    raises `CreateRefused`, exactly as `_link_existing` does."""
    project_id, sibling_id, why = duplicate.project_id, duplicate.sibling_email_id, duplicate.why
    extra: dict = {}
    if sibling_id:
        extra["sibling_of_email_id"] = sibling_id
        extra["flag_reason"] = FLAG_SIBLING
    if not _cas_source(sb, source, project_id, extra=extra):
        raise CreateRefused(_MSG_LOST_RACE)
    number = _project_number(sb, project_id)
    # `duplicate_of` is always the thing this row was found to duplicate: the
    # copy's email id for a sibling hit, the project id for a recent-project
    # hit. The rule that found it is `why`, beside it, never in its place.
    detail = {"project_id": project_id, "number": number,
              "duplicate_of": sibling_id or project_id, "why": why,
              "sibling_of_email_id": sibling_id}
    audit(actor_id, AUDIT_LINK_ACTION, source.kind, source.row["id"], detail)
    if test_session_id:
        rfp_test.record(
            sb, session_id=test_session_id, source=rfp_test.SOURCE_CREATE, kind="linked",
            title=f"Create: joined {number or project_id} ({why}); no second project",
            project_id=project_id, rfp_email_id=source.row["id"], detail=detail,
        )
    else:
        logger.info(
            "rfp create: %s %s joined existing project %s (%s)",
            source.kind, source.row["id"], project_id, why,
        )
    return Created(project_id=project_id, number=number, linked=True, files_job=False,
                   why=why, duplicate_of=sibling_id)


# ── The deleted-project block (docs/PROJECT_DELETE.md 5) ─────────────────────

# The flag a source row carries when the block stopped it at the create step.
FLAG_PROJECT_DELETED = "project_deleted"
BLOCK_COLUMN = "create_blocked_archive_id"


def _block_of(sb, table: str, row_id: str | None) -> str | None:
    """A row's `create_blocked_archive_id`, read fresh (the callers' row
    dicts may come from a select that predates migration 0141)."""
    if not row_id:
        return None
    rows = (
        sb.table(table).select(f"id, {BLOCK_COLUMN}").eq("id", row_id).limit(1).execute()
    ).data or []
    return (rows[0].get(BLOCK_COLUMN) if rows else None) or None


def _sibling_block(sb, row: dict, settings: Settings) -> str | None:
    """The block on another copy of this same email (the sibling rule's
    copies, docs/RFP_MATCHING.md 3.1, read in both directions)."""
    window = int(getattr(settings, "rfp_match_sibling_window_minutes", 0) or 0)
    if window <= 0:
        return None
    rows = rfp_match.read_siblings(
        sb, row, window, select=f"{rfp_match.SIBLING_SELECT}, {BLOCK_COLUMN}",
    )
    for cand in rfp_match.sibling_candidates(row, rows, settings):
        if cand.get(BLOCK_COLUMN):
            return str(cand[BLOCK_COLUMN])
    return None


def _portal_sibling_block(sb, inv: dict) -> str | None:
    """The block on a BuildingConnected package sibling (`sibling_of`): the
    invitation this one follows, or another one following the same lead."""
    lead = inv.get("sibling_of")
    ids = [str(x) for x in (lead,) if x]
    rows: list[dict] = []
    if ids:
        rows.extend(
            sb.table(INVITATIONS_TABLE).select(f"id, {BLOCK_COLUMN}").in_("id", ids).execute().data or []
        )
    root = str(lead or inv.get("id") or "")
    if root:
        rows.extend(
            sb.table(INVITATIONS_TABLE).select(f"id, {BLOCK_COLUMN}").eq("sibling_of", root)
            .limit(50).execute().data or []
        )
    for r in rows:
        if str(r.get("id")) != str(inv.get("id")) and r.get(BLOCK_COLUMN):
            return str(r[BLOCK_COLUMN])
    return None


def create_block_for(
    sb, source_kind: str, row: dict, *, harvest: dict | None = None,
    settings: Settings | None = None,
) -> str | None:
    """The `deleted_projects` id that forbids creating a project from this
    row, or None. In order: the row's own mark; its harvest's mark (every
    email or invitation sharing a harvest, Procore reminders included); a
    copy of the same email (or a BuildingConnected package sibling) that
    carries one; and, for an email, a deleted project the matcher would
    have linked it to (project_delete.tombstone_for_email). Shared by the
    pipeline's create steps and the button path, so neither can create
    what a person deleted."""
    settings = settings or get_settings()
    table = EMAILS_TABLE if source_kind == SOURCE_EMAIL else INVITATIONS_TABLE
    blocked = row.get(BLOCK_COLUMN) if BLOCK_COLUMN in row else _block_of(sb, table, row.get("id"))
    if blocked:
        return str(blocked)
    harvest_id = row.get("harvest_id")
    if harvest is not None and BLOCK_COLUMN in harvest:
        if harvest.get(BLOCK_COLUMN):
            return str(harvest[BLOCK_COLUMN])
    elif harvest_id:
        blocked = _block_of(sb, HARVESTS_TABLE, harvest_id)
        if blocked:
            return str(blocked)
    if source_kind == SOURCE_EMAIL:
        blocked = _sibling_block(sb, row, settings)
        if blocked:
            return blocked
        from app.services import project_delete

        return project_delete.tombstone_for_email(sb, row, settings)
    return _portal_sibling_block(sb, row)


def _refuse_if_blocked(
    sb, source_kind: str, row: dict, harvest: dict | None, settings: Settings,
) -> None:
    """Raise CreateBlocked (with the deleted project named) when the row is
    blocked, stamping the mark on the row first so the email or invitation
    screen can say why (best effort; the refusal stands regardless)."""
    archive_id = create_block_for(sb, source_kind, row, harvest=harvest, settings=settings)
    if not archive_id:
        return
    from app.services import project_delete

    table = EMAILS_TABLE if source_kind == SOURCE_EMAIL else INVITATIONS_TABLE
    if row.get(BLOCK_COLUMN) != archive_id:
        try:
            sb.table(table).update({BLOCK_COLUMN: archive_id}).eq("id", row["id"]).execute()
        except Exception:  # noqa: BLE001
            logger.exception("rfp create: could not mark %s %s as blocked", table, row.get("id"))
    raise CreateBlocked(
        project_delete.blocked_sentence(project_delete.archive_ref(sb, archive_id)), archive_id,
    )


# ── Preconditions (4.1) ──────────────────────────────────────────────────────


def _check_email(row: dict) -> None:
    if row.get("status") not in CREATABLE_STATUSES:
        raise CreateRefused(_MSG_ALREADY_CREATED if row.get("status") == STATUS_CREATED else _MSG_WRONG_STATUS)
    if row.get("created_project_id"):
        raise CreateRefused(_MSG_ALREADY_CREATED)
    if row.get("flag_reason") == FLAG_SIBLING:
        raise CreateRefused(_MSG_SIBLING)
    if not has_project_name(row):
        raise CreateRefused(_MSG_NO_NAME)


def _check_portal(inv: dict) -> None:
    if inv.get("status") not in CREATABLE_STATUSES:
        raise CreateRefused(_MSG_ALREADY_CREATED if inv.get("status") == STATUS_CREATED else _MSG_WRONG_STATUS)
    if inv.get("created_project_id"):
        raise CreateRefused(_MSG_ALREADY_CREATED)
    if not _text(inv.get("title")):
        raise CreateRefused(_MSG_NO_NAME)
    if inv.get("portal") == PORTAL_BC:
        # The BuildingConnected refusals (contract D18): the button path's
        # half of the create gate. The sweep's half lives in the portal's
        # create step, which parks the row instead of refusing it.
        if inv.get("flag_reason") == FLAG_NDA_REQUIRED:
            raise CreateRefused(_MSG_NDA_REQUIRED)
        if _bc_gc_kind_is_none(inv):
            raise CreateRefused(_MSG_NO_GC)


def _bc_gc_kind_is_none(inv: dict) -> bool:
    """Whether a BuildingConnected row has no GC to attach. A row parked
    before its GC was resolved (no project name, the NDA-masked shape) never
    had gc_kind written, so a NULL counts as `none` here exactly as the
    sweep's create gate (rfp_bc_portal.before_create) reads it; otherwise a
    reviewer's "Not a match" on such a row would make a project with no GC
    even though the platform named one."""
    return (inv.get("gc_kind") or _BC_GC_KIND_NONE) == _BC_GC_KIND_NONE


# ── Creation (4.5) ───────────────────────────────────────────────────────────


def _insert_project(sb, payload: dict, *, budgetary: bool = False) -> dict:
    """The projects insert with an app-assigned number (section 2);
    `budgetary` is the number's B suffix (a BuildingConnected BUDGET
    request, contract D14). A seam: the tests stand it in, and the
    numbering module is imported here so this module loads without it."""
    from app.services import project_numbers

    try:
        return project_numbers.insert_with_assigned_number(sb, payload, budgetary=bool(budgetary))
    except project_numbers.NoFreeNumberError as exc:
        raise CreateRefused(_MSG_NO_NUMBER) from exc


def _seed_state(sb, project_id: str, actor_id: str | None) -> None:
    """The stage event and the 4-category seed, the same rows create_project writes."""
    sb.table("stage_events").insert(
        {"project_id": project_id, "from_stage": None, "to_stage": "intake",
         "category": "intake", "actor_id": actor_id}
    ).execute()
    sb.table("project_category_state").insert(
        [
            {
                "project_id": project_id,
                "category": cat,
                "current_task": workflow.CATEGORY_TASKS[cat][0],
                "status": "active" if cat == "intake" else "locked",
                "owner_role": workflow.owner_role_for(workflow.CATEGORY_TASKS[cat][0]) or None,
            }
            for cat in workflow.CATEGORY_ORDER
        ]
    ).execute()


def _compensate(sb, project_id: str, harvest_id: str | None, token: str | None, actor_id: str | None,
                action: str, payload: dict, *, gc_write: _GcWrite | None = None) -> None:
    """Undo a creation that cannot stand: delete the project (cascades clean
    the links, the events, the state and the created record), take back the
    GC and contact this call inserted (`gc_write`, when the plan created
    them) and give the harvest claim back. Never raises."""
    try:
        sb.table("projects").delete().eq("id", project_id).execute()
    except Exception:  # noqa: BLE001 - the original error still propagates
        logger.exception("rfp create: compensating delete failed; project %s remains", project_id)
    if gc_write is not None:
        _delete_inserted_gc(sb, gc_write.inserted_gc_id, gc_write.inserted_contact_id)
    _release_claim(sb, harvest_id, token, project_id=None)
    try:
        audit(actor_id, action, "project", project_id, payload)
    except Exception:  # noqa: BLE001
        logger.exception("rfp create: compensation audit failed for %s", project_id)


def _note_error(sb, project_id: str, text: str) -> None:
    try:
        sb.table(TABLE).update({"last_error": text[:_ERROR_MAX_CHARS]}).eq(
            "project_id", project_id
        ).execute()
    except Exception:  # noqa: BLE001
        logger.exception("rfp create: could not record the error on %s", project_id)


def _mark_files_pending(sb, project_id: str, prior_status: str = FILES_NONE) -> bool:
    """`files_status` `prior_status` -> pending (rfp_create_files.mark_pending),
    the fence before the job is enqueued. `none` for a project this call just
    made; the recent-project link (4.6) passes the status the target's record
    actually carries. Imported here so this module loads without the
    promotion job's storage dependencies."""
    from app.services import rfp_create_files

    return rfp_create_files.mark_pending(sb, project_id, prior_status)


def _unmark_files_pending(sb, project_id: str, prior_status: str = FILES_NONE) -> None:
    from app.services import rfp_create_files

    rfp_create_files.unmark_pending(sb, project_id, prior_status)


def _entries_with_files(harvest: dict | None) -> bool:
    for entry in (harvest or {}).get("files") or []:
        if isinstance(entry, dict) and entry.get("sandbox_file_id") and entry.get("status") in _ENTRY_STATUSES_WITH_FILES:
            return True
    return False


def _start_rescan(project_id: str) -> None:
    """The learn-back (create_project runs it as a background task): Unknown
    PM emails re-identified against the new project, off the caller's
    thread and best effort."""

    def run() -> None:
        try:
            from app.services import email_ingest

            email_ingest.rescan_unknown_for_project(project_id)
        except Exception:  # noqa: BLE001
            logger.exception("rfp create: learn-back rescan failed for %s", project_id)

    threading.Thread(target=run, name=f"rfp-create-rescan-{project_id[:8]}", daemon=True).start()


def _notify(sb, project: dict, missing: list[str], source_kind: str, split_note: str | None = None) -> None:
    """The two creation bells (email mirrored). `split_note` is the Bid File
    Splitter sentence when the split failed or partly failed
    (rfp_split.creation_note, docs/RFP_SPLIT.md 10), appended to both."""
    number, name = project.get("number") or "", project.get("name") or ""
    label = f"{number} {name}".strip()
    meta = {"project_id": project["id"], "number": number, "missing": list(missing), "source_kind": source_kind}
    if split_note:
        meta["split_note"] = split_note
    tail = f". {split_note}" if split_note else ""
    notify_role(
        Role.EXECUTIVE, project["id"], NOTIFY_CREATED,
        f"Project {label} was created from an RFP invitation and is waiting in Go/No-Go{tail}",
        mirror_email=True, metadata=meta,
    )
    phrase = notification_email.intake_missing_phrase(missing) or "none"
    notify_role(
        Role.ESTIMATING_ADMIN, project["id"], NOTIFY_INTAKE_NEEDED,
        f"Project {label} was created from an RFP invitation; intake details needed: {phrase}{tail}",
        mirror_email=True, metadata=meta,
    )


def _attach_harvest(
    sb, source: _Source, harvest: dict | None, token: str | None, project_id: str,
) -> None:
    """Step 7: the harvest link, the claim released, the mates; the split job
    learns its project (docs/RFP_SPLIT.md 4) so corrections in the splitter
    re-file this project's documents. Best effort throughout: the project
    exists either way. Shared with the create-time duplicate guard's
    recent-project link (4.6), whose harvest is this row's own."""
    row = source.row
    harvest_id = harvest.get("id") if harvest else None
    _release_claim(sb, harvest_id, token, project_id=project_id)
    # Re-read the split pointer AFTER the project link landed: a mate row
    # may have staged a split job while this project was being created (the
    # late-link race, review 2026-09-30). The split side re-reads the
    # project after pointing the harvest at its job
    # (rfp_split._link_late_project), so one of the two always links it.
    if harvest_id:
        try:
            fresh = (
                sb.table(HARVESTS_TABLE).select("id, split_status, split_job_id").eq("id", harvest_id)
                .limit(1).execute()
            ).data or []
            if fresh:
                harvest = {**(harvest or {}), **fresh[0]}
        except Exception:  # noqa: BLE001 - the read the caller made stands
            logger.exception("rfp create: split pointer re-read failed for harvest %s", harvest_id)
    if (harvest or {}).get("split_job_id"):
        try:
            sb.table("bid_split_jobs").update({"project_id": project_id}).eq(
                "id", harvest["split_job_id"]).is_("project_id", "null").execute()
            # The split step gave up while its job kept going (rfp_split.
            # give_up leaves a `running` harvest alone): a job that settled
            # BEFORE this link found no project, so bid_split.refresh_job
            # wrote nothing back and the harvest would read `running`
            # forever. Settle it now (a job still processing settles itself).
            if harvest.get("split_status") == rfp_split.SPLIT_RUNNING:
                job = rfp_split._job(sb, harvest["split_job_id"])
                if job is not None and job.get("status") != "processing" and job.get("project_id"):
                    rfp_split.settle_linked_job(sb, job)
        except Exception as exc:  # noqa: BLE001
            logger.exception("rfp create: split job link failed for %s", project_id)
            _note_error(sb, project_id, f"Linking the split job failed: {exc}")
    try:
        link_harvest_mates(
            sb, harvest_id, project_id,
            sibling_of=row["id"] if source.kind == SOURCE_EMAIL else None,
            exclude_id=row["id"],
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("rfp create: linking harvest mates failed for %s", project_id)
        _note_error(sb, project_id, f"Linking related invitations failed: {exc}")


def _enqueue_files_job(
    sb, settings: Settings, harvest: dict | None, project_id: str, actor_id: str | None,
    *, prior_status: str = FILES_NONE,
) -> bool:
    """Step 8: the documents. The record goes `pending` FIRST, fenced on the
    status it was read with, and only then is the job enqueued: a crash
    between the two leaves a pending record "Retry documents" can act on,
    never an enqueued job over a record that still says none. A record that
    moved meanwhile (another copy already promoted these documents) simply
    enqueues nothing.

    The payload NAMES the harvest, so the job promotes this invitation's
    documents even when the record it claims belongs to another invitation's
    (the recent-project link, 4.6); with no harvest the job falls back to the
    record's, exactly as before."""
    if not _entries_with_files(harvest):
        return False
    try:
        if _mark_files_pending(sb, project_id, prior_status):
            try:
                llm_queue.enqueue(
                    JOB_FILES, target_id=project_id, project_id=project_id,
                    payload={"project_id": project_id,
                             "harvest_id": (harvest or {}).get("id")},
                    created_by=actor_id,
                    priority=settings.rfp_create_files_queue_priority, settings=settings,
                    raise_on_active=True,
                )
            except llm_queue.JobAlreadyActive:
                pass
            except Exception:
                _unmark_files_pending(sb, project_id, prior_status)
                raise
            return True
    except Exception as exc:  # noqa: BLE001 - "Retry documents" on the page re-enqueues
        logger.exception("rfp create: files job enqueue failed for %s", project_id)
        _note_error(sb, project_id, f"Document promotion could not be queued: {exc}")
    return False


def _load_files_record(sb, project_id: str) -> dict | None:
    rows = sb.table(TABLE).select("*").eq("project_id", project_id).limit(1).execute().data or []
    return rows[0] if rows else None


def _ensure_files_record(
    sb, source: _Source, harvest: dict | None, project_id: str, *, actor_id: str | None,
    automatic: bool, marker: tuple[bool, str | None], invitation_method: str | None,
    sender_display: str | None, test_session_id: str | None,
) -> dict | None:
    """The `rfp_created_projects` record the promotion job claims and reports
    through, for the recent-project link (4.6). The job resolves its claim,
    its status and its "Retry documents" button from this record, so a target
    project that has NONE (a project someone made through New Bid) would take
    the link and silently drop this invitation's documents. It gets one,
    keyed to THIS harvest, which also gives the promotion a card to report on.

    A target that already has a record (another RFP invitation made it) keeps
    it exactly as it is: the job is told which harvest to promote in its
    payload instead. `project_id` is the record's primary key, so a copy
    racing here loses the insert and this returns the row that won.

    `gc_plan` is `none`: this call attached no GC, the invitation that made
    the project did. None when the record could not be written."""
    existing = _load_files_record(sb, project_id)
    if existing is not None:
        return existing
    payload = {
        "project_id": project_id,
        "source_kind": source.kind,
        "rfp_email_id": source.row["id"] if source.kind == SOURCE_EMAIL else None,
        "portal_invitation_id": source.row["id"] if source.kind == SOURCE_PORTAL else None,
        "harvest_id": (harvest or {}).get("id"),
        "created_by": actor_id,
        "automatic": bool(automatic),
        "invitation_method": invitation_method,
        "sender_display": sender_display,
        "sender_was_unauthorized": bool(marker[0]),
        "sender_allowed_by": marker[1],
        "gc_plan": GC_NONE,
        "bid_time_unknown": False,
        "files_status": FILES_NONE,
        "split_status": (harvest or {}).get("split_status"),
        "split_job_id": (harvest or {}).get("split_job_id"),
        **({"test_session_id": test_session_id} if test_session_id else {}),
    }
    try:
        return sb.table(TABLE).insert(payload).execute().data[0]
    except Exception:  # noqa: BLE001 - a racing copy inserted it first, or the write failed
        logger.exception("rfp create: created-project record insert failed for %s", project_id)
        return _load_files_record(sb, project_id)


def _link_files_job(
    sb, settings: Settings, source: _Source, harvest: dict | None, project_id: str,
    actor_id: str | None, *, automatic: bool, marker: tuple[bool, str | None],
    invitation_method: str | None, sender_display: str | None, test_session_id: str | None,
) -> bool:
    """Step 8 for the recent-project link (4.6): THIS row's harvest documents
    into the project that already exists. Ensures the record the job needs,
    then enqueues from whatever status that record carries."""
    if not _entries_with_files(harvest):
        return False
    record = _ensure_files_record(
        sb, source, harvest, project_id, actor_id=actor_id, automatic=automatic, marker=marker,
        invitation_method=invitation_method, sender_display=sender_display,
        test_session_id=test_session_id,
    )
    if record is None:
        return False
    prior = record.get("files_status") or FILES_NONE
    if prior not in _FILES_ENQUEUEABLE:
        # A promotion of the other invitation's documents holds the record,
        # and the queue allows one active job per project. Say so on the card;
        # "Retry documents" enqueues these once that run lets go.
        logger.warning(
            "rfp create: documents for harvest %s wait on project %s (files_status %s)",
            (harvest or {}).get("id"), project_id, prior,
        )
        _note_error(sb, project_id, _MSG_FILES_BUSY)
        return False
    return _enqueue_files_job(
        sb, settings, harvest, project_id, actor_id, prior_status=prior,
    )


def _create(
    sb, settings: Settings, source: _Source, harvest: dict | None, facts: Facts, plan: GcPlan,
    *, actor_id: str | None, automatic: bool, marker: tuple[bool, str | None],
    invitation_method: str | None, sender_display: str | None,
) -> Created:
    from app.services import project_intake

    row = source.row
    harvest_id = harvest.get("id") if harvest else None
    test_session_id = rfp_test.session_for_email(row)

    # 4.2: the harvest's project, or its claim.
    token: str | None = None
    if harvest:
        if harvest.get("project_id"):
            return _link_existing(sb, source, harvest["project_id"], actor_id)
        # A split still going to file these documents (a mate row at
        # `split`, or a live staging claim): creating now would build the
        # project from whole files the split then never re-files the normal
        # way. Wait for it (docs/RFP_SPLIT.md 10.5).
        if rfp_split.creation_must_wait(sb, harvest, settings=settings, exclude=(source.table, str(row["id"]))):
            raise CreateWaitingForSplit(rfp_split.MSG_CREATE_WAITS)
        token = uuid.uuid4().hex
        if not _claim_harvest(sb, harvest_id, token, settings):
            fresh = _load_harvest(sb, harvest_id) or {}
            if fresh.get("project_id"):
                return _link_existing(sb, source, fresh["project_id"], actor_id)
            raise CreateInProgress(_MSG_IN_PROGRESS)

    # 4.6: the last read before the insert. An email whose copy already made
    # the project (or whose project was made minutes ago for the same GC)
    # joins it instead of making a second one. Email rows only: a portal
    # invitation has no sender, no subject and no copies.
    if source.kind == SOURCE_EMAIL:
        duplicate = duplicate_project_for(sb, row, settings)
        if duplicate is not None:
            if duplicate.why == WHY_RECENT_PROJECT and not automatic:
                # A person is creating: never attach their click to a project
                # the app only inferred. Say what exists and let them decide.
                _release_claim(sb, harvest_id, token, project_id=None)
                raise CreateRefused(recent_project_sentence(duplicate.project))
            made = _link_duplicate(
                sb, source, duplicate, actor_id, test_session_id=test_session_id,
            )
            if duplicate.why == WHY_RECENT_PROJECT:
                # This row's OWN harvest holds documents nobody has promoted:
                # a copy did not make this project, a different invitation
                # row did. Steps 7 and 8, against the project that exists.
                _attach_harvest(sb, source, harvest, token, made.project_id)
                files_job = _link_files_job(
                    sb, settings, source, harvest, made.project_id, actor_id,
                    automatic=automatic, marker=marker,
                    invitation_method=invitation_method, sender_display=sender_display,
                    test_session_id=test_session_id,
                )
                return replace(made, files_job=files_job)
            # A copy of this same message made the project, so its harvest
            # (this invitation's documents) is already linked and promoted.
            _release_claim(sb, harvest_id, token, project_id=None)
            return made

    # Step 1: the project row with an assigned number.
    payload = {
        "name": facts.name,
        "actual_bid_at": facts.actual_bid_at,
        "invitation_at": facts.invitation_at,
        "address": facts.address,
        "bidding_url": facts.bidding_url,
        "no_bidding_url": facts.no_bidding_url,
        "bid_notes": facts.bid_notes,
        "notes": facts.notes,
        "is_ngem": facts.is_ngem,
        "is_rebid": False,
        "created_by": actor_id,
        "current_stage": "intake",
        "current_owner_role": Role.ESTIMATING_ADMIN.value,
        # The BuildingConnected columns (migration 0136; null or false for
        # every other source, exactly the column defaults).
        "job_walk_at": facts.job_walk_at,
        "est_start_date": facts.est_start_date,
        "est_finish_date": facts.est_finish_date,
        "project_information": facts.project_information,
        "trade_instructions": facts.trade_instructions,
        "gc_confirm_pending": bool(facts.gc_confirm_pending or plan.kind == GC_PROVISIONAL),
    }
    if facts.files_needed_source:
        # The files flag (D19), raised with the project so the page never
        # sees a BuildingConnected project without it.
        payload["files_needed_source"] = facts.files_needed_source
        payload["files_needed_url"] = facts.files_needed_url
        payload["files_needed_set_at"] = _iso(_now())
    if test_session_id:
        payload["test_session_id"] = test_session_id
    try:
        project = _insert_project(sb, payload, budgetary=bool(facts.is_budgetary))
    except Exception:
        _release_claim(sb, harvest_id, token, project_id=None)
        raise
    project_id = project["id"]
    number = project.get("number") or payload.get("number") or ""

    # Steps 2 to 4: the atomic core, compensated as create_project does. The
    # created record goes in FIRST, right behind the project, so a crash in
    # the GC links or the state seed never leaves a project the "Created
    # from RFPs" page cannot see (the compensation cascades through it).
    gc_write: _GcWrite | None = None
    try:
        sb.table(TABLE).insert(
            {
                "project_id": project_id,
                "source_kind": source.kind,
                "rfp_email_id": row["id"] if source.kind == SOURCE_EMAIL else None,
                "portal_invitation_id": row["id"] if source.kind == SOURCE_PORTAL else None,
                "harvest_id": harvest_id,
                "created_by": actor_id,
                "automatic": bool(automatic),
                "invitation_method": invitation_method,
                "sender_display": sender_display,
                "sender_was_unauthorized": bool(marker[0]),
                "sender_allowed_by": marker[1],
                "gc_plan": plan.kind,
                # A likely GC is named for the page; so is a provisional one
                # (D15), whose confirmation card needs the name too.
                "likely_gc_id": plan.gc_id if plan.kind in (GC_LIKELY, GC_PROVISIONAL) else None,
                "likely_gc_name": plan.name if plan.kind in (GC_LIKELY, GC_PROVISIONAL) else None,
                "bid_time_unknown": bool(facts.bid_time_unknown),
                "files_status": FILES_NONE,
                # The split outcome, denormalized for the page (docs/RFP_SPLIT.md 4).
                "split_status": (harvest or {}).get("split_status"),
                "split_job_id": (harvest or {}).get("split_job_id"),
                **({"test_session_id": test_session_id} if test_session_id else {}),
            }
        ).execute()
        needs_by = _pacific_date(facts.actual_bid_at)
        if plan.kind in (GC_RESOLVED, GC_PROVISIONAL) and plan.gc_id:
            contact_id = plan.contact_id if plan.kind == GC_RESOLVED else None
            if plan.kind == GC_RESOLVED and not contact_id and plan.lead is not None:
                # The BuildingConnected lead (D16): looked up or inserted
                # here, inside the core, so a failed creation takes back a
                # contact it inserted and never one that already existed.
                # Never under a provisional GC: that would turn a similarity
                # guess into a confirmed contact match (gc_aliases step 2).
                contact_id, inserted = ensure_lead_contact(sb, plan.gc_id, plan.lead)
                if inserted:
                    gc_write = _GcWrite(gc_id=plan.gc_id, gc_name=plan.name, contact_id=contact_id,
                                        inserted_contact_id=contact_id)
            _attach_gc(sb, project_id, plan.gc_id, contact_id, needs_by)
        elif plan.kind == GC_CREATED:
            gc_write = _ensure_gc(sb, plan, test_session_id=test_session_id)
            _attach_gc(sb, project_id, gc_write.gc_id, gc_write.contact_id, needs_by)
            if gc_write.reused:
                # An existing GC: attached without a contact and recorded as
                # a likely GC (4.4); `created` means a row this call inserted.
                sb.table(TABLE).update({
                    "gc_plan": GC_LIKELY,
                    "likely_gc_id": gc_write.gc_id,
                    "likely_gc_name": gc_write.gc_name,
                }).eq("project_id", project_id).execute()
        _seed_state(sb, project_id, actor_id)
    except Exception as exc:
        _compensate(
            sb, project_id, harvest_id, token, actor_id, "rfp_create.create_failed",
            {"source_kind": source.kind, "source_id": row["id"], "number": number,
             "error": str(exc)[:_ERROR_MAX_CHARS]},
            gc_write=gc_write,
        )
        raise

    # Step 5: into Go/No-Go, parked in review (never scored: an empty rubric
    # is an automatic No-Go).
    try:
        workflow.advance_category(project_id, "intake", actor_id, "Created from RFP")
        gono.apply_entry_action(project_id, actor_id, "review")
    except Exception as exc:  # noqa: BLE001 - the project exists; the page shows the error
        logger.exception("rfp create: Go/No-Go advance failed for %s", project_id)
        _note_error(sb, project_id, f"Go/No-Go advance failed: {exc}")

    # Step 6: the source row. A miss is the lost race (4.2).
    if not _cas_source(sb, source, project_id):
        _compensate(
            sb, project_id, harvest_id, token, actor_id, "rfp_create.lost_race",
            {"source_kind": source.kind, "source_id": row["id"], "number": number,
             "harvest_id": harvest_id},
            gc_write=gc_write,
        )
        raise CreateRefused(_MSG_LOST_RACE)

    # Steps 7 and 8: the harvest, the mates and the documents.
    _attach_harvest(sb, source, harvest, token, project_id)
    files_job = _enqueue_files_job(sb, settings, harvest, project_id, actor_id)

    # Steps 9 and 10: the bells, the audit row, the learn-back.
    missing: list[str] = []
    try:
        missing = project_intake.missing_intake_fields(project, bid_time_unknown=facts.bid_time_unknown)
        _notify(sb, project, missing, source.kind, rfp_split.creation_note(sb, harvest))
    except Exception as exc:  # noqa: BLE001
        logger.exception("rfp create: notifications failed for %s", project_id)
        _note_error(sb, project_id, f"Notifications failed: {exc}")
    try:
        audit(actor_id, AUDIT_ACTION, "project", project_id, {
            "source_kind": source.kind, "source_id": row["id"], "harvest_id": harvest_id,
            "number": number, "automatic": bool(automatic),
        })
    except Exception:  # noqa: BLE001
        logger.exception("rfp create: audit failed for %s", project_id)
    try:
        _start_rescan(project_id)
    except Exception:  # noqa: BLE001
        logger.exception("rfp create: learn-back could not start for %s", project_id)
    if test_session_id:
        rfp_test.record(
            sb, session_id=test_session_id, source=rfp_test.SOURCE_CREATE, kind="created",
            title=f"Created {number} {facts.name}", project_id=project_id,
            rfp_email_id=row["id"] if source.kind == SOURCE_EMAIL else None,
            detail={
                "project_id": project_id, "number": number, "name": facts.name,
                "actual_bid_at": facts.actual_bid_at, "bid_time_unknown": facts.bid_time_unknown,
                "invitation_at": facts.invitation_at, "address": facts.address,
                "bidding_url": facts.bidding_url, "automatic": bool(automatic), "actor": actor_id,
                "gc_plan": plan.kind, "gc_id": (gc_write.gc_id if gc_write else plan.gc_id),
                "gc_name": (gc_write.gc_name if gc_write else plan.name),
                "gc_inserted": bool(gc_write and gc_write.inserted_gc_id),
                "contact_inserted": bool(gc_write and gc_write.inserted_contact_id),
                "gc_reused": bool(gc_write and gc_write.reused),
                "harvest_id": harvest_id, "files_job": files_job,
                "files": [
                    {"name": e.get("file_path"), "status": e.get("status"), "error": e.get("error")}
                    for e in ((harvest or {}).get("files") or []) if isinstance(e, dict)
                ][:200],
                "sender_was_unauthorized": bool(marker[0]), "sender_allowed_by": marker[1],
                "invitation_method": invitation_method, "missing_intake": list(missing or []),
                "notifications": [Role.EXECUTIVE.value, Role.ESTIMATING_ADMIN.value],
            },
        )
    return Created(project_id=project_id, number=number, linked=False, files_job=files_job)


def create_from_email(sb, row: dict, *, actor_id: str | None, automatic: bool) -> Created:
    """A project from an email invitation row (at `create` from the pipeline,
    `done` from the button). Raises CreateRefused with a sentence, or
    CreateInProgress while another worker holds the harvest's claim."""
    try:
        return _create_from_email(sb, row, actor_id=actor_id, automatic=automatic)
    except (CreateInProgress, CreateWaitingForSplit):
        # Waits, not failures: the create step records its own bench line.
        raise
    except Exception as exc:
        # A test row records its refusal or failure (docs/RFP_TESTING.md 7.2);
        # the sweep and the button both come through here.
        if rfp_test.session_for_email(row):
            rfp_test.record(
                sb, session_id=row["test_session_id"], source=rfp_test.SOURCE_CREATE, kind="failed",
                level=rfp_test.LEVEL_WARN if isinstance(exc, CreateRefused) else rfp_test.LEVEL_ERROR,
                title=f"Create failed: {str(exc)[:160]}", rfp_email_id=row["id"],
                detail={"error": str(exc)[:_ERROR_MAX_CHARS], "refused": isinstance(exc, CreateRefused),
                        "automatic": bool(automatic), "actor": actor_id},
            )
        raise


def _create_from_email(sb, row: dict, *, actor_id: str | None, automatic: bool) -> Created:
    settings = get_settings()
    harvest = _load_harvest(sb, row.get("harvest_id"))
    # A deleted project's source never creates it again (docs/PROJECT_DELETE.md
    # 5), checked first so the refusal says why rather than "already created".
    _refuse_if_blocked(sb, SOURCE_EMAIL, row, harvest, settings)
    _check_email(row)
    facts = facts_for_email(row, harvest, notes_max_chars=settings.rfp_create_notes_max_chars)
    plan = gc_plan_for(sb, row, harvest)
    marker = sender_marker(sb, row)
    display = f"{row.get('from_name') or ''} <{row.get('from_address') or ''}>".strip()
    source = _Source(kind=SOURCE_EMAIL, table=EMAILS_TABLE, row=row, expected_status=row["status"])
    return _create(
        sb, settings, source, harvest, facts, plan, actor_id=actor_id, automatic=automatic,
        marker=marker, invitation_method=row.get("invitation_method"), sender_display=display,
    )


def create_from_portal(sb, inv: dict, *, actor_id: str | None, automatic: bool) -> Created:
    """A project from a portal invitation (at `create` or `done`), by portal:

    - NGEM: no GC (the agency is the owner), no sender marker, `is_ngem`
      set, the harvest's documents promoted;
    - BuildingConnected: the facts off the mirrored opportunity, the GC
      from the invitation's resolution (resolved or provisional, with the
      lead as its contact), the lead as the sender display, the files flag
      raised (no harvest exists: the platform hands over no file)."""
    settings = get_settings()
    _refuse_if_blocked(
        sb, SOURCE_PORTAL, inv,
        None if inv.get("portal") == PORTAL_BC else _load_harvest(sb, inv.get("harvest_id")),
        settings,
    )
    _check_portal(inv)
    source = _Source(kind=SOURCE_PORTAL, table=INVITATIONS_TABLE, row=inv, expected_status=inv["status"])
    if inv.get("portal") == PORTAL_BC:
        facts = facts_for_bc(
            inv,
            text_max_chars=int(getattr(settings, "rfp_bc_text_max_chars", 20000) or 20000),
            notes_max_chars=settings.rfp_create_notes_max_chars,
        )
        plan = gc_plan_for_bc(sb, inv)
        return _create(
            sb, settings, source, None, facts, plan, actor_id=actor_id,
            automatic=automatic, marker=(False, None), invitation_method=PORTAL_BC,
            sender_display=facts.sender_display,
        )
    harvest = _load_harvest(sb, inv.get("harvest_id"))
    facts = facts_for_portal(inv, harvest, notes_max_chars=settings.rfp_create_notes_max_chars)
    return _create(
        sb, settings, source, harvest, facts, GcPlan(kind=GC_NONE), actor_id=actor_id,
        automatic=automatic, marker=(False, None), invitation_method=inv.get("portal"),
        sender_display=None,
    )


# ── The project side of a BuildingConnected row (contract 3.5) ───────────────


def _proposal_send():
    """app/services/proposal_send, imported on first use (it pulls the
    document and mail stack in); one seam for the tests."""
    from app.services import proposal_send

    return proposal_send


def _insert_contact_link(sb, project_gc_id: str, contact_id: str) -> str | None:
    """project_gc_contacts INSERT ... ON CONFLICT DO NOTHING (the pair is
    unique, 0110): the new row's id, None when it was already there."""
    row = {"project_gc_id": project_gc_id, "gc_contact_id": contact_id}
    try:
        resp = (
            sb.table("project_gc_contacts")
            .upsert(row, on_conflict="project_gc_id,gc_contact_id", ignore_duplicates=True)
            .execute()
        )
    except Exception as exc:  # noqa: BLE001
        if _is_unique_violation(exc):
            return None
        raise
    data = resp.data or []
    return data[0].get("id") if data else None


def _project_gc_link(sb, project_id: str, gc_id: str) -> dict | None:
    rows = (
        sb.table("project_gcs")
        .select("id, gc_id, needs_by")
        .eq("project_id", project_id)
        .eq("gc_id", gc_id)
        .limit(1)
        .execute()
    ).data or []
    return rows[0] if rows else None


def _apply_date_value(field: str, value: Any) -> Any:
    """A date column value as the PATCH would store it: the two DATE columns
    take the Pacific calendar day (an instant is converted, a `YYYY-MM-DD`
    kept), the instants are kept as given; None clears."""
    if value is None:
        return None
    if field in _APPLY_DATE_ONLY_FIELDS:
        text = str(value).strip()
        if _DATE_ONLY_RE.match(text):
            return text
        return _pacific_date(text)
    parsed = _parse_ts(value) if isinstance(value, str) else None
    return _iso(parsed) if parsed else value


def _settle_intake_after_dates(
    sb, project_id: str, updated: dict, patch: dict, actor_id: str | None, *, source: str
) -> None:
    """routers/projects._settle_rfp_intake's rule, against the caller's
    client: a written actual bid date settles its time marker (unknown
    only when the new instant is midnight Pacific, D17), and once nothing
    is missing the Estimating Admin's intake bell is dismissed and a score
    at the Go threshold auto-Goes the project (gono.auto_go_if_scored).
    Best effort: the dates are already committed."""
    from app.services import project_intake

    try:
        rows = (
            sb.table(TABLE).select("project_id, bid_time_unknown").eq("project_id", project_id)
            .limit(1).execute()
        ).data or []
        if not rows:
            return
        bid_time_unknown = bool(rows[0].get("bid_time_unknown"))
        if "actual_bid_at" in patch:
            from app.services import bc_facts

            value = patch.get("actual_bid_at")
            now_unknown = bool(value) and bc_facts.is_midnight_pacific(value)
            if now_unknown != bid_time_unknown:
                sb.table(TABLE).update({"bid_time_unknown": now_unknown}).eq(
                    "project_id", project_id
                ).execute()
            bid_time_unknown = now_unknown
        if not project_intake.missing_intake_fields(updated, bid_time_unknown=bid_time_unknown):
            dismiss_notifications(project_id=project_id, types=[NOTIFY_INTAKE_NEEDED])
            gono.auto_go_if_scored(project_id, actor_id)
    except Exception:  # noqa: BLE001
        logger.exception("rfp create: intake settlement after %s dates failed for %s", source, project_id)


def apply_project_dates(sb, project_id: str, patch: dict, actor_id: str | None, *, source: str) -> dict:
    """The GC moved a date (docs/RFP_BUILDINGCONNECTED.md 3.6, contract
    D20): write the ticked project date columns with the PATCH's semantics
    (the DATE columns as Pacific days, the intake settlement, one audit
    row `project.apply_bc_dates`), never `internal_bid_at`. Returns the
    updated projects row. ValueError when nothing applicable was given,
    LookupError when the project does not exist."""
    clean = {
        field: _apply_date_value(field, patch[field])
        for field in APPLY_DATE_FIELDS
        if field in (patch or {})
    }
    if not clean:
        raise ValueError(_MSG_NO_DATE_FIELDS)
    updated = (sb.table("projects").update(clean).eq("id", project_id).execute()).data or []
    if not updated:
        raise LookupError("Project not found")
    project = updated[0]
    try:
        audit(actor_id, AUDIT_APPLY_DATES_ACTION, "project", project_id, {**clean, "source": source})
    except Exception:  # noqa: BLE001
        logger.exception("rfp create: apply-dates audit failed for %s", project_id)
    _settle_intake_after_dates(sb, project_id, project, clean, actor_id, source=source)
    return project


def swap_project_gc(
    sb, project_id: str, old_gc_id: str | None, new_gc_id: str, lead: dict | None, actor_id: str | None,
) -> dict:
    """The GC confirmation on a project whose provisional GC was wrong
    (docs/RFP_BUILDINGCONNECTED.md 3.4, contract 3.5): the old link is
    removed by id unless a proposal was already sent to it (then both stay
    and the card says so), the confirmed GC is linked (its `needs_by`
    carried over from the old link) with the lead as its bid contact, and
    `gc_confirm_pending` clears. When the old link was removed, the lead
    contact filed under the old GC is deleted too if the creation made it
    (created at or after the project) and nothing else refers to it
    (`_remove_stray_lead_contact`). Returns {removed_old, project_gc_id,
    removed_contact}."""
    if not new_gc_id:
        raise ValueError(_MSG_GC_REQUIRED)
    removed_old = False
    needs_by = None
    old_link = _project_gc_link(sb, project_id, old_gc_id) if old_gc_id and old_gc_id != new_gc_id else None
    if old_link is not None:
        needs_by = old_link.get("needs_by")
        proposal_send = _proposal_send()
        try:
            removed_old = bool(
                proposal_send.remove_gc_link(
                    project_id, old_gc_id, link_id=old_link["id"], refuse_if_sent=True, via_unmerge=True,
                )
            )
        except proposal_send.ProposalSendError as exc:
            # A proposal went to the provisional GC: the link stays beside
            # the confirmed one (never a silent detach of a sent bid).
            logger.info("rfp create: kept GC %s on project %s: %s", old_gc_id, project_id, exc)
            removed_old = False
    link = _project_gc_link(sb, project_id, new_gc_id)
    if link is None:
        link = (
            sb.table("project_gcs")
            .insert({"project_id": project_id, "gc_id": new_gc_id, "needs_by": needs_by})
            .execute()
        ).data[0]
    elif needs_by and not link.get("needs_by"):
        sb.table("project_gcs").update({"needs_by": needs_by}).eq("id", link["id"]).execute()
    contact_id, _inserted = ensure_lead_contact(sb, new_gc_id, lead)
    if contact_id:
        _insert_contact_link(sb, link["id"], contact_id)
    sb.table("projects").update({"gc_confirm_pending": False}).eq("id", project_id).execute()
    removed_contact_id = None
    if removed_old and old_gc_id and old_gc_id != new_gc_id:
        removed_contact_id = _remove_stray_lead_contact(sb, project_id, old_gc_id, lead, old_link["id"])
    try:
        audit(actor_id, AUDIT_GC_SWAP_ACTION, "project", project_id, {
            "old_gc_id": old_gc_id, "new_gc_id": new_gc_id, "removed_old": removed_old,
            "project_gc_id": link["id"], "contact_id": contact_id,
            "removed_contact_id": removed_contact_id,
        })
    except Exception:  # noqa: BLE001
        logger.exception("rfp create: GC swap audit failed for %s", project_id)
    return {
        "removed_old": removed_old,
        "project_gc_id": link["id"],
        "removed_contact": removed_contact_id is not None,
    }


# Columns that point at a gc_contacts row by id (0068 rfis, 0122 rfp_emails
# and rfp_project_matches; project_gc_contacts 0110 is read on its own), and
# the jsonb send snapshots that name one as [{<key>: id, ...}] (0110
# proposal_send_events CC, 0071 rfi_sends, 0081 submittal_packages). rfq_sends
# (0020, 0096) names vendor contacts only.
_CONTACT_ID_COLUMNS = (
    ("rfis", "assigned_contact_id"),
    ("rfp_emails", "resolved_gc_contact_id"),
    ("rfp_project_matches", "contact_selected_id"),
)
_CONTACT_SNAPSHOT_COLUMNS = (
    ("proposal_send_events", "cc_recipients", "gc_contact_id"),
    ("rfi_sends", "recipients", "contact_id"),
    ("submittal_packages", "recipients", "contact_id"),
    ("submittal_packages", "cc_recipients", "contact_id"),
)


def _contact_referenced(sb, contact_id: str, gc_id: str, email: str, removed_link_id: str) -> bool:
    """Whether anything but the just-removed project link still refers to the
    contact: another project's bid-contact row, an id column, a send snapshot,
    or a proposal To line at its GC (proposal_sends and proposal_send_events
    record the recipients as text, not by id)."""
    links = (
        sb.table("project_gc_contacts").select("project_gc_id").eq("gc_contact_id", contact_id).execute()
    ).data or []
    if any(r.get("project_gc_id") != removed_link_id for r in links):
        return True
    for table, column in _CONTACT_ID_COLUMNS:
        if (sb.table(table).select("id").eq(column, contact_id).limit(1).execute()).data:
            return True
    for table, column, key in _CONTACT_SNAPSHOT_COLUMNS:
        # A string, so PostgREST gets `cs.[{...}]` (jsonb @>) verbatim.
        needle = json.dumps([{key: contact_id}])
        if (sb.table(table).select("id").contains(column, needle).limit(1).execute()).data:
            return True
    pattern = f"%{_like_literal(email)}%"
    sent = (
        sb.table("proposal_sends").select("id").eq("gc_id", gc_id)
        .in_("status", ["sending", "sent"]).ilike("gc_email", pattern).limit(1).execute()
    ).data
    if sent:
        return True
    events = (
        sb.table("proposal_send_events").select("id").eq("gc_id", gc_id)
        .ilike("recipients", pattern).limit(1).execute()
    ).data
    return bool(events)


def _made_by_the_create(sb, project_id: str, contact: dict) -> bool:
    """Whether the contact is one the project's creation inserted: its
    `created_at` is at or after the project's (the create inserts the project
    first, then the lead contact). A missing or unreadable timestamp on
    either side proves nothing, so the contact counts as pre-existing."""
    contact_at = _parse_ts(contact.get("created_at"))
    if contact_at is None:
        return False
    rows = (
        sb.table("projects").select("created_at").eq("id", project_id).limit(1).execute()
    ).data or []
    project_at = _parse_ts(rows[0].get("created_at")) if rows else None
    return project_at is not None and contact_at >= project_at


def _remove_stray_lead_contact(
    sb, project_id: str, old_gc_id: str, lead: dict | None, removed_link_id: str,
) -> str | None:
    """After the provisional GC's link is removed, the lead contact the
    creation filed under that GC (`ensure_lead_contact`'s lookup: same GC,
    the lead's email) goes too, but only when the creation made it (created
    at or after the project; an older contact the creation merely reused is
    kept even when nothing refers to it) and nothing else refers to it. Only
    that one row under `old_gc_id` is ever a candidate. Best effort: any
    error keeps the contact. The deleted contact's id, or None."""
    email = _lead_email(lead)
    if not email:
        return None
    try:
        contact = _find_lead_contact_row(sb, old_gc_id, email)
        if not contact or not _made_by_the_create(sb, project_id, contact):
            return None
        contact_id = contact["id"]
        if _contact_referenced(sb, contact_id, old_gc_id, email, removed_link_id):
            return None
        deleted = (
            sb.table("gc_contacts").delete().eq("id", contact_id).eq("gc_id", old_gc_id).execute()
        ).data or []
    except Exception:  # noqa: BLE001
        logger.exception("rfp create: stray lead contact cleanup failed under GC %s", old_gc_id)
        return None
    return contact_id if deleted else None


# ── Reads for the routers ────────────────────────────────────────────────────


def created_project_ref(sb, project_id: str | None) -> dict | None:
    """`{id, number, name}` for a created_project_id, or None."""
    if not project_id:
        return None
    rows = (
        sb.table("projects").select("id, number, name").eq("id", project_id).limit(1).execute()
    ).data or []
    if not rows:
        return None
    return {"id": rows[0]["id"], "number": rows[0].get("number"), "name": rows[0].get("name")}


def email_create_available(row: dict) -> bool:
    """Whether the detail's "Create project" button shows: a `done` row with a
    name, not a sibling follower, no project yet."""
    return (
        row.get("status") == STATUS_DONE
        and has_project_name(row)
        and row.get("flag_reason") != FLAG_SIBLING
        and not row.get("created_project_id")
        # A deleted project's source (docs/PROJECT_DELETE.md 5).
        and not row.get(BLOCK_COLUMN)
    )


def portal_create_available(inv: dict) -> bool:
    """Whether the detail's "Create project" button shows. NGEM: a `done`
    row with a title and no project. BuildingConnected (contract D18): a
    row at `create` or `done` (creation is button-first while the auto
    switch is off, and a no-due-date row a reviewer moved on by hand sits
    at `create`), a title, no project, a GC that is not `none` (a NULL
    gc_kind, a row parked before resolution, counts as `none`), and not
    the NDA-masked shape."""
    if inv.get("portal") == PORTAL_BC:
        return (
            inv.get("status") in CREATABLE_STATUSES
            and bool(_text(inv.get("title")))
            and not inv.get("created_project_id")
            and not _bc_gc_kind_is_none(inv)
            and inv.get("flag_reason") != FLAG_NDA_REQUIRED
            and not inv.get(BLOCK_COLUMN)
        )
    return (
        inv.get("status") == STATUS_DONE
        and bool(_text(inv.get("title")))
        and not inv.get("created_project_id")
        and not inv.get(BLOCK_COLUMN)
    )
