-- 0191: Assistant phase 7 follow-up to 0190 (docs/ASSISTANT_PHASE7_PLAN.md 8.4, 4.2a).
-- Idempotent; function bodies only. Dev project (BDR) only until the release.
-- Replaces two 0190 functions with the same signatures; no table, column,
-- constraint or row is touched or removed.
--
-- A11.11 assistant_inbox_link_bell: a record row took the column default
--   severity 'normal'. The router now passes the catalog severity in
--   p_payload->>'severity' (low, normal, high or urgent; anything else is
--   'normal'); it lands in the column and is kept out of the stored payload.
--   A new source sets it; a linked bell leaves it. Likewise the router
--   passes the precedence rule id in p_payload->>'precedence' (R13 and the
--   other record rules; a text matching ^R[0-9]+[a-z]?$ of at most 16
--   characters, anything else is null): it lands in the precedence column,
--   which 0190 left null on this path, and is kept out of the stored payload.
--   A new source sets it; a linked bell leaves it.
-- A11.14 assistant_inbox_fold: the fold overwrote precedence with
--   'quiet_hours', so a row merged by a precedence rule (R4b, R6, R17: in-app
--   only, no Teams) that was held, folded and un-folded lost its rule and
--   could get a Teams message. The fold now keeps the prior precedence in
--   payload->'folded_from_precedence' (JSON null when it had none) and the
--   un-fold restores it and removes the key. While folded the row still
--   reads precedence 'quiet_hours', so the un-fold selector
--   (repo.inbox_folded_pending) and the digest's "High:" line are unchanged.
--   A row folded before this file (no key) keeps 'quiet_hours' on un-fold,
--   exactly as under 0190.
--
-- Conventions as 0190: SECURITY INVOKER, search_path pinned to pg_catalog,
-- public, execute for service_role only (0145 pattern, re-stated below).

-- A11.11 Email-ID collapse for record rows (decision E, 4.2a). One row per inbox key;
--        a new bell N replaces the source S when its rank is higher, or ranks tie and
--        N is more than 120 s after S (a later ingest: the row's created_at moves to
--        N), or ranks tie within 120 s and (bell_at, id) of N is smaller (same ingest:
--        the earliest wins). Otherwise N is linked. Record rows only.
--        Known limitation: the final row is order independent for two bells only.
--        With three or more equal-rank bells spanning more than 120 s the 120 s test
--        chains (it is measured from the current source, not a fixed anchor), so the
--        final source can depend on arrival order (A 0 s, B 100 s, C 200 s: order
--        A B C ends on C, order C B A ends on A). Any fixed anchor still chains;
--        the plan's two-bell golden cases hold.
--        0191: severity comes from p_payload->>'severity' and the precedence rule id
--        from p_payload->>'precedence'; both follow the source.
create or replace function public.assistant_inbox_link_bell(
  p_user_id uuid, p_dedupe_key text, p_notification_id uuid, p_bell_at timestamptz, p_event_type text,
  p_rank int, p_title text, p_why text, p_payload jsonb, p_project_id uuid, p_now timestamptz default now()
)
returns jsonb
language plpgsql
volatile
security invoker
set search_path = pg_catalog, public
as $$
declare
  v_id uuid;
  v_row public.assistant_inbox;
  v_rank int;
  v_at timestamptz;
  v_linked jsonb;
  v_new_source boolean;
  v_later boolean;
  v_severity text;
  v_precedence text;
  v_payload jsonb;
begin
  if p_dedupe_key is null or p_dedupe_key not like 'p7:bell:%' or p_notification_id is null
     or p_bell_at is null or p_rank is null or p_title is null then
    raise exception 'assistant_inbox_link_bell: bad arguments' using errcode = 'check_violation';
  end if;
  v_severity := case when p_payload->>'severity' in ('low', 'normal', 'high', 'urgent')
                     then p_payload->>'severity' else 'normal' end;
  v_precedence := case when char_length(p_payload->>'precedence') <= 16
                            and p_payload->>'precedence' ~ '^R[0-9]+[a-z]?$'
                       then p_payload->>'precedence' end;
  v_payload := coalesce(p_payload, '{}'::jsonb) - 'severity' - 'precedence';
  insert into public.assistant_inbox
    (user_id, dedupe_key, event_type, severity, title, why, payload, project_id, delivery,
     source_notification_id, delivered_at, created_at, precedence)
  values (p_user_id, p_dedupe_key, p_event_type, v_severity, p_title, p_why,
          v_payload
            || jsonb_build_object('rank', p_rank, 'source_bell_at', p_bell_at,
                                  'linked_notification_ids', '[]'::jsonb),
          p_project_id, 'record', p_notification_id, p_now, p_now, v_precedence)
  on conflict (user_id, dedupe_key) do nothing
  returning id into v_id;
  if v_id is not null then
    return jsonb_build_object('inbox_id', v_id, 'linked', false);
  end if;

  select * into v_row from public.assistant_inbox i
   where i.user_id = p_user_id and i.dedupe_key = p_dedupe_key for update;
  if v_row.id is null or v_row.delivery <> 'record' then
    return jsonb_build_object('inbox_id', v_row.id, 'linked', false, 'code', 'not_record');
  end if;
  v_linked := coalesce(v_row.payload->'linked_notification_ids', '[]'::jsonb);
  if v_row.source_notification_id = p_notification_id or v_linked ? p_notification_id::text then
    return jsonb_build_object('inbox_id', v_row.id, 'linked', true, 'noop', true);
  end if;
  v_rank := coalesce((v_row.payload->>'rank')::int, 0);
  v_at := coalesce((v_row.payload->>'source_bell_at')::timestamptz, v_row.created_at);
  v_later := p_rank = v_rank and p_bell_at > v_at + interval '120 seconds';
  v_new_source := p_rank > v_rank
    or v_later
    or (p_rank = v_rank and p_bell_at >= v_at - interval '120 seconds'
        and (p_bell_at, p_notification_id) < (v_at, v_row.source_notification_id));

  if v_new_source then
    -- S joins the linked list: distinct, arrival order, at most 20 entries.
    if v_row.source_notification_id is not null then
      v_linked := v_linked || jsonb_build_array(v_row.source_notification_id::text);
    end if;
    v_linked := (select coalesce(jsonb_agg(d.x order by d.o), '[]'::jsonb)
                   from (select t.x, min(t.o) as o
                           from jsonb_array_elements_text(v_linked) with ordinality t(x, o)
                          where t.x <> p_notification_id::text
                          group by t.x order by min(t.o) limit 20) d);
    update public.assistant_inbox i
       set event_type = p_event_type, severity = v_severity, title = p_title, why = p_why,
           precedence = v_precedence, source_notification_id = p_notification_id,
           project_id = coalesce(p_project_id, i.project_id),
           payload = v_payload
             || jsonb_build_object('rank', p_rank, 'source_bell_at', p_bell_at, 'linked_notification_ids', v_linked),
           created_at = case when v_later then p_bell_at else i.created_at end,
           dismissed_at = case when v_later and i.dismissed_reason = 'user' then null else i.dismissed_at end,
           dismissed_reason = case when v_later and i.dismissed_reason = 'user' then null else i.dismissed_reason end
     where i.id = v_row.id;
  elsif jsonb_array_length(v_linked) < 20 then
    update public.assistant_inbox i
       set payload = i.payload || jsonb_build_object('linked_notification_ids',
                                                     v_linked || jsonb_build_array(p_notification_id::text))
     where i.id = v_row.id;
  end if;
  return jsonb_build_object('inbox_id', v_row.id, 'linked', true, 'source', v_new_source);
end $$;

-- A11.14 Quiet-hours fold and un-fold (8.4), under the per-user lock of A11.3 and
--        A11.4. A held row (high, not approval-expiring, deliver_after > created_at)
--        folds into the AM digest while no AM ledger row exists and p_now is before
--        p_fold_until; a folded row returns to immediate once the AM digest went out
--        without it or the window closed. Returns folded, deliver, wait or none.
--        0191: the fold keeps the prior precedence in payload (folded_from_precedence)
--        and the un-fold restores it, so a merge rule still means no Teams.
create or replace function public.assistant_inbox_fold(
  p_user_id uuid, p_inbox_id uuid, p_am_date date, p_am_slot_at timestamptz, p_fold_until timestamptz,
  p_now timestamptz default now()
)
returns text
language plpgsql
volatile
security invoker
set search_path = pg_catalog, public
as $$
declare
  v_row public.assistant_inbox;
  v_am boolean;
begin
  perform pg_advisory_xact_lock(hashtext('assistant_inbox_deliver:' || p_user_id::text));
  select * into v_row from public.assistant_inbox i
   where i.id = p_inbox_id and i.user_id = p_user_id for update;
  if not found then
    return 'none';
  end if;
  v_am := p_am_date is not null and exists (
    select 1 from public.assistant_digests d
     where d.user_id = p_user_id and d.digest_date = p_am_date and d.slot = 'am');
  if v_row.delivery = 'immediate' and v_row.delivered_at is null and v_row.dismissed_at is null
     and v_row.deliver_after <= p_now and v_row.severity = 'high'
     and coalesce(v_row.event_type, '') <> 'assistant.approval_expiring'
     and v_row.deliver_after > v_row.created_at then
    if p_am_date is not null and p_am_slot_at is not null and p_fold_until is not null
       and p_now < p_fold_until and not v_am then
      update public.assistant_inbox i
         set delivery = 'digest', precedence = 'quiet_hours', deliver_after = p_am_slot_at,
             payload = i.payload || jsonb_build_object('folded_from_precedence', i.precedence)
       where i.id = v_row.id;
      return 'folded';
    end if;
    return 'deliver';
  end if;
  if v_row.delivery = 'digest' and v_row.precedence = 'quiet_hours'
     and v_row.delivered_at is null and v_row.dismissed_at is null then
    if v_am or p_fold_until is null or p_now >= p_fold_until then
      update public.assistant_inbox i
         set delivery = 'immediate', deliver_after = p_now,
             precedence = case when i.payload ? 'folded_from_precedence'
                               then i.payload->>'folded_from_precedence' else i.precedence end,
             payload = i.payload - 'folded_from_precedence'
       where i.id = v_row.id;
      return 'deliver';
    end if;
    return 'wait';
  end if;
  return 'none';
end $$;

-- Execute for service_role only (0145 pattern; create or replace keeps the 0190
-- grants, re-stated so this file stands alone).
revoke all on function public.assistant_inbox_link_bell(uuid, text, uuid, timestamptz, text, int, text, text, jsonb, uuid, timestamptz) from public;
revoke all on function public.assistant_inbox_link_bell(uuid, text, uuid, timestamptz, text, int, text, text, jsonb, uuid, timestamptz) from anon, authenticated;
grant execute on function public.assistant_inbox_link_bell(uuid, text, uuid, timestamptz, text, int, text, text, jsonb, uuid, timestamptz) to service_role;
revoke all on function public.assistant_inbox_fold(uuid, uuid, date, timestamptz, timestamptz, timestamptz) from public;
revoke all on function public.assistant_inbox_fold(uuid, uuid, date, timestamptz, timestamptz, timestamptz) from anon, authenticated;
grant execute on function public.assistant_inbox_fold(uuid, uuid, date, timestamptz, timestamptz, timestamptz) to service_role;

notify pgrst, 'reload schema';
