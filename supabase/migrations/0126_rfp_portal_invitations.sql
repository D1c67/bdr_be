-- 0126 - RFP Ingestion: NGEM portal invitations (docs/RFP_NGEM_PORTAL.md, section 4).
-- Apply after 0125.
--
-- NGEM (the Nevada Government eMarketplace, an Ionwave supplier portal) sends
-- no usable invitation mail, so the application logs into the company's
-- supplier account on a schedule, reads the "My Invitations" grid, decides
-- with the existing matcher whether each invitation is already a project and,
-- for the new ones, pulls the notes and the bid attachments into an Ingestion
-- Sandbox run. Its own records, next to the email pipeline:
--
--   rfp_portal_runs        : one row per scan (a scheduled slot claimed through
--                            the unique (portal, scheduled_for) ledger, or a
--                            manual "Run now"); at most one queued/running per
--                            portal.
--   rfp_portal_invitations : one row per (portal, agency, bid number), walked
--                            match -> harvest -> done, or resolved to exists
--                            (already a project), parked at review_match for a
--                            person, or ignored by one.
--
-- Shared with the email side: rfp_harvests (rfp_email_id becomes nullable,
-- portal_invitation_id is the other source, exactly one of the two is set),
-- rfp_harvest_sessions (a second row, provider 'ngem', no DDL), the sandbox
-- (rfp_ingest_runs gains source_kind 'rfp_portal' and portal_invitation_id)
-- and the queue (two job types).
--
-- Status vocabulary of rfp_portal_invitations:
--   pending  : match, harvest
--   human    : review_match
--   terminal : exists, done, ignored
--
-- Release order: apply this migration, then deploy. Nothing reads or writes
-- the new tables until RFP_NGEM_ENABLED is on. Every statement here is
-- idempotent.

-- ── 1. rfp_portal_runs: one row per scan ────────────────────────────────

create table if not exists rfp_portal_runs (
  id                         uuid primary key default gen_random_uuid(),
  portal                     text not null,                     -- 'ngem'
  trigger                    text not null check (trigger in ('scheduled', 'manual')),
  scheduled_for              timestamptz,                       -- the slot; null for a manual run
  requested_by               uuid references profiles(id) on delete set null,
  status                     text not null default 'queued'
                             check (status in ('queued', 'running', 'complete', 'failed')),
  claimed_by                 text,
  started_at                 timestamptz,
  finished_at                timestamptz,
  pages_scanned              int not null default 0,
  invitations_seen           int not null default 0,
  invitations_new            int not null default 0,
  invitations_changed        int not null default 0,
  invitations_skipped_closed int not null default 0,
  last_error                 text,
  next_attempt_at            timestamptz,
  created_at                 timestamptz not null default now(),
  updated_at                 timestamptz not null default now()
);

comment on table rfp_portal_runs is
  'One row per portal scan. A scheduled slot is claimed by inserting (portal, scheduled_for): the unique index makes the second worker''s insert a 23505, so a slot runs once across workers and restarts. At most one run per portal is queued or running.';
comment on column rfp_portal_runs.next_attempt_at is
  'Set when the scan job parked the run (logins locked, portal unavailable): the scheduler re-queues the scan once it passes.';

create unique index if not exists rfp_portal_runs_slot_uidx
  on rfp_portal_runs (portal, scheduled_for) where scheduled_for is not null;
create unique index if not exists rfp_portal_runs_active_uidx
  on rfp_portal_runs (portal) where status in ('queued', 'running');
create index if not exists rfp_portal_runs_portal_created_idx
  on rfp_portal_runs (portal, created_at desc);

drop trigger if exists rfp_portal_runs_updated_at on rfp_portal_runs;
create trigger rfp_portal_runs_updated_at before update on rfp_portal_runs
  for each row execute function set_updated_at();

alter table rfp_portal_runs enable row level security;
alter table rfp_portal_runs force row level security;

-- ── 2. rfp_portal_invitations: one row per (portal, agency, bid number) ──

create table if not exists rfp_portal_invitations (
  id                        uuid primary key default gen_random_uuid(),
  portal                    text not null,                      -- 'ngem'
  agency                    text not null,
  agency_key                text not null,                      -- agency lowercased, non-alphanumerics collapsed to '-'
  bid_number                text not null,                      -- the raw number with a trailing "Addendum N" stripped
  bid_number_raw            text not null,                      -- as shown on the grid
  addendum_no               int,
  title                     text not null,
  issued_on                 date,
  close_at                  timestamptz,
  time_left                 text,
  bid_status                text,
  response_status           text,
  response_code             text,
  status_code               text,
  view_url                  text,                               -- session-bound; refreshed each scan; never returned by the API
  status                    text not null default 'match'
                            check (status in ('match', 'harvest', 'review_match', 'exists', 'done', 'ignored')),
  flag_reason               text,
  decided_at_step           text,
  attempts                  int not null default 0,
  next_attempt_at           timestamptz,
  last_error                text,
  match_candidates          jsonb,
  match_project_id          uuid references projects(id) on delete set null,
  match_score               numeric(5,3),
  match_llm_model           text,
  match_llm_prompt_version  text,
  match_weights             jsonb,
  matched_at                timestamptz,
  possible_rebid_project_id uuid references projects(id) on delete set null,
  possible_rebid_score      numeric(5,3),
  excluded_project_ids      uuid[] not null default '{}',
  resolution                text check (resolution in ('system', 'human')),
  resolved_by               uuid references profiles(id) on delete set null,
  resolved_at               timestamptz,
  reopen_reason             text,
  ignored_by                uuid references profiles(id) on delete set null,
  ignored_at                timestamptz,
  ignore_reason             text,
  harvest_id                uuid references rfp_harvests(id) on delete set null,
  harvested_at              timestamptz,
  change_log                jsonb not null default '[]'::jsonb,  -- [{at, field, old, new, run_id}], capped at 50
  first_seen_at             timestamptz not null default now(),
  last_seen_at              timestamptz not null default now(),
  first_seen_run_id         uuid references rfp_portal_runs(id) on delete set null,
  last_seen_run_id          uuid references rfp_portal_runs(id) on delete set null,
  seen_count                int not null default 1,
  missing_since             timestamptz,
  created_at                timestamptz not null default now(),
  updated_at                timestamptz not null default now()
);

comment on table rfp_portal_invitations is
  'One row per invitation on a supplier portal, keyed (portal, agency_key, bid_number). Walked match -> harvest -> done by the sweep and the harvest job; exists when the matcher (or a person) found the project; review_match when a person must decide; ignored by a person.';
comment on column rfp_portal_invitations.view_url is
  'The grid''s per-row link. A session-bound token: refreshed on every scan, used only by the harvest job, never returned by any route.';
comment on column rfp_portal_invitations.change_log is
  'Array of {at, field, old, new, run_id} for every title, close date, addendum number or raw bid number change a later scan saw; oldest entries dropped past 50.';
comment on column rfp_portal_invitations.missing_since is
  'First scan that no longer listed the invitation (it closed, or was withdrawn); cleared when it is seen again.';

create unique index if not exists rfp_portal_invitations_key_uidx
  on rfp_portal_invitations (portal, agency_key, bid_number);
create index if not exists rfp_portal_invitations_sweep_idx
  on rfp_portal_invitations (portal, status, next_attempt_at);
create index if not exists rfp_portal_invitations_project_idx
  on rfp_portal_invitations (match_project_id) where match_project_id is not null;
create index if not exists rfp_portal_invitations_close_idx
  on rfp_portal_invitations (close_at desc);
create index if not exists rfp_portal_invitations_harvest_idx
  on rfp_portal_invitations (harvest_id) where harvest_id is not null;

drop trigger if exists rfp_portal_invitations_updated_at on rfp_portal_invitations;
create trigger rfp_portal_invitations_updated_at before update on rfp_portal_invitations
  for each row execute function set_updated_at();

alter table rfp_portal_invitations enable row level security;
alter table rfp_portal_invitations force row level security;

-- ── 3. rfp_harvests: a second source, exactly one of the two ─────────────
-- `method` has no check constraint (0123); 'ngem' needs no DDL there.

alter table rfp_harvests alter column rfp_email_id drop not null;

alter table rfp_harvests
  add column if not exists portal_invitation_id uuid references rfp_portal_invitations(id) on delete cascade;

comment on column rfp_harvests.portal_invitation_id is
  'The portal invitation whose harvest job created the row (method ngem). Exactly one of rfp_email_id and portal_invitation_id is set.';

do $$
begin
  if not exists (
    select 1 from pg_constraint
    where conrelid = 'public.rfp_harvests'::regclass
      and conname = 'rfp_harvests_source_check'
  ) then
    alter table rfp_harvests add constraint rfp_harvests_source_check
      check ((rfp_email_id is not null) <> (portal_invitation_id is not null));
  end if;
end
$$;

create index if not exists rfp_harvests_portal_invitation_idx
  on rfp_harvests (portal_invitation_id) where portal_invitation_id is not null;

-- ── 4. rfp_ingest_runs: a fourth source kind ────────────────────────────

alter table rfp_ingest_runs
  add column if not exists portal_invitation_id uuid references rfp_portal_invitations(id) on delete set null;

comment on column rfp_ingest_runs.portal_invitation_id is
  'The portal invitation a source_kind rfp_portal run was harvested for.';

do $$
declare
  con record;
begin
  for con in
    select c.conname
    from pg_constraint c
    join pg_attribute a on a.attrelid = c.conrelid and a.attnum = any (c.conkey)
    where c.conrelid = 'public.rfp_ingest_runs'::regclass
      and c.contype = 'c'
      and a.attname = 'source_kind'
      and array_length(c.conkey, 1) = 1
  loop
    execute format('alter table rfp_ingest_runs drop constraint %I', con.conname);
  end loop;

  alter table rfp_ingest_runs add constraint rfp_ingest_runs_source_kind_check
    check (source_kind in ('upload', 'email', 'rfp_email', 'rfp_portal'));
end
$$;

create index if not exists rfp_ingest_runs_portal_invitation_idx
  on rfp_ingest_runs (portal_invitation_id) where portal_invitation_id is not null;

-- ── 5. llm_jobs: the two portal job types ───────────────────────────────

alter table llm_jobs drop constraint if exists llm_jobs_job_type_check;
alter table llm_jobs add constraint llm_jobs_job_type_check
  check (job_type in ('boq_extraction', 'general_material', 'proposal_lines',
                      'bid_split', 'rfp_ingest', 'rfp_harvest',
                      'rfp_portal_scan', 'rfp_portal_harvest'));

notify pgrst, 'reload schema';
