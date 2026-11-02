-- DEV ONLY after 0191 (BDR project). Never on BDR_Prod. Synthetic fixtures, all rolled back.
-- Checks the 0191 bodies of assistant_inbox_fold (prior precedence kept on fold and
-- restored on un-fold) and assistant_inbox_link_bell (severity and the precedence
-- rule id from the payload).
-- Run as service_role; the DO block always ends in RAISE 'TEST_RESULT ...', so nothing
-- it wrote survives even before the outer rollback.
begin;
do $$
declare
  u uuid := gen_random_uuid();
  t0 timestamptz := '2026-10-30 13:00:00+00';   -- 06:00 PT, Friday
  am_date date := '2026-10-30';
  am_slot timestamptz := '2026-10-30 14:00:00+00'; -- 07:00 PT
  fold_until timestamptz := '2026-10-30 17:00:00+00'; -- 10:00 PT
  merged uuid := gen_random_uuid();
  plain uuid := gen_random_uuid();
  legacy uuid := gen_random_uuid();
  b1 uuid := gen_random_uuid();
  b2 uuid := gen_random_uuid();
  b3 uuid := gen_random_uuid();
  r public.assistant_inbox;
  v text;
  j jsonb;
  k text;
  n_pass int := 0;
  fails text[] := '{}';
begin
  -- 0. 0191 is the live body, and both functions stay service_role only.
  if position('folded_from_precedence' in (select p.prosrc from pg_proc p
       where p.oid = 'public.assistant_inbox_fold(uuid, uuid, date, timestamptz, timestamptz, timestamptz)'::regprocedure)) > 0
     and position('v_severity' in (select p.prosrc from pg_proc p
       where p.oid = 'public.assistant_inbox_link_bell(uuid, text, uuid, timestamptz, text, int, text, text, jsonb, uuid, timestamptz)'::regprocedure)) > 0
     and position('v_precedence' in (select p.prosrc from pg_proc p
       where p.oid = 'public.assistant_inbox_link_bell(uuid, text, uuid, timestamptz, text, int, text, text, jsonb, uuid, timestamptz)'::regprocedure)) > 0
  then n_pass := n_pass + 1; else fails := array_append(fails, '0191_not_applied'::text); end if;
  if not has_function_privilege('anon', 'public.assistant_inbox_fold(uuid, uuid, date, timestamptz, timestamptz, timestamptz)', 'EXECUTE')
     and not has_function_privilege('authenticated', 'public.assistant_inbox_fold(uuid, uuid, date, timestamptz, timestamptz, timestamptz)', 'EXECUTE')
     and has_function_privilege('service_role', 'public.assistant_inbox_fold(uuid, uuid, date, timestamptz, timestamptz, timestamptz)', 'EXECUTE')
     and not has_function_privilege('anon', 'public.assistant_inbox_link_bell(uuid, text, uuid, timestamptz, text, int, text, text, jsonb, uuid, timestamptz)', 'EXECUTE')
     and not has_function_privilege('authenticated', 'public.assistant_inbox_link_bell(uuid, text, uuid, timestamptz, text, int, text, text, jsonb, uuid, timestamptz)', 'EXECUTE')
     and has_function_privilege('service_role', 'public.assistant_inbox_link_bell(uuid, text, uuid, timestamptz, text, int, text, text, jsonb, uuid, timestamptz)', 'EXECUTE')
  then n_pass := n_pass + 1; else fails := array_append(fails, 'grants'::text); end if;

  insert into auth.users(id, email) values (u, 'phase7-0191-' || u::text || '@example.invalid');
  insert into public.profiles(id, full_name, email, role, is_active)
  values (u, 'Phase 7 0191 rollback fixture', 'phase7-0191-' || u::text || '@example.invalid', 'it_admin', true)
  on conflict (id) do update set role = 'it_admin', is_active = true;

  -- Three held high rows (created 02:00 PT, ringing 06:00 PT): merged by R17, plain, and
  -- one already folded under 0190 (no folded_from_precedence key).
  insert into public.assistant_inbox
    (id, user_id, dedupe_key, severity, title, why, payload, delivery, precedence, deliver_after, event_type, created_at)
  values
    (merged, u, 'p7:todo.due:0191-merged', 'high', 'Fixture merged', 'R17', '{"todo_id":"t1"}'::jsonb,
     'immediate', 'R17', t0, 'todo.due', t0 - interval '4 hours'),
    (plain, u, 'p7:todo.due:0191-plain', 'high', 'Fixture plain', null, '{"todo_id":"t2"}'::jsonb,
     'immediate', null, t0, 'todo.due', t0 - interval '4 hours'),
    (legacy, u, 'p7:todo.due:0191-legacy', 'high', 'Fixture legacy', null, '{"todo_id":"t3"}'::jsonb,
     'digest', 'quiet_hours', am_slot, 'todo.due', t0 - interval '4 hours');

  -- 1. Fold: quiet_hours while folded, the prior precedence parked in the payload.
  v := public.assistant_inbox_fold(u, merged, am_date, am_slot, fold_until, t0);
  select * into r from public.assistant_inbox where id = merged;
  if v = 'folded' and r.delivery = 'digest' and r.precedence = 'quiet_hours' and r.deliver_after = am_slot
     and r.payload->>'folded_from_precedence' = 'R17' and r.payload->>'todo_id' = 't1'
  then n_pass := n_pass + 1; else fails := array_append(fails, ('fold_merged:' || coalesce(v, 'null'))::text); end if;

  v := public.assistant_inbox_fold(u, plain, am_date, am_slot, fold_until, t0);
  select * into r from public.assistant_inbox where id = plain;
  if v = 'folded' and r.precedence = 'quiet_hours' and r.payload ? 'folded_from_precedence'
     and jsonb_typeof(r.payload->'folded_from_precedence') = 'null'
  then n_pass := n_pass + 1; else fails := array_append(fails, ('fold_plain:' || coalesce(v, 'null'))::text); end if;

  -- 2. Before the window closes the folded row waits and is unchanged.
  v := public.assistant_inbox_fold(u, merged, am_date, am_slot, fold_until, t0 + interval '30 minutes');
  select * into r from public.assistant_inbox where id = merged;
  if v = 'wait' and r.precedence = 'quiet_hours' and r.payload ? 'folded_from_precedence'
  then n_pass := n_pass + 1; else fails := array_append(fails, ('wait:' || coalesce(v, 'null'))::text); end if;

  -- 3. Un-fold after 10:00 PT with no AM digest: the rule comes back, the key goes.
  v := public.assistant_inbox_fold(u, merged, am_date, am_slot, fold_until, fold_until + interval '1 minute');
  select * into r from public.assistant_inbox where id = merged;
  if v = 'deliver' and r.delivery = 'immediate' and r.precedence = 'R17'
     and r.deliver_after = fold_until + interval '1 minute'
     and not (r.payload ? 'folded_from_precedence') and r.payload->>'todo_id' = 't1'
  then n_pass := n_pass + 1; else fails := array_append(fails, ('unfold_merged:' || coalesce(v, 'null') || ':' || coalesce(r.precedence, 'null'))::text); end if;

  v := public.assistant_inbox_fold(u, plain, am_date, am_slot, fold_until, fold_until + interval '1 minute');
  select * into r from public.assistant_inbox where id = plain;
  if v = 'deliver' and r.delivery = 'immediate' and r.precedence is null and not (r.payload ? 'folded_from_precedence')
  then n_pass := n_pass + 1; else fails := array_append(fails, ('unfold_plain:' || coalesce(v, 'null') || ':' || coalesce(r.precedence, 'null'))::text); end if;

  -- 4. A row folded under 0190 (no key) un-folds exactly as 0190 did.
  v := public.assistant_inbox_fold(u, legacy, am_date, am_slot, fold_until, fold_until + interval '1 minute');
  select * into r from public.assistant_inbox where id = legacy;
  if v = 'deliver' and r.delivery = 'immediate' and r.precedence = 'quiet_hours' and r.payload = '{"todo_id":"t3"}'::jsonb
  then n_pass := n_pass + 1; else fails := array_append(fails, ('unfold_legacy:' || coalesce(v, 'null'))::text); end if;

  -- 5. The un-folded merged row delivers and keeps R17. The no-Teams decision is made
  --    in Python from that precedence (NO_TEAMS_RULES, covered by
  --    test_unfolded_row_keeps_its_merge_rule_for_the_teams_decision), not by this RPC,
  --    so this check proves only the restored precedence.
  j := public.assistant_inbox_deliver(u, merged, 'Fixture merged', '{}'::jsonb, false, null, fold_until + interval '2 minutes');
  select * into r from public.assistant_inbox where id = merged;
  if (j->>'delivered')::boolean and r.precedence = 'R17'
  then n_pass := n_pass + 1; else fails := array_append(fails, 'deliver_merged'::text); end if;

  -- 6. Link-bell severity and precedence rule id from the payload, both kept out of the
  --    stored payload.
  k := 'p7:bell:email:0191-' || u::text;
  j := public.assistant_inbox_link_bell(u, k, b1, t0, 'bell.quote_received', 1, 'Quote', null,
                                        jsonb_build_object('notification_id', b1, 'severity', 'low',
                                                           'precedence', 'R13'), null, t0);
  select * into r from public.assistant_inbox where user_id = u and dedupe_key = k;
  if (j->>'linked')::boolean = false and r.delivery = 'record' and r.severity = 'low' and r.precedence = 'R13'
     and not (r.payload ? 'severity') and not (r.payload ? 'precedence') and r.payload->>'rank' = '1'
  then n_pass := n_pass + 1; else fails := array_append(fails, ('link_insert:' || coalesce(r.severity, 'null') || ':' || coalesce(r.precedence, 'null'))::text); end if;

  -- A higher-rank bell becomes the source and brings its severity and precedence.
  j := public.assistant_inbox_link_bell(u, k, b2, t0 + interval '30 seconds', 'bell.late_quote', 2, 'Late quote', null,
                                        jsonb_build_object('notification_id', b2, 'severity', 'high',
                                                           'precedence', 'R4b'), null, t0);
  select * into r from public.assistant_inbox where user_id = u and dedupe_key = k;
  if (j->>'source')::boolean and r.severity = 'high' and r.precedence = 'R4b' and r.source_notification_id = b2
     and not (r.payload ? 'severity') and not (r.payload ? 'precedence')
  then n_pass := n_pass + 1; else fails := array_append(fails, ('link_source:' || coalesce(r.severity, 'null') || ':' || coalesce(r.precedence, 'null'))::text); end if;

  -- A lower-rank bell is only linked and leaves the severity and precedence alone.
  j := public.assistant_inbox_link_bell(u, k, b3, t0 + interval '40 seconds', 'bell.quote_received', 1, 'Quote', null,
                                        jsonb_build_object('notification_id', b3, 'severity', 'urgent',
                                                           'precedence', 'R12'), null, t0);
  select * into r from public.assistant_inbox where user_id = u and dedupe_key = k;
  if (j->>'source')::boolean = false and r.severity = 'high' and r.precedence = 'R4b'
     and r.payload->'linked_notification_ids' ? b3::text and not (r.payload ? 'precedence')
  then n_pass := n_pass + 1; else fails := array_append(fails, ('link_linked:' || coalesce(r.severity, 'null') || ':' || coalesce(r.precedence, 'null'))::text); end if;

  -- A bad or missing severity is normal (the 0190 default); a bad precedence is null.
  j := public.assistant_inbox_link_bell(u, k || ':bad', gen_random_uuid(), t0, 'bell.quote_received', 1, 'Quote', null,
                                        '{"severity":"severe","precedence":"quiet_hours"}'::jsonb, null, t0);
  select * into r from public.assistant_inbox where user_id = u and dedupe_key = k || ':bad';
  if r.severity = 'normal' and r.precedence is null and not (r.payload ? 'severity') and not (r.payload ? 'precedence')
  then n_pass := n_pass + 1; else fails := array_append(fails, ('link_bad:' || coalesce(r.severity, 'null') || ':' || coalesce(r.precedence, 'null'))::text); end if;

  j := public.assistant_inbox_link_bell(u, k || ':long', gen_random_uuid(), t0, 'bell.quote_received', 1, 'Quote', null,
                                        jsonb_build_object('precedence', 'R' || repeat('1', 20)), null, t0);
  select * into r from public.assistant_inbox where user_id = u and dedupe_key = k || ':long';
  if r.precedence is null and not (r.payload ? 'precedence')
  then n_pass := n_pass + 1; else fails := array_append(fails, ('link_long:' || coalesce(r.precedence, 'null'))::text); end if;

  j := public.assistant_inbox_link_bell(u, k || ':num', gen_random_uuid(), t0, 'bell.quote_received', 1, 'Quote', null,
                                        '{"precedence":13}'::jsonb, null, t0);
  select * into r from public.assistant_inbox where user_id = u and dedupe_key = k || ':num';
  if r.precedence is null and not (r.payload ? 'precedence')
  then n_pass := n_pass + 1; else fails := array_append(fails, ('link_num:' || coalesce(r.precedence, 'null'))::text); end if;

  j := public.assistant_inbox_link_bell(u, k || ':none', gen_random_uuid(), t0, 'bell.quote_received', 1, 'Quote', null,
                                        null, null, t0);
  select * into r from public.assistant_inbox where user_id = u and dedupe_key = k || ':none';
  if r.severity = 'normal' and r.precedence is null and r.payload->>'rank' = '1'
  then n_pass := n_pass + 1; else fails := array_append(fails, ('link_none:' || coalesce(r.severity, 'null'))::text); end if;

  raise exception 'TEST_RESULT % pass=% fail=% failures=%',
    case when cardinality(fails) = 0 then 'PASS' else 'FAIL' end, n_pass, cardinality(fails), fails;
end $$;
rollback;
