-- 0063 — Certified Payroll (CP) module core: enrollment on the projects spine,
-- the company-wide employee registry, and CP reference data (classifications /
-- prevailing-wage rates). CP mirrors the PM pattern (0057): a project is "in CP"
-- when cp_enrolled_at is set AND a cp_details row exists. Enrollment is explicit
-- (user-selected, prevailing-wage projects only) — never automatic on won.

-- Placeholder bidding stage for payroll-only projects imported from the legacy
-- CPR app (jobs that never existed in BDR). Same rationale and ADD VALUE trick
-- as 'pm_only' (0057): the label is never referenced later in this transaction.
alter type project_stage add value if not exists 'cp_only';

create type cp_report_type as enum ('lcp_tracker', 'comply', 'paper');
create type cp_shift_type as enum ('four_tens', 'nights', 'swing', 'regular');
create type cp_doc_category as enum ('w4', 'i9', 'certification', 'license', 'other');

-- Live enrollment marker stays on projects so lists filter without joins
-- (the pm_stage precedent). NULL = not in Certified Payroll.
alter table projects add column if not exists cp_enrolled_at timestamptz;
alter table projects add column if not exists cp_enrolled_by uuid references profiles(id) on delete set null;
create index if not exists projects_cp_enrolled_idx on projects(cp_enrolled_at)
  where cp_enrolled_at is not null;

-- 1:1 CP detail record (the pm_details pattern): row existence = enrolled.
-- Only contract_id is NOT NULL: the enroll endpoint's Pydantic body requires the
-- full compliance set (report_type, shift_type, pwp_number, public body,
-- contractor address), but legacy CPR imports may lack them.
create table cp_details (
  id                            uuid primary key default gen_random_uuid(),
  project_id                    uuid not null unique references projects(id) on delete cascade,
  contract_id                   text not null,
  report_type                   cp_report_type,
  shift_type                    cp_shift_type not null default 'regular',
  shift_start_time              time,
  pwp_number                    text,
  public_body_awarding_contract text,
  contractor_address_street     text,
  contractor_address_city       text,
  contractor_address_state      text,
  contractor_address_zip        text,
  -- Legacy CPR free-text customer; new enrollments leave it null (BDR knows the
  -- GC/customer through the project spine).
  customer_name                 text,
  is_active                     boolean not null default true,
  created_at                    timestamptz not null default now(),
  updated_at                    timestamptz not null default now()
);
create trigger cp_details_updated_at before update on cp_details
  for each row execute function set_updated_at();

create table cp_classifications (
  id                       uuid primary key default gen_random_uuid(),
  code                     text not null,
  name                     text not null,
  description              text,
  display_order            int not null default 0,
  -- Only field classifications appear on certified reports.
  is_field                 boolean not null default true,
  is_apprentice            boolean not null default false,
  apprentice_period        int check (apprentice_period between 1 and 10),
  percentage_of_journeyman numeric(6,2),
  created_at               timestamptz not null default now(),
  updated_at               timestamptz not null default now()
);
create unique index cp_classifications_code_unique_idx on cp_classifications (lower(btrim(code)));
create trigger cp_classifications_updated_at before update on cp_classifications
  for each row execute function set_updated_at();

-- Prevailing-wage rate table, strictly 1:1 with a classification (unique FK).
create table cp_rates (
  id                uuid primary key default gen_random_uuid(),
  classification_id uuid not null unique references cp_classifications(id) on delete cascade,
  hourly_rate       numeric(10,2) not null,
  overtime_rate     numeric(10,2) not null,
  doubletime_rate   numeric(10,2) not null,
  pension           numeric(10,2) not null default 0,
  health_welfare    numeric(10,2) not null default 0,
  training          numeric(10,2) not null default 0,
  other             numeric(10,2) not null default 0,
  total_hourly      numeric(10,2) not null,
  dues              numeric(10,2) not null default 0,
  effective_date    date not null default current_date,
  created_at        timestamptz not null default now(),
  updated_at        timestamptz not null default now()
);
create trigger cp_rates_updated_at before update on cp_rates
  for each row execute function set_updated_at();

-- Company-wide employee registry. Deliberately UNprefixed: this is shared HR
-- data other BDR modules will reuse (the reason the CPR app was merged in).
-- SSN policy: last four digits ONLY — no encrypted SSN is stored anywhere.
create table employees (
  id                uuid primary key default gen_random_uuid(),
  employee_id       text,  -- external payroll id (e.g. Gusto)
  first_name        text not null,
  last_name         text not null,
  alt_ee_name       text,  -- timesheet display-name override used in matching
  ssn_last_four     text check (ssn_last_four ~ '^[0-9]{4}$'),
  personal_email    text,
  jurisdiction      text,  -- 2-letter work state for paper CPR
  classification_id uuid references cp_classifications(id) on delete set null,
  is_active         boolean not null default true,
  created_by        uuid references profiles(id) on delete set null,
  created_at        timestamptz not null default now(),
  updated_at        timestamptz not null default now()
);
create unique index employees_employee_id_unique_idx
  on employees (lower(btrim(employee_id))) where employee_id is not null;
create index employees_name_idx on employees (last_name, first_name);
create trigger employees_updated_at before update on employees
  for each row execute function set_updated_at();

-- Metadata only — bytes live in the project-files bucket under
-- payroll/employees/{employee_id}/…, replacing the legacy BYTEA-in-DB storage.
create table employee_documents (
  id           uuid primary key default gen_random_uuid(),
  employee_id  uuid not null references employees(id) on delete cascade,
  category     cp_doc_category not null,
  storage_path text not null,
  filename     text not null,
  mime_type    text not null,
  size_bytes   bigint not null,
  uploaded_by  uuid references profiles(id) on delete set null,
  created_at   timestamptz not null default now()
);
create index employee_documents_employee_idx on employee_documents(employee_id);

-- Known non-payroll raw project names from timesheets (office/shop/service
-- codes, non-PW jobs). Matching treats a hit as intentionally non-CP: the hours
-- still count toward OT allocation and pay proration but the name never nags in
-- the unmatched list and never reaches a certified report. shift_type feeds the
-- daily OT thresholds for those hours (registry-editable so a real 4x10 non-CP
-- job computes correctly). Replaces the legacy hard-coded "G3 Office" special
-- case.
create table cp_ignored_projects (
  id         uuid primary key default gen_random_uuid(),
  raw_number text,
  raw_name   text not null,
  shift_type cp_shift_type not null default 'regular',
  note       text,
  created_by uuid references profiles(id) on delete set null,
  created_at timestamptz not null default now()
);
create unique index cp_ignored_projects_name_unique_idx
  on cp_ignored_projects (lower(btrim(raw_name)));
create unique index cp_ignored_projects_number_unique_idx
  on cp_ignored_projects (lower(btrim(raw_number))) where raw_number is not null;

insert into cp_ignored_projects (raw_name, note)
values ('G3 Office', 'Office/non-billable time (seeded; was hard-coded in the legacy CPR app)');

-- RLS deny-by-default + forced (0007/0055 pattern); the service-role backend
-- bypasses it and FastAPI deps are the real authz boundary.
alter table cp_details          enable row level security;
alter table cp_details          force  row level security;
alter table cp_classifications  enable row level security;
alter table cp_classifications  force  row level security;
alter table cp_rates            enable row level security;
alter table cp_rates            force  row level security;
alter table employees           enable row level security;
alter table employees           force  row level security;
alter table employee_documents  enable row level security;
alter table employee_documents  force  row level security;
alter table cp_ignored_projects enable row level security;
alter table cp_ignored_projects force  row level security;

notify pgrst, 'reload schema';
