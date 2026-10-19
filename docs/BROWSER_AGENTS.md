# Browser agents: operator guide

Browser agents let BDR do web work that has no API, starting with one job: when
a GC's Procore emails G3 about an RFI on a linked project, an agent opens that
RFI in a real browser and BDR imports it into the project's RFI log. A Windows
PC in the office runs the browser. BDR runs everything else.

The design is in `docs/browser_agents/SPEC.md`, with the owner's decisions in
`docs/browser_agents/SPEC_OVERRIDES.md` (those win). The exact web app routes
are in `docs/browser_agents/HUMAN_API.md`. The worker contract is
`app/services/browser_agents/protocol.py`.

## 1. Architecture

| Piece | Where | What it does |
|---|---|---|
| Worker | A Windows PC (`agent_worker/`), service "BDR Agent" | Runs headless Chromium. Each job gets a fresh browser context, closed when the job ends. It only runs tools. It holds no provider keys and stores no cookies or passwords. |
| Worker API | `/agent-worker/v1` (`routers/agent_worker*.py`) | Enrollment, hello, config, heartbeat, claim, steps, artifacts, turns, login, help, capture, finish. Auth is a per-worker hashed token (`bdrw_...`), never a user login. |
| Queue | `browser_agent_jobs` and the 0157 RPCs | Leases with an epoch and the worker's boot id, so a stale or duplicate process is fenced out. Per-type concurrency is exact. |
| Model proxy | `POST /jobs/{id}/turns` (`services/browser_agents/turns.py`) | BDR calls the AI model for every step. Provider keys stay on Railway. Every request and response is scrubbed and stored. |
| Sweeper | `queue.polling_loop` (every 20 s, both uvicorn workers) | Reclaims lost leases, parks held jobs, reconciles applies, expires, cancels, spend alerts, worker offline alerts, and the help, login and contribution steps. Every write is compare-and-set. |
| Human API | `/browser-agents` (`routers/browser_agents.py`) | The Agents pages, the help chat, Settings > Browser agents. |

**The RFI path (O1).** Every `rfi_from_email` job is driven by the model. It
logs in, finds the RFI and calls `capture_rfi_detail`, which saves Procore's
REST detail JSON as an artifact. The model never writes RFI data. BDR checks
the capture (right project, right RFI, fresh, still linked), then applies it
through the existing PM RFI sync (`trigger='agent'`). The job stays in
`applying` until the sweeper reads the sync run's result. Agent-written rows
are provisional (`rfis.import_method='agent'`); the next daily sync re-reads
them from Procore and overwrites them (`import_method='web_session'`).

**Never writes to Procore.** The worker allows only GET, HEAD and OPTIONS
toward Procore, inside a per-type host and path ceiling written in code.
Settings can narrow the hosts but can never widen them.

**Login (O2).** The agent logs in itself. It first asks BDR for the shared
stored cookie jar (`identities/procore/session`). Only when there is none, or
it is logged out, does it ask for a login (`login/begin`). BDR reserves the
shared login slot (the same 10 minute spacing and 3-strike lockout the daily
sync and the RFP harvest use) and sends the email and password in that one
response. The worker types them into login.procore.com and reports the
outcome. A good login saves the jar for every Procore client in BDR.

## 2. Turning it on

1. Migrations 0157 and 0158 must be on the database.
2. Railway env:
   - `BROWSER_AGENTS_ENABLED=true`
   - `BROWSER_AGENT_SECRETS_KEY` and `BROWSER_AGENT_FINGERPRINT_KEY`: two different values, each base64 of 32 random bytes (`python -c "import os,base64;print(base64.b64encode(os.urandom(32)).decode())"`).
   - `BROWSER_AGENT_RELEASE_PUBKEY`: printed by the keygen (section 7).
   - `BROWSER_AGENT_API_BASE_URL`: the public https address of the API (used in the install command).
   - `TRUST_FORWARDED_FOR=true` in production.
   - The PM RFI sync must be on (`PM_ENABLED`, `PM_RFI_SYNC_ENABLED`), plus `PROCORE_LOGIN_EMAIL`.
   - Provider keys as needed: `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `GOOGLE_API_KEY`, `XAI_API_KEY`, `META_API_KEY`.
3. Settings > Browser agents (IT Admin or a dev account):
   - set the Procore password (stored encrypted, write only);
   - pick each type's provider and model, press **Test model**, set prices, enable it;
   - add the worker (section 6) and approve it;
   - press **Run self-test**;
   - finally switch the global kill switch on.

## 3. Settings

All settings live in the database and reach the PC within about 15 seconds
(the heartbeat carries `config_version`). Nothing is edited on the PC.

| Setting | Default | Notes |
|---|---|---|
| Kill switch (`enabled`) | off | Off: no claims; running model loops answer `stop: disabled`. |
| Daily cap (`daily_cost_cap_usd`) | $25 | Model spend since the later of midnight Pacific and the last reset. Bell at 50%, bell and email at 100%. |
| Per-job cap (`per_job_cost_cap_usd`) | $2 | Also per-type step (40), token (400k) and active time caps. |
| Reset daily window | | IT Admin, audited, re-arms both alerts (O4). |
| Help hold | 900 s | How long a job keeps its page open waiting for an answer before it parks. |
| Help reminder | 120 min | Up to 3 reminders while unclaimed. |
| Claim lapse | 30 min | "I'm on it" lapses without helper activity; a reminder goes out again. |
| Help expiry | 48 h | Waiting jobs then fail `help_timeout`. |
| Worker offline alert / email | 60 min / 4 h | Only while queued jobs wait. |
| Job TTL | 168 h | A queued job not run by then expires. |
| Desired worker version | none | Must be a published release, not below any release's `min_version`. |

Per type: enabled, provider, model, prices, max concurrency, attempts (3),
interruptions (20), steps, tokens, active seconds, host narrowing, priority.
Changing the provider or model needs a passing **Test model** for that pair
within 10 minutes, or **Save anyway** (logged with the change). An enabled
type always has a provider, a model and both prices. `rfi_from_email` cannot
be enabled while the PM RFI sync is off.

Per worker: name, drain, max slots (1 to 8), allowed types, optional IP
allowlist (empty means any; O5), approve, revoke.

## 4. Help flow

An agent that cannot continue asks a person. One request per real problem:

| Reasons | Scope | Shared by |
|---|---|---|
| login_failed, login_locked, mfa, captcha, sso, logged_out | `login:procore` | every job that logs in |
| access_denied, not_found | `link:<project>` | jobs of that project |
| everything else | `job:<id>` | that job |

- **Who is told.** Estimating Admins get a bell and an email once per request
  (`browser_agent.help_needed`). Login requests also go to IT Admin. If there
  is no active Estimating Admin, IT Admin gets it instead. The text is a fixed
  template per reason; it never quotes the agent, the web page or a screenshot.
  Executives can view everything but cannot answer.
- **The help page** (`/agents/help/{id}`) shows the title, the agent's own
  message (untrusted: shown escaped and collapsed), the masked screenshot, the
  waiting jobs and the chat.
- **Messages.** Idempotent per send (`client_msg_id`). A plaintext message that
  contains the Procore password, or is just a 4 to 10 digit code on a login
  request, is refused (`looks_like_secret`).
- **Codes.** On a login request the helper can tick "This is a code". BDR
  encrypts the code before it is stored. Only the job holding the open login
  page can use it, once, during a login; then the ciphertext is erased.
  Unused codes are erased after 30 minutes. The code's HMAC is kept 7 days so
  a leak back through the browser is caught.
- **Files.** Up to 150 MB, checked by content: pdf, png, jpg, docx, xlsx and
  txt may be marked "OK to attach to the RFI"; zip and dwg stay in the chat
  only. `services/browser_agents/scan.py` is the scanner hook (no-op in v1).
  Files and notes reach the RFI only when the agent cites them
  (`/contributions`). They apply when the RFI exists in BDR, now or after the
  next sync imports it. No placeholder RFIs are created.
- **Buttons.**
  - **Retry now**: resolves the request and requeues every waiting job ahead of the queue (uncharged). On a `cap_reached` request it also resets the job's model counters, once for a helper; after that only IT Admin.
  - **Skip** (the daily sync will catch it): waiters are canceled `skipped_by_helper`.
  - **Give up**: waiters fail `help_declined`.
  - After Skip or Give up on a `link:` request, new requests for that link are suppressed for 24 hours (the job fails `access_denied_suppressed`), and the link shows the reason.
- **Parked jobs.** A job whose hold runs out parks (its browser closes). A
  helper's message requeues it; on its next attempt the model sees the
  answer. A successful Procore login resolves the login request and requeues
  its waiters.
- **Expiry.** `job:` and `link:` requests expire after the configured hours.
  A `login:` request stays open while Procore logins are locked or the last
  login needed a code or a robot check; IT Admin gets one email instead.
- **Limit.** A real captcha has no remote fix until live view exists: Retry
  later, Skip, or Give up.

## 5. Audit

- **Step ledger** (`browser_agent_steps`): every worker, model, BDR, helper and
  admin action per job, numbered per job. Append-only; a trigger refuses
  updates and deletes. The one exception is a break-glass redaction of the
  payload, stamped with who and when.
- **Model turns** (`browser_agent_turns`): the action, tokens and cost. The
  scrubbed request and response are stored as `llm_request`/`llm_response`
  artifacts; only IT Admin can open those.
- **Settings change log** (`browser_agent_settings_changes`): one row per
  changed field, with old and new values, the Test model result or
  "Save anyway", and who. Append-only. Secrets appear only as
  `{set, fingerprint}`.
- **audit_log** actions: `browser_agent.settings_update`, `.secret_set`,
  `.secret_cleared`, `.enrollment_created`, `.worker_enrolled`,
  `.worker_approved`, `.worker_revoked`, `.help_reply`, `.help_file`,
  `.help_claim`, `.job_cancel`, `.job_retry`, `.spend_reset`, `.canary`,
  `.stop_running`.
- Login attempts (`browser_agent_login_attempts`) record every jar handout and
  every released password, never the password itself.

## 6. Failure and shutdown behavior

- **PC loses power or restarts.** The new process calls `/hello` with a new
  boot id. Every lease the old process held is released uncharged: running
  jobs are requeued, jobs waiting for help are parked. If the PC stays off,
  the sweeper reclaims leases once the lease (90 s) has expired and the
  worker has been silent for a lease, never in the first 105 s after BDR
  itself starts.
- **BDR is down.** The worker keeps going and spools steps locally. A live
  lease is not reclaimed for a BDR outage alone. After 15 minutes without
  BDR the worker aborts its jobs; they resume later.
- **Limits.** More than 20 interruptions fail a job `interrupted_repeatedly`.
  Crashes are charged; past 3 attempts the job fails `crashed_repeatedly`.
  IT Admin gets one `browser_agent.job_failed` bell per failure.
- **Provider outage.** Jobs requeue uncharged with backoff (5, 15, then 60
  min). A key or model error disables that type and bells IT Admin.
- **Stop running jobs** (Settings) requests a cancel on every running and
  waiting job, delivered by the next heartbeat. **Cancel** on a queued job
  cancels it at once.
- **Revoke** clears the worker's token at once. Its leases lapse and are
  requeued. Re-enrolling needs one installer run on the PC.
- **A worker restarting more than 6 times in an hour** is set to draining and
  IT Admin is told (`worker_suspicious`).

## 7. Releases (O3)

Releases are signed with an Ed25519 key that never leaves the owner's Mac.

1. **Once:** generate the key.
   ```
   uv run python scripts/agent_release_keygen.py --public agent_worker/install/release_key.pub
   ```
   Set the printed `BROWSER_AGENT_RELEASE_PUBKEY` in Railway, and save the
   fingerprint in the password manager.
2. **Each release:** bump `agent_worker/bdr_agent/__init__.py` `__version__`, then:
   ```
   uv run python scripts/publish_agent_worker_release.py --version 1.0.1 --min-version 1.0.0 \
       --key ~/.bdr/agent_release_ed25519.pem --installer-api https://<bdr-api>
   ```
   This builds the zip (`agent_worker/tools/build_release_bundle.py`), signs
   `{version, sha256, min_version, created_at}`, checks the signature against
   `BROWSER_AGENT_RELEASE_PUBKEY`, uploads to the private `browser-agent`
   bucket and registers the `browser_agent_worker_releases` row. With
   `--installer-api` it also uploads the installer template served at
   `GET /agent-worker/v1/installer.ps1`. Use `--dry-run` to sign without
   uploading. It refuses BDR_Prod unless `--allow-prod` is given with the
   owner's approval.
3. In Settings, pick the **desired worker version**. Workers drain, install
   side by side, verify the signature and sha256, and restart into it. A
   version that fails its local health check is rolled back and reported in
   the worker's `bad_versions`.

**Add a worker.** Settings > Add worker shows a single-use 20-character code
(30 min), the install command and the key fingerprint. On the PC, as
Administrator, run the command within 15 minutes, compare the fingerprint
before typing `yes`, enter the code, then **Approve** the worker in Settings.

## 8. Future: live view and remote control (not built)

A helper may one day need to see and drive the agent's browser, for example
for a captcha. The seams are in place so this can be added without changing
the protocol's shape:

- **Screencast.** The worker's `seams/screencast.py` is where Chromium's CDP
  `Page.startScreencast` would stream frames.
- **Control channel.** The heartbeat response carries `controls` (always `[]`
  in v1). `browser_agent_workers.control_capabilities` would advertise
  `{"screencast": true}`.
- **Reserved route.** `/agent-worker/v1/jobs/{id}/live` is reserved and
  returns 404 in v1.
- **Job mode.** `browser_agent_jobs.control_mode` is `agent` only. A later
  migration would allow `operator`, and the model loop would pause while a
  person drives.
- **The rule for any remote input.** It arrives as `helper`-actor tool calls
  through the worker's executor. So it is fenced, checked against the same
  read-only ceiling, and written to the step ledger like every other action.
