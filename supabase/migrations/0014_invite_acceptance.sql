-- 0014 — Invite acceptance tracking. Splits the single is_active flag into a
-- clearer lifecycle so admins can tell pending invites from working accounts:
--   • invite_accepted_at IS NULL  → "invited" (email sent, not yet accepted)
--   • invite_accepted_at IS NOT NULL, is_active = true → "active"
--   • is_active = false → "disabled"
--
-- The timestamp is stamped the first time the user successfully authenticates
-- (see get_current_user) — i.e. once they've accepted the invite and set a
-- password. Existing profiles predate this column and are already working
-- accounts, so backfill them as accepted to avoid flagging them as pending.

alter table profiles add column invite_accepted_at timestamptz;

update profiles set invite_accepted_at = created_at where invite_accepted_at is null;
