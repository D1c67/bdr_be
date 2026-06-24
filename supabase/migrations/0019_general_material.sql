-- 0019 — General Material is priced from the estimate, not vendor quotes.
--
-- For the General Material category we don't gather RFQ quotes. Instead a Claude
-- Sonnet 4.6 call reads the "wiring" material cost from the estimate workbook's
-- "Bid Recap and summary" sheet (the "bid recap" table). That number stands in
-- for the category's quotes inside the materials price total (pricing step 8/9).
--
-- `is_general` flags the category so the pricing code can find it even if an IT
-- admin renames it. `general_material_estimates` holds the per-project number
-- (extracted or manually entered) plus the extraction run's status.

alter table material_categories add column is_general boolean not null default false;
update material_categories set is_general = true where lower(name) = 'general material';

create table general_material_estimates (
  project_id        uuid primary key references projects(id) on delete cascade,
  amount            numeric(14,2),                 -- null until found / entered
  source            text not null default 'extracted'
                    check (source in ('extracted', 'manual')),
  status            text not null default 'pending'
                    check (status in ('pending', 'running', 'done', 'not_found', 'failed')),
  model             text,
  estimate_file_id  uuid references project_files(id) on delete set null,
  raw_extraction    jsonb,                         -- the model's full JSON reply
  error             text,
  set_by            uuid references profiles(id) on delete set null,
  created_at        timestamptz not null default now(),
  updated_at        timestamptz not null default now()
);
create trigger general_material_estimates_updated_at before update
  on general_material_estimates for each row execute function set_updated_at();

-- RLS deny-by-default (the service-role backend bypasses it); see 0007.
alter table general_material_estimates enable row level security;
