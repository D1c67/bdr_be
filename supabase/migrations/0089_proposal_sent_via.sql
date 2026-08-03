-- Proposals can be "submitted" without BDR emailing anything: some GCs take
-- bids only through third-party applications (Procore, BuildingConnected,
-- a portal upload, ...). The PA still needs the durable per-GC record of
-- what was bid and when, so those rows reach status 'sent' via an explicit
-- "Mark as submitted" instead of an email send. sent_via records which path
-- a sent row took; rows marked externally have no email_log_id on purpose.
alter table proposal_sends
  add column sent_via text not null default 'email'
  check (sent_via in ('email', 'external'));

comment on column proposal_sends.sent_via is
  'How a sent proposal reached the GC: email = BDR emailed it; external = submitted through a third-party application and recorded by hand (no email, email_log_id stays null).';
