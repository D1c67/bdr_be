-- 0112 - Bid File Splitter: file triage + trade-level split vocabulary.
--
-- The splitter previously classified every page of every upload and split
-- along the runs, which fragmented spec books: figures printed inside a spec
-- section look like drawings page-by-page, but they REFERENCE the drawings
-- and must stay in the manual. The pipeline now triages each FILE first (one
-- vision call over a page sample): specifications, RFP and addendum files
-- are identified and left intact - never split, never re-written - and only
-- drawing sets and mixed packages go on to the per-page pass, which now
-- splits drawings by trade (structural, plumbing, civil, fire protection,
-- low voltage, general/cover joined the original three).
--
--   bid_split_files.file_kind*      the triage verdict (what the FILE is),
--                                   overwritten by the per-page pass's
--                                   derived verdict when one runs. Null on
--                                   rows from before this migration and on
--                                   files that failed before triage.
--   bid_split_segments.is_original  the segment IS the untouched source
--                                   object (whole-file identification or a
--                                   whole-file single run): storage_path
--                                   points at the source; nothing was cut,
--                                   copied or re-written. Retry/delete paths
--                                   must never remove that object when
--                                   sweeping segments (see
--                                   services/bid_split._delete_segments).
--
-- The segment category CHECK widens with the new trade buckets. Fire alarm
-- (FA-) sheets file under fire_protection_drawings (owner's call, 2026-08-21);
-- T-/LV-series under low_voltage_drawings; cover sheets and G-series under
-- general_drawings instead of architectural.

alter table bid_split_files
  add column file_kind text
    check (file_kind in ('drawing_set', 'specifications', 'rfp', 'addendum', 'mixed', 'other')),
  add column file_kind_label text,
  add column file_kind_confidence double precision
    check (file_kind_confidence >= 0 and file_kind_confidence <= 1);

comment on column bid_split_files.file_kind is
  'Triage verdict: what the FILE is. specifications/rfp/addendum/other files are left intact (no split); drawing_set/mixed go through the per-page trade split, which then overwrites this with its derived verdict.';
comment on column bid_split_files.file_kind_label is
  'Free-text label, only meaningful while file_kind = other (e.g. "Geotechnical Report").';

alter table bid_split_segments
  add column is_original boolean not null default false;

comment on column bid_split_segments.is_original is
  'True = this row points at the UNTOUCHED SOURCE object (whole-file identification); storage_path equals the file''s source path and must never be deleted by segment sweeps.';

alter table bid_split_segments drop constraint bid_split_segments_category_check;
alter table bid_split_segments add constraint bid_split_segments_category_check
  check (category in (
    'general_drawings', 'civil_drawings', 'structural_drawings',
    'architectural_drawings', 'mechanical_drawings', 'plumbing_drawings',
    'electrical_drawings', 'fire_protection_drawings', 'low_voltage_drawings',
    'specifications', 'addenda', 'rfp', 'other'));

notify pgrst, 'reload schema';
