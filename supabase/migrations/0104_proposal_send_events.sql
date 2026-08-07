-- 0104 — Proposal re-sends and late-GC sends.
--
-- Two gaps this closes:
--
--   1. A proposal could only ever be emailed once. proposal_sends is unique
--      (project_id, gc_id) and every layer skips a row at status 'sent', so a
--      bounced address or a GC that lost the mail had no recovery path.
--   2. A GC added after "Done sending" could never receive anything: adding is
--      unguarded by stage, but generate/send are gated on the send_out lane
--      head, which has already moved to 'submitted' by then.
--
-- proposal_sends stays exactly what it was: ONE row per (project, GC) recording
-- whether that GC was bid and with which document — the "absence of a sent row
-- IS the data" invariant the whole Send Out stage is built on. It is NOT
-- widened to many rows, because a re-send is not a second bid.
--
-- Instead this table records every individual transmission of that one bid.
-- The first one (kind 'initial') mirrors what proposal_sends already stamped;
-- each later one (kind 'resend') re-emails the SAME stored document — re-sends
-- never regenerate, so a GC can never hold two documents with different numbers
-- under one bid. via='external' marks a submission recorded through a
-- third-party application (no email was sent, so there is no email_log row).
create table proposal_send_events (
  id                uuid primary key default gen_random_uuid(),
  proposal_send_id  uuid not null references proposal_sends(id) on delete cascade,
  project_id        uuid not null references projects(id) on delete cascade,
  -- RESTRICT to match proposal_sends.gc_id: transmission history is legal
  -- evidence of what we sent and to whom, so a GC delete must be blocked.
  gc_id             uuid not null references general_contractors(id) on delete restrict,
  kind              text not null check (kind in ('initial', 'resend')),
  via               text not null default 'email' check (via in ('email', 'external')),
  -- The document this transmission carried. SET NULL so history survives a file
  -- row deletion, exactly like project_files.gc_id in 0024.
  file_id           uuid references project_files(id) on delete set null,
  -- Recipient list string, same ', ' join as proposal_sends.gc_email and
  -- email_log.to_addrs (join_recipients) so the three stay comparable.
  recipients        text,
  status            text not null default 'sending'
                    check (status in ('sending', 'sent', 'failed')),
  error             text,
  email_log_id      uuid references email_log(id) on delete set null,
  sent_at           timestamptz,
  sent_by           uuid references profiles(id) on delete set null,
  created_at        timestamptz not null default now(),
  updated_at        timestamptz not null default now()
);
create index proposal_send_events_send_idx on proposal_send_events(proposal_send_id, created_at);
create index proposal_send_events_project_idx on proposal_send_events(project_id);
create index proposal_send_events_gc_idx on proposal_send_events(gc_id);
create index proposal_send_events_file_idx on proposal_send_events(file_id);
create index proposal_send_events_email_log_idx on proposal_send_events(email_log_id);
create index proposal_send_events_sent_by_idx on proposal_send_events(sent_by);
create trigger proposal_send_events_updated_at before update on proposal_send_events
  for each row execute function set_updated_at();

-- The re-send lock. proposal_sends.status stays 'sent' throughout a re-send
-- (the bid record does not change), so it cannot serve as the in-flight claim
-- the way it does for a first send. This partial unique index is that claim:
-- inserting the 'sending' event IS acquiring the lock, and a concurrent
-- re-send for the same GC fails on the constraint instead of double-emailing.
create unique index proposal_send_events_one_inflight
  on proposal_send_events(proposal_send_id)
  where status = 'sending';

-- Backfill the initial transmission for every bid already submitted, so the
-- history is complete from the first re-send onward rather than starting with
-- an unexplained gap. Externally-marked rows carry via='external' and keep
-- their null recipients/email_log — their absence is the record (0089).
insert into proposal_send_events (
  proposal_send_id, project_id, gc_id, kind, via, file_id,
  recipients, status, email_log_id, sent_at, sent_by, created_at
)
select
  ps.id, ps.project_id, ps.gc_id, 'initial',
  coalesce(ps.sent_via, 'email'), ps.file_id,
  ps.gc_email, 'sent', ps.email_log_id, ps.sent_at, ps.sent_by,
  coalesce(ps.sent_at, ps.created_at)
from proposal_sends ps
where ps.status = 'sent';

-- RLS deny-by-default + FORCE (0055 / 0093 posture): all access is enforced in
-- the FastAPI layer and the service-role key bypasses it.
alter table proposal_send_events enable row level security;
alter table proposal_send_events force row level security;
