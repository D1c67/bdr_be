-- 0002 — Core tables: profiles, general contractors, material categories,
-- projects, project↔GC links, and project files.

-- Reusable updated_at trigger.
create or replace function set_updated_at()
returns trigger language plpgsql as $$
begin
  new.updated_at = now();
  return new;
end;
$$;

-- ── Profiles (1:1 with auth.users) ───────────────────────────────────────
create table profiles (
  id          uuid primary key references auth.users(id) on delete cascade,
  full_name   text not null,
  email       text not null unique,
  role        role not null,
  is_active   boolean not null default true,
  created_at  timestamptz not null default now(),
  updated_at  timestamptz not null default now()
);
create trigger profiles_updated_at before update on profiles
  for each row execute function set_updated_at();

-- ── General Contractors ──────────────────────────────────────────────────
create table general_contractors (
  id          uuid primary key default gen_random_uuid(),
  name        text not null,
  contact     text,
  email       text,
  phone       text,
  created_at  timestamptz not null default now()
);

-- ── Material categories (configurable; seeded in 0009) ────────────────────
create table material_categories (
  id          uuid primary key default gen_random_uuid(),
  name        text not null,
  kind        material_kind not null default 'material',
  is_active   boolean not null default true,
  sort_order  int not null default 0,
  created_at  timestamptz not null default now()
);

-- ── Projects ──────────────────────────────────────────────────────────────
create table projects (
  id                  uuid primary key default gen_random_uuid(),
  name                text not null,
  number              text not null,                 -- manual entry by PA
  -- Bid dates: "internal" is what we tell PM/PE; "actual" is due to the GC.
  internal_bid_at     timestamptz,
  actual_bid_at       timestamptz,
  est_start_date      date,
  est_finish_date     date,
  invitation_at       timestamptz,                   -- when we were invited
  labor_type          labor_type,
  labor_type_note     text,
  due_from_estimator_at timestamptz,
  notes               text,
  current_stage       project_stage not null default 'intake',
  current_owner_role  role,
  created_by          uuid references profiles(id),
  created_at          timestamptz not null default now(),
  updated_at          timestamptz not null default now()
);
create trigger projects_updated_at before update on projects
  for each row execute function set_updated_at();
create index projects_stage_idx on projects(current_stage);
create index projects_number_idx on projects(number);

-- ── Project ↔ GC (invited us / bidding to) ────────────────────────────────
create table project_gcs (
  id            uuid primary key default gen_random_uuid(),
  project_id    uuid not null references projects(id) on delete cascade,
  gc_id         uuid not null references general_contractors(id) on delete cascade,
  relationship  gc_relationship not null,
  unique (project_id, gc_id, relationship)
);
create index project_gcs_project_idx on project_gcs(project_id);

-- ── Project files (stored in Supabase Storage; row holds metadata) ────────
create table project_files (
  id                    uuid primary key default gen_random_uuid(),
  project_id            uuid not null references projects(id) on delete cascade,
  category              file_category not null,
  storage_path          text not null,               -- path in the bucket
  filename              text not null,
  material_category_id  uuid references material_categories(id),
  uploaded_by           uuid references profiles(id),
  created_at            timestamptz not null default now()
);
create index project_files_project_idx on project_files(project_id);
create index project_files_category_idx on project_files(project_id, category);
