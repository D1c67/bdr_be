-- 0123 - RFP Ingestion: project data harvest (docs/RFP_HARVEST.md, section 4).
-- Apply after 0122.
--
-- After the match step finds no existing project, a `harvest` step goes to
-- the platform the invitation came from (Procore first), pulls the project
-- facts and the bidding documents, and parks them: facts on rfp_harvests,
-- documents in an Ingestion Sandbox run (source_kind 'rfp_email'). One
-- rfp_harvests row per platform object (method + external_key), so the
-- daily reminder and the other recipients' copies reuse it. The session the
-- harvester logs in with lives in rfp_harvest_sessions, one row per
-- provider, shared by every worker.
--
-- Status vocabulary after this migration:
--   pending  : received, auth, keywords, classify, authorize, method,
--              extract, match, harvest
--   human    : review_llm, flagged_unauthorized, review_match
--   terminal : done, merged, duplicate, flagged_auth, flagged_no_keywords,
--              flagged_llm_no, rejected_by_review, failed
--
-- Release order: apply this migration, then deploy. The old sweep ignores
-- `harvest`; nothing reaches it until the new match step routes there.
-- Every statement here is idempotent.

-- ── 1. rfp_harvests: one row per platform object ────────────────────────

create table if not exists rfp_harvests (
  id                uuid primary key default gen_random_uuid(),
  rfp_email_id      uuid not null references rfp_emails(id) on delete cascade,
  method            text not null,
  external_key      text not null,                 -- e.g. procore:{company_id}:{bid_id}
  external_url      text,                          -- the rebuilt bid sheet URL, never the email's
  status            text not null default 'pending'
                    check (status in ('pending', 'running', 'complete', 'failed')),
  claim_token       text,
  attempts          int not null default 0,
  last_error        text,
  data              jsonb,                          -- normalized facts (doc 4)
  raw               jsonb,                          -- trimmed platform payloads (no signed URLs, no phones)
  description_text  text,
  instructions_text text,
  files             jsonb not null default '[]'::jsonb,   -- manifest with per-file outcome; never URLs
  file_count        int not null default 0,
  files_accepted    int not null default 0,
  bytes_downloaded  bigint not null default 0,
  sandbox_run_id    uuid references rfp_ingest_runs(id) on delete set null,
  facts_at          timestamptz,
  started_at        timestamptz,
  finished_at       timestamptz,
  created_at        timestamptz not null default now(),
  updated_at        timestamptz not null default now()
);

comment on table rfp_harvests is
  'Project facts and document manifest harvested from the invitation platform for one platform object (method + external_key). Reused by every later email about the same object; "Harvest again" refreshes the row in place.';
comment on column rfp_harvests.rfp_email_id is
  'The email whose job created the row (the first one seen for that platform object). Other emails point at the row through rfp_emails.harvest_id.';
comment on column rfp_harvests.claim_token is
  'Fences the running writes: two workers picking up two emails for the same bid cannot both harvest it.';
comment on column rfp_harvests.facts_at is
  'Stamped when the facts landed, before any document download, so a download failure keeps the facts.';
comment on column rfp_harvests.files is
  'Array of {file_path, size, kind, discipline, drawing_title, revision, sandbox_file_id, status, error}. Signed platform URLs are never stored.';

create unique index if not exists rfp_harvests_key_uidx
  on rfp_harvests (method, external_key);
create index if not exists rfp_harvests_email_idx
  on rfp_harvests (rfp_email_id);
create index if not exists rfp_harvests_run_idx
  on rfp_harvests (sandbox_run_id) where sandbox_run_id is not null;

drop trigger if exists rfp_harvests_updated_at on rfp_harvests;
create trigger rfp_harvests_updated_at before update on rfp_harvests
  for each row execute function set_updated_at();

alter table rfp_harvests enable row level security;
alter table rfp_harvests force row level security;

-- ── 2. rfp_harvest_sessions: the one shared platform session ────────────

create table if not exists rfp_harvest_sessions (
  provider              text primary key,           -- 'procore'
  account               text,                       -- the login email the jar belongs to
  cookies               jsonb,                      -- [{name, value, domain, path, expires, secure}]
  logged_in_at          timestamptz,
  last_used_at          timestamptz,
  last_login_attempt_at timestamptz,
  login_failures        int not null default 0,
  locked_until          timestamptz,
  last_error            text,
  updated_at            timestamptz not null default now()
);

comment on table rfp_harvest_sessions is
  'One row per invitation platform: the cookie jar the harvester logged in with (a bearer credential for the account; service role only, never returned by any route), the consecutive login failure count and the lock it produces.';

drop trigger if exists rfp_harvest_sessions_updated_at on rfp_harvest_sessions;
create trigger rfp_harvest_sessions_updated_at before update on rfp_harvest_sessions
  for each row execute function set_updated_at();

alter table rfp_harvest_sessions enable row level security;
alter table rfp_harvest_sessions force row level security;

-- ── 3. rfp_emails: the link to the harvest and the new pending status ───

alter table rfp_emails
  add column if not exists harvest_id   uuid references rfp_harvests(id) on delete set null,
  add column if not exists harvested_at timestamptz;

comment on column rfp_emails.harvest_id is
  'The rfp_harvests row for this email''s platform object (shared with every other email about it). Set on every exit from the harvest step, failed included, so the detail screen can show the outcome.';

create index if not exists rfp_emails_harvest_idx
  on rfp_emails (harvest_id) where harvest_id is not null;

alter table rfp_emails drop constraint if exists rfp_emails_status_check;
alter table rfp_emails add constraint rfp_emails_status_check check (status in (
  'received', 'auth', 'keywords', 'classify', 'authorize', 'method', 'extract', 'match',
  'harvest',
  'review_llm', 'flagged_unauthorized', 'review_match',
  'done', 'merged', 'duplicate', 'flagged_auth', 'flagged_no_keywords', 'flagged_llm_no',
  'rejected_by_review', 'failed'));

-- ── 4. rfp_ingest_runs: a third source kind ─────────────────────────────

alter table rfp_ingest_runs
  add column if not exists rfp_email_id uuid references rfp_emails(id) on delete set null,
  add column if not exists harvest_id   uuid references rfp_harvests(id) on delete set null;

do $$
declare
  con record;
begin
  for con in
    select c.conname
    from pg_constraint c
    join pg_attribute a on a.attrelid = c.conrelid and a.attnum = any (c.conkey)
    where c.conrelid = 'public.rfp_ingest_runs'::regclass
      and c.contype = 'c'
      and a.attname = 'source_kind'
      and array_length(c.conkey, 1) = 1
  loop
    execute format('alter table rfp_ingest_runs drop constraint %I', con.conname);
  end loop;

  alter table rfp_ingest_runs add constraint rfp_ingest_runs_source_kind_check
    check (source_kind in ('upload', 'email', 'rfp_email'));
end
$$;

create index if not exists rfp_ingest_runs_rfp_email_idx
  on rfp_ingest_runs (rfp_email_id) where rfp_email_id is not null;

-- ── 5. llm_jobs: the harvest job type ───────────────────────────────────

alter table llm_jobs drop constraint if exists llm_jobs_job_type_check;
alter table llm_jobs add constraint llm_jobs_job_type_check
  check (job_type in ('boq_extraction', 'general_material', 'proposal_lines',
                      'bid_split', 'rfp_ingest', 'rfp_harvest'));

notify pgrst, 'reload schema';
