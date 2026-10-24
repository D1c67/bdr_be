-- 0165 plans_reader_autonomy_core (MC in docs/AI_ESTIMATING_BUILD_SPEC.md 5.6)
--
-- The ONE migration of the AI Estimating autonomous build that rewrites
-- existing CHECK constraints, plus the columns that shipped code reads or
-- writes and that therefore ship together with 0164 to 0169 at release.
-- Applied to DEV only during the build (letter order MA..MG = 0163..0169).
-- Live constraint definitions were read with pg_get_constraintdef on
-- 2026-10-21 before the drop and re-add below; every list keeps the current
-- values and appends the new ones. Idempotent: safe to re-run.

-- ---------------------------------------------------------------------------
-- Eval attribution (the ledger of spec 0.4). Nullable; code omits the keys
-- from inserts and selects when the value is null.
-- ---------------------------------------------------------------------------
alter table llm_call_log add column if not exists eval_sweep_id uuid;
create index if not exists llm_call_log_eval_sweep_idx
  on llm_call_log (eval_sweep_id) where eval_sweep_id is not null;
create index if not exists llm_call_log_cost_created_idx
  on llm_call_log (created_at) where cost_usd is not null;

alter table plans_reader_runs add column if not exists eval_case_id  text;
alter table plans_reader_runs add column if not exists eval_sweep_id uuid;
comment on column plans_reader_runs.eval_case_id is
  'Golden case id when the run is an eval run (spec 8.1); mining, library and bands exclude it (leave-one-project-out).';

-- ---------------------------------------------------------------------------
-- Groups: new kinds (derive, recount, review, revise) plus mode, route and
-- durable loop state (spec 4.2, 4.3).
-- ---------------------------------------------------------------------------
alter table plans_reader_groups drop constraint if exists plans_reader_groups_kind_check;
alter table plans_reader_groups add constraint plans_reader_groups_kind_check
  check (kind in ('sub', 'rollup', 'escalation', 'main', 'modify', 'chat',
                  'derive', 'recount', 'review', 'revise'));
alter table plans_reader_groups add column if not exists mode text;
alter table plans_reader_groups drop constraint if exists plans_reader_groups_mode_check;
alter table plans_reader_groups add constraint plans_reader_groups_mode_check
  check (mode is null or mode in ('read', 'review', 'write'));
alter table plans_reader_groups add column if not exists route      jsonb;
alter table plans_reader_groups add column if not exists loop_state jsonb;

-- ---------------------------------------------------------------------------
-- Outputs: policy (WS5), measure (WS3) and evidence (WS8) scopes.
-- ---------------------------------------------------------------------------
alter table plans_reader_outputs drop constraint if exists plans_reader_outputs_scope_check;
alter table plans_reader_outputs add constraint plans_reader_outputs_scope_check
  check (scope in ('category', 'main', 'policy', 'measure', 'evidence'));

-- ---------------------------------------------------------------------------
-- Queue: the page facts job (WS1, non-LLM) and the pricing job (WS6).
-- ---------------------------------------------------------------------------
-- Additive: reads the live definition with pg_get_constraintdef and appends
-- the two types, so it keeps every type any other migration added (the
-- assistant build's 0170 adds its own types the same way) and the apply
-- order between 0165 and 0170 does not matter. Refuses to run when the
-- definition cannot be read as a list, rather than shrinking it.
do $$
declare
  v_def     text;
  v_types   text[];
  v_missing text[];
  v_list    text;
  v_add     constant text[] := array['plans_reader_facts', 'ai_pricing'];
begin
  select pg_get_constraintdef(c.oid) into v_def
  from pg_constraint c
  where c.conrelid = 'public.llm_jobs'::regclass and c.conname = 'llm_jobs_job_type_check';

  if v_def is null then
    raise notice '0165: llm_jobs_job_type_check is missing; job_type stays unconstrained';
    return;
  end if;
  if v_def !~* 'job_type' then
    raise exception '0165: unexpected llm_jobs_job_type_check definition: %', v_def;
  end if;

  select array_agg(replace(r.m[1], '''''', '''') order by r.ord) into v_types
  from regexp_matches(v_def, '''((?:[^'']|'''')*)''', 'g') with ordinality as r(m, ord);

  if v_types is null or cardinality(v_types) = 0 or not ('boq_extraction' = any(v_types)) then
    raise exception '0165: could not read the job type list from llm_jobs_job_type_check: %', v_def;
  end if;

  select array_agg(a.t order by a.ord) into v_missing
  from unnest(v_add) with ordinality as a(t, ord)
  where not (a.t = any(v_types));

  if v_missing is null then
    return;
  end if;

  v_types := v_types || v_missing;
  select string_agg(quote_literal(u.t), ', ' order by u.ord) into v_list
  from unnest(v_types) with ordinality as u(t, ord);

  execute 'alter table public.llm_jobs drop constraint llm_jobs_job_type_check';
  execute format('alter table public.llm_jobs add constraint llm_jobs_job_type_check check (job_type in (%s)) not valid',
                 v_list);
  execute 'alter table public.llm_jobs validate constraint llm_jobs_job_type_check';
end $$;

-- ---------------------------------------------------------------------------
-- Deliverables: the full-size annotated plans PDF (WS8).
-- ---------------------------------------------------------------------------
alter table plans_reader_deliverables drop constraint if exists plans_reader_deliverables_kind_check;
alter table plans_reader_deliverables add constraint plans_reader_deliverables_kind_check
  check (kind in ('boq', 'estimate', 'trench_markup', 'sawcut_markup',
                  'marked_plans', 'brief', 'annotated_plans'));

-- ---------------------------------------------------------------------------
-- File access events: the priced workbook and vendor quote downloads (WS6).
-- ---------------------------------------------------------------------------
alter table file_access_events drop constraint if exists file_access_events_source_check;
alter table file_access_events add constraint file_access_events_source_check
  check (source in ('project_file', 'bid_split_file', 'bid_split_segment',
                    'ai_estimating_document', 'ai_estimating_deliverable',
                    'submittal_file', 'email_attachment',
                    'ai_pricing_estimate', 'ai_pricing_quote'));

-- ---------------------------------------------------------------------------
-- Calls: provider, loop round, cache writes, conservative pricing marker,
-- chat message link (WS2).
-- ---------------------------------------------------------------------------
alter table plans_reader_calls add column if not exists provider           text;
alter table plans_reader_calls add column if not exists round_no           int;
alter table plans_reader_calls add column if not exists cache_write_tokens int;
alter table plans_reader_calls add column if not exists conservative_cost  boolean not null default false;
alter table plans_reader_calls add column if not exists message_id         uuid;
create index if not exists plans_reader_calls_group_idx on plans_reader_calls (group_id);

-- ---------------------------------------------------------------------------
-- Runs: run ceiling input, autonomy state (WS8), gate summary (WS4).
-- ---------------------------------------------------------------------------
alter table plans_reader_runs add column if not exists estimated_cost_usd_max numeric;
alter table plans_reader_runs add column if not exists autonomy_state text not null default 'not_evaluated';
alter table plans_reader_runs drop constraint if exists plans_reader_runs_autonomy_state_check;
alter table plans_reader_runs add constraint plans_reader_runs_autonomy_state_check
  check (autonomy_state in ('not_evaluated', 'in_progress', 'blocked',
                            'needs_review', 'ready_for_verification'));
alter table plans_reader_runs add column if not exists gate_summary jsonb;

-- ---------------------------------------------------------------------------
-- Tool loop rounds and tool calls (WS2 shapes, spec 4.3 and 5.6).
-- ---------------------------------------------------------------------------
create table if not exists plans_reader_loop_rounds (
  id             uuid primary key default gen_random_uuid(),
  run_id         uuid not null references plans_reader_runs(id) on delete cascade,
  group_id       uuid references plans_reader_groups(id) on delete cascade,
  message_id     uuid references plans_reader_chat_messages(id) on delete cascade,
  round_no       int not null,
  status         text not null default 'sent'
                 check (status in ('sent', 'done', 'failed', 'abandoned')),
  request_sha256 text,
  est_usd        numeric,
  response_id    text,
  assistant_raw  jsonb,
  tool_calls     jsonb,
  usage          jsonb,
  cost_usd       numeric,
  error          text,
  created_at     timestamptz not null default now(),
  updated_at     timestamptz not null default now(),
  check (group_id is not null or message_id is not null)
);
create unique index if not exists plans_reader_loop_rounds_uq
  on plans_reader_loop_rounds (coalesce(group_id, message_id), round_no)
  where status <> 'abandoned';
create index if not exists plans_reader_loop_rounds_run_idx
  on plans_reader_loop_rounds (run_id, created_at);

create table if not exists plans_reader_tool_calls (
  id          uuid primary key default gen_random_uuid(),
  run_id      uuid not null references plans_reader_runs(id) on delete cascade,
  group_id    uuid references plans_reader_groups(id) on delete cascade,
  message_id  uuid references plans_reader_chat_messages(id) on delete cascade,
  round_no    int not null,
  call_id     text not null,
  tool_name   text not null,
  args        jsonb,
  result_text text,
  result_data jsonb,
  citations   jsonb,
  views       jsonb,
  refused     boolean not null default false,
  duration_ms int,
  created_at  timestamptz not null default now(),
  check (group_id is not null or message_id is not null)
);
create unique index if not exists plans_reader_tool_calls_uq
  on plans_reader_tool_calls (coalesce(group_id, message_id), call_id);
create index if not exists plans_reader_tool_calls_run_idx
  on plans_reader_tool_calls (run_id, created_at);

-- ---------------------------------------------------------------------------
-- Integrity gate findings (WS4 shape; the pricing stage is written by WS6
-- through WS4's record_findings).
-- ---------------------------------------------------------------------------
create table if not exists plans_reader_gate_findings (
  id               uuid primary key default gen_random_uuid(),
  run_id           uuid not null references plans_reader_runs(id) on delete cascade,
  output_version   int not null default 1,
  stage            text not null check (stage in ('pre', 'final', 'pricing')),
  gate_id          text not null,
  severity         text not null check (severity in ('block', 'warn')),
  route            text not null default 'none'
                   check (route in ('recount', 'measure', 'model_fix', 'human', 'none')),
  status           text not null default 'open'
                   check (status in ('open', 'routed', 'resolved', 'overridden', 'superseded')),
  target           jsonb,
  message          text,
  evidence         jsonb,
  fingerprint      text not null,
  routed_group_id  uuid references plans_reader_groups(id) on delete set null,
  resolved_by      uuid references profiles(id) on delete set null,
  resolved_at      timestamptz,
  note             text,
  created_at       timestamptz not null default now(),
  updated_at       timestamptz not null default now(),
  unique (run_id, output_version, stage, fingerprint)
);
create index if not exists plans_reader_gate_findings_run_idx
  on plans_reader_gate_findings (run_id, stage, status);

-- ---------------------------------------------------------------------------
-- Chat: the sender's role decides the pricing clause; answers produced with
-- an internal_pricing tool are redacted for non-pricing readers (spec 5.5).
-- ---------------------------------------------------------------------------
alter table plans_reader_chat_messages add column if not exists sender_role        text;
alter table plans_reader_chat_messages add column if not exists pricing_restricted boolean not null default false;

-- ---------------------------------------------------------------------------
-- RLS deny-by-default (service-role backend bypasses it); see 0007 / 0055.
-- ---------------------------------------------------------------------------
alter table plans_reader_loop_rounds enable row level security;
alter table plans_reader_loop_rounds force row level security;
alter table plans_reader_tool_calls enable row level security;
alter table plans_reader_tool_calls force row level security;
alter table plans_reader_gate_findings enable row level security;
alter table plans_reader_gate_findings force row level security;
revoke all on plans_reader_loop_rounds from anon, authenticated;
revoke all on plans_reader_tool_calls from anon, authenticated;
revoke all on plans_reader_gate_findings from anon, authenticated;

notify pgrst, 'reload schema';
