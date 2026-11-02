-- Lock order: eligibility (profile, global settings, assistant profile, delegation), then connection, then state.
-- Revoke only locks connection/state and never acquires eligibility locks.
-- Phase 5 offline OAuth foundation. No tenant changes. All authority is service-side.
create table if not exists public.assistant_ms_connections (
 user_id uuid primary key references public.profiles(id) on delete cascade,
 generation bigint not null default 0 check(generation >= 0),
 status text not null default 'disconnected' check(status in ('disconnected','connecting','connected','revoked')),
 tenant_id text, object_id text, home_account_id text, upn text,
 match_basis text check(match_basis in ('email','rfp_mailbox')), matched_address text,
 scopes text[] not null default '{}', cache_ciphertext text, cache_nonce text, cache_key_id text,
 cache_version bigint not null default 0 check(cache_version >= 0), token_expires_at timestamptz,
 refresh_claim_id uuid, refresh_claim_until timestamptz,
 shared_mailboxes_reachable text[] not null default '{}', connected_at timestamptz,
 revoked_at timestamptz, revoked_reason text, last_error_code text,
 created_at timestamptz not null default now(), updated_at timestamptz not null default now(),
 check(status <> 'connected' or (tenant_id is not null and object_id is not null and home_account_id is not null
 and upn is not null and matched_address is not null and matched_address=upn and match_basis is not null and cache_ciphertext is not null
 and cache_nonce is not null and cache_key_id is not null)),
 check(status = 'connected' or cache_ciphertext is null)
);
create table if not exists public.assistant_ms_oauth_states (
 state_hash text primary key check(state_hash ~ '^[a-f0-9]{64}$'),
 browser_hash text not null check(browser_hash ~ '^[a-f0-9]{64}$'),
 user_id uuid not null references public.assistant_ms_connections(user_id) on delete cascade,
 generation bigint not null, flow_ciphertext text, flow_nonce text, flow_key_id text,
 expires_at timestamptz not null, consumed_at timestamptz, completion_id uuid, completed_at timestamptz,
 created_at timestamptz not null default now()
);
create index if not exists assistant_ms_states_owner on public.assistant_ms_oauth_states(user_id,generation);
create index if not exists assistant_ms_states_expiry on public.assistant_ms_oauth_states(expires_at);
alter table public.assistant_ms_connections enable row level security;
alter table public.assistant_ms_connections force row level security;
alter table public.assistant_ms_oauth_states enable row level security;
alter table public.assistant_ms_oauth_states force row level security;
revoke all on public.assistant_ms_connections, public.assistant_ms_oauth_states from public,anon,authenticated;
grant select,insert,update,delete on public.assistant_ms_connections, public.assistant_ms_oauth_states to service_role;

create or replace function public.assistant_ms_eligible(p_user_id uuid) returns jsonb
language plpgsql security invoker set search_path = public, pg_temp as $$
declare
 c public.assistant_ms_connections%rowtype;
 s public.assistant_ms_oauth_states%rowtype;
 
begin
 perform 1 from public.profiles p where p.id=p_user_id and p.is_active=true and p.role::text in
 ('estimating_admin','executive','it_admin','accountant','estimating_engineer_materials','estimating_engineer_labor') for share;
 if not found then return '{"ok":false,"code":"ineligible"}'::jsonb; end if;
 perform 1 from public.assistant_settings where id=1 and enabled=true for share;
 if not found then return '{"ok":false,"code":"ineligible"}'::jsonb; end if;
 perform 1 from public.assistant_profiles where user_id=p_user_id for share;
 if exists(select 1 from public.assistant_profiles where user_id=p_user_id and enabled=false) then return '{"ok":false,"code":"ineligible"}'::jsonb; end if;
 perform 1 from public.assistant_delegations d where d.user_id=p_user_id and d.revoked_at is null and (d.expires_at is null or d.expires_at > now()) for share;
 return jsonb_build_object('ok',found);
end $$;

create or replace function public.assistant_ms_binding_valid(p_user_id uuid, p_identity jsonb) returns jsonb
language plpgsql security invoker set search_path = public, pg_temp as $$
declare
 c public.assistant_ms_connections%rowtype;
 s public.assistant_ms_oauth_states%rowtype;
 v boolean;
begin
 if not coalesce((public.assistant_ms_eligible(p_user_id)->>'ok')::boolean,false) then
 return '{"ok":false,"code":"ineligible"}'::jsonb; end if;
 if p_identity->>'upn' is null or p_identity->>'upn' is distinct from p_identity->>'matched_address' then
 return '{"ok":false,"code":"binding_changed"}'::jsonb; end if;
 select exists(select 1 from public.profiles p where p.id=p_user_id and (
 (p_identity->>'match_basis'='email' and lower(trim(p.email))=p_identity->>'upn') or
 (p_identity->>'match_basis'='rfp_mailbox' and exists(select 1 from unnest(p.rfp_mailboxes) m where lower(trim(m))=p_identity->>'upn')))) into v;
 return jsonb_build_object('ok',v,'code',case when v then null else 'binding_changed' end);
end $$;

create or replace function public.assistant_ms_begin(p_user_id uuid,p_state_hash text,p_browser_hash text,p_flow_ciphertext text,p_flow_nonce text,p_flow_key_id text) returns jsonb
language plpgsql security invoker set search_path = public, pg_temp as $$
declare
 c public.assistant_ms_connections%rowtype;
 s public.assistant_ms_oauth_states%rowtype;
 
begin
 if not coalesce((public.assistant_ms_eligible(p_user_id)->>'ok')::boolean,false) then return '{"ok":false,"code":"ineligible"}'::jsonb; end if;
 if p_flow_ciphertext is null or p_flow_nonce is null or p_flow_key_id is null then raise exception 'Invalid encrypted flow'; end if;
 insert into public.assistant_ms_connections(user_id) values(p_user_id) on conflict do nothing;
 select * into c from public.assistant_ms_connections where user_id=p_user_id for update;
 update public.assistant_ms_connections set generation=generation+1,status='connecting',tenant_id=null,object_id=null,home_account_id=null,upn=null,match_basis=null,matched_address=null,scopes='{}',shared_mailboxes_reachable='{}',cache_ciphertext=null,cache_nonce=null,cache_key_id=null,token_expires_at=null,refresh_claim_id=null,refresh_claim_until=null,revoked_at=null,revoked_reason=null,updated_at=now() where user_id=p_user_id returning * into c;
 update public.assistant_ms_oauth_states set flow_ciphertext=null,flow_nonce=null,flow_key_id=null,expires_at=least(expires_at,now()) where user_id=p_user_id;
 insert into public.assistant_ms_oauth_states(state_hash,browser_hash,user_id,generation,flow_ciphertext,flow_nonce,flow_key_id,expires_at)
 values(p_state_hash,p_browser_hash,p_user_id,c.generation,p_flow_ciphertext,p_flow_nonce,p_flow_key_id,now()+interval '10 minutes');
 return jsonb_build_object('ok',true,'generation',c.generation,'expires_at',now()+interval '10 minutes');
end $$;

create or replace function public.assistant_ms_consume(p_user_id uuid,p_state_hash text,p_browser_hash text,p_completion_id uuid) returns jsonb
language plpgsql security invoker set search_path = public, pg_temp as $$
declare
 c public.assistant_ms_connections%rowtype;
 s public.assistant_ms_oauth_states%rowtype;
 
begin
 if not coalesce((public.assistant_ms_eligible(p_user_id)->>'ok')::boolean,false) then return '{"ok":false,"code":"ineligible"}'::jsonb; end if;
 select * into c from public.assistant_ms_connections where user_id=p_user_id for update;
 select * into s from public.assistant_ms_oauth_states where user_id=p_user_id and state_hash=p_state_hash for update;
 if c.status is distinct from 'connecting' or s.state_hash is null or s.browser_hash is distinct from p_browser_hash
 or s.generation is distinct from c.generation or s.consumed_at is not null or s.expires_at<=now() or s.flow_ciphertext is null or p_completion_id is null then
 return '{"ok":false,"code":"invalid_state"}'::jsonb; end if;
 update public.assistant_ms_oauth_states set consumed_at=now(),completion_id=p_completion_id,flow_ciphertext=null,flow_nonce=null,flow_key_id=null where state_hash=p_state_hash;
 return jsonb_build_object('ok',true,'user_id',p_user_id,'generation',c.generation,'flow_ciphertext',s.flow_ciphertext,'flow_nonce',s.flow_nonce,'flow_key_id',s.flow_key_id);
end $$;

create or replace function public.assistant_ms_complete(p_user_id uuid,p_generation bigint,p_state_hash text,p_completion_id uuid,p_identity jsonb,p_cache_ciphertext text,p_cache_nonce text,p_cache_key_id text,p_token_expires_at timestamptz,p_scopes text[],p_shared_mailboxes_reachable text[]) returns jsonb
language plpgsql security invoker set search_path = public, pg_temp as $$
declare
 c public.assistant_ms_connections%rowtype;
 s public.assistant_ms_oauth_states%rowtype;
 
begin
 if not coalesce((public.assistant_ms_eligible(p_user_id)->>'ok')::boolean,false) then return '{"ok":false,"code":"ineligible"}'::jsonb; end if;
 select * into c from public.assistant_ms_connections where user_id=p_user_id for update;
 select * into s from public.assistant_ms_oauth_states where user_id=p_user_id and state_hash=p_state_hash for update;
 if c.status is distinct from 'connecting' or c.generation is distinct from p_generation or s.generation is distinct from p_generation
 or s.state_hash is null or s.completion_id is distinct from p_completion_id or p_completion_id is null or s.consumed_at is null or s.completed_at is not null or s.expires_at<=now() then
 return '{"ok":false,"code":"stale"}'::jsonb; end if;
 if not coalesce((public.assistant_ms_binding_valid(p_user_id,p_identity)->>'ok')::boolean,false) then return '{"ok":false,"code":"binding_changed"}'::jsonb; end if;
 update public.assistant_ms_connections set status='connected',tenant_id=p_identity->>'tenant_id',object_id=p_identity->>'object_id',home_account_id=p_identity->>'home_account_id',
 upn=p_identity->>'upn',match_basis=p_identity->>'match_basis',matched_address=p_identity->>'matched_address',cache_ciphertext=p_cache_ciphertext,cache_nonce=p_cache_nonce,cache_key_id=p_cache_key_id,
 token_expires_at=p_token_expires_at,scopes=p_scopes,shared_mailboxes_reachable=p_shared_mailboxes_reachable,cache_version=cache_version+1,
 connected_at=now(),revoked_at=null,revoked_reason=null,last_error_code=null,updated_at=now() where user_id=p_user_id returning * into c;
 update public.assistant_ms_oauth_states set completed_at=now(),flow_ciphertext=null,flow_nonce=null,flow_key_id=null where state_hash=p_state_hash;
 return jsonb_build_object('ok',true,'generation',c.generation,'cache_version',c.cache_version);
end $$;

create or replace function public.assistant_ms_refresh_claim(p_user_id uuid,p_generation bigint,p_claim_id uuid) returns jsonb
language plpgsql security invoker set search_path = public, pg_temp as $$
declare
 c public.assistant_ms_connections%rowtype;
 s public.assistant_ms_oauth_states%rowtype;
 
begin
 if not coalesce((public.assistant_ms_eligible(p_user_id)->>'ok')::boolean,false) then return '{"ok":false,"code":"ineligible"}'::jsonb; end if;
 select * into c from public.assistant_ms_connections where user_id=p_user_id for update;
 if c.status is distinct from 'connected' or c.generation is distinct from p_generation or p_claim_id is null then return '{"ok":false,"code":"stale"}'::jsonb; end if;
 if not coalesce((public.assistant_ms_binding_valid(p_user_id,jsonb_build_object('tenant_id',c.tenant_id,'object_id',c.object_id,'home_account_id',c.home_account_id,'upn',c.upn,'match_basis',c.match_basis,'matched_address',c.matched_address))->>'ok')::boolean,false) then return '{"ok":false,"code":"binding_changed"}'::jsonb; end if;
 if c.refresh_claim_until>now() then return '{"ok":false,"code":"busy"}'::jsonb; end if;
 update public.assistant_ms_connections set refresh_claim_id=p_claim_id,refresh_claim_until=now()+interval '60 seconds',updated_at=now() where user_id=p_user_id;
 return jsonb_build_object('ok',true,'generation',c.generation,'cache_version',c.cache_version,'cache_ciphertext',c.cache_ciphertext,'cache_nonce',c.cache_nonce,'cache_key_id',c.cache_key_id,'identity',jsonb_build_object('tenant_id',c.tenant_id,'object_id',c.object_id,'home_account_id',c.home_account_id,'upn',c.upn,'match_basis',c.match_basis,'matched_address',c.matched_address));
end $$;

create or replace function public.assistant_ms_refresh_commit(p_user_id uuid,p_generation bigint,p_claim_id uuid,p_expected_cache_version bigint,p_cache_ciphertext text,p_cache_nonce text,p_cache_key_id text,p_token_expires_at timestamptz) returns jsonb
language plpgsql security invoker set search_path = public, pg_temp as $$
declare
 c public.assistant_ms_connections%rowtype;
 s public.assistant_ms_oauth_states%rowtype;
 
begin
 if not coalesce((public.assistant_ms_eligible(p_user_id)->>'ok')::boolean,false) then return '{"ok":false,"code":"ineligible"}'::jsonb; end if;
 select * into c from public.assistant_ms_connections where user_id=p_user_id for update;
 if c.status is distinct from 'connected' or c.generation is distinct from p_generation or c.refresh_claim_id is distinct from p_claim_id or p_claim_id is null
 or c.cache_version is distinct from p_expected_cache_version or c.refresh_claim_until is null or c.refresh_claim_until<=now() then return '{"ok":false,"code":"stale"}'::jsonb; end if;
 if not coalesce((public.assistant_ms_binding_valid(p_user_id,jsonb_build_object('tenant_id',c.tenant_id,'object_id',c.object_id,'home_account_id',c.home_account_id,'upn',c.upn,'match_basis',c.match_basis,'matched_address',c.matched_address))->>'ok')::boolean,false) then return '{"ok":false,"code":"binding_changed"}'::jsonb; end if;
 update public.assistant_ms_connections set cache_ciphertext=p_cache_ciphertext,cache_nonce=p_cache_nonce,cache_key_id=p_cache_key_id,token_expires_at=p_token_expires_at,
 cache_version=cache_version+1,refresh_claim_id=null,refresh_claim_until=null,last_error_code=null,updated_at=now() where user_id=p_user_id returning * into c;
 return jsonb_build_object('ok',true,'generation',c.generation,'cache_version',c.cache_version);
end $$;

create or replace function public.assistant_ms_refresh_release(p_user_id uuid,p_generation bigint,p_claim_id uuid,p_error_code text) returns jsonb
language plpgsql security invoker set search_path = public, pg_temp as $$
declare
 c public.assistant_ms_connections%rowtype;
 s public.assistant_ms_oauth_states%rowtype;
 
begin
 if p_error_code not in ('provider_unavailable','refresh_failed','identity_changed','cancelled') then p_error_code:='refresh_failed'; end if;
 update public.assistant_ms_connections set refresh_claim_id=null,refresh_claim_until=null,last_error_code=p_error_code,updated_at=now()
 where user_id=p_user_id and generation=p_generation and refresh_claim_id=p_claim_id;
 return jsonb_build_object('ok',found);
end $$;

create or replace function public.assistant_ms_revoke(p_user_id uuid,p_reason text,p_expected_generation bigint default null) returns jsonb
language plpgsql security invoker set search_path = public, pg_temp as $$
declare
 c public.assistant_ms_connections%rowtype;
 s public.assistant_ms_oauth_states%rowtype;
 
begin
 insert into public.assistant_ms_connections(user_id) values(p_user_id) on conflict do nothing;
 select * into c from public.assistant_ms_connections where user_id=p_user_id for update;
 if p_expected_generation is not null and c.generation<>p_expected_generation then return '{"ok":false,"code":"stale"}'::jsonb; end if;
 if p_reason not in ('user_disconnected','profile_remapped','owner_offboarded','invalid_grant','identity_changed','connection_failed') or p_reason is null then p_reason:='connection_failed'; end if;
 update public.assistant_ms_connections set generation=generation+1,cache_version=cache_version+1,status='revoked',tenant_id=null,object_id=null,home_account_id=null,upn=null,match_basis=null,matched_address=null,scopes='{}',shared_mailboxes_reachable='{}',cache_ciphertext=null,cache_nonce=null,cache_key_id=null,token_expires_at=null,refresh_claim_id=null,refresh_claim_until=null,revoked_at=now(),revoked_reason=p_reason,updated_at=now() where user_id=p_user_id returning * into c;
 update public.assistant_ms_oauth_states set flow_ciphertext=null,flow_nonce=null,flow_key_id=null,expires_at=least(expires_at,now()) where user_id=p_user_id;
 return jsonb_build_object('ok',true,'generation',c.generation);
end $$;

revoke all on function public.assistant_ms_eligible(uuid) from public,anon,authenticated;
grant execute on function public.assistant_ms_eligible(uuid) to service_role;

revoke all on function public.assistant_ms_binding_valid(uuid,jsonb) from public,anon,authenticated;
grant execute on function public.assistant_ms_binding_valid(uuid,jsonb) to service_role;

revoke all on function public.assistant_ms_begin(uuid,text,text,text,text,text) from public,anon,authenticated;
grant execute on function public.assistant_ms_begin(uuid,text,text,text,text,text) to service_role;

revoke all on function public.assistant_ms_consume(uuid,text,text,uuid) from public,anon,authenticated;
grant execute on function public.assistant_ms_consume(uuid,text,text,uuid) to service_role;

revoke all on function public.assistant_ms_complete(uuid,bigint,text,uuid,jsonb,text,text,text,timestamptz,text[],text[]) from public,anon,authenticated;
grant execute on function public.assistant_ms_complete(uuid,bigint,text,uuid,jsonb,text,text,text,timestamptz,text[],text[]) to service_role;

revoke all on function public.assistant_ms_refresh_claim(uuid,bigint,uuid) from public,anon,authenticated;
grant execute on function public.assistant_ms_refresh_claim(uuid,bigint,uuid) to service_role;

revoke all on function public.assistant_ms_refresh_commit(uuid,bigint,uuid,bigint,text,text,text,timestamptz) from public,anon,authenticated;
grant execute on function public.assistant_ms_refresh_commit(uuid,bigint,uuid,bigint,text,text,text,timestamptz) to service_role;

revoke all on function public.assistant_ms_refresh_release(uuid,bigint,uuid,text) from public,anon,authenticated;
grant execute on function public.assistant_ms_refresh_release(uuid,bigint,uuid,text) to service_role;

revoke all on function public.assistant_ms_revoke(uuid,text,bigint) from public,anon,authenticated;
grant execute on function public.assistant_ms_revoke(uuid,text,bigint) to service_role;

-- Profile edits fence existing authority in the same transaction, even with
-- runtime feature flags off. The profile row is already locked before the
-- connection, matching the OAuth RPC lock order.
create or replace function public.assistant_ms_profile_changed() returns trigger
language plpgsql security invoker set search_path = public, pg_temp as $$
begin
 if old.email is distinct from new.email or old.rfp_mailboxes is distinct from new.rfp_mailboxes
 or old.role is distinct from new.role or old.is_active is distinct from new.is_active then
  if exists(select 1 from public.assistant_ms_connections where user_id=old.id) then
   perform public.assistant_ms_revoke(old.id,
    case when old.email is distinct from new.email or old.rfp_mailboxes is distinct from new.rfp_mailboxes
    then 'profile_remapped' else 'owner_offboarded' end, null);
  end if;
 end if;
 return new;
end $$;
revoke all on function public.assistant_ms_profile_changed() from public,anon,authenticated;
grant execute on function public.assistant_ms_profile_changed() to service_role;
do $$ begin
 if not exists(select 1 from pg_trigger where tgname='assistant_ms_profile_changed'
 and tgrelid='public.profiles'::regclass) then
  create trigger assistant_ms_profile_changed before update of email,rfp_mailboxes,role,is_active
  on public.profiles for each row execute function public.assistant_ms_profile_changed();
 end if;
end $$;

create or replace function public.assistant_ms_purge_expired() returns jsonb
language plpgsql security invoker set search_path = public, pg_temp as $$
declare v_count integer;
begin
 with expired as (
  select state_hash from public.assistant_ms_oauth_states where expires_at<=now()
  order by expires_at limit 500 for update skip locked
 ), removed as (
  delete from public.assistant_ms_oauth_states s using expired e
  where s.state_hash=e.state_hash returning 1
 ) select count(*) into v_count from removed;
 return jsonb_build_object('ok',true,'deleted',v_count);
end $$;
revoke all on function public.assistant_ms_purge_expired() from public,anon,authenticated;
grant execute on function public.assistant_ms_purge_expired() to service_role;

notify pgrst, 'reload schema';
