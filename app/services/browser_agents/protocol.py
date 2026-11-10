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
from urllib.parse import urlsplit
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


# User browser v2 (docs/ASSISTANT_BROWSER_V2_PLAN.md section 15.3) --------------------
#
# Additive only. Nothing above this line changes for the org agent
# (rfi_from_email), the canary or the retired user_browser_read type:
# SECRET_INPUT_RE, TOKEN_QUERY_KEY_RE, HelpReasonCode, LOGIN_HELP_REASONS,
# ProcoreHost, the Literal["procore"] models, every existing TOOL_ARG_MODELS
# entry, ReleaseManifest and canonical_manifest_bytes keep their bytes and
# meaning. The new type gets its own names: new tools, new argument models,
# new wire models and a separate help reason vocabulary. Origins are validated
# strings, never ProcoreHost.

USER_BROWSER_TYPE = "user_browser"

# Model type_text refuses fields whose name, id, label or placeholder match
# this, for the user type only. Ordinary "Zip code" and "Cost code" fields are
# no longer blocked (the org agent keeps SECRET_INPUT_RE).
USER_SECRET_INPUT_RE = re.compile(r"(?i)password|passcode|one.?time|otp|mfa|2fa|verification code|security code")

SITE_KEY_RE = re.compile(r"^[a-z0-9_.:-]{2,80}$")
ACCOUNT_LABEL_RE = re.compile(r"^[A-Za-z0-9 _.-]{1,40}$")
# A lowercase DNS name with at least one dot (single-label hosts are never
# allowed for the user type, plan 10.1 item 4).
_LABEL = r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
HOST_RE = re.compile(rf"^{_LABEL}(?:\.{_LABEL})+$")
ORIGIN_RE = re.compile(rf"^https?://{_LABEL}(?:\.{_LABEL})+(?::\d{{1,5}})?$")
HTTPS_ORIGIN_RE = re.compile(rf"^https://{_LABEL}(?:\.{_LABEL})+(?::\d{{1,5}})?$")
COOKIE_DOMAIN_RE = re.compile(rf"^\.?{_LABEL}(?:\.{_LABEL})+$")
HTTP_METHOD_RE = re.compile(r"^[A-Z]{3,10}$")
HTTP_URL_RE = re.compile(r"^https?://")
SAFE_FILENAME_RE = re.compile(r"^[^/\\\x00-\x1f]{1,200}$")

USER_GOAL_MAX = 2000
USER_RESULT_MAX = 4000
USER_FACTS_MAX = 20
USER_FACT_TEXT_MAX = 500
USER_HOLD_FIELDS_MAX = 100
USER_HOLD_TOKEN_TTL_SECONDS = 120          # exp of a release token (plan 6.5 step 4)
USER_HOLD_RELEASE_POLL_SECONDS = 25        # long poll per call on the release route
USER_SESSION_POLL_SECONDS = 15             # session pickup poll while another task logs in (7.4)
USER_TYPED_STRING_MIN_CHARS = 12           # typed-string exfiltration rule (10.3)
# Floor maximums in signed code; the job policy may only be lower (11.1).
USER_DOWNLOAD_FILE_BYTES_FLOOR = 450 * 1024 * 1024
USER_DOWNLOAD_TASK_BYTES_FLOOR = 2 * 1024 * 1024 * 1024
USER_DOWNLOAD_FILES_FLOOR = 50
USER_LIVE_FRAME_MAX_BYTES = 1024 * 1024
USER_LIVE_INPUT_BATCH_MAX = 200

# Identity providers are never credential origins (7.5). Signed code: the
# worker refuses to type a credential on these even if the server says otherwise.
IDENTITY_PROVIDER_HOSTS = (
    "login.microsoftonline.com", "login.microsoft.com", "login.live.com", "accounts.google.com",
    "appleid.apple.com", "*.okta.com", "*.onelogin.com", "*.pingone.com",
)
# Domains anyone can host content or forms on (5.1): a grant is an exact
# origin plus a path prefix there, never the whole registrable domain.
MULTI_TENANT_DOMAINS = (
    "google.com", "microsoft.com", "sharepoint.com", "live.com", "office.com", "box.com",
    "dropbox.com", "github.com", "githubusercontent.com", "amazonaws.com", "azurewebsites.net",
    "notion.site", "forms.gle",
)
# Host names refused before any request (10.1 item 4).
BLOCKED_HOST_SUFFIXES = (".local", ".lan", ".internal", ".home.arpa")


def host_matches(host: str, patterns: tuple[str, ...] | list[str]) -> bool:
    """True when `host` equals a pattern or, for "*.x", is a subdomain of x."""
    h = (host or "").strip().lower().rstrip(".")
    for pat in patterns:
        p = pat.lower()
        if p.startswith("*."):
            if h.endswith(p[1:]) and h != p[2:]:
                return True
        elif h == p:
            return True
    return False


def is_identity_provider_host(host: str) -> bool:
    return host_matches(host, IDENTITY_PROVIDER_HOSTS)


def is_multi_tenant_domain(domain: str) -> bool:
    d = (domain or "").strip().lower().lstrip(".")
    return any(d == m or d.endswith("." + m) for m in MULTI_TENANT_DOMAINS)


def url_origin(url: str) -> str | None:
    """scheme://host[:port] in lowercase, default ports dropped; None if invalid."""
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        return None
    scheme = (parts.scheme or "").lower()
    host = (parts.hostname or "").lower()
    if scheme not in ("http", "https") or not host:
        return None
    if port is None or (scheme, port) in (("https", 443), ("http", 80)):
        return f"{scheme}://{host}"
    return f"{scheme}://{host}:{port}"


def _origin_host(origin: str) -> str:
    return (urlsplit(origin).hostname or "").lower()


def _check_regex(value: str) -> str:
    try:
        re.compile(value)
    except re.error as exc:
        raise ValueError(f"invalid regex: {exc}") from None
    return value


SiteKey = Annotated[str, Field(pattern=SITE_KEY_RE.pattern)]
AccountLabel = Annotated[str, Field(pattern=ACCOUNT_LABEL_RE.pattern)]
Host = Annotated[str, Field(pattern=HOST_RE.pattern, max_length=253)]
Origin = Annotated[str, Field(pattern=ORIGIN_RE.pattern, max_length=300)]
HttpsOrigin = Annotated[str, Field(pattern=HTTPS_ORIGIN_RE.pattern, max_length=300)]
CookieDomain = Annotated[str, Field(pattern=COOKIE_DOMAIN_RE.pattern, max_length=254)]
HttpMethod = Annotated[str, Field(pattern=HTTP_METHOD_RE.pattern)]
HttpUrlStr = Annotated[str, Field(pattern=HTTP_URL_RE.pattern, min_length=8, max_length=URL_MAX)]
PathRegex = Annotated[str, Field(min_length=1, max_length=300)]


# Help reasons for the user type only (8.1). The shared HelpReasonCode and
# LOGIN_HELP_REASONS stay as they are for the org agent.
UserHelpReasonCode = Literal[
    "login_failed", "login_locked", "captcha", "mfa", "sso", "logged_out", "access_denied",
    "not_found", "ambiguous", "unexpected_page", "cap_reached", "needs_info", "provider_config",
    "other", "needs_credential", "needs_site", "mfa_step_up", "push", "hold",
]
USER_HELP_REASONS_ADDED = ("needs_credential", "needs_site", "mfa_step_up", "push", "hold")
# Reasons the model may pick in user_request_help (push and hold come from the
# worker and the server, never the model).
USER_MODEL_HELP_REASONS = MODEL_HELP_REASONS + ("needs_credential", "needs_site", "mfa_step_up", "sso")

UserLoginOutcome = Literal[
    "ok", "password_rejected", "mfa", "push", "captcha", "challenge", "sso", "unexpected", "timeout",
]
UserLoginRefusal = Literal[
    "spacing", "login_busy", "locked", "help_open", "not_configured", "ip_not_allowed",
    "not_in_job", "origin_not_allowed", "identity_provider", "not_https", "daily_cap", "sso_required",
]
UserDownloadKind = Literal[
    "pdf", "png", "jpeg", "tiff", "gif", "zip", "ooxml", "ole", "csv", "text", "dwg", "dxf", "rtf",
]


# jobs.input for user_browser ----------------------------------------------------


class UserJobAuthority(_Req):
    """jobs.input.authority, written by assistant_browser_task_enqueue_v2 from
    the admission (role, model and cap are never the caller's). Recorded for
    turn begin and audit; the live authority is rechecked by every fence."""
    role: Annotated[str, Field(min_length=1, max_length=64)]
    model: Annotated[str, Field(min_length=1, max_length=120)] | None = None
    conversation_id: UUID
    max_cost_usd: Annotated[float, Field(gt=0, le=5)]
    admission_id: UUID


class UserBrowserInput(_Req):
    """The admitted task as the worker sees it, exactly the jobs.input that
    assistant_browser_task_enqueue_v2 writes (its caller passes p_input = {}).
    Credentials, grants and caps are never input: they come from the
    lease-fenced job policy (UserJobPolicy)."""
    admission_id: UUID
    goal: Annotated[str, Field(min_length=1, max_length=USER_GOAL_MAX)]
    start_url: HttpUrlStr | None = None
    start_site_key: SiteKey | None = None
    project_id: UUID | None = None
    authority: UserJobAuthority

    @model_validator(mode="after")
    def _has_start(self) -> "UserBrowserInput":
        if self.start_url is None and self.start_site_key is None:
            raise ValueError("start_url or start_site_key is required")
        if self.start_url is not None and url_origin(self.start_url) is None:
            raise ValueError("start_url is not an http(s) URL")
        if self.authority.admission_id != self.admission_id:
            raise ValueError("authority is for another admission")
        return self


# Job policy (10.2): GET /jobs/{job_id}/user/policy --------------------------------


class UserSafeMutation(_Resp):
    method: HttpMethod
    host: Host
    path_regex: PathRegex
    graphql_operations: list[Annotated[str, Field(min_length=1, max_length=120)]] | None = None

    @model_validator(mode="after")
    def _regex(self) -> "UserSafeMutation":
        _check_regex(self.path_regex)
        return self


class UserFinalAction(_Resp):
    method: HttpMethod
    host: Host
    path_regex: PathRegex
    label: Annotated[str, Field(min_length=1, max_length=120)]

    @model_validator(mode="after")
    def _regex(self) -> "UserFinalAction":
        _check_regex(self.path_regex)
        return self


class UserFinalGet(_Resp):
    host: Host
    path_regex: PathRegex
    label: Annotated[str, Field(max_length=120)] | None = None

    @model_validator(mode="after")
    def _regex(self) -> "UserFinalGet":
        _check_regex(self.path_regex)
        return self


class UserLoggedInProbe(_Resp):
    path: Annotated[str, Field(pattern=r"^/", max_length=300)]
    selector: Annotated[str, Field(min_length=1, max_length=300)]


class UserSiteProfile(_Resp):
    """assistant_browser_sites.profile (6.4). final_actions only label cards;
    they never release anything."""
    safe_mutations: list[UserSafeMutation] = Field(default_factory=list, max_length=200)
    final_actions: list[UserFinalAction] = Field(default_factory=list, max_length=200)
    final_get: list[UserFinalGet] = Field(default_factory=list, max_length=200)
    websocket_ok: bool = False
    websocket_reason: Annotated[str, Field(max_length=300)] | None = None
    logged_in_probe: UserLoggedInProbe | None = None


class UserPolicySite(_Resp):
    site_key: SiteKey
    label: Annotated[str, Field(min_length=1, max_length=120)]
    kind: Literal["curated", "ad_hoc"]
    scope: Literal["domain", "origin"]
    grant_version: Annotated[int, Field(ge=1)]
    registrable_domains: list[Host] = Field(default_factory=list, max_length=20)
    exact_origins: list[Origin] = Field(default_factory=list, max_length=20)
    path_prefix: Annotated[str, Field(pattern=r"^/", max_length=300)] | None = None
    login_paths: list[PathRegex] = Field(default_factory=list, max_length=20)
    needs_headed: bool = False
    profile: UserSiteProfile = Field(default_factory=UserSiteProfile)

    @model_validator(mode="after")
    def _shape(self) -> "UserPolicySite":
        if self.scope == "domain" and not self.registrable_domains:
            raise ValueError("a domain grant needs registrable_domains")
        if self.scope == "origin" and not self.exact_origins:
            raise ValueError("an origin grant needs exact_origins")
        for domain in self.registrable_domains:
            if is_multi_tenant_domain(domain):
                raise ValueError(f"multi-tenant domain {domain} is granted by exact origin only")
        for p in self.login_paths:
            _check_regex(p)
        return self

    @property
    def websocket_allowed(self) -> bool:
        """websocket_ok counts only on curated rows (6.2, 6.4)."""
        return self.kind == "curated" and self.profile.websocket_ok


class UserPolicyCredential(_Resp):
    """A credential in the job's set (assistant_browser_job_credentials). Never
    carries a username or password; those come per attempt from login/begin."""
    credential_id: UUID
    site_key: SiteKey
    account_label: AccountLabel
    version: Annotated[int, Field(ge=1)]
    login_origins: Annotated[list[HttpsOrigin], Field(min_length=1, max_length=10)]
    cookie_domains: Annotated[list[CookieDomain], Field(min_length=1, max_length=10)]
    sso_required: bool = False

    @model_validator(mode="after")
    def _no_identity_provider(self) -> "UserPolicyCredential":
        for origin in self.login_origins:
            if is_identity_provider_host(_origin_host(origin)):
                raise ValueError("an identity provider is never a credential login origin")
        return self


class UserSafeRule(_Resp):
    """The owner's own "not a final action" rule (6.3)."""
    site_key: SiteKey
    method: HttpMethod
    host: Host
    path_pattern: PathRegex


class UserPolicyCaps(_Resp):
    offsite_query_bytes: Annotated[int, Field(ge=0, le=1024 * 1024)] = 4096
    download_file_bytes: Annotated[int, Field(ge=0, le=USER_DOWNLOAD_FILE_BYTES_FLOOR)] = 100 * 1024 * 1024
    download_task_bytes: Annotated[int, Field(ge=0, le=USER_DOWNLOAD_TASK_BYTES_FLOOR)] = 500 * 1024 * 1024
    download_files: Annotated[int, Field(ge=0, le=USER_DOWNLOAD_FILES_FLOOR)] = 20
    hold_timeout_seconds: Annotated[int, Field(ge=30, le=3600)] = 600
    park_max_minutes: Annotated[int, Field(ge=1, le=240)] = 15


class UserJobPolicy(_Resp):
    """Server-supplied per-job policy (10.2). The worker fetches it after claim
    and after every site_hold release, safe-rule change or scope fence code;
    a fence returning a newer policy_version means refetch. It can narrow or
    widen the task scope, never the signed floor (private networks, identity
    providers, schemes, holds, token verification, size floors)."""
    policy_version: Annotated[int, Field(ge=1)]
    job_id: UUID
    start_origin: Origin
    sites: Annotated[list[UserPolicySite], Field(min_length=1, max_length=50)]
    credentials: list[UserPolicyCredential] = Field(default_factory=list, max_length=50)
    safe_rules: list[UserSafeRule] = Field(default_factory=list, max_length=500)
    caps: UserPolicyCaps = Field(default_factory=UserPolicyCaps)

    @model_validator(mode="after")
    def _shape(self) -> "UserJobPolicy":
        keys = [s.site_key for s in self.sites]
        if len(set(keys)) != len(keys):
            raise ValueError("duplicate site_key in sites")
        seen: set[str] = set()
        for cred in self.credentials:
            if cred.site_key not in keys:
                raise ValueError("credential for a site outside the task scope")
            if cred.site_key in seen:
                raise ValueError("at most one credential per site in a job")
            seen.add(cred.site_key)
        for rule in self.safe_rules:
            if rule.site_key not in keys:
                raise ValueError("safe rule for a site outside the task scope")
        return self

    def site(self, site_key: str) -> UserPolicySite | None:
        return next((s for s in self.sites if s.site_key == site_key), None)

    def credential(self, credential_id: UUID | str) -> UserPolicyCredential | None:
        return next((c for c in self.credentials if str(c.credential_id) == str(credential_id)), None)


# Holds (6.5): POST /jobs/{job_id}/user/holds,
# GET /jobs/{job_id}/user/holds/{hold_id}/release (long poll) ------------------


class UserHoldField(_Req):
    """One form field in a held request body. `value` is set only when it
    equals a string the model typed with type_text or select_option in this
    job; every other value is hidden whatever the field is named."""
    name: Annotated[str, Field(max_length=200)]
    value: Annotated[str, Field(max_length=500)] | None = None
    hidden: bool = True

    @model_validator(mode="after")
    def _hidden_has_no_value(self) -> "UserHoldField":
        if self.hidden and self.value is not None:
            raise ValueError("a hidden field carries no value")
        if not self.hidden and self.value is None:
            raise ValueError("a shown field carries its value")
        return self


class UserHoldRequest(_Req):
    """A held request (network hold) or a held click (click-level hold, 6.2
    item 4). The full URL with its query lives only in the encrypted hold
    record and on the owner's card; traces keep origin and path."""
    trigger: Literal["network", "click"]
    frame: Literal["main", "sub", "worker"] = "main"
    method: HttpMethod
    url: HttpUrlStr
    origin: Origin
    url_sha256: Sha256
    body_sha256: Sha256
    body_summary: list[UserHoldField] = Field(default_factory=list, max_length=USER_HOLD_FIELDS_MAX)
    element_label: Annotated[str, Field(max_length=200)] | None = None
    profile_label: Annotated[str, Field(max_length=120)] | None = None
    model_description: Annotated[str, Field(max_length=300)] | None = None
    out_of_scope: bool = False
    screenshot_artifact_id: UUID | None = None

    @model_validator(mode="after")
    def _shape(self) -> "UserHoldRequest":
        if url_origin(self.url) != self.origin:
            raise ValueError("origin does not match url")
        if self.trigger == "click" and not self.element_label:
            raise ValueError("a click hold needs the element label")
        return self


class UserSiteHoldRequest(_Req):
    """A top-level navigation outside the task scope, held before DNS (5.4).
    The worker ran the exfiltration rules on the full URL before sending this."""
    trigger: Literal["navigate", "link", "redirect", "popup"]
    url: HttpUrlStr
    target_origin: Origin
    from_origin: Origin | None = None
    carries_data: bool = False

    @model_validator(mode="after")
    def _shape(self) -> "UserSiteHoldRequest":
        if url_origin(self.url) != self.target_origin:
            raise ValueError("target_origin does not match url")
        return self


HOLD_TOKEN_FIELDS = ("job_id", "lease_epoch", "hold_id", "method", "origin", "url_sha256", "body_sha256", "exp")


class UserHoldToken(_Resp):
    """Ed25519 release token (6.5 step 4). The signature covers
    canonical_hold_token_bytes(token) with BROWSER_HOLD_SIGNING_KEY; the worker
    verifies it with the public key compiled into its signed release, plus job,
    epoch, hold id, method, origin, URL hash, body hash and exp, and uses a
    hold_id once."""
    job_id: UUID
    lease_epoch: Annotated[int, Field(ge=0)]
    hold_id: UUID
    method: HttpMethod
    origin: Origin
    url_sha256: Sha256
    body_sha256: Sha256
    exp: Annotated[int, Field(ge=0)]                    # unix seconds
    key_id: Annotated[str, Field(min_length=1, max_length=64)]
    signature: Annotated[str, Field(min_length=1, max_length=200)]   # base64


def canonical_hold_token_bytes(token: UserHoldToken | dict[str, Any]) -> bytes:
    """The exact bytes the hold signing key signs: the eight HOLD_TOKEN_FIELDS,
    sorted keys, no spaces, UTF-8. key_id and signature are not signed."""
    data = token.model_dump(mode="json") if isinstance(token, BaseModel) else dict(token)
    out = {k: data[k] for k in HOLD_TOKEN_FIELDS}
    for k in ("job_id", "hold_id"):
        out[k] = str(out[k]).lower()
    out["lease_epoch"] = int(out["lease_epoch"])
    out["exp"] = int(out["exp"])
    return json.dumps(out, sort_keys=True, separators=(",", ":")).encode("utf-8")


class UserHoldRelease(_Resp):
    """Answer to a hold or site hold, and to the release long poll.
    final_action released: a token for exactly that request. site released:
    `site_release` says whether the held navigation continues (full) or the
    worker opens the origin root (site_only); refetch the policy when
    policy_version is newer. declined, expired, aborted: abort the request;
    `label` is shown to the model ("The owner declined: <label>")."""
    hold_id: UUID
    kind: Literal["final_action", "site"] = "final_action"
    status: Literal["pending", "released", "declined", "expired", "aborted"]
    token: UserHoldToken | None = None
    site_release: Literal["full", "site_only"] | None = None
    policy_version: Annotated[int, Field(ge=1)] | None = None
    label: Annotated[str, Field(max_length=200)] | None = None
    retry_after: Annotated[int, Field(ge=0, le=60)] | None = None

    @model_validator(mode="after")
    def _shape(self) -> "UserHoldRelease":
        released = self.status == "released"
        if self.kind == "final_action":
            if released and self.token is None:
                raise ValueError("a released final action carries a token")
            if self.token is not None and (not released or self.token.hold_id != self.hold_id):
                raise ValueError("a token only for this released hold")
            if self.site_release is not None:
                raise ValueError("site_release is only for site holds")
        else:
            if self.token is not None:
                raise ValueError("site holds carry no token")
            if released and self.site_release is None:
                raise ValueError("a released site hold says full or site_only")
            if not released and self.site_release is not None:
                raise ValueError("site_release only when released")
        return self


# Credentials (7.4): POST /jobs/{job_id}/user/login/begin,
# POST /jobs/{job_id}/user/login/result, GET /jobs/{job_id}/user/session ----------


class UserCredentialFillRequest(_Req):
    """Sent by fill_credential before typing. The worker already checked the
    page is https, the top-level and frame origins are both in the
    credential's login_origins (from the job policy) and not an identity
    provider; the server checks the credential is in the job set again."""
    credential_id: UUID
    origin: HttpsOrigin
    frame_origin: HttpsOrigin
    rejected_jar_logged_in_at: datetime | None = None

    @model_validator(mode="after")
    def _shape(self) -> "UserCredentialFillRequest":
        if self.frame_origin != self.origin:
            raise ValueError("fill_credential never types into a cross-origin frame")
        if is_identity_provider_host(_origin_host(self.origin)):
            raise ValueError("never types a credential on an identity provider")
        return self


class UserCredentialFillResponse(_Resp):
    """granted: type username and password into the filled form, then report
    login/result. Sent with Cache-Control: no-store. Refused: spacing or
    login_busy -> poll the session route (UserSessionPoll) before waiting out
    the spacing; help_open -> join help_id; anything else ends the attempt."""
    granted: bool
    attempt_id: UUID | None = None
    username: SecretStr | None = None
    password: SecretStr | None = None
    reason: UserLoginRefusal | None = None
    retry_at: datetime | None = None
    help_id: UUID | None = None

    @model_validator(mode="after")
    def _shape(self) -> "UserCredentialFillResponse":
        if self.granted and not (self.attempt_id and self.username and self.password):
            raise ValueError("granted requires attempt_id, username and password")
        if not self.granted and (self.password is not None or self.username is not None or self.reason is None):
            raise ValueError("refused carries a reason and no secret")
        return self


class UserCredentialResultRequest(_Req):
    """Outcome of a login attempt. Cookies only with ok, and only those whose
    domain is in the credential's cookie_domains (the server re-checks)."""
    credential_id: UUID
    attempt_id: UUID
    origin: HttpsOrigin
    outcome: UserLoginOutcome
    step: Annotated[str, Field(max_length=60)] | None = None
    cookies: list[Cookie] | None = None

    @model_validator(mode="after")
    def _ok_has_cookies(self) -> "UserCredentialResultRequest":
        if self.outcome == "ok" and not self.cookies:
            raise ValueError("outcome ok requires cookies")
        if self.outcome != "ok" and self.cookies:
            raise ValueError("cookies are sent only with outcome ok")
        return self


class UserCredentialResultResponse(_Resp):
    """attempt_open: mfa and push keep the attempt alive (8.1, 8.4)."""
    attempt_open: bool = False
    locked_until: datetime | None = None
    login_failures: int = 0
    attempts_today: int = 0
    help_reason: UserHelpReasonCode | None = None


class UserSessionPoll(_Req):
    """Query of GET /jobs/{job_id}/user/session: a session for this credential
    saved after `newer_than` (the job's claim time). Without `origin` the
    server answers with the newest unexpired session of that account whose
    origin is in the task scope (assistant_browser_session_get_v2 with a null
    origin). Answered with SessionResponse (ok with cookies, or no_jar)."""
    credential_id: UUID
    newer_than: datetime
    origin: HttpsOrigin | None = None


class UserHelpOpenV2Request(_Req):
    """Help open for user_browser jobs, with the user reason vocabulary and the
    credential the help is about (login and code scope)."""
    reason_code: UserHelpReasonCode
    message: Annotated[str, Field(max_length=HELP_AGENT_MESSAGE_MAX)] | None = None
    screenshot_artifact_id: UUID | None = None
    login_attempt_id: UUID | None = None
    credential_id: UUID | None = None
    hold_id: UUID | None = None

    @model_validator(mode="after")
    def _shape(self) -> "UserHelpOpenV2Request":
        if self.reason_code == "hold" and self.hold_id is None:
            raise ValueError("a hold help row names its hold")
        if self.reason_code in ("mfa", "push") and self.login_attempt_id is None:
            raise ValueError("mfa and push belong to a login attempt")
        if self.reason_code == "mfa_step_up" and self.credential_id is None:
            raise ValueError("mfa_step_up names the credential")
        return self


# Downloads (11): POST /jobs/{job_id}/user/downloads (multipart) --------------------


class UserDownloadMeta(_Req):
    """Multipart form fields beside the `file` part. Sent only for a file that
    passed the type allowlist (magic bytes) and a clean Defender scan."""
    sha256: Sha256
    filename: Annotated[str, Field(pattern=SAFE_FILENAME_RE.pattern)]
    size_bytes: Annotated[int, Field(ge=1, le=USER_DOWNLOAD_FILE_BYTES_FLOOR)]
    sniffed_kind: UserDownloadKind
    source_origin: Origin
    scan: Literal["clean"]


class UserDownloadResponse(_Resp):
    download_id: UUID
    status: Literal["saved_to_project", "staged", "rejected"]
    file_id: UUID | None = None
    reason: Annotated[str, Field(max_length=200)] | None = None


# Live view (9, wave E): POST /jobs/{job_id}/live/frames,
# GET /jobs/{job_id}/live/input ---------------------------------------------------


class LiveFrameMeta(_Req):
    """Form fields beside the JPEG frame. Frames are never stored beyond their
    TTL, never traced and never sent to a model."""
    session_id: UUID
    seq: Annotated[int, Field(ge=0)]
    width: Annotated[int, Field(ge=1, le=4096)]
    height: Annotated[int, Field(ge=1, le=4096)]
    mime_type: Literal["image/jpeg"] = "image/jpeg"
    size_bytes: Annotated[int, Field(ge=1, le=USER_LIVE_FRAME_MAX_BYTES)]
    captured_at: datetime


_LIVE_MOUSE = ("mousePressed", "mouseReleased", "mouseMoved", "mouseWheel")
_LIVE_KEY = ("keyDown", "keyUp", "char")


class LiveInputEvent(_Req):
    """One owner input event, replayed by the worker with CDP
    Input.dispatchMouseEvent or Input.dispatchKeyEvent. Key fields can carry
    a password being typed: repr never shows them and they are never logged."""
    seq: Annotated[int, Field(ge=0)]
    type: Literal["mousePressed", "mouseReleased", "mouseMoved", "mouseWheel", "keyDown", "keyUp", "char"]
    x: Annotated[float, Field(ge=0, le=4096)] | None = None
    y: Annotated[float, Field(ge=0, le=4096)] | None = None
    button: Literal["none", "left", "middle", "right"] | None = None
    click_count: Annotated[int, Field(ge=0, le=3)] | None = None
    delta_x: Annotated[float, Field(ge=-10000, le=10000)] | None = None
    delta_y: Annotated[float, Field(ge=-10000, le=10000)] | None = None
    key: Annotated[str, Field(min_length=1, max_length=40)] | None = None
    code: Annotated[str, Field(min_length=1, max_length=40)] | None = None
    text: Annotated[str, Field(min_length=1, max_length=4)] | None = None
    modifiers: Annotated[int, Field(ge=0, le=15)] = 0

    @model_validator(mode="after")
    def _shape(self) -> "LiveInputEvent":
        if self.type in _LIVE_MOUSE:
            if self.x is None or self.y is None:
                raise ValueError("mouse events need x and y")
            if self.key is not None or self.code is not None or self.text is not None:
                raise ValueError("mouse events carry no key fields")
        else:
            if self.x is not None or self.y is not None or self.button is not None:
                raise ValueError("key events carry no mouse fields")
            if self.type == "char" and self.text is None:
                raise ValueError("char needs text")
            if self.type != "char" and self.key is None:
                raise ValueError("keyDown and keyUp need key")
        return self

    def __repr__(self) -> str:   # never print what was typed
        return f"LiveInputEvent(seq={self.seq}, type={self.type!r})"

    __str__ = __repr__


class LiveInputBatch(_Resp):
    events: list[LiveInputEvent] = Field(default_factory=list, max_length=USER_LIVE_INPUT_BATCH_MAX)


# v2 routes. Kept apart from ENDPOINTS (whose fixture coverage test predates
# v2); WI-6 and WI-21 bind the server routes, WI-10 the worker client.
USER_V2_ENDPOINTS: dict[str, tuple[str, str, type[BaseModel] | None, Any, int]] = {
    "user_policy": ("GET", "/jobs/{job_id}/user/policy", None, UserJobPolicy, 200),
    "user_holds": ("POST", "/jobs/{job_id}/user/holds", UserHoldRequest, UserHoldRelease, 200),
    "user_hold_release": ("GET", "/jobs/{job_id}/user/holds/{hold_id}/release", None, UserHoldRelease, 200),
    "user_site_hold": ("POST", "/jobs/{job_id}/user/site-hold", UserSiteHoldRequest, UserHoldRelease, 200),
    "user_session_poll": ("GET", "/jobs/{job_id}/user/session", UserSessionPoll, SessionResponse, 200),
    "user_login_begin_v2": ("POST", "/jobs/{job_id}/user/login/begin", UserCredentialFillRequest,
                            UserCredentialFillResponse, 200),
    "user_login_result_v2": ("POST", "/jobs/{job_id}/user/login/result", UserCredentialResultRequest,
                             UserCredentialResultResponse, 200),
    "user_help_open_v2": ("POST", "/jobs/{job_id}/user/help", UserHelpOpenV2Request, HelpOpenResponse, 200),
    "user_downloads": ("POST", "/jobs/{job_id}/user/downloads", UserDownloadMeta, UserDownloadResponse, 200),
    "live_frames": ("POST", "/jobs/{job_id}/live/frames", LiveFrameMeta, None, 204),
    "live_input": ("GET", "/jobs/{job_id}/live/input", None, LiveInputBatch, 200),
}

INPUT_MODELS[USER_BROWSER_TYPE] = UserBrowserInput

# Code floor for the user type. Navigation scope is not fixed in code: it
# comes per job from UserJobPolicy, so nav_hosts and nav_paths are empty and
# Settings host narrowing has nothing to narrow. The extra keys are the
# signed-code lists the worker never takes from the server.
CEILINGS[USER_BROWSER_TYPE] = {
    "nav_hosts": [],
    "nav_paths": [],
    "resource_hosts": [],
    "login_hosts": [],
    "login_challenge_hosts": [],
    "identity_provider_hosts": list(IDENTITY_PROVIDER_HOSTS),
    "multi_tenant_domains": list(MULTI_TENANT_DOMAINS),
    "blocked_host_suffixes": list(BLOCKED_HOST_SUFFIXES),
}


# Tools for user_browser (15.2). New names with their own models; the shared
# request_help, finish and their models stay as they are.

_TAB = {"type": "string", "pattern": r"^tab_\d{1,4}$", "description": "A tab id from read_page."}

TOOL_SCHEMAS["switch_tab"] = {
    "name": "switch_tab",
    "description": "Make another tab of this task the active one. Tabs opened by the page belong to the task.",
    "parameters": _obj({"tab_id": _TAB}, ["tab_id"]),
}
TOOL_SCHEMAS["fill_credential"] = {
    "name": "fill_credential",
    "description": (
        "Fill the saved username or password into a login field by ref. You never see the value. "
        "Refused unless the page is the saved login page of the account for this site. Never type "
        "a password or code with type_text."
    ),
    "parameters": _obj({"ref": _REF, "field": {"type": "string", "enum": ["username", "password"]}},
                       ["ref", "field"]),
}
TOOL_SCHEMAS["user_request_help"] = {
    "name": "user_request_help",
    "description": (
        "Ask the task owner for help. needs_credential: no saved login works. needs_site: the goal "
        "needs a site that is not approved. mfa_step_up: the site asks for a code mid-task. sso: the "
        "site only offers Microsoft or Google sign-in. Never ask for a password or code in the message."
    ),
    "parameters": _obj({"reason_code": {"type": "string", "enum": list(USER_MODEL_HELP_REASONS)},
                        "message": {"type": "string", "minLength": 1, "maxLength": HELP_AGENT_MESSAGE_MAX}},
                       ["reason_code", "message"]),
}
TOOL_SCHEMAS["user_finish"] = {
    "name": "user_finish",
    "description": (
        "End the task. completed: give the result and the facts you found, each with the URL of the "
        "page it came from. cannot_complete: give error_kind and say why in result."
    ),
    "parameters": _obj({
        "outcome": {"type": "string", "enum": ["completed", "cannot_complete"]},
        "result": {"type": "string", "minLength": 1, "maxLength": USER_RESULT_MAX},
        "facts": {"type": "array", "maxItems": USER_FACTS_MAX, "items": _obj({
            "text": {"type": "string", "minLength": 1, "maxLength": USER_FACT_TEXT_MAX},
            "source_url": {"type": "string", "pattern": HTTP_URL_RE.pattern, "maxLength": URL_MAX},
        }, ["text", "source_url"])},
        "error_kind": {"type": "string",
                       "enum": ["not_found", "access_denied", "unsupported", "declined", "other"]},
    }, ["outcome", "result"]),
}

USER_TOOLS = ("switch_tab", "fill_credential", "user_request_help", "user_finish")
TOOLS_BY_TYPE[USER_BROWSER_TYPE] = (
    "navigate", "read_page", "find_text", "scroll", "go_back", "wait_for", "click", "type_text",
    "select_option", "press_key",
) + USER_TOOLS


class SwitchTabArgs(_Args):
    tab_id: Annotated[str, Field(pattern=r"^tab_\d{1,4}$")]


class FillCredentialArgs(_Args):
    ref: _RefStr
    field: Literal["username", "password"]


class UserRequestHelpArgs(_Args):
    reason_code: Literal["access_denied", "not_found", "ambiguous", "unexpected_page", "needs_info", "other",
                         "needs_credential", "needs_site", "mfa_step_up", "sso"]
    message: Annotated[str, Field(min_length=1, max_length=HELP_AGENT_MESSAGE_MAX)]


class UserFact(_Args):
    text: Annotated[str, Field(min_length=1, max_length=USER_FACT_TEXT_MAX)]
    source_url: Annotated[str, Field(pattern=HTTP_URL_RE.pattern, max_length=URL_MAX)]


class UserFinishArgs(_Args):
    """The handler maps this to the job outcome and checks every source_url
    is in the task scope (a fact from outside the scope is dropped)."""
    outcome: Literal["completed", "cannot_complete"]
    result: Annotated[str, Field(min_length=1, max_length=USER_RESULT_MAX)]
    facts: Annotated[list[UserFact], Field(max_length=USER_FACTS_MAX)] | None = None
    error_kind: Literal["not_found", "access_denied", "unsupported", "declined", "other"] | None = None

    @model_validator(mode="after")
    def _error_kind(self) -> "UserFinishArgs":
        if self.outcome == "cannot_complete" and self.error_kind is None:
            raise ValueError("cannot_complete requires error_kind")
        if self.outcome == "completed" and self.error_kind is not None:
            raise ValueError("error_kind is only for cannot_complete")
        return self


TOOL_ARG_MODELS["switch_tab"] = SwitchTabArgs
TOOL_ARG_MODELS["fill_credential"] = FillCredentialArgs
TOOL_ARG_MODELS["user_request_help"] = UserRequestHelpArgs
TOOL_ARG_MODELS["user_finish"] = UserFinishArgs
