"""Pydantic request/response models for the BDR API."""

import re
from datetime import date, datetime, time
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, EmailStr, Field, computed_field, field_validator, model_validator

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
    created_by: str | None
    created_at: datetime
    updated_at: datetime


class FilesExportIn(BaseModel):
    """Subset selector for the project-files ZIP export.

    Omit `file_ids` (or send `{}`) to export every file the caller may read;
    `[]` is rejected so "export all" is always explicit, not an accident.
    """

    file_ids: list[str] | None = None

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
    to_stage: str
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
    """Direct creation in Project Management — a project awarded without a bid,
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
# It is only the outer guard against an absurd payload — routers/pm_field.py
# applies the real check to the sanitized value, so markup the author never sees
# can't be what blocks them.
RFI_QUESTION_MAX_CHARS = 24000

# drawing_numbers / applicable_references are free-text chips ("E-101", "Spec
# 26 05 19") — there is no drawings or specs table to point at (see 0068).
_RFI_CHIP_MAX_ITEMS = 50
_RFI_CHIP_MAX_CHARS = 100

# Documents-hub handles ("source:id"), mirroring the rfi_attachments.doc_key
# CHECK constraint. Shape only — routers/pm_field.py is what proves a key names
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
    # Not a column on `rfis` — the router writes these to rfi_attachments.
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
    # Explicit null unassigns — these are nullable FKs.
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

    Distinct from the answer→answered convenience — that only records that an
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


# gc_contacts ids the send is addressed to. Capped like the attachment list — an
# RFI never legitimately fans out to dozens of contacts, and it bounds the payload.
_RFI_RECIPIENTS_MAX = 50


class RFISendIn(BaseModel):
    """App send: email the RFI (as a filled PDF) to selected GC contacts.

    The router proves each contact belongs to the RFI's assigned company and has an
    email address — this only bounds and de-dupes the raw id list.
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
    """A PM material line — the same shape a BOQ extraction item carries
    (no pricing). material_category_id None = uncategorized."""

    material_category_id: str | None = None
    description: str = Field(min_length=1, max_length=2000)
    quantity: Decimal | None = Field(None, ge=0, le=Decimal("999999999"))
    unit: str | None = Field(None, max_length=100)
    notes: str | None = Field(None, max_length=2000)
    site_name: str | None = Field(None, max_length=300)


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
    is a per-request snapshot — it never touches pm_materials."""

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
    # Project materials the sender unchecked — recorded for the "these never had
    # submittals requested" view; they simply produce no request items.
    deselected_material_ids: list[str] = Field(default_factory=list, max_length=1000)
    email_body: str | None = Field(None, max_length=20_000)

    @field_validator("email_body")
    @classmethod
    def _blank_body_means_default(cls, v: str | None) -> str | None:
        # A whitespace-only edit means "no custom body", never an empty email.
        return v if v and v.strip() else None


# ── Submittal Bank ───────────────────────────────────────────────────────────

# Mirror the submittal_category PG enum (0072) — keep in sync with
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
    optional (filling out the bank entry is not required) — an unset name defaults
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

# Mirror the cp_* PG enums (0063/0064) — keep all three in sync with
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
    address from cp_settings). Enrollment implies prevailing wage — there is no
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
    """Direct creation INSIDE Certified Payroll — a brand-new prevailing-wage
    project that never existed as a bid (the CP mirror of PMProjectCreate). It
    adds the projects spine (name / number / address) to the same hard-gated
    compliance set as enrollment, inherited wholesale from CpEnrollBody so the
    two can never drift. The service stamps current_stage='cp_only' and enrolls
    in one shot — mirroring the pm_only direct-create."""

    name: str = Field(min_length=1, max_length=300)
    number: str = Field(min_length=1, max_length=100)
    address: str | None = Field(None, max_length=500)


def _reject_explicit_nulls(model: BaseModel, fields: tuple[str, ...]) -> None:
    """Reject explicit JSON null for PATCH fields backed by NOT NULL columns.

    These Patch models are dumped with exclude_unset, so a field sent as null
    reaches the UPDATE as SET col = NULL and the DB rejects it with a raw 500
    (CORS-less). Turn that into a clean 422 at the edge. Fields not sent at all
    (not in model_fields_set) are untouched — only an explicit null is refused.
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
    """Any date within the target week — the service snaps it to Sun–Sat."""

    week_start_date: date


class CpPaperReportInput(BaseModel):
    """Per-project metadata collected before generating a paper CPR."""

    project_id: str = Field(min_length=1, max_length=100)
    report_number: str = Field(min_length=1, max_length=50)
    report_type: CpPaperReportKind = "regular_weekly"
    notes: str | None = Field(None, max_length=2000)


class CpGenerateBody(BaseModel):
    """Optional body for CPR generation — required only when paper-type
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
