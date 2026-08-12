-- 0108: Vendor nudge reminders on RFQ threads.
--
-- DEPLOY STEP - REQUIRED AFTER APPLYING (one-time, per environment):
--   The inbox poller persists its Graph delta cursor, and Graph delta
--   pagination pins the ORIGINAL $select list the cursor was created with, so
--   internetMessageId (newly added to graph_inbox._DELTA_SELECT for the
--   ingestion guard below) stays ABSENT from poller messages until the cursor
--   resets - the guard is inert on that path until then (the From == bids@
--   sender check still protects in practice). Clear the RFQ poller's cursor
--   so the next poll re-syncs with the new $select. The row id is built by
--   rfq_inbox.poll_once as f"inbox:{settings.ms_sender}" (the MS_SENDER env
--   var, default bids@g3electrical.com):
--
--     update graph_sync_state set delta_link = null
--      where id = 'inbox:bids@g3electrical.com';
--
-- A nudge is a follow-up email sent as a reply-all on an rfq_send's existing
-- Graph conversation ("do you have a quote for X yet?"). One row per nudge
-- email. The row is written at status 'pending' BEFORE the Graph draft is
-- sent: the inbox poller refuses any inbound message whose internetMessageId
-- matches a row here, and our own nudge can land back in the bids@ inbox (the
-- standing desk CC rides on every RFQ thread) moments after the send - so the
-- claim must exist first or the nudge gets ingested as a vendor quote reply.
create table rfq_nudges (
  id                   uuid primary key default gen_random_uuid(),
  rfq_send_id          uuid not null references rfq_sends(id) on delete cascade,
  sent_by              uuid references profiles(id),
  -- The FINAL plain text that went out (tokens already substituted by the
  -- frontend), wrapped in the branded HTML shell at send time like every
  -- outbound vendor email.
  message              text not null,
  status               text not null default 'pending'
                       check (status in ('pending', 'sent', 'failed')),
  error                text,
  graph_message_id     text,            -- immutable id of the reply-all draft
  internet_message_id  text,            -- the ingestion guard's matching key
  email_log_id         uuid references email_log(id) on delete set null,
  created_at           timestamptz not null default now(),
  sent_at              timestamptz
);
create index rfq_nudges_send_idx on rfq_nudges(rfq_send_id);
-- The ingestion guard probes this once per matched RFQ-thread message
-- (rfq_inbox._ingest_message), so the lookup must be a single index hit.
create index rfq_nudges_internet_message_idx on rfq_nudges(internet_message_id)
  where internet_message_id is not null;
-- One in-flight nudge per send. The pending claim row IS the lock, the same
-- insert-as-lock shape as 0104's proposal_send_events_one_inflight: two
-- concurrent nudge batches hitting the same rfq_send collide here on the
-- second insert instead of double-emailing the vendor. _nudge_one reclaims a
-- stale pending row (a crashed run's leftover) after 10 minutes.
create unique index rfq_nudges_one_pending_per_send
  on rfq_nudges(rfq_send_id)
  where status = 'pending';

-- RLS deny-by-default + FORCE (0055 / 0093 posture): all access is enforced in
-- the FastAPI layer and the service-role key bypasses it.
alter table rfq_nudges enable row level security;
alter table rfq_nudges force row level security;

notify pgrst, 'reload schema';
