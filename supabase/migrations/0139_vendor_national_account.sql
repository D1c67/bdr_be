-- 0139: National account vendors. A vendor company can be marked as a
-- national account (a supplier G3 buys from under a company-wide agreement).
-- The flag lives on the company, not the contact, so every rep filed under
-- that vendor carries it. Display only: it shows as a small badge on Receive
-- Quotes and Select Vendors and never changes which quote is lowest or which
-- one prices the bid.

alter table vendors
  add column if not exists is_national_account boolean not null default false;

comment on column vendors.is_national_account is
  'True when this vendor is a national account. Display only (badge on Receive Quotes / Select Vendors); no pricing effect.';

notify pgrst, 'reload schema';
