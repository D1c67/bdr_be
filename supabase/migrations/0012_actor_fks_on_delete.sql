-- 0012 — Make user deletion safe and non-destructive to history.
-- Nullable "actor" columns → ON DELETE SET NULL (preserve the record, drop the
-- link). NOT NULL ownership columns → ON DELETE CASCADE (the row is meaningless
-- without the user). Users are normally deactivated (profiles.is_active=false)
-- rather than deleted, but deletion must never be blocked or destroy history.

-- SET NULL (nullable actor references)
alter table email_log drop constraint email_log_sent_by_fkey;
alter table email_log add constraint email_log_sent_by_fkey
  foreign key (sent_by) references profiles(id) on delete set null;

alter table stage_events drop constraint stage_events_actor_id_fkey;
alter table stage_events add constraint stage_events_actor_id_fkey
  foreign key (actor_id) references profiles(id) on delete set null;

alter table go_no_go_decisions drop constraint go_no_go_decisions_decided_by_fkey;
alter table go_no_go_decisions add constraint go_no_go_decisions_decided_by_fkey
  foreign key (decided_by) references profiles(id) on delete set null;

alter table project_files drop constraint project_files_uploaded_by_fkey;
alter table project_files add constraint project_files_uploaded_by_fkey
  foreign key (uploaded_by) references profiles(id) on delete set null;

alter table projects drop constraint projects_created_by_fkey;
alter table projects add constraint projects_created_by_fkey
  foreign key (created_by) references profiles(id) on delete set null;

alter table rfqs drop constraint rfqs_created_by_fkey;
alter table rfqs add constraint rfqs_created_by_fkey
  foreign key (created_by) references profiles(id) on delete set null;

alter table labor_reviews drop constraint labor_reviews_reviewed_by_fkey;
alter table labor_reviews add constraint labor_reviews_reviewed_by_fkey
  foreign key (reviewed_by) references profiles(id) on delete set null;

alter table markups drop constraint markups_set_by_fkey;
alter table markups add constraint markups_set_by_fkey
  foreign key (set_by) references profiles(id) on delete set null;

alter table verifications drop constraint verifications_verified_by_fkey;
alter table verifications add constraint verifications_verified_by_fkey
  foreign key (verified_by) references profiles(id) on delete set null;

alter table estimator_assignments drop constraint estimator_assignments_assigned_by_fkey;
alter table estimator_assignments add constraint estimator_assignments_assigned_by_fkey
  foreign key (assigned_by) references profiles(id) on delete set null;

-- CASCADE (NOT NULL ownership)
alter table go_no_go_votes drop constraint go_no_go_votes_voter_id_fkey;
alter table go_no_go_votes add constraint go_no_go_votes_voter_id_fkey
  foreign key (voter_id) references profiles(id) on delete cascade;

alter table estimator_assignments drop constraint estimator_assignments_estimator_id_fkey;
alter table estimator_assignments add constraint estimator_assignments_estimator_id_fkey
  foreign key (estimator_id) references profiles(id) on delete cascade;
