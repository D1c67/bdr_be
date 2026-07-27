-- 0078 (0057 on hotfix/BDR_Staging) — Bidding restructure: task-to-task  ->  category (stage-to-stage) DAG.
--
-- The pipeline stops being one linear pointer (projects.current_stage) and becomes
-- FOUR categories, each an ordered subset of the SAME tasks, that progress in
-- parallel under a DAG:
--
--   intake            [intake, go_no_go, to_estimator]
--   material_numbers  [estimate_received, rfqs, receive_quotes]   unlock: intake complete
--   labor_numbers     [labor_numbers, markup]                     unlock: intake complete
--   send_out          [gc_pricing, verify, send_out, submitted, bid_outcome]
--                                                                 unlock: intake+material+labor complete
--
-- material_numbers and labor_numbers run in PARALLEL once intake completes. Within a
-- category tasks stay strictly sequential. `declined` (Go/No-Go "No") is a
-- project-global kill.
--
-- Source of truth becomes `project_category_state` (4 rows/project). projects.current_stage
-- is KEPT as a denormalized "headline" pointer (recomputed by the app on every
-- transition) so analytics / derive_status / the ?stage= filter / due-reminder terminal
-- filters keep working unchanged; the deep analytics rework is deferred.
--
-- No new project_stage VALUEs are needed — all four categories reuse existing task labels.

-- ── New enums ─────────────────────────────────────────────────────────────────
do $$ begin
  if not exists (select 1 from pg_type where typname = 'project_category') then
    create type project_category as enum ('intake', 'material_numbers', 'labor_numbers', 'send_out');
  end if;
end $$;

do $$ begin
  if not exists (select 1 from pg_type where typname = 'category_status') then
    create type category_status as enum ('locked', 'active', 'complete');
  end if;
end $$;

-- ── Per-category state (the new source of truth) ──────────────────────────────
create table if not exists project_category_state (
  project_id   uuid not null references projects(id) on delete cascade,
  category     project_category not null,
  current_task project_stage not null,          -- the category's head task
  status       category_status not null,        -- locked | active | complete
  owner_role   role,                             -- "whose task" hint for this category
  completed_at timestamptz,
  updated_at   timestamptz not null default now(),
  primary key (project_id, category)
);
create index if not exists project_category_state_lookup_idx
  on project_category_state(category, current_task, status);
drop trigger if exists project_category_state_updated_at on project_category_state;
create trigger project_category_state_updated_at before update on project_category_state
  for each row execute function set_updated_at();

-- ── Category tag on the append-only event log (additive, back-compatible) ─────
alter table stage_events add column if not exists category project_category;
create index if not exists stage_events_project_category_idx
  on stage_events(project_id, category, entered_at);

update stage_events set category = case
  when to_stage in ('intake', 'go_no_go', 'to_estimator', 'declined')     then 'intake'::project_category
  when to_stage in ('estimate_received', 'rfqs', 'receive_quotes')        then 'material_numbers'::project_category
  when to_stage in ('labor_numbers', 'markup')                           then 'labor_numbers'::project_category
  when to_stage in ('gc_pricing', 'verify', 'send_out', 'submitted', 'bid_outcome') then 'send_out'::project_category
  else null end
where category is null;

-- ── Backfill per-category state from the single current_stage ─────────────────
-- Deterministic map that respects the NEW unlock rules: a bid mid-material gets
-- material active where it is AND labor freshly unlocked (active@labor_numbers),
-- because intake is already complete and the two now run in parallel.

create or replace function _bdr_stage_order(s project_stage) returns int language sql immutable as $$
  select case s
    when 'intake' then 1 when 'go_no_go' then 2 when 'to_estimator' then 3
    when 'estimate_received' then 4 when 'rfqs' then 5 when 'receive_quotes' then 6
    when 'labor_numbers' then 7 when 'markup' then 8 when 'gc_pricing' then 9
    when 'verify' then 10 when 'send_out' then 11 when 'submitted' then 12
    when 'bid_outcome' then 13 when 'declined' then 99 else 1 end
$$;

create or replace function _bdr_stage_by_order(o int) returns project_stage language sql immutable as $$
  select case o
    when 1 then 'intake' when 2 then 'go_no_go' when 3 then 'to_estimator'
    when 4 then 'estimate_received' when 5 then 'rfqs' when 6 then 'receive_quotes'
    when 7 then 'labor_numbers' when 8 then 'markup' when 9 then 'gc_pricing'
    when 10 then 'verify' when 11 then 'send_out' when 12 then 'submitted'
    when 13 then 'bid_outcome' else 'intake' end::project_stage
$$;

create or replace function _bdr_task_owner(s project_stage) returns role language sql immutable as $$
  select case s
    when 'intake' then 'estimating_admin' when 'go_no_go' then 'estimating_engineer'
    when 'to_estimator' then 'estimating_admin' when 'estimate_received' then 'estimator'
    when 'rfqs' then 'estimating_engineer' when 'receive_quotes' then 'estimating_engineer'
    when 'labor_numbers' then 'estimating_engineer' when 'markup' then 'estimating_engineer'
    when 'gc_pricing' then 'estimating_engineer' when 'verify' then 'executive'
    when 'send_out' then 'estimating_admin' when 'submitted' then 'estimating_admin'
    when 'bid_outcome' then 'estimating_admin' else null end::role
$$;

do $$
declare
  p   record;
  o   int;
  dcl boolean;
  ct  project_stage;
  st  category_status;
begin
  for p in select id, current_stage from projects loop
    o   := _bdr_stage_order(p.current_stage);
    dcl := (p.current_stage = 'declined');

    -- INTAKE
    if dcl then
      ct := 'go_no_go'; st := 'active';
    elsif o > 3 then
      ct := 'to_estimator'; st := 'complete';
    else
      ct := _bdr_stage_by_order(o); st := 'active';   -- o in 1..3
    end if;
    insert into project_category_state(project_id, category, current_task, status, owner_role, completed_at)
      values (p.id, 'intake', ct, st, _bdr_task_owner(ct), case when st = 'complete' then now() else null end)
      on conflict (project_id, category) do nothing;

    -- MATERIAL NUMBERS
    if dcl or o <= 3 then
      ct := 'estimate_received'; st := 'locked';
    elsif o between 4 and 6 then
      ct := _bdr_stage_by_order(o); st := 'active';
    else                                              -- o > 6  ->  material done
      ct := 'receive_quotes'; st := 'complete';
    end if;
    insert into project_category_state(project_id, category, current_task, status, owner_role, completed_at)
      values (p.id, 'material_numbers', ct, st, _bdr_task_owner(ct), case when st = 'complete' then now() else null end)
      on conflict (project_id, category) do nothing;

    -- LABOR NUMBERS
    if dcl or o <= 3 then
      ct := 'labor_numbers'; st := 'locked';
    elsif o between 7 and 8 then
      ct := _bdr_stage_by_order(o); st := 'active';
    elsif o > 8 then
      ct := 'markup'; st := 'complete';
    else                                              -- o in 4..6: intake done, labor freshly unlocked
      ct := 'labor_numbers'; st := 'active';
    end if;
    insert into project_category_state(project_id, category, current_task, status, owner_role, completed_at)
      values (p.id, 'labor_numbers', ct, st, _bdr_task_owner(ct), case when st = 'complete' then now() else null end)
      on conflict (project_id, category) do nothing;

    -- SEND OUT
    if dcl or o < 9 then
      ct := 'gc_pricing'; st := 'locked';
    elsif o >= 13 then
      ct := 'bid_outcome'; st := 'complete';
    else                                              -- o in 9..12
      ct := _bdr_stage_by_order(o); st := 'active';
    end if;
    insert into project_category_state(project_id, category, current_task, status, owner_role, completed_at)
      values (p.id, 'send_out', ct, st, _bdr_task_owner(ct), case when st = 'complete' then now() else null end)
      on conflict (project_id, category) do nothing;
  end loop;
end $$;

drop function if exists _bdr_stage_order(project_stage);
drop function if exists _bdr_stage_by_order(int);
drop function if exists _bdr_task_owner(project_stage);

-- RLS deny-by-default, matching 0055 (the service-role backend bypasses it; there are
-- no per-role policies — all access is enforced in the FastAPI layer).
alter table project_category_state enable row level security;
alter table project_category_state force row level security;

-- Reload PostgREST's schema cache so the new table/column are visible immediately.
notify pgrst, 'reload schema';
