-- DEV ONLY (BDR bpidntbyvoooqvaispup). Migration 0192 SQL check
-- (docs/ASSISTANT_BROWSER_V2_PLAN.md 14.4 and 18.3). Run after every 0192 piece
-- (a1 to a5 and b) is applied. Everything happens inside one transaction that
-- always rolls back: the DO block ends by raising
--   TEST_RESULT PASS pass=N fail=0 failures={}
-- (or TEST_RESULT FAIL ...), so the result comes back as the error text and no
-- fixture row survives. No model, site, worker or notification call happens.
-- Requires an idle personal browser queue (no queued, running or waiting
-- user_browser job on dev).
begin;
create temp table v2_check(label text not null, ok boolean not null) on commit drop;
create function pg_temp.v2ck(p_label text, p_ok boolean) returns void language sql
 as $f$ insert into v2_check values (p_label, coalesce(p_ok, false)) $f$;
do $$
declare
 u uuid := gen_random_uuid(); conv uuid := gen_random_uuid(); w uuid := gen_random_uuid(); b1 uuid := gen_random_uuid();
 b2 uuid := gen_random_uuid(); cid uuid := gen_random_uuid(); env jsonb; v jsonb; n int; i int;
 adm1 uuid; adm2 uuid; adm3 uuid; adm4 uuid; adm5 uuid; adm6 uuid; adm7 uuid; adm8 uuid; adm9 uuid; adm10 uuid; adm11 uuid;
 j1 uuid; j2 uuid; j3 uuid; j4 uuid; j5 uuid; j6 uuid; j7 uuid; j8 uuid; j9 uuid; j10 uuid; conv2 uuid := gen_random_uuid();
 h4 uuid := gen_random_uuid();
 e bigint; turn_id uuid; turn_token uuid; h1 uuid := gen_random_uuid(); h2 uuid := gen_random_uuid(); h3 uuid := gen_random_uuid();
 help uuid; attempt uuid; g_public uuid; g_session uuid; req uuid; cred jsonb; jr public.browser_agent_jobs; fails text[]; n_pass int;
begin
 if exists(select 1 from public.browser_agent_jobs where agent_type = 'user_browser' and status in ('queued','running','waiting_help')) then
  raise exception 'check requires an idle user_browser queue';
 end if;
 env := '{"wrapped_dek":"fake","ciphertext":"fake","nonce":"fake","key_id":"fixture"}'::jsonb;

 -- Schema facts.
 perform pg_temp.v2ck('budget_default_15', (select column_default from information_schema.columns where table_schema = 'public'
  and table_name = 'assistant_profiles' and column_name = 'daily_cost_cap_usd') = '15');
 perform pg_temp.v2ck('type_row', exists(select 1 from public.browser_agent_types where key = 'user_browser' and provider = 'openai'
  and max_steps = 150 and max_total_tokens = 2000000 and max_active_seconds = 1800 and principal_kind = 'user'));
 perform pg_temp.v2ck('retired_label', exists(select 1 from public.browser_agent_types where key = 'user_browser_read'
  and label = 'Retired personal browser read' and not enabled));
 perform pg_temp.v2ck('help_reasons_widened', exists(select 1 from pg_constraint where conname = 'browser_agent_help_requests_reason_v2'
  and pg_get_constraintdef(oid) like '%mfa_step_up%' and pg_get_constraintdef(oid) like '%needs_credential%'));
 perform pg_temp.v2ck('operator_mode', exists(select 1 from pg_constraint where conname = 'browser_agent_jobs_control_mode_v2'));
 perform pg_temp.v2ck('service_check_gone', not exists(select 1 from pg_constraint where conrelid = 'public.assistant_credentials'::regclass
  and contype = 'c' and conname <> 'assistant_credentials_site_account_v2' and pg_get_constraintdef(oid) like '%service%'
  and pg_get_constraintdef(oid) like '%procore%'));
 -- The live v1 upserts (0189 credential_put, 0186 session_put) still need their targets.
 perform pg_temp.v2ck('v1_upsert_targets_kept', exists(select 1 from pg_constraint where conrelid = 'public.assistant_credentials'::regclass
  and pg_get_constraintdef(oid) = 'UNIQUE (user_id, service)') and exists(select 1 from pg_constraint
  where conrelid = 'public.assistant_browser_sessions'::regclass and pg_get_constraintdef(oid) = 'UNIQUE (credential_id, origin)'));
 perform pg_temp.v2ck('anon_closed', not exists(select 1 from pg_proc p join pg_namespace s on s.oid = p.pronamespace
  where s.nspname = 'public' and p.proname like 'assistant_browser_%'
   and (has_function_privilege('anon', p.oid, 'EXECUTE') or has_function_privilege('authenticated', p.oid, 'EXECUTE'))));

 -- Fixtures (rolled back).
 insert into auth.users(id, email) values(u, 'browser-v2-' || u::text || '@example.invalid');
 insert into public.profiles(id, full_name, email, role, is_active) values(u, 'Browser v2 rollback fixture',
  'browser-v2-' || u::text || '@example.invalid', 'it_admin', true) on conflict(id) do update set role = 'it_admin', is_active = true;
 update public.assistant_settings set enabled = true, global_daily_cap_usd = 1000 where id = 1;
 insert into public.assistant_profiles(user_id, daily_cost_cap_usd) values(u, 5)
  on conflict(user_id) do update set daily_cost_cap_usd = 5;
 insert into public.assistant_delegations(user_id, created_session_aal) values(u, 'aal2');
 insert into public.assistant_conversations(id, user_id, mode) values(conv, u, 'normal');
 update public.browser_agent_settings set enabled = true, daily_cost_cap_usd = 100, user_per_owner_concurrency = 8,
  user_per_owner_queued = 20 where id = 1;
 update public.browser_agent_types set enabled = true, model = 'fixture', price_in_per_mtok = 1, price_out_per_mtok = 1,
  model_probe_ok_at = now(), max_concurrency = 1 where key = 'user_browser';
 insert into public.assistant_browser_sites(site_key, label, kind, registrable_domains, login_kind)
  values('zz_public_fixture', 'Public fixture', 'curated', array['example.org'], 'none');
 insert into public.assistant_browser_sites(site_key, label, kind, registrable_domains, login_origins, login_kind, cookie_domains)
  values('zz_session_fixture', 'Session fixture', 'curated', array['example.net'], array['https://login.example.net'], 'password',
   array['.example.net']);
 insert into public.browser_agent_workers(id, name, status, principal_kind, boot_id, allowed_types, capabilities, max_slots)
  values(w, 'browser-v2-' || w::text, 'active', 'user', b1, array['user_browser'], '{"types":{"user_browser":"1.1.0"}}', 2);

 -- Admission states and enqueue.
 v := public.assistant_browser_admission_create(u, jsonb_build_object('conversation_id', conv, 'goal', 'Read the fixture page',
  'start_site_key', 'zz_public_fixture', 'start_url', 'https://example.org/a', 'start_provenance', 'typed', 'fresh_user_message', true));
 adm1 := (v#>>'{admission,id}')::uuid;
 perform pg_temp.v2ck('admission_waiting_grant', v#>>'{admission,status}' = 'waiting_grant' and v->'needs' ? 'site_access');
 v := public.assistant_browser_task_enqueue_v2(u, adm1, '{}'::jsonb, 5, 1000);
 perform pg_temp.v2ck('enqueue_refused_without_grant', v->>'code' = 'needs');
 v := public.assistant_browser_site_grant(u, 'zz_public_fixture', 'domain', 'example.org', null, 'chat_card', 'typed');
 g_public := (v#>>'{grant,id}')::uuid;
 perform pg_temp.v2ck('grant_lists_waiting_admission', v->'waiting_admissions' ? adm1::text);
 v := public.assistant_browser_task_enqueue_v2(u, adm1, '{}'::jsonb, 5, 1000);
 j1 := (v#>>'{job,id}')::uuid;
 perform pg_temp.v2ck('enqueue_no_credential_default_caps', v->>'created' = 'true'
  and (select user_credential_id is null and assistant_admission_id = adm1 from public.browser_agent_jobs where id = j1)
  and not exists(select 1 from public.assistant_browser_job_credentials where job_id = j1));
 perform pg_temp.v2ck('first_step_1_usd_one_expiry', exists(select 1 from public.browser_agent_jobs j join public.assistant_spend_reservations r
  on r.id = j.assistant_reservation_id where j.id = j1 and r.amount_usd = 1 and r.expires_at = j.expires_at and r.task_id = adm1));
 v := public.assistant_spend_reserve(u, 'turn_call', 0.5, 300, 5, 1000, null, null, null);
 perform pg_temp.v2ck('chat_turn_still_fits', v->>'ok' = 'true');
 v := public.assistant_browser_admission_create(u, jsonb_build_object('conversation_id', conv, 'goal', 'Second task',
  'start_site_key', 'zz_public_fixture', 'fresh_user_message', true));
 adm2 := (v#>>'{admission,id}')::uuid;
 v := public.assistant_browser_task_enqueue_v2(u, adm2, '{}'::jsonb, 5, 1000); j2 := (v#>>'{job,id}')::uuid;
 perform pg_temp.v2ck('two_tasks_hold_2_usd', (select sum(amount_usd) from public.assistant_spend_reservations where user_id = u
  and kind = 'browser_task' and status = 'active') = 2);
 v := public.assistant_browser_admission_create(u, jsonb_build_object('conversation_id', conv, 'goal', 'Tainted start',
  'start_site_key', 'zz_public_fixture', 'tainted', true, 'fresh_user_message', true));
 adm3 := (v#>>'{admission,id}')::uuid;
 perform pg_temp.v2ck('admission_waiting_start', v#>>'{admission,status}' = 'waiting_start');
 v := public.assistant_browser_task_enqueue_v2(u, adm3, '{}'::jsonb, 5, 1000);
 perform pg_temp.v2ck('start_card_blocks_enqueue', v->>'code' = 'needs' and v->'needs' ? 'task_start');
 v := public.assistant_browser_start_decide(u, adm3, 'start');
 perform pg_temp.v2ck('start_decide_ready', v#>>'{admission,status}' = 'ready');
 v := public.assistant_browser_task_enqueue_v2(u, adm3, '{}'::jsonb, 5, 1000); j3 := (v#>>'{job,id}')::uuid;
 v := public.assistant_browser_admission_create(u, jsonb_build_object('conversation_id', conv, 'goal', 'Email link',
  'start_site_key', 'zz_public_fixture', 'start_url', 'https://example.org/invite', 'start_provenance', 'email', 'fresh_user_message', true));
 adm4 := (v#>>'{admission,id}')::uuid;
 perform pg_temp.v2ck('email_link_card_on_approved_site', v#>>'{admission,status}' = 'waiting_link');
 v := public.assistant_browser_request_create(u, jsonb_build_object('admission_id', adm4, 'conversation_id', conv, 'kind', 'link_open',
  'site_key', 'zz_public_fixture', 'url', 'https://example.org/invite', 'reason_code', 'link_from_email', 'provenance', 'email'));
 req := (v#>>'{request,id}')::uuid;
 v := public.assistant_browser_request_decide(u, req, 'approve');
 perform pg_temp.v2ck('link_approved_ready', (select status from public.assistant_browser_admissions where id = adm4) = 'ready');
 v := public.assistant_browser_admission_create(u, jsonb_build_object('conversation_id', conv, 'goal', 'Fourth',
  'start_site_key', 'zz_public_fixture', 'fresh_user_message', true));
 adm6 := (v#>>'{admission,id}')::uuid;
 v := public.assistant_browser_task_enqueue_v2(u, adm6, '{}'::jsonb, 5, 1000); j4 := (v#>>'{job,id}')::uuid;
 v := public.assistant_browser_admission_create(u, jsonb_build_object('conversation_id', conv, 'goal', 'Read Procore',
  'start_site_key', 'procore', 'start_url', 'https://app.procore.com/1/project/home', 'fresh_user_message', true));
 adm5 := (v#>>'{admission,id}')::uuid;
 perform 1 from public.assistant_browser_site_grant(u, 'procore', 'domain', 'procore.com', null, 'chat_card', 'typed');
 v := public.assistant_browser_task_enqueue_v2(u, adm5, '{}'::jsonb, 5, 1000);
 perform pg_temp.v2ck('admission_waiting_credential', v->'needs' ? 'credential'
  and (select status from public.assistant_browser_admissions where id = adm5) = 'waiting_credential');

 -- Claims: type limit, per-owner limit, worker slots (two leases on one worker).
 select count(*) into n from public.assistant_browser_claim_user(w, b1, '{"user_browser":3}'::jsonb, 90);
 perform pg_temp.v2ck('type_limit', n = 1 and (select lease_owner = w from public.browser_agent_jobs where id = j1));
 update public.browser_agent_types set max_concurrency = 4 where key = 'user_browser';
 update public.browser_agent_settings set user_per_owner_concurrency = 1 where id = 1;
 select count(*) into n from public.assistant_browser_claim_user(w, b1, '{"user_browser":3}'::jsonb, 90);
 perform pg_temp.v2ck('per_owner_limit', n = 0);
 update public.browser_agent_settings set user_per_owner_concurrency = 8 where id = 1;
 select count(*) into n from public.assistant_browser_claim_user(w, b1, '{"user_browser":3}'::jsonb, 90);
 perform pg_temp.v2ck('two_leases_one_worker_slot_limit', n = 1 and (select count(*) from public.browser_agent_jobs where lease_owner = w) = 2
  and (select lease_owner = w from public.browser_agent_jobs where id = j2)
  and (select config_snapshot->>'per_job_cost_cap_usd' from public.browser_agent_jobs where id = j2)::numeric = 5);

 -- Turns past 2.01 USD and 41 turns at the user task cap, then cap refusals.
 update public.assistant_profiles set daily_cost_cap_usd = 15 where user_id = u;
 select lease_epoch into e from public.browser_agent_jobs where id = j1;
 for i in 1..41 loop
  v := public.assistant_browser_turn_begin_v2(u, j1, w, b1, e, i, 0.05, 1000, 15, 1000);
  if v->>'claimed' is distinct from 'true' then exit; end if;
  perform public.assistant_browser_turn_finish(u, j1, w, b1, e, (v->>'turn_id')::uuid, (v->>'token')::uuid, 0.05, env, env, 1000);
 end loop;
 perform pg_temp.v2ck('past_2_usd_and_41_turns', (select turn_count = 41 and cost_usd = 2.05 from public.browser_agent_jobs where id = j1)
  and (select amount_usd from public.assistant_spend_reservations r join public.browser_agent_jobs j on j.assistant_reservation_id = r.id
   where j.id = j1) = 3);
 update public.assistant_profiles set daily_cost_cap_usd = 5 where user_id = u;
 v := public.assistant_browser_turn_begin_v2(u, j1, w, b1, e, 42, 1.0, 1000, 15, 1000);
 perform pg_temp.v2ck('topup_past_user_cap', v->>'code' = 'cost_cap' and v->>'reason' = 'user_cap');
 update public.assistant_profiles set daily_cost_cap_usd = 15 where user_id = u;
 v := public.assistant_browser_turn_begin_v2(u, j1, w, b1, e, 42, 3.0, 1000, 15, 1000);
 perform pg_temp.v2ck('task_cap_5_usd', v->>'code' = 'cost_cap' and v->>'reason' = 'task_cap');

 -- Holds on j2: open, waiting job keeps its slot, decide, consume once.
 select lease_epoch into e from public.browser_agent_jobs where id = j2;
 v := public.assistant_browser_hold_open(j2, w, b1, e, h1, 'final_action', jsonb_build_object('method', 'POST',
  'origin', 'https://example.org', 'url_sha256', repeat('a', 64), 'body_sha256', repeat('b', 64), 'label', 'Submit'),
  jsonb_build_object('id', gen_random_uuid(), 'kind', 'request', 'seq', 0, 'envelope', env),
  jsonb_build_object('id', gen_random_uuid(), 'kind', 'screenshot', 'seq', 1, 'envelope', env));
 perform pg_temp.v2ck('hold_open', v->>'created' = 'true' and (select status from public.browser_agent_jobs where id = j2) = 'waiting_help'
  and exists(select 1 from public.assistant_browser_chat_outbox where job_id = j2 and event_key = 'hold:' || h1::text));
 perform pg_temp.v2ck('hold_token_fields', exists(select 1 from public.assistant_browser_holds where id = h1 and lease_epoch = e
  and method = 'POST' and origin = 'https://example.org' and url_sha256 = repeat('a', 64) and body_sha256 = repeat('b', 64)));
 select count(*) into n from public.assistant_browser_claim_user(w, b1, '{"user_browser":3}'::jsonb, 90);
 perform pg_temp.v2ck('waiting_job_counts_toward_slots', n = 0);
 v := public.assistant_browser_hold_release_get(j2, w, b1, e, h1);
 perform pg_temp.v2ck('release_pending', v->>'status' = 'pending' and v->>'token' is null);
 v := public.assistant_browser_hold_decide(u, h1, 'approve', repeat('t', 40), now() + interval '2 minutes');
 v := public.assistant_browser_hold_release_get(j2, w, b1, e, h1);
 perform pg_temp.v2ck('release_token_after_approve', v->>'status' = 'approved' and v->>'token' = repeat('t', 40));
 v := public.assistant_browser_hold_consume(j2, w, b1, e, h1);
 perform pg_temp.v2ck('consume_once', v->>'ok' = 'true'
  and (select side_effects_at is not null and status = 'running' from public.browser_agent_jobs where id = j2));
 v := public.assistant_browser_hold_consume(j2, w, b1, e, h1);
 perform pg_temp.v2ck('consume_twice_refused', v->>'code' = 'already_consumed');
 v := public.assistant_browser_hold_open(j2, w, b1, e, h2, 'final_action', jsonb_build_object('method', 'POST',
  'origin', 'https://example.org', 'url_sha256', repeat('c', 64), 'body_sha256', repeat('d', 64)),
  jsonb_build_object('id', gen_random_uuid(), 'kind', 'request', 'seq', 2, 'envelope', env), null);
 v := public.assistant_browser_job_requeue(j2, w, b1, e, null, 'drain');
 perform pg_temp.v2ck('requeue_after_side_effects', (select status = 'failed' and error_kind = 'interrupted_after_action'
  from public.browser_agent_jobs where id = j2) and (select status from public.assistant_browser_holds where id = h2) = 'aborted'
  and exists(select 1 from public.assistant_browser_chat_outbox where job_id = j2 and event_key = 'terminal'));
 v := public.assistant_browser_hold_decide(u, h2, 'approve', repeat('t', 40), now() + interval '2 minutes');
 perform pg_temp.v2ck('decide_refused_on_aborted', v->>'code' = 'hold_aborted');
 perform pg_temp.v2ck('settled_to_actual', exists(select 1 from public.assistant_spend_reservations r join public.browser_agent_jobs j
  on j.assistant_reservation_id = r.id where j.id = j2 and r.status = 'released' and r.actual_usd = j.cost_usd));

 -- An expired park frees the slot for a queued job.
 select count(*) into n from public.assistant_browser_claim_user(w, b1, '{"user_browser":3}'::jsonb, 90);
 select lease_epoch into e from public.browser_agent_jobs where id = j3;
 v := public.assistant_browser_hold_open(j3, w, b1, e, h3, 'final_action', jsonb_build_object('method', 'POST',
  'origin', 'https://example.org', 'url_sha256', repeat('e', 64), 'body_sha256', repeat('f', 64)),
  jsonb_build_object('id', gen_random_uuid(), 'kind', 'request', 'seq', 0, 'envelope', env), null);
 update public.assistant_browser_holds set expires_at = now() - interval '1 second' where id = h3;
 -- The worker's release poll sees the expiry first: the job still parks.
 v := public.assistant_browser_hold_release_get(j3, w, b1, e, h3);
 perform pg_temp.v2ck('release_get_parks_expired_hold', v->>'status' = 'expired'
  and (select expires_at > now() from public.assistant_browser_help_state where hold_id = h3 and status = 'open')
  and exists(select 1 from public.assistant_browser_chat_outbox where job_id = j3 and event_key = 'hold_expired:' || h3::text));
 perform public.assistant_browser_sweep(100);
 perform public.assistant_browser_sweep_v2(100);
 perform pg_temp.v2ck('parked_job_keeps_waiting', (select status from public.browser_agent_jobs where id = j3) = 'waiting_help');
 update public.assistant_browser_help_state set expires_at = now() - interval '1 second' where hold_id = h3;
 -- The 0187 sweep leaves an expired v2 help row to sweep_v2.
 perform public.assistant_browser_sweep(100);
 perform pg_temp.v2ck('v1_sweep_leaves_v2_help', exists(select 1 from public.assistant_browser_help_state where hold_id = h3
  and status = 'open'));
 perform public.assistant_browser_sweep_v2(100);
 select count(*) into n from public.assistant_browser_claim_user(w, b1, '{"user_browser":3}'::jsonb, 90);
 perform pg_temp.v2ck('park_expiry_frees_slot', (select status = 'failed' and error_kind = 'waiting_timeout' from public.browser_agent_jobs
  where id = j3) and n = 1 and (select lease_owner = w from public.browser_agent_jobs where id = j4));

 -- Sweep expiry writes the outbox.
 v := public.assistant_browser_admission_create(u, jsonb_build_object('conversation_id', conv, 'goal', 'Will expire',
  'start_site_key', 'zz_public_fixture', 'fresh_user_message', true));
 adm7 := (v#>>'{admission,id}')::uuid;
 v := public.assistant_browser_task_enqueue_v2(u, adm7, '{}'::jsonb, 15, 1000); j5 := (v#>>'{job,id}')::uuid;
 update public.browser_agent_jobs set expires_at = now() - interval '1 second' where id = j5;
 perform public.assistant_browser_sweep_v2(100);
 perform pg_temp.v2ck('sweep_expiry_outbox', (select status from public.browser_agent_jobs where id = j5) = 'expired'
  and exists(select 1 from public.assistant_browser_chat_outbox where job_id = j5 and event_key = 'terminal'));

 -- Credentials, login, help channel.
 select lease_epoch into e from public.browser_agent_jobs where id = j1;
 perform public.assistant_browser_job_finish(j1, w, b1, e, 'succeeded', null, null);
 perform pg_temp.v2ck('settlement_succeeded', exists(select 1 from public.assistant_spend_reservations r join public.browser_agent_jobs j
  on j.assistant_reservation_id = r.id where j.id = j1 and r.status = 'settled' and r.actual_usd = j.cost_usd));
 cred := jsonb_build_object('id', cid, 'username', 'fixture@example.invalid', 'wrapped_dek', 'fake', 'ciphertext', 'fake',
  'nonce', 'fake', 'key_id', 'fixture', 'fingerprint', 'fixture');
 v := public.assistant_browser_credential_put_v2(u, 'procore', 'default', 0, cred, null);
 perform pg_temp.v2ck('credential_put_v2', v->>'ok' = 'true' and v->'waiting_admissions' ? adm5::text
  and (select login_origins = array['https://login.procore.com'] and is_default from public.assistant_credentials where id = cid));
 v := public.assistant_browser_credential_put_v2(u, 'procore', 'other', 0, cred || jsonb_build_object('id', gen_random_uuid()), null);
 perform pg_temp.v2ck('second_account_same_site', v->>'ok' = 'true' and v#>>'{credential,is_default}' = 'false');
 v := public.assistant_browser_task_enqueue_v2(u, adm5, '{}'::jsonb, 15, 1000); j6 := (v#>>'{job,id}')::uuid;
 perform pg_temp.v2ck('job_credential_pinned', exists(select 1 from public.assistant_browser_job_credentials where job_id = j6
  and credential_id = cid and credential_version = 1));
 perform 1 from public.assistant_browser_claim_user(w, b1, '{"user_browser":3}'::jsonb, 90);
 select lease_epoch into e from public.browser_agent_jobs where id = j6;
 v := public.assistant_browser_login_begin_v2(j6, w, b1, e, cid, 'https://evil.example');
 perform pg_temp.v2ck('login_origin_refused', v->>'code' = 'origin_not_allowed');
 v := public.assistant_browser_login_begin_v2(j6, w, b1, e, cid, 'https://login.microsoftonline.com');
 perform pg_temp.v2ck('login_idp_refused', v->>'code' = 'identity_provider');
 v := public.assistant_browser_login_begin_v2(j6, w, b1, e, gen_random_uuid(), 'https://login.procore.com');
 perform pg_temp.v2ck('credential_not_in_job', v->>'code' = 'not_in_job');
 for i in 1..3 loop
  update public.assistant_credentials set last_login_attempt_at = null where id = cid;
  v := public.assistant_browser_login_begin_v2(j6, w, b1, e, cid, 'https://login.procore.com');
  perform public.assistant_browser_login_finish_v2(j6, w, b1, e, cid, (v->>'attempt_id')::uuid, 'unexpected', null);
 end loop;
 perform pg_temp.v2ck('unexpected_counts_daily', (select attempts_day_count from public.assistant_credentials where id = cid) = 3);
 help := gen_random_uuid();
 v := public.assistant_browser_help_open_v2(j6, w, b1, e, help, jsonb_build_object('id', gen_random_uuid(), 'kind', 'help_request',
  'seq', 0, 'envelope', env), 'needs_credential', null, null);
 v := public.assistant_browser_credential_put_v2(u, 'procore', 'default', 1, cred, null);
 perform pg_temp.v2ck('save_moves_waiting_job', v->'woken_jobs' ? j6::text
  and (select status from public.browser_agent_jobs where id = j6) = 'running'
  and exists(select 1 from public.assistant_browser_job_credentials where job_id = j6 and credential_version = 2)
  and (select attempts_day_count from public.assistant_credentials where id = cid) = 3);
 v := public.assistant_browser_job_heartbeat(j6, w, b1, e, 5);
 perform pg_temp.v2ck('fence_after_save', v->>'ok' = 'true');
 update public.assistant_credentials set last_login_attempt_at = null where id = cid;
 v := public.assistant_browser_login_begin_v2(j6, w, b1, e, cid, 'https://login.procore.com'); attempt := (v->>'attempt_id')::uuid;
 v := public.assistant_browser_login_finish_v2(j6, w, b1, e, cid, attempt, 'mfa', null);
 help := gen_random_uuid();
 v := public.assistant_browser_help_open_v2(j6, w, b1, e, help, jsonb_build_object('id', gen_random_uuid(), 'kind', 'help_request',
  'seq', 1, 'envelope', env), 'mfa', cid, attempt);
 v := public.assistant_browser_help_reply_v2(u, j6, help, gen_random_uuid(), 'code', jsonb_build_object('id', gen_random_uuid(),
  'kind', 'help_secret', 'seq', 2, 'envelope', env, 'secret_hash', repeat('c', 64), 'secret_key_id', 'fixture'));
 perform pg_temp.v2ck('login_scope_code', v->>'created' = 'true');
 v := public.assistant_browser_login_finish_v2(j6, w, b1, e, cid, attempt, 'ok', jsonb_build_object('id', gen_random_uuid(),
  'origin', 'https://app.procore.com', 'envelope', env, 'expires_at', now() + interval '1 hour'));
 perform pg_temp.v2ck('session_saved', v->'session' is not null and v->'session' <> 'null'::jsonb);
 help := gen_random_uuid();
 v := public.assistant_browser_help_open_v2(j6, w, b1, e, help, jsonb_build_object('id', gen_random_uuid(), 'kind', 'help_request',
  'seq', 3, 'envelope', env), 'mfa_step_up', null, null);
 v := public.assistant_browser_help_reply_v2(u, j6, help, gen_random_uuid(), 'code', jsonb_build_object('id', gen_random_uuid(),
  'kind', 'help_secret', 'seq', 4, 'envelope', env, 'secret_hash', repeat('e', 64), 'secret_key_id', 'fixture'));
 perform pg_temp.v2ck('step_up_code', v->>'created' = 'true');
 v := public.assistant_browser_help_reply_v2(u, j6, help, gen_random_uuid(), 'message', jsonb_build_object('id', gen_random_uuid(),
  'kind', 'help_message', 'seq', 5, 'envelope', env));
 help := gen_random_uuid();
 v := public.assistant_browser_help_open_v2(j6, w, b1, e, help, jsonb_build_object('id', gen_random_uuid(), 'kind', 'help_request',
  'seq', 6, 'envelope', env), 'push', null, null);
 v := public.assistant_browser_help_reply_v2(u, j6, help, gen_random_uuid(), 'continue', jsonb_build_object('id', gen_random_uuid(),
  'kind', 'help_message', 'seq', 7, 'envelope', env));
 perform pg_temp.v2ck('push_continue', v->>'created' = 'true' and (select status from public.browser_agent_jobs where id = j6) = 'running');

 -- A site hold is released without a token (5.4).
 v := public.assistant_browser_hold_open(j6, w, b1, e, h4, 'site', jsonb_build_object('method', 'GET',
  'origin', 'https://other.example', 'url_sha256', repeat('a', 64), 'body_sha256', repeat('b', 64), 'out_of_scope', true),
  jsonb_build_object('id', gen_random_uuid(), 'kind', 'request', 'seq', 8, 'envelope', env), null);
 v := public.assistant_browser_hold_decide(u, h4, 'approve_site_only', null, null);
 perform pg_temp.v2ck('site_hold_without_token', v->>'ok' = 'true');
 v := public.assistant_browser_hold_release_get(j6, w, b1, e, h4);
 perform pg_temp.v2ck('site_release_has_no_token', v->>'status' = 'approved' and v->>'token' is null and v->>'kind' = 'site'
  and (select side_effects_at from public.browser_agent_jobs where id = j6) is null);

 -- A session without a credential, found by (site_key, account_label).
 v := public.assistant_browser_site_grant(u, 'zz_session_fixture', 'domain', 'example.net', null, 'settings', 'settings');
 g_session := (v#>>'{grant,id}')::uuid;
 insert into public.assistant_browser_sessions(user_id, credential_id, credential_version, origin, version, wrapped_dek, ciphertext,
  nonce, key_id, expires_at, site_key, account_label, source)
 values(u, null, 1, 'https://app.example.net', 1, 'fake', 'fake', 'fake', 'fixture', now() + interval '1 hour',
  'zz_session_fixture', 'default', 'live_signin');
 v := public.assistant_browser_admission_create(u, jsonb_build_object('conversation_id', conv, 'goal', 'Session only',
  'start_site_key', 'zz_session_fixture', 'fresh_user_message', true));
 adm8 := (v#>>'{admission,id}')::uuid;
 v := public.assistant_browser_task_enqueue_v2(u, adm8, '{}'::jsonb, 15, 1000); j7 := (v#>>'{job,id}')::uuid;
 select lease_epoch into e from public.browser_agent_jobs where id = j4;
 perform public.assistant_browser_job_finish(j4, w, b1, e, 'failed', null, null);
 perform 1 from public.assistant_browser_claim_user(w, b1, '{"user_browser":3}'::jsonb, 90);
 select lease_epoch into e from public.browser_agent_jobs where id = j7;
 v := public.assistant_browser_session_get_v2(j7, w, b1, e, 'zz_session_fixture', 'default', 'https://app.example.net', null);
 perform pg_temp.v2ck('session_without_credential', v->'session' is not null and v#>>'{session,source}' = 'live_signin');
 v := public.assistant_browser_session_get_v2(j7, w, b1, e, 'zz_session_fixture', 'default', null, null);
 perform pg_temp.v2ck('session_poll_without_origin', v#>>'{session,origin}' = 'https://app.example.net');
 v := public.assistant_browser_job_policy(j7, w, b1, e);
 -- Shape of protocol.UserJobPolicy: scope per site, a start origin from the
 -- start site, no unpinned (session-only) credential.
 perform pg_temp.v2ck('job_policy', (v->>'policy_version')::int >= 1 and jsonb_array_length(v->'sites') = 1
  and (v#>>'{caps,max_cost_usd}')::numeric = 5 and v#>>'{sites,0,scope}' = 'domain'
  and v->>'start_origin' = 'https://example.net' and jsonb_array_length(v->'credentials') = 0);

 -- Grant revoke cascade: queued job canceled, sessions removed, running job fenced.
 v := public.assistant_browser_admission_create(u, jsonb_build_object('conversation_id', conv, 'goal', 'Queued on public',
  'start_site_key', 'zz_public_fixture', 'fresh_user_message', true));
 adm9 := (v#>>'{admission,id}')::uuid;
 v := public.assistant_browser_task_enqueue_v2(u, adm9, '{}'::jsonb, 15, 1000); j8 := (v#>>'{job,id}')::uuid;
 v := public.assistant_browser_site_revoke(u, g_public, 1);
 perform pg_temp.v2ck('revoke_cancels_queued', (select status = 'canceled' and error_kind = 'scope_revoked' from public.browser_agent_jobs
  where id = j8) and exists(select 1 from public.assistant_browser_chat_outbox where job_id = j8 and event_key = 'terminal'));
 v := public.assistant_browser_site_revoke(u, g_session, 1);
 perform pg_temp.v2ck('revoke_removes_sessions', not exists(select 1 from public.assistant_browser_sessions where user_id = u
  and site_key = 'zz_session_fixture'));
 begin
  perform public.assistant_browser_job_heartbeat(j7, w, b1, e, 5);
  perform pg_temp.v2ck('revoke_fences_running', false);
 exception when raise_exception then
  perform pg_temp.v2ck('revoke_fences_running', sqlerrm = 'scope_revoked');
 end;

 -- Credential replace: a queued job on the account takes the new version,
 -- the running job pinned to the old one ends with an outbox row.
 v := public.assistant_browser_admission_create(u, jsonb_build_object('conversation_id', conv, 'goal', 'Queued on Procore',
  'start_site_key', 'procore', 'fresh_user_message', true));
 adm10 := (v#>>'{admission,id}')::uuid;
 v := public.assistant_browser_task_enqueue_v2(u, adm10, '{}'::jsonb, 15, 1000); j9 := (v#>>'{job,id}')::uuid;
 v := public.assistant_browser_credential_put_v2(u, 'procore', 'default', 2, cred, null);
 perform pg_temp.v2ck('replace_moves_queued_job', v->>'ok' = 'true'
  and (select status from public.browser_agent_jobs where id = j9) = 'queued'
  and exists(select 1 from public.assistant_browser_job_credentials where job_id = j9 and credential_id = cid and credential_version = 3));
 perform pg_temp.v2ck('replace_ends_running_job', (select status = 'canceled' and error_kind = 'credential_changed'
  from public.browser_agent_jobs where id = j6)
  and exists(select 1 from public.assistant_browser_chat_outbox where job_id = j6 and event_key = 'terminal'));
 v := public.assistant_browser_credential_revoke_v2(u, cid, 3);
 perform pg_temp.v2ck('credential_revoke_outbox', (select status = 'canceled' from public.browser_agent_jobs where id = j9)
  and exists(select 1 from public.assistant_browser_chat_outbox where job_id = j9 and event_key = 'terminal'));
 perform public.browser_agent_hello(w, b2, '{}'::jsonb, null);
 perform pg_temp.v2ck('hello_after_crash_outbox', (select status = 'failed' and error_kind = 'worker_restarted'
  from public.browser_agent_jobs where id = j7) and exists(select 1 from public.assistant_browser_chat_outbox where job_id = j7
  and event_key = 'terminal'));
 v := public.assistant_browser_outbox_drain(200);
 perform pg_temp.v2ck('outbox_drain', jsonb_array_length(v->'rows') >= 5);

 -- A queued job whose authority no longer holds is ended at claim, not skipped.
 insert into public.assistant_conversations(id, user_id, mode) values(conv2, u, 'normal');
 perform 1 from public.assistant_browser_site_grant(u, 'zz_public_fixture', 'domain', 'example.org', null, 'chat_card', 'typed');
 v := public.assistant_browser_admission_create(u, jsonb_build_object('conversation_id', conv2, 'goal', 'Tainted later',
  'start_site_key', 'zz_public_fixture', 'fresh_user_message', true));
 adm11 := (v#>>'{admission,id}')::uuid;
 v := public.assistant_browser_task_enqueue_v2(u, adm11, '{}'::jsonb, 15, 1000); j10 := (v#>>'{job,id}')::uuid;
 update public.assistant_conversations set mailbox_tainted_at = now() where id = conv2;
 update public.browser_agent_workers set status = 'active', capabilities = '{"types":{"user_browser":"1.1.0"}}' where id = w;
 select count(*) into n from public.assistant_browser_claim_user(w, b2, '{"user_browser":3}'::jsonb, 90);
 perform pg_temp.v2ck('claim_ends_lost_authority', n = 0 and (select status = 'canceled' and error_kind = 'lease_lost'
  from public.browser_agent_jobs where id = j10)
  and exists(select 1 from public.assistant_browser_chat_outbox where job_id = j10 and event_key = 'terminal')
  and (select r.status from public.assistant_spend_reservations r join public.browser_agent_jobs j on j.assistant_reservation_id = r.id
   where j.id = j10) = 'released');

 -- Offboarding revokes grants and the owner's rows.
 perform public.assistant_browser_revoke_owner(u);
 perform pg_temp.v2ck('revoke_owner', not exists(select 1 from public.assistant_browser_site_grants where user_id = u and status = 'active'));

 -- The org path is unaffected: an org worker's claim never returns a user job.
 perform pg_temp.v2ck('org_claim_unaffected', not exists(select 1 from public.browser_agent_jobs where principal_kind = 'org'
  and assistant_admission_id is not null));

 select count(*) filter (where ok), coalesce(array_agg(label) filter (where not ok), '{}') into n_pass, fails from v2_check;
 raise exception 'TEST_RESULT % pass=% fail=% failures=%', case when cardinality(fails) = 0 then 'PASS' else 'FAIL' end,
  n_pass, cardinality(fails), fails;
end $$;
rollback;
