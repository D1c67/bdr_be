-- EV Chargers material category. EV charging equipment is quoted by its own set
-- of vendors (ABB, ChargePoint, EVBox) and had no bucket of its own, so those
-- contacts had nowhere to sit. Slots at 56, between Trenching and the markups,
-- so the material trades stay contiguous and nothing else renumbers.
-- Fixed id so dev, staging and prod all agree on it.
insert into material_categories (id, name, kind, sort_order, is_general)
select '6fa47c60-c246-4c0a-8520-cc28ed06e7cb', 'EV Chargers', 'material', 56, false
where not exists (
  select 1 from material_categories where lower(name) = 'ev chargers'
);
