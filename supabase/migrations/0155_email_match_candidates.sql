-- PM email ingestion: fuzzy round 2 (services/email_match.r2_score).
--
-- match_candidates holds round 2's top projects by similarity, so the triage
-- UI can show the near-misses and how close each one was (the RFP Matches
-- tab pattern). A jsonb array of
--   {project_id, number, name, similarity (0..1), kind, conflict}
-- where kind is number | name (an exact hit, similarity 1) | fuzzy, and
-- conflict is the discriminator clash that capped the score (Phase 1 vs 2)
-- or null. Written by round 2 and by the new-project rescan; null on rows
-- that never reached round 2 (conversation-map hits, manual assignments).
--
-- A round 2 assignment now records its similarity in match_confidence (1.0
-- for an exact job-number or name hit) instead of leaving it null.

alter table ingested_emails add column if not exists match_candidates jsonb;

notify pgrst, 'reload schema';
