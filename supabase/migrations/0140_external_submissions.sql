-- 0140: External submissions ("Mark submitted" from the project side menu).
--
-- G3 sometimes sends a proposal outside the app while the project already
-- exists here. This records the numbers that went out and moves the project
-- to Submitted from any stage past Go/No-Go, without walking the pipeline.
-- See docs/EXTERNAL_SUBMISSION.md.
--
--   external_submission_requests  permission requests from non-approver
--                                 writers; a grant is one user, one project,
--                                 48 hours, single use
--   external_submissions          one row per submission (kept after undo)
--   external_submission_lines     one row per material category (data studies)
--   external_submission_gcs       one row per project GC: included or no bid
--
-- The numbers are ALSO written to the canonical tables the normal pipeline
-- uses (labor_reviews, markups, a committed verifications snapshot,
-- project_gcs overrides, proposal_sends rows at status 'sent' with
-- sent_via 'external'), so Win/Loss, analytics and the reports read them
-- unchanged. proposal_sends gains `origin` + `external_submission_id` so these
-- rows can be told apart from Send Out rows. Both writes and the undo run as
-- one transaction each (the two functions at the end).

-- ── 1. requests ─────────────────────────────────────────────────────────────

create table if not exists external_submission_requests (
  id              uuid primary key default gen_random_uuid(),
  project_id      uuid not null references projects(id) on delete cascade,
  requested_by    uuid not null references profiles(id) on delete cascade,
  message         text
                  constraint external_submission_requests_message_len
                  check (message is null or char_length(message) <= 2000),
  -- 'expired' is written lazily once an approved grant passes expires_at
  -- unused (the backend also treats a past expires_at as expired on read).
  status          text not null default 'pending'
                  constraint external_submission_requests_status_check
                  check (status in ('pending', 'approved', 'denied', 'cancelled',
                                    'revoked', 'used', 'expired')),
  decided_by      uuid references profiles(id) on delete set null,
  decided_at      timestamptz,
  deny_reason     text
                  constraint external_submission_requests_reason_len
                  check (deny_reason is null or char_length(deny_reason) <= 2000),
  expires_at      timestamptz,
  revoked_by      uuid references profiles(id) on delete set null,
  revoked_at      timestamptz,
  used_at         timestamptz,
  submission_id   uuid,
  created_at      timestamptz not null default now(),
  updated_at      timestamptz not null default now()
);

-- One open request (pending, or an approved grant not yet used) per user per
-- project. Expiry cannot live in a partial index predicate (now() is not
-- immutable), so the backend flips stale approved rows to 'expired' before it
-- inserts a new request.
create unique index if not exists external_submission_requests_one_open
  on external_submission_requests (project_id, requested_by)
  where status in ('pending', 'approved');
create index if not exists external_submission_requests_project_idx
  on external_submission_requests (project_id, created_at desc);
create index if not exists external_submission_requests_pending_idx
  on external_submission_requests (project_id) where status = 'pending';

-- ── 2. submissions ──────────────────────────────────────────────────────────

create table if not exists external_submissions (
  id                          uuid primary key default gen_random_uuid(),
  project_id                  uuid not null references projects(id) on delete cascade,
  submitted_by                uuid references profiles(id) on delete set null,
  -- Null when an approver (Executive, Estimating Admin, IT Admin) submitted.
  approval_request_id         uuid references external_submission_requests(id) on delete set null,
  status                      text not null default 'active'
                              constraint external_submissions_status_check
                              check (status in ('active', 'undone')),
  -- Section costs: the sum of the category lines per pricing section.
  -- A breakout section with no lines is NULL (not on the bid), like the
  -- verifications snapshot; the residual materials section is always set.
  materials_amount            numeric not null default 0 check (materials_amount >= 0),
  gear_amount                 numeric check (gear_amount >= 0),
  underground_amount          numeric check (underground_amount >= 0),
  low_voltage_amount          numeric check (low_voltage_amount >= 0),
  labor_amount                numeric not null default 0 check (labor_amount >= 0),
  labor_note                  text,
  materials_markup_pct        numeric,
  materials_markup_amount     numeric,
  gear_markup_pct             numeric,
  gear_markup_amount          numeric,
  underground_markup_pct      numeric,
  underground_markup_amount   numeric,
  low_voltage_markup_pct      numeric,
  low_voltage_markup_amount   numeric,
  labor_markup_pct            numeric,
  labor_markup_amount         numeric,
  total_cost                  numeric not null default 0,
  total_price                 numeric not null default 0,
  -- What the submission overwrote, captured inside the apply transaction:
  -- lanes, headline, labor_reviews / markups / verifications rows, the
  -- touched project_gcs overrides and proposal_sends rows. Undo restores it.
  prior_state                 jsonb,
  stage_event_ids             uuid[] not null default '{}',
  submitted_at                timestamptz not null default now(),
  undone_at                   timestamptz,
  undone_by                   uuid references profiles(id) on delete set null,
  undo_reason                 text
                              constraint external_submissions_undo_reason_len
                              check (undo_reason is null or char_length(undo_reason) <= 2000),
  created_at                  timestamptz not null default now()
);

-- At most one live submission per project.
create unique index if not exists external_submissions_one_active
  on external_submissions (project_id) where status = 'active';
create index if not exists external_submissions_project_idx
  on external_submissions (project_id, created_at desc);

do $$
begin
  if not exists (
    select 1 from pg_constraint where conname = 'external_submission_requests_submission_fkey'
  ) then
    alter table external_submission_requests
      add constraint external_submission_requests_submission_fkey
      foreign key (submission_id) references external_submissions(id) on delete set null;
  end if;
end $$;

-- ── 3. category lines (one real row per category, for data studies) ─────────

create table if not exists external_submission_lines (
  id                    uuid primary key default gen_random_uuid(),
  submission_id         uuid not null references external_submissions(id) on delete cascade,
  project_id            uuid not null references projects(id) on delete cascade,
  material_category_id  uuid not null references material_categories(id) on delete restrict,
  -- Snapshots at submit time (category names and sections can change later).
  category_name         text not null,
  pricing_section       text not null
                        constraint external_submission_lines_section_check
                        check (pricing_section in ('materials', 'gear', 'underground', 'low_voltage')),
  amount                numeric not null check (amount >= 0),
  note                  text
                        constraint external_submission_lines_note_len
                        check (note is null or char_length(note) <= 2000),
  sort_order            integer not null default 0,
  created_at            timestamptz not null default now(),
  unique (submission_id, material_category_id)
);
create index if not exists external_submission_lines_category_idx
  on external_submission_lines (material_category_id);
create index if not exists external_submission_lines_project_idx
  on external_submission_lines (project_id);

-- ── 4. per-GC outcome of the submission ─────────────────────────────────────

create table if not exists external_submission_gcs (
  id                    uuid primary key default gen_random_uuid(),
  submission_id         uuid not null references external_submissions(id) on delete cascade,
  project_id            uuid not null references projects(id) on delete cascade,
  gc_id                 uuid not null references general_contractors(id) on delete restrict,
  gc_name               text not null,
  -- true = submitted to this GC, false = no bid.
  included              boolean not null,
  -- true = the GC already had a proposal sent through Send Out before this
  -- submission; its proposal_sends row was left exactly as it was.
  already_sent          boolean not null default false,
  material_amount       numeric,
  gear_amount           numeric,
  underground_amount    numeric,
  low_voltage_amount    numeric,
  labor_amount          numeric,
  total_amount          numeric,
  -- The project default (cost plus markup) this GC's price differs from.
  default_total         numeric,
  proposal_send_id      uuid references proposal_sends(id) on delete set null,
  event_id              uuid references proposal_send_events(id) on delete set null,
  created_at            timestamptz not null default now(),
  unique (submission_id, gc_id)
);
create index if not exists external_submission_gcs_gc_idx on external_submission_gcs (gc_id);

-- ── 5. proposal_sends: where a sent row came from ───────────────────────────

alter table proposal_sends
  add column if not exists origin text not null default 'send_out',
  add column if not exists external_submission_id uuid;

do $$
begin
  if not exists (select 1 from pg_constraint where conname = 'proposal_sends_origin_check') then
    alter table proposal_sends
      add constraint proposal_sends_origin_check
      check (origin in ('send_out', 'external_submission'));
  end if;
  if not exists (
    select 1 from pg_constraint where conname = 'proposal_sends_external_submission_fkey'
  ) then
    alter table proposal_sends
      add constraint proposal_sends_external_submission_fkey
      foreign key (external_submission_id) references external_submissions(id) on delete set null;
  end if;
end $$;

comment on column proposal_sends.origin is
  'send_out = generated/sent (or marked) on the Send Out step; external_submission = recorded by Mark submitted from the side menu (no document, no email).';

-- RLS deny-by-default (the service-role backend bypasses it); see 0007 / 0055.
alter table external_submission_requests enable row level security;
alter table external_submission_requests force row level security;
alter table external_submissions enable row level security;
alter table external_submissions force row level security;
alter table external_submission_lines enable row level security;
alter table external_submission_lines force row level security;
alter table external_submission_gcs enable row level security;
alter table external_submission_gcs force row level security;

-- ── 6. apply_external_submission ────────────────────────────────────────────
-- Everything a submission writes, in one transaction. The backend validates
-- and computes the figures (services/external_submission.py) and posts them
-- here as one jsonb document; this function re-checks the state under a row
-- lock on the project, consumes the grant, snapshots what it will overwrite,
-- and writes. Errors are raised as 'external_submission:<code>' so the backend
-- can map them to a readable 409.

create or replace function apply_external_submission(p jsonb)
returns uuid
language plpgsql
volatile
security invoker
set search_path = pg_catalog, public
as $$
declare
  v_project   uuid := (p->>'project_id')::uuid;
  v_user      uuid := (p->>'user_id')::uuid;
  v_request   uuid := nullif(p->>'request_id', '')::uuid;
  v_sub       uuid;
  v_head      text;
  v_prior     jsonb;
  v_gc        jsonb;
  v_e         jsonb;
  v_ps_id     uuid;
  v_ev_id     uuid;
  v_id        uuid;
  v_stage_ids uuid[] := '{}';
  v_gc_ids    uuid[];
  v_stage     text;
  v_abandoned timestamptz;
  v_return    text;
  v_intake_task   text;
  v_intake_status text;
  s           jsonb := p->'submission';
  m           jsonb := p->'markup';
  vf          jsonb := p->'verification';
begin
  select current_stage::text, abandoned_at, reverify_return_stage::text
    into v_stage, v_abandoned, v_return
    from projects where id = v_project for update;
  if not found then
    raise exception 'external_submission:project_not_found';
  end if;

  -- The eligibility the backend checked, re-checked under the lock: a
  -- Done sending, decline, abandon or Go/No-Go reopen that committed after
  -- the backend's read must not be overwritten.
  if v_stage in ('pm_only', 'cp_only') then
    raise exception 'external_submission:never_bid';
  end if;
  if v_stage = 'declined' then
    raise exception 'external_submission:declined';
  end if;
  if v_abandoned is not null then
    raise exception 'external_submission:abandoned';
  end if;

  -- The send_out lane row is locked too, so a lane write racing this one
  -- waits for the commit instead of interleaving with it.
  select current_task::text into v_head
    from project_category_state
   where project_id = v_project and category = 'send_out'
     for update;
  if v_head in ('submitted', 'bid_outcome')
     or (v_head = 'verify' and v_return in ('submitted', 'bid_outcome')) then
    raise exception 'external_submission:already_submitted';
  end if;
  select current_task::text, status::text into v_intake_task, v_intake_status
    from project_category_state
   where project_id = v_project and category = 'intake';
  if v_intake_status is null or v_intake_status = 'locked'
     or (v_intake_status <> 'complete' and v_intake_task is distinct from 'to_estimator') then
    raise exception 'external_submission:before_go_no_go';
  end if;
  if v_head is distinct from nullif(p->>'expected_send_out_head', '') then
    raise exception 'external_submission:stale';
  end if;
  if exists (select 1 from bid_outcomes where project_id = v_project) then
    raise exception 'external_submission:outcome_recorded';
  end if;
  if exists (
    select 1 from external_submissions where project_id = v_project and status = 'active'
  ) then
    raise exception 'external_submission:already_submitted';
  end if;
  if exists (
    select 1 from proposal_sends where project_id = v_project and status = 'sending'
  ) then
    raise exception 'external_submission:sending_in_progress';
  end if;

  if v_request is not null then
    update external_submission_requests
       set status = 'used', used_at = now(), updated_at = now()
     where id = v_request
       and project_id = v_project
       and requested_by = v_user
       and status = 'approved'
       and expires_at > now();
    if not found then
      raise exception 'external_submission:grant_invalid';
    end if;
  end if;

  v_gc_ids := array(
    select (g->>'gc_id')::uuid
      from jsonb_array_elements(coalesce(p->'gcs', '[]'::jsonb)) g
     where coalesce((g->>'write')::boolean, false)
  );

  v_prior := jsonb_build_object(
    'project', (
      select jsonb_build_object(
        'current_stage', current_stage,
        'current_owner_role', current_owner_role,
        'reverify_return_stage', reverify_return_stage
      ) from projects where id = v_project
    ),
    'category_state', coalesce(
      (select jsonb_agg(to_jsonb(cs)) from project_category_state cs where cs.project_id = v_project),
      '[]'::jsonb
    ),
    'labor_review', (select to_jsonb(r) from labor_reviews r where r.project_id = v_project),
    'markup', (select to_jsonb(mk) from markups mk where mk.project_id = v_project),
    'verification', (select to_jsonb(v) from verifications v where v.project_id = v_project),
    'project_gcs', coalesce(
      (select jsonb_agg(jsonb_build_object(
                'gc_id', pg.gc_id,
                'proposal_material_amount', pg.proposal_material_amount,
                'proposal_gear_amount', pg.proposal_gear_amount,
                'proposal_underground_amount', pg.proposal_underground_amount,
                'proposal_low_voltage_amount', pg.proposal_low_voltage_amount,
                'proposal_labor_amount', pg.proposal_labor_amount))
         from project_gcs pg
        where pg.project_id = v_project and pg.gc_id = any(v_gc_ids)),
      '[]'::jsonb
    ),
    'proposal_sends', coalesce(
      (select jsonb_agg(to_jsonb(ps)) from proposal_sends ps
        where ps.project_id = v_project and ps.gc_id = any(v_gc_ids)),
      '[]'::jsonb
    )
  );

  insert into external_submissions (
    project_id, submitted_by, approval_request_id,
    materials_amount, gear_amount, underground_amount, low_voltage_amount,
    labor_amount, labor_note,
    materials_markup_pct, materials_markup_amount,
    gear_markup_pct, gear_markup_amount,
    underground_markup_pct, underground_markup_amount,
    low_voltage_markup_pct, low_voltage_markup_amount,
    labor_markup_pct, labor_markup_amount,
    total_cost, total_price, prior_state
  ) values (
    v_project, v_user, v_request,
    (s->>'materials_amount')::numeric, (s->>'gear_amount')::numeric,
    (s->>'underground_amount')::numeric, (s->>'low_voltage_amount')::numeric,
    (s->>'labor_amount')::numeric, nullif(s->>'labor_note', ''),
    (s->>'materials_markup_pct')::numeric, (s->>'materials_markup_amount')::numeric,
    (s->>'gear_markup_pct')::numeric, (s->>'gear_markup_amount')::numeric,
    (s->>'underground_markup_pct')::numeric, (s->>'underground_markup_amount')::numeric,
    (s->>'low_voltage_markup_pct')::numeric, (s->>'low_voltage_markup_amount')::numeric,
    (s->>'labor_markup_pct')::numeric, (s->>'labor_markup_amount')::numeric,
    (s->>'total_cost')::numeric, (s->>'total_price')::numeric, v_prior
  ) returning id into v_sub;

  insert into external_submission_lines (
    submission_id, project_id, material_category_id, category_name,
    pricing_section, amount, note, sort_order
  )
  select v_sub, v_project, (l->>'material_category_id')::uuid, l->>'category_name',
         l->>'pricing_section', (l->>'amount')::numeric, nullif(l->>'note', ''),
         coalesce((l->>'sort_order')::int, 0)
    from jsonb_array_elements(coalesce(p->'lines', '[]'::jsonb)) l;

  -- Canonical pricing rows: the same places Labor Numbers, Markup and the
  -- Verify commit write.
  insert into labor_reviews (project_id, labor_amount, reviewed_by, updated_at)
  values (v_project, (p->'labor_review'->>'labor_amount')::numeric, v_user, now())
  on conflict (project_id) do update
     set labor_amount = excluded.labor_amount,
         reviewed_by = excluded.reviewed_by,
         updated_at = now();

  insert into markups (
    project_id, set_by, updated_at,
    materials_markup_pct, materials_markup_amount, gear_markup_pct, gear_markup_amount,
    underground_markup_pct, underground_markup_amount,
    low_voltage_markup_pct, low_voltage_markup_amount,
    labor_markup_pct, labor_markup_amount
  ) values (
    v_project, v_user, now(),
    (m->>'materials_markup_pct')::numeric, (m->>'materials_markup_amount')::numeric,
    (m->>'gear_markup_pct')::numeric, (m->>'gear_markup_amount')::numeric,
    (m->>'underground_markup_pct')::numeric, (m->>'underground_markup_amount')::numeric,
    (m->>'low_voltage_markup_pct')::numeric, (m->>'low_voltage_markup_amount')::numeric,
    (m->>'labor_markup_pct')::numeric, (m->>'labor_markup_amount')::numeric
  )
  on conflict (project_id) do update
     set set_by = excluded.set_by,
         updated_at = now(),
         materials_markup_pct = excluded.materials_markup_pct,
         materials_markup_amount = excluded.materials_markup_amount,
         gear_markup_pct = excluded.gear_markup_pct,
         gear_markup_amount = excluded.gear_markup_amount,
         underground_markup_pct = excluded.underground_markup_pct,
         underground_markup_amount = excluded.underground_markup_amount,
         low_voltage_markup_pct = excluded.low_voltage_markup_pct,
         low_voltage_markup_amount = excluded.low_voltage_markup_amount,
         labor_markup_pct = excluded.labor_markup_pct,
         labor_markup_amount = excluded.labor_markup_amount;

  insert into verifications (
    project_id, verified_by, committed_at, updated_at, notes,
    labor_amount, materials_amount, gear_amount, underground_amount, low_voltage_amount,
    labor_markup_amount, materials_markup_amount, gear_markup_amount,
    underground_markup_amount, low_voltage_markup_amount
  ) values (
    v_project, v_user, now(), now(), vf->>'notes',
    (vf->>'labor_amount')::numeric, (vf->>'materials_amount')::numeric,
    (vf->>'gear_amount')::numeric, (vf->>'underground_amount')::numeric,
    (vf->>'low_voltage_amount')::numeric,
    (vf->>'labor_markup_amount')::numeric, (vf->>'materials_markup_amount')::numeric,
    (vf->>'gear_markup_amount')::numeric, (vf->>'underground_markup_amount')::numeric,
    (vf->>'low_voltage_markup_amount')::numeric
  )
  on conflict (project_id) do update
     set verified_by = excluded.verified_by,
         committed_at = now(),
         updated_at = now(),
         notes = excluded.notes,
         labor_amount = excluded.labor_amount,
         materials_amount = excluded.materials_amount,
         gear_amount = excluded.gear_amount,
         underground_amount = excluded.underground_amount,
         low_voltage_amount = excluded.low_voltage_amount,
         labor_markup_amount = excluded.labor_markup_amount,
         materials_markup_amount = excluded.materials_markup_amount,
         gear_markup_amount = excluded.gear_markup_amount,
         underground_markup_amount = excluded.underground_markup_amount,
         low_voltage_markup_amount = excluded.low_voltage_markup_amount;

  for v_gc in select * from jsonb_array_elements(coalesce(p->'gcs', '[]'::jsonb)) loop
    v_ps_id := null;
    v_ev_id := null;
    if coalesce((v_gc->>'write')::boolean, false) then
      update project_gcs
         set proposal_material_amount = (v_gc->>'override_material')::numeric,
             proposal_gear_amount = (v_gc->>'override_gear')::numeric,
             proposal_underground_amount = (v_gc->>'override_underground')::numeric,
             proposal_low_voltage_amount = (v_gc->>'override_low_voltage')::numeric,
             proposal_labor_amount = (v_gc->>'override_labor')::numeric
       where project_id = v_project and gc_id = (v_gc->>'gc_id')::uuid;
      if not found then
        raise exception 'external_submission:gc_not_on_project';
      end if;

      insert into proposal_sends (
        project_id, gc_id, gc_name, status, sent_via, origin, external_submission_id,
        sent_at, sent_by, gc_email, email_log_id, file_id, draft_id, lines_hash, error,
        material_amount, gear_amount, underground_amount, low_voltage_amount, labor_amount
      ) values (
        v_project, (v_gc->>'gc_id')::uuid, v_gc->>'gc_name', 'sent', 'external',
        'external_submission', v_sub, now(), v_user, null, null, null, null, null, null,
        (v_gc->>'material_amount')::numeric, (v_gc->>'gear_amount')::numeric,
        (v_gc->>'underground_amount')::numeric, (v_gc->>'low_voltage_amount')::numeric,
        (v_gc->>'labor_amount')::numeric
      )
      on conflict (project_id, gc_id) do update
         set gc_name = excluded.gc_name,
             status = 'sent',
             sent_via = 'external',
             origin = 'external_submission',
             external_submission_id = excluded.external_submission_id,
             sent_at = excluded.sent_at,
             sent_by = excluded.sent_by,
             gc_email = null,
             email_log_id = null,
             file_id = null,
             draft_id = null,
             lines_hash = null,
             error = null,
             material_amount = excluded.material_amount,
             gear_amount = excluded.gear_amount,
             underground_amount = excluded.underground_amount,
             low_voltage_amount = excluded.low_voltage_amount,
             labor_amount = excluded.labor_amount
       where proposal_sends.status not in ('sent', 'sending')
      returning id into v_ps_id;
      if v_ps_id is null then
        raise exception 'external_submission:gc_already_sent';
      end if;

      insert into proposal_send_events (
        proposal_send_id, project_id, gc_id, kind, via, status, sent_at, sent_by
      ) values (
        v_ps_id, v_project, (v_gc->>'gc_id')::uuid, 'initial', 'external', 'sent', now(), v_user
      ) returning id into v_ev_id;
    end if;

    insert into external_submission_gcs (
      submission_id, project_id, gc_id, gc_name, included, already_sent,
      material_amount, gear_amount, underground_amount, low_voltage_amount, labor_amount,
      total_amount, default_total, proposal_send_id, event_id
    ) values (
      v_sub, v_project, (v_gc->>'gc_id')::uuid, v_gc->>'gc_name',
      coalesce((v_gc->>'included')::boolean, false),
      coalesce((v_gc->>'already_sent')::boolean, false),
      (v_gc->>'material_amount')::numeric, (v_gc->>'gear_amount')::numeric,
      (v_gc->>'underground_amount')::numeric, (v_gc->>'low_voltage_amount')::numeric,
      (v_gc->>'labor_amount')::numeric, (v_gc->>'total_amount')::numeric,
      (v_gc->>'default_total')::numeric, v_ps_id, v_ev_id
    );
  end loop;

  -- Lanes: every lane but send_out complete, send_out parked on Submitted.
  for v_e in select * from jsonb_array_elements(coalesce(p->'category_state', '[]'::jsonb)) loop
    insert into project_category_state (
      project_id, category, current_task, status, owner_role, completed_at
    ) values (
      v_project,
      (v_e->>'category')::project_category,
      (v_e->>'current_task')::project_stage,
      (v_e->>'status')::category_status,
      nullif(v_e->>'owner_role', '')::public.role,
      case when v_e->>'status' = 'complete' then now() else null end
    )
    on conflict (project_id, category) do update
       set current_task = excluded.current_task,
           status = excluded.status,
           owner_role = excluded.owner_role,
           -- A lane that was already complete keeps its real completion time.
           completed_at = case
             when project_category_state.status = 'complete'
                  and excluded.status = 'complete'
               then project_category_state.completed_at
             else excluded.completed_at
           end;
  end loop;

  for v_e in select * from jsonb_array_elements(coalesce(p->'events', '[]'::jsonb)) loop
    insert into stage_events (project_id, from_stage, to_stage, category, actor_id, note)
    values (
      v_project,
      nullif(v_e->>'from_stage', '')::project_stage,
      (v_e->>'to_stage')::project_stage,
      (v_e->>'category')::project_category,
      v_user,
      v_e->>'note'
    ) returning id into v_id;
    v_stage_ids := v_stage_ids || v_id;
  end loop;

  update projects
     set current_stage = (p->'headline'->>'current_stage')::project_stage,
         current_owner_role = nullif(p->'headline'->>'current_owner_role', '')::public.role,
         reverify_return_stage = null
   where id = v_project;

  update external_submissions set stage_event_ids = v_stage_ids where id = v_sub;
  if v_request is not null then
    update external_submission_requests set submission_id = v_sub where id = v_request;
  end if;
  -- The project is submitted now, so every other open request on it is
  -- moot: pending ones are cancelled and unused grants retired, so none can
  -- be granted later or come back to life after an undo.
  update external_submission_requests
     set status = 'cancelled', updated_at = now()
   where project_id = v_project and status = 'pending';
  update external_submission_requests
     set status = 'expired', updated_at = now()
   where project_id = v_project and status = 'approved';
  return v_sub;
end
$$;

-- ── 7. undo_external_submission ─────────────────────────────────────────────
-- Restores the snapshot the apply took, in one transaction: rows that did not
-- exist are removed, rows that were overwritten get their prior values back,
-- the submission's stage events are deleted (so analytics never counts an
-- undone submission as a bid date), and the submission is kept, marked undone.

create or replace function undo_external_submission(
  p_submission_id uuid, p_user_id uuid, p_reason text
)
returns void
language plpgsql
volatile
security invoker
set search_path = pg_catalog, public
as $$
declare
  v_sub     external_submissions%rowtype;
  v_prior   jsonb;
  v_head    text;
  r         jsonb;
  v_restored uuid[] := '{}';
begin
  select * into v_sub from external_submissions where id = p_submission_id for update;
  if not found then
    raise exception 'external_submission:not_found';
  end if;
  if v_sub.status <> 'active' then
    raise exception 'external_submission:not_active';
  end if;
  perform 1 from projects where id = v_sub.project_id for update;
  if exists (select 1 from bid_outcomes where project_id = v_sub.project_id) then
    raise exception 'external_submission:outcome_recorded';
  end if;
  select current_task::text into v_head
    from project_category_state
   where project_id = v_sub.project_id and category = 'send_out';
  if v_head is distinct from 'submitted' then
    raise exception 'external_submission:not_submitted_head';
  end if;
  v_prior := coalesce(v_sub.prior_state, '{}'::jsonb);

  -- proposal_sends: overwritten rows get their prior values back.
  for r in select * from jsonb_array_elements(coalesce(v_prior->'proposal_sends', '[]'::jsonb)) loop
    update proposal_sends t
       set gc_name = x.gc_name,
           status = x.status,
           sent_via = x.sent_via,
           origin = x.origin,
           external_submission_id = x.external_submission_id,
           sent_at = x.sent_at,
           sent_by = x.sent_by,
           gc_email = x.gc_email,
           email_log_id = x.email_log_id,
           file_id = x.file_id,
           draft_id = x.draft_id,
           lines_hash = x.lines_hash,
           error = x.error,
           material_amount = x.material_amount,
           gear_amount = x.gear_amount,
           underground_amount = x.underground_amount,
           low_voltage_amount = x.low_voltage_amount,
           labor_amount = x.labor_amount
      from jsonb_populate_record(null::proposal_sends, r) x
     where t.id = x.id;
    v_restored := v_restored || (r->>'id')::uuid;
  end loop;
  delete from proposal_send_events
   where id in (
     select event_id from external_submission_gcs
      where submission_id = v_sub.id and event_id is not null
   );
  -- Rows this submission created (no prior row) are removed.
  delete from proposal_sends
   where external_submission_id = v_sub.id
     and origin = 'external_submission'
     and not (id = any(v_restored));

  for r in select * from jsonb_array_elements(coalesce(v_prior->'project_gcs', '[]'::jsonb)) loop
    update project_gcs
       set proposal_material_amount = (r->>'proposal_material_amount')::numeric,
           proposal_gear_amount = (r->>'proposal_gear_amount')::numeric,
           proposal_underground_amount = (r->>'proposal_underground_amount')::numeric,
           proposal_low_voltage_amount = (r->>'proposal_low_voltage_amount')::numeric,
           proposal_labor_amount = (r->>'proposal_labor_amount')::numeric
     where project_id = v_sub.project_id and gc_id = (r->>'gc_id')::uuid;
  end loop;

  delete from labor_reviews where project_id = v_sub.project_id;
  if jsonb_typeof(v_prior->'labor_review') = 'object' then
    insert into labor_reviews
    select * from jsonb_populate_record(null::labor_reviews, v_prior->'labor_review');
  end if;
  delete from markups where project_id = v_sub.project_id;
  if jsonb_typeof(v_prior->'markup') = 'object' then
    insert into markups
    select * from jsonb_populate_record(null::markups, v_prior->'markup');
  end if;
  delete from verifications where project_id = v_sub.project_id;
  if jsonb_typeof(v_prior->'verification') = 'object' then
    insert into verifications
    select * from jsonb_populate_record(null::verifications, v_prior->'verification');
  end if;

  delete from project_category_state where project_id = v_sub.project_id;
  insert into project_category_state
  select * from jsonb_populate_recordset(
    null::project_category_state, coalesce(v_prior->'category_state', '[]'::jsonb)
  );

  delete from stage_events where id = any(v_sub.stage_event_ids);
  -- Send_out events that followed the submission (a re-verify bounce and its
  -- return to Submitted, an outcome recorded and cleared) belong to it too;
  -- left behind, a 'Re-verify committed' row into 'submitted' would still
  -- count as a bid date for a submission that no longer exists.
  delete from stage_events
   where project_id = v_sub.project_id
     and category = 'send_out'
     and entered_at >= v_sub.submitted_at;

  if jsonb_typeof(v_prior->'project') = 'object' then
    update projects
       set current_stage = (v_prior->'project'->>'current_stage')::project_stage,
           current_owner_role = nullif(v_prior->'project'->>'current_owner_role', '')::public.role,
           reverify_return_stage = nullif(v_prior->'project'->>'reverify_return_stage', '')::project_stage
     where id = v_sub.project_id;
  end if;

  update external_submissions
     set status = 'undone', undone_at = now(), undone_by = p_user_id, undo_reason = p_reason
   where id = v_sub.id;
end
$$;

revoke all on function apply_external_submission(jsonb) from public;
revoke all on function apply_external_submission(jsonb) from anon, authenticated;
revoke all on function undo_external_submission(uuid, uuid, text) from public;
revoke all on function undo_external_submission(uuid, uuid, text) from anon, authenticated;
-- Service role only (the backend). Supabase grants it by default privileges;
-- stated explicitly so the revokes above can never leave the backend without it.
grant execute on function apply_external_submission(jsonb) to service_role;
grant execute on function undo_external_submission(uuid, uuid, text) to service_role;

notify pgrst, 'reload schema';
