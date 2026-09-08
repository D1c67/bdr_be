-- 0114 - Bid File Splitter: why the confidence is what it is.
--
-- The splitter already scored every verdict (a triage verdict per FILE, a
-- category per PAGE, averaged into a per-segment score), but the number
-- arrived bare: an estimator seeing "63%" had no way to tell whether the
-- model could not read a title block, saw a sheet that could belong to two
-- trades, or was averaging a segment whose pages disagreed. The model is now
-- asked for the reasoning behind its own score, and the pipeline composes a
-- segment-level explanation from it; the FE shows it on hover over the
-- confidence cell.
--
--   bid_split_segments.confidence_reason   why THIS segment scored what it
--                                          did: the average and the spread,
--                                          the model's own words on its
--                                          weakest (and, when they disagree,
--                                          strongest) page, and a note when
--                                          island repair folded pages in.
--                                          Null wherever confidence is null
--                                          (user corrections are not model
--                                          estimates) and on rows from before
--                                          this migration.
--   bid_split_files.file_kind_confidence_reason
--                                          the same for the file-level
--                                          verdict: the triage reason while
--                                          triage is the standing answer,
--                                          replaced by the per-page summary
--                                          once the page pass overwrites the
--                                          verdict (the two must never drift
--                                          apart - the reason is written in
--                                          the same update as the score).

alter table bid_split_segments add column confidence_reason text;
alter table bid_split_files add column file_kind_confidence_reason text;

comment on column bid_split_segments.confidence_reason is
  'Plain-language reasoning behind confidence, shown on hover in the FE. Always null when confidence is null.';
comment on column bid_split_files.file_kind_confidence_reason is
  'Plain-language reasoning behind file_kind_confidence; rewritten in lockstep with it (triage verdict, then the per-page derived verdict).';

notify pgrst, 'reload schema';
