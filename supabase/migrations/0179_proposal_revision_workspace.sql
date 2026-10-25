-- 0179: Revise proposal becomes a self-contained workspace.
--
-- Everything for an open revision now happens inside the Revise proposal
-- modal (docs/PROPOSAL_REVISIONS.md). Three schema changes support it:
--
--   1. The Executive's re-verify moves INTO the revision. A cost edit made
--      during an open revision no longer bounces the send_out head to Verify
--      (the bid stays at Submitted, no stage_events); the revision records
--      that costs changed, and generate / send are refused until an
--      Executive approves the new figures in the modal (which re-commits the
--      verifications snapshot). Pending = costs_changed_at is set and later
--      than costs_approved_at (or that is null).
--   2. start_verification: the committed pricing snapshot when the revision
--      started (the "held" figures the approval step compares against).
--   3. project_files.proposal_revision_id: revised plans, specs and addenda
--      uploaded from the modal are tagged with their revision.
--
-- Data: the revision open on dev at release (and any other open one) is
-- migrated in step 3: snapshot taken, per-GC overrides it carried over
-- untouched from before the revision cleared (GC prices in a revision start
-- from the current computed figures, like the initial GC Pricing step), and
-- a legacy Verify bounce returned to Submitted as a pending cost change.
-- Every statement is idempotent.

-- ── 1. approval state on the revision ───────────────────────────────────────

alter table proposal_revisions
  add column if not exists costs_changed_at timestamptz,
  add column if not exists costs_changed_by uuid,
  add column if not exists costs_change_reason text,
  add column if not exists costs_approved_at timestamptz,
  add column if not exists costs_approved_by uuid,
  add column if not exists approval_requested_at timestamptz,
  add column if not exists start_verification jsonb;

do $$
begin
  if not exists (select 1 from pg_constraint where conname = 'proposal_revisions_costs_changed_by_fkey') then
    alter table proposal_revisions
      add constraint proposal_revisions_costs_changed_by_fkey
      foreign key (costs_changed_by) references profiles(id) on delete set null;
  end if;
  if not exists (select 1 from pg_constraint where conname = 'proposal_revisions_costs_approved_by_fkey') then
    alter table proposal_revisions
      add constraint proposal_revisions_costs_approved_by_fkey
      foreign key (costs_approved_by) references profiles(id) on delete set null;
  end if;
  if not exists (select 1 from pg_constraint where conname = 'proposal_revisions_change_reason_len') then
    alter table proposal_revisions
      add constraint proposal_revisions_change_reason_len
      check (costs_change_reason is null or char_length(costs_change_reason) <= 500);
  end if;
end $$;

create index if not exists proposal_revisions_costs_changed_by_idx
  on proposal_revisions (costs_changed_by) where costs_changed_by is not null;
create index if not exists proposal_revisions_costs_approved_by_idx
  on proposal_revisions (costs_approved_by) where costs_approved_by is not null;

comment on column proposal_revisions.costs_changed_at is
  'Last cost-side edit (quotes, selection, labor, markup, general material) during the revision. Generate / send wait for an approval later than this.';
comment on column proposal_revisions.costs_approved_at is
  'When an Executive approved the revised costs in Revise proposal (the verifications snapshot was re-committed at that moment).';
comment on column proposal_revisions.start_verification is
  'The verifications row (committed snapshot) when the revision started: the figures the GCs hold.';

-- ── 2. revised files tagged with their revision ─────────────────────────────

alter table project_files
  add column if not exists proposal_revision_id uuid;

do $$
begin
  if not exists (select 1 from pg_constraint where conname = 'project_files_proposal_revision_fkey') then
    alter table project_files
      add constraint project_files_proposal_revision_fkey
      foreign key (proposal_revision_id) references proposal_revisions(id) on delete set null;
  end if;
end $$;

create index if not exists project_files_proposal_revision_idx
  on project_files (proposal_revision_id) where proposal_revision_id is not null;

-- ── 3. open revisions at release ────────────────────────────────────────────

-- a. The held pricing snapshot, where missing.
update proposal_revisions r
   set start_verification = to_jsonb(v)
  from verifications v
 where v.project_id = r.project_id
   and r.status = 'open'
   and r.start_verification is null;

-- b. Per-GC overrides a checked, unsent GC carried in from before the
--    revision, untouched since the start: cleared, so its revised price is
--    computed from the current figures (its start values stay in
--    start_overrides and come back if its revision does not go out).
update project_gcs pg
   set proposal_material_amount = null,
       proposal_gear_amount = null,
       proposal_underground_amount = null,
       proposal_low_voltage_amount = null,
       proposal_labor_amount = null
  from proposal_revision_sends rs
  join proposal_revisions r on r.id = rs.revision_id
 where r.status = 'open'
   and rs.included
   and rs.status in ('pending', 'generated', 'failed')
   and coalesce(rs.error, '') not like 'outcome unknown%'
   and coalesce(rs.error, '') not like 'email delivered but not recorded%'
   and rs.start_overrides is not null
   and pg.project_id = rs.project_id
   and pg.gc_id = rs.gc_id
   and pg.proposal_material_amount is not distinct from (rs.start_overrides->>'proposal_material_amount')::numeric
   and pg.proposal_gear_amount is not distinct from (rs.start_overrides->>'proposal_gear_amount')::numeric
   and pg.proposal_underground_amount is not distinct from (rs.start_overrides->>'proposal_underground_amount')::numeric
   and pg.proposal_low_voltage_amount is not distinct from (rs.start_overrides->>'proposal_low_voltage_amount')::numeric
   and pg.proposal_labor_amount is not distinct from (rs.start_overrides->>'proposal_labor_amount')::numeric
   and (pg.proposal_material_amount is not null or pg.proposal_gear_amount is not null
        or pg.proposal_underground_amount is not null or pg.proposal_low_voltage_amount is not null
        or pg.proposal_labor_amount is not null);

-- c. A legacy Verify bounce inside an open revision: the bid returns to
--    Submitted (with the matching stage event, so time-in-stage closes the
--    span) and the revision carries a pending cost change instead.
with bounced as (
  select r.id as revision_id, r.project_id
    from proposal_revisions r
    join projects p on p.id = r.project_id
    join project_category_state cs on cs.project_id = r.project_id and cs.category = 'send_out'
   where r.status = 'open'
     and cs.current_task = 'verify'
     and p.reverify_return_stage = 'submitted'
), moved as (
  update project_category_state cs
     set current_task = 'submitted', status = 'active', completed_at = null,
         owner_role = 'estimating_admin'
    from bounced b
   where cs.project_id = b.project_id and cs.category = 'send_out'
  returning cs.project_id
), events as (
  insert into stage_events (project_id, from_stage, to_stage, category, actor_id, note)
  select m.project_id, 'verify', 'submitted', 'send_out', null,
         'Revision cost approval moved into Revise proposal (0179)'
    from moved m
  returning project_id
), cleared as (
  update projects p
     set reverify_return_stage = null, current_stage = 'submitted',
         current_owner_role = 'estimating_admin'
    from moved m
   where p.id = m.project_id
  returning p.id
)
update proposal_revisions r
   set costs_changed_at = coalesce(r.costs_changed_at, now()),
       costs_change_reason = coalesce(r.costs_change_reason, 'Costs changed before 0179')
  from bounced b
 where r.id = b.revision_id
   and exists (select 1 from cleared c where c.id = b.project_id)
   and exists (select 1 from events e where e.project_id = b.project_id);

notify pgrst, 'reload schema';
