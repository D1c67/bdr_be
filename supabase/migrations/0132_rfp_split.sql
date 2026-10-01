-- 0132 - RFP Ingestion: Bid File Splitter step (docs/RFP_SPLIT.md section 2).
-- Apply after 0131. DEV ONLY until the release steps in section 8 run.
--
-- After the harvest and before project creation, every harvested document
-- goes through the Bid File Splitter, so a project is born with its
-- documents categorized: drawing sets cut into trade sets, specs / RFP /
-- addenda identified and left intact, non-PDF files identified from their
-- converted PDF or their name. This migration:
--
--   1. widens file_category with the eight trade / document labels the
--      splitter's segments map to (ENUM-ONLY statements first: PG refuses
--      to USE a label added in the same transaction, 0075 precedent; nothing
--      below names them);
--   2. marks promoted project_files rows with the splitter segment and file
--      they came from (a re-cut re-files them) and the "source set" flag
--      (the untouched drawing set kept as `other`, never sent to estimators
--      or vendors); relaxes the addendum metadata CHECK so a split segment
--      filed as an addendum may carry a null number / date until the
--      Estimating Admin fills them in (the API keeps requiring both on a
--      manual upload);
--   3. links bid_split_jobs to the harvest and the project they serve,
--      and bid_split_files to the sandbox file each row was staged from;
--   4. records the split outcome on rfp_harvests and, denormalized, on
--      rfp_created_projects;
--   5. adds the `split` pipeline status between `harvest` and `create` on
--      both invitation sources (status checks dropped by column lookup,
--      the 0124 / 0130 pattern);
--   6. raises the storage buckets 0106 set to 300 MB to 450 MB (the app cap
--      upload_max_bytes moves with it; the Dashboard global limit is a
--      manual release step).
--
-- Every statement is idempotent.

-- ── 1. file_category: the eight new labels (enum-only, nothing uses them) ──

alter type file_category add value if not exists 'civil_drawing';
alter type file_category add value if not exists 'structural_drawing';
alter type file_category add value if not exists 'architectural_drawing';
alter type file_category add value if not exists 'mechanical_drawing';
alter type file_category add value if not exists 'plumbing_drawing';
alter type file_category add value if not exists 'fire_protection_drawing';
alter type file_category add value if not exists 'low_voltage_drawing';
alter type file_category add value if not exists 'rfp';

-- ── 2. project_files: splitter provenance + the source-set flag ───────────

alter table project_files
  add column if not exists bid_split_segment_id uuid references bid_split_segments(id) on delete set null,
  add column if not exists bid_split_file_id    uuid references bid_split_files(id) on delete set null,
  add column if not exists is_source_set        boolean not null default false;

comment on column project_files.bid_split_segment_id is
  'The Bid File Splitter segment this row was promoted from (RFP-created projects). Unique per project, so a retried promotion never duplicates a segment. Null once the segment is re-cut; bid_split_file_id still names the source.';
comment on column project_files.bid_split_file_id is
  'The Bid File Splitter source file this row came from: the segment''s file, the intact original, or the kept source set. A correction in the splitter re-files every row carrying this id.';
comment on column project_files.is_source_set is
  'True on the untouched source PDF kept as `other` after the splitter cut it into segments. Excluded from the estimator package, the Plans & Specs Log, the ZIP export and RFQ attachments.';

create unique index if not exists project_files_split_segment_uidx
  on project_files (project_id, bid_split_segment_id) where bid_split_segment_id is not null;
create index if not exists project_files_split_file_idx
  on project_files (bid_split_file_id) where bid_split_file_id is not null;

-- 0076 required both addendum fields on every addendum row. A split segment
-- filed as an addendum carries the number the segment name parsed (or null)
-- and no issue date; the API still requires both on a manual upload. A
-- non-addendum row must still carry neither.
alter table project_files drop constraint if exists project_files_addendum_meta_ck;
alter table project_files add constraint project_files_addendum_meta_ck check (
  case when category = 'addendum' then
         addendum_number is null
      or (btrim(addendum_number) <> '' and length(addendum_number) <= 40)
       else
         addendum_number is null and addendum_issued_on is null
  end
);

-- ── 3. bid_split_jobs / bid_split_files: the pipeline links ───────────────

alter table bid_split_jobs
  add column if not exists source         text not null default 'manual',
  add column if not exists rfp_harvest_id uuid references rfp_harvests(id) on delete set null,
  add column if not exists project_id     uuid references projects(id) on delete set null;

alter table bid_split_jobs drop constraint if exists bid_split_jobs_source_check;
alter table bid_split_jobs add constraint bid_split_jobs_source_check
  check (source in ('manual', 'rfp'));

comment on column bid_split_jobs.source is
  'manual = uploaded on the /bid-splitter page; rfp = staged by the RFP Ingestion split step from a harvest (created_by null). An rfp job cannot be deleted while its project exists.';
comment on column bid_split_jobs.project_id is
  'The project created from the job''s harvest. Set at creation; corrections in the splitter re-file that project''s documents until its hand-off package has sent.';

create index if not exists bid_split_jobs_harvest_idx
  on bid_split_jobs (rfp_harvest_id) where rfp_harvest_id is not null;
create index if not exists bid_split_jobs_project_idx
  on bid_split_jobs (project_id) where project_id is not null;

alter table bid_split_files
  add column if not exists rfp_sandbox_file_id uuid references rfp_ingest_files(id) on delete set null,
  add column if not exists source_format       text,
  add column if not exists classified_from     text;

alter table bid_split_files drop constraint if exists bid_split_files_classified_from_check;
alter table bid_split_files add constraint bid_split_files_classified_from_check
  check (classified_from is null or classified_from in ('pages', 'converted_pdf', 'name'));

comment on column bid_split_files.source_format is
  'pdf | docx | xlsx | doc | xls | image | other: what the staged file is. Null on rows uploaded by hand (always PDF).';
comment on column bid_split_files.classified_from is
  'pages = the normal triage / per-page split over the PDF; converted_pdf = a non-PDF identified from the sandbox''s converted PDF (triage only, never cut); name = identified from the filename, path and invitation context (one text call). Null on manual rows (pages).';

create index if not exists bid_split_files_sandbox_idx
  on bid_split_files (rfp_sandbox_file_id) where rfp_sandbox_file_id is not null;

-- ── 4. rfp_harvests + rfp_created_projects: the split outcome ─────────────

alter table rfp_harvests
  add column if not exists split_status      text not null default 'none',
  add column if not exists split_job_id      uuid references bid_split_jobs(id) on delete set null,
  add column if not exists split_error       text,
  add column if not exists split_started_at  timestamptz,
  add column if not exists split_finished_at timestamptz;

alter table rfp_harvests drop constraint if exists rfp_harvests_split_status_check;
alter table rfp_harvests add constraint rfp_harvests_split_status_check
  check (split_status in ('none', 'pending', 'running', 'complete', 'failed', 'skipped'));

comment on column rfp_harvests.split_status is
  'The split step''s outcome for this harvest: none (not started), pending (claimed, staging), running (the splitter job is processing), complete, failed (the project is created flagged "split failed"), skipped (flags off, no files, or the harvest already had a project).';

alter table rfp_created_projects
  add column if not exists split_status text,
  add column if not exists split_job_id uuid references bid_split_jobs(id) on delete set null;

comment on column rfp_created_projects.split_status is
  'Copy of rfp_harvests.split_status at creation, for the "Created from RFPs" page (complete / failed / skipped; null on projects created before 0132).';

-- ── 5. The `split` pipeline status on both sources ────────────────────────

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
    'harvest', 'split', 'create',
    'review_llm', 'flagged_unauthorized', 'review_match',
    'done', 'created', 'merged', 'duplicate', 'flagged_auth', 'flagged_no_keywords', 'flagged_llm_no',
    'rejected_by_review', 'failed'));
end
$$;

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
    check (status in ('match', 'harvest', 'split', 'create', 'review_match', 'exists', 'done',
                      'created', 'ignored'));
end
$$;

-- ── 6. Storage buckets: 300 MB -> 450 MB (0106 set project-files) ─────────

update storage.buckets
   set file_size_limit = 471859200   -- 450 MB
 where file_size_limit = 314572800;  -- every bucket 0106 set to 300 MB

notify pgrst, 'reload schema';
