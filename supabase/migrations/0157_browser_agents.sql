-- 0157: Browser agent platform (docs/browser_agents/SPEC.md section 2, adjusted by SPEC_OVERRIDES.md). Apply after 0156. DEV ONLY until the release.
--
-- A Windows PC runs a worker that drives headless Chromium for BDR. BDR owns
-- the queue, the model transcript, the audit ledger and every write to the
-- RFI log; the worker only runs tools. Nothing is ever written to Procore.
--
-- Owner overrides applied here (they win over the spec text):
--   O1  the model drives every job: no llm_escalation_enabled column and no
--       "manually linked only" gate. A type cannot be enabled without a
--       provider, a model and prices.
--   O2  the worker logs in itself (identities session / login/begin /
--       login/result). No broker login, no browser-login fallback gate or
--       daily release cap. procore_reserve_login keeps the shared 600 s
--       spacing and the 3-strike lockout atomic for every caller.
--   O4  spend caps: $25/day global and $2/job, both on browser_agent_settings
--       (per-job cost moved off browser_agent_types). IT Admin can reset the
--       daily window; browser_agent_reset_spend_window audits it.
--   O5  no provider attestation table; worker IP allowlist optional (empty).
--
--   1. Settings, runtime state, types, settings change log
--   2. Encrypted secrets (Procore password; AES-GCM done in Python)
--   3. Workers, enrollment, releases
--   4. Jobs (leases, epochs, boot fencing, dedupe, checkpoint) and turns
--   5. Artifacts, the append-only step ledger, login attempts
--   6. Help requests, waiters, messages, reads, RFI contributions
--   7. Changes to existing tables (pm_rfi_sync_runs 'agent' trigger, rfis
--      provenance, rfp_harvest_sessions, pm_rfi_email_scan_state), bucket
--   8. RLS (deny by default; the service-role backend is the only client)
--   9. Functions: triggers, login RPCs, job RPCs, settings and spend RPCs
--  10. Grants
--
-- Conventions: every function is SECURITY INVOKER with a pinned search_path
-- and is executable only by service_role. RPCs raise with the message set to
-- the protocol ErrorCode value (stale_boot, lease_lost, job_not_running,
-- revoked, unauthorized, capture_invalid, invalid_request) so the API maps it
-- straight to X-Error-Code. Immutable tables keep actor ids as plain uuids
-- plus a label, so deleting a profile never fails on them. Workers and jobs
-- are never deleted, so FKs to them are on delete restrict.

-- 1. Settings, runtime state, types, change log ------------------------------------

create table if not exists browser_agent_settings (
  id                            smallint primary key default 1 check (id = 1),
  enabled                       boolean not null default false,             -- kill switch
  config_version                bigint not null default 1,                  -- bumped by triggers only
  daily_cost_cap_usd            numeric(10,2) not null default 25 check (daily_cost_cap_usd between 0 and 1000),
  per_job_cost_cap_usd          numeric(8,2) not null default 2 check (per_job_cost_cap_usd between 0 and 50),
  help_hold_seconds             int not null default 900 check (help_hold_seconds between 60 and 3600),
  help_reminder_minutes         int not null default 120 check (help_reminder_minutes between 10 and 1440),
  help_claim_lapse_minutes      int not null default 30 check (help_claim_lapse_minutes between 5 and 240),
  help_expire_hours             int not null default 48 check (help_expire_hours between 1 and 336),
  worker_offline_alert_minutes  int not null default 60 check (worker_offline_alert_minutes between 5 and 1440),
  worker_offline_email_hours    int not null default 4 check (worker_offline_email_hours between 1 and 72),
  job_ttl_hours                 int not null default 168 check (job_ttl_hours between 1 and 720),
  desired_worker_version        text check (desired_worker_version ~ '^\d+\.\d+\.\d+$'),
  updated_by                    uuid references profiles(id) on delete set null,
  updated_at                    timestamptz not null default now()
);
insert into browser_agent_settings (id) values (1) on conflict do nothing;

-- Sweeper bookkeeping; writes here never bump config_version. The daily spend
-- window starts at the later of 00:00 America/Los_Angeles and
-- cost_window_started_at (set by an audited IT Admin reset).
create table if not exists browser_agent_runtime_state (
  id                       smallint primary key default 1 check (id = 1),
  cost_window_started_at   timestamptz,
  cost_window_reset_by     uuid,                                            -- plain uuid (audit row has the label)
  half_cap_notified_at     timestamptz,
  cap_hit_notified_at      timestamptz,
  offline_notified_at      timestamptz,
  offline_emailed_at       timestamptz,
  provider_alerts          jsonb not null default '{}'::jsonb,             -- {provider: last_alert_at}
  updated_at               timestamptz not null default now()
);
insert into browser_agent_runtime_state (id) values (1) on conflict do nothing;

create table if not exists browser_agent_types (
  key                  text primary key check (key ~ '^[a-z][a-z0-9_]{2,40}$'),
  label                text not null,
  enabled              boolean not null default false,
  identity             text check (identity ~ '^[a-z][a-z0-9_]{2,40}$'),   -- 'procore' | null
  provider             text check (provider in ('openai', 'anthropic', 'meta', 'google', 'xai')),
  model                text check (model ~ '^[A-Za-z0-9._:/-]{1,120}$'),
  price_in_per_mtok    numeric(10,4) check (price_in_per_mtok >= 0),
  price_out_per_mtok   numeric(10,4) check (price_out_per_mtok >= 0),
  model_probe_ok_at    timestamptz,
  max_concurrency      smallint not null default 2 check (max_concurrency between 0 and 8),
  max_attempts         smallint not null default 3 check (max_attempts between 1 and 10),        -- charged
  max_interruptions    smallint not null default 20 check (max_interruptions between 1 and 100),
  max_steps            int not null default 40 check (max_steps between 0 and 200),
  max_total_tokens     int not null default 400000 check (max_total_tokens between 0 and 5000000),
  max_active_seconds   int not null default 1200 check (max_active_seconds between 60 and 7200),
  host_narrowing       text[],                                              -- null = full code ceiling
  prompt_version       text not null,
  priority_default     smallint not null default 100,
  updated_by           uuid references profiles(id) on delete set null,
  updated_at           timestamptz not null default now(),
  -- O1: every job runs the model, so an enabled type needs a priced model.
  constraint browser_agent_types_enabled_needs_model check (
    not enabled or (provider is not null and model is not null
                    and price_in_per_mtok is not null and price_out_per_mtok is not null))
);
insert into browser_agent_types (key, label, identity, prompt_version) values
  ('rfi_from_email', 'RFI from email', 'procore', 'rfi_from_email.v1'),
  ('canary', 'Worker self-test', null, 'canary.v1')
on conflict do nothing;

-- Append-only (trigger below). Secrets appear as {"set":true,"fingerprint":"ab12cd34"}.
create table if not exists browser_agent_settings_changes (
  id                bigint generated always as identity primary key,
  scope             text not null check (scope in ('global', 'agent_type', 'secret', 'worker', 'release', 'help', 'spend')),
  target            text,
  field             text not null,
  old_value         jsonb,
  new_value         jsonb,
  probe             jsonb,                                                  -- Test model result or "Save anyway"
  changed_by        uuid,
  changed_by_label  text not null,
  changed_at        timestamptz not null default now()
);
create index if not exists browser_agent_settings_changes_at_idx on browser_agent_settings_changes (changed_at desc);

-- 2. Encrypted secrets ------------------------------------------------------------

create table if not exists browser_agent_secrets (
  name         text primary key check (name ~ '^[a-z][a-z0-9_]{2,40}_password$'),   -- '<identity>_password'
  ciphertext   bytea not null,                                              -- AES-256-GCM ct||tag
  nonce        bytea not null check (length(nonce) = 12),
  key_id       text not null,
  version      int not null default 1,                                      -- AAD = 'bdr-agent-secret|'||name||'|'||version
  fingerprint  text not null,                                               -- HMAC-SHA256(fingerprint key, value) hex[:8]
  set_by       uuid references profiles(id) on delete set null,
  set_at       timestamptz not null default now()
);

-- 3. Workers, enrollment, releases ----------------------------------------------

create table if not exists browser_agent_workers (
  id                     uuid primary key default gen_random_uuid(),
  name                   text not null unique check (length(name) between 2 and 60),
  status                 text not null default 'pending' check (status in ('pending', 'active', 'revoked')),
  token_hash             bytea unique,                                      -- sha256 of 'bdrw_'||b64url(32 bytes)
  token_prefix           text,
  draining               boolean not null default false,
  max_slots              smallint not null default 3 check (max_slots between 1 and 8),
  allowed_types          text[] not null default '{}',                      -- admin-set; intersected at claim
  allowed_ips            cidr[] not null default '{}',                      -- O5: optional; empty = any
  boot_id                uuid,
  boot_at                timestamptz,
  hello_count_hour       int not null default 0,
  hello_window_at        timestamptz,
  version                text,
  bad_versions           text[] not null default '{}',
  hostname               text,
  os_info                jsonb,
  chromium_version       text,
  capabilities           jsonb,                                             -- {"types":{"rfi_from_email":"1.0.0"},"slots":3}
  control_capabilities   jsonb not null default '{}'::jsonb,                -- live-view seam: {"screencast":false}
  last_seen_at           timestamptz,
  last_ip                inet,
  enroll_ip              inet,
  created_by             uuid references profiles(id) on delete set null,
  approved_by            uuid references profiles(id) on delete set null,
  approved_at            timestamptz,
  revoked_by             uuid references profiles(id) on delete set null,
  revoked_at             timestamptz,
  created_at             timestamptz not null default now()
);
create index if not exists browser_agent_workers_seen_idx on browser_agent_workers (last_seen_at);

create table if not exists browser_agent_enrollments (
  id           uuid primary key default gen_random_uuid(),
  code_hash    bytea not null unique,                                       -- sha256 of the 20-char base32 code
  worker_name  text not null,
  created_by   uuid references profiles(id) on delete set null,
  created_at   timestamptz not null default now(),
  expires_at   timestamptz not null,                                        -- +30 min
  used_at      timestamptz,
  worker_id    uuid references browser_agent_workers(id) on delete restrict
);

create table if not exists browser_agent_enroll_attempts (
  ip            inet not null,
  window_start  timestamptz not null,
  count         int not null default 0,
  primary key (ip, window_start)
);

-- Written only by scripts/publish_agent_worker_release.py (service role).
create table if not exists browser_agent_worker_releases (
  version           text primary key check (version ~ '^\d+\.\d+\.\d+$'),
  storage_path      text not null,
  sha256            text not null check (sha256 ~ '^[0-9a-f]{64}$'),
  min_version       text not null check (min_version ~ '^\d+\.\d+\.\d+$'),
  manifest          jsonb not null,                                         -- {version, sha256, min_version, created_at}
  signature         text not null,                                          -- Ed25519 over the canonical manifest
  chromium_version  text,
  notes             text,
  created_at        timestamptz not null default now()
);

-- 4. Jobs and turns ------------------------------------------------------------------

create table if not exists browser_agent_jobs (
  id                    uuid primary key default gen_random_uuid(),
  agent_type            text not null references browser_agent_types(key),
  status                text not null default 'queued' check (status in
                          ('queued', 'running', 'waiting_help', 'applying', 'succeeded', 'failed', 'canceled', 'expired')),
  outcome               text check (outcome in ('pending', 'applied', 'apply_failed', 'nothing_to_do',
                                                'already_current', 'skipped_stale', 'no_link', 'self_test_ok')),
  priority              smallint not null default 100,
  dedupe_key            text not null,                                      -- 'email:<id>:rfi:<rfi_id>' | 'email:<id>:locate'
  platform              text,                                               -- 'procore'
  project_id            uuid references projects(id) on delete set null,
  source_email_id       uuid references ingested_emails(id) on delete set null,
  input                 jsonb not null,
  config_snapshot       jsonb,
  lease_owner           uuid references browser_agent_workers(id) on delete restrict,
  lease_boot_id         uuid,
  lease_epoch           bigint not null default 0,
  lease_expires_at      timestamptz,
  attempts              smallint not null default 0,                        -- charged (first claim, crash, error retry)
  interrupted_count     smallint not null default 0,                        -- boot change, BDR outage, shutdown, drain
  next_attempt_at       timestamptz,
  resume_reason         text check (resume_reason in ('retry', 'interrupted', 'help_answered', 'manual',
                                                      'login_spacing', 'shutdown', 'drain', 'provider_outage')),
  last_resume_reason    text,
  checkpoint            jsonb not null default '{}'::jsonb,
  checkpoint_seq        int not null default 0,
  step_count            int not null default 0,
  turn_count            int not null default 0,
  tokens_in             bigint not null default 0,
  tokens_out            bigint not null default 0,
  cost_usd              numeric(10,4) not null default 0,
  active_seconds        int not null default 0,
  helper_retry_used     boolean not null default false,
  apply_attempt         smallint not null default 0,
  sync_run_id           uuid,                                               -- fk added in section 7
  waiting_help_id       uuid,                                               -- fk added in section 6
  hold_until            timestamptz,
  result                jsonb,
  error_kind            text,
  error                 text check (length(error) <= 1000),
  control_mode          text not null default 'agent' check (control_mode in ('agent')),   -- live-view seam
  cancel_requested_at   timestamptz,
  cancel_requested_by   uuid references profiles(id) on delete set null,
  expires_at            timestamptz not null,
  created_by            uuid references profiles(id) on delete set null,
  created_at            timestamptz not null default now(),
  started_at            timestamptz,
  finished_at           timestamptz,
  updated_at            timestamptz not null default now(),
  constraint browser_agent_jobs_lease_shape check ((lease_owner is null) = (lease_expires_at is null))
);
create unique index if not exists browser_agent_jobs_dedupe_uidx on browser_agent_jobs (agent_type, dedupe_key);
create index if not exists browser_agent_jobs_claim_idx on browser_agent_jobs (agent_type, priority, created_at) where status = 'queued';
create index if not exists browser_agent_jobs_lease_idx on browser_agent_jobs (lease_expires_at) where lease_owner is not null;
create index if not exists browser_agent_jobs_applying_idx on browser_agent_jobs (updated_at) where status = 'applying';
create index if not exists browser_agent_jobs_project_idx on browser_agent_jobs (project_id, created_at desc);
create index if not exists browser_agent_jobs_status_idx on browser_agent_jobs (status, created_at desc);
create index if not exists browser_agent_jobs_email_idx on browser_agent_jobs (source_email_id);
create index if not exists browser_agent_jobs_owner_idx on browser_agent_jobs (lease_owner);

drop trigger if exists browser_agent_jobs_updated_at on browser_agent_jobs;
create trigger browser_agent_jobs_updated_at before update on browser_agent_jobs
  for each row execute function set_updated_at();

create table if not exists browser_agent_turns (
  job_id                uuid not null references browser_agent_jobs(id) on delete restrict,
  turn_no               int not null,
  epoch                 bigint not null,
  status                text not null check (status in ('thinking', 'issued', 'failed', 'abandoned')),
  thinking_owner        text,
  thinking_until        timestamptz,
  billing_unknown       boolean not null default false,
  tool_results          jsonb not null,                                     -- redacted observation
  screenshot_artifact_id uuid,
  assistant_text        text,
  action                jsonb,                                              -- {call_id, tool, args}
  provider              text,
  model                 text,
  provider_request_id   text,
  request_artifact_id   uuid,
  response_artifact_id  uuid,
  request_sha256        text,
  response_sha256       text,
  input_tokens          int,
  output_tokens         int,
  cached_tokens         int,
  cost_usd              numeric(10,6),
  duration_ms           int,
  error_kind            text,
  error                 text,
  created_at            timestamptz not null default now(),
  primary key (job_id, turn_no)
);
create index if not exists browser_agent_turns_day_idx on browser_agent_turns (created_at);

-- 5. Artifacts, ledger, login attempts --------------------------------------------

create table if not exists browser_agent_artifacts (
  id                  uuid primary key default gen_random_uuid(),
  job_id              uuid references browser_agent_jobs(id) on delete restrict,
  help_id             uuid,                                                 -- fk added in section 6
  kind                text not null check (kind in ('screenshot', 'rfi_detail_json', 'rfi_list_json', 'page_snapshot',
                                                    'llm_request', 'llm_response', 'helper_upload')),
  storage_path        text not null unique,                                 -- bucket browser-agent
  filename            text,
  mime_type           text not null,
  sniffed_mime        text,
  size_bytes          bigint not null,
  sha256              text not null check (sha256 ~ '^[0-9a-f]{64}$'),      -- of the original bytes
  attachable          boolean not null default false,                       -- helper ticked + type allows
  stripped_at         timestamptz,                                          -- signed query strings removed after terminal
  uploaded_by_worker  uuid references browser_agent_workers(id) on delete restrict,
  uploaded_by_user    uuid references profiles(id) on delete set null,
  created_at          timestamptz not null default now(),
  constraint browser_agent_artifacts_owner check (job_id is not null or help_id is not null)
);
create unique index if not exists browser_agent_artifacts_job_sha_uidx
  on browser_agent_artifacts (job_id, kind, sha256) where job_id is not null;
create index if not exists browser_agent_artifacts_help_idx on browser_agent_artifacts (help_id);

-- Append-only ledger. seq is assigned per job by a BEFORE INSERT trigger.
create table if not exists browser_agent_steps (
  id                      bigint generated always as identity primary key,
  job_id                  uuid not null references browser_agent_jobs(id) on delete restrict,
  seq                     int not null,
  epoch                   bigint not null,
  late                    boolean not null default false,                   -- old-epoch rows, no state effect
  worker_id               uuid,
  idem_key                text,
  kind                    text not null check (kind ~ '^[a-z_]+(\.[a-z_]+)?$'),
  actor                   text not null check (actor in ('worker', 'playbook', 'model', 'bdr', 'helper', 'admin', 'system')),
  actor_user_id           uuid,
  actor_label             text,
  summary                 text not null check (length(summary) <= 500),
  tool_name               text,
  args                    jsonb,                                            -- redacted; 64 KB cap by the writer
  result                  jsonb,
  payload_sha256          text,                                             -- of canonical args||result before storage
  llm_request_sha256      text,
  llm_response_sha256     text,
  artifact_sha256         text,
  url_before              text,                                             -- query strings stripped
  url_after               text,
  screenshot_artifact_id  uuid references browser_agent_artifacts(id) on delete restrict,
  turn_no                 int,
  ok                      boolean,
  duration_ms             int,
  at                      timestamptz not null default now(),
  redacted_at             timestamptz,
  redacted_by_label       text
);
create unique index if not exists browser_agent_steps_seq_uidx on browser_agent_steps (job_id, seq);
create unique index if not exists browser_agent_steps_idem_uidx on browser_agent_steps (job_id, idem_key) where idem_key is not null;
create index if not exists browser_agent_steps_kind_idx on browser_agent_steps (kind, at desc);
create index if not exists browser_agent_steps_claim_idx on browser_agent_steps (job_id, epoch, worker_id) where kind = 'claim';

-- O2: kind 'session' = a stored-jar handout, 'browser' = a login the worker ran.
create table if not exists browser_agent_login_attempts (
  id                  uuid primary key default gen_random_uuid(),
  identity            text not null,
  host                text not null,
  kind                text not null check (kind in ('session', 'browser')),
  job_id              uuid not null references browser_agent_jobs(id) on delete restrict,
  worker_id           uuid references browser_agent_workers(id) on delete restrict,
  epoch               bigint not null,
  granted             boolean not null,
  refused_reason      text check (refused_reason in ('spacing', 'locked', 'not_configured', 'help_open', 'ip_not_allowed', 'no_jar')),
  secret_version      int,
  outcome             text check (outcome in ('ok', 'stored_jar', 'password_rejected', 'mfa', 'captcha', 'challenge',
                                              'sso', 'unexpected', 'timeout', 'unreported')),
  step                text,
  counted_as_failure  boolean not null default false,
  created_at          timestamptz not null default now(),
  reported_at         timestamptz
);
create index if not exists browser_agent_login_attempts_open_idx on browser_agent_login_attempts (created_at)
  where kind = 'browser' and granted and reported_at is null;
create index if not exists browser_agent_login_attempts_job_idx on browser_agent_login_attempts (job_id, created_at desc);

-- 6. Help, chat, contributions --------------------------------------------------------

create table if not exists browser_agent_help_requests (
  id                       uuid primary key default gen_random_uuid(),
  scope_key                text not null check (scope_key ~ '^(login|link|job):[a-z0-9_-]{2,64}$'),
  agent_type               text references browser_agent_types(key),
  reason_code              text not null check (reason_code in ('login_failed', 'login_locked', 'captcha', 'mfa', 'sso',
                             'logged_out', 'access_denied', 'not_found', 'ambiguous', 'unexpected_page', 'cap_reached',
                             'needs_info', 'provider_config', 'other')),
  title                    text not null,                                   -- server template (trusted)
  agent_message            text check (length(agent_message) <= 1000),      -- untrusted
  screenshot_artifact_id   uuid references browser_agent_artifacts(id) on delete restrict,
  project_id               uuid references projects(id) on delete set null,
  opened_by_job_id         uuid references browser_agent_jobs(id) on delete restrict,
  holder_job_id            uuid references browser_agent_jobs(id) on delete restrict,   -- job holding the live page
  status                   text not null default 'open' check (status in ('open', 'answered', 'resolved', 'expired', 'canceled')),
  claimed_by               uuid references profiles(id) on delete set null,
  claimed_at               timestamptz,
  last_helper_activity_at  timestamptz,
  resolved_by              uuid references profiles(id) on delete set null,
  resolved_at              timestamptz,
  resolution               text check (resolution in ('agent_continued', 'retry', 'give_up', 'skip_daily', 'auto_resolved', 'expired')),
  notified_at              timestamptz,
  reminder_count           smallint not null default 0,
  reminder_sent_at         timestamptz,
  expiry_emailed_at        timestamptz,
  expires_at               timestamptz not null,
  created_at               timestamptz not null default now()
);
create unique index if not exists browser_agent_help_open_uidx on browser_agent_help_requests (scope_key)
  where status in ('open', 'answered');
create index if not exists browser_agent_help_status_idx on browser_agent_help_requests (status, created_at desc);
create index if not exists browser_agent_help_scope_recent_idx on browser_agent_help_requests (scope_key, resolved_at desc);
create index if not exists browser_agent_help_project_idx on browser_agent_help_requests (project_id);

create table if not exists browser_agent_help_waiters (
  help_id   uuid not null references browser_agent_help_requests(id) on delete restrict,
  job_id    uuid not null references browser_agent_jobs(id) on delete restrict,
  added_at  timestamptz not null default now(),
  primary key (help_id, job_id)
);
create index if not exists browser_agent_help_waiters_job_idx on browser_agent_help_waiters (job_id);

create table if not exists browser_agent_help_messages (
  id                      bigint generated always as identity primary key,
  help_id                 uuid not null references browser_agent_help_requests(id) on delete restrict,
  author_kind             text not null check (author_kind in ('agent', 'helper', 'system')),
  author_id               uuid,
  author_label            text,
  job_id                  uuid references browser_agent_jobs(id) on delete restrict,
  client_msg_id           uuid,
  action                  text not null default 'message' check (action in ('message', 'retry', 'give_up', 'skip_daily')),
  body                    text check (length(body) <= 8000),
  is_secret               boolean not null default false,
  secret_id               uuid unique,                                      -- client-generated; AAD = 'help-secret|'||secret_id
  secret_ciphertext       bytea,
  secret_nonce            bytea,
  secret_hmac             text,
  secret_consumed_at      timestamptz,
  secret_consumed_by_job  uuid references browser_agent_jobs(id) on delete restrict,
  secret_purged_at        timestamptz,
  artifact_ids            uuid[] not null default '{}',
  created_at              timestamptz not null default now(),
  constraint browser_agent_help_messages_client_uniq unique (help_id, client_msg_id),
  constraint browser_agent_help_messages_secret_shape check (
    (is_secret and body is null and secret_id is not null) or (not is_secret and secret_ciphertext is null))
);
create index if not exists browser_agent_help_messages_help_idx on browser_agent_help_messages (help_id, id);

create table if not exists browser_agent_help_reads (
  job_id       uuid not null references browser_agent_jobs(id) on delete restrict,
  message_id   bigint not null references browser_agent_help_messages(id) on delete restrict,
  epoch        bigint not null,
  consumed_at  timestamptz not null default now(),
  primary key (job_id, message_id)
);

create table if not exists browser_agent_rfi_contributions (
  id                  uuid primary key default gen_random_uuid(),
  project_id          uuid not null references projects(id) on delete cascade,
  platform            text not null,
  external_rfi_id     text not null check (external_rfi_id ~ '^\d{1,20}$'),
  kind                text not null check (kind in ('file_question', 'file_answer', 'note')),
  artifact_id         uuid references browser_agent_artifacts(id) on delete restrict,
  note_text           text check (length(note_text) <= 8000),               -- verbatim helper text
  note_author_label   text,
  source_message_ids  bigint[] not null default '{}',
  help_id             uuid references browser_agent_help_requests(id) on delete restrict,
  job_id              uuid references browser_agent_jobs(id) on delete restrict,
  applied_rfi_id      uuid references rfis(id) on delete set null,
  applied_at          timestamptz,
  created_at          timestamptz not null default now(),
  constraint browser_agent_rfi_contrib_note_shape check ((kind = 'note') = (note_text is not null)),
  constraint browser_agent_rfi_contrib_file_shape check ((kind <> 'note') = (artifact_id is not null))
);
create unique index if not exists browser_agent_rfi_contrib_uidx on browser_agent_rfi_contributions
  (project_id, platform, external_rfi_id, kind, coalesce(artifact_id::text, md5(note_text)));
create index if not exists browser_agent_rfi_contrib_pending_idx on browser_agent_rfi_contributions (project_id)
  where applied_at is null;

alter table browser_agent_jobs drop constraint if exists browser_agent_jobs_help_fk;
alter table browser_agent_jobs add constraint browser_agent_jobs_help_fk
  foreign key (waiting_help_id) references browser_agent_help_requests(id) on delete restrict;
alter table browser_agent_artifacts drop constraint if exists browser_agent_artifacts_help_fk;
alter table browser_agent_artifacts add constraint browser_agent_artifacts_help_fk
  foreign key (help_id) references browser_agent_help_requests(id) on delete restrict;

-- 7. Existing tables, storage ---------------------------------------------------------

-- The inline CHECK from 0156 carries the default name (checked on dev:
-- pm_rfi_sync_runs_trigger_check). Widened, never narrowed: 'email' stays for history.
alter table pm_rfi_sync_runs drop constraint if exists pm_rfi_sync_runs_trigger_check;
alter table pm_rfi_sync_runs add constraint pm_rfi_sync_runs_trigger_check
  check (trigger in ('daily', 'manual', 'email', 'agent'));
alter table pm_rfi_sync_runs add column if not exists agent_job_id uuid references browser_agent_jobs(id) on delete restrict;
alter table pm_rfi_sync_runs add column if not exists apply_attempt smallint;
alter table pm_rfi_sync_runs add column if not exists detail_artifact_id uuid references browser_agent_artifacts(id) on delete restrict;
create unique index if not exists pm_rfi_sync_runs_agent_uidx
  on pm_rfi_sync_runs (agent_job_id, apply_attempt) where agent_job_id is not null;

alter table browser_agent_jobs drop constraint if exists browser_agent_jobs_run_fk;
alter table browser_agent_jobs add constraint browser_agent_jobs_run_fk
  foreign key (sync_run_id) references pm_rfi_sync_runs(id) on delete set null;

alter table rfis add column if not exists import_method text;
alter table rfis drop constraint if exists rfis_import_method_chk;
alter table rfis add constraint rfis_import_method_chk check (import_method in ('web_session', 'agent'));
alter table rfis add column if not exists imported_by_agent_job_id uuid references browser_agent_jobs(id) on delete set null;
alter table rfis add column if not exists external_supplement jsonb;        -- [{"text","by","at","help_id","contribution_id"}]
create index if not exists rfis_imported_by_agent_job_idx on rfis (imported_by_agent_job_id);

-- 'backend' | 'agent:<worker id>' (O2: no broker source).
alter table rfp_harvest_sessions add column if not exists last_login_source text;

-- Pass C (subject-only locate jobs) keeps its own checkpoint.
alter table pm_rfi_email_scan_state add column if not exists subject_checkpoint timestamptz;

insert into storage.buckets (id, name, public, file_size_limit)
values ('browser-agent', 'browser-agent', false, 157286400)                -- 150 MB
on conflict (id) do nothing;

-- 8. RLS: deny-by-default backstop -----------------------------------------------------

alter table browser_agent_settings enable row level security;
alter table browser_agent_settings force row level security;
alter table browser_agent_runtime_state enable row level security;
alter table browser_agent_runtime_state force row level security;
alter table browser_agent_types enable row level security;
alter table browser_agent_types force row level security;
alter table browser_agent_settings_changes enable row level security;
alter table browser_agent_settings_changes force row level security;
alter table browser_agent_secrets enable row level security;
alter table browser_agent_secrets force row level security;
alter table browser_agent_workers enable row level security;
alter table browser_agent_workers force row level security;
alter table browser_agent_enrollments enable row level security;
alter table browser_agent_enrollments force row level security;
alter table browser_agent_enroll_attempts enable row level security;
alter table browser_agent_enroll_attempts force row level security;
alter table browser_agent_worker_releases enable row level security;
alter table browser_agent_worker_releases force row level security;
alter table browser_agent_jobs enable row level security;
alter table browser_agent_jobs force row level security;
alter table browser_agent_turns enable row level security;
alter table browser_agent_turns force row level security;
alter table browser_agent_artifacts enable row level security;
alter table browser_agent_artifacts force row level security;
alter table browser_agent_steps enable row level security;
alter table browser_agent_steps force row level security;
alter table browser_agent_login_attempts enable row level security;
alter table browser_agent_login_attempts force row level security;
alter table browser_agent_help_requests enable row level security;
alter table browser_agent_help_requests force row level security;
alter table browser_agent_help_waiters enable row level security;
alter table browser_agent_help_waiters force row level security;
alter table browser_agent_help_messages enable row level security;
alter table browser_agent_help_messages force row level security;
alter table browser_agent_help_reads enable row level security;
alter table browser_agent_help_reads force row level security;
alter table browser_agent_rfi_contributions enable row level security;
alter table browser_agent_rfi_contributions force row level security;

-- 9. Functions ------------------------------------------------------------------------

-- 9a. config_version ------------------------------------------------------------------
-- Settings: bump on any change other than config_version/updated_by/updated_at.
create or replace function public.browser_agent_settings_bump_version()
returns trigger
language plpgsql
security invoker
set search_path = pg_catalog, public
as $$
begin
  if (to_jsonb(new) - array['config_version', 'updated_by', 'updated_at'])
     is distinct from (to_jsonb(old) - array['config_version', 'updated_by', 'updated_at']) then
    new.config_version := old.config_version + 1;
  end if;
  return new;
end;
$$;

drop trigger if exists browser_agent_settings_version on browser_agent_settings;
create trigger browser_agent_settings_version before update on browser_agent_settings
  for each row execute function public.browser_agent_settings_bump_version();

-- Types and workers: worker-visible columns only. Heartbeat last_seen_at and
-- runtime-state writes never bump it.
create or replace function public.browser_agent_config_touch()
returns trigger
language plpgsql
security invoker
set search_path = pg_catalog, public
as $$
begin
  update browser_agent_settings set config_version = config_version + 1 where id = 1;
  return null;
end;
$$;

drop trigger if exists browser_agent_types_config_touch on browser_agent_types;
create trigger browser_agent_types_config_touch
  after insert or delete or update of enabled, identity, provider, model, max_concurrency, max_steps,
                                      host_narrowing, max_active_seconds
  on browser_agent_types
  for each statement execute function public.browser_agent_config_touch();

drop trigger if exists browser_agent_workers_config_touch on browser_agent_workers;
create trigger browser_agent_workers_config_touch
  after update of status, draining, max_slots, allowed_types, allowed_ips
  on browser_agent_workers
  for each statement execute function public.browser_agent_config_touch();

-- 9b. Ledger: per-job seq and immutability ---------------------------------------------

create or replace function public.browser_agent_steps_assign_seq()
returns trigger
language plpgsql
security invoker
set search_path = pg_catalog, public
as $$
begin
  perform pg_advisory_xact_lock(hashtext('bagent_steps:' || new.job_id::text));
  select coalesce(max(seq), 0) + 1 into new.seq from browser_agent_steps where job_id = new.job_id;
  return new;
end;
$$;

drop trigger if exists browser_agent_steps_seq on browser_agent_steps;
create trigger browser_agent_steps_seq before insert on browser_agent_steps
  for each row execute function public.browser_agent_steps_assign_seq();

-- The only UPDATE allowed is the break-glass redaction: args and/or result
-- become {"redacted":true}, with redacted_at and redacted_by_label stamped.
create or replace function public.browser_agent_steps_guard_update()
returns trigger
language plpgsql
security invoker
set search_path = pg_catalog, public
as $$
declare
  v_redacted constant jsonb := '{"redacted":true}'::jsonb;
  v_keep constant text[] := array['args', 'result', 'redacted_at', 'redacted_by_label'];
begin
  if (to_jsonb(new) - v_keep) is distinct from (to_jsonb(old) - v_keep) then
    raise exception 'browser_agent_steps is append-only';
  end if;
  if new.args is distinct from old.args and new.args is distinct from v_redacted then
    raise exception 'browser_agent_steps: args may only be redacted';
  end if;
  if new.result is distinct from old.result and new.result is distinct from v_redacted then
    raise exception 'browser_agent_steps: result may only be redacted';
  end if;
  if new.redacted_at is null or new.redacted_by_label is null then
    raise exception 'browser_agent_steps: a redaction must stamp redacted_at and redacted_by_label';
  end if;
  return new;
end;
$$;

create or replace function public.browser_agent_refuse_change()
returns trigger
language plpgsql
security invoker
set search_path = pg_catalog, public
as $$
begin
  raise exception '% is append-only (% refused)', tg_table_name, tg_op;
end;
$$;

drop trigger if exists browser_agent_steps_no_update on browser_agent_steps;
create trigger browser_agent_steps_no_update before update on browser_agent_steps
  for each row execute function public.browser_agent_steps_guard_update();
drop trigger if exists browser_agent_steps_no_delete on browser_agent_steps;
create trigger browser_agent_steps_no_delete before delete on browser_agent_steps
  for each row execute function public.browser_agent_refuse_change();
drop trigger if exists browser_agent_steps_no_truncate on browser_agent_steps;
create trigger browser_agent_steps_no_truncate before truncate on browser_agent_steps
  for each statement execute function public.browser_agent_refuse_change();

drop trigger if exists browser_agent_settings_changes_no_update on browser_agent_settings_changes;
create trigger browser_agent_settings_changes_no_update before update on browser_agent_settings_changes
  for each row execute function public.browser_agent_refuse_change();
drop trigger if exists browser_agent_settings_changes_no_delete on browser_agent_settings_changes;
create trigger browser_agent_settings_changes_no_delete before delete on browser_agent_settings_changes
  for each row execute function public.browser_agent_refuse_change();
drop trigger if exists browser_agent_settings_changes_no_truncate on browser_agent_settings_changes;
create trigger browser_agent_settings_changes_no_truncate before truncate on browser_agent_settings_changes
  for each statement execute function public.browser_agent_refuse_change();

-- Help messages: only secret bookkeeping may change (consume, purge, null the
-- ciphertext/nonce/hmac). Never deleted.
create or replace function public.browser_agent_help_messages_guard_update()
returns trigger
language plpgsql
security invoker
set search_path = pg_catalog, public
as $$
declare
  v_keep constant text[] := array['secret_consumed_at', 'secret_consumed_by_job', 'secret_purged_at',
                                  'secret_ciphertext', 'secret_nonce', 'secret_hmac'];
begin
  if (to_jsonb(new) - v_keep) is distinct from (to_jsonb(old) - v_keep) then
    raise exception 'browser_agent_help_messages: only secret bookkeeping may change';
  end if;
  if (new.secret_ciphertext is not null and new.secret_ciphertext is distinct from old.secret_ciphertext)
     or (new.secret_nonce is not null and new.secret_nonce is distinct from old.secret_nonce)
     or (new.secret_hmac is not null and new.secret_hmac is distinct from old.secret_hmac) then
    raise exception 'browser_agent_help_messages: secret material may only be nulled';
  end if;
  if old.secret_consumed_at is not null and new.secret_consumed_at is distinct from old.secret_consumed_at then
    raise exception 'browser_agent_help_messages: a secret is consumed once';
  end if;
  return new;
end;
$$;

drop trigger if exists browser_agent_help_messages_no_update on browser_agent_help_messages;
create trigger browser_agent_help_messages_no_update before update on browser_agent_help_messages
  for each row execute function public.browser_agent_help_messages_guard_update();
drop trigger if exists browser_agent_help_messages_no_delete on browser_agent_help_messages;
create trigger browser_agent_help_messages_no_delete before delete on browser_agent_help_messages
  for each row execute function public.browser_agent_refuse_change();
drop trigger if exists browser_agent_help_messages_no_truncate on browser_agent_help_messages;
create trigger browser_agent_help_messages_no_truncate before truncate on browser_agent_help_messages
  for each statement execute function public.browser_agent_refuse_change();

-- 9c. Shared Procore login RPCs (rfp_harvest_sessions row per provider) -----------------
-- One atomic reservation replaces the read-then-stamp race. Used by the
-- server-side daily sweep / RFP harvest and by login/begin (O2).
create or replace function public.procore_reserve_login(p_provider text, p_min_interval int, p_source text)
returns jsonb
language plpgsql
security invoker
set search_path = pg_catalog, public
as $$
declare
  v_row rfp_harvest_sessions;
begin
  insert into rfp_harvest_sessions (provider) values (p_provider) on conflict (provider) do nothing;

  update rfp_harvest_sessions
     set last_login_attempt_at = now(), last_login_source = p_source, updated_at = now()
   where provider = p_provider
     and (locked_until is null or locked_until <= now())
     and (last_login_attempt_at is null
          or last_login_attempt_at < now() - make_interval(secs => greatest(p_min_interval, 0)))
  returning * into v_row;

  if found then
    return jsonb_build_object('granted', true, 'locked_until', null, 'retry_at', null);
  end if;

  select * into v_row from rfp_harvest_sessions where provider = p_provider;
  if v_row.locked_until is not null and v_row.locked_until > now() then
    return jsonb_build_object('granted', false, 'reason', 'locked',
                              'locked_until', v_row.locked_until, 'retry_at', v_row.locked_until);
  end if;
  return jsonb_build_object('granted', false, 'reason', 'spacing', 'locked_until', null,
                            'retry_at', v_row.last_login_attempt_at + make_interval(secs => greatest(p_min_interval, 0)));
end;
$$;

create or replace function public.procore_record_login_failure(
  p_provider text, p_error text, p_max_failures int, p_lock_seconds int)
returns rfp_harvest_sessions
language plpgsql
security invoker
set search_path = pg_catalog, public
as $$
declare
  v_row rfp_harvest_sessions;
begin
  insert into rfp_harvest_sessions (provider) values (p_provider) on conflict (provider) do nothing;
  update rfp_harvest_sessions
     set login_failures = login_failures + 1,
         last_error = left(p_error, 500),
         locked_until = case when login_failures + 1 >= p_max_failures
                             then now() + make_interval(secs => p_lock_seconds) else locked_until end,
         updated_at = now()
   where provider = p_provider
  returning * into v_row;
  return v_row;
end;
$$;

-- 9d. Job RPCs ---------------------------------------------------------------------------

-- The fence every worker write goes through: the worker is active on this
-- boot, and the job is leased to it at this epoch in one of p_statuses.
-- Locks the job row for the rest of the transaction.
create or replace function public.browser_agent_fence(
  p_job uuid, p_worker uuid, p_boot uuid, p_epoch bigint, p_statuses text[])
returns browser_agent_jobs
language plpgsql
security invoker
set search_path = pg_catalog, public
as $$
declare
  v_worker browser_agent_workers;
  v_job browser_agent_jobs;
begin
  select * into v_worker from browser_agent_workers where id = p_worker;
  if not found then
    raise exception 'unauthorized';
  end if;
  if v_worker.status = 'revoked' then
    raise exception 'revoked';
  end if;
  if v_worker.status <> 'active' or v_worker.boot_id is distinct from p_boot then
    raise exception 'stale_boot';
  end if;
  select * into v_job from browser_agent_jobs where id = p_job for update;
  if not found or v_job.lease_owner is distinct from p_worker or v_job.lease_epoch <> p_epoch
     or v_job.lease_boot_id is distinct from p_boot then
    raise exception 'lease_lost';
  end if;
  if not (v_job.status = any (p_statuses)) then
    raise exception 'job_not_running';
  end if;
  return v_job;
end;
$$;

create or replace function public.browser_agent_log_step(
  p_job uuid, p_epoch bigint, p_worker uuid, p_kind text, p_actor text, p_summary text, p_result jsonb default null)
returns void
language sql
security invoker
set search_path = pg_catalog, public
as $$
  insert into browser_agent_steps (job_id, epoch, worker_id, kind, actor, summary, result)
  values (p_job, p_epoch, p_worker, p_kind, p_actor, left(p_summary, 500), p_result);
$$;

-- Claim (spec 2.8). A boot mismatch or a non-active worker raises; a draining
-- worker or the kill switch gets nothing. Per type: advisory lock, exact
-- max_concurrency, skip-locked rows, epoch + 1, charged only on a retry
-- (a first claim has a null resume_reason, so it charges 1).
create or replace function public.claim_browser_agent_jobs(
  p_worker uuid, p_boot uuid, p_capacity jsonb, p_lease_seconds int default 90)
returns setof browser_agent_jobs
language plpgsql
security invoker
set search_path = pg_catalog, public
as $$
declare
  v_worker browser_agent_workers;
  v_settings browser_agent_settings;
  v_type browser_agent_types;
  v_key text;
  v_want int;
  v_busy int;
  v_take int;
  v_snapshot jsonb;
  v_job browser_agent_jobs;
begin
  select * into v_worker from browser_agent_workers where id = p_worker;
  if not found then
    raise exception 'unauthorized';
  end if;
  if v_worker.status = 'revoked' then
    raise exception 'revoked';
  end if;
  if v_worker.status <> 'active' or v_worker.boot_id is distinct from p_boot then
    raise exception 'stale_boot';
  end if;
  if v_worker.draining then
    return;
  end if;
  select * into v_settings from browser_agent_settings where id = 1;
  if not coalesce(v_settings.enabled, false) then
    return;
  end if;

  for v_key, v_want in
    select c.key, least(greatest((c.value)::int, 0), 8) from jsonb_each_text(coalesce(p_capacity, '{}'::jsonb)) c
  loop
    continue when v_want <= 0;
    continue when not (v_key = any (v_worker.allowed_types));
    continue when v_worker.capabilities is null or not ((v_worker.capabilities -> 'types') ? v_key);
    select * into v_type from browser_agent_types where key = v_key and enabled;
    continue when not found;

    perform pg_advisory_xact_lock(hashtext('bagent:' || v_key));
    select count(*) into v_busy from browser_agent_jobs where agent_type = v_key and lease_owner is not null;
    v_take := least(v_want, v_type.max_concurrency - v_busy);
    continue when v_take <= 0;

    v_snapshot := jsonb_build_object(
      'type', to_jsonb(v_type) - array['updated_by', 'updated_at'],
      'per_job_cost_cap_usd', v_settings.per_job_cost_cap_usd,
      'daily_cost_cap_usd', v_settings.daily_cost_cap_usd,
      'config_version', v_settings.config_version,
      'claimed_at', now());

    for v_job in
      update browser_agent_jobs j
         set status = 'running',
             lease_owner = p_worker,
             lease_boot_id = p_boot,
             lease_epoch = j.lease_epoch + 1,
             lease_expires_at = now() + make_interval(secs => p_lease_seconds),
             attempts = j.attempts + case when coalesce(j.resume_reason, 'retry') = 'retry' then 1 else 0 end,
             last_resume_reason = j.resume_reason,
             resume_reason = null,
             next_attempt_at = null,
             hold_until = null,
             config_snapshot = v_snapshot,
             started_at = coalesce(j.started_at, now())
        from (select id from browser_agent_jobs
               where agent_type = v_key and status = 'queued'
                 and coalesce(next_attempt_at, now()) <= now() and expires_at > now()
               order by priority, created_at
               for update skip locked
               limit v_take) pick
       where j.id = pick.id
      returning j.*
    loop
      update browser_agent_turns set status = 'abandoned'
       where job_id = v_job.id and status = 'thinking';
      insert into browser_agent_steps (job_id, epoch, worker_id, kind, actor, summary, result)
      values (v_job.id, v_job.lease_epoch, p_worker, 'claim', 'bdr',
              'Claimed (attempt ' || v_job.attempts || ', epoch ' || v_job.lease_epoch || ')',
              jsonb_build_object('resume_reason', v_job.last_resume_reason, 'boot_id', p_boot));
      return next v_job;
    end loop;
  end loop;
  return;
end;
$$;

-- Once per worker process start. More than 6 per hour suspends (draining).
-- Leases held under any other boot are released uncharged: running jobs are
-- requeued 'interrupted' (or fail interrupted_repeatedly past the cap),
-- waiting_help jobs are parked (help stays open).
create or replace function public.browser_agent_hello(p_worker uuid, p_boot uuid, p_info jsonb, p_ip inet default null)
returns jsonb
language plpgsql
security invoker
set search_path = pg_catalog, public
as $$
declare
  v_worker browser_agent_workers;
  v_count int;
  v_job browser_agent_jobs;
  v_max int;
  v_requeued int := 0;
  v_failed int := 0;
  v_parked int := 0;
begin
  select * into v_worker from browser_agent_workers where id = p_worker for update;
  if not found then
    raise exception 'unauthorized';
  end if;
  if v_worker.status = 'revoked' then
    raise exception 'revoked';
  end if;

  if v_worker.hello_window_at is null or v_worker.hello_window_at < now() - interval '1 hour' then
    update browser_agent_workers set hello_window_at = now(), hello_count_hour = 1 where id = p_worker;
    v_count := 1;
  else
    update browser_agent_workers set hello_count_hour = hello_count_hour + 1 where id = p_worker
    returning hello_count_hour into v_count;
  end if;

  update browser_agent_workers
     set boot_id = p_boot,
         boot_at = now(),
         version = coalesce(p_info ->> 'version', version),
         chromium_version = coalesce(p_info ->> 'chromium_version', chromium_version),
         bad_versions = coalesce(array(select jsonb_array_elements_text(p_info -> 'bad_versions')), bad_versions),
         capabilities = coalesce(p_info -> 'capabilities', capabilities),
         last_seen_at = now(),
         last_ip = coalesce(p_ip, last_ip)
   where id = p_worker;

  if v_count > 6 and v_worker.status = 'active' and not v_worker.draining then
    update browser_agent_workers set draining = true where id = p_worker;
  end if;

  for v_job in
    select * from browser_agent_jobs
     where lease_owner = p_worker and lease_boot_id is distinct from p_boot
       and status in ('running', 'waiting_help')
     for update
  loop
    if v_job.status = 'waiting_help' then
      update browser_agent_jobs
         set lease_owner = null, lease_boot_id = null, lease_expires_at = null, hold_until = null
       where id = v_job.id;
      perform browser_agent_log_step(v_job.id, v_job.lease_epoch, p_worker, 'job.parked', 'bdr',
                                     'Parked: the worker restarted while waiting for help');
      v_parked := v_parked + 1;
      continue;
    end if;
    select max_interruptions into v_max from browser_agent_types where key = v_job.agent_type;
    if v_job.interrupted_count + 1 > coalesce(v_max, 20) then
      update browser_agent_jobs
         set status = 'failed', error_kind = 'interrupted_repeatedly', error = 'Interrupted too many times',
             interrupted_count = interrupted_count + 1, lease_epoch = lease_epoch + 1,
             lease_owner = null, lease_boot_id = null, lease_expires_at = null, finished_at = now()
       where id = v_job.id;
      perform browser_agent_log_step(v_job.id, v_job.lease_epoch, p_worker, 'job.failed', 'bdr',
                                     'Failed: interrupted too many times');
      v_failed := v_failed + 1;
    else
      update browser_agent_jobs
         set status = 'queued', resume_reason = 'interrupted', interrupted_count = interrupted_count + 1,
             lease_epoch = lease_epoch + 1, lease_owner = null, lease_boot_id = null, lease_expires_at = null
       where id = v_job.id;
      perform browser_agent_log_step(v_job.id, v_job.lease_epoch, p_worker, 'job.interrupted', 'bdr',
                                     'Requeued: the worker restarted (new boot)');
      v_requeued := v_requeued + 1;
    end if;
  end loop;

  select * into v_worker from browser_agent_workers where id = p_worker;
  return jsonb_build_object(
    'status', case when v_worker.status = 'pending' then 'pending'
                   when v_count > 6 then 'suspended' else 'active' end,
    'hello_count_hour', v_count,
    'requeued', v_requeued, 'failed', v_failed, 'parked', v_parked,
    'config_version', (select config_version from browser_agent_settings where id = 1),
    'server_time', now());
end;
$$;

-- Every 15 s. Renews by epoch even when the lease already expired, as long as
-- nobody reclaimed it (owner, epoch and boot unchanged).
create or replace function public.browser_agent_heartbeat(
  p_worker uuid, p_boot uuid, p_jobs jsonb, p_ip inet default null, p_lease_seconds int default 90)
returns jsonb
language plpgsql
security invoker
set search_path = pg_catalog, public
as $$
declare
  v_worker browser_agent_workers;
  v_item record;
  v_job browser_agent_jobs;
  v_renewed jsonb := '[]'::jsonb;
  v_lost jsonb := '[]'::jsonb;
  v_cancel jsonb := '[]'::jsonb;
  v_listed uuid[] := '{}';
begin
  select * into v_worker from browser_agent_workers where id = p_worker;
  if not found then
    raise exception 'unauthorized';
  end if;
  if v_worker.status = 'revoked' then
    raise exception 'revoked';
  end if;
  if v_worker.status <> 'active' or v_worker.boot_id is distinct from p_boot then
    raise exception 'stale_boot';
  end if;
  update browser_agent_workers set last_seen_at = now(), last_ip = coalesce(p_ip, last_ip) where id = p_worker;

  for v_item in
    select (e ->> 'job_id')::uuid as job_id, (e ->> 'epoch')::bigint as epoch,
           least(greatest(coalesce((e ->> 'active_seconds_delta')::int, 0), 0), 30) as delta
      from jsonb_array_elements(coalesce(p_jobs, '[]'::jsonb)) e
  loop
    v_listed := v_listed || v_item.job_id;
    update browser_agent_jobs
       set lease_expires_at = now() + make_interval(secs => p_lease_seconds),
           active_seconds = active_seconds + case when status = 'running' then v_item.delta else 0 end
     where id = v_item.job_id and lease_owner = p_worker and lease_epoch = v_item.epoch
       and lease_boot_id = p_boot and status in ('running', 'waiting_help')
    returning * into v_job;
    if found then
      v_renewed := v_renewed || jsonb_build_object('job_id', v_job.id, 'lease_expires_at', v_job.lease_expires_at);
      if v_job.cancel_requested_at is not null then
        v_cancel := v_cancel || to_jsonb(v_job.id);
      end if;
    else
      v_lost := v_lost || to_jsonb(v_item.job_id);
    end if;
  end loop;

  return jsonb_build_object(
    'config_version', (select config_version from browser_agent_settings where id = 1),
    'renewed', v_renewed,
    'lost', v_lost,
    'cancel', v_cancel,
    'orphans', coalesce((
      select jsonb_agg(jsonb_build_object('job_id', j.id, 'epoch', j.lease_epoch))
        from browser_agent_jobs j
       where j.lease_owner = p_worker and j.lease_boot_id = p_boot
         and j.status in ('running', 'waiting_help') and not (j.id = any (v_listed))), '[]'::jsonb),
    'help_updates', coalesce((
      select jsonb_agg(jsonb_build_object(
               'job_id', w.job_id, 'help_id', h.id, 'status', h.status,
               'latest_message_id', (select max(m.id) from browser_agent_help_messages m where m.help_id = h.id)))
        from browser_agent_help_waiters w
        join browser_agent_help_requests h on h.id = w.help_id
       where w.job_id = any (v_listed)), '[]'::jsonb),
    'drain', v_worker.draining,
    'controls', '[]'::jsonb);
end;
$$;

-- Steps from the worker. Current epoch: inserted normally. An older epoch this
-- worker really held (a claim step proves it): inserted with late = true.
-- Duplicate idem_keys are skipped.
create or replace function public.browser_agent_append_steps(
  p_job uuid, p_worker uuid, p_boot uuid, p_epoch bigint, p_rows jsonb)
returns jsonb
language plpgsql
security invoker
set search_path = pg_catalog, public
as $$
declare
  v_late boolean := false;
  v_total int;
  v_inserted int;
begin
  begin
    perform browser_agent_fence(p_job, p_worker, p_boot, p_epoch, array['running', 'waiting_help']);
  exception when others then
    if sqlerrm in ('lease_lost', 'job_not_running', 'stale_boot')
       and exists (select 1 from browser_agent_steps
                    where job_id = p_job and kind = 'claim' and epoch = p_epoch and worker_id = p_worker) then
      v_late := true;
    else
      raise;
    end if;
  end;

  if exists (select 1 from jsonb_array_elements(p_rows) e
              where coalesce(e ->> 'actor', '') not in ('worker', 'playbook', 'model')) then
    raise exception 'invalid_request';
  end if;

  v_total := jsonb_array_length(p_rows);
  insert into browser_agent_steps (job_id, epoch, late, worker_id, idem_key, kind, actor, summary, tool_name,
                                   args, result, payload_sha256, url_before, url_after, screenshot_artifact_id,
                                   turn_no, ok, duration_ms)
  select p_job, p_epoch, v_late, p_worker, r.idem_key, r.kind, r.actor, left(r.summary, 500), r.tool_name,
         r.args, r.result, r.payload_sha256, left(r.url_before, 2048), left(r.url_after, 2048),
         r.screenshot_artifact_id, r.turn_no, r.ok, r.duration_ms
    from jsonb_to_recordset(p_rows) as r(idem_key text, kind text, actor text, summary text, tool_name text,
                                         args jsonb, result jsonb, payload_sha256 text, url_before text,
                                         url_after text, screenshot_artifact_id uuid, turn_no int, ok boolean,
                                         duration_ms int)
  on conflict (job_id, idem_key) where idem_key is not null do nothing;
  get diagnostics v_inserted = row_count;

  return jsonb_build_object(
    'appended', case when v_late then 0 else v_inserted end,
    'duplicates', v_total - v_inserted,
    'late', case when v_late then v_inserted else 0 end);
end;
$$;

create or replace function public.browser_agent_checkpoint(
  p_job uuid, p_worker uuid, p_boot uuid, p_epoch bigint, p_seq int, p_data jsonb)
returns boolean
language plpgsql
security invoker
set search_path = pg_catalog, public
as $$
declare
  v_job browser_agent_jobs;
begin
  v_job := browser_agent_fence(p_job, p_worker, p_boot, p_epoch, array['running', 'waiting_help']);
  if p_seq <= v_job.checkpoint_seq then
    return false;
  end if;
  update browser_agent_jobs set checkpoint = coalesce(p_data, '{}'::jsonb), checkpoint_seq = p_seq where id = p_job;
  return true;
end;
$$;

-- Open or join the help request for a scope. The first opener holds the live
-- page. Suppression of recently given-up link scopes is checked by the API
-- before this call.
create or replace function public.browser_agent_open_help(
  p_job uuid, p_worker uuid, p_boot uuid, p_epoch bigint, p_scope text, p_reason text, p_title text,
  p_message text, p_shot uuid, p_project uuid, p_hold_seconds int, p_expires_hours int)
returns jsonb
language plpgsql
security invoker
set search_path = pg_catalog, public
as $$
declare
  v_job browser_agent_jobs;
  v_help browser_agent_help_requests;
  v_joined boolean := false;
  v_hold timestamptz := now() + make_interval(secs => p_hold_seconds);
begin
  v_job := browser_agent_fence(p_job, p_worker, p_boot, p_epoch, array['running']);

  insert into browser_agent_help_requests (scope_key, agent_type, reason_code, title, agent_message,
                                           screenshot_artifact_id, project_id, opened_by_job_id, holder_job_id,
                                           expires_at)
  values (p_scope, v_job.agent_type, p_reason, p_title, left(p_message, 1000), p_shot, p_project, p_job, p_job,
          now() + make_interval(hours => p_expires_hours))
  on conflict (scope_key) where status in ('open', 'answered') do nothing
  returning * into v_help;

  if not found then
    v_joined := true;
    select * into v_help from browser_agent_help_requests
     where scope_key = p_scope and status in ('open', 'answered');
  end if;

  insert into browser_agent_help_waiters (help_id, job_id) values (v_help.id, p_job) on conflict do nothing;
  update browser_agent_jobs set status = 'waiting_help', waiting_help_id = v_help.id, hold_until = v_hold
   where id = p_job;
  perform browser_agent_log_step(p_job, p_epoch, p_worker, 'help.opened', 'bdr',
                                 case when v_joined then 'Joined help request ' else 'Opened help request ' end
                                 || v_help.id::text || ' (' || p_reason || ')',
                                 jsonb_build_object('help_id', v_help.id, 'scope', p_scope, 'joined', v_joined));
  return jsonb_build_object('help_id', v_help.id, 'joined_existing', v_joined,
                            'holder', v_help.holder_job_id = p_job, 'hold_until', v_hold);
end;
$$;

-- Hold expired: release the lease, keep waiting_help. An answer requeues it.
create or replace function public.browser_agent_park(p_job uuid, p_worker uuid, p_boot uuid, p_epoch bigint)
returns void
language plpgsql
security invoker
set search_path = pg_catalog, public
as $$
begin
  perform browser_agent_fence(p_job, p_worker, p_boot, p_epoch, array['waiting_help']);
  update browser_agent_jobs
     set lease_owner = null, lease_boot_id = null, lease_expires_at = null, hold_until = null
   where id = p_job;
  perform browser_agent_log_step(p_job, p_epoch, p_worker, 'help.parked', 'bdr', 'Parked until a helper answers');
end;
$$;

-- A validated capture (the API checks the artifact first) becomes one
-- pm_rfi_sync_runs row with trigger 'agent'; the job waits in applying until
-- the sweeper reconciles it from the run.
create or replace function public.browser_agent_submit_rfi_capture(
  p_job uuid, p_worker uuid, p_boot uuid, p_epoch bigint, p_artifact uuid, p_external_rfi_id text)
returns uuid
language plpgsql
security invoker
set search_path = pg_catalog, public
as $$
declare
  v_job browser_agent_jobs;
  v_run uuid;
begin
  v_job := browser_agent_fence(p_job, p_worker, p_boot, p_epoch, array['running', 'waiting_help']);
  if v_job.project_id is null or p_external_rfi_id !~ '^\d{1,20}$' then
    raise exception 'capture_invalid';
  end if;

  insert into pm_rfi_sync_runs (project_id, trigger, agent_job_id, apply_attempt, detail_artifact_id,
                                target_external_rfi_id, source_email_id)
  values (v_job.project_id, 'agent', p_job, v_job.apply_attempt, p_artifact, p_external_rfi_id, v_job.source_email_id)
  on conflict (agent_job_id, apply_attempt) where agent_job_id is not null do nothing;

  select id into v_run from pm_rfi_sync_runs where agent_job_id = p_job and apply_attempt = v_job.apply_attempt;

  update browser_agent_jobs
     set status = 'applying', outcome = 'pending', sync_run_id = v_run, hold_until = null,
         lease_owner = null, lease_boot_id = null, lease_expires_at = null
   where id = p_job;
  perform browser_agent_log_step(p_job, p_epoch, p_worker, 'capture.submit', 'bdr',
                                 'Capture of RFI ' || p_external_rfi_id || ' queued for apply',
                                 jsonb_build_object('run_id', v_run, 'artifact_id', p_artifact));
  return v_run;
end;
$$;

-- Terminal write from the worker. The same terminal status again is a no-op.
create or replace function public.browser_agent_finish(
  p_job uuid, p_worker uuid, p_boot uuid, p_epoch bigint, p_status text, p_outcome text,
  p_error_kind text, p_error text, p_result jsonb)
returns boolean
language plpgsql
security invoker
set search_path = pg_catalog, public
as $$
begin
  if p_status not in ('succeeded', 'failed', 'canceled') then
    raise exception 'invalid_request';
  end if;
  if exists (select 1 from browser_agent_jobs where id = p_job and status = p_status) then
    return false;
  end if;
  perform browser_agent_fence(p_job, p_worker, p_boot, p_epoch, array['running', 'waiting_help']);
  update browser_agent_jobs
     set status = p_status, outcome = p_outcome, error_kind = p_error_kind, error = left(p_error, 1000),
         result = p_result, finished_at = now(), hold_until = null,
         lease_owner = null, lease_boot_id = null, lease_expires_at = null
   where id = p_job;
  perform browser_agent_log_step(p_job, p_epoch, p_worker, 'job.finished', 'bdr',
                                 'Finished ' || p_status || coalesce(' (' || coalesce(p_outcome, p_error_kind) || ')', ''));
  return true;
end;
$$;

-- Give the job back. shutdown/drain need worker.draining or service_stop;
-- crash is charged (resume_reason retry, fails past max_attempts);
-- interrupted counts toward max_interruptions; the rest are free.
create or replace function public.browser_agent_requeue(
  p_job uuid, p_worker uuid, p_boot uuid, p_epoch bigint, p_reason text, p_retry_at timestamptz,
  p_service_stop boolean default false)
returns text
language plpgsql
security invoker
set search_path = pg_catalog, public
as $$
declare
  v_job browser_agent_jobs;
  v_type browser_agent_types;
  v_draining boolean;
  v_resume text;
begin
  if p_reason not in ('shutdown', 'drain', 'crash', 'login_spacing', 'provider_outage', 'interrupted') then
    raise exception 'invalid_request';
  end if;
  v_job := browser_agent_fence(p_job, p_worker, p_boot, p_epoch, array['running', 'waiting_help']);
  select draining into v_draining from browser_agent_workers where id = p_worker;
  if p_reason in ('shutdown', 'drain') and not (v_draining or coalesce(p_service_stop, false)) then
    raise exception 'invalid_request';
  end if;
  select * into v_type from browser_agent_types where key = v_job.agent_type;

  if p_reason = 'crash' and v_job.attempts >= v_type.max_attempts then
    update browser_agent_jobs
       set status = 'failed', error_kind = 'crashed_repeatedly', error = 'Crashed too many times',
           lease_epoch = lease_epoch + 1, finished_at = now(), hold_until = null,
           lease_owner = null, lease_boot_id = null, lease_expires_at = null
     where id = p_job;
    perform browser_agent_log_step(p_job, p_epoch, p_worker, 'job.failed', 'bdr', 'Failed: crashed too many times');
    return 'failed';
  end if;
  if p_reason = 'interrupted' and v_job.interrupted_count + 1 > v_type.max_interruptions then
    update browser_agent_jobs
       set status = 'failed', error_kind = 'interrupted_repeatedly', error = 'Interrupted too many times',
           interrupted_count = interrupted_count + 1, lease_epoch = lease_epoch + 1, finished_at = now(),
           hold_until = null, lease_owner = null, lease_boot_id = null, lease_expires_at = null
     where id = p_job;
    perform browser_agent_log_step(p_job, p_epoch, p_worker, 'job.failed', 'bdr', 'Failed: interrupted too many times');
    return 'failed';
  end if;

  v_resume := case p_reason when 'crash' then 'retry' else p_reason end;
  update browser_agent_jobs
     set status = 'queued', resume_reason = v_resume, next_attempt_at = p_retry_at,
         interrupted_count = interrupted_count
                             + case when p_reason in ('interrupted', 'shutdown', 'drain') then 1 else 0 end,
         lease_epoch = lease_epoch + 1, hold_until = null,
         lease_owner = null, lease_boot_id = null, lease_expires_at = null
   where id = p_job;
  perform browser_agent_log_step(p_job, p_epoch, p_worker, 'job.requeued', 'bdr',
                                 'Requeued (' || p_reason || ')',
                                 jsonb_build_object('reason', p_reason, 'retry_at', p_retry_at));
  return 'queued';
end;
$$;

-- 9e. Settings and spend ------------------------------------------------------------------

-- One transaction: update the row, one change-log row per changed field, one
-- audit_log row. Only whitelisted columns per scope. Returns the changed
-- fields and the new config_version.
create or replace function public.browser_agent_update_settings(
  p_scope text, p_target text, p_patch jsonb, p_actor uuid, p_actor_label text, p_probe jsonb default null)
returns jsonb
language plpgsql
security invoker
set search_path = pg_catalog, public
as $$
declare
  v_table text;
  v_where text;
  v_allowed text[];
  v_old jsonb;
  v_new jsonb;
  v_sets text;
  v_key text;
  v_changed text[] := '{}';
  v_patch jsonb := coalesce(p_patch, '{}'::jsonb);
begin
  if p_scope = 'global' then
    v_table := 'browser_agent_settings';
    v_where := 'id = 1';
    v_allowed := array['enabled', 'daily_cost_cap_usd', 'per_job_cost_cap_usd', 'help_hold_seconds',
                       'help_reminder_minutes', 'help_claim_lapse_minutes', 'help_expire_hours',
                       'worker_offline_alert_minutes', 'worker_offline_email_hours', 'job_ttl_hours',
                       'desired_worker_version'];
    v_patch := v_patch || jsonb_build_object('updated_by', p_actor, 'updated_at', now());
  elsif p_scope = 'agent_type' then
    v_table := 'browser_agent_types';
    v_where := format('key = %L', p_target);
    v_allowed := array['label', 'enabled', 'provider', 'model', 'price_in_per_mtok', 'price_out_per_mtok',
                       'model_probe_ok_at', 'max_concurrency', 'max_attempts', 'max_interruptions', 'max_steps',
                       'max_total_tokens', 'max_active_seconds', 'host_narrowing', 'prompt_version',
                       'priority_default'];
    v_patch := v_patch || jsonb_build_object('updated_by', p_actor, 'updated_at', now());
  elsif p_scope = 'worker' then
    v_table := 'browser_agent_workers';
    v_where := format('id = %L::uuid', p_target);
    v_allowed := array['name', 'status', 'draining', 'max_slots', 'allowed_types', 'allowed_ips'];
    if v_patch ? 'status' then
      if v_patch ->> 'status' = 'active' then
        v_patch := v_patch || jsonb_build_object('approved_by', p_actor, 'approved_at', now());
      elsif v_patch ->> 'status' = 'revoked' then
        v_patch := v_patch || jsonb_build_object('revoked_by', p_actor, 'revoked_at', now(), 'token_hash', null);
      else
        raise exception 'invalid_request';
      end if;
    end if;
  else
    raise exception 'invalid_request';
  end if;

  for v_key in select jsonb_object_keys(coalesce(p_patch, '{}'::jsonb)) loop
    if not (v_key = any (v_allowed)) then
      raise exception 'invalid_request' using detail = 'field not editable: ' || v_key;
    end if;
  end loop;

  execute format('select to_jsonb(t) from %I t where %s for update', v_table, v_where) into v_old;
  if v_old is null then
    raise exception 'invalid_request' using detail = 'no such target';
  end if;

  select string_agg(format('%I = r.%I', k, k), ', ') into v_sets from jsonb_object_keys(v_patch) k;
  execute format('update %I t set %s from jsonb_populate_record(null::%I, $1) r where t.%s',
                 v_table, v_sets, v_table, v_where)
    using v_old || v_patch;
  execute format('select to_jsonb(t) from %I t where %s', v_table, v_where) into v_new;

  for v_key in select jsonb_object_keys(coalesce(p_patch, '{}'::jsonb)) loop
    if (v_old -> v_key) is distinct from (v_new -> v_key) then
      v_changed := v_changed || v_key;
      insert into browser_agent_settings_changes (scope, target, field, old_value, new_value, probe,
                                                  changed_by, changed_by_label)
      values (p_scope, p_target, v_key, v_old -> v_key, v_new -> v_key, p_probe, p_actor, p_actor_label);
    end if;
  end loop;

  if array_length(v_changed, 1) > 0 then
    insert into audit_log (actor_id, action, entity, entity_id, payload)
    values (p_actor, 'browser_agent.settings_update', v_table,
            case when p_scope = 'worker' then p_target::uuid else null end,
            jsonb_build_object('scope', p_scope, 'target', p_target, 'fields', to_jsonb(v_changed),
                               'actor_label', p_actor_label));
  end if;

  return jsonb_build_object('changed', to_jsonb(v_changed),
                            'config_version', (select config_version from browser_agent_settings where id = 1));
end;
$$;

-- The daily spend window: the later of today 00:00 America/Los_Angeles and
-- the last IT Admin reset. Spend = model turn cost (estimates included).
create or replace function public.browser_agent_spend_today()
returns jsonb
language sql
stable
security invoker
set search_path = pg_catalog, public
as $$
  with w as (
    select greatest(
             (date_trunc('day', now() at time zone 'America/Los_Angeles') at time zone 'America/Los_Angeles'),
             coalesce(rs.cost_window_started_at, '-infinity'::timestamptz)) as started_at
      from browser_agent_runtime_state rs where rs.id = 1
  )
  select jsonb_build_object(
           'window_started_at', w.started_at,
           'spend_usd', coalesce((select sum(t.cost_usd) from browser_agent_turns t where t.created_at >= w.started_at), 0),
           'daily_cap_usd', s.daily_cost_cap_usd,
           'per_job_cap_usd', s.per_job_cost_cap_usd)
    from w, browser_agent_settings s where s.id = 1;
$$;

-- O4: IT Admin resets the daily window (audited in the change log and
-- audit_log; alert flags re-arm).
create or replace function public.browser_agent_reset_spend_window(p_actor uuid, p_actor_label text, p_note text default null)
returns jsonb
language plpgsql
security invoker
set search_path = pg_catalog, public
as $$
declare
  v_before jsonb := browser_agent_spend_today();
  v_after jsonb;
begin
  update browser_agent_runtime_state
     set cost_window_started_at = now(), cost_window_reset_by = p_actor,
         half_cap_notified_at = null, cap_hit_notified_at = null, updated_at = now()
   where id = 1;
  v_after := browser_agent_spend_today();
  insert into browser_agent_settings_changes (scope, target, field, old_value, new_value, changed_by, changed_by_label)
  values ('spend', null, 'cost_window_reset',
          jsonb_build_object('window_started_at', v_before -> 'window_started_at', 'spend_usd', v_before -> 'spend_usd'),
          jsonb_build_object('window_started_at', v_after -> 'window_started_at', 'note', left(p_note, 500)),
          p_actor, p_actor_label);
  insert into audit_log (actor_id, action, entity, payload)
  values (p_actor, 'browser_agent.spend_reset', 'browser_agent_runtime_state',
          jsonb_build_object('spend_before_usd', v_before -> 'spend_usd', 'actor_label', p_actor_label,
                             'note', left(p_note, 500)));
  return v_after;
end;
$$;

-- 10. Grants: service_role only --------------------------------------------------------------

revoke all on function public.browser_agent_settings_bump_version() from public;
revoke all on function public.browser_agent_settings_bump_version() from anon, authenticated;
grant execute on function public.browser_agent_settings_bump_version() to service_role;
revoke all on function public.browser_agent_config_touch() from public;
revoke all on function public.browser_agent_config_touch() from anon, authenticated;
grant execute on function public.browser_agent_config_touch() to service_role;
revoke all on function public.browser_agent_steps_assign_seq() from public;
revoke all on function public.browser_agent_steps_assign_seq() from anon, authenticated;
grant execute on function public.browser_agent_steps_assign_seq() to service_role;
revoke all on function public.browser_agent_steps_guard_update() from public;
revoke all on function public.browser_agent_steps_guard_update() from anon, authenticated;
grant execute on function public.browser_agent_steps_guard_update() to service_role;
revoke all on function public.browser_agent_refuse_change() from public;
revoke all on function public.browser_agent_refuse_change() from anon, authenticated;
grant execute on function public.browser_agent_refuse_change() to service_role;
revoke all on function public.browser_agent_help_messages_guard_update() from public;
revoke all on function public.browser_agent_help_messages_guard_update() from anon, authenticated;
grant execute on function public.browser_agent_help_messages_guard_update() to service_role;
revoke all on function public.procore_reserve_login(text, int, text) from public;
revoke all on function public.procore_reserve_login(text, int, text) from anon, authenticated;
grant execute on function public.procore_reserve_login(text, int, text) to service_role;
revoke all on function public.procore_record_login_failure(text, text, int, int) from public;
revoke all on function public.procore_record_login_failure(text, text, int, int) from anon, authenticated;
grant execute on function public.procore_record_login_failure(text, text, int, int) to service_role;
revoke all on function public.browser_agent_fence(uuid, uuid, uuid, bigint, text[]) from public;
revoke all on function public.browser_agent_fence(uuid, uuid, uuid, bigint, text[]) from anon, authenticated;
grant execute on function public.browser_agent_fence(uuid, uuid, uuid, bigint, text[]) to service_role;
revoke all on function public.browser_agent_log_step(uuid, bigint, uuid, text, text, text, jsonb) from public;
revoke all on function public.browser_agent_log_step(uuid, bigint, uuid, text, text, text, jsonb) from anon, authenticated;
grant execute on function public.browser_agent_log_step(uuid, bigint, uuid, text, text, text, jsonb) to service_role;
revoke all on function public.claim_browser_agent_jobs(uuid, uuid, jsonb, int) from public;
revoke all on function public.claim_browser_agent_jobs(uuid, uuid, jsonb, int) from anon, authenticated;
grant execute on function public.claim_browser_agent_jobs(uuid, uuid, jsonb, int) to service_role;
revoke all on function public.browser_agent_hello(uuid, uuid, jsonb, inet) from public;
revoke all on function public.browser_agent_hello(uuid, uuid, jsonb, inet) from anon, authenticated;
grant execute on function public.browser_agent_hello(uuid, uuid, jsonb, inet) to service_role;
revoke all on function public.browser_agent_heartbeat(uuid, uuid, jsonb, inet, int) from public;
revoke all on function public.browser_agent_heartbeat(uuid, uuid, jsonb, inet, int) from anon, authenticated;
grant execute on function public.browser_agent_heartbeat(uuid, uuid, jsonb, inet, int) to service_role;
revoke all on function public.browser_agent_append_steps(uuid, uuid, uuid, bigint, jsonb) from public;
revoke all on function public.browser_agent_append_steps(uuid, uuid, uuid, bigint, jsonb) from anon, authenticated;
grant execute on function public.browser_agent_append_steps(uuid, uuid, uuid, bigint, jsonb) to service_role;
revoke all on function public.browser_agent_checkpoint(uuid, uuid, uuid, bigint, int, jsonb) from public;
revoke all on function public.browser_agent_checkpoint(uuid, uuid, uuid, bigint, int, jsonb) from anon, authenticated;
grant execute on function public.browser_agent_checkpoint(uuid, uuid, uuid, bigint, int, jsonb) to service_role;
revoke all on function public.browser_agent_open_help(uuid, uuid, uuid, bigint, text, text, text, text, uuid, uuid, int, int) from public;
revoke all on function public.browser_agent_open_help(uuid, uuid, uuid, bigint, text, text, text, text, uuid, uuid, int, int) from anon, authenticated;
grant execute on function public.browser_agent_open_help(uuid, uuid, uuid, bigint, text, text, text, text, uuid, uuid, int, int) to service_role;
revoke all on function public.browser_agent_park(uuid, uuid, uuid, bigint) from public;
revoke all on function public.browser_agent_park(uuid, uuid, uuid, bigint) from anon, authenticated;
grant execute on function public.browser_agent_park(uuid, uuid, uuid, bigint) to service_role;
revoke all on function public.browser_agent_submit_rfi_capture(uuid, uuid, uuid, bigint, uuid, text) from public;
revoke all on function public.browser_agent_submit_rfi_capture(uuid, uuid, uuid, bigint, uuid, text) from anon, authenticated;
grant execute on function public.browser_agent_submit_rfi_capture(uuid, uuid, uuid, bigint, uuid, text) to service_role;
revoke all on function public.browser_agent_finish(uuid, uuid, uuid, bigint, text, text, text, text, jsonb) from public;
revoke all on function public.browser_agent_finish(uuid, uuid, uuid, bigint, text, text, text, text, jsonb) from anon, authenticated;
grant execute on function public.browser_agent_finish(uuid, uuid, uuid, bigint, text, text, text, text, jsonb) to service_role;
revoke all on function public.browser_agent_requeue(uuid, uuid, uuid, bigint, text, timestamptz, boolean) from public;
revoke all on function public.browser_agent_requeue(uuid, uuid, uuid, bigint, text, timestamptz, boolean) from anon, authenticated;
grant execute on function public.browser_agent_requeue(uuid, uuid, uuid, bigint, text, timestamptz, boolean) to service_role;
revoke all on function public.browser_agent_update_settings(text, text, jsonb, uuid, text, jsonb) from public;
revoke all on function public.browser_agent_update_settings(text, text, jsonb, uuid, text, jsonb) from anon, authenticated;
grant execute on function public.browser_agent_update_settings(text, text, jsonb, uuid, text, jsonb) to service_role;
revoke all on function public.browser_agent_spend_today() from public;
revoke all on function public.browser_agent_spend_today() from anon, authenticated;
grant execute on function public.browser_agent_spend_today() to service_role;
revoke all on function public.browser_agent_reset_spend_window(uuid, text, text) from public;
revoke all on function public.browser_agent_reset_spend_window(uuid, text, text) from anon, authenticated;
grant execute on function public.browser_agent_reset_spend_window(uuid, text, text) to service_role;

notify pgrst, 'reload schema';
