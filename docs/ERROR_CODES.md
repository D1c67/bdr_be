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
| `file_upload` | File uploads (per-minute budget). On `POST /projects/{id}/files` the same scope also tags the in-flight cap: an upload of `LARGE_UPLOAD_BYTES` (20 MB) or more holds one of `LARGE_UPLOAD_MAX_CONCURRENT` (3) process-wide slots and the account's single slot (`LARGE_UPLOAD_MAX_CONCURRENT_PER_USER`) while it is buffered and pushed to storage. | 20 / min; 3 large in flight per process, 1 per account | Wait; consider uploading fewer files at once. A large upload refused with `Retry-After: 5` means another large upload (yours or someone else's) is still being stored. |
| `file_export` | Project ZIP exports (also serialized: one build at a time) | 5 / min | Wait for `Retry-After`; an export already in progress returns this too. |
| `bulk_send` | RFQ email fan-out | 3 / min | Wait between bulk sends. |
| `rfq_nudge` | RFQ nudge reminder batches (vendor follow-up emails) | 3 / min | Wait between nudge batches. |
| `outbound_email` | Invites, estimator packages, proposal emails | 60 / hour | Protects the shared mailbox's reputation; ask IT if you need more. |
| `gc_pricing_request` | Late-GC price-change requests (each notifies and emails every Executive) | 20 / hour | Wait for `Retry-After`; batch changes for one GC into a single request. |
| `notification_log` | Per-project notification-log reads (each assembles the view from dozens of lookups) | 30 / min | Wait for `Retry-After`; reopening the modal normally never reaches it. |
| `report` | Bid Invitations report reads (each scans several tables across the window) | 30 / min | Wait for `Retry-After`; normal range-switching never reaches it. |
| `model_status` | Forced AI-provider health probes ("Check now" in the Model status modal) | 12 / min | Wait for `Retry-After`; the indicator's own polling is cached and never limited. |
| `llm_monitor` | Dev-only AI Monitor page reads and job actions (summary reads aggregate the whole call ledger) | 120 / min | Wait for `Retry-After`; the page's own polling stays far below the budget. |
| `rfp_ingest` | Dev-only RFP Ingestion sandbox page reads (run, file and page listings, signed URLs) | 120 / min | Wait for `Retry-After`; the page polls every 3 seconds while a run is active, far below the budget. |

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
| `request_body_too_large` | The whole request body exceeded the global backstop: `MAX_REQUEST_BODY_BYTES` only for a `multipart/form-data` request to a route whose handler declares Form/File params (the upload routes), `MAX_JSON_BODY_BYTES` (16 MB) for everything else, including a multipart Content-Type sent to a JSON route. Counted on the bytes actually received, so a chunked or under-declared body is refused at the cap too. |
| (message) | A single upload exceeded `UPLOAD_MAX_BYTES` (450 MB; the external estimator's cap on `POST /projects/{id}/files` is the lower `ESTIMATOR_UPLOAD_MAX_BYTES`, 200 MB); the export bundle exceeded `EXPORT_MAX_TOTAL_BYTES`. |

---

## RFP email intake (`/rfp-emails`)

Every policy refusal on this router carries its stable code in an
**`X-Error-Code`** response header, so the frontend can branch on the code while
still showing the sentence the backend wrote. `detail` holds that sentence,
except for the locked-rule 403, where the code itself IS the detail (the
`mfa_*` precedent) because there is nothing user-specific to say.

| Code | HTTP | Meaning | Frontend action |
| --- | --- | --- | --- |
| `rfp_rule_locked_it_admin_only` | 403 | A locked platform rule was added or deleted by someone other than the IT Admin. | "Locked rules can only be changed by the IT Admin." Hide/disable the locked checkbox and the remove button for other roles. |
| `rfp_rule_invalid` | 400 | The rule value is not a valid email address (kind `address`) or bare hostname (kind `domain`). `detail` explains which. | Show `detail` under the value field. |
| `rfp_rule_public_domain` | 400 | A `domain` rule named a public mailbox provider (gmail.com, outlook.com, yahoo.com …). Trusting one would authorize every account there. | Show `detail`; offer to switch the form to kind `address` with the same value. |
| `rfp_rule_duplicate` | 409 | That (kind, value) rule already exists. | Show `detail`; highlight the existing row. |
| `rfp_email_not_actionable` | 409 | The review / continue / dismiss action lost its race: the message already left the state that action belongs to (another reviewer, or the sweep). | Show `detail` and refresh the tab. |

A GC contact saved at a public mailbox provider is **not** refused. `POST
/gc-contacts` returns the contact with an extra `rfp_notice` string (null for
every other contact) explaining that RFP ingestion will match that contact by
full email address only, so each colleague who might send bid invitations needs
their own contact row.

### RFP project matching (`/rfp-emails/{id}/match/*`, `/projects/{id}/rfp-matches/*`)

Same header contract as the intake table above: the code rides in
`X-Error-Code` and `detail` holds an app-authored sentence. Design record:
`docs/RFP_MATCHING.md`, section 7.

| Code | HTTP | Meaning | Frontend action |
| --- | --- | --- | --- |
| `rfp_match_not_actionable` | 409 | The merge, "already on project", not-a-match, set-GC, reopen, unmerge or acknowledge action lost its race: the email or the match row already left the state that action belongs to (another reviewer, the sweep, or an unmerge that finished first). Also `dismiss` on a `review_match` row (the only rejection exit there is "Not a match"). | Show `detail` and refresh the tab or modal. |
| `rfp_match_gc_required` | 400 | Merge or "already on project" was asked for on an email whose GC is not resolved and no `gc_id` was sent. | Open the GC picker: set the GC first. |
| `rfp_match_gc_not_on_project` | 409 | "Already on project" named a project the resolved GC is not on. | Show `detail`; offer "Merge into this project" instead. |
| `rfp_match_gc_already_sent` | 409 | Unmerge refused: a proposal to that GC is sent or sending, so the GC stays on the project. | Show `detail`; disable Unmerge for that row. |
| `rfp_match_project_closed` | 409 | The target project is abandoned, declined, PM-only or CP-only, or its bid date is outside the candidate window. | Show `detail`; the reviewer picks another project. |
| `rfp_match_project_excluded` | 409 | The target project was unmerged from this email before; it is never offered again. | Show `detail`; the "Unmerged from" line lists it. |
| `rfp_match_unmerge_required` | 409 | `DELETE /projects/{id}/gcs/{gc_id}` on a GC the matcher added. Only Unmerge (Merged by System modal; Estimating Admin, Executive, IT Admin) detaches it, so the reversal is recorded with who and why. | Open the Merged by System modal at that GC. |
| `rfp_harvest_active` | 409 | `POST /rfp-emails/{id}/harvest` while a harvest job for that email is already queued or running. | Show `detail`; keep polling the detail. |
| `rfp_harvest_not_available` | 409 | `POST /rfp-emails/{id}/harvest` on an email whose method has no harvester, whose body carries no platform link, or whose status is not one a harvest may run from. | Show `detail`; hide the button. |
| `rfp_harvest_locked` | 503 | Platform logins are locked after repeated failures (or credentials are not configured); `detail` says until when. | Show `detail`; disable the button until then. |

### NGEM portal invitations (`/rfp-portal/*`)

Same header contract: the code rides in `X-Error-Code` and `detail` holds an
app-authored sentence. Design record: `docs/RFP_NGEM_PORTAL.md`, section 5.

| Code | HTTP | Meaning | Frontend action |
| --- | --- | --- | --- |
| `rfp_portal_not_actionable` | 409 | The resolve, "not a match", reopen, ignore or un-ignore action lost its race: the invitation is not (or no longer) at the status that action belongs to (another reviewer, or the sweep moved it first). Also `POST /rfp-portal/invitations/{id}/harvest` on a BuildingConnected row: its invitations carry no documents to harvest, so no job is queued. | Show `detail` and refresh the portal tab or modal; hide Harvest on BuildingConnected rows. |
| `rfp_portal_project_required` | 400 | Resolve was asked for without a usable `project_id`: missing, malformed, unknown, excluded from this invitation, or outside the candidate window and not one of the stored candidates. | Open the project picker; pick one of the candidates. |
| `rfp_portal_harvest_active` | 409 | `POST /rfp-portal/invitations/{id}/harvest` while a harvest job for that invitation is already queued or running. | Show `detail`; keep polling the detail. |
| `rfp_portal_run_active` | 409 | `POST /rfp-portal/ngem/runs` or `POST /rfp-portal/buildingconnected/run` while a scan is already queued or running. | Show `detail`; keep polling the portal's status route. |
| `rfp_portal_gc_required` | 400 | `POST /rfp-portal/invitations/{id}/gc` (or `/projects/{id}/gc-confirm`) with decision `same` on a row that has no provisional GC, `pick` without `gc_id`, `create` without the create object, or an unknown GC id. | Show `detail`; let the user pick or add a GC. |
| `rfp_portal_not_restorable` | 409 | `POST /rfp-portal/invitations/{id}/restore` on a row that is not parked (historical, expired, withdrawn), or the restore lost its race. | Show `detail` and refresh the BuildingConnected tab. |
| `rfp_portal_dates_unchanged` | 400 | `POST /rfp-portal/invitations/{id}/apply-dates` where none of the ticked fields carries a value that differs from the project. | Show `detail`; reload the project. |
| `rfp_bc_disconnected` | 409 | Reserved: a BuildingConnected action while the connection was dropped mid-request (the refresh token was refused). | Show `detail`; send an IT Admin or Executive to reconnect on Settings. |
| `rfp_bc_not_connected` | 409 | `POST /rfp-portal/buildingconnected/run` (and the OAuth callback's error redirect `?bc=error&reason=rfp_bc_not_connected`) while no BuildingConnected connection exists or the token exchange failed. | Show `detail`; offer Connect to IT Admins and Executives. |
| `rfp_bc_state_invalid` | 302 | The OAuth callback arrived with an unknown, used or expired `state` (redirects to Settings with `?bc=error&reason=rfp_bc_state_invalid`; a missing state is a plain 400). | Show the sentence; offer Connect again. |
| `rfp_bc_view_all_required` | 302 | The Autodesk user who signed in cannot see the whole Bid Board (`bidBoardPermissions.viewAll` false); nothing was stored. | Show the sentence; a user with "view all" must connect. |
| `rfp_portal_locked` | 503 | Portal logins are locked after repeated failures (or the account is not configured); `detail` says until when. | Show `detail`; disable Run now and Harvest again until then. |
| `rfp_portal_not_available` | 409 | Harvest was asked for on an invitation whose status is not one a harvest may run from (`done` or `harvest`). | Show `detail`; hide the button. |
| `rfp_portal_not_available` | 503 | `POST /rfp-portal/ngem/runs` while the slice is inactive (`rfp_ngem_active` false: the job queue that runs the scan is off, or the account is not configured); nothing was inserted. | Show `detail`; disable Run now. |
| `rfp_portal_reason_invalid` | 400 | The optional reason on ignore or reopen was given but is shorter than 3 characters (or a required reason is missing). The row did not move. | Show `detail` next to the reason field; do not reload the tab. |

### RFP test bench (`/rfp-testing`, docs/RFP_TESTING.md sections 2, 3 and 9)

Dev only. While `RFP_TESTING_ENABLED` is false every route 404s with the bare
"Not Found" body. The code is the `detail` on every row below (the page shows
it verbatim); the mailbox preflight appends the Graph status after a colon.

| Code | HTTP | Meaning | Frontend action |
| --- | --- | --- | --- |
| `rfp_testing_forbidden` | 403 | The caller is not a dev account with the `it_admin` role. | Render the "not available" block. |
| `rfp_testing_already_active` | 409 | Activation while a session is active (the partial unique index turns a race into this). | Refresh the state; show the active session. |
| `rfp_testing_session_active` | 409 | Cleanup asked on the active session. | Deactivate first. |
| `rfp_testing_session_ended` | 409 | A change (auto-create) asked on an ended session. | Refresh the state. |
| `rfp_testing_ingest_off` | 422 | Activation while `RFP_INGESTION_ENABLED` is false. | Show the code. |
| `rfp_testing_graph_off` | 422 | Activation without Graph credentials (`MS_CLIENT_ID` empty). | Show the code. |
| `rfp_testing_mailbox_unreachable` | 422 | `GET /users/{mailbox}/mailFolders/inbox` did not answer 200; `detail` carries the status (404: the mailbox does not exist in the tenant; 403: the app registration's access policy does not cover it). | Show `detail`. |
| `rfp_testing_redirect_invalid` | 422 | The redirect address is malformed. | Show the code. |

---

## Other stable behaviors

- **409 with a human message** on `.../todos/{id}/nudge` is a nudge cooldown, not
  a rate limit — it is shown verbatim and has no `rate_limited` code.
- Unhandled server errors return a generic message; the real cause is in the
  server logs (never echoed to the client).
