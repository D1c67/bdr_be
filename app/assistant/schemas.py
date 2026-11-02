"""Pydantic models for the phase 0 API contract (docs/ASSISTANT.md and the
phase 0 contract in the build task), the phase 1 approval routes
(docs/ASSISTANT_PHASE1_PLAN.md section 5), the phase 2 staging, task and
splitter routes (docs/ASSISTANT_PHASE2_PLAN.md section 5) and the phase 3
routines, memory and holidays routes (docs/ASSISTANT_PHASE3_PLAN.md section
5) and the phase 4 reason, report, memory search and find shapes
(docs/ASSISTANT_PHASE4_PLAN.md section 5). Times are ISO 8601 UTC strings as the database returns them."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.assistant.tools._common import is_uuid

DISPLAY_NAME_MAX = 40
CONTENT_MAX = 8_000
PATH_MAX = 300

SURFACES = ("panel", "page")
Status = Literal["pending", "running", "waiting_approval", "waiting_review", "done", "failed"]
SHA256_PATTERN = r"^[0-9a-f]{64}$"
ACTIVITY_MAX = 50
FILES_PER_MESSAGE = 10
FileState = Literal["staged", "consumed", "expired"]


def _no_em_dash(value: str | None, label: str) -> str | None:
    if value is not None and "\u2014" in value:
        raise ValueError(f"{label} cannot contain an em dash")
    return value


def _uuid_or_none(value: str | None) -> str | None:
    """project_id lands in a uuid column; anything else would be a 22P02
    from PostgREST (an unhandled 500 that loses its CORS headers)."""
    if value is None:
        return None
    value = value.strip()
    if not value:
        return None
    if not is_uuid(value):
        raise ValueError("project_id must be a uuid")
    return value


class HealthOut(BaseModel):
    available: bool
    reason: str | None = None


class SettingsOut(BaseModel):
    display_name: str | None
    default_display_name: str
    enabled: bool
    timezone: str
    daily_cost_cap_usd: float


class SettingsPatchIn(BaseModel):
    display_name: str | None = None

    @field_validator("display_name")
    @classmethod
    def _clean(cls, v: str | None) -> str | None:
        if v is None:
            return None
        v = " ".join(v.split()).strip()
        if not v:
            return None
        if len(v) > DISPLAY_NAME_MAX:
            raise ValueError(f"display_name must be at most {DISPLAY_NAME_MAX} characters")
        return _no_em_dash(v, "display_name")


class ConversationOut(BaseModel):
    mode: Literal["normal", "mailbox_review"] = "normal"
    mailbox_tainted_at: str | None = None
    id: str
    title: str | None = None
    surface: str
    project_id: str | None = None
    created_at: str
    last_message_at: str | None = None
    # surface 'schedule': the routine whose deliveries the conversation holds.
    schedule_id: str | None = None


class ConversationListOut(BaseModel):
    items: list[ConversationOut]


class ConversationCreateIn(BaseModel):
    mode: Literal["normal", "mailbox_review"] = "normal"
    surface: Literal["panel", "page"] = "panel"
    project_id: str | None = None
    path: str | None = Field(default=None, max_length=PATH_MAX)

    @field_validator("project_id")
    @classmethod
    def _project_uuid(cls, v: str | None) -> str | None:
        return _uuid_or_none(v)


class MessageContextIn(BaseModel):
    path: str | None = Field(default=None, max_length=PATH_MAX)
    project_id: str | None = None

    @field_validator("project_id")
    @classmethod
    def _project_uuid(cls, v: str | None) -> str | None:
        return _uuid_or_none(v)


class MessageCreateIn(BaseModel):
    content: str = Field(min_length=1, max_length=CONTENT_MAX)
    context: MessageContextIn | None = None
    # Staged files the message carries (docs/ASSISTANT_PHASE2_PLAN.md 3.2):
    # uuids from POST /assistant/files, distinct, at most 10. A send with
    # files still needs words (content stays required).
    file_ids: list[str] = Field(default_factory=list, max_length=FILES_PER_MESSAGE)
    # Incognito (docs/ASSISTANT_PHASE3_PLAN.md 3.9.3): the turn neither reads
    # nor writes memory. Stored on the user row and the pending answer.
    incognito: bool = False

    @field_validator("content")
    @classmethod
    def _not_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("content cannot be blank")
        return v

    @field_validator("file_ids")
    @classmethod
    def _file_uuids(cls, v: list[str]) -> list[str]:
        out = [str(i).strip() for i in v]
        if any(not is_uuid(i) for i in out):
            raise ValueError("file_ids must be uuids")
        if len(set(out)) != len(out):
            raise ValueError("file_ids must be distinct")
        return out


class ToolCallProgressOut(BaseModel):
    seq: int
    tool_name: str
    progress_label: str
    ok: bool | None = None
    duration_ms: int | None = None


class MessageFileOut(BaseModel):
    """A staged file a user message carried (docs/ASSISTANT_PHASE2_PLAN.md
    5.2). `state`: staged (still live and unclaimed), consumed (a split took
    it) or expired (retired or past its 7 days)."""

    id: str
    filename: str
    size_bytes: int
    pages: int | None = None
    state: FileState


class UsedMemoryOut(BaseModel):
    id: str
    title: str
    kind: str


class MessageOut(BaseModel):
    id: str
    conversation_id: str
    role: Literal["user", "assistant"]
    content: str
    status: Status
    tool_calls: list[ToolCallProgressOut]
    blocks: list[dict[str, Any]]
    error: str | None = None
    created_at: str
    files: list[MessageFileOut] = Field(default_factory=list)
    incognito: bool = False
    # The notes the answer tagged, resolved at read time (a deleted note
    # simply disappears).
    used_memories: list[UsedMemoryOut] = Field(default_factory=list)


class ConversationDetailOut(BaseModel):
    conversation: ConversationOut
    messages: list[MessageOut]


class MessageAcceptedOut(BaseModel):
    user_message: MessageOut
    assistant_message: MessageOut
    job_id: str | None = None


class SkillSummaryOut(BaseModel):
    name: str
    description: str
    version: str


class SkillOut(SkillSummaryOut):
    body: str


# ── Approvals (phase 1) ──────────────────────────────────────────────────────


class ApprovalRevertOut(BaseModel):
    id: str
    status: str


class ApprovalOut(BaseModel):
    """docs/ASSISTANT_PHASE1_PLAN.md 5.2. The payload is never served."""

    id: str
    status: str
    display_status: str
    action_kind: str
    project_id: str | None = None
    conversation_id: str | None = None
    message_id: str | None = None
    preview: dict[str, Any]
    payload_sha256: str
    precondition: dict[str, Any]
    expires_at: str
    expired_reason: str | None = None
    superseded_by: str | None = None
    reverts_approval_id: str | None = None
    decided_at: str | None = None
    executed_at: str | None = None
    created_at: str
    result: dict[str, Any] | None = None
    revertible: bool
    revert_blocked: str | None = None
    revert: ApprovalRevertOut | None = None
    diff: list[dict[str, Any]] | None = None


class ApproveIn(BaseModel):
    payload_sha256: str = Field(pattern=SHA256_PATTERN)
    precondition: dict[str, Any]
    # Required (10 or more characters after trimming) for sensitive_read
    # cards only; the route answers 422 reason_required. Other cards ignore
    # it. Never part of the hashed payload (3.2.2).
    reason: str | None = Field(default=None, max_length=500)


class ResolveIn(BaseModel):
    outcome: Literal["applied", "not_applied"]


class RecheckRowOut(BaseModel):
    field: str
    label: str
    before: str
    live: str


class RecheckOut(BaseModel):
    approval: ApprovalOut
    rows: list[RecheckRowOut]
    matches_before: bool
    matches_intended: bool | None = None


class ActivityOut(BaseModel):
    items: list[ApprovalOut]
    next_before: str | None = None


# ── Staging, tasks and the splitter (phase 2) ────────────────────────────────


class StagedFileOut(BaseModel):
    """POST /assistant/files and GET /assistant/files
    (docs/ASSISTANT_PHASE2_PLAN.md 5.2). `filename` is the normalized name
    every later surface shows."""

    id: str
    filename: str
    size_bytes: int
    pages: int | None = None
    sha256: str
    expires_at: str
    reused: bool = False


class TaskProjectOut(BaseModel):
    id: str
    number: str | None = None


class TaskJobOut(BaseModel):
    id: str
    status: str
    file_count: int = 0
    files_done: int = 0
    files_failed: int = 0


class TaskFileOut(BaseModel):
    """One file of a split task. `filename` is tainted text (the UI renders
    it escaped). `outcome`: pending, split, whole, failed or missing;
    `reason` (for whole): package_sent, no_documents, run_failed,
    refile_failed or other."""

    split_file_id: str
    filename: str
    status: str
    outcome: str
    documents: int = 0
    categories: dict[str, int] = Field(default_factory=dict)
    reason: str | None = None


class TaskOut(BaseModel):
    """GET /assistant/tasks/{id} (docs/ASSISTANT_PHASE2_PLAN.md 5.2)."""

    id: str
    kind: str
    status: str
    title: str | None = None
    project: TaskProjectOut | None = None
    conversation_id: str | None = None
    approval_id: str | None = None
    failure_reason: str | None = None
    created_at: str
    finished_at: str | None = None
    job: TaskJobOut | None = None
    files: list[TaskFileOut] = Field(default_factory=list)
    ready: bool = False


class AeTaskOut(TaskOut):
    """GET /assistant/tasks/{id} for the phase 4 kinds (plans_read_pages and
    ai_estimating_run): TaskOut plus the AI Estimating run the task's card
    links to (payload.plans_run_id, or the run id the composite route stored
    once the run exists) and the settled result {cost_usd, pages}. A subclass,
    so the phase 2 split shape is untouched."""

    plans_run_id: str | None = None
    result: dict[str, Any] | None = None


class SplitStatusOut(BaseModel):
    """GET /assistant/split/status (docs/ASSISTANT_PHASE2_PLAN.md 3.10.3,
    before the tool trims it; ids are full). The lists are the service's
    rows as they come (split_handoff.status_for)."""

    splitter: dict[str, Any]
    limits: dict[str, Any]
    staged: list[dict[str, Any]] = Field(default_factory=list)
    jobs: list[dict[str, Any]] = Field(default_factory=list)
    tasks: list[dict[str, Any]] = Field(default_factory=list)
    project: dict[str, Any] | None = None
    spend: dict[str, Any] = Field(default_factory=dict)


# ── Routines, memory and holidays (phase 3) ──────────────────────────────────
#
# docs/ASSISTANT_PHASE3_PLAN.md 5.2. The request models check types and
# bound sizes only: the cadence is judged by rrules.parse_cadence (422
# bad_cadence), the recipe params by recipes.validate_params (422
# bad_params) and a note's fields by memory.clean_fields (422 bad_memory),
# so each refusal carries its own code and sentence.

Delivery = Literal["chat", "chat_bell", "chat_bell_email"]
ACTIVE_SCHEDULE_CAP = 10
SEEN_MAX = 50
HOLIDAYS_MAX_BODY = 200          # the route allows 60 (422 holidays_invalid); this bounds the payload
PREVIEW_NOTE = "Preview only. Nothing was sent or saved."
_TEXT_BOUND = 20_000             # a note's text past this is refused before it is read


def _iso_instant(value: str | None, label: str) -> str | None:
    """An ISO 8601 instant (the expected_updated_at a card or form holds);
    anything else would be a 22007 from PostgREST."""
    if value is None:
        return None
    text = str(value).strip()
    try:
        datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{label} must be an ISO 8601 timestamp") from exc
    return text


def _uuid_list(values: list[str], label: str) -> list[str]:
    out = [str(v).strip() for v in values]
    if any(not is_uuid(v) for v in out):
        raise ValueError(f"{label} must be uuids")
    if len(set(out)) != len(out):
        raise ValueError(f"{label} must be distinct")
    return out


class CadenceIn(BaseModel):
    """The structured cadence (rrules.Cadence); the model never writes a
    rule. Also the `cadence` of ScheduleOut, rebuilt from the stored rule."""

    frequency: str = Field(max_length=20)
    hour: int | None = None
    minute: int = 0
    days: list[str] = Field(default_factory=list, max_length=7)
    day_of_month: int | None = None
    interval: int = 1
    window_start_hour: int | None = None
    window_end_hour: int | None = None


CadenceOut = CadenceIn


class ScheduleCreateIn(BaseModel):
    """POST /assistant/schedules (approval-bound) and the normalized body
    of the draft."""

    recipe: str = Field(max_length=40)
    cadence: CadenceIn
    delivery: Delivery = "chat_bell"
    only_if_changed: bool = False
    horizon_days: int | None = None
    project_id: str | None = None
    # Every-n-weeks rules only: the Monday 00:00 Pacific of the week the draft
    # was read in. The rule's week parity comes from dtstart, so the create
    # uses this instead of the approval instant and runs on the weeks the
    # card showed (a card approved across a week boundary is superseded:
    # the re-read draft carries the new week's anchor).
    anchor: datetime | None = None
    # The month (estimator_vs_bid) and the bid week (quote_readiness) of the
    # coded recipes; the recipe validates the value (period_invalid and
    # week_invalid), these only bound the length.
    period: str | None = Field(default=None, max_length=10)
    week: str | None = Field(default=None, max_length=10)

    @field_validator("project_id")
    @classmethod
    def _project_uuid(cls, v: str | None) -> str | None:
        return _uuid_or_none(v)


class ScheduleProjectOut(BaseModel):
    """A watched project (the name is redacted per role)."""

    id: str
    number: str | None = None
    name: str | None = None


class ScheduleLastRunOut(BaseModel):
    id: str
    status: str
    scheduled_for: str
    finished_at: str | None = None


class ScheduleOut(BaseModel):
    """GET /assistant/schedules/{id} and every schedule write. `next_runs`
    are three UTC instants computed server-side for active rows, so the
    frontend never expands a rule. `note` is set only by the tool end route
    when a tainted turn paused instead of ending."""

    id: str
    title: str
    recipe: str
    status: str
    paused_reason: str | None = None
    ended_reason: str | None = None
    cadence: CadenceOut | None = None
    cadence_text: str | None = None
    rrule: str
    delivery: str
    only_if_changed: bool = False
    horizon_days: int | None = None
    project: ScheduleProjectOut | None = None
    conversation_id: str | None = None
    next_runs: list[str] = Field(default_factory=list)
    last_run: ScheduleLastRunOut | None = None
    unread_deliveries: int = 0
    consecutive_failures: int = 0
    created_at: str
    ended_at: str | None = None
    note: str | None = None


class ScheduleListOut(BaseModel):
    items: list[ScheduleOut]


class ScheduleRunOut(BaseModel):
    """One occurrence of a routine. `delivered` is the claim ledger
    ({"chat": true, "bell": true, "email": "<email_log id>" | false})."""

    id: str
    schedule_id: str
    kind: str = "scheduled"
    scheduled_for: str
    status: str
    started_at: str | None = None
    finished_at: str | None = None
    delivered: dict[str, Any] | None = None
    message_id: str | None = None
    error_kind: str | None = None
    error: str | None = None
    stats: dict[str, Any] | None = None


class ScheduleRunsOut(BaseModel):
    items: list[ScheduleRunOut]
    next_before: str | None = None


class ScheduleWarningOut(BaseModel):
    code: str
    text: str


class ScheduleDraftOut(BaseModel):
    """GET /assistant/schedules/draft: never writes. Every refusal is `ok`
    false with `code` and `message`; the card is refused then, and when
    `active_count` reaches `active_cap`."""

    ok: bool
    code: str | None = None
    message: str | None = None
    body: ScheduleCreateIn | None = None
    title: str | None = None
    rrule: str | None = None
    cadence_text: str | None = None
    next_runs: list[str] = Field(default_factory=list)
    warnings: list[ScheduleWarningOut] = Field(default_factory=list)
    active_count: int = 0
    active_cap: int = ACTIVE_SCHEDULE_CAP
    project: ScheduleProjectOut | None = None


class SchedulePreviewOut(BaseModel):
    """GET /assistant/schedules/{id}/preview and /assistant/schedules/preview:
    a dry run that writes no run, no message and no bell."""

    schedule_id: str | None = None
    recipe: str
    window: dict[str, Any]
    content: str
    blocks: list[dict[str, Any]] = Field(default_factory=list)
    rows: int = 0
    would_deliver: dict[str, bool] = Field(default_factory=dict)
    suppressed: str | None = None
    note: str = PREVIEW_NOTE


class SchedulesSeenIn(BaseModel):
    ids: list[str] = Field(min_length=1, max_length=SEEN_MAX)

    @field_validator("ids")
    @classmethod
    def _ids(cls, v: list[str]) -> list[str]:
        return _uuid_list(v, "ids")


class SchedulesSeenOut(BaseModel):
    updated: int


class RecipeRowOut(BaseModel):
    """One bid of a recipe result; `project` is redacted per role."""

    project: dict[str, Any]
    checks: dict[str, Any] = Field(default_factory=dict)
    severity: str | None = None
    missing_categories: list[str] | None = None
    # Recipe-specific detail (estimator_vs_bid prices and verdict,
    # quote_readiness categories). The route removes bid amounts from it for
    # roles outside ACTUAL_BID_VIEWER_ROLES.
    extra: dict[str, Any] | None = None


class RecipeRowsOut(BaseModel):
    """GET /assistant/recipes/{recipe}/rows."""

    recipe: str
    window: dict[str, Any]
    rows: list[RecipeRowOut] = Field(default_factory=list)
    watch: dict[str, Any] | None = None
    digest_sent_today: bool = False
    # estimator_vs_bid only (3.10.2): the whole month's like-for-like and
    # all-rows totals, and the bids sent early for the next month. Bid
    # amounts are removed for roles outside ACTUAL_BID_VIEWER_ROLES.
    totals: dict[str, Any] | None = None
    sent_early: list[RecipeRowOut] | None = None


class MemoryProjectOut(BaseModel):
    id: str
    number: str | None = None
    name: str | None = None


class MemoryRevisionOut(BaseModel):
    """A proposed change to an active note, waiting for the user."""

    id: str
    title: str
    body: str = ""
    origin_tainted: bool = False
    created_at: str


class MemoryOut(BaseModel):
    """A note as the Memory tab shows it. `warning` is "untrusted_origin"
    when the user pinned a note of untrusted origin (it still loads as data,
    never as an instruction)."""

    id: str
    kind: str
    title: str
    body: str = ""
    project: MemoryProjectOut | None = None
    status: str
    source: str
    pinned: bool = False
    origin_tainted: bool = False
    last_used_at: str | None = None
    use_count: int = 0
    archived_at: str | None = None
    created_at: str
    updated_at: str
    pending_revision: MemoryRevisionOut | None = None
    warning: str | None = None


class MemoryListOut(BaseModel):
    items: list[MemoryOut]
    # The updated_at to pass as `before` for the next page; None on the last.
    next_before: str | None = None


class MemorySummaryOut(BaseModel):
    active: int = 0
    active_cap: int = 300
    pinned: int = 0
    pinned_cap: int = 40
    pinned_chars: int = 0
    pinned_chars_cap: int = 6000
    pending_review: int = 0
    pending_revisions: int = 0


class MemoryCreateIn(BaseModel):
    """POST /assistant/memories (by hand) and the body of the tool route
    POST /assistant/tools/memory/save: these four fields only, so no status,
    source or pin can ride along (docs/ASSISTANT.md:108)."""

    model_config = ConfigDict(extra="forbid")

    kind: str = Field(max_length=40)
    title: str = Field(max_length=_TEXT_BOUND)
    body: str = Field(default="", max_length=_TEXT_BOUND)
    project_id: str | None = None


MemorySaveIn = MemoryCreateIn


class MemoryPatchIn(BaseModel):
    """PATCH /assistant/memories/{id} (op edit); `expected_updated_at` is
    the updated_at the form rendered."""

    model_config = ConfigDict(extra="forbid")

    kind: str | None = Field(default=None, max_length=40)
    title: str | None = Field(default=None, max_length=_TEXT_BOUND)
    body: str | None = Field(default=None, max_length=_TEXT_BOUND)
    project_id: str | None = None
    expected_updated_at: str

    @field_validator("expected_updated_at")
    @classmethod
    def _instant(cls, v: str) -> str:
        return _iso_instant(v, "expected_updated_at") or v


class MemoryDecisionIn(BaseModel):
    """POST /assistant/memories/{id}/approve and /reject: required, so a Save
    on text the user never saw answers 409 memory_stale."""

    expected_updated_at: str

    @field_validator("expected_updated_at")
    @classmethod
    def _instant(cls, v: str) -> str:
        return _iso_instant(v, "expected_updated_at") or v


class MemoryReviseIn(BaseModel):
    """POST /assistant/tools/memory/{id}/revise."""

    model_config = ConfigDict(extra="forbid")

    title: str | None = Field(default=None, max_length=_TEXT_BOUND)
    body: str | None = Field(default=None, max_length=_TEXT_BOUND)
    expected_updated_at: str | None = None

    @field_validator("expected_updated_at")
    @classmethod
    def _instant(cls, v: str | None) -> str | None:
        return _iso_instant(v, "expected_updated_at")


class MemorySimilarOut(BaseModel):
    id: str
    title: str
    similarity: float


class MemorySaveToolOut(BaseModel):
    """The tool route's answer. `message` is fixed text: "Saved." or
    "Waiting for the user to click Save." `pinned` never appears."""

    memory_id: str
    status: str
    source: str
    origin_tainted: bool = False
    similar: list[MemorySimilarOut] = Field(default_factory=list)
    show_note: bool = True
    message: str


class MemoryReviseToolOut(BaseModel):
    memory_id: str
    revision_id: str | None = None
    status: str
    show_note: bool = True


class MemoryForgetToolOut(BaseModel):
    memory_id: str
    status: str


class MemoryToolItemOut(BaseModel):
    """One row of GET /assistant/tools/memory (active and pending notes; the
    trimmer moves the text of an origin_tainted row to untrusted fields)."""

    id: str
    kind: str
    title: str
    body: str = ""
    status: str
    pinned: bool = False
    project_id: str | None = None
    origin_tainted: bool = False


class MemoryToolListOut(BaseModel):
    items: list[MemoryToolItemOut]


class HolidaysIn(BaseModel):
    """PUT /assistant/admin/holidays. The route validates the dates (ISO,
    distinct, at most 60, within 2 years) and answers 422 holidays_invalid."""

    holidays: list[str] = Field(max_length=HOLIDAYS_MAX_BODY)


class HolidaysOut(BaseModel):
    holidays: list[str]
    updated_at: str | None = None

    @field_validator("holidays", mode="before")
    @classmethod
    def _iso_dates(cls, v: Any) -> list[str]:
        return [d.isoformat() if isinstance(d, date) else str(d) for d in (v or [])]


# ── Phase 4 (docs/ASSISTANT_PHASE4_PLAN.md sections 3.2, 3.9, 3.15, 3.16, 5) ──

# assistant_tasks.kind values (0180 B3 widens the check to these four).
TASK_KINDS = ("split_into_project", "instruction", "plans_read_pages", "ai_estimating_run")
AE_TASK_KINDS = ("plans_read_pages", "ai_estimating_run")

# Specs of assistant_reports.spec (0180 A4).
REPORT_SPECS = (
    "bid_invitations", "quote_readiness", "estimator_vs_bid", "project_timeline",
    "project_file_access", "change_history", "ai_usage", "person_evidence",
)
# The specs the model may ask build_report for; person_evidence is written
# only by the approval-bound evidence route (3.9.2).
TOOL_REPORT_SPECS = tuple(s for s in REPORT_SPECS if s != "person_evidence")


class ReportBuildIn(BaseModel):
    """POST /assistant/tools/reports (turn principal only). `range` is a
    vocabulary word of the spec (the route validates it per spec and answers
    422 range_invalid); `week` and `period` carry the recipe specs'
    parameters."""

    spec: Literal[
        "bid_invitations", "quote_readiness", "estimator_vs_bid", "project_timeline",
        "project_file_access", "change_history", "ai_usage",
    ]
    range: str | None = Field(default=None, max_length=20)
    date_from: date | None = None
    date_to: date | None = None
    week: str | None = Field(default=None, max_length=10)
    period: str | None = Field(default=None, max_length=10)
    project_id: str | None = None

    @field_validator("project_id")
    @classmethod
    def _project_uuid(cls, v: str | None) -> str | None:
        return _uuid_or_none(v)


class ReportToolOut(BaseModel):
    """The report tool's answer (5.2). Never a URL: the link is minted at the
    click by GET /assistant/reports/{id}/download."""

    report_id: str
    title: str
    filename: str
    spec: str
    row_count: int
    truncated: bool = False
    totals: dict[str, Any] = Field(default_factory=dict)
    preview_rows: list[dict[str, Any]] = Field(default_factory=list)
    expires_at_pt: str


class ReportOut(BaseModel):
    """One row of GET /assistant/reports (the person's own files)."""

    id: str
    spec: str
    title: str
    filename: str
    row_count: int = 0
    truncated: bool = False
    status: Literal["ready", "purged"]
    expires_at: str
    created_at: str


class ReportListOut(BaseModel):
    reports: list[ReportOut]


class ReportDownloadOut(BaseModel):
    """GET /assistant/reports/{id}/download: a 60 second signed URL with the
    filename as the download name."""

    url: str
    filename: str
    expires_at: str


class MemorySearchItemOut(MemoryToolItemOut):
    """One row of GET /assistant/tools/memory/search: the memory_list item
    plus its rank (the trimmer moves an origin_tainted row's text to
    untrusted fields). Results carry no [Mn] tags (3.15)."""

    rank: float | None = None


class MemorySearchOut(BaseModel):
    items: list[MemorySearchItemOut]


class FindToolItemOut(BaseModel):
    name: str
    description: str


class FindToolsOut(BaseModel):
    """GET /assistant/tools/find: the deferred tools of this person's toolset
    that match the query, best first (3.16.2)."""

    tools: list[FindToolItemOut]
    hint: str = ""
