-- 0094 - LLM job queue + call log.
--
-- Durable queue for the three long-running AI jobs (BOQ extraction, general
-- material estimate, proposal scope lines). Until now these ran as in-process
-- FastAPI BackgroundTasks: a deploy or crash mid-run stranded the domain row
-- at pending/running forever (proposal_scope grew a 15-minute lazy stale-fail;
-- boq/general-material had nothing). llm_jobs makes dispatch survive
-- restarts: routers insert a job row, a worker loop in each uvicorn worker
-- claims due jobs atomically and runs them, transient failures are retried on
-- a fixed backoff schedule, and a crashed worker's lease simply expires so
-- the job is picked up again instead of being lost.
--
-- Design notes:
-- - Job statuses are job-like -> text + check, not pg enums (0061 precedent).
-- - The domain tables (boq_analyses, general_material_estimates,
--   proposal_drafts) keep their existing status vocabularies untouched;
--   'pending' now also covers "queued / waiting for a retry". Queue detail
--   (position, attempt count, next retry) is joined into the poll responses
--   from this table instead of widening three CHECK constraints and every
--   status consumer with new states.
-- - One ACTIVE job per (job_type, target_id) via a partial unique index
--   (0076 claim-row precedent: the insert either wins or 23505s). target_id
--   is text, not a uuid FK: it points at a different table per job_type
--   (analysis id / project id / draft id).
-- - claim_llm_jobs() is the only way jobs move queued -> running. It uses
--   FOR UPDATE SKIP LOCKED so the two prod uvicorn workers (Dockerfile CMD)
--   can never claim the same job. SECURITY INVOKER + pinned search_path per
--   the search_submittals convention (0072); the backend calls it as the
--   service-role key.
-- - Attempts are counted at claim time, so a claim that dies with the worker
--   still consumed its attempt; the lease-expiry sweep decides requeue vs
--   terminal failure without guessing what happened.
--
-- llm_call_log is the monitoring feed for the dev AI monitor page: one row
-- per LLM call across ALL features and tiers (queued jobs, ingest pipeline
-- calls, interactive calls), success or failure, so failures per day/week can
-- be counted without scraping three domain tables. error holds the sanitized
-- user-facing message, never a raw exception (raw SDK errors can carry the
-- endpoint URL; llm_health precedent). Append-only; the worker loop prunes
-- rows past the retention window.

-- ── llm_jobs ────────────────────────────────────────────────────────────

create table llm_jobs (
  id               uuid primary key default gen_random_uuid(),
  job_type         text not null
                   check (job_type in ('boq_extraction', 'general_material', 'proposal_lines')),
  feature          text not null,  -- llm.py feature key ('boq' | 'estimate' | 'proposal')
  target_id        text not null,  -- domain row this job works on (see job_type)
  project_id       uuid references projects(id) on delete cascade,
  payload          jsonb not null default '{}'::jsonb,
  status           text not null default 'queued'
                   check (status in ('queued', 'running', 'succeeded', 'failed', 'canceled')),
  priority         int not null default 100,  -- lower runs first
  attempts         int not null default 0,
  max_attempts     int not null default 6,
  next_attempt_at  timestamptz not null default now(),
  claimed_by       text,        -- worker token holding the lease
  lease_expires_at timestamptz, -- running past this = worker died, sweep requeues
  error_kind       text,        -- classified: unreachable | timeout | overloaded | ...
  last_error       text,        -- sanitized user-facing message of the last failure
  created_by       uuid references profiles(id) on delete set null,
  started_at       timestamptz, -- first claim
  finished_at      timestamptz, -- terminal transition
  created_at       timestamptz not null default now(),
  updated_at       timestamptz not null default now()
);

comment on column llm_jobs.target_id is
  'Domain row the job works on: boq_analyses.id, general_material_estimates.project_id, or proposal_drafts.id depending on job_type. Text (not FK) because the referenced table varies.';
comment on column llm_jobs.next_attempt_at is
  'Job is claimable once now() passes this. Set into the future by the retry backoff schedule.';
comment on column llm_jobs.lease_expires_at is
  'NULL unless running. A running job whose lease expired is treated as interrupted (worker crashed or was redeployed) and requeued by the sweep.';
comment on column llm_jobs.last_error is
  'Sanitized user-facing message of the most recent failed attempt. NULL = no attempt has failed.';

-- One active job per target: the second enqueue for the same work 23505s
-- (must stay a partial index; only queued/running block a fresh enqueue).
create unique index llm_jobs_active_target_uq
  on llm_jobs(job_type, target_id) where status in ('queued', 'running');

-- Claim scan: due queued jobs in (priority, created_at) order.
create index llm_jobs_claim_idx
  on llm_jobs(priority, created_at) where status = 'queued';

-- Lease-expiry sweep.
create index llm_jobs_running_lease_idx
  on llm_jobs(lease_expires_at) where status = 'running';

create index llm_jobs_project_idx on llm_jobs(project_id, created_at desc)
  where project_id is not null;

-- Monitor page: recent jobs newest first.
create index llm_jobs_created_idx on llm_jobs(created_at desc);

create trigger llm_jobs_updated_at before update on llm_jobs
  for each row execute function set_updated_at();

alter table llm_jobs enable row level security;
alter table llm_jobs force row level security;

-- ── claim_llm_jobs ──────────────────────────────────────────────────────

-- Atomically claim up to max_jobs due jobs for one worker. FOR UPDATE SKIP
-- LOCKED makes concurrent claims from the two uvicorn workers disjoint.
-- Attempts increment here: the attempt is spent the moment a worker takes
-- the job, so a worker that dies mid-run cannot grant the job a free retry.
create or replace function claim_llm_jobs(worker_id text, lease_seconds int, max_jobs int)
returns setof llm_jobs
language sql
volatile
security invoker
set search_path = pg_catalog, public
as $$
  update llm_jobs
  set status = 'running',
      claimed_by = worker_id,
      lease_expires_at = now() + make_interval(secs => lease_seconds),
      attempts = attempts + 1,
      started_at = coalesce(started_at, now())
  where id in (
    select id
    from llm_jobs
    where status = 'queued' and next_attempt_at <= now()
    order by priority, created_at
    limit max_jobs
    for update skip locked
  )
  returning *;
$$;

-- ── llm_call_log ────────────────────────────────────────────────────────

create table llm_call_log (
  id          uuid primary key default gen_random_uuid(),
  feature     text not null,  -- llm.py feature key ('boq', 'quote_pdf', ...)
  provider    text not null,  -- 'anthropic' | 'openai' | 'self_hosted'
  model       text,
  tier        text not null check (tier in ('job', 'interactive', 'pipeline')),
  job_id      uuid references llm_jobs(id) on delete set null,
  ok          boolean not null,
  error_kind  text,  -- NULL when ok
  error       text,  -- sanitized user-facing message, NULL when ok
  duration_ms int,
  created_at  timestamptz not null default now()
);

comment on column llm_call_log.tier is
  'How the call reached the model: job = durable llm_jobs worker, pipeline = ingest loops (email match, quote extraction), interactive = a user-facing request waiting on the answer.';
comment on column llm_call_log.error is
  'Sanitized user-facing message (llm_errors.user_message), never str(exc): raw SDK errors can carry the endpoint URL or response bodies.';

create index llm_call_log_created_idx on llm_call_log(created_at desc);
create index llm_call_log_feature_idx on llm_call_log(feature, created_at desc);
create index llm_call_log_failed_idx on llm_call_log(created_at desc) where not ok;

alter table llm_call_log enable row level security;
alter table llm_call_log force row level security;

notify pgrst, 'reload schema';
