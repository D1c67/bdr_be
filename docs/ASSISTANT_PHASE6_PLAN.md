# Assistant phase 6: per-user browser tasks

Status, 2026-10-29: personal worker1.0.4 is signed, published, selected and live-verified after automatic Windows upgrade. Repaired4510 frontend and5071 API are running. Designated owner/credential/model/budgets verified and only user_browser_read enabled for a new controlled Mater Academy test (config29); organization types remain off, shared .env unchanged. Previous reads proved password login but failed project navigation; no RFIs are yet verified.1.0.4 addresses a reproduced response-header compatibility limit and improves precise diagnostics, but its live Procore result is pending. Phase6 remains incomplete. Read the newest build-log checkpoint before another action.

## 1. Scope and decisions

Build user-principal, read-only browser tasks on the existing browser worker platform. The implemented local adapter is Procore using the existing code-fixed login routine and RFI read paths. BuildingConnected and SmartBid are unavailable until their signed worker adapters and exact ceilings are implemented and accepted. No arbitrary origin, operating-system control, file upload, business form submission, RFI capture into BDR or organization identity is available to a user task. Deterministic password login is the narrowly isolated exception to GET-only navigation. SSO and Microsoft 365 passwords are refused.

`user_browser_task` is an expensive assistant tool with the existing immutable WriteSpec approval flow. `user_browser_status` is a read tool. Neither is offered in mailbox-review mode or to Accountants/external estimators. A task approval shows the exact site, URL, objective, credential label/version, maximum cost and eight-hour expiry. URLs must be typed by the owner or independently resolved from an approved server source, never authorized by a browser/page result. Page content and browser results are untrusted and sticky-tainted.

User authority comes from the current BDR profile, assistant grant, enabled settings/profile, normal conversation and owned active credential. Browser credentials prove site authentication only. An IT Admin assistant receives no additional browser authority.

## 2. HTTP contract

All human endpoints require normal current-user authentication, no-store and live role checks. Credential mutations and help answers are human-only and excluded from the assistant shim. Errors must not echo secret-bearing Pydantic values. Owner mismatch and missing IDs have the same 404.

- `GET /assistant/browser-sites`: `{enabled, items}`. Sites carry fixed service, label, origins, available and reason.
- `GET /assistant/browser-credentials`: `{items}`. Credential metadata carries id, service, label, username, version, status, fingerprint, updated_at; never encryption fields. Configured does not mean site login or worker reach was tested.
- `PUT /assistant/browser-credentials/{service}`: write-only password enrollment/replacement; `{label, username, password, expected_version, acknowledge_personal_password:true}`. Server fixes origins and service. Replacement is compare-and-set, increments version, invalidates sessions and cancels tasks referencing the old version. Raw secret cleared from client state after any completion/close.
- `DELETE /assistant/browser-credentials/{service}?expected_version=N`: human-only revoke, invalidates sessions and tasks before secret destruction; metadata tombstone preserves generation fencing.
- `POST /assistant/browser-tasks`: sole assistant job-creation route. Approval-bound body includes service, URL, objective, credential id/version and client_request_id. Owner, principal kind, type, conversation, approval, cost and expiry are server-bound. The model cannot choose any of those authority fields.
- `GET /assistant/browser-tasks`: owner-scoped, bounded recent list. Encrypted trace pages use timestamp/id cursors.
- `GET /assistant/browser-tasks/{id}`: owner-scoped status, expiry, sanitized result and help metadata. No direct storage URLs for encrypted content.
- Owner help routes use the user-task ownership predicate and owner notifications. They cannot reuse organization helper authorization. A secret answer is write-only and consumed by the deterministic login routine.
- Any IT Admin inspection is a separate reason-audited endpoint. Existing organization admin role or is_dev never bypasses user ownership. Audit succeeds before content is returned.

Worker identity, turn, trace, help, heartbeat and completion endpoints live under the dedicated `/agent-worker/v1/jobs/{job_id}/user/` namespace. The identity suffixes are `/session`, `/login/begin` and `/login/result`. They require the current worker, boot, lease epoch, user pool, live owner authority, matching credential/version and exact permitted top-level origin. No request parameter can supply owner or credential. Organization identity routes reject user jobs before reading a shared password or cookie jar.

## 3. Persistence and transactional boundaries

Migration 0186 is the applied foundation; forward migrations 0187 (private runtime persistence) and 0188 (claims and legacy-path isolation) implement the runtime contract. All new tables are forced RLS, service-only and use invoker RPCs with fixed search paths. No user-facing direct table policies. Existing applied migrations are not rewritten.

Add principal_kind to browser_agent_types, browser_agent_workers and browser_agent_jobs, defaulting existing rows to org. User jobs also carry principal_user_id, user_credential_id, user_credential_version, assistant_approval_id, assistant_conversation_id and assistant_reservation_id. Job/type/worker principal must agree. Claim and every lease fence validate this in the database. User job admission is idempotent by owner and approval/request id. Existing organization enqueue refuses user types.

`assistant_credentials`: owner/service unique, id, fixed origins, label, username metadata and encrypted password envelope fields, version, fingerprint, status, login-attempt/lock metadata and timestamps. Separate USER_CRED_KEK; fresh data-encryption key per envelope, wrapped with authenticated owner/id/version/purpose AAD. Neither organization keys nor an environment credential fallback may be used. Credential CAS/revoke clears sessions and fences active tasks atomically.

`assistant_browser_sessions`: owner, credential id/version, exact origin, encrypted envelope, session version and expiry; unique credential/origin. Saves require the live credential version and an outstanding login attempt bound to job/epoch. Old results cannot restore cookies after replacement or revoke. Failed-login counters use atomic updates and a three-strike lock; login spacing is 600 seconds. Session reuse validates origin and credential generation before decrypting.

`assistant_browser_access`: append-only IT Admin inspection ledger with owner label, viewer, reason, resource and timestamp. Browser trace/artifact encryption metadata is recorded separately from public metadata. Never return a signed URL to ciphertext as if it were a preview; the authorized API decrypts bounded content.

Task admission reserves its full maximum model cost in the existing assistant spend system as external kind browser_task. Use the Pacific-day user/company budgets and settle terminal work with actual browser model cost, exactly once. A pending or offline task retains its reservation through eight-hour expiry. The browser global/per-job caps still apply. At most one active user task per owner is claimed, with database locking so different user types cannot evade the cap. Failures after enqueue are recoverable by durable reservation/job identifiers.

RPC families to implement and test: credential CAS/revoke; user identity begin/result/session CAS; user task enqueue; user authorization predicate; principal-aware claim/fence; terminal settlement and expiry. SQL claims lock live profile, delegation, credential and conversation authority before admitting work. Never infer authority from payload JSON.

## 4. Browser platform and worker integration

A separate Windows service account and separate enrolled worker token run the user pool. The authenticated worker database row fixes principal_kind; allowed_types only narrows it. A user worker cannot claim organization jobs, and an organization worker cannot claim user jobs even if type names or capabilities are forged. Signed release capabilities include the user type explicitly.

The worker creates a fresh context per job, injects only that owner's authenticated cookie jar and closes it on all exits. User login tools call only the user identity namespace. User tool definitions omit organization RFI capture, BDR helpers, arbitrary network fetches and arbitrary JavaScript. Secrets are registered in both worker and server redactors before any logging. No login screenshot contains a password or one-time code.

User network policy uses exact HTTPS origins and the code-fixed path ceiling. Resource, navigation, redirect, APIRequestContext, popup, websocket and service-worker paths are covered. A post-navigation blank page alone does not prevent an outbound redirect request. Until a path can guarantee destination enforcement before network access, it remains unavailable. Model page actions cannot gain the deterministic login page's POST exception.

Old `/browser-agents` lists, summary, detail, steps, turns, artifact URLs, help, history, retries and cancellation must exclude user rows, including for executives and is_dev. Adding owner endpoints without closing these existing paths is not sufficient. User help scopes include the owner (`login:u-<id>-<service>`); helper roles receive no user task content or secrets. Notices go only to the owner.

An offline task stays queued/parked for at most eight hours; expiry emits one owner bell plus email and releases unspent reservation. Retry/requeue/help cannot extend the original expiry. Offboarding or credential revoke stops queued and running jobs, denies subsequent worker turns/identity calls and invalidates sessions.

## 5. Frontend and retention

Settings provides a fixed-site credential card with write-only password, explicit personal site account explanation, no SSO/Microsoft option and clear unavailable/configuration states. Assistant task list/status is owner-scoped; help uses owner endpoint wrappers. Approval cards show destination and spend. No raw content HTML or remote image rendering.

User screenshots, steps and transcripts are encrypted and retained for 90 days. Owner legal hold suspends trace deletion. Purge removes storage and encrypted payloads while retaining required minimal audit/task accounting metadata. No user traces leak to organization history or helper lists. Credentials and cookie jars are destroyed at offboarding; historical audit ledgers retain user_label without cascading owner FKs.

## 6. Verification and release gates

Required offline checks: credential envelope AAD swapping and key separation; stale version/login result/session refusal; user/org pool claim and lease mismatch; seven-role assistant tool matrix; review-mode exclusion; immutable approval mismatch; URL binding; every organization human/worker route refusing user resources; owner-only artifacts/help; revocation during active lease; concurrent quota/cap admission; eight-hour expiry and exact-once notices/settlement; guard redirect/method/resource isolation; secret-redacted logging and validation; frontend type/lint/tests.

Dev SQL transactional checks must exercise real tables/RPCs and roll back fixtures. Independent two-worker claim/cap probes supplement sequential race interleavings. No production apply. Existing browser tests have known baseline failures; report new versus existing failures accurately.

General availability still requires: approved site/terms decisions; organization RFI worker proven and its prerequisites deployed under separate authorization; separate Windows account/service and signed release verified; KEK provisioning; real designated personal Procore login and MFA acceptance; owner-isolation screenshots and trace checks; offline/restart/revoke drills. No real credential, site login or worker connection is needed for local code verification. Designated acceptance uses staged configuration below; it cannot require live login proof before enabling the path that performs that login.

### Staged setup and designated acceptance

1. Provision a backend-only, base64-encoded 32-byte `USER_CRED_KEK` and stable `USER_CRED_KEK_ID`, distinct from organization browser encryption/fingerprint keys. Keep the key recoverable for retained encrypted records; rotation retains necessary old keys. Validate configuration without printing key material. `EXECUTION_READY` and `assistant_browser_runtime_ready()` are code/schema capability checks, not evidence of worker, key or site readiness.
2. Keep `user_browser_read.enabled=false`. Enable `ASSISTANT_BROWSER_ENABLED` in the designated acceptance environment after the assistant/browser platform prerequisites and keys are configured. Credential routes return 404 while this environment flag is false, so enrollment must follow this step. Type-disabled preview, enqueue and claim remain unavailable. This setup stage exposes human credential management but does not permit personal jobs.
3. Have the designated eligible owner save their own personal Procore password through the human settings UI with a current grant and session. Verify metadata, replacement CAS and revoke behavior. Do not place the password in an assistant conversation or command. Install and enroll the separately signed user-pool worker with a separate Windows service account, authoritative user-pool enrollment code and token; confirm its pool/capability and that organization jobs remain separate. Enrollment and credential setup alone do not prove login.
4. Configure and probe the user type's supported OpenAI model and explicit pricing. Verify the personal worker, owner, credential generation, exact page URL and approval budget. Enable the user type for the controlled acceptance window, then submit the designated owner's approved read to exercise actual login/MFA, provider spend, encrypted trace, owner isolation and interruption/revoke paths. The current type switch is shared, not an owner allowlist: use an isolated acceptance environment or otherwise ensure no other personal owners/workers are enabled for this window.
5. Record the acceptance outcomes. Disable the user type immediately if any required check fails, and disable the environment flag if credential-management access should also close. Broader rollout follows the remaining site, worker and operational gates. Existing organization platform switches need not be turned off to stage personal setup.

### Development release preparation

Signed worker release 1.0.1 is published and verified in development project
`bpidntbyvoooqvaispup`, and selected as desired version (config version 20).
All browser types remain disabled. Do not rebuild or overwrite this release.
Public release artifacts are retained under ignored `dist/phase6-worker-1.0.1/`.

Update October 28: the personal worker is installed and the owner's credential
is saved. The designated US02 project-hub URL requires worker 1.0.2 and migration
0189. The migration is applied to development; 1.0.2 is now published, verified
and selected (config 22), and the personal worker reports 1.0.2 with a fresh
heartbeat after automatic upgrade. Keep both signed releases immutable.
Before the controlled read, restart
the isolated credential UI so the owner can Replace/save the credential with
the displayed US02 origin. Existing saved credentials are not widened by the
migration. Acceptance startup now requires worker 1.0.3 after the failed first read. See the build log for
the latest artifact hash, verification and handoff.

For the completed initial installation, the Mac command was `bash scripts/prepare_phase6_windows_install.sh`, or
the script's absolute path. With the updated backend running on port 5051,
this verifies the published bundle/signature, verifies official Python 3.12.10
NuGet archive metadata/hash, publishes the prepared runtime setup and current
installer template, starts and verifies a new development tunnel, and updates
only the local worker API URL. It uses the public release key without requesting
the signing passphrase. Keep its terminal open; it prints the Windows commands.
Create a fresh **Personal tasks** enrollment code in BDR and run those commands
in elevated 64-bit Windows PowerShell. Compare the prompted release fingerprint
with the independently retained trusted fingerprint.

The helper checks both installer endpoints directly on localhost before
checking them through the tunnel. A working `/health` alone does not prove
that the running backend can serve the storage-backed installer routes.
Public startup checks use a three-minute retry window and
report the failing endpoint and HTTP status or transport error category.
Signature and content mismatches still stop immediately. Failed preparation
does not update `.env` or print Windows installation commands; any installer
templates already uploaded remain published, so rerun preparation successfully
before installing. The helper stops only the tunnel it created.

The runtime setup installs under `C:\Program Files\BDR Python 3.12.10`, with
archive, executable-signature, ownership and virtual-environment checks. The
personal installer uses `-PythonPath` to select that runtime without changing
the existing organization Python registration or service. Windows execution,
service enrollment and subsequent heartbeat remain acceptance gates; offline
tests are not proof of Windows installation. The helper does not enable any
personal feature/type or reconnect the existing organization service.

For future releases, the publisher supports `--dry-run --output-dir <directory>` for retained
signed local artifacts without uploads, and `--expected-project <ref>` refuses
another database before building/signing. Enrollment commands pass `-ApiBase`
explicitly, so a current API setting overrides an older template's embedded
tunnel address. Updating `.env` still requires the running backend to reload
its settings. A temporary tunnel must be running for Windows acceptance.

### Credential setup after Windows enrollment

The regular development backend on 5051 intentionally keeps the assistant
feature off. Do not enable ASSISTANT_ENABLED in its shared .env. After the
personal worker is approved and heartbeats are verified, use
`bash scripts/start_phase6_credential_setup.sh` in a separate Mac terminal.
This starts a temporary backend on 5071 and a scratch frontend on 4510 with
process-only assistant/personal-browser overrides and the existing vault key.
It runs without application lifespan tasks and with the LLM queue disabled;
this stage serves credential management, not assistant execution. Existing
servers, the worker tunnel and the .env file remain unchanged.

The owner opens `http://localhost:4510/settings/assistant`, signs into BDR if
needed, and saves their own Procore credentials in **Browser sign-ins**.
Saving a credential does not prove site login. Keep user_browser_read disabled
until designated read acceptance is ready. The installed personal worker still
uses the 5051 tunnel; controlled execution will require the isolated runtime
and worker routing to be prepared together. Never copy the organization vault
password into the personal vault automatically.

### Controlled read after credential setup

After validating the designated owner's exact project/RFI URL and objective,
stop only the credential-setup helper and run
`bash scripts/start_phase6_acceptance.sh`. The replacement uses the same
isolated ports and a custom lifespan limited to assistant queue types and
private browser expiry/lease maintenance. The normal queue sweeper is excluded
because it also processes unrelated job types. The launcher requires an empty
assistant queue, one designated personal worker and disabled browser types.
Claims filter by type, not owner; verify that no other personal owner/worker
or assistant test producer participates in this acceptance window.

The launcher creates a tunnel to 5071 and prints a hash-pinned personal-worker
reconnect command. Verify the new worker boot/heartbeat before enabling
user_browser_read. Its normal UI approval card remains mandatory. No existing
worker reinstall, shared .env feature change or organization-worker change is
needed. Stopping this helper stops only its own services/tunnel; forced shutdown
can leave in-flight leases requiring recovery. This is an acceptance harness,
not persistent production hosting.

## 7. Ownership for this implementation wave

Root: assistant services/vault, routes/tools, shared config, main, offboarding/maintenance integration.
Schema agent: this plan, 0186 to 0188 migrations/RPCs, migration tests and transactional SQL checks.
Worker agent: existing browser platform Python, principal-aware protocol/identity/help/visibility, worker runner and Windows service documentation/tests.
Frontend agent: credential/task/help UI, API wrappers, translations and frontend verification.

Reviewers must report any unimplemented requirement as pending rather than marking phase 6 complete. The final build log records applied schema separately from authored schema and local checks separately from live acceptance.

## 8. Runtime implementation and database contract

The earlier foundation-only checkpoint is historical. Migration 0187 adds `assistant_browser_records`, `assistant_browser_help_state`, `assistant_browser_turns` and `assistant_browser_notices`, all forced-RLS service-only tables. Browser observations, screenshots, help text and model request/response bodies are authenticated encrypted envelopes. Legacy steps, turns, artifacts and help tables reject user-job writes in database triggers. Legacy lease and heartbeat RPCs permanently reject the user pool. Migration 0188 routes authenticated user-worker claims to a separate claim function and marks the code contract ready; environment, global, profile and type switches still control availability.

New user worker fencing checks the active authenticated worker and boot, the live profile/grant, owner mutex, locked job epoch/lease/expiry, credential generation and normal conversation. It locks profile authority before the owner mutex and job, matching sensitive profile-update revocation. User claims acquire at most one owner/job per worker and never write a legacy trace. Re-enrollment codes carry an immutable server-selected pool; the client cannot convert an organization code or worker into a user pool.

Approval-bound admission now persists the frozen authority object inside the immutable payload and rechecks role, credential, conversation, model, price availability, exact code-fixed destination and maximum cost inside its transaction. It reserves the full cost ceiling in the assistant's Pacific-day budget. One active job per owner is admitted; queued/parked work expires after eight hours. Terminal transitions settle the reservation at the accumulated actual cost, or a conservative reserved cost for an uncertain provider call.

Private turn begin/finish RPCs use turn numbers starting at one, a durable token and one begun turn per job. The conservative cost/token upper bound is charged before the provider call; completion replaces it with actual usage. Repeated begun turns report thinking and do not create another paid call. A crash or unresolved turn cannot automatically replay the provider request. Live type/model, step, active-time, token and monetary caps apply.

Login attempts bind credential generation, job and lease epoch, enforce 600-second spacing and three-strike lockout, and release only that owner's credentials. MFA help binds the same unexpired attempt. One secret message per help request is consumed once, returning its encrypted envelope while clearing stored ciphertext in the same transaction. An owner/job-scoped HMAC remains for redaction. No organization helper receives these records. Login success replaces only the matching owner's encrypted cookie jar; stale results cannot restore revoked sessions. SSO and Microsoft login remain unsupported.

Owner help messages/retry can resume a parked task, while login codes require the active leased login page. Requeue preserves the original expiry and adds bounded backoff for spacing/outages. Worker restart conservatively terminates interrupted user work. Expiry and terminal notices create an owner inbox item and bell atomically; a claimed notification is handed to the existing best-effort email mirror at most once, so delivery is not guaranteed after an ambiguous transport failure.

Retention locks the owner's profile/preferences and rechecks legal hold before deleting 90-day encrypted traces. Expired cookie jars and one-time codes are destroyed independently of trace legal hold. IT Admin reads insert a reason-audit record before returning encrypted records and exclude secret messages; owner inspection history omits the private reason.

Sensitive profile updates revoke through a conditional invoker trigger; name-only updates do not invoke it. Hard deletion first runs critical backend offboarding, then ownership FKs cascade credential/session material. The GoTrue role receives no private-vault grants. The actual GoTrue-role SQL probe remains unverified because the connector refused SET ROLE; the foundation cascade check passed under its existing role.

## 9. Verification evidence and remaining activation

Foundation migration 0186 is applied on dev as version `20261027171259`; its transactional checks passed, including an isolated organization claim/fence regression, and fixtures rolled back. The current runtime SQL script is `scripts/check_assistant_browser_runtime.sql`; it supersedes the historical foundation script's deliberate runtime-unavailable assertions after 0188. Schema guard tests are `tests/test_assistant_migration_0186.py` and `tests/test_assistant_migration_0187.py`. Runtime migrations 0187 and 0188 applied to dev `bpidntbyvoooqvaispup` as versions `20261027182104` and `20261027182113`. `scripts/check_assistant_browser_runtime.sql` passed and rolled back. It verified approved admission and replay, user claim, permanent legacy trace/fence refusal, origin/login/MFA and one-use code semantics, encrypted session persistence, turn claim/token/replay/cost behavior, inspected-read audit, terminal reservation settlement/notices and legal-hold retention. Follow-up counts found zero fixture accounts/workers, personal jobs, credentials and sessions. `assistant_browser_runtime_ready()` is true as the schema capability; the user type remains disabled and the personal browser environment flag is unchanged/false. Existing global database switches are true, as they were before the test.

These SQL checks use sequential transactional interleavings. Independent concurrent two-connection quota/claim races have not been exercised live and remain acceptance evidence to collect. Fifteen schema guard tests pass across the 0186/0187 test modules; SQL/PLpgSQL parser checks and Ruff pass. No Windows, site login, model-provider call or notification email send was part of the database check.

`scripts/check_assistant_browser_concurrency.py` is an executable independent-session probe for a disposable local PostgreSQL database with migrations through 0188. It checks distinct backend IDs and observes `pg_blocking_pids` before releasing the holder transaction, then checks company-budget admission, two-worker claim exclusivity, owner active-job admission and owner daily-budget reservation races. It accepts only a numeric loopback URI named `phase6_probe_*` via `PHASE6_PROBE_DATABASE_URL`, refuses existing accounts/jobs/spend/usage, and needs optional psycopg 3. Run it with no application, maintenance process or worker attached. It commits synthetic fixtures and enabled settings to that disposable database; discard the whole database afterward, including after failure. Never use a tunnel to a hosted database. Thirteen harness tests pass in `tests/test_assistant_browser_concurrency_probe.py`; these validate orchestration and target checks only. The live concurrency probe has not yet run, so this script adds a reproducible acceptance procedure rather than concurrency evidence.

Remaining activation evidence: designated personal Procore credentials and MFA, real Windows account/service isolation and signed release, approved launch terms/sites, actual Chromium redirect/network behavior, production organization-worker prerequisites, live provider cost behavior and visual acceptance. Local loopback Chromium execution was blocked by the sandbox, so no real-browser enforcement proof is claimed. These are rollout gates; controlled setup and designated acceptance follow the staged sequence above. Personal browser execution currently remains off; existing organization platform settings are unchanged.
