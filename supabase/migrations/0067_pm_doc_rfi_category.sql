-- 0067 — Give RFI attachments a home in the unified Documents hub.
--
-- Files attached to an RFI are uploaded into pm_documents (the hub's only writable
-- store) and must land somewhere findable. Filing them under 'correspondence' would
-- bury them among unrelated mail, so RFIs get their own category and, in
-- app/services/pm_folders.py, their own business folder.
--
-- Additive only (new enum label): ALTER TYPE ... ADD VALUE is non-destructive and
-- safe on existing rows. Kept in its own migration because a new enum value may not
-- be referenced in the same transaction that adds it — see 0065.

alter type pm_doc_category add value if not exists 'rfi';

notify pgrst, 'reload schema';
