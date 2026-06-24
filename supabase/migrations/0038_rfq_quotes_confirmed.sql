-- 0038 — Per-RFQ "quotes complete" attestation (receive-quotes step).
--
-- The PE confirms, per material category, that the vendor quoted the entire
-- RFQ and didn't miss a material. The receive-quotes step can't be left until
-- every (non-General) category is confirmed — a frontend hard gate; the backend
-- only records the attestation (who/when), mirroring the custom_set_* pattern.

alter table rfqs
  add column quotes_confirmed     boolean not null default false,
  add column quotes_confirmed_by  uuid references profiles(id) on delete set null,
  add column quotes_confirmed_at  timestamptz;
