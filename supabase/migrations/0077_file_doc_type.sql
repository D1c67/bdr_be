-- 0077 — the plans-vs-specs axis on post-hand-off files + per-section send notes.
--
-- WHY
-- ---
-- `category` already distinguishes the INITIAL package ('drawing' vs
-- 'specification'), but everything sent AFTER the hand-off collapses into one
-- bucket: a revised sheet and a revised spec section are both category
-- 'revision', and an addendum's drawing pages and its spec pages are both
-- category 'addendum'. Estimators price plans and specs from different
-- documents and routinely hand them to different people, so "which of these is
-- a drawing change and which is a spec change" is a question the record could
-- not answer — the Revisions modal offered ONE drop zone, the package email
-- rendered ONE "Changes/Revisions" list, and the Plans & Specs Log grouped them
-- the same way.
--
-- This migration adds the missing axis:
--
--   project_files.doc_type        'drawing' | 'specification' — WHICH DOCUMENT
--                                 SET this post-hand-off file belongs to
--   file_send_batches.section_notes  per-section "what changed" note captured
--                                 at send time ("what changed in the plans" /
--                                 "…in the specs"), alongside the existing
--                                 per-file `note` and the batch-wide `message`
--
-- doc_type is a SECOND axis, deliberately NOT new categories
-- ------------------------------------------------------------------
-- 'revision_drawing' / 'revision_specification' / 'addendum_drawing' / … would
-- have been four more `file_category` labels, and every one of the ~12 category
-- sets in app/core/file_categories.py (ESTIMATOR_READ, UPDATE_CATEGORIES,
-- SENT_GATED_CATEGORIES, PACKAGE_CATEGORIES, CATEGORY_DISPLAY_ORDER, …) plus
-- every count key in the six i18n catalogs would have had to grow to match. The
-- lock rules, the note rule and the sent-gate are properties of the CATEGORY and
-- are unchanged by which document set a file belongs to — so the two facts are
-- orthogonal and stored orthogonally.
--
-- NULLABLE, and legacy rows stay NULL
-- -----------------------------------
-- Every revision/addendum uploaded before this migration has no recorded
-- document set, and there is no honest way to infer one from a filename. They
-- stay NULL and render in an untitled "Changes/Revisions" / "Addenda" group
-- exactly as they do today — the CHECK below therefore constrains the DOMAIN and
-- the CATEGORY PAIRING but never demands non-null, so the validating scan cannot
-- fail on existing data. "A revision must declare its document set" is enforced
-- where the modal that collects it lives: files.upload_file (400).
--
-- Addenda are the exception the API makes deliberately: the initial
-- "Upload plans and specs" modal also uploads addenda and does NOT ask for a
-- document set, so doc_type stays optional for category='addendum'.
--
-- Re-runnable: `if not exists` on both columns, drop-then-add on the CHECK.
-- Not DDL-locked to anything: 0075/0076 must already be applied (this file
-- names 'addendum' in a CHECK, and file_send_batches is 0076's table).

-- ── 1. project_files.doc_type ──────────────────────────────────────────────
-- TEXT + CHECK rather than a new enum type: the domain is two values that mirror
-- two EXISTING file_category labels, and reusing the label spelling means the
-- frontend resolves one set of i18n strings (filesPanel.categories.drawing /
-- .specification) for both axes instead of inventing a parallel vocabulary.
alter table project_files
  add column if not exists doc_type text;

-- Domain + pairing. Deliberately NOT `... then doc_type is not null`: see the
-- header — legacy rows are NULL and must stay valid.
alter table project_files drop constraint if exists project_files_doc_type_ck;
alter table project_files add constraint project_files_doc_type_ck check (
  doc_type is null
  or (doc_type in ('drawing', 'specification')
      and category in ('revision', 'addendum'))
);

-- The log / email / portal all group by (category, doc_type) within one project.
create index if not exists project_files_doc_type_idx
  on project_files(project_id, category, doc_type)
  where doc_type is not null;

-- ── 2. file_send_batches.section_notes ─────────────────────────────────────
-- Keyed by SECTION, e.g.
--   {"revision:drawing":"Panel schedule reissued on E-301 and E-302",
--    "revision:specification":"16123 conductor sizes updated",
--    "addendum":"…", "additional":"…"}
--
-- On the BATCH, not on the file: "what changed in the plans" is a statement
-- about THIS send, one per section, and duplicating it onto every file in the
-- section would make an edit ambiguous and a re-send wrong. The per-file `note`
-- (0048) still says what changed in each individual file — the two are
-- complementary and both travel to the estimator.
--
-- jsonb NOT NULL DEFAULT '{}' mirrors `summary` (0076): readers treat missing
-- and empty identically, so nothing has to null-check.
alter table file_send_batches
  add column if not exists section_notes jsonb not null default '{}'::jsonb;

-- DDL — without this PostgREST keeps the stale column list cached and every
-- insert naming section_notes / doc_type fails as an unknown column, which the
-- FE surfaces as the opaque "Failed to fetch" (a raw 500 loses its CORS
-- headers).
notify pgrst, 'reload schema';
