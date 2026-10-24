-- 0166 (MD) - G3 scope policy (docs/AI_ESTIMATING_BUILD_SPEC.md 5.6 MD, WS5).
--
-- Applied to DEV only during the build (letter order MA..MG = 0163..0169).
--
-- ai_scope_policies             versioned policy documents (scope_policy.validate
--                               shape). status draft | active | archived; at most
--                               one active row (partial unique index). sha256 is
--                               scope_policy.policy_sha(doc). Estimating Admin,
--                               Executive and IT Admin edit drafts (only while
--                               AI_ESTIMATING_STATE=live); Executive and IT Admin
--                               activate (Q24).
-- plans_reader_runs             scope_policy_id, scope_policy_sha and the run's
--                               BidConditions (bid_conditions jsonb), stamped by
--                               scope_engine.prepare. All nullable; code omits the
--                               keys while their value is null.
-- plans_reader_scope_overrides  one human decision per (run, decision_key):
--                               include | exclude | accept with a written reason
--                               (routers/scope_policy.py; AI_GATE_OVERRIDE_ROLES).
--
-- Idempotent: every statement can run twice. Service role only: RLS on and
-- forced, no policies, nothing granted to anon or authenticated.

create table if not exists public.ai_scope_policies (
  id           uuid primary key default gen_random_uuid(),
  version      integer not null check (version >= 1),
  status       text not null default 'draft'
               check (status in ('draft', 'active', 'archived')),
  doc          jsonb not null,
  sha256       text not null check (sha256 ~ '^[0-9a-f]{64}$'),
  based_on     uuid references public.ai_scope_policies (id) on delete set null,
  notes        text,
  created_by   uuid references public.profiles (id) on delete set null,
  created_at   timestamptz not null default now(),
  activated_by uuid references public.profiles (id) on delete set null,
  activated_at timestamptz,
  constraint ai_scope_policies_version_key unique (version)
);

-- At most one active policy.
create unique index if not exists ai_scope_policies_one_active
  on public.ai_scope_policies ((status)) where status = 'active';
create index if not exists ai_scope_policies_created_idx
  on public.ai_scope_policies (created_at desc);

comment on table public.ai_scope_policies is
  'G3 scope policy versions (WS5). One active row; drafts edited by Estimating Admin, Executive, IT Admin; activated by Executive or IT Admin.';

alter table public.plans_reader_runs add column if not exists scope_policy_id uuid;
alter table public.plans_reader_runs add column if not exists scope_policy_sha text;
alter table public.plans_reader_runs add column if not exists bid_conditions jsonb;

do $$
begin
  if not exists (
    select 1 from pg_constraint where conname = 'plans_reader_runs_scope_policy_id_fkey'
  ) then
    alter table public.plans_reader_runs
      add constraint plans_reader_runs_scope_policy_id_fkey
      foreign key (scope_policy_id) references public.ai_scope_policies (id) on delete set null;
  end if;
end $$;

comment on column public.plans_reader_runs.scope_policy_id is
  'ai_scope_policies row the run was prepared with (null = the code seed).';
comment on column public.plans_reader_runs.scope_policy_sha is
  'sha256 of the scope policy document the run was prepared with.';
comment on column public.plans_reader_runs.bid_conditions is
  'BidConditions of the run (spec 5.4): wage type and basis, labor time, union, bond, rate table key.';

create table if not exists public.plans_reader_scope_overrides (
  id           uuid primary key default gen_random_uuid(),
  run_id       uuid not null references public.plans_reader_runs (id) on delete cascade,
  decision_key text not null check (length(decision_key) between 3 and 300),
  action       text not null check (action in ('include', 'exclude', 'accept')),
  note         text check (note is null or length(btrim(note)) >= 10),
  created_by   uuid references public.profiles (id) on delete set null,
  created_at   timestamptz not null default now(),
  constraint plans_reader_scope_overrides_run_key unique (run_id, decision_key)
);

create index if not exists plans_reader_scope_overrides_run_idx
  on public.plans_reader_scope_overrides (run_id);

comment on table public.plans_reader_scope_overrides is
  'Human scope decisions per run (include, exclude, accept) with a written reason; applied by scope_engine.enforce.';

-- RLS deny-by-default (the service-role backend bypasses it); see 0007 / 0055.
alter table public.ai_scope_policies enable row level security;
alter table public.ai_scope_policies force row level security;
alter table public.plans_reader_scope_overrides enable row level security;
alter table public.plans_reader_scope_overrides force row level security;
revoke all on table public.ai_scope_policies from anon, authenticated;
revoke all on table public.plans_reader_scope_overrides from anon, authenticated;

notify pgrst, 'reload schema';
