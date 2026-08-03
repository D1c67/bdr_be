-- 0090: per-GC "needs by" date on the project ↔ GC link.
--
-- Different GCs on the same bid can want our number on different days, so the
-- date lives on the membership row (project_gcs), not the project. Captured at
-- intake and editable from the project's GC panel; surfaced on the Bid
-- Invitations report next to each GC.

alter table project_gcs
    add column if not exists needs_by date;

comment on column project_gcs.needs_by is
    'Date this GC needs our bid by (per-GC deadline, distinct from the project bid date).';
