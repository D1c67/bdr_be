-- 0170: Assistant core (docs/ASSISTANT.md, Data model, "0170, assistant core"). Apply after 0158 and after the 0163-0169 AI Estimating block. DEV ONLY until the release.
--
-- Every internal user gets one assistant. It is one more client of the
-- FastAPI app: tools are real HTTP routes called in-process under a bdra_
-- principal, so nothing in this file grants the assistant any data access.
-- These tables hold the assistant's own housekeeping: profiles and settings,
-- the delegation grant, conversations and messages, the append-only tool
-- call ledger, approval cards, memory, schedules, tasks, staged files, the
-- proactive outbox and inbox, the IT Admin conversation view log and the
-- eval lock.
--
-- Migration ordering. Prod has 0001-0153 and 0159-0162. 0154 re-creates
-- llm_jobs_job_type_check with its own full list (0154:70-71), as 0152 did
-- (0152:32-33). 0154 to 0158 are applied to prod before 0170, with the
-- owner's explicit approval at release time. Defensively, section 9 widens
-- the constraint by reading pg_get_constraintdef and appending the assistant
-- types, so an out-of-order apply cannot drop them, and it never removes a
-- type that is already present. scripts/check_llm_jobs_constraint_relists.py
-- fails when a third migration re-lists the constraint with a full list.
--
-- Conventions (docs/ASSISTANT.md, Data model):
--   * uuid primary keys (the two singletons, assistant_settings and
--     assistant_eval_lock, use id = 1 like browser_agent_settings in 0157).
--   * RLS enabled and forced with no policies; every privilege on every
--     table revoked from anon and authenticated. The service-role backend is
--     the only client (0007, 0093).
--   * Rows private to a person: user_id references profiles(id) on delete
--     cascade. Ledgers that must survive the person (assistant_tool_calls,
--     assistant_approvals, assistant_schedule_runs,
--     assistant_conversation_views) store the person as a plain uuid plus a
--     label, with no FK, like the immutable tables in 0157.
--   * Actor FKs are on delete set null (0012). project_id never has an FK
--     here (0141: a project delete must never be blocked or cascade into the
--     assistant's history).
--   * assistant_refuse_change() is this migration's own append-only guard.
--     It is never browser_agent_refuse_change (0157:662), which prod does
--     not have.
--   * Every function is SECURITY INVOKER with search_path pinned to
--     pg_catalog, public (0145) and is executable by service_role only.
--   * Every statement is idempotent. Triggers use create or replace (Postgres
--     14+) and the todos constraint is guarded by a pg_constraint lookup, so
--     the file contains no drop statement outside the llm_jobs widening block:
--     the Supabase MCP connector rejects migrations with top-level drops.
--     Applied to dev 2026-10-22 in three parts (tables / functions and
--     triggers / alters and grants) with identical content.
--
--   1. Settings, profiles, delegations
--   2. Conversations, messages, out-of-line tool results
--   3. Tool call ledger and approvals
--   4. Memory
--   5. Schedules, tasks, staged files
--   6. Proactive outbox, subscriptions, inbox
--   7. IT Admin conversation views, eval lock
--   8. Functions and triggers
--   9. Changes to existing tables
--  10. RLS and grants

-- 1. Settings, profiles, delegations ----------------------------------------------

-- Global switches. The mailbox exclusion list is the MAILBOX_EXCLUSIONS
-- setting, not a column, so the PM Emails scoping and read_mailbox read one
-- source.
create table if not exists public.assistant_settings (
  id                    smallint primary key default 1 check (id = 1),
  enabled               boolean not null default false,                     -- kill switch
  model                 text check (model ~ '^[A-Za-z0-9._:/-]{1,120}$'),
  global_daily_cap_usd  numeric(10,2) not null default 50 check (global_daily_cap_usd between 0 and 10000),
  disabled_tools        text[] not null default '{}'::text[],
  holidays              date[] not null default '{}'::date[],               -- G3 holiday list for "weekday" schedules
  updated_by            uuid references public.profiles(id) on delete set null,
  updated_at            timestamptz not null default now()
);
insert into public.assistant_settings (id) values (1) on conflict do nothing;

-- One row per person. display_name names the panel only (null renders
-- "<first word of full_name>'s assistant"); records always derive the label.
create table if not exists public.assistant_profiles (
  user_id             uuid primary key references public.profiles(id) on delete cascade,
  display_name        text check (display_name is null or length(display_name) between 1 and 60),
  enabled             boolean not null default true,                        -- IT Admin editable
  timezone            text not null default 'America/Los_Angeles',
  daily_cost_cap_usd  numeric(10,2) not null default 5 check (daily_cost_cap_usd between 0 and 1000),
  quiet_hours         jsonb,                                                -- {"start":"18:00","end":"06:00"}; null = defaults
  calendar_mode       text not null default 'context' check (calendar_mode in ('context', 'all')),
  legal_hold          boolean not null default false,                       -- suspends every purge for this user
  created_at          timestamptz not null default now(),
  updated_at          timestamptz not null default now()
);

-- The delegation grant. Created under the user's own aal2 session; schedules,
-- continuations and approval executions run only while an unrevoked row exists.
create table if not exists public.assistant_delegations (
  id                   uuid primary key default gen_random_uuid(),
  user_id              uuid not null references public.profiles(id) on delete cascade,
  created_session_aal  text not null check (created_session_aal = 'aal2'),
  created_at           timestamptz not null default now(),
  expires_at           timestamptz,
  revoked_at           timestamptz,
  revoked_reason       text,
  constraint assistant_delegations_revoked_reason_chk check (revoked_at is null or revoked_reason is not null)
);
-- At most one live grant per person; a new grant revokes the old one first.
create unique index if not exists assistant_delegations_live_uq
  on public.assistant_delegations (user_id) where revoked_at is null;

-- 2. Conversations, messages, out-of-line tool results -----------------------------

-- tainted_at and mailbox_tainted_at are set the first time any message in
-- the conversation is tainted and are never cleared (trigger in section 8).
create table if not exists public.assistant_conversations (
  id                  uuid primary key default gen_random_uuid(),
  user_id             uuid not null references public.profiles(id) on delete cascade,
  title               text,
  surface             text not null default 'panel' check (surface in ('panel', 'page', 'schedule', 'task')),
  mode                text not null default 'normal' check (mode in ('normal', 'mailbox_review')),
  project_id          uuid,                                                 -- page context, no FK (0141)
  tainted_at          timestamptz,
  mailbox_tainted_at  timestamptz,
  last_message_at     timestamptz,
  archived_at         timestamptz,
  created_at          timestamptz not null default now(),
  updated_at          timestamptz not null default now()
);
create index if not exists assistant_conversations_user_idx
  on public.assistant_conversations (user_id, last_message_at desc nulls last);

-- blocks holds the replayable transcript items (reasoning items with
-- encrypted content, function_call and function_call_output) in order.
create table if not exists public.assistant_messages (
  id                uuid primary key default gen_random_uuid(),
  conversation_id   uuid not null references public.assistant_conversations(id) on delete cascade,
  user_id           uuid not null references public.profiles(id) on delete cascade,
  role              text not null check (role in ('user', 'assistant', 'system')),
  content           text,
  blocks            jsonb not null default '[]'::jsonb,
  tool_calls        jsonb not null default '[]'::jsonb,                     -- progress, written after each tool
  context           jsonb,                                                  -- {path, project_id} sent by the panel
  status            text not null default 'done'
    check (status in ('pending', 'running', 'waiting_approval', 'waiting_review', 'done', 'failed')),
  error_kind        text,
  llm_job_id        uuid references public.llm_jobs(id) on delete set null,
  model             text,
  input_tokens      int,
  cached_tokens     int,
  output_tokens     int,
  reasoning_tokens  int,
  cost_usd          numeric(12,6),
  tainted           boolean not null default false,
  mailbox_tainted   boolean not null default false,
  incognito         boolean not null default false,                         -- neither reads nor writes memory
  used_memory_ids   uuid[] not null default '{}'::uuid[],
  created_at        timestamptz not null default now(),
  updated_at        timestamptz not null default now(),
  completed_at      timestamptz
);
create index if not exists assistant_messages_conversation_idx
  on public.assistant_messages (conversation_id, created_at);
create index if not exists assistant_messages_user_idx
  on public.assistant_messages (user_id, created_at desc);
-- One in-flight turn per conversation.
create unique index if not exists assistant_messages_one_active_uq
  on public.assistant_messages (conversation_id) where status in ('pending', 'running');

-- Out-of-line tool payloads (raw mail bodies, large results). Never in blocks.
-- Purged nightly once expires_at passes: 30 days, 7 for cross-mailbox and
-- payroll-aggregate results (the app sets expires_at from retention_class).
create table if not exists public.assistant_tool_results (
  id               uuid primary key default gen_random_uuid(),
  user_id          uuid not null references public.profiles(id) on delete cascade,
  conversation_id  uuid references public.assistant_conversations(id) on delete cascade,
  message_id       uuid references public.assistant_messages(id) on delete cascade,
  tool_call_id     uuid,                                                    -- ledger row, no FK
  tool_name        text not null,
  retention_class  text not null default 'standard'
    check (retention_class in ('standard', 'cross_mailbox', 'payroll_aggregate')),
  tainted          boolean not null default false,
  payload          jsonb not null,
  payload_sha256   text check (payload_sha256 ~ '^[0-9a-f]{64}$'),
  size_bytes       int,
  expires_at       timestamptz not null default (now() + interval '30 days'),
  created_at       timestamptz not null default now()
);
create index if not exists assistant_tool_results_message_idx
  on public.assistant_tool_results (message_id);
create index if not exists assistant_tool_results_expiry_idx
  on public.assistant_tool_results (expires_at);

-- 3. Tool call ledger and approvals -------------------------------------------------

-- Append-only phase ledger. One 'begun' row per step (written by
-- assistant_tool_call_begin) and at most one terminal row ('completed' or
-- 'failed'). A terminal insert must carry the begun row's lease_owner (fence
-- trigger in section 8). A requeued turn treats a step as done only when a
-- completed row exists; a begun-without-completed write is never replayed.
create table if not exists public.assistant_tool_calls (
  id               uuid primary key default gen_random_uuid(),
  user_id          uuid not null,                                           -- plain uuid: ledger survives the person
  user_label       text not null,
  conversation_id  uuid,
  message_id       uuid,
  run_id           uuid not null,
  seq              int not null check (seq >= 0),
  idempotency_key  text not null check (length(idempotency_key) between 8 and 200),
  phase            text not null check (phase in ('begun', 'completed', 'failed')),
  lease_owner      text not null,
  tool_name        text not null,
  tool_version     text,
  effect           text not null
    check (effect in ('read', 'sensitive_read', 'write_internal', 'external_send', 'expensive')),
  sub_app          text,
  method           text check (method in ('GET', 'POST', 'PUT', 'PATCH')),   -- DELETE is refused in the shim
  path_format      text,
  args_redacted    jsonb check (args_redacted is null or octet_length(args_redacted::text) <= 8192),
  payload_sha256   text check (payload_sha256 ~ '^[0-9a-f]{64}$'),
  before_snapshot  jsonb,
  after_snapshot   jsonb,
  result_summary   jsonb,
  ok               boolean,
  error_kind       text,
  project_id       uuid,                                                    -- no FK (0141)
  approval_id      uuid,
  tainted_context  boolean not null default false,
  duration_ms      int,
  created_at       timestamptz not null default now(),
  constraint assistant_tool_calls_key_phase_uq unique (idempotency_key, phase),
  constraint assistant_tool_calls_begun_no_outcome_chk check (phase <> 'begun' or ok is null)
);
create index if not exists assistant_tool_calls_run_idx
  on public.assistant_tool_calls (run_id, seq);
create index if not exists assistant_tool_calls_user_idx
  on public.assistant_tool_calls (user_id, created_at desc);
create index if not exists assistant_tool_calls_project_idx
  on public.assistant_tool_calls (project_id, created_at desc) where project_id is not null;

-- Approval cards. The click is a compare-and-set over (id, status, user_id,
-- expires_at, payload_sha256, precondition); payload, payload_sha256,
-- preview, precondition, user_id and action_kind never change after insert
-- and status only moves forward (assistant_approval_immutable, section 8).
-- precondition is not null so the CAS jsonb equality never meets a null.
-- The route to execute rides inside payload. project_id lets the sweep
-- expire cards whose project was submitted, abandoned or verified.
create table if not exists public.assistant_approvals (
  id                   uuid primary key default gen_random_uuid(),
  user_id              uuid not null,                                       -- plain uuid: ledger survives the person
  user_label           text not null,
  conversation_id      uuid,
  message_id           uuid,
  tool_call_id         uuid,
  project_id           uuid,                                                -- no FK (0141)
  action_kind          text not null,
  payload              jsonb not null,
  payload_sha256       text not null check (payload_sha256 ~ '^[0-9a-f]{64}$'),
  preview              jsonb not null default '{}'::jsonb,
  precondition         jsonb not null default '{}'::jsonb,
  status               text not null default 'pending'
    check (status in ('pending', 'approved', 'rejected', 'expired', 'executing', 'executed', 'failed')),
  expired_reason       text,                                                -- changed_since_preview, feature_off, owner_offboarded, ...
  decided_by           uuid,                                                -- plain uuid, like user_id
  decided_at           timestamptz,
  decided_session_aal  text check (decided_session_aal in ('aal1', 'aal2')),
  execute_job_id       uuid references public.llm_jobs(id) on delete set null,
  executed_at          timestamptz,
  result               jsonb,
  email_log_id         uuid references public.email_log(id) on delete set null,
  expires_at           timestamptz not null,
  superseded_by        uuid,                                                -- the new row minted on changed_since_preview
  created_at           timestamptz not null default now(),
  updated_at           timestamptz not null default now(),
  constraint assistant_approvals_expired_reason_chk check (status <> 'expired' or expired_reason is not null)
);
create index if not exists assistant_approvals_pending_idx
  on public.assistant_approvals (user_id) where status = 'pending';
create index if not exists assistant_approvals_pending_expiry_idx
  on public.assistant_approvals (expires_at) where status = 'pending';
create index if not exists assistant_approvals_pending_project_idx
  on public.assistant_approvals (project_id) where status = 'pending' and project_id is not null;
create index if not exists assistant_approvals_user_idx
  on public.assistant_approvals (user_id, created_at desc);

-- 4. Memory ---------------------------------------------------------------------------

-- Typed facts, soft-retired, user-visible. source and status are derived
-- server-side, never set by the model. Caps by trigger (section 8): 300
-- active, 40 pinned, 6000 pinned characters (title plus body) per person.
create table if not exists public.assistant_memories (
  id               uuid primary key default gen_random_uuid(),
  user_id          uuid not null references public.profiles(id) on delete cascade,
  kind             text not null check (kind in ('preference', 'about_me', 'instruction', 'project_note', 'reference')),
  title            text not null check (length(title) between 1 and 200),
  body             text not null default '' check (length(body) <= 4000),
  project_id       uuid,                                                    -- no FK (0141)
  status           text not null default 'pending_review' check (status in ('active', 'pending_review', 'archived')),
  source           text not null check (source in ('user_stated', 'user_confirmed', 'user_settings', 'inferred')),
  pinned           boolean not null default false,
  origin_tainted   boolean not null default false,
  conversation_id  uuid references public.assistant_conversations(id) on delete set null,
  message_id       uuid references public.assistant_messages(id) on delete set null,
  last_used_at     timestamptz,
  use_count        int not null default 0,
  archived_at      timestamptz,
  created_at       timestamptz not null default now(),
  updated_at       timestamptz not null default now(),
  tsv              tsvector generated always as (
                     to_tsvector('english'::regconfig, coalesce(title, '') || ' ' || coalesce(body, ''))
                   ) stored,
  constraint assistant_memories_project_note_chk check (kind <> 'project_note' or project_id is not null)
);
create index if not exists assistant_memories_user_idx
  on public.assistant_memories (user_id, status, last_used_at desc nulls last);
create index if not exists assistant_memories_project_idx
  on public.assistant_memories (project_id) where project_id is not null;
create index if not exists assistant_memories_tsv_idx
  on public.assistant_memories using gin (tsv);
create index if not exists assistant_memories_title_trgm_idx
  on public.assistant_memories using gin (title gin_trgm_ops);

-- Append-only change history. Deleted only by the cascade from its memory or
-- its person (permanent delete, retention purge, hard delete of a user).
create table if not exists public.assistant_memory_events (
  id               uuid primary key default gen_random_uuid(),
  memory_id        uuid not null references public.assistant_memories(id) on delete cascade,
  user_id          uuid not null references public.profiles(id) on delete cascade,
  event            text not null check (event in ('created', 'updated', 'approved', 'rejected', 'pinned', 'unpinned',
                                                  'archived', 'restored', 'revision_proposed',
                                                  'revision_approved', 'revision_rejected')),
  actor            text not null check (actor in ('user', 'assistant', 'system')),
  payload          jsonb not null default '{}'::jsonb,
  conversation_id  uuid,
  message_id       uuid,
  created_at       timestamptz not null default now()
);
create index if not exists assistant_memory_events_memory_idx
  on public.assistant_memory_events (memory_id, created_at);
create index if not exists assistant_memory_events_user_idx
  on public.assistant_memory_events (user_id, created_at desc);

-- Pending edits to active rows. The old body stays live until the user
-- approves the revision in Settings or through the toast.
create table if not exists public.assistant_memory_revisions (
  id               uuid primary key default gen_random_uuid(),
  memory_id        uuid not null references public.assistant_memories(id) on delete cascade,
  user_id          uuid not null references public.profiles(id) on delete cascade,
  title            text not null check (length(title) between 1 and 200),
  body             text not null default '' check (length(body) <= 4000),
  origin_tainted   boolean not null default false,
  conversation_id  uuid references public.assistant_conversations(id) on delete set null,
  message_id       uuid references public.assistant_messages(id) on delete set null,
  decision         text check (decision in ('approved', 'rejected')),
  created_at       timestamptz not null default now(),
  decided_at       timestamptz,
  constraint assistant_memory_revisions_decided_chk check ((decision is null) = (decided_at is null))
);
create index if not exists assistant_memory_revisions_pending_idx
  on public.assistant_memory_revisions (user_id, memory_id) where decided_at is null;

-- 5. Schedules, tasks, staged files ---------------------------------------------------

-- Recipe-only schedules in phase 3. rrule is an RFC 5545 rule expanded in
-- America/Los_Angeles. Cap 10 active per person and a 1 hour minimum
-- interval, both enforced by trigger (section 8).
create table if not exists public.assistant_schedules (
  id                    uuid primary key default gen_random_uuid(),
  user_id               uuid not null references public.profiles(id) on delete cascade,
  title                 text not null check (length(title) between 1 and 200),
  recipe                text not null check (recipe in ('due_today', 'due_this_week', 'missing_quotes', 'labor_not_entered')),
  params                jsonb not null default '{}'::jsonb,
  rrule                 text not null check (length(rrule) between 10 and 500),
  timezone              text not null default 'America/Los_Angeles',
  status                text not null default 'active' check (status in ('active', 'paused', 'paused_feature_off', 'ended')),
  paused_reason         text,
  sub_app               text,
  delivery              text not null default 'chat_bell' check (delivery in ('chat', 'chat_bell', 'chat_bell_email')),
  only_if_changed       boolean not null default false,
  last_result_hash      text,
  project_id            uuid,                                               -- project watch, no FK (0141)
  conversation_id       uuid references public.assistant_conversations(id) on delete set null,
  next_run_at           timestamptz,
  last_run_at           timestamptz,
  consecutive_failures  int not null default 0,
  unread_deliveries     int not null default 0,
  ended_at              timestamptz,
  created_at            timestamptz not null default now(),
  updated_at            timestamptz not null default now()
);
create index if not exists assistant_schedules_user_idx
  on public.assistant_schedules (user_id, status);
create index if not exists assistant_schedules_due_idx
  on public.assistant_schedules (next_run_at) where status = 'active';

-- One row per occurrence; UNIQUE(schedule_id, scheduled_for) makes a claim
-- by either worker idempotent. Ledger: plain uuids plus a label.
create table if not exists public.assistant_schedule_runs (
  id             uuid primary key default gen_random_uuid(),
  schedule_id    uuid not null,                                             -- no FK: runs outlive the schedule
  user_id        uuid not null,
  user_label     text not null,
  scheduled_for  timestamptz not null,
  status         text not null default 'claimed'
    check (status in ('claimed', 'running', 'delivered', 'no_change', 'skipped', 'failed')),
  claimed_by     text,
  started_at     timestamptz,
  finished_at    timestamptz,
  result_hash    text,
  delivered      jsonb,                                                     -- {"chat":true,"bell":true,"email":"<email_log id>"}
  message_id     uuid,
  error_kind     text,
  error          text,
  created_at     timestamptz not null default now(),
  constraint assistant_schedule_runs_occurrence_uq unique (schedule_id, scheduled_for)
);
create index if not exists assistant_schedule_runs_user_idx
  on public.assistant_schedule_runs (user_id, scheduled_for desc);

-- Tasks: 'split_into_project' waits on a bid_split_jobs row (its
-- assistant_task_id points back here); 'instruction' runs as the owner with
-- an approval card before any write.
create table if not exists public.assistant_tasks (
  id               uuid primary key default gen_random_uuid(),
  user_id          uuid not null references public.profiles(id) on delete cascade,
  kind             text not null check (kind in ('split_into_project', 'instruction')),
  status           text not null default 'pending'
    check (status in ('pending', 'running', 'waiting_external', 'waiting_approval', 'done', 'failed', 'canceled')),
  title            text,
  instruction      text,
  project_id       uuid,                                                    -- no FK (0141)
  conversation_id  uuid references public.assistant_conversations(id) on delete set null,
  message_id       uuid references public.assistant_messages(id) on delete set null,
  split_job_id     uuid references public.bid_split_jobs(id) on delete set null,
  payload          jsonb not null default '{}'::jsonb,
  result           jsonb,
  failure_reason   text,                                                    -- feature_off, filed_whole, ...
  created_by       uuid references public.profiles(id) on delete set null,  -- the assigner
  finished_at      timestamptz,
  created_at       timestamptz not null default now(),
  updated_at       timestamptz not null default now()
);
create index if not exists assistant_tasks_user_idx
  on public.assistant_tasks (user_id, status, created_at desc);
create index if not exists assistant_tasks_waiting_idx
  on public.assistant_tasks (status) where status = 'waiting_external';

-- Staging for files dropped in the panel. Caps: 450 MB per file (check), 10
-- files per message and 2 GB of unconsumed staging per person (trigger).
-- MIME allowlist matches bid_split.check_upload (bid_split.py:349: PDF only).
-- Unconsumed rows are purged after 7 days.
create table if not exists public.assistant_files (
  id                   uuid primary key default gen_random_uuid(),
  user_id              uuid not null references public.profiles(id) on delete cascade,
  conversation_id      uuid references public.assistant_conversations(id) on delete set null,
  message_id           uuid references public.assistant_messages(id) on delete set null,
  filename             text not null check (length(filename) between 1 and 255),
  storage_path         text not null unique,
  size_bytes           bigint not null check (size_bytes > 0 and size_bytes <= 471859200),
  sha256               text not null check (sha256 ~ '^[0-9a-f]{64}$'),
  mime                 text not null check (mime in ('application/pdf')),
  consumed_at          timestamptz,
  consumed_project_id  uuid,                                                -- no FK (0141)
  expires_at           timestamptz not null default (now() + interval '7 days'),
  created_at           timestamptz not null default now()
);
create index if not exists assistant_files_user_idx
  on public.assistant_files (user_id, created_at desc);
create index if not exists assistant_files_message_idx
  on public.assistant_files (message_id) where message_id is not null;
create index if not exists assistant_files_sha_idx
  on public.assistant_files (user_id, sha256);
create index if not exists assistant_files_unconsumed_expiry_idx
  on public.assistant_files (expires_at) where consumed_at is null;

-- 6. Proactive outbox, subscriptions, inbox (schema in phase 3, router in phase 7) ---

create table if not exists public.assistant_events (
  id               uuid primary key default gen_random_uuid(),
  event_type       text not null,
  dedupe_key       text not null unique,
  project_id       uuid,                                                    -- no FK (0141)
  subject_user_id  uuid,
  payload          jsonb not null default '{}'::jsonb,
  occurred_at      timestamptz not null default now(),
  processed_at     timestamptz,
  created_at       timestamptz not null default now()
);
create index if not exists assistant_events_unprocessed_idx
  on public.assistant_events (occurred_at) where processed_at is null;

-- Role defaults (role set, user_id null) and per-person overrides (user_id
-- set, role null).
create table if not exists public.assistant_subscriptions (
  id          uuid primary key default gen_random_uuid(),
  role        text,
  user_id     uuid references public.profiles(id) on delete cascade,
  event_type  text not null,
  enabled     boolean not null default true,
  severity    text not null default 'normal' check (severity in ('low', 'normal', 'high', 'urgent')),
  created_at  timestamptz not null default now(),
  updated_at  timestamptz not null default now(),
  constraint assistant_subscriptions_target_chk check ((role is null) <> (user_id is null))
);
create unique index if not exists assistant_subscriptions_role_uq
  on public.assistant_subscriptions (role, event_type) where user_id is null;
create unique index if not exists assistant_subscriptions_user_uq
  on public.assistant_subscriptions (user_id, event_type) where role is null;

create table if not exists public.assistant_inbox (
  id            uuid primary key default gen_random_uuid(),
  user_id       uuid not null references public.profiles(id) on delete cascade,
  dedupe_key    text not null,
  event_id      uuid references public.assistant_events(id) on delete set null,
  severity      text not null default 'normal' check (severity in ('low', 'normal', 'high', 'urgent')),
  title         text not null,
  why           text,
  payload       jsonb not null default '{}'::jsonb,
  project_id    uuid,                                                       -- no FK (0141)
  delivered_at  timestamptz,
  read_at       timestamptz,
  feedback      text check (feedback in ('useful', 'not_useful', 'mute')),
  feedback_at   timestamptz,
  created_at    timestamptz not null default now(),
  constraint assistant_inbox_dedupe_uq unique (user_id, dedupe_key)
);
create index if not exists assistant_inbox_undelivered_idx
  on public.assistant_inbox (user_id, created_at) where delivered_at is null;

-- 7. IT Admin conversation views, eval lock -------------------------------------------

-- Append-only log of IT Admin reads and exports of another person's
-- conversations. Plain uuids plus labels: it outlives both people.
create table if not exists public.assistant_conversation_views (
  id               uuid primary key default gen_random_uuid(),
  viewer_id        uuid not null,
  viewer_label     text not null,
  owner_id         uuid not null,
  conversation_id  uuid,                                                    -- null = every conversation (export)
  action           text not null default 'read' check (action in ('read', 'export')),
  -- 'legal' lets an export include mailbox_tainted messages (Retention and offboarding).
  reason_kind      text not null default 'other' check (reason_kind in ('support', 'legal', 'offboarding', 'other')),
  reason           text not null check (length(btrim(reason)) between 3 and 1000),
  created_at       timestamptz not null default now()
);
create index if not exists assistant_conversation_views_owner_idx
  on public.assistant_conversation_views (owner_id, created_at desc);
create index if not exists assistant_conversation_views_conversation_idx
  on public.assistant_conversation_views (conversation_id) where conversation_id is not null;

-- One row; the eval harness takes it with a conditional update
-- (holder is null or expires_at < now()) so CI runs never overlap.
create table if not exists public.assistant_eval_lock (
  id           smallint primary key default 1 check (id = 1),
  holder       text,
  run_label    text,
  acquired_at  timestamptz,
  expires_at   timestamptz,
  updated_at   timestamptz not null default now()
);
insert into public.assistant_eval_lock (id) values (1) on conflict do nothing;

-- 8. Functions and triggers -----------------------------------------------------------

-- 8a. updated_at -------------------------------------------------------------------------
create or replace trigger assistant_settings_updated_at before update on public.assistant_settings
  for each row execute function public.set_updated_at();
create or replace trigger assistant_profiles_updated_at before update on public.assistant_profiles
  for each row execute function public.set_updated_at();
create or replace trigger assistant_conversations_updated_at before update on public.assistant_conversations
  for each row execute function public.set_updated_at();
create or replace trigger assistant_messages_updated_at before update on public.assistant_messages
  for each row execute function public.set_updated_at();
create or replace trigger assistant_approvals_updated_at before update on public.assistant_approvals
  for each row execute function public.set_updated_at();
create or replace trigger assistant_memories_updated_at before update on public.assistant_memories
  for each row execute function public.set_updated_at();
create or replace trigger assistant_schedules_updated_at before update on public.assistant_schedules
  for each row execute function public.set_updated_at();
create or replace trigger assistant_tasks_updated_at before update on public.assistant_tasks
  for each row execute function public.set_updated_at();
create or replace trigger assistant_subscriptions_updated_at before update on public.assistant_subscriptions
  for each row execute function public.set_updated_at();
create or replace trigger assistant_eval_lock_updated_at before update on public.assistant_eval_lock
  for each row execute function public.set_updated_at();

-- 8b. Append-only guard --------------------------------------------------------------------
-- Refuses UPDATE, DELETE and TRUNCATE. With the trigger argument
-- 'allow_cascade' a DELETE is let through only when it arrives from an FK
-- cascade (the referential action runs as a trigger, so pg_trigger_depth()
-- is above 1); a direct DELETE is still refused. Without that, the cascade
-- from a profile or memory delete would be blocked.
create or replace function public.assistant_refuse_change()
returns trigger
language plpgsql
security invoker
set search_path = pg_catalog, public
as $$
begin
  if tg_op = 'DELETE' and tg_nargs > 0 and tg_argv[0] = 'allow_cascade' and pg_trigger_depth() > 1 then
    return old;
  end if;
  raise exception '% is append-only (% refused)', tg_table_name, tg_op
    using errcode = 'restrict_violation';
end;
$$;

create or replace trigger assistant_tool_calls_no_update before update on public.assistant_tool_calls
  for each row execute function public.assistant_refuse_change();
create or replace trigger assistant_tool_calls_no_delete before delete on public.assistant_tool_calls
  for each row execute function public.assistant_refuse_change();
create or replace trigger assistant_tool_calls_no_truncate before truncate on public.assistant_tool_calls
  for each statement execute function public.assistant_refuse_change();

create or replace trigger assistant_memory_events_no_update before update on public.assistant_memory_events
  for each row execute function public.assistant_refuse_change();
create or replace trigger assistant_memory_events_no_delete before delete on public.assistant_memory_events
  for each row execute function public.assistant_refuse_change('allow_cascade');
create or replace trigger assistant_memory_events_no_truncate before truncate on public.assistant_memory_events
  for each statement execute function public.assistant_refuse_change();

create or replace trigger assistant_conversation_views_no_update before update on public.assistant_conversation_views
  for each row execute function public.assistant_refuse_change();
create or replace trigger assistant_conversation_views_no_delete before delete on public.assistant_conversation_views
  for each row execute function public.assistant_refuse_change();
create or replace trigger assistant_conversation_views_no_truncate before truncate on public.assistant_conversation_views
  for each statement execute function public.assistant_refuse_change();

-- 8c. Tool call fence --------------------------------------------------------------------
-- A terminal row needs the begun row, the same lease_owner (else
-- 'lease_lost') and no other terminal row for the key. The advisory lock
-- serializes a completed and a failed insert racing for one key.
create or replace function public.assistant_tool_calls_fence()
returns trigger
language plpgsql
security invoker
set search_path = pg_catalog, public
as $$
declare
  v_owner text;
begin
  if new.phase = 'begun' then
    return new;
  end if;
  perform pg_advisory_xact_lock(hashtext('assistant_tool_calls:' || new.idempotency_key));
  select t.lease_owner into v_owner
  from public.assistant_tool_calls t
  where t.idempotency_key = new.idempotency_key and t.phase = 'begun';
  if not found then
    raise exception 'no_begun_row' using errcode = 'foreign_key_violation';
  end if;
  if v_owner is distinct from new.lease_owner then
    raise exception 'lease_lost' using errcode = 'check_violation';
  end if;
  if exists (
    select 1 from public.assistant_tool_calls t
    where t.idempotency_key = new.idempotency_key and t.phase in ('completed', 'failed')
  ) then
    raise exception 'already_finished' using errcode = 'unique_violation';
  end if;
  return new;
end;
$$;

create or replace trigger assistant_tool_calls_fence before insert on public.assistant_tool_calls
  for each row execute function public.assistant_tool_calls_fence();

-- 8d. Phase ledger RPC ---------------------------------------------------------------------
-- Inserts the begun row, or returns the rows already recorded for the key.
-- Result: {"inserted": true|false, "rows": [<assistant_tool_calls rows>]}.
-- inserted=false with a completed row means the step is done; with only a
-- begun row it means the step may have partially run (a read is re-run under
-- a fresh key, a write parks as waiting_review).
create or replace function public.assistant_tool_call_begin(
  p_idempotency_key text,
  p_lease_owner text,
  p_user_id uuid,
  p_user_label text,
  p_run_id uuid,
  p_seq int,
  p_tool_name text,
  p_effect text,
  p_conversation_id uuid default null,
  p_message_id uuid default null,
  p_tool_version text default null,
  p_sub_app text default null,
  p_method text default null,
  p_path_format text default null,
  p_args_redacted jsonb default null,
  p_payload_sha256 text default null,
  p_before_snapshot jsonb default null,
  p_project_id uuid default null,
  p_approval_id uuid default null,
  p_tainted_context boolean default false
)
returns jsonb
language plpgsql
volatile
security invoker
set search_path = pg_catalog, public
as $$
declare
  v_row public.assistant_tool_calls;
  v_rows jsonb;
begin
  insert into public.assistant_tool_calls (
    user_id, user_label, conversation_id, message_id, run_id, seq, idempotency_key, phase,
    lease_owner, tool_name, tool_version, effect, sub_app, method, path_format, args_redacted,
    payload_sha256, before_snapshot, project_id, approval_id, tainted_context
  ) values (
    p_user_id, p_user_label, p_conversation_id, p_message_id, p_run_id, p_seq, p_idempotency_key, 'begun',
    p_lease_owner, p_tool_name, p_tool_version, p_effect, p_sub_app, p_method, p_path_format, p_args_redacted,
    p_payload_sha256, p_before_snapshot, p_project_id, p_approval_id, coalesce(p_tainted_context, false)
  )
  on conflict (idempotency_key, phase) do nothing
  returning * into v_row;

  if found then
    return jsonb_build_object('inserted', true, 'rows', jsonb_build_array(to_jsonb(v_row)));
  end if;

  select coalesce(jsonb_agg(to_jsonb(t) order by t.created_at, t.id), '[]'::jsonb) into v_rows
  from public.assistant_tool_calls t
  where t.idempotency_key = p_idempotency_key;
  return jsonb_build_object('inserted', false, 'rows', v_rows);
end;
$$;

-- 8e. Approval immutability --------------------------------------------------------------
-- Status ranks: pending 0, approved 1, executing 2, terminal 3 (rejected,
-- expired, executed, failed). A move must raise the rank; a terminal row
-- never changes status again.
create or replace function public.assistant_approval_immutable()
returns trigger
language plpgsql
security invoker
set search_path = pg_catalog, public
as $$
declare
  v_old int;
  v_new int;
begin
  if new.payload is distinct from old.payload
     or new.payload_sha256 is distinct from old.payload_sha256
     or new.preview is distinct from old.preview
     or new.precondition is distinct from old.precondition
     or new.user_id is distinct from old.user_id
     or new.action_kind is distinct from old.action_kind then
    raise exception 'assistant_approvals: payload, payload_sha256, preview, precondition, user_id and action_kind are immutable'
      using errcode = 'restrict_violation';
  end if;
  if new.status is distinct from old.status then
    v_old := case old.status when 'pending' then 0 when 'approved' then 1 when 'executing' then 2 else 3 end;
    v_new := case new.status when 'pending' then 0 when 'approved' then 1 when 'executing' then 2 else 3 end;
    if v_new <= v_old then
      raise exception 'assistant_approvals: status cannot move from % to %', old.status, new.status
        using errcode = 'restrict_violation';
    end if;
  end if;
  return new;
end;
$$;

create or replace trigger assistant_approval_immutable before update on public.assistant_approvals
  for each row execute function public.assistant_approval_immutable();

-- 8f. Sticky conversation taint ------------------------------------------------------------
-- tainted_at and mailbox_tainted_at are set once and never cleared.
create or replace function public.assistant_conversation_taint_sticky()
returns trigger
language plpgsql
security invoker
set search_path = pg_catalog, public
as $$
begin
  new.tainted_at := coalesce(old.tainted_at, new.tainted_at);
  new.mailbox_tainted_at := coalesce(old.mailbox_tainted_at, new.mailbox_tainted_at);
  return new;
end;
$$;

create or replace trigger assistant_conversation_taint_sticky before update on public.assistant_conversations
  for each row execute function public.assistant_conversation_taint_sticky();

-- A tainted message taints its conversation (mailbox taint implies taint).
-- A message never becomes untainted.
create or replace function public.assistant_message_taint()
returns trigger
language plpgsql
security invoker
set search_path = pg_catalog, public
as $$
begin
  if tg_op = 'UPDATE' then
    new.tainted := old.tainted or new.tainted;
    new.mailbox_tainted := old.mailbox_tainted or new.mailbox_tainted;
  end if;
  if new.mailbox_tainted then
    new.tainted := true;
  end if;
  if new.tainted then
    update public.assistant_conversations
    set tainted_at = coalesce(tainted_at, now()),
        mailbox_tainted_at = case when new.mailbox_tainted then coalesce(mailbox_tainted_at, now())
                                  else mailbox_tainted_at end
    where id = new.conversation_id
      and (tainted_at is null or (new.mailbox_tainted and mailbox_tainted_at is null));
  end if;
  return new;
end;
$$;

create or replace trigger assistant_message_taint before insert or update of tainted, mailbox_tainted on public.assistant_messages
  for each row execute function public.assistant_message_taint();

-- 8g. Memory caps ----------------------------------------------------------------------------
-- 300 active, 40 pinned, 6000 pinned characters (title plus body) per
-- person. Pinned counts only active rows. A change is checked only when it
-- grows the measured quantity, so archiving or unpinning always works. The
-- per-person advisory lock makes concurrent saves see each other.
create or replace function public.assistant_memories_caps()
returns trigger
language plpgsql
security invoker
set search_path = pg_catalog, public
as $$
declare
  v_was_active boolean := false;
  v_was_pinned boolean := false;
  v_old_chars  int := 0;
  v_is_pinned  boolean := new.status = 'active' and new.pinned;
  v_count int;
  v_chars bigint;
begin
  if new.status <> 'active' then
    return new;
  end if;
  if tg_op = 'UPDATE' and old.user_id = new.user_id then
    v_was_active := old.status = 'active';
    v_was_pinned := old.status = 'active' and old.pinned;
    v_old_chars := length(old.title) + length(old.body);
  end if;
  perform pg_advisory_xact_lock(hashtext('assistant_memories:' || new.user_id::text));

  if not v_was_active then
    select count(*) into v_count from public.assistant_memories m
    where m.user_id = new.user_id and m.status = 'active' and m.id <> new.id;
    if v_count + 1 > 300 then
      raise exception 'memory_cap_active: at most 300 active memories' using errcode = 'check_violation';
    end if;
  end if;

  if v_is_pinned and not v_was_pinned then
    select count(*) into v_count from public.assistant_memories m
    where m.user_id = new.user_id and m.status = 'active' and m.pinned and m.id <> new.id;
    if v_count + 1 > 40 then
      raise exception 'memory_cap_pinned: at most 40 pinned memories' using errcode = 'check_violation';
    end if;
  end if;

  if v_is_pinned and (not v_was_pinned or length(new.title) + length(new.body) > v_old_chars) then
    select coalesce(sum(length(m.title) + length(m.body)), 0) into v_chars from public.assistant_memories m
    where m.user_id = new.user_id and m.status = 'active' and m.pinned and m.id <> new.id;
    if v_chars + length(new.title) + length(new.body) > 6000 then
      raise exception 'memory_cap_pinned_chars: pinned memories are limited to 6000 characters'
        using errcode = 'check_violation';
    end if;
  end if;
  return new;
end;
$$;

create or replace trigger assistant_memories_caps before insert or update of status, pinned, title, body, user_id
  on public.assistant_memories
  for each row execute function public.assistant_memories_caps();

-- 8h. Schedule caps ----------------------------------------------------------------------------
-- 10 active per person. Minimum interval 1 hour, enforced on the rule: FREQ
-- must be HOURLY or slower, and BYSECOND, RDATE and multi-value BYMINUTE are
-- refused. With at most one minute value and no seconds, two occurrences
-- always fall in different hours, so they are at least an hour apart (a DST
-- repeated hour runs once, per the design).
create or replace function public.assistant_schedules_caps()
returns trigger
language plpgsql
security invoker
set search_path = pg_catalog, public
as $$
declare
  v_rule text := upper(new.rrule);
  v_freq text;
  v_byminute text;
  v_count int;
  v_rule_changed boolean := true;
  v_newly_active boolean := new.status = 'active';
begin
  if tg_op = 'UPDATE' then
    v_rule_changed := new.rrule is distinct from old.rrule;
    v_newly_active := new.status = 'active' and (old.status <> 'active' or old.user_id <> new.user_id);
  end if;

  if v_rule_changed then
    v_freq := substring(v_rule from 'FREQ=([A-Z]+)');
    if v_freq is null or v_freq not in ('HOURLY', 'DAILY', 'WEEKLY', 'MONTHLY', 'YEARLY') then
      raise exception 'schedule_interval: FREQ must be HOURLY or slower (minimum interval 1 hour)'
        using errcode = 'check_violation';
    end if;
    if v_rule ~ 'BYSECOND=' or v_rule ~ '(^|[^A-Z])RDATE' then
      raise exception 'schedule_interval: BYSECOND and RDATE are not allowed (minimum interval 1 hour)'
        using errcode = 'check_violation';
    end if;
    v_byminute := substring(v_rule from 'BYMINUTE=([0-9,]+)');
    if v_byminute is not null and position(',' in v_byminute) > 0 then
      raise exception 'schedule_interval: one BYMINUTE value at most (minimum interval 1 hour)'
        using errcode = 'check_violation';
    end if;
  end if;

  if v_newly_active then
    perform pg_advisory_xact_lock(hashtext('assistant_schedules:' || new.user_id::text));
    select count(*) into v_count from public.assistant_schedules s
    where s.user_id = new.user_id and s.status = 'active' and s.id <> new.id;
    if v_count + 1 > 10 then
      raise exception 'schedule_cap_active: at most 10 active schedules' using errcode = 'check_violation';
    end if;
  end if;
  return new;
end;
$$;

create or replace trigger assistant_schedules_caps before insert or update of rrule, status, user_id
  on public.assistant_schedules
  for each row execute function public.assistant_schedules_caps();

-- 8i. Staged file caps -------------------------------------------------------------------------
-- 10 files per message, 2 GB of live unconsumed staging per person.
create or replace function public.assistant_files_caps()
returns trigger
language plpgsql
security invoker
set search_path = pg_catalog, public
as $$
declare
  v_count int;
  v_bytes bigint;
begin
  perform pg_advisory_xact_lock(hashtext('assistant_files:' || new.user_id::text));
  if new.message_id is not null then
    select count(*) into v_count from public.assistant_files f
    where f.message_id = new.message_id and f.id <> new.id;
    if v_count + 1 > 10 then
      raise exception 'file_cap_message: at most 10 files per message' using errcode = 'check_violation';
    end if;
  end if;
  select coalesce(sum(f.size_bytes), 0) into v_bytes from public.assistant_files f
  where f.user_id = new.user_id and f.consumed_at is null and f.expires_at > now() and f.id <> new.id;
  if v_bytes + new.size_bytes > 2147483648 then
    raise exception 'file_cap_user: at most 2 GB of staged files' using errcode = 'check_violation';
  end if;
  return new;
end;
$$;

create or replace trigger assistant_files_caps before insert on public.assistant_files
  for each row execute function public.assistant_files_caps();

-- 9. Changes to existing tables --------------------------------------------------------

-- 9a. llm_jobs job types, widened defensively. Reads the live constraint,
-- keeps every type already present and appends the assistant types, so a
-- migration that re-listed an older full list (0152, 0154) cannot have
-- dropped them for good. Refuses to run when the definition cannot be read
-- as a list, rather than shrinking it.
do $$
declare
  v_def     text;
  v_types   text[];
  v_missing text[];
  v_list    text;
  v_add     constant text[] := array['assistant_turn', 'assistant_continue', 'assistant_approval_execute'];
begin
  select pg_get_constraintdef(c.oid) into v_def
  from pg_constraint c
  where c.conrelid = 'public.llm_jobs'::regclass and c.conname = 'llm_jobs_job_type_check';

  if v_def is null then
    raise notice '0170: llm_jobs_job_type_check is missing; job_type stays unconstrained';
    return;
  end if;
  if v_def !~* 'job_type' then
    raise exception '0170: unexpected llm_jobs_job_type_check definition: %', v_def;
  end if;

  select array_agg(replace(r.m[1], '''''', '''') order by r.ord) into v_types
  from regexp_matches(v_def, '''((?:[^'']|'''')*)''', 'g') with ordinality as r(m, ord);

  if v_types is null or cardinality(v_types) = 0 or not ('boq_extraction' = any(v_types)) then
    raise exception '0170: could not read the job type list from llm_jobs_job_type_check: %', v_def;
  end if;

  select array_agg(a.t order by a.ord) into v_missing
  from unnest(v_add) with ordinality as a(t, ord)
  where not (a.t = any(v_types));

  if v_missing is null then
    return;
  end if;

  v_types := v_types || v_missing;
  select string_agg(quote_literal(u.t), ', ' order by u.ord) into v_list
  from unnest(v_types) with ordinality as u(t, ord);

  execute 'alter table public.llm_jobs drop constraint llm_jobs_job_type_check';
  execute format('alter table public.llm_jobs add constraint llm_jobs_job_type_check check (job_type in (%s)) not valid',
                 v_list);
  execute 'alter table public.llm_jobs validate constraint llm_jobs_job_type_check';
end $$;

-- 9b. llm_call_log: per-user spend and the assistant run behind a call.
-- Plain uuids: the log is pruned at 90 days and must never block a profile delete.
alter table public.llm_call_log add column if not exists user_id uuid;
alter table public.llm_call_log add column if not exists assistant_run_id uuid;
create index if not exists llm_call_log_user_idx
  on public.llm_call_log (user_id, created_at desc) where user_id is not null;
create index if not exists llm_call_log_assistant_run_idx
  on public.llm_call_log (assistant_run_id) where assistant_run_id is not null;

comment on column public.llm_call_log.user_id is
  'The person a call was made for (assistant turns and other per-user features); summed per day for the assistant daily cap. NULL for system calls.';
comment on column public.llm_call_log.assistant_run_id is
  'Assistant run (tool ledger run_id) that made the call; NULL outside the assistant.';

-- 9c. projects: create_project_draft passes a client idempotency token, so a
-- replayed step finds the row instead of creating a second project.
alter table public.projects add column if not exists assistant_idempotency_key text;
create unique index if not exists projects_assistant_idempotency_key_uq
  on public.projects (assistant_idempotency_key) where assistant_idempotency_key is not null;

comment on column public.projects.assistant_idempotency_key is
  'Client idempotency token from the assistant create_project_draft tool. NULL for every project not created by an assistant.';

-- 9d. bid_split_jobs: a split job started by an assistant task; _after_done
-- enqueues assistant_continue when this is set. No FK, like project_id here.
alter table public.bid_split_jobs add column if not exists assistant_task_id uuid;
create index if not exists bid_split_jobs_assistant_task_idx
  on public.bid_split_jobs (assistant_task_id) where assistant_task_id is not null;

-- 9e. todos: assistant tasks appear on the To-dos page.
alter table public.todos add column if not exists due_at timestamptz;
alter table public.todos add column if not exists project_id uuid;
alter table public.todos add column if not exists source text not null default 'user';
alter table public.todos add column if not exists assistant_task_id uuid;
do $$
begin
  if not exists (
    select 1 from pg_constraint where conrelid = 'public.todos'::regclass and conname = 'todos_source_check'
  ) then
    alter table public.todos add constraint todos_source_check check (source in ('user', 'assistant', 'system'));
  end if;
end $$;
create index if not exists todos_assistant_task_idx
  on public.todos (assistant_task_id) where assistant_task_id is not null;

-- 10. RLS and grants ----------------------------------------------------------------------

alter table public.assistant_settings enable row level security;
alter table public.assistant_settings force row level security;
revoke all on table public.assistant_settings from anon, authenticated;
alter table public.assistant_profiles enable row level security;
alter table public.assistant_profiles force row level security;
revoke all on table public.assistant_profiles from anon, authenticated;
alter table public.assistant_delegations enable row level security;
alter table public.assistant_delegations force row level security;
revoke all on table public.assistant_delegations from anon, authenticated;
alter table public.assistant_conversations enable row level security;
alter table public.assistant_conversations force row level security;
revoke all on table public.assistant_conversations from anon, authenticated;
alter table public.assistant_messages enable row level security;
alter table public.assistant_messages force row level security;
revoke all on table public.assistant_messages from anon, authenticated;
alter table public.assistant_tool_results enable row level security;
alter table public.assistant_tool_results force row level security;
revoke all on table public.assistant_tool_results from anon, authenticated;
alter table public.assistant_tool_calls enable row level security;
alter table public.assistant_tool_calls force row level security;
revoke all on table public.assistant_tool_calls from anon, authenticated;
alter table public.assistant_approvals enable row level security;
alter table public.assistant_approvals force row level security;
revoke all on table public.assistant_approvals from anon, authenticated;
alter table public.assistant_memories enable row level security;
alter table public.assistant_memories force row level security;
revoke all on table public.assistant_memories from anon, authenticated;
alter table public.assistant_memory_events enable row level security;
alter table public.assistant_memory_events force row level security;
revoke all on table public.assistant_memory_events from anon, authenticated;
alter table public.assistant_memory_revisions enable row level security;
alter table public.assistant_memory_revisions force row level security;
revoke all on table public.assistant_memory_revisions from anon, authenticated;
alter table public.assistant_schedules enable row level security;
alter table public.assistant_schedules force row level security;
revoke all on table public.assistant_schedules from anon, authenticated;
alter table public.assistant_schedule_runs enable row level security;
alter table public.assistant_schedule_runs force row level security;
revoke all on table public.assistant_schedule_runs from anon, authenticated;
alter table public.assistant_tasks enable row level security;
alter table public.assistant_tasks force row level security;
revoke all on table public.assistant_tasks from anon, authenticated;
alter table public.assistant_files enable row level security;
alter table public.assistant_files force row level security;
revoke all on table public.assistant_files from anon, authenticated;
alter table public.assistant_events enable row level security;
alter table public.assistant_events force row level security;
revoke all on table public.assistant_events from anon, authenticated;
alter table public.assistant_subscriptions enable row level security;
alter table public.assistant_subscriptions force row level security;
revoke all on table public.assistant_subscriptions from anon, authenticated;
alter table public.assistant_inbox enable row level security;
alter table public.assistant_inbox force row level security;
revoke all on table public.assistant_inbox from anon, authenticated;
alter table public.assistant_conversation_views enable row level security;
alter table public.assistant_conversation_views force row level security;
revoke all on table public.assistant_conversation_views from anon, authenticated;
alter table public.assistant_eval_lock enable row level security;
alter table public.assistant_eval_lock force row level security;
revoke all on table public.assistant_eval_lock from anon, authenticated;

revoke all on function public.assistant_refuse_change() from public;
revoke all on function public.assistant_refuse_change() from anon, authenticated;
grant execute on function public.assistant_refuse_change() to service_role;

revoke all on function public.assistant_tool_calls_fence() from public;
revoke all on function public.assistant_tool_calls_fence() from anon, authenticated;
grant execute on function public.assistant_tool_calls_fence() to service_role;

revoke all on function public.assistant_tool_call_begin(text, text, uuid, text, uuid, integer, text, text, uuid, uuid, text, text, text, text, jsonb, text, jsonb, uuid, uuid, boolean) from public;
revoke all on function public.assistant_tool_call_begin(text, text, uuid, text, uuid, integer, text, text, uuid, uuid, text, text, text, text, jsonb, text, jsonb, uuid, uuid, boolean) from anon, authenticated;
grant execute on function public.assistant_tool_call_begin(text, text, uuid, text, uuid, integer, text, text, uuid, uuid, text, text, text, text, jsonb, text, jsonb, uuid, uuid, boolean) to service_role;

revoke all on function public.assistant_approval_immutable() from public;
revoke all on function public.assistant_approval_immutable() from anon, authenticated;
grant execute on function public.assistant_approval_immutable() to service_role;

revoke all on function public.assistant_conversation_taint_sticky() from public;
revoke all on function public.assistant_conversation_taint_sticky() from anon, authenticated;
grant execute on function public.assistant_conversation_taint_sticky() to service_role;

revoke all on function public.assistant_message_taint() from public;
revoke all on function public.assistant_message_taint() from anon, authenticated;
grant execute on function public.assistant_message_taint() to service_role;

revoke all on function public.assistant_memories_caps() from public;
revoke all on function public.assistant_memories_caps() from anon, authenticated;
grant execute on function public.assistant_memories_caps() to service_role;

revoke all on function public.assistant_schedules_caps() from public;
revoke all on function public.assistant_schedules_caps() from anon, authenticated;
grant execute on function public.assistant_schedules_caps() to service_role;

revoke all on function public.assistant_files_caps() from public;
revoke all on function public.assistant_files_caps() from anon, authenticated;
grant execute on function public.assistant_files_caps() to service_role;

notify pgrst, 'reload schema';
