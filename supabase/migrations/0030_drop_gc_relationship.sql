-- 0030: drop the invited_us / bidding_to distinction.
--
-- Every GC on a project is implicitly a bid candidate. The decision NOT to
-- bid to one is made at Send Out by simply never sending them a proposal,
-- and the proposal_sends record is the durable evidence of who we bid to.
-- Send Out now ends by explicit PA action ("Done sending"), not by counting
-- a declared bidding set.

-- Collapse legacy duplicates that differ only by relationship (the old
-- unique key was (project_id, gc_id, relationship)).
delete from project_gcs a
  using project_gcs b
  where a.project_id = b.project_id
    and a.gc_id = b.gc_id
    and a.id > b.id;

-- Dropping the column also drops the 3-column unique constraint built on it.
alter table project_gcs drop column relationship;
alter table project_gcs add constraint project_gcs_project_id_gc_id_key unique (project_id, gc_id);
drop type gc_relationship;
