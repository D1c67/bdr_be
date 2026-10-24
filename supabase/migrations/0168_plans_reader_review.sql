-- 0168 plans_reader_review (MF in docs/AI_ESTIMATING_BUILD_SPEC.md 5.6, WS7)
--
-- The independent review of a priced AI estimate, its findings, the blind
-- recount targets and outcomes, and the persisted history bands.
--
--   plans_reader_reviews          one review per (run, base pricing version);
--                                 verdict clear | advisory | needs_human |
--                                 incomplete | skipped | stale (spec 5.4
--                                 ReviewResult); `result` holds the full
--                                 ReviewResult, `models` the model per stage.
--   plans_reader_review_findings  one row per finding of a review (rule, band,
--                                 gate, scope, recount or reviewer origin),
--                                 with the human label of the verify view
--                                 (human_label, note, labeled_by, labeled_at).
--   plans_reader_recounts         one row per recount target of a run
--                                 (pass 3 runs in READING, before any review,
--                                 so rows key on the run; review_id is set
--                                 when a review requested the recount).
--   ai_history_bands              one row per (band_version, metric, stratum)
--                                 from plans_reader_bands.persist_bands; at
--                                 most one active band_version.
--
-- Applied to DEV only during the build (letter order MA..MG = 0163..0169;
-- needs 0165 for the recount, review and revise group kinds). Idempotent:
-- safe to re-run. Service role only: RLS enabled and forced, no policies,
-- nothing granted to anon or authenticated. Money fields (impact_usd, the
-- bid_per_device band values) are internal and served only behind
-- require_pricing_view.

-- ---------------------------------------------------------------------------
-- Reviews
-- ---------------------------------------------------------------------------
create table if not exists plans_reader_reviews (
  id                      uuid primary key default gen_random_uuid(),
  run_id                  uuid not null references plans_reader_runs(id) on delete cascade,
  base_pricing_version    int not null,
  revised_pricing_version int,
  mode                    text not null default 'shadow'
                          check (mode in ('shadow', 'apply')),
  stage                   text not null default 'rules'
                          check (stage in ('rules', 'review', 'revise', 'recheck',
                                           'done', 'failed', 'partial')),
  verdict                 text
                          check (verdict is null or verdict in ('clear', 'advisory', 'needs_human',
                                                                'incomplete', 'skipped', 'stale')),
  reasons                 jsonb not null default '[]'::jsonb,
  result                  jsonb,
  models                  jsonb not null default '{}'::jsonb,
  budget_usd              numeric(10, 2) not null default 4.00
                          check (budget_usd >= 0 and budget_usd <= 6),
  cost_usd                numeric(12, 6) not null default 0,
  band_version            text,
  error                   text,
  created_at              timestamptz not null default now(),
  updated_at              timestamptz not null default now(),
  finished_at             timestamptz,
  unique (run_id, base_pricing_version)
);
create index if not exists plans_reader_reviews_run_idx
  on plans_reader_reviews (run_id, created_at desc);

comment on table plans_reader_reviews is
  'WS7 independent review of one priced version of an AI Estimating run (one review per base pricing version; a review revision never starts another review).';

-- ---------------------------------------------------------------------------
-- Review findings
-- ---------------------------------------------------------------------------
create table if not exists plans_reader_review_findings (
  id                 uuid primary key default gen_random_uuid(),
  review_id          uuid not null references plans_reader_reviews(id) on delete cascade,
  run_id             uuid not null references plans_reader_runs(id) on delete cascade,
  finding_id         text not null,
  origin             text not null
                     check (origin in ('rule', 'band', 'gate', 'scope', 'recount', 'reviewer')),
  rule_id            text,
  kind               text not null,
  severity           text not null check (severity in ('critical', 'warning', 'info')),
  title              text not null,
  detail             text,
  line_ids           jsonb not null default '[]'::jsonb,
  evidence           jsonb not null default '[]'::jsonb,
  evidence_verified  boolean not null default false,
  proposed_ops       jsonb not null default '[]'::jsonb,
  impact_usd         numeric(14, 2),
  impact_hours       numeric(12, 2),
  disposition        text not null default 'open'
                     check (disposition in ('open', 'applied', 'declined', 'contested',
                                            'rejected_by_rule', 'info_only')),
  disposition_reason text,
  disposition_by     text,
  post_revision      boolean not null default false,
  fingerprint        text,
  human_label        text check (human_label is null or human_label in ('valid', 'invalid', 'partly_valid')),
  note               text,
  labeled_by         uuid references profiles(id) on delete set null,
  labeled_at         timestamptz,
  created_at         timestamptz not null default now(),
  updated_at         timestamptz not null default now(),
  unique (review_id, finding_id)
);
create index if not exists plans_reader_review_findings_run_idx
  on plans_reader_review_findings (run_id, severity);

-- ---------------------------------------------------------------------------
-- Recount targets and outcomes
-- ---------------------------------------------------------------------------
create table if not exists plans_reader_recounts (
  id            uuid primary key default gen_random_uuid(),
  run_id        uuid not null references plans_reader_runs(id) on delete cascade,
  review_id     uuid references plans_reader_reviews(id) on delete set null,
  target_id     text not null,
  row_id        text not null,
  document_id   uuid not null,
  page          int not null,
  sheet_key     text,
  priority      text not null check (priority in ('P1', 'P2', 'P3', 'P4')),
  builder_qty   numeric,
  recount_qty   numeric,
  census_floor  numeric,
  tiebreak_qty  numeric,
  mean_conf     numeric,
  outcome       text check (outcome is null or outcome in ('confirmed', 'raise', 'lower', 'unresolved')),
  reason        text,
  marks         jsonb not null default '[]'::jsonb,
  model         text,
  group_id      uuid references plans_reader_groups(id) on delete set null,
  created_at    timestamptz not null default now(),
  updated_at    timestamptz not null default now(),
  unique (run_id, target_id)
);
create index if not exists plans_reader_recounts_group_idx
  on plans_reader_recounts (group_id);

-- ---------------------------------------------------------------------------
-- History bands (plans_reader_bands.persist_bands / load_active_bands)
-- "values" is quoted: VALUES is a reserved word.
-- ---------------------------------------------------------------------------
create table if not exists ai_history_bands (
  band_version     text not null,
  metric           text not null,
  stratum          text not null,
  n                int not null,
  p10              numeric,
  p25              numeric,
  p50              numeric,
  p75              numeric,
  p90              numeric,
  "values"         jsonb not null default '[]'::jsonb,
  zeros            jsonb not null default '[]'::jsonb,
  lopo             jsonb not null default '{}'::jsonb,
  basis            text not null default 'estimator_workbook',
  settings         jsonb not null default '{}'::jsonb,
  excluded_project_ids jsonb not null default '[]'::jsonb,
  manifest_sha256  text not null,
  built_at         timestamptz not null default now(),
  active           boolean not null default false,
  created_at       timestamptz not null default now(),
  primary key (band_version, metric, stratum)
);
create index if not exists ai_history_bands_active_idx
  on ai_history_bands (band_version) where active;

comment on table ai_history_bands is
  'WS7 history bands on the estimator basis (spec 8.7 bands population); values hold one {project_id, value} per project; at most one band_version is active.';

-- ---------------------------------------------------------------------------
-- RLS deny-by-default (service-role backend bypasses it); see 0007 / 0055.
-- ---------------------------------------------------------------------------
alter table plans_reader_reviews enable row level security;
alter table plans_reader_reviews force row level security;
alter table plans_reader_review_findings enable row level security;
alter table plans_reader_review_findings force row level security;
alter table plans_reader_recounts enable row level security;
alter table plans_reader_recounts force row level security;
alter table ai_history_bands enable row level security;
alter table ai_history_bands force row level security;
revoke all on plans_reader_reviews from anon, authenticated;
revoke all on plans_reader_review_findings from anon, authenticated;
revoke all on plans_reader_recounts from anon, authenticated;
revoke all on ai_history_bands from anon, authenticated;

notify pgrst, 'reload schema';
