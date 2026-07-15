-- 0057 — Project Management (PM) module core. PM shares the projects spine but is
-- a SEPARATE lifecycle from the bidding pipeline: pm_stage (precon → active
-- construction → closeout) lives alongside current_stage so a project can be
-- simultaneously won (bid fact, current_stage stays 'bid_outcome' forever) and in
-- preconstruction (PM fact). NULL pm_stage = not in PM. PM code never touches
-- stage_events (bidding analytics' source of truth) — PM transitions log to
-- pm_stage_events instead.

-- Placeholder bidding stage for projects created directly in PM (awarded without a
-- bid / onboarded live jobs): they were never in the pipeline, so they get an
-- honest terminal stage instead of faking 'intake'. Bidding surfaces exclude it.
-- PG12+ allows ADD VALUE in a transaction so long as the label is not used later
-- in the same transaction — this migration never references 'pm_only' (the same
-- trick 0033 used for 'bid_outcome').
alter type project_stage add value if not exists 'pm_only';

create type pm_stage as enum ('precon', 'active_construction', 'closeout');
create type pm_origin as enum ('bid', 'direct');

-- The two live PM lifecycle markers stay on projects so lists filter without
-- joins (same reason current_stage lives there). Completion mirrors the abandon
-- pattern (0039): it preserves pm_stage ('closeout') and only flips the marker.
alter table projects add column if not exists pm_stage pm_stage;
alter table projects add column if not exists pm_origin pm_origin;
alter table projects add column if not exists pm_completed_at timestamptz;
alter table projects add column if not exists pm_completed_by uuid references profiles(id) on delete set null;
create index if not exists projects_pm_stage_idx on projects(pm_stage) where pm_stage is not null;

-- 1:1 PM detail record (the bid_outcomes pattern): row existence = the project has
-- been activated in PM. current_contract_value is NEVER stored — it is derived as
-- original_contract_value + sum of approved change orders (0059), matching the
-- derive-don't-store rule status follows.
create table pm_details (
  id                      uuid primary key default gen_random_uuid(),
  project_id              uuid not null unique references projects(id) on delete cascade,
  -- The customer is usually the winning GC; free-text fallback for owners/direct
  -- customers that aren't a GC row. SET NULL keeps the record if the GC goes.
  customer_gc_id          uuid references general_contractors(id) on delete set null,
  customer_name           text,
  original_contract_value numeric(14,2),
  awarded_at              date,
  ntp_date                date,
  -- Planned dates are seeded as COPIES of projects.est_start/finish_date at
  -- activation: PM schedule edits must not mutate the bidding estimate fields.
  planned_start_date      date,
  planned_finish_date     date,
  actual_start_date       date,
  actual_finish_date      date,
  superintendent_name     text,
  contract_number         text,
  retainage_percent       numeric(5,2) check (retainage_percent >= 0 and retainage_percent <= 100),
  notes                   text,
  activated_by            uuid references profiles(id) on delete set null,
  activated_at            timestamptz not null default now(),
  created_at              timestamptz not null default now(),
  updated_at              timestamptz not null default now()
);
create index if not exists pm_details_customer_gc_idx on pm_details(customer_gc_id);
create trigger pm_details_updated_at before update on pm_details
  for each row execute function set_updated_at();

-- Append-only PM stage log — the PM timeline's source of truth, mirroring
-- stage_events (0003) but typed on the PM enum so the two lifecycles can never
-- bleed into each other's analytics.
create table pm_stage_events (
  id          uuid primary key default gen_random_uuid(),
  project_id  uuid not null references projects(id) on delete cascade,
  from_stage  pm_stage,
  to_stage    pm_stage not null,
  actor_id    uuid references profiles(id) on delete set null,
  note        text,
  entered_at  timestamptz not null default now()
);
create index if not exists pm_stage_events_project_idx on pm_stage_events(project_id, entered_at);

-- RLS deny-by-default + forced (0007/0055 pattern); the service-role backend
-- bypasses it and FastAPI deps are the real authz boundary.
alter table pm_details      enable row level security;
alter table pm_details      force  row level security;
alter table pm_stage_events enable row level security;
alter table pm_stage_events force  row level security;

notify pgrst, 'reload schema';
