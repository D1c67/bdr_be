-- 0037 — Add the 'specification' file category. Specifications (project spec
-- books / divisions) are uploaded alongside electrical drawings at intake and
-- on the To Estimator step, but — unlike drawings — they are optional and are
-- NOT emailed to the estimator. The estimator whitelist (ESTIMATOR_READ /
-- ESTIMATOR_WRITE in files.py) deliberately excludes 'specification', so the
-- external estimator never sees them.

-- New enum label. PG12+ allows ADD VALUE in a transaction as long as the new
-- label is not used later in the same transaction — this migration never
-- references 'specification'.
alter type file_category add value if not exists 'specification';
