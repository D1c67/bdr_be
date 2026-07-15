-- 0062 — PM materials: the project's material list on the PM side, broken out
-- by material category (the same axis the bidding RFQs use). Bid-origin
-- projects are seeded at won→Precon activation with exactly what the BOQ
-- extraction returned (preferring the confirmed rfq_line_items, falling back
-- to the latest done extraction when the bid never confirmed); direct
-- (pm_only) projects start empty. Either way users add rows by hand
-- afterwards — `source` distinguishes the two, and only 'manual' rows count
-- as PM work for the retraction probe (services/pm.pm_activity_exists).

create table pm_materials (
  id                    uuid primary key default gen_random_uuid(),
  project_id            uuid not null references projects(id) on delete cascade,
  -- Nullable: extraction groups that never matched a category stay
  -- uncategorized; category_label preserves the group name for display.
  material_category_id  uuid references material_categories(id) on delete set null,
  category_label        text,
  site_name             text,
  description           text not null,
  quantity              numeric,
  unit                  text,
  notes                 text,
  source                text not null default 'manual' check (source in ('boq', 'manual')),
  sort_order            int not null default 0,
  created_by            uuid references profiles(id) on delete set null,
  created_at            timestamptz not null default now(),
  updated_at            timestamptz not null default now()
);
create index pm_materials_project_idx  on pm_materials(project_id);
create index pm_materials_category_idx on pm_materials(project_id, material_category_id);
create trigger pm_materials_updated_at before update on pm_materials
  for each row execute function set_updated_at();

alter table pm_materials enable row level security;
alter table pm_materials force  row level security;

notify pgrst, 'reload schema';
