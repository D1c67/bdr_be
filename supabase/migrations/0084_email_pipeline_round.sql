-- Email ingestion: record WHICH identification round decided each email.
--
-- Neither existing column answers "what round was this processed on":
--   * `status` names the step about to RUN, and every failure collapses it to
--     'failed' — so once a row is terminal the step it died on is gone.
--   * `matched_by` names the kind of evidence, not the round: 'conversation'
--     is written by both the live R1 round AND the manual retro-assign, and
--     'subject'/'llm' by both the live R2/R3 rounds AND the new-project rescan
--     of the Unknown pool.
--
-- `pipeline_round` records the step itself, for both matches and dead ends:
--   fetch  -- died in the body/attachment fetch step; never reached a round
--   r1     -- conversation-map / submittal-send round
--   r2     -- deterministic subject round
--   r3     -- LLM round; also where an unmatched email lands in the Unknown pool
--   manual -- a person assigned it
--   retro  -- filed as a conversation sibling of a manual assignment
--   rescan -- filed by the Unknown-pool rescan run when a project was created
--
-- Nullable on purpose: a row that has not reached a terminal state yet has no
-- deciding round (its current round is readable from `status`), and the
-- backfill below leaves null wherever the old columns can't prove one.

alter table ingested_emails
  add column pipeline_round text
    check (pipeline_round in ('fetch','r1','r2','r3','manual','retro','rescan'));

-- Backfill, narrowest claim first; each statement only touches rows the
-- previous ones left null. Historical rows can't distinguish r1 from retro or
-- r2/r3 from rescan (that's exactly the ambiguity this column removes going
-- forward), so they get the live-round reading — the common case by far.
update ingested_emails set pipeline_round = 'manual' where matched_by = 'manual';
update ingested_emails set pipeline_round = 'r1'
  where pipeline_round is null and matched_by = 'conversation';
update ingested_emails set pipeline_round = 'r2'
  where pipeline_round is null and matched_by = 'subject';
update ingested_emails set pipeline_round = 'r3'
  where pipeline_round is null and matched_by = 'llm';
-- Unassigned but processed = the pipeline ran out of rounds at R3.
update ingested_emails set pipeline_round = 'r3'
  where pipeline_round is null and project_id is null and status = 'processed';
-- Failed with no body ever fetched = it died in the fetch step (the same
-- heuristic email_ingest._needs_content_fetch uses).
update ingested_emails set pipeline_round = 'fetch'
  where pipeline_round is null and status = 'failed' and body_text is null;
-- Failed WITH a body = it died in an identification round, and in practice
-- that means R3: R1/R2 are pure Supabase reads, and a Supabase outage fails
-- process_pending's own worklist query first (the sweep aborts) rather than
-- burning attempts row by row. Only R3 makes a per-row network call that can
-- retry to exhaustion. Confirmed against the dev pool, where every such row
-- carries an OpenAI error.
update ingested_emails set pipeline_round = 'r3'
  where pipeline_round is null and status = 'failed';

notify pgrst, 'reload schema';
