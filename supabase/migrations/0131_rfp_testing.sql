-- 0131 - RFP Ingestion: test mode and monitor (docs/RFP_TESTING.md sections
-- 3, 7 and 10). Apply after 0130. DEV ONLY: this migration goes to the dev
-- database only and never to production without explicit approval
-- (RFP_TESTING_ENABLED stays false in Railway; the prod config guard refuses
-- to boot with it on).
--
-- A test session is the IT Admin exercising the whole RFP Ingestion feature
-- end to end against real forwarded mail in a dedicated test mailbox. Every
-- row a session produces is tagged with its id (`test_session_id`, on the
-- nine tables below), every step is captured in rfp_test_events, and "Clean
-- up" on an ended session deletes the tagged rows while the session row and
-- its events stay as the record. `rfp_emails.forward_meta` records how a
-- forwarded invitation was unwrapped (section 5); `email_log.redirected_to`
-- records where a redirected send really went (section 6).
--
-- Every statement is idempotent.

-- ── 1. rfp_test_sessions ────────────────────────────────────────────────

create table if not exists rfp_test_sessions (
  id                    uuid primary key default gen_random_uuid(),
  status                text not null check (status in ('active', 'ended')),
  name                  text,                                   -- optional label typed at activation
  started_by            uuid references profiles(id) on delete set null,
  started_at            timestamptz not null default now(),
  ended_by              uuid references profiles(id) on delete set null,
  ended_at              timestamptz,
  mailbox               text not null,                          -- snapshot of the settings at activation
  sender                text not null,
  redirect_to           text not null,
  auto_create           boolean not null default false,
  intake_last_tick_at   timestamptz,                            -- heartbeat: RFP intake poller
  filer_last_tick_at    timestamptz,                            -- heartbeat: project email filer
  cleanup_started_at    timestamptz,
  cleanup_finished_at   timestamptz,
  cleanup_report        jsonb,                                  -- what was deleted / kept (section 9)
  created_at            timestamptz not null default now()
);

comment on table rfp_test_sessions is
  'One row per RFP Ingestion test session (docs/RFP_TESTING.md): who started it, the mailbox / sender / redirect snapshot it ran with, the two poller heartbeats and the cleanup report. Kept forever as the record; only the tagged rows are deleted by cleanup.';
comment on column rfp_test_sessions.mailbox is
  'The test mailbox the pollers watched while the session was active (RFP_TESTING_MAILBOX at activation). Only messages from `sender` received at or after started_at are read.';

-- Two activations can never race into two active sessions.
create unique index if not exists rfp_test_sessions_one_active
  on rfp_test_sessions ((status)) where status = 'active';
create index if not exists rfp_test_sessions_started_idx
  on rfp_test_sessions (started_at desc);

alter table rfp_test_sessions enable row level security;
alter table rfp_test_sessions force row level security;

-- ── 2. rfp_test_events ──────────────────────────────────────────────────

create table if not exists rfp_test_events (
  id                 bigint generated always as identity primary key,
  session_id         uuid not null references rfp_test_sessions(id) on delete cascade,
  at                 timestamptz not null default now(),
  source             text not null,   -- session | intake | filer | harvest | create | project | mail_out | human
  kind               text not null,   -- docs/RFP_TESTING.md 7.2
  level              text not null default 'info' check (level in ('info', 'warn', 'error')),
  title              text not null,   -- one line, human readable
  rfp_email_id       uuid references rfp_emails(id) on delete set null,
  ingested_email_id  uuid references ingested_emails(id) on delete set null,
  harvest_id         uuid references rfp_harvests(id) on delete set null,
  project_id         uuid references projects(id) on delete set null,
  detail             jsonb not null default '{}'::jsonb
);

comment on table rfp_test_events is
  'The event ledger of a test session (docs/RFP_TESTING.md section 7): one row per captured step (intake, filer, harvest, create, mail out, human action, session). `detail` is capped at 64 KB by the writer. The referenced rows may be deleted by cleanup; the event stays.';

create index if not exists rfp_test_events_session_idx
  on rfp_test_events (session_id, id);
create index if not exists rfp_test_events_email_idx
  on rfp_test_events (rfp_email_id) where rfp_email_id is not null;
create index if not exists rfp_test_events_project_idx
  on rfp_test_events (project_id) where project_id is not null;

alter table rfp_test_events enable row level security;
alter table rfp_test_events force row level security;

-- ── 3. The tag column on every table a session writes ───────────────────
-- `on delete set null`: a session row is never deleted, but the FK keeps a
-- typo'd id out. The partial indexes serve the sweep filters (normal mode
-- reads `test_session_id is null`, which the planner answers without them)
-- and the cleanup's per-session deletes.

alter table rfp_emails
  add column if not exists test_session_id uuid references rfp_test_sessions(id) on delete set null,
  add column if not exists forward_meta    jsonb;
comment on column rfp_emails.test_session_id is
  'Set on rows the RFP intake inserted from the test mailbox while that session was active. The sweep touches tagged rows only in test mode and untagged rows only in normal mode.';
comment on column rfp_emails.forward_meta is
  'Test rows only (docs/RFP_TESTING.md 5.3): how the forward was unwrapped ("inline", "eml" or "none"), the raw From / subject / received time of the forward itself, the forwarder''s note, and the .eml parts when the original came as an attachment.';
create index if not exists rfp_emails_test_session_idx
  on rfp_emails (test_session_id) where test_session_id is not null;

alter table rfp_harvests
  add column if not exists test_session_id uuid references rfp_test_sessions(id) on delete set null;
create index if not exists rfp_harvests_test_session_idx
  on rfp_harvests (test_session_id) where test_session_id is not null;

alter table rfp_ingest_runs
  add column if not exists test_session_id uuid references rfp_test_sessions(id) on delete set null;
create index if not exists rfp_ingest_runs_test_session_idx
  on rfp_ingest_runs (test_session_id) where test_session_id is not null;

alter table rfp_created_projects
  add column if not exists test_session_id uuid references rfp_test_sessions(id) on delete set null;
create index if not exists rfp_created_projects_test_session_idx
  on rfp_created_projects (test_session_id) where test_session_id is not null;

alter table projects
  add column if not exists test_session_id uuid references rfp_test_sessions(id) on delete set null;
comment on column projects.test_session_id is
  'Set on projects the RFP creation step made from a test session''s rows. Cleanup of that session deletes them (FK cascades carry the children, exactly as discard does).';
create index if not exists projects_test_session_idx
  on projects (test_session_id) where test_session_id is not null;

alter table general_contractors
  add column if not exists test_session_id uuid references rfp_test_sessions(id) on delete set null;
create index if not exists general_contractors_test_session_idx
  on general_contractors (test_session_id) where test_session_id is not null;

alter table gc_contacts
  add column if not exists test_session_id uuid references rfp_test_sessions(id) on delete set null;
create index if not exists gc_contacts_test_session_idx
  on gc_contacts (test_session_id) where test_session_id is not null;

alter table ingested_emails
  add column if not exists test_session_id uuid references rfp_test_sessions(id) on delete set null;
create index if not exists ingested_emails_test_session_idx
  on ingested_emails (test_session_id) where test_session_id is not null;

alter table email_log
  add column if not exists test_session_id uuid references rfp_test_sessions(id) on delete set null,
  add column if not exists redirected_to   text;
comment on column email_log.redirected_to is
  'Where a send made while a test session was active really went (RFP_TESTING_REDIRECT_TO). to_addrs keeps the INTENDED To line, which proposal_send''s crash recovery compares against.';
create index if not exists email_log_test_session_idx
  on email_log (test_session_id) where test_session_id is not null;

notify pgrst, 'reload schema';
