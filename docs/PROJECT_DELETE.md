# Delete a project, recoverably

RFP ingestion creates bidding projects on its own (docs/RFP_CREATE.md), and
some of them should not exist, especially during the initial backfill. A
person with an admin role deletes one; the app keeps a full record of
everything that went with it, so an IT Admin can put it back exactly as it
was from the Deleted Projects page.

Status: built 2026-09-29, DEV ONLY. Migration `0141_project_delete_archive.sql`
is applied to the dev database (`bpidntbyvoooqvaispup`) only. Nothing here is
on staging or production.

Naming: migration `0141_project_delete_archive.sql`, table `deleted_projects`,
column `create_blocked_archive_id` on `rfp_emails`, `rfp_portal_invitations`
and `rfp_harvests`, database functions `archive_and_delete_project`,
`restore_deleted_project`, `project_delete_preview` (plus the internal
helpers `project_archive_fks`, `project_archive_pk_expr`,
`project_archive_pk_match`, `project_archive_collect`,
`project_archive_delete_rows`), service `app/services/project_delete.py`,
routes on `app/routers/projects.py` and router `app/routers/deleted_projects.py`,
audit actions `project.delete`, `project.restore`, `project.restore_failed`,
source flag `project_deleted`, FE component `components/DeleteProjectModal.tsx`,
FE page `/deleted-projects`, i18n namespaces `deleteProject` and
`deletedProjects`.

---

## 1. Decisions (taken with the user, 2026-09-29)

| Topic | Decision |
|---|---|
| Where | A Delete action on every row of "Created from RFP Ingestion" (`/rfp-created`) and on every project page (bottom action bar, beside Abandon; also shown on an abandoned project). |
| Confirmation | A modal that says, in plain words, what deleting does, then asks for the project name typed EXACTLY (trimmed, case-sensitive; the project number when there is no name). The server checks the typed text again (`confirm_name`), inside the database transaction, so a stray API call cannot delete. |
| Reason | Required, stored as a column: `rfp_should_not_exist` ("RFP ingestion should not have created this project"), `duplicate`, `created_by_mistake`, `other` (a note is required for `other`; optional otherwise, 2000 characters at most). `rfp_should_not_exist` is offered only for a project RFP ingestion created (it has an `rfp_created_projects` row) and is preselected on the RFP Created page. |
| Who | Delete: Estimating Admin, Executive, IT Admin (the RFP Created page's roles). Deleted Projects page and Restore: IT Admin only. |
| Blocked | A project in Project Management (`pm_stage` set) or Certified Payroll (`cp_enrolled_at` set) cannot be deleted: they hold billing and payroll compliance records. Every bidding state (any stage, abandoned, submitted) can. |
| RFP source | The email, invitation or harvest that made the project is marked so the pipeline never creates it again, on either path (the sweep and the "Create project" button). A restore lifts that. |
| Silence | A delete and a restore send nothing: no email, no bell, no stage event of their own. |
| Trail | The archive row stays after a restore (`restored_at`, `restored_by` filled). Deleting the same project again later writes a new row. |

## 2. Why a hard delete with a snapshot

About 64 foreign keys point at `projects(id)` (51 ON DELETE CASCADE, 13 SET
NULL), with grandchildren below them (rfqs to rfq_sends to rfq_messages,
quotes, quote notes and revisions, and so on). A soft delete would need every
query in the app (dashboards, reports, reminders, digests, pollers, analytics)
filtered by a new column. A hard delete removes the project everywhere by
construction; the snapshot makes it recoverable.

## 3. Data model (migration 0141)

`deleted_projects` (RLS enabled and forced, no policies: service role only):

| column | meaning |
|---|---|
| `id` | the archive id |
| `project_id` | the deleted project's id (NOT a foreign key: the project is gone) |
| `project_number`, `project_name`, `project_stage` | for the list |
| `project_bid_at` | `coalesce(actual_bid_at, internal_bid_at)`, read by the tombstone check (5.3) |
| `test_session_id` | the project's test-bench tag, so a test row only ever matches a test archive |
| `gc_ids`, `gc_names`, `gc_links` | the GCs at delete time (`gc_links` = `[{gc_id, needs_by}]`) |
| `reason` | CHECK in the four codes; `other` requires a non-blank note (table CHECK) |
| `note` | up to 2000 characters |
| `from_rfp`, `rfp_source_kind`, `rfp_source_id`, `rfp_source_label` | whether RFP ingestion made it, and from which email subject or portal title |
| `deleted_by`, `deleted_at` | who and when |
| `project_row` | the `projects` row as jsonb (a light read for the tombstone check) |
| `snapshot` | `{"version": 1, "order": [...], "tables": {table: [to_jsonb(row), ...]}}`: every row the delete removed, the project included |
| `set_null_links` | `[{table, pk, columns, values}]`: rows OUTSIDE the project that pointed at a removed row through a SET NULL key, with the value they held |
| `row_counts`, `row_total` | per table and in total |
| `restored_at`, `restored_by`, `restore_notes`, `restore_error` | the restore (notes: rows restored, links relinked or skipped, references cleared, deferred columns, insert order); the last failure's sentence |

Indexes: `(deleted_at desc, id desc)` for the page, `(reason, deleted_at desc)`
for the reason filter and the "how often was RFP ingestion wrong" count,
`(project_bid_at) where restored_at is null` for the tombstone check, and a
partial UNIQUE `(project_id) where restored_at is null` (one open archive per
project).

`create_blocked_archive_id uuid references deleted_projects(id) on delete set
null` on `rfp_emails`, `rfp_portal_invitations` and `rfp_harvests`, each with
a partial index.

`set_updated_at()` learns one switch: when the transaction-local setting
`bdr.preserve_updated_at` is `on` it leaves `updated_at` alone. Only
`restore_deleted_project` sets it, and only around its second pass (3 below).

How often RFP ingestion was wrong:

```sql
select count(*) from deleted_projects where reason = 'rfp_should_not_exist';
-- or per month, restored or not
select date_trunc('month', deleted_at), count(*) from deleted_projects
 where reason = 'rfp_should_not_exist' group by 1 order by 1;
```

## 4. The database functions

All are `security definer`, `search_path` pinned to `pg_catalog, public,
pg_temp`, execute revoked from `public`, `anon` and `authenticated`; the three
entry points are granted to `service_role`. Errors are raised as
`project_delete:<code>` (details in the error's `details`), which
`project_delete.ProjectDeleteError` maps to a sentence and a status.

### 4.1 The walk (`project_archive_collect`)

Reads every foreign key into a public table from `pg_constraint` (so a table a
later migration adds is covered with no code change), then, starting from the
project row, repeatedly follows every ON DELETE CASCADE key from a table it has
rows in, collecting each row as `to_jsonb` keyed by its primary key, until a
pass adds nothing (a row reached through two chains is kept once). Then, for
every SET NULL (and SET DEFAULT) key into the collected set, it records the
outside rows and the values they hold, and for every RESTRICT / NO ACTION key
it counts outside rows that point in. Transaction-local temp tables hold the
result. A table without a primary key refuses (`no_primary_key`).

### 4.2 `archive_and_delete_project(p_project_id, p_actor, p_reason, p_note, p_confirm_name)`

One transaction:

1. Reason in the four codes (`bad_reason`), a note for `other`
   (`note_required`), the note cap (`note_too_long`).
2. `select ... for update` the project (`not_found`); refuse PM
   (`pm_enrolled`) and CP (`cp_enrolled`).
3. `btrim(p_confirm_name)` must equal `coalesce(nullif(btrim(name), ''),
   btrim(number))` exactly (`name_mismatch`).
4. `rfp_should_not_exist` only when an `rfp_created_projects` row exists
   (`not_from_rfp`).
5. The walk (4.1). Any outside RESTRICT / NO ACTION referrer refuses
   (`referenced`, with the tables in the details). None exist today.
6. Insert the `deleted_projects` row with the snapshot and the links.
7. The RFP block (5.1).
8. `project_archive_delete_rows`: deletes every collected row in an order the
   NO ACTION keys inside the set allow. A plain `delete from projects` fails
   on real data: a cascade runs as its own internal statement, so
   `project_files` (cascaded straight from the project) is checked against
   `quotes.quote_file_id` and `rfqs.split_file_id` (NO ACTION) before the
   quotes and RFQs further down the chain are gone. For every such key child
   to parent, the child's rows go before the parent's and before every table
   whose cascade reaches the parent. Cascades and SET NULL keys then only
   touch rows already archived.
9. `delete from projects` (already gone; kept as a backstop).

Returns `{archive_id, project_id, number, name, from_rfp, reason, deleted_at,
row_total, links}`.

### 4.3 `restore_deleted_project(p_archive_id, p_actor)`

One transaction; any error rolls everything back:

1. Lock the archive (`archive_not_found`); refuse a restored one
   (`already_restored`) and a project id that exists again
   (`project_exists`); refuse when the project number is now another
   project's (`number_taken`, the other project named in the details; the
   0052 unique index on `lower(btrim(number))` would refuse it anyway).
2. Every snapshot table must still exist (`table_missing`).
3. Insert order from the CURRENT schema: a topological sort over every
   foreign key between the snapshot's tables (self references ignored: one
   INSERT checks them at the end of the statement). A cycle (today:
   `project_gcs.rfp_match_id` and `rfp_project_matches.project_gc_id`;
   `project_files.rfq_message_id` through rfq_messages, rfq_sends and rfqs back
   to `rfqs.split_file_id`) is broken on a table whose keys into what is left
   are all nullable: those columns go in as null and are put back in a second
   pass.
4. Per table: a nullable reference whose target is gone since the delete (a GC,
   a contact, a user, a test session) is cleared and noted in
   `restore_notes.references_cleared`; a NOT NULL one refuses
   (`missing_reference`). Then one `insert ... overriding system value select
   <columns> from jsonb_populate_recordset(null::<table>, $1)`: generated
   columns are skipped, only columns present in the snapshot are named (a
   column added since takes its default), and the inserted count must equal
   the snapshot's (`restore_count`).
5. The deferred columns, with `bdr.preserve_updated_at` on, so the restored
   rows keep the `updated_at` the snapshot had.
6. The SET NULL links: `update <table> set <col> = <old value>` only where the
   outside row still exists AND the column is still null (a person may have
   pointed it elsewhere since). Counted as relinked or skipped.
7. A unique, foreign key, check or not-null violation anywhere in 4 to 6 is
   reported as `restore_conflict` with the database's message.
8. Lift the RFP block (5.5), stamp `restored_at`, `restored_by`,
   `restore_notes`.

Side effects: the only row triggers on the archived tables are BEFORE UPDATE
`set_updated_at` (checked 2026-09-29: no INSERT trigger on any of them), so a
restore sends nothing, enqueues nothing and writes no stage event. A trigger
added to one of these tables later must be checked against this. After the
function returns, the API fails any `llm_jobs` row of the restored project
that is still `queued` or `running` (the snapshot caught it in flight and its
worker is gone), with its domain row, so nothing reruns on its own
(`llm_queue.release_restored_project_jobs`).

### 4.4 `project_delete_preview(p_project_id)`

The walk alone: `{counts: {table: n}, total, links, blockers: [{table,
constraint, rows}]}` for the modal.

## 5. The RFP block

### 5.1 At delete time (inside the transaction, before the rows go)

`create_blocked_archive_id = <archive id>` on:

- every `rfp_harvests` row that is the project's (`project_id`), the one the
  `rfp_created_projects` record names, or the harvest of any email or
  invitation that made the project or was DECIDED to be it;
- every `rfp_emails` row whose `created_project_id` is the project, or whose
  `match_project_id` is the project at `merged` / `duplicate`, the record's
  source email, every row sharing one of those harvests (Procore reminders
  share one), and every direct sibling follower of a creating row;
- every `rfp_portal_invitations` row whose `created_project_id` is the
  project, or whose `match_project_id` is the project at `exists`, the
  record's source invitation, and every row sharing one of those harvests.

A row still waiting at `review_match` with this project as its PROPOSED match
is not marked: nobody has decided it is this project, and a reviewer's "Not a
match" must still be able to create.

The foreign keys then null `created_project_id`, `match_project_id` and
`rfp_harvests.project_id` as usual; the archive's `set_null_links` keeps the
values for the restore.

### 5.2 At create time, on every path

`rfp_create.create_block_for(sb, source_kind, row)` returns the archive id that
forbids creating from a row, checking in order: the row's own mark (read fresh
when the caller's select lacks the column); its harvest's mark; a copy of the
same email (the sibling rule's copies, both directions) that carries one, or a
BuildingConnected package sibling (`sibling_of`) that does; and, for an email,
the tombstone check (5.3).

- Email sweep (`rfp_email_ingest._step_create`): after the link-only path and
  before the auto-create switch, a blocked row drains to `done` with
  `flag_reason = project_deleted` and the archive id, whatever the switch
  says. A `CreateBlocked` raised by the service later in the step lands the
  same way.
- Portal sweep (`rfp_portal_ingest._step_create`, NGEM and BuildingConnected):
  the same, before the portal's own `before_create` hook.
- The service (`create_from_email`, `create_from_portal`), which both the
  sweeps and the "Create project" buttons call: `_refuse_if_blocked` runs
  first (before the "already created" check, so the refusal names the
  deletion), stamps the row, and raises `CreateBlocked` (a `CreateRefused`,
  so the routers' existing 409 carries the sentence: "26.9.7120 NAME was made
  from this invitation and then deleted, so it will not be created again. An
  IT Admin can restore it from Deleted Projects.").
- `email_create_available` and `portal_create_available` are false for a
  marked row, so the button does not show.

Why nothing else can recreate it: a new email or sighting sharing the harvest
(Procore, PipelineSuite and SmartBid reuse one harvest per `(method, external_key)`, the
email harvester and NGEM likewise per their key) meets the harvest's mark; a
copy of the email meets the copy's mark; the link-only path finds
`rfp_harvests.project_id` null and falls through to the block; a person's
"Set project name", reopen or "Not a match" sends the row back through match
and harvest to `create`, where the block is checked again.

### 5.3 The tombstone check (`project_delete.tombstone_for_email`)

The matcher never sees a deleted project, so a later invitation for the same
bid with no shared harvest and no copy (another sender, a reminder a
different way) would otherwise walk straight to `create`. For an email row
with a project name, candidates are archives not restored, with
`project_bid_at` inside `RFP_MATCH_CANDIDATE_WINDOW_DAYS`, in the row's
test-bench scope, not in the row's `excluded_project_ids`. Each is scored with
the matcher's own `rfp_match.score_candidate` and must pass the deterministic
half of `rfp_match.is_confident` (the total at `RFP_MATCH_AUTO_THRESHOLD`, the
name floor, no discriminator conflict, a date score of 1.0 when the email has
a date). The LLM half is replaced by agreement a person can see: the email has
a bid date (which matched exactly), or the email's resolved GC is one of the
deleted project's GCs. A row a reviewer marked "Not a match"
(`match_review_decision = no_match`) is never inferred onto a deleted project.
A failed read of the archive never blocks.

### 5.4 What the screens show

`GET /rfp-emails/{id}` (and every action answer on that router) and the portal
invitation detail carry `deleted_project`: `{archive_id, project_id, number,
name, deleted_at, deleted_by {id, full_name}, reason, note, restored_at}` or
null. The email and portal list rows carry `create_blocked_archive_id`. The FE
shows "Project deleted by NAME on DATE: REASON" (with the note) where it showed
the created project, a "Project deleted" badge in the lists, and the label
"Project was deleted" for `flag_reason = project_deleted`.

### 5.5 Restore lifts it

After the links are back (so `rfp_harvests.project_id` and
`created_project_id` point at the project again): a row the block stopped at
the create step while the project was gone (`done` / `project_deleted` / this
archive) goes where it would have gone: `created` with the project when its
harvest belongs to the project again, else back to `match` (with attempts,
flag and error cleared), where the restored project is a candidate. Then every
mark naming this archive is cleared on all three tables.

## 6. API

All under the Bidding feature guard.

| route | roles | answer |
|---|---|---|
| `GET /projects/{id}/delete-info` | Estimating Admin, Executive, IT Admin | `{project_id, number, name, confirm_text, confirm_uses_number, from_rfp, blocked: null or {code, message}, counts: {rfqs, vendor_emails, quotes, files, gcs, other}, total_rows, reasons}`. `blocked.code` is `pm_enrolled`, `cp_enrolled` or `referenced`. 404 unknown project. |
| `POST /projects/{id}/delete` `{reason, note, confirm_name}` | same | 200 `{archive_id, project_id, number, name, from_rfp, reason, deleted_at}`. 422 name mismatch, bad reason, `other` without a note, the RFP reason on a non-RFP project; 409 PM / CP / referenced; 404. `X-Error-Code: project_delete_<code>`. Audited `project.delete` `{archive_id, reason, note, number, name, from_rfp, row_total}`. |
| `GET /deleted-projects?limit&before&before_id&reason&restored` | IT Admin | newest first, paged on `(deleted_at, id)`; `restored=false` (default), `true`, `all`; `reason` one code (400 otherwise). Items: `{id, project_id, project_number, project_name, gc_names, stage, reason, note, from_rfp, rfp_source {kind, id, label} or null, deleted_at, deleted_by, row_total, counts, restored_at, restored_by, restore_error}`. |
| `GET /deleted-projects/summary` | IT Admin | `{total, not_restored, restored, by_reason: {code: n}}` (by_reason counts every deletion, restored or not). |
| `POST /deleted-projects/{id}/restore` | IT Admin | 200 `{archive_id, project_id, number, name, restored_at, restored_by, rows_restored, links_relinked, links_skipped}`; 409 already restored, number taken, missing reference, conflict; 404. Audited `project.restore` (or `project.restore_failed`, with the sentence kept in `restore_error`). |

The creator-only `DELETE /projects/{id}` (discard a creation leftover, no
record kept) is unchanged.

## 7. Frontend

- `components/DeleteProjectModal.tsx`: loads delete-info, explains (the
  project disappears from every page, dashboard, report and reminder; nothing
  is emailed; an IT Admin can restore it from Deleted Projects; for an RFP
  project, the source will not create it again), lists what will be removed,
  asks for the reason (and the note for Other), and enables Delete only when
  the trimmed typed text equals `confirm_text` exactly. A blocked project
  shows the reason instead of the inputs.
- Project page: "Delete project" in the bottom action bar beside Abandon;
  after a delete, `router.replace` to `/rfp-created` (RFP project) or
  `/dashboard`.
- `/rfp-created`: Delete on every row (reason preselected), the row removed
  and the counts refreshed in place.
- `/deleted-projects`: IT Admin only (sidebar entry and page gate), reason and
  status filters, summary line, Load more, Restore with its own confirmation.
- `DeletedProjectNotice` in the RFP email and portal modals; badges in the
  lists.
- Help Center: the RFP ingestion topic's "Getting rid of a project that should
  not exist" section, and HELP_CENTER.md.

## 8. Storage

A delete never touches Storage: every object under `{project_id}/` stays, so
the restored `project_files` rows find their bytes. The only project-prefix
sweeper, `storage.delete_project_prefix` (the creator-only discard), now
refuses to sweep a project id that has an open (not restored) archive, and
treats a failed check as "held". Splitter objects (`bid-splits/{job}/`) are
separate copies; a splitter job whose `project_id` the delete nulled can be
deleted from the splitter page without affecting the project's files (the
restore then skips relinking that job).

## 9. A vanished project elsewhere

Audited 2026-09-29: no poller, sweep or queue worker crashes when a project is
deleted mid-flight (every loop catches per row; the queue treats a vanished
job row as a lost lease). Fixed as part of this work:

- `GET /projects/{id}` and every other `.single()` project read on a deleted
  project: PostgREST's PGRST116 is now a 404 (an explicit catch in
  `_fetch_project_with_outcome`, and a global handler in `app/main.py` for the
  rest; other PostgREST errors re-raise unchanged).
- Portal "Same as project X" on a stored candidate whose project was deleted:
  a sentence instead of a raw foreign key 500.
- BuildingConnected "Apply dates" racing a delete: 404 instead of 500.

Known, not fixed (narrow races, self-healing or out of scope):
`rfp_create.swap_project_gc` and `rfp_split.resync_project_files` inserting
into a project deleted a moment earlier (a 500 on that one request); an LLM
cost row for a job cascaded mid-call is dropped (`llm_gate`); a vendor reply
landing during the delete can leave an unreferenced object under the deleted
project's prefix (`rfq_inbox._store_quote_file`); the general-material
fallback path (queue off) can log a failed write.

## 10. Tests and live run

- `tests/test_project_delete.py` (54): route tables and gates, the mount, the
  pure rules, delete and delete-info (every database code mapped), the list,
  the summary, restore success and failure, the screens' ref, the tombstone
  rule, the email and portal create steps, `create_block_for`, the PGRST116
  handler.
- `tests/test_project_delete_rfp_create.py` (8): the service refuses a blocked
  email (both paths), a blocked row at `created`, a tombstone hit, a portal
  invitation and a harvest mate; a restored source and an unrelated email
  still create.
- Live on dev, 2026-09-29 (database functions called through the service and
  PostgREST with the backend's own client, and through SQL):
  - E2E-0099 (a sent bid with RFQs, quotes, vendor messages, a proposal): 166
    rows in 33 tables and 44 SET NULL links archived and deleted; restore put
    back 166 rows and relinked 44 links; per-table md5 fingerprints of every
    row (updated_at included) and of the links were identical before and
    after. The restore deferred 5 columns to break the cycles.
  - 26.9.7120 (RFP-created, 13 emails, a Procore-style harvest, 5 match
    records): 59 rows in 12 tables, 29 links; 9 emails and 1 harvest marked
    (the 4 emails still in review_match were not); the create path refused
    the source email, a reminder sharing the harvest and a fresh email with
    the same name and date (tombstone); the email detail showed the deleted
    project; a restore attempted while a probe project held the number was
    refused (409, kept in `restore_error`); after removing the probe the
    restore put back 59 rows, relinked 29 links, lifted 10 marks, and the
    fingerprints were identical. A second delete of the same project wrote a
    second archive row, and two rows stopped at `create` while it was deleted
    went to `created` (sharing the harvest) and `match` (not) on restore
    (that last cycle run inside a rolled-back transaction).
  - The preview on the largest dev bidding project (307 rows) took about
    250 ms.

## 11. Release

1. Apply `0141_project_delete_archive.sql` to staging, then to production,
   each only with the user's explicit approval (it creates a table, adds three
   columns, replaces `set_updated_at` and adds functions; idempotent).
   `NOTIFY pgrst, 'reload schema';` is at its end.
2. Deploy the backend and the frontend together (the FE calls the new routes;
   the BE reads the new columns in the RFP sweeps, so the backend must not
   ship before the migration).
3. No environment variables.
4. After release: an IT Admin opens Deleted Projects once to confirm the page
   loads; the first deletions can be checked with the SQL in section 3.

## 12. Limits

- Statement time: the delete and the restore run in one statement through
  PostgREST, under the `authenticator` role's 8 second statement timeout. Dev
  projects take well under a second; a very large project (thousands of
  notifications and files) should still fit, but was not measured on
  production data.
- A snapshot holds rows, not Storage bytes (they stay in place, section 8).
- A trigger added later to an archived table fires on restore (4.3).
- The tombstone check covers email rows; a portal invitation is blocked by its
  own mark, its harvest or its package sibling only.
