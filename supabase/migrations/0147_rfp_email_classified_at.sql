-- 0147: RFP email intake, classify budget counted by classification time
-- (docs/RFP_EMAIL_INGESTION.md 3.5, security review 2026-09-30).
--
-- The per-sender-domain classify budget counted the calls spent in the last
-- day by the rows' received_at. A backlog received more than a day ago
-- therefore counted as zero once it was finally classified, so the daily cap
-- only applied to fresh mail. The classify step now stamps the moment the
-- model answered and the budget counts by that.
--
-- Applied to the DEV database only; production waits for the release.

alter table rfp_emails
  add column if not exists classified_at timestamptz;

comment on column rfp_emails.classified_at is
  'When the classify model answered for this row (llm_model set). The per-sender classify budget counts calls by this, not by received_at.';

create index if not exists rfp_emails_classified_at_idx
  on rfp_emails (classified_at)
  where classified_at is not null;

notify pgrst, 'reload schema';
