-- 0058 — PM documents: a PARALLEL structure to project_files, deliberately NOT new
-- file_category enum values. project_files carries bidding machinery (estimator
-- visibility sets, the hand-off lock, submission rounds, the ZIP export); PM
-- documents have none of those rules and the external estimator must never reach
-- them — the separation is structural, not conditional. Objects live in the same
-- private 'project-files' bucket under {project_id}/pm/{category}/….

create type pm_doc_category as enum (
  'contract', 'change_order', 'submittal', 'permit', 'as_built',
  'drawing', 'schedule', 'correspondence', 'photo', 'closeout', 'other'
);

create table pm_documents (
  id           uuid primary key default gen_random_uuid(),
  project_id   uuid not null references projects(id) on delete cascade,
  category     pm_doc_category not null,
  storage_path text not null,
  filename     text not null,
  mime_type    text,
  size_bytes   bigint,
  note         text,
  uploaded_by  uuid references profiles(id) on delete set null,
  created_at   timestamptz not null default now(),
  updated_at   timestamptz not null default now()
);
create index if not exists pm_documents_project_idx  on pm_documents(project_id);
create index if not exists pm_documents_category_idx on pm_documents(project_id, category);
create trigger pm_documents_updated_at before update on pm_documents
  for each row execute function set_updated_at();

alter table pm_documents enable row level security;
alter table pm_documents force  row level security;

notify pgrst, 'reload schema';
