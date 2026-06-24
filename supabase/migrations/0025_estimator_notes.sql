-- 0025 — Estimator notes: a per-project message thread between the internal
-- team and the external estimator. Deliberately separate from projects.notes
-- (the intake summary field): these are conversational, append-only, and
-- visible to the estimator, so nothing sensitive is ever auto-placed here.

create table estimator_notes (
  id          uuid primary key default gen_random_uuid(),
  project_id  uuid not null references projects(id) on delete cascade,
  -- Actor convention (see 0012): keep the note if the author is deleted.
  author_id   uuid references profiles(id) on delete set null,
  body        text not null,
  created_at  timestamptz not null default now()
);
create index estimator_notes_project_idx on estimator_notes(project_id, created_at);
create index estimator_notes_author_idx on estimator_notes(author_id);

-- RLS deny-by-default (the service-role backend bypasses it); see 0007.
-- Forced like the other estimator-reachable tables.
alter table estimator_notes enable row level security;
alter table estimator_notes force row level security;
