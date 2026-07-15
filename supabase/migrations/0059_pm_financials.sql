-- 0059 — PM financials: change orders + full SOV (schedule-of-values) billing.
-- The current contract value is NEVER stored: original (pm_details, 0057) + sum of
-- approved change_orders.amount. Pay-application totals are likewise computed on
-- read from pay_app_lines (the G702/G703 model: previous / this period / stored
-- materials per SOV line), so the ledger can't drift from its lines.

create type change_order_status as enum ('draft', 'submitted', 'approved', 'rejected');
create type pay_app_status      as enum ('draft', 'submitted', 'approved', 'paid', 'rejected');

create table change_orders (
  id                 uuid primary key default gen_random_uuid(),
  project_id         uuid not null references projects(id) on delete cascade,
  co_number          text not null,           -- our per-project CO number
  title              text not null,
  description        text,
  status             change_order_status not null default 'draft',
  amount             numeric(14,2) not null default 0,  -- signed: deductive COs are negative
  days_added         int,                     -- schedule impact (may be negative)
  customer_reference text,                    -- the customer's CO / PCO number
  submitted_at       date,
  approved_at        date,
  created_by         uuid references profiles(id) on delete set null,
  created_at         timestamptz not null default now(),
  updated_at         timestamptz not null default now(),
  unique (project_id, co_number)
);
create index if not exists change_orders_project_idx on change_orders(project_id);
create trigger change_orders_updated_at before update on change_orders
  for each row execute function set_updated_at();

-- The project's schedule of values: the billing breakdown the pay apps draw
-- against. Approved change orders typically add lines (change_order_id tracks
-- the provenance; SET NULL keeps the line if the CO is later deleted).
create table sov_lines (
  id              uuid primary key default gen_random_uuid(),
  project_id      uuid not null references projects(id) on delete cascade,
  line_number     text not null,
  description     text not null,
  scheduled_value numeric(14,2) not null,
  change_order_id uuid references change_orders(id) on delete set null,
  sort_order      int not null default 0,
  created_by      uuid references profiles(id) on delete set null,
  created_at      timestamptz not null default now(),
  updated_at      timestamptz not null default now(),
  unique (project_id, line_number)
);
create index if not exists sov_lines_project_idx on sov_lines(project_id, sort_order);
create trigger sov_lines_updated_at before update on sov_lines
  for each row execute function set_updated_at();

create table pay_applications (
  id                uuid primary key default gen_random_uuid(),
  project_id        uuid not null references projects(id) on delete cascade,
  app_number        int not null,             -- server-assigned: max+1 per project
  period_start      date,
  period_end        date not null,
  status            pay_app_status not null default 'draft',
  retainage_percent numeric(5,2) check (retainage_percent >= 0 and retainage_percent <= 100),
  submitted_at      date,
  approved_at       date,
  paid_at           date,
  notes             text,
  created_by        uuid references profiles(id) on delete set null,
  created_at        timestamptz not null default now(),
  updated_at        timestamptz not null default now(),
  unique (project_id, app_number)
);
create index if not exists pay_applications_project_idx on pay_applications(project_id);
create trigger pay_applications_updated_at before update on pay_applications
  for each row execute function set_updated_at();

-- One row per (pay app × SOV line) — the G703 worksheet. previous_completed is a
-- server-side snapshot of everything billed on prior apps at creation time; the
-- user enters this_period and stored_materials. sov_line_id is DEFERRABLE
-- INITIALLY DEFERRED (checked at commit), NOT restrict: a direct delete of a
-- billed line still fails (its referencing rows survive to commit), but a
-- whole-project cascade delete succeeds because the sibling pay_applications
-- cascade removes these rows in the same transaction — RESTRICT (and even a
-- non-deferred NO ACTION) fires mid-cascade and wedges project deletion.
create table pay_app_lines (
  id                 uuid primary key default gen_random_uuid(),
  pay_app_id         uuid not null references pay_applications(id) on delete cascade,
  sov_line_id        uuid not null references sov_lines(id) deferrable initially deferred,
  previous_completed numeric(14,2) not null default 0,
  this_period        numeric(14,2) not null default 0,
  stored_materials   numeric(14,2) not null default 0,
  created_at         timestamptz not null default now(),
  updated_at         timestamptz not null default now(),
  unique (pay_app_id, sov_line_id)
);
create index if not exists pay_app_lines_pay_app_idx on pay_app_lines(pay_app_id);
create index if not exists pay_app_lines_sov_line_idx on pay_app_lines(sov_line_id);
create trigger pay_app_lines_updated_at before update on pay_app_lines
  for each row execute function set_updated_at();

alter table change_orders    enable row level security;
alter table change_orders    force  row level security;
alter table sov_lines        enable row level security;
alter table sov_lines        force  row level security;
alter table pay_applications enable row level security;
alter table pay_applications force  row level security;
alter table pay_app_lines    enable row level security;
alter table pay_app_lines    force  row level security;

notify pgrst, 'reload schema';
