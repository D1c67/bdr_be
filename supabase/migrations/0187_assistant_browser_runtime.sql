-- Phase 6 runtime persistence. Apply only after 0186. Flags remain off.
create table if not exists public.assistant_browser_records (
 id uuid primary key, user_id uuid not null references public.profiles(id) on delete cascade,
 job_id uuid not null references public.browser_agent_jobs(id) on delete cascade,
 kind text not null check(kind in ('step','turn','request','response','result','snapshot','screenshot','help_request','help_message','help_secret')),
 seq int not null default 0 check(seq>=0), envelope jsonb,
 help_id uuid, client_msg_id uuid, consumed_at timestamptz, secret_hash text,secret_key_id text,
 check((kind='help_secret' and secret_hash ~ '^[0-9a-f]{64}$' and secret_hash is not null and secret_key_id is not null and length(secret_key_id) between 1 and 80) or (kind<>'help_secret' and secret_hash is null and secret_key_id is null)),
 created_at timestamptz not null default now(), expires_at timestamptz not null default now()+interval '90 days',
 check((envelope is not null and jsonb_typeof(envelope)='object') or (kind='help_secret' and consumed_at is not null))
);
create index if not exists assistant_browser_records_page on public.assistant_browser_records(user_id,job_id,created_at desc,id desc);
create unique index if not exists assistant_browser_records_message_uq on public.assistant_browser_records(help_id,client_msg_id) where client_msg_id is not null;
create index if not exists assistant_browser_records_expiry on public.assistant_browser_records(expires_at);
create table if not exists public.assistant_browser_help_state (
 id uuid primary key, user_id uuid not null references public.profiles(id) on delete cascade,
 job_id uuid not null references public.browser_agent_jobs(id) on delete cascade,
 request_record_id uuid not null references public.assistant_browser_records(id) on delete cascade,
 status text not null default 'open' check(status in ('open','resolved','canceled')),
 login_attempt_id uuid, is_login_scope boolean not null default false,
 created_at timestamptz not null default now(), expires_at timestamptz not null,
 check(is_login_scope=(login_attempt_id is not null))
);
create unique index if not exists assistant_browser_help_open_uq on public.assistant_browser_help_state(job_id) where status='open';
create table if not exists public.assistant_browser_turns (
 id uuid primary key default gen_random_uuid(),user_id uuid not null references public.profiles(id) on delete cascade,
 job_id uuid not null references public.browser_agent_jobs(id) on delete cascade,epoch bigint not null,turn_no int not null check(turn_no>=0),
 token uuid not null default gen_random_uuid(),status text not null default 'begun' check(status in ('begun','done','uncertain')),
 estimated_cost numeric not null check(estimated_cost>=0),actual_cost numeric,estimated_tokens int not null default 0,actual_tokens int,
 request_envelope jsonb,response_envelope jsonb,
 created_at timestamptz not null default now(),finished_at timestamptz,unique(job_id,turn_no)
);
create unique index if not exists assistant_browser_turns_begun_uq on public.assistant_browser_turns(job_id) where status='begun';
create table if not exists public.assistant_browser_notices (
 id uuid primary key default gen_random_uuid(),user_id uuid not null references public.profiles(id) on delete cascade,
 job_id uuid not null references public.browser_agent_jobs(id) on delete cascade,
 notice_key text not null,notification_id uuid not null references public.notifications(id) on delete cascade,
 email_claimed_at timestamptz,created_at timestamptz not null default now(),unique(job_id,notice_key)
);
do $$ declare t text; begin
 foreach t in array array['assistant_browser_records','assistant_browser_help_state','assistant_browser_turns','assistant_browser_notices'] loop
  execute format('alter table public.%I enable row level security',t);
  execute format('alter table public.%I force row level security',t);
  execute format('revoke all on public.%I from public,anon,authenticated',t);
  execute format('grant select,insert,update,delete on public.%I to service_role',t);
 end loop;
end $$;

-- All new user worker paths use this fence. Lock order: worker, live profile/
-- authority rows, per-owner mutex, job, credential. Profile mutation acquires
-- profile then owner mutex; no owner mutex holder waits for the profile row.
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
 if c.id is null or c.user_id is distinct from j.principal_user_id or c.version is distinct from j.user_credential_version or c.status<>'active' then raise exception 'lease_lost'; end if;
 perform 1 from public.assistant_conversations where id=j.assistant_conversation_id and user_id=j.principal_user_id and mode='normal' and mailbox_tainted_at is null for share;
 if not found then raise exception 'lease_lost'; end if;
 return j;
end $$;

create or replace function public.assistant_browser_record_valid(p_record jsonb)
returns boolean language sql immutable security invoker set search_path=pg_catalog,public as $$
 select coalesce(jsonb_typeof(p_record)='object' and p_record->>'id' is not null
  and p_record->>'kind' in ('step','turn','request','response','result','snapshot','screenshot','help_request','help_message','help_secret')
  and jsonb_typeof(p_record->'envelope')='object'
  and length(p_record::text)<=16000000
  and (p_record->'envelope') ?& array['wrapped_dek','ciphertext','nonce','key_id'],false)
$$;
create or replace function public.assistant_browser_record_insert(p_user_id uuid,p_job_id uuid,p_record jsonb,p_help_id uuid default null,p_client_msg_id uuid default null)
returns public.assistant_browser_records language plpgsql security invoker set search_path=pg_catalog,public as $$
declare r public.assistant_browser_records;
begin
 if not public.assistant_browser_record_valid(p_record) then raise exception 'record_invalid'; end if;
 select * into r from public.assistant_browser_records where id=(p_record->>'id')::uuid;
 if found then
  if r.user_id is distinct from p_user_id or r.job_id is distinct from p_job_id or r.kind is distinct from p_record->>'kind'
   or r.seq is distinct from coalesce((p_record->>'seq')::int,0) or r.envelope is distinct from p_record->'envelope'
   or r.help_id is distinct from p_help_id or r.client_msg_id is distinct from p_client_msg_id
   or r.secret_hash is distinct from p_record->>'secret_hash' or r.secret_key_id is distinct from p_record->>'secret_key_id' then raise exception 'record_conflict'; end if;
  return r;
 end if;
 insert into public.assistant_browser_records(id,user_id,job_id,kind,seq,envelope,help_id,client_msg_id,secret_hash,secret_key_id,expires_at)
 values((p_record->>'id')::uuid,p_user_id,p_job_id,p_record->>'kind',coalesce((p_record->>'seq')::int,0),p_record->'envelope',p_help_id,p_client_msg_id,p_record->>'secret_hash',p_record->>'secret_key_id',
 least(coalesce((p_record->>'expires_at')::timestamptz,now()+interval '90 days'),now()+interval '90 days')) returning * into r;
 return r;
end $$;
create or replace function public.assistant_browser_record_put(p_user_id uuid,p_job_id uuid,p_worker_id uuid,p_boot_id uuid,p_epoch bigint,p_record jsonb)
returns jsonb language plpgsql security invoker set search_path=pg_catalog,public as $$
declare j public.browser_agent_jobs;r public.assistant_browser_records;
begin
 j:=public.assistant_browser_user_fence(p_job_id,p_worker_id,p_boot_id,p_epoch);
 if j.principal_user_id is distinct from p_user_id or p_record->>'kind' not in ('step','turn','snapshot','screenshot','request','response','result') then return '{"ok":false,"code":"forbidden"}'::jsonb; end if;
 r:=public.assistant_browser_record_insert(p_user_id,p_job_id,p_record);
 return jsonb_build_object('ok',true,'record',to_jsonb(r));
end $$;

create or replace function public.assistant_browser_notice(p_job_id uuid,p_notice_key text,p_type text,p_text text)
returns void language plpgsql security invoker set search_path=pg_catalog,public as $$
declare j public.browser_agent_jobs; n uuid:=gen_random_uuid();
begin
 select * into j from public.browser_agent_jobs where id=p_job_id and principal_kind='user';
 if j.id is null or not exists(select 1 from public.profiles where id=j.principal_user_id and is_active=true) then return; end if;
 if exists(select 1 from public.assistant_browser_notices where job_id=j.id and notice_key=p_notice_key) then return; end if;
 insert into public.notifications(id,user_id,type,message,metadata)
 values(n,j.principal_user_id,p_type,p_text,jsonb_build_object('browser_job_id',j.id));
 insert into public.assistant_browser_notices(user_id,job_id,notice_key,notification_id) values(j.principal_user_id,j.id,p_notice_key,n);
 insert into public.assistant_inbox(user_id,dedupe_key,title,why,payload,delivered_at)
 values(j.principal_user_id,'browser:'||j.id::text||':'||p_notice_key,p_text,null,jsonb_build_object('browser_job_id',j.id),now()) on conflict(user_id,dedupe_key) do nothing;
end $$;
create or replace function public.assistant_browser_notice_claim(p_limit int default 100)
returns jsonb language plpgsql security invoker set search_path=pg_catalog,public as $$
declare rows jsonb;
begin
 with picked as (select id from public.assistant_browser_notices where email_claimed_at is null order by created_at for update skip locked limit greatest(1,least(p_limit,100))),
 claimed as(update public.assistant_browser_notices n set email_claimed_at=now() from picked p where n.id=p.id returning n.notification_id)
 select coalesce(jsonb_agg(to_jsonb(n)),'[]'::jsonb) into rows from public.notifications n join claimed c on n.id=c.notification_id;
 return jsonb_build_object('rows',rows);
end $$;

create or replace function public.assistant_browser_login_begin(p_job uuid,p_worker uuid,p_boot uuid,p_epoch bigint,p_origin text)
returns jsonb language plpgsql security invoker set search_path=pg_catalog,public as $$
declare j public.browser_agent_jobs;c public.assistant_credentials;attempt uuid:=gen_random_uuid();
begin
 j:=public.assistant_browser_user_fence(p_job,p_worker,p_boot,p_epoch);
 select * into c from public.assistant_credentials where id=j.user_credential_id for update;
 if p_origin is distinct from 'https://login.procore.com' or not(p_origin=any(c.origins)) then return '{"ok":false,"code":"origin_refused"}'::jsonb; end if;
 if c.locked_until>now() then return jsonb_build_object('ok',true,'granted',false,'code','locked','retry_at',c.locked_until); end if;
 if c.login_attempt_id is not null and c.login_attempt_expires_at>now() then
  return jsonb_build_object('ok',true,'granted',false,'code','login_busy','retry_at',c.login_attempt_expires_at);
 end if;
 if c.last_login_attempt_at>now()-interval '600 seconds' then
  return jsonb_build_object('ok',true,'granted',false,'code','spacing','retry_at',c.last_login_attempt_at+interval '600 seconds');
 end if;
 update public.assistant_credentials set login_attempt_id=attempt,login_job_id=j.id,login_epoch=p_epoch,
  login_attempt_expires_at=least(j.expires_at,now()+interval '10 minutes'),last_login_attempt_at=now() where id=c.id returning * into c;
 return jsonb_build_object('ok',true,'granted',true,'attempt_id',attempt,'credential',to_jsonb(c));
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
  if p_session->>'origin' is distinct from 'https://app.procore.com' or not public.assistant_browser_record_valid(jsonb_build_object('id',p_session->>'id','kind','snapshot','envelope',p_session->'envelope')) then return '{"ok":false,"code":"session_invalid"}'::jsonb; end if;
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
 if p_origin is distinct from 'https://app.procore.com' then return '{"ok":false,"code":"origin_refused"}'::jsonb; end if;
 select * into s from public.assistant_browser_sessions where user_id=j.principal_user_id and credential_id=j.user_credential_id
 and credential_version=j.user_credential_version and origin=p_origin and expires_at>now();
 return jsonb_build_object('ok',true,'session',case when s.id is null then null else to_jsonb(s) end);
end $$;
-- The old unfenced session scaffold must never become an identity write API.
revoke execute on function public.assistant_browser_session_put(uuid,uuid,int,text,int,jsonb) from service_role;

create or replace function public.assistant_browser_turn_begin(p_user_id uuid,p_job uuid,p_worker uuid,p_boot uuid,p_epoch bigint,p_turn_no int,p_estimated_cost numeric,p_estimated_tokens int default 0)
returns jsonb language plpgsql security invoker set search_path=pg_catalog,public as $$
declare j public.browser_agent_jobs;t public.assistant_browser_turns;typ public.browser_agent_types;caps jsonb;
begin
 j:=public.assistant_browser_user_fence(p_job,p_worker,p_boot,p_epoch);
 if j.principal_user_id is distinct from p_user_id then return '{"ok":false,"code":"forbidden"}'::jsonb; end if;
 select * into t from public.assistant_browser_turns where job_id=j.id and turn_no=p_turn_no;
 if found then return jsonb_build_object('ok',true,'claimed',false,'turn_id',t.id,'token',null,'code',case when t.status='begun' then 'thinking' else t.status end,
 'replay_envelope',t.response_envelope,'job',to_jsonb(j)); end if;
 if p_turn_no is null or p_turn_no<>j.turn_count+1 or p_estimated_cost is null or p_estimated_cost<0 or p_estimated_tokens is null or p_estimated_tokens<=0
  or exists(select 1 from public.assistant_browser_turns where job_id=j.id and status in ('begun','uncertain')) then return '{"ok":false,"code":"turn_conflict"}'::jsonb; end if;
 select * into typ from public.browser_agent_types where key=j.agent_type for share;
 if typ.provider is distinct from 'openai' or typ.model is distinct from j.input#>>'{authority,model}' or j.turn_count>=typ.max_steps or j.active_seconds>=typ.max_active_seconds or j.tokens_in+p_estimated_tokens>typ.max_total_tokens then return '{"ok":false,"code":"type_unavailable"}'::jsonb; end if;
 perform pg_advisory_xact_lock(hashtext('assistant-browser-model-spend'));
 caps:=public.browser_agent_spend_today();
 if j.cost_usd+p_estimated_cost>least(2,coalesce((j.input->>'max_cost_usd')::numeric,2),(caps->>'per_job_cap_usd')::numeric)
 or (caps->>'spend_usd')::numeric+p_estimated_cost>(caps->>'daily_cap_usd')::numeric then return '{"ok":false,"code":"cost_cap"}'::jsonb; end if;
 insert into public.assistant_browser_turns(user_id,job_id,epoch,turn_no,estimated_cost,estimated_tokens)
 values(p_user_id,j.id,p_epoch,p_turn_no,p_estimated_cost,p_estimated_tokens) returning * into t;
 update public.browser_agent_jobs set cost_usd=cost_usd+p_estimated_cost,tokens_in=tokens_in+p_estimated_tokens,turn_count=turn_count+1 where id=j.id returning * into j;
 return jsonb_build_object('ok',true,'claimed',true,'turn_id',t.id,'token',t.token,'replay_envelope',null,'job',to_jsonb(j),'type',to_jsonb(typ));
end $$;
create or replace function public.assistant_browser_turn_finish(p_user_id uuid,p_job uuid,p_worker uuid,p_boot uuid,p_epoch bigint,
 p_turn_id uuid,p_token uuid,p_actual_cost numeric,p_response_envelope jsonb,p_request_envelope jsonb,p_actual_tokens int default null)
returns jsonb language plpgsql security invoker set search_path=pg_catalog,public as $$
declare j public.browser_agent_jobs;t public.assistant_browser_turns;
begin
 j:=public.assistant_browser_user_fence(p_job,p_worker,p_boot,p_epoch);
 if j.principal_user_id is distinct from p_user_id then return '{"ok":false,"code":"forbidden"}'::jsonb; end if;
 select * into t from public.assistant_browser_turns where id=p_turn_id and user_id=p_user_id and job_id=p_job for update;
 if t.id is null or t.token is distinct from p_token or t.epoch is distinct from p_epoch then return '{"ok":false,"code":"turn_conflict"}'::jsonb; end if;
 if t.status='done' then return jsonb_build_object('ok',true,'changed',false,'replay_envelope',t.response_envelope); end if;
 if t.status<>'begun' or p_actual_cost is null or p_actual_cost<0 or p_actual_cost>t.estimated_cost or coalesce(p_actual_tokens,t.estimated_tokens)<0 or coalesce(p_actual_tokens,t.estimated_tokens)>t.estimated_tokens
  or jsonb_typeof(p_response_envelope) is distinct from 'object' or jsonb_typeof(p_request_envelope) is distinct from 'object'
  or not(p_response_envelope ?& array['wrapped_dek','ciphertext','nonce','key_id'])
  or not(p_request_envelope ?& array['wrapped_dek','ciphertext','nonce','key_id']) then return '{"ok":false,"code":"turn_invalid"}'::jsonb; end if;
 update public.assistant_browser_turns set status='done',actual_cost=p_actual_cost,actual_tokens=coalesce(p_actual_tokens,t.estimated_tokens),response_envelope=p_response_envelope,
 request_envelope=p_request_envelope,finished_at=now() where id=t.id;
 update public.browser_agent_jobs set cost_usd=greatest(0,cost_usd-t.estimated_cost+p_actual_cost),tokens_in=greatest(0,tokens_in-t.estimated_tokens+coalesce(p_actual_tokens,t.estimated_tokens)) where id=j.id;
 return '{"ok":true,"changed":true}'::jsonb;
end $$;

create or replace function public.assistant_browser_help_open(p_job uuid,p_worker uuid,p_boot uuid,p_epoch bigint,p_help_id uuid,p_record jsonb,p_login_attempt_id uuid)
returns jsonb language plpgsql security invoker set search_path=pg_catalog,public as $$
declare j public.browser_agent_jobs;h public.assistant_browser_help_state;r public.assistant_browser_records;c public.assistant_credentials;
begin
 j:=public.assistant_browser_user_fence(p_job,p_worker,p_boot,p_epoch);
 select * into h from public.assistant_browser_help_state where job_id=j.id and status='open';
 if found then return jsonb_build_object('ok',true,'created',false,'help',to_jsonb(h)); end if;
 if p_help_id is null or p_record->>'kind' is distinct from 'help_request' then return '{"ok":false,"code":"invalid"}'::jsonb; end if;
 if p_login_attempt_id is not null then
  select * into c from public.assistant_credentials where id=j.user_credential_id;
  if c.login_attempt_id is distinct from p_login_attempt_id or c.login_job_id is distinct from j.id
   or c.login_epoch is distinct from p_epoch or c.login_attempt_expires_at<=now() then return '{"ok":false,"code":"attempt_changed"}'::jsonb; end if;
 end if;
 r:=public.assistant_browser_record_insert(j.principal_user_id,j.id,p_record,p_help_id,null);
 insert into public.assistant_browser_help_state(id,user_id,job_id,request_record_id,login_attempt_id,is_login_scope,expires_at)
 values(p_help_id,j.principal_user_id,j.id,r.id,p_login_attempt_id,p_login_attempt_id is not null,
 least(j.expires_at,coalesce(c.login_attempt_expires_at,j.expires_at))) returning * into h;
 update public.browser_agent_jobs set status='waiting_help' where id=j.id;
 perform public.assistant_browser_notice(j.id,'help:'||h.id::text,'assistant.browser_help','Your browser task needs your help.');
 return jsonb_build_object('ok',true,'created',true,'help',to_jsonb(h));
end $$;
create or replace function public.assistant_browser_help_reply(p_user_id uuid,p_job_id uuid,p_help_id uuid,p_client_msg_id uuid,p_action text,p_record jsonb)
returns jsonb language plpgsql security invoker set search_path=pg_catalog,public as $$
declare j public.browser_agent_jobs;h public.assistant_browser_help_state;r public.assistant_browser_records;c public.assistant_credentials;
begin
 if not public.assistant_browser_owner_eligible(p_user_id,true) then return '{"ok":false,"code":"forbidden"}'::jsonb; end if;
 perform pg_advisory_xact_lock(hashtextextended('assistant-browser-owner:'||p_user_id::text,0));
 select * into j from public.browser_agent_jobs where id=p_job_id and principal_kind='user' and principal_user_id=p_user_id for update;
 select * into h from public.assistant_browser_help_state where id=p_help_id and job_id=p_job_id and user_id=p_user_id for update;
 if j.id is null or h.id is null or p_client_msg_id is null or p_action is null or p_action not in ('message','code','retry','give_up') then return '{"ok":false,"code":"help_changed"}'::jsonb; end if;
 select * into r from public.assistant_browser_records where help_id=h.id and client_msg_id=p_client_msg_id;
 if found then return jsonb_build_object('ok',true,'created',false,'record',to_jsonb(r),'help',to_jsonb(h)); end if;
 if h.status<>'open' or h.expires_at<=now() or j.status not in ('running','waiting_help') then return '{"ok":false,"code":"help_changed"}'::jsonb; end if;
 if (select count(*) from public.assistant_browser_records where help_id=h.id and client_msg_id is not null)>=100 then return '{"ok":false,"code":"help_limit"}'::jsonb; end if;
 if p_action='code' then
  if exists(select 1 from public.assistant_browser_records where help_id=h.id and kind='help_secret') then return '{"ok":false,"code":"code_already_supplied"}'::jsonb; end if;
  select * into c from public.assistant_credentials where id=j.user_credential_id for update;
  if not h.is_login_scope or c.status<>'active' or c.version<>j.user_credential_version or c.login_attempt_id is distinct from h.login_attempt_id
   or c.login_job_id is distinct from j.id or c.login_epoch is distinct from j.lease_epoch or c.login_attempt_expires_at<=now()
   or j.lease_owner is null or j.lease_expires_at<=now() then return '{"ok":false,"code":"attempt_changed"}'::jsonb; end if;
  if p_record->>'kind' is distinct from 'help_secret' then return '{"ok":false,"code":"invalid"}'::jsonb; end if;
 else
  if p_record->>'kind' is distinct from 'help_message' then return '{"ok":false,"code":"invalid"}'::jsonb; end if;
 end if;
 r:=public.assistant_browser_record_insert(p_user_id,j.id,p_record,h.id,p_client_msg_id);
 if p_action='give_up' then
  update public.assistant_browser_help_state set status='canceled' where id=h.id returning * into h;
  update public.browser_agent_jobs set cancel_requested_at=now() where id=j.id;
 elsif p_action in ('retry','message') then
  update public.assistant_browser_help_state set status='resolved' where id=h.id returning * into h;
  update public.browser_agent_jobs set status=case when lease_owner is null then 'queued' else 'running' end,next_attempt_at=null where id=j.id;
 end if;
 return jsonb_build_object('ok',true,'created',true,'record',to_jsonb(r),'help',to_jsonb(h));
end $$;
create or replace function public.assistant_browser_help_consume(p_job uuid,p_worker uuid,p_boot uuid,p_epoch bigint,p_help_id uuid,p_record_id uuid,p_login_attempt_id uuid)
returns jsonb language plpgsql security invoker set search_path=pg_catalog,public as $$
declare j public.browser_agent_jobs;h public.assistant_browser_help_state;r public.assistant_browser_records;c public.assistant_credentials;
begin
 j:=public.assistant_browser_user_fence(p_job,p_worker,p_boot,p_epoch);
 select * into h from public.assistant_browser_help_state where id=p_help_id and job_id=j.id and status='open' and expires_at>now() for update;
 select * into c from public.assistant_credentials where id=j.user_credential_id for update;
 if h.id is null or not h.is_login_scope or h.login_attempt_id is distinct from p_login_attempt_id
  or c.login_attempt_id is distinct from p_login_attempt_id or c.login_job_id is distinct from j.id or c.login_epoch is distinct from p_epoch
  or c.login_attempt_expires_at<=now() then return '{"ok":false,"code":"attempt_changed"}'::jsonb; end if;
 select * into r from public.assistant_browser_records where id=p_record_id and job_id=j.id and help_id=h.id and kind='help_secret' and consumed_at is null for update;
 if r.id is null then return '{"ok":false,"code":"already_consumed"}'::jsonb; end if;
 update public.assistant_browser_records set envelope=null,consumed_at=now() where id=r.id;
 return jsonb_build_object('ok',true,'record',to_jsonb(r));
end $$;

create or replace function public.assistant_browser_inspect(p_viewer_id uuid,p_user_id uuid,p_job_id uuid,p_reason text,p_kind text default 'all')
returns jsonb language plpgsql security invoker set search_path=pg_catalog,public as $$
declare v_label text;v_audit uuid;v_rows jsonb;
begin
 perform 1 from public.profiles where id=p_viewer_id and is_active=true and role::text='it_admin' for share;
 if not found or length(btrim(coalesce(p_reason,''))) not between 10 and 500 then return '{"ok":false,"code":"forbidden"}'::jsonb; end if;
 perform 1 from public.browser_agent_jobs where id=p_job_id and principal_kind='user' and principal_user_id=p_user_id;
 if not found then return '{"ok":false,"code":"not_found"}'::jsonb; end if;
 select coalesce(full_name,'Former user') into v_label from public.profiles where id=p_user_id;
 insert into public.assistant_browser_access(user_id,user_label,viewer_id,resource_kind,resource_id,reason)
 values(p_user_id,coalesce(v_label,'Former user'),p_viewer_id,'job',p_job_id,btrim(p_reason)) returning id into v_audit;
 select coalesce(jsonb_agg(to_jsonb(x)),'[]'::jsonb) into v_rows from
 (select * from public.assistant_browser_records where user_id=p_user_id and job_id=p_job_id and kind<>'help_secret'
  and (p_kind='all' or kind=p_kind) order by created_at desc,id desc limit 100) x;
 return jsonb_build_object('ok',true,'audit_id',v_audit,'records',v_rows);
end $$;

-- Terminal transition accounting also covers credential/profile cancellation.
create or replace function public.assistant_browser_terminal()
returns trigger language plpgsql security invoker set search_path=pg_catalog,public as $$
begin
 if new.principal_kind<>'user' or new.status not in ('succeeded','failed','canceled','expired')
  or old.status in ('succeeded','failed','canceled','expired') then return new; end if;
 update public.assistant_browser_turns set status='uncertain',finished_at=now() where job_id=new.id and status='begun';
 update public.assistant_spend_reservations set status=case when new.status='succeeded' then 'settled' else 'released' end,
  actual_usd=new.cost_usd,settled_at=now() where id=new.assistant_reservation_id and user_id=new.principal_user_id and status='active';
 update public.assistant_browser_help_state set status='canceled' where job_id=new.id and status='open';
 update public.assistant_browser_records set envelope=null,consumed_at=coalesce(consumed_at,now()) where job_id=new.id and kind='help_secret' and consumed_at is null;
 update public.assistant_credentials set login_attempt_id=null,login_job_id=null,login_epoch=null,login_attempt_expires_at=null where login_job_id=new.id;
 perform public.assistant_browser_notice(new.id,'terminal','assistant.browser_task',case when new.status='expired' then
 'Your browser task expired while waiting for a worker.' when new.status='succeeded' then 'Your browser task finished.' else 'Your browser task stopped.' end);
 return new;
end $$;
do $$ begin
 if not exists(select 1 from pg_trigger where tgname='assistant_browser_terminal' and tgrelid='public.browser_agent_jobs'::regclass) then
 create trigger assistant_browser_terminal after update of status on public.browser_agent_jobs for each row execute function public.assistant_browser_terminal(); end if;
end $$;
create or replace function public.assistant_browser_job_finish(p_job uuid,p_worker uuid,p_boot uuid,p_epoch bigint,p_status text,p_result_record jsonb,p_error_code text default null)
returns jsonb language plpgsql security invoker set search_path=pg_catalog,public as $$
declare j public.browser_agent_jobs;r public.assistant_browser_records;
begin
 j:=public.assistant_browser_user_fence(p_job,p_worker,p_boot,p_epoch);
 if p_status is null or p_status not in ('succeeded','failed','canceled') then return '{"ok":false,"code":"invalid"}'::jsonb; end if;
 if p_result_record is not null then
  if p_result_record->>'kind' is distinct from 'result' then return '{"ok":false,"code":"invalid"}'::jsonb; end if;
  r:=public.assistant_browser_record_insert(j.principal_user_id,j.id,p_result_record);
 end if;
 update public.browser_agent_jobs set status=p_status,finished_at=now(),lease_owner=null,lease_boot_id=null,lease_expires_at=null,
  hold_until=null,result=case when r.id is null then '{}'::jsonb else jsonb_build_object('record_id',r.id) end,
  error_kind=case when p_status='failed' then 'user_browser_failed' else null end,error=null
 where id=j.id returning * into j;
 return jsonb_build_object('ok',true,'job',to_jsonb(j));
end $$;
create or replace function public.assistant_browser_job_heartbeat(p_job uuid,p_worker uuid,p_boot uuid,p_epoch bigint,p_active_seconds_delta int default 0)
returns jsonb language plpgsql security invoker set search_path=pg_catalog,public as $$
declare j public.browser_agent_jobs;h uuid;
begin
 j:=public.assistant_browser_user_fence(p_job,p_worker,p_boot,p_epoch);
 update public.browser_agent_jobs set lease_expires_at=least(expires_at,now()+interval '90 seconds'),
 active_seconds=active_seconds+greatest(0,least(coalesce(p_active_seconds_delta,0),30)) where id=j.id returning * into j;
 select id into h from public.assistant_browser_help_state where job_id=j.id and status='open' and expires_at>now();
 return jsonb_build_object('ok',true,'job',to_jsonb(j),'help_id',h);
end $$;
create or replace function public.assistant_browser_worker_ping(p_worker uuid,p_boot uuid)
returns boolean language plpgsql security invoker set search_path=pg_catalog,public as $$
begin
 update public.browser_agent_workers set last_seen_at=now() where id=p_worker and boot_id=p_boot and principal_kind='user' and status='active';
 return found;
end $$;
create or replace function public.assistant_browser_job_requeue(p_job uuid,p_worker uuid,p_boot uuid,p_epoch bigint,p_retry_at timestamptz default null,p_reason text default 'interrupted')
returns jsonb language plpgsql security invoker set search_path=pg_catalog,public as $$
declare j public.browser_agent_jobs;
begin
 j:=public.assistant_browser_user_fence(p_job,p_worker,p_boot,p_epoch);
 if exists(select 1 from public.assistant_browser_turns where job_id=j.id and status='begun') then
  update public.browser_agent_jobs set status='failed',error_kind='turn_uncertain',finished_at=now(),lease_owner=null,lease_boot_id=null,lease_expires_at=null where id=j.id;
 else
  update public.browser_agent_jobs set status=case when exists(select 1 from public.assistant_browser_help_state where job_id=j.id and status='open' and expires_at>now()) then 'waiting_help' else 'queued' end,
   resume_reason=case when p_reason in ('login_spacing','provider_outage') then p_reason else 'interrupted' end,
   next_attempt_at=least(expires_at,greatest(now()+interval '30 seconds',coalesce(p_retry_at,now()))),
   lease_owner=null,lease_boot_id=null,lease_expires_at=null where id=j.id;
 end if;
 return '{"ok":true}'::jsonb;
end $$;
create or replace function public.assistant_browser_sweep(p_limit int default 100)
returns jsonb language plpgsql security invoker set search_path=pg_catalog,public as $$
declare candidate record;j public.browser_agent_jobs;n int:=0;
begin
 for candidate in select id,principal_user_id from public.browser_agent_jobs where principal_kind='user' and status in ('queued','running','waiting_help')
 and (expires_at<=now() or cancel_requested_at is not null or (lease_owner is not null and lease_expires_at<=now()) or exists(select 1 from public.assistant_browser_help_state h where h.job_id=browser_agent_jobs.id and h.status='open' and h.expires_at<=now())) order by created_at limit greatest(1,least(p_limit,100)) loop
  perform pg_advisory_xact_lock(hashtextextended('assistant-browser-owner:'||candidate.principal_user_id::text,0));
  select * into j from public.browser_agent_jobs where id=candidate.id for update skip locked;
  if j.id is null or j.status not in ('queued','running','waiting_help') then continue; end if;
  if j.expires_at<=now() or j.cancel_requested_at is not null or exists(select 1 from public.assistant_browser_help_state h where h.job_id=j.id and h.status='open' and h.expires_at<=now()) or exists(select 1 from public.assistant_browser_turns where job_id=j.id and status='begun') then
   update public.browser_agent_jobs set status=case when expires_at<=now() then 'expired' when cancel_requested_at is not null then 'canceled' else 'failed' end,
    error_kind=case when expires_at<=now() then 'offline_expired' else 'execution_interrupted' end,finished_at=now(),
    lease_owner=null,lease_boot_id=null,lease_expires_at=null,hold_until=null where id=j.id;
  elsif j.lease_owner is not null and j.lease_expires_at<=now() then
   update public.browser_agent_jobs set status='queued',resume_reason='interrupted',lease_owner=null,lease_boot_id=null,lease_expires_at=null where id=j.id;
  end if;
  n:=n+1;
 end loop;
 -- Expired one-time codes are destroyed even while audit content is on hold.
 update public.assistant_browser_help_state set status='canceled' where status='open' and expires_at<=now();
 update public.assistant_browser_records r set envelope=null,consumed_at=now() where r.kind='help_secret' and r.consumed_at is null
 and exists(select 1 from public.assistant_browser_help_state h where h.id=r.help_id and (h.status<>'open' or h.expires_at<=now()));
 -- Cookie jars are authentication material, never subject to trace legal hold.
 with expired as(select id from public.assistant_browser_sessions where expires_at<=now()
  order by expires_at for update skip locked limit greatest(1,least(p_limit,1000)))
 delete from public.assistant_browser_sessions s using expired e where s.id=e.id;
 return jsonb_build_object('processed',n);
end $$;
create or replace function public.assistant_browser_records_purge(p_limit int default 100)
returns jsonb language plpgsql security invoker set search_path=pg_catalog,public as $$
declare owner uuid;n int:=0;t int:=0;changed int;held boolean;
begin
 for owner in select distinct user_id from public.assistant_browser_records where expires_at<=now()
  union select distinct user_id from public.assistant_browser_turns where created_at<now()-interval '90 days'
  order by 1 limit greatest(1,least(p_limit,100)) loop
  perform 1 from public.profiles where id=owner for share;
  if not found then continue; end if;
  -- Materialize absent preferences before locking; a concurrent legal-hold
  -- upsert then serializes on the same row instead of racing NOT EXISTS.
  insert into public.assistant_profiles(user_id) values(owner) on conflict(user_id) do nothing;
  select legal_hold into held from public.assistant_profiles where user_id=owner for share;
  if held then continue; end if;
  with expired as(select id from public.assistant_browser_records where user_id=owner and expires_at<=now()
   order by expires_at for update skip locked limit greatest(1,least(p_limit,1000)))
  delete from public.assistant_browser_records r using expired e where r.id=e.id;
  get diagnostics changed=row_count;n:=n+changed;
  with expired as(select id from public.assistant_browser_turns where user_id=owner and created_at<now()-interval '90 days'
   order by created_at for update skip locked limit greatest(1,least(p_limit,1000)))
  delete from public.assistant_browser_turns r using expired e where r.id=e.id;
  get diagnostics changed=row_count;t:=t+changed;
 end loop;
 return jsonb_build_object('records',n,'turns',t);
end $$;

do $$ declare r record; begin
 for r in select p.oid::regprocedure as signature from pg_proc p join pg_namespace n on n.oid=p.pronamespace
 where n.nspname='public' and p.proname like 'assistant_browser_%' and p.proname<>'assistant_browser_session_put' loop
  execute format('revoke all on function %s from public,anon,authenticated',r.signature);
  execute format('grant execute on function %s to service_role',r.signature);
 end loop;
end $$;
notify pgrst,'reload schema';
