-- Explicit regional Procore read support. Existing credential authority is unchanged.
-- Owners must resave through credential CAS to authorize the additional origin.
-- No credential data update, job creation, feature enable or release selection occurs here.

create or replace function public.assistant_browser_task_origin(p_url text)
returns text language sql immutable security invoker set search_path=pg_catalog,public as $$
 select case when p_url ~ '^https://(app|us02)\.procore\.com/([0-9]{1,20}/project/(home|rfi(/list)?|rfi/show/[0-9]{1,20})|webclients/host/companies/[0-9]{1,20}/projects/[0-9]{1,20}/tools/(projecthome|rfis(/[0-9]{1,20})?|hubs/project-hub/views/overview))/?$'
 then 'https://'||split_part(p_url,'/',3) else null end
$$;
revoke all on function public.assistant_browser_task_origin(text) from public,anon,authenticated;
grant execute on function public.assistant_browser_task_origin(text) to service_role;


create or replace function public.assistant_browser_credential_put(
 p_user_id uuid,p_service text,p_expected_version int,p_record jsonb)
returns jsonb language plpgsql security invoker set search_path=pg_catalog,public as $$
declare c public.assistant_credentials; v_id uuid; v_version int; v_origins text[];
begin
 if not public.assistant_browser_owner_eligible(p_user_id,false) then return '{"ok":false,"code":"ineligible"}'::jsonb; end if;
 if p_service is distinct from 'procore' or p_expected_version is null or p_expected_version<0
  or jsonb_typeof(p_record) is distinct from 'object' then return '{"ok":false,"code":"invalid"}'::jsonb; end if;
 perform pg_advisory_xact_lock(hashtextextended('assistant-browser-owner:'||p_user_id::text,0));
 select * into c from public.assistant_credentials where user_id=p_user_id and service=p_service for update;
 if (c.id is null and p_expected_version<>0) or (c.id is not null and c.version<>p_expected_version) then
  return '{"ok":false,"code":"version_conflict"}'::jsonb;
 end if;
 v_id:=(p_record->>'id')::uuid;
 if v_id is null or (c.id is not null and v_id<>c.id) then return '{"ok":false,"code":"invalid"}'::jsonb; end if;
 v_version:=coalesce(c.version,0)+1;
 v_origins:=array['https://app.procore.com','https://login.procore.com','https://us02.procore.com'];
 if p_record->'origins' is distinct from to_jsonb(v_origins)
  or length(coalesce(p_record->>'label','')) not between 1 and 100
  or length(coalesce(p_record->>'username','')) not between 1 and 320
  or length(coalesce(p_record->>'wrapped_dek','')) not between 1 and 4096
  or length(coalesce(p_record->>'ciphertext','')) not between 1 and 32768
  or length(coalesce(p_record->>'nonce','')) not between 1 and 128
  or length(coalesce(p_record->>'key_id','')) not between 1 and 80
  or length(coalesce(p_record->>'fingerprint','')) not between 1 and 128 then
  return '{"ok":false,"code":"invalid"}'::jsonb;
 end if;
 if c.id is not null then perform public.assistant_browser_cancel_credentials(p_user_id,c.id); end if;
 insert into public.assistant_credentials(id,user_id,service,label,origins,username,wrapped_dek,ciphertext,nonce,key_id,version,fingerprint)
 values(v_id,p_user_id,p_service,p_record->>'label',v_origins,p_record->>'username',p_record->>'wrapped_dek',
 p_record->>'ciphertext',p_record->>'nonce',p_record->>'key_id',v_version,p_record->>'fingerprint')
 on conflict(user_id,service) do update set label=excluded.label,origins=excluded.origins,username=excluded.username,
 wrapped_dek=excluded.wrapped_dek,ciphertext=excluded.ciphertext,nonce=excluded.nonce,key_id=excluded.key_id,
 version=excluded.version,fingerprint=excluded.fingerprint,status='active',login_failures=0,locked_until=null,
 last_login_attempt_at=null,login_attempt_id=null,login_job_id=null,login_epoch=null,login_attempt_expires_at=null,updated_at=now()
 returning * into c;
 return jsonb_build_object('ok',true,'credential',to_jsonb(c));
exception when invalid_text_representation then return '{"ok":false,"code":"invalid"}'::jsonb;
end $$;

create or replace function public.assistant_browser_task_enqueue(
 p_user_id uuid,p_approval_id uuid,p_run_id uuid,p_payload_sha256 text,p_input jsonb,
 p_user_cap_default numeric default 5,p_global_cap_default numeric default 50)
returns jsonb language plpgsql security invoker set search_path=pg_catalog,public as $$
declare a public.assistant_approvals; c public.assistant_credentials; j public.browser_agent_jobs;
 v_hold jsonb; v_cost numeric; v_type public.browser_agent_types; v_settings public.browser_agent_settings;v_role text;
begin
 if not public.assistant_browser_runtime_ready() then return '{"ok":false,"code":"runtime_unavailable"}'::jsonb; end if;
 if not public.assistant_browser_owner_eligible(p_user_id,true) then return '{"ok":false,"code":"ineligible"}'::jsonb; end if;
 perform pg_advisory_xact_lock(hashtextextended('assistant-browser-owner:'||p_user_id::text,0));
 select * into j from public.browser_agent_jobs where principal_user_id=p_user_id and assistant_approval_id=p_approval_id;
 if found then return jsonb_build_object('ok',true,'created',false,'job',to_jsonb(j),'reservation_id',j.assistant_reservation_id); end if;
 select * into a from public.assistant_approvals where id=p_approval_id and user_id=p_user_id for update;
 if a.id is null or a.status not in ('approved','executing') or a.decided_by is distinct from p_user_id
  or a.expires_at<=now() or a.payload_sha256 is distinct from p_payload_sha256
  or a.action_kind<>'user_browser_task' or a.payload#>>'{request,method}' is distinct from 'POST'
  or a.payload#>>'{request,path}' is distinct from '/assistant/browser-tasks'
  or a.payload#>'{request,json}' is distinct from p_input then return '{"ok":false,"code":"approval_invalid"}'::jsonb; end if;
 perform 1 from public.assistant_conversations where id=a.conversation_id and user_id=p_user_id
  and mode='normal' and mailbox_tainted_at is null for share;
 if not found then return '{"ok":false,"code":"conversation_changed"}'::jsonb; end if;
 select * into c from public.assistant_credentials where id=(p_input->>'credential_id')::uuid and user_id=p_user_id for share;
 if c.id is null or c.status<>'active' or c.version is distinct from (p_input->>'credential_version')::int
  or c.service is distinct from p_input->>'service' then return '{"ok":false,"code":"credential_changed"}'::jsonb; end if;
 select * into v_type from public.browser_agent_types where key='user_browser_read' and principal_kind='user' and enabled for share;
 if not found then return '{"ok":false,"code":"type_unavailable"}'::jsonb; end if;
 select role::text into v_role from public.profiles where id=p_user_id;
 if p_input#>>'{authority,role}' is distinct from v_role
  or p_input#>>'{authority,model}' is distinct from v_type.model
  or p_input#>>'{authority,conversation_id}' is distinct from a.conversation_id::text
  or p_input#>>'{authority,credential_id}' is distinct from c.id::text
  or p_input#>>'{authority,credential_version}' is distinct from c.version::text
  or p_input#>>'{authority,url}' is distinct from p_input->>'url'
  or p_input#>>'{authority,objective}' is distinct from p_input->>'objective'
  or p_input#>>'{authority,max_cost_usd}' is distinct from p_input->>'max_cost_usd'
  or v_type.provider is distinct from 'openai' or v_type.model_probe_ok_at is null
  or coalesce(v_type.price_in_per_mtok,0)<=0 or coalesce(v_type.price_out_per_mtok,0)<=0 then
  return '{"ok":false,"code":"authority_changed"}'::jsonb;
 end if;
 if not coalesce(public.assistant_browser_task_origin(p_input->>'url')=any(c.origins),false)
  or length(coalesce(p_input->>'objective','')) not between 1 and 2000 then return '{"ok":false,"code":"destination_invalid"}'::jsonb; end if;
 select * into v_settings from public.browser_agent_settings where id=1 and enabled for share;
 if not found then return '{"ok":false,"code":"type_unavailable"}'::jsonb; end if;
 if exists(select 1 from public.browser_agent_jobs where principal_user_id=p_user_id
  and status in ('queued','running','waiting_help','applying')) then return '{"ok":false,"code":"concurrency_cap"}'::jsonb; end if;
 v_cost:=(p_input->>'max_cost_usd')::numeric;
 if v_cost is null or v_cost<=0 or v_cost>least(2,v_settings.per_job_cost_cap_usd) then
  return '{"ok":false,"code":"invalid_cost"}'::jsonb;
 end if;
 v_hold:=public.assistant_spend_reserve(p_user_id,'browser_task',v_cost,28800,p_user_cap_default,p_global_cap_default,p_run_id,null,p_approval_id);
 if not coalesce((v_hold->>'ok')::boolean,false) then return jsonb_build_object('ok',false,'code',v_hold->>'reason'); end if;
 insert into public.browser_agent_jobs(agent_type,dedupe_key,platform,input,created_by,expires_at,
 principal_kind,principal_user_id,user_credential_id,user_credential_version,assistant_approval_id,assistant_conversation_id,assistant_reservation_id)
 values('user_browser_read','assistant:'||p_user_id::text||':'||p_approval_id::text,c.service,p_input,p_user_id,now()+interval '8 hours',
 'user',p_user_id,c.id,c.version,p_approval_id,a.conversation_id,(v_hold->>'id')::uuid) returning * into j;
 return jsonb_build_object('ok',true,'created',true,'job',to_jsonb(j),'reservation_id',j.assistant_reservation_id);
end $$;

create or replace function public.assistant_browser_claim_user(p_worker uuid,p_boot uuid,p_capacity jsonb,p_lease_seconds int default 90)
returns setof public.browser_agent_jobs language plpgsql security invoker set search_path=pg_catalog,public as $$
declare w public.browser_agent_workers;typ public.browser_agent_types;candidate record;j public.browser_agent_jobs;c public.assistant_credentials;cfg public.browser_agent_settings;
begin
 select * into w from public.browser_agent_workers where id=p_worker for share;
 if w.id is null or w.principal_kind<>'user' or w.status<>'active' or w.boot_id is distinct from p_boot then raise exception 'lease_lost'; end if;
 if w.draining or not public.assistant_browser_runtime_ready() then return; end if;
 select * into cfg from public.browser_agent_settings where id=1 and enabled;
 if not found then return; end if;
 select * into typ from public.browser_agent_types where key='user_browser_read' and principal_kind='user' and enabled and provider='openai';
 if not found or not(typ.key=any(w.allowed_types)) or not(coalesce(w.capabilities->'types','{}'::jsonb) ? typ.key)
  or coalesce((p_capacity->>typ.key)::int,0)<1 then return; end if;
 perform pg_advisory_xact_lock(hashtext('bagent:'||typ.key));
 if (select count(*) from public.browser_agent_jobs where agent_type=typ.key and lease_owner is not null)>=typ.max_concurrency
  or exists(select 1 from public.browser_agent_jobs where lease_owner=w.id) then return; end if;
 for candidate in select id,principal_user_id from public.browser_agent_jobs where agent_type=typ.key and principal_kind='user'
  and status='queued' and cancel_requested_at is null and expires_at>now() and coalesce(next_attempt_at,now())<=now()
  order by priority,created_at limit 1 loop
  if not public.assistant_browser_owner_eligible(candidate.principal_user_id,true) then return; end if;
  perform pg_advisory_xact_lock(hashtextextended('assistant-browser-owner:'||candidate.principal_user_id::text,0));
  select * into j from public.browser_agent_jobs where id=candidate.id and status='queued' and expires_at>now()
   and cancel_requested_at is null for update skip locked;
  if j.id is null then return; end if;
  if exists(select 1 from public.browser_agent_jobs where principal_user_id=j.principal_user_id and lease_owner is not null) then return; end if;
  select * into c from public.assistant_credentials where id=j.user_credential_id for share;
  if c.id is null or c.user_id is distinct from j.principal_user_id or c.version is distinct from j.user_credential_version or c.status<>'active'
   or not coalesce(public.assistant_browser_task_origin(j.input->>'url')=any(c.origins),false) then return; end if;
  perform 1 from public.assistant_conversations where id=j.assistant_conversation_id and user_id=j.principal_user_id and mode='normal' and mailbox_tainted_at is null for share;
  if not found then return; end if;
  if j.attempts>=typ.max_attempts or exists(select 1 from public.assistant_browser_turns where job_id=j.id and status in ('begun','uncertain')) then
   update public.browser_agent_jobs set status='failed',error_kind='execution_interrupted',finished_at=now() where id=j.id;
   return;
  end if;
  update public.browser_agent_jobs set status='running',lease_owner=w.id,lease_boot_id=p_boot,lease_epoch=lease_epoch+1,
   lease_expires_at=least(expires_at,now()+make_interval(secs=>least(90,greatest(30,p_lease_seconds)))),attempts=attempts+1,
   started_at=coalesce(started_at,now()),resume_reason=null,next_attempt_at=null,
   config_snapshot=jsonb_build_object('type',to_jsonb(typ)-array['updated_by','updated_at'],'per_job_cost_cap_usd',least(2,cfg.per_job_cost_cap_usd),
   'daily_cost_cap_usd',cfg.daily_cost_cap_usd,'config_version',cfg.config_version,'claimed_at',now())
   where id=j.id returning * into j;
  return next j;
  return;
 end loop;
end $$;

create or replace function public.assistant_browser_user_fence(p_job uuid,p_worker uuid,p_boot uuid,p_epoch bigint)
returns public.browser_agent_jobs language plpgsql security invoker set search_path=pg_catalog,public as $$
declare j public.browser_agent_jobs; w public.browser_agent_workers; c public.assistant_credentials;
begin
 if not public.assistant_browser_runtime_ready() then raise exception 'browser_runtime_unavailable'; end if;
 select * into w from public.browser_agent_workers where id=p_worker for share;
 if w.id is null or w.principal_kind<>'user' or w.status<>'active' or w.boot_id is distinct from p_boot then raise exception 'lease_lost'; end if;
 select * into j from public.browser_agent_jobs where id=p_job and principal_kind='user';
 if j.id is null or not public.assistant_browser_owner_eligible(j.principal_user_id,true) then raise exception 'lease_lost'; end if;
 perform pg_advisory_xact_lock(hashtextextended('assistant-browser-owner:'||j.principal_user_id::text,0));
 select * into j from public.browser_agent_jobs where id=p_job for update;
 if j.principal_kind<>'user' or j.lease_owner is distinct from p_worker or j.lease_boot_id is distinct from p_boot
  or j.lease_epoch is distinct from p_epoch or j.status not in ('running','waiting_help') or j.expires_at<=now()
  or j.lease_expires_at is null or j.lease_expires_at<=now()
  or j.cancel_requested_at is not null or not (j.agent_type=any(w.allowed_types)) then raise exception 'lease_lost'; end if;
 perform 1 from public.browser_agent_types where key=j.agent_type and principal_kind='user' and enabled;
 if not found then raise exception 'lease_lost'; end if;
 perform 1 from public.browser_agent_settings where id=1 and enabled;
 if not found then raise exception 'lease_lost'; end if;
 select * into c from public.assistant_credentials where id=j.user_credential_id for share;
 if c.id is null or c.user_id is distinct from j.principal_user_id or c.version is distinct from j.user_credential_version or c.status<>'active'
  or not coalesce(public.assistant_browser_task_origin(j.input->>'url')=any(c.origins),false) then raise exception 'lease_lost'; end if;
 perform 1 from public.assistant_conversations where id=j.assistant_conversation_id and user_id=j.principal_user_id and mode='normal' and mailbox_tainted_at is null for share;
 if not found then raise exception 'lease_lost'; end if;
 return j;
end $$;

create or replace function public.assistant_browser_login_finish(p_job uuid,p_worker uuid,p_boot uuid,p_epoch bigint,p_attempt_id uuid,p_outcome text,p_session jsonb)
returns jsonb language plpgsql security invoker set search_path=pg_catalog,public as $$
declare j public.browser_agent_jobs;c public.assistant_credentials;s public.assistant_browser_sessions;
begin
 j:=public.assistant_browser_user_fence(p_job,p_worker,p_boot,p_epoch);
 select * into c from public.assistant_credentials where id=j.user_credential_id for update;
 if c.login_attempt_id is distinct from p_attempt_id or c.login_job_id is distinct from j.id or c.login_epoch is distinct from p_epoch
  or c.login_attempt_expires_at<=now() then return '{"ok":false,"code":"attempt_changed"}'::jsonb; end if;
 if p_outcome is null or p_outcome not in ('ok','password_rejected','mfa','captcha','challenge','sso','unexpected','timeout') then return '{"ok":false,"code":"invalid"}'::jsonb; end if;
 -- MFA keeps the exact attempt alive for a one-time code. It never releases a new password.
 if p_outcome in ('mfa','captcha','challenge') then return jsonb_build_object('ok',true,'attempt_id',p_attempt_id,'help_required',true); end if;
 if p_outcome='ok' then
  if p_session->>'origin' is distinct from public.assistant_browser_task_origin(j.input->>'url')
   or not coalesce((p_session->>'origin')=any(c.origins),false) or not public.assistant_browser_record_valid(jsonb_build_object('id',p_session->>'id','kind','snapshot','envelope',p_session->'envelope')) then return '{"ok":false,"code":"session_invalid"}'::jsonb; end if;
  delete from public.assistant_browser_sessions where credential_id=c.id;
  insert into public.assistant_browser_sessions(id,user_id,credential_id,credential_version,origin,version,wrapped_dek,ciphertext,nonce,key_id,expires_at)
  values((p_session->>'id')::uuid,c.user_id,c.id,c.version,p_session->>'origin',1,p_session#>>'{envelope,wrapped_dek}',p_session#>>'{envelope,ciphertext}',p_session#>>'{envelope,nonce}',p_session#>>'{envelope,key_id}',
  least(coalesce((p_session->>'expires_at')::timestamptz,now()+interval '24 hours'),now()+interval '24 hours')) returning * into s;
 end if;
 update public.assistant_credentials set login_attempt_id=null,login_job_id=null,login_epoch=null,login_attempt_expires_at=null,
 login_failures=case when p_outcome='ok' then 0 when p_outcome='password_rejected' then login_failures+1 else login_failures end,
 locked_until=case when p_outcome='ok' then null when p_outcome='password_rejected' and login_failures+1>=3 then now()+interval '24 hours' else locked_until end
 where id=c.id returning * into c;
 update public.assistant_browser_help_state set status='resolved' where job_id=j.id and login_attempt_id=p_attempt_id and status='open';
 update public.assistant_browser_records set envelope=null,consumed_at=coalesce(consumed_at,now()) where job_id=j.id and kind='help_secret' and consumed_at is null;
 return jsonb_build_object('ok',true,'login_failures',c.login_failures,'locked_until',c.locked_until,'session',case when s.id is null then null else to_jsonb(s) end);
end $$;

create or replace function public.assistant_browser_session_get(p_job uuid,p_worker uuid,p_boot uuid,p_epoch bigint,p_origin text)
returns jsonb language plpgsql security invoker set search_path=pg_catalog,public as $$
declare j public.browser_agent_jobs;s public.assistant_browser_sessions;
begin
 j:=public.assistant_browser_user_fence(p_job,p_worker,p_boot,p_epoch);
 if p_origin is distinct from public.assistant_browser_task_origin(j.input->>'url') then return '{"ok":false,"code":"origin_refused"}'::jsonb; end if;
 select * into s from public.assistant_browser_sessions where user_id=j.principal_user_id and credential_id=j.user_credential_id
 and credential_version=j.user_credential_version and origin=p_origin and expires_at>now();
 return jsonb_build_object('ok',true,'session',case when s.id is null then null else to_jsonb(s) end);
end $$;

notify pgrst,'reload schema';
