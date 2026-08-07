-- 0102 — Quote approval, plus the state migration for the two lanes 0101 declared.
--
-- Part 1: APPROVAL. A quote is "approved" when a human has confirmed the figure the
-- extractor pulled off the vendor's PDF is actually right and the sales-tax question
-- has been answered. That happens on Receive Quotes. Only an approved quote can be
-- picked as a category's winner on Select Vendors — but approval is per QUOTE, not
-- per category: a winner can be chosen from the approved quotes that have arrived
-- while other vendors are still out.
--
-- Part 2: LANE SPLIT. Seed the `select_vendors` and `markup` lane rows for every
-- project that already has category state, and collapse the labor lane from
-- [labor_numbers, markup] to [labor_numbers].
--
-- 0101 must already be applied (this file USES the labels it declares).

-- ── Part 1: quote approval ───────────────────────────────────────────────────
alter table quotes
  add column if not exists is_approved boolean not null default false,
  add column if not exists approved_by uuid references profiles(id) on delete set null,
  add column if not exists approved_at timestamptz;

create index if not exists quotes_approved_idx on quotes(rfq_id) where is_approved;

-- Every quote that already exists predates the approval step. Left unapproved, they
-- would be unselectable, so every live bid's winner would become unpickable the
-- moment this deploys. Grandfather them in.
update quotes
   set is_approved = true,
       approved_at = coalesce(approved_at, received_at)
 where is_approved = false;

-- ── Part 2: seed the two new lanes ───────────────────────────────────────────
-- Both new lanes are derived from the lane they split off, so a bid keeps exactly
-- the trajectory it already had:
--
--   material lane complete  ->  select_vendors complete   (it is past this work; a
--                               fresh `active` lane would RE-LOCK Send Out on bids
--                               that are already out the door)
--   material lane active    ->  select_vendors active     (openable immediately —
--                               that is the whole point of the new step)
--   material lane locked    ->  select_vendors locked
--
-- and the same shape for markup off the labor lane. A labor lane sitting ON `markup`
-- means labor itself is finished, so it collapses to a COMPLETE single-task lane.
do $$
declare l record;
begin
  for l in
    select project_id, status, current_task
      from project_category_state
     where category = 'labor_numbers'
  loop
    insert into project_category_state
      (project_id, category, current_task, status, owner_role, completed_at)
    values (
      l.project_id,
      'markup',
      'markup',
      case
        when l.status = 'complete' then 'complete'::category_status
        when l.status = 'locked'   then 'locked'::category_status
        else 'active'::category_status
      end,
      'estimating_engineer_labor'::role,
      case when l.status = 'complete' then now() else null end
    )
    on conflict (project_id, category) do nothing;
  end loop;

  for l in
    select project_id, status
      from project_category_state
     where category = 'material_numbers'
  loop
    insert into project_category_state
      (project_id, category, current_task, status, owner_role, completed_at)
    values (
      l.project_id,
      'select_vendors',
      'select_vendors',
      case
        when l.status = 'complete' then 'complete'::category_status
        when l.status = 'locked'   then 'locked'::category_status
        else 'active'::category_status
      end,
      'estimating_engineer_materials'::role,
      case when l.status = 'complete' then now() else null end
    )
    on conflict (project_id, category) do nothing;
  end loop;
end $$;

-- Collapse the labor lane to its single remaining task. This MUST run after the
-- markup seeding above, which reads the pre-collapse head.
--
-- A row left at current_task = 'markup' would be a live crash, not cosmetic: the
-- lane predicates in services/workflow.py do tasks.index(current_task) against
-- CATEGORY_TASKS['labor_numbers'], which no longer contains 'markup'.
--
-- SET expressions see the pre-UPDATE row, so the CASEs still read the old head.
update project_category_state
   set current_task  = 'labor_numbers',
       status        = case
                         when status = 'active' and current_task = 'markup'
                         then 'complete'::category_status
                         else status
                       end,
       completed_at  = case
                         when status = 'active' and current_task = 'markup'
                         then now()
                         else completed_at
                       end,
       owner_role    = 'estimating_engineer_labor'::role
 where category = 'labor_numbers';

-- Re-tag historical Markup events onto the lane that now owns them, so time-in-lane
-- analytics does not show an empty Markup lane for every project ever bid.
update stage_events
   set category = 'markup'::project_category
 where to_stage = 'markup'
   and category = 'labor_numbers';

notify pgrst, 'reload schema';
