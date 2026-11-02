-- DEV ONLY after 0189. Synthetic fixtures; no real credential/site/model calls.
-- Existing credentials and flags are unchanged after rollback.
begin;
do $$
declare
 u uuid:=gen_random_uuid();cid uuid:=gen_random_uuid();conv uuid:=gen_random_uuid();
 ap uuid:=gen_random_uuid();app_ap uuid:=gen_random_uuid();new_ap uuid:=gen_random_uuid();
 w uuid:=gen_random_uuid();boot uuid:=gen_random_uuid();attempt uuid;
 v jsonb;cred jsonb;payload jsonb;app_payload jsonb;env jsonb;j public.browser_agent_jobs;
 regional text:='https://us02.procore.com/webclients/host/companies/562949953430361/projects/562949955106559/tools/hubs/project-hub/views/overview';
begin
 assert not exists(select 1 from public.browser_agent_jobs where principal_kind='user' and status in ('queued','running','waiting_help')),'regional check requires idle user queue';
 assert public.assistant_browser_task_origin(regional)='https://us02.procore.com','regional hub refused';
 assert public.assistant_browser_task_origin(replace(regional,'us02','us03')) is null,'arbitrary region allowed';
 assert public.assistant_browser_task_origin(regional||'?redirect=evil') is null,'query allowed';
 insert into auth.users(id,email) values(u,'phase6-regional-'||u::text||'@example.invalid');
 insert into public.profiles(id,full_name,email,role,is_active)
 values(u,'Regional rollback fixture','phase6-regional-'||u::text||'@example.invalid','it_admin',true)
 on conflict(id) do update set role='it_admin',is_active=true;
 update public.assistant_settings set enabled=true where id=1;
 update public.browser_agent_settings set enabled=true,per_job_cost_cap_usd=2,daily_cost_cap_usd=100 where id=1;
 update public.browser_agent_types set enabled=true,provider='openai',model='fixture',price_in_per_mtok=1,price_out_per_mtok=1,max_concurrency=8,model_probe_ok_at=now() where key='user_browser_read';
 insert into public.assistant_delegations(user_id,created_session_aal) values(u,'aal2');
 insert into public.assistant_conversations(id,user_id,mode) values(conv,u,'normal');
 env:='{"wrapped_dek":"fake","ciphertext":"fake","nonce":"fake","key_id":"fixture"}'::jsonb;
 cred:=jsonb_build_object('id',cid,'label','Fixture','username','fixture@example.invalid',
 'origins',jsonb_build_array('https://app.procore.com','https://login.procore.com','https://us02.procore.com'),
 'wrapped_dek','fake','ciphertext','fake','nonce','fake','key_id','fixture','fingerprint','fixture');
 v:=public.assistant_browser_credential_put(u,'procore',0,cred);assert v->>'ok'='true','credential failed';
 -- Represent a pre-0189 credential, only on this generated rollback fixture.
 update public.assistant_credentials set origins=array['https://app.procore.com','https://login.procore.com'] where id=cid;
 payload:=jsonb_build_object('service','procore','url',regional,'objective','Read fixture',
 'credential_id',cid,'credential_version',1,'client_request_id',gen_random_uuid(),'max_cost_usd',2);
 payload:=payload||jsonb_build_object('authority',jsonb_build_object('role','it_admin','model','fixture','conversation_id',conv,
 'credential_id',cid,'credential_version',1,'url',regional,'objective','Read fixture','max_cost_usd','2'));
 insert into public.assistant_approvals(id,user_id,user_label,conversation_id,action_kind,payload,payload_sha256,status,decided_by,decided_at,expires_at)
 values(ap,u,'Fixture',conv,'user_browser_task',jsonb_build_object('request',jsonb_build_object('method','POST','path','/assistant/browser-tasks','json',payload)),repeat('a',64),'approved',u,now(),now()+interval '1 hour');
 v:=public.assistant_browser_task_enqueue(u,ap,gen_random_uuid(),repeat('a',64),payload,100,100);
 assert v->>'code'='destination_invalid','old credential gained regional authority';
 assert (select version=1 and not('https://us02.procore.com'=any(origins)) from public.assistant_credentials where id=cid),'old credential changed';

 app_payload:=jsonb_set(jsonb_set(payload,'{url}','"https://app.procore.com/123/project/rfi"'),'{authority,url}','"https://app.procore.com/123/project/rfi"');
 insert into public.assistant_approvals(id,user_id,user_label,conversation_id,action_kind,payload,payload_sha256,status,decided_by,decided_at,expires_at)
 values(app_ap,u,'Fixture',conv,'user_browser_task',jsonb_build_object('request',jsonb_build_object('method','POST','path','/assistant/browser-tasks','json',app_payload)),repeat('b',64),'approved',u,now(),now()+interval '1 hour');
 v:=public.assistant_browser_task_enqueue(u,app_ap,gen_random_uuid(),repeat('b',64),app_payload,100,100);
 assert v->>'created'='true','legacy app credential stopped working';
 select * into j from public.browser_agent_jobs where id=(v#>>'{job,id}')::uuid;
 insert into public.browser_agent_workers(id,name,status,principal_kind,boot_id,allowed_types,capabilities)
 values(w,'phase6-regional-'||w::text,'active','user',boot,array['user_browser_read'],'{"types":{"user_browser_read":"1.0.0"}}');
 -- An internally malformed queued job still cannot expand stored origin authority.
 update public.browser_agent_jobs set input=payload where id=j.id;
 assert not exists(select 1 from public.claim_browser_agent_jobs(w,boot,'{"user_browser_read":1}',90)),'claim accepted old regional credential';
 update public.browser_agent_jobs set input=app_payload where id=j.id;
 select * into j from public.claim_browser_agent_jobs(w,boot,'{"user_browser_read":1}',90);
 assert j.lease_owner=w,'legacy app claim failed';
 update public.browser_agent_jobs set input=payload where id=j.id;
 begin
  perform public.assistant_browser_user_fence(j.id,w,boot,j.lease_epoch);
  raise exception 'fence accepted old regional credential';
 exception when raise_exception then if sqlerrm<>'lease_lost' then raise; end if; end;
 update public.browser_agent_jobs set input=app_payload where id=j.id;

 v:=public.assistant_browser_credential_put(u,'procore',1,cred);
 assert v#>>'{credential,version}'='2','explicit owner CAS did not advance generation';
 assert (select status='canceled' from public.browser_agent_jobs where id=j.id),'credential replacement did not cancel old job';
 v:=public.assistant_browser_credential_put(u,'procore',1,cred);
 assert v->>'code'='version_conflict','stale owner save accepted';
 v:=public.assistant_browser_task_enqueue(u,ap,gen_random_uuid(),repeat('a',64),payload,100,100);
 assert v->>'code'='credential_changed','old approval used replacement authority';
 payload:=jsonb_set(jsonb_set(payload,'{credential_version}','2'),'{authority,credential_version}','2');
 insert into public.assistant_approvals(id,user_id,user_label,conversation_id,action_kind,payload,payload_sha256,status,decided_by,decided_at,expires_at)
 values(new_ap,u,'Fixture',conv,'user_browser_task',jsonb_build_object('request',jsonb_build_object('method','POST','path','/assistant/browser-tasks','json',payload)),repeat('c',64),'approved',u,now(),now()+interval '1 hour');
 v:=public.assistant_browser_task_enqueue(u,new_ap,gen_random_uuid(),repeat('c',64),payload,100,100);
 assert v->>'created'='true','resaved regional credential refused';
 select * into j from public.claim_browser_agent_jobs(w,boot,'{"user_browser_read":1}',90);
 assert j.lease_owner=w and j.user_credential_version=2,'regional claim failed';
 v:=public.assistant_browser_login_begin(j.id,w,boot,j.lease_epoch,'https://login.procore.com');
 assert v->>'granted'='true','regional login begin failed';attempt:=(v->>'attempt_id')::uuid;
 v:=public.assistant_browser_login_finish(j.id,w,boot,j.lease_epoch,attempt,'ok',jsonb_build_object('id',gen_random_uuid(),'origin','https://app.procore.com','envelope',env));
 assert v->>'code'='session_invalid','regional job saved app cookie jar';
 v:=public.assistant_browser_login_finish(j.id,w,boot,j.lease_epoch,attempt,'ok',jsonb_build_object('id',gen_random_uuid(),'origin','https://us02.procore.com','envelope',env));
 assert v->>'ok'='true','regional cookie jar refused';
 v:=public.assistant_browser_session_get(j.id,w,boot,j.lease_epoch,'https://app.procore.com');
 assert v->>'code'='origin_refused','regional job retrieved different host jar';
 v:=public.assistant_browser_session_get(j.id,w,boot,j.lease_epoch,'https://us02.procore.com');
 assert v#>>'{session,credential_version}'='2' and v#>>'{session,origin}'='https://us02.procore.com','regional session mismatch';
end $$;
rollback;
