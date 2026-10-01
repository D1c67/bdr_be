-- 0122 - RFP Ingestion: field extract and project matching
-- (docs/RFP_MATCHING.md, section 4). Apply after 0121.
--
-- Two pipeline steps follow `method` from now on: `extract` (four facts
-- pulled from the email by a small schema-enforced LLM call) and `match`
-- (does the project already exist?). A match that merges attaches the
-- inviting GC to the existing project; a GC already on it is a `duplicate`.
-- Every such decision is a row in rfp_project_matches, and the one
-- project_gcs link the matcher created carries rfp_match_id so a crash
-- resume and an unmerge can tell it from a link a person added. projects
-- gains bid_notes (the GC's instructions about the bid, separate from
-- notes) and the manual is_rebid flag.
--
-- Status vocabulary after this migration:
--   pending  : received, auth, keywords, classify, authorize, method,
--              extract, match
--   human    : review_llm, flagged_unauthorized, review_match
--   terminal : done, merged, duplicate, flagged_auth, flagged_no_keywords,
--              flagged_llm_no, rejected_by_review, failed
--
-- Release order: apply this migration, then deploy. The old sweep ignores
-- `extract` and `match`, so the backfilled rows sit untouched in between,
-- and the new backend repeats the same guarded backfill once at startup.
-- Rolling the code back leaves rows at `extract` / `match` waiting; nothing
-- is lost. Every statement here is idempotent.

-- ── 1. rfp_emails: extracted facts, GC resolution, match decision ────────

alter table rfp_emails
  add column if not exists extracted_project_name     text,
  add column if not exists extracted_gc_name          text,
  add column if not exists extracted_bid_due_at       timestamptz,
  add column if not exists extracted_bid_due_has_time boolean not null default false,
  add column if not exists extracted_bid_notes        text,
  add column if not exists extract_model              text,
  add column if not exists extract_prompt_version     text,
  add column if not exists extracted_at               timestamptz,
  add column if not exists sibling_of_email_id        uuid references rfp_emails(id) on delete set null,
  add column if not exists resolved_gc_id             uuid references general_contractors(id) on delete set null,
  add column if not exists resolved_gc_contact_id     uuid references gc_contacts(id) on delete set null,
  add column if not exists gc_match_kind              text
    check (gc_match_kind in ('contact', 'domain', 'name', 'human', 'sibling')),
  add column if not exists gc_match_score             numeric(4,3),
  add column if not exists gc_candidates              jsonb not null default '[]'::jsonb,
  add column if not exists match_candidates           jsonb not null default '[]'::jsonb,
  add column if not exists match_project_id           uuid references projects(id) on delete set null,
  add column if not exists match_score                numeric(4,3),
  add column if not exists match_llm_model            text,
  add column if not exists match_llm_prompt_version   text,
  add column if not exists match_weights              jsonb,
  add column if not exists matched_at                 timestamptz,
  add column if not exists match_review_decision      text
    check (match_review_decision in ('merge', 'duplicate', 'no_match')),
  add column if not exists match_review_by            uuid references profiles(id) on delete set null,
  add column if not exists match_review_at            timestamptz,
  add column if not exists match_review_agreed        boolean,
  add column if not exists excluded_project_ids       uuid[] not null default '{}',
  add column if not exists possible_rebid_project_id  uuid references projects(id) on delete set null,
  add column if not exists possible_rebid_score       numeric(4,3);

comment on column rfp_emails.extracted_bid_due_at is
  'Bid due instant from the extract step; the wall time localized in the zone the email named (fixed table, Pacific by default). A date with no time is midnight Pacific.';
comment on column rfp_emails.extracted_at is
  'Stamped on every exit from extract (success, unusable output, sibling copy). Null on a done row means "parked by the intake slice, never extracted"; the backfill below and the startup pass key on it.';
comment on column rfp_emails.sibling_of_email_id is
  'The oldest per-recipient copy of the same platform message (same sender, subject and attachments within RFP_MATCH_SIBLING_WINDOW_MINUTES); this row followed its decision without an LLM call.';
comment on column rfp_emails.gc_match_kind is
  'How resolved_gc_id was found: contact (sender address), domain (sender domain one GC owns), name (fuzzy on extracted_gc_name), human (set on the review screen), sibling (copied from the leader).';
comment on column rfp_emails.match_candidates is
  'Top RFP_MATCH_MAX_CANDIDATES by total from the latest match evaluation: {project_id, name, number, breakdown, verdict, confidence, reasoning}. Breakdowns hold date KINDS only, never a project date value. Replaced whenever the row passes match again.';
comment on column rfp_emails.match_weights is
  'The resolved rfp_match_* settings plus scorer_version and auto_merge_enabled at the latest evaluation, so tuning never makes an old decision unexplainable.';
comment on column rfp_emails.matched_at is
  'Stamped on every match route. updated_at is not usable: method correction and the later creation slice write to done rows.';
comment on column rfp_emails.match_review_agreed is
  'True when the human decision landed on match_project_id (the system''s best), false for another project or no match, null until reviewed. Feeds GET /rfp-emails/match-stats.';
comment on column rfp_emails.excluded_project_ids is
  'Projects unmerged from this email; never offered again and skipped by every later evaluation.';

-- The sibling leader lookup (find_sibling_leader) reads by key, not by this
-- column; the index serves the creation slice, which collapses followers by
-- it, and the FK convention.
create index if not exists rfp_emails_sibling_idx
  on rfp_emails (sibling_of_email_id) where sibling_of_email_id is not null;
create index if not exists rfp_emails_match_project_idx
  on rfp_emails (match_project_id) where match_project_id is not null;

-- ── 2. Status vocabulary ────────────────────────────────────────────────
-- The 0120 constraint is inline and unnamed, so it is looked up from
-- pg_constraint on the status column; on a re-run the named constraint
-- below is what gets dropped and re-added.

do $$
declare
  con record;
begin
  for con in
    select c.conname
    from pg_constraint c
    join pg_attribute a on a.attrelid = c.conrelid and a.attnum = any (c.conkey)
    where c.conrelid = 'public.rfp_emails'::regclass
      and c.contype = 'c'
      and a.attname = 'status'
      and array_length(c.conkey, 1) = 1
  loop
    execute format('alter table rfp_emails drop constraint %I', con.conname);
  end loop;

  alter table rfp_emails add constraint rfp_emails_status_check check (status in (
    'received', 'auth', 'keywords', 'classify', 'authorize', 'method', 'extract', 'match',
    'review_llm', 'flagged_unauthorized', 'review_match',
    'done', 'merged', 'duplicate', 'flagged_auth', 'flagged_no_keywords', 'flagged_llm_no',
    'rejected_by_review', 'failed'));
end
$$;

-- ── 3. Backfill: rows the intake slice parked at done, never extracted ──
-- Exactly "parked by the intake slice, never extracted": every exit from
-- extract stamps extracted_at, so this is a no-op on any later run. The new
-- backend repeats the same guarded UPDATE once at startup.

update rfp_emails
   set status = 'extract', attempts = 0, last_error = null, next_attempt_at = null
 where status = 'done' and extracted_at is null;

-- ── 4. rfp_project_matches: every merge and duplicate decision ──────────

create table if not exists rfp_project_matches (
  id                   uuid primary key default gen_random_uuid(),
  rfp_email_id         uuid not null references rfp_emails(id) on delete cascade,
  project_id           uuid not null references projects(id) on delete cascade,
  gc_id                uuid not null references general_contractors(id) on delete cascade,
  project_gc_id        uuid references project_gcs(id) on delete set null,
  kind                 text not null check (kind in ('merged', 'duplicate')),
  gc_added             boolean not null default false,
  contact_selected_id  uuid references gc_contacts(id) on delete set null,
  sibling_of_email_id  uuid,                      -- informational
  score                numeric(4,3),
  candidate_rank       int,                       -- null when the project was not in the stored list
  breakdown            jsonb,
  candidates           jsonb,
  weights              jsonb,
  -- provenance copied from the email at decision time
  gc_match_kind        text,
  gc_match_score       numeric(4,3),
  authorization_kind   text,
  invitation_method    text,
  sender_address       text,                      -- rfp_emails.from_address, lowercased
  auth_dmarc           text,
  auth_compauth        text,
  decided_by           uuid references profiles(id) on delete set null,   -- null = the system
  decided_at           timestamptz not null default now(),                -- the row's creation time
  acknowledged_by      uuid references profiles(id) on delete set null,
  acknowledged_at      timestamptz,
  acknowledge_reason   text,
  unmerged_by          uuid references profiles(id) on delete set null,
  unmerged_at          timestamptz,
  unmerge_reason       text
);

comment on table rfp_project_matches is
  'One row per merge or duplicate decision the matcher (decided_by null) or a reviewer made for an RFP email. Unmerge closes a row (unmerged_at), it never deletes it; a reopen closes a duplicate row the same way.';
comment on column rfp_project_matches.project_gc_id is
  'The project_gcs link the merge inserted (set null by the FK when it is removed). Null on a merged row with gc_added = true is the durable proof the system''s link is gone.';
comment on column rfp_project_matches.gc_added is
  'True once the merge inserted its project_gcs link (step 5 of the merge order); the resume path keys on it.';
comment on column rfp_project_matches.breakdown is
  'The chosen candidate''s breakdown merged with its verdict, confidence and reasoning; date kinds only, never a date value, never the row''s own project_id, name or number.';
comment on column rfp_project_matches.candidate_rank is
  '1-based position of the project in the email''s stored match_candidates at decision time; null when a reviewer chose a project outside that list.';
comment on column rfp_project_matches.decided_by is
  'The reviewer who merged or marked the duplicate; null means the system decided.';

create index if not exists rfp_project_matches_project_idx
  on rfp_project_matches (project_id, decided_at desc);
comment on index rfp_project_matches_project_idx is
  'The project''s Merged by System modal, the list/detail counts (unmerged_at is null as a row filter) and the unmerge cascade.';

create index if not exists rfp_project_matches_email_idx
  on rfp_project_matches (rfp_email_id, decided_at desc);
comment on index rfp_project_matches_email_idx is
  'The merge resume path and the email detail route''s latest row (open or closed).';

create unique index if not exists rfp_project_matches_open_uidx
  on rfp_project_matches (rfp_email_id) where unmerged_at is null;
comment on index rfp_project_matches_open_uidx is
  'One open decision per email; the merge resume relies on it.';

create index if not exists rfp_project_matches_gc_idx
  on rfp_project_matches (gc_id);
comment on index rfp_project_matches_gc_idx is
  'FK convention; the GC panel''s system-added lookup reads by project, not here.';

-- ── 5. project_gcs: the link the matcher created ────────────────────────
-- The (project_id, gc_id) unique key alone cannot tell the system's own link
-- from a concurrent human add after a crash; this column is the durable
-- marker the merge resume and the unmerge read.

alter table project_gcs
  add column if not exists rfp_match_id uuid references rfp_project_matches(id) on delete set null;

comment on column project_gcs.rfp_match_id is
  'link the matcher created (null = added by a person or at project creation)';

create index if not exists project_gcs_rfp_match_idx
  on project_gcs (rfp_match_id) where rfp_match_id is not null;

-- ── 6. remove_project_gc_unless_sent ────────────────────────────────────
-- The conditional delete behind proposal_send.remove_gc_link (spec 3.7 b).
-- Deletes the named link unless a proposal to that GC is sending (always)
-- or sent (when p_refuse_if_sent), in one statement so a send that lands in
-- between refuses the delete instead of racing it. Returns the deleted id,
-- or null when nothing was deleted (the caller re-reads project_gcs to tell
-- "refused because of a send" from "already removed"). With p_link_id null
-- the pair is resolved to its current link. SECURITY INVOKER + pinned
-- search_path per the claim_llm_jobs convention (0094); the backend calls
-- it as the service-role key.

create or replace function remove_project_gc_unless_sent(
  p_link_id uuid, p_project_id uuid, p_gc_id uuid, p_refuse_if_sent boolean
)
returns uuid
language plpgsql
volatile
security invoker
set search_path = pg_catalog, public
as $$
declare
  v_link_id uuid := p_link_id;
  v_deleted uuid;
begin
  if v_link_id is null then
    select id into v_link_id
      from project_gcs
     where project_id = p_project_id and gc_id = p_gc_id
     limit 1;
  end if;
  if v_link_id is null then
    return null;
  end if;

  delete from project_gcs pg
   where pg.id = v_link_id
     and pg.project_id = p_project_id
     and not exists (
       select 1
         from proposal_sends ps
        where ps.project_id = p_project_id
          and ps.gc_id = p_gc_id
          and (ps.status = 'sending' or (p_refuse_if_sent and ps.status = 'sent'))
     )
  returning pg.id into v_deleted;

  return v_deleted;
end
$$;

-- ── 7. projects: bid notes and the rebid flag ───────────────────────────

alter table projects
  add column if not exists bid_notes text,
  add column if not exists is_rebid  boolean not null default false;

comment on column projects.bid_notes is
  'The GC''s instructions about this bid (scope notes, walk dates, delivery rules), entered on intake or project details. Separate from notes. Scored as a bonus by the RFP matcher; never written by it.';
comment on column projects.is_rebid is
  'Manual flag: this job was bid before under another invitation. Set on intake (New Bid "Create as a rebid") or project details.';

-- ── 8. RLS: deny by default; the service-role backend is the only reader ─

alter table rfp_project_matches enable row level security;
alter table rfp_project_matches force row level security;

notify pgrst, 'reload schema';
