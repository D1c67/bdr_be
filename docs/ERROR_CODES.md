# BDR API error codes

When the API rejects a request for a policy reason (not a random 500), it returns
a **stable string code** in the JSON `detail` field. Codes never change wording,
so a user can read one off the screen and quote it to a developer, who looks it
up here. The frontend maps these codes to friendly messages.

The canonical list lives in code: `app/core/error_codes.py`
(`ErrorCode`, `RateLimitScope`, `RATE_LIMIT_HELP`). Keep this file in sync.

---

## Rate limiting — `rate_limited` (HTTP 429)

A per-account request budget was exceeded. This is almost always a script, a
double-click, or an unusually busy session — **not** a block on the user. The
response carries two headers:

| Header | Meaning |
| --- | --- |
| `Retry-After` | Seconds to wait before retrying. The UI shows "try again in N seconds". |
| `X-RateLimit-Scope` | Which limit tripped (see table below). Quote this to support. |

### Scopes (`X-RateLimit-Scope`)

| Scope | Protects | Default budget | If a real user hits it |
| --- | --- | --- | --- |
| `estimator_api` | External estimator portal requests | 60 / min | Wait for `Retry-After`; normal use never reaches it. |
| `ai_jobs` | BOQ analysis / proposal-line generation (each spends model tokens) | 5 / min | Wait and retry, or ask IT to raise `AI_RATE_LIMIT_PER_MIN`. |
| `file_upload` | File uploads | 20 / min | Wait; consider uploading fewer files at once. |
| `file_export` | Project ZIP exports (also serialized: one build at a time) | 5 / min | Wait for `Retry-After`; an export already in progress returns this too. |
| `bulk_send` | RFQ email fan-out | 3 / min | Wait between bulk sends. |
| `rfq_nudge` | RFQ nudge reminder batches (vendor follow-up emails) | 3 / min | Wait between nudge batches. |
| `outbound_email` | Invites, estimator packages, proposal emails | 60 / hour | Protects the shared mailbox's reputation; ask IT if you need more. |
| `notification_log` | Per-project notification-log reads (each assembles the view from dozens of lookups) | 30 / min | Wait for `Retry-After`; reopening the modal normally never reaches it. |
| `report` | Bid Invitations report reads (each scans several tables across the window) | 30 / min | Wait for `Retry-After`; normal range-switching never reaches it. |
| `model_status` | Forced AI-provider health probes ("Check now" in the Model status modal) | 12 / min | Wait for `Retry-After`; the indicator's own polling is cached and never limited. |
| `llm_monitor` | Dev-only AI Monitor page reads and job actions (summary reads aggregate the whole call ledger) | 120 / min | Wait for `Retry-After`; the page's own polling stays far below the budget. |

All budgets are configurable via environment variables (see `app/core/config.py`,
the `*_rate_limit_*` settings). To lift a limit for a specific user, raise the
relevant setting or, in an incident, set `RATE_LIMIT_ENABLED=false` to disable
all rate limiting instantly.

**For developers diagnosing a user report of "rate_limited":**
1. Ask for the `X-RateLimit-Scope` header (or which action they were doing).
2. Check whether it's a genuine burst (a script/loop) or a too-tight budget.
3. Adjust the matching `*_RATE_LIMIT_*` env var, or raise it just for the
   affected flow. The in-memory counter resets on the next window / restart.

---

## Two-factor auth (HTTP 403)

| Code | Meaning | Frontend action |
| --- | --- | --- |
| `mfa_enrollment_required` | The user has no TOTP factor yet. | Send to the 2FA enrollment (QR) screen. |
| `mfa_step_up_required` | Enrolled, but this session hasn't stepped up to `aal2`. | Prompt for a 6-digit code. |

---

## Request too large (HTTP 413)

| Code | Meaning |
| --- | --- |
| `request_body_too_large` | The whole request body exceeded `MAX_REQUEST_BODY_BYTES` (global backstop). |
| (message) | A single upload exceeded `UPLOAD_MAX_BYTES`; the export bundle exceeded `EXPORT_MAX_TOTAL_BYTES`. |

---

## Other stable behaviors

- **409 with a human message** on `.../todos/{id}/nudge` is a nudge cooldown, not
  a rate limit — it is shown verbatim and has no `rate_limited` code.
- Unhandled server errors return a generic message; the real cause is in the
  server logs (never echoed to the client).
