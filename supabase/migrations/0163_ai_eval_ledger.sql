-- 0163 (MA) - AI Estimating eval ledger: the $400 live spend ceiling of the
-- autonomous build (docs/AI_ESTIMATING_BUILD_SPEC.md 0.4, 5.6 MA, 8.4).
--
-- DEV ONLY (Supabase project bpidntbyvoooqvaispup). This migration never
-- ships to staging or prod: only eval code reads these tables, and only in
-- a process with AI_EVAL_LEDGER_ENFORCE=true (services/eval_ledger.py,
-- scripts/eval_ledger.py).
--
-- ai_eval_budget       single row: the ceiling (at most 400 USD), the epoch
--                      the build's spend counts from, and `enforce`, which
--                      records that the operator armed the ledger on the
--                      sweep backend (informational; the guard is switched
--                      by the process setting).
-- ai_eval_allocations  one row per workstream (WS2..WS8, shared,
--                      contingency); the sum stays at or under the ceiling
--                      (checked by eval_ledger.allocate). Contingency is
--                      released only by allocate with an owner-visible note.
-- ai_eval_sweeps       one row per live sweep; at most one open at a time
--                      (partial unique index). spent_usd is frozen at close
--                      from llm_call_log.eval_sweep_id (that column arrives
--                      with MC).
--
-- Idempotent: every statement can run twice. Service role only: RLS on and
-- forced, no policies, nothing granted to anon or authenticated.

create table if not exists public.ai_eval_budget (
  id          smallint primary key default 1 check (id = 1),
  ceiling_usd numeric(10, 2) not null default 400
              check (ceiling_usd > 0 and ceiling_usd <= 400),
  epoch_at    timestamptz not null default now(),
  enforce     boolean not null default false,
  note        text,
  updated_at  timestamptz not null default now()
);

insert into public.ai_eval_budget (id, ceiling_usd, enforce, note)
values (1, 400, false, 'Seeded by 0163: $400 live spend ceiling of the AI Estimating autonomous build.')
on conflict (id) do nothing;

create table if not exists public.ai_eval_allocations (
  workstream    text primary key,
  allocated_usd numeric(10, 2) not null check (allocated_usd >= 0),
  note          text,
  updated_at    timestamptz not null default now()
);

-- Section 8.4 allocations (sum 400).
insert into public.ai_eval_allocations (workstream, allocated_usd, note) values
  ('WS2', 29, 'L1 $3, L2 $20, L2 repeats $6'),
  ('WS3', 6, 'assist probes on 6 pool projects'),
  ('WS4', 26, 'consolidator iterations $12, main A/B $14'),
  ('WS5', 6, 'attribution $3, digest re-run $3'),
  ('WS6', 8, 'quote fallback $4, chat $4'),
  ('WS7', 28, 'recount live and tiebreak $8, reviewer gauntlet $15, revise $5'),
  ('WS8', 5, 'chat $3, translation $2'),
  ('shared', 278, 'WS0 runs: integration 7154 $45, noise $18, smoke $45, core $97, holdout $73'),
  ('contingency', 14, 'released only by allocate with an owner-visible note')
on conflict (workstream) do nothing;

create table if not exists public.ai_eval_sweeps (
  id           uuid primary key default gen_random_uuid(),
  label        text not null,
  workstream   text not null references public.ai_eval_allocations (workstream),
  purpose      text not null
               check (purpose in ('dev_probe', 'smoke', 'core', 'holdout', 'noise', 'ab')),
  budget_usd   numeric(10, 2) not null check (budget_usd > 0),
  status       text not null default 'open'
               check (status in ('open', 'closed', 'aborted')),
  opened_at    timestamptz not null default now(),
  closed_at    timestamptz,
  spent_usd    numeric(12, 6) check (spent_usd is null or spent_usd >= 0),
  code_version text,
  prompt_hash  text,
  config       jsonb not null default '{}'::jsonb,
  cases        jsonb not null default '[]'::jsonb,
  result_path  text
);

-- At most one open sweep (spec 0.4 rule 2).
create unique index if not exists ai_eval_sweeps_one_open
  on public.ai_eval_sweeps ((status)) where status = 'open';
create index if not exists ai_eval_sweeps_opened_idx
  on public.ai_eval_sweeps (opened_at desc);

comment on table public.ai_eval_budget is
  'Dev only. Single row: the $400 live spend ceiling of the AI Estimating autonomous build.';
comment on table public.ai_eval_allocations is
  'Dev only. Live spend allocation per workstream (section 8.4); contingency moves only with a note.';
comment on table public.ai_eval_sweeps is
  'Dev only. One row per live eval sweep; one open at a time; spent_usd frozen at close.';

-- RLS deny-by-default (the service-role backend bypasses it); see 0007 / 0055.
alter table public.ai_eval_budget enable row level security;
alter table public.ai_eval_budget force row level security;
alter table public.ai_eval_allocations enable row level security;
alter table public.ai_eval_allocations force row level security;
alter table public.ai_eval_sweeps enable row level security;
alter table public.ai_eval_sweeps force row level security;
revoke all on table public.ai_eval_budget from anon, authenticated;
revoke all on table public.ai_eval_allocations from anon, authenticated;
revoke all on table public.ai_eval_sweeps from anon, authenticated;

notify pgrst, 'reload schema';
