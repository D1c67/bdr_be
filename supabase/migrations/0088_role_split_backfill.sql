-- 0088 — engineer role split, part 2 of 2: remap stored 'estimating_engineer'.
-- The enum value survives (0087) but nothing may reference it anymore: the app
-- enum no longer parses it, so a stray profiles.role would brick that login at
-- the CurrentUser choke point.
--
-- Columns of type role: profiles.role, projects.current_owner_role,
-- project_category_state.owner_role (0078). The owner columns store the FIRST
-- owner of the head task, so engineer-valued rows sit on the tasks where the
-- old engineer was first owner; remap them to that task's new first owner:
--   go_no_go, rfqs, receive_quotes      -> estimating_engineer_materials
--   labor_numbers, markup, gc_pricing   -> estimating_engineer_labor
--
-- Profiles: any remaining old-role user becomes the MATERIALS engineer as the
-- mechanical default (dev has none today; on staging/prod this runs only at a
-- release, where each engineer's focus gets assigned by hand afterwards via the
-- Users admin).

update profiles
set role = 'estimating_engineer_materials'
where role = 'estimating_engineer';

update projects
set current_owner_role = case
    when current_stage in ('labor_numbers', 'markup', 'gc_pricing')
        then 'estimating_engineer_labor'::role
    else 'estimating_engineer_materials'::role
end
where current_owner_role = 'estimating_engineer';

update project_category_state
set owner_role = case
    when current_task in ('labor_numbers', 'markup', 'gc_pricing')
        then 'estimating_engineer_labor'::role
    else 'estimating_engineer_materials'::role
end
where owner_role = 'estimating_engineer';

-- Reload PostgREST's schema cache so the new enum values are visible immediately.
notify pgrst, 'reload schema';
