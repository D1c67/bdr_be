-- 0121 - RFP Ingestion sandbox: millisecond widths and the indexes the code
-- actually queries by.
--
-- Follow-up to 0119 (never edit that file; it is already applied to dev and
-- goes to staging and prod at release). Three
-- corrections, all of them idempotent and none of them changing a shape the
-- application reads:
--
-- 1. render_ms / elapsed_ms are int4 but the parent accepts a duration of up
--    to 2**40 ms from the sandbox child (rfp_sandbox_runner._int bounds both
--    at 2**40, roughly 512x the int4 ceiling). A child reporting anything
--    above 2147483647 made the whole 500-row page upsert fail with 22003, so
--    a file whose pages all rendered fine was recorded as failed/interrupted
--    with no page rows at all and its derived objects were left behind. The
--    columns now hold what the parent already admits: bigint.
--
-- 2. rfp_ingest_files_active_idx was on status alone, but every read of
--    rfp_ingest_files is scoped to one run (_next_pending: run_id +
--    status = 'pending' order by created_at limit 1; _run_files: run_id
--    order by created_at). Nothing could ever use it, so it was write cost
--    only. It is replaced by the composite the claim path actually wants.
--
-- 3. rfp_ingest_runs had only (status, created_at desc), which a leading
--    status cannot serve for the unfiltered runs listing (order by
--    created_at desc with an exact count) nor for the retention prune
--    (status in (4 terminal values) and completed_at < cutoff order by
--    completed_at). One index for each, matching 0111's
--    bid_split_jobs_created_idx precedent.

-- ── 1. Millisecond columns hold what the parent accepts ─────────────────

do $$
begin
  if exists (
    select 1 from information_schema.columns
    where table_schema = 'public' and table_name = 'rfp_ingest_pages'
      and column_name = 'render_ms' and data_type <> 'bigint'
  ) then
    alter table rfp_ingest_pages alter column render_ms type bigint;
  end if;

  if exists (
    select 1 from information_schema.columns
    where table_schema = 'public' and table_name = 'rfp_ingest_files'
      and column_name = 'elapsed_ms' and data_type <> 'bigint'
  ) then
    alter table rfp_ingest_files alter column elapsed_ms type bigint;
  end if;
end
$$;

comment on column rfp_ingest_pages.render_ms is
  'Milliseconds the child spent rendering the page. bigint: the parent admits any duration up to 2**40 ms from the child, which does not fit int4.';
comment on column rfp_ingest_files.elapsed_ms is
  'Milliseconds the child ran, as reported in its done frame. bigint for the same reason as rfp_ingest_pages.render_ms.';

-- ── 2. The files index the claim path can use ───────────────────────────

drop index if exists rfp_ingest_files_active_idx;
create index if not exists rfp_ingest_files_pending_idx
  on rfp_ingest_files (run_id, status, created_at)
  where status in ('pending', 'running');

-- ── 3. Runs: unfiltered listing and the retention prune ─────────────────

create index if not exists rfp_ingest_runs_created_idx
  on rfp_ingest_runs (created_at desc);
create index if not exists rfp_ingest_runs_prune_idx
  on rfp_ingest_runs (completed_at)
  where status in ('done', 'done_with_errors', 'failed', 'canceled');

notify pgrst, 'reload schema';
