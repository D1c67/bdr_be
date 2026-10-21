-- 0161 - Per-user file access tracking (file_access_events).
--
-- Answers "did user X open file Y in project Z, and how many times". Before
-- this, the only trail was audit_log (file.download / file.preview /
-- file.export) keyed by the file id alone: no project, no filename, and the
-- frontend's hover prefetch minted /download URLs that looked exactly like
-- real downloads.
--
-- One row per access. `action` separates a real open ('view'), an attachment
-- download ('download'), a ZIP export ('export', one row per file in the
-- archive sharing `export_id`) and a hover prefetch ('prefetch', kept for
-- completeness but hidden from the reports by default). `source` names the
-- table the file lives in, so a file id is only meaningful together with it.
--
-- project_id / file_id carry NO foreign keys on purpose: the history must
-- survive the project or the file being deleted. filename and category are
-- snapshots taken at access time for the same reason. The actor FK mirrors
-- audit_log (0011): a deleted user nulls out rather than blocking the delete.
--
-- Server-only table: written and read by the service-role backend.

create table if not exists public.file_access_events (
  id          uuid primary key default gen_random_uuid(),
  created_at  timestamptz not null default now(),
  actor_id    uuid references profiles(id) on delete set null,
  actor_role  text,
  project_id  uuid,
  source      text not null check (source in (
    'project_file',
    'bid_split_file',
    'bid_split_segment',
    'ai_estimating_document',
    'ai_estimating_deliverable',
    'submittal_file',
    'email_attachment'
  )),
  file_id     uuid,
  filename    text,
  category    text,
  action      text not null check (action in ('view', 'download', 'export', 'prefetch')),
  export_id   uuid,
  detail      jsonb
);

create index if not exists file_access_events_project_idx
  on public.file_access_events (project_id, created_at desc);
create index if not exists file_access_events_file_idx
  on public.file_access_events (file_id, created_at desc);
create index if not exists file_access_events_actor_idx
  on public.file_access_events (actor_id, created_at desc);
create index if not exists file_access_events_source_file_idx
  on public.file_access_events (source, file_id);
-- The backfill guard below (and any re-run of it) looks rows up by audit id.
create index if not exists file_access_events_audit_id_idx
  on public.file_access_events ((detail->>'audit_id'))
  where detail ? 'audit_id';

comment on table public.file_access_events is
  'One row per file access (view/download/export/prefetch). No FK on project_id/file_id so history outlives deletes; filename/category are snapshots.';
comment on column public.file_access_events.export_id is
  'Groups the rows written for one ZIP export (one row per file in the archive).';

-- RLS deny-by-default (the service-role backend bypasses it); see 0007 / 0055.
alter table public.file_access_events enable row level security;
alter table public.file_access_events force row level security;
revoke all on table public.file_access_events from anon, authenticated;

-- ── Backfill from audit_log ─────────────────────────────────────────────────
-- Every backfilled row carries detail.backfill = true and detail.audit_id, and
-- the not-exists guard on audit_id makes a re-run a no-op. actor_role is the
-- actor's CURRENT role (audit_log never recorded one).

-- file.download / file.preview: entity_id is the project_files id. A file
-- deleted since then still backfills, just without project/filename/category.
insert into public.file_access_events
  (created_at, actor_id, actor_role, project_id, source, file_id, filename, category, action, detail)
select
  a.created_at,
  a.actor_id,
  p.role::text,
  f.project_id,
  'project_file',
  a.entity_id,
  f.filename,
  f.category::text,
  case a.action when 'file.download' then 'download' else 'view' end,
  (case when jsonb_typeof(a.payload) = 'object' then a.payload else '{}'::jsonb end)
    || jsonb_build_object('backfill', true, 'audit_id', a.id::text)
from audit_log a
left join project_files f on f.id = a.entity_id
left join profiles p on p.id = a.actor_id
where a.action in ('file.download', 'file.preview')
  and not exists (
    select 1 from public.file_access_events e
    where e.detail ? 'audit_id' and e.detail->>'audit_id' = a.id::text
  );

-- file.export: entity = 'project', entity_id = the project. The archive's file
-- list was never recorded, so this is one project-level row with file_id null.
insert into public.file_access_events
  (created_at, actor_id, actor_role, project_id, source, file_id, filename, category, action, detail)
select
  a.created_at,
  a.actor_id,
  p.role::text,
  case when a.entity = 'project' then a.entity_id end,
  'project_file',
  null,
  null,
  null,
  'export',
  (case when jsonb_typeof(a.payload) = 'object' then a.payload else '{}'::jsonb end)
    || jsonb_build_object('backfill', true, 'audit_id', a.id::text)
from audit_log a
left join profiles p on p.id = a.actor_id
where a.action = 'file.export'
  and not exists (
    select 1 from public.file_access_events e
    where e.detail ? 'audit_id' and e.detail->>'audit_id' = a.id::text
  );

notify pgrst, 'reload schema';
