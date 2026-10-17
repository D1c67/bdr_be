# Merge an RFP-created project into a project we are already bidding

RFP ingestion sometimes creates a new project for an invitation that belongs
to a bid the team already has (the matcher missed it: a different name, a
date outside the window, an unresolved GC). Merge Projects fixes that by hand:
the RFP-created project's GCs, their bid dates and contacts, its RFP links and
any documents the other project does not already have move onto the project
the team is bidding, and the RFP-created project is then deleted, recoverably.

Status: BACKEND BUILT 2026-10-07, DEV ONLY. Migration
`0149_project_merge.sql` is written but NOT YET APPLIED: applying it to the
dev database (`bpidntbyvoooqvaispup`) was declined on 2026-10-07, so the
routes answer a database error on dev until it is applied there (dev only,
never staging or production without explicit approval). See the Build record
at the end.

Naming: migration `0149_project_merge.sql`; column `project_files.sha256`;
`deleted_projects` reason `merged` plus columns `merged_into_project_id`,
`merged_into_number`, `merged_into_name`, `merge_summary`; database function
`merge_rfp_project` (plus the internal archive variant in 4.2); service
`app/services/project_merge.py` (hashing helpers may live in
`app/services/file_hashes.py`); routes on `app/routers/projects.py`; audit
action `project.merge`; FE component `components/MergeProjectModal.tsx`; i18n
namespace `mergeProject`.

Words used below: **source** = the RFP-created project the user is on (the
"old project", it goes away). **target** = the active project the user picks
(the "new project", it stays).

---

## 1. Decisions

| Topic | Decision |
|---|---|
| Where | A "Merge Projects" button inside the Project Details modal (the `details` box from the project side menu), shown only when the project was created by RFP ingestion (`project.rfp_created` is set) and the viewer may merge. |
| Who | Estimating Admin, Executive, IT Admin (`PROJECT_DELETE_ROLES`, FE `canSeeRfpCreated`). |
| Pick | The modal searches active bidding projects (2.2); clicking one opens the "Are you sure?" step. |
| Confirm | The "Are you sure?" step shows what will move, then asks the user to type the old project name AND the new project name exactly (trimmed, case-sensitive; the project number when a project has no name: the delete rule `coalesce(nullif(btrim(name), ''), btrim(number))`). The database function checks both again under lock. |
| Old project afterwards | Deleted through the existing archive (docs/PROJECT_DELETE.md) with the new reason `merged` and "merged into" columns. It shows on Deleted Projects as "Merged into NUMBER NAME"; an IT Admin can restore it (it comes back WITHOUT what moved, 6). (User, 2026-10-07: "Delete it, restorable".) |
| Documents | Only documents the target does not already have move. "Already has" = the same SHA-256 of the bytes (user, 2026-10-07: sha256, not name and size). Skipped documents stay with the old project in the archive. |
| Direction | The RFP-created project merges INTO the picked project (user, 2026-10-07). |
| Silence | Like a delete: no stage event. One notification (7.3). |

## 2. Eligibility

### 2.1 Source (the project the button is on)

Refused, with a sentence the modal shows instead of the search:

| code | rule |
|---|---|
| `not_from_rfp` | no `rfp_created_projects` row |
| `pm_enrolled` / `cp_enrolled` | `pm_stage` set / `cp_enrolled_at` set (the archive refuses them anyway) |
| `work_started` | the source has any of: an `rfq_sends` row (through its rfqs), a `quotes` row, an `estimator_assignments` row, a `proposal_sends` row, an `external_submissions` row, a `bid_outcomes` row. The sentence lists which. That work cannot move cleanly; delete or bid it instead. |
| `files_arriving` | `rfp_created_projects.files_status` in (`pending`, `running`): documents are still being copied in |
| `split_running` | a `bid_split_jobs` row for the project that is still queued or running (use the splitter's own "active" predicate) |

An abandoned source may be merged (cleanup is still valid).

### 2.2 Target (what the search lists and what the merge accepts)

- not the source; exists;
- `abandoned_at` null; `current_stage` not in (`declined`, `pm_only`, `cp_only`) (check `workflow.STAGES` for any other closed stage); no `bid_outcomes` row (an outcome means the bid is over);
- the same test-bench scope as the source (`projects.test_session_id` equal, both null in normal use);
- may itself be RFP-created.

Refusal codes: `target_not_found`, `same_project`, `target_closed`, `target_scope`.

Search (`GET /projects/{id}/merge-targets?q=&limit=`): `q` trimmed, at most 100 characters, matched case-insensitively against number, name and the target's GC names (escape `%`, `_` and `,` the way the existing searches do, see `rfp_create._like_literal`). Empty `q`: up to 10 likely targets, ranked by `rfp_match.name_score` against the source's name (only those above a small floor, e.g. 0.35), so the right project is usually one click away. Non-empty `q`: up to `limit` (default 20, max 50) ordered by bid date nearest first. Each item: `{id, number, name, stage, bid_at, gc_names, rfp_created}`. `bid_at` follows the actual-bid confidentiality rule (`ACTUAL_BID_VIEWER_ROLES`; `internal_bid_at` for anyone else). If an existing project search (the RFP Matches drawer's) already does this, reuse it.

## 3. What moves, what stays

### 3.1 GCs

For every `project_gcs` row on the source:

- **GC not on the target**: the row MOVES (`update project_gcs set project_id = target`), keeping its id, so `project_gc_contacts`, `rfp_match_id` and every `rfp_project_matches.project_gc_id` stay valid. `needs_by` becomes `coalesce(needs_by, pacific_date(source.actual_bid_at))`: the source was made from that GC's invitation, so its bid date is that GC's due date.
- **GC already on the target**: the target's link wins. The source link's contacts are added to the target link (`on conflict do nothing`); the target link's `needs_by` is filled only when it is null. The source link stays with the source (archived).

Moved links land exactly like a GC an RFP merge adds today (`rfp_email_ingest._insert_link`): no extra side effects. Check the manual add path (`POST /projects/{id}/gcs`) and Calling In for anything a newly added GC needs (late-GC state is derived, so a GC added after Send Out shows as a late GC needing a proposal, which is correct).

### 3.2 RFP links (every RFP pointer at the source moves to the target)

- `rfp_harvests.project_id`, `rfp_emails.created_project_id`, `rfp_emails.match_project_id` (any status, including a `review_match` proposal), `rfp_emails.possible_rebid_project_id`, and the same three columns on `rfp_portal_invitations`: source -> target. A later email sharing the harvest then links to the target through the existing link-only path, and the email screens' "project" link opens the target.
- `rfp_project_matches` rows on the source move to the target (`project_id`). A row whose GC was ALREADY on the target (3.1, second case) becomes what the matcher would have recorded: `kind = 'duplicate'`, `gc_added = false`, `project_gc_id = null`; its email, if at `merged` with `match_project_id` = source, goes to `duplicate`. (A merged row with `gc_added` true and a null `project_gc_id` would read as "GC removed", so it must not be left that way.)
- No RFP block is written (5.1 of PROJECT_DELETE.md does not apply: nothing points at the source any more, and the bid lives on as the target). `project_delete.tombstone_for_email` must ignore archives with reason `merged` (the matcher finds the target itself).

### 3.3 Project email filing

`ingested_emails.project_id` and `ingested_emails.suggested_project_id`: source -> target. `email_conversation_projects` rows: source -> target, dropping a row the target already has (respect its unique key).

### 3.4 Documents

Candidates: every `project_files` row on the source. Hash every candidate and every target file that has no `sha256` yet (4.3). A candidate is skipped when its `sha256` equals the `sha256` of any target file; otherwise it moves.

A moved document:

1. Its object (and its preview derivative, when `preview_path` is set) is COPIED server-side (`storage.copy_file`) to a target path: `storage.build_object_path(target, category, filename)` and `{target}/previews/{file_id}.pdf`. Copy, not move: the source archive keeps its own bytes, and nothing on the target ever lives under the source's prefix (a later sweep of the source's prefix, `storage.delete_project_prefix`, must never take target files).
2. The database function then sets `project_id`, `storage_path`, `preview_path` in the same transaction as everything else. If the function fails, the copies are removed (best effort, logged).
3. It lands on the target in the state an internal upload to the target at its current stage would produce (read `routers/files.py`: after the initial files are locked by an estimator assignment, a new file is a revision or additional file with a note; follow that rule so the File Updates flow and the Plans & Specs log treat it as new). The note says where it came from: "Moved from NUMBER NAME (merged)". `sent_to_estimators_at` stays null.
4. `is_source_set` is cleared when the target already has a source set and any code assumes one per project (check `core/file_categories.py`, `routers/files.py`, `routers/estimator.py`); otherwise kept.

### 3.5 What stays with the source (archived with it)

Intake fields, notes and bid notes, BuildingConnected details, the `rfp_created_projects` row, stage events, the Go/No-Go decision, notifications, open events, LLM jobs, BOQ analyses, due reminder log, skipped documents, the source link of a GC already on the target, and **estimator notes** (they must never move: the target may have an assigned external estimator, and notes written on the source were written where no estimator could read them). `bid_split_jobs.project_id` is set null by the delete as today.

## 4. Database (migration 0149)

### 4.1 Schema

- `project_files.sha256 text` with `check (sha256 is null or sha256 ~ '^[0-9a-f]{64}$')` and an index `(project_id, sha256) where sha256 is not null`.
- `deleted_projects`: the reason CHECK gains `merged`; new columns `merged_into_project_id uuid references projects(id) on delete set null`, `merged_into_number text`, `merged_into_name text`, `merge_summary jsonb`; a CHECK that `reason = 'merged'` iff `merged_into_number is not null`.

### 4.2 The archive, reused

`archive_and_delete_project` (0141) keeps its public signature and behavior and keeps refusing `merged` (`bad_reason`): only the merge function may archive with that reason. Split it into an internal function that takes one more argument (`p_rfp_block boolean`) and a public wrapper, or add the reason path inside; either way the walk, the snapshot, the delete-order logic and the restore stay exactly as they are. **The authoritative current definitions are the dev database's** (`select pg_get_functiondef('archive_and_delete_project'::regproc)`): 0141 went to dev as six separate migrations, so the local file may differ. Diff before replacing anything.

### 4.3 `merge_rfp_project(p_source uuid, p_target uuid, p_actor uuid, p_confirm_source text, p_confirm_target text, p_files jsonb)`

`security definer`, `search_path` pinned to `pg_catalog, public, pg_temp`, execute revoked from `public`, `anon`, `authenticated`, granted to `service_role` (the 0141 pattern). Errors raise `project_merge:<code>` with details, like `project_delete:<code>`.

`p_files` = `[{file_id, storage_path, preview_path}]`: the documents to move with their NEW paths (the service already copied the objects).

One transaction:

1. Lock both projects `for update` in id order (no deadlock between two people merging the pair both ways). Refuse per 2.1 and 2.2 again.
2. Both typed names against the confirm text (`source_name_mismatch`, `target_name_mismatch`).
3. Every `p_files` id must be a source file and its `sha256` must not equal any target file's (`files_changed`: the target gained a file since the hashes were read; the service hashes again and retries once, then the modal shows the fresh preview).
4. 3.1, 3.2, 3.3, then the file updates of 3.4.
5. Archive and delete the source with reason `merged`, no RFP block, `confirm_name` = the source's confirm text; then fill `merged_into_*` and `merge_summary` on the archive row.
6. Return `{archive_id, source: {id, number, name}, target: {id, number, name}, gcs_moved: [{gc_id, name, needs_by}], gcs_already: [{gc_id, name}], files_moved, files_skipped, rfp_links_moved}`.

### 4.4 Hashing (service side)

`sha256` is filled lazily and cached on the row: stream the object through a short-lived signed URL with httpx in chunks (never load a 450 MB file into memory; `storage.download_file` returns bytes, so do not use it here), hash, write `sha256` where it is still null. The preview endpoint works within a time budget (about 20 seconds per call, checked between files) and reports progress; the modal calls it again until everything is hashed. Hashes persist, so every call resumes where the last stopped and the merge call is fast. A file whose object is missing gets no hash and is reported in the preview (`files_unreadable`); it moves (it cannot be compared) and the preview says so.

## 5. API

Bidding feature guard, `PROJECT_DELETE_ROLES`, the delete rate limit. Errors carry `X-Error-Code: project_merge_<code>`. Sync `def` handlers (event-loop rule).

| route | answer |
|---|---|
| `GET /projects/{id}/merge-targets?q=&limit=` | `{items: [...], blocked: null or {code, message}}` (2.2). `blocked` is the source's refusal (2.1), so the modal can explain it before the user searches. |
| `POST /projects/{id}/merge-preview` `{target_project_id}` | `{ready, hashed, total, source: {id, number, name, confirm_text}, target: {id, number, name, confirm_text, stage}, gcs_moving: [{gc_id, name, needs_by}], gcs_already: [{gc_id, name}], files_moving: [{id, filename, category}], files_skipped: [{id, filename, category, same_as_filename}], files_unreadable: [{id, filename}], rfp_links: n, blocked: null or {code, message}}`. File lists capped at 200 each, with totals. POST because it writes hashes. |
| `POST /projects/{id}/merge` `{target_project_id, confirm_source_name, confirm_target_name}` | 200 the 4.3 result. 422 name mismatch; 409 any refusal, and `files_not_ready` when hashing is still incomplete after the call's own budget (the modal goes back to the preview); 404 unknown project. |

The server decides the file list at merge time from the stored hashes; it never trusts a list from the client.

## 6. Restore

Restoring a `merged` archive works like any restore and brings back the source with what did NOT move (intake, notes, stage events, skipped documents, its `rfp_created_projects` row, the GC links that were already on the target). The moved GCs, documents and RFP links stay on the target. Deleted Projects shows that sentence on a merged row before the Restore confirmation.

## 7. Side effects

1. Audit `project.merge` on the TARGET (entity `project`, target id) with `{archive_id, source_project_id, source_number, source_name, gcs_moved, gcs_already, files_moved, files_skipped, rfp_links_moved}`; the archive writes its usual `project.delete` row (reason `merged`).
2. `GET /deleted-projects` items gain `merged_into: {project_id, number, name} or null`; the reason filter accepts `merged`; the summary's `by_reason` counts it.
3. One notification to the people who own the target's current stage, the same way the RFP merge notifies when it adds a GC to a project (read `rfp_email_ingest` for `NOTIFY_TYPE_MERGED` and reuse its audience and email mirror), with a project-merge message: "NAME merged NUMBER NAME into this project: GC added (due DATE)".

## 8. Frontend

- `components/MergeProjectModal.tsx`, opened from a "Merge Projects" button in the Project Details modal (bottom of the details box, secondary style, `components/ui`). Two steps in one modal:
  1. **Pick**: one line on what merging does; a search box (debounced about 300 ms, seq-guarded loads); likely matches before the user types; rows show number, name, stage, bid date, GC names. A `blocked` source shows its sentence and no search.
  2. **Are you sure?**: calls merge-preview, showing "Comparing documents: N of M" and calling again until `ready`; then lists GCs moving (with due date), GCs already on the target, documents moving and skipped (collapsible lists), RFP links; explains that the old project is deleted and an IT Admin can restore it from Deleted Projects. Two inputs: "Type the old project name" with the expected text shown, "Type the new project name" likewise. "Merge projects" is enabled only when both trimmed values equal the confirm texts exactly. Back returns to Pick.
- On success: `router.replace('/projects/{target}')` and a success message naming both projects.
- `/deleted-projects`: a merged row shows "Merged into NUMBER NAME" (a link to the target when it still exists), the restore confirmation adds the 6 sentence, and the reason filter offers Merged.
- Help Center: a "Merging a project into one you're already bidding" section in the RFP Ingestion topic, and HELP_CENTER.md.
- i18n: namespace `mergeProject` plus the new deleted-projects keys, in all 6 catalogs (follow what the `deleteProject` namespace did).
- The squared-off neumorphic system, no glass (`components/ui`). No em dashes in any string.

## 9. Tests and checks

- Unit: eligibility (2.1, 2.2), the confirm texts, file selection by hash, the `merged` reason refused on the public delete.
- Database, on dev with throwaway projects: a source with two GCs (one already on the target), three files (one identical to a target file), an RFP email at `created`, a second email merged into the source, an ingested email. After the merge: links moved with ids kept, contacts unioned, needs_by filled, the duplicate rule applied, files copied and repointed, the identical file left with the archive, every RFP pointer on the target, the archive row reason `merged` with `merged_into_*`, no RFP block. Then restore the archive and check 6.
- Lock-order: two concurrent merges of the same pair do not deadlock; the second gets a clean refusal.

---

## 10. Build record (backend, 2026-10-07)

### What was built

- Migration `supabase/migrations/0149_project_merge.sql`: `project_files.sha256`
  (CHECK, index), the `merged` reason and `merged_into_*` / `merge_summary`
  on `deleted_projects` (with the CHECK that `merged` goes with
  `merged_into_number`), the internal archive `project_archive_and_delete`,
  the public `archive_and_delete_project` as a thin wrapper, the helper
  `project_merge_file_refs`, and `merge_rfp_project`. The archive body is the
  dev database's definition (checked against the local 0141 file by md5 of
  every function body on 2026-10-07: all identical except
  `restore_deleted_project`, which is not touched).
- `app/services/file_hashes.py` (streamed SHA-256 through a 10-minute signed
  URL, 1 MB chunks, a budget checked between files, the first file always
  attempted so every call makes progress, a conditional write of the hash).
- `app/services/project_merge.py` (eligibility, search, preview, merge, the
  side effects) and the three routes on `app/routers/projects.py`
  (`ProjectMergePreviewIn`, `ProjectMergeIn` in `app/models/schemas.py`).
- `project_delete`: `REASON_MERGED`, `LIST_REASONS` (the Deleted Projects
  filter and `by_reason`), `merged_into` on list items, the tombstone check
  skips `merged` archives. `REASONS` (what a person may pick) is unchanged,
  so `POST /projects/{id}/delete` still refuses `merged` (and so does the
  database).
- `notification_email._TYPE_META`: `project_merged`.
- Tests: `tests/test_project_merge.py` (85); `tests/test_project_delete.py`
  updated for `by_reason.merged`.

### Deviations from the design, and why

1. The internal archive takes the merge TARGET (`p_merged_into uuid`), not
   `p_rfp_block boolean`: the archive row's CHECK needs `merged_into_*` at
   insert. Null = the old function exactly (four reasons, RFP block written);
   set = reason `merged`, `merged_into_*` filled, no RFP block. It is revoked
   from the service role too (stronger than 0141's helpers), so only the two
   security-definer entry points reach it.
2. `p_files` entries also carry `category`, `doc_type` and `note`: the
   landing state (3.4) is decided by the backend from `routers/files.py`'s
   `handoff_locked`, the one definition of the hand-off lock. The database
   validates the category against the enum and the doc_type CHECK applies.
3. Documents another row points at (any foreign key into `project_files`,
   read from `pg_constraint` by `project_merge_file_refs`: an RFQ's split
   file, a quote's file, a BOQ analysis, a proposal), and the work
   categories `quote`, `rfq_split`, `proposal`, stay with the old project:
   moving them would leave its restored rows pointing at the target's file.
   The preview lists them in `files_skipped` with `reason: "in_use"` and
   `same_as_filename: null`; true duplicates carry `reason: "duplicate"`.
   The database refuses such a file (`files_changed`).
4. Hashing is limited to what can match: equal bytes have equal sizes, so a
   document whose recorded size no file on the other side has is never
   hashed (a row without a size, and everything facing it, is). `hashed` /
   `total` count only the rows the comparison needs.
5. `files_arriving` and `split_running` are checked by the service right
   before the database call, not again under the lock: they follow the
   backend's lease and heartbeat rules (`rfp_create_files.claim_is_stale`,
   `rfp_split.split_running`). A promotion or split insert racing the merge
   waits on the project row lock and then fails its foreign key. Every other
   2.1 and 2.2 rule is checked again in the database.
6. A target in Project Management (`pm_stage` set) is `target_closed` too.
7. Statuses: `target_not_found` and `same_project` are 409 (refusals); 404
   is only an unknown source. The second of two simultaneous merges of the
   pair answers `project_merge_gone` (409, "This project no longer exists:
   it may have just been merged or deleted."). `copy_failed` (409) when the
   storage copy fails (nothing merged, copies removed). The archive's
   `project_delete:referenced` passes through as `project_merge_referenced`.
8. The bell (7.3): type `project_merged`, one row per active user of each
   internal owner role of the target's current stage (`workflow.STAGES`,
   never the estimator; Estimating Admin when a stage names none), bell only
   (`mirror_email=False`), like the matcher's `rfp_match.merged`, which is
   bell only. The message names the GCs: "Tom Moore merged 26.10.7301 NAME
   into this project: GC added: Acme Builders (due 10/20/2026)", then "N
   documents added"; "GCs added: A (due ...), B" for several.
9. Moved links land like `_insert_link`: their per-GC pricing columns
   (`proposal_*` amounts, `pricing_*`) are cleared; the values they had are
   kept in `merge_summary.before.project_gcs`. A source link that stays (its
   GC was already on the target) loses its `rfp_match_id` (the match row
   moved), so a restore brings it back as a plain link. A portal
   invitation whose `project_gc_id` named such a link gets null (it no longer
   owns a link). An open match row converted to a duplicate keeps its
   previous state in `merge_summary.before.rfp_project_matches`; a closed
   (unmerged) one only moves.
10. `email_conversation_projects` is unique on `(mailbox, conversation_id)`
    across all projects, so a source row can never collide with a target
    row: every one moves, none is dropped.
11. Moved documents keep their provenance (`rfp_harvest_id`,
    `rfp_sandbox_file_id`, `bid_split_file_id`, `bid_split_segment_id`),
    except a sandbox or segment id the target already holds (its unique
    indexes), which is cleared. `is_source_set` is kept: no code assumes one
    source set per project (checked `core/file_categories.py`,
    `routers/files.py`, `routers/estimator.py`, `services/rfp_split.py`).
    `estimator_deliverable` false, `submission_round` and
    `sent_to_estimators_at` null, addendum fields kept only on an addendum.
    The note is "Moved from NUMBER NAME (merged)", then ". " and the note the
    file had. A document whose object is missing moves without bytes (the new
    path has no object, as the old one had none); a missing preview
    derivative leaves `preview_path` null and `preview_status` `none`. The
    original objects stay under the old project's prefix (copy, not move).
    After the merge the BuildingConnected files flag is cleared when a moved
    document lands as a drawing or specification, as an upload would.
12. Likely targets (empty `q`) are drawn from open projects bid inside
    `RFP_MATCH_PRECREATE_WINDOW_DAYS` (60) back (the actual bid date read only
    for the actual-bid roles); a typed search has no date window. `q` is
    accepted up to 400 characters at the API, then trimmed and cut to 100.
13. Response details for the frontend: the preview adds
    `files_moving_total`, `files_skipped_total`, `files_unreadable_total` and
    `files_skipped[].reason`; a blocked preview may carry `target: null` (the
    target is the source, or unknown); the merge answer has exactly the 4.3
    keys. Every refusal sentence is in `detail`.
14. The source's `project.delete` audit row (reason `merged`, with
    `merged_into`) is written by the service, as `delete_project` does.

### Checks

- Unit: `uv run pytest tests/test_project_merge.py` (85 passed), the delete
  suites (63 passed with the `by_reason` change), the whole backend suite
  (6634 passed, 1 skipped). The internal archive's service-role revoke is a
  statement of its own so the 0145 static check (`from anon,
  authenticated;`) still reads the migration.
- Dev database: NOT RUN. `apply_migration` of 0149 to `bpidntbyvoooqvaispup`
  was declined, so no throwaway projects were created on dev and nothing on
  dev's database changed (its `deleted_projects_reason_check` still lists
  four reasons).
- Local replica instead (a throwaway Postgres 17 container from the same
  Supabase image, the dev catalog of the 29 tables involved recreated with
  their columns, keys, checks and unique indexes, then 0141, dev's
  `restore_deleted_project` and 0149; 0149 also re-applied to check it is
  idempotent). The section 9 scenario passed: refusals (both names,
  `files_changed` for a file the target has and for a file in use, bad
  paths and categories, `same_project`, `not_from_rfp`, `work_started`,
  `target_closed`), the service role refused on the internal archive, the
  public delete refusing `merged`; then the merge (as the service role): the
  moving link kept its id and contact, got its due date from the source's
  bid date on the Pacific calendar and lost its pricing; the shared GC's
  target link gained the source contact and its due date; the merged email's
  match row became a duplicate with no link and the email went to
  `duplicate`; the created email, the review email, the harvest and the
  invitation pointed at the target (the invitation's link cleared); the
  ingested email and the conversation moved; two documents moved with their
  revision state, notes and preview; the identical one stayed in the archive;
  the archive row had reason `merged`, `merged_into_*`, `merge_summary`, no
  RFP block anywhere, 6 rows and 1 SET NULL link (the split job). Restore
  brought back 6 rows, relinked the split job, the shared GC's source link
  (as a plain link) and the identical document; the moved GC, documents,
  match row and emails stayed on the target. The plain delete still wrote
  its RFP block, and a `merged` row without `merged_into_number` is refused
  by the CHECK.
- Lock order, locally: session A merged the pair and held its transaction 3
  seconds; session B (same pair) waited about 2 seconds and got
  `project_merge:not_found` (the service's `project_merge_gone`), session C
  (the reverse direction) got `project_merge:target_not_found`; no deadlock,
  one archive.
- Dev Storage (no database needed): two throwaway objects under a `zz-merge-
  test-` prefix of `project-files`, one 3 MB: the streamed hashes equal the
  local SHA-256, a server-side copy hashes the same, a missing object reads
  as missing on signing (`ObjectMissing`) and on copy (404 `not_found`); all
  objects removed (0 left under the prefix).

### Still to do

1. Apply `0149_project_merge.sql` to dev (`bpidntbyvoooqvaispup`) once
   approved, then run the section 9 live check there with `ZZ MERGE TEST`
   projects through the service (scripts in the session scratchpad mirror the
   local scenario).
2. Staging and production only at release, with explicit approval.
