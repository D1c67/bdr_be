-- 0192: Assistant browser v2 (docs/ASSISTANT_BROWSER_V2_PLAN.md section 14,
-- owner decisions in docs/BROWSER_V2_OWNER_DECISIONS.md). Dev project (BDR) only.
--
-- One file, six ledger pieces split by the marker lines below (0190 style;
-- scripts/check_migration_prefixes.sh allows one file per number). Apply in order:
--   0192_assistant_browser_v2_a1  tables, columns, indexes, seeds, budget default, pure helpers
--   0192_assistant_browser_v2_a2  grants, admission, enqueue, credential functions
--   0192_assistant_browser_v2_a3  v2 fence, job guard, policy, login and session functions
--   0192_assistant_browser_v2_a4  help channel, holds, turn begin, requeue
--   0192_assistant_browser_v2_a5  sweep, safe rules, downloads, live open, outbox, claim, terminal trigger
--   0192_assistant_browser_v2_b   owner at the screen: constraint swaps and every function whose
--                                 body removes rows
-- Part A (a1 to a5) is additive only; its one destructive-looking word is the
-- foreign key referential action. Every piece is at most 35 KB and revokes and
-- grants each function it creates, so a piece applied alone is safe. v2 jobs
-- can be inserted only after part B (the job shape check).
--
-- Conventions (0186 to 0189): forced RLS with no policies, nothing granted to
-- anon or authenticated, every function SECURITY INVOKER with search_path
-- pinned and executable by service_role only. Per-owner work serializes on
-- the advisory lock 'assistant-browser-owner:<user id>'.
--
-- Owner answer 5 (2026-11-02): the daily assistant budget is 15 USD per user
-- for every user. A1 moves the column default of
-- assistant_profiles.daily_cost_cap_usd from 5 to 15 and moves rows that
-- still hold exactly the old default (5) to 15.
--
-- ==== PART A1 ====

-- A1.1 Site registry (plan 5.1).
create table if not exists public.assistant_browser_sites (
 site_key text primary key check (site_key ~ '^[a-z0-9_.:-]{2,80}$'),
 label text not null check (length(label) between 1 and 100),
 kind text not null check (kind in ('curated','ad_hoc')),
 registrable_domains text[] not null default '{}',
 exact_origins text[] not null default '{}',
 path_prefix text check (path_prefix is null or (path_prefix ~ '^/' and length(path_prefix) <= 512)),
 login_origins text[] not null default '{}',
 login_paths text[] not null default '{}',
 login_kind text not null default 'password' check (login_kind in ('password','password_mfa','sso_only','none')),
 cookie_domains text[] not null default '{}',
 code_email_senders text[] not null default '{}',
 code_email_regex text check (code_email_regex is null or length(code_email_regex) <= 500),
 profile jsonb not null default '{}'::jsonb check (jsonb_typeof(profile) = 'object'),
 needs_headed boolean not null default false,
 challenge_count int not null default 0 check (challenge_count >= 0),
 prefer_api text check (prefer_api is null or length(prefer_api) <= 500),
 enabled boolean not null default true,
 created_by uuid references public.profiles(id) on delete set null,
 created_at timestamptz not null default now(),
 updated_at timestamptz not null default now(),
 constraint assistant_browser_sites_has_scope check (cardinality(registrable_domains) + cardinality(exact_origins) >= 1),
 constraint assistant_browser_sites_ws_curated check (kind = 'curated' or coalesce(profile->>'websocket_ok','false') = 'false')
);

-- A1.2 Per-user standing site approvals (5.2).
create table if not exists public.assistant_browser_site_grants (
 id uuid primary key default gen_random_uuid(),
 user_id uuid not null references public.profiles(id) on delete cascade,
 site_key text not null references public.assistant_browser_sites(site_key),
 scope text not null check (scope in ('domain','origin')),
 value text not null check (length(value) between 1 and 300),
 path_prefix text check (path_prefix is null or (path_prefix ~ '^/' and length(path_prefix) <= 512)),
 version int not null default 1 check (version > 0),
 status text not null default 'active' check (status in ('active','revoked')),
 granted_at timestamptz not null default now(),
 granted_via text not null check (granted_via in ('chat_card','settings')),
 provenance text not null check (provenance in ('typed','bdr_record','search','email','page','unknown','settings')),
 request_id uuid,
 revoked_at timestamptz,
 updated_at timestamptz not null default now()
);
create unique index if not exists assistant_browser_site_grants_active_uq
 on public.assistant_browser_site_grants(user_id, site_key) where status = 'active';
create index if not exists assistant_browser_site_grants_user_idx
 on public.assistant_browser_site_grants(user_id, granted_at desc);

-- A1.3 Task admissions (5.3). One admission becomes at most one job.
create table if not exists public.assistant_browser_admissions (
 id uuid primary key default gen_random_uuid(),
 user_id uuid not null references public.profiles(id) on delete cascade,
 role text not null,
 conversation_id uuid not null,
 run_id uuid,
 goal text not null check (length(goal) between 1 and 2000),
 start_url text check (start_url is null or length(start_url) <= 2048),
 start_provenance text not null default 'unknown'
  check (start_provenance in ('typed','bdr_record','search','email','page','unknown','none')),
 start_site_key text not null check (start_site_key ~ '^[a-z0-9_.:-]{2,80}$'),
 site_scope jsonb not null default '[]'::jsonb check (jsonb_typeof(site_scope) = 'array'),
 account_label text check (account_label is null or account_label ~ '^[A-Za-z0-9 _.-]{1,40}$'),
 project_id uuid,
 model text,
 max_cost_usd numeric(8,2) not null check (max_cost_usd > 0 and max_cost_usd <= 5),
 tainted boolean not null default false,
 fresh_user_message boolean not null default true,
 start_approved_at timestamptz,
 link_approved_at timestamptz,
 needs text[] not null default '{}',
 status text not null default 'waiting_grant' check (status in
  ('waiting_grant','waiting_credential','waiting_link','waiting_start','ready','enqueued','declined','expired')),
 job_id uuid,
 expires_at timestamptz not null default now() + interval '24 hours',
 created_at timestamptz not null default now(),
 updated_at timestamptz not null default now()
);
create index if not exists assistant_browser_admissions_user_idx
 on public.assistant_browser_admissions(user_id, created_at desc);
create index if not exists assistant_browser_admissions_open_idx
 on public.assistant_browser_admissions(expires_at) where status like 'waiting_%' or status = 'ready';

-- A1.4 Credentials pinned per job (5.3 step 7). credential_id is null for a
-- session-only account (wave E live sign-in).
create table if not exists public.assistant_browser_job_credentials (
 id uuid primary key default gen_random_uuid(),
 admission_id uuid not null references public.assistant_browser_admissions(id) on delete cascade,
 job_id uuid,
 user_id uuid not null references public.profiles(id) on delete cascade,
 site_key text not null,
 account_label text not null check (account_label ~ '^[A-Za-z0-9 _.-]{1,40}$'),
 credential_id uuid references public.assistant_credentials(id) on delete cascade,
 credential_version int check (credential_version is null or credential_version > 0),
 created_at timestamptz not null default now(),
 updated_at timestamptz not null default now(),
 constraint assistant_browser_job_credentials_pin check ((credential_id is null) = (credential_version is null))
);
create unique index if not exists assistant_browser_job_credentials_site_uq
 on public.assistant_browser_job_credentials(admission_id, site_key);
create index if not exists assistant_browser_job_credentials_job_idx
 on public.assistant_browser_job_credentials(job_id) where job_id is not null;
create index if not exists assistant_browser_job_credentials_cred_idx
 on public.assistant_browser_job_credentials(credential_id) where credential_id is not null;

-- A1.5 Card requests (site access, credential, email link, task start).
create table if not exists public.assistant_browser_requests (
 id uuid primary key default gen_random_uuid(),
 user_id uuid not null references public.profiles(id) on delete cascade,
 conversation_id uuid,
 admission_id uuid references public.assistant_browser_admissions(id) on delete cascade,
 job_id uuid,
 kind text not null check (kind in ('site_access','credential','link_open','task_start')),
 site_key text check (site_key is null or site_key ~ '^[a-z0-9_.:-]{2,80}$'),
 account_label text check (account_label is null or account_label ~ '^[A-Za-z0-9 _.-]{1,40}$'),
 url text check (url is null or length(url) <= 2048),
 reason_code text check (reason_code is null or reason_code in ('task_needs_site','user_asked','link_from_email',
  'link_from_page','no_saved_login','login_rejected','tainted_conversation','unattended_turn','site_hold')),
 provenance text check (provenance is null or provenance in ('typed','bdr_record','search','email','page','unknown')),
 status text not null default 'requested' check (status in ('requested','approved','declined','saved','cancelled','expired','aborted')),
 message_id uuid,
 result jsonb,
 created_at timestamptz not null default now(),
 decided_at timestamptz,
 expires_at timestamptz not null default now() + interval '24 hours'
);
create index if not exists assistant_browser_requests_user_idx
 on public.assistant_browser_requests(user_id, created_at desc);
create index if not exists assistant_browser_requests_open_idx
 on public.assistant_browser_requests(job_id) where status = 'requested' and job_id is not null;

-- A1.6 Holds (6.5): final actions, site holds, code waits, live sign-in.
create table if not exists public.assistant_browser_holds (
 id uuid primary key,
 user_id uuid not null references public.profiles(id) on delete cascade,
 job_id uuid not null references public.browser_agent_jobs(id),
 help_id uuid,
 kind text not null check (kind in ('final_action','site','code','live_signin')),
 status text not null default 'open' check (status in ('open','approved','declined','expired','aborted','consumed')),
 lease_epoch bigint not null,
 method text not null check (method ~ '^[A-Z]{3,10}$'),
 origin text not null check (length(origin) between 8 and 300),
 url_sha256 text not null check (url_sha256 ~ '^[0-9a-f]{64}$'),
 body_sha256 text not null check (body_sha256 ~ '^[0-9a-f]{64}$'),
 out_of_scope boolean not null default false,
 label text check (label is null or length(label) <= 200),
 target_site_key text,
 summary_record_id uuid,
 screenshot_record_id uuid,
 decision text check (decision is null or decision in ('approve','approve_site_only','decline')),
 decided_by uuid,
 decided_at timestamptz,
 release_token text check (release_token is null or length(release_token) <= 4096),
 release_exp timestamptz,
 consumed_at timestamptz,
 expires_at timestamptz not null,
 created_at timestamptz not null default now()
);
create unique index if not exists assistant_browser_holds_open_uq
 on public.assistant_browser_holds(job_id) where status = 'open';
create index if not exists assistant_browser_holds_user_idx
 on public.assistant_browser_holds(user_id, created_at desc);

-- A1.7 Chat outbox (4.5). Written in the same transaction as the event.
create table if not exists public.assistant_browser_chat_outbox (
 id bigint generated always as identity primary key,
 job_id uuid references public.browser_agent_jobs(id),
 admission_id uuid,
 user_id uuid not null references public.profiles(id) on delete cascade,
 conversation_id uuid,
 event_key text not null check (length(event_key) between 1 and 120),
 payload jsonb not null default '{}'::jsonb,
 attempts int not null default 0,
 claimed_at timestamptz,
 drained_at timestamptz,
 created_at timestamptz not null default now()
);
create unique index if not exists assistant_browser_chat_outbox_event_uq
 on public.assistant_browser_chat_outbox(coalesce(job_id, admission_id), event_key);
create index if not exists assistant_browser_chat_outbox_pending_idx
 on public.assistant_browser_chat_outbox(created_at) where drained_at is null;

-- A1.8 Owner safe rules and IT Admin suggestions (6.3).
create table if not exists public.assistant_browser_user_safe_rules (
 id uuid primary key default gen_random_uuid(),
 user_id uuid not null references public.profiles(id) on delete cascade,
 site_key text not null references public.assistant_browser_sites(site_key),
 method text not null check (method ~ '^[A-Z]{3,10}$'),
 host text not null check (host ~ '^[a-z0-9.-]{1,253}(:[0-9]{1,5})?$'),
 path_pattern text not null check (length(path_pattern) between 1 and 512),
 graphql_operation text check (graphql_operation is null or graphql_operation ~ '^[A-Za-z0-9_]{1,100}$'),
 source_hold_id uuid,
 created_at timestamptz not null default now()
);
create unique index if not exists assistant_browser_user_safe_rules_uq on public.assistant_browser_user_safe_rules
 (user_id, site_key, method, host, path_pattern, coalesce(graphql_operation, ''));
create table if not exists public.assistant_browser_profile_suggestions (
 id uuid primary key default gen_random_uuid(),
 user_id uuid references public.profiles(id) on delete set null,
 site_key text not null references public.assistant_browser_sites(site_key),
 rule jsonb not null check (jsonb_typeof(rule) = 'object'),
 status text not null default 'new' check (status in ('new','promoted','dismissed')),
 reviewed_by uuid,
 reviewed_at timestamptz,
 created_at timestamptz not null default now()
);

-- A1.9 Downloads (11.2).
create table if not exists public.assistant_browser_downloads (
 id uuid primary key default gen_random_uuid(),
 user_id uuid not null references public.profiles(id) on delete cascade,
 job_id uuid not null references public.browser_agent_jobs(id),
 project_id uuid,
 file_id uuid,
 staged_id uuid,
 origin text not null check (length(origin) between 8 and 300),
 filename text check (filename is null or length(filename) <= 255),
 sha256 text not null check (sha256 ~ '^[0-9a-f]{64}$'),
 size_bytes bigint not null check (size_bytes >= 0),
 sniffed_type text not null check (length(sniffed_type) between 1 and 40),
 scan_verdict text not null check (scan_verdict in ('clean','rejected','unavailable')),
 status text not null check (status in ('stored','staged','rejected')),
 reason text check (reason is null or length(reason) <= 200),
 storage_path text check (storage_path is null or length(storage_path) <= 600),
 expires_at timestamptz,
 purged_at timestamptz,
 created_at timestamptz not null default now()
);
-- WI-15 (11.4): a download with no project waits as a staged object of the
-- owner (7 days). These three columns and the index are the staged kind
-- `browser_download`; assistant_files (PDF only, the splitter tray) is not used.
alter table public.assistant_browser_downloads
 add column if not exists reason text check (reason is null or length(reason) <= 200),
 add column if not exists storage_path text check (storage_path is null or length(storage_path) <= 600),
 add column if not exists expires_at timestamptz,
 add column if not exists purged_at timestamptz;
create unique index if not exists assistant_browser_downloads_job_sha_uq
 on public.assistant_browser_downloads(job_id, sha256);
create index if not exists assistant_browser_downloads_staged_idx
 on public.assistant_browser_downloads(expires_at) where status = 'staged' and purged_at is null;
create index if not exists assistant_browser_downloads_user_idx
 on public.assistant_browser_downloads(user_id, created_at desc);

-- A1.10 Live sign-in (wave E): metadata plus two unlogged shared tables.
create table if not exists public.assistant_browser_live_sessions (
 id uuid primary key default gen_random_uuid(),
 user_id uuid not null references public.profiles(id) on delete cascade,
 job_id uuid not null references public.browser_agent_jobs(id),
 site_key text not null,
 account_label text not null check (account_label ~ '^[A-Za-z0-9 _.-]{1,40}$'),
 status text not null default 'open' check (status in ('open','ended')),
 outcome text check (outcome is null or outcome in ('signed_in','stopped','idle','job_ended','failed')),
 started_at timestamptz not null default now(),
 last_input_at timestamptz,
 ended_at timestamptz
);
create unique index if not exists assistant_browser_live_sessions_open_uq
 on public.assistant_browser_live_sessions(job_id) where status = 'open';
create unlogged table if not exists public.assistant_browser_live_frames (
 session_id uuid primary key,
 frame bytea not null,
 seq bigint not null,
 created_at timestamptz not null default now()
);
create unlogged table if not exists public.assistant_browser_live_input (
 id bigint generated always as identity primary key,
 session_id uuid not null,
 seq bigint not null,
 event jsonb not null,
 created_at timestamptz not null default now()
);
create index if not exists assistant_browser_live_input_session_idx
 on public.assistant_browser_live_input(session_id, seq);

-- A1.11 Emailed codes already used (8.3), keyed by a hash of the Graph id.
create table if not exists public.assistant_browser_mail_codes_used (
 user_id uuid not null references public.profiles(id) on delete cascade,
 message_sha256 text not null check (message_sha256 ~ '^[0-9a-f]{64}$'),
 job_id uuid,
 used_at timestamptz not null default now(),
 primary key (user_id, message_sha256)
);

do $$ declare t text; begin
 foreach t in array array['assistant_browser_sites','assistant_browser_site_grants','assistant_browser_admissions',
  'assistant_browser_job_credentials','assistant_browser_requests','assistant_browser_holds','assistant_browser_chat_outbox',
  'assistant_browser_user_safe_rules','assistant_browser_profile_suggestions','assistant_browser_downloads',
  'assistant_browser_live_sessions','assistant_browser_live_frames','assistant_browser_live_input',
  'assistant_browser_mail_codes_used'] loop
  execute format('alter table public.%I enable row level security', t);
  execute format('alter table public.%I force row level security', t);
  execute format('revoke all on table public.%I from anon, authenticated', t);
  execute format('grant all on table public.%I to service_role', t);
 end loop;
end $$;

-- A1.12 Credential re-keying (7.2). Backfill existing Procore rows.
alter table public.assistant_credentials
 add column if not exists site_key text references public.assistant_browser_sites(site_key),
 add column if not exists account_label text not null default 'default',
 add column if not exists login_origins text[] not null default '{}',
 add column if not exists cookie_domains text[] not null default '{}',
 add column if not exists is_default boolean not null default false,
 add column if not exists sso_required boolean not null default false,
 add column if not exists attempts_day_count int not null default 0,
 add column if not exists attempts_day_started_at timestamptz;
do $$ begin
 if not exists(select 1 from pg_constraint where conname = 'assistant_credentials_account_label_chk') then
  alter table public.assistant_credentials add constraint assistant_credentials_account_label_chk
   check (account_label ~ '^[A-Za-z0-9 _.-]{1,40}$');
 end if;
 if not exists(select 1 from pg_constraint where conname = 'assistant_credentials_attempts_day_chk') then
  alter table public.assistant_credentials add constraint assistant_credentials_attempts_day_chk
   check (attempts_day_count >= 0);
 end if;
end $$;
alter table public.assistant_browser_sessions
 add column if not exists site_key text,
 add column if not exists account_label text,
 add column if not exists source text not null default 'password';
do $$ begin
 if not exists(select 1 from pg_constraint where conname = 'assistant_browser_sessions_source_chk') then
  alter table public.assistant_browser_sessions add constraint assistant_browser_sessions_source_chk
   check (source in ('password','live_signin'));
 end if;
end $$;

-- A1.13 Jobs, messages, help state.
alter table public.browser_agent_jobs
 add column if not exists assistant_admission_id uuid references public.assistant_browser_admissions(id),
 add column if not exists side_effects_at timestamptz,
 add column if not exists policy_version int not null default 1;
create unique index if not exists browser_agent_jobs_assistant_admission_uq
 on public.browser_agent_jobs(assistant_admission_id) where assistant_admission_id is not null;
alter table public.assistant_messages add column if not exists origin text;
do $$ begin
 if not exists(select 1 from pg_constraint where conname = 'assistant_messages_origin_chk') then
  alter table public.assistant_messages add constraint assistant_messages_origin_chk
   check (origin is null or origin in ('browser_result','user_carried'));
 end if;
end $$;
alter table public.assistant_browser_help_state
 add column if not exists reason_code text,
 add column if not exists credential_id uuid,
 add column if not exists hold_id uuid;
do $$ begin
 if not exists(select 1 from pg_constraint where conname = 'assistant_browser_help_state_reason_chk') then
  alter table public.assistant_browser_help_state add constraint assistant_browser_help_state_reason_chk
   check (reason_code is null or reason_code in ('login_failed','login_locked','captcha','mfa','sso','logged_out',
    'access_denied','not_found','ambiguous','unexpected_page','cap_reached','needs_info','provider_config','other',
    'needs_credential','needs_site','mfa_step_up','push','hold'));
 end if;
end $$;

-- A1.14 Settings (12.1, 12.2). The per-task cap of user jobs is 5 USD at most (D8).
alter table public.browser_agent_settings
 add column if not exists user_per_job_cost_cap_usd numeric(8,2) not null default 5,
 add column if not exists user_task_queue_hours int not null default 8,
 add column if not exists user_per_owner_concurrency smallint not null default 2,
 add column if not exists user_per_owner_queued smallint not null default 5,
 add column if not exists user_hold_timeout_seconds int not null default 600,
 add column if not exists user_park_max_minutes int not null default 15,
 add column if not exists user_mfa_mail_poll_seconds int not null default 300,
 add column if not exists user_task_reserve_step_usd numeric(8,2) not null default 1.00,
 add column if not exists user_offsite_query_bytes int not null default 4096,
 add column if not exists user_password_session_max_hours int not null default 24,
 add column if not exists user_live_session_max_days int not null default 14;
do $$ begin
 if not exists(select 1 from pg_constraint where conname = 'browser_agent_settings_user_v2_chk') then
  alter table public.browser_agent_settings add constraint browser_agent_settings_user_v2_chk check (
   user_per_job_cost_cap_usd > 0 and user_per_job_cost_cap_usd <= 5
   and user_task_queue_hours between 1 and 11
   and user_per_owner_concurrency between 1 and 8
   and user_per_owner_queued between 1 and 50
   and user_hold_timeout_seconds between 60 and 3600
   and user_park_max_minutes between 1 and 240
   and user_mfa_mail_poll_seconds between 30 and 1800
   and user_task_reserve_step_usd > 0 and user_task_reserve_step_usd <= user_per_job_cost_cap_usd
   and user_offsite_query_bytes between 0 and 65536
   and user_password_session_max_hours between 1 and 168
   and user_live_session_max_days between 1 and 30);
 end if;
end $$;

-- A1.15 Owner answer 5: 15 USD per user per day, for every user.
alter table public.assistant_profiles alter column daily_cost_cap_usd set default 15;
update public.assistant_profiles set daily_cost_cap_usd = 15, updated_at = now() where daily_cost_cap_usd = 5;

-- A1.16 Curated sites (verify each origin in the WI-20 live probes; a later
-- migration corrects the seed).
insert into public.assistant_browser_sites(site_key, label, kind, registrable_domains, login_origins, login_kind,
 cookie_domains, code_email_senders, prefer_api) values
 ('procore', 'Procore', 'curated', array['procore.com'], array['https://login.procore.com'], 'password_mfa',
  array['.procore.com'], array['procore.com'], 'For RFIs on BDR projects, prefer BDR''s Procore RFI sync.'),
 ('autodesk', 'Autodesk Construction Cloud', 'curated', array['autodesk.com'], array['https://accounts.autodesk.com'],
  'password_mfa', array['.autodesk.com'], array['autodesk.com'], null),
 ('buildingconnected', 'BuildingConnected', 'curated', array['buildingconnected.com','autodesk.com'],
  array['https://accounts.autodesk.com'], 'password_mfa', array['.buildingconnected.com','.autodesk.com'],
  array['autodesk.com'], 'Prefer BDR''s BuildingConnected integration (Autodesk APS) for bid invitations.'),
 ('smartbid', 'SmartBid', 'curated', array['smartbidnet.com'], array['https://www.smartbidnet.com'], 'password',
  array['.smartbidnet.com'], array['smartbidnet.com'], null)
on conflict (site_key) do nothing;
update public.assistant_credentials c set site_key = 'procore',
 account_label = coalesce(nullif(left(btrim(regexp_replace(c.label, '[^A-Za-z0-9 _.-]', '', 'g')), 40), ''), 'default'),
 login_origins = array['https://login.procore.com'], cookie_domains = array['.procore.com'], is_default = true
 where c.service = 'procore' and c.site_key is null;
update public.assistant_browser_sessions s set site_key = c.site_key, account_label = c.account_label
 from public.assistant_credentials c where c.id = s.credential_id and s.site_key is null;
create unique index if not exists assistant_credentials_site_account_uq
 on public.assistant_credentials(user_id, site_key, account_label) where site_key is not null;
create unique index if not exists assistant_credentials_site_default_uq
 on public.assistant_credentials(user_id, site_key) where is_default and status = 'active';
create unique index if not exists assistant_browser_sessions_site_account_uq
 on public.assistant_browser_sessions(user_id, site_key, account_label, origin);

-- A1.17 The v2 type row (3.4, 14.2 item 3). Model and prices from
-- app/services/llm_pricing.PRICES (gpt-5.4). Stays disabled until the release
-- steps (15.5) run the audited model probe.
insert into public.browser_agent_types(key, label, identity, prompt_version, principal_kind, enabled, max_concurrency,
 provider, model, price_in_per_mtok, price_out_per_mtok, max_steps, max_total_tokens, max_active_seconds)
 values('user_browser', 'Personal browser', null, 'user_browser.v1', 'user', false, 4,
 'openai', 'gpt-5.4', 2.5, 15, 150, 2000000, 1800)
 on conflict (key) do nothing;
update public.browser_agent_types set label = 'Retired personal browser read', enabled = false
 where key = 'user_browser_read' and label is distinct from 'Retired personal browser read';

-- A1.18 One expiry for job and reservation: queue hours plus the type's active
-- seconds stay within the 43200 second reservation TTL (0174).
create or replace function public.assistant_browser_v2_ttl_guard()
returns trigger language plpgsql security invoker set search_path = pg_catalog, public as $$
declare v_hours int; v_secs int;
begin
 if tg_table_name = 'browser_agent_settings' then
  v_hours := new.user_task_queue_hours;
  select max_active_seconds into v_secs from public.browser_agent_types where key = 'user_browser';
 else
  if new.key is distinct from 'user_browser' then return new; end if;
  v_secs := new.max_active_seconds;
  select user_task_queue_hours into v_hours from public.browser_agent_settings where id = 1;
 end if;
 if coalesce(v_hours, 0) * 3600 + coalesce(v_secs, 0) > 43200 then raise exception 'browser_v2_ttl_exceeded'; end if;
 return new;
end $$;
revoke all on function public.assistant_browser_v2_ttl_guard() from public;
revoke all on function public.assistant_browser_v2_ttl_guard() from anon, authenticated;
grant execute on function public.assistant_browser_v2_ttl_guard() to service_role;
do $$ begin
 if not exists(select 1 from pg_trigger where tgname = 'assistant_browser_v2_ttl_settings' and tgrelid = 'public.browser_agent_settings'::regclass) then
  create trigger assistant_browser_v2_ttl_settings before insert or update on public.browser_agent_settings
   for each row execute function public.assistant_browser_v2_ttl_guard();
 end if;
 if not exists(select 1 from pg_trigger where tgname = 'assistant_browser_v2_ttl_types' and tgrelid = 'public.browser_agent_types'::regclass) then
  create trigger assistant_browser_v2_ttl_types before insert or update on public.browser_agent_types
   for each row execute function public.assistant_browser_v2_ttl_guard();
 end if;
end $$;

-- A1.19 Pure helpers.
create or replace function public.assistant_browser_v2_host(p_url text)
returns text language sql immutable security invoker set search_path = pg_catalog, public as $$
 select lower(substring(p_url from '^https?://([A-Za-z0-9.-]+)(?::[0-9]{1,5})?(?:[/?#]|$)'))
$$;
create or replace function public.assistant_browser_v2_origin(p_url text)
returns text language sql immutable security invoker set search_path = pg_catalog, public as $$
 select lower(substring(p_url from '^(https?://[A-Za-z0-9.-]+(?::[0-9]{1,5})?)(?:[/?#]|$)'))
$$;
-- Identity providers are never credential origins (7.5).
create or replace function public.assistant_browser_v2_is_idp(p_url text)
returns boolean language sql immutable security invoker set search_path = pg_catalog, public as $$
 select coalesce(public.assistant_browser_v2_host(p_url) in ('login.microsoftonline.com','login.microsoft.com',
  'login.live.com','accounts.google.com','appleid.apple.com')
  or public.assistant_browser_v2_host(p_url) ~ '(^|\.)(okta|onelogin|pingone)\.com$', false)
$$;
create or replace function public.assistant_browser_v2_site_matches(p_site public.assistant_browser_sites, p_url text)
returns boolean language sql stable security invoker set search_path = pg_catalog, public as $$
 select coalesce(public.assistant_browser_v2_origin(p_url) = any(p_site.exact_origins)
  or exists(select 1 from unnest(p_site.registrable_domains) d
   where public.assistant_browser_v2_host(p_url) = d or public.assistant_browser_v2_host(p_url) like '%.' || d), false)
$$;

-- A1.20 What an admission still needs (5.3 steps 3 to 5), read live.
create or replace function public.assistant_browser_admission_needs(p_admission_id uuid)
returns text[] language plpgsql stable security invoker set search_path = pg_catalog, public as $$
declare a public.assistant_browser_admissions; v_needs text[] := '{}'; v_key text; s public.assistant_browser_sites;
 v_label text; v_sso boolean;
begin
 select * into a from public.assistant_browser_admissions where id = p_admission_id;
 if a.id is null then return array['missing']; end if;
 for v_key in select jsonb_array_elements_text(coalesce(jsonb_path_query_array(a.site_scope, '$[*].site_key'), '[]'::jsonb))
  union select a.start_site_key loop
  select * into s from public.assistant_browser_sites where site_key = v_key;
  if s.site_key is null or not exists(select 1 from public.assistant_browser_site_grants g
   where g.user_id = a.user_id and g.site_key = v_key and g.status = 'active') then
   v_needs := v_needs || 'site_access'::text; continue;
  end if;
  if not s.enabled then v_needs := v_needs || 'site_disabled'::text; continue; end if;
  if s.login_kind = 'none' then continue; end if;
  v_label := case when v_key = a.start_site_key then a.account_label end;
  if exists(select 1 from public.assistant_credentials c where c.user_id = a.user_id and c.site_key = v_key
    and c.status = 'active' and not c.sso_required and (c.account_label = v_label or (v_label is null and c.is_default)))
   or exists(select 1 from public.assistant_browser_sessions x where x.user_id = a.user_id and x.site_key = v_key
    and x.expires_at > now() and (x.account_label = v_label or (v_label is null and x.account_label = 'default'))) then
   continue;
  end if;
  select bool_or(c.sso_required) into v_sso from public.assistant_credentials c
   where c.user_id = a.user_id and c.site_key = v_key and (c.account_label = v_label or v_label is null);
  v_needs := v_needs || (case when coalesce(v_sso, false) or s.login_kind = 'sso_only' then 'live_signin' else 'credential' end)::text;
 end loop;
 if a.start_provenance = 'email' and a.link_approved_at is null then v_needs := v_needs || 'link_open'::text; end if;
 if (a.tainted or not a.fresh_user_message) and a.start_approved_at is null then v_needs := v_needs || 'task_start'::text; end if;
 return (select coalesce(array_agg(distinct n), '{}') from unnest(v_needs) n);
end $$;
create or replace function public.assistant_browser_admission_status(p_needs text[])
returns text language sql immutable security invoker set search_path = pg_catalog, public as $$
 select case when 'site_access' = any(p_needs) or 'site_disabled' = any(p_needs) then 'waiting_grant'
  when 'credential' = any(p_needs) or 'live_signin' = any(p_needs) then 'waiting_credential'
  when 'link_open' = any(p_needs) then 'waiting_link'
  when 'task_start' = any(p_needs) then 'waiting_start' else 'ready' end
$$;

revoke all on function public.assistant_browser_v2_host(text) from public;
revoke all on function public.assistant_browser_v2_host(text) from anon, authenticated;
grant execute on function public.assistant_browser_v2_host(text) to service_role;
revoke all on function public.assistant_browser_v2_origin(text) from public;
revoke all on function public.assistant_browser_v2_origin(text) from anon, authenticated;
grant execute on function public.assistant_browser_v2_origin(text) to service_role;
revoke all on function public.assistant_browser_v2_is_idp(text) from public;
revoke all on function public.assistant_browser_v2_is_idp(text) from anon, authenticated;
grant execute on function public.assistant_browser_v2_is_idp(text) to service_role;
revoke all on function public.assistant_browser_v2_site_matches(public.assistant_browser_sites, text) from public;
revoke all on function public.assistant_browser_v2_site_matches(public.assistant_browser_sites, text) from anon, authenticated;
grant execute on function public.assistant_browser_v2_site_matches(public.assistant_browser_sites, text) to service_role;
revoke all on function public.assistant_browser_admission_needs(uuid) from public;
revoke all on function public.assistant_browser_admission_needs(uuid) from anon, authenticated;
grant execute on function public.assistant_browser_admission_needs(uuid) to service_role;
revoke all on function public.assistant_browser_admission_status(text[]) from public;
revoke all on function public.assistant_browser_admission_status(text[]) from anon, authenticated;
grant execute on function public.assistant_browser_admission_status(text[]) to service_role;

notify pgrst, 'reload schema';

-- ==== PART A2 ====

-- A2.3 Grants. Only the human route calls this; an unknown site becomes an
-- ad hoc registry row (5.1). Returns the admissions waiting on this site.
create or replace function public.assistant_browser_site_grant(p_user_id uuid, p_site_key text, p_scope text, p_value text,
 p_path_prefix text, p_granted_via text, p_provenance text, p_request_id uuid default null, p_ad_hoc jsonb default null)
returns jsonb language plpgsql security invoker set search_path = pg_catalog, public as $$
declare s public.assistant_browser_sites; g public.assistant_browser_site_grants; v_waiting jsonb;
begin
 if not public.assistant_browser_owner_eligible(p_user_id, false) then return '{"ok":false,"code":"ineligible"}'::jsonb; end if;
 if p_site_key is null or p_site_key !~ '^[a-z0-9_.:-]{2,80}$' or p_scope not in ('domain','origin')
  or length(coalesce(p_value, '')) not between 1 and 300 or p_granted_via not in ('chat_card','settings')
  or p_provenance not in ('typed','bdr_record','search','email','page','unknown','settings') then
  return '{"ok":false,"code":"invalid"}'::jsonb;
 end if;
 perform pg_advisory_xact_lock(hashtextextended('assistant-browser-owner:' || p_user_id::text, 0));
 select * into s from public.assistant_browser_sites where site_key = p_site_key for share;
 if s.site_key is null then
  if p_ad_hoc is null or jsonb_typeof(p_ad_hoc) <> 'object' or p_site_key !~ '^(domain|origin):'
   or length(coalesce(p_ad_hoc->>'label', '')) not between 1 and 100 then return '{"ok":false,"code":"unknown_site"}'::jsonb; end if;
  insert into public.assistant_browser_sites(site_key, label, kind, registrable_domains, exact_origins, path_prefix,
   login_kind, code_email_senders, created_by)
  values(p_site_key, p_ad_hoc->>'label', 'ad_hoc',
   case when p_scope = 'domain' then array[lower(p_value)] else '{}' end,
   case when p_scope = 'origin' then array[lower(p_value)] else '{}' end,
   case when p_scope = 'origin' then p_path_prefix end, 'password',
   case when p_scope = 'domain' then array[lower(p_value)] else '{}' end, p_user_id)
  on conflict (site_key) do nothing;
  select * into s from public.assistant_browser_sites where site_key = p_site_key for share;
 end if;
 if not s.enabled then return '{"ok":false,"code":"site_disabled"}'::jsonb; end if;
 select * into g from public.assistant_browser_site_grants where user_id = p_user_id and site_key = p_site_key and status = 'active' for update;
 if g.id is null then
  insert into public.assistant_browser_site_grants(user_id, site_key, scope, value, path_prefix, granted_via, provenance, request_id)
  values(p_user_id, p_site_key, p_scope, lower(p_value), p_path_prefix, p_granted_via, p_provenance, p_request_id) returning * into g;
 end if;
 if p_request_id is not null then
  update public.assistant_browser_requests set status = 'approved', decided_at = now(),
   result = jsonb_build_object('grant_id', g.id, 'grant_version', g.version)
   where id = p_request_id and user_id = p_user_id and kind = 'site_access' and status = 'requested';
 end if;
 select coalesce(jsonb_agg(a.id), '[]'::jsonb) into v_waiting from public.assistant_browser_admissions a
  where a.user_id = p_user_id and a.status like 'waiting_%' and a.expires_at > now()
   and (a.start_site_key = p_site_key or a.site_scope @> jsonb_build_array(jsonb_build_object('site_key', p_site_key)));
 return jsonb_build_object('ok', true, 'grant', to_jsonb(g), 'waiting_admissions', v_waiting);
end $$;

-- Adds an approved site to a running job's scope (5.4 step 2) and bumps the
-- policy version, so the worker refetches its policy.
create or replace function public.assistant_browser_scope_extend(p_user_id uuid, p_job_id uuid, p_site_key text)
returns jsonb language plpgsql security invoker set search_path = pg_catalog, public as $$
declare j public.browser_agent_jobs; g public.assistant_browser_site_grants; a public.assistant_browser_admissions;
begin
 perform pg_advisory_xact_lock(hashtextextended('assistant-browser-owner:' || p_user_id::text, 0));
 select * into j from public.browser_agent_jobs where id = p_job_id and principal_user_id = p_user_id
  and agent_type = 'user_browser' for update;
 if j.id is null or j.status not in ('queued','running','waiting_help') then return '{"ok":false,"code":"job_changed"}'::jsonb; end if;
 select * into g from public.assistant_browser_site_grants where user_id = p_user_id and site_key = p_site_key and status = 'active';
 if g.id is null then return '{"ok":false,"code":"no_grant"}'::jsonb; end if;
 select * into a from public.assistant_browser_admissions where id = j.assistant_admission_id for update;
 if not (a.site_scope @> jsonb_build_array(jsonb_build_object('site_key', p_site_key))) then
  update public.assistant_browser_admissions set site_scope = site_scope || jsonb_build_array(jsonb_build_object(
   'site_key', g.site_key, 'grant_id', g.id, 'grant_version', g.version)), updated_at = now() where id = a.id;
  update public.browser_agent_jobs set policy_version = policy_version + 1 where id = j.id returning * into j;
 end if;
 return jsonb_build_object('ok', true, 'policy_version', j.policy_version);
end $$;

-- A2.4 Admission (5.3 step 7). Model and cap come from the type row and the
-- settings, never from the caller.
create or replace function public.assistant_browser_admission_create(p_user_id uuid, p_record jsonb)
returns jsonb language plpgsql security invoker set search_path = pg_catalog, public as $$
declare a public.assistant_browser_admissions; v_role text; v_conv record; cfg public.browser_agent_settings;
 typ public.browser_agent_types; v_needs text[]; v_key text; v_scope jsonb := '[]'::jsonb; g public.assistant_browser_site_grants;
begin
 if not public.assistant_browser_owner_eligible(p_user_id, true) then return '{"ok":false,"code":"ineligible"}'::jsonb; end if;
 if jsonb_typeof(p_record) is distinct from 'object' or length(coalesce(p_record->>'goal', '')) not between 1 and 2000
  or coalesce(p_record->>'start_site_key', '') !~ '^[a-z0-9_.:-]{2,80}$'
  or coalesce(p_record->>'start_provenance', 'unknown') not in ('typed','bdr_record','search','email','page','unknown','none')
  or (p_record ? 'account_label' and p_record->>'account_label' !~ '^[A-Za-z0-9 _.-]{1,40}$')
  or length(coalesce(p_record->>'start_url', '')) > 2048 then return '{"ok":false,"code":"invalid"}'::jsonb; end if;
 perform pg_advisory_xact_lock(hashtextextended('assistant-browser-owner:' || p_user_id::text, 0));
 select id, mode, tainted_at, mailbox_tainted_at into v_conv from public.assistant_conversations
  where id = (p_record->>'conversation_id')::uuid and user_id = p_user_id;
 if v_conv.id is null or v_conv.mode <> 'normal' or v_conv.mailbox_tainted_at is not null then
  return '{"ok":false,"code":"conversation_changed"}'::jsonb;
 end if;
 select * into typ from public.browser_agent_types where key = 'user_browser' and principal_kind = 'user' and enabled;
 select * into cfg from public.browser_agent_settings where id = 1 and enabled;
 if typ.key is null or cfg.id is null then return '{"ok":false,"code":"type_unavailable"}'::jsonb; end if;
 if exists(select 1 from public.assistant_browser_sites where site_key = p_record->>'start_site_key' and not enabled) then
  return '{"ok":false,"code":"site_disabled"}'::jsonb;
 end if;
 select role::text into v_role from public.profiles where id = p_user_id;
 for v_key in select distinct x from jsonb_array_elements_text(coalesce(p_record->'site_keys', '[]'::jsonb)
  || jsonb_build_array(p_record->>'start_site_key')) x loop
  if v_key !~ '^[a-z0-9_.:-]{2,80}$' then return '{"ok":false,"code":"invalid"}'::jsonb; end if;
  select * into g from public.assistant_browser_site_grants where user_id = p_user_id and site_key = v_key and status = 'active';
  v_scope := v_scope || jsonb_build_array(jsonb_build_object('site_key', v_key, 'grant_id', g.id, 'grant_version', g.version));
 end loop;
 insert into public.assistant_browser_admissions(user_id, role, conversation_id, run_id, goal, start_url, start_provenance,
  start_site_key, site_scope, account_label, project_id, model, max_cost_usd, tainted, fresh_user_message, expires_at)
 values(p_user_id, v_role, v_conv.id, (p_record->>'run_id')::uuid, p_record->>'goal', nullif(p_record->>'start_url', ''),
  coalesce(p_record->>'start_provenance', 'unknown'), p_record->>'start_site_key', v_scope, p_record->>'account_label',
  (p_record->>'project_id')::uuid, typ.model, cfg.user_per_job_cost_cap_usd,
  coalesce((p_record->>'tainted')::boolean, false) or v_conv.tainted_at is not null,
  coalesce((p_record->>'fresh_user_message')::boolean, false), now() + interval '24 hours')
 returning * into a;
 v_needs := public.assistant_browser_admission_needs(a.id);
 update public.assistant_browser_admissions set needs = v_needs, status = public.assistant_browser_admission_status(v_needs)
  where id = a.id returning * into a;
 return jsonb_build_object('ok', true, 'admission', to_jsonb(a), 'needs', to_jsonb(v_needs));
exception when invalid_text_representation then return '{"ok":false,"code":"invalid"}'::jsonb;
end $$;

-- The start card (4.3) and the email link card (5.5): a human click only.
create or replace function public.assistant_browser_start_decide(p_user_id uuid, p_admission_id uuid, p_decision text)
returns jsonb language plpgsql security invoker set search_path = pg_catalog, public as $$
declare a public.assistant_browser_admissions; v_needs text[];
begin
 if p_decision not in ('start','decline') then return '{"ok":false,"code":"invalid"}'::jsonb; end if;
 perform pg_advisory_xact_lock(hashtextextended('assistant-browser-owner:' || p_user_id::text, 0));
 select * into a from public.assistant_browser_admissions where id = p_admission_id and user_id = p_user_id for update;
 if a.id is null or a.status not like 'waiting_%' or a.expires_at <= now() then return '{"ok":false,"code":"admission_changed"}'::jsonb; end if;
 if p_decision = 'decline' then
  update public.assistant_browser_admissions set status = 'declined', updated_at = now() where id = a.id returning * into a;
  update public.assistant_browser_requests set status = 'declined', decided_at = now() where admission_id = a.id and status = 'requested';
  return jsonb_build_object('ok', true, 'admission', to_jsonb(a), 'needs', '[]'::jsonb);
 end if;
 update public.assistant_browser_admissions set start_approved_at = coalesce(start_approved_at, now()) where id = a.id;
 update public.assistant_browser_requests set status = 'approved', decided_at = now()
  where admission_id = a.id and kind = 'task_start' and status = 'requested';
 v_needs := public.assistant_browser_admission_needs(a.id);
 update public.assistant_browser_admissions set needs = v_needs, status = public.assistant_browser_admission_status(v_needs),
  updated_at = now() where id = a.id returning * into a;
 return jsonb_build_object('ok', true, 'admission', to_jsonb(a), 'needs', to_jsonb(v_needs));
end $$;

create or replace function public.assistant_browser_request_create(p_user_id uuid, p_record jsonb)
returns jsonb language plpgsql security invoker set search_path = pg_catalog, public as $$
declare r public.assistant_browser_requests;
begin
 if not public.assistant_browser_owner_eligible(p_user_id, true) then return '{"ok":false,"code":"ineligible"}'::jsonb; end if;
 if jsonb_typeof(p_record) is distinct from 'object' then return '{"ok":false,"code":"invalid"}'::jsonb; end if;
 if p_record ? 'admission_id' and not exists(select 1 from public.assistant_browser_admissions
  where id = (p_record->>'admission_id')::uuid and user_id = p_user_id) then return '{"ok":false,"code":"invalid"}'::jsonb; end if;
 if p_record ? 'job_id' and not exists(select 1 from public.browser_agent_jobs
  where id = (p_record->>'job_id')::uuid and principal_user_id = p_user_id) then return '{"ok":false,"code":"invalid"}'::jsonb; end if;
 insert into public.assistant_browser_requests(user_id, conversation_id, admission_id, job_id, kind, site_key, account_label,
  url, reason_code, provenance)
 values(p_user_id, (p_record->>'conversation_id')::uuid, (p_record->>'admission_id')::uuid, (p_record->>'job_id')::uuid,
  p_record->>'kind', p_record->>'site_key', p_record->>'account_label', p_record->>'url', p_record->>'reason_code',
  p_record->>'provenance') returning * into r;
 return jsonb_build_object('ok', true, 'request', to_jsonb(r));
exception when invalid_text_representation or check_violation or not_null_violation then
 return '{"ok":false,"code":"invalid"}'::jsonb;
end $$;

-- Decline or cancel any card; approve a link card (site access approves
-- through assistant_browser_site_grant, credentials through put_v2).
create or replace function public.assistant_browser_request_decide(p_user_id uuid, p_request_id uuid, p_decision text)
returns jsonb language plpgsql security invoker set search_path = pg_catalog, public as $$
declare r public.assistant_browser_requests; v_needs text[];
begin
 if p_decision not in ('approve','decline','cancel') then return '{"ok":false,"code":"invalid"}'::jsonb; end if;
 perform pg_advisory_xact_lock(hashtextextended('assistant-browser-owner:' || p_user_id::text, 0));
 select * into r from public.assistant_browser_requests where id = p_request_id and user_id = p_user_id for update;
 if r.id is null or r.status <> 'requested' or r.expires_at <= now() then return '{"ok":false,"code":"request_changed"}'::jsonb; end if;
 if p_decision = 'approve' and r.kind <> 'link_open' then return '{"ok":false,"code":"invalid"}'::jsonb; end if;
 update public.assistant_browser_requests set status = case p_decision when 'approve' then 'approved' when 'decline' then 'declined'
  else 'cancelled' end, decided_at = now() where id = r.id returning * into r;
 if r.admission_id is not null then
  if p_decision = 'approve' then
   update public.assistant_browser_admissions set link_approved_at = coalesce(link_approved_at, now()) where id = r.admission_id;
   v_needs := public.assistant_browser_admission_needs(r.admission_id);
   update public.assistant_browser_admissions set needs = v_needs, status = public.assistant_browser_admission_status(v_needs),
    updated_at = now() where id = r.admission_id and status like 'waiting_%';
  else
   update public.assistant_browser_admissions set status = 'declined', updated_at = now()
    where id = r.admission_id and status like 'waiting_%';
  end if;
 end if;
 return jsonb_build_object('ok', true, 'request', to_jsonb(r), 'needs', to_jsonb(coalesce(v_needs, '{}')));
end $$;

-- A2.5 Enqueue (5.3 step 8, 12.1, 12.2). First reserve step only; one expiry
-- for the reservation and the job; per-owner queued limit instead of one job.
create or replace function public.assistant_browser_task_enqueue_v2(p_user_id uuid, p_admission_id uuid, p_input jsonb,
 p_user_cap_default numeric default 15, p_global_cap_default numeric default 50)
returns jsonb language plpgsql security invoker set search_path = pg_catalog, public as $$
declare a public.assistant_browser_admissions; j public.browser_agent_jobs; typ public.browser_agent_types;
 cfg public.browser_agent_settings; v_needs text[]; v_hold jsonb; v_ttl int; v_step numeric; v_exp timestamptz;
 v_key text; c public.assistant_credentials; v_label text; v_input jsonb; v_role text;
begin
 if not public.assistant_browser_runtime_ready() then return '{"ok":false,"code":"runtime_unavailable"}'::jsonb; end if;
 if not public.assistant_browser_owner_eligible(p_user_id, true) then return '{"ok":false,"code":"ineligible"}'::jsonb; end if;
 if exists(select 1 from public.assistant_settings where id = 1 and 'browser_task_start' = any(disabled_tools)) then
  return '{"ok":false,"code":"ineligible"}'::jsonb;
 end if;
 perform pg_advisory_xact_lock(hashtextextended('assistant-browser-owner:' || p_user_id::text, 0));
 select * into a from public.assistant_browser_admissions where id = p_admission_id and user_id = p_user_id for update;
 if a.id is null then return '{"ok":false,"code":"admission_changed"}'::jsonb; end if;
 select * into j from public.browser_agent_jobs where assistant_admission_id = a.id;
 if j.id is not null then return jsonb_build_object('ok', true, 'created', false, 'job', to_jsonb(j), 'reservation_id', j.assistant_reservation_id); end if;
 if a.status in ('enqueued','declined','expired') or a.expires_at <= now() then return '{"ok":false,"code":"admission_changed"}'::jsonb; end if;
 perform 1 from public.assistant_conversations where id = a.conversation_id and user_id = p_user_id
  and mode = 'normal' and mailbox_tainted_at is null for share;
 if not found then return '{"ok":false,"code":"conversation_changed"}'::jsonb; end if;
 select role::text into v_role from public.profiles where id = p_user_id;
 if v_role is distinct from a.role then return '{"ok":false,"code":"authority_changed"}'::jsonb; end if;
 select * into typ from public.browser_agent_types where key = 'user_browser' and principal_kind = 'user' and enabled for share;
 if typ.key is null or typ.provider is distinct from 'openai' or typ.model_probe_ok_at is null
  or coalesce(typ.price_in_per_mtok, 0) <= 0 or coalesce(typ.price_out_per_mtok, 0) <= 0 then
  return '{"ok":false,"code":"type_unavailable"}'::jsonb;
 end if;
 if typ.model is distinct from a.model then return '{"ok":false,"code":"authority_changed"}'::jsonb; end if;
 select * into cfg from public.browser_agent_settings where id = 1 and enabled for share;
 if cfg.id is null then return '{"ok":false,"code":"type_unavailable"}'::jsonb; end if;
 v_needs := public.assistant_browser_admission_needs(a.id);
 if cardinality(v_needs) > 0 then
  update public.assistant_browser_admissions set needs = v_needs, status = public.assistant_browser_admission_status(v_needs),
   updated_at = now() where id = a.id;
  return jsonb_build_object('ok', false, 'code', 'needs', 'needs', to_jsonb(v_needs));
 end if;
 if a.project_id is not null and not exists(select 1 from public.projects where id = a.project_id) then
  return '{"ok":false,"code":"project_invalid"}'::jsonb;
 end if;
 if (select count(*) from public.browser_agent_jobs where principal_user_id = p_user_id and agent_type = 'user_browser'
  and status in ('queued','running','waiting_help','applying')) >= cfg.user_per_owner_queued then
  return '{"ok":false,"code":"concurrency_cap"}'::jsonb;
 end if;
 if a.max_cost_usd > cfg.user_per_job_cost_cap_usd then return '{"ok":false,"code":"invalid_cost"}'::jsonb; end if;
 -- Pin the grant versions that hold now (a grant made after admission).
 update public.assistant_browser_admissions set site_scope = (select coalesce(jsonb_agg(jsonb_build_object('site_key', e->>'site_key',
  'grant_id', g.id, 'grant_version', g.version)), '[]'::jsonb) from jsonb_array_elements(a.site_scope) e
  join public.assistant_browser_site_grants g on g.user_id = p_user_id and g.site_key = e->>'site_key' and g.status = 'active')
  where id = a.id returning * into a;
 v_step := least(cfg.user_task_reserve_step_usd, a.max_cost_usd);
 v_ttl := cfg.user_task_queue_hours * 3600 + typ.max_active_seconds;
 v_hold := public.assistant_spend_reserve(p_user_id, 'browser_task', v_step, v_ttl, p_user_cap_default, p_global_cap_default,
  a.run_id, a.id, null);
 if not coalesce((v_hold->>'ok')::boolean, false) then return jsonb_build_object('ok', false, 'code', v_hold->>'reason'); end if;
 select expires_at into v_exp from public.assistant_spend_reservations where id = (v_hold->>'id')::uuid;
 v_input := coalesce(p_input, '{}'::jsonb) || jsonb_build_object('admission_id', a.id, 'goal', a.goal,
  'start_url', a.start_url, 'start_site_key', a.start_site_key, 'project_id', a.project_id,
  'authority', jsonb_build_object('role', a.role, 'model', a.model, 'conversation_id', a.conversation_id,
   'max_cost_usd', a.max_cost_usd, 'admission_id', a.id));
 insert into public.browser_agent_jobs(agent_type, dedupe_key, platform, input, created_by, expires_at, project_id,
  principal_kind, principal_user_id, assistant_admission_id, assistant_conversation_id, assistant_reservation_id)
 values('user_browser', 'assistant:' || p_user_id::text || ':admission:' || a.id::text, a.start_site_key, v_input,
  p_user_id, v_exp, a.project_id, 'user', p_user_id, a.id, a.conversation_id, (v_hold->>'id')::uuid) returning * into j;
 for v_key in select jsonb_array_elements_text(jsonb_path_query_array(a.site_scope, '$[*].site_key')) loop
  if exists(select 1 from public.assistant_browser_sites where site_key = v_key and login_kind = 'none') then continue; end if;
  v_label := case when v_key = a.start_site_key then a.account_label end;
  select * into c from public.assistant_credentials where user_id = p_user_id and site_key = v_key and status = 'active'
   and not sso_required and (account_label = v_label or (v_label is null and is_default)) for share;
  if c.id is not null then
   insert into public.assistant_browser_job_credentials(admission_id, job_id, user_id, site_key, account_label, credential_id, credential_version)
   values(a.id, j.id, p_user_id, v_key, c.account_label, c.id, c.version);
  else
   insert into public.assistant_browser_job_credentials(admission_id, job_id, user_id, site_key, account_label)
   values(a.id, j.id, p_user_id, v_key, coalesce(v_label, 'default'));
  end if;
 end loop;
 update public.assistant_browser_admissions set status = 'enqueued', job_id = j.id, needs = '{}', updated_at = now() where id = a.id;
 return jsonb_build_object('ok', true, 'created', true, 'job', to_jsonb(j), 'reservation_id', j.assistant_reservation_id);
end $$;

-- A2.6 Credentials v2 (7.2, 7.3). Curated sites take origins and cookie
-- domains from the registry, ad hoc sites from the one confirmed origin.
-- A save moves waiting jobs on this account forward instead of cancelling
-- them, and never resets the rolling daily attempt counter.
create or replace function public.assistant_browser_credential_put_v2(p_user_id uuid, p_site_key text, p_account_label text,
 p_expected_version int, p_record jsonb, p_request_id uuid default null)
returns jsonb language plpgsql security invoker set search_path = pg_catalog, public as $$
declare c public.assistant_credentials; s public.assistant_browser_sites; v_id uuid; v_login text[]; v_cookie text[];
 v_origin text; v_default boolean; v_woken jsonb := '[]'::jsonb; r public.assistant_browser_requests; jc record;
begin
 if not public.assistant_browser_owner_eligible(p_user_id, false) then return '{"ok":false,"code":"ineligible"}'::jsonb; end if;
 if p_expected_version is null or p_expected_version < 0 or jsonb_typeof(p_record) is distinct from 'object'
  or coalesce(p_account_label, '') !~ '^[A-Za-z0-9 _.-]{1,40}$'
  or length(coalesce(p_record->>'username', '')) not between 1 and 320
  or length(coalesce(p_record->>'wrapped_dek', '')) not between 1 and 4096
  or length(coalesce(p_record->>'ciphertext', '')) not between 1 and 32768
  or length(coalesce(p_record->>'nonce', '')) not between 1 and 128
  or length(coalesce(p_record->>'key_id', '')) not between 1 and 80
  or length(coalesce(p_record->>'fingerprint', '')) not between 1 and 128 then return '{"ok":false,"code":"invalid"}'::jsonb; end if;
 perform pg_advisory_xact_lock(hashtextextended('assistant-browser-owner:' || p_user_id::text, 0));
 select * into s from public.assistant_browser_sites where site_key = p_site_key and enabled for share;
 if s.site_key is null then return '{"ok":false,"code":"unknown_site"}'::jsonb; end if;
 if s.kind = 'curated' then
  v_login := s.login_origins; v_cookie := s.cookie_domains;
 else
  v_origin := lower(p_record->>'confirmed_login_origin');
  if v_origin is null or v_origin !~ '^https://[a-z0-9.-]+(:[0-9]{1,5})?$'
   or not public.assistant_browser_v2_site_matches(s, v_origin) then return '{"ok":false,"code":"origin_invalid"}'::jsonb; end if;
  v_login := array[v_origin]; v_cookie := array[public.assistant_browser_v2_host(v_origin)];
 end if;
 if cardinality(v_login) = 0 or exists(select 1 from unnest(v_login) o where public.assistant_browser_v2_is_idp(o) or o !~ '^https://') then
  return '{"ok":false,"code":"identity_provider_refused"}'::jsonb;
 end if;
 select * into c from public.assistant_credentials where user_id = p_user_id and site_key = p_site_key
  and account_label = p_account_label for update;
 if (c.id is null and p_expected_version <> 0) or (c.id is not null and c.version <> p_expected_version) then
  return '{"ok":false,"code":"version_conflict"}'::jsonb;
 end if;
 v_id := coalesce(c.id, (p_record->>'id')::uuid);
 if v_id is null or (c.id is not null and (p_record->>'id')::uuid is distinct from c.id) then return '{"ok":false,"code":"invalid"}'::jsonb; end if;
 v_default := coalesce(c.is_default and c.status = 'active', false) or not exists(select 1 from public.assistant_credentials
  where user_id = p_user_id and site_key = p_site_key and status = 'active' and is_default and id <> v_id);
 if c.id is null then
  insert into public.assistant_credentials(id, user_id, service, label, origins, username, wrapped_dek, ciphertext, nonce, key_id,
   version, fingerprint, site_key, account_label, login_origins, cookie_domains, is_default)
  values(v_id, p_user_id, null, p_account_label, v_login, p_record->>'username', p_record->>'wrapped_dek', p_record->>'ciphertext',
   p_record->>'nonce', p_record->>'key_id', 1, p_record->>'fingerprint', p_site_key, p_account_label, v_login, v_cookie, v_default)
  returning * into c;
 else
  update public.assistant_credentials set label = p_account_label, origins = v_login, username = p_record->>'username',
   wrapped_dek = p_record->>'wrapped_dek', ciphertext = p_record->>'ciphertext', nonce = p_record->>'nonce',
   key_id = p_record->>'key_id', version = version + 1, fingerprint = p_record->>'fingerprint', status = 'active',
   login_origins = v_login, cookie_domains = v_cookie, is_default = v_default, sso_required = false,
   login_failures = 0, locked_until = null, login_attempt_id = null, login_job_id = null, login_epoch = null,
   login_attempt_expires_at = null, updated_at = now()
  where id = c.id returning * into c;
 end if;
 for jc in select x.id, x.job_id from public.assistant_browser_job_credentials x join public.browser_agent_jobs j on j.id = x.job_id
  where x.user_id = p_user_id and x.site_key = p_site_key and x.account_label = p_account_label
   and j.status = 'waiting_help' and exists(select 1 from public.assistant_browser_help_state h where h.job_id = j.id
    and h.status = 'open' and h.reason_code in ('needs_credential','login_failed')) for update of x loop
  update public.assistant_browser_job_credentials set credential_id = c.id, credential_version = c.version, updated_at = now() where id = jc.id;
  update public.assistant_browser_help_state set status = 'resolved' where job_id = jc.job_id and status = 'open'
   and reason_code in ('needs_credential','login_failed');
  update public.browser_agent_jobs set status = case when lease_owner is null then 'queued' else 'running' end,
   next_attempt_at = null, policy_version = policy_version + 1 where id = jc.job_id;
  v_woken := v_woken || to_jsonb(jc.job_id);
 end loop;
 -- Queued jobs on this account take the new version before any claim; an
 -- active job still pinned to the old version ends now with a chat message
 -- (plan 14.4) instead of failing its next fence.
 for jc in select x.id, x.job_id from public.assistant_browser_job_credentials x join public.browser_agent_jobs j on j.id = x.job_id
  where x.user_id = p_user_id and x.site_key = p_site_key and x.account_label = p_account_label and j.status = 'queued'
   and x.credential_version is distinct from c.version for update of x loop
  update public.assistant_browser_job_credentials set credential_id = c.id, credential_version = c.version, updated_at = now() where id = jc.id;
  update public.browser_agent_jobs set policy_version = policy_version + 1 where id = jc.job_id;
 end loop;
 update public.browser_agent_jobs j set status = 'canceled', error_kind = 'credential_changed', error = 'Browser authorization changed.',
  cancel_requested_at = now(), finished_at = now(), lease_owner = null, lease_boot_id = null, lease_expires_at = null, hold_until = null
  from public.assistant_browser_job_credentials x where x.job_id = j.id and x.credential_id = c.id
   and x.credential_version is distinct from c.version and j.status in ('running','waiting_help','applying');
 if p_request_id is not null then
  update public.assistant_browser_requests set status = 'saved', decided_at = now(),
   result = jsonb_build_object('saved', true, 'site_key', p_site_key, 'account_label', p_account_label, 'version', c.version)
   where id = p_request_id and user_id = p_user_id and kind = 'credential' and status = 'requested'
    and site_key = p_site_key and coalesce(account_label, p_account_label) = p_account_label returning * into r;
  -- A site added mid-task gets its child row when the user saves an account for it.
  if r.job_id is not null and not exists(select 1 from public.assistant_browser_job_credentials where job_id = r.job_id and site_key = p_site_key) then
   insert into public.assistant_browser_job_credentials(admission_id, job_id, user_id, site_key, account_label, credential_id, credential_version)
   select j.assistant_admission_id, j.id, p_user_id, p_site_key, p_account_label, c.id, c.version
    from public.browser_agent_jobs j where j.id = r.job_id and j.assistant_admission_id is not null;
   update public.browser_agent_jobs set policy_version = policy_version + 1 where id = r.job_id;
  end if;
 end if;
 return jsonb_build_object('ok', true, 'credential', to_jsonb(c) - array['wrapped_dek','ciphertext','nonce'],
  'woken_jobs', v_woken, 'waiting_admissions', (select coalesce(jsonb_agg(a.id), '[]'::jsonb)
   from public.assistant_browser_admissions a where a.user_id = p_user_id and a.status = 'waiting_credential' and a.expires_at > now()));
exception when invalid_text_representation then return '{"ok":false,"code":"invalid"}'::jsonb;
end $$;

create or replace function public.assistant_browser_credential_revoke_v2(p_user_id uuid, p_credential_id uuid, p_expected_version int)
returns jsonb language plpgsql security invoker set search_path = pg_catalog, public as $$
declare c public.assistant_credentials; jrow public.browser_agent_jobs;
begin
 perform pg_advisory_xact_lock(hashtextextended('assistant-browser-owner:' || p_user_id::text, 0));
 select * into c from public.assistant_credentials where id = p_credential_id and user_id = p_user_id for update;
 if c.id is null then return '{"ok":false,"code":"not_found"}'::jsonb; end if;
 if c.version is distinct from p_expected_version then return '{"ok":false,"code":"version_conflict"}'::jsonb; end if;
 for jrow in select j.* from public.browser_agent_jobs j join public.assistant_browser_job_credentials x on x.job_id = j.id
  where x.credential_id = c.id and j.status in ('queued','running','waiting_help','applying') for update of j loop
  update public.browser_agent_jobs set status = 'canceled', error_kind = 'credential_revoked', error = 'Browser authorization changed.',
   cancel_requested_at = now(), finished_at = now(), lease_owner = null, lease_boot_id = null, lease_expires_at = null, hold_until = null
   where id = jrow.id;
 end loop;
 -- Cancels v1 jobs and removes the credential's saved sessions (0186).
 perform public.assistant_browser_cancel_credentials(p_user_id, c.id);
 update public.assistant_credentials set status = 'revoked', version = version + 1, wrapped_dek = null, ciphertext = null, nonce = null,
  key_id = null, is_default = false, login_attempt_id = null, login_job_id = null, login_epoch = null, login_attempt_expires_at = null,
  updated_at = now() where id = c.id;
 if c.is_default then
  update public.assistant_credentials set is_default = true where id = (select id from public.assistant_credentials
   where user_id = p_user_id and site_key = c.site_key and status = 'active' and id <> c.id order by created_at limit 1);
 end if;
 return jsonb_build_object('ok', true, 'version', c.version + 1);
end $$;

do $$ declare f text; begin
 foreach f in array array[
  'assistant_browser_site_grant(uuid, text, text, text, text, text, text, uuid, jsonb)',
  'assistant_browser_scope_extend(uuid, uuid, text)', 'assistant_browser_admission_create(uuid, jsonb)',
  'assistant_browser_start_decide(uuid, uuid, text)', 'assistant_browser_request_create(uuid, jsonb)',
  'assistant_browser_request_decide(uuid, uuid, text)',
  'assistant_browser_task_enqueue_v2(uuid, uuid, jsonb, numeric, numeric)',
  'assistant_browser_credential_put_v2(uuid, text, text, int, jsonb, uuid)',
  'assistant_browser_credential_revoke_v2(uuid, uuid, int)'] loop
  execute format('grant execute on function public.%s to service_role', f);
 end loop;
end $$;
revoke all on function public.assistant_browser_site_grant(uuid, text, text, text, text, text, text, uuid, jsonb) from public;
revoke all on function public.assistant_browser_site_grant(uuid, text, text, text, text, text, text, uuid, jsonb) from anon, authenticated;
revoke all on function public.assistant_browser_scope_extend(uuid, uuid, text) from public;
revoke all on function public.assistant_browser_scope_extend(uuid, uuid, text) from anon, authenticated;
revoke all on function public.assistant_browser_admission_create(uuid, jsonb) from public;
revoke all on function public.assistant_browser_admission_create(uuid, jsonb) from anon, authenticated;
revoke all on function public.assistant_browser_start_decide(uuid, uuid, text) from public;
revoke all on function public.assistant_browser_start_decide(uuid, uuid, text) from anon, authenticated;
revoke all on function public.assistant_browser_request_create(uuid, jsonb) from public;
revoke all on function public.assistant_browser_request_create(uuid, jsonb) from anon, authenticated;
revoke all on function public.assistant_browser_request_decide(uuid, uuid, text) from public;
revoke all on function public.assistant_browser_request_decide(uuid, uuid, text) from anon, authenticated;
revoke all on function public.assistant_browser_task_enqueue_v2(uuid, uuid, jsonb, numeric, numeric) from public;
revoke all on function public.assistant_browser_task_enqueue_v2(uuid, uuid, jsonb, numeric, numeric) from anon, authenticated;
revoke all on function public.assistant_browser_credential_put_v2(uuid, text, text, int, jsonb, uuid) from public;
revoke all on function public.assistant_browser_credential_put_v2(uuid, text, text, int, jsonb, uuid) from anon, authenticated;
revoke all on function public.assistant_browser_credential_revoke_v2(uuid, uuid, int) from public;
revoke all on function public.assistant_browser_credential_revoke_v2(uuid, uuid, int) from anon, authenticated;

notify pgrst, 'reload schema';

-- ==== PART A3 ====

-- A3.1 Live authority of a v2 job: pinned grant versions, pinned credential
-- versions and the conversation. Null when everything still holds.
create or replace function public.assistant_browser_v2_authority(p_job public.browser_agent_jobs)
returns text language plpgsql stable security invoker set search_path = pg_catalog, public as $$
declare a public.assistant_browser_admissions;
begin
 select * into a from public.assistant_browser_admissions where id = p_job.assistant_admission_id;
 if a.id is null or a.user_id is distinct from p_job.principal_user_id then return 'lease_lost'; end if;
 if exists(select 1 from jsonb_array_elements(a.site_scope) e where not exists(select 1 from public.assistant_browser_site_grants g
  where g.id = (e->>'grant_id')::uuid and g.user_id = a.user_id and g.site_key = e->>'site_key' and g.status = 'active'
   and g.version = (e->>'grant_version')::int)) then return 'scope_revoked'; end if;
 if exists(select 1 from public.assistant_browser_job_credentials x where x.job_id = p_job.id and x.credential_id is not null
  and not exists(select 1 from public.assistant_credentials c where c.id = x.credential_id and c.user_id = a.user_id
   and c.status = 'active' and c.version = x.credential_version)) then return 'lease_lost'; end if;
 if not exists(select 1 from public.assistant_conversations where id = p_job.assistant_conversation_id
  and user_id = p_job.principal_user_id and mode = 'normal' and mailbox_tainted_at is null) then return 'lease_lost'; end if;
 return null;
end $$;

-- A3.2 Fence for v2 jobs. Raises lease_lost or scope_revoked; the job row
-- carries policy_version so a stale worker refetches its policy (10.2).
create or replace function public.assistant_browser_user_fence_v2(p_job uuid, p_worker uuid, p_boot uuid, p_epoch bigint)
returns public.browser_agent_jobs language plpgsql security invoker set search_path = pg_catalog, public as $$
declare j public.browser_agent_jobs; w public.browser_agent_workers; v_code text;
begin
 if not public.assistant_browser_runtime_ready() then raise exception 'browser_runtime_unavailable'; end if;
 select * into w from public.browser_agent_workers where id = p_worker for share;
 if w.id is null or w.principal_kind <> 'user' or w.status <> 'active' or w.boot_id is distinct from p_boot then raise exception 'lease_lost'; end if;
 select * into j from public.browser_agent_jobs where id = p_job and principal_kind = 'user' and agent_type = 'user_browser';
 if j.id is null or not public.assistant_browser_owner_eligible(j.principal_user_id, true) then raise exception 'lease_lost'; end if;
 perform pg_advisory_xact_lock(hashtextextended('assistant-browser-owner:' || j.principal_user_id::text, 0));
 select * into j from public.browser_agent_jobs where id = p_job for update;
 if j.lease_owner is distinct from p_worker or j.lease_boot_id is distinct from p_boot or j.lease_epoch is distinct from p_epoch
  or j.status not in ('running','waiting_help') or j.expires_at <= now() or j.lease_expires_at is null or j.lease_expires_at <= now()
  or j.cancel_requested_at is not null or not (j.agent_type = any(w.allowed_types)) then raise exception 'lease_lost'; end if;
 perform 1 from public.browser_agent_types where key = 'user_browser' and principal_kind = 'user' and enabled;
 if not found then raise exception 'lease_lost'; end if;
 perform 1 from public.browser_agent_settings where id = 1 and enabled;
 if not found then raise exception 'lease_lost'; end if;
 v_code := public.assistant_browser_v2_authority(j);
 if v_code is not null then raise exception '%', v_code; end if;
 return j;
end $$;

-- The 0189 fence hands v2 jobs to the v2 fence, so the shared runtime RPCs
-- (heartbeat, record put, turn finish, job finish) serve both types.
create or replace function public.assistant_browser_user_fence(p_job uuid,p_worker uuid,p_boot uuid,p_epoch bigint)
returns public.browser_agent_jobs language plpgsql security invoker set search_path=pg_catalog,public as $$
declare j public.browser_agent_jobs; w public.browser_agent_workers; c public.assistant_credentials;
begin
 if exists(select 1 from public.browser_agent_jobs where id=p_job and agent_type='user_browser') then
  return public.assistant_browser_user_fence_v2(p_job,p_worker,p_boot,p_epoch);
 end if;
 if not public.assistant_browser_runtime_ready() then raise exception 'browser_runtime_unavailable'; end if;
 select * into w from public.browser_agent_workers where id=p_worker for share;
 if w.id is null or w.principal_kind<>'user' or w.status<>'active' or w.boot_id is distinct from p_boot then raise exception 'lease_lost'; end if;
 select * into j from public.browser_agent_jobs where id=p_job and principal_kind='user';
 if j.id is null or not public.assistant_browser_owner_eligible(j.principal_user_id,true) then raise exception 'lease_lost'; end if;
 perform pg_advisory_xact_lock(hashtextextended('assistant-browser-owner:'||j.principal_user_id::text,0));
 select * into j from public.browser_agent_jobs where id=p_job for update;
 if j.principal_kind<>'user' or j.lease_owner is distinct from p_worker or j.lease_boot_id is distinct from p_boot
  or j.lease_epoch is distinct from p_epoch or j.status not in ('running','waiting_help') or j.expires_at<=now()
  or j.lease_expires_at is null or j.lease_expires_at<=now()
  or j.cancel_requested_at is not null or not (j.agent_type=any(w.allowed_types)) then raise exception 'lease_lost'; end if;
 perform 1 from public.browser_agent_types where key=j.agent_type and principal_kind='user' and enabled;
 if not found then raise exception 'lease_lost'; end if;
 perform 1 from public.browser_agent_settings where id=1 and enabled;
 if not found then raise exception 'lease_lost'; end if;
 select * into c from public.assistant_credentials where id=j.user_credential_id for share;
 if c.id is null or c.user_id is distinct from j.principal_user_id or c.version is distinct from j.user_credential_version or c.status<>'active'
  or not coalesce(public.assistant_browser_task_origin(j.input->>'url')=any(c.origins),false) then raise exception 'lease_lost'; end if;
 perform 1 from public.assistant_conversations where id=j.assistant_conversation_id and user_id=j.principal_user_id and mode='normal' and mailbox_tainted_at is null for share;
 if not found then raise exception 'lease_lost'; end if;
 return j;
end $$;

-- A3.3 Job guard: v2 jobs carry an admission and no single credential.
create or replace function public.assistant_browser_job_guard()
returns trigger language plpgsql security invoker set search_path=pg_catalog,public as $$
declare v_kind text;
begin
 if tg_op='UPDATE' and (new.principal_kind is distinct from old.principal_kind
  or new.principal_user_id is distinct from old.principal_user_id
  or new.user_credential_id is distinct from old.user_credential_id
  or new.user_credential_version is distinct from old.user_credential_version
  or new.assistant_approval_id is distinct from old.assistant_approval_id
  or new.assistant_admission_id is distinct from old.assistant_admission_id
  or new.assistant_conversation_id is distinct from old.assistant_conversation_id
  or new.assistant_reservation_id is distinct from old.assistant_reservation_id) then
  raise exception 'browser_principal_immutable';
 end if;
 select principal_kind into v_kind from public.browser_agent_types where key=new.agent_type;
 if v_kind is distinct from new.principal_kind then raise exception 'browser_principal_mismatch'; end if;
 if new.lease_owner is not null then
  select principal_kind into v_kind from public.browser_agent_workers where id=new.lease_owner;
  if v_kind is distinct from new.principal_kind then raise exception 'browser_pool_mismatch'; end if;
  if new.principal_kind='user' and not public.assistant_browser_runtime_ready() then raise exception 'browser_runtime_unavailable'; end if;
 end if;
 if new.principal_kind='user' and tg_op='INSERT' then
  if not public.assistant_browser_runtime_ready() then raise exception 'browser_runtime_unavailable'; end if;
  if new.agent_type='user_browser' then
   if not public.assistant_browser_owner_eligible(new.principal_user_id,true) or new.assistant_admission_id is null
    or new.assistant_approval_id is not null or new.user_credential_id is not null
    or not exists(select 1 from public.assistant_browser_admissions where id=new.assistant_admission_id and user_id=new.principal_user_id) then
    raise exception 'browser_owner_ineligible';
   end if;
  elsif not public.assistant_browser_owner_eligible(new.principal_user_id,true) or new.assistant_admission_id is not null
   or not exists(select 1 from public.assistant_credentials where id=new.user_credential_id
    and user_id=new.principal_user_id and version=new.user_credential_version and status='active') then
   raise exception 'browser_owner_ineligible';
  end if;
 end if;
 return new;
end $$;

-- A3.4 Scope and job policy (5.4, 10.2).
create or replace function public.assistant_browser_scope_allows(p_job uuid, p_url text)
returns boolean language sql stable security invoker set search_path = pg_catalog, public as $$
 select coalesce(bool_or(case when g.scope = 'origin' then public.assistant_browser_v2_origin(p_url) = g.value
  else public.assistant_browser_v2_site_matches(s, p_url) end), false)
  from public.browser_agent_jobs j join public.assistant_browser_admissions a on a.id = j.assistant_admission_id
  cross join lateral jsonb_array_elements(a.site_scope) e
  join public.assistant_browser_site_grants g on g.id = (e->>'grant_id')::uuid and g.status = 'active'
  join public.assistant_browser_sites s on s.site_key = g.site_key and s.enabled
  where j.id = p_job
$$;
create or replace function public.assistant_browser_job_policy(p_job uuid, p_worker uuid, p_boot uuid, p_epoch bigint)
returns jsonb language plpgsql security invoker set search_path = pg_catalog, public as $$
declare j public.browser_agent_jobs; a public.assistant_browser_admissions; cfg public.browser_agent_settings; typ public.browser_agent_types;
 st public.assistant_browser_sites;
begin
 j := public.assistant_browser_user_fence_v2(p_job, p_worker, p_boot, p_epoch);
 select * into a from public.assistant_browser_admissions where id = j.assistant_admission_id;
 select * into cfg from public.browser_agent_settings where id = 1;
 select * into typ from public.browser_agent_types where key = 'user_browser';
 select * into st from public.assistant_browser_sites where site_key = a.start_site_key;
 -- Wire shape is protocol.UserJobPolicy: 'scope' per site, a start_origin even
 -- when the task was admitted by site key only, and only pinned credentials
 -- (a session-only account has no credential row to fill).
 return jsonb_build_object('ok', true, 'policy_version', j.policy_version, 'job_id', j.id, 'start_url', a.start_url,
  'start_origin', coalesce(public.assistant_browser_v2_origin(a.start_url), st.exact_origins[1], 'https://' || st.registrable_domains[1]),
  'start_site_key', a.start_site_key, 'project_id', a.project_id,
  'sites', (select coalesce(jsonb_agg(jsonb_build_object('site_key', s.site_key, 'label', s.label, 'kind', s.kind,
    'registrable_domains', s.registrable_domains,
    'exact_origins', case when g.scope = 'origin' then array[g.value] else s.exact_origins end,
    'scope', g.scope, 'grant_scope', g.scope, 'grant_value', g.value,
    'path_prefix', coalesce(g.path_prefix, s.path_prefix), 'login_paths', s.login_paths, 'login_kind', s.login_kind,
    'profile', case when s.kind = 'curated' then s.profile else s.profile - 'websocket_ok' end, 'needs_headed', s.needs_headed,
    'grant_version', g.version)), '[]'::jsonb)
   from jsonb_array_elements(a.site_scope) e join public.assistant_browser_site_grants g on g.id = (e->>'grant_id')::uuid
   join public.assistant_browser_sites s on s.site_key = g.site_key),
  'credentials', (select coalesce(jsonb_agg(jsonb_build_object('credential_id', x.credential_id, 'site_key', x.site_key,
    'account_label', x.account_label, 'version', x.credential_version, 'login_origins', c.login_origins,
    'cookie_domains', c.cookie_domains, 'sso_required', c.sso_required)), '[]'::jsonb)
   from public.assistant_browser_job_credentials x join public.assistant_credentials c on c.id = x.credential_id
   where x.job_id = j.id and x.credential_id is not null),
  'safe_rules', (select coalesce(jsonb_agg(jsonb_build_object('site_key', r.site_key, 'method', r.method, 'host', r.host,
    'path_pattern', r.path_pattern, 'graphql_operation', r.graphql_operation)), '[]'::jsonb)
   from public.assistant_browser_user_safe_rules r where r.user_id = j.principal_user_id
    and a.site_scope @> jsonb_build_array(jsonb_build_object('site_key', r.site_key))),
  'caps', jsonb_build_object('max_cost_usd', least(a.max_cost_usd, cfg.user_per_job_cost_cap_usd),
   'offsite_query_bytes', cfg.user_offsite_query_bytes, 'hold_timeout_seconds', cfg.user_hold_timeout_seconds,
   'park_max_minutes', cfg.user_park_max_minutes, 'mfa_mail_poll_seconds', cfg.user_mfa_mail_poll_seconds,
   'max_steps', typ.max_steps, 'max_total_tokens', typ.max_total_tokens, 'max_active_seconds', typ.max_active_seconds));
end $$;

-- A3.5 Login on any site (7.4). The credential must be in the job's set and
-- the origin in its login_origins; rolling 24 hour cap of 5 counted attempts.
create or replace function public.assistant_browser_login_begin_v2(p_job uuid, p_worker uuid, p_boot uuid, p_epoch bigint,
 p_credential_id uuid, p_origin text)
returns jsonb language plpgsql security invoker set search_path = pg_catalog, public as $$
declare j public.browser_agent_jobs; x public.assistant_browser_job_credentials; c public.assistant_credentials; attempt uuid := gen_random_uuid();
begin
 j := public.assistant_browser_user_fence_v2(p_job, p_worker, p_boot, p_epoch);
 -- Refusal codes are protocol.UserLoginRefusal values. The fence already
 -- checked every pinned credential is active at its pinned version.
 select * into x from public.assistant_browser_job_credentials where job_id = j.id and credential_id = p_credential_id;
 if x.id is null then return '{"ok":false,"code":"not_in_job"}'::jsonb; end if;
 select * into c from public.assistant_credentials where id = p_credential_id for update;
 if c.status <> 'active' or c.version is distinct from x.credential_version then return '{"ok":false,"code":"not_configured"}'::jsonb; end if;
 if c.sso_required then return '{"ok":false,"code":"sso_required"}'::jsonb; end if;
 if p_origin is null or p_origin !~ '^https://' then return '{"ok":false,"code":"not_https"}'::jsonb; end if;
 if public.assistant_browser_v2_is_idp(p_origin) then return '{"ok":false,"code":"identity_provider"}'::jsonb; end if;
 if not (p_origin = any(c.login_origins)) then return '{"ok":false,"code":"origin_not_allowed"}'::jsonb; end if;
 if c.locked_until > now() then return jsonb_build_object('ok', true, 'granted', false, 'code', 'locked', 'retry_at', c.locked_until); end if;
 if c.login_attempt_id is not null and c.login_attempt_expires_at > now() then
  return jsonb_build_object('ok', true, 'granted', false, 'code', 'login_busy', 'retry_at', c.login_attempt_expires_at);
 end if;
 if c.last_login_attempt_at > now() - interval '600 seconds' then
  return jsonb_build_object('ok', true, 'granted', false, 'code', 'spacing', 'retry_at', c.last_login_attempt_at + interval '600 seconds');
 end if;
 if c.attempts_day_started_at > now() - interval '24 hours' and c.attempts_day_count >= 5 then
  return jsonb_build_object('ok', true, 'granted', false, 'code', 'daily_cap', 'retry_at', c.attempts_day_started_at + interval '24 hours');
 end if;
 update public.assistant_credentials set login_attempt_id = attempt, login_job_id = j.id, login_epoch = p_epoch,
  login_attempt_expires_at = least(j.expires_at, now() + interval '10 minutes'), last_login_attempt_at = now()
  where id = c.id returning * into c;
 return jsonb_build_object('ok', true, 'granted', true, 'attempt_id', attempt, 'credential', to_jsonb(c));
end $$;

create or replace function public.assistant_browser_login_finish_v2(p_job uuid, p_worker uuid, p_boot uuid, p_epoch bigint,
 p_credential_id uuid, p_attempt_id uuid, p_outcome text, p_session jsonb)
returns jsonb language plpgsql security invoker set search_path = pg_catalog, public as $$
declare j public.browser_agent_jobs; c public.assistant_credentials; s public.assistant_browser_sessions; cfg public.browser_agent_settings;
 v_counts boolean; v_kind text; v_code text;
begin
 j := public.assistant_browser_user_fence_v2(p_job, p_worker, p_boot, p_epoch);
 if not exists(select 1 from public.assistant_browser_job_credentials where job_id = j.id and credential_id = p_credential_id) then
  return '{"ok":false,"code":"credential_not_in_job"}'::jsonb;
 end if;
 select * into c from public.assistant_credentials where id = p_credential_id for update;
 if c.login_attempt_id is distinct from p_attempt_id or c.login_job_id is distinct from j.id or c.login_epoch is distinct from p_epoch
  or c.login_attempt_expires_at <= now() then return '{"ok":false,"code":"attempt_changed"}'::jsonb; end if;
 if p_outcome is null or p_outcome not in ('ok','password_rejected','mfa','push','captcha','challenge','sso','unexpected','timeout') then
  return '{"ok":false,"code":"invalid"}'::jsonb;
 end if;
 -- MFA and push keep the exact attempt alive; they never release a new password.
 if p_outcome in ('mfa','push') then return jsonb_build_object('ok', true, 'attempt_id', p_attempt_id, 'help_required', true); end if;
 select kind into v_kind from public.assistant_browser_sites where site_key = c.site_key;
 if p_outcome = 'ok' then
  select * into cfg from public.browser_agent_settings where id = 1;
  if coalesce(p_session->>'origin', '') !~ '^https://' or not public.assistant_browser_scope_allows(j.id, p_session->>'origin')
   or not public.assistant_browser_record_valid(jsonb_build_object('id', p_session->>'id', 'kind', 'snapshot', 'envelope', p_session->'envelope')) then
   return '{"ok":false,"code":"session_invalid"}'::jsonb;
  end if;
  -- A v1 row for this credential and origin (no site key) takes the v2 key,
  -- so the upsert below lands on it instead of UNIQUE (credential_id, origin).
  update public.assistant_browser_sessions set site_key = c.site_key, account_label = c.account_label
   where credential_id = c.id and origin = p_session->>'origin' and site_key is null;
  insert into public.assistant_browser_sessions(id, user_id, credential_id, credential_version, origin, version, wrapped_dek, ciphertext,
   nonce, key_id, expires_at, site_key, account_label, source)
  values((p_session->>'id')::uuid, c.user_id, c.id, c.version, p_session->>'origin', 1, p_session#>>'{envelope,wrapped_dek}',
   p_session#>>'{envelope,ciphertext}', p_session#>>'{envelope,nonce}', p_session#>>'{envelope,key_id}',
   least(coalesce((p_session->>'expires_at')::timestamptz, now() + make_interval(hours => cfg.user_password_session_max_hours)),
    now() + make_interval(hours => cfg.user_password_session_max_hours)), c.site_key, c.account_label, 'password')
  on conflict (user_id, site_key, account_label, origin) do update set credential_id = excluded.credential_id,
   credential_version = excluded.credential_version, version = assistant_browser_sessions.version + 1,
   wrapped_dek = excluded.wrapped_dek, ciphertext = excluded.ciphertext, nonce = excluded.nonce, key_id = excluded.key_id,
   expires_at = excluded.expires_at, source = 'password', updated_at = now()
  returning * into s;
 end if;
 if p_outcome in ('captcha','challenge') then
  update public.assistant_browser_sites set challenge_count = challenge_count + 1, needs_headed = needs_headed or challenge_count + 1 >= 3,
   updated_at = now() where site_key = c.site_key;
 end if;
 v_counts := p_outcome not in ('ok','mfa','push');
 update public.assistant_credentials set login_attempt_id = null, login_job_id = null, login_epoch = null, login_attempt_expires_at = null,
  login_failures = case when p_outcome = 'ok' then 0 when p_outcome = 'password_rejected' then login_failures + 1 else login_failures end,
  locked_until = case when p_outcome = 'ok' then null when p_outcome = 'password_rejected' and login_failures + 1 >= 3
   then now() + interval '24 hours' else locked_until end,
  sso_required = sso_required or p_outcome = 'sso',
  attempts_day_count = case when not v_counts then attempts_day_count
   when attempts_day_started_at is null or attempts_day_started_at <= now() - interval '24 hours' then 1 else attempts_day_count + 1 end,
  attempts_day_started_at = case when v_counts and (attempts_day_started_at is null or attempts_day_started_at <= now() - interval '24 hours')
   then now() else attempts_day_started_at end
  where id = c.id returning * into c;
 update public.assistant_browser_help_state set status = 'resolved' where job_id = j.id and login_attempt_id = p_attempt_id and status = 'open';
 update public.assistant_browser_records set envelope = null, consumed_at = coalesce(consumed_at, now())
  where job_id = j.id and kind = 'help_secret' and consumed_at is null;
 v_code := case when p_outcome in ('captcha','challenge') then 'site_challenge' when p_outcome = 'sso' then 'sso_required'
  when p_outcome in ('unexpected','timeout') and v_kind = 'ad_hoc' then 'login_unclear' end;
 return jsonb_build_object('ok', true, 'code', v_code, 'login_failures', c.login_failures, 'locked_until', c.locked_until,
  'attempts_day_count', c.attempts_day_count, 'session', case when s.id is null then null else to_jsonb(s) end);
exception when invalid_text_representation then return '{"ok":false,"code":"session_invalid"}'::jsonb;
end $$;

-- Session pickup by a second task on the same account (7.4) or a live
-- sign-in session (no credential), never another account's jar (10.5).
-- With no origin (UserSessionPoll names only the credential), the newest
-- unexpired session of that account whose origin is in the task scope.
create or replace function public.assistant_browser_session_get_v2(p_job uuid, p_worker uuid, p_boot uuid, p_epoch bigint,
 p_site_key text, p_account_label text, p_origin text default null, p_newer_than timestamptz default null)
returns jsonb language plpgsql security invoker set search_path = pg_catalog, public as $$
declare j public.browser_agent_jobs; x public.assistant_browser_job_credentials; s public.assistant_browser_sessions;
begin
 j := public.assistant_browser_user_fence_v2(p_job, p_worker, p_boot, p_epoch);
 select * into x from public.assistant_browser_job_credentials where job_id = j.id and site_key = p_site_key and account_label = p_account_label;
 if x.id is null then return '{"ok":false,"code":"credential_not_in_job"}'::jsonb; end if;
 if p_origin is not null and not public.assistant_browser_scope_allows(j.id, p_origin) then
  return '{"ok":false,"code":"origin_refused"}'::jsonb;
 end if;
 select * into s from public.assistant_browser_sessions where user_id = j.principal_user_id and site_key = x.site_key
  and account_label = x.account_label and (origin = p_origin or (p_origin is null and public.assistant_browser_scope_allows(j.id, origin)))
  and expires_at > now()
  and (x.credential_id is null or (credential_id = x.credential_id and credential_version = x.credential_version))
  and (p_newer_than is null or updated_at > p_newer_than)
  order by updated_at desc limit 1;
 return jsonb_build_object('ok', true, 'session', case when s.id is null then null else to_jsonb(s) end);
end $$;

revoke all on function public.assistant_browser_v2_authority(public.browser_agent_jobs) from public;
revoke all on function public.assistant_browser_v2_authority(public.browser_agent_jobs) from anon, authenticated;
grant execute on function public.assistant_browser_v2_authority(public.browser_agent_jobs) to service_role;
revoke all on function public.assistant_browser_user_fence_v2(uuid, uuid, uuid, bigint) from public;
revoke all on function public.assistant_browser_user_fence_v2(uuid, uuid, uuid, bigint) from anon, authenticated;
grant execute on function public.assistant_browser_user_fence_v2(uuid, uuid, uuid, bigint) to service_role;
revoke all on function public.assistant_browser_user_fence(uuid, uuid, uuid, bigint) from public;
revoke all on function public.assistant_browser_user_fence(uuid, uuid, uuid, bigint) from anon, authenticated;
grant execute on function public.assistant_browser_user_fence(uuid, uuid, uuid, bigint) to service_role;
revoke all on function public.assistant_browser_job_guard() from public;
revoke all on function public.assistant_browser_job_guard() from anon, authenticated;
grant execute on function public.assistant_browser_job_guard() to service_role;
revoke all on function public.assistant_browser_scope_allows(uuid, text) from public;
revoke all on function public.assistant_browser_scope_allows(uuid, text) from anon, authenticated;
grant execute on function public.assistant_browser_scope_allows(uuid, text) to service_role;
revoke all on function public.assistant_browser_job_policy(uuid, uuid, uuid, bigint) from public;
revoke all on function public.assistant_browser_job_policy(uuid, uuid, uuid, bigint) from anon, authenticated;
grant execute on function public.assistant_browser_job_policy(uuid, uuid, uuid, bigint) to service_role;
revoke all on function public.assistant_browser_login_begin_v2(uuid, uuid, uuid, bigint, uuid, text) from public;
revoke all on function public.assistant_browser_login_begin_v2(uuid, uuid, uuid, bigint, uuid, text) from anon, authenticated;
grant execute on function public.assistant_browser_login_begin_v2(uuid, uuid, uuid, bigint, uuid, text) to service_role;
revoke all on function public.assistant_browser_login_finish_v2(uuid, uuid, uuid, bigint, uuid, uuid, text, jsonb) from public;
revoke all on function public.assistant_browser_login_finish_v2(uuid, uuid, uuid, bigint, uuid, uuid, text, jsonb) from anon, authenticated;
grant execute on function public.assistant_browser_login_finish_v2(uuid, uuid, uuid, bigint, uuid, uuid, text, jsonb) to service_role;
revoke all on function public.assistant_browser_session_get_v2(uuid, uuid, uuid, bigint, text, text, text, timestamptz) from public;
revoke all on function public.assistant_browser_session_get_v2(uuid, uuid, uuid, bigint, text, text, text, timestamptz) from anon, authenticated;
grant execute on function public.assistant_browser_session_get_v2(uuid, uuid, uuid, bigint, text, text, text, timestamptz) to service_role;

notify pgrst, 'reload schema';

-- ==== PART A4 ====

-- A4.1 Help channel v2 (8.1): credential id, alphanumeric codes are checked
-- in Python before encryption, continue for push, step-up outside a login.
create or replace function public.assistant_browser_help_open_v2(p_job uuid, p_worker uuid, p_boot uuid, p_epoch bigint, p_help_id uuid,
 p_record jsonb, p_reason_code text, p_credential_id uuid default null, p_login_attempt_id uuid default null)
returns jsonb language plpgsql security invoker set search_path = pg_catalog, public as $$
declare j public.browser_agent_jobs; h public.assistant_browser_help_state; r public.assistant_browser_records; c public.assistant_credentials;
 cfg public.browser_agent_settings;
begin
 j := public.assistant_browser_user_fence_v2(p_job, p_worker, p_boot, p_epoch);
 select * into h from public.assistant_browser_help_state where job_id = j.id and status = 'open';
 if found then return jsonb_build_object('ok', true, 'created', false, 'help', to_jsonb(h)); end if;
 if p_help_id is null or p_record->>'kind' is distinct from 'help_request' or p_reason_code is null or p_reason_code = 'hold'
  or p_reason_code not in ('login_failed','captcha','mfa','sso','logged_out','access_denied','not_found','ambiguous','unexpected_page',
   'cap_reached','needs_info','other','needs_credential','needs_site','mfa_step_up','push') then return '{"ok":false,"code":"invalid"}'::jsonb; end if;
 if p_login_attempt_id is not null then
  if not exists(select 1 from public.assistant_browser_job_credentials where job_id = j.id and credential_id = p_credential_id) then
   return '{"ok":false,"code":"credential_not_in_job"}'::jsonb;
  end if;
  select * into c from public.assistant_credentials where id = p_credential_id;
  if c.login_attempt_id is distinct from p_login_attempt_id or c.login_job_id is distinct from j.id
   or c.login_epoch is distinct from p_epoch or c.login_attempt_expires_at <= now() then return '{"ok":false,"code":"attempt_changed"}'::jsonb; end if;
 end if;
 select * into cfg from public.browser_agent_settings where id = 1;
 r := public.assistant_browser_record_insert(j.principal_user_id, j.id, p_record, p_help_id, null);
 insert into public.assistant_browser_help_state(id, user_id, job_id, request_record_id, login_attempt_id, is_login_scope, expires_at,
  reason_code, credential_id)
 values(p_help_id, j.principal_user_id, j.id, r.id, p_login_attempt_id, p_login_attempt_id is not null,
  least(j.expires_at, coalesce(c.login_attempt_expires_at, now() + make_interval(mins => cfg.user_park_max_minutes))),
  p_reason_code, p_credential_id) returning * into h;
 update public.browser_agent_jobs set status = 'waiting_help' where id = j.id;
 perform public.assistant_browser_notice(j.id, 'help:' || h.id::text, 'assistant.browser_help', 'Your browser task needs your help.');
 insert into public.assistant_browser_chat_outbox(job_id, admission_id, user_id, conversation_id, event_key, payload)
 values(j.id, j.assistant_admission_id, j.principal_user_id, j.assistant_conversation_id, 'help:' || h.id::text,
  jsonb_build_object('help_id', h.id, 'reason_code', p_reason_code)) on conflict do nothing;
 return jsonb_build_object('ok', true, 'created', true, 'help', to_jsonb(h));
end $$;

create or replace function public.assistant_browser_help_reply_v2(p_user_id uuid, p_job_id uuid, p_help_id uuid, p_client_msg_id uuid,
 p_action text, p_record jsonb)
returns jsonb language plpgsql security invoker set search_path = pg_catalog, public as $$
declare j public.browser_agent_jobs; h public.assistant_browser_help_state; r public.assistant_browser_records; c public.assistant_credentials;
begin
 if not public.assistant_browser_owner_eligible(p_user_id, true) then return '{"ok":false,"code":"forbidden"}'::jsonb; end if;
 perform pg_advisory_xact_lock(hashtextextended('assistant-browser-owner:' || p_user_id::text, 0));
 select * into j from public.browser_agent_jobs where id = p_job_id and principal_kind = 'user' and principal_user_id = p_user_id
  and agent_type = 'user_browser' for update;
 select * into h from public.assistant_browser_help_state where id = p_help_id and job_id = p_job_id and user_id = p_user_id for update;
 if j.id is null or h.id is null or p_client_msg_id is null or p_action is null
  or p_action not in ('message','code','retry','give_up','continue') then return '{"ok":false,"code":"help_changed"}'::jsonb; end if;
 select * into r from public.assistant_browser_records where help_id = h.id and client_msg_id = p_client_msg_id;
 if found then return jsonb_build_object('ok', true, 'created', false, 'record', to_jsonb(r), 'help', to_jsonb(h)); end if;
 if h.status <> 'open' or h.expires_at <= now() or j.status not in ('running','waiting_help') then return '{"ok":false,"code":"help_changed"}'::jsonb; end if;
 if (select count(*) from public.assistant_browser_records where help_id = h.id and client_msg_id is not null) >= 100 then
  return '{"ok":false,"code":"help_limit"}'::jsonb;
 end if;
 if p_action = 'continue' and coalesce(h.reason_code, '') not in ('push','mfa','mfa_step_up') then return '{"ok":false,"code":"invalid"}'::jsonb; end if;
 if p_action = 'code' then
  if exists(select 1 from public.assistant_browser_records where help_id = h.id and kind = 'help_secret') then
   return '{"ok":false,"code":"code_already_supplied"}'::jsonb;
  end if;
  if j.lease_owner is null or j.lease_expires_at <= now() then return '{"ok":false,"code":"attempt_changed"}'::jsonb; end if;
  if h.is_login_scope then
   select * into c from public.assistant_credentials where id = h.credential_id for update;
   if c.id is null or c.status <> 'active' or not exists(select 1 from public.assistant_browser_job_credentials x where x.job_id = j.id
     and x.credential_id = c.id and x.credential_version = c.version)
    or c.login_attempt_id is distinct from h.login_attempt_id or c.login_job_id is distinct from j.id
    or c.login_epoch is distinct from j.lease_epoch or c.login_attempt_expires_at <= now() then return '{"ok":false,"code":"attempt_changed"}'::jsonb; end if;
  elsif coalesce(h.reason_code, '') not in ('mfa','mfa_step_up') then
   return '{"ok":false,"code":"attempt_changed"}'::jsonb;
  end if;
  if p_record->>'kind' is distinct from 'help_secret' then return '{"ok":false,"code":"invalid"}'::jsonb; end if;
 elsif p_record->>'kind' is distinct from 'help_message' then return '{"ok":false,"code":"invalid"}'::jsonb;
 end if;
 r := public.assistant_browser_record_insert(p_user_id, j.id, p_record, h.id, p_client_msg_id);
 if p_action = 'give_up' then
  update public.assistant_browser_help_state set status = 'canceled' where id = h.id returning * into h;
  update public.browser_agent_jobs set cancel_requested_at = now() where id = j.id;
 elsif p_action in ('retry','message','continue') then
  update public.assistant_browser_help_state set status = 'resolved' where id = h.id returning * into h;
  update public.browser_agent_jobs set status = case when lease_owner is null then 'queued' else 'running' end, next_attempt_at = null where id = j.id;
 end if;
 return jsonb_build_object('ok', true, 'created', true, 'record', to_jsonb(r), 'help', to_jsonb(h));
end $$;

create or replace function public.assistant_browser_help_consume_v2(p_job uuid, p_worker uuid, p_boot uuid, p_epoch bigint, p_help_id uuid,
 p_record_id uuid, p_login_attempt_id uuid default null)
returns jsonb language plpgsql security invoker set search_path = pg_catalog, public as $$
declare j public.browser_agent_jobs; h public.assistant_browser_help_state; r public.assistant_browser_records; c public.assistant_credentials;
begin
 j := public.assistant_browser_user_fence_v2(p_job, p_worker, p_boot, p_epoch);
 select * into h from public.assistant_browser_help_state where id = p_help_id and job_id = j.id and status = 'open' and expires_at > now() for update;
 if h.id is null then return '{"ok":false,"code":"attempt_changed"}'::jsonb; end if;
 if h.is_login_scope then
  select * into c from public.assistant_credentials where id = h.credential_id for update;
  if h.login_attempt_id is distinct from p_login_attempt_id or c.login_attempt_id is distinct from p_login_attempt_id
   or c.login_job_id is distinct from j.id or c.login_epoch is distinct from p_epoch or c.login_attempt_expires_at <= now() then
   return '{"ok":false,"code":"attempt_changed"}'::jsonb;
  end if;
 elsif p_login_attempt_id is not null or coalesce(h.reason_code, '') not in ('mfa','mfa_step_up') then
  return '{"ok":false,"code":"attempt_changed"}'::jsonb;
 end if;
 select * into r from public.assistant_browser_records where id = p_record_id and job_id = j.id and help_id = h.id
  and kind = 'help_secret' and consumed_at is null for update;
 if r.id is null then return '{"ok":false,"code":"already_consumed"}'::jsonb; end if;
 update public.assistant_browser_records set envelope = null, consumed_at = now() where id = r.id;
 return jsonb_build_object('ok', true, 'record', to_jsonb(r));
end $$;

-- Emailed codes (8.3): the server reader writes the code, never the model.
create or replace function public.assistant_browser_help_secret_from_mail(p_user_id uuid, p_job_id uuid, p_help_id uuid,
 p_message_sha256 text, p_record jsonb)
returns jsonb language plpgsql security invoker set search_path = pg_catalog, public as $$
declare j public.browser_agent_jobs; h public.assistant_browser_help_state; r public.assistant_browser_records; c public.assistant_credentials;
begin
 -- WI-14: the record is sealed in Python with its own client_msg_id (the
 -- envelope binds it), so the row must store that id, never a new one.
 if p_message_sha256 is null or p_message_sha256 !~ '^[0-9a-f]{64}$' or p_record->>'kind' is distinct from 'help_secret'
  or coalesce(p_record->>'client_msg_id', '') !~ '^[0-9a-f-]{36}$' then
  return '{"ok":false,"code":"invalid"}'::jsonb;
 end if;
 if not public.assistant_browser_owner_eligible(p_user_id, true) then return '{"ok":false,"code":"forbidden"}'::jsonb; end if;
 perform pg_advisory_xact_lock(hashtextextended('assistant-browser-owner:' || p_user_id::text, 0));
 select * into j from public.browser_agent_jobs where id = p_job_id and principal_kind = 'user' and principal_user_id = p_user_id
  and agent_type = 'user_browser' for update;
 select * into h from public.assistant_browser_help_state where id = p_help_id and job_id = p_job_id and user_id = p_user_id
  and status = 'open' and expires_at > now() for update;
 if j.id is null or h.id is null or coalesce(h.reason_code, '') not in ('mfa','mfa_step_up') or j.status not in ('running','waiting_help') then
  return '{"ok":false,"code":"help_changed"}'::jsonb;
 end if;
 -- Login scope: the help's attempt must still be the credential's live one (as help_reply_v2).
 if h.is_login_scope then
  select * into c from public.assistant_credentials where id = h.credential_id for update;
  if c.id is null or c.status <> 'active' or not exists(select 1 from public.assistant_browser_job_credentials x where x.job_id = j.id
    and x.credential_id = c.id and x.credential_version = c.version)
   or c.login_attempt_id is distinct from h.login_attempt_id or c.login_job_id is distinct from j.id
   or c.login_attempt_expires_at <= now() then return '{"ok":false,"code":"attempt_changed"}'::jsonb; end if;
 end if;
 if exists(select 1 from public.assistant_browser_records where help_id = h.id and kind = 'help_secret') then
  return '{"ok":false,"code":"code_already_supplied"}'::jsonb;
 end if;
 insert into public.assistant_browser_mail_codes_used(user_id, message_sha256, job_id) values(p_user_id, p_message_sha256, j.id)
  on conflict do nothing;
 if not found then return '{"ok":false,"code":"message_used"}'::jsonb; end if;
 r := public.assistant_browser_record_insert(p_user_id, j.id, p_record, h.id, (p_record->>'client_msg_id')::uuid);
 return jsonb_build_object('ok', true, 'record_id', r.id, 'code_delivered', true);
end $$;

-- A4.2 Holds (6.5). The worker opens a hold on the request it keeps pending;
-- the owner decides through the human route; the token is signed in Python
-- with BROWSER_HOLD_SIGNING_KEY and stored here so either API process can
-- answer the long poll; the worker consumes it once.
create or replace function public.assistant_browser_hold_open(p_job uuid, p_worker uuid, p_boot uuid, p_epoch bigint, p_hold_id uuid,
 p_kind text, p_hold jsonb, p_summary_record jsonb, p_screenshot_record jsonb default null)
returns jsonb language plpgsql security invoker set search_path = pg_catalog, public as $$
declare j public.browser_agent_jobs; h public.assistant_browser_holds; rs public.assistant_browser_records; sc public.assistant_browser_records;
 cfg public.browser_agent_settings; v_help uuid := gen_random_uuid();
begin
 j := public.assistant_browser_user_fence_v2(p_job, p_worker, p_boot, p_epoch);
 select * into h from public.assistant_browser_holds where id = p_hold_id;
 if h.id is not null then
  if h.job_id <> j.id then return '{"ok":false,"code":"invalid"}'::jsonb; end if;
  return jsonb_build_object('ok', true, 'created', false, 'hold', to_jsonb(h) - 'release_token');
 end if;
 if p_kind not in ('final_action','site','code','live_signin') or jsonb_typeof(p_hold) is distinct from 'object'
  or coalesce(p_hold->>'method', '') !~ '^[A-Z]{3,10}$' or coalesce(p_hold->>'origin', '') !~ '^https?://'
  or coalesce(p_hold->>'url_sha256', '') !~ '^[0-9a-f]{64}$' or coalesce(p_hold->>'body_sha256', '') !~ '^[0-9a-f]{64}$'
  or p_summary_record->>'kind' is distinct from 'request'
  or (p_screenshot_record is not null and p_screenshot_record->>'kind' is distinct from 'screenshot') then
  return '{"ok":false,"code":"invalid"}'::jsonb;
 end if;
 if exists(select 1 from public.assistant_browser_help_state where job_id = j.id and status = 'open') then
  return '{"ok":false,"code":"help_busy"}'::jsonb;
 end if;
 select * into cfg from public.browser_agent_settings where id = 1;
 -- Python seals both records with no help id; they stay bound to this hold
 -- through summary_record_id and screenshot_record_id.
 rs := public.assistant_browser_record_insert(j.principal_user_id, j.id, p_summary_record, null, null);
 if p_screenshot_record is not null then sc := public.assistant_browser_record_insert(j.principal_user_id, j.id, p_screenshot_record, null, null); end if;
 insert into public.assistant_browser_help_state(id, user_id, job_id, request_record_id, expires_at, reason_code, hold_id)
 values(v_help, j.principal_user_id, j.id, rs.id, least(j.expires_at, now() + make_interval(secs => cfg.user_hold_timeout_seconds)), 'hold', p_hold_id);
 insert into public.assistant_browser_holds(id, user_id, job_id, help_id, kind, lease_epoch, method, origin, url_sha256, body_sha256,
  out_of_scope, label, target_site_key, summary_record_id, screenshot_record_id, expires_at)
 values(p_hold_id, j.principal_user_id, j.id, v_help, p_kind, p_epoch, p_hold->>'method', p_hold->>'origin', p_hold->>'url_sha256',
  p_hold->>'body_sha256', coalesce((p_hold->>'out_of_scope')::boolean, false), left(p_hold->>'label', 200), p_hold->>'target_site_key',
  rs.id, sc.id, least(j.expires_at, now() + make_interval(secs => cfg.user_hold_timeout_seconds))) returning * into h;
 update public.browser_agent_jobs set status = 'waiting_help' where id = j.id;
 insert into public.assistant_browser_chat_outbox(job_id, admission_id, user_id, conversation_id, event_key, payload)
 values(j.id, j.assistant_admission_id, j.principal_user_id, j.assistant_conversation_id, 'hold:' || h.id::text,
  jsonb_build_object('hold_id', h.id, 'kind', h.kind, 'out_of_scope', h.out_of_scope)) on conflict do nothing;
 return jsonb_build_object('ok', true, 'created', true, 'hold', to_jsonb(h) - 'release_token');
end $$;

-- Hold expiry (6.5, 6.6), one place for every caller (decide, release poll,
-- sweep): the hold ends, its help state parks the job for
-- user_park_max_minutes and the chat outbox hears about it. The caller holds
-- the owner lock and the job row; an already ended hold is returned as is.
create or replace function public.assistant_browser_hold_expire(p_hold_id uuid)
returns public.assistant_browser_holds language plpgsql security invoker set search_path = pg_catalog, public as $$
declare h public.assistant_browser_holds; j public.browser_agent_jobs; cfg public.browser_agent_settings;
begin
 update public.assistant_browser_holds set status = 'expired', release_token = null
  where id = p_hold_id and status = 'open' and expires_at <= now() returning * into h;
 if h.id is null then
  select * into h from public.assistant_browser_holds where id = p_hold_id;
  return h;
 end if;
 select * into cfg from public.browser_agent_settings where id = 1;
 select * into j from public.browser_agent_jobs where id = h.job_id;
 update public.assistant_browser_help_state set expires_at = now() + make_interval(mins => cfg.user_park_max_minutes)
  where id = h.help_id and status = 'open';
 insert into public.assistant_browser_chat_outbox(job_id, admission_id, user_id, conversation_id, event_key, payload)
 values(h.job_id, j.assistant_admission_id, h.user_id, j.assistant_conversation_id, 'hold_expired:' || h.id::text,
  jsonb_build_object('hold_id', h.id, 'kind', h.kind)) on conflict do nothing;
 return h;
end $$;

create or replace function public.assistant_browser_hold_decide(p_user_id uuid, p_hold_id uuid, p_decision text,
 p_token text default null, p_token_exp timestamptz default null)
returns jsonb language plpgsql security invoker set search_path = pg_catalog, public as $$
declare h public.assistant_browser_holds; j public.browser_agent_jobs;
begin
 if p_decision not in ('approve','approve_site_only','decline') then return '{"ok":false,"code":"invalid"}'::jsonb; end if;
 if not public.assistant_browser_owner_eligible(p_user_id, true) then return '{"ok":false,"code":"forbidden"}'::jsonb; end if;
 perform pg_advisory_xact_lock(hashtextextended('assistant-browser-owner:' || p_user_id::text, 0));
 select * into h from public.assistant_browser_holds where id = p_hold_id and user_id = p_user_id for update;
 if h.id is null then return '{"ok":false,"code":"not_found"}'::jsonb; end if;
 if h.status = 'aborted' then return '{"ok":false,"code":"hold_aborted"}'::jsonb; end if;
 if h.status <> 'open' then return '{"ok":false,"code":"hold_changed"}'::jsonb; end if;
 select * into j from public.browser_agent_jobs where id = h.job_id for update;
 if j.status not in ('running','waiting_help') or j.lease_epoch is distinct from h.lease_epoch or j.lease_owner is null then
  update public.assistant_browser_holds set status = 'aborted' where id = h.id;
  return '{"ok":false,"code":"hold_aborted"}'::jsonb;
 end if;
 if h.expires_at <= now() then
  perform public.assistant_browser_hold_expire(h.id);
  return '{"ok":false,"code":"hold_expired"}'::jsonb;
 end if;
 if p_decision = 'approve_site_only' and h.kind <> 'site' then return '{"ok":false,"code":"invalid"}'::jsonb; end if;
 -- Only a final action is released by a signed token; a site hold is
 -- released by scope extension (5.4) and carries none.
 if p_decision <> 'decline' and h.kind = 'final_action' and (length(coalesce(p_token, '')) not between 16 and 4096
  or p_token_exp is null or p_token_exp <= now() or p_token_exp > now() + interval '5 minutes') then
  return '{"ok":false,"code":"invalid"}'::jsonb;
 end if;
 update public.assistant_browser_holds set status = case when p_decision = 'decline' then 'declined' else 'approved' end,
  decision = p_decision, decided_by = p_user_id, decided_at = now(),
  release_token = case when p_decision <> 'decline' and h.kind = 'final_action' then p_token end,
  release_exp = case when p_decision <> 'decline' and h.kind = 'final_action' then p_token_exp end
  where id = h.id returning * into h;
 update public.assistant_browser_help_state set status = 'resolved' where id = h.help_id and status = 'open';
 update public.browser_agent_jobs set status = 'running',
  side_effects_at = case when h.kind = 'final_action' then coalesce(side_effects_at, now()) else side_effects_at end where id = j.id;
 return jsonb_build_object('ok', true, 'hold', to_jsonb(h) - 'release_token');
end $$;

create or replace function public.assistant_browser_hold_release_get(p_job uuid, p_worker uuid, p_boot uuid, p_epoch bigint, p_hold_id uuid)
returns jsonb language plpgsql security invoker set search_path = pg_catalog, public as $$
declare j public.browser_agent_jobs; h public.assistant_browser_holds;
begin
 j := public.assistant_browser_user_fence_v2(p_job, p_worker, p_boot, p_epoch);
 select * into h from public.assistant_browser_holds where id = p_hold_id and job_id = j.id for update;
 if h.id is null then return '{"ok":false,"code":"not_found"}'::jsonb; end if;
 if h.status = 'open' and h.expires_at <= now() then h := public.assistant_browser_hold_expire(h.id); end if;
 return jsonb_build_object('ok', true, 'status', case when h.status = 'open' then 'pending' else h.status end, 'decision', h.decision,
  'kind', h.kind,
  'token', case when h.kind = 'final_action' and h.status = 'approved' and h.release_exp > now() then h.release_token end,
  'exp', case when h.kind = 'final_action' and h.status = 'approved' then h.release_exp end);
end $$;

create or replace function public.assistant_browser_hold_consume(p_job uuid, p_worker uuid, p_boot uuid, p_epoch bigint, p_hold_id uuid)
returns jsonb language plpgsql security invoker set search_path = pg_catalog, public as $$
declare j public.browser_agent_jobs; h public.assistant_browser_holds;
begin
 j := public.assistant_browser_user_fence_v2(p_job, p_worker, p_boot, p_epoch);
 select * into h from public.assistant_browser_holds where id = p_hold_id and job_id = j.id for update;
 if h.id is null then return '{"ok":false,"code":"not_found"}'::jsonb; end if;
 if h.status = 'consumed' then return '{"ok":false,"code":"already_consumed"}'::jsonb; end if;
 if h.status <> 'approved' then return '{"ok":false,"code":"hold_changed"}'::jsonb; end if;
 if h.release_exp <= now() or h.lease_epoch is distinct from p_epoch then return '{"ok":false,"code":"token_expired"}'::jsonb; end if;
 update public.assistant_browser_holds set status = 'consumed', consumed_at = now(), release_token = null where id = h.id;
 update public.browser_agent_jobs set side_effects_at = coalesce(side_effects_at, now()) where id = j.id;
 return '{"ok":true}'::jsonb;
end $$;

-- A4.3 Turn begin v2 (12.1): the user task cap from settings, a stepped
-- reservation topped up against the user and company day caps, and the org
-- browser daily total shared with the RFI agent. No least(2, ...).
create or replace function public.assistant_browser_turn_begin_v2(p_user_id uuid, p_job uuid, p_worker uuid, p_boot uuid, p_epoch bigint,
 p_turn_no int, p_estimated_cost numeric, p_estimated_tokens int, p_user_cap_default numeric default 15, p_global_cap_default numeric default 50)
returns jsonb language plpgsql security invoker set search_path = pg_catalog, public as $$
declare j public.browser_agent_jobs; t public.assistant_browser_turns; typ public.browser_agent_types; cfg public.browser_agent_settings;
 caps jsonb; res public.assistant_spend_reservations; v_cap numeric; v_step numeric; v_state jsonb;
begin
 j := public.assistant_browser_user_fence_v2(p_job, p_worker, p_boot, p_epoch);
 if j.principal_user_id is distinct from p_user_id then return '{"ok":false,"code":"forbidden"}'::jsonb; end if;
 select * into t from public.assistant_browser_turns where job_id = j.id and turn_no = p_turn_no;
 if found then return jsonb_build_object('ok', true, 'claimed', false, 'turn_id', t.id, 'token', null,
  'code', case when t.status = 'begun' then 'thinking' else t.status end, 'replay_envelope', t.response_envelope, 'job', to_jsonb(j)); end if;
 if p_turn_no is null or p_turn_no <> j.turn_count + 1 or p_estimated_cost is null or p_estimated_cost < 0 or p_estimated_tokens is null
  or p_estimated_tokens <= 0 or exists(select 1 from public.assistant_browser_turns where job_id = j.id and status in ('begun','uncertain')) then
  return '{"ok":false,"code":"turn_conflict"}'::jsonb;
 end if;
 select * into typ from public.browser_agent_types where key = 'user_browser' for share;
 if typ.provider is distinct from 'openai' or typ.model is distinct from j.input#>>'{authority,model}' or j.turn_count >= typ.max_steps
  or j.active_seconds >= typ.max_active_seconds or j.tokens_in + p_estimated_tokens > typ.max_total_tokens then
  return '{"ok":false,"code":"type_unavailable"}'::jsonb;
 end if;
 select * into cfg from public.browser_agent_settings where id = 1;
 v_cap := least(cfg.user_per_job_cost_cap_usd, coalesce((j.input#>>'{authority,max_cost_usd}')::numeric, cfg.user_per_job_cost_cap_usd));
 if j.cost_usd + p_estimated_cost > v_cap then return '{"ok":false,"code":"cost_cap","reason":"task_cap"}'::jsonb; end if;
 perform pg_advisory_xact_lock(hashtext('assistant-browser-model-spend'));
 caps := public.browser_agent_spend_today();
 if (caps->>'spend_usd')::numeric + p_estimated_cost > (caps->>'daily_cap_usd')::numeric then
  return '{"ok":false,"code":"cost_cap","reason":"org_daily_cap"}'::jsonb;
 end if;
 select * into res from public.assistant_spend_reservations where id = j.assistant_reservation_id for update;
 if res.id is null or res.status <> 'active' or res.expires_at <= now() then return '{"ok":false,"code":"cost_cap","reason":"reservation_lapsed"}'::jsonb; end if;
 while j.cost_usd + p_estimated_cost > res.amount_usd loop
  v_step := least(cfg.user_task_reserve_step_usd, v_cap - res.amount_usd);
  if v_step <= 0 then return '{"ok":false,"code":"cost_cap","reason":"task_cap"}'::jsonb; end if;
  perform pg_advisory_xact_lock(hashtext('assistant_spend'));
  v_state := public.assistant_spend_state(p_user_id, p_user_cap_default, p_global_cap_default);
  if (v_state->>'user_spent')::numeric + (v_state->>'user_held')::numeric + v_step >= (v_state->>'user_cap')::numeric then
   return '{"ok":false,"code":"cost_cap","reason":"user_cap"}'::jsonb;
  end if;
  if (v_state->>'global_spent')::numeric + (v_state->>'global_held')::numeric + v_step >= (v_state->>'global_cap')::numeric then
   return '{"ok":false,"code":"cost_cap","reason":"global_cap"}'::jsonb;
  end if;
  update public.assistant_spend_reservations set amount_usd = amount_usd + v_step where id = res.id returning * into res;
 end loop;
 insert into public.assistant_browser_turns(user_id, job_id, epoch, turn_no, estimated_cost, estimated_tokens)
 values(p_user_id, j.id, p_epoch, p_turn_no, p_estimated_cost, p_estimated_tokens) returning * into t;
 update public.browser_agent_jobs set cost_usd = cost_usd + p_estimated_cost, tokens_in = tokens_in + p_estimated_tokens,
  turn_count = turn_count + 1 where id = j.id returning * into j;
 return jsonb_build_object('ok', true, 'claimed', true, 'turn_id', t.id, 'token', t.token, 'replay_envelope', null, 'job', to_jsonb(j),
  'type', to_jsonb(typ), 'reserved_usd', res.amount_usd);
end $$;

-- A4.4 Requeue v2 (12.3): a job with released actions is never re-run.
create or replace function public.assistant_browser_job_requeue_v2(p_job uuid, p_worker uuid, p_boot uuid, p_epoch bigint,
 p_retry_at timestamptz default null, p_reason text default 'interrupted')
returns jsonb language plpgsql security invoker set search_path = pg_catalog, public as $$
declare j public.browser_agent_jobs;
begin
 j := public.assistant_browser_user_fence_v2(p_job, p_worker, p_boot, p_epoch);
 update public.assistant_browser_holds set status = 'aborted' where job_id = j.id and status = 'open';
 update public.assistant_browser_help_state set status = 'canceled' where job_id = j.id and status = 'open' and reason_code = 'hold';
 if j.side_effects_at is not null then
  update public.browser_agent_jobs set status = 'failed', error_kind = 'interrupted_after_action', finished_at = now(),
   lease_owner = null, lease_boot_id = null, lease_expires_at = null where id = j.id;
 elsif exists(select 1 from public.assistant_browser_turns where job_id = j.id and status = 'begun') then
  update public.browser_agent_jobs set status = 'failed', error_kind = 'turn_uncertain', finished_at = now(),
   lease_owner = null, lease_boot_id = null, lease_expires_at = null where id = j.id;
 else
  update public.browser_agent_jobs set status = case when exists(select 1 from public.assistant_browser_help_state where job_id = j.id
    and status = 'open' and expires_at > now()) then 'waiting_help' else 'queued' end,
   resume_reason = case when p_reason in ('login_spacing','provider_outage') then p_reason else 'interrupted' end,
   next_attempt_at = least(expires_at, greatest(now() + interval '30 seconds', coalesce(p_retry_at, now()))),
   lease_owner = null, lease_boot_id = null, lease_expires_at = null where id = j.id;
 end if;
 return '{"ok":true}'::jsonb;
end $$;
create or replace function public.assistant_browser_job_requeue(p_job uuid,p_worker uuid,p_boot uuid,p_epoch bigint,p_retry_at timestamptz default null,p_reason text default 'interrupted')
returns jsonb language plpgsql security invoker set search_path=pg_catalog,public as $$
declare j public.browser_agent_jobs;
begin
 if exists(select 1 from public.browser_agent_jobs where id=p_job and agent_type='user_browser') then
  return public.assistant_browser_job_requeue_v2(p_job,p_worker,p_boot,p_epoch,p_retry_at,p_reason);
 end if;
 j:=public.assistant_browser_user_fence(p_job,p_worker,p_boot,p_epoch);
 if exists(select 1 from public.assistant_browser_turns where job_id=j.id and status='begun') then
  update public.browser_agent_jobs set status='failed',error_kind='turn_uncertain',finished_at=now(),lease_owner=null,lease_boot_id=null,lease_expires_at=null where id=j.id;
 else
  update public.browser_agent_jobs set status=case when exists(select 1 from public.assistant_browser_help_state where job_id=j.id and status='open' and expires_at>now()) then 'waiting_help' else 'queued' end,
   resume_reason=case when p_reason in ('login_spacing','provider_outage') then p_reason else 'interrupted' end,
   next_attempt_at=least(expires_at,greatest(now()+interval '30 seconds',coalesce(p_retry_at,now()))),
   lease_owner=null,lease_boot_id=null,lease_expires_at=null where id=j.id;
 end if;
 return '{"ok":true}'::jsonb;
end $$;

revoke all on function public.assistant_browser_help_open_v2(uuid, uuid, uuid, bigint, uuid, jsonb, text, uuid, uuid) from public;
revoke all on function public.assistant_browser_help_open_v2(uuid, uuid, uuid, bigint, uuid, jsonb, text, uuid, uuid) from anon, authenticated;
grant execute on function public.assistant_browser_help_open_v2(uuid, uuid, uuid, bigint, uuid, jsonb, text, uuid, uuid) to service_role;
revoke all on function public.assistant_browser_help_reply_v2(uuid, uuid, uuid, uuid, text, jsonb) from public;
revoke all on function public.assistant_browser_help_reply_v2(uuid, uuid, uuid, uuid, text, jsonb) from anon, authenticated;
grant execute on function public.assistant_browser_help_reply_v2(uuid, uuid, uuid, uuid, text, jsonb) to service_role;
revoke all on function public.assistant_browser_help_consume_v2(uuid, uuid, uuid, bigint, uuid, uuid, uuid) from public;
revoke all on function public.assistant_browser_help_consume_v2(uuid, uuid, uuid, bigint, uuid, uuid, uuid) from anon, authenticated;
grant execute on function public.assistant_browser_help_consume_v2(uuid, uuid, uuid, bigint, uuid, uuid, uuid) to service_role;
revoke all on function public.assistant_browser_help_secret_from_mail(uuid, uuid, uuid, text, jsonb) from public;
revoke all on function public.assistant_browser_help_secret_from_mail(uuid, uuid, uuid, text, jsonb) from anon, authenticated;
grant execute on function public.assistant_browser_help_secret_from_mail(uuid, uuid, uuid, text, jsonb) to service_role;
revoke all on function public.assistant_browser_hold_open(uuid, uuid, uuid, bigint, uuid, text, jsonb, jsonb, jsonb) from public;
revoke all on function public.assistant_browser_hold_open(uuid, uuid, uuid, bigint, uuid, text, jsonb, jsonb, jsonb) from anon, authenticated;
grant execute on function public.assistant_browser_hold_open(uuid, uuid, uuid, bigint, uuid, text, jsonb, jsonb, jsonb) to service_role;
revoke all on function public.assistant_browser_hold_expire(uuid) from public;
revoke all on function public.assistant_browser_hold_expire(uuid) from anon, authenticated;
grant execute on function public.assistant_browser_hold_expire(uuid) to service_role;
revoke all on function public.assistant_browser_hold_decide(uuid, uuid, text, text, timestamptz) from public;
revoke all on function public.assistant_browser_hold_decide(uuid, uuid, text, text, timestamptz) from anon, authenticated;
grant execute on function public.assistant_browser_hold_decide(uuid, uuid, text, text, timestamptz) to service_role;
revoke all on function public.assistant_browser_hold_release_get(uuid, uuid, uuid, bigint, uuid) from public;
revoke all on function public.assistant_browser_hold_release_get(uuid, uuid, uuid, bigint, uuid) from anon, authenticated;
grant execute on function public.assistant_browser_hold_release_get(uuid, uuid, uuid, bigint, uuid) to service_role;
revoke all on function public.assistant_browser_hold_consume(uuid, uuid, uuid, bigint, uuid) from public;
revoke all on function public.assistant_browser_hold_consume(uuid, uuid, uuid, bigint, uuid) from anon, authenticated;
grant execute on function public.assistant_browser_hold_consume(uuid, uuid, uuid, bigint, uuid) to service_role;
revoke all on function public.assistant_browser_turn_begin_v2(uuid, uuid, uuid, uuid, bigint, int, numeric, int, numeric, numeric) from public;
revoke all on function public.assistant_browser_turn_begin_v2(uuid, uuid, uuid, uuid, bigint, int, numeric, int, numeric, numeric) from anon, authenticated;
grant execute on function public.assistant_browser_turn_begin_v2(uuid, uuid, uuid, uuid, bigint, int, numeric, int, numeric, numeric) to service_role;
revoke all on function public.assistant_browser_job_requeue_v2(uuid, uuid, uuid, bigint, timestamptz, text) from public;
revoke all on function public.assistant_browser_job_requeue_v2(uuid, uuid, uuid, bigint, timestamptz, text) from anon, authenticated;
grant execute on function public.assistant_browser_job_requeue_v2(uuid, uuid, uuid, bigint, timestamptz, text) to service_role;
revoke all on function public.assistant_browser_job_requeue(uuid, uuid, uuid, bigint, timestamptz, text) from public;
revoke all on function public.assistant_browser_job_requeue(uuid, uuid, uuid, bigint, timestamptz, text) from anon, authenticated;
grant execute on function public.assistant_browser_job_requeue(uuid, uuid, uuid, bigint, timestamptz, text) to service_role;

notify pgrst, 'reload schema';

-- ==== PART A5 ====

-- A5.1 Sweep v2 (6.6, 12.3): expired holds park the job, a parked job ends
-- waiting_timeout after the park limit, lapsed leases never requeue a job
-- with released actions, admissions and requests expire. Row removal of
-- sessions and live frames lives in part B (assistant_browser_purge_v2).
create or replace function public.assistant_browser_sweep_v2(p_limit int default 100)
returns jsonb language plpgsql security invoker set search_path = pg_catalog, public as $$
declare candidate record; j public.browser_agent_jobs; v_hold uuid; n int := 0; v_adm int; v_req int;
begin
 -- Lock order as 0187 to 0189: owner lock, then the job, then its holds and
 -- help rows. Expired holds park their job here (assistant_browser_hold_expire).
 for candidate in select id, principal_user_id from public.browser_agent_jobs where agent_type = 'user_browser'
  and status in ('queued','running','waiting_help') and (expires_at <= now() or cancel_requested_at is not null
   or (lease_owner is not null and lease_expires_at <= now()) or exists(select 1 from public.assistant_browser_help_state x
    where x.job_id = browser_agent_jobs.id and x.status = 'open' and x.expires_at <= now())
   or exists(select 1 from public.assistant_browser_holds hx where hx.job_id = browser_agent_jobs.id and hx.status = 'open'
    and hx.expires_at <= now()))
  order by created_at limit greatest(1, least(p_limit, 100)) loop
  perform pg_advisory_xact_lock(hashtextextended('assistant-browser-owner:' || candidate.principal_user_id::text, 0));
  select * into j from public.browser_agent_jobs where id = candidate.id for update skip locked;
  if j.id is null or j.status not in ('queued','running','waiting_help') then continue; end if;
  for v_hold in select hx.id from public.assistant_browser_holds hx where hx.job_id = j.id and hx.status = 'open'
   and hx.expires_at <= now() for update loop
   perform public.assistant_browser_hold_expire(v_hold);
  end loop;
  update public.assistant_browser_holds set status = 'aborted' where job_id = j.id and status = 'open';
  if j.expires_at <= now() or j.cancel_requested_at is not null or exists(select 1 from public.assistant_browser_help_state x
   where x.job_id = j.id and x.status = 'open' and x.expires_at <= now())
   or exists(select 1 from public.assistant_browser_turns where job_id = j.id and status = 'begun')
   or (j.side_effects_at is not null and j.lease_owner is not null and j.lease_expires_at <= now()) then
   update public.browser_agent_jobs set status = case when expires_at <= now() then 'expired' when cancel_requested_at is not null then 'canceled'
     else 'failed' end,
    error_kind = case when expires_at <= now() then 'offline_expired' when cancel_requested_at is not null then null
     when exists(select 1 from public.assistant_browser_help_state x where x.job_id = j.id and x.status = 'open' and x.expires_at <= now())
      then 'waiting_timeout'
     when side_effects_at is not null then 'interrupted_after_action' else 'execution_interrupted' end,
    finished_at = now(), lease_owner = null, lease_boot_id = null, lease_expires_at = null, hold_until = null where id = j.id;
  elsif j.lease_owner is not null and j.lease_expires_at <= now() then
   update public.browser_agent_jobs set status = 'queued', resume_reason = 'interrupted', lease_owner = null, lease_boot_id = null,
    lease_expires_at = null where id = j.id;
  end if;
  n := n + 1;
 end loop;
 -- Open help rows of an active job are the candidate loop's (park, then
 -- waiting_timeout); only rows left on an ended job are closed here.
 update public.assistant_browser_help_state x set status = 'canceled' from public.browser_agent_jobs bj
  where bj.id = x.job_id and bj.agent_type = 'user_browser' and x.status = 'open' and x.expires_at <= now()
   and bj.status not in ('queued','running','waiting_help','applying');
 update public.assistant_browser_admissions set status = 'expired', updated_at = now()
  where (status like 'waiting_%' or status = 'ready') and expires_at <= now();
 get diagnostics v_adm = row_count;
 update public.assistant_browser_requests set status = 'expired' where status = 'requested' and expires_at <= now();
 get diagnostics v_req = row_count;
 return jsonb_build_object('processed', n, 'admissions_expired', v_adm, 'requests_expired', v_req);
end $$;

-- A5.2 Owner safe rules (6.3), downloads (11.2), live sign-in open (9), outbox (4.5).
create or replace function public.assistant_browser_safe_rule_add(p_user_id uuid, p_hold_id uuid, p_method text, p_host text,
 p_path_pattern text, p_graphql_operation text default null)
returns jsonb language plpgsql security invoker set search_path = pg_catalog, public as $$
declare h public.assistant_browser_holds; v_site text; r public.assistant_browser_user_safe_rules;
begin
 perform pg_advisory_xact_lock(hashtextextended('assistant-browser-owner:' || p_user_id::text, 0));
 select * into h from public.assistant_browser_holds where id = p_hold_id and user_id = p_user_id;
 if h.id is null or h.kind <> 'final_action' or h.out_of_scope or h.method is distinct from p_method then
  return '{"ok":false,"code":"invalid"}'::jsonb;
 end if;
 if lower(coalesce(p_path_pattern, '') || ' ' || coalesce(p_graphql_operation, ''))
  ~ '(submit|send|accept|declin|delet|remov|pay|approv|answer|respond|sign|award|confirm)' then
  return '{"ok":false,"code":"final_verb"}'::jsonb;
 end if;
 select s.site_key into v_site from public.browser_agent_jobs j join public.assistant_browser_admissions a on a.id = j.assistant_admission_id
  cross join lateral jsonb_array_elements(a.site_scope) e join public.assistant_browser_sites s on s.site_key = e->>'site_key'
  where j.id = h.job_id and public.assistant_browser_v2_site_matches(s, 'https://' || lower(p_host)) limit 1;
 if v_site is null then return '{"ok":false,"code":"out_of_scope"}'::jsonb; end if;
 insert into public.assistant_browser_user_safe_rules(user_id, site_key, method, host, path_pattern, graphql_operation, source_hold_id)
 values(p_user_id, v_site, p_method, lower(p_host), p_path_pattern, p_graphql_operation, h.id) on conflict do nothing returning * into r;
 insert into public.assistant_browser_profile_suggestions(user_id, site_key, rule)
 values(p_user_id, v_site, jsonb_build_object('method', p_method, 'host', lower(p_host), 'path_regex', p_path_pattern,
  'graphql_operations', case when p_graphql_operation is null then '[]'::jsonb else jsonb_build_array(p_graphql_operation) end));
 return jsonb_build_object('ok', true, 'rule', to_jsonb(r));
exception when check_violation then return '{"ok":false,"code":"invalid"}'::jsonb;
end $$;

create or replace function public.assistant_browser_download_record(p_job uuid, p_worker uuid, p_boot uuid, p_epoch bigint, p_record jsonb)
returns jsonb language plpgsql security invoker set search_path = pg_catalog, public as $$
declare j public.browser_agent_jobs; d public.assistant_browser_downloads;
begin
 j := public.assistant_browser_user_fence_v2(p_job, p_worker, p_boot, p_epoch);
 if (p_record->>'project_id')::uuid is distinct from j.project_id and p_record->>'status' = 'stored' then
  return '{"ok":false,"code":"project_mismatch"}'::jsonb;
 end if;
 insert into public.assistant_browser_downloads(user_id, job_id, project_id, file_id, staged_id, origin, filename, sha256, size_bytes,
  sniffed_type, scan_verdict, status, reason, storage_path, expires_at)
 values(j.principal_user_id, j.id, (p_record->>'project_id')::uuid, (p_record->>'file_id')::uuid, (p_record->>'staged_id')::uuid,
  p_record->>'origin', p_record->>'filename', p_record->>'sha256', (p_record->>'size_bytes')::bigint, p_record->>'sniffed_type',
  p_record->>'scan_verdict', p_record->>'status', p_record->>'reason', p_record->>'storage_path',
  (p_record->>'expires_at')::timestamptz)
 on conflict (job_id, sha256) do nothing returning * into d;
 return jsonb_build_object('ok', true, 'created', d.id is not null, 'download', to_jsonb(d));
exception when invalid_text_representation or check_violation or not_null_violation then return '{"ok":false,"code":"invalid"}'::jsonb;
end $$;

create or replace function public.assistant_browser_live_session_open(p_user_id uuid, p_job_id uuid, p_site_key text, p_account_label text)
returns jsonb language plpgsql security invoker set search_path = pg_catalog, public as $$
declare j public.browser_agent_jobs; l public.assistant_browser_live_sessions;
begin
 if not public.assistant_browser_owner_eligible(p_user_id, true) then return '{"ok":false,"code":"forbidden"}'::jsonb; end if;
 perform pg_advisory_xact_lock(hashtextextended('assistant-browser-owner:' || p_user_id::text, 0));
 select * into j from public.browser_agent_jobs where id = p_job_id and principal_user_id = p_user_id and agent_type = 'user_browser' for update;
 if j.id is null or j.status not in ('running','waiting_help') or j.lease_owner is null then return '{"ok":false,"code":"job_changed"}'::jsonb; end if;
 if not exists(select 1 from public.assistant_browser_admissions a where a.id = j.assistant_admission_id
  and a.site_scope @> jsonb_build_array(jsonb_build_object('site_key', p_site_key))) then return '{"ok":false,"code":"out_of_scope"}'::jsonb; end if;
 select * into l from public.assistant_browser_live_sessions where job_id = j.id and status = 'open';
 if l.id is not null then return jsonb_build_object('ok', true, 'created', false, 'session', to_jsonb(l)); end if;
 insert into public.assistant_browser_live_sessions(user_id, job_id, site_key, account_label)
 values(p_user_id, j.id, p_site_key, p_account_label) returning * into l;
 update public.browser_agent_jobs set control_mode = 'operator' where id = j.id;
 return jsonb_build_object('ok', true, 'created', true, 'session', to_jsonb(l));
exception when check_violation then return '{"ok":false,"code":"invalid"}'::jsonb;
end $$;

create or replace function public.assistant_browser_outbox_drain(p_limit int default 50)
returns jsonb language plpgsql security invoker set search_path = pg_catalog, public as $$
declare v_rows jsonb;
begin
 with picked as (select id from public.assistant_browser_chat_outbox where drained_at is null
   and (claimed_at is null or claimed_at < now() - interval '5 minutes') order by id
   for update skip locked limit greatest(1, least(p_limit, 200))),
  claimed as (update public.assistant_browser_chat_outbox o set claimed_at = now(), attempts = attempts + 1
   from picked p where o.id = p.id returning o.*)
 select coalesce(jsonb_agg(to_jsonb(c) order by c.id), '[]'::jsonb) into v_rows from claimed c;
 return jsonb_build_object('rows', v_rows);
end $$;
create or replace function public.assistant_browser_outbox_ack(p_ids bigint[])
returns int language plpgsql security invoker set search_path = pg_catalog, public as $$
declare n int;
begin
 update public.assistant_browser_chat_outbox set drained_at = now() where id = any(p_ids) and drained_at is null;
 get diagnostics n = row_count;
 return n;
end $$;

-- A5.3 Claim rewrite (12.3), same signature: user_browser only, several
-- leases per worker below max_slots (waiting jobs keep their lease and so
-- count), the type limit, the per-owner limit, fair order by the owner's
-- latest start, authority from grants and job credentials.
create or replace function public.assistant_browser_claim_user(p_worker uuid,p_boot uuid,p_capacity jsonb,p_lease_seconds int default 90)
returns setof public.browser_agent_jobs language plpgsql security invoker set search_path=pg_catalog,public as $$
declare w public.browser_agent_workers; typ public.browser_agent_types; cfg public.browser_agent_settings; candidate record;
 j public.browser_agent_jobs; v_want int; v_taken int := 0; v_code text;
begin
 select * into w from public.browser_agent_workers where id = p_worker for share;
 if w.id is null or w.principal_kind <> 'user' or w.status <> 'active' or w.boot_id is distinct from p_boot then raise exception 'lease_lost'; end if;
 if w.draining or not public.assistant_browser_runtime_ready() then return; end if;
 select * into cfg from public.browser_agent_settings where id = 1 and enabled;
 if not found then return; end if;
 select * into typ from public.browser_agent_types where key = 'user_browser' and principal_kind = 'user' and enabled and provider = 'openai';
 if not found or not (typ.key = any(w.allowed_types)) or not (coalesce(w.capabilities->'types', '{}'::jsonb) ? typ.key) then return; end if;
 v_want := least(greatest(coalesce((p_capacity->>typ.key)::int, 0), 0), 8);
 if v_want < 1 then return; end if;
 perform pg_advisory_xact_lock(hashtext('bagent:' || typ.key));
 for candidate in select q.id, q.principal_user_id from public.browser_agent_jobs q where q.agent_type = typ.key and q.principal_kind = 'user'
  and q.status = 'queued' and q.cancel_requested_at is null and q.expires_at > now() and coalesce(q.next_attempt_at, now()) <= now()
  order by (select max(o.started_at) from public.browser_agent_jobs o where o.principal_user_id = q.principal_user_id
   and o.agent_type = typ.key) nulls first, q.priority, q.created_at limit 50 loop
  exit when v_taken >= v_want;
  exit when (select count(*) from public.browser_agent_jobs where lease_owner = w.id) >= w.max_slots;
  exit when (select count(*) from public.browser_agent_jobs where agent_type = typ.key and lease_owner is not null) >= typ.max_concurrency;
  continue when (select count(*) from public.browser_agent_jobs where principal_user_id = candidate.principal_user_id
   and agent_type = typ.key and lease_owner is not null) >= cfg.user_per_owner_concurrency;
  continue when not public.assistant_browser_owner_eligible(candidate.principal_user_id, true);
  perform pg_advisory_xact_lock(hashtextextended('assistant-browser-owner:' || candidate.principal_user_id::text, 0));
  select * into j from public.browser_agent_jobs where id = candidate.id and status = 'queued' and expires_at > now()
   and cancel_requested_at is null for update skip locked;
  continue when j.id is null;
  continue when (select count(*) from public.browser_agent_jobs where principal_user_id = j.principal_user_id
   and agent_type = typ.key and lease_owner is not null) >= cfg.user_per_owner_concurrency;
  -- Authority that no longer holds never comes back for this job: end it so
  -- the terminal trigger releases the reservation and writes the outbox row.
  v_code := public.assistant_browser_v2_authority(j);
  if v_code is not null then
   update public.browser_agent_jobs set status = 'canceled', error_kind = v_code, error = 'Browser authorization changed.',
    cancel_requested_at = now(), finished_at = now() where id = j.id;
   continue;
  end if;
  if j.attempts >= typ.max_attempts or exists(select 1 from public.assistant_browser_turns where job_id = j.id and status in ('begun','uncertain')) then
   update public.browser_agent_jobs set status = 'failed', error_kind = 'execution_interrupted', finished_at = now() where id = j.id;
   continue;
  end if;
  update public.browser_agent_jobs set status = 'running', lease_owner = w.id, lease_boot_id = p_boot, lease_epoch = lease_epoch + 1,
   lease_expires_at = least(expires_at, now() + make_interval(secs => least(90, greatest(30, p_lease_seconds)))), attempts = attempts + 1,
   started_at = coalesce(started_at, now()), resume_reason = null, next_attempt_at = null,
   config_snapshot = jsonb_build_object('type', to_jsonb(typ) - array['updated_by','updated_at'],
    'per_job_cost_cap_usd', cfg.user_per_job_cost_cap_usd, 'daily_cost_cap_usd', cfg.daily_cost_cap_usd,
    'config_version', cfg.config_version, 'policy_version', j.policy_version, 'claimed_at', now())
   where id = j.id returning * into j;
  v_taken := v_taken + 1;
  return next j;
 end loop;
end $$;

-- A5.4 Terminal trigger (4.5): v2 jobs also write the chat outbox row and
-- abort open holds, card requests and live sessions in the same transaction.
create or replace function public.assistant_browser_terminal()
returns trigger language plpgsql security invoker set search_path=pg_catalog,public as $$
begin
 if new.principal_kind<>'user' or new.status not in ('succeeded','failed','canceled','expired')
  or old.status in ('succeeded','failed','canceled','expired') then return new; end if;
 update public.assistant_browser_turns set status='uncertain',finished_at=now() where job_id=new.id and status='begun';
 update public.assistant_spend_reservations set status=case when new.status='succeeded' then 'settled' else 'released' end,
  actual_usd=new.cost_usd,settled_at=now() where id=new.assistant_reservation_id and user_id=new.principal_user_id and status='active';
 update public.assistant_browser_help_state set status='canceled' where job_id=new.id and status='open';
 update public.assistant_browser_records set envelope=null,consumed_at=coalesce(consumed_at,now()) where job_id=new.id and kind='help_secret' and consumed_at is null;
 update public.assistant_credentials set login_attempt_id=null,login_job_id=null,login_epoch=null,login_attempt_expires_at=null where login_job_id=new.id;
 if new.agent_type='user_browser' then
  update public.assistant_browser_holds set status='aborted',release_token=null where job_id=new.id and status in ('open','approved');
  update public.assistant_browser_requests set status='aborted' where job_id=new.id and status='requested';
  update public.assistant_browser_live_sessions set status='ended',ended_at=now(),outcome='job_ended' where job_id=new.id and status='open';
  update public.browser_agent_jobs set control_mode='agent' where id=new.id and control_mode<>'agent';
  insert into public.assistant_browser_chat_outbox(job_id,admission_id,user_id,conversation_id,event_key,payload)
  values(new.id,new.assistant_admission_id,new.principal_user_id,new.assistant_conversation_id,'terminal',
   jsonb_build_object('status',new.status,'error_kind',new.error_kind,'side_effects',new.side_effects_at is not null)) on conflict do nothing;
 end if;
 perform public.assistant_browser_notice(new.id,'terminal','assistant.browser_task',case when new.status='expired' then
 'Your browser task expired while waiting for a worker.' when new.status='succeeded' then 'Your browser task finished.' else 'Your browser task stopped.' end);
 return new;
end $$;
-- A5.5 IT Admin inspection (10.7), replaced in place (0187 signature): held
-- request bodies and final-action screenshots are owner-only, so the records
-- a hold points at are never returned, beside the help secrets.
create or replace function public.assistant_browser_inspect(p_viewer_id uuid,p_user_id uuid,p_job_id uuid,p_reason text,p_kind text default 'all')
returns jsonb language plpgsql security invoker set search_path=pg_catalog,public as $$
declare v_label text;v_audit uuid;v_rows jsonb;
begin
 perform 1 from public.profiles where id=p_viewer_id and is_active=true and role::text='it_admin' for share;
 if not found or length(btrim(coalesce(p_reason,''))) not between 10 and 500 then return '{"ok":false,"code":"forbidden"}'::jsonb; end if;
 perform 1 from public.browser_agent_jobs where id=p_job_id and principal_kind='user' and principal_user_id=p_user_id;
 if not found then return '{"ok":false,"code":"not_found"}'::jsonb; end if;
 select coalesce(full_name,'Former user') into v_label from public.profiles where id=p_user_id;
 insert into public.assistant_browser_access(user_id,user_label,viewer_id,resource_kind,resource_id,reason)
 values(p_user_id,coalesce(v_label,'Former user'),p_viewer_id,'job',p_job_id,btrim(p_reason)) returning id into v_audit;
 select coalesce(jsonb_agg(to_jsonb(x)),'[]'::jsonb) into v_rows from
 (select r.* from public.assistant_browser_records r where r.user_id=p_user_id and r.job_id=p_job_id and r.kind<>'help_secret'
  and (p_kind='all' or r.kind=p_kind)
  and not exists(select 1 from public.assistant_browser_holds h where h.job_id=r.job_id
   and r.id in (h.summary_record_id, h.screenshot_record_id))
  order by r.created_at desc,r.id desc limit 100) x;
 return jsonb_build_object('ok',true,'audit_id',v_audit,'records',v_rows);
end $$;

revoke all on function public.assistant_browser_sweep_v2(int) from public;
revoke all on function public.assistant_browser_sweep_v2(int) from anon, authenticated;
grant execute on function public.assistant_browser_sweep_v2(int) to service_role;
revoke all on function public.assistant_browser_safe_rule_add(uuid, uuid, text, text, text, text) from public;
revoke all on function public.assistant_browser_safe_rule_add(uuid, uuid, text, text, text, text) from anon, authenticated;
grant execute on function public.assistant_browser_safe_rule_add(uuid, uuid, text, text, text, text) to service_role;
revoke all on function public.assistant_browser_download_record(uuid, uuid, uuid, bigint, jsonb) from public;
revoke all on function public.assistant_browser_download_record(uuid, uuid, uuid, bigint, jsonb) from anon, authenticated;
grant execute on function public.assistant_browser_download_record(uuid, uuid, uuid, bigint, jsonb) to service_role;
revoke all on function public.assistant_browser_live_session_open(uuid, uuid, text, text) from public;
revoke all on function public.assistant_browser_live_session_open(uuid, uuid, text, text) from anon, authenticated;
grant execute on function public.assistant_browser_live_session_open(uuid, uuid, text, text) to service_role;
revoke all on function public.assistant_browser_outbox_drain(int) from public;
revoke all on function public.assistant_browser_outbox_drain(int) from anon, authenticated;
grant execute on function public.assistant_browser_outbox_drain(int) to service_role;
revoke all on function public.assistant_browser_outbox_ack(bigint[]) from public;
revoke all on function public.assistant_browser_outbox_ack(bigint[]) from anon, authenticated;
grant execute on function public.assistant_browser_outbox_ack(bigint[]) to service_role;
revoke all on function public.assistant_browser_claim_user(uuid, uuid, jsonb, int) from public;
revoke all on function public.assistant_browser_claim_user(uuid, uuid, jsonb, int) from anon, authenticated;
grant execute on function public.assistant_browser_claim_user(uuid, uuid, jsonb, int) to service_role;
revoke all on function public.assistant_browser_terminal() from public;
revoke all on function public.assistant_browser_terminal() from anon, authenticated;
grant execute on function public.assistant_browser_terminal() to service_role;
revoke all on function public.assistant_browser_inspect(uuid, uuid, uuid, text, text) from public;
revoke all on function public.assistant_browser_inspect(uuid, uuid, uuid, text, text) from anon, authenticated;
grant execute on function public.assistant_browser_inspect(uuid, uuid, uuid, text, text) to service_role;

-- A5.6 Curated code email regexes (review fix, WI-14). The email code
-- reader trusts a curated site's mail only through its template regex,
-- anchored at the start of the code email's own subject
-- (the reader matches subject + newline + body), so user-written text in
-- the same platform's signed notifications can never supply the code.
-- Verify the subjects in the WI-20 live probes. SmartBid has no MFA: no regex.
update public.assistant_browser_sites set code_email_regex =
 '(?is)\A\W{0,3}(?:procore\W{1,3})?(?:your\s+)?(?:procore\s+)?(?:verification|security|one[- ]time|sign[- ]in|login|authentication)\s+(?:code|passcode)\b.{0,300}?(?<![A-Za-z0-9])([0-9]{6})(?![A-Za-z0-9])'
 where site_key = 'procore' and code_email_regex is null;
update public.assistant_browser_sites set code_email_regex =
 '(?is)\A\W{0,3}(?:autodesk\W{1,3})?(?:your\s+)?(?:autodesk\s+)?(?:verification|security|one[- ]time|sign[- ]in|login|authentication)\s+(?:code|passcode)\b.{0,300}?(?<![A-Za-z0-9])([0-9]{6})(?![A-Za-z0-9])'
 where site_key in ('autodesk', 'buildingconnected') and code_email_regex is null;

notify pgrst, 'reload schema';

-- ==== PART B ====
-- Destructive text, owner at the screen. Constraint swaps run inside DO blocks
-- that find the generated names in pg_constraint by table, type and
-- definition, and raise unless exactly one matches (a re-run after the swap
-- finds the replacement and skips).

-- B1. Credentials keyed by site and account label (7.2); service is legacy.
-- The unique (user_id, service) constraint stays: the live v1 credential
-- upsert (0189) targets it, and v2 rows carry a null service, which never
-- collides.
do $$ declare v_names text[]; begin
 if not exists(select 1 from pg_constraint where conname = 'assistant_credentials_site_account_v2') then
  select array_agg(conname) into v_names from pg_constraint where conrelid = 'public.assistant_credentials'::regclass
   and contype = 'c' and pg_get_constraintdef(oid) like '%service%' and pg_get_constraintdef(oid) like '%procore%';
  if cardinality(coalesce(v_names, '{}')) <> 1 then raise exception 'assistant_credentials service check not found exactly once'; end if;
  execute format('alter table public.assistant_credentials drop constraint %I', v_names[1]);
  alter table public.assistant_credentials alter column service drop not null;
  alter table public.assistant_credentials add constraint assistant_credentials_site_account_v2
   check (site_key is not null or service = 'procore');
 end if;
end $$;

-- B2. Job shape: user jobs carry exactly one of an approval (v1) or an
-- admission (v2); credential and version may be null together.
do $$ declare v_names text[]; begin
 if not exists(select 1 from pg_constraint where conname = 'browser_agent_jobs_user_shape_v2') then
  select array_agg(conname) into v_names from pg_constraint where conrelid = 'public.browser_agent_jobs'::regclass
   and contype = 'c' and conname = 'browser_agent_jobs_user_shape';
  if cardinality(coalesce(v_names, '{}')) <> 1 then raise exception 'browser_agent_jobs_user_shape not found exactly once'; end if;
  execute format('alter table public.browser_agent_jobs drop constraint %I', v_names[1]);
  alter table public.browser_agent_jobs add constraint browser_agent_jobs_user_shape_v2 check (
   (principal_kind = 'org' and principal_user_id is null and user_credential_id is null and user_credential_version is null
    and assistant_approval_id is null and assistant_admission_id is null and assistant_conversation_id is null
    and assistant_reservation_id is null)
   or (principal_kind = 'user' and principal_user_id is not null and assistant_conversation_id is not null
    and assistant_reservation_id is not null and ((assistant_approval_id is not null) <> (assistant_admission_id is not null))
    and ((user_credential_id is null) = (user_credential_version is null))
    and (user_credential_version is null or user_credential_version > 0)));
 end if;
end $$;

-- B3. control_mode gains operator (live sign-in), for user jobs only.
do $$ declare v_names text[]; begin
 if not exists(select 1 from pg_constraint where conname = 'browser_agent_jobs_control_mode_v2') then
  select array_agg(conname) into v_names from pg_constraint where conrelid = 'public.browser_agent_jobs'::regclass
   and contype = 'c' and pg_get_constraintdef(oid) like '%control_mode%';
  if cardinality(coalesce(v_names, '{}')) <> 1 then raise exception 'browser_agent_jobs control_mode check not found exactly once'; end if;
  execute format('alter table public.browser_agent_jobs drop constraint %I', v_names[1]);
  alter table public.browser_agent_jobs add constraint browser_agent_jobs_control_mode_v2
   check (control_mode in ('agent','operator') and (control_mode = 'agent' or principal_kind = 'user'));
 end if;
end $$;

-- B4. Sessions without a password credential (live sign-in) and several
-- accounts per site; v2 upserts on assistant_browser_sessions_site_account_uq (A1).
-- The unique (credential_id, origin) constraint stays for the live v1 session
-- upsert (0186); a live sign-in row has a null credential, which never collides.
do $$ begin
 if not exists(select 1 from pg_constraint where conname = 'assistant_browser_sessions_owner_v2') then
  alter table public.assistant_browser_sessions alter column credential_id drop not null;
  alter table public.assistant_browser_sessions add constraint assistant_browser_sessions_owner_v2
   check (credential_id is not null or (site_key is not null and account_label is not null));
 end if;
end $$;

-- B5. Help reasons: the 14 existing plus five for the personal browser (8.1).
do $$ declare v_names text[]; begin
 if not exists(select 1 from pg_constraint where conname = 'browser_agent_help_requests_reason_v2') then
  select array_agg(conname) into v_names from pg_constraint where conrelid = 'public.browser_agent_help_requests'::regclass
   and contype = 'c' and pg_get_constraintdef(oid) like '%reason_code%';
  if cardinality(coalesce(v_names, '{}')) <> 1 then raise exception 'browser_agent_help_requests reason_code check not found exactly once'; end if;
  execute format('alter table public.browser_agent_help_requests drop constraint %I', v_names[1]);
  alter table public.browser_agent_help_requests add constraint browser_agent_help_requests_reason_v2 check (reason_code in
   ('login_failed','login_locked','captcha','mfa','sso','logged_out','access_denied','not_found','ambiguous','unexpected_page',
    'cap_reached','needs_info','provider_config','other','needs_credential','needs_site','mfa_step_up','push','hold'));
 end if;
end $$;

-- B6. Site revoke (5.2): one transaction revokes the grant, removes the
-- user's sessions for the site and cancels queued jobs that include it;
-- running jobs stop at their next fence (scope_revoked).
create or replace function public.assistant_browser_site_revoke(p_user_id uuid, p_grant_id uuid, p_expected_version int)
returns jsonb language plpgsql security invoker set search_path = pg_catalog, public as $$
declare g public.assistant_browser_site_grants; n int := 0; s int;
begin
 perform pg_advisory_xact_lock(hashtextextended('assistant-browser-owner:' || p_user_id::text, 0));
 select * into g from public.assistant_browser_site_grants where id = p_grant_id and user_id = p_user_id for update;
 if g.id is null then return '{"ok":false,"code":"not_found"}'::jsonb; end if;
 if g.version is distinct from p_expected_version or g.status <> 'active' then return '{"ok":false,"code":"version_conflict"}'::jsonb; end if;
 update public.assistant_browser_site_grants set status = 'revoked', version = version + 1, revoked_at = now(), updated_at = now()
  where id = g.id returning * into g;
 delete from public.assistant_browser_sessions where user_id = p_user_id and site_key = g.site_key;
 get diagnostics s = row_count;
 update public.browser_agent_jobs j set status = 'canceled', error_kind = 'scope_revoked', error = 'Site approval revoked.',
  cancel_requested_at = now(), finished_at = now()
  from public.assistant_browser_admissions a where a.id = j.assistant_admission_id and j.principal_user_id = p_user_id
   and j.status = 'queued' and a.site_scope @> jsonb_build_array(jsonb_build_object('site_key', g.site_key));
 get diagnostics n = row_count;
 update public.browser_agent_jobs j set policy_version = policy_version + 1
  from public.assistant_browser_admissions a where a.id = j.assistant_admission_id and j.principal_user_id = p_user_id
   and j.status in ('running','waiting_help') and a.site_scope @> jsonb_build_array(jsonb_build_object('site_key', g.site_key));
 return jsonb_build_object('ok', true, 'version', g.version, 'canceled_jobs', n, 'sessions_removed', s);
end $$;

-- Settings: sign out of one saved session; remove one owner safe rule.
create or replace function public.assistant_browser_session_signout(p_user_id uuid, p_session_id uuid)
returns jsonb language plpgsql security invoker set search_path = pg_catalog, public as $$
declare n int;
begin
 perform pg_advisory_xact_lock(hashtextextended('assistant-browser-owner:' || p_user_id::text, 0));
 delete from public.assistant_browser_sessions where id = p_session_id and user_id = p_user_id;
 get diagnostics n = row_count;
 return jsonb_build_object('ok', n = 1, 'code', case when n = 1 then null else 'not_found' end);
end $$;
create or replace function public.assistant_browser_safe_rule_remove(p_user_id uuid, p_rule_id uuid)
returns jsonb language plpgsql security invoker set search_path = pg_catalog, public as $$
declare n int;
begin
 delete from public.assistant_browser_user_safe_rules where id = p_rule_id and user_id = p_user_id;
 get diagnostics n = row_count;
 return jsonb_build_object('ok', n = 1, 'code', case when n = 1 then null else 'not_found' end);
end $$;

-- Live sign-in close (9 step 6): keep only the site's session, capped by
-- user_live_session_max_days; frames and input for the session are removed.
create or replace function public.assistant_browser_live_session_close(p_user_id uuid, p_session_id uuid, p_outcome text,
 p_session jsonb default null)
returns jsonb language plpgsql security invoker set search_path = pg_catalog, public as $$
declare l public.assistant_browser_live_sessions; cfg public.browser_agent_settings; s public.assistant_browser_sessions;
begin
 if p_outcome not in ('signed_in','stopped','idle','failed') then return '{"ok":false,"code":"invalid"}'::jsonb; end if;
 perform pg_advisory_xact_lock(hashtextextended('assistant-browser-owner:' || p_user_id::text, 0));
 select * into l from public.assistant_browser_live_sessions where id = p_session_id and user_id = p_user_id for update;
 if l.id is null or l.status <> 'open' then return '{"ok":false,"code":"session_changed"}'::jsonb; end if;
 if p_outcome = 'signed_in' and p_session is not null then
  select * into cfg from public.browser_agent_settings where id = 1;
  if coalesce(p_session->>'origin', '') !~ '^https://' or not public.assistant_browser_scope_allows(l.job_id, p_session->>'origin')
   or not public.assistant_browser_record_valid(jsonb_build_object('id', p_session->>'id', 'kind', 'snapshot', 'envelope', p_session->'envelope')) then
   return '{"ok":false,"code":"session_invalid"}'::jsonb;
  end if;
  insert into public.assistant_browser_sessions(id, user_id, credential_id, credential_version, origin, version, wrapped_dek, ciphertext,
   nonce, key_id, expires_at, site_key, account_label, source)
  values((p_session->>'id')::uuid, p_user_id, null, 1, p_session->>'origin', 1, p_session#>>'{envelope,wrapped_dek}',
   p_session#>>'{envelope,ciphertext}', p_session#>>'{envelope,nonce}', p_session#>>'{envelope,key_id}',
   least(coalesce((p_session->>'expires_at')::timestamptz, now() + make_interval(days => cfg.user_live_session_max_days)),
    now() + make_interval(days => cfg.user_live_session_max_days)), l.site_key, l.account_label, 'live_signin')
  on conflict (user_id, site_key, account_label, origin) do update set credential_id = null, credential_version = 1,
   version = assistant_browser_sessions.version + 1, wrapped_dek = excluded.wrapped_dek, ciphertext = excluded.ciphertext,
   nonce = excluded.nonce, key_id = excluded.key_id, expires_at = excluded.expires_at, source = 'live_signin', updated_at = now()
  returning * into s;
 end if;
 update public.assistant_browser_live_sessions set status = 'ended', ended_at = now(), outcome = p_outcome where id = l.id;
 update public.browser_agent_jobs set control_mode = 'agent' where id = l.job_id and control_mode <> 'agent';
 delete from public.assistant_browser_live_frames where session_id = l.id;
 delete from public.assistant_browser_live_input where session_id = l.id;
 return jsonb_build_object('ok', true, 'session', case when s.id is null then null else to_jsonb(s) - array['wrapped_dek','ciphertext','nonce'] end);
exception when invalid_text_representation then return '{"ok":false,"code":"session_invalid"}'::jsonb;
end $$;

-- Purge (runs next to assistant_browser_sweep_v2): expired cookie jars,
-- live frames and input older than 30 seconds, idle live sessions.
create or replace function public.assistant_browser_purge_v2(p_limit int default 1000)
returns jsonb language plpgsql security invoker set search_path = pg_catalog, public as $$
declare v_sessions int; v_frames int; v_input int; v_idle int;
begin
 with expired as (select id from public.assistant_browser_sessions where expires_at <= now() order by expires_at
  for update skip locked limit greatest(1, least(p_limit, 1000)))
 delete from public.assistant_browser_sessions s using expired e where s.id = e.id;
 get diagnostics v_sessions = row_count;
 update public.assistant_browser_live_sessions set status = 'ended', ended_at = now(), outcome = 'idle'
  where status = 'open' and coalesce(last_input_at, started_at) < now() - interval '10 minutes';
 get diagnostics v_idle = row_count;
 update public.browser_agent_jobs j set control_mode = 'agent' where j.control_mode = 'operator'
  and not exists(select 1 from public.assistant_browser_live_sessions l where l.job_id = j.id and l.status = 'open');
 delete from public.assistant_browser_live_frames f where f.created_at < now() - interval '30 seconds'
  or not exists(select 1 from public.assistant_browser_live_sessions l where l.id = f.session_id and l.status = 'open');
 get diagnostics v_frames = row_count;
 delete from public.assistant_browser_live_input i where i.created_at < now() - interval '30 seconds'
  or not exists(select 1 from public.assistant_browser_live_sessions l where l.id = i.session_id and l.status = 'open');
 get diagnostics v_input = row_count;
 return jsonb_build_object('sessions', v_sessions, 'live_idle', v_idle, 'frames', v_frames, 'input', v_input);
end $$;

-- Offboarding (14.2 item 6): the 0186 owner revoke plus grants, admissions,
-- card requests, live sessions and the owner's safe rules. Jobs, holds and
-- sessions follow through assistant_browser_cancel_credentials and the
-- terminal trigger.
create or replace function public.assistant_browser_revoke_owner(p_user_id uuid)
returns int language plpgsql security invoker set search_path=pg_catalog,public as $$
declare n int;
begin
 perform pg_advisory_xact_lock(hashtextextended('assistant-browser-owner:'||p_user_id::text,0));
 perform public.assistant_browser_cancel_credentials(p_user_id,null);
 update public.assistant_credentials set status='revoked',version=version+1,wrapped_dek=null,ciphertext=null,nonce=null,key_id=null,
 is_default=false,login_attempt_id=null,login_job_id=null,login_epoch=null,login_attempt_expires_at=null,updated_at=now()
 where user_id=p_user_id and status<>'revoked';
 get diagnostics n=row_count;
 update public.assistant_browser_site_grants set status='revoked',version=version+1,revoked_at=now(),updated_at=now()
  where user_id=p_user_id and status='active';
 update public.assistant_browser_admissions set status='declined',updated_at=now()
  where user_id=p_user_id and (status like 'waiting_%' or status='ready');
 update public.assistant_browser_requests set status='cancelled',decided_at=now() where user_id=p_user_id and status='requested';
 update public.assistant_browser_holds set status='aborted',release_token=null where user_id=p_user_id and status in ('open','approved');
 update public.assistant_browser_live_sessions set status='ended',ended_at=now(),outcome='stopped' where user_id=p_user_id and status='open';
 delete from public.assistant_browser_user_safe_rules where user_id=p_user_id;
 delete from public.assistant_browser_sessions where user_id=p_user_id;
 return n;
end $$;

-- The 0187 sweep leaves v2 jobs to assistant_browser_sweep_v2, so a lapsed
-- lease never requeues a job with released actions.
create or replace function public.assistant_browser_sweep(p_limit int default 100)
returns jsonb language plpgsql security invoker set search_path=pg_catalog,public as $$
declare candidate record;j public.browser_agent_jobs;n int:=0;
begin
 for candidate in select id,principal_user_id from public.browser_agent_jobs where principal_kind='user' and agent_type<>'user_browser' and status in ('queued','running','waiting_help')
 and (expires_at<=now() or cancel_requested_at is not null or (lease_owner is not null and lease_expires_at<=now()) or exists(select 1 from public.assistant_browser_help_state h where h.job_id=browser_agent_jobs.id and h.status='open' and h.expires_at<=now())) order by created_at limit greatest(1,least(p_limit,100)) loop
  perform pg_advisory_xact_lock(hashtextextended('assistant-browser-owner:'||candidate.principal_user_id::text,0));
  select * into j from public.browser_agent_jobs where id=candidate.id for update skip locked;
  if j.id is null or j.status not in ('queued','running','waiting_help') then continue; end if;
  if j.expires_at<=now() or j.cancel_requested_at is not null or exists(select 1 from public.assistant_browser_help_state h where h.job_id=j.id and h.status='open' and h.expires_at<=now()) or exists(select 1 from public.assistant_browser_turns where job_id=j.id and status='begun') then
   update public.browser_agent_jobs set status=case when expires_at<=now() then 'expired' when cancel_requested_at is not null then 'canceled' else 'failed' end,
    error_kind=case when expires_at<=now() then 'offline_expired' else 'execution_interrupted' end,finished_at=now(),
    lease_owner=null,lease_boot_id=null,lease_expires_at=null,hold_until=null where id=j.id;
  elsif j.lease_owner is not null and j.lease_expires_at<=now() then
   update public.browser_agent_jobs set status='queued',resume_reason='interrupted',lease_owner=null,lease_boot_id=null,lease_expires_at=null where id=j.id;
  end if;
  n:=n+1;
 end loop;
 -- Expired one-time codes are destroyed even while audit content is on hold.
 -- v2 help rows (park and waiting_timeout) belong to assistant_browser_sweep_v2.
 update public.assistant_browser_help_state h set status='canceled' where h.status='open' and h.expires_at<=now()
 and not exists(select 1 from public.browser_agent_jobs bj where bj.id=h.job_id and bj.agent_type='user_browser');
 update public.assistant_browser_records r set envelope=null,consumed_at=now() where r.kind='help_secret' and r.consumed_at is null
 and exists(select 1 from public.assistant_browser_help_state h where h.id=r.help_id and (h.status<>'open' or h.expires_at<=now()));
 -- Cookie jars are authentication material, never subject to trace legal hold.
 with expired as(select id from public.assistant_browser_sessions where expires_at<=now()
  order by expires_at for update skip locked limit greatest(1,least(p_limit,1000)))
 delete from public.assistant_browser_sessions s using expired e where s.id=e.id;
 return jsonb_build_object('processed',n);
end $$;
revoke all on function public.assistant_browser_site_revoke(uuid, uuid, int) from public;
revoke all on function public.assistant_browser_site_revoke(uuid, uuid, int) from anon, authenticated;
grant execute on function public.assistant_browser_site_revoke(uuid, uuid, int) to service_role;
revoke all on function public.assistant_browser_session_signout(uuid, uuid) from public;
revoke all on function public.assistant_browser_session_signout(uuid, uuid) from anon, authenticated;
grant execute on function public.assistant_browser_session_signout(uuid, uuid) to service_role;
revoke all on function public.assistant_browser_safe_rule_remove(uuid, uuid) from public;
revoke all on function public.assistant_browser_safe_rule_remove(uuid, uuid) from anon, authenticated;
grant execute on function public.assistant_browser_safe_rule_remove(uuid, uuid) to service_role;
revoke all on function public.assistant_browser_live_session_close(uuid, uuid, text, jsonb) from public;
revoke all on function public.assistant_browser_live_session_close(uuid, uuid, text, jsonb) from anon, authenticated;
grant execute on function public.assistant_browser_live_session_close(uuid, uuid, text, jsonb) to service_role;
revoke all on function public.assistant_browser_purge_v2(int) from public;
revoke all on function public.assistant_browser_purge_v2(int) from anon, authenticated;
grant execute on function public.assistant_browser_purge_v2(int) to service_role;
revoke all on function public.assistant_browser_revoke_owner(uuid) from public;
revoke all on function public.assistant_browser_revoke_owner(uuid) from anon, authenticated;
grant execute on function public.assistant_browser_revoke_owner(uuid) to service_role;
revoke all on function public.assistant_browser_sweep(int) from public;
revoke all on function public.assistant_browser_sweep(int) from anon, authenticated;
grant execute on function public.assistant_browser_sweep(int) to service_role;

-- B7. Ledger effect tier 'server_gated' (plan 4.3, WI-12): the browser tools
-- whose authority check is in the server route run inline under a begun
-- ledger row. Same swap as 0176 C, under a new name.
do $$ declare v_names text[]; begin
 if not exists(select 1 from pg_constraint where conname = 'assistant_tool_calls_effect_v2') then
  select array_agg(conname) into v_names from pg_constraint where conrelid = 'public.assistant_tool_calls'::regclass
   and contype = 'c' and pg_get_constraintdef(oid) like '%sensitive_read%';
  if cardinality(coalesce(v_names, '{}')) <> 1 then raise exception 'assistant_tool_calls effect check not found exactly once'; end if;
  execute format('alter table public.assistant_tool_calls drop constraint %I', v_names[1]);
  alter table public.assistant_tool_calls add constraint assistant_tool_calls_effect_v2 check (effect in
   ('read','sensitive_read','write_internal','external_send','expensive','housekeeping','server_gated'));
 end if;
end $$;

notify pgrst, 'reload schema';
