-- 0107: Saved New Bid drafts.
--
-- A draft is the New Bid intake form parked before the project exists. Saving
-- one has NO side effects: no project row, no number reservation, no emails,
-- nothing downstream - deleting a draft erases every trace of it. Only name
-- and number are required; the rest of the form rides in `data` verbatim so
-- the frontend can re-load exactly what the user typed.

create table bid_drafts (
  id         uuid primary key default gen_random_uuid(),
  name       text not null,
  -- Deliberately NOT unique: a draft reserves nothing. Two drafts may carry
  -- the same number, and POST /projects still 409s a duplicate number at
  -- create time (projects_number_unique_idx, 0052).
  number     text not null,
  -- The rest of the intake form verbatim - fields/isNgem/noBiddingUrl/
  -- selectedGcs/gcNeedsBy - opaque to the backend except for the confidential
  -- fields.actual_bid_at redaction (routers/bid_drafts.py).
  data       jsonb not null default '{}'::jsonb,
  -- Who saved it. Actor set-null convention (0012): drafts are a shared team
  -- resource and must survive their creator's departure.
  created_by uuid references profiles(id) on delete set null,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);
create index bid_drafts_updated_idx on bid_drafts(updated_at desc);
create trigger bid_drafts_updated_at before update on bid_drafts
  for each row execute function set_updated_at();

-- RLS deny-by-default + FORCE (0055 / 0093 posture): all access is enforced in
-- the FastAPI layer and the service-role key bypasses it.
alter table bid_drafts enable row level security;
alter table bid_drafts force row level security;

notify pgrst, 'reload schema';
