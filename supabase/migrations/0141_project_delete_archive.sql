-- 0141 - Delete a project, recoverably (docs/PROJECT_DELETE.md). Apply after 0140.
--
-- RFP ingestion creates bidding projects on its own, and some of them should
-- not exist. A person deletes one; the app keeps a full record so an IT Admin
-- can put it back exactly as it was.
--
-- A HARD delete with a full snapshot, not a soft delete: a soft delete would
-- need every query in the app filtered. What lands here:
--
--   deleted_projects            one row per deletion (a second delete of the
--                               same project after a restore is a new row):
--                               who, when, the reason code (a column, so how
--                               often RFP ingestion was wrong is one query),
--                               the note, every row that went with the project
--                               (snapshot), every outside column the delete
--                               set to null (set_null_links), and the restore.
--   create_blocked_archive_id   on rfp_emails, rfp_portal_invitations and
--                               rfp_harvests: a project made from this source
--                               was deleted, so the pipeline and the "Create
--                               project" button never create it again. A
--                               restore lifts it.
--   archive_and_delete_project  the delete, one transaction.
--   restore_deleted_project     the restore, one transaction.
--   project_delete_preview      what a delete would remove (the modal's counts).
--   project_archive_collect,    internal helpers (service role only): the FK
--   project_archive_delete_rows walk and the ordered row delete.
--
-- The functions walk the foreign-key graph from pg_constraint at run time, so
-- a table added by a later migration is archived and restored without a code
-- change. Storage objects under {project_id}/ are never swept by a delete, so
-- a restore gets its files back.
--
-- set_updated_at learns one switch (bdr.preserve_updated_at): the restore's
-- second pass re-points a few columns inside the restored rows and must not
-- stamp them with a new updated_at. Nothing else sets it.
--
-- Every statement is idempotent.

-- ── 1. deleted_projects ─────────────────────────────────────────────────────

create table if not exists deleted_projects (
  id                uuid primary key default gen_random_uuid(),
  -- Not a foreign key: the project is gone while this row matters.
  project_id        uuid not null,
  project_number    text,
  project_name      text,
  project_stage     text,
  -- coalesce(actual_bid_at, internal_bid_at): the create-time tombstone check
  -- reads only archives inside the matcher's candidate window.
  project_bid_at    timestamptz,
  test_session_id   uuid,
  gc_ids            uuid[] not null default '{}',
  gc_names          text[] not null default '{}',
  -- [{gc_id, needs_by}] from project_gcs, for the tombstone check's dates.
  gc_links          jsonb not null default '[]'::jsonb,
  reason            text not null
                    constraint deleted_projects_reason_check
                    check (reason in ('rfp_should_not_exist', 'duplicate', 'created_by_mistake', 'other')),
  note              text
                    constraint deleted_projects_note_len
                    check (note is null or char_length(note) <= 2000),
  from_rfp          boolean not null default false,
  rfp_source_kind   text,
  rfp_source_id     uuid,
  rfp_source_label  text,
  deleted_by        uuid references profiles(id) on delete set null,
  deleted_at        timestamptz not null default now(),
  -- The projects row as it was (also inside snapshot; kept apart so a light
  -- read never pulls the whole snapshot).
  project_row       jsonb not null,
  -- {"version": 1, "order": [table, ...], "tables": {table: [row, ...]}}:
  -- every row the delete removed, the project included, as to_jsonb(row).
  snapshot          jsonb not null,
  -- [{table, pk, columns, values}]: rows OUTSIDE the project that pointed at
  -- a removed row through an ON DELETE SET NULL key, with the value they had.
  set_null_links    jsonb not null default '[]'::jsonb,
  row_counts        jsonb not null default '{}'::jsonb,
  row_total         integer not null default 0,
  restored_at       timestamptz,
  restored_by       uuid references profiles(id) on delete set null,
  restore_notes     jsonb,
  restore_error     text,
  created_at        timestamptz not null default now(),
  updated_at        timestamptz not null default now(),
  constraint deleted_projects_other_note
    check (reason <> 'other' or nullif(btrim(coalesce(note, '')), '') is not null)
);

comment on table deleted_projects is
  'One row per project deletion (docs/PROJECT_DELETE.md): the reason, the note, a snapshot of every removed row and the SET NULL links, and the restore. Kept after a restore so the trail is permanent. Service role only.';
comment on column deleted_projects.reason is
  'rfp_should_not_exist | duplicate | created_by_mistake | other (other requires a note).';

create index if not exists deleted_projects_deleted_idx
  on deleted_projects (deleted_at desc, id desc);
create index if not exists deleted_projects_reason_idx
  on deleted_projects (reason, deleted_at desc);
create index if not exists deleted_projects_open_bid_idx
  on deleted_projects (project_bid_at) where restored_at is null;
-- One open (not restored) archive per project: a project that exists again
-- was restored; deleting it again writes a new row.
create unique index if not exists deleted_projects_one_open
  on deleted_projects (project_id) where restored_at is null;

drop trigger if exists deleted_projects_updated_at on deleted_projects;
create trigger deleted_projects_updated_at before update on deleted_projects
  for each row execute function set_updated_at();

alter table deleted_projects enable row level security;
alter table deleted_projects force row level security;

-- ── 2. The RFP create block ─────────────────────────────────────────────────

alter table rfp_emails
  add column if not exists create_blocked_archive_id uuid references deleted_projects(id) on delete set null;
alter table rfp_portal_invitations
  add column if not exists create_blocked_archive_id uuid references deleted_projects(id) on delete set null;
alter table rfp_harvests
  add column if not exists create_blocked_archive_id uuid references deleted_projects(id) on delete set null;

comment on column rfp_emails.create_blocked_archive_id is
  'A project made from (or merged with) this email was deleted: the create step and the Create project button refuse it. Cleared by the restore.';
comment on column rfp_portal_invitations.create_blocked_archive_id is
  'A project made from (or merged with) this invitation was deleted: no project is created from it again. Cleared by the restore.';
comment on column rfp_harvests.create_blocked_archive_id is
  'The project made from this harvest was deleted: no email or invitation sharing the harvest creates a project. Cleared by the restore.';

create index if not exists rfp_emails_create_blocked_idx
  on rfp_emails (create_blocked_archive_id) where create_blocked_archive_id is not null;
create index if not exists rfp_portal_invitations_create_blocked_idx
  on rfp_portal_invitations (create_blocked_archive_id) where create_blocked_archive_id is not null;
create index if not exists rfp_harvests_create_blocked_idx
  on rfp_harvests (create_blocked_archive_id) where create_blocked_archive_id is not null;

-- ── 3. set_updated_at: the restore's switch ─────────────────────────────────

create or replace function public.set_updated_at()
returns trigger
language plpgsql
set search_path = ''
as $$
begin
  -- Set (transaction-local) only by restore_deleted_project while it puts
  -- back columns inside rows it has just re-inserted.
  if current_setting('bdr.preserve_updated_at', true) = 'on' then
    return new;
  end if;
  new.updated_at = now();
  return new;
end;
$$;

-- ── 4. Graph helpers ────────────────────────────────────────────────────────

-- Every foreign key INTO a public table: child, parent (regclass text under
-- this search_path, so public tables are unqualified), the delete action
-- (c cascade, n set null, d set default, r restrict, a no action) and the
-- column lists in key order.
create or replace function public.project_archive_fks()
returns table (
  conname text, child text, parent text, del text,
  ccols text[], ctypes text[], cnotnull boolean[], pcols text[]
)
language sql
stable
security definer
set search_path = pg_catalog, public
as $$
  select
    c.conname::text,
    c.conrelid::regclass::text,
    c.confrelid::regclass::text,
    c.confdeltype::text,
    array(select a.attname::text
            from unnest(c.conkey) with ordinality k(n, o)
            join pg_attribute a on a.attrelid = c.conrelid and a.attnum = k.n
           order by k.o),
    array(select format_type(a.atttypid, a.atttypmod)
            from unnest(c.conkey) with ordinality k(n, o)
            join pg_attribute a on a.attrelid = c.conrelid and a.attnum = k.n
           order by k.o),
    array(select a.attnotnull
            from unnest(c.conkey) with ordinality k(n, o)
            join pg_attribute a on a.attrelid = c.conrelid and a.attnum = k.n
           order by k.o),
    array(select a.attname::text
            from unnest(c.confkey) with ordinality k(n, o)
            join pg_attribute a on a.attrelid = c.confrelid and a.attnum = k.n
           order by k.o)
  from pg_constraint c
  join pg_class pc on pc.oid = c.confrelid
  where c.contype = 'f'
    and pc.relnamespace = 'public'::regnamespace
$$;

-- `jsonb_build_object('<pk col>', <alias>.<pk col>, ...)` for a table.
create or replace function public.project_archive_pk_expr(p_tbl text, p_alias text)
returns text
language sql
stable
security definer
set search_path = pg_catalog, public
as $$
  select 'jsonb_build_object(' || string_agg(format('%L, %I.%I', a.attname, p_alias, a.attname), ', ' order by k.o) || ')'
    from pg_index i
    cross join lateral unnest(i.indkey::int2[]) with ordinality k(n, o)
    join pg_attribute a on a.attrelid = i.indrelid and a.attnum = k.n
   where i.indrelid = p_tbl::regclass and i.indisprimary
$$;

-- `<alias>.<pk col> = (<json>->>'<pk col>')::<type> and ...` for a table.
create or replace function public.project_archive_pk_match(p_tbl text, p_alias text, p_json text)
returns text
language sql
stable
security definer
set search_path = pg_catalog, public
as $$
  select string_agg(
           format('%I.%I = (%s->>%L)::%s', p_alias, a.attname, p_json, a.attname,
                  format_type(a.atttypid, a.atttypmod)),
           ' and ' order by k.o)
    from pg_index i
    cross join lateral unnest(i.indkey::int2[]) with ordinality k(n, o)
    join pg_attribute a on a.attrelid = i.indrelid and a.attnum = k.n
   where i.indrelid = p_tbl::regclass and i.indisprimary
$$;

-- Fills the transaction's temp tables for one project:
--   _pda_rows      every row an ON DELETE CASCADE chain from the project reaches,
--                  transitively (the project itself at pass 0)
--   _pda_links     rows OUTSIDE that set whose SET NULL / SET DEFAULT key points
--                  into it, with the value they hold now
--   _pda_blockers  RESTRICT / NO ACTION referrers outside the set (the delete
--                  refuses while any exist)
create or replace function public.project_archive_collect(p_project_id uuid)
returns void
language plpgsql
volatile
security definer
set search_path = pg_catalog, public, pg_temp
as $$
declare
  fk        record;
  v_pass    integer := 0;
  v_added   bigint;
  v_n       bigint;
  v_cols    text;
  v_vals    text;
  v_in      text;
  v_pk      text;
begin
  create temp table if not exists _pda_fk (
    conname text, child text, parent text, del text,
    ccols text[], ctypes text[], cnotnull boolean[], pcols text[]
  ) on commit drop;
  create temp table if not exists _pda_rows (
    tbl text not null, pk jsonb not null, row jsonb not null, pass integer not null,
    primary key (tbl, pk)
  ) on commit drop;
  create temp table if not exists _pda_links (
    tbl text not null, pk jsonb not null, cols text[] not null, vals jsonb not null
  ) on commit drop;
  create temp table if not exists _pda_blockers (
    tbl text not null, conname text not null, n bigint not null
  ) on commit drop;
  truncate pg_temp._pda_fk, pg_temp._pda_rows, pg_temp._pda_links, pg_temp._pda_blockers;
  insert into pg_temp._pda_fk select * from public.project_archive_fks();

  insert into pg_temp._pda_rows (tbl, pk, row, pass)
  select 'projects', jsonb_build_object('id', p.id), to_jsonb(p), 0
    from projects p where p.id = p_project_id;

  -- Cascade closure: repeat until a pass adds nothing (a row reached twice
  -- through two chains is kept once).
  loop
    v_pass := v_pass + 1;
    v_added := 0;
    for fk in
      select f.* from pg_temp._pda_fk f
       where f.del = 'c' and f.parent in (select distinct r.tbl from pg_temp._pda_rows r)
    loop
      select string_agg(format('c.%I', col), ', ' order by o),
             string_agg(format('(r.row->>%L)::%s', pcol, typ), ', ' order by o)
        into v_cols, v_vals
        from unnest(fk.ccols, fk.pcols, fk.ctypes) with ordinality u(col, pcol, typ, o);
      -- Rows are keyed by their primary key: a table without one cannot be archived.
      v_pk := public.project_archive_pk_expr(fk.child, 'c');
      if v_pk is null then
        raise exception 'project_delete:no_primary_key' using detail = fk.child;
      end if;
      execute format(
        'insert into pg_temp._pda_rows (tbl, pk, row, pass)
         select %L, %s, to_jsonb(c), %s from %s c
          where (%s) in (select %s from pg_temp._pda_rows r where r.tbl = %L)
         on conflict do nothing',
        fk.child, v_pk, v_pass, fk.child,
        v_cols, v_vals, fk.parent);
      get diagnostics v_n = row_count;
      v_added := v_added + v_n;
    end loop;
    exit when v_added = 0;
    if v_pass > 64 then
      raise exception 'project_delete:graph_too_deep';
    end if;
  end loop;

  -- Outside referrers.
  for fk in
    select f.* from pg_temp._pda_fk f
     where f.del in ('n', 'd', 'r', 'a')
       and f.parent in (select distinct r.tbl from pg_temp._pda_rows r)
  loop
    select string_agg(format('c.%I', col), ', ' order by o),
           string_agg(format('(r.row->>%L)::%s', pcol, typ), ', ' order by o)
      into v_cols, v_vals
      from unnest(fk.ccols, fk.pcols, fk.ctypes) with ordinality u(col, pcol, typ, o);
    v_pk := public.project_archive_pk_expr(fk.child, 'c');
    if v_pk is null then
      raise exception 'project_delete:no_primary_key' using detail = fk.child;
    end if;
    v_in := format(
      '(%s) in (select %s from pg_temp._pda_rows r where r.tbl = %L)
       and not exists (select 1 from pg_temp._pda_rows x where x.tbl = %L and x.pk = %s)',
      v_cols, v_vals, fk.parent, fk.child, v_pk);
    if fk.del in ('n', 'd') then
      execute format(
        'insert into pg_temp._pda_links (tbl, pk, cols, vals)
         select %L, %s, %L::text[], jsonb_build_array(%s) from %s c where %s',
        fk.child, v_pk, fk.ccols, v_cols, fk.child, v_in);
    else
      execute format('select count(*) from %s c where %s', fk.child, v_in) into v_n;
      if v_n > 0 then
        insert into pg_temp._pda_blockers (tbl, conname, n) values (fk.child, fk.conname, v_n);
      end if;
    end if;
  end loop;
end;
$$;

-- Deletes every collected row in an order the NO ACTION / RESTRICT keys
-- inside the set allow. A plain `delete from projects` is not enough: a
-- cascade runs as its own internal statement, so project_files (cascaded
-- straight from the project) would be checked against quotes.quote_file_id
-- and rfqs.split_file_id (NO ACTION) before the quotes and RFQs further down
-- the chain are gone. For every such key child -> parent, the child's rows
-- go before the parent's and before every table whose cascade reaches the
-- parent. Cascades and SET NULL keys then only touch rows already archived.
create or replace function public.project_archive_delete_rows()
returns void
language plpgsql
volatile
security definer
set search_path = pg_catalog, public, pg_temp
as $$
declare
  v_tables     text[];
  v_remaining  text[];
  v_pick       text;
  t            text;
  v_in         text;
begin
  select array_agg(distinct r.tbl) into v_tables from pg_temp._pda_rows r;
  create temp table if not exists _pda_before (a text not null, b text not null) on commit drop;
  truncate pg_temp._pda_before;
  insert into pg_temp._pda_before (a, b)
  with recursive anc(tbl, ancestor) as (
    select x.tbl, x.tbl from unnest(v_tables) x(tbl)
    union
    select anc.tbl, f.parent
      from anc
      join pg_temp._pda_fk f
        on f.child = anc.ancestor and f.del = 'c'
       and f.parent = any(v_tables) and f.parent <> f.child
  )
  select distinct f.child, anc.ancestor
    from pg_temp._pda_fk f
    join anc on anc.tbl = f.parent
   where f.del in ('a', 'r')
     and f.child = any(v_tables) and f.parent = any(v_tables)
     and f.child <> anc.ancestor;

  v_remaining := v_tables;
  while coalesce(array_length(v_remaining, 1), 0) > 0 loop
    v_pick := null;
    foreach t in array v_remaining loop
      if not exists (select 1 from pg_temp._pda_before o
                      where o.b = t and o.a <> t and o.a = any(v_remaining)) then
        v_pick := t;
        exit;
      end if;
    end loop;
    if v_pick is null then
      raise exception 'project_delete:delete_cycle' using detail = array_to_string(v_remaining, ', ');
    end if;
    select format('(%s) in (select %s from pg_temp._pda_rows r where r.tbl = %L)',
                  string_agg(format('c.%I', a.attname), ', ' order by k.o),
                  string_agg(format('(r.pk->>%L)::%s', a.attname, format_type(a.atttypid, a.atttypmod)),
                             ', ' order by k.o),
                  v_pick)
      into v_in
      from pg_index i
      cross join lateral unnest(i.indkey::int2[]) with ordinality k(n, o)
      join pg_attribute a on a.attrelid = i.indrelid and a.attnum = k.n
     where i.indrelid = v_pick::regclass and i.indisprimary;
    execute format('delete from %s c where %s', v_pick, v_in);
    v_remaining := array_remove(v_remaining, v_pick);
  end loop;
end;
$$;

-- ── 5. project_delete_preview ───────────────────────────────────────────────

create or replace function public.project_delete_preview(p_project_id uuid)
returns jsonb
language plpgsql
volatile
security definer
set search_path = pg_catalog, public, pg_temp
as $$
begin
  if not exists (select 1 from projects where id = p_project_id) then
    raise exception 'project_delete:not_found';
  end if;
  perform public.project_archive_collect(p_project_id);
  return jsonb_build_object(
    'counts', coalesce((select jsonb_object_agg(s.tbl, s.n)
                          from (select tbl, count(*) as n from pg_temp._pda_rows group by tbl) s), '{}'::jsonb),
    'total', (select count(*) from pg_temp._pda_rows),
    'links', (select count(*) from pg_temp._pda_links),
    'blockers', coalesce((select jsonb_agg(jsonb_build_object('table', b.tbl, 'constraint', b.conname, 'rows', b.n))
                            from pg_temp._pda_blockers b), '[]'::jsonb)
  );
end;
$$;

-- ── 6. archive_and_delete_project ───────────────────────────────────────────
-- Errors are raised as 'project_delete:<code>' (docs/PROJECT_DELETE.md 4) so
-- the backend maps them to a readable 404 / 409 / 422.

create or replace function public.archive_and_delete_project(
  p_project_id uuid, p_actor uuid, p_reason text, p_note text, p_confirm_name text
)
returns jsonb
language plpgsql
volatile
security definer
set search_path = pg_catalog, public, pg_temp
as $$
declare
  v_p          projects%rowtype;
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
  if p_reason is null or p_reason not in ('rfp_should_not_exist', 'duplicate', 'created_by_mistake', 'other') then
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
    row_counts, row_total
  ) values (
    v_id, v_p.id, v_p.number, v_p.name, v_p.current_stage::text,
    coalesce(v_p.actual_bid_at, v_p.internal_bid_at), v_p.test_session_id,
    v_gc_ids, v_gc_names, v_gc_links, p_reason, v_note, v_from_rfp,
    case when v_from_rfp then v_rec.source_kind end, v_src_id, left(v_src_label, 300),
    p_actor, v_now, to_jsonb(v_p),
    jsonb_build_object('version', 1, 'order', v_order, 'tables', v_tables),
    v_links, v_counts, v_total
  );

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

-- ── 7. restore_deleted_project ──────────────────────────────────────────────
-- Re-inserts the snapshot in foreign-key order (parents first, computed from
-- the CURRENT schema; a cycle is broken on a nullable key, which is put back
-- in a second pass), nulls a nullable reference whose target no longer
-- exists (recorded in restore_notes) and refuses a NOT NULL one, relinks the
-- SET NULL columns that are still null, lifts the RFP block, and stamps the
-- archive. Inserts fire no trigger here (the only row triggers on these
-- tables are BEFORE UPDATE set_updated_at), so a restore sends nothing,
-- enqueues nothing and writes no stage event of its own. Atomic: any error
-- rolls all of it back.

create or replace function public.restore_deleted_project(p_archive_id uuid, p_actor uuid)
returns jsonb
language plpgsql
volatile
security definer
set search_path = pg_catalog, public, pg_temp
as $$
declare
  v_a          deleted_projects%rowtype;
  v_other      record;
  v_tables     text[];
  v_remaining  text[];
  v_order      text[] := '{}';
  v_pick       text;
  t            text;
  c            text;
  v_deferred   jsonb := '{}'::jsonb;
  v_rows       jsonb;
  v_cols       text;
  v_set        text;
  v_null       text;
  v_n          bigint;
  v_want       bigint;
  v_inserted   bigint := 0;
  v_notes      jsonb := '[]'::jsonb;
  fk           record;
  g            record;
  v_relinked   bigint := 0;
  v_skipped    bigint := 0;
  v_unblocked  bigint := 0;
  v_now        timestamptz := now();
begin
  select * into v_a from deleted_projects where id = p_archive_id for update;
  if not found then
    raise exception 'project_delete:archive_not_found';
  end if;
  if v_a.restored_at is not null then
    raise exception 'project_delete:already_restored';
  end if;
  if exists (select 1 from projects where id = v_a.project_id) then
    raise exception 'project_delete:project_exists';
  end if;
  select p.id, p.number, p.name into v_other
    from projects p
   where lower(btrim(p.number)) = lower(btrim(v_a.project_number))
   limit 1;
  if found then
    raise exception 'project_delete:number_taken'
      using detail = btrim(format('%s %s', v_other.number, coalesce(v_other.name, '')));
  end if;

  select array_agg(k.name order by k.name) into v_tables
    from jsonb_object_keys(v_a.snapshot->'tables') k(name);
  foreach t in array coalesce(v_tables, '{}') loop
    if to_regclass(t) is null then
      raise exception 'project_delete:table_missing' using detail = t;
    end if;
  end loop;

  create temp table if not exists _pda_fk (
    conname text, child text, parent text, del text,
    ccols text[], ctypes text[], cnotnull boolean[], pcols text[]
  ) on commit drop;
  truncate pg_temp._pda_fk;
  insert into pg_temp._pda_fk select * from public.project_archive_fks();

  -- Topological order over the snapshot's tables (self references ignored: a
  -- single INSERT checks them at the end of the statement).
  v_remaining := coalesce(v_tables, '{}');
  while coalesce(array_length(v_remaining, 1), 0) > 0 loop
    v_pick := null;
    foreach t in array v_remaining loop
      if not exists (select 1 from pg_temp._pda_fk f
                      where f.child = t and f.parent <> t and f.parent = any(v_remaining)) then
        v_pick := t;
        exit;
      end if;
    end loop;
    if v_pick is null then
      -- A cycle: take a table whose keys into what is left are all nullable,
      -- and put those columns back after every table is in.
      foreach t in array v_remaining loop
        if not exists (select 1 from pg_temp._pda_fk f
                        where f.child = t and f.parent <> t and f.parent = any(v_remaining)
                          and true = any(f.cnotnull)) then
          v_pick := t;
          v_deferred := v_deferred || jsonb_build_object(t, (
            select jsonb_agg(distinct u.col)
              from pg_temp._pda_fk f, unnest(f.ccols) u(col)
             where f.child = t and f.parent <> t and f.parent = any(v_remaining)));
          exit;
        end if;
      end loop;
    end if;
    if v_pick is null then
      raise exception 'project_delete:restore_cycle' using detail = array_to_string(v_remaining, ', ');
    end if;
    v_order := v_order || v_pick;
    v_remaining := array_remove(v_remaining, v_pick);
  end loop;

  begin
    foreach t in array v_order loop
      v_rows := v_a.snapshot->'tables'->t;
      v_want := coalesce(jsonb_array_length(v_rows), 0);
      continue when v_want = 0;
      if v_deferred ? t then
        select jsonb_agg(r || (select jsonb_object_agg(d.col, 'null'::jsonb)
                                 from jsonb_array_elements_text(v_deferred->t) d(col)))
          into v_rows from jsonb_array_elements(v_rows) r;
      end if;

      -- References to rows that are gone since the delete (a GC, a contact,
      -- a user, a test session): a nullable one is cleared and noted, a
      -- NOT NULL one refuses the restore.
      for fk in
        select f.* from pg_temp._pda_fk f
         where f.child = t and f.parent <> t and array_length(f.ccols, 1) = 1
           and not (coalesce(v_deferred->t, '[]'::jsonb) ? f.ccols[1])
      loop
        execute format(
          'select count(*) from jsonb_array_elements($1) r
            where (r->>%L) is not null
              and not exists (select 1 from %s p where p.%I = (r->>%L)::%s)',
          fk.ccols[1], fk.parent, fk.pcols[1], fk.ccols[1], fk.ctypes[1])
          using v_rows into v_n;
        if v_n > 0 then
          if fk.cnotnull[1] then
            raise exception 'project_delete:missing_reference'
              using detail = format('%s.%s -> %s (%s)', t, fk.ccols[1], fk.parent, v_n);
          end if;
          execute format(
            'select jsonb_agg(case when (r->>%L) is not null
                                     and not exists (select 1 from %s p where p.%I = (r->>%L)::%s)
                                   then r || jsonb_build_object(%L, null) else r end)
               from jsonb_array_elements($1) r',
            fk.ccols[1], fk.parent, fk.pcols[1], fk.ccols[1], fk.ctypes[1], fk.ccols[1])
            using v_rows into v_rows;
          v_notes := v_notes || jsonb_build_object(
            'table', t, 'column', fk.ccols[1], 'references', fk.parent, 'rows_cleared', v_n);
        end if;
      end loop;

      select string_agg(format('%I', a.attname), ', ' order by a.attnum) into v_cols
        from pg_attribute a
       where a.attrelid = t::regclass and a.attnum > 0 and not a.attisdropped
         and a.attgenerated = '' and (v_rows->0) ? a.attname::text;
      execute format(
        'insert into %s (%s) overriding system value select %s from jsonb_populate_recordset(null::%s, $1)',
        t, v_cols, v_cols, t) using v_rows;
      get diagnostics v_n = row_count;
      if v_n <> v_want then
        raise exception 'project_delete:restore_count' using detail = format('%s: %s of %s', t, v_n, v_want);
      end if;
      v_inserted := v_inserted + v_n;
    end loop;

    -- The deferred columns, now that every row is in. updated_at stays as
    -- the snapshot had it.
    perform set_config('bdr.preserve_updated_at', 'on', true);
    for t in select jsonb_object_keys(v_deferred) loop
      for c in select jsonb_array_elements_text(v_deferred->t) loop
        for fk in
          select f.* from pg_temp._pda_fk f
           where f.child = t and array_length(f.ccols, 1) = 1 and f.ccols[1] = c
           limit 1
        loop
          execute format(
            'update %s x set %I = (r->>%L)::%s
               from jsonb_array_elements($1) r
              where %s and (r->>%L) is not null
                and exists (select 1 from %s p where p.%I = (r->>%L)::%s)',
            t, c, c, fk.ctypes[1], public.project_archive_pk_match(t, 'x', 'r'), c,
            fk.parent, fk.pcols[1], c, fk.ctypes[1])
            using v_a.snapshot->'tables'->t;
        end loop;
      end loop;
    end loop;

    -- The SET NULL links: only where the outside row still exists and the
    -- column is still null (a person may have pointed it elsewhere since).
    for g in
      select l->>'table' as tbl, l->'columns' as cols, jsonb_agg(l) as links, count(*) as n
        from jsonb_array_elements(v_a.set_null_links) l
       group by 1, 2
    loop
      if to_regclass(g.tbl) is null then
        v_skipped := v_skipped + g.n;
        continue;
      end if;
      select string_agg(format('%I = (l->''values''->>%s)::%s', u.col, u.o - 1,
                               format_type(a.atttypid, a.atttypmod)), ', ' order by u.o),
             string_agg(format('x.%I is null', u.col), ' and ' order by u.o)
        into v_set, v_null
        from jsonb_array_elements_text(g.cols) with ordinality u(col, o)
        join pg_attribute a on a.attrelid = g.tbl::regclass and a.attname = u.col
       where not a.attisdropped;
      execute format(
        'update %s x set %s from jsonb_array_elements($1) l where %s and %s',
        g.tbl, v_set, public.project_archive_pk_match(g.tbl, 'x', 'l->''pk'''), v_null)
        using g.links;
      get diagnostics v_n = row_count;
      v_relinked := v_relinked + v_n;
      v_skipped := v_skipped + (g.n - v_n);
    end loop;
    perform set_config('bdr.preserve_updated_at', 'off', true);
  exception
    when unique_violation or foreign_key_violation or check_violation or not_null_violation then
      raise exception 'project_delete:restore_conflict' using detail = sqlerrm;
  end;

  -- Lift the RFP block. A row the block stopped at the create step since the
  -- delete (`done` / project_deleted) takes the road it would have taken:
  -- one sharing a harvest the project owns again links (`created`), any
  -- other goes back to `match`, where the restored project is a candidate.
  update rfp_emails e
     set status = case when e.harvest_id in (select h.id from rfp_harvests h where h.project_id = v_a.project_id)
                       then 'created' else 'match' end,
         created_project_id = case when e.harvest_id in (select h.id from rfp_harvests h where h.project_id = v_a.project_id)
                                   then v_a.project_id else e.created_project_id end,
         decided_at_step = case when e.harvest_id in (select h.id from rfp_harvests h where h.project_id = v_a.project_id)
                                then 'create' else null end,
         flag_reason = null, attempts = 0, next_attempt_at = null, last_error = null,
         create_blocked_archive_id = null
   where e.create_blocked_archive_id = v_a.id and e.status = 'done' and e.flag_reason = 'project_deleted';
  get diagnostics v_n = row_count;
  v_unblocked := v_unblocked + v_n;
  update rfp_portal_invitations i
     set status = case when i.harvest_id in (select h.id from rfp_harvests h where h.project_id = v_a.project_id)
                       then 'created' else 'match' end,
         created_project_id = case when i.harvest_id in (select h.id from rfp_harvests h where h.project_id = v_a.project_id)
                                   then v_a.project_id else i.created_project_id end,
         decided_at_step = case when i.harvest_id in (select h.id from rfp_harvests h where h.project_id = v_a.project_id)
                                then 'create' else null end,
         flag_reason = null, attempts = 0, next_attempt_at = null, last_error = null,
         create_blocked_archive_id = null
   where i.create_blocked_archive_id = v_a.id and i.status = 'done' and i.flag_reason = 'project_deleted';
  get diagnostics v_n = row_count;
  v_unblocked := v_unblocked + v_n;
  update rfp_emails set create_blocked_archive_id = null where create_blocked_archive_id = v_a.id;
  get diagnostics v_n = row_count;
  v_unblocked := v_unblocked + v_n;
  update rfp_portal_invitations set create_blocked_archive_id = null where create_blocked_archive_id = v_a.id;
  get diagnostics v_n = row_count;
  v_unblocked := v_unblocked + v_n;
  update rfp_harvests set create_blocked_archive_id = null where create_blocked_archive_id = v_a.id;
  get diagnostics v_n = row_count;
  v_unblocked := v_unblocked + v_n;

  update deleted_projects
     set restored_at = v_now, restored_by = p_actor, restore_error = null,
         restore_notes = jsonb_build_object(
           'rows_restored', v_inserted, 'links_relinked', v_relinked, 'links_skipped', v_skipped,
           'rfp_rows_unblocked', v_unblocked, 'references_cleared', v_notes,
           'deferred_columns', v_deferred, 'order', to_jsonb(v_order))
   where id = v_a.id;

  return jsonb_build_object(
    'archive_id', v_a.id, 'project_id', v_a.project_id, 'number', v_a.project_number,
    'name', v_a.project_name, 'restored_at', v_now, 'rows_restored', v_inserted,
    'links_relinked', v_relinked, 'links_skipped', v_skipped, 'rfp_rows_unblocked', v_unblocked,
    'references_cleared', v_notes
  );
end;
$$;

-- ── 8. Grants: service role only ────────────────────────────────────────────

revoke all on function public.project_archive_fks() from public;
revoke all on function public.project_archive_fks() from anon, authenticated;
revoke all on function public.project_archive_pk_expr(text, text) from public;
revoke all on function public.project_archive_pk_expr(text, text) from anon, authenticated;
revoke all on function public.project_archive_pk_match(text, text, text) from public;
revoke all on function public.project_archive_pk_match(text, text, text) from anon, authenticated;
revoke all on function public.project_archive_collect(uuid) from public;
revoke all on function public.project_archive_collect(uuid) from anon, authenticated;
revoke all on function public.project_archive_delete_rows() from public;
revoke all on function public.project_archive_delete_rows() from anon, authenticated;
revoke all on function public.project_delete_preview(uuid) from public;
revoke all on function public.project_delete_preview(uuid) from anon, authenticated;
revoke all on function public.archive_and_delete_project(uuid, uuid, text, text, text) from public;
revoke all on function public.archive_and_delete_project(uuid, uuid, text, text, text) from anon, authenticated;
revoke all on function public.restore_deleted_project(uuid, uuid) from public;
revoke all on function public.restore_deleted_project(uuid, uuid) from anon, authenticated;
grant execute on function public.project_delete_preview(uuid) to service_role;
grant execute on function public.archive_and_delete_project(uuid, uuid, text, text, text) to service_role;
grant execute on function public.restore_deleted_project(uuid, uuid) to service_role;

notify pgrst, 'reload schema';
