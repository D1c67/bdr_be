-- 0175 - Microsoft Teams alerts (docs/TEAMS_ALERTS.md). Every notification
-- that is mirrored to email is also sent to the recipient in Teams as a
-- 1:1 message from the "BDR Alerts" bot, with a button that opens the same
-- page the email links to.
--
-- One row per internal user who has a Teams state: their on/off switch
-- (Settings > Microsoft Teams alerts; absent row = on) and the cached
-- Teams addressing the sender resolved for them (Entra object id, the
-- bot's 1:1 chat id), plus the last delivery and the last error for the
-- Settings status line. Service role only: the API reads and writes it.
create table if not exists teams_alert_users (
  user_id uuid primary key references profiles(id) on delete cascade,
  enabled boolean not null default true,
  aad_object_id text,
  conversation_id text,
  last_sent_at timestamptz,
  last_error text,
  last_error_at timestamptz,
  updated_at timestamptz not null default now()
);

alter table teams_alert_users enable row level security;
alter table teams_alert_users force row level security;

comment on table teams_alert_users is
  'Teams alerts per user (0175): the on/off switch (absent row = on) and the cached Teams addressing (Entra object id, bot chat id) the sender resolved. Service role only.';

-- When the Teams copy of a bell row was delivered. NULL = not sent to Teams
-- (feature off, user switched it off, not mirrored, or the send failed).
alter table notifications add column if not exists teams_sent_at timestamptz;

comment on column notifications.teams_sent_at is
  'When the Teams alert for this notification was delivered (0175). NULL = no Teams copy.';

notify pgrst, 'reload schema';
