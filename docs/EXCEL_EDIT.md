# Edit in Excel

Internal users edit `.xlsx` / `.xlsm` project files in Excel for the web, in a
new browser tab. Excel autosaves; the app pulls the changes back on its own.
Each editing session saves one new version of the file and every earlier
version is kept.

Migration `0193_excel_edit.sql` (0192 is reserved by the assistant browser v2
plan). Service `app/services/excel_edit.py`, routes `app/routers/file_edit.py`,
tests `tests/test_excel_edit.py`.

## 1. Who and what

- Internal writer roles only (`WRITER_ROLES`). The accountant can see the
  status and the version list but cannot edit. The estimator never reaches
  the routes.
- Bidding project files (`project_files`) ending `.xlsx` or `.xlsm`. `.xls`
  is not supported (Excel for the web would have to convert it). Macros in
  `.xlsm` do not run in the browser but are kept in the file.
- Refused, mirroring the delete route's evidence rules:
  - an initial-package file (`INITIAL_CATEGORIES`) once the hand-off lock is on;
  - an update or addendum already emailed to estimators;
  - proposals;
  - files larger than `UPLOAD_MAX_BYTES`.
- Allowed on estimator deliverables (`boq`, `estimate`, `markup`). The
  estimator's own upload stays as version 1. Note the estimator portal then
  shows the edited workbook, since it reads the file's current content.

## 2. Flow

1. **Start** (`POST /projects/{p}/files/{f}/excel-edit`). The current object is
   uploaded to the `MS_DRIVE_OWNER` OneDrive at
   `EXCEL_EDIT_DRIVE_FOLDER/<session id>/<filename>`. The user gets write access
   on that one copy (Graph driveItem `invite`, `requireSignIn`, no invitation
   email), so their BDR sign-in email must be their G3 Microsoft 365 account.
   The response carries the copy's `webUrl`, which the frontend opens in a new
   tab. If the file already has an open session, the caller is granted access
   and joins it (Excel co-authoring).
2. **Sync** (poller, every `EXCEL_EDIT_POLL_SECONDS`, default 30). For each
   open session, under a per-session lease (`lease_until`, so both uvicorn
   workers can run the loop), the copy's `cTag` is compared with the last one
   pulled. When it moved, the bytes are downloaded, checked to be a zip
   container, hashed, and written back if the hash differs from the file's.
3. **Versions.** The first change of a session records the file's prior
   content as version 1 (`source = original`) when the file has no versions
   yet, then writes the new bytes to a fresh object as the next version
   (`source = excel_edit`) and points `project_files.storage_path` (plus
   `size_bytes`, `sha256`) at it. Later changes in the same session replace
   that version's object; the replaced objects are listed in
   `superseded_paths` and deleted when the session closes, so a link signed
   mid-session keeps working until then. Version objects are never deleted
   while the file exists.
4. **Preview.** Every write regenerates the PDF derivative to a fresh path
   (`generate_preview(fresh_path=True)`) and deletes the old one, so memoized
   signed URLs and CDN caches never serve a stale preview.
5. **Close.** `POST .../excel-edit/finish` (Finish editing) pulls once more and
   closes. The poller closes a session with no change for
   `EXCEL_EDIT_IDLE_MINUTES` (default 60). Closing deletes the OneDrive copy,
   so an Excel tab still open on it can no longer save. A deleted file
   (`file_id` set null) or a copy deleted from OneDrive also closes the session.
   The next Edit in Excel starts a new session, so a new version.

## 3. Routes

| Route | Who | Returns |
| --- | --- | --- |
| `GET  /projects/{p}/files/{f}/excel-edit` | internal | `editable`, `reason`, `current_version_no`, `session` |
| `POST /projects/{p}/files/{f}/excel-edit` | writer | `edit_url`, `session` |
| `POST /projects/{p}/files/{f}/excel-edit/finish` | writer | `changed`, `version_no`, `session` |
| `GET  /projects/{p}/files/{f}/versions` | internal | `versions[]`, newest first, `current` marked |
| `GET  /projects/{p}/files/{f}/versions/{v}/download` | internal | signed attachment `url`, `filename` |

`GET /features` gains `excel_edit` (bidding on, feature on, writer role).

Audit actions: `file.excel_edit_started`, `file.excel_edit_version`,
`file.excel_edit_finished`. Starting a session also records a `view` in the
file access log with `detail.excel_edit = true`.

## 4. Configuration

| Setting | Default | Notes |
| --- | --- | --- |
| `EXCEL_EDIT_ENABLED` | `false` | Feature and poller. Also needs Graph creds (`MS_CLIENT_ID`...). |
| `EXCEL_EDIT_POLL_SECONDS` | `30` | Poller interval. |
| `EXCEL_EDIT_IDLE_MINUTES` | `60` | Auto-close after this long with no change. |
| `EXCEL_EDIT_DRIVE_FOLDER` | `BDR/excel-edit` | Folder in the drive owner's OneDrive. |

Graph permission: `Files.ReadWrite.All` (application), already granted for the
RFQ drawing links and the Graph preview engine. `MS_DRIVE_OWNER` must be a
licensed account with a provisioned OneDrive.

## 5. Release notes

- Apply `0193_excel_edit.sql` (dev applied 2026-11-02), then
  `notify pgrst, 'reload schema'`.
- Set `EXCEL_EDIT_ENABLED=true` on the API service. No frontend env change.
- Live-check once per environment: open a workbook, type in a cell, wait for
  "Last saved to BDR", Finish editing, confirm the preview and the Versions
  list show v2.

## 6. Known limits

- Edits are pulled back about every 30 seconds. Finish editing pulls at once,
  but Excel itself saves a few seconds after typing, so finishing right after
  a keystroke can miss that last change.
- Content edits do not re-run anything derived from the file earlier (BOQ
  extraction, RFQ splits already made, quotes parsed). Re-run those by hand
  if needed.
- Deleting a whole project removes its storage objects; an OneDrive copy of a
  session open at that moment is left behind (rows cascade away).
