-- 0159: Auto send (docs/AUTO_SEND.md section 3).
--
-- At 5:05 PM Pacific on a project's internal bid date, when nobody has sent
-- or marked the proposal submitted, the backend emails every GC a proposal
-- priced at the external estimator's Bid Recap plus a markup. Auto send keeps
-- its own record and never writes the Send Out tables (proposal_sends,
-- proposal_send_events, stage_events, project_category_state) except through
-- apply_auto_send_acceptance below, the explicit "Accept as submitted" action.
--
--   auto_send_settings    single row: the on/off switch (default OFF) and
--                         the markup percent
--   auto_send_runs        one row per (project, internal bid date): the claim
--                         that makes a send happen at most once, plus the
--                         frozen numbers and scope lines that went out
--   auto_send_gcs         one row per GC of a run: recipients, document,
--                         email_log row and outcome
--   auto_send_email_log   claim ledger for the 3:00 PM preflight and the
--                         5:10 PM digest (one email per user per day per kind)
--   auto_send_estimates   parse cache of the estimator workbook, keyed by the
--                         project_files row
--
-- proposal_sends gains origin 'auto_send' and auto_send_run_id so an accepted
-- auto send can be told apart from Send Out and Mark submitted rows.
--
-- project_files and estimator_submissions gain impersonated_by: the dev
-- account behind a "View as" upload or submit. Those writes land under the
-- real estimator's id, so auto send reads this stamp to never email GCs a
-- price the dev account produced.
--
-- (0154-0158 exist on another branch; this file is numbered past them so the
-- unique-prefix rule holds when the branches merge. Nothing here depends on
-- them.)
--
-- Every statement is idempotent. DEV database only until the release.

-- ── 1. settings ─────────────────────────────────────────────────────────────

create table if not exists auto_send_settings (
  id                  smallint primary key default 1 check (id = 1),
  enabled             boolean not null default false,
  markup_pct          numeric(5, 2) not null default 10
                      constraint auto_send_settings_markup_pct_check
                      check (markup_pct >= 0 and markup_pct <= 100),
  -- Set whenever enabled flips. Missed detection ignores send instants before
  -- the latest flip to ON, so turning the switch on never back-fills misses.
  enabled_changed_at  timestamptz,
  updated_by          uuid references profiles(id) on delete set null,
  updated_at          timestamptz not null default now()
);

comment on table auto_send_settings is
  'Single row. enabled is the IT Admin on/off switch (default OFF); markup_pct is added to the estimator Bid Recap materials and labor (Executive and IT Admin edit it). The env AUTO_SEND_POLLER_ENABLED is a separate kill switch.';

insert into auto_send_settings (id) values (1) on conflict (id) do nothing;

-- ── 2. runs ─────────────────────────────────────────────────────────────────

create table if not exists auto_send_runs (
  id                uuid primary key default gen_random_uuid(),
  project_id        uuid not null references projects(id) on delete cascade,
  -- Pacific calendar date of projects.internal_bid_at this run is for.
  bid_date          date not null,
  status            text not null
                    constraint auto_send_runs_status_check
                    check (status in ('sending', 'sent', 'partial', 'failed', 'skipped', 'missed')),
  trigger           text not null default 'scheduled'
                    constraint auto_send_runs_trigger_check
                    check (trigger in ('scheduled', 'manual')),
  -- [{code, message, gc_id?}]
  reasons           jsonb not null default '[]'::jsonb,
  markup_pct        numeric(5, 2),
  -- EstimateResult snapshot (services/auto_send_estimate.py load_estimate).
  estimate          jsonb,
  material_amount   numeric(14, 2),
  labor_amount      numeric(14, 2),
  total_amount      numeric(14, 2),
  draft_id          uuid references proposal_drafts(id) on delete set null,
  -- Frozen copy of the scope lines that went out, and their hash.
  lines_json        jsonb,
  lines_hash        text,
  -- AUTO_SEND_GC_REDIRECT in effect (non-production test sends), if any.
  redirected_to     text,
  started_at        timestamptz,
  finished_at       timestamptz,
  -- When the first GC email went out.
  sent_at           timestamptz,
  -- The user who pressed Send now (manual runs); null for scheduled runs.
  triggered_by      uuid references profiles(id) on delete set null,
  accepted_at       timestamptz,
  accepted_by       uuid references profiles(id) on delete set null,
  -- {labor_review, markup, verification} as they were before Accept wrote
  -- the auto sent numbers over them (audit / undo, like 0140's prior_state).
  accept_prior_state jsonb,
  created_at        timestamptz not null default now(),
  -- Doubles as the run's heartbeat: the sender bumps it before every GC step
  -- and the stuck-run reclaim measures staleness from it.
  updated_at        timestamptz not null default now(),
  -- The claim: inserting this row IS acquiring the right to send, so two
  -- uvicorn workers can never both send the same project for the same date.
  constraint auto_send_runs_project_date_key unique (project_id, bid_date)
);

comment on table auto_send_runs is
  'One auto send attempt per (project, internal bid date). The unique key is the multi-worker claim. A run at sent or partial blocks every later run for the project, even if the internal date moves.';

-- Idempotent for a database that ran an earlier copy of this file.
alter table auto_send_runs add column if not exists accept_prior_state jsonb;

create index if not exists auto_send_runs_bid_date_idx on auto_send_runs (bid_date);
create index if not exists auto_send_runs_project_idx on auto_send_runs (project_id);
create index if not exists auto_send_runs_sending_idx
  on auto_send_runs (started_at) where status = 'sending';

-- ── 3. per-GC outcome ───────────────────────────────────────────────────────

create table if not exists auto_send_gcs (
  id                uuid primary key default gen_random_uuid(),
  run_id            uuid not null references auto_send_runs(id) on delete cascade,
  project_id        uuid not null references projects(id) on delete cascade,
  -- RESTRICT like proposal_sends: what was emailed to a GC is a bid record.
  gc_id             uuid not null references general_contractors(id) on delete restrict,
  gc_name           text not null,
  status            text not null default 'pending'
                    constraint auto_send_gcs_status_check
                    check (status in ('pending', 'sending', 'sent', 'failed', 'unknown', 'skipped')),
  error             text,
  -- proposal_send.join_recipients(to) form, comparable with email_log.to_addrs.
  recipients        text,
  cc                jsonb not null default '[]'::jsonb,
  -- The rendered docx (project_files category 'proposal').
  file_id           uuid references project_files(id) on delete set null,
  email_log_id      uuid references email_log(id) on delete set null,
  material_amount   numeric(14, 2),
  labor_amount      numeric(14, 2),
  total_amount      numeric(14, 2),
  attempts          int not null default 0,
  sent_at           timestamptz,
  created_at        timestamptz not null default now(),
  updated_at        timestamptz not null default now(),
  constraint auto_send_gcs_run_gc_key unique (run_id, gc_id)
);

create index if not exists auto_send_gcs_project_idx on auto_send_gcs (project_id);
create index if not exists auto_send_gcs_gc_idx on auto_send_gcs (gc_id);
create index if not exists auto_send_gcs_file_idx on auto_send_gcs (file_id);
create index if not exists auto_send_gcs_email_log_idx on auto_send_gcs (email_log_id);

-- ── 4. internal email claim ledger ──────────────────────────────────────────
-- Same idempotency mechanism as due_digest_log (0116): the poller upserts with
-- ignore-duplicates and emails only the rows genuinely inserted; a failed
-- Graph send deletes its claim so the next tick retries that recipient.

create table if not exists auto_send_email_log (
  id          uuid primary key default gen_random_uuid(),
  send_date   date not null,
  kind        text not null
              constraint auto_send_email_log_kind_check
              check (kind in ('preflight', 'digest')),
  user_id     uuid not null references profiles(id) on delete cascade,
  created_at  timestamptz not null default now(),
  constraint auto_send_email_log_dedup_key unique (send_date, kind, user_id)
);

-- ── 5. estimate parse cache ─────────────────────────────────────────────────

create table if not exists auto_send_estimates (
  estimate_file_id  uuid primary key references project_files(id) on delete cascade,
  project_id        uuid not null references projects(id) on delete cascade,
  sha256            text,
  ok                boolean not null,
  code              text,
  message           text,
  result            jsonb not null default '{}'::jsonb,
  parsed_at         timestamptz not null default now()
);

comment on table auto_send_estimates is
  'Deterministic Bid Recap parse of a sealed estimator workbook. A row is reused only while its sha256 matches the file and result.parser_version matches the code; otherwise the workbook is parsed again and the row replaced.';

create index if not exists auto_send_estimates_project_idx on auto_send_estimates (project_id);

-- ── 6. proposal_sends: auto send origin ─────────────────────────────────────

alter table proposal_sends
  add column if not exists auto_send_run_id uuid;

alter table proposal_sends drop constraint if exists proposal_sends_origin_check;
alter table proposal_sends
  add constraint proposal_sends_origin_check
  check (origin in ('send_out', 'external_submission', 'auto_send'));

do $$
begin
  if not exists (
    select 1 from pg_constraint where conname = 'proposal_sends_auto_send_run_fkey'
  ) then
    alter table proposal_sends
      add constraint proposal_sends_auto_send_run_fkey
      foreign key (auto_send_run_id) references auto_send_runs(id) on delete set null;
  end if;
end $$;

create index if not exists proposal_sends_auto_send_run_idx
  on proposal_sends (auto_send_run_id) where auto_send_run_id is not null;

comment on column proposal_sends.origin is
  'send_out = generated/sent (or marked) on the Send Out step; external_submission = recorded by Mark submitted from the side menu (no document, no email); auto_send = an auto sent proposal accepted as the bid (Accept as submitted), auto_send_run_id points at the run.';

-- ── 6b. dev "View as" stamps ────────────────────────────────────────────────
-- Written only on impersonated writes (app/core/deps.py _impersonated_estimator),
-- NULL otherwise. auto_send._impersonated_estimate treats a stamped workbook
-- or round as priced by the dev account (owner decision: not auto sent).

alter table project_files
  add column if not exists impersonated_by uuid references profiles(id) on delete set null;
alter table estimator_submissions
  add column if not exists impersonated_by uuid references profiles(id) on delete set null;

comment on column project_files.impersonated_by is
  'The dev account that uploaded this file while viewing the portal as the estimator (uploaded_by is the estimator). NULL for every normal upload.';
comment on column estimator_submissions.impersonated_by is
  'The dev account that sealed this round while viewing the portal as the estimator. NULL for every normal submit.';

-- ── 7. RLS deny-by-default (the service-role backend bypasses it); see 0007 / 0055.

alter table auto_send_settings enable row level security;
alter table auto_send_settings force row level security;
alter table auto_send_runs enable row level security;
alter table auto_send_runs force row level security;
alter table auto_send_gcs enable row level security;
alter table auto_send_gcs force row level security;
alter table auto_send_email_log enable row level security;
alter table auto_send_email_log force row level security;
alter table auto_send_estimates enable row level security;
alter table auto_send_estimates force row level security;

-- ── 8. updated_at triggers ──────────────────────────────────────────────────

drop trigger if exists auto_send_settings_updated_at on auto_send_settings;
create trigger auto_send_settings_updated_at before update on auto_send_settings
  for each row execute function set_updated_at();
drop trigger if exists auto_send_runs_updated_at on auto_send_runs;
create trigger auto_send_runs_updated_at before update on auto_send_runs
  for each row execute function set_updated_at();
drop trigger if exists auto_send_gcs_updated_at on auto_send_gcs;
create trigger auto_send_gcs_updated_at before update on auto_send_gcs
  for each row execute function set_updated_at();

-- ── 9. apply_auto_send_acceptance ───────────────────────────────────────────
-- "Accept auto-send as submitted" (docs/AUTO_SEND.md section 9): records the
-- auto sent numbers as the bid, in one transaction. Modelled on
-- apply_external_submission (0140): the backend computes the lanes
-- (external_submission.target_lanes shape: [{category, current_task, status,
-- owner_role}]) and this function re-checks the state under row locks on the
-- project, the send_out lane and the run, then writes. Errors are raised as
-- 'auto_send_accept:<code>' so the backend can map them to a readable 409.
--
-- Like 0140 it also writes the canonical pricing rows (labor_reviews,
-- markups, a committed verifications snapshot) from the run's numbers, so the
-- pricing summary, analytics and a late GC's proposal read the auto sent
-- price as the bid. The rows they replace are kept on the run
-- (accept_prior_state).
--
-- Input: {project_id, run_id, actor_id, expected_send_out_head, lanes, note}
-- Returns {ok: true, run_id, proposal_send_ids: [...], gc_ids: [...]}.

create or replace function apply_auto_send_acceptance(p jsonb)
returns jsonb
language plpgsql
volatile
security definer
set search_path = public, pg_temp
as $$
declare
  v_project       uuid := (p->>'project_id')::uuid;
  v_run_id        uuid := (p->>'run_id')::uuid;
  v_actor         uuid := nullif(p->>'actor_id', '')::uuid;
  v_run           auto_send_runs%rowtype;
  v_stage         text;
  v_abandoned     timestamptz;
  v_return        text;
  v_head          text;
  v_head_status   text;
  v_g             auto_send_gcs%rowtype;
  v_e             jsonb;
  v_ps_id         uuid;
  v_ps_ids        uuid[] := '{}';
  v_gc_ids        uuid[] := '{}';
  v_owner         text;
  v_pct           numeric;
  v_base_m        numeric;
  v_base_l        numeric;
  v_prior         jsonb;
begin
  select current_stage::text, abandoned_at, reverify_return_stage::text
    into v_stage, v_abandoned, v_return
    from projects where id = v_project for update;
  if not found then
    raise exception 'auto_send_accept:project_not_found';
  end if;
  if v_stage in ('pm_only', 'cp_only') then
    raise exception 'auto_send_accept:never_bid';
  end if;
  if v_stage = 'declined' then
    raise exception 'auto_send_accept:declined';
  end if;
  if v_abandoned is not null then
    raise exception 'auto_send_accept:abandoned';
  end if;

  -- The send_out lane row is locked too, so a Send Out write racing this one
  -- waits for the commit instead of interleaving with it.
  select current_task::text, status::text into v_head, v_head_status
    from project_category_state
   where project_id = v_project and category = 'send_out'
     for update;

  select * into v_run from auto_send_runs where id = v_run_id for update;
  if not found or v_run.project_id is distinct from v_project then
    raise exception 'auto_send_accept:run_not_for_project';
  end if;
  if v_run.status not in ('sent', 'partial') then
    raise exception 'auto_send_accept:run_not_sent';
  end if;
  if v_run.accepted_at is not null then
    raise exception 'auto_send_accept:already_accepted';
  end if;

  if exists (
    select 1 from proposal_sends
     where project_id = v_project and status in ('sent', 'sending')
  ) then
    raise exception 'auto_send_accept:already_sent';
  end if;
  if exists (
    select 1 from external_submissions where project_id = v_project and status = 'active'
  ) then
    raise exception 'auto_send_accept:external_submission_active';
  end if;
  if exists (select 1 from bid_outcomes where project_id = v_project) then
    raise exception 'auto_send_accept:outcome_recorded';
  end if;
  if v_head in ('submitted', 'bid_outcome')
     or (v_head = 'verify' and v_return in ('submitted', 'bid_outcome')) then
    raise exception 'auto_send_accept:already_submitted';
  end if;
  if v_head is distinct from nullif(p->>'expected_send_out_head', '') then
    raise exception 'auto_send_accept:stale';
  end if;

  -- One proposal_sends row (plus its initial transmission event) per GC the
  -- run actually emailed. sent_by is the Send now user for a manual run and
  -- null for a scheduled one: the acceptor did not send anything.
  for v_g in
    select * from auto_send_gcs
     where run_id = v_run.id and status = 'sent'
     order by gc_name, id
  loop
    v_ps_id := null;
    insert into proposal_sends (
      project_id, gc_id, gc_name, status, sent_via, origin, auto_send_run_id,
      external_submission_id, sent_at, sent_by, gc_email, email_log_id, file_id,
      draft_id, lines_hash, error,
      material_amount, gear_amount, underground_amount, low_voltage_amount, labor_amount
    ) values (
      v_project, v_g.gc_id, v_g.gc_name, 'sent', 'email', 'auto_send', v_run.id,
      null, coalesce(v_g.sent_at, v_run.sent_at, now()), v_run.triggered_by, v_g.recipients,
      v_g.email_log_id, v_g.file_id, v_run.draft_id, v_run.lines_hash, null,
      v_g.material_amount, null, null, null, v_g.labor_amount
    )
    on conflict (project_id, gc_id) do update
       set gc_name = excluded.gc_name,
           status = 'sent',
           sent_via = 'email',
           origin = 'auto_send',
           auto_send_run_id = excluded.auto_send_run_id,
           external_submission_id = null,
           sent_at = excluded.sent_at,
           sent_by = excluded.sent_by,
           gc_email = excluded.gc_email,
           email_log_id = excluded.email_log_id,
           file_id = excluded.file_id,
           draft_id = excluded.draft_id,
           lines_hash = excluded.lines_hash,
           error = null,
           material_amount = excluded.material_amount,
           gear_amount = null,
           underground_amount = null,
           low_voltage_amount = null,
           labor_amount = excluded.labor_amount
     where proposal_sends.status not in ('sent', 'sending')
    returning id into v_ps_id;
    if v_ps_id is null then
      raise exception 'auto_send_accept:gc_already_sent';
    end if;

    insert into proposal_send_events (
      proposal_send_id, project_id, gc_id, kind, via, file_id, recipients, status,
      email_log_id, cc_recipients, sent_at, sent_by
    ) values (
      v_ps_id, v_project, v_g.gc_id, 'initial', 'email', v_g.file_id, v_g.recipients, 'sent',
      -- cc_recipients holds same-GC contacts ([{gc_contact_id, name, email}],
      -- 0110). Auto send CCs only the internal bids mailbox, which Send Out
      -- never records there either, so the column stays null.
      v_g.email_log_id, null,
      coalesce(v_g.sent_at, v_run.sent_at, now()), v_run.triggered_by
    );

    v_ps_ids := v_ps_ids || v_ps_id;
    v_gc_ids := v_gc_ids || v_g.gc_id;
  end loop;

  if coalesce(array_length(v_ps_ids, 1), 0) = 0 then
    raise exception 'auto_send_accept:no_sent_gcs';
  end if;

  -- Canonical pricing rows: the same places Labor Numbers, Markup and the
  -- Verify commit write (0140 precedent), so the auto sent price IS the bid
  -- everywhere the app reads one. Base figures are the estimator's Bid Recap
  -- (the run's frozen estimate snapshot); the markup is what was added on
  -- top. Section figures stay NULL: an auto send has no section rows, and a
  -- NULL section figure means "not part of the decomposition".
  if v_run.material_amount is null or v_run.labor_amount is null then
    raise exception 'auto_send_accept:no_amounts';
  end if;
  v_pct := coalesce(v_run.markup_pct, 0);
  v_base_m := nullif(v_run.estimate->>'materials', '')::numeric;
  v_base_l := nullif(v_run.estimate->>'labor', '')::numeric;
  if v_base_m is null then
    v_base_m := round(v_run.material_amount / (1 + v_pct / 100), 2);
  end if;
  if v_base_l is null then
    v_base_l := round(v_run.labor_amount / (1 + v_pct / 100), 2);
  end if;

  v_prior := jsonb_build_object(
    'labor_review', (select to_jsonb(r) from labor_reviews r where r.project_id = v_project),
    'markup', (select to_jsonb(mk) from markups mk where mk.project_id = v_project),
    'verification', (select to_jsonb(v) from verifications v where v.project_id = v_project)
  );

  insert into labor_reviews (project_id, labor_amount, reviewed_by, updated_at)
  values (v_project, v_base_l, v_actor, now())
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
    v_project, v_actor, now(),
    v_pct, v_run.material_amount - v_base_m, null, null,
    null, null,
    null, null,
    v_pct, v_run.labor_amount - v_base_l
  )
  on conflict (project_id) do update
     set set_by = excluded.set_by,
         updated_at = now(),
         materials_markup_pct = excluded.materials_markup_pct,
         materials_markup_amount = excluded.materials_markup_amount,
         gear_markup_pct = null,
         gear_markup_amount = null,
         underground_markup_pct = null,
         underground_markup_amount = null,
         low_voltage_markup_pct = null,
         low_voltage_markup_amount = null,
         labor_markup_pct = excluded.labor_markup_pct,
         labor_markup_amount = excluded.labor_markup_amount;

  insert into verifications (
    project_id, verified_by, committed_at, updated_at, notes,
    labor_amount, materials_amount, gear_amount, underground_amount, low_voltage_amount,
    labor_markup_amount, materials_markup_amount, gear_markup_amount,
    underground_markup_amount, low_voltage_markup_amount
  ) values (
    v_project, v_actor, now(), now(), nullif(p->>'note', ''),
    v_base_l, v_base_m, null, null, null,
    v_run.labor_amount - v_base_l, v_run.material_amount - v_base_m, null,
    null, null
  )
  on conflict (project_id) do update
     set verified_by = excluded.verified_by,
         committed_at = now(),
         updated_at = now(),
         notes = excluded.notes,
         labor_amount = excluded.labor_amount,
         materials_amount = excluded.materials_amount,
         gear_amount = null,
         underground_amount = null,
         low_voltage_amount = null,
         labor_markup_amount = excluded.labor_markup_amount,
         materials_markup_amount = excluded.materials_markup_amount,
         gear_markup_amount = null,
         underground_markup_amount = null,
         low_voltage_markup_amount = null;

  -- Lanes: every lane but send_out complete, send_out parked on Submitted
  -- (same upsert as apply_external_submission).
  for v_e in select * from jsonb_array_elements(coalesce(p->'lanes', '[]'::jsonb)) loop
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
    if v_e->>'category' = 'send_out' then
      v_owner := nullif(v_e->>'owner_role', '');
    end if;
  end loop;

  -- One stage event into Submitted, dated when the auto send went out so
  -- analytics reads the real bid date, not the acceptance date.
  insert into stage_events (project_id, from_stage, to_stage, category, actor_id, note, entered_at)
  values (
    v_project,
    case when v_head_status = 'locked' then null else nullif(v_head, '')::project_stage end,
    'submitted',
    'send_out',
    v_actor,
    nullif(p->>'note', ''),
    coalesce(v_run.sent_at, now())
  );

  update projects
     set current_stage = 'submitted',
         current_owner_role = v_owner::public.role,
         reverify_return_stage = null
   where id = v_project;

  update auto_send_runs
     set accepted_at = now(), accepted_by = v_actor, accept_prior_state = v_prior
   where id = v_run.id;

  return jsonb_build_object(
    'ok', true,
    'run_id', v_run.id,
    'proposal_send_ids', to_jsonb(v_ps_ids),
    'gc_ids', to_jsonb(v_gc_ids)
  );
end
$$;

revoke all on function apply_auto_send_acceptance(jsonb) from public;
revoke all on function apply_auto_send_acceptance(jsonb) from anon, authenticated;
-- Service role only (the backend). Supabase grants it by default privileges;
-- stated explicitly so the revokes above can never leave the backend without it.
grant execute on function apply_auto_send_acceptance(jsonb) to service_role;

notify pgrst, 'reload schema';
