-- 0193 - Edit in Excel (docs/EXCEL_EDIT.md).
--
-- Internal users open an .xlsx/.xlsm project file in Excel for the web. The
-- backend copies the file to the MS_DRIVE_OWNER OneDrive, grants the user
-- write access and pulls Excel's autosaves back on a timer. One editing
-- session becomes one new version of the file; every earlier version's
-- object is kept.
--
-- (0192 is reserved by docs/ASSISTANT_BROWSER_V2_PLAN.md.)

-- Every stored content of a file. Version 1 is the content the file had
-- before its first edit, recorded when that edit first syncs; a file that was
-- never edited has no rows. `project_files.storage_path` always points at the
-- newest version's object.
create table if not exists project_file_versions (
  id               uuid primary key default gen_random_uuid(),
  file_id          uuid not null references project_files(id) on delete cascade,
  project_id       uuid not null references projects(id) on delete cascade,
  version_no       int not null check (version_no >= 1),
  storage_path     text not null,
  filename         text not null,
  size_bytes       bigint,
  sha256           text,
  source           text not null check (source in ('original', 'excel_edit')),
  edit_session_id  uuid,
  created_by       uuid references profiles(id) on delete set null,
  -- Display name Microsoft reports for the last person who changed the
  -- workbook during the session (co-authors may differ from created_by).
  last_modified_by text,
  created_at       timestamptz not null default now(),
  updated_at       timestamptz not null default now(),
  unique (file_id, version_no)
);
create index if not exists project_file_versions_file_idx on project_file_versions(file_id);

-- One row per Excel for the web editing session. file_id goes null when the
-- file is deleted mid-session, so the poller can still remove the OneDrive
-- copy and close the row.
create table if not exists file_edit_sessions (
  id                uuid primary key default gen_random_uuid(),
  project_id        uuid not null references projects(id) on delete cascade,
  file_id           uuid references project_files(id) on delete set null,
  started_by        uuid references profiles(id) on delete set null,
  drive_item_id     text not null,
  drive_path        text not null,
  edit_url          text not null,
  -- Microsoft's content tag of the last copy pulled back (or uploaded).
  last_ctag         text,
  -- The version this session writes; null until the first change syncs.
  version_id        uuid references project_file_versions(id) on delete set null,
  status            text not null default 'open' check (status in ('open', 'closed')),
  last_change_at    timestamptz,
  last_synced_at    timestamptz,
  last_modified_by  text,
  -- Profiles granted write access on the OneDrive copy.
  granted_user_ids  uuid[] not null default '{}',
  -- This session's own earlier writes, replaced by a later sync. Kept until
  -- the session closes so a link signed mid-session keeps working.
  superseded_paths  text[] not null default '{}',
  -- Per-tick claim so the two API workers never sync one session at once.
  lease_until       timestamptz,
  close_reason      text,
  last_error        text,
  closed_at         timestamptz,
  created_at        timestamptz not null default now()
);
-- At most one open session per file: a second person joins it.
create unique index if not exists file_edit_sessions_one_open
  on file_edit_sessions(file_id) where status = 'open';
create index if not exists file_edit_sessions_open_idx
  on file_edit_sessions(status) where status = 'open';

alter table project_file_versions drop constraint if exists project_file_versions_session_fk;
alter table project_file_versions add constraint project_file_versions_session_fk
  foreign key (edit_session_id) references file_edit_sessions(id) on delete set null;

alter table project_file_versions enable row level security;
alter table project_file_versions force row level security;
alter table file_edit_sessions enable row level security;
alter table file_edit_sessions force row level security;
revoke all on table project_file_versions from public, anon, authenticated;
revoke all on table file_edit_sessions from public, anon, authenticated;
grant all on table project_file_versions to service_role;
grant all on table file_edit_sessions to service_role;

notify pgrst, 'reload schema';
