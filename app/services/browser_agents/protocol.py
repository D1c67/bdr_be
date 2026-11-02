"""Worker <-> BDR wire contract for the browser agent platform (Phase 0, C1).

This file is the single source of truth for every request and response the
Windows worker and the BDR API exchange under /agent-worker/v1, plus the
tool schemas the model sees, the step-kind rule and the checkpoint shape.

It is vendored byte for byte into agent_worker/bdr_agent/protocol.py and
tests/test_agent_protocol_drift.py fails on any difference. So:
  - it imports nothing from app/ (the worker has only pydantic and stdlib);
  - the backend lane owns it; the worker lane asks for changes, never edits.

Spec: docs/browser_agents/SPEC.md sections 3, 4.1, 4.5, adjusted by
SPEC_OVERRIDES.md:
  O1  the model drives every job; no escalation gate; finish(captured) needs
      a valid rfi_detail_json artifact of this job.
  O2  the worker logs in itself: identities session / login/begin /
      login/result. No server-side broker login.
  O4  $25/day global and $2/job caps (server side; surfaced in TurnCaps).
  O5  no provider attestations.

Conventions:
  - Requests forbid unknown fields (a typo is a 422, not silently ignored).
  - Responses ignore unknown fields, so BDR can add a field without breaking
    a worker that has not updated yet.
  - Secrets (login password, helper OTP) are SecretStr: repr and default
    JSON dumps mask them. The server sends them with `wire_json()`.
"""

import json
import re
from datetime import datetime
from enum import StrEnum
from typing import Annotated, Any, Literal, Union
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    ValidationError,
    model_validator,
)

PROTOCOL_VERSION = "1"
API_PREFIX = "/agent-worker/v1"

# Headers -----------------------------------------------------------------------

HEADER_AUTH = "Authorization"           # "Bearer bdrw_<43 chars b64url>"
HEADER_LEASE = "X-Agent-Lease"          # "<job_id>:<epoch>" on every job-scoped call
HEADER_BOOT = "X-Agent-Boot"            # "<boot_id>" on every call after /hello
HEADER_ERROR = "X-Error-Code"           # ErrorCode value on every 4xx
HEADER_IDEMPOTENCY = "Idempotency-Key"  # every POST
HEADER_CONFIG_ETAG = "If-None-Match"    # GET /config, value = config_version

WORKER_TOKEN_PREFIX = "bdrw_"
WORKER_TOKEN_RE = re.compile(r"^bdrw_[A-Za-z0-9_-]{43}$")
ENROLL_CODE_RE = re.compile(r"^[A-Z2-7]{20}$")
SEMVER_RE = re.compile(r"^\d+\.\d+\.\d+$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
AGENT_TYPE_RE = re.compile(r"^[a-z][a-z0-9_]{2,40}$")
PROCORE_ID_RE = re.compile(r"^\d{1,20}$")

# Timing ------------------------------------------------------------------------

LEASE_SECONDS = 90
HEARTBEAT_SECONDS = 15
HEARTBEAT_ACTIVE_SECONDS_MAX = 30       # server clamps active_seconds_delta per beat
BDR_UNREACHABLE_ABORT_SECONDS = 900     # worker aborts a job after 15 min without BDR
HELP_POLL_SECONDS = 3
SESSION_CALL_TIMEOUT_SECONDS = 180
TURN_THINKING_SECONDS = 120
TURN_PENDING_RETRY_SECONDS = 5
LOGIN_RESULT_DEADLINE_SECONDS = 300     # an unreported login attempt counts as a failure after this

# Size limits -------------------------------------------------------------------

MAX_STEPS_PER_POST = 200
STEP_SUMMARY_MAX = 500
STEP_PAYLOAD_MAX_BYTES = 64 * 1024      # args + result, canonical JSON, per step
TOOL_RESULT_MAX_CHARS = 32 * 1024
URL_MAX = 2048
HELP_AGENT_MESSAGE_MAX = 1000
HELP_AGENT_REPLY_MAX = 2000
FINISH_SUMMARY_MAX = 500
ERROR_TEXT_MAX = 1000
RFI_LIST_MODEL_MAX_ROWS = 500

# Spend (O4). Defaults only; the live values come from browser_agent_settings.
DAILY_COST_CAP_USD_DEFAULT = 25.0
PER_JOB_COST_CAP_USD_DEFAULT = 2.0
SPEND_ALERT_FRACTIONS = (0.5, 1.0)

# Per-type caps defaults (browser_agent_types columns).
MAX_STEPS_DEFAULT = 40
MAX_TOTAL_TOKENS_DEFAULT = 400_000
MAX_ACTIVE_SECONDS_DEFAULT = 1200
MAX_NAVIGATIONS_PER_JOB = 40


# Error codes (X-Error-Code) ----------------------------------------------------


class ErrorCode(StrEnum):
    LEASE_LOST = "lease_lost"
    STALE_BOOT = "stale_boot"
    JOB_NOT_RUNNING = "job_not_running"
    CAPTURE_INVALID = "capture_invalid"
    ARTIFACT_INVALID = "artifact_invalid"
    IDOR = "idor"
    PENDING_APPROVAL = "pending_approval"
    REVOKED = "revoked"
    UNAUTHORIZED = "unauthorized"
    IP_NOT_ALLOWED = "ip_not_allowed"
    ENROLL_INVALID = "enroll_invalid"
    RATE_LIMITED = "rate_limited"
    INVALID_REQUEST = "invalid_request"


ERROR_HTTP_STATUS: dict[ErrorCode, int] = {
    ErrorCode.LEASE_LOST: 409,
    ErrorCode.STALE_BOOT: 409,
    ErrorCode.JOB_NOT_RUNNING: 409,
    ErrorCode.CAPTURE_INVALID: 422,
    ErrorCode.ARTIFACT_INVALID: 422,
    ErrorCode.IDOR: 403,
    ErrorCode.PENDING_APPROVAL: 401,
    ErrorCode.REVOKED: 401,
    ErrorCode.UNAUTHORIZED: 401,
    ErrorCode.IP_NOT_ALLOWED: 403,
    ErrorCode.ENROLL_INVALID: 400,
    ErrorCode.RATE_LIMITED: 429,
    ErrorCode.INVALID_REQUEST: 422,
}

# What the worker does on each 409 (spec 3.1).
WORKER_ON_ERROR: dict[ErrorCode, str] = {
    ErrorCode.STALE_BOOT: "exit_process_code_3",
    ErrorCode.LEASE_LOST: "abort_job",
    ErrorCode.JOB_NOT_RUNNING: "abort_job",
    ErrorCode.REVOKED: "stop_service",
    ErrorCode.PENDING_APPROVAL: "wait_for_approval",
}
STALE_BOOT_EXIT_CODE = 3


# Vocabularies (each mirrors a CHECK in 0157_browser_agents.sql) ------------------

JobStatus = Literal[
    "queued", "running", "waiting_help", "applying", "succeeded", "failed", "canceled", "expired",
]
JOB_TERMINAL_STATUSES = ("succeeded", "failed", "canceled", "expired")

JobOutcome = Literal[
    "pending", "applied", "apply_failed", "nothing_to_do", "already_current",
    "skipped_stale", "no_link", "self_test_ok",
]

ResumeReason = Literal[
    "retry", "interrupted", "help_answered", "manual", "login_spacing", "shutdown", "drain",
    "provider_outage",
]

RequeueReason = Literal["shutdown", "drain", "crash", "login_spacing", "provider_outage", "interrupted"]
# Requeue reasons that do not charge an attempt (crash is charged).
UNCHARGED_REQUEUE_REASONS = ("shutdown", "drain", "login_spacing", "provider_outage", "interrupted")

# Error kinds a job can end with. Worker-sent kinds are FinishErrorKind; the
# rest are written by BDR (sweeper, help flow, caps, capture validation).
FinishErrorKind = Literal[
    "not_found", "access_denied", "access_denied_suppressed", "unsupported", "cannot_complete",
    "login_failed", "secret_leak_blocked", "context_crashed", "other",
]
SERVER_ERROR_KINDS = (
    "interrupted_repeatedly", "crashed_repeatedly", "help_timeout", "help_declined",
    "skipped_by_helper", "cap_reached", "capture_invalid", "provider_config", "apply_failed",
    "expired", "canceled", "no_link",
)

StepActor = Literal["worker", "playbook", "model", "bdr", "helper", "admin", "system"]
WORKER_STEP_ACTORS = ("worker", "playbook", "model")

ArtifactKind = Literal[
    "screenshot", "rfi_detail_json", "rfi_list_json", "page_snapshot", "llm_request",
    "llm_response", "helper_upload",
]
WorkerArtifactKind = Literal["screenshot", "rfi_detail_json", "rfi_list_json", "page_snapshot"]

# Per-kind upload rules for POST /jobs/{id}/artifacts (spec 3.2). The server
# recomputes sha256 and sniffs magic bytes.
ARTIFACT_RULES: dict[str, dict[str, Any]] = {
    "screenshot": {"max_bytes": 2 * 1024 * 1024, "mime_types": ["image/png", "image/jpeg"]},
    "rfi_detail_json": {"max_bytes": 8 * 1024 * 1024, "mime_types": ["application/json"]},
    "rfi_list_json": {"max_bytes": 8 * 1024 * 1024, "mime_types": ["application/json"]},
    "page_snapshot": {"max_bytes": 2 * 1024 * 1024, "mime_types": ["text/plain", "application/json"]},
}

HelpReasonCode = Literal[
    "login_failed", "login_locked", "captcha", "mfa", "sso", "logged_out", "access_denied",
    "not_found", "ambiguous", "unexpected_page", "cap_reached", "needs_info", "provider_config",
    "other",
]
LOGIN_HELP_REASONS = ("login_failed", "login_locked", "mfa", "captcha", "sso", "logged_out")
LINK_HELP_REASONS = ("access_denied", "not_found")
# Reasons the model may pick in request_help (the rest come from tools/BDR).
MODEL_HELP_REASONS = ("access_denied", "not_found", "ambiguous", "unexpected_page", "needs_info", "other")

HelpStatus = Literal["open", "answered", "resolved", "expired", "canceled"]
HelpMessageAction = Literal["message", "retry", "give_up", "skip_daily"]
HelpAuthorKind = Literal["agent", "helper", "system"]
HelpResolution = Literal["agent_continued", "retry", "give_up", "skip_daily", "auto_resolved", "expired"]


def help_scope_key(reason_code: str, *, identity: str | None, project_id: UUID | str | None,
                   job_id: UUID | str) -> str:
    """The dedupe scope BDR derives from a reason code (spec 6.1)."""
    if reason_code in LOGIN_HELP_REASONS and identity:
        return f"login:{identity}"
    if reason_code in LINK_HELP_REASONS and project_id:
        return f"link:{project_id}"
    return f"job:{job_id}"


# Login (O2) ---------------------------------------------------------------------

PROCORE_HOSTS = ("app.procore.com", "us02.procore.com")
ProcoreHost = Literal["app.procore.com", "us02.procore.com"]
PROCORE_LOGIN_HOST = "login.procore.com"

SessionStatus = Literal["ok", "no_jar", "locked", "help_open", "not_configured"]
LoginRefusal = Literal["spacing", "locked", "help_open", "not_configured", "ip_not_allowed"]
LoginOutcome = Literal[
    "ok", "password_rejected", "mfa", "captcha", "challenge", "sso", "unexpected", "timeout",
]

# The help reason the worker must open (scope login:<identity>) after a
# non-ok login outcome. BDR returns the same value as LoginResultResponse.help_reason,
# upgraded to login_locked when the failure locked the account.
LOGIN_OUTCOME_HELP_REASON: dict[str, str | None] = {
    "ok": None,
    "password_rejected": "login_failed",
    "mfa": "mfa",
    "captcha": "captcha",
    "challenge": "captcha",
    "sso": "sso",
    "unexpected": "login_failed",
    "timeout": "login_failed",
}


# Turns ----------------------------------------------------------------------------

StopReason = Literal[
    "cap_reached", "daily_cap_reached", "provider_outage", "provider_config", "disabled",
    "canceled", "secret_leak_blocked",
]

Provider = Literal["openai", "anthropic", "meta", "google", "xai"]
PROVIDERS = ("openai", "anthropic", "meta", "google", "xai")
EXPERIMENTAL_PROVIDERS = ("meta",)


# Notification types and their metadata (C3) -------------------------------------

NOTIFICATION_TYPES: dict[str, dict[str, Any]] = {
    "browser_agent.help_needed": {"metadata": ["help_id", "job_id", "reason"], "to": ["estimating_admin"]},
    "browser_agent.help_reminder": {"metadata": ["help_id", "job_id", "reason"], "to": ["estimating_admin"]},
    "browser_agent.help_expired": {"metadata": ["help_id", "job_id", "reason"], "to": ["estimating_admin"]},
    "browser_agent.job_failed": {"metadata": ["job_id", "error_kind"], "to": ["it_admin"]},
    "browser_agent.worker_offline": {"metadata": ["worker_id"], "to": ["it_admin"]},
    "browser_agent.worker_pending": {"metadata": ["worker_id", "hostname", "ip"], "to": ["it_admin"]},
    "browser_agent.worker_suspicious": {"metadata": ["worker_id"], "to": ["it_admin"]},
    "browser_agent.login_released": {"metadata": ["job_id", "attempt_id"], "to": ["it_admin"]},
    "browser_agent.provider_problem": {"metadata": ["provider", "kind"], "to": ["it_admin"]},
    "browser_agent.spend_alert": {"metadata": ["fraction", "spend_usd", "cap_usd"], "to": ["it_admin"]},
}
# Login-scope help notices also go to IT Admin; every help notice falls back
# to IT Admin when no active Estimating Admin exists (O5).


# Step kinds -------------------------------------------------------------------------

STEP_KIND_RE = re.compile(r"^[a-z_]+(\.[a-z_]+)?$")
# Kinds a worker may append (the rest are BDR-written: claim, llm.*, help.*,
# capture.*, job.*, settings.*).
WORKER_STEP_KIND_PREFIXES = ("playbook", "tool", "nav", "http", "login", "browser", "error", "note", "security")


def worker_step_kind_allowed(kind: str) -> bool:
    return bool(STEP_KIND_RE.match(kind)) and kind.split(".", 1)[0] in WORKER_STEP_KIND_PREFIXES


# URL rules shared by both policy checks ------------------------------------------

# Query keys that make a URL refusable (signed links, tokens, sessions).
TOKEN_QUERY_KEY_RE = re.compile(r"(?i)token|sig|signature|key|auth|code|session|passport|x-amz-")
# Input names/ids/labels type_text refuses.
SECRET_INPUT_RE = re.compile(r"(?i)otp|passcode|mfa|verification|2fa|code|password")

# Code ceilings per agent type (spec 4.3). Settings host_narrowing can only
# remove hosts from `nav_hosts`. "$api_host" is the BDR API host the worker
# was enrolled against.
CEILINGS: dict[str, dict[str, list[str]]] = {
    "rfi_from_email": {
        "nav_hosts": list(PROCORE_HOSTS),
        "nav_paths": [
            r"^/\d{1,20}/project/(home|rfi(/list)?|rfi/show/\d{1,20})/?$",
            r"^/webclients/host/companies/\d{1,20}/projects/\d{1,20}/tools/rfis(/\d{1,20})?/?$",
        ],
        "resource_hosts": ["*.procore.com", "*.procoretech.com", "*.procorecdn.com"],
        "login_hosts": [PROCORE_LOGIN_HOST],
        "login_challenge_hosts": ["challenges.cloudflare.com", "www.google.com", "www.gstatic.com"],
    },
    "user_browser_read": {
        "nav_hosts": ["app.procore.com", "us02.procore.com"],
        "nav_paths": [
            r"^/\d{1,20}/project/(home|rfi(/list)?|rfi/show/\d{1,20})/?$",
            r"^/webclients/host/companies/\d{1,20}/projects/\d{1,20}/tools/(projecthome|hubs/project-hub/views/overview|rfis(/\d{1,20})?)/?$",
        ],
        "resource_hosts": ["app.procore.com", "login.procore.com", "us02.procore.com"],
        "login_hosts": ["login.procore.com"],
        "login_challenge_hosts": [],
    },
    "canary": {
        "nav_hosts": ["$api_host"],
        "nav_paths": [r"^/agent-worker/v1/canary/page/?$"],
        "resource_hosts": [],
        "login_hosts": [],
        "login_challenge_hosts": [],
    },
}


# Base models --------------------------------------------------------------------


class _Req(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=False)


class _Resp(BaseModel):
    model_config = ConfigDict(extra="ignore")

    def wire_json(self) -> str:
        """JSON with SecretStr values revealed. Server side only, for the two
        responses that carry a secret (LoginBeginResponse, HelpSecretUseResponse)."""
        data = self.model_dump(mode="json")
        for name in type(self).model_fields:
            value = getattr(self, name)
            if isinstance(value, SecretStr):
                data[name] = value.get_secret_value()
        return json.dumps(data, separators=(",", ":"))


Semver = Annotated[str, Field(pattern=SEMVER_RE.pattern)]
Sha256 = Annotated[str, Field(pattern=SHA256_RE.pattern)]
AgentTypeKey = Annotated[str, Field(pattern=AGENT_TYPE_RE.pattern)]
ProcoreId = Annotated[str, Field(pattern=PROCORE_ID_RE.pattern)]
Url = Annotated[str, Field(max_length=URL_MAX)]


# 1. POST /enroll (no auth) --------------------------------------------------------


class EnrollRequest(_Req):
    expected_pool: Literal["org", "user"] = "org"
    code: Annotated[str, Field(pattern=ENROLL_CODE_RE.pattern)]
    hostname: Annotated[str, Field(min_length=1, max_length=255)]
    os_info: dict[str, Any] = Field(default_factory=dict)
    version: Semver


class EnrollResponse(_Resp):
    principal_kind: Literal["org", "user"] = "org"
    worker_id: UUID
    token: Annotated[str, Field(pattern=WORKER_TOKEN_RE.pattern)]
    status: Literal["pending"]


# 2. POST /hello -----------------------------------------------------------------


class WorkerCapabilities(_Req):
    types: dict[str, Semver]             # {"rfi_from_email": "1.0.0", "canary": "1.0.0"}
    slots: Annotated[int, Field(ge=1, le=8)]


class HelloRequest(_Req):
    boot_id: UUID
    version: Semver
    chromium_version: str | None = None
    bad_versions: list[Semver] = Field(default_factory=list)
    capabilities: WorkerCapabilities


class HelloResponse(_Resp):
    status: Literal["active", "pending", "suspended"]
    config_version: int
    desired_version: Semver | None = None
    min_version: Semver | None = None
    server_time: datetime
    requeued: int = 0                    # leases of older boots requeued as interrupted


# 3. GET /config ---------------------------------------------------------------


class WorkerConfig(_Resp):
    principal_kind: Literal["org", "user"] = "org"
    status: Literal["pending", "active", "revoked"]
    max_slots: int
    draining: bool
    allowed_types: list[str]


class TypeConfig(_Resp):
    enabled: bool
    identity: str | None = None
    max_concurrency: int
    max_steps: int
    max_active_seconds: int
    host_narrowing: list[str] | None = None   # null = the full code ceiling


class ReleaseManifest(_Resp):
    version: Semver
    sha256: Sha256
    min_version: Semver
    created_at: datetime


class ReleaseInfo(_Resp):
    version: Semver
    sha256: Sha256
    min_version: Semver
    manifest: ReleaseManifest
    signature: str                       # base64 Ed25519 over canonical_manifest_bytes(manifest)


def canonical_manifest_bytes(manifest: ReleaseManifest | dict[str, Any]) -> bytes:
    """The exact bytes the release key signs: sorted keys, no spaces, UTF-8."""
    data = manifest.model_dump(mode="json") if isinstance(manifest, BaseModel) else dict(manifest)
    data = {k: data[k] for k in ("version", "sha256", "min_version", "created_at")}
    return json.dumps(data, sort_keys=True, separators=(",", ":")).encode("utf-8")


class ConfigResponse(_Resp):
    """No keys, passwords, prompts, providers or models ever appear here."""
    config_version: int
    enabled: bool                        # global kill switch
    worker: WorkerConfig
    types: dict[str, TypeConfig]
    help_hold_seconds: int
    release: ReleaseInfo | None = None


class PendingResponse(_Resp):
    """What /hello's sibling /config returns to a pending (not yet approved) worker."""
    pending: Literal[True]


# 4. POST /heartbeat -------------------------------------------------------------


class HeartbeatJob(_Req):
    job_id: UUID
    epoch: int
    phase: Annotated[str, Field(max_length=40)] | None = None
    step_count: Annotated[int, Field(ge=0)] = 0
    active_seconds_delta: Annotated[int, Field(ge=0, le=3600)] = 0


class HeartbeatRequest(_Req):
    boot_id: UUID
    config_version: int
    slots_free: dict[str, Annotated[int, Field(ge=0, le=8)]] = Field(default_factory=dict)
    loop_lag_ms: Annotated[int, Field(ge=0)] = 0
    jobs: list[HeartbeatJob] = Field(default_factory=list)


class RenewedLease(_Resp):
    job_id: UUID
    lease_expires_at: datetime


class OrphanLease(_Resp):
    job_id: UUID
    epoch: int


class HelpUpdate(_Resp):
    job_id: UUID
    help_id: UUID
    latest_message_id: int | None = None
    status: HelpStatus


class HeartbeatResponse(_Resp):
    config_version: int
    renewed: list[RenewedLease] = Field(default_factory=list)
    lost: list[UUID] = Field(default_factory=list)
    cancel: list[UUID] = Field(default_factory=list)
    orphans: list[OrphanLease] = Field(default_factory=list)   # worker requeues them 'interrupted'
    help_updates: list[HelpUpdate] = Field(default_factory=list)
    drain: bool = False
    controls: list[dict[str, Any]] = Field(default_factory=list)   # live-view seam; always [] in v1


# Checkpoint and seeds ----------------------------------------------------------

CheckpointPhase = Literal["seeded", "session_ready", "located", "detail_captured", "submitted"]


class RfiCheckpoint(BaseModel):
    """Checkpoint shape for rfi_from_email (spec 4.5). Stored as jobs.checkpoint."""
    model_config = ConfigDict(extra="forbid")

    phase: CheckpointPhase = "seeded"
    company_id: ProcoreId | None = None
    rfi_id: ProcoreId | None = None
    detail_artifact_id: UUID | None = None
    captured_at: datetime | None = None
    last_url: Url | None = None
    contributions_sent: list[str] = Field(default_factory=list)


class RfiFromEmailInput(BaseModel):
    """jobs.input for rfi_from_email (spec 5.1). The email body is never here."""
    model_config = ConfigDict(extra="forbid")

    platform: Literal["procore"]
    host: ProcoreHost
    external_project_id: ProcoreId
    rfi_id: ProcoreId | None = None           # null = locate mode (Pass C)
    company_id: ProcoreId | None = None
    rfi_number_hint: Annotated[str, Field(max_length=40)] | None = None
    event_hint: Literal["closed", "response", "other"] = "other"
    email_message_at: datetime
    source_email_ids: list[UUID] = Field(default_factory=list)

    @model_validator(mode="after")
    def _locate_needs_hint(self) -> "RfiFromEmailInput":
        if self.rfi_id is None and not self.rfi_number_hint:
            raise ValueError("rfi_id or rfi_number_hint is required")
        return self


class CanaryInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expect_text: Annotated[str, Field(max_length=200)] = "BDR canary ok"


class UserBrowserReadInput(_Req):
    """A frozen human-approved read request; credentials are never input."""
    service: Literal["procore"]
    url: Annotated[str, Field(min_length=1, max_length=URL_MAX)]
    objective: Annotated[str, Field(min_length=1, max_length=2000)]


INPUT_MODELS: dict[str, type[BaseModel]] = {
    "rfi_from_email": RfiFromEmailInput, "canary": CanaryInput,
    "user_browser_read": UserBrowserReadInput,
}
CHECKPOINT_MODELS: dict[str, type[BaseModel]] = {"rfi_from_email": RfiCheckpoint}


def parse_input(agent_type: str, data: dict[str, Any]) -> BaseModel:
    return INPUT_MODELS[agent_type].model_validate(data)


# Help messages as the worker sees them ------------------------------------------


class HelpFile(_Resp):
    artifact_id: UUID
    filename: str
    size_bytes: int
    mime_type: str
    attachable: bool


class SecretRef(_Resp):
    message_id: int
    consumed: bool


class WorkerHelpMessage(_Resp):
    """Never carries a secret value: secret messages come with secret_ref only."""
    id: int
    author_kind: HelpAuthorKind
    author_label: str | None = None
    action: HelpMessageAction = "message"
    body: str | None = None
    secret_ref: SecretRef | None = None
    files: list[HelpFile] = Field(default_factory=list)
    created_at: datetime


class UnconsumedHelp(_Resp):
    help_id: UUID
    status: HelpStatus
    messages: list[WorkerHelpMessage]


# 5. POST /claim -------------------------------------------------------------------


class ClaimRequest(_Req):
    boot_id: UUID
    capacity: dict[str, Annotated[int, Field(ge=0, le=8)]]


class ClaimedJob(_Resp):
    job_id: UUID
    agent_type: str
    principal_kind: Literal["org", "user"] = "org"
    principal_user_id: UUID | None = None
    epoch: int
    lease_expires_at: datetime
    input: dict[str, Any]                # validate with parse_input(agent_type, input)
    checkpoint: dict[str, Any] = Field(default_factory=dict)
    checkpoint_seq: int = 0
    attempts: int
    last_resume_reason: ResumeReason | None = None
    turn_count: int = 0
    unconsumed_help: list[UnconsumedHelp] = Field(default_factory=list)


class ClaimResponse(_Resp):
    jobs: list[ClaimedJob] = Field(default_factory=list)


# 6. POST /jobs/{id}/steps ---------------------------------------------------------


class StepIn(_Req):
    idem_key: Annotated[str, Field(min_length=1, max_length=120)]
    kind: Annotated[str, Field(pattern=STEP_KIND_RE.pattern, max_length=60)]
    actor: Literal["worker", "playbook", "model"]
    summary: Annotated[str, Field(max_length=STEP_SUMMARY_MAX)]
    tool_name: Annotated[str, Field(max_length=60)] | None = None
    args: dict[str, Any] | None = None   # redacted by the worker; BDR redacts again
    result: dict[str, Any] | None = None
    url_before: Url | None = None        # query strings stripped
    url_after: Url | None = None
    screenshot_artifact_id: UUID | None = None
    turn_no: int | None = None
    ok: bool | None = None
    duration_ms: Annotated[int, Field(ge=0)] | None = None
    at: datetime | None = None           # worker clock; BDR keeps its own `at` too

    @model_validator(mode="after")
    def _worker_kind(self) -> "StepIn":
        if not worker_step_kind_allowed(self.kind):
            raise ValueError(f"step kind not allowed from a worker: {self.kind}")
        return self


class AppendStepsRequest(_Req):
    steps: Annotated[list[StepIn], Field(min_length=1, max_length=MAX_STEPS_PER_POST)]


class AppendStepsResponse(_Resp):
    appended: int
    duplicates: int
    late: int


# 7. PUT /jobs/{id}/checkpoint (204) ----------------------------------------------


class CheckpointRequest(_Req):
    seq: Annotated[int, Field(ge=1)]
    data: dict[str, Any]


# 8. POST /jobs/{id}/artifacts (multipart) ----------------------------------------


class ArtifactUploadForm(_Req):
    """Multipart form fields beside the `file` part."""
    kind: WorkerArtifactKind
    sha256: Sha256
    filename: Annotated[str, Field(min_length=1, max_length=200)]


class ArtifactUploadResponse(_Resp):
    artifact_id: UUID
    existed: bool


class RfiDetailEnvelope(BaseModel):
    """The JSON stored as an rfi_detail_json artifact (capture_rfi_detail)."""
    model_config = ConfigDict(extra="forbid")
    request_path: Annotated[str, Field(pattern=r"^/rest/v1\.0/projects/\d{1,20}/rfis/\d{1,20}$")]
    status: int
    captured_at: datetime
    company_id: ProcoreId | None = None
    body: dict[str, Any]


class RfiListEnvelope(BaseModel):
    """The JSON stored as an rfi_list_json artifact (procore_list_rfis)."""
    model_config = ConfigDict(extra="forbid")
    request_path: Annotated[str, Field(pattern=r"^/rest/v1\.0/projects/\d{1,20}/rfis$")]
    status: int
    captured_at: datetime
    company_id: ProcoreId | None = None
    pages: int = 1
    body: list[dict[str, Any]]


# 9. POST /jobs/{id}/turns ---------------------------------------------------------


class ToolResult(_Req):
    call_id: Annotated[str, Field(min_length=1, max_length=120)]
    ok: bool
    content: Annotated[str, Field(max_length=TOOL_RESULT_MAX_CHARS)]


class TurnRequest(_Req):
    prev_turn_no: Annotated[int, Field(ge=0)]   # 0 on the first turn of a job
    tool_results: list[ToolResult] = Field(default_factory=list, max_length=1)
    screenshot_artifact_id: UUID | None = None
    url: Url | None = None
    title: Annotated[str, Field(max_length=300)] | None = None
    helper_message_ids_seen: list[int] = Field(default_factory=list)


class ToolCall(_Resp):
    call_id: str
    tool: str
    args: dict[str, Any]


class TurnCaps(_Resp):
    steps_left: int
    tokens_left: int
    cost_left_usd: float                 # min(per-job cap left, global daily cap left)


class TurnActionResponse(_Resp):
    """200: the one validated tool call for this turn."""
    turn_no: int
    assistant_text: str | None = None
    action: ToolCall
    caps: TurnCaps


class TurnPendingResponse(_Resp):
    """202: another BDR process is thinking on this turn; call again after retry_after s."""
    retry_after: int = TURN_PENDING_RETRY_SECONDS


class TurnStopResponse(_Resp):
    """200 with stop=true. cap_reached / daily_cap_reached: open cap_reached help.
    provider_outage: /requeue provider_outage with retry_at. provider_config:
    open provider_config help. disabled / canceled: finish canceled."""
    stop: Literal[True]
    reason: StopReason
    retry_at: datetime | None = None
    detail: str | None = None


def parse_turn_response(status_code: int, data: dict[str, Any]) -> Union[
        TurnActionResponse, TurnPendingResponse, TurnStopResponse]:
    if status_code == 202:
        return TurnPendingResponse.model_validate(data)
    if data.get("stop") is True:
        return TurnStopResponse.model_validate(data)
    return TurnActionResponse.model_validate(data)


# 10. Identities (O2): POST /jobs/{id}/identities/{identity}/session ----------------


class Cookie(BaseModel):
    """Playwright add_cookies shape. Values are secrets: never logged."""
    model_config = ConfigDict(extra="forbid")
    name: Annotated[str, Field(min_length=1, max_length=200)]
    value: Annotated[str, Field(max_length=8192)]
    domain: Annotated[str, Field(min_length=1, max_length=255)]
    path: str = "/"
    expires: float = -1                  # unix seconds; -1 = session cookie
    secure: bool = True
    httpOnly: bool = False
    sameSite: Literal["Strict", "Lax", "None"] = "Lax"

    def __repr__(self) -> str:           # never print a cookie value
        return f"Cookie(name={self.name!r}, domain={self.domain!r})"

    __str__ = __repr__


class SessionRequest(_Req):
    host: ProcoreHost
    # The logged_in_at of a jar the worker already found logged out; BDR then
    # does not hand the same jar back.
    reject_jar_logged_in_at: datetime | None = None


class SessionResponse(_Resp):
    """ok: inject cookies and probe GET /rest/v1.0/companies. Anything else (or
    a failed probe): call login/begin, unless help_open (join and park) or
    not_configured (finish failed login_failed)."""
    status: SessionStatus
    cookies: list[Cookie] | None = None
    logged_in_at: datetime | None = None
    source: Literal["stored"] | None = None
    retry_at: datetime | None = None
    help_id: UUID | None = None

    @model_validator(mode="after")
    def _ok_has_cookies(self) -> "SessionResponse":
        if self.status == "ok" and not self.cookies:
            raise ValueError("status ok requires cookies")
        return self


class LoginBeginRequest(_Req):
    host: ProcoreHost
    rejected_jar_logged_in_at: datetime | None = None


class LoginBeginResponse(_Resp):
    """granted: fill login.procore.com with email + password, then report
    login/result within LOGIN_RESULT_DEADLINE_SECONDS. Sent with
    Cache-Control: no-store; the password is never logged, persisted or
    shown to the model. Refused: spacing -> wait or /requeue login_spacing;
    help_open -> join help_id and park; locked -> open login_locked help."""
    granted: bool
    attempt_id: UUID | None = None
    email: str | None = None
    password: SecretStr | None = None
    reason: LoginRefusal | None = None
    retry_at: datetime | None = None
    help_id: UUID | None = None

    @model_validator(mode="after")
    def _shape(self) -> "LoginBeginResponse":
        if self.granted and not (self.attempt_id and self.email and self.password):
            raise ValueError("granted requires attempt_id, email and password")
        if not self.granted and (self.password is not None or self.reason is None):
            raise ValueError("refused carries a reason and no password")
        return self


class LoginResultRequest(_Req):
    attempt_id: UUID
    outcome: LoginOutcome
    step: Annotated[str, Field(max_length=60)] | None = None   # classifier step name, no URLs
    cookies: list[Cookie] | None = None

    @model_validator(mode="after")
    def _ok_has_cookies(self) -> "LoginResultRequest":
        if self.outcome == "ok" and not self.cookies:
            raise ValueError("outcome ok requires cookies")
        if self.outcome != "ok" and self.cookies:
            raise ValueError("cookies are sent only with outcome ok")
        return self


class LoginResultResponse(_Resp):
    locked_until: datetime | None = None
    login_failures: int = 0
    # When set, the worker opens help with this reason (scope login:<identity>).
    help_reason: HelpReasonCode | None = None


# 11. Help ----------------------------------------------------------------------------


class HelpOpenRequest(_Req):
    reason_code: HelpReasonCode
    message: Annotated[str, Field(max_length=HELP_AGENT_MESSAGE_MAX)] | None = None
    screenshot_artifact_id: UUID | None = None


class HelpOpenResponse(_Resp):
    """suppressed=true: a link scope was given up/skipped in the last 24 h; the
    worker finishes failed access_denied_suppressed."""
    help_id: UUID | None = None
    joined_existing: bool = False
    holder: bool = False
    suppressed: bool = False
    hold_until: datetime | None = None


class HelpMessagesResponse(_Resp):
    status: HelpStatus
    claimed_by_label: str | None = None
    messages: list[WorkerHelpMessage] = Field(default_factory=list)


class HelpConsumeRequest(_Req):
    message_ids: Annotated[list[int], Field(min_length=1, max_length=100)]


class HelpReplyRequest(_Req):
    body: Annotated[str, Field(min_length=1, max_length=HELP_AGENT_REPLY_MAX)]


class HelpReplyResponse(_Resp):
    id: int


class HelpSecretUseResponse(_Resp):
    """One-time: holder job only, login phase only. Consumed by the login routine."""
    value: SecretStr


# 12. POST /jobs/{id}/contributions ------------------------------------------------


class ContributionFileItem(_Req):
    kind: Literal["file_question", "file_answer"]
    artifact_id: UUID


class ContributionNoteItem(_Req):
    kind: Literal["note"]
    message_ids: Annotated[list[int], Field(min_length=1, max_length=5)]


ContributionItem = Annotated[Union[ContributionFileItem, ContributionNoteItem], Field(discriminator="kind")]


class ContributionsRequest(_Req):
    help_id: UUID
    external_rfi_id: ProcoreId
    items: Annotated[list[ContributionItem], Field(min_length=1, max_length=20)]


class ContributionAccepted(_Resp):
    index: int
    contribution_id: UUID


class ContributionRejected(_Resp):
    index: int
    reason: Literal["not_attachable", "not_this_help", "not_helper_message", "duplicate", "invalid"]


class ContributionsResponse(_Resp):
    accepted: list[ContributionAccepted] = Field(default_factory=list)
    rejected: list[ContributionRejected] = Field(default_factory=list)


# 13. POST /jobs/{id}/rfi-capture ---------------------------------------------------


class UserSecretRef(SecretRef):
    message_id: UUID


class UserWorkerHelpMessage(WorkerHelpMessage):
    id: UUID
    secret_ref: UserSecretRef | None = None


class UserHelpMessagesResponse(HelpMessagesResponse):
    messages: list[UserWorkerHelpMessage] = Field(default_factory=list)


class UserHelpOpenRequest(HelpOpenRequest):
    login_attempt_id: UUID | None = None


class UserHelpConsumeRequest(HelpConsumeRequest):
    message_ids: Annotated[list[UUID], Field(min_length=1, max_length=100)]


class UserHelpReplyResponse(HelpReplyResponse):
    id: UUID


class UserHelpSecretUseRequest(_Req):
    login_attempt_id: UUID


class UserTurnRequest(TurnRequest):
    helper_message_ids_seen: list[UUID] = Field(default_factory=list)


class UserSessionRequest(SessionRequest):
    origin: Annotated[str, Field(min_length=1, max_length=200)]


class UserLoginBeginRequest(LoginBeginRequest):
    origin: Annotated[str, Field(min_length=1, max_length=200)]


class UserLoginResultRequest(LoginResultRequest):
    origin: Annotated[str, Field(min_length=1, max_length=200)]


class RfiCaptureRequest(_Req):
    platform: Literal["procore"]
    detail_artifact_id: UUID
    external_rfi_id: ProcoreId
    company_id: ProcoreId


class RfiCaptureResponse(_Resp):
    run_id: UUID
    accepted: Literal[True]


# 14. POST /jobs/{id}/finish (204) ------------------------------------------------


class FinishRequest(_Req):
    """rfi_from_email: succeeded only with nothing_to_do; a capture goes
    through /rfi-capture instead (the job becomes applying). canary:
    succeeded with self_test_ok."""
    status: Literal["succeeded", "failed", "canceled"]
    outcome: Literal["nothing_to_do", "self_test_ok"] | None = None
    error_kind: FinishErrorKind | None = None
    error: Annotated[str, Field(max_length=ERROR_TEXT_MAX)] | None = None
    summary: Annotated[str, Field(max_length=FINISH_SUMMARY_MAX)]

    @model_validator(mode="after")
    def _shape(self) -> "FinishRequest":
        if self.status == "succeeded" and self.outcome is None:
            raise ValueError("succeeded requires an outcome")
        if self.status == "failed" and self.error_kind is None:
            raise ValueError("failed requires an error_kind")
        if self.status != "succeeded" and self.outcome is not None:
            raise ValueError("outcome is only for succeeded")
        return self


# 15. POST /jobs/{id}/requeue (204), POST /jobs/{id}/park (204) --------------------


class RequeueRequest(_Req):
    reason: RequeueReason
    retry_at: datetime | None = None
    service_stop: bool = False           # shutdown/drain are accepted only with this or worker.draining


# 16. GET /releases/{version} --------------------------------------------------------


class ReleaseResponse(_Resp):
    manifest: ReleaseManifest
    signature: str
    url: str                             # signed storage URL, 900 s; its token query is redacted in logs


# Endpoint table (method, path template, request model, response model, status) --

ENDPOINTS: dict[str, tuple[str, str, type[BaseModel] | None, Any, int]] = {
    "enroll": ("POST", "/enroll", EnrollRequest, EnrollResponse, 200),
    "hello": ("POST", "/hello", HelloRequest, HelloResponse, 200),
    "config": ("GET", "/config", None, ConfigResponse, 200),
    "heartbeat": ("POST", "/heartbeat", HeartbeatRequest, HeartbeatResponse, 200),
    "claim": ("POST", "/claim", ClaimRequest, ClaimResponse, 200),
    "steps": ("POST", "/jobs/{job_id}/steps", AppendStepsRequest, AppendStepsResponse, 200),
    "checkpoint": ("PUT", "/jobs/{job_id}/checkpoint", CheckpointRequest, None, 204),
    "artifacts": ("POST", "/jobs/{job_id}/artifacts", ArtifactUploadForm, ArtifactUploadResponse, 200),
    "turns": ("POST", "/jobs/{job_id}/turns", TurnRequest,
              (TurnActionResponse, TurnPendingResponse, TurnStopResponse), 200),
    "identity_session": ("POST", "/jobs/{job_id}/identities/{identity}/session",
                         SessionRequest, SessionResponse, 200),
    "login_begin": ("POST", "/jobs/{job_id}/identities/{identity}/login/begin",
                    LoginBeginRequest, LoginBeginResponse, 200),
    "login_result": ("POST", "/jobs/{job_id}/identities/{identity}/login/result",
                     LoginResultRequest, LoginResultResponse, 200),
    "help_open": ("POST", "/jobs/{job_id}/help", HelpOpenRequest, HelpOpenResponse, 200),
    "help_messages": ("GET", "/jobs/{job_id}/help/{help_id}/messages", None, HelpMessagesResponse, 200),
    "help_consume": ("POST", "/jobs/{job_id}/help/{help_id}/consume", HelpConsumeRequest, None, 204),
    "help_reply": ("POST", "/jobs/{job_id}/help/{help_id}/messages", HelpReplyRequest, HelpReplyResponse, 200),
    "help_secret_use": ("POST", "/jobs/{job_id}/help/{help_id}/secrets/{message_id}/use",
                        None, HelpSecretUseResponse, 200),
    "park": ("POST", "/jobs/{job_id}/park", None, None, 204),
    "contributions": ("POST", "/jobs/{job_id}/contributions", ContributionsRequest, ContributionsResponse, 200),
    "rfi_capture": ("POST", "/jobs/{job_id}/rfi-capture", RfiCaptureRequest, RfiCaptureResponse, 200),
    "finish": ("POST", "/jobs/{job_id}/finish", FinishRequest, None, 204),
    "requeue": ("POST", "/jobs/{job_id}/requeue", RequeueRequest, None, 204),
    "release": ("GET", "/releases/{version}", None, ReleaseResponse, 200),
    "canary_page": ("GET", "/canary/page", None, None, 200),   # static HTML
}
# Reserved: /jobs/{job_id}/live returns 404 in v1 (live-view seam).
# Personal jobs use a distinct encrypted backend path. No fallback to org endpoints.
for _user_endpoint in (
    "steps", "checkpoint", "artifacts", "turns", "help_open", "help_messages", "help_consume",
    "help_reply", "help_secret_use", "park", "finish", "requeue",
):
    _method, _path, _request, _response, _status = ENDPOINTS[_user_endpoint]
    ENDPOINTS["user_" + _user_endpoint] = (
        _method, _path.replace("/jobs/{job_id}/", "/jobs/{job_id}/user/", 1), _request, _response, _status,
    )
for _user_endpoint, _request in (
    ("identity_session", UserSessionRequest), ("login_begin", UserLoginBeginRequest),
    ("login_result", UserLoginResultRequest),
):
    _method, _path, _old_request, _response, _status = ENDPOINTS[_user_endpoint]
    ENDPOINTS["user_" + _user_endpoint] = (
        _method, _path.replace("/identities/", "/user-identities/", 1), _request, _response, _status,
    )

ENDPOINTS["user_heartbeat"] = ("POST", "/user/heartbeat", HeartbeatRequest, HeartbeatResponse, 200)
for _name, _request, _response in (
    ("turns", UserTurnRequest, (TurnActionResponse, TurnPendingResponse, TurnStopResponse)),
    ("help_open", UserHelpOpenRequest, HelpOpenResponse),
    ("help_messages", None, UserHelpMessagesResponse),
    ("help_consume", UserHelpConsumeRequest, None),
    ("help_reply", HelpReplyRequest, UserHelpReplyResponse),
    ("help_secret_use", UserHelpSecretUseRequest, HelpSecretUseResponse),
):
    _method, _path, _, _old_response, _status = ENDPOINTS["user_" + _name]
    ENDPOINTS["user_" + _name] = (_method, _path, _request, _response, _status)


JOB_SCOPED_ENDPOINTS = tuple(k for k, v in ENDPOINTS.items() if v[1].startswith("/jobs/"))
NO_AUTH_ENDPOINTS = ("enroll",)
PENDING_ALLOWED_ENDPOINTS = ("hello", "config")


# Tools (spec 4.1 adjusted by O1) ----------------------------------------------------
#
# Neutral JSON Schema, identical for every provider; adapters translate the
# envelope only. Each tool also has a pydantic args model (TOOL_ARG_MODELS)
# that both policy checks (BDR /turns and the worker executor) validate with.


def _obj(props: dict[str, Any], required: list[str]) -> dict[str, Any]:
    return {"type": "object", "properties": props, "required": required, "additionalProperties": False}


_HOST = {"type": "string", "enum": list(PROCORE_HOSTS), "description": "Procore web host from the job seed."}
_PROJECT = {"type": "string", "pattern": PROCORE_ID_RE.pattern, "description": "Procore project id (digits)."}
_COMPANY = {"type": "string", "pattern": PROCORE_ID_RE.pattern, "description": "Procore company id (digits)."}
_REF = {"type": "string", "pattern": r"^ref_\d{1,6}$", "description": "An element ref from read_page."}

TOOL_SCHEMAS: dict[str, dict[str, Any]] = {
    # Site tools (adapter procore). Prefer these: cheap and exact.
    "ensure_login": {
        "name": "ensure_login",
        "description": (
            "Make sure this job's browser is logged in to Procore. Uses the shared stored session "
            "when it still works, otherwise logs in. You never see credentials. Returns "
            "{ok, outcome} where outcome is one of ok, spacing, locked, help_open, password_rejected, "
            "mfa, captcha, challenge, sso, unexpected, timeout, not_configured."
        ),
        "parameters": _obj({"host": _HOST}, ["host"]),
    },
    "procore_resolve_company": {
        "name": "procore_resolve_company",
        "description": "Find the Procore company id that owns a project. Returns {company_id} or an error.",
        "parameters": _obj({"host": _HOST, "project_id": _PROJECT}, ["host", "project_id"]),
    },
    "procore_list_rfis": {
        "name": "procore_list_rfis",
        "description": (
            f"List the RFIs on a Procore project (read only). Returns at most {RFI_LIST_MODEL_MAX_ROWS} "
            "rows of {id, number, revision, subject, status, updated_at}. Use it to find an RFI id "
            "from a number hint."
        ),
        "parameters": _obj({"host": _HOST, "project_id": _PROJECT, "company_id": _COMPANY},
                           ["host", "project_id", "company_id"]),
    },
    "capture_rfi_detail": {
        "name": "capture_rfi_detail",
        "description": (
            "Capture the full detail of one RFI as the job's data record. This is the ONLY way RFI "
            "data reaches BDR. Returns {number, subject, status, revision, attachment_count} or an "
            "error (unauthorized, forbidden, not_found, challenge, not_json)."
        ),
        "parameters": _obj({"host": _HOST, "project_id": _PROJECT,
                            "rfi_id": {"type": "string", "pattern": PROCORE_ID_RE.pattern,
                                       "description": "Procore RFI id (digits)."},
                            "company_id": _COMPANY},
                           ["host", "project_id", "rfi_id", "company_id"]),
    },
    # Browser tools. Use only when the site tools fail.
    "navigate": {
        "name": "navigate",
        "description": "Open an https URL inside the allowed Procore pages. Other URLs are refused.",
        "parameters": _obj({"url": {"type": "string", "pattern": "^https://", "maxLength": URL_MAX}}, ["url"]),
    },
    "read_page": {
        "name": "read_page",
        "description": "Read the current page as an accessibility snapshot with element refs (ref_N).",
        "parameters": _obj({"max_chars": {"type": "integer", "minimum": 500, "maximum": 20000}}, []),
    },
    "find_text": {
        "name": "find_text",
        "description": "Find visible text on the current page. Returns matching snippets with refs.",
        "parameters": _obj({"query": {"type": "string", "minLength": 1, "maxLength": 200}}, ["query"]),
    },
    "click": {
        "name": "click",
        "description": "Click an element by ref. Reports stale_ref when the page changed.",
        "parameters": _obj({"ref": _REF}, ["ref"]),
    },
    "type_text": {
        "name": "type_text",
        "description": "Type into a text field by ref. Refused on password, code and login fields.",
        "parameters": _obj({"ref": _REF, "text": {"type": "string", "maxLength": 500},
                            "submit": {"type": "boolean"}}, ["ref", "text"]),
    },
    "select_option": {
        "name": "select_option",
        "description": "Choose an option in a select element by ref.",
        "parameters": _obj({"ref": _REF, "value": {"type": "string", "maxLength": 200}}, ["ref", "value"]),
    },
    "press_key": {
        "name": "press_key",
        "description": "Press one key.",
        "parameters": _obj({"key": {"type": "string", "enum": [
            "Enter", "Tab", "Escape", "ArrowUp", "ArrowDown", "PageUp", "PageDown"]}}, ["key"]),
    },
    "scroll": {
        "name": "scroll",
        "description": "Scroll the page.",
        "parameters": _obj({"direction": {"type": "string", "enum": ["up", "down"]},
                            "amount": {"type": "integer", "minimum": 1, "maximum": 10}},
                           ["direction", "amount"]),
    },
    "go_back": {
        "name": "go_back",
        "description": "Go back one page.",
        "parameters": _obj({}, []),
    },
    "wait_for": {
        "name": "wait_for",
        "description": "Wait for text to appear, or for a number of seconds.",
        "parameters": _obj({"text": {"type": "string", "maxLength": 200},
                            "seconds": {"type": "integer", "minimum": 1, "maximum": 10}}, []),
    },
    # Help tools.
    "request_help": {
        "name": "request_help",
        "description": (
            "Ask a G3 Estimating Admin for help. A screenshot is attached automatically. Waits for "
            "an answer; returns {messages, files} or {parked: true}. Never ask for passwords."
        ),
        "parameters": _obj({"reason_code": {"type": "string", "enum": list(MODEL_HELP_REASONS)},
                            "message": {"type": "string", "minLength": 1,
                                        "maxLength": HELP_AGENT_MESSAGE_MAX}},
                           ["reason_code", "message"]),
    },
    "reply_help": {
        "name": "reply_help",
        "description": "Reply in the open help conversation.",
        "parameters": _obj({"text": {"type": "string", "minLength": 1, "maxLength": HELP_AGENT_REPLY_MAX}},
                           ["text"]),
    },
    "attach_helper_file": {
        "name": "attach_helper_file",
        "description": "Attach a file the helper uploaded (and marked attachable) to the RFI.",
        "parameters": _obj({"artifact_id": {"type": "string", "format": "uuid"},
                            "kind": {"type": "string", "enum": ["question", "answer"]}},
                           ["artifact_id", "kind"]),
    },
    "add_helper_note": {
        "name": "add_helper_note",
        "description": "Add the helper's own words (by message id) to the RFI as a note, verbatim.",
        "parameters": _obj({"message_ids": {"type": "array", "items": {"type": "integer"},
                                            "minItems": 1, "maxItems": 5}}, ["message_ids"]),
    },
    # End.
    "finish": {
        "name": "finish",
        "description": (
            "End the job. captured: only after capture_rfi_detail succeeded. nothing_to_do: the RFI "
            "needs no import (for example a draft). cannot_complete: give error_kind."
        ),
        "parameters": _obj({"outcome": {"type": "string", "enum": ["captured", "nothing_to_do", "cannot_complete"]},
                            "summary": {"type": "string", "maxLength": FINISH_SUMMARY_MAX},
                            "error_kind": {"type": "string",
                                           "enum": ["not_found", "access_denied", "unsupported", "other"]}},
                           ["outcome", "summary"]),
    },
}

SITE_TOOLS_PROCORE = ("ensure_login", "procore_resolve_company", "procore_list_rfis", "capture_rfi_detail")
BROWSER_TOOLS = ("navigate", "read_page", "find_text", "click", "type_text", "select_option",
                 "press_key", "scroll", "go_back", "wait_for")
HELP_TOOLS = ("request_help", "reply_help", "attach_helper_file", "add_helper_note")
FINISH_TOOLS = ("finish",)

TOOLS_BY_TYPE: dict[str, tuple[str, ...]] = {
    "rfi_from_email": SITE_TOOLS_PROCORE + BROWSER_TOOLS + HELP_TOOLS + FINISH_TOOLS,
    "canary": ("navigate", "read_page", "find_text", "wait_for") + FINISH_TOOLS,
    "user_browser_read": ("ensure_login", "navigate", "read_page", "find_text", "scroll", "go_back",
                          "wait_for", "request_help", "reply_help", "finish"),
}

# Never exposed to a model: fill_secret, JS evaluation, uploads, downloads,
# console/network reads, new tabs, file:// and chrome:// URLs.
FORBIDDEN_TOOL_NAMES = ("fill_secret", "evaluate", "upload_file", "download", "new_tab")


def tools_for(agent_type: str) -> list[dict[str, Any]]:
    """The tool schema list BDR sends to the provider for this type."""
    return [TOOL_SCHEMAS[name] for name in TOOLS_BY_TYPE[agent_type]]


# Tool args models (same rules as TOOL_SCHEMAS; a test keeps them in step).

_RefStr = Annotated[str, Field(pattern=r"^ref_\d{1,6}$")]


class _Args(BaseModel):
    model_config = ConfigDict(extra="forbid")


class EnsureLoginArgs(_Args):
    host: ProcoreHost


class ResolveCompanyArgs(_Args):
    host: ProcoreHost
    project_id: ProcoreId


class ListRfisArgs(_Args):
    host: ProcoreHost
    project_id: ProcoreId
    company_id: ProcoreId


class CaptureRfiDetailArgs(_Args):
    host: ProcoreHost
    project_id: ProcoreId
    rfi_id: ProcoreId
    company_id: ProcoreId


class NavigateArgs(_Args):
    url: Annotated[str, Field(pattern=r"^https://", max_length=URL_MAX)]


class ReadPageArgs(_Args):
    max_chars: Annotated[int, Field(ge=500, le=20000)] | None = None


class FindTextArgs(_Args):
    query: Annotated[str, Field(min_length=1, max_length=200)]


class ClickArgs(_Args):
    ref: _RefStr


class TypeTextArgs(_Args):
    ref: _RefStr
    text: Annotated[str, Field(max_length=500)]
    submit: bool | None = None


class SelectOptionArgs(_Args):
    ref: _RefStr
    value: Annotated[str, Field(max_length=200)]


class PressKeyArgs(_Args):
    key: Literal["Enter", "Tab", "Escape", "ArrowUp", "ArrowDown", "PageUp", "PageDown"]


class ScrollArgs(_Args):
    direction: Literal["up", "down"]
    amount: Annotated[int, Field(ge=1, le=10)]


class GoBackArgs(_Args):
    pass


class WaitForArgs(_Args):
    text: Annotated[str, Field(max_length=200)] | None = None
    seconds: Annotated[int, Field(ge=1, le=10)] | None = None


class RequestHelpArgs(_Args):
    reason_code: Literal["access_denied", "not_found", "ambiguous", "unexpected_page", "needs_info", "other"]
    message: Annotated[str, Field(min_length=1, max_length=HELP_AGENT_MESSAGE_MAX)]


class ReplyHelpArgs(_Args):
    text: Annotated[str, Field(min_length=1, max_length=HELP_AGENT_REPLY_MAX)]


class AttachHelperFileArgs(_Args):
    artifact_id: UUID
    kind: Literal["question", "answer"]


class AddHelperNoteArgs(_Args):
    message_ids: Annotated[list[int], Field(min_length=1, max_length=5)]


class FinishArgs(_Args):
    outcome: Literal["captured", "nothing_to_do", "cannot_complete"]
    summary: Annotated[str, Field(max_length=FINISH_SUMMARY_MAX)]
    error_kind: Literal["not_found", "access_denied", "unsupported", "other"] | None = None

    @model_validator(mode="after")
    def _error_kind(self) -> "FinishArgs":
        if self.outcome == "cannot_complete" and self.error_kind is None:
            raise ValueError("cannot_complete requires error_kind")
        return self


TOOL_ARG_MODELS: dict[str, type[_Args]] = {
    "ensure_login": EnsureLoginArgs,
    "procore_resolve_company": ResolveCompanyArgs,
    "procore_list_rfis": ListRfisArgs,
    "capture_rfi_detail": CaptureRfiDetailArgs,
    "navigate": NavigateArgs,
    "read_page": ReadPageArgs,
    "find_text": FindTextArgs,
    "click": ClickArgs,
    "type_text": TypeTextArgs,
    "select_option": SelectOptionArgs,
    "press_key": PressKeyArgs,
    "scroll": ScrollArgs,
    "go_back": GoBackArgs,
    "wait_for": WaitForArgs,
    "request_help": RequestHelpArgs,
    "reply_help": ReplyHelpArgs,
    "attach_helper_file": AttachHelperFileArgs,
    "add_helper_note": AddHelperNoteArgs,
    "finish": FinishArgs,
}


class ToolCallInvalid(ValueError):
    """A model tool call failed the schema or the per-type tool set. BDR turns
    it into an error observation (it still counts as a step)."""

    def __init__(self, tool: str, reason: str):
        super().__init__(f"{tool}: {reason}")
        self.tool = tool
        self.reason = reason


def validate_tool_call(agent_type: str, tool: str, args: dict[str, Any]) -> _Args:
    """Schema and tool-set check shared by BDR /turns and the worker executor.
    Host/path ceilings and help-file ownership are checked separately."""
    if tool not in TOOLS_BY_TYPE.get(agent_type, ()):
        raise ToolCallInvalid(tool, "tool_not_allowed")
    try:
        return TOOL_ARG_MODELS[tool].model_validate(args)
    except ValidationError as exc:
        first = exc.errors()[0] if exc.errors() else {"loc": (), "msg": "invalid"}
        where = ".".join(str(p) for p in first.get("loc", ())) or "args"
        raise ToolCallInvalid(tool, f"invalid_args: {where}: {first.get('msg')}") from None


def finish_to_job(agent_type: str, args: FinishArgs) -> dict[str, Any]:
    """Map the model's finish() to the job write. `captured` is not a finish:
    the executor submits /rfi-capture (only when a valid rfi_detail_json
    artifact exists for this job) and the job moves to applying."""
    if args.outcome == "captured":
        if agent_type != "rfi_from_email":
            raise ToolCallInvalid("finish", "captured_not_supported_for_type")
        return {"action": "rfi_capture"}
    if args.outcome == "nothing_to_do":
        outcome = "self_test_ok" if agent_type == "canary" else "nothing_to_do"
        return {"action": "finish", "status": "succeeded", "outcome": outcome}
    return {"action": "finish", "status": "failed", "error_kind": args.error_kind or "other"}
