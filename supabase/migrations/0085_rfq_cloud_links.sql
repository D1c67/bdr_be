-- Inbound vendor replies: ingest cloud-share links (OneDrive/SharePoint,
-- Google Drive, Dropbox, Box) as quote files.
--
--   * extraction_status gains 'needs_review' — the poller has written this
--     value since the implausible-amount guard landed, but the original check
--     constraint never allowed it, so that status update always failed.
--   * cloud_link_count records how many share links were detected in the
--     reply, so the UI can offer a "fetch linked files" retry on messages
--     whose links could not be downloaded automatically.

alter table rfq_messages drop constraint rfq_messages_extraction_status_check;
alter table rfq_messages add constraint rfq_messages_extraction_status_check
  check (extraction_status in
         ('skipped', 'pending', 'done', 'no_amount', 'failed', 'needs_review'));

alter table rfq_messages add column cloud_link_count int not null default 0;

-- Backfill: flag pre-existing replies whose stored body mentions a supported
-- share host so their retry button appears. The exact count doesn't matter —
-- the refetch endpoint re-parses the body.
update rfq_messages
   set cloud_link_count = 1
 where cloud_link_count = 0
   and body ~* '(sharepoint\.com|1drv\.ms|onedrive\.live\.com|drive\.google\.com|dropbox\.com|app\.box\.com)';
