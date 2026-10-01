-- 0138: Late quotes. Vendors keep quoting after the proposal has gone out
-- (send_out lane head `submitted` or `bid_outcome`, which also covers a won
-- bid handed to Project Management). Those quotes are recorded on Receive
-- Quotes for the record only: the price that went out never changes, the
-- selected quote in every category is locked, and nothing bounces the bid
-- back to Verify.
--
-- The flag is stamped once, when the quote row is created inside that window
-- (by hand on Receive Quotes, from a reply, or by the inbox poller), and is
-- never recomputed. It drives the "Received after submission" chip and keeps
-- late quotes out of the "lowest" badge and every analytics comparison.

alter table quotes
  add column if not exists received_after_submission boolean not null default false;

comment on column quotes.received_after_submission is
  'True when the quote was created after the bid was submitted (send_out head submitted/bid_outcome). Record only: never selected, excluded from lowest/analytics comparisons.';

notify pgrst, 'reload schema';
