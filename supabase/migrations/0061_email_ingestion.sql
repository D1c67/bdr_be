-- Email ingestion: poll a PM mailbox (Inbox + Sent Items) via Graph delta
-- queries, persist every message, and run the identification pipeline
-- (R1 conversation map -> R2 deterministic subject match -> R3 LLM subject
-- match). Unassigned processed/failed emails form the "Unknown" triage pool.
--
-- Statuses are job-like -> text + check (not pg enums). `status` names the
-- NEXT step to run, so a crashed worker resumes exactly where it stopped:
--   received -> body/attachment fetch pending
--   id_r1    -> conversation-map lookup pending
--   id_r2    -> deterministic subject match pending
--   id_r3    -> LLM match pending (also the retry state; next_attempt_at gates)
--   processed / failed -> terminal (failed still surfaces for manual triage).
--
-- No graph_sync_state DDL needed: the poller creates rows at runtime under
-- ids `pm-mail:{mailbox}:inbox`, `pm-mail:{mailbox}:sentitems` (delta cursors)
-- and `pm-mail:{mailbox}:lease` (single-runner lease). The mailbox is embedded
-- so switching to a shared mailbox later is env-only.

create table ingested_emails (
  id                   uuid primary key default gen_random_uuid(),
  mailbox              text not null,
  folder               text not null check (folder in ('inbox','sentitems')),
  direction            text not null check (direction in ('inbound','outbound')),
  graph_message_id     text not null unique,   -- ImmutableId; the dedup key
  internet_message_id  text,
  conversation_id      text,
  from_name            text,
  from_address         text,
  to_recipients        jsonb not null default '[]'::jsonb,  -- [{name,address},...]
  cc_recipients        jsonb not null default '[]'::jsonb,
  subject              text,
  body_preview         text,
  body_text            text,                   -- plain-text body, capped (email_body_max_chars)
  body_truncated       boolean not null default false,
  message_at           timestamptz,            -- sentDateTime (outbound) / receivedDateTime (inbound)
  has_attachments      boolean not null default false,
  status               text not null default 'received'
                         check (status in ('received','id_r1','id_r2','id_r3','processed','failed')),
  attempts             int not null default 0,
  next_attempt_at      timestamptz,            -- retry/backoff gate for the sweep
  error                text,
  project_id           uuid references projects(id) on delete set null,
  matched_by           text check (matched_by in ('conversation','subject','llm','manual')),
  match_confidence     numeric(4,3),
  match_model          text,
  -- Below-threshold LLM guess, kept to aid manual triage of the Unknown pool.
  suggested_project_id uuid references projects(id) on delete set null,
  suggested_confidence numeric(4,3),
  assigned_by          uuid references profiles(id) on delete set null,  -- manual assigns only
  assigned_at          timestamptz,
  processed_at         timestamptz,
  created_at           timestamptz not null default now(),
  updated_at           timestamptz not null default now()
);
create trigger ingested_emails_updated_at before update on ingested_emails
  for each row execute function set_updated_at();

create index ingested_emails_conversation_idx on ingested_emails (mailbox, conversation_id);
create index ingested_emails_project_idx on ingested_emails (project_id, message_at desc);
-- The worklist sweep: every non-terminal email, every tick.
create index ingested_emails_pending_idx on ingested_emails (status)
  where status not in ('processed','failed');
-- The Unknown pool: unassigned terminal emails, newest first.
create index ingested_emails_unknown_idx on ingested_emails (message_at desc)
  where project_id is null and status in ('processed','failed');
create index ingested_emails_imsgid_idx on ingested_emails (internet_message_id);

create table ingested_email_attachments (
  id                  uuid primary key default gen_random_uuid(),
  email_id            uuid not null references ingested_emails(id) on delete cascade,
  graph_attachment_id text,
  filename            text not null,
  mime_type           text,
  size_bytes          bigint,
  storage_path        text,  -- null = metadata-only (content not stored; see skipped_reason)
  skipped_reason      text check (skipped_reason in ('too_large','too_many','item_attachment')),
  created_at          timestamptz not null default now()
);
create index ingested_email_attachments_email_idx on ingested_email_attachments (email_id);
-- DB-level idempotency backstop: if two workers ever overlap on the same email
-- (lease expiry during a long sweep), the second batch insert dedups instead of
-- doubling every attachment row.
create unique index ingested_email_attachments_email_att_uidx
  on ingested_email_attachments (email_id, graph_attachment_id);
-- FK support: project deletes must not seq-scan the email table.
create index ingested_emails_suggested_project_idx on ingested_emails (suggested_project_id);

-- Lease fencing for the email poller: the holder token lets a runner renew its
-- own lease mid-sweep (and re-acquire it next tick) while any other runner's
-- renewal fails closed. The RFQ poller ignores this column.
alter table graph_sync_state add column if not exists holder text;

-- Learn-back map: one project per (mailbox, conversation). Source of truth for
-- R1. Written on every assignment (subject/llm/manual); manual always outranks
-- auto sources, so an automatic match can never demote a human decision.
create table email_conversation_projects (
  id              uuid primary key default gen_random_uuid(),
  mailbox         text not null,
  conversation_id text not null,
  project_id      uuid not null references projects(id) on delete cascade,
  source          text not null check (source in ('subject','llm','manual')),
  created_by      uuid references profiles(id) on delete set null,
  created_at      timestamptz not null default now(),
  updated_at      timestamptz not null default now(),
  unique (mailbox, conversation_id)
);
create trigger email_conversation_projects_updated_at before update on email_conversation_projects
  for each row execute function set_updated_at();
create index email_conversation_projects_project_idx on email_conversation_projects (project_id);

-- RLS: deny-by-default backstop (0007/0055 pattern) — the service-role backend
-- bypasses RLS; FastAPI role dependencies are the authorization boundary.
alter table ingested_emails enable row level security;
alter table ingested_emails force row level security;
alter table ingested_email_attachments enable row level security;
alter table ingested_email_attachments force row level security;
alter table email_conversation_projects enable row level security;
alter table email_conversation_projects force row level security;

notify pgrst, 'reload schema';
