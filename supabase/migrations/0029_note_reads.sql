-- 0029 — Per-user read state for estimator notes: powers the unread-count
-- badge on the project side menu. One row per (project, user); last_read_at
-- is a high-water mark — notes created after it count as unread for that
-- user. Rows only ever move forward (the API keeps the max).

create table estimator_note_reads (
  project_id   uuid not null references projects(id) on delete cascade,
  -- Read state is meaningless without its user, so cascade rather than the
  -- 0012 actor convention (this row is bookkeeping, not authored content).
  user_id      uuid not null references profiles(id) on delete cascade,
  last_read_at timestamptz not null,
  primary key (project_id, user_id)
);

-- RLS deny-by-default (the service-role backend bypasses it); see 0007.
-- Forced like the other estimator-reachable tables.
alter table estimator_note_reads enable row level security;
alter table estimator_note_reads force row level security;
