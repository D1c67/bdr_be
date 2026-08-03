-- BOQ extraction: user-correction drafts + dev training capture.
-- Replaces the refine ("ask model to fix") loop.

-- Exact model input, captured when the run starts: {"system": <text>, "user": <text>}.
alter table boq_analyses add column if not exists input_snapshot jsonb;

-- Reviewer's working draft (inline edits / category moves / removals).
alter table boq_analyses add column if not exists draft_json jsonb;
alter table boq_analyses add column if not exists draft_updated_by uuid references profiles(id) on delete set null;
alter table boq_analyses add column if not exists draft_updated_at timestamptz;

-- One training example per confirmed analysis; re-confirm replaces it (latest = truth).
create table boq_training_examples (
  id            uuid primary key default gen_random_uuid(),
  analysis_id   uuid not null unique references boq_analyses(id) on delete cascade,
  project_id    uuid not null references projects(id) on delete cascade,
  boq_file_id   uuid references project_files(id) on delete set null,
  model         text,
  user_output   jsonb not null,
  diff_json     jsonb not null,
  modified      boolean not null default false,
  held_groups   jsonb,
  confirmed_by  uuid references profiles(id) on delete set null,
  confirmed_at  timestamptz not null default now(),
  reviewed_by   uuid references profiles(id) on delete set null,
  reviewed_at   timestamptz,
  review_note   text,
  created_at    timestamptz not null default now(),
  updated_at    timestamptz not null default now()
);
create index boq_training_examples_project_idx on boq_training_examples(project_id, confirmed_at desc);
create index boq_training_examples_time_idx on boq_training_examples(confirmed_at desc);
alter table boq_training_examples enable row level security;
alter table boq_training_examples force row level security;
create trigger boq_training_examples_updated_at before update on boq_training_examples
  for each row execute function set_updated_at();
