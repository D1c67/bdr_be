-- 0149 - Merge an RFP-created project into a project we are already bidding
-- (docs/PROJECT_MERGE.md). Apply after 0148.
--
-- RFP ingestion sometimes creates a project for an invitation that belongs to
-- a bid the team already has. Merge Projects moves the RFP-created project's
-- GCs (with their bid dates and contacts), its RFP links, its email filing and
-- the documents the other project does not already have onto the project the
-- team is bidding, then deletes the RFP-created project through the existing
-- archive (0141) with the new reason `merged`, recoverably.
--
-- What lands here:
--
--   project_files.sha256        the SHA-256 of the object's bytes, filled
--                               lazily by the backend (app/services/
--                               file_hashes.py) and cached: "the target
--                               already has this document" is the same hash.
--   deleted_projects            reason `merged` (written only by
--                               merge_rfp_project) and the merged_into_*
--                               columns plus merge_summary.
--   project_archive_and_delete  the 0141 archive, unchanged, as an internal
--                               function with one more argument (the merge
--                               target): set, it archives with reason
--                               `merged`, fills merged_into_* and writes no
--                               RFP block; null, it is exactly the old
--                               function. Executable by no API role.
--   archive_and_delete_project  the public 0141 entry point, now a thin
--                               wrapper: same signature, same behavior, and
--                               it still refuses `merged` (bad_reason).
--   project_merge_file_refs     which of the given project_files ids some
--                               other row points at (any foreign key into
--                               project_files, read from pg_constraint):
--                               those stay with the old project.
--   merge_rfp_project           the merge, one transaction.
--
-- The archive walk, the snapshot, the delete order and the restore
-- (restore_deleted_project) are not touched. The archive function body below
-- is the dev database's definition as of 2026-10-07 (identical to 0141's).
--
-- Every statement is idempotent.

-- ── 1. project_files.sha256 ─────────────────────────────────────────────────

alter table project_files add column if not exists sha256 text;

alter table project_files drop constraint if exists project_files_sha256_ck;
alter table project_files add constraint project_files_sha256_ck
  check (sha256 is null or sha256 ~ '^[0-9a-f]{64}$');

comment on column project_files.sha256 is
  'SHA-256 (lowercase hex) of the stored object''s bytes, filled lazily by the backend and cached (docs/PROJECT_MERGE.md 4.4). Null until hashed, and for an object that could not be read.';

create index if not exists project_files_sha256_idx
  on project_files (project_id, sha256) where sha256 is not null;

-- ── 2. deleted_projects: the merged reason ──────────────────────────────────

alter table deleted_projects
  add column if not exists merged_into_project_id uuid references projects(id) on delete set null,
  add column if not exists merged_into_number     text,
  add column if not exists merged_into_name       text,
  add column if not exists merge_summary          jsonb;

alter table deleted_projects drop constraint if exists deleted_projects_reason_check;
alter table deleted_projects add constraint deleted_projects_reason_check
  check (reason in ('rfp_should_not_exist', 'duplicate', 'created_by_mistake', 'other', 'merged'));

alter table deleted_projects drop constraint if exists deleted_projects_merged_into;
alter table deleted_projects add constraint deleted_projects_merged_into
  check ((reason = 'merged') = (merged_into_number is not null));

comment on column deleted_projects.reason is
  'rfp_should_not_exist | duplicate | created_by_mistake | other (other requires a note) | merged (written only by merge_rfp_project, docs/PROJECT_MERGE.md).';
comment on column deleted_projects.merged_into_project_id is
  'Reason merged: the project this one was merged into. Set null if that project is deleted later (merged_into_number and merged_into_name keep the label).';
comment on column deleted_projects.merge_summary is
  'Reason merged: what moved (GCs, documents, RFP links) and the moved rows as they were before the merge.';

create index if not exists deleted_projects_merged_into_idx
  on deleted_projects (merged_into_project_id) where merged_into_project_id is not null;

-- ── 3. project_archive_and_delete (internal) ────────────────────────────────
-- The 0141 archive with one more argument. p_merged_into null: the old
-- function exactly (four reasons, RFP block written). Set: reason must be
-- `merged`, merged_into_* are filled on the archive row (its CHECK needs them
-- at insert) and no RFP block is written (nothing points at the project any
-- more: the merge moved every RFP pointer to the target first).

create or replace function public.project_archive_and_delete(
  p_project_id uuid, p_actor uuid, p_reason text, p_note text, p_confirm_name text,
  p_merged_into uuid
)
returns jsonb
language plpgsql
volatile
security definer
set search_path = pg_catalog, public, pg_temp
as $$
declare
  v_p          projects%rowtype;
  v_into       projects%rowtype;
  v_rec        rfp_created_projects%rowtype;
  v_note       text := nullif(btrim(coalesce(p_note, '')), '');
  v_expected   text;
  v_id         uuid := gen_random_uuid();
  v_from_rfp   boolean;
  v_src_id     uuid;
  v_src_label  text;
  v_blockers   text;
  v_tables     jsonb;
  v_counts     jsonb;
  v_total      integer;
  v_order      jsonb;
  v_links      jsonb;
  v_gc_ids     uuid[];
  v_gc_names   text[];
  v_gc_links   jsonb;
  v_now        timestamptz := now();
begin
  if p_reason is null
     or p_reason not in ('rfp_should_not_exist', 'duplicate', 'created_by_mistake', 'other', 'merged') then
    raise exception 'project_delete:bad_reason';
  end if;
  if (p_reason = 'merged') is distinct from (p_merged_into is not null) then
    raise exception 'project_delete:bad_reason';
  end if;
  if p_reason = 'other' and v_note is null then
    raise exception 'project_delete:note_required';
  end if;
  if v_note is not null and char_length(v_note) > 2000 then
    raise exception 'project_delete:note_too_long';
  end if;

  select * into v_p from projects where id = p_project_id for update;
  if not found then
    raise exception 'project_delete:not_found';
  end if;
  if v_p.pm_stage is not null then
    raise exception 'project_delete:pm_enrolled';
  end if;
  if v_p.cp_enrolled_at is not null then
    raise exception 'project_delete:cp_enrolled';
  end if;
  v_expected := coalesce(nullif(btrim(v_p.name), ''), btrim(v_p.number));
  if btrim(coalesce(p_confirm_name, '')) is distinct from v_expected then
    raise exception 'project_delete:name_mismatch';
  end if;
  if p_merged_into is not null then
    if p_merged_into = p_project_id then
      raise exception 'project_delete:bad_reason';
    end if;
    select * into v_into from projects where id = p_merged_into;
    if not found then
      raise exception 'project_delete:not_found';
    end if;
  end if;

  select * into v_rec from rfp_created_projects where project_id = p_project_id;
  v_from_rfp := found;
  if p_reason = 'rfp_should_not_exist' and not v_from_rfp then
    raise exception 'project_delete:not_from_rfp';
  end if;

  perform public.project_archive_collect(p_project_id);

  select string_agg(format('%s (%s)', b.tbl, b.n), ', ' order by b.tbl) into v_blockers
    from pg_temp._pda_blockers b;
  if v_blockers is not null then
    raise exception 'project_delete:referenced' using detail = v_blockers;
  end if;

  select jsonb_object_agg(g.tbl, g.rows), jsonb_object_agg(g.tbl, g.n), sum(g.n)::integer,
         jsonb_agg(g.tbl order by g.first_pass, g.tbl)
    into v_tables, v_counts, v_total, v_order
    from (select r.tbl, jsonb_agg(r.row order by r.pk::text) as rows, count(*) as n,
                 min(r.pass) as first_pass
            from pg_temp._pda_rows r group by r.tbl) g;
  select coalesce(jsonb_agg(jsonb_build_object('table', l.tbl, 'pk', l.pk, 'columns', to_jsonb(l.cols),
                                               'values', l.vals)), '[]'::jsonb)
    into v_links from pg_temp._pda_links l;
  select coalesce(array_agg(g.id order by lower(g.name), g.id), '{}'),
         coalesce(array_agg(g.name order by lower(g.name), g.id), '{}'),
         coalesce(jsonb_agg(jsonb_build_object('gc_id', pg.gc_id, 'needs_by', pg.needs_by)), '[]'::jsonb)
    into v_gc_ids, v_gc_names, v_gc_links
    from project_gcs pg join general_contractors g on g.id = pg.gc_id
   where pg.project_id = p_project_id;

  if v_from_rfp then
    if v_rec.source_kind = 'rfp_portal' then
      v_src_id := v_rec.portal_invitation_id;
      select i.title into v_src_label from rfp_portal_invitations i where i.id = v_src_id;
    else
      v_src_id := v_rec.rfp_email_id;
      select e.subject into v_src_label from rfp_emails e where e.id = v_src_id;
    end if;
  end if;

  insert into deleted_projects (
    id, project_id, project_number, project_name, project_stage, project_bid_at, test_session_id,
    gc_ids, gc_names, gc_links, reason, note, from_rfp, rfp_source_kind, rfp_source_id,
    rfp_source_label, deleted_by, deleted_at, project_row, snapshot, set_null_links,
    row_counts, row_total, merged_into_project_id, merged_into_number, merged_into_name
  ) values (
    v_id, v_p.id, v_p.number, v_p.name, v_p.current_stage::text,
    coalesce(v_p.actual_bid_at, v_p.internal_bid_at), v_p.test_session_id,
    v_gc_ids, v_gc_names, v_gc_links, p_reason, v_note, v_from_rfp,
    case when v_from_rfp then v_rec.source_kind end, v_src_id, left(v_src_label, 300),
    p_actor, v_now, to_jsonb(v_p),
    jsonb_build_object('version', 1, 'order', v_order, 'tables', v_tables),
    v_links, v_counts, v_total, v_into.id, v_into.number, v_into.name
  );

  if p_merged_into is null then
    -- The RFP block (docs/PROJECT_DELETE.md 5): the harvests first (the one
    -- the record names, the project's, and those of every email or invitation
    -- that made the project or was DECIDED to be it: merged, duplicate,
    -- exists), then every such row and every row sharing one of those
    -- harvests. A row still waiting in review_match with this project as its
    -- proposed match is not marked: nobody has decided it is this project.
    update rfp_harvests h
       set create_blocked_archive_id = v_id
     where h.project_id = p_project_id
        or h.id = v_rec.harvest_id
        or h.id in (select e.harvest_id from rfp_emails e
                     where e.harvest_id is not null
                       and (e.created_project_id = p_project_id
                            or (e.match_project_id = p_project_id and e.status in ('merged', 'duplicate'))))
        or h.id in (select i.harvest_id from rfp_portal_invitations i
                     where i.harvest_id is not null
                       and (i.created_project_id = p_project_id
                            or (i.match_project_id = p_project_id and i.status = 'exists')));
    update rfp_emails e
       set create_blocked_archive_id = v_id
     where e.created_project_id = p_project_id
        or (e.match_project_id = p_project_id and e.status in ('merged', 'duplicate'))
        or e.id = v_rec.rfp_email_id
        or e.harvest_id in (select h.id from rfp_harvests h where h.create_blocked_archive_id = v_id)
        or e.sibling_of_email_id in (select x.id from rfp_emails x where x.created_project_id = p_project_id);
    update rfp_portal_invitations i
       set create_blocked_archive_id = v_id
     where i.created_project_id = p_project_id
        or (i.match_project_id = p_project_id and i.status = 'exists')
        or i.id = v_rec.portal_invitation_id
        or i.harvest_id in (select h.id from rfp_harvests h where h.create_blocked_archive_id = v_id);
  end if;

  perform public.project_archive_delete_rows();
  -- Already gone with the rows above; kept so the project row is removed
  -- even if the walk ever misses a table.
  delete from projects where id = p_project_id;

  return jsonb_build_object(
    'archive_id', v_id, 'project_id', v_p.id, 'number', v_p.number, 'name', v_p.name,
    'from_rfp', v_from_rfp, 'reason', p_reason, 'deleted_at', v_now, 'row_total', v_total,
    'links', jsonb_array_length(v_links)
  );
end;
$$;

-- ── 4. archive_and_delete_project (public, unchanged contract) ──────────────
-- Same signature and behavior as 0141: the four reasons only (`merged` is
-- bad_reason here, so only merge_rfp_project can archive with it), then the
-- archive with no merge target, which writes the RFP block as before.

create or replace function public.archive_and_delete_project(
  p_project_id uuid, p_actor uuid, p_reason text, p_note text, p_confirm_name text
)
returns jsonb
language plpgsql
volatile
security definer
set search_path = pg_catalog, public, pg_temp
as $$
begin
  if p_reason is null or p_reason not in ('rfp_should_not_exist', 'duplicate', 'created_by_mistake', 'other') then
    raise exception 'project_delete:bad_reason';
  end if;
  return public.project_archive_and_delete(
    p_project_id, p_actor, p_reason, p_note, p_confirm_name, null
  );
end;
$$;

-- ── 5. project_merge_file_refs ──────────────────────────────────────────────
-- The ids among p_file_ids that any row points at through a foreign key into
-- project_files (read from pg_constraint at run time, so a key a later
-- migration adds is covered): an RFQ's split file, a quote's file, a BOQ
-- analysis, a proposal and so on. Such a document belongs to the old
-- project's own records and stays with it.

create or replace function public.project_merge_file_refs(p_file_ids uuid[])
returns uuid[]
language plpgsql
stable
security definer
set search_path = pg_catalog, public, pg_temp
as $$
declare
  r      record;
  v_hit  uuid[];
  v_out  uuid[] := '{}';
begin
  if p_file_ids is null or cardinality(p_file_ids) = 0 then
    return v_out;
  end if;
  for r in
    select c.conrelid::regclass as tbl, a.attname as col
      from pg_constraint c
      join pg_attribute a on a.attrelid = c.conrelid and a.attnum = c.conkey[1]
      join pg_class k on k.oid = c.conrelid
      join pg_namespace n on n.oid = k.relnamespace
     where c.contype = 'f'
       and c.confrelid = 'public.project_files'::regclass
       and cardinality(c.conkey) = 1
       and n.nspname = 'public'
  loop
    execute format('select coalesce(array_agg(distinct %I), ''{}'') from %s where %I = any($1)',
                   r.col, r.tbl, r.col)
      into v_hit using p_file_ids;
    v_out := v_out || v_hit;
  end loop;
  return coalesce((select array_agg(distinct x) from unnest(v_out) x), '{}');
end;
$$;

-- ── 6. merge_rfp_project ────────────────────────────────────────────────────
-- One transaction (docs/PROJECT_MERGE.md 4.3). Errors are raised as
-- 'project_merge:<code>' (details in the error's detail) so the backend maps
-- them to a readable 404 / 409 / 422; the archive's own 'project_delete:'
-- codes pass through.
--
-- p_files: [{file_id, storage_path, preview_path, category, doc_type, note}]:
-- the documents to move, with their NEW object paths (the backend already
-- copied the objects under the target's prefix) and the state they land in
-- on the target (category, doc_type and note per docs/PROJECT_MERGE.md 3.4,
-- decided by the backend from routers/files.py's hand-off lock).

create or replace function public.merge_rfp_project(
  p_source uuid, p_target uuid, p_actor uuid, p_confirm_source text, p_confirm_target text,
  p_files jsonb
)
returns jsonb
language plpgsql
volatile
security definer
set search_path = pg_catalog, public, pg_temp
as $$
declare
  v_s               projects%rowtype;
  v_t               projects%rowtype;
  v_files           jsonb := coalesce(p_files, '[]'::jsonb);
  v_file_ids        uuid[];
  v_work            text[] := '{}';
  v_s_expected      text;
  v_t_expected      text;
  v_s_due           date;
  v_already         uuid[];
  v_gcs_moved       jsonb;
  v_gcs_already     jsonb;
  v_links_before    jsonb;
  v_matches_before  jsonb;
  v_files_before    jsonb;
  v_files_moved     integer := 0;
  v_files_skipped   integer := 0;
  v_rfp_links       integer := 0;
  v_filing          integer := 0;
  v_n               integer;
  v_archive         jsonb;
begin
  if p_source is null then
    raise exception 'project_merge:not_found';
  end if;
  if p_target is null then
    raise exception 'project_merge:target_not_found';
  end if;
  if p_source = p_target then
    raise exception 'project_merge:same_project';
  end if;
  if jsonb_typeof(v_files) <> 'array' then
    raise exception 'project_merge:bad_files';
  end if;

  -- 1. Both projects locked in id order, so two people merging the pair
  -- (either way round) queue up instead of deadlocking.
  perform 1 from projects where id = least(p_source, p_target) for update;
  perform 1 from projects where id = greatest(p_source, p_target) for update;
  select * into v_s from projects where id = p_source;
  if not found then
    raise exception 'project_merge:not_found';
  end if;
  select * into v_t from projects where id = p_target;
  if not found then
    raise exception 'project_merge:target_not_found';
  end if;

  -- The source (2.1). files_arriving and split_running depend on the
  -- backend's lease and heartbeat rules and are checked by it right before
  -- this call; an insert racing the merge waits on the row lock above and
  -- then fails its foreign key.
  if not exists (select 1 from rfp_created_projects where project_id = p_source) then
    raise exception 'project_merge:not_from_rfp';
  end if;
  if v_s.pm_stage is not null then
    raise exception 'project_merge:pm_enrolled';
  end if;
  if v_s.cp_enrolled_at is not null then
    raise exception 'project_merge:cp_enrolled';
  end if;
  if exists (select 1 from rfq_sends x join rfqs r on r.id = x.rfq_id where r.project_id = p_source) then
    v_work := v_work || 'rfq_sends'::text;
  end if;
  if exists (select 1 from quotes x join rfqs r on r.id = x.rfq_id where r.project_id = p_source) then
    v_work := v_work || 'quotes'::text;
  end if;
  if exists (select 1 from estimator_assignments x where x.project_id = p_source) then
    v_work := v_work || 'estimator_assignments'::text;
  end if;
  if exists (select 1 from proposal_sends x where x.project_id = p_source) then
    v_work := v_work || 'proposal_sends'::text;
  end if;
  if exists (select 1 from external_submissions x where x.project_id = p_source) then
    v_work := v_work || 'external_submissions'::text;
  end if;
  if exists (select 1 from bid_outcomes x where x.project_id = p_source) then
    v_work := v_work || 'bid_outcomes'::text;
  end if;
  if cardinality(v_work) > 0 then
    raise exception 'project_merge:work_started' using detail = array_to_string(v_work, ',');
  end if;

  -- The target (2.2).
  if v_t.abandoned_at is not null
     or v_t.current_stage::text in ('declined', 'pm_only', 'cp_only')
     or v_t.pm_stage is not null
     or exists (select 1 from bid_outcomes x where x.project_id = p_target) then
    raise exception 'project_merge:target_closed';
  end if;
  if v_t.test_session_id is distinct from v_s.test_session_id then
    raise exception 'project_merge:target_scope';
  end if;

  -- 2. Both typed names (the delete rule).
  v_s_expected := coalesce(nullif(btrim(v_s.name), ''), btrim(v_s.number));
  v_t_expected := coalesce(nullif(btrim(v_t.name), ''), btrim(v_t.number));
  if btrim(coalesce(p_confirm_source, '')) is distinct from v_s_expected then
    raise exception 'project_merge:source_name_mismatch';
  end if;
  if btrim(coalesce(p_confirm_target, '')) is distinct from v_t_expected then
    raise exception 'project_merge:target_name_mismatch';
  end if;

  -- 3. The documents: every id once, still a source file, landing under the
  -- target's prefix, not referenced by another row, and not the same bytes
  -- as a target file (the target gained one since the hashes were read).
  create temp table if not exists _pm_files (
    id uuid primary key, sp text, pp text, cat text, dt text, note text
  ) on commit drop;
  truncate pg_temp._pm_files;
  begin
    insert into pg_temp._pm_files (id, sp, pp, cat, dt, note)
    select (e->>'file_id')::uuid, e->>'storage_path', nullif(e->>'preview_path', ''),
           e->>'category', nullif(e->>'doc_type', ''), nullif(btrim(coalesce(e->>'note', '')), '')
      from jsonb_array_elements(v_files) e;
  exception when unique_violation or not_null_violation or invalid_text_representation then
    raise exception 'project_merge:bad_files';
  end;
  if exists (select 1 from pg_temp._pm_files f
              where f.cat is null
                 or f.cat not in (select unnest(enum_range(null::file_category))::text)
                 or coalesce(f.sp, '') not like p_target::text || '/%'
                 or (f.pp is not null and f.pp not like p_target::text || '/%')) then
    raise exception 'project_merge:bad_files';
  end if;
  select coalesce(array_agg(f.id), '{}') into v_file_ids from pg_temp._pm_files f;
  if exists (select 1 from pg_temp._pm_files f
              where not exists (select 1 from project_files pf where pf.id = f.id and pf.project_id = p_source)) then
    raise exception 'project_merge:files_changed' using detail = 'a document left the project';
  end if;
  if cardinality(public.project_merge_file_refs(v_file_ids)) > 0 then
    raise exception 'project_merge:files_changed' using detail = 'a document is in use';
  end if;
  if exists (select 1 from pg_temp._pm_files f
               join project_files pf on pf.id = f.id
               join project_files tf on tf.project_id = p_target and tf.sha256 = pf.sha256
              where pf.sha256 is not null) then
    raise exception 'project_merge:files_changed' using detail = 'the target has the same document';
  end if;

  -- 4a. GCs (3.1). A GC already on the target: the target's link wins, the
  -- source link's contacts are added to it, its needs_by is filled only when
  -- null, and the source link stays with the source (archived) without the
  -- match row it named (that row moves to the target below).
  v_s_due := (v_s.actual_bid_at at time zone 'America/Los_Angeles')::date;
  select coalesce(array_agg(s.gc_id), '{}') into v_already
    from project_gcs s
   where s.project_id = p_source
     and exists (select 1 from project_gcs t where t.project_id = p_target and t.gc_id = s.gc_id);
  select coalesce(jsonb_agg(jsonb_build_object('gc_id', s.gc_id, 'name', g.name)
                            order by lower(g.name), g.id), '[]'::jsonb)
    into v_gcs_already
    from project_gcs s join general_contractors g on g.id = s.gc_id
   where s.project_id = p_source and s.gc_id = any(v_already);
  insert into project_gc_contacts (project_gc_id, gc_contact_id)
  select t.id, c.gc_contact_id
    from project_gcs s
    join project_gcs t on t.project_id = p_target and t.gc_id = s.gc_id
    join project_gc_contacts c on c.project_gc_id = s.id
   where s.project_id = p_source
  on conflict (project_gc_id, gc_contact_id) do nothing;
  update project_gcs t
     set needs_by = coalesce(s.needs_by, v_s_due)
    from project_gcs s
   where s.project_id = p_source and t.project_id = p_target and t.gc_id = s.gc_id
     and t.needs_by is null and coalesce(s.needs_by, v_s_due) is not null;
  update project_gcs s
     set rfp_match_id = null
   where s.project_id = p_source and s.gc_id = any(v_already) and s.rfp_match_id is not null;

  -- A GC not on the target: the link MOVES (its id kept, so its contacts,
  -- rfp_match_id and every rfp_project_matches.project_gc_id stay valid) and
  -- lands like a GC an RFP merge adds (rfp_email_ingest._insert_link): the
  -- due date filled from the source's bid date, no per-GC pricing.
  select coalesce(jsonb_agg(to_jsonb(s) order by s.id), '[]'::jsonb) into v_links_before
    from project_gcs s
   where s.project_id = p_source and not (s.gc_id = any(v_already));
  with moved as (
    update project_gcs s
       set project_id = p_target,
           needs_by = coalesce(s.needs_by, v_s_due),
           proposal_material_amount = null, proposal_labor_amount = null,
           proposal_gear_amount = null, proposal_underground_amount = null,
           proposal_low_voltage_amount = null,
           pricing_approval_status = null, pricing_requested_by = null, pricing_requested_at = null,
           pricing_request_note = null, pricing_decided_by = null, pricing_decided_at = null,
           pricing_decision_note = null
     where s.project_id = p_source and not (s.gc_id = any(v_already))
    returning s.id, s.gc_id, s.needs_by
  )
  select coalesce(jsonb_agg(jsonb_build_object('gc_id', m.gc_id, 'name', g.name, 'needs_by', m.needs_by,
                                               'link_id', m.id)
                            order by lower(g.name), g.id), '[]'::jsonb)
    into v_gcs_moved
    from moved m join general_contractors g on g.id = m.gc_id;

  -- 4b. RFP links (3.2). An open match row whose GC was already on the target
  -- becomes what the matcher would have recorded (a duplicate, no link), and
  -- its email at `merged` on the source goes to `duplicate`; then every RFP
  -- pointer at the source moves to the target.
  select coalesce(jsonb_agg(to_jsonb(m) order by m.id), '[]'::jsonb) into v_matches_before
    from rfp_project_matches m
   where m.project_id = p_source and m.unmerged_at is null and m.gc_id = any(v_already)
     and (m.kind <> 'duplicate' or m.gc_added or m.project_gc_id is not null);
  with conv as (
    update rfp_project_matches m
       set kind = 'duplicate', gc_added = false, project_gc_id = null
     where m.project_id = p_source and m.unmerged_at is null and m.gc_id = any(v_already)
       and (m.kind <> 'duplicate' or m.gc_added or m.project_gc_id is not null)
    returning m.rfp_email_id
  )
  update rfp_emails e
     set status = 'duplicate'
    from conv
   where e.id = conv.rfp_email_id and e.status = 'merged' and e.match_project_id = p_source;

  update rfp_project_matches set project_id = p_target where project_id = p_source;
  get diagnostics v_n = row_count;
  v_rfp_links := v_rfp_links + v_n;

  update rfp_emails e
     set created_project_id = case when e.created_project_id = p_source then p_target else e.created_project_id end,
         match_project_id = case when e.match_project_id = p_source then p_target else e.match_project_id end,
         possible_rebid_project_id = case when e.possible_rebid_project_id = p_source then p_target
                                          else e.possible_rebid_project_id end
   where e.created_project_id = p_source or e.match_project_id = p_source
      or e.possible_rebid_project_id = p_source;
  get diagnostics v_n = row_count;
  v_rfp_links := v_rfp_links + v_n;

  -- An invitation's project_gc_id names the link its merge added (reopen
  -- removes it by that id): one that named a source link staying with the
  -- source (its GC was already on the target) no longer owns a link.
  update rfp_portal_invitations i
     set created_project_id = case when i.created_project_id = p_source then p_target else i.created_project_id end,
         match_project_id = case when i.match_project_id = p_source then p_target else i.match_project_id end,
         possible_rebid_project_id = case when i.possible_rebid_project_id = p_source then p_target
                                          else i.possible_rebid_project_id end,
         project_gc_id = case when i.project_gc_id in (select s.id from project_gcs s where s.project_id = p_source)
                              then null else i.project_gc_id end
   where i.created_project_id = p_source or i.match_project_id = p_source
      or i.possible_rebid_project_id = p_source;
  get diagnostics v_n = row_count;
  v_rfp_links := v_rfp_links + v_n;

  update rfp_harvests set project_id = p_target where project_id = p_source;
  get diagnostics v_n = row_count;
  v_rfp_links := v_rfp_links + v_n;

  -- 4c. Project email filing (3.3). email_conversation_projects is unique on
  -- (mailbox, conversation_id) across all projects, so a source row can never
  -- collide with a target row: every one moves.
  update ingested_emails e
     set project_id = case when e.project_id = p_source then p_target else e.project_id end,
         suggested_project_id = case when e.suggested_project_id = p_source then p_target
                                     else e.suggested_project_id end
   where e.project_id = p_source or e.suggested_project_id = p_source;
  get diagnostics v_n = row_count;
  v_filing := v_filing + v_n;
  update email_conversation_projects set project_id = p_target where project_id = p_source;
  get diagnostics v_n = row_count;
  v_filing := v_filing + v_n;

  -- 4d. The documents (3.4): repointed onto the copies, in the state an
  -- internal upload to the target would produce. A provenance id the target
  -- already holds (its unique indexes) is dropped from the moved row.
  select coalesce(jsonb_agg(to_jsonb(pf) order by pf.id), '[]'::jsonb) into v_files_before
    from project_files pf where pf.id = any(v_file_ids);
  update project_files pf
     set project_id = p_target,
         storage_path = f.sp,
         preview_path = f.pp,
         preview_status = case when pf.preview_path is not null and f.pp is null then 'none'
                               else pf.preview_status end,
         category = f.cat::file_category,
         doc_type = f.dt,
         addendum_number = case when f.cat = 'addendum' then pf.addendum_number end,
         addendum_issued_on = case when f.cat = 'addendum' then pf.addendum_issued_on end,
         note = left(f.note, 2000),
         sent_to_estimators_at = null,
         estimator_deliverable = false,
         submission_round = null,
         rfp_sandbox_file_id = case
           when pf.rfp_sandbox_file_id is not null and exists (
             select 1 from project_files t
              where t.project_id = p_target and t.rfp_sandbox_file_id = pf.rfp_sandbox_file_id)
           then null else pf.rfp_sandbox_file_id end,
         bid_split_segment_id = case
           when pf.bid_split_segment_id is not null and exists (
             select 1 from project_files t
              where t.project_id = p_target and t.bid_split_segment_id = pf.bid_split_segment_id)
           then null else pf.bid_split_segment_id end
    from pg_temp._pm_files f
   where pf.id = f.id and pf.project_id = p_source;
  get diagnostics v_files_moved = row_count;
  select count(*)::integer into v_files_skipped from project_files where project_id = p_source;

  -- 5. Archive and delete the source: reason merged, no RFP block.
  v_archive := public.project_archive_and_delete(
    p_source, p_actor, 'merged', null, p_confirm_source, p_target
  );
  update deleted_projects
     set merge_summary = jsonb_build_object(
           'version', 1,
           'target', jsonb_build_object('id', v_t.id, 'number', v_t.number, 'name', v_t.name),
           'merged_by', p_actor,
           'gcs_moved', v_gcs_moved,
           'gcs_already', v_gcs_already,
           'files_moved', v_files_moved,
           'files_skipped', v_files_skipped,
           'rfp_links_moved', v_rfp_links,
           'email_filing_moved', v_filing,
           'before', jsonb_build_object(
             'project_gcs', v_links_before,
             'rfp_project_matches', v_matches_before,
             'project_files', v_files_before))
   where id = (v_archive->>'archive_id')::uuid;

  -- 6. The answer.
  return jsonb_build_object(
    'archive_id', v_archive->>'archive_id',
    'source', jsonb_build_object('id', v_s.id, 'number', v_s.number, 'name', v_s.name),
    'target', jsonb_build_object('id', v_t.id, 'number', v_t.number, 'name', v_t.name),
    'gcs_moved', (select coalesce(jsonb_agg(x - 'link_id'), '[]'::jsonb) from jsonb_array_elements(v_gcs_moved) x),
    'gcs_already', v_gcs_already,
    'files_moved', v_files_moved,
    'files_skipped', v_files_skipped,
    'rfp_links_moved', v_rfp_links,
    'row_total', (v_archive->>'row_total')::integer
  );
end;
$$;

-- ── 7. Privileges (the 0141 pattern) ────────────────────────────────────────
-- The internal archive is executable by no API role at all (not even the
-- service role): only the two security-definer entry points reach it, which
-- is what keeps reason `merged` to merge_rfp_project.

revoke all on function public.project_archive_and_delete(uuid, uuid, text, text, text, uuid) from public;
revoke all on function public.project_archive_and_delete(uuid, uuid, text, text, text, uuid) from anon, authenticated;
revoke all on function public.project_archive_and_delete(uuid, uuid, text, text, text, uuid) from service_role;
revoke all on function public.archive_and_delete_project(uuid, uuid, text, text, text) from public;
revoke all on function public.archive_and_delete_project(uuid, uuid, text, text, text) from anon, authenticated;
grant execute on function public.archive_and_delete_project(uuid, uuid, text, text, text) to service_role;
revoke all on function public.project_merge_file_refs(uuid[]) from public;
revoke all on function public.project_merge_file_refs(uuid[]) from anon, authenticated;
grant execute on function public.project_merge_file_refs(uuid[]) to service_role;
revoke all on function public.merge_rfp_project(uuid, uuid, uuid, text, text, jsonb) from public;
revoke all on function public.merge_rfp_project(uuid, uuid, uuid, text, text, jsonb) from anon, authenticated;
grant execute on function public.merge_rfp_project(uuid, uuid, uuid, text, text, jsonb) to service_role;

notify pgrst, 'reload schema';
