-- 0176: Assistant phase 3, routines, memory, offboarding (docs/ASSISTANT_PHASE3_PLAN.md, section 2). Apply after 0170, 0171, 0172 and 0174. Idempotent; safe to run twice. Dev project (BDR) only until the release.
--
-- 0175 is 0175_teams_alerts.sql (another build); nothing here touches it.
-- Release order: 0176 is applied to the target database before the backend
-- deploy, never after. The phase 3 selects and RPC calls name the columns and
-- functions added here.

-- A. Schedules ------------------------------------------------------------------------

-- A1. Anchor for INTERVAL rules (the rule string never carries DTSTART), the
--     second slot of the occurrence queue (A3), the approval that created the
--     row (one schedule per approved card), pause and end bookkeeping.
alter table public.assistant_schedules add column if not exists dtstart timestamptz;
alter table public.assistant_schedules add column if not exists after_next_run_at timestamptz;
alter table public.assistant_schedules add column if not exists approval_id uuid;
alter table public.assistant_schedules add column if not exists paused_at timestamptz;
alter table public.assistant_schedules add column if not exists ended_reason text;
alter table public.assistant_schedules add column if not exists last_delivered_at timestamptz;
alter table public.assistant_schedules add column if not exists last_seen_at timestamptz;
create unique index if not exists assistant_schedules_approval_uq
  on public.assistant_schedules (approval_id) where approval_id is not null;
create index if not exists assistant_schedules_refill_idx
  on public.assistant_schedules (next_run_at) where status = 'active' and after_next_run_at is null;
create index if not exists assistant_schedules_paused_idx
  on public.assistant_schedules (paused_at) where status in ('paused', 'paused_feature_off');
create index if not exists assistant_schedules_conversation_idx
  on public.assistant_schedules (conversation_id) where conversation_id is not null;

do $$
begin
  if not exists (select 1 from pg_constraint where conname = 'assistant_schedules_timezone_chk') then
    alter table public.assistant_schedules
      add constraint assistant_schedules_timezone_chk check (timezone = 'America/Los_Angeles') not valid;
    alter table public.assistant_schedules validate constraint assistant_schedules_timezone_chk;
  end if;
  if not exists (select 1 from pg_constraint where conname = 'assistant_schedules_params_chk') then
    alter table public.assistant_schedules
      add constraint assistant_schedules_params_chk check (jsonb_typeof(params) = 'object') not valid;
    alter table public.assistant_schedules validate constraint assistant_schedules_params_chk;
  end if;
  if not exists (select 1 from pg_constraint where conname = 'assistant_schedules_active_next_chk') then
    alter table public.assistant_schedules
      add constraint assistant_schedules_active_next_chk
      check (status <> 'active' or (next_run_at is not null and dtstart is not null)) not valid;
    alter table public.assistant_schedules validate constraint assistant_schedules_active_next_chk;
  end if;
  if not exists (select 1 from pg_constraint where conname = 'assistant_schedules_ended_chk') then
    alter table public.assistant_schedules
      add constraint assistant_schedules_ended_chk check ((status = 'ended') = (ended_at is not null)) not valid;
    alter table public.assistant_schedules validate constraint assistant_schedules_ended_chk;
  end if;
end $$;

-- A2. Caps trigger, recreated with the same name and binding (0170 section 8h)
--     plus refusals for every rule part the cadence builder never writes
--     (COUNT, UNTIL, DTSTART, EXDATE, EXRULE, BYSETPOS, BYWEEKNO, BYYEARDAY,
--     BYEASTER). The app validates the cadence first (docs 3.1); this is the
--     backstop. The 1 hour minimum and the 10 active cap are unchanged.
create or replace function public.assistant_schedules_caps()
returns trigger
language plpgsql
security invoker
set search_path = pg_catalog, public
as $$
declare
  v_rule text := upper(new.rrule);
  v_freq text;
  v_byminute text;
  v_count int;
  v_rule_changed boolean := true;
  v_newly_active boolean := new.status = 'active';
begin
  if tg_op = 'UPDATE' then
    v_rule_changed := new.rrule is distinct from old.rrule;
    v_newly_active := new.status = 'active' and (old.status <> 'active' or old.user_id <> new.user_id);
  end if;

  if v_rule_changed then
    v_freq := substring(v_rule from 'FREQ=([A-Z]+)');
    if v_freq is null or v_freq not in ('HOURLY', 'DAILY', 'WEEKLY', 'MONTHLY', 'YEARLY') then
      raise exception 'schedule_interval: FREQ must be HOURLY or slower (minimum interval 1 hour)'
        using errcode = 'check_violation';
    end if;
    -- Unchanged from 0170: any BYSECOND is refused (to_rrule never writes
    -- one; dtstart is floored to the minute, so seconds default to 0).
    if v_rule ~ 'BYSECOND=' or v_rule ~ '(^|[^A-Z])RDATE' then
      raise exception 'schedule_interval: BYSECOND and RDATE are not allowed (minimum interval 1 hour)'
        using errcode = 'check_violation';
    end if;
    v_byminute := substring(v_rule from 'BYMINUTE=([0-9,]+)');
    if v_byminute is not null and position(',' in v_byminute) > 0 then
      raise exception 'schedule_interval: one BYMINUTE value at most (minimum interval 1 hour)'
        using errcode = 'check_violation';
    end if;
    if v_rule ~ '(COUNT|UNTIL|DTSTART|EXDATE|EXRULE|BYSETPOS|BYWEEKNO|BYYEARDAY|BYEASTER)' then
      raise exception 'schedule_rule: this rule part is not supported' using errcode = 'check_violation';
    end if;
  end if;

  if v_newly_active then
    perform pg_advisory_xact_lock(hashtext('assistant_schedules:' || new.user_id::text));
    select count(*) into v_count from public.assistant_schedules s
    where s.user_id = new.user_id and s.status = 'active' and s.id <> new.id;
    if v_count + 1 > 10 then
      raise exception 'schedule_cap_active: at most 10 active schedules' using errcode = 'check_violation';
    end if;
  end if;
  return new;
end;
$$;
-- The 0170 trigger binding (before insert or update of rrule, status, user_id)
-- already points at this function; recreate it so a partial 0170 apply cannot
-- leave it missing.
create or replace trigger assistant_schedules_caps before insert or update of rrule, status, user_id
  on public.assistant_schedules
  for each row execute function public.assistant_schedules_caps();

-- A3. Runs: run kind (a Run now click is its own occurrence), the ledger
--     guards (no delete, no truncate: 7-year ledger, never purged), and a
--     forward-only status (claimed < running < terminal).
alter table public.assistant_schedule_runs add column if not exists kind text not null default 'scheduled';
alter table public.assistant_schedule_runs add column if not exists stats jsonb;
do $$
begin
  if not exists (select 1 from pg_constraint where conname = 'assistant_schedule_runs_kind_chk') then
    alter table public.assistant_schedule_runs
      add constraint assistant_schedule_runs_kind_chk check (kind in ('scheduled', 'run_now')) not valid;
    alter table public.assistant_schedule_runs validate constraint assistant_schedule_runs_kind_chk;
  end if;
end $$;
create index if not exists assistant_schedule_runs_schedule_idx
  on public.assistant_schedule_runs (schedule_id, scheduled_for desc);
create index if not exists assistant_schedule_runs_open_idx
  on public.assistant_schedule_runs (started_at) where status in ('claimed', 'running');
create index if not exists assistant_schedule_runs_manual_idx
  on public.assistant_schedule_runs (created_at) where kind = 'run_now' and status = 'claimed' and claimed_by is null;

create or replace function public.assistant_schedule_run_forward()
returns trigger
language plpgsql
security invoker
set search_path = pg_catalog, public
as $$
declare
  v_old int := case old.status when 'claimed' then 0 when 'running' then 1 else 2 end;
  v_new int := case new.status when 'claimed' then 0 when 'running' then 1 else 2 end;
begin
  if new.schedule_id <> old.schedule_id or new.user_id <> old.user_id or new.scheduled_for <> old.scheduled_for
     or new.kind <> old.kind or new.user_label <> old.user_label then
    raise exception 'assistant_schedule_runs: identity columns are immutable' using errcode = 'restrict_violation';
  end if;
  if v_new < v_old or (v_old = 2 and new.status <> old.status) then
    raise exception 'assistant_schedule_runs: status moves forward only (% to %)', old.status, new.status
      using errcode = 'restrict_violation';
  end if;
  return new;
end;
$$;
create or replace trigger assistant_schedule_runs_forward before update on public.assistant_schedule_runs
  for each row execute function public.assistant_schedule_run_forward();
create or replace trigger assistant_schedule_runs_no_delete before delete on public.assistant_schedule_runs
  for each row execute function public.assistant_refuse_change();
create or replace trigger assistant_schedule_runs_no_truncate before truncate on public.assistant_schedule_runs
  for each statement execute function public.assistant_refuse_change();

-- A3b. At most one Run now row waiting for pickup per schedule, so two
--      overlapping Run now requests cannot both insert (the second gets 23505,
--      answered 409 schedule_busy). Scheduled rows never match it.
create unique index if not exists assistant_schedule_runs_one_pending_run_now_uq
  on public.assistant_schedule_runs (schedule_id)
  where kind = 'run_now' and status = 'claimed' and claimed_by is null;

-- A4. The claim. One statement: lock due active schedules (skip the ones the
--     other worker holds), move after_next_run_at into next_run_at (the
--     advance), insert one run per occurrence ON CONFLICT DO NOTHING (the
--     occurrence key makes a double claim harmless), and hand back only rows
--     this call inserted. A schedule whose second slot is not filled yet is
--     not claimable (Python refills it first, docs 3.4.2). An occurrence older
--     than the catch-up window is recorded as skipped 'missed'; one whose
--     previous run is still open (a pending Run now row included) is
--     skipped 'overlap'. The same statement claims Run now rows nobody has
--     taken (kind run_now, claimed_by null, younger than 10 minutes) unless
--     another run of the same schedule is already held by a worker, so one
--     schedule never has two runs in flight.
create or replace function public.claim_due_assistant_schedules(
  p_worker text, p_limit int, p_catchup_seconds int
)
returns setof public.assistant_schedule_runs
language sql
volatile
security invoker
set search_path = pg_catalog, public
as $$
  with due as (
    select s.id, s.user_id, s.next_run_at, s.after_next_run_at
      from public.assistant_schedules s
     where s.status = 'active'
       and s.next_run_at <= now()
       and s.after_next_run_at is not null
     order by s.next_run_at
     limit greatest(1, least(coalesce(p_limit, 20), 50))
     for update skip locked
  ),
  advanced as (
    update public.assistant_schedules s
       set next_run_at = d.after_next_run_at,
           after_next_run_at = null,
           last_run_at = d.next_run_at
      from due d
     where s.id = d.id
    returning s.id, s.user_id, d.next_run_at as scheduled_for, d.after_next_run_at as next_slot
  ),
  labelled as (
    select a.id, a.user_id, a.scheduled_for,
           coalesce(nullif(btrim(p.full_name), ''), p.email, a.user_id::text) as user_label,
           case
             when a.scheduled_for < now() - make_interval(secs => greatest(coalesce(p_catchup_seconds, 10800), 0))
               then 'missed'
             -- A newer occurrence is already due (catch-up after an outage):
             -- this one is superseded, so a backlog collapses to one run.
             when a.next_slot <= now()
               then 'missed'
             when exists (select 1 from public.assistant_schedule_runs r
                           where r.schedule_id = a.id and r.status in ('claimed', 'running')
                             and r.created_at > now() - interval '30 minutes')
               then 'overlap'
           end as skip_kind
      from advanced a
      left join public.profiles p on p.id = a.user_id
  ),
  inserted as (
    insert into public.assistant_schedule_runs
      (schedule_id, user_id, user_label, scheduled_for, kind, status, claimed_by, started_at, finished_at, error_kind)
    select l.id, l.user_id, l.user_label, l.scheduled_for, 'scheduled',
           case when l.skip_kind is null then 'claimed' else 'skipped' end,
           p_worker, now(),
           case when l.skip_kind is null then null else now() end,
           l.skip_kind
      from labelled l
    on conflict (schedule_id, scheduled_for) do nothing
    returning *
  ),
  manual as (
    update public.assistant_schedule_runs r
       set claimed_by = p_worker, started_at = now()
     where r.id in (select r2.id from public.assistant_schedule_runs r2
                     where r2.kind = 'run_now' and r2.status = 'claimed' and r2.claimed_by is null
                       and r2.created_at > now() - interval '10 minutes'
                       and not exists (select 1 from public.assistant_schedule_runs r3
                                        where r3.schedule_id = r2.schedule_id and r3.id <> r2.id
                                          and r3.status in ('claimed', 'running')
                                          and r3.created_at > now() - interval '30 minutes'
                                          -- held by a worker, or an older Run now row nobody took
                                          -- yet: at most one row per schedule per claim
                                          and (r3.claimed_by is not null
                                               or (r3.kind = 'run_now' and (r3.created_at, r3.id) < (r2.created_at, r2.id))))
                     order by r2.created_at
                     limit greatest(1, least(coalesce(p_limit, 20), 50))
                     for update skip locked)
    returning r.*
  )
  select * from inserted
  union all
  select * from manual;
$$;

-- A5. Delivery claim ledger per run and channel (docs 3.4.5). 'claim' sets
--     the channel to "claimed" only while it is absent and the run is
--     running; 'done' records the value (true, or the email_log id); 'failed'
--     records false. A channel is never claimed twice, so a crash between
--     claim and send can lose a delivery but never double it.
create or replace function public.assistant_schedule_run_channel(
  p_user_id uuid, p_run_id uuid, p_channel text, p_action text, p_value jsonb default null
)
returns boolean
language plpgsql
volatile
security invoker
set search_path = pg_catalog, public
as $$
declare
  v_n int;
begin
  if p_channel not in ('chat', 'bell', 'email') or p_action not in ('claim', 'done', 'failed') then
    raise exception 'assistant_schedule_run_channel: bad arguments' using errcode = 'check_violation';
  end if;
  if p_action = 'claim' then
    update public.assistant_schedule_runs r
       set delivered = coalesce(r.delivered, '{}'::jsonb) || jsonb_build_object(p_channel, 'claimed')
     where r.id = p_run_id and r.user_id = p_user_id and r.status = 'running'
       and not (coalesce(r.delivered, '{}'::jsonb) ? p_channel);
  else
    update public.assistant_schedule_runs r
       set delivered = r.delivered || jsonb_build_object(
             p_channel, case when p_action = 'done' then coalesce(p_value, 'true'::jsonb) else 'false'::jsonb end)
     where r.id = p_run_id and r.user_id = p_user_id and r.status = 'running'
       and r.delivered ->> p_channel = 'claimed';
  end if;
  get diagnostics v_n = row_count;
  return v_n = 1;
end;
$$;

-- B. Memory ---------------------------------------------------------------------------

-- B1. Idempotency for tool writes (the ledger's step key, docs 3.7.3) and the
--     revision's base (the memory's updated_at when the revision was proposed).
alter table public.assistant_memories add column if not exists idempotency_key text;
create unique index if not exists assistant_memories_idem_uq
  on public.assistant_memories (user_id, idempotency_key) where idempotency_key is not null;
alter table public.assistant_memory_revisions add column if not exists base_updated_at timestamptz;
alter table public.assistant_memory_revisions add column if not exists idempotency_key text;
create unique index if not exists assistant_memory_revisions_idem_uq
  on public.assistant_memory_revisions (user_id, idempotency_key) where idempotency_key is not null;
-- At most one pending revision per memory; a new proposal supersedes the old.
create unique index if not exists assistant_memory_revisions_one_pending_uq
  on public.assistant_memory_revisions (memory_id) where decided_at is null;
-- The full memory ids a memory_* tool result carried (the model sees only
-- short ids, so a text match on the full id cannot find them; docs 3.7.2).
-- Written by the executor's _store_result; read by op 'delete' (B6).
alter table public.assistant_tool_results add column if not exists memory_ids uuid[];
create index if not exists assistant_tool_results_memory_ids_idx
  on public.assistant_tool_results using gin (memory_ids) where memory_ids is not null;

-- B2. updated_at moves only on a content or state change, so a last_used_at
--     touch never invalidates the expected_updated_at a card or a form holds.
create or replace trigger assistant_memories_updated_at
  before update of kind, title, body, project_id, status, pinned, source, origin_tainted
  on public.assistant_memories
  for each row execute function public.set_updated_at();

-- B3. Event types: add 'confirmed' (the Save click on a pending row) and
--     'moved' (a project note following a project merge). Re-created from the
--     live definition so nothing already allowed is lost.
do $$
declare
  v_name text;
  v_def  text;
begin
  select c.conname, pg_get_constraintdef(c.oid) into v_name, v_def
    from pg_constraint c
   where c.conrelid = 'public.assistant_memory_events'::regclass and c.contype = 'c'
     and pg_get_constraintdef(c.oid) like '%revision_rejected%';
  if v_name is null then
    raise exception '0176: the assistant_memory_events event check is missing; apply 0170 first';
  end if;
  if position('''confirmed''' in v_def) > 0 and position('''moved''' in v_def) > 0 then
    return;
  end if;
  execute format('alter table public.assistant_memory_events drop constraint %I', v_name);
  alter table public.assistant_memory_events add constraint assistant_memory_events_event_chk
    check (event in ('created', 'updated', 'approved', 'rejected', 'pinned', 'unpinned', 'archived', 'restored',
                     'revision_proposed', 'revision_approved', 'revision_rejected', 'confirmed', 'moved')) not valid;
  alter table public.assistant_memory_events validate constraint assistant_memory_events_event_chk;
end $$;

-- B4. Only the user pins. An insert never carries pinned = true, and a row
--     becomes pinned only inside assistant_memory_apply's 'pin' op, which sets
--     the transaction-local flag this trigger reads.
create or replace function public.assistant_memories_pin_guard()
returns trigger
language plpgsql
security invoker
set search_path = pg_catalog, public
as $$
begin
  if tg_op = 'INSERT' then
    if new.pinned then
      raise exception 'memory_pin_user_only: a memory is pinned only by the user' using errcode = 'check_violation';
    end if;
    return new;
  end if;
  if new.pinned and not old.pinned
     and coalesce(current_setting('bdr.assistant_memory_pin', true), '') <> 'user' then
    raise exception 'memory_pin_user_only: a memory is pinned only by the user' using errcode = 'check_violation';
  end if;
  return new;
end;
$$;
create or replace trigger assistant_memories_pin_guard before insert or update of pinned
  on public.assistant_memories
  for each row execute function public.assistant_memories_pin_guard();

-- B5. Near duplicates by title (pg_trgm, 0072). At most 300 live rows per
--     person, so a scan of the person's rows is fine.
create or replace function public.assistant_memory_similar(
  p_user_id uuid, p_title text, p_threshold real default 0.5, p_limit int default 5
)
returns table (id uuid, title text, status text, kind text, similarity real)
language sql
stable
security invoker
set search_path = pg_catalog, public, extensions
as $$
  select m.id, m.title, m.status, m.kind, similarity(m.title, p_title)
    from public.assistant_memories m
   where m.user_id = p_user_id and m.status <> 'archived'
     and similarity(m.title, p_title) > greatest(coalesce(p_threshold, 0.5), 0.3)
   order by similarity(m.title, p_title) desc, m.updated_at desc
   limit greatest(1, least(coalesce(p_limit, 5), 10));
$$;

-- B6. Every memory mutation plus its event in one transaction (PostgREST has
--     none across two calls). Ops per actor: the tool routes pass 'assistant',
--     the Settings routes 'user', the nightly step 'system'. Results are
--     {"ok": true, "replay", "memory", "revision"} or {"ok": false, "code"}.
--     Cap and pin-guard violations raise check_violation with their message
--     prefix (repo maps them). Event payloads never carry title or body text,
--     so a permanent delete has nothing to redact there.

-- B5b. Transcript redaction for op 'delete': every memory_* function_call
--      and its function_call_output (matched by call_id), plus any item that
--      names the full id. Over-redacts other notes' copies in the same
--      transcript on purpose (history only; _history replays content only).
create or replace function public.assistant_redact_memory_items(p_items jsonb, p_id text)
returns jsonb
language sql
immutable
security invoker
set search_path = pg_catalog, public
as $$
  with calls as (
    select distinct i->>'call_id' as call_id
      from jsonb_array_elements(p_items) as c(i)
     where i->>'type' = 'function_call' and i->>'name' like 'memory\_%'
  )
  select coalesce(jsonb_agg(
           case
             when i->>'type' = 'function_call'
                  and (i->>'call_id' in (select call_id from calls) or i::text like '%' || p_id || '%')
               then jsonb_build_object('type', 'function_call', 'call_id', i->'call_id', 'name', i->'name',
                                       'arguments', '{"redacted":true}')
             when i->>'type' = 'function_call_output'
                  and (i->>'call_id' in (select call_id from calls) or i::text like '%' || p_id || '%')
               then jsonb_build_object('type', 'function_call_output', 'call_id', i->'call_id',
                                       'output', '{"redacted":true}')
             when i::text like '%' || p_id || '%'
               then jsonb_build_object('type', i->'type', 'redacted', true)
             else i
           end order by o), '[]'::jsonb)
    from jsonb_array_elements(p_items) with ordinality as t(i, o);
$$;

create or replace function public.assistant_memory_apply(
  p_user_id uuid, p_op text, p_memory_id uuid, p_expected_updated_at timestamptz,
  p_fields jsonb, p_actor text, p_conversation_id uuid, p_message_id uuid, p_idempotency_key text
)
returns jsonb
language plpgsql
volatile
security invoker
set search_path = pg_catalog, public
as $$
declare
  v_f       jsonb := coalesce(p_fields, '{}'::jsonb);
  v_row     public.assistant_memories%rowtype;
  v_rev     public.assistant_memory_revisions%rowtype;
  v_old     public.assistant_memory_revisions%rowtype;
  v_event   text;
  v_payload jsonb := '{}'::jsonb;
  v_id      text;
  v_msgs    uuid[];
  v_pending int;
  v_before  timestamptz;
begin
  if not ((p_actor = 'assistant' and p_op in ('create', 'revise_pending', 'propose_revision', 'forget'))
       or (p_actor = 'user' and p_op in ('create', 'edit', 'approve', 'confirm', 'reject', 'archive', 'restore',
                                          'pin', 'unpin', 'approve_revision', 'reject_revision', 'delete'))
       or (p_actor = 'system' and p_op in ('archive', 'move', 'delete'))) then
    raise exception 'assistant_memory_apply: op % is not allowed for actor %', p_op, p_actor
      using errcode = 'check_violation';
  end if;

  if p_op = 'create' then
    if p_idempotency_key is not null then
      select * into v_row from public.assistant_memories m
       where m.user_id = p_user_id and m.idempotency_key = p_idempotency_key;
      if found then
        return jsonb_build_object('ok', true, 'replay', true, 'memory', to_jsonb(v_row) - 'tsv');
      end if;
    end if;
    if (p_actor = 'assistant' and not (coalesce(v_f->>'status', '') in ('active', 'pending_review')
                                       and coalesce(v_f->>'source', '') in ('user_stated', 'inferred')))
       or (p_actor = 'user' and not (coalesce(v_f->>'status', '') = 'active'
                                     and coalesce(v_f->>'source', '') = 'user_settings')) then
      raise exception 'assistant_memory_apply: bad status or source for %', p_actor using errcode = 'check_violation';
    end if;
    if v_f->>'status' = 'pending_review' then
      -- The 0170 caps trigger ignores non-active rows; bound pending ones
      -- here under the same per-person lock (docs 3.8.2).
      perform pg_advisory_xact_lock(hashtext('assistant_memories:' || p_user_id::text));
      select count(*) into v_pending from public.assistant_memories m
       where m.user_id = p_user_id and m.status = 'pending_review';
      if v_pending + 1 > 50 then
        raise exception 'memory_cap_pending: at most 50 notes waiting for review' using errcode = 'check_violation';
      end if;
    end if;
    insert into public.assistant_memories (user_id, kind, title, body, project_id, status, source, origin_tainted,
                                           conversation_id, message_id, idempotency_key)
    values (p_user_id, v_f->>'kind', v_f->>'title', coalesce(v_f->>'body', ''),
            nullif(v_f->>'project_id', '')::uuid, v_f->>'status', v_f->>'source',
            coalesce((v_f->>'origin_tainted')::boolean, false),
            p_conversation_id, p_message_id, p_idempotency_key)
    returning * into v_row;
    v_event := 'created';
    v_payload := jsonb_build_object('status', v_row.status, 'source', v_row.source,
                                    'origin_tainted', v_row.origin_tainted);
  else
    select * into v_row from public.assistant_memories m
     where m.id = p_memory_id and m.user_id = p_user_id
     for update;
    if not found then
      return jsonb_build_object('ok', false, 'code', 'memory_not_found');
    end if;
    v_before := v_row.updated_at;
    if p_expected_updated_at is not null and v_row.updated_at is distinct from p_expected_updated_at then
      return jsonb_build_object('ok', false, 'code', 'memory_stale', 'memory', to_jsonb(v_row) - 'tsv');
    end if;

    if p_op = 'edit' then
      if v_row.status = 'archived' then
        return jsonb_build_object('ok', false, 'code', 'memory_archived');
      end if;
      update public.assistant_memories m set
        kind = coalesce(v_f->>'kind', m.kind),
        title = coalesce(v_f->>'title', m.title),
        body = coalesce(v_f->>'body', m.body),
        project_id = case when v_f ? 'project_id' then nullif(v_f->>'project_id', '')::uuid else m.project_id end,
        source = 'user_settings'
       where m.id = v_row.id returning * into v_row;
      v_event := 'updated';
      v_payload := jsonb_build_object('fields', (select coalesce(jsonb_agg(k order by k), '[]'::jsonb)
                                                   from jsonb_object_keys(v_f) k));
    elsif p_op = 'revise_pending' then
      if v_row.status <> 'pending_review' then
        return jsonb_build_object('ok', false, 'code', 'memory_wrong_status');
      end if;
      update public.assistant_memories m set
        title = coalesce(v_f->>'title', m.title), body = coalesce(v_f->>'body', m.body),
        origin_tainted = m.origin_tainted or coalesce((v_f->>'origin_tainted')::boolean, false)
       where m.id = v_row.id returning * into v_row;
      v_event := 'updated';
    elsif p_op in ('approve', 'confirm') then
      if v_row.status <> 'pending_review' then
        return jsonb_build_object('ok', false, 'code', 'memory_wrong_status');
      end if;
      update public.assistant_memories m set status = 'active', source = 'user_confirmed'
       where m.id = v_row.id returning * into v_row;
      v_event := case when p_op = 'approve' then 'approved' else 'confirmed' end;
    elsif p_op = 'reject' then
      if v_row.status <> 'pending_review' then
        return jsonb_build_object('ok', false, 'code', 'memory_wrong_status');
      end if;
      update public.assistant_memories m set status = 'archived', archived_at = now()
       where m.id = v_row.id returning * into v_row;
      v_event := 'rejected';
    elsif p_op in ('archive', 'forget') then
      if v_row.status = 'archived' then
        return jsonb_build_object('ok', true, 'replay', true, 'memory', to_jsonb(v_row) - 'tsv');
      end if;
      if p_op = 'forget' and v_row.pinned then
        return jsonb_build_object('ok', false, 'code', 'memory_pinned_user_only');
      end if;
      update public.assistant_memories m set status = 'archived', archived_at = now(), pinned = false
       where m.id = v_row.id returning * into v_row;
      v_event := 'archived';
      v_payload := jsonb_build_object('by', p_op);
    elsif p_op = 'restore' then
      if v_row.status <> 'archived' then
        return jsonb_build_object('ok', false, 'code', 'memory_wrong_status');
      end if;
      update public.assistant_memories m set status = 'active', archived_at = null
       where m.id = v_row.id returning * into v_row;
      v_event := 'restored';
    elsif p_op in ('pin', 'unpin') then
      if v_row.status <> 'active' then
        return jsonb_build_object('ok', false, 'code', 'memory_wrong_status');
      end if;
      if p_op = 'pin' then
        perform set_config('bdr.assistant_memory_pin', 'user', true);
      end if;
      update public.assistant_memories m set pinned = (p_op = 'pin')
       where m.id = v_row.id returning * into v_row;
      v_event := case when p_op = 'pin' then 'pinned' else 'unpinned' end;
    elsif p_op = 'propose_revision' then
      if v_row.status <> 'active' then
        return jsonb_build_object('ok', false, 'code', 'memory_wrong_status');
      end if;
      if v_row.pinned then
        return jsonb_build_object('ok', false, 'code', 'memory_pinned_user_only');
      end if;
      if p_idempotency_key is not null then
        select * into v_rev from public.assistant_memory_revisions r
         where r.user_id = p_user_id and r.idempotency_key = p_idempotency_key;
        if found then
          return jsonb_build_object('ok', true, 'replay', true, 'memory', to_jsonb(v_row) - 'tsv',
                                    'revision', to_jsonb(v_rev));
        end if;
      end if;
      for v_old in
        update public.assistant_memory_revisions r set decision = 'rejected', decided_at = now()
         where r.memory_id = v_row.id and r.decided_at is null
        returning *
      loop
        insert into public.assistant_memory_events (memory_id, user_id, event, actor, payload)
        values (v_row.id, p_user_id, 'revision_rejected', 'system',
                jsonb_build_object('revision_id', v_old.id, 'superseded', true));
      end loop;
      insert into public.assistant_memory_revisions (memory_id, user_id, title, body, origin_tainted,
                                                     conversation_id, message_id, base_updated_at, idempotency_key)
      values (v_row.id, p_user_id, coalesce(v_f->>'title', v_row.title), coalesce(v_f->>'body', v_row.body),
              coalesce((v_f->>'origin_tainted')::boolean, false), p_conversation_id, p_message_id,
              v_row.updated_at, p_idempotency_key)
      returning * into v_rev;
      v_event := 'revision_proposed';
      v_payload := jsonb_build_object('revision_id', v_rev.id);
    elsif p_op in ('approve_revision', 'reject_revision') then
      select * into v_rev from public.assistant_memory_revisions r
       where r.id = nullif(v_f->>'revision_id', '')::uuid and r.memory_id = v_row.id and r.decided_at is null
       for update;
      if not found then
        return jsonb_build_object('ok', false, 'code', 'revision_not_found');
      end if;
      if p_op = 'approve_revision' then
        if v_row.status <> 'active' then
          return jsonb_build_object('ok', false, 'code', 'memory_wrong_status');
        end if;
        if v_rev.base_updated_at is distinct from v_row.updated_at then
          return jsonb_build_object('ok', false, 'code', 'memory_stale', 'memory', to_jsonb(v_row) - 'tsv');
        end if;
        update public.assistant_memories m set title = v_rev.title, body = v_rev.body,
               origin_tainted = m.origin_tainted or v_rev.origin_tainted
         where m.id = v_row.id returning * into v_row;
        update public.assistant_memory_revisions r set decision = 'approved', decided_at = now()
         where r.id = v_rev.id returning * into v_rev;
        v_event := 'revision_approved';
      else
        update public.assistant_memory_revisions r set decision = 'rejected', decided_at = now()
         where r.id = v_rev.id returning * into v_rev;
        v_event := 'revision_rejected';
      end if;
      v_payload := jsonb_build_object('revision_id', v_rev.id);
    elsif p_op = 'move' then
      v_payload := jsonb_build_object('from', v_row.project_id, 'to', v_f->>'project_id');
      update public.assistant_memories m set project_id = nullif(v_f->>'project_id', '')::uuid
       where m.id = v_row.id returning * into v_row;
      v_event := 'moved';
    elsif p_op = 'delete' then
      -- Permanent delete (docs 3.8.7). Refused under legal hold for every
      -- actor (the design: legal hold suspends every purge). Copies first,
      -- then the row; events and revisions go with it by cascade
      -- (allow_cascade, 0170 section 8b).
      if exists (select 1 from public.assistant_profiles ap where ap.user_id = p_user_id and ap.legal_hold) then
        return jsonb_build_object('ok', false, 'code', 'legal_hold');
      end if;
      v_id := v_row.id::text;
      -- Messages whose transcripts may carry the note: where it was saved,
      -- changed or revised, and where a memory tool returned it (by the full
      -- ids the executor recorded, B1), not by matching short ids as text.
      select coalesce(array_agg(distinct s.x), '{}'::uuid[]) into v_msgs from (
        select v_row.message_id as x
        union select e.message_id from public.assistant_memory_events e where e.memory_id = v_row.id
        union select r.message_id from public.assistant_memory_revisions r where r.memory_id = v_row.id
        union select t.message_id from public.assistant_tool_results t
               where t.user_id = p_user_id and t.memory_ids @> array[v_row.id]
      ) s where s.x is not null;
      update public.assistant_messages m set used_memory_ids = array_remove(m.used_memory_ids, v_row.id)
       where m.user_id = p_user_id and m.used_memory_ids @> array[v_row.id];
      update public.assistant_messages m set blocks = (
               select coalesce(jsonb_agg(
                 case when b->>'type' = 'transcript' and jsonb_typeof(b->'items') = 'array' then
                   jsonb_set(b, '{items}', public.assistant_redact_memory_items(b->'items', v_id))
                 else b end order by bo), '[]'::jsonb)
                 from jsonb_array_elements(m.blocks) with ordinality as u(b, bo))
       where m.user_id = p_user_id and jsonb_typeof(m.blocks) = 'array'
         and (m.id = any(v_msgs) or m.blocks::text like '%' || v_id || '%');
      update public.assistant_tool_results t set payload = jsonb_build_object('redacted', true),
             payload_sha256 = null, taint_corpus = null
       where t.user_id = p_user_id and t.tool_name like 'memory\_%'
         and (t.memory_ids @> array[v_row.id] or t.payload::text like '%' || v_id || '%');
      delete from public.assistant_memories m where m.id = v_row.id;
      return jsonb_build_object('ok', true, 'deleted', true, 'memory_id', v_row.id);
    end if;
  end if;

  -- An op that leaves the title and body alone (B2 still moves updated_at)
  -- re-bases an open revision that was current, so approve_revision's fence
  -- refuses only a revision whose base content changed.
  if p_op in ('archive', 'forget', 'restore', 'pin', 'unpin', 'move') then
    update public.assistant_memory_revisions r set base_updated_at = v_row.updated_at
     where r.memory_id = v_row.id and r.decided_at is null and r.base_updated_at = v_before;
  end if;

  insert into public.assistant_memory_events (memory_id, user_id, event, actor, payload, conversation_id, message_id)
  values (v_row.id, p_user_id, v_event, p_actor, v_payload, p_conversation_id, p_message_id);
  return jsonb_build_object('ok', true, 'replay', false, 'memory', to_jsonb(v_row) - 'tsv',
                            'revision', case when v_rev.id is null then null else to_jsonb(v_rev) end);
end;
$$;

-- B7. Usage touch (no updated_at change, B2).
create or replace function public.assistant_memory_touch(p_user_id uuid, p_ids uuid[])
returns int
language sql
volatile
security invoker
set search_path = pg_catalog, public
as $$
  with t as (
    update public.assistant_memories m set last_used_at = now(), use_count = m.use_count + 1
     where m.user_id = p_user_id and m.id = any(p_ids) and m.status = 'active'
    returning 1
  )
  select count(*)::int from t;
$$;

-- B8. A turn tainted only by its notes data block (docs 3.9.2) records that
--     here, not in assistant_messages.tainted: the 0170 assistant_message_taint
--     trigger fires on that column and would mark the conversation for good.
alter table public.assistant_messages add column if not exists notes_tainted boolean not null default false;

-- C. Ledger effect tier 'housekeeping' (docs 3.7): inline writes to the
--    person's own assistant rows. Re-created from the live definition.
do $$
declare
  v_name text;
  v_def  text;
begin
  select c.conname, pg_get_constraintdef(c.oid) into v_name, v_def
    from pg_constraint c
   where c.conrelid = 'public.assistant_tool_calls'::regclass and c.contype = 'c'
     and pg_get_constraintdef(c.oid) like '%sensitive_read%';
  if v_name is null then
    raise exception '0176: the assistant_tool_calls effect check is missing; apply 0170 first';
  end if;
  if position('''housekeeping''' in v_def) > 0 then
    return;
  end if;
  execute format('alter table public.assistant_tool_calls drop constraint %I', v_name);
  alter table public.assistant_tool_calls add constraint assistant_tool_calls_effect_chk
    check (effect in ('read', 'sensitive_read', 'write_internal', 'external_send', 'expensive', 'housekeeping'))
    not valid;
  alter table public.assistant_tool_calls validate constraint assistant_tool_calls_effect_chk;
end $$;

-- D. Holidays: seeded once, only while the list is empty (owner question 7;
--    Executive and IT Admin edit it in Settings).
update public.assistant_settings
   set holidays = array['2026-11-26', '2026-11-27', '2026-12-25', '2027-01-01', '2027-05-31', '2027-07-05',
                        '2027-09-06', '2027-11-25', '2027-11-26', '2027-12-24']::date[]
 where id = 1 and cardinality(coalesce(holidays, '{}'::date[])) = 0;

-- E. Sweep claims: one row per step per Pacific day, so the nightly step
--    runs once across both workers.
create table if not exists public.assistant_sweeps (
  step         text not null check (step in ('nightly', 'orphans')),
  sweep_date   date not null,
  claimed_by   text not null,
  started_at   timestamptz not null default now(),
  finished_at  timestamptz,
  result       jsonb,
  primary key (step, sweep_date)
);
alter table public.assistant_sweeps enable row level security;
alter table public.assistant_sweeps force row level security;
revoke all on table public.assistant_sweeps from anon, authenticated;

-- F. Phase 7 owns the inbox router; the schema must already be there.
do $$
begin
  if to_regclass('public.assistant_events') is null or to_regclass('public.assistant_subscriptions') is null
     or to_regclass('public.assistant_inbox') is null then
    raise exception '0176: the 0170 inbox tables are missing; apply 0170 first';
  end if;
end $$;

-- G. Execute for service_role only (0145 pattern).
revoke all on function public.assistant_schedules_caps() from public;
revoke all on function public.assistant_schedules_caps() from anon, authenticated;
grant execute on function public.assistant_schedules_caps() to service_role;
revoke all on function public.assistant_schedule_run_forward() from public;
revoke all on function public.assistant_schedule_run_forward() from anon, authenticated;
grant execute on function public.assistant_schedule_run_forward() to service_role;
revoke all on function public.claim_due_assistant_schedules(text, int, int) from public;
revoke all on function public.claim_due_assistant_schedules(text, int, int) from anon, authenticated;
grant execute on function public.claim_due_assistant_schedules(text, int, int) to service_role;
revoke all on function public.assistant_schedule_run_channel(uuid, uuid, text, text, jsonb) from public;
revoke all on function public.assistant_schedule_run_channel(uuid, uuid, text, text, jsonb) from anon, authenticated;
grant execute on function public.assistant_schedule_run_channel(uuid, uuid, text, text, jsonb) to service_role;
revoke all on function public.assistant_memories_pin_guard() from public;
revoke all on function public.assistant_memories_pin_guard() from anon, authenticated;
grant execute on function public.assistant_memories_pin_guard() to service_role;
revoke all on function public.assistant_memory_similar(uuid, text, real, int) from public;
revoke all on function public.assistant_memory_similar(uuid, text, real, int) from anon, authenticated;
grant execute on function public.assistant_memory_similar(uuid, text, real, int) to service_role;
revoke all on function public.assistant_redact_memory_items(jsonb, text) from public;
revoke all on function public.assistant_redact_memory_items(jsonb, text) from anon, authenticated;
grant execute on function public.assistant_redact_memory_items(jsonb, text) to service_role;
revoke all on function public.assistant_memory_apply(uuid, text, uuid, timestamptz, jsonb, text, uuid, uuid, text) from public;
revoke all on function public.assistant_memory_apply(uuid, text, uuid, timestamptz, jsonb, text, uuid, uuid, text) from anon, authenticated;
grant execute on function public.assistant_memory_apply(uuid, text, uuid, timestamptz, jsonb, text, uuid, uuid, text) to service_role;
revoke all on function public.assistant_memory_touch(uuid, uuid[]) from public;
revoke all on function public.assistant_memory_touch(uuid, uuid[]) from anon, authenticated;
grant execute on function public.assistant_memory_touch(uuid, uuid[]) to service_role;

notify pgrst, 'reload schema';
