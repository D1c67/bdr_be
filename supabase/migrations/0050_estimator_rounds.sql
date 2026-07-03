-- 0050 — Estimator submission rounds: deliverables lock once sent to the team;
-- later changes go out as explicit revision rounds with per-user review acks.
--
-- Mirror of 0048's team→estimator flow, in the other direction: round 1 is the
-- original estimate/BOQ/markup hand-off; every later send is a numbered round
-- of revised deliverables and/or estimator-supplied additional files. Sent
-- rounds are immutable to the estimator; the team acknowledges each round
-- per-user (change_review_acks powers the red "review the changes" banner).

-- Estimator-supplied supplementary files ("Additional Files" box on the
-- estimator portal). A distinct label — 'additional' (0048) belongs to the
-- team→estimator updates flow and is picked up by /send-file-updates, so
-- estimator uploads must never share it. PG12+ allows ADD VALUE in a
-- transaction as long as the new label is not used later in the same
-- transaction — this migration never references it.
alter type file_category add value if not exists 'estimator_additional';

-- One row per send. The UNIQUE (project_id, round) is the concurrency guard:
-- a double-clicked Send or two racing tabs can't create the same round twice.
create table estimator_submissions (
  id           uuid primary key default gen_random_uuid(),
  project_id   uuid not null references projects(id) on delete cascade,
  -- 0012 actor convention: keep the row if the estimator account goes away.
  estimator_id uuid references profiles(id) on delete set null,
  round        int  not null check (round >= 1),
  submitted_at timestamptz not null default now(),
  -- {"estimate": 1, "markup": 2, ...} — snapshot of what the round contained,
  -- for the team banner and round history (files may later be deleted by
  -- internal writers; the summary keeps the record).
  summary      jsonb,
  unique (project_id, round)
);
create index estimator_submissions_project_idx
  on estimator_submissions(project_id, round desc);
alter table estimator_submissions enable row level security;
alter table estimator_submissions force row level security;

alter table project_files
  -- True for files the external estimator uploaded as deliverables. Internal
  -- uploads never set it, which makes "unsent estimator draft" expressible as
  -- (estimator_deliverable AND submission_round IS NULL) with no role joins.
  add column estimator_deliverable boolean not null default false,
  -- The round this file went to the team in; NULL = still a draft (the open,
  -- unsent round). Sealed by the submit endpoint, never at upload time.
  add column submission_round int;

-- The submit endpoint seals "all drafts on this project" in one UPDATE; keep
-- that lookup cheap.
create index project_files_unsent_draft_idx
  on project_files(project_id)
  where estimator_deliverable and submission_round is null;

-- Per-user "I reviewed the changes" high-water mark (estimator_note_reads
-- pattern, 0029). A user needs review when the latest round-≥2 submission's
-- submitted_at is newer than their mark (or they have no row at all).
create table change_review_acks (
  project_id       uuid not null references projects(id) on delete cascade,
  user_id          uuid not null references profiles(id) on delete cascade,
  last_reviewed_at timestamptz not null,
  primary key (project_id, user_id)
);
alter table change_review_acks enable row level security;
alter table change_review_acks force row level security;

-- ── Backfill ────────────────────────────────────────────────────────────────
-- Flag historical estimator uploads. Dev accounts exercising the estimator
-- flow keep role != 'estimator' and are skipped — dev/test data only.
update project_files pf
   set estimator_deliverable = true
 where pf.category in ('estimate', 'boq', 'markup')
   and pf.uploaded_by in (select id from profiles where role = 'estimator');

-- Every project already handed back (returned_at set) gets a round-1
-- submission stamped at the first return.
insert into estimator_submissions (project_id, estimator_id, round, submitted_at)
select ea.project_id,
       (array_agg(ea.estimator_id order by ea.returned_at))[1],
       1,
       min(ea.returned_at)
  from estimator_assignments ea
 where ea.returned_at is not null
 group by ea.project_id;

-- Seal existing estimator files on those projects into round 1. Projects
-- mid-flight (files uploaded, never submitted) stay NULL = open drafts:
-- deliberate — the estimator finishes uploading and submits round 1 normally.
-- Consequence to coordinate pre-deploy: until that submit, those files stop
-- feeding pickers/exports (they were never formally handed off). Find such
-- projects with:
--   select distinct pf.project_id from project_files pf
--     join profiles p on p.id = pf.uploaded_by
--    where p.role = 'estimator' and pf.category in ('estimate','boq','markup')
--      and not exists (select 1 from estimator_assignments ea
--                       where ea.project_id = pf.project_id
--                         and ea.returned_at is not null);
update project_files pf
   set submission_round = 1
 where pf.estimator_deliverable
   and pf.submission_round is null
   and exists (
     select 1 from estimator_submissions es where es.project_id = pf.project_id
   );

notify pgrst, 'reload schema';
