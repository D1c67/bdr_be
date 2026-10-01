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

    # ── RFP email intake (raised in app/routers/rfp_emails.py) ────────────────
    # Every refusal on that router carries its code in an `X-Error-Code` header.
    # The two rows whose code IS the contract (the locked-rule 403) also put the
    # code in `detail`; the rest put an app-authored sentence there, because the
    # sentence is what the reviewer needs to see and it is never attacker text.
    RFP_RULE_LOCKED_IT_ADMIN_ONLY = "rfp_rule_locked_it_admin_only"
    RFP_RULE_INVALID = "rfp_rule_invalid"
    RFP_RULE_PUBLIC_DOMAIN = "rfp_rule_public_domain"
    RFP_RULE_DUPLICATE = "rfp_rule_duplicate"
    RFP_EMAIL_NOT_ACTIONABLE = "rfp_email_not_actionable"

    # ── RFP blocked senders (routers/rfp_emails.py blocked-senders/*;
    # docs/RFP_EMAIL_INGESTION.md section 3.1). Same header contract. The
    # locked-row 403 puts its code in `detail` too, like the rule one above.
    RFP_BLOCK_LOCKED_IT_ADMIN_ONLY = "rfp_block_locked_it_admin_only"  # 403: platform row, IT Admin only
    RFP_BLOCK_INVALID = "rfp_block_invalid"                  # 400: not an address / bare domain
    RFP_BLOCK_PUBLIC_DOMAIN = "rfp_block_public_domain"      # 400: gmail.com and kin as a domain
    RFP_BLOCK_INTERNAL_DOMAIN = "rfp_block_internal_domain"  # 400: our own domain (already ignored)
    RFP_BLOCK_DUPLICATE = "rfp_block_duplicate"              # 409: already blocked
    RFP_BLOCK_CONFIRM_MISMATCH = "rfp_block_confirm_mismatch"  # 400: typed value did not match

    # ── RFP project matching (routers/rfp_emails.py match/* and routers/projects.py
    # rfp-matches/*; docs/RFP_MATCHING.md section 7). Same header contract as
    # the intake rows above: the code rides in `X-Error-Code`, `detail` is an
    # app-authored sentence.
    RFP_MATCH_NOT_ACTIONABLE = "rfp_match_not_actionable"      # 409: lost the race / wrong state
    RFP_MATCH_GC_REQUIRED = "rfp_match_gc_required"            # 400: no resolved GC and none given
    RFP_MATCH_GC_NOT_ON_PROJECT = "rfp_match_gc_not_on_project"  # 409: "already on X" but it is not
    RFP_MATCH_GC_ALREADY_SENT = "rfp_match_gc_already_sent"    # 409: unmerge after a sent/sending proposal
    RFP_MATCH_PROJECT_CLOSED = "rfp_match_project_closed"      # 409: target not open / outside the window
    RFP_MATCH_PROJECT_EXCLUDED = "rfp_match_project_excluded"  # 409: target was unmerged from this email
    RFP_MATCH_UNMERGE_REQUIRED = "rfp_match_unmerge_required"  # 409: ordinary Remove on a system-added GC

    # ── RFP project data harvest (routers/rfp_emails.py {id}/harvest;
    # docs/RFP_HARVEST.md section 5). Same header contract.
    RFP_HARVEST_ACTIVE = "rfp_harvest_active"                  # 409: a harvest job is already queued/running
    RFP_HARVEST_NOT_AVAILABLE = "rfp_harvest_not_available"    # 409: no harvester, no link, or wrong state
    RFP_HARVEST_LOCKED = "rfp_harvest_locked"                  # 503: platform logins are locked

    # ── NGEM portal invitations (routers/rfp_portal.py; docs/RFP_NGEM_PORTAL.md
    # section 5). Same header contract.
    RFP_PORTAL_NOT_ACTIONABLE = "rfp_portal_not_actionable"    # 409: lost the race / wrong status
    RFP_PORTAL_PROJECT_REQUIRED = "rfp_portal_project_required"  # 400: resolve without a usable project
    RFP_PORTAL_HARVEST_ACTIVE = "rfp_portal_harvest_active"    # 409: a harvest job is already queued/running
    RFP_PORTAL_RUN_ACTIVE = "rfp_portal_run_active"            # 409: a scan is already queued/running
    RFP_PORTAL_LOCKED = "rfp_portal_locked"                    # 503: portal logins locked / not configured
    RFP_PORTAL_NOT_AVAILABLE = "rfp_portal_not_available"      # 409: the action does not apply to the row (503 from Run now while the slice is inactive)
    RFP_PORTAL_REASON_INVALID = "rfp_portal_reason_invalid"    # 400: a reason that is too short (or missing where required)
    RFP_PORTAL_GC_REQUIRED = "rfp_portal_gc_required"          # 400: GC card without a usable GC (none picked, gone, or NDA-masked company)
    RFP_PORTAL_NOT_RESTORABLE = "rfp_portal_not_restorable"    # 409: restore on a row that is not historical / expired / withdrawn
    RFP_PORTAL_DATES_UNCHANGED = "rfp_portal_dates_unchanged"  # 400: apply-dates with nothing to apply

    # ── BuildingConnected (routers/rfp_bc.py; docs/RFP_BUILDINGCONNECTED.md
    # sections 4.3 and 6). Same header contract; the callback carries the
    # code as `reason=` on its redirect to the settings page instead.
    RFP_BC_DISCONNECTED = "rfp_bc_disconnected"                # 409: the connection died (refresh rejected); reconnect
    RFP_BC_NOT_CONNECTED = "rfp_bc_not_connected"              # 409: no connection (Run now), or the callback could not make one
    RFP_BC_STATE_INVALID = "rfp_bc_state_invalid"              # 400 / callback reason: OAuth state missing, unknown or expired
    RFP_BC_VIEW_ALL_REQUIRED = "rfp_bc_view_all_required"      # callback reason: the Autodesk user cannot see the whole Bid Board

    # ── RFP test bench (routers/rfp_testing.py; docs/RFP_TESTING.md sections
    # 2, 3 and 9). The code IS the detail on every one of these (the page
    # shows it verbatim); the activation preflight adds the Graph status to
    # `detail` after a colon.
    RFP_TESTING_FORBIDDEN = "rfp_testing_forbidden"            # 403: not a dev it_admin
    RFP_TESTING_ALREADY_ACTIVE = "rfp_testing_already_active"  # 409: a session is active
    RFP_TESTING_SESSION_ACTIVE = "rfp_testing_session_active"  # 409: cleanup asked on the active session
    RFP_TESTING_SESSION_ENDED = "rfp_testing_session_ended"    # 409: a change asked on an ended session
    RFP_TESTING_INGEST_OFF = "rfp_testing_ingest_off"          # 422: RFP_INGESTION_ENABLED is false
    RFP_TESTING_GRAPH_OFF = "rfp_testing_graph_off"            # 422: no Graph credentials
    RFP_TESTING_MAILBOX_UNREACHABLE = "rfp_testing_mailbox_unreachable"  # 422: the test mailbox did not answer 200
    RFP_TESTING_REDIRECT_INVALID = "rfp_testing_redirect_invalid"  # 422: redirect address malformed


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
    RFQ_NUDGE = "rfq_nudge"             # RFQ nudge reminder fan-out
    OUTBOUND_EMAIL = "outbound_email"   # invites + package / proposal mail
    NOTIFICATION_LOG = "notification_log"  # per-project notification-log assembly
    REPORT = "report"                   # multi-table report assembly (bid invitations)
    MODEL_STATUS = "model_status"       # forced AI provider health probe
    LLM_MONITOR = "llm_monitor"         # dev AI monitor page reads/actions
    GC_PRICING_REQUEST = "gc_pricing_request"  # late-GC price-change requests (Executive fan-out)
    RFP_INGEST = "rfp_ingest"           # dev-only RFP Ingestion sandbox page reads
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
    RateLimitScope.RFQ_NUDGE: (
        "Caps RFQ nudge reminder batches per account per minute. Each batch "
        "emails vendors on their existing RFQ threads; wait for the "
        "Retry-After window between batches."
    ),
    RateLimitScope.OUTBOUND_EMAIL: (
        "Caps branded outbound email (invites, packages, proposals) per account "
        "per hour to protect the shared mailbox's sending reputation."
    ),
    RateLimitScope.GC_PRICING_REQUEST: (
        "Caps per-GC pricing change requests per account per hour. Each request "
        "notifies and emails every Executive; wait for the Retry-After window."
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
    RateLimitScope.MODEL_STATUS: (
        "Caps forced AI-provider health checks ('Check now' in the Model status "
        "modal) per account per minute — each one reaches out to the model "
        "provider. The indicator's own background reads are never limited."
    ),
    RateLimitScope.LLM_MONITOR: (
        "Caps the dev-only AI monitor page's reads and job actions per account "
        "per minute. The summary endpoints aggregate the whole call ledger in "
        "memory, so runaway polling would be costly. Wait for the Retry-After "
        "window."
    ),
    RateLimitScope.RFP_INGEST: (
        "Caps the dev-only RFP Ingestion sandbox page's reads (run, file and "
        "page listings, signed URLs) per account per minute. The page polls "
        "every 3 seconds while a run is active, far below the budget; wait "
        "for the Retry-After window."
    ),
    RateLimitScope.DEFAULT: "Generous catch-all request budget for other routes.",
}
