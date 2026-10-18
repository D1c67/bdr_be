-- 0152: AI Estimating (plans reader), docs/PLANS_READER.md.
--
-- GPT-6 Astra reads a bid set: the Bid File Splitter cuts the set by trade,
-- one "sub agent" reads each category in token-budgeted groups, a main agent
-- consolidates everything into a brief plus the BOQ, estimate and markup
-- specs the app renders into files, and users chat with the stored outputs.
-- Every model call is recorded with its tokens and cost; a daily spend cap
-- stops new calls and notifies IT Admins and Executives.
--
-- Also widens llm_call_log with token and cost columns for every feature,
-- adds the plans reader job types to llm_jobs, lets a splitter job carry
-- source 'plans_reader' (no project, so no re-filing side effects), and
-- stores per-page roles and titles on bid_split_segments so a trade segment
-- knows its own legend, index and schedule pages.
--
-- Every statement is idempotent. DEV database only until the release.

-- ---------------------------------------------------------------------------
-- Shared LLM plumbing
-- ---------------------------------------------------------------------------
alter table llm_call_log add column if not exists input_tokens     int;
alter table llm_call_log add column if not exists cached_tokens    int;
alter table llm_call_log add column if not exists output_tokens    int;
alter table llm_call_log add column if not exists reasoning_tokens int;
alter table llm_call_log add column if not exists cost_usd         numeric(12, 6);

comment on column llm_call_log.input_tokens is
  'Provider-reported prompt tokens (OpenAI usage.input_tokens). NULL when the transport did not report usage.';
comment on column llm_call_log.cost_usd is
  'Computed from the per-model price table in services/llm_pricing.py at call time; NULL when the model has no price row.';

alter table llm_jobs drop constraint if exists llm_jobs_job_type_check;
alter table llm_jobs add constraint llm_jobs_job_type_check
  check (job_type in ('boq_extraction', 'general_material', 'proposal_lines',
                      'bid_split', 'rfp_ingest', 'rfp_harvest',
                      'rfp_portal_scan', 'rfp_portal_harvest', 'rfp_create_files',
                      'plans_reader_agent', 'plans_reader_main',
                      'plans_reader_modify', 'plans_reader_chat'));

alter table bid_split_jobs drop constraint if exists bid_split_jobs_source_check;
alter table bid_split_jobs add constraint bid_split_jobs_source_check
  check (source in ('manual', 'rfp', 'project', 'plans_reader'));

comment on column bid_split_jobs.source is
  'manual = uploaded on the /bid-splitter page; rfp = staged by the RFP Ingestion split step from a harvest (created_by null); project = opened from a project page''s missing-files drop zone, linked to the project from birth; plans_reader = staged by an AI Estimating run (project_id stays NULL so nothing is re-filed into a project). An rfp or project job cannot be deleted while its project exists.';

alter table bid_split_segments add column if not exists page_meta jsonb;

comment on column bid_split_segments.page_meta is
  'Per-page detail for this segment, in page order: [{"page": <1-based source page>, "title": <sheet number and title or null>, "page_role": <cover_index|legend_symbols|general_notes|schedule|plan|detail|diagram|site|spec_text|other>, "confidence": <0..1>}]. Written after island repair; NULL on segments cut before 0152.';

-- ---------------------------------------------------------------------------
-- Runs
-- ---------------------------------------------------------------------------
create table if not exists plans_reader_runs (
  id                   uuid primary key default gen_random_uuid(),
  name                 text not null,
  status               text not null default 'created'
    check (status in ('created', 'uploading', 'splitting', 'reading', 'consolidating',
                      'rendering', 'done', 'done_with_errors', 'failed', 'canceled', 'over_cap')),
  source               text not null default 'upload' check (source in ('upload', 'project')),
  project_id           uuid references projects(id) on delete set null,
  render_mode          text not null default 'adaptive' check (render_mode in ('adaptive', 'tiles_260')),
  reasoning_effort     text not null default 'medium'
    check (reasoning_effort in ('low', 'medium', 'high', 'xhigh', 'max')),
  model                text,
  settings_snapshot    jsonb not null default '{}'::jsonb,
  split_job_id         uuid references bid_split_jobs(id) on delete set null,
  ledger               jsonb,
  documents_total      int not null default 0,
  groups_total         int not null default 0,
  groups_done          int not null default 0,
  groups_failed        int not null default 0,
  estimated_input_tokens bigint,
  estimated_cost_usd   numeric(12, 4),
  input_tokens         bigint not null default 0,
  cached_tokens        bigint not null default 0,
  output_tokens        bigint not null default 0,
  reasoning_tokens     bigint not null default 0,
  cost_usd             numeric(12, 6) not null default 0,
  error                text,
  created_by           uuid references profiles(id) on delete set null,
  started_at           timestamptz,
  finished_at          timestamptz,
  created_at           timestamptz not null default now(),
  updated_at           timestamptz not null default now()
);

comment on table plans_reader_runs is
  'One AI Estimating run over a set of uploaded files or a copy of an existing project''s files. Token and cost totals are summed from plans_reader_calls as calls finish.';
comment on column plans_reader_runs.render_mode is
  'adaptive = one native image per sheet at the largest ppi under 30,000 patches, then 260 ppi tiles only for regions the model flags; tiles_260 = every sheet as 260 ppi native tiles.';
comment on column plans_reader_runs.ledger is
  'The run ledger sent with every group call: sheet index, per-sheet titles, legend and general-notes page refs, spec table of contents.';

create index if not exists plans_reader_runs_created_idx on plans_reader_runs (created_at desc);
create index if not exists plans_reader_runs_status_idx on plans_reader_runs (status, created_at desc);
create index if not exists plans_reader_runs_project_idx on plans_reader_runs (project_id) where project_id is not null;

create table if not exists plans_reader_files (
  id                      uuid primary key default gen_random_uuid(),
  run_id                  uuid not null references plans_reader_runs(id) on delete cascade,
  filename                text not null,
  storage_path            text not null,
  size_bytes              bigint not null default 0,
  mime_type               text,
  page_count              int,
  label                   text,
  source_project_file_id  uuid references project_files(id) on delete set null,
  split_file_id           uuid references bid_split_files(id) on delete set null,
  status                  text not null default 'pending'
    check (status in ('pending', 'splitting', 'split', 'labeled', 'converted', 'failed')),
  error                   text,
  created_at              timestamptz not null default now()
);

comment on column plans_reader_files.label is
  'Splitter-vocabulary category the user gave at upload (electrical_drawings, specifications, ...), or NULL = unlabeled, goes through the splitter.';

create index if not exists plans_reader_files_run_idx on plans_reader_files (run_id, created_at);

create table if not exists plans_reader_documents (
  id               uuid primary key default gen_random_uuid(),
  run_id           uuid not null references plans_reader_runs(id) on delete cascade,
  file_id          uuid not null references plans_reader_files(id) on delete cascade,
  segment_id       uuid references bid_split_segments(id) on delete set null,
  category         text not null
    check (category in ('general_drawings', 'civil_drawings', 'structural_drawings',
                        'architectural_drawings', 'mechanical_drawings', 'plumbing_drawings',
                        'electrical_drawings', 'fire_protection_drawings', 'low_voltage_drawings',
                        'specifications', 'addenda', 'rfp', 'other')),
  other_type       text,
  name             text not null,
  storage_path     text not null,
  page_start       int not null default 1,
  page_end         int not null default 1,
  page_count       int not null default 1,
  page_meta        jsonb,
  sheet_width_in   numeric(6, 2),
  sheet_height_in  numeric(6, 2),
  status           text not null default 'pending'
    check (status in ('pending', 'reading', 'done', 'failed')),
  created_at       timestamptz not null default now()
);

comment on table plans_reader_documents is
  'One readable unit: a splitter segment or a pre-labeled file. storage_path points at the segment output or, for intact files, the source object. page_start/page_end are source-file pages; page_meta mirrors bid_split_segments.page_meta.';

create index if not exists plans_reader_documents_run_idx on plans_reader_documents (run_id, category, created_at);

create table if not exists plans_reader_groups (
  id                 uuid primary key default gen_random_uuid(),
  run_id             uuid not null references plans_reader_runs(id) on delete cascade,
  kind               text not null
    check (kind in ('sub', 'rollup', 'escalation', 'main', 'modify', 'chat')),
  category           text,
  group_index        int not null default 0,
  pass               int not null default 1,
  pages              jsonb not null default '[]'::jsonb,
  est_input_tokens   int,
  status             text not null default 'queued'
    check (status in ('queued', 'running', 'done', 'failed', 'canceled')),
  output             jsonb,
  error              text,
  attempts           int not null default 0,
  llm_job_id         uuid references llm_jobs(id) on delete set null,
  started_at         timestamptz,
  finished_at        timestamptz,
  created_at         timestamptz not null default now()
);

comment on table plans_reader_groups is
  'One model call unit (a tiered_plans_agent): the pages of one category packed under the token budget, or a roll-up, escalation, main or modify call. Each is its own llm_jobs job so leases, retries and parallelism are per call.';
comment on column plans_reader_groups.pages is
  '[{"document_id": uuid, "page": <1-based page within the document>, "ppi": int, "detail": "original", "tiles": <null or [[x0,y0,x1,y1] fractions]>, "kind": "image"|"file"}]';

create index if not exists plans_reader_groups_run_idx on plans_reader_groups (run_id, kind, category, group_index);
create index if not exists plans_reader_groups_status_idx on plans_reader_groups (status) where status in ('queued', 'running');

create table if not exists plans_reader_calls (
  id                uuid primary key default gen_random_uuid(),
  run_id            uuid references plans_reader_runs(id) on delete cascade,
  group_id          uuid references plans_reader_groups(id) on delete set null,
  thread_id         uuid,
  kind              text not null,
  category          text,
  model             text,
  reasoning_effort  text,
  request_snapshot  jsonb,
  response_id       text,
  input_tokens      int,
  cached_tokens     int,
  output_tokens     int,
  reasoning_tokens  int,
  cost_usd          numeric(12, 6),
  duration_ms       int,
  ok                boolean not null default true,
  error_kind        text,
  error             text,
  created_at        timestamptz not null default now()
);

comment on table plans_reader_calls is
  'Every OpenAI call the feature makes, with tokens and cost. request_snapshot holds prompts, page refs, ppi and detail per image but never image bytes; the pages re-render from the stored documents.';

create index if not exists plans_reader_calls_run_idx on plans_reader_calls (run_id, created_at);
create index if not exists plans_reader_calls_created_idx on plans_reader_calls (created_at desc);

create table if not exists plans_reader_outputs (
  id                uuid primary key default gen_random_uuid(),
  run_id            uuid not null references plans_reader_runs(id) on delete cascade,
  scope             text not null check (scope in ('category', 'main')),
  category          text,
  version           int not null default 1,
  output            jsonb not null,
  source_group_ids  uuid[] not null default '{}',
  created_at        timestamptz not null default now()
);

create unique index if not exists plans_reader_outputs_uq
  on plans_reader_outputs (run_id, scope, coalesce(category, ''), version);

create table if not exists plans_reader_deliverables (
  id             uuid primary key default gen_random_uuid(),
  run_id         uuid not null references plans_reader_runs(id) on delete cascade,
  kind           text not null
    check (kind in ('boq', 'estimate', 'trench_markup', 'sawcut_markup', 'marked_plans', 'brief')),
  version        int not null default 1,
  filename       text not null,
  storage_path   text not null,
  size_bytes     bigint not null default 0,
  spec_json      jsonb not null,
  instruction    text,
  created_by     uuid references profiles(id) on delete set null,
  created_at     timestamptz not null default now()
);

comment on column plans_reader_deliverables.spec_json is
  'The JSON the file was rendered from (the main agent''s boq / estimate_rows / markups / marked_plans / brief, or a modified version). A chat modification writes a new version with the instruction.';

create unique index if not exists plans_reader_deliverables_uq
  on plans_reader_deliverables (run_id, kind, version);

create table if not exists plans_reader_chat_threads (
  id          uuid primary key default gen_random_uuid(),
  run_id      uuid not null references plans_reader_runs(id) on delete cascade,
  title       text,
  created_by  uuid references profiles(id) on delete set null,
  created_at  timestamptz not null default now(),
  updated_at  timestamptz not null default now()
);

create index if not exists plans_reader_chat_threads_run_idx on plans_reader_chat_threads (run_id, created_at);

create table if not exists plans_reader_chat_messages (
  id             uuid primary key default gen_random_uuid(),
  thread_id      uuid not null references plans_reader_chat_threads(id) on delete cascade,
  role           text not null check (role in ('user', 'assistant')),
  content        text not null default '',
  tool_calls     jsonb,
  pro_max        boolean not null default false,
  status         text not null default 'done' check (status in ('pending', 'running', 'done', 'failed')),
  error          text,
  input_tokens   int,
  output_tokens  int,
  cost_usd       numeric(12, 6),
  llm_job_id     uuid references llm_jobs(id) on delete set null,
  created_by     uuid references profiles(id) on delete set null,
  created_at     timestamptz not null default now()
);

comment on column plans_reader_chat_messages.tool_calls is
  'Assistant turns: [{"name": ..., "arguments": {...}, "result_summary": ...}] in call order, so the UI can show what the model looked at (and which deliverable version a modify produced).';

create index if not exists plans_reader_chat_messages_thread_idx on plans_reader_chat_messages (thread_id, created_at);

-- ---------------------------------------------------------------------------
-- Spend cap (single settings row + event trail)
-- ---------------------------------------------------------------------------
create table if not exists ai_spend_settings (
  id                            smallint primary key default 1 check (id = 1),
  plans_reader_daily_cap_usd    numeric(10, 2) not null default 75,
  cap_window_started_at         timestamptz,
  cap_hit_notified_at           timestamptz,
  updated_by                    uuid references profiles(id) on delete set null,
  updated_at                    timestamptz not null default now()
);

comment on table ai_spend_settings is
  'Single row. The AI Estimating daily cap in USD. Spend counts plans_reader_calls.cost_usd since the later of the Pacific-time start of day and cap_window_started_at (a reset moves cap_window_started_at to now). cap_hit_notified_at dedupes the cap-hit notification per window.';

insert into ai_spend_settings (id) values (1) on conflict (id) do nothing;

create table if not exists ai_spend_events (
  id          bigserial primary key,
  kind        text not null check (kind in ('cap_hit', 'cap_reset', 'cap_changed')),
  amount_usd  numeric(12, 4),
  details     jsonb,
  actor_id    uuid references profiles(id) on delete set null,
  created_at  timestamptz not null default now()
);

create index if not exists ai_spend_events_created_idx on ai_spend_events (created_at desc);

-- ---------------------------------------------------------------------------
-- RLS deny-by-default (service-role backend bypasses it); see 0007 / 0055.
-- ---------------------------------------------------------------------------
alter table plans_reader_runs enable row level security;
alter table plans_reader_runs force row level security;
alter table plans_reader_files enable row level security;
alter table plans_reader_files force row level security;
alter table plans_reader_documents enable row level security;
alter table plans_reader_documents force row level security;
alter table plans_reader_groups enable row level security;
alter table plans_reader_groups force row level security;
alter table plans_reader_calls enable row level security;
alter table plans_reader_calls force row level security;
alter table plans_reader_outputs enable row level security;
alter table plans_reader_outputs force row level security;
alter table plans_reader_deliverables enable row level security;
alter table plans_reader_deliverables force row level security;
alter table plans_reader_chat_threads enable row level security;
alter table plans_reader_chat_threads force row level security;
alter table plans_reader_chat_messages enable row level security;
alter table plans_reader_chat_messages force row level security;
alter table ai_spend_settings enable row level security;
alter table ai_spend_settings force row level security;
alter table ai_spend_events enable row level security;
alter table ai_spend_events force row level security;

-- updated_at triggers follow the existing helper when present.
do $$
begin
  if exists (select 1 from pg_proc where proname = 'set_updated_at') then
    execute 'drop trigger if exists plans_reader_runs_updated_at on plans_reader_runs';
    execute 'create trigger plans_reader_runs_updated_at before update on plans_reader_runs for each row execute function set_updated_at()';
    execute 'drop trigger if exists plans_reader_chat_threads_updated_at on plans_reader_chat_threads';
    execute 'create trigger plans_reader_chat_threads_updated_at before update on plans_reader_chat_threads for each row execute function set_updated_at()';
    execute 'drop trigger if exists ai_spend_settings_updated_at on ai_spend_settings';
    execute 'create trigger ai_spend_settings_updated_at before update on ai_spend_settings for each row execute function set_updated_at()';
  end if;
end $$;

notify pgrst, 'reload schema';
