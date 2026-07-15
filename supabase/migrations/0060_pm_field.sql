-- 0060 — PM field operations: milestones, daily logs, RFIs, manpower.

create type rfi_status as enum ('open', 'answered', 'closed');

create table pm_milestones (
  id           uuid primary key default gen_random_uuid(),
  project_id   uuid not null references projects(id) on delete cascade,
  name         text not null,
  planned_date date,
  actual_date  date,               -- set = complete; no status enum needed
  sort_order   int not null default 0,
  notes        text,
  created_by   uuid references profiles(id) on delete set null,
  created_at   timestamptz not null default now(),
  updated_at   timestamptz not null default now()
);
create index if not exists pm_milestones_project_idx on pm_milestones(project_id, sort_order);
create trigger pm_milestones_updated_at before update on pm_milestones
  for each row execute function set_updated_at();

-- Multiple logs per (project, day) are legitimate — several crews / authors.
create table daily_logs (
  id             uuid primary key default gen_random_uuid(),
  project_id     uuid not null references projects(id) on delete cascade,
  log_date       date not null,
  weather        text,
  manpower_count int check (manpower_count >= 0),   -- headline; detail in manpower_entries
  work_performed text not null,
  delays         text,
  safety_notes   text,
  created_by     uuid references profiles(id) on delete set null,
  created_at     timestamptz not null default now(),
  updated_at     timestamptz not null default now()
);
create index if not exists daily_logs_project_date_idx on daily_logs(project_id, log_date desc);
create trigger daily_logs_updated_at before update on daily_logs
  for each row execute function set_updated_at();

create table rfis (
  id          uuid primary key default gen_random_uuid(),
  project_id  uuid not null references projects(id) on delete cascade,
  rfi_number  int not null,        -- server-assigned: max+1 per project
  subject     text not null,
  question    text not null,
  answer      text,
  status      rfi_status not null default 'open',
  asked_of    text,                -- GC / engineer / owner rep (free text)
  sent_at     date,
  due_at      date,
  answered_at date,
  created_by  uuid references profiles(id) on delete set null,
  created_at  timestamptz not null default now(),
  updated_at  timestamptz not null default now(),
  unique (project_id, rfi_number)
);
create index if not exists rfis_project_idx on rfis(project_id);
create trigger rfis_updated_at before update on rfis
  for each row execute function set_updated_at();

create table manpower_entries (
  id             uuid primary key default gen_random_uuid(),
  project_id     uuid not null references projects(id) on delete cascade,
  daily_log_id   uuid references daily_logs(id) on delete set null,  -- optional link
  work_date      date not null,
  classification text not null,    -- free text: foreman / journeyman / apprentice / …
  workers        int not null check (workers >= 0),
  hours          numeric(6,2) check (hours >= 0),
  notes          text,
  created_by     uuid references profiles(id) on delete set null,
  created_at     timestamptz not null default now(),
  updated_at     timestamptz not null default now()
);
create index if not exists manpower_entries_project_date_idx on manpower_entries(project_id, work_date desc);
create trigger manpower_entries_updated_at before update on manpower_entries
  for each row execute function set_updated_at();

alter table pm_milestones    enable row level security;
alter table pm_milestones    force  row level security;
alter table daily_logs       enable row level security;
alter table daily_logs       force  row level security;
alter table rfis             enable row level security;
alter table rfis             force  row level security;
alter table manpower_entries enable row level security;
alter table manpower_entries force  row level security;

notify pgrst, 'reload schema';
