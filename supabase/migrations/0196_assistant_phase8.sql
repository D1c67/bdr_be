-- 0196: Assistant phase 8, model triage of the inbox feed (docs/ASSISTANT_PHASE8_PLAN.md,
-- section 4; owner decisions in docs/PHASE8_OWNER_DECISIONS.md). Dev project (BDR) only
-- until the release. Never re-lists llm_jobs_job_type_check and touches no quotes object.
-- Idempotent; safe to run twice.
--
-- Two parts split by the PART B marker line below (0190 style; one file per number).
-- Part A as a whole is over the 40 KB per-apply size (plan 4.4), so it goes through
-- apply_migration only in three pieces cut at the two split points, as
-- 0196_assistant_phase8_a1, _a2 and _a3 in that order (notify is last, in _a3); never
-- apply part A in one piece. Part A holds every
-- new object: the digest email switch, the company triage switch, its switch-on time
-- and the fuse latch, the company spend identity, the two triage tables, the enqueue
-- index, the retry helper and the seven RPCs of A6. Part A carries no destructive
-- statement; its only such word is the foreign key referential action. The one error
-- kind whose name holds such a word is spelled in two halves, see A3.
-- Part B (0196_assistant_phase8_b, the owner at the screen) swaps the spend kind check
-- and adds the company identity check, redefines assistant_spend_reserve (kind
-- triage, company id only, company cap only) and assistant_inbox_feedback (mute on the
-- new kind) and adds the triage purge and the one-statement company triage switch (B6).
--
-- Note for later migrations (F9): 0196 is now the latest definition of
-- assistant_spend_reserve and of assistant_spend_reservations_kind_chk. Any later
-- redefinition must copy from 0196, not from 0186, and keep 'triage' and the company
-- id clause (pinned by tests/test_assistant_migration_0196.py).
--
-- Conventions (0170, 0190): RLS enabled and forced with no policies and nothing granted
-- to anon or authenticated; every function is SECURITY INVOKER with search_path pinned
-- to pg_catalog, public and executable by service_role only; project_id has no FK
-- (0141). Every RPC but assistant_triage_company_spend takes p_now (D12) for the tick
-- script's time travel, but p_now drives only the debounce, the window, next_attempt_at
-- and the stale tests. Leases, the lease retirement and the finish fence use the real
-- clock (clock_timestamp()), and so does every comparison with the epoch
-- assistant_settings.triage_enabled_at (the item's enqueued_at, the run's claimed_at,
-- the payload's emitted_at). The enqueue window is the one deliberate exception: it
-- compares the inbox row's created_at with the epoch.
--
-- ==== PART A ====

-- A1. Per-person assistant digest email switch (owner question 17). An absent
--     assistant_profiles row means on.
alter table public.assistant_profiles
  add column if not exists digest_email_enabled boolean not null default true;

-- A2. Company triage switch (Executive and IT Admin, default off), its last switch-on
--     time (the epoch) and the fuse latch (owner question 10 (a)). The switch route
--     writes triage_enabled_at from the real clock and clears the latch in the same
--     update; only the claim sets the latch.
alter table public.assistant_settings add column if not exists triage_enabled boolean not null default false;
alter table public.assistant_settings add column if not exists triage_enabled_at timestamptz;
alter table public.assistant_settings add column if not exists triage_fuse_tripped_at timestamptz;
do $$
begin
  -- A switch that is on always has an epoch, so turning triage on never backfills.
  if not exists (select 1 from pg_constraint where conname = 'assistant_settings_triage_epoch_chk') then
    alter table public.assistant_settings add constraint assistant_settings_triage_epoch_chk
      check (not triage_enabled or triage_enabled_at is not null) not valid;
    alter table public.assistant_settings validate constraint assistant_settings_triage_epoch_chk;
  end if;
end $$;

-- A2a. Company spend identity (owner question 12 (b)). A fixed reserved uuid with
--      version nibble 0, so gen_random_uuid() (version 4, the source of every
--      profiles.id) never produces it. Triage holds and calls are attributed to it,
--      so they count against the company cap and in no person's budget. Python
--      mirrors it as spend.COMPANY_SPEND_ID.
create or replace function public.assistant_company_spend_id()
returns uuid
language sql
immutable
security invoker
set search_path = pg_catalog, public
as $$ select '00000000-0000-0000-0000-000000000001'::uuid $$;

do $$
begin
  if exists (select 1 from public.profiles where id = public.assistant_company_spend_id()) then
    raise exception 'assistant_company_spend_id collides with a profile';
  end if;
end $$;

revoke all on function public.assistant_company_spend_id() from public;
revoke all on function public.assistant_company_spend_id() from anon, authenticated;
grant execute on function public.assistant_company_spend_id() to service_role;

-- A3. One row per model call. id is also the assistant_run_id on llm_call_log;
--     user_id is whose rows were triaged, never the payer (the company id pays).
--     The error kind for a cut-off answer is spelled 'trunc' || 'ated' throughout
--     part A (the connector screens part A for destructive words).
create table if not exists public.assistant_triage_runs (
  id             uuid primary key default gen_random_uuid(),
  user_id        uuid not null references public.profiles(id) on delete cascade,
  status         text not null default 'claimed' check (status in ('claimed', 'decided', 'skipped', 'failed')),
  claimed_by     text not null check (char_length(claimed_by) between 1 and 200),
  claimed_at     timestamptz not null,                      -- real clock
  lease_until    timestamptz not null,                      -- real clock
  item_count     smallint not null check (item_count between 1 and 20),
  model          text check (model is null or model ~ '^[A-Za-z0-9._:/-]{1,120}$'),
  reservation_id uuid,                                      -- a hold of kind triage, held by the company id; no FK
  input_tokens   int check (input_tokens is null or input_tokens >= 0),
  cached_tokens  int check (cached_tokens is null or cached_tokens >= 0),
  output_tokens  int check (output_tokens is null or output_tokens >= 0),
  cost_usd       numeric(12,6) check (cost_usd is null or cost_usd >= 0),
  error_kind     text,
  finished_at    timestamptz,
  created_at     timestamptz not null,                      -- p_now at claim
  constraint assistant_triage_runs_error_kind_chk check (error_kind is null or error_kind in
    ('cap_global', 'cap_test', 'spend_check', 'ineligible', 'model_error', 'bad_answer',
     'trunc' || 'ated', 'unpriced', 'busy', 'timeout', 'lease_expired', 'switched_off', 'fuse', 'breaker'))
);
create index if not exists assistant_triage_runs_user_idx
  on public.assistant_triage_runs (user_id, created_at desc);
create index if not exists assistant_triage_runs_open_idx
  on public.assistant_triage_runs (lease_until) where status = 'claimed';

-- A4. One row per record row triaged. Lives and goes with its inbox row (90 days,
--     legal hold respected by the Phase 7 inbox purge). attempts counts answer-quality
--     failures only; infra_retries counts infrastructure failures and never stops an
--     item; cap_deferred_at is the first cap refusal (the item then waits for Pacific
--     midnight, owner question 13 (b)). reason and action are pattern-checked, not
--     enum-checked: the Python validator is the enum gate.
create table if not exists public.assistant_triage_items (
  id                     uuid primary key default gen_random_uuid(),
  user_id                uuid not null references public.profiles(id) on delete cascade,
  inbox_id               uuid not null references public.assistant_inbox(id) on delete cascade,
  event_type             text not null check (event_type ~ '^[a-z][a-z0-9_.]{2,63}$'),
  project_id             uuid,                              -- no FK (0141)
  source_notification_id uuid,                              -- no FK; the bell at enqueue time
  status                 text not null default 'pending'
                           check (status in ('pending', 'claimed', 'decided', 'skipped', 'failed')),
  skip_reason            text check (skip_reason is null or skip_reason in
                           ('cap', 'ineligible', 'offboarded', 'stale', 'dismissed', 'switched_off', 'fuse',
                            'bad_answer')),
  run_id                 uuid references public.assistant_triage_runs(id) on delete set null,
  attempts               smallint not null default 0 check (attempts between 0 and 3),
  infra_retries          smallint not null default 0 check (infra_retries between 0 and 1000),
  next_attempt_at        timestamptz,
  cap_deferred_at        timestamptz,
  decision               text check (decision is null or decision in ('none', 'suggest', 'escalate')),
  reason                 text check (reason is null or reason ~ '^[a-z][a-z0-9_]{2,40}$'),
  action                 text check (action is null or action ~ '^[a-z][a-z0-9_]{2,40}$'),
  severity_hint          text check (severity_hint is null or severity_hint in ('normal', 'high', 'urgent')),
  escalation_event_id    uuid,                              -- assistant_events.id; no FK (events purge at 90 days)
  decided_at             timestamptz,
  created_at             timestamptz not null,              -- p_now at enqueue (debounce, window)
  enqueued_at            timestamptz not null default clock_timestamp(),  -- real clock; the epoch sweep and claim fence
  updated_at             timestamptz not null,
  constraint assistant_triage_items_inbox_uq unique (user_id, inbox_id),
  constraint assistant_triage_items_decided_chk check ((status = 'decided') = (decision is not null)),
  constraint assistant_triage_items_escalate_chk check (decision is distinct from 'escalate' or severity_hint is not null),
  constraint assistant_triage_items_skip_chk check ((status = 'skipped') = (skip_reason is not null))
);
create index if not exists assistant_triage_items_pending_idx
  on public.assistant_triage_items (user_id, created_at) where status = 'pending';
create index if not exists assistant_triage_items_run_idx on public.assistant_triage_items (run_id);
create index if not exists assistant_triage_items_decided_idx
  on public.assistant_triage_items (decided_at desc) where status = 'decided';

-- A5. Enqueue sweep over undismissed record rows.
create index if not exists assistant_inbox_record_triage_idx
  on public.assistant_inbox (created_at, user_id) where delivery = 'record' and dismissed_at is null;

-- A7 (tables). Forced RLS, no policies, nothing for anon or authenticated.
alter table public.assistant_triage_runs enable row level security;
alter table public.assistant_triage_runs force row level security;
revoke all on table public.assistant_triage_runs from anon, authenticated;
alter table public.assistant_triage_items enable row level security;
alter table public.assistant_triage_items force row level security;
revoke all on table public.assistant_triage_items from anon, authenticated;

-- ---- PART A split point 2 (required; _a1 ends here, _a2 starts here) ----

-- A4 helper. The one owner of every return to pending (CCF-10). Called only by the
-- claim's lease retirement and by the finish, never by the service. Three classes:
--   answer quality (bad_answer, the cut-off kind): attempts + 1; at 3 the item ends
--     skipped bad_answer (bad_answer) or failed (cut off); else pending after 60 s,
--     then 5 min.
--   infrastructure (model_error, timeout, busy, spend_check, unpriced, lease_expired,
--     breaker): attempts unchanged, infra_retries + 1, pending after
--     least(60 x 2^infra_retries, 900) s.
--   cap (a cap_global or cap_test refusal): nothing counted, cap_deferred_at kept from
--     the first refusal, pending until the next Pacific midnight plus a stable per-item
--     offset under 2 hours (MC-8), so a deferred backlog drains from 00:00 to 02:00 PT.
-- Acts only on a claimed item; returns the new status, or null when the item is not
-- claimed.
create or replace function public.assistant_triage_item_retry(
  p_item_id uuid, p_kind text, p_now timestamptz default now()
)
returns text
language plpgsql
volatile
security invoker
set search_path = pg_catalog, public
as $$
declare
  v_item public.assistant_triage_items;
  v_attempts int;
  v_midnight timestamptz;
  v_cut text := 'trunc' || 'ated';
begin
  if p_item_id is null or p_kind is null or p_now is null then
    raise exception 'assistant_triage_item_retry: bad arguments' using errcode = 'check_violation';
  end if;
  select * into v_item from public.assistant_triage_items t
   where t.id = p_item_id and t.status = 'claimed' for update;
  if not found then
    return null;
  end if;
  if p_kind = 'bad_answer' or p_kind = v_cut then
    v_attempts := least(v_item.attempts + 1, 3);
    if v_attempts >= 3 then
      update public.assistant_triage_items t
         set attempts = v_attempts,
             status = case when p_kind = v_cut then 'failed' else 'skipped' end,
             skip_reason = case when p_kind = v_cut then null else 'bad_answer' end,
             next_attempt_at = null, updated_at = p_now
       where t.id = v_item.id;
      return case when p_kind = v_cut then 'failed' else 'skipped' end;
    end if;
    update public.assistant_triage_items t
       set attempts = v_attempts, status = 'pending',
           next_attempt_at = p_now + case when v_attempts = 1 then interval '60 seconds' else interval '5 minutes' end,
           updated_at = p_now
     where t.id = v_item.id;
    return 'pending';
  elsif p_kind in ('model_error', 'timeout', 'busy', 'spend_check', 'unpriced', 'lease_expired', 'breaker') then
    update public.assistant_triage_items t
       set infra_retries = least(v_item.infra_retries + 1, 1000), status = 'pending',
           next_attempt_at = p_now + make_interval(
             secs => least(60 * power(2, least(v_item.infra_retries, 10)), 900)::double precision),
           updated_at = p_now
     where t.id = v_item.id;
    return 'pending';
  elsif p_kind = 'cap' then
    v_midnight := (date_trunc('day', p_now at time zone 'America/Los_Angeles') + interval '1 day')
                  at time zone 'America/Los_Angeles';
    update public.assistant_triage_items t
       set cap_deferred_at = coalesce(t.cap_deferred_at, p_now), status = 'pending',
           next_attempt_at = v_midnight + make_interval(
             secs => mod(abs(hashtext(p_item_id::text)::bigint), 7200)::double precision),
           updated_at = p_now
     where t.id = v_item.id;
    return 'pending';
  end if;
  raise exception 'assistant_triage_item_retry: unknown kind' using errcode = 'check_violation';
end $$;

-- A6.1 Enqueue. Does nothing unless the company switch is on, has an epoch and the
--      fuse latch is clear. One pending item per undismissed bell-sourced record row
--      (event_type like 'bell.%', so project.failed_to_bid is never enqueued, P2) of any
--      owner (no per-person switch, P14), created inside the window, never before the
--      last switch-on (no backfill) and never after p_now. No cursor: the unique item
--      makes it idempotent and a collapsed row whose created_at moved is never doubled
--      (P20). p_user_ids is the optional rollout allowlist (null means everyone).
create or replace function public.assistant_triage_enqueue(
  p_now timestamptz default now(), p_window_hours int default 24, p_limit int default 500,
  p_user_ids uuid[] default null
)
returns int
language plpgsql
volatile
security invoker
set search_path = pg_catalog, public
as $$
declare
  v_set record;
  v_count int;
begin
  if p_now is null or p_window_hours is null or p_window_hours not between 1 and 72
     or p_limit is null or p_limit not between 1 and 1000 then
    raise exception 'assistant_triage_enqueue: bad arguments' using errcode = 'check_violation';
  end if;
  select s.triage_enabled, s.triage_enabled_at, s.triage_fuse_tripped_at into v_set
    from public.assistant_settings s where s.id = 1;
  if v_set.triage_enabled is not true or v_set.triage_enabled_at is null
     or v_set.triage_fuse_tripped_at is not null then
    return 0;
  end if;
  insert into public.assistant_triage_items
    (user_id, inbox_id, event_type, project_id, source_notification_id, created_at, updated_at)
  select i.user_id, i.id, i.event_type, i.project_id, i.source_notification_id, p_now, p_now
    from public.assistant_inbox i
   where i.delivery = 'record' and i.dismissed_at is null
     and i.event_type like 'bell.%'
     and i.created_at >= greatest(p_now - make_interval(hours => p_window_hours), v_set.triage_enabled_at)
     and i.created_at <= p_now
     and (p_user_ids is null or i.user_id = any(p_user_ids))
     and not exists (select 1 from public.assistant_triage_items t
                      where t.user_id = i.user_id and t.inbox_id = i.id)
   order by i.created_at, i.id
   limit p_limit
  on conflict (user_id, inbox_id) do nothing;
  get diagnostics v_count = row_count;
  return v_count;
end $$;

-- A6.2 Claim one person's batch, in this order:
--   1. switch and latch: {switched_off: true} or {fuse: true}, claiming nothing;
--   2. epoch sweep (F2): pending items enqueued (real clock) before the last switch-on
--      settle skipped switched_off, cap-deferred ones included (a flip is a fresh start);
--   3. fuse (P10): over decisions whose run was claimed (real clock) at or after the
--      switch-on, the ratio wire (more than p_fuse_ratio escalations among the last 50,
--      at least 20) and the hourly wire (more than p_fuse_per_hour escalations decided
--      in the hour before p_now); a trip sets the latch once and returns {fuse: true};
--   4. retires expired claims (real-clock lease) as failed lease_expired, their items
--      back to pending through the helper (an infrastructure kind, no attempt);
--   5. picks a person inside p_user_ids (when given) whose oldest eligible pending item
--      is p_debounce_seconds old or who has p_batch_max eligible items, skipping anyone
--      with a live claimed run; steps 5 to 7 take only items enqueued (real clock) at or
--      after the switch-on, so a pre-epoch item the 500-row sweep has not reached yet is
--      never claimed;
--   6. takes the per-person advisory lock and re-checks under it (MC-5): no live run
--      and an eligible item still there, else the next candidate (at most 5);
--   7. locks up to p_batch_max items (an item that failed an answer check before is
--      taken alone), inserts the run with the real count and marks the items claimed.
-- Returns {run_id, user_id, items: [...]}, {switched_off: true}, {fuse: true} or null.
create or replace function public.assistant_triage_claim(
  p_worker text, p_now timestamptz default now(), p_lease_seconds int default 420,
  p_debounce_seconds int default 300, p_batch_max int default 10, p_fuse_ratio numeric default 0.5,
  p_fuse_per_hour int default 30, p_user_ids uuid[] default null
)
returns jsonb
language plpgsql
volatile
security invoker
set search_path = pg_catalog, public
as $$
declare
  v_set record;
  v_sample int;
  v_sample_esc int;
  v_hour_esc int;
  v_rows int;
  v_run record;
  v_item record;
  v_cand record;
  v_first record;
  v_ids uuid[];
  v_run_id uuid;
begin
  if p_worker is null or char_length(p_worker) not between 1 and 200 or p_now is null
     or p_lease_seconds is null or p_lease_seconds not between 60 and 1800
     or p_debounce_seconds is null or p_debounce_seconds not between 0 and 3600
     or p_batch_max is null or p_batch_max not between 1 and 20
     or p_fuse_ratio is null or p_fuse_ratio < 0 or p_fuse_ratio > 1
     or p_fuse_per_hour is null or p_fuse_per_hour not between 0 and 5000 then
    raise exception 'assistant_triage_claim: bad arguments' using errcode = 'check_violation';
  end if;

  -- 1. Switch and latch.
  select s.triage_enabled, s.triage_enabled_at, s.triage_fuse_tripped_at into v_set
    from public.assistant_settings s where s.id = 1;
  if v_set.triage_enabled is not true or v_set.triage_enabled_at is null then
    return jsonb_build_object('switched_off', true);
  end if;
  if v_set.triage_fuse_tripped_at is not null then
    return jsonb_build_object('fuse', true, 'tripped_now', false);
  end if;

  -- 2. Epoch sweep (real clock against real clock).
  update public.assistant_triage_items t
     set status = 'skipped', skip_reason = 'switched_off', next_attempt_at = null, updated_at = p_now
   where t.id in (select x.id from public.assistant_triage_items x
                   where x.status = 'pending' and x.enqueued_at < v_set.triage_enabled_at
                   order by x.enqueued_at limit 500 for update skip locked);

  -- 3. Fuse, both wires counted only since the switch-on (by the run's real-clock claim).
  select count(*), count(*) filter (where d.decision = 'escalate') into v_sample, v_sample_esc
    from (select t.decision from public.assistant_triage_items t
            join public.assistant_triage_runs r on r.id = t.run_id
           where t.status = 'decided' and r.claimed_at >= v_set.triage_enabled_at
           order by t.decided_at desc, t.id desc limit 50) d;
  select count(*) into v_hour_esc
    from public.assistant_triage_items t
    join public.assistant_triage_runs r on r.id = t.run_id
   where t.status = 'decided' and t.decision = 'escalate'
     and r.claimed_at >= v_set.triage_enabled_at
     and t.decided_at > p_now - interval '1 hour';
  if (p_fuse_ratio > 0 and v_sample >= 20 and v_sample_esc::numeric / greatest(v_sample, 1) > p_fuse_ratio)
     or (p_fuse_per_hour > 0 and v_hour_esc > p_fuse_per_hour) then
    update public.assistant_settings s set triage_fuse_tripped_at = clock_timestamp()
     where s.id = 1 and s.triage_fuse_tripped_at is null;
    get diagnostics v_rows = row_count;
    return jsonb_build_object('fuse', true, 'tripped_now', v_rows = 1);
  end if;

  -- 4. Retire expired claims (real clock), items back without an attempt.
  for v_run in
    select r.id from public.assistant_triage_runs r
     where r.status = 'claimed' and r.lease_until < clock_timestamp()
     order by r.lease_until limit 50 for update skip locked
  loop
    update public.assistant_triage_runs r
       set status = 'failed', error_kind = 'lease_expired', finished_at = p_now
     where r.id = v_run.id;
    for v_item in
      select t.id from public.assistant_triage_items t
       where t.run_id = v_run.id and t.status = 'claimed' order by t.id for update
    loop
      perform public.assistant_triage_item_retry(v_item.id, 'lease_expired', p_now);
    end loop;
  end loop;

  -- 5. Candidates.
  for v_cand in
    select t.user_id
      from public.assistant_triage_items t
     where t.status = 'pending' and (t.next_attempt_at is null or t.next_attempt_at <= p_now)
       and t.enqueued_at >= v_set.triage_enabled_at
       and (p_user_ids is null or t.user_id = any(p_user_ids))
       and not exists (select 1 from public.assistant_triage_runs r
                        where r.user_id = t.user_id and r.status = 'claimed'
                          and r.lease_until > clock_timestamp())
     group by t.user_id
    having min(t.created_at) <= p_now - make_interval(secs => p_debounce_seconds)
        or count(*) >= p_batch_max
     order by min(t.created_at), t.user_id
     limit 5
  loop
    -- 6. Lock the person, then re-check under the lock.
    perform pg_advisory_xact_lock(hashtext('assistant_triage:' || v_cand.user_id::text));
    if exists (select 1 from public.assistant_triage_runs r
                where r.user_id = v_cand.user_id and r.status = 'claimed'
                  and r.lease_until > clock_timestamp()) then
      continue;
    end if;
    select t.id, t.attempts into v_first
      from public.assistant_triage_items t
     where t.user_id = v_cand.user_id and t.status = 'pending'
       and (t.next_attempt_at is null or t.next_attempt_at <= p_now)
       and t.enqueued_at >= v_set.triage_enabled_at
     order by t.created_at, t.id
     limit 1 for update skip locked;
    if not found then
      continue;
    end if;

    -- 7. The batch, then the run with its real count.
    if v_first.attempts > 0 then
      v_ids := array[v_first.id];
    else
      select array_agg(x.id order by x.created_at, x.id) into v_ids
        from (select t.id, t.created_at from public.assistant_triage_items t
               where t.user_id = v_cand.user_id and t.status = 'pending' and t.attempts = 0
                 and (t.next_attempt_at is null or t.next_attempt_at <= p_now)
                 and t.enqueued_at >= v_set.triage_enabled_at
               order by t.created_at, t.id
               limit p_batch_max for update skip locked) x;
    end if;
    if v_ids is null or cardinality(v_ids) = 0 then
      continue;
    end if;
    v_run_id := gen_random_uuid();
    insert into public.assistant_triage_runs
      (id, user_id, status, claimed_by, claimed_at, lease_until, item_count, created_at)
    values (v_run_id, v_cand.user_id, 'claimed', p_worker, clock_timestamp(),
            clock_timestamp() + make_interval(secs => p_lease_seconds), cardinality(v_ids), p_now);
    update public.assistant_triage_items t
       set status = 'claimed', run_id = v_run_id, updated_at = p_now
     where t.id = any(v_ids);
    return jsonb_build_object(
      'run_id', v_run_id,
      'user_id', v_cand.user_id,
      'items', (select coalesce(jsonb_agg(jsonb_build_object(
                  'id', t.id, 'inbox_id', t.inbox_id, 'event_type', t.event_type, 'project_id', t.project_id,
                  'source_notification_id', t.source_notification_id, 'created_at', t.created_at,
                  'inbox_created_at', i.created_at, 'cap_deferred_at', t.cap_deferred_at)
                  order by t.created_at, t.id), '[]'::jsonb)
                  from public.assistant_triage_items t
                  join public.assistant_inbox i on i.id = t.inbox_id
                 where t.run_id = v_run_id and t.status = 'claimed'));
  end loop;
  return null;
end $$;

revoke all on function public.assistant_triage_item_retry(uuid, text, timestamptz) from public;
revoke all on function public.assistant_triage_item_retry(uuid, text, timestamptz) from anon, authenticated;
grant execute on function public.assistant_triage_item_retry(uuid, text, timestamptz) to service_role;
revoke all on function public.assistant_triage_enqueue(timestamptz, int, int, uuid[]) from public;
revoke all on function public.assistant_triage_enqueue(timestamptz, int, int, uuid[]) from anon, authenticated;
grant execute on function public.assistant_triage_enqueue(timestamptz, int, int, uuid[]) to service_role;
revoke all on function public.assistant_triage_claim(text, timestamptz, int, int, int, numeric, int, uuid[]) from public;
revoke all on function public.assistant_triage_claim(text, timestamptz, int, int, int, numeric, int, uuid[]) from anon, authenticated;
grant execute on function public.assistant_triage_claim(text, timestamptz, int, int, int, numeric, int, uuid[]) to service_role;

-- ---- PART A split point 3 (required; _a2 ends here, _a3 starts here) ----

-- A6.3 Finish one run in one transaction, fenced on the run row (claimed by p_worker,
--      still claimed, real-clock lease live); a lost fence writes nothing and returns
--      {fenced: false}. Inside the same transaction:
--   - for every p_status, the p_items entries that carry a skip_reason (dismissed,
--     stale, cap: the per-item results of plan 5.3 step 3) settle skipped first (MC-6);
--   - the company switch is re-read and fenced on the epoch (P8R-3): switch off, or the
--     run claimed before the last switch-on, settles every still-claimed item skipped
--     switched_off; the fuse latch settles them skipped fuse; neither emits anything;
--   - decided: each well-formed entry {id, decision, reason, action, severity_hint}
--     of an item still claimed by this run is stored; an escalate entry inserts its
--     assistant_events row (kind assistant.triage_escalation, source emit, dedupe key
--     'p8:esc:' || inbox_id, subject, project and inbox id from the item row, never
--     from p_items) in this same transaction; still-claimed items not named go through
--     the helper as bad_answer;
--   - skipped or failed: cap_global and cap_test defer every still-claimed item to the
--     next Pacific midnight (kind cap); ineligible, switched_off and fuse become the
--     skip reason; any other error kind goes through the helper.
-- Writes the model, tokens, cost and finished_at. Returns {fenced, emitted, status,
-- error_kind}. Decision and escalation commit together, so a worker that lost its
-- lease never emits, and a crash before the finish leaves no event.
create or replace function public.assistant_triage_finish(
  p_run_id uuid, p_worker text, p_now timestamptz, p_status text, p_error_kind text,
  p_items jsonb, p_usage jsonb
)
returns jsonb
language plpgsql
volatile
security invoker
set search_path = pg_catalog, public
as $$
declare
  v_run public.assistant_triage_runs;
  v_set record;
  v_list jsonb := coalesce(p_items, '[]'::jsonb);
  v_entry jsonb;
  v_item public.assistant_triage_items;
  v_left record;
  v_status text := p_status;
  v_error text := p_error_kind;
  v_emitted int := 0;
  v_event uuid;
  v_skip text;
  v_decision text;
  v_reason text;
  v_action text;
  v_severity text;
  v_model text;
  v_uuid_re text := '^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$';
begin
  if p_run_id is null or p_worker is null or p_now is null or p_status is null
     or p_status not in ('decided', 'skipped', 'failed')
     or jsonb_typeof(v_list) <> 'array'
     or (p_status = 'decided' and p_error_kind is not null)
     or (p_status <> 'decided' and (p_error_kind is null or p_error_kind not in
         ('cap_global', 'cap_test', 'spend_check', 'ineligible', 'model_error', 'bad_answer',
          'trunc' || 'ated', 'unpriced', 'busy', 'timeout', 'lease_expired', 'switched_off', 'fuse',
          'breaker'))) then
    raise exception 'assistant_triage_finish: bad arguments' using errcode = 'check_violation';
  end if;

  select * into v_run from public.assistant_triage_runs r
   where r.id = p_run_id and r.claimed_by = p_worker and r.status = 'claimed'
     and r.lease_until > clock_timestamp()
   for update;
  if not found then
    return jsonb_build_object('fenced', false, 'emitted', 0);
  end if;
  select s.triage_enabled, s.triage_enabled_at, s.triage_fuse_tripped_at into v_set
    from public.assistant_settings s where s.id = 1 for share;

  -- Per-item skips first, for every status (MC-6).
  for v_entry in select e.value from jsonb_array_elements(v_list) e loop
    continue when jsonb_typeof(v_entry) <> 'object' or coalesce(v_entry->>'skip_reason', '') = '';
    v_skip := v_entry->>'skip_reason';
    if v_skip not in ('dismissed', 'stale', 'cap') then
      raise exception 'assistant_triage_finish: bad skip_reason' using errcode = 'check_violation';
    end if;
    continue when coalesce(v_entry->>'id', '') !~ v_uuid_re;
    update public.assistant_triage_items t
       set status = 'skipped', skip_reason = v_skip, next_attempt_at = null, updated_at = p_now
     where t.id = (v_entry->>'id')::uuid and t.run_id = p_run_id and t.status = 'claimed';
  end loop;

  -- The company switch, fenced on the epoch, then the latch.
  if v_set.triage_enabled is not true or v_set.triage_enabled_at is null
     or v_run.claimed_at < v_set.triage_enabled_at then
    v_status := 'skipped';
    v_error := 'switched_off';
  elsif v_set.triage_fuse_tripped_at is not null then
    v_status := 'skipped';
    v_error := 'fuse';
  end if;

  if v_status = 'decided' then
    for v_entry in select e.value from jsonb_array_elements(v_list) e loop
      continue when jsonb_typeof(v_entry) <> 'object' or coalesce(v_entry->>'skip_reason', '') <> '';
      continue when coalesce(v_entry->>'id', '') !~ v_uuid_re;
      v_decision := v_entry->>'decision';
      v_reason := v_entry->>'reason';
      v_action := v_entry->>'action';
      v_severity := v_entry->>'severity_hint';
      continue when v_decision is null or v_decision not in ('none', 'suggest', 'escalate');
      continue when v_reason is null or v_reason !~ '^[a-z][a-z0-9_]{2,40}$';
      if v_decision = 'suggest' then
        continue when v_action is null or v_action !~ '^[a-z][a-z0-9_]{2,40}$';
      else
        v_action := null;
      end if;
      if v_decision = 'escalate' then
        continue when v_severity is null or v_severity not in ('normal', 'high', 'urgent');
      else
        v_severity := null;
      end if;
      update public.assistant_triage_items t
         set status = 'decided', decision = v_decision, reason = v_reason, action = v_action,
             severity_hint = v_severity, decided_at = p_now, next_attempt_at = null, updated_at = p_now
       where t.id = (v_entry->>'id')::uuid and t.run_id = p_run_id and t.status = 'claimed'
      returning * into v_item;
      continue when not found;
      if v_decision = 'escalate' then
        v_event := null;
        insert into public.assistant_events
          (event_type, dedupe_key, project_id, subject_user_id, payload, occurred_at, status, source)
        values ('assistant.triage_escalation', 'p8:esc:' || v_item.inbox_id::text, v_item.project_id,
                v_item.user_id,
                jsonb_build_object('inbox_id', v_item.inbox_id, 'triage_item_id', v_item.id, 'run_id', p_run_id,
                                   'reason', v_item.reason, 'severity_hint', v_item.severity_hint,
                                   'source_kind', v_item.event_type, 'emitted_at', clock_timestamp()),
                p_now, 'pending', 'emit')
        on conflict (dedupe_key) do nothing
        returning id into v_event;
        if v_event is null then
          select e.id into v_event from public.assistant_events e
           where e.dedupe_key = 'p8:esc:' || v_item.inbox_id::text;
        else
          v_emitted := v_emitted + 1;
        end if;
        update public.assistant_triage_items t set escalation_event_id = v_event where t.id = v_item.id;
      end if;
    end loop;
    for v_left in
      select t.id from public.assistant_triage_items t
       where t.run_id = p_run_id and t.status = 'claimed' order by t.id for update
    loop
      perform public.assistant_triage_item_retry(v_left.id, 'bad_answer', p_now);
    end loop;
  elsif v_error in ('cap_global', 'cap_test') then
    for v_left in
      select t.id from public.assistant_triage_items t
       where t.run_id = p_run_id and t.status = 'claimed' order by t.id for update
    loop
      perform public.assistant_triage_item_retry(v_left.id, 'cap', p_now);
    end loop;
  elsif v_error in ('ineligible', 'switched_off', 'fuse') then
    update public.assistant_triage_items t
       set status = 'skipped', skip_reason = v_error, next_attempt_at = null, updated_at = p_now
     where t.run_id = p_run_id and t.status = 'claimed';
  else
    for v_left in
      select t.id from public.assistant_triage_items t
       where t.run_id = p_run_id and t.status = 'claimed' order by t.id for update
    loop
      perform public.assistant_triage_item_retry(v_left.id, v_error, p_now);
    end loop;
  end if;

  v_model := p_usage->>'model';
  if v_model is not null and v_model !~ '^[A-Za-z0-9._:/-]{1,120}$' then
    v_model := null;
  end if;
  update public.assistant_triage_runs r
     set status = v_status, error_kind = v_error,
         model = coalesce(v_model, r.model),
         reservation_id = coalesce(case when coalesce(p_usage->>'reservation_id', '') ~ v_uuid_re
                                        then (p_usage->>'reservation_id')::uuid end, r.reservation_id),
         input_tokens = case when jsonb_typeof(p_usage->'input_tokens') = 'number'
                             then least(greatest((p_usage->>'input_tokens')::numeric, 0), 2000000000)::int end,
         cached_tokens = case when jsonb_typeof(p_usage->'cached_tokens') = 'number'
                              then least(greatest((p_usage->>'cached_tokens')::numeric, 0), 2000000000)::int end,
         output_tokens = case when jsonb_typeof(p_usage->'output_tokens') = 'number'
                              then least(greatest((p_usage->>'output_tokens')::numeric, 0), 2000000000)::int end,
         cost_usd = case when jsonb_typeof(p_usage->'cost_usd') = 'number'
                         then least(greatest((p_usage->>'cost_usd')::numeric, 0), 999999)::numeric(12,6) end,
         finished_at = p_now
   where r.id = p_run_id;
  return jsonb_build_object('fenced', true, 'emitted', v_emitted, 'status', v_status, 'error_kind', v_error);
end $$;

-- A6.4 Settle one person's pending and claimed items (offboarding, the per-batch
--      eligibility failure), bounded 500 per call, and fail the person's claimed runs
--      first (ineligible keeps its error kind, every other reason is switched_off), so
--      a late finish from a worker mid-call is fenced out and emits nothing. It takes
--      the claim's per-person advisory lock first, so a claim still in flight cannot
--      commit a live run after this call.
create or replace function public.assistant_triage_skip_user(
  p_user_id uuid, p_reason text, p_now timestamptz default now()
)
returns int
language plpgsql
volatile
security invoker
set search_path = pg_catalog, public
as $$
declare
  v_count int;
begin
  if p_user_id is null or p_now is null or p_reason is null
     or p_reason not in ('offboarded', 'switched_off', 'ineligible') then
    raise exception 'assistant_triage_skip_user: bad arguments' using errcode = 'check_violation';
  end if;
  -- The claim's per-person lock (step 6): a claim mid-transaction for this person
  -- commits first and its run is failed below, or this call commits first and the
  -- claim's re-check under the lock finds no pending item.
  perform pg_advisory_xact_lock(hashtext('assistant_triage:' || p_user_id::text));
  update public.assistant_triage_runs r
     set status = 'failed',
         error_kind = case when p_reason = 'ineligible' then 'ineligible' else 'switched_off' end,
         finished_at = p_now
   where r.user_id = p_user_id and r.status = 'claimed';
  update public.assistant_triage_items t
     set status = 'skipped', skip_reason = p_reason, next_attempt_at = null, updated_at = p_now
   where t.id in (select x.id from public.assistant_triage_items x
                   where x.user_id = p_user_id and x.status in ('pending', 'claimed')
                   order by x.created_at, x.id limit 500 for update skip locked);
  get diagnostics v_count = row_count;
  return v_count;
end $$;

-- A6.7 Today's company triage spend (Pacific day of the real clock, like
--      assistant_spend_state). No p_now on purpose (MC-4): the live-test cap reads the
--      real day even while the tick script runs days ahead. spent = llm_call_log cost
--      under the company id; held = the unused part of live triage holds.
create or replace function public.assistant_triage_company_spend()
returns jsonb
language plpgsql
stable
security invoker
set search_path = pg_catalog, public
as $$
declare
  v_since timestamptz := (date_trunc('day', now() at time zone 'America/Los_Angeles')) at time zone 'America/Los_Angeles';
  v_spent numeric;
  v_held numeric;
begin
  select coalesce(sum(l.cost_usd), 0) into v_spent
    from public.llm_call_log l
   where l.user_id = public.assistant_company_spend_id() and l.created_at >= v_since;
  select coalesce(sum(greatest(r.amount_usd - coalesce((select sum(l.cost_usd) from public.llm_call_log l
                                                          where l.spend_reservation_id = r.id), 0), 0)), 0)
    into v_held
    from public.assistant_spend_reservations r
   where r.kind = 'triage' and r.user_id = public.assistant_company_spend_id()
     and r.status = 'active' and r.expires_at > now();
  return jsonb_build_object('spent', v_spent, 'held', v_held, 'since', v_since);
end $$;

-- A6.5 Counts for the admin health block: counts only, by status, decision, reason,
--      severity hint, skip reason and run error kind, the cost sum, cap-deferred items
--      still pending, the fuse inputs (only decisions whose run was claimed at or after
--      the switch-on, P8R-1, MC-2), the latch, and today's spend breakdown (the global
--      numbers of assistant_spend_state, the browser_task share of live holds, the
--      company triage spend). The per-role breakdown is withheld whole when any role
--      cell has fewer than 3 distinct people (the A11.10 rule). No user id leaves it.
create or replace function public.assistant_triage_counts(
  p_since timestamptz, p_now timestamptz default now()
)
returns jsonb
language plpgsql
stable
security invoker
set search_path = pg_catalog, public
as $$
declare
  v_set record;
  v_sample int := 0;
  v_sample_esc int := 0;
  v_hour_esc int := 0;
  v_state jsonb;
  v_browser numeric;
  v_company jsonb;
  v_roles jsonb;
  v_hidden boolean;
begin
  if p_since is null or p_now is null then
    raise exception 'assistant_triage_counts: bad arguments' using errcode = 'check_violation';
  end if;
  select s.triage_enabled, s.triage_enabled_at, s.triage_fuse_tripped_at into v_set
    from public.assistant_settings s where s.id = 1;
  if v_set.triage_enabled_at is not null then
    select count(*), count(*) filter (where d.decision = 'escalate') into v_sample, v_sample_esc
      from (select t.decision from public.assistant_triage_items t
              join public.assistant_triage_runs r on r.id = t.run_id
             where t.status = 'decided' and r.claimed_at >= v_set.triage_enabled_at
             order by t.decided_at desc, t.id desc limit 50) d;
    select count(*) into v_hour_esc
      from public.assistant_triage_items t
      join public.assistant_triage_runs r on r.id = t.run_id
     where t.status = 'decided' and t.decision = 'escalate'
       and r.claimed_at >= v_set.triage_enabled_at
       and t.decided_at > p_now - interval '1 hour';
  end if;
  v_state := public.assistant_spend_state(null::uuid, 0, 50);
  select coalesce(sum(greatest(r.amount_usd - coalesce((select sum(l.cost_usd) from public.llm_call_log l
                                                          where l.spend_reservation_id = r.id), 0), 0)), 0)
    into v_browser
    from public.assistant_spend_reservations r
   where r.kind = 'browser_task' and r.status = 'active' and r.expires_at > now();
  v_company := public.assistant_triage_company_spend();
  with base as (
    select t.user_id, t.decision, coalesce(p.role::text, 'unknown') as role_name
      from public.assistant_triage_items t
      left join public.profiles p on p.id = t.user_id
     where t.created_at >= p_since
  ), cells as (
    select b.role_name, count(distinct b.user_id) as people, count(*) as items,
           count(*) filter (where b.decision = 'escalate') as escalations
      from base b group by b.role_name
  )
  select coalesce(bool_or(c.people < 3), false),
         coalesce(jsonb_agg(jsonb_build_object('role', c.role_name, 'items', c.items,
                                               'escalations', c.escalations) order by c.role_name), '[]'::jsonb)
    into v_hidden, v_roles
    from cells c;
  if v_hidden then
    v_roles := '[]'::jsonb;
  end if;
  return jsonb_build_object(
    'since', p_since,
    'triage_enabled', coalesce(v_set.triage_enabled, false),
    'triage_enabled_at', v_set.triage_enabled_at,
    'fuse_tripped_at', v_set.triage_fuse_tripped_at,
    'status', (select coalesce(jsonb_object_agg(x.k, x.n), '{}'::jsonb)
                 from (select t.status as k, count(*) as n from public.assistant_triage_items t
                        where t.created_at >= p_since group by t.status) x),
    'decision', (select coalesce(jsonb_object_agg(x.k, x.n), '{}'::jsonb)
                   from (select t.decision as k, count(*) as n from public.assistant_triage_items t
                          where t.created_at >= p_since and t.decision is not null group by t.decision) x),
    'reason', (select coalesce(jsonb_object_agg(x.k, x.n), '{}'::jsonb)
                 from (select t.reason as k, count(*) as n from public.assistant_triage_items t
                        where t.created_at >= p_since and t.reason is not null group by t.reason) x),
    'severity_hint', (select coalesce(jsonb_object_agg(x.k, x.n), '{}'::jsonb)
                        from (select t.severity_hint as k, count(*) as n from public.assistant_triage_items t
                               where t.created_at >= p_since and t.severity_hint is not null
                               group by t.severity_hint) x),
    'skip_reason', (select coalesce(jsonb_object_agg(x.k, x.n), '{}'::jsonb)
                      from (select t.skip_reason as k, count(*) as n from public.assistant_triage_items t
                             where t.created_at >= p_since and t.skip_reason is not null
                             group by t.skip_reason) x),
    'error_kind', (select coalesce(jsonb_object_agg(x.k, x.n), '{}'::jsonb)
                     from (select r.error_kind as k, count(*) as n from public.assistant_triage_runs r
                            where r.created_at >= p_since and r.error_kind is not null
                            group by r.error_kind) x),
    'runs', (select count(*) from public.assistant_triage_runs r where r.created_at >= p_since),
    'cost_usd', (select coalesce(sum(r.cost_usd), 0) from public.assistant_triage_runs r
                  where r.created_at >= p_since),
    'cap_deferred_pending', (select count(*) from public.assistant_triage_items t
                              where t.status = 'pending' and t.cap_deferred_at is not null),
    'fuse', jsonb_build_object('sample', v_sample, 'sample_escalations', v_sample_esc,
                               'hour_escalations', v_hour_esc),
    'spend', jsonb_build_object('global_cap', v_state->'global_cap', 'global_spent', v_state->'global_spent',
                                'global_held', v_state->'global_held', 'browser_held', v_browser,
                                'triage_spent', v_company->'spent', 'triage_held', v_company->'held'),
    'roles', v_roles,
    'roles_suppressed', v_hidden);
end $$;

-- A6.8 Settle pending items while triage is meant to be stopped (F2): reason
--      switched_off acts only while assistant_settings.enabled, proactive_enabled or
--      triage_enabled is off; reason fuse only while the latch is set. Otherwise 0.
create or replace function public.assistant_triage_skip_all(
  p_reason text, p_now timestamptz default now(), p_limit int default 500
)
returns int
language plpgsql
volatile
security invoker
set search_path = pg_catalog, public
as $$
declare
  v_set record;
  v_count int;
begin
  if p_reason is null or p_reason not in ('switched_off', 'fuse') or p_now is null
     or p_limit is null or p_limit not between 1 and 1000 then
    raise exception 'assistant_triage_skip_all: bad arguments' using errcode = 'check_violation';
  end if;
  select s.enabled, s.proactive_enabled, s.triage_enabled, s.triage_fuse_tripped_at into v_set
    from public.assistant_settings s where s.id = 1;
  if p_reason = 'fuse' and v_set.triage_fuse_tripped_at is null then
    return 0;
  end if;
  if p_reason = 'switched_off' and v_set.enabled is true and v_set.proactive_enabled is true
     and v_set.triage_enabled is true then
    return 0;
  end if;
  update public.assistant_triage_items t
     set status = 'skipped', skip_reason = p_reason, next_attempt_at = null, updated_at = p_now
   where t.id in (select x.id from public.assistant_triage_items x
                   where x.status = 'pending'
                   order by x.created_at, x.id limit p_limit for update skip locked);
  get diagnostics v_count = row_count;
  return v_count;
end $$;

-- A7 (functions). Execute for service_role only (0145 pattern).
revoke all on function public.assistant_triage_finish(uuid, text, timestamptz, text, text, jsonb, jsonb) from public;
revoke all on function public.assistant_triage_finish(uuid, text, timestamptz, text, text, jsonb, jsonb) from anon, authenticated;
grant execute on function public.assistant_triage_finish(uuid, text, timestamptz, text, text, jsonb, jsonb) to service_role;
revoke all on function public.assistant_triage_skip_user(uuid, text, timestamptz) from public;
revoke all on function public.assistant_triage_skip_user(uuid, text, timestamptz) from anon, authenticated;
grant execute on function public.assistant_triage_skip_user(uuid, text, timestamptz) to service_role;
revoke all on function public.assistant_triage_company_spend() from public;
revoke all on function public.assistant_triage_company_spend() from anon, authenticated;
grant execute on function public.assistant_triage_company_spend() to service_role;
revoke all on function public.assistant_triage_counts(timestamptz, timestamptz) from public;
revoke all on function public.assistant_triage_counts(timestamptz, timestamptz) from anon, authenticated;
grant execute on function public.assistant_triage_counts(timestamptz, timestamptz) to service_role;
revoke all on function public.assistant_triage_skip_all(text, timestamptz, int) from public;
revoke all on function public.assistant_triage_skip_all(text, timestamptz, int) from anon, authenticated;
grant execute on function public.assistant_triage_skip_all(text, timestamptz, int) to service_role;

notify pgrst, 'reload schema';

-- ==== PART B ====
-- Destructive text lives only here (the owner at the screen): the spend kind check swap,
-- the two redefinitions and the triage purge. B6 (the switch) carries none.

-- B1. The spend kind check gains 'triage' (re-listing exactly the five 0186 kinds plus
--     triage; no later migration changed it), and the company identity check: a triage
--     hold is always the company's, and the company id never holds any other kind.
do $$
declare
  v_name text;
  v_def text;
begin
  select conname, pg_get_constraintdef(oid) into v_name, v_def from pg_constraint
   where conrelid = 'public.assistant_spend_reservations'::regclass and contype = 'c'
     and pg_get_constraintdef(oid) like '%split_task%';
  if v_name is null then
    raise exception 'assistant_spend_kind_constraint_missing';
  end if;
  if position('''triage''' in v_def) = 0 then
    execute format('alter table public.assistant_spend_reservations drop constraint %I', v_name);
    alter table public.assistant_spend_reservations add constraint assistant_spend_reservations_kind_chk
      check (kind in ('turn_call', 'split_task', 'plans_pages', 'ai_estimate', 'browser_task', 'triage'));
  end if;
  if not exists (select 1 from pg_constraint where conname = 'assistant_spend_reservations_company_chk') then
    if exists (select 1 from public.assistant_spend_reservations r
                where (r.kind = 'triage') <> (r.user_id = public.assistant_company_spend_id())) then
      raise exception 'assistant_spend_reservations_company_chk: existing rows break it';
    end if;
    alter table public.assistant_spend_reservations add constraint assistant_spend_reservations_company_chk
      check ((kind = 'triage') = (user_id = public.assistant_company_spend_id())) not valid;
    alter table public.assistant_spend_reservations validate constraint assistant_spend_reservations_company_chk;
  end if;
end $$;

-- B2. Reserve, copied from 0186 (the latest body; 0188, 0189, 0192 and 0194 only call it)
--     with two changes: kind 'triage', held only by the company spend identity (a person
--     with kind triage, a null id with kind triage, or the company id with any other
--     kind is a bad argument), and for kind triage the per-person cap check is skipped:
--     only the company cap applies, the same comparison as every kind (pure sharing,
--     owner question 11 (b)).
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
  if p_kind not in ('turn_call', 'split_task', 'plans_pages', 'ai_estimate', 'browser_task', 'triage') or p_amount is null or p_amount < 0
     or p_ttl_seconds is null or p_ttl_seconds < 30 or p_ttl_seconds > 43200 then
    raise exception 'assistant_spend_reserve: bad arguments' using errcode = 'check_violation';
  end if;
  if (p_kind = 'triage' and p_user_id is distinct from public.assistant_company_spend_id())
     or (p_kind <> 'triage' and p_user_id is not distinct from public.assistant_company_spend_id()) then
    raise exception 'assistant_spend_reserve: bad arguments' using errcode = 'check_violation';
  end if;
  perform pg_advisory_xact_lock(hashtext('assistant_spend'));
  v_state := public.assistant_spend_state(p_user_id, p_user_cap_default, p_global_cap_default);
  if p_kind <> 'triage'
     and (v_state->>'user_spent')::numeric + (v_state->>'user_held')::numeric + p_amount
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

-- B3. Feedback, copied from 0190 part B with one change: mute is accepted for
--     assistant.triage_escalation as for assistant.approval_expiring (the person's own
--     mute, kept by the owner). The not_useful auto-downgrade and mute_project already
--     apply to any router row.
create or replace function public.assistant_inbox_feedback(
  p_user_id uuid, p_inbox_id uuid, p_value text, p_now timestamptz default now()
)
returns jsonb
language plpgsql
volatile
security invoker
set search_path = pg_catalog, public
as $$
declare
  v_row public.assistant_inbox;
  v_count int;
  v_rows int;
  v_auto boolean := false;
begin
  if p_value is null or p_value not in ('useful', 'not_useful', 'mute', 'mute_project') then
    return jsonb_build_object('ok', false, 'code', 'invalid_feedback');
  end if;
  select * into v_row from public.assistant_inbox i
   where i.id = p_inbox_id and i.user_id = p_user_id for update;
  if not found then
    return jsonb_build_object('ok', false, 'code', 'not_found');
  end if;
  if p_value = 'mute' and (v_row.delivery = 'direct' or v_row.event_type is null
       or (v_row.event_type like 'assistant.%' and v_row.event_type not in ('assistant.approval_expiring', 'assistant.triage_escalation'))) then
    return jsonb_build_object('ok', false, 'code', 'not_mutable');
  end if;
  if p_value = 'mute_project' and (v_row.delivery = 'direct' or v_row.project_id is null) then
    return jsonb_build_object('ok', false, 'code', 'not_mutable');
  end if;
  update public.assistant_inbox i set feedback = p_value, feedback_at = p_now where i.id = v_row.id;
  if p_value = 'mute' then
    insert into public.assistant_subscriptions
      (user_id, event_type, enabled, severity, source, updated_by, created_at, updated_at)
    values (p_user_id, v_row.event_type, false, 'normal', 'user', p_user_id, p_now, p_now)
    on conflict (user_id, event_type) where role is null
    do update set enabled = false, source = 'user', updated_by = p_user_id, updated_at = p_now;
    update public.assistant_inbox i set dismissed_at = p_now, dismissed_reason = 'muted'
     where i.user_id = p_user_id and i.event_type = v_row.event_type
       and i.delivery in ('immediate', 'digest') and i.delivered_at is null and i.dismissed_at is null;
  elsif p_value = 'mute_project' then
    insert into public.assistant_project_mutes (user_id, project_id, created_at)
    values (p_user_id, v_row.project_id, p_now)
    on conflict (user_id, project_id) do nothing;
    update public.assistant_inbox i set dismissed_at = p_now, dismissed_reason = 'muted'
     where i.user_id = p_user_id and i.project_id = v_row.project_id
       and i.delivery in ('immediate', 'digest') and i.delivered_at is null and i.dismissed_at is null;
  elsif p_value = 'not_useful' and v_row.delivery in ('immediate', 'digest') and v_row.event_type is not null then
    select count(*) into v_count from public.assistant_inbox i
     where i.user_id = p_user_id and i.event_type = v_row.event_type
       and i.delivery in ('immediate', 'digest') and i.feedback = 'not_useful'
       and i.feedback_at > p_now - interval '14 days';
    if v_count >= 3 then
      insert into public.assistant_subscriptions
        (user_id, event_type, enabled, severity, source, updated_by, created_at, updated_at)
      values (p_user_id, v_row.event_type, true, 'low', 'feedback_auto', p_user_id, p_now, p_now)
      on conflict (user_id, event_type) where role is null do nothing;
      get diagnostics v_rows = row_count;
      v_auto := v_rows = 1;
    end if;
  end if;
  return jsonb_build_object('ok', true, 'feedback', p_value, 'event_type', v_row.event_type,
                            'auto_downgraded', v_auto);
end $$;

-- B4. Triage run retention (owner question 21 (a)): runs older than p_run_days whose
--     items are gone or older, never a claimed run, skipping every legal-hold person,
--     bounded by p_limit, one worker at a time. Items need no purge of their own: they
--     cascade with their inbox row (the Phase 7 purge, legal hold respected).
create or replace function public.assistant_triage_purge(p_now timestamptz, p_run_days int, p_limit int)
returns jsonb
language plpgsql
volatile
security invoker
set search_path = pg_catalog, public
as $$
declare
  v_runs int;
begin
  if p_now is null or p_run_days is null or p_run_days not between 7 and 3650
     or p_limit is null or p_limit not between 1 and 5000 then
    raise exception 'assistant_triage_purge: bad arguments' using errcode = 'check_violation';
  end if;
  if not pg_try_advisory_xact_lock(hashtext('assistant_triage_purge')) then
    return jsonb_build_object('locked', false, 'assistant_triage_runs', 0);
  end if;
  -- Serialize with a concurrent legal-hold change (0190 B4).
  lock table public.assistant_profiles in share mode;
  with doomed as (
    select r.id from public.assistant_triage_runs r
     where r.status <> 'claimed'
       and r.created_at < p_now - make_interval(days => p_run_days)
       and not exists (select 1 from public.assistant_profiles ap where ap.user_id = r.user_id and ap.legal_hold)
       and not exists (select 1 from public.assistant_triage_items t
                        where t.run_id = r.id and t.created_at >= p_now - make_interval(days => p_run_days))
     order by r.created_at limit p_limit for update skip locked
  )
  delete from public.assistant_triage_runs r using doomed where r.id = doomed.id;
  get diagnostics v_runs = row_count;
  return jsonb_build_object('locked', true, 'assistant_triage_runs', v_runs);
end $$;

-- B5. Execute for service_role only (0145 pattern).
revoke all on function public.assistant_spend_reserve(uuid, text, numeric, int, numeric, numeric, uuid, uuid, uuid) from public;
revoke all on function public.assistant_spend_reserve(uuid, text, numeric, int, numeric, numeric, uuid, uuid, uuid) from anon, authenticated;
grant execute on function public.assistant_spend_reserve(uuid, text, numeric, int, numeric, numeric, uuid, uuid, uuid) to service_role;
revoke all on function public.assistant_inbox_feedback(uuid, uuid, text, timestamptz) from public;
revoke all on function public.assistant_inbox_feedback(uuid, uuid, text, timestamptz) from anon, authenticated;
grant execute on function public.assistant_inbox_feedback(uuid, uuid, text, timestamptz) to service_role;
revoke all on function public.assistant_triage_purge(timestamptz, int, int) from public;
revoke all on function public.assistant_triage_purge(timestamptz, int, int) from anon, authenticated;
grant execute on function public.assistant_triage_purge(timestamptz, int, int) to service_role;

-- B6. The company triage switch in one statement (D-1). Flips triage_enabled on the
--     company row only when it changes; a flip to on sets the epoch triage_enabled_at
--     from the real clock and clears the fuse latch in the same update, so the epoch is
--     the database clock with no app-clock stamp and no second write. On while on, or
--     off while off, writes nothing. A flip to off keeps the epoch and the latch (only
--     an off and on flip clears it, owner question 10 (a)). p_user_id is the acting
--     admin (updated_by), never the company spend identity. Returns the switch fields
--     of the row and whether it changed. Execute for service_role only (0145 pattern).
create or replace function public.assistant_triage_set_enabled(p_user_id uuid, p_enabled boolean)
returns jsonb
language plpgsql
volatile
security invoker
set search_path = pg_catalog, public
as $$
declare
  v_set public.assistant_settings;
  v_changed boolean;
begin
  if p_user_id is null or p_enabled is null or p_user_id = public.assistant_company_spend_id() then
    raise exception 'assistant_triage_set_enabled: bad arguments' using errcode = 'check_violation';
  end if;
  update public.assistant_settings s
     set triage_enabled = p_enabled,
         triage_enabled_at = case when p_enabled then clock_timestamp() else s.triage_enabled_at end,
         triage_fuse_tripped_at = case when p_enabled then null else s.triage_fuse_tripped_at end,
         updated_by = p_user_id
   where s.id = 1 and s.triage_enabled = not p_enabled
  returning * into v_set;
  v_changed := found;
  if not v_changed then
    select * into v_set from public.assistant_settings s where s.id = 1;
    if not found then
      raise exception 'assistant_triage_set_enabled: the settings row is missing';
    end if;
  end if;
  return jsonb_build_object('triage_enabled', v_set.triage_enabled, 'triage_enabled_at', v_set.triage_enabled_at,
                            'triage_fuse_tripped_at', v_set.triage_fuse_tripped_at,
                            'updated_at', v_set.updated_at, 'changed', v_changed);
end $$;

revoke all on function public.assistant_triage_set_enabled(uuid, boolean) from public;
revoke all on function public.assistant_triage_set_enabled(uuid, boolean) from anon, authenticated;
grant execute on function public.assistant_triage_set_enabled(uuid, boolean) to service_role;

notify pgrst, 'reload schema';
