-- 0015 — BOQ → RFQ extraction (Claude Opus 4.8).
--
-- The estimator's BOQ Excel is sent to Claude, which separates the materials by
-- category and returns JSON. `boq_analyses` records each extraction run (and its
-- latest, possibly PE-edited, result). On confirm we create the RFQs (one per
-- material category, merging sites) and persist the materials behind each RFQ in
-- `rfq_line_items` — the structured source for the generated RFQ Excel file.

create table boq_analyses (
  id           uuid primary key default gen_random_uuid(),
  project_id   uuid not null references projects(id) on delete cascade,
  boq_file_id  uuid references project_files(id) on delete set null,
  status       text not null default 'pending'
               check (status in ('pending', 'running', 'done', 'failed')),
  model        text,
  result_json  jsonb,          -- the {sites:[...], summary, total_material_count} payload
  error        text,
  created_by   uuid references profiles(id),
  created_at   timestamptz not null default now(),
  updated_at   timestamptz not null default now()
);
create index boq_analyses_project_idx on boq_analyses(project_id, created_at desc);
create trigger boq_analyses_updated_at before update on boq_analyses
  for each row execute function set_updated_at();

-- Confirmed materials behind each RFQ. Sites are merged into one RFQ per category
-- (matching rfqs' unique(project_id, material_category_id)); the source site is
-- preserved per line item.
create table rfq_line_items (
  id           uuid primary key default gen_random_uuid(),
  rfq_id       uuid not null references rfqs(id) on delete cascade,
  site_name    text,
  sr_no        text,
  description  text not null,
  quantity     numeric,
  unit         text,
  notes        text,
  sort_order   int not null default 0,
  created_at   timestamptz not null default now()
);
create index rfq_line_items_rfq_idx on rfq_line_items(rfq_id);

-- RLS deny-by-default (the service-role backend bypasses it); see 0007.
alter table boq_analyses   enable row level security;
alter table rfq_line_items enable row level security;
