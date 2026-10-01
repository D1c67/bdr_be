-- 0130 - RFP Ingestion: project creation + automatic project numbers
-- (docs/RFP_CREATE.md sections 2, 3, 6). Apply after 0129.
--
-- Two things land here. First, project numbers become app-assigned: one
-- global counter in the YY.M.NNNN form the company already uses, advanced
-- by a single UPDATE ... RETURNING so concurrent creators get consecutive
-- numbers, wrapping 9999 -> 0001. The counter is seeded from THIS
-- database's own highest existing counter and is never moved by a re-run.
-- Existing project numbers are never rewritten (the seed only reads them;
-- the 0052 unique index on lower(btrim(number)) stays the backstop).
-- Second, the pipeline gains a `create` step and a `created` terminal on
-- both invitation sources, a record of every project the slice created
-- (rfp_created_projects, what the "Created from RFPs" page lists), the
-- harvest -> project link that makes creation idempotent per harvest, the
-- project_files columns that mark promoted documents, and the promotion
-- job type.
--
-- Every statement is idempotent; the two status checks are dropped by
-- column lookup (the 0124 pattern) and re-added with the widened set.

-- ── 1. Project numbers ──────────────────────────────────────────────────

create table if not exists project_number_counter (
  id          smallint primary key check (id = 1),
  last        integer not null check (last between 0 and 9999),
  updated_at  timestamptz not null default now()
);

comment on table project_number_counter is
  'The single global counter behind app-assigned project numbers (YY.M.NNNN). One row, id = 1. Advanced only through next_project_number(); seeded once from the highest existing number and never rewound.';

-- Seed from the highest conforming number already in the table (0 on an
-- empty database, so the first assigned number is 0001). btrim tolerates
-- the one legacy value that carries a trailing space. on conflict do
-- nothing keeps a second apply from moving the counter.
insert into project_number_counter (id, last)
select 1, coalesce(max((regexp_match(btrim(number), '^\d{2}\.\d{1,2}\.(\d{4})B?$'))[1]::integer), 0)
from projects
on conflict (id) do nothing;

alter table project_number_counter enable row level security;
alter table project_number_counter force row level security;

-- One statement, one row lock: callers serialize on the row, each sees the
-- value the previous one left. Service role only.
create or replace function public.next_project_number()
returns integer
language sql
security definer
set search_path = public
as $$
  update project_number_counter
     set last = case when last >= 9999 then 1 else last + 1 end,
         updated_at = now()
   where id = 1
  returning last;
$$;

revoke all on function public.next_project_number() from public;
revoke all on function public.next_project_number() from anon, authenticated;

-- Drafts no longer carry a number (the server assigns one when the draft
-- becomes a project). Old drafts keep whatever they stored.
alter table bid_drafts alter column number drop not null;

-- ── 2. rfp_created_projects ─────────────────────────────────────────────

create table if not exists rfp_created_projects (
  project_id              uuid primary key references projects(id) on delete cascade,
  source_kind             text not null check (source_kind in ('rfp_email', 'rfp_portal')),
  rfp_email_id            uuid references rfp_emails(id) on delete set null,
  portal_invitation_id    uuid references rfp_portal_invitations(id) on delete set null,
  harvest_id              uuid references rfp_harvests(id) on delete set null,
  created_by              uuid references profiles(id) on delete set null,   -- null = System
  automatic               boolean not null default false,
  invitation_method       text,
  sender_display          text,
  sender_was_unauthorized boolean not null default false,
  sender_allowed_by       uuid references profiles(id) on delete set null,
  gc_plan                 text not null check (gc_plan in ('resolved', 'likely', 'created', 'none')),
  likely_gc_id            uuid references general_contractors(id) on delete set null,
  likely_gc_name          text,
  bid_time_unknown        boolean not null default false,
  files_status            text not null default 'none'
                          check (files_status in ('none', 'pending', 'running', 'complete', 'failed')),
  files_claim_token       text,
  files_claimed_at        timestamptz,
  files_promoted          integer not null default 0,
  files_skipped           jsonb not null default '[]'::jsonb,
  files_error             text,
  last_error              text,
  cleared_at              timestamptz,
  cleared_by              uuid references profiles(id) on delete set null,
  restored_at             timestamptz,
  created_at              timestamptz not null default now(),
  updated_at              timestamptz not null default now()
);

comment on table rfp_created_projects is
  'One row per bidding project the RFP Ingestion creation step made (from an email invitation or a portal invitation): who or what created it, the sender marker, how the GC was decided, the document promotion outcome, and whether a person cleared it from the "Created from RFPs" page.';
comment on column rfp_created_projects.files_skipped is
  'Array of {file_path, reason}: harvested documents that were not promoted into the project (hazard, unverified, rejected, too large), capped at 200 entries.';
comment on column rfp_created_projects.bid_time_unknown is
  'actual_bid_at came from a date-only source and was stored at midnight Pacific; "bid_time" joins the missing intake list until a person edits the date.';

create index if not exists rfp_created_projects_created_idx
  on rfp_created_projects (created_at desc);
create index if not exists rfp_created_projects_active_idx
  on rfp_created_projects (created_at desc) where cleared_at is null;
create index if not exists rfp_created_projects_email_idx
  on rfp_created_projects (rfp_email_id) where rfp_email_id is not null;
create index if not exists rfp_created_projects_portal_idx
  on rfp_created_projects (portal_invitation_id) where portal_invitation_id is not null;

drop trigger if exists rfp_created_projects_updated_at on rfp_created_projects;
create trigger rfp_created_projects_updated_at before update on rfp_created_projects
  for each row execute function set_updated_at();

alter table rfp_created_projects enable row level security;
alter table rfp_created_projects force row level security;

-- ── 3. rfp_harvests: the project link and the create claim ──────────────

alter table rfp_harvests
  add column if not exists project_id         uuid references projects(id) on delete set null,
  add column if not exists create_claim_token text,
  add column if not exists create_claimed_at  timestamptz;

comment on column rfp_harvests.project_id is
  'The project created from this harvest. Set once; every later email or invitation sharing the harvest links to it instead of creating another project.';
comment on column rfp_harvests.create_claim_token is
  'Fences creation: two workers holding two emails for the same harvest cannot both create a project from it.';

create index if not exists rfp_harvests_project_idx
  on rfp_harvests (project_id) where project_id is not null;

-- ── 4. rfp_emails: created_project_id + the two statuses ────────────────

alter table rfp_emails
  add column if not exists created_project_id uuid references projects(id) on delete set null;

comment on column rfp_emails.created_project_id is
  'The project the creation step made from this email (or from the harvest it shares). Distinct from match_project_id, which is the EXISTING project a merged or duplicate row attached to.';

create index if not exists rfp_emails_created_project_idx
  on rfp_emails (created_project_id) where created_project_id is not null;

do $$
declare
  con record;
begin
  for con in
    select c.conname
    from pg_constraint c
    join pg_attribute a on a.attrelid = c.conrelid and a.attnum = any (c.conkey)
    where c.conrelid = 'public.rfp_emails'::regclass
      and c.contype = 'c'
      and a.attname = 'status'
      and array_length(c.conkey, 1) = 1
  loop
    execute format('alter table rfp_emails drop constraint %I', con.conname);
  end loop;

  alter table rfp_emails add constraint rfp_emails_status_check check (status in (
    'received', 'auth', 'keywords', 'classify', 'authorize', 'method', 'extract', 'match',
    'harvest', 'create',
    'review_llm', 'flagged_unauthorized', 'review_match',
    'done', 'created', 'merged', 'duplicate', 'flagged_auth', 'flagged_no_keywords', 'flagged_llm_no',
    'rejected_by_review', 'failed'));
end
$$;

-- ── 5. rfp_portal_invitations: the same two statuses ────────────────────

alter table rfp_portal_invitations
  add column if not exists created_project_id uuid references projects(id) on delete set null;

create index if not exists rfp_portal_invitations_created_project_idx
  on rfp_portal_invitations (created_project_id) where created_project_id is not null;

do $$
declare
  con record;
begin
  for con in
    select c.conname
    from pg_constraint c
    join pg_attribute a on a.attrelid = c.conrelid and a.attnum = any (c.conkey)
    where c.conrelid = 'public.rfp_portal_invitations'::regclass
      and c.contype = 'c'
      and a.attname = 'status'
      and array_length(c.conkey, 1) = 1
  loop
    execute format('alter table rfp_portal_invitations drop constraint %I', con.conname);
  end loop;

  alter table rfp_portal_invitations add constraint rfp_portal_invitations_status_check
    check (status in ('match', 'harvest', 'create', 'review_match', 'exists', 'done', 'created', 'ignored'));
end
$$;

-- ── 6. project_files: promoted-document markers ─────────────────────────

alter table project_files
  add column if not exists rfp_harvest_id      uuid references rfp_harvests(id) on delete set null,
  add column if not exists rfp_sandbox_file_id uuid references rfp_ingest_files(id) on delete set null;

comment on column project_files.rfp_sandbox_file_id is
  'The verified Ingestion Sandbox file this row was promoted from. Unique per project, so a retried promotion job never duplicates a document.';

create unique index if not exists project_files_rfp_sandbox_file_uidx
  on project_files (project_id, rfp_sandbox_file_id) where rfp_sandbox_file_id is not null;

-- ── 7. llm_jobs: the promotion job type ─────────────────────────────────

alter table llm_jobs drop constraint if exists llm_jobs_job_type_check;
alter table llm_jobs add constraint llm_jobs_job_type_check
  check (job_type in ('boq_extraction', 'general_material', 'proposal_lines',
                      'bid_split', 'rfp_ingest', 'rfp_harvest',
                      'rfp_portal_scan', 'rfp_portal_harvest', 'rfp_create_files'));

notify pgrst, 'reload schema';
