-- 0136 - RFP Ingestion: BuildingConnected Bid Board (docs/RFP_BUILDINGCONNECTED.md section 5, scratchpad BUILD_CONTRACT.md section 2). Apply after 0135. DEV ONLY until the release steps in section 9 run.
--
-- BuildingConnected invitations arrive through the Autodesk Platform
-- Services API (OAuth, three-legged), not through email or a scraped portal.
-- Each opportunity on the Bid Board becomes one rfp_portal_invitations row
-- (portal 'buildingconnected'), goes through the shared match step and, when
-- nothing matches, becomes a project with no files (the files flag tells the
-- team to go and get them). This migration:
--
--   1. adds the BuildingConnected columns to rfp_portal_invitations (all
--      nullable, so NGEM rows are untouched): the raw mirror and its hash,
--      the dates, the trade package, the GC as BuildingConnected names it
--      and as we resolved it, the lead contact, who ignored the row
--      (user or system), the project_gcs link an `exists` merge made, and
--      the package sibling the row was linked to;
--   2. widens the invitation status check with the parked statuses
--      `historical`, `expired`, `withdrawn` (dropped by column lookup, the
--      0124 / 0130 / 0132 pattern);
--   3. indexes the BuildingConnected lookups;
--   4. gives rfp_portal_runs a `kind` (incremental or full sync), the high
--      water mark it started from and four row counters, and recreates the
--      slot index as unique (portal, kind, scheduled_for) so an incremental
--      slot and a full sync slot never collide;
--   5. adds rfp_portal_state (the per-portal high water mark);
--   6. adds rfp_oauth_connections (one row per provider: the tokens, who
--      connected, the refresh lock);
--   7. adds rfp_oauth_states (the OAuth `state` values, delete on use);
--   8. adds gc_external_aliases (a portal company id confirmed as one of
--      our GCs, asked once);
--   9. adds the BuildingConnected project facts and the files flag to
--      projects;
--  10. widens rfp_created_projects.gc_plan with `provisional`.
--
-- No new queue job type: the scan runs as `rfp_portal_scan` and the run
-- row's `kind` says what to do, so llm_jobs_job_type_check is not touched.
-- Bell types need no SQL (notifications.type has no check).
--
-- Every statement is idempotent.

-- ── 1. rfp_portal_invitations: BuildingConnected columns ─────────────────

alter table rfp_portal_invitations
  add column if not exists external_id        text,
  add column if not exists external_url       text,
  add column if not exists payload            jsonb,
  add column if not exists payload_hash       text,
  add column if not exists bc_updated_at      timestamptz,
  add column if not exists invited_at         timestamptz,
  add column if not exists job_walk_at        timestamptz,
  add column if not exists expected_start_at  timestamptz,
  add column if not exists expected_finish_at timestamptz,
  add column if not exists rfis_due_at        timestamptz,
  add column if not exists address            text,
  add column if not exists trade_name         text,
  add column if not exists submission_state   text,
  add column if not exists workflow_bucket    text,
  add column if not exists source             text,
  add column if not exists request_type       text,
  add column if not exists is_archived        boolean,
  add column if not exists is_nda_required    boolean,
  add column if not exists is_sealed          boolean,
  add column if not exists gc_external_id     text,
  add column if not exists gc_external_name   text,
  add column if not exists gc_id              uuid references general_contractors(id) on delete set null,
  add column if not exists gc_kind            text,
  add column if not exists gc_candidates      jsonb,
  add column if not exists gc_confirmed_at    timestamptz,
  add column if not exists gc_confirmed_by    uuid references profiles(id) on delete set null,
  add column if not exists lead               jsonb,
  add column if not exists ignore_source      text,
  add column if not exists project_gc_id      uuid references project_gcs(id) on delete set null,
  add column if not exists sibling_of         uuid references rfp_portal_invitations(id) on delete set null;

alter table rfp_portal_invitations drop constraint if exists rfp_portal_invitations_gc_kind_check;
alter table rfp_portal_invitations add constraint rfp_portal_invitations_gc_kind_check
  check (gc_kind in ('alias', 'contact', 'domain', 'provisional', 'none'));

alter table rfp_portal_invitations drop constraint if exists rfp_portal_invitations_ignore_source_check;
alter table rfp_portal_invitations add constraint rfp_portal_invitations_ignore_source_check
  check (ignore_source in ('user', 'system'));

comment on column rfp_portal_invitations.external_id is
  'The portal''s own id for the invitation (BuildingConnected opportunity id). Also stored in bid_number so the existing (portal, agency_key, bid_number) key holds.';
comment on column rfp_portal_invitations.payload is
  'The full opportunity as last pulled (the raw mirror). Returned by the API to IT Admins only.';
comment on column rfp_portal_invitations.payload_hash is
  'sha256 of the canonical JSON of payload, so a scan skips no-op updates.';
comment on column rfp_portal_invitations.gc_kind is
  'How gc_id was resolved: alias (confirmed alias table), contact or domain (lead email), provisional (best name guess, not yet confirmed) or none.';
comment on column rfp_portal_invitations.gc_candidates is
  'Top GC guesses by name, [{gc_id, name, score}], shown when the GC is not yet confirmed.';
comment on column rfp_portal_invitations.lead is
  'The GC lead contact as the portal gives it: {first_name, last_name, email, phone}.';
comment on column rfp_portal_invitations.ignore_source is
  'Who ignored the row: user (a reviewer, ignored_by set) or system (declined or archived on the portal, ignored_by null).';
comment on column rfp_portal_invitations.project_gc_id is
  'The project_gcs link an exists merge inserted for this invitation (rfp_match_id null). Reopen removes the link by this id.';
comment on column rfp_portal_invitations.sibling_of is
  'The invitation this row was linked to as another package of the same project (same GC, same normalized name).';

-- ── 2. rfp_portal_invitations: the parked statuses ───────────────────────
-- historical (already past or not entered on first sight), expired (the due
-- date passed with no action) and withdrawn (gone from the board) are parked:
-- the sweep never touches them, a reviewer can restore one to `match`.

do $$
declare
  con record;
begin
  for con in
    select c.conname
    from pg_constraint c
    join pg_attribute a on a.attrelid = c.conrelid and a.attnum = any (c.conkey)
    where c.conrelid = 'public.rfp_portal_invitations'::regclass
      and c.contype = 'c'
      and a.attname = 'status'
      and array_length(c.conkey, 1) = 1
  loop
    execute format('alter table rfp_portal_invitations drop constraint %I', con.conname);
  end loop;

  alter table rfp_portal_invitations add constraint rfp_portal_invitations_status_check
    check (status in ('match', 'harvest', 'split', 'create', 'review_match', 'exists', 'done',
                      'created', 'ignored', 'historical', 'expired', 'withdrawn'));
end
$$;

-- ── 3. rfp_portal_invitations: indexes ───────────────────────────────────

create index if not exists rfp_portal_invitations_external_idx
  on rfp_portal_invitations (portal, external_id) where external_id is not null;
create index if not exists rfp_portal_invitations_status_close_idx
  on rfp_portal_invitations (portal, status, close_at);
create index if not exists rfp_portal_invitations_gc_external_idx
  on rfp_portal_invitations (gc_external_id) where gc_external_id is not null;
create index if not exists rfp_portal_invitations_project_gc_idx
  on rfp_portal_invitations (project_gc_id) where project_gc_id is not null;

-- ── 4. rfp_portal_runs: kind, high water, counters, the slot index ───────

alter table rfp_portal_runs
  add column if not exists kind              text not null default 'incremental',
  add column if not exists high_water_before timestamptz,
  add column if not exists rows_pulled       int not null default 0,
  add column if not exists rows_pipeline     int not null default 0,
  add column if not exists rows_expired      int not null default 0,
  add column if not exists rows_withdrawn    int not null default 0;

alter table rfp_portal_runs drop constraint if exists rfp_portal_runs_kind_check;
alter table rfp_portal_runs add constraint rfp_portal_runs_kind_check
  check (kind in ('incremental', 'full'));

comment on column rfp_portal_runs.kind is
  'incremental (changes since the high water mark, minus the overlap) or full (the whole board, the nightly sync). NGEM runs are always incremental.';
comment on column rfp_portal_runs.high_water_before is
  'The portal high water mark when the run started. Only a successful run moves rfp_portal_state.high_water_at.';

-- The slot is now per kind, so the nightly full sync never collides with
-- the incremental slot at the same instant. The active-run index (one
-- queued or running run per portal) stays as it is.
drop index if exists rfp_portal_runs_slot_uidx;
create unique index rfp_portal_runs_slot_uidx
  on rfp_portal_runs (portal, kind, scheduled_for) where scheduled_for is not null;

-- ── 5. rfp_portal_state: per-portal high water mark ──────────────────────

create table if not exists rfp_portal_state (
  portal              text primary key,
  high_water_at       timestamptz,
  last_full_sync_at   timestamptz,
  last_incremental_at timestamptz,
  created_at          timestamptz not null default now(),
  updated_at          timestamptz not null default now()
);

comment on table rfp_portal_state is
  'One row per portal: the high water mark the next incremental scan starts from, and when each kind of scan last succeeded.';

drop trigger if exists rfp_portal_state_updated_at on rfp_portal_state;
create trigger rfp_portal_state_updated_at before update on rfp_portal_state
  for each row execute function set_updated_at();

alter table rfp_portal_state enable row level security;
alter table rfp_portal_state force row level security;

-- ── 6. rfp_oauth_connections: one connection per provider ────────────────

create table if not exists rfp_oauth_connections (
  provider            text primary key,
  status              text not null default 'disconnected',
  access_token        text,
  refresh_token       text,
  expires_at          timestamptz,
  scope               text,
  connected_by        uuid references profiles(id) on delete set null,
  connected_at        timestamptz,
  external_user_id    text,
  external_user_name  text,
  external_user_email text,
  external_company_id text,
  view_all            boolean,
  last_refresh_at     timestamptz,
  last_used_at        timestamptz,
  last_error          text,
  refresh_lock_until  timestamptz,
  refresh_lock_owner  text,
  disconnected_at     timestamptz,
  created_at          timestamptz not null default now(),
  updated_at          timestamptz not null default now(),
  constraint rfp_oauth_connections_status_check check (status in ('connected', 'disconnected'))
);

comment on table rfp_oauth_connections is
  'Tokens are plain text; RLS forced, service role only. One row per provider (buildingconnected). The refresh lock (refresh_lock_until, refresh_lock_owner) makes one worker refresh at a time; the new refresh token is written in the same update that clears the lock.';

drop trigger if exists rfp_oauth_connections_updated_at on rfp_oauth_connections;
create trigger rfp_oauth_connections_updated_at before update on rfp_oauth_connections
  for each row execute function set_updated_at();

alter table rfp_oauth_connections enable row level security;
alter table rfp_oauth_connections force row level security;

-- ── 7. rfp_oauth_states: OAuth state values, delete on use ───────────────

create table if not exists rfp_oauth_states (
  state      text primary key,
  provider   text not null,
  actor_id   uuid references profiles(id) on delete cascade,
  created_at timestamptz not null default now(),
  expires_at timestamptz not null
);

comment on table rfp_oauth_states is
  'One row per started OAuth connect: the callback consumes (deletes) the state and refuses one that is missing or expired. Service role only.';

alter table rfp_oauth_states enable row level security;
alter table rfp_oauth_states force row level security;

-- ── 8. gc_external_aliases: a portal company confirmed as our GC ─────────

create table if not exists gc_external_aliases (
  id            uuid primary key default gen_random_uuid(),
  source        text not null,
  external_id   text not null,
  external_name text not null,
  gc_id         uuid not null references general_contractors(id) on delete cascade,
  confirmed_by  uuid references profiles(id) on delete set null,
  confirmed_at  timestamptz not null default now(),
  created_at    timestamptz not null default now(),
  updated_at    timestamptz not null default now()
);

comment on table gc_external_aliases is
  'A portal''s company id (source buildingconnected) confirmed by a person as one of our general contractors. Asked once per company; a name change on the portal only renames external_name.';

create unique index if not exists gc_external_aliases_uidx
  on gc_external_aliases (source, external_id);
create index if not exists gc_external_aliases_gc_idx
  on gc_external_aliases (gc_id);

drop trigger if exists gc_external_aliases_updated_at on gc_external_aliases;
create trigger gc_external_aliases_updated_at before update on gc_external_aliases
  for each row execute function set_updated_at();

alter table gc_external_aliases enable row level security;
alter table gc_external_aliases force row level security;

-- ── 9. projects: BuildingConnected facts and the files flag ──────────────

alter table projects
  add column if not exists job_walk_at             timestamptz,
  add column if not exists project_information     text,
  add column if not exists trade_instructions      text,
  add column if not exists gc_confirm_pending      boolean not null default false,
  add column if not exists files_needed_source     text,
  add column if not exists files_needed_url        text,
  add column if not exists files_needed_set_at     timestamptz,
  add column if not exists files_needed_cleared_at timestamptz,
  add column if not exists files_needed_cleared_by uuid references profiles(id) on delete set null;

comment on column projects.gc_confirm_pending is
  'True while the project''s GC is a provisional guess from a portal invitation that nobody has confirmed yet.';
comment on column projects.files_needed_set_at is
  'Set when the project was created or merged from a portal invitation that carries no documents. Cleared by the first drawing or specification upload, or dismissed by hand.';

create index if not exists projects_files_needed_idx
  on projects (files_needed_set_at)
  where files_needed_set_at is not null and files_needed_cleared_at is null;

-- ── 10. rfp_created_projects.gc_plan: provisional ────────────────────────

do $$
declare
  con record;
begin
  for con in
    select c.conname
    from pg_constraint c
    join pg_attribute a on a.attrelid = c.conrelid and a.attnum = any (c.conkey)
    where c.conrelid = 'public.rfp_created_projects'::regclass
      and c.contype = 'c'
      and a.attname = 'gc_plan'
      and array_length(c.conkey, 1) = 1
  loop
    execute format('alter table rfp_created_projects drop constraint %I', con.conname);
  end loop;

  alter table rfp_created_projects add constraint rfp_created_projects_gc_plan_check
    check (gc_plan in ('resolved', 'likely', 'created', 'none', 'provisional'));
end
$$;

-- ── 11. Reload the PostgREST schema cache ────────────────────────────────

notify pgrst, 'reload schema';
