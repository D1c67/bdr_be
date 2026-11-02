-- 0190: Assistant phase 7, the proactive router (docs/ASSISTANT_PHASE7_PLAN.md, section 3).
-- Idempotent; safe to run twice, in either part or piece order: part A's feedback
-- RPC never replaces part B's (a guard skips it once the live one accepts mute_project).
-- Dev project (BDR) only until the release.
-- Never re-lists llm_jobs_job_type_check and touches no quotes object.
--
-- Two parts split by the PART B marker line below. Part A goes through
-- apply_migration as 0190_assistant_phase7_a and holds every new object
-- (tables, columns, checks, indexes, the classify trigger and the fourteen
-- router RPCs A11.1 to A11.14). Part B (the feedback check swap, the
-- feedback RPC that accepts mute_project, the reset and the purge) goes
-- through as 0190_assistant_phase7_b with the owner at the screen.
-- Part A is larger than the usual 40 KB, so it carries two optional split
-- points; applied in order, the three pieces equal part A (notify is last).
-- Each piece revokes and grants every function it creates, so no function is
-- ever left executable by anon or authenticated between pieces.
--
-- Conventions (0170, 0176): RLS enabled and forced with no policies and
-- nothing granted to anon or authenticated; every function is SECURITY
-- INVOKER with search_path pinned to pg_catalog, public and executable by
-- service_role only; project_id has no FK (0141); every router RPC writes its
-- times from p_now (D12), so the dev tick script can time-travel.
--
-- ==== PART A ====

-- A1. Company switch (IT Admin and Executive edit it; default off).
alter table public.assistant_settings
  add column if not exists proactive_enabled boolean not null default false;

-- A2. Outbox claim, status and outcome.
alter table public.assistant_events
  add column if not exists status          text not null default 'pending',
  add column if not exists source          text,
  add column if not exists claimed_by      text,
  add column if not exists claimed_at      timestamptz,
  add column if not exists attempts        integer not null default 0,
  add column if not exists next_attempt_at timestamptz,
  add column if not exists last_error      text,
  add column if not exists routed          jsonb not null default '{}'::jsonb;
do $$
begin
  if not exists (select 1 from pg_constraint where conname = 'assistant_events_status_chk') then
    alter table public.assistant_events add constraint assistant_events_status_chk
      check (status in ('pending', 'claimed', 'processed', 'skipped', 'failed')) not valid;
    alter table public.assistant_events validate constraint assistant_events_status_chk;
  end if;
  if not exists (select 1 from pg_constraint where conname = 'assistant_events_done_chk') then
    alter table public.assistant_events add constraint assistant_events_done_chk
      check ((status in ('processed', 'skipped', 'failed')) = (processed_at is not null)) not valid;
    alter table public.assistant_events validate constraint assistant_events_done_chk;
  end if;
  if not exists (select 1 from pg_constraint where conname = 'assistant_events_shape_chk') then
    alter table public.assistant_events add constraint assistant_events_shape_chk
      check (event_type ~ '^[a-z][a-z0-9_.]{2,63}$'
             and char_length(dedupe_key) between 4 and 300
             and attempts >= 0
             and (last_error is null or char_length(last_error) <= 500)
             and (source is null or source in ('bell_tail', 'audit_tail', 'sweeper', 'emit'))
             and pg_column_size(payload) <= 4096) not valid;
    alter table public.assistant_events validate constraint assistant_events_shape_chk;
  end if;
end $$;
create index if not exists assistant_events_purge_idx
  on public.assistant_events (processed_at) where processed_at is not null;
-- assistant_events_unprocessed_idx (occurred_at) where processed_at is null (0170) serves the claim.

-- A3. Subscriptions: who changed a row, and roles limited to internal roles.
alter table public.assistant_subscriptions
  add column if not exists source     text not null default 'admin',
  add column if not exists updated_by uuid references public.profiles(id) on delete set null;
do $$
begin
  if not exists (select 1 from pg_constraint where conname = 'assistant_subscriptions_source_chk') then
    alter table public.assistant_subscriptions add constraint assistant_subscriptions_source_chk
      check (source in ('admin', 'user', 'feedback_auto')
             and (role is null or source = 'admin')
             and event_type ~ '^[a-z][a-z0-9_.]{2,63}$') not valid;
    alter table public.assistant_subscriptions validate constraint assistant_subscriptions_source_chk;
  end if;
  if not exists (select 1 from pg_constraint where conname = 'assistant_subscriptions_role_chk') then
    -- The list equals app.core.roles.INTERNAL_ROLES, six values (asserted by the migration test).
    -- The legacy enum labels 'estimating_engineer' and 'pe' and the external 'estimator' are refused.
    alter table public.assistant_subscriptions add constraint assistant_subscriptions_role_chk
      check (role is null or role in ('executive', 'estimating_admin', 'it_admin', 'accountant',
             'estimating_engineer_materials', 'estimating_engineer_labor')) not valid;
    alter table public.assistant_subscriptions validate constraint assistant_subscriptions_role_chk;
  end if;
end $$;

-- A4. Digest ledger: one row per user, Pacific date and slot. The claim.
create table if not exists public.assistant_digests (
  id              uuid primary key default gen_random_uuid(),
  user_id         uuid not null references public.profiles(id) on delete cascade,
  digest_date     date not null,                                   -- Pacific office date
  slot            text not null check (slot in ('am', 'pm')),
  slot_at         timestamptz not null,                            -- the slot instant (07:00 or 13:00 PT)
  since_at        timestamptz not null,                            -- start of the "other updates" window
  item_count      integer not null default 0 check (item_count >= 0),
  record_count    integer not null default 0 check (record_count >= 0),
  notification_id uuid,                                            -- the bell row; no FK
  claimed_by      text,
  created_at      timestamptz not null,                            -- written from p_now (D12), no default
  constraint assistant_digests_slot_uq unique (user_id, digest_date, slot)
);
create index if not exists assistant_digests_purge_idx on public.assistant_digests (created_at);

-- A5. Inbox: kind, delivery mode, precedence outcome, hold, digest link, bell link, dismissal.
alter table public.assistant_inbox
  add column if not exists event_type       text,
  add column if not exists delivery         text not null default 'direct',
  add column if not exists precedence       text,
  add column if not exists deliver_after    timestamptz,
  add column if not exists digest_id        uuid references public.assistant_digests(id) on delete set null,
  add column if not exists notification_id  uuid,                  -- the router's own bell row; no FK
  add column if not exists source_notification_id uuid,            -- record rows only: the BDR bell mirrored; no FK
  add column if not exists dismissed_at     timestamptz,
  add column if not exists dismissed_reason text,
  add column if not exists snooze_count     smallint not null default 0;
do $$
begin
  if not exists (select 1 from pg_constraint where conname = 'assistant_inbox_delivery_chk') then
    -- Router rows always carry deliver_after: immediate = when it may ring (p_now, or the next
    -- 06:00 PT when held), digest = the slot instant it belongs to. Delivery depends on row state.
    alter table public.assistant_inbox add constraint assistant_inbox_delivery_chk
      check (delivery in ('direct', 'immediate', 'digest', 'record')
             and (delivery not in ('immediate', 'digest') or deliver_after is not null)
             and (source_notification_id is null or delivery = 'record')
             and (precedence is null or char_length(precedence) <= 64)
             and snooze_count between 0 and 3
             and (event_type is null or event_type ~ '^[a-z][a-z0-9_.]{2,63}$')) not valid;
    alter table public.assistant_inbox validate constraint assistant_inbox_delivery_chk;
  end if;
  if not exists (select 1 from pg_constraint where conname = 'assistant_inbox_dismissed_chk') then
    alter table public.assistant_inbox add constraint assistant_inbox_dismissed_chk
      check ((dismissed_at is null) = (dismissed_reason is null)
             and (dismissed_reason is null or dismissed_reason in
                  ('user', 'project_closed', 'owner_offboarded', 'superseded', 'muted'))) not valid;
    alter table public.assistant_inbox validate constraint assistant_inbox_dismissed_chk;
  end if;
end $$;

-- Direct rows (0184 calendar and mail notices, 0187 browser notices) name their columns and
-- never set event_type: classify them by key prefix. Router rows (p7:) carry their own kind.
create or replace function public.assistant_inbox_classify()
returns trigger
language plpgsql
security invoker
set search_path = pg_catalog, public
as $$
begin
  if new.event_type is null then
    new.event_type := case split_part(new.dedupe_key, ':', 1)
      when 'browser' then 'assistant.browser'
      when 'calendar' then 'assistant.calendar'
      when 'p7' then null
      else 'assistant.mailbox_access' end;
  end if;
  return new;
end $$;
do $$
begin
  if not exists (select 1 from pg_trigger where tgname = 'assistant_inbox_classify_biu'
                 and tgrelid = 'public.assistant_inbox'::regclass) then
    create trigger assistant_inbox_classify_biu before insert on public.assistant_inbox
      for each row execute function public.assistant_inbox_classify();
  end if;
end $$;
revoke all on function public.assistant_inbox_classify() from public;
revoke all on function public.assistant_inbox_classify() from anon, authenticated;
grant execute on function public.assistant_inbox_classify() to service_role;
update public.assistant_inbox
   set event_type = case split_part(dedupe_key, ':', 1)
     when 'browser' then 'assistant.browser' when 'calendar' then 'assistant.calendar'
     else 'assistant.mailbox_access' end
 where event_type is null and dedupe_key not like 'p7:%';
create index if not exists assistant_inbox_list_idx
  on public.assistant_inbox (user_id, created_at desc) where dismissed_at is null;
-- Unread badge: router-owned rows only. Record rows take read state from their source bell,
-- direct rows (0184, 0187) are read in the bell they came with; neither lights the badge.
create index if not exists assistant_inbox_unread_idx
  on public.assistant_inbox (user_id)
  where delivery in ('immediate', 'digest') and read_at is null and delivered_at is not null and dismissed_at is null;
create index if not exists assistant_inbox_source_bell_idx
  on public.assistant_inbox (source_notification_id) where source_notification_id is not null;
create index if not exists assistant_inbox_pending_digest_idx
  on public.assistant_inbox (user_id, deliver_after)
  where delivery = 'digest' and delivered_at is null and dismissed_at is null;
create index if not exists assistant_inbox_pending_immediate_idx
  on public.assistant_inbox (deliver_after)
  where delivery = 'immediate' and delivered_at is null and dismissed_at is null;
create index if not exists assistant_inbox_digest_idx
  on public.assistant_inbox (digest_id) where digest_id is not null;
create index if not exists assistant_inbox_created_idx on public.assistant_inbox (created_at);

-- A6. Per-user project mutes (until unmuted or the project closes).
create table if not exists public.assistant_project_mutes (
  user_id    uuid not null references public.profiles(id) on delete cascade,
  project_id uuid not null,                                         -- no FK (0141)
  created_at timestamptz not null default now(),
  primary key (user_id, project_id)
);

-- A7. Tail cursors: a keyset (cursor_at, cursor_id) per source. The seed is only a starting
-- value: the loop reseeds a cursor older than ASSISTANT_PROACTIVE_CURSOR_MAX_LAG_MINUTES at start,
-- and the company switch reseeds both when it flips from off to on (4.1, 6.1). Two tails only
-- (owner question 17, decision D).
create table if not exists public.assistant_router_cursors (
  name       text primary key check (name in ('notifications', 'audit_log')),
  cursor_at  timestamptz not null,
  cursor_id  uuid,                                                  -- tie-break inside cursor_at
  updated_by text,
  updated_at timestamptz not null default now()
);
insert into public.assistant_router_cursors (name, cursor_at)
values ('notifications', now()), ('audit_log', now())
on conflict (name) do nothing;

-- A8. Daily step ledger with a lease (digest slots and the purge).
create table if not exists public.assistant_proactive_slots (
  step        text not null check (step in ('digest_am', 'digest_pm', 'purge')),
  slot_date   date not null,                                        -- Pacific office date
  claimed_by  text not null,
  claimed_at  timestamptz not null,                                 -- written from the tick's now (D12)
  finished_at timestamptz,
  result      jsonb not null default '{}'::jsonb,                   -- counts only; failures and error kind
  primary key (step, slot_date)
);

-- A9. The bell tail reads notifications by insertion time. On prod the release builds this
-- index concurrently by hand first (owner question 27); then this line is a no-op.
create index if not exists notifications_created_at_idx on public.notifications (created_at, id);

-- A9a. Send ledger: one row per (target, channel), exactly one external send each (8.5).
-- Rows are inserted only inside assistant_inbox_deliver (Teams) and assistant_digest_deliver
-- (email), in the same transaction as the bell; sends are claimed and settled by A11.12 and A11.13.
-- Ids, enums and an error kind only: never an address or an exception text.
create table if not exists public.assistant_external_sends (
  id              uuid primary key default gen_random_uuid(),
  user_id         uuid not null references public.profiles(id) on delete cascade,
  target_kind     text not null check (target_kind in ('inbox', 'digest')),
  target_id       uuid not null,                                    -- inbox row or digest id; no FK
  channel         text not null check (channel in ('teams', 'email')),
  status          text not null default 'pending',
  not_before      timestamptz not null,                             -- p_now, or quiet-hours end (P31)
  claimed_by      text,
  claimed_at      timestamptz,                                      -- the lease starts here
  attempts        integer not null default 0,
  next_attempt_at timestamptz,                                      -- retry backoff
  sent_at         timestamptz,
  error_kind      text,                                             -- an enum, never an exception text
  email_log_id    uuid,                                             -- email only: the email_log row; no FK
  created_at      timestamptz not null,                             -- written from p_now (D12)
  updated_at      timestamptz not null,
  constraint assistant_external_sends_target_uq unique (target_kind, target_id, channel),
  constraint assistant_external_sends_state_chk check (
    status in ('pending', 'claimed', 'sent', 'failed', 'skipped', 'uncertain')
    and attempts between 0 and 5
    and ((status = 'sent') = (sent_at is not null))
    and (status <> 'claimed' or (claimed_by is not null and claimed_at is not null))
    and (error_kind is null or error_kind in ('transient', 'permanent', 'uncertain', 'lease_expired',
         'channel_off', 'opted_out', 'stale', 'offboarded', 'quiet_hours'))
    and (channel = 'email' or email_log_id is null)
    and ((target_kind = 'inbox' and channel = 'teams') or (target_kind = 'digest' and channel = 'email')))
);
create index if not exists assistant_external_sends_due_idx
  on public.assistant_external_sends (not_before) where status = 'pending';
create index if not exists assistant_external_sends_claimed_idx
  on public.assistant_external_sends (claimed_at) where status = 'claimed';
create index if not exists assistant_external_sends_purge_idx
  on public.assistant_external_sends (created_at);
create index if not exists assistant_external_sends_user_idx
  on public.assistant_external_sends (user_id, status);

-- A10. RLS, forced, no policies; nothing for anon or authenticated.
alter table public.assistant_digests enable row level security;
alter table public.assistant_digests force row level security;
revoke all on table public.assistant_digests from anon, authenticated;
alter table public.assistant_project_mutes enable row level security;
alter table public.assistant_project_mutes force row level security;
revoke all on table public.assistant_project_mutes from anon, authenticated;
alter table public.assistant_router_cursors enable row level security;
alter table public.assistant_router_cursors force row level security;
revoke all on table public.assistant_router_cursors from anon, authenticated;
alter table public.assistant_proactive_slots enable row level security;
alter table public.assistant_proactive_slots force row level security;
revoke all on table public.assistant_proactive_slots from anon, authenticated;
alter table public.assistant_external_sends enable row level security;
alter table public.assistant_external_sends force row level security;
revoke all on table public.assistant_external_sends from anon, authenticated;

-- ---- PART A split point 2 (optional; part A also applies in one piece) ----

-- A11.1 Event claim. Retires poison events (five claims that never finished) as
--       failed lease_exhausted, then claims pending or lease-expired events with
--       skip locked. Attempts rise at claim (the claim_llm_jobs rule). p_ids lets a
--       slot holder take exactly its slot's events (5.2).
create or replace function public.assistant_events_claim(
  p_worker text, p_limit int, p_lease_seconds int, p_now timestamptz default now(), p_ids uuid[] default null
)
returns setof public.assistant_events
language plpgsql
volatile
security invoker
set search_path = pg_catalog, public
as $$
declare
  v_lease interval := make_interval(secs => greatest(60, coalesce(p_lease_seconds, 600)));
begin
  if p_worker is null or btrim(p_worker) = '' or p_now is null then
    raise exception 'assistant_events_claim: bad arguments' using errcode = 'check_violation';
  end if;
  update public.assistant_events e
     set status = 'failed', processed_at = p_now, last_error = 'lease_exhausted'
   where e.processed_at is null and e.status = 'claimed'
     and e.claimed_at < p_now - v_lease and e.attempts >= 5
     and (p_ids is null or e.id = any(p_ids));
  return query
  with picked as (
    select e.id from public.assistant_events e
     where e.processed_at is null and e.attempts < 5
       and (p_ids is null or e.id = any(p_ids))
       and (e.next_attempt_at is null or e.next_attempt_at <= p_now)
       and (e.status = 'pending' or (e.status = 'claimed' and e.claimed_at < p_now - v_lease))
     order by e.occurred_at, e.id
     limit greatest(1, least(coalesce(p_limit, 50), 200))
     for update skip locked
  )
  update public.assistant_events e
     set status = 'claimed', claimed_by = p_worker, claimed_at = p_now, attempts = e.attempts + 1
    from picked
   where e.id = picked.id
  returning e.*;
end $$;

-- A11.2 Event finish, fenced on the claim. retry re-queues with a backoff, or
--       dead-letters at five attempts.
create or replace function public.assistant_event_finish(
  p_event_id uuid, p_worker text, p_status text, p_routed jsonb, p_error text, p_retry_seconds int,
  p_now timestamptz default now()
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
  if p_status is null or p_status not in ('processed', 'skipped', 'failed', 'retry') then
    raise exception 'assistant_event_finish: invalid status' using errcode = 'check_violation';
  end if;
  if p_status = 'retry' then
    update public.assistant_events e
       set status = case when e.attempts >= 5 then 'failed' else 'pending' end,
           claimed_by = case when e.attempts >= 5 then e.claimed_by end,
           next_attempt_at = case when e.attempts >= 5 then null
                                  else p_now + make_interval(secs => greatest(0, coalesce(p_retry_seconds, 60))) end,
           processed_at = case when e.attempts >= 5 then p_now end,
           last_error = left(p_error, 500),
           routed = coalesce(p_routed, e.routed)
     where e.id = p_event_id and e.claimed_by = p_worker and e.status = 'claimed';
  else
    update public.assistant_events e
       set status = p_status, processed_at = p_now, next_attempt_at = null,
           last_error = coalesce(left(p_error, 500), e.last_error),
           routed = coalesce(p_routed, e.routed)
     where e.id = p_event_id and e.claimed_by = p_worker and e.status = 'claimed';
  end if;
  get diagnostics v_n = row_count;
  return v_n = 1;
end $$;

-- A11.3 Immediate delivery: the row, its one bell and (p_teams) its one Teams ledger
--       row in one transaction, under the per-user lock A11.4 and A11.14 also take.
--       Selects by row state, so any worker may deliver any due row exactly once.
--       No cap (owner question 16). Sends nothing itself.
create or replace function public.assistant_inbox_deliver(
  p_user_id uuid, p_inbox_id uuid, p_bell_message text, p_bell_metadata jsonb, p_teams boolean,
  p_teams_not_before timestamptz, p_now timestamptz default now()
)
returns jsonb
language plpgsql
volatile
security invoker
set search_path = pg_catalog, public
as $$
declare
  v_id uuid;
  v_bell uuid;
  v_send uuid;
begin
  perform pg_advisory_xact_lock(hashtext('assistant_inbox_deliver:' || p_user_id::text));
  select i.id into v_id from public.assistant_inbox i
   where i.id = p_inbox_id and i.user_id = p_user_id and i.delivery = 'immediate'
     and i.delivered_at is null and i.dismissed_at is null and i.deliver_after <= p_now
   for update;
  if v_id is null then
    return jsonb_build_object('delivered', false);
  end if;
  if p_bell_message is not null then
    insert into public.notifications (user_id, project_id, type, message, metadata, created_at)
    values (p_user_id, null, 'assistant.inbox', left(p_bell_message, 300),
            coalesce(p_bell_metadata, '{}'::jsonb)
              || jsonb_build_object('inbox_id', p_inbox_id, 'actor_kind', 'system'),
            p_now)
    returning id into v_bell;
  end if;
  update public.assistant_inbox i set delivered_at = p_now, notification_id = v_bell where i.id = v_id;
  if coalesce(p_teams, false) then
    insert into public.assistant_external_sends
      (user_id, target_kind, target_id, channel, status, not_before, created_at, updated_at)
    values (p_user_id, 'inbox', p_inbox_id, 'teams', 'pending',
            greatest(p_now, coalesce(p_teams_not_before, p_now)), p_now, p_now)
    on conflict (target_kind, target_id, channel) do nothing
    returning id into v_send;
  end if;
  return jsonb_build_object('delivered', true, 'notification_id', v_bell, 'teams_send_id', v_send);
end $$;

-- A11.4 Digest delivery for one user and slot: ledger claim, attach every due row,
--       one bell, and (p_email) one email ledger row. An empty slot writes nothing.
create or replace function public.assistant_digest_deliver(
  p_user_id uuid, p_date date, p_slot text, p_slot_at timestamptz, p_worker text, p_email boolean,
  p_now timestamptz default now()
)
returns jsonb
language plpgsql
volatile
security invoker
set search_path = pg_catalog, public
as $$
declare
  v_since timestamptz;
  v_id uuid;
  v_n int := 0;
  v_r int := 0;
  v_bell uuid;
  v_send uuid;
begin
  if p_slot is null or p_slot not in ('am', 'pm') or p_slot_at is null or p_date is null then
    raise exception 'assistant_digest_deliver: bad arguments' using errcode = 'check_violation';
  end if;
  perform pg_advisory_xact_lock(hashtext('assistant_inbox_deliver:' || p_user_id::text));
  if not exists (select 1 from public.assistant_inbox i
                  where i.user_id = p_user_id and i.delivery = 'digest' and i.delivered_at is null
                    and i.dismissed_at is null and i.deliver_after <= p_slot_at) then
    return jsonb_build_object('claimed', false);
  end if;
  -- The later of the last delivered digest (one that rang its bell) and three days ago.
  select max(d.created_at) into v_since from public.assistant_digests d
   where d.user_id = p_user_id and d.notification_id is not null;
  v_since := greatest(coalesce(v_since, p_now - interval '3 days'), p_now - interval '3 days');
  insert into public.assistant_digests (user_id, digest_date, slot, slot_at, since_at, claimed_by, created_at)
  values (p_user_id, p_date, p_slot, p_slot_at, v_since, p_worker, p_now)
  on conflict (user_id, digest_date, slot) do nothing
  returning id into v_id;
  if v_id is null then
    return jsonb_build_object('claimed', false);
  end if;
  update public.assistant_inbox i set digest_id = v_id, delivered_at = p_now
   where i.user_id = p_user_id and i.delivery = 'digest' and i.delivered_at is null
     and i.dismissed_at is null and i.deliver_after <= p_slot_at;
  get diagnostics v_n = row_count;
  select count(*) into v_r
    from public.assistant_inbox i
    join public.notifications n on n.id = i.source_notification_id and n.user_id = i.user_id
   where i.user_id = p_user_id and i.delivery = 'record' and i.dismissed_at is null
     and i.created_at >= v_since and i.created_at < p_now
     and n.dismissed_at is null and n.read_at is null;
  if v_n > 0 then
    insert into public.notifications (user_id, project_id, type, message, metadata, created_at)
    values (p_user_id, null, 'assistant.digest',
            format('Your %s digest: %s %s.', case p_slot when 'am' then '7:00 AM' else '1:00 PM' end,
                   v_n, case when v_n = 1 then 'item' else 'items' end),
            jsonb_build_object('digest_id', v_id, 'actor_kind', 'system'), p_now)
    returning id into v_bell;
    if coalesce(p_email, false) then
      insert into public.assistant_external_sends
        (user_id, target_kind, target_id, channel, status, not_before, created_at, updated_at)
      values (p_user_id, 'digest', v_id, 'email', 'pending', p_now, p_now, p_now)
      on conflict (target_kind, target_id, channel) do nothing
      returning id into v_send;
    end if;
  end if;
  update public.assistant_digests d
     set item_count = v_n, record_count = v_r, notification_id = v_bell
   where d.id = v_id;
  return jsonb_build_object('claimed', true, 'digest_id', v_id, 'item_count', v_n, 'record_count', v_r,
                            'notification_id', v_bell, 'email_send_id', v_send);
end $$;

-- A11.5 Mark read: router-owned rows only, and their own bells. A record row's
--       source bell (BDR's) is never written here (owner question 31).
create or replace function public.assistant_inbox_mark_read(
  p_user_id uuid, p_ids uuid[], p_now timestamptz default now()
)
returns integer
language plpgsql
volatile
security invoker
set search_path = pg_catalog, public
as $$
declare
  v_n int;
begin
  with upd as (
    update public.assistant_inbox i set read_at = p_now
     where i.user_id = p_user_id and i.delivery in ('immediate', 'digest')
       and i.delivered_at is not null and i.read_at is null
       and (p_ids is null or i.id = any(p_ids))
    returning i.notification_id
  ), bells as (
    update public.notifications n set read_at = p_now
      from upd
     where n.id = upd.notification_id and n.user_id = p_user_id and n.read_at is null
    returning n.id
  )
  select count(*) into v_n from upd;
  return v_n;
end $$;

-- A11.6 Feedback. Part A refuses mute_project (invalid_feedback); part B replaces this
--       function once the feedback check is widened. mute writes the user override
--       through the partial unique index; the third not_useful mark on one kind in
--       14 days moves the kind to inbox only (feedback_auto), at most once.
--       Guarded: a re-run of part A (or of piece 2) after part B keeps part B's
--       version, so Mute this project never silently stops working.
do $guard$
declare
  v_fn regprocedure := to_regprocedure('public.assistant_inbox_feedback(uuid, uuid, text, timestamptz)');
begin
  if v_fn is not null and position('mute_project' in pg_get_functiondef(v_fn)) > 0 then
    return;
  end if;
  execute $fn$
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
  if p_value is null or p_value not in ('useful', 'not_useful', 'mute') then
    return jsonb_build_object('ok', false, 'code', 'invalid_feedback');
  end if;
  select * into v_row from public.assistant_inbox i
   where i.id = p_inbox_id and i.user_id = p_user_id for update;
  if not found then
    return jsonb_build_object('ok', false, 'code', 'not_found');
  end if;
  if p_value = 'mute' and (v_row.delivery = 'direct' or v_row.event_type is null
       or (v_row.event_type like 'assistant.%' and v_row.event_type <> 'assistant.approval_expiring')) then
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
$fn$;
end $guard$;

-- A11.7 Remind me next digest: a delivered router-owned row returns, unread, at
--       p_next_slot (from Python, 8.2), at most three times; its own bell clears and
--       the row lets go of it (notification_id = null), because a router row counts
--       as read when its own bell is read (10.1) and must come back unread. The Teams
--       ledger keys on the inbox id, so nothing else needs the old bell.
--       precedence becomes 'snoozed' so A11.14 never mistakes it for a folded row.
create or replace function public.assistant_inbox_snooze(
  p_user_id uuid, p_inbox_id uuid, p_next_slot timestamptz, p_now timestamptz default now()
)
returns jsonb
language plpgsql
volatile
security invoker
set search_path = pg_catalog, public
as $$
declare
  v_row public.assistant_inbox;
begin
  if p_next_slot is null or p_next_slot <= p_now then
    raise exception 'assistant_inbox_snooze: bad slot' using errcode = 'check_violation';
  end if;
  select * into v_row from public.assistant_inbox i
   where i.id = p_inbox_id and i.user_id = p_user_id for update;
  if not found then
    return jsonb_build_object('ok', false, 'code', 'not_found');
  end if;
  if v_row.delivery not in ('immediate', 'digest') or v_row.delivered_at is null or v_row.dismissed_at is not null then
    return jsonb_build_object('ok', false, 'code', 'not_mutable');
  end if;
  if v_row.snooze_count >= 3 then
    return jsonb_build_object('ok', false, 'code', 'snooze_limit');
  end if;
  update public.assistant_inbox i
     set delivery = 'digest', delivered_at = null, read_at = null, digest_id = null, notification_id = null,
         deliver_after = p_next_slot, precedence = 'snoozed', snooze_count = i.snooze_count + 1
   where i.id = v_row.id;
  if v_row.notification_id is not null then
    update public.notifications n set read_at = p_now
     where n.id = v_row.notification_id and n.user_id = p_user_id and n.read_at is null;
  end if;
  return jsonb_build_object('ok', true, 'inbox_id', v_row.id, 'next_slot', p_next_slot,
                            'snooze_count', v_row.snooze_count + 1);
end $$;

-- A11.1 to A11.7: execute for service_role only, inside piece 2 (0145 pattern).
revoke all on function public.assistant_events_claim(text, int, int, timestamptz, uuid[]) from public;
revoke all on function public.assistant_events_claim(text, int, int, timestamptz, uuid[]) from anon, authenticated;
grant execute on function public.assistant_events_claim(text, int, int, timestamptz, uuid[]) to service_role;
revoke all on function public.assistant_event_finish(uuid, text, text, jsonb, text, int, timestamptz) from public;
revoke all on function public.assistant_event_finish(uuid, text, text, jsonb, text, int, timestamptz) from anon, authenticated;
grant execute on function public.assistant_event_finish(uuid, text, text, jsonb, text, int, timestamptz) to service_role;
revoke all on function public.assistant_inbox_deliver(uuid, uuid, text, jsonb, boolean, timestamptz, timestamptz) from public;
revoke all on function public.assistant_inbox_deliver(uuid, uuid, text, jsonb, boolean, timestamptz, timestamptz) from anon, authenticated;
grant execute on function public.assistant_inbox_deliver(uuid, uuid, text, jsonb, boolean, timestamptz, timestamptz) to service_role;
revoke all on function public.assistant_digest_deliver(uuid, date, text, timestamptz, text, boolean, timestamptz) from public;
revoke all on function public.assistant_digest_deliver(uuid, date, text, timestamptz, text, boolean, timestamptz) from anon, authenticated;
grant execute on function public.assistant_digest_deliver(uuid, date, text, timestamptz, text, boolean, timestamptz) to service_role;
revoke all on function public.assistant_inbox_mark_read(uuid, uuid[], timestamptz) from public;
revoke all on function public.assistant_inbox_mark_read(uuid, uuid[], timestamptz) from anon, authenticated;
grant execute on function public.assistant_inbox_mark_read(uuid, uuid[], timestamptz) to service_role;
revoke all on function public.assistant_inbox_feedback(uuid, uuid, text, timestamptz) from public;
revoke all on function public.assistant_inbox_feedback(uuid, uuid, text, timestamptz) from anon, authenticated;
grant execute on function public.assistant_inbox_feedback(uuid, uuid, text, timestamptz) to service_role;
revoke all on function public.assistant_inbox_snooze(uuid, uuid, timestamptz, timestamptz) from public;
revoke all on function public.assistant_inbox_snooze(uuid, uuid, timestamptz, timestamptz) from anon, authenticated;
grant execute on function public.assistant_inbox_snooze(uuid, uuid, timestamptz, timestamptz) to service_role;

-- ---- PART A split point 3 (optional) ----

-- A11.8 Subscription upsert: exactly one of role and user. Plain SQL on conflict
--       targets the partial unique indexes, which PostgREST cannot. urgent is
--       accepted on role rows only and only for the three router-owned kinds,
--       assistant.approval_expiring included (P5, P39, 6.3); record kinds never push.
create or replace function public.assistant_subscription_set(
  p_role text, p_user_id uuid, p_event_type text, p_enabled boolean, p_severity text, p_source text,
  p_actor uuid
)
returns jsonb
language plpgsql
volatile
security invoker
set search_path = pg_catalog, public
as $$
declare
  v_row public.assistant_subscriptions;
begin
  if (p_role is null) = (p_user_id is null) then
    return jsonb_build_object('ok', false, 'code', 'bad_target');
  end if;
  if p_source is null or p_source not in ('admin', 'user', 'feedback_auto')
     or (p_role is not null and p_source <> 'admin') then
    return jsonb_build_object('ok', false, 'code', 'bad_source');
  end if;
  if p_role is not null and p_role not in ('executive', 'estimating_admin', 'it_admin', 'accountant',
       'estimating_engineer_materials', 'estimating_engineer_labor') then
    return jsonb_build_object('ok', false, 'code', 'bad_role');
  end if;
  if p_event_type is null or p_event_type !~ '^[a-z][a-z0-9_.]{2,63}$' then
    return jsonb_build_object('ok', false, 'code', 'unknown_kind');
  end if;
  if p_enabled is null or p_severity is null or p_severity not in ('low', 'normal', 'high', 'urgent')
     or (p_severity = 'urgent' and (p_role is null
         or p_event_type not in ('bid.due_not_ready', 'todo.due', 'assistant.approval_expiring'))) then
    return jsonb_build_object('ok', false, 'code', 'invalid_mode');
  end if;
  if p_role is not null then
    insert into public.assistant_subscriptions (role, user_id, event_type, enabled, severity, source, updated_by)
    values (p_role, null, p_event_type, p_enabled, p_severity, 'admin', p_actor)
    on conflict (role, event_type) where user_id is null
    do update set enabled = excluded.enabled, severity = excluded.severity, source = excluded.source,
                  updated_by = excluded.updated_by
    returning * into v_row;
  else
    insert into public.assistant_subscriptions (role, user_id, event_type, enabled, severity, source, updated_by)
    values (null, p_user_id, p_event_type, p_enabled, p_severity, p_source, p_actor)
    on conflict (user_id, event_type) where role is null
    do update set enabled = excluded.enabled, severity = excluded.severity, source = excluded.source,
                  updated_by = excluded.updated_by
    returning * into v_row;
  end if;
  return jsonb_build_object('ok', true, 'subscription', to_jsonb(v_row));
end $$;

-- A11.9 Dismiss (user, router or offboarding). Marks the router's own linked bell
--       read, never a record row's source bell (owner question 31).
create or replace function public.assistant_inbox_dismiss(
  p_user_id uuid, p_ids uuid[], p_reason text, p_now timestamptz default now()
)
returns integer
language plpgsql
volatile
security invoker
set search_path = pg_catalog, public
as $$
declare
  v_n int;
begin
  if p_reason is null or p_reason not in ('user', 'project_closed', 'owner_offboarded', 'superseded', 'muted') then
    raise exception 'assistant_inbox_dismiss: invalid reason' using errcode = 'check_violation';
  end if;
  if p_ids is null or cardinality(p_ids) = 0 then
    return 0;
  end if;
  with upd as (
    update public.assistant_inbox i set dismissed_at = p_now, dismissed_reason = p_reason
     where i.user_id = p_user_id and i.id = any(p_ids) and i.dismissed_at is null
    returning i.notification_id
  ), bells as (
    update public.notifications n set read_at = p_now
      from upd
     where n.id = upd.notification_id and n.user_id = p_user_id and n.read_at is null
    returning n.id
  )
  select count(*) into v_n from upd;
  return v_n;
end $$;

-- A11.10 Feedback report, counts only (owner question 22). Direct rows excluded; any
--        cell with fewer than greatest(3, p_floor) distinct users is suppressed; no
--        user id leaves the function. Complementary suppression: a kind total minus its
--        visible role cells must never isolate fewer than the floor (a user has one
--        role, so the hidden role cells sum to that remainder); when it would, the
--        kind's role breakdown is withheld (roles [] and roles_suppressed true).
--        The health block (9.3) rides along.
create or replace function public.assistant_inbox_feedback_report(
  p_since timestamptz, p_floor int default 3, p_now timestamptz default now()
)
returns jsonb
language plpgsql
stable
security invoker
set search_path = pg_catalog, public
as $$
declare
  v_floor int := greatest(3, coalesce(p_floor, 3));
  v_kinds jsonb;
  v_health jsonb;
begin
  with src as (
    select i.event_type, i.user_id, coalesce(p.role::text, 'unknown') as role,
           (i.delivered_at is not null) as delivered,
           case when i.delivery = 'record' then sb.read_at is not null
                else i.read_at is not null or nb.read_at is not null end as is_read,
           i.feedback, i.snooze_count
      from public.assistant_inbox i
      left join public.profiles p on p.id = i.user_id
      left join public.notifications nb on nb.id = i.notification_id
      left join public.notifications sb on sb.id = i.source_notification_id
     where i.delivery <> 'direct' and i.event_type is not null and i.created_at >= p_since
  ), subs as (
    select s.event_type,
           count(*) filter (where s.source = 'feedback_auto') as auto_downgraded,
           count(*) filter (where s.enabled = false) as muted_kind
      from public.assistant_subscriptions s
     where s.role is null
     group by s.event_type
  ), cells as (
    select r.event_type, r.role, count(distinct r.user_id) as users,
           count(*) filter (where r.delivered) as delivered,
           count(*) filter (where r.is_read) as read,
           count(*) filter (where r.feedback = 'useful') as useful,
           count(*) filter (where r.feedback = 'not_useful') as not_useful,
           count(*) filter (where r.feedback = 'mute') as muted,
           count(*) filter (where r.feedback = 'mute_project') as project_muted,
           count(*) filter (where r.snooze_count > 0) as snoozed
      from src r group by grouping sets ((r.event_type), (r.event_type, r.role))
  )
  select coalesce(jsonb_agg(k.obj order by k.event_type), '[]'::jsonb) into v_kinds
    from (
      select t.event_type,
             case when t.users < v_floor then
               jsonb_build_object('event_type', t.event_type, 'suppressed', true, 'users', null)
             else
               jsonb_build_object('event_type', t.event_type, 'suppressed', false, 'users', t.users,
                 'delivered', t.delivered, 'read', t.read, 'useful', t.useful, 'not_useful', t.not_useful,
                 'muted', t.muted, 'project_muted', t.project_muted, 'snoozed', t.snoozed,
                 'auto_downgraded', coalesce(s.auto_downgraded, 0), 'muted_kind', coalesce(s.muted_kind, 0),
                 'roles_suppressed', t.hidden between 1 and v_floor - 1,
                 'roles', case when t.hidden between 1 and v_floor - 1 then '[]'::jsonb else coalesce((
                   select jsonb_agg(case when c.users < v_floor then
                            jsonb_build_object('role', c.role, 'suppressed', true, 'users', null)
                          else
                            jsonb_build_object('role', c.role, 'suppressed', false, 'users', c.users,
                              'delivered', c.delivered, 'read', c.read, 'useful', c.useful,
                              'not_useful', c.not_useful, 'muted', c.muted, 'project_muted', c.project_muted,
                              'snoozed', c.snoozed)
                          end order by c.role)
                     from cells c where c.event_type = t.event_type and c.role is not null), '[]'::jsonb) end)
             end as obj
        from (select t0.*,
                     (select coalesce(sum(c.users), 0) from cells c
                       where c.event_type = t0.event_type and c.role is not null and c.users < v_floor) as hidden
                from cells t0 where t0.role is null) t
        left join subs s on s.event_type = t.event_type
    ) k;

  select jsonb_build_object(
    'events_pending', (select count(*) from public.assistant_events e where e.processed_at is null),
    'events_failed_7d', (select count(*) from public.assistant_events e
                          where e.status = 'failed' and e.processed_at >= p_now - interval '7 days'),
    'events_lease_exhausted_7d', (select count(*) from public.assistant_events e
                                   where e.status = 'failed' and e.last_error = 'lease_exhausted'
                                     and e.processed_at >= p_now - interval '7 days'),
    'slot_failures_7d', (select coalesce(sum(coalesce((sl.result->>'failures')::int, 0)), 0)
                           from public.assistant_proactive_slots sl
                          where sl.claimed_at >= p_now - interval '7 days'),
    'workers', (select coalesce(jsonb_agg(jsonb_build_object('worker', w.claimed_by, 'last_claim_at', w.at)
                                          order by w.at desc), '[]'::jsonb)
                  from (select sl.claimed_by, max(sl.claimed_at) as at from public.assistant_proactive_slots sl
                         group by sl.claimed_by order by 2 desc limit 10) w),
    'cursors', (select coalesce(jsonb_agg(jsonb_build_object('name', c.name, 'cursor_at', c.cursor_at,
                                                             'updated_by', c.updated_by, 'updated_at', c.updated_at)
                                          order by c.name), '[]'::jsonb)
                  from public.assistant_router_cursors c),
    'sends_7d', (select coalesce(jsonb_agg(jsonb_build_object('channel', x.channel, 'status', x.status,
                                                              'error_kind', x.error_kind, 'count', x.n)
                                           order by x.channel, x.status, x.error_kind), '[]'::jsonb)
                   from (select s.channel, s.status, s.error_kind, count(*) as n
                           from public.assistant_external_sends s
                          where s.created_at >= p_now - interval '7 days' or s.status in ('pending', 'claimed')
                          group by s.channel, s.status, s.error_kind) x)
  ) into v_health;

  return jsonb_build_object('since', p_since, 'floor', v_floor, 'kinds', v_kinds, 'health', v_health);
end $$;

-- A11.11 Email-ID collapse for record rows (decision E, 4.2a). One row per inbox key;
--        a new bell N replaces the source S when its rank is higher, or ranks tie and
--        N is more than 120 s after S (a later ingest: the row's created_at moves to
--        N), or ranks tie within 120 s and (bell_at, id) of N is smaller (same ingest:
--        the earliest wins). Otherwise N is linked. Record rows only.
--        Known limitation: the final row is order independent for two bells only.
--        With three or more equal-rank bells spanning more than 120 s the 120 s test
--        chains (it is measured from the current source, not a fixed anchor), so the
--        final source can depend on arrival order (A 0 s, B 100 s, C 200 s: order
--        A B C ends on C, order C B A ends on A). Any fixed anchor still chains;
--        the plan's two-bell golden cases hold.
create or replace function public.assistant_inbox_link_bell(
  p_user_id uuid, p_dedupe_key text, p_notification_id uuid, p_bell_at timestamptz, p_event_type text,
  p_rank int, p_title text, p_why text, p_payload jsonb, p_project_id uuid, p_now timestamptz default now()
)
returns jsonb
language plpgsql
volatile
security invoker
set search_path = pg_catalog, public
as $$
declare
  v_id uuid;
  v_row public.assistant_inbox;
  v_rank int;
  v_at timestamptz;
  v_linked jsonb;
  v_new_source boolean;
  v_later boolean;
begin
  if p_dedupe_key is null or p_dedupe_key not like 'p7:bell:%' or p_notification_id is null
     or p_bell_at is null or p_rank is null or p_title is null then
    raise exception 'assistant_inbox_link_bell: bad arguments' using errcode = 'check_violation';
  end if;
  insert into public.assistant_inbox
    (user_id, dedupe_key, event_type, title, why, payload, project_id, delivery,
     source_notification_id, delivered_at, created_at)
  values (p_user_id, p_dedupe_key, p_event_type, p_title, p_why,
          coalesce(p_payload, '{}'::jsonb)
            || jsonb_build_object('rank', p_rank, 'source_bell_at', p_bell_at,
                                  'linked_notification_ids', '[]'::jsonb),
          p_project_id, 'record', p_notification_id, p_now, p_now)
  on conflict (user_id, dedupe_key) do nothing
  returning id into v_id;
  if v_id is not null then
    return jsonb_build_object('inbox_id', v_id, 'linked', false);
  end if;

  select * into v_row from public.assistant_inbox i
   where i.user_id = p_user_id and i.dedupe_key = p_dedupe_key for update;
  if v_row.id is null or v_row.delivery <> 'record' then
    return jsonb_build_object('inbox_id', v_row.id, 'linked', false, 'code', 'not_record');
  end if;
  v_linked := coalesce(v_row.payload->'linked_notification_ids', '[]'::jsonb);
  if v_row.source_notification_id = p_notification_id or v_linked ? p_notification_id::text then
    return jsonb_build_object('inbox_id', v_row.id, 'linked', true, 'noop', true);
  end if;
  v_rank := coalesce((v_row.payload->>'rank')::int, 0);
  v_at := coalesce((v_row.payload->>'source_bell_at')::timestamptz, v_row.created_at);
  v_later := p_rank = v_rank and p_bell_at > v_at + interval '120 seconds';
  v_new_source := p_rank > v_rank
    or v_later
    or (p_rank = v_rank and p_bell_at >= v_at - interval '120 seconds'
        and (p_bell_at, p_notification_id) < (v_at, v_row.source_notification_id));

  if v_new_source then
    -- S joins the linked list: distinct, arrival order, at most 20 entries.
    if v_row.source_notification_id is not null then
      v_linked := v_linked || jsonb_build_array(v_row.source_notification_id::text);
    end if;
    v_linked := (select coalesce(jsonb_agg(d.x order by d.o), '[]'::jsonb)
                   from (select t.x, min(t.o) as o
                           from jsonb_array_elements_text(v_linked) with ordinality t(x, o)
                          where t.x <> p_notification_id::text
                          group by t.x order by min(t.o) limit 20) d);
    update public.assistant_inbox i
       set event_type = p_event_type, title = p_title, why = p_why,
           source_notification_id = p_notification_id,
           project_id = coalesce(p_project_id, i.project_id),
           payload = coalesce(p_payload, '{}'::jsonb)
             || jsonb_build_object('rank', p_rank, 'source_bell_at', p_bell_at, 'linked_notification_ids', v_linked),
           created_at = case when v_later then p_bell_at else i.created_at end,
           dismissed_at = case when v_later and i.dismissed_reason = 'user' then null else i.dismissed_at end,
           dismissed_reason = case when v_later and i.dismissed_reason = 'user' then null else i.dismissed_reason end
     where i.id = v_row.id;
  elsif jsonb_array_length(v_linked) < 20 then
    update public.assistant_inbox i
       set payload = i.payload || jsonb_build_object('linked_notification_ids',
                                                     v_linked || jsonb_build_array(p_notification_id::text))
     where i.id = v_row.id;
  end if;
  return jsonb_build_object('inbox_id', v_row.id, 'linked', true, 'source', v_new_source);
end $$;

-- A11.12 Send claim, one call per row in practice (8.5 step 3). A stale claim is
--        retired as uncertain lease_expired and never handed out again: neither
--        helper is idempotent, so a possible second send is worse than a missed one.
create or replace function public.assistant_external_sends_claim(
  p_worker text, p_limit int, p_lease_seconds int, p_now timestamptz default now()
)
returns setof public.assistant_external_sends
language plpgsql
volatile
security invoker
set search_path = pg_catalog, public
as $$
declare
  v_lease interval := make_interval(secs => greatest(60, coalesce(p_lease_seconds, 600)));
begin
  if p_worker is null or btrim(p_worker) = '' or p_now is null then
    raise exception 'assistant_external_sends_claim: bad arguments' using errcode = 'check_violation';
  end if;
  update public.assistant_external_sends s
     set status = 'uncertain', error_kind = 'lease_expired', claimed_by = null, updated_at = p_now
   where s.status = 'claimed' and s.claimed_at < p_now - v_lease;
  return query
  with picked as (
    select s.id from public.assistant_external_sends s
     where s.status = 'pending' and s.not_before <= p_now
       and (s.next_attempt_at is null or s.next_attempt_at <= p_now)
       and s.attempts < 5
     order by s.not_before, s.id
     limit greatest(1, least(coalesce(p_limit, 1), 50))
     for update skip locked
  )
  update public.assistant_external_sends s
     set status = 'claimed', claimed_by = p_worker, claimed_at = p_now, attempts = s.attempts + 1,
         updated_at = p_now
    from picked
   where s.id = picked.id
  returning s.*;
end $$;

-- A11.13 Send settle, fenced on the claim. deferred (quiet hours) gives the attempt
--        back; transient retries with a backoff and dead-letters at five attempts;
--        uncertain is terminal. An unknown outcome, or an error kind outside the
--        A9a list on any outcome, raises invalid_outcome.
create or replace function public.assistant_external_send_settle(
  p_id uuid, p_worker text, p_outcome text, p_error_kind text, p_email_log_id uuid, p_retry_seconds int,
  p_now timestamptz default now()
)
returns boolean
language plpgsql
volatile
security invoker
set search_path = pg_catalog, public
as $$
declare
  v_n int;
  v_secs interval := make_interval(secs => greatest(0, coalesce(p_retry_seconds, 60)));
begin
  if p_outcome is null or p_outcome not in ('sent', 'transient', 'permanent', 'uncertain', 'skipped', 'deferred')
     or (p_error_kind is not null and p_error_kind not in ('transient', 'permanent', 'uncertain', 'lease_expired',
         'channel_off', 'opted_out', 'stale', 'offboarded', 'quiet_hours'))
     or (p_outcome = 'skipped' and (p_error_kind is null
         or p_error_kind not in ('channel_off', 'opted_out', 'stale', 'offboarded'))) then
    raise exception 'invalid_outcome' using errcode = 'check_violation';
  end if;
  update public.assistant_external_sends s
     set status = case p_outcome
                    when 'sent' then 'sent'
                    when 'transient' then case when s.attempts >= 5 then 'failed' else 'pending' end
                    when 'permanent' then 'failed'
                    when 'uncertain' then 'uncertain'
                    when 'skipped' then 'skipped'
                    else 'pending' end,
         sent_at = case when p_outcome = 'sent' then p_now end,
         email_log_id = case when p_outcome = 'sent' and s.channel = 'email' then p_email_log_id
                             else s.email_log_id end,
         error_kind = case p_outcome
                        when 'sent' then null
                        when 'transient' then 'transient'
                        when 'permanent' then 'permanent'
                        when 'uncertain' then 'uncertain'
                        when 'skipped' then p_error_kind
                        else 'quiet_hours' end,
         claimed_by = case when p_outcome = 'deferred' or (p_outcome = 'transient' and s.attempts < 5) then null
                           else s.claimed_by end,
         next_attempt_at = case when p_outcome = 'transient' and s.attempts < 5 then p_now + v_secs end,
         not_before = case when p_outcome = 'deferred' then p_now + v_secs else s.not_before end,
         attempts = case when p_outcome = 'deferred' then greatest(0, s.attempts - 1) else s.attempts end,
         updated_at = p_now
   where s.id = p_id and s.claimed_by = p_worker and s.status = 'claimed';
  get diagnostics v_n = row_count;
  return v_n = 1;
end $$;

-- A11.14 Quiet-hours fold and un-fold (8.4), under the per-user lock of A11.3 and
--        A11.4. A held row (high, not approval-expiring, deliver_after > created_at)
--        folds into the AM digest while no AM ledger row exists and p_now is before
--        p_fold_until; a folded row returns to immediate once the AM digest went out
--        without it or the window closed. Returns folded, deliver, wait or none.
create or replace function public.assistant_inbox_fold(
  p_user_id uuid, p_inbox_id uuid, p_am_date date, p_am_slot_at timestamptz, p_fold_until timestamptz,
  p_now timestamptz default now()
)
returns text
language plpgsql
volatile
security invoker
set search_path = pg_catalog, public
as $$
declare
  v_row public.assistant_inbox;
  v_am boolean;
begin
  perform pg_advisory_xact_lock(hashtext('assistant_inbox_deliver:' || p_user_id::text));
  select * into v_row from public.assistant_inbox i
   where i.id = p_inbox_id and i.user_id = p_user_id for update;
  if not found then
    return 'none';
  end if;
  v_am := p_am_date is not null and exists (
    select 1 from public.assistant_digests d
     where d.user_id = p_user_id and d.digest_date = p_am_date and d.slot = 'am');
  if v_row.delivery = 'immediate' and v_row.delivered_at is null and v_row.dismissed_at is null
     and v_row.deliver_after <= p_now and v_row.severity = 'high'
     and coalesce(v_row.event_type, '') <> 'assistant.approval_expiring'
     and v_row.deliver_after > v_row.created_at then
    if p_am_date is not null and p_am_slot_at is not null and p_fold_until is not null
       and p_now < p_fold_until and not v_am then
      update public.assistant_inbox i
         set delivery = 'digest', precedence = 'quiet_hours', deliver_after = p_am_slot_at
       where i.id = v_row.id;
      return 'folded';
    end if;
    return 'deliver';
  end if;
  if v_row.delivery = 'digest' and v_row.precedence = 'quiet_hours'
     and v_row.delivered_at is null and v_row.dismissed_at is null then
    if v_am or p_fold_until is null or p_now >= p_fold_until then
      update public.assistant_inbox i set delivery = 'immediate', deliver_after = p_now where i.id = v_row.id;
      return 'deliver';
    end if;
    return 'wait';
  end if;
  return 'none';
end $$;

-- A12. Execute for service_role only (0145 pattern): A11.8 to A11.14 (A11.1 to A11.7
--      are granted at the end of piece 2 and the classify trigger in piece 1).
revoke all on function public.assistant_subscription_set(text, uuid, text, boolean, text, text, uuid) from public;
revoke all on function public.assistant_subscription_set(text, uuid, text, boolean, text, text, uuid) from anon, authenticated;
grant execute on function public.assistant_subscription_set(text, uuid, text, boolean, text, text, uuid) to service_role;
revoke all on function public.assistant_inbox_dismiss(uuid, uuid[], text, timestamptz) from public;
revoke all on function public.assistant_inbox_dismiss(uuid, uuid[], text, timestamptz) from anon, authenticated;
grant execute on function public.assistant_inbox_dismiss(uuid, uuid[], text, timestamptz) to service_role;
revoke all on function public.assistant_inbox_feedback_report(timestamptz, int, timestamptz) from public;
revoke all on function public.assistant_inbox_feedback_report(timestamptz, int, timestamptz) from anon, authenticated;
grant execute on function public.assistant_inbox_feedback_report(timestamptz, int, timestamptz) to service_role;
revoke all on function public.assistant_inbox_link_bell(uuid, text, uuid, timestamptz, text, int, text, text, jsonb, uuid, timestamptz) from public;
revoke all on function public.assistant_inbox_link_bell(uuid, text, uuid, timestamptz, text, int, text, text, jsonb, uuid, timestamptz) from anon, authenticated;
grant execute on function public.assistant_inbox_link_bell(uuid, text, uuid, timestamptz, text, int, text, text, jsonb, uuid, timestamptz) to service_role;
revoke all on function public.assistant_external_sends_claim(text, int, int, timestamptz) from public;
revoke all on function public.assistant_external_sends_claim(text, int, int, timestamptz) from anon, authenticated;
grant execute on function public.assistant_external_sends_claim(text, int, int, timestamptz) to service_role;
revoke all on function public.assistant_external_send_settle(uuid, text, text, text, uuid, int, timestamptz) from public;
revoke all on function public.assistant_external_send_settle(uuid, text, text, text, uuid, int, timestamptz) from anon, authenticated;
grant execute on function public.assistant_external_send_settle(uuid, text, text, text, uuid, int, timestamptz) to service_role;
revoke all on function public.assistant_inbox_fold(uuid, uuid, date, timestamptz, timestamptz, timestamptz) from public;
revoke all on function public.assistant_inbox_fold(uuid, uuid, date, timestamptz, timestamptz, timestamptz) from anon, authenticated;
grant execute on function public.assistant_inbox_fold(uuid, uuid, date, timestamptz, timestamptz, timestamptz) to service_role;

notify pgrst, 'reload schema';

-- ==== PART B ====
-- Destructive text lives only here (owner at the screen): the feedback check swap,
-- the reset and the purge.

-- B1. Feedback vocabulary gains mute_project (constraint swap inside a do block, 0180 style).
do $$
declare v_def text;
begin
  select pg_get_constraintdef(oid) into v_def from pg_constraint
   where conrelid = 'public.assistant_inbox'::regclass and conname = 'assistant_inbox_feedback_check';
  if v_def is not null and position('''mute_project''' in v_def) > 0 then return; end if;
  if v_def is not null then
    execute 'alter table public.assistant_inbox drop constraint assistant_inbox_feedback_check';
  end if;
  execute $sql$alter table public.assistant_inbox add constraint assistant_inbox_feedback_check
    check (feedback in ('useful', 'not_useful', 'mute', 'mute_project')) not valid$sql$;
  execute 'alter table public.assistant_inbox validate constraint assistant_inbox_feedback_check';
end $$;

-- B2. Feedback, now accepting mute_project (owner question 34, a): the project mute
--     row plus the user's pending rows for that project dismissed 'muted'.
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
       or (v_row.event_type like 'assistant.%' and v_row.event_type <> 'assistant.approval_expiring')) then
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

-- B3. Reset one override row (role or user) to the code default, like 0177 set_cell
--     writing NULL. The route writes the audit row first (10.1).
create or replace function public.assistant_subscription_reset(
  p_role text, p_user_id uuid, p_event_type text, p_actor uuid
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
  if (p_role is null) = (p_user_id is null) or p_event_type is null then
    raise exception 'assistant_subscription_reset: bad_target' using errcode = 'check_violation';
  end if;
  if p_role is not null then
    delete from public.assistant_subscriptions s
     where s.role = p_role and s.user_id is null and s.event_type = p_event_type;
  else
    delete from public.assistant_subscriptions s
     where s.user_id = p_user_id and s.role is null and s.event_type = p_event_type;
  end if;
  get diagnostics v_n = row_count;
  return v_n > 0;
end $$;

-- B4. Retention purge (owner question 15, decision H), bounded by p_limit per table
--     per call. Skips every legal-hold user in every table; never touches direct
--     inbox rows, pending or claimed sends, or the two cursor rows.
create or replace function public.assistant_proactive_purge(
  p_now timestamptz, p_inbox_days int, p_event_days int, p_digest_days int, p_send_days int, p_limit int
)
returns jsonb
language plpgsql
volatile
security invoker
set search_path = pg_catalog, public
as $$
declare
  v_limit int := greatest(1, least(coalesce(p_limit, 200), 5000));
  v_inbox int; v_events int; v_digests int; v_slots int; v_sends int; v_mutes int;
begin
  if p_now is null or coalesce(p_inbox_days, 0) < 1 or coalesce(p_event_days, 0) < 1
     or coalesce(p_digest_days, 0) < 1 or coalesce(p_send_days, 0) < 1 then
    raise exception 'assistant_proactive_purge: bad arguments' using errcode = 'check_violation';
  end if;
  -- Serialize with a concurrent legal-hold change. A table lock in share mode, not a
  -- row lock: assistant_profiles rows are created lazily, so a hold that inserts the
  -- person's first row must wait too. Blocks profile writes until this call commits.
  lock table public.assistant_profiles in share mode;

  with doomed as (
    select i.id from public.assistant_inbox i
     where i.delivery in ('immediate', 'digest', 'record')
       and i.created_at < p_now - make_interval(days => p_inbox_days)
       and not exists (select 1 from public.assistant_profiles ap where ap.user_id = i.user_id and ap.legal_hold)
     order by i.created_at limit v_limit for update skip locked
  )
  delete from public.assistant_inbox i using doomed where i.id = doomed.id;
  get diagnostics v_inbox = row_count;

  with doomed as (
    select e.id from public.assistant_events e
     where e.processed_at is not null and e.processed_at < p_now - make_interval(days => p_event_days)
       and not exists (select 1 from public.assistant_profiles ap
                        where ap.user_id = e.subject_user_id and ap.legal_hold)
       and not exists (select 1 from public.assistant_inbox i
                         join public.assistant_profiles ap on ap.user_id = i.user_id and ap.legal_hold
                        where i.event_id = e.id)
     order by e.processed_at limit v_limit for update skip locked
  )
  delete from public.assistant_events e using doomed where e.id = doomed.id;
  get diagnostics v_events = row_count;

  with doomed as (
    select d.id from public.assistant_digests d
     where d.created_at < p_now - make_interval(days => p_digest_days)
       and not exists (select 1 from public.assistant_profiles ap where ap.user_id = d.user_id and ap.legal_hold)
     order by d.created_at limit v_limit for update skip locked
  )
  delete from public.assistant_digests d using doomed where d.id = doomed.id;
  get diagnostics v_digests = row_count;

  with doomed as (
    select sl.step, sl.slot_date from public.assistant_proactive_slots sl
     where sl.claimed_at < p_now - make_interval(days => p_digest_days)
     order by sl.claimed_at limit v_limit for update skip locked
  )
  delete from public.assistant_proactive_slots sl using doomed
   where sl.step = doomed.step and sl.slot_date = doomed.slot_date;
  get diagnostics v_slots = row_count;

  with doomed as (
    select s.id from public.assistant_external_sends s
     where s.status in ('sent', 'failed', 'skipped', 'uncertain')
       and s.created_at < p_now - make_interval(days => p_send_days)
       and not exists (select 1 from public.assistant_profiles ap where ap.user_id = s.user_id and ap.legal_hold)
     order by s.created_at limit v_limit for update skip locked
  )
  delete from public.assistant_external_sends s using doomed where s.id = doomed.id;
  get diagnostics v_sends = row_count;

  with doomed as (
    select m.user_id, m.project_id from public.assistant_project_mutes m
      join public.projects p on p.id = m.project_id
     where (p.abandoned_at is not null or p.failed_to_bid_at is not null
            or p.current_stage::text in ('declined', 'bid_outcome', 'pm_only', 'cp_only'))
       and not exists (select 1 from public.assistant_profiles ap where ap.user_id = m.user_id and ap.legal_hold)
     limit v_limit for update of m skip locked
  )
  delete from public.assistant_project_mutes m using doomed
   where m.user_id = doomed.user_id and m.project_id = doomed.project_id;
  get diagnostics v_mutes = row_count;

  return jsonb_build_object('limit', v_limit, 'assistant_inbox', v_inbox, 'assistant_events', v_events,
                            'assistant_digests', v_digests, 'assistant_proactive_slots', v_slots,
                            'assistant_external_sends', v_sends, 'assistant_project_mutes', v_mutes);
end $$;

-- B5. Execute for service_role only (0145 pattern).
revoke all on function public.assistant_inbox_feedback(uuid, uuid, text, timestamptz) from public;
revoke all on function public.assistant_inbox_feedback(uuid, uuid, text, timestamptz) from anon, authenticated;
grant execute on function public.assistant_inbox_feedback(uuid, uuid, text, timestamptz) to service_role;
revoke all on function public.assistant_subscription_reset(text, uuid, text, uuid) from public;
revoke all on function public.assistant_subscription_reset(text, uuid, text, uuid) from anon, authenticated;
grant execute on function public.assistant_subscription_reset(text, uuid, text, uuid) to service_role;
revoke all on function public.assistant_proactive_purge(timestamptz, int, int, int, int, int) from public;
revoke all on function public.assistant_proactive_purge(timestamptz, int, int, int, int, int) from anon, authenticated;
grant execute on function public.assistant_proactive_purge(timestamptz, int, int, int, int, int) to service_role;

notify pgrst, 'reload schema';
