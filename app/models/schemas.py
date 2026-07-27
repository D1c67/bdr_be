"""Pydantic request/response models for the BDR API."""

from datetime import date, datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, EmailStr, Field, computed_field, field_validator

from app.core.roles import Role
from app.services.due_reminder_prefs import NotificationPrefsDoc
from app.services.project_status import ProjectStatus

# ── Profiles / users ──────────────────────────────────────────────────────

# Account lifecycle, derived from is_active + invite_accepted_at:
#   "disabled" — admin turned the account off (is_active = false)
#   "invited"  — invite email sent, user hasn't accepted it yet
#   "active"   — user accepted the invite and has authenticated
UserStatus = Literal["active", "invited", "disabled"]

# Supported UI / notification languages. Mirrors SUPPORTED_LOCALES in the
# frontend (bdr_fe/lib/locales.ts) and the profiles.locale CHECK constraint
# (migration 0040) — keep all three in sync when adding a language.
SupportedLocale = Literal["en", "fil", "ceb", "sw", "hi", "ur"]


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


class UpdateMeIn(BaseModel):
    """Self-service profile edits — display name and UI language. Each field is
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

    `is_customized` drives the Settings page's "Reset to default" button —
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


# Membership is just the link — any GC on a project is a bid candidate; who
# we actually bid to is recorded by which proposals were sent (Send Out).
class ProjectGCIn(BaseModel):
    gc_id: str


class ProjectCreate(BaseModel):
    name: str
    number: str
    # Required at intake — mirrored by the New Project form's `required` fields.
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
    # True when the project came to us from NGEM (checkbox on the intake form).
    is_ngem: bool = False
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


class ProjectUpdate(BaseModel):
    name: str | None = None
    number: str | None = None
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
    is_ngem: bool | None = None
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


class CategoryStateOut(BaseModel):
    """One category's progress head (source of truth for the bidding board)."""

    category: str
    current_task: str
    status: str  # 'locked' | 'active' | 'complete'
    owner_role: Role | None = None
    completed_at: datetime | None = None


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
    # True when the project originated from NGEM. Default lets reads degrade
    # gracefully before migration 0046 is applied.
    is_ngem: bool = False
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
    # Last successful files export (drives the post-send-out export banner).
    # Default lets reads degrade gracefully before migration 0041 is applied.
    files_exported_at: datetime | None = None
    created_by: str | None
    created_at: datetime
    updated_at: datetime


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


# ── Go / No-Go ──────────────────────────────────────────────────────────--


class GonoDecisionIn(BaseModel):
    outcome: Literal["go", "no_go"]
    note: str | None = None


# ── Vendors ───────────────────────────────────────────────────────────────


class VendorIn(BaseModel):
    name: str
    notes: str | None = None


class VendorContactIn(BaseModel):
    vendor_id: str
    name: str
    email: EmailStr
    phone: str | None = None
    material_category_id: str | None = None


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
    # None = the default set (BOM split + drawings + Trenching markup). An
    # explicit list (possibly empty) is exactly what the PE left in the confirm
    # modal after adding/removing files — what they saw is what gets sent.
    attachment_file_ids: list[str] | None = Field(default=None, max_length=50)


class RFQBulkSendIn(BaseModel):
    # One email per contact per group — recipients are never CC'd together.
    groups: list[RFQBulkSendGroup] = Field(..., min_length=1, max_length=100)
    # PE-edited body template: "<Contact Name>" is replaced per recipient and
    # the text is sent verbatim (no AI variation). None = generated default.
    email_body: str | None = Field(None, max_length=20_000)

    @field_validator("email_body")
    @classmethod
    def _blank_body_means_default(cls, v: str | None) -> str | None:
        # A whitespace-only edit means "no custom body", never an empty email.
        return v if v and v.strip() else None


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


class QuoteOverrideIn(BaseModel):
    amount: Decimal = Field(**_AMOUNT_BOUNDS)
    note: str | None = None


class RfqCustomPriceIn(BaseModel):
    """Custom category price on the receive-quotes step; null clears it."""

    amount: Decimal | None = Field(None, **_AMOUNT_BOUNDS)
    note: str | None = None


class RfqQuotesConfirmIn(BaseModel):
    """Receive-quotes attestation: the PE confirms the vendor quoted the
    entire RFQ and didn't miss a material (false retracts it)."""

    confirmed: bool


class TaxIn(BaseModel):
    """Tax attestation for a priced figure on the receive-quotes step — a vendor
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


class BoqRefineIn(BaseModel):
    message: str


class RFQLineItemIn(BaseModel):
    site_name: str | None = None
    sr_no: str | None = None
    description: str
    quantity: Decimal | None = None
    unit: str | None = None
    notes: str | None = None


class RFQGroupIn(BaseModel):
    material_category_id: str
    # Comfortably above any real BOQ category, but bounds the rfq_line_items bulk
    # insert (and the generated workbook size) against a runaway request.
    items: list[RFQLineItemIn] = Field(default_factory=list, max_length=2000)


class BoqConfirmIn(BaseModel):
    # One group per material category; sites already merged client-side, invented
    # categories already mapped to a material_category_id. Material categories are
    # a small fixed table, so a modest cap can't reject legitimate input.
    groups: list[RFQGroupIn] = Field(..., min_length=1, max_length=50)


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
    notes: str | None = None


class GeneralMaterialIn(BaseModel):
    # Manual entry / override of the general-material (wiring) price when the
    # estimate extraction can't find it or the PM/PE wants to correct it.
    amount: Decimal | None = None


class VerifyOverrideIn(BaseModel):
    # The final figures the Executive/PM commit at the verify step (9). Stored as
    # a snapshot on `verifications` so the upstream tables stay untouched and the
    # delta from the original numbers remains computable.
    labor_amount: Decimal | None = None
    materials_amount: Decimal | None = None
    labor_markup_amount: Decimal | None = None
    materials_markup_amount: Decimal | None = None
    notes: str | None = None


# ── Send Out / proposals (step 10) ──────────────────────────────────────--


class ProposalGenerateIn(BaseModel):
    boq_file_id: str | None = None  # default: latest 'boq' upload


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


class ProposalAmountsIn(BaseModel):
    # One GC's proposal figures (GC Pricing step editor). None clears the
    # override back to the pricing base; the total is never stored — it is
    # always material + labor.
    material_amount: Decimal | None = Field(None, **_AMOUNT_BOUNDS)
    labor_amount: Decimal | None = Field(None, **_AMOUNT_BOUNDS)


class ProposalSendIn(BaseModel):
    proposal_ids: list[str] = Field(..., min_length=1, max_length=100)
    # proposal_id → gc_contact ids chosen in the confirm dialog. Missing key =
    # all contacts with an email (legacy clients / tests).
    contacts: dict[str, list[str]] | None = None
    email_body: str | None = None  # None = generated cover note
    force: bool = False  # required to retry an outcome-unknown failure

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


# ── Win / Loss (bid outcome) — final step ───────────────────────────────────


class BidGcOutcomeIn(BaseModel):
    # One GC we bid to. All detail is optional / "unknown" — the PA records what
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


# ── Estimator hand-off: send-batch log + compact summary (0075/0076) ─────────
#
# These shape the two new estimator-hand-off reads. The service layer
# (app.services.file_sends: build_log / build_handoff) does the role-dependent
# scoping and returns plain dicts; these models are the response contract.
#
# PRIVACY — the estimator projection: for the estimator viewer, build_log OMITS
# the `recipients` and `sent_by_name` keys entirely (they are absent, not null),
# so no co-assignee's identity is ever serialized. A router MUST NOT re-add them
# via a response_model that fills the Optional fields with null: return the
# service dict as-is, or serialize with the null keys excluded. Two projections,
# never one payload post-filtered.


class SendBatchFileOut(BaseModel):
    file_id: str
    category: str
    # 0077 — WHICH DOCUMENT SET a post-hand-off file belongs to. Set only on
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
    # 0077 — per-section "what changed" notes captured at send time, keyed by
    # file_categories.section_key(): "revision:drawing", "revision:specification",
    # "addendum", "additional". Shown to BOTH viewers (the estimator is who they
    # are written for); the per-file `note` still describes each file.
    section_notes: dict[str, str] = Field(default_factory=dict)
    files: list[SendBatchFileOut]
    # INTERNAL ONLY. Both keys are ABSENT (not null) in the estimator payload —
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
