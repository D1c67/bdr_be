-- 0043 — re-verification on a pricing-affecting edit.
-- When a pricing-affecting edit (labor, markup, quotes/prices, general material,
-- BOQ confirm) lands on a project that has ALREADY passed Verify (current_stage
-- order > 10: send_out / submitted / bid_outcome), the project is bounced back to
-- 'verify' so the Executive re-commits the numbers they signed off on. We remember
-- the stage it was on so the re-commit returns it THERE (not always send_out) — see
-- app/services/workflow.py reopen_verify / return_from_reverify. NULL = not
-- currently bounced; set once on the first bounce, never overwritten while bounced,
-- and cleared when the Executive re-commits. Defaults to NULL so reads degrade
-- gracefully before this migration is applied.
alter table projects
    add column if not exists reverify_return_stage project_stage;

-- Reload PostgREST's schema cache so the new column is visible immediately.
notify pgrst, 'reload schema';
