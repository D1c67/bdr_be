-- 0169 plans_reader_evidence_signoff (MG in docs/AI_ESTIMATING_BUILD_SPEC.md 5.6, WS8)
--
-- The human price verification record of an AI Estimating run: a verifier
-- in VERIFY_ROLES records `verified` or `rejected` for ONE priced version
-- (plans_reader_pricing.version of the run, migration 0167), with a note and
-- the autonomy status and totals as they were at sign-off time (spec 1.4).
-- A newer priced version supersedes older sign-offs on read (no update).
--
-- The evidence output itself needs no table: it is a plans_reader_outputs
-- row with scope 'evidence' (the scope CHECK is widened by 0165), and the
-- `annotated_plans` deliverable kind is added by 0165 too.
--
-- Applied to DEV only during the build (letter order MA..MG = 0163..0169).
-- Idempotent: safe to re-run. RLS deny-by-default: the service-role backend
-- bypasses it; anon and authenticated get nothing (see 0007 / 0055).
--
-- pricing_version is deliberately not a foreign key: plans_reader_pricing is
-- keyed by (run_id, version) in 0167 and a sign-off must survive a pricing
-- row being pruned; the route validates the version before inserting.

create table if not exists plans_reader_price_signoffs (
  id                 uuid primary key default gen_random_uuid(),
  run_id             uuid not null references plans_reader_runs(id) on delete cascade,
  pricing_version    int not null check (pricing_version >= 1),
  decision           text not null check (decision in ('verified', 'rejected')),
  note               text,
  autonomy_snapshot  jsonb not null default '{}'::jsonb,
  totals_snapshot    jsonb not null default '{}'::jsonb,
  created_by         uuid references profiles(id) on delete set null,
  created_at         timestamptz not null default now()
);

create index if not exists plans_reader_price_signoffs_run_idx
  on plans_reader_price_signoffs (run_id, created_at desc);
create index if not exists plans_reader_price_signoffs_version_idx
  on plans_reader_price_signoffs (run_id, pricing_version);

comment on table plans_reader_price_signoffs is
  'Human sign-off of one AI priced version (spec 1.4): verified or rejected with a note, plus the autonomy and totals snapshots taken at sign-off. Internal pricing data: served only to pricing roles.';
comment on column plans_reader_price_signoffs.pricing_version is
  'plans_reader_pricing.version of the same run that was signed. A later version supersedes this sign-off on read.';
comment on column plans_reader_price_signoffs.autonomy_snapshot is
  'AutonomyStatus (plans_reader_autonomy.status) at sign-off time: overall state, the eight checks and the accepted failing checks.';
comment on column plans_reader_price_signoffs.totals_snapshot is
  'Hours, material, Verify bid and bid total of the signed priced version at sign-off time.';

alter table plans_reader_price_signoffs enable row level security;
alter table plans_reader_price_signoffs force row level security;
revoke all on plans_reader_price_signoffs from anon, authenticated;

notify pgrst, 'reload schema';
