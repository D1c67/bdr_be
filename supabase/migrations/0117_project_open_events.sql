-- 0117 - Project open tracking, feeding the Estimating Engineer (Labor)
-- analytics page.
--
-- One row per time an internal user opens a project page (kind 'project') or
-- the Project Details box on it (kind 'details'). Rows are written by
-- POST /projects/{id}/opens, a fire-and-forget beacon from the frontend that
-- throttles itself client-side, so counts read as "distinct visits", not raw
-- component renders. The labor-engineer dashboard reads the first 'project'
-- open and the 'details' open count per project, filtered to users whose role
-- is estimating_engineer_labor at read time.
--
-- History note: nothing recorded page opens before this table existed, so the
-- analytics columns built on it stay empty for opens that happened earlier.
--
-- (0107-0115 exist on another branch; this file is numbered past them so the
-- unique-prefix rule holds when the branches merge, matching 0116.)

create table project_open_events (
  id          uuid primary key default gen_random_uuid(),
  project_id  uuid not null references projects(id) on delete cascade,
  user_id     uuid not null references profiles(id) on delete cascade,
  kind        text not null check (kind in ('project', 'details')),
  opened_at   timestamptz not null default now()
);

-- The analytics aggregates per project (first open, per-kind counts).
create index project_open_events_project_idx
  on project_open_events (project_id, kind, opened_at);

-- RLS deny-by-default: no policies; all access flows through the service-role
-- backend (which bypasses RLS), matching 0007/0032/0116.
alter table project_open_events enable row level security;
alter table project_open_events force row level security;
