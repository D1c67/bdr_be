-- 0005 — Estimator assignments (gates external-estimator access) plus the
-- pricing tables: labor reviews, markups, and executive verification.

-- ── Estimator assignments (security gate — see app/core/deps.py) ──────────
create table estimator_assignments (
  id            uuid primary key default gen_random_uuid(),
  project_id    uuid not null references projects(id) on delete cascade,
  estimator_id  uuid not null references profiles(id),
  assigned_by   uuid references profiles(id),
  due_at        timestamptz,
  expires_at    timestamptz,        -- access auto-revokes after this
  revoked_at    timestamptz,        -- instant revoke by IT Admin / PM
  created_at    timestamptz not null default now()
);
create index estimator_assignments_lookup_idx
  on estimator_assignments(estimator_id, project_id);

-- ── Labor review (step 7, PM) ─────────────────────────────────────────────
create table labor_reviews (
  id           uuid primary key default gen_random_uuid(),
  project_id   uuid not null unique references projects(id) on delete cascade,
  reviewed_by  uuid references profiles(id),
  labor_notes  text,
  verified     boolean not null default false,
  updated_at   timestamptz not null default now()
);

-- ── Markup (step 8) ───────────────────────────────────────────────────────
create table markups (
  id            uuid primary key default gen_random_uuid(),
  project_id    uuid not null unique references projects(id) on delete cascade,
  markup_pct    numeric(6,3),
  markup_amount numeric(14,2),
  set_by        uuid references profiles(id),
  notes         text,
  updated_at    timestamptz not null default now()
);

-- ── Executive verification / commit (step 9) ──────────────────────────────
create table verifications (
  id           uuid primary key default gen_random_uuid(),
  project_id   uuid not null unique references projects(id) on delete cascade,
  verified_by  uuid references profiles(id),
  committed_at timestamptz,
  notes        text
);
