# BDR Browser Agent Platform: Final Build Spec (migration 0157, dev DB "BDR" only)

Status: this is a design only. No files were changed. I checked every code fact below against `/Users/thomasmoore/Desktop/BDR/bdr_be` on 2026-10-19. The facts that drove revisions:

**Attachment downloads**
- `_download_to_documents` (`pm_rfi_sync.py` ~1117) turns a storage 403 into `reason="refused"` and returns `None`. `sync_attachments` then `continue`s with `complete` still True, and `apply_detail` marks the row complete.
- `ProcoreSession.__init__(config, store, ...)` requires `store` (`procore_client.py:641`).

**Run claiming and status**
- `claim_next` (556) abandons a run itself once `attempts >= max_attempts`, and calls `_update_link_status(..., "failed")`.
- `process_run` (1614) always calls `_update_link_status` on success and on failure.
- `_progress` (1217): a status never moves backwards. `_finished` (1243) means closed and complete.

**Login**
- `ensure_session` (977) reads `last_login_attempt_at`, then runs `_login()`, and stamps the time only inside `record_login`.
- `_SessionStore.record_login` (`rfp_harvest.py:1088`) is read-modify-write.
- `save_cookies` (1072) resets the failure count and the lock.

**Email scan**
- `scan_emails` (1889) moves `new_checkpoint` past every row before it handles any of them, and swallows per-email exceptions.

**Notifications**
- `dismiss_notifications` (`notifications.py:117`) needs a project or rfq scope. It already supports `metadata_eq`.
- `_deep_link` (`notification_email.py:231`) enters `if project_id:` first.

**Config call sites**
- `procore_configured` is read in 9 places:
  - `main.py:165`
  - `routers/pm_rfi_sync.py:54`
  - `pm_rfi_sync.py:1632`
  - `pm_rfi_sync.py:2108`
  - `rfp_harvest.py:928`, `1018`, `1246`, `1357`, `2458`

**Other code facts**
- The Dockerfile runs `uvicorn --workers 2` with no proxy-header flags.
- `_read_capped` is `async def` (`files.py:150`).
- `pyproject.toml` has `anthropic>=0.40` and `openai>=1.50`. `cryptography` arrives only through `pyjwt[crypto]`.
- `ingested_emails` has `subject` and `message_at`, and no auth-results header column.
- `pm_rfi_sync_runs_email_uidx` is partial: `where trigger='email' and source_email_id is not null`.

---

## 0. Decision log (final)

| # | Decision | Final choice | Why |
|---|---|---|---|
| D1 | Where LLM calls run | **BDR proxies every call and owns the transcript.** The worker only runs tools. | Provider keys never reach the PC. Spending is fenced by the lease. There is one audit and cost choke point, and a reclaimed job resumes from BDR state. |
| D2 | Turn shape | One tool call per turn, parallel calls off. The server numbers turns within an epoch. A stale `thinking` turn is taken over by compare-and-set, and the takeover is billed as an estimate. | The audit stays deterministic, a redeploy cannot strand a turn, and the "never double-bill" claim is replaced by explicit estimated billing. |
| D3 | Provider adapters | OpenAI SDK for OpenAI, xAI and Meta (Meta labelled experimental). Native `anthropic`. Native `google-genai`, pinned. No LiteLLM, no browser-use. | Supply chain and opacity. |
| D4 | Queue | New `browser_agent_jobs` table. SECURITY INVOKER claim RPC with `SKIP LOCKED`, a per-type advisory lock and integer fencing `lease_epoch`. Every worker RPC checks `boot_id`. | Exact per-type concurrency. Zombie and duplicate processes are fenced out. |
| D5 | How the RFI is obtained | Deterministic playbook first: REST detail JSON fetched inside the job's browser context. The LLM runs only on escalation and never writes imported data. | Parity with `map_detail`, about $0 per job, and injection-proof data. Owner sign-off required (section 13). |
| D6 | Who writes the RFI log | Only BDR. A capture becomes a `pm_rfi_sync_runs` row with `trigger='agent'`, applied by `claim_next`/`process_run(agent=True)` through `AgentCaptureSession`. The job sits in `applying` until the sweeper reconciles it from the run's status. | Reuses the busy-project rule, collisions and notifications, and is durable across process death. |
| D7 | Attachment files | BDR downloads them with the cookie-less `ProcoreSession.download` through the shim. A storage 403/404 maps to `ProcoreTransient`. Signed URLs are checked against the seed `projectId`/`companyId` at download time. | No second file pipeline. An expired link leaves the row incomplete, and the sweep refetches it. |
| D8 | Provisional agent writes | An agent apply never sets `external_updated_at`, so `_finished` is never true for agent-written data. The next daily sweep re-reads and overwrites it. A stale guard skips captures that are older than local data. | A forged or stale capture cannot become permanent, and lost files self-heal. |
| D9 | Backfill | Agent runs are target-only. When the link is not backfilled, BDR also queues a web-session `manual` run, which joins any open full run. | Volume stays on the web session, and email-linked projects still backfill at once. |
| D10 | Procore login | **BDR-brokered session.** BDR runs the existing httpx `ensure_session`, with the password read from the encrypted DB secret, and hands the worker a cookie jar for the job's context. A browser login on the PC (password released) is only a fallback after a broker failure at `challenge`/`login_page`/`redirect`, and it is capped, alerted and gated. | The password stays on Railway in the normal case. Backend, RFP harvest and agent share one jar and one lock, so they stop logging each other out. |
| D11 | Browser contexts | One Chromium process per worker and **an ephemeral context per job** with brokered cookies injected. No persistent profiles. | No profile corruption after power loss, no `Singleton*` cleanup, no cookies at rest on the PC, and no cross-job contamination. |
| D12 | Never write to Procore | Hard-coded per-type ceiling in worker code: navigation path regexes, resource hosts, and the REST path allowlist. Settings can only narrow it. A network guard allows only GET/HEAD/OPTIONS. Login POSTs are allowed only from the login page object, only to login-chain paths. URLs with token-like query parameters are refused. WebSockets are closed. | A fooled model, or a compromised IT Admin session, cannot widen reach. |
| D13 | Audit | Append-only ledger enforced by triggers. The only UPDATE allowed is a break-glass redaction of payload columns. Each step row carries hashes of the LLM request and response and of the artifact sha256. Actor ids are plain uuids plus labels. **No hash chain or anchors in v1.** | Meets "fully auditable" without breaking user deletion. Secrets that slip through can be redacted. |
| D14 | Help dedupe | Scopes `login:<identity>`, `link:<project_id>` (access denied / not found) and `job:<id>`. The claim lapses after 30 min. Notices fall back to IT Admin when no Estimating Admin exists. | One notice per real problem, and no orphaned requests. |
| D15 | Helper data into the RFI | Files and notes go to the RFI only through `browser_agent_rfi_contributions` keyed `(project_id, platform, external_rfi_id)`. They apply to the row when it exists, or later when the sweep first imports it. **No helper-authored placeholder rows.** zip and dwg files are never attachable. | Meets "the agent can add files and info to the RFI" without fake Procore data. |
| D16 | Hosting | WinSW service under a standard local user `bdr-agent`, headless Chromium, a stable launcher, side-by-side versions, Ed25519-signed releases from an offline key, a signed `min_version` floor, and local-health rollback plus `bad_versions`. | Survives power loss with no logon. BDR cannot push unsigned code. A bad release never needs a PC visit. |
| D17 | Worker code location | `bdr_be/agent_worker/` with its own `pyproject.toml`. The shared protocol models are vendored, with a drift test. | One repo and a trivial contract test. |
| D18 | Old email path | Pass A detection and auto-link stay. Enqueue routes to the agent when it is active, otherwise to the legacy `enqueue_run('email')`. The legacy branch is deleted 30 clean days after the prod release. | Rollback switch. |
| D19 | Claim-time short-circuit | `prepare(job)` at claim: no link means `no_link`. The target row is `_finished`, or its `external_synced_at >= email.message_at`, means `already_current`. | Common after the PC was off. |
| D20 | PM-app agnostic | A `PmAppAdapter` registry keyed by `pm_external_links.platform`, on both sides. Secrets and login locks are keyed by identity. Procore is the only adapter in v1. | A second PM app is an adapter, not a rewrite. |
| D21 | Daily cost cap | Enforced in `/turns` only. Claims stay open, and free playbook jobs keep running. | One runaway job must not halt the free path. |

---

## 1. Where LLM calls run: BDR proxies (D1, D2, D3)

The worker calls `POST /agent-worker/v1/jobs/{id}/turns`. BDR handles it in `services/browser_agents/turns.py`, a plain `def`, in this order:

1. **Fence.** `(job_id, lease_owner = worker, lease_epoch, lease_boot_id = worker.boot_id, status='running')`. A mismatch returns 409 `lease_lost` or `stale_boot`.

2. **Idempotency.** The request carries `prev_turn_no`. BDR looks up turn `prev_turn_no + 1` for the current epoch:

   | State of that turn | BDR does |
   |---|---|
   | `issued` | Returns the stored action. |
   | `thinking`, `thinking_until > now()` | Returns `202 {retry_after: 5}`. |
   | `thinking`, `thinking_until <= now()` | Takes the turn over (`update ... set thinking_owner=me, thinking_until=now()+120s where thinking_until <= now()`). Sets `billing_unknown=true` and charges an estimate (`max_output_tokens * price_out + request_tokens_estimate * price_in`) to the job and the daily total. Writes an `llm.takeover` step. |
   | Absent | Inserts `thinking` with `thinking_owner = WORKER_ID`, `thinking_until = now()+120s`. |

3. **Caps** (section 4.4). Global daily spend, then the job's steps, tokens, cost and active seconds. A hit returns `200 {stop:true, reason:'cap_reached'}` and the worker opens `cap_reached` help.

4. **Scrub before anything persists.** `core/redact.py` plus:
   - the live-secret tripwire: HMAC of the current Procore password, every unexpired helper secret, and every consumed helper secret of this job;
   - the n-gram match over observation text and `tool_results`.

   A hit fails the job with `secret_leak_blocked` and persists only `{"redacted":true}` in its place.

5. **Store the observation** (redacted) in the ledger (`tool.result`) and on the turn row.

6. **Build the context** from `browser_agent_turns` of all epochs (abandoned turns are summarized, not replayed):
   - versioned system prompt (its sha256 goes into `config_snapshot`);
   - tool schemas for the type;
   - seed facts;
   - prior turns;
   - the last 2 screenshots as images. Older screenshots become `[screenshot <artifact id> omitted]`, and `read_page` output older than 3 turns is cut to 1 KB.

   Untrusted page text goes in `<untrusted_page_content url="...">` with zero-width and bidi characters stripped. Helper text goes in `<helper_message from="Name (Role)">`. A resumed attempt gets one fixed system note: "Attempt N: the previous attempt was interrupted after step S at URL U; the browser restarted on a blank page."

7. **Call the provider.**
   - Inside `llm_gate.slot(provider, "job", settings)`, 90 s timeout, 1 retry on transient errors. Stateless: `store=false`, no `previous_response_id`.
   - Errors are classified with `llm_errors.classify`:

   | Error class | Action |
   |---|---|
   | Transient or outage | The turn row becomes `failed`. Reply `200 {stop:true, reason:'provider_outage', retry_at}`. The worker calls `/requeue {reason:'provider_outage'}`, which is uncharged. Backoff is 5, 15, then 60 min. One IT Admin `browser_agent.provider_problem` bell per provider per hour (compare-and-set in `browser_agent_runtime_state.provider_alerts`). |
   | Configuration (`model_not_found`, 401/403 key) | Clear `model_probe_ok_at`, set the type's `llm_escalation_enabled=false` (the playbook keeps running), notify IT Admin once (bell plus email), and reply `stop: provider_config`. The job goes to `NeedsHelp(needs_info)`. |

8. **Record.**
   - Request and response go to `llm_request` / `llm_response` artifacts. They are written after the scrub, and their hashes go into the turn row and an `llm.turn` step.
   - The turn row gets usage, cost and the provider request id.
   - A mirror row goes into `llm_call_log` (`feature='browser_agent'`, `tier='job'`, `job_id` null).
   - The provider name is added to `llm_gate` gate keys (`google`, `xai`, `meta`).

9. **Validate.** Exactly one tool call, checked against the JSON schema and the policy: the code ceiling for hosts and paths, the per-type tool set, and help-file ownership. A violation becomes an error observation and still counts as a step. A valid call becomes `issued` and is returned.

**Adapters.** All live in `bdr_be/app/services/browser_agents/llm/`:

| File | Contents |
|---|---|
| `base.py` | `complete(system, messages, tools, images, max_output_tokens) -> LlmResult{text, tool_calls, usage{in,out,cached}, provider_request_id, stop_reason}` |
| `openai_.py` | Responses API, `parallel_tool_calls=False`, `store=False` |
| `compat.py` | xAI `https://api.x.ai/v1` and Meta `https://api.meta.ai/v1`, both Chat Completions with parallel calls off |
| `anthropic_.py` | Messages API, `disable_parallel_tool_use=True`, `tool_choice={"type":"auto"}` |
| `google_.py` | `google-genai`. The key is sent only in the `x-goog-api-key` header. |

**Model picker and probe.**
- `GET /browser-agents/admin/models?provider=` lists models with the env key, caches them for 1 h, and filters by reported capabilities when the provider reports them.
- Saving a provider and model requires **Test model** to pass: one forced-shape tool call plus a 1x1 PNG. "Save anyway" is a logged override.
- A provider cannot be selected until IT Admin records the data-use attestation (`browser_agent_provider_attestations`): paid tier, no training, retention reviewed. The attestation id is copied into `config_snapshot`.

**Pricing.** `price_in_per_mtok` and `price_out_per_mtok` are prefilled from `llm_pricing.py` when the model is known, and required before a type can enable LLM escalation.

**Keys.** These are new in Railway env only: `GOOGLE_API_KEY`, `XAI_API_KEY`, `META_API_KEY`. `OPENAI_API_KEY` and `ANTHROPIC_API_KEY` already exist.

**Logging.** A global httpx log filter, `core/redact.install_httpx_filter()`, applies `redact_url` to every httpx log record. It is installed in the `main.py` lifespan and in the worker's `__main__`.

---

## 2. Data model: `bdr_be/supabase/migrations/0157_browser_agents.sql`

**Conventions:**
- Every new table: `enable` plus `force row level security`, with no policies.
- Every function is `security invoker` with `set search_path = pg_catalog, public`.
- `revoke all on function ... from public, anon, authenticated; grant execute ... to service_role;`. This also applies to trigger functions.
- No pgcrypto: `gen_random_uuid()` is core, and hashing is done in Python.
- Immutable tables store actor ids as plain `uuid` with no FK, plus an `*_label text` (name and role at the time).
- Workers and jobs are never deleted, so FKs to them are `on delete restrict`.
- The file ends with `notify pgrst, 'reload schema';`.
- **Apply to dev BDR only.**

### 2.1 Settings, runtime state, types, attestations, change log

```sql
create table browser_agent_settings (
  id smallint primary key default 1 check (id = 1),
  enabled boolean not null default false,                       -- kill switch
  config_version bigint not null default 1,                     -- bumped by triggers only
  daily_cost_cap_usd numeric(10,2) not null default 25 check (daily_cost_cap_usd between 0 and 1000),
  help_hold_seconds int not null default 900 check (help_hold_seconds between 60 and 3600),
  help_reminder_minutes int not null default 120 check (help_reminder_minutes between 10 and 1440),
  help_claim_lapse_minutes int not null default 30 check (help_claim_lapse_minutes between 5 and 240),
  help_expire_hours int not null default 48 check (help_expire_hours between 1 and 336),
  worker_offline_alert_minutes int not null default 60 check (worker_offline_alert_minutes between 5 and 1440),
  worker_offline_email_hours int not null default 4 check (worker_offline_email_hours between 1 and 72),
  job_ttl_hours int not null default 168 check (job_ttl_hours between 1 and 720),
  browser_login_fallback_enabled boolean not null default true,
  browser_login_releases_per_day smallint not null default 3 check (browser_login_releases_per_day between 0 and 10),
  desired_worker_version text,
  updated_by uuid references profiles(id) on delete set null,
  updated_at timestamptz not null default now()
);
insert into browser_agent_settings (id) values (1) on conflict do nothing;

create table browser_agent_runtime_state (                      -- sweeper bookkeeping; no config_version bump
  id smallint primary key default 1 check (id = 1),
  cost_window_started_at timestamptz,
  half_cap_notified_at timestamptz, cap_hit_notified_at timestamptz,
  offline_notified_at timestamptz, offline_emailed_at timestamptz,
  provider_alerts jsonb not null default '{}'::jsonb,           -- {provider: last_alert_at}
  updated_at timestamptz not null default now()
);
insert into browser_agent_runtime_state (id) values (1) on conflict do nothing;

create table browser_agent_types (
  key text primary key check (key ~ '^[a-z][a-z0-9_]{2,40}$'),
  label text not null,
  enabled boolean not null default false,
  identity text check (identity ~ '^[a-z][a-z0-9_]{2,40}$'),    -- 'procore' | null
  llm_escalation_enabled boolean not null default false,
  provider text check (provider in ('openai','anthropic','meta','google','xai')),
  model text check (model ~ '^[A-Za-z0-9._:/-]{1,120}$'),
  price_in_per_mtok numeric(10,4), price_out_per_mtok numeric(10,4),
  model_probe_ok_at timestamptz,
  max_concurrency smallint not null default 2 check (max_concurrency between 0 and 8),
  max_attempts smallint not null default 3 check (max_attempts between 1 and 10),        -- charged
  max_interruptions smallint not null default 20 check (max_interruptions between 1 and 100),
  max_steps int not null default 40 check (max_steps between 0 and 200),
  max_total_tokens int not null default 400000 check (max_total_tokens between 0 and 5000000),
  max_cost_usd numeric(8,2) not null default 2.00 check (max_cost_usd between 0 and 50),
  max_active_seconds int not null default 1200 check (max_active_seconds between 60 and 7200),
  host_narrowing text[],                                        -- null = full code ceiling; API checks subset
  prompt_version text not null,
  priority_default smallint not null default 100,
  updated_by uuid references profiles(id) on delete set null,
  updated_at timestamptz not null default now(),
  check (not llm_escalation_enabled or (provider is not null and model is not null
         and price_in_per_mtok is not null and price_out_per_mtok is not null))
);
insert into browser_agent_types (key, label, identity, prompt_version) values
  ('rfi_from_email','RFI from email','procore','rfi_from_email.v1'),
  ('canary','Worker self-test',null,'canary.v1')
on conflict do nothing;

create table browser_agent_provider_attestations (
  provider text primary key check (provider in ('openai','anthropic','meta','google','xai')),
  confirmed boolean not null,
  note text check (length(note) <= 1000),
  attested_by uuid, attested_by_label text not null,
  attested_at timestamptz not null default now()
);

create table browser_agent_settings_changes (                   -- append-only
  id bigint generated always as identity primary key,
  scope text not null check (scope in ('global','agent_type','secret','worker','release','attestation','help')),
  target text, field text not null,
  old_value jsonb, new_value jsonb,                              -- secrets: {"set":true,"fingerprint":"ab12cd34"}
  probe jsonb,
  changed_by uuid, changed_by_label text not null,
  changed_at timestamptz not null default now()
);
create index browser_agent_settings_changes_at_idx on browser_agent_settings_changes (changed_at desc);
```

**`config_version` triggers:**
- `BEFORE UPDATE` on `browser_agent_settings`: sets `new.config_version := old.config_version + 1` when any column other than `config_version`, `updated_by` or `updated_at` changed.
- `AFTER INSERT OR DELETE OR UPDATE OF enabled, identity, llm_escalation_enabled, max_concurrency, host_narrowing, max_active_seconds` on types, and `AFTER UPDATE OF status, draining, max_slots, allowed_types, allowed_ips` on workers: `update browser_agent_settings set config_version = config_version + 1 where id = 1`.

  The heartbeat's `last_seen_at` writes and the sweeper's runtime-state writes never bump it.

### 2.2 Encrypted secrets

```sql
create table browser_agent_secrets (
  name text primary key check (name ~ '^[a-z][a-z0-9_]{2,40}_password$'),   -- '<identity>_password'
  ciphertext bytea not null,                                    -- AES-256-GCM ct||tag
  nonce bytea not null check (length(nonce) = 12),
  key_id text not null,
  version int not null default 1,                               -- AAD = 'bdr-agent-secret|'||name||'|'||version
  fingerprint text not null,                                    -- HMAC-SHA256(fingerprint key, value) hex[:8]
  set_by uuid references profiles(id) on delete set null,
  set_at timestamptz not null default now()
);
```

### 2.3 Workers, enrollment, releases

```sql
create table browser_agent_workers (
  id uuid primary key default gen_random_uuid(),
  name text not null unique check (length(name) between 2 and 60),
  status text not null default 'pending' check (status in ('pending','active','revoked')),
  token_hash bytea unique,                                      -- sha256 of 'bdrw_'||b64url(32 bytes)
  token_prefix text,
  draining boolean not null default false,
  max_slots smallint not null default 3 check (max_slots between 1 and 8),
  allowed_types text[] not null default '{}',                   -- admin-set; intersected at claim
  allowed_ips cidr[] not null default '{}',                     -- empty = any
  boot_id uuid, boot_at timestamptz, hello_count_hour int not null default 0, hello_window_at timestamptz,
  version text, bad_versions text[] not null default '{}',
  hostname text, os_info jsonb, chromium_version text,
  capabilities jsonb,                                           -- {"types":{"rfi_from_email":"1.0.0"},"slots":3}
  control_capabilities jsonb not null default '{}'::jsonb,      -- seam: {"screencast":false}
  last_seen_at timestamptz, last_ip inet, enroll_ip inet,
  created_by uuid references profiles(id) on delete set null,
  approved_by uuid references profiles(id) on delete set null, approved_at timestamptz,
  revoked_by uuid references profiles(id) on delete set null, revoked_at timestamptz,
  created_at timestamptz not null default now()
);
create index browser_agent_workers_seen_idx on browser_agent_workers (last_seen_at);

create table browser_agent_enrollments (
  id uuid primary key default gen_random_uuid(),
  code_hash bytea not null unique,                              -- sha256 of 20-char base32 code
  worker_name text not null,
  created_by uuid references profiles(id) on delete set null,
  created_at timestamptz not null default now(),
  expires_at timestamptz not null,                              -- +30 min
  used_at timestamptz,
  worker_id uuid references browser_agent_workers(id) on delete restrict
);

create table browser_agent_enroll_attempts (
  ip inet not null, window_start timestamptz not null, count int not null default 0,
  primary key (ip, window_start)
);

create table browser_agent_worker_releases (                    -- written only by the publish script
  version text primary key check (version ~ '^\d+\.\d+\.\d+$'),
  storage_path text not null,
  sha256 text not null check (sha256 ~ '^[0-9a-f]{64}$'),
  min_version text not null check (min_version ~ '^\d+\.\d+\.\d+$'),
  manifest jsonb not null,                                      -- {version, sha256, min_version, created_at}
  signature text not null,                                      -- Ed25519 over canonical manifest
  chromium_version text,
  notes text,
  created_at timestamptz not null default now()
);
```

### 2.4 Jobs and turns

```sql
create table browser_agent_jobs (
  id uuid primary key default gen_random_uuid(),
  agent_type text not null references browser_agent_types(key),
  status text not null default 'queued' check (status in
    ('queued','running','waiting_help','applying','succeeded','failed','canceled','expired')),
  outcome text check (outcome in ('pending','applied','apply_failed','nothing_to_do',
                                  'already_current','skipped_stale','no_link')),
  priority smallint not null default 100,
  dedupe_key text not null,                                     -- 'email:<id>:rfi:<rfi_id>' | 'email:<id>:locate'
  platform text,                                                -- 'procore'
  project_id uuid references projects(id) on delete set null,
  source_email_id uuid references ingested_emails(id) on delete set null,
  input jsonb not null,
  config_snapshot jsonb,
  lease_owner uuid references browser_agent_workers(id) on delete restrict,
  lease_boot_id uuid,
  lease_epoch bigint not null default 0,
  lease_expires_at timestamptz,
  attempts smallint not null default 0,                         -- charged (first claim, crash, error retry)
  interrupted_count smallint not null default 0,                -- boot change, BDR outage, shutdown, drain
  next_attempt_at timestamptz,
  resume_reason text check (resume_reason in ('retry','interrupted','help_answered','manual',
                            'login_spacing','shutdown','drain','provider_outage')),
  last_resume_reason text,
  checkpoint jsonb not null default '{}'::jsonb,
  checkpoint_seq int not null default 0,
  step_count int not null default 0, turn_count int not null default 0,
  tokens_in bigint not null default 0, tokens_out bigint not null default 0,
  cost_usd numeric(10,4) not null default 0,
  active_seconds int not null default 0,
  helper_retry_used boolean not null default false,
  apply_attempt smallint not null default 0,
  sync_run_id uuid,                                             -- fk added in 2.7
  waiting_help_id uuid,
  hold_until timestamptz,
  result jsonb, error_kind text, error text check (length(error) <= 1000),
  control_mode text not null default 'agent' check (control_mode in ('agent')),   -- seam
  cancel_requested_at timestamptz, cancel_requested_by uuid references profiles(id) on delete set null,
  expires_at timestamptz not null,
  created_by uuid references profiles(id) on delete set null,
  created_at timestamptz not null default now(), started_at timestamptz,
  finished_at timestamptz, updated_at timestamptz not null default now(),
  constraint lease_shape check ((lease_owner is null) = (lease_expires_at is null))
);
create unique index browser_agent_jobs_dedupe_uidx on browser_agent_jobs (agent_type, dedupe_key);
create index browser_agent_jobs_claim_idx on browser_agent_jobs (agent_type, priority, created_at) where status = 'queued';
create index browser_agent_jobs_lease_idx on browser_agent_jobs (lease_expires_at) where lease_owner is not null;
create index browser_agent_jobs_applying_idx on browser_agent_jobs (updated_at) where status = 'applying';
create index browser_agent_jobs_project_idx on browser_agent_jobs (project_id, created_at desc);
create index browser_agent_jobs_status_idx on browser_agent_jobs (status, created_at desc);

create table browser_agent_turns (
  job_id uuid not null references browser_agent_jobs(id) on delete restrict,
  turn_no int not null,
  epoch bigint not null,
  status text not null check (status in ('thinking','issued','failed','abandoned')),
  thinking_owner text, thinking_until timestamptz,
  billing_unknown boolean not null default false,
  tool_results jsonb not null,                                  -- redacted observation
  screenshot_artifact_id uuid,
  assistant_text text, action jsonb,                            -- {call_id, tool, args}
  provider text, model text, provider_request_id text,
  request_artifact_id uuid, response_artifact_id uuid,
  request_sha256 text, response_sha256 text,
  input_tokens int, output_tokens int, cached_tokens int, cost_usd numeric(10,6),
  duration_ms int, error_kind text, error text,
  created_at timestamptz not null default now(),
  primary key (job_id, turn_no)
);
create index browser_agent_turns_day_idx on browser_agent_turns (created_at);
```

### 2.5 Artifacts, ledger, login attempts

```sql
create table browser_agent_artifacts (
  id uuid primary key default gen_random_uuid(),
  job_id uuid references browser_agent_jobs(id) on delete restrict,
  help_id uuid,                                                 -- fk below
  kind text not null check (kind in ('screenshot','rfi_detail_json','rfi_list_json','page_snapshot',
                                     'llm_request','llm_response','helper_upload')),
  storage_path text not null unique,                            -- bucket browser-agent
  filename text, mime_type text not null, sniffed_mime text, size_bytes bigint not null,
  sha256 text not null check (sha256 ~ '^[0-9a-f]{64}$'),       -- of the original bytes
  attachable boolean not null default false,                    -- helper uploads: helper ticked + type allows
  stripped_at timestamptz,                                      -- signed query strings removed after terminal
  uploaded_by_worker uuid references browser_agent_workers(id) on delete restrict,
  uploaded_by_user uuid references profiles(id) on delete set null,
  created_at timestamptz not null default now(),
  check (job_id is not null or help_id is not null)
);
create unique index browser_agent_artifacts_job_sha_uidx on browser_agent_artifacts (job_id, kind, sha256) where job_id is not null;

create table browser_agent_steps (                              -- append-only ledger
  id bigint generated always as identity primary key,
  job_id uuid not null references browser_agent_jobs(id) on delete restrict,
  seq int not null,                                             -- per job, BEFORE INSERT trigger (advisory xact lock)
  epoch bigint not null,
  late boolean not null default false,                          -- old-epoch rows, no state effect
  worker_id uuid,
  idem_key text,
  kind text not null check (kind ~ '^[a-z_]+(\.[a-z_]+)?$'),
  actor text not null check (actor in ('worker','playbook','model','bdr','helper','admin','system')),
  actor_user_id uuid, actor_label text,
  summary text not null check (length(summary) <= 500),
  tool_name text, args jsonb, result jsonb,                     -- redacted; 64 KB cap by writer
  payload_sha256 text,                                          -- of canonical args||result before storage
  llm_request_sha256 text, llm_response_sha256 text, artifact_sha256 text,
  url_before text, url_after text,                              -- query strings stripped
  screenshot_artifact_id uuid references browser_agent_artifacts(id) on delete restrict,
  turn_no int, ok boolean, duration_ms int,
  at timestamptz not null default now(),
  redacted_at timestamptz, redacted_by_label text
);
create unique index browser_agent_steps_seq_uidx on browser_agent_steps (job_id, seq);
create unique index browser_agent_steps_idem_uidx on browser_agent_steps (job_id, idem_key) where idem_key is not null;
create index browser_agent_steps_kind_idx on browser_agent_steps (kind, at desc);
-- BEFORE UPDATE: raise unless every column is unchanged except args/result -> '{"redacted":true}', redacted_at, redacted_by_label.
-- BEFORE DELETE and BEFORE TRUNCATE: raise. Same immutability (no update allowed) on settings_changes.

create table browser_agent_login_attempts (
  id uuid primary key default gen_random_uuid(),
  identity text not null, host text not null,
  kind text not null check (kind in ('broker','browser')),
  job_id uuid not null references browser_agent_jobs(id) on delete restrict,
  worker_id uuid references browser_agent_workers(id) on delete restrict,
  epoch bigint not null,
  granted boolean not null,
  refused_reason text check (refused_reason in ('spacing','locked','not_configured','help_open',
                             'daily_release_cap','fallback_disabled','no_broker_failure','ip_not_allowed')),
  secret_version int,
  outcome text check (outcome in ('ok','stored_jar','password_rejected','mfa','captcha','challenge',
                                  'sso','login_page','redirect','unexpected','timeout','unreported')),
  step text, counted_as_failure boolean not null default false,
  created_at timestamptz not null default now(), reported_at timestamptz
);
create index browser_agent_login_attempts_open_idx on browser_agent_login_attempts (created_at)
  where kind = 'browser' and granted and reported_at is null;
```

### 2.6 Help, chat, contributions

```sql
create table browser_agent_help_requests (
  id uuid primary key default gen_random_uuid(),
  scope_key text not null,                                      -- 'login:procore' | 'link:<project uuid>' | 'job:<uuid>'
  agent_type text references browser_agent_types(key),
  reason_code text not null check (reason_code in ('login_failed','login_locked','captcha','mfa','sso',
     'logged_out','access_denied','not_found','ambiguous','unexpected_page','cap_reached',
     'needs_info','provider_config','other')),
  title text not null,                                          -- server template (trusted)
  agent_message text check (length(agent_message) <= 1000),     -- untrusted
  screenshot_artifact_id uuid references browser_agent_artifacts(id) on delete restrict,
  project_id uuid references projects(id) on delete set null,
  opened_by_job_id uuid references browser_agent_jobs(id) on delete restrict,
  holder_job_id uuid references browser_agent_jobs(id) on delete restrict,   -- job holding the live page
  status text not null default 'open' check (status in ('open','answered','resolved','expired','canceled')),
  claimed_by uuid references profiles(id) on delete set null, claimed_at timestamptz,
  last_helper_activity_at timestamptz,
  resolved_by uuid references profiles(id) on delete set null, resolved_at timestamptz,
  resolution text check (resolution in ('agent_continued','retry','give_up','skip_daily','auto_resolved','expired')),
  notified_at timestamptz, reminder_count smallint not null default 0, reminder_sent_at timestamptz,
  expiry_emailed_at timestamptz,
  expires_at timestamptz not null,
  created_at timestamptz not null default now()
);
create unique index browser_agent_help_open_uidx on browser_agent_help_requests (scope_key) where status in ('open','answered');
create index browser_agent_help_status_idx on browser_agent_help_requests (status, created_at desc);
create index browser_agent_help_scope_recent_idx on browser_agent_help_requests (scope_key, resolved_at desc);

create table browser_agent_help_waiters (
  help_id uuid not null references browser_agent_help_requests(id) on delete restrict,
  job_id uuid not null references browser_agent_jobs(id) on delete restrict,
  added_at timestamptz not null default now(),
  primary key (help_id, job_id)
);

create table browser_agent_help_messages (
  id bigint generated always as identity primary key,
  help_id uuid not null references browser_agent_help_requests(id) on delete restrict,
  author_kind text not null check (author_kind in ('agent','helper','system')),
  author_id uuid, author_label text,
  job_id uuid references browser_agent_jobs(id) on delete restrict,
  client_msg_id uuid,
  action text not null default 'message' check (action in ('message','retry','give_up','skip_daily')),
  body text check (length(body) <= 8000),
  is_secret boolean not null default false,
  secret_id uuid unique,                                        -- client-generated; AAD = 'help-secret|'||secret_id
  secret_ciphertext bytea, secret_nonce bytea, secret_hmac text,
  secret_consumed_at timestamptz, secret_consumed_by_job uuid references browser_agent_jobs(id) on delete restrict,
  secret_purged_at timestamptz,
  artifact_ids uuid[] not null default '{}',
  created_at timestamptz not null default now(),
  unique (help_id, client_msg_id),
  check ((is_secret and body is null and secret_id is not null) or (not is_secret and secret_ciphertext is null))
);
-- BEFORE UPDATE: only secret_consumed_at/_by_job, secret_purged_at and nulling secret_ciphertext/secret_nonce.
-- BEFORE DELETE: raise.

create table browser_agent_help_reads (
  job_id uuid not null references browser_agent_jobs(id) on delete restrict,
  message_id bigint not null references browser_agent_help_messages(id) on delete restrict,
  epoch bigint not null,
  consumed_at timestamptz not null default now(),
  primary key (job_id, message_id)
);

create table browser_agent_rfi_contributions (
  id uuid primary key default gen_random_uuid(),
  project_id uuid not null references projects(id) on delete cascade,
  platform text not null, external_rfi_id text not null check (external_rfi_id ~ '^\d{1,20}$'),
  kind text not null check (kind in ('file_question','file_answer','note')),
  artifact_id uuid references browser_agent_artifacts(id) on delete restrict,
  note_text text check (length(note_text) <= 8000),             -- verbatim helper text
  note_author_label text, source_message_ids bigint[] not null default '{}',
  help_id uuid references browser_agent_help_requests(id) on delete restrict,
  job_id uuid references browser_agent_jobs(id) on delete restrict,
  applied_rfi_id uuid references rfis(id) on delete set null, applied_at timestamptz,
  created_at timestamptz not null default now(),
  check ((kind = 'note') = (note_text is not null)),
  check ((kind <> 'note') = (artifact_id is not null))
);
create unique index browser_agent_rfi_contrib_uidx on browser_agent_rfi_contributions
  (project_id, platform, external_rfi_id, kind, coalesce(artifact_id::text, md5(note_text)));
create index browser_agent_rfi_contrib_pending_idx on browser_agent_rfi_contributions (project_id) where applied_at is null;

alter table browser_agent_jobs add constraint browser_agent_jobs_help_fk
  foreign key (waiting_help_id) references browser_agent_help_requests(id) on delete restrict;
alter table browser_agent_artifacts add constraint browser_agent_artifacts_help_fk
  foreign key (help_id) references browser_agent_help_requests(id) on delete restrict;
```

### 2.7 Changes to existing tables, storage, login RPCs

```sql
-- the inline CHECK in 0156:111 has the default name; verify with
-- select conname from pg_constraint where conrelid='pm_rfi_sync_runs'::regclass and contype='c';
alter table pm_rfi_sync_runs drop constraint if exists pm_rfi_sync_runs_trigger_check;
alter table pm_rfi_sync_runs add constraint pm_rfi_sync_runs_trigger_check
  check (trigger in ('daily','manual','email','agent'));
alter table pm_rfi_sync_runs add column if not exists agent_job_id uuid references browser_agent_jobs(id) on delete restrict;
alter table pm_rfi_sync_runs add column if not exists apply_attempt smallint;
alter table pm_rfi_sync_runs add column if not exists detail_artifact_id uuid references browser_agent_artifacts(id) on delete restrict;
create unique index if not exists pm_rfi_sync_runs_agent_uidx
  on pm_rfi_sync_runs (agent_job_id, apply_attempt) where agent_job_id is not null;
alter table browser_agent_jobs add constraint browser_agent_jobs_run_fk
  foreign key (sync_run_id) references pm_rfi_sync_runs(id) on delete set null;

alter table rfis add column if not exists import_method text check (import_method in ('web_session','agent'));
alter table rfis add column if not exists imported_by_agent_job_id uuid references browser_agent_jobs(id) on delete set null;
alter table rfis add column if not exists external_supplement jsonb;     -- [{"text","by","at","help_id","contribution_id"}]

alter table rfp_harvest_sessions add column if not exists last_login_source text;   -- 'backend' | 'broker:<job>' | 'agent:<worker>'

insert into storage.buckets (id, name, public, file_size_limit)
values ('browser-agent','browser-agent', false, 157286400) on conflict (id) do nothing;   -- 150 MB
```

**Login RPCs.** These replace the read-then-stamp race:

| RPC | Behavior |
|---|---|
| `procore_reserve_login(p_provider text, p_min_interval int, p_source text, p_bypass_after_broker boolean) returns jsonb` | Inserts the row if missing (`on conflict do nothing`). Then runs one `update rfp_harvest_sessions set last_login_attempt_at = now(), last_login_source = p_source where provider = p_provider and (locked_until is null or locked_until <= now()) and (last_login_attempt_at is null or last_login_attempt_at < now() - make_interval(secs => p_min_interval) or p_bypass_after_broker) returning ...`. Returns `{granted, locked_until, retry_at}`. |
| `procore_record_login_failure(p_provider text, p_error text, p_max_failures int, p_lock_seconds int) returns rfp_harvest_sessions` | `login_failures = login_failures + 1`. Sets `locked_until` when the failures reach the cap. Atomic. |

The `p_bypass_after_broker` grant is honored by the API only when the immediately preceding attempt was a broker attempt for this job within 5 min that failed at `challenge`, `login_page` or `redirect`.

**Python (`pm_rfi_sync.py`):**
- `TRIGGERS += ("agent",)`.
- `EXTERNAL_FIELDS` gains `import_method`, `imported_by_agent_job_id` and `external_supplement`.

### 2.8 Job RPCs

All are SECURITY INVOKER. Each fences inside its own transaction on:

```
id = p_job and lease_owner = p_worker and lease_epoch = p_epoch
  and lease_boot_id = p_boot and status in (<allowed>)
```

and also checks `browser_agent_workers.boot_id = p_boot and status = 'active'`.

| RPC | Does |
|---|---|
| `claim_browser_agent_jobs(p_worker, p_boot, p_capacity jsonb, p_lease_seconds)` | Raises `stale_boot` (mapped to 409) on a boot mismatch, or when the worker is not active or is draining. Returns nothing when the kill switch is off. For each key in `p_capacity` that is in `allowed_types`, in `capabilities.types`, and enabled: <br>1. `pg_advisory_xact_lock(hashtext('bagent:'||key))`. <br>2. `take = least(want, max_concurrency - count(lease_owner is not null))`. <br>3. Select queued rows where `coalesce(next_attempt_at, now()) <= now() and expires_at > now()`, ordered by `priority, created_at`, `for update skip locked`, `limit take`. <br>4. Set `running`, the lease, `lease_epoch+1`, `attempts = attempts + case when coalesce(resume_reason,'retry') = 'retry' then 1 else 0 end`, `last_resume_reason = resume_reason`, `resume_reason = null`, `config_snapshot`, `started_at = coalesce(started_at, now())`. <br>5. Mark this job's `thinking` turns `abandoned`. <br>6. Insert a `claim` step (worker_id, epoch). |
| `browser_agent_hello(p_worker, p_boot, p_info jsonb)` | Counts hellos per hour. More than 6 per hour sets `draining=true` and returns `suspended` (IT Admin bell `browser_agent.worker_suspicious`). Sets `boot_id`. Requeues jobs leased under any other boot: `queued`, `resume_reason='interrupted'`, `interrupted_count+1`, `lease_epoch+1`, lease cleared, `job.interrupted` step. Over `max_interruptions` the job fails with `interrupted_repeatedly`. |
| `browser_agent_heartbeat(p_worker, p_boot, p_jobs jsonb)` | Stamps `last_seen_at` and `last_ip`. For each listed `(job, epoch)`: renews when owner, epoch and boot match, **even if the lease expired, provided nobody reclaimed it** (the epoch is unchanged). Adds `active_seconds_delta`, capped at 30 per beat. Returns `renewed`, `lost`, `cancel`, `help_updates`, and `orphans` (leases this boot holds that were not listed). |
| `browser_agent_append_steps(p_job, p_worker, p_boot, p_epoch, p_rows jsonb)` | If the epoch is current and fenced, inserts normally. If `p_epoch < lease_epoch` and a `claim` step exists with `(p_epoch, p_worker)`, inserts with `late=true`. Otherwise raises. Duplicate `idem_key` rows are skipped. |
| `browser_agent_checkpoint(p_job, p_worker, p_boot, p_epoch, p_seq, p_data)` | Fenced. Applied only when `p_seq > checkpoint_seq`. |
| `browser_agent_open_help(p_job, p_worker, p_boot, p_epoch, p_scope, p_reason, p_title, p_message, p_shot, p_hold_seconds, p_expires_hours)` | `insert ... on conflict (scope_key) where status in ('open','answered') do nothing`, then select the open row and join it. Adds a waiter. The first opener becomes `holder_job_id`. The job becomes `waiting_help` with `hold_until`. Writes a `help.opened` step. Returns `(help_id, joined)`. |
| `browser_agent_park(...)` | Fenced. Clears the lease and keeps `waiting_help`. |
| `browser_agent_submit_rfi_capture(p_job, p_worker, p_boot, p_epoch, p_artifact, p_external_rfi_id)` | Fenced. Inserts `pm_rfi_sync_runs(project_id, trigger='agent', agent_job_id, apply_attempt=job.apply_attempt, detail_artifact_id, target_external_rfi_id, source_email_id)` with `on conflict (agent_job_id, apply_attempt) where agent_job_id is not null do nothing`, then selects it. The job becomes `applying`, `outcome='pending'`, `sync_run_id` set, lease cleared. Writes a `capture.submit` step. |
| `browser_agent_finish(p_job, p_worker, p_boot, p_epoch, p_status, p_outcome, p_error_kind, p_error, p_result)` | Fenced. Terminal write. A no-op when the same terminal status is already set. |
| `browser_agent_requeue(p_job, p_worker, p_boot, p_epoch, p_reason, p_retry_at)` | Fenced. `shutdown` and `drain` are accepted only when `worker.draining` is set or the request carries `service_stop=true`, and are uncharged. `provider_outage` and `login_spacing` are uncharged. `crash` maps to `resume_reason='retry'` and is charged. Bumps the epoch. |
| `browser_agent_update_settings(p_scope, p_target, p_patch jsonb, p_actor uuid, p_actor_label text, p_probe jsonb)` | One transaction: update the row, insert one `settings_changes` row per changed field, insert `audit_log`. |

---

## 3. Worker to BDR protocol

### 3.1 Transport and auth

**Router.** `bdr_be/app/routers/agent_worker.py`:
- Prefix `/agent-worker/v1`, on the shared spine.
- Router dependency `require_browser_agents`: a bare 404 when `settings.browser_agents_active` is false.
- All handlers and dependencies are plain `def`.
- Every response sets `Cache-Control: no-store`.
- Multipart routes are added to `form_routes_matcher`.
- Rate limit `agent_worker_rate_limit`: 600 per minute per worker, in memory per process. That is acceptable, because the DB fence is the real guard.

**Auth: `core/agent_auth.py:require_agent_worker` (plain `def`).**
- Reads `Authorization: Bearer bdrw_...`.
- Hashes the token with sha256, looks it up by `token_hash`, and compares with `hmac.compare_digest`.
- Rejects with 401 unless the worker status is `active`. `pending` workers may call only `/hello` and `/config`, which return `{pending:true}`.
- Checks `client_ip(request)` against `allowed_ips` when that list is non-empty. `client_ip` reads the rightmost `X-Forwarded-For` entry only when `settings.trust_forwarded_for` is true, which is set on Railway. No global uvicorn change.
- A JWT is never accepted here, and `get_current_user` never accepts `bdrw_`.

**Fencing.**
- Job-scoped calls carry `X-Agent-Lease: <job_id>:<epoch>` and `X-Agent-Boot: <boot_id>`.
- A failure returns 409 with `X-Error-Code` set to one of `lease_lost`, `stale_boot` or `job_not_running`:

  | Code | Worker does |
  |---|---|
  | `stale_boot` | The process exits with code 3, and the launcher restarts it. |
  | `lease_lost` | Aborts that job at once. Late steps may still be appended. |

**IDOR checks.**
- Every `help_id` must be in `help_waiters` for the job.
- Every `message_id` must belong to that `help_id`.
- Every `artifact_id` must belong to the job, or to one of its help requests.

**Idempotency.** Every POST carries `Idempotency-Key`. The natural keys are:

| Write | Key |
|---|---|
| Steps | `(job_id, idem_key)` |
| Turns | `(job_id, turn_no)` |
| Artifacts | `(job_id, kind, sha256)` |
| Capture | `(agent_job_id, apply_attempt)` |
| Finish | The same terminal status means no-op |

### 3.2 Endpoints (the worker contract)

| Method and path | Request | Response | Notes |
|---|---|---|---|
| `POST /enroll` (no auth) | `{code, hostname, os_info, version}` | `{worker_id, token, status:'pending'}` | Code is single use, 30 min, 20 base32 characters. DB counter: 5 per hour per IP. IT Admin gets a `browser_agent.worker_pending` bell showing hostname and IP. |
| `POST /hello` | `{boot_id, version, chromium_version, bad_versions[], capabilities:{types:{key:ver}, slots}}` | `{status, config_version, desired_version, min_version, server_time, requeued}` | Once per process start. |
| `GET /config` (`If-None-Match: <config_version>`) | none | `{config_version, enabled, worker:{status, max_slots, draining, allowed_types}, types:{key:{enabled, identity, llm_escalation_enabled, max_concurrency, max_active_seconds, host_narrowing}}, help_hold_seconds, release:{version, sha256, min_version, manifest, signature} or null}` | No keys, passwords or prompts. |
| `POST /heartbeat` (every 15 s, its own thread) | `{boot_id, config_version, slots_free:{type:n}, loop_lag_ms, jobs:[{job_id, epoch, phase, step_count, active_seconds_delta}]}` | `{config_version, renewed:[{job_id, lease_expires_at}], lost:[job_id], cancel:[job_id], orphans:[{job_id, epoch}], help_updates:[{job_id, help_id, latest_message_id, status}], drain, controls:[]}` | `controls` is the live-view seam and is always `[]` in v1. The worker releases orphans with `/requeue {reason:'interrupted'}`. |
| `POST /claim` | `{boot_id, capacity:{type:n}}` | `{jobs:[{job_id, agent_type, epoch, lease_expires_at, input, checkpoint, checkpoint_seq, last_resume_reason, turn_count, unconsumed_help:[...]}]}` | BDR runs `prepare(job)` in try/except. A finished job is not returned. An exception releases the lease uncharged (`resume_reason='interrupted'`). |
| `POST /jobs/{id}/steps` | `[{idem_key, kind, actor, summary, tool_name, args, result, url_before, url_after, screenshot_artifact_id, turn_no, ok, duration_ms}]` up to 200 | `{appended, duplicates, late}` | Worker kinds: `playbook.*`, `tool.*`, `nav`, `http.blocked`, `login.*`, `browser.*`, `error.*`, `note`. |
| `PUT /jobs/{id}/checkpoint` | `{seq, data}` | 204 | Monotonic. |
| `POST /jobs/{id}/artifacts` (multipart) | `kind, sha256, filename, file` | `{artifact_id, existed}` | Per-kind caps: `screenshot` 2 MB PNG/JPEG (magic bytes); `rfi_detail_json` and `rfi_list_json` 8 MB valid JSON; `page_snapshot` 2 MB. sha256 is recomputed. |
| `POST /jobs/{id}/turns` | `{prev_turn_no, tool_results:[{call_id, ok, content<=32KB}], screenshot_artifact_id, url, title, helper_message_ids_seen[]}` | `200 {turn_no, assistant_text, action:{call_id, tool, args}, caps:{steps_left, cost_left_usd}}`, or `202 {retry_after}`, or `200 {stop:true, reason, retry_at?}` | Section 1. |
| `POST /jobs/{id}/identities/{identity}/session` | `{host, reject_jar_logged_in_at?}` | `{status:'ok', cookies:[{name,value,domain,path,expires,secure,httpOnly,sameSite}], logged_in_at, source:'stored'|'fresh_login'}` or `{status:'spacing'|'locked'|'login_failed'|'browser_login_needed'|'not_configured'|'help_open', retry_at?}` | Section 5.5. Worker timeout 180 s. |
| `POST /jobs/{id}/identities/{identity}/login/begin` | `{host}` | `{granted:true, attempt_id, email, password}` or `{granted:false, reason, retry_at}` | Fallback only. |
| `POST /jobs/{id}/identities/{identity}/login/{attempt_id}/result` | `{outcome, step, cookies?}` | `{locked_until}` | `ok` requires cookies. |
| `POST /jobs/{id}/help` | `{reason_code, message<=1000, screenshot_artifact_id}` | `{help_id, joined_existing, holder}` | BDR derives the scope and the title. A link scope suppressed in the last 24 h returns `{suppressed:true}`, and the worker finishes with `access_denied_suppressed`. |
| `GET /jobs/{id}/help/{hid}/messages?after=` | none | `{status, claimed_by_label, messages:[{id, author_kind, author_label, body or {secret_ref}, action, files:[{artifact_id, filename, size, mime, attachable}]}]}` | Never returns secret bodies. |
| `POST /jobs/{id}/help/{hid}/consume` | `{message_ids}` | 204 | Writes `help_reads`. |
| `POST /jobs/{id}/help/{hid}/messages` | `{body<=2000}` | `{id}` | Agent reply. |
| `POST /jobs/{id}/help/{hid}/secrets/{message_id}/use` | none | `{value}` | Holder job only, `kind='login'` phase only, compare-and-set on `secret_consumed_at`. The ciphertext is then nulled and `secret_hmac` kept for the tripwire. |
| `POST /jobs/{id}/park` | none | 204 | |
| `POST /jobs/{id}/contributions` | `{help_id, items:[{kind:'file_question'|'file_answer', artifact_id} or {kind:'note', message_ids[]}]}` | `{accepted:[...], rejected:[{item, reason}]}` | A file must be `attachable` and from this job's help. A note is the verbatim text of the cited helper messages. |
| `POST /jobs/{id}/rfi-capture` | `{platform:'procore', detail_artifact_id, external_rfi_id, company_id}` | `{run_id, accepted:true}` or 422 `{code}` | Section 5.4. |
| `POST /jobs/{id}/finish` | `{status, outcome?, error_kind?, error?, summary}` | 204 | `rfi_from_email` `succeeded` is allowed only for `nothing_to_do`. Captures go through `applying`. |
| `POST /jobs/{id}/requeue` | `{reason:'shutdown'|'drain'|'crash'|'login_spacing'|'provider_outage'|'interrupted', retry_at?, service_stop?}` | 204 | Section 2.8. |
| `GET /releases/{version}` | none | `{manifest, signature, url}` | Signed URL, 900 s. The token query is redacted in logs. |
| `GET /canary/page` | none | static HTML | Self-test target. |
| `/jobs/{id}/live` | | 404 | Reserved for live view. |

**Error codes** in `X-Error-Code`:

| Code | Status |
|---|---|
| `lease_lost`, `stale_boot`, `job_not_running` | 409 |
| `capture_invalid`, `artifact_invalid`, `idor` | 422 / 403 |
| `pending_approval`, `revoked` | 401 |
| `rate_limited` | 429 |

### 3.3 Leases, outages and hard shutdown

**Timing.** Lease 90 s, heartbeat 15 s from a dedicated thread with its own `httpx.Client`. A watchdog writes a `browser.loop_stall` step when the asyncio loop lags more than 5 s.

**BDR unreachable.** The worker keeps running its playbooks. Steps spool to `C:\ProgramData\BDRAgent\spool\<job>.jsonl`, which never contains secrets. A call that needs BDR (session, capture, turns) retries with jitter. After **15 min** without BDR, the worker aborts locally and the spool is uploaded later as late steps.

**Sweeper reclaim rule.** A `running` job is reclaimed only when all three hold:
- `lease_expires_at < now()`;
- the owning worker's `last_seen_at < now() - lease_seconds`;
- this sweeper process has been up more than `lease_seconds + 15 s`, which covers a BDR restart.

So a BDR outage alone never kills a live job: the worker's heartbeat renews by epoch once BDR is back.

**Hard shutdown or power pull mid-job:**
1. **PC comes back.** The new process calls `/hello` with a new `boot_id`, and every old lease is requeued `interrupted`, uncharged, with the epoch bumped.
2. **PC stays off.** The sweeper reclaims after the lease plus silence (also uncharged `interrupted`).
3. **Interruption cap.** At `interrupted_count > max_interruptions` (20) the job fails `interrupted_repeatedly`.
4. **Charged failures.** Crashes, context crashes and errors are charged `attempts`. Over `max_attempts` (3) the job fails `crashed_repeatedly`. Each failure sends one `browser_agent.job_failed` bell to IT Admin.

**Resume.** Checkpoint plus `turn_count` (section 4.5). Replay is safe because the browser is read-only toward Procore and every BDR write is idempotent (5.6).

**Boot hygiene, before `/hello`:**
- kill any `chrome.exe` whose command line contains `C:\ProgramData\BDRAgent\`;
- wipe `tmp\`;
- validate every local file (atomic writes, `.bak` fallback).

The worker never resumes from local state, except to upload spooled late steps.

### 3.4 BDR sweeper

`browser_agents.queue.polling_loop()`:
- Started in the `main.py` lifespan when `browser_agents_active and supabase_url`.
- Runs every 20 s via `asyncio.to_thread`, in both uvicorn workers.
- Every action is compare-and-set.

It does these things:

1. **Reclaim** expired `running` leases (section 3.3 rule).
2. **Park** `waiting_help` jobs whose lease expired past `hold_until`: lease cleared, no charge.
3. **Reconcile `applying` jobs** from `pm_rfi_sync_runs.status`:

   | Run status | Job becomes |
   |---|---|
   | `done` | `succeeded`, outcome = `run.stats.agent_outcome` or `applied` |
   | `failed` | `failed / apply_failed`; IT Admin bell |
   | `queued` / `running` | wait |

   This covers the `claim_next` abandon path and the `lost` path.
4. **Strip signed queries.** For `rfi_detail_json` artifacts of terminal jobs, rewrite the blob with attachment query strings removed and set `stripped_at`.
5. **Help:**
   - one reminder (bell plus email) after `help_reminder_minutes` while unclaimed, up to 3;
   - claims lapse after `help_claim_lapse_minutes` without helper activity, which reopens the request and sends another reminder;
   - at `expires_at`, `job:` and `link:` scopes become `expired` and their waiters fail `help_timeout` (Estimating Admin bell);
   - `login:` scopes never expire while `locked_until > now()` or the last outcome was `mfa`/`captcha`. Instead IT Admin gets an email at `expires_at` (`expiry_emailed_at` compare-and-set), and the request stays open.
6. **Secrets.** Purge helper secrets unconsumed after 30 min. The ciphertext is nulled; `secret_hmac` is kept for 7 days for the tripwire, then nulled.
7. **Unreported browser logins.** An attempt older than 5 min with no result: `procore_record_login_failure(..., 'agent attempt did not report')`, `outcome='unreported'`.
8. **Expiry.** Queued jobs past `expires_at` become `expired`, with a `job.expired` step.
9. **Cancels.** Jobs with `cancel_requested_at` whose lease lapsed become `canceled`.
10. **Spend.** Reset the daily window at 00:00 PT. Alert at 50% and 100% of the cap (bell, compare-and-set).
11. **Worker offline.** When no active worker has been seen for `worker_offline_alert_minutes` and a queued job is older than that: one IT Admin bell. After `worker_offline_email_hours`: one email. Both reset on the next heartbeat.
12. **Contributions.** Apply pending `browser_agent_rfi_contributions` whose RFI row now exists (`contributions.apply_pending(sb, project_id)`).

No retention purge in v1.

### 3.5 Config and model propagation
- A config change bumps `config_version` (section 2.1 triggers). The heartbeat returns it, and the worker refetches `/config` within about 15 s.
- The worker applies slots, enablement, narrowing, drain and the release. A running job is never preempted.
- **Provider and model never reach the PC.** They are snapshotted at claim, so a change applies to the next attempt.
- **Kill switch.** `enabled=false` empties claims, and `/turns` answers `stop: disabled`.
- **Stop running jobs** sets `cancel_requested_at`, which the heartbeat delivers as `cancel`.
- **No token rotation in v1.** A compromise is handled by Revoke plus re-enroll.

### 3.6 Slots and multiple agent types
- One worker process, one asyncio loop, and `max_slots` slot tasks.
- One shared Chromium `Browser`. **Each job gets its own new `BrowserContext`**, closed in `finally`.
- A crash of the Chromium process requeues every running job as `crash` (charged) and relaunches the browser.
- Claim capacity is round-robin across advertised types. The RPC enforces each type's global `max_concurrency` exactly.
- **Adding a type** needs:
  1. a worker module `agents/<key>/` with a `TypeSpec` (playbook, tool set, nav ceiling);
  2. a BDR `registry.py` `AgentTypeSpec` (seed schema, tool schemas, prompt, `prepare`, capture handler, help reasons, identity);
  3. an enqueue call site;
  4. a `browser_agent_types` row inserted by data script.

  No CHECK constraint changes.

---

## 4. The agent loop

```
claim -> prepare (BDR) -> new context -> playbook.run(job, checkpoint)
   Captured(artifact)      -> POST /rfi-capture (job -> applying) -> done
   NothingToDo             -> finish(succeeded, nothing_to_do)
   NeedsHelp(reason)       -> help flow (sec 6)
   NeedsAgent(reason, ctx) -> if type.llm_escalation_enabled and link eligible (5.3):
                                 LLM loop: screenshot -> POST /turns -> execute -> repeat
                                 until finish | stop | lease_lost | cancel
                              else NeedsHelp(unexpected_page | ambiguous)
```

### 4.1 Tools

The schemas are neutral JSON Schema, served by BDR with each turn and identical for all five providers. `executor.py` is the only choke point: every action writes a ledger row with URL before and after, duration and redacted arguments, plus a screenshot (JPEG q70, masked).

**Browser tools:**

| Tool | Args | Rules |
|---|---|---|
| `navigate` | `{url: string, https, <=2048}` | Host and path must match the type's code ceiling (minus `host_narrowing`), checked by BDR and again by the worker. Rechecked on every main-frame navigation and redirect. Query keys matching `(?i)token|sig|signature|key|auth|code|session|passport|x-amz-` are refused. A violation goes to `about:blank`. |
| `read_page` | `{max_chars?: int<=20000}` | Accessibility snapshot with `ref_N`. Values of `password`, OTP-like and secret-filled inputs are replaced with `[hidden]`. |
| `find_text` | `{query: string<=200}` | |
| `click` | `{ref: string}` | Refs only. Reports `stale_ref`. |
| `type_text` | `{ref, text<=500, submit?: bool}` | Refused on `type=password`, `autocomplete=one-time-code`, any input whose name, id or label matches `(?i)otp|passcode|mfa|verification|2fa|code|password`, and any page on a login host. |
| `select_option` | `{ref, value<=200}` | |
| `press_key` | `{key: enum[Enter,Tab,Escape,ArrowUp,ArrowDown,PageUp,PageDown]}` | |
| `scroll` | `{direction: enum[up,down], amount: int 1..10}` | |
| `go_back` | `{}` | |
| `wait_for` | `{text?: string<=200, seconds?: int 1..10}` | |

**Site tools** (adapter `procore`, identity `procore`):

| Tool | Args | Notes |
|---|---|---|
| `ensure_login` | `{host: enum[app.procore.com, us02.procore.com]}` | Runs the login playbook (4.2). Returns `{ok, outcome}`. The model never sees credentials. |
| `procore_resolve_company` | `{host, project_id: string ^\d{1,20}$}` | REST `/rest/v1.0/companies`, or the project-home redirect. |
| `procore_list_rfis` | `{host, project_id, company_id}` | Stores an `rfi_list_json` artifact. The model gets at most 500 `{id, number, revision, subject, status, updated_at}`. |
| `capture_rfi_detail` | `{host, project_id, rfi_id, company_id}` | Stores the envelope `{request_path, status, captured_at, body}`. The model gets `{number, subject, status, revision, attachment_count}` only. |

The REST calls use `context.request`, which shares the job context's cookies. The executor itself enforces the vendored `RFI_ALLOWED_PATHS` regexes, a JSON content type and an 8 MB limit, because `APIRequestContext` bypasses `route`.

**Help tools:**

| Tool | Args | Notes |
|---|---|---|
| `request_help` | `{reason_code: enum[access_denied,not_found,ambiguous,unexpected_page,needs_info,other], message: string<=1000}` | Attaches a screenshot automatically. Blocks, polling every 3 s until `hold_until`. Returns `{messages, files}` or `{parked:true}`. |
| `reply_help` | `{text<=2000}` | |
| `attach_helper_file` | `{artifact_id, kind: enum[question,answer]}` | Becomes a contribution. Only `attachable` files. |
| `add_helper_note` | `{message_ids: int[1..5]}` | Becomes a contribution with the verbatim text. |

**Finish:**

| Tool | Args |
|---|---|
| `finish` | `{outcome: enum[captured, nothing_to_do, cannot_complete], summary<=500, error_kind?: enum[not_found, access_denied, unsupported, other]}` |

`captured` is accepted only when a valid detail artifact exists. The executor then submits the capture.

**Never exposed:** `fill_secret`, JavaScript evaluation, uploads, downloads, console or network reads, new tabs (popups are closed and logged), `file://`, `chrome://`. Helper secrets are consumed only by the login playbook.

### 4.2 Credentials kept out of the model
- **Primary login path:** the BDR-brokered jar (5.5). No password on the PC.
- **Fallback browser login,** `agents/pm_apps/procore/login.py`:
  1. Runs on a dedicated `login_page` object.
  2. Before every fill, asserts `urlparse(page.url).hostname == 'login.procore.com'` and the path is in `_LOGIN_LOGIN_PATHS`.
  3. Fills the email, then the password, and submits.
  4. Classifies the result by URL, selector and cookie: `ok`, `password_rejected`, `mfa`, `captcha`, `challenge`, `sso` or `unexpected`.
  5. On `ok`, posts its cookies back.
  6. Drops its references to the password. (A Python `str` cannot be zeroed. This is documented, and that is why release is a gated fallback.)
- **Context options:**
  - Chromium launched with `--password-store=basic`;
  - context prefs: `credentials_enable_service=false`, `profile.password_manager_enabled=false`;
  - no persistent profile.
- **Never in model input:** the password, the email, the cookies, the OTP. Playwright tracing, HAR and `DEBUG=pw:*` are hard-disabled, and the service clears `DEBUG` at start.
- **Screenshots:** `mask=` covers password inputs, OTP-like inputs and every element filled from a secret. The executor keeps a per-job `secret_filled_refs` set and the consumed values in memory for scrubbing only, for the life of the job.
- **Redaction on both sides:**
  - The worker scrubs the live password, consumed OTPs, the worker token, cookie values, and the `sig`, `token`, `X-Amz-*` and `passport` URL parameters, in plain, URL-encoded and base64 forms.
  - BDR scrubs again before persisting (section 1, step 4).
  - A worker-side hit emits `security.secret_scrubbed` and fails the job.

### 4.3 Domain ceiling and read-only guard (`browser/guard.py`)
- **Context:** `service_workers='block'`, `accept_downloads=False`, viewport 1366x768, headless.
- **Code ceilings** live in `agents/<type>/ceiling.py`. For `rfi_from_email`:
  - `nav`: host `app.procore.com` or `us02.procore.com`, with paths matching:
    - `^/\d{1,20}/project/(home|rfi(/list)?|rfi/show/\d{1,20})/?$`
    - `^/webclients/host/companies/\d{1,20}/projects/\d{1,20}/tools/rfis(/\d{1,20})?/?$`
  - `login`: `login.procore.com` with `_LOGIN_LOGIN_PATHS`, used only by the login playbook.
  - `resource` (GET only): `*.procore.com`, `*.procoretech.com`, `*.procorecdn.com`.
  - `login_challenge` (GET plus POST, only while `request.frame.page is login_page`): `challenges.cloudflare.com`, `www.google.com/recaptcha/*`, `www.gstatic.com/recaptcha/*`.

  `host_narrowing` in Settings can only remove entries. A new required host means a new signed release.
- **`context.route('**/*')`:**
  - GET, HEAD and OPTIONS to ceiling hosts pass;
  - POST passes only when `request.frame.page is login_page`, the host is `login.procore.com` and the path is in `_LOGIN_LOGIN_PATHS`, or it targets the challenge hosts from that same page;
  - everything else aborts with an `http.blocked` step (method, host, path, no query), limited to 1 row per host and method per job.
- **`context.route_web_socket('**/*')`:** close every socket.
- **Stated limit:** Procore UI features that read through POST (some searches and filters) will not work in LLM browsing. The model's primary tools are the REST site tools. The live dev run measures which POSTs the RFI page needs (10.4). Read-only is binding.

### 4.4 Caps

| Cap | Default | Enforced by |
|---|---|---|
| Model steps per job | 40 | BDR `/turns` |
| Tokens per job | 400k | BDR |
| Cost per job | $2.00 | BDR (estimated takeover cost included) |
| Active time per job, help wait excluded | 20 min | BDR (heartbeat deltas) plus the worker locally |
| Global daily spend | $25 (alerts at 50% and 100%) | `/turns` only |
| Navigations per job | 40 | Worker |
| Charged attempts / interruptions | 3 / 20 | Claim RPC and sweeper |

A cap hit replies `stop` and the worker opens `cap_reached` help (scope `job:`). The helper's first **Retry** resets the job's counters (logged). After that, only IT Admin can Retry.

### 4.5 Checkpoint and resume (RFI type)

Checkpoint shape:

```
{phase: seeded|session_ready|located|detail_captured|submitted,
 company_id, rfi_id, detail_artifact_id, last_url, contributions_sent:[...]}
```

| Phase on resume | Action |
|---|---|
| `seeded`, `session_ready`, `located` | Rerun from the session step. Reads are idempotent. |
| `detail_captured` | If `captured_at` is less than 30 min old, go straight to `/rfi-capture`. Otherwise recapture. |
| `submitted` | Nothing to do; the job is already `applying` on BDR. |

In LLM mode, `browser_agent_turns` is the transcript, and the worker also PUTs `{last_url}` every 5 steps.

---

## 5. The RFI agent (`rfi_from_email`, adapter `procore`)

### 5.1 Trigger and seeding (`pm_rfi_sync.scan_emails`)

**Pass A, unchanged detection.** `extract_rfi_links` on filed PM emails, plus auto-link through `_trusted_for_autolink` and `_procore_conflict`. `_email_trigger` collects the matching links and calls the new `_queue_email_rfi(sb, settings, email, project_id, link_row, rfi_links)`:

- **Agent active** (`settings.browser_agents_active` and `browser_agents.type_enabled('rfi_from_email')`, cached 30 s): one job per RFI link, at most 5, through `browser_agents.queue.enqueue(...)`.
  - `dedupe_key = f"email:{email_id}:rfi:{rfi_id}"`.
  - `input`:
    ```
    {platform:'procore', host, external_project_id, rfi_id, company_id|null,
     rfi_number_hint|null, event_hint: closed|response|other,
     email_message_at, source_email_ids:[email_id]}
    ```
  - `company_id` comes from the link row, then `pc.extract_company_id`.
  - The hints come from the subject only, by strict regex: `#\s*([A-Za-z]+\s*-?\s*\d+(?:\.\d+)?)` and `RFI Closed|Response to RFI`.
  - **The email body is never put in the seed.**
- **Otherwise:** the legacy `enqueue_run(sb, pid, "email", ...)`, unchanged.

**Pass C, new and deterministic** (when the agent is active). An email qualifies when all of these hold:
- inbound, filed to a PM project whose link is `platform='procore'` with `sync_enabled`;
- sender matches `_PROCORE_SENDER_RE`, and the sender's region matches the link host (`@us02.procoretech.com` needs `us02.procore.com`; anything else needs `app.procore.com`);
- subject matches `RFI (Closed|Response to RFI)?.*#\s*<num>`;
- the body has no RFI show link and does not contain `/company/planroom/`.

It seeds `rfi_id=null` with `rfi_number_hint`, and `dedupe_key = f"email:{email_id}:locate"`. It uses its own checkpoint, `pm_rfi_email_scan_state.subject_checkpoint`. That one column is added in 0157:

```sql
alter table pm_rfi_email_scan_state add column if not exists subject_checkpoint timestamptz;
```

**Pass B** (classifications) stays legacy and inert. The future 0154 classifier hook is `_queue_email_rfi(..., rfi_links=[])`, which makes a locate job when the subject yields a hint. Classifier triggering is out of v1.

**Checkpoint safety.** For Passes A and C, `new_checkpoint` is held at the `updated_at` of the earliest email whose enqueue raised. `dedupe_key` makes re-reading harmless.

**Backfill.** When `link.backfilled_at` is null, the enqueue also calls `enqueue_run(sb, pid, "manual")` with `requested_by=None`. It joins an open full run.

### 5.2 `prepare(job)` at claim (`services/browser_agents/types/rfi_from_email.py`)

| Condition | Result |
|---|---|
| The link was removed, disabled, or now points to a different Procore project or host | `failed / no_link`, no notice |
| Local row has `_finished(local)`, or `local.external_synced_at >= input.email_message_at` | `succeeded / already_current` |
| Otherwise | Returned to the worker |

### 5.3 Extraction on the worker (playbook, no model)

1. **Session.** `POST /identities/procore/session`, then `context.add_cookies(cookies)`. Then:

   | Session status | Worker does |
   |---|---|
   | `ok` | Continue |
   | `spacing` | Wait while holding the slot if `retry_at` is 120 s away or less. Otherwise `/requeue {login_spacing, retry_at}`. |
   | `locked` / `login_failed` | `NeedsHelp(login_locked / login_failed)`, scope `login:procore` |
   | `browser_login_needed` | Fallback browser login (5.5). If that ends in `mfa` / `captcha` / `sso`, `NeedsHelp`, scope `login:procore`. |
   | `help_open` | Join the help request and park |

2. **Company.** Missing: `procore_resolve_company`.
3. **Locate** (Pass C only). `procore_list_rfis`, then an exact match on the normalized number (lowercase, whitespace removed, `-` kept). Several rows with the same number: take the highest `revision`/`current_revision`. Zero or still ambiguous: `NeedsAgent("locate")`.
4. **Capture.** `capture_rfi_detail`:

   | Result | Action |
   |---|---|
   | 401 or login redirect | Session again with `reject_jar_logged_in_at`, retry once. Still refused: fallback browser login. |
   | 403 / 404 | `NeedsHelp(access_denied)`, scope `link:<project>` |
   | Cloudflare challenge or non-JSON | Navigate the page to the RFI show URL (lets Chromium pass a JS challenge), wait 10 s, retry once. Still blocked: `NeedsHelp(captcha)`, scope `login:procore`. |
   | Draft | `NothingToDo` |
   | OK | Upload `rfi_detail_json`, checkpoint `detail_captured` |

5. **Submit.** `POST /rfi-capture`. The job becomes `applying`. The worker frees the slot.

**Escalation eligibility.** `NeedsAgent` runs the LLM only when the type has `llm_escalation_enabled` **and** `pm_external_links.linked_via = 'manual'`. Auto-linked projects never get model browsing until a person saves the link (verify that PUT `/pm-link` writes `linked_via='manual'`, and add it if not). Otherwise `NeedsAgent` becomes `NeedsHelp(unexpected_page | ambiguous)`.

The REST detail JSON is the only data source, so `map_detail` stays the single mapping.

### 5.4 Writing through existing code (BDR side)

**`POST /rfi-capture` validation** (`services/browser_agents/pm_apps/procore.py:validate_capture`):
- The artifact belongs to this job, has kind `rfi_detail_json`, its sha256 matches, and it is valid JSON.
- `request_path == f"/rest/v1.0/projects/{seed_project}/rfis/{rfi_id}"`, and `body.id == rfi_id`. In locate mode, `rfi_id` must appear in this job's `rfi_list_json` artifact.
- `captured_at` is no more than 30 min before receipt (by server clock), and no more than 2 min in the future.
- The live `pm_external_links` row still has `platform='procore'` and the same host and project.
- Attachment URLs are **not** checked at capture time. The shim checks them at download time.

A failure returns 422 `capture_invalid` and the job fails.

**`pm_rfi_sync.py` changes:**

1. **`run_pending`.** When `run["trigger"] == "agent"`, call `process_run(sb, run, settings, session_factory=rfi_session.factory_for(run), agent=True)`.

2. **`process_run(..., agent=False)`.** When `agent` is true:
   - skip the `procore_configured` check;
   - pass `target_only=True` and `provisional=True` to `sync_project`;
   - call `contributions.apply_pending(sb, project_id)` before `_finish(done)`;
   - **never call `_update_link_status`** (on success or failure);
   - call `notify_after_run` only when `link.backfilled_at` is set (otherwise the backfill run's `pm_rfi.linked` notice covers it);
   - record `stats["agent_outcome"]`.

3. **`sync_project(..., target_only=False, provisional=False)`.** When `target_only`:
   - take the targeted branch even when `backfill` is pending, and set `stats["backfill"]=False`;
   - **stale guard:** skip with `stats["agent_outcome"]='skipped_stale'` when the local row has `external_synced_at >= capture.captured_at`, or `external_updated_at >= body.updated_at`.

   `_finished(local)` skips stay as they are.

4. **`apply_detail(..., provisional=False, import_method='web_session', agent_job_id=None)`.**
   - When `provisional`, do not call `_mark_complete`. `external_updated_at` stays null, so the next daily sweep re-reads the row and its files.
   - `import_method` and `imported_by_agent_job_id` are written **on insert only**.
   - A sweep update overwrites nothing except normal fields. On a sweep update of a row whose `import_method='agent'`, set `import_method='web_session'`, because the data is now verified.

5. **`AgentCaptureSession`** (`services/browser_agents/rfi_session.py`, about 60 lines):

   | Method | Behavior |
   |---|---|
   | `get_rfi(p, r, c)` | Returns the captured body when `r` is the target; otherwise raises `pc.ProcoreForbidden`. |
   | `resolve_company_id(p)` | Returns the captured company id. |
   | `list_rfis` | Raises `RuntimeError` (never reached with `target_only`). |
   | `download(url, dest, max_bytes)` | Requires `projectId` and `companyId` query parameters equal to the seed project and link company (parsed with `pc._query_id`); otherwise `ProcoreForbidden`. Delegates to `pc.ProcoreSession(pc.ProcoreConfig(email="", password=""), pc.MemorySessionStore()).download(...)`, which already allows only the storage host plus one `*.amazonaws.com` redirect and the cap. **Re-raises a storage 403/404 as `pc.ProcoreTransient`**, so the file is retried later and not marked refused. |
   | `close()` | Closes the download session. |

   Note: `download` goes through the process-wide `_pace`, which is acceptable.

6. **`contributions.apply_pending(sb, project_id)`** (`services/browser_agents/contributions.py`). For each pending row whose `(project_id, external_id)` RFI exists:
   - **File:** copy the artifact blob into `project-files` at `storage.build_object_path(pid, "pm/rfi", filename)`. Create `pm_documents` (category `rfi`, note `"Added from agent help {help_id} [agent-help-file:<sha256>]"`, reused through a marker lookup). Insert `rfi_attachments(kind, doc_key, external_file_id='help:'||sha256)`, deduped by the 0156 unique index.
   - **Note:** append `{text, by, at, help_id, contribution_id}` to `rfis.external_supplement` when the contribution id is not already present.
   - Set `applied_at` and `applied_rfi_id`.

   It is also called by the sweeper, so a contribution for an RFI that the account cannot yet read applies when the RFI first appears. `purge_imported` on Unlink drops `help:` attachments; that is documented in `docs/BROWSER_AGENTS.md`.

7. **Retry after `apply_failed`.** BDR increments `job.apply_attempt`, re-inserts a run for the same artifact (new unique key), and sets the job back to `applying`.

**Reused unchanged:** `_load_local`, `match_internal`, `_retire_stale`, `sync_attachments`, `_download_to_documents`, the `[procore-file:<id>]` marker, the 150 MB cap, `_finish`, `notify_after_run`, `_reject_read_only`, and the per-project busy rule in `claim_next`.

### 5.5 Shared Procore login and session broker

**`app/services/procore_creds.py`:**

| Function | Behavior |
|---|---|
| `password(settings, identity='procore')` | Always a fresh DB read of `browser_agent_secrets`; env `PROCORE_LOGIN_PASSWORD` is the fallback. Used only at login time. |
| `configured(settings)` | Email set and (DB secret or env). Cached 60 s per process; `harvester_for` calls it per row. |
| `config(settings, host)` | Builds `ProcoreConfig` with the DB password. Replaces `rfp_harvest.procore_config`. |

Every `settings.procore_configured` read listed at the top of this spec is replaced by `procore_creds.configured(settings)`, except the property definition. A test greps `app/` and fails on any other reference.

**`procore_client.py` changes:**
- `SessionStore` gains `reserve_login(min_interval: int, source: str) -> dict`. `MemorySessionStore` implements it in memory. `rfp_harvest._SessionStore` calls the RPC. `_SharedLockStore` delegates to the shared `procore` row.
- `ensure_session` replaces the read of `last_login_attempt_at` with `reserve_login(...)`. When not granted, it raises `ProcoreUnavailable(locked_until=retry_at)`.
- `record_login(ok=False)` in `_SessionStore` uses `procore_record_login_failure`.
- A failed login raises `ProcoreUnavailable` with a new attribute `.step`.
- New `ProcoreSession.probe()`: a GET of `/rest/v1.0/companies` through the login-retry path.

**Broker** (`services/browser_agents/procore_broker.py`, called by `/identities/procore/session`). It refuses with `help_open` when an open `login:procore` help exists and the caller is not the holder. Otherwise it opens `pm_rfi_sync.open_session(host)` with `on_lock=_notify_lock`:

| Situation | Result |
|---|---|
| Stored jar for the same account, `logged_in_at` within 12 h, not equal to `reject_jar_logged_in_at`, and `last_used_at` within 10 min | Return the jar with no Procore call |
| Otherwise | `probe()`, which may run `ensure_session` |
| Success | Return `_export_cookies()` filtered to Procore domains; stamp `last_login_source` |
| Spacing / locked | `spacing` / `locked` |
| `.step` in `challenge`, `login_page`, `redirect` | `browser_login_needed` |
| Other steps | `login_failed` |

Every broker call writes a `browser_agent_login_attempts` row with `kind='broker'`.

**Browser fallback `begin`** grants only when all of these hold:
- `browser_login_fallback_enabled`;
- this job's last broker attempt within 5 min returned `browser_login_needed`;
- releases today are below the cap;
- the worker IP is allowed;
- no `login:procore` help is open unless this job is its holder;
- `procore_reserve_login(..., p_bypass_after_broker=true)` is granted.

Each release sends one IT Admin bell, `browser_agent.login_released`, and writes a ledger step.

**`/result`:**

| Outcome | BDR does |
|---|---|
| `ok` | `record_login(ok=True)` and `save_cookies(email, cookies)` with `last_login_source='agent:<worker>'`, so the daily sweep and RFP harvest adopt the jar through `_jar_is_newer`. |
| `password_rejected` | Atomic failure RPC. A lock fires `_notify_lock`, whose text becomes "Check the Procore password in Settings > Browser agents." |
| `mfa`, `captcha`, `challenge`, `sso`, `unexpected` | Set `last_error` only. Help scope `login:procore` follows, and other jobs then get `help_open`. |

**Password set in Settings:**
- resets `login_failures`, `locked_until` and `last_login_attempt_at` on the `procore` row (audited);
- auto-resolves an open `login:` help only after the next `ok` broker or browser login, never on a refused `begin`.

### 5.6 Exactly-once effects

| Effect | Guard |
|---|---|
| Job per email and RFI | `(agent_type, dedupe_key)` unique |
| LLM spend per turn | Turn primary key, `thinking_until` takeover, `billing_unknown` estimate |
| Ledger row | `(job_id, idem_key)` |
| Capture and apply | `pm_rfi_sync_runs_agent_uidx (agent_job_id, apply_attempt)` and `claim_next` |
| RFI row and Procore files | 0156 unique indexes, `apply_detail` collisions, `sync_attachments` |
| Helper contribution | `browser_agent_rfi_contrib_uidx`, `rfi_attachments (rfi_id, external_file_id='help:'||sha)`, the `pm_documents` marker, the supplement contribution id |
| Help notice | `scope_key` open-unique index, compare-and-set on `notified_at` and `reminder_count` |
| Helper secret | Compare-and-set on `secret_consumed_at` |
| Message consumption | `help_reads` primary key |
| Login counting | `procore_reserve_login` and `procore_record_login_failure` RPCs |

### 5.7 The old email path and its tests

**Release N.** The flag routes the enqueue, and `process_run` still handles `email` rows.

**Release N+1** (30 clean days after the prod release). Delete the legacy branch and its flag-off tests. `'email'` stays in the CHECK for history rows.

**`tests/test_pm_rfi_sync.py`:**

| Tests | Change |
|---|---|
| 433, 441, 446, 951, 960, 985, 1508, 1609 | Unchanged |
| 932, 972 | Parametrized `agent_off` / `agent_on`. With the agent on: one `browser_agent_jobs` row with the seed and `dedupe_key`, no `email` run, and a `manual` run only when the link is not backfilled. |
| 1469 | Keep. Add "Sync now does not join an `agent` run". |
| New | Pass C (Cobblestone subject-only queues `locate`; Boyd `email_boyd_rfi20.txt` does not); checkpoint held on enqueue failure; several links give several jobs; `process_run(agent=True)` never touches the link status. |

`_settings` defaults to `browser_agents_enabled=False`, so the current suite stays green unchanged.

---

## 6. Help flow end to end

1. **Open.** The playbook (`NeedsHelp`) or the model (`request_help`) calls `POST /jobs/{id}/help`. The scope comes from the reason code:

   | Reason codes | Scope |
   |---|---|
   | `login_failed`, `login_locked`, `mfa`, `captcha`, `sso`, `logged_out` | `login:<identity>` |
   | `access_denied`, `not_found` | `link:<project_id>`. After a Give up or Skip on that scope within 24 h, new requests are suppressed and the job fails `access_denied_suppressed`. `pm_external_links.last_error` is set; nothing else on the link changes. |
   | Everything else | `job:<id>` |

   The title comes from a server template per reason code and the RFI label.

2. **Notify once per request** (compare-and-set on `notified_at`; joining jobs never notify):
   - `notify_role(Role.ESTIMATING_ADMIN, project_id, "browser_agent.help_needed", <template text>, mirror_email=True, metadata={"help_id","job_id","reason"})`.
   - Login scopes also notify `Role.IT_ADMIN`.
   - If `notify_role` created zero rows, notify `Role.IT_ADMIN` instead. `notify_role` gains a return of the created count.
   - Email text is fixed per reason code and never includes model text, page content or screenshots.

3. **Reminders.** `browser_agent.help_reminder` (bell plus email) after `help_reminder_minutes` while unclaimed, at most 3. A lapsed claim re-arms it.

4. **Helper page** `/agents/help/{id}` (Estimating Admin or IT Admin). It shows:
   - the template title;
   - the agent message collapsed under "Automated agent text, may quote web content. Never enter your own passwords or codes here unless the title asks for a Procore code." (escaped, never auto-linked);
   - the masked screenshot;
   - the waiting jobs with their RFI and project.

   **Actions:**
   - **I'm on it** claims the request; it lapses after 30 min with no helper activity.
   - **Messages,** guarded by `client_msg_id`. Plaintext messages are rejected with 422 `looks_like_secret` when their HMAC matches the Procore password fingerprint, or when the whole message is 4 to 10 digits during a login scope ("Tick 'This is a code' to send it securely").
   - **Secret message,** shown only for login scopes. The client generates `secret_id`, and BDR encrypts before the single insert (AAD `help-secret|<secret_id>`). Plaintext never enters a DB call.
   - **Uploads:** multipart, a sync capped reader (`files._read_capped_sync`, new), 150 MB, a magic-byte sniff that must agree with the extension, an allowlist (pdf, png, jpg, docx, xlsx, txt are attachable; zip and dwg stay in the thread only), and a scan hook `services/browser_agents/scan.py` (no-op in v1).
   - **Buttons:** **Retry now**, **Skip: the daily sync will catch it**, **Give up**.

   Every action writes a `help.*` step on each waiter, and an `audit_log` row.

5. **Agent consumes the input:**
   - **Held page:** the heartbeat `help_updates` or the 3 s poll returns new messages. They become the `request_help` result, and the job calls `/consume`. The model may call `attach_helper_file`, `add_helper_note`, `ensure_login`, `reply_help` or `finish`.
   - **Login scope:** handled by the playbook only. The holder job uses a secret message for the OTP field (`/secrets/{id}/use`). **Retry now** after a password fix re-runs the session broker.
   - **Parked jobs:** a helper message or Retry requeues every waiter with `priority=10` and `resume_reason='help_answered'` (uncharged). The claim returns `unconsumed_help`, and BDR adds it as a turn. Secrets work only for a holder whose page is still held. After parking, the next attempt starts a new login.

6. **Park.** At `hold_until` the worker calls `/park` and closes the context.

7. **Resolve:**
   - A `job:` or `link:` request becomes `resolved` when its last waiter finishes.
   - A `login:` request auto-resolves on the next `ok` login.
   - Give up fails waiters `help_declined`. Skip cancels them `skipped_by_helper`.
   - Bells are dismissed with `dismiss_notifications(types=[...], metadata_eq={"help_id": id}, project_id=...)` per project. `dismiss_notifications` also accepts `metadata_eq` containing `help_id` as a valid scope when no project exists.

8. **Expire.** As in section 3.4: `login:` scopes stay open and email IT Admin.

9. **Limit stated plainly.** An interactive captcha has no remote fix until live view exists. The options are Retry later, Skip, or Give up.

---

## 7. Frontend surfaces (`/Users/thomasmoore/Desktop/BDR/bdr_fe`)

**Flag.**
- `lib/features.ts`: `browser_agents?: boolean`, `false` in `ALL_ENABLED`, `subAppForPath('/agents') -> null`.
- Backend: `/features` (`main.py:534`) and `core/features.py` from `settings.browser_agents_active`.

**Library** `lib/browserAgents.ts`:
- types matching contract C2 (section 12);
- API wrappers;
- `AGENT_HELP_ROLES = ["estimating_admin","it_admin"]`, `AGENT_VIEW_ROLES = [...AGENT_HELP_ROLES, "executive"]`;
- `canAnswerAgentHelp(role)`, `canAdminAgents(role, isDev)`;
- `agentStatusKey(job)` (modeled on `lib/aiQueue.ts`);
- `helpScopeLabel(scope)`;
- tests in `lib/__tests__/browserAgents.test.mjs`.

**Settings.**
- `app/(app)/settings/page.tsx` mounts `<BrowserAgentSettings/>` (`components/BrowserAgentSettings.tsx`) when `features?.browser_agents === true && (role === "it_admin" || profile.is_dev)`. The card summarizes enabled state, workers online, open help and today's spend, and links to the sub-page.
- `app/(app)/settings/browser-agents/page.tsx` follows the `rfp-ingestion` redirect pattern, with sections in `components/browserAgents/`:

| Component | Contents |
|---|---|
| `AgentGlobalSection.tsx` | Kill switch, Stop running jobs, daily cap, help timings, job TTL, browser-login fallback toggle and daily release cap |
| `AgentTypesSection.tsx` | Per type: enable, LLM escalation toggle, provider (5, Meta tagged "Experimental"), model from `/admin/models`, prices, **Test model** (required; "Save anyway" confirm), concurrency, caps, host narrowing (checkboxes from the code ceiling list returned by the API). Copies `ModelServerSettings.tsx`. |
| `AgentProvidersSection.tsx` | Per-provider data-use attestation (confirm checkbox plus note), who and when |
| `AgentSecretsSection.tsx` | Procore email read-only. Password write-only (Set, Replace, Clear) showing "Set on {date} by {name}, fingerprint {8 chars}" and the lock status (reuses `RfpHarvestStatusSection` data plus `last_login_source`). |
| `AgentWorkersSection.tsx` | List (status, online dot, last seen, IP, version, boot time, Chromium version with a warning past 30 days, slots, running jobs). **Add worker** shows the one-time code plus the install command and the release key fingerprint in a `Modal`. **Approve** for pending workers (shows hostname and enroll IP). Drain, Revoke, max slots, allowed types, allowed IPs. **Run self-test** queues a `canary` job. |
| `AgentReleaseSection.tsx` | Published versions with `min_version`, a desired-version select (refuses below `min_version`), and per-worker `bad_versions` |
| `AgentSettingsHistory.tsx` | `browser_agent_settings_changes` list |

**Agents pages** (`AGENT_VIEW_ROLES`; writes need `AGENT_HELP_ROLES`):
- `app/(app)/agents/page.tsx`: `Tabs` for Needs help, Running, Queued, Applying, Finished and Failed; filters for type, project, status and date; a worker-offline `Callout`.
- `app/(app)/agents/jobs/[id]/page.tsx`:
  - Header: type, RFI link, source email, status, outcome, attempts and interruptions, epoch, model, tokens, cost.
  - `components/browserAgents/AgentTimeline.tsx`: steps paged by `after_seq`, arguments in `<details>`, a `late` badge, a `redacted` badge, screenshots through `FilePreviewModal`, LLM turns with tokens, cost and a `billing_unknown` badge.
  - Cancel and Retry buttons.
- `app/(app)/agents/help/[id]/page.tsx`: `components/browserAgents/AgentHelpChat.tsx`, generalized from the `aiEstimating/ChatDrawer.tsx` bubbles and 2 s polling, with:
  - the pinned screenshot and the claim banner;
  - a compact `DropZone` with an "OK to attach to the RFI" checkbox (disabled for zip and dwg);
  - the secret checkbox (login scopes only);
  - the action buttons.
- `components/browserAgents/AgentActivityProvider.tsx`: copies `RfpEmailsActivity` (visible-tab polling every 30 s of `GET /browser-agents/summary`). Mounted in `app/(app)/layout.tsx`.

**Sidebar and bell.**
- `components/Sidebar.tsx`: shared `AGENTS_NAV {href:'/agents', labelKey:'nav.agents', icon: IconBot, roles: AGENT_VIEW_ROLES, featureFlag:'browser_agents'}` with a red open-help count. It is carried across sub-apps like `RFP_HUB_NAV`. Add `IconBot` to `components/ui/icons.tsx`.
- `components/NotificationsBell.tsx` `destination()`: a new block **before** `if (!n.project_id) return null` and before the PM branch, gated on `features?.browser_agents === true`:

| Type | Destination |
|---|---|
| `browser_agent.help_needed`, `browser_agent.help_reminder` | `/agents/help/{metadata.help_id}` |
| `browser_agent.help_expired`, `browser_agent.job_failed` | `/agents/jobs/{metadata.job_id}` |
| `browser_agent.worker_offline`, `.worker_pending`, `.worker_suspicious`, `.login_released`, `.provider_problem`, `.spend_alert` | `/settings/browser-agents` |

The metadata type gains `help_id`.

- Backend `notification_email.py`:
  - a `browser_agent.` branch at the **top** of `_deep_link`, before `if project_id:`;
  - `_TYPE_META` titles.

  The types are not added to `PM_NOTIFICATION_TYPES`.

**RFI page.**
- `RfisSection.tsx` Source column: a "Procore (agent, unverified)" badge when `import_method === "agent"`.
- `RfiDetailModal.tsx` `ProcoreFacts`: "Imported by agent from email on {date}; the daily sync will verify it", and an "Added from help" block rendering each `external_supplement[]` entry as plain text with "Supplied by {name}".
- `lib/types.ts` `Rfi` gains `import_method`, `imported_by_agent_job_id` and `external_supplement`. `_enrich_rfis` (`routers/pm_field.py:457`) passes them through.
- **No PmLinkCard change in v1.**

**Text.** `locales/en/translation.json` keys `browserAgents.*`, `nav.agents` and `notificationsBell.*`, English only, no em dashes. Help center: an "Agents" topic in `lib/help/registry.ts`.

**Human backend routes** (`bdr_be/app/routers/browser_agents.py`, plain `def`). The role sets go in `core/roles.py`:

| Routes | Roles |
|---|---|
| `/browser-agents/admin/*` | `BROWSER_AGENT_ADMIN_ROLES = (Role.IT_ADMIN,)` or `is_dev` |
| `/browser-agents/summary`, `/jobs*`, `/help*` reads | `BROWSER_AGENT_VIEW_ROLES = (ESTIMATING_ADMIN, IT_ADMIN, EXECUTIVE)` |
| Help writes, cancel, retry | `BROWSER_AGENT_HELPER_ROLES = (ESTIMATING_ADMIN, IT_ADMIN)` |
| `llm_request` / `llm_response` artifact URLs | Admin only |

---

## 8. Worker package (`/Users/thomasmoore/Desktop/BDR/bdr_be/agent_worker`)

```
agent_worker/
  pyproject.toml            # py3.12; exact pins + hashes (uv.lock): playwright, httpx, pydantic, cryptography, pywin32. No LLM SDKs.
  launcher/launcher.py      # stable; runs versions\<current>; local-health rollback; bad_versions
  bdr_agent/
    __main__.py             # enroll | run | selftest ; installs httpx redact filter; clears DEBUG
    protocol.py             # vendored copy of app/services/browser_agents/protocol.py (drift test)
    local_config.py         # C:\ProgramData\BDRAgent\worker.json {api_base_url, worker_id}; atomic write + .bak
    tokenstore.py           # DPAPI (service-user scope); atomic write + .bak
    client.py               # retries with jitter, lease/boot headers, idempotency keys, 409 mapping
    heartbeat.py            # dedicated thread + own httpx.Client; renew, cancel, orphans, help_updates
    supervisor.py           # hello, config refresh, claim loop, slot pool, drain, loop-lag watchdog
    slot.py                 # one job: context lifecycle, finally-close, timeouts, crash -> requeue(crash)
    spool.py                # per-job jsonl spool, late-step upload
    browser/
      browser.py            # one Chromium per worker, relaunch on crash, prefs, --password-store=basic
      guard.py              # route + websocket guard, nav guard, login_page gating, ceiling
      executor.py           # all tools; second policy check; masked screenshots; ledger rows; secret scrub
      snapshot.py           # accessibility tree -> ref_N, hidden values
    agent/loop.py           # playbook -> NeedsAgent -> /turns loop; caps; checkpoints
    agent/playbook.py       # Playbook base, phases, NeedsHelp / NeedsAgent / NothingToDo / Captured
    redact.py               # mirrors core/redact.py + live-secret scrub
    agents/
      __init__.py           # REGISTRY {key: TypeSpec}
      pm_apps/procore/{login.py, rest.py, paths.py, ceiling.py}   # paths.py vendors RFI_ALLOWED_PATHS
      rfi_from_email/{playbook.py, tools.py}
      canary/{playbook.py, ceiling.py}
    seams/screencast.py     # start_screencast(on_frame) stub -> NotImplementedError
  install/install.ps1       # bakes release public key; prints its fingerprint
  install/winsw/BDRAgent.xml
  tests/
```

**Hosting.**
- One WinSW service, **BDR Agent**, runs `launcher.py` as the standard local user `bdr-agent` with "Log on as a service". It is not LocalSystem.
- Delayed automatic start; restart on failure after 10 s, 30 s, then 60 s.
- Headless Chromium, which works in Session 0 and needs no autologon.
- The headful at-logon fallback is not built.

**Browser.** Playwright's bundled Chromium at `C:\ProgramData\BDRAgent\ms-playwright`. An ephemeral context per job, and nothing persistent except worker.json, the token, versions and the spool.

**Install (once).**
1. IT Admin clicks Add worker and gets a 20-character code, the command and the expected key fingerprint.
2. As Administrator, on the PC:
   ```
   irm https://<api>/agent-worker/v1/installer.ps1 -OutFile $env:TEMP\bdr-install.ps1
   powershell -ExecutionPolicy Bypass -File $env:TEMP\bdr-install.ps1
   ```
3. The installer prints the release public key fingerprint. **The owner compares it with the fingerprint saved in the password manager at keygen** before typing `yes`.
4. The installer then:
   - creates `bdr-agent`;
   - installs `uv` and Python 3.12;
   - downloads the desired release and verifies its sha256 and Ed25519 signature;
   - installs into `versions\<v>` (`uv pip install --require-hashes`);
   - runs `playwright install chromium`;
   - asks for the code and calls `/enroll`;
   - stores the DPAPI token;
   - writes `worker.json`;
   - registers and starts the service.
5. IT Admin clicks **Approve** in Settings.

**Update.**
1. On the Mac, `bdr_be/scripts/publish_agent_worker_release.py` builds the bundle, signs `{version, sha256, min_version, created_at}` with the offline key, and uploads it with the service role. There is no UI upload route.
2. IT Admin picks the desired version.
3. The worker refuses any version below its embedded and the manifest's `min_version`, or listed in `bad_versions`. Otherwise it drains (no new claims, at most 10 min, then `/requeue {drain}`), downloads, verifies, installs side by side, writes `pending.txt` atomically and exits.
4. The launcher starts the new version. **Health is local only:** the process starts, config loads and Chromium launches within 3 min. If not, or after 3 crashes in 10 min, it reverts to `current.txt` and adds the version to `bad_versions`, which is reported on the next `/hello`. When BDR is unreachable it waits and never rolls back for that reason.

**Atomic files.** Every write is temp file, flush and fsync, then `os.replace`, with the previous copy kept as `.bak`. Corrupt JSON falls back to `.bak`.

**The owner never edits config on the PC.** Model, provider, caps, slots, allowed types and IPs, password, drain, revoke and code version are all set in BDR. The exception: re-enrolling after a revoke needs one installer run.

**Live view and remote control: seam only, not built.**
- `jobs.control_mode` (CHECK widened later to `operator`);
- `workers.control_capabilities`;
- the heartbeat `controls` field;
- the reserved `/jobs/{id}/live` route;
- `seams/screencast.py` (CDP `Page.startScreencast`);
- the rule: any remote input arrives as `helper`-actor tool calls through `executor.py`, so it is guarded, fenced and audited, and the LLM loop pauses while `control_mode='operator'`.

---

## 9. Security

| Threat | Controls |
|---|---|
| Stolen worker token | IP allowlist; boot fencing and the hello rate limit (suspends on abuse); `allowed_types` set by admin; no password in the normal path; browser release only right after a broker challenge failure, capped per day and alerted; IDOR checks on help, messages and artifacts; a per-turn global spend cap with a 50% alert. Response: Revoke (instant), change the Procore password, re-enroll. |
| Stolen or compromised PC | DPAPI token, BitLocker, no cookies or password at rest (ephemeral contexts), no provider keys. Same response. |
| Forged capture from a compromised worker | Provisional writes (never `_finished`; the next sweep overwrites), the stale guard, the live link check, the `projectId`/`companyId` check on every attachment download, path and id checks. |
| Database read leak | AES-GCM secrets under `BROWSER_AGENT_SECRETS_KEY` (Railway env), the HMAC fingerprint under a separate key, token and code hashes only. PostgREST error `details` are stripped from logs for `browser_agent_*` tables (`main.py` handler). |
| Prompt injection | Untrusted wrapping, a code-fixed tool and nav ceiling, the network read-only guard, no secrets reachable by the model, one action per turn, caps, data only from executor capture plus verbatim helper text, no escalation on auto-linked projects, collapsed and warned agent text in the help UI, the plaintext-secret block, fixed-text emails. |
| Forged Procore email | Seeds come only from `extract_rfi_links` or the strict subject regex. Auto-link keeps `_trusted_for_autolink` and `_procore_conflict`. The capture is validated against the live link. Worst case: a provisional read of an RFI the G3 account can see. |
| SSRF through attachment URLs | `ProcoreSession.download` (storage host, one `*.amazonaws.com` redirect, cap) plus the seed project and company match. |
| Writes to Procore | The code ceiling, GET-only routing, login POST gated to the login page object, token-query URL refusal, no write tools. |
| Malicious helper upload | Size cap, magic-byte sniff, allowlist, zip and dwg never attached, scan hook, never opened by the worker or the model. |
| Insider or log tampering | Append-only triggers (only payload redaction is allowed and is recorded), `audit_log` for every human action (`browser_agent.settings_update`, `.secret_set`, `.worker_enrolled`, `.worker_approved`, `.worker_revoked`, `.help_reply`, `.job_cancel`, `.job_retry`, `.rfi_applied`, `.step_redacted`). |
| Secret leakage | Scrub before persist, LLM tripwire with HMACs of consumed secrets, tracing and HAR off, masked screenshots and hidden snapshot values, `no-store`, global httpx URL redaction, write-only secret routes (tests inspect serialized responses), a profile-free worker. |
| Supply chain | Hash-pinned dependencies, no LiteLLM or browser-use, signed releases with a `min_version` floor, out-of-band key fingerprint check at install, a Chromium-age warning. |
| Data to providers | A provider is selectable only after attestation, which is recorded in `config_snapshot`. Meta is experimental. |
| Least privilege | **Worker:** only section 3 endpoints, scoped to its leases. **Estimating Admin:** help, cancel, retry, view. **IT Admin:** everything. **Executive:** read-only. **Estimator:** nothing. |

**Config block** (`core/config.py`, a new feature section):
- `browser_agents_enabled=False`
- `browser_agent_secrets_key`, `browser_agent_secrets_key_id="k1"`, `browser_agent_secrets_old_keys=""`
- `browser_agent_fingerprint_key`
- `google_api_key`, `xai_api_key`, `meta_api_key`
- `browser_agent_lease_seconds=90`, `browser_agent_sweep_seconds=20`
- `browser_agent_release_pubkey`
- `trust_forwarded_for=False`
- Derived: `browser_agents_active = browser_agents_enabled and bool(supabase_url)`.

**Validator `_validate_browser_agents`.** When enabled:
- both keys must be base64 of 32 bytes;
- `PROCORE_LOGIN_EMAIL` must be set;
- `pm_rfi_sync_active` must be true;
- in production, `trust_forwarded_for` must be true.

The Settings API also refuses to enable `rfi_from_email` while `pm_rfi_sync_active` is false.

**Key rotation:** `scripts/rotate_agent_secrets_key.py`.

**Dependencies** (`pyproject.toml`): `cryptography` (explicit) and `google-genai` (exact pin). `anthropic` and `openai` are already present.

---

## 10. Test plan

### 10.1 Backend unit tests (FakeDB style, no network), `bdr_be/tests/`

| File | Covers |
|---|---|
| `test_browser_agents_queue.py` | Claim respects the kill switch, per-type concurrency, `allowed_types`, advertised types, `next_attempt_at` and expiry. **First claim with null `resume_reason` charges 1.** Help, spacing, shutdown, drain and provider-outage resumes are uncharged; crashes are charged. The epoch goes up on every claim. A stale epoch or boot gets 409 on every fenced call. Late steps are accepted only with a matching claim step. `hello` requeues other boots uncharged and suspends after 6 per hour. The heartbeat renews an expired but unreclaimed lease. The sweeper does not reclaim while the worker is seen, or during sweeper startup grace. `prepare` gives `already_current` (finished, or `synced_at >= message_at`) and `no_link`. `prepare` raising releases uncharged. Cancel propagation. Offline bell then email, once each. `config_version` is not bumped by heartbeats. The `applying` reconcile covers done, failed, abandoned and lost runs. |
| `test_browser_agents_turns.py` | Repeated `prev_turn_no` returns the stored action with one fake LLM call. 202 while thinking. A stale-thinking takeover marks `billing_unknown` and charges the estimate. Old-epoch turns become `abandoned` and are never returned. Each cap gives `stop`. The daily cap blocks `/turns` but not claims. Schema, host and path violations go back to the model. Exactly one action per reply. The tripwire uses live and consumed HMACs. Scrub happens before persist (nothing secret in steps or artifacts). Bidi and zero-width stripping. Screenshot trimming. The `llm_call_log` mirror. Outage gives `stop` plus requeue and one alert. A config error disables escalation and alerts once. |
| `test_browser_agents_llm_adapters.py` | Recorded fixtures per provider, the `base_url` mapping, the Google header-only key, cost math. |
| `test_browser_agents_secrets.py` | AES-GCM round trip. A changed AAD or name fails. Rotation with old keys. No route echoes a secret (serialized responses). Fingerprint. The validator. `procore_creds.password` reads the DB fresh. `configured` caches. Setting a password resets the lock. **The grep test: no `procore_configured` outside `config.py` and `procore_creds.py`.** |
| `test_browser_agents_auth.py` | Enrollment single use, expiry and DB attempt counter. Pending workers are limited. Approve. Revoke. IP allowlist through `client_ip`. JWT refused on worker routes, worker token refused on human routes. aal2 never involved. IDOR on help, messages, secrets and artifacts. `no-store` header. |
| `test_browser_agents_ledger.py` | UPDATE, DELETE and TRUNCATE are refused by the trigger simulation. The redaction update is allowed only on payload columns. **Deleting a profile that owns rows in every new table succeeds** (FakeDB plus SQL smoke). |
| `test_browser_agents_help.py` | Scope dedupe (`login:`, `link:`, `job:`). The 24 h link suppression. One notice, then up to 3 reminders. Claim lapse. IT Admin fallback when no Estimating Admin exists. `login:` never expires while locked and emails IT Admin. Secret encrypt-before-insert with `secret_id` AAD. One-time use limited to the holder. Purge keeps the HMAC. Exactly-once consumption. Park, then requeue on reply. Plaintext secret-like messages refused. Email text has no agent text. Dismissal via `metadata_eq` `help_id`. One helper Retry on `cap_reached`, then IT Admin only. |
| `test_rfi_agent.py` | Capture validation (wrong id, project or host, stale `captured_at`, foreign artifact, locate id not in list). `process_run(agent=True)` with `AgentCaptureSession` on `detail_27914290.json` and `detail_27699412.json`: `list_rfis` never called, no `_update_link_status`, `external_updated_at` stays null (provisional), files downloaded through the cookie-less path. **A storage 403 leaves the row incomplete and its file `retry`, not `refused`.** A `projectId` mismatch refuses. The stale guard gives `skipped_stale`. A not-backfilled link gets target only and no summary notice. `import_method` is set on insert only and becomes `web_session` on sweep update. The sweep later completes the row. Contributions apply once, and apply later when the RFI appears. Double submit gives one run. Retry after `apply_failed` creates `apply_attempt+1`. An agent run waits behind a running daily run. |
| `test_procore_login_lock.py` | `reserve_login` is atomic. Concurrent `ensure_session` and broker never both log in. The failure increment is atomic. An unreported browser attempt is counted after 5 min. `help_open` refusal for non-holders. Bypass only right after a broker challenge failure. `save_cookies` from the worker is adopted by `_jar_is_newer`. |
| `test_pm_rfi_sync.py` | Section 5.7. |
| `test_notification_email.py` | `_deep_link` for each `browser_agent.*` type, with and without `project_id`. |
| `test_migration_0157.py` (style of `test_migration_0136.py`) | CHECKs, unique indexes, immutability triggers, grants revoked from public, `search_path` pinned. |

### 10.2 Worker tests

These run under pytest plus Playwright against a local aiohttp fixture that imitates Procore: login chain, bad password, MFA page, Turnstile page, RFI JSON, a 401 jar, a page that POSTs on load, a GET-with-token link, and an injection page.

- The guard blocks POSTs, websockets, off-ceiling paths and token-query URLs, and logs `http.blocked`.
- Login POST is allowed only from `login_page`.
- `type_text` is refused on OTP-like fields.
- Snapshot values are hidden after a secret fill. Screenshots are masked.
- Logs, steps and spool never contain the secret.
- The Chromium profile has no saved password (grep of the temp user-data dir).
- The login classifier gives the right outcome for each page.
- The playbook resumes from each phase.
- Two types run at once in separate contexts.
- The heartbeat thread survives a blocked loop.
- The spool replays in order as late steps.
- `stale_boot` exits.
- Atomic file `.bak` recovery.
- The updater rejects a bad signature, a version below `min_version`, and a `bad_versions` entry.
- The launcher rolls back on local health only.
- The `paths.py` drift test against `app.services.procore_client`.
- The `protocol.py` drift test.

### 10.3 SQL smoke on dev BDR (`scripts/agent_db_smoke.py`, dev only)
- the claim RPC under 5 concurrent callers (first-claim attempts = 1);
- immutability and redaction triggers;
- `procore_reserve_login` under concurrency;
- unique indexes;
- a profile delete with rows in every table.

### 10.4 End to end on dev

PC worker against dev BDR, real project 3167322 and RFI Const-198, read-only:

1. Replay `email_closed_const198.txt`. Check the happy path: brokered cookies work from the PC IP, the row is provisional, files land, and the next manual Sync now verifies it.
2. Pull the power mid-job at each phase. Expect exactly one RFI row, one set of attachments, one summary notice, and a complete ledger with late steps.
3. Keep the PC off for 2 h while 3 emails arrive. On boot they drain, and `already_current` skips the ones the sweep caught.
4. A Railway redeploy while a job holds help: the job survives with no attempt charged.
5. A network partition longer than 15 min: the job is reclaimed and old writes are rejected or marked late.
6. Force the broker to `browser_login_needed` (fixture): the fallback release is alerted, and its cookies are saved back to `rfp_harvest_sessions`.
7. A simulated MFA through the fixture: help chat end to end with a secret, plus an upload and a contribution.
8. Run the canary on all 5 providers, plus one escalated locate job (Pass C subject-only email) on 2 providers.
9. Kill switch; Stop running jobs; revoke the worker mid-job; hello abuse suspends.
10. A daily sweep plus an agent apply on one project, serialized by `claim_next`.
11. **Measure which POSTs the Procore RFI page issues in LLM browsing,** and record them in `docs/BROWSER_AGENTS.md`.
12. A us02 capture if a us02 PM project exists.

### 10.5 Frontend
`node --test` for the `lib/browserAgents.ts` helpers, `npx eslint .`, and Playwright screenshots of `/settings/browser-agents`, `/agents`, the job timeline and the help chat (smoketest account, cookie injection).

---

## 11. What the owner must do

**Env** (dev `.env` now; Railway prod only at release, with explicit approval):
1. `BROWSER_AGENTS_ENABLED=true` (dev).
2. Generate `BROWSER_AGENT_SECRETS_KEY` (plus `BROWSER_AGENT_SECRETS_KEY_ID=k1`) and `BROWSER_AGENT_FINGERPRINT_KEY`, each with:
   ```
   python -c "import secrets,base64;print(base64.b64encode(secrets.token_bytes(32)).decode())"
   ```
   Keep copies in the password manager.
3. `GOOGLE_API_KEY`, `XAI_API_KEY`, `META_API_KEY` for any provider you want selectable.
4. `BROWSER_AGENT_RELEASE_PUBKEY` (from the keygen step below).
5. Railway only: `TRUST_FORWARDED_FOR=true`.
6. Keep `PROCORE_LOGIN_EMAIL`. Keep `PROCORE_LOGIN_PASSWORD` as the fallback until the DB password has worked for 7 days, then remove it.

**Database:** confirm the 0156 constraint name (query in 2.7). Apply `0157_browser_agents.sql` to **dev BDR only**, then reload PostgREST.

**On the Mac, once:**
1. Run `bdr_be/scripts/agent_release_keygen.py`. Keep the private key in Keychain, and **write the printed key fingerprint into the password manager.**
2. Publish the first release with `publish_agent_worker_release.py`.

**In BDR Settings > Browser agents, as IT Admin:**
1. Set the Procore password.
2. Record the provider attestation for each provider you intend to use.
3. For `canary` (and optionally `rfi_from_email` escalation): pick provider and model, run **Test model**, confirm the prices and caps.
4. Pick the desired worker version.
5. Click **Add worker** and copy the command and fingerprint.
6. After the PC enrolls, **Approve** it, set its allowed types and allowed IPs (office static IP), and run **Run self-test**.
7. Enable `rfi_from_email`.

**On the Windows PC, once:**
1. Windows 11 Pro, fully updated, BitLocker on, automatic time sync on.
2. BIOS: "Restore on AC power loss" set to On. Power plan: never sleep or hibernate. Fast Startup off.
3. Allow outbound HTTPS to the BDR API host, `*.procore.com`, `*.procoretech.com`, `*.procorecdn.com`, `challenges.cloudflare.com`, `www.google.com`, `www.gstatic.com` and `*.amazonaws.com`. No LLM endpoints are needed.
4. Run the two PowerShell lines as Administrator. Compare the printed fingerprint with the password manager, then paste the enrollment code.
5. Optional: an antivirus exclusion for `C:\ProgramData\BDRAgent`, and Windows Update active hours set to business hours.

**Confirm:** whether the G3 Procore bot account has MFA or SSO (today's code assumes neither, `docs/RFP_HARVEST.md:40`).

---

## 12. Build order: three parallel workstreams

### 12.0 Phase 0: the contract (day 1, backend lead, frozen before parallel work)

**C1. Worker protocol.** `bdr_be/app/services/browser_agents/protocol.py`: pydantic v2 models for every request and response in section 3.2, the error-code enum, the tool schemas (section 4.1 as JSON Schema constants), the step-kind regex, and the checkpoint shape. It is copied verbatim to `agent_worker/bdr_agent/protocol.py`, and `tests/test_agent_protocol_drift.py` asserts byte equality. Fixtures go in `bdr_be/tests/fixtures_browser_agents/`:
- one JSON example per endpoint (request plus response);
- `claim_rfi.json`, `turn_action_navigate.json`, `session_ok.json`, `session_browser_login_needed.json`, `help_messages.json`.

The worker builds against a **mock BDR** (`agent_worker/tests/mock_bdr.py`, aiohttp) that serves exactly these fixtures.

**C2. Human API.** `bdr_fe/lib/browserAgents.ts` types are written first, from this list (the backend returns exactly these):

```
Summary          {enabled, open_help, waiting_jobs, worker_online, spend_today_usd, daily_cap_usd}
JobRow           {id, agent_type, status, outcome, project_id, project_name, rfi_label, attempts,
                  interrupted_count, created_at, finished_at, waiting_help_id, cost_usd}
JobDetail        JobRow & {input_summary, config_snapshot:{provider, model, prompt_sha}, lease_epoch,
                  tokens_in, tokens_out, step_count, turn_count, sync_run_id, error_kind, error,
                  source_email_id}
Step             {seq, epoch, late, kind, actor, actor_label, summary, tool_name, args, result,
                  url_before, url_after, screenshot_artifact_id, turn_no, ok, duration_ms, at,
                  redacted_at}
Turn             {turn_no, epoch, status, assistant_text, action, provider, model, input_tokens,
                  output_tokens, cost_usd, billing_unknown, created_at}
HelpRow          {id, scope_key, reason_code, title, status, project_id, project_name,
                  claimed_by_label, waiting_jobs, created_at, expires_at}
HelpDetail       HelpRow & {agent_message, screenshot_artifact_id,
                  waiters:[{job_id, rfi_label, status}], messages:HelpMessage[]}
HelpMessage      {id, author_kind, author_label, action, body|null, is_secret, secret_consumed,
                  files:[{artifact_id, filename, size_bytes, mime_type, attachable}], created_at}
AdminState       {settings, runtime:{spend_today_usd}, types:TypeCfg[], workers:Worker[],
                  secrets:[{name, set, set_at, set_by_label, fingerprint}],
                  procore:{email, locked_until, login_failures, last_login_source, last_error},
                  releases:Release[], attestations:Attestation[],
                  ceilings:{[type]: string[]}}
```

**Routes** (section 7, exact paths):

| Area | Routes |
|---|---|
| Summary | `GET /browser-agents/summary` |
| Jobs | `GET /browser-agents/jobs?status=&type=&project_id=&cursor=` -> `{items: JobRow[], next_cursor}`<br>`GET /jobs/{id}`<br>`GET /jobs/{id}/steps?after_seq=&limit=200`<br>`GET /jobs/{id}/turns`<br>`POST /jobs/{id}/cancel`<br>`POST /jobs/{id}/retry` |
| Artifacts | `GET /browser-agents/artifacts/{id}/url` -> `{url, filename, mime_type}` |
| Help | `GET /browser-agents/help?status=`<br>`GET /help/{id}`<br>`GET /help/{id}/messages?after_id=`<br>`POST /help/{id}/messages {client_msg_id, action, body?, is_secret?, secret_id?, secret_value?, artifact_ids?}`<br>`POST /help/{id}/files` (multipart `file`, `attachable`) -> `{artifact_id}`<br>`POST /help/{id}/claim`<br>`POST /help/{id}/unclaim` |
| Admin | `GET /browser-agents/admin/state`<br>`PATCH /admin/settings`<br>`PATCH /admin/types/{key}`<br>`GET /admin/models?provider=`<br>`POST /admin/types/{key}/probe {provider, model}`<br>`PUT /admin/attestations/{provider}`<br>`PUT /admin/secrets/{name} {value}`<br>`DELETE /admin/secrets/{name}`<br>`POST /admin/enrollments {worker_name}` -> `{code, expires_at, install_command, key_fingerprint}`<br>`PATCH /admin/workers/{id}`<br>`POST /admin/workers/{id}/approve`<br>`POST /admin/workers/{id}/revoke`<br>`GET /admin/history?cursor=`<br>`POST /admin/canary`<br>`POST /admin/stop-running` |

**Error contract:** `X-Error-Code` values `looks_like_secret`, `pm_rfi_sync_off`, `probe_failed`, `attestation_missing`, `below_min_version`.

**C3. Notification types and metadata** (section 7 table), fixed on day 1.

### 12.1 Workstream B: backend (`bdr_be`)

| Phase | Deliverables |
|---|---|
| B1 | Migration 0157 (all of section 2), `test_migration_0157.py`, `scripts/agent_db_smoke.py`, config block and validator, `core/roles.py` sets, `core/features.py` flag |
| B2 | `core/agent_auth.py`, `routers/agent_worker.py` (enroll, hello, config, heartbeat, claim, steps, checkpoint, artifacts, finish, requeue, park, releases, canary page), `services/browser_agents/queue.py` (enqueue, sweeper), `registry.py`, `types/canary.py` |
| B3 | `services/browser_agents/llm/*`, `turns.py`, `redact` extensions and global httpx filter, `llm_gate` provider keys |
| B4 | `procore_creds.py` and all call-site replacements, `SessionStore.reserve_login` plus the RPCs, `procore_broker.py`, the login fallback endpoints, `ProcoreUnavailable.step`, `ProcoreSession.probe` |
| B5 | `pm_apps/procore.py` (seed, validate_capture), `rfi_session.py`, `pm_rfi_sync` changes (section 5.4, Pass C, checkpoint hold, `_queue_email_rfi`), `contributions.py`, `types/rfi_from_email.py` (`prepare`) |
| B6 | `help.py` (open, notify, reminders, lapse, expire, secrets, contributions endpoints), `notifications` and `notification_email` wiring |
| B7 | `routers/browser_agents.py` (human API C2), `_enrich_rfis` fields, `audit_log` actions, `scripts/publish_agent_worker_release.py`, `agent_release_keygen.py`, `rotate_agent_secrets_key.py`, `docs/BROWSER_AGENTS.md` |

### 12.2 Workstream W: worker (`bdr_be/agent_worker`)

| Phase | Deliverables |
|---|---|
| W1 | Package skeleton, `protocol.py` copy, `client.py`, `tokenstore.py`, `local_config.py`, `heartbeat.py`, `supervisor.py`, `slot.py`, `spool.py` against `mock_bdr.py` |
| W2 | `browser/` (browser, guard, executor, snapshot), the Procore fixture server, guard and executor tests |
| W3 | `agent/loop.py`, `agent/playbook.py`, canary type end to end against the mock |
| W4 | `pm_apps/procore/*` (rest, login classifier, ceiling, vendored paths), `rfi_from_email` playbook and tools, help consumption |
| W5 | `launcher`, updater, `install.ps1`, WinSW XML, signature and `min_version` checks |

### 12.3 Workstream F: frontend (`bdr_fe`)

| Phase | Deliverables |
|---|---|
| F1 | `lib/browserAgents.ts` (C2 types, wrappers, role helpers, tests), feature flag, `IconBot`, i18n keys |
| F2 | Settings card and sub-page sections, built against a local fixture JSON per C2 type until B7 lands |
| F3 | `/agents` list, job page with timeline, `AgentActivityProvider`, Sidebar nav and badge |
| F4 | Help chat page (messages, uploads, secret, claim, actions) |
| F5 | Bell routing, RFI badge and detail-modal provenance, help-center topic, Playwright screenshots |

### 12.4 Integration milestones
- **M1:** B2 plus W1 to W3 give the canary end to end on dev.
- **M2:** B3 plus the canary LLM turn on each provider.
- **M3:** B4 to B6 plus W4 give the RFI happy path, help and MFA through the fixture.
- **M4:** F1 to F5 against the real API.
- **M5:** the section 10.4 E2E list, then the prod release (with approval).

---

## 13. Decisions the owner should confirm

1. **LLM only on escalation.** RFI jobs run a deterministic playbook, and the cloud LLM is called only when the playbook is stuck (locate ambiguity, unexpected pages), and only on manually linked projects. Most jobs cost $0. Confirm this meets "drives Chromium with a cloud LLM". If not, the LLM becomes the primary navigator for every job (higher cost, more injection surface).
2. **Where the Procore password is used.** BDR logs in on the server and hands the PC session cookies. The password goes to the PC only when Procore shows a challenge that needs a real browser: at most 3 times a day, each one alerted to IT Admin. Confirm, or choose "never send the password to the PC" (challenges then always become help requests).
3. **MFA or SSO** on the G3 Procore bot account: yes or no.
4. **Provider attestation:** that each provider key you add is a paid tier with no training on your data. Meta stays "experimental" until its tool-calling endpoint is proven.
5. **Spend:** a $25 daily cap and $2 per job.
6. **Subject-only trigger (Pass C):** Procore RFI emails with no link start a "locate" job. Confirm it is on.
7. **Who answers help:** Estimating Admins and IT Admins answer. Executives can view only.
8. **Retention:** keep all agent logs, screenshots and LLM transcripts indefinitely in v1 (no purge).
9. **Office static IP** to lock the worker token to, if one exists.
10. **Compromise response:** if the PC or its token is compromised, the response is Revoke plus one installer run at the PC. This is the only time anyone touches the PC.

---

## Appendix A: critique dispositions

**Accepted:**

| Critique | Disposition |
|---|---|
| C0-1 | storage 403 to Transient, `MemorySessionStore`, test; plus provisional writes |
| C0-2 | heartbeat renews by epoch; reclaim needs a silent worker and sweeper grace; 15 min worker tolerance |
| C0-4 | `applying` status plus sweeper reconcile; contributions inside `process_run` |
| C0-5 | thinking takeover, abandoned old-epoch turns, `billing_unknown` estimate |
| C0-6 | atomic `reserve_login` and failure RPCs, unreported attempts counted, `help_open` gating, cookie exchange as the main path; credentials read fresh at login |
| C0-7 | stale guard plus `skipped_stale` |
| C0-8 | checkpoint held at the earliest failed email |
| C0-9 | cap in `/turns` only |
| C0-10 | `shutdown`/`drain`/`provider_outage` reasons, charged attempts vs interruptions, orphans in heartbeat, `prepare` in try |
| C0-11 | `boot_id` on every RPC, `stale_boot` exit, epoch bump on hello |
| C0-12 | claim lapse, IT Admin fallback, login scopes stay open and email IT Admin, auto-resolve only on an `ok` login |
| C0-13 | error classification, outage requeue uncharged, config error disables escalation |
| C0-14 | late steps |
| C0-16 | heartbeat thread, `to_thread`, loop-stall watchdog |
| C0-17 | atomic files with `.bak`, local-health rollback, `bad_versions` (profile quarantine made moot by D11) |
| C0-18 | `synced_at >= message_at` short-circuit, offline email at 4 h, `job.expired` step |
| C0-19 | no link status writes from agent runs; validator requires `pm_rfi_sync_active` |
| C0-20 | restrict FKs instead of set null, `on conflict` join in `open_help`, link and job scopes |
| C1-1 | broker cookies primary, gated fallback release, Chromium password prefs, profile grep test, IP allowlist |
| C1-2 | provisional writes, attachment `projectId`/`companyId` check |
| C1-3 | scrub before persist, redactable payloads, stripped detail JSON after terminal |
| C1-4 | `fill_secret` removed from model tools; hidden values and masks; consumed-secret HMAC tripwire; traces cut |
| C1-5 | code ceilings, narrowing only, token-query refusal, login POST gated to the login page object, host assert |
| C1-6 | templates, collapsed warning, plaintext secret block, secrets only in login scopes |
| C1-7 | full fence spec, IDOR checks, admin `allowed_types` |
| C1-8 | rotation cut, hello rate limit and suspend, `client_ip` from `X-Forwarded-For` behind a flag, 20-character codes, pending approval, DB enroll counters, IP alerts |
| C1-9 | out-of-band fingerprint check, signed `min_version`, Chromium-age warning |
| C1-10 | INVOKER, `search_path`, revoke from public |
| C1-12 | magic-byte sniff, zip/dwg thread-only, scan hook, sync capped reader |
| C1-13 | client `secret_id` AAD, encrypt before insert, error details stripped |
| C1-14 | global httpx filter, Google header key, `no-store` |
| C1-15 | provider attestation |
| C1-16 | no LLM escalation on auto-linked projects |
| C1-17 | hashes in steps, one helper Retry, 50% spend alert |
| C2-1 | same fix as C0-1 |
| C2-2 | plain-uuid actors plus labels, enrollments set null, profile-delete test |
| C2-3 | `coalesce` charge rule, `resume_reason` cleared on claim |
| C2-4 | coalescing cut |
| C2-5 | `applying`, reconcile, `apply_attempt` index, no link failure |
| C2-6 | all call sites plus grep test |
| C2-7 | `_deep_link` and `destination()` ordering, deep-link test |
| C2-8 | column-scoped triggers, runtime-state row |
| C2-9 | same as C0-5 |
| C2-10 | web-session backfill run queued, summary suppressed |
| C2-11 | Pass C, revision tie-break, owner confirmation, classifier hook |
| C2-12 | `PmAppAdapter` registry, identity-keyed secrets and locks, `platform` field |
| C2-13 | no capture rejection over hosts; us02 E2E |
| C2-14 | `link:` scope plus 24 h suppression |
| C2-15 | same as C0-9 |
| C2-16 | placeholders cut, contributions table, `import_method` on insert only |
| C2-17 | login challenge hosts, REST tools primary, POST measurement |
| C2-18 | shim store, plain-`def` auth, settings RPC, boot check, dependency validator, Meta experimental |
| C2-19 | hash chain, anchors, rotation, coalesce, propose fields, traces, purge and the PmLinkCard row all cut |

**Rejected or modified, one line each:**
- **C0-3 (two-phase token rotation):** moot, because rotation is cut from v1 (C2-19); compromise response is revoke plus re-enroll.
- **C0-6, replace the 60 s cache with a version check:** modified. Login-time password reads are always fresh, and only `configured()` keeps the 60 s cache, which `harvester_for` needs per row (C2-6).
- **C0-15 / C1-11 (placeholder status fixes):** moot, because helper-authored placeholder rows are removed.
- **C1-2, BDR re-fetches `body.id` before applying:** rejected. Provisional writes plus the next sweep's re-read already verify every agent row without doubling Procore calls.
- **C1-8, rotation as hygiene:** rejected. Rotation over the heartbeat hands the new token to whoever holds the old one, and v1 has no need for it.
- **C1-16, DMARC pass before auto-link:** deferred. `ingested_emails` stores no Authentication-Results header, and the no-escalation rule removes the amplification.
- **C1-17, per-worker daily spend cap:** rejected for v1. With one worker it equals the global cap; the 50% alert covers abuse detection.
- **C2-1, requeue a stale capture for recapture:** rejected. Provisional writes plus storage-403-as-retry let the daily sweep fetch fresh files without a second agent run.
- **C2-19, cut the launcher's automatic rollback:** rejected. Without it a crashing release needs a PC visit, which the owner ruled out. It is kept as local-health-only (C0-17).
- **D11, per-slot browser profiles (from the original design debate):** rejected in favor of ephemeral per-job contexts with brokered cookies. Profiles add corruption and lock-file failure modes and store cookies at rest.