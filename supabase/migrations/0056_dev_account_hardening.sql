-- ============================================================================
-- OPTIONAL / NEEDS OWNER CONFIRMATION BEFORE APPLYING. The is_dev flag is a
-- deliberate owner feature (role-switch testing). This migration only removes
-- the SELF-REINSTATING trigger (the one-time UPDATE in 0013 already flagged
-- the live profile, so the flag persists); it does NOT remove is_dev itself.
-- Do not apply to prod without owner sign-off.
-- ============================================================================
--
-- 0056 — Drop the dev-account auto-flag trigger from 0013.
--
-- 0013 installed a BEFORE INSERT trigger that re-flags any newly inserted
-- profile whose email matches the hardcoded dev address. That means deleting
-- and re-inviting the account silently re-grants dev powers — an email-based
-- backdoor. Removing the trigger makes is_dev a plain column managed only by
-- explicit UPDATEs (service_role / SQL), while the existing dev profile keeps
-- its flag.

drop trigger if exists profiles_flag_dev on profiles;
drop function if exists flag_dev_account();
