"""Stable, machine-readable error codes returned in HTTPException ``detail``.

These codes are a contract, not prose: the frontend (bdr_fe/lib/api.ts) maps
them to user-facing messages, and they are documented for support/developers in
docs/ERROR_CODES.md. When a legitimate user trips one — e.g. a rate limit hit by
accident — they receive a stable code they can quote to the developers, who look
it up here and in the docs to explain exactly what happened and what to do.

Keep this module, docs/ERROR_CODES.md, and the frontend handling in sync.
"""

from enum import StrEnum


class ErrorCode(StrEnum):
    # ── Authentication / 2FA (raised in app/core/deps.py) ─────────────────────
    MFA_ENROLLMENT_REQUIRED = "mfa_enrollment_required"
    MFA_STEP_UP_REQUIRED = "mfa_step_up_required"

    # ── Rate limiting (raised in app/core/ratelimit.py) ───────────────────────
    # Accompanied by a Retry-After header (seconds until the window resets) and
    # an X-RateLimit-Scope header naming which limit tripped (see RATE_LIMITS).
    RATE_LIMITED = "rate_limited"


class RateLimitScope(StrEnum):
    """Values emitted in the X-RateLimit-Scope header for a RATE_LIMITED 429.

    A single stable string per protected surface so support/developers can tell,
    from one header, exactly which limit a user hit.
    """

    ESTIMATOR_API = "estimator_api"     # the external estimator's per-account cap
    AI_JOBS = "ai_jobs"                 # Claude/OpenAI extraction + generation
    FILE_UPLOAD = "file_upload"         # file uploads
    FILE_EXPORT = "file_export"         # ZIP export builds
    BULK_SEND = "bulk_send"             # RFQ email fan-out
    OUTBOUND_EMAIL = "outbound_email"   # invites + package / proposal mail
    NOTIFICATION_LOG = "notification_log"  # per-project notification-log assembly
    REPORT = "report"                   # multi-table report assembly (bid invitations)
    DEFAULT = "api"                     # generic catch-all budget


# Developer/support catalog: what each rate-limit scope protects and what a user
# who trips it should do. Surfaced verbatim in docs/ERROR_CODES.md.
RATE_LIMIT_HELP: dict[str, str] = {
    RateLimitScope.ESTIMATOR_API: (
        "Per-account request cap for the external estimator portal. Normal use "
        "never reaches it; wait for the Retry-After window and retry."
    ),
    RateLimitScope.AI_JOBS: (
        "Caps how often AI extraction/generation (BOQ analysis, proposal lines) "
        "can be launched per account, since each call spends model tokens. Wait "
        "and retry, or contact IT if you need a larger allowance."
    ),
    RateLimitScope.FILE_UPLOAD: "Caps file uploads per account per minute.",
    RateLimitScope.FILE_EXPORT: (
        "Caps project ZIP exports per account per minute (each builds a large "
        "archive). Wait for the Retry-After window."
    ),
    RateLimitScope.BULK_SEND: "Caps RFQ email fan-out per account per minute.",
    RateLimitScope.OUTBOUND_EMAIL: (
        "Caps branded outbound email (invites, packages, proposals) per account "
        "per hour to protect the shared mailbox's sending reputation."
    ),
    RateLimitScope.NOTIFICATION_LOG: (
        "Caps per-project notification-log reads per account per minute — each "
        "request fans out into dozens of database lookups to assemble the "
        "event/recipient view. Wait for the Retry-After window."
    ),
    RateLimitScope.REPORT: (
        "Caps multi-table report reads (Bid Invitations) per account per "
        "minute — each request scans several tables across the whole window. "
        "Wait for the Retry-After window."
    ),
    RateLimitScope.DEFAULT: "Generous catch-all request budget for other routes.",
}
