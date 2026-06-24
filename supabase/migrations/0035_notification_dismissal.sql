-- 0035 — Notification auto-dismissal.
--
-- Notifications represent pending action items. Once the underlying task is
-- complete (bid submitted, stage advanced, reply read, quote priced, …) the
-- notification should disappear from the bell — distinct from `read_at`, which
-- the user sets manually and which only greys the row. `dismissed_at` is a
-- soft-delete: the app filters these rows out of GET /notifications but keeps
-- them for history. `rfq_id` lets quote/reply notifications be dismissed for a
-- specific RFQ when that category is priced (notifications otherwise reference
-- only the project). Mirrors email_log, which already pairs project_id + rfq_id.
--
-- Numbered 0035 (was originally authored as a second 0034, colliding with
-- 0034_todo_nudge): that duplicate version caused this migration to be skipped
-- entirely against the live DB. Statements are idempotent so re-applying over
-- the already-recovered database is a safe no-op.

alter table notifications add column if not exists dismissed_at timestamptz;
alter table notifications add column if not exists rfq_id uuid references rfqs(id) on delete cascade;

-- The bell's hot query is "active notifications for a user, newest first".
-- A partial index keeps only undismissed rows, so it stays compact as history
-- accumulates.
create index if not exists notifications_user_active_idx
  on notifications(user_id, created_at desc) where dismissed_at is null;
