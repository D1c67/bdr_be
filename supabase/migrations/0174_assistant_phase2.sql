-- 0174: Assistant phase 2, splitter handoff (docs/ASSISTANT_PHASE2_PLAN.md, section 2). Apply after 0170, 0171 and 0172. Idempotent; safe to run twice. Dev project (BDR) only until the release.
--
-- Release order: 0174 is applied to the target database before the backend
-- deploy, never after. The phase 2 selects and inserts name the columns
-- added here and would answer PGRST204 or 42703 without them.

-- A. Staged files: page count recorded at staging (split-drop never
--    downloads the bytes again), the object purge mark, and the task that
--    claimed the row (split-drop sets consumed_at and consumed_task_id in
--    one fenced update before any side effect; a failure clears both).
alter table public.assistant_files add column if not exists page_count int;
alter table public.assistant_files add column if not exists purged_at timestamptz;
alter table public.assistant_files add column if not exists consumed_task_id uuid;
do $$
begin
  if not exists (select 1 from pg_constraint where conname = 'assistant_files_page_count_chk') then
    alter table public.assistant_files
      add constraint assistant_files_page_count_chk check (page_count is null or page_count > 0) not valid;
    alter table public.assistant_files validate constraint assistant_files_page_count_chk;
  end if;
end $$;
-- The purge scans rows whose object still exists.
create index if not exists assistant_files_object_live_idx
  on public.assistant_files (expires_at) where purged_at is null;

-- B. Tasks: one task per approved split card (idempotency), the run that
--    started it, its spend hold, and the message the continuation posted.
alter table public.assistant_tasks add column if not exists approval_id uuid;
alter table public.assistant_tasks add column if not exists run_id uuid;
alter table public.assistant_tasks add column if not exists spend_reservation_id uuid;
alter table public.assistant_tasks add column if not exists result_message_id uuid;
create unique index if not exists assistant_tasks_approval_uq
  on public.assistant_tasks (approval_id) where approval_id is not null;
create index if not exists assistant_tasks_split_job_idx
  on public.assistant_tasks (split_job_id) where split_job_id is not null;

-- C. Split jobs started by an assistant carry their spend hold, so the run
--    can stamp it on every llm_call_log row (no FK, like assistant_task_id).
alter table public.bid_split_jobs add column if not exists spend_reservation_id uuid;
-- C2. The assistant scope is per FILE, not per job: split-drop stamps the
--    run and the hold on each file it stages; a human Retry or Reprocess on
--    the Bid Splitter page clears both, so that run is the person's (no
--    assistant attribution, no assistant provenance). No FK, like the job's.
alter table public.bid_split_files add column if not exists assistant_run_id uuid;
alter table public.bid_split_files add column if not exists spend_reservation_id uuid;

-- D. llm_call_log: the hold a call counts against (a split job's calls
--    reduce its hold instead of being counted twice).
alter table public.llm_call_log add column if not exists spend_reservation_id uuid;
create index if not exists llm_call_log_spend_reservation_idx
  on public.llm_call_log (spend_reservation_id) where spend_reservation_id is not null;

-- E. Spend reservations (docs/ASSISTANT.md Permissions item 12). A hold is
--    live while status = 'active' and expires_at > now(); an expired hold
--    needs no sweeper. user_id has no FK: the ledger outlives the person.
create table if not exists public.assistant_spend_reservations (
  id           uuid primary key default gen_random_uuid(),
  user_id      uuid not null,
  kind         text not null check (kind in ('turn_call', 'split_task')),
  run_id       uuid,
  task_id      uuid,
  approval_id  uuid,
  amount_usd   numeric(12,6) not null check (amount_usd >= 0),
  status       text not null default 'active' check (status in ('active', 'settled', 'released')),
  actual_usd   numeric(12,6),
  created_at   timestamptz not null default now(),
  expires_at   timestamptz not null,
  settled_at   timestamptz
);
create index if not exists assistant_spend_reservations_live_idx
  on public.assistant_spend_reservations (user_id, expires_at) where status = 'active';
create unique index if not exists assistant_spend_reservations_task_uq
  on public.assistant_spend_reservations (task_id) where task_id is not null and kind = 'split_task';
alter table public.assistant_spend_reservations enable row level security;
alter table public.assistant_spend_reservations force row level security;
revoke all on table public.assistant_spend_reservations from anon, authenticated;

-- F. Spend state and admission. One advisory lock serializes admission
--    across both uvicorn workers (PostgREST has no multi-statement
--    transaction, so this must be one function). Day = Pacific midnight.
--    Global = the assistant features plus every attributed call
--    (assistant_run_id set), which includes split calls the assistant started.
create or replace function public.assistant_spend_state(
  p_user_id uuid, p_user_cap_default numeric, p_global_cap_default numeric
)
returns jsonb
language plpgsql
stable
security invoker
set search_path = pg_catalog, public
as $$
declare
  v_since timestamptz := (date_trunc('day', now() at time zone 'America/Los_Angeles')) at time zone 'America/Los_Angeles';
  v_user_cap numeric;
  v_global_cap numeric;
  v_user_spent numeric;
  v_global_spent numeric;
  v_user_held numeric;
  v_global_held numeric;
begin
  select coalesce((select p.daily_cost_cap_usd from public.assistant_profiles p where p.user_id = p_user_id),
                  p_user_cap_default) into v_user_cap;
  select coalesce((select s.global_daily_cap_usd from public.assistant_settings s where s.id = 1),
                  p_global_cap_default) into v_global_cap;
  select coalesce(sum(l.cost_usd), 0) into v_user_spent
    from public.llm_call_log l where l.user_id = p_user_id and l.created_at >= v_since;
  select coalesce(sum(l.cost_usd), 0) into v_global_spent
    from public.llm_call_log l
   where l.created_at >= v_since
     and (l.feature in ('assistant', 'assistant_extract') or l.assistant_run_id is not null);
  with live as (
    select r.user_id,
           greatest(r.amount_usd - coalesce((select sum(l.cost_usd) from public.llm_call_log l
                                              where l.spend_reservation_id = r.id), 0), 0) as held
      from public.assistant_spend_reservations r
     where r.status = 'active' and r.expires_at > now()
  )
  select coalesce(sum(held) filter (where user_id = p_user_id), 0), coalesce(sum(held), 0)
    into v_user_held, v_global_held from live;
  return jsonb_build_object(
    'user_cap', v_user_cap, 'user_spent', v_user_spent, 'user_held', v_user_held,
    'global_cap', v_global_cap, 'global_spent', v_global_spent, 'global_held', v_global_held);
end;
$$;

create or replace function public.assistant_spend_reserve(
  p_user_id uuid, p_kind text, p_amount numeric, p_ttl_seconds int,
  p_user_cap_default numeric, p_global_cap_default numeric,
  p_run_id uuid, p_task_id uuid, p_approval_id uuid
)
returns jsonb
language plpgsql
volatile
security invoker
set search_path = pg_catalog, public
as $$
declare
  v_state jsonb;
  v_id uuid;
begin
  if p_kind not in ('turn_call', 'split_task') or p_amount is null or p_amount < 0
     or p_ttl_seconds is null or p_ttl_seconds < 30 or p_ttl_seconds > 43200 then
    raise exception 'assistant_spend_reserve: bad arguments' using errcode = 'check_violation';
  end if;
  perform pg_advisory_xact_lock(hashtext('assistant_spend'));
  v_state := public.assistant_spend_state(p_user_id, p_user_cap_default, p_global_cap_default);
  if (v_state->>'user_spent')::numeric + (v_state->>'user_held')::numeric + p_amount
       >= (v_state->>'user_cap')::numeric then
    return jsonb_build_object('ok', false, 'reason', 'user_cap', 'id', null, 'state', v_state);
  end if;
  if (v_state->>'global_spent')::numeric + (v_state->>'global_held')::numeric + p_amount
       >= (v_state->>'global_cap')::numeric then
    return jsonb_build_object('ok', false, 'reason', 'global_cap', 'id', null, 'state', v_state);
  end if;
  insert into public.assistant_spend_reservations (user_id, kind, run_id, task_id, approval_id, amount_usd, expires_at)
  values (p_user_id, p_kind, p_run_id, p_task_id, p_approval_id, p_amount,
          now() + make_interval(secs => p_ttl_seconds))
  returning id into v_id;
  return jsonb_build_object('ok', true, 'reason', null, 'id', v_id, 'state', v_state);
end;
$$;

revoke all on function public.assistant_spend_state(uuid, numeric, numeric) from public;
revoke all on function public.assistant_spend_state(uuid, numeric, numeric) from anon, authenticated;
grant execute on function public.assistant_spend_state(uuid, numeric, numeric) to service_role;
revoke all on function public.assistant_spend_reserve(uuid, text, numeric, int, numeric, numeric, uuid, uuid, uuid) from public;
revoke all on function public.assistant_spend_reserve(uuid, text, numeric, int, numeric, numeric, uuid, uuid, uuid) from anon, authenticated;
grant execute on function public.assistant_spend_reserve(uuid, text, numeric, int, numeric, numeric, uuid, uuid, uuid) to service_role;

-- G. The continuation job type must already be allowed (0170:974). Fail
--    loudly instead of widening the check a second way.
do $$
declare v_def text;
begin
  select pg_get_constraintdef(c.oid) into v_def from pg_constraint c
   where c.conrelid = 'public.llm_jobs'::regclass and c.conname = 'llm_jobs_job_type_check';
  if v_def is null or position('assistant_continue' in v_def) = 0 then
    raise exception '0174: llm_jobs_job_type_check does not allow assistant_continue; apply 0170 first';
  end if;
end $$;

notify pgrst, 'reload schema';
