-- 0087 — engineer role split, part 1 of 2: new enum values.
-- Business change: the single Estimating Engineer role is split BY FOCUS into
--   estimating_engineer_materials  (owns the material_numbers lane)
--   estimating_engineer_labor      (owns the labor_numbers lane + the engineer
--                                   side of send_out)
-- Permissions are identical (both are full writers, same as the old role); the
-- split only changes the per-stage "whose task" hint that drives to-do lists,
-- default reminder audiences, and role-targeted notifications.
--
-- Split across two migrations because a value added by ALTER TYPE ... ADD VALUE
-- cannot be USED in the same transaction (Postgres 55P04); the row remaps are
-- in 0088. The old 'estimating_engineer' value stays in the enum unused —
-- Postgres cannot drop enum values (same as the orphaned 'pe' from 0044).

alter type role add value if not exists 'estimating_engineer_materials';
alter type role add value if not exists 'estimating_engineer_labor';
