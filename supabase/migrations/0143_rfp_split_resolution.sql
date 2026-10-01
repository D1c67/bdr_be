-- 0143: RFP split failure resolution (docs/RFP_SPLIT.md section 10). Apply after 0142.
--
-- A split that cannot finish no longer blocks the project: the project is
-- created with every document whole and flagged "the Bid File Splitter
-- failed". The failure itself stays on the harvest (rfp_harvests.split_status
-- / split_error, 0132), read live by the flag. What a person decided about it
-- lives here:
--
--   split_resolution   null (open) | 'outside' ("I'll split it outside the app")
--   split_resolved_by  who marked it
--   split_resolved_at  when
--
-- The in-app "Run the splitter" needs no column: it re-queues the failed files
-- (or stages a fresh job linked to the project) and the harvest's split status
-- follows the run. Undo clears all three columns.
--
-- Every statement is idempotent.

alter table rfp_created_projects
  add column if not exists split_resolution  text,
  add column if not exists split_resolved_by uuid references profiles(id) on delete set null,
  add column if not exists split_resolved_at timestamptz;

alter table rfp_created_projects drop constraint if exists rfp_created_projects_split_resolution_check;
alter table rfp_created_projects add constraint rfp_created_projects_split_resolution_check
  check (split_resolution is null or split_resolution in ('outside'));

comment on column rfp_created_projects.split_resolution is
  'How a person resolved a failed or partial Bid File Splitter run: null = open, outside = split outside the app (docs/RFP_SPLIT.md 10). The failure itself is rfp_harvests.split_status / split_error.';
comment on column rfp_created_projects.split_resolved_by is
  'Who marked the split resolution (null when open).';
comment on column rfp_created_projects.split_resolved_at is
  'When the split resolution was marked (null when open).';

notify pgrst, 'reload schema';
