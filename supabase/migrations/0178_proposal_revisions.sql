-- 0178: Revised proposals after submission.
--
-- A bid was submitted (proposals sent to GCs), then the GC pushed the bid date
-- out and the team wants to revise its numbers and send a Revised Proposal.
-- See docs/PROPOSAL_REVISIONS.md.
--
-- proposal_sends stays exactly ONE row per (project, GC), meaning "the bid
-- this GC currently holds". It is never widened and never deleted (deleting
-- would cascade proposal_send_events history). Its row stays at status 'sent'
-- for the whole revision, so "was this GC bid" never flickers. A revised
-- document lives in proposal_revision_sends until it goes out; the moment it
-- is sent (or marked submitted) apply_proposal_revision_send swaps it onto
-- the live proposal_sends row, snapshots what it replaced, and appends a
-- proposal_send_events row of kind 'revision', all in one transaction.
--
-- The live row's submission record (sent_at, sent_by, sent_via, email_log_id,
-- gc_email) is NEVER overwritten: it stays the original submission, the same
-- rule re-sends follow, because reports, Calling In, bid invitations and the
-- notification log key off it. The swap moves only "which document the GC
-- holds" (file_id, draft_id, lines_hash, the amounts, subject,
-- revision_number) and stamps revised_at / revised_by.
--
-- Claim, swap and close all lock the revision row first, so Done / Cancel can
-- never race an email in flight (claim_proposal_revision_send,
-- apply_proposal_revision_send, finish_proposal_revision).
--
--   proposal_revisions        one row per revision (Rev 1, Rev 2, ...);
--                             at most one open per project
--   proposal_revision_sends   one row per (revision, GC that already held a
--                             sent proposal): its own revised document and
--                             lifecycle, plus the prior live values it replaced

-- ── 1. revisions ────────────────────────────────────────────────────────────

create table if not exists proposal_revisions (
  id                        uuid primary key default gen_random_uuid(),
  project_id                uuid not null references projects(id) on delete cascade,
  revision_number           integer not null check (revision_number >= 1),
  status                    text not null default 'open'
                            constraint proposal_revisions_status_check
                            check (status in ('open', 'completed', 'cancelled')),
  -- "What changed": required, short.
  reason                    text not null
                            constraint proposal_revisions_reason_len
                            check (char_length(reason) between 1 and 2000),
  -- The bid dates as they stood when the revision started, and the new ones
  -- the start form set (through the normal project PATCH, so the bid date
  -- change email still goes out). Null new_* = not changed at start.
  previous_internal_bid_at  timestamptz,
  new_internal_bid_at       timestamptz,
  previous_actual_bid_at    timestamptz,
  new_actual_bid_at         timestamptz,
  started_by                uuid references profiles(id) on delete set null,
  started_at                timestamptz not null default now(),
  -- 'auto' = every included GC was sent or marked; 'manual' = Done.
  completion                text
                            constraint proposal_revisions_completion_check
                            check (completion is null or completion in ('auto', 'manual')),
  completed_by              uuid references profiles(id) on delete set null,
  completed_at              timestamptz,
  cancelled_by              uuid references profiles(id) on delete set null,
  cancelled_at              timestamptz,
  created_at                timestamptz not null default now(),
  updated_at                timestamptz not null default now(),
  unique (project_id, revision_number)
);

-- The GCs on the project when the revision started: a GC linked after this
-- is "added during the revision", and the revision waits for its initial
-- proposal before completing on its own.
alter table proposal_revisions
  add column if not exists start_gc_ids jsonb not null default '[]'::jsonb;

create unique index if not exists proposal_revisions_one_open
  on proposal_revisions (project_id) where status = 'open';
create index if not exists proposal_revisions_project_idx
  on proposal_revisions (project_id, revision_number desc);
create index if not exists proposal_revisions_started_by_idx on proposal_revisions (started_by);
create index if not exists proposal_revisions_completed_by_idx on proposal_revisions (completed_by);
create index if not exists proposal_revisions_cancelled_by_idx on proposal_revisions (cancelled_by);

create or replace trigger proposal_revisions_updated_at before update on proposal_revisions
  for each row execute function set_updated_at();

-- ── 2. per-GC revised documents ─────────────────────────────────────────────

create table if not exists proposal_revision_sends (
  id                      uuid primary key default gen_random_uuid(),
  revision_id             uuid not null references proposal_revisions(id) on delete cascade,
  project_id              uuid not null references projects(id) on delete cascade,
  -- RESTRICT like proposal_sends: what was sent to a GC is a bid record.
  gc_id                   uuid not null references general_contractors(id) on delete restrict,
  -- The live row this revision replaces the document on.
  proposal_send_id        uuid references proposal_sends(id) on delete set null,
  gc_name                 text not null,
  -- Unchecked in the GC list: this GC keeps its current proposal.
  included                boolean not null default true,
  -- pending = no revised document yet; generated -> sending -> sent | failed.
  status                  text not null default 'pending'
                          constraint proposal_revision_sends_status_check
                          check (status in ('pending', 'generated', 'sending', 'sent', 'failed')),
  error                   text,
  draft_id                uuid references proposal_drafts(id) on delete set null,
  lines_hash              text,
  file_id                 uuid references project_files(id) on delete set null,
  -- The figures this revised document carries (NULL section = not on it).
  material_amount         numeric,
  gear_amount             numeric,
  underground_amount      numeric,
  low_voltage_amount      numeric,
  labor_amount            numeric,
  -- Written by the claim BEFORE the email goes out: the exact recipient
  -- string, subject and body used. The crash reclaim proves a send against
  -- email_log on gc_email + subject + created_at >= claimed_at.
  gc_email                text,
  subject                 text
                          constraint proposal_revision_sends_subject_len
                          check (subject is null or char_length(subject) <= 300),
  body                    text,
  cc_recipients           jsonb,
  claimed_at              timestamptz,
  claimed_by              uuid references profiles(id) on delete set null,
  sent_at                 timestamptz,
  sent_by                 uuid references profiles(id) on delete set null,
  sent_via                text
                          constraint proposal_revision_sends_via_check
                          check (sent_via is null or sent_via in ('email', 'external')),
  email_log_id            uuid references email_log(id) on delete set null,
  send_event_id           uuid references proposal_send_events(id) on delete set null,
  -- Snapshot of the live proposal_sends values this document replaced.
  prior_file_id           uuid references project_files(id) on delete set null,
  prior_draft_id          uuid references proposal_drafts(id) on delete set null,
  prior_lines_hash        text,
  prior_sent_at           timestamptz,
  prior_sent_by           uuid references profiles(id) on delete set null,
  prior_sent_via          text,
  prior_email_log_id      uuid references email_log(id) on delete set null,
  prior_gc_email          text,
  prior_subject           text,
  prior_material_amount   numeric,
  prior_gear_amount       numeric,
  prior_underground_amount numeric,
  prior_low_voltage_amount numeric,
  prior_labor_amount      numeric,
  prior_revision_number   integer,
  created_at              timestamptz not null default now(),
  updated_at              timestamptz not null default now(),
  unique (revision_id, gc_id)
);

create index if not exists proposal_revision_sends_project_idx on proposal_revision_sends (project_id);
create index if not exists proposal_revision_sends_gc_idx on proposal_revision_sends (gc_id);
create index if not exists proposal_revision_sends_send_idx on proposal_revision_sends (proposal_send_id);
create index if not exists proposal_revision_sends_draft_idx on proposal_revision_sends (draft_id);
create index if not exists proposal_revision_sends_file_idx on proposal_revision_sends (file_id);
create index if not exists proposal_revision_sends_email_log_idx on proposal_revision_sends (email_log_id);
create index if not exists proposal_revision_sends_event_idx on proposal_revision_sends (send_event_id);
create index if not exists proposal_revision_sends_prior_file_idx on proposal_revision_sends (prior_file_id);
create index if not exists proposal_revision_sends_prior_draft_idx on proposal_revision_sends (prior_draft_id);
create index if not exists proposal_revision_sends_prior_email_log_idx on proposal_revision_sends (prior_email_log_id);
create index if not exists proposal_revision_sends_claimed_by_idx on proposal_revision_sends (claimed_by);
create index if not exists proposal_revision_sends_sent_by_idx on proposal_revision_sends (sent_by);
create index if not exists proposal_revision_sends_prior_sent_by_idx on proposal_revision_sends (prior_sent_by);

create or replace trigger proposal_revision_sends_updated_at before update on proposal_revision_sends
  for each row execute function set_updated_at();

-- origin / auto_send_run_id the swap replaced (an accepted auto send row
-- becomes a Send Out row once it holds a revised document), and the GC's
-- per-GC overrides (project_gcs.proposal_*_amount) when the revision started,
-- restored when the revision ends without this GC's revised document going
-- out (unchecked, Done, Cancel), so its figures match the document it holds.
alter table proposal_revision_sends
  add column if not exists prior_origin text,
  add column if not exists prior_auto_send_run_id uuid,
  add column if not exists start_overrides jsonb;

-- ── 3. proposal_sends: which document the GC holds ─────────────────────────

-- 0 = the original proposal; N = the GC holds Revision N. `subject` is the
-- exact subject that document last went out under (null = derived as before),
-- so a re-send of a revised document repeats the revision's subject.
alter table proposal_sends
  add column if not exists revision_number integer not null default 0,
  add column if not exists subject text,
  add column if not exists revised_at timestamptz,
  add column if not exists revised_by uuid;

do $$
begin
  if not exists (select 1 from pg_constraint where conname = 'proposal_sends_revised_by_fkey') then
    alter table proposal_sends
      add constraint proposal_sends_revised_by_fkey
      foreign key (revised_by) references profiles(id) on delete set null;
  end if;
end $$;
create index if not exists proposal_sends_revised_by_idx
  on proposal_sends (revised_by) where revised_by is not null;

comment on column proposal_sends.revised_at is
  'When the latest revised proposal reached this GC (null = never revised). sent_at stays the original submission.';

comment on column proposal_sends.revision_number is
  '0 = the GC holds the original proposal; N = it holds Revision N (proposal_revisions.revision_number).';
comment on column proposal_sends.subject is
  'The exact email subject the current document last went out under; null = the derived Send Out subject.';

-- ── 4. proposal_send_events: the revision transmission ─────────────────────

-- Swapped only while it still lacks 'revision', so a re-run is a no-op.
do $$
begin
  if exists (
    select 1 from pg_constraint
     where conname = 'proposal_send_events_kind_check'
       and pg_get_constraintdef(oid) not like '%revision%'
  ) then
    alter table proposal_send_events drop constraint proposal_send_events_kind_check;
  end if;
  if not exists (
    select 1 from pg_constraint where conname = 'proposal_send_events_kind_check'
  ) then
    alter table proposal_send_events
      add constraint proposal_send_events_kind_check
      check (kind in ('initial', 'resend', 'revision'));
  end if;
end $$;

-- Set on the 'revision' events, and on the 'initial' events of GCs added and
-- bid while a revision was open, so the revision history lists both.
alter table proposal_send_events
  add column if not exists proposal_revision_id uuid;

do $$
begin
  if not exists (
    select 1 from pg_constraint where conname = 'proposal_send_events_revision_fkey'
  ) then
    alter table proposal_send_events
      add constraint proposal_send_events_revision_fkey
      foreign key (proposal_revision_id) references proposal_revisions(id) on delete set null;
  end if;
end $$;

create index if not exists proposal_send_events_revision_idx
  on proposal_send_events (proposal_revision_id) where proposal_revision_id is not null;

-- RLS deny-by-default + FORCE (0055 / 0093 posture).
alter table proposal_revisions enable row level security;
alter table proposal_revisions force row level security;
alter table proposal_revision_sends enable row level security;
alter table proposal_revision_sends force row level security;


-- ── 5. claim, swap, close ───────────────────────────────────────────────────
-- Every function locks the revision row FIRST (then the revision send rows),
-- so they serialize on it and never deadlock with each other:
--   claim  : the revision must be open; the row must be checked and at
--            'generated' / 'failed'; no re-send of this GC's live proposal
--            may be in flight. Writes the claim (status 'sending' plus the
--            exact recipients, subject and body the crash reclaim proves).
--   swap   : the revision must be open, the live row 'sent'; the 'revision'
--            event is appended, the prior live values are copied onto the
--            revision send row, and the live row points at the new document.
--   finish : refuses while any row is 'sending', so a Done or Cancel can never
--            close a revision under an email in flight. Cancel also refuses
--            once anything may have reached a GC (a sent row, an initial
--            proposal tagged with the revision, an outcome-unknown or
--            delivered-but-not-recorded row).
-- A null return = the row was no longer claimable (a racing request won).
-- Errors are raised as 'proposal_revision:<code>' and mapped by the backend.

create or replace function claim_proposal_revision_send(
  p_revision_send_id uuid,
  p_user_id uuid,
  p_gc_email text,
  p_subject text,
  p_body text,
  p_cc jsonb
)
returns jsonb
language plpgsql
volatile
security invoker
set search_path = pg_catalog, public
as $$
declare
  r     proposal_revision_sends%rowtype;
  rev   proposal_revisions%rowtype;
  v_out jsonb;
begin
  select * into r from proposal_revision_sends where id = p_revision_send_id;
  if not found then
    raise exception 'proposal_revision:not_found';
  end if;
  select * into rev from proposal_revisions where id = r.revision_id for update;
  if not found or rev.status <> 'open' then
    raise exception 'proposal_revision:not_open';
  end if;
  select * into r from proposal_revision_sends where id = p_revision_send_id for update;
  if not r.included then
    raise exception 'proposal_revision:excluded';
  end if;
  if r.status not in ('generated', 'failed') then
    return null;
  end if;
  if coalesce(r.error, '') like 'email delivered but not recorded%' then
    raise exception 'proposal_revision:delivered';
  end if;
  if exists (
    select 1
      from proposal_send_events e
      join proposal_sends ps on ps.id = e.proposal_send_id
     where ps.project_id = r.project_id and ps.gc_id = r.gc_id and e.status = 'sending'
  ) then
    raise exception 'proposal_revision:resend_in_flight';
  end if;
  update proposal_revision_sends
     set status = 'sending',
         gc_email = p_gc_email,
         subject = p_subject,
         body = p_body,
         cc_recipients = p_cc,
         claimed_at = now(),
         claimed_by = p_user_id
   where id = r.id;
  select to_jsonb(x) into v_out from proposal_revision_sends x where x.id = r.id;
  return v_out;
end;
$$;

create or replace function apply_proposal_revision_send(
  p_revision_send_id uuid,
  p_user_id uuid,
  p_via text,
  p_email_log_id uuid,
  p_sent_at timestamptz,
  p_recipients text,
  p_cc jsonb,
  p_subject text
)
returns jsonb
language plpgsql
volatile
security invoker
set search_path = pg_catalog, public
as $$
declare
  r       proposal_revision_sends%rowtype;
  rev     proposal_revisions%rowtype;
  ps      proposal_sends%rowtype;
  v_event uuid;
  v_now   timestamptz := coalesce(p_sent_at, now());
  v_out   jsonb;
begin
  if p_via not in ('email', 'external') then
    raise exception 'proposal_revision:bad_via';
  end if;
  select * into r from proposal_revision_sends where id = p_revision_send_id;
  if not found then
    raise exception 'proposal_revision:not_found';
  end if;
  select * into rev from proposal_revisions where id = r.revision_id for update;
  if not found or rev.status <> 'open' then
    raise exception 'proposal_revision:not_open';
  end if;
  select * into r from proposal_revision_sends where id = p_revision_send_id for update;
  if p_via = 'email' and r.status <> 'sending' then
    return null;
  end if;
  if p_via = 'external' and r.status not in ('generated', 'failed') then
    return null;
  end if;
  if p_via = 'external' and coalesce(r.error, '') like 'email delivered but not recorded%' then
    raise exception 'proposal_revision:delivered';
  end if;
  if not r.included then
    raise exception 'proposal_revision:excluded';
  end if;
  if r.file_id is null then
    raise exception 'proposal_revision:no_document';
  end if;

  select * into ps from proposal_sends
   where project_id = r.project_id and gc_id = r.gc_id
     for update;
  if not found or ps.status <> 'sent' then
    raise exception 'proposal_revision:not_sent';
  end if;

  insert into proposal_send_events (
    proposal_send_id, project_id, gc_id, kind, via, file_id, recipients,
    cc_recipients, status, email_log_id, sent_at, sent_by, proposal_revision_id
  ) values (
    ps.id, r.project_id, r.gc_id, 'revision', p_via, r.file_id,
    case when p_via = 'email' then p_recipients else null end,
    case when p_via = 'email' then p_cc else null end,
    'sent', p_email_log_id, v_now, p_user_id, rev.id
  ) returning id into v_event;

  update proposal_revision_sends
     set status = 'sent',
         error = null,
         sent_via = p_via,
         sent_at = v_now,
         sent_by = p_user_id,
         email_log_id = p_email_log_id,
         subject = coalesce(p_subject, r.subject),
         gc_email = case when p_via = 'email' then r.gc_email else null end,
         send_event_id = v_event,
         proposal_send_id = ps.id,
         prior_file_id = ps.file_id,
         prior_draft_id = ps.draft_id,
         prior_lines_hash = ps.lines_hash,
         prior_sent_at = coalesce(ps.revised_at, ps.sent_at),
         prior_sent_by = coalesce(ps.revised_by, ps.sent_by),
         prior_sent_via = ps.sent_via,
         prior_email_log_id = ps.email_log_id,
         prior_gc_email = ps.gc_email,
         prior_subject = ps.subject,
         prior_material_amount = ps.material_amount,
         prior_gear_amount = ps.gear_amount,
         prior_underground_amount = ps.underground_amount,
         prior_low_voltage_amount = ps.low_voltage_amount,
         prior_labor_amount = ps.labor_amount,
         prior_revision_number = ps.revision_number,
         prior_origin = ps.origin,
         prior_auto_send_run_id = ps.auto_send_run_id
   where id = r.id;

  -- The submission record (sent_at, sent_by, sent_via, email_log_id,
  -- gc_email) is left exactly as it was; only the held document moves.
  update proposal_sends
     set file_id = r.file_id,
         draft_id = r.draft_id,
         lines_hash = r.lines_hash,
         gc_name = r.gc_name,
         material_amount = r.material_amount,
         gear_amount = r.gear_amount,
         underground_amount = r.underground_amount,
         low_voltage_amount = r.low_voltage_amount,
         labor_amount = r.labor_amount,
         subject = coalesce(p_subject, r.subject),
         revision_number = rev.revision_number,
         revised_at = v_now,
         revised_by = p_user_id,
         origin = 'send_out',
         auto_send_run_id = null,
         error = null
   where id = ps.id;

  select to_jsonb(x) into v_out from proposal_revision_sends x where x.id = r.id;
  return v_out;
end;
$$;

create or replace function finish_proposal_revision(
  p_revision_id uuid,
  p_user_id uuid,
  p_mode text
)
returns jsonb
language plpgsql
volatile
security invoker
set search_path = pg_catalog, public
as $$
declare
  rev       proposal_revisions%rowtype;
  v_reached boolean;
  v_out     jsonb;
begin
  if p_mode not in ('cancel', 'done', 'auto') then
    raise exception 'proposal_revision:bad_mode';
  end if;
  select * into rev from proposal_revisions where id = p_revision_id for update;
  if not found then
    raise exception 'proposal_revision:not_found';
  end if;
  if rev.status <> 'open' then
    raise exception 'proposal_revision:not_open';
  end if;
  perform 1 from proposal_revision_sends where revision_id = rev.id for update;
  if exists (
    select 1 from proposal_revision_sends where revision_id = rev.id and status = 'sending'
  ) then
    raise exception 'proposal_revision:sending';
  end if;
  v_reached := exists (
    select 1 from proposal_revision_sends
     where revision_id = rev.id
       and (status = 'sent'
            or (status = 'failed'
                and (coalesce(error, '') like 'outcome unknown%'
                     or coalesce(error, '') like 'email delivered but not recorded%')))
  ) or exists (
    select 1 from proposal_send_events
     where proposal_revision_id = rev.id and kind = 'initial' and status = 'sent'
  );
  if p_mode = 'cancel' and v_reached then
    raise exception 'proposal_revision:went_out';
  end if;
  if p_mode = 'done' and not v_reached then
    raise exception 'proposal_revision:nothing_sent';
  end if;
  update proposal_revisions
     set status = case when p_mode = 'cancel' then 'cancelled' else 'completed' end,
         completion = case p_mode when 'auto' then 'auto' when 'done' then 'manual' else null end,
         completed_by = case when p_mode = 'cancel' then null else p_user_id end,
         completed_at = case when p_mode = 'cancel' then null else now() end,
         cancelled_by = case when p_mode = 'cancel' then p_user_id else null end,
         cancelled_at = case when p_mode = 'cancel' then now() else null end
   where id = rev.id;
  select to_jsonb(x) into v_out from proposal_revisions x where x.id = rev.id;
  return v_out;
end;
$$;

revoke all on function claim_proposal_revision_send(uuid, uuid, text, text, text, jsonb) from public;
revoke all on function claim_proposal_revision_send(uuid, uuid, text, text, text, jsonb) from anon, authenticated;
grant execute on function claim_proposal_revision_send(uuid, uuid, text, text, text, jsonb) to service_role;
revoke all on function apply_proposal_revision_send(uuid, uuid, text, uuid, timestamptz, text, jsonb, text) from public;
revoke all on function apply_proposal_revision_send(uuid, uuid, text, uuid, timestamptz, text, jsonb, text) from anon, authenticated;
grant execute on function apply_proposal_revision_send(uuid, uuid, text, uuid, timestamptz, text, jsonb, text) to service_role;
revoke all on function finish_proposal_revision(uuid, uuid, text) from public;
revoke all on function finish_proposal_revision(uuid, uuid, text) from anon, authenticated;
grant execute on function finish_proposal_revision(uuid, uuid, text) to service_role;

notify pgrst, 'reload schema';
