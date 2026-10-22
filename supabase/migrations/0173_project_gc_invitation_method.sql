-- 0173 - How each GC on a project invited us. The project page lists every
-- GC with its invitation method(s): the ones derived from RFP ingestion
-- (the RFP that created the project, the live merges in
-- rfp_project_matches, the BuildingConnected invitations) plus the one a
-- person set by hand on the GCs box, stored here. NULL = nobody set one; a
-- GC with no derived method and no set one reads as "By hand".
--
-- The allowed keys are validated in the API (PATCH /projects/{id}/gcs/{gc_id})
-- against the same list the frontend select offers. The CHECK below only
-- guards the shape (a short lowercase key), so a new invitation method (0127,
-- 0129 and 0148 each added one) never needs a migration here.
alter table project_gcs add column if not exists invitation_method text;

alter table project_gcs drop constraint if exists project_gcs_invitation_method_shape;
alter table project_gcs add constraint project_gcs_invitation_method_shape
  check (invitation_method is null or invitation_method ~ '^[a-z][a-z0-9_]{0,39}$');

comment on column project_gcs.invitation_method is
  'The invitation method a person set for this GC on this project (an rfp_emails.invitation_method key, a portal key such as buildingconnected or ngem, or other). NULL = not set: the page shows the methods derived from RFP ingestion, or "By hand" when there are none. Validated against an allow-list in the API; the CHECK only guards the shape.';

notify pgrst, 'reload schema';
