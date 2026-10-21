-- 0160 - @mentions in project notes (estimator_notes).
--
-- A note may name other users with "@Full Name". The composer sends the
-- picked user ids alongside the body; the backend keeps only ids that are
-- mentionable on the project (active internal users plus the actively
-- assigned estimators) AND whose "@Full Name" is still in the body, then
-- stores them here. Every id in the array got a `note_mention` bell row and
-- the matching notification email when the note was posted.
--
-- Plain uuid[] rather than a link table: the list is written once with the
-- note (notes are append-only), read back only with the note, and never
-- queried by user. No FK on array elements, so a deleted user just stops
-- resolving to a name when the thread is rendered.

alter table estimator_notes
  add column if not exists mentioned_user_ids uuid[] not null default '{}';
