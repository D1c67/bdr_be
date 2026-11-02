-- Phase 6 foundation. User execution is deliberately unavailable until encrypted
-- traces and the signed user worker have been implemented and verified.
create table if not exists public.assistant_credentials (
 id uuid primary key,
 user_id uuid not null references public.profiles(id) on delete cascade,
 service text not null check (service in ('procore')),
 label text not null check (length(label) between 1 and 100),
 origins text[] not null,
 username text not null check (length(username) between 1 and 320),
 wrapped_dek text, ciphertext text, nonce text, key_id text,
 version int not null check (version > 0),
 fingerprint text not null,
 status text not null default 'active' check (status in ('active','revoked')),
 login_failures int not null default 0 check (login_failures >= 0),
 last_login_attempt_at timestamptz, locked_until timestamptz,
 login_attempt_id uuid, login_job_id uuid, login_epoch bigint,
 login_attempt_expires_at timestamptz,
 created_at timestamptz not null default now(), updated_at timestamptz not null default now(),
 unique(user_id,service),
 constraint assistant_credentials_material check (
   (status='active' and wrapped_dek is not null and ciphertext is not null and nonce is not null and key_id is not null)
   or (status='revoked' and wrapped_dek is null and ciphertext is null and nonce is null and key_id is null))
);
create table if not exists public.assistant_browser_sessions (
 id uuid primary key default gen_random_uuid(),
 user_id uuid not null references public.profiles(id) on delete cascade,
 credential_id uuid not null references public.assistant_credentials(id) on delete cascade,
 credential_version int not null check (credential_version>0),
 origin text not null, version int not null check (version>0),
 wrapped_dek text not null, ciphertext text not null, nonce text not null, key_id text not null,
 expires_at timestamptz not null,
 created_at timestamptz not null default now(), updated_at timestamptz not null default now(),
 unique(credential_id,origin)
);
create index if not exists assistant_browser_sessions_user_idx on public.assistant_browser_sessions(user_id);
create table if not exists public.assistant_browser_access (
 id uuid primary key default gen_random_uuid(), user_id uuid not null, user_label text not null,
 viewer_id uuid not null, resource_kind text not null, resource_id uuid not null,
 reason text not null check (length(btrim(reason)) between 10 and 500),
 created_at timestamptz not null default now()
);
create index if not exists assistant_browser_access_user_idx on public.assistant_browser_access(user_id,created_at desc,id desc);

alter table public.assistant_credentials enable row level security;
alter table public.assistant_credentials force row level security;
alter table public.assistant_browser_sessions enable row level security;
alter table public.assistant_browser_sessions force row level security;
alter table public.assistant_browser_access enable row level security;
alter table public.assistant_browser_access force row level security;
revoke all on public.assistant_credentials,public.assistant_browser_sessions,public.assistant_browser_access from public,anon,authenticated;
grant select,insert,update,delete on public.assistant_credentials,public.assistant_browser_sessions to service_role;
grant select,insert on public.assistant_browser_access to service_role;
do $$ begin
 if not exists(select 1 from pg_trigger where tgname='assistant_browser_access_immutable' and tgrelid='public.assistant_browser_access'::regclass) then
  create trigger assistant_browser_access_immutable before update or delete on public.assistant_browser_access
   for each row execute function public.assistant_refuse_change();
  create trigger assistant_browser_access_no_truncate before truncate on public.assistant_browser_access
   for each statement execute function public.assistant_refuse_change();
 end if;
end $$;

alter table public.browser_agent_types add column if not exists principal_kind text not null default 'org';
alter table public.browser_agent_workers add column if not exists principal_kind text not null default 'org';
alter table public.browser_agent_jobs add column if not exists principal_kind text not null default 'org';
alter table public.browser_agent_jobs add column if not exists principal_user_id uuid;
alter table public.browser_agent_jobs add column if not exists user_credential_id uuid;
alter table public.browser_agent_jobs add column if not exists user_credential_version int;
alter table public.browser_agent_jobs add column if not exists assistant_approval_id uuid;
alter table public.browser_agent_jobs add column if not exists assistant_conversation_id uuid;
alter table public.browser_agent_jobs add column if not exists assistant_reservation_id uuid;
alter table public.browser_agent_help_requests add column if not exists principal_kind text not null default 'org';
alter table public.browser_agent_help_requests add column if not exists principal_user_id uuid;
do $$ declare t text; begin
 foreach t in array array['browser_agent_types','browser_agent_workers','browser_agent_jobs','browser_agent_help_requests'] loop
  if not exists(select 1 from pg_constraint where conname=t||'_principal_kind_check') then
   execute format('alter table public.%I add constraint %I check (principal_kind in (''org'',''user''))',t,t||'_principal_kind_check');
  end if;
 end loop;
 if not exists(select 1 from pg_constraint where conname='browser_agent_jobs_user_shape') then
  alter table public.browser_agent_jobs add constraint browser_agent_jobs_user_shape check (
   (principal_kind='org' and principal_user_id is null and user_credential_id is null and user_credential_version is null
    and assistant_approval_id is null and assistant_conversation_id is null and assistant_reservation_id is null)
   or (principal_kind='user' and principal_user_id is not null and user_credential_id is not null and user_credential_version is not null and user_credential_version>0
    and assistant_approval_id is not null and assistant_conversation_id is not null and assistant_reservation_id is not null));
 end if;
 if not exists(select 1 from pg_constraint where conname='browser_agent_help_requests_user_shape') then
  alter table public.browser_agent_help_requests add constraint browser_agent_help_requests_user_shape
    check ((principal_kind='org' and principal_user_id is null) or (principal_kind='user' and principal_user_id is not null));
 end if;
end $$;
create unique index if not exists browser_agent_jobs_assistant_approval_uq on public.browser_agent_jobs(assistant_approval_id)
 where assistant_approval_id is not null;
create index if not exists browser_agent_jobs_user_idx on public.browser_agent_jobs(principal_user_id,created_at desc,id desc)
 where principal_kind='user';
create index if not exists browser_agent_jobs_user_credential_idx on public.browser_agent_jobs(user_credential_id)
 where principal_kind='user';
create index if not exists browser_agent_help_requests_user_idx on public.browser_agent_help_requests(principal_user_id)
 where principal_kind='user';
insert into public.browser_agent_types(key,label,identity,prompt_version,principal_kind,enabled,max_concurrency)
 values('user_browser_read','Personal browser read',null,'user_browser_read.v1','user',false,1)
 on conflict(key) do nothing;

-- This is a code gate, not a configurable setting. A later reviewed migration
-- must replace it only after encrypted trace storage and worker isolation exist.
create or replace function public.assistant_browser_runtime_ready()
returns boolean language sql immutable security invoker set search_path=pg_catalog,public
as $$ select false $$;

create or replace function public.assistant_browser_owner_eligible(p_user_id uuid,p_require_grant boolean default true)
returns boolean language plpgsql security invoker set search_path=pg_catalog,public as $$
begin
 perform 1 from public.profiles where id=p_user_id and is_active=true and role::text in
 ('estimating_admin','executive','it_admin','estimating_engineer_materials','estimating_engineer_labor') for share;
 if not found then return false; end if;
 if not p_require_grant then return true; end if;
 perform 1 from public.assistant_settings where id=1 and enabled=true and not ('user_browser_task'=any(disabled_tools)) for share;
 if not found then return false; end if;
 perform 1 from public.assistant_profiles where user_id=p_user_id for share;
 if exists(select 1 from public.assistant_profiles where user_id=p_user_id and enabled=false) then return false; end if;
 perform 1 from public.assistant_delegations where user_id=p_user_id and revoked_at is null
  and (expires_at is null or expires_at>now()) for share;
 return found;
end $$;

-- Cancel first, then erase credentials. The job and its reservation remain as
-- minimal accounting records. Terminal settlement includes already spent cost.
create or replace function public.assistant_browser_cancel_credentials(p_user_id uuid,p_credential_id uuid)
returns int language plpgsql security invoker set search_path=pg_catalog,public as $$
declare j public.browser_agent_jobs; n int:=0;
begin
 for j in select * from public.browser_agent_jobs where principal_kind='user' and principal_user_id=p_user_id
  and (p_credential_id is null or user_credential_id=p_credential_id)
  and status in ('queued','running','waiting_help','applying') for update loop
  update public.browser_agent_jobs set status='canceled',error_kind='credential_revoked',error='Browser authorization changed.',
   cancel_requested_at=now(),finished_at=now(),lease_owner=null,lease_boot_id=null,lease_expires_at=null,hold_until=null
   where id=j.id;
  update public.assistant_spend_reservations set status='released',actual_usd=j.cost_usd,settled_at=now()
   where id=j.assistant_reservation_id and user_id=p_user_id and status='active';
  n:=n+1;
 end loop;
 delete from public.assistant_browser_sessions where user_id=p_user_id
  and (p_credential_id is null or credential_id=p_credential_id);
 return n;
end $$;

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
 v_origins:=array['https://app.procore.com','https://login.procore.com'];
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

create or replace function public.assistant_browser_credential_revoke(p_user_id uuid,p_service text,p_expected_version int)
returns jsonb language plpgsql security invoker set search_path=pg_catalog,public as $$
declare c public.assistant_credentials;
begin
 perform pg_advisory_xact_lock(hashtextextended('assistant-browser-owner:'||p_user_id::text,0));
 select * into c from public.assistant_credentials where user_id=p_user_id and service=p_service for update;
 if c.id is null then return '{"ok":false,"code":"not_found"}'::jsonb; end if;
 if c.version is distinct from p_expected_version then return '{"ok":false,"code":"version_conflict"}'::jsonb; end if;
 perform public.assistant_browser_cancel_credentials(p_user_id,c.id);
 update public.assistant_credentials set status='revoked',version=version+1,wrapped_dek=null,ciphertext=null,nonce=null,key_id=null,
 login_attempt_id=null,login_job_id=null,login_epoch=null,login_attempt_expires_at=null,updated_at=now() where id=c.id;
 return jsonb_build_object('ok',true,'version',c.version+1);
end $$;

create or replace function public.assistant_browser_revoke_owner(p_user_id uuid)
returns int language plpgsql security invoker set search_path=pg_catalog,public as $$
declare n int;
begin
 perform pg_advisory_xact_lock(hashtextextended('assistant-browser-owner:'||p_user_id::text,0));
 perform public.assistant_browser_cancel_credentials(p_user_id,null);
 update public.assistant_credentials set status='revoked',version=version+1,wrapped_dek=null,ciphertext=null,nonce=null,key_id=null,
 login_attempt_id=null,login_job_id=null,login_epoch=null,login_attempt_expires_at=null,updated_at=now()
 where user_id=p_user_id and status<>'revoked';
 get diagnostics n=row_count;
 return n;
end $$;

-- Session writes are CAS protected, but no login/session release endpoint is
-- exposed in this milestone. Encryption AAD binds owner, credential and origin.
create or replace function public.assistant_browser_session_put(
 p_user_id uuid,p_credential_id uuid,p_credential_version int,p_origin text,p_expected_version int,p_record jsonb)
returns jsonb language plpgsql security invoker set search_path=pg_catalog,public as $$
declare c public.assistant_credentials; s public.assistant_browser_sessions;
begin
 if not public.assistant_browser_runtime_ready() then return '{"ok":false,"code":"runtime_unavailable"}'::jsonb; end if;
 if not public.assistant_browser_owner_eligible(p_user_id,true) then return '{"ok":false,"code":"ineligible"}'::jsonb; end if;
 perform pg_advisory_xact_lock(hashtextextended('assistant-browser-owner:'||p_user_id::text,0));
 select * into c from public.assistant_credentials where id=p_credential_id and user_id=p_user_id for update;
 if c.id is null or c.status<>'active' or c.version is distinct from p_credential_version
  or p_origin is null or not (p_origin=any(c.origins)) then return '{"ok":false,"code":"credential_changed"}'::jsonb; end if;
 select * into s from public.assistant_browser_sessions where credential_id=c.id and origin=p_origin for update;
 if coalesce(s.version,0) is distinct from p_expected_version then return '{"ok":false,"code":"version_conflict"}'::jsonb; end if;
 insert into public.assistant_browser_sessions(id,user_id,credential_id,credential_version,origin,version,wrapped_dek,ciphertext,nonce,key_id,expires_at)
 values(coalesce(s.id,(p_record->>'id')::uuid),p_user_id,c.id,c.version,p_origin,coalesce(s.version,0)+1,
 p_record->>'wrapped_dek',p_record->>'ciphertext',p_record->>'nonce',p_record->>'key_id',
 least((p_record->>'expires_at')::timestamptz,now()+interval '24 hours'))
 on conflict(credential_id,origin) do update set credential_version=excluded.credential_version,version=excluded.version,
 wrapped_dek=excluded.wrapped_dek,ciphertext=excluded.ciphertext,nonce=excluded.nonce,key_id=excluded.key_id,
 expires_at=excluded.expires_at,updated_at=now() returning * into s;
 return jsonb_build_object('ok',true,'session',to_jsonb(s));
end $$;

-- Preserve immutable job ownership across retries, help and administrative edits.
create or replace function public.assistant_browser_job_guard()
returns trigger language plpgsql security invoker set search_path=pg_catalog,public as $$
declare v_kind text;
begin
 if tg_op='UPDATE' and (new.principal_kind is distinct from old.principal_kind
  or new.principal_user_id is distinct from old.principal_user_id
  or new.user_credential_id is distinct from old.user_credential_id
  or new.user_credential_version is distinct from old.user_credential_version
  or new.assistant_approval_id is distinct from old.assistant_approval_id
  or new.assistant_conversation_id is distinct from old.assistant_conversation_id
  or new.assistant_reservation_id is distinct from old.assistant_reservation_id) then
  raise exception 'browser_principal_immutable';
 end if;
 select principal_kind into v_kind from public.browser_agent_types where key=new.agent_type;
 if v_kind is distinct from new.principal_kind then raise exception 'browser_principal_mismatch'; end if;
 if new.lease_owner is not null then
  select principal_kind into v_kind from public.browser_agent_workers where id=new.lease_owner;
  if v_kind is distinct from new.principal_kind then raise exception 'browser_pool_mismatch'; end if;
  if new.principal_kind='user' and not public.assistant_browser_runtime_ready() then
   raise exception 'browser_runtime_unavailable';
  end if;
 end if;
 if new.principal_kind='user' and tg_op='INSERT' then
  if not public.assistant_browser_runtime_ready() then raise exception 'browser_runtime_unavailable'; end if;
  if not public.assistant_browser_owner_eligible(new.principal_user_id,true)
   or not exists(select 1 from public.assistant_credentials where id=new.user_credential_id
     and user_id=new.principal_user_id and version=new.user_credential_version and status='active') then
   raise exception 'browser_owner_ineligible';
  end if;
 end if;
 return new;
end $$;
create or replace function public.assistant_browser_pool_guard()
returns trigger language plpgsql security invoker set search_path=pg_catalog,public as $$
begin
 if tg_op='UPDATE' and new.principal_kind is distinct from old.principal_kind then
  raise exception 'browser_pool_immutable';
 end if;
 return new;
end $$;
do $$ begin
 if not exists(select 1 from pg_trigger where tgname='assistant_browser_job_guard' and tgrelid='public.browser_agent_jobs'::regclass) then
  create trigger assistant_browser_job_guard before insert or update on public.browser_agent_jobs
   for each row execute function public.assistant_browser_job_guard();
 end if;
 if not exists(select 1 from pg_trigger where tgname='assistant_browser_worker_pool_guard' and tgrelid='public.browser_agent_workers'::regclass) then
  create trigger assistant_browser_worker_pool_guard before update on public.browser_agent_workers
   for each row execute function public.assistant_browser_pool_guard();
 end if;
 if not exists(select 1 from pg_trigger where tgname='assistant_browser_type_pool_guard' and tgrelid='public.browser_agent_types'::regclass) then
  create trigger assistant_browser_type_pool_guard before update on public.browser_agent_types
   for each row execute function public.assistant_browser_pool_guard();
 end if;
end $$;

-- User external-job accounting follows the phase 4 paid-work convention.
do $$ declare v_name text; begin
 select conname into v_name from pg_constraint where conrelid='public.assistant_spend_reservations'::regclass
  and contype='c' and pg_get_constraintdef(oid) like '%split_task%';
 if v_name is null then raise exception 'assistant_spend_kind_constraint_missing'; end if;
 execute format('alter table public.assistant_spend_reservations drop constraint %I',v_name);
 alter table public.assistant_spend_reservations add constraint assistant_spend_reservations_kind_chk
  check(kind in ('turn_call','split_task','plans_pages','ai_estimate','browser_task'));
end $$;
create unique index if not exists assistant_spend_browser_approval_uq on public.assistant_spend_reservations(approval_id)
 where kind='browser_task' and approval_id is not null;

create or replace function public.assistant_spend_state(
  p_user_id uuid, p_user_cap_default numeric, p_global_cap_default numeric
)
returns jsonb
language plpgsql
stable
security invoker
set search_path = pg_catalog, public
as $$
declare
  v_since timestamptz := (date_trunc('day', now() at time zone 'America/Los_Angeles')) at time zone 'America/Los_Angeles';
  v_user_cap numeric;
  v_global_cap numeric;
  v_user_spent numeric;
  v_global_spent numeric;
  v_user_held numeric;
  v_global_held numeric;
begin
  select coalesce((select p.daily_cost_cap_usd from public.assistant_profiles p where p.user_id = p_user_id),
                  p_user_cap_default) into v_user_cap;
  select coalesce((select s.global_daily_cap_usd from public.assistant_settings s where s.id = 1),
                  p_global_cap_default) into v_global_cap;
  select coalesce(sum(l.cost_usd), 0) into v_user_spent
    from public.llm_call_log l where l.user_id = p_user_id and l.created_at >= v_since;
  select coalesce(sum(l.cost_usd), 0) into v_global_spent
    from public.llm_call_log l
   where l.created_at >= v_since
     and (l.feature in ('assistant', 'assistant_extract') or l.assistant_run_id is not null);
  v_user_spent := v_user_spent + coalesce((select sum(r.actual_usd)
    from public.assistant_spend_reservations r
   where r.user_id = p_user_id and r.kind in ('plans_pages', 'ai_estimate', 'browser_task')
     and r.status in ('settled', 'released') and r.actual_usd is not null
     and r.settled_at >= v_since), 0);
  v_global_spent := v_global_spent + coalesce((select sum(r.actual_usd)
    from public.assistant_spend_reservations r
   where r.kind in ('plans_pages', 'ai_estimate', 'browser_task')
     and r.status in ('settled', 'released') and r.actual_usd is not null
     and r.settled_at >= v_since), 0);
  with live as (
    select r.user_id,
           greatest(r.amount_usd - coalesce((select sum(l.cost_usd) from public.llm_call_log l
                                              where l.spend_reservation_id = r.id), 0), 0) as held
      from public.assistant_spend_reservations r
     where r.status = 'active' and r.expires_at > now()
  )
  select coalesce(sum(held) filter (where user_id = p_user_id), 0), coalesce(sum(held), 0)
    into v_user_held, v_global_held from live;
  return jsonb_build_object(
    'user_cap', v_user_cap, 'user_spent', v_user_spent, 'user_held', v_user_held,
    'global_cap', v_global_cap, 'global_spent', v_global_spent, 'global_held', v_global_held);
end;
$$;

create or replace function public.assistant_spend_reserve(
  p_user_id uuid, p_kind text, p_amount numeric, p_ttl_seconds int,
  p_user_cap_default numeric, p_global_cap_default numeric,
  p_run_id uuid, p_task_id uuid, p_approval_id uuid
)
returns jsonb
language plpgsql
volatile
security invoker
set search_path = pg_catalog, public
as $$
declare
  v_state jsonb;
  v_id uuid;
begin
  if p_kind not in ('turn_call', 'split_task', 'plans_pages', 'ai_estimate', 'browser_task') or p_amount is null or p_amount < 0
     or p_ttl_seconds is null or p_ttl_seconds < 30 or p_ttl_seconds > 43200 then
    raise exception 'assistant_spend_reserve: bad arguments' using errcode = 'check_violation';
  end if;
  perform pg_advisory_xact_lock(hashtext('assistant_spend'));
  v_state := public.assistant_spend_state(p_user_id, p_user_cap_default, p_global_cap_default);
  if (v_state->>'user_spent')::numeric + (v_state->>'user_held')::numeric + p_amount
       >= (v_state->>'user_cap')::numeric then
    return jsonb_build_object('ok', false, 'reason', 'user_cap', 'id', null, 'state', v_state);
  end if;
  if (v_state->>'global_spent')::numeric + (v_state->>'global_held')::numeric + p_amount
       >= (v_state->>'global_cap')::numeric then
    return jsonb_build_object('ok', false, 'reason', 'global_cap', 'id', null, 'state', v_state);
  end if;
  insert into public.assistant_spend_reservations (user_id, kind, run_id, task_id, approval_id, amount_usd, expires_at)
  values (p_user_id, p_kind, p_run_id, p_task_id, p_approval_id, p_amount,
          now() + make_interval(secs => p_ttl_seconds))
  returning id into v_id;
  return jsonb_build_object('ok', true, 'reason', null, 'id', v_id, 'state', v_state);
end;
$$;

create or replace function public.browser_agent_fence(
  p_job uuid, p_worker uuid, p_boot uuid, p_epoch bigint, p_statuses text[])
returns browser_agent_jobs
language plpgsql
security invoker
set search_path = pg_catalog, public
as $$
declare
  v_worker browser_agent_workers;
  v_job browser_agent_jobs;
begin
  select * into v_worker from browser_agent_workers where id = p_worker;
  if not found then
    raise exception 'unauthorized';
  end if;
  if v_worker.status = 'revoked' then
    raise exception 'revoked';
  end if;
  if v_worker.status <> 'active' or v_worker.boot_id is distinct from p_boot then
    raise exception 'stale_boot';
  end if;
  select * into v_job from browser_agent_jobs where id = p_job for update;
  if not found or v_job.lease_owner is distinct from p_worker or v_job.lease_epoch <> p_epoch
     or v_job.lease_boot_id is distinct from p_boot then
    raise exception 'lease_lost';
  end if;
  if not (v_job.status = any (p_statuses)) then
    raise exception 'job_not_running';
  end if;
  if v_job.principal_kind is distinct from v_worker.principal_kind
     or not exists(select 1 from browser_agent_types where key=v_job.agent_type and principal_kind=v_job.principal_kind) then
    raise exception 'lease_lost';
  end if;
  if v_job.principal_kind='user' then
    if not public.assistant_browser_runtime_ready() then raise exception 'browser_runtime_unavailable'; end if;
    if not public.assistant_browser_owner_eligible(v_job.principal_user_id,true)
      or v_job.expires_at<=now()
      or not exists(select 1 from public.assistant_credentials where id=v_job.user_credential_id
        and user_id=v_job.principal_user_id and version=v_job.user_credential_version and status='active')
      or not exists(select 1 from public.assistant_conversations where id=v_job.assistant_conversation_id
        and user_id=v_job.principal_user_id and mode='normal' and mailbox_tainted_at is null) then
      raise exception 'lease_lost';
    end if;
  end if;
  return v_job;
end;
$$;

create or replace function public.claim_browser_agent_jobs(
  p_worker uuid, p_boot uuid, p_capacity jsonb, p_lease_seconds int default 90)
returns setof browser_agent_jobs
language plpgsql
security invoker
set search_path = pg_catalog, public
as $$
declare
  v_worker browser_agent_workers;
  v_settings browser_agent_settings;
  v_type browser_agent_types;
  v_key text;
  v_want int;
  v_busy int;
  v_take int;
  v_snapshot jsonb;
  v_job browser_agent_jobs;
begin
  -- FOR SHARE: serializes with browser_agent_hello's FOR UPDATE, so a claim
  -- racing a new boot either sees the new boot (stale_boot) or commits first
  -- (hello then requeues what it leased).
  select * into v_worker from browser_agent_workers where id = p_worker for share;
  if not found then
    raise exception 'unauthorized';
  end if;
  if v_worker.status = 'revoked' then
    raise exception 'revoked';
  end if;
  if v_worker.status <> 'active' or v_worker.boot_id is distinct from p_boot then
    raise exception 'stale_boot';
  end if;
  -- Hard fail closed before a claim can persist any user trace.
  if v_worker.principal_kind='user' and not public.assistant_browser_runtime_ready() then return; end if;
  if v_worker.draining then
    return;
  end if;
  select * into v_settings from browser_agent_settings where id = 1;
  if not coalesce(v_settings.enabled, false) then
    return;
  end if;

  for v_key, v_want in
    select c.key, least(greatest((c.value)::int, 0), 8) from jsonb_each_text(coalesce(p_capacity, '{}'::jsonb)) c
  loop
    continue when v_want <= 0;
    continue when not (v_key = any (v_worker.allowed_types));
    continue when v_worker.capabilities is null or not ((v_worker.capabilities -> 'types') ? v_key);
    select * into v_type from browser_agent_types where key = v_key and enabled and principal_kind=v_worker.principal_kind;
    continue when not found;

    perform pg_advisory_xact_lock(hashtext('bagent:' || v_key));
    select count(*) into v_busy from browser_agent_jobs where agent_type = v_key and lease_owner is not null;
    v_take := least(v_want, v_type.max_concurrency - v_busy);
    continue when v_take <= 0;

    v_snapshot := jsonb_build_object(
      'type', to_jsonb(v_type) - array['updated_by', 'updated_at'],
      'per_job_cost_cap_usd', v_settings.per_job_cost_cap_usd,
      'daily_cost_cap_usd', v_settings.daily_cost_cap_usd,
      'config_version', v_settings.config_version,
      'claimed_at', now());

    for v_job in
      update browser_agent_jobs j
         set status = 'running',
             lease_owner = p_worker,
             lease_boot_id = p_boot,
             lease_epoch = j.lease_epoch + 1,
             lease_expires_at = now() + make_interval(secs => p_lease_seconds),
             attempts = j.attempts + case when coalesce(j.resume_reason, 'retry') = 'retry' then 1 else 0 end,
             last_resume_reason = j.resume_reason,
             resume_reason = null,
             next_attempt_at = null,
             hold_until = null,
             config_snapshot = v_snapshot,
             started_at = coalesce(j.started_at, now())
        from (select id from browser_agent_jobs
               where agent_type = v_key and status = 'queued' and principal_kind=v_worker.principal_kind and cancel_requested_at is null
                 and coalesce(next_attempt_at, now()) <= now() and expires_at > now()
               order by priority, created_at
               for update skip locked
               limit v_take) pick
       where j.id = pick.id
      returning j.*
    loop
      update browser_agent_turns set status = 'abandoned'
       where job_id = v_job.id and status = 'thinking';
      insert into browser_agent_steps (job_id, epoch, worker_id, kind, actor, summary, result)
      values (v_job.id, v_job.lease_epoch, p_worker, 'claim', 'bdr',
              'Claimed (attempt ' || v_job.attempts || ', epoch ' || v_job.lease_epoch || ')',
              jsonb_build_object('resume_reason', v_job.last_resume_reason, 'boot_id', p_boot));
      return next v_job;
    end loop;
  end loop;
  return;
end;
$$;

create or replace function public.assistant_browser_task_enqueue(
 p_user_id uuid,p_approval_id uuid,p_run_id uuid,p_payload_sha256 text,p_input jsonb,
 p_user_cap_default numeric default 5,p_global_cap_default numeric default 50)
returns jsonb language plpgsql security invoker set search_path=pg_catalog,public as $$
declare a public.assistant_approvals; c public.assistant_credentials; j public.browser_agent_jobs;
 v_hold jsonb; v_cost numeric; v_type public.browser_agent_types; v_settings public.browser_agent_settings;
begin
 -- This milestone cannot create executable user jobs, even with flags enabled.
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

-- Critical profile updates revoke credentials in the same transaction.
-- DELETE uses FK cascades for secret destruction and the backend critical
-- offboarding hook for accounting. GoTrue cascades as supabase_auth_admin,
-- which must not gain access to private credential RPCs or tables.
create or replace function public.assistant_browser_profile_changed()
returns trigger language plpgsql security invoker set search_path=pg_catalog,public as $$
begin
 if new.role is distinct from old.role or new.is_active is distinct from old.is_active
  or new.email is distinct from old.email or new.mfa_enrolled is distinct from old.mfa_enrolled then perform public.assistant_browser_revoke_owner(old.id); end if;
 return new;
end $$;
do $$ begin
 if not exists(select 1 from pg_trigger where tgname='assistant_browser_profile_changed' and tgrelid='public.profiles'::regclass) then
  create trigger assistant_browser_profile_changed before update on public.profiles
   for each row when (new.role is distinct from old.role or new.is_active is distinct from old.is_active
    or new.email is distinct from old.email or new.mfa_enrolled is distinct from old.mfa_enrolled)
   execute function public.assistant_browser_profile_changed();
 end if;
end $$;

-- All new RPCs are internal service calls. Existing claim/fence grants stay intact.
do $$ declare r record; begin
 for r in select p.oid::regprocedure as signature from pg_proc p join pg_namespace n on n.oid=p.pronamespace
  where n.nspname='public' and p.proname like 'assistant_browser_%' loop
  execute format('revoke all on function %s from public,anon,authenticated',r.signature);
  execute format('grant execute on function %s to service_role',r.signature);
 end loop;
end $$;
notify pgrst,'reload schema';
