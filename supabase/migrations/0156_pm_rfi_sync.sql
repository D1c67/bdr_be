-- 0156: PM RFI sync from the GC's Procore (docs/PM_RFI_SYNC.md).
--
-- A PM project can be linked to the GC's project management app. For
-- Procore, a daily sync (06:30 America/Los_Angeles), a manual "Sync now" and
-- inbound Procore RFI emails pull every RFI on the GC project into the RFI
-- log, read only, through the shared logged-in Procore web session (the same
-- rfp_harvest_sessions row as the RFP harvest). Nothing is ever written to
-- Procore.
--
--   1. pm_external_links: one row per project naming the GC's app and its
--      project. The partial unique index keeps two BDR projects off the same
--      Procore project.
--   2. rfis: imported rows (source 'procore') carry no rfi_number; G3's own
--      rows (source 'internal') still need one. external_* hold Procore's
--      view; a linked internal row fills them without becoming 'procore'.
--      external_autolink_blocked is set by Unlink so the next sync does not
--      immediately re-link the row it was split from.
--   3. rfi_attachments.external_file_id: the Procore file id, so a file is
--      downloaded once per RFI.
--   4. pm_rfi_sync_runs: the run queue (leases for the two uvicorn workers)
--      and, through the daily unique index, the slot ledger.
--   5. pm_rfi_email_scan_state: the email trigger scan's checkpoints.

-- 1. The link ------------------------------------------------------------------

create table if not exists pm_external_links (
  id                   uuid primary key default gen_random_uuid(),
  project_id           uuid not null unique references projects(id) on delete cascade,
  platform             text not null check (platform in ('procore', 'other', 'none')),
  host                 text,
  external_company_id  text,
  external_project_id  text,
  external_url         text,
  sync_enabled         boolean not null default true,
  linked_by            uuid references profiles(id) on delete set null,
  linked_via           text check (linked_via in ('manual', 'email')),
  last_synced_at       timestamptz,
  last_sync_status     text,
  last_error           text,
  backfilled_at        timestamptz,
  -- Procore RFI ids the backfill could not read (refused): imported later
  -- without a "new" announcement, since they existed before the link.
  backfill_skipped_ids text[] not null default '{}',
  created_at           timestamptz not null default now(),
  updated_at           timestamptz not null default now()
);

alter table pm_external_links
  add column if not exists backfill_skipped_ids text[] not null default '{}';

create unique index if not exists pm_external_links_procore_project_uidx
  on pm_external_links (platform, host, external_project_id)
  where platform = 'procore';
create index if not exists pm_external_links_linked_by_idx on pm_external_links (linked_by);

drop trigger if exists pm_external_links_updated_at on pm_external_links;
create trigger pm_external_links_updated_at before update on pm_external_links
  for each row execute function set_updated_at();

-- 2. rfis ------------------------------------------------------------------------

alter table rfis alter column rfi_number drop not null;

alter table rfis
  add column if not exists source text not null default 'internal',
  add column if not exists external_id text,
  add column if not exists external_number text,
  add column if not exists external_revision text default '0',
  add column if not exists external_status text,
  add column if not exists external_url text,
  add column if not exists responsible_contractor text,
  add column if not exists received_from text,
  add column if not exists initiated_at timestamptz,
  add column if not exists ball_in_court text,
  add column if not exists external_due_date date,
  add column if not exists external_updated_at timestamptz,
  add column if not exists external_synced_at timestamptz,
  add column if not exists external_raw jsonb,
  add column if not exists external_autolink_blocked boolean not null default false,
  add column if not exists source_email_id uuid references ingested_emails(id) on delete set null;

alter table rfis drop constraint if exists rfis_source_chk;
alter table rfis add constraint rfis_source_chk check (source in ('internal', 'procore'));
alter table rfis drop constraint if exists rfis_internal_number_chk;
alter table rfis add constraint rfis_internal_number_chk
  check (source <> 'internal' or rfi_number is not null);

-- The per-project numbering unique, now only over rows that carry a number
-- (routers/pm_field._is_rfi_number_conflict names this index).
alter table rfis drop constraint if exists rfis_project_id_rfi_number_key;
create unique index if not exists rfis_project_rfi_number_uidx
  on rfis (project_id, rfi_number) where rfi_number is not null;

create unique index if not exists rfis_project_external_id_uidx
  on rfis (project_id, external_id) where external_id is not null;
create unique index if not exists rfis_project_external_number_rev_uidx
  on rfis (project_id, external_number, external_revision) where source = 'procore';
create index if not exists rfis_source_email_idx on rfis (source_email_id);

-- 3. rfi_attachments -------------------------------------------------------------

alter table rfi_attachments add column if not exists external_file_id text;
create unique index if not exists rfi_attachments_external_file_uidx
  on rfi_attachments (rfi_id, external_file_id) where external_file_id is not null;

-- 4. The run queue and slot ledger -----------------------------------------------

create table if not exists pm_rfi_sync_runs (
  id                      uuid primary key default gen_random_uuid(),
  project_id              uuid not null references projects(id) on delete cascade,
  trigger                 text not null check (trigger in ('daily', 'manual', 'email')),
  -- The daily slot instant (the ledger key); null for manual and email runs.
  scheduled_for           timestamptz,
  target_external_rfi_id  text,
  source_email_id         uuid references ingested_emails(id) on delete set null,
  requested_by            uuid references profiles(id) on delete set null,
  status                  text not null default 'queued'
                            check (status in ('queued', 'running', 'done', 'failed')),
  lease_until             timestamptz,
  lease_owner             text,
  attempts                int not null default 0,
  next_attempt_at         timestamptz,
  stats                   jsonb,
  error                   text,
  created_at              timestamptz not null default now(),
  started_at              timestamptz,
  finished_at             timestamptz
);

create unique index if not exists pm_rfi_sync_runs_daily_uidx
  on pm_rfi_sync_runs (project_id, trigger, scheduled_for) where trigger = 'daily';
-- One email run per email: the scan re-reads rows whose updated_at moved.
create unique index if not exists pm_rfi_sync_runs_email_uidx
  on pm_rfi_sync_runs (source_email_id, trigger) where trigger = 'email' and source_email_id is not null;
create index if not exists pm_rfi_sync_runs_open_idx
  on pm_rfi_sync_runs (status, created_at) where status in ('queued', 'running');
create index if not exists pm_rfi_sync_runs_project_idx on pm_rfi_sync_runs (project_id, created_at desc);
create index if not exists pm_rfi_sync_runs_email_idx on pm_rfi_sync_runs (source_email_id);
create index if not exists pm_rfi_sync_runs_requested_by_idx on pm_rfi_sync_runs (requested_by);

-- 5. Email scan checkpoints (one row, id = 'default') ----------------------------

create table if not exists pm_rfi_email_scan_state (
  id                        text primary key default 'default',
  emails_checkpoint         timestamptz,
  classifications_checkpoint timestamptz,
  lease_until               timestamptz,
  lease_owner               text,
  updated_at                timestamptz not null default now()
);

drop trigger if exists pm_rfi_email_scan_state_updated_at on pm_rfi_email_scan_state;
create trigger pm_rfi_email_scan_state_updated_at before update on pm_rfi_email_scan_state
  for each row execute function set_updated_at();

-- RLS: deny-by-default backstop; the service-role backend is the only client.
alter table pm_external_links enable row level security;
alter table pm_external_links force row level security;
alter table pm_rfi_sync_runs enable row level security;
alter table pm_rfi_sync_runs force row level security;
alter table pm_rfi_email_scan_state enable row level security;
alter table pm_rfi_email_scan_state force row level security;

notify pgrst, 'reload schema';
