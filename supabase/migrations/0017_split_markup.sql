-- 0017 — Split markup into separate labor and materials markup (step 8). The PM
-- marks up labor and materials independently; the legacy single markup_pct/
-- markup_amount columns are retained (nullable) for backward compatibility.

alter table markups add column labor_markup_pct        numeric(6,3);
alter table markups add column labor_markup_amount     numeric(14,2);
alter table markups add column materials_markup_pct    numeric(6,3);
alter table markups add column materials_markup_amount numeric(14,2);
