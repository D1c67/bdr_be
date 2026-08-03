-- 0082 — Submittal approval verdicts + resubmittals
--
-- 0081 shipped the GC-facing send and laid down `approval_status` at both the
-- package and the item grain, with nothing writing to them. This migration
-- completes the round trip:
--
--   1. WHO recorded the verdict. The GC's answer arrives as an email, a marked-up
--      transmittal, or a phone call — a human reads it and logs it here, so the
--      row needs to say which human. (An automated reply-matcher can write the
--      same columns later with responded_by null.)
--
--   2. RESUBMITTALS. A GC routinely approves most of a package and rejects one
--      cut sheet; the team fixes it and sends it again. That resend is a NEW
--      package (its own number, transmittal, email thread and verdicts) that
--      points back at the one it answers, so the original's verdicts stay frozen
--      as the historical record instead of being overwritten.
--
-- No new status values are needed: 0081's CHECK constraints already cover
-- pending/approved/partial/denied at the package grain and
-- pending/approved/approved_as_noted/rejected at the item grain. The package
-- headline is DERIVED from its items in the service layer
-- (submittal_approval._rollup) rather than by a trigger, because "all decided
-- and all approved" vs "some still pending" is presentation policy, not a
-- storage invariant, and it needs to change without a migration.

-- 1. Who logged the verdict, at both grains. ────────────────────────────────
--    Nullable: rows verdicted before this column existed have no answer, and a
--    future automated matcher legitimately has no user to name.
alter table submittal_packages
  add column responded_by uuid references profiles(id) on delete set null;

alter table submittal_package_items
  add column responded_by uuid references profiles(id) on delete set null;

-- 2. The resubmittal link. ───────────────────────────────────────────────────
--    Self-referential and nullable — most packages are originals. ON DELETE SET
--    NULL rather than CASCADE: deleting a superseded package must not take its
--    resubmittals (the live ones) with it; the chain just loses a link.
--
--    Deliberately NOT unique. A package can be resubmitted more than once (the
--    second fix gets rejected too), so several rows may point at the same
--    parent, and the log presents them by number order.
alter table submittal_packages
  add column supersedes_package_id uuid references submittal_packages(id) on delete set null;

create index submittal_packages_supersedes_idx
  on submittal_packages(supersedes_package_id)
  where supersedes_package_id is not null;

notify pgrst, 'reload schema';
