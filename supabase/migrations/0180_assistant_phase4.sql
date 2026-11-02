-- 0180: Assistant phase 4: knowledge, reports and evidence (docs/ASSISTANT_PHASE4_PLAN.md, section 2).
-- Idempotent; safe to run twice. Dev project (BDR) only until the release.
-- 0179 belongs to another build; nothing here touches it.
-- Never re-lists llm_jobs_job_type_check (scripts/check_llm_jobs_constraint_relists.py).
--
-- Two parts split by the PART B marker line below. Part A goes through
-- apply_migration as assistant_phase4_a. Part B (the check swaps and the
-- ledger trigger bindings) goes through with the owner at the screen, or
-- through execute_sql with the identical text plus a ledger row
-- assistant_phase4_b, as phase 3 part C did.
--
-- ==== PART A ====

-- A1. Plans notes: one row per note-like item of the latest output versions of
--     an AI Estimating run, for cheap search (docs 3.3). Bodies are model
--     output derived from untrusted PDFs. No project_id (the 0141 project
--     removal walks FKs; project to runs resolves through
--     plans_reader_runs.project_id). category is '' (never null) for main and
--     brief rows, so the key is a plain unique constraint that PostgREST
--     on_conflict can name (an expression index on coalesce(category, '')
--     answers 42P10).
create table if not exists public.plans_reader_notes (
  id              uuid primary key default gen_random_uuid(),
  run_id          uuid not null references public.plans_reader_runs(id) on delete cascade,
  source_scope    text not null check (source_scope in ('category', 'main', 'brief_deliverable')),
  category        text not null default '' check (char_length(category) <= 60),
  source_version  int not null check (source_version >= 1),
  source_key      text not null check (char_length(source_key) between 1 and 200),
  kind            text not null check (kind in (
                    'category_summary', 'page_note', 'scope', 'spec', 'schedule', 'detail', 'question',
                    'brief_summary', 'system', 'assumption', 'exclusion', 'risk', 'rfi', 'missing_document',
                    'other_trade', 'sheet_summary', 'spec_highlight', 'material_note', 'confidence_note')),
  document_id     uuid,
  page            int check (page is null or page >= 1),
  sheet_number    text check (sheet_number is null or char_length(sheet_number) <= 60),
  sheet_title     text check (sheet_title is null or char_length(sheet_title) <= 300),
  title           text check (title is null or char_length(title) <= 300),
  body            text not null check (char_length(body) between 1 and 4000),
  quote           text check (quote is null or char_length(quote) <= 1000),
  section         text check (section is null or char_length(section) <= 120),
  severity        text check (severity is null or char_length(severity) <= 40),
  created_at      timestamptz not null default now(),
  tsv             tsvector generated always as (
                    to_tsvector('english'::regconfig,
                      coalesce(title, '') || ' ' || coalesce(sheet_number, '') || ' ' || coalesce(sheet_title, '')
                      || ' ' || body)
                  ) stored
);
do $$
begin
  if not exists (select 1 from pg_constraint where conname = 'plans_reader_notes_key_uq') then
    alter table public.plans_reader_notes add constraint plans_reader_notes_key_uq
      unique (run_id, source_scope, category, source_version, source_key);
  end if;
end $$;
create index if not exists plans_reader_notes_run_idx
  on public.plans_reader_notes (run_id, source_scope, category, source_version);
create index if not exists plans_reader_notes_tsv_idx on public.plans_reader_notes using gin (tsv);
create index if not exists plans_reader_notes_body_trgm_idx
  on public.plans_reader_notes using gin (body gin_trgm_ops);
create index if not exists plans_reader_notes_sheet_trgm_idx
  on public.plans_reader_notes using gin (sheet_number gin_trgm_ops) where sheet_number is not null;
alter table public.plans_reader_notes enable row level security;
alter table public.plans_reader_notes force row level security;
revoke all on table public.plans_reader_notes from anon, authenticated;

-- A2. Hybrid search: websearch tsquery rank plus trigram word similarity
--     (English stemming mangles tags like 1HD-21 and L1/1HD-21).
create or replace function public.search_plans_notes(
  p_q text, p_run_ids uuid[], p_kinds text[] default null, p_category text default null, p_limit int default 30
)
returns table (id uuid, run_id uuid, source_scope text, category text, source_version int, kind text,
               document_id uuid, page int, sheet_number text, sheet_title text, title text, body text,
               quote text, section text, severity text, rank real)
language sql
stable
security invoker
set search_path = pg_catalog, public, extensions
as $$
  with q as (
    select websearch_to_tsquery('english'::regconfig, coalesce(p_q, '')) as ts, lower(coalesce(p_q, '')) as raw
  )
  select n.id, n.run_id, n.source_scope, n.category, n.source_version, n.kind, n.document_id, n.page,
         n.sheet_number, n.sheet_title, n.title, n.body, n.quote, n.section, n.severity,
         (ts_rank(n.tsv, q.ts) + greatest(word_similarity(q.raw, lower(n.body)),
                                          word_similarity(q.raw, lower(coalesce(n.sheet_number, '')))))::real as rank
    from public.plans_reader_notes n, q
   where n.run_id = any(p_run_ids)
     and (p_kinds is null or n.kind = any(p_kinds))
     and (p_category is null or n.category = p_category)
     and (n.tsv @@ q.ts or q.raw <% lower(n.body) or q.raw <% lower(coalesce(n.sheet_number, '')))
   order by rank desc, n.run_id, n.page nulls last
   limit greatest(1, least(coalesce(p_limit, 30), 50));
$$;

-- A3. Evidence access log (Permissions item 5; Retention: 7 years, append-only;
--     the ledger triggers are bound in part B4). Person ids are plain uuids
--     plus labels: the ledger outlives the person. One approval reads one
--     subject (estimator_timeline requires an assignment_id, 3.8.6), so
--     approval_id stays unique.
create table if not exists public.assistant_evidence_access (
  id                uuid primary key default gen_random_uuid(),
  reader_user_id    uuid not null,
  reader_label      text not null,
  reader_role       text not null,
  subject_kind      text not null check (subject_kind in ('user', 'estimator')),
  subject_user_id   uuid,
  subject_label     text,
  project_id        uuid,
  tool              text not null check (tool in ('person_evidence', 'estimator_timeline')),
  view              text not null check (view in ('activity', 'file_access', 'notifications', 'ai_usage', 'estimator_timeline')),
  filters           jsonb not null default '{}'::jsonb check (jsonb_typeof(filters) = 'object'),
  row_count         int not null default 0 check (row_count >= 0),
  truncated         boolean not null default false,
  xlsx_report_id    uuid,
  reason            text not null check (char_length(btrim(reason)) between 10 and 500),
  approval_id       uuid not null,
  conversation_id   uuid,
  run_id            uuid,
  created_at        timestamptz not null default now()
);
create unique index if not exists assistant_evidence_access_approval_uq
  on public.assistant_evidence_access (approval_id);
create index if not exists assistant_evidence_access_reader_idx
  on public.assistant_evidence_access (reader_user_id, created_at desc);
create index if not exists assistant_evidence_access_subject_idx
  on public.assistant_evidence_access (subject_user_id, created_at desc) where subject_user_id is not null;
alter table public.assistant_evidence_access enable row level security;
alter table public.assistant_evidence_access force row level security;
revoke all on table public.assistant_evidence_access from anon, authenticated;

-- A4. Report files (D5). Private to the person; the object lives at
--     assistant-reports/{user_id}/{id}.xlsx in the project-files bucket.
create table if not exists public.assistant_reports (
  id              uuid primary key default gen_random_uuid(),
  user_id         uuid not null references public.profiles(id) on delete cascade,
  spec            text not null check (spec in ('bid_invitations', 'quote_readiness', 'estimator_vs_bid',
                    'project_timeline', 'project_file_access', 'change_history', 'ai_usage', 'person_evidence')),
  params          jsonb not null default '{}'::jsonb check (jsonb_typeof(params) = 'object'),
  title           text not null check (char_length(title) between 1 and 200),
  filename        text not null check (filename ~ '^[A-Za-z0-9 ._()-]{1,120}\.xlsx$'),
  storage_path    text not null,
  row_count       int not null default 0 check (row_count >= 0),
  truncated       boolean not null default false,
  bytes           int not null default 0 check (bytes >= 0),
  conversation_id uuid,
  run_id          uuid,
  approval_id     uuid,
  status          text not null default 'ready' check (status in ('ready', 'purged')),
  expires_at      timestamptz not null,
  purged_at       timestamptz,
  created_at      timestamptz not null default now(),
  constraint assistant_reports_purged_chk check ((status = 'purged') = (purged_at is not null)),
  -- The download route signs whatever the row holds, so the path is pinned
  -- to the owner's prefix and the row's own id.
  constraint assistant_reports_path_chk
    check (storage_path = 'assistant-reports/' || user_id::text || '/' || id::text || '.xlsx')
);
create index if not exists assistant_reports_user_idx on public.assistant_reports (user_id, created_at desc);
create index if not exists assistant_reports_expiry_idx on public.assistant_reports (expires_at) where status = 'ready';
create unique index if not exists assistant_reports_approval_uq
  on public.assistant_reports (approval_id) where approval_id is not null;
alter table public.assistant_reports enable row level security;
alter table public.assistant_reports force row level security;
revoke all on table public.assistant_reports from anon, authenticated;

-- A5. The human-typed reason of a sensitive_read card (D3). Mutable column,
--     not in the immutable set of assistant_approval_immutable (0172), set
--     once by the decide RPC below and never changed after.
alter table public.assistant_approvals add column if not exists decision_reason text;
do $$
begin
  if not exists (select 1 from pg_constraint where conname = 'assistant_approvals_decision_reason_chk') then
    alter table public.assistant_approvals add constraint assistant_approvals_decision_reason_chk
      check (decision_reason is null or char_length(btrim(decision_reason)) between 10 and 500) not valid;
    alter table public.assistant_approvals validate constraint assistant_approvals_decision_reason_chk;
  end if;
end $$;

create or replace function public.assistant_approval_reason_once()
returns trigger
language plpgsql
security invoker
set search_path = pg_catalog, public
as $$
begin
  if old.decision_reason is not null and new.decision_reason is distinct from old.decision_reason then
    raise exception 'assistant_approvals.decision_reason is set once' using errcode = 'restrict_violation';
  end if;
  return new;
end;
$$;
create or replace trigger assistant_approvals_reason_once before update on public.assistant_approvals
  for each row execute function public.assistant_approval_reason_once();

-- The approve CAS of 0172 (assistant_approval_decide, 0172:214-268) plus the
-- reason, in one statement. Approve only; reject keeps the 0172 RPC.
create or replace function public.assistant_approval_decide_with_reason(
  p_user_id uuid, p_approval_id uuid, p_payload_sha256 text, p_precondition jsonb,
  p_session_aal text, p_reason text
)
returns jsonb
language plpgsql
volatile
security invoker
set search_path = pg_catalog, public
as $$
declare
  v_row public.assistant_approvals;
  v_reason text := btrim(coalesce(p_reason, ''));
begin
  if char_length(v_reason) < 10 or char_length(v_reason) > 500 then
    return jsonb_build_object('ok', false, 'reason', 'reason_invalid', 'row', null);
  end if;
  update public.assistant_approvals a
     set status = 'approved', decided_by = p_user_id, decided_at = now(),
         decided_session_aal = p_session_aal, decision_reason = v_reason
   where a.id = p_approval_id and a.user_id = p_user_id and a.status = 'pending'
     and a.expires_at > now() and a.payload_sha256 = p_payload_sha256 and a.precondition = p_precondition
  returning * into v_row;
  if found then
    return jsonb_build_object('ok', true, 'reason', null, 'row', to_jsonb(v_row));
  end if;
  -- Not approved: answer exactly as the 0172 RPC would (expired, superseded,
  -- not_pending, stale, not_found) by delegating with p_decision 'approve'
  -- against a hash that cannot match (the row is never changed by that call
  -- except the timed_out expiry, which is the same rule).
  return public.assistant_approval_decide(p_user_id, p_approval_id, 'approve', repeat('0', 64),
                                          p_precondition, p_session_aal);
end;
$$;

-- A6. memory_search (3.15): the person's own active and pending notes.
create or replace function public.assistant_memory_search(
  p_user_id uuid, p_q text, p_statuses text[] default array['active', 'pending_review'],
  p_kind text default null, p_limit int default 10
)
returns table (id uuid, kind text, status text, title text, body text, project_id uuid, pinned boolean,
               origin_tainted boolean, updated_at timestamptz, rank real)
language sql
stable
security invoker
set search_path = pg_catalog, public, extensions
as $$
  with q as (select websearch_to_tsquery('english'::regconfig, coalesce(p_q, '')) as ts, lower(coalesce(p_q, '')) as raw)
  select m.id, m.kind, m.status, m.title, m.body, m.project_id, m.pinned, m.origin_tainted, m.updated_at,
         (ts_rank(m.tsv, q.ts) + similarity(lower(m.title), q.raw))::real
    from public.assistant_memories m, q
   where m.user_id = p_user_id
     and m.status = any(coalesce(p_statuses, array['active', 'pending_review']))
     and m.status <> 'archived'
     and (p_kind is null or m.kind = p_kind)
     and (m.tsv @@ q.ts or similarity(lower(m.title), q.raw) > 0.3 or q.raw <% lower(m.body))
   order by 10 desc, m.updated_at desc
   limit greatest(1, least(coalesce(p_limit, 10), 25));
$$;

-- A7. Evidence indexes (evidence reader section 4; 0172 already added the
--     stage_events, project_open_events, email_log and proposal_send_events ones).
create index if not exists audit_log_entity_idx on public.audit_log (entity, entity_id, created_at desc);
create index if not exists audit_log_created_idx on public.audit_log (created_at desc);
create index if not exists notifications_user_created_idx on public.notifications (user_id, created_at desc);
create index if not exists notifications_project_created_idx
  on public.notifications (project_id, created_at desc) where project_id is not null;
create index if not exists rfp_emails_review_by_idx on public.rfp_emails (review_by) where review_by is not null;
create index if not exists estimator_notes_author_created_idx on public.estimator_notes (author_id, created_at desc);
create index if not exists pm_stage_events_actor_idx on public.pm_stage_events (actor_id, entered_at desc);

-- A8. Spend: two hold kinds for AE work (D10). The table check widens in
--     part B2; the function guard and the state term are replaced here.
--     Closed holds of these kinds (settled, or released after a cancel,
--     failure or timeout: repo.close_reservation writes actual_usd and
--     settled_at on both) count their actual_usd (the AE ledger's cost)
--     toward today's spent, because their calls are not attributed in
--     llm_call_log (user_id and assistant_run_id are null on AE jobs).
--     Both bodies are the 0174 definitions (0174:85-160) with only these
--     changes: two added terms in the state, two added kinds in the guard.
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
   where r.user_id = p_user_id and r.kind in ('plans_pages', 'ai_estimate')
     and r.status in ('settled', 'released') and r.actual_usd is not null
     and r.settled_at >= v_since), 0);
  v_global_spent := v_global_spent + coalesce((select sum(r.actual_usd)
    from public.assistant_spend_reservations r
   where r.kind in ('plans_pages', 'ai_estimate')
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
  if p_kind not in ('turn_call', 'split_task', 'plans_pages', 'ai_estimate') or p_amount is null or p_amount < 0
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

create unique index if not exists assistant_spend_reservations_task_ae_uq
  on public.assistant_spend_reservations (task_id)
  where task_id is not null and kind in ('plans_pages', 'ai_estimate');
-- One AE hold per approval: the routes look the hold up by approval before
-- reserving, so a crash between reserve and task insert cannot strand a
-- second hold on retry (3.5.3, 3.6.3).
create unique index if not exists assistant_spend_reservations_approval_ae_uq
  on public.assistant_spend_reservations (approval_id)
  where approval_id is not null and kind in ('plans_pages', 'ai_estimate');

-- A9. Open AE tasks for the sweep (3.6.5).
create index if not exists assistant_tasks_open_kind_idx
  on public.assistant_tasks (kind, updated_at) where status in ('pending', 'running', 'waiting_external');

-- A10. Execute for service_role only (0145 pattern), for every function above.
revoke all on function public.search_plans_notes(text, uuid[], text[], text, int) from public;
revoke all on function public.search_plans_notes(text, uuid[], text[], text, int) from anon, authenticated;
grant execute on function public.search_plans_notes(text, uuid[], text[], text, int) to service_role;
revoke all on function public.assistant_approval_reason_once() from public;
revoke all on function public.assistant_approval_reason_once() from anon, authenticated;
grant execute on function public.assistant_approval_reason_once() to service_role;
revoke all on function public.assistant_approval_decide_with_reason(uuid, uuid, text, jsonb, text, text) from public;
revoke all on function public.assistant_approval_decide_with_reason(uuid, uuid, text, jsonb, text, text) from anon, authenticated;
grant execute on function public.assistant_approval_decide_with_reason(uuid, uuid, text, jsonb, text, text) to service_role;
revoke all on function public.assistant_memory_search(uuid, text, text[], text, int) from public;
revoke all on function public.assistant_memory_search(uuid, text, text[], text, int) from anon, authenticated;
grant execute on function public.assistant_memory_search(uuid, text, text[], text, int) to service_role;
revoke all on function public.assistant_spend_state(uuid, numeric, numeric) from public;
revoke all on function public.assistant_spend_state(uuid, numeric, numeric) from anon, authenticated;
grant execute on function public.assistant_spend_state(uuid, numeric, numeric) to service_role;
revoke all on function public.assistant_spend_reserve(uuid, text, numeric, int, numeric, numeric, uuid, uuid, uuid) from public;
revoke all on function public.assistant_spend_reserve(uuid, text, numeric, int, numeric, numeric, uuid, uuid, uuid) from anon, authenticated;
grant execute on function public.assistant_spend_reserve(uuid, text, numeric, int, numeric, numeric, uuid, uuid, uuid) to service_role;

-- A11. Server-built messages that carry untrusted text (evidence rows, page
--      read answers) are marked so _history replays them as a user-role
--      data block, never as the model's own words (3.2.4, 3.16.3).
alter table public.assistant_messages add column if not exists server_data boolean not null default false;

notify pgrst, 'reload schema';

-- ==== PART B (owner at the screen) ====

-- B1. Recipes (3.10): re-created from the live definition (the 0176:335-354 pattern).
do $$
declare v_name text; v_def text;
begin
  select c.conname, pg_get_constraintdef(c.oid) into v_name, v_def from pg_constraint c
   where c.conrelid = 'public.assistant_schedules'::regclass and c.contype = 'c'
     and pg_get_constraintdef(c.oid) like '%labor_not_entered%';
  if v_name is null then raise exception '0180: the assistant_schedules recipe check is missing'; end if;
  if position('''estimator_vs_bid''' in v_def) > 0 and position('''quote_readiness''' in v_def) > 0 then return; end if;
  execute format('alter table public.assistant_schedules drop constraint %I', v_name);
  alter table public.assistant_schedules add constraint assistant_schedules_recipe_chk
    check (recipe in ('due_today', 'due_this_week', 'missing_quotes', 'labor_not_entered',
                      'estimator_vs_bid', 'quote_readiness')) not valid;
  alter table public.assistant_schedules validate constraint assistant_schedules_recipe_chk;
end $$;

-- B2. Spend kinds (3.7): the 0174 kind check widened to the two AE hold kinds.
do $$
declare v_name text; v_def text;
begin
  select c.conname, pg_get_constraintdef(c.oid) into v_name, v_def from pg_constraint c
   where c.conrelid = 'public.assistant_spend_reservations'::regclass and c.contype = 'c'
     and pg_get_constraintdef(c.oid) like '%split_task%';
  if v_name is null then raise exception '0180: the assistant_spend_reservations kind check is missing'; end if;
  if position('''plans_pages''' in v_def) > 0 and position('''ai_estimate''' in v_def) > 0 then return; end if;
  execute format('alter table public.assistant_spend_reservations drop constraint %I', v_name);
  alter table public.assistant_spend_reservations add constraint assistant_spend_reservations_kind_chk
    check (kind in ('turn_call', 'split_task', 'plans_pages', 'ai_estimate')) not valid;
  alter table public.assistant_spend_reservations validate constraint assistant_spend_reservations_kind_chk;
end $$;

-- B3. Task kinds (3.5.3, 3.6.3): page reads and assistant-started AE runs.
do $$
declare v_name text; v_def text;
begin
  select c.conname, pg_get_constraintdef(c.oid) into v_name, v_def from pg_constraint c
   where c.conrelid = 'public.assistant_tasks'::regclass and c.contype = 'c'
     and pg_get_constraintdef(c.oid) like '%split_into_project%';
  if v_name is null then raise exception '0180: the assistant_tasks kind check is missing'; end if;
  if position('''plans_read_pages''' in v_def) > 0 and position('''ai_estimating_run''' in v_def) > 0 then return; end if;
  execute format('alter table public.assistant_tasks drop constraint %I', v_name);
  alter table public.assistant_tasks add constraint assistant_tasks_kind_chk
    check (kind in ('split_into_project', 'instruction', 'plans_read_pages', 'ai_estimating_run')) not valid;
  alter table public.assistant_tasks validate constraint assistant_tasks_kind_chk;
end $$;

-- B4. The evidence access log is append-only, bound exactly as
--     0170:615-620 binds assistant_conversation_views (no FK, nothing
--     cascades, so assistant_refuse_change() takes no allow_cascade).
create or replace trigger assistant_evidence_access_no_update before update on public.assistant_evidence_access
  for each row execute function public.assistant_refuse_change();
create or replace trigger assistant_evidence_access_no_delete before delete on public.assistant_evidence_access
  for each row execute function public.assistant_refuse_change();
create or replace trigger assistant_evidence_access_no_truncate before truncate on public.assistant_evidence_access
  for each statement execute function public.assistant_refuse_change();

-- B5. Sweep steps: report_purge claims once per Pacific day through
--     _claim_today. The 10-minute riders (plans_notes_sweep, ae_task_sweep)
--     are per-process interval riders and claim nothing (3.3, 3.5.4).
do $$
declare v_name text; v_def text;
begin
  select c.conname, pg_get_constraintdef(c.oid) into v_name, v_def from pg_constraint c
   where c.conrelid = 'public.assistant_sweeps'::regclass and c.contype = 'c'
     and pg_get_constraintdef(c.oid) like '%orphans%';
  if v_name is null then raise exception '0180: the assistant_sweeps step check is missing'; end if;
  if position('''report_purge''' in v_def) > 0 then return; end if;
  execute format('alter table public.assistant_sweeps drop constraint %I', v_name);
  alter table public.assistant_sweeps add constraint assistant_sweeps_step_chk
    check (step in ('nightly', 'orphans', 'report_purge')) not valid;
  alter table public.assistant_sweeps validate constraint assistant_sweeps_step_chk;
end $$;

notify pgrst, 'reload schema';
