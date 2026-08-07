-- 0105: Manual quote entry FROM a vendor reply (Receive Quotes).
--
-- When extraction fails (or finds nothing) the reply notice becomes a dead end:
-- the only manual path is the generic "Add another number" form, which creates
-- a quote the system never ties back to the reply: the failed notice stays,
-- the send still looks unanswered, and the poller keeps watching it. This
-- migration is the schema half of the fix:
--
--   * project_files.rfq_message_id: quote files ingested from a reply now
--     remember WHICH reply they came from, so the review modal can show that
--     reply's own attachments (not just every quote file in the category).
--   * extraction_status gains 'manual': the terminal status written when a
--     human enters the amount for a reply by hand. Distinct from 'done' so the
--     record shows extraction did not produce the number, a person did.

alter table project_files
  add column rfq_message_id uuid references rfq_messages(id) on delete set null;
create index project_files_rfq_message_idx on project_files(rfq_message_id)
  where rfq_message_id is not null;

alter table rfq_messages drop constraint rfq_messages_extraction_status_check;
alter table rfq_messages add constraint rfq_messages_extraction_status_check
  check (extraction_status in
         ('skipped', 'pending', 'done', 'no_amount', 'failed', 'needs_review',
          'manual'));

-- Backfill what can be known: a quote row created by extraction carries both
-- the file and the message it came from. Replies that never produced a quote
-- (the failed ones this feature is for) have no recoverable link: their modal
-- falls back to the category's quote files.
update project_files pf
   set rfq_message_id = q.rfq_message_id
  from quotes q
 where q.quote_file_id = pf.id
   and q.rfq_message_id is not null
   and pf.rfq_message_id is null;
