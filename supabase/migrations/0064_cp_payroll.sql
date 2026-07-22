-- 0064 — Certified Payroll weekly pipeline: one company-wide report per payroll
-- week (Sun–Sat), the parsed timesheet + Gusto payroll-detail rows behind it,
-- generated CPR revision records (files in storage, not BYTEA), and the two
-- report-identity singletons (company subcontractor identity + per-user signer).

create type cp_payroll_status as enum
  ('draft', 'awaiting_timesheet', 'awaiting_payroll_detail', 'processing', 'processed', 'submitted');

create table cp_payroll_reports (
  id                          uuid primary key default gen_random_uuid(),
  week_start_date             date not null,  -- Sunday
  week_end_date               date not null,  -- Saturday
  status                      cp_payroll_status not null default 'draft',
  -- Source uploads are persisted to storage (payroll/reports/{id}/uploads/…);
  -- the legacy app kept only filenames, so migrated rows have null paths.
  timesheet_filename          text,
  timesheet_storage_path      text,
  payroll_detail_filename     text,
  payroll_detail_storage_path text,
  total_hours                 numeric(10,2),
  total_employees             int,
  finalized_at                timestamptz,
  submitted_at                timestamptz,
  created_by                  uuid references profiles(id) on delete set null,
  finalized_by                uuid references profiles(id) on delete set null,
  submitted_by                uuid references profiles(id) on delete set null,
  created_at                  timestamptz not null default now(),
  updated_at                  timestamptz not null default now()
);
-- Company-wide: exactly one report per payroll week (was per-user in the legacy
-- app, which had no constraint at all — only a check-then-insert race).
create unique index cp_payroll_reports_week_unique_idx on cp_payroll_reports(week_start_date);
create trigger cp_payroll_reports_updated_at before update on cp_payroll_reports
  for each row execute function set_updated_at();

create table cp_time_entries (
  id                      uuid primary key default gen_random_uuid(),
  payroll_report_id       uuid not null references cp_payroll_reports(id) on delete cascade,
  employee_id             uuid references employees(id) on delete set null,
  project_id              uuid references projects(id) on delete set null,
  raw_employee_first_name text not null,
  raw_employee_last_name  text not null,
  raw_project_number      text,
  raw_project_name        text,
  work_date               date not null,
  -- NAIVE local wall-clock on purpose (legacy CPR migration 020): the early-start
  -- OT rule compares these against cp_details.shift_start_time in local time, and
  -- timestamptz would re-introduce the browser timezone-shift bug 020 fixed.
  -- Deliberate deviation from the timestamptz house style — do not "fix".
  start_time              timestamp not null,
  end_time                timestamp not null,
  break_duration_minutes  int not null default 0,
  total_hours             numeric(10,2) not null,  -- quarter-hour rounded at ingest
  customer                text,
  cost_code               text,
  cost_code_desc          text,
  description             text,
  subproject_1_number     text,
  subproject_1_name       text,
  is_employee_matched     boolean not null default false,
  is_project_matched      boolean not null default false,
  created_at              timestamptz not null default now()
);
create index cp_time_entries_report_idx on cp_time_entries(payroll_report_id);
create index cp_time_entries_work_date_idx on cp_time_entries(work_date);
create index cp_time_entries_project_idx on cp_time_entries(project_id) where project_id is not null;

-- One row per employee per report, parsed from the Gusto payroll-detail export.
-- The numeric(10,2) money/hour columns are copied verbatim from the legacy
-- PayrollDetailEntry model.
create table cp_payroll_detail_entries (
  id                                  uuid primary key default gen_random_uuid(),
  payroll_report_id                   uuid not null references cp_payroll_reports(id) on delete cascade,
  employee_name                       text not null,
  employee_id                         uuid references employees(id) on delete set null,
  is_employee_matched                 boolean not null default false,
  pay_date                            date,
  time_period                         text,
  -- Hours
  hours_total                         numeric(10,2),
  hours_regular                       numeric(10,2),
  hours_grave_shift                   numeric(10,2),
  hours_ot                            numeric(10,2),
  hours_holiday                       numeric(10,2),
  hours_foreman                       numeric(10,2),
  hours_gf                            numeric(10,2),
  hours_sal                           numeric(10,2),
  hours_regular_pay                   numeric(10,2),
  hours_overtime_pay                  numeric(10,2),
  hours_salary                        numeric(10,2),
  hours_holiday_pay                   numeric(10,2),
  -- Gross pay
  gross_pay_total                     numeric(10,2),
  gross_pay_regular                   numeric(10,2),
  gross_pay_grave_shift               numeric(10,2),
  gross_pay_ot                        numeric(10,2),
  gross_pay_holiday                   numeric(10,2),
  gross_pay_foreman                   numeric(10,2),
  gross_pay_reimb                     numeric(10,2),
  gross_pay_gf                        numeric(10,2),
  gross_pay_sal                       numeric(10,2),
  gross_pay_regular_pay               numeric(10,2),
  gross_pay_overtime_pay              numeric(10,2),
  gross_pay_reimbursement             numeric(10,2),
  gross_pay_salary                    numeric(10,2),
  gross_pay_holiday_pay               numeric(10,2),
  -- Pre-tax deductions
  pretax_deductions_total             numeric(10,2),
  pretax_401k                         numeric(10,2),
  pretax_401k_catchup                 numeric(10,2),
  adjusted_gross                      numeric(10,2),
  -- Other pay
  other_pay_total                     numeric(10,2),
  other_pay_qot                       numeric(10,2),
  -- Employee taxes
  employee_taxes_total                numeric(10,2),
  employee_taxes_fit                  numeric(10,2),
  employee_taxes_ss                   numeric(10,2),
  employee_taxes_med                  numeric(10,2),
  -- After-tax deductions
  aftertax_deductions_total           numeric(10,2),
  aftertax_working_dues               numeric(10,2),
  aftertax_roth_401k                  numeric(10,2),
  -- Net pay
  net_pay                             numeric(10,2),
  -- Employer taxes & contributions
  employer_taxes_contributions_total  numeric(10,2),
  employer_taxes_total                numeric(10,2),
  employer_taxes_futa                 numeric(10,2),
  employer_taxes_ss                   numeric(10,2),
  employer_taxes_med                  numeric(10,2),
  employer_taxes_sui                  numeric(10,2),
  employer_taxes_cep                  numeric(10,2),
  -- Company contributions
  company_contributions_total         numeric(10,2),
  company_contributions_pension       numeric(10,2),
  company_contributions_401k          numeric(10,2),
  company_contributions_401k_catchup  numeric(10,2),
  company_contributions_dental_vision numeric(10,2),
  total_payroll_cost                  numeric(10,2),
  created_at                          timestamptz not null default now()
);
create index cp_payroll_detail_entries_report_idx on cp_payroll_detail_entries(payroll_report_id);

-- One CPR generation event (revision history). created_by is the generator —
-- their signer profile is printed on paper reports.
create table cp_records (
  id                uuid primary key default gen_random_uuid(),
  payroll_report_id uuid not null references cp_payroll_reports(id) on delete cascade,
  revision_number   int not null default 0,
  paper_metadata    jsonb,
  flags             jsonb,
  created_by        uuid references profiles(id) on delete set null,
  created_at        timestamptz not null default now()
);
create index cp_records_report_idx on cp_records(payroll_report_id, created_at);

-- Generated file metadata; bytes in storage under
-- payroll/reports/{report_id}/cpr/{record_id}/….
create table cp_record_files (
  id           uuid primary key default gen_random_uuid(),
  record_id    uuid not null references cp_records(id) on delete cascade,
  filename     text not null,
  content_type text not null,
  storage_path text not null,
  size_bytes   bigint not null,
  created_at   timestamptz not null default now()
);
create index cp_record_files_record_idx on cp_record_files(record_id);

-- Company-wide singleton (was per-user in the legacy app): the subcontractor
-- identity printed on every report. The bool-true PK enforces exactly one row.
create table cp_settings (
  id             boolean primary key default true check (id),
  name           text,
  street_address text,
  city           text,
  state          text,
  zip_code       text,
  phone          text,
  license_number text,
  created_at     timestamptz not null default now(),
  updated_at     timestamptz not null default now()
);
create trigger cp_settings_updated_at before update on cp_settings
  for each row execute function set_updated_at();

-- Per-user signer identity for the paper CPR Statement of Compliance, keyed to
-- BDR profiles (the generating user signs).
create table cp_signer_profiles (
  profile_id        uuid primary key references profiles(id) on delete cascade,
  first_name        text,
  last_name         text,
  job_title         text,
  personal_email    text,
  date_of_birth     date,
  profile_completed boolean not null default false,
  created_at        timestamptz not null default now(),
  updated_at        timestamptz not null default now()
);
create trigger cp_signer_profiles_updated_at before update on cp_signer_profiles
  for each row execute function set_updated_at();

alter table cp_payroll_reports        enable row level security;
alter table cp_payroll_reports        force  row level security;
alter table cp_time_entries           enable row level security;
alter table cp_time_entries           force  row level security;
alter table cp_payroll_detail_entries enable row level security;
alter table cp_payroll_detail_entries force  row level security;
alter table cp_records                enable row level security;
alter table cp_records                force  row level security;
alter table cp_record_files           enable row level security;
alter table cp_record_files           force  row level security;
alter table cp_settings               enable row level security;
alter table cp_settings               force  row level security;
alter table cp_signer_profiles        enable row level security;
alter table cp_signer_profiles        force  row level security;

notify pgrst, 'reload schema';
