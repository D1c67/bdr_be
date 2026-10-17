-- 0151: missing files on an RFP-created project (docs/RFP_MISSING_FILES.md).
-- Apply after 0150.
--
-- When the harvest cannot bring every bid document into a project made from
-- an RFP invitation, each file or link that did not come in becomes one row
-- here, with why, the share link (https only) where one exists, and the email
-- it was attached to when there is no link. A person resolves items one by
-- one (a file of the same name landing in the project resolves its item on
-- its own), or dismisses them all with an Undo.
--
--   kind        file = a named file (attachment, zip member, folder file,
--                      platform document); link = a whole share link the
--                      harvester could not list; harvest = the whole harvest
--                      failed
--   "group"     not_downloaded = nothing reached the app, fetch it by hand;
--               refused = the sandbox or the promotion turned it away,
--               downloading it again will not help
--   item_key    the stable identity a re-sync diffs on (never duplicates,
--               never reopens a resolved row)
--   resolved_*  null while open; how = name_match | manual |
--               auto_first_file | dismiss_all
--
-- rfp_created_projects gains the total-failure mark the first-file rule reads
-- and the "Dismiss all" stamp its Undo clears. bid_split_jobs.source gains
-- 'project': a splitter job opened from the project page's drop zone, linked
-- to the project from birth so its segments are filed by trade.
--
-- Every statement is idempotent. DEV database only until the release.

create table if not exists rfp_file_issues (
  id            uuid primary key default gen_random_uuid(),
  project_id    uuid not null references projects(id) on delete cascade,
  harvest_id    uuid references rfp_harvests(id) on delete set null,
  item_key      text not null,
  kind          text not null,
  "group"       text not null,
  origin        text,
  provider      text,
  status        text not null,
  name          text,
  url           text,
  reason        text,
  email_ref     jsonb,
  resolved_at   timestamptz,
  resolved_by   uuid references profiles(id) on delete set null,
  resolved_how  text,
  created_at    timestamptz not null default now(),
  updated_at    timestamptz not null default now()
);

alter table rfp_file_issues drop constraint if exists rfp_file_issues_kind_check;
alter table rfp_file_issues add constraint rfp_file_issues_kind_check
  check (kind in ('file', 'link', 'harvest'));

alter table rfp_file_issues drop constraint if exists rfp_file_issues_group_check;
alter table rfp_file_issues add constraint rfp_file_issues_group_check
  check ("group" in ('not_downloaded', 'refused'));

alter table rfp_file_issues drop constraint if exists rfp_file_issues_resolved_how_check;
alter table rfp_file_issues add constraint rfp_file_issues_resolved_how_check
  check (resolved_how is null or resolved_how in ('name_match', 'manual', 'auto_first_file', 'dismiss_all'));

alter table rfp_file_issues drop constraint if exists rfp_file_issues_url_https_check;
alter table rfp_file_issues add constraint rfp_file_issues_url_https_check
  check (url is null or url like 'https://%');

create unique index if not exists rfp_file_issues_project_key_uidx
  on rfp_file_issues (project_id, item_key);
create index if not exists rfp_file_issues_open_idx
  on rfp_file_issues (project_id) where resolved_at is null;

drop trigger if exists rfp_file_issues_updated_at on rfp_file_issues;
create trigger rfp_file_issues_updated_at before update on rfp_file_issues
  for each row execute function set_updated_at();

alter table rfp_file_issues enable row level security;
alter table rfp_file_issues force row level security;

comment on table rfp_file_issues is
  'One row per file or link that did not make it into an RFP-created project, with why and where to get it (docs/RFP_MISSING_FILES.md).';
comment on column rfp_file_issues.item_key is
  'Stable identity for the sync diff: (kind, origin, status, name) for files and harvests, (kind, link key) for links.';
comment on column rfp_file_issues.url is
  'The GC share link or the platform bid sheet URL, https only; null for an email attachment.';
comment on column rfp_file_issues.email_ref is
  'For an attachment with no link: {from, from_address, subject, received_at, mailbox} so a person can find the message.';
comment on column rfp_file_issues.resolved_how is
  'name_match = a file of the same name landed; manual = Mark handled; auto_first_file = first drawing or spec on a total failure; dismiss_all = Dismiss all (Undo reopens exactly these).';

alter table rfp_created_projects
  add column if not exists files_issue_total        boolean not null default false,
  add column if not exists files_issue_dismissed_at timestamptz,
  add column if not exists files_issue_dismissed_by uuid references profiles(id) on delete set null;

comment on column rfp_created_projects.files_issue_total is
  'True when the harvest brought in no accepted or reused file at all (stamped once by the first sync): the first drawing or specification then resolves every open item.';
comment on column rfp_created_projects.files_issue_dismissed_at is
  'When a person dismissed every open missing-files item (null = not dismissed; Undo clears it).';
comment on column rfp_created_projects.files_issue_dismissed_by is
  'Who dismissed the missing-files items.';

alter table bid_split_jobs drop constraint if exists bid_split_jobs_source_check;
alter table bid_split_jobs add constraint bid_split_jobs_source_check
  check (source in ('manual', 'rfp', 'project'));

comment on column bid_split_jobs.source is
  'manual = uploaded on the /bid-splitter page; rfp = staged by the RFP Ingestion split step from a harvest (created_by null); project = opened from a project page''s missing-files drop zone, linked to the project from birth. An rfp or project job cannot be deleted while its project exists.';

notify pgrst, 'reload schema';
