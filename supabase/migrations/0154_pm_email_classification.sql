-- AI classification of emails filed to PM projects (docs/EMAIL_CLASSIFICATION.md).
--
-- One row per classified email in email_classifications (1:1 with
-- ingested_emails, keyed by email_id) so the mail table stays lean. The AI's
-- labels and a user's corrected labels are stored side by side: the effective
-- labels are user_labels when a writer overrode them, else ai_labels. Every
-- correction is logged in email_label_corrections (before/after) as material
-- for future prompt work.
--
-- Labels are a jsonb array of {category, subcategory, title, confidence}.
-- category is one of materials_submittals | rfis | financials |
-- change_management | other; the backend validates every write (model output
-- and user edits) against the same enums, so no CHECK is placed on the jsonb.
--
-- status: pending (queued or waiting for a retry), running, done, failed.
-- The classify step runs ONLY on the self-hosted box (services/llm feature
-- email_classify, self_hosted_only), as llm_jobs job type 'email_classify'.

create table if not exists email_classifications (
  email_id         uuid primary key references ingested_emails(id) on delete cascade,
  -- Copied at enqueue time so the project list joins in one query and the
  -- backfill can walk a project's threads in order. Not authoritative: the
  -- email's own project_id is (labels describe the email, not the project).
  project_id       uuid references projects(id) on delete set null,
  conversation_id  text,
  status           text not null default 'pending'
                     check (status in ('pending', 'running', 'done', 'failed')),
  ai_labels        jsonb,
  user_labels      jsonb,
  overridden_by    uuid references profiles(id) on delete set null,
  overridden_at    timestamptz,
  email_summary    text,
  -- Running summary of the whole thread through this message; fed as context
  -- when the next message of the same thread is classified.
  thread_summary   text,
  model            text,
  error            text,
  classified_at    timestamptz,
  created_at       timestamptz not null default now(),
  updated_at       timestamptz not null default now()
);

drop trigger if exists email_classifications_updated_at on email_classifications;
create trigger email_classifications_updated_at before update on email_classifications
  for each row execute function set_updated_at();

create index if not exists email_classifications_project_idx
  on email_classifications (project_id, status);
create index if not exists email_classifications_conversation_idx
  on email_classifications (conversation_id);
create index if not exists email_classifications_overridden_by_idx
  on email_classifications (overridden_by);

create table if not exists email_label_corrections (
  id          uuid primary key default gen_random_uuid(),
  email_id    uuid not null references ingested_emails(id) on delete cascade,
  user_id     uuid references profiles(id) on delete set null,
  -- 'override' = a writer replaced the label set; 'reset' = back to the AI's.
  action      text not null default 'override' check (action in ('override', 'reset')),
  before      jsonb,
  after       jsonb,
  created_at  timestamptz not null default now()
);
create index if not exists email_label_corrections_email_idx
  on email_label_corrections (email_id, created_at desc);
create index if not exists email_label_corrections_user_idx
  on email_label_corrections (user_id);

-- The queue job type (same list as 0152 plus email_classify).
alter table llm_jobs drop constraint if exists llm_jobs_job_type_check;
alter table llm_jobs add constraint llm_jobs_job_type_check
  check (job_type in ('boq_extraction', 'general_material', 'proposal_lines',
                      'bid_split', 'rfp_ingest', 'rfp_harvest',
                      'rfp_portal_scan', 'rfp_portal_harvest', 'rfp_create_files',
                      'plans_reader_agent', 'plans_reader_main',
                      'plans_reader_modify', 'plans_reader_chat',
                      'email_classify'));

-- RLS: deny-by-default backstop; the service-role backend is the only client.
alter table email_classifications enable row level security;
alter table email_classifications force row level security;
alter table email_label_corrections enable row level security;
alter table email_label_corrections force row level security;

notify pgrst, 'reload schema';
