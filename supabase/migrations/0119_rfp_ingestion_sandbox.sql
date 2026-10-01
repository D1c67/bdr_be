-- 0119 - RFP Ingestion sandbox.
--
-- First slice of RFP Ingestion: inbound RFP PDFs (an email's attachments, or
-- a dev upload) are treated as attacker-controlled bytes. The API process
-- never parses them; it quarantines the raw file, spawns a credential-free,
-- rlimit-bounded sandbox child that renders every page to JPEG and extracts
-- the text, re-validates what came back at the byte level, and uploads only
-- those derived outputs. Downstream consumers (the later agent slice) read
-- the derived outputs and never the raw file. Design and the trust boundary:
-- bdr_be/docs/RFP_INGESTION_SANDBOX.md; the child <-> parent contract and
-- every verdict code: app/sandbox/protocol.py.
--
-- Deliberately standalone, like the Bid File Splitter (0111): NO foreign key
-- into projects and nothing in the bidding pipeline reads these tables yet.
-- Routes are gated by RFP_INGEST_ENABLED (default OFF; 404 while off) and
-- are dev-only.
--
-- Shape:
--   rfp_ingest_runs    one batch (an email's attachments or one upload set).
--                      status: staging (upload run collecting files) ->
--                      pending (queued) -> running -> done / done_with_errors
--                      / failed; canceled is sticky; expired = the retention
--                      prune removed both storage prefixes (rows and
--                      manifests are kept). Counters are derived from the
--                      files when the run goes terminal.
--   rfp_ingest_files   one PDF; the unit of work. status pending -> running
--                      (claim_token fences every write) -> verified /
--                      verified_with_gaps / rejected (permanent, never
--                      retried) / failed (retryable). reject_code is the
--                      verdict code for BOTH rejected and failed rows.
--                      manifest (jsonb) carries the whole verification
--                      record; hazards the audit inventory. phase /
--                      pages_done / last_progress_at / child_pid are the
--                      live progress fields the runner refreshes while the
--                      child runs.
--   rfp_ingest_pages   one row per page index of a processed file: ok pages
--                      carry the derived object paths and dimensions, failed
--                      pages a code and detail (placeholders for gaps).
--
-- Queue: one llm_jobs row per RUN (job_type 'rfp_ingest'); the job_type
-- CHECK is widened (drop then add, 0111 precedent). claim_llm_jobs gains a
-- job_types filter so the worker can claim sandbox runs against a separate
-- capacity from LLM jobs; the 3-parameter overload is dropped first so
-- PostgREST RPC resolution stays unambiguous (existing callers pass three
-- named arguments and match the defaulted fourth).
--
-- Storage: two NEW private buckets (0008 precedent; no storage policies, the
-- service-role backend is the only reader/writer). rfp-quarantine holds the
-- raw files (never signed); rfp-derived holds the outputs (signed URLs only
-- here). Supabase enforces min(project global limit, bucket limit), so the
-- Dashboard global limit must stay >= 300 MB (0106).
--
-- Every statement is idempotent where Postgres allows it, so a partial manual
-- apply can be re-run.
--
-- (Numbering: 0118 is the highest prefix on every branch as of 2026-09-09;
-- this file takes 0119 so the unique-prefix rule holds when the branches
-- merge, the 0116/0117 pattern.)

-- ── rfp_ingest_runs ─────────────────────────────────────────────────────

create table if not exists rfp_ingest_runs (
  id               uuid primary key default gen_random_uuid(),
  source_kind      text not null check (source_kind in ('upload', 'email')),
  email_id         uuid references ingested_emails(id) on delete set null,
  status           text not null default 'staging'
                   check (status in ('staging', 'pending', 'running', 'done',
                                     'done_with_errors', 'failed', 'canceled',
                                     'expired')),
  error            text,              -- app-authored sentence, never an exception
  file_count       int not null default 0,
  files_verified   int not null default 0,
  files_gapped     int not null default 0,
  files_rejected   int not null default 0,
  files_failed     int not null default 0,
  pages_total      int not null default 0,
  limits           jsonb,             -- the exact limits document the run used
  limits_hash      text,              -- protocol.limits_hash of it
  sandbox_version  text,
  protocol_version int,
  created_by       uuid references profiles(id) on delete set null,
  started_at       timestamptz,
  completed_at     timestamptz,
  created_at       timestamptz not null default now(),
  updated_at       timestamptz not null default now()
);

comment on table rfp_ingest_runs is
  'One RFP Ingestion sandbox batch. Standalone (no project FK); status transitions are compare-and-swap in app/services/rfp_ingest.py.';
comment on column rfp_ingest_runs.status is
  'staging = upload run still collecting files; pending = queued; running; done / done_with_errors / failed = terminal; canceled is sticky; expired = retention prune removed the storage prefixes (rows kept).';
comment on column rfp_ingest_runs.error is
  'App-authored user-facing sentence (protocol.VERDICT_MESSAGES or a fixed string), never str(exc), a filename or child output.';

create index if not exists rfp_ingest_runs_status_idx
  on rfp_ingest_runs (status, created_at desc);
create index if not exists rfp_ingest_runs_email_idx
  on rfp_ingest_runs (email_id);
create index if not exists rfp_ingest_runs_created_by_idx
  on rfp_ingest_runs (created_by);

drop trigger if exists rfp_ingest_runs_updated_at on rfp_ingest_runs;
create trigger rfp_ingest_runs_updated_at before update on rfp_ingest_runs
  for each row execute function set_updated_at();

-- RLS deny-by-default: no policies; all access flows through the service-role
-- backend (which bypasses RLS), matching 0007/0032/0116.
alter table rfp_ingest_runs enable row level security;
alter table rfp_ingest_runs force row level security;

-- ── rfp_ingest_files ────────────────────────────────────────────────────

create table if not exists rfp_ingest_files (
  id                   uuid primary key default gen_random_uuid(),
  run_id               uuid not null references rfp_ingest_runs(id) on delete cascade,
  source_attachment_id uuid references ingested_email_attachments(id) on delete set null,
  filename             text not null,   -- display only, sanitized
  declared_mime        text,            -- recorded, never trusted
  size_bytes           bigint,          -- measured after materialization
  sha256               text,
  quarantine_path      text,            -- rfp-quarantine: {run_id}/{file_id}/source.pdf
  status               text not null default 'pending'
                       check (status in ('pending', 'running', 'verified',
                                         'verified_with_gaps', 'rejected',
                                         'failed')),
  reject_code          text,
  error                text,            -- app-authored sentence (protocol.VERDICT_MESSAGES)
  claim_token          text,            -- fences every write while running
  phase                text,            -- materialize | sandbox | verify | upload
  pages_done           int not null default 0,
  last_progress_at     timestamptz,
  child_pid            int,
  page_count           int,
  pages_ok             int,
  pages_failed         int,
  hazards              jsonb,
  manifest             jsonb,
  derived_prefix       text,            -- rfp-derived: {run_id}/{file_id}
  images_pdf_paths     jsonb,           -- ["{prefix}/images-001.pdf", ...]
  text_path            text,            -- {prefix}/text.json
  restarts             int not null default 0,
  elapsed_ms           int,
  sandbox_version      text,
  protocol_version     int,
  limits_hash          text,
  started_at           timestamptz,
  finished_at          timestamptz,
  created_at           timestamptz not null default now(),
  updated_at           timestamptz not null default now()
);

comment on table rfp_ingest_files is
  'One quarantined PDF of an RFP Ingestion run; the unit of sandbox work. rejected is permanent (never retried), failed is retryable.';
comment on column rfp_ingest_files.reject_code is
  'Verdict code for both rejected (permanent) and failed (retryable) rows. No CHECK constraint on purpose: app/sandbox/protocol.py (REJECT_CODES and FAIL_CODES) is the authority and changes with the sandbox, not with a migration.';
comment on column rfp_ingest_files.claim_token is
  'Set by the runner that claimed the file (status running); every later write to the row is fenced on it so a stale runner cannot clobber a fresh claim.';
comment on column rfp_ingest_files.manifest is
  'The whole verification record: identity, sniff flags, structure, hazards, bounds hit, output integrity, provenance (see docs section 2).';

create index if not exists rfp_ingest_files_run_idx
  on rfp_ingest_files (run_id, created_at);
create index if not exists rfp_ingest_files_active_idx
  on rfp_ingest_files (status) where status in ('pending', 'running');
-- Within a run a duplicate sha256 is refused: the CAS update that sets sha256
-- catches the 23505 and records rejected/duplicate.
create unique index if not exists rfp_ingest_files_run_sha_uidx
  on rfp_ingest_files (run_id, sha256) where sha256 is not null;
create index if not exists rfp_ingest_files_source_attachment_idx
  on rfp_ingest_files (source_attachment_id);

drop trigger if exists rfp_ingest_files_updated_at on rfp_ingest_files;
create trigger rfp_ingest_files_updated_at before update on rfp_ingest_files
  for each row execute function set_updated_at();

-- RLS deny-by-default: no policies; all access flows through the service-role
-- backend (which bypasses RLS), matching 0007/0032/0116.
alter table rfp_ingest_files enable row level security;
alter table rfp_ingest_files force row level security;

-- ── rfp_ingest_pages ────────────────────────────────────────────────────

create table if not exists rfp_ingest_pages (
  id             uuid primary key default gen_random_uuid(),
  file_id        uuid not null references rfp_ingest_files(id) on delete cascade,
  page_index     int not null,          -- 0-based
  status         text not null check (status in ('ok', 'failed')),
  code           text,                  -- failed: protocol page failure code
  detail         text,                  -- failed: capped detail
  width_pt       numeric,
  height_pt      numeric,
  rotation       int,
  tier           text,                  -- full | full_small
  thumb_path     text,                  -- rfp-derived: {prefix}/thumb/NNNN.jpg
  full_path      text,                  -- rfp-derived: {prefix}/full/NNNN.jpg
  thumb_w        int,
  thumb_h        int,
  full_w         int,
  full_h         int,
  text_chars     int,
  text_truncated boolean not null default false,
  text_hazards   jsonb,                 -- protocol.TEXT_HAZARD_KEYS counts
  render_ms      int,
  created_at     timestamptz not null default now(),
  -- The runner upserts on (file_id, page_index) and ignores duplicates.
  constraint rfp_ingest_pages_file_page_uq unique (file_id, page_index)
);

comment on table rfp_ingest_pages is
  'One row per page index of a processed RFP Ingestion file; failed pages are the placeholders that make a verified_with_gaps file usable.';

-- RLS deny-by-default: no policies; all access flows through the service-role
-- backend (which bypasses RLS), matching 0007/0032/0116.
alter table rfp_ingest_pages enable row level security;
alter table rfp_ingest_pages force row level security;

-- ── llm_jobs: admit the new queue job type ──────────────────────────────

alter table llm_jobs drop constraint if exists llm_jobs_job_type_check;
alter table llm_jobs add constraint llm_jobs_job_type_check
  check (job_type in ('boq_extraction', 'general_material', 'proposal_lines',
                      'bid_split', 'rfp_ingest'));

-- ── claim_llm_jobs: optional job_types filter ───────────────────────────

-- The worker claims in two passes per tick (LLM job types against the LLM
-- capacity, then ['rfp_ingest'] against the sandbox capacity), so the claim
-- needs a type filter. NULL keeps the 0094 behavior (any type). The old
-- 3-parameter overload is dropped first: PostgREST resolves RPCs by argument
-- names and two candidates for {worker_id, lease_seconds, max_jobs} would be
-- ambiguous. The body is the 0094 body plus the one filter line.
drop function if exists claim_llm_jobs(text, int, int);
create or replace function claim_llm_jobs(
  worker_id text,
  lease_seconds int,
  max_jobs int,
  job_types text[] default null
)
returns setof llm_jobs
language sql
volatile
security invoker
set search_path = pg_catalog, public
as $$
  update llm_jobs
  set status = 'running',
      claimed_by = worker_id,
      lease_expires_at = now() + make_interval(secs => lease_seconds),
      attempts = attempts + 1,
      started_at = coalesce(started_at, now())
  where id in (
    select id
    from llm_jobs
    where status = 'queued' and next_attempt_at <= now()
      and (job_types is null or job_type = any(job_types))
    order by priority, created_at
    limit max_jobs
    for update skip locked
  )
  returning *;
$$;

-- ── Storage buckets ─────────────────────────────────────────────────────

-- Private, policy-less (0008 precedent). Quarantine holds attacker-controlled
-- bytes and is never signed; derived holds sandbox outputs and is the only
-- bucket signed URLs are minted for. Limits are explicit so a bucket never
-- inherits a lower project default (0106 precedent).
insert into storage.buckets (id, name, public, file_size_limit)
values ('rfp-quarantine', 'rfp-quarantine', false, 314572800)  -- 300 MB
on conflict (id) do nothing;

insert into storage.buckets (id, name, public, file_size_limit)
values ('rfp-derived', 'rfp-derived', false, 209715200)  -- 200 MB
on conflict (id) do nothing;

notify pgrst, 'reload schema';
