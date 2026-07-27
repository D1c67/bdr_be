-- 0076 — Estimator send batches (the "Plans and specs log") + addendum metadata.
--
-- Today ASSIGN == SEND (estimator.py:140 assign_estimator) and nothing records
-- WHO received WHICH files: project_files.sent_to_estimators_at (0048:26)
-- stores only WHEN, and only for the update categories (stamped at
-- estimator.py:234 and estimator.py:405, both filtered), so the initial package
-- leaves no per-file trace at all. Recipients survive only in email_log.to_addrs
-- and audit_log payloads, which no UI reads. This migration adds the missing
-- send-batch grain:
--
--   file_send_batches       one row per outbound send (initial | revision | reassign)
--   file_send_recipients    M:N — a batch may go to several estimators, and the
--                           delivered EMAIL ADDRESS is the evidence, not the
--                           profile id (which goes NULL on account deletion)
--   file_send_batch_files   M:N — a reassign batch re-sends files that already
--                           belong to an earlier batch, so this CANNOT be a
--                           send_batch_id column on project_files
--
-- Plus the addendum metadata (number + issue date) that 0075's new label needs;
-- it lives here because a CHECK naming 'addendum' may not run in the same
-- transaction that added the label.
--
-- Runs AFTER 0075 as its own transaction, so it may reference 'addendum'
-- freely. Independent of 0057_stage_categories.sql (that migration touches
-- projects / stage_events / project_category_state only) — 0075+0076 may be
-- applied before or after it.
--
-- Re-runnable: every DDL statement is guarded, and every backfill INSERT is
-- guarded on the exact row it would create, never merely on "this project has
-- no batches". A second apply is a no-op.
--
-- PostgREST cache: DDL, so the reload at the bottom is required.

-- ── 1. Addendum metadata on project_files ──────────────────────────────────
alter table project_files
  -- TEXT, not int: real addenda are numbered "1", "02", "3A", "ADD-4".
  -- Length is capped in the CHECK (40) rather than varchar(n) so widening later
  -- is a constraint swap, not a table rewrite.
  add column if not exists addendum_number    text,
  -- DATE, not timestamptz: the issue date printed on the addendum cover sheet
  -- is a calendar date with no timezone meaning. A timestamptz renders a day
  -- early/late for the ur/hi/fil/sw/ceb locales.
  add column if not exists addendum_issued_on date;

-- Both required for an addendum, both NULL for everything else. The API mirrors
-- this (files.py upload_file) but the DB is the backstop: a handcrafted request
-- must never persist an addendum with no number, nor an addendum_number on a
-- drawing. Existing rows all satisfy the ELSE branch (both columns are new and
-- therefore NULL), so the validating scan cannot fail.
alter table project_files drop constraint if exists project_files_addendum_meta_ck;
alter table project_files add constraint project_files_addendum_meta_ck check (
  case when category = 'addendum' then
         addendum_number is not null
     and btrim(addendum_number) <> ''
     and length(addendum_number) <= 40
     and addendum_issued_on is not null
       else
         addendum_number is null and addendum_issued_on is null
  end
);

-- Grouping index for the log ("Addendum 3" is routinely several files: the
-- narrative plus the revised sheets). Predicate is addendum_number IS NOT NULL,
-- not category = 'addendum': enum_out is STABLE and index predicates must be
-- IMMUTABLE. The CHECK above makes the two predicates equivalent.
create index if not exists project_files_addendum_idx
  on project_files(project_id, addendum_number)
  where addendum_number is not null;

-- DELIBERATELY NO unique constraint on (project_id, addendum_number): one
-- addendum is normally issued as several files. Uniqueness would make the
-- common case unrepresentable.

-- ── 2. Send batches ────────────────────────────────────────────────────────
-- A NEW type, so its labels ARE usable later in this same transaction — the
-- ADD VALUE restriction applies only to pre-existing types. Guarded create
-- matches 0057's pg_type idiom (CREATE TYPE has no IF NOT EXISTS).
do $$ begin
  if not exists (select 1 from pg_type where typname = 'file_send_kind') then
    create type file_send_kind as enum ('initial', 'revision', 'reassign');
  end if;
end $$;

create table if not exists file_send_batches (
  id            uuid primary key default gen_random_uuid(),
  project_id    uuid not null references projects(id) on delete cascade,
  -- Three values, not four: `reconstructed` below is a flag, not a kind, so a
  -- backfilled initial send still sorts and labels as an initial send.
  kind          file_send_kind not null,
  -- Optional "message from the G3 team" shown at the top of the email and in
  -- the log row. NULL for reconstructed history (never recorded).
  message       text,
  -- 0012 actor convention: nullable actor -> ON DELETE SET NULL (keep the
  -- record, drop the link). Matches email_log.sent_by (0012:8-10) and
  -- project_files.uploaded_by (0012:20-22). Losing an employee must not delete
  -- send history.
  sent_by       uuid references profiles(id) on delete set null,
  sent_at       timestamptz not null default now(),
  -- Category counts snapshotted AT SEND TIME: {"drawing":12,"specification":3,
  -- "addendum":1,"revision":2,"additional":0,"addendum_numbers":["3","3A"]}.
  -- Rendered as the log headline instead of counting the live join, because a
  -- later file delete must not retroactively rewrite what was sent. Exactly the
  -- reasoning behind estimator_submissions.summary (0050:27-30).
  summary       jsonb not null default '{}'::jsonb,
  -- TRUE for rows this migration reconstructed from history rather than
  -- recorded live. The UI labels these and hides the (empty) message block.
  reconstructed boolean not null default false,
  created_at    timestamptz not null default now()
);

-- Double-send guard, mirroring estimator_submissions' UNIQUE (project_id, round)
-- (0050:31): the one-shot initial hand-off may happen exactly once per project.
-- The API claims this row BEFORE sending the email, so two racing tabs cannot
-- both email the initial package — one wins, the other gets a 23505 -> 409.
create unique index if not exists file_send_batches_one_initial_idx
  on file_send_batches(project_id) where kind = 'initial';

-- Re-run guard for the reconstruction blocks below. Live rows are excluded so
-- two genuine same-second sends are never rejected in production.
create unique index if not exists file_send_batches_reconstructed_dedupe_idx
  on file_send_batches(project_id, kind, sent_at) where reconstructed;

-- The log query: newest batch first, per project.
create index if not exists file_send_batches_project_idx
  on file_send_batches(project_id, sent_at desc);

-- RLS deny-by-default with ZERO policies (0007 + 0055 + 0057 convention).
-- Supabase grants anon/authenticated access to new public tables by default;
-- this is the only thing between a leaked anon key and every estimator's email
-- address. FORCE closes the table-owner path. The service-role backend carries
-- BYPASSRLS and is unaffected.
alter table file_send_batches enable row level security;
alter table file_send_batches force  row level security;

-- Recipients are M:N. Identity within a batch is the EMAIL ADDRESS, not the
-- profile id: the address is the immutable evidence of what was delivered, and
-- estimator_id goes NULL if the account is deleted (0012 SET NULL convention).
create table if not exists file_send_recipients (
  batch_id     uuid not null references file_send_batches(id) on delete cascade,
  estimator_id uuid references profiles(id) on delete set null,
  email        text not null,
  full_name    text,   -- snapshot at send time; profile renames don't rewrite history
  -- PER-RECIPIENT, not per-batch: one email is sent per recipient so the
  -- personalised greeting survives and no estimator learns another's address
  -- (graph_email.send_mail puts every address in toRecipients — there is no
  -- BCC path). SET NULL so purging email_log never destroys the send record.
  email_log_id uuid references email_log(id) on delete set null,
  -- Also de-dupes a double-add of the same person to one batch.
  primary key (batch_id, email)
);
-- "Which batches were sent to me" — the external estimator's log scope.
create index if not exists file_send_recipients_estimator_idx
  on file_send_recipients(estimator_id);

alter table file_send_recipients enable row level security;
alter table file_send_recipients force  row level security;

-- Files are M:N: a 'reassign' batch re-sends files that already belong to an
-- earlier batch, so a send_batch_id column on project_files cannot express it.
create table if not exists file_send_batch_files (
  batch_id uuid not null references file_send_batches(id) on delete cascade,
  -- CASCADE: a batch must never point at a nonexistent row. The `summary`
  -- snapshot means the log headline survives the delete regardless.
  file_id  uuid not null references project_files(id) on delete cascade,
  primary key (batch_id, file_id)
);
-- Reverse lookup: "which sends contained this file" (per-file provenance).
create index if not exists file_send_batch_files_file_idx
  on file_send_batch_files(file_id);

alter table file_send_batch_files enable row level security;
alter table file_send_batch_files force  row level security;

-- ── 3. Backfill — MANDATORY, not optional ──────────────────────────────────
-- Without this, every project already handed off reads as "never sent": the
-- one-shot Upload button reappears on a project whose drawings are frozen, and
-- the external portal (whose file surface IS the log after this change) shows
-- an empty state to estimators mid-bid. Each INSERT is guarded on the exact row
-- it would create, so re-running the file is a no-op.
do $$
declare
  -- assign_estimator stamps the assignment (estimator.py:223) and the pending
  -- update files (estimator.py:234) in two statements inside ONE request, so
  -- the two now() values differ by milliseconds. Without this window the same
  -- send reconstructs as BOTH an assignment batch and a phantom revision batch.
  v_window constant interval := interval '5 seconds';
begin
  -- A. Assignment-driven batches. Earliest send per project = initial hand-off;
  --    every later distinct timestamp = a reassign.
  insert into file_send_batches (project_id, kind, sent_at, sent_by, reconstructed)
  select s.project_id,
         (case when s.seq = 1 then 'initial' else 'reassign' end)::file_send_kind,
         s.sent_at, s.assigned_by, true
    from (
      select project_id,
             sent_to_estimator_at as sent_at,
             (array_agg(assigned_by) filter (where assigned_by is not null))[1] as assigned_by,
             row_number() over (partition by project_id order by sent_to_estimator_at) as seq
        from estimator_assignments
       where sent_to_estimator_at is not null
       group by project_id, sent_to_estimator_at
    ) s
   where not exists (
     select 1 from file_send_batches b
      where b.project_id = s.project_id and b.sent_at = s.sent_at
   )
   -- Second, coarser re-run guard. Once the app is live it writes the batch and
   -- stamps estimator_assignments.sent_to_estimator_at from two different now()
   -- values, so the exact-timestamp guard above would MISS a live batch and
   -- re-insert it — and for seq = 1 that is a file_send_batches_one_initial_idx
   -- unique violation that aborts the whole apply. Reconstruction only ever
   -- concerns history predating this feature, so a project that already owns a
   -- live batch was necessarily reconstructed by the first apply and must be
   -- skipped. No effect on the first apply: the table is empty, so no project
   -- has a live batch.
   and not exists (
     select 1 from file_send_batches b2
      where b2.project_id = s.project_id and not b2.reconstructed
   );

  -- B. Revision batches: one per distinct sent_to_estimators_at over the update
  --    categories, EXCLUDING stamps that coincide with an assignment send
  --    (those files rode along in the package — estimator.py:232-240).
  --    GUARDED ON THE EXACT ROW, not on "this project has no live batch": a
  --    project whose only batches are run-1 reconstructions would otherwise
  --    re-insert a duplicate on every re-run.
  --    'addendum' is intentionally absent from the category list: the label did
  --    not exist before 0075, so no historical row can carry it.
  insert into file_send_batches (project_id, kind, sent_at, reconstructed)
  select distinct pf.project_id, 'revision'::file_send_kind, pf.sent_to_estimators_at, true
    from project_files pf
   where pf.sent_to_estimators_at is not null
     and pf.category in ('revision', 'additional')
     and not exists (
       select 1 from file_send_batches b
        where b.project_id = pf.project_id
          and b.kind in ('initial', 'reassign')
          and pf.sent_to_estimators_at between b.sent_at - v_window and b.sent_at + v_window
     )
     and not exists (
       select 1 from file_send_batches b3
        where b3.project_id = pf.project_id
          and b3.kind = 'revision'
          and b3.sent_at = pf.sent_to_estimators_at
     )
     -- Same coarse re-run guard as block A, for the same reason: stamp_sent()
     -- and the batch row take their now() separately, so a live revision send
     -- would otherwise reconstruct as a duplicate on every later apply.
     and not exists (
       select 1 from file_send_batches b4
        where b4.project_id = pf.project_id and not b4.reconstructed
     );

  -- C. Recipients for assignment-driven batches: the assignments carrying that
  --    exact send timestamp.
  insert into file_send_recipients (batch_id, estimator_id, email, full_name)
  select b.id, ea.estimator_id, p.email, p.full_name
    from file_send_batches b
    join estimator_assignments ea
      on ea.project_id = b.project_id and ea.sent_to_estimator_at = b.sent_at
    join profiles p on p.id = ea.estimator_id
   where b.reconstructed and b.kind in ('initial', 'reassign') and p.email is not null
  on conflict do nothing;

  -- D. Recipients for reconstructed revision batches: everyone ACTIVE and
  --    already SENT at that moment — _active_assignments' predicate
  --    (estimator.py:92-104) evaluated at the historical timestamp.
  insert into file_send_recipients (batch_id, estimator_id, email, full_name)
  select distinct b.id, ea.estimator_id, p.email, p.full_name
    from file_send_batches b
    join estimator_assignments ea on ea.project_id = b.project_id
    join profiles p on p.id = ea.estimator_id
   where b.reconstructed and b.kind = 'revision'
     and ea.sent_to_estimator_at is not null
     and ea.sent_to_estimator_at <= b.sent_at
     and (ea.revoked_at is null or ea.revoked_at > b.sent_at)
     and (ea.expires_at is null or ea.expires_at > b.sent_at)
     and p.email is not null
  on conflict do nothing;

  -- E. Files in assignment-driven batches: the package as it stood then — every
  --    drawing/spec uploaded by that point plus every update already sent by
  --    that point (what _package_files (estimator.py:59-76) returns).
  insert into file_send_batch_files (batch_id, file_id)
  select b.id, pf.id
    from file_send_batches b
    join project_files pf on pf.project_id = b.project_id
   where b.reconstructed and b.kind in ('initial', 'reassign')
     and (
       (pf.category in ('drawing', 'specification') and pf.created_at <= b.sent_at + v_window)
       or (pf.sent_to_estimators_at is not null
           and pf.sent_to_estimators_at <= b.sent_at + v_window)
     )
  on conflict do nothing;

  -- F. Files in reconstructed revision batches: exact — the stamp IS the key.
  insert into file_send_batch_files (batch_id, file_id)
  select b.id, pf.id
    from file_send_batches b
    join project_files pf
      on pf.project_id = b.project_id and pf.sent_to_estimators_at = b.sent_at
   where b.reconstructed and b.kind = 'revision'
  on conflict do nothing;

  -- G. Snapshot summary counts for every reconstructed batch from the rows just
  --    linked, so the log headline survives a later file delete. Only rows still
  --    at the '{}' default are touched, which is what makes a re-run a no-op.
  update file_send_batches b
     set summary = coalesce(c.counts, '{}'::jsonb)
    from (
      select t.batch_id, jsonb_object_agg(t.category, t.n) as counts
        from (
          select bf.batch_id, pf.category::text as category, count(*) as n
            from file_send_batch_files bf
            join project_files pf on pf.id = bf.file_id
           group by bf.batch_id, pf.category
        ) t
       group by t.batch_id
    ) c
   where b.id = c.batch_id and b.reconstructed and b.summary = '{}'::jsonb;
end $$;

-- Reload PostgREST's schema cache so the new columns/tables are visible
-- immediately (this file is DDL; without it the API 400s on the new fields).
notify pgrst, 'reload schema';
