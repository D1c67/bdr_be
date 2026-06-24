-- 0024 — Send Out overhaul: LLM-drafted proposal scope lines, per-GC generated
-- .docx proposals with send tracking, projects.address, and the 'proposal'
-- file category. The estimator never sees proposals: files.py whitelists
-- (ESTIMATOR_READ / ESTIMATOR_WRITE) exclude the category, and 'proposal' is
-- deliberately NOT in the upload VALID_CATEGORIES — only the generator creates
-- these rows, which keeps gc_id provenance trustworthy.

-- New enum label. PG12+ allows ADD VALUE in a transaction as long as the new
-- label is not used later in the same transaction — this migration never
-- references 'proposal'.
alter type file_category add value if not exists 'proposal';

-- Project street address (intake field; rendered into the proposal cover line).
alter table projects add column address text;

-- GC provenance on generated files — cross-GC isolation, layer 1. SET NULL so
-- file rows (and the audit trail in their filenames) survive a GC delete.
alter table project_files add column gc_id uuid references general_contractors(id) on delete set null;
create index project_files_gc_idx on project_files(gc_id) where gc_id is not null;

-- One row per scope-line generation job (boq_analyses-style status polling).
-- result_json is the immutable raw LLM payload; lines_json is the PA/PM-edited
-- ordered ["line", ...] seeded from it. Edits clear approval.
create table proposal_drafts (
  id           uuid primary key default gen_random_uuid(),
  project_id   uuid not null references projects(id) on delete cascade,
  boq_file_id  uuid references project_files(id) on delete set null,
  status       text not null default 'pending'
               check (status in ('pending', 'running', 'done', 'failed')),
  model        text,
  result_json  jsonb,
  lines_json   jsonb,
  approved_at  timestamptz,
  approved_by  uuid references profiles(id) on delete set null,
  error        text,
  created_by   uuid references profiles(id) on delete set null,
  created_at   timestamptz not null default now(),
  updated_at   timestamptz not null default now()
);
create index proposal_drafts_project_idx on proposal_drafts(project_id, created_at desc);
create index proposal_drafts_boq_file_idx on proposal_drafts(boq_file_id);
create index proposal_drafts_approved_by_idx on proposal_drafts(approved_by);
create index proposal_drafts_created_by_idx on proposal_drafts(created_by);
create trigger proposal_drafts_updated_at before update on proposal_drafts
  for each row execute function set_updated_at();

-- Current per-GC proposal state: one row per (project, GC). Regeneration
-- replaces the row in place; rows for GCs no longer bidding are marked
-- 'superseded' (never deleted — send history must survive for audit).
-- gc_id is RESTRICT on purpose: deleting a GC with proposal history must be
-- blocked, not cascaded — the send record is legal evidence of what we bid.
-- gc_email is a nullable snapshot taken at generation time: generation is
-- allowed for an email-less GC (PA can preview while chasing the address);
-- send is blocked per-row until an email exists and matches the live row.
-- lines_hash = sha256 of the draft's lines_json at generation time; send
-- re-hashes the live draft and refuses to ship a stale document.
create table proposal_sends (
  id            uuid primary key default gen_random_uuid(),
  project_id    uuid not null references projects(id) on delete cascade,
  draft_id      uuid references proposal_drafts(id) on delete set null,
  gc_id         uuid not null references general_contractors(id) on delete restrict,
  gc_name       text not null,
  gc_email      text,
  file_id       uuid references project_files(id) on delete set null,
  lines_hash    text,
  status        text not null default 'generated'
                check (status in ('generated', 'sending', 'sent', 'failed', 'superseded')),
  error         text,
  sent_at       timestamptz,
  sent_by       uuid references profiles(id) on delete set null,
  email_log_id  uuid references email_log(id) on delete set null,
  created_at    timestamptz not null default now(),
  updated_at    timestamptz not null default now(),
  unique (project_id, gc_id)
);
create index proposal_sends_project_idx on proposal_sends(project_id);
create index proposal_sends_gc_idx on proposal_sends(gc_id);
create index proposal_sends_draft_idx on proposal_sends(draft_id);
create index proposal_sends_file_idx on proposal_sends(file_id);
create index proposal_sends_email_log_idx on proposal_sends(email_log_id);
create index proposal_sends_sent_by_idx on proposal_sends(sent_by);
create trigger proposal_sends_updated_at before update on proposal_sends
  for each row execute function set_updated_at();

-- RLS deny-by-default (the service-role backend bypasses it); see 0007.
alter table proposal_drafts enable row level security;
alter table proposal_sends  enable row level security;
