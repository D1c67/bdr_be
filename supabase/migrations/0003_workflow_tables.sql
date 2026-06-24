-- 0003 — Workflow: Go/No-Go voting + decision, and the stage-event log that
-- powers time-in-stage analytics.

-- ── Go/No-Go votes (PM, PA, Executive — one each) ─────────────────────────
create table go_no_go_votes (
  id          uuid primary key default gen_random_uuid(),
  project_id  uuid not null references projects(id) on delete cascade,
  voter_id    uuid not null references profiles(id),
  vote        vote_choice not null,
  comment     text,
  created_at  timestamptz not null default now(),
  unique (project_id, voter_id)   -- one vote per person per project
);
create index gono_votes_project_idx on go_no_go_votes(project_id);

-- ── Go/No-Go decision (one per project) ───────────────────────────────────
create table go_no_go_decisions (
  id          uuid primary key default gen_random_uuid(),
  project_id  uuid not null unique references projects(id) on delete cascade,
  outcome     vote_choice not null,
  method      gono_method not null,        -- majority | override
  decided_by  uuid references profiles(id),
  decided_at  timestamptz not null default now()
);

-- ── Stage events (append-only; source of truth for analytics) ─────────────
create table stage_events (
  id          uuid primary key default gen_random_uuid(),
  project_id  uuid not null references projects(id) on delete cascade,
  from_stage  project_stage,
  to_stage    project_stage not null,
  actor_id    uuid references profiles(id),
  note        text,
  entered_at  timestamptz not null default now()
);
create index stage_events_project_idx on stage_events(project_id, entered_at);
create index stage_events_to_stage_idx on stage_events(to_stage);
