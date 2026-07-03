-- 0045 — Cached "user has a verified TOTP factor" flag.
--
-- The source of truth for MFA factors is Supabase Auth (GoTrue). This column is
-- a cheap local cache the FastAPI backend self-stamps to TRUE on a user's first
-- AAL2 request (reaching aal2 proves a verified factor exists), mirroring how
-- invite_accepted_at is stamped on first auth (see get_current_user). It is reset
-- to FALSE by the admin/self 2FA-reset endpoints. Two uses:
--   1. choose the AAL1 rejection signal returned by get_current_user
--      (mfa_enrollment_required when false vs mfa_step_up_required when true);
--   2. surface a "2FA on/off" column in the admin user list (ProfileOut.mfa_enrolled).
--
-- No backfill: every existing user currently has zero factors, so the default
-- FALSE is correct. They are caught by the forced MFA gate on next login and the
-- flag self-stamps TRUE once they reach aal2.
alter table public.profiles
  add column if not exists mfa_enrolled boolean not null default false;

-- Reload PostgREST's schema cache so the new column is exposed immediately.
notify pgrst, 'reload schema';
