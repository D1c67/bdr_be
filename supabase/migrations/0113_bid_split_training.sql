-- 0113 - Bid File Splitter: user corrections + training capture.
--
-- The splitter exists to find out whether the model is good enough; that
-- requires knowing exactly where it is wrong. Users can now correct a
-- finished file - change the triage verdict (file_kind), recategorize
-- segments, or redraw the page ranges - and every correction is captured as
-- a training example pairing what the model saw and said with what the user
-- says is right, reviewable from the dev Training page.
--
--   bid_split_files.input_snapshot   exact model input references captured
--                                    before each vision call: prompts,
--                                    sampled page numbers, per-batch page
--                                    lists, render settings, model. Images
--                                    are not inlined; they are re-renderable
--                                    from the stored source PDF.
--   bid_split_files.model_output     pristine model output: validated triage
--                                    verdict + per-page classifications
--                                    captured BEFORE island repair. The
--                                    derived file_kind and segment rows are
--                                    downstream computations.
--   bid_split_files.user_corrected   the user has overridden this file's
--                                    verdict or segments (FE badge).
--
-- bid_split_training_examples holds one example per corrected source file
-- (upsert on file_id; the model side freezes on the first correction, later
-- corrections replace only the user side and the diff).
--
-- DESIGN DECISION: unlike boq_training_examples (cascade), file_id/job_id
-- are ON DELETE SET NULL and every field the Training page needs is
-- denormalized onto the example, because splitter jobs are routinely deleted
-- by testers and the training data must outlive them. training_source_path
-- points at a server-side copy of the source PDF under
-- bid-splits/training/{file_id}/, which job deletion does not sweep
-- (delete_bid_split_prefix only walks bid-splits/{job_id}/{source,output}).

alter table bid_split_files add column input_snapshot jsonb;
alter table bid_split_files add column model_output jsonb;
alter table bid_split_files add column user_corrected boolean not null default false;

comment on column bid_split_files.input_snapshot is
  'Exact model input references captured before each vision call: prompts, sampled page numbers, per-batch page lists, render settings, model. Images are not inlined; they are re-renderable from the stored source PDF.';
comment on column bid_split_files.model_output is
  'Pristine model output: validated triage verdict + per-page classifications captured BEFORE island repair. The derived file_kind and segment rows are downstream computations.';
comment on column bid_split_files.user_corrected is
  'The user has overridden this file''s verdict or segments (correction endpoints, 0113).';

create table bid_split_training_examples (
  id                   uuid primary key default gen_random_uuid(),
  file_id              uuid unique references bid_split_files(id) on delete set null,
  job_id               uuid references bid_split_jobs(id) on delete set null,
  source_filename      text not null,
  page_count           int,
  model                text,
  input_snapshot       jsonb,
  model_output         jsonb not null,
  user_output          jsonb not null,
  diff_json            jsonb not null,
  modified             boolean not null default false,
  training_source_path text,
  corrected_by         uuid references profiles(id) on delete set null,
  corrected_at         timestamptz not null default now(),
  reviewed_by          uuid references profiles(id) on delete set null,
  reviewed_at          timestamptz,
  review_note          text,
  created_at           timestamptz not null default now(),
  updated_at           timestamptz not null default now()
);

comment on table bid_split_training_examples is
  'One training example per corrected splitter source file: the model''s answer as the user saw it vs the user''s corrected version. Denormalized on purpose - examples must survive job deletion (SET NULL FKs, own source-PDF copy).';

create index bid_split_training_examples_time_idx on bid_split_training_examples(corrected_at desc);

alter table bid_split_training_examples enable row level security;
alter table bid_split_training_examples force row level security;

create trigger bid_split_training_examples_updated_at before update on bid_split_training_examples
  for each row execute function set_updated_at();

notify pgrst, 'reload schema';
