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

LaborTime = Literal["day_work", "night_work"]
WageType = Literal["prevailing_wage", "non_prevailing_wage"]

# Go/No-Go scoring answers (reference only). The rubric — labels, points,
# thresholds — lives in the frontend (bdr_fe/lib/gonoScoring.ts); these
# Literals are its value lists verbatim and must stay in sync with it. The
# backend stores the answers but never scores or acts on them.
ProjectType = Literal[
    "new_construction",
    "ti",
    "multi_family",
    "casino_strip",
    "casino_other",
    "lighting",
    "roadway",
    "generator",
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
]
LaborNeeded = Literal["union", "ce_cw", "non_union"]
BidMethod = Literal["hard_bid", "cmar", "single_gc_hard_bid"]
CompetitorKnown = Literal["yes_1_2", "yes_3_plus", "no_unknown", "only_ec_bidding"]
GCKnown = Literal[
    "yes_1_2",
    "yes_3_plus",
    "no_unknown",
    "only_gc_bidding",
    "no_gc_needed",
]
SubsNeeded = Literal[
    "no",
    "yes_underground",
    "yes_low_voltage",
    "yes_fire_alarm",
    "two_subs",
    "three_plus_subs",
]
EstValueBand = Literal["under_50k", "50k_150k", "150k_500k", "500k_1m", "1m_3m", "over_3m"]
ScopeFit = Literal["yes", "no", "maybe"]


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
    status: ProjectStatus = "active"
    created_by: str | None
    created_at: datetime
    updated_at: datetime


# ── Workflow ────────────────────────────────────────────────────────────--


class TransitionIn(BaseModel):
    to_stage: str
    note: str | None = None


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


class VoteIn(BaseModel):
    vote: Literal["go", "no_go"]
    comment: str | None = None


class OverrideIn(BaseModel):
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
    vendor_contact_ids: list[str]
    # None = the default set (BOM split + drawings + Trenching markup). An
    # explicit list (possibly empty) is exactly what the PE left in the confirm
    # modal after adding/removing files — what they saw is what gets sent.
    attachment_file_ids: list[str] | None = None


class RFQBulkSendIn(BaseModel):
    # One email per contact per group — recipients are never CC'd together.
    groups: list[RFQBulkSendGroup]
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


# ── BOQ → RFQ extraction ──────────────────────────────────────────────────


class BoqAnalysisStart(BaseModel):
    # Defaults to the project's most recent BOQ upload when omitted.
    boq_file_id: str | None = None


class BoqRefineIn(BaseModel):
    message: str


class BoqResultIn(BaseModel):
    # The PE's directly-edited extraction payload ({sites:[...], ...}).
    result_json: dict


class RFQLineItemIn(BaseModel):
    site_name: str | None = None
    sr_no: str | None = None
    description: str
    quantity: Decimal | None = None
    unit: str | None = None
    notes: str | None = None


class RFQGroupIn(BaseModel):
    material_category_id: str
    items: list[RFQLineItemIn]


class BoqConfirmIn(BaseModel):
    # One group per material category; sites already merged client-side, invented
    # categories already mapped to a material_category_id.
    groups: list[RFQGroupIn]


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
    # One GC's proposal figures (Send Out numbers editor). None clears the
    # override back to the committed pricing default; the total is never
    # stored — it is always material + labor.
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
