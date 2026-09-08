-- 0115 - Bid File Splitter: per-segment edit marker + the score it replaced.
--
-- Editing a file's splits used to null confidence on EVERY replacement row,
-- so one correction wiped the model's scores off rows the user never touched.
-- Untouched rows (same category, label and range) now keep their score, and
-- rows the user did change are marked instead of silently unscored:
--
--   bid_split_segments.user_edited       true on rows a user created or moved
--                                        (Edit splits, or the collapse-to-
--                                        intact file-kind correction). The FE
--                                        shows "Edited" in the confidence
--                                        column instead of a bare dash.
--   bid_split_segments.prior_confidence  the model's score on the old segment
--                                        this edit replaced (greatest page
--                                        overlap), surfaced on hover so the
--                                        original verdict stays inspectable.
--                                        Null when the predecessor had no
--                                        score. Meaningful only while
--                                        user_edited is true.
--
-- A re-run replaces the rows outright, so both columns reset via defaults.

alter table bid_split_segments
  add column user_edited boolean not null default false,
  add column prior_confidence double precision
    check (prior_confidence >= 0 and prior_confidence <= 1);

comment on column bid_split_segments.user_edited is
  'True on rows a user created or moved via a correction; the FE renders Edited in the confidence column.';
comment on column bid_split_segments.prior_confidence is
  'Model confidence of the old segment this user edit replaced (greatest page overlap); null when it had no score.';

notify pgrst, 'reload schema';
