-- 0158: Browser agent fixes (claim and heartbeat locking, cancel-requested jobs, atomic RFI notes). Apply after 0157. DEV ONLY until the release.
--
--   1. claim_browser_agent_jobs reads the worker row FOR SHARE, so a claim
--      racing browser_agent_hello (FOR UPDATE) can no longer commit leases
--      under a boot that hello already replaced; queued jobs with a cancel
--      request are skipped (the sweeper's cancel step closes them).
--   2. browser_agent_heartbeat checks the boot under a row lock and returns
--      help_updates only for the jobs it actually renewed (a worker can no
--      longer read help state of jobs it does not hold).
--   3. browser_agent_append_rfi_supplement appends one helper note to
--      rfis.external_supplement in a single statement, skipped when that
--      contribution id is already there (no lost update between writers).

-- 1 and 2. Claim and heartbeat ------------------------------------------------------

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
    select * into v_type from browser_agent_types where key = v_key and enabled;
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
               where agent_type = v_key and status = 'queued' and cancel_requested_at is null
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

-- 3. Atomic helper note append -------------------------------------------------------

-- True when the entry is on the RFI after the call (appended now or before).
create or replace function public.browser_agent_append_rfi_supplement(p_rfi uuid, p_entry jsonb)
returns boolean
language plpgsql
security invoker
set search_path = pg_catalog, public
as $$
declare
  v_probe jsonb := jsonb_build_array(jsonb_build_object('contribution_id', p_entry ->> 'contribution_id'));
begin
  if p_entry ->> 'contribution_id' is null then
    raise exception 'invalid_request';
  end if;
  update rfis
     set external_supplement = coalesce(external_supplement, '[]'::jsonb) || jsonb_build_array(p_entry)
   where id = p_rfi
     and not (coalesce(external_supplement, '[]'::jsonb) @> v_probe);
  return exists (select 1 from rfis where id = p_rfi and coalesce(external_supplement, '[]'::jsonb) @> v_probe);
end;
$$;

-- Grants -------------------------------------------------------------------------------

revoke all on function public.claim_browser_agent_jobs(uuid, uuid, jsonb, int) from public;
revoke all on function public.claim_browser_agent_jobs(uuid, uuid, jsonb, int) from anon, authenticated;
grant execute on function public.claim_browser_agent_jobs(uuid, uuid, jsonb, int) to service_role;
revoke all on function public.browser_agent_heartbeat(uuid, uuid, jsonb, inet, int) from public;
revoke all on function public.browser_agent_heartbeat(uuid, uuid, jsonb, inet, int) from anon, authenticated;
grant execute on function public.browser_agent_heartbeat(uuid, uuid, jsonb, inet, int) to service_role;
revoke all on function public.browser_agent_append_rfi_supplement(uuid, jsonb) from public;
revoke all on function public.browser_agent_append_rfi_supplement(uuid, jsonb) from anon, authenticated;
grant execute on function public.browser_agent_append_rfi_supplement(uuid, jsonb) to service_role;

notify pgrst, 'reload schema';
