-- 0111 - Bid File Splitter (experimental AI tool).
--
-- GCs send bid invitations as huge combined PDFs (whole plan sets, spec books,
-- addenda and the RFP in one or a few files). Splitting those by hand into the
-- documents the estimating team actually works from is slow; this tool tests
-- whether a vision LLM can do it: the user uploads PDFs, the model classifies
-- every page (rendered to an image) into a category, contiguous runs become
-- segments, and the backend splits the source PDF along those page ranges into
-- one output PDF per segment.
--
-- Deliberately standalone: NO foreign key into projects and no project_files
-- rows. The tool is not connected to the bidding pipeline yet (the user keeps
-- testing and tuning it first), so its data lives entirely in these three
-- tables and under the reserved `bid-splits/{job_id}/` storage prefix. Wiring
-- it into intake later is additive.
--
-- Access: the routes are gated by the BID_FILE_SPLITTER_ENABLED env flag
-- (default OFF; 404 while off, sub-app precedent) - see app/core/features.py
-- and routers/bid_splitter.py. Dev-only for now by way of the flag being on
-- only in the dev environment.
--
-- Shape:
--   bid_split_jobs      one upload batch. Aggregate status is derived from its
--                       files (processing / done / done_with_errors / failed).
--   bid_split_files     one source PDF; the unit of work (one llm_jobs entry
--                       per file, so a 40-file drop retries per file, not per
--                       batch). Statuses mirror the queue's domain vocabulary:
--                       pending covers queued/waiting-for-retry (0094).
--   bid_split_segments  one output PDF: a contiguous page range of its source
--                       with the model's category, name, description and
--                       confidence. category 'other' carries the model's free-
--                       text label in other_type so users still learn what the
--                       pages are.
--
-- llm_jobs.job_type CHECK is widened with 'bid_split' (0094 records that
-- adding a queue job type requires touching this constraint).
--
-- Timing/analytics columns (started_at/finished_at, llm_calls, llm_ms,
-- page_count) exist so the dev analytics view can report throughput and
-- durations without scraping the call ledger.

-- ── bid_split_jobs ──────────────────────────────────────────────────────

create table bid_split_jobs (
  id           uuid primary key default gen_random_uuid(),
  status       text not null default 'processing'
               check (status in ('processing', 'done', 'done_with_errors', 'failed')),
  file_count   int not null default 0,
  model        text,  -- llm.active_model('bid_split') at creation, for history
  created_by   uuid references profiles(id) on delete set null,
  completed_at timestamptz,  -- set when the last file goes terminal
  created_at   timestamptz not null default now(),
  updated_at   timestamptz not null default now()
);

comment on table bid_split_jobs is
  'One Bid File Splitter upload batch. Standalone (no project FK) - the tool is not connected to the bidding pipeline yet.';

create index bid_split_jobs_created_idx on bid_split_jobs(created_at desc);

create trigger bid_split_jobs_updated_at before update on bid_split_jobs
  for each row execute function set_updated_at();

alter table bid_split_jobs enable row level security;
alter table bid_split_jobs force row level security;

-- ── bid_split_files ─────────────────────────────────────────────────────

create table bid_split_files (
  id           uuid primary key default gen_random_uuid(),
  job_id       uuid not null references bid_split_jobs(id) on delete cascade,
  filename     text not null,       -- display name exactly as uploaded
  storage_path text not null,       -- bid-splits/{job_id}/source/{uuid}-{name}
  size_bytes   bigint not null,
  page_count   int,
  status       text not null default 'pending'
               check (status in ('pending', 'running', 'done', 'failed')),
  error        text,                -- sanitized user-facing message (llm_errors)
  llm_calls    int not null default 0,
  llm_ms       int not null default 0,  -- total model wall time for this file
  started_at   timestamptz,
  finished_at  timestamptz,
  created_at   timestamptz not null default now(),
  updated_at   timestamptz not null default now()
);

comment on column bid_split_files.status is
  'pending covers queued and waiting-for-retry (queue detail joined from llm_jobs, 0094 precedent).';

create index bid_split_files_job_idx on bid_split_files(job_id, created_at);
create index bid_split_files_status_idx on bid_split_files(status, created_at desc);

create trigger bid_split_files_updated_at before update on bid_split_files
  for each row execute function set_updated_at();

alter table bid_split_files enable row level security;
alter table bid_split_files force row level security;

-- ── bid_split_segments ──────────────────────────────────────────────────

create table bid_split_segments (
  id           uuid primary key default gen_random_uuid(),
  file_id      uuid not null references bid_split_files(id) on delete cascade,
  sort_order   int not null default 0,
  category     text not null check (category in (
                 'electrical_drawings', 'mechanical_drawings',
                 'architectural_drawings', 'specifications',
                 'addenda', 'rfp', 'other')),
  other_type   text,  -- model's free-text label, only meaningful for 'other'
  name         text not null,
  description  text,
  confidence   double precision check (confidence >= 0 and confidence <= 1),
  page_start   int not null,
  page_end     int not null,
  storage_path text not null,       -- bid-splits/{job_id}/output/{uuid}-{name}
  filename     text not null,       -- display name of the split PDF
  size_bytes   bigint not null,
  created_at   timestamptz not null default now(),
  check (page_start >= 1 and page_end >= page_start)
);

comment on column bid_split_segments.confidence is
  'Model confidence 0..1 - the mean of the per-page classification confidences inside the range.';

create index bid_split_segments_file_idx on bid_split_segments(file_id, sort_order);

alter table bid_split_segments enable row level security;
alter table bid_split_segments force row level security;

-- ── llm_jobs: admit the new queue job type ──────────────────────────────

alter table llm_jobs drop constraint llm_jobs_job_type_check;
alter table llm_jobs add constraint llm_jobs_job_type_check
  check (job_type in ('boq_extraction', 'general_material', 'proposal_lines', 'bid_split'));

notify pgrst, 'reload schema';
