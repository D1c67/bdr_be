-- 0092 — Remember that an external estimator has taken the portal tour.
--
-- The portal's first-run tour (a click-through over the fictional sample
-- project) must offer itself exactly once. Until now the only first-run marker
-- in the portal was a localStorage key behind the guide banner, which is a
-- per-DEVICE fact: an estimator who signs in from the office desktop and then
-- from a laptop gets asked twice, and nobody on the team can see whether
-- someone has actually been onboarded or tell the portal to ask them again.
--
-- A timestamp rather than a boolean, matching invite_accepted_at (0014): it
-- answers "have they" and "when" with one column, and the when is the only
-- thing that makes it useful for onboarding follow-up. Set once — a replay
-- from the documentation page does NOT restamp it, so the value stays the
-- first completion. NULL = never finished it (including "skipped part way"),
-- which is the same answer for the purpose it serves: offer the tour.
--
-- Nullable with no default: every existing estimator reads as "hasn't taken
-- it", which is true — the tour did not exist when they were invited. They
-- get it offered once on their next visit and can dismiss it in one click.

alter table profiles
  add column if not exists estimator_tour_completed_at timestamptz;

comment on column profiles.estimator_tour_completed_at is
  'When this user first finished the estimator portal tour. NULL = never finished it (never started, or skipped part way), which is what makes the portal offer it. Only the external estimator portal reads it; a replay does not restamp it.';

notify pgrst, 'reload schema';
