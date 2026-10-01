"""Pydantic request/response models for the BDR API."""

import re
from datetime import date, datetime, time
from decimal import Decimal
from typing import Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    EmailStr,
    Field,
    computed_field,
    field_validator,
    model_validator,
)

from app.core.roles import Role
from app.services.due_reminder_prefs import NotificationPrefsDoc
from app.services.project_status import ProjectStatus

# ── Profiles / users ──────────────────────────────────────────────────────

# Account lifecycle, derived from is_active + invite_accepted_at:
#   "disabled" - admin turned the account off (is_active = false)
#   "invited"  - invite email sent, user hasn't accepted it yet
#   "active"   - user accepted the invite and has authenticated
UserStatus = Literal["active", "invited", "disabled"]

# Supported UI / notification languages. Mirrors SUPPORTED_LOCALES in the
# frontend (bdr_fe/lib/locales.ts) and the profiles.locale CHECK constraint
# (migration 0040) - keep all three in sync when adding a language.
SupportedLocale = Literal["en", "fil", "ceb", "sw", "hi", "ur"]

# Deliberately plain: this validates a MAILBOX WE OWN, typed by an admin on
# Settings > Users, not an arbitrary internet address, so the useful check is
# "one @, a dotted domain, nothing exotic" rather than a full RFC grammar.
#
# The excluded character class is not cosmetic. These values are compared as a
# PostgREST array literal (`mailboxes=ov.{a,b}`), which the client builds by
# joining them raw, so a comma, a quote, a brace or a backslash inside one
# value would be read as a SEPARATOR and silently widen the scope of the
# query: `a,tmoore@g3electrical.com` would let the holder list rows for a
# mailbox they do not own. Whitespace is out for the same reason.
# rfp_email_visibility.apply_scope drops the same characters again, so a row
# written before this validator existed cannot reach PostgREST either.
_MAILBOX_FORBIDDEN = set(',"\'{}\\')
_EMAIL_RE = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+")


class ProfileOut(BaseModel):
    id: str
    full_name: str
    email: str
    role: Role
    is_active: bool
    is_dev: bool = False
    # Cached "user has a verified TOTP factor" flag (migration 0045). Defaults
    # false so reads degrade gracefully before the column is deployed. The
    # backend self-stamps it true on the user's first AAL2 request; the 2FA-reset
    # endpoints clear it. Surfaces a "2FA on/off" indicator in the admin list.
    mfa_enrolled: bool = False
    invite_accepted_at: datetime | None = None
    # Defaults to English so reads degrade gracefully if migration 0040 hasn't
    # been applied yet.
    locale: SupportedLocale = "en"
    # When this user first finished the estimator portal tour (migration 0092).
    # NULL/absent = offer it. Defaults to None so reads degrade gracefully
    # before the column is deployed - the portal then offers the tour, which is
    # the safe direction to be wrong in.
    estimator_tour_completed_at: datetime | None = None
    # The RFP mailboxes this person owns (migration 0134,
    # docs/RFP_EMAIL_VISIBILITY.md): which rows of the RFP review queue they
    # may see and act on. Defaults to empty so reads degrade gracefully before
    # the column is deployed, and because empty is the safe direction: the
    # person then sees only the shared mailboxes.
    rfp_mailboxes: list[str] = Field(default_factory=list)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def status(self) -> UserStatus:
        if not self.is_active:
            return "disabled"
        return "active" if self.invite_accepted_at else "invited"


class TeammateOut(BaseModel):
    """Minimal profile for pickers any internal user may see (e.g. To-Dos)."""

    id: str
    full_name: str
    email: str
    role: Role


class InviteUserIn(BaseModel):
    email: EmailStr
    full_name: str
    role: Role


class RoleSwitchIn(BaseModel):
    role: Role


class AdminUpdateUserIn(BaseModel):
    """An admin's edits to ANOTHER user's account (IT Admin / Executive).

    Every field is optional so the caller PATCHes only what changed. Name and
    email are the fields the user cannot fix themselves (`UpdateMeIn` covers
    self-service name and locale, never email). Changing the email rewrites the
    Supabase Auth login address as well as the profile row, so it is deliberately
    admin-only.
    """

    full_name: str | None = Field(default=None, min_length=1, max_length=120)
    email: EmailStr | None = None
    role: Role | None = None
    is_active: bool | None = None
    is_dev: bool | None = None
    # The RFP mailboxes this user owns (docs/RFP_EMAIL_VISIBILITY.md 3.5).
    # None leaves the mapping alone; [] clears it. The router refuses the list
    # for an external estimator, since that role never reaches the queue.
    rfp_mailboxes: list[str] | None = None

    @field_validator("full_name")
    @classmethod
    def _strip_name(cls, v: str | None) -> str | None:
        if v is None:
            return v
        v = v.strip()
        if not v:
            raise ValueError("full_name must not be blank")
        return v

    @field_validator("rfp_mailboxes")
    @classmethod
    def _clean_mailboxes(cls, v: list[str] | None) -> list[str] | None:
        """Trim, lowercase, drop blanks, dedupe (first spelling wins), and
        refuse anything that is not an address or a list longer than ten.

        The cap is a sanity bound, not a policy: a person owns a handful of
        mailboxes, and an unbounded array would be sent on every profile read
        and compared on every visibility check.
        """
        if v is None:
            return None
        out: list[str] = []
        for raw in v:
            if not isinstance(raw, str):
                raise ValueError("rfp_mailboxes must be email addresses")
            addr = raw.strip().lower()
            if not addr:
                continue
            if (
                not _EMAIL_RE.fullmatch(addr)
                or _MAILBOX_FORBIDDEN & set(addr)
                or any(ch.isspace() for ch in addr)
            ):
                raise ValueError(f"{raw.strip()} is not a valid email address")
            if addr not in out:
                out.append(addr)
        if len(out) > 10:
            raise ValueError("rfp_mailboxes is limited to 10 addresses")
        return out


class UpdateMeIn(BaseModel):
    """Self-service profile edits - display name and UI language. Each field is
    optional so the caller can PATCH just the name or just the locale; email and
    role stay admin-managed."""

    full_name: str | None = Field(default=None, min_length=1, max_length=120)
    locale: SupportedLocale | None = None

    @field_validator("full_name")
    @classmethod
    def _strip_name(cls, v: str | None) -> str | None:
        if v is None:
            return v
        v = v.strip()
        if not v:
            raise ValueError("full_name must not be blank")
        return v


class NotificationPrefsOut(BaseModel):
    """Effective due-date reminder prefs + whether a custom row exists.

    `is_customized` drives the Settings page's "Reset to default" button -
    true iff the user has a notification_prefs row stored.
    """

    prefs: NotificationPrefsDoc
    is_customized: bool


# ── General contractors ─────────────────────────────────────────────────--


class GCIn(BaseModel):
    name: str


class GCOut(GCIn):
    id: str


class GCContactIn(BaseModel):
    gc_id: str
    name: str
    email: EmailStr | None = None  # nullable: proposal sends need it, the directory doesn't
    phone: str | None = None


class GCContactOut(GCContactIn):
    id: str


# ── Projects ────────────────────────────────────────────────────────────--

# Free text (migration 0049). The old enum values ('day_work' / 'night_work' and
# 'prevailing_wage' / 'non_prevailing_wage') are still the picker's suggested
# options, but estimators may store any custom string a project calls for.
LaborTime = str
WageType = str

# Project Management lifecycle (migration 0057). A SEPARATE axis from the bidding
# pipeline's current_stage: a won project keeps current_stage='bid_outcome' forever
# while pm_stage tracks its construction life. NULL pm_stage = not in PM.
PMStage = Literal["precon", "active_construction", "closeout"]
PMOrigin = Literal["bid", "direct"]

# Go/No-Go scoring answers. The rubric labels live in the frontend
# (bdr_fe/lib/gonoScoring.ts); the points are mirrored by the backend scorer
# (app/services/gono.py), which decides the Go/No-Go outcome from the total.
# These Literals are the value lists verbatim and must stay in sync with both.
ProjectType = Literal[
    "new_construction",
    "ti",
    "multi_family",
    "casino_strip",
    "casino_other",
    "lighting",
    "roadway",
    "generator",
    "other",
    "unknown",
]
OwnerType = Literal[
    "rtc",
    "doa",
    "ccsd",
    "public_other",
    "casino_strip",
    "casino_other",
    "private_commercial",
    "private_residential",
    "other",
    "unknown",
]
LaborNeeded = Literal["union", "ce_cw", "ce", "cw", "non_union", "other", "unknown"]
BidMethod = Literal["hard_bid", "cmar", "single_gc_hard_bid", "other", "unknown"]
CompetitorKnown = Literal["yes_1_2", "yes_3_plus", "no_unknown", "only_ec_bidding", "other"]
GCKnown = Literal[
    "yes_1_2",
    "yes_3_plus",
    "no_unknown",
    "only_gc_bidding",
    "no_gc_needed",
    "other",
]
SubsNeeded = Literal[
    "no",
    "yes_underground",
    "yes_low_voltage",
    "yes_fire_alarm",
    "two_subs",
    "three_plus_subs",
    "other",
    "unknown",
]
EstValueBand = Literal[
    "under_50k", "50k_150k", "150k_500k", "500k_1m", "1m_3m", "over_3m", "other", "unknown"
]
ScopeFit = Literal["yes", "no", "maybe", "other", "unknown"]


# Membership is just the link - any GC on a project is a bid candidate; who
# we actually bid to is recorded by which proposals were sent (Send Out).
# needs_by is per-GC because GCs on the same bid can want our number on
# different days. Bounded to a sane window: a stored 9999-12-31 (typo or
# malice) overflows the +1-day deadline math in the Bid Invitations report and
# would 500 the report for everyone until the row is found and fixed.
_NEEDS_BY_MIN = date(2000, 1, 1)
_NEEDS_BY_MAX = date(2100, 12, 31)


def _check_needs_by(v: date | None) -> date | None:
    if v is not None and not (_NEEDS_BY_MIN <= v <= _NEEDS_BY_MAX):
        raise ValueError("needs_by must be between 2000-01-01 and 2100-12-31")
    return v


class ProjectGCIn(BaseModel):
    gc_id: str
    needs_by: date | None = None
    # The specific people at this GC the team intends to bid to on this
    # project. Advisory: the Send Out dialog seeds and highlights them, but any
    # contact of the GC may still be emailed. None/[] = no specific selection.
    contact_ids: list[str] | None = Field(default=None, max_length=50)

    _needs_by_bounds = field_validator("needs_by")(_check_needs_by)

    @field_validator("contact_ids")
    @classmethod
    def _dedupe_contacts(cls, v: list[str] | None) -> list[str] | None:
        return list(dict.fromkeys(v)) if v is not None else None


class ProjectGCUpdate(BaseModel):
    needs_by: date | None = None
    # Omitted = leave the selection alone; [] = clear it. The handler tells the
    # two apart via model_fields_set, so this None default never clears.
    contact_ids: list[str] | None = Field(default=None, max_length=50)

    _needs_by_bounds = field_validator("needs_by")(_check_needs_by)

    @field_validator("contact_ids")
    @classmethod
    def _dedupe_contacts(cls, v: list[str] | None) -> list[str] | None:
        return list(dict.fromkeys(v)) if v is not None else None


class ProjectOpenIn(BaseModel):
    """POST /projects/{id}/opens - which surface the user opened."""

    kind: Literal["project", "details"]


# The bidding-site link is rendered as an href on the project page, so the
# scheme is allow-listed here rather than trusted: without this, `javascript:`
# or `data:` text typed into the field would become a working XSS payload the
# moment someone clicked the button.
_BIDDING_URL_MAX = 2000
# The two BuildingConnected text blocks on a project (project_information,
# trade_instructions): the same cap the ingestion applies (RFP_BC_TEXT_MAX_CHARS
# tops out here), so a stored value always round-trips through Edit Details.
PROJECT_TEXT_MAX_CHARS = 20000

# Leading "<word>:" - a candidate scheme. Only a candidate: a host with a port
# ("example.com:8080", "localhost:3000") wears the same shape, so what follows
# decides which one it is.
_SCHEME_RE = re.compile(r"^([A-Za-z][A-Za-z0-9+.\-]*):(.*)$", re.DOTALL)


def _clean_bidding_url(value: str | None) -> str | None:
    """Normalize a bidding-site URL; empty/whitespace reads as "not provided".

    People paste what they copied - "app.buildingconnected.com/projects/abc" as
    often as a full link - so a missing scheme is filled in with https:// rather
    than bounced back at them. A URL that names some *other* scheme is still
    refused, because this ends up as an href on the project page.
    """
    if value is None:
        return None
    url = value.strip()
    if not url:
        return None
    if len(url) > _BIDDING_URL_MAX:
        raise ValueError(f"bidding_url may not exceed {_BIDDING_URL_MAX} characters")
    scheme_match = _SCHEME_RE.match(url)
    if scheme_match:
        scheme, rest = scheme_match.group(1).lower(), scheme_match.group(2)
        if scheme in ("http", "https"):
            # Also repairs a fumbled "https:/example.com" / "https:example.com".
            url = f"{scheme}://{rest.lstrip('/')}"
        elif "." in scheme or rest[:1].isdigit():
            # Not a scheme at all - a host with a port, or a host whose TLD the
            # colon follows. Treat it like any other scheme-less paste.
            url = f"https://{url}"
        else:
            raise ValueError("bidding_url must be a web link (http:// or https://)")
    else:
        # "//host/path" is protocol-relative; everything else is a bare host.
        url = f"https://{url.lstrip('/')}" if url.startswith("//") else f"https://{url}"
    # Whatever we built has to have an actual host behind the scheme - "https://"
    # on its own, or a paste that was nothing but slashes, is not a link.
    if not re.match(r"^https?://[^/\s]", url, re.IGNORECASE):
        raise ValueError("bidding_url must be a web link (http:// or https://)")
    return url


class ProjectCreate(BaseModel):
    name: str
    # The number is assigned by the server on save (services/project_numbers,
    # docs/RFP_CREATE.md section 2): nobody types one. An old client that still
    # sends a `number` key is ignored (pydantic's default extra="ignore").
    # `budgetary` appends the B marker to the assigned number.
    budgetary: bool = False
    # Required at intake - mirrored by the New Project form's `required` fields.
    internal_bid_at: datetime
    actual_bid_at: datetime | None = None
    est_start_date: date | None = None
    est_finish_date: date | None = None
    invitation_at: datetime
    labor_time: LaborTime | None = None
    wage_type: WageType | None = None
    labor_note: str | None = None
    due_from_estimator_at: datetime
    due_from_vendors_at: datetime
    notes: str | None = None
    address: str | None = None
    # Link to the site hosting the bid details (BuildingConnected, iSqFt, a GC's
    # own portal). Required at intake: either a URL, or no_bidding_url ticked to
    # say this project has none. The two are mutually exclusive.
    bidding_url: str | None = None
    no_bidding_url: bool = False
    # True when the project came to us from NGEM (checkbox on the intake form).
    is_ngem: bool = False
    # RFP matching (docs/RFP_MATCHING.md 3.9). `bid_notes` is the GC's own
    # instructions about this bid (walk dates, delivery rules, scope notes),
    # kept apart from `notes` because the matcher scores it against the notes
    # extracted from an invitation email. `is_rebid` marks a job that was bid
    # before under another invitation (the New Bid similar-projects check
    # offers "Create as a rebid").
    bid_notes: str | None = None
    is_rebid: bool = False
    # Go/No-Go scoring answers (reference only for scoring, but required at intake)
    project_type: ProjectType
    owner_type: OwnerType
    labor_needed: LaborNeeded
    bid_method: BidMethod
    competitor_known: CompetitorKnown
    gc_known: GCKnown
    subs_needed: SubsNeeded
    est_value_band: EstValueBand
    scope_fit: ScopeFit
    gcs: list[ProjectGCIn] = []

    @field_validator("bidding_url")
    @classmethod
    def _check_bidding_url(cls, v: str | None) -> str | None:
        return _clean_bidding_url(v)

    @model_validator(mode="after")
    def _bidding_url_answered(self) -> "ProjectCreate":
        """Intake must answer the bidding link one way or the other."""
        if self.no_bidding_url and self.bidding_url:
            raise ValueError(
                "Provide a bidding_url or set no_bidding_url - not both"
            )
        if not self.no_bidding_url and not self.bidding_url:
            raise ValueError(
                "A bidding_url is required; set no_bidding_url if the project has no link"
            )
        return self


class ProjectUpdate(BaseModel):
    name: str | None = None
    # The number itself is never edited; `budgetary` adds or strips its B
    # marker (a rename of the same number, refused on a legacy number).
    budgetary: bool | None = None
    internal_bid_at: datetime | None = None
    actual_bid_at: datetime | None = None
    est_start_date: date | None = None
    est_finish_date: date | None = None
    invitation_at: datetime | None = None
    labor_time: LaborTime | None = None
    wage_type: WageType | None = None
    labor_note: str | None = None
    due_from_estimator_at: datetime | None = None
    due_from_vendors_at: datetime | None = None
    notes: str | None = None
    address: str | None = None
    # Cross-field consistency (URL xor "no link") is settled by the router, which
    # is the only place that knows which half of the pair the patch touched.
    bidding_url: str | None = None
    no_bidding_url: bool | None = None
    is_ngem: bool | None = None
    bid_notes: str | None = None
    is_rebid: bool | None = None
    # BuildingConnected facts (migration 0136, docs/RFP_BUILDINGCONNECTED.md
    # 3.7): the job walk instant and the two "Project details" text blocks,
    # capped at the ingestion's own limit (RFP_BC_TEXT_MAX_CHARS) so an
    # untouched field round-trips through Edit Details.
    job_walk_at: datetime | None = None
    project_information: str | None = Field(default=None, max_length=PROJECT_TEXT_MAX_CHARS)
    trade_instructions: str | None = Field(default=None, max_length=PROJECT_TEXT_MAX_CHARS)
    # Go/No-Go scoring answers (reference only)
    project_type: ProjectType | None = None
    owner_type: OwnerType | None = None
    labor_needed: LaborNeeded | None = None
    bid_method: BidMethod | None = None
    competitor_known: CompetitorKnown | None = None
    gc_known: GCKnown | None = None
    subs_needed: SubsNeeded | None = None
    est_value_band: EstValueBand | None = None
    scope_fit: ScopeFit | None = None

    @field_validator("bidding_url")
    @classmethod
    def _check_bidding_url(cls, v: str | None) -> str | None:
        return _clean_bidding_url(v)


class GcCreateIn(BaseModel):
    """The "Add a GC" half of a GC confirmation (docs/RFP_BUILDINGCONNECTED.md
    3.4): the GC's name and, optionally, its contact from the platform's lead."""

    name: str = Field(min_length=1, max_length=200)
    contact_name: str | None = Field(default=None, max_length=200)
    contact_email: str | None = Field(default=None, max_length=320)
    contact_phone: str | None = Field(default=None, max_length=60)


class ProjectGcConfirmIn(BaseModel):
    """POST /projects/{id}/gc-confirm: `same` keeps the provisional GC,
    `pick` names another (`gc_id`), `create` adds one (`create`)."""

    decision: Literal["same", "pick", "create"]
    gc_id: str | None = None
    create: GcCreateIn | None = None

    @model_validator(mode="after")
    def _decision_has_its_argument(self) -> "ProjectGcConfirmIn":
        if self.decision == "pick" and not (self.gc_id or "").strip():
            raise ValueError("gc_id is required to pick a GC")
        if self.decision == "create" and self.create is None:
            raise ValueError("create is required to add a GC")
        return self


class GcRefOut(BaseModel):
    id: str | None = None
    name: str | None = None


class ProjectGcConfirmOut(BaseModel):
    """GET /projects/{id}/gc-confirm: what the project-page card shows
    (contract 3.5). `pending` mirrors projects.gc_confirm_pending; the
    rest comes off the BuildingConnected invitation that made or merged
    onto the project (null when there is none)."""

    pending: bool = False
    external_name: str | None = None
    provisional_gc: GcRefOut | None = None
    candidates: list[dict[str, Any]] = Field(default_factory=list)
    lead: dict[str, Any] | None = None
    invitation_id: str | None = None
    portal: str | None = None


class CategoryStateOut(BaseModel):
    """One category's progress head (source of truth for the bidding board)."""

    category: str
    current_task: str
    status: str  # 'locked' | 'active' | 'complete'
    owner_role: Role | None = None
    completed_at: datetime | None = None


class PortalChangeOut(BaseModel):
    """One tracked change on a BuildingConnected invitation (docs/
    RFP_BUILDINGCONNECTED.md 3.6): the field, the old and new values. The
    values of a `close_at` change are nulled for roles that may not see the
    actual bid date (the router does it)."""

    at: datetime | None = None
    field: str
    old: Any = None
    new: Any = None


class PortalSourceOut(BaseModel):
    """One portal invitation that resolved to `exists` on a project (NGEM),
    or, for BuildingConnected (contract D32), one at `exists` OR the one that
    created the project: the "Also invited through ..." block with the deep
    link, the package trade, the inviting GC and the change list."""

    portal: str
    invitation_id: str
    agency: str | None = None
    bid_number: str | None = None
    bid_number_raw: str | None = None
    addendum_no: int | None = None
    close_at: datetime | None = None
    resolution: str | None = None
    resolved_at: datetime | None = None
    external_url: str | None = None
    trade_name: str | None = None
    invited_at: datetime | None = None
    gc_external_name: str | None = None
    gc_id: str | None = None
    gc_name: str | None = None
    changes: list[PortalChangeOut] = Field(default_factory=list)


class RfpCreatedSummary(BaseModel):
    """The project-header facts about a project the RFP creation step made
    (rfp_created_projects, docs/RFP_CREATE.md section 8): who or what created
    it, the unauthorized-sender marker, and the document promotion outcome.
    `invitation_method` names the source (an email method, `ngem`,
    `buildingconnected`) and `gc_plan` how the GC was decided. The intake
    modal shows where the invitation came from: `source_kind` ('rfp_email' or
    'rfp_portal'), `mailbox` (the receiving mailbox of an email source; null
    for a portal) and the likely GC the creation step settled on."""

    automatic: bool = False
    sender_was_unauthorized: bool = False
    sender_display: str | None = None
    sender_allowed_by_name: str | None = None
    files_status: str = "none"
    files_promoted: int = 0
    bid_time_unknown: bool = False
    invitation_method: str | None = None
    gc_plan: str | None = None
    source_kind: str | None = None
    mailbox: str | None = None
    likely_gc_id: str | None = None
    likely_gc_name: str | None = None
    # The Bid File Splitter flag (docs/RFP_SPLIT.md 10), detail route only:
    # `{state: failed | partial | running, reason, job_id, files_total,
    # files_done, files_failed_count, files_failed: [{file_id, filename,
    # error}], started_at, finished_at, resolution: {kind, by, at} | null,
    # package_sent}`; null when the split succeeded, was skipped, or the
    # route does not compute it.
    split_issue: dict[str, Any] | None = None


class ProjectOut(BaseModel):
    id: str
    name: str
    number: str
    internal_bid_at: datetime | None
    actual_bid_at: datetime | None
    est_start_date: date | None
    est_finish_date: date | None
    invitation_at: datetime | None
    labor_time: LaborTime | None
    wage_type: WageType | None
    labor_note: str | None
    due_from_estimator_at: datetime | None
    due_from_vendors_at: datetime | None = None
    notes: str | None
    address: str | None = None
    # Bidding-site link, and the "this project has no link" answer. Defaults let
    # reads degrade gracefully before migration 0079 is applied; a project
    # created before it reads as unanswered (null + false), which the UI shows
    # as "add a bidding link".
    bidding_url: str | None = None
    no_bidding_url: bool = False
    # True when the project originated from NGEM. Default lets reads degrade
    # gracefully before migration 0046 is applied.
    is_ngem: bool = False
    # RFP matching (migration 0122): the GC's bid instructions and the manual
    # rebid flag. Defaults let reads degrade gracefully before it is applied.
    bid_notes: str | None = None
    is_rebid: bool = False
    # BuildingConnected (migration 0136, docs/RFP_BUILDINGCONNECTED.md 3.7
    # and 3.8): the job walk, the two "Project details" text blocks, the
    # "same GC?" question still open, and the files flag (set / cleared).
    # Defaults let reads degrade gracefully before 0136 is applied.
    job_walk_at: datetime | None = None
    project_information: str | None = None
    trade_instructions: str | None = None
    gc_confirm_pending: bool = False
    files_needed_source: str | None = None
    files_needed_url: str | None = None
    files_needed_set_at: datetime | None = None
    files_needed_cleared_at: datetime | None = None
    # Go/No-Go scoring answers (reference only); defaults so reads degrade
    # gracefully if the 0027 migration hasn't been applied yet.
    project_type: ProjectType | None = None
    owner_type: OwnerType | None = None
    labor_needed: LaborNeeded | None = None
    bid_method: BidMethod | None = None
    competitor_known: CompetitorKnown | None = None
    gc_known: GCKnown | None = None
    subs_needed: SubsNeeded | None = None
    est_value_band: EstValueBand | None = None
    scope_fit: ScopeFit | None = None
    current_stage: str
    current_owner_role: Role | None
    # Abandon marker (set by /abandon, cleared by /reactivate). `status` is
    # derived from these + current_stage + the bid outcome and is populated by
    # the router (it needs the cross-table outcome result). Defaults let reads
    # degrade gracefully before migration 0039 is applied.
    abandoned_at: datetime | None = None
    abandoned_by: str | None = None
    # Set when a post-verify pricing edit bounced the project back to `verify`;
    # holds the stage it will resume at after the Executive re-commits. NULL =
    # not currently in re-verification (see migration 0043 / workflow.reopen_verify).
    reverify_return_stage: str | None = None
    # Per-category progress (the new source of truth), keyed by category
    # ('intake' | 'material_numbers' | 'labor_numbers' | 'send_out'). `current_stage`
    # above is the denormalized headline pointer. Default None lets reads degrade
    # gracefully before migration 0057 is applied / for rows without category state.
    category_state: dict[str, CategoryStateOut] | None = None
    status: ProjectStatus = "active"
    # Project Management lifecycle (migration 0057): pm_stage/pm_origin are set
    # when a won bid enters Precon (or a project is created directly in PM);
    # pm_completed_at mirrors the abandon pattern (preserves pm_stage='closeout').
    # Defaults let reads degrade gracefully before the migration is applied.
    pm_stage: PMStage | None = None
    pm_origin: PMOrigin | None = None
    pm_completed_at: datetime | None = None
    # Last successful files export (drives the post-send-out export banner).
    # Default lets reads degrade gracefully before migration 0041 is applied.
    files_exported_at: datetime | None = None
    # GCs added after the bid went out whose per-GC price change is waiting on
    # an Executive (migration 0118). The dashboard lists the project as an
    # Executive task while this is above zero; the project's stage itself never
    # moves for it.
    gc_pricing_approvals_pending: int = 0
    # RFP matching (docs/RFP_MATCHING.md 3.9). `rfp_merged_count` is the open
    # system merges (a GC the matcher added that is still on the project);
    # `rfp_history_count` is every other rfp_project_matches row (unmerged
    # merges, merges whose link a person removed, duplicates), so any row makes
    # the "Merged by System" modal reachable; `rfp_new_count` is the subset of
    # open merges on a bid that has already gone out whose GC has no sent or
    # sending proposal and is not acknowledged (the "New RFP" pill). Derived
    # by the list and detail routes only; every other route leaves the
    # defaults, as gc_pricing_approvals_pending does.
    rfp_merged_count: int = 0
    rfp_history_count: int = 0
    rfp_new_count: int = 0
    # NGEM portal invitations resolved onto this project (docs/RFP_NGEM_PORTAL.md
    # section 5): the "NGEM" pill and the Portal invitations section. Derived
    # by the detail route only while RFP_NGEM_ENABLED is on; every other
    # route leaves the default, as the counts above do.
    portal_sources: list[PortalSourceOut] = Field(default_factory=list)
    # RFP project creation (docs/RFP_CREATE.md sections 7 and 8): the created
    # row's summary, and the intake fields the Estimating Admin still has to
    # fill (null when the project was not created by that slice, else the
    # possibly empty list). Derived by the list and detail routes only.
    rfp_created: RfpCreatedSummary | None = None
    rfp_intake_missing: list[str] | None = None
    created_by: str | None
    created_at: datetime
    updated_at: datetime


class BidsTodayProjectOut(ProjectOut):
    """A Bids Today row: a full project plus the one page-specific fact the
    client can't derive - whether the bid went out earlier today (rows sent
    today stay on the page with a Sent badge and drop off tomorrow)."""

    sent_today: bool = False


class FilesExportIn(BaseModel):
    """Subset selector for the project-files ZIP export.

    Omit `file_ids` (or send `{}`) to export every file the caller may read;
    `[]` is rejected so "export all" is always explicit, not an accident.
    """

    file_ids: list[str] | None = None
    # Whether this export stamps projects.files_exported_at. Defaults True so the
    # existing "export all" path is unchanged, but the per-batch ZIP download in
    # the Plans & Specs Log passes False: an internal user pulling a 2-file
    # revision batch must NOT set files_exported_at and thereby suppress the
    # post-send-out "export your files to the team" banner for everyone
    # (files.py export_files → app/(app)/projects/[id]/page.tsx). The stamp is
    # additionally gated to non-estimator callers in export_files.
    stamp_exported: bool = True

    @field_validator("file_ids")
    @classmethod
    def _sane(cls, v: list[str] | None) -> list[str] | None:
        if v is None:
            return v
        if len(v) == 0:
            raise ValueError("file_ids cannot be empty; omit it to export all files")
        if len(v) > 1000:
            raise ValueError("Too many files requested in one export")
        return list(dict.fromkeys(v))  # de-dupe, preserve order


class PmDocsExportIn(BaseModel):
    """Subset selector for the unified PM documents ZIP export.

    `keys` are opaque "source:id" handles (e.g. "pm:…", "bid:…", "cp:…") from the
    hub listing. Omit `keys` (or send `{}`) to export every document the caller
    may read; `[]` is rejected so "export all" is always explicit.
    """

    keys: list[str] | None = None

    @field_validator("keys")
    @classmethod
    def _sane(cls, v: list[str] | None) -> list[str] | None:
        if v is None:
            return v
        if len(v) == 0:
            raise ValueError("keys cannot be empty; omit it to export all documents")
        if len(v) > 2000:
            raise ValueError("Too many documents requested in one export")
        return list(dict.fromkeys(v))  # de-dupe, preserve order


# ── Workflow ────────────────────────────────────────────────────────────--


class TransitionIn(BaseModel):
    # The category whose head to advance in the new category model
    # ('intake' | 'material_numbers' | 'labor_numbers' | 'send_out'). Required by
    # /advance. `to_stage` is retained for backward compatibility / logging only and
    # is ignored by the endpoint (the server computes the next task per category).
    category: str | None = None
    to_stage: str | None = None
    note: str | None = None
    # Only honored when advancing into go_no_go: 'score' (default) lets the
    # thresholds decide, 'review' holds the project for a manual decision, and
    # 'go'/'no_go' push the outcome regardless of the score.
    gono_action: Literal["score", "review", "go", "no_go"] = "score"


class AbandonIn(BaseModel):
    """Optional reason captured when a bid is abandoned (stored in the audit log,
    not as a project column)."""

    note: str | None = None

    @field_validator("note")
    @classmethod
    def _note_sane(cls, v: str | None) -> str | None:
        if v is not None and len(v) > 2000:
            raise ValueError("Note must be 2,000 characters or fewer")
        return v


class ProjectDeleteIn(BaseModel):
    """POST /projects/{id}/delete (docs/PROJECT_DELETE.md 6). The reason code,
    the note and the typed confirmation are checked by the service and again
    by the database function, each failure a 422 with a sentence (so these
    fields are plain strings here, capped only to bound the request)."""

    reason: str | None = Field(default=None, max_length=64)
    note: str | None = Field(default=None, max_length=4000)
    confirm_name: str | None = Field(default=None, max_length=1000)


# ── RFP matching (docs/RFP_MATCHING.md sections 3.7 to 3.9) ──────────────

_RFP_REASON_MAX = 500


def _clean_reason(v: str | None) -> str | None:
    """A free-text reason, whitespace-trimmed; blank reads as not given."""
    if v is None:
        return None
    v = v.strip()
    return v or None


class SimilarProjectsIn(BaseModel):
    """POST /projects/similar: the New Bid form's pre-create check.

    Name-only by design: the check scores project names and windows on the
    internal bid date, so no score, membership or sort order in the response
    can be a function of the confidential actual date. Any other key a client
    sends (dates included) is dropped, never read.
    """

    model_config = ConfigDict(extra="ignore")

    name: str = Field(min_length=1, max_length=500)


class RfpMatchMergeIn(BaseModel):
    """POST /rfp-emails/{id}/match/merge: the human "merge into X" action.

    `gc_id` is optional; when given it also becomes the email's resolved GC
    (kind `human`). With neither it nor a resolved GC the service refuses
    with 400 rfp_match_gc_required. Both ids are plain strings here; the
    router refuses a malformed id (and an unknown GC) with a 404 before the
    service runs, so a bad id never reaches PostgREST as a 500."""

    project_id: str = Field(min_length=1, max_length=64)
    gc_id: str | None = Field(default=None, min_length=1, max_length=64)


class RfpMatchDuplicateIn(BaseModel):
    """POST /rfp-emails/{id}/match/duplicate: "the GC is already on X". A
    malformed project_id is a 404 in the router."""

    project_id: str = Field(min_length=1, max_length=64)


class RfpMatchGcIn(BaseModel):
    """POST /rfp-emails/{id}/match/gc: set the email's GC by hand. A
    malformed or unknown gc_id is a 404 in the router."""

    gc_id: str = Field(min_length=1, max_length=64)


class RfpMatchReopenIn(BaseModel):
    """POST /rfp-emails/{id}/match/reopen: optional reason, audit only."""

    reason: str | None = Field(default=None, max_length=_RFP_REASON_MAX)

    _reason_clean = field_validator("reason")(_clean_reason)


class RfpMatchUnmergeIn(BaseModel):
    """Unmerge (by email or by project): the reason is required, 3 to 500
    characters, and is stored on the closed match row."""

    reason: str = Field(min_length=3, max_length=_RFP_REASON_MAX)

    @field_validator("reason")
    @classmethod
    def _reason_required(cls, v: str) -> str:
        v = v.strip()
        if len(v) < 3:
            raise ValueError("A reason of at least 3 characters is required")
        return v


class RfpMatchAcknowledgeIn(BaseModel):
    """POST /projects/{id}/rfp-matches/{match_id}/acknowledge: "no proposal
    needed", optional reason."""

    reason: str | None = Field(default=None, max_length=_RFP_REASON_MAX)

    _reason_clean = field_validator("reason")(_clean_reason)


# ── Go / No-Go ──────────────────────────────────────────────────────────--


class GonoDecisionIn(BaseModel):
    outcome: Literal["go", "no_go"]
    note: str | None = None


# ── Vendors ───────────────────────────────────────────────────────────────


class VendorIn(BaseModel):
    name: str
    notes: str | None = None
    # 0139: a national account vendor. Display only, no pricing effect.
    is_national_account: bool = False


class VendorUpdate(BaseModel):
    """Toggle a vendor company's national account flag (0139)."""

    is_national_account: bool


class VendorContactIn(BaseModel):
    vendor_id: str
    name: str
    email: EmailStr
    phone: str | None = None
    # A contact may quote several trades at their company (0095), so the picker
    # always submits the full set. Empty list = uncategorized.
    material_category_ids: list[str] = Field(default_factory=list)


class VendorContactUpdate(BaseModel):
    """Replace a contact's category set (the picker submits the whole set)."""

    material_category_ids: list[str] = Field(default_factory=list)


# ── RFQs / quotes ─────────────────────────────────────────────────────────


class RFQCreate(BaseModel):
    material_category_id: str
    due_date: date | None = None
    split_file_id: str | None = None


class RFQBulkSendGroup(BaseModel):
    rfq_id: str
    # One email is sent per contact per group; cap the fan-out so a single
    # request can't be turned into a mass-mail amplifier.
    vendor_contact_ids: list[str] = Field(..., min_length=1, max_length=100)
    # None = the default set: BOM split + Electrical Drawings (falling back to
    # General Drawings/Plans when no electrical set exists) + the project's
    # Specifications (0132, every category). Trenching swaps
    # the BOM split for the estimator's markup files (vendors price trenching
    # from the markup, not counts). An explicit list (possibly empty) is
    # exactly what the PE left in the Modify Files / confirm modals after
    # adding/removing files - what they saw is what gets sent.
    # 50 until 0132. The default set now includes every spec file, so a PE who
    # opens Modify Files and confirms it unchanged posts the whole list back:
    # 50 was reachable on a real spec book.
    attachment_file_ids: list[str] | None = Field(default=None, max_length=200)
    # Optional CC lists keyed by To-contact id: each CC contact is copied on
    # that one email instead of getting their own. The send layer enforces that
    # every CC works at the same vendor company as its To contact.
    cc: dict[str, list[str]] | None = None

    @model_validator(mode="after")
    def _normalize_cc(self):
        if not self.cc:
            self.cc = None
            return self
        to_ids = set(self.vendor_contact_ids)
        cleaned: dict[str, list[str]] = {}
        for to_id, cc_ids in self.cc.items():
            if to_id not in to_ids:
                raise ValueError("cc keys must be selected recipient contact ids")
            # A contact already getting their own email never doubles as a CC.
            ids = [c for c in dict.fromkeys(cc_ids) if c not in to_ids]
            if len(ids) > 10:
                raise ValueError("At most 10 CC contacts per email")
            if ids:
                cleaned[to_id] = ids
        self.cc = cleaned or None
        return self


class RFQBulkSendIn(BaseModel):
    # One email per To contact per group; same-company CCs ride on that email.
    groups: list[RFQBulkSendGroup] = Field(..., min_length=1, max_length=100)
    # PE-edited body template: "<Contact Name>" is replaced per recipient and
    # the text is sent verbatim (no AI variation). None = generated default.
    email_body: str | None = Field(None, max_length=20_000)

    @field_validator("email_body")
    @classmethod
    def _blank_body_means_default(cls, v: str | None) -> str | None:
        # A whitespace-only edit means "no custom body", never an empty email.
        return v if v and v.strip() else None


class RFQNudgeTarget(BaseModel):
    """One nudge: a reminder emailed as a reply-all on this send's existing
    Graph thread. `message` is the FINAL plain text - the frontend has already
    substituted the {vendor name}/{contact name}/{project name}/{due date}
    template tokens - and goes out wrapped in the branded HTML shell."""

    rfq_send_id: str
    message: str = Field(..., min_length=1, max_length=5_000)

    @field_validator("message")
    @classmethod
    def _message_not_blank(cls, v: str) -> str:
        # A whitespace-only reminder is a mistake, never an empty email.
        if not v.strip():
            raise ValueError("message must not be blank")
        return v


class RFQNudgeIn(BaseModel):
    # One reminder email per target, sent sequentially; cap the fan-out like
    # bulk-send so a single request can't be turned into a mass-mailer.
    targets: list[RFQNudgeTarget] = Field(..., min_length=1, max_length=50)


class RFQNudgeResult(BaseModel):
    rfq_send_id: str
    # Null when no rfq_nudges row was created (validation failure or skip).
    nudge_id: str | None = None
    status: Literal["sent", "failed", "skipped_quote_received"]
    error: str | None = None


class RFQNudgeOut(BaseModel):
    results: list[RFQNudgeResult]


class RFQRecipientCc(BaseModel):
    """One CC'd coworker from the send's cc_recipients snapshot (0096)."""

    vendor_contact_id: str
    name: str
    email: str


class RFQLastNudge(BaseModel):
    created_at: str
    sent_at: str | None = None
    status: Literal["pending", "sent", "failed"]
    error: str | None = None


class RFQRecipientStatus(BaseModel):
    """One vendor CONTACT's standing on an RFQ. Re-sends collapse onto the
    contact's latest send; `rfq_send_id` is the send a nudge replies on (the
    latest one that actually went out with a Graph id)."""

    rfq_send_id: str
    vendor_contact_id: str
    contact_name: str | None = None
    contact_email: str | None = None
    vendor_name: str | None = None
    cc_recipients: list[RFQRecipientCc] | None = None
    sent_at: str | None = None
    send_status: Literal["pending", "sent", "failed"]
    send_error: str | None = None
    state: Literal["quote_received", "replied_no_quote", "no_reply", "failed", "queued"]
    replied_at: str | None = None
    nudgeable: bool
    last_nudge: RFQLastNudge | None = None


class RFQRecipientsOut(BaseModel):
    recipients: list[RFQRecipientStatus]


# Bounds shared by every hand-entered price: no negatives, max two decimal
# places, and stay inside the DB's numeric(14,2) so neither an overflow nor a
# silent round can happen at the write. (decimal_places also rejects values
# like 999999999999.999 that are < 10^12 but round PAST the column limit.)
_AMOUNT_BOUNDS = {"ge": 0, "le": Decimal("999999999999.99"), "decimal_places": 2}


class QuoteIn(BaseModel):
    vendor_id: str
    vendor_contact_id: str | None = None
    amount: Decimal = Field(**_AMOUNT_BOUNDS)
    notes: str | None = None
    # An already-uploaded quote file (category 'quote', this RFQ's category) to
    # carry as the quote's document. Reference only: nothing is extracted from
    # it, the typed amount IS the quote.
    quote_file_id: str | None = None
    # Optional sales-tax answer recorded with the row. REQUIRED once the bid
    # has been submitted (a late quote, 0138): the router 422s without it.
    tax_included: bool | None = None
    tax_rate: Decimal = Field(Decimal("8.375"), ge=0, le=Decimal("100"), decimal_places=3)


class QuoteOverrideIn(BaseModel):
    amount: Decimal = Field(**_AMOUNT_BOUNDS)
    note: str | None = None


class ManualQuoteIn(BaseModel):
    """A hand-entered candidate on a category (quotes.origin = 'manual'): a
    number the estimator already has in hand that never came through the
    mailbox, e.g. a price given over the phone or carried across from another
    job. It has no vendor behind it and no priority whatsoever: it is one more
    quote competing to be picked on Select Vendors, and a category may hold as
    many of them as the estimator enters.

    tax_included answers the sales-tax question for this figure at the moment it
    is typed, which is exactly what a quote approval attests to.
    """

    amount: Decimal = Field(**_AMOUNT_BOUNDS)
    # Does the figure already include sales tax? When it does not, tax_rate (a
    # percent) is applied on top, as it is for any vendor quote.
    tax_included: bool
    tax_rate: Decimal = Field(Decimal("8.375"), ge=0, le=Decimal("100"), decimal_places=3)
    # Shown beside the figure so the team can see where the number came from.
    notes: str | None = Field(None, max_length=2_000)
    # An already-uploaded quote file (category 'quote', this RFQ's category) to
    # carry as the quote's document. Reference only: nothing is extracted from
    # it, the typed amount IS the quote.
    quote_file_id: str | None = None


class QuoteNoteIn(BaseModel):
    """One note on one quote (quote_notes, 0137): commentary that tells quotes
    in the same category apart ("excludes fixtures", "lead time 12 weeks").
    Never moves a price. Trimmed; the 1..2000 bound matches the table check."""

    body: str = Field(min_length=1, max_length=2_000)

    @field_validator("body")
    @classmethod
    def _not_blank(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("Note cannot be empty")
        return v


class ReplyManualQuoteIn(BaseModel):
    """Manual entry for a quote that ARRIVED on a vendor reply but never became
    a number (extraction failed, found no amount, or needs review). Unlike
    ManualQuoteIn this figure has a vendor behind it (the contact the RFQ send
    went to), so the quote lands as origin 'vendor' bound to the reply, the
    reply is marked resolved, and the send stops polling. It still has to be
    approved on the table like any emailed quote; the optional tax answer here
    just saves the second trip to the row.
    """

    amount: Decimal = Field(**_AMOUNT_BOUNDS)
    # Null = leave the sales-tax question for the approval pass.
    tax_included: bool | None = None
    tax_rate: Decimal = Field(Decimal("8.375"), ge=0, le=Decimal("100"), decimal_places=3)
    notes: str | None = Field(None, max_length=2_000)
    # One of the reply's quote files to carry as the quote's file (what Select
    # Vendors offers as the preview).
    quote_file_id: str | None = None


class QuoteApprovalIn(BaseModel):
    """Receive-quotes sign-off on ONE quote: a human confirms the amount on the
    row is the amount the vendor actually quoted, and that its sales-tax
    question has been answered. Only an approved quote may be picked as a
    category's winner; withdrawing approval from the quote that currently wins
    withdraws the selection with it."""

    approved: bool


class RfqCustomPriceIn(BaseModel):
    """RETIRED: the hand-entered category price that used to outrank every
    vendor quote. Selection is now the only thing that prices a category, so a
    hand-entered figure is entered as a quote instead (see ManualQuoteIn) and no
    route builds this model. Kept only because tests/test_pricing.py still
    imports it to exercise the shared amount bounds."""

    amount: Decimal | None = Field(None, **_AMOUNT_BOUNDS)
    note: str | None = None


class RfqQuotesConfirmIn(BaseModel):
    """Receive-quotes attestation: the PE confirms the vendor quoted the
    entire RFQ and didn't miss a material (false retracts it)."""

    confirmed: bool


class TaxIn(BaseModel):
    """Tax attestation for a priced figure on the receive-quotes step - a vendor
    quote or the General Material estimate: does the number already include
    sales tax? When not, tax_rate (a percent, default the Clark County 8.375%)
    is applied on top, and pricing compares/carries the tax-inclusive figure so
    the materials cost is the true cost incurred. tax_rate is ignored when
    tax_included."""

    tax_included: bool
    tax_rate: Decimal = Field(Decimal("8.375"), ge=0, le=Decimal("100"), decimal_places=3)


# ── BOQ → RFQ extraction ──────────────────────────────────────────────────


class BoqAnalysisStart(BaseModel):
    # Defaults to the project's most recent BOQ upload when omitted.
    boq_file_id: str | None = None


class BoqItemSrc(BaseModel):
    """An item's position in the analysis's pristine result_json -
    sites[s].material_groups[g].items[i]. Rides through drafts and the confirm
    payload so the server can diff the user's output against the model's."""

    s: int = Field(..., ge=0)
    g: int = Field(..., ge=0)
    i: int = Field(..., ge=0)


class BoqOverrideIn(BaseModel):
    """One touched item in the correction draft. quantity/unit are the item's
    CURRENT effective values (may equal the original); category_id null means
    "inherit the group mapping"; removed excludes the item from confirm."""

    src: BoqItemSrc
    quantity: Decimal | None = None
    unit: str | None = Field(None, max_length=80)
    category_id: str | None = Field(None, max_length=64)
    removed: bool = False


class BoqDraftBody(BaseModel):
    # Sparse - an entry exists only for touched items - so the cap comfortably
    # exceeds any real correction pass while bounding the stored jsonb.
    overrides: list[BoqOverrideIn] = Field(default_factory=list, max_length=5000)
    # Mirrors the panel's group→category Select ("" = Hold/skip).
    group_mappings: dict[str, str] = Field(default_factory=dict)

    @field_validator("group_mappings")
    @classmethod
    def _cap_mappings(cls, v: dict[str, str]) -> dict[str, str]:
        if len(v) > 200:
            raise ValueError("Too many group mappings in one draft")
        # Key/value length caps too - the draft is stored verbatim as jsonb and
        # echoed back on every /latest, so unbounded strings would be amplified.
        for name, cat_id in v.items():
            if len(name) > 300:
                raise ValueError("Group name too long")
            if len(cat_id) > 64:
                raise ValueError("Category id too long")
        return v


class BoqDraftIn(BaseModel):
    # null clears the stored draft.
    draft: BoqDraftBody | None = None


class RFQLineItemIn(BaseModel):
    site_name: str | None = None
    sr_no: str | None = None
    description: str
    quantity: Decimal | None = None
    unit: str | None = None
    notes: str | None = None
    # Where this item sits in the model's result_json (for the training diff).
    # Optional so pre-corrections confirm payloads still validate.
    src: BoqItemSrc | None = None


class RFQGroupIn(BaseModel):
    material_category_id: str
    # Comfortably above any real BOQ category, but bounds the rfq_line_items bulk
    # insert (and the generated workbook size) against a runaway request.
    items: list[RFQLineItemIn] = Field(default_factory=list, max_length=2000)


class BoqGroupMapIn(BaseModel):
    group_name: str = Field(..., max_length=300)
    material_category_id: str


class BoqConfirmIn(BaseModel):
    # One group per material category; sites already merged client-side, invented
    # categories already mapped to a material_category_id. Material categories are
    # a small fixed table, so a modest cap can't reject legitimate input.
    groups: list[RFQGroupIn] = Field(..., min_length=1, max_length=50)
    # Model group names the reviewer left on Hold - neutral for the training
    # diff: their items are never counted as removed.
    held_groups: list[str] = Field(default_factory=list, max_length=200)
    # The panel's group→category mapping at confirm time, so the training diff
    # knows which model groups went in and under which category.
    group_mappings: list[BoqGroupMapIn] = Field(default_factory=list, max_length=200)

    @field_validator("held_groups")
    @classmethod
    def _cap_held(cls, v: list[str]) -> list[str]:
        if any(len(name) > 300 for name in v):
            raise ValueError("Held group name too long")
        return v


class BoqManualItemIn(BaseModel):
    """One hand-typed BOQ item. Same shape the model produces per item, so
    the manual analysis reads/edits/confirms through the normal panel."""

    description: str = Field(..., max_length=500)
    quantity: Decimal | None = Field(None, ge=0, le=Decimal("1000000000"))
    unit: str | None = Field(None, max_length=80)
    notes: str | None = Field(None, max_length=500)

    @field_validator("description")
    @classmethod
    def _desc(cls, v: str) -> str:
        cleaned = " ".join(v.split())
        if not cleaned:
            raise ValueError("Each item needs a description")
        return cleaned

    @field_validator("unit", "notes")
    @classmethod
    def _opt(cls, v: str | None) -> str | None:
        cleaned = " ".join(v.split()) if v else ""
        return cleaned or None


class BoqManualGroupIn(BaseModel):
    material_category_id: str
    items: list[BoqManualItemIn] = Field(..., min_length=1, max_length=2000)


class BoqManualIn(BaseModel):
    """POST /boq-analysis/manual: the reviewer's own item list, one group per
    material category, for a project run without the model (instance down,
    or a BOQ the model has no business reading). Lands as a `done` analysis
    with model='manual' so review, corrections and confirm work unchanged."""

    groups: list[BoqManualGroupIn] = Field(..., min_length=1, max_length=50)

    @field_validator("groups")
    @classmethod
    def _unique_categories(cls, v: list[BoqManualGroupIn]) -> list[BoqManualGroupIn]:
        ids = [g.material_category_id for g in v]
        if len(set(ids)) != len(ids):
            raise ValueError("Each material category can be listed once")
        return v


class TrainingReviewIn(BaseModel):
    """Dev Training page sign-off on a captured example (BOQ or bid-split);
    false clears it."""

    reviewed: bool
    note: str | None = Field(None, max_length=2000)


# ── Material categories ────────────────────────────────────────────────────


class MaterialCategoryUpdate(BaseModel):
    name: str | None = None
    is_active: bool | None = None
    sort_order: int | None = None


# ── Pricing ─────────────────────────────────────────────────────────────--


class LaborField(BaseModel):
    name: str = ""
    amount: Decimal | None = None


class LaborReviewIn(BaseModel):
    labor_notes: str | None = None
    verified: bool = False
    labor_amount: Decimal | None = None
    labor_breakdown: list[LaborField] | None = None


class MarkupIn(BaseModel):
    labor_markup_pct: Decimal | None = None
    labor_markup_amount: Decimal | None = None
    materials_markup_pct: Decimal | None = None
    materials_markup_amount: Decimal | None = None
    # Section breakouts (gear / underground / low voltage): same pct/amount pair
    # per section; only sections present on the project are shown in the UI.
    gear_markup_pct: Decimal | None = None
    gear_markup_amount: Decimal | None = None
    underground_markup_pct: Decimal | None = None
    underground_markup_amount: Decimal | None = None
    low_voltage_markup_pct: Decimal | None = None
    low_voltage_markup_amount: Decimal | None = None
    notes: str | None = None


class GeneralMaterialIn(BaseModel):
    # Manual entry / override of the general-material (wiring) price when the
    # estimate extraction can't find it or the PM/PE wants to correct it.
    amount: Decimal | None = None


class VerifyOverrideIn(BaseModel):
    # The final figures the Executive/PM commit at the verify step (9). Stored as
    # a snapshot on `verifications` so the upstream tables stay untouched and the
    # delta from the original numbers remains computable. materials_amount is the
    # RESIDUAL materials figure (section breakouts excluded); the commit stores
    # NULL for the section fields of sections not on the project.
    labor_amount: Decimal | None = None
    materials_amount: Decimal | None = None
    gear_amount: Decimal | None = None
    underground_amount: Decimal | None = None
    low_voltage_amount: Decimal | None = None
    labor_markup_amount: Decimal | None = None
    materials_markup_amount: Decimal | None = None
    gear_markup_amount: Decimal | None = None
    underground_markup_amount: Decimal | None = None
    low_voltage_markup_amount: Decimal | None = None
    notes: str | None = None


# ── Send Out / proposals (step 10) ──────────────────────────────────────--


class ProposalGenerateIn(BaseModel):
    boq_file_id: str | None = None  # default: latest 'boq' upload
    # Restrict document generation to these GCs (general_contractors ids). The
    # side-menu "Send proposal" flow for a GC added after the bid went out
    # generates that one GC's document only. None = every GC without a sent
    # document (the Send Out panel's batch behaviour).
    gc_ids: list[str] | None = Field(default=None, max_length=100)


class ProposalLinesIn(BaseModel):
    # Strict counterpart of proposal_scope.normalize_lines (which permissively
    # cleans LLM output): human edits are REJECTED, not silently mutated.
    # Limits are imported from proposal_scope so the two can't drift.
    lines: list[str] = Field(..., min_length=1, max_length=200)

    @field_validator("lines")
    @classmethod
    def _clean(cls, v: list[str]) -> list[str]:
        from app.services.proposal_scope import MAX_LINE_CHARS

        cleaned = [" ".join(line.split()) for line in v]
        if any(not line for line in cleaned):
            raise ValueError("Scope lines cannot be blank")
        if any(len(line) > MAX_LINE_CHARS for line in cleaned):
            raise ValueError(f"Scope lines must be {MAX_LINE_CHARS} characters or fewer")
        if any("<" in line or ">" in line for line in cleaned):
            raise ValueError("Scope lines cannot contain '<' or '>' characters")
        return cleaned


class ProposalManualIn(BaseModel):
    """Seed for a hand-entered scope-lines draft: no BOQ read, no model call.
    Lines are optional (an empty list just opens the editor); blank rows are
    dropped rather than rejected so a half-typed list still lands, but the
    same length/character rules as ProposalLinesIn apply to what is kept."""

    lines: list[str] = Field(default_factory=list, max_length=200)

    @field_validator("lines")
    @classmethod
    def _clean(cls, v: list[str]) -> list[str]:
        from app.services.proposal_scope import MAX_LINE_CHARS

        cleaned = [" ".join(line.split()) for line in v]
        cleaned = [line for line in cleaned if line]
        if any(len(line) > MAX_LINE_CHARS for line in cleaned):
            raise ValueError(f"Scope lines must be {MAX_LINE_CHARS} characters or fewer")
        if any("<" in line or ">" in line for line in cleaned):
            raise ValueError("Scope lines cannot contain '<' or '>' characters")
        return cleaned


class ProposalAmountsIn(BaseModel):
    # One GC's proposal figures (GC Pricing step editor). None clears the
    # override back to the pricing base; the total is never stored, it is
    # always the sum of the present-section figures plus labor. A non-null
    # value for a section not on the project is a 409 downstream.
    material_amount: Decimal | None = Field(None, **_AMOUNT_BOUNDS)
    gear_amount: Decimal | None = Field(None, **_AMOUNT_BOUNDS)
    underground_amount: Decimal | None = Field(None, **_AMOUNT_BOUNDS)
    low_voltage_amount: Decimal | None = Field(None, **_AMOUNT_BOUNDS)
    labor_amount: Decimal | None = Field(None, **_AMOUNT_BOUNDS)


class ProposalAmountsRequestIn(ProposalAmountsIn):
    # A per-GC price change for a GC added after the bid went out. Posted by
    # the sender (request) and by the Executive deciding it (approve, where the
    # figures may be edited before they are accepted). The note travels with
    # the request / decision into the GC row and the notifications.
    note: str | None = Field(default=None, max_length=1000)


class ProposalAmountsRejectIn(BaseModel):
    note: str | None = Field(default=None, max_length=1000)


class ProposalDispatchIn(BaseModel):
    # Shared wire shape of the two per-GC email paths (send, re-send): which
    # proposal rows, who at each GC, and the cover note.
    proposal_ids: list[str] = Field(..., min_length=1, max_length=100)
    # proposal_id → gc_contact ids chosen in the confirm dialog. Missing key =
    # all contacts with an email (legacy clients / tests).
    contacts: dict[str, list[str]] | None = None
    email_body: str | None = None  # None = generated cover note
    # Extra project files the PA picked in the Modify Files modal. They ride on
    # EVERY email in this dispatch alongside each GC's own proposal PDF (which
    # is always attached and never part of this list). None/empty = none.
    extra_attachment_file_ids: list[str] | None = Field(default=None, max_length=25)
    # proposal_id → same-GC gc_contact ids to CC on that GC's one email
    # (rfq_sends CC precedent). The send layer enforces that every CC contact
    # belongs to that proposal's GC and has an email; anyone already on the To
    # line is dropped there too.
    cc: dict[str, list[str]] | None = None

    @model_validator(mode="after")
    def _normalize_cc(self):
        if not self.cc:
            self.cc = None
            return self
        if len(self.cc) > 100:
            raise ValueError("Too many CC selections")
        cleaned: dict[str, list[str]] = {}
        for pid, cc_ids in self.cc.items():
            # A contact picked as a To recipient never doubles as a CC.
            to_ids = set((self.contacts or {}).get(pid) or [])
            ids = [c for c in dict.fromkeys(cc_ids) if c not in to_ids]
            if len(ids) > 10:
                raise ValueError("At most 10 CC contacts per proposal email")
            if ids:
                cleaned[pid] = ids
        self.cc = cleaned or None
        return self

    @field_validator("email_body")
    @classmethod
    def _body_sane(cls, v: str | None) -> str | None:
        if v is not None and not (10 <= len(v) <= 10000):
            raise ValueError("Email body must be between 10 and 10,000 characters")
        return v

    @field_validator("contacts")
    @classmethod
    def _contacts_sane(cls, v: dict[str, list[str]] | None) -> dict[str, list[str]] | None:
        if v is not None and (len(v) > 100 or any(len(ids) > 50 for ids in v.values())):
            raise ValueError("Too many recipient selections")
        return v


class ProposalSendIn(ProposalDispatchIn):
    force: bool = False  # required to retry an outcome-unknown failure


class ProposalResendIn(ProposalDispatchIn):
    # Email an already-sent proposal to its GC again (bounced address, GC lost
    # the mail). No `force`: that flag exists to unblock a first send whose
    # outcome is unknown, and a re-send leaves proposal_sends at 'sent' whatever
    # happens, so there is no ambiguous state for it to unlock.
    pass


class ProposalMarkSubmittedIn(BaseModel):
    # The bid went out through a third-party application (GC portal etc.), not
    # our email - record the listed proposals as submitted without sending.
    proposal_ids: list[str] = Field(..., min_length=1, max_length=100)


# ── Win / Loss (bid outcome) - final step ───────────────────────────────────


class BidGcOutcomeIn(BaseModel):
    # One GC we bid to. All detail is optional / "unknown" - the PA records what
    # they've heard back, which is usually partial. winning_amount is the number
    # that GC actually went with (lets us show how far off ours was); our_amount
    # is snapshotted server-side from proposal_sends, never trusted from the client.
    gc_id: str
    gc_award_result: Literal["won", "lost", "unknown"] = "unknown"
    our_bid_selection: Literal["used_us", "used_other", "unknown"] = "unknown"
    winning_amount: Decimal | None = Field(None, **_AMOUNT_BOUNDS)


class BidOutcomeIn(BaseModel):
    # The PA's closeout of a submitted bid. `result` is G3's overall outcome;
    # `winning_gc_id` (optional) is the GC that won the job; `gcs` carries the
    # per-GC detail for the GCs we bid to.
    result: Literal["won", "lost", "no_award"]
    winning_gc_id: str | None = None
    notes: str | None = None
    gcs: list[BidGcOutcomeIn] = Field(default_factory=list, max_length=100)

    @field_validator("notes")
    @classmethod
    def _notes_sane(cls, v: str | None) -> str | None:
        if v is not None and len(v) > 4000:
            raise ValueError("Notes must be 4,000 characters or fewer")
        return v


# ── Project Management ──────────────────────────────────────────────────────
# The PM module (migrations 0057-0060). Money follows the house convention:
# Decimal in, string out (routers serialize via str()). Signed bounds where
# deductive amounts are legitimate (change orders, SOV lines from them).

_SIGNED_AMOUNT_BOUNDS = {
    "ge": Decimal("-999999999999.99"),
    "le": Decimal("999999999999.99"),
    "decimal_places": 2,
}


def _max_len(field: str, limit: int):
    """Shared length validator factory for PM free-text fields."""

    @field_validator(field)
    @classmethod
    def _check(cls, v: str | None) -> str | None:  # noqa: N805
        if v is not None and len(v) > limit:
            raise ValueError(f"{field} must be {limit:,} characters or fewer")
        return v

    return _check


class PMProjectCreate(BaseModel):
    """Direct creation in Project Management - a project awarded without a bid,
    or an already-live job being onboarded (initial_stage picks where it enters).
    Deliberately NOT ProjectCreate: the bidding intake's required fields (bid
    dates, go/no-go answers) don't exist for a never-bid project."""

    name: str = Field(min_length=1, max_length=300)
    number: str = Field(min_length=1, max_length=100)
    initial_stage: PMStage = "precon"
    customer_gc_id: str | None = None
    customer_name: str | None = Field(None, max_length=300)
    original_contract_value: Decimal | None = Field(None, **_SIGNED_AMOUNT_BOUNDS)
    awarded_at: date | None = None
    ntp_date: date | None = None
    address: str | None = Field(None, max_length=500)
    planned_start_date: date | None = None
    planned_finish_date: date | None = None
    actual_start_date: date | None = None
    superintendent_name: str | None = Field(None, max_length=200)
    contract_number: str | None = Field(None, max_length=100)
    notes: str | None = Field(None, max_length=4000)


class PMDetailsUpdate(BaseModel):
    """PATCH for pm_details (+ the shared projects.notes is bidding-owned; PM
    notes live on pm_details). exclude_unset semantics: explicit null clears."""

    customer_gc_id: str | None = None
    customer_name: str | None = Field(None, max_length=300)
    original_contract_value: Decimal | None = Field(None, **_SIGNED_AMOUNT_BOUNDS)
    awarded_at: date | None = None
    ntp_date: date | None = None
    planned_start_date: date | None = None
    planned_finish_date: date | None = None
    actual_start_date: date | None = None
    actual_finish_date: date | None = None
    superintendent_name: str | None = Field(None, max_length=200)
    contract_number: str | None = Field(None, max_length=100)
    retainage_percent: Decimal | None = Field(None, ge=0, le=Decimal("100"), decimal_places=2)
    notes: str | None = Field(None, max_length=4000)


class PMStageTransitionIn(BaseModel):
    to_stage: PMStage
    # Required (validated in the service) when moving BACKWARD a stage.
    note: str | None = Field(None, max_length=2000)


class ChangeOrderIn(BaseModel):
    co_number: str = Field(min_length=1, max_length=50)
    title: str = Field(min_length=1, max_length=300)
    description: str | None = Field(None, max_length=4000)
    status: Literal["draft", "submitted", "approved", "rejected"] = "draft"
    amount: Decimal = Field(Decimal(0), **_SIGNED_AMOUNT_BOUNDS)  # deductive COs are negative
    days_added: int | None = Field(None, ge=-3650, le=3650)
    customer_reference: str | None = Field(None, max_length=100)
    submitted_at: date | None = None
    approved_at: date | None = None


class ChangeOrderUpdate(BaseModel):
    co_number: str | None = Field(None, min_length=1, max_length=50)
    title: str | None = Field(None, min_length=1, max_length=300)
    description: str | None = Field(None, max_length=4000)
    status: Literal["draft", "submitted", "approved", "rejected"] | None = None
    amount: Decimal | None = Field(None, **_SIGNED_AMOUNT_BOUNDS)
    days_added: int | None = Field(None, ge=-3650, le=3650)
    customer_reference: str | None = Field(None, max_length=100)
    submitted_at: date | None = None
    approved_at: date | None = None


class SovLineIn(BaseModel):
    line_number: str = Field(min_length=1, max_length=50)
    description: str = Field(min_length=1, max_length=500)
    scheduled_value: Decimal = Field(**_SIGNED_AMOUNT_BOUNDS)  # CO lines may be deductive
    change_order_id: str | None = None
    sort_order: int = 0


class SovLineUpdate(BaseModel):
    line_number: str | None = Field(None, min_length=1, max_length=50)
    description: str | None = Field(None, min_length=1, max_length=500)
    scheduled_value: Decimal | None = Field(None, **_SIGNED_AMOUNT_BOUNDS)
    change_order_id: str | None = None
    sort_order: int | None = None


class PayAppCreate(BaseModel):
    """Creating a pay app auto-populates one line per current SOV line, with
    previous_completed snapshotted from all prior apps server-side."""

    period_start: date | None = None
    period_end: date
    retainage_percent: Decimal | None = Field(None, ge=0, le=Decimal("100"), decimal_places=2)
    notes: str | None = Field(None, max_length=4000)


class PayAppUpdate(BaseModel):
    period_start: date | None = None
    period_end: date | None = None
    status: Literal["draft", "submitted", "approved", "paid", "rejected"] | None = None
    retainage_percent: Decimal | None = Field(None, ge=0, le=Decimal("100"), decimal_places=2)
    submitted_at: date | None = None
    approved_at: date | None = None
    paid_at: date | None = None
    notes: str | None = Field(None, max_length=4000)


class PayAppLineUpdate(BaseModel):
    """The two user-entered G703 columns; previous_completed is server-owned."""

    this_period: Decimal | None = Field(None, **_SIGNED_AMOUNT_BOUNDS)  # corrections may be negative
    stored_materials: Decimal | None = Field(None, ge=0, le=Decimal("999999999999.99"), decimal_places=2)


class MilestoneIn(BaseModel):
    name: str = Field(min_length=1, max_length=300)
    planned_date: date | None = None
    actual_date: date | None = None
    sort_order: int = 0
    notes: str | None = Field(None, max_length=2000)


class MilestoneUpdate(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=300)
    planned_date: date | None = None
    actual_date: date | None = None
    sort_order: int | None = None
    notes: str | None = Field(None, max_length=2000)


class DailyLogIn(BaseModel):
    log_date: date
    weather: str | None = Field(None, max_length=200)
    manpower_count: int | None = Field(None, ge=0, le=10000)
    work_performed: str = Field(min_length=1, max_length=8000)
    delays: str | None = Field(None, max_length=4000)
    safety_notes: str | None = Field(None, max_length=4000)


class DailyLogUpdate(BaseModel):
    log_date: date | None = None
    weather: str | None = Field(None, max_length=200)
    manpower_count: int | None = Field(None, ge=0, le=10000)
    work_performed: str | None = Field(None, min_length=1, max_length=8000)
    delays: str | None = Field(None, max_length=4000)
    safety_notes: str | None = Field(None, max_length=4000)


RFIPriority = Literal["standard", "urgent"]

# `question` is sanitized HTML (migration 0068), so this bounds *markup*, not
# prose: tags inflate a plain-text question well past the 8000 it used to cost.
# It is only the outer guard against an absurd payload - routers/pm_field.py
# applies the real check to the sanitized value, so markup the author never sees
# can't be what blocks them.
RFI_QUESTION_MAX_CHARS = 24000

# drawing_numbers / applicable_references are free-text chips ("E-101", "Spec
# 26 05 19") - there is no drawings or specs table to point at (see 0068).
_RFI_CHIP_MAX_ITEMS = 50
_RFI_CHIP_MAX_CHARS = 100

# Documents-hub handles ("source:id"), mirroring the rfi_attachments.doc_key
# CHECK constraint. Shape only - routers/pm_field.py is what proves a key names
# a document this project may actually see.
_ATTACHMENT_KEY_RE = re.compile(r"^(pm|bid|cp):[0-9a-f-]{36}$")
_RFI_ATTACHMENT_MAX = 50


def _clean_chips(v: list[str] | None, field: str) -> list[str] | None:
    """Blank/duplicate chips are cleaned silently (they carry no intent); too
    many, or too long, is rejected rather than truncated."""
    if v is None:
        return None
    cleaned = [" ".join(s.split()) for s in v]
    cleaned = [s for s in cleaned if s]
    cleaned = list(dict.fromkeys(cleaned))  # de-dupe, preserve order
    if len(cleaned) > _RFI_CHIP_MAX_ITEMS:
        raise ValueError(f"{field} is limited to {_RFI_CHIP_MAX_ITEMS} entries")
    if any(len(s) > _RFI_CHIP_MAX_CHARS for s in cleaned):
        raise ValueError(f"Each {field} entry must be {_RFI_CHIP_MAX_CHARS} characters or fewer")
    return cleaned


def _clean_attachment_keys(v: list[str] | None) -> list[str] | None:
    if v is None:
        return None
    cleaned = list(dict.fromkeys(s.strip() for s in v))  # de-dupe: (rfi_id, doc_key) is unique
    if len(cleaned) > _RFI_ATTACHMENT_MAX:
        raise ValueError(f"An RFI is limited to {_RFI_ATTACHMENT_MAX} attachments")
    bad = [s for s in cleaned if not _ATTACHMENT_KEY_RE.match(s)]
    if bad:
        raise ValueError("attachment_keys must be document handles like 'pm:<uuid>'")
    return cleaned


class RFIIn(BaseModel):
    subject: str = Field(min_length=1, max_length=300)
    question: str = Field(min_length=1, max_length=RFI_QUESTION_MAX_CHARS)
    # Superseded by assigned_gc_id/assigned_contact_id but kept writable: 0068
    # preserves un-backfilled legacy values rather than destroying them.
    asked_of: str | None = Field(None, max_length=200)
    sent_at: date | None = None
    due_at: date | None = None
    priority: RFIPriority = "standard"
    drawing_numbers: list[str] = Field(default_factory=list)
    applicable_references: list[str] = Field(default_factory=list)
    assigned_gc_id: str | None = None
    assigned_contact_id: str | None = None
    # Not a column on `rfis` - the router writes these to rfi_attachments.
    attachment_keys: list[str] = Field(default_factory=list)

    @field_validator("drawing_numbers", "applicable_references")
    @classmethod
    def _chips(cls, v: list[str], info) -> list[str]:
        return _clean_chips(v, info.field_name)

    @field_validator("attachment_keys")
    @classmethod
    def _keys(cls, v: list[str]) -> list[str]:
        return _clean_attachment_keys(v)


class RFIUpdate(BaseModel):
    subject: str | None = Field(None, min_length=1, max_length=300)
    question: str | None = Field(None, min_length=1, max_length=RFI_QUESTION_MAX_CHARS)
    answer: str | None = Field(None, max_length=8000)  # plain text, not rich text
    # Who supplied the answer (free text). Normally set at close; kept editable
    # here so a mistyped responder can be corrected afterwards.
    answered_by: str | None = Field(None, max_length=200)
    status: Literal["open", "answered", "closed"] | None = None
    asked_of: str | None = Field(None, max_length=200)
    sent_at: date | None = None
    due_at: date | None = None
    answered_at: date | None = None
    priority: RFIPriority | None = None
    drawing_numbers: list[str] | None = None
    applicable_references: list[str] | None = None
    # Explicit null unassigns - these are nullable FKs.
    assigned_gc_id: str | None = None
    assigned_contact_id: str | None = None
    # Absent = leave attachments alone; present = replace the whole set. The
    # router relies on exclude_unset to tell those apart.
    attachment_keys: list[str] | None = None

    @field_validator("drawing_numbers", "applicable_references")
    @classmethod
    def _chips(cls, v: list[str] | None, info) -> list[str] | None:
        return _clean_chips(v, info.field_name)

    @field_validator("attachment_keys")
    @classmethod
    def _keys(cls, v: list[str] | None) -> list[str] | None:
        return _clean_attachment_keys(v)


class RFIClose(BaseModel):
    """Closing an RFI is gated: the responder, the answer, and at least one
    response document are all required (routers/pm_field.py enforces the last).

    Distinct from the answer→answered convenience - that only records that an
    answer was typed; this is the formal terminal state with an audit-worthy
    responder and a response document attached.
    """

    answer: str = Field(max_length=8000)  # plain text, not rich text
    answered_by: str = Field(max_length=200)
    # Optional: the router stamps today (LA time) when omitted, same as the
    # convenience path, so the form's "Date needed" logic stays timezone-correct.
    answered_at: date | None = None
    attachment_keys: list[str]

    @field_validator("answer", "answered_by")
    @classmethod
    def _required_text(cls, v: str, info) -> str:
        v = v.strip()
        if not v:
            raise ValueError(f"{info.field_name} is required to close an RFI")
        return v

    @field_validator("attachment_keys")
    @classmethod
    def _keys(cls, v: list[str]) -> list[str]:
        cleaned = _clean_attachment_keys(v) or []
        if not cleaned:
            raise ValueError("At least one response document is required to close an RFI")
        return cleaned


# gc_contacts ids the send is addressed to. Capped like the attachment list - an
# RFI never legitimately fans out to dozens of contacts, and it bounds the payload.
_RFI_RECIPIENTS_MAX = 50


class RFISendIn(BaseModel):
    """App send: email the RFI (as a filled PDF) to selected GC contacts.

    The router proves each contact belongs to the RFI's assigned company and has an
    email address - this only bounds and de-dupes the raw id list.
    """

    contact_ids: list[str] = Field(min_length=1, max_length=_RFI_RECIPIENTS_MAX)
    message: str | None = Field(None, max_length=4000)

    @field_validator("contact_ids")
    @classmethod
    def _dedupe(cls, v: list[str]) -> list[str]:
        cleaned = list(dict.fromkeys(s.strip() for s in v if s and s.strip()))
        if not cleaned:
            raise ValueError("Select at least one contact to send to")
        return cleaned

    @field_validator("message")
    @classmethod
    def _trim_message(cls, v: str | None) -> str | None:
        return (v or "").strip() or None


class RFIMarkSentIn(BaseModel):
    """Record that the RFI was already sent outside BDR (Procore/Autodesk), so the
    log's send status is truthful without BDR sending an email."""

    platform: Literal["procore", "autodesk"]


class ManpowerIn(BaseModel):
    work_date: date
    classification: str = Field(min_length=1, max_length=200)
    workers: int = Field(ge=0, le=10000)
    hours: Decimal | None = Field(None, ge=0, le=Decimal("9999.99"), decimal_places=2)
    daily_log_id: str | None = None
    notes: str | None = Field(None, max_length=2000)


class ManpowerUpdate(BaseModel):
    work_date: date | None = None
    classification: str | None = Field(None, min_length=1, max_length=200)
    workers: int | None = Field(None, ge=0, le=10000)
    hours: Decimal | None = Field(None, ge=0, le=Decimal("9999.99"), decimal_places=2)
    daily_log_id: str | None = None
    notes: str | None = Field(None, max_length=2000)


class PmMaterialIn(BaseModel):
    """A PM material line - the same shape a BOQ extraction item carries
    (no pricing). material_category_id None = uncategorized."""

    material_category_id: str | None = None
    description: str = Field(min_length=1, max_length=2000)
    quantity: Decimal | None = Field(None, ge=0, le=Decimal("999999999"))
    unit: str | None = Field(None, max_length=100)
    notes: str | None = Field(None, max_length=2000)
    site_name: str | None = Field(None, max_length=300)


class PmMaterialBulkIn(BaseModel):
    """A batch of material lines typed in one sitting (the add-materials modal).
    Bounded so a runaway paste can't turn into an unbounded insert."""

    materials: list[PmMaterialIn] = Field(min_length=1, max_length=200)


class PmMaterialUpdate(BaseModel):
    material_category_id: str | None = None
    description: str | None = Field(None, min_length=1, max_length=2000)
    quantity: Decimal | None = Field(None, ge=0, le=Decimal("999999999"))
    unit: str | None = Field(None, max_length=100)
    notes: str | None = Field(None, max_length=2000)
    site_name: str | None = Field(None, max_length=300)


# ── Project submittals (per-project submittal requests to vendors, 0073) ──────


class SubmittalCategoryGroup(BaseModel):
    """One material category's slice of a submittal request: which of the
    project's materials to request submittals for, any typed-in extras (to cover
    ourselves), and the vendor contacts of that category to email. Add/deselect
    is a per-request snapshot - it never touches pm_materials."""

    material_category_id: str | None = None
    included_material_ids: list[str] = Field(default_factory=list, max_length=500)
    adhoc_descriptions: list[str] = Field(default_factory=list, max_length=100)
    # One email is sent per contact; cap the fan-out so a single request can't be
    # turned into a mass-mail amplifier (mirrors RFQBulkSendGroup).
    vendor_contact_ids: list[str] = Field(default_factory=list, max_length=100)


class SubmittalRequestIn(BaseModel):
    """Create-and-send a submittal request across one or more categories."""

    groups: list[SubmittalCategoryGroup] = Field(..., min_length=1, max_length=50)
    include_specs: bool = False
    # Documents-hub keys ("source:id") of the spec sheets to attach. Plans are
    # always attached (not listed here). Only honored when include_specs is true.
    spec_document_keys: list[str] = Field(default_factory=list, max_length=100)
    # Project materials the sender unchecked - recorded for the "these never had
    # submittals requested" view; they simply produce no request items.
    deselected_material_ids: list[str] = Field(default_factory=list, max_length=1000)
    email_body: str | None = Field(None, max_length=20_000)

    @field_validator("email_body")
    @classmethod
    def _blank_body_means_default(cls, v: str | None) -> str | None:
        # A whitespace-only edit means "no custom body", never an empty email.
        return v if v and v.strip() else None


# ── Submittal approval packages (GC-facing, migration 0081) ──────────────────


class SubmittalApprovalGroup(BaseModel):
    """One category's contribution to an approval package: which of that
    category's available files the sender ticked. `material_category_id` is null
    for the Uncategorized bucket. Keys are opaque ("att:"/"bank:"/"pm:") and are
    validated against the project's available set on the server - never trusted."""

    material_category_id: str | None = None
    file_keys: list[str] = Field(default_factory=list, max_length=300)


class SubmittalApprovalIn(BaseModel):
    """Send collected submittals to the GC for approval - one email, To + CC."""

    groups: list[SubmittalApprovalGroup] = Field(..., min_length=1, max_length=100)
    # gc_contacts ids. The fan-out is one message, so these bound the header
    # size rather than a send count, but a cap keeps a pasted list sane.
    recipient_contact_ids: list[str] = Field(..., min_length=1, max_length=50)
    cc_contact_ids: list[str] = Field(default_factory=list, max_length=50)
    message: str | None = Field(None, max_length=20_000)

    @field_validator("message")
    @classmethod
    def _blank_message_is_none(cls, v: str | None) -> str | None:
        return v if v and v.strip() else None


# Mirror of submittal_package_items.approval_status (0081) - no 'partial' here:
# one file is approved, approved with comments, or rejected. 'partial' belongs
# to the package alone, and is derived from these rather than sent by the client.
SubmittalItemVerdict = Literal["pending", "approved", "approved_as_noted", "rejected"]


class SubmittalVerdictItemIn(BaseModel):
    """One file's verdict. `id` is a submittal_package_items id, re-validated
    server-side against the package it's being recorded on."""

    id: str
    approval_status: SubmittalItemVerdict
    response_notes: str | None = Field(None, max_length=5_000)


class SubmittalVerdictIn(BaseModel):
    """Record the GC's response to an approval package, per file.

    Only the items listed are touched, so the modal can save one row or all of
    them. The PACKAGE's headline status is deliberately not accepted here - it is
    derived from the items (submittal_approval._rollup) so the two can never
    disagree.
    """

    items: list[SubmittalVerdictItemIn] = Field(default_factory=list, max_length=300)
    response_notes: str | None = Field(None, max_length=20_000)


# ── Submittal Bank ───────────────────────────────────────────────────────────

# Mirror the submittal_category PG enum (0072) - keep in sync with
# bdr_fe/lib/types.ts when a value is added.
SubmittalCategory = Literal["general_material", "low_voltage", "switchgear"]


class SubmittalIn(BaseModel):
    """A single bank material (one size/color SKU). `aliases` None → auto-generate
    industry/slang search aliases with a cheap model; pass a list to set them by
    hand, or generate_aliases=False to skip the AI call entirely."""

    category: SubmittalCategory
    name: str = Field(min_length=1, max_length=300)
    size: str | None = Field(None, max_length=100)
    color: str | None = Field(None, max_length=100)
    made_in_usa: bool | None = None
    manufacturer: str | None = Field(None, max_length=200)
    notes: str | None = Field(None, max_length=2000)
    aliases: list[str] | None = None
    generate_aliases: bool = True


class SubmittalUpdate(BaseModel):
    category: SubmittalCategory | None = None
    name: str | None = Field(None, min_length=1, max_length=300)
    size: str | None = Field(None, max_length=100)
    color: str | None = Field(None, max_length=100)
    made_in_usa: bool | None = None
    manufacturer: str | None = Field(None, max_length=200)
    notes: str | None = Field(None, max_length=2000)
    aliases: list[str] | None = None


class SubmittalVariantIn(BaseModel):
    """One (size, color) row of a group create. name/made_in_usa override the
    group defaults when set (both may differ per size/color)."""

    size: str | None = Field(None, max_length=100)
    color: str | None = Field(None, max_length=100)
    name: str | None = Field(None, min_length=1, max_length=300)
    made_in_usa: bool | None = None


class SubmittalGroupIn(BaseModel):
    """Create several materials that share a name/manufacturer in one shot (a
    "group"). The client uploads one PDF afterward and links it to every returned
    material id, so a shared cut-sheet is stored once."""

    category: SubmittalCategory
    name: str = Field(min_length=1, max_length=300)
    manufacturer: str | None = Field(None, max_length=200)
    made_in_usa: bool | None = None
    notes: str | None = Field(None, max_length=2000)
    variants: list[SubmittalVariantIn] = Field(min_length=1, max_length=100)
    generate_aliases: bool = True


class SubmittalFileUpdate(BaseModel):
    vendor: str | None = Field(None, max_length=200)
    title: str | None = Field(None, max_length=300)
    notes: str | None = Field(None, max_length=2000)


# ── Project ↔ Submittal Bank links (0074) ────────────────────────────────────


class PmBankPullIn(BaseModel):
    """Materials to pull matching bank submittals for. Each gets its best
    file-bearing fuzzy match linked (materials that already have a link, or have
    no matching bank submittal, are skipped)."""

    material_ids: list[str] = Field(min_length=1, max_length=500)


class PmAddToBankIn(BaseModel):
    """Push an uploaded project submittal PDF into the global bank. Everything is
    optional (filling out the bank entry is not required) - an unset name defaults
    to the material's description on the backend."""

    category: SubmittalCategory = "general_material"
    name: str | None = Field(None, min_length=1, max_length=300)
    size: str | None = Field(None, max_length=100)
    color: str | None = Field(None, max_length=100)
    made_in_usa: bool | None = None
    manufacturer: str | None = Field(None, max_length=200)
    notes: str | None = Field(None, max_length=2000)
    generate_aliases: bool = True


# ── Email ingestion ──────────────────────────────────────────────────────────


class EmailAssignIn(BaseModel):
    """Manual assignment of an ingested email to a project."""

    project_id: str = Field(min_length=1, max_length=100)


# ── Certified Payroll ────────────────────────────────────────────────────────

# Mirror the cp_* PG enums (0063/0064) - keep all three in sync with
# bdr_fe/lib/types.ts when a value is added.
CpReportType = Literal["lcp_tracker", "comply", "paper"]
CpShiftType = Literal["four_tens", "nights", "swing", "regular"]
CpDocCategory = Literal["w4", "i9", "certification", "license", "other"]
CpPayrollStatus = Literal[
    "draft",
    "awaiting_timesheet",
    "awaiting_payroll_detail",
    "processing",
    "processed",
    "submitted",
]
CpPaperReportKind = Literal["regular_weekly", "final"]

_CP_MONEY_BOUNDS = {"ge": Decimal(0), "le": Decimal("99999"), "decimal_places": 2}


class CpEnrollBody(BaseModel):
    """The enroll-into-Certified-Payroll hard gate: a project may not enter CP
    until every compliance field is supplied (the FE prefills the contractor
    address from cp_settings). Enrollment implies prevailing wage - there is no
    project_group. Legacy imports bypass this via the migration script only."""

    contract_id: str = Field(min_length=1, max_length=200)
    report_type: CpReportType
    shift_type: CpShiftType
    shift_start_time: time | None = None
    pwp_number: str = Field(min_length=1, max_length=200)
    public_body_awarding_contract: str = Field(min_length=1, max_length=300)
    contractor_address_street: str = Field(min_length=1, max_length=300)
    contractor_address_city: str = Field(min_length=1, max_length=100)
    contractor_address_state: str = Field(min_length=1, max_length=50)
    contractor_address_zip: str = Field(min_length=1, max_length=20)


class CpProjectCreate(CpEnrollBody):
    """Direct creation INSIDE Certified Payroll - a brand-new prevailing-wage
    project that never existed as a bid (the CP mirror of PMProjectCreate). It
    adds the projects spine (name / number / address) to the same hard-gated
    compliance set as enrollment, inherited wholesale from CpEnrollBody so the
    two can never drift. The service stamps current_stage='cp_only' and enrolls
    in one shot - mirroring the pm_only direct-create."""

    name: str = Field(min_length=1, max_length=300)
    number: str = Field(min_length=1, max_length=100)
    address: str | None = Field(None, max_length=500)


def _reject_explicit_nulls(model: BaseModel, fields: tuple[str, ...]) -> None:
    """Reject explicit JSON null for PATCH fields backed by NOT NULL columns.

    These Patch models are dumped with exclude_unset, so a field sent as null
    reaches the UPDATE as SET col = NULL and the DB rejects it with a raw 500
    (CORS-less). Turn that into a clean 422 at the edge. Fields not sent at all
    (not in model_fields_set) are untouched - only an explicit null is refused.
    """
    for name in fields:
        if name in model.model_fields_set and getattr(model, name) is None:
            raise ValueError(f"{name} cannot be null")


class CpDetailsPatch(BaseModel):
    """PATCH for cp_details. exclude_unset semantics: explicit null clears a
    nullable column. contract_id / shift_type / is_active are NOT NULL, so an
    explicit null on those is rejected (see _reject_explicit_nulls).
    Name/number edits go through the shared PATCH /projects/{id}."""

    contract_id: str | None = Field(None, min_length=1, max_length=200)
    report_type: CpReportType | None = None
    shift_type: CpShiftType | None = None
    shift_start_time: time | None = None
    pwp_number: str | None = Field(None, max_length=200)
    public_body_awarding_contract: str | None = Field(None, max_length=300)
    contractor_address_street: str | None = Field(None, max_length=300)
    contractor_address_city: str | None = Field(None, max_length=100)
    contractor_address_state: str | None = Field(None, max_length=50)
    contractor_address_zip: str | None = Field(None, max_length=20)
    is_active: bool | None = None

    @model_validator(mode="after")
    def _no_null_required(self):
        _reject_explicit_nulls(self, ("contract_id", "shift_type", "is_active"))
        return self


class CpEmployeeCreate(BaseModel):
    """Company-wide employee registry row. SSN policy: last four digits only."""

    employee_id: str | None = Field(None, max_length=50)
    first_name: str = Field(min_length=1, max_length=100)
    last_name: str = Field(min_length=1, max_length=100)
    alt_ee_name: str | None = Field(None, max_length=100)
    ssn_last_four: str | None = Field(None, pattern=r"^\d{4}$")
    personal_email: str | None = Field(None, max_length=320)
    jurisdiction: str | None = Field(None, pattern=r"^[A-Za-z]{2}$")
    classification_id: str | None = None
    is_active: bool = True


class CpEmployeePatch(BaseModel):
    employee_id: str | None = Field(None, max_length=50)
    first_name: str | None = Field(None, min_length=1, max_length=100)
    last_name: str | None = Field(None, min_length=1, max_length=100)
    alt_ee_name: str | None = Field(None, max_length=100)
    ssn_last_four: str | None = Field(None, pattern=r"^\d{4}$")
    personal_email: str | None = Field(None, max_length=320)
    jurisdiction: str | None = Field(None, pattern=r"^[A-Za-z]{2}$")
    classification_id: str | None = None
    is_active: bool | None = None

    @model_validator(mode="after")
    def _no_null_required(self):
        # first_name / last_name / is_active are NOT NULL on employees.
        _reject_explicit_nulls(self, ("first_name", "last_name", "is_active"))
        return self


class CpClassificationCreate(BaseModel):
    code: str = Field(min_length=1, max_length=20)
    name: str = Field(min_length=1, max_length=100)
    description: str | None = Field(None, max_length=2000)
    display_order: int = Field(0, ge=0, le=10000)
    is_field: bool = True
    is_apprentice: bool = False
    apprentice_period: int | None = Field(None, ge=1, le=10)
    percentage_of_journeyman: Decimal | None = Field(None, ge=0, le=Decimal("200"))


class CpClassificationPatch(BaseModel):
    code: str | None = Field(None, min_length=1, max_length=20)
    name: str | None = Field(None, min_length=1, max_length=100)
    description: str | None = Field(None, max_length=2000)
    display_order: int | None = Field(None, ge=0, le=10000)
    is_field: bool | None = None
    is_apprentice: bool | None = None
    apprentice_period: int | None = Field(None, ge=1, le=10)
    percentage_of_journeyman: Decimal | None = Field(None, ge=0, le=Decimal("200"))

    @model_validator(mode="after")
    def _no_null_required(self):
        # code / name / display_order / is_field / is_apprentice are NOT NULL.
        _reject_explicit_nulls(
            self, ("code", "name", "display_order", "is_field", "is_apprentice")
        )
        return self


class CpRateCreate(BaseModel):
    """total_hourly is always recomputed server-side (base + fringes)."""

    classification_id: str = Field(min_length=1, max_length=100)
    hourly_rate: Decimal = Field(**_CP_MONEY_BOUNDS)
    overtime_rate: Decimal = Field(**_CP_MONEY_BOUNDS)
    doubletime_rate: Decimal = Field(**_CP_MONEY_BOUNDS)
    pension: Decimal = Field(Decimal(0), **_CP_MONEY_BOUNDS)
    health_welfare: Decimal = Field(Decimal(0), **_CP_MONEY_BOUNDS)
    training: Decimal = Field(Decimal(0), **_CP_MONEY_BOUNDS)
    other: Decimal = Field(Decimal(0), **_CP_MONEY_BOUNDS)
    dues: Decimal = Field(Decimal(0), **_CP_MONEY_BOUNDS)
    effective_date: date | None = None


class CpRatePatch(BaseModel):
    hourly_rate: Decimal | None = Field(None, **_CP_MONEY_BOUNDS)
    overtime_rate: Decimal | None = Field(None, **_CP_MONEY_BOUNDS)
    doubletime_rate: Decimal | None = Field(None, **_CP_MONEY_BOUNDS)
    pension: Decimal | None = Field(None, **_CP_MONEY_BOUNDS)
    health_welfare: Decimal | None = Field(None, **_CP_MONEY_BOUNDS)
    training: Decimal | None = Field(None, **_CP_MONEY_BOUNDS)
    other: Decimal | None = Field(None, **_CP_MONEY_BOUNDS)
    dues: Decimal | None = Field(None, **_CP_MONEY_BOUNDS)
    effective_date: date | None = None


class CpReportCreate(BaseModel):
    """Any date within the target week - the service snaps it to Sun–Sat."""

    week_start_date: date


class CpPaperReportInput(BaseModel):
    """Per-project metadata collected before generating a paper CPR."""

    project_id: str = Field(min_length=1, max_length=100)
    report_number: str = Field(min_length=1, max_length=50)
    report_type: CpPaperReportKind = "regular_weekly"
    notes: str | None = Field(None, max_length=2000)


class CpGenerateBody(BaseModel):
    """Optional body for CPR generation - required only when paper-type
    projects are in scope for the week."""

    paper_reports: list[CpPaperReportInput] | None = None


class CpSettingsUpdate(BaseModel):
    """Company-wide subcontractor identity printed on every report."""

    name: str | None = Field(None, max_length=300)
    street_address: str | None = Field(None, max_length=500)
    city: str | None = Field(None, max_length=100)
    state: str | None = Field(None, max_length=50)
    zip_code: str | None = Field(None, max_length=20)
    phone: str | None = Field(None, max_length=50)
    license_number: str | None = Field(None, max_length=100)


class CpSignerProfileUpdate(BaseModel):
    """The caller's own signer identity for the paper CPR compliance statement."""

    first_name: str | None = Field(None, max_length=100)
    last_name: str | None = Field(None, max_length=100)
    job_title: str | None = Field(None, max_length=150)
    personal_email: str | None = Field(None, max_length=320)
    date_of_birth: date | None = None


class CpIgnoredProjectCreate(BaseModel):
    """Registry entry marking a raw timesheet project name as intentionally
    non-payroll: counted for OT/proration, never reported, never nagging."""

    raw_name: str = Field(min_length=1, max_length=300)
    raw_number: str | None = Field(None, max_length=100)
    shift_type: CpShiftType = "regular"
    note: str | None = Field(None, max_length=2000)


# ── Estimator hand-off: send-batch log + compact summary (0075/0076) ─────────
#
# These shape the two new estimator-hand-off reads. The service layer
# (app.services.file_sends: build_log / build_handoff) does the role-dependent
# scoping and returns plain dicts; these models are the response contract.
#
# PRIVACY - the estimator projection: for the estimator viewer, build_log OMITS
# the `recipients` and `sent_by_name` keys entirely (they are absent, not null),
# so no co-assignee's identity is ever serialized. A router MUST NOT re-add them
# via a response_model that fills the Optional fields with null: return the
# service dict as-is, or serialize with the null keys excluded. Two projections,
# never one payload post-filtered.


class SendBatchFileOut(BaseModel):
    file_id: str
    category: str
    # 0077 - WHICH DOCUMENT SET a post-hand-off file belongs to. Set only on
    # 'revision' / 'addendum'; None on the initial package (whose category is
    # already the document set) and on rows predating the column.
    doc_type: Literal["drawing", "specification"] | None = None
    filename: str
    size_bytes: int | None = None
    note: str | None = None
    addendum_number: str | None = None
    addendum_issued_on: date | None = None
    # False when the project_files row is gone, or (estimator) when the file is
    # no longer visible to them. Render greyed, no open, exclude from the ZIP.
    available: bool = True


class SendBatchRecipientOut(BaseModel):
    estimator_id: str | None = None
    full_name: str | None = None
    email: str


class SendBatchOut(BaseModel):
    id: str
    kind: Literal["initial", "revision", "reassign"]
    sent_at: datetime
    message: str | None = None
    reconstructed: bool = False
    counts: dict[str, int]  # from the batch.summary snapshot, not the live join
    # 0077 - per-section "what changed" notes captured at send time, keyed by
    # file_categories.section_key(): "revision:drawing", "revision:specification",
    # "addendum", "additional". Shown to BOTH viewers (the estimator is who they
    # are written for); the per-file `note` still describes each file.
    section_notes: dict[str, str] = Field(default_factory=dict)
    files: list[SendBatchFileOut]
    # INTERNAL ONLY. Both keys are ABSENT (not null) in the estimator payload -
    # build_log emits a separate dict shape, never a post-filter.
    recipients: list[SendBatchRecipientOut] | None = None
    sent_by_name: str | None = None


class SendBatchLogOut(BaseModel):
    viewer: Literal["internal", "estimator"]
    batches: list[SendBatchOut]  # newest first


class HandoffAssigneeOut(BaseModel):
    assignment_id: str
    estimator_id: str
    full_name: str | None = None
    email: str | None = None  # ALWAYS None for Role.ESTIMATOR
    due_at: datetime | None = None
    expires_at: datetime | None = None
    revoked_at: datetime | None = None
    sent_to_estimator_at: datetime | None = None


class LatestAddendumOut(BaseModel):
    number: str
    issued_on: date


class HandoffOut(BaseModel):
    # SOURCE OF TRUTH for the button predicates. sent_at of the kind='initial'
    # batch; None => the initial package was never emailed. NOT the same as
    # `locked`.
    package_sent_at: datetime | None = None
    last_sent_at: datetime | None = None
    batch_count: int = 0
    due_back_at: datetime | None = None  # projects.due_from_estimator_at
    # Internal: every assignment, revoked included, newest first.
    # ESTIMATOR: EXACTLY the caller's own row, email blanked.
    assignees: list[HandoffAssigneeOut]
    # Uploaded, never emailed. Internal only; {} for the estimator.
    staged: dict[str, int]
    # Cumulative distinct files across the caller's visible batches.
    sent: dict[str, int]
    latest_addendum: LatestAddendumOut | None = None
    locked: bool  # mirrors GET /files/lock
    # ESTIMATOR only: their own assignment window. Never another's.
    my_access_expires_at: datetime | None = None
    my_due_at: datetime | None = None


# ── External submission ("Mark submitted" from the side menu, 0140) ─────────
# A proposal sent outside the app, recorded from any stage past Go/No-Go.
# See services/external_submission.py and docs/EXTERNAL_SUBMISSION.md.

ExternalPricingSection = Literal["materials", "gear", "underground", "low_voltage"]


class ExternalSubmissionRequestIn(BaseModel):
    # Optional note to the approvers ("sent through the GC portal at 3 PM").
    message: str | None = Field(default=None, max_length=2000)


class ExternalSubmissionDenyIn(BaseModel):
    reason: str | None = Field(default=None, max_length=2000)


class ExternalSubmissionUndoIn(BaseModel):
    reason: str | None = Field(default=None, max_length=2000)


class ExternalSubmissionLineIn(BaseModel):
    # One material category in one pricing section. The section is posted so
    # the server can prove the category really belongs to it.
    material_category_id: str = Field(..., min_length=1, max_length=64)
    pricing_section: ExternalPricingSection
    amount: Decimal = Field(..., ge=0, max_digits=14, decimal_places=2)
    note: str | None = Field(default=None, max_length=2000)


class ExternalSubmissionMarkupIn(BaseModel):
    # Mirrors the Markup step: a percent and the dollar amount it works out to.
    # The amount is what prices the bid; the percent is kept for the record.
    # Under 1000: markups.*_markup_pct is numeric(6,3).
    pct: Decimal | None = Field(default=None, ge=0, lt=1000, max_digits=6, decimal_places=3)
    amount: Decimal | None = Field(default=None, ge=0, max_digits=14, decimal_places=2)


class ExternalSubmissionMarkupsIn(BaseModel):
    materials: ExternalSubmissionMarkupIn = Field(default_factory=ExternalSubmissionMarkupIn)
    gear: ExternalSubmissionMarkupIn = Field(default_factory=ExternalSubmissionMarkupIn)
    underground: ExternalSubmissionMarkupIn = Field(default_factory=ExternalSubmissionMarkupIn)
    low_voltage: ExternalSubmissionMarkupIn = Field(default_factory=ExternalSubmissionMarkupIn)
    labor: ExternalSubmissionMarkupIn = Field(default_factory=ExternalSubmissionMarkupIn)


class ExternalSubmissionGcIn(BaseModel):
    # included = submitted to this GC; excluded = no bid. The five amounts are
    # this GC's own per-section overrides (null = the project price, cost plus
    # markup), exactly like project_gcs.proposal_*_amount.
    gc_id: str = Field(..., min_length=1, max_length=64)
    included: bool
    material_amount: Decimal | None = Field(default=None, ge=0, max_digits=14, decimal_places=2)
    gear_amount: Decimal | None = Field(default=None, ge=0, max_digits=14, decimal_places=2)
    underground_amount: Decimal | None = Field(default=None, ge=0, max_digits=14, decimal_places=2)
    low_voltage_amount: Decimal | None = Field(default=None, ge=0, max_digits=14, decimal_places=2)
    labor_amount: Decimal | None = Field(default=None, ge=0, max_digits=14, decimal_places=2)


class ExternalSubmissionIn(BaseModel):
    lines: list[ExternalSubmissionLineIn] = Field(default_factory=list, max_length=300)
    labor_amount: Decimal = Field(default=Decimal(0), ge=0, max_digits=14, decimal_places=2)
    labor_note: str | None = Field(default=None, max_length=2000)
    markups: ExternalSubmissionMarkupsIn = Field(default_factory=ExternalSubmissionMarkupsIn)
    # May be empty: GCs whose proposal already went out through Send Out are
    # never posted (they are left untouched) yet still count as included, so
    # a bid already sent to every GC posts no rows. The service enforces "at
    # least one GC included" over both.
    gcs: list[ExternalSubmissionGcIn] = Field(default_factory=list, max_length=100)
