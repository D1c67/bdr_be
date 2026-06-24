-- 0016 — Explicit labor number for step 7. Adds a defined dollar amount plus an
-- optional named breakdown (custom fields the PM can add and sum into the total).

alter table labor_reviews add column labor_amount    numeric(14,2);
alter table labor_reviews add column labor_breakdown jsonb;  -- [{name, amount}, ...]
