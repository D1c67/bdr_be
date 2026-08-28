-- 0116 - Daily "bids due today" digest: per-recipient send ledger.
--
-- due_digest_log: one row per digest email actually sent to a user for one
-- office-calendar (Pacific) date. The unique index is the idempotency
-- mechanism: the poller upserts with ignore-duplicates and emails only the
-- rows that were genuinely inserted, so concurrent uvicorn workers cannot
-- double-send and a restart inside the send window resumes exactly where it
-- left off. A failed Graph send deletes its claim row so the next tick
-- retries that recipient.
--
-- (0107-0115 exist on another branch; this file is numbered past them so the
-- unique-prefix rule holds when the branches merge.)

create table due_digest_log (
  id           uuid primary key default gen_random_uuid(),
  digest_date  date not null,
  user_id      uuid not null references profiles(id) on delete cascade,
  created_at   timestamptz not null default now()
);

create unique index due_digest_log_dedup_idx
  on due_digest_log (digest_date, user_id);

-- RLS deny-by-default: no policies; all access flows through the service-role
-- backend (which bypasses RLS), matching 0007/0032.
alter table due_digest_log enable row level security;
alter table due_digest_log force row level security;
