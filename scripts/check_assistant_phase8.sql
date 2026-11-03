-- DEV ONLY after 0196 parts A and B (BDR project, bpidntbyvoooqvaispup). Never on BDR_Prod
-- or BDR_Staging. Synthetic fixtures, all rolled back (docs/ASSISTANT_PHASE8_PLAN.md 4.4, 16.2).
-- Run as service_role through execute_sql. The DO block always ends in
-- RAISE 'TEST_RESULT PASS|FAIL pass=N fail=M failures={...}', so nothing it wrote survives
-- even before the outer rollback; the helper functions and the results table live in
-- pg_temp and go with the transaction.
--
-- Concurrency (two sessions) is not tested here: that is scripts/check_triage_claims.py
-- (16.3). The single-session forms of those cases (a second claim for a person with a live
-- run, a 15-item race at batch 10) are below.
begin;

create temp table p8_results (n serial, label text not null, ok boolean not null);

create function pg_temp.p8_check(p_label text, p_ok boolean) returns void language sql as $$
  insert into p8_results (label, ok) values (p_label, coalesce(p_ok, false)) $$;

-- A record row (or another delivery) for a fixture person, with a project and a source bell.
create function pg_temp.p8_inbox(p_user uuid, p_kind text, p_at timestamptz, p_delivery text default 'record')
returns uuid language sql as $$
  insert into public.assistant_inbox
    (user_id, dedupe_key, title, delivery, event_type, source_notification_id, project_id,
     created_at, delivered_at, deliver_after)
  values (p_user, 'p7:bell:p8chk:' || gen_random_uuid()::text, 'Phase 8 fixture', p_delivery, p_kind,
          case when p_delivery = 'record' then gen_random_uuid() end, gen_random_uuid(),
          p_at, case when p_delivery = 'record' then p_at end,
          case when p_delivery in ('immediate', 'digest') then p_at end)
  returning id $$;

-- A claimed run with a live real-clock lease, claimed now (real clock).
create function pg_temp.p8_run(p_user uuid, p_worker text) returns uuid language sql as $$
  insert into public.assistant_triage_runs (user_id, status, claimed_by, claimed_at, lease_until, item_count, created_at)
  values (p_user, 'claimed', p_worker, clock_timestamp(), clock_timestamp() + interval '10 minutes', 1, clock_timestamp())
  returning id $$;

-- An item for an inbox row: claimed by p_run when given, else pending.
create function pg_temp.p8_item(p_inbox uuid, p_run uuid, p_attempts int default 0) returns uuid language sql as $$
  insert into public.assistant_triage_items
    (user_id, inbox_id, event_type, project_id, source_notification_id, status, run_id, attempts, created_at, updated_at)
  select i.user_id, i.id, i.event_type, i.project_id, i.source_notification_id,
         case when p_run is null then 'pending' else 'claimed' end, p_run, p_attempts,
         clock_timestamp(), clock_timestamp()
    from public.assistant_inbox i where i.id = p_inbox
  returning id $$;

-- A fresh switch-on: every switch on, a new real-clock epoch, the latch clear.
create function pg_temp.p8_epoch() returns timestamptz language sql as $$
  update public.assistant_settings
     set enabled = true, proactive_enabled = true, triage_enabled = true,
         triage_enabled_at = clock_timestamp(), triage_fuse_tripped_at = null
   where id = 1
  returning triage_enabled_at $$;

do $$
declare
  u1 uuid := gen_random_uuid();     -- materials engineer
  u2 uuid := gen_random_uuid();     -- it admin (legal hold in the purge case)
  u3 uuid := gen_random_uuid();     -- executive
  u4 uuid := gen_random_uuid();     -- executive, batch and race cases
  co uuid := public.assistant_company_spend_id();
  w text := 'p8chk-worker-a';
  w2 text := 'p8chk-worker-b';
  cut text := 'trunc' || 'ated';
  la text := 'America/Los_Angeles';
  v_epoch timestamptz;
  v_now timestamptz;
  v_x timestamptz;
  v_mid timestamptz;
  v_mid2 timestamptz;
  v_off2 interval;
  v_off3 interval;
  v_cap_now timestamptz;
  r_old uuid; r1 uuid; r2 uuid; r3 uuid; r_ftb uuid; r_dis uuid; r_u2 uuid; r_u3 uuid;
  a uuid; b uuid; c uuid; d uuid; i1 uuid; i2 uuid; i3 uuid; i4 uuid; i5 uuid; i6 uuid; x1 uuid;
  run1 uuid; run2 uuid; run3 uuid;
  v_n int;
  v_m int;
  v_ok boolean;
  v_s text;
  j jsonb;
  j2 jsonb;
  st0 jsonb;
  st1 jsonb;
  cs0 jsonb;
  cs1 jsonb;
  v_cap numeric;
  v_base numeric;
  v_head numeric;
  it public.assistant_triage_items;
  ev public.assistant_events;
  rn public.assistant_triage_runs;
  v_pass int;
  v_fail int;
  v_fails text[];
begin
  -- 0. Objects, grants, RLS.
  perform pg_temp.p8_check('grants', not exists (
    select 1 from pg_proc p join pg_namespace ns on ns.oid = p.pronamespace
     where ns.nspname = 'public'
       and p.proname in ('assistant_company_spend_id', 'assistant_triage_item_retry', 'assistant_triage_enqueue',
                         'assistant_triage_claim', 'assistant_triage_finish', 'assistant_triage_skip_user',
                         'assistant_triage_company_spend', 'assistant_triage_counts', 'assistant_triage_skip_all',
                         'assistant_triage_purge', 'assistant_spend_reserve', 'assistant_inbox_feedback',
                         'assistant_triage_set_enabled')
       and (has_function_privilege('anon', p.oid, 'EXECUTE')
            or has_function_privilege('authenticated', p.oid, 'EXECUTE')
            or not has_function_privilege('service_role', p.oid, 'EXECUTE')
            or p.prosecdef)));
  perform pg_temp.p8_check('function_count', (select count(*) from pg_proc p join pg_namespace ns on ns.oid = p.pronamespace
     where ns.nspname = 'public' and p.proname like 'assistant_triage%') = 10);
  perform pg_temp.p8_check('rls_forced', (select count(*) from pg_class pc join pg_namespace ns on ns.oid = pc.relnamespace
     where ns.nspname = 'public' and pc.relname in ('assistant_triage_runs', 'assistant_triage_items')
       and pc.relrowsecurity and pc.relforcerowsecurity) = 2);
  perform pg_temp.p8_check('no_client_table_grants', not exists (
    select 1 from information_schema.role_table_grants g
     where g.table_schema = 'public' and g.table_name in ('assistant_triage_runs', 'assistant_triage_items')
       and g.grantee in ('anon', 'authenticated')));
  perform pg_temp.p8_check('company_spend_takes_no_argument',
    (select p.pronargs from pg_proc p where p.oid = 'public.assistant_triage_company_spend()'::regprocedure) = 0);
  perform pg_temp.p8_check('company_id_value', co = '00000000-0000-0000-0000-000000000001'::uuid);
  perform pg_temp.p8_check('company_id_not_a_profile', not exists (select 1 from public.profiles where id = co));
  perform pg_temp.p8_check('kind_check_has_triage', exists (
    select 1 from pg_constraint where conname = 'assistant_spend_reservations_kind_chk'
       and position('''triage''' in pg_get_constraintdef(oid)) > 0));
  perform pg_temp.p8_check('company_check_present', exists (
    select 1 from pg_constraint where conname = 'assistant_spend_reservations_company_chk' and convalidated));

  insert into auth.users (id, email) values
    (u1, 'p8chk-' || u1::text || '@example.invalid'), (u2, 'p8chk-' || u2::text || '@example.invalid'),
    (u3, 'p8chk-' || u3::text || '@example.invalid'), (u4, 'p8chk-' || u4::text || '@example.invalid');
  insert into public.profiles (id, full_name, email, role, is_active) values
    (u1, 'Phase 8 fixture one', 'p8chk-' || u1::text || '@example.invalid', 'estimating_engineer_materials', true),
    (u2, 'Phase 8 fixture two', 'p8chk-' || u2::text || '@example.invalid', 'it_admin', true),
    (u3, 'Phase 8 fixture three', 'p8chk-' || u3::text || '@example.invalid', 'executive', true),
    (u4, 'Phase 8 fixture four', 'p8chk-' || u4::text || '@example.invalid', 'executive', true)
  on conflict (id) do update set role = excluded.role, is_active = true;

  -- 1. Enqueue.
  v_epoch := pg_temp.p8_epoch();
  r_old := pg_temp.p8_inbox(u1, 'bell.quote_received', v_epoch - interval '1 hour');
  r1 := pg_temp.p8_inbox(u1, 'bell.quote_received', clock_timestamp());
  r2 := pg_temp.p8_inbox(u1, 'bell.drawing_changed', clock_timestamp());
  r_ftb := pg_temp.p8_inbox(u1, 'project.failed_to_bid', clock_timestamp());
  r_dis := pg_temp.p8_inbox(u1, 'bell.quote_received', clock_timestamp());
  update public.assistant_inbox set dismissed_at = clock_timestamp(), dismissed_reason = 'user' where id = r_dis;
  r_u2 := pg_temp.p8_inbox(u2, 'bell.quote_received', clock_timestamp());
  v_now := clock_timestamp() + interval '1 second';

  update public.assistant_settings set triage_enabled = false where id = 1;
  perform pg_temp.p8_check('enqueue_off_returns_0', public.assistant_triage_enqueue(v_now, 24, 500, array[u1, u2]) = 0);
  update public.assistant_settings set triage_enabled = true where id = 1;
  v_n := public.assistant_triage_enqueue(v_now, 24, 500, array[u1, u2]);
  perform pg_temp.p8_check('enqueue_count', v_n = 3);
  perform pg_temp.p8_check('enqueue_rows', (select count(*) from public.assistant_triage_items
     where inbox_id in (r1, r2, r_u2) and status = 'pending' and created_at = v_now) = 3);
  perform pg_temp.p8_check('enqueue_starts_at_epoch', not exists (select 1 from public.assistant_triage_items where inbox_id = r_old));
  perform pg_temp.p8_check('enqueue_never_failed_to_bid', not exists (select 1 from public.assistant_triage_items where inbox_id = r_ftb));
  perform pg_temp.p8_check('enqueue_never_dismissed', not exists (select 1 from public.assistant_triage_items where inbox_id = r_dis));
  perform pg_temp.p8_check('enqueue_copies_columns', exists (
    select 1 from public.assistant_triage_items t join public.assistant_inbox i on i.id = t.inbox_id
     where t.inbox_id = r1 and t.event_type = i.event_type and t.project_id = i.project_id
       and t.source_notification_id = i.source_notification_id and t.enqueued_at >= v_epoch));
  perform pg_temp.p8_check('enqueue_idempotent', public.assistant_triage_enqueue(v_now, 24, 500, array[u1, u2]) = 0);
  update public.assistant_inbox set created_at = v_now + interval '5 seconds' where id = r1;
  perform public.assistant_triage_enqueue(v_now + interval '10 seconds', 24, 500, array[u1, u2]);
  perform pg_temp.p8_check('enqueue_survives_moved_created_at',
    (select count(*) from public.assistant_triage_items where inbox_id = r1) = 1);
  perform pg_temp.p8_check('enqueue_travel_back_enqueues_nothing',
    public.assistant_triage_enqueue(v_epoch - interval '1 day', 24, 500, array[u1, u2, u3]) = 0);
  r_u3 := pg_temp.p8_inbox(u3, 'bell.quote_received', clock_timestamp());
  v_n := public.assistant_triage_enqueue(clock_timestamp() + interval '1 second', 24, 1000, null);
  perform pg_temp.p8_check('enqueue_every_owner_without_a_flag',
    v_n >= 1 and exists (select 1 from public.assistant_triage_items where inbox_id = r_u3));
  update public.assistant_settings set triage_fuse_tripped_at = clock_timestamp() where id = 1;
  perform pg_temp.p8_inbox(u1, 'bell.quote_received', clock_timestamp());
  perform pg_temp.p8_check('enqueue_latched_returns_0',
    public.assistant_triage_enqueue(clock_timestamp() + interval '1 second', 24, 500, array[u1]) = 0);
  perform pg_temp.p8_check('claim_latched_returns_fuse',
    (public.assistant_triage_claim(w, clock_timestamp(), 420, 0, 10, 0, 0, array[u1])->>'fuse')::boolean);
  update public.assistant_settings set triage_enabled = false where id = 1;
  perform pg_temp.p8_check('claim_off_returns_switched_off',
    (public.assistant_triage_claim(w, clock_timestamp(), 420, 0, 10, 0, 0, array[u1])->>'switched_off')::boolean);

  -- 2. Claim: debounce, batch size, allowlist, a retried item alone, no second run, time travel.
  v_epoch := pg_temp.p8_epoch();
  perform pg_temp.p8_inbox(u1, 'bell.quote_received', clock_timestamp());
  perform pg_temp.p8_inbox(u1, 'bell.quote_received', clock_timestamp());
  perform pg_temp.p8_inbox(u1, 'bell.drawing_changed', clock_timestamp());
  perform pg_temp.p8_inbox(u2, 'bell.quote_received', clock_timestamp());
  perform pg_temp.p8_inbox(u2, 'bell.quote_received', clock_timestamp());
  v_x := clock_timestamp() + interval '1 second';
  perform public.assistant_triage_enqueue(v_x, 24, 500, array[u1, u2]);
  perform pg_temp.p8_check('claim_debounce_waits',
    public.assistant_triage_claim(w, v_x + interval '100 seconds', 420, 300, 10, 0, 0, array[u1]) is null);
  j := public.assistant_triage_claim(w, v_x + interval '301 seconds', 420, 300, 10, 0, 0, array[u1]);
  run1 := (j->>'run_id')::uuid;
  perform pg_temp.p8_check('claim_after_debounce', run1 is not null and (j->>'user_id')::uuid = u1
    and jsonb_array_length(j->'items') = 3);
  -- The claim output is exactly these keys and nothing else (no title, payload or other row text).
  perform pg_temp.p8_check('claim_item_shape', (select array_agg(k order by k collate "C") from jsonb_object_keys(j) k)
      = array['items', 'run_id', 'user_id']
    and (select bool_and((select array_agg(k order by k collate "C") from jsonb_object_keys(e) k)
                         = array['cap_deferred_at', 'created_at', 'event_type', 'id', 'inbox_created_at', 'inbox_id',
                                 'project_id', 'source_notification_id'])
           from jsonb_array_elements(j->'items') e));
  select * into rn from public.assistant_triage_runs where id = run1;
  perform pg_temp.p8_check('claim_run_row', rn.status = 'claimed' and rn.claimed_by = w and rn.item_count = 3
    and rn.lease_until > clock_timestamp() + interval '400 seconds' and rn.created_at = v_x + interval '301 seconds');
  -- The live-run guard (claim steps 5 and 6). u1 gets one more pending item that is past the
  -- debounce, so the only thing keeping a second claim from returning a run is run1's live
  -- lease; without the not-exists filter and the re-check under the lock this check fails.
  i1 := pg_temp.p8_item(pg_temp.p8_inbox(u1, 'bell.quote_received', clock_timestamp()), null);
  j2 := public.assistant_triage_claim(w2, v_x + interval '400 seconds', 420, 300, 10, 0, 0, array[u1]);
  perform pg_temp.p8_check('claim_no_second_run_for_a_person', j2 is null
    and (select count(*) from public.assistant_triage_runs where user_id = u1 and status = 'claimed') = 1
    and (select status from public.assistant_triage_items where id = i1) = 'pending');
  -- The same shape of claim does return a run once the lease is gone (the guard is the only
  -- difference), and the expired run is retired by that claim.
  d := pg_temp.p8_run(u3, w);
  i2 := pg_temp.p8_item(pg_temp.p8_inbox(u3, 'bell.quote_received', clock_timestamp()), null);
  j2 := public.assistant_triage_claim(w2, clock_timestamp() + interval '1 hour', 420, 300, 10, 0, 0, array[u3]);
  perform pg_temp.p8_check('claim_live_run_blocks_another_run', j2 is null
    and (select count(*) from public.assistant_triage_runs where user_id = u3 and status = 'claimed') = 1
    and (select status from public.assistant_triage_items where id = i2) = 'pending');
  update public.assistant_triage_runs set lease_until = clock_timestamp() - interval '1 second' where id = d;
  j2 := public.assistant_triage_claim(w2, clock_timestamp() + interval '1 hour', 420, 300, 10, 0, 0, array[u3]);
  perform pg_temp.p8_check('claim_after_the_lease_expires_takes_a_run', j2 is not null
    and (j2->>'user_id')::uuid = u3 and (j2->>'run_id')::uuid <> d and jsonb_array_length(j2->'items') = 1
    and (select status from public.assistant_triage_runs where id = d) = 'failed');
  perform public.assistant_triage_claim(w2, v_x + interval '8 days', 420, 300, 10, 0, 0, array[u1]);
  perform pg_temp.p8_check('claim_ahead_never_retires_a_live_lease',
    (select status from public.assistant_triage_runs where id = run1) = 'claimed');
  perform public.assistant_triage_claim(w2, v_x + interval '30 days', 420, 300, 10, 0, 0, array[u1]);
  perform pg_temp.p8_check('claim_far_ahead_still_live',
    (select status from public.assistant_triage_runs where id = run1) = 'claimed');
  -- Two statements: a subquery in the same statement as the writing claim would read the
  -- statement-start snapshot and always see the state from before the claim.
  j := public.assistant_triage_claim(w2, v_x + interval '400 seconds', 420, 300, 10, 0, 0, array[u1]);
  perform pg_temp.p8_check('claim_allowlist_filters', j is null
    and (select count(*) from public.assistant_triage_items where user_id = u2 and status = 'pending') = 2);
  update public.assistant_triage_items set attempts = 1
   where id = (select id from public.assistant_triage_items where user_id = u2 and status = 'pending'
                order by created_at, id limit 1);
  j := public.assistant_triage_claim(w2, v_x + interval '400 seconds', 420, 300, 10, 0, 0, array[u2]);
  run2 := (j->>'run_id')::uuid;
  perform pg_temp.p8_check('claim_retried_item_alone', jsonb_array_length(j->'items') = 1
    and (select attempts from public.assistant_triage_items where id = (j->'items'->0->>'id')::uuid) = 1);
  -- Batch size and the single-session race form (MC-5): 15 items, batch 10, one run.
  for v_n in 1..15 loop
    perform pg_temp.p8_inbox(u4, 'bell.quote_received', clock_timestamp());
  end loop;
  v_x := clock_timestamp() + interval '1 second';
  perform public.assistant_triage_enqueue(v_x, 24, 500, array[u4]);
  j := public.assistant_triage_claim(w, v_x, 420, 3600, 10, 0, 0, array[u4]);
  -- Debounce 0: the 5 leftover items qualify on their own (age 0 <= 0), so only run3's live
  -- lease keeps this second claim from returning a run.
  j2 := public.assistant_triage_claim(w2, v_x, 420, 0, 10, 0, 0, array[u4]);
  run3 := (j->>'run_id')::uuid;
  perform pg_temp.p8_check('claim_batch_max_and_one_run', jsonb_array_length(j->'items') = 10 and j2 is null
    and (select count(*) from public.assistant_triage_runs where user_id = u4) = 1
    and (select count(*) from public.assistant_triage_items where user_id = u4 and status = 'pending') = 5);

  -- 3. Finish.
  select t.id into a from public.assistant_triage_items t where t.run_id = run1 order by t.created_at, t.id limit 1;
  select t.id into b from public.assistant_triage_items t where t.run_id = run1 and t.id <> a order by t.created_at, t.id limit 1;
  select t.id into c from public.assistant_triage_items t where t.run_id = run1 and t.id not in (a, b) limit 1;
  select t.id into x1 from public.assistant_triage_items t where t.run_id = run3 limit 1;
  v_now := clock_timestamp();
  j := public.assistant_triage_finish(run1, w, v_now, 'decided', null, jsonb_build_array(
         jsonb_build_object('id', a, 'decision', 'escalate', 'reason', 'due_soon_part_missing', 'action', null,
                            'severity_hint', 'urgent'),
         jsonb_build_object('id', b, 'decision', 'suggest', 'reason', 'follow_up_due',
                            'action', 'review_changed_drawings', 'severity_hint', null),
         jsonb_build_object('id', x1, 'decision', 'escalate', 'reason', 'unread_long', 'action', null,
                            'severity_hint', 'high')),
         jsonb_build_object('model', 'gpt-6-luna', 'input_tokens', 900, 'cached_tokens', 0, 'output_tokens', 300,
                            'cost_usd', 0.00024));
  perform pg_temp.p8_check('finish_decided', (j->>'fenced')::boolean and (j->>'emitted')::int = 1);
  select * into it from public.assistant_triage_items where id = a;
  select * into ev from public.assistant_events where dedupe_key = 'p8:esc:' || it.inbox_id::text;
  perform pg_temp.p8_check('finish_escalation_event', ev.id is not null and it.escalation_event_id = ev.id
    and ev.event_type = 'assistant.triage_escalation' and ev.source = 'emit' and ev.status = 'pending'
    and ev.subject_user_id = u1 and ev.project_id = it.project_id and ev.occurred_at = v_now
    and ev.payload->>'inbox_id' = it.inbox_id::text and ev.payload->>'triage_item_id' = a::text
    and ev.payload->>'run_id' = run1::text and ev.payload->>'severity_hint' = 'urgent'
    and ev.payload->>'reason' = 'due_soon_part_missing' and ev.payload ? 'emitted_at'
    -- exactly these keys: no inbox title or other row text rides in the payload
    and (select array_agg(k order by k collate "C") from jsonb_object_keys(ev.payload) k)
        = array['emitted_at', 'inbox_id', 'reason', 'run_id', 'severity_hint', 'source_kind', 'triage_item_id']);
  perform pg_temp.p8_check('finish_urgent_stored', it.status = 'decided' and it.decision = 'escalate'
    and it.severity_hint = 'urgent' and it.decided_at = v_now);
  select * into it from public.assistant_triage_items where id = b;
  perform pg_temp.p8_check('finish_suggest_stored', it.decision = 'suggest' and it.action = 'review_changed_drawings'
    and it.severity_hint is null and it.escalation_event_id is null);
  select * into it from public.assistant_triage_items where id = c;
  perform pg_temp.p8_check('finish_unnamed_item_retried', it.status = 'pending' and it.attempts = 1
    and it.next_attempt_at = v_now + interval '60 seconds');
  perform pg_temp.p8_check('finish_ignores_another_runs_item',
    (select status from public.assistant_triage_items where id = x1) = 'claimed'
    and not exists (select 1 from public.assistant_events e join public.assistant_triage_items t on e.dedupe_key = 'p8:esc:' || t.inbox_id::text
                     where t.id = x1));
  select * into rn from public.assistant_triage_runs where id = run1;
  perform pg_temp.p8_check('finish_usage', rn.status = 'decided' and rn.model = 'gpt-6-luna' and rn.input_tokens = 900
    and rn.output_tokens = 300 and rn.cost_usd = 0.00024 and rn.finished_at = v_now);
  perform pg_temp.p8_check('finish_twice_is_fenced',
    not (public.assistant_triage_finish(run1, w, v_now, 'decided', null, '[]'::jsonb, null)->>'fenced')::boolean);
  -- The same row escalated twice yields one event.
  d := pg_temp.p8_run(u1, w);
  update public.assistant_triage_items
     set status = 'claimed', run_id = d, decision = null, reason = null, action = null, severity_hint = null,
         decided_at = null, escalation_event_id = null
   where id = a;
  j := public.assistant_triage_finish(d, w, v_now, 'decided', null, jsonb_build_array(
         jsonb_build_object('id', a, 'decision', 'escalate', 'reason', 'unread_long', 'action', null,
                            'severity_hint', 'high')), null);
  perform pg_temp.p8_check('finish_dedupes_the_event', (j->>'emitted')::int = 0
    and (select count(*) from public.assistant_events e join public.assistant_triage_items t
           on e.dedupe_key = 'p8:esc:' || t.inbox_id::text where t.id = a) = 1
    and (select escalation_event_id from public.assistant_triage_items where id = a) = ev.id);
  -- A lost real-clock lease: nothing written, nothing emitted; the next claim retires it.
  update public.assistant_triage_runs set lease_until = clock_timestamp() - interval '1 second' where id = run3;
  j := public.assistant_triage_finish(run3, w, v_now, 'decided', null, jsonb_build_array(
         jsonb_build_object('id', x1, 'decision', 'escalate', 'reason', 'unread_long', 'action', null,
                            'severity_hint', 'high')), null);
  perform pg_temp.p8_check('finish_after_lease_is_fenced', not (j->>'fenced')::boolean
    and (select status from public.assistant_triage_items where id = x1) = 'claimed'
    and not exists (select 1 from public.assistant_events e join public.assistant_triage_items t
                     on e.dedupe_key = 'p8:esc:' || t.inbox_id::text where t.id = x1));
  perform public.assistant_triage_claim(w2, clock_timestamp(), 420, 3600, 10, 0, 0, array[u2]);
  select * into rn from public.assistant_triage_runs where id = run3;
  select * into it from public.assistant_triage_items where id = x1;
  perform pg_temp.p8_check('claim_retires_expired_lease', rn.status = 'failed' and rn.error_kind = 'lease_expired'
    and it.status = 'pending' and it.attempts = 0 and it.infra_retries = 1);
  -- A finish after skip_user (ineligible) is fenced; skip_user fails the run as ineligible.
  v_n := public.assistant_triage_skip_user(u2, 'ineligible', v_now);
  select * into rn from public.assistant_triage_runs where id = run2;
  perform pg_temp.p8_check('skip_user_ineligible', v_n >= 1 and rn.status = 'failed' and rn.error_kind = 'ineligible'
    and not exists (select 1 from public.assistant_triage_items where user_id = u2 and status in ('pending', 'claimed'))
    and exists (select 1 from public.assistant_triage_items where user_id = u2 and skip_reason = 'ineligible'));
  select t.id into i1 from public.assistant_triage_items t where t.run_id = run2 limit 1;
  j := public.assistant_triage_finish(run2, w2, v_now, 'decided', null, jsonb_build_array(
         jsonb_build_object('id', i1, 'decision', 'escalate', 'reason', 'unread_long', 'action', null,
                            'severity_hint', 'high')), null);
  perform pg_temp.p8_check('finish_after_skip_user_is_fenced', not (j->>'fenced')::boolean
    and not exists (select 1 from public.assistant_events e join public.assistant_triage_items t
                     on e.dedupe_key = 'p8:esc:' || t.inbox_id::text where t.id = i1));
  d := pg_temp.p8_run(u3, w);
  perform pg_temp.p8_item(pg_temp.p8_inbox(u3, 'bell.quote_received', clock_timestamp()), d);
  perform public.assistant_triage_skip_user(u3, 'offboarded', v_now);
  perform pg_temp.p8_check('skip_user_offboarded_fails_switched_off',
    (select status || ':' || error_kind from public.assistant_triage_runs where id = d) = 'failed:switched_off'
    and exists (select 1 from public.assistant_triage_items where run_id = d and skip_reason = 'offboarded'));
  -- skip_user takes the claim's per-person advisory lock (same key) before its updates,
  -- so a claim in flight cannot commit a live run after it (one session cannot race it).
  v_s := pg_get_functiondef('public.assistant_triage_skip_user(uuid, text, timestamptz)'::regprocedure);
  perform pg_temp.p8_check('skip_user_takes_the_claim_lock',
    position('pg_advisory_xact_lock(hashtext(''assistant_triage:'' || p_user_id::text))' in v_s) > 0
    and position('pg_advisory_xact_lock(hashtext(''assistant_triage:'' || p_user_id::text))' in v_s)
        < position('update public.assistant_triage_runs' in v_s));
  -- Switch off before the finish: skipped switched_off, no event.
  d := pg_temp.p8_run(u1, w);
  i1 := pg_temp.p8_item(pg_temp.p8_inbox(u1, 'bell.quote_received', clock_timestamp()), d);
  update public.assistant_settings set triage_enabled = false where id = 1;
  j := public.assistant_triage_finish(d, w, v_now, 'decided', null, jsonb_build_array(
         jsonb_build_object('id', i1, 'decision', 'escalate', 'reason', 'unread_long', 'action', null,
                            'severity_hint', 'urgent')), null);
  perform pg_temp.p8_check('finish_switch_off', (j->>'fenced')::boolean and (j->>'emitted')::int = 0
    and (select status || ':' || skip_reason from public.assistant_triage_items where id = i1) = 'skipped:switched_off'
    and (select status || ':' || error_kind from public.assistant_triage_runs where id = d) = 'skipped:switched_off');
  -- Epoch fence (P8R-3): claimed, then off and on inside the lease, then the finish.
  update public.assistant_settings set triage_enabled = true where id = 1;
  d := pg_temp.p8_run(u1, w);
  i1 := pg_temp.p8_item(pg_temp.p8_inbox(u1, 'bell.quote_received', clock_timestamp()), d);
  v_epoch := pg_temp.p8_epoch();
  j := public.assistant_triage_finish(d, w, v_now, 'decided', null, jsonb_build_array(
         jsonb_build_object('id', i1, 'decision', 'escalate', 'reason', 'unread_long', 'action', null,
                            'severity_hint', 'high')), null);
  perform pg_temp.p8_check('finish_epoch_fence', (j->>'emitted')::int = 0
    and (select status || ':' || skip_reason from public.assistant_triage_items where id = i1) = 'skipped:switched_off');
  -- Latch set before the finish: skipped fuse, no event.
  d := pg_temp.p8_run(u1, w);
  i1 := pg_temp.p8_item(pg_temp.p8_inbox(u1, 'bell.quote_received', clock_timestamp()), d);
  update public.assistant_settings set triage_fuse_tripped_at = clock_timestamp() where id = 1;
  j := public.assistant_triage_finish(d, w, v_now, 'decided', null, jsonb_build_array(
         jsonb_build_object('id', i1, 'decision', 'escalate', 'reason', 'unread_long', 'action', null,
                            'severity_hint', 'high')), null);
  perform pg_temp.p8_check('finish_latch_fence', (j->>'emitted')::int = 0
    and (select status || ':' || skip_reason from public.assistant_triage_items where id = i1) = 'skipped:fuse');
  -- A malformed entry is not a decision: it goes back through the helper.
  v_epoch := pg_temp.p8_epoch();
  d := pg_temp.p8_run(u1, w);
  i1 := pg_temp.p8_item(pg_temp.p8_inbox(u1, 'bell.quote_received', clock_timestamp()), d);
  i2 := pg_temp.p8_item(pg_temp.p8_inbox(u1, 'bell.quote_received', clock_timestamp()), d);
  j := public.assistant_triage_finish(d, w, v_now, 'decided', null, jsonb_build_array(
         jsonb_build_object('id', i1, 'decision', 'escalate', 'reason', 'unread_long', 'action', null,
                            'severity_hint', 'loud'),
         jsonb_build_object('id', i2, 'decision', 'none', 'reason', 'covered_by_app', 'action', 'open_receive_quotes',
                            'severity_hint', 'high')), null);
  perform pg_temp.p8_check('finish_malformed_entry_retried',
    (select status || ':' || attempts from public.assistant_triage_items where id = i1) = 'pending:1'
    and (select decision || ':' || coalesce(action, '-') || ':' || coalesce(severity_hint, '-')
           from public.assistant_triage_items where id = i2) = 'none:-:-');

  -- 4. Retry helper from attempts = 2, and the infrastructure class.
  d := pg_temp.p8_run(u1, w);
  i1 := pg_temp.p8_item(pg_temp.p8_inbox(u1, 'bell.quote_received', clock_timestamp()), d, 2);
  i2 := pg_temp.p8_item(pg_temp.p8_inbox(u1, 'bell.quote_received', clock_timestamp()), d, 2);
  i3 := pg_temp.p8_item(pg_temp.p8_inbox(u1, 'bell.quote_received', clock_timestamp()), d, 2);
  i4 := pg_temp.p8_item(pg_temp.p8_inbox(u1, 'bell.quote_received', clock_timestamp()), d, 2);
  v_s := public.assistant_triage_item_retry(i1, 'bad_answer', v_now);
  perform pg_temp.p8_check('retry_bad_answer_at_3', v_s = 'skipped'
    and (select attempts || ':' || skip_reason from public.assistant_triage_items where id = i1) = '3:bad_answer');
  v_s := public.assistant_triage_item_retry(i2, cut, v_now);
  perform pg_temp.p8_check('retry_cut_off_at_3', v_s = 'failed'
    and (select status || ':' || attempts || ':' || coalesce(skip_reason, '-') from public.assistant_triage_items
          where id = i2) = 'failed:3:-');
  v_s := public.assistant_triage_item_retry(i3, 'unpriced', v_now);
  perform pg_temp.p8_check('retry_infra_keeps_attempts',
    v_s = 'pending'
    and (select attempts || ':' || infra_retries from public.assistant_triage_items where id = i3) = '2:1'
    and (select next_attempt_at from public.assistant_triage_items where id = i3) = v_now + interval '60 seconds');
  perform pg_temp.p8_check('retry_not_claimed_is_null', public.assistant_triage_item_retry(i3, 'bad_answer', v_now) is null);
  v_ok := false;
  begin
    perform public.assistant_triage_item_retry(i4, 'nonsense', v_now);
  exception when check_violation then v_ok := true;
  end;
  perform pg_temp.p8_check('retry_unknown_kind_raises', v_ok);
  j := public.assistant_triage_finish(d, w, v_now, 'decided', null, '[]'::jsonb, null);
  perform pg_temp.p8_check('finish_unnamed_at_attempts_2',
    (select status || ':' || attempts || ':' || skip_reason from public.assistant_triage_items where id = i4)
      = 'skipped:3:bad_answer');
  d := pg_temp.p8_run(u1, w);
  i1 := pg_temp.p8_item(pg_temp.p8_inbox(u1, 'bell.quote_received', clock_timestamp()), d, 2);
  j := public.assistant_triage_finish(d, w, v_now, 'failed', cut, '[]'::jsonb, null);
  perform pg_temp.p8_check('finish_cut_off_run_at_attempts_2',
    (select status || ':' || attempts from public.assistant_triage_items where id = i1) = 'failed:3');
  d := pg_temp.p8_run(u1, w);
  i1 := pg_temp.p8_item(pg_temp.p8_inbox(u1, 'bell.quote_received', clock_timestamp()), d, 2);
  j := public.assistant_triage_finish(d, w, v_now, 'failed', 'model_error', '[]'::jsonb, null);
  perform pg_temp.p8_check('finish_infra_never_bumps_attempts',
    (select status || ':' || attempts || ':' || infra_retries from public.assistant_triage_items where id = i1)
      = 'pending:2:1');
  d := pg_temp.p8_run(u1, w);
  i1 := pg_temp.p8_item(pg_temp.p8_inbox(u1, 'bell.quote_received', clock_timestamp()), d, 2);
  update public.assistant_triage_runs set lease_until = clock_timestamp() - interval '1 second' where id = d;
  perform public.assistant_triage_claim(w, clock_timestamp(), 420, 3600, 10, 0, 0, array[u2]);
  perform pg_temp.p8_check('lease_retirement_at_attempts_2',
    (select status || ':' || attempts from public.assistant_triage_items where id = i1) = 'pending:2');

  -- 5. Cap deferral to Pacific midnight (P13), finish ordering (MC-6), offset (MC-8), DST.
  update public.assistant_triage_items set status = 'skipped', skip_reason = 'stale'
   where user_id in (u1, u4) and status in ('pending', 'claimed');
  update public.assistant_triage_runs set status = 'failed', error_kind = 'switched_off' where user_id in (u1, u4) and status = 'claimed';
  v_epoch := pg_temp.p8_epoch();
  v_cap_now := (date_trunc('day', clock_timestamp() at time zone la) + interval '1 day 15 hours') at time zone la;
  v_mid := (date_trunc('day', clock_timestamp() at time zone la) + interval '2 days') at time zone la;
  d := pg_temp.p8_run(u4, w);
  i1 := pg_temp.p8_item(pg_temp.p8_inbox(u4, 'bell.quote_received', v_cap_now - interval '25 hours'), d);
  i2 := pg_temp.p8_item(pg_temp.p8_inbox(u4, 'bell.quote_received', v_cap_now - interval '1 hour'), d);
  i3 := pg_temp.p8_item(pg_temp.p8_inbox(u4, 'bell.quote_received', v_cap_now - interval '1 hour'), d);
  i4 := pg_temp.p8_item(pg_temp.p8_inbox(u4, 'bell.quote_received', v_mid - interval '25 hours'), d);
  j := public.assistant_triage_finish(d, w, v_cap_now, 'skipped', 'cap_global',
         jsonb_build_array(jsonb_build_object('id', i1, 'skip_reason', 'cap')), null);
  perform pg_temp.p8_check('cap_finish_skips_first', (j->>'fenced')::boolean
    and (select status || ':' || skip_reason from public.assistant_triage_items where id = i1) = 'skipped:cap');
  perform pg_temp.p8_check('cap_finish_defers_to_midnight', (select bool_and(
      t.status = 'pending' and t.cap_deferred_at = v_cap_now and t.attempts = 0 and t.infra_retries = 0
      and t.next_attempt_at - v_mid >= interval '0' and t.next_attempt_at - v_mid < interval '2 hours'
      and extract(epoch from t.next_attempt_at - v_mid) = mod(abs(hashtext(t.id::text)::bigint), 7200))
    from public.assistant_triage_items t where t.id in (i2, i3, i4)));
  select t.next_attempt_at - v_mid into v_off2 from public.assistant_triage_items t where t.id = i2;
  select t.next_attempt_at - v_mid into v_off3 from public.assistant_triage_items t where t.id = i3;
  perform pg_temp.p8_check('cap_not_claimed_before_midnight',
    public.assistant_triage_claim(w, v_mid - interval '1 minute', 420, 0, 10, 0, 0, array[u4]) is null);
  j := public.assistant_triage_claim(w, v_mid + interval '2 hours 1 second', 420, 300, 10, 0, 0, array[u4]);
  perform pg_temp.p8_check('cap_claimed_after_midnight', jsonb_array_length(j->'items') = 3
    and (select bool_and(e->>'cap_deferred_at' is not null) from jsonb_array_elements(j->'items') e));
  perform pg_temp.p8_check('cap_age_reported', exists (select 1 from jsonb_array_elements(j->'items') e
    where (e->>'id')::uuid = i4 and (e->>'inbox_created_at')::timestamptz <= v_mid + interval '2 hours' - interval '24 hours'));
  -- A second refusal after midnight defers again; the per-item skip then lands first.
  j2 := public.assistant_triage_finish((j->>'run_id')::uuid, w, v_mid + interval '2 hours 2 seconds', 'skipped', 'cap_test',
          jsonb_build_array(jsonb_build_object('id', i4, 'skip_reason', 'cap')), null);
  perform pg_temp.p8_check('cap_test_defers_again', (select status || ':' || skip_reason from public.assistant_triage_items
    where id = i4) = 'skipped:cap' and (select bool_and(t.status = 'pending' and t.cap_deferred_at = v_cap_now
      and t.next_attempt_at >= v_mid + interval '1 day') from public.assistant_triage_items t where t.id in (i2, i3)));
  -- MC-8: the offset is per item and stable, so after the next Pacific midnight each item
  -- keeps the offset of its first deferral (a re-salted or random offset fails here).
  v_mid2 := (date_trunc('day', (v_mid + interval '2 hours 2 seconds') at time zone la) + interval '1 day') at time zone la;
  perform pg_temp.p8_check('cap_offset_stable', v_off2 is not null and v_off3 is not null
    and (select next_attempt_at - v_mid2 from public.assistant_triage_items where id = i2) = v_off2
    and (select next_attempt_at - v_mid2 from public.assistant_triage_items where id = i3) = v_off3
    and v_off2 >= interval '0' and v_off2 < interval '2 hours'
    and v_off3 >= interval '0' and v_off3 < interval '2 hours'
    and extract(epoch from v_off2) = mod(abs(hashtext(i2::text)::bigint), 7200)
    and extract(epoch from v_off3) = mod(abs(hashtext(i3::text)::bigint), 7200));
  -- DST: the next midnight is local 00:00 on both sides of each 2026 change (spring forward
  -- 2026-03-08 02:00, fall back 2026-11-01 02:00). A midnight built as local midnight plus 24
  -- hours would land on 01:00 after the spring change and 23:00 after the fall change.
  d := pg_temp.p8_run(u4, w);
  i1 := pg_temp.p8_item(pg_temp.p8_inbox(u4, 'bell.quote_received', clock_timestamp()), d);
  i2 := pg_temp.p8_item(pg_temp.p8_inbox(u4, 'bell.quote_received', clock_timestamp()), d);
  i3 := pg_temp.p8_item(pg_temp.p8_inbox(u4, 'bell.quote_received', clock_timestamp()), d);
  i4 := pg_temp.p8_item(pg_temp.p8_inbox(u4, 'bell.quote_received', clock_timestamp()), d);
  i5 := pg_temp.p8_item(pg_temp.p8_inbox(u4, 'bell.quote_received', clock_timestamp()), d);
  i6 := pg_temp.p8_item(pg_temp.p8_inbox(u4, 'bell.quote_received', clock_timestamp()), d);
  perform public.assistant_triage_item_retry(i1, 'cap', timestamptz '2026-10-31 23:30:00 America/Los_Angeles');
  perform public.assistant_triage_item_retry(i2, 'cap', timestamptz '2026-11-01 23:30:00 America/Los_Angeles');
  perform public.assistant_triage_item_retry(i3, 'cap', timestamptz '2026-03-08 01:30:00 America/Los_Angeles');
  perform public.assistant_triage_item_retry(i4, 'cap', timestamptz '2026-03-08 23:30:00 America/Los_Angeles');
  perform public.assistant_triage_item_retry(i5, 'cap', timestamptz '2026-11-01 00:30:00 America/Los_Angeles');
  perform public.assistant_triage_item_retry(i6, 'cap', timestamptz '2026-11-01 15:00:00 America/Los_Angeles');
  perform pg_temp.p8_check('cap_midnight_across_dst',
    (select next_attempt_at - make_interval(secs => mod(abs(hashtext(id::text)::bigint), 7200)::double precision)
       from public.assistant_triage_items where id = i1) = timestamptz '2026-11-01 07:00:00+00'
    and (select next_attempt_at - make_interval(secs => mod(abs(hashtext(id::text)::bigint), 7200)::double precision)
       from public.assistant_triage_items where id = i2) = timestamptz '2026-11-02 08:00:00+00'
    and (select next_attempt_at - make_interval(secs => mod(abs(hashtext(id::text)::bigint), 7200)::double precision)
       from public.assistant_triage_items where id = i5) = timestamptz '2026-11-02 08:00:00+00'
    and (select next_attempt_at - make_interval(secs => mod(abs(hashtext(id::text)::bigint), 7200)::double precision)
       from public.assistant_triage_items where id = i6) = timestamptz '2026-11-02 08:00:00+00');
  perform pg_temp.p8_check('cap_midnight_spring_forward',
    (select next_attempt_at - make_interval(secs => mod(abs(hashtext(id::text)::bigint), 7200)::double precision)
       from public.assistant_triage_items where id = i3) = timestamptz '2026-03-09 07:00:00+00'
    and (select next_attempt_at - make_interval(secs => mod(abs(hashtext(id::text)::bigint), 7200)::double precision)
       from public.assistant_triage_items where id = i4) = timestamptz '2026-03-09 07:00:00+00');

  -- 6. Epoch sweep (F2): an item enqueued 2 days ahead, then an off and on flip.
  v_epoch := pg_temp.p8_epoch();
  r3 := pg_temp.p8_inbox(u1, 'bell.quote_received', clock_timestamp());
  v_x := clock_timestamp() + interval '2 days';
  perform public.assistant_triage_enqueue(v_x, 72, 500, array[u1]);
  perform pg_temp.p8_check('sweep_setup', exists (select 1 from public.assistant_triage_items
    where inbox_id = r3 and status = 'pending' and created_at = v_x));
  update public.assistant_settings set triage_enabled = false where id = 1;
  v_epoch := pg_temp.p8_epoch();
  j := public.assistant_triage_claim(w, v_x + interval '10 minutes', 420, 300, 10, 0, 0, array[u1]);
  perform pg_temp.p8_check('sweep_settles_pre_flip_items', j is null
    and (select status || ':' || skip_reason from public.assistant_triage_items where inbox_id = r3) = 'skipped:switched_off');
  -- More than the sweep's 500 pre-epoch pending items, an off and on flip with no tick
  -- between, then one post-epoch item: the claim takes only the post-epoch item and never
  -- an unswept pre-epoch one (F2, FQ11).
  update public.assistant_triage_items set status = 'skipped', skip_reason = 'stale', next_attempt_at = null
   where user_id = u1 and status in ('pending', 'claimed');
  update public.assistant_triage_runs set status = 'failed', error_kind = 'switched_off' where user_id = u1 and status = 'claimed';
  for v_m in 1..505 loop
    perform pg_temp.p8_item(pg_temp.p8_inbox(u1, 'bell.quote_received', clock_timestamp()), null);
  end loop;
  update public.assistant_settings set triage_enabled = false where id = 1;
  v_epoch := pg_temp.p8_epoch();
  i1 := pg_temp.p8_item(pg_temp.p8_inbox(u1, 'bell.quote_received', clock_timestamp()), null);
  j := public.assistant_triage_claim(w, clock_timestamp() + interval '10 minutes', 420, 300, 10, 0, 0, array[u1]);
  perform pg_temp.p8_check('sweep_leaves_some_pre_epoch_pending', exists (select 1 from public.assistant_triage_items
    where user_id = u1 and status = 'pending' and enqueued_at < v_epoch));
  perform pg_temp.p8_check('claim_never_takes_unswept_pre_epoch_items', j is not null
    and jsonb_array_length(j->'items') = 1 and (j->'items'->0->>'id')::uuid = i1
    and not exists (select 1 from public.assistant_triage_items
                     where user_id = u1 and status = 'claimed' and enqueued_at < v_epoch));
  update public.assistant_triage_items set status = 'skipped', skip_reason = 'stale', next_attempt_at = null
   where user_id = u1 and status in ('pending', 'claimed');
  update public.assistant_triage_runs set status = 'failed', error_kind = 'switched_off' where user_id = u1 and status = 'claimed';

  -- 7. Fuse latch (P10, P8R-1, MC-2, F1).
  v_epoch := pg_temp.p8_epoch();
  d := pg_temp.p8_run(u1, w);
  v_now := clock_timestamp();
  for v_n in 1..30 loop
    i1 := pg_temp.p8_item(pg_temp.p8_inbox(u1, 'bell.quote_received', clock_timestamp()), d);
    update public.assistant_triage_items
       set status = 'decided', decision = 'escalate', reason = 'unread_long', severity_hint = 'urgent',
           decided_at = v_now - interval '10 minutes'
     where id = i1;
  end loop;
  update public.assistant_triage_runs set status = 'decided' where id = d;
  -- Boundary: "more than 30 in one hour" (owner answer 10 a), so exactly 30 does not trip (ratio wire off).
  j := public.assistant_triage_claim(w, v_now, 420, 300, 10, 0, 30, array[u1]);
  perform pg_temp.p8_check('fuse_hourly_exactly_30_does_not_trip', j is null
    and (select triage_fuse_tripped_at from public.assistant_settings where id = 1) is null);
  i1 := pg_temp.p8_item(pg_temp.p8_inbox(u1, 'bell.quote_received', clock_timestamp()), d);
  update public.assistant_triage_items
     set status = 'decided', decision = 'escalate', reason = 'unread_long', severity_hint = 'urgent',
         decided_at = v_now - interval '10 minutes'
   where id = i1;
  j := public.assistant_triage_claim(w, v_now, 420, 300, 10, 0, 30, array[u1]);
  perform pg_temp.p8_check('fuse_hourly_trips', (j->>'fuse')::boolean and (j->>'tripped_now')::boolean
    and (select triage_fuse_tripped_at from public.assistant_settings where id = 1) is not null);
  j := public.assistant_triage_claim(w, v_now + interval '61 minutes', 420, 300, 10, 0, 30, array[u1]);
  perform pg_temp.p8_check('fuse_latched_after_61_minutes', (j->>'fuse')::boolean);
  j := public.assistant_triage_claim(w2, v_now, 420, 300, 10, 0, 30, array[u1]);
  perform pg_temp.p8_check('fuse_seen_by_the_other_worker', (j->>'fuse')::boolean and not (j->>'tripped_now')::boolean);
  j := public.assistant_triage_counts(v_epoch, v_now);
  perform pg_temp.p8_check('counts_report_the_fuse', j->>'fuse_tripped_at' is not null
    and (j->'fuse'->>'hour_escalations')::int = 31 and (j->'decision'->>'escalate')::int >= 31
    and j ? 'spend' and j ? 'cap_deferred_pending' and j->'roles' = '[]'::jsonb);
  -- A6.5: no user id (and no inbox text) leaves the counts.
  perform pg_temp.p8_check('counts_carry_no_user_id', position(u1::text in j::text) = 0
    and position(u2::text in j::text) = 0 and position(u3::text in j::text) = 0
    and position(u4::text in j::text) = 0 and position('Phase 8 fixture' in j::text) = 0);
  i2 := pg_temp.p8_item(pg_temp.p8_inbox(u1, 'bell.quote_received', clock_timestamp()), null);
  v_n := public.assistant_triage_skip_all('fuse', v_now, 500);
  perform pg_temp.p8_check('skip_all_fuse_while_latched', v_n >= 1
    and (select skip_reason from public.assistant_triage_items where id = i2) = 'fuse');
  update public.assistant_settings set triage_enabled = false where id = 1;
  v_epoch := pg_temp.p8_epoch();
  update public.assistant_triage_items set decided_at = v_now + interval '1 day' where run_id = d;
  j := public.assistant_triage_claim(w, v_now + interval '1 day 1 minute', 420, 300, 10, 0.5, 30, array[u1]);
  perform pg_temp.p8_check('fuse_flip_clears_and_ignores_pre_flip_runs', j is null
    and (select triage_fuse_tripped_at from public.assistant_settings where id = 1) is null);
  -- Ratio wire: more than half of the last 50, at least 20 (owner answer 10 a). 19 escalations
  -- alone do not trip; exactly 20 decisions with 10 escalations (a half) do not; 11 of 20 trips.
  v_epoch := pg_temp.p8_epoch();
  d := pg_temp.p8_run(u1, w);
  for v_n in 1..19 loop
    i1 := pg_temp.p8_item(pg_temp.p8_inbox(u1, 'bell.quote_received', clock_timestamp()), d);
    update public.assistant_triage_items
       set status = 'decided', decision = 'escalate', reason = 'unread_long', severity_hint = 'high',
           decided_at = v_now - interval '3 hours' + make_interval(secs => v_n)
     where id = i1;
  end loop;
  j := public.assistant_triage_claim(w, v_now, 420, 300, 10, 0.5, 0, array[u1]);
  perform pg_temp.p8_check('fuse_ratio_needs_20', j is null);
  update public.assistant_triage_items set decision = 'none', reason = 'covered_by_app', severity_hint = null
   where id in (select id from public.assistant_triage_items where run_id = d order by decided_at limit 9);
  i1 := pg_temp.p8_item(pg_temp.p8_inbox(u1, 'bell.quote_received', clock_timestamp()), d);
  update public.assistant_triage_items
     set status = 'decided', decision = 'none', reason = 'covered_by_app', severity_hint = null,
         decided_at = v_now - interval '2 hours'
   where id = i1;
  j := public.assistant_triage_claim(w, v_now, 420, 300, 10, 0.5, 0, array[u1]);
  perform pg_temp.p8_check('fuse_ratio_exactly_half_does_not_trip', j is null
    and (select count(*) from public.assistant_triage_items where run_id = d and status = 'decided') = 20
    and (select count(*) from public.assistant_triage_items where run_id = d and decision = 'escalate') = 10
    and (select triage_fuse_tripped_at from public.assistant_settings where id = 1) is null);
  update public.assistant_triage_items
     set decision = 'escalate', reason = 'unread_long', severity_hint = 'high'
   where id = i1;
  j := public.assistant_triage_claim(w, v_now, 420, 300, 10, 0.5, 0, array[u1]);
  perform pg_temp.p8_check('fuse_ratio_trips', (j->>'fuse')::boolean);
  -- skip_all acts only while a switch is off or the latch is set.
  v_epoch := pg_temp.p8_epoch();
  i1 := pg_temp.p8_item(pg_temp.p8_inbox(u1, 'bell.quote_received', clock_timestamp()), null);
  v_n := public.assistant_triage_skip_all('switched_off', v_now, 500);
  v_m := public.assistant_triage_skip_all('fuse', v_now, 500);
  perform pg_temp.p8_check('skip_all_refuses_while_running', v_n = 0 and v_m = 0
    and (select status from public.assistant_triage_items where id = i1) = 'pending');
  update public.assistant_settings set proactive_enabled = false where id = 1;
  v_n := public.assistant_triage_skip_all('switched_off', v_now, 500);
  perform pg_temp.p8_check('skip_all_acts_when_proactive_off', v_n >= 1
    and (select skip_reason from public.assistant_triage_items where id = i1) = 'switched_off');
  v_epoch := pg_temp.p8_epoch();
  i1 := pg_temp.p8_item(pg_temp.p8_inbox(u1, 'bell.quote_received', clock_timestamp()), null);
  update public.assistant_settings set enabled = false where id = 1;
  perform pg_temp.p8_check('skip_all_acts_when_assistant_off',
    public.assistant_triage_skip_all('switched_off', v_now, 500) >= 1);
  v_epoch := pg_temp.p8_epoch();

  -- 8. Spend (part B): kind triage only under the company id, company cap only.
  v_ok := false;
  begin
    perform public.assistant_spend_reserve(u1, 'triage', 0.001, 300, 15, 50, gen_random_uuid(), null, null);
  exception when check_violation then v_ok := true;
  end;
  perform pg_temp.p8_check('reserve_triage_person_raises', v_ok);
  v_ok := false;
  begin
    perform public.assistant_spend_reserve(co, 'turn_call', 0.001, 300, 15, 50, gen_random_uuid(), null, null);
  exception when check_violation then v_ok := true;
  end;
  perform pg_temp.p8_check('reserve_company_other_kind_raises', v_ok);
  v_ok := false;
  begin
    perform public.assistant_spend_reserve(null, 'triage', 0.001, 300, 15, 50, gen_random_uuid(), null, null);
  exception when check_violation then v_ok := true;
  end;
  perform pg_temp.p8_check('reserve_triage_null_raises', v_ok);
  v_ok := false;
  begin
    insert into public.assistant_spend_reservations (user_id, kind, amount_usd, expires_at)
    values (u1, 'triage', 0.01, now() + interval '5 minutes');
  exception when check_violation then v_ok := true;
  end;
  perform pg_temp.p8_check('company_check_refuses_person_triage_row', v_ok);
  st0 := public.assistant_spend_state(null, 0, 50);
  v_base := (st0->>'global_spent')::numeric + (st0->>'global_held')::numeric;
  v_cap := ceil(v_base * 100) / 100 + 1;
  update public.assistant_settings set global_daily_cap_usd = v_cap where id = 1;
  v_head := v_cap - v_base;
  j := public.assistant_spend_reserve(co, 'triage', v_head - 0.000001, 300, 0, 50, gen_random_uuid(), null, null);
  perform pg_temp.p8_check('reserve_triage_skips_the_person_cap', (j->>'ok')::boolean);
  j2 := public.assistant_spend_reserve(co, 'triage', 0.000001, 300, 0, 50, gen_random_uuid(), null, null);
  perform pg_temp.p8_check('reserve_triage_refused_exactly_at_the_cap', not (j2->>'ok')::boolean
    and j2->>'reason' = 'global_cap');
  cs0 := public.assistant_triage_company_spend();
  perform pg_temp.p8_check('company_spend_counts_live_triage_holds', (cs0->>'held')::numeric >= v_head - 0.000001);
  update public.assistant_spend_reservations set status = 'released', settled_at = now(), actual_usd = 0
   where id = (j->>'id')::uuid;
  insert into public.assistant_spend_reservations (user_id, kind, amount_usd, expires_at)
  values (u1, 'browser_task', v_head, now() + interval '1 hour');
  j2 := public.assistant_spend_reserve(co, 'triage', 0.000001, 300, 0, 50, gen_random_uuid(), null, null);
  perform pg_temp.p8_check('reserve_triage_refused_by_a_browser_hold', not (j2->>'ok')::boolean
    and j2->>'reason' = 'global_cap');
  update public.assistant_settings set global_daily_cap_usd = 10000 where id = 1;
  st0 := public.assistant_spend_state(u1, 15, 50);
  cs0 := public.assistant_triage_company_spend();
  insert into public.llm_call_log (feature, provider, model, tier, ok, user_id, assistant_run_id, cost_usd)
  values ('assistant_triage', 'openai', 'gpt-6-luna', 'pipeline', true, co, gen_random_uuid(), 0.010000);
  st1 := public.assistant_spend_state(u1, 15, 50);
  cs1 := public.assistant_triage_company_spend();
  perform pg_temp.p8_check('company_call_counts_globally_only',
    (st1->>'global_spent')::numeric = (st0->>'global_spent')::numeric + 0.01
    and (st1->>'user_spent')::numeric = (st0->>'user_spent')::numeric
    and (cs1->>'spent')::numeric = (cs0->>'spent')::numeric + 0.01);
  insert into public.llm_call_log (feature, provider, model, tier, ok, user_id, assistant_run_id, cost_usd)
  values ('assistant', 'openai', 'gpt-6-luna', 'interactive', true, u1, gen_random_uuid(), 0.500000);
  perform pg_temp.p8_check('company_spend_ignores_people',
    (public.assistant_triage_company_spend()->>'spent')::numeric = (cs1->>'spent')::numeric);

  -- 9. Mute on the new kind (B3), unchanged elsewhere.
  i1 := pg_temp.p8_inbox(u1, 'assistant.triage_escalation', clock_timestamp(), 'immediate');
  j := public.assistant_inbox_feedback(u1, i1, 'mute', clock_timestamp());
  perform pg_temp.p8_check('mute_new_kind', (j->>'ok')::boolean and exists (
    select 1 from public.assistant_subscriptions s where s.user_id = u1 and s.role is null
       and s.event_type = 'assistant.triage_escalation' and not s.enabled and s.source = 'user'));
  i2 := pg_temp.p8_inbox(u1, 'assistant.todo_nudge', clock_timestamp(), 'immediate');
  j := public.assistant_inbox_feedback(u1, i2, 'mute', clock_timestamp());
  perform pg_temp.p8_check('mute_other_assistant_kind_still_refused', j->>'code' = 'not_mutable');

  -- 10. Purge (B4): old finished runs go, legal hold and claimed runs stay.
  insert into public.assistant_profiles (user_id, legal_hold) values (u2, true)
  on conflict (user_id) do update set legal_hold = true;
  insert into public.assistant_triage_runs (id, user_id, status, claimed_by, claimed_at, lease_until, item_count, created_at)
  values (gen_random_uuid(), u1, 'decided', w, now() - interval '100 days', now() - interval '100 days', 1, now() - interval '100 days'),
         (gen_random_uuid(), u2, 'decided', w, now() - interval '100 days', now() - interval '100 days', 1, now() - interval '100 days'),
         (gen_random_uuid(), u3, 'claimed', w, now() - interval '100 days', now() - interval '100 days', 1, now() - interval '100 days');
  j := public.assistant_triage_purge(clock_timestamp(), 90, 100);
  perform pg_temp.p8_check('purge_skips_legal_hold_and_claimed', (j->>'locked')::boolean
    and not exists (select 1 from public.assistant_triage_runs where user_id = u1 and created_at < now() - interval '90 days')
    and exists (select 1 from public.assistant_triage_runs where user_id = u2 and created_at < now() - interval '90 days')
    and exists (select 1 from public.assistant_triage_runs where user_id = u3 and created_at < now() - interval '90 days'));

  -- 11. The company triage switch (B6, D-1): one statement flips it; a flip to on stamps the
  --     epoch from the real clock and clears the latch; no flip writes nothing. Each read-back
  --     is its own statement after the call.
  update public.assistant_settings
     set triage_enabled = true, triage_enabled_at = clock_timestamp(), triage_fuse_tripped_at = clock_timestamp()
   where id = 1;
  j := public.assistant_triage_set_enabled(u3, true);
  perform pg_temp.p8_check('switch_on_while_on_writes_nothing',
    not (j->>'changed')::boolean and (j->>'triage_enabled')::boolean and j->>'triage_fuse_tripped_at' is not null);
  j := public.assistant_triage_set_enabled(u3, false);
  perform pg_temp.p8_check('switch_off_keeps_the_epoch_and_the_latch', (j->>'changed')::boolean
    and not (j->>'triage_enabled')::boolean and j->>'triage_enabled_at' is not null
    and j->>'triage_fuse_tripped_at' is not null);
  v_x := clock_timestamp();
  j := public.assistant_triage_set_enabled(u3, true);
  perform pg_temp.p8_check('switch_on_stamps_the_real_clock_and_clears_the_latch', (j->>'changed')::boolean
    and (j->>'triage_enabled')::boolean and j->>'triage_fuse_tripped_at' is null
    and (j->>'triage_enabled_at')::timestamptz >= v_x and (j->>'triage_enabled_at')::timestamptz > now());
  perform pg_temp.p8_check('switch_row_matches_the_answer', exists (
    select 1 from public.assistant_settings s
     where s.id = 1 and s.triage_enabled and s.triage_fuse_tripped_at is null and s.updated_by = u3
       and s.triage_enabled_at = (j->>'triage_enabled_at')::timestamptz));
  v_ok := false;
  begin
    perform public.assistant_triage_set_enabled(co, false);
  exception when check_violation then v_ok := true;
  end;
  perform pg_temp.p8_check('switch_refuses_the_company_id', v_ok);

  select count(*) filter (where ok), count(*) filter (where not ok),
         coalesce(array_agg(label order by n) filter (where not ok), '{}')
    into v_pass, v_fail, v_fails from p8_results;
  raise exception 'TEST_RESULT % pass=% fail=% failures=%',
    case when v_fail = 0 then 'PASS' else 'FAIL' end, v_pass, v_fail, v_fails;
end $$;
rollback;
