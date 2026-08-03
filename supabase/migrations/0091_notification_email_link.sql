-- 0091 — Link a bell notification to the email that mirrored it.
--
-- Every in-app notification is also mirrored to the recipient's inbox as a
-- branded email (services/notification_email), and each of those sends already
-- writes an `email_log` row. Until now nothing tied the two together, so the
-- per-project notification log had to guess (subject prefix + recipient +
-- time window) which email belonged to which bell row — and a log people are
-- meant to trust must not be assembled from guesses.
--
-- This is the same explicit-link convention every other sender already
-- follows: rfq_sends.email_log_id (0020), proposal_sends.email_log_id (0024),
-- file_send_recipients.email_log_id (0076), submittal_request_sends (0073),
-- submittal_packages (0081).
--
-- ON DELETE SET NULL, matching all of the above: purging email_log must never
-- destroy the notification record — it only drops the delivery evidence. Rows
-- created before this migration stay NULL forever; the notification-log
-- builder falls back to the legacy time-window match for those.

alter table notifications
  add column if not exists email_log_id uuid references email_log(id) on delete set null;

comment on column notifications.email_log_id is
  'The email_log row for this notification''s mirror email, set best-effort after the send. NULL = no mirror was sent (mirror_email=False, emails disabled, no recipient address), the send failed, or the row predates migration 0091.';

-- Only the notification-log join reads this, and only for rows that have one.
create index if not exists notifications_email_log_idx
  on notifications(email_log_id) where email_log_id is not null;

notify pgrst, 'reload schema';
