# Owner overrides to the build spec (2026-10-19). These WIN over design.md wherever they conflict.

## O1. AI drives every job (replaces D5 "playbook first, LLM only on escalation")
- Every `rfi_from_email` job runs the LLM loop from the start. There is no `llm_escalation_enabled` gate and no "only on manually linked projects" gate. Remove those columns/conditions.
- The model plans and acts: it calls `ensure_login`, locates the RFI (`procore_list_rfis` when only a number hint exists), calls `capture_rfi_detail`, handles unexpected pages with browser tools, asks for help with `request_help`, consumes helper input with `attach_helper_file` / `add_helper_note`, and ends with `finish`.
- Data fidelity stays: the RFI data that BDR writes comes ONLY from the `rfi_detail_json` artifact captured by `capture_rfi_detail` (Procore REST detail fetched in the job's browser context), applied server side through `process_run(agent=True)` + `map_detail`. The model never authors RFI field values. `finish(captured)` is accepted only when a valid detail artifact exists for this job.
- The system prompt should push the model toward the structured site tools first (cheap, exact) and use page browsing only when they fail. Keep caps (steps 40, tokens 400k, $2/job).
- Deterministic helpers still exist as TOOLS the model calls (ensure_login, capture). Fixed-script shortcuts are fine inside a tool, but the job is driven by the model.

## O2. The agent logs in itself (replaces D10 "BDR-brokered httpx session")
- No server-side httpx login on behalf of the worker. Remove `procore_broker.py`'s login path and the "browser fallback, max 3/day" gate.
- `ensure_login` tool on the worker (deterministic, model never sees credentials):
  1. `POST /agent-worker/v1/identities/procore/session` returns the shared stored cookie jar from `rfp_harvest_sessions` (if any, same account). Worker injects it into the ephemeral job context and checks login with a cheap REST GET (`/rest/v1.0/companies`). If OK, done, no login.
  2. Otherwise `POST /identities/procore/login/begin`: BDR runs atomic `reserve_login` (respects the shared 600 s spacing, the 3-strike lockout, and an open `login:procore` help), and returns `{granted, email, password, retry_at}`. Password comes from the encrypted DB secret (env `PROCORE_LOGIN_PASSWORD` fallback). It is sent only in this response, over HTTPS, `Cache-Control: no-store`, never logged, never persisted on the PC, never shown to the model, never in screenshots (masked) or audit rows.
  3. Worker fills login.procore.com in the real browser, classifies the outcome (`ok`, `password_rejected`, `mfa`, `captcha`, `challenge`, `sso`, `unexpected`) and calls `POST /identities/procore/login/result`. On `ok` it posts cookies; BDR `record_login(ok=True)` + `save_cookies` so the daily sweep and RFP harvest adopt the jar. `password_rejected` uses the atomic failure RPC (shared 3-strike lockout, `_notify_lock`). `mfa/captcha/challenge/sso/unexpected` -> help request scope `login:procore` (Estimating Admins; IT Admin also notified for login scopes). An MFA code can be supplied by the helper as a secret message and consumed by the login routine only.
- Keep the encrypted secret store (`BROWSER_AGENT_SECRETS_KEY`), `procore_creds.py` with call-site replacement of `settings.procore_configured`, and the atomic `reserve_login` / failure RPCs, since the server-side daily sweep and RFP harvest still log in with the same account.

## O3. Signed auto-update: KEEP as designed (D16, Ed25519 offline key on the Mac, `min_version`, local-health rollback).

## O4. Spend caps: $25/day global, $2/job default. IT Admin can edit both caps AND reset the daily window from Settings (audited). Alerts at 50% and 100%.

## O5. Trims for v1 (cut these; leave TODOs only if they are seams)
- Cut provider data-use attestations (`browser_agent_provider_attestations`, `attestation_missing`). Keep the **Test model** probe before save (with "Save anyway" logged).
- Cut the n-gram secret tripwire. Keep exact-value redaction of known secrets (password, consumed helper secrets, worker token, cookie values) in plain, URL-encoded and base64 forms on both sides, plus the URL query redaction.
- Worker IP allowlist: optional field, not required, off by default.
- Help answerers: Estimating Admins (owner decision). IT Admin also gets login-scope notices and is the fallback when no active Estimating Admin exists. Executives can view.
- Future live view / remote control: do NOT build; keep the seam (CDP screencast) documented in docs/BROWSER_AGENTS.md "Future".

## Repo rules (binding)
- No em dashes anywhere (code, comments, docs, UI strings, i18n).
- Migrations: next is 0157, apply ONLY to the dev Supabase project "BDR", NEVER BDR_Prod. Reload PostgREST after DDL.
- Never call the sync Supabase SDK inside `async def`. Plain `def` route handlers.
- Never write to Procore. PM RFI imports stay read-only in BDR.
- Never `git checkout`/`git stash`/reset tracked files. Do not commit (the orchestrator commits).
- Match surrounding style; tests with FakeDB style, no network.
