-- Complete 0187 with user claim admission and permanent legacy-path isolation.
alter table public.browser_agent_enrollments add column if not exists principal_kind text not null default 'org';
do $$ begin
 if not exists(select 1 from pg_constraint where conname='browser_agent_enrollments_principal_kind_check') then
 alter table public.browser_agent_enrollments add constraint browser_agent_enrollments_principal_kind_check check(principal_kind in ('org','user')); end if;
 if not exists(select 1 from pg_trigger where tgname='assistant_browser_enrollment_pool_guard' and tgrelid='public.browser_agent_enrollments'::regclass) then
 create trigger assistant_browser_enrollment_pool_guard before update on public.browser_agent_enrollments for each row execute function public.assistant_browser_pool_guard(); end if;
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
  if c.id is null or c.user_id is distinct from j.principal_user_id or c.version is distinct from j.user_credential_version or c.status<>'active' then return; end if;
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

-- Legacy trace tables can never receive a user job's plaintext, even if an
-- internal caller accidentally bypasses the HTTP/protocol boundary.
create or replace function public.assistant_browser_reject_legacy_trace()
returns trigger language plpgsql security invoker set search_path=pg_catalog,public as $$
declare v_job uuid;
begin
 if tg_table_name='browser_agent_help_requests' then
  if new.principal_kind='user' then raise exception 'user_trace_requires_encryption'; end if;
  v_job:=new.opened_by_job_id;
 else v_job:=new.job_id; end if;
 if exists(select 1 from public.browser_agent_jobs where id=v_job and principal_kind='user') then raise exception 'user_trace_requires_encryption'; end if;
 return new;
end $$;
do $$ declare t text; begin
 foreach t in array array['browser_agent_steps','browser_agent_turns','browser_agent_artifacts','browser_agent_help_requests'] loop
 if not exists(select 1 from pg_trigger where tgname='assistant_browser_private_trace' and tgrelid=('public.'||t)::regclass) then
 execute format('create trigger assistant_browser_private_trace before insert or update on public.%I for each row execute function public.assistant_browser_reject_legacy_trace()',t);
 end if; end loop;
end $$;


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
  if v_worker.principal_kind='user' then
    return query select * from public.assistant_browser_claim_user(p_worker,p_boot,p_capacity,p_lease_seconds);
    return;
  end if;
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
  if v_job.principal_kind='user' then raise exception 'user_protocol_required'; end if;
  return v_job;
end;
$$;


create or replace function public.browser_agent_heartbeat(
  p_worker uuid, p_boot uuid, p_jobs jsonb, p_ip inet default null, p_lease_seconds int default 90)
returns jsonb
language plpgsql
security invoker
set search_path = pg_catalog, public
as $$
declare
  v_worker browser_agent_workers;
  v_item record;
  v_job browser_agent_jobs;
  v_renewed jsonb := '[]'::jsonb;
  v_lost jsonb := '[]'::jsonb;
  v_cancel jsonb := '[]'::jsonb;
  v_listed uuid[] := '{}';
  v_renewed_ids uuid[] := '{}';
begin
  select * into v_worker from browser_agent_workers where id = p_worker for no key update;
  if not found then
    raise exception 'unauthorized';
  end if;
  if v_worker.principal_kind='user' then raise exception 'user_protocol_required'; end if;
  if v_worker.status = 'revoked' then
    raise exception 'revoked';
  end if;
  if v_worker.status <> 'active' or v_worker.boot_id is distinct from p_boot then
    raise exception 'stale_boot';
  end if;
  update browser_agent_workers set last_seen_at = now(), last_ip = coalesce(p_ip, last_ip) where id = p_worker;

  for v_item in
    select (e ->> 'job_id')::uuid as job_id, (e ->> 'epoch')::bigint as epoch,
           least(greatest(coalesce((e ->> 'active_seconds_delta')::int, 0), 0), 30) as delta
      from jsonb_array_elements(coalesce(p_jobs, '[]'::jsonb)) e
  loop
    v_listed := v_listed || v_item.job_id;
    update browser_agent_jobs
       set lease_expires_at = now() + make_interval(secs => p_lease_seconds),
           active_seconds = active_seconds + case when status = 'running' then v_item.delta else 0 end
     where id = v_item.job_id and lease_owner = p_worker and lease_epoch = v_item.epoch
       and lease_boot_id = p_boot and status in ('running', 'waiting_help')
    returning * into v_job;
    if found then
      v_renewed_ids := v_renewed_ids || v_job.id;
      v_renewed := v_renewed || jsonb_build_object('job_id', v_job.id, 'lease_expires_at', v_job.lease_expires_at);
      if v_job.cancel_requested_at is not null then
        v_cancel := v_cancel || to_jsonb(v_job.id);
      end if;
    else
      v_lost := v_lost || to_jsonb(v_item.job_id);
    end if;
  end loop;

  return jsonb_build_object(
    'config_version', (select config_version from browser_agent_settings where id = 1),
    'renewed', v_renewed,
    'lost', v_lost,
    'cancel', v_cancel,
    'orphans', coalesce((
      select jsonb_agg(jsonb_build_object('job_id', j.id, 'epoch', j.lease_epoch))
        from browser_agent_jobs j
       where j.lease_owner = p_worker and j.lease_boot_id = p_boot
         and j.status in ('running', 'waiting_help') and not (j.id = any (v_listed))), '[]'::jsonb),
    'help_updates', coalesce((
      select jsonb_agg(jsonb_build_object(
               'job_id', w.job_id, 'help_id', h.id, 'status', h.status,
               'latest_message_id', (select max(m.id) from browser_agent_help_messages m where m.help_id = h.id)))
        from browser_agent_help_waiters w
        join browser_agent_help_requests h on h.id = w.help_id
       where w.job_id = any (v_renewed_ids)), '[]'::jsonb),
    'drain', v_worker.draining,
    'controls', '[]'::jsonb);
end;
$$;


create or replace function public.browser_agent_spend_today()
returns jsonb
language sql
stable
security invoker
set search_path = pg_catalog, public
as $$
  with w as (
    select greatest(
             (date_trunc('day', now() at time zone 'America/Los_Angeles') at time zone 'America/Los_Angeles'),
             coalesce(rs.cost_window_started_at, '-infinity'::timestamptz)) as started_at
      from browser_agent_runtime_state rs where rs.id = 1
  )
  select jsonb_build_object(
           'window_started_at', w.started_at,
           'spend_usd', coalesce((select sum(t.cost_usd) from browser_agent_turns t where t.created_at >= w.started_at), 0)
             +coalesce((select sum(coalesce(t.actual_cost,t.estimated_cost)) from assistant_browser_turns t where t.created_at>=w.started_at),0),
           'daily_cap_usd', s.daily_cost_cap_usd,
           'per_job_cap_usd', s.per_job_cost_cap_usd)
    from w, browser_agent_settings s where s.id = 1;
$$;

create or replace function public.browser_agent_hello(p_worker uuid, p_boot uuid, p_info jsonb, p_ip inet default null)
returns jsonb
language plpgsql
security invoker
set search_path = pg_catalog, public
as $$
declare
  v_worker browser_agent_workers;
  v_count int;
  v_job browser_agent_jobs;
  v_max int;
  v_requeued int := 0;
  v_failed int := 0;
  v_parked int := 0;
begin
  select * into v_worker from browser_agent_workers where id = p_worker for update;
  if not found then
    raise exception 'unauthorized';
  end if;
  if v_worker.status = 'revoked' then
    raise exception 'revoked';
  end if;

  if v_worker.hello_window_at is null or v_worker.hello_window_at < now() - interval '1 hour' then
    update browser_agent_workers set hello_window_at = now(), hello_count_hour = 1 where id = p_worker;
    v_count := 1;
  else
    update browser_agent_workers set hello_count_hour = hello_count_hour + 1 where id = p_worker
    returning hello_count_hour into v_count;
  end if;

  update browser_agent_workers
     set boot_id = p_boot,
         boot_at = now(),
         version = coalesce(p_info ->> 'version', version),
         chromium_version = coalesce(p_info ->> 'chromium_version', chromium_version),
         bad_versions = coalesce(array(select jsonb_array_elements_text(p_info -> 'bad_versions')), bad_versions),
         capabilities = coalesce(p_info -> 'capabilities', capabilities),
         last_seen_at = now(),
         last_ip = coalesce(p_ip, last_ip)
   where id = p_worker;

  if v_count > 6 and v_worker.status = 'active' and not v_worker.draining then
    update browser_agent_workers set draining = true where id = p_worker;
  end if;

  -- A new user-pool boot conservatively stops old user jobs. It does not
  -- replay an uncertain provider turn or persist plaintext legacy steps.
  if v_worker.principal_kind='user' then
    for v_job in select * from browser_agent_jobs where lease_owner=p_worker
      and lease_boot_id is distinct from p_boot and principal_kind='user'
      and status in ('running','waiting_help') order by principal_user_id,id loop
      perform pg_advisory_xact_lock(hashtextextended('assistant-browser-owner:'||v_job.principal_user_id::text,0));
      update browser_agent_jobs set status='failed',error_kind='worker_restarted',error=null,finished_at=now(),
        lease_owner=null,lease_boot_id=null,lease_expires_at=null,hold_until=null
        where id=v_job.id and lease_owner=p_worker and lease_boot_id is distinct from p_boot;
      v_failed:=v_failed+1;
    end loop;
    return jsonb_build_object('status',case when v_worker.status='pending' then 'pending' when v_count>6 then 'suspended' else 'active' end,
      'hello_count_hour',v_count,'requeued',0,'failed',v_failed,'parked',0,
      'config_version',(select config_version from browser_agent_settings where id=1),'server_time',now());
  end if;

  for v_job in
    select * from browser_agent_jobs
     where lease_owner = p_worker and lease_boot_id is distinct from p_boot
       and status in ('running', 'waiting_help')
     for update
  loop
    if v_job.status = 'waiting_help' then
      update browser_agent_jobs
         set lease_owner = null, lease_boot_id = null, lease_expires_at = null, hold_until = null
       where id = v_job.id;
      perform browser_agent_log_step(v_job.id, v_job.lease_epoch, p_worker, 'job.parked', 'bdr',
                                     'Parked: the worker restarted while waiting for help');
      v_parked := v_parked + 1;
      continue;
    end if;
    select max_interruptions into v_max from browser_agent_types where key = v_job.agent_type;
    if v_job.interrupted_count + 1 > coalesce(v_max, 20) then
      update browser_agent_jobs
         set status = 'failed', error_kind = 'interrupted_repeatedly', error = 'Interrupted too many times',
             interrupted_count = interrupted_count + 1, lease_epoch = lease_epoch + 1,
             lease_owner = null, lease_boot_id = null, lease_expires_at = null, finished_at = now()
       where id = v_job.id;
      perform browser_agent_log_step(v_job.id, v_job.lease_epoch, p_worker, 'job.failed', 'bdr',
                                     'Failed: interrupted too many times');
      v_failed := v_failed + 1;
    else
      update browser_agent_jobs
         set status = 'queued', resume_reason = 'interrupted', interrupted_count = interrupted_count + 1,
             lease_epoch = lease_epoch + 1, lease_owner = null, lease_boot_id = null, lease_expires_at = null
       where id = v_job.id;
      perform browser_agent_log_step(v_job.id, v_job.lease_epoch, p_worker, 'job.interrupted', 'bdr',
                                     'Requeued: the worker restarted (new boot)');
      v_requeued := v_requeued + 1;
    end if;
  end loop;

  select * into v_worker from browser_agent_workers where id = p_worker;
  return jsonb_build_object(
    'status', case when v_worker.status = 'pending' then 'pending'
                   when v_count > 6 then 'suspended' else 'active' end,
    'hello_count_hour', v_count,
    'requeued', v_requeued, 'failed', v_failed, 'parked', v_parked,
    'config_version', (select config_version from browser_agent_settings where id = 1),
    'server_time', now());
end;
$$;

create or replace function public.assistant_browser_task_enqueue(
 p_user_id uuid,p_approval_id uuid,p_run_id uuid,p_payload_sha256 text,p_input jsonb,
 p_user_cap_default numeric default 5,p_global_cap_default numeric default 50)
returns jsonb language plpgsql security invoker set search_path=pg_catalog,public as $$
declare a public.assistant_approvals; c public.assistant_credentials; j public.browser_agent_jobs;
 v_hold jsonb; v_cost numeric; v_type public.browser_agent_types; v_settings public.browser_agent_settings;v_role text;
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
 if p_input->>'url' is null or p_input->>'url' !~ '^https://app\.procore\.com/([0-9]{1,20}/project/(home|rfi(/list)?|rfi/show/[0-9]{1,20})|webclients/host/companies/[0-9]{1,20}/projects/[0-9]{1,20}/tools/(projecthome|rfis(/[0-9]{1,20})?))/?$'
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

create or replace function public.assistant_browser_runtime_ready()
returns boolean language sql immutable security invoker set search_path=pg_catalog,public as $$ select true $$;
-- Existing environment/global/profile/type kill switches still apply.
revoke all on function public.assistant_browser_claim_user(uuid,uuid,jsonb,int) from public,anon,authenticated;
grant execute on function public.assistant_browser_claim_user(uuid,uuid,jsonb,int) to service_role;
revoke all on function public.assistant_browser_reject_legacy_trace() from public,anon,authenticated;
grant execute on function public.assistant_browser_reject_legacy_trace() to service_role;
notify pgrst,'reload schema';
