-- 0033 — Win/Loss (bid outcome): the final step after a bid is submitted. The PA
-- records what actually happened to the bid once GCs report back. G3 bids the same
-- job to several GCs, so the outcome is two independent facts PER GC: did that GC
-- win the overall job, and did that GC go with our number? The overall result for
-- G3 (won/lost/no_award) and a free-text note live on the project-level row.

-- New terminal stage after 'submitted'. PG12+ allows ADD VALUE in a transaction so
-- long as the new label is not used later in the same transaction — this migration
-- never references 'bid_outcome' (same trick 0024 used for 'proposal').
alter type project_stage add value if not exists 'bid_outcome';

-- G3's overall outcome on the bid.
create type bid_result as enum ('won', 'lost', 'no_award');
-- Did this GC win the overall job? ('unknown' = we don't know yet / never heard).
create type gc_award_result as enum ('won', 'lost', 'unknown');
-- Did this GC go with our number, or a competitor's?
create type our_bid_selection as enum ('used_us', 'used_other', 'unknown');

-- One row per project: the overall closeout the PA records (Win/Loss step).
create table bid_outcomes (
  id            uuid primary key default gen_random_uuid(),
  project_id    uuid not null unique references projects(id) on delete cascade,
  result        bid_result not null,
  -- The GC that ultimately won the job (optional — may be unknown, or a GC we
  -- didn't bid to, in which case it's left null). SET NULL keeps the outcome row
  -- if that GC is later deleted.
  winning_gc_id uuid references general_contractors(id) on delete set null,
  notes         text,
  recorded_by   uuid references profiles(id) on delete set null,
  recorded_at   timestamptz not null default now(),
  updated_at    timestamptz not null default now()
);
create index bid_outcomes_winning_gc_idx on bid_outcomes(winning_gc_id);
create index bid_outcomes_recorded_by_idx on bid_outcomes(recorded_by);
create trigger bid_outcomes_updated_at before update on bid_outcomes
  for each row execute function set_updated_at();

-- One row per GC we bid to: the per-GC detail. Every field is optional /
-- unknown-tolerant — the PA records what they've heard, which is often partial.
-- our_amount is a snapshot of what we bid that GC (proposal_sends material+labor)
-- taken at record time, so "how far off" stays correct even if pricing changes.
-- gc_id is RESTRICT like proposal_sends: a GC with recorded bid history can't be
-- silently deleted out from under the outcome.
create table bid_gc_outcomes (
  id                uuid primary key default gen_random_uuid(),
  project_id        uuid not null references projects(id) on delete cascade,
  gc_id             uuid not null references general_contractors(id) on delete restrict,
  gc_award_result   gc_award_result not null default 'unknown',
  our_bid_selection our_bid_selection not null default 'unknown',
  our_amount        numeric(14,2),
  winning_amount    numeric(14,2) check (winning_amount >= 0),
  created_at        timestamptz not null default now(),
  updated_at        timestamptz not null default now(),
  unique (project_id, gc_id)
);
create index bid_gc_outcomes_project_idx on bid_gc_outcomes(project_id);
create index bid_gc_outcomes_gc_idx on bid_gc_outcomes(gc_id);
create trigger bid_gc_outcomes_updated_at before update on bid_gc_outcomes
  for each row execute function set_updated_at();

-- RLS deny-by-default (the service-role backend bypasses it); see 0007.
alter table bid_outcomes    enable row level security;
alter table bid_gc_outcomes enable row level security;
