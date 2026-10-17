# RFP Ingestion: missing files on a created project

Design record, 2026-10-09. Status at the bottom (section 13).

When the harvest cannot bring every bid document into a project made from an
RFP invitation, the project is still created (docs/RFP_CREATE.md, docs/RFP_SPLIT.md
section 10) and today the only trace is a "Missing files" badge on the Created
from RFP Ingestion page. A person then has to go find the email or the portal
to work out what is missing. This slice puts the whole story on the project
itself: every file or link that did not come in, why, a clickable link where one
exists, a drop zone that runs what the person fetched by hand through the Bid
File Splitter, and a flag that clears item by item.

The user's words (2026-10-09): "when the app fails to download [files] from the
email in the RFP ingestion, we need to give the users a link to click to get
the files (so they can do it from inside the app and they don't have to go find
the email". And: "I also want the user to be prompted to drop the files that
they manually downloaded into the project and have them run through the
splitter. But we need to let the user know if the files were not downloaded
because they failed the sandbox." And: "any failure, but tell the user what
happened. ex. if only one file failed tell the user which file failed and why.
this will help them not download the entire folder."

---

## 1. Decisions locked in (the user, 2026-10-09)

| Topic | Decision |
|---|---|
| What is offered | The GC's share links (SharePoint, OneDrive, Dropbox, Box, Google Drive, ShareFile, Egnyte, WeTransfer, Hightail) as clickable links on the project page, for EVERY failed outcome, not only the sign-in wall and unsupported host the harvest card shows today. No in-app attachment download, no Outlook deep link. |
| Failed attachments (no link exists) | Name the file and the email it came from (sender, subject, received date, receiving mailbox) so a person can find it in Outlook. The user approved this default. |
| Trigger | Any failure, total or partial. Each failed file or link is listed by name with its reason, so one failed file out of a folder of fourteen does not send a person back for the whole folder. |
| Two groups, two messages | "Could not be downloaded" (fetch it by hand, link offered) and "Downloaded but refused" (the Ingestion Sandbox or the promotion refused it: downloading again will not help, fix the file and drop it in). |
| Platform invitations | Procore, SmartBid, PipelineSuite and NGEM harvest failures get the same callout with the bid sheet or portal link (`rfp_harvests.external_url`). BuildingConnected keeps its own "Files needed" flag (docs/RFP_BUILDINGCONNECTED.md 3.8): it never harvests. |
| Surfaces | A callout on the project page (below the split callout), a badge and a "Get files" action on the Created from RFP Ingestion row, and one sentence in the creation bell and its email. |
| Drop zone | In the callout. PDFs go through the Bid File Splitter on a job linked to the project; the segments are filed into the project documents by trade exactly as the RFP split does. Word and Excel files skip the splitter. If the splitter is off, the model is away, the file is over the page cap, or the hand-off package has sent, the file lands whole. |
| Clearing | Per item. A known file resolves when a file of the same name lands in the project by any path, or when a person marks it handled; a named file NEVER clears any other way (review 2026-10-09: one drop must not wipe the list a person is working from). A whole link or a whole-harvest failure resolves only when a person marks it handled, except that a TOTAL failure (nothing landed at all) also clears those unknown-content items when the first drawing or specification is added BY A PERSON (an upload, a drop, a draft transfer, a merge; never the promotion's own inserts). "Dismiss all" with who and when and an Undo; items a later sync adds reopen the issue. |
| Who | Everyone who can open the project sees the callout. The actions (mark handled, reopen, dismiss, undo, drop) are for project writers: `WRITER_ROLES` on an assigned project, the same gate as a file upload; the estimator role never sees it (the estimator portal does not render the project page). |
| Images | Not items. The harvester skips every image by policy (RFP_HARVEST.md 2.5) and the harvest card already lists the non-signature ones by name; nothing changes there. |

---

## 2. Vocabulary

- **Item**: one thing that did not make it into the project. Three kinds:
  - `file`: a named file (an attachment, a zip member, a file listed inside a
    share folder, a platform document) that was not downloaded or was refused.
  - `link`: a whole share link the harvester could not list (sign-in wall,
    unsupported host, unreachable, an HTML page instead of a file, over the
    file cap, past the link cap), so the files behind it are unknown.
  - `harvest`: the whole harvest failed (`rfp_harvests.status = failed`): the
    platform refused the bid, the message is no longer in any mailbox, the caps
    were exceeded before anything was downloaded, and so on.
- **Group**: `not_downloaded` (nothing reached the sandbox: fetch it by hand) or
  `refused` (it reached the sandbox or the promotion and was turned away:
  downloading it again will not help).
- **Issue**: the set of items on one project plus the dismissal and the latest
  drop job. The callout renders the issue.
- **Total failure**: the harvest brought in no accepted or reused file at all
  (or there is no harvest row). Drives the first-file auto-clear.

---

## 3. Where the items come from (pure builders in `app/services/rfp_file_issues.py`)

`items_from_harvest(harvest: dict | None, email: dict | None, skipped: list[dict]) -> list[ItemSpec]`
is pure and deterministic; the sync (section 4) diffs its answer against the
table. `email` is the `rfp_emails` row of an email source (null for a portal
source); `skipped` is `rfp_created_projects.files_skipped`.

| Source | Kind | Group | status (token) | name | url | reason |
|---|---|---|---|---|---|---|
| `harvest.files[]` entry with status `download_failed`, `too_large`, `skipped_cap` | file | not_downloaded | the status | `file_path` (the full path; the FE shows the basename and the path as a hint) | the parent link's `data.links[].url` when `link_key` names one and the URL is https; for a platform file (no `origin`) the harvest's `external_url` (the bid sheet or portal page, review finding 3); null for an attachment | `error` |
| `harvest.files[]` entry with status `rejected` | file | refused | `rejected` | `file_path` | same rule (link url, platform bid sheet url, or null) | `error` (the sandbox's sentence) |
| `harvest.data.links[]` with status in `needs_sign_in`, `unsupported`, `unreachable`, `html_page`, `too_many_files`, `skipped_cap` | link | not_downloaded | the status | `label` when present, else the provider's display name | `url` when https | `error` |
| `harvest.status = failed` | harvest | not_downloaded | `harvest_failed` | null | `harvest.external_url` when https (the bid sheet or portal page) | `harvest.last_error` |
| `files_skipped[]` entry (promotion, RFP_CREATE.md 5) | file | refused | `skipped:` + the entry's `reason` token (e.g. `skipped:unscannable:encrypted`, `skipped:too_large`, `skipped:hazard:javascript`) | `file_path` | the parent link's URL when the harvest entry with the same `file_path` carries a `link_key`, else null | null (the FE labels the token) |

Entries with status `accepted`, `reused`, `expanded`, `skipped` (the harvester's
own policy skips: images and attached emails) and `null` are never items. A
`links[]` row with status `listed` is never an item (its failed files are
their own items). A harvest with no `files` and no `links` and a status other
than `failed` yields nothing: the existing "Missing files" badge still covers
"nothing was harvested" (section 8).

`email_ref` (stored on the item, shown under attachment items and under an
email-method `harvest` item): `{from: from_name or from_address, from_address,
subject, received_at, mailbox: primary_mailbox}` when the item's origin is
`attachment` or `zip` with a null `link_key`, or the kind is `harvest` and the
harvest method is one of `EMAIL_METHODS` (RFP_HARVEST.md 2.5); null otherwise.
The email is the HARVEST's own (`rfp_harvests.rfp_email_id`), not the created
record's: on the recent-project link path (RFP_CREATE.md 4.6) invitation B's
harvest can be promoted into a project whose record names email A, and B's
failed attachments belong to B's message (review finding 6).

`origin` on the item: the entry's `origin` for the email harvester
(`attachment` | `zip` | `link`), `platform` for a Procore / PipelineSuite /
SmartBid / NGEM file (entries without `origin`), `link` for a `link` item,
`harvest` for a `harvest` item, `promotion` for a `files_skipped` item.
`provider`: the entry's or link's `provider` (`graph` for an attachment), the
harvest `method` for platform items.

The stable identity of an item (the sync's diff key): `(kind, origin, status,
name)` for files and harvests, `(kind, link_key)` for links. The key is stored
in `item_key` so a second sync after "Harvest again" or "Retry documents" adds
only new items and never duplicates or reopens a resolved one.

`total_failure(harvest) -> bool`: no entry with status `accepted` or `reused`,
or no harvest at all.

---

## 4. Sync, resolution, dismissal (`app/services/rfp_file_issues.py`)

All sync Supabase, plain `def`, called from plain `def` routes, the queue
worker, or `run_in_threadpool` (docs/MEMORY: the event-loop rule).

`sync_project(sb, project_id, *, harvest=None, record=None, email=None) -> int`
reads what it was not handed (the `rfp_created_projects` row, its harvest,
the email row), builds the item specs, inserts the ones whose `item_key` is not
on the table yet, and returns how many it inserted. It never deletes, never
reopens, and swallows and logs every failure (best effort: the project exists
either way). It also stamps `rfp_created_projects.files_issue_total` the first
time it runs (never flips it later: the first-file rule reads what the
creation saw). Call sites:

1. `rfp_create` right after `_notify` (section 9 computes the sentence from the
   same specs, so `creation_note` is called before the insert and the sync
   after; both read the harvest the creation step already holds).
2. `rfp_create_files` when the promotion job reaches `complete` or `failed`
   (after `files_skipped` is written), and again after "Retry documents".
3. `rfp_harvest._complete_harvest` and `mark_from_queue` when the harvest row
   carries a `project_id` ("Harvest again" on a harvest that already made a
   project): new failures become items; a file that now landed is resolved by
   the promotion's name-match hook, never by the sync.

`after_file_insert(sb, project_id, *, category, filenames, actor_id) -> list[str]`
is the one hook every `project_files` insert path calls, beside the existing
`files_needed.clear_if_satisfied` call (routers/files.upload_file,
rfp_create_files._promote_one, rfp_split._insert_row, routers/bid_drafts
transfer, project_merge's payload loop if it inserts rows). Best effort, never
raises. It:

- resolves every open `file` item whose normalised name matches any of
  `filenames` (`how = name_match`). `filenames` carries the stored filename
  and, for a splitter segment, the SOURCE PDF's filename
  (`bid_split_files.filename`), because a segment is named after its trade,
  not after the file the person fetched;
- when `files_issue_total` is true, the insert was made BY A PERSON
  (`by_person=True`: routers/files.upload_file, the drop route, the bid
  draft transfer, project_merge; the promotion job and the RFP split's own
  re-file pass `by_person=False`), `category` is in
  `files_needed.CLEAR_CATEGORIES` and the project had no row in those
  categories before this insert (the check `files_needed.project_has_files`
  made BEFORE the insert by the caller, passed as `first_file=True`), resolves
  every open `link` and `harvest` item (`how = auto_first_file`). Named
  `file` items are never auto-resolved by this rule (review finding 2);
- returns the resolved item ids (the drop route reports them).

Name normalisation (`normalise_name`, pure): NFC, casefold, take the last
segment after `/` or `\`, collapse whitespace, strip surrounding whitespace.
Two names match when the normalised basenames are equal, or, failing that,
when BOTH names carry a recognised extension (a final `.` followed by 1 to 5
letters or digits) and their stems (the basename without that extension) are
equal. A name without an extension matches only exactly (review finding 14:
`E-1.1` must not match `E-1.pdf` or `E-1.2`). Tests cover `A-101.PDF` vs
`a-101.pdf`, `Set.zip/Plans/A-101.pdf` vs `A-101.pdf`, `E-1.pdf` vs
`E-1.PDF`, `E-1.pdf` vs `E-1.dwg` (match), and that `E-1.pdf` never matches
`E-10.pdf` and `E-1.1` never matches `E-1.pdf`.

`resolve(sb, project_id, item_id, actor_id) -> dict | None` (manual; 404 when
the item is not this project's or is already resolved), `reopen(sb,
project_id, item_id, actor_id) -> dict | None` (any resolved item; clears the
three resolution columns), `dismiss_all(sb, project_id, actor_id) -> dict |
None` (stamps `files_issue_dismissed_at/_by` on the record and resolves every
open item with `how = dismiss_all`; 409 when nothing is open), `undismiss(sb,
project_id, actor_id)` (clears the two columns and reopens exactly the items
whose `how = dismiss_all`). A sync that adds items after a dismissal leaves
the stamp alone: the issue's `state` is `open` whenever any item is open
(review finding 12), and a second "Dismiss all" re-stamps and resolves the
new ones (the earlier `dismiss_all` rows stay resolved). Every one audits
(`rfp_files.resolve`, `rfp_files.reopen`, `rfp_files.dismiss`,
`rfp_files.undismiss`, entity `project`).

`issue_for_project(sb, project_id, *, record=None, names=None) -> dict | None`
builds the wire shape of section 7 (null when the project has no items at
all). `names` is the profiles map the caller already holds (the list route
batches one read); without it the detail route reads the few it needs.

`creation_note(specs) -> str | None` (section 9).

---

## 5. The drop zone (`app/services/rfp_file_drop.py`, routes in section 7)

One file per request, like every upload surface (the 460 MB request-body cap
is per request, the 450 MB file cap per file). The browser sends the files of
one drop in sequence, passing the `job_id` the first PDF's answer returned so
the batch shares one splitter job.

Routing, decided per file in `route_for(filename, content_type, magic, pages,
settings, *, splitter_available, package_sent) -> Route` (pure). The browser
threads the `job_id` from the LATEST answer that carried one, not the first,
so a batch whose first job filled or expired follows the server onto the
next job (review finding 9):

| File | Where it goes |
|---|---|
| PDF (extension AND magic bytes), the splitter on (`BID_FILE_SPLITTER_ENABLED`, `llm.is_configured("bid_split")`), the model not away (`rfp_split.model_away` is None), at or under `bid_split_max_pages_per_file`, and the hand-off package not sent | `splitter`: a `bid_split_files` row on the project's drop job (below), dispatched like `routers/bid_splitter.upload_job_file` does |
| PDF otherwise | `whole`: a `project_files` row with category `specification` when the name says so, else `drawing`; note "Added whole from the missing-files drop zone: {why}" where `why` names the splitter being off, the model being away, the page count over the cap, or the package having sent |
| `.doc`, `.docx`, `.xls`, `.xlsx` (extension and the office magic the upload route already checks) | `whole`: category `specification` when the name says so, else `other`; note "Added from the missing-files drop zone. Check the category." |
| Anything else | 415 with `{name}: the drop zone accepts PDF, Word and Excel files` |

"The name says so": the pure `looks_like_spec(name)` matches, case-insensitive,
`spec`, `specification`, `project manual`, `division` or a leading
`\d{2} \d{2} \d{2}` section number. Tested.

The drop job: `bid_split_jobs` with `source = 'project'` (new; migration
0151 widens the check), `project_id`, `created_by = the person`,
`rfp_harvest_id = null`, status `processing`, `file_count` re-stamped per
upload exactly as `upload_job_file` does. A job opened by this request whose
first file fails to stage is deleted again before the error is answered,
and `issue.drop` ignores a job with no files, so no empty job ever reads
`processing` forever (review finding 5). PDF validation is
`bid_split.check_upload` itself, never a copy (review finding 16). One job per drop batch: the route
takes an optional `job_id` form field and appends to it when the job is this
project's, source `project`, and still `processing` or ended within the last
10 minutes; otherwise it opens a new one. The per-file validation is the
splitter's own (extension and magic, must open, page cap, per-job file cap),
reused, not copied: factor `routers/bid_splitter.upload_job_file`'s body into
`services/bid_split.stage_upload(sb, job, filename, content, pages, user_id)`
+ `dispatch` so both routes share it.

Filing the segments: `rfp_split.resync_after_run` already fires for any job
with a `project_id` whose package has not sent, and `promote_split_file`
handles a file with no `rfp_sandbox_file_id` provided `fetch_original()`
answers the bytes. A dropped PDF is never lost (review finding 1): when a
`project` job's file ends `failed`, when its package has sent by the time
the run ends, or when the re-file raises, `rfp_split.file_project_job_whole`
files the stored source PDF whole into the project (idempotent on
`bid_split_file_id`; category by `looks_like_spec`, `additional` once the
package has sent; note "The Bid File Splitter could not split this file
({reason}); it was added whole." or "...the package had already been sent
when the split finished, so it was added whole under Additional files."),
so the item resolved at drop time always points at a document. For a `project` job `fetch_original` streams the job's
stored source PDF (`bid_split_files.storage_path`) and answers
`(Promote(category by looks_like_spec, source_format pdf), bytes)`; the
source-set rule (the untouched PDF kept once as `other` / `is_source_set`)
applies unchanged. `harvest_id` is null on every row it inserts;
`bid_split_file_id` / `bid_split_segment_id` carry the provenance so a
correction in the splitter re-files the project exactly as for RFP jobs.
Every place that keys on `source == SOURCE_RFP` AND a project
(`routers/bid_splitter.delete_job`'s refusal, the correction lock
`_project_guard`, `_resync`) treats `SOURCE_PROJECT` the same; the splitter
page's job list labels the source "Project" (FE).

A whole file goes through the same validation and insert the upload route
uses (`routers/files.upload_file` body, factored or called), as the person
(`uploaded_by`), so the file-add hooks fire (files_needed, this slice's
`after_file_insert`).

The route resolves items BEFORE anything is stored: `after_file_insert`'s
name matching is what the insert paths run, but the drop route also calls
`rfp_file_issues.resolve_by_names(sb, project_id, [filename], actor_id)` up
front so a PDF headed for the splitter (whose segments land minutes later
under trade names) resolves its item at drop time. The response carries the
resolved ids.

Audit `rfp_files.drop` with `{route, filename, job_id, project_file_id,
resolved}`.

---

## 6. Data model (migration `0151_rfp_file_issues.sql`, dev only until release)

```
rfp_file_issues
  id             uuid pk default gen_random_uuid()
  project_id     uuid not null references projects(id) on delete cascade
  harvest_id     uuid references rfp_harvests(id) on delete set null
  item_key       text not null                      -- section 3 identity
  kind           text not null check in ('file', 'link', 'harvest')
  "group"        text not null check in ('not_downloaded', 'refused')
  origin         text                               -- attachment | zip | link | platform | harvest | promotion
  provider       text
  status         text not null                      -- the token (section 3)
  name           text                               -- file path, link label, or null
  url            text                               -- https only, else null
  reason         text                               -- the sentence, or null
  email_ref      jsonb                              -- {from, from_address, subject, received_at, mailbox} or null
  resolved_at    timestamptz
  resolved_by    uuid references profiles(id) on delete set null
  resolved_how   text check (null or in ('name_match', 'manual', 'auto_first_file', 'dismiss_all'))
  created_at, updated_at timestamptz not null default now()
  unique (project_id, item_key)
  index (project_id) where resolved_at is null
  RLS enabled + forced, no policies; set_updated_at trigger

rfp_created_projects
  + files_issue_total         boolean not null default false
  + files_issue_dismissed_at  timestamptz
  + files_issue_dismissed_by  uuid references profiles(id) on delete set null

bid_split_jobs.source check gains 'project' (drop and re-add the 0132 constraint)

notify pgrst, 'reload schema'
```

Idempotent DDL (`create table if not exists`, `add column if not exists`,
constraints dropped and re-added by name). Applied to the dev project
(bpidntbyvoooqvaispup) by the orchestrator on 2026-10-09; staging and prod at
release with approval (docs/MEMORY never-touch-prod rule).

---

## 7. API (`app/routers/rfp_files.py`, prefix `/projects/{project_id}/rfp-files`)

Every route depends on `require_project_assignment` (the project's own gate);
the mutating ones also require `user.role in WRITER_ROLES` (403 otherwise).
Rate limit: the project routes' limiter. Literal paths before parameters.

| Route | Answer |
|---|---|
| `GET /issue` | the issue, or `null` |
| `POST /items/{item_id}/resolve` | `{item, issue}`; 404 when not this project's or already resolved |
| `DELETE /items/{item_id}/resolve` | reopen; `{item, issue}`; 404 when not resolved |
| `POST /dismiss` | `{issue}`; 409 "Nothing is open to dismiss." (a second dismiss after new items arrived re-stamps and resolves only the new open items; review finding 12) |
| `DELETE /dismiss` | `{issue}`; 409 when not dismissed |
| `POST /drop` (multipart `file`, optional `job_id`) | `{route: "splitter" \| "whole", job_id, split_file_id, project_file_id, category, resolved: [item ids], issue}`; 415 / 413 / 409 (`rfp_split.MSG_PACKAGE_SENT` only refuses the splitter route, never the whole route) |

The issue (`RfpFilesIssue`):

```
{
  "state": "open" | "dismissed" | "resolved",
  "total_failure": bool,
  "counts": {"open": n, "resolved": n, "not_downloaded": n, "refused": n},   // open items by group in the last two
  "items": [{
    "id", "kind": "file" | "link" | "harvest", "group": "not_downloaded" | "refused",
    "origin", "provider", "status", "name", "url", "reason",
    "email": {"from", "from_address", "subject", "received_at", "mailbox"} | null,
    "resolved": {"at", "by": {"id", "name"} | null, "how"} | null
  }],                                                     // open first (by group, then name), then resolved
  "dismissed": {"at", "by": {"id", "name"} | null} | null,
  "drop": {"job_id", "status", "files_total", "files_done", "files_failed", "failed": [{"filename", "error"}], "created_at"} | null,
  "splitter_available": bool,     // on, configured, model not away
  "splitter_unavailable_reason": str | null,
  "package_sent": bool,
  "page_cap": int
}
```

`state`: `open` when any item is open; else `dismissed` when
`files_issue_dismissed_at` is set; else `resolved`. `total_failure` is the
record's stamp AND `files_promoted == 0` live, so a re-harvest that brought
files in stops the "none came in" headline (review finding 4). `drop` is the
newest `bid_split_jobs` row with `source = 'project'` for this project that
has at least one file (null when none), with its files' statuses aggregated
and `failed[]` carrying each failed file's name and error; the FE polls
while it is `processing`. The list route's `flags.files_issue` read pages
its item select in 1000-row chunks (review finding 11).

Elsewhere:

- `GET /projects/{id}` (`ProjectOut.rfp_created`) gains `files_issue` (the
  issue, detail route only, like `split_issue`); the list routes leave it null.
- `GET /rfp-created` and `/counts` are unchanged in shape except `flags` gains
  `files_issue: {state, open, total} | null` (one batched read of open and
  resolved counts per page, grouped by project). The `missing_files` flag is
  unchanged.

---

## 8. Frontend

- `lib/rfpFiles.ts`: the types above, `fetchRfpFilesIssue`, `resolveRfpFileItem`,
  `reopenRfpFileItem`, `dismissRfpFiles`, `undismissRfpFiles`, `dropRfpFile(projectId,
  file, jobId?)` (multipart via `apiFetch`'s upload path), pure helpers
  `rfpFilesIssueOpen(issue)`, `rfpFileReasonText(item, t)` (the sentence when
  present, else `projectPage.rfpFiles.reason.<token>` by exact token then by
  the token's prefix before the first `:`, else the raw token), `rfpFileBasename`.
- `components/RfpFilesIssueCallout.tsx` on the project page, directly under
  the split callout (`!abandoned && project.rfp_created?.files_issue &&
  state !== "resolved"`), anchored `id="rfp-files"` so the Created page's "Get
  files" lands on it. Tone `warn` (open) / muted note (dismissed, with who and
  when and Undo, the split-outside pattern). Contents, in order:
  1. The headline, chosen by the open items' groups (review finding 8):
     total failure with only `not_downloaded` items "None of the
     invitation's files could be downloaded."; total with only `refused`
     items "All of the invitation's files were downloaded and refused.";
     total with both "None of the invitation's files came in."; partial
     "{{open}} of the invitation's files did not come in." Plus the
     harvest-level reason when a `harvest` item exists.
  2. Group "Could not be downloaded": one row per open item: an icon by kind,
     the basename (full path as the title), the provider, the reason, then
     "Open link" (an `https://` anchor, `target=_blank rel="noopener
     noreferrer"`; never rendered for any other scheme) and, for an attachment,
     the line "Attached to the email from {{from}}, "{{subject}}", received
     {{date}} in {{mailbox}}". A sentence above the group: "Fetch these by
     hand from the link, then drop them below."
  3. Group "Downloaded but refused": the same rows; the sentence above:
     "These reached the app and were refused, so downloading them again will
     not help. Fix the file (re-save it as a PDF, remove the password) and
     drop it below."
  4. Each open row has "Mark handled" (writers). Resolved rows fold into
     "{{n}} handled" (expand to see them with how and who; "Reopen" on each).
  5. The drop zone (`DropZone` from the kit, `accept=".pdf,.doc,.docx,.xls,.xlsx"`,
     folders flattened): "Drop the files you downloaded here. PDFs are split
     by trade and filed into this project's documents; Word and Excel files
     are filed whole." Below it the drop progress: per file in this batch
     (uploading / sent to the splitter / filed whole as {{category}} / failed:
     {{error}}), then, while `drop.status === "processing"`, "The Bid File
     Splitter is working on {{done}} of {{total}} files" with the "Open in
     splitter" link (`/bid-splitter?job=`), polled every 15 s through the
     parent's reload (the split callout's pattern). When the splitter is
     unavailable the hint shows the server's `splitter_unavailable_reason`
     as its own sentence followed by "PDFs dropped here will be filed
     whole." (never nested inside another sentence, review finding 7). When
     `package_sent`, the sentence says files are added whole because the
     package has sent. A failed line shows the server sentence once: when it
     already starts with the file's name and a colon, the name is not
     repeated (review finding 15).
  6. Footer: "Dismiss all" (writers), with the confirm-free semantics of the
     split-outside button (it is undoable).
- `app/(app)/rfp-created/page.tsx`: whenever `flags.files_issue` is non-null
  the "Missing files" badge is NOT shown for that row (the promotion counts
  never learn about drops, review finding 13); instead `open` shows "Files to
  get ({{n}})" (tone warn) with a row action "Get files" (`ButtonLink` to
  `/projects/{id}#rfp-files`), `dismissed` a neutral "Files dismissed" badge,
  `resolved` a neutral "Files handled" badge.
- `lib/help/rfp/troubleshooting.ts` (or `newProjects.ts`, whichever holds the
  "Missing files" scenario today): a scenario "Some of the invitation's files
  did not come in" describing the callout, the two groups, the drop zone and
  the clearing rules.
- i18n: `projectPage.rfpFiles.*` and `rfpCreated.flags.filesToGet`,
  `rfpCreated.flags.filesDismissed`, `rfpCreated.actions.getFiles` in all six
  catalogs (English text in the non-English ones, the RFP convention). Reason
  labels for every token in section 3 and every `files_skipped` reason prefix
  (`not_in_sandbox`, `pages_unverified`, `hazards_unknown`,
  `changed_since_verification`, `converted_sha_mismatch`, `no_digest`,
  `missing_in_storage`, `too_large`, `no_source_object`, `unsupported_format`,
  `unscannable`, `hazard`, `marker`, `converted`, `source_set`).
- The splitter page's job list labels `source = "project"` as "Project" next
  to the project link it already renders.
- Design system: `Callout`, `Badge`, `Button`, `ButtonLink`, `DropZone`,
  `Spinner`, `ErrorText` from `components/ui`; logical utilities (`ps-`,
  `ms-`, `text-start`); no em dashes anywhere in copy.

---

## 9. Notifications

`rfp_file_issues.creation_note(specs) -> str | None`: one sentence appended to
both creation bells and their emails (the `split_note` pattern in
`rfp_create._notify`, metadata key `files_note`):

- total, only not_downloaded: "None of the invitation's files could be
  downloaded; open the project to get them by hand."
- total, only refused: "All of the invitation's files were downloaded and
  refused; open the project to see why."
- total, both: "None of the invitation's files came in; open the project to
  see what happened."
- partial: "{n} of the invitation's files did not come in ({a} could not be
  downloaded, {b} were refused); open the project to see which." (a zero
  part is left out; one reads "was".)

Nothing on resolve or dismiss. The promotion's own completion (if it notifies)
is unchanged.

---

## 10. Security notes

- Every `url` stored is the GC's share link, the platform bid sheet URL the
  harvest rebuilt, or null; the service stores it only when it is `https://`
  on a host the harvester recognised (`cloud_folders` providers or the
  platform clients' hosts), the FE renders it only as a plain anchor with
  `rel="noopener noreferrer"`, and never inside HTML. Names, reasons and
  subjects are GC-typed text rendered as text.
- The drop route runs the upload route's and the splitter route's own
  validation (extension and magic, must open, caps) and its permission gate
  (assignment + writer role); the estimator role is refused.
- `email_ref` repeats facts already visible on the RFP email row to the same
  roles; the receiving mailbox is shown because a person needs it to find the
  message. The project page is not scoped by `profiles.rfp_mailboxes`; this is
  accepted (the sender and subject already appear on the project header as
  `sender_display`).
- The table carries RLS enabled and forced with no policies; only the service
  role reads it.

---

Known gaps (review 2026-10-09, accepted for this slice): the 0141 delete
archive does not snapshot `rfp_file_issues`, so a restored project comes
back without its items (the harvest still holds the facts; a later sync
rebuilds them only on a new promotion or harvest). A project merge moves the
source's open items to the target (`project_merge` updates `project_id`,
skipping an `item_key` the target already has) so nothing a person was
working from disappears with the archived source, but only when the target
is itself an RFP-created project (it has an `rfp_created_projects` row, so
its page shows the callout and its record can carry a dismissal); onto any
other target the items stay with the archived source and the merge logs one
info line.

---

## 11. Configuration

Nothing new. `BID_FILE_SPLITTER_ENABLED`, `RFP_INGESTION_ENABLED` and the
existing split and upload caps decide behaviour.

---

## 12. Tests

- `tests/test_rfp_file_issues.py`: `items_from_harvest` over an email harvest
  (attachment `download_failed`, zip member `too_large`, folder file
  `rejected`, link `needs_sign_in`, link `listed` ignored, images and
  `accepted` ignored, `email_ref` only on attachment items), a Procore
  harvest (`download_failed` with no URL, `skipped_cap`), a failed harvest
  (one `harvest` item with the external URL), `files_skipped` tokens; the
  identity key; `normalise_name` and the match rules; `sync_project`
  idempotent on a second run and additive on a changed harvest;
  `after_file_insert` name match, the first-file rule only on a total failure
  and only once, segment source-name matching; resolve / reopen / dismiss /
  undismiss state machine; `creation_note` wording; `issue_for_project`
  ordering and counts.
- `tests/test_rfp_files_router.py`: permissions (unassigned 403 / accountant
  403 on writes / reads allowed), each route's happy path and refusals, the
  drop route's routing table (PDF to splitter, PDF whole when the splitter is
  off / model away / over cap / package sent, docx to `specification` by
  name, xlsx to `other`, png 415), `job_id` reuse and expiry, up-front
  resolution.
- `tests/test_rfp_split*.py` additions: a `project` job's `fetch_original`,
  `resync_after_run` on a `project` job, the delete refusal and the
  correction lock honouring `SOURCE_PROJECT`.
- Frontend: `tsc` and `eslint` clean; a vitest file for `rfpFileReasonText`
  and `rfpFilesIssueOpen`.

---

## 13. Build record

(Filled in by the build.)

### 13.1 Backend build record (2026-10-09)

Built in `bdr_be` against migration 0151 as written (applied to dev by the
orchestrator; nothing was run against staging or prod, and the migration
was not edited). Unit tests only: no live HTTP call was made.

**Files touched (bdr_be)**

- `app/services/rfp_file_issues.py` (new): the pure builders
  (`items_from_harvest`, `total_failure`, `normalise_name`, `names_match`,
  `safe_url`, `creation_note`, `note_for_harvest`), `sync_project`, the
  resolution paths (`after_file_insert`, `resolve_by_names`,
  `is_first_file` / `no_drawings_yet`, `resolve`, `reopen`, `dismiss_all`,
  `undismiss`, `unresolve_name_matches`) and the wire shapes
  (`issue_for_project`, `item_wire`, `flags_for_records`).
- `app/services/rfp_file_drop.py` (new): `looks_like_spec`, `file_kind`,
  `route_for`, `splitter_state`, `drop_job`, `drop`.
- `app/routers/rfp_files.py` (new) and `app/main.py` (mounted with the
  bidding guard, after `files`).
- `app/services/bid_split.py`: `UploadRefused`, `is_pdf_upload`,
  `check_upload` (extension and magic, must open, page cap),
  `stage_upload` (per-job cap, object, row, `file_count`) and `dispatch`
  (queue, inline fallback), factored out of the splitter route.
- `app/routers/bid_splitter.py`: `upload_job_file` now calls
  `check_upload` + `stage_upload`; `_dispatch` is a thin seam over
  `bid_split.dispatch` (tests patch it); `delete_job` refuses a `project`
  job while its project exists (`MSG_PROJECT_JOB_HAS_PROJECT`). The
  correction lock (`_project_guard`) and `_resync` already keyed on the
  job's project, not its source, so they cover `project` jobs unchanged.
- `app/services/rfp_split.py`: `SOURCE_PROJECT`,
  `MSG_PROJECT_JOB_HAS_PROJECT`; `_insert_row` takes `source_name` and runs
  the first-file pre-check and `after_file_insert` (both callers pass the
  splitter file's filename); `resync_project_files` branches to
  `_resync_project_job` for a `project` job (`project_original` is its
  `fetch_original`); `promote_split_file` recognises a no-sandbox-id file's
  original-bytes row (`_original_rows`); `_PROJECT_ROW_SELECT` gains
  `size_bytes`.
- `app/routers/files.py`: `check_upload_lock` (the hand-off lock rules),
  `_insert_file_row` (insert, object reclaimed on failure) and
  `_after_file_added` (drawing bell, files flag, missing-files hook)
  factored out of `upload_file`, which uses them; `add_team_file` (the
  drop's whole-file path) runs the same three plus the writer gate, the note
  cap and the safe content type.
- Insert-path hooks beside `files_needed.clear_if_satisfied`:
  `routers/files.upload_file`, `services/rfp_create_files._promote_one`,
  `services/rfp_split._insert_row`, `routers/bid_drafts` transfer,
  `services/project_merge._after` (with the target's "no drawings yet" read
  in `merge` before the transaction and `(category, filename)` per moved
  document).
- Sync call sites: `rfp_create._create` right after `_notify` (same harvest,
  the email row when the source is an email); `rfp_create_files.execute`
  after the `complete` write (with this run's `files_skipped`) and
  `mark_from_queue` on `failed`; `rfp_harvest._complete_harvest` (skipped
  without a read when the harvest row says it has no project) and
  `mark_from_queue` on `failed`, both through `_sync_file_issues`, which
  re-reads the harvest. "Retry documents" re-enqueues the promotion job, so
  its sync is the `complete` one; the retry route itself needed no change.
- `rfp_create._notify` gains `files_note` (metadata key `files_note`,
  appended after `split_note` on both bells and their emails).
- `app/routers/projects.py` (`_files_issue_for`, detail route only) and
  `app/models/schemas.py` (`RfpCreatedSummary.files_issue`).
- `app/routers/rfp_created.py`: `flags.files_issue` from one batched read
  per page (`_files_issues`).
- Tests: `tests/test_rfp_file_issues.py` (36), `tests/test_rfp_files_router.py`
  (38), `tests/test_rfp_split_project_jobs.py` (10). Two existing shape
  assertions gained the new null key: `tests/test_rfp_created_router.py`
  (`flags.files_issue`) and `tests/test_projects_router_numbers.py`
  (`rfp_created.files_issue`).

**Decisions where the contract was open**

- URL policy (`safe_url`, applied when an item is stored AND again on the
  way out): `https`, a host, no credentials, no whitespace or control
  characters, at most 2048 characters, and a host the harvester recognises:
  any `cloud_folders` share link (`link_from_url`, unsupported providers
  included) or fetch host (`_host_allowed`), or a platform host the harvest
  clients build bid-sheet URLs on (`app.procore.com`, `login.procore.com`,
  `*.procore.com`, `*.pipelinesuite.com`, `*.smartbid.co`,
  `*.smartbidnet.com`, `*.smartinsight.co`, `*.ionwave.net`). Anything else
  stores null; the item still lists with its reason.
- Item key: `kind|origin|status|name` (files and harvests), `link|<links[].key>`
  (falling back to the URL, then the label). A key over 400 characters
  swaps the name for its sha256.
- `provider` for the email harvester's methods (`organic`, `general`,
  `nonorganic`) is `email` on a `harvest` item (the FE labels `email`); a
  file entry keeps its own provider (`graph`, `sharepoint`, ...).
- `files_skipped` repeats every harvest entry that never reached the
  sandbox (`download_failed`, `rejected`, `expanded`, `skipped`, ...). A
  skip becomes a promotion item only when the harvest entry with the same
  basename was `accepted` or `reused`; with no matching entry it counts
  unless its reason is one of those harvest tokens (`too_large` is not one:
  it is also the promotion's own cap).
- `files_issue_total` is stamped by the sync that records the project's
  first items when the harvest brought in nothing (`total_failure`); it is
  never flipped later. A creation whose harvest yields no items leaves it
  false.
- `creation_note(specs, *, total=False)`: the total flag is passed in
  (`note_for_harvest` computes it). The partial sentence omits a zero part
  and uses "was" for one: "3 of the invitation's files did not come in (2
  could not be downloaded, 1 was refused); open the project to see which."
- `after_file_insert(..., first_file=False)`: `is_first_file(sb,
  project_id, category)` is the before-insert check (no query unless the
  category is a drawing or specification). Name matches run first; the
  first-file rule then resolves the rest. Automatic resolutions audit
  `rfp_files.resolve` with `how` (`name_match` / `auto_first_file`).
- Gates: the router 404s while `RFP_INGESTION_ENABLED` is off and is
  mounted with the bidding guard. The read takes `require_internal` as well
  as `require_project_assignment`, so the external estimator never reads an
  issue (the project detail route's own role rule); the writes take
  `require_writer`. Budgets: the catch-all on reads and toggles, the upload
  budget on the drop.
- Drop routing:
  - Once the package has sent, every whole file (PDF or Office) lands under
    `additional` with its note, because the shared lock policy freezes
    `drawing` / `specification` after the hand-off and post-hand-off
    material goes in as an update. The PDF note reads "Added whole from the
    missing-files drop zone: this project's package has already been sent,
    so it was added under Additional files." `MSG_PACKAGE_SENT` therefore
    never refuses a drop (the splitter route is never chosen once sent).
  - The page count is read only when a PDF could go to the splitter. A PDF
    that will not open is a 415 with pdf_split's sentence (the splitter's
    "must open" rule); when the splitter could not take it anyway it lands
    whole unread.
  - Office magic is checked here (OLE for `.doc` / `.xls`, the zip container
    for `.docx` / `.xlsx`); the upload route itself checks no magic, so the
    drop's check is new rather than reused.
  - `looks_like_spec`: underscores, dots and dashes read as spaces, then
    `spec` at the start of a word (so "Specs", "SpecBook", "Specifications",
    not "Inspection"), `project manual`, `division`, or a leading `NN NN NN`.
  - The why clauses: "the Bid File Splitter is turned off on this server",
    "the Bid File Splitter cannot run right now because {the model-away
    reason}", "the PDF has {n} pages, over the splitter's limit of {cap}
    pages per file".
  - `job_id` joins only a uuid that names this project's `project` job,
    processing or settled within 10 minutes, below the per-job file cap;
    otherwise a new job (so a full job never 400s a drop).
  - The up-front name match runs after the validation and the lock check,
    before anything is stored; if the store then fails, exactly those
    `name_match` resolutions are reopened. The splitter run is dispatched
    after the staging succeeded.
  - The drop route checks the project exists (404) before reading the body
    and holds the large-upload slot while the body is resident.
- A `project` job's re-file never touches `rfp_created_projects.files_promoted`
  (the promotion's count). Its original-bytes row (the source set or the
  intact file) is recognised by the source's filename AND byte size, so a
  correction updates it in place instead of deleting and uploading it again.
  `Promote` carries no category: the splitter's segment decides it, as for an
  RFP job (`looks_like_spec` only names whole drops).
- Messages: 409 "Nothing is open to dismiss." / "Already dismissed." / "The
  missing files are not dismissed."; 404 "This item is not open on this
  project." / "This item is not marked handled on this project." / "This
  project was not created from an RFP invitation."; 415 "{name}: the drop
  zone accepts PDF, Word and Excel files"; the splitter's own 400 / 413
  sentences pass through.

**Tests and lint (final tree)**

- `.venv/bin/pytest -q`: 6728 passed, 1 skipped (84 of them new).
- `.venv/bin/ruff check app tests`: All checks passed!

**Not finished, and risks**

- No live pass on dev (unit tests only, by instruction). Worth checking
  there: a real email harvest with a sign-in link and a failed attachment,
  the creation bell sentence, a drop of two PDFs and a .docx through the
  queue, the segments filing by trade, and "Harvest again" adding items.
- A PDF dropped while the package is unsent but which finishes splitting
  after the package sends is not filed (`resync_after_run` respects the
  lock, as for RFP jobs); its item was already resolved at drop time.
- Project delete / restore: `rfp_file_issues` cascades with the project;
  whether the 0141 archive snapshot picks the new table up was not checked.
- Staging and prod need 0151 at release, with approval. The code is
  defensive before it (every hook, sync and read is best effort and the
  project page answers `files_issue: null`), but the router's writes and
  the drop's `project` job need the migration.

### 13.1a Backend fix pass (2026-10-09)

The review's findings against 13.1, fixed in `bdr_be` (unit tests only, no
live HTTP call, no SQL anywhere, migration 0151 unchanged).

**By finding**

1. A dropped PDF is never lost. New `rfp_split.file_project_job_whole(sb,
   file_id, reason, *, package_sent=None)` (and `_quietly`, which never
   raises) files a `project` job's stored source PDF whole: category by
   `rfp_file_drop.looks_like_spec` else `drawing`, `additional` once the
   package has sent; `uploaded_by` the job's `created_by`; no harvest;
   `bid_split_file_id` set; the normal insert hooks as a system insert
   (`by_person=False`); audited `file.upload` with `whole: true`. It is a
   no-op for any other job and when a row of the same `bid_split_file_id`
   already holds the original bytes (`_original_rows`: same filename and
   size), so a later successful re-file turns that row into the source set
   in place. Called from `bid_split._mark` on every `failed` transition (the
   queue's ladder and the inline fallback both mark through it), and from
   `resync_after_run` when the package has sent before the run ended, when
   the re-file raises, and when a `done` run filed nothing. Notes: "The Bid
   File Splitter could not split this file ({reason}); it was added whole."
   (plus "under Additional files because the package had already been
   sent." after the hand-off) and "The Bid File Splitter split this file,
   but the package had already been sent when the split finished, so it
   was added whole under Additional files."
2. `auto_first_file` resolves only open `link` and `harvest` items; named
   `file` items never clear that way.
3. A platform file item (no `origin`) carries `safe_url(harvest.external_url)`;
   promotion items keep their link-only rule.
4. `after_file_insert(..., by_person=False)`: the first-file rule runs only
   for `by_person=True`. True: `routers/files.upload_file` (and so the drop's
   whole route through `add_team_file`), the bid draft transfer,
   `project_merge`, and a `project` drop job's segments (a person dropped
   the file; `promote_split_file` threads `by_person` down to `_insert_row`).
   False: the promotion (`_promote_one` no longer runs the before-insert
   check at all), the RFP split's re-file, and the whole-file fallback of
   finding 1. `total_failure_live(record)` (the stamp AND `files_promoted ==
   0`) is the issue's `total_failure` and the rule's test.
5. A job opened by this drop whose first file fails to stage is discarded
   (its objects, then the row, only while it still holds no file); a job the
   batch already used is kept. `issue.drop` is the newest `project` job WITH
   files (the newest 20 are scanned in one files read). Also
   `routers/bid_splitter._attach_segments` no longer queries with a
   placeholder id when a job has no files (the smoke's 500).
6. `email_ref` comes from the harvest's own `rfp_email_id`, read only for an
   email-harvester method; an `email` handed in that is not that message is
   ignored.
8. `creation_note(specs, total=True)` reads by group: only not downloaded,
   only refused, or both (section 9 wording).
10. `add_team_file` makes everything after its insert best effort (audit,
    preview, drawing bell, files flag, missing-files hook; logged), so the
    drop reopens its up-front matches only when nothing was stored.
11. `flags_for_records` pages each 200-project chunk in 1000-row ranges
    (ordered by id) until a short page; the per-project item reads
    (`_items`, `_open_items`) page the same way.
12. `state` is `open` whenever an item is open, else `dismissed` when
    stamped, else `resolved` (the list flags use the same rule).
    `dismiss_all` answers 409 only when nothing is open ("Nothing is open to
    dismiss."); a second dismissal re-stamps and resolves the new items;
    `undismiss` reopens exactly the `dismiss_all` rows. "Already dismissed."
    is gone.
14. The stem fallback needs a recognised extension on both names: a final
    `.` and 1 to 5 letters or digits WITH at least one letter. The letter is
    what keeps `E-1.1` (a sheet number) from matching `E-1.pdf` or `E-1.2`,
    as the tests require; an all-digit suffix is part of the name.
16. The drop validates PDFs with `bid_split.check_upload` itself; its 413
    now carries `pages` (`UploadRefused.pages`), which the drop turns into
    the over-cap whole route; its 415s answer as they are.
17. `project_merge.merge` moves the source's OPEN items to the target
    (`rfp_file_issues.move_open_items`, skipping an `item_key` the target
    already has) right before the first `merge_rfp_project` call, and moves
    them back (`move_items_back`) when the merge then fails. Best effort.

**Files touched in this pass**

`app/services/rfp_file_issues.py`, `app/services/rfp_file_drop.py`,
`app/services/rfp_split.py`, `app/services/bid_split.py`,
`app/services/rfp_create_files.py`, `app/services/project_merge.py`,
`app/routers/files.py` (also gained its module logger),
`app/routers/bid_splitter.py`, `app/routers/bid_drafts.py`,
`app/routers/rfp_files.py` (docstring); tests
`tests/test_rfp_file_issues.py` (40), `tests/test_rfp_files_router.py` (41),
`tests/test_rfp_split_project_jobs.py` (15), and two merge tests appended to
`tests/test_project_merge.py`. The service tests now capture `audit`
module-wide (an uncaptured call would have reached for the live client).

**Tests and lint (final tree)**

- `.venv/bin/pytest -q`: 6742 passed, 1 skipped.
- `.venv/bin/ruff check app tests`: All checks passed!
- `.venv/bin/python -c "import app.main"`: clean.

**Still open**

- Items moved onto a merge target that has no `rfp_created_projects` row are
  readable through `GET /rfp-files/issue`, but the project page only renders
  the callout from `rfp_created.files_issue`, and "Dismiss all" answers 404
  there (no record to stamp). Mark handled and Reopen work.
- The 0141 restore gap stays as section 10 records it.
- Section 7's table still lists "Already dismissed." as a 409 sentence; the
  service no longer answers it (finding 12).

### 13.1b Backend final fix pass (2026-10-09)

Three items from the re-verification, fixed in `bdr_be` (unit tests only,
no live HTTP call, no SQL anywhere).

- **N1, a dropped PDF landing twice.** `rfp_split.file_project_job_whole`
  now reads every row of the file first. A row holding the original bytes
  still means "nothing to do". When the file has OTHER rows (a re-file that
  landed some segments and then failed: a segment copy, or the source-set
  upload), the original is kept as the source set (`other`, `is_source_set`,
  "Source set: split into {n} documents", through `_upload_original`) and
  never filed whole in a hand-off category beside its own segments. The
  next successful re-file recognises that row (`_original_rows`) and keeps
  it in place, so the project converges to one source set plus the
  segments. Only a file with no rows at all is filed whole in `drawing` /
  `specification` / `additional` as before. The reviewer's repro
  (`scratchpad/review/test_repro_pass2.py`) passes (3 passed, run with the
  suite's conftest); `tests/test_rfp_split_project_jobs.py` gains both
  scenarios with the convergence asserted.
- **N3, the drop summary under a row cap.** `issue.drop` reads the newest
  drop jobs (up to 20) newest first and each job's files on their own,
  paged by 1000 rows (`_job_files`), stopping at the first job with a file.
  The newest job's files are never cut and an older job is never shown in
  its place; usually one jobs read and one files read.
- **Finding 17, merge onto a non-RFP target.** `move_open_items` moves the
  source's open items only when the target has an `rfp_created_projects`
  row; otherwise they stay with the archived source and one info line is
  logged. Section 10's merge sentence was edited in place to say so. The
  merge tests now seed the target's record for the move, and a new test
  pins that a target without one never takes the items.

**Files touched in this pass:** `app/services/rfp_split.py`,
`app/services/rfp_file_issues.py`, `docs/RFP_MISSING_FILES.md` (section 10,
one sentence, and this record), `tests/test_rfp_split_project_jobs.py` (17),
`tests/test_rfp_file_issues.py` (41), `tests/test_project_merge.py` (88).

**Tests and lint (final tree)**

- `.venv/bin/pytest -q`: 6746 passed, 1 skipped.
- `.venv/bin/ruff check app tests`: All checks passed!
- `.venv/bin/python -c "import app.main"`: clean.

### 13.2 Frontend build record (2026-10-09)

Built against section 7 only: the backend routes were not on dev while the
frontend was written, so nothing here has been exercised against a live
issue yet.

**Files touched (bdr_fe)**

- `lib/rfpFilesView.ts` (new): the section 7 wire shapes and every pure
  helper (`rfpFilesIssueOpen`, `rfpFileReasonText`, `rfpFileReasonKey`,
  `rfpFileBasename`, `rfpFileHttpsUrl`, `rfpFileProviderText`,
  `rfpFileResolvedHowKey`, `rfpFilesGroups`, `rfpFilesHeadline`,
  `rfpFilesCalloutVisible`, `rfpFilesDropRunning`, `rfpDropPrecheck`,
  `rfpDropNextJobId`). It has no runtime imports, so plain `node --test`
  can load it, the same pattern as `lib/rfpBcView.ts`.
- `lib/rfpFiles.ts` (new): `fetchRfpFilesIssue`, `resolveRfpFileItem`,
  `reopenRfpFileItem`, `dismissRfpFiles`, `undismissRfpFiles`,
  `dropRfpFile(projectId, file, jobId?)` (multipart through `apiFetch`, which
  leaves the Content-Type to the browser for FormData), and it re-exports
  `rfpFilesView`, so `lib/rfpFiles.ts` has the whole surface section 8 names.
- `components/RfpFilesIssueCallout.tsx` (new): the callout.
- `lib/rfpCreated.ts`: `RfpCreatedSummary.files_issue` (the issue) and
  `RfpCreatedFlags.files_issue` (`{state, open, total}`), both optional.
- `app/(app)/projects/[id]/page.tsx`: mounted directly under the split
  callout, wrapped in `div#rfp-files` (`scroll-mt-20` for the sticky header).
  Actions use `canEditDetails` (`canWrite`, WRITER_ROLES); everyone sees the
  lists.
- `app/(app)/rfp-created/page.tsx`: "Files to get ({{n}})" (warn) replaces
  "Missing files" while `files_issue.state === "open"`. "Files dismissed"
  (neutral) shows when dismissed. The "Get files" ButtonLink goes to
  `/projects/{id}#rfp-files` and only shows while the issue is open.
- `app/(app)/bid-splitter/page.tsx`: a "Project" badge for `source =
  "project"`, next to the project link in the job list and in the job
  detail. The delete modal treats a `project` job like an `rfp` job (refused
  while the project exists, mirroring section 5) and has its own sentence.
- Help: `lib/help/rfp/harvest.ts` gets the new scenario `s-harvest-files-to-get`
  ("Some of the invitation's files did not come in") next to the Missing
  files scenario. The "Fix a project flagged Missing files" steps and the
  old scenario now send people to the box first. The "Nothing tells you"
  callout now mentions the creation bell's sentence. Also updated:
  `failures.ts` (the at-a-glance row that said "Download from the source and
  upload by hand", and the principles bullet), `newProjects.ts` (rows for
  the two new flags) and `glossary.ts` ("Files to get"). `index.ts` did not
  need a change.
- `locales/{en,ceb,fil,hi,sw,ur}/translation.json`: `projectPage.rfpFiles.*`,
  `rfpCreated.flags.filesToGet`, `rfpCreated.flags.filesToGetTitle`,
  `rfpCreated.flags.filesDismissed`, `rfpCreated.actions.getFiles`,
  `bidSplitter.detail.projectSource`, `bidSplitter.detail.projectSourceTitle`,
  `bidSplitter.history.deleteRefusedProjectDrop`. Every catalog gets the
  English text (the RFP convention). The keys were merged by a script that
  only adds keys. All six catalogs are still valid JSON.
- `lib/__tests__/rfpFiles.test.mjs` (new, 18 tests). It covers the reason
  lookup, open and visibility, grouping, the headline, the https-only rule,
  basenames, providers, resolution lines, the drop precheck, and job-id
  threading. It also checks that every section 3 token and every
  `files_skipped` prefix has the same English label in all six catalogs, and
  that the new copy has no em dash.

**Copy and behaviour decisions**

- Reason labels. i18next reads `:` as its namespace separator, so no key
  ever holds a `:`.
  - Section 3 tokens are labelled under `projectPage.rfpFiles.reason.<token>`.
  - Promotion tokens (`skipped:<reason>`) are labelled under
    `projectPage.rfpFiles.skippedReason.<prefix before the first ":">`. The
    rest of the token shows in brackets: `skipped:unscannable:encrypted`
    reads "The PDF could not be scanned (encrypted)".
  - "Exact token" therefore covers tokens without a `:`. A token with a `:`
    falls back to its prefix.
  - Promotion reasons get their own root because `too_large` means two
    things: the download cap and the project-document cap.
  - Also labelled: `ooxml`, `rejected` and `failed`, which
    `rfp_create_files` also writes as skip reasons.
  - Unknown or non-key-safe tokens show raw. Labels carry no closing
    period; server sentences keep theirs.
- Headline. When an open `harvest` item exists, its reason joins the
  headline ("What went wrong: {{reason}}.") and is not repeated on that row.
- Email line.
  - Attachments and zip members read "Attached to the email from ...".
  - An email-method `harvest` item reads "From the email from ...".
  - The sender shows as "Name (address)" when the two differ.
  - Missing parts get fallbacks ("no subject", "an unknown mailbox").
- Visibility, one extension to section 8. The callout also stays (info tone,
  progress and "Open in splitter" only) while `drop.status === "processing"`
  after the last item resolved. Without this, dropping the last missing PDF
  resolves it up front and the splitter progress disappears at once. The
  rule is `rfpFilesCalloutVisible`.
- Drop zone.
  - The browser refuses three things before sending: non PDF/Word/Excel
    files (same sentence as the 415), empty files, and files over
    `UPLOAD_MAX_BYTES` (450 MB). Every other check is the server's.
  - Each request is wrapped in `withRateLimitRetry`, so a big drop waits
    out the upload limiter.
  - A batch starts with no `job_id` and passes the first splitter answer's
    `job_id` for the rest.
  - The project reloads once, at the end of the batch.
  - Per-file lines: waiting / uploading / sent to the splitter / filed
    whole as {{category}} (labelled via `filesPanel.categories`) /
    failed: {{server sentence}}.
  - A new drop replaces the previous batch's lines. The zone is disabled
    while a batch is sending.
- Hint under the zone.
  - When the splitter can run, the hint says how PDFs and Office files
    are filed, and the sentence under it gives the page cap.
  - When the splitter is unavailable, that sentence quotes
    `splitter_unavailable_reason` and says PDFs will be filed whole.
  - When `package_sent`, it says dropped files are added whole because
    the package has sent.
- "Working on" copy reads "The Bid File Splitter is working on the dropped
  files: {{done}} of {{total}} done. Each one is filed by trade as it
  finishes.", because the contract's wording read as if `done` files were
  in progress. Once the drop job has failed files, they are listed (first 8,
  then "and N more") with "Open in splitter". It polls through the parent's
  reload every 15 s, the split callout's pattern.
- Dismissed shows a muted note ("Missing files dismissed by {{name}} on
  {{date}}.") with Undo for writers, and no list or drop zone. "Dismiss all"
  has no confirm because it is undoable.
- Handled items fold into "{{n}} handled". Expanding shows each one with how
  and who: "a file with this name arrived on ...", "marked handled by ...",
  "cleared ... when the first drawing or specification arrived", "dismissed
  by ...". Each has "Reopen" for writers.
- Anchor. The project loads after navigation, so the browser's own hash
  scroll finds nothing. The callout scrolls `#rfp-files` into view on mount
  when the URL hash asks for it.
- Links. Only a URL that parses as `https:` with a host and no credentials
  renders, as an anchor with `target="_blank" rel="noopener noreferrer"`.
  Every other value renders nothing. Names, reasons and subjects are text.
- "Open in splitter" is shown to everyone, as in the split callout. The
  splitter page's own gate applies.

**Gates (final tree, run from bdr_fe)**

- `npm run build`: exit 0. "Compiled successfully in 3.9s", "Finished
  TypeScript in 6.6s", "Generating static pages using 9 workers (53/53)".
- `npm run lint`: exit 0. "2 problems (0 errors, 2 warnings)". Both warnings
  are older and in files this slice did not touch
  (`NotificationPrefsSection.tsx:100`, `Sidebar.tsx:291`).
- `npm test`: exit 0. tests 46, pass 46, fail 0 (18 of them new).

**Not finished**

- No browser pass, because the routes did not exist yet. Once the backend
  lands, check these on dev:
  - an open callout with mixed groups and a harvest item
  - Mark handled / Reopen
  - Dismiss all / Undo
  - a drop of two PDFs and a .docx (one splitter job, the running poll)
  - the splitter-off and package-sent sentences
  - the Created page badge and "Get files" landing on the anchor
- There is no help screenshot of the callout.
- The help text about the creation bell's files sentence assumes section 9
  ships.

**Contract notes (nothing blocking, no extra fields added)**

- `drop.status` is read as the `bid_split_jobs` status. The frontend only
  compares it with `"processing"`.
- `provider` labels cover `graph`, `email`, the cloud_folders spellings
  (`gdrive`, plus `google_drive` just in case) and the platform methods.
  Any other value shows verbatim.
- The drop answer's `category` is read as a `project_files` category token.
- A `resolved.how` outside the four known values gets a generic line.
- The frontend does not read `issue` from action answers; it reloads the
  project instead. A null `issue` there is therefore harmless.
- The 409, 413 and 415 sentences from `POST /drop` appear verbatim on that
  file's progress line, so they should stay short and person-facing.
- Section 12 asks for "a vitest file". The repo runs `node --test` over
  `lib/__tests__/*.test.mjs`, so the test follows the repo.

### 13.2a Frontend fix pass (2026-10-09)

This pass answers the review and the dev smoke; the callout itself passed
every browser step. Numbering follows the review.

- **7, the hint under the drop zone.** The new pure helper
  `rfpFilesZoneNote(issue, t)` decides it. The server's
  `splitter_unavailable_reason` is now its own sentence: trimmed, with a
  full stop added when it has no closing punctuation. It is followed by
  "PDFs dropped here will be filed whole, not split by trade." The new key
  `projectPage.rfpFiles.splitterUnavailableReason` is
  "{{reason}} PDFs dropped here ...". The old nested key
  `projectPage.rfpFiles.splitterUnavailable` is removed from all six
  catalogs. Package sent, the no-reason sentence and the page cap are
  unchanged.
- **9, the job id.** `rfpDropNextJobId` now threads the `job_id` of the
  LATEST answer that carried one. An answer without one (a file filed whole)
  keeps the current id.
- **13, the Created page.** Whenever `flags.files_issue` is non-null, the row
  never shows "Missing files". The new pure helper `rfpFilesFlagBadge(flag)`
  picks the badge:
  - `open`: "Files to get ({{n}})" (warn), plus "Get files".
  - `dismissed`: "Files dismissed" (neutral).
  - `resolved`: "Files handled" (neutral, new key
    `rfpCreated.flags.filesHandled`).

  A row without an issue keeps "Missing files" as before. The help flags
  table (`lib/help/rfp/newProjects.ts`) gains the "Files handled" row and
  says the issue badge replaces Missing files.
- **15, the failed line.** The new pure helper `rfpDropErrorText(name,
  error)` drops a leading "<name>:", compared trimmed and case-insensitively
  against the name and its basename. The line now reads "X.pdf: failed: the
  drop zone accepts PDF, Word and Excel files". The drop job's failed list
  gets the same treatment.
- **8, the headline.** `rfpFilesHeadline` chooses by `counts.not_downloaded`
  and `counts.refused` when `total_failure` is set:
  - only not-downloaded: "None of the invitation's files could be
    downloaded." (`headlineTotal`)
  - only refused: "All of the invitation's files were downloaded and
    refused." (new `headlineTotalRefused`)
  - both: "None of the invitation's files came in." (new
    `headlineTotalBoth`)

  Partial is unchanged.
- **12, the state.** The callout branches on the new pure helper
  `rfpFilesCalloutMode(issue)`:
  - `list` whenever the state is `open`, even with `issue.dismissed` set.
  - `dismissed` (the muted note with Undo) only for the `dismissed` state.
  - `progress` for a resolved issue whose drop is still splitting.
  - nothing otherwise.

  `rfpFilesCalloutVisible` now derives from the mode, and tests cover it.
- **Smoke copy, the delete refusal.**
  `bidSplitter.history.deleteRefusedProjectDrop` now matches the API word
  for word: "This split was started from a project's missing-files drop zone
  and its project still exists; discard the project first."

**Files touched (bdr_fe)**

- `lib/rfpFilesView.ts`
- `components/RfpFilesIssueCallout.tsx`
- `app/(app)/rfp-created/page.tsx`
- `lib/help/rfp/newProjects.ts`
- the six `locales/*/translation.json`. A script made exact
  add / replace / remove edits that check the old value first. The keys
  are identical in all six, with English text.
- `lib/__tests__/rfpFiles.test.mjs`: 23 tests now. The new ones cover each
  finding above and check that the `projectPage.rfpFiles` subtree is
  identical in all six catalogs.

**Gates (final tree, run from bdr_fe)**

- `npm run build`: exit 0. "Compiled successfully in 4.0s", "Finished
  TypeScript in 6.8s", "Generating static pages using 9 workers (53/53)".
- `npm run lint`: exit 0. "2 problems (0 errors, 2 warnings)", both older
  and outside this slice (`NotificationPrefsSection.tsx:100`,
  `Sidebar.tsx:291`).
- `npm test`: exit 0. tests 51, pass 51, fail 0.

**Open, for the backend**

- `rfp_file_issues.issue_state` still answers `dismissed` whenever the stamp
  is set, as checked on 2026-10-09. The frontend already follows the new
  rule.
- `dismiss_all` still answers 409 "Already dismissed." while the stamp is
  set. If that stays, "Dismiss all" on an issue a sync reopened after a
  dismissal will show that sentence. Either re-stamp and resolve when items
  are open, or tell the frontend to offer Undo there.
- Left as is, per the coordinator: the intake modal opening over the "Get
  files" landing (existing intake behaviour).

### 13.2b Frontend: Undo dismissal on a reopened issue (2026-10-09)

Finding from the dev browser re-run, which passed all 7 checks against
13.2a. A later sync can add items after a Dismiss all. The state is then
`open` and the list shows, but the earlier dismissal could only be undone
item by item with Reopen.

- New pure helper `rfpFilesCanUndoDismissal(issue)`. It is true only when
  the state is `open` AND `issue.dismissed` is non-null. The `dismissed`
  state keeps its own Undo on the muted note.
- In that case the callout footer shows "Undo dismissal" (writers only), a
  ghost button beside "Dismiss all".
  - It calls `undismissRfpFiles` (`DELETE /dismiss`), which reopens exactly
    the `dismiss_all` items.
  - Its title says who dismissed and when (the existing `dismissedNote`
    sentence).
  - It shares the busy and error state of the other actions.
  - The dismissed items still appear in the handled fold as "dismissed by
    ...".
- New key `projectPage.rfpFiles.undoDismissal` = "Undo dismissal", in all
  six catalogs (English).
- Files touched (bdr_fe): `lib/rfpFilesView.ts`,
  `components/RfpFilesIssueCallout.tsx`, the six
  `locales/*/translation.json`, and `lib/__tests__/rfpFiles.test.mjs`. The
  new test covers the visibility rule (open + stamp yes; open without stamp,
  dismissed, resolved and null no) and the key in all six catalogs.
- Backend check (2026-10-09): `issue_state` now answers `open` whenever any
  item is open. `dismiss_all` now re-stamps and resolves the new items
  instead of answering 409. Both items 13.2a left open for the backend are
  closed, so "Dismiss all" and "Undo dismissal" both work on a reopened
  issue.

**Gates (final tree, run from bdr_fe)**

- `npm run build`: exit 0. "Compiled successfully in 3.9s", "Finished
  TypeScript in 6.8s", "Generating static pages using 9 workers (53/53)".
- `npm run lint`: exit 0. "2 problems (0 errors, 2 warnings)", both older
  and outside this slice (`NotificationPrefsSection.tsx:100`,
  `Sidebar.tsx:291`).
- `npm test`: exit 0. tests 52, pass 52, fail 0.
