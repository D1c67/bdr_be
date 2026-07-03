-- 0044 — role consolidation.
-- Business change (confirmed with G3): rename the PA role to "Estimating Admin"
-- and merge the PM + PE roles into a single "Estimating Engineer" role.
--
-- The `role` enum (defined in 0001) is the type of exactly two columns:
--   profiles.role            (NOT NULL)
--   projects.current_owner_role (nullable)
-- Renaming an enum VALUE is metadata-only and rewrites both columns' stored
-- values automatically. Merging two values cannot be done by rename alone, so we
-- rename one (pm → estimating_engineer) and migrate the rows of the other (pe)
-- onto it with explicit UPDATEs. The orphaned 'pe' value is left in the enum
-- (Postgres cannot DROP an enum value) — it is simply never used again.
--
-- All authorization is enforced in the FastAPI layer (RLS is deny-by-default with
-- no per-role policies — see 0007), so no policy changes are needed here.

-- 1. Rename: pa → estimating_admin, pm → estimating_engineer.
alter type role rename value 'pa' to 'estimating_admin';
alter type role rename value 'pm' to 'estimating_engineer';

-- 2. Merge: fold the former PE rows into the (renamed) engineer value.
update profiles set role = 'estimating_engineer' where role = 'pe';
update projects set current_owner_role = 'estimating_engineer' where current_owner_role = 'pe';

-- Reload PostgREST's schema cache so the renamed enum is visible immediately.
notify pgrst, 'reload schema';
