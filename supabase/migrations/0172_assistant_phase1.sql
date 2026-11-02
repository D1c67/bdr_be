-- 0172: Assistant phase 1, confirmed writes (docs/ASSISTANT_PHASE1_PLAN.md, section 2). Apply after 0170 and 0171. Idempotent; safe to run twice. Dev project (BDR) only until the release.
--
-- Release order: 0170, 0171 and 0172 are applied to the target database
-- before the backend deploy, never after. The provenance selects and inserts
-- in the backend name the columns added here and do not tolerate a missing
-- column (PGRST204 on writes, 42703 on reads).
--
-- Sections:
--   A   actor provenance columns on 14 tables, plus five read-side indexes.
--   A2  labor_reviews provenance guard trigger.
--   B   approvals: reverts_approval_id, the one-revert index, lookup indexes,
--       assistant_tool_results.taint_corpus, the supersede RPC, the widened
--       immutability function, and the no delete and no truncate triggers.
--   C   the decide RPC (the compare-and-set behind Approve and Reject).
--
-- This file has no drop statement at all. Constraints are added behind
-- pg_constraint guards; functions and triggers use create or replace.

-- Section A: actor provenance (docs/ASSISTANT.md "Actor provenance details";
-- the design calls this 0171, which is taken by 0171_assistant_delegation_aal).
do $$
declare t text;
begin
  foreach t in array array['audit_log','stage_events','pm_stage_events','estimator_notes',
                           'quote_notes','file_access_events','email_log','proposal_send_events',
                           'labor_reviews','project_files',
                           'projects','project_gcs','todos','bid_split_jobs']
  loop
    execute format('alter table public.%I add column if not exists actor_kind text not null default ''human''', t);
    execute format('alter table public.%I add column if not exists assistant_run_id uuid', t);
    if not exists (select 1 from pg_constraint where conname = t || '_actor_kind_chk') then
      execute format('alter table public.%I add constraint %I check (actor_kind in (''human'',''assistant'',''system'')) not valid', t, t || '_actor_kind_chk');
      execute format('alter table public.%I validate constraint %I', t, t || '_actor_kind_chk');
    end if;
    if not exists (select 1 from pg_constraint where conname = t || '_assistant_run_chk') then
      execute format('alter table public.%I add constraint %I check (actor_kind <> ''assistant'' or assistant_run_id is not null) not valid', t, t || '_assistant_run_chk');
      execute format('alter table public.%I validate constraint %I', t, t || '_assistant_run_chk');
    end if;
    execute format('create index if not exists %I on public.%I (assistant_run_id) where assistant_run_id is not null', t || '_assistant_run_idx', t);
  end loop;
end $$;

create index if not exists stage_events_actor_idx on public.stage_events (actor_id, entered_at desc);
create index if not exists project_open_events_user_idx on public.project_open_events (user_id, opened_at desc);
create index if not exists email_log_sent_by_idx on public.email_log (sent_by, created_at desc);
-- New name: proposal_send_events_sent_by_idx already exists on (sent_by) alone (0104:52).
create index if not exists proposal_send_events_sent_by_created_idx on public.proposal_send_events (sent_by, created_at desc);
create index if not exists ingested_emails_mailbox_msg_idx on public.ingested_emails (mailbox, message_at desc);

-- Section A2: labor_reviews provenance guard.
-- (1) 0140 undo_external_submission restores a pre-0172 snapshot with
--     "insert into labor_reviews select * from jsonb_populate_record(...)",
--     which supplies an explicit NULL actor_kind (NOT NULL is checked after
--     BEFORE triggers, so filling it here keeps the undo working).
-- (2) 0140 apply_external_submission and 0159 auto_send_accept upsert the row
--     without touching the provenance columns. A save that leaves the run id
--     unchanged is not the assistant's, so the row goes back to human.
create or replace function public.labor_reviews_actor_provenance()
returns trigger
language plpgsql
security invoker
set search_path = pg_catalog, public
as $$
begin
  if new.actor_kind is null then
    new.actor_kind := 'human';
    new.assistant_run_id := null;
    return new;
  end if;
  if tg_op = 'UPDATE' and old.actor_kind = 'assistant'
     and new.assistant_run_id is not distinct from old.assistant_run_id then
    new.actor_kind := 'human';
    new.assistant_run_id := null;
  end if;
  return new;
end;
$$;
create or replace trigger labor_reviews_actor_provenance
  before insert or update on public.labor_reviews
  for each row execute function public.labor_reviews_actor_provenance();

-- Trigger functions fire whatever the caller's EXECUTE privilege, so this
-- only keeps the function off the PostgREST RPC surface (house style, 0170).
revoke all on function public.labor_reviews_actor_provenance() from public;
revoke all on function public.labor_reviews_actor_provenance() from anon, authenticated;
grant execute on function public.labor_reviews_actor_provenance() to service_role;

-- Section B: approvals (phase 1).
alter table public.assistant_approvals add column if not exists reverts_approval_id uuid;

-- At most one live or executed revert per executed approval.
create unique index if not exists assistant_approvals_one_revert_uq
  on public.assistant_approvals (reverts_approval_id)
  where reverts_approval_id is not null
    and status in ('pending', 'approved', 'executing', 'executed');
create index if not exists assistant_approvals_message_idx
  on public.assistant_approvals (message_id) where message_id is not null;
create index if not exists assistant_tool_calls_approval_idx
  on public.assistant_tool_calls (approval_id, phase) where approval_id is not null;
create index if not exists assistant_tool_results_tainted_idx
  on public.assistant_tool_results (conversation_id, created_at desc) where tainted;

-- The binding corpus of a tainted result, extracted before truncation:
-- [{"f": <taint field>, "t": <whitespace-collapsed text>}]. Null for phase 0
-- rows and untainted results.
alter table public.assistant_tool_results add column if not exists taint_corpus jsonb;

-- Supersede in one transaction: the old row leaves executing (expired,
-- changed_since_preview, superseded_by set) before the new pending row is
-- inserted, so a revert card can be superseded although
-- assistant_approvals_one_revert_uq covers executing. The insert names its
-- columns; any error in it rolls the expiry back with it.
create or replace function public.assistant_approval_supersede(
  p_user_id uuid,
  p_old_id uuid,
  p_new jsonb
)
returns jsonb
language plpgsql
volatile
security invoker
set search_path = pg_catalog, public
as $$
declare
  v_old public.assistant_approvals;
  v_new public.assistant_approvals;
  v_new_id uuid := (p_new->>'id')::uuid;
begin
  if v_new_id is null or (p_new->>'user_id')::uuid is distinct from p_user_id then
    raise exception 'assistant_approval_supersede: bad new row' using errcode = 'check_violation';
  end if;
  update public.assistant_approvals a
     set status = 'expired', expired_reason = 'changed_since_preview', superseded_by = v_new_id
   where a.id = p_old_id and a.user_id = p_user_id and a.status = 'executing'
  returning * into v_old;
  if not found then
    return jsonb_build_object('ok', false, 'reason', 'not_executing', 'row', null);
  end if;
  insert into public.assistant_approvals (
    id, user_id, user_label, conversation_id, message_id, project_id, action_kind,
    payload, payload_sha256, preview, precondition, status, expires_at, reverts_approval_id
  ) values (
    v_new_id, p_user_id, p_new->>'user_label', (p_new->>'conversation_id')::uuid,
    (p_new->>'message_id')::uuid, (p_new->>'project_id')::uuid, p_new->>'action_kind',
    p_new->'payload', p_new->>'payload_sha256', p_new->'preview', p_new->'precondition',
    'pending', (p_new->>'expires_at')::timestamptz, (p_new->>'reverts_approval_id')::uuid
  )
  returning * into v_new;
  return jsonb_build_object('ok', true, 'reason', null, 'row', to_jsonb(v_new));
end;
$$;

revoke all on function public.assistant_approval_supersede(uuid, uuid, jsonb) from public;
revoke all on function public.assistant_approval_supersede(uuid, uuid, jsonb) from anon, authenticated;
grant execute on function public.assistant_approval_supersede(uuid, uuid, jsonb) to service_role;

-- Replaces the 0170 body (same name, same trigger binding, so no trigger
-- change). Mutable after insert, by design: status (forward only),
-- expired_reason, decided_by, decided_at, decided_session_aal,
-- execute_job_id, executed_at, result, tool_call_id, superseded_by (once),
-- email_log_id, updated_at.
create or replace function public.assistant_approval_immutable()
returns trigger
language plpgsql
security invoker
set search_path = pg_catalog, public
as $$
declare
  v_old int;
  v_new int;
begin
  if new.payload is distinct from old.payload
     or new.payload_sha256 is distinct from old.payload_sha256
     or new.preview is distinct from old.preview
     or new.precondition is distinct from old.precondition
     or new.user_id is distinct from old.user_id
     or new.user_label is distinct from old.user_label
     or new.action_kind is distinct from old.action_kind
     or new.expires_at is distinct from old.expires_at
     or new.project_id is distinct from old.project_id
     or new.conversation_id is distinct from old.conversation_id
     or new.message_id is distinct from old.message_id
     or new.reverts_approval_id is distinct from old.reverts_approval_id
     or new.created_at is distinct from old.created_at then
    raise exception 'assistant_approvals: the card content and its links are immutable'
      using errcode = 'restrict_violation';
  end if;
  if old.superseded_by is not null and new.superseded_by is distinct from old.superseded_by then
    raise exception 'assistant_approvals: superseded_by is set once'
      using errcode = 'restrict_violation';
  end if;
  if new.status is distinct from old.status then
    v_old := case old.status when 'pending' then 0 when 'approved' then 1 when 'executing' then 2 else 3 end;
    v_new := case new.status when 'pending' then 0 when 'approved' then 1 when 'executing' then 2 else 3 end;
    if v_new <= v_old then
      raise exception 'assistant_approvals: status cannot move from % to %', old.status, new.status
        using errcode = 'restrict_violation';
    end if;
  end if;
  return new;
end;
$$;

-- Approvals are a ledger: no delete, no truncate (no FK cascades reach them).
create or replace trigger assistant_approvals_no_delete before delete on public.assistant_approvals
  for each row execute function public.assistant_refuse_change();
create or replace trigger assistant_approvals_no_truncate before truncate on public.assistant_approvals
  for each statement execute function public.assistant_refuse_change();

-- Section C: the decide RPC (the compare-and-set). a.precondition =
-- p_precondition is jsonb structural equality (key order and whitespace do
-- not matter), which is why the CAS lives in an RPC and not in a PostgREST
-- filter. Reject ignores the hash and the precondition.
create or replace function public.assistant_approval_decide(
  p_user_id uuid,
  p_approval_id uuid,
  p_decision text,
  p_payload_sha256 text,
  p_precondition jsonb,
  p_session_aal text
)
returns jsonb
language plpgsql
volatile
security invoker
set search_path = pg_catalog, public
as $$
declare
  v_row public.assistant_approvals;
begin
  if p_decision not in ('approve', 'reject') then
    raise exception 'assistant_approval_decide: bad decision %', p_decision using errcode = 'check_violation';
  end if;
  update public.assistant_approvals a
     set status = case p_decision when 'approve' then 'approved' else 'rejected' end,
         decided_by = p_user_id,
         decided_at = now(),
         decided_session_aal = p_session_aal
   where a.id = p_approval_id
     and a.user_id = p_user_id
     and a.status = 'pending'
     and a.expires_at > now()
     and (p_decision = 'reject'
          or (a.payload_sha256 = p_payload_sha256 and a.precondition = p_precondition))
  returning * into v_row;
  if found then
    return jsonb_build_object('ok', true, 'reason', null, 'row', to_jsonb(v_row));
  end if;

  select * into v_row from public.assistant_approvals a
   where a.id = p_approval_id and a.user_id = p_user_id;
  if not found then
    return jsonb_build_object('ok', false, 'reason', 'not_found', 'row', null);
  end if;
  if v_row.status = 'pending' and v_row.expires_at <= now() then
    update public.assistant_approvals a
       set status = 'expired', expired_reason = 'timed_out'
     where a.id = p_approval_id and a.status = 'pending'
    returning * into v_row;
    return jsonb_build_object('ok', false, 'reason', 'expired', 'row', to_jsonb(v_row));
  end if;
  if v_row.status = 'expired' and v_row.superseded_by is not null then
    return jsonb_build_object('ok', false, 'reason', 'superseded', 'row', to_jsonb(v_row));
  end if;
  if v_row.status = 'expired' then
    return jsonb_build_object('ok', false, 'reason', 'expired', 'row', to_jsonb(v_row));
  end if;
  if v_row.status <> 'pending' then
    return jsonb_build_object('ok', false, 'reason', 'not_pending', 'row', to_jsonb(v_row));
  end if;
  return jsonb_build_object('ok', false, 'reason', 'stale', 'row', to_jsonb(v_row));
end;
$$;

revoke all on function public.assistant_approval_decide(uuid, uuid, text, text, jsonb, text) from public;
revoke all on function public.assistant_approval_decide(uuid, uuid, text, text, jsonb, text) from anon, authenticated;
grant execute on function public.assistant_approval_decide(uuid, uuid, text, text, jsonb, text) to service_role;

notify pgrst, 'reload schema';
