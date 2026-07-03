-- 0053 — Sales-tax attestation for General Material (receive-quotes step).
--
-- General Material is priced from the estimate workbook (or entered by hand),
-- not vendor quotes — but the figure feeds the materials total all the same,
-- so it needs the same tax question the vendor quotes got in 0052: does the
-- number already include sales tax? NULL = unanswered (gates leaving the step
-- once an amount exists); when false, tax_rate is applied on top so the
-- materials cost is the true cost G3 incurs.

alter table general_material_estimates
  add column tax_included boolean,
  add column tax_rate     numeric(6,3) not null default 8.375;

comment on column general_material_estimates.tax_included is
  'Estimator attestation: does the general-material figure include sales tax? NULL = unanswered.';
comment on column general_material_estimates.tax_rate is
  'Sales-tax percent applied to the amount when tax_included is false (default 8.375).';

notify pgrst, 'reload schema';
