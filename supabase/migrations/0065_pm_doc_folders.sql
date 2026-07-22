-- 0065 — Extend pm_doc_category so the unified Documents hub can file uploads into
-- the business folders that previously only existed on the bidding side. The hub
-- (app/services/pm_folders.py) maps every source category — pm_documents,
-- project_files, and certified-payroll files — into a flat set of business folders
-- (Plans, Specs, Quotes, Estimates, Billing, …). Bidding/CP files are read-only
-- mirrors; the writable store stays pm_documents, so these four folders need a
-- pm_doc_category to receive uploads.
--
-- Additive only (new enum labels): ALTER TYPE ... ADD VALUE is non-destructive and
-- safe on existing rows. Kept in its own migration because a new enum value may not
-- be referenced in the same transaction that adds it.

alter type pm_doc_category add value if not exists 'specification';
alter type pm_doc_category add value if not exists 'quote';
alter type pm_doc_category add value if not exists 'estimate';
alter type pm_doc_category add value if not exists 'billing';

notify pgrst, 'reload schema';
