# RFP Ingestion: Bid File Splitter step (harvest to categorized project files)

Design contract for the sixth slice of RFP Ingestion: after harvest and before
project creation, every harvested document goes through the Bid File Splitter
so the project is born with its documents already categorized (drawing sets
cut into trade sets, specs / RFP / addenda identified and left intact), every
category travels to the external estimators with the hand-off, and every
vendor RFQ carries the specifications from now on.

Status: decisions taken with the user 2026-09-18 (section 1). DEV ONLY.
Migration 0132 applies to the dev database only. No em dashes anywhere.

This file is the contract three parallel builders share. Names here are
final; where a builder finds the code disagrees with a detail, the builder
follows the code's existing pattern and records the difference in section 9.

Naming: setting prefix `rfp_split_` (env `RFP_SPLIT_`), service
`app/services/rfp_split.py`, pipeline status `split` (pending) on
`rfp_emails` and `rfp_portal_invitations`, migration `0132_rfp_split.sql`,
test-bench step `split`, FE namespace additions under `rfpCreated` and
`bidSplitter`.

---

## 1. Decisions locked in (2026-09-18)

| Topic | Decision |
|---|---|
| Where | New pipeline status `split` between `harvest` and `create`, on both rfp_emails and rfp_portal_invitations. The pipeline splits from the verified originals, then creates the project with the segments already categorized. No human gate before creation; corrections happen afterward (section 6). |
| Waiting | The card shows "Splitting documents" while the split job runs. The row re-checks every `rfp_split_poll_seconds` (30). A split that fails outright still creates the project, flagged "split failed", with files promoted by the pre-split mapping. |
| Categories | Eight new `file_category` values: `civil_drawing`, `structural_drawing`, `architectural_drawing`, `mechanical_drawing`, `plumbing_drawing`, `fire_protection_drawing`, `low_voltage_drawing`, `rfp`. They are first-class everywhere (upload modals, Plans & Specs Log, ZIP export, estimator portal, hand-off freeze), not only on RFP-created projects. Splitter map in section 3. |
| Estimators | Every category above plus `rfp` and `other` are part of the initial package: readable by estimators the moment they exist, frozen once the hand-off has sent. The hand-off flow itself does not change. |
| Source set | After a drawing set is split, the untouched source PDF is kept on the project as `other` with the note "Source set: split into N documents". It is NOT sent to estimators or vendors (excluded from the package and RFQ attachments by its `is_source_set` flag). |
| Addenda | Split segments filed as `addendum` carry the addendum number parsed from the model's segment name when it parses (`Addendum 3`, `Add. #3`, `ADD-03`), otherwise null, and a null issue date. The Estimating Admin fills them in later. Manual uploads keep requiring both. |
| Specs to vendors | Every RFQ send that uses default attachments adds the project's `specification` files to the same shared folder link the drawings use. The Modify Files modal lists them pre-checked. Nudges and re-sends are untouched. |
| Non-PDF files | The splitter never splits a non-PDF. Instead the AI categorizes the file into one applicable category (section 3.3): docx/xlsx/doc/xls through the sandbox's converted PDF (triage only, no per-page pass), everything else from the filename, path and invitation context. The original bytes are what gets promoted. |
| Limits | Splitter cap raised 100 to 250 files per job; harvest caps raised to 250 (`rfp_harvest_max_files`, `rfp_ingest_max_files_per_run`). File size allowance raised 50%: `upload_max_bytes` 300 MB to 450 MB, the storage buckets that were set to 300 MB in 0106 to 471859200, `rfp_ingest_max_file_bytes` scaled by the same factor, FE constants to match. The Supabase Dashboard global upload limit is a manual setting (release step, section 8). |
| Flags | The split step runs only when `BID_FILE_SPLITTER_ENABLED` and the new `RFP_SPLIT_ENABLED` (default false) are both on. Otherwise a row entering `split` falls straight through to `create`, so behavior without the flag equals today's. |
| Corrections | The Created-from-RFPs card and the project's files panel link to the split job (`/bid-splitter?job=`). A correction in the splitter UI re-files the matching project files as long as the hand-off has not sent; after that the splitter answers 409 "This project's package has already been sent; correct the files on the project." |
| Test bench | The monitor gains a `split` step after `harvest`, with per-file events. |

---

## 2. Schema (migration `0132_rfp_split.sql`, owned by builder A)

```
alter type file_category add value if not exists 'civil_drawing';         (x8, see section 1)
alter type file_category add value if not exists 'rfp';

project_files
  + bid_split_segment_id uuid null references bid_split_segments(id) on delete set null
  + bid_split_file_id uuid null references bid_split_files(id) on delete set null   (the segment FK nulls on a re-cut; resync finds the old rows by file)
  + is_source_set boolean not null default false
  partial unique index project_files_split_segment_uidx (project_id, bid_split_segment_id) where bid_split_segment_id is not null
  addendum number / issue date columns: allow null (check the 0075 columns; the API keeps requiring them on manual upload)

bid_split_jobs
  + source text not null default 'manual' check (source in ('manual','rfp'))
  + rfp_harvest_id uuid null references rfp_harvests(id) on delete set null
  + project_id uuid null references projects(id) on delete set null
  index on rfp_harvest_id, index on project_id

bid_split_files
  + rfp_sandbox_file_id uuid null references rfp_ingest_files(id) on delete set null
  + source_format text null            -- pdf | docx | xlsx | doc | xls | image | other
  + classified_from text null          -- 'pages' (normal split/triage) | 'converted_pdf' | 'name'
  page_count stays nullable (null for non-PDF rows)

rfp_harvests
  + split_status text not null default 'none' check (split_status in ('none','pending','running','complete','failed','skipped'))
  + split_job_id uuid null references bid_split_jobs(id) on delete set null
  + split_error text null
  + split_started_at timestamptz, split_finished_at timestamptz

rfp_emails.status CHECK and rfp_portal_invitations.status CHECK: add 'split' (pending, between harvest and create)
rfp_created_projects: + split_status text null, + split_job_id uuid null  (denormalized for the page; written at creation)
rfp_test_events: no DDL (kinds are free text)

storage.buckets: every bucket 0106 set to 314572800 goes to 471859200
```

Widen the `bid_split_segments.category` CHECK only if a new bucket is
needed (none is: the splitter's own 13 categories stay as they are; the map
to `file_category` lives in code, section 3.1).

Statuses: `split` joins the pending sets and the ladders in
`rfp_email_ingest.py` and `rfp_portal_ingest.py` exactly where `create` did
in 0130 (`set_method` accepts it, `dismiss` refuses it, `ignore` accepts it).

---

## 3. The split step (builder A)

### 3.1 Category map (code, `app/services/rfp_split.py`)

```
SEGMENT_TO_FILE_CATEGORY = {
  "general_drawings": "drawing",
  "civil_drawings": "civil_drawing",
  "structural_drawings": "structural_drawing",
  "architectural_drawings": "architectural_drawing",
  "mechanical_drawings": "mechanical_drawing",
  "plumbing_drawings": "plumbing_drawing",
  "electrical_drawings": "electrical_drawing",
  "fire_protection_drawings": "fire_protection_drawing",
  "low_voltage_drawings": "low_voltage_drawing",
  "specifications": "specification",
  "addenda": "addendum",
  "rfp": "rfp",
  "other": "other",
}
```

### 3.2 `_step_split(sb, row)` in both ingest modules

0. Flags off (`settings.bid_file_splitter_enabled and settings.rfp_split_enabled`
   is false) or the harvest has no accepted/reused entries or the harvest
   already has a project (link-only case) -> `split_status = skipped`, CAS
   `split -> create`. Return.
0b. The splitter's model is away (`rfp_split.model_away`: nothing
   configured for `bid_split`, or the cached LLM health snapshot grades it
   provider_down / model_missing / unconfigured, the same read the three
   LLM intake steps make) -> no job is staged, no attempt is spent:
   `next_attempt_at = now + RFP_EMAIL_INGESTION_CLASSIFY_RETRY_SECONDS`,
   the reason in `last_error`, a `split.waiting` bench event. The row waits
   for as long as the model is away (2026-09-18 rule: the ladder is only
   for real failures; "LLM not connected" always waits).
0c. The sandbox is still checking any entry the split would stage
   (`rfp_split.sandbox_busy`: an entry's `rfp_ingest_files` row is not
   terminal while its run is still active or not yet written) -> no claim,
   no job, no attempt spent: `next_attempt_at = now + RFP_SPLIT_POLL_SECONDS`,
   a `split.waiting` bench event (`state: sandbox_busy`, `still_checking`,
   `total`). Checked BEFORE the model: the harvest goes `complete` the
   moment its downloads land (`rfp_harvest._complete_harvest`) and the
   verdicts arrive later; a split that stages before them refuses every
   file as unverified (2026-09-21: five verified PDFs skipped as `no_files`
   because the split ran 12 seconds before the first verdict, and a
   168-file harvest staged 22). Once the run itself is terminal every file
   has its answer and the promotion check decides.
1. `rfp_harvests.split_status = none` -> `rfp_split.start(sb, harvest)`:
   - CAS `split_status none -> pending` (claim; a lost CAS means another
     worker started it: fall to step 2).
   - Create `bid_split_jobs {source: 'rfp', rfp_harvest_id, created_by null,
     model: llm.active_model('bid_split')}`.
   - For each harvest entry, run the SAME promotion decision the files job
     uses (`rfp_create_files.promotion_for` plus the byte check of its step 3;
     factor those into a shared helper rather than copying). A `Skip` entry is
     not sent to the splitter (it will be listed skipped at promotion as
     today). A `Promote` entry is copied into `bid-splits/{job_id}/source/`
     and gets a `bid_split_files` row with `rfp_sandbox_file_id`,
     `source_format` and:
       - PDF -> the normal splitter path (one `bid_split` llm job).
       - docx/xlsx/doc/xls -> `classified_from = converted_pdf`: the
         sandbox's converted PDF is the bytes the triage samples; the
         verdict is recorded as one `is_original` segment with the mapped
         category and the file is never cut. The ORIGINAL (or, for doc/xls,
         the converted PDF, exactly as 0130 section 5 decides) is what
         promotion uploads.
       - anything else (images, unknown) -> `classified_from = name`: one
         text LLM call (route through `services/llm.py` under the
         `bid_split` feature) with the filename, the harvest path, the
         invitation subject / project name and the list of allowed
         categories; answer validated against the map's values; failure
         -> `other`. Recorded as one `is_original` segment.
   - Cap: at most `bid_split_max_files_per_job` (250) files; entries beyond
     it are not staged and promote through the pre-split mapping (section 4).
   - `split_status = running`, `split_started_at`, `split_job_id`.
   - Nothing staged (every entry refused by the promotion check): the job
     row is KEPT (`status = failed`, `file_count = 0`, `completed_at`), never
     deleted, `split_status = skipped` with `split_error = no_files` and
     `split_job_id` linked, and the `split.skipped` event carries every
     refusal (`files: [{filename, staged: false, reason}]`). Deleting the
     job hid why nothing was staged (2026-09-21).
2. Job not terminal -> `next_attempt_at = now + rfp_split_poll_seconds`, no
   attempt spent. Return.
2b. Job terminal but some files died of a model outage (their latest
   `llm_jobs` run failed with an outage kind: unreachable, timeout,
   not_configured, out_of_tokens, unauthorized; the queue's own short
   ladder, 10 s to 3 min, runs out well inside a real outage) -> while the
   model is still away, wait as in 0b (`split.waiting`); once it is back,
   those files go `pending` and are queued again, the job returns to
   `processing` through the mark, `split.requeued`. A file is queued at most
   `_OUTAGE_RUNS_MAX` (3) times in all; after that its failure is real
   (a storage fault the queue also grades `unreachable` cannot loop).
3. Job terminal (`done`, `done_with_errors`) -> `split_status = complete`,
   `split_finished_at`, CAS `split -> create`. Per-file failures are
   promoted through the pre-split mapping (section 4).
4. Job `failed` -> `split_status = failed`, `split_error`, CAS `split ->
   create`; the created project gets the "split failed" flag.
5. Any exception -> `_retry_or_fail(step="split")` like the other steps
   (1 min, 5 min, then 15 min). Superseded at the ladder's end by section
   10 (2026-09-30): the 4th failure no longer ends the row `failed` (email)
   or holds it at the cap (portal); the harvest is marked split `failed`
   with the reason and the row moves on to `create`.

Test bench events (section 7) are emitted at start, per file terminal, and
at finish.

### 3.3 Splitter changes (`bid_split.py`, `bid_splitter.py`)

- `bid_split_max_files_per_job` default 250; the list route's limit clamp to
  250; the FE constant to match.
- Non-PDF rows: `refresh_job` counts them like any file; the UI shows them
  as "Identified (not a PDF)" with the category and no page thumbnails.
- Correction routes (`PATCH /files/{id}`, `PUT /files/{id}/segments`,
  `POST /files/{id}/reprocess`): when `bid_split_jobs.project_id` is set,
  check the project's hand-off lock through the files module's existing
  "package sent" predicate. Sent -> 409 with the sentence from section 1.
  Not sent -> apply the correction, then `rfp_split.resync_project_files(
  sb, file_id)`: category-only changes update `project_files.category` on
  rows matched by `bid_split_segment_id`; re-cut segments replace the
  files for that source (delete the old project_files rows and objects
  for that `bid_split_file`, promote the new segments). Audit
  `rfp_split.resync` on the project.
- Deleting a job with `source = 'rfp'` is refused (409) while its project
  exists.
- The FE splitter job page, when `project_id` is set, shows the project
  number as a link and the "package sent" banner when locked.

---

## 4. Promotion (`rfp_create_files.py`, builder A)

`rfp_create.create_from_*` copies `split_status` and `split_job_id` onto
`rfp_created_projects` and sets `bid_split_jobs.project_id`.

The promotion job, per harvest entry:

- Entry has a `bid_split_files` row with status `done`:
  - split into segments (non-original segments exist): upload each segment
    PDF from `bid-splits/{job}/segments/...` (server-side copy or download +
    upload, whichever the storage helper supports) as a `project_files` row
    with the mapped category, `bid_split_segment_id`, filename = segment
    name + `.pdf`; then upload the source once more as `other` with
    `is_source_set = true` and the note "Source set: split into N documents".
  - intact (one `is_original` segment): promote exactly as 0130 section 5
    does today (same bytes, same checks) with the mapped category and
    `bid_split_segment_id`.
  - `addendum` category: parse the number from the segment name
    (`Addendum 3`, `Add. #3`, `ADD-03`, case-insensitive); null when nothing
    parses; issue date null.
- Entry has a `bid_split_files` row with status `failed`, or no row at all
  (flags off, skipped, over cap): today's mapping (Procore kind, else
  `other`).
- Everything else in the job (claims, idempotency on
  `(project_id, rfp_sandbox_file_id)`, skips, counters) is unchanged; the
  idempotency index for segments is `(project_id, bid_split_segment_id)`.
- `files_promoted` counts segments; the card text becomes "N documents from M
  files".

The `rfp_created.py` list/detail payload gains `split_status`,
`split_job_id`, `split_files_total`, `split_files_done`, `split_segments`
and the flag `split_failed`.

---

## 5. Categories everywhere (builder B backend, builder C frontend)

`app/core/file_categories.py` (the single source of truth):

```
DRAWING_CATEGORIES = {"drawing", "civil_drawing", "structural_drawing", "architectural_drawing",
                      "mechanical_drawing", "plumbing_drawing", "electrical_drawing",
                      "fire_protection_drawing", "low_voltage_drawing"}
ESTIMATOR_READ = DRAWING_CATEGORIES | {"specification", "rfp", "other"}
INITIAL_CATEGORIES = ESTIMATOR_READ - {"other"}   (frozen once the hand-off has sent; `other` stays
                                                  uploadable after send because RFQ Modify Files, the GC
                                                  proposal send and Send Out all upload as `other`)
VALID_CATEGORIES += the eight new values
CATEGORY_DISPLAY_ORDER: rfp, drawing, civil, structural, architectural, mechanical, plumbing,
   electrical, fire_protection, low_voltage, specification, addendum, revision, additional,
   ... existing tail unchanged, other last
```

Rows with `is_source_set = true` are excluded from `PACKAGE_CATEGORIES`
queries (hand-off email, Plans & Specs Log, estimator list/export/ZIP) and
from RFQ default attachments. Add the predicate in one place (a helper in
`file_categories.py` or the files service) and use it at every site.

Note `other` joining `ESTIMATOR_READ` changes the estimator read gate for
existing projects: `other` files uploaded by writers become estimator-visible
from the moment they exist. This is the user's decision ("all file types").
`rfq_split` and `quote` stay internal.

Backend consumers to update (B finds every one; the module docstring lists
the historical sites): files router validation and labels, estimator
router/email/rounds, file export ZIP folder names, Plans & Specs Log /
HandoffSummary payloads, `section_key` / `section_notes` keys if a new
category is sendable through the Revisions modal (it is not: new categories
are initial-package only, like `drawing`), RFQ sender (section 5.1),
notification texts that name categories.

### 5.1 Specs on every RFQ (builder B)

`rfq_sending.py`: where default attachments are assembled, add the
project's `specification` files (excluding `is_source_set`) to the same
folder / link the drawings go through, for every category including
Trenching. The email sentence names "drawings and specifications". The
`GET` that feeds the Modify Files modal returns the specs pre-checked
(builder C renders them under a "Specifications" heading). An explicit
`attachment_file_ids` list is still honored verbatim.

### 5.2 Frontend surfaces (builder C)

`lib/fileCategories.ts` is the FE mirror: add the eight categories, labels,
display order, `MAX_FILES_PER_BATCH` to 250 where the batch is the splitter
or the New Bid drop (keep the estimator round cap as is unless it is the same
constant). Every surface that lists or filters categories: New Bid upload and
per-file category modal, FilesPanel, Plans & Specs Log, HandoffSummary,
Revisions modal (initial-only categories are NOT added there), estimator
portal list / buckets / documentation page, bid drafts, ZIP export labels,
bid splitter page (non-PDF rows, project link, sent banner, cap 250),
Created-from-RFPs card (split state, "Open in splitter" link, "N documents
from M files", split failed flag), RFQ Modify Files modal (specs section
pre-checked), RFP testing monitor (split step chip and events), upload size
copy (450 MB). Locales: en plus ceb, fil, hi, sw, ur for every new key.

---

## 6. Corrections after creation

See section 3.3. The project's FilesPanel shows an "Open in splitter" link
when any file carries `bid_split_segment_id`. Category edits made directly on
the project (existing behavior, if any) are not pushed back to the splitter.

---

## 7. Test bench (`rfp_test.py`, `rfp_testing.py`, FE `/rfp-testing`)

`STEPS` gains `split` after `harvest`. `decided_at_step = "split"` maps to
that chip. Events, all carrying `test_session_id` from the row:

```
split.started   {harvest_id, job_id, files}
split.file      {job_id, file_id, filename, status, kind, segments, classified_from}
split.finished  {harvest_id, job_id, status, files_done, files_failed, segments}
split.skipped   {harvest_id, reason: flags_off | queue_off | no_files | linked, job_id?, why?, files?}
                (no_files after staging: the kept job and every refusal reason)
split.waiting   {harvest_id, job_id?, state, why, requeued}   (model away: no attempt spent)
split.waiting   {harvest_id, job_id: null, state: sandbox_busy, why, still_checking, total, sandbox_run_id}
                (sandbox still checking: no claim, no attempt spent)
split.requeued  {harvest_id, job_id, requeued, files}         (model back: outage files queued again)
```

The monitor page renders them like the harvest events. The test cleanup
that deletes a session's rows also deletes its `bid_split_jobs` (source
`rfp`, harvest tagged with the session) and their storage prefix.

---

## 8. Release steps (not now; dev only)

1. Apply 0132 to staging, then prod with approval.
2. Raise the Supabase Dashboard global upload limit to 450 MB on each
   project (manual, Storage settings).
3. Railway: `BID_FILE_SPLITTER_ENABLED=true`, `RFP_SPLIT_ENABLED=true`.
4. Existing projects: no backfill; new categories simply become available.

---

## 9. Build record

(Each builder appends what it built and where it deviated.)

### Builder C (frontend), 2026-09-18

Surfaces changed (all under `bdr_fe/`):

- `lib/fileCategories.ts`: eight new categories in `HANDOFF_CATEGORIES`,
  `INITIAL_UPLOAD_CATEGORIES`, `HANDOFF_GROUPS`, `HANDOFF_GROUP_LABEL_KEYS`,
  `HANDOFF_COUNT_KEYS` and `CATEGORY_DISPLAY_ORDER` (order per section 5, rfp
  first, other last); new `DRAWING_CATEGORIES` + `isDrawingCategory`,
  `isSourceSet`, `UPLOAD_MAX_MB = 450` / `UPLOAD_MAX_BYTES`;
  `MAX_FILES_PER_BATCH` 60 to 250 (it is the hand-off modals' cap, and the
  initial modal seeds every already-uploaded package file as a row, so a split
  set would have blocked it at 60; the Revisions modal shares the constant).
- `lib/types.ts` `ProjectFile`: `bid_split_segment_id`, `bid_split_job_id`,
  `is_source_set`. `lib/bidDrafts.ts`: draft file category is any
  `InitialUploadCategory`.
- `components/FilesPanel.tsx`: the eight categories in the dropdown (and so in
  the per-file folder modal), locked after send like drawing; "Source set"
  badge on `is_source_set` rows; trade-drawing notice; "Open in splitter"
  link (`/bid-splitter?job=`) when any file carries `bid_split_segment_id`,
  internal roles only.
- `components/UploadPackageModal.tsx`: picker offers the new categories; seed
  skips `is_source_set` rows; the drawing gate accepts any drawing set.
  `HandoffSummary.tsx` / `EstimatorPanel.tsx`: same gate.
- `components/PlansSpecsLogModal.tsx`, `HandoffSummary`, estimator portal
  package section and `estimator/sampleScreens.tsx`: new groups/counts via the
  shared constants (no per-file edits needed). Revisions modal untouched.
- `components/NewProjectModal.tsx`: upload cap copy 450 MB; a fifth bucket
  "Other documents" (RFP + the seven trade sets) with a per-row category
  picker, saved to drafts and transferred like addenda; an unpicked row blocks
  Save for Later and Create.
- `components/RFQSendPanel.tsx`: defaults seed from the new
  `GET /projects/{id}/rfqs/default-attachments` (`by_rfq[rfq].file_ids`),
  falling back to the client rule with `specification` added for every RFQ
  (Trenching included); source sets never attachable.
  `ModifySendFilesModal.tsx`: specifications rendered under their own
  "Specifications" heading, pre-checked, removable. `RFQConfirmSendModal.tsx`:
  labels for the new categories.
- `app/(app)/bid-splitter/page.tsx`: non-PDF rows ("Identified (not a PDF)",
  category chip, classified-from hint, no page count, no preview, no split
  editor, no split-choice modal on a kind change); job header shows the RFP
  badge, the project number link and the "package sent" banner; corrections
  hidden while `package_sent`; 409 messages already surface inline; History
  gains a Project column and the delete confirm refuses an rfp job with a live
  project up front (server 409 text still shown otherwise). Cap 250 comes from
  `/bid-splitter/status`, no FE constant.
- `lib/rfpCreated.ts` + `app/(app)/rfp-created/page.tsx`: `split_*` read from
  `flags` (or the row); "Splitting documents" spinner badge with "n of m files
  split" title, "Split failed" badge, "N documents from M files" line under the
  flags, "Open in splitter" action.
- `lib/rfpTesting.ts`, `app/(app)/rfp-testing/{shared,tabs,EventDetail}.tsx`:
  `split` step + source; the row line shows `split_status` with an "Open in
  splitter" link; `split.started/finished/skipped` and `split.file` render
  like the harvest events (facts + raw JSON).
- Locales: every new key in en, ceb, fil, hi, sw, ur (group/count nouns follow
  the catalogs' existing English-noun convention next to their neighbours;
  category labels and sentences translated); "300 MB" copy is now "450 MB".
  `bidSplitter.*` had no non-en namespace before; the new keys create one.

Backend fields the FE reads that must exist:

- `GET /projects/{id}/files` rows: `bid_split_job_id` (the "Open in splitter"
  link needs the JOB id; `rfp_split.py` writes `bid_split_segment_id` and
  `bid_split_file_id` only). Without it the link never renders.
- `GET /bid-splitter/jobs/{id}` and the `/bid-splitter/jobs` rows: `source`,
  `project_id`, `project_number`, `package_sent` (boolean, the hand-off lock).
  `bid_splitter.py` does not expose these yet (builder A). Without them the
  page degrades to today's view (no link, no banner, editors stay on and the
  409 text shows inline).
- `bid_split_files` rows on the job payload: `source_format`,
  `classified_from`, nullable `page_count` (per section 2).
- RFP test email rows: `split_status`, `split_job_id` (present in
  `rfp_testing.py`).

Deviations: `other` is not in `HANDOFF_CATEGORIES` / `INITIAL_UPLOAD_CATEGORIES`
(mirrors builder B's `INITIAL_CATEGORIES = ESTIMATOR_READ - {"other"}`);
manual splitter jobs still accept PDFs only (non-PDF classification is the
pipeline's path).

### Builder B (backend: categories, specs on RFQs, size limits), 2026-09-18

Files changed

* `app/core/file_categories.py` - the eight new categories, `DRAWING_CATEGORIES`
  (all nine drawing sets), `ESTIMATOR_READ = DRAWING_CATEGORIES | {specification,
  rfp, other}`, `VALID_CATEGORIES`, `CATEGORY_DISPLAY_ORDER` (rfp first, other
  last), a new `CATEGORY_LABELS` + `category_label()` + `category_rank()`, and
  the source-set predicate `exclude_source_set(q)` / `is_source_set(row)`.
  Docstring now lists every consumer site.
* `app/routers/files.py` - upload validation and the delete gate follow the sets;
  the drawing-changed notification fires for every drawing set and names it from
  `CATEGORY_LABELS`; `_estimator_visible` refuses a source set outright; the
  estimator list and export queries add `exclude_source_set` (internal callers
  keep seeing source sets). Manual uploads still require an addendum number and
  issue date; only the splitter's promotion path may leave them null.
* `app/routers/estimator.py` - `DRAWING_CATEGORIES` now imported, not
  re-declared; `_package_files` reads `PACKAGE_CATEGORIES` and excludes source
  sets; `NO_DRAWING_MESSAGE` is "Upload at least one drawing/plan first".
* `app/routers/workflow.py` - the intake drawing gate takes any drawing set.
* `app/routers/bid_drafts.py` - docstring only (`DRAFT_FILE_CATEGORIES` is
  derived from `INITIAL_CATEGORIES`, so the new categories arrive for free).
* `app/routers/rfqs.py` - new `GET /projects/{id}/rfqs/default-attachments`.
* `app/services/estimator_email.py` - `SECTION_TITLES`, `_INITIAL` and
  `_CONTENTS_LABELS` gained the eight categories (literals, as that module is a
  leaf; a test pins them to `file_categories`).
* `app/services/file_sends.py` - the staged counts exclude source sets.
* `app/services/file_export.py` - dropped its duplicated `_CATEGORY_ORDER` and
  imports `category_rank`. ZIP folder names stay the raw category value.
* `app/services/office_preview.py` - no PDF derivative for any drawing set.
* `app/services/pm_folders.py` - the seven trade sets map to `plans`, `rfp` to
  `specifications` (the hub has no bid-documents folder).
* `app/services/rfq_sending.py` - see below.
* `app/core/config.py`, `app/core/supabase_client.py` (comment), `.env.example`.
* `app/models/schemas.py` - comment, and `attachment_file_ids` max_length 50 to
  200 (the default set now includes a whole spec book, and the modal posts the
  full list back).
* Tests: `tests/test_file_updates.py` (+11), `tests/test_rfq_sending.py` (+7),
  `tests/test_feature_flags.py`, `tests/test_rfp_create_files.py` (the too-large
  fixture reads `upload_max_bytes` instead of hard-coding 300 MB).

Labels chosen for the eight categories (FE must match, `CATEGORY_LABELS`)

| category | label |
|---|---|
| `rfp` | RFP / bid documents |
| `civil_drawing` | Civil drawings |
| `structural_drawing` | Structural drawings |
| `architectural_drawing` | Architectural drawings |
| `mechanical_drawing` | Mechanical drawings |
| `plumbing_drawing` | Plumbing drawings |
| `fire_protection_drawing` | Fire protection drawings |
| `low_voltage_drawing` | Low voltage drawings |

Unchanged for reference: `drawing` = "General drawings/plans",
`electrical_drawing` = "Electrical drawings", `specification` =
"Specifications", `other` = "Other".

Specs on every RFQ (section 5.1)

`_prepare_drawings(sb, project)` now returns the drawings (Electrical set when
one exists, else General) PLUS every `specification` file, through the same
size check and the same single OneDrive folder link, for every category
including Trenching. Both are read through `_files_query`, which excludes
unsent estimator drafts and `is_source_set` rows. The link sentence is
`DOCUMENTS_LINK_SENTENCE` = "The drawings and specifications are available
here:" and the review line reads "If there are any other attachments, drawings
or specifications, please review them as well." An explicit
`attachment_file_ids` list is still sent verbatim; nudges and re-sends are
untouched.

Modify Files payload (builder C): `GET /projects/{project_id}/rfqs/default-attachments`

```
{
  "drawings_category": "electrical_drawing" | "drawing" | null,
  "by_rfq": {
    "<rfq_id>": {
      "trenching": true|false,
      "file_ids": ["<id>", ...],
      "sections": [
        {"key": "counts",         "label": "BOM split",           "file_ids": [...]},
        {"key": "drawings",       "label": "Electrical drawings", "file_ids": [...]},
        {"key": "specifications", "label": "Specifications",      "file_ids": [...]},
        {"key": "markup",         "label": "Trench markup",       "file_ids": [...]}
      ]
    }
  }
}
```

Every id in the payload is a default, so the modal renders it CHECKED. Empty
sections are omitted (no counts section for Trenching, no markup section for
anything else). `file_ids` is the sections concatenated in send order, so the
modal can seed its selection from that one key and still draw a heading per
section; the `drawings` label is whichever bucket was chosen. File metadata
(name, size) still comes from `GET /projects/{id}/files`, keyed by id. Internal
roles only. This endpoint exists so the modal stops recomputing the default
rule client side; the send path and the modal now read the same function
(`rfq_sending.default_attachments`).

Size limits

`upload_max_bytes` 300 to 450 MB, `max_request_body_bytes` 310 to 460 MB (the
same 10 MB multipart headroom), `rfp_ingest_max_file_bytes` 300 to 450 MB (its
"must not exceed upload_max_bytes" validator still holds),
`rfq_attachments_total_limit_mb` 300 to 450 (it is documented as matching the
per-file cap). `.env.example` samples updated.

Other 300 MB mentions found by grep. Not edited, listed for their owners:

* `app/routers/bid_splitter.py:171` - docstring says "(max_request_body_bytes,
  310 MB) is per REQUEST while the 300 MB upload ..." (builder A owns
  `bid_split*.py`).
* `docs/RFP_INGESTION_SANDBOX.md` lines 620, 654, 1054, 1279 - the sandbox doc's
  bucket limit / settings table still say 300 MB.
* `app/services/rfp_ingest_storage.py:110` and `app/services/rfp_image_pdf.py:11`
  - comments only, unrelated arithmetic.

Frontend, for builder C (not edited by me):

* `bdr_fe/locales/*/translation.json` - the estimator documentation string "300
  MB per file, and around 20 uploads a minute..." (en line 2051, and the ceb,
  fil, hi, sw, ur copies) becomes 450 MB.
* `bdr_fe/lib/folderDrop.ts:37` - `MAX_DROPPED_FILES = 300` is a file-count cap,
  not a size; section 5.2 raises the splitter/New Bid batch to 250, so decide
  deliberately whether this one moves.
* The "Upload a General or Electrical drawing first" copy:
  `bdr_fe/locales/en/translation.json` 1638 and 1640, plus the mirroring
  comments in `UploadPackageModal.tsx:612`, `HandoffSummary.tsx:208` and
  `EstimatorPanel.tsx:99`. The backend messages are now "Upload at least one
  drawing/plan first" and "Upload at least one drawing/plan before completing
  Intake", and the gate accepts any of the nine drawing sets.

Deviations

1. `INITIAL_CATEGORIES = ESTIMATOR_READ - {"other"}`, not `= ESTIMATOR_READ`.
   `other` is the category three POST-hand-off flows upload into (the RFQ
   Modify Files modal, the GC proposal send modal and Send Out all POST
   `category=other`); putting it in `INITIAL_CATEGORIES` would freeze it behind
   the hand-off lock and 409 every one of them on any project whose package had
   been sent. `other` is estimator-READABLE the moment it exists, as decided,
   but it is not a frozen package block and so does not appear in the hand-off
   email or the Plans & Specs Log. Pinned by
   `test_other_is_estimator_readable_but_not_a_frozen_package_block`.
2. The RFQ default drawing set is still Electrical-with-General-fallback. The
   seven new trade sets are NOT default vendor attachments: an electrical vendor
   prices from the electrical sheets, and a PE who wants the civil set on a
   particular RFQ adds it in Modify Files. Section 5.1 only asked for specs.
3. The Modify Files data comes from a NEW endpoint rather than a changed
   response on an existing one: nothing existed to change (the modal computed
   the default set in `RFQSendPanel.tsx` from `GET .../files`).
4. `attachment_file_ids` max_length raised 50 to 200 (see above).
5. `app/services/pm_folders.py` and the internal (non-estimator) ZIP export
   still include source sets. They are internal-only surfaces, and the contract
   scopes the exclusion to estimator/package/RFQ queries.

Tests: `cd bdr_be && python -m pytest tests -q`. With builder A's in-flight
`split` status work on disk the whole-suite run is 54 failed / 4542 passed / 1
skipped (the failure count moved as A kept editing); EVERY failure is in
`tests/test_rfp_*` (A's ladder, 0130/0132 migration text, created-router
payload, test-bench cleanup), including
`test_rfp_emails_router.py::test_match_stats_answers_the_services_tally`, which
fails only because the local `.env` sets `RFP_MATCH_AUTO_MERGE_ENABLED=true`
(it passes with that unset). Excluding the eight `tests/test_rfp_*` modules
A owns: 3995 passed, 1 skipped, 0 failed. `ruff check app tests` reports the
same 5 pre-existing findings as before this work, none in a file I touched.

### Builder A (backend: sections 2, 3, 4, 7), 2026-09-18

**Built.** Migration `supabase/migrations/0132_rfp_split.sql` (APPLIED to
the dev project "BDR" bpidntbyvoooqvaispup as two migrations,
`0132_rfp_split_enum` then `0132_rfp_split`, because the MCP apply runs
one transaction and PG refuses to use a label added in the same
transaction; the file is the single record and re-runs cleanly). New
service `app/services/rfp_split.py`. The `split` step in
`app/services/rfp_email_ingest.py` (`_step_split`) and
`app/services/rfp_portal_ingest.py` (`_step_split`, `_retry_or_hold_at`,
`STATUS_SPLIT`, pending / ignorable / "new" view). `rfp_harvest._finish_email`
and `rfp_portal_ingest._finish_invitation` now target `split`. Promotion in
`app/services/rfp_create_files.py` (split-aware loop, public `fetch_entry`,
`fetch_verified`, `file_rows`; the pre-split row now writes the three new
columns explicitly). `app/services/rfp_create.py` copies `split_status` /
`split_job_id` onto `rfp_created_projects`, sets `bid_split_jobs.project_id`
(step 7), and `MATE_STATUSES` links a mate waiting at `split`.
`app/routers/rfp_created.py` payload fields. `app/services/bid_split.py`
non-PDF path (`_execute_non_pdf`, `_identify_converted`,
`_identify_by_name`, `_NAME_CLASSIFY_*`) and the `_after_done` resync hook.
`app/routers/bid_splitter.py`: `_project_guard`, `_resync`, `_is_pdf_row`,
`_attach_projects`, cap 250 on the list clamp, delete refusal, non-PDF
handling in the three correction routes, `_FILE_COLUMNS` gains
`rfp_sandbox_file_id, source_format, classified_from`. `app/core/config.py`:
`rfp_split_enabled` (False), `rfp_split_poll_seconds` (30, validated >= 5),
`bid_split_max_files_per_job` 250, `rfp_harvest_max_files` 250,
`rfp_ingest_max_files_per_run` 250. `app/services/rfp_test.py`: `SOURCE_SPLIT`,
`STEPS` gains `split`, `step_chips` (skipped on merged / duplicate, no
harvest, or `split_status` skipped), cleanup step 3a deletes the session's
`source = 'rfp'` jobs and their storage prefix (`deleted.split_jobs`).
`app/routers/rfp_testing.py`: rows carry `split_status`, `split_job_id`,
events of source `split` feed the chip. `app/routers/rfp_emails.py`: the
"processed" tab lists `split`. `app/routers/files.py`: list rows carry
`bid_split_job_id` (resolved through `bid_split_files` in one query; the
coordinator's request). Local `.env`: `RFP_SPLIT_ENABLED=true`. Tests:
`tests/test_rfp_split.py` (48) plus the vocabulary / expectation updates in
the existing rfp, splitter and feature-flag tests.

**API shapes B and C rely on.**

- `GET /rfp-created` items, under `flags`: `split_status` (`complete` |
  `failed` | `skipped` | null for pre-0132 records; `pending` / `running`
  never reach the page because the project is created after the step),
  `split_job_id`, `split_files_total`, `split_files_done`, `split_segments`,
  `split_failed` (bool). `files_promoted` counts DOCUMENTS (segments and
  intact files; the source set is not one); "N documents from M files" =
  `files_promoted` from `split_files_done`.
- `GET /bid-splitter/jobs` rows and `GET /bid-splitter/jobs/{id}`: `source`
  (`manual` | `rfp`), `project_id`, `project_number`, `project_name`,
  `package_sent` (bool; the hand-off lock). Job file rows: `source_format`
  (`pdf` | `docx` | `xlsx` | `doc` | `xls` | `image` | `other`, null on
  manual rows), `classified_from` (`pages` | `converted_pdf` | `name`, null
  on manual rows = pages), nullable `page_count` (null on non-PDF rows). A
  non-PDF row's one segment is `is_original` with `page_start = 1` and
  `page_end` = the converted PDF's pages, or 1 when only the name was read.
  `GET /bid-splitter/files/{id}/download` on a `converted_pdf` row hands
  back the converted PDF named `<stem>.pdf`.
- The correction routes (`PATCH /files/{id}`, `PUT /files/{id}/segments`,
  `POST /files/{id}/reprocess`) answer 409 with exactly
  `This project's package has already been sent; correct the files on the project.`
  when the job's project has sent; PATCH and PUT responses on an unsent
  project carry `project_resync`: `{project_id, ok, documents, inserted,
  replaced, skipped}` or `{project_id, ok: false, error}`; null on a manual
  job. A reprocess re-files when its run ends (server side). Non-PDF rows
  answer 409 `This file is not a PDF; it was identified, not split, and cannot be re-cut.`
  on PUT, reprocess and a PATCH to a split kind; a PATCH to an intact kind
  re-identifies them. `DELETE /jobs/{id}` on an `rfp` job whose project
  exists: 409 `This split was staged from an RFP invitation and its project still exists; discard the project first.`
- `GET /projects/{id}/files` rows: `bid_split_segment_id`, `bid_split_file_id`,
  `is_source_set` (columns) and `bid_split_job_id` (resolved) for the
  "Open in splitter" link (`/bid-splitter?job=<bid_split_job_id>`).
- Test bench events (source `split`, all with `harvest_id` and the email's
  `rfp_email_id`): `split.started {harvest_id, job_id, files: [{filename,
  staged, file_id?, classified_from?, source_format?, reason?, error?}],
  over_cap}`, `split.file {job_id, file_id, filename, status, kind, segments,
  classified_from, source_format, error}` (one per file, emitted when the
  job settles), `split.finished {harvest_id, job_id, status, files_done,
  files_failed, segments}`, `split.skipped {harvest_id, reason: flags_off |
  no_files | linked | queue_off}`. `GET /rfp-testing/sessions/{id}/emails`
  rows gain `split_status` and `split_job_id`; the step chip list has 11
  entries with `split` after `harvest`.

**Deviations, recorded.**

1. `project_files.bid_split_file_id` was added beside `bid_split_segment_id`
   (not in section 2): the segment FK is `on delete set null`, so after a
   re-cut the old rows could not be found by segment; the file id is what
   a resync deletes and re-files by. The source set carries the file id and
   the sandbox id, never a segment id.
2. `promote_split_file` reconciles rather than "delete then promote": a
   category-only change updates the row in place, a segment that still
   exists keeps its object, and an intact row that gets cut becomes the
   source set in place (no second upload of the original). Segments are
   copied server-side (`storage.copy_file`) from `bid-splits/`.
3. Entries beyond the per-job cap are promoted through the pre-split
   mapping, not marked `skipped_cap` (section 3.2 said skipped_cap; section
   4 says pre-split mapping; the harvest caps are also 250, so it is moot).
4. `split.skipped` has a fourth reason, `queue_off`: the splitter runs only
   through the llm_jobs queue; with the queue off the step falls through
   like the flags off.
5. Per-file `split.file` events are emitted when the job settles (from the
   sweep), not from the worker, so the worker stays free of the test bench.
6. A PDF that fails the page-count / page-cap check at staging is inserted
   as a `failed` row with the sentence and never queued (promotion falls
   back to the pre-split mapping for it).
7. `rfp_test.step_chips` marks `split` skipped for `created` / `done` rows
   whose harvest reports `split_status = skipped` (the testing router passes
   it in) so the chip strip does not show a step that never ran as done.
8. `resync_after_run` (worker side) checks the lock itself and skips a sent
   project; the router paths check it before the correction.

**Not done / open.** The dev server was not restarted or live-driven in this
build (a `next dev` and a uvicorn may be running); the step has not been
exercised against a real harvest yet. `tests/test_rfp_emails_router.py::
test_match_stats_answers_the_services_tally` fails locally before and after
this build because the local `.env` sets `RFP_MATCH_AUTO_MERGE_ENABLED=true`
(not pinned in conftest); unrelated to this slice.

### Review 2026-09-18 (review-and-fix pass over A, B and C)

Scope: sections 1 to 7 against the code on disk (no git; the section 9 file
lists plus grep). Priorities: pipeline safety, sync SDK inside `async def`,
storage leaks, role gates and project scoping, FE/BE shape agreement, the
recorded deviations against section 1, em dashes.

Findings fixed

1. HIGH, `app/services/rfp_split.py` `_start`: a staging exception part way
   through the loop (storage or the sandbox bucket dying on entry N) left the
   `bid_split_jobs` row at `processing` with `file_count = 0` and the
   already-staged objects under `bid-splits/{job}/source/` behind it, while
   `advance` put the harvest claim back to `none` and the next pass staged a
   SECOND job. The orphan never settled (nothing queued for it), so the app
   shell's `?status=processing` in-flight marker stayed lit forever and the
   objects leaked. Fix: `_discard_job` (best effort: `delete_bid_split_prefix`
   then the job row, files cascading) on the exception path before re-raising;
   the caller's ladder is unchanged. Test
   `test_start_discards_the_job_when_staging_throws`.
2. HIGH, `app/services/rfp_split.py` `advance`: a `pending` claim was waited
   on forever. A worker killed between the `none -> pending` CAS and
   `running` (a deploy mid-staging) stranded every row behind that harvest at
   `split`, polling every 30 s with no way out (`dismiss` refuses `split`;
   the email side has no release action). Fix: the claim stamps
   `split_started_at`; `pending_is_stale` (older than
   `_PENDING_STALE_SECONDS` = 3600, or no stamp; since 2026-09-30 the
   heartbeat window RFP_SPLIT_STAGING_STALE_SECONDS = 300, section 10.5)
   CASes `pending -> none` with
   `split_error` "The staging claim expired; the split is being started
   again." and starts again on the same pass. A fresh claim still waits. Test
   `test_advance_reclaims_a_stale_staging_claim`.
3. LOW, `tests/test_rfp_split.py`: the migration test carried the em dash and
   en dash as literals; now the backslash-u escapes 2014 and 2013.
4. LOW, em dashes: none in this feature's added text. The touched files
   carried 512 pre-existing em-dash lines (comments, docstrings, two estimator
   email sentences, the "number - name" label, a few "-" placeholders in
   `sampleScreens.tsx` / `RFQSendPanel.tsx` / `RFQConfirmSendModal.tsx`);
   replaced with hyphens in every touched backend `.py` and FE `.ts` / `.tsx`
   file from section 9. One of those is user-visible: the estimator email's
   addenda section titles are now "Addenda - plans/drawings" and "Addenda -
   specifications" (`estimator_email.SECTION_TITLES`;
   `tests/test_file_doc_type.py` pins the new text). The older
   `test_advance_claims_stages_and_waits_then_completes` fixture for "another
   worker holds the claim" now carries a fresh stamp, since an unstamped
   `pending` is exactly what finding 2 reclaims. Left: the six locale
   catalogs (en 228, ceb / fil / hi
   / sw / ur 50 each, all pre-existing user copy outside this feature; a
   catalog-wide sweep is its own task) and untouched files elsewhere in the
   repo.

Verified, no change needed

- `split` step, both ingest modules: `flags off / queue off / no files /
  linked` fall through `split -> create` in the same pass; a wait pushes
  `next_attempt_at` by `rfp_split_poll_seconds` and spends no attempt; a
  `failed` / missing job still CASes to `create` (the project is created
  flagged "split failed"); exceptions go through `_retry_or_fail(step="split")`
  (email) and `_retry_or_hold_at` (portal) exactly like the neighbouring
  steps. `rfp_harvest._finish_email` and `_finish_invitation` land at
  `split`; the harvest step's own `finish()` (no harvester / no reference)
  still jumps to `create`, which is right: there is nothing to split.
  `MATE_STATUSES` links a mate waiting at `split`.
- Promotion (`rfp_create_files.py` + `promote_split_file`): segment rows carry
  no sandbox id (so `(project_id, rfp_sandbox_file_id)` still holds for the
  one source-set / intact row), the segment index `(project_id,
  bid_split_segment_id)` is read as "already there" on a race, the source set
  is `other` + `is_source_set` + the note, addendum numbers parse from the
  segment name with a null issue date, `failed` / unstaged / over-cap entries
  take the pre-split mapping, `files_promoted` = documents (source set
  excluded), the split rows' `files_promoted` progress survives a crash.
  Storage objects are deleted on every insert failure path.
- Corrections: `_project_guard` reads the project only from the job row and
  `resync_project_files` writes only to `job.project_id` (never a
  caller-supplied project); the 409 sentence matches section 1; the delete
  refusal covers `source = rfp` with a live project (a discarded project nulls
  the FK, so the job becomes deletable). Non-PDF rows: `_identify_converted`
  goes through the existing `_validate_triage`, `_identify_by_name` through
  `validate_name_category` with `other` on any failure; both are
  `is_original` and never re-cut (409 on PUT / reprocess / PATCH to a split
  kind).
- Sync SDK inside `async def`: none added. The three pre-existing async
  routes in `bid_splitter.py` and `files.py` keep their `run_in_threadpool`
  pattern; every new helper (`_attach_projects`, `_resync`, `_project_guard`,
  `rfp_split.*`, `default_attachments`) is sync and called from sync routes,
  the sweep thread or the queue worker.
- Role gates: `GET /projects/{id}/rfqs/default-attachments` uses the router's
  `_internal` gate like `list_rfqs`; the splitter payload fields ride the
  existing `require_internal` / `require_writer` routes; `handoff_locked` is
  read per distinct project.
- FE/BE shapes agree: `flags.split_status / split_job_id / split_files_total /
  split_files_done / split_segments / split_failed` (rfp_created.py `_item`
  and `lib/rfpCreated.ts`); job `source / project_id / project_number /
  package_sent` (`_attach_projects` and the splitter page); file rows
  `source_format / classified_from` (`_FILE_COLUMNS`); project files
  `bid_split_job_id / is_source_set` (files.py list and `FilesPanel`);
  `by_rfq[rfq].file_ids` (`RFQSendPanel`; the modal groups specifications by
  category from the files list, so `sections` is informational). Category
  labels match `CATEGORY_LABELS`. Test-bench rows carry `split_status /
  split_job_id` and 11 step chips.
- Test bench: `split.started / file / finished / skipped` all carry
  `harvest_id` and the email id; cleanup step 3a deletes the session's
  `source = rfp` jobs and their prefix before the harvests (the FK from
  `rfp_harvests.split_job_id` is `set null`, projects go first so
  `project_files` never dangle).

Deviations judged against section 1

- Left as built (deliberate, recorded by B / A): `INITIAL_CATEGORIES =
  ESTIMATOR_READ - {other}` (freezing `other` would 409 the three post-hand-off
  flows that upload into it; `other` is still estimator-readable the moment it
  exists, as decided); the RFQ default drawing set stays Electrical-with-General
  fallback (section 5.1 only added specifications); entries beyond the per-job
  cap promote through the pre-split mapping (the harvest caps are 250 too);
  `queue_off` as a fourth skip reason; `bid_split_file_id` beside the segment
  id; reconcile-in-place instead of delete-then-promote.
- Noted, not a violation: the `name` classifier path is reachable only when
  an office file's converted PDF cannot be fetched, because images and unknown
  formats never pass the sandbox and so are never promoted (section 1 "the
  original bytes are what gets promoted" and RFP_CREATE.md 5 agree); manual
  splitter jobs stay PDF-only (C's record).
- Noted: after `rfp_email_ingestion_classify_max_attempts` repeated staging
  exceptions the email row ends `failed` with `split_error`, per section 3.2
  step 5 (the same ladder the harvest step uses), not "created flagged split
  failed"; that flag is for a split job that ran and failed.

Runs after the fixes: `cd bdr_be && python -m pytest tests -q` = 4645 passed,
1 skipped, 1 failed (the known
`test_match_stats_answers_the_services_tally`, local `.env`); `ruff check app
tests` = the same 5 pre-existing findings; `cd bdr_fe && npx tsc --noEmit`
clean; `npx eslint .` = 0 errors, the 2 pre-existing warnings
(`NotificationPrefsSection.tsx`, `Sidebar.tsx`).

### Live drive 2026-09-18 (dev :5051, DEV Supabase "BDR", self-hosted qwen-3.5-4b)

Row driven: rfp_emails `98bbdbb4-4408-4e4c-9432-f84ee22dcc0b` ("INVITATION TO
BID - Kane County Office Renovations", pipelinesuite, SHF Contracting), harvest
`f0dee42e-7d1c-4620-8c15-7fd8c44a1d78` (complete, no project, 8 entries over 6
verified sandbox files: two multi-page drawing sets, one 2-page PDF, two docx,
one xlsx; 59 pages). The row was moved `done -> split` by SQL with the
harvest's `split_status` reset to `none`, and `RFP_CREATE_AUTO_ENABLED=true`
was added to the local `.env` for the run only (removed afterwards, server
restarted; the flags are as before except `RFP_SPLIT_ENABLED=true`).

Gotcha: the intake sweep never picked the row up because the test bench
session `73bce002` (started 2026-09-17) is still `active`, and in test mode
the sweep touches only rows tagged with that session (RFP_TESTING.md 4).
Rather than end the user's session, the row was advanced by calling
`rfp_email_ingest._process_email(sb, row)` every 20 s from a script in the
venv (the same step code the sweep runs); the split job itself ran in the
server's llm_jobs worker, which the session does not gate.

Outcome, per stage:

- `split`: claim `none -> pending` 17:20:12Z, 6 files staged (the two
  duplicate entries collapsed, see fix 1) into job
  `92252013-cdac-4992-a53d-61850bea0b57` (`source = rfp`, model
  `self-hosted:qwen-3.5-4b`), `running` at 17:20:30Z (18 s of staging: 6
  downloads from `rfp-quarantine` / `rfp-derived`, 6 uploads under
  `bid-splits/{job}/source/`, 6 llm_jobs). Job `done` 17:22:44Z: 2 min 14 s
  for 6 files / 59 pages. Per file: 30-page set `mixed`, 7 segments, 7 LLM
  calls, 108 s; 14-page set `mixed`, 5 segments, 4 calls, 57 s (one
  "unusable batch reply, retrying once"); xlsx `other` via `converted_pdf`
  (19 s); 2-page PDF `addendum` (10 s); docx `addendum` via `converted_pdf`
  (8 s); 10-page docx `rfp` via `converted_pdf` (16 s). Harvest
  `split_status = complete` 17:22:54Z, row CASed `split -> create` in the
  same pass. No test-bench events (the row carries no session).
- `create`: project `26.9.7124` "Kane County Office Renovations" at
  `go_no_go`, `automatic`, actual bid date carried, no GC (`no_gc` flag),
  12-item intake list; row `created` 17:22:56Z; bells `rfp_create.created`
  and `rfp_create.intake_needed`. `rfp_created_projects.split_status =
  complete`, `split_job_id` set, `bid_split_jobs.project_id` set.
- `rfp_create_files`: complete 17:23:34Z (about 40 s), `files_promoted = 16`
  documents from 6 files, 0 skipped: 12 segment rows (1 drawing + 1
  addendum + 5 trade sets from the 30-page set; 1 drawing + 1 addendum + 3
  trade sets from the 14-page set) copied server-side, 4 intact rows
  (xlsx `other`, PDF `addendum` #1, docx `addendum` #1, docx `rfp`), plus 2
  `other` / `is_source_set` rows with "Source set: split into 7 documents"
  and "... 5 documents". Segment rows carry `bid_split_segment_id` +
  `bid_split_file_id` and no sandbox id; intact and source-set rows carry
  the sandbox id; the source sets carry no segment id. Addendum numbers:
  "1" where the segment name carried one ("Addendum No. 1"), null for
  "Addendum Index"; issue dates null. 18 rows in all.
- `GET /rfp-created` flags: `split_status complete`, `split_job_id`,
  `split_files_total 6`, `split_files_done 6`, `split_segments 16`,
  `split_failed false`, `files_promoted 16`, `documents_skipped 0`.
  `GET /projects/{id}/files`: every row carries `bid_split_job_id`,
  `bid_split_segment_id` / `bid_split_file_id`, `is_source_set`.
  `GET /bid-splitter/jobs/{id}` and the list: `source rfp`, `project_id`,
  `project_number 26.9.7124`, `project_name`, `package_sent false`; the
  three non-PDF rows show `classified_from converted_pdf`, their
  `source_format`, null `page_count`, one `is_original` segment
  (`page_end` = the converted PDF's pages: 1, 2, 10).
- Corrections (E2E executive account): `PATCH /files/{intact PDF}`
  `addendum -> specifications` answered 200 with `project_resync {ok,
  documents 1, inserted 0, replaced 0}` and the project row changed
  category in place (addendum number cleared); `PUT /files/{14-page
  set}/segments` with page 7 moved `addenda -> general_drawings` answered
  200 with `{documents 5, inserted 5, replaced 5}`: the five old rows and
  objects went, five new segment rows came, the source set stayed with its
  note, 0 dangling rows; both audited as `rfp_split.resync` on the project.
  Both were then restored the same way. Guards: `POST .../reprocess` on the
  xlsx row 409 "This file is not a PDF ..."; `DELETE /jobs/{id}` 409 "...
  its project still exists; discard the project first." The package-sent
  409 was not driven live (unit-tested; sending the hand-off would email
  estimators).

Fixed from the run:

1. `app/services/rfp_split.py` `_entries` and
   `app/services/rfp_create_files.py` promotion loop: a portal that lists
   the same bytes twice (PipelineSuite shows a file at the root and again
   under "Addendum 01/") harvests two entries sharing one sandbox id (the
   harvester's byte reuse); the split step staged and cut the same drawing
   set twice, `split_rows_for_job` kept only one of the two rows, and the
   split branch of the promotion loop (which runs before the `done_ids`
   check) would have promoted and counted it twice. Both loops now keep the
   first entry per sandbox id. Tests
   `test_entries_keep_one_entry_per_sandbox_file` and the duplicate-entry
   tail of `test_promotion_job_routes_done_split_rows_and_falls_back_for_the_rest`.
   Two harvests on dev carry such duplicates (both pipelinesuite).
2. `app/services/rfp_split.py` `category_fields(category, segment,
   filename=None)`: a kind correction back to `addendum` collapses the
   intact segment to the fallback name "Addenda", so the number parsed at
   promotion ("1" from "...Addendum No. 1...") was lost on the round trip.
   The number now falls back to the file's own name (`_upload_original`
   passes the decision filename, the intact-update path the row's
   filename); segment rows are unchanged. Verified live: "1" again after
   `specifications -> addendum`. Test in
   `test_category_fields_carry_the_addendum_number_and_nothing_else`.

Noted, not changed: a category-only correction keeps the row's storage
object under its original category prefix (`.../addendum/...` for a row now
filed `specification`; the category column is what every consumer reads);
the splitter's fallback segment name "General / Cover Sheets" carries a
slash into `project_files.filename` (the object key is sanitized); a PUT
that leaves ranges untouched still gets new segment ids from the re-cut, so
the resync replaces every row of that file (correct, just not minimal);
the split test-bench events were not exercised (no session on the row).

Runs: `tests/test_rfp_split.py` 51 passed; `tests/test_rfp_create_files.py
tests/test_rfp_created_router.py tests/test_bid_splitter.py
tests/test_rfp_email_ingest.py` 207 passed; ruff clean on the touched files.

### Split failure fallback + SSL retry, 2026-09-30 (section 10)

Backend: `app/services/storage.py` (`upload_file` / `copy_file` retry,
`_is_duplicate`), `app/services/rfp_split.py` (`_start` queues after the
loop and takes `project_id` / `created_by`; `failure_reason`; `give_up`;
`sync_record`; `settle_linked_job`; `split_issue`; `issues_for_records`;
`creation_note`; `RunRefused`; `start_manual_run`; `stage_for_project`),
`app/services/bid_split.py` (`refresh_job` -> `_after_job_settled`; a note on
`cut_segment`'s 3 x 3 tries), `app/services/rfp_email_ingest.py`
(`_split_ladder_spent`, `_split_gave_up`), `app/services/rfp_portal_ingest.py`
(same pair), `app/services/rfp_create.py` (`_notify` split note),
`app/routers/rfp_created.py` (flags, three routes, `issue_for_project`),
`app/routers/projects.py` + `app/models/schemas.py` (`split_issue` on the
detail route). Migration `supabase/migrations/0143_rfp_split_resolution.sql`
(applied to the dev project "BDR" bpidntbyvoooqvaispup only). Tests:
`tests/test_rfp_split_fallback.py` (22) plus the route-table and payload
expectations in `tests/test_rfp_created_router.py` and
`tests/test_projects_router_numbers.py`.

Frontend: `components/RfpSplitIssueCallout.tsx` (banner + `rfpSplitIssueText`),
`app/(app)/projects/[id]/page.tsx` (rendered under the files-needed callout),
`app/(app)/rfp-created/page.tsx` (badges, the sentence, Run / Split outside /
Undo in the row), `lib/rfpCreated.ts` (`RfpSplitIssue`, `runRfpSplit`,
`markRfpSplitOutside`, `undoRfpSplitOutside`, `rfpSplitIssueOpen`),
`app/(app)/rfp-testing/EventDetail.tsx` (`split.gave_up`). Locales:
`rfpCreated.split.*` and `rfpTesting.detail.split.gaveUp`, English in all
six catalogs.

Decided here: no IT Admin failure alert at the fallback (nothing failed; the
project flag and the creation bells say it). The manual run uses FastAPI
BackgroundTasks, not the llm queue (no new job type; the splitter router
already falls back to BackgroundTasks). A harvest at `running` when the ladder
runs out is not marked failed (its job keeps going and re-files the project).
Page-cap failures are not re-queued by "Run the splitter" (they would fail the
same way).

Known limits: a server restart during a background staging leaves the harvest
`pending` (the flag reads "interrupted" after `_PENDING_STALE_SECONDS`, one
hour, and the run can be pressed again) and the half-staged job at
`processing` with nothing queued (the same exposure the pipeline's own
staging has). The email sweep stages inline and serially, so a 77 to 105 file
staging holds the whole intake sweep for minutes (not changed here). All
three were fixed the same day: section 10.5.

Review 2026-09-30 (adversarial pass, fixes with tests in
`tests/test_rfp_split_fallback.py` section D):

- "Run the splitter" is refused (409, `MSG_FILES_PROMOTING`) while the
  document promotion is `pending` / `running` (a stale claim excepted): the
  promotion and a run's re-file reconcile the same sandbox ids, and the
  promotion's whole row could win the unique index over the source set and
  stay in its trade category beside the new segments.
- The manual claim is fenced on `split_started_at` as well as the status
  (`_claim_manual`): a `running` harvest whose job settled without the
  write-back let two presses both pass a running -> running CAS.
- Creation settles a job that finished before it was linked
  (`rfp_create._attach_harvest`): the ladder gave up with the harvest at
  `running`, the job settled with no project, no write-back happened, and
  the harvest (and the record's copy) read `running` forever.
- A mate row at the split step never reclaims a STALE claim on a linked
  harvest (an interrupted manual run): `advance` answers `skipped` / `linked`
  without writing, so the failure is not overwritten with `skipped` (which
  would have made the flag vanish).
- The Created from RFP Ingestion card takes "Splitting" from the live flag
  when the payload carries it, not the record's denormalized split_status
  (which lags after an interrupted run).

### Split in flight: four gaps, 2026-09-30 (section 10.5)

Backend: `app/core/config.py` (`rfp_split_stage_concurrency`,
`rfp_split_staging_heartbeat_seconds`, `rfp_split_staging_stale_seconds`
and their validation), `app/services/rfp_split.py` (`stale_seconds`,
`pending_is_stale`, `_cas_claim`, `StagingClaimLost`, `_StagingClaim`,
`advance` (`renew`, the fenced put-back and reclaim, `_discard_orphans`),
`creation_must_wait` + `MSG_CREATE_WAITS`, `_start` / `_stage_one` /
`_stage_all`, `_check` (dead jobs), `_untouched`, `_active_run_targets`,
`dead_job_ids`, `_reap_dead_job`, `_reap_if_dead`, `_discard_orphans`,
`_adopt_sandbox_row` in `promote_split_file`, `split_issue` (`dead`),
`issues_for_records`, `split_running`, `MSG_RETRY_WHILE_SPLITTING`,
`start_manual_run`, `stage_for_project`), `app/services/rfp_create.py`
(`CreateWaitingForSplit`, the check in `_create`), `app/services/
rfp_email_ingest.py` (`_step_split(renew=)`, `_step_create` waits),
`app/services/rfp_portal_ingest.py` (`_step_split(renew=)`,
`_split_gate_wait`, `_step_create` waits), `app/routers/rfp_created.py`
(`retry_rfp_created_files` 409). No migration. Tests:
`tests/test_rfp_split_inflight.py` (18), two in `tests/test_rfp_create.py`,
signature updates in `tests/test_rfp_split.py` and
`tests/test_rfp_split_fallback.py`.

Frontend: `app/(app)/rfp-created/page.tsx` (Retry documents disabled with a
tooltip while the split runs); locale `rfpCreated.split.retryWhileRunning`,
English in all six catalogs. The Create project buttons (email detail,
portal invitation) already show a 409's sentence; nothing changed there.

Decided here: creation waits only on something certainly moving (the rule
in 10.5.1), so no harvest can hold creation forever. The promotion race is
fixed at the row (the re-file adopts the promotion's whole row), not by
making the promotion wait (the queue's retry ladder is too short for a long
split). A dead job's stranded files are marked failed "interrupted" rather
than re-queued automatically, so one mechanism ("Run the splitter")
recovers them after creation and the pipeline's own poll moves the row on.
(Superseded by the review below: in the pipeline they are re-queued
automatically up to the outage cap.)

Review 2026-09-30 (adversarial pass over 10.5, fixes with tests in
`tests/test_rfp_split_inflight.py` section 5; suite 5978 passed, 1 skipped):

- Verified, no change: the fence compares `split_started_at` by value, not
  by text. PostgREST casts the filter to timestamptz (the `+00:00` goes out
  as `%2B00%3A00`), dev returns microsecond stamps with trailing zeros
  trimmed ("20:27:37.9331+00:00"), and a stamp read back through
  `to_json` equals the stored value (checked read-only on dev). Python
  isoformat writes microseconds, so a written stamp matches exactly.
  Shared client from the staging workers: the one HTTP/1.1 httpx client is
  already shared by every threadpool request; each worker's scratch file
  (`{i}-{uuid}`) and object key (`uuid`-prefixed) are its own; `slots` is
  written only by the coordinator; the heartbeat runs on the coordinator,
  so there is no timer thread to outlive the staging. `_adopt_sandbox_row`
  can only match the one row the (project_id, rfp_sandbox_file_id) index
  allows; a person's upload has no sandbox id. `dead_job_ids`: a file whose
  run is queued (including waiting for a retry, which the queue keeps
  `queued`) or running (a 600-page set on the self-hosted model; a lease
  expiry requeues it, still active) is never dead.
- Medium, fixed (`_StagingClaim.beat` / `cas`): a heartbeat whose write
  landed but whose answer was lost (the SSL drops of 10.1) left the claim
  holding the old stamp, so the next beat missed and the staging discarded
  itself as "taken over", and the put-back missed too (the harvest then
  waited out the stale window). The claim now remembers the stamp it may
  have written and tries it before calling itself lost; every put-back goes
  through the claim (`claim.cas`). The put-back in `advance` no longer
  masks the staging error when it fails itself.
- Low, fixed (`_stage_all`): after the first failure the coordinator
  stopped beating while the in-flight workers drained (a 450 MB upload with
  its retries can take minutes), so the claim could age out and be
  reclaimed under them. It beats until they are joined.
- Low, fixed (`_start`): a claim lost during the enqueue loop was ignored
  (runs kept being queued for a job the harvest would never point at), and
  a failed final CAS only logged. Both now discard the job and raise
  `StagingClaimLost`.
- Medium, fixed (`creation_must_wait` / `_row_at_split`): a mate row frozen
  at `split` (an ended test session: the sweep never touches its rows; a
  sweep switched off) held creation forever, button included. See 10.5.1.
- Orchestrator A, done (`_outage_failures`): the pipeline re-queues files a
  dead job stranded (10.5.4). The manual run is unchanged.
- Orchestrator B, done (the late-link race: creation passed the wait, a
  mate reached `split` moments later and staged a job never linked to the
  project, whose documents stayed whole): the split claim in `advance` is
  fenced on `project_id is null` (a row that read the harvest before the
  link skips `linked`); `_start` re-reads `project_id` AFTER pointing the
  harvest at the job and links the job (`_link_late_project`: the created
  record's copy refreshed, files already done re-filed, a settled job
  settled onto the project); `rfp_create._attach_harvest` re-reads
  `split_job_id` AFTER writing the project link. Each side writes, then
  reads what the other writes, so one of them always links.
- Known, not changed: a restart in the middle of the enqueue loop leaves a
  job with some runs queued; the reclaim's orphan sweep leaves a job with
  active runs alone, so those runs spend model time on a job nothing files
  (clutter, no wrong result). The late link re-files files already done
  from the sweep thread (rare; a few copies per file).

---

## 10. When the split cannot finish (2026-09-30)

User decision, verbatim: "if the splitter fails, then project is created with
a flag saying the splitter failed and why, but the user can still work with
the project and decide if they want to manually run it through the splitter
in app or if they want to split it outside the application. the splitter
still needs to go through the retry after initial fails".

### 10.1 The SSL fix

Staging died on Supabase Storage uploads with `httpx` ReadError / WriteError
"[SSL: SSLV3_ALERT_BAD_RECORD_MAC] ssl/tls alert bad record mac" (Government
Center AHU, Boot Barn and the Plumas St portal invitation within one minute on
2026-09-30; Boulder Highway on 09-18). One failed upload out of 77 discarded
the whole job and spent an attempt; four of them failed the row.

- `storage.upload_file` retries `httpx.TransportError` up to
  `_TRANSFER_ATTEMPTS` (3) with `download_file`'s backoff (2 s, 4 s), a
  warning per retry. The first attempt keeps the caller's `upsert`; every
  retry upserts (the failed attempt may have landed the object, and every
  path is ours). storage3 2.30 does not wrap transport errors (its
  `_request` converts only `HTTPStatusError` to `StorageApiError`). Only
  in-memory bytes are retried.
- `storage.copy_file` (the segment copies of `_promote_segment`) retries the
  same way; a "Duplicate" (409) on a RETRY is the copy the dropped attempt
  landed, so it counts as done.
- Every other upload on the split / create path now retries:
  `_stage_row` and `_upload_original` (through `upload_file`), the whole-file
  promotion (`rfp_create_files._promote_one`, same), the sandbox buckets
  (`rfp_ingest_storage`, already), `bid_split.cut_segment` (its own 3-try
  loop around `upload_file`: up to 3 x 3 = 9 tries, about 30 s of backoff;
  kept on purpose), the downloads (already).
- `rfp_split._start` stages EVERY row first and queues the pending rows only
  once the loop has finished. Before, each row was queued as it was staged,
  so a staging exception on file N discarded the job while runs 1..N-1 were
  queued and later failed "This file no longer exists" (114 such llm_jobs in
  an hour on dev), some after spending model time.

### 10.2 The fallback at the ladder's end

The ladder is unchanged: 4 attempts, 1 / 5 / 15 min; the model away and the
sandbox still checking wait without spending one. Only when the LAST attempt
fails at `split`:

- `rfp_split.give_up` marks the harvest `split_status = failed` with
  `split_error` = "The documents could not be staged for splitting after 4
  attempts: <the error text>" and `split_finished_at` (CAS from `none`, where
  `advance` puts the claim back, or from a `pending` the put-back missed).
  A harvest at `running` (the job exists, only the check failed) is left
  alone: creation links the job, each file that ends re-files the project
  and the settle writes the outcome back (10.4).
- The row moves `split -> create` with attempts reset, exactly like a
  terminal split outcome (`decided_at_step = split`, flag_reason kept), and
  the reason in `last_error` until the project exists. Email:
  `rfp_email_ingest._split_gave_up`; portal: `rfp_portal_ingest._split_gave_up`
  instead of `_hold_at_cap` (the other portal steps still hold). A portal row
  an older build held at the cap falls through on its next failure.
- A harvest-lookup failure at the cap does the same when the row has a
  harvest id; if the harvest cannot be written either, the row still moves
  on and creation retries on its own ladder (no second mechanism).
- Bench event `split.gave_up {harvest_id, attempts, error, reason,
  marked_failed, next: "create"}` (level error). No IT Admin alert: nothing
  failed, the project carries the flag.
- Creation then runs as always (auto-create, or the Create project button):
  with no split job every verified document is promoted WHOLE through the
  pre-split mapping.

A job that ends `failed` (every staged file failed) already moved on; its
`split_error` now names the reasons (`failure_reason`: "Every document failed
to split (<shared reason>)." or "...: a.pdf: <reason>; b.pdf: <reason>.").
A job that ends `done_with_errors` is a PARTIAL failure: the files that failed
(for example "The PDF has 674 pages; the splitter limit is 600 pages per
file.") were promoted whole, and the project carries the flag listing them.
`skipped` (flags_off, queue_off, no_files, linked) is never a splitter
failure: the documents-skipped and missing-files flags cover those.

### 10.3 The flag

Computed live, never stored (`rfp_split.split_issue` over the harvest, its
job and the job's files; `issues_for_records` batches it, one query per
table):

```
split_issue = null | {
  state: failed | partial | running,
  reason,                       -- the harvest's split_error (failed only)
  job_id, files_total, files_done, files_failed_count,
  files_failed: [{file_id, filename, error}],   (first 100)
  started_at, finished_at,
  resolution: null | {kind: "outside", by: {id, full_name}, at},
  package_sent                  -- the hand-off lock
}
```

- `GET /projects/{id}` (detail only): `rfp_created.split_issue`. The project
  page shows `RfpSplitIssueCallout` under the files-needed callout, to every
  internal role; the actions to the Created from RFP Ingestion roles.
- `GET /rfp-created` items: `flags.split_issue`, `flags.split_partial`, and
  `flags.split_failed` now true for any outright failure (staging gave up,
  every file failed). The card shows "Split failed" / "Split incomplete (n of
  m)" / "Split outside the app" with the sentence under the flags and the
  actions in the row.
- Wording: "The Bid File Splitter failed on this project: <reason>. All
  documents were added whole, not split by trade." / "The Bid File Splitter
  could not split N of M files. Those files were added whole." then one line
  per file "<name>: <reason>".
- The "Project created" bells (Executive and Estimating Admin, email
  mirrored) append `rfp_split.creation_note`: "The Bid File Splitter failed
  on it (<reason>); every document was added whole." or "... could not split
  N of M files; those files were added whole."; metadata `split_note`.

### 10.4 The two actions

Permission: the Created from RFP Ingestion roles (`require_page`: Estimating
Admin, Executive, IT Admin), the same as Clear / Restore / Retry documents.

`POST /rfp-created/{project_id}/split/run` (202, `ai_rate_limit`), "Run the
splitter". Refused 409 while the splitter or the queue is off, the project is
marked split outside the app, its hand-off package has sent
(`MSG_PACKAGE_SENT`, the corrections rule), a run is going, there is nothing
to re-run (no flag, or only page-cap failures: "split them outside the app"),
or the model is away (its sentence). Otherwise, claimed on the harvest's
split_status by a CAS from the status just read:

- `requeue`: the harvest has a job with failed files. Those files (not the
  ones over the page cap) go `pending` and are queued again on that job,
  which is linked to the project (`bid_split_jobs.project_id`); harvest
  `running`.
- `stage`: no job (staging gave up): harvest `pending`, and
  `rfp_split.stage_for_project` runs in the background (FastAPI
  BackgroundTasks, the splitter router's own fallback pattern): the
  pipeline's `_start` stages a fresh job born linked to the project, with
  the person as `created_by` (they get the splitter's finished bell). A
  staging exception marks the harvest failed with "The documents could not
  be staged for splitting: <error>"; nothing staged marks it failed too.

Answer `{mode, job_id, files, split}`; audited `rfp_created.split_run`.

The re-file: each file that finishes `done` calls `resync_after_run` (the
worker's `_after_done`), which skips a sent project and otherwise runs
`resync_project_files` -> `promote_split_file`. A project promoted whole
before any job existed has one row per file carrying ONLY the sandbox id;
`rows_for_file` matches it by `rfp_sandbox_file_id`, so a cut file turns that
row into the source set in place (no second upload) and adds the segments,
and an intact file gives that row its category and segment id. When the job
settles, `bid_split.refresh_job` calls `rfp_split.settle_linked_job` (only
for an `rfp` job whose project exists): the harvest pointing at the job goes
`complete` / `failed` exactly as `_check` grades it, and
`rfp_created_projects.split_status / split_job_id` follow (`sync_record`).
The flag clears on full success and shows the new reason otherwise. A retry
in the splitter UI after creation settles the same way.

`POST /rfp-created/{project_id}/split/outside`, "I'll split it outside the
app": 409 when there is no open flag or it is already marked; writes
`split_resolution = 'outside'`, `split_resolved_by`, `split_resolved_at`
(migration `0143_rfp_split_resolution.sql`); audited
`rfp_created.split_outside`. The banner folds into "Marked as split outside
the app by <name> on <date>." with an Undo. The person uploads the split
files through the normal upload flow.

`DELETE /rfp-created/{project_id}/split/outside`, Undo: clears the three
columns; audited `rfp_created.split_outside_undo`.

### 10.5 A split in flight (2026-09-30)

Four gaps around a split that is still going, closed without a migration.

#### 10.5.1 Creation waits for the split

Several rows can share one harvest (a reminder, a second copy). Pressing
Create project on a `done` copy while the leader row was still at `split`
built the project from whole, unsplit files, and the split for that harvest
then never re-filed them the normal way. `rfp_create._create` now asks
`rfp_split.creation_must_wait(sb, harvest, exclude=(table, row id))` right
after the linked-harvest check (before the harvest claim) and raises
`CreateWaitingForSplit` (a `CreateRefused`) with "The documents for this
invitation are still being split. The project will be created when the split
finishes."

The rule (every wait ends when something certainly moving finishes, so no
harvest can hold creation forever):

- never: no harvest, the harvest already has a project (the row links), the
  split step off (`BID_FILE_SPLITTER_ENABLED` or `RFP_SPLIT_ENABLED`), the
  queue off, or a terminal split (complete / failed / skipped; the leader
  that just finished its own split creates in the same pass);
- wait while a staging claim is live (`pending` with a fresh heartbeat);
- otherwise (`none`, `running`, a stale `pending`) wait only while an email
  or portal invitation sharing the harvest (not the creating row) sits at
  `split`, since that row moves the split along. No row at `split` means
  nothing will ever move a `none` harvest (the flags came on later, a
  harvest older than the step) or grade a `running` one (the ladder gave up
  on its check and the leader moved on): creation proceeds and links the
  running job (10.2). A `none` harvest with no harvested entries never waits.
  A row at `split` counts only while a sweep still visits it: its
  `next_attempt_at` is ahead, or its `updated_at` is within the stale window
  plus the longest wait interval (10 minutes by default). A row nothing
  visits any more (an ended test session's frozen rows, a sweep switched
  off) no longer holds creation forever (review 2026-09-30).

Button path: 409 `rfp_create_refused` with the sentence (both routers already
map `CreateRefused`; the email detail and portal invitation dialogs show a
409's sentence). Sweep path (email and portal `_step_create`): the row stays
at `create`, `next_attempt_at = now + RFP_SPLIT_POLL_SECONDS`, the sentence
in `last_error`, no attempt spent (a bench row gets a `create.waiting`
event). A read failure in the check never blocks creation.

Known cost: while the splitter's model is away, the leader waits at `split`
without spending attempts, so Create on its copy waits too.

#### 10.5.2 Concurrent staging

`_start` stages RFP_SPLIT_STAGE_CONCURRENCY entries at once (default 4,
validated >= 1) on a `ThreadPoolExecutor` (`_stage_all` / `_stage_one`): each
worker fetches one verified document, checks it, uploads it and inserts its
`bid_split_files` row. Kept exactly: all or nothing (the first failure stops
new entries, the ones in flight are joined, then the job and its objects are
discarded and the exception re-raised for the ladder), the per-job cap,
first-entry-wins dedupe (`_entries`), `staged_names` in entry order, nothing
queued until every entry is staged, the scratch directory removed.
`page_count` uses pypdf with one reader per call and never touches PDFium
(the renderer's `_PDFIUM_LOCK` stays the only PDFium entry point).

Memory: a worker holds one document's bytes (plus a converted PDF for an
office file) only while it checks and uploads it; nothing is kept after its
row is inserted. Peak is about concurrency x the largest documents in
flight: 4 x 450 MB (UPLOAD_MAX_BYTES) worst case, typically 4 x 20 to 100 MB.
Lower RFP_SPLIT_STAGE_CONCURRENCY on a small container.

The sweep lease: the email and portal sweeps pass their lease renewal into
`advance(renew=)`, and every staging heartbeat calls it, so a long staging
keeps the lease (RFP_EMAIL_INGESTION_LEASE_SECONDS, 600 by default, is far
above the 30 s heartbeat). A failed renewal is only logged: the harvest
claim, not the lease, fences the staging.

#### 10.5.3 Retry documents during a split

`POST /rfp-created/{id}/retry-files` answers 409 "The Bid File Splitter is
still running on this project. Retry documents once it finishes." while the
split runs (`rfp_split.split_running`: the flag reads `running`, that is a
staging claim with a fresh heartbeat or a job processing that is not dead).
The Created from RFP Ingestion card disables the button with that sentence
as its tooltip.

The automatic promotion can still overlap a linked job (creation after the
ladder gave up with the job still running). The race was real: the re-file
read the project's rows, the promotion inserted the whole file, and the
re-file's source-set insert lost the unique index on (project_id,
rfp_sandbox_file_id), leaving the whole file in its trade category beside
the new segments. `promote_split_file` now adopts that row in place
(`_adopt_sandbox_row`): the source set for a cut file, the splitter's
category and segment for an intact one. Every other interleaving already
ended right (the promotion's own insert loses the index to the re-file's
row and counts it).

#### 10.5.4 Heartbeat, staleness and dead jobs

Settings: RFP_SPLIT_STAGING_HEARTBEAT_SECONDS (30, >= 5) and
RFP_SPLIT_STAGING_STALE_SECONDS (300, >= 3 x the heartbeat).

- The staging claim (`_StagingClaim`) re-stamps `split_started_at` from the
  staging loop: `_stage_all` waits on its workers in slices of the
  heartbeat and beats after each slice (a file finishing or the interval
  passing), and the enqueue loop beats too. Every write is a CAS on
  (`pending`, our last stamp): a claim another worker took over fails the
  beat, the staging stops (`StagingClaimLost`) and discards its job. A
  transport error on the beat is not a lost claim. So a live staging, however
  slow, is never stale, and a dead one (the server restarted) is stale within
  five minutes instead of an hour.
- `pending_is_stale` uses the stale window everywhere: the pipeline's own
  reclaim in `advance` (now fenced on the stamp it read, so a beat that just
  landed wins), the project flag (`split_issue`: "interrupted", and "Run the
  splitter" is offered again) and `creation_must_wait`. The put-backs in
  `advance` and the failure mark in `stage_for_project` are fenced on the
  claim's last stamp.
- The reviewer's double-press fence (`_claim_manual`, on the stamp) still
  holds: a press while a staging is live is refused because the flag reads
  `running`; `stage_for_project` takes the claim over with a forced beat
  first, so a second task that read the same claim stands down.
- Orphans: a staging killed before it queued anything leaves a job the
  harvest never pointed at. The worker that reclaims a stale claim, the
  manual stage run, and a manual requeue from a stale `pending` discard the
  harvest's other `processing` rfp jobs that have no queue run active
  (`_discard_orphans`).
- Dead jobs (`dead_job_ids`): `processing`, untouched (updated_at, else
  created_at) for the stale window, every non-terminal file untouched as
  long, no `llm_jobs` run queued or running for any of them, and the harvest
  not at a fresh `pending`. Missing stamps never read as dead; a slow file
  with its queue run active is never dead. This is a restart between a
  file's `pending` mark and its enqueue (the outage requeue, a manual run's
  requeue). `_reap_dead_job` marks the stranded files failed "The split was
  interrupted (the server restarted). Run it again." and lets the job settle
  through `bid_split.refresh_job` (a linked job writes its harvest back).
  The pipeline's `_check` reaps and, in the same poll, queues the
  interrupted files again through the outage requeue (`_outage_failures` /
  `_requeue_after_outage`, the model-away wait included) up to
  `_OUTAGE_RUNS_MAX` queue runs per file, so a restart needs no person;
  past that cap they stay failed, the row moves on and the project shows
  the partial flag (review 2026-09-30). After creation, the project flag
  reads such a job as `failed` with that sentence before anyone acts, and
  "Run the splitter" reaps it and queues the interrupted files again.

The portal's split step now treats our own AI gate being busy
(`rfp_email_ingest.gate_busy`) as a wait (the model-wait interval, no
attempt spent, never a give-up), as the email side does.
