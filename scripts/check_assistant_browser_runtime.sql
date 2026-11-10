-- RETIRED (browser v2, migration 0192): this script checks the runtime primitives of the v1 personal browser (user_browser_task approvals, user_browser_read jobs, v1 enqueue and fences).
-- It inserts user_browser_task approvals and user_browser_read jobs, a type that
-- 0192 relabels "Retired personal browser read" and disables; no application code
-- or worker reaches those v1 SQL functions any more, and they stay in the database
-- only until a later cleanup migration drops them. It is NOT acceptance for v2.
-- v2 acceptance is scripts/check_assistant_browser_v2.sql (admissions, site
-- grants, credentials per site, claim limits, holds, downloads, purge) and the
-- race probe scripts/check_assistant_browser_concurrency.py. Run this script
-- only to confirm the old v1 functions still behave while they remain; delete it
-- with the cleanup migration.
-- DEV ONLY. Real transactional runtime primitives; all fixtures roll back.
-- No model/site calls, real credentials, Windows actions or notification sends.
begin;
do $$
declare u uuid:=gen_random_uuid();cid uuid:=gen_random_uuid();conv uuid:=gen_random_uuid();ap uuid:=gen_random_uuid();run uuid:=gen_random_uuid();
 w uuid:=gen_random_uuid();boot uuid:=gen_random_uuid();v jsonb;payload jsonb;cred jsonb;env jsonb;
 j public.browser_agent_jobs;r public.assistant_browser_records;attempt uuid;helpid uuid:=gen_random_uuid();recid uuid;msgid uuid:=gen_random_uuid();
 turnid uuid;turntoken uuid;before_cost numeric;f record;secondid uuid;
begin
 assert not exists(select 1 from public.browser_agent_jobs where principal_kind='user' and status in ('queued','running','waiting_help')),'runtime check requires idle user queue';
 insert into auth.users(id,email) values(u,'phase6-runtime-'||u::text||'@example.invalid');
 insert into public.profiles(id,full_name,email,role,is_active) values(u,'Browser runtime rollback fixture','phase6-runtime-'||u::text||'@example.invalid','it_admin',true)
 on conflict(id) do update set role='it_admin',is_active=true;
 update public.assistant_settings set enabled=true where id=1;
 update public.browser_agent_settings set enabled=true,per_job_cost_cap_usd=2,daily_cost_cap_usd=100 where id=1;
 update public.browser_agent_types set enabled=true,provider='openai',model='fixture',price_in_per_mtok=1,price_out_per_mtok=1,max_concurrency=8,model_probe_ok_at=now() where key='user_browser_read';
 insert into public.assistant_delegations(user_id,created_session_aal) values(u,'aal2');
 insert into public.assistant_conversations(id,user_id,mode) values(conv,u,'normal');
 env:='{"wrapped_dek":"fake","ciphertext":"fake","nonce":"fake","key_id":"fixture"}'::jsonb;
 cred:=jsonb_build_object('id',cid,'label','Fixture','username','fixture@example.invalid','origins',jsonb_build_array('https://app.procore.com','https://login.procore.com','https://us02.procore.com'),
 'wrapped_dek','fake','ciphertext','fake','nonce','fake','key_id','fixture','fingerprint','fixture');
 v:=public.assistant_browser_credential_put(u,'procore',0,cred);assert v->>'ok'='true','credential failed';
 payload:=jsonb_build_object('service','procore','url','https://app.procore.com/123/project/rfi','objective','Read fixture',
 'credential_id',cid,'credential_version',1,'client_request_id',gen_random_uuid(),'max_cost_usd',2);
 payload:=payload||jsonb_build_object('authority',jsonb_build_object('role','it_admin','model','fixture','conversation_id',conv,'credential_id',cid,'credential_version',1,'url',payload->>'url','objective',payload->>'objective','max_cost_usd','2'));
 insert into public.assistant_approvals(id,user_id,user_label,conversation_id,action_kind,payload,payload_sha256,status,decided_by,decided_at,expires_at)
 values(ap,u,'Fixture',conv,'user_browser_task',jsonb_build_object('request',jsonb_build_object('method','POST','path','/assistant/browser-tasks','json',payload)),repeat('a',64),'approved',u,now(),now()+interval '1 hour');
 v:=public.assistant_browser_task_enqueue(u,ap,run,repeat('a',64),payload,100,100);assert v->>'ok'='true' and v->>'created'='true','admission failed';
 select * into j from public.browser_agent_jobs where id=(v#>>'{job,id}')::uuid;
 assert j.principal_user_id=u and j.principal_kind='user' and j.user_credential_version=1,'principal wrong';
 assert j.expires_at=now()+interval '8 hours','wrong expiry';
 v:=public.assistant_browser_task_enqueue(u,ap,run,repeat('a',64),payload,100,100);assert v->>'created'='false','duplicate queued';
 insert into public.browser_agent_workers(id,name,status,principal_kind,boot_id,allowed_types,capabilities)
 values(w,'phase6-'||w::text,'active','user',boot,array['user_browser_read','rfi_from_email'],
 '{"types":{"user_browser_read":"1.0.0","rfi_from_email":"1.0.0"}}');
 select * into j from public.claim_browser_agent_jobs(w,boot,'{"user_browser_read":1,"rfi_from_email":1}',90);
 assert j.principal_user_id=u and j.lease_owner=w and j.lease_epoch=1,'user claim failed';
 assert not exists(select 1 from public.browser_agent_steps where job_id=j.id),'claim wrote plaintext';
 assert not exists(select 1 from public.claim_browser_agent_jobs(w,boot,'{"user_browser_read":1}',90)),'worker claimed duplicate';
 begin
  perform public.browser_agent_fence(j.id,w,boot,j.lease_epoch,array['running']);raise exception 'legacy fence accepted user';
 exception when raise_exception then if sqlerrm<>'user_protocol_required' then raise; end if; end;
 begin
  insert into public.browser_agent_steps(job_id,epoch,kind,actor,summary) values(j.id,j.lease_epoch,'fixture','worker','secret');
  raise exception 'legacy trace accepted user';
 exception when raise_exception then if sqlerrm<>'user_trace_requires_encryption' then raise; end if; end;
 v:=public.assistant_browser_login_begin(j.id,w,boot,j.lease_epoch,'https://evil.invalid');assert v->>'code'='origin_refused','origin leaked credential';
 v:=public.assistant_browser_login_begin(j.id,w,boot,j.lease_epoch,'https://login.procore.com');assert v->>'granted'='true','login begin failed';attempt:=(v->>'attempt_id')::uuid;
 v:=public.assistant_browser_login_begin(j.id,w,boot,j.lease_epoch,'https://login.procore.com');assert v->>'granted'='false','login begun twice';
 v:=public.assistant_browser_login_finish(j.id,w,boot,j.lease_epoch,attempt,'mfa',null);assert v->>'help_required'='true','MFA did not preserve attempt';
 v:=public.assistant_browser_help_open(j.id,w,boot,j.lease_epoch,helpid,jsonb_build_object('id',gen_random_uuid(),'kind','help_request','seq',0,'envelope',env),attempt);
 assert v->>'ok'='true','help open failed';
 recid:=gen_random_uuid();
 v:=public.assistant_browser_help_reply(u,j.id,helpid,msgid,'code',jsonb_build_object('id',recid,'kind','help_secret','seq',1,'envelope',env,'secret_hash',repeat('c',64),'secret_key_id','fixture'));
 assert v->>'created'='true','code reply failed';
 v:=public.assistant_browser_help_reply(u,j.id,helpid,gen_random_uuid(),'code',jsonb_build_object('id',gen_random_uuid(),'kind','help_secret','seq',2,'envelope',env,'secret_hash',repeat('d',64),'secret_key_id','fixture'));
 assert v->>'code'='code_already_supplied','second code accepted';
 v:=public.assistant_browser_help_consume(j.id,w,boot,j.lease_epoch,helpid,recid,attempt);
 assert v->>'ok'='true' and v#>'{record,envelope}'=env,'secret consume failed';
 assert exists(select 1 from public.assistant_browser_records where id=recid and envelope is null and consumed_at is not null and secret_hash=repeat('c',64)),'secret not destroyed';
 v:=public.assistant_browser_help_consume(j.id,w,boot,j.lease_epoch,helpid,recid,attempt);assert v->>'code'='already_consumed','secret replay';
 v:=public.assistant_browser_login_finish(j.id,w,boot,j.lease_epoch,attempt,'ok',jsonb_build_object('id',gen_random_uuid(),'origin','https://app.procore.com','envelope',env,'expires_at',now()+interval '1 hour'));
 assert v->>'ok'='true','session save failed';
 v:=public.assistant_browser_session_get(j.id,w,boot,j.lease_epoch,'https://app.procore.com');assert v#>>'{session,credential_version}'='1','session read failed';
 v:=public.assistant_browser_login_finish(j.id,w,boot,j.lease_epoch,attempt,'ok',null);assert v->>'code'='attempt_changed','login result replay';
 v:=public.assistant_browser_turn_begin(u,j.id,w,boot,j.lease_epoch,1,0.20,1000);assert v->>'claimed'='true','turn begin failed';turnid:=(v->>'turn_id')::uuid;turntoken:=(v->>'token')::uuid;
 v:=public.assistant_browser_turn_begin(u,j.id,w,boot,j.lease_epoch,1,0.20,1000);assert v->>'claimed'='false','turn duplicated';
 v:=public.assistant_browser_turn_finish(u,j.id,w,boot,j.lease_epoch,turnid,gen_random_uuid(),0.10,env,env);assert v->>'code'='turn_conflict','foreign token accepted';
 v:=public.assistant_browser_turn_finish(u,j.id,w,boot,j.lease_epoch,turnid,turntoken,0.10,env,env);assert v->>'changed'='true','turn finish failed';
 v:=public.assistant_browser_turn_begin(u,j.id,w,boot,j.lease_epoch,1,0.20,1000);assert v->'replay_envelope'=env,'turn replay response missing';
 select cost_usd into before_cost from public.browser_agent_jobs where id=j.id;assert before_cost=0.10,'estimate was not replaced';
 v:=public.assistant_browser_turn_begin(u,j.id,w,boot,j.lease_epoch,2,2.00,1000);assert v->>'code'='cost_cap','job cap bypass';
 v:=public.assistant_browser_job_heartbeat(j.id,w,boot,j.lease_epoch,15);assert v->>'ok'='true','heartbeat failed';
 v:=public.assistant_browser_inspect(u,u,j.id,'Rollback inspection fixture','all');assert v->>'ok'='true','inspection failed';
 assert not exists(select 1 from jsonb_array_elements(v->'records') x where x->>'kind'='help_secret'),'inspection returned secret';
 assert exists(select 1 from public.assistant_browser_access where id=(v->>'audit_id')::uuid),'inspection audit absent';
 v:=public.assistant_browser_job_finish(j.id,w,boot,j.lease_epoch,'succeeded',jsonb_build_object('id',gen_random_uuid(),'kind','result','seq',2,'envelope',env),null);
 assert v->>'ok'='true','finish failed';
 assert exists(select 1 from public.assistant_spend_reservations where id=j.assistant_reservation_id and status='settled' and actual_usd=0.10),'hold not settled';
 assert (select count(*) from public.assistant_browser_notices where job_id=j.id and notice_key='terminal')=1,'terminal notice missing';
 v:=public.assistant_browser_notice_claim(100);assert exists(select 1 from jsonb_array_elements(v->'rows') n where n->>'user_id'=u::text),'email notice not claimed';
 -- Retention observes legal hold, while secret envelopes already stay cleared.
 insert into public.assistant_profiles(user_id,legal_hold) values(u,true) on conflict(user_id) do update set legal_hold=true;
 update public.assistant_browser_records set expires_at=now()-interval '1 day' where job_id=j.id;
 perform public.assistant_browser_records_purge(1000);
 assert exists(select 1 from public.assistant_browser_records where job_id=j.id),'legal hold purged trace';
 update public.assistant_profiles set legal_hold=false where user_id=u;
 perform public.assistant_browser_records_purge(1000);
 assert not exists(select 1 from public.assistant_browser_records where job_id=j.id),'expired trace retained';
 for f in select p.oid from pg_proc p join pg_namespace n on n.oid=p.pronamespace where n.nspname='public' and p.proname like 'assistant_browser_%' loop
  assert not has_function_privilege('anon',f.oid,'EXECUTE'),'anon RPC access';
  assert not has_function_privilege('authenticated',f.oid,'EXECUTE'),'authenticated RPC access';
 end loop;
end $$;
rollback;
