-- 0142: Calling In (docs/CALLING_IN.md). Apply after 0141.
--
-- After we send proposals, someone calls every GC we bid to about our
-- proposal, once before the actual bid time (round pre_bid, "List 1") and
-- once in the 10 days after it (round post_bid, "List 2"). Which projects are
-- on which list is computed live from proposal_sends, projects.actual_bid_at
-- and the calls below; nothing here stores list membership as a flag.
--
--   call_in_calls     one row per logged call attempt (outcome, note, the
--                     GC contacts spoken to as a snapshot)
--   call_in_entries   one row per time a project lands on a list. The
--                     partial unique index is the multi-worker claim for the
--                     "landed on a list" notification: the worker whose
--                     insert wins is the only one that notifies.
--   call_in_meta      a single row whose started_at (its insert time) is the
--                     go-live marker the analytics count missed slots from.
--
-- Requires 0131 (projects.test_session_id): the Calling In reads select it.
--
-- notifications.type is unconstrained text (0006), so the two new types
-- (call_in_pre_bid, call_in_post_bid) need no registration here.
--
-- Every statement is idempotent.

-- 1. Calls --------------------------------------------------------------------

create table if not exists call_in_calls (
  id          uuid primary key default gen_random_uuid(),
  project_id  uuid not null references projects(id) on delete cascade,
  -- RESTRICT like proposal_sends: a GC with call history is not silently
  -- deleted out from under it.
  gc_id       uuid not null references general_contractors(id) on delete restrict,
  round       text not null
              constraint call_in_calls_round_check
              check (round in ('pre_bid', 'post_bid')),
  -- Only 'spoke' marks the GC done for the round; the others are attempts.
  outcome     text not null
              constraint call_in_calls_outcome_check
              check (outcome in ('spoke', 'voicemail', 'no_answer')),
  note        text not null
              constraint call_in_calls_note_check
              check (length(btrim(note)) > 0 and char_length(note) <= 4000),
  -- Snapshot of who was called: [{gc_contact_id, name, phone, email}], at
  -- least one. gc_contact_id is null once that contact no longer exists.
  contacts    jsonb not null
              constraint call_in_calls_contacts_check
              check (jsonb_typeof(contacts) = 'array' and jsonb_array_length(contacts) > 0),
  called_at   timestamptz not null default now(),
  -- Actor convention (see 0012): keep the call if the author is deleted.
  created_by  uuid references profiles(id) on delete set null,
  edited_by   uuid references profiles(id) on delete set null,
  created_at  timestamptz not null default now(),
  updated_at  timestamptz not null default now()
);

create index if not exists call_in_calls_project_round_gc_idx
  on call_in_calls (project_id, round, gc_id);
create index if not exists call_in_calls_called_at_idx
  on call_in_calls (called_at);
create index if not exists call_in_calls_gc_idx on call_in_calls (gc_id);
create index if not exists call_in_calls_created_by_idx on call_in_calls (created_by);
create index if not exists call_in_calls_edited_by_idx on call_in_calls (edited_by);

drop trigger if exists call_in_calls_updated_at on call_in_calls;
create trigger call_in_calls_updated_at before update on call_in_calls
  for each row execute function set_updated_at();

comment on table call_in_calls is
  'Calling In: one row per logged call to a GC about our proposal, per round (pre_bid before the actual bid time, post_bid in the 10 days after). Only outcome spoke marks the GC done for that round.';
comment on column call_in_calls.contacts is
  'Snapshot array of the GC contacts on the call: [{gc_contact_id, name, phone, email}], at least one.';

-- 2. List entries (the notification claim) -----------------------------------

create table if not exists call_in_entries (
  id            uuid primary key default gen_random_uuid(),
  project_id    uuid not null references projects(id) on delete cascade,
  round         text not null
                constraint call_in_entries_round_check
                check (round in ('pre_bid', 'post_bid')),
  opened_at     timestamptz not null default now(),
  -- Null when the entry was claimed silently (the 24-hour burst guard).
  notified_at   timestamptz,
  closed_at     timestamptz,
  close_reason  text
                constraint call_in_entries_close_reason_check
                check (close_reason is null or close_reason in ('cleared', 'window_closed', 'left')),
  created_at    timestamptz not null default now(),
  updated_at    timestamptz not null default now(),
  constraint call_in_entries_closed_reason_pair
    check ((closed_at is null) = (close_reason is null))
);

-- The claim: at most one OPEN entry per project and round. The poller in
-- every uvicorn worker inserts; only the insert that wins notifies.
create unique index if not exists call_in_entries_one_open
  on call_in_entries (project_id, round) where closed_at is null;
create index if not exists call_in_entries_project_idx
  on call_in_entries (project_id, opened_at desc);

drop trigger if exists call_in_entries_updated_at on call_in_entries;
create trigger call_in_entries_updated_at before update on call_in_entries
  for each row execute function set_updated_at();

comment on table call_in_entries is
  'Calling In: one row per time a project lands on a list (round). Open while closed_at is null; the partial unique index call_in_entries_one_open is the multi-worker notification claim.';

-- 3. Go-live marker ------------------------------------------------------------
--
-- One row. Its started_at is when Calling In went live on this database (the
-- insert below runs when this migration is applied). Analytics count a missed
-- call slot only when its window closed at or after started_at, so bids that
-- closed before the feature existed never read as missed. A missing row reads
-- as now in the backend (nothing historic counts).

create table if not exists call_in_meta (
  -- Singleton guard: the key can only ever be true, so at most one row.
  id          boolean primary key default true
              constraint call_in_meta_singleton check (id),
  started_at  timestamptz not null default now(),
  created_at  timestamptz not null default now()
);

insert into call_in_meta (id) values (true) on conflict (id) do nothing;

comment on table call_in_meta is
  'Calling In: single row. started_at is the go-live moment; analytics only count missed slots whose call window closed at or after it.';

-- 4. RLS deny-by-default (the service-role backend bypasses it); see 0007 / 0055.

alter table call_in_calls enable row level security;
alter table call_in_calls force row level security;
alter table call_in_entries enable row level security;
alter table call_in_entries force row level security;
alter table call_in_meta enable row level security;
alter table call_in_meta force row level security;
