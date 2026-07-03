-- 0048 — Changes/Revisions & Additional files (post-hand-off file updates).
--
-- Once the estimator hand-off has begun (an assignment exists or the package
-- was already emailed), the initial "Drawings & plans" / "Specifications"
-- blocks lock. Anything arriving after that is uploaded under two new
-- categories, each requiring a per-file note:
--   revision   — "Changes/Revisions": plan changes, addenda, spec revisions.
--   additional — "Additional files": supplementary documents.
-- Updates are emailed on demand to every active assignee (send-file-updates),
-- and a later-assigned estimator's welcome package includes them, grouped so
-- initial files stay distinguishable from updates.

-- New enum labels. PG12+ allows ADD VALUE in a transaction as long as the new
-- label is not used later in the same transaction — this migration never
-- references 'revision' or 'additional'.
alter type file_category add value if not exists 'revision';
alter type file_category add value if not exists 'additional';

alter table project_files
  -- Required for revision/additional (enforced in files.py); editable even
  -- after the file is sent — the file itself becomes immutable, its note not.
  add column if not exists note text,
  -- Stamped when the file was emailed to the assigned estimators. Sent files
  -- are undeletable (they're what the estimator priced off), and the external
  -- estimator only ever sees updates that were actually sent.
  add column if not exists sent_to_estimators_at timestamptz;

notify pgrst, 'reload schema';
