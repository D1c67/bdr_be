-- 0018 — Verify step (9) editable overrides. Exec/PM adjust the final figures at
-- verification; we store the committed snapshot here so upstream tables stay
-- untouched and the delta (what changed at verify) remains computable for stats.
alter table verifications add column labor_amount            numeric(14,2);
alter table verifications add column materials_amount        numeric(14,2);
alter table verifications add column labor_markup_amount     numeric(14,2);
alter table verifications add column materials_markup_amount numeric(14,2);
alter table verifications add column updated_at              timestamptz not null default now();
