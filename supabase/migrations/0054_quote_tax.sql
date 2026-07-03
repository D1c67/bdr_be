-- Renamed from 0052_quote_tax.sql to resolve a duplicate 0052 prefix. ALREADY APPLIED TO PRODUCTION on 2026-07-03 — do not re-run against prod.
--
-- 0054 — Sales-tax attestation moves from the RFQ (category) to each quote.
--
-- 0051 recorded one tax answer per category, but vendors differ: within the
-- same category one vendor's quote can include sales tax while another's does
-- not, and comparing raw amounts would be misleading. So the attestation now
-- lives on every quote: tax_included (NULL = unanswered) plus the rate to
-- apply when it wasn't. Pricing compares and carries quotes by their
-- tax-INCLUSIVE amount — the true cost G3 incurs — and the receive-quotes
-- step can't be left until every quote in a non-General category is answered
-- (frontend hard gate, mirroring quotes_confirmed).
--
-- Written idempotently (if not exists / guarded backfill) so an accidental
-- re-run is a no-op instead of an error.

alter table quotes
  add column if not exists tax_included boolean,
  add column if not exists tax_rate     numeric(6,3) not null default 8.375;

comment on column quotes.tax_included is
  'Estimator attestation: did this vendor include sales tax in the quote? NULL = unanswered.';
comment on column quotes.tax_rate is
  'Sales-tax percent applied to the amount when tax_included is false (default 8.375).';

-- Carry any category-level answers (0051) down to that category's quotes,
-- then drop the per-RFQ columns — the quote is the single source of truth.
-- The backfill reads rfqs.tax_included, which this same migration then drops,
-- so the whole block is skipped when that column is already gone (re-run
-- case) — otherwise the UPDATE would fail on the missing column.
do $$
begin
  if exists (
    select 1
      from information_schema.columns
     where table_schema = 'public'
       and table_name   = 'rfqs'
       and column_name  = 'tax_included'
  ) then
    update quotes q
       set tax_included = r.tax_included,
           tax_rate     = r.tax_rate
      from rfqs r
     where q.rfq_id = r.id
       and r.tax_included is not null;

    alter table rfqs
      drop column tax_included,
      drop column if exists tax_rate;
  end if;
end;
$$;

notify pgrst, 'reload schema';
