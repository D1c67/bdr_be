-- 0177 - Notification routing by role (docs/TEAMS_ALERTS.md, "Who gets what").
-- Executives and IT Admins choose, per role and per notification event,
-- whether it goes out by email and whether it goes to Microsoft Teams
-- (Settings > Notification routing). The defaults live in code
-- (services/notification_routing.py); a row here overrides one cell of the
-- grid. NULL in a channel column = that channel uses the code default.
-- The bell is not routed: every notification still lands in the bell.
-- Service role only: the API reads and writes it.
create table if not exists notification_channel_prefs (
  role text not null,
  event text not null,
  email boolean,
  teams boolean,
  updated_by uuid references profiles(id) on delete set null,
  updated_at timestamptz not null default now(),
  primary key (role, event)
);

alter table notification_channel_prefs enable row level security;
alter table notification_channel_prefs force row level security;

comment on table notification_channel_prefs is
  'Per-role email/Teams routing overrides for notification events (0177). Absent row or NULL column = code default. Service role only.';

notify pgrst, 'reload schema';
