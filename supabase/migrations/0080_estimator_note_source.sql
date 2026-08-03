-- 0080 — Where an estimator note came from.
--
-- Until now every row in estimator_notes was hand-written in the Project notes
-- panel (0025). The send flows also collect a free-text "Message to the
-- estimator", which went only into the outbound email and onto the send batch
-- (file_send_batches.message, 0076) — so a message left at send time never
-- appeared in the thread and read as lost. Those messages are now mirrored into
-- this table as a note authored by the sender.
--
-- 0025's "nothing sensitive is ever auto-placed here" still holds: the ONLY
-- text mirrored is the batch-wide message, which the author wrote for these
-- same estimators and which is already delivered to them verbatim at the top of
-- the package/update email. Per-file notes and the per-section "what changed"
-- notes are NOT mirrored — they belong to the file rows and the Plans & Specs
-- Log.
--
-- null  = written by a human in the notes panel (every pre-existing row)
-- others = mirrored from the send of that kind, so the UI can label it.

alter table estimator_notes add column source text;

alter table estimator_notes
  add constraint estimator_notes_source_chk
  check (source is null or source in ('package_send', 'update_send'));

-- Backfill: messages left on sends that already went out, so the thread isn't
-- missing the history the log already has. Authored by whoever sent the batch
-- and dated to the send, not to this migration. The not-exists guard keys on
-- (project, body, sent_at) so re-running adds nothing.
insert into estimator_notes (project_id, author_id, body, created_at, source)
select b.project_id, b.sent_by, b.message, b.sent_at,
       case when b.kind = 'revision' then 'update_send' else 'package_send' end
from file_send_batches b
where b.message is not null
  and btrim(b.message) <> ''
  and b.sent_at is not null
  and not exists (
    select 1 from estimator_notes n
    where n.project_id = b.project_id
      and n.body = b.message
      and n.created_at = b.sent_at
  );
