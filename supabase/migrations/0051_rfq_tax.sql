-- 0051 — Per-RFQ sales-tax attestation (receive-quotes step).
--
-- For every (non-General) material category the estimator records whether the
-- vendor's quote already included sales tax. When it did NOT, a tax rate
-- (default the Clark County 8.375%) is applied on top of the category's
-- effective price so the materials figure carried into pricing reflects the
-- true cost G3 incurs. tax_included stays NULL until answered; the
-- receive-quotes step can't be left until every non-General category has an
-- answer (frontend hard gate, mirroring quotes_confirmed). General Material is
-- priced from the estimate, not a vendor quote, so it has no tax question.

alter table rfqs
  add column tax_included boolean,
  add column tax_rate     numeric(6,3) not null default 8.375;

comment on column rfqs.tax_included is
  'Estimator attestation: did the vendor include sales tax in the quote? NULL = unanswered.';
comment on column rfqs.tax_rate is
  'Sales-tax percent applied to the effective price when tax_included is false (default 8.375).';

notify pgrst, 'reload schema';
