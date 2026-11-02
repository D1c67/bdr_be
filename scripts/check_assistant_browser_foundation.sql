-- DEV ONLY: transactional fake-secret fixtures, all changes rolled back.
-- Checks the foundation gate, not live Windows, login, MFA or browser execution.
begin;
do $$
declare u uuid:=gen_random_uuid(); c uuid:=gen_random_uuid(); w uuid:=gen_random_uuid(); b uuid:=gen_random_uuid();
 v jsonb; payload jsonb; r record; before_version int;
 org_worker uuid:=gen_random_uuid(); org_boot uuid:=gen_random_uuid(); org_job uuid:=gen_random_uuid();
 org_type text:='phase6_'||replace(gen_random_uuid()::text,'-','');
 claimed public.browser_agent_jobs; fenced public.browser_agent_jobs;
begin
 insert into auth.users(id,email) values(u,'phase6-'||u::text||'@example.invalid');
 insert into public.profiles(id,full_name,email,role,is_active)
 values(u,'Phase 6 rollback fixture','phase6-'||u::text||'@example.invalid','it_admin',true)
 on conflict(id) do update set role='it_admin',is_active=true;
 payload:=jsonb_build_object('id',c,'label','Fixture','username','fixture@example.invalid',
  'origins',jsonb_build_array('https://app.procore.com','https://login.procore.com'),
  'wrapped_dek','fixture-wrapped','ciphertext','fixture-ciphertext','nonce','fixture-nonce','key_id','fixture-key','fingerprint','fixture-fingerprint');
 v:=public.assistant_browser_credential_put(u,'procore',0,payload);
 assert v->>'ok'='true','credential create failed';
 assert (v#>>'{credential,version}')::int=1,'initial generation incorrect';
 update public.profiles set full_name='Phase 6 name-only update' where id=u;
 assert exists(select 1 from public.assistant_credentials where id=c and status='active' and version=1),'name-only update revoked credentials';
 v:=public.assistant_browser_credential_put(u,'procore',0,payload);
 assert v->>'code'='version_conflict','stale creation overwrote credential';
 v:=public.assistant_browser_credential_put(u,'procore',1,payload||jsonb_build_object('origins',jsonb_build_array('https://evil.invalid')));
 assert v->>'code'='invalid','arbitrary origin accepted';
 v:=public.assistant_browser_credential_put(u,'procore',1,payload||jsonb_build_object('id',gen_random_uuid()));
 assert v->>'code'='invalid','credential ID changed across generation';
 insert into public.assistant_browser_sessions(user_id,credential_id,credential_version,origin,version,wrapped_dek,ciphertext,nonce,key_id,expires_at)
 values(u,c,1,'https://app.procore.com',1,'fake','fake','fake','fake',now()+interval '1 hour');
 v:=public.assistant_browser_credential_put(u,'procore',1,payload);
 assert v->>'ok'='true' and (v#>>'{credential,version}')::int=2,'credential replace failed';
 assert not exists(select 1 from public.assistant_browser_sessions where credential_id=c),'replacement retained session';
 v:=public.assistant_browser_credential_revoke(u,'procore',1);
 assert v->>'code'='version_conflict','stale tab revoked new credential';
 v:=public.assistant_browser_credential_revoke(u,'procore',2);
 assert v->>'ok'='true' and (v->>'version')::int=3,'revoke failed';
 assert exists(select 1 from public.assistant_credentials where id=c and status='revoked' and ciphertext is null and wrapped_dek is null),'revoke retained secret';
 v:=public.assistant_browser_credential_put(u,'procore',0,payload);
 assert v->>'code'='version_conflict','revoked tombstone bypassed generation';
 v:=public.assistant_browser_credential_put(u,'procore',3,payload);
 assert v->>'ok'='true' and (v#>>'{credential,version}')::int=4,'restore with current generation failed';
 update public.profiles set mfa_enrolled=not mfa_enrolled where id=u;
 assert exists(select 1 from public.assistant_credentials where id=c and version=5 and status='revoked'),'MFA profile change did not revoke';
 v:=public.assistant_browser_credential_put(u,'procore',5,payload);
 assert v->>'ok'='true','restore before offboarding failed';
 update public.profiles set is_active=false where id=u;
 assert exists(select 1 from public.assistant_credentials where id=c and version=7 and status='revoked'),'deactivation did not revoke';
 v:=public.assistant_browser_credential_put(u,'procore',7,payload);
 assert v->>'code'='ineligible','inactive owner saved credential';
 update public.profiles set is_active=true,role='accountant' where id=u;
 v:=public.assistant_browser_credential_put(u,'procore',7,payload);
 assert v->>'code'='ineligible','Accountant saved credential';
 assert not public.assistant_browser_runtime_ready(),'runtime hard gate opened';
 v:=public.assistant_browser_task_enqueue(u,null,null,null,'{}'::jsonb);
 assert v->>'code'='runtime_unavailable','task admission bypassed hard gate';
 v:=public.assistant_browser_session_put(u,c,1,'https://app.procore.com',0,'{}'::jsonb);
 assert v->>'code'='runtime_unavailable','session save bypassed hard gate';
 insert into public.browser_agent_workers(id,name,status,principal_kind,boot_id,allowed_types,capabilities)
 values(w,'phase6-'||w::text,'active','user',b,array['user_browser_read','rfi_from_email'],
  '{"types":{"user_browser_read":"1.0.0","rfi_from_email":"1.0.0"}}');
 assert not exists(select 1 from public.claim_browser_agent_jobs(w,b,'{"user_browser_read":1,"rfi_from_email":1}',90)),'user pool claimed work';
 -- A fresh custom org type keeps this regression probe away from real queues.
 insert into public.browser_agent_types(key,label,identity,prompt_version,principal_kind,enabled,
   provider,model,price_in_per_mtok,price_out_per_mtok,max_concurrency)
 values(org_type,'Phase 6 organization regression',null,'fixture.v1','org',true,'openai','fixture',1,1,1);
 update public.browser_agent_settings set enabled=true where id=1;
 insert into public.browser_agent_workers(id,name,status,principal_kind,boot_id,allowed_types,capabilities)
 values(org_worker,'phase6-'||org_worker::text,'active','org',org_boot,array[org_type],
   jsonb_build_object('types',jsonb_build_object(org_type,'1.0.0')));
 insert into public.browser_agent_jobs(id,agent_type,dedupe_key,input,expires_at,principal_kind)
 values(org_job,org_type,'phase6:'||org_job::text,'{}'::jsonb,now()+interval '1 hour','org');
 select * into claimed from public.claim_browser_agent_jobs(org_worker,org_boot,jsonb_build_object(org_type,1),90);
 assert claimed.id=org_job and claimed.status='running','organization claim did not select its isolated fixture';
 assert claimed.lease_owner=org_worker and claimed.lease_boot_id=org_boot and claimed.lease_epoch=1,'organization lease shape changed';
 fenced:=public.browser_agent_fence(org_job,org_worker,org_boot,claimed.lease_epoch,array['running']);
 assert fenced.id=org_job and fenced.principal_kind='org','organization lease fence failed';
 assert not exists(select 1 from public.claim_browser_agent_jobs(org_worker,org_boot,jsonb_build_object(org_type,1),90)),
   'organization job was claimed twice';
 begin
  perform public.browser_agent_fence(org_job,org_worker,org_boot,claimed.lease_epoch+1,array['running']);
  raise exception 'stale organization epoch accepted';
 exception when raise_exception then
  if sqlerrm<>'lease_lost' then raise; end if;
 end;

 begin
  update public.browser_agent_workers set principal_kind='org' where id=w;
  raise exception 'pool reassignment accepted';
 exception when raise_exception then
  if sqlerrm<>'browser_pool_immutable' then raise; end if;
 end;
 for r in select p.oid,n.nspname,p.proname from pg_proc p join pg_namespace n on n.oid=p.pronamespace
  where n.nspname='public' and p.proname like 'assistant_browser_%' loop
  assert not has_function_privilege('anon',r.oid,'EXECUTE'),'anon RPC access';
  assert not has_function_privilege('authenticated',r.oid,'EXECUTE'),'authenticated RPC access';
  assert has_function_privilege('service_role',r.oid,'EXECUTE'),'service RPC missing';
 end loop;
 for r in select c.oid,c.relname,c.relrowsecurity,c.relforcerowsecurity from pg_class c join pg_namespace n on n.oid=c.relnamespace
  where n.nspname='public' and c.relname in ('assistant_credentials','assistant_browser_sessions','assistant_browser_access') loop
  assert r.relrowsecurity and r.relforcerowsecurity,'RLS not forced';
  assert not has_table_privilege('anon',r.oid,'SELECT'),'anon table read';
  assert not has_table_privilege('authenticated',r.oid,'SELECT'),'authenticated table read';
 end loop;
 update public.profiles set role='it_admin',is_active=true where id=u;
 select version into before_version from public.assistant_credentials where id=c;
 v:=public.assistant_browser_credential_put(u,'procore',before_version,payload);
 assert v->>'ok'='true','credential restore before auth cascade failed';
 insert into public.assistant_browser_sessions(user_id,credential_id,credential_version,origin,version,wrapped_dek,ciphertext,nonce,key_id,expires_at)
 values(u,c,(v#>>'{credential,version}')::int,'https://app.procore.com',1,'fake','fake','fake','fake',now()+interval '1 hour');
 perform set_config('assistant.phase6_fixture_user',u::text,true);
end $$;
-- Verify cascade behavior under the connector's existing database role.
-- The MCP runner cannot SET ROLE supabase_auth_admin, so the actual GoTrue
-- role privilege check remains UNVERIFIED. No role grants are added.
delete from auth.users where id=current_setting('assistant.phase6_fixture_user')::uuid;
do $$ declare u uuid:=current_setting('assistant.phase6_fixture_user')::uuid; begin
 assert not exists(select 1 from public.profiles where id=u),'auth delete retained profile';
 assert not exists(select 1 from public.assistant_credentials where user_id=u),'auth delete retained credential';
 assert not exists(select 1 from public.assistant_browser_sessions where user_id=u),'auth delete retained session';
end $$;
rollback;
