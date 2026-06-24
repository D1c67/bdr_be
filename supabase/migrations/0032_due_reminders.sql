-- 0032 — Due-date reminders: per-user dedup ledger + notification preferences.
--
-- due_reminder_log: one row per reminder actually issued to a user. The
-- 5-column unique index is the idempotency mechanism — the poller upserts with
-- ignore-duplicates and creates notifications only for rows that were genuinely
-- inserted. Changing a due date changes due_at_snapshot, which re-arms every
-- offset for the new date automatically. The index must stay non-partial:
-- PostgREST on_conflict only targets full unique constraints/indexes.
--
-- notification_prefs: per-user reminder customization. Absent row = role
-- defaults computed in code; "Reset to default" = DELETE the row.

create table due_reminder_log (
  id               uuid primary key default gen_random_uuid(),
  project_id       uuid not null references projects(id) on delete cascade,
  user_id          uuid not null references profiles(id) on delete cascade,
  kind             text not null check (kind in
                     ('internal_bid','due_from_estimator','due_from_vendors','actual_bid')),
  offset_key       text not null,
  due_at_snapshot  timestamptz not null,
  created_at       timestamptz not null default now(),
  check ((kind = 'actual_bid' and offset_key in ('24h','8h','1h'))
      or (kind <> 'actual_bid' and offset_key in ('2w','1w','2d','1d','1h','expired')))
);

create unique index due_reminder_log_dedup_idx
  on due_reminder_log (project_id, user_id, kind, offset_key, due_at_snapshot);

create table notification_prefs (
  user_id     uuid primary key references profiles(id) on delete cascade,
  prefs       jsonb not null default '{}'::jsonb,
  updated_at  timestamptz not null default now()
);

create trigger notification_prefs_updated_at before update on notification_prefs
  for each row execute function set_updated_at();

-- RLS deny-by-default: no policies; all access flows through the service-role
-- backend (which bypasses RLS), matching 0007.
alter table due_reminder_log   enable row level security;
alter table due_reminder_log   force row level security;
alter table notification_prefs enable row level security;
alter table notification_prefs force row level security;
