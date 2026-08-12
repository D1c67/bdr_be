-- 0109: Files attached to saved New Bid drafts.
--
-- A draft (0107) can now hold the files the user attached in the New Bid modal
-- before the project exists. Draft objects live in the SAME private
-- `project-files` bucket as project files, under a reserved storage prefix
-- `drafts/{draft_id}/`, keyed with the same `{category}/{uuid}-{filename}`
-- scheme a project upload uses. At transfer (POST /bid-drafts/{id}/transfer)
-- each object is MOVED (not copied) onto the project - the prefix swaps to
-- `{project_id}/` and the rest of the key is preserved, so the landed key is
-- identical to what a fresh upload of that file would have produced.
--
-- Addendum metadata (number, issue date, doc_type) is OPTIONAL at draft stage:
-- a draft saves incomplete work, so an addendum may sit here with no number or
-- date yet. The transfer endpoint enforces completeness (number + non-future
-- issue date) BEFORE moving anything; project_files_addendum_meta_ck (0076)
-- remains the backstop on the destination table. The CHECKs below only pin
-- what can never be right at any stage: metadata on a non-addendum row, an
-- over-long number, a doc_type outside the project_files domain.

create table bid_draft_files (
  id                 uuid primary key default gen_random_uuid(),
  draft_id           uuid not null references bid_drafts(id) on delete cascade,
  -- Only the intake package categories: the New Bid modal attaches the initial
  -- blocks plus addenda, nothing post-hand-off.
  category           text not null check (
    category in ('drawing', 'electrical_drawing', 'specification', 'addendum')
  ),
  filename           text not null,
  storage_path       text not null,
  size_bytes         bigint,
  content_type       text,
  -- Optional at draft stage (see header); shape-checked but never required.
  addendum_number    text,
  addendum_issued_on date,
  doc_type           text,
  created_at         timestamptz not null default now()
);

-- Metadata may be absent on an addendum (a draft saves incomplete work) but
-- must be well-formed when present, and never appears on a non-addendum.
-- Mirrors the shape rules of project_files_addendum_meta_ck (0076) minus the
-- not-null requirement, which the transfer endpoint enforces instead.
alter table bid_draft_files add constraint bid_draft_files_addendum_meta_ck check (
  case when category = 'addendum' then
         addendum_number is null
      or (btrim(addendum_number) <> '' and length(addendum_number) <= 40)
       else
         addendum_number is null and addendum_issued_on is null
  end
);

-- Same doc_type domain and pairing as project_files_doc_type_ck (0077),
-- narrowed to this table's categories: of the four intake categories only
-- 'addendum' may carry a doc_type ('revision' is not an intake category).
alter table bid_draft_files add constraint bid_draft_files_doc_type_ck check (
  doc_type is null
  or (doc_type in ('drawing', 'specification') and category = 'addendum')
);

create index bid_draft_files_draft_idx on bid_draft_files(draft_id);

-- RLS deny-by-default + FORCE (0055 / 0093 posture): all access is enforced in
-- the FastAPI layer and the service-role key bypasses it.
alter table bid_draft_files enable row level security;
alter table bid_draft_files force row level security;

notify pgrst, 'reload schema';
